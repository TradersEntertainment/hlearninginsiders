"""🎯 Liq doğrulama + keşif şeridi — /kriptoliq'te gösterilen her pozisyon kısa süre önce HL'den okunmuş olsun.

Kullanıcı (22.10, /kriptoliq ekranı): "sitedeki kripto liqlere bakıyorum hepsi eski basıyorum öyle bir poz yok veya liqi
çok farklı bence kripto büyük liqler yakındakileri düzgün tespit etmiyoruz" · "tüm pozlarda en yakın liq arama yeri çok
iyi çalışmalı ve poz boyutu filtreleyebilmeliyim 500k+ 1m$ vs gibi" · "hem güvenilir olacak hem de poz kaçırmayacak".

Ölçüm (22.10, canlı HL, scratchpad/kliq): ekrandaki 7 satırdan çözülebilen 6'sı bayattı — AZTEC 6 gün önce kapanmış,
HYPE 10 gün önce kapanmış, SOL 13–18.10'da likide olmuş, kPEPE / XRP / ZEC saatler–1,5 gün önce kapanmış, HYPE
$286K açık ama liq ~%9,4 kaymış (gerçek mesafe ~%9,5, sayfada %0,03). Kök neden: addr_positions satırı adres en son
okunduğundaki fotoğraf; sayfa HL'ye sormuyor ve yaş göstermiyordu; ana dex okuması sayıma (census) kalmıştı ve bu
adresler günlerce okunmamıştı. "Liq'e en yakın" sıralaması ölü satırları en üste taşıyor: fiyat eski liq'e dönünce
"%0,03" görünüyor. Gerçekten açık yakın-liq pozisyonlar sakin bir saatte 10 dk içinde neredeyse hiç değişmiyor (40'ın
0'ı kapandı; çapraz teminatta 60 dk'da %21'inin mesafesi >1 puan kaydı).

Kararlar:
  • DOĞRULAMA: liq'e ≤ %5 olan açık ana dex satırı (≥ sayfa tabanı) 10 dk'dan eskiyse, %5–10 olan 30 dk'dan eskiyse,
    liq'i fiyatın öbür yanına geçmiş (likide olmuş olabilir) satır hemen yeniden okunur — adres başına TEK istek
    (clearinghouseState, ana dex, ağırlık 2), sayımın yazıcısıyla (census.write_main: kapananı damgalar). Önce en yakın.
  • KEŞİF: son 15 dk'da ana dex kriptoda ≥ LIQ_DISCOVER_MIN_USD ($50K) işlem yapan ve o işlemden sonra okunmamış
    adres okunur — yeni açılan büyük pozisyon havuza dakikalar içinde girer (sayımın sıcak şeridi kuyrukta kalabiliyor).
  • Bütçe: LIQ_VERIFY_RPM (40/dk ≈ 80 ağırlık, HL sınırının ~%7'si); botun kotasında MIN_FREE'den az boş varsa tur
    atlanır (radarlar önce). Doğrulama önce, keşif kalan payla.
  • Sayfa (cryptoliq.page) doğrulama penceresinden eski satırı göstermez, "doğrulanıyor" diye sayar; her satırda en eski
    doğrulamanın yaşı yazar.

Kör nokta, açıkça: hiç işlem yapmamış ve sayımın görmediği bir adresin pozisyonu bulunamaz (HL'de "bütün pozisyonlar"
sorgusu yok); keşif yalnız WS'te dinlenen coinlerin işlemlerinden ($5K altı işlem fills'e yazılmaz).
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..db import db, kv_get, kv_set, now

log = logging.getLogger("radar.liqverify")

STATS_KV = "liqverify_stats"
TICK = 15
NEAR_PCT = 5.0
FAR_PCT = 10.0
NEAR_SEC = 600                 # ≤ %5: bundan eski satır yeniden okunur (sayfa 15 dk'ya kadar gösterir)
FAR_SEC = 1800                 # %5–10: bundan eski satır yeniden okunur (sayfa 45 dk'ya kadar gösterir)
CROSSED_SEC = 60               # liq fiyatın öbür yanında: bu kadar eskiyse hemen
PAGE_NEAR_SEC = 900
PAGE_FAR_SEC = 2700
DISCOVER_WINDOW = 900
DISCOVER_REREAD_SEC = 600      # keşifte aynı adres en erken bu kadar sonra yeniden (sürekli işlem yapan MM/HLP payı yemesin)
RETRY_SEC = 60                 # düşen okuma bu kadar sonra yeniden aday
MIN_FREE = 30
READ_TIMEOUT = 8.0
LAST_READ: dict[str, float] = {}       # son BAŞARILI okuma
RETRY_AT: dict[str, float] = {}        # düşen okumanın yeniden aday olacağı an (LAST_READ'e dokunmaz)
STATS: dict = {"reads": 0, "verify": 0, "discover": 0, "closed_seen": 0, "errors": 0, "skipped_busy": 0,
               "due": 0, "disc_due": 0, "last": 0}


def _cfg(cfg, name, default):
    v = getattr(cfg, name, default)
    return default if v is None else v


def fresh_sec(dist: float | None) -> int:
    """Sayfa için: bu mesafedeki satır en çok kaç sn önce doğrulanmış olmalı."""
    return PAGE_NEAR_SEC if dist is None or dist <= NEAR_PCT else PAGE_FAR_SEC


def _crossed(side: str, liq: float, mark: float) -> bool:
    return (side == "long" and liq >= mark) or (side == "short" and liq <= mark)


async def due_verify(cfg, marks: dict, t: int) -> list[str]:
    """Yeniden okunması gereken adresler, önce en yakın (liq'i geçilmiş = 0)."""
    floor = min(float(_cfg(cfg, "crypto_liq_show_usd", 200_000) or 0) or 200_000,
                float(_cfg(cfg, "crypto_liq_min_usd", 500_000) or 500_000))
    async with db() as conn:
        cur = await conn.execute(
            "SELECT coin, address, side, liq_px, ts FROM addr_positions WHERE closed_ts IS NULL AND liq_px > 0"
            " AND instr(coin, ':') = 0 AND notional >= ?", (floor,))
        rows = [dict(r) for r in await cur.fetchall()]
    best: dict[str, float] = {}
    for r in rows:
        mark = marks.get(r["coin"])
        if not mark:
            continue
        liq, age = float(r["liq_px"]), t - int(r["ts"] or 0)
        if _crossed(r["side"], liq, float(mark)):
            key, need = 0.0, CROSSED_SEC
        else:
            d = abs(float(mark) - liq) / float(mark) * 100
            if d > FAR_PCT:
                continue
            key, need = d, (NEAR_SEC if d <= NEAR_PCT else FAR_SEC)
        if age < need or t - LAST_READ.get(r["address"], 0) < need or RETRY_AT.get(r["address"], 0) > t:
            continue
        best[r["address"]] = min(best.get(r["address"], 1e9), key)
    return sorted(best, key=lambda a: best[a])


async def due_discover(cfg, t: int) -> list[str]:
    """Son DISCOVER_WINDOW'da ana dex kriptoda büyük işlem yapıp o işlemden sonra okunmamış adresler.

    Sıra: hiç okunmamış önce, sonra en büyük TEK işleme göre (toplam değil: sürekli pasif hacim tek büyük yeni işlemi
    geçmesin). Aynı adres DISCOVER_REREAD_SEC'ten önce yeniden okunmaz; mm / vault etiketliler keşfe girmez
    (inceleme 22.10: sürekli işlem yapan MM'ler her tur ilk sıraya dönüp yeni balinayı aç bırakıyordu)."""
    floor = float(_cfg(cfg, "liq_discover_min_usd", 50_000) or 50_000)
    async with db() as conn:
        cur = await conn.execute(
            "SELECT f.address, MAX(f.ts) last, MAX(f.notional) big FROM fills f"
            " LEFT JOIN addresses a ON a.address = f.address"
            " WHERE f.ts >= ? AND f.notional >= ? AND instr(f.coin, ':') = 0 AND substr(f.coin, 1, 1) != '@'"
            " AND COALESCE(a.entity, '') NOT IN ('mm', 'vault') GROUP BY f.address", (t - DISCOVER_WINDOW, floor))
        rows = [dict(r) for r in await cur.fetchall()]
    cand = [r for r in rows
            if LAST_READ.get(r["address"], 0) < int(r["last"] or 0)
            and t - LAST_READ.get(r["address"], 0) >= DISCOVER_REREAD_SEC
            and RETRY_AT.get(r["address"], 0) <= t]
    cand.sort(key=lambda r: (LAST_READ.get(r["address"], 0) > 0, -float(r["big"] or 0)))
    return [r["address"] for r in cand]


async def read(cfg, client, addr: str) -> bool:
    """Ana dex defterini oku ve yaz (sayımın yazıcısı: açık pozisyonlar güncellenir, tutulmayanlar kapandı damgası)."""
    from . import census
    t = time.time()
    try:
        resp = await asyncio.wait_for(client.clearinghouse(addr, "", priority="low", retries=1), READ_TIMEOUT)
        if not isinstance(resp, dict) or "assetPositions" not in resp:
            raise ValueError("bozuk yanıt")             # bozuk yanıt kayıt kapatmaz (sayımla aynı kural)
        async with db() as conn:
            cur = await conn.execute("SELECT coin FROM addr_positions WHERE address=? AND closed_ts IS NULL"
                                     " AND instr(coin, ':') = 0", (addr,))
            before = {r["coin"] for r in await cur.fetchall()}
        await census.write_main(cfg, addr, resp, now(), None)
        # kapanan = önce açık olup yanıtta olmayan (yeni açılanla sayıca birbirini götürmesin)
        STATS["closed_seen"] += len(before - set(census._sw._parse_all_positions(resp, 0)))
    except Exception:                                       # noqa: BLE001
        STATS["errors"] += 1
        log.debug("liq doğrulama okuması %s", addr, exc_info=True)
        RETRY_AT[addr] = t + RETRY_SEC                      # düşen okuma: 1 dk sonra yeniden aday (eşikten bağımsız)
        return False
    LAST_READ[addr] = t
    RETRY_AT.pop(addr, None)
    STATS["reads"] += 1
    return True


def _busy(client) -> bool:
    """Düşük şerit şu an istek geçirmez mi — şeridin kendi kapısıyla aynı kural (429 sonrası susma, pencere
    LOW_SHARE'i doldurdu) ya da ortak kotada boş yer az. Öyleyse okumayı 8 sn bekletip hata saymak yerine tur atlanır."""
    try:
        u = client.usage()
        if float(u.get("low_paused") or 0) > 0:
            return True
        if "rpm" in u and "cap" in u and int(u["rpm"]) >= float(u["cap"]) * float(u.get("low_share") or 0.7) - 2:
            return True
        return int(u["free"]) < MIN_FREE
    except Exception:                                       # noqa: BLE001
        return False


async def tick(cfg, client, marks: dict, t: int | None = None) -> dict:
    """Bir tur: payı doğrulamaya (önce) ve keşfe böl. Dönüş: {'verify': [...], 'discover': [...]}."""
    t = int(t or now())
    per = max(1, int(float(_cfg(cfg, "liq_verify_rpm", 40) or 40) * TICK / 60))
    v = await due_verify(cfg, marks, t)
    dsc = [a for a in await due_discover(cfg, t) if a not in v]
    STATS["due"], STATS["disc_due"] = len(v), len(dsc)
    done = {"verify": [], "discover": []}
    for kind, lst in (("verify", v), ("discover", dsc)):
        for a in lst:
            if len(done["verify"]) + len(done["discover"]) >= per:
                break
            if _busy(client):
                STATS["skipped_busy"] += 1
                return done
            if await read(cfg, client, a):
                done[kind].append(a)
                STATS[kind] += 1
    STATS["last"] = t
    for a in [a for a, at in RETRY_AT.items() if at <= t - 3600]:
        RETRY_AT.pop(a, None)
    if len(LAST_READ) > 50_000:
        for a in sorted(LAST_READ, key=LAST_READ.get)[:10_000]:
            LAST_READ.pop(a, None)
    return done


async def _marks(cfg, client) -> dict:
    from ..hl.universe import MAIN_CTX_KV, main_dex_ctx
    try:
        rec = await main_dex_ctx(client, ttl=150)
    except Exception:                                       # noqa: BLE001
        rec = await kv_get(MAIN_CTX_KV) or {}
    return {c: v["m"] for c, v in ((rec or {}).get("c") or {}).items() if (v or {}).get("m")}


async def loop(cfg, client) -> None:
    from .. import health
    await asyncio.sleep(60)
    while True:
        try:
            await health.beat("liqverify")
            if getattr(cfg, "liq_verify_enabled", True) and client is not None:
                await tick(cfg, client, await _marks(cfg, client))
            await kv_set(STATS_KV, {**STATS, "ts": now()})
        except asyncio.CancelledError:
            raise
        except Exception:                                   # noqa: BLE001
            STATS["errors"] += 1
            log.exception("liq doğrulama turu")
        await asyncio.sleep(TICK)


async def diag_line(cfg) -> str:
    if not getattr(cfg, "liq_verify_enabled", True):
        return "🎯 liq doğrulama: KAPALI (LIQ_VERIFY_ENABLED=0) — /kriptoliq satırları doğrulanmadan, yaşıyla gösterir"
    s = await kv_get(STATS_KV) or {}
    if not s:
        return "🎯 liq doğrulama: henüz tur yok"
    return (f"🎯 liq doğrulama: okunan {s.get('reads', 0)} (doğrulama {s.get('verify', 0)} · keşif {s.get('discover', 0)})"
            f" · kapanmış bulunan {s.get('closed_seen', 0)} · bekleyen {s.get('due', 0)} + keşif {s.get('disc_due', 0)}"
            + (f" · ⚠️ kota dolu atlanan {s['skipped_busy']}" if s.get("skipped_busy") else "")
            + (f" · ⚠️ hata {s['errors']}" if s.get("errors") else ""))
