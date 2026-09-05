"""Liq attack radarı — "hafta sonu yakın pozları patlatıp fiyata geri dönüyorlar".

MEKANİZMA. Hafta sonu hissenin GERÇEK fiyatı sabittir (borsa kapalı). Perp
fiyatını yakın likidasyon kümesine kadar itip patlatan biri için dönüş
garantidir: Pazartesi fiyat zaten Cuma kapanışına döner. Saldırgan için
neredeyse risksiz — ve tam da bu yüzden ÖNGÖRÜLEBİLİR: hedef (yakın küme),
pencere (hafta sonu, ince defter) ve dönüş çıpası (Cuma kapanışı) önceden belli.

SKOR = patlayacak $ / itmek için yenmesi gereken defter $.
  • Payda emir defterinden (l2Book, CANLI): fiyatı d% itmek için o yöndeki
    seviyelerin toplam notionali. Görünen defter d'ye VARMADAN bitiyorsa
    (`book_thin`) itmek neredeyse bedava — en güçlü sinyal.
  • Pay likidasyon haritasının verisi: d içinde liq fiyatı olan pozisyonlar.
  • d, ince bir ızgarada taranır; L(d) ≥ eşik olan d'ler arasında oranı
    maksimize eden seçilir. Oran bir KÂR tahmini değil, göreli çekicilik.

Sitedeki her tahminci gibi KARNE tutar: geçmiş hafta sonlarında gerçekleşen
saldırıları (1 dk'lık mark serisinde sıçrama + o aralıkta liq olan pozisyonlar
+ geri dönüş) sonradan tespit eder ve "önceden işaretlemiş miydik" diye ölçer.
Karnesiz tahmin sadece histir.

Yalnız HAFTA SONU tarar (kullanıcı tercihi ve mekanizmanın gereği). Pencere
dışında sayfa son sonuçları ve karneyi gösterir, istek atmaz.
"""
import asyncio
import logging

from ..db import alert_log, alert_recent, db, kv_set, now
from . import hourstats
from .bookwall import _parse_book

log = logging.getLogger("radar.liqattack")

GRID_STEP = 0.25          # d ızgarası (%)
MIN_COST_USD = 5_000      # payda tabanı: sıfır/ufak defterde oran sonsuza gitmesin
TOP_TARGETS = 3           # mesaj/sayfada gösterilen hedef pozisyon
DETECT_LOOKBACK_D = 21    # geçmiş saldırı taraması kaç gün geriye baksın
DETECT_REF_MIN = 30       # sıçrama referansı: önceki bu kadar dakikanın medyanı
DETECT_REVERT_PCT = 0.5   # geri dönüş: referansın bu kadar yakınına
RETENTION_D = 90


# ─────────────────────────────────────────────── saf hesaplar (test edilir)

def depth_cost(bids: list[dict], asks: list[dict], mark: float, direction: str,
               d_pct: float) -> tuple[float, bool]:
    """Fiyatı d% itmek için yenmesi gereken defter $ ve 'defter oraya varmıyor'.

    down: satarak BID'leri yersin (mark → mark·(1-d)); up: alarak ASK'leri.
    Görünen seviyeler d'ye kadar uzanmıyorsa maliyet görünenin toplamı ve
    `thin=True` — gerçek maliyet bundan BÜYÜK olamaz, yani oran alt sınır değil
    üst sınır: dürüst yönde hata.
    """
    if direction == "down":
        lim = mark * (1 - d_pct / 100)
        lv = sorted(bids, key=lambda x: -x["px"])
        inside = [x for x in lv if x["px"] >= lim]
        reaches = any(x["px"] < lim for x in lv)
    else:
        lim = mark * (1 + d_pct / 100)
        lv = sorted(asks, key=lambda x: x["px"])
        inside = [x for x in lv if x["px"] <= lim]
        reaches = any(x["px"] > lim for x in lv)
    cost = sum(x["px"] * x["sz"] for x in inside)
    return cost, not reaches


def liq_within(rows: list[dict], mark: float, direction: str,
               d_pct: float) -> list[dict]:
    """d içinde patlayacak pozisyonlar (yönü doğru tarafta olanlar)."""
    out = []
    for p in rows:
        liq = p.get("liq_px")
        if not liq or (p.get("notional") or 0) <= 0:
            continue
        if direction == "down" and p.get("side") == "long" and \
                mark * (1 - d_pct / 100) <= liq < mark:
            out.append(p)
        elif direction == "up" and p.get("side") == "short" and \
                mark < liq <= mark * (1 + d_pct / 100):
            out.append(p)
    return out


def score_direction(rows: list[dict], bids: list[dict], asks: list[dict],
                    mark: float, direction: str, min_usd: float,
                    max_dist_pct: float) -> dict | None:
    """Bir yön için en çekici d*. L(d*) ≥ min_usd yoksa None."""
    best, steps = None, int(round(max_dist_pct / GRID_STEP))
    for i in range(1, steps + 1):
        d = round(i * GRID_STEP, 4)
        hit = liq_within(rows, mark, direction, d)
        L = sum(p["notional"] for p in hit)
        if L < min_usd:
            continue
        C, thin = depth_cost(bids, asks, mark, direction, d)
        ratio = L / max(C, MIN_COST_USD)
        if not best or ratio > best["score"]:
            hit.sort(key=lambda p: -p["notional"])
            best = {"direction": direction, "dist_pct": d, "liq_usd": L,
                    "cost_usd": C, "score": ratio, "book_thin": thin,
                    "n_pos": len(hit),
                    "targets": [{"address": p["address"], "notional": p["notional"],
                                 "liq_px": p["liq_px"]} for p in hit[:TOP_TARGETS]],
                    "target_px": mark * (1 - d / 100) if direction == "down"
                    else mark * (1 + d / 100)}
    return best


def find_spikes(series: list[tuple[int, float]], spike_pct: float,
                revert_min: int) -> list[dict]:
    """1 dk'lık (ts, px) serisinde 'sıçra ve geri dön' desenleri.

    Referans: önceki DETECT_REF_MIN dakikanın medyanı. Sapma ≥ spike_pct olur,
    sonra revert_min içinde referansın DETECT_REVERT_PCT yakınına dönerse
    bir olay. Geri DÖNMEYEN hareket olay DEĞİL — o gerçek fiyat hareketi.
    """
    out, i, n = [], 0, len(series)
    while i < n:
        ts, px = series[i]
        ref_win = [p for t, p in series[max(0, i - DETECT_REF_MIN):i]
                   if ts - t <= DETECT_REF_MIN * 60 * 2]
        if len(ref_win) < 5:
            i += 1
            continue
        ref = sorted(ref_win)[len(ref_win) // 2]
        dev = (px - ref) / ref * 100
        if abs(dev) < spike_pct:
            i += 1
            continue
        direction = "down" if dev < 0 else "up"
        # sıçramanın ucu ve geri dönüş
        j, ext_px, ext_ts, end = i, px, ts, None
        while j < n and series[j][0] - ts <= revert_min * 60:
            t2, p2 = series[j]
            if (direction == "down" and p2 < ext_px) or (direction == "up" and p2 > ext_px):
                ext_px, ext_ts = p2, t2
            if abs((p2 - ref) / ref * 100) <= DETECT_REVERT_PCT and t2 > ext_ts:
                end = t2
                break
            j += 1
        if end is None:
            i = j + 1 if j > i else i + 1
            continue
        out.append({"ts_start": ts, "ts_peak": ext_ts, "ts_end": end,
                    "direction": direction, "ref_px": ref, "extreme_px": ext_px,
                    "move_pct": abs(ext_px - ref) / ref * 100})
        i = j + 1
    return out


# ─────────────────────────────────────────────── veri

async def _equity_positions() -> dict[str, list[dict]]:
    async with db() as conn:
        cur = await conn.execute(
            """SELECT p.coin, p.address, p.side, p.notional, p.liq_px, t.symbol
               FROM positions_current p JOIN tickers t ON t.coin = p.coin
               WHERE p.liq_px IS NOT NULL AND p.notional > 0""")
        rows = [dict(r) for r in await cur.fetchall()]
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["coin"], []).append(r)
    return by


async def _marks(coins: list[str]) -> dict[str, float]:
    if not coins:
        return {}
    q = ",".join("?" * len(coins))
    async with db() as conn:
        cur = await conn.execute(
            f"""SELECT coin, mark_px FROM asset_metrics
                WHERE coin IN ({q}) AND ts >= ?
                ORDER BY ts""", (*coins, now() - 2 * 3600))
        out = {}
        for r in await cur.fetchall():
            if r["mark_px"]:
                out[r["coin"]] = float(r["mark_px"])        # sonuncu kazanır
        return out


async def _fri_close_dev(cfg) -> dict[str, float]:
    """Cuma kapanışından sapma — 'zaten gerilmiş mi' bağlamı."""
    try:
        from .offhours import screener
        scr = await screener(cfg)
        return {r["coin"]: r["dev"] for r in scr["rows"]}
    except Exception:
        return {}


# ─────────────────────────────────────────────── tarama (yalnız hafta sonu)

async def scan(cfg, client, notifier=None) -> dict:
    out = {"window": False, "coins": 0, "books": 0, "book_err": 0,
           "candidates": 0, "alerted": 0, "failed": 0, "skipped": ""}
    wk = hourstats.weekend_window(None, int(getattr(cfg, "offhours_close_hour", 0)))
    if not wk:
        out["skipped"] = "hafta sonu değil — pencere kapalı"
        return await _stats(out)
    out["window"] = True
    anchor = wk[0]
    min_usd = float(getattr(cfg, "liq_attack_min_usd", 2_000_000))
    max_dist = float(getattr(cfg, "liq_attack_max_dist_pct", 4.0))
    min_score = float(getattr(cfg, "liq_attack_min_score", 2.0))
    cool = int(getattr(cfg, "liq_attack_cooldown", 4 * 3600))

    by = await _equity_positions()
    marks = await _marks(list(by))
    devs = await _fri_close_dev(cfg)
    ts = now()
    rows_out = []
    for coin, rows in by.items():
        mark = marks.get(coin)
        if not mark:
            continue
        # ÖN ELEME: max_dist içinde eşik kadar liq yoksa defter çekmeye değmez
        # (istek bütçesi: hafta sonu her 5 dk, coin başına 1 l2Book).
        near_dn = sum(p["notional"] for p in liq_within(rows, mark, "down", max_dist))
        near_up = sum(p["notional"] for p in liq_within(rows, mark, "up", max_dist))
        if near_dn < min_usd and near_up < min_usd:
            continue
        out["coins"] += 1
        try:
            book = await client.l2_book(coin)
            bids, asks = _parse_book(book)
            out["books"] += 1
        except Exception as e:
            out["book_err"] += 1
            log.warning("l2Book alınamadı (%s): %s", coin, e)
            continue
        for direction, near in (("down", near_dn), ("up", near_up)):
            if near < min_usd:
                continue
            s = score_direction(rows, bids, asks, mark, direction, min_usd, max_dist)
            if not s:
                continue
            s.update({"coin": coin, "symbol": rows[0]["symbol"], "mark": mark,
                      "dev_close": devs.get(coin), "ts": ts, "weekend_ts": anchor,
                      "hot": s["score"] >= min_score})
            rows_out.append(s)
    rows_out.sort(key=lambda s: -s["score"])
    out["candidates"] = sum(1 for s in rows_out if s["hot"])

    async with db() as conn:
        await conn.executemany(
            """INSERT INTO liq_attack_candidates(coin,direction,ts,weekend_ts,mark,
                 dist_pct,liq_usd,cost_usd,score,book_thin,n_pos,target_px,dev_close,hot)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(s["coin"], s["direction"], ts, anchor, s["mark"], s["dist_pct"],
              s["liq_usd"], s["cost_usd"], s["score"], int(s["book_thin"]),
              s["n_pos"], s["target_px"], s["dev_close"], int(s["hot"]))
             for s in rows_out])

    if notifier is not None:
        chat = getattr(cfg, "liq_attack_chat_id", "") or ""
        for s in rows_out:
            if not s["hot"]:
                continue
            key = f"attack:{s['coin']}:{s['direction']}:{anchor}"
            if await alert_recent("liqattack", key, cool):
                continue
            from ..telegram import format as fmt
            text = fmt.liq_attack_alert(s, wk)
            # MARKER YALNIZ GİDERSE (kapalı seans dersi): başarısız gönderim
            # cooldown'u yakmasın, bir sonraki tur yeniden denesin.
            if await notifier.send("liqattack", text, priority="high", key=key,
                                   chat_id=chat):
                await alert_log("liqattack", key, text)
                out["alerted"] += 1
            else:
                await alert_log("fail:liqattack", key, text[:200])
                out["failed"] += 1
    out["top"] = [{"symbol": s["symbol"], "direction": s["direction"],
                   "score": round(s["score"], 1), "liq_usd": s["liq_usd"]}
                  for s in rows_out[:3]]
    return await _stats(out)


async def _stats(out: dict) -> dict:
    try:
        await kv_set("liqattack_stats", {**out, "ts": now()})
    except Exception:
        log.debug("liqattack_stats yazılamadı", exc_info=True)
    return out


# ─────────────────────────────────────────────── geçmiş saldırılar (karne)

async def detect(cfg) -> int:
    """Son DETECT_LOOKBACK_D günün hafta sonlarında gerçekleşen saldırılar.

    1 dk'lık mark serisinde sıçra-ve-dön deseni + o aralıkta kapanmış ve liq
    fiyatı sıçramanın içinde kalan pozisyonlar (addr_positions, her boyut).
    Liq olmadan geri dönen fitil saldırı sayılmaz — dürüstlük.
    """
    spike_pct = float(getattr(cfg, "liq_attack_spike_pct", 1.5))
    revert_min = int(getattr(cfg, "liq_attack_revert_min", 90))
    h = int(getattr(cfg, "offhours_close_hour", 0))
    since = now() - DETECT_LOOKBACK_D * 86400
    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol FROM tickers")
        tick = {r["coin"]: r["symbol"] for r in await cur.fetchall()}
    n_new = 0
    for coin, symbol in tick.items():
        async with db() as conn:
            cur = await conn.execute(
                "SELECT ts, mark_px FROM asset_metrics WHERE coin=? AND ts>=?"
                " AND mark_px > 0 ORDER BY ts", (coin, since))
            series = [(int(r["ts"]), float(r["mark_px"])) for r in await cur.fetchall()]
        # yalnız hafta sonu örnekleri
        series = [(t, p) for t, p in series if hourstats.weekend_window(t, h)]
        if len(series) < 60:
            continue
        for ev in find_spikes(series, spike_pct, revert_min):
            lo, hi = sorted((ev["ref_px"], ev["extreme_px"]))
            async with db() as conn:
                cur = await conn.execute(
                    """SELECT address, notional, liq_px FROM addr_positions
                       WHERE coin=? AND closed_ts BETWEEN ? AND ?
                         AND liq_px BETWEEN ? AND ?""",
                    (coin, ev["ts_start"] - 600, ev["ts_end"] + 1800, lo, hi))
                liq = [dict(r) for r in await cur.fetchall()]
            if not liq:
                continue                       # fitil var, liq yok → saldırı değil
            wk = hourstats.weekend_window(ev["ts_start"], h)
            anchor = wk[0] if wk else 0
            async with db() as conn:
                cur = await conn.execute(
                    """SELECT MAX(score) s FROM liq_attack_candidates
                       WHERE coin=? AND direction=? AND weekend_ts=? AND ts < ?""",
                    (coin, ev["direction"], anchor, ev["ts_start"]))
                r = await cur.fetchone()
                pre = float(r["s"]) if r and r["s"] is not None else None
                cur = await conn.execute(
                    """INSERT OR IGNORE INTO liq_attacks(coin,ts_start,ts_peak,ts_end,
                         direction,ref_px,extreme_px,move_pct,liq_usd,n_liq,
                         predicted_score,weekend_ts,found_ts)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (coin, ev["ts_start"], ev["ts_peak"], ev["ts_end"],
                     ev["direction"], ev["ref_px"], ev["extreme_px"], ev["move_pct"],
                     sum(x["notional"] for x in liq), len(liq), pre, anchor, now()))
                n_new += cur.rowcount or 0
    if n_new:
        log.info("liq attack: %d yeni geçmiş saldırı tespit edildi", n_new)
    return n_new


async def record(cfg) -> dict:
    """Karne: tespit edilen saldırıların kaçını önceden işaretlemiştik, kaç
    aday boşa çıktı. Dürüst iki sayı: isabet ve yanlış alarm."""
    min_score = float(getattr(cfg, "liq_attack_min_score", 2.0))
    async with db() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) n, SUM(predicted_score >= ?) hit FROM liq_attacks",
            (min_score,))
        a = dict(await cur.fetchone())
        cur = await conn.execute(
            """SELECT COUNT(DISTINCT coin || ':' || direction || ':' || weekend_ts) n
               FROM liq_attack_candidates WHERE hot=1 AND weekend_ts < ?""",
            (now() - 2 * 86400,))
        flagged = (await cur.fetchone())["n"] or 0
        cur = await conn.execute(
            """SELECT COUNT(DISTINCT c.coin || ':' || c.direction || ':' || c.weekend_ts) n
               FROM liq_attack_candidates c JOIN liq_attacks a
                 ON a.coin = c.coin AND a.direction = c.direction
                AND a.weekend_ts = c.weekend_ts AND a.ts_start > c.ts
               WHERE c.hot=1 AND c.weekend_ts < ?""", (now() - 2 * 86400,))
        flagged_hit = (await cur.fetchone())["n"] or 0
    n, hit = a["n"] or 0, a["hit"] or 0
    return {"attacks": n, "predicted": hit,
            "hit_rate": (hit / n * 100) if n else None,
            "flagged": flagged, "flagged_hit": flagged_hit,
            "false_alarm": flagged - flagged_hit,
            "precision": (flagged_hit / flagged * 100) if flagged else None}


# ─────────────────────────────────────────────── sayfa

async def page(cfg) -> dict:
    wk = hourstats.weekend_window(None, int(getattr(cfg, "offhours_close_hour", 0)))
    async with db() as conn:
        cur = await conn.execute(
            "SELECT MAX(ts) t FROM liq_attack_candidates")
        last = (await cur.fetchone())["t"]
        cands = []
        if last:
            cur = await conn.execute(
                """SELECT c.*, t.symbol FROM liq_attack_candidates c
                   JOIN tickers t ON t.coin = c.coin
                   WHERE c.ts = ? ORDER BY c.score DESC""", (last,))
            cands = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute(
            """SELECT a.*, t.symbol FROM liq_attacks a JOIN tickers t ON t.coin = a.coin
               ORDER BY a.ts_start DESC LIMIT 40""")
        attacks = [dict(r) for r in await cur.fetchall()]
    from ..db import kv_get
    h = int(getattr(cfg, "offhours_close_hour", 0))
    # Ölçek: liq ve defter barları aynı $ ekseninde — karşılaştırma bu.
    mx = max([max(c["liq_usd"] or 0, c["cost_usd"] or 0) for c in cands] or [1.0])
    by = await _equity_positions() if cands else {}
    for c in cands:
        c["liq_w"] = max(1.5, (c["liq_usd"] or 0) / mx * 100)
        c["cost_w"] = max(1.5, (c["cost_usd"] or 0) / mx * 100)
        # Hedef pozisyonlar tabloda saklanmıyor (kimlik zaten positions_current'ta);
        # tur anındaki mark ile yeniden bulunur — ucuz ve tek kaynak.
        hit = liq_within(by.get(c["coin"], []), c["mark"], c["direction"], c["dist_pct"])
        hit.sort(key=lambda p: -p["notional"])
        c["targets"] = [{"address": p["address"], "notional": p["notional"],
                         "liq_px": p["liq_px"]} for p in hit[:TOP_TARGETS]]
    return {"weekend": wk, "last_ts": last, "cands": cands, "attacks": attacks,
            "rec": await record(cfg),
            "stats": await kv_get("liqattack_stats") or {},
            "next_weekend": next_weekend(None, h)}


def next_weekend(ref_ts: int | None, tsi_hour: int = 0) -> int:
    """Sonraki pencere başlangıcı (Cumartesi tsi_hour:00 TSİ). Pencere içindeysek
    şu anki pencerenin başı — sayfa 'açık' der, geri sayım bitişe bakar."""
    from datetime import datetime, time as dtime, timedelta
    ts = int(ref_ts) if ref_ts else now()
    wk = hourstats.weekend_window(ts, tsi_hour)
    if wk:
        return wk[0]
    d = datetime.fromtimestamp(ts, hourstats.TR)
    for ahead in range(0, 8):
        day = (d + timedelta(days=ahead)).date()
        if day.weekday() != 5:
            continue
        start = datetime.combine(day, dtime(tsi_hour % 24), hourstats.TR)
        if start.timestamp() > ts:
            return int(start.timestamp())
    return ts


async def prune(days: int = RETENTION_D) -> int:
    async with db() as conn:
        cur = await conn.execute(
            "DELETE FROM liq_attack_candidates WHERE ts < ?", (now() - days * 86400,))
        n = cur.rowcount or 0
        cur = await conn.execute(
            "DELETE FROM liq_attacks WHERE ts_start < ?", (now() - days * 86400,))
        return n + (cur.rowcount or 0)


async def loop(cfg, client, notifier=None) -> None:
    from ..health import beat
    await asyncio.sleep(240)
    tick = 0
    while True:
        try:
            await beat("liqattack")
            await scan(cfg, client, notifier)
            if tick % 12 == 0:                 # saatte bir: ucuz SQL
                await detect(cfg)
                await prune()
            tick += 1
            await beat("liqattack")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("liq attack turu hatası")
        await asyncio.sleep(max(60, int(getattr(cfg, "liq_attack_scan_sec", 300))))
