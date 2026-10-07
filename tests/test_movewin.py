"""⏱ /5dk · /15dk — "şimdiden N dakika ölç, en çok oynayan hisseleri söyle" (haber saatleri).

Kullanıcı (07.10): "/5dk yazdığımda gelecek 5 dk en çok hangi hissenin oynadığını söylesin, 15dk
yazarsam 15 dk — herhangi bir an; haber saatlerinde kullanacağım." Kararlar: hemen başlar; istersen
saat de verilir; PROPR hisseleri; ölçerken 📊 tuşu.

Pinlenenler:
  • ayrıştırma: /5dk /15dk /5 /5 dk /10dakika /60dk; 0 · 61 · /5m → değil / hata; saat biçimleri;
    TSİ saat: 23:58'de 00:05 → yarın, 00:01'de 23:59 → dün (geriye dönük), 15:40'ta 15:30 → ret
    (yarına kaymaz), tam 3 dk geri sınırı, 12 saat ileri sınırı
  • ölçüm: geriye dönük başlangıç tampondan (referans = t0 öncesi son işlem, seyrek hissede çapa),
    t0 sonrası tampon işlemleri deftere — çift sayım yok; HL yeniden yollaması sayılmaz; t1'deki
    işlem dışarıda; † (referans yok) ve ° (referans 5 dk'dan eski); taban süreyle ölçeklenir;
    24s katı; ileri saatli pencerede referans t0'a dek canlı güncellenir
  • sınırlar: sohbet başına 2, toplam 5; aynı komut birleşir; evren yoksa dürüst ret
  • döngü (sahte saat): rapor t1+5 sn'de bir kez, komutun sohbetine, yayına gitmez; hata → 30 sn
    sonra yeniden + gecikme notu; 30 dk sonra bırakılır; yarıdan az görüldü → açıklama
  • yeniden başlatma: başlamamış geri yüklenir, yarıda kalan → tek not, eski / bozuk kayıt atlanır;
    geri yüklemeden önce yazılan komut eski kayıtları silmez
  • motor: tekrar teslim gün geçişinde de; bozuk pencere açılış ölçümünü aç bırakmaz
  • 📊: ilk 5, düz metin, emojisiz, ≤ 200; başlamadı / bitti / bulunamadı / geçersiz
  • Telegram: sahip, hisse kanalı üyesi, kanal gönderisi, yabancı grup sessiz, coin-liq'e düşmez;
    tuş kendi sohbette çalışır, yabancıda sessiz
  • kablolama: ayar künyesi, .env, yardım, README, tanı satırı (sohbet id'si yok), döngü kancası
"""
import asyncio
import datetime as dt
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-movewin.db")
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.hl import collector as colmod  # noqa: E402
from app.radar import movewin as mw  # noqa: E402
from app.radar import openmove as om  # noqa: E402
from app.telegram import format as fmt  # noqa: E402

UTC = dt.timezone.utc
BANNED = ("muhtemel", "olası", "olabilir", "bekleniyor", "yükselecek", "düşecek")


def ts(y, m, d, hh=0, mm=0, ss=0):
    return int(dt.datetime(y, m, d, hh, mm, ss, tzinfo=UTC).timestamp())


T = ts(2026, 10, 7, 12, 30)            # Çar 07.10 15:30:00 TSİ (haber saati)
TICKERS = [("xyz:SNDK", "xyz", "SNDK"), ("xyz:CBRS", "xyz", "CBRS"), ("xyz:MU", "xyz", "MU"),
           ("xyz:HOOD", "xyz", "HOOD"), ("xyz:GOLD", "xyz", "GOLD"), ("xyz:XYZ100", "xyz", "XYZ100"),
           ("BTC", "", "BTC")]
REPORT = "\n".join([
    "⏱ <b>En hareketliler</b> — 5 dk ölçüm (15:30:00–15:35:00 TSİ) · Çar 07.10",
    "1. <b>MU</b>† 🟢 <b>+5.00%</b> · aralık %5.00 · $22K",
    "2. <b>CBRS</b>° 🟢 <b>+4.00%</b> · aralık %0.00 · $21K",
    "3. <b>SNDK</b> 🟢 <b>+2.79%</b> · aralık %2.99 · $87K (24s ort. 2.9×)",
    "<i>4 hisse izlendi (PROPR, endeks/emtia hariç) · pencerede işlem gören: 4 · hacmi $10K altında kalıp"
    " sıralamaya girmeyen: 1 · referans: başlangıçtan önceki son işlem († başlangıçtan önce işlem görülmedi:"
    " referans pencerenin ilk işlemi) (° referans işlemi başlangıçtan 5 dk'dan eski) · canlı işlem akışı ·"
    " ölçüm, tahmin değil</i>"])


def _cfg():
    cfg = Config()
    cfg.telegram_chat_id, cfg.telegram_owner_id = "111", "7"
    cfg.crypto_stocks_id, cfg.crypto_chat_id, cfg.account_chat_id = "-300", "-100", "-500"
    cfg.equity_dexes = ["xyz"]
    cfg.open_movers_enabled, cfg.open_movers_top = True, 10
    cfg.move_window_min_usd = 10_000.0
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    cfg.public_bot_enabled = False
    return cfg


async def _fresh(boot=T - 3600):
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "movewin.db"))
    async with dbm.db() as c:
        for coin, dex, sym in TICKERS:
            await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES(?,?,?)", (coin, dex, sym))
        await c.execute("INSERT INTO asset_metrics(coin,ts,day_volume) VALUES('xyz:SNDK',?,?)", (T - 7200, 8_640_000.0))
        await c.commit()
    om.REG = om._Reg()                 # süreç yeni açılmış gibi
    cfg = _cfg()
    if boot:
        await om.ensure_universe(cfg, boot)
        om.REG.wins_loaded = True      # geri yükleme ayrı testte
    return cfg


class Live:
    def __init__(self, down_log=(), down_since=0.0):
        self.down_log, self.down_since = list(down_log), down_since
        self.connected = not down_since


def _live(x):
    real = colmod.LIVE
    colmod.LIVE = x
    return real


class Sink:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    async def send(self, kind, text, **kw):
        self.sent.append((kind, text, kw))
        return self.ok


def pre_trades():
    """Başlangıçtan (T) önce: SNDK 501 (T−30), CBRS 100 (T−900: seyrek, eski referans), MU yok."""
    ob = om.observe
    ob("xyz:SNDK", 500.0, 1, T - 200, "p1")
    ob("xyz:SNDK", 501.0, 1, T - 30, "p2")
    ob("xyz:CBRS", 100.0, 1, T - 900, "q1")


def early_trades():
    """T ile komut (T+40) arası — geriye dönük başlangıçta tampondan gelmeli."""
    ob = om.observe
    ob("xyz:SNDK", 505.0, 101, T + 10, "s1")     # $51,005
    ob("xyz:CBRS", 104.0, 200, T + 20, "c1")     # $20,800 → +%4 (eski referanstan)


def late_trades():
    ob = om.observe
    ob("xyz:MU", 200.0, 100, T + 50, "m1")       # $20,000 (referans yok → †)
    ob("xyz:HOOD", 50.0, 10, T + 60, "h1")       # $500 → tabanın altı
    ob("xyz:SNDK", 520.0, 50, T + 100, "s2")     # $26,000
    ob("xyz:SNDK", 520.0, 50, T + 100, "s2")     # yeniden bağlanınca tekrar teslim
    ob("xyz:SNDK", 505.0, 101, T + 10, "s1")     # HL abonelik tekrarı (eski)
    ob("xyz:MU", 210.0, 10, T + 200, "m2")       # $2,100 → MU +%5
    ob("xyz:SNDK", 515.0, 20, T + 299, "s3")     # $10,300 → son fiyat
    ob("xyz:SNDK", 600.0, 10, T + 300, "s4")     # t1'de: pencere DIŞI


def _texts_ok(*texts):
    for t in texts:
        assert not any(w in t.lower() for w in BANNED), t
        assert not re.search(r"\d['’]", t), ("sayıya ek", t)


# ---------------- ayrıştırma + saat ----------------

def test_parse_and_start_time():
    p = mw.parse
    assert p("5dk", []) == {"mins": 5, "at": None} and p("15dk", [])["mins"] == 15
    assert p("5", [])["mins"] == 5 and p("5", ["dk"]) == {"mins": 5, "at": None} and p("10dakika", [])["mins"] == 10
    assert p("60dk", [])["mins"] == 60 and p("5dak", [])["mins"] == 5
    for bad in ("0dk", "61dk", "0"):
        assert "1–60" in p(bad, [])["error"], bad
    for not_cmd in ("dk", "acilis", "5m", "123dk", "sndk", ""):
        assert p(not_cmd, []) is None, not_cmd
    assert p("5dk", ["15:30"])["at"] == (15, 30, 0) and p("5dk", ["15.30"])["at"] == (15, 30, 0)
    assert p("5", ["dk", "15:30:20"])["at"] == (15, 30, 20) and p("15dk", ["9:05"])["at"] == (9, 5, 0)
    for bad in (["25:00"], ["15:61"], ["SNDK"], ["15:30:99"]):
        assert p("5dk", bad)["error"] == "saat biçimi: /5dk 15:30 (TSİ)", bad
    r = mw.resolve_start
    assert r(None, T) == (T, "") and r((15, 30, 0), T) == (T, "")
    assert r((15, 30, 0), T + 180) == (T, ""), "tam 3 dk geri: kabul"
    t0, why = r((15, 30, 0), T + 181)
    assert t0 is None and why.startswith("başlangıç saati geçmiş — en çok 3 dk geriye")
    assert r((15, 30, 0), T + 600)[0] is None, "15:40'ta 15:30 → ret, yarına kaymaz"
    assert r((0, 5, 0), ts(2026, 10, 7, 20, 58))[0] == ts(2026, 10, 7, 21, 5), "23:58'de 00:05 → yarın"
    assert r((23, 59, 0), ts(2026, 10, 7, 21, 1))[0] == ts(2026, 10, 7, 20, 59), "00:01'de 23:59 → dün"
    assert r((3, 29, 0), T)[0] == T + 11 * 3600 + 59 * 60, "12 saat içi"
    assert r((3, 31, 0), T)[0] is None, "12 saati aşan ret"
    print("✅ ayrıştırma) /5dk /15dk /5 /5 dk /10dakika; 0 · 61 hata, /5m komut değil; saat biçimleri; TSİ:"
          " yarın / dün / ret (kaymaz), 3 dk geri ve 12 saat ileri sınırı")


# ---------------- ölçüm ----------------

def test_retro_window_measure_and_report():
    async def run():
        cfg = await _fresh()
        real = _live(Live())
        try:
            pre_trades()
            early_trades()
            r = om.REG
            assert list(r.recent["xyz:CBRS"]) == [(T - 900, 100.0, 1), (T + 20, 104.0, 200)], "çapa korunur"
            res = mw.start(5, "-300", T + 40, T)                      # 15:30:40'ta "/5dk 15:30"
            w15 = mw.start(15, "-300", T + 40, T)["win"]              # aynı anda /15dk 15:30
            w = res["win"]
            assert res["ok"] and not res["merged"] and (w["t0"], w["t1"]) == (T, T + 300)
            assert w["ref"]["xyz:SNDK"] == (T - 30, 501.0) and w["ref"]["xyz:CBRS"] == (T - 900, 100.0)
            assert "xyz:MU" not in w["ref"]
            assert w["agg"]["xyz:SNDK"]["n"] == 1 and w["agg"]["xyz:CBRS"]["n"] == 1, "tampondan yeniden oynatıldı"
            late_trades()
            a = w["agg"]["xyz:SNDK"]
            assert a["n"] == 3 and abs(a["vol"] - 87_305) < 1e-6 and a["last"] == 515.0, a
            assert w["agg"]["xyz:MU"]["n"] == 2 and "xyz:GOLD" not in w["agg"]
            rep = await mw.build(cfg, w, T + 305)
            assert rep["cover"]["full"] and rep["floor"] == 10_000 and rep["late"] == 0
            assert fmt.movewin_report(rep) == REPORT, fmt.movewin_report(rep)
            # 15 dk penceresi: taban $30K, 24s payı 15 dk; T+300'deki işlem bu pencerenin İÇİNDE
            rep15 = await mw.build(cfg, w15, T + 905)
            assert rep15["floor"] == 30_000 and [x["symbol"] for x in rep15["rows"]] == ["SNDK"]
            assert abs(rep15["rows"][0]["vol"] - 93_305) < 1e-6 and rep15["rows"][0]["ref_pre"]
            assert abs(rep15["rows"][0]["mult"] - 93_305 / 90_000) < 1e-9 and rep15["n_below"] == 3
            # ileri saatli pencere: referans başlangıca dek canlı güncellenir
            om.REG.wins.clear()
            T2 = T + 3600
            fut = mw.start(5, "111", T + 1000, T2)["win"]
            assert fut["ref"]["xyz:SNDK"] == (T + 300, 600.0), "kurulurken en yeni işlem"
            om.observe("xyz:SNDK", 610.0, 1, T2 - 5, "u1")
            om.observe("xyz:SNDK", 620.0, 50, T2 + 5, "u2")
            assert fut["ref"]["xyz:SNDK"] == (T2 - 5, 610.0) and fut["agg"]["xyz:SNDK"]["n"] == 1
            _texts_ok(fmt.movewin_report(rep), fmt.movewin_report(rep15))
        finally:
            colmod.LIVE = real
        print("✅ ölçüm) geriye dönük başlangıç: referans t0 öncesi son işlem (seyrek hissede çapa), t0 sonrası"
              " tampondan, çift sayım yok; t1 dışarıda; † ve °; taban ve 24s payı süreyle; ileri saatte"
              " referans canlı")
    asyncio.run(run())


def test_limits_merge_and_not_ready():
    async def run():
        await _fresh(boot=0)
        assert mw.start(5, "111", T, T)["error"].startswith("ölçüm henüz hazır değil"), "evren yok"
        cfg = await _fresh()
        assert mw.start(5, "111", T, T)["ok"]
        again = mw.start(5, "111", T + 3, T)
        assert again["ok"] and again["merged"] and len(om.REG.wins) == 1, "aynı komut birleşir"
        assert mw.start(15, "111", T, T)["ok"]
        assert mw.start(10, "111", T, T)["error"] == "bu sohbette aynı anda en çok 2 ölçüm — biri bitince yeniden dene"
        for chat in ("-300", "-100", "-500"):
            assert mw.start(5, chat, T, T)["ok"]
        assert mw.start(5, "-999", T, T)["error"] == "aynı anda en çok 5 ölçüm — biri bitince yeniden dene"
        ack = fmt.movewin_ack(again["win"], T + 3, 4, merged=True)
        assert ack.startswith("⏱ Bu ölçüm zaten açık — 5 dk, Çar 07.10 15:30:00 → 15:35:00 TSİ")
        assert cfg.move_window_min_usd == 10_000
        print("✅ sınırlar) evren yoksa dürüst ret; aynı komut birleşir; sohbet başına 2, toplam 5")
    asyncio.run(run())


def test_engine_dedupe_and_isolation():
    async def run():
        await _fresh()
        r = om.REG
        om.configure(dt.date(2026, 10, 7), T + 3600, T - 600)             # açılış 16:30
        om.observe("xyz:SNDK", 500.0, 1, T + 100, "d1")
        om.configure(dt.date(2026, 10, 8), T + 3600 + 86400, T + 5000)     # gün geçişi
        om.observe("xyz:SNDK", 499.0, 1, T + 90, "d0")                      # gün geçişinden sonra eski tekrar
        assert r.hwm["xyz:SNDK"][0] == T + 100 and [x[0] for x in r.recent["xyz:SNDK"]] == [T + 100]
        # bozuk pencere: açılış ölçümü yine yürür, hata sayılır
        om.configure(dt.date(2026, 10, 7), T + 3600, T - 600)
        r.wins.append({"t0": "bozuk"})
        om.observe("xyz:SNDK", 501.0, 2, T + 3600 + 10, "d2")
        assert r.agg30["xyz:SNDK"]["n"] == 1 and r.win_errors == 1
        r.wins.clear()
        print("✅ motor) tekrar teslim gün geçişinde de sayılmaz, tampon sıralı; bozuk pencere açılış ölçümünü"
              " aç bırakmaz")
    asyncio.run(run())


# ---------------- döngü (sahte saat) ----------------

def test_tick_report_retry_and_short():
    async def run():
        cfg = await _fresh()
        live = Live()
        real = _live(live)
        try:
            pre_trades()
            early_trades()
            mw.start(5, "-300", T + 40, T)
            late_trades()
            sink = Sink()
            assert (await mw.tick(cfg, sink, T + 304))["sent"] == 0 and not sink.sent
            out = await mw.tick(cfg, sink, T + 305)
            assert out["sent"] == 1 and len(sink.sent) == 1 and not om.REG.wins
            kind, text, kw = sink.sent[0]
            assert kind == "movewin" and text == REPORT
            assert kw == {"chat_id": "-300", "key": f"movewin:-300:{T}:5", "public": False}
            await mw.tick(cfg, sink, T + 400)
            assert len(sink.sent) == 1, "bir kez"
            doc = await dbm.kv_get(mw.KV)
            assert doc["wins"] == [] and doc["done"] == 1 and doc["last"]["result"] == "gönderildi"
            # gönderim hatası: 30 sn'de bir yeniden; gecikme künyede
            T3 = T + 1000
            mw.start(1, "111", T3, T3)
            om.observe("xyz:SNDK", 530.0, 100, T3 + 10, "r1")
            bad = Sink(ok=False)
            await mw.tick(cfg, bad, T3 + 65)
            await mw.tick(cfg, bad, T3 + 80)
            assert len(bad.sent) == 1, "30 sn dolmadan yeniden deneme yok"
            await mw.tick(cfg, bad, T3 + 96)
            assert len(bad.sent) == 2
            bad.ok = True
            await mw.tick(cfg, bad, T3 + 130)
            assert len(bad.sent) == 3 and not om.REG.wins
            assert bad.sent[-1][1].endswith(" · rapor 1 dk 10 sn gecikmeli gönderildi</i>"), bad.sent[-1][1]
            # hiç gidemezse 30 dk sonra bırakılır
            T4 = T + 5000
            mw.start(1, "111", T4, T4)
            dead = Sink(ok=False)
            await mw.tick(cfg, dead, T4 + 65)
            await mw.tick(cfg, dead, T4 + 60 + mw.GIVE_UP + 1)
            assert not om.REG.wins and (await dbm.kv_get(mw.KV))["last"]["result"] == "gönderilemedi (Telegram)"
            # yarıdan az görüldü (kopukluk) → rapor yerine açıklama
            T5 = T + 9000
            mw.start(5, "111", T5, T5)
            live.down_log = [(T5 + 20, T5 + 250)]
            await mw.tick(cfg, sink, T5 + 305)
            short = sink.sent[-1][1]
            assert short == ("⏱ 5 dk ölçüm (18:00:00–18:05:00 TSİ): pencerenin görülen payı %23 (en az %50"
                             " gerekir) — rapor yok. Canlı akışın görülmediği aralık: 18:00–18:04. /5dk ile"
                             " yeniden ölçebilirsin."), short
            assert (await dbm.kv_get(mw.KV))["last"]["result"] == "yetersiz kapsam"
            _texts_ok(short, bad.sent[-1][1])
        finally:
            colmod.LIVE = real
        print("✅ döngü) rapor t1+5 sn'de bir kez, komutun sohbetine, yayına gitmez; hata → 30 sn sonra yeniden"
              " + gecikme notu; 30 dk sonra bırakılır; yarıdan az görüldü → açıklama")
    asyncio.run(run())


def test_restore_after_restart():
    async def run():
        cfg = await _fresh(boot=0)
        real = _live(Live())
        now_ = T
        await dbm.kv_set(mw.KV, {"wins": [
            {"chat": "111", "t0": now_ + 600, "mins": 5},          # başlamamış → geri yüklenir
            {"chat": "-300", "t0": now_ - 60, "mins": 5},          # yarıda kaldı → not
            {"chat": "111", "t0": now_ - 9000, "mins": 5},         # çok eski → sessiz
            {"chat": "", "t0": now_ + 100, "mins": 5}, {"chat": "1", "t0": "x", "mins": 5},
            {"chat": "1", "t0": now_ + 100, "mins": 0}, "bozuk"], "done": 4})
        real_now = mw.now
        mw.now = lambda: now_ - 5
        try:
            # geri yüklemeden önce gelen komut eski kayıtları silmez
            text, kb = await mw.command(cfg, "15dk", [], "-500")
            assert text.startswith("⏱ <b>15 dk ölçüm başladı</b>") and kb["inline_keyboard"][0][0]["callback_data"] == f"mvw:{now_ - 5}:15"
            assert len((await dbm.kv_get(mw.KV))["wins"]) == 7, "6 eski sözlük + yeni (bozuk metin elenir)"
            sink = Sink()
            out = await mw.tick(cfg, sink, now_)
            assert out["notes"] == 1 and len(sink.sent) == 1
            kind, text, kw = sink.sent[0]
            assert kw["chat_id"] == "-300" and text == ("⏱ 5 dk ölçüm (15:29:00–15:34:00 TSİ) yarıda kaldı — bot"
                                                        " yeniden başladı, ölçüm verisi bu süreçte yok. Rapor yok;"
                                                        " /5dk ile yeniden ölçebilirsin.")
            got = sorted((w["chat"], w["t0"], w["mins"]) for w in om.REG.wins)
            assert got == [("-500", now_ - 5, 15), ("111", now_ + 600, 5)], got
            doc = await dbm.kv_get(mw.KV)
            assert sorted((e["chat"], e["t0"]) for e in doc["wins"]) == [("-500", now_ - 5), ("111", now_ + 600)]
            assert doc["last"]["result"] == "yarıda kaldı (bot yeniden başladı)" and doc["done"] == 5
            await mw.tick(cfg, sink, now_ + 10)
            assert len(sink.sent) == 1, "geri yükleme bir kez"
            _texts_ok(text)
        finally:
            mw.now = real_now
            colmod.LIVE = real
        print("✅ yeniden başlatma) başlamamış geri yüklenir, yarıda kalan tek not, eski / bozuk atlanır; önce"
              " gelen komut eski kayıtları silmez")
    asyncio.run(run())


# ---------------- 📊 ----------------

def test_peek_popup():
    async def run():
        cfg = await _fresh()
        real = _live(Live())
        try:
            pre_trades()
            early_trades()
            w = mw.start(5, "-300", T + 40, T)["win"]
            om.observe("xyz:MU", 200.0, 100, T + 50, "m1")
            om.observe("xyz:HOOD", 50.0, 10, T + 60, "h1")
            om.observe("xyz:SNDK", 520.0, 50, T + 100, "s2")
            text = mw.peek(cfg, "-300", T, 5, T + 130)
            assert text == ("5 dk ölçüm · geçen 2 dk 10 sn\n1. CBRS° +4.00% $21K\n2. SNDK +3.79% $77K\n"
                            "3. MU† +0.00% $20K"), text
            assert mw.peek_data(cfg, f"mvw:{T}:5", "-300", T + 130) == text
            assert mw.peek(cfg, "-300", T, 5, T + 302).startswith("5 dk ölçüm bitti · rapor birazdan\n1. ")
            fut = mw.start(5, "111", T + 40, T + 600)["win"]
            assert mw.peek(cfg, "111", fut["t0"], 5, T + 60) == ("5 dk ölçüm henüz başlamadı — başlangıç"
                                                                 " 15:40:00 TSİ (kalan 9 dk 0 sn).")
            assert mw.peek(cfg, "-100", T, 5, T + 400) == "Ölçüm bitti — rapor bu sohbette."
            assert mw.peek(cfg, "-100", T, 5, T + 100).startswith("Bu ölçüm bulunamadı")
            assert mw.peek_data(cfg, "mvw:abc", "-300") == "Geçersiz tuş."
            # sığmazsa alttan satır düşer (≤ 200 UTF-16), emoji yok
            rows = [{"symbol": f"COKUZUNSEMBOLADI{i}XYZW", "ref_pre": True, "chg": 12.345 + i, "vol": 1.23e6}
                    for i in range(5)]
            long = fmt.movewin_peek({"mins": 15}, rows, 30_000, 400)
            assert fmt.visible_len(long) <= 200 and long.count("\n") < 5, long
            for t in (text, long):
                assert not re.search("[\U0001F300-\U0001FAFF☀-➿]", t), t
            assert fmt.movewin_peek({"mins": 5}, [], 4333, 130) == "5 dk ölçüm · geçen 2 dk 10 sn\nHenüz $4K üstü işlem gören hisse yok."
        finally:
            colmod.LIVE = real
        print("✅ 📊) ilk 5 düz metin, emojisiz, ≤ 200 (sığmazsa satır düşer); başlamadı / bitti / bulunamadı /"
              " geçersiz")
    asyncio.run(run())


# ---------------- Telegram ----------------

def test_bot_routing_and_button():
    async def run():
        cfg = await _fresh()
        real = _live(Live())
        from app.telegram.bot import TelegramBot
        bot = TelegramBot(cfg, None, None, {})
        sent, answers = [], []

        async def fake_send(text, chat_id=None, reply_markup=None):
            sent.append((chat_id, text, reply_markup))
            return True

        async def fake_answer(cq_id, text="", alert=False):
            answers.append((cq_id, text, alert))
            return True
        bot.send, bot.answer_callback = fake_send, fake_answer

        async def msg(text, chat=111, uid=111, ctype="private"):
            await bot._handle_update({"message": {"chat": {"id": chat, "type": ctype},
                                                  "from": {"id": uid}, "text": text}})
        real_now = mw.now
        mw.now = lambda: T
        try:
            pre_trades()
            await msg("/5dk")
            chat, text, kb = sent[-1]
            assert chat == "111" and text == ("⏱ <b>5 dk ölçüm başladı</b> — Çar 07.10 15:30:00 → 15:35:00 TSİ ·"
                                              " 4 hisse (PROPR)\nRapor bitince bu sohbete · referans:"
                                              " başlangıçtan önceki son işlem · ara durum: 📊 tuşu"), text
            assert kb == {"inline_keyboard": [[{"text": "📊 Şu ana kadar", "callback_data": f"mvw:{T}:5"}]]}
            await msg("/15dk", chat=-300, uid=42, ctype="supergroup")            # hisse kanalı üyesi
            assert sent[-1][0] == "-300" and "15 dk ölçüm başladı" in sent[-1][1]
            await bot._handle_update({"channel_post": {"chat": {"id": -300, "type": "channel"},
                                                       "text": "/5 dk 15:31"}})     # kanal gönderisi, ileri saat
            assert sent[-1][0] == "-300" and "5 dk ölçüm kuruldu" in sent[-1][1] and "başlamasına 1 dk 0 sn" in sent[-1][1]
            await msg("/10dk", chat=-300, uid=42, ctype="supergroup")
            assert sent[-1][1] == "⏱ bu sohbette aynı anda en çok 2 ölçüm — biri bitince yeniden dene"
            n = len(sent)
            await msg("/5dk", chat=-999, uid=42, ctype="supergroup")             # yabancı grup
            assert len(sent) == n and len(om.REG.wins) == 3
            await msg("/61dk")
            assert sent[-1][1] == "⏱ süre 1–60 dk arası olmalı: /5dk, /15dk", "komut-yok yardımına düşmez"
            await msg("/5dk 15:20")
            assert sent[-1][1].startswith("⏱ başlangıç saati geçmiş")
            # 📊 tuşu: kendi sohbette açılır pencere, yabancıda sessiz
            om.observe("xyz:SNDK", 510.0, 100, T + 30, "b1")
            mw.now = lambda: T + 40

            def cq(chat, uid, data):
                return {"update_id": 1, "callback_query": {"id": "cb", "from": {"id": uid}, "data": data,
                                                           "message": {"message_id": 5, "chat": {"id": chat, "type": "supergroup"}}}}
            await bot._handle_update(cq(111, 111, f"mvw:{T}:5"))
            assert answers[-1] == ("cb", "5 dk ölçüm · geçen 40 sn\n1. SNDK +1.80% $51K", True), answers[-1]
            await bot._handle_update(cq(-999, 42, f"mvw:{T}:5"))
            assert answers[-1] == ("cb", "", False), "yabancı sohbet: boş cevap"
            assert "/5dk" in fmt.help_text()
        finally:
            mw.now = real_now
            colmod.LIVE = real
        print("✅ Telegram) sahip, hisse kanalı üyesi, kanal gönderisi (ileri saat), sohbet sınırı, yabancı grup"
              " sessiz, /61dk ve geçmiş saat dürüst cevap; 📊 kendi sohbette açılır pencere, yabancıda sessiz")
    asyncio.run(run())


# ---------------- kablolama ----------------

def test_wiring():
    async def run():
        from app import diag, notify
        src = lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read()   # noqa: E731
        assert "await movewin.tick(cfg, notifier, ts)" in src("app/radar/openmove.py")
        assert "movewin" not in notify.KINDS, "istenmiş cevap: türe bağlı kapatma yok"
        meta = EDITABLE_FIELDS["move_window_min_usd"]
        assert meta["group"] == "🔔 Açılışın en hareketlileri" and meta["type"] == "float" and len(meta["desc"]) > 60
        assert Config().move_window_min_usd == 10_000
        assert "MOVE_WINDOW_MIN_USD=10000" in src(".env.example")
        readme = src("README.md")
        assert "## ⏱ Pencere Ölçümü: `/5dk` · `/15dk` (haber saatleri)" in readme
        assert "| `/5dk` · `/15dk` · `/5dk 15:30` |" in readme
        cfg = await _fresh()
        real = _live(Live())
        try:
            mw.start(5, "-300", T, T)
            await mw.tick(cfg, Sink(), T + 305)
        finally:
            colmod.LIVE = real
        txt = "\n".join(await diag._subsystems(cfg))
        line = [x for x in txt.split("\n") if "pencere ölçümü" in x]
        assert line == ["  ⏱ pencere ölçümü (/5dk): aktif 0 · kayıtlı 0 · tamamlanan 1 · son: 5 dk"
                        " 15:30:00–15:35:00 → gönderildi"], line
        assert "-300" not in line[0], "sohbet id'si tanıya düşmez"
        print("✅ kablolama) döngü kancası, bildirim türü yok, ayar künyesi, .env, README, tanı satırı (sohbet id'si yok)")
    asyncio.run(run())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
