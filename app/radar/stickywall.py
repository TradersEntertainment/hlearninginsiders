"""🧲 Yapışkan duvar — defterin tepesine yapışan dev TEK emir.

Kullanıcı isteği (02.10, SAND ekranı): "büyük bir emir sürekli oralarda bekliyor,
market buy ile alınması için… aşağı da gidebiliyor yukarı da. 1M$'dan büyükse ve
hacme oranla yüksekse bildirim istiyorum."

NE OLDUĞU — canlı veriyle kanıtlandı (README "🧲 Yapışkan duvar"):
  • Likidasyon DEĞİL. HL likidasyonu piyasa (IOC, `LiquidationMarket`) emridir,
    defterde durmaz; kalanı saniyeler içinde HLP Liquidator'a geçer, o da ≤$18K'lık
    dilimlerle çoğunlukla taker çıkar.
  • Bir hesabın post-only (`Alo`) emri: her 1.5–3 sn iptal edip KALANINI en iyi
    fiyata yeniden koyar (cloid sabit, oid değişir) → fiyatla aşağı da yukarı da
    gider. Taker alışlar onu yer; sahibi taker ücreti ödemeden pozisyon kurar ya da
    boşaltır. SAND: vault "drkmttr" 56 dk'da $2.46M short açtı, dolumların %100'ü
    maker, hiçbirinde `liquidation` yok.

NASIL YAKALANIR:
  • Defter seviyesindeki `n` EMİR SAYISIDIR: n==1 → tek emir. MM yığınları n=5–21.
  • Kapı: ≥ `sticky_min_usd` VE ≥ 24s hacmin `sticky_min_vol_pct` %'si. Canlı
    kalibrasyon: SAND'ın tek emri %5.8; ana dexteki en büyük MM yığını ≤%0.24.
  • İki kaynak: (1) kollektörün ZATEN aldığı işlem akışı — tek maker bir yanın
    taker akışını domine ediyorsa o coin ODAĞA girer (bedava, anında); (2) seyrek,
    düşük öncelikli l2Book taraması (yenmeyen duvar da görülsün). `bbo` aboneliği
    120 coinde 525 mesaj/sn ölçüldü → kollektöre bindirilmedi.
  • Kimlik oid ya da fiyatla DEĞİL — ikisi de saniyede değişir. (coin, yön) + sahip.
  • Sahip: akıştaki baskın maker (`users`), `frontendOpenOrders` ile emri doğrulanır.

Kanal yalnız İLK alarmı alır (kripto kanalı, `/takip_N` ile); yarılanma, yeni dilim,
bitiş (yenildi/çekildi) yalnız takip edene gider. TAHMİN YOK: mesaj ölçüleni yazar.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque

from ..db import alert_log, alert_recent, db, kv_set, now

log = logging.getLogger("radar.stickywall")

TICK_SEC = 15               # döngü adımı
FLOW_SEC = 180              # coin başına akış penceresi (maker dolumları)
TRIGGER_WIN = 120           # tetik: son bu kadar sn'deki taker akışı
TRIGGER_USD = 25_000        # tetik: tek makerın o yanda aldığı en az $
TRIGGER_SHARE = 0.60        # tetik: o yanın taker akışındaki payı
TRIGGER_COOLDOWN = 900      # boş çıkan tetikten sonra aynı coin bu kadar susar (tek MM'li coin
                            # her 2 dk'da normal öncelikli defter yemesin)
FOCUS_SEC = 30              # odaktaki coinin defteri bu aralıkla sorulur
FOCUS_HOLD = 300            # tetikle giren odak en çok bu kadar sürer
CONFIRM_SEC = 60            # aday → teyit: en az bu kadar arayla iki görüş
GONE_SEC = 90               # bu kadar görünmezse bitti (iptal-yeniden-koy boşluğu var)
MISS_MIN = 2                # … ve en az bu kadar ARDIŞIK kaçırma (tek bakış asla bitirmez)
REJOIN_SEC = 900            # aynı sahip bu süre içinde dönerse aynı duvar ("yeniden geldi")
COOLDOWN_SEC = 6 * 3600     # aynı coin+yön için kanal alarmı aralığı
MAX_LEVELS = 10             # en iyi fiyattan bu kadar seviye içinde
NEAR_PCT = 1.0              # ve aynı yanın en iyi fiyatına %1 içinde
CONT_MIN_USD = 50_000       # izlenen duvar bunun altına inerse "görünmüyor" sayılır
CONT_SHARE = 0.10           # … ya da tepesinin %10'unun altına
TRANCHE_UP = 1.25           # boyut bu katı aşarsa yeni dilim
TRANCHE_MIN_USD = 250_000   # … ve en az bu kadar artarsa
HALF_SHARE = 0.5            # takipçiye "yarılandı": kalan ≤ tepenin yarısı
EATEN_FULL = 0.70           # bitişte son kalanın ≥%70'i sahibin dolumuysa → yenildi
PULLED_MAX = 0.20           # ≤%20'si dolumsa → çekildi
OWNER_BAND_PCT = 0.5        # sahip dolumu duvar fiyatının bu bandında sayılır
OWNER_MIN_USD = 5_000       # akıştan sahip adayı için en az dolum
MATCH_TOL = 0.35            # açık emir eşleşmesi: $ ±%35 (yeniden koyma arası dolar)
FALLBACK_CANDS = 5          # akış sahibi doğrulanamazsa sorulacak pozisyon sahibi
FOLLOW_DAYS = 2             # takip kendiliğinden kapanır
TRACK_DRIFT_PCT = 3.0       # doğrulanmış sahibin emri: son fiyattan bu kadar içinde aranır
SWEEP_TIMEOUT = 8           # düşük öncelikli tarama isteği en çok bu kadar bekler (odak beklemesin)
KEEP_DAYS = 30              # bitmiş duvar kaydı
STATS_KV = "sticky_stats"
EFFECT_TXT = {("ask", "open"): "SHORT açıyor / büyütüyor",
              ("ask", "close"): "LONG'u boşaltıyor",
              ("ask", "flip"): "LONG'u kapatıp SHORT açıyor",
              ("bid", "open"): "LONG açıyor / büyütüyor",
              ("bid", "close"): "SHORT'u kapatıyor",
              ("bid", "flip"): "SHORT'u kapatıp LONG açıyor"}


class Registry:
    """Bellek içi durum. Yeniden başlatmada akış ve adaylar SIFIRLANIR (izlenen
    duvarlar DB'de); künye bunu söyler."""

    def __init__(self):
        self.flow: dict[str, deque] = {}      # coin -> (ts, maker, wall_side, ntl, px)
        self.observed = 0
        self.errors = 0
        self.focus: dict[str, int] = {}       # tetikle odak: coin -> bitiş ts
        self.quiet: dict[str, int] = {}       # boş çıkan tetik: coin -> susma bitişi
        self.cands: dict[tuple[str, str], dict] = {}
        self.last_check: dict[str, int] = {}
        self.eat_ts: dict[int, int] = {}      # wall_id -> yenen hesabının imleci
        self.misses: dict[int, list] = {}     # wall_id -> [ilk kaçırma ts, ardışık kaçırma]
        self.counted: set[tuple] = set()      # (wall_id, sebep): gönderilemedi bir kez sayılsın
        self.names: dict[str, str] = {}       # vault adı önbelleği ('' = vault değil)
        self.cursor = 0
        self.started = now()
        self.ticks = 0
        self.tot: dict[str, int] = {}


REG = Registry()


def _bump(key: str, n: int = 1) -> None:
    REG.tot[key] = REG.tot.get(key, 0) + n


# ---------------- sıcak yol ----------------

def observe(coin: str, aggr: str, buyer: str, seller: str, px: float, sz: float, ts: int) -> None:
    """Kollektörün her işleminde (yalnız ana dex kripto) — SENKRON, I/O yok.

    HL'de `side` AGRESÖRÜ söyler: "B" = taker alış → MAKER satıcıdır ve duvar
    `ask` tarafındadır; "A" = taker satış → maker alıcı, duvar `bid`."""
    if aggr == "B":
        maker, side = seller, "ask"
    elif aggr == "A":
        maker, side = buyer, "bid"
    else:
        return
    if not maker:
        return
    dq = REG.flow.get(coin)
    if dq is None:
        dq = REG.flow[coin] = deque()
    dq.append((int(ts), maker.lower(), side, float(px) * float(sz), float(px)))
    lim = int(ts) - FLOW_SEC
    while dq and dq[0][0] < lim:
        dq.popleft()
    REG.observed += 1


# ---------------- saf yardımcılar ----------------

def parse_levels(book: dict) -> tuple[list[dict], list[dict]]:
    """l2Book → (bids, asks); her seviye {px, sz, n}. `bookwall._parse_book`
    `n`'yi atar — burada tek emir sorusunun cevabı tam o alan."""
    levels = (book or {}).get("levels") or []
    if len(levels) != 2:
        return [], []
    out = []
    for side_levels in levels:
        parsed = []
        for lv in side_levels or []:
            try:
                parsed.append({"px": float(lv["px"]), "sz": float(lv["sz"]),
                               "n": int(lv.get("n") or 0)})
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
        out.append(parsed)
    return out[0], out[1]


def find_sticky(bids: list[dict], asks: list[dict], day_vol: float | None,
                min_usd: float, min_vol_pct: float, max_n: int = 1,
                max_levels: int = MAX_LEVELS, near_pct: float = NEAR_PCT) -> list[dict]:
    """Yan başına en büyük tek emirli seviye (en iyi fiyata yakın), kapıyı geçerse.

    `min_vol_pct > 0` iken hacim bilinmiyorsa seviye DÖNMEZ — oran kuralı
    uygulanamıyorsa alarm da yok. `max_n=2`: izlenen duvarın aynı fiyata denk gelen
    küçük bir MM emriyle paylaştığı an (WS'te görüldü) kaçmasın."""
    if not bids or not asks:
        return []
    best_bid, best_ask = bids[0]["px"], asks[0]["px"]
    if best_bid <= 0 or best_ask <= 0:
        return []
    mid = (best_bid + best_ask) / 2
    bid_ntl = sum(lv["px"] * lv["sz"] for lv in bids)
    ask_ntl = sum(lv["px"] * lv["sz"] for lv in asks)
    out = []
    for side, levels, best, side_ntl, opp_ntl in (("ask", asks, best_ask, ask_ntl, bid_ntl),
                                                  ("bid", bids, best_bid, bid_ntl, ask_ntl)):
        top = None
        for i, lv in enumerate(levels[:max_levels]):
            if abs(lv["px"] - best) / best * 100 > near_pct:
                break                       # seviyeler dışa doğru sıralı
            if not 1 <= lv["n"] <= max_n:
                continue
            ntl = lv["px"] * lv["sz"]
            if top is None or ntl > top["ntl"]:
                top = {"px": lv["px"], "sz": lv["sz"], "n": lv["n"], "ntl": ntl, "level": i}
        if top is None or top["ntl"] < min_usd:
            continue
        vol_pct = top["ntl"] / day_vol * 100 if day_vol and day_vol > 0 else None
        if min_vol_pct > 0 and (vol_pct is None or vol_pct < min_vol_pct):
            continue
        out.append({**top, "side": side, "vol_pct": vol_pct, "mid": mid,
                    "dist_pct": abs(top["px"] - mid) / mid * 100,
                    "side_ntl": side_ntl, "opp_ntl": opp_ntl,
                    "share": top["ntl"] / side_ntl if side_ntl else 1.0})
    return out


def maker_fills(rows, side: str, since: int, px_lo: float | None = None,
                px_hi: float | None = None, maker: str | None = None,
                until: int | None = None) -> dict[str, list]:
    """maker -> [toplam $, dolum sayısı] (yan + [since, until] + isteğe bağlı fiyat bandı)."""
    agg: dict[str, list] = {}
    for ts, mk, s, ntl, px in rows or ():
        if s != side or ts < since or (until is not None and ts > until):
            continue
        if maker is not None and mk != maker:
            continue
        if px_lo is not None and not (px_lo <= px <= px_hi):
            continue
        a = agg.setdefault(mk, [0.0, 0])
        a[0] += ntl
        a[1] += 1
    return agg


def dominant_maker(rows, side: str, since: int, px_lo: float | None = None,
                   px_hi: float | None = None) -> dict | None:
    """O yanın taker akışını en çok karşılayan maker: {address, ntl, n, share, total}."""
    agg = maker_fills(rows, side, since, px_lo, px_hi)
    total = sum(v[0] for v in agg.values())
    if not agg or total <= 0:
        return None
    addr, (ntl, n) = max(agg.items(), key=lambda kv: kv[1][0])
    return {"address": addr, "ntl": ntl, "n": n, "share": ntl / total, "total": total}


def outcome(last_ntl: float | None, filled_after: float | None, measured: bool) -> str:
    """Bitişte ne oldu: son görülen kalanın ne kadarı sahibin dolumu oldu.
    `measured=False` (sahip yok, dolum geçmişi okunamadı, akış o aralığı görmedi)
    → 'kayboldu': dolum mu çekilme mi, veriye dayanmadan söylenmez."""
    if not measured or not last_ts_ok(last_ntl):
        return "kayboldu"
    r = (filled_after or 0.0) / last_ntl
    if r >= EATEN_FULL:
        return "yenildi"
    if r <= PULLED_MAX:
        return "çekildi"
    return "kısmen"


def last_ts_ok(last_ntl) -> bool:
    try:
        return float(last_ntl or 0) > 0
    except (TypeError, ValueError):
        return False


def effect_of(side: str, pos: dict | None, reduce_only, sz: float | None = None) -> str:
    """'open' (açıyor/büyütüyor) | 'close' (kapatıyor/boşaltıyor) | 'flip' (karşı
    pozisyondan BÜYÜK, reduce-only olmayan emir: kapatıp ters yönü açar)."""
    if reduce_only:
        return "close"
    if pos is None:
        return "open"
    against = pos["side"] == ("long" if side == "ask" else "short")
    if not against:
        return "open"
    if sz and abs(float(sz)) > abs(float(pos.get("szi") or 0)):
        return "flip"
    return "close"


def position_of(state: dict | None, coin: str) -> dict | None:
    for p in (state or {}).get("assetPositions") or []:
        pp = (p or {}).get("position") or {}
        if pp.get("coin") != coin:
            continue
        try:
            szi = float(pp.get("szi") or 0)
        except (TypeError, ValueError):
            return None
        if not szi:
            return None
        try:
            liq = float(pp["liquidationPx"]) if pp.get("liquidationPx") else None
        except (TypeError, ValueError):
            liq = None
        return {"side": "long" if szi > 0 else "short", "szi": szi,
                "ntl": abs(float(pp.get("positionValue") or 0)),
                "entry": float(pp.get("entryPx") or 0) or None, "liq": liq}
    return None


def match_order(orders, coin: str, side: str, px: float, ntl: float) -> dict | None:
    """Açık emirlerde duvarla eşleşen emir: aynı coin+yön, fiyat bandında, $ ±MATCH_TOL."""
    want = "A" if side == "ask" else "B"
    best, best_d = None, None
    for o in orders or []:
        try:
            if (o.get("coin") or "") != coin or (o.get("side") or "") != want:
                continue
            if o.get("isTrigger"):
                continue
            opx = float(o.get("limitPx") or 0)
            osz = float(o.get("sz") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if opx <= 0 or osz <= 0 or px <= 0:
            continue
        if abs(opx - px) / px * 100 > OWNER_BAND_PCT:
            continue
        ontl = opx * osz
        if not (1 - MATCH_TOL) * ntl <= ontl <= (1 + MATCH_TOL) * ntl:
            continue
        d = abs(ontl - ntl)
        if best is None or d < best_d:
            best, best_d = o, d
    if best is None:
        return None
    return {"tif": best.get("tif") or best.get("orderType") or "",
            "reduce_only": 1 if best.get("reduceOnly") else 0,
            "cloid": best.get("cloid") or "", "order_ts": int((best.get("timestamp") or 0) // 1000),
            "oid": best.get("oid")}


def owner_order(orders, coin: str, side: str, px_ref: float, reduce_only=None,
                drift_pct: float = TRACK_DRIFT_PCT) -> dict | None:
    """Doğrulanmış sahibin açık emirleri içinde DUVAR: o coin+yönde, son fiyata
    `drift_pct` içinde, aynı reduce-only bayrağında EN BÜYÜK emir. Defterdeki
    "en büyük seviye" yerine sahibin kendi emri izlenir: yakına başka birinin
    büyük emri gelse de kimlik kaymaz, taban altına inen kuyruk da görünür kalır."""
    want = "A" if side == "ask" else "B"
    best = None
    for o in orders or []:
        try:
            if (o.get("coin") or "") != coin or (o.get("side") or "") != want or o.get("isTrigger"):
                continue
            if reduce_only is not None and bool(o.get("reduceOnly")) != bool(reduce_only):
                continue
            opx, osz = float(o.get("limitPx") or 0), float(o.get("sz") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if opx <= 0 or osz <= 0 or (px_ref and abs(opx - px_ref) / px_ref * 100 > drift_pct):
            continue
        if best is None or opx * osz > best["ntl"]:
            best = {"px": opx, "sz": osz, "ntl": opx * osz, "cloid": o.get("cloid") or "",
                    "oid": o.get("oid")}
    return best


def book_level(bids: list[dict], asks: list[dict], side: str, px: float) -> dict:
    """Defterde o fiyatın sırası, emir sayısı ve karşı derinlik (yoksa level None)."""
    levels, opp = (asks, bids) if side == "ask" else (bids, asks)
    out = {"level": None, "n": None, "opp_ntl": sum(lv["px"] * lv["sz"] for lv in opp)}
    for i, lv in enumerate(levels):
        if abs(lv["px"] - px) <= px * 1e-9:
            out.update(level=i, n=lv["n"])
            break
    return out


def pick_continuation(bids: list[dict], asks: list[dict], side: str, row: dict,
                      floor: float) -> dict | None:
    """Sahibi DOĞRULANMAMIŞ duvarın devamı: en büyük seviye değil, son fiyata en
    yakın tek emirli seviye — ve son boyuttan BÜYÜK olmayan (büyüme doğrulanamaz;
    başka birinin emri dilim gibi okunmasın)."""
    levels, opp = (asks, bids) if side == "ask" else (bids, asks)
    if not levels or not opp:
        return None
    best_px = levels[0]["px"]
    last_px = float(row.get("px_last") or best_px)
    cap = float(row.get("ntl_last") or 0) * 1.10 + 1.0
    best = None
    for i, lv in enumerate(levels[:MAX_LEVELS]):
        if abs(lv["px"] - best_px) / best_px * 100 > NEAR_PCT:
            break
        ntl = lv["px"] * lv["sz"]
        if not 1 <= lv["n"] <= 2 or ntl < floor or ntl > cap:
            continue
        d = abs(lv["px"] - last_px)
        if best is None or d < best[0]:
            best = (d, {"px": lv["px"], "sz": lv["sz"], "n": lv["n"], "ntl": ntl, "level": i,
                        "opp_ntl": sum(x["px"] * x["sz"] for x in opp)})
    return best[1] if best else None


def _flow_covered(since: int) -> bool:
    """Akış [since, şimdi] aralığını gördü mü? Yeniden başlatma ya da WS kesintisi
    varsa HAYIR — o zaman "çekildi" demek veriye dayanmaz."""
    if REG.started > since:
        return False
    try:
        from ..hl import collector as colmod
        live = colmod.LIVE
        if live is None:
            return True                     # kollektörsüz kurulum (test) — akış elle beslenir
        return bool(live.connected) and float(live.connected_since or 0) <= since
    except Exception:
        return False


# ---------------- DB ----------------

_COLS = ("coin", "side", "first_ts", "confirm_ts", "last_ts", "alerted_ts", "px_first",
         "px_last", "px_min", "px_max", "ntl_first", "ntl_last", "peak_ntl", "sz_last",
         "level_last", "n_last", "day_vol", "oi_usd", "opp_ntl", "n_seen", "n_moves",
         "tranches", "owner", "owner_src", "owner_name", "owner_fill", "owner_share",
         "order_tif", "reduce_only", "cloid", "order_ts", "pos_side", "pos_szi", "pos_ntl",
         "pos_entry", "pos_liq", "effect", "eaten_usd", "status", "end_ts", "end_pos_side",
         "end_pos_szi", "end_pos_ntl", "active", "offer_id", "seg_peak")


async def _insert(row: dict) -> int:
    cols = [c for c in _COLS if c in row]
    async with db() as conn:
        cur = await conn.execute(
            f"INSERT INTO sticky_walls({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
            tuple(row[c] for c in cols))
        return int(cur.lastrowid)


async def _update(wall_id: int, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    async with db() as conn:
        await conn.execute(f"UPDATE sticky_walls SET {sets} WHERE id=?",
                           (*fields.values(), int(wall_id)))


async def wall(wall_id: int) -> dict | None:
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM sticky_walls WHERE id=?", (int(wall_id),))
        r = await cur.fetchone()
        return dict(r) if r else None


async def _actives() -> dict[tuple[str, str], dict]:
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM sticky_walls WHERE active=1")
        return {(r["coin"], r["side"]): dict(r) for r in await cur.fetchall()}


async def _recent_ended(ts: int) -> dict[tuple[str, str], dict]:
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM sticky_walls WHERE active=0 AND end_ts>=? ORDER BY end_ts",
            (ts - REJOIN_SEC,))
        return {(r["coin"], r["side"]): dict(r) for r in await cur.fetchall()}


async def page_rows(hours: int = 24) -> dict:
    """/yapiskan için: aktifler + son `hours` saatte bitenler + bellekteki adaylar.
    SAF DEĞİL ama HL'ye istek ATMAZ (DB + bellek)."""
    ts = now()
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM sticky_walls WHERE active=1 ORDER BY ntl_last DESC")
        act = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT * FROM sticky_walls WHERE active=0 AND end_ts>=? ORDER BY end_ts DESC LIMIT 100",
            (ts - hours * 3600,))
        ended = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute("SELECT wall_id, COUNT(*) n FROM sticky_follows WHERE active=1 GROUP BY wall_id")
        fol = {r["wall_id"]: r["n"] for r in await cur.fetchall()}
    for r in act + ended:
        r["followers"] = fol.get(r["id"], 0)
        r["effect_txt"] = EFFECT_TXT.get((r["side"], r.get("effect") or ""), "")
        r["vol_pct"] = (r["peak_ntl"] / r["day_vol"] * 100) if r.get("day_vol") else None
    cands = [{"coin": c, "side": s, **{k: v for k, v in d.items() if k != "w"},
              "ntl": d["w"]["ntl"], "px": d["w"]["px"], "vol_pct": d["w"].get("vol_pct")}
             for (c, s), d in REG.cands.items()]
    return {"active": act, "ended": ended, "cands": cands, "since_start": ts - REG.started}


# ---------------- sahip ----------------

async def _vault_name(client, addr: str) -> str:
    if addr in REG.names:
        return REG.names[addr]
    try:
        d = await client.vault_details(addr)
    except Exception:
        return ""                           # hata önbelleklenmez — sonra yine sorulur
    name = (d or {}).get("name") or "" if isinstance(d, dict) else ""
    REG.names[addr] = str(name)[:40]
    return REG.names[addr]


async def _fallback_cands(coin: str, exclude: str) -> list[str]:
    """Akış sahibi doğrulanamadıysa: o coinde bilinen en büyük pozisyon sahipleri."""
    async with db() as conn:
        cur = await conn.execute(
            "SELECT address FROM addr_positions WHERE coin=? AND closed_ts IS NULL"
            " ORDER BY notional DESC LIMIT ?", (coin, FALLBACK_CANDS))
        return [r["address"] for r in await cur.fetchall() if r["address"] != exclude]


async def attribute(client, coin: str, side: str, w: dict, since: int,
                    px_lo: float, px_hi: float, prefer: str = "", until: int | None = None) -> dict:
    """Duvarın sahibi: önce `prefer` (bitmiş duvarın sahibi), sonra akıştaki baskın
    maker (bant + [since, until] içinde), sonra bilinen pozisyon sahipleri — her biri
    AÇIK EMRİYLE doğrulanır. Doğrulanan adres akış sahibinden farklıysa dolum
    rakamı o adres için yeniden hesaplanır (başkasının dolumu sahibe yazılmasın).
    Dönüş alanları DB sütunlarıyla aynı adlıdır."""
    out = {"owner": "", "owner_src": "", "owner_fill": 0.0, "owner_share": None}
    band_lo, band_hi = px_lo * (1 - OWNER_BAND_PCT / 100), px_hi * (1 + OWNER_BAND_PCT / 100)
    rows = REG.flow.get(coin) or ()
    d = None
    agg = maker_fills(rows, side, since, band_lo, band_hi, until=until)
    total = sum(v[0] for v in agg.values())
    if agg and total > 0:
        addr, (ntl, n) = max(agg.items(), key=lambda kv: kv[1][0])
        d = {"address": addr, "ntl": ntl, "share": ntl / total}
    flow_owner = d["address"] if d and d["ntl"] >= OWNER_MIN_USD else ""

    def _fill_of(addr: str) -> tuple[float, float | None]:
        v = agg.get(addr)
        return (v[0], v[0] / total) if v and total > 0 else (0.0, None)

    cands: list[str] = []
    for a in ([prefer] if prefer else []) + ([flow_owner] if flow_owner else []) \
            + await _fallback_cands(coin, flow_owner):
        if a and a not in cands:
            cands.append(a)
    for addr in cands:
        try:
            orders = await client.frontend_open_orders(addr)
        except Exception:
            log.debug("açık emirler alınamadı %s", addr[:10], exc_info=True)
            continue
        m = match_order(orders, coin, side, w["px"], w["ntl"])
        if m:
            fill, share = _fill_of(addr)
            out.update(owner=addr, owner_src="order", order_tif=m["tif"], owner_fill=fill,
                       owner_share=share, reduce_only=m["reduce_only"], cloid=m["cloid"],
                       order_ts=m["order_ts"])
            return out
    if flow_owner:
        fill, share = _fill_of(flow_owner)
        out.update(owner=flow_owner, owner_src="flow", owner_fill=fill, owner_share=share)
    return out


async def _position(client, addr: str, coin: str) -> tuple[bool, dict | None]:
    """(okunabildi mi, pozisyon)."""
    try:
        st = await client.clearinghouse(addr)
    except Exception:
        log.debug("pozisyon okunamadı %s", addr[:10], exc_info=True)
        return False, None
    return True, position_of(st, coin)


# ---------------- tur ----------------

def _universe() -> list[str]:
    try:
        from ..hl import collector as colmod
        live = colmod.LIVE
        if live is not None and live.crypto_coins:
            return sorted(live.crypto_coins)
    except Exception:
        pass
    return sorted(REG.flow)


def _triggers(ts: int) -> list[str]:
    """Akıştan bedava tetik: bir yanda tek maker ≥%60 ve ≥$25K → odak."""
    hit = []
    for coin, dq in list(REG.flow.items()):
        if ts < REG.quiet.get(coin, 0) or REG.focus.get(coin, 0) > ts:
            continue
        for side in ("ask", "bid"):
            d = dominant_maker(dq, side, ts - TRIGGER_WIN)
            if d and d["ntl"] >= TRIGGER_USD and d["share"] >= TRIGGER_SHARE:
                REG.focus[coin] = ts + FOCUS_HOLD
                hit.append(coin)
                break
    return hit


async def _book(client, coin: str, low: bool):
    from ..hl.client import PRIORITY
    tok = PRIORITY.set("low" if low else "normal")
    try:
        return await client.l2_book(coin)
    finally:
        PRIORITY.reset(tok)


async def _ctx_map(client) -> dict:
    from ..hl.universe import main_dex_ctx
    try:
        return (await main_dex_ctx(client)).get("c") or {}
    except Exception:
        log.debug("ana dex ctx alınamadı", exc_info=True)
        return {}


async def tick(cfg, client, notifier, ts: int | None = None) -> dict:
    """Bir adım: tetik → ODAK defterleri (normal öncelik) → takipçi notları →
    tarama dilimi (düşük öncelik, zaman sınırlı). Sıra önemli: düşük şerit 429
    sonrası dakikalarca bekleyebilir; izlenen duvarlar ve notlar onu beklemez."""
    ts = ts or now()
    REG.ticks += 1
    out = {"checked": 0, "swept": 0, "focus": 0, "triggers": 0, "cands": 0, "confirmed": 0,
           "alerted": 0, "no_chat": 0, "cooldown": 0, "failed": 0, "ended": 0, "reopened": 0,
           "tranches": 0, "follow_sent": 0, "follow_failed": 0, "book_err": 0,
           "focus_checked": 0, "sweep_skipped": 0}
    out["triggers"] = len(_triggers(ts))
    _bump("triggers", out["triggers"])
    actives = await _actives()
    recent = await _recent_ended(ts)
    cmap = await _ctx_map(client)

    focus = {c for c, until in REG.focus.items() if until > ts}
    focus |= {c for c, _s in actives} | {c for c, _s in REG.cands}
    for c in [c for c, until in REG.focus.items() if until <= ts]:
        REG.focus.pop(c, None)
    due = [c for c in sorted(focus) if ts - REG.last_check.get(c, 0) >= FOCUS_SEC]
    out["focus"] = len(focus)

    async def _check(coin: str, low: bool) -> None:
        try:
            if low:
                book = await asyncio.wait_for(_book(client, coin, True), SWEEP_TIMEOUT)
            else:
                book = await _book(client, coin, False)
        except asyncio.TimeoutError:
            out["sweep_skipped"] += 1
            return
        except Exception as e:
            out["book_err"] += 1
            _bump("book_err")
            log.debug("l2Book %s: %s", coin, e)
            return
        REG.last_check[coin] = ts
        out["checked"] += 1
        out["swept" if low else "focus_checked"] += 1
        try:
            await _process(cfg, client, notifier, coin, book, cmap.get(coin) or {},
                           actives, recent, ts, out)
        except Exception:
            REG.errors += 1
            log.exception("yapışkan duvar işleme %s", coin)

    for coin in due:
        await _check(coin, False)
    try:
        await follow_tick(cfg, notifier, ts, out)
    except Exception:
        REG.errors += 1
        log.exception("yapışkan duvar takipçileri")

    poll = int(getattr(cfg, "sticky_poll_sec", 300) or 0)
    uni = _universe()
    paused = 0.0
    try:
        paused = float(client.low_paused()) if hasattr(client, "low_paused") else 0.0
    except Exception:
        paused = 0.0
    if poll > 0 and uni and paused <= 0:
        k = max(1, -(-len(uni) * TICK_SEC // poll))      # tavana yuvarla
        for _ in range(min(k, len(uni))):
            REG.cursor %= len(uni)
            c = uni[REG.cursor]
            REG.cursor += 1
            if c not in focus:
                await _check(c, True)
    elif poll > 0 and uni:
        out["sweep_skipped"] += 1           # 429 sonrası düşük şerit susuyor — bu tur atla
    out["cands"] = len(REG.cands)
    for k in ("confirmed", "alerted", "ended", "reopened", "tranches", "follow_sent", "checked",
              "no_chat", "failed", "follow_failed", "sweep_skipped"):
        _bump(k, out[k])
    return out


def _cont_floor(row: dict) -> float:
    return max(CONT_MIN_USD, CONT_SHARE * float(row.get("peak_ntl") or 0))


async def _process(cfg, client, notifier, coin: str, book: dict, cinfo: dict,
                   actives: dict, recent: dict, ts: int, out: dict) -> None:
    bids, asks = parse_levels(book)
    if not bids or not asks:
        return
    day_vol = float(cinfo.get("v") or 0) or None
    mark = float(cinfo.get("m") or 0)
    oi_usd = float(cinfo.get("oi") or 0) * mark if mark else None
    min_usd = float(getattr(cfg, "sticky_min_usd", 1_000_000) or 0)
    min_pct = float(getattr(cfg, "sticky_min_vol_pct", 2.0) or 0)
    strong = {w["side"]: w for w in find_sticky(bids, asks, day_vol, min_usd, min_pct)}
    found_any = bool(strong)
    for side in ("ask", "bid"):
        key = (coin, side)
        row = actives.get(key)
        if row:
            found_any = True
            await _track(cfg, client, notifier, row, bids, asks, day_vol, oi_usd, ts, out)
            continue
        prev = recent.get(key)
        w = strong.get(side)
        if w is None and prev is not None:
            # Bitmiş duvarın sahibi dönebilir: aday tabanı izleme tabanına iner.
            w = {x["side"]: x for x in find_sticky(bids, asks, day_vol, _cont_floor(prev), 0)}.get(side)
            if w is not None:
                found_any = True
        await _candidate(cfg, client, notifier, coin, side, w, prev, day_vol, oi_usd, ts, out)
    if not found_any and REG.focus.get(coin) and not any(k[0] == coin for k in REG.cands):
        # Tetik boş çıktı (akışı tek MM karşılıyor ama defterde tek dev emir yok):
        # odaktan çık, bir süre aynı coin için tetik dinleme.
        REG.focus.pop(coin, None)
        REG.quiet[coin] = ts + TRIGGER_COOLDOWN


async def _candidate(cfg, client, notifier, coin, side, w, prev, day_vol, oi_usd, ts, out) -> None:
    key = (coin, side)
    c = REG.cands.get(key)
    if w is None:
        if c and ts - c["last_ts"] >= GONE_SEC:
            REG.cands.pop(key, None)
        return
    if c is None:
        REG.cands[key] = {"first_ts": ts, "last_ts": ts, "n": 1, "w": w, "first_w": w,
                          "max_ntl": w["ntl"], "px_min": w["px"], "px_max": w["px"], "moves": 0}
        return
    if w["px"] != c["w"]["px"]:
        c["moves"] += 1
    c.update(last_ts=ts, n=c["n"] + 1, w=w, px_min=min(c["px_min"], w["px"]),
             px_max=max(c["px_max"], w["px"]), max_ntl=max(c["max_ntl"], w["ntl"]))
    if ts - c["first_ts"] < CONFIRM_SEC:
        return
    REG.cands.pop(key, None)
    since = c["first_ts"] - TRIGGER_WIN
    if prev is not None and prev.get("end_ts"):
        since = max(since, int(prev["end_ts"]) + 1)   # bitmiş duvarın dolumları yeni sahibe sayılmaz
    until = max(ts, now())
    own = await attribute(client, coin, side, w, since, c["px_min"], c["px_max"],
                          prefer=(prev or {}).get("owner") or "", until=until)
    # Bitmiş duvarın sahibi döndü → aynı satır yeniden açılır (kanal tekrar yok). Yalnız
    # AÇIK EMRİYLE doğrulanmış aynı sahip: akış tek başına kimlik kanıtı değildir.
    if (prev is not None and own["owner"] and own["owner_src"] == "order"
            and own["owner"] == prev.get("owner")):
        back = _eaten_since(prev, int(prev.get("end_ts") or ts), until)
        await _update(prev["id"], active=1, status="aktif", end_ts=None, last_ts=ts,
                      px_last=w["px"], ntl_last=w["ntl"], sz_last=w["sz"], level_last=w["level"],
                      n_last=w["n"], peak_ntl=max(float(prev.get("peak_ntl") or 0), c["max_ntl"]),
                      seg_peak=c["max_ntl"],
                      px_min=min(float(prev.get("px_min") or w["px"]), c["px_min"]),
                      px_max=max(float(prev.get("px_max") or w["px"]), c["px_max"]),
                      tranches=int(prev.get("tranches") or 0) + 1,
                      n_moves=int(prev.get("n_moves") or 0) + c["moves"],
                      eaten_usd=float(prev.get("eaten_usd") or 0) + back,
                      owner_src="order", order_tif=own.get("order_tif"),
                      reduce_only=own.get("reduce_only"), order_ts=own.get("order_ts"),
                      cloid=own.get("cloid") or prev.get("cloid"))
        REG.eat_ts[prev["id"]] = until
        REG.misses.pop(prev["id"], None)
        out["reopened"] += 1
        log.info("🧲 yeniden geldi: %s %s %s", coin, side, _usd(w["ntl"]))
        return
    min_usd = float(getattr(cfg, "sticky_min_usd", 1_000_000) or 0)
    min_pct = float(getattr(cfg, "sticky_min_vol_pct", 2.0) or 0)
    if w["ntl"] < min_usd or (min_pct > 0 and (w.get("vol_pct") or 0) < min_pct):
        return                              # dönüş adayıydı ama sahibi farklı ve kapı altı
    fw = c["first_w"]
    row = {"coin": coin, "side": side, "first_ts": c["first_ts"], "confirm_ts": ts, "last_ts": ts,
           "px_first": fw["px"], "px_last": w["px"], "px_min": c["px_min"], "px_max": c["px_max"],
           "ntl_first": fw["ntl"], "ntl_last": w["ntl"], "peak_ntl": c["max_ntl"],
           "seg_peak": c["max_ntl"], "sz_last": w["sz"],
           "level_last": w["level"], "n_last": w["n"], "day_vol": day_vol, "oi_usd": oi_usd,
           "opp_ntl": w["opp_ntl"], "n_seen": c["n"], "n_moves": c["moves"], "tranches": 0,
           "eaten_usd": 0.0, "status": "aktif", "active": 1, **own}
    if own["owner"]:
        row["owner_name"] = await _vault_name(client, own["owner"])
        ok, pos = await _position(client, own["owner"], coin)
        if ok:
            row.update(pos_side=pos["side"] if pos else "", pos_szi=pos["szi"] if pos else 0.0,
                       pos_ntl=pos["ntl"] if pos else 0.0, pos_entry=(pos or {}).get("entry"),
                       pos_liq=(pos or {}).get("liq"),
                       effect=effect_of(side, pos, row.get("reduce_only"), w["sz"]))
        row["eaten_usd"] = own.get("owner_fill") or 0.0
    row["id"] = await _insert(row)
    REG.eat_ts[row["id"]] = until
    out["confirmed"] += 1
    log.info("🧲 yapışkan duvar: %s %s %s (%s) sahibi %s", coin, side, _usd(w["ntl"]),
             f"%{w['vol_pct']:.1f}" if w.get("vol_pct") else "?", (own["owner"] or "?")[:10])
    await _alert(cfg, notifier, row, out)


def _usd(n) -> str:
    from ..telegram import format as fmt
    return fmt.usd(n)


def _count_once(row: dict, reason: str, out: dict) -> None:
    """Gönderilemeyen alarm duvar başına BİR kez sayılır (her 30 sn'lik deneme değil)."""
    k = (int(row["id"]), reason)
    if k not in REG.counted:
        REG.counted.add(k)
        out[reason] += 1


async def _alert(cfg, notifier, row: dict, out: dict) -> None:
    """Kanal alarmı (kripto kanalı) + `/takip_N`. İşaret yalnız başarılı gönderimden sonra.
    Tür kapalıysa denenmez; teklif duvar başına bir kez yazılır."""
    from ..notify import kind_enabled
    from ..telegram import format as fmt
    from .twaplive import chat_for
    if notifier is None or not kind_enabled(cfg, "sticky"):
        return
    chat, can = chat_for(cfg, row["coin"])
    if not can:
        _count_once(row, "no_chat", out)
        return
    key = f"{row['coin']}:{row['side']}"
    if await alert_recent("sticky", key, COOLDOWN_SEC):
        out["cooldown"] += 1
        return
    offer = row.get("offer_id")
    if not offer:
        offer = await _offer(row)
        if offer:
            await _update(row["id"], offer_id=offer)
            row["offer_id"] = offer
    text = fmt.sticky_alert(row, offer)
    # Anahtar duvar kimliğiyle: satılabilir botun 12 sa tekilleştirmesi aynı coin+yöndeki
    # SONRAKİ duvarı (6 sa kanal soğumasından sonra) yutmasın.
    ok = await notifier.send("sticky", text, priority="high", key=f"sticky:{row['id']}",
                             chat_id=chat, coin=row["coin"])
    if ok:
        await _update(row["id"], alerted_ts=now())
        row["alerted_ts"] = now()
        await alert_log("sticky", key, text)
        out["alerted"] += 1
    else:
        _count_once(row, "failed", out)


async def _offer(row: dict) -> int | None:
    from .. import assets
    try:
        async with db() as conn:
            cur = await conn.execute(
                """INSERT INTO track_offers(address,coin,symbol,side,notional,created_ts,kind,ref_ts)
                   VALUES(?,?,?,?,?,?,'sticky',?)""",
                (row.get("owner") or "", row["coin"], assets.label(row["coin"]), row["side"],
                 float(row.get("ntl_last") or 0), now(), int(row["id"])))
            return int(cur.lastrowid)
    except Exception:
        log.debug("yapışkan duvar takip teklifi yazılamadı", exc_info=True)
        return None


def _eaten_since(row: dict, since: int, until: int) -> float:
    """Sahibin (since, until] aralığında bu duvarın bandında aldığı maker dolumu (akıştan)."""
    owner = row.get("owner") or ""
    if not owner:
        return 0.0
    lo = float(row.get("px_min") or 0) * (1 - NEAR_PCT / 100)
    hi = float(row.get("px_max") or 0) * (1 + NEAR_PCT / 100)
    agg = maker_fills(REG.flow.get(row["coin"]) or (), row["side"], since + 1, lo, hi,
                      maker=owner, until=until)
    return sum(v[0] for v in agg.values()) if agg else 0.0


async def _owner_fills_after(client, row: dict, since: int, until: int) -> float | None:
    """Sahibin GERÇEK dolum geçmişinden (userFillsByTime) bu coin+yönde, [since, until]
    aralığındaki maker dolumu. Yeniden başlatmaya ve WS kesintisine dayanıklı;
    okunamazsa None (akışa düşülür, o da kapsanmıyorsa sonuç 'kayboldu')."""
    want = "A" if row["side"] == "ask" else "B"
    try:
        fills = await client.user_fills_by_time(row["owner"], int(since) * 1000, int(until) * 1000)
    except Exception:
        log.debug("dolum geçmişi alınamadı %s", (row.get("owner") or "")[:10], exc_info=True)
        return None
    if not isinstance(fills, list):
        return None
    tot = 0.0
    for f in fills:
        try:
            if f.get("coin") != row["coin"] or f.get("side") != want or f.get("crossed"):
                continue
            tot += float(f.get("px") or 0) * float(f.get("sz") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
    return tot


async def _track(cfg, client, notifier, row: dict, bids, asks, day_vol, oi_usd, ts, out) -> None:
    """İzlenen duvarın bu bakışı. Sahibi AÇIK EMRİYLE doğrulanmışsa duvar o emrin
    kendisidir (defterdeki en büyük seviye değil); değilse son fiyata en yakın,
    büyümeyen tek emirli seviye. Bitiş: ardışık kaçırmalar GONE_SEC'i doldurunca."""
    wid = int(row["id"])
    verified = row.get("owner_src") == "order" and row.get("owner")
    w = None
    if verified:
        try:
            orders = await client.frontend_open_orders(row["owner"])
        except Exception:
            # Sahibin emirleri okunamadı: bu bakış ne görüş ne kaçırmadır — defterdeki
            # başka bir seviyeyi duvar sanmaktansa bir sonraki bakışı bekle.
            log.debug("izleme: açık emirler alınamadı", exc_info=True)
            return
        o = owner_order(orders, row["coin"], row["side"], float(row.get("px_last") or 0),
                        row.get("reduce_only"))
        if o is not None:
            bl = book_level(bids, asks, row["side"], o["px"])
            w = {**o, "level": bl["level"], "n": bl["n"], "opp_ntl": bl["opp_ntl"]}
    else:
        w = pick_continuation(bids, asks, row["side"], row, _cont_floor(row))
    until = max(ts, now())
    if w is not None:
        REG.misses.pop(wid, None)
        cur = REG.eat_ts.get(wid, until)
        eaten = float(row.get("eaten_usd") or 0) + _eaten_since(row, cur, until)
        REG.eat_ts[wid] = until
        last = float(row.get("ntl_last") or 0)
        fields = {"last_ts": ts, "px_last": w["px"], "ntl_last": w["ntl"], "sz_last": w["sz"],
                  "level_last": w.get("level"), "n_last": w.get("n"),
                  "n_seen": int(row.get("n_seen") or 0) + 1,
                  "px_min": min(float(row.get("px_min") or w["px"]), w["px"]),
                  "px_max": max(float(row.get("px_max") or w["px"]), w["px"]),
                  "peak_ntl": max(float(row.get("peak_ntl") or 0), w["ntl"]),
                  "seg_peak": max(float(row.get("seg_peak") or 0), w["ntl"]),
                  "eaten_usd": eaten, "opp_ntl": w.get("opp_ntl")}
        if w.get("cloid"):
            fields["cloid"] = w["cloid"]
        if day_vol:
            fields["day_vol"] = day_vol
        if oi_usd:
            fields["oi_usd"] = oi_usd
        if w["px"] != row.get("px_last"):
            fields["n_moves"] = int(row.get("n_moves") or 0) + 1
        # Dilim yalnız DOĞRULANMIŞ sahibin kendi emri büyüyünce (başka emir dilim sayılmaz).
        if verified and last and w["ntl"] >= last * TRANCHE_UP and w["ntl"] - last >= TRANCHE_MIN_USD:
            fields["tranches"] = int(row.get("tranches") or 0) + 1
            fields["seg_peak"] = w["ntl"]
            out["tranches"] += 1
            log.info("🧲 yeni dilim: %s %s %s → %s", row["coin"], row["side"], _usd(last), _usd(w["ntl"]))
        await _update(wid, **fields)
        row.update(fields)
        # Kanal alarmı daha önce gidemediyse ve hâlâ kapıdaysa yeniden dene.
        if not row.get("alerted_ts"):
            min_usd = float(getattr(cfg, "sticky_min_usd", 1_000_000) or 0)
            min_pct = float(getattr(cfg, "sticky_min_vol_pct", 2.0) or 0)
            vp = w["ntl"] / day_vol * 100 if day_vol else None
            if w["ntl"] >= min_usd and (min_pct <= 0 or (vp or 0) >= min_pct):
                await _alert(cfg, notifier, row, out)
        return
    # Kaçırma: tek bakış (iptal-yeniden-koy boşluğu, yeniden başlatma sonrası ilk tur)
    # ASLA bitirmez — ardışık en az MISS_MIN kaçırma ve GONE_SEC dolmalı.
    m = REG.misses.setdefault(wid, [ts, 0])
    m[1] += 1
    if m[1] < MISS_MIN or ts - m[0] < GONE_SEC - FOCUS_SEC:
        return
    last_ts = int(row.get("last_ts") or ts)
    cur = REG.eat_ts.get(wid, until)
    eaten = float(row.get("eaten_usd") or 0) + _eaten_since(row, cur, until)
    after = None
    if verified:
        after = await _owner_fills_after(client, row, last_ts, until)
    if after is None and row.get("owner") and _flow_covered(last_ts):
        after = _eaten_since(row, last_ts, until)
    status = outcome(row.get("ntl_last"), after, after is not None)
    fields = {"active": 0, "status": status, "end_ts": ts, "eaten_usd": eaten}
    if row.get("owner"):
        ok, pos = await _position(client, row["owner"], row["coin"])
        if ok:
            fields.update(end_pos_side=pos["side"] if pos else "",
                          end_pos_szi=pos["szi"] if pos else 0.0,
                          end_pos_ntl=pos["ntl"] if pos else 0.0)
    await _update(wid, **fields)
    REG.eat_ts.pop(wid, None)
    REG.misses.pop(wid, None)
    out["ended"] += 1
    log.info("🧲 bitti: %s %s %s (tepe %s, yenen %s)", row["coin"], row["side"], status,
             _usd(row.get("peak_ntl")), _usd(eaten))


# ---------------- takip (yalnız basana) ----------------

async def follow_start(cfg, wall_id: int, chat_id: str = "") -> tuple[int | None, dict | None]:
    """(takip id, duvar). Duvar yoksa ya da dönüş penceresi de geçtiyse (None, duvar)."""
    w = await wall(wall_id)
    if not w:
        return None, None
    ts = now()
    if not w["active"] and ts - int(w.get("end_ts") or 0) >= REJOIN_SEC:
        return None, w
    async with db() as conn:
        cur = await conn.execute("SELECT id FROM sticky_follows WHERE wall_id=? AND chat_id=?",
                                 (int(wall_id), chat_id or ""))
        r = await cur.fetchone()
        if r:
            # Yeniden basış: işaretler de BUGÜNE çekilir (aradaki eski dilim/bitiş yeni not olmasın).
            await conn.execute(
                "UPDATE sticky_follows SET active=1, expires_ts=?, end_note=NULL, tranche_seen=?,"
                " half_ts=NULL, end_seen_ts=? WHERE id=?",
                (ts + FOLLOW_DAYS * 86400, int(w.get("tranches") or 0),
                 None if w["active"] else w.get("end_ts"), r["id"]))
            return int(r["id"]), w
        cur = await conn.execute(
            """INSERT INTO sticky_follows(wall_id,chat_id,created_ts,expires_ts,active,tranche_seen,
                 end_seen_ts) VALUES(?,?,?,?,1,?,?)""",
            (int(wall_id), chat_id or "", ts, ts + FOLLOW_DAYS * 86400,
             int(w.get("tranches") or 0), None if w["active"] else w.get("end_ts")))
        return int(cur.lastrowid), w


async def follow_stop(follow_id: int, note: str = "bırakıldı") -> bool:
    async with db() as conn:
        cur = await conn.execute("UPDATE sticky_follows SET active=0, end_note=? WHERE id=? AND active=1",
                                 (note, int(follow_id)))
        return bool(cur.rowcount)


async def follows_active(chat_id: str | None = None) -> list[dict]:
    q = ("SELECT f.*, w.coin, w.side, w.ntl_last, w.peak_ntl, w.status, w.active wall_active"
         " FROM sticky_follows f JOIN sticky_walls w ON w.id=f.wall_id WHERE f.active=1")
    args: list = []
    if chat_id is not None:
        q += " AND f.chat_id=?"
        args.append(chat_id)
    async with db() as conn:
        cur = await conn.execute(q + " ORDER BY f.created_ts DESC", tuple(args))
        return [dict(r) for r in await cur.fetchall()]


async def follow_tick(cfg, notifier, ts: int, out: dict) -> None:
    """Takipçilere aşama notları. Gönderilemezse işaret YAZILMAZ — sonraki adım dener."""
    from ..telegram import format as fmt
    async with db() as conn:
        cur = await conn.execute(
            "SELECT f.id fid, f.chat_id, f.expires_ts, f.half_ts, f.tranche_seen, f.end_seen_ts,"
            " w.* FROM sticky_follows f JOIN sticky_walls w ON w.id=f.wall_id WHERE f.active=1")
        rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        fid = r["fid"]
        if r["expires_ts"] and ts >= int(r["expires_ts"]):
            await follow_stop(fid, "süre doldu")
            continue
        if notifier is None:
            continue
        stage, fields = None, {}
        if not r["active"]:
            if r.get("end_seen_ts") != r.get("end_ts"):
                stage, fields = "end", {"end_seen_ts": r["end_ts"]}
            elif ts - int(r.get("end_ts") or 0) >= REJOIN_SEC:
                await follow_stop(fid, r.get("status") or "bitti")
                continue
        elif int(r.get("tranches") or 0) > int(r.get("tranche_seen") or 0):
            stage = "reopen" if r.get("end_seen_ts") else "tranche"
            fields = {"tranche_seen": int(r["tranches"]), "end_seen_ts": None, "half_ts": None}
        elif (not r.get("half_ts") and (r.get("seg_peak") or r.get("peak_ntl"))
              and float(r.get("ntl_last") or 0)
              <= HALF_SHARE * float(r.get("seg_peak") or r["peak_ntl"])):
            # Kıyas bu DİLİMİN tepesiyle (seg_peak): eski tepenin yarısı altında dönen
            # duvar "yeniden geldi"nin hemen ardından "yarılandı" demesin.
            stage, fields = "half", {"half_ts": ts}
        if not stage:
            continue
        text = fmt.sticky_end(r) if stage == "end" else fmt.sticky_note(r, stage)
        # 'critical': kişi bunu bilerek istedi — sessiz saatte ertelenip her 15 sn'de
        # sabah özetine yeniden yazılmasın (twapfollow bitiş notuyla aynı).
        ok = await notifier.send("track", text, priority="critical",
                                 key=f"sticky:{fid}:{stage}:{r.get('tranches') or 0}",
                                 chat_id=r["chat_id"] or "")
        if not ok:
            out["follow_failed"] += 1
            continue
        out["follow_sent"] += 1
        sets = ", ".join(f"{k}=?" for k in fields)
        async with db() as conn:
            await conn.execute(f"UPDATE sticky_follows SET {sets} WHERE id=?", (*fields.values(), fid))


async def prune() -> int:
    ts = now()
    async with db() as conn:
        cur = await conn.execute("DELETE FROM sticky_walls WHERE active=0 AND end_ts < ?",
                                 (ts - KEEP_DAYS * 86400,))
        n = cur.rowcount or 0
        await conn.execute("DELETE FROM sticky_follows WHERE active=0 AND created_ts < ?",
                           (ts - KEEP_DAYS * 86400,))
    return n


async def loop(cfg, client, notifier) -> None:
    """Denetimli döngü. Site ASLA buna bağımlı değil."""
    from ..health import beat
    await asyncio.sleep(75)               # evren + kollektör akışı otursun
    n = 0
    while True:
        try:
            if getattr(cfg, "sticky_enabled", True):
                out = await tick(cfg, client, notifier)
                n += 1
                if n % 4 == 1:              # ~dakikada bir yaz (kv yazımı her 15 sn gereksiz)
                    await kv_set(STATS_KV, {**out, "ts": now(), "tot": dict(REG.tot),
                                            "observed": REG.observed, "errors": REG.errors,
                                            "flow_coins": len(REG.flow), "universe": len(_universe()),
                                            "started": REG.started})
                if out["confirmed"] or out["ended"] or out["alerted"]:
                    log.info("yapışkan duvar: %d teyit, %d bildirim, %d bitti", out["confirmed"],
                             out["alerted"], out["ended"])
                if n % 240 == 0:            # ~saatte bir
                    await prune()
            else:
                await kv_set(STATS_KV, {"ts": now(), "disabled": True})
            await beat("stickywall")
        except asyncio.CancelledError:
            raise
        except Exception:
            REG.errors += 1
            log.exception("yapışkan duvar turu")
        await asyncio.sleep(TICK_SEC)
