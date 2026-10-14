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
    tl._LIVE_Q.clear()
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
        assert okey in lo.OPEN and lo.BY_KEY[("PUMP", A, "buy")] == [okey]
        assert tf.F.recent[0]["reason"] == "short_small" and tf.F.recent[0]["loose"] == "sent"
        assert "🔸 yan kanal: gönderildi" in tf.recent_line(tf.F.recent[0])
        # dilim sayacı (sıcak yol) + iptal → ⛔ yalnız yan kanala
        for i in range(1, 4):
            lo.observe(("PUMP", A, "buy"), PX * 1.02, 3_000_000, 15_000, started + 2 + 30 * i)   # collector kancası
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
        assert p["queued"] and p["loose"] and lo.STATS["pre_queued"] == 1 and tf.F.stats["pre_low"] == 1 \
            and tf.F.stats["queued"] == 0, "ana sayaçlar bugünkü gibi"
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
        assert lo.BY_KEY[("PUMP", A, "buy")] == [okey]
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


BTC_DV = 2e9


async def _btc(cfg):
    await _fresh_l(cfg)
    from app.hl import universe as uni
    await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"PUMP": {"m": PX, "oi": 1, "f": 0, "v": DV, "p": None},
                                             "BTC": {"m": 60_000, "oi": 1, "f": 0, "v": BTC_DV, "p": None}},
                                       "ts": dbm.now()})
    tf.F.vols = await tl.volumes(None)


def btc_order(started, minutes, plan, status="activated"):
    o = twap_order(started, minutes=minutes, plan=plan, status=status, coin="BTC")
    sz = plan / 60_000
    o["state"].update(sz=str(sz), executedSz=str(sz * 0.05), executedNtl=str(sz * 0.05 * 60_000))
    return o


def test_loose_lookup_keeps_main_state():
    """İnceleme (14.10): yalnız gevşek ön süzgeçle sorulan emir ana yolun F.known'unu doldurup aynı adresin sonraki
    ⚡ emrini ana kanalda kaçırtıyordu; 🧪 LOG-TWAPF ve ana sayaçlar da değişiyordu. Artık: KNOWN / sayaç yan kanalda,
    ana yol bugünkü gibi. 24 sa'ten uzun emir ana kapıyı geçerse ilk dilim yolu dokunmaz (eski yol yazar)."""
    async def run():
        from test_twapfast import _q
        cfg = _lcfg()
        await _btc(cfg)
        now = dbm.now()
        t1 = now - 100
        hist = {"h": [btc_order(t1, 30, 4_900_000)]}
        col = Col(lambda: hist["h"])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        tf.observe("BTC", A, "buy", 60_000, 80_000 / 60_000, 80_000, t1 + 2, 1000)
        assert tf.F.pending[("BTC", A, "buy")]["loose"]
        before = dict(tf.F.stats)
        await _drain(cfg, notifier, col)
        assert ("BTC", A, "buy") not in tf.F.known and ("BTC", A, "buy") in lo.KNOWN, "ana bastırma yok"
        assert not _q("LOG-TWAPF") and len(tf.F.recent) == 0, "🧪 ve ana karar listesi değişmez"
        assert {k: v for k, v in tf.F.stats.items() if v != before.get(k)} == {}, tf.F.stats
        assert lo.STATS["lookups"] == 1 and lo.RECENT[0]["reason"] == "slow", list(lo.RECENT)
        # aynı adresin yeni ⚡ emri (5 dk $50M): ilk dilim ana yoldan sorulur ve ana kanala gider
        t2 = now - 10
        hist["h"] = [btc_order(t2, 5, 50_000_000), btc_order(t1, 30, 4_900_000)]
        tf.observe("BTC", A, "buy", 60_000, 4_500_000 / 60_000, 4_500_000, t2 + 2, 1000)
        p = tf.F.pending[("BTC", A, "buy")]
        assert p["queued"] and not p["loose"]
        await _drain(cfg, notifier, col)
        assert _chats(bot)[:2] == ["-100", "-200"] and tf.F.stats["known_skip"] == 0, bot.sent
        # yalnız-gevşek dilim aynı (yan kanalca bilinen) emirden: yeniden sorulmaz, ana sayaç bugünkü gibi pre_low
        n_pre = tf.F.stats["pre_low"]
        tf.F.known.clear()
        tf.observe("BTC", A, "buy", 60_000, 80_000 / 60_000, 80_000, now, 1000)
        assert not tf.F.pending[("BTC", A, "buy")]["queued"] and tf.F.stats["pre_low"] == n_pre + 1
        assert lo.STATS["known_skip"] == 1
        # 48 sa'lik $500M emir: ön süzgeç öngöremez, ana kapı geçer → ilk dilim yolu dokunmaz (eski yol yazar)
        lo.KNOWN.clear()
        tf.F.pending.clear()
        t3 = now - 50
        hist["h"] = [btc_order(t3, 2880, 500_000_000)]
        n = len(bot.sent)
        tf.observe("BTC", A, "buy", 60_000, 80_000 / 60_000, 80_000, now + 1, 1000)
        assert tf.F.pending[("BTC", A, "buy")]["loose"]
        await _drain(cfg, notifier, col)
        assert len(bot.sent) == n and lo.STATS["strict_later"] == 1 and await _kind("twap_loose") <= 1
    asyncio.run(run())
    print("✅ inceleme) yalnız-gevşek sorgu ana yolun F.known / 🧪 / sayaçlarına dokunmaz; aynı adresin ⚡ emri ana"
          " kanala gider; ana kapılık uzun emir eski yola kalır")


def test_flag_upgrade_and_concurrency_slot():
    """İnceleme: aynı saniyenin ikinci parçası dilimi ana ön süzgeç üstüne çıkarırsa aday ana sıraya geçer; yalnız-
    gevşek adaylar eşzamanlı yerlerin hepsini tutamaz (ana yola bir yer kalır)."""
    async def run():
        cfg = _lcfg()
        await _btc(cfg)
        now = dbm.now()
        tf.observe("BTC", A, "buy", 60_000, 1.0, 60_000, now, 1000)
        p = tf.F.pending[("BTC", A, "buy")]
        assert p["queued"] and p["loose"]
        tf.observe("BTC", A, "buy", 60_000, 1.0, 60_000, now, 1000)            # aynı dilim: $120K ≥ $111K
        assert p["loose"] is False and tf.F.stats["queued"] == 1
        tf.F.pending.clear()
        started = []
        gate = asyncio.Event()

        async def slow_resolve(cfg_, client, notifier, collector, key, p_, vols):
            started.append((key[1][:4], bool(p_.get("loose"))))
            await gate.wait()
        orig = tf.resolve
        tf.resolve = slow_resolve
        try:
            for i, ch in enumerate("bcd"):
                tf.observe("BTC", "0x" + ch * 40, "buy", 60_000, 1.0, 60_000, now, 1000)
            tasks: set = set()
            assert await tf.dispatch(cfg, None, None, Col([]), now, tasks) == 2, "yalnız-gevşek: en çok CONCURRENCY-1"
            tf.observe("PUMP", A, "buy", PX, 3_000_000, 15_000, now, 1000)       # ana aday gelir
            assert await tf.dispatch(cfg, None, None, Col([]), now, tasks) == 1
            await asyncio.sleep(0)
            assert len(started) == 3 and started[-1] == ("0xaa", False), started
            gate.set()
            await asyncio.gather(*tasks)
        finally:
            tf.resolve = orig
    asyncio.run(run())
    print("✅ inceleme) büyüyen dilim ana sıraya geçer; ana yola bir eşzamanlı yer kalır")


def test_no_chat_and_orphan_notes():
    """Ana hedef + ayna tanımsız: ana eşiği geçen emir eski yoldan da yan kanala ('ana eşiği geçti' başlığıyla; 'eşiğin
    altında' denmez), iptali bir kez. Yan kanalda duyurulmamış emrin ⛔ kopyası yazılmaz."""
    async def run():
        cfg = _lcfg(mirror="")
        cfg.crypto_chat_id = ""
        cfg.twap_fast_enabled = False
        await _fresh_l(cfg)
        now = dbm.now()
        started = now - 400
        state = {"st": "activated"}
        col = Col(lambda: [twap_order(started, minutes=120, plan=3_000_000, ex_frac=0.2, status=state["st"])])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        _slow_feed(started)
        out = await tl.evaluate(cfg, notifier, None, col)
        assert out["no_chat"] == 1 and _chats(bot) == [LOOSE], (out, bot.sent)
        assert bot.sent[0][1].startswith("🔸 <b>Yan kanal</b> · ana eşiği geçti; ana kanal tanımlı değil\n"), bot.sent
        assert "altında" not in bot.sent[0][1].split("\n")[0]
        state["st"] = "terminated"
        run_ = tl.REG.runs[("PUMP", A, "buy")]
        run_.lookup_ts = now - 700
        tl._lookup_cache.clear()
        await tl.evaluate(cfg, notifier, None, col)
        okey = f"PUMP:{A}:buy:{started}"
        lo.OPEN[okey]["lookup_ts"] = 0
        await lo.lifecycle(cfg, notifier, col, now + 400)
        assert [c for c, t in bot.sent if "TWAP iptal edildi" in t] == [LOOSE], bot.sent
        # ilk dilim yolu: aynı başlık
        lo.reset()
        head = fmt.twap_loose_head([], no_chat=True)
        assert "altında" not in head
        # öksüz not: ana kanal emri yan kanal tanımlanmadan ÖNCE bildirdi → iptal kopyası yan kanala gitmez
        cfg2 = _cfg()
        await _fresh_l(cfg2)
        now = dbm.now()
        started = now - 200
        st2 = {"st": "activated"}
        col2 = Col(lambda: [twap_order(started, minutes=10, plan=600_000, status=st2["st"])])
        bot2 = Bot()
        n2 = Notifier(cfg2, bot2)
        tf.observe("PUMP", A, "buy", PX, 12_000_000, 60_000, started + 2, 1000)
        await _drain(cfg2, n2, col2)
        cfg2.twap_loose_chat_id = LOOSE                                  # yan kanal sonradan tanımlandı
        rec = tf.F.open[("PUMP", A, "buy")]
        st2["st"] = "terminated"
        rec["last_ts"], rec["lookup_ts"] = now - 46, now - 31
        assert await tf.lifecycle(cfg2, n2, col2, now) == 1
        assert _chats(bot2) == ["-100", "-200", "-100", "-200"] and lo.STATS["orphan_skip"] == 1, bot2.sent
    asyncio.run(run())
    print("✅ inceleme) ana hedef tanımsız: eski yoldan da yan kanala, doğru başlıkla, ⛔ bir kez; duyurulmamış emrin"
          " ⛔ kopyası yok")


def test_collector_feeds_counter_and_two_orders():
    """İnceleme: ilk dilim yolu kapalıyken de collector sayacı besler (yoksa uzun emir 30 sn'de bir sorulurdu, ⛔
    fiyatı donardı); aynı adres + coin + yönde iki emir: dolum başlangıcı önce olan en yeni emre."""
    async def run():
        import json
        from app.hl.collector import Collector
        from test_twapfast import ZERO
        cfg = _lcfg()
        cfg.twap_fast_enabled = False
        await _fresh_l(cfg)
        now = dbm.now()
        o1 = tl.parse_twap_orders([twap_order(now - 3000, minutes=240, plan=900_000)], "PUMP", "buy", PX, now)[0]
        o2 = tl.parse_twap_orders([twap_order(now - 60, minutes=30, plan=700_000)], "PUMP", "buy", PX, now)[0]
        k1, k2 = f"PUMP:{A}:buy:{now - 3000}", f"PUMP:{A}:buy:{now - 60}"
        lo._register(k1, "PUMP", A, "buy", o1, now - 2900, now - 2998, now - 2900, 4, 40_000, 8e6, PX, PX)
        lo._register(k2, "PUMP", A, "buy", o2, now - 50, now - 58, now - 50, 1, 10_000, 2e6, PX, PX)
        assert lo.BY_KEY[("PUMP", A, "buy")] == [k1, k2]
        col = Collector(cfg, None)
        col.valid_coins = col.crypto_coins = {"PUMP"}
        data = [{"coin": "PUMP", "side": "B", "px": str(PX * 1.1), "sz": "2000000", "time": now * 1000,
                 "tid": "z1", "hash": ZERO, "users": [A, "0x" + "e" * 40]}]
        await col._handle(json.dumps({"channel": "trades", "data": data}))
        assert lo.OPEN[k2]["n"] == 2 and abs(lo.OPEN[k2]["px_last"] - PX * 1.1) < 1e-12 and lo.OPEN[k2]["last_ts"] == now
        assert lo.OPEN[k1]["n"] == 4, "eski emir yenisinin dolumunu almaz"
        lo.observe(("PUMP", A, "buy"), PX, 1, 5, now - 100)                    # yenisi başlamadan önceki dolum → eskisi
        assert lo.OPEN[k1]["n"] == 5 and lo.OPEN[k2]["n"] == 2
        lo.OPEN[k2]["ended"] = now - 700
        await lo.lifecycle(cfg, Notifier(cfg, Bot()), Col([]), now)
        assert k2 not in lo.OPEN and lo.BY_KEY[("PUMP", A, "buy")] == [k1]
    asyncio.run(run())
    print("✅ inceleme) collector sayacı ilk dilim yolu kapalıyken de besler; aynı anahtarda iki emir ayrı sayılır")


def test_review2_retry_counters_notify_alias():
    """2. inceleme: (a) ana hedef tanımsızken yan kanala gidemeyen alarm geri çekilmeden sonra yeniden denenir;
    (b) yalnız-gevşek adayın sırada eskimesi ana sayacı değiştirmez, ana sıraya geçen aday sıra süresini baştan alır;
    (c) notify_twap kapalıyken yan kanal ömrü donmaz, geç ⛔ yazılmaz; (d) emir bitişte HL geçmişinden düşse de yan
    kanalda duyurulmuş emrin 🏁 kopyası gider (tur takma adı)."""
    async def run():
        # (a)
        cfg = _lcfg(mirror="")
        cfg.crypto_chat_id = ""
        cfg.twap_fast_enabled = False
        await _fresh_l(cfg)
        now = dbm.now()
        started = now - 400
        col = Col(lambda: [twap_order(started, minutes=120, plan=3_000_000, ex_frac=0.2)])
        bot = Bot(fail_chats=(LOOSE,))
        notifier = Notifier(cfg, bot)
        _slow_feed(started)
        out = await tl.evaluate(cfg, notifier, None, col)
        d = [x for x in out["decisions"] if x["addr"] == A][0]
        assert d["loose"] == "failed" and tl.REG.runs[("PUMP", A, "buy")].alerted_ts is None
        out = await tl.evaluate(cfg, notifier, None, col)
        assert [x for x in out["decisions"] if x["addr"] == A][0]["loose"] == "backoff"
        bot.fail.clear()
        lo.FAILED.clear()
        out = await tl.evaluate(cfg, notifier, None, col)
        assert [x for x in out["decisions"] if x["addr"] == A][0]["loose"] == "sent" and _chats(bot) == [LOOSE]
        assert tl.REG.runs[("PUMP", A, "buy")].alerted_ts is not None
        # (b)
        cfg = _lcfg()
        await _btc(cfg)
        now = dbm.now()
        tf.observe("BTC", A, "buy", 60_000, 1.0, 60_000, now, 1000)
        p = tf.F.pending[("BTC", A, "buy")]
        p["queued"] = now - 200
        await tf.dispatch(cfg, None, None, Col([]), now, set())
        assert tf.F.stats["dropped_old"] == 0 and lo.STATS["dropped_old"] == 1
        tf.observe("BTC", A, "buy", 60_000, 1.0, 60_000, now + 10, 1000)         # yeni dilim: yine yalnız-gevşek
        p = tf.F.pending[("BTC", A, "buy")]
        assert p["queued"] and p["loose"]
        p["queued"] = now - 140
        pre = tf.F.stats["pre_low"]
        tf.observe("BTC", A, "buy", 60_000, 0.5, 30_000, now + 40, 1000)         # sıradayken gelen küçük dilim
        assert tf.F.stats["pre_low"] == pre + 1, "ana sayaç dilim başına bir (bugünkü gibi)"
        tf.observe("BTC", A, "buy", 60_000, 1.5, 90_000, now + 40, 1000)         # aynı dilim $120K → ana aday
        assert p["loose"] is False and p["queued"] >= now, "sıra süresi baştan"
        # (c)
        cfg = _lcfg()
        await _fresh_l(cfg)
        now = dbm.now()
        order = tl.parse_twap_orders([twap_order(now - 100, minutes=5, plan=150_000)], "PUMP", "buy", PX, now)[0]
        okey = f"PUMP:{A}:buy:{now - 100}"
        lo._register(okey, "PUMP", A, "buy", order, now - 90, now - 98, now - 60, 2, 20_000, 4e6, PX, PX)
        cfg.notify_twap = False
        col = Col(lambda: [twap_order(now - 100, minutes=5, plan=150_000, status="terminated")])
        bot = Bot()
        lo.OPEN[okey]["lookup_ts"] = 0
        assert await lo.lifecycle(cfg, Notifier(cfg, bot), col, now) == 0
        assert lo.OPEN[okey]["ended"] and bot.sent == [], "kapalıyken not yok, kayıt kapanır"
        cfg.notify_twap = True
        assert await lo.lifecycle(cfg, Notifier(cfg, bot), col, now + 3600) == 0 and bot.sent == []
        # (d)
        cfg = _lcfg()
        cfg.twap_fast_enabled = False
        cfg.twap_alert_finish_note = True
        await _fresh_l(cfg)
        now = dbm.now()
        started = now - 400
        hist = {"h": [twap_order(started, minutes=120, plan=3_000_000, ex_frac=0.2)]}
        col = Col(lambda: hist["h"])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        _slow_feed(started)
        await tl.evaluate(cfg, notifier, None, col)
        assert _chats(bot) == ["-100", "-200", LOOSE]
        hist["h"] = []                                                       # emir geçmişten düştü
        run_ = tl.REG.runs[("PUMP", A, "buy")]
        run_.last_ts = now - 350                                             # sustu (budanmadan)
        run_.lookup_ts = now - 700
        tl._lookup_cache.clear()
        await tl.evaluate(cfg, notifier, None, col)
        ends = [c for c, t in bot.sent if "TWAP bitti" in t]
        assert ends == ["-100", "-200", LOOSE], bot.sent
        line = await lo.diag_line(cfg)
        assert "🔸 twap yan kanalı" in line
    asyncio.run(run())
    print("✅ 2. inceleme) ana hedef tanımsızken başarısız yan kanal alarmı yeniden denenir; yalnız-gevşek eskime ana"
          " sayaca yazılmaz; ana sıraya geçen aday taze; notify kapalıyken geç ⛔ yok; düşen emrin 🏁 kopyası gider")


def test_small_slices_not_delayed():
    """Kullanıcı 14.10: TRX 6,5 sa $564K emir (~$721 dilim, 24s hacim $1.9M) 🔸 kanala 36 dk geç geldi — ilk dilim yolu
    $1K altı dilimi sormuyordu, eski yol gözlenen $50K'yı bekliyordu (72 dilim). Artık: (a) ilk dilim tabanı $250
    (varsayılan) → ilk dilimde; (b) eski yol 10 dilim / 5 dk sonra, ortalama dilim kapıya yetebilecekse $50K
    beklemeden sorar. Küçük botlar (ortalama dilim kapıya yetmez) yine sorulmaz."""
    async def run():
        from app.hl import universe as uni
        # (b) eski yol
        cfg = _lcfg()
        cfg.twap_fast_enabled = False
        cfg.twap_fast_min_slice_usd = Config().twap_fast_min_slice_usd   # varsayılan $250 (erken sorgu da bu tabanla)
        await _fresh_l(cfg)
        PXT = 0.3333
        await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"TRX": {"m": PXT, "oi": 1, "f": 0, "v": 1.9e6, "p": None},
                                                 "PUMP": {"m": PX, "oi": 1, "f": 0, "v": DV, "p": None}},
                                           "ts": dbm.now()})
        tl.VOL_CACHE.update(ts=dbm.now(), v=await tl.volumes(None))
        now = dbm.now()
        started = now - 400
        sz = 1_690_000
        hist = [{"time": started * 1000, "twapId": 9, "status": {"status": "activated"},
                 "state": {"coin": "TRX", "user": A, "side": "B", "sz": str(sz), "executedSz": "0.0",
                           "executedNtl": "0.0", "minutes": 390, "timestamp": started * 1000, "randomize": False,
                           "reduceOnly": False}}]
        col = Col(lambda: hist)
        bot = Bot()
        notifier = Notifier(cfg, bot)
        for i in range(13):                                            # 6 dk, 13 × $721 = $9.4K (< $50K)
            tl.observe("TRX", A, "buy", PXT, 721 / PXT, 721, started + 2 + 30 * i, 1, True)
        tiny = "0x" + "9" * 40
        for i in range(13):                                            # küçük bot: $40 dilim → sorulmaz
            tl.observe("TRX", tiny, "buy", PXT, 40 / PXT, 40, started + 2 + 30 * i, 1, True)
        out = await tl.evaluate(cfg, notifier, None, col)
        assert col.calls == [A] and out.get("early") == 1, (col.calls, out.get("early"))
        assert [c for c, _ in bot.sent] == [LOOSE], bot.sent
        assert "emir $563K (ana kanal ≥ $1.0M)" in bot.sent[0][1], bot.sent[0][1]
        # (a) ilk dilim yolu: $721 dilim varsayılan tabanla ($250) sorulur
        cfg2 = _lcfg()
        assert Config().twap_fast_min_slice_usd == 250
        await _fresh_l(cfg2)
        await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"TRX": {"m": PXT, "oi": 1, "f": 0, "v": 1.9e6, "p": None}},
                                           "ts": dbm.now()})
        tf.F.vols = await tl.volumes(None)
        bot2 = Bot()
        tf.observe("TRX", A, "buy", PXT, 721 / PXT, 721, started + 2, Config().twap_fast_min_slice_usd)
        assert tf.F.pending[("TRX", A, "buy")]["queued"]
        await _drain(cfg2, Notifier(cfg2, bot2), Col(lambda: hist))
        assert [c for c, _ in bot2.sent] == [LOOSE], bot2.sent
    asyncio.run(run())
    print("✅ küçük dilim) TRX $721 dilimli $564K emir: ilk dilimde ($250 tabanı) ya da eski yolda 5 dk sonra ($50K"
          " beklemeden); küçük bot sorulmaz")


def test_second_order_same_key_not_blind():
    """Kullanıcı 14.10 (NEAR, 0x7491…d90f): 15:16:26 95K NEAR 5 dk (≈ $487K, kapı dışı) sürerken 15:18:59 150K NEAR
    5 dk (≈ $770K, 🔸 ⚡) — ikincisi 275 sn geç geldi: ilk emir okununca anahtar (coin + adres + yön) ilk emrin bitişi +
    2 dk'ya kadar susturuluyordu. Artık bilinen anahtarda dilim temposu tek TWAP'ı aşınca (65 sn'de ≥ 4 dilim) yeniden
    sorulur → ikinci emir ~1 dk içinde."""
    async def run():
        from app.hl import universe as uni
        cfg = _lcfg()
        await _fresh_l(cfg)
        PXN = 5.13
        await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"NEAR": {"m": PXN, "oi": 1, "f": 0, "v": 167e6, "p": None}},
                                           "ts": dbm.now()})
        tf.F.vols = await tl.volumes(None)
        now = dbm.now()
        t1, t2 = now - 300, now - 147                                  # ikinci emir ilkinden 153 sn sonra

        def hl(start, sz, status="activated"):
            return {"time": start * 1000, "status": {"status": status},
                    "state": {"coin": "NEAR", "user": A, "side": "B", "sz": str(sz), "executedSz": "0.0",
                              "executedNtl": "0.0", "minutes": 5, "timestamp": start * 1000, "randomize": False,
                              "reduceOnly": False}}
        hist = {"h": [hl(t1, 95_000)]}
        col = Col(lambda: hist["h"])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        s1 = 95_000 * PXN / 11                                         # ~$44K dilim (5 dk = 11 dilim)
        s2 = 150_000 * PXN / 11                                        # ~$70K
        tf.observe("NEAR", A, "buy", PXN, s1 / PXN, s1, t1 + 1, 250)
        await _drain(cfg, notifier, col, t1 + 2)
        assert bot.sent == [] and ("NEAR", A, "buy") in tf.F.known, "ilk emir kapı dışı, anahtar bastırıldı"
        hist["h"] = [hl(t2, 150_000), hl(t1, 95_000)]
        events = sorted([(t1 + 1 + 30 * i, s1) for i in range(1, 10)] + [(t2 + 1 + 30 * i, s2) for i in range(0, 5)])
        alerted_at = None
        for ts, usd in events:
            tf.observe("NEAR", A, "buy", PXN, usd / PXN, usd, ts, 250)
            if tf.F.pending.get(("NEAR", A, "buy"), {}).get("queued"):
                await _drain(cfg, notifier, col, ts)
                if bot.sent and alerted_at is None:
                    alerted_at = ts
        assert tf.F.stats["relook"] >= 1, tf.F.stats
        assert alerted_at is not None and alerted_at - t2 <= 70, (alerted_at, t2)
        assert [c for c, _ in bot.sent] == [LOOSE] and "plan <b>$770K</b>" in bot.sent[0][1], bot.sent
    asyncio.run(run())
    print("✅ aynı anahtarda ikinci emir) bilinen emrin yanında yeni TWAP ~1 dk içinde sorulur (eskiden ilk emrin bitişi"
          " + 2 dk'ya kadar kör)")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
