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
from . import health
from .config import get_config
from .earnings import calendar as cal
from .earnings import evaluator
from .hl import universe as uni
from .hl.client import HLClient
from .hl.collector import Collector
from .notify import Notifier, clear_digest_items, pending_digest_items
from .radar import (anomaly, autoscan, bookwall, hourstats, liqwatch, lowvol,
                    metrics, report, sweeper, tracker)
from .telegram.bot import TelegramBot
from .web.routes import router

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("main")

TR = ZoneInfo("Europe/Istanbul")
STATE: dict = {}
TASKS: dict[str, dict] = {}  # bekçinin yeniden başlatabilmesi için görev kaydı


def _stamp(key: str) -> None:
    STATE[key] = datetime.now(TR).strftime("%d.%m %H:%M")


async def supervised(name: str, factory, notifier=None):
    """Görev çökerse exponential backoff ile yeniden başlat + kullanıcıya bildir."""
    from . import health
    delay = 5
    while True:
        try:
            await factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.getLogger(name).exception("görev çöktü, %ds sonra yeniden", delay)
            try:
                if await health.note_crash(name, e) and notifier:
                    from .telegram import format as fmt
                    rec = await dbm.kv_get(f"crash:{name}") or {}
                    await notifier.send("health", fmt.crash_alert(name, e, rec.get("count", 1)),
                                        priority="high", key=f"crash:{name}")
            except Exception:
                pass
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)


def _spawn(name: str, factory, notifier=None) -> None:
    TASKS[name] = {"factory": factory, "notifier": notifier,
                   "task": asyncio.create_task(supervised(name, factory, notifier))}


def _respawn(name: str) -> bool:
    """Bekçi: takılan görevi iptal edip aynı fabrikayla yeniden yarat."""
    info = TASKS.get(name)
    if not info:
        return False
    info["task"].cancel()
    info["task"] = asyncio.create_task(
        supervised(name, info["factory"], info.get("notifier")))
    return True


# ---------------- periyodik görevler ----------------

async def universe_loop(cfg, client):
    while True:
        try:
            coins = await uni.refresh_universe(client, cfg.equity_dexes)
            await health.beat("universe")
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
            await health.beat("calendar")
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
                await health.beat("metrics")
                _stamp("metrik toplama")
        except Exception as e:
            log.warning("metrik toplanamadı: %s", e)
        await asyncio.sleep(cfg.metrics_poll_sec)


async def check_due(cfg, client, notifier):
    async with dbm.db() as conn:
        cur = await conn.execute("SELECT * FROM earnings_events WHERE evaluated=0")
        events = [dict(r) for r in await cur.fetchall()]
    ts = dbm.now()
    for ev in events:
        # Tek bozuk earnings satırı tüm döngüyü kilitlemesin
        try:
            est = cal.event_ts_estimate(ev)
            for stage, due in cal.stages(ev):
                flag = "alerted_t1" if stage == "t1" else "alerted_pre"
                if ev.get(flag) or ts < due:
                    continue
                # T-1h raporu AÇIKLAMA ANINI aşmamalı: restart penceredeyken
                # (est, due+2h] arasına denk gelirse 'T-1h' snapshot'ı açıklama
                # SONRASI pozları kaydedip haber-sonrası takipçileri insider
                # sanıyordu. Açıklama geçtiyse sessizce işaretle, rapor koşma.
                stale = ts - due > 7200 or (stage == "t1" and est and ts >= est)
                if stale:
                    async with dbm.db() as conn:
                        await conn.execute(
                            f"UPDATE earnings_events SET {flag}=1 WHERE id=?", (ev["id"],))
                    continue
                log.info("⏰ %s %s raporu tetiklendi", ev["symbol"], stage)
                await report.run_stage(cfg, client, notifier, ev, stage)
                ev[flag] = 1
        except Exception:
            log.exception("earnings işlenemedi: %s (%s)",
                          ev.get("symbol"), ev.get("date_et"))


async def due_loop(cfg, client, notifier):
    await asyncio.sleep(30)
    while True:
        try:
            await check_due(cfg, client, notifier)
            await evaluator.evaluate_due(cfg, client, notifier)
            # earnings saati geçtiyse "çıkışı takip edelim mi?" teklifi
            await tracker.make_offers(cfg, client, notifier)
            await health.beat("due")
        except Exception:
            log.exception("due check hatası")
        await asyncio.sleep(cfg.due_check_sec)


async def tracker_loop(cfg, client, notifier):
    """Takipteki balina pozlarını düzenli kontrol et (adım/kapanış/yön değişimi)."""
    await asyncio.sleep(90)
    while True:
        try:
            await health.beat("tracker")  # turbaşı: yavaş API sahte alarm üretmesin
            await tracker.check_trackers(cfg, client, notifier)
            await health.beat("tracker")
        except Exception:
            log.exception("takip kontrol hatası")
        await asyncio.sleep(cfg.track_poll_sec)


async def anomaly_loop(cfg, notifier):
    await asyncio.sleep(120)  # önce metrik birikmeye başlasın
    while True:
        try:
            await anomaly.check_anomalies(cfg, notifier)
            await health.beat("anomaly")
            _stamp("anomali taraması")
        except Exception:
            log.exception("anomali taraması hatası")
        await asyncio.sleep(cfg.anomaly_poll_sec)


async def digest_loop(cfg, notifier):
    """Her gün belirlenen saatte: gece biriken bildirimler + günün gündemi."""
    await asyncio.sleep(180)
    while True:
        try:
            hour = datetime.now(TR).hour
            last = await dbm.kv_get("digest_last_day")
            today = datetime.now(TR).strftime("%Y-%m-%d")
            if hour == int(cfg.digest_hour) and last != today:
                await dbm.kv_set("digest_last_day", today)
                if cfg.notify_digest:
                    from .earnings.calendar import annotate, upcoming_events
                    from .telegram import format as fmt
                    pending = await pending_digest_items()
                    events = annotate(await upcoming_events(3))
                    async with dbm.db() as conn:
                        cur = await conn.execute(
                            """SELECT p.*, t.symbol FROM positions_current p
                               JOIN tickers t ON t.coin=p.coin
                               LEFT JOIN addresses a ON a.address=p.address
                               WHERE COALESCE(p.score,0) >= 50 AND COALESCE(a.entity,'')=''
                               ORDER BY p.score DESC LIMIT 5""")
                        top = [dict(r) for r in await cur.fetchall()]
                    text = fmt.digest(pending, events, top)
                    if await notifier.send("digest", text, priority="normal"):
                        await clear_digest_items()
                    _stamp("günlük özet")
            await health.beat("digest")
        except Exception:
            log.exception("özet hatası")
        await asyncio.sleep(600)


async def watchdog_loop(cfg, notifier):
    """Bekçi: kalp atışlarını denetler, takılanı yeniden başlatır, bildirir."""
    await asyncio.sleep(240)  # açılışta her görev ilk turunu atsın
    log.info("bekçi başladı (her 120 sn kontrol)")
    while True:
        try:
            res = await health.watchdog_cycle(cfg, notifier, _respawn)
            if res.get("downs"):
                STATE["⚠️ sorunlu görevler"] = ", ".join(res["downs"])
            else:
                STATE.pop("⚠️ sorunlu görevler", None)
        except Exception:
            log.exception("bekçi hatası")
        await asyncio.sleep(120)


# ---------------- uygulama ----------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_config()
    dirn = os.path.dirname(cfg.db_path)
    if dirn:
        os.makedirs(dirn, exist_ok=True)
    await dbm.init_db(cfg.db_path)
    log.info("DB hazır: %s", cfg.db_path)

    # Dashboard'dan kaydedilmiş ayar override'larını yükle
    overrides = await dbm.kv_get("settings_overrides")
    if overrides:
        cfg.apply_overrides(overrides)
        log.info("%d ayar override'ı yüklendi: %s", len(overrides), ", ".join(overrides))

    # Sicil eşiği değiştiyse eski kayıtları yeni kurala göre yeniden hesapla (bir kez)
    from .recompute import recompute_records
    try:
        await recompute_records(cfg)
    except Exception:
        log.exception("sicil yeniden hesaplanamadı")

    session = aiohttp.ClientSession()
    client = HLClient(session, cfg.api_base, cfg.stats_leaderboard_url,
                      concurrency=cfg.scan_concurrency, max_rpm=cfg.hl_max_rpm)
    bot = TelegramBot(cfg, session, client, STATE) if cfg.telegram_bot_token else None
    notifier = Notifier(cfg, bot)
    collector = Collector(cfg, session, bot, notifier, client)
    if bot:
        bot.collector = collector

    app.state.cfg = cfg
    app.state.client = client
    app.state.bot = bot
    app.state.notifier = notifier
    app.state.collector = collector
    app.state.state = STATE

    await dbm.kv_set("boot_ts", dbm.now())  # bekçinin açılış toleransı
    _spawn("universe", lambda: universe_loop(cfg, client), notifier)
    _spawn("calendar", lambda: calendar_loop(cfg, session), notifier)
    _spawn("metrics", lambda: metrics_loop(cfg, client), notifier)
    _spawn("due", lambda: due_loop(cfg, client, notifier), notifier)
    _spawn("anomaly", lambda: anomaly_loop(cfg, notifier), notifier)
    _spawn("autoscan", lambda: autoscan.loop(cfg, client, notifier), notifier)
    _spawn("liqwatch", lambda: liqwatch.loop(cfg, client, notifier), notifier)
    _spawn("tracker", lambda: tracker_loop(cfg, client, notifier), notifier)
    _spawn("lowvol", lambda: lowvol.loop(cfg, notifier), notifier)
    _spawn("bookwall", lambda: bookwall.loop(cfg, client, notifier), notifier)
    _spawn("sweeper", lambda: sweeper.loop(cfg, client), notifier)
    _spawn("hourstats", lambda: hourstats.refresh_loop(cfg, client), notifier)
    _spawn("digest", lambda: digest_loop(cfg, notifier), notifier)
    _spawn("collector", collector.run, notifier)
    _spawn("watchdog", lambda: watchdog_loop(cfg, notifier), notifier)
    if bot:
        _spawn("telegram", bot.run_polling, notifier)
        log.info("Telegram bot aktif")
    else:
        log.warning("TELEGRAM_BOT_TOKEN yok — alert'ler kapalı, sadece dashboard")

    yield

    for info in TASKS.values():
        info["task"].cancel()
    await asyncio.gather(*(i["task"] for i in TASKS.values()), return_exceptions=True)
    await session.close()


app = FastAPI(title="HL Insider Radar", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
async def health_endpoint():
    coll = getattr(app.state, "collector", None)
    out = {"ok": True, "ws_connected": bool(coll and coll.connected), "state": STATE}
    try:
        snap = await health.snapshot(app.state.cfg)
        out["tasks"] = {n: {"ok": c["ok"], "silent_sec": c["silent"]}
                        for n, c in snap["checks"].items()}
        out["problems"] = snap["problems"]
        out["ok"] = not snap["problems"]
    except Exception:
        pass
    return out
