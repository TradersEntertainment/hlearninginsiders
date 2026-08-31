"""Hisse hacim patlaması — 24 saatin en yüksek 5 dakikası (xyz dex).

`cryptovol` ile AYNI dedektör, farklı evren ve farklı kanal. Neden ayrı modül:
kripto yolu hiç ellenmiyor (sıfır regresyon riski) ve isimler dürüst kalıyor.
Rekor mantığı KOPYALANMIYOR — saf yardımcılar `cryptovol`'den içe aktarılıyor,
yani birim-agnostik karşılaştırma (ham `v`) tek yerde duruyor.

NEDEN VAR: SHEIN 31 Ağustos Pazartesi sabahı 5 dakikada hacim patlattı ve
%12.5 çöktü; bot susmuştu çünkü kripto radarı `xyz:` önekli hisse perp'lerini
bilerek dışarıda bırakıyor. Bu modül o boşluğu kapatıyor.

EVREN: xyz dex tickers ∩ PROPR listesi. Kripto tarafından farklı olarak hacmi
API'den ÇEKMİYOR — `asset_metrics.day_volume` zaten `metrics.poll_metrics`
tarafından her turda dolduruluyor, tekrar sormak boşuna istek olurdu.

LİSTE KAYMASI: PROPR listesi elle yazılmış bir kopya ve eskiyor (SHEIN de
MRNA da yoktu). `not_listed()` dexte olup listede olmayanları döndürür; sayfa
ve /tani bunu gösterir ki filtre bir daha sessizce körleşmesin.
"""
import asyncio
import logging

from ..db import alert_log, alert_recent, db, kv_set, now
from .cryptovol import (INTERVAL, LOOKBACK_SEC, find_record, parse_vol_candles,
                        unit_sane)

log = logging.getLogger("radar.equityvol")


async def _tickers() -> list[dict]:
    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol FROM tickers")
        return [dict(r) for r in await cur.fetchall()]


async def _day_volumes() -> dict[str, float]:
    """coin → 24s hacim, YEREL veriden (ekstra API çağrısı yok)."""
    async with db() as conn:
        cur = await conn.execute(
            """SELECT m.coin, m.day_volume FROM asset_metrics m
               JOIN (SELECT coin, MAX(ts) mts FROM asset_metrics GROUP BY coin) x
                 ON x.coin = m.coin AND x.mts = m.ts""")
        return {r["coin"]: (r["day_volume"] or 0.0) for r in await cur.fetchall()}


async def universe(cfg) -> tuple[list[str], dict]:
    """(taranacak coin'ler, coin→24s hacim). PROPR ∩ xyz dex, hacimce büyükten."""
    from ..propr import is_listed as propr_listed
    tick = await _tickers()
    vols = await _day_volumes()
    cap = max(1, int(getattr(cfg, "equity_vol_max_coins", 120)))
    coins = sorted((t["coin"] for t in tick if propr_listed(t["symbol"])),
                   key=lambda c: -vols.get(c, 0))[:cap]
    return coins, vols


async def not_listed() -> list[str]:
    """Dexte olup PROPR listemizde OLMAYAN semboller — kayma raporu.

    Bunlar taranmıyor. Sayfada gösteriliyor ki "neden bildirim gelmedi"
    sorusunun cevabı bir daha kodun içinde saklı kalmasın.
    """
    from ..propr import is_listed as propr_listed
    return sorted(t["symbol"] for t in await _tickers()
                  if not propr_listed(t["symbol"]))


async def scan(cfg, client, notifier=None) -> dict:
    """Bir tur: her hisse için 5dk mumlarını çek, rekoru bul, kaydet, bildir."""
    out = {"checked": 0, "events": 0, "alerted": 0, "err": 0,
           "unit_bad": [], "skipped": "", "coins": 0, "missing": []}
    if not getattr(cfg, "equity_vol_enabled", True):
        out["skipped"] = "kapalı"
        return out
    try:
        coins, vols = await universe(cfg)
        out["missing"] = await not_listed()
    except Exception as e:
        out["skipped"] = f"evren alınamadı: {type(e).__name__}: {e}"
        log.warning("hisse hacim evreni alınamadı: %s", e)
        return out
    out["coins"] = len(coins)
    min_usd = float(getattr(cfg, "equity_vol_min_usd", 100000))
    cool = max(60, int(getattr(cfg, "equity_vol_cooldown", 1800)))
    chat = (getattr(cfg, "crypto_stocks_id", "") or "").strip()
    ts = now()
    end_ms, start_ms = ts * 1000, (ts - LOOKBACK_SEC) * 1000

    for coin in coins:
        try:
            candles = parse_vol_candles(
                await client.candles(coin, INTERVAL, start_ms, end_ms))
        except Exception as e:
            out["err"] += 1
            if out["err"] == 1:
                log.warning("5dk mum alınamadı (%s): %s", coin, e)
            continue
        out["checked"] += 1
        if unit_sane(candles, vols.get(coin)) is False:
            out["unit_bad"].append(coin.split(":")[-1])
        rec = find_record(candles, ts)
        if not rec or rec["notional"] < min_usd:
            continue

        # UNIQUE(coin,bucket_ts): aynı kova iki kez taransa da tek satır kalır.
        async with db() as conn:
            cur = await conn.execute(
                """INSERT OR IGNORE INTO vol_events(coin,ts,bucket_ts,vol,notional,
                     prev_max,ratio,px,chg_pct,alerted,market)
                   VALUES(?,?,?,?,?,?,?,?,?,0,'equity')""",
                (coin, ts, rec["bucket_ts"], rec["vol"], rec["notional"],
                 rec["prev_max"], rec["ratio"], rec["px"], rec["chg_pct"]))
            fresh = (cur.rowcount or 0) > 0
        if not fresh:
            continue                       # bu kovayı zaten kaydetmiştik
        out["events"] += 1

        if notifier is None or not chat:
            continue                       # kanal yoksa GÖNDERME (ana kanalı kirletme)
        key = f"equityvol:{coin}"
        if await alert_recent("equityvol", key, cool):
            continue                       # olay kaydedildi ama bildirilmiyor
        from ..telegram import format as fmt
        text = fmt.equity_vol_alert({**rec, "coin": coin,
                                     "day_vol": vols.get(coin)})
        if await notifier.send("equityvol", text, priority="high",
                               key=f"{key}:{rec['bucket_ts']}", chat_id=chat):
            await alert_log("equityvol", key, text)
            async with db() as conn:
                await conn.execute(
                    "UPDATE vol_events SET alerted=1 WHERE coin=? AND bucket_ts=?",
                    (coin, rec["bucket_ts"]))
            out["alerted"] += 1

    if out["unit_bad"]:
        log.warning("hisse hacim birimi şüpheli (%d sembol, ör. %s): v×fiyat "
                    "toplamı dayNtlVlm ile uyuşmuyor — $ değerleri yanıltıcı olabilir",
                    len(out["unit_bad"]), ", ".join(out["unit_bad"][:5]))
    if out["events"]:
        log.info("hisse hacim: %d sembol tarandı, %d rekor, %d bildirim",
                 out["checked"], out["events"], out["alerted"])
    await kv_set("equityvol_stats", {**out, "ts": ts,
                                     "unit_bad": out["unit_bad"][:10],
                                     "missing": out["missing"][:40],
                                     "n_missing": len(out["missing"]),
                                     "chat": bool(chat)})
    return out


async def recent(limit: int = 60, hours: int = 48) -> list[dict]:
    from .cryptovol import recent as _recent
    return await _recent(limit, hours, market="equity")


async def loop(cfg, client, notifier) -> None:
    """Denetimli döngü. Site ASLA buna bağımlı değil: patlasa da yalnız kendi
    paneli boş kalır. Budama `cryptovol.prune` ile ortak (aynı tablo)."""
    from ..health import beat
    await asyncio.sleep(180)               # kripto turuyla aynı saniyeye düşmesin
    while True:
        try:
            await beat("equityvol")
            await scan(cfg, client, notifier)
            await beat("equityvol")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("hisse hacim turu hatası")
        await asyncio.sleep(max(60, int(getattr(cfg, "equity_vol_poll_sec", 300))))
