"""OI / funding anomali dedektörü.

Pozisyon sahibini bulamasak bile "birileri birikiyor" erken alarmı:
- OI'de anormal artış (earnings yaklaşırken eşik düşer)
- Funding'in aşırıya kayması (yön beklentisinin bedeli ödeniyor demek)
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .. import assets
from ..config import Config
from ..db import alert_log, alert_recent, db, now
from ..earnings.calendar import event_ts_estimate
from ..telegram import format as fmt
from . import metrics

log = logging.getLogger("radar.anomaly")

ET = ZoneInfo("America/New_York")
COOLDOWN = 12 * 3600
# earnings açıklandıktan sonra bu süre boyunca anomali arama — sonrası
# haber akışıdır, "birileri biliyor olabilir" demek yanlış sinyal olur
POST_EARNINGS_QUIET = 48 * 3600


async def _events_within(hours: int) -> dict[str, dict]:
    """coin -> yaklaşan earnings eventi (varsa)."""
    frm = datetime.now(ET) - timedelta(days=1)
    to = datetime.now(ET) + timedelta(hours=hours)
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM earnings_events WHERE date_et>=? AND date_et<=? AND evaluated=0",
            (frm.strftime("%Y-%m-%d"), to.strftime("%Y-%m-%d")))
        out = {}
        for r in await cur.fetchall():
            out[r["coin"]] = dict(r)
        return out


async def check_anomalies(cfg: Config, notifier) -> None:
    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol FROM tickers")
        coins = [(r["coin"], r["symbol"]) for r in await cur.fetchall()]
    if not coins:
        return
    soon = await _events_within(72)
    ts = now()

    for coin, sym in coins:
        # Event var ama SAATİ geçmişse: "yaklaşıyor" deme. Yeni geçtiyse
        # (48h) coini komple atla — earnings sonrası OI patlaması normaldir.
        ev = soon.get(coin)
        if ev:
            est = event_ts_estimate(ev)
            if est and est <= ts:
                if ts - est < POST_EARNINGS_QUIET:
                    continue
                ev = None  # eski, değerlendirilememiş kayıt — normal eşik uygula

        cur_m = await metrics.latest_metric(coin)
        prev24 = await metrics.metric_at(coin, ts - 86400)
        prev4 = await metrics.metric_at(coin, ts - 4 * 3600)
        if not cur_m or not cur_m.get("mark_px"):
            continue
        oi_ntl = (cur_m["oi"] or 0) * cur_m["mark_px"]
        has_event = ev is not None
        triggers: list[str] = []

        # FX/endeks/emtia/kripto'da mikro OI'den %175 artış anlamsız — taban yüksek
        floor = (cfg.oi_spike_big_floor_usd if assets.kind(sym) == "non_equity"
                 else cfg.oi_spike_floor_usd)

        if prev24 and prev24["oi"] and oi_ntl >= floor:
            chg24 = (cur_m["oi"] - prev24["oi"]) / prev24["oi"] * 100
            thr = cfg.oi_spike_pct_event if has_event else cfg.oi_spike_pct_normal
            if chg24 >= thr:
                triggers.append(f"OI 24 saatte +%{chg24:.0f} ({fmt.usd(oi_ntl)})")
        if (has_event and prev4 and prev4["oi"] and oi_ntl >= floor):
            chg4 = (cur_m["oi"] - prev4["oi"]) / prev4["oi"] * 100
            if chg4 >= 30:
                triggers.append(f"OI son 4 saatte +%{chg4:.0f} (hızlı birikim)")

        funding = cur_m.get("funding")
        if funding is not None and abs(funding) >= cfg.funding_extreme:
            side = "shortlar" if funding < 0 else "longlar"
            t = f"funding aşırı: {funding * 100:+.4f}%/h ({side} ödemeyi göze almış)"
            if has_event:
                triggers.append(t)
            elif triggers:  # earnings yoksa funding tek başına yetmez, OI'ye eşlik etsin
                triggers.append(t)

        if not triggers:
            continue
        if await alert_recent("anomaly", coin, COOLDOWN):
            continue
        text = fmt.anomaly_alert(sym, coin, triggers, ev)
        try:
            prio = "high" if has_event else "normal"
            await notifier.send("anomaly", text, priority=prio, key=coin)
        except Exception as e:
            log.warning("anomali alerti gönderilemedi: %s", e)
        await alert_log("anomaly", coin, text)
        log.info("anomali: %s → %s", sym, "; ".join(triggers))
