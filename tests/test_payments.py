"""💳 Ödeme katmanı (satılabilir bot) — pay/core, pay/hl, pay/stars, pay/billing, admin.

Pinlenenler:
  • ledger ayrıştırma: yalnız bize GELEN USDC (internalTransfer / spotTransfer USDC);
    giden, başka token, köprü yatırımı, başkasına giden, bozuk kayıt atlanır
  • eşleşme: gönderen adres + fiyatı tutara sığan EN ESKİ bekleyen; aynı hash ikinci kez
    kredi vermez; kısa tutar eşleşmez; uzatma mevcut bitişin üstüne
  • poll: ledger sorgusu, ilk ham örnek, adres yoksa atlar, hata sayacı
  • Stars: sendInvoice XTR + payload, pre_checkout ret/onay, successful_payment → Pro,
    çift charge yok, fatura başarısız → kayıt bayat, /iade → refunded + Pro kapanır
  • /pro klavyesi yöntemlere göre; USDC akışı adres ister, talimat yazar, yeni paket
    eskisini bayatlatır; /hesap bekleyeni gösterir
  • billing: 3 gün kala ve bitince tek DM; bayat bekleyen temizliği; satış kv'si
  • sahip: /kullanicilar /pro_ver /odemeler /duyuru (engelli hariç) /iade
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-pay.db")
from app import db as dbm
from app import users
from app.config import Config
from app.pay import billing, core, hl
from app.telegram import public
from app.telegram.bot import TelegramBot

PAY = "0x" + "9" * 40
A, B, C = ("0x" + c * 40 for c in "abc")


class Client:
    def __init__(self, ledger=None):
        self.ledger, self.ledger_calls = ledger or [], []

    async def ledger_updates(self, addr, start_ms):
        self.ledger_calls.append((addr, start_ms))
        return self.ledger


def _cfg():
    cfg = Config()
    cfg.telegram_chat_id = "111"
    cfg.crypto_chat_id = "-100"
    cfg.public_bot_enabled = True
    cfg.pay_hl_address = PAY
    cfg.nowpayments_api_key = ""
    cfg.stars_per_usd = 77
    cfg.pro_price_usd_1m, cfg.pro_price_usd_3m, cfg.pro_price_usd_12m = 2.99, 7.99, 24.99
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    return cfg


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "pay.db"))
    users.reset_memory()
    public.reset_memory()


def _bot(cfg, cli=None):
    bot = TelegramBot(cfg, None, cli or Client(), {})
    sent, photos, calls = [], [], []
    bot.call_fail = set()

    async def fake_send(text, chat_id=None, reply_markup=None):
        sent.append((chat_id, text, reply_markup))
        return True

    async def fake_photo(png, caption="", chat_id=None, reply_markup=None):
        photos.append((chat_id, caption))
        return True

    async def fake_cb(cq_id, text="", alert=False):
        return True

    async def fake_call(method, payload, timeout=30):
        calls.append((method, payload))
        if method in bot.call_fail:
            return 400, {"ok": False, "description": "boom"}
        return 200, {"ok": True, "result": {}}
    bot.send, bot.send_photo, bot.answer_callback, bot.call = fake_send, fake_photo, fake_cb, fake_call
    return bot, sent, photos, calls


def dm(uid, text):
    return {"message": {"chat": {"id": uid, "type": "private"},
                        "from": {"id": uid, "first_name": "Ali", "username": f"u{uid}"}, "text": text}}


def cq(uid, data, cid="cq1"):
    return {"callback_query": {"id": cid, "from": {"id": uid, "first_name": "Ali", "username": f"u{uid}"},
                               "message": {"chat": {"id": uid, "type": "private"}, "message_id": 7}, "data": data}}


def own(text):
    return {"message": {"chat": {"id": 111, "type": "private"}, "text": text}}


def led(typ, frm, dest, usd, h, ts, **extra):
    d = {"type": typ, "user": frm, "destination": dest, "usdc": str(usd)}
    d.update(extra)
    return {"time": ts * 1000, "hash": h, "delta": d}


def _btn_data(markup):
    return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]


# ------------------------------------------------ 1) ledger ayrıştırma + eşleşme + poll
def test_parse_match_poll():
    async def run():
        await _fresh()
        cfg = _cfg()
        bot, sent, photos, calls = _bot(cfg)
        now = dbm.now()
        raw = [led("internalTransfer", A, PAY, 2.99, "h1", now - 100),
               led("internalTransfer", PAY, A, 5.0, "h_out", now - 90),                    # giden
               {"time": (now - 80) * 1000, "hash": "h2", "delta": {"type": "spotTransfer", "token": "USDC",
                                                                   "amount": "24.99", "user": B, "destination": PAY}},
               {"time": (now - 70) * 1000, "hash": "h3", "delta": {"type": "spotTransfer", "token": "PURR",
                                                                   "amount": "100", "user": B, "destination": PAY}},
               {"time": (now - 60) * 1000, "hash": "h4", "delta": {"type": "deposit", "usdc": "50"}},
               led("internalTransfer", C, "0x" + "1" * 40, 9.0, "h5", now - 50),          # başkasına
               {"delta": "bozuk"}, None,
               {"time": "x", "hash": "h6", "delta": {"type": "internalTransfer", "user": C, "destination": PAY, "usdc": "abc"}},
               led("internalTransfer", C, PAY, 2.99, "h7", now - 40)]                     # kayıtsız adres
        tr = hl.parse_transfers(raw, PAY)
        assert [t["hash"] for t in tr] == ["h1", "h2", "h7"], tr
        assert tr[0]["from"] == A and tr[0]["usd"] == 2.99 and tr[0]["ts"] == now - 100 and tr[1]["type"] == "spotTransfer"
        assert hl.parse_transfers(None, PAY) == [] and hl.parse_transfers("çöp", PAY) == []
        for uid, addr in ((1, A), (2, B)):
            await users.upsert_from_update({"id": uid, "first_name": f"u{uid}", "username": f"u{uid}"}, str(uid))
            await users.set_address(uid, addr)
        p1 = await core.create_pending(1, "hl", "1m", cfg, from_addr=A)
        p2 = await core.create_pending(2, "hl", "12m", cfg, from_addr=B)
        p3 = await core.create_pending(1, "hl", "12m", cfg, from_addr=A)
        assert p1["status"] == "pending" and p1["amount_usd"] == 2.99 and p1["currency"] == "USD" and p3["id"] > p1["id"]
        assert await core.create_pending(1, "hl", "99m", cfg) is None and await core.create_pending(1, "kart", "1m", cfg) is None
        res = await hl.match(tr, cfg, bot)
        assert res["matched"] == 2 and res["dup"] == 0 and [t["hash"] for t in res["unmatched"]] == ["h7"], res
        u1, u2 = await users.get(1), await users.get(2)
        assert abs(u1["pro_until"] - (now + 30 * 86400)) <= 3 and abs(u2["pro_until"] - (now + 365 * 86400)) <= 3
        assert (await core.get(p1["id"]))["status"] == "paid" and (await core.get(p1["id"]))["ext_id"] == "h1"
        assert (await core.get(p2["id"]))["ext_id"] == "h2" and (await core.get(p3["id"]))["status"] == "pending"
        msgs = [(c, t) for c, t, _ in sent]
        assert any(c == "1" and "Pro açıldı" in t for c, t in msgs) and any(c == "2" and "Pro açıldı" in t for c, t in msgs)
        assert sum(1 for c, t in msgs if c == "111" and "💰 Ödeme" in t) == 2
        # aynı transferler tekrar → çift kredi yok
        res2 = await hl.match(tr, cfg, bot)
        assert res2["matched"] == 0 and res2["dup"] == 2 and (await users.get(1))["pro_until"] == u1["pro_until"]
        # kısa tutar eşleşmez; tam tutar 12 aylık bekleyeni açar, bitişin üstüne ekler
        res3 = await hl.match(hl.parse_transfers([led("internalTransfer", A, PAY, 2.90, "h8", now - 10)], PAY), cfg, bot)
        assert res3["matched"] == 0 and len(res3["unmatched"]) == 1
        res4 = await hl.match(hl.parse_transfers([led("internalTransfer", A, PAY, 24.99, "h9", now - 5)], PAY), cfg, bot)
        assert res4["matched"] == 1 and abs((await users.get(1))["pro_until"] - (u1["pro_until"] + 365 * 86400)) <= 3
        # poll: sorgu, örnek, eşleşme; adres yoksa atlar; hata sayacı
        cli = Client(ledger=[led("internalTransfer", C, PAY, 2.99, "h10", now - 3)])
        await users.upsert_from_update({"id": 3, "first_name": "u3"}, "3")
        await users.set_address(3, C)
        await core.create_pending(3, "hl", "1m", cfg, from_addr=C)
        st = await hl.poll(cfg, cli, bot)
        assert cli.ledger_calls[0][0] == PAY and st["ok"] == 1 and st["n_in"] == 1 and st["matched_total"] == 1 and st["sample"]
        assert users.is_pro(await users.get(3)) and st["skipped"] == ""
        cfg.pay_hl_address = ""
        st = await hl.poll(cfg, cli, bot)
        assert st["skipped"] and len(cli.ledger_calls) == 1
        cfg.pay_hl_address = PAY

        async def boom(*a, **k):
            raise RuntimeError("HL 500")
        cli.ledger_updates = boom
        st = await hl.poll(cfg, cli, bot)
        assert st["err"] == 1 and "HL 500" in st["last_err"]
        print("✅ USDC) ledger ayrıştırma; adres+tutar eşleşme; çift hash yok; kısa tutar yok; uzatma; poll/örnek/hata")
    asyncio.run(run())


# ------------------------------------------------ 2) Telegram Stars akışı
def test_stars_flow():
    async def run():
        await _fresh()
        cfg = _cfg()
        bot, sent, photos, calls = _bot(cfg)
        await bot._handle_update(dm(5, "/start"))
        await bot._handle_update(cq(5, "pay:stars:1m"))
        inv = [p for m, p in calls if m == "sendInvoice"]
        assert len(inv) == 1 and inv[0]["currency"] == "XTR" and inv[0]["chat_id"] == "5", inv
        assert inv[0]["prices"] == [{"label": "Pro 1 ay", "amount": 230}] and inv[0]["payload"].startswith("pay:")
        pid = int(inv[0]["payload"].split(":")[1])
        pay = await core.get(pid)
        assert pay["method"] == "stars" and pay["amount_raw"] == 230 and pay["status"] == "pending" and pay["user_id"] == 5
        await bot._handle_update(dm(5, "/hesap"))
        assert f"bekleyen Stars faturası #{pid}" in sent[-1][1]
        # pre_checkout: yanlış tutar → ret; doğru → onay
        await bot._handle_update({"pre_checkout_query": {"id": "pcq1", "from": {"id": 5}, "currency": "XTR",
                                                         "total_amount": 100, "invoice_payload": f"pay:{pid}"}})
        ans = [p for m, p in calls if m == "answerPreCheckoutQuery"]
        assert ans[-1]["ok"] is False and "error_message" in ans[-1]
        await bot._handle_update({"pre_checkout_query": {"id": "pcq2", "from": {"id": 5}, "currency": "XTR",
                                                         "total_amount": 230, "invoice_payload": f"pay:{pid}"}})
        ans = [p for m, p in calls if m == "answerPreCheckoutQuery"]
        assert ans[-1] == {"pre_checkout_query_id": "pcq2", "ok": True}
        # successful_payment → Pro + DM + sahibe not
        sp = {"message": {"chat": {"id": 5, "type": "private"}, "from": {"id": 5, "first_name": "A"},
                          "successful_payment": {"currency": "XTR", "total_amount": 230, "invoice_payload": f"pay:{pid}",
                                                 "telegram_payment_charge_id": "ch_1"}}}
        await bot._handle_update(sp)
        u = await users.get(5)
        assert users.is_pro(u) and abs(u["pro_until"] - (dbm.now() + 30 * 86400)) <= 3
        assert (await core.get(pid))["status"] == "paid" and (await core.get(pid))["ext_id"] == "ch_1"
        assert any(c == "5" and "Pro açıldı" in t for c, t, _ in sent) and any(c == "111" and "💰" in t for c, t, _ in sent)
        await bot._handle_update(sp)
        assert (await users.get(5))["pro_until"] == u["pro_until"], "aynı charge ikinci kez kredi vermez"
        # fatura başarısız → kayıt bayat, hata mesajı
        bot.call_fail = {"sendInvoice"}
        await bot._handle_update(cq(5, "pay:stars:3m", "cq9"))
        assert "Fatura oluşturulamadı" in sent[-1][1] and await core.pending("stars") == []
        bot.call_fail = set()
        # sahip /iade → refundStarPayment, kayıt refunded, Pro kapanır, kullanıcıya DM
        await bot._handle_update(own("/iade ch_1"))
        assert any(m == "refundStarPayment" and p == {"user_id": 5, "telegram_payment_charge_id": "ch_1"} for m, p in calls)
        assert (await core.get(pid))["status"] == "refunded" and not users.is_pro(await users.get(5))
        assert "İade edildi" in sent[-1][1] and any(c == "5" and "iade edildi" in t for c, t, _ in sent)
        await bot._handle_update(own("/iade nope"))
        assert "İade olmadı" in sent[-1][1]
        await bot._handle_update(own("/iade"))
        assert "Kullanım" in sent[-1][1]
        print("✅ Stars) fatura XTR/230, pre_checkout ret/onay, successful_payment → Pro, çift charge yok, iade")
    asyncio.run(run())


# ------------------------------------------------ 3) /pro klavyesi + USDC akışı
def test_pro_menu_and_hl_flow():
    async def run():
        await _fresh()
        cfg = _cfg()
        bot, sent, photos, calls = _bot(cfg)
        await bot._handle_update(dm(7, "/start"))
        await bot._handle_update(dm(7, "/pro"))
        datas = _btn_data(sent[-1][2])
        assert "pay:stars:1m" in datas and "pay:hl:1m" in datas and "pay:hl:12m" in datas and not any(d.startswith("pay:np") for d in datas)
        assert "230 Stars" in sent[-1][1] and "$2.99" in sent[-1][1] and "USDC" in sent[-1][1]
        # adres yok → /adres iste, bekleyen kayıt yok
        await bot._handle_update(cq(7, "pay:hl:1m"))
        assert "/adres" in sent[-1][1] and await core.pending("hl") == []
        await bot._handle_update(dm(7, f"/adres {A}"))
        await bot._handle_update(cq(7, "pay:hl:1m", "cq2"))
        pend = await core.pending("hl")
        assert len(pend) == 1 and pend[0]["from_addr"] == A and pend[0]["plan"] == "1m" and pend[0]["user_id"] == 7
        assert PAY in sent[-1][1] and "$2.99 USDC" in sent[-1][1] and f"#{pend[0]['id']}" in sent[-1][1] and A in sent[-1][1]
        await bot._handle_update(dm(7, "/hesap"))
        assert f"bekleyen ödeme #{pend[0]['id']}" in sent[-1][1]
        # yeni paket seçince eskisi bayatlar
        await bot._handle_update(cq(7, "pay:hl:3m", "cq3"))
        pend2 = await core.pending("hl")
        assert len(pend2) == 1 and pend2[0]["plan"] == "3m" and (await core.get(pend[0]["id"]))["status"] == "expired"
        # NOWPayments anahtarı varsa düğme görünür (akış S4)
        cfg.nowpayments_api_key = "k"
        await bot._handle_update(dm(7, "/pro"))
        assert any(d.startswith("pay:np") for d in _btn_data(sent[-1][2]))
        await bot._handle_update(cq(7, "pay:np:1m", "cq4"))
        assert "sonraki sürümde" in sent[-1][1]
        cfg.nowpayments_api_key = ""
        # USDC gelir → 3 ay Pro; /pro artık 'Pro'sun' der
        res = await hl.match(hl.parse_transfers([led("internalTransfer", A, PAY, 7.99, "hx", dbm.now())], PAY), cfg, bot)
        assert res["matched"] == 1
        u = await users.get(7)
        assert abs(u["pro_until"] - (dbm.now() + 90 * 86400)) <= 3
        await bot._handle_update(dm(7, "/pro"))
        assert "Şu an ⭐ Pro'sun" in sent[-1][1]
        # USDC kapalıysa düğme yok, düğme verisi reddedilir; paket satışta değilse de
        cfg.pay_hl_address = ""
        await bot._handle_update(dm(7, "/pro"))
        assert not any(d.startswith("pay:hl") for d in _btn_data(sent[-1][2]))
        await bot._handle_update(cq(7, "pay:hl:1m", "cq5"))
        assert "açık değil" in sent[-1][1]
        await bot._handle_update(cq(7, "pay:stars:99m", "cq6"))
        assert "açık değil" in sent[-1][1]
        print("✅ /pro) klavye yöntemlere göre; USDC: adres şartı, talimat, eski bekleyen bayatlar; /hesap; Pro uzatma")
    asyncio.run(run())


# ------------------------------------------------ 4) faturalama + sahip komutları
def test_billing_and_admin():
    async def run():
        await _fresh()
        cfg = _cfg()
        bot, sent, photos, calls = _bot(cfg)
        now = dbm.now()
        for uid in (1, 2, 3, 4):
            await users.upsert_from_update({"id": uid, "first_name": f"u{uid}", "username": f"u{uid}"}, str(uid))
        async with dbm.db() as c:
            await c.execute("UPDATE users SET pro_until=? WHERE id=1", (now + 2 * 86400,))
            await c.execute("UPDATE users SET pro_until=? WHERE id=2", (now - 3600,))
            await c.execute("UPDATE users SET pro_until=? WHERE id=3", (now + 30 * 86400,))
        await users.mark_blocked("4")
        out = await billing.run_once(cfg, bot)
        assert out == {"expired_payments": 0, "reminded": 1, "expired_users": 1}, out
        assert any(c == "1" and "bitiyor" in t for c, t, _ in sent) and any(c == "2" and "Pro süren bitti" in t for c, t, _ in sent)
        out = await billing.run_once(cfg, bot)
        assert out["reminded"] == 0 and out["expired_users"] == 0, "hatırlatma tek sefer"
        st = await dbm.kv_get(billing.KV_STATS)
        assert st["pro"] == 2 and st["u_total"] == 4 and st["mrr"] == round(2 * 2.99, 2) and st["u_blocked"] == 1
        # bayat bekleyen: 25 saat → expired; bekleyen olmayana kredi yok; iade olmayan ödemeye refund yok
        p = await core.create_pending(3, "hl", "1m", cfg, from_addr=A)
        async with dbm.db() as c:
            await c.execute("UPDATE payments SET created_ts=? WHERE id=?", (now - 25 * 3600, p["id"]))
        assert (await billing.run_once(cfg, bot))["expired_payments"] == 1 and (await core.get(p["id"]))["status"] == "expired"
        assert await core.credit(p["id"], "zz") is None and await core.mark_refunded("hl", "zz") is None
        # sahip komutları
        await bot._handle_update(own("/kullanicilar"))
        t = sent[-1][1]
        assert "👥 kullanıcı 4" in t and "⭐ Pro 2" in t and "MRR ≈ <b>$5.98</b>" in t and "açık" in t and "engelli 1" in t, t
        await bot._handle_update(own("/pro_ver 4 30"))
        assert any(c == "111" and "✅ #4 Pro" in t for c, t, _ in sent) and users.is_pro(await users.get(4))
        assert any(c == "4" and "🎁 Pro açıldı" in t for c, t, _ in sent)
        await bot._handle_update(own("/pro_ver 99 30"))
        assert "kayıtlı kullanıcı yok" in sent[-1][1]
        await bot._handle_update(own("/pro_ver x"))
        assert "Kullanım" in sent[-1][1]
        await bot._handle_update(own("/odemeler"))
        assert "Son ödemeler" in sent[-1][1] and "expired" in sent[-1][1] and "@u3" in sent[-1][1]
        n = len(sent)
        await bot._handle_update(own("/duyuru merhaba dünya"))
        await asyncio.sleep(0.05)
        targets = sorted(c for c, t, _ in sent[n:] if t == "📣 merhaba dünya")
        assert targets == ["1", "2", "3"] and any("Duyuru bitti: 3 gitti" in t for c, t, _ in sent[n:]), "engelli 4 hariç"
        await bot._handle_update(own("/duyuru"))
        assert "Kullanım" in sent[-1][1]
        # yabancı sohbetten sahip komutu çalışmaz
        n = len(sent)
        await bot._handle_update(dm(9, "/kullanicilar"))
        assert not any("👥 kullanıcı" in t for c, t, _ in sent[n:]), "DM'de /kullanicilar coin sorgusu sayılır, satış özeti değil"
        # bağlantı
        from app.health import limits, periods
        assert "paywatch" in limits(cfg) and "billing" in periods(cfg)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "app", "main.py"), encoding="utf-8").read()
        assert '_spawn("paywatch"' in src and '_spawn("billing"' in src
        from app.telegram import format as fmt
        assert fmt.TASK_TR.get("paywatch") and fmt.TASK_TR.get("billing")
        print("✅ faturalama/sahip) 3 gün + bitiş DM tek sefer; bayat temizliği; /kullanicilar /pro_ver /odemeler /duyuru; bağlantı")
    asyncio.run(run())
