"""📊 Son 5 dakikanın en büyük fiyat hareketleri — PROPR, 24s hacim tabanı üstü.

Kullanıcı isteği: "sitemize son 5dk en büyük hareketler şeyi koyalım, propr'da
listeli olan şeyler olsun, 24 saatlik hacmi 1M$+ olan."

NEDEN AYRI BİR SAYFA, `/hacim` VARKEN: `/hacim` başka bir soru soruyor —
*hacim* kendi 24 saatlik normalini kırdı mı. Bir coin %8 oynayıp hacim rekoru
kırmazsa `vol_events`'e hiç girmiyor, yani "en çok oynayan kim" oradan
cevaplanamıyor. Burası fiyata bakıyor.

VERİ NEREDEN: hacim radarları (`cryptovol` + `equityvol`) zaten tam olarak
istenen evreni (PROPR ∩ ana dex, PROPR ∩ tickers) 5 dakikalık mumlarla tarıyor
ve 24 saatlik hacmi de elinde tutuyor. Onlar her turda son KAPANMIŞ kovayı
`movers5m:*` kv'sine yazıyor (`cryptovol.save_movers`). Burası yalnız okur:
**yeni HL isteği YOK.**

SAF: kv okur, ağa çıkmaz — bu yüzden `page()` bir `client` almaz.
"""
from __future__ import annotations

import logging

from ..db import kv_get, now
from .cryptovol import MOVERS_KV

log = logging.getLogger("radar.movers")

MOVERS_MAX = 50
MARKETS = ("hepsi", "kripto", "hisse")
# Sayfadaki hacim tabanı seçenekleri ($). İlki kullanıcının kuralı: 24s ≥ $1M.
VOL_STEPS = (1_000_000.0, 5_000_000.0, 25_000_000.0, 0.0)
# kv anahtarındaki piyasa adı → sayfadaki süzgeç adı. Ayrım PROPR listesini
# bölerek değil dex üyeliğiyle yapılıyor (ana dexte kalan her şey kripto).
_KIND = {"crypto": "kripto", "equity": "hisse"}


async def page(cfg, market: str = "hepsi", min_day_vol: float | None = None,
               limit: int = MOVERS_MAX) -> dict:
    """5dk en büyük fiyat hareketleri. SAF: yalnız kv okur, HL'ye istek ATMAZ.

    Hareket = son KAPANMIŞ 5 dakikalık kovanın **açılış→kapanış** değişimi.
    Devam eden mum sayılmaz (yarım kova "%3 düştü" diye yalan söylerdi).
    Sıralama |%| büyükten küçüğe; yön renkle belli olur.

    Dönüş sayfanın künyesi için gereken her şeyi taşır: iki turun yaşı, taranan
    coin sayısı, ön süzgeçle atlananlar, hacim tabanına takılanlar.
    """
    floor = float(min_day_vol if min_day_vol is not None
                  else getattr(cfg, "movers_min_day_vol", 1_000_000) or 0)
    min_chg = float(getattr(cfg, "movers_min_chg_pct", 0) or 0)
    market = market if market in MARKETS else "hepsi"
    ts = now()

    rows: list[dict] = []
    meta: dict[str, dict] = {}
    n_below_vol = n_below_chg = 0
    for mk in ("crypto", "equity"):
        rec = await kv_get(f"{MOVERS_KV}:{mk}") or {}
        kind = _KIND[mk]
        meta[kind] = {"ts": int(rec.get("ts") or 0),
                      "age": (ts - int(rec["ts"])) if rec.get("ts") else None,
                      "n_coins": int(rec.get("n_coins") or 0),
                      "n_prefiltered": int(rec.get("n_prefiltered") or 0),
                      "n_nodata": int(rec.get("n_nodata") or 0),
                      "n_rows": len(rec.get("rows") or [])}
        if market not in ("hepsi", kind):
            continue
        for r in rec.get("rows") or []:
            try:
                dv, chg = float(r.get("day_vol") or 0), float(r.get("chg_pct") or 0)
            except (TypeError, ValueError):
                continue
            if floor and dv < floor:
                n_below_vol += 1
                continue
            if min_chg and abs(chg) < min_chg:
                n_below_chg += 1
                continue
            rows.append({**r, "kind": kind, "chg_pct": chg, "day_vol": dv,
                         "up": chg > 0})
    rows.sort(key=lambda r: -abs(r["chg_pct"]))
    shown = rows[:limit]
    ages = [m["age"] for m in meta.values() if m["age"] is not None]
    return {
        "rows": shown, "n_all": len(rows), "cap": limit,
        "market": market, "floor": floor, "min_chg": min_chg,
        "n_below_vol": n_below_vol, "n_below_chg": n_below_chg,
        "meta": meta, "vol_steps": VOL_STEPS, "markets": MARKETS,
        # En ESKİ tur belirleyicidir: sayfa "şu an" demez, en kötü yaşı söyler.
        "age": max(ages) if ages else None,
        "n_up": sum(1 for r in shown if r["up"]),
        "n_down": sum(1 for r in shown if not r["up"]),
    }
