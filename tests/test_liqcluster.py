"""🧲 Likidasyon duvarı bildirimi — sınıf kapıları (liqwatch.check_clusters).

Pinlenenler:
  • endeks/emtia/FX (assets.NON_EQUITY, ör. CL): liq attack ile AYNI kapı — fiyatın
    ≤%1 içindeki liq ≥ $50M değilse mesaj YOK. CL vakası ($31.6M, 13 poz, %2.4–4.9)
    sessiz; toplam değil YAKIN liq sayılır; sayfa (find_clusters) duvarı yine listeler
  • geçen endeks duvarı: 📐 kapı satırı; aynı coin+yön 6 saat tekrar etmez
  • hisse: toplam ≥ $5M; top-10 hisse: ≥ $20M — eski davranış aynen, 📐 satırı yok
  • bağlantı: gate_for("CL") → (1, $50M, True); ayar açıklaması; README
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-liqcluster.db")
from app import db as dbm
from app.config import Config
from app.notify import Notifier
from app.radar import bookwall
from app.radar import liqattack as la
from app.radar import liqwatch as lw

MARK = 100.0
CL, SNDK, NVDA = ("xyz:CL", "CL"), ("xyz:SNDK", "SNDK"), ("xyz:NVDA", "NVDA")


class Bot:
    def __init__(self):
        self.sent = []

    async def send(self, text, chat_id=None):
        self.sent.append(text)
        return True


class Bigs:
    """bookwall.big_coin_set'i sabit kümeyle değiştir (check_clusters içinde import edilir)."""

    def __init__(self, bigs=()):
        self.bigs = set(bigs)

    def __enter__(self):
        self.orig = bookwall.big_coin_set

        async def fake(_cfg):
            return set(self.bigs)
        bookwall.big_coin_set = fake

    def __exit__(self, *a):
        bookwall.big_coin_set = self.orig


def _cfg():
    cfg = Config()
    cfg.telegram_chat_id = "-1"
    cfg.notify_liqmap = True
    cfg.alert_forensics = False
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    return cfg


async def seed(coins, positions):
    """coins: [(coin, sembol)] · positions: [(coin, adres, yön, mesafe %, notional)]"""
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "lc.db"))
    now = dbm.now()
    async with dbm.db() as c:
        for coin, sym in coins:
            await c.execute("INSERT INTO tickers(coin,symbol) VALUES(?,?)", (coin, sym))
            await c.execute("INSERT OR REPLACE INTO asset_metrics(coin,ts,mark_px,oi,funding,day_volume)"
                            " VALUES(?,?,?,1,0,0)", (coin, now, MARK))
        for coin, a, side, dist, ntl in positions:
            liq = MARK * (1 - dist / 100) if side == "long" else MARK * (1 + dist / 100)
            await c.execute("INSERT INTO positions_current(coin,address,ts,side,szi,entry_px,leverage,"
                            "liq_px,upnl,notional) VALUES(?,?,?,?,1,100,3,?,0,?)",
                            (coin, a, now, side, liq, ntl))


async def _log_count():
    async with dbm.db() as c:
        cur = await c.execute("SELECT COUNT(*) n FROM alerts_log")
        return (await cur.fetchone())["n"]


# ------------------------------------------------ 1) CL vakası: $31.6M / %2.4–4.9 → sessiz
def test_cl_wall_gated():
    async def run():
        small = [(CL[0], f"0xcl{i:02d}", "long", 2.4 + i * 0.22, 166_667) for i in range(12)]
        await seed([CL], [(CL[0], "0xbig", "long", 3.8, 29_600_000)] + small
                   + [(CL[0], "0xsh", "short", 3.0, 6_000_000)])
        cfg = _cfg()
        bot = Bot()
        with Bigs():
            out = await lw.check_clusters(cfg, Notifier(cfg, bot))
        assert out["walls"] == 2 and out["gated_big"] == 2 and out["gated"] == 2, out
        assert out["alerted"] == 0 and bot.sent == [] and await _log_count() == 0, "≤%1 içinde $50M yok → mesaj yok"
        # sayfa (ana sayfa haritası) duvarı yine görür: 13 poz, $31.6M, karşı yön $6M
        cl = next(c for c in await lw.find_clusters(cfg) if c["side"] == "long")
        assert cl["count"] == 13 and abs(cl["total"] - 31_600_004) < 1 and cl["other_total"] == 6_000_000
        assert 3.5 < cl["avg_dist"] < 4.0 and "gate" not in cl
        # $60M toplam ama ≤%1 içinde yalnız $10M → yine yok (toplam değil yakın liq sayılır)
        await seed([CL], [(CL[0], "0xa", "long", 0.8, 10_000_000), (CL[0], "0xb", "long", 3.0, 50_000_000)])
        with Bigs():
            out = await lw.check_clusters(cfg, Notifier(cfg, bot))
        assert out["gated_big"] == 1 and out["alerted"] == 0 and bot.sent == [], out
        print("✅ CL) $31.6M / %3.9 duvarı Telegram'a düşmez, sayfada durur; uzak $50M de sayılmaz")
    asyncio.run(run())


# ------------------------------------------------ 2) ≤%1 içinde $60M → gider (📐 satırı), 6 saat tekrar yok
def test_cl_wall_passes_and_cooldown():
    async def run():
        await seed([CL], [(CL[0], "0xa", "long", 0.8, 35_000_000), (CL[0], "0xb", "long", 0.9, 25_000_000)])
        cfg = _cfg()
        bot = Bot()
        with Bigs():
            out = await lw.check_clusters(cfg, Notifier(cfg, bot))
            assert out["alerted"] == 1 and out["gated"] == 0 and len(bot.sent) == 1, out
            t = bot.sent[0]
            assert "🧲 <b>LİKİDASYON DUVARI — CL</b>" in t and "$60.0M" in t, t
            assert "📐 endeks/emtia/FX kapısı: ≤%1 içinde $60.0M (eşik $50.0M)" in t, t
            assert await dbm.alert_recent("liq_cluster", "xyz:CL:long", lw.CLUSTER_COOLDOWN)
            out = await lw.check_clusters(cfg, Notifier(cfg, bot))
            assert out["cooldown"] == 1 and out["alerted"] == 0 and len(bot.sent) == 1, out
        print("✅ CL geçer) %0.8'de $60M → mesaj + 📐 kapı satırı; aynı duvar 6 saat tekrar etmez")
    asyncio.run(run())


# ------------------------------------------------ 3) hisse $5M / top-10 hisse $20M — eski davranış
def test_equity_and_top10_unchanged():
    async def run():
        cfg = _cfg()
        for ntl, expect in ((2_000_000, 1), (1_300_000, 0)):        # 3 poz: $6M gider, $3.9M yok
            await seed([SNDK], [(SNDK[0], f"0xs{i}", "long", 1.0 + i, ntl) for i in range(3)])
            bot = Bot()
            with Bigs():
                out = await lw.check_clusters(cfg, Notifier(cfg, bot))
            assert out["alerted"] == expect and len(bot.sent) == expect and out["gated_big"] == 0, (ntl, out)
            if expect:
                assert "SNDK" in bot.sent[0] and "📐" not in bot.sent[0], "hisse mesajında endeks kapısı satırı yok"
        for ntl, expect in ((25_000_000, 1), (15_000_000, 0)):      # top-10 hisse: $20M toplam
            await seed([NVDA], [(NVDA[0], "0xa", "long", 1.0, ntl * 0.6), (NVDA[0], "0xb", "long", 2.0, ntl * 0.4)])
            bot = Bot()
            with Bigs({NVDA[0]}):
                out = await lw.check_clusters(cfg, Notifier(cfg, bot))
            assert out["alerted"] == expect and len(bot.sent) == expect and out["gated_big"] == 0, (ntl, out)
        print("✅ hisse) $5M / top-10 $20M toplam eşikleri aynen; 📐 satırı yalnız endeks/emtia/FX'te")
    asyncio.run(run())


# ------------------------------------------------ 4) bağlantı
def test_wiring():
    from app.config import EDITABLE_FIELDS
    c = Config()
    assert la.gate_for(c, "CL") == (1.0, 50_000_000, True) and la.gate_for(c, "xyz:GOLD")[2] is True
    assert la.gate_for(c, "SNDK") == (2.0, 1_000_000, False)
    f = EDITABLE_FIELDS["liq_cluster_big_min_usd"]
    assert "KULLANMAZ" in f["desc"] and "Top-10" in f["label"] and c.liq_cluster_big_min_usd == 20_000_000
    assert "duvar" in EDITABLE_FIELDS["liq_attack_alert_big_min_usd"]["desc"]
    assert "duvar" in EDITABLE_FIELDS["liq_attack_alert_big_dist_pct"]["desc"]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rd = lambda *p: open(os.path.join(root, *p), encoding="utf-8").read()  # noqa: E731
    assert "from .liqattack import gate_for" in rd("app", "radar", "liqwatch.py")
    assert "bildiriminde de geçerli" in rd("README.md") and "endeks/emtia/FX kapısı" in rd("app", "telegram", "format.py")
    print("✅ bağlantı) CL non_equity → (%1, $50M); ayar açıklamaları; README; tek kaynak gate_for")
