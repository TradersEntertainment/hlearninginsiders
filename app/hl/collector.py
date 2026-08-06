"""7/24 WebSocket trade dinleyicisi.

Her trade mesajındaki users:[buyer, seller] alanından adres havuzunu büyütür,
büyük fill'leri kaydeder, eşik üstü / sicilli (watchlist) işlemlerde anlık
Telegram alert'i gönderir.
"""
import asyncio
import json
import logging

import aiohttp

from ..config import Config
from ..db import alert_log, alert_recent, db, now
from ..telegram import format as fmt

log = logging.getLogger("hl.collector")

WHALE_FILL_COOLDOWN = 1800  # aynı adres+coin için 30 dk'da bir alert


class Collector:
    def __init__(self, cfg: Config, session: aiohttp.ClientSession, bot=None, notifier=None):
        self.cfg = cfg
        self.session = session
        self.bot = bot
        self.notifier = notifier
        self.connected = False
        self.subscribed: set[str] = set()
        self.fills_seen = 0

    async def _current_coins(self) -> list[str]:
        async with db() as conn:
            cur = await conn.execute("SELECT coin FROM tickers")
            return [r["coin"] for r in await cur.fetchall()]

    async def run(self):
        delay = 5
        while True:
            try:
                await self._run_once()
                delay = 5
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected = False
                log.warning("WS koptu: %s — %ds sonra yeniden", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    async def _run_once(self):
        coins = await self._current_coins()
        if not coins:
            await asyncio.sleep(30)
            return
        async with self.session.ws_connect(self.cfg.ws_url, heartbeat=None,
                                           timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
            self.connected = True
            self.subscribed = set()
            for coin in coins:
                await ws.send_json({"method": "subscribe",
                                    "subscription": {"type": "trades", "coin": coin}})
                self.subscribed.add(coin)
                await asyncio.sleep(0.02)
            log.info("WS bağlı, %d coin'e abone", len(coins))

            ping_task = asyncio.create_task(self._pinger(ws))
            resub_task = asyncio.create_task(self._resubscriber(ws))
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
            finally:
                self.connected = False
                ping_task.cancel()
                resub_task.cancel()

    async def _pinger(self, ws):
        while True:
            await asyncio.sleep(45)
            await ws.send_json({"method": "ping"})

    async def _resubscriber(self, ws):
        """Evren yenilenince yeni coin'lere canlı sokette abone ol."""
        while True:
            await asyncio.sleep(600)
            for coin in await self._current_coins():
                if coin not in self.subscribed:
                    await ws.send_json({"method": "subscribe",
                                        "subscription": {"type": "trades", "coin": coin}})
                    self.subscribed.add(coin)
                    log.info("yeni coin'e abone: %s", coin)

    async def _handle(self, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if msg.get("channel") != "trades":
            return
        trades = msg.get("data") or []
        rows = []
        alerts = []
        for t in trades:
            try:
                coin = t["coin"]
                px = float(t["px"])
                sz = float(t["sz"])
                ts = int(t["time"]) // 1000
                tid = str(t.get("tid") or t.get("hash") or "")
                users = t.get("users") or []
            except (KeyError, TypeError, ValueError):
                continue
            notional = px * sz
            if notional < self.cfg.min_fill_notional or len(users) < 2:
                continue
            buyer, seller = users[0], users[1]
            rows.append((coin, tid, buyer.lower(), "buy", px, sz, notional, ts))
            rows.append((coin, tid, seller.lower(), "sell", px, sz, notional, ts))
            if notional >= self.cfg.whale_alert_notional:
                alerts.append((coin, buyer.lower(), "buy", px, notional))
                alerts.append((coin, seller.lower(), "sell", px, notional))

        if not rows:
            return
        watch = set()
        async with db() as conn:
            for r in rows:
                await conn.execute(
                    "INSERT OR IGNORE INTO fills(coin,tid,address,side,px,sz,notional,ts)"
                    " VALUES(?,?,?,?,?,?,?,?)", r)
                await conn.execute(
                    "INSERT INTO addresses(address, first_seen) VALUES(?,?)"
                    " ON CONFLICT(address) DO NOTHING", (r[2], r[7]))
            self.fills_seen += len(rows)
            addr_list = list({r[2] for r in rows})
            q = ",".join("?" * len(addr_list))
            cur = await conn.execute(
                f"SELECT address FROM addresses WHERE watchlist=1 AND address IN ({q})", addr_list)
            watch = {row["address"] for row in await cur.fetchall()}

        # Watchlist adresi eşik altı işlem yapsa da alert (sicilli balina)
        for r in rows:
            coin, _, addr, side, px, _, notional, _ = r
            if addr in watch and (coin, addr, side, px, notional) not in alerts:
                alerts.append((coin, addr, side, px, notional))

        for coin, addr, side, px, notional in alerts:
            await self._maybe_alert(coin, addr, side, px, notional, addr in watch)

    async def _maybe_alert(self, coin, addr, side, px, notional, is_watch):
        if not self.bot:
            return
        key = f"{coin}:{addr}"
        if await alert_recent("whale_fill", key, WHALE_FILL_COOLDOWN):
            return
        async with db() as conn:
            cur = await conn.execute(
                "SELECT hits, misses, entity FROM addresses WHERE address=?", (addr,))
            row = await cur.fetchone()
        if row and row["entity"] and not is_watch:
            return  # MM/vault fill'i — gürültü
        record = (row["hits"], row["misses"]) if row else (0, 0)
        text = fmt.whale_fill_alert(coin, addr, side, px, notional, is_watch, record)
        try:
            prio = "high" if is_watch else "normal"
            if await self.notifier.send("whale_fill", text, priority=prio, key=key):
                await alert_log("whale_fill", key, text)
        except Exception as e:
            log.warning("alert gönderilemedi: %s", e)
