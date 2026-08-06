"""Likidasyon radarı — TÜM Hyperliquid (ana dex + HIP-3 dex'leri).

Dev pozisyonlar (varsayılan $70M+) likidasyon fiyatına yaklaşırken kademeli
bildirim: mesafe %1 → ilk, %0.5 → ikinci, %0.1 → son uyarı. Pozisyon
uzaklaşırsa (>%1.5) kademeler sıfırlanır; izlenen pozisyon yok olursa
(likidasyon ya da kapatma) kapanış notu gider.
"""
import asyncio
import logging

from ..config import Config
from ..db import db, now
from ..hl.client import HLClient
from ..hl.universe import norm_coin
from ..telegram import format as fmt
from .scanner import _leaderboard_addrs

log = logging.getLogger("radar.liqwatch")

# (kademe, mesafe eşiği %) — sıralı
STAGES = [(3, 0.1), (2, 0.5), (1, 1.0)]
RESET_DIST = 1.5  # bu mesafenin üstüne çıkarsa kademeler sıfırlanır


def needed_stage(dist_pct: float) -> int:
    for stage, thr in STAGES:
        if dist_pct <= thr:
            return stage
    return 0


def _iter_states(resp):
    """clearinghouseState cevabı: tek state ya da dex->state sözlüğü olabilir."""
    if not isinstance(resp, dict):
        return
    if "assetPositions" in resp:
        yield resp
        return
    for v in resp.values():
        if isinstance(v, dict) and "assetPositions" in v:
            yield v


async def _mark_map(cfg: Config, client: HLClient) -> dict[str, float]:
    marks: dict[str, float] = {}
    for dex in ["", *cfg.equity_dexes]:
        try:
            data = await client.meta_and_ctxs(dex)
            meta, ctxs = data[0], data[1]
        except Exception as e:
            log.debug("ctxs(%s): %s", dex, e)
            continue
        for asset, ctx in zip(meta.get("universe") or [], ctxs):
            name = asset.get("name") or ""
            if not name:
                continue
            try:
                m = float(ctx.get("markPx") or 0)
            except (TypeError, ValueError):
                continue
            if m:
                marks[norm_coin(name, dex)] = m
    return marks


async def _candidates(cfg: Config, client: HLClient) -> list[str]:
    lb = await _leaderboard_addrs(client, cfg.liq_watch_top_accounts)
    async with db() as conn:
        cur = await conn.execute("SELECT address FROM addresses WHERE watchlist=1")
        watch = [r["address"] for r in await cur.fetchall()]
        cur = await conn.execute("SELECT DISTINCT address FROM liq_watch")
        tracked = [r["address"] for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT DISTINCT address FROM positions_current WHERE notional>=?",
            (cfg.liq_watch_min_notional,))
        big_eq = [r["address"] for r in await cur.fetchall()]
    ordered, seen = [], set()
    for group in (tracked, watch, big_eq, lb):
        for a in group:
            a = (a or "").lower()
            if a and a not in seen:
                seen.add(a)
                ordered.append(a)
    return ordered[: cfg.liq_watch_top_accounts + 50]


async def run_cycle(cfg: Config, client: HLClient, bot) -> None:
    marks = await _mark_map(cfg, client)
    if not marks:
        return
    addrs = await _candidates(cfg, client)
    if not addrs:
        return

    found: dict[tuple[str, str], dict] = {}

    async def one(addr: str):
        try:
            resp = await client.clearinghouse(addr, "ALL_DEXES")
        except Exception:
            try:
                resp = await client.clearinghouse(addr, "")
            except Exception:
                return
        for state in _iter_states(resp):
            for ap in state.get("assetPositions") or []:
                pos = ap.get("position") or {}
                try:
                    szi = float(pos.get("szi") or 0)
                    ntl = float(pos.get("positionValue") or 0)
                except (TypeError, ValueError):
                    continue
                liq = pos.get("liquidationPx")
                if szi == 0 or not liq or ntl < cfg.liq_watch_min_notional:
                    continue
                coin = pos.get("coin") or ""
                found[(addr, coin)] = {
                    "side": "long" if szi > 0 else "short",
                    "notional": ntl, "liq_px": float(liq),
                    "entry_px": float(pos.get("entryPx") or 0),
                }

    await asyncio.gather(*(one(a) for a in addrs))

    ts = now()
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM liq_watch")
        tracked = {(r["address"], r["coin"]): dict(r) for r in await cur.fetchall()}

    # 1) Kaybolanlar: kademe başlamışsa kapanış notu
    for key, row in tracked.items():
        if key in found:
            continue
        if row["stage"] >= 1 and bot:
            try:
                await bot.send(fmt.liq_closed(key[1], key[0], row))
            except Exception as e:
                log.warning("liq kapanış notu gönderilemedi: %s", e)
        async with db() as conn:
            await conn.execute("DELETE FROM liq_watch WHERE address=? AND coin=?", key)

    # 2) Görülenler: mesafe + kademe işle
    for (addr, coin), p in found.items():
        mark = marks.get(coin)
        if not mark:
            continue
        dist = abs(mark - p["liq_px"]) / mark * 100
        stage_sent = tracked.get((addr, coin), {}).get("stage") or 0
        need = needed_stage(dist)
        if need > stage_sent:
            if bot:
                try:
                    await bot.send(fmt.liq_alert(coin, addr, p, mark, dist, need))
                except Exception as e:
                    log.warning("liq alerti gönderilemedi: %s", e)
            log.info("liq kademe %d: %s %s %%%.2f", need, coin, addr, dist)
            stage_sent = need
        elif stage_sent and dist > RESET_DIST:
            log.info("liq kademe sıfırlandı: %s %s %%%.2f", coin, addr, dist)
            stage_sent = 0
        async with db() as conn:
            await conn.execute(
                """INSERT INTO liq_watch(address,coin,side,notional,liq_px,stage,last_dist,updated_ts)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(address,coin) DO UPDATE SET
                     side=excluded.side, notional=excluded.notional, liq_px=excluded.liq_px,
                     stage=?, last_dist=excluded.last_dist, updated_ts=excluded.updated_ts""",
                (addr, coin, p["side"], p["notional"], p["liq_px"], stage_sent, dist, ts,
                 stage_sent))


async def loop(cfg: Config, client: HLClient, bot=None) -> None:
    await asyncio.sleep(90)
    log.info("likidasyon radarı başladı (min $%.0fM, periyot %ds)",
             cfg.liq_watch_min_notional / 1e6, cfg.liq_watch_poll_sec)
    while True:
        try:
            await run_cycle(cfg, client, bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("liq radarı döngü hatası")
        await asyncio.sleep(cfg.liq_watch_poll_sec)
