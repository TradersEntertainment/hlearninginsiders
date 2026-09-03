"""OI / funding / hacim zaman serisi — anomali tespiti ve raporların temeli."""
import logging

from ..assets import is_excluded
from ..config import Config
from ..db import db, now
from ..hl.client import HLClient
from ..hl.universe import norm_coin
from ..propr import is_listed as propr_listed

log = logging.getLogger("radar.metrics")


async def poll_metrics(cfg: Config, client: HLClient) -> int:
    n = 0
    # ANA DEX ("") de geziliyor: kripto OI'si olmadan "long mu kapattılar,
    # short mu açtılar" sorusu cevaplanamıyor (OI↑fiyat↓ = yeni short,
    # OI↓fiyat↓ = long kapanışı). Poll başına +1 istek.
    dexes = list(cfg.equity_dexes)
    if getattr(cfg, "crypto_metrics_enabled", True):
        dexes = ["", *dexes]
    for dex in dexes:
        try:
            data = await client.meta_and_ctxs(dex)
            meta, ctxs = data[0], data[1]
        except Exception as e:
            log.warning("metaAndAssetCtxs(%s) alınamadı: %s", dex, e)
            continue
        universe = meta.get("universe") or []
        ts = now()
        async with db() as conn:
            for asset, ctx in zip(universe, ctxs):
                name = asset.get("name") or ""
                if not name or is_excluded(name):
                    continue
                coin = norm_coin(name, dex)
                # Ana dexte YALNIZ PROPR'daki coinler: tüm ana dex 200+ satır
                # eder ve asset_metrics 45 gün saklıyor. İzlemediğimiz coinin
                # OI geçmişini tutmanın kimseye faydası yok.
                if not dex and not propr_listed(coin):
                    continue
                try:
                    mark = float(ctx.get("markPx") or 0)
                    oi = float(ctx.get("openInterest") or 0)
                    funding = float(ctx.get("funding") or 0)
                    vol = float(ctx.get("dayNtlVlm") or 0)
                except (TypeError, ValueError):
                    continue
                await conn.execute(
                    "INSERT OR REPLACE INTO asset_metrics(coin,ts,mark_px,oi,funding,day_volume)"
                    " VALUES(?,?,?,?,?,?)", (coin, ts, mark, oi, funding, vol))
                n += 1
    return n


async def latest_metric(coin: str) -> dict | None:
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM asset_metrics WHERE coin=? ORDER BY ts DESC LIMIT 1", (coin,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def metric_at(coin: str, ts: int) -> dict | None:
    """ts'ten önceki en yakın ölçüm."""
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM asset_metrics WHERE coin=? AND ts<=? ORDER BY ts DESC LIMIT 1",
            (coin, ts))
        row = await cur.fetchone()
        return dict(row) if row else None


async def summary(coin: str) -> dict:
    """Rapor başlığı için: güncel + 24h önceki karşılaştırma."""
    cur = await latest_metric(coin)
    prev = await metric_at(coin, now() - 86400)
    out = {"mark": None, "oi_ntl": None, "funding": None, "day_volume": None,
           "oi_change_pct": None, "px_change_pct": None}
    if cur:
        out["mark"] = cur["mark_px"]
        out["oi_ntl"] = (cur["oi"] or 0) * (cur["mark_px"] or 0)
        out["funding"] = cur["funding"]
        out["day_volume"] = cur["day_volume"]
        if prev and prev["oi"] and cur["oi"]:
            out["oi_change_pct"] = (cur["oi"] - prev["oi"]) / prev["oi"] * 100
        if prev and prev["mark_px"] and cur["mark_px"]:
            out["px_change_pct"] = (cur["mark_px"] - prev["mark_px"]) / prev["mark_px"] * 100
    return out
