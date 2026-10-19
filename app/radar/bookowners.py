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

İnceleme düzeltmeleri (18.10, yeniden üretildi):
  • Süre: defter ≤ BOOK_TIMEOUT (tek deneme), sahip sorguları kendi süresiyle (OWNERS_BUDGET'e kadar) — bitmeyen aday
    "okunamadı" sayılır, okunmuş defter ASLA atılmaz; /defter'in 10 sn beklemesi içinde biter. Düşük şerit 429
    sonrası duraklıyken adaylar hiç sorulmaz ("ertelendi"). Kurulum yine de düşerse hata sonucu yazılır: sayfa
    "hazırlanıyor" döngüsünde kalmaz.
  • Tazelik: emir önbelleği 60 sn; mesaj/sayfa kullanılan emir verisinin yaşını yazar; sahip tutarı kademeyi aşamaz.
  • Adaylar önce EN KALIN KADEMELERDE pasif dolumu görülenler; boş yerler kalan dolumcularla dolar.
  • HL `frontendOpenOrders` bazı hesaplarda (HLP alt hesapları: 3735 emirden 100'ü) TAM 100'de keser; 100 gelirse
    sınırsız `openOrders` ile tamamlanır (acctwatch.FRONT_CAP ile aynı yol). O da okunamazsa liste "kesik" sayılır,
    "emri yok" denmez (`partial`). Önbellek ham yanıtı değil yalnız emirlerin kısa kaydını tutar.
  • 2. inceleme (18.10, canlı): `openOrders` tetikli emri (Stop / TP Market) BAYRAKSIZ verir. Tamamlamadan gelen ve
    görünen kademeye düşen / büyük emir `orderStatus` (ağırlık 2) ile doğrulanır: açık ve tetiksiz değilse sahip
    sayılmaz; doğrulanamayan (HL eski emre "unknownOid" der) da sayılmaz (`unverified`), o adreste "emri yok" denmez.
  • Açık emirler hiç sorulamadıysa (HL hız sınırı / hepsi okunamadı) kademe "sahip bilinmiyor" der, "bulunamadı"
    değil; ertelenen sonuç kısa önbellekte (DEFER_CACHE) kalır.
  • Bilinen/bulunamayan kısım ADETLE (kova fiyatı × Σadet): gruplu satış kovasında hayalet "bulunamayan" çıkmaz.
  • "Dolan" adresin açık emirleri okunup o kademede emri yoksa ayrı yazılır ("dolan, şu an emri yok") — sahip sanılmaz.
  • Kapsam: l2Book her yönde 20 kademe verir; sonuç kapsadığı aralığı (fiyata %) taşır, dışı görünmez.
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
ADDR_TTL = 60                # adresin açık emirleri bu kadar önbellekte (yaşı mesajda yazar)
ADDR_TIMEOUT = 5.0           # tek adres sorgusu en çok
BOOK_TIMEOUT = 4.0           # l2Book (tek deneme) en çok
OWNERS_BUDGET = 7.0          # kurulumun başından sahip sorgularının bitmesi gereken an (sn) — /defter 10 sn bekler
VERIFY_BUDGET = 8.5          # … tamamlamadan gelen emirlerin orderStatus doğrulaması bitmesi gereken an (sn)
VERIFY_MAX = 8               # kurulum başına en çok doğrulanan emir
DEFER_CACHE = 10             # HL hız sınırı yüzünden sahip sorulmayan sonuç bu kadar taze sayılır
BUILD_TIMEOUT = 12.0         # kurulumun tamamı en çok (yedek emniyet)
OPEN_ORDERS_CAP = 100        # HL frontendOpenOrders bazı hesaplarda bu sayıda keser → openOrders ile tamamla
BIG_ORDER_MIN = 25_000       # "bilinen büyük emirler" listesi tabanı ($)
MAKER_MIN = 5_000            # adaylığa giren pasif dolum ($, son 15 dk)

RESULTS: dict[str, tuple[int, dict]] = {}
INFLIGHT: dict[str, asyncio.Future] = {}
ADDR_ORDERS: dict[tuple[str, str], tuple[int, list, bool]] = {}   # (adres, dex) → (an, kısa emirler, kesik mi)
OID_OK: dict[tuple[str, int], tuple[int, bool]] = {}               # (adres, oid) → (an, defterde bekleyen limit mi)
STATS: dict = {"builds": 0, "asked": 0, "full": 0, "verified": 0, "failed": 0, "errors": 0, "timeouts": 0,
               "deferred": 0, "partial": 0, "unverified": 0}


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


def _compact(orders, ext: bool = False) -> list[tuple]:
    """Açık emir yanıtı → limit emirlerin kısa kaydı (coin, yön, px, adet, oid, reduce-only, ext). Tetikli / TP-SL
    emir (defterde değil) atlanır. ext=True: `openOrders` tamamlaması — tetik bayrağı yok, doğrulanmadan sayılmaz."""
    out = []
    for o in orders or []:
        try:
            if o.get("isTrigger") or o.get("isPositionTpsl"):
                continue
            px = float(o.get("limitPx") or 0)
            sz = float(o.get("sz") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if px > 0 and sz > 0:
            out.append((str(o.get("coin") or ""), "bid" if o.get("side") == "B" else "ask", px, sz, o.get("oid"),
                        bool(o.get("reduceOnly")), ext))
    return out


def _for_coin(compact: list[tuple], coin: str) -> list[dict]:
    return [{"side": sd, "px": px, "sz": sz, "usd": px * sz, "oid": oid, "ro": ro, "ext": ext}
            for c, sd, px, sz, oid, ro, ext in compact if c == coin]


def parse_orders(orders, coin: str) -> list[dict]:
    """frontendOpenOrders → bu coinin limit emirleri [{side 'bid'/'ask', px, sz, usd, oid, ro}] (tetikli/TP-SL hariç)."""
    return _for_coin(_compact(orders), coin)


def _range(lv: dict, side: str, grouped: bool, unit: float) -> tuple[float, float]:
    """Kademenin kapsadığı fiyat aralığı. HL gruplaması: alış kovası aşağı, satış kovası yukarı yuvarlar
    (alış [px, px+birim), satış (px−birim, px]); tam fiyat yalnız kendisi."""
    if grouped:
        return (lv["px"], lv["px"] + unit * 0.999999) if side == "bid" else (lv["px"] - unit * 0.999999, lv["px"])
    return lv["px"] * (1 - 1e-9), lv["px"] * (1 + 1e-9)


async def _candidates(coin: str, summ: dict, ranges: list[tuple[str, float, float]] = ()) -> list[str]:
    """Öncelik: (0) EN KALIN KADEMELERDE son 15 dk pasif emri dolanlar (büyükten) · (1) bu coinde ≥ MAKER_MIN pasif
    dolumu olanlar · (2) bu coinde pozisyonu olanlar (büyükten) · (3) son 15 dk'nın büyük net alıcı/satıcıları ·
    (4) kalan pozisyon sahipleri ve dolumcular boş yerlere. Tekrarsız, en çok CAND_MAX."""
    seen: list[str] = []

    def add(a):
        a = (a or "").lower()
        if a and a not in seen and len(seen) < CAND_MAX:
            seen.append(a)
    near: dict[str, float] = {}
    for side, lo, hi in ranges:
        for a, usd in tape.makers_near(summ, side, lo, hi):
            near[a] = near.get(a, 0.0) + usd
    near_sorted = [a for a, _ in sorted(near.items(), key=lambda kv: -kv[1])]
    mk: dict[str, float] = {}
    for (_sd, _px, addr), usd in (summ.get("makers") or {}).items():
        mk[addr] = mk.get(addr, 0.0) + usd
    mk_sorted = [a for a, usd in sorted(mk.items(), key=lambda kv: -kv[1]) if usd >= MAKER_MIN]
    for a in near_sorted:
        if len(seen) >= CAND_MAX // 2:
            break
        add(a)
    for a in mk_sorted:
        if len(seen) >= CAND_MAX // 2:
            break
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
    for a in pos[4:] + near_sorted + mk_sorted:
        add(a)
    return seen


def _prune(t: int) -> None:
    for k in [k for k, v in ADDR_ORDERS.items() if t - v[0] >= ADDR_TTL]:
        ADDR_ORDERS.pop(k, None)
    for k in [k for k, v in OID_OK.items() if t - v[0] >= ADDR_TTL]:
        OID_OK.pop(k, None)
    if len(ADDR_ORDERS) > 3000:
        for k, _ in sorted(ADDR_ORDERS.items(), key=lambda kv: kv[1][0])[:500]:
            ADDR_ORDERS.pop(k, None)


async def _ask(client, typ: str, addr: str, dex: str):
    p = {"type": typ, "user": addr}
    if dex:
        p["dex"] = dex
    STATS["asked" if typ == "frontendOpenOrders" else "full"] += 1
    return await asyncio.wait_for(client.info(p, retries=1, priority="low"), ADDR_TIMEOUT)


async def _book_order(client, addr: str, oid) -> bool | None:
    """orderStatus: emir defterde bekleyen tetiksiz limit emir mi (True/False); bilinemezse None. Önbellekli.
    3. inceleme (18.10, canlı): HL orderStatus yalnız YAKIN geçmişi bilir — saatlerdir defterde bekleyen emre de
    "unknownOid" der (HLP: 590 dk'lık TRX emri hâlâ openOrders'ta). unknownOid "emir yok" DEĞİL → None
    (doğrulanamadı; önbelleğe yazılmaz). Yalnız status=order cevabı karar verir."""
    hit = OID_OK.get((addr, oid))
    if hit and now() - hit[0] < ADDR_TTL:
        return hit[1]
    try:
        res = await asyncio.wait_for(client.info({"type": "orderStatus", "user": addr, "oid": oid}, retries=1,
                                                 priority="low"), ADDR_TIMEOUT)
    except Exception as e:                              # noqa: BLE001
        log.debug("emir durumu %s/%s: %s", addr[:10], oid, e)
        return None
    if not isinstance(res, dict) or res.get("status") != "order":
        return None                                     # unknownOid: HL bu emri artık hatırlamıyor — bilinmiyor
    STATS["verified"] += 1
    od = res.get("order") or {}
    o = od.get("order") or {}
    ok = bool(od.get("status") == "open" and not o.get("isTrigger") and not o.get("isPositionTpsl"))
    OID_OK[(addr, oid)] = (now(), ok)
    return ok


async def _orders_of(client, addr: str, dex: str) -> tuple[int, list, bool] | None:
    """(soru anı, kısa emirler, kesik mi) ya da None (okunamadı). Okunamayan önbelleğe girmez. frontendOpenOrders
    ≥ OPEN_ORDERS_CAP döndürdüyse openOrders (sınırsız, yalnız defterde bekleyenler) ile tamamlanır."""
    hit = ADDR_ORDERS.get((addr, dex))
    if hit and now() - hit[0] < ADDR_TTL:
        return hit
    try:
        res = await _ask(client, "frontendOpenOrders", addr, dex)
    except Exception as e:                              # noqa: BLE001
        STATS["failed"] += 1
        log.debug("açık emir %s: %s", addr[:10], e)
        return None
    if not isinstance(res, list):
        STATS["failed"] += 1
        return None
    compact, cut = _compact(res), False
    if len(res) == OPEN_ORDERS_CAP:                     # HL tam 100'de keser; 100'den çok gelen liste tamdır
        try:
            full = await _ask(client, "openOrders", addr, dex)
        except Exception as e:                          # noqa: BLE001
            log.debug("açık emir (tam) %s: %s", addr[:10], e)
            full = None
        if isinstance(full, list):
            seen = {o[4] for o in compact}
            trig = {o.get("oid") for o in res if isinstance(o, dict) and (o.get("isTrigger") or o.get("isPositionTpsl"))}
            compact += [o for o in _compact(full, ext=True) if o[4] not in seen and o[4] not in trig]
        else:
            cut = True
            STATS["partial"] += 1
    t = now()
    _prune(t)
    ADDR_ORDERS[(addr, dex)] = (t, compact, cut)
    return t, compact, cut


def _attach(levels: list[dict], side: str, orders: list[dict], summ: dict, grouped: bool, unit: float,
            read_ok: set | frozenset = frozenset()) -> None:
    """Kademeye sahip ekle. Bilinen kısım ADETLE (kova fiyatı × Σadet; gruplu satış kovasında emir fiyatı kova
    fiyatından düşüktür). Sahip tutarı kademeyi aşmaz. Açık emirleri okunmuş ve bu aralıkta emri olmayan 'dolan'
    adres `makers_gone`a (sahip sanılmasın)."""
    for lv in levels:
        lo, hi = _range(lv, side, grouped, unit)
        lv["lo"], lv["hi"] = lo, hi
        own: dict[str, float] = {}
        own_sz = 0.0
        for o in orders:
            if o["side"] == side and lo <= o["px"] <= hi:
                own[o["addr"]] = own.get(o["addr"], 0.0) + o["usd"]
                own_sz += o["sz"]
        lv["owners"] = sorted(((a, min(u, lv["usd"])) for a, u in own.items()), key=lambda kv: -kv[1])
        known_sz = min(own_sz, lv["sz"])
        lv["known_usd"] = known_sz * lv["px"]
        lv["unknown_usd"] = max(0.0, lv["sz"] - known_sz) * lv["px"]
        mk = tape.makers_near(summ, side, lo, hi)
        lv["makers"] = [(a, u) for a, u in mk if a in own or a not in read_ok][:4]
        lv["makers_gone"] = [(a, u) for a, u in mk if a not in own and a in read_ok][:4]


def _reach(levels: list[dict], mid: float | None) -> float | None:
    """Kademe listesinin fiyata en uzak noktası (% fiyata) — l2Book her yönde 20 kademe verir, dışı görünmez."""
    if not levels or not mid:
        return None
    return (levels[-1]["px"] - mid) / mid * 100


async def _book(client, coin: str, sig: int | None):
    # "user" şeridi: sayfadaki / /defter'deki kişi bekliyor; arka plan yükü defteri dondurmasın (kullanıcı 19.10)
    return await asyncio.wait_for(client.l2_book(coin, sig, retries=1, priority="user"), BOOK_TIMEOUT)


def _empty(coin: str, err: list[str]) -> dict:
    return {"coin": coin, "ts": now(), "err": err, "raw": {"bid": [], "ask": []}, "grouped": {"bid": [], "ask": []},
            "orders": [], "cands": 0, "asked": 0, "failed": 0, "partial": 0, "unverified": 0, "mid": None, "unit": 0.0,
            "unit_txt": "", "depth": {}, "orders_ts": None, "deferred": False, "owners_unknown": False, "why": ""}


async def build(client, coin: str) -> dict:
    """Bir coin için en kalın kademeler + sahipler (ölçüm). Hata yükseltmez; okunamayan parça 'err'de.
    Defter okunduysa sonuç her durumda kademeleri taşır; sahip sorguları OWNERS_BUDGET'te kesilir."""
    t0 = asyncio.get_running_loop().time()
    STATS["builds"] += 1
    out = _empty(coin, [])
    dex = coin_dex(coin)
    raw, grp = await asyncio.gather(_book(client, coin, None), _book(client, coin, GROUP_SIG), return_exceptions=True)
    rb, ra = _levels(raw) if not isinstance(raw, BaseException) else ([], [])
    gb, ga = _levels(grp) if not isinstance(grp, BaseException) else ([], [])
    if not (rb or ra):
        out["err"].append("tam fiyat defteri okunamadı" if (gb or ga) else "defter okunamadı")
    elif not (gb or ga):
        out["err"].append("gruplu defter okunamadı")
    # neden: sayfa "Defter okunamadı (defter okunamadı)" diye kendini tekrar ediyordu (kullanıcı 19.10 ekranı)
    bad = next((x for x in (raw, grp) if isinstance(x, BaseException)), None)
    if out["err"] and bad is not None:
        from .livefeed import fail_reason
        out["why"] = fail_reason(bad, BOOK_TIMEOUT)
    mid = (rb[0]["px"] + ra[0]["px"]) / 2 if rb and ra else None
    out["mid"] = mid
    unit = group_unit(mid or (gb[0]["px"] if gb else 0))
    out["unit"] = unit
    out["unit_txt"] = (f"{unit:.12f}".rstrip("0").rstrip(".") if 0 < unit < 1 else f"{unit:g}") if unit else ""
    pick = lambda xs: sorted(xs, key=lambda x: -x["usd"])[:LEVELS]   # noqa: E731
    out["raw"] = {"bid": pick(rb), "ask": pick(ra)}
    out["grouped"] = {"bid": pick(gb), "ask": pick(ga)}
    out["depth"] = {"raw_bid": sum(x["usd"] for x in rb), "raw_ask": sum(x["usd"] for x in ra),
                    "raw_bid_far": rb[-1]["px"] if rb else None, "raw_ask_far": ra[-1]["px"] if ra else None,
                    "reach": {"raw": (_reach(rb, mid), _reach(ra, mid)), "grouped": (_reach(gb, mid), _reach(ga, mid))}}
    if not (rb or ra or gb or ga):
        return out                                      # defter yok: aday sormanın anlamı yok
    summ = tape.summary(coin)
    ranges = [(side, *_range(lv, side, view == "grouped", unit))
              for view in ("grouped", "raw") for side in ("bid", "ask") for lv in out[view][side]]
    orders: list[dict] = []
    read_ok: set[str] = set()
    paused = float(getattr(client, "low_paused", lambda: 0.0)() or 0)
    if paused > 0:
        STATS["deferred"] += 1
        out["deferred"] = True
        out["err"].append("açık emirler sorulmadı (HL hız sınırı, birazdan yeniden)")
        cands = []
    else:
        cands = await _candidates(coin, summ, ranges)
    out["cands"] = out["asked"] = len(cands)
    stamps: list[int] = []
    sem = asyncio.Semaphore(4)

    async def one(a):
        async with sem:
            res = await _orders_of(client, a, dex)
        if res is None:
            out["failed"] += 1
            return
        ts, lst, partial = res
        stamps.append(ts)
        if partial:
            out["partial"] += 1
        else:
            read_ok.add(a)                              # tam liste: bu kademede emri yoksa gerçekten yok
        for o in _for_coin(lst, coin):
            orders.append({**o, "addr": a})
    if cands:
        tasks = [asyncio.ensure_future(one(a)) for a in cands]
        left = max(0.5, OWNERS_BUDGET - (asyncio.get_running_loop().time() - t0))
        _done, pend = await asyncio.wait(tasks, timeout=left)
        for tk in pend:
            tk.cancel()
        if pend:
            out["failed"] += len(pend)
            STATS["timeouts"] += len(pend)
            await asyncio.gather(*pend, return_exceptions=True)
    # openOrders tamamlamasından gelen emir (tetik bayrağı yok): görünen kademeye düşen ya da büyük olan orderStatus ile
    # doğrulanır; doğrulanamayan sahip sayılmaz ve o adreste "şu an emri yok" denmez.
    ext = [o for o in orders if o.get("ext")]
    if ext:
        need = [o for o in ext if o["usd"] >= BIG_ORDER_MIN
                or any(sd == o["side"] and lo <= o["px"] <= hi for sd, lo, hi in ranges)]
        need.sort(key=lambda o: -o["usd"])
        chk, rest = need[:VERIFY_MAX], need[VERIFY_MAX:]
        verdict: dict[int, bool | None] = {}

        async def ver(o):
            async with sem:
                verdict[id(o)] = await _book_order(client, o["addr"], o["oid"])
        if chk:
            vt = [asyncio.ensure_future(ver(o)) for o in chk]
            left = max(0.3, VERIFY_BUDGET - (asyncio.get_running_loop().time() - t0))
            _done, pend = await asyncio.wait(vt, timeout=left)
            for tk in pend:
                tk.cancel()
            if pend:
                await asyncio.gather(*pend, return_exceptions=True)
        bad = [o for o in chk if verdict.get(id(o)) is None] + rest
        out["unverified"] = len(bad)
        STATS["unverified"] += len(bad)
        for o in bad:
            read_ok.discard(o["addr"])
        keep = {id(o) for o in chk if verdict.get(id(o))}
        orders = [o for o in orders if not o.get("ext") or id(o) in keep]
    out["owners_unknown"] = bool(out.get("deferred") or (cands and out["failed"] >= len(cands)))
    out["orders_ts"] = min(stamps) if stamps else None
    for side in ("bid", "ask"):
        _attach(out["raw"][side], side, orders, summ, False, 0.0, read_ok)
        _attach(out["grouped"][side], side, orders, summ, True, unit, read_ok)
    if mid:
        for o in orders:
            o["dist_pct"] = (o["px"] - mid) / mid * 100
    out["orders"] = sorted((o for o in orders if o["usd"] >= BIG_ORDER_MIN), key=lambda o: -o["usd"])[:15]
    return out


def _store(coin: str, res: dict) -> None:
    RESULTS[coin] = (now(), res)
    if len(RESULTS) > 200:
        for k, _ in sorted(RESULTS.items(), key=lambda kv: kv[1][0])[:50]:
            RESULTS.pop(k, None)


async def _run(client, coin: str) -> None:
    try:
        res = await asyncio.wait_for(build(client, coin), BUILD_TIMEOUT)
        _store(coin, res)
    except Exception as e:                              # noqa: BLE001
        STATS["errors"] += 1
        log.debug("defter sahipleri %s", coin, exc_info=True)
        # hata da sonuçtur: sayfa "hazırlanıyor" döngüsünde kalmasın, CACHE_SEC sonra yeniden denenir
        _store(coin, _empty(coin, ["süre aştı" if isinstance(e, asyncio.TimeoutError) else "kurulamadı"]))
    finally:
        INFLIGHT.pop(coin, None)


def kick(client, coin: str) -> bool:
    """Sonuç bayatsa arka planda kur (tek süren kurulum). Dönüş: kuruluyor mu."""
    if client is None or not coin:
        return False
    hit = RESULTS.get(coin)
    if hit and now() - hit[0] < (DEFER_CACHE if hit[1].get("deferred") else CACHE_SEC):
        return False
    if coin in INFLIGHT and not INFLIGHT[coin].done():
        return True
    INFLIGHT[coin] = asyncio.ensure_future(_run(client, coin))
    return True


def err_text(res: dict | None) -> str:
    """"Defter okunamadı (…)" parantezinin içi: neden + diğer eksikler. Yalnız "defter okunamadı" varsa ve neden
    bilinmiyorsa boş — sayfa "Defter okunamadı (defter okunamadı)" diye tekrar etmesin."""
    res = res or {}
    parts = [e for e in (res.get("err") or []) if e != "defter okunamadı"]
    if res.get("why"):
        parts.append(res["why"])
    return ", ".join(parts)


def cached(coin: str) -> dict | None:
    hit = RESULTS.get(coin)
    if not hit or now() - hit[0] > SHOW_MAX_AGE:
        return None
    return {**hit[1], "age": now() - hit[0], "err_txt": err_text(hit[1])}


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
            + (f" · {s['timeouts']} süre aştı" if s["timeouts"] else "")
            + (f" · {s['deferred']} kez HL hız sınırı yüzünden ertelendi" if s["deferred"] else "")
            + (f" · {s['full']} tam liste (openOrders) · {s['verified']} emir doğrulandı" if s["full"] else "")
            + (f" · {s['unverified']} emir doğrulanamadı" if s["unverified"] else "")
            + (f" · {s['partial']} liste kesik kaldı (tamamlanamadı)" if s["partial"] else "")
            + (f" · ⚠️ {s['errors']} hata" if s["errors"] else "") + f" · önbellekte {len(RESULTS)} coin")
