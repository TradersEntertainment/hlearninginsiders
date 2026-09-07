"""🚦 Hisse bildirim kapıları — sınıfa göre tek kaynak (radar/alertgate) + anomali OI birikimi.

Pinlenenler:
  • equity_class: SNDK normal, NVDA (hacimce ilk N) major, SP500/SOXL non_equity, para:ANSEM normal
  • big_floor 8M / None (major, 0 = yok) / None (endeks); fill_floor sicilli 250K her sınıfta,
    sicilsiz 5M yalnız normal; oi_delta_floor 5M / 2.5M (event) / None
  • anomali: pazartesi hacim 4× → bildirim YOK; OI +$6M → bildirim ("+$6.0M (+%60 → $16.0M)");
    +$2M → yok; NVDA +$20M ve SP500 +$50M → sınıf dışı; 12 sa bekleme; tur tavanı 5; anomaly_stats; /tani
  • _alert_new_big: $8.5M skor 10 → bildirim (skor kapısı yok); $6M → yok; NVDA $50M → yok; SP500 → yok
  • ayar/env/README/collector bağlantısı; /bildirimler metni kademeleri yazar
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-alertgate.db")
from app import assets  # noqa: E402
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config, get_config  # noqa: E402
from app.notify import Notifier  # noqa: E402
from app.radar import alertgate as ag  # noqa: E402
from app.radar import anomaly, autoscan  # noqa: E402

BIG = {"xyz:NVDA", "xyz:TSLA"}
ADDR = "0x" + "a" * 40


class Bot:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    async def send(self, text, chat_id=None):
        if self.ok:
            self.sent.append((chat_id, text))
        return self.ok


def _cfg():
    cfg = Config()
    cfg.telegram_chat_id = "111"
    cfg.notify_anomaly = cfg.notify_new_big = True
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    cfg.big_alert_min_usd, cfg.big_alert_major_usd, cfg.big_alert_index_usd = 8_000_000, 0, 0
    cfg.whale_alert_notional, cfg.whale_alert_watch_notional = 5_000_000, 250_000
    cfg.anomaly_oi_delta_min_usd, cfg.anomaly_oi_delta_event_usd = 5_000_000, 2_500_000
    cfg.fresh_big_alert_hours = 24
    cfg.equity_dexes, cfg.crypto_dexes = ["xyz"], ["para"]
    g = get_config()
    g.equity_dexes, g.crypto_dexes = ["xyz"], ["para"]
    assets.set_crypto_dex_symbols(["ANSEM"])
    return cfg


def test_gates_pure():
    cfg = _cfg()
    assert ag.equity_class("xyz:SNDK", BIG) == "normal" and ag.equity_class("xyz:NVDA", BIG) == "major"
    assert ag.equity_class("xyz:SP500", BIG) == "non_equity" and ag.equity_class("xyz:SOXL", BIG) == "non_equity"
    assert ag.equity_class("para:ANSEM", BIG | {"para:ANSEM"}) == "normal", "kripto dex 'major' sayılmaz"
    assert ag.big_floor(cfg, "xyz:SNDK", BIG) == 8_000_000 and ag.big_floor(cfg, "xyz:NVDA", BIG) is None
    assert ag.big_floor(cfg, "xyz:SP500", BIG) is None and ag.big_floor(cfg, "para:ANSEM", BIG) == 8_000_000
    cfg.big_alert_major_usd, cfg.big_alert_index_usd = 15_000_000, 20_000_000
    assert ag.big_floor(cfg, "xyz:NVDA", BIG) == 15_000_000 and ag.big_floor(cfg, "xyz:SP500", BIG) == 20_000_000
    assert ag.fill_floor(cfg, "xyz:SNDK", BIG, False) == 5_000_000 and ag.fill_floor(cfg, "xyz:NVDA", BIG, False) is None
    assert ag.fill_floor(cfg, "xyz:SP500", BIG, False) is None
    for c in ("xyz:SNDK", "xyz:NVDA", "xyz:SP500"):
        assert ag.fill_floor(cfg, c, BIG, True) == 250_000, c
    assert ag.oi_delta_floor(cfg, "xyz:SNDK", BIG, False) == 5_000_000 and ag.oi_delta_floor(cfg, "xyz:SNDK", BIG, True) == 2_500_000
    assert ag.oi_delta_floor(cfg, "xyz:NVDA", BIG, False) is None and ag.oi_delta_floor(cfg, "xyz:SP500", BIG, True) is None
    cfg.whale_alert_notional = 0
    assert ag.fill_floor(cfg, "xyz:SNDK", BIG, False) is None, "0 = kapalı"
    t = ag.tiers(_cfg())
    assert t["big_normal"] == 8e6 and t["big_major"] is None and t["big_index"] is None and t["fill_watch"] == 250_000
    assert autoscan.alert_floor(_cfg(), "xyz:SNDK", BIG) == 8_000_000 and autoscan.alert_floor(_cfg(), "xyz:NVDA", BIG) is None
    print("✅ kapılar) sınıf; yeni poz 8M/yok/yok; işlem 5M yalnız normal, sicilli 250K her sınıf; OI 5M/2.5M/yok; 0 = kapalı")


async def _seed(coins: dict, t: int):
    """coins: coin -> (oi_prev, oi_now, mark, vol_prev, vol_now)."""
    async with dbm.db() as c:
        for coin, (oi0, oi1, mark, v0, v1) in coins.items():
            sym = coin.split(":")[-1]
            await c.execute("INSERT OR IGNORE INTO tickers(coin,dex,symbol) VALUES(?,?,?)", (coin, "xyz", sym))
            await c.execute("INSERT OR REPLACE INTO asset_metrics(coin,ts,mark_px,oi,funding,day_volume) VALUES(?,?,?,?,?,?)",
                            (coin, t - 86400, mark, oi0, 0.0, v0))
            await c.execute("INSERT OR REPLACE INTO asset_metrics(coin,ts,mark_px,oi,funding,day_volume) VALUES(?,?,?,?,?,?)",
                            (coin, t - 30, mark, oi1, 0.0, v1))


def test_anomaly_oi_delta():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "anom.db"))
        cfg = _cfg()
        t = dbm.now()
        autoscan._big_cache.update({"ts": t, "coins": set(BIG)})
        await _seed({"xyz:SNDK": (100_000, 160_000, 100.0, 1e6, 4e6),     # +$6M, hacim 4× (pazartesi)
                     "xyz:MRVL": (100_000, 120_000, 100.0, 1e6, 4e6),     # +$2M → yok; hacim 4× → yok
                     "xyz:NVDA": (1_000_000, 1_200_000, 100.0, 1e8, 3e8),  # +$20M ama büyük hisse → yok
                     "xyz:SP500": (10_000, 20_000, 5000.0, 1e8, 3e8)}, t)  # +$50M ama endeks → yok
        bot = Bot()
        await anomaly.check_anomalies(cfg, Notifier(cfg, bot))
        assert len(bot.sent) == 1 and "ANOMALİ — SNDK" in bot.sent[0][1], bot.sent
        assert "OI 24 saatte +$6.0M (+%60 → $16.0M)" in bot.sent[0][1] and "hacim" not in bot.sent[0][1], bot.sent[0][1]
        st = await dbm.kv_get(anomaly.STATS_KV)
        assert st["checked"] == 4 and st["skipped_class"] == 2 and st["triggered"] == 1 and st["alerted"] == 1, st
        # bekleme: aynı tur tekrar → mesaj yok
        await anomaly.check_anomalies(cfg, Notifier(cfg, bot))
        assert len(bot.sent) == 1 and (await dbm.kv_get(anomaly.STATS_KV))["cooldown"] == 1
        # /tani satırı
        from app import diag
        txt = await diag.report(cfg, None)
        assert "anomali (OI birikimi): 4 coin bakıldı · 1 tetik · 0 bildirim · 2 sınıf dışı" in txt, txt
        # tur tavanı: 7 normal hisse +$6M → 5 bildirim, 2 tavana takılır; sonraki tur 2 daha
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "anom2.db"))
        await _seed({f"xyz:C{i}": (100_000, 160_000, 100.0, 1e6, 1e6) for i in range(7)}, t)
        bot = Bot()
        await anomaly.check_anomalies(cfg, Notifier(cfg, bot))
        st = await dbm.kv_get(anomaly.STATS_KV)
        assert len(bot.sent) == 5 and st["alerted"] == 5 and st["capped"] == 2, st
        await anomaly.check_anomalies(cfg, Notifier(cfg, bot))
        assert len(bot.sent) == 7, "tavana takılanlar sonraki turda gelir (bekleme kurulmamıştı)"
        print("✅ anomali) hacim 4× sessiz; OI +$6M bildirim; +$2M yok; NVDA/SP500 sınıf dışı; bekleme; tavan 5; /tani")
    asyncio.run(run())


def test_new_big_and_wiring():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "nb.db"))
        cfg = _cfg()
        t = dbm.now()
        autoscan._big_cache.update({"ts": t, "coins": set(BIG)})
        from app.telegram import format as fmt
        orig = fmt.new_big_position_alert
        fmt.new_big_position_alert = lambda coin, p, ev: f"BIG {coin} {p['address']} {p['notional']}"
        try:
            def row(ntl, score=10, addr=ADDR):
                return {"address": addr, "notional": ntl, "score": score, "opened_ts": t - 600,
                        "first_seen_ts": t - 600, "entity": None, "side": "long"}
            bot = Bot()
            n = Notifier(cfg, bot)
            await autoscan._alert_new_big(cfg, n, "xyz:SNDK", [row(8_500_000), row(6_000_000, addr="0x" + "b" * 40)])
            assert len(bot.sent) == 1 and "8500000" in bot.sent[0][1], bot.sent
            await autoscan._alert_new_big(cfg, n, "xyz:NVDA", [row(50_000_000, score=90, addr="0x" + "c" * 40)])
            await autoscan._alert_new_big(cfg, n, "xyz:SP500", [row(50_000_000, score=90, addr="0x" + "d" * 40)])
            assert len(bot.sent) == 1, "büyük hisse / endeks: bildirim yok"
            await autoscan._alert_new_big(cfg, n, "xyz:CBRS", [{**row(9_000_000, score=0, addr="0x" + "e" * 40), "entity": "mm"}])
            assert len(bot.sent) == 1, "mm elenir"
        finally:
            fmt.new_big_position_alert = orig
        # bağlantı
        c = Config()
        for f in ("anomaly_oi_delta_min_usd", "anomaly_oi_delta_event_usd"):
            assert EDITABLE_FIELDS[f]["group"] == "Anomali dedektörü" and hasattr(c, f), f
        assert "whale_alert_watch_notional" in EDITABLE_FIELDS and hasattr(c, "whale_alert_watch_notional")
        for f in ("oi_spike_pct_event", "oi_spike_pct_normal", "oi_spike_floor_usd", "oi_spike_big_floor_usd",
                  "vol_spike_mult", "vol_spike_min_usd"):
            assert f not in EDITABLE_FIELDS and not hasattr(c, f), f
        if not os.getenv("BIG_ALERT_MIN_USD"):
            assert c.big_alert_min_usd == 8e6 and c.big_alert_major_usd == 0 and c.big_alert_index_usd == 0
            assert c.whale_alert_notional == 5e6 and c.whale_alert_watch_notional == 250_000
            assert c.anomaly_oi_delta_min_usd == 5e6 and c.anomaly_oi_delta_event_usd == 2.5e6
        rd = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()  # noqa: E731
        assert "alertgate.fill_floor(self.cfg, coin, big_coins, is_watch)" in rd("app", "hl", "collector.py")
        assert "alert_min_score" not in rd("app", "radar", "autoscan.py") and "vol_spike" not in rd("app", "radar", "anomaly.py")
        assert "ANOMALY_OI_DELTA_MIN_USD=5000000" in rd(".env.example") and "BIG_ALERT_MAJOR_USD=0" in rd(".env.example")
        assert "Hisse kuralı" in rd("README.md") and "OI birikimi" in rd("app", "diag.py")
        # /bildirimler metni kademeleri yazar
        from app.telegram.bot import TelegramBot
        tb = TelegramBot(cfg, None, None, {})
        sent = []

        async def fake_send(text, chat_id=None, reply_markup=None):
            sent.append(text)
            return True
        tb.send = fake_send
        await tb._cmd_notifications("111")
        assert "normal hisse ≥ <b>$8.0M</b> · büyük hisse yok · endeks yok · tek işlem ≥ $5.0M (sicilli $250K) · OI birikimi ≥ $5.0M" in sent[-1], sent[-1]
        print("✅ yeni poz) $8.5M skor 10 → bildirim; $6M yok; NVDA/SP500 yok; mm yok; ayar/env/README/collector; /bildirimler kademeleri")
    asyncio.run(run())
