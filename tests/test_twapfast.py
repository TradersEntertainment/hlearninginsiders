"""⚡ Hızlı TWAP (ilk dilim) + TWAP ayna grubu.

Kullanıcı (12.10): "5 dk'lık TWAP coini çok sert oynatıyor, 1. dakikada yakalayabilir miyiz" ve "TWAP'lar
normal kanallara gelmeye devam etsin, ayrıca bu gruba da gelsin" (TWAP_MIRROR_CHAT_ID).

Pinlenenler:
  • sıfır hash'li dolum TWAP dilimidir, sahibi TAKER; aynı saniyenin parçaları tek dilim; sıfırsız hash yok sayılır
  • ön süzgeç (G/Ç yok): iki kapıdan birini geçemeyecek dilim için sorgu yok (BTC'de $5K dilim, hacim yok)
  • ilk dilim → tek emir sorgusu → kapı → ana kanal + ayna; 5 dk'lık emir dakika hızı kuralıyla geçer
  • eski yolun alanlarına (order / alerted_ts / lookup önbelleği / 'twap' işareti) DOKUNULMAZ:
    🧪 LOG-TWAP olayı hızlı bildirim olsa da olmasa da AYNI; hızlı karar ayrı aile LOG-TWAPF
  • eski yol aynı emri sonra aday görürse tekrar mesaj yok; kısa TWAP'ın 🏁 bitiş notu emir süresi dolunca
  • emir yoksa (likidasyon) bildirim yok; mm/vault, bekleme, TWAP bildirimi kapalı → yok
  • ayna: boşsa hiç gönderilmez; ana hedefle aynıysa ikinci kopya yok; ana gönderim başarısız olup her tur
    yeniden denense de ayna BİR kez; ana kripto kanalı yokken yalnız ayna; ayna grubu botun kendi sohbeti
  • inceleme düzeltmeleri: eski yolun atlaması EMRE bağlı (aynı adresin sonraki farklı emri eski yoldan yine
    gider); ana kanal düşerse hızlı yol "bildirildi" saymaz ve aynaya da yazmaz (eski yol ikisini dener);
    iki yol aynı emri aynı anda gönderemez; bitiş notu emir anahtarıyla tekil; aynı saniyenin parçaları ön
    süzgeçte yeniden tartılır; geç gelen başka adresin snapshot'ı bekleyen sorguya düşmez
"""
import asyncio
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-twapfast.db")
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.hl import universe as uni  # noqa: E402
from app.notify import KINDS, PUBLIC_KINDS, Notifier  # noqa: E402
from app.radar import twapfast as tf  # noqa: E402
from app.radar import twaplive as tl  # noqa: E402
from app.telegram import format as fmt  # noqa: E402

A, B = "0x" + "a" * 40, "0x" + "b" * 40
ZERO = "0x" + "0" * 64
PX = 0.005                                     # PUMP benzeri ucuz coin
DV = 20_000_000                                # 24s hacim $20M → dakika ortalaması ~$13.9K


class Bot:
    def __init__(self, fail_chats=()):
        self.sent, self.fail = [], set(fail_chats)

    async def send(self, text, chat_id=None):
        if (chat_id or "") in self.fail:
            return False
        self.sent.append((chat_id, text))
        return True


class Col:
    def __init__(self, hist):
        self.hist, self.calls, self._twap_waiters = hist, [], {}

    async def fetch_twap_history(self, addr, timeout=6.0):
        self.calls.append(addr)
        return self.hist() if callable(self.hist) else self.hist


def twap_order(started, minutes=5, plan=600_000, ex_frac=0.05, status="activated", coin="PUMP", side="B"):
    sz = plan / PX
    return {"state": {"coin": coin, "side": side, "sz": str(sz), "executedSz": str(sz * ex_frac),
                      "executedNtl": str(sz * ex_frac * PX), "minutes": minutes, "timestamp": started * 1000,
                      "randomize": False, "reduceOnly": False, "user": A},
            "status": {"status": status}, "time": started * 1000}


def _cfg(chat="-100", mirror="-200"):
    cfg = Config()
    cfg.telegram_chat_id = "111"
    cfg.crypto_chat_id = chat
    cfg.twap_mirror_chat_id = mirror
    cfg.notify_twap = True
    cfg.twap_live_enabled = True
    cfg.twap_fast_enabled = True
    cfg.twap_alert_min_usd = 1_000_000
    cfg.twap_alert_min_left_usd = 1_000_000
    cfg.twap_alert_vol_pct = 5
    cfg.twap_alert_big_usd = 0
    cfg.twap_alert_min_slices = 10
    cfg.twap_lookup_min_usd = 50_000
    cfg.twap_alert_cooldown = 21600
    cfg.twap_fast_min_slice_usd = 1000
    cfg.twap_fast_short_max_min = 60
    cfg.twap_fast_short_min_usd = 250_000
    cfg.twap_fast_rate_x = 2.0
    cfg.twap_fast_lookups_min = 30
    cfg.twap_follow_enabled = True
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    cfg.lab_k_budget, cfg.lab_alpha, cfg.lab_epoch = 4, 0.05, 1
    return cfg


async def _fresh(cfg, coin="PUMP", dv=DV):
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "tf.db"))
    tl.REG.clear()
    tl._lookup_cache.clear()
    tl.VOL_CACHE.update(ts=0, v={})
    for k in tl.MIRROR:
        tl.MIRROR[k] = 0
    tf.F.clear()
    tf.F.cfg = cfg
    await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {coin: {"m": PX, "oi": 1, "f": 0, "v": dv, "p": None},
                                             "BTC": {"m": 60_000, "oi": 1, "f": 0, "v": 2e9, "p": None}},
                                       "ts": dbm.now()})
    tf.F.vols = await tl.volumes(None)
    from app.lab import registry, specs
    registry._Q.clear()
    registry.ACTIVE.clear()
    registry.DERIVED.clear()
    registry._SEEN.clear()
    registry._DAY.clear()
    rules = [r for r in specs.RULES if r["id"] in ("LOG-TWAP", "LOG-TWAPF", "AKI-TWAP-F")]
    await registry.sync(cfg, ts=dbm.now() - 86400, rules=rules)


def _q(rid):
    from app.lab import registry
    return [e for e in registry._Q if e["rule_id"] == rid]


async def _kind(kind):
    async with dbm.db() as c:
        return (await (await c.execute("SELECT COUNT(*) n FROM alerts_log WHERE kind=?", (kind,))).fetchone())["n"]


async def _drain(cfg, notifier, col, t=None):
    tasks: set = set()
    n = await tf.dispatch(cfg, None, notifier, col, int(t or dbm.now()), tasks)
    if tasks:
        await asyncio.gather(*tasks)
    return n


def test_zero_hash_taker_and_prefilter():
    async def run():
        from app.hl.collector import Collector
        cfg = _cfg()
        await _fresh(cfg)
        col = Collector(cfg, None)
        col.valid_coins = {"PUMP", "BTC"}
        col.crypto_coins = {"PUMP", "BTC"}
        now = dbm.now()
        sz1, sz2 = 6_000_000, 2_000_000                       # tek dilim iki seviyeye bölündü: $30K + $10K
        data = [{"coin": "PUMP", "side": "B", "px": str(PX), "sz": str(sz1), "time": (now - 1) * 1000,
                 "tid": "z1", "hash": ZERO, "users": [A, B]},
                {"coin": "PUMP", "side": "B", "px": str(PX), "sz": str(sz2), "time": (now - 1) * 1000 + 200,
                 "tid": "z2", "hash": ZERO, "users": [A, B]},
                {"coin": "PUMP", "side": "A", "px": str(PX), "sz": "9000000", "time": now * 1000, "tid": "n1",
                 "hash": "0x" + "1" * 64, "users": [A, B]},                    # imzalı işlem: TWAP değil
                {"coin": "BTC", "side": "A", "px": "60000", "sz": "0.1", "time": now * 1000, "tid": "z3",
                 "hash": ZERO, "users": [A, B]}]                                # BTC'de $6K dilim: hiçbir kapı
        await col._handle(json.dumps({"channel": "trades", "data": data}))
        p = tf.F.pending[("PUMP", A, "buy")]
        assert p["n"] == 1 and abs(p["usd"] - (sz1 + sz2) * PX) < 1e-6 and p["queued"], p
        assert ("PUMP", B, "sell") not in tf.F.pending, "maker TWAP sahibi değil"
        assert ("BTC", B, "sell") in tf.F.pending and not tf.F.pending[("BTC", B, "sell")]["queued"]
        assert tf.F.stats["pre_low"] == 1 and tf.F.stats["queued"] == 1 and tf.F.stats["zero"] == 3
        assert tl.REG.runs[("PUMP", A, "buy")].n == 1, "eski sayaç eskisi gibi"
        assert tf.is_twap_hash(ZERO) and not tf.is_twap_hash("0x" + "0" * 63 + "1") and not tf.is_twap_hash(None)
        assert not tf.is_twap_hash("0x")
        # ön süzgeç sayıları: hacim yok → sorgu yok; ince spot → yok; uzun TWAP olabilecek dilim → sorulur
        assert tf.prefilter(cfg, "XYZ", 50_000, None, now) == "pre_novol"
        assert tf.prefilter(cfg, "PUMP", 1_000, (DV, now), now) == ""            # 24 sa'lik $1M+ olabilir
        assert tf.prefilter(cfg, "BTC", 50_000, (2e9, now), now) == "pre_low"
        cfg.spot_twap_min_day_vol = 1_000_000
        assert tf.prefilter(cfg, "@107", 50_000, (300_000, now), now) == "pre_spot_thin"
    asyncio.run(run())
    print("✅ hızlı twap) sıfır hash = TWAP dilimi, sahibi taker; aynı saniye tek dilim; ön süzgeç sorguyu ayıklar")


def test_first_slice_alert_mirror_and_end_note():
    """5 dk'lık $600K emir (dakikada $120K = ortalamanın ~8.6 katı): ilk dilimde ana kanal + ayna; eski
    yolun alanları boş; bitişte 🏁 (ana + ayna) bir kez; 🧪 LOG-TWAPF; LOG-TWAP eski yolun kendi anında."""
    async def run():
        cfg = _cfg()
        await _fresh(cfg)
        now = dbm.now()
        started = now - 340                                    # emir 5 dk 40 sn önce verildi
        state = {"st": "activated"}
        col = Col(lambda: [twap_order(started, status=state["st"],
                                      ex_frac=0.05 if state["st"] == "activated" else 1.0)])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        tl.observe("PUMP", A, "buy", PX, 12_000_000, 60_000, started + 2, 1)    # eski sayaç (collector sırası)
        tf.observe("PUMP", A, "buy", PX, 12_000_000, 60_000, started + 2, 1000)
        assert await _drain(cfg, notifier, col) == 1
        assert [c for c, _ in bot.sent] == ["-100", "-200"], bot.sent
        text = bot.sent[0][1]
        assert bot.sent[1][1] == text, "ayna = birebir kopya"
        assert "⏳ <b>TWAP başladı</b> · ⚡ <b>HIZLI · 5 dk</b> · <b>PUMP</b>" in text and "plan <b>$600K</b>" in text
        assert "katı</b>" in text and "dakikada ~<b>$120K</b>" in text
        assert "🔔 iptal edilirse ⛔ buraya otomatik yazılır" in text and "/takip_" not in text, "⚡: tuş yok"
        assert "🏁" not in text, "12.10: normal bitiş kanala yazılmaz (varsayılan)"
        async with dbm.db() as c:
            assert (await (await c.execute("SELECT COUNT(*) n FROM track_offers")).fetchone())["n"] == 0
        assert "⚡ dakika hızı kuralıyla" in text and "bu hızla" not in text
        run_ = tl.REG.runs[("PUMP", A, "buy")]
        assert not hasattr(run_, "fast_ts") and run_.order is None and run_.alerted_ts is None \
            and run_.lookup_ts is None, "eski yolun sayacına dokunulmaz"
        assert tl._lookup_cache == {}, "eski yolun emir önbelleği"
        rec = tf.F.open[("PUMP", A, "buy")]
        assert rec["started"] == started and rec["end_at"] == started + 300 and rec["n"] == 1
        assert await _kind("twap") == 0 and await _kind("twap_fast") == 1 and await _kind("twap_mirror") == 1
        f = _q("LOG-TWAPF")
        assert len(f) == 1 and f[0]["gated"] and json.loads(f[0]["features"])["kapi"] == "short", f
        assert not _q("LOG-TWAP"), "eski yolun kaydı yalnız kendi anında"
        st = tf.F.snapshot(now)
        assert st["alerts"] == 1 and st["lat_med"] is not None and st["lat_med"] >= 340
        # bilinen emir: sonraki dilimler yeniden sorgulanmaz
        tf.observe("PUMP", A, "buy", PX, 12_000_000, 60_000, started + 32, 1000)
        assert tf.F.stats["known_skip"] == 1 and await _drain(cfg, notifier, col) == 0
        # kalan 10 dilim (hızlı kayıt sayar, sorgu yok); emir 'finished' → hızlı yolun 🏁 notu bir kez
        for i in range(1, 11):
            tl.observe("PUMP", A, "buy", PX * (1 + i / 100), 12_000_000, 60_000, started + 2 + 30 * i, 1)
            tf.observe("PUMP", A, "buy", PX * (1 + i / 100), 12_000_000, 60_000, started + 2 + 30 * i, 1000)
        assert rec["n"] == 11 and abs(rec["px_last"] - PX * 1.1) < 1e-12
        state["st"] = "finished"
        rec["lookup_ts"] = 0                                       # zaman geçti (hızlı sorgu 5 dk önceydi)
        # 12.10 kullanıcı: "iptal etmedikçe bildirim atma" — normal bitiş varsayılan olarak SESSİZ
        assert await tf.lifecycle(cfg, notifier, col, now) == 0 and rec["ended"]
        assert not [t for _, t in bot.sent if "TWAP bitti" in t] and len(bot.sent) == 2
        assert ("PUMP", A, "buy") not in tf.F.known, "biten emrin yerine yeni emir sorulabilir"
        # ayar açılırsa (🏁 bitti notu) bir kez yazılır — ⚡ rozetli, fiyat değişimiyle
        cfg.twap_alert_finish_note = True
        rec["ended"], rec["lookup_ts"] = 0, 0
        assert await tf.lifecycle(cfg, notifier, col, now) == 1
        ends = [(c, t) for c, t in bot.sent if "TWAP bitti" in t]
        assert [c for c, _ in ends] == ["-100", "-200"], bot.sent
        assert "+10.0%" in ends[0][1] and "🏁 <b>TWAP bitti</b> · ⚡ <b>HIZLI · 5 dk</b>" in ends[0][1], ends[0][1]
        cfg.twap_alert_finish_note = False
        # eski yol: 11 dilim + 5 dk → aday → emir 'finished' → LOG-TWAP eskisi gibi (st=finished, order_done),
        # mesaj yok (bildirmediği turun bitiş notu yok)
        n = len(bot.sent)
        out = await tl.evaluate(cfg, notifier, None, col)
        lt = _q("LOG-TWAP")
        assert len(lt) == 1 and json.loads(lt[0]["features"])["st"] == "finished", lt
        assert out["order_done"] == 1 and await _kind("twap") == 0 and len(bot.sent) == n
        assert await tf.lifecycle(cfg, notifier, col, now + 60) == 0, "bitiş notu bir kez"
    asyncio.run(run())
    print("✅ hızlı twap) ilk dilimde ana + ayna; eski yolun alanları boş; 🏁 bir kez; LOG-TWAPF + LOG-TWAP ayrı")


def test_cancel_note_and_wait_while_activated():
    """İptal: dilimler 90 sn kesilince emir sorulur; HL 'activated' derken not yok (bekler), 'terminated' → ⛔."""
    async def run():
        cfg = _cfg()
        await _fresh(cfg)
        now = dbm.now()
        started = now - 200
        state = {"st": "activated"}
        col = Col(lambda: [twap_order(started, minutes=10, status=state["st"])])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        tf.observe("PUMP", A, "buy", PX, 12_000_000, 60_000, started + 2, 1000)
        await _drain(cfg, notifier, col)
        assert len(bot.sent) == 2
        for i in range(1, 4):                                    # 1.5 dk sonra kesildi
            tf.observe("PUMP", A, "buy", PX, 12_000_000, 60_000, started + 2 + 30 * i, 1000)
        rec = tf.F.open[("PUMP", A, "buy")]
        rec["lookup_ts"] = 0
        assert await tf.lifecycle(cfg, notifier, col, now) == 0 and not rec["ended"], "activated: bekle"
        state["st"] = "terminated"
        # ⚡ HIZLI: son dilimden 46 sn sonra (bir dilim gecikti) ve son sorgudan 31 sn sonra yeniden sorulur
        rec["last_ts"], rec["lookup_ts"] = now - 46, now - 31
        assert await tf.lifecycle(cfg, notifier, col, now) == 1
        ends = [(c, t) for c, t in bot.sent if "TWAP iptal edildi" in t]
        assert [c for c, _ in ends] == ["-100", "-200"], bot.sent
        assert "⛔ <b>TWAP iptal edildi</b> · ⚡ <b>HIZLI · 10 dk</b>" in ends[0][1], ends[0][1]
        assert not _q("LOG-TWAP"), "eski yol aday değil → kaydı yok (bugünkü gibi)"
    asyncio.run(run())
    print("✅ hızlı twap) iptal: ⚡ HIZLI'da dilim 45 sn gecikince sorulur; activated iken bekler; terminated → ⛔")


def test_quick_cancel_slow_path_refresh():
    """Eski yolun bildirdiği ⚡ HIZLI emir: dakikada bir durum sorgusu (önbelleğe YAZMADAN) → iptal → ⛔ kanal + ayna.
    Uzun emirde 10 dk'lık eski düzen sürer."""
    async def run():
        cfg = _cfg()
        cfg.twap_fast_enabled = False                            # yalnız eski yol
        await _fresh(cfg)
        now = dbm.now()
        started = now - 400
        state = {"st": "activated"}
        col = Col(lambda: [twap_order(started, minutes=45, plan=3_000_000, ex_frac=0.2, status=state["st"])])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        for i in range(13):
            tl.observe("PUMP", A, "buy", PX, 10_000_000, 50_000, started + 2 + 30 * i, 1)
        out = await tl.evaluate(cfg, notifier, None, col)
        assert out["alerted"] == 1 and [c for c, _ in bot.sent] == ["-100", "-200"]
        assert "⚡ <b>HIZLI · 45 dk</b>" in bot.sent[0][1] and "🔔 iptal edilirse ⛔" in bot.sent[0][1]
        assert "/takip_" not in bot.sent[0][1]
        cache = dict(tl._lookup_cache)
        run_ = tl.REG.runs[("PUMP", A, "buy")]
        state["st"] = "terminated"
        run_.lookup_ts = now - 30
        await tl.evaluate(cfg, notifier, None, col)
        assert len(bot.sent) == 2, "60 sn dolmadan sorgu yok"
        run_.lookup_ts = now - 61
        out = await tl.evaluate(cfg, notifier, None, col)
        ends = [(c, t) for c, t in bot.sent if "TWAP iptal edildi" in t]
        assert [c for c, _ in ends] == ["-100", "-200"] and out.get("quick_lookups") == 1, (bot.sent, out)
        assert tl._lookup_cache == cache, "eski yolun emir önbelleği değişmedi"
    asyncio.run(run())
    print("✅ ⚡ HIZLI) eski yoldan bildirilen kısa emir: dakikada bir sorgu → iptal ⛔ kanal + ayna; önbellek aynı")


def test_slow_path_after_fast_same_lab_event_no_resend():
    """30 dk'lık $3M emir (hacmin %15'i): hızlı bildirim → eski yol 6 dk sonra aday: aynı LOG-TWAP olayı
    (hızlı bildirim olmasaydı neyse o), mesaj YOK, 'twap' işareti ve alerted_ts eski yolun yazacağı gibi."""
    async def scenario(fast: bool):
        cfg = _cfg()
        await _fresh(cfg)
        now = dbm.now()
        started = now - 365
        col = Col(lambda: [twap_order(started, minutes=30, plan=3_000_000, ex_frac=0.2)])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        tl.observe("PUMP", A, "buy", PX, 10_000_000, 50_000, started + 2, 1)
        if fast:
            tf.observe("PUMP", A, "buy", PX, 10_000_000, 50_000, started + 2, 1000)
            await _drain(cfg, notifier, col)
            assert len(bot.sent) == 2 and "TWAP başladı" in bot.sent[0][1] and "⚡ <b>HIZLI · 30 dk</b>" in bot.sent[0][1]
        for i in range(1, 13):
            tl.observe("PUMP", A, "buy", PX, 10_000_000, 50_000, started + 2 + 30 * i, 1)
        n0 = len(bot.sent)
        out = await tl.evaluate(cfg, notifier, None, col)
        ev = _q("LOG-TWAP")
        assert len(ev) == 1 and ev[0]["gated"], ev
        run_ = tl.REG.runs[("PUMP", A, "buy")]
        return {"feat": json.loads(ev[0]["features"]), "sent": len(bot.sent) - n0, "out": out,
                "twap_marker": await _kind("twap"), "alerted": bool(run_.alerted_ts),
                "aki": len(_q("AKI-TWAP-F"))}
    async def run():
        ctl = await scenario(False)
        got = await scenario(True)
        assert got["feat"] == ctl["feat"], (got["feat"], ctl["feat"])
        assert got["aki"] == ctl["aki"], "türetilmiş kural örneklemi aynı"
        assert ctl["sent"] == 2 and got["sent"] == 0, "eski yol hızlı bildirileni yeniden yazmaz"
        assert got["out"].get("fast_already") == 1 and got["twap_marker"] == 1 and got["alerted"]
        assert ctl["twap_marker"] == 1 and ctl["alerted"]
    asyncio.run(run())
    print("✅ hızlı twap) eski yol: aynı 🧪 LOG-TWAP olayı (+ türetilen), tekrar mesaj yok, işaretler aynı")


def test_review_fixes_order_keyed_skip_primary_fail_race_end_dedupe():
    async def run():
        # (1) hızlı bildirim (5 dk'lık) sonrası AYNI adresin FARKLI uzun emri: hızlı yol beklemede, eski yol yine gönderir
        cfg = _cfg()
        await _fresh(cfg)
        now = dbm.now()
        bot = Bot()
        notifier = Notifier(cfg, bot)
        s1 = now - 2000
        tf.observe("PUMP", A, "buy", PX, 12_000_000, 60_000, s1 + 2, 1000)
        await _drain(cfg, notifier, Col([twap_order(s1)]))
        assert len(bot.sent) == 2
        s2 = now - 400
        long_ = Col([twap_order(s2, minutes=60, plan=3_000_000, ex_frac=0.2)])
        tf.F.known.clear()
        tf.observe("PUMP", A, "buy", PX, 10_000_000, 50_000, s2 + 2, 1000)
        await _drain(cfg, notifier, long_)
        assert tf.F.stats["cooldown"] == 1 and len(bot.sent) == 2, "hızlı yol: 6 sa bekleme"
        for i in range(13):
            tl.observe("PUMP", A, "buy", PX, 10_000_000, 50_000, s2 + 2 + 30 * i, 1)
        out = await tl.evaluate(cfg, notifier, None, long_)
        assert out["alerted"] == 1 and [c for c, _ in bot.sent[2:]] == ["-100", "-200"], bot.sent
        assert "⏳ <b>TWAP</b>" in bot.sent[2][1]
        # (2) ana kanal düşer + ayna başarılı → "bildirildi" sayılmaz, ayna da yazmaz; eski yol ikisini gönderir
        cfg = _cfg()
        await _fresh(cfg)
        tf.SEND_RETRY = 0
        bot = Bot(fail_chats={"-100"})
        notifier = Notifier(cfg, bot)
        s3 = now - 365
        col = Col([twap_order(s3, minutes=30, plan=3_000_000, ex_frac=0.2)])
        tf.observe("PUMP", A, "buy", PX, 10_000_000, 50_000, s3 + 2, 1000)
        await _drain(cfg, notifier, col)
        assert bot.sent == [] and await _kind("twap_fast") == 0 and await _kind("fail:twap_fast") == 1
        bot.fail.clear()
        for i in range(13):
            tl.observe("PUMP", A, "buy", PX, 10_000_000, 50_000, s3 + 2 + 30 * i, 1)
        out = await tl.evaluate(cfg, notifier, None, col)
        assert out["alerted"] == 1 and [c for c, _ in bot.sent] == ["-100", "-200"], bot.sent
        tf.SEND_RETRY = 5
        # (3) yarış: hızlı yol aynı emri gönderirken eski yol bekler (sonraki tur karar verir), çift mesaj yok
        cfg = _cfg()
        await _fresh(cfg)
        bot = Bot()
        notifier = Notifier(cfg, bot)
        for i in range(13):
            tl.observe("PUMP", A, "buy", PX, 10_000_000, 50_000, s3 + 2 + 30 * i, 1)
        tl.SENDING.add(f"PUMP:{A}:buy:{s3}")
        out = await tl.evaluate(cfg, notifier, None, col)
        assert out.get("fast_inflight") == 1 and bot.sent == [] and not tl.REG.runs[("PUMP", A, "buy")].alerted_ts
        tl.SENDING.clear()
        await dbm.alert_log("twap_fast", f"PUMP:{A}:buy:{s3}", "x")      # hızlı yol gönderdi
        out = await tl.evaluate(cfg, notifier, None, col)
        assert out.get("fast_already") == 1 and bot.sent == []
        # (4) bitiş notu emir anahtarıyla tekil: hızlı yol yazdıysa eski yolun bitiş dalı yazmaz
        await dbm.alert_log("twap_endo", f"PUMP:{A}:buy:{s3}", "")
        done = Col([twap_order(s3, minutes=30, plan=3_000_000, ex_frac=1.0, status="finished")])
        tl._lookup_cache.clear()
        tl.REG.runs[("PUMP", A, "buy")].lookup_ts = 0
        await tl.evaluate(cfg, notifier, None, done)
        assert not [t for _, t in bot.sent if "TWAP bitti" in t] and tl.REG.runs[("PUMP", A, "buy")].ended_ts
    asyncio.run(run())
    print("✅ inceleme) eski yol atlaması emre bağlı; ana kanal düşerse hızlı yol saymaz; yarışta tek mesaj; bitiş tekil")


def test_prefilter_fragments_and_stray_snapshot():
    async def run():
        from app.hl.collector import Collector
        cfg = _cfg()
        await _fresh(cfg, dv=200_000_000)                         # dakika ortalaması ~$139K
        now = dbm.now()
        # aynı saniyede iki parça: ilki ($1.5K) ön süzgeçte kalır ($200M hacimde 24 sa'lik emir bile < %5), toplam
        # ($3K → ≤ 24 sa'lik emir ≥ $10.8M ≥ hacmin %5'i) uzun TWAP olabilir → sıraya
        tf.observe("PUMP", A, "buy", PX, 300_000, 1_500, now, 1000)
        assert not tf.F.pending[("PUMP", A, "buy")]["queued"] and tf.F.stats["pre_low"] == 1
        tf.observe("PUMP", A, "buy", PX, 300_000, 1_500, now, 1000)
        assert tf.F.pending[("PUMP", A, "buy")]["queued"] and tf.F.stats["pre_low"] == 1, "dilim başına bir sayaç"
        # hacimden bağımsız eşik açıkken ince spot da sorulur (order_gate 'big'i önce yoklar)
        cfg.twap_alert_big_usd, cfg.spot_twap_min_day_vol = 5e6, 1e6
        assert tf.prefilter(cfg, "@107", 10e6 / 2881, (500_000, now), now) == ""
        # geç gelen BAŞKA adresin snapshot'ı tek bekleyen sorguya düşmez
        col = Collector(cfg, None)
        fut = asyncio.get_running_loop().create_future()
        col._twap_waiters[A] = fut
        col._on_twap_history({"user": B, "isSnapshot": True, "history": [twap_order(now)]})
        assert not fut.done(), "başka adresin cevabı"
        col._on_twap_history({"isSnapshot": True, "history": []})
        assert fut.done() and fut.result() == [], "kullanıcısız mesaj tek bekleyene (eski davranış)"
    asyncio.run(run())
    print("✅ inceleme) aynı saniyenin parçaları yeniden tartılır; big önce; başka adresin snapshot'ı düşmez")


def test_quick_cancel_reliability():
    """İnceleme: ⚡ HIZLI iptal notu her yolda gerçekten gelir — yeniden başlatma, kapı düşmesi, aynı anahtarda
    başka TWAP, Telegram anlık hatası, iki yolun aynı anda yazması; bitiş notu kapalıyken 'otomatik' yazılmaz."""
    async def run():
        cfg = _cfg()
        await _fresh(cfg)
        now = dbm.now()
        started = now - 120
        state = {"st": "activated", "ex": 0.05}
        col = Col(lambda: [twap_order(started, minutes=30, plan=900_000, ex_frac=state["ex"], status=state["st"])])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        tf.observe("PUMP", A, "buy", PX, 12_000_000, 60_000, started + 2, 1000)
        await _drain(cfg, notifier, col)
        assert len(bot.sent) == 2 and "🔔 iptal edilirse ⛔" in bot.sent[0][1]
        assert [r["started"] for r in await dbm.kv_get(tf.OPEN_KV)] == [started], "kayıt kv'de"
        # (1) yeniden başlatma: bellek gider, kv'den yüklenir; ilk turda sorulur → kapalıyken iptal → ⛔
        tf.F.clear()
        assert await tf.load_open(now) == 1 and ("PUMP", A, "buy") in tf.F.known
        state["st"] = "terminated"
        assert await tf.lifecycle(cfg, notifier, col, now) == 1
        ends = [c for c, t in bot.sent if "TWAP iptal edildi" in t]
        assert ends == ["-100", "-200"], bot.sent
        assert await dbm.kv_get(tf.OPEN_KV) == [], "biten emir kayıttan düşer"
        # (2) kapı düşse de (kalan az) aynı emir yeniden sahiplenilir
        await _fresh(cfg)
        bot = Bot()
        notifier = Notifier(cfg, bot)
        state.update(st="activated", ex=0.05)
        tf.observe("PUMP", A, "buy", PX, 12_000_000, 60_000, started + 2, 1000)
        await _drain(cfg, notifier, col)
        tf.F.clear()
        state["ex"] = 0.8                                       # %80 doldu → kısa TWAP kapısı 'short_left'
        tf.observe("PUMP", A, "buy", PX, 12_000_000, 60_000, now - 1, 1000)
        await _drain(cfg, notifier, col)
        assert ("PUMP", A, "buy") in tf.F.open and len(bot.sent) == 2, "yeniden sahiplenildi, mesaj tekrar yok"
        # (3) aynı anahtarda başka TWAP dilim atıyor (sessizlik yok) → dakikada bir sorgu yine iptali yakalar
        rec = tf.F.open[("PUMP", A, "buy")]
        rec["lookup_ts"], rec["last_ts"] = now - 61, now - 5
        state["st"] = "terminated"
        # (4) Telegram anlık hatası: ana kanal bir kez düşer → işaret yok, sonraki turda yeniden; ayna bir kez
        bot.fail = {"-100"}
        assert await tf.lifecycle(cfg, notifier, col, now) == 0 and not rec["ended"]
        assert await _kind("twap_endo") == 0
        bot.fail = set()
        assert await tf.lifecycle(cfg, notifier, col, now + 31) == 1
        ends = [c for c, t in bot.sent if "TWAP iptal edildi" in t]
        assert ends == ["-100", "-200"], bot.sent
        # (5) iki yol aynı anda: eski yol yazıyorsa hızlı yol bekler
        await _fresh(cfg)
        bot = Bot()
        notifier = Notifier(cfg, bot)
        state.update(st="activated", ex=0.05)
        tf.observe("PUMP", A, "buy", PX, 12_000_000, 60_000, started + 2, 1000)
        await _drain(cfg, notifier, col)
        state["st"] = "terminated"
        rec = tf.F.open[("PUMP", A, "buy")]
        rec["lookup_ts"] = 0
        tl.ENDING.add(f"PUMP:{A}:buy:{started}")
        assert await tf.lifecycle(cfg, notifier, col, now) == 0 and not rec["ended"]
        tl.ENDING.clear()
        rec["lookup_ts"] = 0
        assert await tf.lifecycle(cfg, notifier, col, now) == 1
        # (6) bitiş notu kapalıysa 'otomatik yazılır' denmez, takip satırı kalır
        cfg2 = _cfg()
        cfg2.twap_alert_end_note = False
        await _fresh(cfg2)
        b2 = Bot()
        state.update(st="activated", ex=0.05)
        tf.observe("PUMP", A, "buy", PX, 12_000_000, 60_000, started + 2, 1000)
        await _drain(cfg2, Notifier(cfg2, b2), col)
        assert "🔔" not in b2.sent[0][1] and "/takip_" in b2.sent[0][1]
    asyncio.run(run())
    print("✅ ⚡ iptal) yeniden başlatma, kapı düşmesi, aynı anahtarda başka TWAP, Telegram hatası, iki yol → ⛔ bir kez")


def test_slow_quick_end_retry_and_rehydrate_merge():
    async def run():
        cfg = _cfg()
        cfg.twap_fast_enabled = False
        await _fresh(cfg)
        now = dbm.now()
        started = now - 400
        state = {"st": "activated"}
        col = Col(lambda: [twap_order(started, minutes=45, plan=3_000_000, ex_frac=0.2, status=state["st"])])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        for i in range(13):
            tl.observe("PUMP", A, "buy", PX, 10_000_000, 50_000, started + 2 + 30 * i, 1)
        await tl.evaluate(cfg, notifier, None, col)
        assert len(bot.sent) == 2
        # yeniden başlatma: yeni dilimler taze tur kurdu → kayıtlı (bildirilmiş) tur onun üzerine yüklenir
        tl.REG.clear()
        tl.observe("PUMP", A, "buy", PX, 10_000_000, 50_000, now - 20, 1)
        assert await tl._rehydrate(4 * 3600) == 1
        run_ = tl.REG.runs[("PUMP", A, "buy")]
        assert run_.alerted_ts and run_.order and run_.order["minutes"] == 45 and run_.n == 14
        # iptal: ana kanal bir kez düşer → işaret yok, sonraki tur yeniden; sonra ⛔ kanal + ayna bir kez
        state["st"] = "terminated"
        run_.lookup_ts = now - 61
        bot.fail = {"-100"}
        await tl.evaluate(cfg, notifier, None, col)
        assert not run_.ended_ts and await _kind("twap_end") == 0
        bot.fail = set()
        run_.lookup_ts = now - 61
        await tl.evaluate(cfg, notifier, None, col)
        ends = [c for c, t in bot.sent if "TWAP iptal edildi" in t]
        assert ends == ["-100", "-200"] and run_.ended_ts, bot.sent
    asyncio.run(run())
    print("✅ ⚡ iptal) eski yol: yeniden başlatmada tur birleşir; ana kanal hatasında sonraki tur yeniden; ⛔ bir kez")


def _two_records(started, minutes, sz, end_status, end_t, ex_sz, ex_ntl, coin="FIL", twap_id=2308272):
    """HL'nin GERÇEK şekli (12.10, FIL 0x2606…1a84): emir başına durum değişimi kaydı — 'activated' kaydı kalır,
    yanına bitiş/iptal kaydı eklenir (aynı twapId, aynı state.timestamp)."""
    base = {"coin": coin, "user": A, "side": "B", "sz": str(sz), "minutes": minutes, "reduceOnly": False,
            "randomize": False, "timestamp": started * 1000 + 1, "trigger": None, "stopPx": None}
    return [{"time": end_t, "state": {**base, "executedSz": str(ex_sz), "executedNtl": str(ex_ntl)},
             "status": {"status": end_status}, "twapId": twap_id},
            {"time": started, "state": {**base, "executedSz": "0.0", "executedNtl": "0.0"},
             "status": {"status": "activated"}, "twapId": twap_id}]


def test_real_history_two_records_per_order():
    """Canlı vaka (12.10): FIL 25 dk $1M TWAP 83 sn sonra iptal (HL 'terminated', hypurrscan 'error') ama ⛔ gelmedi —
    ayrıştırıcı eski 'activated' kaydını seçiyordu. Emir başına EN SON kayıt: iptal görülür, ⛔ kanal + ayna."""
    async def run():
        now = dbm.now()
        started = now - 200
        hist = _two_records(started, 25, 803019.3, "terminated", started + 83, 47236.0, 58990.36)
        o = tl.parse_twap_orders(hist, "FIL", "buy", 1.25, now)
        assert len(o) == 1 and o[0]["status"] == "terminated" and abs(o[0]["executed_usd"] - 58990.36) < 1e-6
        assert len(tl.latest_per_twap(hist + [{"bozuk": 1}, "x"])) == 2, "bozuk kayıt atlanır, emir başına bir"
        # aynı anda iki kayıt (aynı time): bitiş kaydı kazanır; twapId yoksa (coin, yön, başlangıç, adet) anahtarı
        same = [{**h, "time": started} for h in hist]
        assert tl.parse_twap_orders(same, "FIL", "buy", 1.25, now)[0]["status"] == "terminated"
        noid = [{k: v for k, v in h.items() if k != "twapId"} for h in hist]
        assert [x["status"] for x in tl.parse_twap_orders(noid, "FIL", "buy", 1.25, now)] == ["terminated"]
        # ilk dilim yolu: bildirildi → iptal → ⛔ (önce 'activated' kaydı yüzünden hiç gelmiyordu)
        cfg = _cfg()
        await _fresh(cfg, coin="FIL", dv=6_000_000)
        state = {"h": hist[1:]}                                   # başta yalnız 'activated' kaydı
        col = Col(lambda: state["h"])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        tf.observe("FIL", A, "buy", 1.25, 16_000, 20_000, started + 1, 1000)
        await _drain(cfg, notifier, col)
        assert len(bot.sent) == 2 and "⚡ <b>HIZLI · 25 dk</b> · <b>FIL</b>" in bot.sent[0][1], bot.sent
        state["h"] = hist                                         # iptal kaydı eklendi, eski kayıt duruyor
        rec = tf.F.open[("FIL", A, "buy")]
        rec["lookup_ts"] = now - 61
        assert await tf.lifecycle(cfg, notifier, col, now) == 1
        ends = [(c, t) for c, t in bot.sent if "TWAP iptal edildi" in t]
        assert [c for c, _ in ends] == ["-100", "-200"] and "$59K" in ends[0][1], bot.sent
        # eski yol: bitmiş 5 dk'lık emir (iki kayıt) bitmiş görülür — 'aktif' sanılıp uyarılmaz
        cfg2 = _cfg()
        cfg2.twap_fast_enabled = False
        await _fresh(cfg2)
        s5 = now - 330
        h5 = _two_records(s5, 5, 120_000_000, "finished", s5 + 301, 120_000_000, 600_000, coin="PUMP", twap_id=7)
        b2 = Bot()
        for i in range(11):
            tl.observe("PUMP", A, "buy", PX, 12_000_000, 60_000, s5 + 1 + 30 * i, 1)
        out = await tl.evaluate(cfg2, Notifier(cfg2, b2), None, Col(h5))
        assert out["order_done"] == 1 and not b2.sent, (out, b2.sent)
        # takip (/takip_N): iptal kaydı görülür → ⛔ (önce eski kayıt yüzünden sürüyor sanılıyordu)
        from app.radar import twapfollow
        await twapfollow.start(cfg, "FIL", A, "buy", started + 1, chat_id="-100")

        class TB:
            def __init__(self):
                self.sent = []

            async def send(self, kind, text, **kw):
                self.sent.append(text)
                return True
        tb = TB()
        r = await twapfollow.check(cfg, Col(hist), tb)
        assert r["cancelled"] == 1 and "TWAP iptal edildi" in tb.sent[-1], (r, tb.sent)
    asyncio.run(run())
    print("✅ gerçek geçmiş) emir başına iki kayıt: en son kayıt — iptal ⛔ (ilk dilim, eski yol, takip); biten emir aktif sanılmaz")


def test_gates_and_skips():
    async def run():
        cfg = _cfg()
        await _fresh(cfg)
        now = dbm.now()
        ok = lambda **kw: tl.parse_twap_orders([twap_order(now - 2, **kw)], "PUMP", "buy", PX, now)[0]  # noqa: E731
        g, det = tf.decide_gate(cfg, "PUMP", ok(), DV, now, now)
        assert g == "short" and 8 < det["rate_x"] < 9 and abs(det["per_min"] - 120_000) < 1
        assert tf.decide_gate(cfg, "PUMP", ok(plan=200_000), DV, now, now)[0] == "short_small"
        assert tf.decide_gate(cfg, "PUMP", ok(minutes=20, plan=300_000), DV, now, now)[0] == "slow"   # ~1.1 kat
        assert tf.decide_gate(cfg, "PUMP", ok(minutes=240, plan=2_000_000), DV, now, now)[0] == "ok"   # %10
        assert tf.decide_gate(cfg, "PUMP", ok(minutes=240, plan=600_000), DV, now, now)[0] == "order_small"
        assert tf.decide_gate(cfg, "PUMP", ok(status="finished"), DV, now, now)[0] == "order_done"
        assert tf.decide_gate(cfg, "PUMP", ok(), DV, now - 7200, now)[0] == "no_vol"
        assert tf.decide_gate(cfg, "PUMP", ok(ex_frac=0.8), DV, now, now)[0] == "short_left"
        bot = Bot()
        notifier = Notifier(cfg, bot)
        # emir yok (likidasyon): iki deneme → no_order, 5 dk yok sayılır, mesaj yok
        col = Col([])
        tf.observe("PUMP", A, "buy", PX, 12_000_000, 60_000, now - 1, 1000)
        await _drain(cfg, notifier, col)
        assert tf.F.pending[("PUMP", A, "buy")]["tries"] == 1
        await _drain(cfg, notifier, col, t=now + 10)
        assert tf.F.stats["no_order"] == 1 and ("PUMP", A, "buy") in tf.F.known and not bot.sent
        assert len(col.calls) == 2
        # bekleme: eski yol 6 sa içinde bildirdiyse hızlı yol yazmaz
        await dbm.alert_log("twap", f"PUMP:{B}:buy", "")
        col2 = Col([dict(twap_order(now - 2), state={**twap_order(now - 2)["state"], "user": B})])
        tf.observe("PUMP", B, "buy", PX, 12_000_000, 60_000, now - 1, 1000)
        await _drain(cfg, notifier, col2)
        assert tf.F.stats["cooldown"] == 1 and not bot.sent
        # mm/vault: sorgu bile yok
        c3 = "0x" + "c" * 40
        async with dbm.db() as c:
            await c.execute("INSERT INTO addresses(address, first_seen, entity) VALUES(?,?,?)", (c3, now, "mm"))
        col3 = Col([twap_order(now - 2)])
        tf.observe("PUMP", c3, "buy", PX, 12_000_000, 60_000, now - 1, 1000)
        await _drain(cfg, notifier, col3)
        assert tf.F.stats["mm"] == 1 and col3.calls == []
        # TWAP bildirimi kapalı: kayıt (🧪) var, mesaj yok
        d4 = "0x" + "d" * 40
        cfg.notify_twap = False
        n_lab = len(_q("LOG-TWAPF"))
        tf.observe("PUMP", d4, "buy", PX, 12_000_000, 60_000, now - 1, 1000)
        await _drain(cfg, notifier, Col([twap_order(now - 2)]))
        assert tf.F.stats["notify_off"] == 1 and not bot.sent and len(_q("LOG-TWAPF")) == n_lab + 1
        # soket dolu: eski yol / takip sorguları önce
        cfg.notify_twap = True
        e5 = "0x" + "e" * 40
        tf.observe("PUMP", e5, "buy", PX, 12_000_000, 60_000, now - 1, 1000)
        busy = Col([twap_order(now - 2)])
        busy._twap_waiters = {str(i): None for i in range(tf.WS_CEILING)}
        assert await _drain(cfg, notifier, busy) == 0 and tf.F.stats["no_ws"] >= 1
    asyncio.run(run())
    print("✅ hızlı twap) kapı: kısa (dakika hızı) / uzun (eski eşik); emir yok, bekleme, mm, kapalı, soket dolu → yok")


def test_mirror_rules_slow_path():
    async def run():
        from app.radar import twaplive
        # ana kripto gönderimi başarısız → her tur yeniden dener, ayna BİR kez
        cfg = _cfg()
        await _fresh(cfg)
        now = dbm.now()
        col = Col([twap_order(now - 400, minutes=60, plan=3_000_000, ex_frac=0.2)])
        bot = Bot(fail_chats={"-100"})
        notifier = Notifier(cfg, bot)
        for i in range(13):
            tl.observe("PUMP", A, "buy", PX, 10_000_000, 50_000, now - 400 + 2 + 30 * i, 1)
        await tl.evaluate(cfg, notifier, None, col)
        tl._lookup_cache.clear()
        await tl.evaluate(cfg, notifier, None, col)
        assert [c for c, _ in bot.sent] == ["-200"], bot.sent
        # ayna boş: hiç gönderilmez (bot.send boş id'de ana sohbete düşerdi)
        cfg2 = _cfg(mirror="")
        await _fresh(cfg2)
        assert await twaplive.mirror(cfg2, Notifier(cfg2, bot), "x", "k") is None
        # ayna = ana hedef → ikinci kopya yok
        cfg3 = _cfg(mirror="-100")
        assert await twaplive.mirror(cfg3, Notifier(cfg3, Bot()), "x", "k2", primary="-100") is None
        cfg4 = _cfg(mirror="111")
        assert await twaplive.mirror(cfg4, Notifier(cfg4, Bot()), "x", "k3", primary="") is None, "ana sohbet"
        # ana kripto kanalı yok + ayna: yalnız ayna; durum eskisi gibi (no_chat, 'twap' işareti yok)
        cfg5 = _cfg(chat="")
        await _fresh(cfg5)
        b5 = Bot()
        for i in range(13):
            tl.observe("PUMP", A, "buy", PX, 10_000_000, 50_000, now - 400 + 2 + 30 * i, 1)
        out = await tl.evaluate(cfg5, Notifier(cfg5, b5), None, col)
        assert [c for c, _ in b5.sent] == ["-200"] and out["no_chat"] == 1 and await _kind("twap") == 0
        # ayna grubu botun kendi sohbeti (komutlar çalışır)
        from app.telegram.bot import TelegramBot
        assert "-200" in TelegramBot._own_chats(type("X", (), {"cfg": _cfg()})())
    asyncio.run(run())
    print("✅ ayna) ana başarısız olup yeniden denense de bir kez; boş/aynı hedef yok; kanalsızda yalnız ayna")


def test_wiring_and_wording():
    from app.lab import specs
    cfg = Config()
    assert cfg.twap_fast_enabled and cfg.twap_fast_min_slice_usd == 1000 and cfg.twap_fast_lookups_min == 30
    assert cfg.twap_fast_short_max_min == 60 and cfg.twap_fast_short_min_usd == 250_000 and cfg.twap_fast_rate_x == 2
    for k in ("twap_fast_enabled", "twap_fast_min_slice_usd", "twap_fast_lookups_min", "twap_fast_short_max_min",
              "twap_fast_short_min_usd", "twap_fast_rate_x"):
        assert EDITABLE_FIELDS[k]["group"] == "TWAP radarı" and EDITABLE_FIELDS[k]["desc"], k
    assert "twap_mirror_chat_id" not in EDITABLE_FIELDS, "chat id env-only"
    assert KINDS["twap_mirror"][0] == "notify_twap" and "twap_mirror" not in PUBLIC_KINDS
    assert "$2M" not in EDITABLE_FIELDS["notify_twap"]["desc"]
    log = next(r for r in specs.RULES if r["id"] == "LOG-TWAPF")
    assert log["evidence"] == "log" and log["ver"] >= 2 and log["latency_s"] <= 60
    env = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
    assert "TWAP_MIRROR_CHAT_ID=" in env and "TWAP_FAST_MIN_SLICE_USD=" in env
    main = open(os.path.join(ROOT, "app/main.py"), encoding="utf-8").read()
    assert '_spawn("twapfast"' in main
    colsrc = open(os.path.join(ROOT, "app/hl/collector.py"), encoding="utf-8").read()
    assert "twapfast.is_twap_hash" in colsrc and "twapfast.observe" in colsrc
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    assert "TWAP_MIRROR_CHAT_ID" in readme and "İlk dilimde TWAP" in readme and "⚡ HIZLI" in readme
    from app.health import periods
    assert "twapfast" in periods(cfg)
    # mesaj: ölçülü dil
    sys.path.insert(0, HERE)
    import test_wording as tw
    now = dbm.now()
    o = tl.parse_twap_orders([twap_order(now - 3)], "PUMP", "buy", PX, now)[0]
    for g in ("short", "ok"):
        txt = fmt.twap_fast(o, {"coin": "PUMP", "address": A, "side": "buy", "gate": g, "day_vol": DV,
                                "vol_pct": 3.0, "per_min": 120_000, "rate_x": 8.6, "slice_usd": 60_000,
                                "slice_px": PX, "pos": {"none": True}, "klass": "kripto", "offer": 7, "now": now})
        assert not tw.hits(txt), tw.hits(txt)
        assert "emirden 3 sn sonra" in txt and "/takip_7" in txt
    print("✅ bağlantı) ayarlar + açıklamalar; ayna env-only; tür anahtarı; LOG-TWAPF; README/.env; ölçülü mesaj")


def test_quick_badge_one_hour():
    """⚡ HIZLI = emir süresi ≤ 1 sa (kullanıcı 12.10): ilk dilim, eski yol, yarı, bitiş mesajlarında; üstünde yok;
    sınır ayardan; dakika hızı kuralı aynı sınırla (45 dk'lık emir artık o kuralla geçebilir, 90 dk geçemez)."""
    now = dbm.now()
    cfg = _cfg()
    mk = lambda mins: tl.parse_twap_orders([twap_order(now - 3, minutes=mins)], "PUMP", "buy", PX, now)[0]  # noqa: E731
    base = {"coin": "PUMP", "address": A, "side": "buy", "gate": "ok", "day_vol": DV, "pos": None,
            "klass": "kripto", "now": now, "quick_max": tl.quick_max(cfg)}
    assert "⚡ <b>HIZLI · 1 s 0 dk</b>" in fmt.twap_fast(mk(60), base)
    assert "⚡" not in fmt.twap_fast(mk(61), base).split("\n")[0] and "<b>1 s 1 dk</b>" in fmt.twap_fast(mk(61), base)
    assert "⚡" not in fmt.twap_fast(mk(5), {**base, "quick_max": 0}).split("\n")[0], "sınır yoksa rozet yok"
    m = {"coin": "PUMP", "address": A, "side": "buy", "n": 12, "total": 1e5, "sz_total": 2e7, "avg_slice": 8e3,
         "median_gap": 30, "px_first": PX, "px_last": PX, "px_chg_pct": 0.0, "dur": 360, "taker_pct": 100}
    for f, extra in ((fmt.twap_alert, {"vol_pct": 7.0}), (fmt.twap_progress, {}), (fmt.twap_end, {})):
        assert "⚡ <b>HIZLI · 45 dk</b>" in f(m, {"order": mk(45), "quick_max": 60, **extra}).split("\n")[0], f
        assert "⚡" not in f(m, {"order": mk(240), "quick_max": 60, **extra}).split("\n")[0], f
    # dakika hızı kuralının nedeni doğru yazılır: $600K emir $2M hacmin %30'u → "hacim eşiğinin altında" DEĞİL
    gx, dx = tf.decide_gate(cfg, "PUMP", mk(5), 2e6, now, now)
    txt = fmt.twap_fast(mk(5), {**base, "gate": gx, "why": dx["reason"], "vol_pct": dx["vol_pct"]})
    assert gx == "short" and "hacim eşiğinin altında" not in txt and "(emir toplamı tabanın altında)" in txt
    g45, _ = tf.decide_gate(cfg, "PUMP", mk(45), 2e6, now, now)        # $600K / 45 dk vs $2M günlük → hızlı kural
    g90, _ = tf.decide_gate(cfg, "PUMP", mk(90), 20e6, now, now)
    assert g45 == "short" and g90 == "order_small", (g45, g90)
    print("✅ ⚡ HIZLI) ≤ 1 sa: dört mesaj türünde rozet; üstünde yok; dakika hızı kuralı aynı sınırla")


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
