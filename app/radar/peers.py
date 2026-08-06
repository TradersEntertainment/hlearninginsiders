"""Korele hisse eşlemesi — insider bazen doğrudan hisse yerine sektör
komşusuyla oynar (SNDK earnings'inde WDC/MU pozisyonu gibi).

PEERS env ile override: "SNDK:WDC|MU;TSLA:RIVN"
"""
DEFAULT_PEERS: dict[str, list[str]] = {
    # depolama / bellek
    "SNDK": ["WDC", "MU", "STX"],
    "WDC": ["SNDK", "MU", "STX"],
    "MU": ["SNDK", "WDC"],
    "STX": ["WDC", "SNDK"],
    # yarı iletken
    "NVDA": ["AMD", "AVGO", "TSM", "SMCI"],
    "AMD": ["NVDA", "AVGO", "INTC"],
    "AVGO": ["NVDA", "AMD", "TSM"],
    "TSM": ["NVDA", "AVGO"],
    "INTC": ["AMD", "NVDA"],
    "SMCI": ["NVDA", "DELL"],
    "ASML": ["TSM", "NVDA"],
    "QCOM": ["AVGO", "AMD"],
    "ARM": ["NVDA", "QCOM"],
    "MRVL": ["AVGO", "NVDA"],
    # mega tech
    "AAPL": ["MSFT", "GOOGL"],
    "MSFT": ["GOOGL", "AMZN"],
    "GOOGL": ["META", "MSFT"],
    "META": ["GOOGL", "SNAP"],
    "AMZN": ["MSFT", "GOOGL"],
    "NFLX": ["DIS"],
    # EV / oto
    "TSLA": ["RIVN", "LCID", "NIO"],
    "RIVN": ["TSLA", "LCID"],
    "LCID": ["TSLA", "RIVN"],
    # kripto-finans
    "COIN": ["HOOD", "MSTR"],
    "HOOD": ["COIN", "SOFI"],
    "MSTR": ["COIN"],
    "CRCL": ["COIN"],
    # diğer
    "ORCL": ["MSFT", "AMZN"],
    "CRM": ["MSFT", "ORCL"],
    "UBER": ["LYFT"],
    "PYPL": ["SQ", "SOFI"],
}


def parse_override(s: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for part in s.split(";"):
        part = part.strip()
        if ":" not in part:
            continue
        sym, rest = part.split(":", 1)
        peers = [p.strip().upper() for p in rest.split("|") if p.strip()]
        if sym.strip() and peers:
            out[sym.strip().upper()] = peers
    return out


def peers_of(symbol: str, override_env: str = "") -> list[str]:
    table = dict(DEFAULT_PEERS)
    if override_env:
        table.update(parse_override(override_env))
    return table.get(symbol.upper(), [])
