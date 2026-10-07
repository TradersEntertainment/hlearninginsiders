"""🔔 Açılışın en hareketlileri — ABD açılışının ilk 5 ve ilk 30 dakikasında en çok oynayan hisseler.

Kullanıcı (07.10): "açılışın en hareketli hissesi diye bir şey yapalım; 16:30–16:35 arası ölçüm
yapsın rapor yollasın, sonra 17:00'e kadar hep ölçsün, 17'de tekrar rapor — en hareketli hisseler".
Kararlar: fiyat oynamasıyla sırala (açılış fiyatından |%|; satırda aralık ve hacim de), hisse kanalı
(CRYPTO_STOCKS_ID), PROPR'da listeli hisseler (endeks / emtia / döviz hariç), saat ABD açılışına
bağlı: 9:30 ET → yazın 16:30 TSİ, kışın 17:30 TSİ; hafta sonu ve NYSE tatilinde rapor yok.

Ölçüm CANLI işlem akışından (collector, her işlem) — HL'ye ek istek yok, hiçbir işlem atlanmaz:
  • referans = açılıştan ÖNCEKİ son işlem (perp 7/24 işler; 9:29:59'daki fiyat). Açılış öncesi işlem
    görülmediyse pencerenin ilk işlemi (satırda † ile)
  • değişim = son fiyat / referans − 1 · aralık = (tepe − dip) / referans · hacim = Σ fiyat × adet
  • "24s ort. 14×" = penceredeki hacim / (24 saatlik hacmin aynı süreye düşen payı) — betimleme
  • pencerede hacmi tabanın (open_movers_min_usd) altında kalan sıralamaya girmez: tek işlemlik
    oynama "en hareketli" sayılmasın; kaç tane olduğu künyede yazar
Kapsam dürüst: akışın görülmediği aralıklar (yeniden başlatma, WS kopukluğu — collector.down_log)
rapora saatleriyle yazılır; pencerenin yarısından azı görüldüyse rapor gönderilmez. HL abonelikte
son 30 işlemi yeniden yollar (canlı denendi 07.10) — yeniden bağlanınca o işlemler iki kez sayılmaz.
Geç kalan rapor (10 dk+) gitmez.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import date, datetime

from ..db import db, kv_get, kv_set, now

log = logging.getLogger("radar.openmove")

FIRST = 300                      # ilk rapor: açılıştan 5 dk sonra
WINDOW = 1800                    # ölçüm penceresi ve ikinci rapor: 30 dk
GRACE = 5                        # canlı akışın gecikmesi payı
LATE_MAX = 600                   # bundan geç kalan rapor gönderilmez (bayat)
HOLD = GRACE + LATE_MAX + 60     # pencereden sonra gün bu kadar tutulur (30 dk raporunun "geç" notu yazılsın)
COVER_LEAD = 60                  # referans için akış açılıştan en az bu kadar önce bağlı olmalı
MIN_COVER = 0.5                  # pencerenin yarısından azı görüldüyse rapor yok
UNIVERSE_TTL = 600
RETRY_SEC = 30                   # gönderim başarısızsa yeniden deneme aralığı
SENT_KV = "openmove_sent"        # {"day": "2026-10-07", "sent": [5, 30]}
LAST_KV = "openmove_last"        # son rapor (/acilis, /tani)
STATS_KV = "openmove_stats"


class _Reg:
    """Sıcak yol durumu (collector her işlemde `observe` çağırır)."""

    def __init__(self):
        self.coins: set[str] = set()          # evren: PROPR ∩ tickers ∩ hisse
        self.day = ""                         # ölçülen ET işlem günü (iso)
        self.open_ts = 0                      # 9:30 ET (UTC sn); 0 = ayarlanmadı
        self.since = 0                        # bu gün için gözlemin başladığı an (süreç / gün geçişi)
        self.pre: dict[str, tuple[int, float]] = {}   # coin → (ts, fiyat) açılıştan önceki son işlem
        self.agg5: dict[str, dict] = {}       # ilk 5 dk
        self.agg30: dict[str, dict] = {}      # ilk 30 dk
        # coin → (en yeni işlem sn'si, o saniyenin tid'leri): akış düzeyinde, gün geçişinde sıfırlanmaz
        self.hwm: dict[str, tuple[int, set]] = {}
        self.trades = 0
        self.errors = 0
        self.universe_ts = 0
        self.fail_ts: dict[int, int] = {}     # rapor → son başarısız gönderim (yeniden deneme aralığı)
        # ⏱ /5dk pencereleri (app/radar/movewin.py)
        self.recent: dict[str, deque] = {}    # coin → son KEEP sn'nin işlemleri (ts, fiyat, adet) + bir çapa
        self.wins: list[dict] = []            # {chat, t0, t1, mins, ref{coin:(ts,px)}, agg{}}
        self.obs_since = 0                    # bu süreçte gözlemin başladığı an (ilk evren yüklemesi)
        self.wins_loaded = False              # kv'deki pencereler bu süreçte geri yüklendi mi
        self.win_errors = 0


REG = _Reg()
KEEP = 240                       # tampon: 180 sn geriye dönük başlangıç + 60 sn saat kayması payı


def _add(book: dict, coin: str, px: float, sz: float, ts: int) -> None:
    a = book.get(coin)
    if a is None:
        book[coin] = {"first": px, "first_ts": ts, "hi": px, "lo": px, "last": px, "last_ts": ts,
                      "vol": px * sz, "n": 1}
        return
    if px > a["hi"]:
        a["hi"] = px
    if px < a["lo"]:
        a["lo"] = px
    if ts < a["first_ts"]:                   # sırası karışık teslim: ilk = en erken
        a["first"], a["first_ts"] = px, ts
    if ts >= a["last_ts"]:
        a["last"], a["last_ts"] = px, ts
    a["vol"] += px * sz
    a["n"] += 1


def _dup(r: _Reg, coin: str, ts: int, tid: str) -> bool:
    """HL abonelikte (yeniden bağlanınca da) coinin son 30 işlemini ARTAN sırayla yeniden yollar
    (07.10 canlı görüldü); akış coin başına sıralı → en yeni andan eskisi zaten görüldü, aynı
    saniyede tid bakılır. Bellek coin başına tek saniye."""
    h = r.hwm.get(coin)
    if h is not None:
        if ts < h[0]:
            return True
        if ts == h[0]:
            if tid in h[1]:
                return True
            h[1].add(tid)
            return False
    r.hwm[coin] = (ts, {tid})
    return False


def _open_add(r: _Reg, coin: str, px: float, sz: float, ts: int) -> None:
    if ts < r.open_ts:
        p = r.pre.get(coin)
        if p is None or ts >= p[0]:
            r.pre[coin] = (ts, px)            # açılıştan önceki son işlem = referans
        return
    if ts >= r.open_ts + WINDOW:
        return
    _add(r.agg30, coin, px, sz, ts)
    if ts < r.open_ts + FIRST:
        _add(r.agg5, coin, px, sz, ts)
    r.trades += 1


def observe(coin: str, px: float, sz: float, ts: int, tid: str = "") -> None:
    """Collector'ın işlem döngüsünden, HER işlemde: senkron, I/O yok (çağıran try/except'li).
    Sıra: tekrar teslim → tampon (⏱ geriye dönük başlangıç) → açılış penceresi → ⏱ pencereler."""
    r = REG
    if coin not in r.coins:
        return
    if tid and _dup(r, coin, ts, tid):
        return
    dq = r.recent.get(coin)
    if dq is None:
        dq = r.recent[coin] = deque()
    dq.append((ts, px, sz))
    cut = ts - KEEP
    while len(dq) > 1 and dq[1][0] < cut:     # dq[0] çapa: kesimden önceki son işlem (referans)
        dq.popleft()
    if r.open_ts:
        _open_add(r, coin, px, sz, ts)
    if r.wins:
        try:
            for w in r.wins:
                if ts < w["t0"]:
                    p = w["ref"].get(coin)
                    if p is None or ts >= p[0]:
                        w["ref"][coin] = (ts, px)
                elif ts < w["t1"]:
                    _add(w["agg"], coin, px, sz, ts)
        except Exception:                     # bozuk pencere açılış ölçümünü aç bırakmasın
            r.win_errors += 1


# ---------------- takvim ----------------

def open_ts_for(d: date) -> int:
    from .hourstats import ET, MKT_OPEN
    return int(datetime.combine(d, MKT_OPEN, ET).timestamp())


def session_for(ts: int, tail: int = HOLD) -> tuple[date, int]:
    """Bu anda ölçülen / beklenen işlem günü ve açılışı: bugün işlem günüyse ve pencere (+ `tail`:
    döngü için geç kalma payı, /acilis için 0) bitmediyse bugün; değilse sonraki işlem günü."""
    from .hourstats import ET
    from .seans import is_trading_day, next_trading_day
    d = datetime.fromtimestamp(int(ts), ET).date()
    if is_trading_day(d) and ts < open_ts_for(d) + WINDOW + tail:
        return d, open_ts_for(d)
    d = next_trading_day(d)
    return d, open_ts_for(d)


def configure(d: date, open_ts: int, ts: int) -> None:
    """Gün değişince durum sıfırlanır (önceki günün referansları taşınmaz)."""
    if REG.day == d.isoformat() and REG.open_ts == open_ts:
        return
    REG.day, REG.open_ts, REG.since = d.isoformat(), open_ts, int(ts)
    REG.pre, REG.agg5, REG.agg30 = {}, {}, {}           # hwm akış düzeyinde: sıfırlanmaz
    REG.trades, REG.fail_ts = 0, {}


async def universe(cfg) -> set[str]:
    """PROPR'da listeli hisseler (tickers ∩ PROPR ∩ sınıf 'hisse' — endeks/emtia/döviz/kripto dex hariç).
    Aynı hisse birden çok hisse dex'indeyse tek satır: `equity_dexes` sırasındaki ilk dex."""
    from .. import assets
    from ..propr import is_listed
    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol FROM tickers")
        rows = [dict(r) for r in await cur.fetchall()]
    order = [str(x).strip().lower() for x in (getattr(cfg, "equity_dexes", None) or ["xyz"])]
    rank = lambda c: order.index(c.split(":")[0]) if c.split(":")[0] in order else len(order)   # noqa: E731
    best: dict[str, str] = {}
    for r in rows:
        coin = r["coin"]
        if not (is_listed(r["symbol"] or coin) and assets.klass(coin) == "hisse"):
            continue
        sym = coin.split(":")[-1].upper()
        if sym not in best or rank(coin) < rank(best[sym]):
            best[sym] = coin
    return set(best.values())


async def ensure_universe(cfg, ts: int) -> None:
    """Evren ~10 dk'da bir tazelenir (açılış raporu kapalıyken de: /5dk aynı evreni kullanır).
    İlk yüklemede `obs_since` = bu süreçte gözlemin başladığı an."""
    if ts - REG.universe_ts >= UNIVERSE_TTL or not REG.coins:
        try:
            REG.coins = await universe(cfg)
            REG.universe_ts = int(ts)
        except Exception:
            log.warning("açılış evreni okunamadı", exc_info=True)
    if REG.coins and not REG.obs_since:
        REG.obs_since = int(ts)


async def _day_volumes() -> dict[str, float]:
    from .equityvol import _day_volumes
    try:
        return await _day_volumes()
    except Exception:
        log.debug("24s hacimleri okunamadı", exc_info=True)
        return {}


def coverage(open_ts: int, end_ts: int, ts: int | None = None, since: int | None = None) -> dict:
    """Pencerenin ne kadarını gördük. Görülmeyen aralıklar: bu gözlemin başlangıcından önce
    (yeniden başlatma / gün geçişi; ⏱ pencereler `since=obs_since` verir) ve canlı akışın kopuk
    olduğu süreler (collector.down_log, şu an kopuksa down_since → şimdi). Referans için
    başlangıçtan COVER_LEAD sn öncesi de sayılır.
    {"frac": pencerenin görülen payı, "full": hiç boşluk yok, "gaps": [(baş, son), …]}."""
    lo, hi = open_ts - COVER_LEAD, end_ts
    ts = int(ts if ts is not None else now())
    start = int(REG.since if since is None else since)
    raw: list[tuple[int, int]] = []
    if start > lo:
        raw.append((lo, start))
    try:
        from ..hl import collector as col
        live = col.LIVE
    except Exception:
        live = None
    if live is not None:
        for a, b in list(getattr(live, "down_log", None) or ()):
            raw.append((int(a), int(b)))
        down = float(getattr(live, "down_since", 0) or 0)
        if down:
            raw.append((int(down), max(ts, int(down))))
    gaps: list[list[int]] = []
    for a, b in sorted((max(a, lo), min(b, hi)) for a, b in raw if b > lo and a < hi):
        if gaps and a <= gaps[-1][1]:
            gaps[-1][1] = max(gaps[-1][1], b)
        elif b > a:
            gaps.append([a, b])
    lost = sum(max(0, b - max(a, open_ts)) for a, b in gaps)
    frac = max(0.0, 1 - lost / max(1, hi - open_ts))
    return {"frac": frac, "full": not gaps, "gaps": [(a, b) for a, b in gaps]}


# ---------------- rapor ----------------

def rank(book: dict, refs: dict, vols: dict, span: int, floor: float, top: int,
         t0: int = 0, stale: int = 0) -> tuple[list[dict], int]:
    """SAF sıralama (açılış raporu ve ⏱ pencereler): defter + referanslar → (ilk `top` satır,
    taban altı sayısı). Referans yoksa pencerenin ilk işlemi (ref_pre False → †). `stale` > 0 ise
    referansı t0'dan `stale` sn'den eski satır işaretlenir (ref_old → °). Await yok: defter
    dolaşılırken canlı akış araya giremez."""
    from .. import assets
    from ..propr import is_listed
    rows, below = [], 0
    for coin, a in book.items():
        pre = refs.get(coin)
        ref, ref_pre = (pre[1], True) if pre else (a["first"], False)
        if not ref or ref <= 0:
            continue
        if a["vol"] < floor:
            below += 1
            continue
        normal = float(vols.get(coin) or 0) * span / 86400
        rows.append({"coin": coin, "symbol": assets.label(coin), "ref": ref, "ref_pre": ref_pre,
                     "last": a["last"], "chg": (a["last"] - ref) / ref * 100,
                     "rng": (a["hi"] - a["lo"]) / ref * 100, "vol": a["vol"], "n": a["n"],
                     "mult": a["vol"] / normal if normal > 0 else None,
                     "propr": is_listed(assets.label(coin)),
                     "ref_old": bool(pre and stale and t0 - pre[0] > stale)})
    rows.sort(key=lambda r: (-abs(r["chg"]), -r["rng"]))
    return rows[:top], below


async def build(cfg, which, ts: int) -> dict:
    """which: 5 | 30 | 'live' (pencere içinde anlık). Saf hesap — REG'den okur."""
    open_ts = REG.open_ts
    if which == 5:
        book, end = REG.agg5, open_ts + FIRST
    elif which == 30:
        book, end = REG.agg30, open_ts + WINDOW
    else:
        book, end = REG.agg30, min(int(ts), open_ts + WINDOW)
    span = max(60, end - open_ts)
    floor = float(getattr(cfg, "open_movers_min_usd", 25_000) or 0)
    top = max(1, int(getattr(cfg, "open_movers_top", 10) or 10))
    vols = await _day_volumes()
    rows, below = rank(book, REG.pre, vols, span, floor, top)
    if which == 30:
        for r in rows:
            b = REG.agg5.get(r["coin"])
            r["chg5"] = (b["last"] - r["ref"]) / r["ref"] * 100 if b else None
    return {"which": which, "day": REG.day, "open_ts": open_ts, "end_ts": end, "ts": int(ts),
            "rows": rows, "n_universe": len(REG.coins), "n_traded": len(book), "n_below": below,
            "floor": floor, "cover": coverage(open_ts, end, ts)}


async def tick(cfg, notifier, ts: int) -> dict:
    """Bir adım: günü ayarla, evreni tazele, zamanı gelen raporu gönder (günde birer kez)."""
    out = {"sent": [], "skipped": [], "day": None}
    await ensure_universe(cfg, ts)                    # kapalıyken de: ⏱ /5dk aynı evreni kullanır
    if not getattr(cfg, "open_movers_enabled", True):
        out["disabled"] = True
        return out
    d, open_ts = session_for(ts)
    configure(d, open_ts, ts)
    out["day"] = REG.day
    sent = await kv_get(SENT_KV) or {}
    if sent.get("day") != REG.day:
        sent = {"day": REG.day, "sent": [], "notes": {}}
    changed = False
    chat = str(getattr(cfg, "crypto_stocks_id", "") or "").strip()
    from ..notify import kind_enabled
    for which, at in ((5, open_ts + FIRST), (30, open_ts + WINDOW)):
        if which in sent["sent"] or ts < at + GRACE or ts < REG.fail_ts.get(which, 0) + RETRY_SEC:
            continue
        note = None
        if ts > at + GRACE + LATE_MAX:
            note = ("10 dk boyunca gönderilemedi (Telegram hatası) — bayat rapor bırakıldı" if which in REG.fail_ts
                    else "rapor saatinin üzerinden 10 dk geçmişti (bot kapalı / döngü durmuştu) — bayat rapor gönderilmedi")
        rep = None if note else await build(cfg, which, ts)
        if rep is not None and rep["cover"]["frac"] < MIN_COVER:
            note = (f"pencerenin görülen payı %{rep['cover']['frac'] * 100:.0f} (en az %{MIN_COVER * 100:.0f}"
                    " gerekir) — rapor gönderilmedi")
        if rep is not None and note is None and (not chat or notifier is None):
            note = "hisse kanalı (CRYPTO_STOCKS_ID) yok"
        if rep is not None and note is None and not kind_enabled(cfg, "openmove"):
            note = "bildirim kapalı (Ayarlar → Bildirimler → 🔔 Açılışın en hareketlileri)"
        if note:
            sent["sent"].append(which)
            sent.setdefault("notes", {})[str(which)] = note
            out["skipped"].append(which)
            changed = True
            continue
        from ..telegram import format as fmt
        text = fmt.openmove_report(rep)
        try:
            ok = await notifier.send("openmove", text, chat_id=chat, key=f"openmove:{REG.day}:{which}")
        except Exception:
            log.warning("açılış raporu gönderilemedi", exc_info=True)
            ok = False
        if ok:
            sent["sent"].append(which)
            changed = True
            out["sent"].append(which)
            await kv_set(LAST_KV, {"ts": int(ts), "which": which, "day": REG.day, "text": text})
        else:                                 # işaret yok: RETRY_SEC sonra yeniden (LATE_MAX'e kadar)
            REG.fail_ts[which] = int(ts)
    if changed:
        await kv_set(SENT_KV, sent)
    return out


async def live_view(cfg, ts: int | None = None) -> str:
    """/acilis: pencere içindeyse anlık sıralama, değilse sıradaki pencere + son rapor."""
    from ..telegram import format as fmt
    ts = int(ts or now())
    d, open_ts = session_for(ts, tail=0)
    if open_ts <= ts:                                  # pencere şu an açık
        if REG.open_ts == open_ts and REG.coins:
            return fmt.openmove_report(await build(cfg, "live", ts))
        return "🔔 Açılış penceresi açık ama ölçüm bu süreçte henüz başlamadı (bot yeni açıldı) — birazdan tekrar dene."
    return fmt.openmove_idle(await kv_get(LAST_KV) or {}, d, open_ts)


async def loop(cfg, notifier) -> None:
    """Denetimli döngü (5 sn). Site ASLA buna bağımlı değil. Durum kv'ye dakikada bir (ya da
    rapor anında) yazılır — her adımda değil. ⏱ /5dk pencereleri de bu döngüden (movewin.tick)."""
    from ..health import beat
    from . import movewin
    await asyncio.sleep(10)
    last_stats = 0
    while True:
        ts = now()
        try:
            out = await tick(cfg, notifier, ts)
            if out.get("sent") or out.get("skipped") or ts - last_stats >= 60:
                last_stats = ts
                await kv_set(STATS_KV, {**out, "ts": ts, "coins": len(REG.coins), "trades": REG.trades,
                                        "errors": REG.errors, "open_ts": REG.open_ts, "since": REG.since})
            await beat("openmove")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("açılış hareketlileri turu")
        try:
            await movewin.tick(cfg, notifier, ts)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("⏱ pencere ölçümü turu")
        await asyncio.sleep(5)
