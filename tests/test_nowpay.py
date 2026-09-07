"""🪙 NOWPayments + satış sayfaları (S4) — nowpay.py, /pay/ipn, /kullanicilar, /bot, tanı satırı.

Pinlenenler:
  • imza: sıralı+kompakt JSON'un HMAC-SHA512'si; yanlış imza/gizli anahtar yok → kredi yok, sayaç
  • IPN: waiting → payment_id saklanır; finished → tahsilat + DM (aynı payment_id ikinci kez yok);
    failed → bekleyen kapanır; bilinmeyen order → 200 'kayıt yok'
  • fatura: POST /v1/invoice (x-api-key, order_id=pay:<id>, ipn_callback_url), invoice_url düğmesi;
    başarısız → kayıt bayat; anahtar yoksa menüde yok
  • yoklama yedeği: payment_id bilinen 10 dk'dan eski bekleyen → GET /v1/payment/<id> → tahsilat
  • /pay/ipn route'u; /kullanicilar ve /bot şablonları; /tani 'satış' satırı
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-nowpay.db")
from app import db as dbm
from app import users
from app.config import Config
from app.pay import core, nowpay
from app.telegram import public
from app.telegram.bot import TelegramBot

SECRET = "ipn-secret-123"


class Resp:
    def __init__(self, status, data):
        self.status, self._d = status, data

    async def json(self, content_type=None):
        return self._d

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class Session:
    def __init__(self, post=(201, None), get=(200, None)):
        self.post_resp, self.get_resp, self.calls = post, get, []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("post", url, json, headers))
        return Resp(*self.post_resp)

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("get", url, None, headers))
        return Resp(*self.get_resp)


def _cfg():
    cfg = Config()
    cfg.telegram_chat_id = "111"
    cfg.public_bot_enabled = True
    cfg.nowpayments_api_key = "apikey"
    cfg.nowpayments_ipn_secret = SECRET
    cfg.public_base_url = "https://radar.example.app"
    cfg.pay_hl_address = "0x" + "9" * 40
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    return cfg


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "np.db"))
    users.reset_memory()
    public.reset_memory()


def _bot(cfg):
    bot = TelegramBot(cfg, None, None, {})
    sent = []

    async def fake_send(text, chat_id=None, reply_markup=None):
        sent.append((chat_id, text, reply_markup))
        return True

    async def fake_cb(cq_id, text="", alert=False):
        return True
    bot.send, bot.answer_callback = fake_send, fake_cb
    return bot, sent


def ipn(pid, status, payment_id="np_1", **extra):
    d = {"payment_id": payment_id, "payment_status": status, "order_id": f"pay:{pid}", "price_amount": 2.99,
         "price_currency": "usd", "pay_currency": "usdttrc20", "actually_paid": 2.99, "outcome_amount": 2.9}
    d.update(extra)
    body = json.dumps(d).encode()
    return body, nowpay.signature(SECRET, d)


def dm(uid, text):
    return {"message": {"chat": {"id": uid, "type": "private"}, "from": {"id": uid, "first_name": "Ali"}, "text": text}}


def cq(uid, data, cid="c1"):
    return {"callback_query": {"id": cid, "from": {"id": uid, "first_name": "Ali"},
                               "message": {"chat": {"id": uid, "type": "private"}, "message_id": 7}, "data": data}}


# ------------------------------------------------ 1) imza + IPN durumları
def test_signature_and_ipn():
    async def run():
        await _fresh()
        cfg = _cfg()
        bot, sent = _bot(cfg)
        obj = {"b": 1, "a": {"y": [3, {"z": 1, "x": 2}], "x": "ç"}}
        assert nowpay.canonical(obj) == '{"a":{"x":"ç","y":[3,{"x":2,"z":1}]},"b":1}'
        sig = nowpay.signature(SECRET, obj)
        assert nowpay.verify(SECRET, json.dumps(obj).encode(), sig.upper()) == (True, obj)
        assert nowpay.verify(SECRET, json.dumps(obj).encode(), "0" * 128)[0] is False
        assert nowpay.verify(SECRET, b"not json", sig)[0] is False and nowpay.verify("", b"{}", sig)[0] is False
        assert nowpay.order_pid("pay:12") == 12 and nowpay.order_pid("x") is None and nowpay.order_pid("pay:z") is None
        await users.upsert_from_update({"id": 5, "first_name": "Ali", "username": "ali"}, "5")
        pay = await core.create_pending(5, "nowpay", "1m", cfg)
        pid = pay["id"]
        # gizli anahtar yok → 503; kötü imza → 403 + sayaç, kredi yok
        cfg.nowpayments_ipn_secret = ""
        assert (await nowpay.handle_ipn(cfg, bot, *ipn(pid, "finished")))[0] == 503
        cfg.nowpayments_ipn_secret = SECRET
        body, _ = ipn(pid, "finished")
        code, note = await nowpay.handle_ipn(cfg, bot, body, "deadbeef")
        assert code == 403 and (await core.get(pid))["status"] == "pending" and not users.is_pro(await users.get(5))
        st = await dbm.kv_get(nowpay.KV_STATE)
        assert st["bad_sig"] == 1 and "finished" in st["bad_sample"]
        # waiting → payment_id saklanır
        code, note = await nowpay.handle_ipn(cfg, bot, *ipn(pid, "waiting", payment_id="np_9"))
        assert code == 200 and note == "waiting" and json.loads((await core.get(pid))["raw"])["payment_id"] == "np_9"
        # bilinmeyen kayıt → 200
        assert (await nowpay.handle_ipn(cfg, bot, *ipn(999, "finished")))[1] == "kayıt yok"
        # finished → tahsilat + DM + sahibe not; aynı payment_id tekrar → 'zaten işlenmiş'
        code, note = await nowpay.handle_ipn(cfg, bot, *ipn(pid, "finished", payment_id="np_9"))
        assert code == 200 and note == "tahsilat"
        u = await users.get(5)
        assert users.is_pro(u) and abs(u["pro_until"] - (dbm.now() + 30 * 86400)) <= 3
        p = await core.get(pid)
        assert p["status"] == "paid" and p["ext_id"] == "np_9" and p["amount_raw"] == 2.99
        assert any(c == "5" and "Pro açıldı" in t for c, t, _ in sent) and any(c == "111" and "💰" in t for c, t, _ in sent)
        code, note = await nowpay.handle_ipn(cfg, bot, *ipn(pid, "finished", payment_id="np_9"))
        assert note == "zaten işlenmiş" and (await users.get(5))["pro_until"] == u["pro_until"]
        st = await dbm.kv_get(nowpay.KV_STATE)
        assert st["ipn"] == 4 and st["paid"] == 1 and st["last_status"] == "finished" and st["sample"]
        # failed → bekleyen kapanır
        pay2 = await core.create_pending(5, "nowpay", "3m", cfg)
        assert (await nowpay.handle_ipn(cfg, bot, *ipn(pay2["id"], "failed", payment_id="np_10")))[1] == "kapandı"
        assert (await core.get(pay2["id"]))["status"] == "expired"
        print("✅ IPN) kanonik imza; kötü imza/anahtar yok → kredi yok; waiting/finished/failed; tekil payment_id")
    asyncio.run(run())


# ------------------------------------------------ 2) fatura + /pro akışı + yoklama yedeği
def test_invoice_flow_and_poll():
    async def run():
        await _fresh()
        cfg = _cfg()
        bot, sent = _bot(cfg)
        bot.session = Session(post=(201, {"id": "inv_1", "invoice_url": "https://nowpayments.io/payment/?iid=inv_1"}))
        await bot._handle_update(dm(7, "/start"))
        await bot._handle_update(dm(7, "/pro"))
        datas = [b["callback_data"] for row in sent[-1][2]["inline_keyboard"] for b in row]
        assert "pay:np:1m" in datas and "🪙 kripto" in sent[-1][1]
        await bot._handle_update(cq(7, "pay:np:1m"))
        kind, url, payload, headers = bot.session.calls[-1]
        assert kind == "post" and url.endswith("/invoice") and headers == {"x-api-key": "apikey"}
        assert payload["price_amount"] == 2.99 and payload["price_currency"] == "usd" and payload["order_id"].startswith("pay:")
        assert payload["ipn_callback_url"] == "https://radar.example.app/pay/ipn"
        pend = await core.pending("nowpay")
        assert len(pend) == 1 and json.loads(pend[0]["raw"])["invoice_id"] == "inv_1"
        btn = sent[-1][2]["inline_keyboard"][0][0]
        assert btn["url"].startswith("https://nowpayments.io/") and "Kripto ile ödeme" in sent[-1][1] and f"#{pend[0]['id']}" in sent[-1][1]
        # yeni fatura eskisini bayatlatır; fatura başarısız → kayıt yok + hata
        bot.session = Session(post=(400, {"message": "bad currency"}))
        await bot._handle_update(cq(7, "pay:np:3m", "c2"))
        assert "oluşturulamadı" in sent[-1][1] and await core.pending("nowpay") == []
        assert (await core.get(pend[0]["id"]))["status"] == "expired"
        # anahtar yoksa düğme yok, düğme verisi reddedilir
        cfg.nowpayments_api_key = ""
        await bot._handle_update(dm(7, "/pro"))
        assert not any(b["callback_data"].startswith("pay:np") for row in sent[-1][2]["inline_keyboard"] for b in row)
        await bot._handle_update(cq(7, "pay:np:1m", "c3"))
        assert "açık değil" in sent[-1][1]
        cfg.nowpayments_api_key = "apikey"
        # yoklama yedeği: payment_id bilinen, 10 dk'dan eski bekleyen → GET → finished → Pro
        pay = await core.create_pending(7, "nowpay", "1m", cfg)
        await nowpay._set_raw(pay["id"], {"payment_id": "np_55"})
        ses = Session(get=(200, {"payment_id": "np_55", "payment_status": "finished", "actually_paid": 2.99}))
        assert (await nowpay.poll_pending(cfg, ses, bot))["checked"] == 0, "10 dk dolmadan yoklanmaz"
        async with dbm.db() as c:
            await c.execute("UPDATE payments SET created_ts=? WHERE id=?", (dbm.now() - 700, pay["id"]))
        out = await nowpay.poll_pending(cfg, ses, bot)
        assert out == {"checked": 1, "paid": 1} and ses.calls[-1][1].endswith("/payment/np_55")
        assert users.is_pro(await users.get(7)) and (await core.get(pay["id"]))["ext_id"] == "np_55"
        pay3 = await core.create_pending(7, "nowpay", "1m", cfg)
        await nowpay._set_raw(pay3["id"], {"payment_id": "np_56"})
        async with dbm.db() as c:
            await c.execute("UPDATE payments SET created_ts=? WHERE id=?", (dbm.now() - 700, pay3["id"]))
        out = await nowpay.poll_pending(cfg, Session(get=(200, {"payment_status": "expired"})), bot)
        assert out["checked"] == 1 and (await core.get(pay3["id"]))["status"] == "expired"
        assert (await nowpay.poll_pending(cfg, None, bot))["checked"] == 0
        print("✅ fatura) /pro düğmesi → POST /invoice + url düğmesi; başarısız → bayat; anahtar yok; yoklama yedeği")
    asyncio.run(run())


# ------------------------------------------------ 3) route + sayfalar + tanı
def test_route_pages_diag():
    async def run():
        await _fresh()
        cfg = _cfg()
        bot, sent = _bot(cfg)
        await users.upsert_from_update({"id": 5, "first_name": "Ali", "username": "ali"}, "5")
        pay = await core.create_pending(5, "nowpay", "1m", cfg)
        from app.web import routes

        class St:
            pass
        st = St()
        st.cfg, st.bot = cfg, bot

        class App:
            state = st

        class Req:
            app = App()

            def __init__(self, body, sig):
                self._b, self.headers = body, {"x-nowpayments-sig": sig}

            async def body(self):
                return self._b
        body, sig = ipn(pay["id"], "finished", payment_id="np_r1")
        resp = await routes.pay_ipn(Req(body, "bad"))
        assert resp.status_code == 403 and json.loads(resp.body)["ok"] is False
        resp = await routes.pay_ipn(Req(body, sig))
        assert resp.status_code == 200 and json.loads(resp.body) == {"ok": True, "note": "tahsilat"}
        assert users.is_pro(await users.get(5))
        # sayfalar
        from app.web.routes import templates
        now = dbm.now()
        rows = [await users.get(5)]
        pays = await core.recent(10)
        req = type("R", (), {"url": type("U", (), {"path": "/kullanicilar"})()})()
        html = templates.env.get_template("kullanicilar.html").render(
            request=req, k="", is_admin=True, has_pw=True, cfg=cfg, ts=now, st=await users.stats(now),
            ss=await core.sales_stats(cfg, now), rows=rows, kinds={5: 3}, coins={5: 2}, pays=pays, fan=[],
            pw={"ok": 3, "err": 0, "matched_total": 1, "unmatched": [{"from": "0x" + "d" * 40, "usd": 2.5}], "ts": now},
            fo={"events": 2, "sent": 2, "ts": now}, npst={"ipn": 2, "paid": 1, "bad_sig": 0, "last_status": "finished"},
            stars=230, username="hl_radar_bot")
        assert "🛒 Satılabilir bot" in html and "AÇIK" in html and "@ali" in html and "⭐ Pro" in html and "np_r1" in html
        assert "0xdddddddd…" in html and "https://t.me/hl_radar_bot" in html and "MRR" in html and "230 Stars" in html
        pub = templates.env.get_template("bot_public.html").render(
            request=req, plans=public.plans(cfg), free=3, username="hl_radar_bot", enabled=True, nowpay=True, support="@destek")
        assert "https://t.me/hl_radar_bot" in pub and "$2.99" in pub and "günde 3 sorgu" in pub and "NOWPayments" in pub
        assert "yatırım tavsiyesi değildir" in pub and "@destek" in pub
        pub2 = templates.env.get_template("bot_public.html").render(request=req, plans=public.plans(cfg), free=3,
                                                                    username="", enabled=False, nowpay=False, support="")
        assert "yakında" in pub2 and "t.me" not in pub2
        # tanı satırı
        from app import diag
        txt = await diag.report(cfg, None)
        line = next((ln for ln in txt.splitlines() if "satış:" in ln), "")
        npl = next((ln for ln in txt.splitlines() if "NOWPayments:" in ln), "")
        assert "satış: bot AÇIK" in line and "Pro 1" in line, line
        assert "NOWPayments: 1 IPN · 1 tahsilat · 1 kötü imza" in npl and "kötü imza örneği" in npl, npl
        # bağlantı
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        rd = lambda *p: open(os.path.join(root, *p), encoding="utf-8").read()  # noqa: E731
        assert "/kullanicilar" in rd("app", "web", "templates", "base.html") and "PUBLIC_BASE_URL" in rd(".env.example")
        assert "nowpay.poll_pending" in rd("app", "pay", "hl.py") and hasattr(Config(), "public_base_url")
        print("✅ sayfa/route) /pay/ipn imza→tahsilat; /kullanicilar ve /bot şablonları; tanı 'satış' satırı")
    asyncio.run(run())
