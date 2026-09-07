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

# Ana dex'in CANLI özeti: coin → mark / OI / funding / 24s hacim / önceki gün
# fiyatı. `asset_metrics` yalnız PROPR'daki coinleri saklıyor (200+ coinin OI
# geçmişini tutmanın faydası yok); kripto liq radarı ve kripto coin sayfası
# ise HER ana dex coininin ŞİMDİKİ fiyatını istiyor — geçmişini değil. Tek kv
# satırı, metrik döngüsü zaten çektiği yanıttan yazar (ek istek yok).
MAIN_CTX_KV = "main_dex_ctx"
MAIN_CTX_TTL = 150


def parse_main_dex_ctx(meta, ctxs) -> dict[str, dict]:
    """metaAndAssetCtxs("") → {coin: {m: mark, oi: adet, f: funding, v: 24s $, p: önceki gün}}.

    `prevDayPx` ilk kez burada okunuyor: yoksa None (24 saatlik değişim boş
    kalır, uydurulmaz). `openInterest` ADET — $ için mark ile çarpılır
    (metrics.summary ile aynı).
    """
    out: dict[str, dict] = {}
    for asset, ctx in zip((meta or {}).get("universe") or [], ctxs or []):
        name = asset.get("name") or ""
        if not name or asset.get("isDelisted") or ":" in name:
            continue
        ctx = ctx or {}
        try:
            rec = {"m": float(ctx.get("markPx") or 0),
                   "oi": float(ctx.get("openInterest") or 0),
                   "f": float(ctx.get("funding") or 0),
                   "v": float(ctx.get("dayNtlVlm") or 0)}
        except (TypeError, ValueError):
            continue
        try:
            rec["p"] = float(ctx.get("prevDayPx")) if ctx.get("prevDayPx") else None
        except (TypeError, ValueError):
            rec["p"] = None
        if rec["m"] > 0:
            out[name] = rec
    return out


async def store_main_dex_ctx(meta, ctxs) -> dict[str, dict]:
    """Eldeki yanıttan kv'yi yaz (metrik döngüsü: ek istek yok)."""
    c = parse_main_dex_ctx(meta, ctxs)
    if c:
        await kv_set(MAIN_CTX_KV, {"c": c, "ts": now()})
    return c


async def main_dex_ctx(client: HLClient | None, ttl: int = MAIN_CTX_TTL,
                       fetch: bool = True) -> dict:
    """{"c": {coin: …}, "ts"} — kv tazeyse o; değilse (ve `fetch`) tek istekle
    yenile. İstek düşerse bayat kayıt döner; elde hiçbir şey yoksa HATA fırlatır
    (sessiz boş "ana dexte coin yok" gibi okunurdu). `fetch=False`: yalnız kv
    (sayfa yolu — sayfa açılışı HL'ye istek atmaz)."""
    cached = await kv_get(MAIN_CTX_KV) or {}
    c = cached.get("c") or {}
    if c and now() - int(cached.get("ts") or 0) < ttl:
        return cached
    if not fetch or client is None:
        return cached
    try:
        data = await client.meta_and_ctxs("")
        meta, ctxs = data[0], data[1]
    except Exception as e:
        log.warning("ana dex ctx alınamadı: %s", e)
        if not c:
            raise
        return cached
    fresh = await store_main_dex_ctx(meta, ctxs)
    return {"c": fresh, "ts": now()} if fresh else cached


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


async def crypto_names() -> dict[str, str]:
    """Ana dex coin adları: BÜYÜK HARF → gerçek ad ('KPEPE' → 'kPEPE').

    Ağ yok, yalnız kv (`main_dex_ctx` ∪ `main_dex_volumes`): sayfa açılışı
    HL'ye istek atmaz; ikisi de boşsa (ilk açılış, metrik/hacim turu henüz
    koşmadı) evren boştur ve sayfa bunu söyler.
    """
    names: dict[str, str] = {}
    for key, field in ((MAIN_CTX_KV, "c"), (MAIN_VOL_KV, "vols")):
        rec = await kv_get(key) or {}
        for name in (rec.get(field) or {}):
            if isinstance(name, str) and name and ":" not in name:
                names.setdefault(name.upper(), name)
    return names


async def resolve_coin(symbol_or_coin: str) -> dict | None:
    """Sembol → {coin, symbol, dex, kind} — hisse evreni ÖNCE, sonra ana dex kripto.

    Kripto coin `tickers`'a ASLA yazılmaz: `refresh_universe` orada olmayanı
    budar ve hisse hattının her sorgusu tickers ile JOIN'li — BTC satırı
    sessizce davranış değiştirirdi. Kripto kimliği yalnız bu dönüş değerinde
    yaşar. Sembol çakışmasında (aynı ad iki yerde) hisse kazanır.
    """
    t = await find_ticker(symbol_or_coin)
    if t:
        return {**t, "kind": "equity"}
    s = (symbol_or_coin or "").strip()
    if not s or ":" in s:
        return None
    from ..assets import is_excluded
    name = (await crypto_names()).get(s.upper())
    if not name or is_excluded(name):
        return None
    return {"coin": name, "symbol": name, "dex": "", "kind": "crypto"}


# ---------------- HIP-3 (builder) dex keşfi — yalnız tanı ve arama cevabı için ----------------
# İzleme listesi (EQUITY_DEXES) buradan GENİŞLEMEZ: her ek dex derin keşifte adres
# başına +1 istek demektir, o karar Ayarlar'da kullanıcının. Burası "ANSEM nerede?"
# sorusuna cevap verir: 1 perpDexs + dex başına 1 meta, 6 saatte bir, kv'de.
HIP3_KV = "hip3_dexes"
HIP3_TTL = 6 * 3600


def parse_perp_dexs(raw) -> list[dict]:
    """perpDexs yanıtı → [{name, full_name}] (ilk eleman ana dex için None gelir; atlanır)."""
    out = []
    for d in (raw or []) if isinstance(raw, list) else []:
        if not isinstance(d, dict):
            continue
        name = str(d.get("name") or "").strip()
        if not name or ":" in name:
            continue
        out.append({"name": name, "full_name": str(d.get("full_name") or d.get("fullName") or "")[:60]})
    return out


async def discover_dexes(client: HLClient, ttl: int = HIP3_TTL, fetch: bool = True) -> dict:
    """HL'deki tüm builder dex'leri ve coin sembolleri → kv `hip3_dexes`
    {ts, dexes: [{name, full_name, n, assets: [SEMBOL…]}]}. Hata olursa eski kv korunur;
    meta alınamayan dex `n: None` ile listelenir (yanlış 'yok' demeyelim)."""
    rec = await kv_get(HIP3_KV) or {}
    if not fetch or (rec.get("ts") and now() - int(rec["ts"]) < ttl):
        return rec
    try:
        raw = await client.perp_dexs()
    except Exception as e:
        log.warning("perpDexs alınamadı: %s", e)
        return rec
    dexes = []
    for d in parse_perp_dexs(raw):
        try:
            meta = await client.meta(d["name"])
            assets = sorted({symbol_of(norm_coin(a.get("name") or "", d["name"]))
                             for a in ((meta or {}).get("universe") or [])
                             if a.get("name") and not a.get("isDelisted")})
            dexes.append({**d, "n": len(assets), "assets": assets})
        except Exception as e:
            log.warning("meta(%s) alınamadı: %s", d["name"], e)
            dexes.append({**d, "n": None, "assets": []})
    if not dexes and rec.get("dexes"):
        return rec                                  # boş/bozuk yanıt: eski liste kalsın
    rec = {"ts": now(), "dexes": dexes}
    await kv_set(HIP3_KV, rec)
    log.info("HIP-3 dex keşfi: %s", ", ".join(f"{d['name']}({d['n']})" for d in dexes) or "yok")
    return rec


async def find_in_hip3(symbol: str, watched=None) -> dict | None:
    """Sembol hangi builder dex'inde: {dex, full_name, n, watched} — kv-only, ağ yok."""
    sym = symbol_of(symbol or "")
    if not sym:
        return None
    rec = await kv_get(HIP3_KV) or {}
    w = {str(x).strip() for x in (watched or []) if str(x).strip()}
    for d in rec.get("dexes") or []:
        if sym in (d.get("assets") or []):
            return {"dex": d["name"], "full_name": d.get("full_name") or "", "n": d.get("n"),
                    "watched": d["name"] in w}
    return None


async def hip3_known() -> bool:
    return bool((await kv_get(HIP3_KV) or {}).get("dexes"))


async def similar_names(symbol: str, limit: int = 5) -> list[str]:
    """Yakın adlar: sorguyu içeren (ya da sorgunun içerdiği) ana dex kripto adları ve
    izlenen hisse sembolleri — 'yazımı kontrol et' yerine somut öneri."""
    q = (symbol or "").strip().upper().split(":")[-1]
    if len(q) < 2:
        return []
    names = set((await crypto_names()).values())
    async with db() as conn:
        cur = await conn.execute("SELECT symbol FROM tickers")
        names |= {r["symbol"] for r in await cur.fetchall() if r["symbol"]}
    hits = [n for n in names if n.upper() != q and (q in n.upper() or (len(n) >= 3 and n.upper() in q))]
    return sorted(hits, key=lambda n: (len(n), n))[:limit]
