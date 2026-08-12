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


async def _position_timeline(client: HLClient, coin: str, address: str) -> dict | None:
    """userFillsByTime ile pozisyon zaman çizelgesi:
    opened (serinin başladığı an), last_add (son ekleme), last_trim (son kırpma)."""
    try:
        start_ms = (now() - 14 * 86400) * 1000
        fills = await client.user_fills_by_time(address, start_ms)
    except Exception as e:
        log.debug("userFills %s: %s", address, e)
        return None
    sym = symbol_of(coin)
    evs = []
    for f in fills or []:
        if not (f.get("coin") == coin or symbol_of(f.get("coin") or "") == sym):
            continue
        try:
            sz = float(f.get("sz") or 0)
            delta = sz if f.get("side") == "B" else -sz
            t = int(f.get("time") or 0) // 1000
        except (TypeError, ValueError):
            continue
        if t:
            evs.append((t, delta))
    evs.sort()
    net = 0.0
    opened = None
    for t, delta in evs:
        prev = net
        net += delta
        if abs(net) < 1e-9:
            net, opened = 0.0, None      # pozisyon kapandı, seri sıfırlandı
        elif prev == 0.0 or (prev > 0 > net) or (prev < 0 < net):
            opened = t                   # yeni seri başladı
    if not opened or net == 0:
        return {"opened": None, "last_add": None, "last_trim": None}
    sign = 1 if net > 0 else -1
    adds = [t for t, d in evs if t >= opened and (d > 0) == (sign > 0)]
    trims = [t for t, d in evs if t >= opened and (d > 0) != (sign > 0)]
    return {"opened": opened,
            "last_add": max(adds) if adds else None,
            "last_trim": max(trims) if trims else None}


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


async def _detect_entity(cfg: Config, client: HLClient, address: str,
                         n_open_positions: int | None) -> str | None:
    """Market maker / vault tespiti — bunlar insider değil, gürültü.
    1) Çok sayıda açık pozisyon (insan insider 1-3 poz taşır, MM onlarca)
    2) 24 saatte aşırı sayıda çift yönlü büyük fill
    3) Hyperliquid vault'u mu? (vaultDetails)"""
    if (n_open_positions or 0) >= cfg.mm_max_positions:
        return "mm"
    since = now() - 86400
    async with db() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) c, SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END) b"
            " FROM fills WHERE address=? AND ts>=?", (address, since))
        r = await cur.fetchone()
    c, b = (r["c"] or 0), (r["b"] or 0)
    if c >= cfg.mm_max_fills_24h and 0 < b < c:
        return "mm"
    try:
        v = await client.vault_details(address)
        if v and (v.get("name") or v.get("vaultAddress")):
            return "vault"
    except Exception as e:
        log.debug("vaultDetails %s: %s", address, e)
    return None


ENTITY_LABEL = {"mm": "🤖 market maker", "vault": "🏦 vault",
                "manual": "🚫 elle elendi"}


async def _coin_focus(coin: str, address: str) -> bool:
    """Adres son 30 günde ağırlıklı olarak (>=%90) sadece bu hisseyi mi trade etmiş?"""
    since = now() - 30 * 86400
    async with db() as conn:
        cur = await conn.execute(
            "SELECT coin, COUNT(*) c FROM fills WHERE address=? AND ts>=? GROUP BY coin",
            (address, since))
        rows = await cur.fetchall()
    total = sum(r["c"] for r in rows)
    if total < 5:
        return False
    mine = next((r["c"] for r in rows if r["coin"] == coin), 0)
    return mine / total >= 0.9


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
            ref_ts: int, twap_n: int = 0, funded_by_watch: bool = False,
            coin_focus: bool = False) -> tuple[int, list[str]]:
    pts = 0
    reasons: list[str] = []

    opened = pos.get("opened_ts")
    opened_recent = False
    age_h = None
    if opened:
        age_h = max(0.0, (ref_ts - opened) / 3600)
        age_txt = f"{age_h:.0f}h" if age_h < 48 else f"{age_h / 24:.0f}g"
        if age_h < 6:
            pts += 25
            opened_recent = True
            reasons.append(f"pozisyon {age_txt} önce açıldı")
        elif age_h < 48:
            pts += 18
            reasons.append(f"pozisyon {age_txt} önce açıldı")
        elif age_h < 72:
            pts += 12
            reasons.append(f"pozisyon {age_txt} önce açıldı")
        elif age_h < 168:
            pts += 8
            reasons.append(f"pozisyon {age_txt} önce açıldı")
        elif age_h < 504:
            # CBRS dersi: insider illa taze olmak zorunda değil — 2 hafta önceden
            # çaktırmadan açıp bekleyebilir
            pts += 6
            reasons.append(f"sabırlı açılış ({age_txt} önce) — erken kuş")

    # Eski pozisyona earnings'ten hemen önce EKLEME yapmak da şüpheli
    last_add = pos.get("last_add_ts")
    if last_add and not opened_recent:
        add_age_h = max(0.0, (ref_ts - last_add) / 3600)
        if add_age_h < 6:
            pts += 10
            reasons.append(f"son ekleme {add_age_h:.0f}h önce")

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

    if coin_focus:
        pts += 8
        reasons.append("30 gündür sadece bu hisseyi trade ediyor")

    # Mutlak boyut: zengin balina liq'i uzak tutar ama BOYUT yalan söylemez
    ntl = pos["notional"]
    if ntl >= cfg.huge_position_usd:
        pts += 20
        reasons.append(f"dev pozisyon (${ntl / 1e6:.1f}M)")
    elif ntl >= cfg.big_position_usd:
        pts += 10
        reasons.append(f"büyük pozisyon (${ntl / 1e6:.1f}M)")

    # ASIL KALIP: büyük pozisyon + earnings'e yakın açılış (2-3 gün, maks 1 hafta)
    if (age_h is not None and ntl >= cfg.big_position_usd
            and age_h <= cfg.combo_window_hours):
        pts += 15
        reasons.append("🎯 büyük + taze açılış (insider paterni)")
    elif age_h is not None and ntl >= cfg.big_position_usd and age_h <= 504:
        # büyük poz + sessizce erkenden açılmış (CBRS paterni: 2 hafta önce)
        pts += 8
        reasons.append("🦉 büyük + sabırlı açılış (erken kuş insider)")

    if oi_ntl and oi_ntl > 0:
        share = ntl / oi_ntl * 100
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

        # Otomatik hesaplar (MM/vault) insider skorlamasına girmez
        entity = arow.get("entity")
        if not entity and deep and i < DEEP_TOP_N:
            entity = await _detect_entity(cfg, client, p["address"],
                                          p.get("n_open_positions"))
            if entity:
                async with db() as conn:
                    # MM/vault olarak etiketle VE varsa yanlış birikmiş sicilini sil
                    await conn.execute(
                        "INSERT INTO addresses(address, first_seen, entity) VALUES(?,?,?)"
                        " ON CONFLICT(address) DO UPDATE SET entity=excluded.entity,"
                        " hits=0, misses=0, watchlist=0",
                        (p["address"], now(), entity))
                    await conn.execute(
                        "DELETE FROM address_wins WHERE address=?", (p["address"],))
                log.info("otomatik hesap etiketlendi: %s → %s (sicil temizlendi)",
                         p["address"], entity)
        if entity:
            p["entity"] = entity
            p["score"] = 0
            p["score_reasons"] = json.dumps(
                [f"{ENTITY_LABEL.get(entity, entity)} — skorlama dışı"],
                ensure_ascii=False)
            p["watch_record"] = (arow.get("hits") or 0, arow.get("misses") or 0)
            p["fresh"] = False
            continue

        if deep and i < DEEP_TOP_N:
            tl = await _position_timeline(client, coin, p["address"])
            if tl is not None:
                # API verisi yerel yaklaşıklığı ezer (daha kesin)
                p["opened_ts"] = tl["opened"] or p.get("opened_ts")
                p["last_add_ts"] = tl["last_add"] or p.get("last_add_ts")
                p["last_trim_ts"] = tl["last_trim"] or p.get("last_trim_ts")
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
        focus = await _coin_focus(coin, p["address"])
        score, reasons = compute(cfg, p, oi_ntl, funding, arow, fresh, last_dep, ref,
                                 twap_n=twap_n, funded_by_watch=funded_by_watch,
                                 coin_focus=focus)
        p["score"] = score
        p["score_reasons"] = json.dumps(reasons, ensure_ascii=False)
        p["watch_record"] = (arow.get("hits") or 0, arow.get("misses") or 0)
        p["fresh"] = bool(fresh)

    async with db() as conn:
        for p in rows:
            await conn.execute(
                """UPDATE positions_current SET score=?, score_reasons=?,
                     opened_ts=COALESCE(?, opened_ts),
                     last_add_ts=COALESCE(?, last_add_ts),
                     last_trim_ts=COALESCE(?, last_trim_ts)
                   WHERE coin=? AND address=?""",
                (p["score"], p["score_reasons"], p.get("opened_ts"),
                 p.get("last_add_ts"), p.get("last_trim_ts"), coin, p["address"]))
    return rows
