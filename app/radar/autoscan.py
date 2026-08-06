"""Arka plan oto-tarayıcı.

Kullanıcı "şimdi tara"ya basmak zorunda kalmasın diye evrendeki coin'ler
sürekli sırayla taranır: earnings'i yaklaşanlar öncelikli, sonra en bayat.
Coin sayfası açıldığında veri bayatsa anında arka planda tarama tetiklenir.
"""
import asyncio
import logging

from ..config import Config
from ..db import db, now
from ..hl.client import HLClient

log = logging.getLogger("radar.autoscan")

_inflight: set[str] = set()


def is_scanning(coin: str) -> bool:
    return coin in _inflight


async def scan_coin(cfg: Config, client: HLClient, coin: str, dex: str) -> None:
    from .report import build_scan  # döngüsel importu kır
    if coin in _inflight:
        return
    _inflight.add(coin)
    try:
        await build_scan(cfg, client, coin, dex, quick=True)
        log.info("oto-tarama tamam: %s", coin)
    except Exception as e:
        log.warning("oto-tarama %s: %s", coin, e)
    finally:
        _inflight.discard(coin)


def kick(cfg: Config, client: HLClient, coin: str, dex: str) -> None:
    """Ateşle-unut: sayfa açılışında bayat coin için arka plan taraması."""
    if coin not in _inflight:
        asyncio.create_task(scan_coin(cfg, client, coin, dex))


async def _pick_next(cfg: Config) -> tuple[str, str] | None:
    ts = now()
    async with db() as conn:
        # 1) earnings'i yaklaşan (±3 gün) ve 10+ dk'dır taranmamış coin
        cur = await conn.execute(
            """SELECT t.coin, t.dex FROM tickers t
               LEFT JOIN scans s ON s.coin = t.coin
               WHERE COALESCE(s.ts, 0) < ?
                 AND EXISTS(SELECT 1 FROM earnings_events e
                            WHERE e.coin = t.coin AND e.evaluated = 0
                              AND e.date_et BETWEEN date('now','-1 day') AND date('now','+3 day'))
               ORDER BY COALESCE(s.ts, 0) ASC LIMIT 1""",
            (ts - 600,))
        r = await cur.fetchone()
        if r:
            return r["coin"], r["dex"]
        # 2) genel: en bayat coin (en az 15 dk arayla)
        cur = await conn.execute(
            """SELECT t.coin, t.dex FROM tickers t
               LEFT JOIN scans s ON s.coin = t.coin
               WHERE COALESCE(s.ts, 0) < ?
               ORDER BY COALESCE(s.ts, 0) ASC LIMIT 1""",
            (ts - 900,))
        r = await cur.fetchone()
        return (r["coin"], r["dex"]) if r else None


async def loop(cfg: Config, client: HLClient) -> None:
    await asyncio.sleep(45)  # evren keşfini bekle
    log.info("oto-tarayıcı başladı (periyot: %ds)", cfg.auto_scan_interval_sec)
    while True:
        try:
            nxt = await _pick_next(cfg)
            if nxt:
                await scan_coin(cfg, client, nxt[0], nxt[1])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("oto-tarama döngü hatası")
        await asyncio.sleep(cfg.auto_scan_interval_sec)
