"""Ödeme çekirdeği — yöntemden bağımsız: bekleyen kayıt → tahsilat → Pro süresi.

Idempotent: `payments(method, ext_id)` UNIQUE — aynı işlem (HL tx hash, Telegram
charge id, NOWPayments id) ikinci kez gelirse kredi verilmez. Tahsilat `users.grant`
ile süreyi şimdi ya da mevcut bitişten (ilerideyse) uzatır; iade Pro'yu hemen kapatır.
Bekleyen kayıt 24 saatte bayatlar (`expire_stale`)."""
import json
import logging
import sqlite3

from .. import users
from ..db import db, now

log = logging.getLogger("pay")

PLAN_DAYS = {"1m": 30, "3m": 90, "12m": 365}
STALE_SEC = 24 * 3600
METHOD_TR = {"hl": "USDC (Hyperliquid)", "stars": "Telegram Stars", "nowpay": "kripto (NOWPayments)"}


def plan_of(cfg, code: str) -> dict | None:
    from ..telegram.public import plans
    for p in plans(cfg):
        if p["code"] == code:
            return p
    return None


async def get(pid: int) -> dict | None:
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM payments WHERE id=?", (int(pid),))
        r = await cur.fetchone()
        return dict(r) if r else None


async def create_pending(uid: int, method: str, plan: str, cfg, from_addr: str | None = None,
                         ext_id: str | None = None, raw: str | None = None) -> dict | None:
    p = plan_of(cfg, plan)
    if not p or method not in METHOD_TR:
        return None
    amount_raw = p["stars"] if method == "stars" else p["usd"]
    currency = "XTR" if method == "stars" else "USD"
    async with db() as conn:
        cur = await conn.execute(
            """INSERT INTO payments(user_id, method, plan, months, amount_usd, amount_raw, currency,
                 ext_id, from_addr, status, created_ts, raw)
               VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?)""",
            (int(uid), method, plan, p["months"], p["usd"], amount_raw, currency, ext_id, from_addr, now(), raw))
        pid = cur.lastrowid
    return await get(pid)


async def pending(method: str | None = None) -> list[dict]:
    async with db() as conn:
        if method:
            cur = await conn.execute(
                "SELECT * FROM payments WHERE status='pending' AND method=? ORDER BY created_ts", (method,))
        else:
            cur = await conn.execute("SELECT * FROM payments WHERE status='pending' ORDER BY created_ts")
        return [dict(r) for r in await cur.fetchall()]


async def pending_for_user(uid: int) -> list[dict]:
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM payments WHERE status='pending' AND user_id=? ORDER BY created_ts", (int(uid),))
        return [dict(r) for r in await cur.fetchall()]


async def find_pending(method: str, from_addr: str | None = None, max_usd: float | None = None) -> dict | None:
    """Aynı yöntem (+ gönderen adres) için EN ESKİ bekleyen kayıt; `max_usd` verilirse
    fiyatı bu tutara sığan (transfer tutarı ≥ plan fiyatı) ilk kayıt."""
    rows = await pending(method)
    for r in rows:
        if from_addr is not None and (r.get("from_addr") or "").lower() != from_addr.lower():
            continue
        if max_usd is not None and float(r.get("amount_usd") or 0) > max_usd:
            continue
        return r
    return None


async def cancel_pending(uid: int, method: str | None = None) -> int:
    """Kullanıcının bekleyen kayıtları (yeni akış başlarken eskisi bayatlar)."""
    async with db() as conn:
        if method:
            cur = await conn.execute(
                "UPDATE payments SET status='expired' WHERE status='pending' AND user_id=? AND method=?",
                (int(uid), method))
        else:
            cur = await conn.execute(
                "UPDATE payments SET status='expired' WHERE status='pending' AND user_id=?", (int(uid),))
        return cur.rowcount or 0


async def already_credited(method: str, ext_id: str) -> bool:
    if not ext_id:
        return False
    async with db() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM payments WHERE method=? AND ext_id=? AND status IN ('paid','refunded')", (method, ext_id))
        return (await cur.fetchone()) is not None


async def credit(pid: int, ext_id: str, amount_raw=None, raw: str | None = None) -> dict | None:
    """Tahsilat: pending → paid (+ext_id), Pro süresi uzar. Aynı ext_id daha önce
    kredilendiyse ya da kayıt bekleyen değilse None (çift kredi yok)."""
    row = await get(pid)
    if not row or row.get("status") != "pending":
        return None
    try:
        async with db() as conn:
            cur = await conn.execute(
                """UPDATE payments SET status='paid', ext_id=?, amount_raw=COALESCE(?, amount_raw),
                     paid_ts=?, raw=COALESCE(?, raw) WHERE id=? AND status='pending'""",
                (ext_id, amount_raw, now(), raw, int(pid)))
            if not cur.rowcount:
                return None
    except sqlite3.IntegrityError:
        log.warning("çift tahsilat engellendi: %s %s", row.get("method"), ext_id)
        return None
    days = PLAN_DAYS.get(row.get("plan") or "", int(row.get("months") or 1) * 30)
    until = await users.grant(int(row["user_id"]), days, f"{row['method']}:{row['plan']}")
    log.info("ödeme alındı #%s %s %s $%.2f → kullanıcı %s Pro %s'e kadar",
             pid, row["method"], row["plan"], float(row.get("amount_usd") or 0), row["user_id"], until)
    return {"payment": await get(pid), "until": until}


async def mark_refunded(method: str, ext_id: str) -> dict | None:
    """İade: kayıt refunded, kullanıcının Pro'su hemen biter."""
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM payments WHERE method=? AND ext_id=? AND status='paid'", (method, ext_id))
        row = await cur.fetchone()
        if not row:
            return None
        row = dict(row)
        await conn.execute("UPDATE payments SET status='refunded' WHERE id=?", (row["id"],))
        await conn.execute("UPDATE users SET pro_until=? WHERE id=? AND COALESCE(pro_until,0) > ?",
                           (now(), row["user_id"], now()))
    return row


async def expire_stale(ts=None) -> int:
    ts = ts or now()
    async with db() as conn:
        cur = await conn.execute(
            "UPDATE payments SET status='expired' WHERE status='pending' AND created_ts < ?", (ts - STALE_SEC,))
        return cur.rowcount or 0


async def recent(limit: int = 10) -> list[dict]:
    async with db() as conn:
        cur = await conn.execute(
            "SELECT p.*, u.username, u.first_name FROM payments p LEFT JOIN users u ON u.id = p.user_id"
            " ORDER BY p.created_ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def sales_stats(cfg, ts=None) -> dict:
    ts = ts or now()
    async with db() as conn:
        async def one(q, p=()):
            cur = await conn.execute(q, p)
            return (await cur.fetchone())[0]
        day0 = ts - 86400
        out = {"paid_24h": await one("SELECT COUNT(*) FROM payments WHERE status='paid' AND paid_ts > ?", (day0,)),
               "usd_24h": float(await one("SELECT COALESCE(SUM(amount_usd),0) FROM payments WHERE status='paid' AND paid_ts > ?", (day0,))),
               "paid_30d": await one("SELECT COUNT(*) FROM payments WHERE status='paid' AND paid_ts > ?", (ts - 30 * 86400,)),
               "usd_30d": float(await one("SELECT COALESCE(SUM(amount_usd),0) FROM payments WHERE status='paid' AND paid_ts > ?", (ts - 30 * 86400,))),
               "pending": await one("SELECT COUNT(*) FROM payments WHERE status='pending'"),
               "refunded": await one("SELECT COUNT(*) FROM payments WHERE status='refunded'"),
               "pro": await one("SELECT COUNT(*) FROM users WHERE pro_until > ?", (ts,))}
    out["mrr"] = round(out["pro"] * float(getattr(cfg, "pro_price_usd_1m", 0) or 0), 2)
    return out


def paid_text(pay: dict, until: int) -> str:
    return (f"✅ <b>Pro açıldı</b> — {int(pay.get('months') or 1)} ay · {METHOD_TR.get(pay.get('method'), pay.get('method'))}\n"
            f"Bitiş: <b>{users.tr_dt(until)}</b>\n"
            "Artık sorgular sınırsız; /bildirimler ile anlık bildirim türlerini seç. Teşekkürler 🙌")


async def notify_paid(bot, cfg, pay: dict, until: int) -> None:
    """Kullanıcıya 'Pro açıldı', sahibe kısa not (ana sohbet). Gönderim hatası tahsilatı etkilemez."""
    if bot is None:
        return
    try:
        u = await users.get(int(pay["user_id"]))
        if u and u.get("chat_id"):
            await bot.send(paid_text(pay, until), u["chat_id"])
        if getattr(cfg, "telegram_chat_id", ""):
            who = f"@{u['username']}" if u and u.get("username") else f"#{pay['user_id']}"
            await bot.send(f"💰 Ödeme: {who} · {pay.get('plan')} · ${float(pay.get('amount_usd') or 0):.2f}"
                           f" · {METHOD_TR.get(pay.get('method'), pay.get('method'))}", cfg.telegram_chat_id)
    except Exception:
        log.debug("ödeme bildirimi", exc_info=True)


def dumps(o) -> str:
    try:
        return json.dumps(o, ensure_ascii=False)[:2000]
    except Exception:
        return str(o)[:2000]
