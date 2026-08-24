"""HL hisse perp evreni — HIP-3 dex'lerinden coin listesini keşfeder."""
import logging

from ..db import db, now
from .client import HLClient

log = logging.getLogger("hl.universe")


def norm_coin(name: str, dex: str) -> str:
    """HIP-3 coin adları 'dex:TICKER' formatında; meta bazen düz ad dönebilir."""
    if ":" in name or not dex:
        return name
    return f"{dex}:{name}"


def symbol_of(coin: str) -> str:
    return coin.split(":")[-1].upper()


async def refresh_universe(client: HLClient, equity_dexes: list[str]) -> list[str]:
    from ..assets import excluded_set, is_excluded
    coins: list[str] = []
    all_ok = True  # tüm dex meta'ları alındı mı (kısmi hata varsa silme yapma)
    for dex in equity_dexes:
        try:
            meta = await client.meta(dex)
        except Exception as e:
            log.warning("meta(%s) alınamadı: %s", dex, e)
            all_ok = False
            continue
        universe = (meta or {}).get("universe") or []
        async with db() as conn:
            for asset in universe:
                name = asset.get("name") or ""
                if not name or asset.get("isDelisted"):
                    continue
                coin = norm_coin(name, dex)
                sym = symbol_of(coin)
                if is_excluded(sym):
                    continue  # kullanıcı bu hisseyi tamamen takip dışı bıraktı
                coins.append(coin)
                await conn.execute(
                    """INSERT INTO tickers(coin,dex,symbol,name,max_leverage,listed_at)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(coin) DO UPDATE SET
                         dex=excluded.dex, symbol=excluded.symbol,
                         max_leverage=excluded.max_leverage""",
                    (coin, dex, sym, name, asset.get("maxLeverage"), now()),
                )
    # Hariç tutulanların eski kayıtları da düşsün (autoscan/duvar/saat istatistiği
    # tickers'ı taradığı için buradan silinince her yerden çıkarlar)
    exc = excluded_set()
    if exc:
        q = ",".join("?" * len(exc))
        async with db() as conn:
            await conn.execute(
                f"DELETE FROM tickers WHERE UPPER(symbol) IN ({q})", tuple(exc))
    # Delist olan / meta'dan tamamen düşen coin'leri de temizle — eskiden yalnız
    # INSERT'te atlanıyor, tickers satırı kalıyordu → autoscan ölü marketi
    # taramaya, bookwall boş l2Book çağırmaya, dashboard hayalet coin göstermeye
    # devam ediyordu. YALNIZ tüm dex'ler başarıyla alındıysa sil (kısmi hatada
    # canlı coin'i yanlışlıkla düşürme).
    if all_ok and coins:
        placeholders = ",".join("?" * len(coins))
        async with db() as conn:
            cur = await conn.execute(
                f"DELETE FROM tickers WHERE coin NOT IN ({placeholders})", tuple(coins))
            if cur.rowcount:
                log.info("evrenden düşen %d coin tickers'tan silindi", cur.rowcount)
    if coins:
        log.info("evren yenilendi: %d coin (%s)", len(coins), ", ".join(coins[:8]) + ("…" if len(coins) > 8 else ""))
    return coins


async def get_universe() -> list[dict]:
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM tickers ORDER BY symbol")
        return [dict(r) for r in await cur.fetchall()]


async def find_ticker(symbol_or_coin: str) -> dict | None:
    s = symbol_or_coin.strip()
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM tickers WHERE coin=? OR UPPER(symbol)=? LIMIT 1",
            (s, s.upper()),
        )
        row = await cur.fetchone()
        return dict(row) if row else None
