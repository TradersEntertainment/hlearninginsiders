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
  • Restart bellekteki bandı sıfırlar: özet kapsamayı (`covered_sec`) yazar — tahmin yok.
"""
from __future__ import annotations

import heapq
import time
from collections import deque

WINDOW = 900                 # 15 dk
BUCKET = 60                  # dakikalık kova
TOP_PER_BUCKET = 40          # kova başına en büyük işlem
ADDR_CAP = 4000              # kova başına adres (BTC gibi coinlerde dakikada binlerce adres olabilir)
MAKER_CAP = 3000             # kova başına (yön, fiyat, adres) pasif dolum kaydı
COIN_CAP = 600               # en çok bu kadar coin (kollektör ~160 dinler)

STARTED = int(time.time())
STATS: dict = {"trades": 0, "errors": 0, "coins": 0}


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
    if q and minute < q[-1].minute:                  # geç gelen (sıra dışı) işlem: kendi kovası varsa oraya
        for b in reversed(q):
            if b.minute == minute:
                return b
            if b.minute < minute:
                break
        return None if minute <= q[0].minute - 1 else q[-1]
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
            twap: bool = False) -> None:
    """Tek işlem (kollektörün sıcak yolu). `aggr`: 'B' (alıcı agresör) / 'A' (satıcı agresör) / bilinmiyor."""
    usd = float(px) * float(sz)
    if usd <= 0 or not coin:
        return
    b = _bucket(coin, ts)
    if b is None:
        return
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


def summary(coin: str, now_ts: int | None = None, window: int = WINDOW, top: int = 30, top_addr: int = 12) -> dict:
    """Son `window` sn: toplamlar, en büyük işlemler, en çok alan / satan adresler (net), pasif dolumlar kademeyle.
    Veri yoksa {"n": 0, ...} (coin dinlenmiyor ya da sessiz)."""
    t = int(now_ts if now_ts is not None else time.time())
    lo = (t - window) // BUCKET
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
    return {
        "coin": coin, "n": sum(b.n for b in q), "buy": buy, "sell": sell, "net": buy - sell,
        "twap": sum(b.twap for b in q), "n_addr": len(addrs),
        "trades": [{"usd": it[0], "ts": it[1], "px": it[2], "side": "buy" if it[3] == "B" else (
                    "sell" if it[3] == "A" else ""), "taker": it[4] if it[3] == "B" else (it[5] if it[3] == "A" else ""),
                    "maker": it[5] if it[3] == "B" else (it[4] if it[3] == "A" else ""), "twap": it[6]} for it in big],
        "buyers": buyers, "sellers": sellers,
        "makers": makers,                       # (yön, px, adres) → $ — bookowners
        "other": sum(b.other_addr for b in q),
        "covered_sec": max(0, min(window, t - STARTED)), "window": window,
        "first_ts": q[0].minute * BUCKET if q else None,
    }


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
            f" · {mem:,} kayıt bellekte" + (f" · ⚠️ {STATS['errors']} hata" if STATS["errors"] else ""))
