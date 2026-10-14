"""🕐 Büyük TWAP kanalı (TWAP_BIG_CHAT_ID) — süresi ≤ 3 sa ve planı ≥ $1M her HL TWAP emri, hacimden bağımsız.

Kullanıcı (14.10, HYPE 0xedcd… 150 dk $1,6M satış hacmin %0,7'si olduğu için hiçbir kanala gelmedi): "yeni kanal kuralım,
max 2-3 saat içinde $1M'dan yüksek tüm TWAP'ları göndereceğimiz".

Pinlenenler:
  • kural: süren emir, 0 < süre ≤ TWAP_BIG_MAX_MIN (180), plan ≥ TWAP_BIG_MIN_USD ($1M); hacim / ana kapı fark etmez
  • BTC ve ETH'de taban $20M (kullanıcı 14.10: "btc eth kaldırmak yerine 20-30M"; HIP-3 aynı sembol + spot UBTC/UETH)
  • ilk dilim: ana ön süzgeç (SOL: dilim ≥ $27.8K) elese de dilim ≤ 3 sa'lik ≥ $1M emirden olabiliyorsa sorulur
    (yalnız-yan-kanal katmanı: ana yolun F.known / sayaç / 🧪 kayıtları değişmez); ana kanala giden emir de buraya gelir
  • eski yol: ana kararlardan sonra, aynı emir bir kez; ⛔ iptal notu buraya, bir kez; yeniden başlatmada kayıt geri gelir
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
    assert lo.big_head(cfg) == "🕐 <b>Büyük TWAP</b> · plan ≥ $1.0M · süre ≤ 3 sa"
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
        assert text.startswith("🕐 <b>Büyük TWAP</b> · plan ≥ $1.0M · süre ≤ 3 sa\n⏳ <b>TWAP başladı</b> · <b>SOL</b>"), text
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
        assert bot.sent[2][1].startswith("🕐 <b>Büyük TWAP</b>") and "⚡ <b>HIZLI · 10 dk</b>" in bot.sent[2][1]
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
        assert text.startswith("🕐 <b>Büyük TWAP</b>") and "doldu ≈" in text and "doldu <b>$0</b>" not in text, text
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
        assert "🔸" not in bot.sent[2][1] and bot.sent[3][1].startswith("🕐")
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
        assert bot.sent[0][1].startswith("🔸 <b>Yan kanal</b>") and bot.sent[1][1].startswith("🕐 <b>Büyük TWAP</b>")
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
        assert lo.big_head(cfg, "BTC") == "🕐 <b>Büyük TWAP</b> · plan ≥ $20.0M (BTC/ETH) · süre ≤ 3 sa"
        assert lo.big_head(cfg, "SOL") == "🕐 <b>Büyük TWAP</b> · plan ≥ $1.0M · süre ≤ 3 sa"
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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
