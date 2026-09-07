"""Hyperliquid info API istemcisi — pacing + retry + küresel istek bütçesi.

İki öncelik şeridi: "normal" (radarlar, kullanıcı sorguları) bütçeyi bugünkü gibi
kullanır; "low" (sayım, süpürücü yetişme modu) yalnız pencere kullanımı
LOW_SHARE'in altındayken ilerler ve HL'den herhangi bir 429 gelince
LOW_PAUSE_SEC susar — arka plan işi artan kapasiteyi kullanır, fırtınada çekilir.
Öncelik ya çağrıda (`priority=`) ya da görev bağlamında (`PRIORITY` contextvar,
süpürücü partisi) verilir.

Ağırlık (HL belgesi: 1200 ağırlık/dk/IP; l2Book, allMids, clearinghouseState,
orderStatus, spotClearinghouseState, exchangeStatus 2; userRole 60; diğerleri 20;
batchClearinghouseStates belgesiz → 20 varsayılır) yalnız SAYAÇ içindir
(`usage()["weight"]`, /tani): bütçe istek sayısıyla uygulanır — canlıdaki ölçüm
bize gerçek sınırı öğretir, tahminle radarları yavaşlatmayız.
"""
import asyncio
import contextvars
import logging
import random
import time
from collections import deque

import aiohttp

log = logging.getLogger("hl.client")

PRIORITY: contextvars.ContextVar[str] = contextvars.ContextVar("hl_priority", default="normal")
WEIGHTS = {"l2Book": 2, "allMids": 2, "clearinghouseState": 2, "orderStatus": 2,
           "spotClearinghouseState": 2, "exchangeStatus": 2, "userRole": 60}
DEFAULT_WEIGHT = 20
HL_WEIGHT_LIMIT = 1200      # HL: dakikada IP başına toplam ağırlık (belge)
LOW_SHARE = 0.70            # düşük şerit: pencere kullanımı bunun üstündeyse bekler
LOW_PAUSE_SEC = 60.0        # herhangi bir 429'dan sonra düşük şerit bu kadar susar
# Uyarlanır tavan (AIMD): HL'nin gerçek hesabını 429 öğretir. Her 429'da etkin tavan
# CAP_DOWN ile iner (fırtınada 10 sn'de en çok bir adım), CAP_UP_EVERY sn 429'suz her
# dakika CAP_UP_STEP çıkar; hl_max_rpm üst sınır, CAP_FLOOR alt sınır.
CAP_FLOOR = 120
CAP_DOWN = 0.8
CAP_DOWN_MIN_GAP = 10.0
CAP_UP_STEP = 25
CAP_UP_EVERY = 60.0


def weight_of(payload: dict) -> int:
    return WEIGHTS.get(str((payload or {}).get("type") or ""), DEFAULT_WEIGHT)


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
        self._w_times: deque[tuple[float, int]] = deque()   # (damga, ağırlık) — yalnız sayaç
        self._rl_lock = asyncio.Lock()
        # HL'den gelen 429 sayısı (kümülatif) ve sonuncusunun anı: /tani yazar,
        # düşük şerit LOW_PAUSE_SEC susar. Retry içinde yutulan 429'lar da sayılır.
        self.n_429 = 0
        self._last_429 = 0.0
        self._cap = float(max_rpm)          # etkin tavan (AIMD)
        self._cap_down_ts = 0.0
        self._cap_up_ts = time.monotonic()

    def _note_429(self) -> None:
        t = time.monotonic()
        self.n_429 += 1
        self._last_429 = t
        if t - self._cap_down_ts >= CAP_DOWN_MIN_GAP:
            self._cap = max(float(CAP_FLOOR), self._cap * CAP_DOWN)
            self._cap_down_ts = t
            log.warning("HL 429 — etkin tavan %.0f istek/dk", self._cap)

    def _cap_tick(self, t: float) -> None:
        """Sessizlikte kademeli geri: son 429'dan ve son artıştan CAP_UP_EVERY geçtiyse +CAP_UP_STEP."""
        if (self._cap < self._max_rpm and t - self._last_429 >= CAP_UP_EVERY
                and t - self._cap_up_ts >= CAP_UP_EVERY):
            self._cap = min(float(self._max_rpm), self._cap + CAP_UP_STEP)
            self._cap_up_ts = t

    def _trim(self, t: float) -> None:
        while self._req_times and t - self._req_times[0] >= self._rpm_window:
            self._req_times.popleft()
        while self._w_times and t - self._w_times[0][0] >= self._rpm_window:
            self._w_times.popleft()

    def low_paused(self, t: float | None = None) -> float:
        """Düşük şeridin 429 sonrası kalan bekleme süresi (sn); 0 = açık."""
        t = time.monotonic() if t is None else t
        return max(0.0, self._last_429 + LOW_PAUSE_SEC - t) if self._last_429 else 0.0

    async def _acquire_budget(self, priority: str = "normal", weight: int = DEFAULT_WEIGHT) -> None:
        low = priority == "low"
        while True:
            async with self._rl_lock:
                t = time.monotonic()
                self._trim(t)
                self._cap_tick(t)
                used = len(self._req_times)
                if low:
                    pause = self.low_paused(t)
                    if pause > 0:
                        wait = pause
                    elif used < self._cap * LOW_SHARE:
                        self._req_times.append(t)
                        self._w_times.append((t, int(weight)))
                        return
                    else:
                        wait = (self._req_times[0] + self._rpm_window - t) if self._req_times else 0.25
                elif used < self._cap:
                    self._req_times.append(t)
                    self._w_times.append((t, int(weight)))
                    return
                else:
                    wait = self._req_times[0] + self._rpm_window - t
            await asyncio.sleep(min(max(wait, 0.01), 5.0))

    def usage(self) -> dict:
        """Son pencerede kaç istek yapıldı — süpürücü BOŞTAKİ bütçeyi kullansın.

        Sayaç zaten `_acquire_budget` için tutuluyor; burada yalnız okunuyor.
        Süresi dolmuş damgalar temizlenir (await yok — tek iş parçacıklı
        döngüde bölünmez). `weight`: son dakikanın TAHMİNİ HL ağırlığı (belge
        tablosu), `low_paused`: düşük şeridin 429 sonrası kalan beklemesi.
        """
        t = time.monotonic()
        self._trim(t)
        self._cap_tick(t)
        used = len(self._req_times)
        return {"rpm": used, "max": self._max_rpm, "cap": int(round(self._cap)),
                "free": max(0, int(round(self._cap)) - used),
                "window": self._rpm_window,
                "weight": sum(w for _, w in self._w_times), "weight_max": HL_WEIGHT_LIMIT,
                "low_share": LOW_SHARE, "low_paused": round(self.low_paused(t), 1),
                "n_429": self.n_429,
                "last_429_ago": (round(t - self._last_429) if self._last_429 else None)}

    async def info(self, payload: dict, retries: int = 4, priority: str | None = None,
                   stats: dict | None = None):
        """`priority`: "normal" | "low" (verilmezse görev bağlamındaki PRIORITY).
        `stats`: çağıranın sözlüğü — her 429'da stats["429"] += 1 (kendi 429'unu
        bilsin; küresel sayaç başkalarının fırtınasını ona yüklüyordu)."""
        url = f"{self.base}/info"
        delay = 1.0
        await self._acquire_budget(priority or PRIORITY.get(), weight_of(payload))
        async with self._sem:
            for attempt in range(retries + 1):
                try:
                    async with self.session.post(url, json=payload,
                                                 timeout=aiohttp.ClientTimeout(total=20)) as r:
                        if r.status == 200:
                            data = await r.json()
                            await asyncio.sleep(self._min_interval)
                            return data
                        if r.status == 429:
                            self._note_429()
                            if stats is not None:
                                stats["429"] = int(stats.get("429", 0)) + 1
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

    async def clearinghouse(self, user: str, dex: str = "", priority: str | None = None,
                            stats: dict | None = None):
        p = {"type": "clearinghouseState", "user": user}
        if dex:
            p["dex"] = dex
        return await self.info(p, priority=priority, stats=stats)

    async def batch_clearinghouse(self, users: list[str], dex: str = "", priority: str | None = None,
                                  stats: dict | None = None):
        """TOPLU defter: `batchClearinghouseStates` — sağlayıcı belgelerinde
        (Dwellir/QuickNode/Chainstack) api.hyperliquid.xyz/info örneğiyle var,
        resmi Python SDK'da yok. Bu yüzden canlıda SONDA ile doğrulanır
        (radar/census.probe_mode): 200 + `users` sırasında state listesi →
        toplu mod; 4xx/şekil hatası → tek tek mod. Dönüş ham yanıttır."""
        p: dict = {"type": "batchClearinghouseStates", "users": list(users)}
        if dex:
            p["dex"] = dex
        return await self.info(p, retries=2, priority=priority, stats=stats)

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

    async def l2_book(self, coin: str, n_sig_figs: int | None = None):
        """Emir defteri (anonim toplam derinlik) — levels: [bids, asks].

        `n_sig_figs` (2–5): fiyat bu kadar anlamlı haneye TOPLULAŞTIRILIR; 20
        seviye daha geniş bir aralığı kapsar (zincir simülasyonu 3 kullanır).
        Duvar radarı hassas seviye ister, varsayılanı (None) kullanır."""
        p: dict = {"type": "l2Book", "coin": coin}
        if n_sig_figs:
            p["nSigFigs"] = int(n_sig_figs)
        return await self.info(p)

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
