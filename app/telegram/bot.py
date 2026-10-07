"""Telegram bot — long polling + komutlar + mesaj gönderimi."""
import asyncio
import json
import logging
import re
import time
from collections import deque

import aiohttp

from .. import assets
from ..config import Config
from ..db import db, now
from ..db import kv_get
from ..earnings import calendar as cal
from ..earnings.calendar import upcoming_events
from ..hl.client import HLClient
from ..hl.universe import find_ticker
from . import format as fmt
from .format import tr_time

log = logging.getLogger("telegram.bot")

MAX_LEN = 4000
SEND_PER_SEC = 20                  # Telegram küresel sınırı 30 msg/sn; pay bırak
# Herkese açık DM akışı açıkken menü düğmesinde görünen komutlar (BotFather listesi)
PUBLIC_COMMANDS = [("start", "Başla / menü"), ("pro", "Pro abonelik ve ödeme"),
                   ("bildirimler", "Bildirim türlerini seç"), ("hesap", "Hesabım: katman, kota, adres"),
                   ("adres", "HL adresini kaydet (USDC ödemesi eşleşir)"), ("yardim", "Yardım")]


def _cmd_lower(s: str) -> str:
    """Komut adını locale-güvenli küçült. Türkçe büyük İ'nin str.lower()'ı
    'i̇' (i + U+0307 combining dot above) üretir; '/TAKİP_1' → 'taki̇p_1' hiçbir
    komuta düşmüyordu. Combining dot'u sil — ı/ğ/ş gibi diğer harfler korunur
    (mevcut 'sağlık', 'bırak_' komutları bozulmasın)."""
    return s.lower().replace("̇", "")


CAPTION_LIMIT = fmt.CAPTION_VISIBLE   # Telegram: ayrıştırılmış altyazı, UTF-16 birim
# Yalnız GERÇEK etiketler: "</?harf…>". Eski `<[^>]+>` metindeki kaçışsız bir '<'ten
# ("2 toz pozisyon (< $1K)") sonraki '>'ye kadar her şeyi yutuyordu — /pump metni
# "toz pozisyon (" diye kesik geliyordu. Tanım format.py'de (bot'u import edemez).
TAG_RE, strip_tags = fmt.TAG_RE, fmt.strip_tags


def _caption_fit(caption: str, limit: int = CAPTION_LIMIT,
                 force_plain: bool = False) -> tuple[str, bool]:
    """(altyazı, HTML mi). Sınır GÖRÜNÜR metin üzerinden: etiketler sayılmaz.
    Sığıyorsa dokunma (etiketli metni ortadan kesmek HTML'i bozar → 400);
    sığmıyorsa etiketler sıyrılır, varlıklar çözülür, kesilir + '…'."""
    import html as _html
    if not caption:
        return "", False
    visible = strip_tags(caption)
    if not force_plain and len(visible.encode("utf-16-le")) // 2 <= limit:
        return caption, True
    plain = _html.unescape(visible)
    if len(plain.encode("utf-16-le")) // 2 > limit:
        plain = plain[:limit - 24].rstrip() + "…"
    return plain, False


class TelegramBot:
    def __init__(self, cfg: Config, session: aiohttp.ClientSession,
                 client: HLClient, state: dict):
        self.cfg = cfg
        self.session = session
        self.client = client
        self.state = state
        self.api = f"https://api.telegram.org/bot{cfg.telegram_bot_token}"
        self._sent_ts: deque = deque()             # son 1 sn'deki gönderimler (küresel hız)
        self._chat_last: dict[str, float] = {}     # sohbet başına son gönderim (1 msg/sn)
        self.blocked_chats: set[str] = set()       # 403 / silinmiş hesap görülen sohbetler
        self.on_blocked = self._default_on_blocked
        self.username = getattr(cfg, "bot_username", "") or ""   # /bot sayfası ve tanıtım linki (getMe ile dolar)
        self._admin_cache: dict[tuple[str, str], tuple[float, bool]] = {}   # (kanal, kullanıcı) → yönetici mi

    # ---------- gönderim ----------

    async def send(self, text: str, chat_id: str | None = None,
                   reply_markup: dict | None = None) -> bool:
        """HTML metin; `reply_markup` (inline klavye) son parçaya takılır."""
        chat = chat_id or self.cfg.telegram_chat_id
        if not chat:
            log.info("TELEGRAM_CHAT_ID yok, mesaj atlandı:\n%s", text[:200])
            return False
        ok = True
        chunks = self._split(text)
        for i, chunk in enumerate(chunks):
            await self._pace(chat)
            if not await self._send_chunk(chat, chunk, reply_markup if i == len(chunks) - 1 else None):
                ok = False
        return ok

    async def send_pre(self, text: str, chat_id: str | None = None) -> bool:
        """Ham (HTML'siz) uzun metni <pre> içinde gönder — HER parça kendi
        <pre>…</pre>'si ile. Eskiden yalnız ilk parça açıp son parça kapatıyordu:
        4000'i aşan /tani'nin ilk ve son parçası HTML hatası alıp düz metne
        düşüyordu. Parçalama KAÇIŞLI metin üzerinden (varlıklar uzatır)."""
        ok = True
        for chunk in self._split(fmt.esc(text), MAX_LEN - len("<pre></pre>")):
            if not await self.send(f"<pre>{chunk}</pre>", chat_id):
                ok = False
        return ok

    async def send_photo(self, png: bytes, caption: str = "",
                         chat_id: str | None = None, reply_markup: dict | None = None) -> bool:
        """Resim (PNG bayt) + HTML altyazı — Telegram sendPhoto, TEK mesaj.

        Altyazı sınırı 1024 görünür karakter (etiketler sayılmaz); `_caption_fit`
        sığdırır. Çağıran genelde tam alarm metnini altyazı yapar (birleşik
        mesaj); sığmazsa metni ayrı yollar, burası yalnız kısa altyazı alır."""
        chat = chat_id or self.cfg.telegram_chat_id
        if not chat or not png:
            return False
        cap, is_html = _caption_fit(caption)
        await self._pace(chat)
        return await self._send_photo_once(chat, png, cap, is_html, reply_markup=reply_markup)

    async def _send_photo_once(self, chat: str, png: bytes, cap: str, is_html: bool,
                               _retry: bool = True, reply_markup: dict | None = None) -> bool:
        form = aiohttp.FormData()              # her denemede yeni: FormData tek kullanımlık
        form.add_field("chat_id", str(chat))
        if cap:
            form.add_field("caption", cap)
            if is_html:
                form.add_field("parse_mode", "HTML")
        if reply_markup:
            form.add_field("reply_markup", json.dumps(reply_markup))
        form.add_field("photo", png, filename="chart.png", content_type="image/png")
        try:
            async with self.session.post(
                f"{self.api}/sendPhoto", data=form,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                if r.status == 200:
                    return True
                body = (await r.text())[:300]
                log.warning("sendPhoto %s: %s", r.status, body)
                await self._note_error(chat, r.status, body)
                if r.status == 429 and _retry:
                    try:
                        wait = int(((await r.json()).get("parameters") or {})
                                   .get("retry_after") or 1)
                    except Exception:
                        wait = 1
                    await asyncio.sleep(min(wait, 30))
                    return await self._send_photo_once(chat, png, cap, is_html, _retry=False,
                                                       reply_markup=reply_markup)
                if r.status == 400 and is_html and _retry and not self.blocked_reason(400, body):
                    # HTML reddedildi: etiketsiz düz altyazıyla bir kez daha
                    plain, _ = _caption_fit(strip_tags(cap), force_plain=True)
                    return await self._send_photo_once(chat, png, plain, False, _retry=False,
                                                       reply_markup=reply_markup)
                return False
        except Exception as e:
            log.warning("sendPhoto hatası: %s", e)
            return False

    async def _send_chunk(self, chat: str, chunk: str, reply_markup: dict | None = None,
                          _retry: bool = True) -> bool:
        payload = {"chat_id": chat, "text": chunk, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            async with self.session.post(
                f"{self.api}/sendMessage", json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status == 200:
                    return True
                body = (await r.text())[:300]
                log.warning("sendMessage %s: %s", r.status, body)
                await self._note_error(chat, r.status, body)
                # 429: Telegram retry_after kadar bekleyip bir kez daha dene
                if r.status == 429 and _retry:
                    try:
                        wait = int(((await r.json()).get("parameters") or {})
                                   .get("retry_after") or 1)
                    except Exception:
                        wait = 1
                    await asyncio.sleep(min(wait, 30))
                    return await self._send_chunk(chat, chunk, reply_markup, _retry=False)
                # 400 "can't parse entities": HTML bozuksa parse_mode'suz düz metin
                # gönder — bildirim tamamen kaybolmasın (escape kaçağı son çare)
                if r.status == 400 and _retry and not self.blocked_reason(400, body):
                    return await self._send_plain(chat, chunk, reply_markup)
                return False
        except Exception as e:
            log.warning("sendMessage hatası: %s", e)
            return False

    async def _send_plain(self, chat: str, chunk: str, reply_markup: dict | None = None) -> bool:
        import html as _html
        plain = _html.unescape(strip_tags(chunk))  # etiketleri sıyır, &lt; → <
        payload = {"chat_id": chat, "text": plain, "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            async with self.session.post(
                f"{self.api}/sendMessage", json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status == 200:
                    log.info("mesaj düz-metin olarak gönderildi (HTML reddedildi)")
                    return True
                log.warning("düz-metin sendMessage de başarısız: %s", r.status)
                return False
        except Exception as e:
            log.warning("düz-metin sendMessage hatası: %s", e)
            return False

    @staticmethod
    def _split(text: str, limit: int = MAX_LEN) -> list[str]:
        """Satır sınırlarından parçala; tek satır sınırı aşarsa SERT kes (eskiden
        4000'lik parça sınırsız büyüyebiliyor, Telegram 'message is too long' diyordu)."""
        if len(text) <= limit:
            return [text]
        parts, cur = [], ""
        for line in text.split("\n"):
            while len(line) > limit:
                if cur:
                    parts.append(cur)
                    cur = ""
                parts.append(line[:limit])
                line = line[limit:]
            if len(cur) + len(line) + 1 > limit:
                if cur:
                    parts.append(cur)
                cur = line
            else:
                cur = f"{cur}\n{line}" if cur else line
        if cur:
            parts.append(cur)
        return parts

    # ---------- hız + engel + genel Bot API ----------

    async def _pace(self, chat: str) -> None:
        """Küresel SEND_PER_SEC msg/sn + sahip dışı sohbetlerde sohbet başına 1 msg/sn
        (Telegram sınırları). Sahibin tek tük alarmında hissedilmez; fan-out ve
        duyuru kuyruğunu düzenler. 429 gelirse ayrıca retry_after beklenir."""
        own = chat == (self.cfg.telegram_chat_id or "") or chat in self._own_chats()
        while True:
            ts = time.monotonic()
            while self._sent_ts and self._sent_ts[0] <= ts - 1.0:
                self._sent_ts.popleft()
            wait = 0.0
            if not own:
                wait = max(0.0, self._chat_last.get(str(chat), 0.0) + 1.0 - ts)
            if len(self._sent_ts) >= SEND_PER_SEC:
                wait = max(wait, self._sent_ts[0] + 1.0 - ts)
            if wait <= 0:
                break
            await asyncio.sleep(min(wait, 1.0))
        t = time.monotonic()
        self._sent_ts.append(t)
        self._chat_last[str(chat)] = t

    @staticmethod
    def blocked_reason(status: int, body: str) -> str:
        """Telegram hata gövdesinden 'bu sohbete bir daha yazma' kararı:
        403 (bot engellendi / kullanıcı hesabı silindi) → 'blocked';
        400 'chat not found' / 'user is deactivated' / 'bot was kicked' → 'gone'."""
        b = (body or "").lower()
        if status == 403:
            return "blocked"
        if status == 400 and any(k in b for k in ("chat not found", "user is deactivated", "bot was kicked",
                                                  "bot was blocked")):
            return "gone"
        return ""

    async def _note_error(self, chat: str, status: int, body: str) -> None:
        why = self.blocked_reason(status, body)
        if not why:
            return
        self.blocked_chats.add(str(chat))
        if self.on_blocked:
            try:
                await self.on_blocked(str(chat), why)
            except Exception:
                log.debug("engel kancası", exc_info=True)

    async def _default_on_blocked(self, chat: str, why: str) -> None:
        """Herkese açık kullanıcı botu engelledi / hesabı silindi → users.blocked_ts
        (fan-out, hatırlatma ve duyurudan düşer). Sahibin sohbetleri dokunulmaz."""
        if chat in self._own_chats():
            return
        from .. import users
        n = await users.mark_blocked(chat)
        if n:
            log.info("kullanıcı erişilemez (%s): %s", why, chat)

    async def call(self, method: str, payload: dict, timeout: int = 30) -> tuple[int, dict]:
        """Genel Bot API çağrısı (JSON). Dönüş (HTTP durumu, gövde); ağ hatası → (0, {...})."""
        if self.session is None:
            return 0, {"ok": False, "description": "oturum yok"}
        try:
            async with self.session.post(f"{self.api}/{method}", json=payload,
                                         timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                try:
                    data = await r.json()
                except Exception:
                    data = {"ok": False, "description": (await r.text())[:300]}
                if r.status != 200:
                    log.warning("%s %s: %s", method, r.status, str(data.get("description"))[:200])
                return r.status, data
        except Exception as e:
            log.warning("%s hatası: %s", method, e)
            return 0, {"ok": False, "description": str(e)}

    async def answer_callback(self, cq_id, text: str = "", alert: bool = False) -> bool:
        payload = {"callback_query_id": cq_id}
        if text:
            payload.update({"text": text[:200], "show_alert": bool(alert)})
        st, data = await self.call("answerCallbackQuery", payload, timeout=10)
        return st == 200 and bool(data.get("ok"))

    async def edit_reply_markup(self, chat: str, message_id, reply_markup: dict | None) -> bool:
        st, data = await self.call("editMessageReplyMarkup",
                                   {"chat_id": chat, "message_id": message_id,
                                    "reply_markup": reply_markup or {"inline_keyboard": []}}, timeout=10)
        return st == 200 and bool(data.get("ok"))

    async def set_my_commands(self, commands: list[tuple[str, str]], scope: dict | None = None) -> bool:
        payload = {"commands": [{"command": c, "description": d[:256]} for c, d in commands]}
        if scope:
            payload["scope"] = scope
        st, data = await self.call("setMyCommands", payload, timeout=10)
        return st == 200 and bool(data.get("ok"))

    def _public_on(self) -> bool:
        return bool(getattr(self.cfg, "public_bot_enabled", False))

    async def _setup_commands(self) -> None:
        """Açık DM akışı açıksa herkese görünen komut listesi (menü düğmesi); getMe ile kullanıcı adı."""
        if self.session is None:
            return
        try:
            st, data = await self.call("getMe", {}, timeout=10)
            if st == 200 and data.get("ok"):
                self.username = (data.get("result") or {}).get("username") or self.username
        except Exception:
            log.debug("getMe", exc_info=True)
        if not self._public_on():
            return
        try:
            await self.set_my_commands(PUBLIC_COMMANDS, {"type": "all_private_chats"})
        except Exception:
            log.debug("setMyCommands", exc_info=True)

    # ---------- polling ----------

    async def run_polling(self) -> None:
        offset = None
        log.info("Telegram polling başladı")
        err_wait = 5
        await self._setup_commands()
        while True:
            try:
                params = {"timeout": 50}
                if offset is not None:
                    params["offset"] = offset
                async with self.session.get(
                    f"{self.api}/getUpdates", params=params,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as r:
                    status = r.status
                    data = await r.json()
                # ok:false / HTTP!=200 → beat ATMA (bekçi 'sağlıklı' sanmasın) +
                # artan bekleme. 409 (ikinci instance) / 401 (kötü token) JSON
                # döndüğü için exception atmıyordu → sleep'siz sıcak döngü olurdu.
                if status != 200 or not data.get("ok", True):
                    desc = data.get("description") or f"HTTP {status}"
                    log.warning("getUpdates reddedildi: %s — %ds bekle", desc, err_wait)
                    await asyncio.sleep(err_wait)
                    err_wait = min(err_wait * 2, 120)
                    continue
                err_wait = 5
                from ..health import beat
                await beat("telegram")
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    try:
                        await self._handle_update(upd)
                    except Exception:
                        log.exception("komut işlenemedi")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("polling hatası: %s", e)
                await asyncio.sleep(err_wait)
                err_wait = min(err_wait * 2, 120)

    async def _handle_update(self, upd: dict) -> None:
        # 🛑 takip tuşu (sahibin sohbetleri) — herkese açık akıştan ÖNCE, kendi öneki var
        cq = upd.get("callback_query")
        if cq and str(cq.get("data") or "").startswith(fmt.TRACK_CB + ":"):
            await self._track_callback(cq)
            return
        if cq and str(cq.get("data") or "").startswith(fmt.WAKE_CB + ":"):
            await self._wake_callback(cq)                # 🚨 uyandırma: ✅ Uyandım / 🗑 alarm
            return
        if cq and str(cq.get("data") or "").startswith(fmt.MOVEWIN_CB + ":"):
            await self._movewin_callback(cq)             # ⏱ /5dk: 📊 Şu ana kadar
            return
        # Diğer düğmeler (callback) ve ödeme olayları yalnız herkese açık DM akışında anlamlı
        if "callback_query" in upd or "pre_checkout_query" in upd:
            if self._public_on():
                from . import public
                await public.handle(self, upd)
            return
        msg = upd.get("message") or upd.get("channel_post") or {}
        text = (msg.get("text") or "").strip()
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if not chat_id:
            return
        if msg.get("successful_payment") and self._public_on():
            from . import public                   # Stars tahsilatı (sahip kendi hesabıyla da deneyebilir)
            await public.handle(self, upd)
            return
        # Yetki: sahip = TELEGRAM_CHAT_ID sohbeti YA DA sahibin kullanıcı id'siyle
        # yazan kişi (kendi kanalında /tani "bir şey olmuyor"du: sahiplik yalnız chat
        # id'ye bakıyordu). BOŞSA KİMSE sahip değil. Kendi kanallarında sahip-dışı
        # sınırlı komut; diğer herkes → herkese açık DM akışı (bayrak açıksa) ya da
        # yalnız chat id.
        is_owner = self._is_owner(chat_id, msg)
        if not is_owner and chat_id not in self._own_chats():
            if self._public_on() and chat.get("type") == "private":
                from . import public
                await public.handle(self, upd)
                return
            if text.startswith("/"):
                cmd = _cmd_lower(text.split()[0][1:].split("@")[0])
                if cmd in ("start", "id"):
                    await self.send(f"Bu sohbetin chat id'si: <code>{chat_id}</code>", chat_id)
            return
        if not text.startswith("/"):
            return
        parts = text.split()
        cmd = _cmd_lower(parts[0][1:].split("@")[0])
        args = parts[1:]

        # Botun kendi kanalları (kripto, hisse hacim, liq attack, örüntü, yayın, SİM):
        # takip komutları, /sim ve /hype gibi coin görüntüsü — sahip zinciri DEĞİL.
        if not is_owner:
            if await self._dispatch_track(cmd, args, chat_id):
                return                     # kanaldan /takip_N, /birak_N, /takipler
            if cmd in ("sim", "sım"):
                await self._cmd_sim(chat_id)   # coin çözümlemesine düşmesin
            elif cmd in ("start", "id"):
                await self.send(f"Bu sohbetin chat id'si: <code>{chat_id}</code>", chat_id)
            elif not args:
                await self._cmd_coin_liq(cmd, chat_id)
            return

        if cmd in ("start", "help"):
            await self.send(fmt.help_text() + f"\n\nChat id: <code>{chat_id}</code>", chat_id)
        elif cmd == "id":
            await self.send(f"Chat id: <code>{chat_id}</code>", chat_id)
        elif cmd == "upcoming":
            await self.send(fmt.upcoming_list(await upcoming_events(14)), chat_id)
        elif cmd == "refresh":
            await self.send("🔄 Takvim üç kaynaktan yenileniyor (yahoo+finnhub+nasdaq)…", chat_id)
            try:
                n = await cal.refresh_calendar(self.cfg, self.session)
                stats = await kv_get("calendar_stats") or {}
                srcs = " · ".join(
                    f"{k}: {stats.get(k, 0)}"
                    for k in ("tradingview", "yahoo", "nasdaq", "finnhub"))
                warn = ""
                if not stats.get("tradingview"):
                    warn = ("\n⚠️ TradingView'dan veri gelmedi — tam saatler eksik kalabilir."
                            " Yanlış gördüğün saati <code>/settime SEMBOL bmo</code> ile düzelt.")
                await self.send(
                    f"✅ Takvim yenilendi: <b>{n}</b> HL-eşleşen earnings\n"
                    f"Kaynaklar → {srcs}{warn}\n\n"
                    + fmt.upcoming_list(await upcoming_events(14)), chat_id)
            except Exception as e:
                await self.send(f"❌ Takvim yenilenemedi: {fmt.esc(e)}", chat_id)
        elif cmd == "scan":
            await self._cmd_scan(args, chat_id)
        elif cmd == "whale":
            await self._cmd_whale(args, chat_id)
        elif cmd == "twap":
            await self._cmd_twap(args, chat_id)
        elif cmd == "watch":
            await self._cmd_watch(args, chat_id, add=True)
        elif cmd == "unwatch":
            await self._cmd_watch(args, chat_id, add=False)
        elif cmd == "ignore":
            await self._cmd_ignore(args, chat_id, on=True)
        elif cmd == "unignore":
            await self._cmd_ignore(args, chat_id, on=False)
        elif cmd in ("forget", "unut"):
            await self._cmd_forget(args, chat_id)
        elif cmd == "watchlist":
            await self._cmd_watchlist(chat_id)
        elif cmd.startswith("takip_"):
            await self._cmd_track_start(cmd, chat_id)
        elif cmd in ("twaptakipler", "twaptakip"):
            await self._cmd_twap_follow_list(chat_id)
        elif cmd in ("duvartakipler", "duvartakip"):
            await self._cmd_sticky_follow_list(chat_id)
        elif cmd in ("hesaplar", "hesap"):
            await self._cmd_accounts(chat_id)
        elif cmd in ("balina", "dilim", "dilimli", "dilimler"):
            await self._cmd_slices(chat_id)
        elif cmd in ("seans", "seanslar"):
            await self._cmd_seans(args, chat_id)
        elif cmd in ("acilis", "açılış", "acılıs"):
            await self._cmd_open_movers(chat_id)
        elif cmd in ("alarm", "alarmlar", "alarm_test", "alarmtest", "uyandim", "uyandım"):
            await self._cmd_alarm(cmd, args, chat_id)   # 🚨 yalnız sahip: gece telefonu çaldırır
        elif cmd in ("sim", "sım", "simulasyon", "simülasyon"):
            await self._cmd_sim(chat_id)
        elif cmd in ("takipler", "takip", "trackers"):
            if cmd == "takip" and args:      # /takip 0xADRES SEMBOL → manuel takip
                await self._cmd_track_manual(args, chat_id)
            else:
                await self._cmd_track_list(chat_id)
        elif cmd.startswith("birak_") or cmd.startswith("bırak_"):
            await self._cmd_track_stop(cmd, chat_id)
        elif cmd in ("bildirimler", "notifications", "notif"):
            await self._cmd_notifications(chat_id)
        elif cmd in ("settime", "saat"):
            await self._cmd_settime(args, chat_id)
        elif cmd in ("gecmis", "history", "arsiv"):
            async with db() as conn:
                cur = await conn.execute(
                    """SELECT symbol, date_et, hour_hint, move_pct, result_note
                       FROM earnings_events WHERE evaluated=1 AND result_note IS NOT NULL
                       ORDER BY date_et DESC LIMIT 12""")
                rows = [dict(r) for r in await cur.fetchall()]
            await self.send(fmt.history_list(rows), chat_id)
        elif cmd in ("winners", "kazananlar"):
            async with db() as conn:
                cur = await conn.execute(
                    """SELECT address, hits, misses, watchlist FROM addresses
                       WHERE hits > 0 AND COALESCE(entity,'')=''
                       ORDER BY hits DESC, misses ASC LIMIT 15""")
                rows = [dict(r) for r in await cur.fetchall()]
            await self.send(fmt.winners_list(rows), chat_id)
        elif cmd == "status":
            await self._cmd_status(chat_id)
        elif cmd in ("saglik", "sağlık", "health"):
            from ..health import snapshot
            await self.send(fmt.health_report(await snapshot(self.cfg)), chat_id)
        elif cmd in ("tani", "tanı", "diag"):
            # Tam sistem dökümü. Uzun — send_pre parça parça, her parça kendi <pre>'si ile
            # (kopyalanınca hizalama bozulmasın).
            # `self` durum nesnesi olarak geçiyor: bot.collector main'de atanıyor,
            # diag yalnız `.collector` özniteliğine bakıyor.
            from ..diag import report
            txt = await report(self.cfg, self)
            await self.send_pre(txt, chat_id)
        elif cmd in ("devler", "big", "biggest"):
            from ..radar import bigpos
            await self.send(
                fmt.big_positions(await bigpos.live_big(15),
                                  await bigpos.stats(self.cfg),
                                  bigpos.tiers(self.cfg)), chat_id)
        elif await self._cmd_movewin(cmd, args, chat_id):
            pass                                  # ⏱ /5dk, /15dk, /5dk 15:30
        elif await self._admin(cmd, args, chat_id):
            pass                                  # /kullanicilar, /odemeler, /pro_ver, /duyuru, /iade
        elif not args and await self._cmd_coin_liq(cmd, chat_id):
            pass                                  # /hype, /pump, /sndk → liq görüntüsü
        else:
            # Eskiden bilinmeyen komut SESSİZCE yutuluyordu: yazdığın şey
            # cevapsız kalınca bot ölü mü, komut mu yok anlaşılmıyordu.
            await self.send(
                f"❓ <code>/{fmt.esc(cmd)}</code> diye bir komut yok.\n\n"
                + fmt.help_text(), chat_id)

    # ---------- 🛑 takip tuşu ----------

    async def _ack(self, cq_id, text: str = "") -> None:
        try:
            await self.answer_callback(cq_id, text)
        except Exception:
            log.debug("answerCallbackQuery", exc_info=True)

    async def _chat_admin(self, chat_id: str, uid: str) -> bool:
        """Kanal yöneticisi mi (getChatMember) — başarılı okuma 10 dk önbellekte; okunamazsa hayır."""
        key, t = (chat_id, uid), time.monotonic()
        hit = self._admin_cache.get(key)
        if hit and t - hit[0] < 600:
            return hit[1]
        if not uid.lstrip("-").isdigit():
            return False
        st, data = await self.call("getChatMember", {"chat_id": chat_id, "user_id": int(uid)}, timeout=10)
        if st != 200 or not data.get("ok"):
            return False
        ok = (data.get("result") or {}).get("status") in ("creator", "administrator")
        self._admin_cache[key] = (t, ok)
        return ok

    def _owner_press(self, chat: dict, frm: dict) -> bool:
        """Sahibe özel tuşlar (uyandırma: gece telefonu çaldırır / susturur): sahibin kullanıcı
        id'si, ya da ana sohbet (kanal değilse — kanalda tuşa her abone basabilir)."""
        uid, oid = str(frm.get("id") or ""), self._owner_user_id()
        if oid and uid == oid:
            return True
        return chat.get("type") != "channel" and bool(self.cfg.telegram_chat_id) \
            and str(chat.get("id") or "") == str(self.cfg.telegram_chat_id)

    async def _may_press(self, chat: dict, frm: dict) -> bool:
        """Tuşa kim basabilir — komutlarla aynı kural, kanalda daha sıkı: kanalda tuşa her abone
        basabilir (komutu ise yalnız yönetici yazabilir) → sahip id'si ya da kanal yöneticisi.
        Özel sohbet / grup: botun kendi sohbetleri (grupta üyeler /birak_N'i zaten yazabiliyor)."""
        chat_id, uid = str(chat.get("id") or ""), str(frm.get("id") or "")
        if not chat_id or not uid or frm.get("is_bot"):
            return False
        oid = self._owner_user_id()
        if oid and uid == oid:
            return True
        if chat_id not in self._own_chats():
            return False
        if chat.get("type") == "channel":
            return await self._chat_admin(chat_id, uid)
        return True

    async def _track_callback(self, cq: dict) -> None:
        """🛑 tek dokunuş bırakır, ↩️ geri alır. Klavye DURUMDAN çizilir (basılan tuştan değil):
        aynı takibin eski mesajlarındaki tuşlar da basılınca doğru hâle döner; biten takipte kalkar."""
        from ..radar import trackctl
        cq_id = cq.get("id")
        msg = cq.get("message") or {}
        chat = msg.get("chat") or {}
        parts = str(cq.get("data") or "").split(":")
        if len(parts) != 4 or parts[1] not in ("stop", "undo", "wake", "nowake") \
                or parts[2] not in trackctl.KINDS or not parts[3].isdigit() \
                or (parts[1] in ("wake", "nowake") and parts[2] != "pos"):
            await self._ack(cq_id)
            return
        _, act, kind, fid = parts[0], parts[1], parts[2], int(parts[3])
        frm = cq.get("from") or {}
        if act in ("wake", "nowake"):
            # ⏰ gece telefonu çaldırır: grupta başkası açıp kapatamasın — yalnız sahip
            if not self._owner_press(chat, frm):
                await self._ack(cq_id, "⏰ Uyandırmayı yalnız sahibi açıp kapatabilir")
                return
        elif not await self._may_press(chat, frm):
            await self._ack(cq_id, "Bu tuşu yalnız sahibi ya da kanal yöneticisi kullanabilir")
            return
        if act in ("wake", "nowake"):
            from ..radar import wake
            done = await trackctl.set_wake(fid, act == "wake")
            st = await trackctl.state(kind, fid)
            label = (st or {}).get("label") or f"#{fid}"
            if not done:
                note = f"{label} açık değil — uyandırma ayarlanamadı"
            elif act == "nowake":
                note = f"🔕 {label}: uyandırma kapandı (bildirimler sürer)"
            elif wake.tg_user(self.cfg):
                note = f"⏰ {label}: balina kapatırsa / yön değiştirirse seni Telegram'dan arayacağım"
            else:
                note = f"⏰ {label}: açık ama WAKE_TELEGRAM_USER yok — yalnız mesaj gelir (/alarm)"
        elif act == "stop":
            done = await trackctl.stop(kind, fid)
            st = await trackctl.state(kind, fid)
            label = (st or {}).get("label") or f"#{fid}"
            if done:
                note = f"🛑 {label} bırakıldı — yanlışlıkla mı? ↩️ Geri al"
            elif st is None:
                note = f"#{fid} bulunamadı"
            elif st["active"]:
                note = f"{label} hâlâ açık — yeniden dene"
            elif st.get("end_note") == trackctl.MANUAL:
                note = f"{label} zaten bırakılmış"
            else:
                note = f"{label} bitmiş: {st.get('end_note') or 'kapandı'}"
        else:
            ok, why = await trackctl.resume(kind, fid)
            st = await trackctl.state(kind, fid)
            label = (st or {}).get("label") or f"#{fid}"
            note = f"↩️ {label} yeniden açık — kaldığı yerden sürüyor" if ok else f"{label}: {why}"
        if st and st["active"]:
            kb = fmt.stop_kb(kind, fid, wake=bool(st.get("wake")))
        elif st and st.get("end_note") == trackctl.MANUAL:
            kb = fmt.undo_kb(kind, fid)
        else:
            kb = None                                    # takip bitti → tuş kalkar
        await self._ack(cq_id, note)
        cur = msg.get("reply_markup") or {}
        cur = cur if cur.get("inline_keyboard") else None
        if kb != cur and msg.get("message_id") and chat.get("id") is not None:
            try:
                await self.edit_reply_markup(str(chat["id"]), msg["message_id"], kb)
            except Exception:
                log.debug("takip tuşu güncellenemedi", exc_info=True)

    # ---------- 🚨 uyandırma alarmı ----------

    async def _cmd_alarm(self, cmd: str, args: list[str], chat_id: str) -> None:
        """/alarm [SEMBOL seviye [seviye] | SEMBOL %X | 0xADRES [%X]], /alarmlar, /alarm_test,
        /uyandim — yalnız SAHİP zincirinde (gece telefonu çaldırır)."""
        from ..radar import wake
        user, pl = wake.tg_user(self.cfg), wake.plan(self.cfg)
        if cmd in ("uyandim", "uyandım"):
            acked = await wake.ack(None, "komut")
            for ev in acked:
                await self._wake_mark(ev)
            await self.send(f"✅ Aramalar durdu ({len(acked)} uyandırma)." if acked
                            else "Açık uyandırma yok.", chat_id)
            return
        if cmd in ("alarm_test", "alarmtest"):
            eid = await wake.fire(self.cfg, "🧪 Deneme araması",
                                  "🧪 <b>Deneme araması</b> — Rahatsız Etme açıkken telefonun çalıyor mu?",
                                  "Bu bir deneme araması. Telefonun Rahatsız Etme açıkken çaldıysa kurulum tamam."
                                  " Telegram'da uyandım tuşuna bas.", f"test:{now()}", source="test",
                                  chat_id=chat_id)
            await self.send("🧪 Deneme başladı — birkaç saniye içinde mesaj ve Telegram araması gelir"
                            f" (2 arama, ~{pl['gap']} sn arayla)." if eid and user else
                            "🧪 Deneme mesajı geliyor — ama arama YOK:\n" + fmt._wake_call_line(user, pl), chat_id)
            return
        if cmd == "alarmlar" or not args:
            alarms = await wake.active_alarms()
            if cmd == "alarm" and not args and not alarms:
                await self.send(fmt.wake_help(user, pl), chat_id)
                return
            prices: dict = {}
            from ..radar.report import coin_dex
            for dex in {coin_dex(a["coin"]) for a in alarms if a.get("coin")}:
                try:
                    prices.update(await wake.mids(self.client, dex))
                except Exception:
                    log.debug("alarm listesi fiyatı", exc_info=True)
            text = fmt.wake_list(alarms, prices, user, pl)
            if cmd == "alarm":
                text = fmt.wake_help(user, pl) + "\n\n" + text
            await self.send(text, chat_id, reply_markup=fmt.wake_del_kb(alarms))
            return
        res = await wake.arm(self.cfg, self.client, wake.parse_args(args), chat_id=chat_id)
        if not res["ok"]:
            await self.send(f"⏰✗ Alarm kurulamadı: {fmt.esc(res['reason'])}\n\n" + fmt.wake_help(user, pl),
                            chat_id)
            return
        hours = max(1, int(getattr(self.cfg, "wake_alarm_hours", 18) or 18))
        await self.send(fmt.wake_armed(res, user, pl, hours), chat_id,
                        reply_markup=fmt.wake_del_kb([res["alarm"]]))

    async def _wake_mark(self, ev: dict) -> None:
        """Onaylanan uyandırmanın mesajındaki tuş → "✅ Uyandın 03:12"."""
        if ev.get("msg_id") and ev.get("msg_chat"):
            try:
                await self.edit_reply_markup(str(ev["msg_chat"]), ev["msg_id"], fmt.wake_acked_kb(ev))
            except Exception:
                log.debug("uyandırma tuşu güncellenemedi", exc_info=True)

    async def _wake_callback(self, cq: dict) -> None:
        """✅ Uyandım (aramaları durdurur) · 🗑 alarm kaldır · bilgi. Yalnız sahip."""
        from ..radar import wake
        cq_id = cq.get("id")
        msg = cq.get("message") or {}
        chat = msg.get("chat") or {}
        parts = str(cq.get("data") or "").split(":")
        if len(parts) != 3 or parts[1] not in ("ack", "del", "noop") or not parts[2].isdigit():
            await self._ack(cq_id)
            return
        act, xid = parts[1], int(parts[2])
        if not self._owner_press(chat, cq.get("from") or {}):
            await self._ack(cq_id, "🚨 Bu tuşu yalnız sahibi kullanabilir")
            return
        if act == "del":
            ok = await wake.cancel(xid, "kaldırıldı")
            await self._ack(cq_id, f"🗑 Alarm #{xid} kaldırıldı" if ok else f"Alarm #{xid} zaten aktif değil")
            kb = fmt.wake_del_kb([a for a in await wake.active_alarms()
                                  if any(f"{fmt.WAKE_CB}:del:{a['id']}" == b.get("callback_data")
                                         for row in (msg.get("reply_markup") or {}).get("inline_keyboard") or []
                                         for b in row)])
            if msg.get("message_id") and chat.get("id") is not None:
                try:
                    await self.edit_reply_markup(str(chat["id"]), msg["message_id"], kb)
                except Exception:
                    log.debug("alarm tuşları güncellenemedi", exc_info=True)
            return
        ev = await wake.event(xid)
        if ev is None:
            await self._ack(cq_id, "Uyandırma kaydı yok")
            return
        if act == "ack" and not ev.get("ack_ts"):
            acked = await wake.ack(xid, "tuş")
            ev = acked[0] if acked else await wake.event(xid)
            await self._ack(cq_id, "✅ Günaydın — aramalar durdu")
        else:
            await self._ack(cq_id, f"✅ Zaten onaylandı ({fmt.tr_time(int(ev['ack_ts']))})" if ev.get("ack_ts")
                            else "Henüz onaylanmadı — ✅ Uyandım'a bas")
        if ev.get("ack_ts") and msg.get("message_id") and chat.get("id") is not None:
            kb = fmt.wake_acked_kb(ev)
            if (msg.get("reply_markup") or {}) != kb:
                try:
                    await self.edit_reply_markup(str(chat["id"]), msg["message_id"], kb)
                except Exception:
                    log.debug("uyandırma tuşu güncellenemedi", exc_info=True)

    async def _admin(self, cmd: str, args: list[str], chat_id: str) -> bool:
        """Satış tarafı sahip komutları (app/telegram/admin.py)."""
        from . import admin
        return await admin.handle(self, cmd, args, chat_id)

    async def _dispatch_track(self, cmd: str, args: list[str], chat_id: str) -> bool:
        """Takip komutları (kanaldan da çalışır; haber komutun geldiği sohbete gider)."""
        if await self._cmd_movewin(cmd, args, chat_id):
            return True                    # ⏱ /5dk — coin-liq aramasına düşmesin
        if cmd.startswith("takip_"):
            await self._cmd_track_start(cmd, chat_id)
        elif cmd in ("twaptakipler", "twaptakip"):
            await self._cmd_twap_follow_list(chat_id)
        elif cmd in ("duvartakipler", "duvartakip"):
            await self._cmd_sticky_follow_list(chat_id)
        elif cmd in ("hesaplar", "hesap"):
            await self._cmd_accounts(chat_id)
        elif cmd in ("balina", "dilim", "dilimli", "dilimler"):
            await self._cmd_slices(chat_id)
        elif cmd in ("seans", "seanslar"):
            await self._cmd_seans(args, chat_id)
        elif cmd in ("acilis", "açılış", "acılıs"):
            await self._cmd_open_movers(chat_id)
        elif cmd in ("takipler", "takip", "trackers"):
            if cmd == "takip" and args:
                await self._cmd_track_manual(args, chat_id)
            else:
                await self._cmd_track_list(chat_id)
        elif cmd.startswith("birak_") or cmd.startswith("bırak_"):
            await self._cmd_track_stop(cmd, chat_id)
        else:
            return False
        return True

    async def _cmd_sim(self, chat_id: str) -> None:
        """/sim — kâğıt üstü liq simülasyonunun özeti (bakiye, açık işlem, son kapanışlar)."""
        try:
            from ..radar import sim
            await self.send(fmt.sim_summary(await sim.summary(self.cfg)), chat_id)
        except Exception as e:
            log.exception("sim özeti hatası")
            await self.send(f"❌ sim okunamadı: {fmt.esc(e)}", chat_id)

    def _track_chat(self, chat_id: str) -> str:
        """Takip haberleri nereye: ana sohbetten başlatıldıysa varsayılan (boş),
        kanaldan başlatıldıysa o kanal."""
        return "" if chat_id == (self.cfg.telegram_chat_id or "") else chat_id

    def _own_chats(self) -> set[str]:
        """Botun yazdığı tüm sohbetler (ana + kanallar) — boşlar hariç."""
        return {str(v).strip() for v in (
            self.cfg.telegram_chat_id, getattr(self.cfg, "crypto_chat_id", ""),
            getattr(self.cfg, "crypto_stocks_id", ""), getattr(self.cfg, "liq_attack_chat_id", ""),
            getattr(self.cfg, "pattern_chat_id", ""), getattr(self.cfg, "telegram_channel_id", ""),
            getattr(self.cfg, "sim_chat_id", ""), getattr(self.cfg, "account_chat_id", ""))
            if v and str(v).strip()}

    def _owner_user_id(self) -> str:
        """Sahibin KULLANICI id'si: TELEGRAM_OWNER_ID varsa o; yoksa TELEGRAM_CHAT_ID
        özel sohbetse (pozitif sayı — Telegram'da özel sohbetin id'si kullanıcının
        id'sidir) o; grup id'si (negatif) kullanıcı olamaz → boş."""
        oid = (getattr(self.cfg, "telegram_owner_id", "") or "").strip()
        if oid:
            return oid
        cid = (self.cfg.telegram_chat_id or "").strip()
        return cid if cid.isdigit() else ""

    def _is_owner(self, chat_id: str, msg: dict) -> bool:
        """Sahip DM'i ya da sahibin kullanıcı id'siyle yazan kişi (hangi sohbette
        olursa olsun — kendi kanalında /tani cevap alsın). Boş id'de kimse sahip değil."""
        if self.cfg.telegram_chat_id and chat_id == self.cfg.telegram_chat_id:
            return True
        uid = str((msg.get("from") or {}).get("id") or "")
        oid = self._owner_user_id()
        return bool(uid and oid and uid == oid)

    # ---------- komutlar ----------

    async def _cmd_coin_liq(self, cmd: str, chat_id: str) -> bool:
        """/hype, /pump, /sndk → o coinin liq'e en yakın büyük pozisyonları +
        grafik, canlı fiyat ve güncel kalan mesafeyle. Dönüş: komut bir coin
        miydi (değilse çağıran 'komut yok' der). TAM liste altyazıya sığıyorsa
        (kısa liste) resim + altyazı TEK mesaj; sığmıyorsa metin parçalı gider ve
        resim ⭐ band altyazısıyla peşinden gelir — hiçbir pozisyon gizlenmez."""
        from ..hl.universe import resolve_coin
        from ..radar import cryptoliq
        t = await resolve_coin(cmd)
        if not t:
            return False
        sym = fmt.esc(t["symbol"])
        try:
            s = await cryptoliq.snapshot(self.cfg, self.client, t["coin"], t.get("kind", "crypto"))
        except Exception as e:
            log.exception("liq görüntüsü hatası: %s", t["coin"])
            await self.send(f"❌ <b>{sym}</b> okunamadı: {fmt.esc(e)}", chat_id)
            return True
        offers: list[int] = []
        if s.get("rows"):
            try:
                from ..radar.tracker import OFFER_MAX, offer_positions
                offers = await offer_positions(t["coin"], t["symbol"], s["rows"][:OFFER_MAX])
            except Exception:
                log.debug("takip teklifi yazılamadı (%s)", t["coin"], exc_info=True)
        png = s.get("png")
        # TEK MESAJ garantisi, üç kademe: (1) tam metin altyazıya sığıyorsa o gider;
        # (2) sığmıyorsa compact sürüm — öncelik merdiveni zincir/bağlam eklerini ve
        # en uzak satırları düşürerek 1024'e indirir, düşen sayı satır sonunda yazılır;
        # (3) foto yoksa ya da compact bile sığmadıysa metin ayrı + foto. Kullanıcı
        # "foto ve mesaj ayrı geldi yine" dedi: (2) tam olarak bunun için var.
        for cap in ([fmt.crypto_liq_snapshot(s, offers=offers, compact=c) for c in (False, True)]
                    if png else []):
            if _caption_fit(cap)[1] and await self.send_photo(png, cap, chat_id):
                return True
        await self.send(fmt.crypto_liq_snapshot(s, offers=offers), chat_id)
        if png:
            await self.send_photo(png, fmt.crypto_liq_photo_caption(s), chat_id)
        return True

    async def _cmd_status(self, chat_id: str) -> None:
        async with db() as conn:
            cur = await conn.execute("SELECT COUNT(*) c FROM tickers")
            n_tick = (await cur.fetchone())["c"]
            cur = await conn.execute("SELECT COUNT(*) c FROM addresses")
            n_addr = (await cur.fetchone())["c"]
            cur = await conn.execute("SELECT COUNT(*) c FROM addresses WHERE watchlist=1")
            n_watch = (await cur.fetchone())["c"]
        st = dict(self.state)
        coll = getattr(self, "collector", None)
        st["WS"] = "🟢 bağlı" if coll and coll.connected else "🔴 kopuk"
        cstats = await kv_get("calendar_stats") or {}
        if cstats:
            st["takvim kaynakları"] = (f"yahoo:{cstats.get('yahoo', 0)}"
                                       f" finnhub:{cstats.get('finnhub', 0)}"
                                       f" nasdaq:{cstats.get('nasdaq', 0)}")
        st["evren"] = f"{n_tick} hisse coin"
        if coll and coll.crypto_coins:
            st["evren"] += f" + {len(coll.crypto_coins)} kripto (yalnız sonda tetiği)"
        elif coll and getattr(coll, "crypto_err", ""):
            # Sessizce kapanan özellik = olmayan özellik. Sebebi burada okunsun.
            st["evren"] += f" · ⚠️ kripto tetiği kapalı ({coll.crypto_err})"
        n_fills = await kv_get("fills_count")
        st["fill havuzu"] = n_fills if n_fills is not None else "sayılıyor…"
        st["adres havuzu"] = n_addr
        st["watchlist"] = n_watch
        sw = await kv_get("sweep_stats") or {}
        if sw.get("hot"):
            last_full = await kv_get("sweep_last_full")
            errnote = ""
            if sw.get("err") and not sw.get("ok"):
                errnote = " ⚠️ son parti TAMAMEN hata"
                if sw.get("err_msg"):
                    errnote += f": {sw['err_msg']}"
            elif sw.get("err"):
                errnote = f" ({sw.get('ok', 0)}✓/{sw['err']}✗)"
            pace = ""
            if sw.get("batch"):
                pace = (f" · parti {sw['batch']} adres"
                        f" ({sw.get('batch_hot', 0)} sıcak/{sw.get('batch_cold', 0)} soğuk)")
                if sw.get("rpm_max"):
                    pct = round(sw.get("rpm", 0) / sw["rpm_max"] * 100)
                    pace += f", istek bütçesi %{pct}"
            st["derin keşif"] = (f"sıcak {sw['hot']} adres"
                                 f" (tur ~{sw.get('tour_min', '?')} dk)"
                                 f" · soğuk kuyruk {sw.get('cold', 0)}"
                                 + (f" (tur ~{sw['cold_min']} dk)" if sw.get("cold_min") else "")
                                 + pace
                                 + (f", son tam tur {tr_time(int(last_full))}"
                                    if last_full else ", ilk tur sürüyor")
                                 + errnote)
        # Duvar radarı: sıfır sonuç "duvar yok" da olabilir "defter alınamadı"
        # da. Nabız attığı için sağlık YEŞİL kalıyordu; ikisini ayırt et.
        wl = await kv_get("wall_stats") or {}
        if wl.get("book_err"):
            st["duvar radarı"] = (
                f"⚠️ {wl['book_err']}/{wl.get('coins', '?')} coinde defter alınamadı"
                + (f": {wl['err_msg']}" if wl.get("err_msg") else ""))
        hv = await kv_get("harvest_stats") or {}
        if hv.get("total"):
            st["işlem hasadı"] = f"{hv['total']} fill REST'ten toplandı"
        from ..health import snapshot
        snap = await snapshot(self.cfg)
        n_ok = sum(1 for c in snap["checks"].values() if c["ok"])
        st["⚕️ sağlık"] = (f"{n_ok}/{len(snap['checks'])} görev ✅"
                          + ("" if not snap["problems"] else
                             f" — ⚠️ sorun: {', '.join(snap['problems'])} (/saglik)"))
        await self.send(fmt.status_text(st), chat_id)

    async def _cmd_scan(self, args: list[str], chat_id: str) -> None:
        from ..radar import clusters
        from ..radar.report import build_scan, coin_dex  # döngüsel importu kır

        if not args:
            await self.send("Kullanım: /scan SNDK", chat_id)
            return
        t = await find_ticker(args[0])
        if not t:
            await self.send(f"'{args[0]}' HL hisse evreninde yok. /status ile evren boyutuna bak.", chat_id)
            return
        await self.send(f"🔍 <b>{t['symbol']}</b> taranıyor…", chat_id)
        try:
            summ, rows = await build_scan(self.cfg, self.client, t["coin"],
                                          coin_dex(t["coin"]), quick=True)
            cluster_list = await clusters.find_clusters(rows)
            ev = {"symbol": t["symbol"]}
            text = fmt.earnings_report(ev, "ondemand", summ, rows, self.cfg,
                                       cluster_list=cluster_list)
            await self.send(text, chat_id)
        except Exception as e:
            log.exception("scan hatası: %s", t["symbol"])
            await self.send(f"❌ <b>{t['symbol']}</b> taranamadı: {fmt.esc(e)}", chat_id)

    async def _cmd_twap(self, args: list[str], chat_id: str) -> None:
        """/twap 0xADRES → adresin dizileri + HL emirleri + kapı kararı; /twap COIN → dinleme,
        hacim, gereken emir; /twap → özet + son kararlar. "Niye gelmedi" sorusunun cevabı."""
        from ..radar import twaplive as tl
        arg = (args[0] if args else "").strip()
        if arg.lower().startswith("0x"):
            txt = await tl.diag_address(self.cfg, getattr(self, "collector", None), arg)
        elif arg:
            txt = await tl.diag_coin(self.cfg, arg)
        else:
            txt = await tl.diag_summary(self.cfg)
        await self.send(txt, chat_id)

    async def _cmd_whale(self, args: list[str], chat_id: str) -> None:
        if not args or not args[0].startswith("0x"):
            await self.send("Kullanım: /whale 0xADRES", chat_id)
            return
        addr = args[0].lower()
        lines = [f"👤 {fmt.alink(addr)}"]
        async with db() as conn:
            cur = await conn.execute("SELECT * FROM addresses WHERE address=?", (addr,))
            row = await cur.fetchone()
        if row:
            r = dict(row)
            lines.append(f"🎯 Sicil: {r.get('hits') or 0} doğru / {r.get('misses') or 0} yanlış"
                         + (" │ ⭐ watchlist" if r.get("watchlist") else ""))
        pos_found = False
        n_fail = 0
        from .. import assets
        for dex in [*assets.watched_dexes(self.cfg), ""]:
            try:
                state = await self.client.clearinghouse(addr, dex)
            except Exception as e:
                # ALL_DEXES dersi: yutulan hata "burada bir şey yok"a dönüşüyordu.
                # Burada daha kötüsü oluyordu — hiç bakılmadan "poz yok" DENİYORDU.
                n_fail += 1
                log.warning("/balina %s… dex=%r sorgusu düştü: %s", addr[:10], dex, e)
                continue
            for ap in (state or {}).get("assetPositions") or []:
                p = ap.get("position") or {}
                try:
                    szi = float(p.get("szi") or 0)
                except (TypeError, ValueError):
                    continue
                if szi == 0:
                    continue
                pos_found = True
                side = "🔴SHORT" if szi < 0 else "🟢LONG"
                lines.append(f"  {p.get('coin')} {side} {fmt.usd(float(p.get('positionValue') or 0))} "
                             f"@{fmt.px(float(p.get('entryPx') or 0))}")
        n_dex = len(assets.watched_dexes(self.cfg)) + 1
        if not pos_found and n_fail >= n_dex:
            lines.append("⚠️ Hiçbir dex sorgusu yanıt vermedi — <b>poz yok demiyoruz</b>, "
                         "HL'ye ulaşılamadı. Birazdan tekrar dene.")
        elif not pos_found:
            lines.append("Açık pozisyon yok (hisse dex'leri + ana dex bakıldı)."
                         + (f" ⚠️ {n_fail} dex sorgusu düştü, eksik olabilir." if n_fail else ""))
        elif n_fail:
            lines.append(f"⚠️ {n_fail} dex sorgusu düştü — liste eksik olabilir.")
        await self.send("\n".join(lines), chat_id)

    async def _cmd_forget(self, args: list[str], chat_id: str) -> None:
        """Adresin tüm sicilini sıfırla: hits/misses=0, watchlist'ten çıkar.
        Yanlışlıkla (küçük pozisyonla) eklenmiş adresleri temizlemek için."""
        if not args or not args[0].startswith("0x"):
            await self.send("Kullanım: /forget 0xADRES — sicilini sıfırlar,"
                            " watchlist'ten çıkarır", chat_id)
            return
        addr = args[0].lower()
        async with db() as conn:
            cur = await conn.execute(
                "SELECT hits, misses, watchlist FROM addresses WHERE address=?", (addr,))
            row = await cur.fetchone()
            await conn.execute(
                "UPDATE addresses SET hits=0, misses=0, watchlist=0 WHERE address=?", (addr,))
        if row:
            await self.send(
                f"🧹 {fmt.short(addr)} unutuldu — sicil sıfırlandı"
                f" (önceki: {row['hits']}✓/{row['misses']}✗"
                + (", watchlist'teydi" if row["watchlist"] else "") + ")", chat_id)
        else:
            await self.send(f"{fmt.short(addr)} zaten kayıtlı değil.", chat_id)

    async def _cmd_watch(self, args: list[str], chat_id: str, add: bool) -> None:
        if not args or not args[0].startswith("0x"):
            await self.send(f"Kullanım: /{'watch' if add else 'unwatch'} 0xADRES", chat_id)
            return
        addr = args[0].lower()
        async with db() as conn:
            if add:
                await conn.execute(
                    "INSERT INTO addresses(address, first_seen, watchlist) VALUES(?,?,1)"
                    " ON CONFLICT(address) DO UPDATE SET watchlist=1",
                    (addr, now()))
            else:
                await conn.execute(
                    "UPDATE addresses SET watchlist=0 WHERE address=?", (addr,))
        await self.send(("⭐ Eklendi: " if add else "Çıkarıldı: ") + fmt.short(addr), chat_id)

    async def _cmd_notifications(self, chat_id: str) -> None:
        from ..notify import KINDS, in_quiet_hours, recent_sent
        lines = ["🔔 <b>Bildirim ayarları</b>"]
        for kind, (field, label, prio) in KINDS.items():
            if not field:
                continue
            on = bool(getattr(self.cfg, field, True))
            lines.append(f"  {'✅' if on else '⛔'} {label}")
        qs, qe = int(self.cfg.quiet_start_hour), int(self.cfg.quiet_end_hour)
        if qs == qe:
            lines.append("\n🌙 Sessiz saat: kapalı")
        else:
            lines.append(f"\n🌙 Sessiz saat: <b>{qs:02d}:00–{qe:02d}:00</b> TSİ"
                         + (" (şu an sessizdeyiz)" if in_quiet_hours(self.cfg) else "")
                         + ("\n   Önemliler yine de geliyor" if self.cfg.quiet_allow_high
                            else "\n   Hiçbir şey gelmiyor, sabah özetine yazılıyor"))
        lines.append(f"🌅 Sabah özeti: <b>{int(self.cfg.digest_hour):02d}:00</b> TSİ"
                     + ("" if self.cfg.notify_digest else " (kapalı)"))
        from ..radar.alertgate import tiers
        t = tiers(self.cfg)
        lines.append("🎯 Yeni büyük poz bildirimi: normal hisse ≥ <b>" + fmt.usd(t["big_normal"] or 0) + "</b>"
                     + " · büyük hisse " + (fmt.usd(t["big_major"]) if t["big_major"] else "yok")
                     + " · endeks " + (fmt.usd(t["big_index"]) if t["big_index"] else "yok")
                     + f" · tek işlem ≥ {fmt.usd(t['fill'] or 0)} (sicilli {fmt.usd(t['fill_watch'] or 0)})"
                     + f" · OI birikimi ≥ {fmt.usd(t['oi_delta'] or 0)}")
        sent = await recent_sent(8)
        if sent:
            lines.append("\n📜 <b>Son bildirimler:</b>")
            for s in sent:
                kind = (s["kind"] or "").split(":", 1)
                mark = "😴" if kind[0] == "quiet" else "✅"
                # Önce HTML tag'lerini sıyır SONRA kes — [:60] </b> gibi bir tag'i
                # ortadan bölerse tüm /bildirimler mesajı Telegram'dan 400 alırdı.
                raw = re.sub(r"<[^>]+>", "", (s.get("payload") or "").split("\n")[0])
                lines.append(f"  {mark} {tr_time(s['ts'])} {fmt.esc(raw[:60])}")
        lines.append("\n<i>Ayarları dashboard → ⚙️ Ayarlar → Bildirimler'den değiştir.</i>")
        await self.send("\n".join(lines), chat_id)

    async def _cmd_settime(self, args: list[str], chat_id: str) -> None:
        """Bilanço saatini elle düzelt — kaynaklar yanılırsa son söz sende.
        /settime SHAZ bmo | amc | 16:30 (TSİ) | 09:30et [YYYY-MM-DD]"""
        from datetime import datetime as _dt

        from ..earnings.calendar import ET, TR, annotate

        if len(args) < 2:
            await self.send(
                "Kullanım:\n"
                "<code>/settime SHAZ bmo</code> — sabah (açılış öncesi)\n"
                "<code>/settime SHAZ amc</code> — akşam (kapanış sonrası)\n"
                "<code>/settime SHAZ 16:30</code> — tam saat (<b>TSİ</b>)\n"
                "<code>/settime SHAZ 09:30et</code> — tam saat (ET/New York)\n"
                "Tarih eklemek için sona: <code>/settime BIRD amc 2026-08-12</code>\n\n"
                "Elle girilen kayıtları hiçbir kaynak ezmez.", chat_id)
            return

        sym = args[0].upper().lstrip("$")
        spec = args[1].lower()
        want_date = args[2] if len(args) > 2 else None

        # Tarih verildiyse ISO (YYYY-MM-DD) olmalı — Türkçe '12.08.2026' gibi bir
        # değer geçersiz kayıt yaratıp sonraki refresh'te sessizce siliniyordu.
        if want_date and not cal.valid_date_et(want_date):
            await self.send(
                f"Tarih formatı <b>YYYY-MM-DD</b> olmalı (ör. 2026-08-12).\n"
                f"'{fmt.esc(want_date)}' anlaşılamadı.", chat_id)
            return

        t = await find_ticker(sym)
        if not t:
            await self.send(f"'{sym}' HL evreninde yok.", chat_id)
            return

        async with db() as conn:
            if want_date:
                cur = await conn.execute(
                    "SELECT * FROM earnings_events WHERE symbol=? AND date_et=?",
                    (sym, want_date))
            else:
                cur = await conn.execute(
                    """SELECT * FROM earnings_events WHERE symbol=? AND evaluated=0
                       ORDER BY date_et LIMIT 1""", (sym,))
            row = await cur.fetchone()
        ev = dict(row) if row else None
        date_et = want_date or (ev["date_et"] if ev else None)
        if not date_et:
            await self.send(f"{sym} için takvimde kayıt yok. Tarih de ver:"
                            f" <code>/settime {sym} {spec} 2026-08-12</code>", chat_id)
            return

        hint, exact_ts = None, None
        if spec in ("bmo", "amc"):
            hint = spec
        else:
            zone, raw = TR, spec
            if raw.endswith("et"):
                zone, raw = ET, raw[:-2].strip()
            elif raw.endswith("tsi"):
                raw = raw[:-3].strip()
            try:
                hh, mm = (int(x) for x in raw.replace(".", ":").split(":"))
                d = _dt.strptime(date_et, "%Y-%m-%d").replace(
                    hour=hh, minute=mm, tzinfo=zone)
            except (ValueError, TypeError):
                await self.send("Saati anlayamadım. Örnek: <code>/settime SHAZ 16:30</code>", chat_id)
                return
            exact_ts = int(d.timestamp())
            et_t = _dt.fromtimestamp(exact_ts, ET).time()
            hint = "bmo" if et_t.hour < 12 else "amc"

        async with db() as conn:
            if ev:
                await conn.execute(
                    """UPDATE earnings_events SET hour_hint=?, exact_ts=?, date_et=?,
                         source='manual', note='✍️ saat elle girildi'
                       WHERE id=?""", (hint, exact_ts, date_et, ev["id"]))
            else:
                await conn.execute(
                    """INSERT INTO earnings_events(symbol,coin,date_et,hour_hint,exact_ts,
                         source,note,created_ts)
                       VALUES(?,?,?,?,?,'manual','✍️ saat elle girildi',?)
                       ON CONFLICT(symbol,date_et) DO UPDATE SET
                         hour_hint=excluded.hour_hint, exact_ts=excluded.exact_ts,
                         source='manual', note=excluded.note""",
                    (sym, t["coin"], date_et, hint, exact_ts, now()))

        ann = annotate([{"symbol": sym, "date_et": date_et, "hour_hint": hint,
                         "exact_ts": exact_ts}])[0]
        await self.send(
            f"✅ <b>{sym}</b> güncellendi: {ann['icon']} <b>{ann['tsi']} TSİ</b>"
            f" ({ann['when_txt']}{'' if ann['exact'] else ', yaklaşık'})"
            f"\n{'❗ Bu saat geçti' if ann['passed'] else '⏳ ' + ann['countdown'] + ' kaldı'}"
            "\n<i>Bu kayıt artık kaynaklar tarafından değiştirilmez.</i>", chat_id)

    async def _cmd_ignore(self, args: list[str], chat_id: str, on: bool) -> None:
        if not args or not args[0].startswith("0x"):
            await self.send(f"Kullanım: /{'ignore' if on else 'unignore'} 0xADRES", chat_id)
            return
        addr = args[0].lower()
        async with db() as conn:
            if on:
                await conn.execute(
                    "INSERT INTO addresses(address, first_seen, entity) VALUES(?,?,'manual')"
                    " ON CONFLICT(address) DO UPDATE SET entity='manual'", (addr, now()))
            else:
                await conn.execute(
                    "UPDATE addresses SET entity=NULL WHERE address=?", (addr,))
        await self.send(("🚫 Elendi (MM/vault muamelesi): " if on else "✅ Tekrar dahil: ")
                        + fmt.short(addr), chat_id)

    async def _cmd_twap_follow_list(self, chat_id: str) -> None:
        """/twaptakipler — izlenen TWAP emirleri."""
        from ..radar import twapfollow
        rows = await twapfollow.active()
        if not rows:
            await self.send("👁 İzlenen TWAP emri yok. TWAP alarmındaki /takip_N komutuna bas.", chat_id)
            return
        lines = [f"👁 <b>İzlenen TWAP emirleri</b> ({len(rows)})"]
        for r in rows[:20]:
            lines.append(f"#{r['id']} <b>{fmt.esc(assets.label(r['coin']))}</b>"
                         f" · {'ALIM' if r['side'] == 'buy' else 'SATIM'}"
                         f" · {fmt.alink(r['address'])}"
                         f" · {fmt.age_str(r['created_ts'])} önce → /birak_twap_{r['id']}")
        await self.send("\n".join(lines), chat_id)

    async def _cmd_twap_follow(self, offer: dict, offer_id: int, chat_id: str) -> None:
        """TWAP EMRİ takibi: iptal edilirse ⛔, bitince 🏁 haber gelir — yalnız
        basana. Pozisyon takibinden farkı emrin izlenmesidir; spot TWAP'ta
        pozisyon yoktur ama emir vardır."""
        from ..radar import twapfollow
        async with db() as conn:
            await conn.execute("UPDATE track_offers SET used=1 WHERE id=?", (offer_id,))
        fid = await twapfollow.start(self.cfg, offer["coin"], offer["address"],
                                     offer["side"], int(offer.get("ref_ts") or 0),
                                     chat_id=self._track_chat(chat_id))
        days = int(getattr(self.cfg, "twap_follow_expire_days", 7) or 7)
        sym = fmt.esc(offer.get("symbol") or offer["coin"])
        half = "; yarılanınca da not düşerim" if getattr(self.cfg, "twap_follow_progress", True) else ""
        await self.send(
            f"👁 <b>{sym}</b> TWAP emri takipte (#{fid}) · {fmt.alink(offer['address'])}\n"
            f"İptal edilirse <b>⛔</b>, bitince <b>🏁</b> haber vereceğim{half}."
            f" Takip {days} gün sonra kendiliğinden kapanır · bırakmak için"
            f" /birak_twap_{fid}", chat_id, reply_markup=fmt.stop_kb("twap", fid))

    async def _cmd_accounts(self, chat_id: str) -> None:
        """/hesaplar — önce 🔂 dilimli alım-satım durumu (kv'den), sonra izlenen hesapların son
        anlık durumu (pozisyon + duvar + bekleyen kapatma/stop emirleri) son yoklamanın
        kaydından; ayrıca son 1 saatin GERÇEKLEŞEN dolumları (hesap başına bir istek grubu,
        60 sn önbellekli)."""
        from ..hl.universe import main_dex_ctx
        from ..radar import acctwatch, slicewatch
        snaps = await acctwatch.snapshots(self.cfg)
        slices = await slicewatch.snapshots(self.cfg)
        if not snaps and not slices:
            await self.send("👤 İzlenen hesap yok. Ayarlar → 👤 İzlenen hesaplar → 'adres:isim'"
                            " (dilimli alım-satım: 🔂 → 'adres:SEMBOL').", chat_id)
            return
        # 🔂 dilimli alım-satım durumu ÖNCE (kv'den, HL'ye istek yok); hesap özetleri ardından.
        sctx = self._slice_ctx()
        for sn in slices:
            await self.send(fmt.slice_status(sn, sctx), chat_id)
        if not snaps:
            return
        try:
            marks = {c: v.get("m") for c, v in ((await main_dex_ctx(None, fetch=False)).get("c") or {}).items()}
        except Exception:
            marks = {}
        for s in snaps:
            st = s["state"]
            if not st:
                why = ("ACCOUNT_CHAT_ID tanımsız — yoklanmıyor"
                       if not (getattr(self.cfg, "account_chat_id", "") or "").strip()
                       else "henüz yoklanmadı")
                await self.send(f"👤 <b>{fmt.esc(s['name'])}</b> · {fmt.alink(s['address'])} — {why}.", chat_id)
                continue
            flow = (await acctwatch.fill_flow_cached(self.client, s["address"])
                    if self.client is not None else None)
            await self.send(fmt.acct_started(s["address"], st, marks,
                                             title=f"anlık durum ({fmt.age_str(st.get('ts'))} önce)",
                                             tail=False, flow=flow), chat_id)

    def _slice_ctx(self) -> dict:
        from ..radar import slicewatch
        quiet_ms, start_n, start_usd = slicewatch.rules(self.cfg)
        return {"now_ms": now() * 1000, "lookback_s": slicewatch.LOOKBACK_SEC,
                "chat_ok": bool((getattr(self.cfg, "account_chat_id", "") or "").strip()),
                "enabled": bool(getattr(self.cfg, "slice_watch_enabled", True)),
                "quiet_s": quiet_ms // 1000, "start_n": start_n, "start_usd": start_usd,
                "poll_s": max(10, int(getattr(self.cfg, "slice_poll_sec", 30) or 30))}

    async def _cmd_slices(self, chat_id: str) -> None:
        """/balina — 🔂 dilimli alım-satımın şu anki durumu: kaç saattir alıyor/satıyor, ne
        kadar, son dilim, fiyat, pozisyon. Son yoklamanın kaydından (kv; HL'ye istek yok)."""
        from ..radar import slicewatch
        slices = await slicewatch.snapshots(self.cfg)
        if not slices:
            await self.send("🔂 Dilimli alım-satım listesi boş. Ayarlar → 🔂 Dilimli alım-satım →"
                            " 'adres:SEMBOL' (ör. 0x30af…:CBRS).", chat_id)
            return
        sctx = self._slice_ctx()
        for sn in slices:
            await self.send(fmt.slice_status(sn, sctx), chat_id)

    async def _cmd_open_movers(self, chat_id: str) -> None:
        """/acilis — 🔔 açılış penceresindeyse anlık sıralama (canlı akıştan, HL'ye istek yok),
        değilse sıradaki ölçüm saatleri + son rapor."""
        from ..radar import openmove
        try:
            await self.send(await openmove.live_view(self.cfg), chat_id)
        except Exception as e:
            log.exception("/acilis")
            await self.send(f"❌ açılış ölçümü okunamadı: {fmt.esc(e)}", chat_id)

    async def _cmd_movewin(self, cmd: str, args: list[str], chat_id: str) -> bool:
        """⏱ /5dk · /15dk · /5dk 15:30 — şimdiden (ya da verilen TSİ saatten) N dk ölç, bitince en
        hareketli hisseler BU sohbete. Pencere komutu değilse False (sıradaki komuta geçilir)."""
        from ..radar import movewin
        if movewin.parse(cmd, args) is None:
            return False
        try:
            text, kb = await movewin.command(self.cfg, cmd, args, chat_id)
            await self.send(text, chat_id, reply_markup=kb)
        except Exception as e:
            log.exception("/%s", cmd)
            await self.send(f"❌ ölçüm başlatılamadı: {fmt.esc(e)}", chat_id)
        return True

    async def _movewin_callback(self, cq: dict) -> None:
        """📊 Şu ana kadar — o ana kadarki ilk 5 açılır pencerede. Salt okunur: botun kendi
        sohbetlerinde herkes basabilir (kanalda yönetici şartı yok); yabancı sohbette sessiz."""
        from ..radar import movewin
        msg = cq.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        uid = str((cq.get("from") or {}).get("id") or "")
        oid = self._owner_user_id()
        if not chat_id or not (chat_id in self._own_chats() or (oid and uid == oid)):
            await self._ack(cq.get("id"))
            return
        try:
            text = movewin.peek_data(self.cfg, str(cq.get("data") or ""), chat_id)
        except Exception:
            log.exception("📊 ara bakış")
            text = "Ara durum okunamadı."
        try:
            await self.answer_callback(cq.get("id"), text, alert=True)
        except Exception:
            log.debug("answerCallbackQuery", exc_info=True)

    async def _cmd_seans(self, args: list[str], chat_id: str) -> None:
        """/seans — 🕰 ABD seans karnesi: ayardaki semboller (XYZ100 + SP500) TEK mesajda;
        /seans NVDA tek sembol. Bugünün seans durumu + karnenin özeti; ayrıntı sayfada."""
        from ..radar import seans
        syms = [a.strip().upper().split(":")[-1][:24] for a in args[:1] if a.strip()]
        views = []
        for sym in (syms or seans.default_symbols(self.cfg))[:4]:
            try:
                views.append(await seans.view(self.cfg, self.client, sym))
            except Exception as e:                   # noqa: BLE001 — sembol başına
                log.exception("/seans %s", sym)
                views.append({"ok": False, "sym": sym, "reason": f"hesaplanamadı — {type(e).__name__}: {e}"[:160]})
        await self.send(fmt.seans_card(views, base_url=getattr(self.cfg, "public_base_url", "") or ""), chat_id)

    async def _cmd_sticky_follow_list(self, chat_id: str) -> None:
        """/duvartakipler — izlenen yapışkan duvarlar."""
        from ..radar import stickywall
        rows = await stickywall.follows_active()
        if not rows:
            await self.send("🧲 İzlenen yapışkan duvar yok. Alarmdaki /takip_N komutuna bas.", chat_id)
            return
        lines = [f"🧲 <b>İzlenen yapışkan duvarlar</b> ({len(rows)})"]
        for r in rows[:20]:
            state = (f"şimdi {fmt.usd(r['ntl_last'])}" if r["wall_active"]
                     else f"bitti: {fmt.esc(r.get('status') or '?')}")
            lines.append(f"#{r['id']} <b>{fmt.esc(assets.label(r['coin']))}</b>"
                         f" · {'SATIŞ' if r['side'] == 'ask' else 'ALIŞ'} · {state}"
                         f" (tepe {fmt.usd(r['peak_ntl'])})"
                         f" · {fmt.age_str(r['created_ts'])} önce → /birak_duvar_{r['id']}")
        await self.send("\n".join(lines), chat_id)

    async def _cmd_sticky_follow(self, offer: dict, offer_id: int, chat_id: str) -> None:
        """YAPIŞKAN DUVAR takibi: yarılanma, yeni dilim, bitiş (yenildi/çekildi) —
        yalnız basana. Kanal yalnız ilk alarmı alır."""
        from ..radar import stickywall
        fid, w = await stickywall.follow_start(self.cfg, int(offer.get("ref_ts") or 0),
                                               chat_id=self._track_chat(chat_id))
        sym = fmt.esc(offer.get("symbol") or offer["coin"])
        if fid is None:
            if w is None:
                await self.send(f"#{offer_id} numaralı duvar kaydı bulunamadı.", chat_id)
            else:
                await self.send(f"🧲 <b>{sym}</b> duvarı ZATEN BİTTİ"
                                f" ({fmt.esc(w.get('status') or '?')}, {fmt.age_str(w.get('end_ts'))} önce)"
                                " — takip edilecek bir şey kalmadı.", chat_id)
            return
        async with db() as conn:
            await conn.execute("UPDATE track_offers SET used=1 WHERE id=?", (offer_id,))
        now_txt = (f"şimdi {fmt.usd(w.get('ntl_last'))}" if w.get("active")
                   else "az önce bitti — 15 dk içinde dönerse haber veririm")
        await self.send(
            f"👁 <b>{sym}</b> yapışkan duvarı takipte (#{fid}) · {now_txt}\n"
            "Yarılanınca, yeni dilim gelince ve bitince (<b>yenildi</b> / <b>çekildi</b>) haber"
            f" vereceğim. Takip {stickywall.FOLLOW_DAYS} gün sonra kendiliğinden kapanır ·"
            f" bırakmak için /birak_duvar_{fid}", chat_id, reply_markup=fmt.stop_kb("wall", fid))

    async def _cmd_track_start(self, cmd: str, chat_id: str) -> None:
        """Teklif mesajındaki /takip_N — balina çıkış takibini başlat."""
        from ..radar.tracker import live_position
        try:
            offer_id = int(cmd.split("_", 1)[1])
        except (ValueError, IndexError):
            await self.send("Teklif mesajındaki /takip_N komutuna bas.", chat_id)
            return
        async with db() as conn:
            cur = await conn.execute("SELECT * FROM track_offers WHERE id=?", (offer_id,))
            row = await cur.fetchone()
        if not row:
            await self.send(f"#{offer_id} numaralı takip teklifi bulunamadı.", chat_id)
            return
        offer = dict(row)
        # Teklif türü: 'twap' = EMİR takibi (iptal/bitiş haberi), 'pos' = balina
        # POZİSYONU takibi (boyut adımı, liq kayması, kapanış). Eski satırlarda
        # sütun NULL gelir → 'pos' okunur.
        if (offer.get("kind") or "pos") == "twap":
            await self._cmd_twap_follow(offer, offer_id, chat_id)
            return
        if offer.get("kind") == "sticky":
            await self._cmd_sticky_follow(offer, offer_id, chat_id)
            return
        async with db() as conn:
            cur = await conn.execute(
                "SELECT id FROM trackers WHERE active=1 AND address=? AND coin=?",
                (offer["address"], offer["coin"]))
            existing = await cur.fetchone()
        if existing:
            await self.send(f"Bu balina zaten takipte (#{existing['id']})."
                            " /takipler ile bakabilirsin.", chat_id)
            return
        try:
            live = await live_position(self.client, offer["address"], offer["coin"])
        except Exception as e:
            await self.send(f"❌ Pozisyon okunamadı, tekrar dene: {fmt.esc(e)}", chat_id)
            return
        async with db() as conn:
            await conn.execute("UPDATE track_offers SET used=1 WHERE id=?", (offer_id,))
        if not live:
            await self.send(
                f"🚪 {fmt.alink(offer['address'])} <b>{offer['symbol']}</b> pozisyonunu"
                " ZATEN KAPATMIŞ — takip edilecek bir şey kalmadı.", chat_id)
            return
        from ..radar.tracker import start_tracker
        tid = await start_tracker(self.cfg, offer["address"], offer["coin"],
                                  offer["symbol"], live, chat_id=self._track_chat(chat_id))
        await self.send(fmt.track_started(tid, offer["symbol"], offer["address"],
                                          live, self.cfg), chat_id, reply_markup=fmt.stop_kb("pos", tid))

    async def _cmd_track_manual(self, args: list[str], chat_id: str) -> None:
        """/takip 0xADRES SEMBOL — teklif beklemeden elle takip başlat."""
        from ..radar.tracker import live_position, start_tracker
        addr = next((a.lower() for a in args if a.lower().startswith("0x")), "")
        sym = next((a.upper().lstrip("$") for a in args
                    if not a.lower().startswith("0x")), "")
        if not addr or not sym:
            await self.send(
                "Kullanım: <code>/takip 0xADRES SEMBOL</code>"
                " (ör. <code>/takip 0xabc... SNDK</code>)\n"
                "Sadece <code>/takip</code> yazarsan aktif takipleri listeler.", chat_id)
            return
        # Hisse ÖNCE, sonra ana dex kripto (HYPE, PUMP…) — /takip 0x… HYPE de çalışsın.
        from ..hl.universe import resolve_coin
        t = await resolve_coin(sym)
        if not t:
            await self.send(f"'{sym}' HL evreninde yok.", chat_id)
            return
        sym = t["symbol"]
        async with db() as conn:
            cur = await conn.execute(
                "SELECT id FROM trackers WHERE active=1 AND address=? AND coin=?",
                (addr, t["coin"]))
            existing = await cur.fetchone()
        if existing:
            await self.send(f"Bu balina zaten takipte (#{existing['id']})."
                            " /takipler ile bakabilirsin.", chat_id)
            return
        try:
            live = await live_position(self.client, addr, t["coin"])
        except Exception as e:
            await self.send(f"❌ Pozisyon okunamadı, tekrar dene: {fmt.esc(e)}", chat_id)
            return
        if not live:
            await self.send(f"{fmt.alink(addr)} adresinin <b>{sym}</b> pozisyonu yok"
                            " — takip edilecek bir şey bulamadım.", chat_id)
            return
        tid = await start_tracker(self.cfg, addr, t["coin"], sym, live,
                                  chat_id=self._track_chat(chat_id))
        await self.send(fmt.track_started(tid, sym, addr, live, self.cfg), chat_id,
                        reply_markup=fmt.stop_kb("pos", tid))

    async def _cmd_track_list(self, chat_id: str) -> None:
        async with db() as conn:
            cur = await conn.execute(
                "SELECT * FROM trackers WHERE active=1 ORDER BY id DESC LIMIT 20")
            rows = [dict(r) for r in await cur.fetchall()]
        await self.send(fmt.track_list(rows), chat_id)

    async def _cmd_track_stop(self, cmd: str, chat_id: str) -> None:
        """/birak_N (pozisyon), /birak_duvar_N (🧲), /birak_twap_N (👁) — bildirimdeki 🛑 tuşuyla
        aynı yol (trackctl); onayda ↩️ geri al tuşu."""
        from ..radar import trackctl
        rest = cmd.split("_", 1)[1] if "_" in cmd else ""
        kind, usage = "pos", "Kullanım: /takipler listesindeki /birak_N komutuna bas."
        if rest.startswith("duvar_"):
            kind, usage = "wall", "Kullanım: /duvartakipler listesindeki /birak_duvar_N komutuna bas."
        elif rest.startswith("twap_"):
            kind, usage = "twap", "Kullanım: /twaptakipler listesindeki /birak_twap_N komutuna bas."
        try:
            fid = int(rest.rsplit("_", 1)[-1] if kind != "pos" else rest)
        except ValueError:
            await self.send(usage, chat_id)
            return
        if await trackctl.stop(kind, fid):
            st = await trackctl.state(kind, fid) or {}
            text = {"pos": f"👣 Takip #{fid} (<b>{fmt.esc(st.get('symbol') or '')}</b>"
                           f" {fmt.short(st.get('address') or '')}) bırakıldı.",
                    "wall": f"🧲 Yapışkan duvar takibi #{fid} bırakıldı.",
                    "twap": f"👁 TWAP emri takibi #{fid} bırakıldı."}[kind]
            await self.send(text, chat_id, reply_markup=fmt.undo_kb(kind, fid))
            return
        await self.send({"pos": f"#{fid} numaralı aktif takip yok. /takipler ile listeye bak.",
                         "wall": f"#{fid} numaralı aktif duvar takibi yok.",
                         "twap": f"#{fid} numaralı aktif TWAP takibi yok."}[kind], chat_id)

    async def _cmd_watchlist(self, chat_id: str) -> None:
        async with db() as conn:
            cur = await conn.execute(
                "SELECT * FROM addresses WHERE watchlist=1 ORDER BY hits DESC LIMIT 30")
            rows = [dict(r) for r in await cur.fetchall()]
        if not rows:
            await self.send("Watchlist boş. Earnings değerlendirmeleri doldurdukça"
                            " otomatik eklenecek; /watch 0x… ile elle de ekleyebilirsin.", chat_id)
            return
        lines = ["⭐ <b>Watchlist:</b>"]
        for r in rows:
            lines.append(f"  {fmt.alink(r['address'])} — {r.get('hits') or 0}✓/{r.get('misses') or 0}✗")
        await self.send("\n".join(lines), chat_id)
