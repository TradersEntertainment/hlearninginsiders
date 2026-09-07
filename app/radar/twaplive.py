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

Kapı (gözlenen toplam üzerinden, tahmin değil):
  A) toplam ≥ twap_alert_min_usd VE dilim hızı güne yayılınca 24s hacmin
     ≥ twap_alert_rate_pct'i  ($2.2K / 30 sn ≈ $6.3M/gün, hacim $9.9M → %64)
  B) toplam ≥ twap_alert_big_usd (hacimden bağımsız; BTC'de bile ilginç)
Kripto → CRYPTO_CHAT_ID (boşsa gönderilmez), hisse/endeks → ana sohbet.
İlerleme notu (toplam basamakları) ve bitiş notu (3 aralık dilim gelmeyince).

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
VOL_MAX_AGE = 3600          # hacim ölçümü bundan eskiyse oran kapısı atlanır
NATIVE_GAP = (25, 35)       # ortanca aralık bu banttaysa "HL TWAP düzenine uyuyor"
STATS_KV = "twaplive_stats"


class Run:
    __slots__ = ("coin", "address", "side", "first_ts", "last_ts", "n", "total", "sz_total",
                 "pxsz", "px_first", "px_last", "tk_n", "known_n", "slices", "alerted_ts",
                 "alert_total", "progress_ts", "progress_step", "ended_ts", "day_volume",
                 "rate_day", "gate")

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


def gate(cfg, m: dict, day_vol, vol_ts, ref_ts: int) -> str:
    """'ratio' (hacme göre büyük) | 'big' (hacimden bağımsız) | '' (kapı kapalı)."""
    if int(m.get("n") or 0) < int(getattr(cfg, "twap_alert_min_slices", 20) or 0):
        return ""
    if int(m.get("dur") or 0) < MIN_DUR_SEC:
        return ""
    total = float(m.get("total") or 0)
    if total >= float(getattr(cfg, "twap_alert_big_usd", 5_000_000) or 0):
        return "big"
    if day_vol and float(day_vol) > 0 and vol_ts and ref_ts - int(vol_ts) <= VOL_MAX_AGE:
        rate_pct = float(m.get("rate_day") or 0) / float(day_vol) * 100
        if (total >= float(getattr(cfg, "twap_alert_min_usd", 100_000) or 0)
                and rate_pct >= float(getattr(cfg, "twap_alert_rate_pct", 20) or 0)):
            return "ratio"
    return ""


def is_ended(run: Run, m: dict | None, ref_ts: int) -> bool:
    gap = float((m or {}).get("avg_gap") or run.rate_day and 30 or 30)
    return ref_ts - run.last_ts > max(END_GAPS * gap, END_MIN_SEC)


def next_progress_step(run: Run) -> float | None:
    """Alarm toplamının üstünde geçilen EN BÜYÜK basamak (sıçrama olursa tepe yazılır)."""
    floor = max(float(run.progress_step or 0), float(run.alert_total or 0))
    crossed = [s for s in PROGRESS_STEPS if s > floor and run.total >= s]
    return max(crossed) if crossed else None


def klass_of(coin: str) -> str:
    """kripto | hisse | endeks (mesaj rozeti ve yönlendirme)."""
    if ":" not in (coin or ""):
        return "kripto"
    from .. import assets
    return "endeks" if assets.kind(coin) == "non_equity" else "hisse"


# ---------------- DB yardımcıları ----------------

async def volumes() -> dict[str, tuple[float, int]]:
    """{coin: (24s hacim $, ölçüm ts)} — kripto kv özetinden, hisse/endeks asset_metrics'ten."""
    out: dict[str, tuple[float, int]] = {}
    try:
        from ..hl.universe import main_dex_ctx
        ctx = await main_dex_ctx(None, fetch=False)
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
    async with db() as conn:
        await conn.execute(
            """INSERT INTO twap_runs(coin,address,side,first_ts,last_ts,n_slices,total,avg_slice,
                 avg_gap,cv_gap,cv_size,taker_pct,ts,day_volume,rate_day,src,alerted_ts,ended_ts,
                 px_first,px_last,sz_total)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(coin,address,side,first_ts) DO UPDATE SET
                 last_ts=excluded.last_ts, n_slices=excluded.n_slices, total=excluded.total,
                 avg_slice=excluded.avg_slice, avg_gap=excluded.avg_gap, cv_gap=excluded.cv_gap,
                 cv_size=excluded.cv_size, taker_pct=excluded.taker_pct, ts=excluded.ts,
                 day_volume=excluded.day_volume, rate_day=excluded.rate_day, src=excluded.src,
                 alerted_ts=excluded.alerted_ts, ended_ts=excluded.ended_ts,
                 px_first=excluded.px_first, px_last=excluded.px_last, sz_total=excluded.sz_total""",
            (run.coin, run.address, run.side, run.first_ts, run.last_ts, run.n, run.total,
             (run.total / run.n) if run.n else 0.0, m.get("avg_gap"), m.get("cv_gap"), m.get("cv_size"),
             m.get("taker_pct"), now(), run.day_volume, run.rate_day, src, run.alerted_ts,
             run.ended_ts, run.px_first, run.px_last, run.sz_total))


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
        REG.runs[key] = run
        n += 1
    return n


def chat_for(cfg, coin: str) -> tuple[str, bool]:
    """(chat_id, gönderilebilir mi): kripto → CRYPTO_CHAT_ID (boşsa gönderme),
    hisse/endeks → ana sohbet."""
    if ":" not in (coin or ""):
        chat = (getattr(cfg, "crypto_chat_id", "") or "").strip()
        return chat, bool(chat)
    return "", True


# ---------------- değerlendirme turu ----------------

async def evaluate(cfg, notifier, client=None) -> dict:
    ts = now()
    window = int(getattr(cfg, "twap_live_window_min", 240) or 240) * 60
    out = {"keys": len(REG.runs), "observed": REG.observed, "errors": REG.errors, "cands": 0,
           "regular": 0, "alerted": 0, "progress": 0, "ended": 0, "skipped_mm": 0,
           "no_chat": 0, "no_vol": 0, "failed": 0, "best": None}
    if not getattr(cfg, "twap_live_enabled", True):
        out["skipped"] = "kapalı"
        await kv_set(STATS_KV, {**out, "ts": ts})
        return out
    out["pruned"] = REG.prune(ts, window)
    out["keys"] = len(REG.runs)
    min_slices = int(getattr(cfg, "twap_alert_min_slices", 20) or 0)
    cands = [r for r in REG.runs.values()
             if r.alerted_ts or (r.n >= min_slices and r.last_ts - r.first_ts >= MIN_DUR_SEC)]
    out["cands"] = len(cands)
    if not cands:
        await kv_set(STATS_KV, {**out, "ts": ts})
        return out
    vols = await volumes()
    ents = await entities(list({r.address for r in cands}))
    from ..telegram import format as fmt
    for run in cands:
        try:
            m = measure(run) if run.slices else None
            key = f"{run.coin}:{run.address}:{run.side}"
            # --- bitiş notu (bildirilmiş turlar) ---
            if run.alerted_ts and not run.ended_ts:
                if is_ended(run, m, ts):
                    run.ended_ts = ts
                    out["ended"] += 1
                    vol = vols.get(run.coin)
                    if vol:
                        run.day_volume = vol[0]
                    mm = m or _scalar_measure(run)
                    if getattr(cfg, "twap_alert_end_note", True) and not await alert_recent(
                            "twap_end", f"{key}:{run.first_ts}", 86400):
                        await alert_log("twap_end", f"{key}:{run.first_ts}", "")
                        chat, can = chat_for(cfg, run.coin)
                        if can:
                            text = fmt.twap_end(mm, {"day_vol": run.day_volume, "klass": klass_of(run.coin)})
                            ok = await notifier.send("twap", text, priority="high",
                                                     key=f"twap_end:{key}", chat_id=chat)
                            if not ok:
                                out["failed"] += 1
                    await persist(run, m)
                    continue
                # --- ilerleme notu ---
                if m and getattr(cfg, "twap_alert_progress", True):
                    step = next_progress_step(run)
                    if step and (not run.progress_ts or ts - run.progress_ts >= PROGRESS_MIN_GAP):
                        run.progress_ts, run.progress_step = ts, step
                        vol = vols.get(run.coin)
                        if vol:
                            run.day_volume, run.rate_day = vol[0], m["rate_day"]
                        pkey = f"{key}:{run.first_ts}:{int(step)}"
                        if not await alert_recent("twap_prog", pkey, 86400):
                            await alert_log("twap_prog", pkey, "")
                            chat, can = chat_for(cfg, run.coin)
                            if can:
                                ctx = {"day_vol": run.day_volume, "step": step, "alert_total": run.alert_total,
                                       "klass": klass_of(run.coin),
                                       "rate_pct": (m["rate_day"] / run.day_volume * 100) if run.day_volume else None}
                                ok = await notifier.send("twap", fmt.twap_progress(m, ctx), priority="high",
                                                         key=f"twap_prog:{key}", chat_id=chat)
                                out["progress"] += 1
                                if not ok:
                                    out["failed"] += 1
                        await persist(run, m)
                continue
            # --- ilk alarm ---
            if not m:
                continue
            out["regular"] += 1
            if ents.get(run.address) in ("mm", "vault"):
                out["skipped_mm"] += 1
                continue
            vol = vols.get(run.coin)
            day_vol, vol_ts = (vol if vol else (None, None))
            g = gate(cfg, m, day_vol, vol_ts, ts)
            rate_pct = (m["rate_day"] / day_vol * 100) if day_vol else None
            if not day_vol or (vol_ts and ts - int(vol_ts) > VOL_MAX_AGE):
                out["no_vol"] += 1
            if (not out["best"]) or m["total"] > out["best"]["total"]:
                out["best"] = {"coin": run.coin, "side": run.side, "total": m["total"],
                               "rate_pct": rate_pct, "n": run.n}
            if not g:
                continue
            if await alert_recent("twap", key, int(getattr(cfg, "twap_alert_cooldown", 21600) or 0)):
                run.alerted_ts = run.alerted_ts or ts        # bekleme içinde: tekrar sayılmaz
                run.alert_total = run.alert_total or run.total
                continue
            chat, can = chat_for(cfg, run.coin)
            run.alerted_ts, run.alert_total, run.gate = ts, run.total, g
            run.day_volume, run.rate_day = day_vol, m["rate_day"]
            if not can:
                out["no_chat"] += 1
                await persist(run, m)
                continue
            await alert_log("twap", key, "")                  # işaret ÖNCE (sessiz saat/tekrar)
            pos = await position_ctx(client, run.coin, run.address)
            ctx = {"day_vol": day_vol, "rate_pct": rate_pct, "pos": pos, "gate": g,
                   "klass": klass_of(run.coin), "entity": ents.get(run.address) or ""}
            text = fmt.twap_alert(m, ctx)
            ok = await notifier.send("twap", text, priority="high", key=f"twap:{key}", chat_id=chat)
            if ok:
                await alert_log("twap", key, text)
                out["alerted"] += 1
                log.info("canlı twap: %s %s %s toplam %.0f$ (%s) bildirildi", run.coin, run.side,
                         run.address[:10], run.total, g)
            elif not chat and in_quiet_hours(cfg):
                out["quiet"] = out.get("quiet", 0) + 1          # sabah özetine bırakıldı, hata değil
            else:
                out["failed"] += 1
                await alert_log("fail:twap", key, text)
            await persist(run, m)
        except Exception:
            out["errors"] += 1
            log.exception("canlı twap değerlendirme hatası %s", getattr(run, "key", "?"))
    await kv_set(STATS_KV, {**out, "ts": ts})
    return out


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


async def loop(cfg, client, notifier) -> None:
    """Denetimli döngü: 60 sn'de bir değerlendirme. Tamamen yerel."""
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
            await evaluate(cfg, notifier, client)
            await beat("twaplive")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("canlı twap turu hatası")
            await kv_set(STATS_KV, {"error": f"{type(e).__name__}: {e}"[:200], "ts": now()})
        await asyncio.sleep(max(20, int(getattr(cfg, "twap_live_eval_sec", 60) or 60)))
