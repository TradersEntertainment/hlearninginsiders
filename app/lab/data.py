"""🧪 Laboratuvar verisi — mum önbelleği (lab_candles), ağırlık bütçesi, evren.

lab_candles: (coin, tf sn, ts = mum AÇILIŞI) → o/h/l/c/v/n + kapandı bayrağı. Kaynaklar:
  • bars.refresh'in AYNI 1h yanıtı (tee) — ek istek yok
  • lab döngüsünün ağırlık bütçeli derin dolumu (1h ~208 gün, 1d) — coin başına bir kez
  • çözücünün pencere isteği (1m/5m) — kısa saklanır, budanır
HL ağırlığı: candleSnapshot 20 + dönen her 60 mum için +1; istemci istek sayar, ağırlığı saymaz →
lab kendi bütçesini tutar (dakikada ≤ LAB_WEIGHT_MIN) ve paylaşılan pencere doluyken bekler.
Hiçbir yazım toplayıcıya (collector) dokunmaz.
"""
from __future__ import annotations

import logging
import time
from collections import deque

from ..db import db, kv_get, kv_set, now

log = logging.getLogger("lab.data")

TF_NAME = {60: "1m", 300: "5m", 900: "15m", 1800: "30m", 3600: "1h", 86400: "1d"}
NAME_TF = {v: k for k, v in TF_NAME.items()}
CLOSED_MARGIN = 60                 # HL mumu kapandıktan sonra geç düzeltme payı (seans ile aynı)
HL_MAX = 5000                      # HL her dilimde yalnız son 5000 mumu verir
KEEP_S = {60: 7 * 86400, 300: 21 * 86400}   # kısa dilimler budanır; 15m ve üstü süresiz
DEEP_TFS = (3600, 86400)
DEEP_KV = "lab_deep"
WEIGHT_MIN = 240                   # lab'ın kendi dakikalık HL ağırlık tavanı (paylaşılan pencere ayrıca korunur)
SHARED_MAX = 0.5                   # paylaşılan pencerenin (HL 1200/dk) bu payı doluysa lab bekler


# ---------------- saf ----------------

def parse(raw, tf: int, fetch_ts: int) -> list[dict]:
    """candleSnapshot → mumlar (tavan yok). Bozuk satır atılır; aynı damgada sonuncu kalır;
    `n` yoksa None (bilinmiyor → işlem var sayılır). closed = t + tf + 60 ≤ fetch_ts."""
    out: dict[int, dict] = {}
    for x in raw or []:
        try:
            t = int(x["t"]) // 1000
            o, h, lo, c = (float(x[k]) for k in ("o", "h", "l", "c"))
            v = float(x.get("v") or 0)
            n = int(x["n"]) if x.get("n") is not None else None
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
        if t % tf or min(o, h, lo, c) <= 0 or h < lo:
            continue
        out[t] = {"t": t, "o": o, "h": h, "l": lo, "c": c, "v": v, "n": n,
                  "closed": t + tf + CLOSED_MARGIN <= int(fetch_ts)}
    return [out[t] for t in sorted(out)]


def last_closed(tf: int, now_ts: int) -> int:
    """Kapanmış (payı dolmuş) en son mumun açılış damgası."""
    return (int(now_ts) - CLOSED_MARGIN - tf) // tf * tf


def hl_start(tf: int, now_ts: int) -> int:
    """HL'nin bu dilimde hâlâ verdiği en eski mum (yaklaşık; bir mum pay)."""
    return (int(now_ts) // tf - HL_MAX + 2) * tf


class Budget:
    """Lab'ın HL ağırlık bütçesi: kendi kayan dakikası + paylaşılan istemcinin durumu.
    Canlı radarlar asla lab yüzünden 429 yemesin — şüphede bekle."""

    def __init__(self, per_min: int = WEIGHT_MIN, clock=time.monotonic):
        self.per_min = int(per_min)
        self.clock = clock
        self._w: deque[tuple[float, int]] = deque()
        self.spent_total = 0
        self.denied = 0

    def used(self) -> int:
        t = self.clock()
        while self._w and t - self._w[0][0] >= 60:
            self._w.popleft()
        return sum(w for _, w in self._w)

    def can(self, w: int, client=None) -> bool:
        ok = self.used() + int(w) <= self.per_min
        if ok and client is not None:
            try:
                if client.low_paused() > 0:
                    ok = False
                else:
                    u = client.usage()
                    ok = u["weight"] + int(w) <= SHARED_MAX * u["weight_max"]
            except Exception:                        # noqa: BLE001 — sahte istemci / eski sürüm
                pass
        if not ok:
            self.denied += 1
        return ok

    def spend(self, w: int) -> None:
        self._w.append((self.clock(), int(w)))
        self.spent_total += int(w)


# ---------------- önbellek ----------------

# (coin, tf) → (lo, hi): bu süreçte HL'den çekilip yazılmış kapanmış mum aralığı. Yeniden başlayınca
# boşalır (ilk istek pencereyi bir kez daha çeker); aralıkta mum yoksa o dilimde işlem yoktur.
_COVER: dict[tuple[str, int], tuple[int, int]] = {}
_SHORT: set[tuple[str, int]] = set()


def _note_cover(coin: str, tf: int, a: int, b: int) -> None:
    if b < a:
        return
    old = _COVER.get((coin, tf))
    if old and a <= old[1] + tf and b >= old[0] - tf:
        _COVER[(coin, tf)] = (min(a, old[0]), max(b, old[1]))
    else:
        _COVER[(coin, tf)] = (a, b)


async def upsert(coin: str, tf: int, rows: list[dict]) -> int:
    """1000'lik işlemlerle yaz; kapanmış mum, kapanmamış sürümüyle EZİLMEZ."""
    data = [(coin, tf, r["t"], r["o"], r["h"], r["l"], r["c"], r["v"], r["n"], 1 if r["closed"] else 0)
            for r in rows]
    for i in range(0, len(data), 1000):
        async with db() as conn:
            await conn.executemany(
                "INSERT INTO lab_candles(coin, tf, ts, o, h, l, c, v, n, closed) VALUES(?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(coin, tf, ts) DO UPDATE SET o=excluded.o, h=excluded.h, l=excluded.l,"
                " c=excluded.c, v=excluded.v, n=excluded.n, closed=excluded.closed"
                " WHERE excluded.closed >= lab_candles.closed", data[i:i + 1000])
    if tf in KEEP_S and data:
        _SHORT.add((coin, tf))
    return len(data)


async def candles(coin: str, tf: int, t0: int, t1: int, closed_only: bool = True) -> list[dict]:
    """[t0, t1] açılışlı mumlar (artan). tf=1800: seans arşivi (yalnız kapanmış)."""
    if tf == 1800:
        q = ("SELECT ts, o, h, l, c, v, n, 1 AS closed FROM seans_bars WHERE coin=? AND ts>=? AND ts<=?"
             " ORDER BY ts")
        args: tuple = (coin, int(t0), int(t1))
    else:
        q = ("SELECT ts, o, h, l, c, v, n, closed FROM lab_candles WHERE coin=? AND tf=? AND ts>=? AND ts<=?"
             + (" AND closed=1" if closed_only else "") + " ORDER BY ts")
        args = (coin, int(tf), int(t0), int(t1))
    async with db() as conn:
        cur = await conn.execute(q, args)
        rows = await cur.fetchall()
    return [{"t": int(r["ts"]), "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"],
             "n": (int(r["n"]) if r["n"] is not None and int(r["n"]) >= 0 else None),
             "closed": bool(r["closed"])} for r in rows]


async def ensure_window(client, coin: str, tf: int, t0: int, t1: int, budget: Budget | None = None,
                        now_ts: int | None = None) -> list[dict] | None:
    """Önbellekten oku; HL penceresinde bu süreçte çekilmemiş kapanmış aralık varsa TEK istekle
    yalnız eksik kuyruğu çek. Bütçe izin vermezse None (çağıran erteler — eksik veriyle ölçülmez)."""
    from ..hl.client import weight_of
    now_ts = int(now_ts or now())
    a = max(int(t0) // tf * tf, hl_start(tf, now_ts))
    b = min(int(t1) // tf * tf, last_closed(tf, now_ts))
    cov = _COVER.get((coin, tf))
    if b >= a and not (cov and cov[0] <= a and cov[1] >= b) and client is not None:
        fa = max(a, cov[1] - 2 * tf) if (cov and cov[0] <= a <= cov[1] + tf) else a
        fb = min(now_ts, b + tf)
        payload = {"type": "candleSnapshot", "req": {"coin": coin, "interval": TF_NAME[tf],
                                                     "startTime": fa * 1000, "endTime": fb * 1000}}
        w = weight_of(payload)
        if budget is not None and not budget.can(w, client):
            return None
        raw = await client.candles(coin, TF_NAME[tf], fa * 1000, fb * 1000)
        if budget is not None:
            budget.spend(w)
        await upsert(coin, tf, parse(raw, tf, now_ts))
        _note_cover(coin, tf, fa, b)
    return await candles(coin, tf, t0, t1)


async def tee_from_bars(coin: str, tf_str: str, raw, fetch_ts: int) -> int:
    """bars.refresh'in aynı yanıtı: yalnız 1h ve yalnız laboratuvar evreni (PROPR) — ek istek yok."""
    tf = NAME_TF.get(tf_str)
    if tf != 3600 or not in_universe(coin):
        return 0
    rows = parse(raw, tf, fetch_ts)
    if not rows:
        return 0
    n = await upsert(coin, tf, rows)
    _note_cover(coin, tf, rows[0]["t"], last_closed(tf, fetch_ts))
    return n


async def deep_fill_step(client, budget: Budget, coins, now_ts: int | None = None, max_n: int = 3) -> dict:
    """Coin başına bir kez derin dolum (1h ~208 gün, 1d tüm geçmiş). Bütçe biterse kaldığı yerden
    sonraki turda sürer; ilerleme kv'de."""
    now_ts = int(now_ts or now())
    done = (await kv_get(DEEP_KV)) or {}
    n, left, err = 0, 0, ""
    for coin in coins:
        for tf in DEEP_TFS:
            key = f"{coin}|{tf}"
            if key in done:
                continue
            if n >= max_n:
                left += 1
                continue
            try:
                r = await ensure_window(client, coin, tf, now_ts - HL_MAX * tf, now_ts, budget, now_ts)
            except Exception as e:                   # noqa: BLE001 — coin başına; sonraki turda yeniden
                err = f"{coin}/{TF_NAME[tf]}: {type(e).__name__}: {e}"[:160]
                left += 1
                continue
            if r is None:
                left += 1
                n = max_n                            # bütçe doldu: bu tur bitti
                continue
            done[key] = now_ts
            n += 1
    if n:
        await kv_set(DEEP_KV, done)
    return {"filled": n, "left": left, "done": len(done), "err": err}


async def prune(now_ts: int | None = None) -> int:
    """Kısa dilimler (1m 7 gün, 5m 21 gün): coin başına birincil anahtar aralığı silinir."""
    now_ts = int(now_ts or now())
    if not _SHORT:
        async with db() as conn:
            for tf in KEEP_S:
                cur = await conn.execute("SELECT DISTINCT coin FROM lab_candles WHERE tf=?", (tf,))
                _SHORT.update((r["coin"], tf) for r in await cur.fetchall())
    total = 0
    for coin, tf in sorted(_SHORT):
        async with db() as conn:
            cur = await conn.execute("DELETE FROM lab_candles WHERE coin=? AND tf=? AND ts<?",
                                     (coin, tf, now_ts - KEEP_S[tf]))
            total += cur.rowcount or 0
    return total


# ---------------- evren ----------------

_UNIV: dict = {"ts": 0, "coins": {}}
UNIV_TTL = 3600
BENCH_KRIPTO = "BTC"
BENCH_ABD = "xyz:XYZ100"


async def lab_coins(cfg=None, ts: int | None = None) -> dict[str, str]:
    """PROPR'da listeli HL coinleri → sınıf ('kripto' | 'hisse' | 'endeks') + kıyas serileri.
    Aynı sembol birden çok hisse dex'indeyse `equity_dexes` sırasındaki ilki. Ağ yok (tickers + kv)."""
    from .. import assets
    from ..hl.universe import MAIN_CTX_KV
    from ..propr import is_listed
    ts = int(ts or now())
    if _UNIV["coins"] and ts - _UNIV["ts"] < UNIV_TTL:
        return _UNIV["coins"]
    order = [str(x).strip().lower() for x in (getattr(cfg, "equity_dexes", None) or ["xyz"])]
    rank = lambda c: order.index(c.split(":")[0]) if c.split(":")[0] in order else len(order)   # noqa: E731
    out: dict[str, str] = {}
    best: dict[str, str] = {}
    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol FROM tickers")
        rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        coin = r["coin"] or ""
        if not coin or assets.is_excluded(coin) or not is_listed(r["symbol"] or coin):
            continue
        k = assets.klass(coin)
        if k == "kripto":
            out[coin] = k
            continue
        sym = coin.split(":")[-1].upper()
        if sym not in best or rank(coin) < rank(best[sym]):
            best[sym] = coin
    for coin in best.values():
        out[coin] = assets.klass(coin)
    ctx = ((await kv_get(MAIN_CTX_KV)) or {}).get("c") or {}
    for coin in ctx:
        if is_listed(coin) and not assets.is_excluded(coin):
            out[coin] = "kripto"
    out.setdefault(BENCH_KRIPTO, "kripto")
    out.setdefault(BENCH_ABD, "endeks")
    _UNIV.update(ts=ts, coins=out)
    return out


def in_universe(coin: str) -> bool:
    """Önbellekteki evrene göre (döngü tazeler); boşsa PROPR listesi."""
    if _UNIV["coins"]:
        return coin in _UNIV["coins"]
    from ..propr import is_listed
    return is_listed(coin)


def bench_of(coin: str, klass: str) -> str | None:
    """Piyasa kıyası: kripto → BTC; ABD hisse ve ABD endeks/ETF perp'i → XYZ100; emtia/döviz/ABD dışı
    endeks → yok (düzeltilmemiş getiri). Kıyasın kendisi için yok."""
    if coin in (BENCH_KRIPTO, BENCH_ABD):
        return None
    if klass == "kripto":
        return BENCH_KRIPTO
    if klass == "hisse":
        return BENCH_ABD
    from ..radar.seans import US_NONEQ_OK
    return BENCH_ABD if coin.split(":")[-1].upper() in US_NONEQ_OK else None
