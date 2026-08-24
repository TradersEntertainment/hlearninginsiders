"""7/24 WebSocket trade dinleyicisi.

Her trade mesajındaki users:[buyer, seller] alanından adres havuzunu büyütür,
büyük fill'leri kaydeder, eşik üstü / sicilli (watchlist) işlemlerde anlık
Telegram alert'i gönderir.
"""
import asyncio
import json
import logging
import time

import aiohttp

from ..config import Config
from ..db import alert_log, alert_recent, db, now
from ..telegram import format as fmt

log = logging.getLogger("hl.collector")

WHALE_FILL_COOLDOWN = 1800  # aynı adres+coin için 30 dk'da bir alert
WS_RECEIVE_TIMEOUT = 90     # 90 sn hiç mesaj gelmezse (pong dahil) bağlantı ölü say


class Collector:
    def __init__(self, cfg: Config, session: aiohttp.ClientSession, bot=None,
                 notifier=None, client=None):
        self.cfg = cfg
        self.session = session
        self.bot = bot
        self.notifier = notifier
        self.client = client
        self.connected = False
        self.subscribed: set[str] = set()
        self.valid_coins: set[str] = set()  # canlı akışta kabul edilen coin'ler
        self.fills_seen = 0

    async def _current_coins(self) -> list[str]:
        async with db() as conn:
            cur = await conn.execute("SELECT coin FROM tickers")
            return [r["coin"] for r in await cur.fetchall()]

    async def run(self):
        delay = 5
        while True:
            started = time.monotonic()
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected = False
                log.warning("WS koptu: %s — %ds sonra yeniden", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)
                continue
            # Temiz kapanış (close frame): eskiden delay=5'e resetlenip HİÇ
            # beklemeden yeniden bağlanıyordu → sunucu handshake'i kabul edip hemen
            # kapatırsa saniyede birçok bağlanma fırtınası. Kısa yaşadıysa backoff.
            lived = time.monotonic() - started
            if lived < 30:
                log.info("WS temiz kapandı (%.0fs) — %ds bekle", lived, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)
            else:
                delay = 5
                await asyncio.sleep(1)

    async def _run_once(self):
        coins = await self._current_coins()
        if not coins:
            await asyncio.sleep(30)
            return
        # ws_receive=90: yarı-açık (half-open) TCP'de eskiden 'async for' 15-19 dk
        # bloklanır, connected=True yalan söylerdi. 45 sn'lik ping'e HL pong döndüğü
        # için 90 sn içinde mesaj gelmezse bağlantı gerçekten ölmüştür → reconnect.
        async with self.session.ws_connect(
                self.cfg.ws_url, heartbeat=None,
                timeout=aiohttp.ClientWSTimeout(ws_close=10,
                                                ws_receive=WS_RECEIVE_TIMEOUT)) as ws:
            self.connected = True
            self.subscribed = set()
            self.valid_coins = set(coins)
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
        """Evren yenilenince yeni coin'lere canlı sokette abone ol + geçerli
        coin setini tazele (exclude edilen coin _handle'da süzülsün)."""
        while True:
            await asyncio.sleep(600)
            current = await self._current_coins()
            self.valid_coins = set(current)
            for coin in current:
                if coin not in self.subscribed:
                    await ws.send_json({"method": "subscribe",
                                        "subscription": {"type": "trades", "coin": coin}})
                    self.subscribed.add(coin)
                    log.info("yeni coin'e abone: %s", coin)

    async def _handle(self, raw: str):
        from ..health import beat
        await beat("collector")  # WS'ten mesaj akıyor = canlı (30 sn throttle içeride)
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
            # Exclude edilen / delist olan coin canlı sokette hâlâ akabilir
            # (unsubscribe yok, reconnect'e kadar sürer). valid_coins boşsa (ilk
            # abonelik öncesi) süzme — aksi halde tickers'ta olmayanı at.
            if self.valid_coins and coin not in self.valid_coins:
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

        # Sicilli adres SADECE daha önce kazandığı hisseye dönerse ilgi çeker.
        # (Genel whale alertleri watchlist dışı büyük pozları da yakalar.)
        win_map: dict[str, set[str]] = {}
        if watch:
            from ..recompute import winner_coins_map
            win_map = await winner_coins_map(list(watch))
            for r in rows:
                coin, _, addr, side, px, _, notional, _ = r
                if addr in watch and coin in win_map.get(addr, set()) \
                        and (coin, addr, side, px, notional) not in alerts:
                    alerts.append((coin, addr, side, px, notional))

        # is_watch yalnız adresin BU coin'i kazandığı durumda True olmalı — yoksa
        # sicilli adresin hiç kazanmadığı bir coindeki fill'i sahte "🎯 SİCİLLİ
        # BALİNA X'E DÖNDÜ" alarmı üretiyor VE eval_min altı pozisyonda generic
        # 🐋 alarmını tamamen susturuyordu.
        for coin, addr, side, px, notional in alerts:
            is_watch = addr in watch and coin in win_map.get(addr, set())
            await self._maybe_alert(coin, addr, side, px, notional, is_watch)

    async def _position_notional(self, addr, coin, dex):
        """Adresin bu coindeki güncel pozisyon büyüklüğü ($). Client yoksa None."""
        if not self.client:
            return None
        try:
            state = await self.client.clearinghouse(addr, dex)
        except Exception:
            return None
        sym = coin.split(":")[-1]
        for ap in (state or {}).get("assetPositions") or []:
            pos = ap.get("position") or {}
            pcoin = pos.get("coin") or ""
            if pcoin == coin or pcoin.split(":")[-1] == sym:
                try:
                    return float(pos.get("positionValue") or 0)
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

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

        # Cooldown'u işleme kararı verir vermez (REST'ten ÖNCE) kur. Eskiden yalnız
        # başarılı gönderimde kurulduğu için: sessiz saatte/tip kapalıyken her fill
        # yeniden işlenip digest'i şişiriyor, is_watch'ta ise pozisyon eval_min
        # altındaysa fill başına bir clearinghouse REST çağrısı süresiz tekrarlıyordu.
        await alert_log("whale_fill", key, "")

        pos_ntl = notional
        if is_watch:
            # Sicilli adres kazandığı hisseye döndü — gerçek pozisyon $300K+ mı?
            dex = coin.split(":")[0] if ":" in coin else ""
            pn = await self._position_notional(addr, coin, dex)
            if pn is not None:
                pos_ntl = pn
            if pos_ntl < self.cfg.eval_min_notional:
                return  # küçük poz/probe — bildirme (cooldown zaten kuruldu)

        record = (row["hits"], row["misses"]) if row else (0, 0)
        text = fmt.whale_fill_alert(coin, addr, side, px, pos_ntl, is_watch, record)
        try:
            prio = "high" if is_watch else "normal"
            await self.notifier.send("whale_fill", text, priority=prio, key=key)
        except Exception as e:
            log.warning("alert gönderilemedi: %s", e)
