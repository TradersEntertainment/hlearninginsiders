"""🔔 Açılışın en hareketlileri — ABD açılışının ilk 5 ve ilk 30 dakikası (hisse kanalı).

Kullanıcı (07.10): "açılışın en hareketli hissesi diye bir şey yapalım; 16:30–16:35 arası ölçüm
yapsın rapor yollasın, sonra 17:00'e kadar hep ölçsün, 17'de tekrar rapor". Kararlar: fiyat
oynaması, hisse kanalı, PROPR'da listeli hisseler (endeks / emtia / döviz hariç), saat ABD
açılışına bağlı.

Pinlenenler:
  • takvim: yaz 07.10 açılış 13:30 UTC (16:30 / 16:35 / 17:00 TSİ), kış 03.11 14:30 UTC (17:30 /
    17:35 / 18:00); Cumartesi → Pazartesi, İşçi Bayramı 07.09 → 08.09, Şükran Günü 26.11 → 27.11;
    pencere (+ geç notu payı) bitince sonraki işlem günü; /acilis için pay yok
  • evren: PROPR ∩ hisse (endeks / emtia / döviz / kripto / listede olmayan hariç); iki dex'te aynı
    hisse tek satır (equity_dexes sırası)
  • ölçüm: açılış öncesi son işlem = referans (geç teslim edilen eski işlem ezmez); pencere içi
    ilk / tepe / dip / son / hacim; pencere sonrası ve evren dışı sayılmaz; tekrar teslim (tid)
    iki kez sayılmaz; 5 dk kopyası
  • rapor: |%| sırası, hacim tabanı (sayısı künyede), 24s katı, ilk N, † referans, 30 dk'da "5 dk:"
  • döngü (sahte saat): 5 dk ve 30 dk raporu tam zamanında birer kez, hisse kanalına; yeniden
    başlatmada çift yok; 10 dk geç → gönderilmez (notu /tani'de); yarıdan az görüldü → yok; kanal
    yok / bildirim kapalı / ölçüm kapalı; gönderim hatası 30 sn sonra yeniden, 10 dk sonra not
  • kapsam: görülmeyen aralıklar saatleriyle (yeniden başlatma, WS kopukluğu, şu an kopuk,
    açılıştan önceki 1 dk)
  • collector: işlem mesajı kancayı tid ile çağırır; kanca patlasa da akış sürer; kopukluk günlüğü
  • mesaj biçimi (tahmin sözü yok, sayıya ek yok); /acilis (sahip, hisse kanalı üyesi, yabancı
    grup sessiz, pencere içi / dışı / süreç yeni)
  • kablolama: tür, ayarlar, spawn, sağlık, TASK_TR, yardım, README, .env, tanı
"""
import asyncio
import datetime as dt
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-openmove.db")
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.hl import collector as colmod  # noqa: E402
from app.radar import openmove as om  # noqa: E402
from app.telegram import format as fmt  # noqa: E402

UTC = dt.timezone.utc
BANNED = ("muhtemel", "olası", "olabilir", "bekleniyor", "yükselecek", "düşecek")
A = "0x" + "a" * 40
B = "0x" + "b" * 40
DAY = 86400


def ts(y, m, d, hh=0, mm=0, ss=0):
    return int(dt.datetime(y, m, d, hh, mm, ss, tzinfo=UTC).timestamp())


O = ts(2026, 10, 7, 13, 30)            # Çar 07.10 açılış = 16:30 TSİ (yaz saati)

TICKERS = [("xyz:SNDK", "xyz", "SNDK"), ("xyz:CBRS", "xyz", "CBRS"), ("xyz:MU", "xyz", "MU"),
           ("xyz:GOLD", "xyz", "GOLD"), ("xyz:XYZ100", "xyz", "XYZ100"), ("xyz:EUR", "xyz", "EUR"),
           ("xyz:ZZZZ", "xyz", "ZZZZ"), ("BTC", "", "BTC")]
FOOT = ("<i>3 hisse izlendi (PROPR, endeks/emtia hariç) · pencerede işlem gören: 3 · hacmi $25K altında"
        " kalıp sıralamaya girmeyen: 1 · referans: açılıştan önceki son işlem († açılıştan önce işlem"
        " görülmedi: referans pencerenin ilk işlemi) · canlı işlem akışı · ölçüm, tahmin değil</i>")
REPORT5 = "\n".join([
    "🔔 <b>Açılışın en hareketlileri</b> — ilk 5 dk (16:30–16:35 TSİ) · Çar 07.10",
    "1. <b>CBRS</b>† 🔴 <b>-4.00%</b> · aralık %4.00 · $59K",
    "2. <b>SNDK</b> 🟢 <b>+2.79%</b> · aralık %2.99 · $87K (24s ort. 2.9×)",
    FOOT])
REPORT30 = "\n".join([
    "🔔 <b>Açılışın en hareketlileri</b> — ilk 30 dk (16:30–17:00 TSİ) · Çar 07.10",
    "1. <b>SNDK</b> 🟢 <b>+4.99%</b> · aralık %4.99 · $113K (24s ort. 0.6×) · 5 dk: +2.79%",
    "2. <b>CBRS</b>† 🔴 <b>-4.00%</b> · aralık %4.00 · $59K · 5 dk: -4.00%",
    FOOT])


def _cfg():
    cfg = Config()
    cfg.telegram_chat_id, cfg.telegram_owner_id = "111", "7"
    cfg.crypto_stocks_id, cfg.crypto_chat_id, cfg.account_chat_id = "-300", "-100", "-500"
    cfg.equity_dexes = ["xyz"]
    cfg.open_movers_enabled, cfg.notify_openmove = True, True
    cfg.open_movers_top, cfg.open_movers_min_usd = 10, 25000.0
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    cfg.public_bot_enabled = False
    return cfg


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "openmove.db"))
    async with dbm.db() as c:
        for coin, dex, sym in TICKERS:
            await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES(?,?,?)", (coin, dex, sym))
        # 24 saatlik hacim: 5 dk'ya düşen pay $30K, 30 dk'ya $180K
        await c.execute("INSERT INTO asset_metrics(coin,ts,day_volume) VALUES('xyz:SNDK',?,?)", (O - 3600, 8_640_000.0))
        await c.commit()
    om.REG = om._Reg()                 # süreç yeni açılmış gibi
    return _cfg()


class Live:
    """collector.LIVE taklidi: kopukluk günlüğü (koptu, yeniden abone oldu) ve şu anki kopukluk."""

    def __init__(self, down_log=(), down_since=0.0):
        self.down_log, self.down_since = list(down_log), down_since
        self.connected = not down_since


def _live(x):
    real = colmod.LIVE
    colmod.LIVE = x
    return real


class Sink:
    """Sahte Notifier: gönderilenleri tutar; ok=False → gönderim başarısız."""

    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    async def send(self, kind, text, **kw):
        self.sent.append((kind, text, kw))
        return self.ok


def feed(o=O):
    """Açılış o. SNDK: referans 501 (o−30); CBRS: açılıştan önce işlem yok (†); MU: taban altı."""
    ob = om.observe
    ob("xyz:SNDK", 500.0, 1, o - 120, "a1")
    ob("xyz:SNDK", 501.0, 1, o - 30, "a2")
    ob("xyz:SNDK", 499.0, 1, o - 90, "a0")       # geç teslim edilen ESKİ işlem referansı ezmez
    ob("xyz:GOLD", 2400.0, 1000, o + 10, "g1")   # emtia → evren dışı
    ob("BTC", 61000.0, 10, o + 10, "b1")         # kripto → evren dışı
    ob("xyz:SNDK", 505.0, 100, o + 10, "s1")     # $50,500
    ob("xyz:SNDK", 520.0, 50, o + 100, "s2")     # $26,000
    ob("xyz:SNDK", 520.0, 50, o + 100, "s2")     # yeniden bağlanınca tekrar teslim
    ob("xyz:SNDK", 515.0, 20, o + 299, "s3")     # $10,300 → 5 dk'nın son fiyatı
    ob("xyz:SNDK", 530.0, 40, o + 600, "s4")     # $21,200
    ob("xyz:SNDK", 526.0, 10, o + 1799, "s5")    # $5,260 → 30 dk'nın son fiyatı
    ob("xyz:SNDK", 600.0, 10, o + 1800, "s6")    # pencere sonrası
    ob("xyz:CBRS", 100.0, 300, o + 5, "c1")      # $30,000
    ob("xyz:CBRS", 96.0, 300, o + 200, "c2")     # $28,800 → −%4
    ob("xyz:MU", 190.0, 1, o - 10, "m0")
    ob("xyz:MU", 200.0, 10, o + 50, "m1")        # $2,000 → $25K tabanının altı


async def _sent_kv():
    return await dbm.kv_get(om.SENT_KV) or {}


# ---------------- takvim ----------------

def test_calendar_dst_holidays():
    d, o = om.session_for(ts(2026, 10, 7, 10, 0))
    assert d == dt.date(2026, 10, 7) and o == O
    assert (fmt.tr_time(o), fmt.tr_time(o + om.FIRST), fmt.tr_time(o + om.WINDOW)) == ("16:30", "16:35", "17:00")
    d, o = om.session_for(ts(2026, 11, 3, 9, 0))          # kış saati (ABD 1 Kasım'da geri aldı)
    assert d == dt.date(2026, 11, 3) and o == ts(2026, 11, 3, 14, 30)
    assert (fmt.tr_time(o), fmt.tr_time(o + om.FIRST), fmt.tr_time(o + om.WINDOW)) == ("17:30", "17:35", "18:00")
    assert om.session_for(ts(2026, 11, 2, 9, 0))[1] == ts(2026, 11, 2, 14, 30), "değişimden sonraki ilk Pazartesi"
    assert om.session_for(ts(2026, 10, 30, 9, 0))[1] == ts(2026, 10, 30, 13, 30), "Avrupa değişti, ABD henüz değil"
    assert om.session_for(ts(2026, 10, 10, 13, 35, 5))[0] == dt.date(2026, 10, 12), "Cumartesi → Pazartesi"
    assert om.session_for(ts(2026, 9, 7, 13, 35, 5))[0] == dt.date(2026, 9, 8), "İşçi Bayramı"
    assert om.session_for(ts(2026, 11, 26, 14, 35, 5))[0] == dt.date(2026, 11, 27), "Şükran Günü"
    # döngü: pencere + geç notu payı boyunca gün tutulur; /acilis için pay yok
    assert om.session_for(O + om.WINDOW + om.HOLD - 1)[0] == dt.date(2026, 10, 7)
    assert om.session_for(O + om.WINDOW + om.HOLD)[0] == dt.date(2026, 10, 8)
    assert om.session_for(O + om.WINDOW, tail=0) == (dt.date(2026, 10, 8), O + DAY)
    assert om.session_for(O + om.WINDOW - 1, tail=0) == (dt.date(2026, 10, 7), O)
    print("✅ takvim) yaz 16:30/16:35/17:00, kış 17:30/17:35/18:00 TSİ; Cumartesi, İşçi Bayramı, Şükran"
          " Günü atlanır; pencere + pay bitince sonraki gün; /acilis için pay yok")


# ---------------- evren + ölçüm ----------------

def test_universe_and_measure():
    async def run():
        cfg = await _fresh()
        assert await om.universe(cfg) == {"xyz:SNDK", "xyz:CBRS", "xyz:MU"}, "endeks/emtia/döviz/kripto/listesiz hariç"
        async with dbm.db() as c:
            await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES('flx:SNDK','flx','SNDK')")
            await c.commit()
        cfg.equity_dexes = ["xyz", "flx"]
        assert await om.universe(cfg) == {"xyz:SNDK", "xyz:CBRS", "xyz:MU"}, "iki dex'te aynı hisse tek satır"
        cfg.equity_dexes = ["flx", "xyz"]
        assert "flx:SNDK" in await om.universe(cfg) and "xyz:SNDK" not in await om.universe(cfg)
        cfg.equity_dexes = ["xyz"]
        # yapılandırılmadan (açılış bilinmeden) hiçbir şey sayılmaz
        om.observe("xyz:SNDK", 500.0, 1, O + 10, "z")
        assert not om.REG.agg30 and not om.REG.pre
        om.configure(dt.date(2026, 10, 7), O, O - 600)
        om.REG.coins = await om.universe(cfg)
        feed()
        r = om.REG
        assert r.pre == {"xyz:SNDK": (O - 30, 501.0), "xyz:MU": (O - 10, 190.0)}
        s5, s30 = r.agg5["xyz:SNDK"], r.agg30["xyz:SNDK"]
        assert (s5["first"], s5["hi"], s5["lo"], s5["last"], s5["n"]) == (505.0, 520.0, 505.0, 515.0, 3)
        assert abs(s5["vol"] - 86_800) < 1e-6
        assert (s30["hi"], s30["lo"], s30["last"], s30["n"]) == (530.0, 505.0, 526.0, 5) and abs(s30["vol"] - 113_260) < 1e-6
        assert set(r.agg30) == {"xyz:SNDK", "xyz:CBRS", "xyz:MU"} and r.trades == 8, r.trades
        # karışık sıralı teslim: ilk = en erken, son = en geç
        book = {}
        for px, t in ((10.0, 100), (9.0, 90), (11.0, 95)):
            om._add(book, "X", px, 1, t)
        assert (book["X"]["first"], book["X"]["last"], book["X"]["hi"], book["X"]["lo"]) == (9.0, 10.0, 11.0, 9.0)
        # 5 dk raporu: |%| sırası; † pencerenin ilk işlemi; taban altı sayılır; 24s katı
        rep = await om.build(cfg, 5, O + 305)
        assert [x["symbol"] for x in rep["rows"]] == ["CBRS", "SNDK"]
        cb, sn = rep["rows"]
        assert not cb["ref_pre"] and cb["ref"] == 100.0 and abs(cb["chg"] + 4.0) < 1e-9 and cb["mult"] is None
        assert sn["ref_pre"] and sn["ref"] == 501.0 and abs(sn["chg"] - 14 / 501 * 100) < 1e-9
        assert abs(sn["rng"] - 15 / 501 * 100) < 1e-9 and abs(sn["mult"] - 86_800 / 30_000) < 1e-9
        assert (rep["n_universe"], rep["n_traded"], rep["n_below"], rep["end_ts"]) == (3, 3, 1, O + 300)
        # 30 dk raporu: sıra değişir, satırda 5 dk değişimi
        rep = await om.build(cfg, 30, O + 1805)
        assert [x["symbol"] for x in rep["rows"]] == ["SNDK", "CBRS"]
        sn = rep["rows"][0]
        assert abs(sn["chg"] - 25 / 501 * 100) < 1e-9 and abs(sn["chg5"] - 14 / 501 * 100) < 1e-9
        assert abs(sn["mult"] - 113_260 / 180_000) < 1e-9 and rep["end_ts"] == O + 1800
        # ilk N; taban 0 → MU da girer (referans 190 → +%5.26 ile ilk sıra)
        cfg.open_movers_top = 1
        assert [x["symbol"] for x in (await om.build(cfg, 30, O + 1805))["rows"]] == ["SNDK"]
        cfg.open_movers_top, cfg.open_movers_min_usd = 10, 0
        rep = await om.build(cfg, 5, O + 305)
        assert [x["symbol"] for x in rep["rows"]] == ["MU", "CBRS", "SNDK"] and rep["n_below"] == 0
        # canlı (/acilis): pencere o ana kadar
        rep = await om.build(cfg, "live", O + 725)
        assert rep["end_ts"] == O + 725 and rep["which"] == "live"
        # yeniden bağlanma: HL son 30 işlemi ARTAN sırayla yeniden yollar (canlı görüldü) → yalnız
        # yeniler sayılır; aynı saniyedeki yeni tid sayılır; bellek coin başına tek saniye
        c = r.agg30["xyz:CBRS"]
        n0, v0 = c["n"], c["vol"]
        for px, t, tid in ((100.0, O + 5, "c1"), (96.0, O + 200, "c2"), (97.0, O + 200, "c3"), (98.0, O + 260, "c4")):
            om.observe("xyz:CBRS", px, 1, t, tid)
        assert c["n"] == n0 + 2 and abs(c["vol"] - (v0 + 97 + 98)) < 1e-9 and c["last"] == 98.0
        assert r.hwm["xyz:CBRS"] == (O + 260, {"c4"})
        print("✅ ölçüm) evren PROPR ∩ hisse, iki dex'te tek; referans açılış öncesi son işlem (eski teslim"
              " ezmez); tepe/dip/son/hacim; pencere sonrası, evren dışı, tekrar teslim sayılmaz (abonelik"
              " tekrarı); |%| sırası, taban, 24s katı, ilk N, 5 dk kopyası")
    asyncio.run(run())


# ---------------- döngü (sahte saat) ----------------

def test_tick_reports_once_on_time():
    async def run():
        cfg = await _fresh()
        real = _live(Live())
        sink = Sink()
        try:
            out = await om.tick(cfg, sink, O - 600)                   # 16:20: gün + evren
            assert out["day"] == "2026-10-07" and not out["sent"] and om.REG.coins == {"xyz:SNDK", "xyz:CBRS", "xyz:MU"}
            feed()
            for t in (O + 300, O + 304):                              # canlı akış payı (5 sn)
                assert not (await om.tick(cfg, sink, t))["sent"]
            out = await om.tick(cfg, sink, O + 305)
            assert out["sent"] == [5] and len(sink.sent) == 1
            kind, text, kw = sink.sent[0]
            assert kind == "openmove" and kw == {"chat_id": "-300", "key": "openmove:2026-10-07:5"}
            assert text == REPORT5, text
            for t in (O + 310, O + 900, O + 1804):
                assert not (await om.tick(cfg, sink, t))["sent"]
            out = await om.tick(cfg, sink, O + 1805)
            assert out["sent"] == [30] and len(sink.sent) == 2
            assert sink.sent[1][1] == REPORT30, sink.sent[1][1]
            assert sink.sent[1][2]["key"] == "openmove:2026-10-07:30"
            # yeniden başlatma: bellek sıfır, kv'deki işaret → çift yok
            om.REG = om._Reg()
            for t in (O + 1810, O + 2000, O + 2400):
                assert not (await om.tick(cfg, sink, t))["sent"]
            assert len(sink.sent) == 2
            snt = await _sent_kv()
            assert snt["day"] == "2026-10-07" and sorted(snt["sent"]) == [5, 30] and not snt["notes"]
            last = await dbm.kv_get(om.LAST_KV)
            assert last["which"] == 30 and last["day"] == "2026-10-07" and last["text"] == REPORT30
            # pay bitince sonraki işlem günü; önceki günün ölçümü taşınmaz
            out = await om.tick(cfg, sink, O + om.WINDOW + om.HOLD)
            assert out["day"] == "2026-10-08" and om.REG.open_ts == O + DAY and not om.REG.agg30 and not om.REG.pre
        finally:
            colmod.LIVE = real
        print("✅ döngü) 16:35:05'te 5 dk, 17:00:05'te 30 dk raporu birer kez hisse kanalına; yeniden"
              " başlatmada çift yok; ertesi gün temiz")
    asyncio.run(run())


def test_tick_late_partial_and_winter():
    async def run():
        cfg = await _fresh()
        real = _live(Live())
        sink = Sink()
        try:
            # Per 08.10: bot 16:46:40'ta açıldı → 5 dk raporu bayat; 30 dk'nın %44'ü görüldü → yok
            D1 = O + DAY
            out = await om.tick(cfg, sink, D1 + 1000)
            assert out["skipped"] == [5] and not sink.sent
            om.observe("xyz:SNDK", 500.0, 100, D1 + 1100, "x1")
            out = await om.tick(cfg, sink, D1 + 1805)
            assert out["skipped"] == [30] and not sink.sent
            notes = (await _sent_kv())["notes"]
            assert notes["5"] == ("rapor saatinin üzerinden 10 dk geçmişti (bot kapalı / döngü durmuştu)"
                                  " — bayat rapor gönderilmedi"), notes
            assert notes["30"] == "pencerenin görülen payı %44 (en az %50 gerekir) — rapor gönderilmedi", notes
            # Cum 09.10: bot 16:35:20'de açıldı → 5 dk'dan hiç görülmedi; 30 dk gider, boşluğu yazar
            D2 = O + 2 * DAY
            om.REG = om._Reg()
            out = await om.tick(cfg, sink, D2 + 320)
            assert out["skipped"] == [5]
            assert (await _sent_kv())["notes"]["5"].startswith("pencerenin görülen payı %0 ")
            om.observe("xyz:SNDK", 500.0, 100, D2 + 400, "f1")
            om.observe("xyz:SNDK", 510.0, 100, D2 + 900, "f2")
            out = await om.tick(cfg, sink, D2 + 1805)
            assert out["sent"] == [30] and len(sink.sent) == 1
            assert sink.sent[0][1].split("\n")[:3] == [
                "🔔 <b>Açılışın en hareketlileri</b> — ilk 30 dk (16:30–17:00 TSİ) · Cum 09.10",
                "⚠️ <i>canlı akışın görülmediği aralık: 16:29–16:35 — o aralığın işlemleri eksik</i>",
                "1. <b>SNDK</b>† 🟢 <b>+2.00%</b> · aralık %2.00 · $101K (24s ort. 0.6×)"], sink.sent[0][1]
            # Cumartesi ve İşçi Bayramı: rapor yok
            for t in (ts(2026, 10, 10, 13, 0), ts(2026, 10, 10, 13, 35, 5), ts(2026, 10, 10, 14, 0, 5)):
                out = await om.tick(cfg, sink, t)
                assert out["day"] == "2026-10-12" and not out["sent"] and not out["skipped"]
            for t in (ts(2026, 9, 7, 13, 0), ts(2026, 9, 7, 13, 35, 5)):
                assert (await om.tick(cfg, sink, t))["day"] == "2026-09-08"
            assert len(sink.sent) == 1
            # kış: Sal 03.11 17:35 ve 18:00 TSİ
            for t in (ts(2026, 11, 3, 14, 0), ts(2026, 11, 3, 14, 35, 4)):
                assert not (await om.tick(cfg, sink, t))["sent"]
            assert (await om.tick(cfg, sink, ts(2026, 11, 3, 14, 35, 5)))["sent"] == [5]
            assert (await om.tick(cfg, sink, ts(2026, 11, 3, 15, 0, 5)))["sent"] == [30]
            assert "— ilk 5 dk (17:30–17:35 TSİ) · Sal 03.11" in sink.sent[-2][1]
            assert "— ilk 30 dk (17:30–18:00 TSİ) · Sal 03.11" in sink.sent[-1][1]
            assert "Bu pencerede $25K üstü işlem gören hisse yok." in sink.sent[-1][1]
        finally:
            colmod.LIVE = real
        print("✅ geç / eksik) 10 dk geç → bayat, gönderilmez; yarıdan az görüldü → yok; yeniden başlatma"
              " aralığı raporda; hafta sonu / tatil yok; kış 17:35 / 18:00")
    asyncio.run(run())


def test_tick_switches_and_retry():
    async def run():
        cfg = await _fresh()
        real = _live(Live())
        try:
            # ölçüm kapalı → hiçbir şey
            cfg.open_movers_enabled = False
            sink = Sink()
            out = await om.tick(cfg, sink, O + 305)
            assert out.get("disabled") and not sink.sent and not await _sent_kv()
            cfg.open_movers_enabled = True
            # hisse kanalı yok → 5 dk atlanır; bildirim türü kapalı → 30 dk atlanır
            cfg.crypto_stocks_id = ""
            await om.tick(cfg, sink, O - 600)
            assert (await om.tick(cfg, sink, O + 305))["skipped"] == [5]
            cfg.crypto_stocks_id, cfg.notify_openmove = "-300", False
            assert (await om.tick(cfg, sink, O + 1805))["skipped"] == [30] and not sink.sent
            notes = (await _sent_kv())["notes"]
            assert notes == {"5": "hisse kanalı (CRYPTO_STOCKS_ID) yok",
                             "30": "bildirim kapalı (Ayarlar → Bildirimler → 🔔 Açılışın en hareketlileri)"}, notes
            cfg.notify_openmove = True
            # gönderim hatası: 30 sn sonra yeniden; düzelince bir kez gider
            D = O + 6 * DAY                                            # Sal 13.10
            bad = Sink(ok=False)
            await om.tick(cfg, bad, D - 600)
            assert not (await om.tick(cfg, bad, D + 305))["sent"] and len(bad.sent) == 1
            await om.tick(cfg, bad, D + 320)
            assert len(bad.sent) == 1, "30 sn dolmadan yeniden deneme yok"
            await om.tick(cfg, bad, D + 336)
            assert len(bad.sent) == 2
            bad.ok = True
            assert (await om.tick(cfg, bad, D + 366))["sent"] == [5] and len(bad.sent) == 3
            await om.tick(cfg, bad, D + 400)
            assert len(bad.sent) == 3
            # 30 dk raporu 10 dk boyunca gidemezse: gün tutulur, sebebiyle not düşer
            bad.ok = False
            await om.tick(cfg, bad, D + 1805)
            out = await om.tick(cfg, bad, D + om.WINDOW + om.GRACE + om.LATE_MAX + 5)
            assert out["day"] == "2026-10-13" and out["skipped"] == [30]
            snt = await _sent_kv()
            assert snt["notes"] == {"30": "10 dk boyunca gönderilemedi (Telegram hatası) — bayat rapor bırakıldı"}
            assert sorted(snt["sent"]) == [5, 30]
        finally:
            colmod.LIVE = real
        print("✅ anahtarlar) ölçüm kapalı → yok; kanal yok / bildirim kapalı → sebepli atlanır; hata → 30 sn"
              " sonra yeniden, 10 dk sonra Telegram notu")
    asyncio.run(run())


def test_coverage_gaps():
    async def run():
        cfg = await _fresh()
        D = O + 5 * DAY                                                # Pzt 12.10
        live = Live()
        real = _live(live)
        sink = Sink()
        try:
            await om.tick(cfg, sink, D - 600)
            # açılıştan 1 dk öncesindeki kopukluk yalnız referansı etkiler: pay %100 ama yazılır
            live.down_log = [(D - 200, D - 20)]
            cov = om.coverage(D, D + 300, D + 305)
            assert cov == {"frac": 1.0, "full": False, "gaps": [(D - 60, D - 20)]}, cov
            live.down_log = []
            assert om.coverage(D, D + 300, D + 305) == {"frac": 1.0, "full": True, "gaps": []}
            assert (await om.tick(cfg, sink, D + 305))["sent"] == [5] and "⚠️" not in sink.sent[-1][1]
            # pencerede iki kopukluk (biri 30 sn) + rapor anında hâlâ kopuk
            live.down_log = [(D + 600, D + 690), (D + 900, D + 930)]
            live.down_since, live.connected = D + 1500, False
            assert (await om.tick(cfg, sink, D + 1805))["sent"] == [30]
            lines = sink.sent[-1][1].split("\n")
            assert lines[1] == ("⚠️ <i>canlı akışın görülmediği aralık: 16:40–16:41, 16:45 (30 sn), 16:55–17:00"
                                " — o aralığın işlemleri eksik</i>"), lines[1]
            cov = om.coverage(D, D + 1800, D + 1805)
            assert abs(cov["frac"] - (1 - 420 / 1800)) < 1e-9 and len(cov["gaps"]) == 3
            # akış hiç yoksa (collector yok) yalnız gözlem başlangıcı sayılır
            colmod.LIVE = None
            assert om.coverage(D, D + 1800, D + 1805)["full"]
        finally:
            colmod.LIVE = real
        print("✅ kapsam) kopukluk aralıkları saatleriyle (30 sn'lik dahil), şu anki kopukluk rapor anına kadar;"
              " açılıştan önceki 1 dk referans için yazılır")
    asyncio.run(run())


def test_loop_one_turn():
    async def run():
        cfg = await _fresh()
        real = _live(Live())
        naps = []

        class Aio:
            CancelledError = asyncio.CancelledError

            @staticmethod
            async def sleep(sec):
                naps.append(sec)
                if len(naps) >= 2:
                    raise asyncio.CancelledError

        real_aio, real_now = om.asyncio, om.now
        om.asyncio, om.now = Aio, (lambda: O + 305)
        try:
            try:
                await om.loop(cfg, Sink())
            except asyncio.CancelledError:
                pass
        finally:
            om.asyncio, om.now = real_aio, real_now
            colmod.LIVE = real
        st = await dbm.kv_get(om.STATS_KV)
        assert naps == [10, 5] and st["day"] == "2026-10-07" and st["coins"] == 3 and st["skipped"] == [5], st
        assert st["open_ts"] == O and st["since"] == O + 305
        print("✅ döngü turu) 10 sn bekler, adım atar, durum kv'ye (açılışta açılan süreç 5 dk raporunu atlar), 5 sn")
    asyncio.run(run())


# ---------------- collector kancası ----------------

def test_collector_hook():
    async def run():
        cfg = await _fresh()
        real = colmod.LIVE
        from app.radar import twaplive
        try:
            col = colmod.Collector(cfg, None)
            assert colmod.LIVE is col and col.down_since > 0 and not col.down_log, "ilk aboneliğe kadar akış yok"
            col.valid_coins = {"xyz:SNDK", "BTC"}
            o = dbm.now() - 100
            om.configure(dt.date(2026, 10, 7), o, o - 600)
            om.REG.coins = {"xyz:SNDK"}

            def tr(px, sz, t, tid, coin="xyz:SNDK"):
                return {"coin": coin, "side": "B", "px": str(px), "sz": str(sz), "time": t * 1000,
                        "tid": tid, "users": [A, B]}
            await col._handle(json.dumps({"channel": "trades", "data": [
                tr(500, 1, o - 5, 1), tr(505, 1, o + 3, 2), tr(510, 1, o + 4, 3), tr(61000, 0.001, o + 4, 4, "BTC"),
                {"coin": "xyz:SNDK", "px": "x", "sz": "1", "time": (o + 5) * 1000, "tid": 5, "users": [A, B]}]}))
            await col._handle(json.dumps({"channel": "trades", "data": [tr(510, 1, o + 4, 3)]}))   # tekrar teslim
            a = om.REG.agg30["xyz:SNDK"]
            assert om.REG.pre["xyz:SNDK"] == (o - 5, 500.0) and a["n"] == 2 and abs(a["vol"] - 1015) < 1e-9
            assert "BTC" not in om.REG.agg30 and om.REG.errors == 0
            # kanca patlasa da akış sürer (sonraki kanca çalışır), hata sayılır
            seen = []
            real_obs, real_tl = om.observe, twaplive.observe

            def boom(*a, **k):
                raise RuntimeError("kanca")
            om.observe = boom
            twaplive.observe = lambda *a, **k: seen.append(a[0])
            try:
                await col._handle(json.dumps({"channel": "trades", "data": [tr(520, 1, o + 50, 9)]}))
            finally:
                om.observe, twaplive.observe = real_obs, real_tl
            assert om.REG.errors == 1 and seen == ["xyz:SNDK", "xyz:SNDK"], seen
        finally:
            colmod.LIVE = real
        src = open(os.path.join(ROOT, "app/hl/collector.py"), encoding="utf-8").read()
        assert "self.down_log.append((self.down_since, time.time()))" in src
        assert "if not self.down_since:\n                    self.down_since = time.time()" in src
        print("✅ collector) işlem mesajı kancayı tid ile çağırır, tekrar teslim sayılmaz; kanca patlasa da"
              " akış sürer; kopukluk günlüğü")
    asyncio.run(run())


# ---------------- mesaj biçimi ----------------

def test_message_format():
    def rep(**kw):
        base = {"which": 5, "day": "2026-10-07", "open_ts": O, "end_ts": O + 300, "ts": O + 305,
                "rows": [], "n_universe": 58, "n_traded": 40, "n_below": 12, "floor": 25000.0,
                "cover": {"frac": 1.0, "full": True, "gaps": []}}
        base.update(kw)
        return base
    rows = [{"symbol": "SNDK", "ref_pre": True, "chg": 4.823, "rng": 6.1, "vol": 3.2e6, "mult": 14.2},
            {"symbol": "CBRS", "ref_pre": True, "chg": -3.9, "rng": 5.02, "vol": 1.1e6, "mult": 9.04},
            {"symbol": "MU", "ref_pre": True, "chg": 0.0, "rng": 0.4, "vol": 30_000.0, "mult": None}]
    text = fmt.openmove_report(rep(rows=rows))
    assert text == "\n".join([
        "🔔 <b>Açılışın en hareketlileri</b> — ilk 5 dk (16:30–16:35 TSİ) · Çar 07.10",
        "1. <b>SNDK</b> 🟢 <b>+4.82%</b> · aralık %6.10 · $3.2M (24s ort. 14×)",
        "2. <b>CBRS</b> 🔴 <b>-3.90%</b> · aralık %5.02 · $1.1M (24s ort. 9.0×)",
        "3. <b>MU</b> ⚪ <b>+0.00%</b> · aralık %0.40 · $30K",
        "<i>58 hisse izlendi (PROPR, endeks/emtia hariç) · pencerede işlem gören: 40 · hacmi $25K altında"
        " kalıp sıralamaya girmeyen: 12 · referans: açılıştan önceki son işlem · canlı işlem akışı ·"
        " ölçüm, tahmin değil</i>"]), text
    empty = fmt.openmove_report(rep(which=30, end_ts=O + 1800, n_traded=0, n_below=0))
    assert empty.split("\n")[1] == "Bu pencerede $25K üstü işlem gören hisse yok."
    gaps = [(O - 60, O - 20)] + [(O + 60 * i, O + 60 * i + 90) for i in range(2, 7)]
    live = fmt.openmove_report(rep(which="live", end_ts=O + 725, cover={"frac": 0.7, "full": False, "gaps": gaps}))
    head, warn = live.split("\n")[:2]
    assert head == "🔔 <b>Açılışın en hareketlileri</b> — şu ana kadar, ilk 12 dk (16:30–16:42 TSİ) · Çar 07.10"
    assert warn == ("⚠️ <i>canlı akışın görülmediği aralık: 16:29 (40 sn), 16:32–16:33, 16:33–16:34, 16:34–16:35"
                    " (+2) — o aralığın işlemleri eksik</i>"), warn
    for t in (text, empty, live, fmt.openmove_report(rep(rows=[dict(rows[0], ref_pre=False, chg5=-1.5)]))):
        low = t.lower()
        assert not any(w in low for w in BANNED), t
        assert not re.search(r"\d['’]", t), "sayıya ek yok"
    idle = fmt.openmove_idle({"ts": O - DAY + 1805, "day": "2026-10-06", "text": "ESKİ"}, dt.date(2026, 10, 7), O)
    assert idle == ("🔔 Açılış penceresi dışında. Sıradaki ölçüm <b>Çar 07.10</b>: 16:30–16:35 (5 dk raporu)"
                    " ve 17:00 (30 dk raporu) TSİ.\n\n<i>Son rapor (Sal 06.10 17:00):</i>\nESKİ"), idle
    assert fmt.openmove_idle({}, dt.date(2026, 11, 3), ts(2026, 11, 3, 14, 30)).endswith(
        "<b>Sal 03.11</b>: 17:30–17:35 (5 dk raporu) ve 18:00 (30 dk raporu) TSİ.")
    print("✅ mesaj) başlık, satır (ok, %, aralık, hacim, 24s katı), boş pencere, kopukluk satırı (+N),"
          " canlı başlık, bekleme cevabı; tahmin sözü yok, sayıya ek yok")


# ---------------- /acilis ----------------

def test_acilis_command():
    async def run():
        cfg = await _fresh()
        real = _live(Live())
        from app.telegram.bot import TelegramBot
        bot = TelegramBot(cfg, None, None, {})
        sent = []

        async def fake_send(text, chat_id=None, reply_markup=None):
            sent.append((chat_id, text))
            return True
        bot.send = fake_send

        async def cmd(text, chat=111, uid=111, ctype="private"):
            await bot._handle_update({"message": {"chat": {"id": chat, "type": ctype},
                                                  "from": {"id": uid}, "text": text}})
        real_now = om.now
        try:
            om.now = lambda: O - 1800                                  # 16:00
            await om.tick(cfg, Sink(), O - 1800)
            await dbm.kv_set(om.LAST_KV, {"ts": O - DAY + 1805, "which": 30, "day": "2026-10-06", "text": "ESKİ RAPOR"})
            await cmd("/acilis")
            assert sent[-1] == ("111", "🔔 Açılış penceresi dışında. Sıradaki ölçüm <b>Çar 07.10</b>: 16:30–16:35"
                                       " (5 dk raporu) ve 17:00 (30 dk raporu) TSİ.\n\n<i>Son rapor (Sal 06.10"
                                       " 17:00):</i>\nESKİ RAPOR"), sent[-1]
            feed()
            om.now = lambda: O + 725                                   # 16:42:05 — pencere içi
            await cmd("/açılış", chat=-300, uid=42, ctype="supergroup")   # hisse kanalından üye
            assert sent[-1][0] == "-300" and sent[-1][1].startswith(
                "🔔 <b>Açılışın en hareketlileri</b> — şu ana kadar, ilk 12 dk (16:30–16:42 TSİ)"), sent[-1]
            assert "1. <b>SNDK</b> 🟢 <b>+4.99%</b>" in sent[-1][1]
            n = len(sent)
            await cmd("/acilis", chat=-999, uid=42, ctype="supergroup")   # yabancı grup: sessiz
            assert len(sent) == n
            om.now = lambda: O + 2100                                  # 17:05: pencere bitti → yarın
            await cmd("/acilis")
            assert "Sıradaki ölçüm <b>Per 08.10</b>" in sent[-1][1] and "ESKİ RAPOR" in sent[-1][1]
            om.REG = om._Reg()                                         # süreç açılışta yeni başladı
            om.now = lambda: O + DAY + 60
            await cmd("/acilis")
            assert sent[-1][1].startswith("🔔 Açılış penceresi açık ama ölçüm bu süreçte henüz başlamadı")
        finally:
            om.now = real_now
            colmod.LIVE = real
        assert "/acilis" in fmt.help_text()
        print("✅ /acilis) dışarıda sıradaki saatler + son rapor (tarihli); içeride anlık sıralama; hisse"
              " kanalı üyesi kullanır, yabancı grup sessiz; süreç yeniyse dürüst cevap")
    asyncio.run(run())


# ---------------- kablolama ----------------

def test_wiring():
    async def run():
        from app import diag, health, notify
        src = lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read()   # noqa: E731
        assert '_spawn("openmove", lambda: openmove.loop(cfg, notifier), notifier)' in src("app/main.py")
        assert "openmove.observe(coin, px, sz, ts, tid)" in src("app/hl/collector.py")
        assert notify.KINDS["openmove"][0] == "notify_openmove" and notify.KINDS["openmove"][2] == "normal"
        cfg = Config()
        assert cfg.open_movers_enabled and cfg.notify_openmove
        assert cfg.open_movers_top == 10 and cfg.open_movers_min_usd == 25000
        for f in ("open_movers_enabled", "open_movers_top", "open_movers_min_usd"):
            meta = EDITABLE_FIELDS[f]
            assert meta["group"] == "🔔 Açılışın en hareketlileri" and meta["label"] and len(meta["desc"]) > 30, f
        assert EDITABLE_FIELDS["notify_openmove"]["group"] == "Bildirimler"
        assert health.limits(cfg)["openmove"] == 600 and health.periods(cfg)["openmove"] == 5
        assert fmt.TASK_TR["openmove"] == "açılışın en hareketlileri" and "/acilis" in fmt.help_text()
        readme, env = src("README.md"), src(".env.example")
        assert "## 🔔 Açılışın En Hareketlileri (hisse kanalı)" in readme and "| `/acilis` |" in readme
        assert "OPEN_MOVERS_TOP=10" in env and "OPEN_MOVERS_MIN_USD=25000" in env
        cfg2 = await _fresh()
        await dbm.kv_set(om.STATS_KV, {"coins": 58, "trades": 1234, "errors": 2, "day": "2026-10-07"})
        await dbm.kv_set(om.SENT_KV, {"day": "2026-10-07", "sent": [5, 30],
                                      "notes": {"30": "hisse kanalı (CRYPTO_STOCKS_ID) yok"}})
        txt = "\n".join(await diag._subsystems(cfg2))
        line = [x for x in txt.split("\n") if "açılış hareketlileri" in x]
        assert line == ["  açılış hareketlileri: 58 hisse izleniyor · gün 2026-10-07 · gönderilen 5 dk · bu"
                        " pencerede 1234 işlem · ⚠️ kanca hatası 2 · atlanan: 30 dk: hisse kanalı"
                        " (CRYPTO_STOCKS_ID) yok"], line
        print("✅ kablolama) spawn, kanca, bildirim türü, 3 ayar künyeli + bildirim anahtarı, sağlık, TASK_TR,"
              " yardım, README, .env; /tani satırı (atlanan gönderilmiş sayılmaz)")
    asyncio.run(run())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
