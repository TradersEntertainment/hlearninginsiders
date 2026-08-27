"""Arka plan oto-tarayıcı.

Kullanıcı "şimdi tara"ya basmak zorunda kalmasın diye evrendeki coin'ler
sürekli sırayla taranır: earnings'i yaklaşanlar öncelikli, sonra en bayat.
Coin sayfası açıldığında veri bayatsa anında arka planda tarama tetiklenir.
"""
import asyncio
import logging

from .. import assets
from ..config import Config
from ..db import alert_log, alert_recent, db, now
from ..hl.client import HLClient
from ..hl.universe import symbol_of
from ..telegram import format as fmt

log = logging.getLogger("radar.autoscan")

_inflight: set[str] = set()

NEW_BIG_COOLDOWN = 48 * 3600  # aynı coin+adres için tekrar alert etme
# Earnings'i yakın coin bu aralıkla yeniden taranır. Daha sık olursa (eski 600)
# 2 earnings coini tüm tarama slotlarını tekeller, evrenin kalanı hiç sıra alamaz.
EARNINGS_RESCAN = 1800
BIG_CACHE_TTL = 600           # "hacimce top-N hisse" listesi bu kadar önbelleklenir

_big_cache: dict = {"ts": 0, "coins": set()}


async def _big_coins(cfg: Config) -> set[str]:
    """Hacimce top-N hisse (NVDA gibi) — her coin taramasında yeniden
    hesaplanmasın diye önbellekli. Hata olursa BOŞ küme değil, bayat liste
    döner: boş küme dev hisseleri küçük sınıfa düşürüp alarm yağdırırdı."""
    if now() - int(_big_cache["ts"]) < BIG_CACHE_TTL:
        return _big_cache["coins"]
    from .bookwall import big_coin_set
    try:
        _big_cache["coins"] = await big_coin_set(cfg)
        _big_cache["ts"] = now()
    except Exception:
        log.debug("büyük sınıf listesi alınamadı, öncekiyle devam")
    return _big_cache["coins"]


def alert_floor(cfg: Config, coin: str, big_coins: set[str]) -> float:
    """Bu enstrümanda BİLDİRİM için gereken pozisyon boyutu.

    "Büyük" enstrümana göre değişir: XYZ100'de $3M gürültüdür (endeksin OI'si
    zaten devasa), ama SNDK gibi küçük bir hissede $3M piyasanın yarısıdır ve
    tam da aradığımız sinyaldir. Tek eşik ikisini birden idare edemiyordu.

    Kademeler — kullanıcı kuralı:
      endeks/emtia/FX/ETF (XYZ100, SP500, GOLD, SILVER…)  → $10M
      hacimce top-N hisse (NVDA…)                          → $5M
      diğer hisseler                                       → mevcut eşik ($1M)

    Bu YALNIZ bildirim kapısıdır: sitede ve skorlamada eşik değişmez, küçük
    pozisyonlar görünmeye devam eder.
    """
    if assets.kind(symbol_of(coin)) == "non_equity":
        return float(getattr(cfg, "big_alert_index_usd", 10_000_000))
    if coin in big_coins:
        return float(getattr(cfg, "big_alert_major_usd", 5_000_000))
    return float(getattr(cfg, "big_alert_min_usd", 0) or cfg.big_position_usd)


def is_scanning(coin: str) -> bool:
    return coin in _inflight


async def _upcoming_event(coin: str) -> dict | None:
    async with db() as conn:
        # date_et New York günüdür; date('now') SQLite'ta UTC. 00:00-04:00 UTC
        # (=20:00-24:00 ET) arası UTC gün bir ileridedir → o akşamın AMC eventi
        # bağlamdan düşerdi. '-1 day' tamponu (routes ile tutarlı) bunu kapatır.
        cur = await conn.execute(
            """SELECT * FROM earnings_events WHERE coin=? AND evaluated=0
               AND date_et>=date('now','-1 day') ORDER BY date_et LIMIT 1""", (coin,))
        r = await cur.fetchone()
        return dict(r) if r else None


async def _alert_new_big(cfg: Config, notifier, coin: str, rows: list[dict]) -> None:
    """Earnings şartı YOK: yeni açılmış büyük pozisyon görülünce anında haber ver.
    (CEO istifası, ele geçirme, dava... insider her zaman ortaya çıkabilir.)"""
    if not notifier:
        return
    ts = now()
    floor = alert_floor(cfg, coin, await _big_coins(cfg))
    for p in rows:
        if p.get("entity"):  # MM/vault — gürültü, alert yok
            continue
        if p["notional"] < floor:
            continue
        if (p.get("score") or 0) < cfg.alert_min_score:
            continue
        opened = p.get("opened_ts")
        if not opened or ts - opened > cfg.fresh_big_alert_hours * 3600:
            continue
        # Uzun süredir GÖRDÜĞÜMÜZ pozisyon "yeni" değildir: opened_ts fill
        # penceresinden kayabilir, first_seen_ts bağımsız alt sınırdır.
        seen = p.get("first_seen_ts")
        if seen and ts - seen > cfg.fresh_big_alert_hours * 3600:
            continue
        key = f"{coin}:{p['address']}"
        if await alert_recent("new_big_pos", key, NEW_BIG_COOLDOWN):
            continue
        event = await _upcoming_event(coin)
        text = fmt.new_big_position_alert(coin, p, event)
        try:
            prio = "critical" if (p.get("score") or 0) >= 70 else "high"
            if await notifier.send("new_big", text, priority=prio, key=key):
                await alert_log("new_big_pos", key, text)
        except Exception as e:
            log.warning("yeni-poz alerti gönderilemedi: %s", e)


async def scan_coin(cfg: Config, client: HLClient, coin: str, dex: str, notifier=None,
                    from_loop: bool = False) -> None:
    from .report import build_scan  # döngüsel importu kır
    if coin in _inflight:
        return
    _inflight.add(coin)
    try:
        # yalnız oto-tarayıcı DÖNGÜSÜ kendi nabzını (autoscan) tazeler; sayfa
        # kick'i / /scan / due bu yolu çağırırsa hb:autoscan'ı KİRLETMESİN
        _, rows = await build_scan(cfg, client, coin, dex, quick=True,
                                   beat_name="autoscan" if from_loop else None)
        await _alert_new_big(cfg, notifier, coin, rows)
        log.info("oto-tarama tamam: %s (%d pozisyon)", coin, len(rows))
    except Exception as e:
        log.warning("oto-tarama %s: %s", coin, e)
    finally:
        _inflight.discard(coin)


def kick(cfg: Config, client: HLClient, coin: str, dex: str, notifier=None) -> None:
    """Ateşle-unut: sayfa açılışında bayat coin için arka plan taraması."""
    if coin not in _inflight:
        asyncio.create_task(scan_coin(cfg, client, coin, dex, notifier))


async def _pick_next(cfg: Config) -> tuple[str, str] | None:
    ts = now()
    async with db() as conn:
        # 1) earnings'i yaklaşan (±3 gün) ve 30+ dk'dır taranmamış coin
        cur = await conn.execute(
            """SELECT t.coin, t.dex FROM tickers t
               LEFT JOIN scans s ON s.coin = t.coin
               WHERE COALESCE(s.ts, 0) < ?
                 AND EXISTS(SELECT 1 FROM earnings_events e
                            WHERE e.coin = t.coin AND e.evaluated = 0
                              AND e.date_et BETWEEN date('now','-1 day') AND date('now','+3 day'))
               ORDER BY COALESCE(s.ts, 0) ASC LIMIT 1""",
            (ts - EARNINGS_RESCAN,))
        r = await cur.fetchone()
        if r:
            return r["coin"], r["dex"]
        # 2) genel: en bayat coin (en az 15 dk arayla)
        cur = await conn.execute(
            """SELECT t.coin, t.dex FROM tickers t
               LEFT JOIN scans s ON s.coin = t.coin
               WHERE COALESCE(s.ts, 0) < ?
               ORDER BY COALESCE(s.ts, 0) ASC LIMIT 1""",
            (ts - 900,))
        r = await cur.fetchone()
        return (r["coin"], r["dex"]) if r else None


async def loop(cfg: Config, client: HLClient, notifier=None) -> None:
    await asyncio.sleep(45)  # evren keşfini bekle
    log.info("oto-tarayıcı başladı (periyot: %ds)", cfg.auto_scan_interval_sec)
    while True:
        try:
            from ..health import beat
            await beat("autoscan")  # turbaşı: uzun tarama sahte alarm üretmesin
            nxt = await _pick_next(cfg)
            if nxt:
                await scan_coin(cfg, client, nxt[0], nxt[1], notifier, from_loop=True)
            await beat("autoscan")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("oto-tarama döngü hatası")
        await asyncio.sleep(cfg.auto_scan_interval_sec)
