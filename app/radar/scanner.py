"""Balina pozisyon tarayıcısı.

Aday adres havuzu = (bu coini trade etmiş adresler) ∪ watchlist ∪ leaderboard
∪ halihazırda pozisyonu bilinenler. Hepsinin clearinghouseState'i sorgulanıp
coin'deki gerçek pozisyonlar çıkarılır.
"""
import asyncio
import logging

from ..config import Config
from ..db import db, kv_get, kv_set, now
from ..hl.client import HLClient
from ..hl.universe import symbol_of

log = logging.getLogger("radar.scanner")

LEADERBOARD_TTL = 86400
# Önbellek SABİT büyük boyutta saklanır — hangi görev (liq 300 / scanner 500 /
# sweeper 1500) önce yenilerse yenilesin, en büyük tüketici (derin keşif) hep
# tam listeyi alsın. Eskiden ilk çağıranın top_n*2'siyle boyutlanıp sweeper'ın
# 1500'ü 600-1000'e kırpılıyordu → uyuyan dev poz kaçırma riski geri geliyordu.
LEADERBOARD_CACHE_SIZE = 3000


async def _leaderboard_addrs(client: HLClient, top_n: int) -> list[str]:
    cached = await kv_get("leaderboard")
    if cached and now() - cached.get("ts", 0) < LEADERBOARD_TTL:
        return cached["addrs"][:top_n]
    data = await client.leaderboard()
    if not data:
        return (cached or {}).get("addrs", [])[:top_n]
    rows = data.get("leaderboardRows") or []
    try:
        rows.sort(key=lambda r: float(r.get("accountValue") or 0), reverse=True)
    except Exception:
        pass
    addrs = [r.get("ethAddress", "").lower()
             for r in rows[:LEADERBOARD_CACHE_SIZE] if r.get("ethAddress")]
    await kv_set("leaderboard", {"ts": now(), "addrs": addrs})
    log.info("leaderboard yenilendi: %d adres", len(addrs))
    return addrs[:top_n]


async def candidates(cfg: Config, client: HLClient, coin: str) -> list[str]:
    since = now() - cfg.fills_lookback_days * 86400
    async with db() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT address FROM fills WHERE coin=? AND ts>=?", (coin, since))
        pool = [r["address"] for r in await cur.fetchall()]
        cur = await conn.execute("SELECT address FROM addresses WHERE watchlist=1")
        watch = [r["address"] for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT address FROM positions_current WHERE coin=?", (coin,))
        known = [r["address"] for r in await cur.fetchall()]
    lb = await _leaderboard_addrs(client, cfg.leaderboard_top)

    ordered: list[str] = []
    seen: set[str] = set()
    for group in (watch, known, pool, lb):
        for a in group:
            a = a.lower()
            if a and a not in seen:
                seen.add(a)
                ordered.append(a)
    return ordered[: cfg.scan_max_candidates]


def _parse_positions(state: dict, coin: str) -> list[dict]:
    out = []
    target_sym = symbol_of(coin)
    n_open = 0
    for ap in state.get("assetPositions") or []:
        pos = ap.get("position") or {}
        try:
            szi = float(pos.get("szi") or 0)
        except (TypeError, ValueError):
            continue
        if szi == 0:
            continue
        n_open += 1
        pcoin = pos.get("coin") or ""
        if pcoin != coin and symbol_of(pcoin) != target_sym:
            continue
        liq = pos.get("liquidationPx")
        out.append({
            "szi": szi,
            "side": "long" if szi > 0 else "short",
            "entry_px": float(pos.get("entryPx") or 0),
            "leverage": float((pos.get("leverage") or {}).get("value") or 0),
            "liq_px": float(liq) if liq else None,
            "upnl": float(pos.get("unrealizedPnl") or 0),
            "notional": float(pos.get("positionValue") or 0),
        })
    for p in out:
        p["n_open_positions"] = n_open
    return out


async def timeline_from_fills(coin: str, address: str, side: str,
                              lookback_days: int,
                              retention_days: int | None = None) -> dict:
    """Yerel fill kayıtlarından yaklaşık zaman çizelgesi:
    opened (ilk açılış), last_add (son ekleme), last_trim (son kırpma).

    UYARI: fill'ler retention_days'ten eski silindiği için, penceredeki EN ESKİ
    add fill 'açılış' DEĞİLDİR — pozisyon pencereden önce açılmış olabilir. Bu
    yüzden opened veri ufkuna (retention sınırı) yapışıksa 'bilinmiyor' (None)
    döneriz; 'az önce açıldı' demeyiz (eski poz + küçük ekleme = sahte taze alarm)."""
    want_add = "buy" if side == "long" else "sell"
    want_trim = "sell" if side == "long" else "buy"
    since = now() - lookback_days * 86400
    async with db() as conn:
        cur = await conn.execute(
            "SELECT MIN(ts) a, MAX(ts) b FROM fills"
            " WHERE coin=? AND address=? AND side=? AND ts>=?",
            (coin, address, want_add, since))
        row = await cur.fetchone()
        opened, last_add = (row["a"], row["b"]) if row else (None, None)
        trim_since = opened or since
        cur = await conn.execute(
            "SELECT MAX(ts) t FROM fills"
            " WHERE coin=? AND address=? AND side=? AND ts>=?",
            (coin, address, want_trim, trim_since))
        row = await cur.fetchone()
        last_trim = row["t"] if row else None
    # Veri ufku sınırına yapışık açılış = büyük olasılıkla pencere-öncesinden taşıyor
    if opened is not None and retention_days:
        horizon = now() - (int(retention_days) - 1) * 86400
        if opened <= horizon:
            opened = None  # bilinmiyor — taze sanılmasın
    return {"opened": opened, "last_add": last_add, "last_trim": last_trim}


async def scan(cfg: Config, client: HLClient, coin: str, dex: str,
               max_candidates: int | None = None,
               beat_name: str | None = None) -> list[dict]:
    """Coin'deki pozisyonları bul, positions_current'ı güncelle, notional'a göre sırala.

    beat_name: ilerleme nabzının HANGİ göreve yazılacağı. Eskiden koşulsuz
    'autoscan'dı — web 'şimdi tara', /scan, due raporları da hb:autoscan'ı
    tazeliyor, oto-tarayıcı görevi ölmüşken bile bekçiye 'canlı' gösteriyordu.
    Sadece autoscan.loop 'autoscan' geçsin; diğer çağıranlar None geçip
    hiçbir görevin nabzını yanlış tazelemesin."""
    addrs = await candidates(cfg, client, coin)
    if max_candidates:
        addrs = addrs[:max_candidates]
    async with db() as conn:
        await conn.execute("INSERT OR REPLACE INTO scans(coin, ts) VALUES(?,?)",
                           (coin, now()))
    if not addrs:
        return []
    log.info("%s taranıyor: %d aday adres", coin, len(addrs))

    async def one(addr: str):
        if beat_name:
            from ..health import beat
            await beat(beat_name)  # ilerleme nabzı — yalnız gerçek çağıran görevine
        try:
            state = await client.clearinghouse(addr, dex)
            return addr, _parse_positions(state or {}, coin)
        except Exception as e:
            log.debug("clearinghouse %s: %s", addr, e)
            return addr, None  # hata: mevcut kaydı silme

    results = await asyncio.gather(*(one(a) for a in addrs))

    found: list[dict] = []
    ts = now()
    async with db() as conn:
        for addr, positions in results:
            if positions is None:
                continue
            # toz filtresi: eşik altı pozisyon = yok say
            positions = [p for p in positions
                         if p["notional"] >= cfg.min_position_notional]
            if not positions:
                await conn.execute(
                    "DELETE FROM positions_current WHERE coin=? AND address=?", (coin, addr))
                continue
            p = positions[0]
            p["address"] = addr
            p["coin"] = coin
            found.append(p)
    # zaman çizelgesi (yerel fill'lerden, yaklaşık; derin taramada API ile netleşir)
    for p in found:
        tl = await timeline_from_fills(coin, p["address"], p["side"],
                                       cfg.fills_lookback_days,
                                       cfg.fills_retention_days)
        p["opened_ts"] = tl["opened"]
        p["last_add_ts"] = tl["last_add"]
        p["last_trim_ts"] = tl["last_trim"]

    async with db() as conn:
        for p in found:
            await conn.execute(
                """INSERT INTO positions_current
                   (coin,address,ts,side,szi,entry_px,leverage,liq_px,upnl,notional,
                    opened_ts,score,score_reasons,last_add_ts,last_trim_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(coin,address) DO UPDATE SET
                     ts=excluded.ts, side=excluded.side, szi=excluded.szi,
                     entry_px=excluded.entry_px, leverage=excluded.leverage,
                     liq_px=excluded.liq_px, upnl=excluded.upnl, notional=excluded.notional,
                     opened_ts=CASE
                       WHEN positions_current.opened_ts IS NULL THEN excluded.opened_ts
                       WHEN excluded.opened_ts IS NULL THEN positions_current.opened_ts
                       ELSE MIN(positions_current.opened_ts, excluded.opened_ts) END,
                     score=COALESCE(excluded.score, positions_current.score),
                     score_reasons=COALESCE(excluded.score_reasons, positions_current.score_reasons),
                     last_add_ts=COALESCE(excluded.last_add_ts, positions_current.last_add_ts),
                     last_trim_ts=COALESCE(excluded.last_trim_ts, positions_current.last_trim_ts)""",
                (coin, p["address"], ts, p["side"], p["szi"], p["entry_px"], p["leverage"],
                 p["liq_px"], p["upnl"], p["notional"], p.get("opened_ts"), None, None,
                 p.get("last_add_ts"), p.get("last_trim_ts")))

    found.sort(key=lambda p: p["notional"], reverse=True)
    log.info("%s: %d pozisyon bulundu (en büyük: $%.0f)",
             coin, len(found), found[0]["notional"] if found else 0)
    return found


async def snapshot(event_id: int | None, phase: str, rows: list[dict]) -> None:
    ts = now()
    async with db() as conn:
        for p in rows:
            await conn.execute(
                """INSERT INTO position_snapshots
                   (event_id,phase,coin,address,ts,side,szi,entry_px,leverage,liq_px,upnl,notional,
                    score,score_reasons,last_add_ts,last_trim_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event_id, phase, p["coin"], p["address"], ts, p["side"], p["szi"],
                 p["entry_px"], p["leverage"], p["liq_px"], p["upnl"], p["notional"],
                 p.get("score"), p.get("score_reasons"),
                 p.get("last_add_ts"), p.get("last_trim_ts")))
