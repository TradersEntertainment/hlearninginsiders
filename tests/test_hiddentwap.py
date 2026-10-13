"""🕵️ Gizli TWAP — HL TWAP emri OLMADAN düzenli dilimleme (adres bazlı genel radar).

Kullanıcı (13.10): "genel gizli TWAP'ları yakalayabilir miyiz — CBRS balinası TWAP'ı HL'den değil kendi
sisteminden yapıyordu". twaplive'ın düzenli dizisi + emir sorgusu BAŞARILI ve emir yok → bu radar.

Pinlenenler:
  • kapı: taker payı, iki yönlü (market yapıcı), görülen tabanı + dakika hacmi katı, hacimden bağımsız büyük,
    hacim yok
  • akış: emirsiz düzenli dizi → 🕵️ mesaj (kripto kanalı + ayna), 'htwap' bekleme (ikinci turda yok); eski
    TWAP akışı aynen ('twap' işareti yok, karar no_order, /twap satırında 🕵️ nedeni); sorgu düşerse karar yok;
    hisse → ana sohbet + PROPR notu; kapalıyken yok
  • 🧪 LOG-HTWAP: $100K'yı ilk geçişte bir kez, kapı sonucu ne olursa olsun
  • mesaj ölçülü: plan/toplam/süre görünmez denir, tahmin dili yok
  • kablolama: tür (fan-out yok), ayarlar, .env, README, lab kuralı, /tani satırı
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-htwap.db")
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.hl import universe as uni  # noqa: E402
from app.notify import Notifier  # noqa: E402
from app.radar import hiddentwap as ht  # noqa: E402
from app.radar import twaplive as tl  # noqa: E402
from app.telegram import format as fmt  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A, B = "0x" + "a" * 40, "0x" + "b" * 40
PX = 10.0


class Bot:
    def __init__(self):
        self.sent = []

    async def send(self, text, chat_id=None):
        self.sent.append((chat_id, text))
        return True


class Col:
    """collector.fetch_twap_history taklidi: [] = sorgu başarılı, emir yok; None = sorgu düştü."""

    def __init__(self, hist):
        self.hist, self.calls, self.twap_sample = hist, [], None

    async def fetch_twap_history(self, addr, timeout=6.0):
        self.calls.append(addr)
        return self.hist


def _cfg(chat="-100"):
    cfg = Config()
    cfg.telegram_chat_id, cfg.crypto_chat_id = "111", chat
    cfg.twap_mirror_chat_id = ""
    cfg.notify_twap = cfg.notify_htwap = True
    cfg.twap_live_enabled = cfg.hidden_twap_enabled = True
    cfg.twap_alert_min_slices, cfg.twap_lookup_min_usd, cfg.twap_lookup_cooldown = 10, 50_000, 600
    cfg.twap_alert_cooldown = 21600
    cfg.hidden_twap_min_usd, cfg.hidden_twap_rate_x = 250_000, 0.5
    cfg.hidden_twap_big_usd, cfg.hidden_twap_taker_min_pct = 1_000_000, 50
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    cfg.twap_follow_enabled = False
    return cfg


async def _fresh(vol=2e6, coin="PUMP"):
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "ht.db"))
    tl.REG.clear()
    tl._lookup_cache.clear()
    tl.VOL_CACHE.update(ts=0, v={})
    for k in tl.MIRROR:
        tl.MIRROR[k] = 0
    ht.STATS.update(considered=0, alerted=0, lab=0, reasons={}, last_ts=0)
    ht._LAB_DONE.clear()
    await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {coin: {"m": PX, "oi": 1, "f": 0, "v": vol, "p": None},
                                             "BTC": {"m": 60_000, "oi": 1, "f": 0, "v": 2e9, "p": None}},
                                       "ts": dbm.now()})
    from app.lab import registry, specs
    for x in (registry._Q, registry.ACTIVE, registry.DERIVED, registry._SEEN, registry._DAY):
        x.clear()
    await registry.sync(_cfg(), ts=dbm.now() - 86400, rules=[r for r in specs.RULES if r["id"] in ("LOG-HTWAP", "LOG-TWAP")])


def _q(rid):
    from app.lab import registry
    return [e for e in registry._Q if e["rule_id"] == rid]


def _f(e):
    import json
    f = e["features"]
    return json.loads(f) if isinstance(f, str) else f


def feed(coin, addr, side, n, ntl, gap=30, taker=1, t_end=None):
    t_end = t_end or dbm.now() - 5
    for i in range(n):
        ts = t_end - (n - 1 - i) * gap
        tl.observe(coin, addr, side, PX, ntl / PX, ntl, int(ts), taker)


async def _kind(kind):
    async with dbm.db() as c:
        return (await (await c.execute("SELECT COUNT(*) n FROM alerts_log WHERE kind=?", (kind,))).fetchone())["n"]


# ---------------- kapı ----------------

def test_gate():
    async def run():
        await _fresh()
        cfg = _cfg()
        feed("PUMP", A, "buy", 20, 15_000)                    # $300K, 10 dk, ~$30K/dk; 24s $2M → ~21×
        r = tl.REG.runs[("PUMP", A, "buy")]
        m = tl.measure(r)
        assert m and abs(m["taker_pct"] - 100) < 1e-9
        g, x = ht.gate(cfg, r, m, 2e6, tl.REG)
        assert g == "ok" and x > 10, (g, x)
        assert ht.gate(cfg, r, m, None, tl.REG)[0] == "no_vol"
        assert ht.gate(cfg, r, m, 5e9, tl.REG)[0] == "slow", "BTC ölçeğinde hacim → yavaş"
        cfg.hidden_twap_min_usd = 400_000
        assert ht.gate(cfg, r, m, 2e6, tl.REG)[0] == "small"
        cfg.hidden_twap_big_usd = 250_000
        assert ht.gate(cfg, r, m, 5e9, tl.REG)[0] == "big", "büyük: hızdan bağımsız"
        cfg.hidden_twap_big_usd, cfg.hidden_twap_min_usd = 1_000_000, 250_000
        m2 = dict(m, taker_pct=30.0)
        assert ht.gate(cfg, r, m2, 2e6, tl.REG)[0] == "maker"
        feed("PUMP", A, "sell", 20, 5_000)                     # aynı adres karşı yönde $100K → iki yönlü
        assert ht.gate(cfg, r, m, 2e6, tl.REG)[0] == "two_sided"
        print("✅ kapı) ok / hacim yok / yavaş / küçük / büyük / maker / iki yönlü")
    asyncio.run(run())


# ---------------- akış ----------------

def test_flow_alert_cooldown_and_old_path_untouched():
    async def run():
        await _fresh()
        cfg = _cfg()
        cfg.twap_mirror_chat_id = "-777"
        bot = Bot()
        n = Notifier(cfg, bot)
        feed("PUMP", A, "buy", 20, 15_000)
        col = Col([])
        out = await tl.evaluate(cfg, n, None, col)
        assert out["no_order"] == 1 and out["alerted"] == 0, out
        chats = [c for c, _ in bot.sent]
        assert chats == ["-100", "-777"], bot.sent
        txt = bot.sent[0][1]
        assert txt.startswith("🕵️ <b>Gizli TWAP</b> · <b>PUMP</b>"), txt
        assert "HL'de TWAP emri yok — kendi botuyla dilimliyor" in txt and "20 dilim" in txt and "her 30 sn" in txt
        assert "şimdiye kadar <b>$300K</b>" in txt and "%100 taker" in txt
        assert "Plan, toplam ve süre görünmez" in txt and "bu hızla" not in txt and "24 saatte" not in txt
        assert await _kind("htwap") == 1 and await _kind("twap") == 0, "eski yolun 'twap' işareti yazılmaz"
        assert tl.REG.runs[("PUMP", A, "buy")].alerted_ts is None, "eski yolun tur durumu değişmez"
        last = await dbm.kv_get(tl.LAST_KV)
        assert last[0]["reason"] == "no_order" and last[0]["hidden"] == "ok", last[0]
        assert "🕵️ gizli twap: bildirildi" in tl.decision_line(last[0])
        assert len(_q("LOG-HTWAP")) == 1 and _f(_q("LOG-HTWAP")[0])["kapi"] == "ok"
        assert not _q("LOG-TWAP"), "emir yokken LOG-TWAP yazılmaz (eskisi gibi)"
        # ikinci tur: bekleme → mesaj yok, lab tekrar yok
        tl._lookup_cache.clear()
        feed("PUMP", A, "buy", 3, 15_000, t_end=dbm.now())
        await tl.evaluate(cfg, n, None, col)
        assert len(bot.sent) == 2 and len(_q("LOG-HTWAP")) == 1 and ht.STATS["reasons"].get("cooldown") == 1
        line = await ht.diag_line(cfg)
        assert line.startswith("gizli twap: 2 aday (emirsiz düzenli dizi) · 1 bildirim · 🧪 1 kayıt"), line
        assert "son: PUMP" in line
        print("✅ akış) emirsiz düzenli dizi → 🕵️ kanal + ayna; 'htwap' bekleme; eski yol aynen (no_order, 'twap'"
              " işareti yok, LOG-TWAP yok); LOG-HTWAP bir kez; /tani")
    asyncio.run(run())


def test_lookup_fail_small_maker_off():
    async def run():
        # sorgu düştü → karar yok
        await _fresh()
        cfg = _cfg()
        bot = Bot()
        feed("PUMP", A, "buy", 20, 15_000)
        out = await tl.evaluate(cfg, Notifier(cfg, bot), None, Col(None))
        assert out["lookup_fail"] == 1 and not bot.sent and ht.STATS["considered"] == 0
        # küçük: $150K (≥ lab tabanı) → mesaj yok, 🧪 kayıt VAR (kontrol grubu)
        await _fresh()
        feed("PUMP", A, "buy", 15, 10_000)
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        assert not bot.sent and ht.STATS["reasons"] == {"small": 1}
        e = _q("LOG-HTWAP")
        assert len(e) == 1 and _f(e[0])["kapi"] == "small" and e[0]["gated"] == 0 and e[0]["side"] == 1
        # maker dizisi → elenir
        await _fresh()
        feed("PUMP", A, "sell", 20, 15_000, taker=0)
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        assert not bot.sent and ht.STATS["reasons"] == {"maker": 1} and _q("LOG-HTWAP")[0]["side"] == -1
        # kapalı
        await _fresh()
        cfg.hidden_twap_enabled = False
        feed("PUMP", A, "buy", 20, 15_000)
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        assert not bot.sent and ht.STATS["reasons"] == {"off": 1} and not _q("LOG-HTWAP")
        print("✅ eleme) sorgu düşerse karar yok; küçük → yalnız 🧪 kayıt; maker → yok; kapalı → hiçbir şey")
    asyncio.run(run())


def test_own_group():
    async def run():
        await _fresh()
        cfg = _cfg()
        cfg.twap_mirror_chat_id = "-777"
        cfg.hidden_twap_chat_id = "-555"
        bot = Bot()
        feed("PUMP", A, "buy", 20, 15_000)
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        assert [c for c, _ in bot.sent] == ["-555"], "kendi grubu: yalnız oraya (TWAP kanalı ve ayna değil)"
        assert await _kind("htwap") == 1 and tl.MIRROR["sent"] == 0
        assert "grup: HIDDEN_TWAP_CHAT_ID" in await ht.diag_line(cfg)
        # kripto kanalı tanımsız olsa da kendi grubuna gider
        await _fresh()
        cfg2 = _cfg(chat="")
        cfg2.hidden_twap_chat_id = "-555"
        bot2 = Bot()
        feed("PUMP", A, "buy", 20, 15_000)
        await tl.evaluate(cfg2, Notifier(cfg2, bot2), None, Col([]))
        assert [c for c, _ in bot2.sent] == ["-555"]
        from app.telegram.bot import TelegramBot
        assert "-555" in TelegramBot._own_chats(type("B", (), {"cfg": cfg})())
        print("✅ grup) HIDDEN_TWAP_CHAT_ID tanımlıysa yalnız oraya; kripto kanalı yoksa da; bot o grupta komut alır")
    asyncio.run(run())


def test_equity_main_chat_and_propr_note():
    async def run():
        await _fresh(vol=3e6, coin="xyz:CBRS")
        cfg = _cfg()
        bot = Bot()
        async with dbm.db() as c:
            await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES('xyz:CBRS','xyz','CBRS')")
            await c.execute("INSERT INTO asset_metrics(coin,ts,day_volume,mark_px) VALUES('xyz:CBRS',?,3e6,10)",
                            (dbm.now(),))
            await c.commit()
        feed("xyz:CBRS", A, "buy", 40, 8_000, gap=32)            # CBRS balinası ölçüleri: ~$8K / 32 sn
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        assert len(bot.sent) == 1 and bot.sent[0][0] in (None, "", "111"), bot.sent
        txt = bot.sent[0][1]
        assert "<b>CBRS</b>" in txt and "✅ <b>PROPR'da listeli</b>" in txt and "her 32 sn" in txt, txt
        print("✅ hisse) CBRS benzeri dizi → ana sohbet + PROPR notu")
    asyncio.run(run())


# ---------------- kablolama ----------------

def test_wiring():
    from app import notify
    from app.lab import specs
    src = lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read()   # noqa: E731
    assert notify.KINDS["htwap"][0] == "notify_htwap" and "htwap" not in notify.PUBLIC_KINDS
    cfg = Config()
    assert cfg.hidden_twap_enabled and cfg.notify_htwap and cfg.hidden_twap_min_usd == 250_000
    assert cfg.hidden_twap_rate_x == 0.5 and cfg.hidden_twap_big_usd == 1_000_000 and cfg.hidden_twap_taker_min_pct == 50
    for f in ("hidden_twap_enabled", "hidden_twap_min_usd", "hidden_twap_rate_x", "hidden_twap_big_usd",
              "hidden_twap_taker_min_pct"):
        assert EDITABLE_FIELDS[f]["group"] == "🕵️ Gizli TWAP" and len(EDITABLE_FIELDS[f]["desc"]) > 30, f
    assert EDITABLE_FIELDS["notify_htwap"]["group"] == "Bildirimler"
    rule = next(r for r in specs.RULES if r["id"] == "LOG-HTWAP")
    assert rule["evidence"] == "log" and rule["horizons_s"] == [900, 3600, 14400, 86400]
    readme, env = src("README.md"), src(".env.example")
    assert "### 🕵️ Gizli TWAP — HL emri olmadan dilimleme" in readme and "LOG-HTWAP" in readme
    for k in ("HIDDEN_TWAP_ENABLED=1", "HIDDEN_TWAP_MIN_USD=250000", "HIDDEN_TWAP_RATE_X=0.5",
              "HIDDEN_TWAP_BIG_USD=1000000", "HIDDEN_TWAP_TAKER_MIN_PCT=50"):
        assert k in env, k
    assert "hiddentwap.diag_line(cfg)" in src("app/diag.py")
    assert hasattr(fmt, "hidden_twap")
    print("✅ kablolama) tür (fan-out yok), ayarlar, .env, README, LOG-HTWAP, /tani")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
