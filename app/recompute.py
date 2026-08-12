"""Sicil yeniden hesaplama.

Eşik değişince (ör. artık sadece $300K+ pozlar sayılıyor) eski sicili sıfırdan
kurar: her değerlendirilmiş earnings için, yalnız eşik üstü snapshot'ları
sayar, kazananların hisse setini (address_wins) yeniden doldurur, 2+ doğruyu
watchlist'e alır. Bir kez çalışır (kv bayrağı ile).
"""
import logging

from .config import Config
from .db import db, kv_get, kv_set, now

log = logging.getLogger("recompute")

# Bu sürümü artırırsan migration bir kez daha çalışır
RECOMPUTE_VERSION = 2


async def recompute_records(cfg: Config, force: bool = False) -> dict:
    if not force:
        done = await kv_get("recompute_version")
        if done == RECOMPUTE_VERSION:
            return {"skipped": True}

    async with db() as conn:
        # 1) tüm sicili sıfırla
        await conn.execute("UPDATE addresses SET hits=0, misses=0, watchlist=0")
        await conn.execute("DELETE FROM address_wins")

        # 2) değerlendirilmiş, yönü belli event'leri sırayla oyna
        cur = await conn.execute(
            """SELECT id, coin, move_pct FROM earnings_events
               WHERE evaluated=1 AND move_pct IS NOT NULL
               ORDER BY date_et""")
        events = [dict(r) for r in await cur.fetchall()]

        n_hits = n_events = 0
        for ev in events:
            move = ev["move_pct"]
            if move is None or abs(move) < cfg.eval_move_threshold:
                continue
            n_events += 1
            direction = "up" if move > 0 else "down"
            cur = await conn.execute(
                """SELECT address, side, notional FROM position_snapshots
                   WHERE event_id=? AND phase IN ('T-1h','pre')
                     AND notional >= ?""",
                (ev["id"], cfg.eval_min_notional))
            for s in await cur.fetchall():
                hit = (s["side"] == "long" and direction == "up") or \
                      (s["side"] == "short" and direction == "down")
                col = "hits" if hit else "misses"
                # adres kaydı yoksa oluştur
                await conn.execute(
                    "INSERT INTO addresses(address, first_seen) VALUES(?,?)"
                    " ON CONFLICT(address) DO NOTHING", (s["address"], now()))
                await conn.execute(
                    f"UPDATE addresses SET {col}={col}+1 WHERE address=?", (s["address"],))
                if hit:
                    n_hits += 1
                    await conn.execute(
                        "INSERT OR REPLACE INTO address_wins(address,coin,event_id,notional,ts)"
                        " VALUES(?,?,?,?,?)",
                        (s["address"], ev["coin"], ev["id"], s["notional"], now()))

        # 3) 2+ doğruyu watchlist'e al
        await conn.execute("UPDATE addresses SET watchlist=1 WHERE hits >= 2")
        cur = await conn.execute("SELECT COUNT(*) c FROM addresses WHERE watchlist=1")
        n_watch = (await cur.fetchone())["c"]

    await kv_set("recompute_version", RECOMPUTE_VERSION)
    stats = {"events": n_events, "hits": n_hits, "watchlist": n_watch}
    log.info("sicil yeniden hesaplandı (min $%.0fK): %s",
             cfg.eval_min_notional / 1000, stats)
    return stats


async def winner_coins(address: str) -> set[str]:
    """Bu adresin geçmişte doğru bildiği hisseler."""
    async with db() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT coin FROM address_wins WHERE address=?", (address,))
        return {r["coin"] for r in await cur.fetchall()}


async def winner_coins_map(addresses: list[str]) -> dict[str, set[str]]:
    if not addresses:
        return {}
    q = ",".join("?" * len(addresses))
    out: dict[str, set[str]] = {}
    async with db() as conn:
        cur = await conn.execute(
            f"SELECT address, coin FROM address_wins WHERE address IN ({q})", addresses)
        for r in await cur.fetchall():
            out.setdefault(r["address"], set()).add(r["coin"])
    return out
