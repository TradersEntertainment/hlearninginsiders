"""Bildirim merkezi.

Tüm Telegram mesajları buradan geçer: tip bazlı açma/kapama, sessiz saatler,
öncelik (kritik olanlar sessiz saatte bile geçer), kayıt ve günlük özet.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Config
from .db import alert_log, db, now

log = logging.getLogger("notify")

TR = ZoneInfo("Europe/Istanbul")

# tip -> (ayar adı, insanca isim, varsayılan öncelik)
KINDS: dict[str, tuple[str, str, str]] = {
    "earnings":  ("notify_earnings",  "📊 Earnings raporları (T-1h)",        "high"),
    "eval":      ("notify_eval",      "🏁 Earnings sonuç raporları",          "normal"),
    "new_big":   ("notify_new_big",   "🆕 Yeni büyük pozisyon",               "high"),
    "whale_fill": ("notify_whale_fill", "🐋 Büyük işlem / sicilli balina",    "normal"),
    "anomaly":   ("notify_anomaly",   "📡 OI / funding anomalisi",            "normal"),
    "liq":       ("notify_liq",       "💥 Likidasyon radarı",                 "high"),
    "liqmap":    ("notify_liqmap",    "🧲 Likidasyon duvarı (küme)",          "high"),
    "track":     ("notify_track",     "👣 Pozisyon kapanış takibi",           "high"),
    "lowvol":    ("notify_lowvol",    "🐘 Sessiz su devi (düşük hacim)",      "high"),
    "wall":      ("notify_wall",      "🧱 Emir defteri duvarı",               "high"),
    "offhours":  ("notify_offhours",  "🌙 Kapalı seans hareketi",             "high"),
    "cryptovol": ("notify_cryptovol", "🚀 Kripto hacim patlaması",            "high"),
    "equityvol": ("notify_equityvol", "📈 Hisse hacim patlaması",             "high"),
    "pattern":   ("notify_pattern",   "🔮 Örüntü sinyali",                    "high"),
    "listing":   ("notify_listing",   "🆕 Yeni hisse listelendi",             "high"),
    "health":    ("notify_health",    "⚕️ Sistem sağlığı",                    "high"),
    "digest":    ("notify_digest",    "🌅 Günlük özet",                       "normal"),
    "test":      ("",                 "🔧 Test",                              "critical"),
}


def kind_enabled(cfg: Config, kind: str) -> bool:
    field = KINDS.get(kind, ("", "", ""))[0]
    return bool(getattr(cfg, field, True)) if field else True


def in_quiet_hours(cfg: Config, ts: int | None = None) -> bool:
    """Sessiz saat penceresi (TSİ). start==end ise kapalı."""
    s, e = int(cfg.quiet_start_hour), int(cfg.quiet_end_hour)
    if s == e:
        return False
    h = datetime.fromtimestamp(ts or now(), TR).hour
    return (s <= h < e) if s < e else (h >= s or h < e)


class Notifier:
    """bot.send'i sarmalar; kararı verir, kaydı tutar."""

    def __init__(self, cfg: Config, bot):
        self.cfg = cfg
        self.bot = bot
        self.suppressed = 0

    async def send(self, kind: str, text: str, *, priority: str = "",
                   key: str = "", chat_id: str = "") -> bool:
        if not self.bot:
            return False
        prio = priority or KINDS.get(kind, ("", "", "normal"))[2]

        # Tip kapalıysa hiçbir şey gönderilmez — kullanıcının ayarı her zaman
        # kazanır (critical bile). critical yalnız SESSİZ SAAT'i deler (aşağıda);
        # tip toggle'ını değil. Önce: notify_earnings/track/liq=0 iken 'critical'
        # earnings raporu / kapanış / SON UYARI ayarı yok sayıp gidiyordu.
        if not kind_enabled(self.cfg, kind):
            log.debug("bildirim kapalı, atlandı: %s", kind)
            return False

        # KANAL HEDEFLİ mesajda sessiz saat ertelemesi UYGULANMAZ. Sessiz saat
        # kişisel sohbeti korumak içindir; ayrı kanal kullanıcının bilerek
        # açtığı bir akıştır ve 5 dakikalık bir hacim patlamasını sabah
        # özetine ertelemek onu değersiz kılar. Tip toggle'ı yine geçerli.
        if not chat_id and prio != "critical" and in_quiet_hours(self.cfg):
            if not (prio == "high" and self.cfg.quiet_allow_high):
                self.suppressed += 1
                async with db() as conn:
                    await conn.execute(
                        "INSERT INTO alerts_log(kind,key,ts,payload) VALUES(?,?,?,?)",
                        (f"quiet:{kind}", key or kind, now(), text[:2000]))
                log.info("sessiz saat — %s bildirimi sabah özetine bırakıldı", kind)
                return False

        ok = await self.bot.send(text, chat_id or None)
        if ok:
            await alert_log(f"sent:{kind}", key or kind, text)
        return ok


async def pending_digest_items(hours: int = 14) -> list[dict]:
    """Sessiz saatte biriken bildirimler."""
    async with db() as conn:
        cur = await conn.execute(
            "SELECT kind, key, ts, payload FROM alerts_log"
            " WHERE kind LIKE 'quiet:%' AND ts > ? ORDER BY ts",
            (now() - hours * 3600,))
        return [dict(r) for r in await cur.fetchall()]


async def clear_digest_items(hours: int = 14) -> None:
    async with db() as conn:
        await conn.execute(
            "DELETE FROM alerts_log WHERE kind LIKE 'quiet:%' AND ts <= ?",
            (now(),))


async def recent_sent(limit: int = 50) -> list[dict]:
    async with db() as conn:
        cur = await conn.execute(
            "SELECT kind, key, ts, payload FROM alerts_log"
            " WHERE kind LIKE 'sent:%' OR kind LIKE 'quiet:%'"
            " ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]
