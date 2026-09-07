"""TWAP / düzenli birikim radarı — "birileri sessizce $5M topluyor".

TESPİT KENDİ KAYITLARIMIZDAN yapılır: aynı adres + coin + yönde, **düzenli
aralıklarla** ve **benzer boyutlarda** gelen fill dizisi. HL'nin yerel TWAP
emri işaretine bakmıyoruz — kendi botuyla dilimleyen biri de aynı şeyi yapıyor
ve aynı derecede ilginç. (Ayrıca o işareti buradan doğrulayamıyoruz.)

KÖR NOKTA — açıkça söylenmeli: yalnız YAKALAMA EŞİĞİNİN üstündeki dilimleri
görürüz. Eşiğin altına inen sabırlı bir TWAP tamamen görünmezdir ve
kaçırdığımızı bile bilemeyiz. Bu yüzden kullanıcı eşiği $5K'ya indirdi ve sayfa
bu körlüğü yazıyor.

`detect()` SAF ve TEK KAYNAK: `scorer._twap_fills` de bunu çağırır. İki ayrı
uygulama olsaydı biri düzeltilip diğeri unutulurdu ve insider skoru sessizce
sekmeden farklı davranırdı.
"""
import asyncio
import logging
import statistics

from ..db import db, kv_set, now

log = logging.getLogger("radar.twap")

MIN_SLICES = 5          # bundan az fill'de "düzenlilik" ölçülemez
CV_GAP_MAX = 0.35       # aralıkların değişkenlik katsayısı tavanı
CV_SIZE_MAX = 0.35      # dilim boyutlarının değişkenlik katsayısı tavanı
RETENTION_D = 30
# Son dilimden bu kadar süre geçmediyse tur HÂLÂ SÜRÜYOR sayılır (ortalama
# aralığın katı olarak). Süren bir TWAP, bitmiş olandan daha ilginçtir.
ACTIVE_GAPS = 3


def detect(rows: list[dict], *, cv_gap_max: float = CV_GAP_MAX,
           cv_size_max: float = CV_SIZE_MAX, min_slices: int = MIN_SLICES) -> dict | None:
    """Zaman sıralı fill dizisi → TWAP ölçümleri. Düzenli değilse None.

    `rows`: [{"ts", "sz", "notional"}] — artan zaman. Eşikler parametre:
    arşiv taraması ve skor varsayılanı kullanır; canlı radar HL'nin
    rastgeleleştirmesi için daha gevşek geçer (twaplive).
    """
    if len(rows) < min_slices:
        return None
    ts = [int(r["ts"]) for r in rows]
    sizes = [float(r["sz"] or 0) for r in rows]
    ntls = [float(r.get("notional") or 0) for r in rows]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    mg, ms = statistics.mean(gaps), statistics.mean(sizes)
    if mg <= 0 or ms <= 0:
        return None
    cv_gap = statistics.pstdev(gaps) / mg
    cv_size = statistics.pstdev(sizes) / ms
    if cv_gap >= cv_gap_max or cv_size >= cv_size_max:
        return None
    return {"n": len(rows), "first_ts": ts[0], "last_ts": ts[-1],
            "total": sum(ntls), "avg_slice": (sum(ntls) / len(rows)),
            "avg_gap": mg, "cv_gap": cv_gap, "cv_size": cv_size,
            "dur": ts[-1] - ts[0]}


def is_active(run: dict, ref_ts: int | None = None) -> bool:
    """Tur hâlâ sürüyor mu? Süren bir birikim bitmiş olandan daha ilginç."""
    ref = int(ref_ts) if ref_ts else now()
    gap = max(1.0, float(run.get("avg_gap") or 0))
    return (ref - int(run["last_ts"])) <= gap * ACTIVE_GAPS


async def _candidates(cfg) -> dict:
    """(coin, address, side) → fill dizisi. Pencere içindeki her şey."""
    hours = max(1, int(getattr(cfg, "twap_window_h", 12)))
    since = now() - hours * 3600
    async with db() as conn:
        cur = await conn.execute(
            """SELECT f.coin, f.address, f.side, f.ts, f.sz, f.notional, f.taker,
                      COALESCE(a.entity,'') entity
               FROM fills f LEFT JOIN addresses a ON a.address = f.address
               WHERE f.ts >= ? ORDER BY f.coin, f.address, f.side, f.ts""",
            (since,))
        rows = [dict(r) for r in await cur.fetchall()]
    out: dict = {}
    for r in rows:
        out.setdefault((r["coin"], r["address"], r["side"]),
                       {"entity": r["entity"], "rows": []})["rows"].append(r)
    return out


async def scan(cfg) -> dict:
    """Bir tur: pencere içindeki her (coin, adres, yön) dizisini sına.

    Tamamen YEREL — fills tablosu okunur, API isteği yoktur.
    """
    out = {"groups": 0, "detected": 0, "big": 0, "skipped_mm": 0,
           "best": None, "window_h": int(getattr(cfg, "twap_window_h", 12))}
    min_usd = float(getattr(cfg, "twap_min_usd", 5_000_000))
    out["min_usd"] = min_usd
    cands = await _candidates(cfg)
    out["groups"] = len(cands)
    ts = now()
    keep = []
    for (coin, addr, side), g in cands.items():
        # Market maker / vault düzenli işlem yapar — TWAP gibi görünür ama
        # sinyal değildir. `entity` zaten bu iş için var.
        if g["entity"] in ("mm", "vault"):
            out["skipped_mm"] += 1
            continue
        d = detect(g["rows"])
        if not d:
            continue
        out["detected"] += 1
        if d["total"] < min_usd:
            continue
        out["big"] += 1
        known = [r for r in g["rows"] if r.get("taker") is not None]
        tk = (sum(1 for r in known if r["taker"]) / len(known) * 100
              if known else None)
        keep.append((coin, addr, side, d["first_ts"], d["last_ts"], d["n"],
                     d["total"], d["avg_slice"], d["avg_gap"], d["cv_gap"],
                     d["cv_size"], tk, ts))
        if not out["best"] or d["total"] > out["best"]["total"]:
            out["best"] = {"coin": coin, "address": addr, "side": side,
                           "total": d["total"], "n": d["n"]}
    if keep:
        async with db() as conn:
            # Aynı PK'ya canlı radar da yazabilir: yalnız KENDİ kolonlarımız güncellenir,
            # canlı kolonları (src/alerted_ts/day_volume…) silinmez.
            await conn.executemany(
                """INSERT INTO twap_runs(coin,address,side,first_ts,
                     last_ts,n_slices,total,avg_slice,avg_gap,cv_gap,cv_size,
                     taker_pct,ts,src) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'fills')
                   ON CONFLICT(coin,address,side,first_ts) DO UPDATE SET
                     last_ts=excluded.last_ts, n_slices=excluded.n_slices, total=excluded.total,
                     avg_slice=excluded.avg_slice, avg_gap=excluded.avg_gap, cv_gap=excluded.cv_gap,
                     cv_size=excluded.cv_size, taker_pct=excluded.taker_pct, ts=excluded.ts,
                     src=COALESCE(src, excluded.src)""", keep)
        log.info("twap: %d grup, %d düzenli, %d büyük (≥%s)",
                 out["groups"], out["detected"], out["big"], min_usd)
    await kv_set("twap_stats", {**out, "ts": ts})
    return out


async def recent(limit: int = 80, hours: int = 48) -> list[dict]:
    """Sayfa: yakın zamanda görülen büyük TWAP turları, süren üstte."""
    async with db() as conn:
        cur = await conn.execute(
            """SELECT t.*, a.account_value, a.account_ts, a.watchlist, a.entity,
                      h.side pos_side, h.notional pos_notional
               FROM twap_runs t
               LEFT JOIN addresses a ON a.address = t.address
               LEFT JOIN hl_positions h ON h.address = t.address AND h.coin = t.coin
               WHERE t.last_ts >= ? ORDER BY t.total DESC LIMIT ?""",
            (now() - hours * 3600, limit))
        rows = [dict(r) for r in await cur.fetchall()]
    ts = now()
    # Aynı tur canlı ve arşivde farklı first_ts ile iki satır olabilir: (coin, adres,
    # yön) başına en çok dilimli olan kalır.
    best: dict[tuple, dict] = {}
    for r in rows:
        k = (r["coin"], r["address"], r["side"])
        if k not in best or int(r.get("n_slices") or 0) > int(best[k].get("n_slices") or 0):
            best[k] = r
    rows = list(best.values())
    for r in rows:
        r["active"] = is_active(r, ts) and not r.get("ended_ts")
        r["symbol"] = (r["coin"] or "").split(":")[-1]
        r["src"] = r.get("src") or "fills"
        dv, rd = r.get("day_volume"), r.get("rate_day")
        r["rate_pct"] = (float(rd) / float(dv) * 100) if dv and rd else None
        # HL TWAP emri (canlı radar sorguladıysa): plan, dolan %, kalan ($ · süre)
        pu, eu = r.get("planned_usd"), r.get("executed_usd")
        r["order_pct"] = (float(pu) / float(dv) * 100) if pu and dv else None
        r["filled_pct"] = (float(eu or 0) / float(pu) * 100) if pu else None
        left = None
        if pu and r.get("order_ts") and r.get("order_min") and not r.get("ended_ts") \
                and (r.get("order_status") or "activated") == "activated":
            left = int(r["order_ts"]) + int(float(r["order_min"]) * 60) - ts
        r["left_sec"] = max(0, left) if left is not None else None
    rows.sort(key=lambda r: (not r["active"], -(r["total"] or 0)))
    return rows


async def coverage() -> dict:
    """Fill arşivinin kapsamı — "neden boş" sorusunun cevabı burada."""
    async with db() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) n, MIN(ts) a FROM fills WHERE coin NOT LIKE '%:%'")
        cr = dict(await cur.fetchone())
        cur = await conn.execute(
            "SELECT COUNT(*) n, MIN(ts) a FROM fills WHERE coin LIKE '%:%'")
        eq = dict(await cur.fetchone())
    return {"crypto": cr, "equity": eq}


async def prune(days: int = RETENTION_D) -> int:
    async with db() as conn:
        cur = await conn.execute("DELETE FROM twap_runs WHERE last_ts < ?",
                                 (now() - days * 86400,))
        return cur.rowcount or 0


async def loop(cfg) -> None:
    """Denetimli döngü. Site ASLA buna bağımlı değil."""
    from ..health import beat
    await asyncio.sleep(200)
    while True:
        try:
            await beat("twap")
            await scan(cfg)
            await prune()
            await beat("twap")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("twap turu hatası")
        await asyncio.sleep(max(120, int(getattr(cfg, "twap_scan_sec", 600))))
