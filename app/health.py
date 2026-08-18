"""Bekçi (watchdog) çekirdeği — sistem kendi kendini izler ve toparlar.

Üç parça:
- beat(name): her döngü başarılı turunda kalp atışı damgası (kv, 30 sn throttle)
- check(cfg): hangi görev ne kadardır sessiz — limiti aşan "sorunlu"
- watchdog_cycle(): sorunluyu Telegram'a bildir + görevi yeniden başlat,
  düzelince "kendine geldi" de; 3+ görev aynı anda düşerse tek toplu mesaj
  (muhtemel API kesintisi), spam yok.

Çökme bildirimleri supervised() içinden note_crash() ile gelir (6h dedupe).
"""
import logging
import time

from .config import Config
from .db import kv_get, kv_set, now

log = logging.getLogger("health")

BEAT_THROTTLE = 30          # aynı görev için kv yazımları arası min sn
CRASH_NOTIFY_GAP = 6 * 3600  # aynı görevin çökme bildirimi aralığı
ESCALATE_AFTER = 30 * 60     # bu kadar süredir düzelmeyen sorun critical olur

_last_beat: dict[str, float] = {}


async def beat(name: str) -> None:
    t = time.monotonic()
    if t - _last_beat.get(name, 0) < BEAT_THROTTLE:
        return
    _last_beat[name] = t
    try:
        await kv_set(f"hb:{name}", now())
    except Exception:
        pass  # kalp atışı asla görevi düşürmesin


async def note_crash(name: str, exc: Exception) -> bool:
    """Çökme kaydı; True dönerse bildirim gönderilmeli (6h dedupe)."""
    rec = await kv_get(f"crash:{name}") or {}
    ts = now()
    rec["count"] = int(rec.get("count") or 0) + 1
    rec["ts"] = ts
    rec["err"] = f"{type(exc).__name__}: {exc}"[:220]
    notify = ts - int(rec.get("notified") or 0) >= CRASH_NOTIFY_GAP
    if notify:
        rec["notified"] = ts
    await kv_set(f"crash:{name}", rec)
    return notify


def limits(cfg: Config) -> dict[str, int]:
    """Görev → izin verilen en uzun sessizlik (sn)."""
    return {
        "universe": cfg.universe_refresh_sec * 3 + 300,
        "calendar": cfg.calendar_refresh_sec * 2 + 600,
        "metrics": cfg.metrics_poll_sec * 3 + 120,
        "due": 360,
        "anomaly": cfg.anomaly_poll_sec * 4 + 120,
        "autoscan": max(cfg.auto_scan_interval_sec * 10, 1800),
        "liqwatch": cfg.liq_watch_poll_sec * 4 + 120,
        "tracker": cfg.track_poll_sec * 5 + 120,
        "lowvol": 1800,
        "bookwall": cfg.wall_poll_sec * 4 + 120,
        "sweeper": cfg.sweep_interval_sec * 5 + 120,
        "digest": 2400,
        "collector": 900,
        "telegram": 600,
    }


async def check(cfg: Config) -> dict[str, dict]:
    """Her görev için {hb, silent, limit, ok} — boot'tan beri hiç atmamışsa
    boot zamanına göre ölçülür (açılış toleransı)."""
    boot = int(await kv_get("boot_ts") or now())
    out = {}
    ts = now()
    for name, limit in limits(cfg).items():
        hb = await kv_get(f"hb:{name}")
        ref = int(hb) if hb else boot
        silent = max(0, ts - ref)
        out[name] = {"hb": hb, "silent": silent, "limit": limit,
                     "ok": silent <= limit}
    return out


async def watchdog_cycle(cfg: Config, notifier, respawn) -> dict:
    """Bir bekçi turu. respawn(name) -> bool: görevi yeniden başlatabildi mi."""
    from .telegram import format as fmt

    checks = await check(cfg)
    state = await kv_get("health_state") or {}
    ts = now()
    downs = {n for n, c in checks.items() if not c["ok"]}
    new_down = sorted(downs - set(state))
    recovered = sorted(set(state) - downs)

    # 1) Yeni sorunlar: yeniden başlat + bildir (3+ ise tek toplu mesaj)
    restarted: dict[str, bool] = {}
    for n in new_down:
        try:
            restarted[n] = bool(respawn(n))
        except Exception:
            log.exception("görev yeniden başlatılamadı: %s", n)
            restarted[n] = False
        state[n] = {"down_since": ts, "escalated": 0}
        log.warning("⚕️ %s %d dk'dır sessiz → restart=%s",
                    n, checks[n]["silent"] // 60, restarted[n])
    if notifier and new_down:
        if len(new_down) >= 3:
            await notifier.send("health", fmt.health_bulk(new_down),
                                priority="high", key="health:bulk")
        else:
            for n in new_down:
                await notifier.send(
                    "health",
                    fmt.health_down(n, checks[n]["silent"] // 60, restarted[n]),
                    priority="high", key=f"health:{n}")

    # 2) Uzayan sorunlar: 30+ dk → bir kez critical eskalasyon
    for n in sorted(downs & set(state)):
        st = state[n]
        if not st.get("escalated") and ts - st["down_since"] >= ESCALATE_AFTER:
            st["escalated"] = ts
            if notifier:
                await notifier.send(
                    "health",
                    fmt.health_still_down(n, (ts - st["down_since"]) // 60),
                    priority="critical", key=f"health:esc:{n}")

    # 3) Düzelenler
    for n in recovered:
        mins = (ts - state[n]["down_since"]) // 60
        del state[n]
        if notifier:
            await notifier.send("health", fmt.health_up(n, mins),
                                priority="normal", key=f"health:up:{n}")
        log.info("✅ %s kendine geldi (%d dk)", n, mins)

    await kv_set("health_state", state)
    return {"downs": sorted(downs), "new": new_down, "recovered": recovered}


async def snapshot(cfg: Config) -> dict:
    """/health ve /saglik için özet."""
    checks = await check(cfg)
    crashes = {}
    for name in limits(cfg):
        rec = await kv_get(f"crash:{name}")
        if rec:
            crashes[name] = rec
    return {"checks": checks, "crashes": crashes,
            "problems": sorted(n for n, c in checks.items() if not c["ok"])}
