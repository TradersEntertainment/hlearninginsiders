"""Sessiz su radarı — düşük hacimli hisselerdeki dev pozisyonlar.

Kimse trade etmiyorken birinin OI'nin yarısını tutması başlı başına sinyaldir:
o boyut o hacimde kolay kapanmaz, yani sahibi uzun süre haklı çıkacağından
emin. Sekme mevcut devleri listeler; Telegram'a ise SADECE gerçekten absürt
boyutlu (varsayılan $2.5M+) YENİ açılanlar düşer.

Tamamen yerel veriden çalışır (positions_current + asset_metrics) — ekstra
API çağrısı yapmaz.
"""
import asyncio
import logging

from ..config import Config
from ..db import alert_log, alert_recent, db, now
from ..telegram import format as fmt

log = logging.getLogger("radar.lowvol")

# aynı coin+adres için tekrar sessiz-su alarmı atmadan önce beklenecek süre
LOWVOL_COOLDOWN = 7 * 86400


async def latest_metrics_all() -> dict[str, dict]:
    """Her coin için en güncel metrik satırı: mark_px, oi, day_volume."""
    async with db() as conn:
        cur = await conn.execute(
            """SELECT m.coin, m.mark_px, m.oi, m.funding, m.day_volume
               FROM asset_metrics m
               JOIN (SELECT coin, MAX(ts) mts FROM asset_metrics GROUP BY coin) x
                 ON x.coin = m.coin AND x.mts = m.ts""")
        return {r["coin"]: dict(r) for r in await cur.fetchall()}


async def dominants(cfg: Config) -> list[dict]:
    """Sekmenin veri kaynağı: düşük hacimli hisselerdeki büyükler + OI hakimleri.

    Giriş şartı (VEYA): hacim düşük (≤ lowvol_max_day_volume) ve poz ≥ taban,
    ya da poz tek başına OI'nin ≥ lowvol_min_oi_share yüzdesini tutuyor.
    """
    mets = await latest_metrics_all()
    async with db() as conn:
        cur = await conn.execute(
            """SELECT p.*, t.symbol,
                      COALESCE(a.entity,'') entity,
                      COALESCE(a.hits,0) hits, COALESCE(a.misses,0) misses,
                      COALESCE(a.watchlist,0) watchlist
               FROM positions_current p
               JOIN tickers t ON t.coin = p.coin
               LEFT JOIN addresses a ON a.address = p.address
               WHERE p.notional >= ?""", (cfg.lowvol_min_notional,))
        rows = [dict(r) for r in await cur.fetchall()]

    out = []
    for p in rows:
        m = mets.get(p["coin"]) or {}
        vol = m.get("day_volume")
        oi_ntl = (m.get("oi") or 0) * (m.get("mark_px") or 0)
        share = p["notional"] / oi_ntl * 100 if oi_ntl > 0 else None
        low_volume = vol is not None and vol < cfg.lowvol_max_day_volume
        dominant = share is not None and share >= cfg.lowvol_min_oi_share
        if not (low_volume or dominant):
            continue
        p["day_volume"] = vol
        p["oi_ntl"] = oi_ntl or None
        p["oi_share"] = min(share, 100.0) if share is not None else None
        p["vol_ratio"] = (p["notional"] / vol) if vol else None  # hacmin kaç katı
        p["mark"] = m.get("mark_px")
        p["low_volume"] = low_volume
        out.append(p)

    # insanlar önce; sonra OI payı, hacim katı, boyut
    out.sort(key=lambda p: (bool(p["entity"]), -(p["oi_share"] or 0),
                            -(p["vol_ratio"] or 0), -p["notional"]))
    return out


async def check_alerts(cfg: Config, notifier) -> int:
    """Yeni açılmış, gerçekten absürt boyutlu sessiz-su devlerini bildir."""
    if not notifier:
        return 0
    rows = await dominants(cfg)
    ts = now()
    sent = 0
    for p in rows:
        if p["entity"]:  # MM/vault — bilgi sekmede, alarm yok
            continue
        if p["notional"] < cfg.lowvol_alert_min_usd:
            continue
        opened = p.get("opened_ts")
        # "yeni açılan" şartı: açılışı bilinmeyen eski pozlar sekmede kalır,
        # Telegram'ı doldurmaz
        if not opened or ts - opened > cfg.fresh_big_alert_hours * 3600:
            continue
        key = f"{p['coin']}:{p['address']}"
        if await alert_recent("lowvol", key, LOWVOL_COOLDOWN):
            continue
        text = fmt.lowvol_alert(p)
        try:
            if await notifier.send("lowvol", text, priority="high", key=key):
                await alert_log("lowvol", key, text)
                sent += 1
        except Exception as e:
            log.warning("sessiz-su alarmı gönderilemedi: %s", e)
    return sent


async def loop(cfg: Config, notifier) -> None:
    """DB üzerinden hafif kontrol — oto-tarayıcı pozisyonları tazeledikçe
    yeni devler buradan yakalanır."""
    await asyncio.sleep(240)  # önce metrik + ilk taramalar biriksin
    log.info("sessiz su radarı başladı (alarm tabanı $%.1fM)",
             cfg.lowvol_alert_min_usd / 1e6)
    while True:
        try:
            from ..health import beat
            await beat("lowvol")
            await check_alerts(cfg, notifier)
            await beat("lowvol")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sessiz su kontrol hatası")
        await asyncio.sleep(300)
