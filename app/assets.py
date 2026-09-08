"""Enstrüman sınıflandırma.

HL'nin `xyz` dex'inde hisse (EQ) yanında endeks, emtia, FX ve ETF'ler de var.
Bunların bilançosu yok — takvim sorgusuna sokmak boşa istek ve log gürültüsü.
"""
from .config import get_config

# Bilanço açıklamayan enstrümanlar
NON_EQUITY: frozenset[str] = frozenset({
    # Endeksler
    "XYZ100", "SP500", "KR200", "JP225", "SPX", "NDX", "DAX", "HSI",
    # Emtia
    "GOLD", "SILVER", "COPPER", "PLATINUM", "PALLADIUM", "BRENTOIL", "NATGAS",
    "CL", "WTI", "OIL", "CORN", "WHEAT", "URANIUM",
    # FX
    "EUR", "JPY", "GBP", "NOK", "CHF", "AUD", "CAD", "TRY", "KRW", "CNH", "MXN",
    # ETF / sepet (bilanço açıklamaz)
    "SMH", "XLE", "XLF", "XLK", "URNM", "EWY", "EWJ", "EWT", "EWZ", "EWW",
    "KORU", "SOXL", "SOXX", "QQQ", "SPY", "IWM", "ARKK", "GDX", "TLT", "LIT",
    # Kripto (ana dex'ten sızarsa)
    "BTC", "ETH", "SOL", "HYPE", "XRP", "DOGE", "BNB", "ADA", "AVAX", "LINK",
    "SUI", "NEAR", "TAO", "ZEC", "XMR", "LTC", "BCH", "TRX", "DOT", "ATOM",
    "PURR", "PUMP", "FARTCOIN", "WLD", "ONDO", "ENA", "AAVE", "UNI", "PAXG",
})

# Halka açık bilanço takvimi olmayan (pre-IPO / sentetik / sepet) enstrümanlar
NO_CALENDAR: frozenset[str] = frozenset({
    "CXMT", "GIGADEV", "MINIMAX", "ZHIPU", "UNITREE", "PURRDAT", "DRAM",
    "NCLD", "BOT", "LYTE", "SPCX",
})


def _extra(name: str) -> set[str]:
    raw = getattr(get_config(), name, "") or ""
    return {s.strip().upper() for s in raw.replace(";", ",").split(",") if s.strip()}


# ---- HIP-3 KRİPTO dex'leri (ör. `para`: para:ANSEM). Veri hattı hisse gibi (tickers,
# positions_current, asset_metrics — süpürücü adres başına +1 istek), SINIF kripto:
# bilanço takvimi yok, mesajlarda "kripto", kripto kanalına gider, endeks/emtia kapıları
# uygulanmaz. Hangi sembollerin bu dex'lerde olduğu evren yenilemesinde kv'ye yazılır
# (`crypto_dex_symbols`) ve açılışta yüklenir — `kind("ANSEM")` gibi öneksiz çağrılar da
# doğru sınıfı bulsun.
CRYPTO_DEX_SYMBOLS: set[str] = set()
CRYPTO_DEX_KV = "crypto_dex_symbols"


def crypto_dexes(cfg=None) -> list[str]:
    raw = getattr(cfg or get_config(), "crypto_dexes", None) or []
    if isinstance(raw, str):
        raw = raw.replace(";", ",").split(",")
    return [str(d).strip() for d in raw if str(d).strip()]


def watched_dexes(cfg) -> list[str]:
    """İzlenen tüm HIP-3 dex'leri: hisse dex'leri + kripto dex'leri (sırayla, tekrarsız)."""
    out = [str(d).strip() for d in (getattr(cfg, "equity_dexes", None) or []) if str(d).strip()]
    for d in crypto_dexes(cfg):
        if d not in out:
            out.append(d)
    return out


def is_crypto_dex(coin_or_symbol: str, cfg=None) -> bool:
    s = (coin_or_symbol or "").strip()
    if not s:
        return False
    if ":" in s:
        return s.split(":")[0] in set(crypto_dexes(cfg))
    return s.upper() in CRYPTO_DEX_SYMBOLS


def fill_floor_for(cfg, coin: str) -> float:
    """Bu coin'de kaç dolardan büyük İŞLEMLER havuza yazılsın: kripto dex (para:ANSEM)
    → `crypto_dex_fill_min_notional` ($1K), diğer tickers coinleri → `min_fill_notional`.
    Ana dex kripto tabanı collector'da ayrı (crypto_fill_min_notional)."""
    if is_crypto_dex(coin, cfg):
        return float(getattr(cfg, "crypto_dex_fill_min_notional", 1000) or 0)
    return float(cfg.min_fill_notional)


def position_floor_for(cfg, coin: str) -> float:
    """Bu coin'de kaç dolardan büyük POZİSYONLAR yazılsın (toz filtresi): kripto dex →
    `crypto_dex_min_position_notional` ($1K), diğerleri → `min_position_notional` ($10K).
    Yalnız kayıt tabanı — bildirim eşikleri (autoscan/liqwatch/lowvol, $1M+) ayrıdır."""
    if is_crypto_dex(coin, cfg):
        return float(getattr(cfg, "crypto_dex_min_position_notional", 1000) or 0)
    return float(cfg.min_position_notional)


def set_crypto_dex_symbols(syms) -> None:
    CRYPTO_DEX_SYMBOLS.clear()
    CRYPTO_DEX_SYMBOLS.update(str(x).upper() for x in (syms or []) if str(x).strip())


async def save_crypto_dex_symbols(syms) -> None:
    from .db import kv_set, now
    set_crypto_dex_symbols(syms)
    await kv_set(CRYPTO_DEX_KV, {"syms": sorted(CRYPTO_DEX_SYMBOLS), "ts": now()})


async def load_crypto_dex_symbols() -> int:
    """Açılış: evren yenilemesi koşana kadar kv'deki son küme geçerli."""
    from .db import kv_get
    rec = await kv_get(CRYPTO_DEX_KV) or {}
    set_crypto_dex_symbols(rec.get("syms") or [])
    return len(CRYPTO_DEX_SYMBOLS)


# Spot çiftlerinin borsa kimliği okunur değildir: HL onlara '@272' der, insan
# 'PURR/USDC' okur. Okunur ad `hl.universe.top_spot_coins` ile kv'ye yazılır;
# burada modül düzeyinde tutulur ki `label()` SENKRON olsun — Telegram
# biçimleyicileri, şablon macro'ları ve teşhis satırları await edemez.
# (İlk canlı spot TWAP alarmı "⏳ TWAP · @272" diye düşmüştü.)
SPOT_NAMES: dict[str, str] = {}
SPOT_NAMES_KV = "spot_top_coins"          # kaynak: hl.universe.SPOT_KV


def is_spot(coin_or_symbol: str) -> bool:
    """Spot çifti mi — HL spot kimliği '@N' biçimindedir (perp'te '@' yok)."""
    return (coin_or_symbol or "").startswith("@")


def label(coin: str) -> str:
    """GÖSTERİM adı — kimlik DEĞİL. Spot '@107' → 'PURR/USDC', HIP-3
    'para:UANSEM' → 'UANSEM', ana dex 'INJ' → 'INJ'. Ad henüz gelmediyse ham
    kimlik döner (eski davranış; mesaj yine gider). Büyük harfe ÇEVİRMEZ:
    HL'den gelen adlar zaten doğru biçimde."""
    c = coin or ""
    if is_spot(c):
        return SPOT_NAMES.get(c) or c
    return c.split(":")[-1]


def spot_coin_of(name: str) -> str:
    """Ters arama: 'PURR/USDC' → '@107'. Bulunamazsa boş — çağıran ham girdiyle
    devam eder ('/twap PURR/USDC' yazan kullanıcı için)."""
    s = (name or "").strip().upper()
    if not s:
        return ""
    if is_spot(s):
        return s
    for coin, lbl in SPOT_NAMES.items():
        if str(lbl).upper() == s:
            return coin
    return ""


def set_spot_names(names) -> None:
    SPOT_NAMES.clear()
    SPOT_NAMES.update({str(k): str(v) for k, v in (names or {}).items()
                       if str(k).strip() and str(v).strip()})


async def save_spot_names(names) -> None:
    """Evren tarafı kv'yi kendi yazıyor; burada yalnız bellek tazelenir."""
    set_spot_names(names)


async def load_spot_names() -> int:
    """Açılış: spot evreni yenilenene kadar kv'deki son ad listesi geçerli."""
    from .db import kv_get
    rec = await kv_get(SPOT_NAMES_KV) or {}
    set_spot_names(rec.get("names") or {})
    return len(SPOT_NAMES)


def excluded_set() -> set[str]:
    """Tamamen takip dışı semboller (evren + tarama + takvim yok)."""
    return _extra("exclude_symbols")


def is_excluded(symbol_or_coin: str) -> bool:
    sym = (symbol_or_coin or "").split(":")[-1].upper()
    return sym in excluded_set()


def kind(symbol_or_coin: str) -> str:
    """'equity' | 'non_equity' | 'no_calendar' | 'crypto' (HIP-3 kripto dex'i ör.
    para:ANSEM, ya da spot çifti ör. @107 — ikisinin de bilanço takvimi yok)"""
    if is_crypto_dex(symbol_or_coin or "") or is_spot(symbol_or_coin or ""):
        return "crypto"          # spot çifti de kripto: bilanço takvimi yok
    sym = (symbol_or_coin or "").split(":")[-1].upper()
    if sym in NON_EQUITY or sym in _extra("non_equity_extra"):
        return "non_equity"
    if sym in NO_CALENDAR or sym in _extra("no_calendar_extra"):
        return "no_calendar"
    return "equity"


def is_index_perp(coin_or_symbol: str) -> bool:
    """HIP-3 (xyz:) üzerindeki endeks/emtia/FX perp'i mi: SP500, XYZ100, EUR, CL…
    Ana dex kripto (BTC, HYPE — ön eksiz) DEĞİL: ana sayfa liq haritası bunları
    hisselerle birlikte tutar, endeks/emtia/FX'i 📐 çipine ayırır. Tek kaynak:
    route, duvar süzgeci ve test bunu kullanır."""
    s = coin_or_symbol or ""
    return ":" in s and kind(s) == "non_equity"


def has_earnings(symbol_or_coin: str) -> bool:
    """Bu enstrüman için bilanço takvimi aranmalı mı?"""
    return kind(symbol_or_coin) == "equity"


def klass(coin_or_symbol: str) -> str:
    """Görünüm/yönlendirme sınıfı: 'kripto' (ana dex ya da kripto dex) | 'endeks' | 'hisse'.
    Coin kimliği bekler (öneksiz = ana dex kripto; 'xyz:SP500' = endeks; 'para:ANSEM' = kripto)."""
    s = coin_or_symbol or ""
    if ":" not in s or is_crypto_dex(s):
        return "kripto"
    return "endeks" if kind(s) == "non_equity" else "hisse"
