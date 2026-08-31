"""Mum arşivi — örüntü bulucunun ham verisi.

`hourstats` zaten her coin için 1h mumları ÇEKİYORDU ama saat-bazlı özete
indirip atıyordu; şekil eşleştirmesi ham seriyi istiyor. Burası o seriyi
kalıcı saklıyor: yalnız **kapanış + hacim**, çünkü eşleştirmenin ve
sparkline'ın ihtiyacı bu (OHLC zaten `pricechart` kv önbelleğinde).

ARTIMLI: her turda saklanan son damgadan itibaren isteniyor, baştan değil.
İlk doldurma tam pencereyi çeker; sonrası birkaç barlık küçük istek.

MALİYET: sembol başına dilim başına turda 1 istek. ~175 sembol × 2 dilim /
30 dk ≈ 12 rpm, bütçe 350.

GÖRÜNÜRLÜK: hangi sembolde kaç bar var, hangileri boş — kv'ye yazılır ve
sayfada/`/tani`'da görünür. Örüntü sekmesi "yeterli veri yok" dediğinde
sebebin arşiv mi eşik mi olduğu buradan anlaşılır.
"""
import asyncio
import logging

from ..db import db, kv_set, now

log = logging.getLogger("radar.bars")

TFS = ("1h", "15m")
TF_SEC = {"1h": 3600, "15m": 900}
# Artımlı çekimde biraz geriye bin: son bar yeniden yazılsın (kapanmamış
# olabilir) ve olası boşluk kapansın.
OVERLAP_BARS = 3


def days_for(cfg, tf: str) -> int:
    return int(getattr(cfg, "bars_1h_days", 180) if tf == "1h"
               else getattr(cfg, "bars_15m_days", 60))


async def last_ts(coin: str, tf: str) -> int | None:
    async with db() as conn:
        cur = await conn.execute(
            "SELECT MAX(ts) t FROM bars WHERE coin=? AND tf=?", (coin, tf))
        r = await cur.fetchone()
    return int(r["t"]) if r and r["t"] else None


async def series(coin: str, tf: str, limit: int = 0) -> tuple[list, list]:
    """(kapanışlar, damgalar) — artan zaman. Eşleştiricinin girdisi."""
    q = "SELECT ts, c FROM bars WHERE coin=? AND tf=? ORDER BY ts"
    async with db() as conn:
        cur = await conn.execute(q, (coin, tf))
        rows = await cur.fetchall()
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    return [float(r["c"]) for r in rows], [int(r["ts"]) for r in rows]


async def depth() -> dict:
    """coin|tf → bar sayısı. Boşluğun sebebini görünür kılan tek şey."""
    async with db() as conn:
        cur = await conn.execute(
            "SELECT coin, tf, COUNT(*) n FROM bars GROUP BY coin, tf")
        return {f"{r['coin']}|{r['tf']}": int(r["n"]) for r in await cur.fetchall()}


async def refresh(cfg, client, coin: str, tf: str) -> int:
    """Bir sembol+dilim güncelle. Döner: yazılan bar sayısı."""
    from .pricechart import parse_candles          # bozuk mum/çift damga temizliği
    ts = now()
    span = days_for(cfg, tf) * 86400
    have = await last_ts(coin, tf)
    start = max(ts - span,
                (have - OVERLAP_BARS * TF_SEC[tf]) if have else ts - span)
    if have and start >= ts:
        return 0
    raw = await client.candles(coin, tf, start * 1000, ts * 1000)
    rows = [(coin, tf, c["t"], c["c"], float(c.get("v") or 0))
            for c in parse_candles(raw)]
    if not rows:
        return 0
    async with db() as conn:
        await conn.executemany(
            "INSERT OR REPLACE INTO bars(coin,tf,ts,c,v) VALUES(?,?,?,?,?)", rows)
    return len(rows)


async def prune(cfg) -> int:
    ts = now()
    total = 0
    async with db() as conn:
        for tf in TFS:
            cur = await conn.execute(
                "DELETE FROM bars WHERE tf=? AND ts < ?",
                (tf, ts - days_for(cfg, tf) * 86400))
            total += cur.rowcount or 0
    return total


async def universe(client=None) -> list[str]:
    """Arşivlenecek semboller: tüm tickers (xyz) + PROPR'daki kripto coinler."""
    async with db() as conn:
        cur = await conn.execute("SELECT coin FROM tickers ORDER BY coin")
        coins = [r["coin"] for r in await cur.fetchall()]
    if client is None:
        return coins
    try:
        from ..hl.universe import main_dex_volumes
        from ..propr import is_listed as propr_listed
        vols = await main_dex_volumes(client)
        coins += sorted((c for c in vols if propr_listed(c)),
                        key=lambda c: -vols.get(c, 0))[:120]
    except Exception as e:
        log.warning("kripto evreni alınamadı, yalnız hisseler arşivlenecek: %s", e)
    return coins


async def refresh_all(cfg, client) -> dict:
    out = {"coins": 0, "tfs": 0, "rows": 0, "err": 0, "empty": [], "err_msg": ""}
    coins = await universe(client)
    out["coins"] = len(coins)
    for coin in coins:
        for tf in TFS:
            try:
                n = await refresh(cfg, client, coin, tf)
            except Exception as e:
                out["err"] += 1
                if not out["err_msg"]:
                    out["err_msg"] = f"{coin}/{tf}: {type(e).__name__}: {e}"
                    log.warning("mum arşivi çekilemedi (%s/%s): %s", coin, tf, e)
                continue
            out["tfs"] += 1
            out["rows"] += n
    d = await depth()
    # HL bu perp'lere her dilimde mum vermeyebilir — boş kalanlar SAYILSIN.
    # Örüntü sekmesi "yeterli veri yok" dediğinde sebebin arşiv mi eşik mi
    # olduğu ancak buradan anlaşılır.
    thin = {c.split(":")[-1] for c in coins for t in TFS if d.get(f"{c}|{t}", 0) < 50}
    out["n_empty"] = len(thin)
    out["empty"] = sorted(thin)[:40]
    out["bars"] = sum(d.values())
    out["deep"] = sum(1 for v in d.values() if v >= 500)
    await kv_set("bars_stats", {**out, "ts": now()})
    return out


async def loop(cfg, client) -> None:
    """Denetimli döngü. Site ASLA buna bağımlı değil: patlarsa yalnız örüntü
    sekmesi 'arşiv boş' der."""
    from ..health import beat
    await asyncio.sleep(90)
    while True:
        try:
            await beat("bars")
            r = await refresh_all(cfg, client)
            await prune(cfg)
            if r["rows"]:
                log.info("mum arşivi: %d sembol, %d bar yazıldı (%d hata)",
                         r["coins"], r["rows"], r["err"])
            await beat("bars")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("mum arşivi turu hatası")
        await asyncio.sleep(max(300, int(getattr(cfg, "bars_refresh_sec", 1800))))
