"""İnsider skorlama (0-100).

Ucuz sinyaller (yerel DB: zamanlama, TWAP, boyut, liq) herkese; pahalı
sinyaller (ledger: taze cüzdan, yeni fonlama, cüzdan bağları) sadece en
büyük N pozisyona uygulanır.
"""
import json
import logging
import statistics

from ..config import Config
from ..db import db, now
from ..hl.client import HLClient
from ..hl.universe import symbol_of
from . import clusters

log = logging.getLogger("radar.scorer")

DEEP_TOP_N = 15

# Hesaba para girişi sayılan ledger olayları
FUNDING_TYPES = {"deposit", "accountClassTransfer"}
# Cüzdanlar arası bağ kuran olaylar
LINK_TYPES = {"internalTransfer", "subAccountTransfer", "spotTransfer"}


async def _opened_ts_from_api(client: HLClient, coin: str, address: str) -> int | None:
    """userFillsByTime ile pozisyon serisinin başlangıcını yaklaşık bul."""
    try:
        start_ms = (now() - 14 * 86400) * 1000
        fills = await client.user_fills_by_time(address, start_ms)
    except Exception as e:
        log.debug("userFills %s: %s", address, e)
        return None
    sym = symbol_of(coin)
    evs = [f for f in fills or []
           if f.get("coin") == coin or symbol_of(f.get("coin") or "") == sym]
    evs.sort(key=lambda f: f.get("time") or 0)
    net = 0.0
    opened = None
    for f in evs:
        try:
            sz = float(f.get("sz") or 0)
            delta = sz if f.get("side") == "B" else -sz
        except (TypeError, ValueError):
            continue
        prev = net
        net += delta
        if abs(net) < 1e-9:
            net, opened = 0.0, None      # pozisyon kapandı, seri sıfırlandı
        elif prev == 0.0 or (prev > 0 > net) or (prev < 0 < net):
            opened = int(f.get("time") or 0) // 1000  # yeni seri başladı
    return opened


async def _ledger_scan(client: HLClient, address: str) -> dict | None:
    """Son 90 günün ledger'ı → fonlama zamanları + cüzdan bağları + fonlayıcılar."""
    try:
        start_ms = (now() - 90 * 86400) * 1000
        updates = await client.ledger_updates(address, start_ms)
    except Exception as e:
        log.debug("ledger %s: %s", address, e)
        return None
    fund_times: list[int] = []
    links: list[tuple[str, str, int]] = []
    funders: set[str] = set()
    for u in updates or []:
        d = u.get("delta") or {}
        t = d.get("type")
        ts = int(u.get("time") or 0) // 1000
        if t in FUNDING_TYPES and ts:
            fund_times.append(ts)
        elif t in LINK_TYPES:
            user = (d.get("user") or "").lower()
            dest = (d.get("destination") or "").lower()
            other = dest if user == address else user
            if other and other.startswith("0x") and other != address:
                links.append((other, t, ts))
                if dest == address and ts:      # para BU hesaba gelmiş → fonlayıcı
                    funders.add(other)
                    fund_times.append(ts)
    return {
        "first": min(fund_times) if fund_times else None,
        "last": max(fund_times) if fund_times else None,
        "links": links,
        "funders": funders,
    }


async def _twap_fills(coin: str, address: str, side: str) -> int:
    """Yerel fill'lerden TWAP paterni: düzenli aralık + benzer boyut. Fill sayısı döner."""
    want = "buy" if side == "long" else "sell"
    since = now() - 48 * 3600
    async with db() as conn:
        cur = await conn.execute(
            "SELECT ts, sz FROM fills WHERE coin=? AND address=? AND side=? AND ts>=?"
            " ORDER BY ts", (coin, address, want, since))
        rows = await cur.fetchall()
    if len(rows) < 5:
        return 0
    ts_list = [r["ts"] for r in rows]
    sizes = [r["sz"] for r in rows]
    gaps = [b - a for a, b in zip(ts_list, ts_list[1:])]
    mg, ms = statistics.mean(gaps), statistics.mean(sizes)
    if mg <= 0 or ms <= 0:
        return 0
    cv_gap = statistics.pstdev(gaps) / mg
    cv_size = statistics.pstdev(sizes) / ms
    return len(rows) if (cv_gap < 0.35 and cv_size < 0.35) else 0


def compute(cfg: Config, pos: dict, oi_ntl: float | None, funding: float | None,
            addr_row: dict | None, fresh: bool | None, last_deposit_ts: int | None,
            ref_ts: int, twap_n: int = 0, funded_by_watch: bool = False) -> tuple[int, list[str]]:
    pts = 0
    reasons: list[str] = []

    opened = pos.get("opened_ts")
    if opened:
        age_h = max(0.0, (ref_ts - opened) / 3600)
        if age_h < 6:
            pts += 25
            reasons.append(f"pozisyon {age_h:.0f}h önce açıldı")
        elif age_h < 48:
            pts += 15
            reasons.append(f"pozisyon {age_h:.0f}h önce açıldı")

    if fresh:
        pts += 25
        reasons.append("taze cüzdan (<7g)")
    elif last_deposit_ts:
        dep_age_h = (now() - last_deposit_ts) / 3600
        if dep_age_h <= cfg.recent_deposit_hours:
            pts += 12
            reasons.append(f"hesap {dep_age_h:.0f}h önce fonlanmış")

    if funded_by_watch:
        pts += 20
        reasons.append("⭐ watchlist adresinden fonlanmış")

    if pos.get("n_open_positions") == 1:
        pts += 10
        reasons.append("hesapta tek pozisyon bu")

    if oi_ntl and oi_ntl > 0:
        share = pos["notional"] / oi_ntl * 100
        if share >= 5:
            pts += 15
            reasons.append(f"OI'nin %{share:.1f}'i")

    mark = pos.get("_mark")
    liq = pos.get("liq_px")
    if mark and liq:
        dist = abs(mark - liq) / mark
        if dist <= 0.15:
            pts += 10
            reasons.append(f"likidasyona %{dist*100:.0f} mesafe (yüksek conviction)")

    if funding is not None:
        paying = (pos["side"] == "long" and funding > 0) or \
                 (pos["side"] == "short" and funding < 0)
        if paying:
            pts += 5
            reasons.append("funding ödeyerek tutuyor")

    if twap_n:
        pts += 5
        reasons.append(f"TWAP paterni ({twap_n} fill/48h)")

    hits = (addr_row or {}).get("hits") or 0
    misses = (addr_row or {}).get("misses") or 0
    if hits >= 2:
        pts += 35
        reasons.append(f"sicilli: {hits} earnings doğru bildi")
    elif hits == 1:
        pts += 20
        reasons.append(f"sicil: 1 doğru / {misses} yanlış")

    return min(pts, 100), reasons


async def _watchlist_set() -> set[str]:
    async with db() as conn:
        cur = await conn.execute("SELECT address FROM addresses WHERE watchlist=1")
        return {r["address"] for r in await cur.fetchall()}


async def score_rows(cfg: Config, client: HLClient, coin: str, rows: list[dict],
                     mark: float | None, oi_ntl: float | None, funding: float | None,
                     ref_ts: int | None = None, deep: bool = True) -> list[dict]:
    ref = ref_ts or now()
    addr_rows: dict[str, dict] = {}
    if rows:
        addrs = [p["address"] for p in rows]
        q = ",".join("?" * len(addrs))
        async with db() as conn:
            cur = await conn.execute(
                f"SELECT * FROM addresses WHERE address IN ({q})", addrs)
            for r in await cur.fetchall():
                addr_rows[r["address"]] = dict(r)
    watch = await _watchlist_set()

    fresh_cut = now() - cfg.fresh_wallet_days * 86400
    for i, p in enumerate(rows):
        p["_mark"] = mark
        arow = addr_rows.get(p["address"]) or {}
        fresh = None
        last_dep = arow.get("last_deposit_ts")
        funded_by_watch = False

        if deep and i < DEEP_TOP_N:
            if not p.get("opened_ts"):
                p["opened_ts"] = await _opened_ts_from_api(client, coin, p["address"])
            info = await _ledger_scan(client, p["address"])
            if info is not None:
                first_dep = info["first"] or arow.get("first_deposit_ts")
                last_dep = info["last"] or last_dep
                if first_dep:
                    fresh = first_dep >= fresh_cut
                funded_by_watch = bool(info["funders"] & watch)
                await clusters.store_links(p["address"], info["links"])
                async with db() as conn:
                    await conn.execute(
                        """UPDATE addresses SET
                             first_deposit_ts=COALESCE(?, first_deposit_ts),
                             last_deposit_ts=COALESCE(?, last_deposit_ts)
                           WHERE address=?""",
                        (info["first"], info["last"], p["address"]))
            elif arow.get("first_deposit_ts"):
                fresh = arow["first_deposit_ts"] >= fresh_cut

        twap_n = await _twap_fills(coin, p["address"], p["side"])
        score, reasons = compute(cfg, p, oi_ntl, funding, arow, fresh, last_dep, ref,
                                 twap_n=twap_n, funded_by_watch=funded_by_watch)
        p["score"] = score
        p["score_reasons"] = json.dumps(reasons, ensure_ascii=False)
        p["watch_record"] = (arow.get("hits") or 0, arow.get("misses") or 0)
        p["fresh"] = bool(fresh)

    async with db() as conn:
        for p in rows:
            await conn.execute(
                "UPDATE positions_current SET score=?, score_reasons=?, opened_ts=COALESCE(?, opened_ts)"
                " WHERE coin=? AND address=?",
                (p["score"], p["score_reasons"], p.get("opened_ts"), coin, p["address"]))
    return rows
