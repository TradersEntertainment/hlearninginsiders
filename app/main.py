"""HL Insider Radar — giriş noktası.

Tek process: FastAPI dashboard + arka plan görevleri
(WS collector, takvim, metrik toplayıcı, T-1h tetikleyici, Telegram bot).
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from fastapi import FastAPI

from . import db as dbm
from .config import get_config
from .earnings import calendar as cal
from .earnings import evaluator
from .hl import universe as uni
from .hl.client import HLClient
from .hl.collector import Collector
from .radar import metrics, report
from .telegram.bot import TelegramBot
from .web.routes import router

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("main")

TR = ZoneInfo("Europe/Istanbul")
STATE: dict = {}


def _stamp(key: str) -> None:
    STATE[key] = datetime.now(TR).strftime("%d.%m %H:%M")


async def supervised(name: str, factory):
    """Görev çökerse exponential backoff ile yeniden başlat."""
    delay = 5
    while True:
        try:
            await factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(name).exception("görev çöktü, %ds sonra yeniden", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)


# ---------------- periyodik görevler ----------------

async def universe_loop(cfg, client):
    while True:
        try:
            coins = await uni.refresh_universe(client, cfg.equity_dexes)
            if coins:
                _stamp("evren yenileme")
        except Exception as e:
            log.warning("evren yenilenemedi: %s", e)
        await asyncio.sleep(cfg.universe_refresh_sec)


async def calendar_loop(cfg, session):
    await asyncio.sleep(20)  # önce evren gelsin
    while True:
        try:
            n = await cal.refresh_calendar(cfg, session)
            STATE["takvimdeki earnings"] = n
            _stamp("takvim yenileme")
        except Exception as e:
            log.warning("takvim yenilenemedi: %s", e)
        await asyncio.sleep(cfg.calendar_refresh_sec)


async def metrics_loop(cfg, client):
    while True:
        try:
            n = await metrics.poll_metrics(cfg, client)
            if n:
                _stamp("metrik toplama")
        except Exception as e:
            log.warning("metrik toplanamadı: %s", e)
        await asyncio.sleep(cfg.metrics_poll_sec)


async def check_due(cfg, client, bot):
    async with dbm.db() as conn:
        cur = await conn.execute("SELECT * FROM earnings_events WHERE evaluated=0")
        events = [dict(r) for r in await cur.fetchall()]
    ts = dbm.now()
    for ev in events:
        for stage, due in cal.stages(ev):
            flag = "alerted_t1" if stage == "t1" else "alerted_pre"
            if ev.get(flag) or ts < due:
                continue
            if ts - due > 7200:
                # pencere kaçmış (ör. bot kapalıymış) — sessizce işaretle
                async with dbm.db() as conn:
                    await conn.execute(
                        f"UPDATE earnings_events SET {flag}=1 WHERE id=?", (ev["id"],))
                continue
            log.info("⏰ %s %s raporu tetiklendi", ev["symbol"], stage)
            await report.run_stage(cfg, client, bot, ev, stage)
            ev[flag] = 1


async def due_loop(cfg, client, bot):
    await asyncio.sleep(30)
    while True:
        try:
            await check_due(cfg, client, bot)
            await evaluator.evaluate_due(cfg, client, bot)
        except Exception:
            log.exception("due check hatası")
        await asyncio.sleep(cfg.due_check_sec)


# ---------------- uygulama ----------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_config()
    dirn = os.path.dirname(cfg.db_path)
    if dirn:
        os.makedirs(dirn, exist_ok=True)
    await dbm.init_db(cfg.db_path)
    log.info("DB hazır: %s", cfg.db_path)

    session = aiohttp.ClientSession()
    client = HLClient(session, cfg.api_base, cfg.stats_leaderboard_url,
                      concurrency=cfg.scan_concurrency)
    bot = TelegramBot(cfg, session, client, STATE) if cfg.telegram_bot_token else None
    collector = Collector(cfg, session, bot)
    if bot:
        bot.collector = collector

    app.state.cfg = cfg
    app.state.client = client
    app.state.bot = bot
    app.state.collector = collector
    app.state.state = STATE

    tasks = [
        asyncio.create_task(supervised("universe", lambda: universe_loop(cfg, client))),
        asyncio.create_task(supervised("calendar", lambda: calendar_loop(cfg, session))),
        asyncio.create_task(supervised("metrics", lambda: metrics_loop(cfg, client))),
        asyncio.create_task(supervised("due", lambda: due_loop(cfg, client, bot))),
        asyncio.create_task(supervised("collector", collector.run)),
    ]
    if bot:
        tasks.append(asyncio.create_task(supervised("telegram", bot.run_polling)))
        log.info("Telegram bot aktif")
    else:
        log.warning("TELEGRAM_BOT_TOKEN yok — alert'ler kapalı, sadece dashboard")

    yield

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await session.close()


app = FastAPI(title="HL Insider Radar", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
async def health():
    coll = getattr(app.state, "collector", None)
    return {"ok": True, "ws_connected": bool(coll and coll.connected), "state": STATE}
