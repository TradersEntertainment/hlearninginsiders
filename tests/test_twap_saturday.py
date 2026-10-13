"""📅 Cumartesi hisse tabanı — hafta sonu TWAP'ı son 24s yerine GEÇEN Cumartesi'nin hacmiyle.

Kullanıcı (13.10): "hafta sonu 24 saat baz alırsa yanlış olur, hafta sonu gelecek yüksek TWAP'ları kaçırırız — bunu
sadece hisseler için yapalım; geçen hafta Cumartesi'ye kıyasla %10'dan büyük TWAP; Pazar değişmeden kalabilir".
Ölçüm: Cumartesi sabahı hissede son 24s hacim, sonraki 24 saatin ~10 katı (Cuma ABD seansı pencerede).

Pinlenenler:
  • TSİ Cumartesi (Cuma 21:00 – Cmt 21:00 UTC); geçen Cumartesi penceresi TSİ 00:00–24:00; Pazar / hafta içi yok
  • yalnız hisse + endeks/emtia (HIP-3, kripto değil, spot değil); %0 = kapalı
  • taban HL saatlik mumlarından (pencere dışı mum sayılmaz), kv'de saklanır; okunamazsa son 24s (eski kural)
  • kapı: geçen Cumartesi hacminin %10'u (ayar); ön süzgeç taban yokken 24s'e göre elemez; hızlı / gizli TWAP hızı
    aynı tabana göre; mesajlar "geçen Cumartesi hacminin %X'i" der; /twap ve karar satırı da
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-twapsat.db")
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.hl import universe as uni  # noqa: E402
from app.radar import hiddentwap as ht  # noqa: E402
from app.radar import twapfast as tf  # noqa: E402
from app.radar import twaplive as tl  # noqa: E402
from app.telegram import format as fmt  # noqa: E402

A = "0x" + "a" * 40
PX = 10.0
U = lambda *a: int(datetime(*a, tzinfo=timezone.utc).timestamp())   # noqa: E731
SAT = U(2026, 10, 17, 9, 0)            # Cumartesi 12:00 TSİ
PREV = (U(2026, 10, 9, 21, 0), U(2026, 10, 10, 21, 0))   # geçen Cumartesi TSİ 00:00–24:00


class Client:
    """candles(coin, '1h', başlangıç ms, bitiş ms): pencerede saatte $per_h; pencere dışına bir mum daha."""

    def __init__(self, per_h=1000.0, fail=False):
        self.per_h, self.fail, self.calls = per_h, fail, []

    async def candles(self, coin, interval, start_ms, end_ms):
        self.calls.append((coin, interval, start_ms // 1000, end_ms // 1000))
        if self.fail:
            raise RuntimeError("HL 500")
        s = start_ms // 1000
        rows = [{"t": (s + h * 3600) * 1000, "v": str(self.per_h / PX), "o": str(PX), "c": str(PX)} for h in range(24)]
        rows.append({"t": (s - 3600) * 1000, "v": "999999", "o": "10", "c": "10"})   # pencere dışı
        return rows


def _cfg():
    cfg = Config()
    cfg.telegram_chat_id, cfg.crypto_chat_id = "111", "-100"
    cfg.twap_mirror_chat_id = cfg.hidden_twap_chat_id = ""
    cfg.twap_alert_vol_pct, cfg.twap_alert_vol_pct_sat = 5, 10
    cfg.twap_alert_min_usd, cfg.twap_alert_min_left_usd, cfg.twap_alert_big_usd = 1_000_000, 1_000_000, 0
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    cfg.twap_follow_enabled = False
    return cfg


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "sat.db"))
    tl.SAT_CACHE.clear()
    tl._SAT_TRY.clear()
    tl._SAT_LOADED[0] = False
    tl.REG.clear()
    tl._lookup_cache.clear()
    tl._LIVE_Q.clear()
    tl.VOL_CACHE.update(ts=0, v={})
    tf.F.clear()
    ht.reset()


def order(coin, ntl=2_000_000, start_ago=1200, minutes=300, status="activated"):
    t0 = dbm.now() - start_ago
    return {"time": t0, "twapId": 1, "status": {"status": status},
            "state": {"coin": coin, "user": A, "side": "B", "sz": str(ntl / PX), "executedSz": str(ntl / PX / 4),
                      "executedNtl": str(ntl / 4), "minutes": minutes, "timestamp": t0 * 1000,
                      "reduceOnly": False, "randomize": False}}


def test_window_and_scope():
    assert tl.sat_window(SAT) == PREV
    assert tl.sat_window(U(2026, 10, 16, 21, 30)) == PREV, "Cuma 21:30 UTC = Cumartesi 00:30 TSİ"
    assert tl.sat_window(U(2026, 10, 16, 20, 30)) is None, "Cuma 23:30 TSİ"
    assert tl.sat_window(U(2026, 10, 17, 21, 30)) is None, "Pazar 00:30 TSİ: değişmez"
    assert tl.sat_window(U(2026, 10, 14, 9, 0)) is None
    cfg = _cfg()
    for coin, want in (("xyz:TSLA", True), ("xyz:GOLD", True), ("xyz:XYZ100", True), ("BTC", False),
                       ("PUMP", False), ("para:ANSEM", False), ("@107", False)):
        assert tl.sat_applies(cfg, coin, SAT) is want, coin
    cfg.twap_alert_vol_pct_sat = 0
    assert not tl.sat_applies(cfg, "xyz:TSLA", SAT), "%0 = kapalı"
    c = Config()
    assert c.twap_alert_vol_pct_sat == 10 and EDITABLE_FIELDS["twap_alert_vol_pct_sat"]["group"] == "TWAP radarı"
    print("✅ pencere/kapsam) TSİ Cumartesi, geçen Cmt 00–24 TSİ; Pazar ve hafta içi yok; hisse+endeks, kripto/spot yok")


def test_base_fetch_cache_and_gate():
    async def run():
        await _fresh()
        cfg = _cfg()
        cl = Client(per_h=1_000_000 / 24 * 10)                       # geçen Cumartesi toplam $10M
        assert await tl.ensure_sat(cfg, cl, ["xyz:TSLA", "BTC", "xyz:TSLA"], SAT) == 1
        assert cl.calls == [("xyz:TSLA", "1h", PREV[0], PREV[1] - 1)], cl.calls
        dv, vts, sat = tl.eff_vol(cfg, "xyz:TSLA", 100e6, SAT - 60, SAT)
        assert sat and abs(dv - 10e6) < 1 and vts == SAT, "pencere dışı mum sayılmaz"
        assert tl.eff_vol(cfg, "BTC", 5e9, SAT - 60, SAT) == (5e9, SAT - 60, False)
        assert tl.eff_vol(cfg, "xyz:TSLA", 100e6, SAT - 60, SAT + 86400)[2] is False, "Pazar: son 24s"
        # kapı: $2M emir · son 24s $100M (%2) · geçen Cmt $10M (%20 ≥ %10) → geçer
        o = tl.parse_twap_orders([order("xyz:TSLA")], "xyz:TSLA", "buy", PX, dbm.now())[0]
        det = tl.gate_detail(cfg, "xyz:TSLA", o, 100e6, SAT - 60, SAT)
        assert det["reason"] == "ok" and det["sat"] and abs(det["vol_pct"] - 20) < 1e-6 and det["pct_needed"] == 10
        assert det["day_vol_24h"] == 100e6 and abs(det["day_vol"] - 10e6) < 1
        tl.SAT_CACHE["xyz:TSLA"] = (PREV[0], 25e6)                    # %8 < %10
        det = tl.gate_detail(cfg, "xyz:TSLA", o, 100e6, SAT - 60, SAT)
        assert det["reason"] == "vol_small" and det["pct_needed"] == 10
        det = tl.gate_detail(cfg, "xyz:TSLA", o, 100e6, SAT + 86400 - 60, SAT + 86400)
        assert det["reason"] == "vol_small" and not det["sat"] and det["pct_needed"] == 5, "Pazar: eski kural"
        # kv'de saklanır: restart sonrası istek yok
        tl.SAT_CACHE.clear()
        tl._SAT_LOADED[0] = False
        cl2 = Client()
        assert await tl.ensure_sat(cfg, cl2, ["xyz:TSLA"], SAT) == 0 and not cl2.calls
        assert abs(tl.eff_vol(cfg, "xyz:TSLA", 1, SAT, SAT)[0] - 10e6) < 1
        # okunamazsa son 24s (eski kural), 10 dk sonra yeniden denenir
        await _fresh()
        bad = Client(fail=True)
        assert await tl.ensure_sat(cfg, bad, ["xyz:NVDA"], SAT) == 0
        assert tl.eff_vol(cfg, "xyz:NVDA", 100e6, SAT - 60, SAT) == (100e6, SAT - 60, False)
        assert await tl.ensure_sat(cfg, bad, ["xyz:NVDA"], SAT + 60) == 0 and len(bad.calls) == 1
        await tl.ensure_sat(cfg, bad, ["xyz:NVDA"], SAT + tl.SAT_RETRY) and None
        assert len(bad.calls) == 2
        print("✅ taban/kapı) mumdan $ (pencere dışı yok), kv'de kalıcı, okunamazsa 24s; %20 geçer, %8 geçmez (eşik"
              " %10); Pazar ve kripto eski kural")
    asyncio.run(run())


def test_prefilter_fast_hidden_and_text():
    async def run():
        await _fresh()
        cfg = _cfg()
        # ön süzgeç: Cumartesi hissede taban yokken son 24s'e göre ELEMEZ (sor)
        assert tf.prefilter(cfg, "xyz:TSLA", 1, (50e9, SAT - 60), SAT) == ""
        assert tf.prefilter(cfg, "xyz:TSLA", 1, (50e9, SAT + 86340), SAT + 86400) == "pre_low", "Pazar: eski kural"
        tl.SAT_CACHE["xyz:TSLA"] = (PREV[0], 10e6)
        assert tf.prefilter(cfg, "xyz:TSLA", 400, (50e9, SAT - 60), SAT) == "", "taban $10M: $400 dilim sorulur"
        assert tf.prefilter(cfg, "BTC", 400, (50e9, SAT - 60), SAT) == "pre_low"
        # hızlı yol kapısı: dakika hızı da geçen Cumartesi'ye göre
        o = tl.parse_twap_orders([order("xyz:TSLA", ntl=600_000, minutes=30)], "xyz:TSLA", "buy", PX, dbm.now())[0]
        g, det = tf.decide_gate(cfg, "xyz:TSLA", o, 100e6, SAT - 60, SAT)
        assert det["sat"] and abs(det["rate_x"] - 20_000 / (10e6 / 1440)) < 1e-6 and g == "short", (g, det)
        g2, det2 = tf.decide_gate(cfg, "xyz:TSLA", o, 100e6, SAT + 86340, SAT + 86400)
        assert not det2["sat"] and g2 != "short", "Pazar: son 24s ($100M) → hız 0.3×"
        # gizli TWAP hızı: aynı taban
        assert ht.rate_x({"rate_day": 2e6}, tl.eff_vol(cfg, "xyz:TSLA", 100e6, SAT, SAT)[0]) == 0.2
        # metinler
        m = {"coin": "xyz:TSLA", "address": A, "side": "buy", "n": 20, "avg_slice": 2000, "median_gap": 30,
             "taker_pct": 100.0, "total": 4e4, "dur": 600, "rate_day": 2e6, "px_first": PX, "px_last": PX,
             "px_chg_pct": 0.0}
        t = fmt.twap_alert(m, {"order": o, "vol_pct": 20.0, "day_vol": 10e6, "sat": True, "klass": "hisse",
                               "pos": {"none": True}})
        assert "🐘 emir geçen Cumartesi hacminin <b>%20</b>'i" in t and "geçen Cumartesi hacmi $10.0M" in t, t
        assert "24s hacmin" not in t
        t = fmt.twap_fast(o, {"coin": "xyz:TSLA", "address": A, "side": "buy", "gate": "short", "day_vol": 10e6,
                              "sat": True, "vol_pct": 6.0, "per_min": 20_000, "rate_x": 2.9, "pos": {"none": True},
                              "klass": "hisse", "now": dbm.now(), "quick_max": 60})
        assert "coinin geçen Cumartesi ortalama dakika hacminin <b>2.9 katı</b>" in t and \
               "emir geçen Cumartesi hacminin %6.0'i" in t and "geçen Cumartesi hacmi $10.0M" in t, t
        t = fmt.hidden_twap(m, {"rate_x": 2.0, "day_vol": 10e6, "sat": True, "pos": {"none": True}, "klass": "hisse"})
        assert "geçen Cumartesi'nin ortalama dakika hacminin <b>2.0×</b>'i" in t and "geçen Cumartesi hacmi" in t, t
        row = {"coin": "xyz:TSLA", "addr": A, "side": "buy", "planned_usd": 2e6, "filled_pct": 25.0, "minutes": 300,
               "left_sec": 3600, "q_ts": dbm.now(), "alerted": False, "vol_pct": 20.0, "spot": False, "sat": True}
        assert "(geçen Cmt hacminin %20.0'i)" in fmt.live_twaps([row])
        assert "taban: geçen Cumartesi hacmi" in tl.decision_line(
            {"coin": "xyz:TSLA", "addr": A, "side": "buy", "planned": 2e6, "vol_pct": 8.0, "pct_needed": 10,
             "reason": "vol_small", "sat": True})
        print("✅ süzgeç/hızlı/gizli/metin) taban yokken elemez; hız aynı tabana göre; mesaj, /twap ve karar satırı"
              " 'geçen Cumartesi' der; Pazar eski kural")
    asyncio.run(run())


def test_evaluate_saturday_equity_alert():
    """Uçtan uca: evaluate Cumartesi (pencere sabitlenir) — son 24s'e göre %2 olan emir geçen Cmt'ye göre %20 → kanal."""
    async def run():
        from app.notify import Notifier
        await _fresh()
        cfg = _cfg()
        await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {}, "ts": dbm.now()})
        async with dbm.db() as c:
            await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES('xyz:TSLA','xyz','TSLA')")
            await c.execute("INSERT INTO asset_metrics(coin,ts,day_volume,mark_px) VALUES('xyz:TSLA',?,100e6,10)",
                            (dbm.now(),))
            await c.commit()
        real = tl.sat_window
        now = dbm.now()
        tl.sat_window = lambda ts: PREV                               # "bugün Cumartesi"
        try:
            for i in range(30):
                tl.observe("xyz:TSLA", A, "buy", PX, 2000 / PX, 2000.0, now - 5 - (29 - i) * 30, 1, True)

            class Bot:
                def __init__(self):
                    self.sent = []

                async def send(self, text, chat_id=None, **k):
                    self.sent.append((chat_id, text))
                    return True

            class Col:
                twap_sample = None

                async def fetch_twap_history(self, addr, timeout=6.0):
                    return [order("xyz:TSLA")]
            bot = Bot()
            cl = Client(per_h=10e6 / 24)
            await tl.evaluate(cfg, Notifier(cfg, bot), cl, Col())
            assert cl.calls and len(bot.sent) == 1, bot.sent
            assert "🐘 emir geçen Cumartesi hacminin <b>%20</b>'i" in bot.sent[0][1], bot.sent[0][1]
            d = (await dbm.kv_get(tl.LAST_KV))[0]
            assert d["reason"] == "alerted" and d["sat"] is True
            rows, _ = await tl.live_twaps_full(None, cfg=cfg)
            assert rows and rows[0]["sat"] and abs(rows[0]["vol_pct"] - 20) < 1e-6, rows
        finally:
            tl.sat_window = real
        print("✅ uçtan uca) Cumartesi hisse: geçen Cmt tabanıyla kanal + /twap; mum yalnız aday coin için")
    asyncio.run(run())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
