"""HL sembolü → TradingView sembolü.

Coin sayfasındaki katlanabilir TradingView gömülüsü için. Kendi mum
grafiğimiz HIP-3 PERP'i (`xyz:NVDA`) çizer — likidasyon/entry seviyeleri o
fiyat uzayında. TradingView ise HİSSE SENEDİNİ gösterir; karşılaştırma ve
tanıdık araçlar için oradadır, seviyeler oraya çizilemez.

Evrenin epey bir kısmının (endeks/emtia/FX ve pre-IPO adlar) TradingView
karşılığı ya yok ya tartışmalı — haritada yoksa panel HİÇ basılmaz. Uydurma
sembolle boş widget göstermek yanıltıcı olurdu.
"""
from .assets import kind
from .config import get_config
from .earnings.calendar import parse_symbol_map

# Yalnız KESİN bilinenler. ABD hisseleri için borsa öneki gerekmez —
# TradingView bare sembolü kendisi çözer; buradakiler ya ABD dışı ya da
# borsa öneki olmadan yanlış enstrümana düşenler.
DEFAULT_TV_MAP: dict[str, str] = {
    # ABD dışı hisseler
    "SMSN": "KRX:005930",       # Samsung Electronics
    "SKHX": "KRX:000660",       # SK Hynix
    "HYUNDAI": "KRX:005380",    # Hyundai Motor
    "SOFTBANK": "TSE:9984",     # SoftBank Group
    "KIOXIA": "TSE:285A",       # Kioxia Holdings
    # Emtia / endeks / FX — hisse değil ama TradingView karşılığı net
    "GOLD": "TVC:GOLD",
    "SILVER": "TVC:SILVER",
    "COPPER": "COMEX:HG1!",
    "PLATINUM": "TVC:PLATINUM",
    "PALLADIUM": "TVC:PALLADIUM",
    "BRENTOIL": "TVC:UKOIL",
    "NATGAS": "TVC:NATGAS",
    "URANIUM": "AMEX:URA",
    "SPX": "SP:SPX", "SP500": "SP:SPX",
    "NDX": "NASDAQ:NDX", "XYZ100": "NASDAQ:NDX",
    "DAX": "XETR:DAX", "JP225": "TVC:NI225",
    "KR200": "KRX:KOSPI200", "HSI": "TVC:HSI",
    "EUR": "FX:EURUSD", "JPY": "FX:USDJPY", "GBP": "FX:GBPUSD",
    "CHF": "FX:USDCHF", "AUD": "FX:AUDUSD", "CAD": "FX:USDCAD",
    "NOK": "FX:USDNOK", "TRY": "FX:USDTRY", "KRW": "FX:USDKRW",
    "CNH": "FX:USDCNH", "MXN": "FX:USDMXN",
}


def tv_symbol(symbol_or_coin: str) -> str | None:
    """TradingView sembolü ya da None (gösterilmesin)."""
    sym = (symbol_or_coin or "").split(":")[-1].upper()
    if not sym:
        return None
    override = parse_symbol_map(getattr(get_config(), "tv_symbol_map", "") or "")
    if sym in override:
        v = override[sym].strip()
        return v or None            # "SYM:" ile elle kapatılabilir
    if sym in DEFAULT_TV_MAP:
        return DEFAULT_TV_MAP[sym]
    # Haritada yok: yalnız normal hisseler bare sembolle denenir. Pre-IPO /
    # sentetik adlarda (CXMT, ZHIPU, UNITREE…) TradingView karşılığı YOK.
    if kind(sym) == "equity":
        return sym
    return None
