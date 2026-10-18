"""📼 15 dakikalık bant — her dinlenen coinde son 15 dk'nın BÜTÜN alım-satımları, adresiyle (coin sayfası, /defter).

Kullanıcı (18.10): "sitemde pump yazıp girdiğimde son 15dk alımlar satımlar hepsi, orderbookta en kalın emirlerin
sahipleri, gelmiş geçmiş büyük twap emirleri en üstte olsun".

Kararlar:
  • Kaynak kollektörün ZATEN aldığı canlı işlem akışı (WS `trades`, users:[alıcı, satıcı]) — ek HL isteği yok. Sıcak
    yol: senkron, I/O yok, asla fırlatmaz (çağıran ayrıca sarar).
  • Bellek sınırlı: işlemler tek tek saklanmaz; dakikalık kovalar (en çok WINDOW/BUCKET) — kova başına toplam alış/satış,
    adres başına alış / satış / taker / TWAP $ (ADDR_CAP), en büyük TOP_PER_BUCKET işlem ve pasif emri dolan adresler
    fiyat kademesiyle (MAKER_CAP). Tavanı aşan adres/kademe "diğer"e sayılır — toplamlar yine doğru.
  • HL'de `side` AGRESÖRÜ söyler: B = alıcı süpürdü (satıcı pasif, ask'taki emri doldu), A = satıcı süpürdü (alıcı
    pasif, bid'deki emri doldu). Pasif tarafın adresi = o fiyattaki bekleyen emrin SAHİBİ (defter anonim; dolan emir
    sahibini ele verir) → bookowners bunu kullanır.
  • Sıfır hash'li dolum HL TWAP alt emridir (twapfast.is_twap_hash) — işlem satırında 🔹 işareti.
  • Restart bellekteki bandı sıfırlar; canlı akış kopabilir. Özet kapsamayı ölçer: açılıştan önceki kısım, kollektörün
    kopuk kaldığı aralıklar (`down_log`) ve şu anki kopukluk (`down_since`) → `gaps`, `covered_sec`, `live_down`.
    Kopukken "işlem görülmedi" değil "bilinmiyor" yazılır — tahmin yok.
  • HL abonelikte (yeniden bağlanınca da) coinin son 30 işlemini yeniden yollar (inceleme 18.10, canlı ölçüldü):
    coin başına yüksek-su işareti + aynı saniyede tid (openmove._dup ile aynı) → hiçbir işlem iki kez sayılmaz.
  • Pencere: yalnız TAMAMI son 15 dk'da kalan kovalar sayılır (yarım dakika 30 sn kova) → kapsam 14,5–15 dk, asla fazla.
  • 2. inceleme (18.10): açılıştan ÖNCEKİ işlem sayılmaz (abonelikte yollanan son 30 işlem "bot N dk önce başladı,
    öncesi yok" etiketiyle çelişiyordu); restart etiketi açılış anından, sonraki kopukluk ayrıca yazılır; bağlı görünen
    ama SILENT_SEC'tir mesaj almayan akış (yarı-açık, 45 sn'lik ping'e pong gelmiyor) şu an kopuk sayılır.
"""
from __future__ import annotations

import heapq
import time
from collections import deque

WINDOW = 900                 # 15 dk
BUCKET = 30                  # yarım dakikalık kova (pencere en çok bu kadar kısalır, asla uzamaz)
TOP_PER_BUCKET = 40          # kova başına en büyük işlem
ADDR_CAP = 4000              # kova başına adres (BTC gibi coinlerde dakikada binlerce adres olabilir)
MAKER_CAP = 3000             # kova başına (yön, fiyat, adres) pasif dolum kaydı
COIN_CAP = 600               # en çok bu kadar coin (kollektör ~160 dinler)
SILENT_SEC = 60              # bağlı ama bu kadar mesaj yok (ping 45 sn'de bir) → akış kopuk sayılır
STARTUP_GRACE = 120          # açılıştan bu kadar içinde kapanan boşluk (açılış → ilk abonelik) kopukluk sayılmaz

STARTED = int(time.time())
STATS: dict = {"trades": 0, "errors": 0, "coins": 0, "dups": 0, "late": 0, "pre": 0}


class _Bucket:
    __slots__ = ("minute", "buy", "sell", "n", "twap", "addrs", "big", "makers", "other_addr", "other_maker")

    def __init__(self, minute: int):
        self.minute = minute
        self.buy = 0.0            # agresör alıcı ($)
        self.sell = 0.0           # agresör satıcı ($)
        self.n = 0
        self.twap = 0.0           # TWAP alt emri dolumları ($)
        self.addrs: dict[str, list] = {}       # adres → [alış $, satış $, taker $, twap $, işlem]
        self.big: list[tuple] = []             # min-heap: (usd, ts, px, aggr, alıcı, satıcı, twap)
        self.makers: dict[tuple, float] = {}   # (defter yönü 'bid'/'ask', px, adres) → pasif dolan $
        self.other_addr = 0.0
        self.other_maker = 0.0


REG: dict[str, deque] = {}
HWM: dict[str, tuple[int, set]] = {}     # coin → (en yeni saniye, o saniyede görülen tid'ler)


def _dup(coin: str, ts: int, tid: str) -> bool:
    """Yeniden yollanan işlem mi? HL akışı coin başına artan sırada; yeniden bağlanınca son 30 işlem yine gelir.
    En yeni andan eskisi zaten görüldü, aynı saniyede tid bakılır. tid yoksa (test / eski çağrı) süzülmez."""
    if not tid:
        return False
    h = HWM.get(coin)
    if h is not None:
        if ts < h[0]:
            return True
        if ts == h[0]:
            if tid in h[1]:
                return True
            h[1].add(tid)
            return False
    HWM[coin] = (ts, {tid})
    return False


def _bucket(coin: str, ts: int) -> _Bucket | None:
    q = REG.get(coin)
    if q is None:
        if len(REG) >= COIN_CAP:
            return None
        q = REG[coin] = deque()
        STATS["coins"] = len(REG)
    minute = int(ts) // BUCKET
    if q and q[-1].minute == minute:
        return q[-1]
    if q and minute < q[-1].minute:                  # geç gelen (sıra dışı) işlem: yalnız kendi kovası varsa oraya
        for b in reversed(q):
            if b.minute == minute:
                return b
            if b.minute < minute:
                break
        STATS["late"] += 1
        return None                                  # kovası yok: yanlış (daha yeni) kovaya yazmaktansa sayma
    b = _Bucket(minute)
    q.append(b)
    keep = WINDOW // BUCKET + 1
    while len(q) > keep or (q and q[0].minute < minute - keep + 1):
        q.popleft()
    return b


def _add_addr(b: _Bucket, addr: str, buy: float, sell: float, taker: float, twap: float) -> None:
    rec = b.addrs.get(addr)
    if rec is None:
        if len(b.addrs) >= ADDR_CAP:
            b.other_addr += buy + sell
            return
        rec = b.addrs[addr] = [0.0, 0.0, 0.0, 0.0, 0]
    rec[0] += buy
    rec[1] += sell
    rec[2] += taker
    rec[3] += twap
    rec[4] += 1


def observe(coin: str, aggr: str, buyer: str, seller: str, px: float, sz: float, ts: int,
            twap: bool = False, tid: str = "") -> bool:
    """Tek işlem (kollektörün sıcak yolu). `aggr`: 'B' (alıcı agresör) / 'A' (satıcı agresör) / bilinmiyor.
    `tid`: HL işlem kimliği — yeniden yollanan işlem (yeniden bağlanma) ikinci kez sayılmaz.
    Dönüş: işlem bantta SAYILDI mı (📟 canlı akış yalnız sayılanı yayar — tekrar / açılış öncesi gitmez)."""
    usd = float(px) * float(sz)
    if usd <= 0 or not coin:
        return False
    if int(ts) < STARTED:                            # abonelikte yollanan açılış öncesi işlem: "öncesi yok" doğru kalsın
        STATS["pre"] += 1
        return False
    if _dup(coin, int(ts), str(tid or "")):
        STATS["dups"] += 1
        return False
    b = _bucket(coin, ts)
    if b is None:
        return False
    STATS["trades"] += 1
    b.n += 1
    if aggr == "B":
        b.buy += usd
    elif aggr == "A":
        b.sell += usd
    tw = usd if twap else 0.0
    b.twap += tw
    taker_buy = usd if aggr == "B" else 0.0
    taker_sell = usd if aggr == "A" else 0.0
    if buyer:
        _add_addr(b, buyer, usd, 0.0, taker_buy, tw if aggr == "B" else 0.0)
    if seller:
        _add_addr(b, seller, 0.0, usd, taker_sell, tw if aggr == "A" else 0.0)
    item = (usd, int(ts), float(px), aggr, buyer, seller, bool(twap))
    if len(b.big) < TOP_PER_BUCKET:
        heapq.heappush(b.big, item)
    elif usd > b.big[0][0]:
        heapq.heapreplace(b.big, item)
    maker = seller if aggr == "B" else (buyer if aggr == "A" else "")
    if maker:
        key = ("ask" if aggr == "B" else "bid", float(px), maker)
        if key in b.makers or len(b.makers) < MAKER_CAP:
            b.makers[key] = b.makers.get(key, 0.0) + usd
        else:
            b.other_maker += usd
    return True


def _gaps(lo: int, hi: int) -> tuple[list[list[int]], bool]:
    """[lo, hi] içinde bandın görmediği aralıklar: açılıştan önce + kollektörün kopuk kaldığı süreler (down_log) +
    şu anki kopukluk (down_since → şimdi). (birleşik aralıklar, şu an kopuk mu). openmove.coverage ile aynı ölçü."""
    raw: list[tuple[int, int]] = []
    if STARTED > lo:
        raw.append((lo, STARTED))
    down_now = False
    try:
        from ..hl import collector as col
        live = col.LIVE
    except Exception:                                   # noqa: BLE001
        live = None
    if live is not None:
        for a, b in list(getattr(live, "down_log", None) or ()):
            raw.append((int(a), int(b)))
        down = float(getattr(live, "down_since", 0) or 0)
        last = float(getattr(live, "last_msg_at", 0) or 0)
        if not down and getattr(live, "connected", False) and last and hi - last > SILENT_SEC:
            down = last                                 # yarı-açık: bağlı görünüyor ama mesaj yok
        if down:
            down_now = True
            raw.append((int(down), max(hi, int(down))))
    gaps: list[list[int]] = []
    for a, b in sorted((max(a, lo), min(b, hi)) for a, b in raw if b > lo and a < hi):
        if gaps and a <= gaps[-1][1]:
            gaps[-1][1] = max(gaps[-1][1], b)
        elif b > a:
            gaps.append([a, b])
    return gaps, down_now


def summary(coin: str, now_ts: int | None = None, window: int = WINDOW, top: int = 30, top_addr: int = 12) -> dict:
    """Son `window` sn: toplamlar, en büyük işlemler, en çok alan / satan adresler (net), pasif dolumlar kademeyle.
    Yalnız TAMAMI pencerede kalan kovalar sayılır (kapsam window−BUCKET … window, asla fazla). Kapsama ölçülür:
    `span` (kovaların kapsadığı sn), `gaps` (açılış öncesi / kopukluk), `covered_sec` (span − boşluk), `restart`
    (bot pencerenin içinde açıldı), `live_down` (akış şu an kopuk → "işlem yok" denemez, bilinmiyor).
    Veri yoksa {"n": 0, ...} (coin dinlenmiyor ya da sessiz)."""
    t = int(now_ts if now_ts is not None else time.time())
    lo = -(-(t - window) // BUCKET)                     # tavan: pencerenin dışına taşan kova sayılmaz
    start = lo * BUCKET
    q = [b for b in REG.get(coin) or () if b.minute >= lo]
    buy = sum(b.buy for b in q)
    sell = sum(b.sell for b in q)
    addrs: dict[str, list] = {}
    for b in q:
        for a, r in b.addrs.items():
            x = addrs.setdefault(a, [0.0, 0.0, 0.0, 0.0, 0])
            for i in range(5):
                x[i] += r[i]
    big = sorted((it for b in q for it in b.big), key=lambda it: -it[0])[:top]
    makers: dict[tuple, float] = {}
    for b in q:
        for k, v in b.makers.items():
            makers[k] = makers.get(k, 0.0) + v
    flows = [{"addr": a, "buy": r[0], "sell": r[1], "net": r[0] - r[1], "taker": r[2], "twap": r[3], "n": r[4]}
             for a, r in addrs.items()]
    buyers = sorted((f for f in flows if f["net"] > 0), key=lambda f: -f["net"])[:top_addr]
    sellers = sorted((f for f in flows if f["net"] < 0), key=lambda f: f["net"])[:top_addr]
    span = max(0, t - start)
    gaps, down_now = _gaps(start, t)
    gap_sec = sum(b - a for a, b in gaps)
    restart = STARTED > start
    since_start = max(0, t - STARTED)
    # açılıştan sonraki kopukluk (açılış → ilk abonelik boşluğu hariç)
    # (yalnız açılıştan kısa sürede kapanan açılış → abonelik boşluğu sayılmaz; uzun sürdüyse tümü sayılır)
    later = sum(min(b, t) - max(a, STARTED) for a, b in gaps if b > STARTED + STARTUP_GRACE)
    return {
        "coin": coin, "n": sum(b.n for b in q), "buy": buy, "sell": sell, "net": buy - sell,
        "twap": sum(b.twap for b in q), "n_addr": len(addrs),
        "trades": [{"usd": it[0], "ts": it[1], "px": it[2], "side": "buy" if it[3] == "B" else (
                    "sell" if it[3] == "A" else ""), "taker": it[4] if it[3] == "B" else (it[5] if it[3] == "A" else ""),
                    "maker": it[5] if it[3] == "B" else (it[4] if it[3] == "A" else ""), "twap": it[6]} for it in big],
        "buyers": buyers, "sellers": sellers,
        "makers": makers,                       # (yön, px, adres) → $ — bookowners
        "other": sum(b.other_addr for b in q),
        "window": window, "span": span, "gaps": gaps, "gap_sec": gap_sec,
        "covered_sec": max(0, span - gap_sec), "restart": restart, "since_start": since_start,
        "later_gap": later if restart else gap_sec,
        "live_down": down_now, "full": not gaps,
        "first_ts": q[0].minute * BUCKET if q else None,
    }


def cover(summ: dict | None) -> dict:
    """Özetin kapsaması, metin için (Telegram ve sayfa aynı ölçüyü yazsın):
    {"label": "son 15 dk" | "son 7 dk", "warn": uyarı ya da "", "unknown": işlem yok denemez mi}."""
    s = summ or {}
    win = int(s.get("window") or WINDOW)
    span = int(s.get("span") if s.get("span") is not None else win)
    if s.get("live_down"):
        return {"label": f"son {win // 60} dk", "warn": "canlı akış şu an kopuk — bu aralık bilinmiyor",
                "unknown": True}
    gap_txt = lambda g: f"{g} sn" if g < 120 else f"{g // 60} dk"   # noqa: E731
    if s.get("restart"):
        since = int(s.get("since_start") if s.get("since_start") is not None else (s.get("covered_sec") or 0))
        ago = f"{since // 60} dk" if since >= 60 else f"{since} sn"
        warn = f"bot {ago} önce yeniden başladı, öncesi bantta yok"
        later = int(s.get("later_gap") or 0)
        if later > 0:
            warn += f"; canlı akış ayrıca {gap_txt(later)} kopuktu, o süre eksik olabilir"
        return {"label": f"son {max(1, since // 60)} dk", "warn": warn, "unknown": False}   # uyarıyla aynı ölçü
    gap = int(s.get("gap_sec") or 0)
    if gap > 0:
        return {"label": f"son {win // 60} dk",
                "warn": f"canlı akış bu aralıkta {gap_txt(gap)} kopuktu, o süre eksik olabilir", "unknown": False}
    return {"label": f"son {max(1, -(-span // 60))} dk", "warn": "", "unknown": False}


def makers_near(summ: dict, side: str, px_lo: float, px_hi: float) -> list[tuple[str, float]]:
    """Özet içinden: defterin `side` ('bid'/'ask') yönünde [px_lo, px_hi] aralığında pasif emri dolan adresler
    (son 15 dk) → [(adres, dolan $)], büyükten küçüğe."""
    acc: dict[str, float] = {}
    for (sd, px, addr), usd in (summ.get("makers") or {}).items():
        if sd == side and px_lo <= px <= px_hi:
            acc[addr] = acc.get(addr, 0.0) + usd
    return sorted(acc.items(), key=lambda kv: -kv[1])


def diag_line() -> str:
    mem = sum(len(b.addrs) + len(b.makers) + len(b.big) for q in REG.values() for b in q)
    return (f"📼 15 dk bant: {STATS['coins']} coin · {STATS['trades']:,} işlem (açılıştan beri)"
            f" · {mem:,} kayıt bellekte"
            + (f" · {STATS['dups']:,} yeniden yollanan işlem atıldı" if STATS["dups"] else "")
            + (f" · {STATS['late']:,} geç işlem sayılmadı" if STATS["late"] else "")
            + (f" · {STATS['pre']:,} açılış öncesi işlem sayılmadı" if STATS["pre"] else "")
            + (f" · ⚠️ {STATS['errors']} hata" if STATS["errors"] else ""))
