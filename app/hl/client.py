"""Hyperliquid info API istemcisi — pacing + retry ile."""
import asyncio
import logging
import random

import aiohttp

log = logging.getLogger("hl.client")


class HLClient:
    def __init__(self, session: aiohttp.ClientSession, base: str, leaderboard_url: str,
                 concurrency: int = 8, min_interval: float = 0.08):
        self.session = session
        self.base = base.rstrip("/")
        self.leaderboard_url = leaderboard_url
        self._sem = asyncio.Semaphore(concurrency)
        self._min_interval = min_interval

    async def info(self, payload: dict, retries: int = 4):
        url = f"{self.base}/info"
        delay = 1.0
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
