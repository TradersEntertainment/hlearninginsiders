"""Hyperliquid info API istemcisi — pacing + retry + küresel istek bütçesi."""
import asyncio
import logging
import random
import time
from collections import deque

import aiohttp

log = logging.getLogger("hl.client")


class HLClient:
    def __init__(self, session: aiohttp.ClientSession, base: str, leaderboard_url: str,
                 concurrency: int = 8, min_interval: float = 0.08,
                 max_rpm: int = 350, rpm_window: float = 60.0):
        self.session = session
        self.base = base.rstrip("/")
        self.leaderboard_url = leaderboard_url
        self._sem = asyncio.Semaphore(concurrency)
        self._min_interval = min_interval
        # Küresel bütçe: TÜM görevlerin paylaştığı istek/dakika tavanı.
        # 429 fırtınası + exponential backoff cezası yerine kibarca kuyruk.
        self._max_rpm = max_rpm
        self._rpm_window = rpm_window
        self._req_times: deque[float] = deque()
        self._rl_lock = asyncio.Lock()

    async def _acquire_budget(self) -> None:
        while True:
            async with self._rl_lock:
                t = time.monotonic()
                while self._req_times and t - self._req_times[0] >= self._rpm_window:
                    self._req_times.popleft()
                if len(self._req_times) < self._max_rpm:
                    self._req_times.append(t)
                    return
                wait = self._req_times[0] + self._rpm_window - t
            await asyncio.sleep(max(wait, 0.01))

    def usage(self) -> dict:
        """Son pencerede kaç istek yapıldı — süpürücü BOŞTAKİ bütçeyi kullansın.

        Sayaç zaten `_acquire_budget` için tutuluyor; burada yalnız okunuyor.
        Süresi dolmuş damgalar temizlenir (await yok — tek iş parçacıklı
        döngüde bölünmez).
        """
        t = time.monotonic()
        while self._req_times and t - self._req_times[0] >= self._rpm_window:
            self._req_times.popleft()
        used = len(self._req_times)
        return {"rpm": used, "max": self._max_rpm,
                "free": max(0, self._max_rpm - used),
                "window": self._rpm_window}

    async def info(self, payload: dict, retries: int = 4):
        url = f"{self.base}/info"
        delay = 1.0
        await self._acquire_budget()
        async with self._sem:
            for attempt in range(retries + 1):
                try:
                    async with self.session.post(url, json=payload,
                                                 timeout=aiohttp.ClientTimeout(total=20)) as r:
                        if r.status == 200:
                            data = await r.json()
                            await asyncio.sleep(self._min_interval)
                            return data
                        if r.status in (429, 500, 502, 503, 504) and attempt < retries:
                            await asyncio.sleep(delay + random.random())
                            delay = min(delay * 2, 30)
                            continue
                        body = (await r.text())[:200]
                        raise RuntimeError(f"HL info {payload.get('type')} HTTP {r.status}: {body}")
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    if attempt < retries:
                        await asyncio.sleep(delay + random.random())
                        delay = min(delay * 2, 30)
                        continue
                    raise RuntimeError(f"HL info {payload.get('type')} ağ hatası: {e}") from e

    # ---- sarmalayıcılar ----

    async def perp_dexs(self):
        return await self.info({"type": "perpDexs"})

    async def meta(self, dex: str = ""):
        p = {"type": "meta"}
        if dex:
            p["dex"] = dex
        return await self.info(p)

    async def meta_and_ctxs(self, dex: str = ""):
        p = {"type": "metaAndAssetCtxs"}
        if dex:
            p["dex"] = dex
        return await self.info(p)

    async def clearinghouse(self, user: str, dex: str = ""):
        p = {"type": "clearinghouseState", "user": user}
        if dex:
            p["dex"] = dex
        return await self.info(p)

    async def clearinghouse_all(self, user: str, dexes: list[str]) -> dict:
        """Adresin BÜTÜN dex'lerdeki defteri — {dex: state} sözlüğü.

        `dex="ALL_DEXES"` diye bir kestirme YOK: canlıda partinin %100'ü hata
        dönüyordu (HL o değeri kabul etmiyor), bu yüzden derin keşif hiçbir şey
        yazamıyor ve "HL'nin en büyükleri" paneli hiç dolmuyordu. Artık her dex
        tek tek ve GERÇEK adıyla sorgulanıyor — earnings tarayıcısının yıllardır
        sorunsuz kullandığı yol.

        Dex'lerden HERHANGİ biri patlarsa HATA YÜKSELİR — sessizce boş/None
        dönmez. Eksik yanıtla devam etmek "adres artık o pozisyonu tutmuyor"
        demektir ve kayıtları SİLERDİ; eksik veri, veri olmamasından daha
        tehlikelidir. Çağıranların hepsi zaten hatayı yakalayıp o adresi bu tur
        atlıyor.
        """
        out: dict[str, dict] = {}
        for dex in dexes:
            resp = await self.clearinghouse(user, dex)
            if not isinstance(resp, dict):
                raise RuntimeError(
                    f"clearinghouseState(dex={dex!r}) beklenmeyen yanıt: {type(resp).__name__}")
            out[dex or "main"] = resp
        if not out:
            raise RuntimeError("clearinghouse_all: sorgulanacak dex yok")
        return out

    async def user_fills_by_time(self, user: str, start_ms: int, end_ms: int | None = None):
        p = {"type": "userFillsByTime", "user": user, "startTime": start_ms}
        if end_ms:
            p["endTime"] = end_ms
        return await self.info(p)

    async def ledger_updates(self, user: str, start_ms: int):
        return await self.info({"type": "userNonFundingLedgerUpdates",
                                "user": user, "startTime": start_ms})

    async def recent_trades(self, coin: str):
        return await self.info({"type": "recentTrades", "coin": coin})

    async def candles(self, coin: str, interval: str, start_ms: int, end_ms: int):
        """Mum verisi — saat istatistiği için (1h, ~90 gün tek istekte)."""
        return await self.info({"type": "candleSnapshot",
                                "req": {"coin": coin, "interval": interval,
                                        "startTime": start_ms, "endTime": end_ms}})

    async def l2_book(self, coin: str):
        """Emir defteri (anonim toplam derinlik) — levels: [bids, asks]."""
        return await self.info({"type": "l2Book", "coin": coin})

    async def frontend_open_orders(self, user: str, dex: str = ""):
        """Adresin açık (bekleyen) emirleri — duvar sahipliğini bulmak için."""
        p = {"type": "frontendOpenOrders", "user": user}
        if dex:
            p["dex"] = dex
        return await self.info(p)

    async def vault_details(self, address: str):
        """Adres bir vault ise detay döner, değilse null."""
        return await self.info({"type": "vaultDetails", "vaultAddress": address})

    async def leaderboard(self):
        """Resmi olmayan leaderboard — soft dependency, hata yutulur (None döner)."""
        try:
            async with self.session.get(self.leaderboard_url,
                                        timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status != 200:
                    log.warning("leaderboard HTTP %s", r.status)
                    return None
                return await r.json()
        except Exception as e:
            log.warning("leaderboard alınamadı: %s", e)
            return None
