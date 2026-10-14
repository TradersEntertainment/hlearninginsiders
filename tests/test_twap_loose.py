"""🔸 TWAP yan kanalı (TWAP_LOOSE_CHAT_ID) — eşikleri biraz gevşetilmiş TWAP bildirimleri.

Kullanıcı (14.10): "TWAP bildirimleriyle ilgili şartları biraz gevşettiğimiz bir yan kanal da açalım; chat id'yi
değişken olarak ekleyeyim".

Pinlenenler:
  • gevşek ayar = ana ayarın canlı görünümü; her eşik min(yan, ana); ana kapıda kapalı (0) kural yan kanalda da kapalı;
    Cumartesi yüzdesi 0 yazılsa da Cumartesi tabanı kapanmaz (yan kanal ana kanaldan sıkı olamaz)
  • tanımsızken hiçbir şey değişmez (yan kanal mesajı yok, ön süzgeç aynı)
  • ana kanala giden her TWAP mesajı (ilk dilim / eski yol ilk alarm, ⛔) yan kanala BİREBİR kopya; TWAP_LOOSE_ONLY_EXTRA=1
    → kopya yok
  • ana kapıdan kalıp gevşek kapıyı geçen emir yalnız yan kanala, "🔸 Yan kanal" başlığıyla (ana eşik + bu emrin
    ölçüsü, sayıyla); ana kanal / ayna / 'twap' bekleme / alerted_ts / twap_o DEĞİŞMEZ; aynı emir bir kez
  • yalnız-yan-kanal emrinin ⛔ iptal notu yan kanala (HL emri sorulur); emir sonradan ana kanala da giderse notu
    ana yol yazar ve kopyası tek gelir; yeniden başlatmada kayıt geri yüklenir
  • ilk dilim ön süzgeci gevşek eşikle bakar; yalnız-gevşek adaylar ana adaylardan SONRA ve tavanın payıyla sorulur
  • bekleme: ana kanal bu adres+coin+yönü 6 sa içinde bildirdiyse yan kanal ek alarm yazmaz
  • yan kanal botun kendi sohbeti (/twap, /takip_N orada çalışır); /tani satırı
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-twaploose.db")
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.notify import KINDS, PUBLIC_KINDS, Notifier  # noqa: E402
from app.radar import twapfast as tf  # noqa: E402
from app.radar import twaplive as tl  # noqa: E402
from app.radar import twaploose as lo  # noqa: E402
from app.telegram import format as fmt  # noqa: E402
from test_twapfast import A, DV, PX, Bot, Col, _cfg, _drain, _fresh, _kind, twap_order  # noqa: E402

LOOSE = "-300"


def _lcfg(chat=LOOSE, mirror="-200"):
    cfg = _cfg(mirror=mirror)
    cfg.twap_loose_chat_id = chat
    cfg.twap_loose_only_extra = False
    return cfg


async def _fresh_l(cfg):
    await _fresh(cfg)
    lo.reset()


def _chats(bot):
    return [c for c, _ in bot.sent]


def test_loose_cfg_view():
    cfg = Config()
    assert cfg.twap_loose_chat_id == "" and not lo.enabled(cfg), "varsayılan: kapalı"
    for env in ("twap_loose_chat_id", "twap_loose_min_usd", "twap_loose_vol_pct", "twap_loose_only_extra"):
        assert env not in EDITABLE_FIELDS, "env-only (kullanıcı ayar sayfası istemiyor)"
    assert KINDS["twap_loose"][0] == "notify_twap" and "twap_loose" not in PUBLIC_KINDS
    cfg.twap_alert_min_usd, cfg.twap_alert_vol_pct, cfg.twap_alert_vol_pct_major = 1_000_000, 5, 20
    cfg.twap_alert_vol_pct_sat, cfg.twap_fast_short_min_usd, cfg.twap_fast_rate_x = 10, 250_000, 2
    cfg.spot_twap_min_day_vol, cfg.twap_alert_min_left_usd = 1_000_000, 1_000_000
    lc = lo.loose_cfg(cfg)
    assert (lc.twap_alert_min_usd, lc.twap_alert_vol_pct, lc.twap_alert_vol_pct_major, lc.twap_alert_vol_pct_sat,
            lc.twap_fast_short_min_usd, lc.twap_fast_rate_x, lc.spot_twap_min_day_vol, lc.twap_alert_min_left_usd) \
        == (500_000, 2.5, 10, 5, 100_000, 1, 500_000, 500_000), "varsayılan: ana eşiklerin yarısı"
    assert lc.twap_fast_short_max_min == cfg.twap_fast_short_max_min and lc.twap_alert_cooldown == cfg.twap_alert_cooldown
    cfg.twap_alert_vol_pct = 1.5                                   # ana eşik ayar sayfasında düştü → yan kanal izler
    assert lc.twap_alert_vol_pct == 1.5, "yan kanal ana kanaldan sıkı olamaz"
    cfg.twap_alert_vol_pct = 0                                     # ana kapıda kural kapalı → yan kanalda da
    assert lc.twap_alert_vol_pct == 0
    cfg.twap_alert_vol_pct = 5
    cfg.twap_loose_vol_pct_sat = 0                                 # 0 Cumartesi tabanını kapatmaz
    assert lc.twap_alert_vol_pct_sat == 10
    cfg.twap_loose_vol_pct = -1                                    # geçersiz → ana
    assert lc.twap_alert_vol_pct == 5
    assert getattr(lc, "yok_boyle_alan", "d") == "d"
    try:
        lc.twap_alert_vol_pct = 1
        raise AssertionError("salt okunur olmalı")
    except AttributeError:
        pass
    cfg.twap_loose_chat_id = "-300"
    assert lo.enabled(cfg)
    cfg.twap_live_enabled = False
    assert not lo.enabled(cfg), "canlı radar kapalıysa yan kanal da"
    print("✅ gevşek ayar) canlı görünüm, min(yan, ana), 0 = kapalı kural, Cumartesi tabanı korunur, env-only")


def test_disabled_changes_nothing():
    """Yan kanal tanımsız: ilk dilim + eski yol bugünkü gibi; yan kanal mesajı / kaydı yok."""
    async def run():
        cfg = _cfg()
        await _fresh_l(cfg)
        now = dbm.now()
        started = now - 340
        col = Col(lambda: [twap_order(started, minutes=5, plan=150_000)])    # ⚡ $150K: ana eşik altı
        bot = Bot()
        notifier = Notifier(cfg, bot)
        tf.observe("PUMP", A, "buy", PX, 3_000_000, 15_000, started + 2, 1000)
        await _drain(cfg, notifier, col)
        assert bot.sent == [] and await _kind("twap_loose") == 0 and not lo.OPEN
        assert not tf.F.pending.get(("PUMP", A, "buy"), {}).get("loose")
        assert "loose" not in tf.F.recent[0], tf.F.recent[0]
        now2 = dbm.now()
        assert tf.prefilter(cfg, "BTC", 80_000, (2e9, now2), now2) == "pre_low"
        tf.observe("BTC", A, "buy", 60_000, 80_000 / 60_000, 80_000, now2, 1000)
        assert not tf.F.pending[("BTC", A, "buy")]["queued"], "ön süzgeç bugünkü gibi"
    asyncio.run(run())
    print("✅ tanımsız) yan kanal mesajı / kaydı yok; ön süzgeç aynı")


def test_fast_extra_and_cancel_note():
    """⚡ 5 dk $150K emir: ana kapı (⚡ $250K, %5) geçmez → yalnız yan kanal, 🔸 başlıklı; ana kanal / ayna boş; iptal → ⛔
    yalnız yan kanala, bir kez."""
    async def run():
        cfg = _lcfg()
        await _fresh_l(cfg)
        now = dbm.now()
        started = now - 200
        state = {"st": "activated"}
        col = Col(lambda: [twap_order(started, minutes=5, plan=150_000, status=state["st"])])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        tf.observe("PUMP", A, "buy", PX, 3_000_000, 15_000, started + 2, 1000)
        await _drain(cfg, notifier, col)
        assert _chats(bot) == [LOOSE], bot.sent
        text = bot.sent[0][1]
        assert text.startswith("🔸 <b>Yan kanal</b> · ana kanal eşiğinin altında: emir $150K (ana kanal ≥ $1.0M;"
                               " ⚡ hızlıda ≥ $250K)\n⏳ <b>TWAP başladı</b>"), text
        assert "⚡ <b>HIZLI · 5 dk</b>" in text and "🔔 iptal edilirse ⛔ buraya otomatik yazılır" in text
        assert await _kind("twap_fast") == 0 and await _kind("twap") == 0 and await _kind("twap_mirror") == 0
        assert await _kind("twap_lcd") == 1 and await _kind("twap_loose") == 1
        assert not tf.F.open, "ana yolun bitiş kaydı yok (ana kanala gitmedi)"
        okey = f"PUMP:{A}:buy:{started}"
        assert okey in lo.OPEN and lo.BY_KEY[("PUMP", A, "buy")] == okey
        assert tf.F.recent[0]["reason"] == "short_small" and tf.F.recent[0]["loose"] == "sent"
        assert "🔸 yan kanal: gönderildi" in tf.recent_line(tf.F.recent[0])
        # dilim sayacı (sıcak yol) + iptal → ⛔ yalnız yan kanala
        for i in range(1, 4):
            tf.observe("PUMP", A, "buy", PX * 1.02, 3_000_000, 15_000, started + 2 + 30 * i, 1000)
        r = lo.OPEN[okey]
        assert r["n"] == 4 and abs(r["px_last"] - PX * 1.02) < 1e-12
        assert await lo.lifecycle(cfg, notifier, col, now) == 0 and not r["ended"], "henüz sorgu zamanı değil"
        r["lookup_ts"] = now - 61
        calls = len(col.calls)
        assert await lo.lifecycle(cfg, notifier, col, now) == 0 and not r["ended"], "activated: bekle"
        assert len(col.calls) == calls and lo.STATS["end_cached"] == 1, "ilk dilim yolunun taze cevabı: yeniden sorulmaz"
        state["st"] = "terminated"
        assert await lo.lifecycle(cfg, notifier, col, now + 61) == 1 and r["ended"]
        assert len(col.calls) == calls + 1 and lo.STATS["end_lookups"] == 1
        assert _chats(bot) == [LOOSE, LOOSE] and "⛔ <b>TWAP iptal edildi</b> · ⚡ <b>HIZLI · 5 dk</b>" in bot.sent[1][1]
        assert "+2.0%" in bot.sent[1][1], bot.sent[1][1]
        r["ended"], r["lookup_ts"] = 0, 0
        assert await lo.lifecycle(cfg, notifier, col, now + 125) == 0 and len(bot.sent) == 2, "⛔ bir kez"
        # aynı emir yeniden sorulsa (yeniden başlatma) ikinci alarm yok
        tf.F.known.clear()
        tf.observe("PUMP", A, "buy", PX, 3_000_000, 15_000, now, 1000)
        state["st"] = "activated"
        await _drain(cfg, notifier, col)
        assert len(bot.sent) == 2, bot.sent
    asyncio.run(run())
    print("✅ ilk dilim) ana eşik altı ⚡ emir yalnız yan kanala (🔸 + sayılar); ⛔ yalnız yan kanala, bir kez")


def test_copy_of_main_and_only_extra():
    """Ana kapıyı geçen emir: ana + ayna + yan kanala BİREBİR kopya (🔸 yok); ⛔ notu da kopyalanır; yalnız-ek modunda kopya yok."""
    async def scenario(only_extra: bool):
        cfg = _lcfg()
        cfg.twap_loose_only_extra = only_extra
        await _fresh_l(cfg)
        now = dbm.now()
        started = now - 200
        state = {"st": "activated"}
        col = Col(lambda: [twap_order(started, minutes=10, plan=600_000, status=state["st"])])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        tf.observe("PUMP", A, "buy", PX, 12_000_000, 60_000, started + 2, 1000)
        await _drain(cfg, notifier, col)
        rec = tf.F.open[("PUMP", A, "buy")]
        state["st"] = "terminated"
        rec["last_ts"], rec["lookup_ts"] = now - 46, now - 31
        assert await tf.lifecycle(cfg, notifier, col, now) == 1
        return bot
    bot = asyncio.run(scenario(False))
    assert _chats(bot) == ["-100", "-200", LOOSE, "-100", "-200", LOOSE], bot.sent
    assert bot.sent[2][1] == bot.sent[0][1] and "🔸" not in bot.sent[2][1], "kopya birebir"
    assert "TWAP iptal edildi" in bot.sent[5][1] and bot.sent[5][1] == bot.sent[3][1]
    bot = asyncio.run(scenario(True))
    assert _chats(bot) == ["-100", "-200", "-100", "-200"], "TWAP_LOOSE_ONLY_EXTRA=1: kopya yok"

    async def same_chat():                                      # yan kanal = ayna grubu → ikinci kopya yok
        cfg = _lcfg(chat="-200")
        await _fresh_l(cfg)
        now = dbm.now()
        col = Col(lambda: [twap_order(now - 200, minutes=10, plan=600_000)])
        bot = Bot()
        tf.observe("PUMP", A, "buy", PX, 12_000_000, 60_000, now - 198, 1000)
        await _drain(cfg, Notifier(cfg, bot), col)
        return bot
    assert _chats(asyncio.run(same_chat())) == ["-100", "-200"]
    print("✅ kopya) ana kanala giden alarm ve ⛔ yan kanala birebir; yalnız-ek modunda yok; ayna = yan kanal → tek")


def _slow_feed(started, n=13):
    for i in range(n):
        tl.observe("PUMP", A, "buy", PX, 10_000_000, 50_000, started + 2 + 30 * i, 1)


def test_slow_extra_upgrade_and_end_copy():
    """Eski yol: 2 sa $700K emir (hacmin %3,5'i, ana tabanı $1M) → yalnız yan kanal, takip tuşuyla; sonraki turlarda
    tekrar yok; ana yolun turu bildirilmemiş kalır. Ana eşik düşünce emir ana kanala da gider: yan kanala ikinci kopya
    YOK; iptal notunu ana yol yazar, yan kanala kopyası bir kez (yan kanal ömrü kendi notunu yazmaz)."""
    async def run():
        cfg = _lcfg()
        cfg.twap_fast_enabled = False
        await _fresh_l(cfg)
        now = dbm.now()
        started = now - 400
        state = {"st": "activated"}
        col = Col(lambda: [twap_order(started, minutes=120, plan=700_000, ex_frac=0.2, status=state["st"])])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        _slow_feed(started)
        out = await tl.evaluate(cfg, notifier, None, col)
        assert out["order_small"] == 1 and out["alerted"] == 0 and _chats(bot) == [LOOSE], (out, bot.sent)
        text = bot.sent[0][1]
        assert text.startswith("🔸 <b>Yan kanal</b> · ana kanal eşiğinin altında: emir $700K (ana kanal ≥ $1.0M)\n"
                               "⏳ <b>TWAP</b>"), text
        assert "/takip_" in text and "🔔" not in text, "uzun emir: takip tuşu"
        run_ = tl.REG.runs[("PUMP", A, "buy")]
        assert run_.alerted_ts is None and await _kind("twap") == 0 and await _kind("twap_o") == 0
        d = [x for x in out["decisions"] if x["addr"] == A][0]
        assert d["reason"] == "order_small" and d["loose"] == "sent"
        assert "🔸 yan kanal: gönderildi" in tl.decision_line(d)
        okey = f"PUMP:{A}:buy:{started}"
        assert okey in lo.OPEN
        out = await tl.evaluate(cfg, notifier, None, col)
        assert len(bot.sent) == 1 and [x for x in out["decisions"] if x["addr"] == A][0]["loose"] == "dup"
        # ana eşik ayar sayfasında düştü → ana kanal + ayna; yan kanala ikinci kopya yok
        cfg.twap_alert_min_usd = 500_000
        cfg.twap_alert_vol_pct = 2
        tl._lookup_cache.clear()
        out = await tl.evaluate(cfg, notifier, None, col)
        assert out["alerted"] == 1 and _chats(bot) == [LOOSE, "-100", "-200"], bot.sent
        assert await lo.main_owns(okey)
        # iptal: yan kanal ömrü kendi notunu yazmaz; ana yol ⛔ yazar → yan kanala kopya bir kez
        state["st"] = "terminated"
        lo.OPEN[okey]["lookup_ts"] = 0
        assert await lo.lifecycle(cfg, notifier, col, now) == 0 and lo.OPEN[okey]["ended"]
        run_.lookup_ts = now - 700
        tl._lookup_cache.clear()
        await tl.evaluate(cfg, notifier, None, col)
        ends = [c for c, t in bot.sent if "TWAP iptal edildi" in t]
        assert ends == ["-100", "-200", LOOSE], bot.sent
    asyncio.run(run())
    print("✅ eski yol) ana eşik altı emir yalnız yan kanala (takip tuşlu), tekrar yok; ana kanala yükselince yan kanala"
          " ikinci kopya yok; ⛔ ana yoldan, yan kanala bir kez")


def test_cooldown_and_inflight_and_main_owned():
    async def run():
        cfg = _lcfg()
        await _fresh_l(cfg)
        now = dbm.now()
        notifier = Notifier(cfg, Bot())
        ckey = f"PUMP:{A}:buy"
        await dbm.alert_log("twap", ckey, "")                       # ana kanal bu adresi az önce bildirdi
        assert await lo._extra(cfg, notifier, f"{ckey}:{now}", ckey, "PUMP", None, "h") == "cooldown"
        tl.SENDING.add(f"{ckey}:{now + 1}")
        assert await lo._extra(cfg, notifier, f"{ckey}:{now + 1}", ckey, "PUMP", None, "h") == "inflight"
        tl.SENDING.clear()
        await dbm.alert_log("twap_o", f"{ckey}:{now + 2}", "")
        assert await lo._extra(cfg, notifier, f"{ckey}:{now + 2}", ckey, "PUMP", None, "h") == "main"
        cfg.twap_alert_cooldown = 0
        assert await lo._extra(cfg, notifier, f"{ckey}:{now + 2}", ckey, "PUMP", None, "h") == "main"
    asyncio.run(run())
    print("✅ bekleme) ana kanal 6 sa içinde bildirdiyse ek alarm yok; öteki yol gönderirken / ana kanala gitmişse yok")


def test_prefilter_and_dispatch_share():
    """BTC $80K dilim: ana ön süzgeç (≥ $400M → dilim ≥ $111K) eler, gevşek (%10 → $55K) geçirir → 'loose' sıraya;
    sorgu sırasında ana adaylar önce; yalnız-gevşek adaylar dakika tavanının payıyla."""
    async def run():
        cfg = _lcfg()
        await _fresh_l(cfg)
        now = dbm.now()
        assert tf.prefilter(cfg, "BTC", 80_000, (2e9, now), now) == "pre_low"
        assert tf.prefilter(lo.loose_cfg(cfg), "BTC", 80_000, (2e9, now), now) == ""
        tf.observe("BTC", A, "buy", 60_000, 80_000 / 60_000, 80_000, now, 1000)
        p = tf.F.pending[("BTC", A, "buy")]
        assert p["queued"] and p["loose"] and lo.STATS["pre_queued"] == 1 and tf.F.stats["pre_low"] == 0
        tf.observe("PUMP", A, "buy", PX, 3_000_000, 15_000, now, 1000)
        assert tf.F.pending[("PUMP", A, "buy")]["loose"] is False
        order = []
        col = Col([])

        async def fake_resolve(cfg_, client, notifier, collector, key, p_, vols):
            order.append(key[0])
        orig = tf.resolve
        tf.resolve = fake_resolve
        try:
            tasks: set = set()
            assert await tf.dispatch(cfg, None, None, col, now, tasks) == 2
            await asyncio.gather(*tasks)
            assert order == ["PUMP", "BTC"], "ana aday önce (BTC'nin önceliği yüksek olsa da)"
            # tavanın payı doldu → yalnız-gevşek aday bekler
            for k in tf.F.pending.values():
                k["queued"] = now
            tf.F.lookup_ts.clear()
            tf.F.lookup_ts.extend([now] * int(30 * lo.QUERY_SHARE))
            order.clear()
            tasks = set()
            await tf.dispatch(cfg, None, None, col, now, tasks)
            await asyncio.gather(*tasks)
            assert order == ["PUMP"] and lo.STATS["pre_share"] == 1, order
        finally:
            tf.resolve = orig
    asyncio.run(run())
    print("✅ ön süzgeç) gevşek eşikle sıraya; ana adaylar önce; yalnız-gevşekler tavanın payıyla")


def test_restart_and_diag_and_bot():
    async def run():
        cfg = _lcfg()
        await _fresh_l(cfg)
        now = dbm.now()
        order = tl.parse_twap_orders([twap_order(now - 100, minutes=30, plan=400_000)], "PUMP", "buy", PX, now)[0]
        okey = f"PUMP:{A}:buy:{now - 100}"
        lo._register(okey, "PUMP", A, "buy", order, now, now - 98, now - 8, 4, 40_000, 8_000_000, PX, PX)
        await lo.save_open()
        lo.reset()
        assert await lo.load_open(now) == 1 and okey in lo.OPEN and lo.OPEN[okey]["lookup_ts"] == 0
        assert lo.BY_KEY[("PUMP", A, "buy")] == okey
        lo.STATS["extra"] = 3
        await lo.save_stats(now)
        line = await lo.diag_line(cfg)
        assert line.startswith("🔸 twap yan kanalı: emir ≥ $500K · hacmin %2.5'i (BTC/ETH %10) · ⚡ ≥ $100K ve 1 kat"), line
        assert "✓3 ek alarm" in line and "1 açık emir" in line
        from app.telegram.bot import TelegramBot
        bot = TelegramBot(cfg, None, None, {})
        assert LOOSE in bot._own_chats()
        assert fmt.twap_loose_head(["a < b"]) == "🔸 <b>Yan kanal</b> · ana kanal eşiğinin altında: a &lt; b"
    asyncio.run(run())
    print("✅ yeniden başlatma / tanı / bot) kayıt geri yüklenir; /tani satırı; yan kanal botun kendi sohbeti")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
