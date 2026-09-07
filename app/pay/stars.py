"""Telegram Stars ile ödeme — Bot API: sendInvoice(currency=XTR, provider_token boş)
→ pre_checkout_query (10 sn içinde answerPreCheckoutQuery) → message.successful_payment
(telegram_payment_charge_id) → tahsilat. Ödeme Telegram içinde biter; bize yalnız
charge id gelir. İade: refundStarPayment (sahip /iade). Fiyat = $ × stars_per_usd
(komisyon kullanıcıya yansır — kullanıcı kuralı)."""
import logging

from .. import users
from . import core

log = logging.getLogger("pay.stars")

TITLE = "HL Insider Radar Pro"


def _pid(payload: str) -> int | None:
    try:
        return int(str(payload or "").split(":")[1]) if str(payload or "").startswith("pay:") else None
    except (IndexError, ValueError):
        return None


async def send_invoice(bot, cfg, u: dict, plan: str) -> dict | None:
    """Bekleyen kayıt + Telegram faturası. Fatura gönderilemezse kayıt bayat sayılır."""
    pay = await core.create_pending(int(u["id"]), "stars", plan, cfg)
    if not pay:
        return None
    months = int(pay["months"])
    payload = {"chat_id": u["chat_id"], "title": f"{TITLE} — {months} ay",
               "description": (f"{months} ay sınırsız sorgu + anlık bildirimler. "
                               f"≈ ${float(pay['amount_usd']):.2f}; Stars fiyatına Telegram komisyonu dahildir."),
               "payload": f"pay:{pay['id']}", "currency": "XTR",
               "prices": [{"label": f"Pro {months} ay", "amount": int(pay["amount_raw"])}]}
    st, data = await bot.call("sendInvoice", payload)
    if st != 200 or not data.get("ok"):
        from ..db import db
        async with db() as conn:
            await conn.execute("UPDATE payments SET status='expired' WHERE id=? AND status='pending'", (pay["id"],))
        log.warning("sendInvoice başarısız: %s", data.get("description"))
        return None
    return pay


async def on_pre_checkout(bot, q: dict) -> bool:
    """Telegram ödeme onayından hemen önce sorar; bekleyen kayıt ve tutar tutuyorsa evet."""
    pid = _pid(q.get("invoice_payload"))
    pay = await core.get(pid) if pid else None
    ok = bool(pay and pay.get("status") == "pending" and str(q.get("currency")) == "XTR"
              and int(q.get("total_amount") or 0) >= int(pay.get("amount_raw") or 0))
    payload = {"pre_checkout_query_id": q.get("id"), "ok": ok}
    if not ok:
        payload["error_message"] = "Ödeme kaydı bulunamadı ya da süresi doldu — /pro ile yeniden dene."
    await bot.call("answerPreCheckoutQuery", payload, timeout=10)
    return ok


async def on_successful_payment(bot, cfg, msg: dict) -> dict | None:
    """message.successful_payment → tahsilat (charge id ile idempotent) → 'Pro açıldı'."""
    sp = msg.get("successful_payment") or {}
    pid = _pid(sp.get("invoice_payload"))
    charge = str(sp.get("telegram_payment_charge_id") or "")
    if not pid or not charge:
        log.warning("successful_payment şekli beklenmedik: %s", core.dumps(sp)[:200])
        return None
    res = await core.credit(pid, charge, amount_raw=sp.get("total_amount"), raw=core.dumps(sp), cfg=cfg)
    if res:
        await core.notify_paid(bot, cfg, res["payment"], res["until"])
    return res


async def refund(bot, charge_id: str) -> tuple[bool, str]:
    """Sahip /iade <charge>: Telegram'a iade + kayıt refunded + Pro kapanır."""
    from ..db import db
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM payments WHERE method='stars' AND ext_id=?", (charge_id,))
        row = await cur.fetchone()
    if not row:
        return False, "kayıt yok"
    row = dict(row)
    if row.get("status") != "paid":
        return False, f"durum {row.get('status')}"
    st, data = await bot.call("refundStarPayment", {"user_id": int(row["user_id"]),
                                                    "telegram_payment_charge_id": charge_id})
    if st != 200 or not data.get("ok"):
        return False, str(data.get("description") or st)
    await core.mark_refunded("stars", charge_id)
    u = await users.get(int(row["user_id"]))
    if u and u.get("chat_id"):
        try:
            await bot.send("↩️ Ödemen iade edildi; Pro kapandı.", u["chat_id"])
        except Exception:
            pass
    return True, "iade edildi"
