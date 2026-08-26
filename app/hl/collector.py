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
MAX_INFLIGHT_PROBES = 24    # aynı anda bekleyen sonda üst sınırı (fırtına freni)


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
        self.last_trade: dict[str, int] = {}  # coin -> son işlem ts (zombi nöbetçisi)
        self.fills_seen = 0
        self._probing: set[str] = set()   # sondası uçuşta olan adresler
        self.probes_ok = 0
        self.probes_err = 0
        self.probes_skipped = 0

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
        n = 0
        while True:
            await asyncio.sleep(45)
            await ws.send_json({"method": "ping"})
            n += 1
            if n % 7 == 0:      # ~5 dakikada bir: coin bazlı son işlem zamanını sakla
                await self._flush_last_trade()

    async def _flush_last_trade(self) -> None:
        """coin -> son işlem ts haritasını kv'ye yaz (zombi abonelik nöbetçisi).
        Abone olunduğu SANILAN ama veri gelmeyen market böyle görünür hale gelir."""
        if not self.last_trade:
            return
        try:
            from ..db import kv_get, kv_set
            stored = await kv_get("coin_last_trade") or {}
            stored.update({c: int(t) for c, t in self.last_trade.items()})
            await kv_set("coin_last_trade", stored)
        except Exception as e:
            log.debug("coin_last_trade yazılamadı: %s", e)

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
        rows = []                                  # fills'e yazılacak satırlar
        agg: dict[tuple, dict] = {}                # (coin, adres, yön) -> parti toplamı
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
            if len(users) < 2 or not tid:
                continue
            notional = px * sz
            self.last_trade[coin] = max(self.last_trade.get(coin, 0), ts)
            # HL trade'inde `side` AGRESÖRÜ söyler: "B" = alıcı süpürdü, "A" = satıcı.
            # İnsider sinyalinde bilgi taşıyan taraf pasif maker değil, fiyatı
            # süpüren taker'dır — bu yüzden adres perspektifinden kaydediyoruz.
            aggr = str(t.get("side") or "").upper()
            buyer, seller = (users[0] or "").lower(), (users[1] or "").lower()
            for addr, side in ((buyer, "buy"), (seller, "sell")):
                if not addr:
                    continue
                taker = None
                if aggr in ("A", "B"):
                    taker = 1 if ((side == "buy" and aggr == "B")
                                  or (side == "sell" and aggr == "A")) else 0
                a = agg.setdefault((coin, addr, side), {
                    "ntl": 0.0, "sz": 0.0, "pxsz": 0.0, "tids": [],
                    "tk": 0.0, "known": 0.0, "ts": ts, "wrote": False})
                a["ntl"] += notional
                a["sz"] += sz
                a["pxsz"] += px * sz
                a["tids"].append(tid)
                a["ts"] = max(a["ts"], ts)
                if taker is not None:
                    a["known"] += notional
                    if taker:
                        a["tk"] += notional
                if notional >= self.cfg.min_fill_notional:
                    rows.append((coin, tid, addr, side, px, sz, notional, ts, taker))
                    a["wrote"] = True

        # Büyük bir market emri aynı blokta onlarca küçük match'e bölünür: tek tek
        # hiçbiri eşiği geçmez ama TOPLAMI $500K olabilir. Böyle bir süpürme için
        # tek sentetik satır yaz (tid deterministik → tekrar teslimde dedupe olur).
        for (coin, addr, side), a in agg.items():
            if a["wrote"] or a["ntl"] < self.cfg.min_fill_notional or not a["sz"]:
                continue
            taker = None
            if a["known"] > 0:
                taker = 1 if a["tk"] >= a["known"] / 2 else 0
            rows.append((coin, f"agg{min(a['tids'])}", addr, side,
                         a["pxsz"] / a["sz"], a["sz"], a["ntl"], a["ts"], taker))

        if not rows:
            return
        watch = set()
        async with db() as conn:
            for r in rows:
                await conn.execute(
                    "INSERT OR IGNORE INTO fills(coin,tid,address,side,px,sz,notional,ts,taker)"
                    " VALUES(?,?,?,?,?,?,?,?,?)", r)
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

        # Alarm kararı PARTİ TOPLAMI üzerinden verilir (bölünmüş emirler kaçmasın)
        for (coin, addr, side), a in agg.items():
            is_watch = addr in watch and coin in win_map.get(addr, set())
            if a["ntl"] < self.cfg.whale_alert_notional and not is_watch:
                continue
            avg_px = a["pxsz"] / a["sz"] if a["sz"] else 0.0
            ratio = (a["tk"] / a["known"]) if a["known"] > 0 else None
            n_parts = len(set(a["tids"]))
            await self._maybe_alert(coin, addr, side, avg_px, a["ntl"], is_watch,
                                    taker_ratio=ratio, n_parts=n_parts)

        # Büyük işlem gördüğümüz adresin TÜM defterine HEMEN bak. Süpürücü
        # havuzu sırayla geziyor; bu adrese sıra saatler sonra gelebilirdi.
        await self._kick_probes(agg)

    async def _kick_probes(self, agg: dict) -> None:
        """Eşiği aşan adresler için anlık profil sondası (arka planda).

        WS döngüsünü ASLA bloklamaz: istekler ayrı görevde koşar. Aynı adres
        için hem uçuşta-tekilleştirme hem kalıcı cooldown var; toplam uçuş
        sayısı da sınırlı (bir işlem fırtınası REST'i boğmasın).
        """
        cfg, client = self.cfg, self.client
        if not client or float(getattr(cfg, "probe_min_notional", 0)) <= 0:
            return
        thr = float(cfg.probe_min_notional)
        # aynı adres birden çok coin'de işlem yaptıysa TOPLAMINA bak
        by_addr: dict[str, float] = {}
        for (coin, addr, side), a in agg.items():
            by_addr[addr] = by_addr.get(addr, 0.0) + a["ntl"]
        for addr, ntl in sorted(by_addr.items(), key=lambda kv: -kv[1]):
            if ntl < thr or addr in self._probing:
                continue
            if len(self._probing) >= MAX_INFLIGHT_PROBES:
                self.probes_skipped += 1
                continue
            if await alert_recent("probe", addr, int(cfg.probe_cooldown_sec)):
                continue
            await alert_log("probe", addr)
            self._probing.add(addr)
            asyncio.create_task(self._probe(addr, ntl))

    async def _probe(self, addr: str, ntl: float) -> None:
        from ..radar.sweeper import probe_address
        try:
            n = await probe_address(self.cfg, self.client, addr)
            self.probes_ok += 1
            log.info("sonda: %s..%s ($%.0fK işlem) → %d hisse pozisyonu tazelendi",
                     addr[:8], addr[-4:], ntl / 1000, n)
        except Exception as e:
            self.probes_err += 1
            log.debug("sonda başarısız %s: %s", addr, e)
        finally:
            self._probing.discard(addr)

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

    async def _maybe_alert(self, coin, addr, side, px, notional, is_watch,
                           taker_ratio=None, n_parts=1):
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
        text = fmt.whale_fill_alert(coin, addr, side, px, pos_ntl, is_watch, record,
                                    taker_ratio=taker_ratio, n_parts=n_parts)
        try:
            prio = "high" if is_watch else "normal"
            await self.notifier.send("whale_fill", text, priority=prio, key=key)
        except Exception as e:
            log.warning("alert gönderilemedi: %s", e)
