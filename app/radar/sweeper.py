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
from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import Config
from ..db import db, kv_get, kv_set, now
from ..hl.client import HLClient
from ..hl.universe import symbol_of
from ..telegram import format as fmt
from .liqwatch import _iter_states
from .scanner import _leaderboard_addrs

log = logging.getLogger("radar.sweeper")

TR = ZoneInfo("Europe/Istanbul")

HARVEST_COINS_PER_CYCLE = 6   # tur başına recentTrades çekilecek coin sayısı
HOT_PER_BATCH = 30            # parti başına sıcak havuz adedi
COLD_PER_BATCH = 10           # parti başına soğuk havuz adedi (uzun kuyruk)
SPEC_INTERVAL = 600           # uzman paneli önbelleği tazeleme aralığı (sn)
METRICS_RETENTION_D = 45      # asset_metrics emekliliği (evaluator ≤7 gün bakar)
ALERTS_RETENTION_D = 30       # alerts_log emekliliği (en uzun cooldown 7 gün)


async def build_pools(cfg: Config, client: HLClient) -> tuple[list[str], list[str]]:
    """(sıcak, soğuk) havuzlar.

    Sıcak: pozisyon güncelliğinin geldiği yer — watchlist + mevcut pozisyon
    sahipleri + leaderboard. Küçük kalır, tur ~1 saatte döner.
    Soğuk: yakın zamanda işlem yapmış ama pozisyonu bilinmeyen adresler —
    uzun kuyruk, günler içinde döner (yenilerini WS/hasat zaten yakalar).
    """
    lb = await _leaderboard_addrs(client, cfg.sweep_leaderboard_top)
    since = now() - cfg.fills_lookback_days * 86400
    async with db() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT address FROM fills WHERE ts >= ?", (since,))
        traded = [r["address"] for r in await cur.fetchall()]
        cur = await conn.execute("SELECT DISTINCT address FROM positions_current")
        holders = [r["address"] for r in await cur.fetchall()]
        cur = await conn.execute("SELECT address FROM addresses WHERE watchlist=1")
        watch = [r["address"] for r in await cur.fetchall()]
    hot, seen = [], set()
    for group in (watch, holders, lb):
        for a in group:
            a = (a or "").lower()
            if a and a not in seen:
                seen.add(a)
                hot.append(a)
    cold = []
    for a in traded:
        a = (a or "").lower()
        if a and a not in seen:
            seen.add(a)
            cold.append(a)
    return hot, cold


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


def _slice(pool: list[str], cursor: int, count: int) -> tuple[list[str], int, bool]:
    """Dönüşümlü dilim: (adresler, yeni imleç, tur tamamlandı mı)."""
    if not pool:
        return [], 0, False
    cursor %= len(pool)
    take = min(count, len(pool))
    batch = [pool[(cursor + i) % len(pool)] for i in range(take)]
    new_cursor = cursor + take
    wrapped = new_cursor >= len(pool)
    return batch, (0 if wrapped else new_cursor), wrapped


async def sweep_batch(cfg: Config, client: HLClient) -> dict:
    hot, cold = await build_pools(cfg, client)
    if not hot and not cold:
        return {"hot": 0, "cold": 0}
    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol FROM tickers")
        rows = await cur.fetchall()
    coin_set = {r["coin"] for r in rows}
    sym_map = {(r["symbol"] or "").upper(): r["coin"] for r in rows}
    if not coin_set:
        return {"hot": len(hot), "cold": len(cold)}

    # parti bütçesi: sıcak öncelikli, artan slot soğuğa (ve tersi)
    n_hot = min(HOT_PER_BATCH, cfg.sweep_batch_size)
    n_cold = max(0, cfg.sweep_batch_size - n_hot)
    hb, hcur, hot_done = _slice(hot, int(await kv_get("sweep_cursor_hot") or 0), n_hot)
    cb, ccur, _ = _slice(cold, int(await kv_get("sweep_cursor_cold") or 0), n_cold)
    batch = hb + cb
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

    await kv_set("sweep_cursor_hot", hcur)
    await kv_set("sweep_cursor_cold", ccur)
    if hot_done:
        await kv_set("sweep_last_full", ts)
        log.info("derin keşif SICAK tur bitti: %d adres (soğuk kuyruk %d)",
                 len(hot), len(cold))
    tour_min = round(len(hot) / max(n_hot, 1) * cfg.sweep_interval_sec / 60)
    await kv_set("sweep_stats", {"hot": len(hot), "cold": len(cold),
                                 "tour_min": tour_min, "cursor": hcur,
                                 "batch_positions": n_pos, "ts": ts})
    return {"hot": len(hot), "cold": len(cold), "positions": n_pos}


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


async def compute_specialists(cfg: Config) -> list[dict]:
    """"Tek hisse uzmanları" panelini arka planda hesaplayıp kv'ye yaz.

    fills GROUP BY milyonlarca satırda saniyeler sürer — istek başına
    çalıştırılamaz (ana sayfadaki 30-40 sn'lik beyaz ekranın kaynağıydı).
    Ana sayfa artık yalnız specialists_cache kv'sini okur.
    """
    ts_now = now()
    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol FROM tickers")
        sym_map = {r["coin"]: r["symbol"] for r in await cur.fetchall()}
        cur = await conn.execute(
            "SELECT address, coin, COUNT(*) c, SUM(notional) v FROM fills"
            " WHERE ts >= ? GROUP BY address, coin",
            (ts_now - cfg.fills_lookback_days * 86400,))
        fillagg = [dict(r) for r in await cur.fetchall()]
    by_addr: dict[str, list[dict]] = {}
    for r in fillagg:
        by_addr.setdefault(r["address"], []).append(r)
    specialists = []
    for addr, lst in by_addr.items():
        total = sum(r["c"] for r in lst)
        if total < 5:
            continue
        top = max(lst, key=lambda r: r["c"])
        if top["c"] / total < 0.9:
            continue
        specialists.append({"address": addr, "coin": top["coin"],
                            "symbol": sym_map.get(top["coin"], top["coin"]),
                            "n": top["c"], "vol": top["v"]})
    specialists.sort(key=lambda s: -s["vol"])
    specialists = specialists[:10]
    if specialists:
        addrs = [s["address"] for s in specialists]
        qm = ",".join("?" * len(addrs))
        async with db() as conn:
            cur = await conn.execute(
                f"SELECT address, hits, misses, watchlist, entity FROM addresses"
                f" WHERE address IN ({qm})", addrs)
            rec = {r["address"]: dict(r) for r in await cur.fetchall()}
            cur = await conn.execute(
                f"SELECT address, coin, side, notional FROM positions_current"
                f" WHERE address IN ({qm})", addrs)
            posmap: dict[str, list[dict]] = {}
            for r in await cur.fetchall():
                posmap.setdefault(r["address"], []).append(dict(r))
        for s in specialists:
            s.update(rec.get(s["address"], {}))
            open_pos = [p for p in posmap.get(s["address"], []) if p["coin"] == s["coin"]]
            s["open"] = open_pos[0] if open_pos else None
        specialists = [s for s in specialists if not s.get("entity")]  # MM/vault hariç
    await kv_set("specialists_cache", specialists)
    return specialists


async def refresh_fills_count() -> int:
    """Şerit sayacı için COUNT(*) — istekte değil, arka planda."""
    async with db() as conn:
        cur = await conn.execute("SELECT COUNT(*) c FROM fills")
        n = (await cur.fetchone())["c"]
    await kv_set("fills_count", n)
    return n


async def maintenance(cfg: Config) -> None:
    """Günlük veri emekliliği — /data volume'u sınırsız büyümesin.

    fills ~700K satır/gün üretir; emeklilik olmadan aylar içinde GB'lara
    çıkar. Zaman çizelgesi/uzman paneli en fazla saklama penceresi kadar
    geriyi görür (taban 7 gün — skorlamanın yakın geçmişi korunur).
    """
    ts_now = now()
    keep = max(int(cfg.fills_retention_days), 7)
    async with db() as conn:
        cur = await conn.execute(
            "DELETE FROM fills WHERE ts < ?", (ts_now - keep * 86400,))
        n_fills = cur.rowcount or 0
        cur = await conn.execute(
            "DELETE FROM asset_metrics WHERE ts < ?",
            (ts_now - METRICS_RETENTION_D * 86400,))
        n_met = cur.rowcount or 0
        cur = await conn.execute(
            "DELETE FROM alerts_log WHERE ts < ?",
            (ts_now - ALERTS_RETENTION_D * 86400,))
        n_al = cur.rowcount or 0
    n_left = await refresh_fills_count()
    if n_fills or n_met or n_al:
        log.info("günlük bakım: %d fill, %d metrik, %d alarm kaydı emekli"
                 " (fills kalan %d)", n_fills, n_met, n_al, n_left)


async def housekeeping(cfg: Config) -> None:
    """Süpürme döngüsünün istek dışı işleri: uzman önbelleği + günlük bakım."""
    if now() - int(await kv_get("spec_last") or 0) >= SPEC_INTERVAL:
        await compute_specialists(cfg)
        await refresh_fills_count()
        await kv_set("spec_last", now())
    today = datetime.now(TR).date().isoformat()
    if await kv_get("maint_last_day") != today:
        await maintenance(cfg)
        await kv_set("maint_last_day", today)


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
        try:
            await housekeeping(cfg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("bakım hatası")
        await asyncio.sleep(cfg.sweep_interval_sec)
