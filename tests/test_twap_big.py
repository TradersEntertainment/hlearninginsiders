"""🕐 Büyük TWAP kanalı (TWAP_BIG_CHAT_ID) — süresi ≤ 3 sa ve planı ≥ $1M her HL TWAP emri, hacimden bağımsız.

Kullanıcı (14.10, HYPE 0xedcd… 150 dk $1,6M satış hacmin %0,7'si olduğu için hiçbir kanala gelmedi): "yeni kanal kuralım,
max 2-3 saat içinde $1M'dan yüksek tüm TWAP'ları göndereceğimiz".

Pinlenenler:
  • kural: süren emir, 0 < süre ≤ TWAP_BIG_MAX_MIN (180), plan ≥ TWAP_BIG_MIN_USD ($1M); hacim / ana kapı fark etmez
  • BTC ve ETH'de taban $20M (kullanıcı 14.10: "btc eth kaldırmak yerine 20-30M"; HIP-3 aynı sembol + spot UBTC/UETH)
  • ilk dilim: ana ön süzgeç (SOL: dilim ≥ $27.8K) elese de dilim ≤ 3 sa'lik ≥ $1M emirden olabiliyorsa sorulur
    (yalnız-yan-kanal katmanı: ana yolun F.known / sayaç / 🧪 kayıtları değişmez); ana kanala giden emir de buraya gelir
  • eski yol: ana kararlardan sonra, aynı emir bir kez; ⛔ iptal notu buraya, bir kez; yeniden başlatmada kayıt geri gelir
  • 📅 hafta sonu kripto olmayan HIP-3: plan ≥ $200K ve süre < 24 sa da gelir (kullanıcı 14.10)
  • tanımsızken hiçbir şey değişmez; env-only; botun kendi sohbeti; /tani satırı
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-twapbig.db")
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.notify import KINDS, PUBLIC_KINDS, Notifier  # noqa: E402
from app.radar import twapfast as tf  # noqa: E402
from app.radar import twaplive as tl  # noqa: E402
from app.radar import twaploose as lo  # noqa: E402
from test_twapfast import A, PX, Bot, Col, _cfg, _drain, _fresh, _kind, twap_order  # noqa: E402

BIGC = "-400"


def _bcfg(loose=""):
    cfg = _cfg()
    cfg.twap_big_chat_id = BIGC
    cfg.twap_loose_chat_id = loose
    return cfg


async def _fresh_b(cfg, btc=False):
    await _fresh(cfg)
    tl._LIVE_Q.clear()
    lo.reset()
    if btc:
        from app.hl import universe as uni
        await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"PUMP": {"m": PX, "oi": 1, "f": 0, "v": 20e6, "p": None},
                                                 "BTC": {"m": 60_000, "oi": 1, "f": 0, "v": 2e9, "p": None},
                                                 "SOL": {"m": 150, "oi": 1, "f": 0, "v": 2e9, "p": None}},
                                           "ts": dbm.now()})
        tf.F.vols = await tl.volumes(None)


def sol_order(started, minutes, plan, status="activated"):
    o = twap_order(started, minutes=minutes, plan=plan, status=status, coin="SOL")
    sz = plan / 150
    o["state"].update(sz=str(sz), executedSz="0.0", executedNtl="0.0")
    return o


def test_gate_and_config():
    cfg = Config()
    assert cfg.twap_big_chat_id == "" and not lo.BIG.enabled(cfg)
    assert (cfg.twap_big_min_usd, cfg.twap_big_max_min) == (1_000_000, 180)
    for f in ("twap_big_chat_id", "twap_big_min_usd", "twap_big_max_min"):
        assert f not in EDITABLE_FIELDS, "env-only"
    assert KINDS["twap_big"][0] == "notify_twap" and "twap_big" not in PUBLIC_KINDS
    now = dbm.now()

    def o(minutes, plan, status="activated", ago=600):
        return {"status": status, "minutes": minutes, "planned_usd": plan, "started_ts": now - ago}
    assert lo.big_gate(cfg, o(150, 1_630_000), now) == "ok", "kullanıcının HYPE emri"
    assert lo.big_gate(cfg, o(180, 1_000_000), now) == "ok", "sınırlar dahil"
    assert lo.big_gate(cfg, o(181, 5e6), now) == "long"
    assert lo.big_gate(cfg, o(0, 5e6), now) == "long", "süresi bilinmeyen"
    assert lo.big_gate(cfg, o(150, 999_999), now) == "small"
    assert lo.big_gate(cfg, o(150, 5e6, "finished"), now) == "order_done"
    assert lo.big_gate(cfg, o(10, 5e6, ago=700), now) == "order_done", "süresi dolmuş"
    assert lo.big_gate(cfg, None, now) == "no_order"
    cfg.twap_big_max_min = 120                                       # "2 saat" denirse
    assert lo.big_gate(cfg, o(150, 2e6), now) == "long"
    cfg.twap_big_max_min = 0
    assert lo.big_gate(cfg, o(150, 2e6), now) == "off"
    cfg.twap_big_max_min = 180
    assert lo.big_pre(cfg, 2_300) and not lo.big_pre(cfg, 2_000), "dilim ≥ $1M·0.8/361 ≈ $2.2K"
    assert lo.big_head(cfg) == "🕐 kanal kuralı: plan ≥ $1.0M · süre ≤ 3 sa"
    print("✅ kural) süren emir, 0 < süre ≤ 180 dk, plan ≥ $1M; ön süzgeç dilim ≥ ~$2.2K; env-only")


def test_fast_path_big_regardless_of_main():
    """SOL 2 sa $5M emir (24s hacmin %0,25'i): ana ön süzgeç eler (dilim $20.7K < $27.8K) → yalnız yan kanal katmanı →
    🕐 kanala; ana kanal / ayna / F.known / sayaç / 🧪 aynı. Ana kanala giden PUMP emri de 🕐'a gelir."""
    async def run():
        from test_twapfast import _q
        cfg = _bcfg()
        await _fresh_b(cfg, btc=True)
        now = dbm.now()
        t0 = now - 60
        col = Col(lambda: [sol_order(t0, 120, 5_000_000)])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        tf.observe("SOL", A, "buy", 150, 20_700 / 150, 20_700, t0 + 2, 1000)
        p = tf.F.pending[("SOL", A, "buy")]
        assert p["queued"] and p["loose"] and tf.F.stats["pre_low"] == 1 and tf.F.stats["queued"] == 0
        before = dict(tf.F.stats)
        await _drain(cfg, notifier, col)
        assert [c for c, _ in bot.sent] == [BIGC], bot.sent
        text = bot.sent[0][1]
        lines = text.split("\n")
        assert text.startswith("⏳ <b>TWAP başladı</b> · <b>SOL</b>"), text         # 14.10: emir en başta, not altta
        assert lines[lines.index("🕐 kanal kuralı: plan ≥ $1.0M · süre ≤ 3 sa") + 1].startswith("👁"), text
        assert "plan <b>$5.0M</b>" in text and "/takip_" in text, "uzun emir: takip tuşu"
        assert ("SOL", A, "buy") not in tf.F.known and not _q("LOG-TWAPF")
        assert {k: v for k, v in tf.F.stats.items() if v != before.get(k)} == {}
        assert await _kind("twap_big") == 1 and await _kind("twap") == 0 and await _kind("twap_fast") == 0
        okey = f"SOL:{A}:buy:{t0}"
        assert okey in lo.BIG.open and lo.BIG.stats["alerts"] == 1
        # ana kanala giden emir de 🕐 kanala (ana kararın ardından)
        bot.sent.clear()
        t1 = now - 200
        col2 = Col(lambda: [twap_order(t1, minutes=10, plan=1_200_000)])
        tf.observe("PUMP", A, "buy", PX, 12_000_000, 60_000, t1 + 2, 1000)
        await _drain(cfg, notifier, col2)
        assert [c for c, _ in bot.sent] == ["-100", "-200", BIGC], bot.sent
        assert bot.sent[2][1].startswith("⏳") and "🕐 kanal kuralı" in bot.sent[2][1] \
            and "⚡ <b>HIZLI · 10 dk</b>" in bot.sent[2][1]
        assert "🔔 iptal edilirse ⛔ buraya otomatik yazılır" in bot.sent[2][1]
    asyncio.run(run())
    print("✅ ilk dilim) hacimden bağımsız: SOL $5M 2 sa → yalnız 🕐 (ana yol aynı); ana kanala giden emir de 🕐'a")


def test_slow_path_once_cancel_restart():
    """Eski yol: PUMP 150 dk $1.6M emir hacmin %0,8'i (ana kapı: vol_small) → 🕐 kanala, turlar boyu bir kez; iptal → ⛔
    yalnız 🕐'a, bir kez; yeniden başlatmada kayıt geri gelir."""
    async def run():
        cfg = _bcfg()
        cfg.twap_fast_enabled = False
        await _fresh_b(cfg)
        from app.hl import universe as uni
        await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"PUMP": {"m": PX, "oi": 1, "f": 0, "v": 200e6, "p": None}},
                                           "ts": dbm.now()})
        now = dbm.now()
        started = now - 400
        state = {"st": "activated"}
        col = Col(lambda: [twap_order(started, minutes=150, plan=1_630_000, ex_frac=0.0, status=state["st"])])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        for i in range(13):
            tl.observe("PUMP", A, "buy", PX, 2_200_000, 11_000, started + 2 + 30 * i, 1, True)
        out = await tl.evaluate(cfg, notifier, None, col)
        assert out["vol_small"] == 1 and [c for c, _ in bot.sent] == [BIGC], (out, bot.sent)
        text = bot.sent[0][1]
        assert text.startswith("⏳") and "🕐 kanal kuralı" in text and "doldu ≈" in text and "doldu <b>$0</b>" not in text, \
            text
        await tl.evaluate(cfg, notifier, None, col)
        assert len(bot.sent) == 1, "aynı emir bir kez"
        okey = f"PUMP:{A}:buy:{started}"
        await lo.BIG.save_open()
        lo.BIG.clear()
        assert await lo.load_all(now) == 1 and okey in lo.BIG.open, "yeniden başlatma"
        state["st"] = "terminated"
        lo.BIG.open[okey]["lookup_ts"] = 0
        assert await lo.lifecycle_all(cfg, notifier, col, now + 400) == 1
        assert [c for c, t in bot.sent if "TWAP iptal edildi" in t] == [BIGC]
        lo.BIG.open[okey]["ended"], lo.BIG.open[okey]["lookup_ts"] = 0, 0
        await lo.lifecycle_all(cfg, notifier, col, now + 500)
        assert len([1 for _, t in bot.sent if "TWAP iptal edildi" in t]) == 1, "⛔ bir kez"
        line = await lo.big_diag_line(cfg)
        assert line.startswith("🕐 büyük twap kanalı: plan ≥ $1.0M · süre ≤ 180 dk") or "plan ≥" in line, line
    asyncio.run(run())
    print("✅ eski yol) hacmin %0,8'i olan 150 dk $1.6M emir 🕐'a bir kez; ⛔ bir kez; yeniden başlatma; /tani")


def test_disabled_and_with_loose():
    async def run():
        # tanımsız: bugünkü gibi (SOL dilimi sorulmaz)
        cfg = _cfg()
        await _fresh_b(cfg, btc=True)
        now = dbm.now()
        tf.observe("SOL", A, "buy", 150, 20_700 / 150, 20_700, now, 1000)
        assert not tf.F.pending[("SOL", A, "buy")]["queued"]
        # 🔸 + 🕐 birlikte. (a) ana kapıyı geçen emir ($1.4M, 24s hacmin %7'si): ana + ayna + 🔸 kopya + 🕐
        cfg = _bcfg(loose="-300")
        await _fresh_b(cfg)
        now = dbm.now()
        t0 = now - 200
        col = Col(lambda: [twap_order(t0, minutes=120, plan=1_400_000)])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        tf.observe("PUMP", A, "buy", PX, 2_000_000, 10_000, t0 + 2, 1000)
        await _drain(cfg, notifier, col)
        assert [c for c, _ in bot.sent] == ["-100", "-200", "-300", BIGC], bot.sent
        assert "🔸" not in bot.sent[2][1] and "🕐 kanal kuralı" in bot.sent[3][1] and bot.sent[3][1].startswith("⏳")
        # (b) ana kapıdan kalan ($1.2M = 24s $40M'ın %3'ü < %5) ama gevşeği (≥ %2,5) ve 🕐'u geçen emir: 🔸 ek + 🕐
        from app.hl import universe as uni
        await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"PUMP": {"m": PX, "oi": 1, "f": 0, "v": 40e6, "p": None}},
                                           "ts": dbm.now()})
        tf.F.vols = await tl.volumes(None)
        tl.VOL_CACHE.update(ts=0, v={})
        B2 = "0x" + "c" * 40
        bot.sent.clear()
        hist = [twap_order(t0, minutes=120, plan=1_200_000)]
        for h in hist:
            h["state"]["user"] = B2
        col = Col(lambda: hist)
        tf.observe("PUMP", B2, "buy", PX, 2_000_000, 10_000, t0 + 2, 1000)
        await _drain(cfg, notifier, col)
        assert [c for c, _ in bot.sent] == ["-300", BIGC], bot.sent
        assert "🔸 ana kanal eşiğinin altında" in bot.sent[0][1] and "🕐 kanal kuralı" in bot.sent[1][1]
        from app.telegram.bot import TelegramBot
        assert BIGC in TelegramBot(cfg, None, None, {})._own_chats()
    asyncio.run(run())
    print("✅ tanımsız) bugünkü gibi; 🔸 ile birlikte: her kanala bir kez; 🕐 kanal botun kendi sohbeti")


def test_btc_eth_major_floor():
    """Kullanıcı 14.10: "btc eth kaldırmak yerine 20-30M diyelim" — BTC / ETH (HIP-3 aynı sembol, spot UBTC/UETH) için
    plan tabanı $20M; ön süzgeç de bu tabanla (BTC'de dilim ≳ $44K değilse sorulmaz). Ayarla değişir."""
    async def run():
        from app import assets
        cfg = _bcfg()
        await _fresh_b(cfg, btc=True)
        assets.SPOT_NAMES.update({"@142": "UBTC/USDC", "@151": "UETH/USDC", "@107": "PURR/USDC"})
        for c in ("BTC", "ETH", "xyz:BTC", "@142", "@151"):
            assert lo.big_is_major(cfg, c) and lo.big_min_for(cfg, c) == 20_000_000, c
        for c in ("SOL", "HYPE", "@107", "xyz:TSLA"):
            assert not lo.big_is_major(cfg, c) and lo.big_min_for(cfg, c) == 1_000_000, c
        now = dbm.now()

        def o(plan):
            return {"status": "activated", "minutes": 120, "planned_usd": plan, "started_ts": now - 60}
        assert lo.big_gate(cfg, o(19e6), now, "BTC") == "small" and lo.big_gate(cfg, o(20e6), now, "BTC") == "ok"
        assert lo.big_gate(cfg, o(19e6), now, "SOL") == "ok"
        assert lo.big_head(cfg, "BTC") == "🕐 kanal kuralı: plan ≥ $20.0M (BTC/ETH) · süre ≤ 3 sa"
        assert lo.big_head(cfg, "SOL") == "🕐 kanal kuralı: plan ≥ $1.0M · süre ≤ 3 sa"
        assert not lo.big_pre(cfg, 40_000, "BTC") and lo.big_pre(cfg, 45_000, "BTC") and lo.big_pre(cfg, 2_300, "SOL")
        tf.observe("BTC", A, "buy", 60_000, 20_700 / 60_000, 20_700, now, 1000)
        assert not tf.F.pending[("BTC", A, "buy")]["queued"], "BTC'de $20.7K dilim bu kanal için sorulmaz"
        # BTC 2 sa $25M emir (24s $2B'nin %1,25'i, ana kapı %20 ister): 🕐 kanala
        t0 = now - 60
        col = Col(lambda: [{**twap_order(t0, minutes=120, plan=25_000_000, coin="BTC"),
                            "state": {**twap_order(t0, minutes=120, plan=25_000_000, coin="BTC")["state"],
                                      "sz": str(25_000_000 / 60_000), "executedSz": "0.0", "executedNtl": "0.0"}}])
        bot = Bot()
        tf.F.pending.clear()
        tf.observe("BTC", A, "buy", 60_000, 104_000 / 60_000, 104_000, t0 + 2, 1000)
        assert tf.F.pending[("BTC", A, "buy")]["loose"]
        await _drain(cfg, Notifier(cfg, bot), col)
        assert [c for c, _ in bot.sent] == [BIGC] and "plan ≥ $20.0M (BTC/ETH)" in bot.sent[0][1], bot.sent
        cfg.twap_big_major_min_usd = 30_000_000                       # "30M" denirse
        assert lo.big_gate(cfg, o(25e6), now, "BTC") == "small"
        cfg.twap_big_major_min_usd = 0                                 # ayrı taban kapalı → genel $1M
        assert lo.big_min_for(cfg, "BTC") == 1_000_000
        cfg.twap_big_major_min_usd = 20_000_000
        line = await lo.big_diag_line(cfg)
        assert "plan ≥ $1.0M (BTC/ETH ≥ $20.0M)" in line, line
        for k in ("@142", "@151", "@107"):
            assets.SPOT_NAMES.pop(k, None)
    asyncio.run(run())
    print("✅ BTC/ETH) taban $20M (HIP-3 aynı sembol, spot UBTC/UETH); ön süzgeç de; ayarla 30M / kapalı")


def test_weekend_stock_rule():
    """Kullanıcı 14.10: "hafta sonu TWAP'la hisse satan çok oluyor, fiyatı aşırı etkiliyor — big TWAP grubuna sadece hafta
    sonu, hisseyse $200K+ ve 1 günden kısa". Hafta sonu = botun ortak penceresi (TSİ Cmt 00:00 → Pzt 00:00, ABD kapalı);
    kripto olmayan HIP-3 (hisse, endeks, emtia…). İlk dilim: 24 sa'lik $200K emrin ~$70'lık dilimi de sorulur (yalnız 🕐
    için; ana yol / 🔸 kanal bu dilimleri bugünkü gibi görmez)."""
    async def run():
        cfg = _bcfg(loose="-300")
        await _fresh_b(cfg)
        now = dbm.now()
        orig = lo.big_weekend_on
        wk = {"on": True}
        lo.big_weekend_on = lambda c, ts: wk["on"]
        try:
            def o(plan, minutes):
                return {"status": "activated", "minutes": minutes, "planned_usd": plan, "started_ts": now - 60}
            T = "xyz:TSLA"
            assert lo.big_gate(cfg, o(250_000, 600), now, T) == "weekend"
            assert lo.big_gate(cfg, o(150_000, 600), now, T) == "small"
            assert lo.big_gate(cfg, o(250_000, 1440), now, T) == "long", "1 günden kısa"
            assert lo.big_gate(cfg, o(250_000, 1439), now, T) == "weekend"
            assert lo.big_gate(cfg, o(2_000_000, 120), now, T) == "ok", "genel kural da geçer"
            assert lo.big_gate(cfg, o(250_000, 120), now, "SOL") == "small", "kripto: hafta sonu kuralı yok"
            assert lo.big_gate(cfg, o(250_000, 600), now, "SOL") == "long"
            assert lo.big_gate(cfg, o(250_000, 120), now, "xyz:TOTAL2") == "small", "kriptoya bağlı HIP-3 hariç"
            assert lo.big_gate(cfg, o(300_000, 120), now, "xyz:BTC") == "small", "HIP-3 BTC: $20M tabanı"
            assert lo.big_head(cfg, T, "weekend") == "🕐 📅 hafta sonu (ABD kapalı), kripto olmayan · plan ≥ $200K · süre &lt; 24 sa"
            assert lo.big_pre(cfg, 60, T, now) and not lo.big_pre(cfg, 60, T, None) and not lo.big_pre(cfg, 60, "SOL", now)
            # ilk dilim: 10 sa $300K emir, dilim ~$250 (< ana $1K tabanı) → yalnız 🕐 için sorulur
            t0 = now - 60
            col = Col(lambda: [twap_order(t0, minutes=600, plan=300_000, ex_frac=0.0, coin=T, side="A")])
            bot = Bot()
            notifier = Notifier(cfg, bot)
            before = dict(tf.F.stats)
            tf.observe(T, A, "sell", PX, 250 / PX, 250, t0 + 2, 1000)
            p = tf.F.pending[(T, A, "sell")]
            assert p["queued"] and p["loose"]
            assert {k: v for k, v in tf.F.stats.items() if v != before.get(k) and k not in ("zero", "slices")} == {}
            await _drain(cfg, notifier, col)
            assert [c for c, _ in bot.sent] == [BIGC], bot.sent
            assert bot.sent[0][1].startswith("⏳") and "🕐 📅 hafta sonu (ABD kapalı)" in bot.sent[0][1], bot.sent[0][1]
            assert "/takip_" in bot.sent[0][1]
            # 2. inceleme: 🔸 açık ve emir 🔸 kapısını da geçse ($600K, 24s $20M'ın %3'ü) küçük dilim 🔸'a SUNULMAZ
            from app.hl import universe as uni
            await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {T: {"m": PX, "oi": 1, "f": 0, "v": 20e6, "p": None}},
                                               "ts": dbm.now()})
            tf.F.vols = await tl.volumes(None)
            B3 = "0x" + "f" * 40
            h3 = [twap_order(t0, minutes=1200, plan=600_000, ex_frac=0.0, coin=T, side="A")]
            h3[0]["state"]["user"] = B3
            bot.sent.clear()
            tf.observe(T, B3, "sell", PX, 250 / PX, 250, t0 + 2, 1000)
            assert tf.F.pending[(T, B3, "sell")]["big_only"]
            await _drain(cfg, notifier, Col(lambda: h3))
            assert [c for c, _ in bot.sent] == [BIGC], bot.sent
            assert lo.RECENT[0]["loose"] == "big_only"
            # hafta içi: aynı küçük dilim sorulmaz
            wk["on"] = False
            tf.F.pending.clear()
            lo.KNOWN.clear()
            tf.observe(T, "0x" + "d" * 40, "sell", PX, 250 / PX, 250, now, 1000)
            assert not tf.F.pending[(T, "0x" + "d" * 40, "sell")]["queued"]
            # 🕐 kapalıyken (yalnız 🔸 açık) hafta sonu da küçük dilim sorulmaz — 🔸 kanal bugünkü gibi
            wk["on"] = True
            cfg.twap_big_chat_id = ""
            tf.observe(T, "0x" + "e" * 40, "sell", PX, 250 / PX, 250, now, 1000)
            assert not tf.F.pending[(T, "0x" + "e" * 40, "sell")]["queued"]
            cfg.twap_big_chat_id = BIGC
            cfg.twap_big_weekend_min_usd = 0                               # kural kapalı → genel kural (≤ 3 sa)
            assert lo.big_gate(cfg, o(250_000, 600), now, T) == "long"
            cfg.twap_big_weekend_min_usd = 200_000
            assert "📅 hafta sonu kripto olmayan: plan ≥ $200K · süre < 1440 dk (şu an hafta sonu)" in \
                await lo.big_diag_line(cfg)
        finally:
            lo.big_weekend_on = orig
        # gerçek pencere: Cumartesi TSİ → evet, Çarşamba → hayır
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tr = ZoneInfo("Europe/Istanbul")
        sat = int(datetime(2026, 10, 17, 14, 0, tzinfo=tr).timestamp())
        wed = int(datetime(2026, 10, 14, 14, 0, tzinfo=tr).timestamp())
        assert lo.big_weekend_on(cfg, sat) and not lo.big_weekend_on(cfg, wed)
    asyncio.run(run())
    print("✅ 📅 hafta sonu) kripto olmayan HIP-3: plan ≥ $200K ve süre < 24 sa → 🕐; küçük dilim yalnız 🕐 için sorulur"
          " (ana yol / 🔸 aynı); hafta içi / kripto / kural kapalı → yok")


def test_review_sort_alerted_branch_precision_prune():
    """İnceleme (14.10): (a) 🕐 kapalıyken 🔸 adaylarının sorgu sırası bugünkü gibi (dakika hızı / ortalama), 🕐 açıkken
    🔸 adayları önce, yalnız-🕐 adaylar sonra ($'a göre); (b) ana kanalın 6 sa içinde bildirdiği adresin yeni emri eski
    yoldan da 🕐'a gelir (bildirilmiş tur dalı); (c) yan kanal mesajında küçük oran %0 değil %0.8; (d) 🕐 geri çekilme
    kaydı budanır."""
    async def run():
        # (a) iki 🔸 adayı (BTC $80K → öncelik 0.115; WIF $10K, 24s $100M → 0.288) + bir yalnız-🕐 aday ($500K), tek yer
        for big in ("", BIGC):
            cfg = _bcfg(loose="-300")
            cfg.twap_big_chat_id = big
            await _fresh_b(cfg, btc=True)
            now = dbm.now()
            vols = {**tf.F.vols, "WIF": (100e6, now), "ARB": (1e9, now)}
            for coin, usd, big_only in (("BTC", 80_000, False), ("WIF", 10_000, False), ("ARB", 500_000, True)):
                tf.F.pending[(coin, A, "buy")] = {"coin": coin, "addr": A, "side": "buy", "max": usd, "usd": usd,
                                                  "queued": now, "not_before": 0, "loose": True, "big_only": big_only,
                                                  "tries": 0, "ts": now, "n": 1}
            got = []

            async def fake(cfg_, client, notifier, collector, key, p_, vols_):
                got.append(key[0])
            orig, ov = tf.resolve, tf.F.vols
            tf.resolve, tf.F.vols = fake, vols
            try:
                for _ in range(3):
                    tf.F.lookup_ts.clear()
                    tf.F.lookup_ts.extend([now] * (int(30 * lo.QUERY_SHARE) - 1))   # her turda tek yer
                    tasks: set = set()
                    await tf.dispatch(cfg, None, None, Col([]), now, tasks)
                    await asyncio.gather(*tasks)
                    for c in got:
                        tf.F.pending.pop((c, A, "buy"), None)
            finally:
                tf.resolve, tf.F.vols = orig, ov
            assert got == ["WIF", "BTC", "ARB"], (big, got)
        # (b) + (c)
        cfg = _bcfg()
        cfg.twap_fast_enabled = False
        await _fresh_b(cfg)
        from app.hl import universe as uni
        await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"PUMP": {"m": PX, "oi": 1, "f": 0, "v": 200e6, "p": None}},
                                           "ts": dbm.now()})
        now = dbm.now()
        started = now - 400
        await dbm.alert_log("twap", f"PUMP:{A}:buy", "")              # ana kanal bu adresi az önce bildirdi
        col = Col(lambda: [twap_order(started, minutes=150, plan=1_630_000, ex_frac=0.0)])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        for i in range(13):
            tl.observe("PUMP", A, "buy", PX, 2_200_000, 11_000, started + 2 + 30 * i, 1, True)
        out = await tl.evaluate(cfg, notifier, None, col)
        assert [d["reason"] for d in out["decisions"]] == ["cooldown"] and bot.sent == []
        await tl.evaluate(cfg, notifier, None, col)                   # bildirilmiş tur: emir sorulur → 🕐
        assert [c for c, _ in bot.sent] == [BIGC], bot.sent
        assert "🐘 emir 24s hacmin <b>%0.8</b>'i" in bot.sent[0][1], bot.sent[0][1]
        await tl.evaluate(cfg, notifier, None, col)
        assert len(bot.sent) == 1, "bir kez"
        # (d)
        lo.BIG.failed["a:x"] = now - 1
        lo.prune(now)
        assert "a:x" not in lo.BIG.failed
    asyncio.run(run())
    print("✅ inceleme) 🔸 sırası 🕐 kapalıyken aynı; ana kanalın bildirdiği adresin emri de 🕐'a; %0.8; geri çekilme budanır")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
