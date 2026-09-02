"""Funding sıralaması — kim kime ödüyor?

Funding bir POZİSYONLANMA sinyalidir: pozitif oran longların shortlara ödediği
anlamına gelir (kalabalık long tarafta), negatif oran tersi. Oran zaten
`asset_metrics.funding`'de 45 gündür toplanıyordu ama sitede yalnız coin
sayfasında tek bir etiket olarak duruyordu — sıralanamıyor, karşılaştırılamıyordu.

Bu modülün ayırt edici parçası son bölüm: funding oranını BİLDİĞİMİZ BALİNA
POZİSYONLARIYLA birleştiriyor. "NVDA'da funding %0.013/saat" herkeste var;
"NVDA long balinası günde $12K funding ödüyor" için oranla pozisyon sahibini
aynı yerde tutmak gerekiyor.

DÜRÜSTLÜK KAYDI: `positions_current` yalnız adres havuzumuzu kapsar. Buradaki
long/short toplamları ve maliyetler gerçek piyasa toplamı DEĞİL, gördüğümüz
kadarının ALT SINIRIDIR. Sayfa bunu yazıyor.

HL funding'i SAATLİK öder (çoğu borsanın 8 saatliği değil). Yıllık karşılık
bu yüzden `oran * 24 * 365`: 0.05%/saat = %438/yıl.
"""
import logging

from ..db import db
from . import metrics
from .hourstats import now

log = logging.getLogger("radar.funding")

HOURS_PER_YEAR = 24 * 365
TOP_PAYERS = 12
LOOKBACK = 86400          # 24 saat önceki oranla karşılaştırma


def annualize(hourly_rate: float) -> float:
    """Saatlik funding oranı (kesir) → yıllık yüzde."""
    return float(hourly_rate) * HOURS_PER_YEAR * 100


async def ranking(cfg=None, top: int = TOP_PAYERS) -> dict:
    """Tüm hisse perp'lerini funding'e göre sırala + maliyeti hesapla."""
    extreme = float(getattr(cfg, "funding_extreme", 0.0005)) if cfg else 0.0005
    ts = now()

    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol FROM tickers ORDER BY symbol")
        tickers = [dict(r) for r in await cur.fetchall()]
        # Yön başına notional TEK sorguda — coin başına sorgu açmıyoruz.
        cur = await conn.execute(
            "SELECT coin, side, COUNT(*) n, SUM(notional) ntl FROM positions_current"
            " GROUP BY coin, side")
        sides: dict[str, dict] = {}
        for r in await cur.fetchall():
            sides.setdefault(r["coin"], {})[r["side"]] = (r["n"], r["ntl"] or 0.0)
        # En çok ödeyenler için tek tek pozisyonlar (maliyet coin'in oranıyla
        # çarpılacağı için oranı bilmeden süzemiyoruz; makul bir tavanla al).
        cur = await conn.execute(
            """SELECT p.coin, p.address, p.side, p.notional, t.symbol, a.watchlist,
                      a.account_value, a.account_ts
               FROM positions_current p
               LEFT JOIN tickers t ON t.coin = p.coin
               LEFT JOIN addresses a ON a.address = p.address
               ORDER BY p.notional DESC LIMIT 400""")
        positions = [dict(r) for r in await cur.fetchall()]

    from ..propr import is_listed as propr_listed
    rows, n_nodata = [], 0
    rate: dict[str, float] = {}
    for t in tickers:
        coin = t["coin"]
        m = await metrics.latest_metric(coin)
        if not m or m.get("funding") is None:
            n_nodata += 1
            continue
        f = float(m["funding"])
        rate[coin] = f
        prev = await metrics.metric_at(coin, ts - LOOKBACK)
        pf = float(prev["funding"]) if prev and prev.get("funding") is not None else None
        # İşaret dönüşü, oranın kendisinden daha çok şey söyler: kalabalık taraf
        # değişmiş demektir. Sıfır "dönüş" sayılmaz (yön yok, geçiş var).
        flipped = pf is not None and pf != 0 and f != 0 and (pf > 0) != (f > 0)

        ln, sn = sides.get(coin, {}).get("long", (0, 0.0)), sides.get(coin, {}).get("short", (0, 0.0))
        rows.append({
            "coin": coin, "symbol": t["symbol"], "propr": propr_listed(t["symbol"]),
            "funding": f, "hourly": f * 100, "apr": annualize(f),
            "prev": pf, "chg": (f - pf) * 100 if pf is not None else None,
            "flipped": flipped, "extreme": abs(f) >= extreme,
            "mark": m.get("mark_px"),
            "oi_usd": float(m.get("oi") or 0) * float(m.get("mark_px") or 0),
            "volume": m.get("day_volume") or 0,
            "n_long": ln[0], "long_ntl": ln[1], "n_short": sn[0], "short_ntl": sn[1],
            # İşaret AKIŞ yönüdür: pozitif funding'de long ÖDER (negatif akış),
            # short ALIR (pozitif akış). Negatif funding'de tersi.
            "long_daily": -ln[1] * f * 24,
            "short_daily": sn[1] * f * 24,
            "ts": m.get("ts"),
        })

    rows.sort(key=lambda r: -r["funding"])      # "en yüksek funding" — istenen sıra

    payers = []
    for p in positions:
        f = rate.get(p["coin"])
        if f is None or not p.get("notional"):
            continue
        daily = float(p["notional"]) * f * 24 * (-1 if p["side"] == "long" else 1)
        if not daily:
            continue
        payers.append({**p, "symbol": p["symbol"] or (p["coin"] or "").split(":")[-1],
                       "funding": f, "daily": daily})
    payers.sort(key=lambda x: -abs(x["daily"]))
    return {"rows": rows, "n_nodata": n_nodata, "n_tickers": len(tickers),
            "extreme": extreme,
            "payers": [x for x in payers if x["daily"] < 0][:top],
            "earners": [x for x in payers if x["daily"] > 0][:top]}
