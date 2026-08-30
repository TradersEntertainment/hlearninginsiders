"""HL hisse perp evreni — HIP-3 dex'lerinden coin listesini keşfeder."""
import logging

from ..db import db, kv_get, kv_set, now
from .client import HLClient

log = logging.getLogger("hl.universe")


def norm_coin(name: str, dex: str) -> str:
    """HIP-3 coin adları 'dex:TICKER' formatında; meta bazen düz ad dönebilir."""
    if ":" in name or not dex:
        return name
    return f"{dex}:{name}"


def symbol_of(coin: str) -> str:
    return coin.split(":")[-1].upper()


async def refresh_universe(client: HLClient, equity_dexes: list[str],
                           new_out: list[dict] | None = None) -> list[str]:
    """Evreni yenile. new_out verilirse YENİ listelenen coin'ler oraya eklenir
    (tickers boşken — ilk açılış — hiçbir şey eklenmez, 100 coin spam olmasın)."""
    from ..assets import excluded_set, is_excluded
    coins: list[str] = []
    all_ok = True  # tüm dex meta'ları alındı mı (kısmi hata varsa silme yapma)
    async with db() as conn:
        cur = await conn.execute("SELECT coin FROM tickers")
        known = {r["coin"] for r in await cur.fetchall()}
    first_boot = not known
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
                if new_out is not None and not first_boot and coin not in known:
                    new_out.append({"coin": coin, "symbol": sym,
                                    "max_leverage": asset.get("maxLeverage")})
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


CRYPTO_KV = "crypto_top_coins"
CRYPTO_TTL = 3600          # ana dex hacim sıralaması saatte bir tazelensin
CRYPTO_CACHE_MIN = 50      # eşik ayardan yükseltilirse yeniden istek atmayalım


MAIN_VOL_KV = "main_dex_volumes"
MAIN_VOL_TTL = 3600


async def main_dex_volumes(client: HLClient, ttl: int = MAIN_VOL_TTL) -> dict:
    """Ana dex'teki TÜM coin'ler → 24 saatlik notional hacim.

    `top_crypto_coins` yalnız ilk N'i saklıyor; kripto hacim radarı hem TAM
    listeye (PROPR kesişimi için) hem `dayNtlVlm`'ye (birim denetimi için)
    ihtiyaç duyuyor. Aynı `metaAndAssetCtxs("")` çağrısı, ayrı önbellek.

    İstek düşerse bayat harita döner; elde hiçbir şey yoksa HATA fırlatır —
    sessizce boş dönmek "ana dexte coin yok" gibi okunurdu.
    """
    cached = await kv_get(MAIN_VOL_KV) or {}
    vols = cached.get("vols") or {}
    if vols and now() - int(cached.get("ts") or 0) < ttl:
        return {k: float(v) for k, v in vols.items()}
    try:
        data = await client.meta_and_ctxs("")
        meta, ctxs = data[0], data[1]
    except Exception as e:
        log.warning("ana dex hacim haritası alınamadı: %s", e)
        if not vols:
            raise
        return {k: float(v) for k, v in vols.items()}
    out: dict[str, float] = {}
    for asset, ctx in zip((meta or {}).get("universe") or [], ctxs or []):
        name = asset.get("name") or ""
        # ":" ana dexte olmaz; olursa HIP-3 (hisse/emtia) coinidir — kripto değil.
        if not name or asset.get("isDelisted") or ":" in name:
            continue
        try:
            out[name] = float((ctx or {}).get("dayNtlVlm") or 0)
        except (TypeError, ValueError):
            continue
    if out:
        await kv_set(MAIN_VOL_KV, {"vols": out, "ts": now()})
    return out


async def top_crypto_coins(client: HLClient, top_n: int,
                           ttl: int = CRYPTO_TTL) -> list[str]:
    """Ana dex'in (kripto) günlük hacimce en büyük ilk N coin'i.

    Bu liste `tickers`'a YAZILMAZ: kripto coin'leri earnings/hisse hattının
    hiçbir yerine girmez — tek işleri canlı akışta dev bir işlem görünce
    profil sondasını tetiklemek. kv'de önbelleklenir, yani abonelik döngüsü
    her tazelemede çağırsa bile saatte tek `metaAndAssetCtxs` isteği eder.

    İstek düşerse önceki liste (bayat da olsa) döner — dinlemeyi kesmek,
    listeyi bir saat eski kullanmaktan daha kötü.
    """
    top_n = int(top_n or 0)
    if top_n <= 0:
        return []
    cached = await kv_get(CRYPTO_KV) or {}
    coins = [c for c in (cached.get("coins") or []) if isinstance(c, str)]
    fresh = (coins and now() - int(cached.get("ts") or 0) < ttl
             and int(cached.get("want") or 0) >= top_n)
    if fresh:
        return coins[:top_n]
    try:
        data = await client.meta_and_ctxs("")
        meta, ctxs = data[0], data[1]
    except Exception as e:
        log.warning("kripto evreni alınamadı: %s", e)
        if not coins:
            # Elde bayat liste bile YOKSA bu bir cevap değil, bir HATA. Sessizce
            # [] dönmek ana dex tetiğini tamamen kapatıp sebebini yutuyordu:
            # çağıran "kripto yok" ile "bakamadım"ı ayırt edemiyordu.
            raise
        return coins[:top_n]
    ranked: list[tuple[float, str]] = []
    for asset, ctx in zip((meta or {}).get("universe") or [], ctxs or []):
        name = asset.get("name") or ""
        # ":" ana dex'te olmamalı; olursa HIP-3 coin'idir, hisse hattına ait
        if not name or asset.get("isDelisted") or ":" in name:
            continue
        try:
            vol = float((ctx or {}).get("dayNtlVlm") or 0)
        except (TypeError, ValueError):
            continue
        ranked.append((vol, name))
    if not ranked:
        return coins[:top_n]
    ranked.sort(key=lambda r: (-r[0], r[1]))
    want = max(top_n, CRYPTO_CACHE_MIN)
    out = [n for _, n in ranked[:want]]
    await kv_set(CRYPTO_KV, {"coins": out, "want": want, "ts": now()})
    log.info("kripto dinleme listesi: %d coin (%s)", min(top_n, len(out)),
             ", ".join(out[:6]) + ("…" if len(out) > 6 else ""))
    return out[:top_n]


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
