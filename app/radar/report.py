"""Earnings raporu üretimi — tarama + skorlama + Telegram + snapshot."""
import logging

from ..config import Config
from ..db import db
from ..earnings.calendar import event_ts_estimate
from ..hl.client import HLClient
from ..telegram import format as fmt
from . import metrics, scanner, scorer

log = logging.getLogger("radar.report")


async def build_scan(cfg: Config, client: HLClient, coin: str, dex: str,
                     ref_ts: int | None = None, quick: bool = False):
    """Tarama + skorlama; (summary, rows) döner."""
    summ = await metrics.summary(coin)
    if summ.get("mark") is None:
        try:
            await metrics.poll_metrics(cfg, client)
            summ = await metrics.summary(coin)
        except Exception:
            pass
    rows = await scanner.scan(cfg, client, coin, dex,
                              max_candidates=250 if quick else None)
    rows = await scorer.score_rows(
        cfg, client, coin, rows,
        mark=summ.get("mark"), oi_ntl=summ.get("oi_ntl"), funding=summ.get("funding"),
        ref_ts=ref_ts, deep=True)
    return summ, rows


async def run_stage(cfg: Config, client: HLClient, bot, event: dict, stage: str) -> None:
    coin, dex = event["coin"], coin_dex(event["coin"])
    ref = event_ts_estimate(event)
    summ, rows = await build_scan(cfg, client, coin, dex, ref_ts=ref)
    text = fmt.earnings_report(event, stage, summ, rows, cfg)
    if bot:
        await bot.send(text)
    await scanner.snapshot(event["id"], "T-1h" if stage == "t1" else "pre", rows)
    flag = "alerted_t1" if stage == "t1" else "alerted_pre"
    async with db() as conn:
        await conn.execute(f"UPDATE earnings_events SET {flag}=1 WHERE id=?", (event["id"],))
    log.info("%s %s raporu gönderildi (%d pozisyon)", event["symbol"], stage, len(rows))


def coin_dex(coin: str) -> str:
    return coin.split(":")[0] if ":" in coin else ""
