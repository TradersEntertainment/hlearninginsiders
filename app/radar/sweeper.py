"""Derin keşif süpürücüsü — eksik pozisyon verisini kapatır.

HL'de "bu marketteki tüm pozisyonlar" API'si yok; yalnız TANIDIĞIMIZ
adreslerin clearinghouse'u sorgulanabilir. Bot açılmadan önce pozisyon açmış
ve o günden beri işlem yapmamış balinalar WS akışına hiç düşmez — PLTR'de
"en büyük poz $1.26M" yanılgısı buradan çıktı.

İki sürekli akışla kapatılır:
1) Adres süpürmesi: geniş leaderboard dilimi (+ görülen tüm adresler)
   dönüşümlü olarak ALL_DEXES ile sorgulanır; TÜM hisse pozisyonları
   positions_current'a işlenir, kapananlar silinir. ~2 istek/sn — bir tam
   tur ~13 dk; tablo coin bağımsız, kapsamlı ve güncel kalır.
2) recentTrades hasadı: WS kopukluklarının kaçırdığı işlemler REST'ten
   toplanır (users alanı) → adres havuzu ve zaman çizelgeleri beslenir.
"""
import asyncio
import logging

from ..config import Config
from ..db import db, kv_get, kv_set, now
from ..hl.client import HLClient
from ..hl.universe import symbol_of
from ..telegram import format as fmt
from .liqwatch import _iter_states
from .scanner import _leaderboard_addrs

log = logging.getLogger("radar.sweeper")

HARVEST_COINS_PER_CYCLE = 6   # tur başına recentTrades çekilecek coin sayısı


async def build_pool(cfg: Config, client: HLClient) -> list[str]:
    """Süpürülecek adresler: geniş leaderboard + görülen herkes."""
    lb = await _leaderboard_addrs(client, cfg.sweep_leaderboard_top)
    async with db() as conn:
        cur = await conn.execute("SELECT DISTINCT address FROM fills")
        traded = [r["address"] for r in await cur.fetchall()]
        cur = await conn.execute("SELECT DISTINCT address FROM positions_current")
        holders = [r["address"] for r in await cur.fetchall()]
        cur = await conn.execute("SELECT address FROM addresses WHERE watchlist=1")
        watch = [r["address"] for r in await cur.fetchall()]
    ordered, seen = [], set()
    for group in (watch, holders, traded, lb):
        for a in group:
            a = (a or "").lower()
            if a and a not in seen:
                seen.add(a)
                ordered.append(a)
    return ordered


def _parse_equity_positions(resp, coin_set: set[str],
                            sym_map: dict[str, str],
                            min_ntl: float) -> tuple[dict[str, dict], bool]:
    """ALL_DEXES yanıtından hisse pozisyonları: (coin -> pozisyon, yanıt geçerli mi).
    Yanıtta hiç state yoksa 'geçersiz' döner — bozuk yanıtla kayıt SİLİNMEZ."""
    out: dict[str, dict] = {}
    states = list(_iter_states(resp))
    for state in states:
        for ap in state.get("assetPositions") or []:
            pos = ap.get("position") or {}
            pcoin = pos.get("coin") or ""
            coin = pcoin if pcoin in coin_set else sym_map.get(symbol_of(pcoin), "")
            if not coin:
                continue
            try:
                szi = float(pos.get("szi") or 0)
                ntl = float(pos.get("positionValue") or 0)
            except (TypeError, ValueError):
                continue
            if szi == 0 or ntl < min_ntl:
                continue
            liq = pos.get("liquidationPx")
            out[coin] = {
                "szi": szi, "side": "long" if szi > 0 else "short",
                "entry_px": float(pos.get("entryPx") or 0),
                "leverage": float((pos.get("leverage") or {}).get("value") or 0),
                "liq_px": float(liq) if liq else None,
                "upnl": float(pos.get("unrealizedPnl") or 0),
                "notional": ntl,
            }
    return out, bool(states)


async def _upsert_address(addr: str, positions: dict[str, dict], ts: int) -> None:
    async with db() as conn:
        for coin, p in positions.items():
            await conn.execute(
                """INSERT INTO positions_current
                   (coin,address,ts,side,szi,entry_px,leverage,liq_px,upnl,notional,
                    opened_ts,score,score_reasons,last_add_ts,last_trim_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(coin,address) DO UPDATE SET
                     ts=excluded.ts, side=excluded.side, szi=excluded.szi,
                     entry_px=excluded.entry_px, leverage=excluded.leverage,
                     liq_px=excluded.liq_px, upnl=excluded.upnl, notional=excluded.notional""",
                (coin, addr, ts, p["side"], p["szi"], p["entry_px"], p["leverage"],
                 p["liq_px"], p["upnl"], p["notional"], None, None, None, None, None))
        # yanıtın otoritesi: adresin artık tutmadığı pozisyonları sil
        if positions:
            q = ",".join("?" * len(positions))
            await conn.execute(
                f"DELETE FROM positions_current WHERE address=? AND coin NOT IN ({q})",
                (addr, *positions.keys()))
        else:
            await conn.execute(
                "DELETE FROM positions_current WHERE address=?", (addr,))


async def sweep_batch(cfg: Config, client: HLClient) -> dict:
    pool = await build_pool(cfg, client)
    if not pool:
        return {"pool": 0}
    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol FROM tickers")
        rows = await cur.fetchall()
    coin_set = {r["coin"] for r in rows}
    sym_map = {(r["symbol"] or "").upper(): r["coin"] for r in rows}
    if not coin_set:
        return {"pool": len(pool)}

    start = int(await kv_get("sweep_cursor") or 0) % len(pool)
    batch = [pool[(start + i) % len(pool)] for i in range(min(cfg.sweep_batch_size, len(pool)))]
    ts = now()
    n_pos = 0

    async def one(addr: str):
        nonlocal n_pos
        from ..health import beat
        await beat("sweeper")  # ilerleme nabzı
        try:
            resp = await client.clearinghouse(addr, "ALL_DEXES")
        except Exception as e:
            log.debug("sweep %s: %s", addr, e)
            return
        positions, valid = _parse_equity_positions(resp, coin_set, sym_map,
                                                   cfg.min_position_notional)
        if not valid:
            return  # bozuk yanıt — mevcut kayıtlara dokunma
        await _upsert_address(addr, positions, ts)
        n_pos += len(positions)

    await asyncio.gather(*(one(a) for a in batch))

    new_cursor = start + len(batch)
    if new_cursor >= len(pool):
        new_cursor = 0
        await kv_set("sweep_last_full", ts)
        log.info("derin keşif tam tur bitti: %d adres, %d hisse pozisyonu bulundu",
                 len(pool), n_pos)
    await kv_set("sweep_cursor", new_cursor)
    await kv_set("sweep_stats", {"pool": len(pool), "cursor": new_cursor,
                                 "batch_positions": n_pos, "ts": ts})
    return {"pool": len(pool), "positions": n_pos}


async def harvest_trades(cfg: Config, client: HLClient) -> int:
    """WS'in kaçırdığı işlemleri REST recentTrades'ten topla (evren rotasyonu)."""
    async with db() as conn:
        cur = await conn.execute("SELECT coin FROM tickers ORDER BY coin")
        coins = [r["coin"] for r in await cur.fetchall()]
    if not coins:
        return 0
    start = int(await kv_get("harvest_cursor") or 0) % len(coins)
    todo = [coins[(start + i) % len(coins)] for i in range(min(HARVEST_COINS_PER_CYCLE, len(coins)))]
    added = 0
    for coin in todo:
        try:
            trades = await client.recent_trades(coin)
        except Exception as e:
            log.debug("recentTrades %s: %s", coin, e)
            continue
        rows = []
        for t in trades or []:
            try:
                px = float(t["px"])
                sz = float(t["sz"])
                tts = int(t["time"]) // 1000
                tid = str(t.get("tid") or t.get("hash") or "")
                users = t.get("users") or []
            except (KeyError, TypeError, ValueError):
                continue
            notional = px * sz
            if not tid or len(users) < 2 or notional < cfg.min_fill_notional:
                continue
            rows.append((coin, tid, (users[0] or "").lower(), "buy", px, sz, notional, tts))
            rows.append((coin, tid, (users[1] or "").lower(), "sell", px, sz, notional, tts))
        if not rows:
            continue
        async with db() as conn:
            for r in rows:
                cur = await conn.execute(
                    "INSERT OR IGNORE INTO fills(coin,tid,address,side,px,sz,notional,ts)"
                    " VALUES(?,?,?,?,?,?,?,?)", r)
                added += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                await conn.execute(
                    "INSERT INTO addresses(address, first_seen) VALUES(?,?)"
                    " ON CONFLICT(address) DO NOTHING", (r[2], r[7]))
    await kv_set("harvest_cursor", (start + len(todo)) % len(coins))
    if added:
        stats = await kv_get("harvest_stats") or {"total": 0}
        stats["total"] = int(stats.get("total") or 0) + added
        stats["ts"] = now()
        await kv_set("harvest_stats", stats)
        log.info("işlem hasadı: %d yeni fill (%s)", added, ", ".join(
            fmt.short(c) if c.startswith("0x") else c.split(":")[-1] for c in todo))
    return added


async def loop(cfg: Config, client: HLClient) -> None:
    await asyncio.sleep(45)  # evren keşfini bekle
    log.info("derin keşif başladı: leaderboard ilk %d + görülen tüm adresler,"
             " %d adres/%ds", cfg.sweep_leaderboard_top, cfg.sweep_batch_size,
             cfg.sweep_interval_sec)
    while True:
        try:
            from ..health import beat
            await beat("sweeper")  # turbaşı: uzun parti sahte alarm üretmesin
            await sweep_batch(cfg, client)
            await beat("sweeper")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("süpürme hatası")
        try:
            await harvest_trades(cfg, client)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("hasat hatası")
        await asyncio.sleep(cfg.sweep_interval_sec)
