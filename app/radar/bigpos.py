"""Hyperliquid'in en büyük pozisyonları — canlı tablo + rekor arşivi.

Kaynak `hl_positions`: süpürücü zaten her adresi `ALL_DEXES` ile sorguluyordu,
ana dex (BTC/ETH…) pozisyonları elimize gelip atılıyordu; artık saklanıyor.
Ek API isteği yok.

DÜRÜSTLÜK SINIRI: HL'de "şu coindeki tüm pozisyonlar" endpoint'i yok. Kapsam
adres havuzu kadardır — havuz leaderboard'ı accountValue'ya göre tohumladığı
için (en zengin hesaplar) gerçek devleri büyük olasılıkla yakalar, ama
"Hyperliquid'in kesin en büyüğü" diye sunulamaz. Panel bunu yazar.
"""
import logging

from ..assets import kind as asset_kind
from ..db import db

log = logging.getLogger("radar.bigpos")

COLS = ("coin, address, dex, side, szi, entry_px, leverage, liq_px, upnl,"
        " notional, ts, first_seen_ts, peak_notional, peak_ts, closed_ts")


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


async def stats() -> dict:
    """Panel başlığı için: kaç adres izleniyor, kaç kayıt var, en son ne zaman."""
    async with db() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT address) a,"
            " SUM(CASE WHEN closed_ts IS NULL THEN 1 ELSE 0 END) open_n,"
            " MAX(ts) last_ts FROM hl_positions")
        r = await cur.fetchone()
    return {"rows": r["n"] or 0, "addrs": r["a"] or 0,
            "open": r["open_n"] or 0, "last_ts": r["last_ts"]}
