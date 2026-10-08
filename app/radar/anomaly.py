"""OI birikimi / funding anomali dedektörü.

Pozisyon sahibini bulamasak bile "birileri BÜYÜK pozisyon açtı" erken alarmı:
- OI'nin 24 saatte $ olarak büyümesi (kullanıcı kuralı ≥ $5M; earnings ≤72 sa
  iken $2.5M ve son 4 saat de bakılır) — yüzde değil, $ artış kriterdir
- Funding'in aşırıya kayması (earnings yakınsa tek başına, değilse OI'ye eşlik)

Hacim tetiği KALDIRILDI: "24 saatlik hacim bir gün öncesinin N katı" pazartesi
hafta sonu tabanına kıyaslanınca onlarca hissede birden patlıyordu; genel işlem
hacminin artması sinyal değil (kullanıcı kuralı). Büyük hisse (NVDA…) ve
endeks/emtia bu kapıdan bildirim almaz (radar/alertgate.py — tek kaynak).
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..config import Config
from ..db import alert_log, alert_recent, db, kv_set, now
from ..earnings.calendar import event_ts_estimate
from ..telegram import format as fmt
from . import metrics

log = logging.getLogger("radar.anomaly")

ET = ZoneInfo("America/New_York")
COOLDOWN = 12 * 3600
ANOMALY_MAX_PER_RUN = 5      # bir turda en çok bu kadar bildirim (kalanlar sonraki turda; bekleme kurulmaz)
STATS_KV = "anomaly_stats"
# earnings açıklandıktan sonra bu süre boyunca anomali arama — sonrası
# haber akışıdır, "birileri biliyor olabilir" demek yanlış sinyal olur
POST_EARNINGS_QUIET = 48 * 3600
# metric_at boşlukta günler öncesini döndürebilir; '24h değişim' bu kadar eskiye
# dayanmasın (6h tolerans — poll aralığı normalde 30 dk)
METRIC_MAX_GAP = 6 * 3600



LAB_OI_MIN = 1_000_000           # 🧪 lab kayıt tabanı: 24 saatte OI artışı ($) — sınıf kapısından bağımsız, sabit
LAB_FUND_MIN = 0.0001            # |funding| ≥ %0.01/sa


def _lab_anomaly(coin: str, ts: int, mark: float, oi_ntl: float, prev24, prev4, funding, has_event: bool) -> bool:
    """🧪 Karar anı kaydı: OI 24 saatte ≥ $1M arttı ya da funding ≥ %0.01/sa — sınıf kapısı, ayarlı
    taban ve beklemeden ÖNCE. Coin başına günde bir kez (ilk geçiş). Yönsüz (0): analiz yönü fiyat
    değişimi ya da funding işaretinden kendi tanımlar. Senkron, G/Ç yok."""
    try:
        d24 = None
        if prev24 and prev24.get("oi") is not None and mark:
            d24 = (oi_ntl / mark - float(prev24["oi"] or 0)) * mark
        d4 = None
        if prev4 and prev4.get("oi") is not None and mark:
            d4 = (oi_ntl / mark - float(prev4["oi"] or 0)) * mark
        f = float(funding) if funding is not None else None
        if not ((d24 is not None and d24 >= LAB_OI_MIN) or (f is not None and abs(f) >= LAB_FUND_MIN)):
            return False
        p24 = (mark / float(prev24["mark_px"]) - 1) if prev24 and prev24.get("mark_px") else None
        from ..lab.registry import emit
        return emit("LOG-ANOM", coin, ts, side=0, px_ref=mark, trig_key=str(int(ts) // 86400),
                    features={"oi24": round(d24) if d24 is not None else None,
                              "oi4": round(d4) if d4 is not None else None, "oi": round(oi_ntl),
                              "fund": f, "px24": round(p24, 5) if p24 is not None else None,
                              "bilanco": int(has_event)})
    except Exception:                                  # noqa: BLE001
        return False

async def _events_within(hours: int) -> tuple[dict[str, dict], dict[str, int]]:
    """(upcoming, quiet_until):
      upcoming[coin] = coin'in en yakın GELECEK earnings eventi (bağlam + düşük eşik)
      quiet_until[coin] = son 48h içinde açıklanan event'in est'i (varsa) → o coin
        anomali taramasından muaf.
    Geçmiş 3 günü de kapsar ve evaluated filtrelemez — evaluator est+24h'te
    evaluated=1 yapınca quiet 48h yerine ~24h'te bitiyordu; ayrıca yalnız-gelecek
    seçimi geçmiş earnings'i maskeleyip 48h sessizliği deliyordu."""
    now_et = datetime.now(ET)
    frm = now_et - timedelta(days=3)
    to = now_et + timedelta(hours=hours)
    ts = now()
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM earnings_events WHERE date_et>=? AND date_et<=?",
            (frm.strftime("%Y-%m-%d"), to.strftime("%Y-%m-%d")))
        rows = [dict(r) for r in await cur.fetchall()]
    upcoming: dict[str, dict] = {}
    quiet_until: dict[str, int] = {}
    for r in rows:
        est = event_ts_estimate(r) or 0
        if not est:
            continue
        coin = r["coin"]
        if est > ts:  # gelecek → en yakınını tut
            prev = upcoming.get(coin)
            if prev is None or est < (event_ts_estimate(prev) or 0):
                upcoming[coin] = r
        elif ts - est < POST_EARNINGS_QUIET:  # 48h içinde açıklandı → sessiz
            quiet_until[coin] = max(quiet_until.get(coin, 0), est)
    return upcoming, quiet_until


async def check_anomalies(cfg: Config, notifier) -> None:
    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol FROM tickers")
        coins = [(r["coin"], r["symbol"]) for r in await cur.fetchall()]
    if not coins:
        return
    upcoming, quiet_until = await _events_within(72)
    ts = now()
    from . import alertgate
    from .autoscan import _big_coins
    big_coins = await _big_coins(cfg)
    stats = {"checked": 0, "alerted": 0, "capped": 0, "skipped_class": 0, "skipped_quiet": 0,
             "cooldown": 0, "triggered": 0, "failed": 0}

    for coin, sym in coins:
        # earnings 48h içinde açıklandıysa coini komple atla — sonrası haber
        # akışı, OI patlaması normaldir (artık gerçekten 48h sürer).
        if coin in quiet_until:
            stats["skipped_quiet"] += 1
            continue
        ev = upcoming.get(coin)  # yalnız GELECEK event bağlam/eşik sağlar

        cur_m = await metrics.latest_metric(coin)
        prev24 = await metrics.metric_at(coin, ts - 86400)
        prev4 = await metrics.metric_at(coin, ts - 4 * 3600)
        if not cur_m or not cur_m.get("mark_px"):
            continue
        # metric_at boşlukta günler öncesini döndürebilir → '24h değişim' yanlış.
        # Referans ölçüm hedeften METRIC_MAX_GAP'ten fazla eskiyse yok say.
        if prev24 and (prev24.get("ts") or 0) < ts - 86400 - METRIC_MAX_GAP:
            prev24 = None
        if prev4 and (prev4.get("ts") or 0) < ts - 4 * 3600 - METRIC_MAX_GAP:
            prev4 = None
        mark = float(cur_m["mark_px"])
        oi_now = float(cur_m["oi"] or 0)
        oi_ntl = oi_now * mark
        has_event = ev is not None
        triggers: list[str] = []
        cats: list[str] = []  # cooldown anahtarı için tetik türleri
        stats["checked"] += 1

        _lab_anomaly(coin, ts, mark, oi_ntl, prev24, prev4, cur_m.get("funding"), has_event)   # 🧪 kapıdan ÖNCE
        # Sınıf kapısı: büyük hisse (NVDA…) ve endeks/emtia bu bildirimi almaz;
        # normal hissede $ artış tabanı (earnings yakınsa düşük)
        floor = alertgate.oi_delta_floor(cfg, coin, big_coins, has_event)
        if floor is None:
            stats["skipped_class"] += 1
            continue

        if prev24 and prev24.get("oi") is not None:
            prev_oi = float(prev24["oi"] or 0)
            delta24 = (oi_now - prev_oi) * mark
            if delta24 >= floor:
                pct = f" (+%{(oi_now - prev_oi) / prev_oi * 100:.0f}" if prev_oi > 0 else " ("
                triggers.append(f"OI 24 saatte +{fmt.usd(delta24)}{pct} → {fmt.usd(oi_ntl)})")
                cats.append("oi24")
        if has_event and prev4 and prev4.get("oi") is not None:
            delta4 = (oi_now - float(prev4["oi"] or 0)) * mark
            if delta4 >= floor:
                triggers.append(f"OI son 4 saatte +{fmt.usd(delta4)} (hızlı birikim)")
                cats.append("oi4")

        funding = cur_m.get("funding")
        if funding is not None and abs(funding) >= cfg.funding_extreme:
            side = "shortlar" if funding < 0 else "longlar"
            t = f"funding aşırı: {funding * 100:+.4f}%/h ({side} ödüyor)"
            if has_event:
                triggers.append(t)
                cats.append("funding")
            elif triggers:  # earnings yoksa funding tek başına yetmez, OI'ye eşlik etsin
                triggers.append(t)
                cats.append("funding")

        if not triggers:
            continue
        # Cooldown anahtarı tetik TÜRÜNÜ içerir: OI alarmı, farklı bir sinyali
        # (funding aşırı = yeni yön bilgisi) 12h sessizce yutmasın.
        key = f"{coin}:{'+'.join(sorted(set(cats)))}"
        stats["triggered"] += 1
        if await alert_recent("anomaly", key, COOLDOWN):
            stats["cooldown"] += 1
            continue
        if stats["alerted"] >= ANOMALY_MAX_PER_RUN:
            stats["capped"] += 1            # bekleme kurulmaz: sonraki turda sırası gelir
            continue
        text = fmt.anomaly_alert(sym, coin, triggers, ev)
        try:
            prio = "high" if has_event else "normal"
            await notifier.send("anomaly", text, priority=prio, key=key, coin=coin)
        except Exception as e:
            stats["failed"] += 1
            log.warning("anomali alerti gönderilemedi: %s", e)
        await alert_log("anomaly", key, text)
        stats["alerted"] += 1
        log.info("anomali: %s → %s", sym, "; ".join(triggers))
    try:
        await kv_set(STATS_KV, {**stats, "ts": ts})
    except Exception:
        log.debug("anomaly_stats yazılamadı", exc_info=True)
