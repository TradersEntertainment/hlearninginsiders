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


def kind(symbol_or_coin: str) -> str:
    """'equity' | 'non_equity' | 'no_calendar'"""
    sym = (symbol_or_coin or "").split(":")[-1].upper()
    if sym in NON_EQUITY or sym in _extra("non_equity_extra"):
        return "non_equity"
    if sym in NO_CALENDAR or sym in _extra("no_calendar_extra"):
        return "no_calendar"
    return "equity"


def has_earnings(symbol_or_coin: str) -> bool:
    """Bu enstrüman için bilanço takvimi aranmalı mı?"""
    return kind(symbol_or_coin) == "equity"
