"""Telegram bot — long polling + komutlar + mesaj gönderimi."""
import asyncio
import logging

import aiohttp

from ..config import Config
from ..db import db, now
from ..earnings.calendar import upcoming_events
from ..hl.client import HLClient
from ..hl.universe import find_ticker
from . import format as fmt

log = logging.getLogger("telegram.bot")

MAX_LEN = 4000


class TelegramBot:
    def __init__(self, cfg: Config, session: aiohttp.ClientSession,
                 client: HLClient, state: dict):
        self.cfg = cfg
        self.session = session
        self.client = client
        self.state = state
        self.api = f"https://api.telegram.org/bot{cfg.telegram_bot_token}"

    # ---------- gönderim ----------

    async def send(self, text: str, chat_id: str | None = None) -> None:
        chat = chat_id or self.cfg.telegram_chat_id
        if not chat:
            log.info("TELEGRAM_CHAT_ID yok, mesaj atlandı:\n%s", text[:200])
            return
        for chunk in self._split(text):
            async with self.session.post(
                f"{self.api}/sendMessage",
                json={"chat_id": chat, "text": chunk, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status != 200:
                    body = (await r.text())[:300]
                    log.warning("sendMessage %s: %s", r.status, body)

    @staticmethod
    def _split(text: str) -> list[str]:
        if len(text) <= MAX_LEN:
            return [text]
        parts, cur = [], ""
        for line in text.split("\n"):
            if len(cur) + len(line) + 1 > MAX_LEN:
                parts.append(cur)
                cur = line
            else:
                cur = f"{cur}\n{line}" if cur else line
        if cur:
            parts.append(cur)
        return parts

    # ---------- polling ----------

    async def run_polling(self) -> None:
        offset = None
        log.info("Telegram polling başladı")
        while True:
            try:
                params = {"timeout": 50}
                if offset is not None:
                    params["offset"] = offset
                async with self.session.get(
                    f"{self.api}/getUpdates", params=params,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as r:
                    data = await r.json()
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
                await asyncio.sleep(5)

    async def _handle_update(self, upd: dict) -> None:
        msg = upd.get("message") or upd.get("channel_post") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        if not text.startswith("/") or not chat_id:
            return
        parts = text.split()
        cmd = parts[0][1:].split("@")[0].lower()
        args = parts[1:]

        # Yapılandırılmış chat dışından sadece /id ve /start'a cevap ver
        if self.cfg.telegram_chat_id and chat_id != self.cfg.telegram_chat_id:
            if cmd in ("start", "id"):
                await self.send(f"Bu sohbetin chat id'si: <code>{chat_id}</code>", chat_id)
            return

        if cmd in ("start", "help"):
            await self.send(fmt.help_text() + f"\n\nChat id: <code>{chat_id}</code>", chat_id)
        elif cmd == "id":
            await self.send(f"Chat id: <code>{chat_id}</code>", chat_id)
        elif cmd == "upcoming":
            await self.send(fmt.upcoming_list(await upcoming_events(14)), chat_id)
        elif cmd == "scan":
            await self._cmd_scan(args, chat_id)
        elif cmd == "whale":
            await self._cmd_whale(args, chat_id)
        elif cmd == "watch":
            await self._cmd_watch(args, chat_id, add=True)
        elif cmd == "unwatch":
            await self._cmd_watch(args, chat_id, add=False)
        elif cmd == "watchlist":
            await self._cmd_watchlist(chat_id)
        elif cmd == "status":
            await self._cmd_status(chat_id)

    # ---------- komutlar ----------

    async def _cmd_status(self, chat_id: str) -> None:
        async with db() as conn:
            cur = await conn.execute("SELECT COUNT(*) c FROM tickers")
            n_tick = (await cur.fetchone())["c"]
            cur = await conn.execute("SELECT COUNT(*) c FROM fills")
            n_fills = (await cur.fetchone())["c"]
            cur = await conn.execute("SELECT COUNT(*) c FROM addresses")
            n_addr = (await cur.fetchone())["c"]
            cur = await conn.execute("SELECT COUNT(*) c FROM addresses WHERE watchlist=1")
            n_watch = (await cur.fetchone())["c"]
        st = dict(self.state)
        coll = getattr(self, "collector", None)
        st["WS"] = "🟢 bağlı" if coll and coll.connected else "🔴 kopuk"
        st["evren"] = f"{n_tick} coin"
        st["fill havuzu"] = n_fills
        st["adres havuzu"] = n_addr
        st["watchlist"] = n_watch
        await self.send(fmt.status_text(st), chat_id)

    async def _cmd_scan(self, args: list[str], chat_id: str) -> None:
        from ..radar.report import build_scan, coin_dex  # döngüsel importu kır

        if not args:
            await self.send("Kullanım: /scan SNDK", chat_id)
            return
        t = await find_ticker(args[0])
        if not t:
            await self.send(f"'{args[0]}' HL hisse evreninde yok. /status ile evren boyutuna bak.", chat_id)
            return
        await self.send(f"🔍 <b>{t['symbol']}</b> taranıyor…", chat_id)
        summ, rows = await build_scan(self.cfg, self.client, t["coin"],
                                      coin_dex(t["coin"]), quick=True)
        fake_event = {"symbol": t["symbol"], "date_et": "şimdi", "hour_hint": "",
                      "eps_est": None, "note": None}
        text = fmt.earnings_report(fake_event, "ondemand", summ, rows, self.cfg)
        await self.send(text.replace("🎯", "🔍").replace("earnings — ⏰ ~1 saat kaldı", "anlık tarama")
                        .replace("earnings — 🕐 erken pencere", "anlık tarama"), chat_id)

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
        for dex in [*self.cfg.equity_dexes, ""]:
            try:
                state = await self.client.clearinghouse(addr, dex)
            except Exception:
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
        if not pos_found:
            lines.append("Açık pozisyon yok (hisse dex'leri + ana dex bakıldı).")
        await self.send("\n".join(lines), chat_id)

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
