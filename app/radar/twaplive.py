"""Canlı TWAP radarı — WS akışındaki HER işlemi adres bazında sayar ve coinin
24 saatlik hacmine göre BÜYÜK olan düzenli dilimlemeyi Telegram'a yazar.
Ekstra HL isteği yok; her şey collector'ın zaten aldığı trade'lerden.

Neden ayrı bir sayaç: HL'nin yerel TWAP emri 30 sn'de bir dilim atar. $1.6M /
6 saat ≈ 720 × $2.2K dilim; kripto yakalama tabanı ($5K) bunları `fills`'e hiç
yazmaz, arşiv radarı (`twap.py`) kördür. Collector ise abone olduğu her coinin
her trade'ini alıcı/satıcı adresiyle alıyor — burada tabanın ALTINDAKİ
dilimler de sayılır ("INJ'e $2M TWAP" tam bu).

Dilim birleştirme: HL alt-emri IOC'dir, ince defterde 3-8 karşı tarafa bölünür
→ aynı saniyede birkaç trade. 2 sn içinde gelen parçalar tek dilim sayılır;
yoksa aralık 0 olur ve düzenlilik hiç tutmaz.

TAHMİN YOK (kullanıcı kuralı): düzenli dizi görülünce adresin HL TWAP EMRİ
WS'ten sorgulanır (`userTwapHistory` snapshot — hypurrscan'in Orders sekmesi):
planlanan boyut, dolan kısım, kalan süre. Kapı: emir ≥ twap_alert_min_usd ($2M)
VE kalan ≥ twap_alert_min_left_usd ($1M) VE emir 24s hacmin ≥ twap_alert_vol_pct'i
(%20). Emir yok / bitmiş / iptal / hacim bilinmiyor → bildirim YOK. "Bu hızla
24 saatte…" gibi ekstrapolasyon mesajlarda yer almaz.
Kripto → CRYPTO_CHAT_ID (boşsa gönderilmez), hisse/endeks → ana sohbet.
İlerleme notu emrin yarısı dolunca (tek sefer), bitiş/iptal notu emir
finished/terminated olunca — gerçek dolan tutarla.

Dedupe DB'de (`alerts_log`, restart'a dayanıklı); işaret GÖNDERİMDEN ÖNCE
yazılır — 60 sn'lik döngü + sessiz saat her dakika yeni özet kaydı üretmesin.
Bellek: tur başına son 64 dilim (düzenlilik onlardan ölçülür), toplamlar
skaler; boşta kalan diziler budanır; en çok MAX_KEYS dizi.
"""
import asyncio
import logging
import statistics
from collections import deque

from ..db import alert_log, alert_recent, db, kv_get, kv_set, now
from ..notify import in_quiet_hours
from . import twap as twapmod

log = logging.getLogger("radar.twaplive")

COALESCE_SEC = 2            # aynı dilimin parçaları (IOC alt-emir defterde bölünür)
KEEP_SLICES = 64            # düzenlilik son bu kadar dilimden ölçülür
IDLE_SEC = 1800             # dilim gelmeyen dizi bu kadar sonra düşer
IDLE_SHORT_SEC = 300        # 5 dilimden az olan (tekil işlem) çok daha çabuk düşer
MAX_KEYS = 20000            # bellek tavanı (BTC/ETH tek başına binlerce adres üretir)
MIN_DUR_SEC = 600           # bildirim için en az bu kadar sürmüş olmalı
CV_GAP_LIVE = 0.5           # HL "randomize" ve dolmayan dilimin taşınması: arşivden gevşek
CV_SIZE_LIVE = 0.6
END_MIN_SEC = 300           # bitiş: en az 5 dk VE 3 aralık dilim yok
END_GAPS = 3
PROGRESS_STEPS = (250e3, 500e3, 1e6, 2e6, 5e6, 10e6, 25e6, 50e6, 100e6)
PROGRESS_MIN_GAP = 1800     # tur başına ilerleme notu en çok 30 dk'da bir
VOL_MAX_AGE = 3600          # hacim ölçümü bundan eskiyse kapı geçmez (tahmin yok)
LOOKUP_MIN_DUR = 300        # sorgudan önce dizi en az 5 dk sürmüş olmalı
LOOKUP_MAX_PER_EVAL = 20    # bir turda en çok bu kadar WS sorgusu
REFRESH_SEC = 600           # bildirilmiş tur: emir 10 dk'da bir yeniden sorgulanır
HALF_PCT = 50               # ilerleme notu: emrin yarısı dolunca
NATIVE_GAP = (25, 35)       # ortanca aralık bu banttaysa "HL TWAP düzenine uyuyor"
STATS_KV = "twaplive_stats"
LAST_KV = "twaplive_last"   # son 50 aday kararı (tur tur ezilmez): "niye gelmedi" sorusunun kaydı
LAST_MAX = 50
DECISIONS_MAX = 30
REASON_TR = {"irregular": "düzensiz dizi", "mm": "mm/vault", "cooldown": "bekleme içinde",
             "no_lookup": "collector yok", "lookup_fail": "emir sorgusu başarısız",
             "no_order": "HL'de TWAP emri yok", "order_done": "emir bitmiş/iptal",
             "order_small": "emir eşik altı", "order_left": "kalan az", "no_vol": "hacim bilinmiyor",
             "vol_small": "hacme göre küçük", "no_chat": "kanal yok", "alerted": "bildirildi",
             "failed": "gönderilemedi", "ok": "geçer", "big": "hacimden bağımsız büyük"}


class Run:
    __slots__ = ("coin", "address", "side", "first_ts", "last_ts", "n", "total", "sz_total",
                 "pxsz", "px_first", "px_last", "tk_n", "known_n", "slices", "alerted_ts",
                 "alert_total", "progress_ts", "progress_step", "ended_ts", "day_volume",
                 "rate_day", "gate", "order", "lookup_ts", "half_ts")

    def __init__(self, coin: str, address: str, side: str, ts: int, px: float):
        self.coin, self.address, self.side = coin, address, side
        self.first_ts = self.last_ts = int(ts)
        self.n = 0
        self.total = self.sz_total = self.pxsz = 0.0
        self.px_first = self.px_last = float(px)
        self.tk_n = self.known_n = 0
        self.slices: deque = deque(maxlen=KEEP_SLICES)   # (ts, sz, ntl, px)
        self.alerted_ts = None
        self.alert_total = 0.0
        self.progress_ts = None
        self.progress_step = 0.0
        self.ended_ts = None
        self.day_volume = None
        self.rate_day = None
        self.gate = ""
        self.order = None          # son sorgudaki HL TWAP emri (parse_twap_orders çıktısı)
        self.lookup_ts = None
        self.half_ts = None

    @property
    def key(self) -> tuple:
        return (self.coin, self.address, self.side)


class Registry:
    def __init__(self):
        self.runs: dict[tuple, Run] = {}
        self.errors = 0
        self.observed = 0

    def observe(self, coin: str, addr: str, side: str, px: float, sz: float,
                notional: float, ts: int, taker=None) -> None:
        """Sıcak yol: dict araması + birkaç float. Await yok, istisna yok."""
        self.observed += 1
        key = (coin, addr, side)
        r = self.runs.get(key)
        if r is None:
            if len(self.runs) >= MAX_KEYS:
                self._evict(int(ts))
            r = self.runs[key] = Run(coin, addr, side, int(ts), px)
        ts = int(ts)
        if r.slices and ts - r.slices[-1][0] <= COALESCE_SEC:
            t0, s0, n0, _ = r.slices[-1]
            s1, n1 = s0 + sz, n0 + notional
            r.slices[-1] = (t0, s1, n1, (n1 / s1) if s1 else px)
        else:
            r.slices.append((ts, sz, notional, px))
            r.n += 1
            if taker is not None:
                r.known_n += 1
                if taker:
                    r.tk_n += 1
        r.total += notional
        r.sz_total += sz
        r.pxsz += px * sz
        if ts >= r.last_ts:
            r.last_ts = ts
            r.px_last = px

    def _evict(self, ref_ts: int) -> None:
        self.prune(ref_ts, IDLE_SEC)
        if len(self.runs) < MAX_KEYS:
            return
        # Hâlâ doluysa en eski %10 (bildirilmemişler) gider
        victims = sorted((r for r in self.runs.values() if not r.alerted_ts),
                         key=lambda r: r.last_ts)[: max(1, MAX_KEYS // 10)]
        for r in victims:
            self.runs.pop(r.key, None)

    def prune(self, ref_ts: int, window_sec: int = 4 * 3600) -> int:
        """Boşta kalan (tekil işlem 5 dk, dizi 30 dk), bitmiş ya da penceresini
        aşmış bildirilmemiş diziler düşer. Bildirilmiş süren tur pencereye takılmaz."""
        dead = []
        for key, r in self.runs.items():
            idle = ref_ts - r.last_ts
            if idle > (IDLE_SHORT_SEC if r.n < 5 else IDLE_SEC):
                dead.append(key)
            elif r.ended_ts and ref_ts - r.ended_ts > IDLE_SEC:
                dead.append(key)
            elif not r.alerted_ts and ref_ts - r.first_ts > window_sec:
                dead.append(key)
        for key in dead:
            self.runs.pop(key, None)
        return len(dead)

    def clear(self) -> None:
        self.runs.clear()
        self.errors = 0
        self.observed = 0


REG = Registry()


def observe(coin: str, addr: str, side: str, px: float, sz: float, notional: float,
            ts: int, taker=None) -> None:
    """Collector kancası (bkz. app/hl/collector.py::_handle)."""
    REG.observe(coin, addr, side, px, sz, notional, ts, taker)


# ---------------- saf ölçüm / kapı ----------------

def measure(run: Run) -> dict | None:
    """Son dilimlerden düzenlilik (twap.detect, gevşek CV) + turun skaler
    toplamları. Düzensizse None. `rate_day`: pencere içindeki hızın güne yayılmışı."""
    rows = [{"ts": t, "sz": s, "notional": n} for (t, s, n, _) in run.slices]
    d = twapmod.detect(rows, cv_gap_max=CV_GAP_LIVE, cv_size_max=CV_SIZE_LIVE)
    if not d:
        return None
    span = rows[-1]["ts"] - rows[0]["ts"]
    win_total = sum(r["notional"] for r in rows)
    gaps = [b["ts"] - a["ts"] for a, b in zip(rows, rows[1:])]
    med_gap = statistics.median(gaps) if gaps else d["avg_gap"]
    rate_day = (win_total / (span + d["avg_gap"]) * 86400) if span > 0 else 0.0
    dur = run.last_ts - run.first_ts
    return {"coin": run.coin, "address": run.address, "side": run.side,
            "n": run.n, "total": run.total, "sz_total": run.sz_total,
            "first_ts": run.first_ts, "last_ts": run.last_ts, "dur": dur,
            "avg_slice": run.total / run.n if run.n else 0.0,
            "avg_gap": d["avg_gap"], "median_gap": med_gap,
            "cv_gap": d["cv_gap"], "cv_size": d["cv_size"],
            "rate_day": rate_day,
            "native_like": NATIVE_GAP[0] <= med_gap <= NATIVE_GAP[1],
            "taker_pct": (run.tk_n / run.known_n * 100) if run.known_n else None,
            "avg_px": (run.pxsz / run.sz_total) if run.sz_total else run.px_last,
            "px_first": run.px_first, "px_last": run.px_last,
            "px_chg_pct": ((run.px_last - run.px_first) / run.px_first * 100) if run.px_first else 0.0}


def parse_twap_orders(history, coin: str, side: str, mark, now_ts: int) -> list[dict]:
    """HL `userTwapHistory` snapshot'ından bu coin + yön için TWAP emirleri. SAF ve
    savunmacı: şekil tutmayan kayıt atlanır. `side` bizim 'buy'/'sell'; HL
    state.side 'B'/'A' (ya da 'Buy'/'Sell') olabilir. Aktif (activated) emirler
    önce, sonra en yeni. planned_usd = adet × bugünkü fiyat (dönüşüm, tahmin değil)."""
    out: list[dict] = []
    if not isinstance(history, (list, tuple)):
        return out
    try:
        mark = float(mark or 0)
    except (TypeError, ValueError):
        mark = 0.0
    for h in history:
        try:
            st = h.get("state") or {}
            status = str(((h.get("status") or {}).get("status")) or "").lower()
            if (st.get("coin") or "") != coin:
                continue
            s = str(st.get("side") or "").upper()
            is_buy = s.startswith("B") or s.startswith("L")
            is_sell = s.startswith("A") or s.startswith("S")
            if (side == "buy" and not is_buy) or (side == "sell" and not is_sell):
                continue
            sz = float(st.get("sz") or 0)
            ex_sz = float(st.get("executedSz") or 0)
            ex_ntl = float(st.get("executedNtl") or 0)
            minutes = float(st.get("minutes") or 0)
            t0 = int(float(st.get("timestamp") or 0))
            if t0 > 10 ** 11:
                t0 //= 1000
            px = mark or ((ex_ntl / ex_sz) if ex_sz else 0.0)
            planned_usd = sz * px
            remaining_sz = max(0.0, sz - ex_sz)
            left = int(t0 + minutes * 60 - now_ts) if (t0 and minutes) else None
            out.append({"status": status or "?", "planned_sz": sz, "planned_usd": planned_usd,
                        "executed_sz": ex_sz, "executed_usd": ex_ntl,
                        "remaining_usd": remaining_sz * px, "remaining_sz": remaining_sz,
                        "minutes": minutes, "started_ts": t0,
                        "left_sec": max(0, left) if left is not None else None,
                        "filled_pct": (ex_sz / sz * 100) if sz > 0 else None,
                        "reduce_only": bool(st.get("reduceOnly")), "randomize": bool(st.get("randomize"))})
        except (TypeError, ValueError, AttributeError):
            continue
    out.sort(key=lambda o: (o["status"] != "activated", -(o["started_ts"] or 0)))
    return out


def vol_pct_for(cfg, coin: str) -> float:
    """Hacim kuralı sınıfa göre: BTC/ETH `twap_alert_vol_pct_major` (%20 — $1-3M gürültü),
    diğerleri `twap_alert_vol_pct` (%5 — PUMP'ta $3M emir hacmin %4,8'iydi, %20 susturuyordu)."""
    from .bigpos import MAJORS
    sym = (coin or "").split(":")[-1].upper()
    if sym in MAJORS:
        return float(getattr(cfg, "twap_alert_vol_pct_major", 20) or 0)
    return float(getattr(cfg, "twap_alert_vol_pct", 5) or 0)


def left_floor(cfg, planned: float) -> float:
    """Kalan kuralı: min(min_left, planın yarısı) — $1M'lik emir yarısı dolana kadar bildirilebilsin."""
    min_left = float(getattr(cfg, "twap_alert_min_left_usd", 1_000_000) or 0)
    return min(min_left, planned * 0.5) if planned > 0 else min_left


def order_gate(cfg, order: dict | None, day_vol, vol_ts, now_ts: int, coin: str = "") -> str:
    """Emir verisiyle kapı: 'ok' | 'big' geçer; diğerleri nedenidir:
    no_order · order_done · order_small · order_left · no_vol · vol_small.
    `coin` hacim yüzdesinin sınıfını seçer (vol_pct_for)."""
    if not order:
        return "no_order"
    if order.get("status") != "activated":
        return "order_done"
    planned = float(order.get("planned_usd") or 0)
    left_usd = float(order.get("remaining_usd") or 0)
    min_left = left_floor(cfg, planned)
    big = float(getattr(cfg, "twap_alert_big_usd", 0) or 0)
    if big > 0 and planned >= big and left_usd >= min_left:
        return "big"
    if planned < float(getattr(cfg, "twap_alert_min_usd", 1_000_000) or 0):
        return "order_small"
    if left_usd < min_left:
        return "order_left"
    if not day_vol or float(day_vol) <= 0 or not vol_ts or now_ts - int(vol_ts) > VOL_MAX_AGE:
        return "no_vol"
    if planned / float(day_vol) * 100 < vol_pct_for(cfg, coin):
        return "vol_small"
    return "ok"


def gate_detail(cfg, coin: str, order: dict | None, day_vol, vol_ts, now_ts: int) -> dict:
    """Kapı kararı SAYILARLA (teşhis): {reason, planned, left, vol_pct, pct_needed, need_usd,
    left_need, day_vol}. need_usd = bildirim için gereken emir = max(min_usd, yüzde × hacim)."""
    reason = order_gate(cfg, order, day_vol, vol_ts, now_ts, coin)
    planned = float((order or {}).get("planned_usd") or 0)
    left = float((order or {}).get("remaining_usd") or 0)
    pct = vol_pct_for(cfg, coin)
    dv = float(day_vol or 0)
    min_usd = float(getattr(cfg, "twap_alert_min_usd", 1_000_000) or 0)
    return {"reason": reason, "planned": planned, "left": left, "day_vol": dv or None,
            "vol_pct": (planned / dv * 100) if dv else None, "pct_needed": pct,
            "need_usd": max(min_usd, dv * pct / 100) if dv else min_usd,
            "left_need": left_floor(cfg, planned), "status": (order or {}).get("status")}


_lookup_cache: dict[str, tuple[int, list]] = {}


async def lookup_order(collector, run: Run, now_ts: int, cfg, out: dict, force: bool = False) -> dict | None:
    """Adresin HL TWAP emrini sorgula (collector.fetch_twap_history → snapshot).
    Adres başına önbellek (`twap_lookup_cooldown`), tur başına sorgu tavanı.
    Dönüş: bu coin+yön için en uygun emir ya da None; `run.order` güncellenir."""
    addr = run.address
    cool = int(getattr(cfg, "twap_lookup_cooldown", 600) or 0)
    cached = _lookup_cache.get(addr)
    hist = None
    if cached and not force and now_ts - cached[0] < cool:
        hist = cached[1]
    else:
        if out.get("lookups", 0) >= LOOKUP_MAX_PER_EVAL:
            out["lookup_capped"] = out.get("lookup_capped", 0) + 1
            return run.order
        fn = getattr(collector, "fetch_twap_history", None)
        if fn is None:
            out["lookup_fail"] = out.get("lookup_fail", 0) + 1
            return None
        out["lookups"] = out.get("lookups", 0) + 1
        try:
            hist = await fn(addr)
        except Exception as e:
            log.debug("twap sorgusu %s: %s", addr[:10], e)
            hist = None
        if hist is None:
            out["lookup_fail"] = out.get("lookup_fail", 0) + 1
            return None
        _lookup_cache[addr] = (now_ts, hist)
        if len(_lookup_cache) > 2000:
            for k in sorted(_lookup_cache, key=lambda k: _lookup_cache[k][0])[:500]:
                _lookup_cache.pop(k, None)
    orders = parse_twap_orders(hist, run.coin, run.side, run.px_last, now_ts)
    run.lookup_ts = now_ts
    run.order = orders[0] if orders else None
    return run.order


def is_ended(run: Run, m: dict | None, ref_ts: int) -> bool:
    gap = float((m or {}).get("avg_gap") or run.rate_day and 30 or 30)
    return ref_ts - run.last_ts > max(END_GAPS * gap, END_MIN_SEC)


def next_progress_step(run: Run) -> float | None:
    """Alarm toplamının üstünde geçilen EN BÜYÜK basamak (sıçrama olursa tepe yazılır)."""
    floor = max(float(run.progress_step or 0), float(run.alert_total or 0))
    crossed = [s for s in PROGRESS_STEPS if s > floor and run.total >= s]
    return max(crossed) if crossed else None


def klass_of(coin: str) -> str:
    """kripto | hisse | endeks (mesaj rozeti ve yönlendirme) — assets.klass (para:ANSEM → kripto)."""
    from .. import assets
    return assets.klass(coin)


# ---------------- DB yardımcıları ----------------

async def volumes(client=None) -> dict[str, tuple[float, int]]:
    """{coin: (24s hacim $, ölçüm ts)} — kripto kv özetinden, hisse/endeks asset_metrics'ten.
    `client` verilirse kv bayatken (≥30 dk) tek istekle tazelenir: metrik turu dursa
    bile kripto TWAP radarı topluca `no_vol`a düşmesin."""
    out: dict[str, tuple[float, int]] = {}
    try:
        from ..hl.universe import main_dex_ctx
        ctx = await main_dex_ctx(client, ttl=VOL_MAX_AGE // 2, fetch=client is not None)
        ts = int(ctx.get("ts") or 0)
        for c, v in (ctx.get("c") or {}).items():
            if v.get("v") is not None:
                out[c] = (float(v["v"]), ts)
    except Exception:
        log.debug("ana dex hacmi okunamadı", exc_info=True)
    try:
        async with db() as conn:
            cur = await conn.execute(
                """SELECT m.coin, m.day_volume, m.ts FROM asset_metrics m
                   JOIN (SELECT coin, MAX(ts) mts FROM asset_metrics GROUP BY coin) x
                     ON x.coin = m.coin AND x.mts = m.ts
                   WHERE m.coin LIKE '%:%' AND m.day_volume IS NOT NULL""")
            for r in await cur.fetchall():
                out[r["coin"]] = (float(r["day_volume"]), int(r["ts"]))
    except Exception:
        log.debug("hisse hacmi okunamadı", exc_info=True)
    return out


async def entities(addrs: list[str]) -> dict[str, str]:
    if not addrs:
        return {}
    async with db() as conn:
        q = ",".join("?" * len(addrs))
        cur = await conn.execute(
            f"SELECT address, COALESCE(entity,'') entity FROM addresses WHERE address IN ({q})", addrs)
        return {r["address"]: r["entity"] for r in await cur.fetchall()}


async def position_ctx(client, coin: str, addr: str) -> dict | None:
    """Adresin bu coindeki pozisyonu: canlı (1 istek) — düşerse süpürme kaydı."""
    if client is not None:
        try:
            from .tracker import live_position
            live = await live_position(client, addr, coin)
            if live:
                return {"side": live["side"], "notional": live["notional"], "src": "canlı", "ts": now()}
            return {"none": True, "src": "canlı"}
        except Exception as e:
            log.debug("twap pozisyon sorgusu %s: %s", addr[:10], e)
    async with db() as conn:
        if ":" in coin:
            cur = await conn.execute(
                "SELECT side, notional, ts FROM positions_current WHERE coin=? AND address=?", (coin, addr))
        else:
            cur = await conn.execute(
                "SELECT side, notional, ts FROM addr_positions WHERE coin=? AND address=?"
                " AND closed_ts IS NULL ORDER BY ts DESC LIMIT 1", (coin, addr))
        r = await cur.fetchone()
    if r:
        return {"side": r["side"], "notional": r["notional"], "src": "süpürme", "ts": r["ts"]}
    return None


async def persist(run: Run, m: dict | None, src: str = "live") -> None:
    """twap_runs'a yaz/güncelle (PK coin,address,side,first_ts). Arşiv taraması
    aynı PK'ya rastlarsa kendi kolonlarını günceller, bizimkilere dokunmaz."""
    m = m or {}
    o = run.order or {}
    async with db() as conn:
        await conn.execute(
            """INSERT INTO twap_runs(coin,address,side,first_ts,last_ts,n_slices,total,avg_slice,
                 avg_gap,cv_gap,cv_size,taker_pct,ts,day_volume,rate_day,src,alerted_ts,ended_ts,
                 px_first,px_last,sz_total,planned_usd,planned_sz,executed_usd,remaining_usd,
                 order_ts,order_min,order_status,lookup_ts)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(coin,address,side,first_ts) DO UPDATE SET
                 last_ts=excluded.last_ts, n_slices=excluded.n_slices, total=excluded.total,
                 avg_slice=excluded.avg_slice, avg_gap=excluded.avg_gap, cv_gap=excluded.cv_gap,
                 cv_size=excluded.cv_size, taker_pct=excluded.taker_pct, ts=excluded.ts,
                 day_volume=excluded.day_volume, rate_day=excluded.rate_day, src=excluded.src,
                 alerted_ts=excluded.alerted_ts, ended_ts=excluded.ended_ts,
                 px_first=excluded.px_first, px_last=excluded.px_last, sz_total=excluded.sz_total,
                 planned_usd=excluded.planned_usd, planned_sz=excluded.planned_sz,
                 executed_usd=excluded.executed_usd, remaining_usd=excluded.remaining_usd,
                 order_ts=excluded.order_ts, order_min=excluded.order_min,
                 order_status=excluded.order_status, lookup_ts=excluded.lookup_ts""",
            (run.coin, run.address, run.side, run.first_ts, run.last_ts, run.n, run.total,
             (run.total / run.n) if run.n else 0.0, m.get("avg_gap"), m.get("cv_gap"), m.get("cv_size"),
             m.get("taker_pct"), now(), run.day_volume, run.rate_day, src, run.alerted_ts,
             run.ended_ts, run.px_first, run.px_last, run.sz_total,
             o.get("planned_usd"), o.get("planned_sz"), o.get("executed_usd"), o.get("remaining_usd"),
             o.get("started_ts"), o.get("minutes"), o.get("status"), run.lookup_ts))


async def _rehydrate(window_sec: int) -> int:
    """Restart: bildirilmiş, bitmemiş canlı turlar skaler olarak geri yüklenir —
    bitiş/ilerleme notu kaybolmasın. Dilim dizisi boş başlar (düzenlilik yeniden ölçülür)."""
    ts = now()
    async with db() as conn:
        cur = await conn.execute(
            """SELECT * FROM twap_runs WHERE src='live' AND alerted_ts IS NOT NULL
               AND ended_ts IS NULL AND last_ts >= ?""", (ts - window_sec,))
        rows = [dict(r) for r in await cur.fetchall()]
    n = 0
    for r in rows:
        key = (r["coin"], r["address"], r["side"])
        if key in REG.runs:
            continue
        run = Run(r["coin"], r["address"], r["side"], int(r["first_ts"]), float(r.get("px_first") or 0))
        run.last_ts = int(r["last_ts"])
        run.n = int(r.get("n_slices") or 0)
        run.total = float(r.get("total") or 0)
        run.sz_total = float(r.get("sz_total") or 0)
        run.px_last = float(r.get("px_last") or run.px_first)
        run.alerted_ts = int(r["alerted_ts"])
        run.alert_total = run.total
        run.day_volume = r.get("day_volume")
        run.rate_day = r.get("rate_day")
        run.lookup_ts = r.get("lookup_ts")
        if r.get("planned_usd"):
            psz = float(r.get("planned_sz") or 0)
            exu = float(r.get("executed_usd") or 0)
            run.order = {"status": r.get("order_status") or "activated", "planned_sz": psz,
                         "planned_usd": float(r["planned_usd"]), "executed_sz": None, "executed_usd": exu,
                         "remaining_usd": float(r.get("remaining_usd") or 0), "remaining_sz": None,
                         "minutes": float(r.get("order_min") or 0), "started_ts": int(r.get("order_ts") or 0),
                         "left_sec": None,
                         "filled_pct": (exu / float(r["planned_usd"]) * 100) if float(r["planned_usd"]) else None,
                         "reduce_only": False, "randomize": False}
        REG.runs[key] = run
        n += 1
    return n


def chat_for(cfg, coin: str) -> tuple[str, bool]:
    """(chat_id, gönderilebilir mi): kripto (ana dex ya da kripto dex) → CRYPTO_CHAT_ID
    (boşsa gönderme), hisse/endeks → ana sohbet."""
    if klass_of(coin) == "kripto":
        chat = (getattr(cfg, "crypto_chat_id", "") or "").strip()
        return chat, bool(chat)
    return "", True


# ---------------- değerlendirme turu ----------------

async def evaluate(cfg, notifier, client=None, collector=None) -> dict:
    """Bir tur: budama → adaylar → düzenlilik → EMİR SORGUSU → kapı → bildirim;
    bildirilmiş turlar: 10 dk'da bir yeniden sorgu → yarı dolunca ilerleme,
    bitince/iptalde bitiş notu (gerçek tutarla). Tahmin yok."""
    ts = now()
    window = int(getattr(cfg, "twap_live_window_min", 240) or 240) * 60
    out = {"keys": len(REG.runs), "observed": REG.observed, "errors": REG.errors, "cands": 0,
           "regular": 0, "irregular": 0, "lookups": 0, "lookup_fail": 0, "alerted": 0, "progress": 0,
           "ended": 0, "skipped_mm": 0, "no_chat": 0, "no_order": 0, "order_done": 0, "order_small": 0,
           "order_left": 0, "no_vol": 0, "vol_small": 0, "no_lookup": 0, "failed": 0, "best": None,
           "decisions": []}
    decisions: list[dict] = out["decisions"]

    def decide(run: "Run", reason: str, detail: dict | None = None) -> None:
        """Bu turun aday kararı — /tani "son elenenler" ve /twap komutu bunu okur."""
        d = {"coin": run.coin, "addr": run.address, "side": run.side, "n": run.n,
             "dur": int(run.last_ts - run.first_ts), "total": round(run.total),
             "reason": reason, "ts": ts}
        if detail:
            d.update({k: detail.get(k) for k in ("planned", "left", "vol_pct", "pct_needed", "need_usd", "status")})
        decisions.append(d)
    if not getattr(cfg, "twap_live_enabled", True):
        out["skipped"] = "kapalı"
        await kv_set(STATS_KV, {**out, "ts": ts})
        return out
    out["pruned"] = REG.prune(ts, window)
    out["keys"] = len(REG.runs)
    min_slices = int(getattr(cfg, "twap_alert_min_slices", 10) or 0)
    lookup_min = float(getattr(cfg, "twap_lookup_min_usd", 50_000) or 0)
    cands = [r for r in REG.runs.values()
             if r.alerted_ts or (r.n >= min_slices and r.last_ts - r.first_ts >= LOOKUP_MIN_DUR
                                 and r.total >= lookup_min)]
    out["cands"] = len(cands)
    sample = getattr(collector, "twap_sample", None) if collector is not None else None
    if sample:
        out["sample"] = str(sample)[:600]
    if collector is not None:
        for k in ("ok", "timeout", "err"):
            out[f"lookups_{k}"] = int(getattr(collector, f"twap_lookups_{k}", 0) or 0)
    if not cands:
        await kv_set(STATS_KV, {**out, "ts": ts})
        return out
    vols = await volumes(client)
    ents = await entities(list({r.address for r in cands}))
    from ..telegram import format as fmt
    for run in cands:
        try:
            m = measure(run) if run.slices else None
            key = f"{run.coin}:{run.address}:{run.side}"
            # --- bildirilmiş tur: yeniden sorgu → ilerleme / bitiş ---
            if run.alerted_ts and not run.ended_ts:
                if collector is not None and ts - int(run.lookup_ts or 0) >= REFRESH_SEC:
                    await lookup_order(collector, run, ts, cfg, out, force=True)
                order = run.order
                mm = m or _scalar_measure(run)
                active = bool(order and order.get("status") == "activated")
                done = bool(order and order.get("status") in ("finished", "terminated", "error"))
                if done or (is_ended(run, mm, ts) and not active):
                    run.ended_ts = ts
                    out["ended"] += 1
                    vol = vols.get(run.coin)
                    if vol:
                        run.day_volume = vol[0]
                    if getattr(cfg, "twap_alert_end_note", True) and not await alert_recent(
                            "twap_end", f"{key}:{run.first_ts}", 86400):
                        await alert_log("twap_end", f"{key}:{run.first_ts}", "")
                        chat, can = chat_for(cfg, run.coin)
                        if can:
                            ctx = {"order": order, "day_vol": run.day_volume, "klass": klass_of(run.coin),
                                   "cancelled": bool(order and order.get("status") in ("terminated", "error"))}
                            ok = await notifier.send("twap", fmt.twap_end(mm, ctx), priority="high", coin=run.coin,
                                                     key=f"twap_end:{key}", chat_id=chat)
                            if not ok and not (not chat and in_quiet_hours(cfg)):
                                out["failed"] += 1
                    await persist(run, m)
                    continue
                if (order and active and not run.half_ts and getattr(cfg, "twap_alert_progress", True)
                        and (order.get("filled_pct") or 0) >= HALF_PCT):
                    run.half_ts = ts
                    pkey = f"{key}:{run.first_ts}:{HALF_PCT}"
                    if not await alert_recent("twap_prog", pkey, 86400):
                        await alert_log("twap_prog", pkey, "")
                        chat, can = chat_for(cfg, run.coin)
                        if can:
                            ctx = {"order": order, "day_vol": run.day_volume, "klass": klass_of(run.coin)}
                            ok = await notifier.send("twap", fmt.twap_progress(mm, ctx), priority="high", coin=run.coin,
                                                     key=f"twap_prog:{key}", chat_id=chat)
                            out["progress"] += 1
                            if not ok and not (not chat and in_quiet_hours(cfg)):
                                out["failed"] += 1
                    await persist(run, m)
                continue
            # --- ilk alarm: düzenli dizi → emir sorgusu → kapı ---
            if not m:
                out["irregular"] += 1
                decide(run, "irregular")
                continue
            out["regular"] += 1
            if ents.get(run.address) in ("mm", "vault"):
                out["skipped_mm"] += 1
                decide(run, "mm")
                continue
            if await alert_recent("twap", key, int(getattr(cfg, "twap_alert_cooldown", 21600) or 0)):
                run.alerted_ts = run.alerted_ts or ts        # bekleme içinde: tekrar sayılmaz
                run.alert_total = run.alert_total or run.total
                decide(run, "cooldown")
                continue
            if collector is None:
                out["no_lookup"] += 1                     # emir doğrulanamaz → tahminle bildirim YOK
                decide(run, "no_lookup")
                continue
            lf_before = out.get("lookup_fail", 0)
            order = await lookup_order(collector, run, ts, cfg, out)
            vol = vols.get(run.coin)
            day_vol, vol_ts = (vol if vol else (None, None))
            det = gate_detail(cfg, run.coin, order, day_vol, vol_ts, ts)
            g = det["reason"]
            planned = det["planned"]
            if order and ((not out["best"]) or planned > out["best"]["planned"]):
                out["best"] = {"coin": run.coin, "side": run.side, "planned": planned,
                               "left": (order or {}).get("remaining_usd"), "status": order.get("status"),
                               "vol_pct": det["vol_pct"]}
            if g not in ("ok", "big"):
                out[g] = out.get(g, 0) + 1
                decide(run, "lookup_fail" if (g == "no_order" and out.get("lookup_fail", 0) > lf_before) else g, det)
                continue
            chat, can = chat_for(cfg, run.coin)
            run.alerted_ts, run.alert_total, run.gate = ts, run.total, g
            run.day_volume = day_vol
            if not can:
                out["no_chat"] += 1
                decide(run, "no_chat", det)
                await persist(run, m)
                continue
            if not chat:
                await alert_log("twap", key, "")          # hisse yolu: sessiz saat özeti için işaret ÖNCE
            pos = await position_ctx(client, run.coin, run.address)
            ctx = {"order": order, "day_vol": day_vol, "vol_pct": det["vol_pct"],
                   "pos": pos, "gate": g, "klass": klass_of(run.coin), "entity": ents.get(run.address) or ""}
            text = fmt.twap_alert(m, ctx)
            ok = await notifier.send("twap", text, priority="high", key=f"twap:{key}", chat_id=chat, coin=run.coin)
            if ok:
                await alert_log("twap", key, text)
                out["alerted"] += 1
                decide(run, "alerted", det)
                log.info("canlı twap: %s %s %s emir %.0f$ kalan %.0f$ (%s) bildirildi", run.coin, run.side,
                         run.address[:10], planned, float(order.get("remaining_usd") or 0), g)
            elif not chat and in_quiet_hours(cfg):
                out["quiet"] = out.get("quiet", 0) + 1          # sabah özetine bırakıldı, hata değil
                decide(run, "alerted", det)
            else:
                # Kripto yolu: işaret yazılmadı → 6 saatlik bekleme yanmaz, sonraki tur yeniden dener
                out["failed"] += 1
                run.alerted_ts = None
                decide(run, "failed", det)
                await alert_log("fail:twap", key, text)
            await persist(run, m)
        except Exception:
            out["errors"] += 1
            log.exception("canlı twap değerlendirme hatası %s", getattr(run, "key", "?"))
    decisions.sort(key=lambda d: -(d.get("planned") or d.get("total") or 0))
    del decisions[DECISIONS_MAX:]
    if decisions:
        try:
            await _remember(decisions)
        except Exception:
            log.debug("twaplive_last yazılamadı", exc_info=True)
    await kv_set(STATS_KV, {**out, "ts": ts})
    return out


async def _remember(decisions: list[dict]) -> None:
    """Son kararları biriktir (aynı coin+adres+yön için en yenisi kalır, en çok LAST_MAX)."""
    last = await kv_get(LAST_KV) or []
    seen = set()
    merged = []
    for d in list(decisions) + list(last):
        k = (d.get("coin"), d.get("addr"), d.get("side"))
        if k in seen:
            continue
        seen.add(k)
        merged.append(d)
    await kv_set(LAST_KV, merged[:LAST_MAX])


def decision_line(d: dict) -> str:
    """Tek karar, düz metin: 'PUMP 0x60b0…991e buy $3.0M emir, hacmin %4.8'i → hacme göre küçük (%5 gerekir)'."""
    from ..telegram.format import short, usd
    r = d.get("reason") or "?"
    txt = f"{d.get('coin')} {short(d.get('addr') or '')} {d.get('side')}"
    if d.get("planned"):
        txt += f" {usd(d['planned'])} emir"
        if d.get("vol_pct") is not None:
            txt += f", hacmin %{float(d['vol_pct']):.1f}'i"
    else:
        txt += f" {d.get('n', 0)} dilim {usd(d.get('total') or 0)}"
    txt += f" → {REASON_TR.get(r, r)}"
    if r == "vol_small" and d.get("pct_needed") is not None:
        txt += f" (%{float(d['pct_needed']):g} gerekir)"
    elif r == "order_left" and d.get("left") is not None:
        txt += f" (kalan {usd(d['left'])})"
    return txt


# ---------------- teşhis metinleri (/twap komutu — bot'tan bağımsız, test edilir)

async def _marks() -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        from ..hl.universe import main_dex_ctx
        ctx = await main_dex_ctx(None, fetch=False)
        for c, v in (ctx.get("c") or {}).items():
            if v.get("m"):
                out[c] = float(v["m"])
    except Exception:
        pass
    return out


def _runs_for(pred) -> list["Run"]:
    return sorted((r for r in REG.runs.values() if pred(r)), key=lambda r: -r.total)


def _run_line(r: "Run") -> str:
    from ..telegram.format import short, usd
    m = measure(r) if r.slices else None
    dur = int(r.last_ts - r.first_ts)
    reg = f"düzenli ✓ (aralık ~{int((m or {}).get('median_gap') or 0)} sn)" if m else "düzensiz/az"
    flag = " · bildirildi" if r.alerted_ts else ""
    return f"{r.coin} {short(r.address)} {r.side} · {r.n} dilim · {dur // 60} dk · {usd(r.total)} · {reg}{flag}"


async def diag_address(cfg, collector, addr: str) -> str:
    """/twap 0xADRES: bellekteki diziler, HL'deki TWAP emirleri (şimdi sorulur) ve her emrin
    kapı kararı sayılarla, son kararlar."""
    from ..telegram.format import alink, esc, usd
    addr = (addr or "").lower()
    ts = now()
    lines = [f"📡 <b>Canlı TWAP</b> · 👤 {alink(addr)}"]
    runs = _runs_for(lambda r: r.address == addr)
    lines.append("Bellekteki diziler: " + ("\n".join("• " + esc(_run_line(r)) for r in runs[:8]) if runs
                                            else "yok (son 4 saatte bu adresten düzenli akış görmedik)"))
    fn = getattr(collector, "fetch_twap_history", None) if collector is not None else None
    if fn is None:
        lines.append("HL TWAP emirleri: sorgulanamadı (collector/soket yok)")
    else:
        try:
            hist = await fn(addr)
        except Exception as e:
            hist = None
            lines.append(f"HL TWAP emirleri: sorgu hatası ({esc(e)})")
        if hist is None:
            lines.append("HL TWAP emirleri: sorgu başarısız (zaman aşımı / soket)")
        elif not hist:
            lines.append("HL TWAP emirleri: yok (userTwapHistory boş)")
        else:
            marks = await _marks()
            vols = await volumes()
            lines.append(f"HL TWAP emirleri ({len(hist)} kayıt, şimdi soruldu):")
            shown = 0
            for h in hist:
                st = (h or {}).get("state") or {}
                coin = str(st.get("coin") or "")
                s = str(st.get("side") or "").upper()
                side = "buy" if (s.startswith("B") or s.startswith("L")) else "sell"
                o = next(iter(parse_twap_orders([h], coin, side, marks.get(coin), ts)), None)
                if not o:
                    continue
                vol = vols.get(coin)
                det = gate_detail(cfg, coin, o, vol[0] if vol else None, vol[1] if vol else None, ts)
                fill = f"doldu {usd(o['executed_usd'])} (%{o['filled_pct']:.0f})" if o.get("filled_pct") is not None else ""
                lines.append(f"• {esc(coin)} {side.upper()} {usd(o['planned_usd'])} · {fill} · kalan {usd(o['remaining_usd'])}"
                             f" · {int(o['minutes'])} dk · {esc(o['status'])}")
                kap = f"  kapı: {REASON_TR.get(det['reason'], det['reason'])}"
                if det["day_vol"]:
                    kap += f" — hacim {usd(det['day_vol'])}, emir hacmin %{det['vol_pct']:.1f}'i (gerek %{det['pct_needed']:g}, ≥ {usd(det['need_usd'])})"
                if det["reason"] == "order_left":
                    kap += f" — kalan {usd(det['left'])} < {usd(det['left_need'])}"
                lines.append(esc(kap))
                shown += 1
                if shown >= 6:
                    break
    last = [d for d in (await kv_get(LAST_KV) or []) if d.get("addr") == addr]
    if last:
        lines.append("Radarın son kararları: " + " · ".join(esc(decision_line(d)) for d in last[:5]))
    return "\n".join(lines)


async def diag_coin(cfg, coin: str) -> str:
    """/twap COIN: dinleniyor mu, 24s hacim, bildirim için gereken emir, coindeki diziler, son kararlar."""
    from ..telegram.format import esc, usd
    coin = (coin or "").upper()
    ws = await kv_get("ws_universe") or {}
    listened = coin in set(ws.get("crypto") or [])
    top = int(getattr(cfg, "crypto_watch_top", 120) or 0)
    lines = [f"📡 <b>{esc(coin)}</b> · " + (f"dinleniyor ✓ (ana dex, hacimce ilk {top})" if listened
                                           else f"dinlenMİyor ✗ (ana dex ilk {top} listesinde değil ya da hisse dex'i)")]
    vols = await volumes()
    vol = vols.get(coin)
    pct = vol_pct_for(cfg, coin)
    min_usd = float(getattr(cfg, "twap_alert_min_usd", 1_000_000) or 0)
    if vol:
        lines.append(f"24s hacim {usd(vol[0])} · bildirim için emir ≥ {usd(max(min_usd, vol[0] * pct / 100))}"
                     f" (max({usd(min_usd)}, %{pct:g} × hacim)) · kalan ≥ min({usd(getattr(cfg, 'twap_alert_min_left_usd', 1e6))}, planın yarısı)")
    else:
        lines.append(f"24s hacim bilinmiyor → kapı geçmez (no_vol); taban {usd(min_usd)}, yüzde %{pct:g}")
    runs = _runs_for(lambda r: r.coin == coin)
    lines.append(f"Bellekteki diziler ({len(runs)}):" + ("\n" + "\n".join("• " + esc(_run_line(r)) for r in runs[:6]) if runs else " yok"))
    last = [d for d in (await kv_get(LAST_KV) or []) if d.get("coin") == coin]
    if last:
        lines.append("Radarın son kararları: " + " · ".join(esc(decision_line(d)) for d in last[:5]))
    return "\n".join(lines)


async def diag_summary(cfg) -> str:
    """/twap: son tur özeti + son kararlar."""
    from ..telegram.format import esc
    st = await kv_get(STATS_KV) or {}
    if not st.get("ts"):
        return "📡 Canlı TWAP: tur henüz çalışmadı."
    lines = [f"📡 <b>Canlı TWAP</b> · {st.get('keys', 0)} dizi bellekte · {st.get('cands', 0)} aday · "
             f"{st.get('regular', 0)} düzenli · {st.get('irregular', 0)} düzensiz · {st.get('alerted', 0)} bildirim"
             + (f" · sorgu ✓{st.get('lookups_ok', 0)}/⏱{st.get('lookups_timeout', 0)}/✗{st.get('lookups_err', 0)}"
                if "lookups_ok" in st else "")]
    last = await kv_get(LAST_KV) or []
    if last:
        lines.append("Son kararlar:\n" + "\n".join("• " + esc(decision_line(d)) for d in last[:10]))
    lines.append("Kullanım: /twap 0xADRES · /twap COIN")
    return "\n".join(lines)


def _scalar_measure(run: Run) -> dict:
    """Dilim dizisi yokken (restart sonrası) bitiş notu için skaler ölçü."""
    dur = run.last_ts - run.first_ts
    n = max(1, run.n)
    return {"coin": run.coin, "address": run.address, "side": run.side, "n": run.n,
            "total": run.total, "sz_total": run.sz_total, "first_ts": run.first_ts,
            "last_ts": run.last_ts, "dur": dur, "avg_slice": run.total / n,
            "avg_gap": (dur / (n - 1)) if n > 1 else 0, "median_gap": (dur / (n - 1)) if n > 1 else 0,
            "cv_gap": None, "cv_size": None, "rate_day": run.rate_day or 0.0, "native_like": False,
            "taker_pct": (run.tk_n / run.known_n * 100) if run.known_n else None,
            "avg_px": (run.pxsz / run.sz_total) if run.sz_total else run.px_last,
            "px_first": run.px_first, "px_last": run.px_last,
            "px_chg_pct": ((run.px_last - run.px_first) / run.px_first * 100) if run.px_first else 0.0}


async def loop(cfg, client, notifier, collector=None) -> None:
    """Denetimli döngü: 60 sn'de bir değerlendirme. Kanca ekstra istek yapmaz;
    aday başına bir WS emir sorgusu (kısa abonelik)."""
    from ..health import beat
    await asyncio.sleep(90)
    try:
        n = await _rehydrate(int(getattr(cfg, "twap_live_window_min", 240) or 240) * 60)
        if n:
            log.info("canlı twap: %d bildirilmiş tur geri yüklendi", n)
    except Exception:
        log.debug("canlı twap geri yükleme", exc_info=True)
    while True:
        try:
            await beat("twaplive")
            await evaluate(cfg, notifier, client, collector)
            await beat("twaplive")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("canlı twap turu hatası")
            await kv_set(STATS_KV, {"error": f"{type(e).__name__}: {e}"[:200], "ts": now()})
        await asyncio.sleep(max(20, int(getattr(cfg, "twap_live_eval_sec", 60) or 60)))
