"""Hyperliquid'in en büyük pozisyonları — canlı tablo + rekor arşivi.

Kaynak `hl_positions`: süpürücü her adresi zaten tüm dex'lerde sorguluyor
(ana dex + HIP-3), ana dex pozisyonları elimize gelip atılıyordu; artık
saklanıyor. Aynı yanıt, ek ayrıştırma — bu tablo için ek istek yok.

DÜRÜSTLÜK SINIRI: HL'de "şu coindeki tüm pozisyonlar" endpoint'i yok. Kapsam
adres havuzu kadardır — havuz leaderboard'ı accountValue'ya göre tohumladığı
için (en zengin hesaplar) gerçek devleri büyük olasılıkla yakalar, ama
"Hyperliquid'in kesin en büyüğü" diye sunulamaz. Panel bunu yazar.
"""
import logging

from ..assets import kind as asset_kind
from ..db import db, kv_get

log = logging.getLogger("radar.bigpos")

COLS = ("coin, address, dex, side, szi, entry_px, leverage, liq_px, upnl,"
        " notional, ts, first_seen_ts, peak_notional, peak_ts, closed_ts")


# Ana dex'in "büyük" tanımı hisse tarafından ÇOK farklı: xyz:NVDA'da $1M dev,
# BTC'de gürültü. Kullanıcı kuralı: BTC/ETH $50M, diğer kripto $20M.
MAJORS = frozenset({"BTC", "ETH"})


def threshold(coin: str, cfg) -> float:
    """Bu coin'de bir pozisyonun kaydedilmeye değer sayılacağı alt sınır."""
    c = (coin or "").upper()
    if ":" in c:                                    # HIP-3 (hisse/emtia/endeks)
        return float(getattr(cfg, "hl_big_min_usd", 1_000_000))
    if c in MAJORS:
        return float(getattr(cfg, "hl_major_min_usd", 50_000_000))
    return float(getattr(cfg, "hl_crypto_min_usd", 20_000_000))


def money_short(v) -> str:
    """$20M / $1.5M / $750K — panelde kademeleri yan yana yazmak için kısa biçim
    ('$20.00M' üç kademe yan yana gelince gürültü oluyor)."""
    v = float(v or 0)
    if v >= 1_000_000:
        return f"${v / 1_000_000:g}M"
    if v >= 1_000:
        return f"${v / 1_000:g}K"
    return f"${v:g}"


def tiers(cfg) -> list[tuple[str, str]]:
    """Panelde gösterilecek (ne, ne kadar) kademeleri — tek kaynağı threshold()."""
    return [("HIP-3 hisse/emtia", money_short(threshold("xyz:X", cfg))),
            ("BTC/ETH", money_short(threshold("BTC", cfg))),
            ("diğer kripto", money_short(threshold("SOL", cfg)))]


def classify(coin: str) -> str:
    """'equity' | 'crypto' — sekme filtresi için.

    HIP-3 dex'indeki her şey (hisse, endeks, emtia, FX) 'equity' tarafıdır;
    ana dex (öneksiz coin) kriptodur. assets.kind() ayrımı hisse/emtia için,
    burada gereken ayrım 'kripto mu değil mi'.
    """
    return "equity" if ":" in (coin or "") else "crypto"


def _decorate(rows: list[dict], sym_map: dict[str, str]) -> list[dict]:
    for r in rows:
        coin = r["coin"] or ""
        r["symbol"] = sym_map.get(coin) or coin.split(":")[-1].upper()
        r["kind"] = classify(coin)
        # hisse tarafında emtia/endeks de var — ipucunda dürüstçe söylensin
        r["asset_kind"] = asset_kind(coin) if r["kind"] == "equity" else "crypto"
        r["closed"] = bool(r.get("closed_ts"))
        peak, ntl = r.get("peak_notional") or 0, r.get("notional") or 0
        # zirveden ne kadar küçülmüş (rekor görünümünde "şu an ne kadarı kaldı")
        r["off_peak"] = (1 - ntl / peak) * 100 if peak and not r["closed"] else None
    return rows


async def _sym_map() -> dict[str, str]:
    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol FROM tickers")
        return {r["coin"]: r["symbol"] for r in await cur.fetchall()}


async def live_big(limit: int = 150, min_ntl: float = 0) -> list[dict]:
    """Şu an AÇIK en büyük pozisyonlar (tüm Hyperliquid)."""
    async with db() as conn:
        cur = await conn.execute(
            f"SELECT {COLS} FROM hl_positions"
            " WHERE closed_ts IS NULL AND notional >= ?"
            " ORDER BY notional DESC LIMIT ?", (min_ntl, limit))
        rows = [dict(r) for r in await cur.fetchall()]
    return _decorate(rows, await _sym_map())


async def record_big(limit: int = 150, min_ntl: float = 0) -> list[dict]:
    """Şimdiye kadar GÖRDÜĞÜMÜZ en büyükler — kapanmışlar dahil."""
    async with db() as conn:
        cur = await conn.execute(
            f"SELECT {COLS} FROM hl_positions WHERE peak_notional >= ?"
            " ORDER BY peak_notional DESC LIMIT ?", (min_ntl, limit))
        rows = [dict(r) for r in await cur.fetchall()]
    return _decorate(rows, await _sym_map())


async def stats(cfg=None) -> dict:
    """Panel başlığı + boş durum teşhisi.

    'İzlediğimiz adres' sayısı süpürme HAVUZUNDAN gelir (sweep_stats), tablodan
    DEĞİL: eskiden COUNT(DISTINCT address) FROM hl_positions kullanılıyordu, o
    da "≥eşik pozisyonu OLAN adres" demek — tablo boşken 0 yazıyor, doluyken de
    gerçek havuzun (1500+) küçük bir alt kümesini gösteriyordu.
    """
    async with db() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT address) a,"
            " SUM(CASE WHEN closed_ts IS NULL THEN 1 ELSE 0 END) open_n,"
            " MAX(ts) last_ts FROM hl_positions")
        r = await cur.fetchone()
    sw = await kv_get("sweep_stats") or {}
    hot = int(sw.get("hot") or 0)
    cursor = int(sw.get("cursor") or 0)
    return {
        "rows": r["n"] or 0, "hit_addrs": r["a"] or 0,
        "open": r["open_n"] or 0, "last_ts": r["last_ts"],
        # süpürme durumu — "neden boş?" sorusunun cevabı
        "watched": hot + int(sw.get("cold") or 0),
        "swept_pct": round(cursor / hot * 100) if hot else 0,
        "tour_min": int(sw.get("tour_min") or 0),
        "last_sweep": sw.get("ts"),
        "hl_err": int(sw.get("hl_err") or 0),
        # partinin tamamı patladıysa NEDENİ — teşhis log'a gömülü kalmasın
        "err_msg": (sw.get("err_msg") or "") if (sw.get("err") and not sw.get("ok")) else "",
        "started": bool(sw),
        # eşik artık tek sayı değil: coin türüne göre kademeli
        "tiers": tiers(cfg) if cfg is not None else [],
    }
