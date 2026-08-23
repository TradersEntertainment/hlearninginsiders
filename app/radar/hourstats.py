"""Saat istatistiği — hisse hangi saatte yükseliyor, hangi saatte düşüyor?

Kullanıcı gözlemi ("overnight effect"): bazı hisselerde (MU, NVDA) getirinin
çoğu ABD borsası KAPALIYKEN gelir. HIP-3 perp'ler 7/24 işlem gördüğü için bu
ölçülebilir: 90 günlük 1 saatlik mumlardan saat-of-day getiri haritası +
borsa açık/kapalı seans ayrımı çıkarılır. Tamamen site özelliği — Telegram
bildirimi üretmez.

Veri: HL candleSnapshot (coin başına günde 1 istek — bütçede önemsiz).
"""
import asyncio
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import Config
from ..db import db, kv_get, kv_set, now
from ..hl.client import HLClient

log = logging.getLogger("radar.hourstats")

ET = ZoneInfo("America/New_York")
TR = ZoneInfo("Europe/Istanbul")

MIN_N = 40         # bir saat kovasının "anlamlı" sayılması için asgari örnek
HOT_MEAN = 0.10    # güçlü saat: ortalama getiri eşiği (%/saat)
HOT_WIN = 55.0     # güçlü saat: kazanma oranı eşiği (%)
MIN_CANDLES = 24 * 20  # en az ~20 günlük veri yoksa istatistik üretme
REFRESH_SEC = 600  # döngü periyodu
PER_CYCLE = 2      # her turda yenilenecek coin sayısı
STALE_SEC = 24 * 3600

_inflight: set[str] = set()


def parse_candles(raw) -> list[dict]:
    out = []
    if not isinstance(raw, list):
        return out
    for c in raw:
        try:
            out.append({"t": int(c["t"]) // 1000,
                        "o": float(c["o"]), "c": float(c["c"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def compute_stats(candles: list[dict]) -> dict | None:
    """Saat haritası + borsa açık/kapalı seans ayrımı. Yetersiz veri → None."""
    if len(candles) < MIN_CANDLES:
        return None
    hours = [{"n": 0, "s": 0.0, "w": 0} for _ in range(24)]
    open_c = closed_c = 1.0
    t_min = t_max = candles[0]["t"]
    for c in candles:
        if c["o"] <= 0:
            continue
        r = c["c"] / c["o"] - 1
        if abs(r) > 0.5:
            continue  # bozuk mum
        dt = datetime.fromtimestamp(c["t"], ET)
        b = hours[dt.hour]
        b["n"] += 1
        b["s"] += r
        b["w"] += 1 if r > 0 else 0
        # NYSE normal seansı ~09:30-16:00 ET; 1h mumda 9-16 başlangıçları
        # "açık" sayılır (09:00 mumunun yarısı kapalıdır — yaklaşıklama)
        if dt.weekday() < 5 and 9 <= dt.hour < 16:
            open_c *= 1 + r
        else:
            closed_c *= 1 + r
        t_min, t_max = min(t_min, c["t"]), max(t_max, c["t"])

    ref = datetime.fromtimestamp(t_max, ET)
    out_hours = []
    for h, b in enumerate(hours):
        tsi = ref.replace(hour=h, minute=0).astimezone(TR).hour
        out_hours.append({
            "et": h, "tsi": tsi,
            "avg": (b["s"] / b["n"] * 100) if b["n"] else 0.0,
            "win": (b["w"] / b["n"] * 100) if b["n"] else 0.0,
            "n": b["n"],
        })
    ranked = [h for h in out_hours if h["n"] >= MIN_N]
    best = sorted(ranked, key=lambda x: -x["avg"])[:3]
    worst = sorted(ranked, key=lambda x: x["avg"])[:3]
    return {
        "hours": out_hours,
        "open_ret": (open_c - 1) * 100, "closed_ret": (closed_c - 1) * 100,
        "days": round((t_max - t_min) / 86400),
        "best": best, "worst": worst,
        "ts": now(),
    }


def verdict(stats: dict, et_hour: int) -> tuple[str, dict]:
    """'güçlü' / 'zayıf' / 'nötr' — şu saatin tarihsel karnesi."""
    b = stats["hours"][et_hour % 24]
    if b["n"] >= MIN_N and b["avg"] >= HOT_MEAN and b["win"] >= HOT_WIN:
        return "güçlü", b
    if b["n"] >= MIN_N and b["avg"] <= -HOT_MEAN and b["win"] <= 100 - HOT_WIN:
        return "zayıf", b
    return "nötr", b


def hot_now(stats_map: dict[str, dict], et_hour: int) -> list[dict]:
    """ŞU SAATİ tarihsel olarak güçlü olan hisseler (ana sayfa bölümü)."""
    out = []
    for coin, s in stats_map.items():
        if not s or s.get("empty"):
            continue
        v, b = verdict(s, et_hour)
        if v != "güçlü":
            continue
        out.append({"coin": coin, "avg": b["avg"], "win": b["win"], "n": b["n"],
                    "closed_heavy": s["closed_ret"] > s["open_ret"],
                    "closed_ret": s["closed_ret"], "open_ret": s["open_ret"]})
    out.sort(key=lambda x: -x["avg"])
    return out


async def all_stats() -> dict[str, dict]:
    """kv'deki tüm hazır istatistikler: coin -> stats."""
    out = {}
    async with db() as conn:
        cur = await conn.execute("SELECT k, v FROM kv WHERE k LIKE 'hstats:%'")
        for r in await cur.fetchall():
            try:
                out[r["k"][7:]] = json.loads(r["v"])
            except (ValueError, TypeError):
                continue
    return out


async def refresh_coin(cfg: Config, client: HLClient, coin: str) -> dict | None:
    end_ms = now() * 1000
    start_ms = end_ms - int(cfg.hourstats_days) * 86400 * 1000
    try:
        raw = await client.candles(coin, "1h", start_ms, end_ms)
    except Exception as e:
        log.debug("candles %s: %s", coin, e)
        return None
    stats = compute_stats(parse_candles(raw))
    await kv_set(f"hstats:{coin}", stats or {"empty": True, "ts": now()})
    if stats:
        log.info("saat istatistiği hazır: %s (%d gün, en iyi saat TSİ %02d:00)",
                 coin.split(":")[-1], stats["days"],
                 stats["best"][0]["tsi"] if stats["best"] else -1)
    return stats


def kick(cfg: Config, client: HLClient, coin: str) -> None:
    """Coin sayfası açıldığında istatistik yoksa arka planda hazırla."""
    if coin in _inflight:
        return

    async def run():
        try:
            await refresh_coin(cfg, client, coin)
        finally:
            _inflight.discard(coin)

    _inflight.add(coin)
    asyncio.create_task(run())


async def refresh_loop(cfg: Config, client: HLClient) -> None:
    await asyncio.sleep(150)
    log.info("saat istatistiği döngüsü başladı (%d coin / %ds)", PER_CYCLE, REFRESH_SEC)
    while True:
        try:
            from ..health import beat
            await beat("hourstats")
            async with db() as conn:
                cur = await conn.execute("SELECT coin FROM tickers ORDER BY coin")
                coins = [r["coin"] for r in await cur.fetchall()]
            if coins:
                have = await all_stats()
                ts = now()
                todo = [c for c in coins
                        if not have.get(c) or ts - int(have[c].get("ts") or 0) > STALE_SEC]
                for coin in todo[:PER_CYCLE]:
                    await refresh_coin(cfg, client, coin)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("saat istatistiği hatası")
        await asyncio.sleep(REFRESH_SEC)
