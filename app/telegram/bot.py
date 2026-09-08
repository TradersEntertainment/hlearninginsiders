"""Telegram bot — long polling + komutlar + mesaj gönderimi."""
import asyncio
import json
import logging
import re
import time
from collections import deque

import aiohttp

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
        # Düğme (callback) ve ödeme olayları yalnız herkese açık DM akışında anlamlı
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

    async def _admin(self, cmd: str, args: list[str], chat_id: str) -> bool:
        """Satış tarafı sahip komutları (app/telegram/admin.py)."""
        from . import admin
        return await admin.handle(self, cmd, args, chat_id)

    async def _dispatch_track(self, cmd: str, args: list[str], chat_id: str) -> bool:
        """Takip komutları (kanaldan da çalışır; haber komutun geldiği sohbete gider)."""
        if cmd.startswith("takip_"):
            await self._cmd_track_start(cmd, chat_id)
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
            getattr(self.cfg, "sim_chat_id", ""))
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
                                          live, self.cfg), chat_id)

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
        await self.send(fmt.track_started(tid, sym, addr, live, self.cfg), chat_id)

    async def _cmd_track_list(self, chat_id: str) -> None:
        async with db() as conn:
            cur = await conn.execute(
                "SELECT * FROM trackers WHERE active=1 ORDER BY id DESC LIMIT 20")
            rows = [dict(r) for r in await cur.fetchall()]
        await self.send(fmt.track_list(rows), chat_id)

    async def _cmd_track_stop(self, cmd: str, chat_id: str) -> None:
        try:
            tid = int(cmd.split("_", 1)[1])
        except (ValueError, IndexError):
            await self.send("Kullanım: /takipler listesindeki /birak_N komutuna bas.", chat_id)
            return
        async with db() as conn:
            cur = await conn.execute(
                "SELECT * FROM trackers WHERE id=? AND active=1", (tid,))
            row = await cur.fetchone()
            if row:
                await conn.execute(
                    "UPDATE trackers SET active=0, end_note='elle bırakıldı' WHERE id=?",
                    (tid,))
        if row:
            await self.send(f"👣 Takip #{tid} (<b>{row['symbol']}</b>"
                            f" {fmt.short(row['address'])}) bırakıldı.", chat_id)
        else:
            await self.send(f"#{tid} numaralı aktif takip yok. /takipler ile listeye bak.", chat_id)

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
