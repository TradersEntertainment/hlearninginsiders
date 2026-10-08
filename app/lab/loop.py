"""🧪 Laboratuvar döngüsü — TEK görev (düşük öncelik): kayıt, kuyruk boşaltma, çözücü, veri dolumu.

  açılış     registry.sync (kurallar kaydolur / hash değişen emekli)
  30 sn      flush (kuyruk 200'ü geçerse hemen) + canlı (onaylı) kural olaylarının teslimi + nabız
  5 dk       resolver.resolve_due
  60 sn      derin dolum adımı (1h ~208 gün, 1d) — ağırlık bütçesi izin verdikçe
  saatlik    planlı bakışlar (looks.run: durum YALNIZ burada değişir), evren tazeleme, kısa dilim
             budaması, durum kv'si (/tani)
  günlük     1d mum tamamlama (bütçeyle, turlara yayılır)
HL isteklerinin hepsi lab bütçesinden (dakikada ≤ LAB_WEIGHT_MIN ağırlık) ve düşük şeritten; canlı
radarlar lab yüzünden 429 yemez. Strateji ASLA aramaz: bu paket wake'i içe aktarmaz.
"""
from __future__ import annotations

import asyncio
import logging

from ..db import kv_set, now
from . import data, deliver, looks, registry, resolver

log = logging.getLogger("lab.loop")

TICK = 30
RESOLVE_EVERY = 300
DEEP_EVERY = 60
HOUR = 3600
STATS_KV = "lab_stats"


class State:
    def __init__(self):
        self.last = {"resolve": 0, "deep": 0, "hour": 0}
        self.synced = False
        self.sync_out: dict = {}
        self.resolve_out: dict = {}
        self.deep_out: dict = {}
        self.d1_day = ""
        self.d1_cursor = 0
        self.err = ""
        self.looks_out: list = []
        self.errs: dict = {}


async def _guard(st: State, name: str, coro):
    """Aşama yalıtımı: bir aşamanın hatası (HL 500, kilit) ötekileri durdurmaz; hata durum kv'sinde."""
    try:
        return await coro
    except asyncio.CancelledError:
        raise
    except Exception as e:                           # noqa: BLE001
        st.errs[name] = f"{type(e).__name__}: {e}"[:160]
        log.exception("lab aşaması: %s", name)
        return None


async def step(cfg, client, budget: data.Budget, st: State, t: int | None = None, notifier=None) -> None:
    """Bir tur (test edilebilir): sırası önemli — önce kuyruk, sonra çözücü (iş birikince HER turda),
    derin dolum yalnız çözücü yetişmişken (bütçe önce ölçüme)."""
    t = int(t or now())
    st.errs = {}
    if not getattr(cfg, "lab_enabled", True):
        if st.synced:
            registry.ACTIVE.clear()                 # kapalı: emit reddedilir, kuyruk boşaltılır
            st.synced = False
        await registry.flush(ts=t)
        return
    if not st.synced:
        st.sync_out = await registry.sync(cfg, ts=t)
        st.synced = True
        if st.sync_out.get("new") or st.sync_out.get("retired"):
            log.info("lab kayıt: yeni %s · emekli %s", st.sync_out["new"], st.sync_out["retired"])
    await registry.flush(ts=t)
    await _guard(st, "teslim", deliver.send_live(notifier, cfg, registry.take_live()))
    if t - st.last["hour"] >= HOUR:
        st.last["hour"] = t
        await _guard(st, "evren", data.lab_coins(cfg, t))
        await _guard(st, "budama", data.prune(t))

        async def _pass(rec):
            return await deliver.on_pass(notifier, cfg, rec)

        async def _stop(rec):
            return await deliver.on_stop(notifier, cfg, rec)
        st.looks_out = (await _guard(st, "bakış", looks.run(t, on_pass=_pass, on_stop=_stop))) or st.looks_out
        await _guard(st, "onay", deliver.resend_pending(notifier, cfg, t))
    backlog = int((st.resolve_out or {}).get("backlog") or 0)
    if backlog or t - st.last["resolve"] >= RESOLVE_EVERY:
        st.last["resolve"] = t
        st.resolve_out = (await _guard(st, "çözücü", resolver.resolve_due(client, budget, t))) or st.resolve_out
        backlog = int((st.resolve_out or {}).get("backlog") or 0)
    if not backlog and t - st.last["deep"] >= DEEP_EVERY:
        st.last["deep"] = t
        coins = sorted(await data.lab_coins(cfg, t))
        st.deep_out = (await _guard(st, "derin dolum", data.deep_fill_step(client, budget, coins, t, max_n=1))) \
            or st.deep_out
        await _guard(st, "1d", _daily_1d(client, budget, coins, st, t))
    st.err = "; ".join(f"{k}: {v}" for k, v in st.errs.items())[:300]
    await kv_set(STATS_KV, {"ts": t, "reg": registry.stats_line(), "sync": st.sync_out,
                            "resolve": st.resolve_out, "deep": st.deep_out, "err": st.err,
                            "looks": st.looks_out[-5:],
                            "budget": {"spent": budget.spent_total, "denied": budget.denied,
                                       "per_min": budget.per_min}})


async def _daily_1d(client, budget: data.Budget, coins: list[str], st: State, t: int) -> None:
    """Günde bir: derin dolumu bitmiş coinlerin 1d serisini tamamla (kalan tura kalır)."""
    from datetime import datetime, timezone
    day = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
    if st.d1_day != day:
        st.d1_day, st.d1_cursor = day, 0
    done = await _deep_done()
    while st.d1_cursor < len(coins):
        coin = coins[st.d1_cursor]
        if f"{coin}|86400" in done:
            r = await data.ensure_window(client, coin, 86400, t - 5 * 86400, t, budget, t)
            if r is None:
                return
        st.d1_cursor += 1


async def _deep_done() -> dict:
    from ..db import kv_get
    return (await kv_get(data.DEEP_KV)) or {}


async def loop(cfg, client, notifier=None) -> None:
    from ..health import beat
    from ..hl.client import PRIORITY
    PRIORITY.set("low")
    budget = data.Budget(int(getattr(cfg, "lab_weight_min", data.WEIGHT_MIN) or data.WEIGHT_MIN))
    st = State()
    try:
        while True:
            try:
                await step(cfg, client, budget, st, notifier=notifier)
            except asyncio.CancelledError:
                raise
            except Exception as e:                   # noqa: BLE001 — tur başına; döngü sürer
                st.err = f"{type(e).__name__}: {e}"[:160]
                log.exception("lab turu")
            await beat("lab")
            await asyncio.sleep(5 if registry.pending() > 200 else TICK)
    except asyncio.CancelledError:
        await shutdown_flush()                       # kapanış/deploy: kuyruktaki karar anı kayıtları kaybolmasın
        raise


async def shutdown_flush(timeout: float = 5.0) -> int:
    """Kuyruğu son kez yaz (sınırlı süre). main.lifespan da kapanışta çağırır; ikinci çağrı boş döner."""
    try:
        return await asyncio.wait_for(registry.flush(limit=registry.QUEUE_MAX), timeout)
    except BaseException:                            # noqa: BLE001 — kapanışta asla takılma
        log.warning("lab kuyruğu kapanışta yazılamadı (%d olay)", registry.pending())
        return 0
