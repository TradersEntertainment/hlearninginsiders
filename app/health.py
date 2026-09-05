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
from .db import alert_log, alert_recent, kv_get, kv_set, now

log = logging.getLogger("health")

BEAT_THROTTLE = 30          # aynı görev için kv yazımları arası min sn
CRASH_NOTIFY_GAP = 6 * 3600  # aynı görevin çökme bildirimi aralığı
ESCALATE_AFTER = 30 * 60     # bu kadar süredir düzelmeyen sorun critical olur
DOWN_NOTIFY_GAP = 3 * 3600   # aynı görevin "sessizdi" bildirimi aralığı (churn spam'i önler)

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
    lim = {
        "universe": cfg.universe_refresh_sec * 3 + 300,
        "calendar": cfg.calendar_refresh_sec * 2 + 600,
        "cryptovol": int(getattr(cfg, "crypto_vol_poll_sec", 300)) * 3 + 300,
        "equityvol": int(getattr(cfg, "equity_vol_poll_sec", 300)) * 3 + 300,
        "bars": int(getattr(cfg, "bars_refresh_sec", 1800)) * 3 + 600,
        "patterns": int(getattr(cfg, "pattern_scan_sec", 1800)) * 3 + 600,
        "twap": int(getattr(cfg, "twap_scan_sec", 600)) * 3 + 600,
        "liqattack": int(getattr(cfg, "liq_attack_scan_sec", 300)) * 3 + 600,
        "metrics": cfg.metrics_poll_sec * 3 + 120,
        "due": 360,
        "anomaly": cfg.anomaly_poll_sec * 4 + 120,
        "autoscan": max(cfg.auto_scan_interval_sec * 10, 1800),
        "liqwatch": cfg.liq_watch_poll_sec * 4 + 120,
        "tracker": cfg.track_poll_sec * 5 + 120,
        "lowvol": 1800,
        "bookwall": cfg.wall_poll_sec * 4 + 120,
        "sweeper": cfg.sweep_interval_sec * 5 + 120,
        "hourstats": 7200,
        "digest": 2400,
        "collector": 900,
        # AI turu 2 saatte bir; kapalıyken bile nabız atar (döngü boşta döner)
        "ai": cfg.ai_interval_sec * 3 + 600,
    }
    # telegram görevi YALNIZ bot token'ı varken spawn edilir (main.lifespan);
    # token'sız 'sadece dashboard' kurulumunda beklenirse kalp atışı hiç
    # gelmez → kalıcı sahte 'telegram sorunlu' rozeti + /health ok:false +
    # var olmayan görevi respawn denemesi. Yoksa listeye hiç koyma.
    if cfg.telegram_bot_token:
        lim["telegram"] = 600
    return lim


def periods(cfg: Config) -> dict[str, int]:
    """Görev → NORMAL çalışma aralığı (sn). `limits()` bunun toleranslı katıdır.

    Sağlık raporunda gösterilir: 30 dakikada bir koşan anomali dedektörünün
    "22dk önce" atması normaldir, ama yanında "0dk önce" yazan görevlerle yan
    yana gelince bozuk görünüyordu. Sayının anlamı ancak periyodu yanında
    yazınca okunabiliyor.
    """
    per = {
        "universe": cfg.universe_refresh_sec,
        "calendar": cfg.calendar_refresh_sec,
        "cryptovol": int(getattr(cfg, "crypto_vol_poll_sec", 300)),
        "equityvol": int(getattr(cfg, "equity_vol_poll_sec", 300)),
        "bars": int(getattr(cfg, "bars_refresh_sec", 1800)),
        "patterns": int(getattr(cfg, "pattern_scan_sec", 1800)),
        "twap": int(getattr(cfg, "twap_scan_sec", 600)),
        "liqattack": int(getattr(cfg, "liq_attack_scan_sec", 300)),
        "metrics": cfg.metrics_poll_sec,
        "due": cfg.due_check_sec,
        "anomaly": cfg.anomaly_poll_sec,
        "autoscan": cfg.auto_scan_interval_sec,
        "liqwatch": cfg.liq_watch_poll_sec,
        "tracker": cfg.track_poll_sec,
        "lowvol": 300,
        "bookwall": cfg.wall_poll_sec,
        "sweeper": cfg.sweep_interval_sec,
        "hourstats": 600,
        "digest": 600,
        "collector": 0,        # olay güdümlü: her WS mesajında atar
        "ai": cfg.ai_interval_sec,
    }
    if cfg.telegram_bot_token:
        per["telegram"] = 0    # olay güdümlü: uzun yoklama
    return per


async def silent_coins(cfg: Config) -> list[dict]:
    """Canlı akıştan uzun süredir hiç işlem GELMEYEN coin'ler.

    Abone olunduğu sanılan ama veri akmayan market (reddedilen subscribe, sessiz
    kopma, ölü/delist edilmiş coin) başka hiçbir yerde görünmüyordu. Yalnız
    BİLGİ — Telegram alarmı üretmez (sağlık sitede konuşur)."""
    from .db import db
    limit_h = max(1, int(getattr(cfg, "zombie_silent_hours", 12)))
    boot = int(await kv_get("boot_ts") or now())
    ts = now()
    if ts - boot < limit_h * 3600:
        return []                      # açılış toleransı: henüz yeterli süre geçmedi
    seen = await kv_get("coin_last_trade") or {}
    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol FROM tickers ORDER BY symbol")
        rows = [(r["coin"], r["symbol"]) for r in await cur.fetchall()]
    out = []
    for coin, sym in rows:
        last = int(seen.get(coin) or 0)
        age_h = (ts - last) / 3600 if last else None
        if last and age_h >= limit_h:
            out.append({"coin": coin, "symbol": sym, "hours": age_h})
        elif not last and ts - boot >= limit_h * 3600:
            out.append({"coin": coin, "symbol": sym, "hours": None})
    out.sort(key=lambda x: -(x["hours"] or 1e9))
    return out


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
    """Bir bekçi turu. respawn(name) -> bool: görevi yeniden başlatabildi mi.

    İki aşamalı tespit: ilk limit aşımı yalnız 'pending' notu bırakır (uzun
    ama ilerleyen cycle'lar için tolerans); BİR SONRAKİ turda da sessizse
    gerçek sorun sayılır → restart + bildirim. Bildirimler görev başına
    3 saatte bir (restart yine yapılır) — churn telefonu boğmaz.
    """
    from .telegram import format as fmt

    checks = await check(cfg)
    state = await kv_get("health_state") or {}
    ts = now()
    silent = {n for n, c in checks.items() if not c["ok"]}

    # 0) pending temizliği: bekleme aşamasındayken düzelenler sessizce çıkar
    for n in [n for n, st in state.items() if st.get("pending") and n not in silent]:
        del state[n]

    # yeni sessizler: önce pending'e al (aksiyon yok)
    confirmed_new = []
    for n in sorted(silent):
        st = state.get(n)
        if st is None:
            state[n] = {"pending": True, "since": ts}
        elif st.get("pending"):
            # ikinci turda da sessiz → onaylı sorun
            state[n] = {"down_since": st["since"], "escalated": 0, "notified": False}
            confirmed_new.append(n)

    # 1) Onaylı yeni sorunlar: yeniden başlat + (cooldown'lu) bildir
    restarted: dict[str, bool] = {}
    for n in confirmed_new:
        try:
            restarted[n] = bool(respawn(n))
        except Exception:
            log.exception("görev yeniden başlatılamadı: %s", n)
            restarted[n] = False
        log.warning("⚕️ %s %d dk'dır sessiz → restart=%s",
                    n, checks[n]["silent"] // 60, restarted[n])
    if notifier and confirmed_new:
        speak = [n for n in confirmed_new
                 if not await alert_recent("healthdown", n, DOWN_NOTIFY_GAP)]
        for n in speak:
            await alert_log("healthdown", n)
            state[n]["notified"] = True
        if len(speak) >= 3:
            await notifier.send("health", fmt.health_bulk(speak),
                                priority="high", key="health:bulk")
        else:
            for n in speak:
                await notifier.send(
                    "health",
                    fmt.health_down(n, checks[n]["silent"] // 60, restarted[n]),
                    priority="high", key=f"health:{n}")

    # 2) Uzayan sorunlar: 30+ dk → bir kez critical eskalasyon
    for n in sorted(silent & set(state)):
        st = state[n]
        if st.get("pending"):
            continue
        if not st.get("escalated") and ts - st["down_since"] >= ESCALATE_AFTER:
            st["escalated"] = ts
            if notifier:
                # high (critical DEĞİL): critical tip anahtarını bypass eder,
                # kullanıcı sağlığı kapattıysa hiçbir sağlık mesajı sızmamalı
                await notifier.send(
                    "health",
                    fmt.health_still_down(n, (ts - st["down_since"]) // 60),
                    priority="high", key=f"health:esc:{n}")

    # 3) Düzelen onaylı sorunlar: yalnız bildirilmiş olanlar "kendine geldi" der
    recovered = sorted(n for n, st in state.items()
                       if not st.get("pending") and n not in silent)
    for n in recovered:
        st = state[n]
        mins = (ts - st["down_since"]) // 60
        del state[n]
        if notifier and st.get("notified"):
            await notifier.send("health", fmt.health_up(n, mins),
                                priority="normal", key=f"health:up:{n}")
        log.info("✅ %s kendine geldi (%d dk)", n, mins)

    await kv_set("health_state", state)
    # Uyarı halkasını burada kalıcılaştır: işi zaten "her şey ayakta mı" olan
    # döngü bu, ve YENİ denetimli görev eklemeden düzenli bir ritim veriyor.
    try:
        from . import diag
        await diag.flush_logs()
    except Exception:
        log.exception("uyarı halkası boşaltılamadı")
    downs = sorted(n for n, st in state.items() if not st.get("pending"))
    return {"downs": downs, "new": confirmed_new, "recovered": recovered}


async def snapshot(cfg: Config) -> dict:
    """/health ve /saglik için özet."""
    checks = await check(cfg)
    crashes = {}
    for name in limits(cfg):
        rec = await kv_get(f"crash:{name}")
        if rec:
            crashes[name] = rec
    try:
        quiet = await silent_coins(cfg)
    except Exception:
        quiet = []
    per = periods(cfg)
    for name, c in checks.items():
        c["period"] = int(per.get(name) or 0)
    return {"checks": checks, "crashes": crashes, "silent_coins": quiet,
            "problems": sorted(n for n, c in checks.items() if not c["ok"])}
