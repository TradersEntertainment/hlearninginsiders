"""Herkese açık DM akışı — satılabilir bot.

Sahibin sohbeti ve kendi kanalları bot.py'de kalır; burası yalnız `chat.type ==
"private"` sohbetlerle ve inline düğme (callback) olaylarıyla konuşur,
`public_bot_enabled` açıkken. Akış:
  /start                → kayıt + karşılama + menü
  düz metin (ya da /hype) → coin sorgusu: MEVCUT `cryptoliq.snapshot` + `liqchart`
                          hattı (canlı fiyat, liq'e en yakın büyük pozisyonlar, zincir
                          hedefi, 15 dk × 48 s grafik). Ücretsizde günlük kota, Pro'da
                          dakika limiti, coin başına önbellek, herkes için ortak kova
  /hesap · /adres 0x… · /yardim · /pro (ödeme, S2) · /bildirimler (abonelik, S3)
Sahip komutları buradan ASLA çalışmaz. Metinler TEXTS'te (ileride EN)."""
import logging
import re
from collections import deque

from .. import users
from ..db import now
from . import format as fmt

log = logging.getLogger("telegram.public")

DISCLAIMER = "<i>Gözlem aracıdır, yatırım tavsiyesi değildir.</i>"
_SYM_RE = re.compile(r"^[A-Za-z0-9:_.\-]{1,24}$")
CACHE_MAX = 500

_cache: dict[str, tuple[int, dict]] = {}   # coin → (ts, anlık görüntü); metin/altyazı kullanıcıya göre üretilir
_bucket: deque = deque()                                 # HL'ye giden sorgular (son 60 sn)


def kb(rows) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def kb_url(text: str, url: str) -> dict:
    return {"inline_keyboard": [[{"text": text, "url": url}]]}


def menu() -> dict:
    return kb([[("📈 Örnek: HYPE", "q:HYPE"), ("🔔 Bildirimler", "m:notif")],
               [("⭐ Pro", "m:pro"), ("❓ Yardım", "m:help")]])


def reset_memory() -> None:
    _cache.clear()
    _bucket.clear()


# ---------------- metinler ----------------

def free_limit(cfg) -> int:
    return int(getattr(cfg, "free_daily_queries", 3) or 0)


def welcome(cfg) -> str:
    return ("🕵️ <b>HL Insider Radar</b>\n"
            "Hyperliquid'de balinaların likidasyon seviyelerini, dev pozisyonları ve sessiz "
            "birikimleri izler.\n\n"
            "📈 Bir coin adı yaz — <code>HYPE</code>, <code>BTC</code>, <code>TSLA</code> — liq'e en "
            "yakın büyük pozisyonlar, zincir hedefi ve grafik gelsin.\n"
            f"🆓 Ücretsiz: günde <b>{free_limit(cfg)}</b> sorgu.\n"
            "⭐ Pro: sınırsız sorgu + seçtiğin türlerde anlık bildirim — /pro\n\n"
            "Komutlar: /hesap · /bildirimler · /adres · /yardim\n" + DISCLAIMER)


def help_text(cfg) -> str:
    return ("❓ <b>Nasıl kullanılır</b>\n"
            "• Coin adı yaz: <code>HYPE</code>, <code>SOL</code>, <code>TSLA</code>, <code>SP500</code>"
            " → liq'e en yakın büyük pozisyonlar, zorunlu alış/satış, zincir hedefi, grafik.\n"
            "• Pozisyon verisi süpürülen ~1500 büyük hesaptan gelir — HL'nin tamamı değil; mesaj ne kadar"
            " eski olduğunu söyler.\n"
            f"• 🆓 Ücretsiz: günde {free_limit(cfg)} sorgu (TSİ 00:00'da yenilenir).\n"
            "• ⭐ Pro: sınırsız sorgu, /bildirimler ile anlık bildirim türleri ve coin filtresi.\n"
            "• /hesap katmanın ve kalan hakkın · /adres 0x… HL adresin (USDC ödemesi buradan eşleşir)\n"
            + DISCLAIMER)


def account_text(u: dict, cfg, ts=None) -> str:
    ts = ts or now()
    pro = users.is_pro(u, ts)
    lim = free_limit(cfg)
    used = int(u.get("q_used") or 0) if u.get("q_day") == users.tr_day(ts) else 0
    lines = ["👤 <b>Hesap</b>",
             (f"Katman: ⭐ <b>Pro</b> · bitiş {users.tr_dt(u['pro_until'])}" if pro else "Katman: 🆓 Ücretsiz"),
             (f"Bugünkü sorgu: {used}" if pro else f"Bugünkü sorgu: {used}/{lim}"),
             f"Toplam sorgu: {int(u.get('q_total') or 0)}",
             (f"HL adresi: <code>{fmt.esc(u['hl_address'])}</code>" if u.get("hl_address")
              else "HL adresi: yok — <code>/adres 0x…</code>")]
    if not pro:
        lines.append("⭐ Pro ile sınırsız sorgu + anlık bildirimler — /pro")
    return "\n".join(lines)


def footer(u: dict, cfg, left) -> str:
    if users.is_pro(u):
        return "⭐ Pro · /bildirimler ile anlık bildirim türlerini seç"
    return (f"🆓 bugün <b>{int(left or 0)}/{free_limit(cfg)}</b> sorgu kaldı"
            " · Pro'da sınırsız + anlık bildirim ⭐ /pro")


def limit_text(cfg, why: str) -> str:
    if why == "minute":
        return f"🐢 Dakikada en çok {int(getattr(cfg, 'pro_query_per_min', 6) or 6)} sorgu — birazdan tekrar dene."
    return (f"⏳ Bugünlük {free_limit(cfg)} ücretsiz sorgun bitti; yarın TSİ 00:00'da yenilenir.\n"
            "⭐ Pro ile sınırsız sorgu + anlık bildirimler — /pro")


def unknown_text(q: str) -> str:
    return (f"❓ <b>{fmt.esc(q[:24])}</b> tanımadım. HL'de listeli bir coin adı yaz: "
            "HYPE, BTC, SOL, PUMP, TSLA, NVDA, SP500…")


BUSY_TEXT = "🚦 Şu an yoğunluk var, 30 sn sonra tekrar dene."


async def unknown_message(cfg, q: str) -> str:
    """Çözümlenemeyen sembol: başka builder dex'inde mi, hariç mi tutulmuş, yakın ad var mı."""
    from ..assets import is_excluded
    from ..hl.universe import find_in_hip3, similar_names
    sym = fmt.esc(q[:24].upper())
    from .. import assets
    h = await find_in_hip3(q, assets.watched_dexes(cfg))
    if h and h.get("watched"):
        return (f"⏳ <b>{sym}</b> Hyperliquid'de <b>{fmt.esc(h['dex'])}</b> dex'inde listeli ve izleme listesinde; "
                "evren yenilemesi (en geç 6 saat) sonrasında sorgulanabilir — birazdan tekrar dene.")
    if h:
        return (f"ℹ️ <b>{sym}</b> Hyperliquid'de <b>{fmt.esc(h['dex'])}</b> builder dex'inde listeli ama bu bot o "
                "dex'i izlemiyor — pozisyon ve liq verisi yok. Başka bir coin dene: HYPE, BTC, SOL…")
    if is_excluded(q):
        return f"⛔ <b>{sym}</b> takip dışı bırakılmış."
    text = unknown_text(q)
    sim = await similar_names(q)
    if sim:
        text += "\nYakın adlar: " + ", ".join(f"<code>{fmt.esc(s)}</code>" for s in sim)
    return text


def plans(cfg) -> list[dict]:
    """Pro paketleri: (kod, ay, $, Stars). Stars = $ × kur, yukarı yuvarlanır."""
    rate = float(getattr(cfg, "stars_per_usd", 77) or 77)
    out = []
    for code, months, field in (("1m", 1, "pro_price_usd_1m"), ("3m", 3, "pro_price_usd_3m"),
                                ("12m", 12, "pro_price_usd_12m")):
        usd = float(getattr(cfg, field, 0) or 0)
        if usd > 0:
            out.append({"code": code, "months": months, "days": months * 30, "usd": round(usd, 2),
                        "stars": int(round(usd * rate))})
    return out


def pay_methods(cfg) -> list[str]:
    """Açık ödeme yolları: Stars her zaman; USDC PAY_HL_ADDRESS varsa; kripto NOWPayments anahtarı varsa."""
    out = ["stars"]
    if getattr(cfg, "pay_hl_address", ""):
        out.append("hl")
    if getattr(cfg, "nowpayments_api_key", ""):
        out.append("np")
    return out


def pro_text(cfg) -> str:
    lines = ["⭐ <b>Pro</b> — sınırsız sorgu + anlık bildirimler (tür ve coin filtresiyle)"]
    for p in plans(cfg):
        per = p["usd"] / p["months"]
        lines.append(f"• {p['months']} ay: <b>${p['usd']:.2f}</b>" + (f" (aylık ${per:.2f})" if p["months"] > 1 else "")
                     + f" · {p['stars']} Stars")
    m = pay_methods(cfg)
    names = {"stars": "⭐ Telegram Stars (komisyon dahil)", "hl": "💵 USDC (Hyperliquid'de Send, ücretsiz)",
             "np": "🪙 kripto (NOWPayments)"}
    lines.append("Ödeme: " + " · ".join(names[x] for x in m))
    lines.append("Aşağıdan paket + ödeme yolunu seç. Uzatma mevcut bitişin üstüne eklenir.")
    sup = getattr(cfg, "support_contact", "") or ""
    if sup:
        lines.append(f"Destek: {fmt.esc(sup)}")
    return "\n".join(lines)


def pay_keyboard(cfg) -> dict:
    rows = []
    m = pay_methods(cfg)
    for p in plans(cfg):
        row = [(f"⭐ {p['months']} ay · {p['stars']} Stars", f"pay:stars:{p['code']}")]
        if "hl" in m:
            row.append((f"💵 {p['months']} ay · ${p['usd']:.2f} USDC", f"pay:hl:{p['code']}"))
        if "np" in m:
            row.append((f"🪙 {p['months']} ay · kripto", f"pay:np:{p['code']}"))
        rows.append(row)
    return kb(rows)


def hl_instructions(cfg, u: dict, pay: dict) -> str:
    return (f"💵 <b>USDC ile ödeme</b> — Pro {int(pay['months'])} ay · ödeme no <b>#{pay['id']}</b>\n"
            f"1️⃣ Hyperliquid'de <b>Send</b> (Transfer) ile <b>tam ${float(pay['amount_usd']):.2f} USDC</b> gönder:\n"
            f"   alıcı: <code>{fmt.esc(getattr(cfg, 'pay_hl_address', ''))}</code>\n"
            f"2️⃣ Gönderen adres kayıtlı adresin olmalı: <code>{fmt.esc(u.get('hl_address') or '')}</code>"
            " (farklıysa önce /adres ile güncelle)\n"
            "3️⃣ 24 saat içinde gelince Pro otomatik açılır (≈1–2 dk); mesajla haber verilir.\n"
            "<i>HL içi USDC gönderimi ücretsizdir; başka ağdan ya da borsadan gönderme — eşleşmez.</i>")


def pending_lines(rows: list[dict]) -> list[str]:
    out = []
    for p in rows:
        if p.get("method") == "hl":
            out.append(f"⏳ bekleyen ödeme #{p['id']}: ${float(p['amount_usd']):.2f} USDC, "
                       f"<code>{fmt.esc(p.get('from_addr') or '')}</code> adresinden (24 s)")
        elif p.get("method") == "stars":
            out.append(f"⏳ bekleyen Stars faturası #{p['id']}: {int(p['amount_raw'] or 0)} Stars")
        else:
            out.append(f"⏳ bekleyen ödeme #{p['id']} ({p.get('method')})")
    return out


# ---------------- akış ----------------

async def handle(bot, upd: dict) -> bool:
    """message / callback_query / ödeme olayları → cevap. Dönüş: bu akış işledi mi."""
    cq = upd.get("callback_query")
    if cq:
        return await _callback(bot, cq)
    pcq = upd.get("pre_checkout_query")
    if pcq:
        from ..pay import stars
        await stars.on_pre_checkout(bot, pcq)
        return True
    msg = upd.get("message") or {}
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return False
    frm = msg.get("from") or {}
    if not frm.get("id") or frm.get("is_bot"):
        return False
    if msg.get("successful_payment"):
        from ..pay import stars
        await users.upsert_from_update(frm, str(chat.get("id")))
        await stars.on_successful_payment(bot, bot.cfg, msg)
        return True
    text = (msg.get("text") or "").strip()
    if not text:
        return False
    uid = int(frm["id"])
    if not users.flood_ok(uid):
        return True                                   # sel: sessiz
    u = await users.upsert_from_update(frm, str(chat.get("id")))
    if text.startswith("/"):
        from .bot import _cmd_lower
        parts = text.split()
        return await command(bot, u, _cmd_lower(parts[0][1:].split("@")[0]), parts[1:])
    return await run_query(bot, u, text)


async def _ack(bot, cq_id, text: str = "") -> None:
    try:
        await bot.answer_callback(cq_id, text)
    except Exception:
        log.debug("answerCallbackQuery", exc_info=True)


async def _callback(bot, cq: dict) -> bool:
    frm = cq.get("from") or {}
    msg = cq.get("message") or {}
    chat = msg.get("chat") or {}
    data = str(cq.get("data") or "")
    cq_id = cq.get("id")
    if not frm.get("id") or chat.get("type") != "private":
        await _ack(bot, cq_id)
        return False
    if not users.flood_ok(int(frm["id"])):
        await _ack(bot, cq_id)
        return True
    u = await users.upsert_from_update(frm, str(chat.get("id")))
    if data.startswith("k:"):                      # tür düğmesi: cevap metni + klavye yerinde güncellenir
        if not users.is_pro(u):
            await _ack(bot, cq_id, "Pro gerekir")
            await notif_menu(bot, u)
            return True
        have, ack = await toggle_kind(bot, u, data[2:])
        await _ack(bot, cq_id, ack)
        try:
            await bot.edit_reply_markup(u["chat_id"], msg.get("message_id"), kinds_keyboard(bot.cfg, have))
        except Exception:
            log.debug("klavye güncelleme", exc_info=True)
        return True
    await _ack(bot, cq_id)
    if data.startswith("q:"):
        return await run_query(bot, u, data[2:])
    if data.startswith("m:"):
        return await command(bot, u, {"help": "yardim", "pro": "pro", "notif": "bildirimler"}.get(data[2:], "yardim"), [])
    if data.startswith("pay:"):
        parts = data.split(":")
        if len(parts) == 3:
            await pay_start(bot, u, parts[1], parts[2])
            return True
    return await command(bot, u, "yardim", [])


async def account_message(u: dict, cfg) -> str:
    from ..pay import core as paycore
    lines = [account_text(u, cfg)]
    lines += pending_lines(await paycore.pending_for_user(int(u["id"])))
    return "\n".join(lines)


async def command(bot, u: dict, cmd: str, args: list[str]) -> bool:
    cfg = bot.cfg
    chat = u["chat_id"]
    if cmd == "start":
        await bot.send(welcome(cfg), chat, reply_markup=menu())
    elif cmd in ("help", "yardim", "yardım"):
        await bot.send(help_text(cfg), chat, reply_markup=menu())
    elif cmd in ("hesap", "account"):
        await bot.send(await account_message(u, cfg), chat)
    elif cmd == "id":
        await bot.send(f"Chat id: <code>{chat}</code>", chat)
    elif cmd == "adres":
        await _cmd_address(bot, u, args)
    elif cmd == "pro":
        await pro_menu(bot, u)
    elif cmd in ("bildirimler", "notif", "notifications", "coinler", "sessiz"):
        await notif_menu(bot, u, cmd, args)
    else:
        return await run_query(bot, u, cmd)          # /hype, /btc…
    return True


async def _cmd_address(bot, u: dict, args: list[str]) -> None:
    chat = u["chat_id"]
    if not args:
        cur = u.get("hl_address")
        await bot.send(("Kayıtlı HL adresin: <code>" + fmt.esc(cur) + "</code>\n" if cur else "Kayıtlı HL adresin yok.\n")
                       + "Kaydetmek için: <code>/adres 0x…</code> (Hyperliquid hesabının adresi — "
                       "USDC ödemesi bu adresten gelince otomatik eşleşir)", chat)
        return
    addr = users.valid_address(args[0])
    if not addr:
        await bot.send("❌ Geçersiz adres. 0x ile başlayan 42 karakterlik HL/EVM adresi olmalı.", chat)
        return
    await users.set_address(int(u["id"]), addr)
    u["hl_address"] = addr
    await bot.send(f"✅ HL adresin kaydedildi: <code>{addr}</code>", chat)


async def pro_menu(bot, u: dict) -> None:
    """Paketler + ödeme yolu düğmeleri; bekleyen ödemesi varsa hatırlatır."""
    from ..pay import core as paycore
    text = pro_text(bot.cfg)
    if users.is_pro(u):
        text = f"Şu an ⭐ Pro'sun (bitiş {users.tr_dt(u['pro_until'])}); uzatma bitişin üstüne eklenir.\n\n" + text
    pend = pending_lines(await paycore.pending_for_user(int(u["id"])))
    if pend:
        text += "\n\n" + "\n".join(pend)
    await bot.send(text, u["chat_id"], reply_markup=pay_keyboard(bot.cfg))


async def pay_start(bot, u: dict, method: str, code: str) -> None:
    """Düğme: pay:<yöntem>:<paket>. Stars → fatura; hl → adres + talimat; np → S4."""
    from ..pay import core as paycore
    cfg = bot.cfg
    chat = u["chat_id"]
    p = paycore.plan_of(cfg, code)
    if not p or method not in pay_methods(cfg):
        await bot.send("Bu paket ya da ödeme yolu şu an açık değil. /pro", chat)
        return
    if method == "stars":
        from ..pay import stars
        await paycore.cancel_pending(int(u["id"]), "stars")
        pay = await stars.send_invoice(bot, cfg, u, code)
        if not pay:
            await bot.send("❌ Fatura oluşturulamadı, birazdan tekrar dene ya da 💵 USDC ile öde.", chat)
        return
    if method == "hl":
        if not u.get("hl_address"):
            await bot.send("Önce Hyperliquid adresini kaydet: <code>/adres 0x…</code> — USDC bu adresten gelince "
                           "otomatik eşleşir. Sonra tekrar /pro.", chat)
            return
        await paycore.cancel_pending(int(u["id"]), "hl")
        pay = await paycore.create_pending(int(u["id"]), "hl", code, cfg, from_addr=u["hl_address"])
        await bot.send(hl_instructions(cfg, u, pay), chat)
        return
    from ..pay import nowpay
    await paycore.cancel_pending(int(u["id"]), "nowpay")
    pay, url = await nowpay.create_invoice(cfg, getattr(bot, "session", None), u, code)
    if not pay:
        await bot.send("❌ Kripto faturası oluşturulamadı; ⭐ Stars ya da 💵 USDC ile dene.", chat)
        return
    await bot.send(f"🪙 <b>Kripto ile ödeme</b> — Pro {int(pay['months'])} ay · ${float(pay['amount_usd']):.2f}"
                   f" · ödeme no <b>#{pay['id']}</b>\nDüğmeden ödeme sayfasını aç, coin/ağı seç ve gönder. Ağ onayı "
                   "gelince Pro otomatik açılır (birkaç dk); mesajla haber verilir. Fatura 24 saat geçerli.",
                   chat, reply_markup=kb_url("🪙 Ödeme sayfasını aç", url))


def allowed_kinds(cfg) -> list[str]:
    """Satılan türler: notify.PUBLIC_KINDS ∩ cfg.public_kinds (sıra PUBLIC_KINDS'ınki)."""
    from ..notify import PUBLIC_KINDS
    pk = users.csv_set(getattr(cfg, "public_kinds", ""))
    return [k for k in PUBLIC_KINDS if k in pk]


def kinds_keyboard(cfg, have: set[str]) -> dict:
    from ..notify import PUBLIC_KINDS
    rows, row = [], []
    for k in allowed_kinds(cfg):
        row.append((("✅ " if k in have else "☐ ") + PUBLIC_KINDS[k][0], f"k:{k}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("🎯 Hepsi açık", "k:*on"), ("🔕 Hepsi kapalı", "k:*off")])
    return kb(rows)


def notif_text(u: dict, kinds: set[str], coins: set[str]) -> str:
    qs, qe = u.get("quiet_start"), u.get("quiet_end")
    quiet = (f"{int(qs):02d}–{int(qe):02d}" if qs is not None and qe is not None and int(qs) != int(qe) else "kapalı")
    return ("🔔 <b>Bildirimler</b> — açık türler olduğu anda DM'ine gelir.\n"
            f"Coin filtresi: <b>{'hepsi' if not coins else fmt.esc(', '.join(sorted(coins)))}</b>"
            " · <code>/coinler HYPE,BTC</code> ya da <code>/coinler hepsi</code>\n"
            f"Sessiz saat (TSİ): <b>{quiet}</b> · <code>/sessiz 23-08</code> ya da <code>/sessiz kapat</code>\n"
            f"<b>{len(kinds)}</b> tür açık — düğmelerle aç/kapat:")


async def notif_menu(bot, u: dict, cmd: str = "bildirimler", args: list[str] | None = None) -> None:
    """Abonelik: tür düğmeleri (k:<tür>), /coinler filtresi, /sessiz saat. Yalnız Pro."""
    cfg = bot.cfg
    chat = u["chat_id"]
    args = args or []
    if not users.is_pro(u):
        await bot.send("🔔 Anlık bildirimler Pro'da: tür seçimi, coin filtresi, sessiz saat. ⭐ /pro",
                       chat, reply_markup=menu())
        return
    uid = int(u["id"])
    if cmd == "coinler":
        if not args:
            coins = await users.get_coins(uid)
            await bot.send("Coin filtresi: <b>" + (fmt.esc(", ".join(sorted(coins))) if coins else "hepsi") + "</b>\n"
                           "<code>/coinler HYPE,BTC,SOL</code> · <code>/coinler hepsi</code>", chat)
            return
        if args[0].lower() in ("hepsi", "tümü", "tumu", "all", "kapat"):
            await users.set_coins(uid, "")
            await bot.send("✅ Coin filtresi kaldırıldı: tüm coinler.", chat)
            return
        syms = await users.set_coins(uid, args)
        if not syms:
            await bot.send("❌ Geçerli sembol yok. Örnek: <code>/coinler HYPE,BTC,SOL</code>", chat)
            return
        await bot.send(f"✅ Coin filtresi: <b>{fmt.esc(', '.join(sorted(syms)))}</b> — yalnız bu coinlerin "
                       "bildirimleri gelir.", chat)
        return
    if cmd == "sessiz":
        if not args:
            await bot.send("Sessiz saat: <code>/sessiz 23-08</code> (TSİ; o aralıkta bildirim gelmez, sorgular "
                           "çalışır) · <code>/sessiz kapat</code>", chat)
            return
        if args[0].lower() in ("kapat", "off", "yok"):
            await users.set_quiet(uid, None, None)
            await bot.send("✅ Sessiz saat kapatıldı.", chat)
            return
        m = re.fullmatch(r"(\d{1,2})\s*[-–:]\s*(\d{1,2})", args[0])
        if not m or not (0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 23) or m.group(1) == m.group(2):
            await bot.send("❌ Biçim: <code>/sessiz 23-08</code> (0-23 arası, başlangıç ≠ bitiş).", chat)
            return
        s, e = int(m.group(1)), int(m.group(2))
        await users.set_quiet(uid, s, e)
        await bot.send(f"✅ Sessiz saat: <b>{s:02d}–{e:02d}</b> TSİ — o aralıkta bildirim gelmez, sorgular çalışır.", chat)
        return
    kinds = await users.ensure_default_kinds(uid, cfg)
    coins = await users.get_coins(uid)
    await bot.send(notif_text(u, kinds, coins), chat, reply_markup=kinds_keyboard(cfg, kinds))


async def toggle_kind(bot, u: dict, what: str) -> tuple[set[str], str]:
    """k:<tür> | k:*on | k:*off → yeni küme ve düğme cevabı."""
    uid = int(u["id"])
    allowed = allowed_kinds(bot.cfg)
    have = await users.get_kinds(uid)
    if what == "*on":
        have, ack = set(allowed), "hepsi açık ✅"
    elif what == "*off":
        have, ack = set(), "hepsi kapalı 🔕"
    elif what in allowed:
        if what in have:
            have.discard(what)
            ack = "kapalı"
        else:
            have.add(what)
            ack = "açık ✅"
    else:
        return have, "bu tür satışta değil"
    await users.set_kinds(uid, have)
    return have, ack


def _bucket_ok(cfg, ts: int) -> bool:
    lim = max(1, int(getattr(cfg, "query_global_per_min", 40) or 40))
    while _bucket and _bucket[0] <= ts - 60:
        _bucket.popleft()
    if len(_bucket) >= lim:
        return False
    _bucket.append(ts)
    return True


async def run_query(bot, u: dict, raw: str) -> bool:
    """Coin sorgusu: çözümle → kota → önbellek/HL → sayaç → resim + altyazı."""
    cfg = bot.cfg
    chat = u["chat_id"]
    q = (raw or "").strip().lstrip("/").split()[0] if (raw or "").strip() else ""
    if not q or not _SYM_RE.match(q):
        await bot.send(unknown_text(raw or ""), chat, reply_markup=menu())
        return True
    from ..hl.universe import resolve_coin
    t = await resolve_coin(q)
    if not t:
        await bot.send(await unknown_message(cfg, q), chat, reply_markup=menu())
        return True
    ok, left, why = users.check_query(u, cfg)
    if not ok:
        await bot.send(limit_text(cfg, why), chat, reply_markup=menu() if why == "daily" else None)
        return True
    coin, sym = t["coin"], fmt.esc(t["symbol"])
    ts = now()
    ttl = int(getattr(cfg, "query_cache_sec", 60) or 0)
    c = _cache.get(coin)
    if c and ts - c[0] < ttl:
        s = c[1]
    else:
        if not _bucket_ok(cfg, ts):
            await bot.send(BUSY_TEXT, chat)
            return True
        try:
            from ..radar import cryptoliq
            s = await cryptoliq.snapshot(cfg, bot.client, coin, t.get("kind", "crypto"))
        except Exception:
            log.exception("açık sorgu hatası: %s", coin)
            await bot.send(f"❌ <b>{sym}</b> şu an okunamadı, birazdan tekrar dene.", chat)
            return True
        if len(_cache) >= CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[coin] = (ts, s)
    left = await users.consume_query(u, cfg, ts)
    foot = footer(u, cfg, left)
    # Altyazı = TAM metin: sığarsa tek foto, sığmazsa `send_snapshot` metni parçalı
    # gönderip fotoyu ⭐ altyazısıyla ekler (bkz. bot._cmd_coin_liq — aynı kural).
    full = fmt.crypto_liq_snapshot(s, extra=foot)
    await send_snapshot(bot, chat, full, s.get("png"), sym, caption=full,
                        fallback=fmt.crypto_liq_photo_caption(s))
    return True


async def send_snapshot(bot, chat: str, text: str, png: bytes | None, sym: str,
                        caption: str | None = None, fallback: str | None = None) -> None:
    """Resim + sığdırılmış altyazı (`caption`, yoksa metin) TEK mesaj; sığmazsa tam
    metin ayrı, resim kısa altyazıyla (`fallback`, yoksa genel)."""
    from .bot import _caption_fit
    cap = caption if caption is not None else text
    if png and _caption_fit(cap)[1] and await bot.send_photo(png, cap, chat):
        return
    await bot.send(text, chat)
    if png:
        await bot.send_photo(png, fallback or f"📈 <b>{sym}</b> · likidasyon grafiği", chat)
