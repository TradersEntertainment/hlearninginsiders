"""Bağlantılı cüzdan kümeleri.

Ledger'daki transferlerden (internalTransfer / subAccountTransfer / spotTransfer)
cüzdanlar arası bağlar çıkarılır. Aynı kümedeki cüzdanlar aynı coin'de aynı yönde
pozisyon taşıyorsa bunlar muhtemelen bölünmüş tek bir balinadır.
"""
import logging

from ..db import db, now

log = logging.getLogger("radar.clusters")


def _norm(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


async def store_links(address: str, links: list[tuple[str, str, int]]) -> None:
    """links: [(counterparty, kind, ts)]"""
    if not links:
        return
    async with db() as conn:
        for other, kind, ts in links:
            other = (other or "").lower()
            if not other.startswith("0x") or other == address:
                continue
            a, b = _norm(address, other)
            await conn.execute(
                """INSERT INTO wallet_links(a,b,kind,last_ts) VALUES(?,?,?,?)
                   ON CONFLICT(a,b) DO UPDATE SET
                     last_ts=MAX(wallet_links.last_ts, excluded.last_ts),
                     kind=excluded.kind""",
                (a, b, kind, ts or now()))


async def linked_addresses(address: str) -> list[dict]:
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM wallet_links WHERE a=? OR b=? ORDER BY last_ts DESC LIMIT 30",
            (address, address))
        out = []
        for r in await cur.fetchall():
            other = r["b"] if r["a"] == address else r["a"]
            out.append({"address": other, "kind": r["kind"], "ts": r["last_ts"]})
        return out


async def find_clusters(rows: list[dict]) -> list[dict]:
    """Taranan pozisyon satırları içinde bağlantılı grupları bul.

    Dönen her küme: {addrs: [...], side: 'long'|'short'|'mixed', total: float}
    Yalnızca 2+ üyeli kümeler döner.
    """
    addrs = [p["address"] for p in rows]
    if len(addrs) < 2:
        return []
    q = ",".join("?" * len(addrs))
    async with db() as conn:
        cur = await conn.execute(
            f"SELECT a,b FROM wallet_links WHERE a IN ({q}) AND b IN ({q})",
            addrs + addrs)
        edges = [(r["a"], r["b"]) for r in await cur.fetchall()]
    if not edges:
        return []

    # union-find
    parent = {a: a for a in addrs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        if a in parent and b in parent:
            parent[find(a)] = find(b)

    groups: dict[str, list[dict]] = {}
    by_addr = {p["address"]: p for p in rows}
    for a in addrs:
        groups.setdefault(find(a), []).append(by_addr[a])

    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        sides = {m["side"] for m in members}
        out.append({
            "addrs": [m["address"] for m in members],
            "side": members[0]["side"] if len(sides) == 1 else "mixed",
            "total": sum(m["notional"] for m in members),
        })
    out.sort(key=lambda c: c["total"], reverse=True)
    return out
