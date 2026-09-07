"""🛒 Herkese açık DM akışı (satılabilir bot) — public.py + bot.py yönlendirme.

Pinlenenler:
  • /start kayıt + karşılama + inline menü; düz metin = coin sorgusu → resim + altyazı,
    altyazıda kalan hak; önbellekten ikinci sorgu HL'ye gitmez ama hak yer
  • tanınmayan metin hak yemez; 4. sorgu → limit + /pro; gün dönünce yenilenir; /hesap
  • Pro: sınırsız, dakika limiti; global kova dolunca 'yoğunluk' (hak yenmez, önbellek muaf)
  • bayrak kapalı → yabancı DM'e yalnız chat id; grup sessiz; sahip zinciri ve kendi
    kanalı eski davranış; TELEGRAM_CHAT_ID boşken kimse sahip değil (fail-open kapalı)
  • callback düğmeleri; /adres doğrulama; 403 → users.blocked_ts, /start ile kalkar;
    sel koruması; PUBLIC_COMMANDS; paket/Stars hesabı
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-public.db")
from app import db as dbm
from app import users
from app.config import Config
from app.hl import universe as uni
from app.telegram import public
from app.telegram.bot import PUBLIC_COMMANDS, TelegramBot

MARK = {"HYPE": 40.0, "SOL": 170.0}
A, B, C = ("0x" + c * 40 for c in "abc")


def synth_candles(mark, n=96):
    t0 = (dbm.now() - n * 1800) * 1000
    out, px = [], mark * 0.98
    for i in range(n):
        o = px
        px = px * (1 + (0.0004 if i % 3 else -0.0005))
        out.append({"t": t0 + i * 1800 * 1000, "o": str(o), "h": str(max(o, px) * 1.001),
                    "l": str(min(o, px) * 0.999), "c": str(px), "v": "1"})
    return out


class Client:
    def __init__(self):
        self.ctx_calls = self.candle_calls = self.book_calls = 0

    async def meta_and_ctxs(self, dex=""):
        self.ctx_calls += 1
        return [{"universe": [{"name": c} for c in MARK]},
                [{"markPx": str(m), "openInterest": "1", "funding": "0", "dayNtlVlm": "1"} for m in MARK.values()]]

    async def l2_book(self, coin, n_sig_figs=None):
        self.book_calls += 1
        m = MARK.get(coin, 1.0)
        return {"levels": [[{"px": str(m * 0.995), "sz": "1e6"}, {"px": str(m * 0.98), "sz": "1e6"}],
                           [{"px": str(m * 1.005), "sz": "1e6"}, {"px": str(m * 1.02), "sz": "1e6"}]]}

    async def candles(self, coin, interval, start_ms, end_ms):
        self.candle_calls += 1
        return synth_candles(MARK.get(coin, 1.0))


def _cfg():
    cfg = Config()
    cfg.telegram_chat_id = "111"
    cfg.crypto_chat_id = "-100"
    cfg.public_bot_enabled = True
    cfg.free_daily_queries = 3
    cfg.pro_query_per_min = 6
    cfg.query_global_per_min = 40
    cfg.query_cache_sec = 60
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    return cfg


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "pb.db"))
    users.reset_memory()
    public.reset_memory()
    now = dbm.now()
    async with dbm.db() as c:
        for coin, addr, side, liq, ntl in (("HYPE", A, "long", 39.0, 2_000_000), ("HYPE", B, "short", 41.5, 900_000),
                                           ("SOL", C, "long", 165.0, 1_500_000)):
            await c.execute(
                "INSERT INTO addr_positions(coin,address,dex,side,szi,entry_px,leverage,liq_px,upnl,notional,ts,closed_ts)"
                " VALUES(?,?,'',?,1,?,5,?,0,?,?,NULL)", (coin, addr, side, MARK[coin], liq, ntl, now - 600))
    await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {k: {"m": v, "oi": 1, "f": 0, "v": 1, "p": None} for k, v in MARK.items()},
                                       "ts": now})


def _bot(cfg, cli):
    bot = TelegramBot(cfg, None, cli, {})
    sent, photos, cbs = [], [], []

    async def fake_send(text, chat_id=None, reply_markup=None):
        sent.append((chat_id, text, reply_markup))
        return True

    async def fake_photo(png, caption="", chat_id=None, reply_markup=None):
        photos.append((chat_id, caption, len(png)))
        return True

    async def fake_cb(cq_id, text="", alert=False):
        cbs.append(cq_id)
        return True
    bot.send, bot.send_photo, bot.answer_callback = fake_send, fake_photo, fake_cb
    return bot, sent, photos, cbs


def dm(uid, text, chat=None, typ="private"):
    return {"message": {"chat": {"id": chat or uid, "type": typ},
                        "from": {"id": uid, "first_name": "Ali", "username": "ali"}, "text": text}}


def cq(uid, data, cid="cq1"):
    return {"callback_query": {"id": cid, "from": {"id": uid, "first_name": "Ali"},
                               "message": {"chat": {"id": uid, "type": "private"}, "message_id": 7}, "data": data}}


# ------------------------------------------------ 1) /start, sorgu, kota, önbellek, gün dönümü
def test_start_query_quota():
    async def run():
        await _fresh()
        cfg = _cfg()
        cli = Client()
        bot, sent, photos, cbs = _bot(cfg, cli)
        await bot._handle_update(dm(555, "/start"))
        u = await users.get(555)
        assert u and u["chat_id"] == "555" and u["username"] == "ali"
        assert sent[-1][0] == "555" and "günde <b>3</b> sorgu" in sent[-1][1] and sent[-1][2]["inline_keyboard"], sent[-1]
        # 1) düz metin → resim + altyazı (tam metin sığar), kalan hak
        await bot._handle_update(dm(555, "hype"))
        assert len(photos) == 1 and photos[0][0] == "555" and "<b>HYPE</b>" in photos[0][1], photos
        assert "bugün <b>2/3</b> sorgu kaldı" in photos[0][1] and "/pro" in photos[0][1], photos[0][1]
        assert cli.candle_calls == 1 and cli.ctx_calls == 0 and (await users.get(555))["q_used"] == 1
        # 2) /HYPE → önbellek: HL'ye gidilmez, hak yine yenir
        await bot._handle_update(dm(555, "/HYPE"))
        assert len(photos) == 2 and cli.candle_calls == 1 and "bugün <b>1/3</b>" in photos[1][1]
        # tanınmayan metin hak yemez
        await bot._handle_update(dm(555, "merhaba nasılsın"))
        assert "tanımadım" in sent[-1][1] and (await users.get(555))["q_used"] == 2
        await bot._handle_update(dm(555, "!!!"))
        assert "tanımadım" in sent[-1][1]
        # 3) SOL → başka coin, HL'ye gider
        await bot._handle_update(dm(555, "sol"))
        assert len(photos) == 3 and "<b>SOL</b>" in photos[2][1] and "0/3" in photos[2][1] and cli.candle_calls == 2
        # 4) limit → resim yok, /pro yönlendirmesi
        await bot._handle_update(dm(555, "hype"))
        assert len(photos) == 3 and "Bugünlük 3 ücretsiz sorgun bitti" in sent[-1][1] and "/pro" in sent[-1][1]
        await bot._handle_update(dm(555, "/hesap"))
        assert "🆓 Ücretsiz" in sent[-1][1] and "3/3" in sent[-1][1] and "Toplam sorgu: 3" in sent[-1][1], sent[-1][1]
        # gün dönünce yenilenir
        async with dbm.db() as c:
            await c.execute("UPDATE users SET q_day='2000-01-01' WHERE id=555")
        await bot._handle_update(dm(555, "hype"))
        assert len(photos) == 4 and "2/3" in photos[3][1]
        print("✅ akış) /start kayıt+menü; sorgu resim+altyazı; önbellek; tanınmayan hak yemez; 4. sorgu limit; /hesap; gün dönümü")
    asyncio.run(run())


# ------------------------------------------------ 2) Pro, dakika limiti, global kova
def test_pro_and_buckets():
    async def run():
        await _fresh()
        cfg = _cfg()
        cfg.pro_query_per_min = 2
        cli = Client()
        bot, sent, photos, cbs = _bot(cfg, cli)
        await bot._handle_update(dm(555, "/start"))
        until = await users.grant(555, 30, "test")
        assert until > dbm.now() + 29 * 86400
        await bot._handle_update(dm(555, "hype"))
        assert "⭐ Pro" in photos[-1][1] and "kaldı" not in photos[-1][1], photos[-1][1]
        await bot._handle_update(dm(555, "sol"))
        await bot._handle_update(dm(555, "hype"))
        assert len(photos) == 2 and "Dakikada en çok 2" in sent[-1][1], sent[-1][1]
        await bot._handle_update(dm(555, "/hesap"))
        assert "⭐ <b>Pro</b>" in sent[-1][1] and "bitiş" in sent[-1][1]
        # global kova 1/dk: önbelleksiz ikinci sorgu yoğunluk der, hak yenmez; önbellek muaf
        cfg.query_global_per_min = 1
        public.reset_memory()
        await bot._handle_update(dm(556, "/start"))
        await bot._handle_update(dm(556, "hype"))
        assert len(photos) == 3 and (await users.get(556))["q_used"] == 1
        await bot._handle_update(dm(557, "/start"))
        await bot._handle_update(dm(557, "sol"))
        assert "yoğunluk" in sent[-1][1] and (await users.get(557))["q_used"] == 0 and len(photos) == 3
        await bot._handle_update(dm(557, "hype"))
        assert len(photos) == 4 and (await users.get(557))["q_used"] == 1, "önbellekten dönen kova yemez"
        # HL patlarsa: hata mesajı, hak yenmez
        cfg.query_global_per_min = 40

        async def boom(*a, **k):
            raise RuntimeError("HL 500")
        cli.candles = boom
        public.reset_memory()
        from app.radar import cryptoliq
        orig = cryptoliq.snapshot

        async def bad_snapshot(*a, **k):
            raise RuntimeError("HL 500")
        cryptoliq.snapshot = bad_snapshot
        try:
            await bot._handle_update(dm(557, "sol"))
        finally:
            cryptoliq.snapshot = orig
        assert "okunamadı" in sent[-1][1] and (await users.get(557))["q_used"] == 1
        print("✅ Pro/kova) Pro altyazısı, dakika limiti, global kova (önbellek muaf, hak yenmez), HL hatası")
    asyncio.run(run())


# ------------------------------------------------ 3) yönlendirme: bayrak, grup, sahip, kanal, fail-open
def test_routing():
    async def run():
        await _fresh()
        cfg = _cfg()
        cli = Client()
        bot, sent, photos, cbs = _bot(cfg, cli)
        # bayrak KAPALI: yabancı DM'e sorgu cevabı yok, /start → chat id (eski davranış), kayıt yok
        cfg.public_bot_enabled = False
        await bot._handle_update(dm(555, "hype"))
        assert sent == [] and photos == []
        await bot._handle_update(dm(555, "/start"))
        assert len(sent) == 1 and "chat id" in sent[0][1] and await users.get(555) is None
        await bot._handle_update(cq(555, "q:HYPE"))
        assert cbs == [] and photos == [], "bayrak kapalıyken düğme de işlenmez"
        cfg.public_bot_enabled = True
        # grup sohbeti sessiz
        await bot._handle_update(dm(555, "hype", chat=-500, typ="supergroup"))
        assert len(sent) == 1 and photos == []
        # sahip sohbeti: eski zincir (/hype → coin görüntüsü 111'e), kullanıcı tablosuna girmez
        await bot._handle_update({"message": {"chat": {"id": 111, "type": "private"}, "from": {"id": 111}, "text": "/hype"}})
        assert photos and photos[-1][0] == "111" and await users.get(111) is None
        await bot._handle_update({"message": {"chat": {"id": 111, "type": "private"}, "from": {"id": 111}, "text": "merhaba"}})
        assert len(photos) == 1, "sahip sohbetinde düz metin sorgu değildir"
        # kendi kanalı: /hype çalışır, sahip komutu (/status) çalışmaz, /id chat id
        await bot._handle_update({"channel_post": {"chat": {"id": -100, "type": "channel"}, "text": "/hype"}})
        assert photos[-1][0] == "-100"
        n = len(sent)
        await bot._handle_update({"channel_post": {"chat": {"id": -100, "type": "channel"}, "text": "/status"}})
        assert len(sent) == n, "kanaldan sahip komutu çalışmaz"
        await bot._handle_update({"channel_post": {"chat": {"id": -100, "type": "channel"}, "text": "/id"}})
        assert sent[-1][0] == "-100" and "chat id" in sent[-1][1]
        # fail-open KAPALI: TELEGRAM_CHAT_ID boşken kimse sahip değil
        cfg.telegram_chat_id = ""
        cfg.public_bot_enabled = False
        n = len(sent)
        await bot._handle_update({"message": {"chat": {"id": 999, "type": "private"}, "text": "/status"}})
        await bot._handle_update({"message": {"chat": {"id": 999, "type": "private"}, "text": "/takipler"}})
        assert len(sent) == n
        print("✅ yönlendirme) bayrak kapalı → eski davranış; grup sessiz; sahip/kanal zinciri aynı; fail-open kapalı")
    asyncio.run(run())


# ------------------------------------------------ 4) düğmeler, adres, engel, sel, komut listesi, paketler
def test_callbacks_address_blocked():
    async def run():
        await _fresh()
        cfg = _cfg()
        cli = Client()
        bot, sent, photos, cbs = _bot(cfg, cli)
        await bot._handle_update(cq(555, "q:HYPE"))
        assert cbs == ["cq1"] and photos and photos[-1][0] == "555" and (await users.get(555))["q_used"] == 1
        await bot._handle_update(cq(555, "m:help", "cq2"))
        assert cbs[-1] == "cq2" and "Nasıl kullanılır" in sent[-1][1]
        await bot._handle_update(cq(555, "m:pro", "cq3"))
        assert "⭐ <b>Pro</b>" in sent[-1][1] and "$2.99" in sent[-1][1] and "Stars" in sent[-1][1]
        await bot._handle_update(cq(555, "m:notif", "cq4"))
        assert "Pro" in sent[-1][1]
        await bot._handle_update(dm(555, "/pro"))
        assert "$7.99" in sent[-1][1] and "$24.99" in sent[-1][1]
        # adres
        await bot._handle_update(dm(555, "/adres"))
        assert "Kayıtlı HL adresin yok" in sent[-1][1]
        await bot._handle_update(dm(555, "/adres 0x123"))
        assert "Geçersiz" in sent[-1][1]
        addr = "0x" + "AB" * 20
        await bot._handle_update(dm(555, f"/adres {addr}"))
        assert "kaydedildi" in sent[-1][1] and (await users.get(555))["hl_address"] == addr.lower()
        await bot._handle_update(dm(555, "/hesap"))
        assert addr.lower() in sent[-1][1]
        # engel: 403 → blocked_ts; /start ile kalkar; sahip sohbetine dokunmaz
        assert bot.blocked_reason(403, "Forbidden: bot was blocked by the user") == "blocked"
        assert bot.blocked_reason(400, '{"description":"Bad Request: chat not found"}') == "gone"
        assert bot.blocked_reason(400, "Bad Request: can't parse entities") == "" and bot.blocked_reason(429, "") == ""
        await bot._note_error("555", 403, "Forbidden: bot was blocked by the user")
        assert (await users.get(555))["blocked_ts"] and "555" in bot.blocked_chats
        await bot._handle_update(dm(555, "/start"))
        assert (await users.get(555))["blocked_ts"] is None
        await bot._note_error("111", 403, "x")
        assert "111" in bot.blocked_chats and await users.get(111) is None
        # sel: dakikada 20 mesaj, 21. sessiz
        users.reset_memory()
        n = len(sent)
        for _ in range(users.FLOOD_PER_MIN + 3):
            await bot._handle_update(dm(556, "/hesap"))
        assert len(sent) == n + users.FLOOD_PER_MIN
        # komut listesi, menü şekli, paket/Stars hesabı
        assert [c for c, _ in PUBLIC_COMMANDS] == ["start", "pro", "bildirimler", "hesap", "adres", "yardim"]
        assert public.menu()["inline_keyboard"][0][0] == {"text": "📈 Örnek: HYPE", "callback_data": "q:HYPE"}
        p = public.plans(cfg)
        assert [x["code"] for x in p] == ["1m", "3m", "12m"] and p[0]["stars"] == 230 and p[0]["days"] == 30
        assert p[1]["usd"] == 7.99 and p[2]["months"] == 12
        cfg.pro_price_usd_3m = 0
        assert [x["code"] for x in public.plans(cfg)] == ["1m", "12m"], "0 = satılmaz"
        # bağlantı: ayarlar grubu, varsayılan kapalı, env-only ödeme alanları
        from app.config import EDITABLE_FIELDS
        c = Config()
        for f in ("public_bot_enabled", "free_daily_queries", "pro_query_per_min", "query_global_per_min",
                  "query_cache_sec", "pro_price_usd_1m", "pro_price_usd_3m", "pro_price_usd_12m", "stars_per_usd",
                  "public_kinds", "pro_default_kinds", "support_contact"):
            assert f in EDITABLE_FIELDS and hasattr(c, f) and EDITABLE_FIELDS[f]["group"] == "Satış / Kullanıcılar", f
            assert all(EDITABLE_FIELDS[f].get(x) for x in ("type", "label", "group", "desc")), f
        assert c.public_bot_enabled is False and c.free_daily_queries == 3 and c.pro_price_usd_1m == 2.99
        assert "pay_hl_address" not in EDITABLE_FIELDS and hasattr(c, "pay_hl_address") and hasattr(c, "nowpayments_api_key")
        assert "cryptoliq" in c.public_kinds and "cryptoliq" in c.pro_default_kinds
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = open(os.path.join(root, ".env.example"), encoding="utf-8").read()
        assert "PUBLIC_BOT_ENABLED=0" in env and "PAY_HL_ADDRESS=" in env
        print("✅ düğme/adres/engel) callback'ler; /adres doğrulama; 403 → engel ve /start ile kalkma; sel; komut listesi; paketler; ayarlar")
    asyncio.run(run())
