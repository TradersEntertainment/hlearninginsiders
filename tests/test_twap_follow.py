"""👁 TWAP emri takibi — "takip butonu olsun, iptal edildiğinde bildirim istiyorum".

Kullanıcı kararı: bildirim **yalnız takip ettikleri** için gelsin, kanal sessiz
kalsın. Bu yüzden POZİSYON takibinden (`tracker`) ayrı bir hat:

  • `tracker` canlı perp pozisyonuna bakar (`live_position`); TWAP'ta izlenecek
    şey EMRİN KENDİSİDİR. Spot TWAP'ta (@107) pozisyon yoktur, emir vardır —
    eski akış "zaten kapatmış" derdi.
  • Durum HL EMİR GEÇMİŞİNDEN okunur, bellekten değil: `twaplive.REG` 30 dk boşta
    kalan turu düşürür; iptal ondan sonra gelirse bellekte karşılığı kalmaz.

Pinlenenler:
  • alarm mesajında `/takip_N`; teklif `kind='twap'` (pozisyon teklifiyle aynı
    tablo, aynı komut, farklı dal)
  • basınca `twap_follows` satırı açılır — POZİSYON takibi (trackers) AÇILMAZ
  • iptal → ⛔, bitiş → 🏁, %50 → 👁; terminal durumda takip kapanır
  • BAŞARISIZ GÖNDERİM TAKİBİ KAPATMAZ (kanal notundaki hatanın tersi)
  • adres başına TEK emir geçmişi isteği; süresi dolan takip sessizce kapanır
  • bitiş notundan önce emir bir kez daha zorla sorulur (araya giren iptal
    "🏁 bitti" diye yazılmasın)
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-twapfollow.db")
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.radar import twapfollow as tf  # noqa: E402
from app.telegram import format as fmt  # noqa: E402

A = "0x" + "a" * 40
MARK, SZ, PLAN = 10.0, 200_000.0, 2_000_000.0


def order(status="activated", ex=0.0, started=1000):
    return [{"time": started * 1000,
             "state": {"coin": "INJ", "side": "B", "sz": str(SZ), "executedSz": str(ex),
                       "minutes": 360, "reduceOnly": False, "randomize": False},
             "status": {"status": status}, "ended": status != "activated"}]


class Col:
    def __init__(self, hist=None):
        self.hist = order() if hist is None else hist
        self.calls = 0

    async def fetch_twap_history(self, addr, timeout=6.0):
        self.calls += 1
        return self.hist


class Bot:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    async def send(self, kind, text, **kw):
        self.sent.append((kw.get("chat_id"), text))
        return self.ok


async def _seed(addrs=(A,)):
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "tf.db"))
    t = dbm.now()
    async with dbm.db() as c:
        for a in addrs:
            await c.execute(
                "INSERT INTO twap_runs(coin,address,side,first_ts,last_ts,n_slices,total,avg_slice,"
                "avg_gap,cv_gap,cv_size,taker_pct,ts,src,planned_usd,executed_usd,order_status,"
                "px_first,px_last,sz_total,day_volume) VALUES('INJ',?,'buy',1000,?,40,400000,10000,"
                "30,0.1,0.1,50,?,'live',?,0,'activated',10.0,10.2,40000,5e7)", (a, t, t, PLAN))
    return t


def test_offer_and_alert_line():
    async def run():
        await _seed()
        from app.radar import twaplive as tl
        run_ = tl.Run("INJ", A, "buy", 1000, MARK)
        run_.total = 400_000.0
        oid = await tl._twap_offer(run_)
        async with dbm.db() as c:
            cur = await c.execute("SELECT * FROM track_offers WHERE id=?", (oid,))
            row = dict(await cur.fetchone())
        assert row["kind"] == "twap" and row["ref_ts"] == 1000 and row["coin"] == "INJ"
        assert row["side"] == "buy" and row["symbol"] == "INJ"
        m = {"coin": "INJ", "address": A, "side": "buy", "n": 40, "avg_slice": 10_000,
             "median_gap": 30, "px_first": 10.0, "px_last": 10.2, "px_chg_pct": 2.0}
        o = {"planned_sz": SZ, "planned_usd": PLAN, "executed_usd": 0, "filled_pct": 0,
             "remaining_usd": PLAN, "minutes": 360, "left_sec": 900, "started_ts": 1000,
             "status": "activated"}
        ctx = {"order": o, "day_vol": 5e7, "vol_pct": 4.0, "pos": None, "klass": "kripto"}
        assert f"👁 emri takip et → /takip_{oid}" in fmt.twap_alert(m, {**ctx, "offer": oid})
        assert "/takip_" not in fmt.twap_alert(m, ctx), "teklif yoksa düğme de yok"
        print("✅ teklif) alarmda /takip_N; teklif kind='twap' + ref_ts=turun first_ts'i")
    asyncio.run(run())


def test_button_opens_follow_not_tracker():
    async def run():
        await _seed()
        cfg = Config()
        async with dbm.db() as c:
            cur = await c.execute(
                "INSERT INTO track_offers(address,coin,symbol,side,notional,created_ts,kind,ref_ts)"
                " VALUES(?,'INJ','INJ','buy',400000,?,'twap',1000)", (A, dbm.now()))
            oid = cur.lastrowid
        from app.telegram.bot import TelegramBot
        bot = TelegramBot.__new__(TelegramBot)
        bot.cfg, bot.client = cfg, None
        sent = []
        async def _send(t, chat=""):
            sent.append((chat, t))
            return True
        bot.send = _send
        bot._track_chat = lambda c: "" if str(c) == str(cfg.telegram_chat_id) else str(c)
        await bot._cmd_track_start(f"takip_{oid}", "-100")
        fl = await tf.active()
        assert len(fl) == 1 and fl[0]["coin"] == "INJ" and fl[0]["first_ts"] == 1000
        assert fl[0]["chat_id"] == "-100", "haber komutun geldiği sohbete gider"
        async with dbm.db() as c:
            cur = await c.execute("SELECT COUNT(*) n FROM trackers")
            assert (await cur.fetchone())["n"] == 0, "POZİSYON takibi açılmaz — emir takibi bu"
            cur = await c.execute("SELECT used FROM track_offers WHERE id=?", (oid,))
            assert (await cur.fetchone())["used"] == 1
        assert "TWAP emri takipte" in sent[-1][1] and "/birak_twap_" in sent[-1][1]
        # ikinci basış yeni kayıt açmaz (UNIQUE) — aynı id döner
        assert await tf.start(cfg, "INJ", A, "buy", 1000) == fl[0]["id"]
        assert len(await tf.active()) == 1
        print("✅ düğme) /takip_N 'twap' teklifinde EMİR takibi açar (pozisyon takibi değil);"
              " ikinci basış çoğaltmaz")
    asyncio.run(run())


def test_cancel_end_progress():
    async def run():
        cfg = Config()
        await _seed()
        await tf.start(cfg, "INJ", A, "buy", 1000, chat_id="-100")
        col, bot = Col(), Bot()
        out = await tf.check(cfg, col, bot)
        assert out["checked"] == 1 and not bot.sent, "activated → haber yok"
        col.hist = order(ex=SZ * 0.6)
        out = await tf.check(cfg, col, bot)
        assert out["progress"] == 1 and "👁 <b>takip ettiğin emir</b>" in bot.sent[-1][1]
        assert "TWAP yarılandı" in bot.sent[-1][1] and bot.sent[-1][0] == "-100"
        out = await tf.check(cfg, col, bot)
        assert out["progress"] == 0 and len(bot.sent) == 1, "yarılanma tek sefer"
        col.hist = order(status="terminated", ex=SZ * 0.6)
        out = await tf.check(cfg, col, bot)
        assert out["cancelled"] == 1 and out["ended"] == 0
        e = bot.sent[-1][1]
        assert "⛔ <b>TWAP iptal edildi</b>" in e and "🏁" not in e, e
        assert e.count("⛔") == 1, "başlık çiftlenmiyor"
        assert not await tf.active(), "terminal durumda takip kapanır"
        out = await tf.check(cfg, col, bot)
        assert out["active"] == 0 and len(bot.sent) == 2, "kapandıktan sonra susar"
        # bitiş yolu: finished → 🏁
        await _seed()
        await tf.start(cfg, "INJ", A, "buy", 1000, chat_id="-100")
        bot2 = Bot()
        out = await tf.check(cfg, Col(order(status="finished", ex=SZ)), bot2)
        assert out["ended"] == 1 and "🏁 <b>TWAP bitti</b>" in bot2.sent[-1][1]
        print("✅ bildirim) iptal → ⛔, bitiş → 🏁, %50 → 👁 tek sefer; terminal durumda kapanır")
    asyncio.run(run())


def test_failed_send_and_expiry_and_budget():
    async def run():
        cfg = Config()
        await _seed()
        await tf.start(cfg, "INJ", A, "buy", 1000, chat_id="-100")
        col = Col(order(status="terminated", ex=SZ * 0.6))
        out = await tf.check(cfg, col, Bot(ok=False))
        assert out["failed"] == 1 and out["cancelled"] == 0
        assert len(await tf.active()) == 1, "BAŞARISIZ GÖNDERİM TAKİBİ KAPATMAZ"
        bot = Bot()
        out = await tf.check(cfg, col, bot)
        assert out["cancelled"] == 1 and "⛔" in bot.sent[-1][1], "sonraki tur yeniden dener"
        # süre dolunca sessizce kapanır (mesaj yok)
        await _seed()
        fid = await tf.start(cfg, "INJ", A, "buy", 1000)
        async with dbm.db() as c:
            await c.execute("UPDATE twap_follows SET expires_ts=? WHERE id=?",
                            (dbm.now() - 1, fid))
        bot2 = Bot()
        out = await tf.check(cfg, Col(), bot2)
        assert out["expired"] == 1 and not bot2.sent and not await tf.active()
        # bütçe: aynı adresteki iki takip TEK istek
        await _seed()
        await tf.start(cfg, "INJ", A, "buy", 1000)
        async with dbm.db() as c:
            await c.execute("INSERT INTO twap_follows(coin,address,side,first_ts,chat_id,"
                            "created_ts,expires_ts,active) VALUES('HYPE',?,'sell',1000,'',?,?,1)",
                            (A, dbm.now(), dbm.now() + 86400))
        col2 = Col()
        await tf.check(cfg, col2, Bot())
        assert col2.calls == 1, "adres başına TEK userTwapHistory isteği"
        # collector yoksa tahminle bildirim YOK
        assert (await tf.check(cfg, None, Bot()))["checked"] == 0
        cfg.twap_follow_enabled = False
        assert (await tf.check(cfg, Col(), Bot()))["active"] == 0, "kapalıysa hiç dönmez"
        print("✅ dayanıklılık) başarısız gönderim kapatmaz · süre dolunca sessiz ·"
              " adres başına tek istek · collector yoksa bildirim yok")
    asyncio.run(run())


def test_wiring():
    rd = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()  # noqa: E731
    for f in ("twap_follow_enabled", "twap_follow_poll_sec", "twap_follow_expire_days",
              "twap_follow_progress"):
        assert f in EDITABLE_FIELDS and hasattr(Config(), f), f
        assert EDITABLE_FIELDS[f]["group"] == "TWAP radarı", f
        assert all(EDITABLE_FIELDS[f].get(x) for x in ("type", "label", "group", "desc")), f
    assert "TWAP_FOLLOW_POLL_SEC=" in rd(".env.example")
    assert '_spawn("twapfollow"' in rd("app", "main.py"), "döngü başlatılıyor"
    assert "twap_follows" in rd("app", "db.py") and "ADD COLUMN kind TEXT" in rd("app", "db.py")
    b = rd("app", "telegram", "bot.py")
    assert 'offer.get("kind") or "pos"' in b, "eski teklifler 'pos' okunur (geriye dönük)"
    assert "_cmd_twap_follow" in b and "twaptakipler" in b and "birak_twap_" in b
    assert "/kriptoliq" in rd("README.md") and "/takip_N" in rd("README.md")
    # kanal notu: bitiş demeden önce emir bir kez daha zorla sorulur
    t = rd("app", "radar", "twaplive.py")
    assert "if not done and collector is not None:" in t, "araya giren iptal '🏁' yazılmasın"
    assert 'force=True' in t
    print("✅ bağlantı) 4 ayar künyeli · döngü kayıtlı · tablo+migration · komutlar ·"
          " README · bitiş öncesi zorla sorgu")
