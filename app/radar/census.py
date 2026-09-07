"""Kripto dex sayımı (census) — leaderboard'daki HER hesabın defteri kripto
dex'lerde (para), günde bir. Varsayılan KAPALI (`crypto_dex_census_enabled`).

Neden: HL'de "marketteki tüm pozisyonlar" API'si yok; havuz ancak gördüğümüz
adresler kadar. Dış ısı haritalarıyla eşitliğe en yakın şey, bakiyesi tabanın
üstündeki ve son hafta işlem yapmış her hesabı o dex'te tek tek sormak — adres
başına dex başına 1 istek (`clearinghouseState`).

Maliyet: N adres ≈ N / `census_rpm` dakika (20.000 adres, 60/dk ≈ 5,5 saat,
küresel 350/dk bütçesinin %17'si; süpürücü yetişme modu o sırada kendiliğinden
yavaşlar). Kapalıyken hiç istek yok.

Güvenlik: yanıtın otoritesi YALNIZ sorgulanan dex (`_upsert_address(dexes=[dex])`):
para satırı yazılır/silinir, xyz ve ana dex satırlarına dokunulmaz. Bozuk/boş
yanıt (assetPositions yok) hata sayılır, kayıt silmez. Kaldığı yerden devam eder
(kv `census_state`, 50 adreste bir); leaderboard her koşuda yeniden çekilir, sıra
bakiyeye göre olduğu için devam indeksi yaklaşıktır (belgelendi, kabul edildi).
"""
import asyncio
import logging
from datetime import datetime, timezone

from ..db import db, kv_get, kv_set, now
from ..health import beat
from . import sweeper as _sw

log = logging.getLogger("radar.census")

STATE_KV = "census_state"
STATS_KV = "census_stats"
START_DELAY = 300      # açılışta evren + leaderboard otursun
IDLE_SEC = 600         # kapalıyken / bitmişken bekleme
STATE_EVERY = 50       # kaç adreste bir ilerleme kaydı


def today_key(ts: int | None = None) -> str:
    return datetime.fromtimestamp(ts or now(), tz=timezone.utc).strftime("%Y-%m-%d")


def rows_above(data, floor: float) -> list[str]:
    """Leaderboard satırlarından sayıma girecek adresler: accountValue ≥ taban ve
    (varsa) son hafta hacmi > 0; bakiyeye göre azalan, tekrarsız, küçük harf."""
    rows = (data or {}).get("leaderboardRows") if isinstance(data, dict) else None
    out: list[tuple[float, str]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        addr = (r.get("ethAddress") or "").lower()
        if not addr:
            continue
        try:
            av = float(r.get("accountValue") or 0)
        except (TypeError, ValueError):
            continue
        if av < floor:
            continue
        wp = r.get("windowPerformances")
        if isinstance(wp, list):
            vol = None
            for item in wp:
                try:
                    name, d = item[0], item[1]
                except (TypeError, IndexError, KeyError):
                    continue
                if name == "week" and isinstance(d, dict):
                    try:
                        vol = float(d.get("vlm") or 0)
                    except (TypeError, ValueError):
                        vol = None
            if vol is not None and vol <= 0:
                continue                      # hiç işlem yapmayan hesap: pozisyon olasılığı düşük
        out.append((av, addr))
    out.sort(key=lambda x: -x[0])
    seen: set[str] = set()
    res: list[str] = []
    for _, a in out:
        if a not in seen:
            seen.add(a)
            res.append(a)
    return res


def due(cfg, state: dict | None, today: str) -> bool:
    """Bugün koşulmalı mı: açık VE (hiç koşmamış / gün değişmiş / yarım kalmış)."""
    if not getattr(cfg, "crypto_dex_census_enabled", False):
        return False
    if not state or state.get("day") != today:
        return True
    return not state.get("finished")


async def run(cfg, client, *, sleep=asyncio.sleep) -> dict:
    """Bir günlük sayım: her kripto dex için uygun adreslerin defteri o dex'te."""
    from .. import assets
    dexes = assets.crypto_dexes(cfg)
    day, t0 = today_key(), now()
    stats: dict = {"day": day, "dexes": {}, "ts": t0, "skipped": ""}
    if not dexes:
        stats["skipped"] = "kripto dex yok (CRYPTO_DEXES boş)"
        await kv_set(STATS_KV, stats)
        await kv_set(STATE_KV, {"day": day, "finished": True, "finished_dexes": [], "ts": now()})
        return stats
    data = await client.leaderboard()
    if not data:
        stats["skipped"] = "leaderboard alınamadı"
        await kv_set(STATS_KV, stats)
        await kv_set(STATE_KV, {"day": day, "finished": False, "finished_dexes": [], "ts": now(),
                                "err": "leaderboard alınamadı"})
        return stats
    addrs = rows_above(data, float(getattr(cfg, "census_min_account_value", 1000) or 0))
    rpm = max(1, int(getattr(cfg, "census_rpm", 60) or 60))
    delay = 60.0 / rpm
    prev = await kv_get(STATE_KV) or {}
    resume = prev if prev.get("day") == day and not prev.get("finished") else {}
    finished_dexes: list[str] = list(resume.get("finished_dexes") or [])
    for dex in dexes:
        if dex in finished_dexes:
            continue
        async with db() as conn:
            cur = await conn.execute("SELECT coin, symbol FROM tickers WHERE dex=?", (dex,))
            rows = await cur.fetchall()
        coin_set = {r["coin"] for r in rows}
        sym_map = {(r["symbol"] or "").upper(): r["coin"] for r in rows}
        start = int(resume.get("done") or 0) if resume.get("dex") == dex else 0
        ok = found = err = 0
        n, t_dex = len(addrs), now()
        for i in range(start, n):
            addr = addrs[i]
            await beat("census")
            await sleep(delay)
            try:
                resp = await client.clearinghouse(addr, dex)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                err += 1
                log.debug("sayım %s %s: %s", dex, addr[:10], e)
                continue
            if not isinstance(resp, dict) or "assetPositions" not in resp:
                err += 1                      # bozuk/boş yanıt: kayıt silme
                continue
            positions, valid = _sw._parse_equity_positions({dex: resp}, coin_set, sym_map, _sw._pos_floor(cfg))
            if not valid:
                err += 1
                continue
            ok += 1
            found += len(positions)
            if coin_set:
                await _sw._upsert_address(addr, positions, now(), dexes=[dex])
            if (i + 1) % STATE_EVERY == 0:
                await kv_set(STATE_KV, {"day": day, "dex": dex, "done": i + 1, "total": n, "ok": ok,
                                        "found": found, "err": err, "ts": now(), "finished": False,
                                        "finished_dexes": finished_dexes})
        finished_dexes.append(dex)
        stats["dexes"][dex] = {"n": n, "ok": ok, "found": found, "err": err,
                               "min": round((now() - t_dex) / 60, 1)}
        await kv_set(STATE_KV, {"day": day, "dex": dex, "done": n, "total": n, "ok": ok, "found": found,
                                "err": err, "ts": now(), "finished": False, "finished_dexes": finished_dexes})
    stats["min"] = round((now() - t0) / 60, 1)
    stats["ts"] = now()
    await kv_set(STATS_KV, stats)
    await kv_set(STATE_KV, {"day": day, "finished": True, "finished_dexes": finished_dexes, "ts": now()})
    log.info("sayım bitti (%s): %s", day, ", ".join(
        f"{d} {v['n']} adres → {v['found']} poz ({v['err']} hata)" for d, v in stats["dexes"].items()))
    return stats


async def loop(cfg, client) -> None:
    await asyncio.sleep(START_DELAY)
    while True:
        try:
            await beat("census")              # kapalıyken de nabız: bekçi görevi ölü sanmasın
            if due(cfg, await kv_get(STATE_KV) or {}, today_key()):
                await run(cfg, client)
                await beat("census")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sayım hatası")
        await asyncio.sleep(IDLE_SEC)
