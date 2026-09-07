"""Faturalama döngüsü (saatlik): Pro bitişine 3 gün kala ve bitince DM, bayat
bekleyen ödemeleri temizle, satış istatistiğini kv'ye yaz. Hatırlatmalar
alerts_log ile tekilleşir (kullanıcı başına bir kez)."""
import asyncio
import logging

from .. import users
from ..db import alert_log, alert_recent, db, kv_set, now
from . import core

log = logging.getLogger("pay.billing")

KV_STATS = "sales_stats"
REMIND_SEC = 3 * 86400


def expiring_text(until: int) -> str:
    return (f"⏳ Pro süren <b>{users.tr_dt(until)}</b>'de bitiyor. Kesintisiz devam için şimdi uzat: /pro\n"
            "(Uzatma mevcut bitişin üstüne eklenir, gün kaybı olmaz.)")


EXPIRED_TEXT = ("🔒 Pro süren bitti — sorgular yeniden günlük ücretsiz limite döndü, anlık bildirimler durdu.\n"
                "Yeniden açmak için: /pro")


async def run_once(cfg, bot=None, ts=None) -> dict:
    ts = ts or now()
    out = {"expired_payments": await core.expire_stale(ts), "reminded": 0, "expired_users": 0}
    async with db() as conn:
        cur = await conn.execute(
            "SELECT id, chat_id, pro_until FROM users WHERE blocked_ts IS NULL AND pro_until > ? AND pro_until <= ?",
            (ts, ts + REMIND_SEC))
        soon = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT id, chat_id, pro_until FROM users WHERE blocked_ts IS NULL AND pro_until <= ? AND pro_until > ?",
            (ts, ts - 7 * 86400))
        gone = [dict(r) for r in await cur.fetchall()]
    for u in soon:
        key = f"{u['id']}:{u['pro_until']}"
        if await alert_recent("pro_expiring", key, 30 * 86400):
            continue
        await alert_log("pro_expiring", key, "")
        out["reminded"] += 1
        if bot:
            try:
                await bot.send(expiring_text(int(u["pro_until"])), u["chat_id"])
            except Exception:
                log.debug("hatırlatma DM", exc_info=True)
    for u in gone:
        key = f"{u['id']}:{u['pro_until']}"
        if await alert_recent("pro_expired", key, 30 * 86400):
            continue
        await alert_log("pro_expired", key, "")
        out["expired_users"] += 1
        if bot:
            try:
                await bot.send(EXPIRED_TEXT, u["chat_id"])
            except Exception:
                log.debug("bitiş DM", exc_info=True)
    stats = {**(await core.sales_stats(cfg, ts)), **{f"u_{k}": v for k, v in (await users.stats(ts)).items()},
             "ts": ts, **out}
    await kv_set(KV_STATS, stats)
    return out


async def loop(cfg, bot=None) -> None:
    from ..health import beat
    await asyncio.sleep(180)
    while True:
        try:
            await beat("billing")
            await run_once(cfg, bot)
            await beat("billing")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("billing turu hatası")
        await asyncio.sleep(3600)
