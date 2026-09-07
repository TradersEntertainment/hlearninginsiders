"""Sahip komutları — satış tarafı: /kullanicilar, /odemeler, /pro_ver, /duyuru, /iade.
Yalnız sahip sohbetinden (bot.py owner zinciri) çağrılır."""
import asyncio
import logging

from .. import users
from ..db import now
from ..pay import core
from . import format as fmt

log = logging.getLogger("telegram.admin")


async def handle(bot, cmd: str, args: list[str], chat_id: str) -> bool:
    if cmd in ("kullanicilar", "kullanıcılar", "users", "satis", "satış"):
        await bot.send(await users_text(bot.cfg), chat_id)
    elif cmd in ("odemeler", "ödemeler", "payments"):
        await bot.send(payments_text(await core.recent(12)), chat_id)
    elif cmd == "pro_ver":
        await _grant(bot, args, chat_id)
    elif cmd == "duyuru":
        await _broadcast(bot, args, chat_id)
    elif cmd in ("iade", "refund"):
        await _refund(bot, args, chat_id)
    else:
        return False
    return True


async def users_text(cfg) -> str:
    st = await users.stats()
    ss = await core.sales_stats(cfg)
    on = "açık" if getattr(cfg, "public_bot_enabled", False) else "KAPALI"
    return (f"🛒 <b>Satış</b> · herkese açık bot: <b>{on}</b>\n"
            f"👥 kullanıcı {st['total']} · son 24s yeni {st['new_24h']} · 7g aktif {st['active_7d']}"
            f" · engelli {st['blocked']} · adresli {st['with_addr']}\n"
            f"⭐ Pro {st['pro']} (3 gün içinde biten {st['expiring_3d']}) · MRR ≈ <b>${ss['mrr']:.2f}</b>\n"
            f"💰 ödeme 24s {ss['paid_24h']} (${ss['usd_24h']:.2f}) · 30g {ss['paid_30d']} (${ss['usd_30d']:.2f})"
            f" · bekleyen {ss['pending']} · iade {ss['refunded']}\n"
            f"🔎 sorgu bugün {st['q_today']} · toplam {st['q_total']}\n"
            "/odemeler · /pro_ver &lt;id&gt; &lt;gün&gt; · /duyuru &lt;metin&gt; · /iade &lt;charge&gt;")


def payments_text(rows: list[dict]) -> str:
    if not rows:
        return "💳 Ödeme kaydı yok."
    lines = ["💳 <b>Son ödemeler</b>"]
    for r in rows:
        who = f"@{r['username']}" if r.get("username") else f"#{r.get('user_id')}"
        ext = fmt.esc(str(r.get("ext_id") or "")[:14])
        lines.append(f"#{r['id']} {fmt.esc(who)} · {r.get('plan')} · ${float(r.get('amount_usd') or 0):.2f}"
                     f" · {core.METHOD_TR.get(r.get('method'), r.get('method'))} · <b>{r.get('status')}</b>"
                     f" · {users.tr_dt(r.get('paid_ts') or r.get('created_ts') or now())}"
                     + (f" · <code>{ext}</code>" if ext else ""))
    return "\n".join(lines)


async def _grant(bot, args: list[str], chat_id: str) -> None:
    if len(args) < 2 or not args[0].lstrip("-").isdigit() or not args[1].isdigit():
        await bot.send("Kullanım: <code>/pro_ver &lt;kullanıcı id&gt; &lt;gün&gt;</code>", chat_id)
        return
    uid, days = int(args[0]), int(args[1])
    u = await users.get(uid)
    if not u:
        await bot.send(f"#{uid} diye kayıtlı kullanıcı yok (önce bota /start yazmalı).", chat_id)
        return
    until = await users.grant(uid, days, "elle")
    await bot.send(f"✅ #{uid} Pro → {users.tr_dt(until)}", chat_id)
    if u.get("chat_id"):
        try:
            await bot.send(f"🎁 Pro açıldı ({days} gün) — bitiş <b>{users.tr_dt(until)}</b>. "
                           "/bildirimler ile türleri seç.", u["chat_id"])
        except Exception:
            log.debug("pro_ver DM", exc_info=True)


async def _broadcast(bot, args: list[str], chat_id: str) -> None:
    text = " ".join(args).strip()
    if not text:
        await bot.send("Kullanım: <code>/duyuru &lt;metin&gt;</code> — engelsiz tüm kullanıcılara DM (20 msg/sn).", chat_id)
        return
    targets = await users.all_active()
    await bot.send(f"📣 {len(targets)} kullanıcıya gönderiliyor…", chat_id)

    async def run():
        sent = fail = 0
        for u in targets:
            try:
                ok = await bot.send(f"📣 {text}", u["chat_id"])
            except Exception:
                ok = False
            sent += 1 if ok else 0
            fail += 0 if ok else 1
        await bot.send(f"📣 Duyuru bitti: {sent} gitti · {fail} gitmedi (engelliler düşüldü).", chat_id)
    asyncio.create_task(run())


async def _refund(bot, args: list[str], chat_id: str) -> None:
    if not args:
        await bot.send("Kullanım: <code>/iade &lt;telegram charge id&gt;</code> (yalnız Stars ödemeleri).", chat_id)
        return
    from ..pay import stars
    ok, why = await stars.refund(bot, args[0])
    await bot.send(("↩️ İade edildi, Pro kapandı." if ok else f"❌ İade olmadı: {fmt.esc(why)}"), chat_id)
