"""Earnings sonrası değerlendirme (T+24h).

Fiyat hareketi yönünü ölçer, T-1h snapshot'ındaki her adresi doğru/yanlış
olarak siciline işler, 2+ doğru bilenleri watchlist'e alır, kapananları
raporlar. Botun "öğrenen hafızası" burasıdır.
"""
import logging

from ..config import Config
from ..db import db, now
from ..hl.client import HLClient
from ..radar import metrics, scanner
from ..radar.report import coin_dex
from ..telegram import format as fmt
from .calendar import event_ts_estimate

log = logging.getLogger("earnings.evaluator")


async def evaluate_due(cfg: Config, client: HLClient, bot) -> None:
    cutoff = now()
    async with db() as conn:
        cur = await conn.execute(
            """SELECT * FROM earnings_events
               WHERE evaluated=0 AND (alerted_t1=1 OR alerted_pre=1)
               AND date_et >= date('now','-6 day')""")
        events = [dict(r) for r in await cur.fetchall()]
    for ev in events:
        est = event_ts_estimate(ev)
        if cutoff < est + 24 * 3600:
            continue
        try:
            await _evaluate(cfg, client, bot, ev, est)
        except Exception as e:
            log.warning("değerlendirme hatası %s: %s", ev["symbol"], e)


async def _evaluate(cfg: Config, client: HLClient, bot, ev: dict, est: int) -> None:
    coin = ev["coin"]
    before = await metrics.metric_at(coin, est - 300)
    after = await metrics.latest_metric(coin)

    async with db() as conn:
        cur = await conn.execute(
            """SELECT * FROM position_snapshots WHERE event_id=? AND phase='T-1h'
               ORDER BY notional DESC""", (ev["id"],))
        snaps = [dict(r) for r in await cur.fetchall()]
        if not snaps:
            cur = await conn.execute(
                """SELECT * FROM position_snapshots WHERE event_id=? AND phase='pre'
                   ORDER BY notional DESC""", (ev["id"],))
            snaps = [dict(r) for r in await cur.fetchall()]

    move_pct = None
    if before and after and before["mark_px"]:
        move_pct = (after["mark_px"] - before["mark_px"]) / before["mark_px"] * 100

    results = []
    promoted = []
    if move_pct is not None and abs(move_pct) >= cfg.eval_move_threshold and snaps:
        direction = "up" if move_pct > 0 else "down"
        async with db() as conn:
            for s in snaps:
                hit = (s["side"] == "long" and direction == "up") or \
                      (s["side"] == "short" and direction == "down")
                col = "hits" if hit else "misses"
                await conn.execute(
                    f"UPDATE addresses SET {col}={col}+1 WHERE address=?", (s["address"],))
                cur = await conn.execute(
                    "SELECT hits, watchlist FROM addresses WHERE address=?", (s["address"],))
                row = await cur.fetchone()
                if row and row["hits"] >= 2 and not row["watchlist"]:
                    await conn.execute(
                        "UPDATE addresses SET watchlist=1 WHERE address=?", (s["address"],))
                    promoted.append(s["address"])
                results.append({**s, "hit": hit})

    # Kim kapattı? — güncel durumu tekrar tara ve T+24h snapshot'ı al
    closed = []
    try:
        rows_now = await scanner.scan(cfg, client, coin, coin_dex(coin))
        await scanner.snapshot(ev["id"], "T+24h", rows_now)
        still = {p["address"] for p in rows_now}
        closed = [s["address"] for s in snaps if s["address"] not in still]
    except Exception as e:
        log.warning("T+24h taraması başarısız %s: %s", coin, e)

    async with db() as conn:
        await conn.execute("UPDATE earnings_events SET evaluated=1 WHERE id=?", (ev["id"],))

    if bot and snaps:
        text = fmt.eval_report(ev, move_pct, results, closed, promoted, cfg)
        await bot.send(text)
    log.info("%s değerlendirildi: hareket %s, %d adres işlendi, %d watchlist'e alındı",
             ev["symbol"], f"%{move_pct:.1f}" if move_pct is not None else "?",
             len(results), len(promoted))
