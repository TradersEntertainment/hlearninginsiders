"""📣 Fan-out (satılabilir bot) — notify yayını, fanout.deliver, /bildirimler, ücretsiz özet.

Pinlenenler:
  • Notifier.send/send_rich her olayı kuyruğa yayınlar: sahibin toggle'ından bağımsız,
    ürünün kapısı public_bot_enabled + public_kinds + PUBLIC_KINDS; send_rich tek yayın
    (resimle); anahtarsız olay metin özetiyle; kuyruk tavanında en eski düşer
  • deliver: Pro + tür + coin filtresi (sembol normalizasyonu) + engelsiz + sessiz saat
    dışı; FOOT eklenir; resim sığarsa foto, reddedilirse metin; (tür, anahtar) 12 s
    tekil; fanout_log; drain
  • /bildirimler: yalnız Pro; varsayılan türler; k:<tür>/k:*on/k:*off düğmeleri ve
    yerinde klavye güncelleme; /coinler (normalize, hepsi, geçersiz); /sessiz (aralık,
    kapat, hata); tahsilatta varsayılan türler
  • ücretsiz sabah özeti: dünkü sent:% kayıtlarından satılan türlerin tekil başlıkları,
    yalnız ücretsiz ve engelsiz kullanıcılara; olay yoksa atlar
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-fanout.db")
from app import db as dbm
from app import users
from app.config import Config
from app.notify import PUBLIC_KINDS, Notifier
from app.telegram import fanout, public
from app.telegram.bot import TelegramBot


def _cfg():
    cfg = Config()
    cfg.telegram_chat_id = "111"
    cfg.public_bot_enabled = True
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    return cfg


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "fo.db"))
    users.reset_memory()
    public.reset_memory()
    fanout.reset()


class Bot:
    def __init__(self, block_chats=(), photo_ok=True):
        self.sent, self.photos, self.blocked_chats = [], [], set()
        self.block, self.photo_ok = set(block_chats), photo_ok

    async def send(self, text, chat_id=None, reply_markup=None):
        if chat_id in self.block:
            self.blocked_chats.add(chat_id)
            return False
        self.sent.append((chat_id, text))
        return True

    async def send_photo(self, png, caption="", chat_id=None, reply_markup=None):
        if not self.photo_ok:
            return False
        if chat_id in self.block:
            self.blocked_chats.add(chat_id)
            return False
        self.photos.append((chat_id, caption))
        return True


def dm(uid, text):
    return {"message": {"chat": {"id": uid, "type": "private"}, "from": {"id": uid, "first_name": "Ali"}, "text": text}}


def cq(uid, data, cid="c1"):
    return {"callback_query": {"id": cid, "from": {"id": uid, "first_name": "Ali"},
                               "message": {"chat": {"id": uid, "type": "private"}, "message_id": 7}, "data": data}}


def _btn(markup):
    return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]


# ------------------------------------------------ 1) yayın kapısı
def test_publish_gate():
    async def run():
        await _fresh()
        cfg = _cfg()
        bot = Bot()
        n = Notifier(cfg, bot)
        assert await n.send("cryptoliq", "💥 <b>PUMP</b> liq", key="k1", coin="PUMP") is True
        assert len(fanout._queue) == 1 and fanout._queue[0]["coin"] == "PUMP" and fanout._queue[0]["png"] is None
        assert fanout._queue[0]["key"] == "k1" and bot.sent[-1][0] is None
        await n.send("sim", "x", key="s1")
        await n.send("health", "x")
        await n.send("track", "x")
        assert len(fanout._queue) == 1, "sahibe özel türler yayınlanmaz"
        cfg.notify_cryptoliq = False
        assert await n.send("cryptoliq", "y", key="k2", coin="PUMP") is False and len(fanout._queue) == 2, \
            "sahip toggle'ı kapalıyken sahibe gitmez ama ürün yayını olur"
        cfg.notify_cryptoliq = True
        cfg.public_kinds = "liqmap"
        await n.send("cryptoliq", "z", key="k3")
        assert len(fanout._queue) == 2, "public_kinds dışı yayınlanmaz"
        cfg.public_kinds = Config().public_kinds
        cfg.public_bot_enabled = False
        await n.send("cryptoliq", "z", key="k4")
        assert len(fanout._queue) == 2, "bayrak kapalı → yayın yok"
        cfg.public_bot_enabled = True
        ok, mode = await n.send_rich("cryptovol", "🚀 rekor", b"\x89PNG" + b"0" * 100, key="v1", coin="HYPE",
                                     short_caption="c")
        assert ok and mode == "combined" and len(fanout._queue) == 3
        assert fanout._queue[-1]["png"] and fanout._queue[-1]["kind"] == "cryptovol", "send_rich tek yayın, resimle"
        await n.send("liqmap", "🧲 duvar", coin="CL")
        assert len(fanout._queue) == 4 and fanout._queue[-1]["key"], "anahtarsız olay metin özetiyle"
        old = fanout.QUEUE_MAX
        fanout.QUEUE_MAX = 5
        try:
            for i in range(4):
                fanout.publish(cfg, "wall", "W", f"t{i}", None, f"w{i}")
            assert len(fanout._queue) == 5 and fanout._stats["dropped"] == 3 and fanout._queue[0]["key"] != "k1"
        finally:
            fanout.QUEUE_MAX = old
        assert not fanout.publish(cfg, "cryptoliq", "X", "", None, "empty"), "boş metin yayınlanmaz"
        print("✅ yayın) sahip toggle'ından bağımsız; tür/bayrak kapısı; send_rich tek yayın; tavan")
    asyncio.run(run())


# ------------------------------------------------ 2) teslimat matrisi
def test_deliver_matrix():
    async def run():
        await _fresh()
        cfg = _cfg()
        bot = Bot(block_chats={"6"})
        now = dbm.now()
        for uid in range(1, 9):
            await users.upsert_from_update({"id": uid, "first_name": f"u{uid}"}, str(uid))
        for uid in (1, 2, 3, 4, 6, 7, 8):
            await users.grant(uid, 30)
        await users.set_kinds(1, "cryptoliq")
        await users.set_kinds(2, "cryptoliq")
        await users.set_coins(2, "HYPE")
        await users.set_kinds(3, "cryptoliq")
        await users.set_coins(3, "BTC")
        await users.set_kinds(5, "cryptoliq")                          # ücretsiz
        await users.set_kinds(6, "cryptoliq")                          # engellenecek
        await users.set_kinds(7, "cryptoliq")
        h = datetime.now(users.TR).hour
        await users.set_quiet(7, h, (h + 1) % 24)                       # şu an sessiz
        await users.set_kinds(8, "equityvol")
        await users.set_coins(8, "xyz:TSLA")
        ev = {"kind": "cryptoliq", "coin": "HYPE", "text": "💥 <b>HYPE</b> liq yakın", "png": None, "key": "e1", "ts": now}
        out = await fanout.deliver(bot, cfg, ev)
        assert out["targets"] == 3 and out["sent"] == 2 and out["blocked"] == 1 and out["quiet"] == 1 and out["fail"] == 0, out
        assert sorted(c for c, _ in bot.sent) == ["1", "2"] and all(fanout.FOOT in t for _, t in bot.sent)
        async with dbm.db() as c:
            cur = await c.execute("SELECT * FROM fanout_log")
            rows = [dict(r) for r in await cur.fetchall()]
        assert len(rows) == 1 and rows[0]["n_sent"] == 2 and rows[0]["n_blocked"] == 1 and rows[0]["coin"] == "HYPE"
        out2 = await fanout.deliver(bot, cfg, ev)
        assert out2["dup"] == 1 and len(bot.sent) == 2, "aynı (tür, anahtar) 12 saat tekil"
        out3 = await fanout.deliver(bot, cfg, {**ev, "coin": "BTC", "key": "e2"})
        assert out3["sent"] == 2 and sorted(c for c, _ in bot.sent[2:]) == ["1", "3"], "coin filtresi"
        out4 = await fanout.deliver(bot, cfg, {"kind": "equityvol", "coin": "xyz:TSLA", "text": "📈 TSLA", "png": None,
                                               "key": "e3", "ts": now})
        assert out4["sent"] == 1 and bot.sent[-1][0] == "8", "xyz:TSLA → TSLA sembol eşleşmesi"
        out5 = await fanout.deliver(bot, cfg, {"kind": "cryptoliq", "coin": "", "text": "💥 genel", "png": None,
                                               "key": "e4", "ts": now})
        assert out5["sent"] == 1 and bot.sent[-1][0] == "1", "coin'siz olay yalnız filtresiz kullanıcıya"
        await users.set_kinds(1, "cryptoliq,cryptovol")
        ev5 = {"kind": "cryptovol", "coin": "HYPE", "text": "🚀 rekor", "png": b"png", "key": "e5", "ts": now}
        out6 = await fanout.deliver(bot, cfg, ev5)
        assert out6["sent"] == 1 and bot.photos[-1][0] == "1" and fanout.FOOT in bot.photos[-1][1], "resim + altyazı"
        bot.photo_ok = False
        out7 = await fanout.deliver(bot, cfg, {**ev5, "key": "e6"})
        assert out7["sent"] == 1 and bot.sent[-1][0] == "1" and "🚀 rekor" in bot.sent[-1][1], "resim reddedilirse metin"
        fanout.publish(cfg, "cryptoliq", "HYPE", "💥 kuyruk", None, "e7")
        assert await fanout.drain(bot, cfg) == 1 and bot.sent[-1][1].startswith("💥 kuyruk")
        fanout.publish(cfg, "cryptoliq", "HYPE", "💥 kuyruk2", None, "e8")
        assert await fanout.drain(None, cfg) == 0 and not fanout._queue, "bot yoksa düşer"
        st = fanout.stats()
        assert st["events"] == 7 and st["queue"] == 0 and st["dup"] == 1 and st["last"].startswith("cryptoliq HYPE")
        print("✅ teslimat) Pro/tür/coin/engel/sessiz matrisi; tekil; resim yedeği; fanout_log; drain")
    asyncio.run(run())


# ------------------------------------------------ 3) /bildirimler menüsü, düğmeler, /coinler, /sessiz
def test_menu_toggles_coins_quiet():
    async def run():
        await _fresh()
        cfg = _cfg()
        tb = TelegramBot(cfg, None, None, {})
        sent, acks, edits = [], [], []

        async def fake_send(text, chat_id=None, reply_markup=None):
            sent.append((chat_id, text, reply_markup))
            return True

        async def fake_ack(cq_id, text="", alert=False):
            acks.append((cq_id, text))
            return True

        async def fake_edit(chat, message_id, reply_markup):
            edits.append((chat, message_id, reply_markup))
            return True
        tb.send, tb.answer_callback, tb.edit_reply_markup = fake_send, fake_ack, fake_edit
        await tb._handle_update(dm(5, "/start"))
        await tb._handle_update(dm(5, "/bildirimler"))
        assert "Pro'da" in sent[-1][1] and sent[-1][2], "ücretsiz: kilitli + menü"
        await users.grant(5, 30)
        await tb._handle_update(dm(5, "/bildirimler"))
        t, mk = sent[-1][1], sent[-1][2]
        datas = _btn(mk)
        assert "🔔 <b>Bildirimler</b>" in t and "k:cryptoliq" in datas and "k:*on" in datas and "k:*off" in datas
        assert "k:sim" not in datas and "k:track" not in datas
        defaults = users.csv_set(cfg.pro_default_kinds)
        assert await users.get_kinds(5) == defaults and f"<b>{len(defaults)}</b> tür açık" in t, "ilk açılışta varsayılanlar"
        labels = [b["text"] for row in mk["inline_keyboard"] for b in row]
        assert any(x.startswith("✅ ") for x in labels) and any(x.startswith("☐ ") for x in labels)
        await tb._handle_update(cq(5, "k:cryptoliq", "c1"))
        assert "cryptoliq" not in await users.get_kinds(5) and acks[-1] == ("c1", "kapalı")
        assert edits[-1][0] == "5" and edits[-1][1] == 7 and "k:cryptoliq" in _btn(edits[-1][2])
        await tb._handle_update(cq(5, "k:cryptoliq", "c2"))
        assert "cryptoliq" in await users.get_kinds(5) and acks[-1][1] == "açık ✅"
        await tb._handle_update(cq(5, "k:*off", "c3"))
        assert await users.get_kinds(5) == set()
        await tb._handle_update(cq(5, "k:*on", "c4"))
        assert await users.get_kinds(5) == set(public.allowed_kinds(cfg)) and len(public.allowed_kinds(cfg)) == len(PUBLIC_KINDS)
        await tb._handle_update(cq(5, "k:sim", "c5"))
        assert acks[-1][1] == "bu tür satışta değil"
        cfg.public_kinds = "cryptoliq,liqmap"
        assert public.allowed_kinds(cfg) == ["cryptoliq", "liqmap"]
        cfg.public_kinds = Config().public_kinds
        # coinler
        await tb._handle_update(dm(5, "/coinler hype, btc xyz:TSLA"))
        assert await users.get_coins(5) == {"HYPE", "BTC", "TSLA"} and "HYPE" in sent[-1][1]
        await tb._handle_update(dm(5, "/coinler"))
        assert "BTC, HYPE, TSLA" in sent[-1][1]
        await tb._handle_update(dm(5, "/bildirimler"))
        assert "BTC, HYPE, TSLA" in sent[-1][1]
        await tb._handle_update(dm(5, "/coinler hepsi"))
        assert await users.get_coins(5) == set() and "tüm coinler" in sent[-1][1]
        await tb._handle_update(dm(5, "/coinler ???"))
        assert "Geçerli sembol yok" in sent[-1][1]
        # sessiz
        await tb._handle_update(dm(5, "/sessiz 23-08"))
        u = await users.get(5)
        assert (u["quiet_start"], u["quiet_end"]) == (23, 8) and "23–08" in sent[-1][1]
        await tb._handle_update(dm(5, "/bildirimler"))
        assert "23–08" in sent[-1][1]
        await tb._handle_update(dm(5, "/sessiz 25-3"))
        assert "❌" in sent[-1][1]
        await tb._handle_update(dm(5, "/sessiz"))
        assert "/sessiz 23-08" in sent[-1][1]
        await tb._handle_update(dm(5, "/sessiz kapat"))
        assert (await users.get(5))["quiet_start"] is None and "kapatıldı" in sent[-1][1]
        # ücretsiz kullanıcı düğmeye basarsa
        await tb._handle_update(dm(6, "/start"))
        await tb._handle_update(cq(6, "k:cryptoliq", "c6"))
        assert acks[-1] == ("c6", "Pro gerekir") and "Pro'da" in sent[-1][1] and await users.get_kinds(6) == set()
        # tahsilatta varsayılan türler
        from app.pay import core
        await tb._handle_update(dm(7, "/start"))
        p = await core.create_pending(7, "hl", "1m", cfg, from_addr="0x" + "a" * 40)
        assert await core.credit(p["id"], "hh1", cfg=cfg) and await users.get_kinds(7) == defaults
        print("✅ menü) Pro kilidi; varsayılan türler; k: düğmeleri + yerinde klavye; /coinler; /sessiz; tahsilat")
    asyncio.run(run())


# ------------------------------------------------ 4) ücretsiz sabah özeti + bağlantı
def test_public_digest_and_wiring():
    async def run():
        await _fresh()
        cfg = _cfg()
        bot = Bot()
        now = dbm.now()
        rows = [("sent:cryptoliq", "💥 <b>PUMP</b> liq yakın\nsatır2", 3600),
                ("sent:liqmap", "🧲 <b>LİKİDASYON DUVARI — CL</b>\n…", 7200),
                ("sent:sim", "🧪 SİM AÇILDI", 100),
                ("sent:cryptoliq", "💥 <b>PUMP</b> liq yakın\nfarklı gövde", 50),
                ("sent:twap", "⏳ TWAP · INJ", 30 * 3600),
                ("quiet:wall", "🧱 x", 10)]
        async with dbm.db() as c:
            for i, (kind, payload, dt) in enumerate(rows):
                await c.execute("INSERT INTO alerts_log(kind,key,ts,payload) VALUES(?,?,?,?)", (kind, f"k{i}", now - dt, payload))
        items = await fanout.digest_items()
        assert [k for k, _, _ in items] == ["cryptoliq", "liqmap"] and items[0][1] == "💥 PUMP liq yakın", items
        text = fanout.digest_text(items)
        assert "Dünün öne çıkanları" in text and "2 olay" in text and "PUMP liq yakın" in text and "/pro" in text
        assert "<b>PUMP</b>" not in text
        for uid in (1, 2, 3):
            await users.upsert_from_update({"id": uid, "first_name": "x"}, str(uid))
        await users.grant(2, 30)
        await users.mark_blocked("3")
        res = await fanout.send_public_digest(bot, cfg)
        assert res == {"targets": 1, "sent": 1, "items": 2} and bot.sent[0][0] == "1", "yalnız ücretsiz + engelsiz"
        await _fresh()
        assert (await fanout.send_public_digest(bot, cfg))["skipped"]
        # bağlantı
        from app.config import EDITABLE_FIELDS
        from app.health import limits, periods
        c = Config()
        assert "fanout" in limits(c) and "public_digest" in periods(c) and c.public_digest_hour == 9 and c.public_digest_enabled
        assert EDITABLE_FIELDS["public_digest_hour"]["group"] == "Satış / Kullanıcılar"
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        rd = lambda *p: open(os.path.join(root, *p), encoding="utf-8").read()  # noqa: E731
        assert '_spawn("fanout"' in rd("app", "main.py") and '_spawn("public_digest"' in rd("app", "main.py")
        from app.telegram import format as fmt
        assert fmt.TASK_TR.get("fanout") and fmt.TASK_TR.get("public_digest")
        for f in ("anomaly", "autoscan", "bookwall", "cryptoliq", "cryptovol", "equityvol", "liqattack", "liqwatch",
                  "lowvol", "offhours", "patterns", "report", "twaplive"):
            assert "coin=" in rd("app", "radar", f + ".py"), f
        assert "coin=coin" in rd("app", "hl", "collector.py")
        assert set(users.csv_set(c.pro_default_kinds)) <= set(PUBLIC_KINDS) and set(users.csv_set(c.public_kinds)) <= set(PUBLIC_KINDS)
        print("✅ özet/bağlantı) tekil başlıklar, satılan türler, yalnız ücretsiz; döngüler, ayarlar, coin= kancaları")
    asyncio.run(run())
