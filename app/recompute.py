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
RECOMPUTE_VERSION = 3


async def recompute_records(cfg: Config, force: bool = False) -> dict:
    if not force:
        done = await kv_get("recompute_version")
        if done == RECOMPUTE_VERSION:
            return {"skipped": True}

    async with db() as conn:
        # Elle /watch eklenen adresleri koru: bunların hits'i 0 ama watchlist=1'dir.
        # Sicil sıfırlaması sonrası yalnız hits>=2 geri terfi ettiğinden bu adresler
        # sessizce watchlist'ten düşüyordu — kullanıcı hâlâ izlendiğini sanırdı.
        cur = await conn.execute(
            "SELECT address FROM addresses WHERE watchlist=1 AND COALESCE(hits,0) < 2")
        manual_watch = [r["address"] for r in await cur.fetchall()]

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
            # MM/vault etiketli adresleri sicile hiç dahil etme. ADRES BAŞINA TEK
            # faz say (T-1h varsa o, yoksa pre) — evaluator'ın kuralıyla aynı.
            # Eskiden pre+T-1h iki satır ayrı ayrı sayılıyordu: tek bilançoyu doğru
            # bilen adres hits=2 alıp tek olaydan watchlist'e giriyordu.
            cur = await conn.execute(
                """SELECT s.address, s.side, s.notional, s.phase FROM position_snapshots s
                   LEFT JOIN addresses a ON a.address = s.address
                   WHERE s.event_id=? AND s.phase IN ('T-1h','pre')
                     AND s.notional >= ? AND COALESCE(a.entity,'')=''""",
                (ev["id"], cfg.eval_min_notional))
            per_addr: dict[str, dict] = {}
            for s in await cur.fetchall():
                cur_row = per_addr.get(s["address"])
                # T-1h önceliklidir; aynı fazda ilk gelen kalır
                if cur_row is None or (s["phase"] == "T-1h" and cur_row["phase"] != "T-1h"):
                    per_addr[s["address"]] = dict(s)
            for s in per_addr.values():
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

        # 3) 2+ doğruyu watchlist'e al + elle eklenenleri geri koy
        await conn.execute("UPDATE addresses SET watchlist=1 WHERE hits >= 2")
        for addr in manual_watch:
            await conn.execute(
                "INSERT INTO addresses(address, first_seen, watchlist) VALUES(?,?,1)"
                " ON CONFLICT(address) DO UPDATE SET watchlist=1", (addr, now()))
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
