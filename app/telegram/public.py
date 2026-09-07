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

_cache: dict[str, tuple[int, str, bytes | None]] = {}   # coin → (ts, metin, png)
_bucket: deque = deque()                                 # HL'ye giden sorgular (son 60 sn)


def kb(rows) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


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


def pro_text(cfg) -> str:
    lines = ["⭐ <b>Pro</b> — sınırsız sorgu + anlık bildirimler (tür ve coin filtresiyle)"]
    for p in plans(cfg):
        per = p["usd"] / p["months"]
        lines.append(f"• {p['months']} ay: <b>${p['usd']:.2f}</b>" + (f" (aylık ${per:.2f})" if p["months"] > 1 else ""))
    lines.append("Ödeme: ⭐ Telegram Stars · 💵 USDC (Hyperliquid'den Send) · 🪙 kripto")
    sup = getattr(cfg, "support_contact", "") or ""
    if sup:
        lines.append(f"Destek: {fmt.esc(sup)}")
    return "\n".join(lines)


# ---------------- akış ----------------

async def handle(bot, upd: dict) -> bool:
    """message / callback_query → cevap. Dönüş: bu akış işledi mi."""
    cq = upd.get("callback_query")
    if cq:
        return await _callback(bot, cq)
    msg = upd.get("message") or {}
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return False
    frm = msg.get("from") or {}
    if not frm.get("id") or frm.get("is_bot"):
        return False
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


async def _callback(bot, cq: dict) -> bool:
    frm = cq.get("from") or {}
    msg = cq.get("message") or {}
    chat = msg.get("chat") or {}
    data = str(cq.get("data") or "")
    try:
        await bot.answer_callback(cq.get("id"))
    except Exception:
        log.debug("answerCallbackQuery", exc_info=True)
    if not frm.get("id") or chat.get("type") != "private":
        return False
    if not users.flood_ok(int(frm["id"])):
        return True
    u = await users.upsert_from_update(frm, str(chat.get("id")))
    if data.startswith("q:"):
        return await run_query(bot, u, data[2:])
    if data.startswith("m:"):
        return await command(bot, u, {"help": "yardim", "pro": "pro", "notif": "bildirimler"}.get(data[2:], "yardim"), [])
    return await command(bot, u, "yardim", [])


async def command(bot, u: dict, cmd: str, args: list[str]) -> bool:
    cfg = bot.cfg
    chat = u["chat_id"]
    if cmd == "start":
        await bot.send(welcome(cfg), chat, reply_markup=menu())
    elif cmd in ("help", "yardim", "yardım"):
        await bot.send(help_text(cfg), chat, reply_markup=menu())
    elif cmd in ("hesap", "account"):
        await bot.send(account_text(u, cfg), chat)
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
    """Paketler + ödeme yolları (ödeme akışı S2'de bu fonksiyonu genişletir)."""
    await bot.send(pro_text(bot.cfg), u["chat_id"])


async def notif_menu(bot, u: dict, cmd: str = "bildirimler", args: list[str] | None = None) -> None:
    """Abonelik menüsü (S3'te tür/coin seçimi). Şimdilik katmanı söyler."""
    if users.is_pro(u):
        await bot.send("🔔 Bildirim türü seçimi bir sonraki sürümde açılıyor; şimdilik tüm Pro "
                       "bildirimleri kapalı, sorgular sınırsız.", u["chat_id"])
    else:
        await bot.send("🔔 Anlık bildirimler Pro'da. ⭐ /pro", u["chat_id"], reply_markup=menu())


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
        await bot.send(unknown_text(q), chat, reply_markup=menu())
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
        text, png = c[1], c[2]
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
        text, png = fmt.crypto_liq_snapshot(s), s.get("png")
        if len(_cache) >= CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[coin] = (ts, text, png)
    left = await users.consume_query(u, cfg, ts)
    await send_snapshot(bot, chat, text + "\n" + footer(u, cfg, left), png, sym)
    return True


async def send_snapshot(bot, chat: str, text: str, png: bytes | None, sym: str) -> None:
    """Resim + tam metin tek mesaj; sığmazsa metin ayrı, resim kısa altyazıyla."""
    from .bot import _caption_fit
    if png and _caption_fit(text)[1] and await bot.send_photo(png, text, chat):
        return
    await bot.send(text, chat)
    if png:
        await bot.send_photo(png, f"📈 <b>{sym}</b> · likidasyon grafiği", chat)
