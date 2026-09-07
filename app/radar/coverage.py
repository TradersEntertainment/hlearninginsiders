"""Kapsama: havuzdaki pozisyonlar HL açık pozisyonunun (OI) yüzde kaçı.

"Bizimki neden eksik" sorusunun dürüst cevabı. HL'de "bu marketteki tüm
pozisyonlar" API'si yok; yalnız tanıdığımız adreslerin defteri sorgulanır. Dış
ısı haritaları ya tüm zinciri indeksler ya da kaldıraç modeliyle TAHMİN eder;
bizimki gerçek pozisyonlardır — yüzde, iki haritayı dürüstçe kıyaslar.

HL `openInterest` TEK TARAFLIDIR: long toplamı = short toplamı = OI × mark.
`pct_long` = havuz long $ / oi_ntl, `pct_short` = havuz short $ / oi_ntl — ikisi
birbirinden bağımsız %100'e yaklaşabilir. %100 ÜSTÜ = bayat satır (kapanan
pozisyon hâlâ tabloda); gizlenmez, ⚠️ ile yazılır — kapsama sayısı bir veri
kalitesi gerçeğidir, tahmin değil.

Ağ isteği yok: pozisyon toplamları SQL, OI `asset_metrics` (HIP-3, metrik turu)
ya da `main_dex_ctx` kv'si (ana dex). Sayfa/PNG/mesaj/`/tani` aynı hesabı kullanır.
"""
from statistics import median

from ..db import db, kv_get

# /tani listesine giren coinlerin asgari HL OI'si: küçük OI'de yüzde anlamsız oynar
COVERAGE_MIN_OI = 250_000


def pct(part, whole) -> float | None:
    """part/whole × 100; whole yoksa/0 ise None (oran hesaplanamaz ≠ %0)."""
    try:
        p, w = float(part or 0), float(whole or 0)
    except (TypeError, ValueError):
        return None
    if w <= 0:
        return None
    return p / w * 100


async def pool_sides(coin: str, kind: str) -> dict:
    """Havuzdaki açık pozisyon toplamları yön yön: {long, short, n, latest_ts}.
    kind='crypto' (ana dex) → addr_positions (açık), diğerleri → positions_current."""
    if kind == "crypto":
        q = ("SELECT side, SUM(notional) s, COUNT(*) n, MAX(ts) t FROM addr_positions"
             " WHERE coin=? AND closed_ts IS NULL AND notional > 0 GROUP BY side")
    else:
        q = ("SELECT side, SUM(notional) s, COUNT(*) n, MAX(ts) t FROM positions_current"
             " WHERE coin=? AND notional > 0 GROUP BY side")
    out = {"long": 0.0, "short": 0.0, "n": 0, "latest_ts": None}
    async with db() as conn:
        cur = await conn.execute(q, (coin,))
        for r in await cur.fetchall():
            side = r["side"] if r["side"] in ("long", "short") else None
            if side:
                out[side] += float(r["s"] or 0)
            out["n"] += int(r["n"] or 0)
            if r["t"]:
                out["latest_ts"] = max(out["latest_ts"] or 0, int(r["t"]))
    return out


async def oi_ntl_for(coin: str, kind: str, summ: dict | None = None, ctx: dict | None = None) -> float | None:
    """HL OI (tek taraflı, $): verilmişse sayfa özetinden (`summ.oi_ntl`), yoksa ana dex
    kv'sinden (oi × mark) ya da asset_metrics özetinden. Bilinmiyorsa None."""
    if summ is not None:
        v = float(summ.get("oi_ntl") or 0)
        return v if v > 0 else None
    if kind == "crypto":
        from ..hl.universe import MAIN_CTX_KV
        rec = ctx if ctx is not None else (await kv_get(MAIN_CTX_KV) or {})
        c = (rec.get("c") or {}).get(coin) or {}
        try:
            v = float(c.get("oi") or 0) * float(c.get("m") or 0)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None
    from .metrics import summary
    v = float((await summary(coin)).get("oi_ntl") or 0)
    return v if v > 0 else None


async def coverage(coin: str, kind: str, cfg=None, summ: dict | None = None,
                   ctx: dict | None = None) -> dict:
    """{long, short, oi_ntl, pct_long, pct_short, n, latest_ts, over, scan}.
    `scan` (yalnız HIP-3): son tam taramanın {ts, n_addrs, n_found} sayımı."""
    sides = await pool_sides(coin, kind)
    oi = await oi_ntl_for(coin, kind, summ, ctx)
    pl, ps = pct(sides["long"], oi), pct(sides["short"], oi)
    scan = None
    if kind != "crypto":
        async with db() as conn:
            cur = await conn.execute("SELECT ts, n_addrs, n_found FROM scans WHERE coin=?", (coin,))
            row = await cur.fetchone()
        scan = dict(row) if row else None
    return {"long": sides["long"], "short": sides["short"], "oi_ntl": oi,
            "pct_long": pl, "pct_short": ps, "n": sides["n"], "latest_ts": sides["latest_ts"],
            "over": bool((pl or 0) > 100 or (ps or 0) > 100), "scan": scan}


def txt(cov: dict | None) -> str:
    """PNG alt başlığı için kısa metin; oran yoksa boş."""
    if not cov or cov.get("pct_long") is None or cov.get("pct_short") is None:
        return ""
    return f"kapsama L %{cov['pct_long']:.0f} · S %{cov['pct_short']:.0f}"


async def overview(cfg=None, limit: int = 5) -> dict:
    """/tani: izlenen (tickers) coinlerde kapsaması en düşük olanlar.
    {worst: [{coin, symbol, dex, pct_long, pct_short, oi_ntl}], n, median} —
    OI ≥ COVERAGE_MIN_OI olanlar; sıralama min(pct_long, pct_short) artan."""
    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol, dex FROM tickers")
        tick = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT coin, side, SUM(notional) s FROM positions_current WHERE notional > 0 GROUP BY coin, side")
        sums: dict[str, dict[str, float]] = {}
        for r in await cur.fetchall():
            sums.setdefault(r["coin"], {})[r["side"]] = float(r["s"] or 0)
        cur = await conn.execute(
            """SELECT a.coin, a.mark_px, a.oi FROM asset_metrics a
               JOIN (SELECT coin, MAX(ts) mts FROM asset_metrics GROUP BY coin) b
                 ON a.coin=b.coin AND a.ts=b.mts""")
        oi = {r["coin"]: float(r["oi"] or 0) * float(r["mark_px"] or 0) for r in await cur.fetchall()}
    rows = []
    for t in tick:
        o = oi.get(t["coin"]) or 0
        if o < COVERAGE_MIN_OI:
            continue
        s = sums.get(t["coin"], {})
        pl, ps = pct(s.get("long", 0), o), pct(s.get("short", 0), o)
        # dex etiketi: tickers.dex boşsa coin önekinden (xyz:MU → xyz), öneksiz → ana dex
        dex = t["dex"] or (t["coin"].split(":")[0] if ":" in (t["coin"] or "") else "")
        rows.append({"coin": t["coin"], "symbol": t["symbol"], "dex": dex,
                     "pct_long": pl or 0.0, "pct_short": ps or 0.0, "oi_ntl": o})
    rows.sort(key=lambda r: min(r["pct_long"], r["pct_short"]))
    med = median([min(r["pct_long"], r["pct_short"]) for r in rows]) if rows else None
    return {"worst": rows[:limit], "n": len(rows), "median": med}
