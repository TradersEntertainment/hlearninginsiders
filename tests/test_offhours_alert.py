"""Kapalı seans bant alarmı — SNDK/MU vakasının pinlenmesi.

Yaşanan: hafta sonu SNDK ve MU ~%2 yükseldi, `/kapali` sapmayı DOĞRU
gösteriyordu ama tek bir bildirim gitmedi.

İki kusur birleşmişti:
  • Gönderim başarısız olsa da dedupe satırı yazılıyordu. Çıpa hafta sonu
    boyunca sabit olduğu için tek bir başarısızlık o bandı 30 gün susturuyordu.
  • Bantlar geri doldurulmuyordu: fiyat iki ölçüm arasında sıçrarsa yalnız
    anlık bant deneniyordu; o da zehirlendiyse geriye hiçbir şey kalmıyordu.

Bu paket ikisinin de geri gelmemesini pinler.
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db as dbm
from app.config import Config
from app.radar import offhours as oh


class Bot:
    """Telegram taklidi: `ok` False iken gönderim BAŞARISIZ."""

    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    async def send(self, text, chat_id=None):
        if not self.ok:
            return False
        self.sent.append(text)
        return True


def notifier(bot, cfg):
    from app.notify import Notifier
    return Notifier(cfg, bot)


async def fresh(cfg):
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "oh.db"))
    async with dbm.db() as c:
        for coin, sym in (("xyz:SNDK", "SNDK"), ("xyz:MU", "MU")):
            await c.execute("INSERT INTO tickers(coin,symbol) VALUES(?,?)",
                            (coin, sym))


async def seed_px(anchor, now_ts, pct, coin="xyz:SNDK", base=100.0):
    """Çıpada `base`, şimdi `base*(1+pct/100)`."""
    async with dbm.db() as c:
        await c.execute(
            "INSERT OR REPLACE INTO asset_metrics(coin,ts,mark_px,oi,funding,"
            "day_volume) VALUES(?,?,?,1000,0,0)", (coin, anchor - 60, base))
        await c.execute(
            "INSERT OR REPLACE INTO asset_metrics(coin,ts,mark_px,oi,funding,"
            "day_volume) VALUES(?,?,?,1010,0,0)",
            (coin, now_ts - 30, base * (1 + pct / 100)))


def cfg_weekend():
    """Hafta sonu + ABD kapalı olduğuna emin bir yapılandırma."""
    c = Config()
    c.telegram_chat_id = "-100"
    c.alert_forensics = False        # özet ayrı test edildi; burada gürültü
    return c


def force_weekend(monkey=True):
    """Testler hafta sonuna bağımlı olmasın: zaman kapılarını sabitle."""
    from app.radar import hourstats as hs
    oh.hourstats.us_closed = lambda *a, **k: True
    oh.hourstats.weekend_window = lambda *a, **k: (0, 4_000_000_000)
    return hs


async def run_round(cfg, bot, dev_pct, coin="xyz:SNDK"):
    """Tek tur: fiyatı ayarla, check_alerts çalıştır."""
    now_ts = dbm.now()
    anchor = oh.hourstats.last_close_ts(now_ts, 0)
    await seed_px(anchor, now_ts, dev_pct, coin)
    return await oh.check_alerts(cfg, notifier(bot, cfg))


# ---------------------------- 1) ASIL HATA: başarısız gönderim zehirlemiyor
def test_no_poison():
    async def main():
        force_weekend()
        cfg = cfg_weekend()
        await fresh(cfg)
        bad = Bot(ok=False)
        out = await run_round(cfg, bad, 2.0)
        assert out["failed"] >= 1, out
        assert bad.sent == []
        # dedupe satırı YAZILMAMALI
        async with dbm.db() as c:
            cur = await c.execute(
                "SELECT COUNT(*) n FROM alerts_log WHERE kind='offhours'")
            assert (await cur.fetchone())["n"] == 0, "başarısız gönderim zehirledi"
            cur = await c.execute(
                "SELECT COUNT(*) n FROM alerts_log WHERE kind='fail:offhours'")
            assert (await cur.fetchone())["n"] >= 1, "başarısızlık görünmüyor"
        # bir sonraki tur AYNI bandı yeniden denemeli
        good = Bot(ok=True)
        out2 = await run_round(cfg, good, 2.0)
        assert out2["dev"] == 1, out2
        assert len(good.sent) == 1 and "SNDK" in good.sent[0]
        print("✅ zehirlenme) başarısız gönderim dedupe satırı YAZMIYOR;"
              " bir sonraki tur aynı bandı yeniden deniyor ve gidiyor")
    asyncio.run(main())


# ------------------------------------ 2) başarılı gönderim tekrar etmiyor
def test_once():
    async def main():
        force_weekend()
        cfg = cfg_weekend()
        await fresh(cfg)
        bot = Bot()
        assert (await run_round(cfg, bot, 2.0))["dev"] == 1
        assert (await run_round(cfg, bot, 2.0))["dev"] == 0, "aynı bant tekrarladı"
        assert (await run_round(cfg, bot, 2.1))["dev"] == 0, "aynı bant tekrarladı"
        assert len(bot.sent) == 1
        print("✅ tekrar yok) aynı bant ikinci kez duyurulmuyor (bant içi"
              " kıpırdanma sessiz)")
    asyncio.run(main())


# ------------------------------------------ 3) SIÇRAMA: tek mesaj, doğru bant
def test_jump():
    async def main():
        force_weekend()
        cfg = cfg_weekend()
        await fresh(cfg)
        bot = Bot()
        # %0.3 (eşik altı) → hiç mesaj
        assert (await run_round(cfg, bot, 0.3))["dev"] == 0
        # tek turda %2.1'e sıçra → TEK mesaj, bant 4
        out = await run_round(cfg, bot, 2.1)
        assert out["dev"] == 1, out
        assert len(bot.sent) == 1, "ara bantlar için ayrı mesaj üretilmemeli"
        assert "2.10" in bot.sent[0] or "2.1" in bot.sent[0]
        print("✅ sıçrama) %0.3 → %2.1 tek turda TEK mesaj üretiyor;"
              " ara bantlar için ayrı bildirim yok")
    asyncio.run(main())


# --------------------------------- 4) geri çekilme: yalnız YÜKSEĞE çıkınca
def test_retrace():
    async def main():
        force_weekend()
        cfg = cfg_weekend()
        await fresh(cfg)
        bot = Bot()
        assert (await run_round(cfg, bot, 2.1))["dev"] == 1      # bant 4
        assert (await run_round(cfg, bot, 1.2))["dev"] == 0      # bant 2 → sus
        assert (await run_round(cfg, bot, 2.3))["dev"] == 0      # yine bant 4
        assert (await run_round(cfg, bot, 2.6))["dev"] == 1      # bant 5 → duyur
        assert len(bot.sent) == 2
        print("✅ geri çekilme) bant düşünce susuyor, ancak DAHA YÜKSEK bir"
              " kademeye çıkınca yeni bildirim")
    asyncio.run(main())


# ------------------------------------------------- 5) yön ayrı olay
def test_direction():
    async def main():
        force_weekend()
        cfg = cfg_weekend()
        await fresh(cfg)
        bot = Bot()
        assert (await run_round(cfg, bot, 2.0))["dev"] == 1       # +bant 4
        assert (await run_round(cfg, bot, -2.0))["dev"] == 1, \
            "ters yöne savrulma AYRI olaydır"
        assert len(bot.sent) == 2
        print("✅ yön) +%2'den -%2'ye savrulma ayrı olay — filigran yön bazlı")
    asyncio.run(main())


# ------------------------------- 6) sessiz saat bastırır AMA zehirlemez
def test_quiet_no_poison():
    async def main():
        force_weekend()
        cfg = cfg_weekend()
        cfg.quiet_allow_high = False
        cfg.quiet_start_hour, cfg.quiet_end_hour = 0, 24  # her an sessiz
        await fresh(cfg)
        bot = Bot()
        out = await run_round(cfg, bot, 2.0)
        assert bot.sent == [] and out["failed"] >= 1, out
        async with dbm.db() as c:
            cur = await c.execute(
                "SELECT COUNT(*) n FROM alerts_log WHERE kind='quiet:offhours'")
            assert (await cur.fetchone())["n"] >= 1, "bastırma kaydı yok"
            cur = await c.execute(
                "SELECT COUNT(*) n FROM alerts_log WHERE kind='offhours'")
            assert (await cur.fetchone())["n"] == 0, "bastırma zehirledi"
        # sessiz saat bitince gitmeli
        cfg.quiet_start_hour = cfg.quiet_end_hour = 0
        assert (await run_round(cfg, bot, 2.0))["dev"] == 1
        print("✅ sessiz saat) bastırılıyor ama ZEHİRLEMİYOR — saat dolunca"
              " alarm gidiyor")
    asyncio.run(main())


# ------------------------------- 7) bir sembol patlarsa diğerleri devam
def test_isolation():
    async def main():
        force_weekend()
        cfg = cfg_weekend()
        await fresh(cfg)
        bot = Bot()
        now_ts = dbm.now()
        anchor = oh.hourstats.last_close_ts(now_ts, 0)
        await seed_px(anchor, now_ts, 2.0, "xyz:SNDK")
        await seed_px(anchor, now_ts, 2.0, "xyz:MU")
        orig = oh.fmt_move
        calls = []

        def boom(m):
            calls.append(m["symbol"])
            if m["symbol"] == "SNDK":
                raise ValueError("biçimlendirme patladı")
            return orig(m)

        oh.fmt_move = boom
        try:
            out = await oh.check_alerts(cfg, notifier(bot, cfg))
        finally:
            oh.fmt_move = orig
        assert set(calls) == {"SNDK", "MU"}, calls
        assert out["dev"] == 1 and out["failed"] >= 1, out
        assert len(bot.sent) == 1 and "MU" in bot.sent[0]
        print("✅ yalıtım) bir sembolün hatası turu düşürmüyor — diğer"
              " semboller yine değerlendiriliyor")
    asyncio.run(main())


# ------------------------------------------- 8) görünürlük: dört ayrı durum
def test_visibility():
    async def main():
        force_weekend()
        cfg = cfg_weekend()
        await fresh(cfg)
        bot = Bot()
        now_ts = dbm.now()
        anchor = oh.hourstats.last_close_ts(now_ts, 0)
        await seed_px(anchor, now_ts, 2.0, "xyz:SNDK")     # gidecek
        await seed_px(anchor, now_ts, 0.2, "xyz:MU")       # eşik altı
        await oh.check_alerts(cfg, notifier(bot, cfg))
        scr = await oh.screener(cfg)
        al = await oh.alarm_status(cfg, scr["rows"], anchor)
        st = {r["symbol"]: r for r in al["rows"]}
        assert st["SNDK"]["state"] == "sent" and st["SNDK"]["mark"] == 4, st["SNDK"]
        assert st["MU"]["state"] == "" and st["MU"]["band"] == 0, st["MU"]
        assert al["stats"].get("looked") == 2, al["stats"]
        assert al["stats"].get("dev") == 1
        # başarısızlık ayrı durum olarak görünmeli
        bad = Bot(ok=False)
        await seed_px(anchor, dbm.now(), 3.0, "xyz:MU")
        await oh.check_alerts(cfg, notifier(bad, cfg))
        al2 = await oh.alarm_status(cfg, (await oh.screener(cfg))["rows"], anchor)
        st2 = {r["symbol"]: r for r in al2["rows"]}
        assert st2["MU"]["state"] == "fail", st2["MU"]
        assert al2["stats"].get("failed") >= 1
        print("✅ görünürlük) gitti / eşik altı / gönderilemedi AYRI durumlar;"
              " tur künyesi kv'ye yazılıyor")
    asyncio.run(main())


# ----------------------------------- 9) atlanma sebebi kaydediliyor
def test_skipped_recorded():
    async def main():
        cfg = cfg_weekend()
        await fresh(cfg)
        oh.hourstats.us_closed = lambda *a, **k: False       # ABD açık
        out = await oh.check_alerts(cfg, notifier(Bot(), cfg))
        assert out["skipped"] == "ABD açık"
        stats = await dbm.kv_get("offhours_stats")
        assert stats and stats["skipped"] == "ABD açık", stats
        print("✅ atlanma) 'neden çalışmadı' sebebi kv'ye yazılıyor —"
              " main.py dönüş değerini atıyordu, artık kaybolmuyor")
    asyncio.run(main())


# --------------------------------------- 10) saklama > dedupe penceresi
def test_retention():
    from app.radar.sweeper import ALERTS_RETENTION_D
    assert ALERTS_RETENTION_D > 30, \
        "alerts_log saklaması dedupe penceresine eşit ya da kısa olmamalı"
    print(f"✅ saklama) alerts_log {ALERTS_RETENTION_D} gün > 30 günlük"
          " dedupe penceresi")


test_no_poison()
test_once()
test_jump()
test_retrace()
test_direction()
test_quiet_no_poison()
test_isolation()
test_visibility()
test_skipped_recorded()
test_retention()
print("\n✅ KAPALI SEANS ALARM TESTLERİ GEÇTİ")
