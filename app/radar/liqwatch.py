"""Likidasyon radarı — TÜM Hyperliquid (ana dex + HIP-3 dex'leri).

Dev pozisyonlar (varsayılan $70M+) likidasyon fiyatına yaklaşırken kademeli
bildirim: mesafe %1 → ilk, %0.5 → ikinci, %0.1 → son uyarı. Pozisyon
uzaklaşırsa (>%1.5) kademeler sıfırlanır; izlenen pozisyon yok olursa
(likidasyon ya da kapatma) kapanış notu gider.
"""
import asyncio
import logging

from .. import assets
from ..config import Config
from ..db import alert_log, alert_recent, db, now
from ..hl.client import HLClient
from ..hl.universe import norm_coin
from ..telegram import format as fmt
from .scanner import _leaderboard_addrs

log = logging.getLogger("radar.liqwatch")

# (kademe, mesafe eşiği %) — sıralı
STAGES = [(3, 0.1), (2, 0.5), (1, 1.0)]
RESET_DIST = 1.5  # bu mesafenin üstüne çıkarsa kademeler sıfırlanır
CLUSTER_COOLDOWN = 6 * 3600  # aynı coin+yön duvarı için tekrar bildirim aralığı


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


async def run_cycle(cfg: Config, client: HLClient, notifier) -> None:
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
        if row["stage"] >= 1 and notifier:
            try:
                await notifier.send("liq", fmt.liq_closed(key[1], key[0], row),
                                    priority="normal", key=f"{key[1]}:{key[0]}:closed")
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
            if notifier:
                try:
                    await notifier.send(
                        "liq", fmt.liq_alert(coin, addr, p, mark, dist, need),
                        priority="critical" if need >= 3 else "high",
                        key=f"{coin}:{addr}:{need}")
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


async def find_clusters(cfg: Config) -> list[dict]:
    """Likidasyon duvarları: tek tek küçük ama TOPLAMDA büyük, fiyata yakın
    liq yığınları — prop firmaların heatmap'te okuduğu cascade/stop-avı mıknatısı.
    Sadece DB verisi kullanır (equity taramaları), API isteği atmaz."""
    async with db() as conn:
        cur = await conn.execute(
            """SELECT a.coin, a.mark_px FROM asset_metrics a
               JOIN (SELECT coin, MAX(ts) mts FROM asset_metrics GROUP BY coin) b
                 ON a.coin=b.coin AND a.ts=b.mts""")
        marks = {r["coin"]: r["mark_px"] for r in await cur.fetchall()}
        cur = await conn.execute(
            """SELECT p.coin, t.symbol, p.side, p.notional, p.liq_px, p.address
               FROM positions_current p JOIN tickers t ON t.coin=p.coin
               WHERE p.liq_px IS NOT NULL AND p.notional > 0""")
        rows = [dict(r) for r in await cur.fetchall()]

    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        mark = marks.get(r["coin"])
        if not mark:
            continue
        r["dist"] = (r["liq_px"] - mark) / mark * 100
        if abs(r["dist"]) > cfg.liq_cluster_window_pct:
            continue
        r["mark"] = mark
        groups.setdefault((r["coin"], r["side"]), []).append(r)

    out = []
    for (coin, side), members in groups.items():
        total = sum(m["notional"] for m in members)
        # duvar: 2+ pozisyon birikmiş YA DA tek ama çok büyük (eşiğin 3 katı)
        if total < cfg.liq_cluster_min_usd or \
                (len(members) < 2 and total < cfg.liq_cluster_min_usd * 3):
            continue
        other = sum(m["notional"] for (c2, s2), ms in groups.items()
                    if c2 == coin and s2 != side for m in ms)
        members.sort(key=lambda m: -m["notional"])
        out.append({"coin": coin, "symbol": members[0]["symbol"], "side": side,
                    "total": total, "count": len(members), "members": members,
                    "other_total": other, "mark": members[0]["mark"],
                    "avg_dist": sum(abs(m["dist"]) for m in members) / len(members)})
    out.sort(key=lambda c: -c["total"])
    return out


async def check_clusters(cfg: Config, notifier) -> None:
    clusters = await find_clusters(cfg)
    if not clusters:
        return
    from .bookwall import big_coin_set  # döngüsel importu kır
    bigs = await big_coin_set(cfg)
    for c in clusters:
        # JPY/GOLD/XYZ100 gibi likit varlıklarda küçük kümeler gürültü —
        # onlarda alarm için büyük-sınıf tabanı gerekir (ana sayfa etkilenmez)
        big = assets.kind(c["symbol"]) == "non_equity" or c["coin"] in bigs
        alert_min = cfg.liq_cluster_big_min_usd if big else cfg.liq_cluster_min_usd
        if c["total"] < alert_min:
            continue
        key = f"{c['coin']}:{c['side']}"
        if await alert_recent("liq_cluster", key, CLUSTER_COOLDOWN):
            continue
        text = fmt.liq_cluster_alert(c)
        await alert_log("liq_cluster", key, text)  # cooldown, gönderim sonucundan bağımsız
        if notifier:
            try:
                await notifier.send("liqmap", text, key=key)
            except Exception as e:
                log.warning("liq duvarı alerti gönderilemedi: %s", e)
        log.info("liq duvarı: %s %s %d poz, toplam $%.0f (ort mesafe %%%.1f)",
                 c["symbol"], c["side"], c["count"], c["total"], c["avg_dist"])


async def loop(cfg: Config, client: HLClient, notifier=None) -> None:
    await asyncio.sleep(90)
    log.info("likidasyon radarı başladı (min $%.0fM, periyot %ds)",
             cfg.liq_watch_min_notional / 1e6, cfg.liq_watch_poll_sec)
    while True:
        try:
            await run_cycle(cfg, client, notifier)
            await check_clusters(cfg, notifier)
            from ..health import beat
            await beat("liqwatch")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("liq radarı döngü hatası")
        await asyncio.sleep(cfg.liq_watch_poll_sec)
