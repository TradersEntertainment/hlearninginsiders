"""Earnings raporu üretimi — tarama + skorlama + kümeler + korele hisseler."""
import logging

from ..config import Config
from ..db import db
from ..earnings.calendar import event_ts_estimate
from ..hl.client import HLClient
from ..hl.universe import find_ticker
from ..telegram import format as fmt
from . import clusters, metrics, peers, scanner, scorer

log = logging.getLogger("radar.report")


def coin_dex(coin: str) -> str:
    return coin.split(":")[0] if ":" in coin else ""


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


async def peers_summary(cfg: Config, symbol: str, limit: int = 3) -> list[dict]:
    """Korele hisselerin hızlı özeti (yeni tarama yapmaz, mevcut veriden)."""
    out = []
    for psym in peers.peers_of(symbol, cfg.peers_override):
        t = await find_ticker(psym)
        if not t:
            continue
        summ = await metrics.summary(t["coin"])
        async with db() as conn:
            cur = await conn.execute(
                "SELECT * FROM positions_current WHERE coin=? ORDER BY notional DESC LIMIT 1",
                (t["coin"],))
            top = await cur.fetchone()
        out.append({"symbol": t["symbol"], "summ": summ,
                    "top": dict(top) if top else None})
        if len(out) >= limit:
            break
    return out


async def run_stage(cfg: Config, client: HLClient, bot, event: dict, stage: str) -> None:
    coin = event["coin"]
    ref = event_ts_estimate(event)
    summ, rows = await build_scan(cfg, client, coin, coin_dex(coin), ref_ts=ref)
    cluster_list = await clusters.find_clusters(rows)
    peer_list = await peers_summary(cfg, event["symbol"])
    text = fmt.earnings_report(event, stage, summ, rows, cfg,
                               cluster_list=cluster_list, peer_list=peer_list)
    if bot:
        await bot.send(text)
    await scanner.snapshot(event["id"], "T-1h" if stage == "t1" else "pre", rows)
    flag = "alerted_t1" if stage == "t1" else "alerted_pre"
    async with db() as conn:
        await conn.execute(f"UPDATE earnings_events SET {flag}=1 WHERE id=?", (event["id"],))
    log.info("%s %s raporu gönderildi (%d pozisyon, %d küme)",
             event["symbol"], stage, len(rows), len(cluster_list))
