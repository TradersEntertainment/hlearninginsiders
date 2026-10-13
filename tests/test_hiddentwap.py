"""🕵️ Gizli TWAP — HL TWAP emri OLMADAN düzenli dilimleme (adres bazlı genel radar).

Kullanıcı (13.10): "genel gizli TWAP'ları yakalayabilir miyiz — CBRS balinası TWAP'ı HL'den değil kendi
sisteminden yapıyordu". twaplive'ın düzenli dizisi + emir sorgusu BAŞARILI ve emir yok → bu radar.

Pinlenenler:
  • kapı: taker payı, iki yönlü (market yapıcı), görülen tabanı + dakika hacmi katı, hacimden bağımsız büyük,
    hacim yok
  • akış: emirsiz düzenli dizi → 🕵️ mesaj YALNIZ HIDDEN_TWAP_CHAT_ID grubuna (kripto kanalı / ana sohbet / ayna
    asla; değişken boşsa hiç mesaj yok, bekleme yakılmaz), 'htwap' bekleme (ikinci turda yok); eski
    TWAP akışı aynen ('twap' işareti yok, karar no_order, /twap satırında 🕵️ nedeni); sorgu düşerse karar yok;
    hisse de aynı gruba + PROPR notu; kapalıyken yok
  • 🧪 LOG-HTWAP: $100K'yı ilk geçişte bir kez, kapı sonucu ne olursa olsun
  • mesaj ölçülü: plan/toplam/süre görünmez denir, tahmin dili yok
  • kablolama: tür (fan-out yok), ayar sayfasında YOK (kullanıcı 13.10: yalnız Railway değişkeni; env okunur,
    eski kayıtlı ayar yok sayılır, /bildirimler'de satır yok), .env, README, lab kuralı, /tani satırı
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


def _cfg(chat="-100", own="-555"):
    cfg = Config()
    cfg.telegram_chat_id, cfg.crypto_chat_id = "111", chat
    cfg.twap_mirror_chat_id = ""
    cfg.notify_twap = True
    cfg.hidden_twap_chat_id = own
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
    ht.reset()
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


def feed(coin, addr, side, n, ntl, gap=30, taker=1, t_end=None, native=False):
    t_end = t_end or dbm.now() - 5
    for i in range(n):
        ts = t_end - (n - 1 - i) * gap
        tl.observe(coin, addr, side, PX, ntl / PX, ntl, int(ts), taker, native)


class ColSeq(Col):
    """Sırayla farklı geçmiş döndürür (son eleman tekrarlanır)."""

    def __init__(self, seq):
        super().__init__(None)
        self.seq = list(seq)

    async def fetch_twap_history(self, addr, timeout=6.0):
        self.calls.append(addr)
        return self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]


def order(coin="PUMP", user=A, side="B", status="activated", start_ago=1300, minutes=60, ntl=2_000_000):
    t0 = dbm.now() - start_ago
    return {"time": t0, "twapId": 9, "status": {"status": status},
            "state": {"coin": coin, "user": user, "side": side, "sz": str(ntl / PX * 2), "executedSz": str(ntl / PX),
                      "executedNtl": str(ntl), "minutes": minutes, "timestamp": t0 * 1000,
                      "reduceOnly": False, "randomize": False}}


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
        assert chats == ["-555"], ("yalnız gizli TWAP grubu: kripto kanalı (-100) ve ayna (-777) değil", bot.sent)
        assert tl.MIRROR["sent"] == 0
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
        assert len(bot.sent) == 1 and len(_q("LOG-HTWAP")) == 1 and ht.STATS["reasons"] == {"same_op": 1}, ht.STATS
        line = await ht.diag_line(cfg)
        assert line.startswith("gizli twap: 1 dizi (emirsiz, düzenli) · 1 bildirim · 🧪 1 kayıt"), line
        assert "son: PUMP" in line
        print("✅ akış) emirsiz düzenli dizi → 🕵️ yalnız gizli TWAP grubu; 'htwap' bekleme; eski yol aynen (no_order, 'twap'"
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


def test_review_fixes_no_false_hidden_claim():
    """İnceleme (13.10): gerçek HL TWAP'ı "emirsiz" diye bildirme yolları kapalı; tek uyarı / operasyon; eleme."""
    async def run():
        cfg = _cfg()
        # 1) tur sorgu tavanı: bu tur sorulmadı → emir YOK sayılmaz (karar lookup_capped, 🕵️ yok, 🧪 yok)
        await _fresh()
        bot, old = Bot(), tl.LOOKUP_MAX_PER_EVAL
        tl.LOOKUP_MAX_PER_EVAL = 0
        try:
            feed("PUMP", A, "buy", 20, 15_000)
            out = await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        finally:
            tl.LOOKUP_MAX_PER_EVAL = old
        assert out["lookup_capped"] == 1 and not bot.sent and ht.STATS["considered"] == 0 and not _q("LOG-HTWAP")
        assert (await dbm.kv_get(tl.LAST_KV))[0]["reason"] == "lookup_capped"
        # 2) adres önbelleği diziden ESKİ (emir sonradan açıldı) → iddiadan önce zorla sorulur → HL emri var
        await _fresh()
        bot = Bot()
        tl._lookup_cache[A] = (dbm.now() - 400, [])             # 600 sn önbellek içinde ama son dilimden eski
        col = Col([order()])
        feed("PUMP", A, "buy", 20, 15_000)
        await tl.evaluate(cfg, Notifier(cfg, bot), None, col)
        assert col.calls == [A] and not bot.sent and ht.STATS["reasons"] == {"native_order": 1}, ht.STATS
        assert not _q("LOG-HTWAP")
        # zorla sorgu düşerse: karar yok (veri yok ≠ emir yok)
        await _fresh()
        tl._lookup_cache[A] = (dbm.now() - 400, [])
        feed("PUMP", A, "buy", 20, 15_000)
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col(None))
        assert not bot.sent and ht.STATS["reasons"] == {"no_data": 1} and not _q("LOG-HTWAP")
        # 3) dizide HL TWAP alt emri dolumu (sıfır hash, taker) → emir var demektir, sorgu gerekmez
        await _fresh()
        feed("PUMP", A, "buy", 20, 15_000, native=True)
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        assert tl.REG.runs[("PUMP", A, "buy")].nat_n == 20 and not bot.sent
        assert ht.STATS["considered"] == 0, "HL emriyle yürüyen dizi gizli TWAP'a hiç girmez (/tani 'emirsiz' saymaz)"
        assert "hidden" not in (await dbm.kv_get(tl.LAST_KV))[0]
        # maker tarafındaki adres sıfır hash'ten etkilenmez (taker=0 → native=False collector'da)
        # 4) dizi başlamadan çok önce BİTMİŞ eski emir → gizli TWAP (adres ömür boyu silinmez)
        await _fresh()
        feed("PUMP", A, "buy", 20, 15_000)
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([order(status="finished", start_ago=20_000, minutes=30)]))
        assert [c for c, _ in bot.sent] == ["-555"] and (await dbm.kv_get(tl.LAST_KV))[0]["reason"] == "order_done"
        # eski emir bu dizinin emri değil: ne 🧪 LOG-TWAP ne /twaplar kaydı ne canlı panelde plan
        assert not _q("LOG-TWAP") and len(_q("LOG-HTWAP")) == 1
        async with dbm.db() as c:
            assert (await (await c.execute("SELECT COUNT(*) n FROM twap_runs")).fetchone())["n"] == 0
        assert tl.live_runs("PUMP")[0]["planned_usd"] is None
        # dizinin süresiyle örtüşen bitmiş/iptal emir → HL emri sayılır
        await _fresh()
        bot = Bot()
        feed("PUMP", A, "buy", 20, 15_000)
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([order(status="terminated", start_ago=900, minutes=60)]))
        assert not bot.sent and ht.STATS["considered"] == 0 and len(_q("LOG-TWAP")) == 1, "HL emri: eski yol aynen"
        # 4b) aynı adres + yön 6 sa içinde HL TWAP bildirimi aldı, emir BİTTİ; şimdi kendi botuyla dilimliyor →
        #     bekleme bu diziyi benimsemez (eski emir bağlanmaz), gizli TWAP gider
        await _fresh()
        bot = Bot()
        await dbm.alert_log("twap", f"PUMP:{A}:buy", "")
        feed("PUMP", A, "buy", 20, 15_000)
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([order(status="finished", start_ago=9000, minutes=30)]))
        assert [c for c, _ in bot.sent] == ["-555"] and tl.REG.runs[("PUMP", A, "buy")].alerted_ts is None
        async with dbm.db() as c:
            assert (await (await c.execute("SELECT COUNT(*) n FROM twap_runs")).fetchone())["n"] == 0
        # bekleme içinde sorgu bir kez düşerse dizi KALICI benimsenmez: karar lookup_fail, sonraki tur gizli TWAP
        await _fresh()
        bot = Bot()
        await dbm.alert_log("twap", f"PUMP:{A}:buy", "")
        feed("PUMP", A, "buy", 20, 15_000)
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col(None))
        r = tl.REG.runs[("PUMP", A, "buy")]
        assert r.alerted_ts is None and (await dbm.kv_get(tl.LAST_KV))[0]["reason"] == "lookup_fail" and not bot.sent
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([order(status="finished", start_ago=9000, minutes=30)]))
        assert [c for c, _ in bot.sent] == ["-555"] and r.alerted_ts is None and r.ended_ts is None
        async with dbm.db() as c:
            assert (await (await c.execute("SELECT COUNT(*) n FROM twap_runs")).fetchone())["n"] == 0
        # ...ama bekleme içinde HL alt emri dolumu olan dizi eskisi gibi sorgusuz benimsenir
        await _fresh()
        bot = Bot()
        await dbm.alert_log("twap", f"PUMP:{A}:buy", "")
        feed("PUMP", A, "buy", 20, 15_000, native=True)
        col = Col([])
        await tl.evaluate(cfg, Notifier(cfg, bot), None, col)
        assert not bot.sent and not col.calls and tl.REG.runs[("PUMP", A, "buy")].alerted_ts
        # 5) /ignore ile elenen adres ('manual') → yok
        await _fresh()
        async with dbm.db() as c:
            await c.execute("INSERT INTO addresses(address, first_seen, entity) VALUES(?,?,'manual')", (A, dbm.now()))
            await c.commit()
        feed("PUMP", A, "buy", 20, 15_000)
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        assert not bot.sent and ht.STATS["reasons"] == {"ignored": 1}
        # 6) ince spot çifti → yok (büyük olsa da)
        await _fresh()
        await dbm.kv_set("spot_top_coins", {"ts": dbm.now(), "vols": {"@107": 400_000}})
        feed("@107", A, "buy", 20, 80_000)                       # $1.6M
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        assert not bot.sent and ht.STATS["reasons"] == {"spot_thin": 1}, ht.STATS
        # 7) hacim ölçümü bayat (> 1 sa) → hız kuralı işlemez (yalnız 'büyük' yolu)
        await _fresh()
        await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"PUMP": {"m": PX, "oi": 1, "f": 0, "v": 2e6, "p": None}},
                                           "ts": dbm.now() - 7200})
        feed("PUMP", A, "buy", 20, 15_000)
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        assert not bot.sent and ht.STATS["reasons"] == {"no_vol": 1}, ht.STATS
        print("✅ inceleme) tavan → karar yok; eski önbellek → zorla sorgu → HL emri; sorgu düşerse yok; sıfır hash"
              " dilim → yok; dizi öncesi bitmiş emir → gizli; örtüşen emir → yok; /ignore, ince spot, bayat hacim")
    asyncio.run(run())


def test_one_alert_per_operation():
    async def run():
        await _fresh()
        cfg = _cfg()
        cfg.twap_alert_cooldown = 0                              # yalnız operasyon hafızası sınansın
        bot = Bot()
        feed("PUMP", A, "buy", 20, 15_000)
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        assert len(bot.sent) == 1 and len(_q("LOG-HTWAP")) == 1 and ht.STATS["considered"] == 1
        # aynı dizi sonraki turlarda: dizi başına sayılır (tur başına değil)
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        assert ht.STATS["considered"] == 1 and ht.STATS["reasons"] == {"same_op": 1}
        # twaplive diziyi yeniden başlattı (4 sa penceresi) ama adres susmadı → aynı operasyon: uyarı/🧪 yok
        del tl.REG.runs[("PUMP", A, "buy")]
        tl._lookup_cache.clear()
        feed("PUMP", A, "buy", 20, 15_000, t_end=dbm.now())
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        assert len(bot.sent) == 1 and len(_q("LOG-HTWAP")) == 1 and ht.STATS["considered"] == 2
        # restart: operasyon hafızası kv'den geri gelir
        await ht.save_stats()
        ht.reset()
        del tl.REG.runs[("PUMP", A, "buy")]
        tl._lookup_cache.clear()
        feed("PUMP", A, "buy", 20, 15_000, t_end=dbm.now())
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        assert len(bot.sent) == 1 and ht.STATS["reasons"] == {"same_op": 1}, ht.STATS
        # 30 dk'dan uzun sessizlik → YENİ operasyon: yeni uyarı + yeni 🧪
        ht.OPS[("PUMP", A, "buy")]["last"] = dbm.now() - 4000
        del tl.REG.runs[("PUMP", A, "buy")]
        tl._lookup_cache.clear()
        feed("PUMP", A, "buy", 20, 15_000, t_end=dbm.now())
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        assert len(bot.sent) == 2 and len(_q("LOG-HTWAP")) == 2, (bot.sent, _q("LOG-HTWAP"))
        print("✅ operasyon) tek uyarı + tek 🧪 / operasyon (dizi yeniden başlasa da, restart'ta da); 30 dk sessizlik"
              " → yeni operasyon; /tani dizi başına sayar")
    asyncio.run(run())


def test_message_ratio_and_send_failure():
    async def run():
        m = {"coin": "BTC", "address": A, "side": "buy", "n": 20, "avg_slice": 75_000, "median_gap": 30,
             "taker_pct": 100.0, "total": 1_500_000, "dur": 570, "rate_day": 216e6, "px_first": 60_000,
             "px_last": 60_100, "px_chg_pct": 0.17}
        t = lambda x: fmt.hidden_twap(m, {"rate_x": x, "pos": {"none": True}})   # noqa: E731
        assert "<b>7.2×</b>'i" in t(7.2) and "<b>%50.0</b>'i" in t(0.5) and "<b>%0.6</b>'i" in t(0.00576)
        assert "<b>%0.1</b>'inden az" in t(0.0002) and "0.0×" not in t(0.0002) + t(0.00576)
        assert "hacminin" not in t(None)
        # gruba gönderilemedi → bekleme yanmaz, /tani uyarır, sonraki tur yeniden dener
        await _fresh()
        cfg = _cfg()

        class Down(Bot):
            async def send(self, text, chat_id=None):
                return False
        feed("PUMP", A, "buy", 20, 15_000)
        await tl.evaluate(cfg, Notifier(cfg, Down()), None, Col([]))
        assert ht.STATS["reasons"] == {"failed": 1} and await _kind("htwap") == 0
        line = await ht.diag_line(cfg)
        assert "⚠ HIDDEN_TWAP_CHAT_ID'ye gönderilemiyor" in line and "gönderilemedi (bot grupta mı" in line, line
        bot = Bot()
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        assert [c for c, _ in bot.sent] == ["-555"] and "grup: HIDDEN_TWAP_CHAT_ID" in await ht.diag_line(cfg)
        assert len(_q("LOG-HTWAP")) == 1, "🧪 ilk turda bir kez (gönderimden bağımsız)"
        assert set(ht.REASON_TR) >= {"ok", "big", "small", "slow", "maker", "two_sided", "no_vol", "cooldown", "off",
                                     "no_chat", "spot_thin", "ignored", "native", "native_order", "no_data",
                                     "same_op", "failed"}
        print("✅ mesaj/hata) oran 1× altında yüzde (0.0× yok); gruba gidemezse bekleme yanmaz, /tani uyarır, sonra gider")
    asyncio.run(run())


def test_ended_claim_first_ops_load_and_touch():
    async def run():
        cfg = _cfg()
        # durmuş dizi (son dilim 25 dk önce): "dilimliyor" iddiası yok, 🧪 geç karar yok
        await _fresh()
        bot = Bot()
        feed("PUMP", A, "buy", 20, 15_000, t_end=dbm.now() - 1500)
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        assert not bot.sent and ht.STATS["reasons"] == {"ended": 1} and not _q("LOG-HTWAP"), ht.STATS
        # teslimden SONRA DB hatası: aynı mesaj ikinci kez gitmez (talep gönderimden önce)
        await _fresh()
        bot = Bot()
        feed("PUMP", A, "buy", 20, 15_000)
        orig = ht.alert_log

        async def boom(*a, **k):
            raise RuntimeError("database is locked")
        ht.alert_log = boom
        try:
            await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        finally:
            ht.alert_log = orig
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        assert len(bot.sent) == 1 and ht.STATS["reasons"].get("same_op") == 1, (bot.sent, ht.STATS)
        # operasyon hafızası okunamazsa: karar yok, kv ezilmez; sonraki tur okur
        await _fresh()
        await dbm.kv_set(ht.OPS_KV, {f"PUMP|{A}|buy": {"start": dbm.now() - 9000, "last": dbm.now() - 60,
                                                       "alerted": dbm.now() - 8000, "lab": 1}})
        bot = Bot()
        feed("PUMP", A, "buy", 20, 15_000)
        okv = ht.kv_get

        async def flaky(k):
            if k == ht.OPS_KV:
                raise RuntimeError("database is locked")
            return await okv(k)
        ht.kv_get = flaky
        try:
            await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        finally:
            ht.kv_get = okv
        assert not bot.sent and ht.STATS["reasons"] == {"no_data": 1} and not ht._OPS_LOADED
        assert (await dbm.kv_get(ht.OPS_KV))[f"PUMP|{A}|buy"]["alerted"], "kv ezilmedi"
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        assert not bot.sent and ht.STATS["reasons"] == {"same_op": 1} and not _q("LOG-HTWAP")
        # touch: consider çağrılmayan turlarda da (düzensiz) operasyonun son dilimi dolumlardan ilerler
        k = ("PUMP", A, "buy")
        ht.OPS[k]["last"] = dbm.now() - 1000
        tl.REG.runs[k].last_ts = dbm.now() - 3
        await ht.save_stats()
        assert ht.OPS[k]["last"] == dbm.now() - 3 or ht.OPS[k]["last"] >= dbm.now() - 5
        print("✅ durmuş dizi iddiası yok; teslim sonrası DB hatası ikinci mesaj üretmez; operasyon hafızası okunamazsa"
              " karar yok (kv ezilmez); son dilim dolumlardan ilerler")
    asyncio.run(run())


def test_only_own_group_never_normal_channels():
    """Kullanıcı 13.10: "bu gizli twap normal kanala gelmeyecek, sadece gizli twap variable'ı olan chate gidecek"."""
    async def run():
        # değişken BOŞ: kripto kanalı + ayna tanımlı olsa da hiçbir yere gitmez; ölçüm + 🧪 sürer
        await _fresh()
        cfg = _cfg(own="")
        cfg.twap_mirror_chat_id = "-777"
        bot = Bot()
        feed("PUMP", A, "buy", 20, 15_000)
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        assert not bot.sent and tl.MIRROR["sent"] == 0, bot.sent
        assert ht.STATS["reasons"] == {"no_chat": 1} and await _kind("htwap") == 0, "bekleme yakılmaz"
        assert len(_q("LOG-HTWAP")) == 1 and _f(_q("LOG-HTWAP")[0])["kapi"] == "ok"
        assert "⚠ HIDDEN_TWAP_CHAT_ID yok → mesaj gönderilmiyor" in await ht.diag_line(cfg)
        assert "🕵️ gizli twap: HIDDEN_TWAP_CHAT_ID yok" in tl.decision_line((await dbm.kv_get(tl.LAST_KV))[0])
        # değişken eklendi → süren dizi bir sonraki turda YALNIZ gruba
        cfg.hidden_twap_chat_id = "-555"
        tl._lookup_cache.clear()
        feed("PUMP", A, "buy", 2, 15_000, t_end=dbm.now())
        await tl.evaluate(cfg, Notifier(cfg, bot), None, Col([]))
        assert [c for c, _ in bot.sent] == ["-555"] and tl.MIRROR["sent"] == 0, bot.sent
        assert await _kind("htwap") == 1 and "grup: HIDDEN_TWAP_CHAT_ID" in await ht.diag_line(cfg)
        # kripto kanalı tanımsız olsa da gruba gider
        await _fresh()
        cfg2 = _cfg(chat="")
        bot2 = Bot()
        feed("PUMP", A, "buy", 20, 15_000)
        await tl.evaluate(cfg2, Notifier(cfg2, bot2), None, Col([]))
        assert [c for c, _ in bot2.sent] == ["-555"]
        # normal TWAP mesajları bu gruba GİTMEZ (yalnız TWAP kanalı + ayna)
        await _fresh()
        cfg3 = _cfg()
        cfg3.twap_mirror_chat_id = "-777"
        bot3 = Bot()
        feed("PUMP", B, "buy", 40, 50_000)
        order = [{"time": dbm.now() - 1300, "state": {"coin": "PUMP", "user": B, "side": "B", "sz": "400000",
                                                      "executedSz": "200000", "executedNtl": "2000000",
                                                      "minutes": 60, "timestamp": (dbm.now() - 1300) * 1000,
                                                      "reduceOnly": False, "randomize": False},
                  "status": {"status": "activated"}, "twapId": 9}]
        await tl.evaluate(cfg3, Notifier(cfg3, bot3), None, Col(order))
        assert bot3.sent and "-555" not in [c for c, _ in bot3.sent], bot3.sent
        from app.telegram.bot import TelegramBot
        assert "-555" in TelegramBot._own_chats(type("B", (), {"cfg": cfg})())
        print("✅ grup) yalnız HIDDEN_TWAP_CHAT_ID: boşsa hiçbir yere (kripto kanalı/ayna değil, bekleme yakılmaz),"
              " eklenince sonraki turda; kripto kanalı yoksa da; normal TWAP gruba gitmez; bot o grupta komut alır")
    asyncio.run(run())


def test_equity_own_group_and_propr_note():
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
        assert len(bot.sent) == 1 and bot.sent[0][0] == "-555", ("hisse de ana sohbete değil gruba", bot.sent)
        txt = bot.sent[0][1]
        assert "<b>CBRS</b>" in txt and "✅ <b>PROPR'da listeli</b>" in txt and "her 32 sn" in txt, txt
        print("✅ hisse) CBRS benzeri dizi → gizli TWAP grubu (ana sohbet değil) + PROPR notu")
    asyncio.run(run())


# ---------------- kablolama ----------------

def test_wiring():
    from app import notify
    from app.lab import specs
    src = lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read()   # noqa: E731
    assert notify.KINDS["htwap"] == ("", "🕵️ Gizli TWAP (emirsiz dilimleme)", "high") and "htwap" not in notify.PUBLIC_KINDS
    cfg = Config()
    assert cfg.hidden_twap_enabled and cfg.hidden_twap_min_usd == 250_000 and notify.kind_enabled(cfg, "htwap")
    assert cfg.hidden_twap_rate_x == 0.5 and cfg.hidden_twap_big_usd == 1_000_000 and cfg.hidden_twap_taker_min_pct == 50
    # kullanıcı 13.10: "ayarlara sokma beni, Railway'den değişken eklerim" → ayar sayfasında / Bildirimler'de yok
    names = ("hidden_twap_enabled", "hidden_twap_min_usd", "hidden_twap_rate_x", "hidden_twap_big_usd",
             "hidden_twap_taker_min_pct", "hidden_twap_chat_id", "notify_htwap")
    assert not [f for f in EDITABLE_FIELDS if f in names or "htwap" in f or "hidden_twap" in f]
    assert not [f for f, v in EDITABLE_FIELDS.items() if "Gizli TWAP" in v["group"] + v["label"] + v["desc"]]
    assert not hasattr(cfg, "notify_htwap")
    # eski kayıtlı ayar (dashboard override) artık yok sayılır; env okunur
    cfg.apply_overrides({"hidden_twap_min_usd": "1", "hidden_twap_enabled": "0", "notify_htwap": "0"})
    assert cfg.hidden_twap_min_usd == 250_000 and cfg.hidden_twap_enabled and not cfg.overrides
    env = {"HIDDEN_TWAP_CHAT_ID": " -100123 ", "HIDDEN_TWAP_ENABLED": "0", "HIDDEN_TWAP_MIN_USD": "400000",
           "HIDDEN_TWAP_RATE_X": "1.5", "HIDDEN_TWAP_BIG_USD": "0", "HIDDEN_TWAP_TAKER_MIN_PCT": "70"}
    old = {k: os.environ.get(k) for k in env}
    try:
        os.environ.update(env)
        e = Config()
        assert (e.hidden_twap_chat_id, e.hidden_twap_enabled, e.hidden_twap_min_usd, e.hidden_twap_rate_x,
                e.hidden_twap_big_usd, e.hidden_twap_taker_min_pct) == ("-100123", False, 400_000, 1.5, 0, 70)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    async def notif():                                   # /bildirimler: düğme satırı yok
        await _fresh()
        from app.telegram.bot import TelegramBot
        out = []

        async def send(text, chat_id=None):
            out.append(text)
        await TelegramBot._cmd_notifications(type("B", (), {"cfg": Config(), "send": staticmethod(send)})(), "1")
        assert out and "Bildirim ayarları" in out[0] and "Gizli TWAP" not in out[0] and "⏳ TWAP" in out[0], out
    asyncio.run(notif())
    rule = next(r for r in specs.RULES if r["id"] == "LOG-HTWAP")
    assert rule["evidence"] == "log" and rule["horizons_s"] == [900, 3600, 14400, 86400]
    readme, env_ex = src("README.md"), src(".env.example")
    assert "### 🕵️ Gizli TWAP — HL emri olmadan dilimleme" in readme and "LOG-HTWAP" in readme
    for k in ("HIDDEN_TWAP_ENABLED=1", "HIDDEN_TWAP_MIN_USD=250000", "HIDDEN_TWAP_RATE_X=0.5",
              "HIDDEN_TWAP_BIG_USD=1000000", "HIDDEN_TWAP_TAKER_MIN_PCT=50"):
        assert k in env_ex, k
    assert "TEK GEREKEN BU" in env_ex and env_ex.index("HIDDEN_TWAP_CHAT_ID=") < env_ex.index("HIDDEN_TWAP_ENABLED=")
    assert "**Ayarlar sayfasında yok**" in readme and "eşikler Ayarlar → 🕵️ Gizli TWAP" not in readme
    assert "Bildirimler → 🕵️ Gizli TWAP" not in readme
    assert "hiddentwap.diag_line(cfg)" in src("app/diag.py")
    assert hasattr(fmt, "hidden_twap")
    print("✅ kablolama) tür (fan-out yok), ayar sayfasında yok (env okunur, eski ayar yok sayılır), .env, README, LOG-HTWAP, /tani")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
