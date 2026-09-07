"""Hisse tarafı bildirim kapıları — sınıfa göre TEK kaynak.

Kullanıcı kuralı (hafta sonu anomali seli sonrası): hisselerden bildirim ancak
biri BÜYÜK pozisyon açarsa gelsin; genel işlem hacminin artması sinyal değil.
  • normal hisse (SNDK, CBRS…, kripto dex coinleri): yeni pozisyon ≥ $8M, tek
    işlem ≥ $5M, OI 24 saatte ≥ $5M artış (earnings ≤72 sa: $2.5M)
  • büyük hisse (hacimce ilk N — NVDA, TSLA…): bu kapılardan bildirim YOK
  • endeks/emtia/FX/ETF (SP500, XYZ100, GOLD, SOXL…): bildirim YOK
  • watchlist (sicilli) adres: tek işlem ≥ $250K her sınıfta (insider dönüşü)
Sayfa ve skorlama etkilenmez — yalnız Telegram kapısı. 0 = o sınıftan bildirim yok.
"""
from .. import assets
from ..hl.universe import symbol_of


def equity_class(coin: str, big_coins) -> str:
    """'non_equity' | 'major' | 'normal'. Kripto dex coinleri (para:…) hacimce ilk
    N'e girse de 'major' sayılmaz — o sınıf likit BÜYÜK hisse (NVDA) içindir."""
    c = coin or ""
    if assets.kind(symbol_of(c)) == "non_equity":
        return "non_equity"
    if assets.is_crypto_dex(c):
        return "normal"
    if c in (big_coins or ()):
        return "major"
    return "normal"


def _floor(v) -> float | None:
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def big_floor(cfg, coin: str, big_coins) -> float | None:
    """'Yeni büyük pozisyon' bildirimi için gereken boyut; None = bu sınıftan bildirim yok."""
    cls = equity_class(coin, big_coins)
    if cls == "non_equity":
        return _floor(getattr(cfg, "big_alert_index_usd", 0))
    if cls == "major":
        return _floor(getattr(cfg, "big_alert_major_usd", 0))
    return _floor(getattr(cfg, "big_alert_min_usd", 0) or getattr(cfg, "big_position_usd", 0))


def fill_floor(cfg, coin: str, big_coins, is_watch: bool) -> float | None:
    """Tek işlem (parti toplamı) bildirimi tabanı; None = bildirim yok."""
    if is_watch:
        return _floor(getattr(cfg, "whale_alert_watch_notional", 250_000))
    if equity_class(coin, big_coins) != "normal":
        return None
    return _floor(getattr(cfg, "whale_alert_notional", 5_000_000))


def oi_delta_floor(cfg, coin: str, big_coins, has_event: bool) -> float | None:
    """OI birikimi (anomali) bildirimi: 24 saatte OI'nin $ artışı bunu geçmeli; None = yok."""
    if equity_class(coin, big_coins) != "normal":
        return None
    field = "anomaly_oi_delta_event_usd" if has_event else "anomaly_oi_delta_min_usd"
    return _floor(getattr(cfg, field, 0))


def tiers(cfg) -> dict:
    """Ayar sayfası / README / tanı için özet."""
    return {"big_normal": _floor(getattr(cfg, "big_alert_min_usd", 0)),
            "big_major": _floor(getattr(cfg, "big_alert_major_usd", 0)),
            "big_index": _floor(getattr(cfg, "big_alert_index_usd", 0)),
            "fill": _floor(getattr(cfg, "whale_alert_notional", 0)),
            "fill_watch": _floor(getattr(cfg, "whale_alert_watch_notional", 0)),
            "oi_delta": _floor(getattr(cfg, "anomaly_oi_delta_min_usd", 0)),
            "oi_delta_event": _floor(getattr(cfg, "anomaly_oi_delta_event_usd", 0))}
