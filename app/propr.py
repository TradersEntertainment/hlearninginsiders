"""propr.xyz listesi — kullanıcının trade ettiği platform.

Bir sinyal propr'da da listeli bir enstrümana aitse alert'lere ve dashboard'a
"✅ PROPR'da listeli — işlem açabilirsin" işareti eklenir.
Liste propr.xyz/universe'ten alındı; /settings'teki "propr_symbols" ile genişletilebilir.
"""
from .config import get_config

DEFAULT_PROPR: frozenset[str] = frozenset({
    # CRYPTO
    "BTC", "ETH", "HYPE", "SOL", "ZEC", "CASHCAT", "PUMP", "XRP", "UNI", "WLD",
    "LIT", "BNB", "XPL", "FARTCOIN", "AAVE", "ENA", "PAXG", "ONDO", "KPEPE",
    "ADA", "LTC", "KAITO", "XMR", "AVAX", "SUI", "NEAR", "TAO", "VVV", "LINK",
    "DOGE", "PENGU", "ARB", "PENDLE", "CRV", "JUP", "ETHFI", "KSHIB", "BCH",
    "LDO", "XLM", "SPX", "CC", "SUSHI", "ASTER", "ZRO", "KBONK", "APT", "GRAM",
    "INJ", "GRASS", "TRX", "DOT", "MON", "OP", "JTO", "TRUMP", "FET", "RENDER",
    "WLFI", "SEI", "MORPHO", "FIL", "ETC", "VIRTUAL", "AERO", "ALGO", "TIA",
    "HBAR", "POL", "STBL", "ATOM", "STRK", "MNT", "CHIP", "POPCAT", "WIF",
    "EIGEN", "PYTH", "MEGA", "CFX", "LINEA", "PEOPLE", "PURR", "W", "SKY",
    "KAS", "ICP", "DYDX", "MET", "BERA", "SYRUP", "SAGA", "RUNE", "STABLE",
    "ENS", "BOME",
    # EQ / IDX
    "SNDK", "SKHX", "XYZ100", "SP500", "MU", "SPCX", "DRAM", "SKHY", "NVDA",
    "INTC", "SMSN", "GOOGL", "AMD", "CRCL", "EWY", "META", "MSFT", "ORCL",
    "AAPL", "MRVL", "AMZN", "PLTR", "CXMT", "TSLA", "NBIS", "KIOXIA", "DELL",
    "CBRS", "MSTR", "MINIMAX", "WDC", "LITE", "TSM", "COIN", "LLY", "HOOD",
    "CRWV", "ZHIPU", "BE", "UNITREE", "AVGO", "RKLB", "BB", "ARM", "STRC",
    "PURRDAT", "NFLX", "HYUNDAI", "BABA", "JP225", "KR200", "QCOM", "SMH",
    "GIGADEV", "HIMS", "QNT", "NOW", "USAR", "SHAZ", "IBM", "ASML", "BOT",
    "GME", "XLE", "URNM", "SHEIN", "MRNA",
    # CMDTY / FX
    "CL", "GOLD", "BRENTOIL", "SILVER", "COPPER", "NATGAS", "PLATINUM",
    "PALLADIUM", "JPY", "EUR", "NOK",
})


def is_listed(symbol_or_coin: str) -> bool:
    sym = (symbol_or_coin or "").split(":")[-1].upper()
    if sym in DEFAULT_PROPR:
        return True
    extra = getattr(get_config(), "propr_symbols", "") or ""
    return sym in {s.strip().upper() for s in extra.split(",") if s.strip()}


PROPR_NOTE = "✅ <b>PROPR'da listeli</b> — işlem açabilirsin"
