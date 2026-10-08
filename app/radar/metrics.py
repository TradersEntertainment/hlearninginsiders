"""OI / funding / hacim zaman serisi — anomali tespiti ve raporların temeli."""
import logging

from ..assets import is_excluded
from ..config import Config
from ..db import db, now
from ..hl.client import HLClient
from ..hl.universe import norm_coin
from ..propr import is_listed as propr_listed

log = logging.getLogger("radar.metrics")

# 🧪 Saatlik özet (strateji laboratuvarı): asset_metrics 45 günde budanıyor ve oraclePx / premium
# hiç saklanmıyordu. Ham tablo BÜYÜMEZ; her coin için o saatin örnekleri bellekte toplanır, saat
# dolunca metrics_hourly'ye TEK satır yazılır (süresiz). Açılışta yarım kalan saat kaybolur — `n`
# kaç örnekten kurulduğunu söyler. funding_avg = ctx'teki ANLIK oranın ortalaması (ödenen değil).
_HOURLY: dict[str, dict] = {}


def hour_acc(acc: dict | None, ts: int, mark: float, oracle: float | None, premium: float | None,
             funding: float, oi_usd: float, day_vol: float) -> tuple[dict, tuple | None]:
    """Saf toplayıcı: (yeni durum, biten saatin satırı ya da None). Satır = (ts, mark_c, oracle_c,
    premium_avg, funding_avg, oi_usd_c, day_volume_c, n); `_c` = saatin son örneği."""
    h = int(ts) // 3600 * 3600
    done = None
    if acc is not None and acc["h"] != h:
        if acc["h"] < h:
            n = acc["n"]
            done = (acc["h"], acc["mark"], acc["oracle"],
                    (acc["prem"] / acc["n_prem"]) if acc["n_prem"] else None,
                    acc["fund"] / n if n else None, acc["oi"], acc["vol"], n)
        acc = None
    if acc is None:
        acc = {"h": h, "n": 0, "fund": 0.0, "prem": 0.0, "n_prem": 0,
               "mark": None, "oracle": None, "oi": None, "vol": None}
    acc["n"] += 1
    acc["fund"] += funding
    if premium is not None:
        acc["prem"] += premium
        acc["n_prem"] += 1
    acc["mark"], acc["oi"], acc["vol"] = mark, oi_usd, day_vol
    if oracle is not None:
        acc["oracle"] = oracle
    return acc, done


def _opt(ctx: dict, key: str) -> float | None:
    try:
        v = ctx.get(key)
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


async def poll_metrics(cfg: Config, client: HLClient) -> int:
    n = 0
    # ANA DEX ("") de geziliyor: kripto OI'si olmadan "long mu kapattılar,
    # short mu açtılar" sorusu cevaplanamıyor (OI↑fiyat↓ = yeni short,
    # OI↓fiyat↓ = long kapanışı). Poll başına +1 istek.
    from .. import assets as _assets
    dexes = _assets.watched_dexes(cfg)              # hisse + kripto HIP-3 dex'leri
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
        if not dex:
            # Aynı yanıt, ek istek yok: kripto liq radarı ve kripto coin sayfası
            # tüm ana dex'in ŞİMDİKİ fiyatını buradan okur (PROPR filtresi yok).
            try:
                from ..hl.universe import store_main_dex_ctx
                await store_main_dex_ctx(meta, ctxs)
            except Exception:
                log.debug("main_dex_ctx yazılamadı", exc_info=True)
        ts = now()
        hourly: list[tuple] = []
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
                try:
                    _HOURLY[coin], done = hour_acc(_HOURLY.get(coin), ts, mark, _opt(ctx, "oraclePx"),
                                                   _opt(ctx, "premium"), funding, oi * mark, vol)
                    if done:
                        hourly.append((coin, *done))
                except Exception:                      # noqa: BLE001 — özet asla ana kaydı düşürmez
                    log.debug("saatlik özet toplanamadı %s", coin, exc_info=True)
            if hourly:
                try:
                    await conn.executemany(
                        "INSERT OR REPLACE INTO metrics_hourly(coin, ts, mark_c, oracle_c, premium_avg,"
                        " funding_avg, oi_usd_c, day_volume_c, n) VALUES(?,?,?,?,?,?,?,?,?)", hourly)
                except Exception:                      # noqa: BLE001
                    log.warning("metrics_hourly yazılamadı", exc_info=True)
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
