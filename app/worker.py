"""Sayım worker'ı — `python -m app.worker` (Dockerfile: ROLE=census-worker).

DB'siz ikinci/üçüncü… servis: ana uygulamadan adres kiralar
(GET /api/census/lease), HL'ye KENDİ istemcisi/IP'siyle sorar (toplu sorgu
sondası kendi, hız kendi — 429 görürse kendi yarıya iner), sonucu
POST /api/census/ingest ile geri yollar; ana uygulama süpürücüyle aynı
yazıcılarla yazar. Ana uygulama worker gördüğünde kendi sayım hızını
`census_rpm_local`'a indirir.

Kurulum (Railway): aynı repodan N servis; env ROLE=census-worker, MAIN_URL
(ana uygulamanın https adresi), WORKER_TOKEN (ana uygulamadaki ile aynı),
isteğe bağlı CENSUS_RPM / CENSUS_BATCH_SIZE / WORKER_LEASE_N / WORKER_NAME.
`/health` (PORT) Railway healthcheck'i için buradan verilir.

Dürüst not: servislerin çıkış IP'si paylaşımlıysa HL 429 döner ve herkes
yavaşlar — kazanım tek IP bütçesiyle sınırlı kalır. Toplu sorgu çalışıyorsa
(tur ≈ 4 dk) worker'a gerek yoktur; /tani "sayım" satırı söyler.
"""
import asyncio
import logging
import os
import socket
import sys
import time

import aiohttp

from .config import get_config
from .hl.client import HLClient
from .radar import census

log = logging.getLogger("worker")

POST_CHUNK = 200            # bir ingest gövdesinde en çok bu kadar sonuç
WAIT_EMPTY = 30             # tur yok / kiralanacak hesap yok → bu kadar bekle
WAIT_ERROR = 60             # ana uygulamaya ulaşılamadı → bu kadar bekle
STATS: dict = {"started": time.time(), "leases": 0, "accounts": 0, "ok": 0, "err": 0,
               "requests": 0, "n_429": 0, "mode": None, "last_lease_ts": None,
               "last_ingest_ts": None, "last_error": ""}


def worker_name(cfg) -> str:
    return (getattr(cfg, "worker_name", "") or os.getenv("RAILWAY_REPLICA_ID", "")
            or os.getenv("RAILWAY_SERVICE_NAME", "") or socket.gethostname())[:40]


def trim_state(state) -> dict | None:
    """Ana uygulamaya yalnız gereken kısım: assetPositions + marginSummary.accountValue.
    Bozuk/boş state → None (ana uygulama hata sayar, kayıt silmez)."""
    if not isinstance(state, dict) or "assetPositions" not in state:
        return None
    ms = state.get("marginSummary")
    av = ms.get("accountValue") if isinstance(ms, dict) else None
    return {"assetPositions": state.get("assetPositions") or [], "marginSummary": {"accountValue": av}}


def _headers(cfg) -> dict:
    return {"X-Worker-Token": getattr(cfg, "worker_token", "") or ""}


async def api_get(session, cfg, path: str, params: dict) -> dict:
    async with session.get(f"{cfg.main_url}{path}", params=params, headers=_headers(cfg),
                           timeout=aiohttp.ClientTimeout(total=60)) as r:
        if r.status != 200:
            raise RuntimeError(f"GET {path} HTTP {r.status}: {(await r.text())[:200]}")
        return await r.json()


async def api_post(session, cfg, path: str, params: dict, body: dict) -> dict:
    async with session.post(f"{cfg.main_url}{path}", params=params, json=body, headers=_headers(cfg),
                            timeout=aiohttp.ClientTimeout(total=120)) as r:
        if r.status != 200:
            raise RuntimeError(f"POST {path} HTTP {r.status}: {(await r.text())[:200]}")
        return await r.json()


async def one_lease(cfg, session, client, state: dict, *, sleep=asyncio.sleep) -> int:
    """Bir kira: kirala → sorgula → yolla. Döner: kiralanan hesap sayısı (0 = tur yok / boş)."""
    name = worker_name(cfg)
    lease = await api_get(session, cfg, "/api/census/lease",
                          {"worker": name, "n": int(getattr(cfg, "worker_lease_n", 500) or 500)})
    STATS["last_lease_ts"] = time.time()
    STATS["leases"] += 1
    accounts = lease.get("accounts") or []
    if not accounts:
        state["wait"] = int(lease.get("wait") or WAIT_EMPTY)
        return 0
    addrs = [str(x.get("a") or "").lower() for x in accounts if isinstance(x, dict) and x.get("a")]
    vals = {str(x.get("a") or "").lower(): x.get("v") for x in accounts if isinstance(x, dict)}
    # Mod: KENDİ sondası (kendi IP'si, kendi istemcisi); tek bulunduysa PROBE_TTL sonra yeniden
    if state.get("mode") is None or (state["mode"] == "single"
                                     and time.time() - float(state.get("probe_ts") or 0) >= census.PROBE_TTL):
        mode, why = await census.probe_batch(client, addrs[:2])
        state.update(mode=mode, probe_ts=time.time(), why=why)
        log.info("worker %s modu: %s%s", name, census.MODE_TR.get(mode, mode), f" ({why})" if why else "")
    pace = state.get("pace")
    if pace is None:
        pace = state["pace"] = census.Pace(int(getattr(cfg, "census_rpm", 250) or 250))
    fetcher = census.Fetcher(client, state["mode"], int(getattr(cfg, "census_batch_size", 50) or 50),
                             pace, sleep=sleep)
    hip3_floor = float(lease.get("hip3_floor") or 0)
    results: list[dict] = []
    t0 = time.time()
    for dex in lease.get("dexes") or [""]:
        dex = str(dex or "")
        sub = addrs if dex == "" else [a for a in addrs if (vals.get(a) or 0) >= hip3_floor]
        if not sub:
            continue
        for a, st in await fetcher.fetch(sub, dex):
            results.append({"a": a, "dex": dex, "state": trim_state(st)})
    if fetcher.fell_back:
        state.update(mode="single", probe_ts=time.time(), why="kira içinde üst üste toplu hata")
    STATS["mode"] = state["mode"]
    STATS["requests"] += fetcher.requests
    STATS["n_429"] = pace.n_429
    ok = err = 0
    for i in range(0, len(results), POST_CHUNK):
        resp = await api_post(session, cfg, "/api/census/ingest", {"worker": name},
                              {"pass_ts": lease.get("pass_ts"), "results": results[i:i + POST_CHUNK]})
        ok += int(resp.get("ok_n") or 0)
        err += int(resp.get("err") or 0)
    STATS["accounts"] += len(addrs)
    STATS["ok"] += ok
    STATS["err"] += err
    STATS["last_ingest_ts"] = time.time()
    log.info("kira: %d hesap → %d ok, %d hata · %s · %d istek · %.0f sn · 429: %d",
             len(addrs), ok, err, census.MODE_TR.get(state["mode"], state["mode"]), fetcher.requests,
             time.time() - t0, pace.n_429)
    return len(addrs)


async def run(cfg, session, client, *, sleep=asyncio.sleep, once: bool = False) -> int:
    """Sonsuz döngü (once=True: tek kira, test). Ana uygulamaya ulaşılamazsa bekler, düşmez."""
    state: dict = {}
    while True:
        n = 0
        try:
            n = await one_lease(cfg, session, client, state, sleep=sleep)
            STATS["last_error"] = ""
        except asyncio.CancelledError:
            raise
        except Exception as e:
            STATS["last_error"] = f"{type(e).__name__}: {e}"[:200]
            log.warning("worker hatası: %s", STATS["last_error"])
            state["wait"] = WAIT_ERROR
        if once:
            return n
        if n == 0:
            await sleep(int(state.get("wait") or WAIT_EMPTY))


def health_payload() -> dict:
    # STATS'ın "ok" sayacı üst düzey "ok" bayrağını ezmesin: sayaçlar "stats" altında
    return {"ok": True, "role": "census-worker", "uptime_sec": int(time.time() - STATS["started"]),
            "stats": {k: v for k, v in STATS.items() if k != "started"}}


async def serve_health(port: int):
    """Railway healthcheck (/health) — worker'ın kendi küçük HTTP sunucusu."""
    from aiohttp import web

    async def health(_req):
        return web.json_response(health_payload())

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    cfg = get_config()
    if not getattr(cfg, "main_url", "") or not getattr(cfg, "worker_token", ""):
        log.error("worker için MAIN_URL ve WORKER_TOKEN gerekli (ana uygulamada da aynı WORKER_TOKEN)")
        sys.exit(2)
    port = int(os.getenv("PORT", "8000"))
    async with aiohttp.ClientSession() as session:
        client = HLClient(session, cfg.api_base, cfg.stats_leaderboard_url,
                          concurrency=cfg.scan_concurrency, max_rpm=cfg.hl_max_rpm)
        runner = await serve_health(port)
        log.info("sayım worker'ı %s: ana %s · %d istek/dk · kira %d hesap · /health :%d",
                 worker_name(cfg), cfg.main_url, cfg.census_rpm, cfg.worker_lease_n, port)
        try:
            await run(cfg, session, client)
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
