"""Mum verisi — coin sayfasındaki fiyat grafiği.

`hourstats` ile aynı desen: HL candleSnapshot → kv önbellek → coin sayfası
açıldığında yoksa arka planda hazırlanır ("hazırlanıyor" durumu). Fark:
hourstats yalnız açılış/kapanış alıp istatistik çıkarır, burada mumun kendisi
(o/h/l/c) grafiğe basılmak üzere saklanır.

Yalnız AÇILAN coin önbelleğe girer — bütçe ve disk doğal olarak sınırlı.
Telegram bildirimi üretmez, tamamen site özelliğidir.
"""
import asyncio
import logging

from ..config import Config
from ..db import kv_get, kv_set, now
from ..hl.client import HLClient

log = logging.getLogger("radar.pricechart")

INTERVAL = "1h"
TTL = 300            # bu kadar taze önbellek yeniden çekilmez
MAX_CANDLES = 1200   # kv satırı şişmesin (30 gün 1h ≈ 720)
MIN_CANDLES = 8      # bundan azı grafik değil, gürültü

_inflight: set[str] = set()


def parse_candles(raw) -> list[dict]:
    """HL candleSnapshot → [{t,o,h,l,c}] (t saniye). Bozuk mumlar atlanır."""
    out = []
    if not isinstance(raw, list):
        return out
    for c in raw:
        try:
            o, hi, lo, cl = float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"])
        except (KeyError, TypeError, ValueError):
            continue
        if o <= 0 or hi <= 0 or lo <= 0 or cl <= 0 or hi < lo:
            continue
        try:
            t = int(c["t"]) // 1000
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"t": t, "o": o, "h": hi, "l": lo, "c": cl})
    out.sort(key=lambda x: x["t"])
    # Aynı damgayı iki kez veren snapshot'lar oluyor; sonuncusu geçerli.
    # (lightweight-charts artan ve TEKİL zaman bekler, yoksa setData patlar.)
    dedup: list[dict] = []
    for c in out:
        if dedup and dedup[-1]["t"] == c["t"]:
            dedup[-1] = c
        else:
            dedup.append(c)
    return dedup[-MAX_CANDLES:]


def fresh(rec: dict | None, ttl: int = TTL) -> bool:
    return bool(rec) and (now() - int(rec.get("ts") or 0)) < ttl


async def get(coin: str) -> dict | None:
    """Önbellekteki kayıt: {"candles": [...], "ts": …} ya da {"empty": True, …}."""
    return await kv_get(f"pxc:{coin}")


async def _keep_or_mark(coin: str, prev: dict | None, **mark) -> dict | None:
    """Elde ÇALIŞAN mum varsa onu koru — geçici bir API tökezlemesi yüzünden
    dolu bir grafiği boşaltmak (kullanıcıya "veri yok" demek) yanlış. Yalnız
    ts tazelenir ki bir sonraki deneme TTL kadar sonra olsun."""
    if prev and prev.get("candles"):
        rec = {**prev, "ts": now(), "stale": True}
        await kv_set(f"pxc:{coin}", rec)
        return rec
    await kv_set(f"pxc:{coin}", {"empty": True, "ts": now(), **mark})
    return None


async def refresh(cfg: Config, client: HLClient, coin: str) -> dict | None:
    days = max(1, int(getattr(cfg, "pricechart_days", 30)))
    end_ms = now() * 1000
    start_ms = end_ms - days * 86400 * 1000
    prev = await kv_get(f"pxc:{coin}")
    try:
        raw = await client.candles(coin, INTERVAL, start_ms, end_ms)
    except Exception as e:
        log.debug("mum çekilemedi %s: %s", coin, e)
        # Hatada da taze ts yaz: yoksa bozuk/delist coin her sayfa açılışında
        # yeniden denenir (hourstats'ta aynı tuzağa düşülmüştü).
        return await _keep_or_mark(coin, prev, error=True)
    candles = parse_candles(raw)
    if len(candles) < MIN_CANDLES:
        return await _keep_or_mark(coin, prev)
    rec = {"candles": candles, "ts": now(), "interval": INTERVAL}
    await kv_set(f"pxc:{coin}", rec)
    log.info("mumlar hazır: %s (%d mum, %d gün)",
             coin.split(":")[-1], len(candles), days)
    return rec


def kick(cfg: Config, client: HLClient, coin: str) -> None:
    """Coin sayfası açıldığında mum yoksa/bayatsa arka planda hazırla."""
    if coin in _inflight:
        return

    async def run():
        try:
            await refresh(cfg, client, coin)
        except Exception:
            log.exception("mum hazırlama hatası: %s", coin)
        finally:
            _inflight.discard(coin)

    _inflight.add(coin)
    asyncio.create_task(run())
