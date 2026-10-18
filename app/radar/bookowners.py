"""🧱 En kalın emirler ve sahipleri (coin sayfası / /defter).

Kullanıcı (18.10): "orderbookta en kalın emirlerin sahipleri … bulmak için bir aracımız yok? hem sitede hem
telegramda sorarak olmalı". Örnek: PUMP'ta 0.0041'de $9.29M alış "tek kademe".

Ölçülen gerçekler (canlı 18.10):
  • HL arayüzü kademeleri GRUPLAR: PUMP'ın "0.004100 · $9.29M" satırı 0.004100–0.004109 aralığındaki 160 ayrı emirdi
    (l2Book nSigFigs=3, n=160). Bu yüzden iki görünüm: tam fiyat (ilk 20 kademe) ve gruplu (arayüz gibi, 3 basamak).
  • Defter ANONİMDİR; HL'nin açık API'si emrin sahibini vermez. Düğümün işlem akışı (explorerTxs) adres taşır ama
    blokların ~%0,1'ini yayınlıyor (30 sn'de 449K işlemden 404'ü) → güvenilir kaynak değil, kullanılmadı.

Sahip iki yoldan, ikisi de ÖLÇÜM (tahmin yok):
  1. Pasif dolum (📼 tape): son 15 dk'da o fiyat kademesinde pasif emri dolan adres — dolan emir sahibini ele verir.
  2. Aday doğrulama: adaylar (o kademede/coinde pasif dolumu olanlar, bu coinde pozisyonu olanlar, son 15 dk'nın
     büyük alıcı/satıcıları) için `frontendOpenOrders` (ağırlık 20, düşük şerit) — kademeye denk gelen açık emri
     varsa "doğrulandı" ve tutarı. Aday dışındaki sahipler BULUNAMAZ: kademenin bulunamayan kısmı ayrıca yazılır.

Sayfa görünce arka planda kurulur (kick), CACHE_SEC önbellek; adres başına açık emirler ADDR_TTL önbellekte.
"""
from __future__ import annotations

import asyncio
import logging
import math

from ..db import db, now
from . import tape
from .report import coin_dex

log = logging.getLogger("radar.bookowners")

CACHE_SEC = 30               # aynı coin için sonuç bu kadar taze sayılır
SHOW_MAX_AGE = 600           # bundan eski sonuç sayfada gösterilmez
LEVELS = 4                   # yön başına en kalın kademe (tam fiyat ve gruplu ayrı)
GROUP_SIG = 3                # gruplu görünüm: HL arayüzünün 0.00001 adımı (PUMP) ≈ 3 anlamlı basamak
CAND_MAX = 10                # kurulum başına en çok sorulan aday
ADDR_TTL = 120               # adresin açık emirleri bu kadar önbellekte
ADDR_TIMEOUT = 5.0           # tek adres sorgusu en çok
BUILD_TIMEOUT = 12.0         # kurulumun tamamı en çok
BIG_ORDER_MIN = 25_000       # "bilinen büyük emirler" listesi tabanı ($)
MAKER_MIN = 5_000            # adaylığa giren pasif dolum ($, son 15 dk)

RESULTS: dict[str, tuple[int, dict]] = {}
INFLIGHT: dict[str, asyncio.Future] = {}
ADDR_ORDERS: dict[tuple[str, str], tuple[int, list]] = {}
STATS: dict = {"builds": 0, "asked": 0, "failed": 0, "errors": 0}


def _levels(book) -> tuple[list[dict], list[dict]]:
    lv = (book or {}).get("levels") if isinstance(book, dict) else None
    if not isinstance(lv, list) or len(lv) != 2:
        return [], []
    out = []
    for side in lv:
        rows = []
        for x in side or []:
            try:
                px, sz = float(x["px"]), float(x["sz"])
                rows.append({"px": px, "sz": sz, "usd": px * sz, "n": int(x.get("n") or 0)})
            except (KeyError, TypeError, ValueError):
                continue
        out.append(rows)
    return out[0], out[1]


def group_unit(px: float, sig: int = GROUP_SIG) -> float:
    """nSigFigs gruplamasında bir kovanın genişliği (ör. 0.0041 · 3 basamak → 0.00001)."""
    if px <= 0:
        return 0.0
    return 10 ** (math.floor(math.log10(px)) - (sig - 1))


def parse_orders(orders, coin: str) -> list[dict]:
    """frontendOpenOrders → bu coinin limit emirleri [{side 'bid'/'ask', px, sz, usd, oid, ro}] (tetikli/TP-SL hariç)."""
    out = []
    for o in orders or []:
        try:
            if (o.get("coin") or "") != coin:
                continue
            if o.get("isTrigger") or o.get("isPositionTpsl"):
                continue
            px = float(o.get("limitPx") or 0)
            sz = float(o.get("sz") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if px <= 0 or sz <= 0:
            continue
        out.append({"side": "bid" if o.get("side") == "B" else "ask", "px": px, "sz": sz, "usd": px * sz,
                    "oid": o.get("oid"), "ro": bool(o.get("reduceOnly")), "tif": o.get("tif") or ""})
    return out


async def _candidates(coin: str, summ: dict) -> list[str]:
    """Öncelik: (0) bu coinde pasif emri dolan (son 15 dk, ≥ MAKER_MIN) · (1) bu coinde pozisyonu olanlar (büyükten)
    · (2) son 15 dk'nın büyük net alıcı/satıcıları. Tekrarsız, en çok CAND_MAX."""
    seen: list[str] = []

    def add(a):
        a = (a or "").lower()
        if a and a not in seen:
            seen.append(a)
    mk: dict[str, float] = {}
    for (_sd, _px, addr), usd in (summ.get("makers") or {}).items():
        mk[addr] = mk.get(addr, 0.0) + usd
    for a, usd in sorted(mk.items(), key=lambda kv: -kv[1]):
        if usd >= MAKER_MIN and len(seen) < CAND_MAX // 2:
            add(a)
    try:
        async with db() as conn:
            if ":" in coin:
                cur = await conn.execute("SELECT address FROM positions_current WHERE coin=? ORDER BY notional DESC"
                                         " LIMIT ?", (coin, CAND_MAX))
            else:
                cur = await conn.execute("SELECT address FROM addr_positions WHERE coin=? AND closed_ts IS NULL"
                                         " ORDER BY notional DESC LIMIT ?", (coin, CAND_MAX))
            pos = [r["address"] for r in await cur.fetchall()]
    except Exception:                                   # noqa: BLE001 — tablo yoksa (test / ilk açılış)
        pos = []
    for a in pos[:4]:
        add(a)
    for f in (summ.get("buyers") or [])[:2] + (summ.get("sellers") or [])[:2]:
        add(f["addr"])
    for a in pos[4:]:
        add(a)
    return seen[:CAND_MAX]


async def _orders_of(client, addr: str, dex: str) -> list | None:
    hit = ADDR_ORDERS.get((addr, dex))
    if hit and now() - hit[0] < ADDR_TTL:
        return hit[1]
    p = {"type": "frontendOpenOrders", "user": addr}
    if dex:
        p["dex"] = dex
    STATS["asked"] += 1
    try:
        res = await asyncio.wait_for(client.info(p, retries=1, priority="low"), ADDR_TIMEOUT)
    except Exception as e:                              # noqa: BLE001
        STATS["failed"] += 1
        log.debug("açık emir %s: %s", addr[:10], e)
        return None
    if not isinstance(res, list):
        STATS["failed"] += 1
        return None
    ADDR_ORDERS[(addr, dex)] = (now(), res)
    if len(ADDR_ORDERS) > 3000:
        for k, _ in sorted(ADDR_ORDERS.items(), key=lambda kv: kv[1][0])[:500]:
            ADDR_ORDERS.pop(k, None)
    return res


def _attach(levels: list[dict], side: str, orders: list[dict], summ: dict, grouped: bool, unit: float) -> None:
    for lv in levels:
        if grouped:                                    # HL gruplaması: alış kovası aşağı, satış kovası yukarı yuvarlar
            lo, hi = (lv["px"], lv["px"] + unit * 0.999999) if side == "bid" else (lv["px"] - unit * 0.999999, lv["px"])
        else:
            lo, hi = lv["px"] * (1 - 1e-9), lv["px"] * (1 + 1e-9)
        lv["lo"], lv["hi"] = lo, hi
        own: dict[str, float] = {}
        for o in orders:
            if o["side"] == side and lo <= o["px"] <= hi:
                own[o["addr"]] = own.get(o["addr"], 0.0) + o["usd"]
        lv["owners"] = sorted(own.items(), key=lambda kv: -kv[1])
        known = sum(own.values())
        lv["known_usd"] = known
        lv["unknown_usd"] = max(0.0, lv["usd"] - known)
        lv["makers"] = tape.makers_near(summ, side, lo, hi)[:4]


async def build(client, coin: str) -> dict:
    """Bir coin için en kalın kademeler + sahipler (ölçüm). Hata yükseltmez; okunamayan parça 'err'de."""
    STATS["builds"] += 1
    out: dict = {"coin": coin, "ts": now(), "err": [], "raw": {"bid": [], "ask": []},
                 "grouped": {"bid": [], "ask": []}, "orders": [], "cands": 0, "asked": 0, "failed": 0}
    dex = coin_dex(coin)
    try:
        raw, grp = await asyncio.gather(client.l2_book(coin), client.l2_book(coin, GROUP_SIG),
                                        return_exceptions=True)
    except Exception as e:                              # noqa: BLE001
        raw = grp = e
    rb, ra = _levels(raw) if not isinstance(raw, BaseException) else ([], [])
    gb, ga = _levels(grp) if not isinstance(grp, BaseException) else ([], [])
    if not (rb or ra):
        out["err"].append("defter okunamadı")
    mid = (rb[0]["px"] + ra[0]["px"]) / 2 if rb and ra else None
    out["mid"] = mid
    unit = group_unit(mid or (gb[0]["px"] if gb else 0))
    out["unit"] = unit
    out["unit_txt"] = (f"{unit:.12f}".rstrip("0").rstrip(".") if 0 < unit < 1 else f"{unit:g}") if unit else ""
    pick = lambda xs: sorted(xs, key=lambda x: -x["usd"])[:LEVELS]   # noqa: E731
    out["raw"] = {"bid": pick(rb), "ask": pick(ra)}
    out["grouped"] = {"bid": pick(gb), "ask": pick(ga)}
    out["depth"] = {"raw_bid": sum(x["usd"] for x in rb), "raw_ask": sum(x["usd"] for x in ra),
                    "raw_bid_far": rb[-1]["px"] if rb else None, "raw_ask_far": ra[-1]["px"] if ra else None}
    summ = tape.summary(coin)
    cands = await _candidates(coin, summ)
    out["cands"] = len(cands)
    orders: list[dict] = []
    sem = asyncio.Semaphore(4)

    async def one(a):
        async with sem:
            res = await _orders_of(client, a, dex)
        if res is None:
            out["failed"] += 1
            return
        for o in parse_orders(res, coin):
            orders.append({**o, "addr": a})
    out["asked"] = len(cands)
    await asyncio.gather(*(one(a) for a in cands))
    for side in ("bid", "ask"):
        _attach(out["raw"][side], side, orders, summ, False, 0.0)
        _attach(out["grouped"][side], side, orders, summ, True, unit)
    if mid:
        for o in orders:
            o["dist_pct"] = (o["px"] - mid) / mid * 100
    out["orders"] = sorted((o for o in orders if o["usd"] >= BIG_ORDER_MIN), key=lambda o: -o["usd"])[:15]
    return out


async def _run(client, coin: str) -> None:
    try:
        res = await asyncio.wait_for(build(client, coin), BUILD_TIMEOUT)
        RESULTS[coin] = (now(), res)
        if len(RESULTS) > 200:
            for k, _ in sorted(RESULTS.items(), key=lambda kv: kv[1][0])[:50]:
                RESULTS.pop(k, None)
    except Exception:                                   # noqa: BLE001
        STATS["errors"] += 1
        log.debug("defter sahipleri %s", coin, exc_info=True)
    finally:
        INFLIGHT.pop(coin, None)


def kick(client, coin: str) -> bool:
    """Sonuç bayatsa arka planda kur (tek süren kurulum). Dönüş: kuruluyor mu."""
    if client is None or not coin:
        return False
    hit = RESULTS.get(coin)
    if hit and now() - hit[0] < CACHE_SEC:
        return False
    if coin in INFLIGHT and not INFLIGHT[coin].done():
        return True
    INFLIGHT[coin] = asyncio.ensure_future(_run(client, coin))
    return True


def cached(coin: str) -> dict | None:
    hit = RESULTS.get(coin)
    if not hit or now() - hit[0] > SHOW_MAX_AGE:
        return None
    return {**hit[1], "age": now() - hit[0]}


async def get(client, coin: str, wait: float = 0.0) -> dict | None:
    """Önbellekteki sonuç; bayatsa kurulum başlatılır, `wait` sn kadar beklenir (Telegram komutu)."""
    kick(client, coin)
    if wait > 0 and coin in INFLIGHT:
        try:
            await asyncio.wait_for(asyncio.shield(INFLIGHT[coin]), wait)
        except Exception:                               # noqa: BLE001
            pass
    return cached(coin)


def diag_line() -> str:
    s = STATS
    return (f"🧱 defter sahipleri: {s['builds']} kurulum · {s['asked']} adres soruldu"
            + (f" · ⚠️ {s['failed']} okunamadı" if s["failed"] else "")
            + (f" · ⚠️ {s['errors']} hata" if s["errors"] else "") + f" · önbellekte {len(RESULTS)} coin")
