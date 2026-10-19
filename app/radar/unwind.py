"""🧯 Sert satış / pozisyon boşaltma — fiyatı oynatacak büyüklükte pozisyonu olan cüzdan onu piyasaya vurarak kapatmaya
başlayınca, aynı cüzdanın HÂLÂ AÇIK öteki pozisyonları (ölçüm; tahmin değil).

Kullanıcı (19.10, 0x0871…e927 "cbrskapatan"): "bir coinin fiyatını etkileyebilecek kadar büyük pozu varsa ve tüm pozları
kapamaya başladıysa bildirim istiyorum yeni kanal" · "adam kapamaya başladı ve yarım saatte çok büyük pozlar kapadı bizim
o yarım saat içinde yakalayıp kapayacağı diğer coinleri olası olarak yakalayacağımız bir şey" · "dünü ayrı bugünü ayrı
yakalamak lazımdı … dün yaptı bugün devam eder gibi bi paterne kayma sakın; mantık: sert satışçının diğer pozları da
kapatabileceği".

Vaka (19.10, scratchpad/unwind/case_checked.md): 20:22–20:58'de 7 long ($10,47M) 80 elle piyasa emriyle (FrontendMarket
reduceOnly; yarım / çeyrek / sabit dilim) kapandı; ilk coin CBRS (24s hacmin %14'ü) 3 dk'da 20 emir. 18.10 20:20'de CBRS'nin
%20'si 1 dk'da piyasaya vuruldu; 23:16 ve 23:40 dalgalarıyla 3,3 sa'te yarısına indi (−%2,8). 21 günde 7 kez bütün defter
dakikalar içinde boşaltılmış.

Kararlar:
  • Tetik canlı WS akışından (ek istek yok): TEK adres bir coinde UNWIND_WINDOW_SEC (300) içinde piyasadan (taker)
    ≥ max(UNWIND_MIN_USD $100K, UNWIND_RATE_X (10) × coinin ortalama dakika hacmi) satarsa (short için alırsa) aday.
    Her sert satış kendi başına bir olaydır — "dün başladı bugün sürer" mantığı yok. Spot çiftleri tetiklemez.
  • Doğrulama önce UCUZ olandan: adresin yalnız pencere içindeki toplulaştırılmış işlemleri (1 istek). Satış KENDİ
    pozisyonunu azaltıyor olmalı ('Close Long/Short', taker, kendi likidasyonu değil); azaltılan pozisyon satıştan önce
    "fiyatı etkileyecek" büyüklükte (≥ UNWIND_BIG_USD $250K VE ≥ UNWIND_BIG_MIN (30) dk ortalama hacim). Ancak bunlar
    geçerse bütün dex'lerin pozisyonları: ≤ UNWIND_MAX_POSITIONS (15) pozisyon; ≥1 öteki açık ≥ UNWIND_OTHER_MIN_USD.
  • Mesaj yalnız UNWIND_CHAT_ID (env-only; boşsa hiç sorgu yok, mesaj yok — ana sohbete düşmez). Aynı cüzdan 2 sa
    içinde başka bir coini sert satarsa kısa "devam" mesajı (en çok UNWIND_MAX_UPDATES); defter boşalınca tek kapanış
    özeti. Ölçülü dil: "kapatacak / muhtemelen" yok; "hâlâ açık" listesi büyüklük ve hacme oranıyla.
  • "mm" etiketine güvenilmez (bu cüzdan 24 sa'te 80+ fill kuralıyla mm sayılabiliyordu) — süzgeç pozisyon sayısı.

İnceleme (19.10, yeniden üretildi): işlemler 1 saat geriden isteniyordu — HL en ESKİ 2000 satırı verdiği için yoğun cüzdanda
satışın kendisi kesilebiliyordu; bekleme yönsüzdü — reddedilen bir alım aynı coindeki gerçek satışı 15 dk susturuyordu;
aday adres başına tekti; susturulan yoklamalar bölümü hiç bitirmiyordu; 11 dex sorgusu ucuz işlem kontrolünden önceydi
(çöküşte kuyruk tıkanırdı); STATE 2 sn'de bir yazılıyordu.

Kör nokta, açıkça: yalnız WS'te dinlenen coinlerdeki satış tetikler (ana dex ilk 120 + PROPR, xyz, para); dinlenmeyen
bir coinde boşaltma başlarsa ilk mesaj, cüzdan dinlenen bir coinde sert satana kadar gelmez. Limit emirle (maker)
yavaş boşaltma tetiklemez. Coin hacmi bilinmiyorsa o coin tetiklemez.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from ..db import alert_log, kv_get, kv_set, now

log = logging.getLogger("radar.unwind")

STATE_KV = "unwind_state"
STATS_KV = "unwind_stats"
TICK = 2                       # aday kuyruğu (sn)
VOL_SEC = 300                  # coin dakika hacmi tazeleme
POLL_SEC = 120                 # açık bölümlerde pozisyon yoklaması (defter boşaldı mı)
EPISODE_GAP = 2 * 3600         # aynı cüzdanın sonraki sert satışı bu süre içindeyse aynı bölüm (devam mesajı)
EPISODE_END = 3 * 3600         # bu kadar mesajsız geçerse bölüm sessizce kapanır
EPISODE_MAX = 6 * 3600         # bölüm en çok bu kadar sürer (sonraki sert satış yeni ilk mesaj olur)
PROBE_COOL = 900               # aynı adres+coin+yön doğrulandıktan / elendikten sonra bekleme
PROBE_RETRY = 60               # sorgu düştüyse bu kadar sonra yeniden aday olabilir
MIN_FILL_USD = 1_000           # bundan küçük işlem YENİ akış açmaz (varsa son kovaya eklenir)
MAX_PROBES_TICK = 5            # tur başına en çok doğrulama (çöküşte kuyruk nabzı / yoklamayı aç bırakmasın)
CAND_MAX_AGE = 300             # bundan eski aday atılır (son işlem anına göre)
PENDING_MAX = 200
FILL_PAGES = 3                 # toplulaştırılmış işlemlerde 2000 satır sınırı: en çok bu kadar sayfa

ON = [False]                   # döngü çalışıyor mu (kapalı / kanalsızken kanca hiçbir şey tutmaz)
FLOW: dict[tuple, deque] = {}  # (adres, coin, 'S'|'B') → deque[[ts, $, px]] — yalnız taker
PENDING: dict[tuple, dict] = {}   # (adres, coin, yön) → aday
COOL: dict[tuple, float] = {}  # (adres, coin, yön) → beklemenin bittiği an
VOL1M: dict[str, float] = {}   # coin → ortalama dakika hacmi ($)
STATE: dict = {"episodes": {}}
STATS: dict = {"errors": 0, "candidates": 0, "probes": 0, "probe_err": 0, "sent": 0, "updates": 0, "done": 0,
               "rejected": {}, "day": "", "sent_day": 0, "dropped": 0}


def _cfg(cfg, name, default):
    v = getattr(cfg, name, default)
    return default if v is None else v


def _spot(coin: str) -> bool:
    return coin.startswith("@") or "/" in coin


# ---------------- sıcak yol: kollektör kancası ----------------

def threshold(cfg, coin: str) -> float | None:
    v1 = VOL1M.get(coin)
    if not v1:
        return None
    return max(float(_cfg(cfg, "unwind_min_usd", 100_000)), float(_cfg(cfg, "unwind_rate_x", 10)) * v1)


def observe(cfg, coin: str, aggr: str, buyer: str, seller: str, px: float, sz: float, ts: int) -> None:
    """Her WS işlemi (yeniden yollanan hariç). Yalnız taker tarafı, adres+coin+yön başına kayan pencere. G/Ç yok."""
    if not ON[0] or aggr not in ("A", "B") or _spot(coin):
        return
    usd = px * sz
    taker, side = (buyer, "B") if aggr == "B" else (seller, "S")
    if not taker:
        return
    key = (taker, coin, side)
    dq = FLOW.get(key)
    if dq is None:
        if usd < MIN_FILL_USD:
            return                                  # küçük işlem yeni akış açmaz (bellek)
        dq = FLOW[key] = deque()
    if usd < MIN_FILL_USD and dq:
        dq[-1][1] += usd                            # tarama emrinin küçük parçaları kaybolmasın (tetik bir emir geç kalmasın)
    else:
        dq.append([int(ts), usd, px])
    win = int(_cfg(cfg, "unwind_window_sec", 300))
    while dq and dq[0][0] < int(ts) - win:
        dq.popleft()
    thr = threshold(cfg, coin)
    if thr is None:
        return
    total = sum(x[1] for x in dq)
    if total < thr or time.time() < COOL.get(key, 0):
        return
    if key not in PENDING:
        if len(PENDING) >= PENDING_MAX:
            STATS["dropped"] += 1
            return
        STATS["candidates"] += 1
    PENDING[key] = {"addr": taker, "coin": coin, "side": side, "usd": total, "first_ts": dq[0][0],
                    "last_ts": int(ts), "px0": dq[0][2], "px1": px, "n": len(dq), "thr": thr}


def _prune(t: float) -> None:
    for key in [k for k, dq in FLOW.items() if not dq or dq[-1][0] < t - 900]:
        FLOW.pop(key, None)
    for key in [k for k, v in COOL.items() if v < t]:
        COOL.pop(key, None)


# ---------------- doğrulama ----------------

def _positions(states: dict) -> list[dict]:
    out = []
    for st in (states or {}).values():
        for it in (st or {}).get("assetPositions") or []:
            p = (it or {}).get("position") or {}
            try:
                szi = float(p.get("szi") or 0)
                val = abs(float(p.get("positionValue") or 0))
            except (TypeError, ValueError):
                continue
            if not szi or not p.get("coin"):
                continue
            out.append({"coin": str(p["coin"]), "szi": szi, "usd": val, "side": "LONG" if szi > 0 else "SHORT",
                        "upnl": float(p.get("unrealizedPnl") or 0)})
    out.sort(key=lambda x: -x["usd"])
    return out


def since_ms(cfg, cand: dict) -> int:
    """judge'ın baktığı pencerenin başı — istek de TAM buradan başlar (HL en eski 2000 satırı verir)."""
    return (int(cand["first_ts"]) - int(_cfg(cfg, "unwind_window_sec", 300))) * 1000


def reduce_rows(fills, coin: str, side: str, since: int) -> list[dict]:
    """Adayın coininde, pencere içindeki POZİSYON AZALTAN satırlar (long kapatma = satış 'S')."""
    want = "Close Long" if side == "S" else "Close Short"
    rows = []
    for f in fills or []:
        try:
            if f.get("coin") != coin or int(f.get("time") or 0) < since:
                continue
            if not str(f.get("dir") or "").startswith(want):
                continue
            rows.append(f)
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda f: int(f.get("time") or 0))
    return rows


def judge_fills(cfg, cand: dict, fills: list) -> dict:
    """Yalnız işlemlerle verilebilen kararlar (1 istek): azaltma mı, kendi likidasyonu mu, piyasadan mı, büyük mü."""
    coin, side, addr = cand["coin"], cand["side"], str(cand.get("addr") or "").lower()
    rows = reduce_rows(fills, coin, side, since_ms(cfg, cand))
    if not rows:
        return {"ok": False, "why": "azaltma değil (açma / ekleme ya da başkasının adına)"}
    if any(str((r.get("liquidation") or {}).get("liquidatedUser") or "").lower() == addr for r in rows):
        return {"ok": False, "why": "likidasyon"}
    taker = [r for r in rows if r.get("crossed", True)]
    if not taker:
        return {"ok": False, "why": "piyasadan değil (maker)"}
    first = rows[0]
    px0 = float(first.get("px") or 0)
    before = abs(float(first.get("startPosition") or 0)) * px0
    closed = sum(abs(float(r.get("sz") or 0)) * float(r.get("px") or 0) for r in taker)
    v1 = VOL1M.get(coin) or 0
    if before < float(_cfg(cfg, "unwind_big_usd", 250_000)) or (v1 and before < float(_cfg(cfg, "unwind_big_min", 30)) * v1):
        return {"ok": False, "why": "pozisyon coine göre küçük", "before": before}
    return {"ok": True, "why": "", "before": before, "closed": closed, "pos_side": "LONG" if side == "S" else "SHORT",
            "orders": len({r.get("oid") for r in taker}), "px_first": px0, "px_last": float(taker[-1].get("px") or 0),
            "since": int(first.get("time") or 0) // 1000, "until": int(taker[-1].get("time") or 0) // 1000}


def judge(cfg, cand: dict, positions: list[dict] | None, fills: list) -> dict:
    """Aday + REST verisi → karar. `positions` None ise yalnız işlem kapıları (pozisyon sorgusu gerekmeden)."""
    res = judge_fills(cfg, cand, fills)
    if not res["ok"] or positions is None:
        return res
    coin = cand["coin"]
    maxp = int(_cfg(cfg, "unwind_max_positions", 15))
    if len(positions) > maxp:
        return {**res, "ok": False, "why": f"sepet defteri ({len(positions)} pozisyon > {maxp})"}
    other_min = float(_cfg(cfg, "unwind_other_min_usd", 100_000))
    others = [p for p in positions if p["coin"] != coin and p["usd"] >= other_min]
    same = next((p for p in positions if p["coin"] == coin and p["side"] == res["pos_side"]), None)
    flip = next((p for p in positions if p["coin"] == coin and p["side"] != res["pos_side"]), None)
    res = {**res, "now": same["usd"] if same else 0.0, "flip": flip, "others": others, "positions": positions}
    if not others and bool(_cfg(cfg, "unwind_require_others", True)):
        return {**res, "ok": False, "why": "başka açık pozisyon yok"}
    return res


async def _dexes(cfg) -> list[str]:
    from .. import assets
    from ..hl.universe import HIP3_KV
    names = [d.get("name") for d in ((await kv_get(HIP3_KV) or {}).get("dexes") or []) if d.get("name")]
    for d in assets.watched_dexes(cfg):
        if d not in names:
            names.append(d)
    return [""] + names


def _dex_of(coin: str) -> str:
    return coin.split(":", 1)[0] if ":" in coin else ""


async def _fills(client, addr: str, since: int) -> list:
    """Toplulaştırılmış işlemler `since`'ten bugüne; 2000 satır sınırında son satırdan ileri sayfalanır."""
    out: list = []
    seen = set()
    start = since
    for _ in range(FILL_PAGES):
        page = await client.info({"type": "userFillsByTime", "user": addr, "startTime": start, "aggregateByTime": True})
        page = page or []
        for f in page:
            k = (f.get("tid"), f.get("oid"), f.get("time"), f.get("coin"), f.get("sz"))
            if k not in seen:
                seen.add(k)
                out.append(f)
        if len(page) < 2000:
            break
        start = max(int(f.get("time") or 0) for f in page)
    return out


def _reject(why: str) -> None:
    r = STATS["rejected"]
    k = why.split(" (")[0]
    r[k] = r.get(k, 0) + 1


async def probe(cfg, client, notifier, cand: dict) -> str | None:
    """Adayı doğrula; uygunsa mesajı gönder. Dönüş: gönderilen metin (yoksa None)."""
    from ..telegram import format as fmt
    addr, coin, side = cand["addr"], cand["coin"], cand["side"]
    key = (addr, coin, side)
    COOL[key] = time.time() + PROBE_COOL                # sorgu sürerken aynı aday yeniden kuyruğa girmesin
    STATS["probes"] += 1
    try:
        fills = await _fills(client, addr, since_ms(cfg, cand))
        v = judge(cfg, cand, None, fills)
        if not v["ok"]:
            _reject(v["why"])
            return None
        positions = _positions(await client.clearinghouse_all(addr, await _dexes(cfg)))
    except Exception as e:                              # noqa: BLE001 — sorgu düştü: kısa sürede yeniden denenebilsin
        COOL[key] = time.time() + PROBE_RETRY
        STATS["probe_err"] += 1
        log.debug("unwind sorgu %s: %s", addr, e)
        return None
    v = judge(cfg, cand, positions, fills)
    if not v["ok"]:
        _reject(v["why"])
        return None
    chat = (getattr(cfg, "unwind_chat_id", "") or "").strip()
    t = int(time.time())
    eps = STATE.setdefault("episodes", {})
    ep = eps.get(addr)
    if ep and (t - int(ep.get("last", 0)) >= EPISODE_GAP or t - int(ep.get("start", 0)) >= EPISODE_MAX):
        ep = None                                       # yeni olay: ilk mesaj
    from .twaplive import volumes_cached
    vols = await volumes_cached(None)
    vol24 = {c: (vols.get(c) or (None,))[0] for c in [coin] + [p["coin"] for p in positions]}
    if ep:
        if coin in ep.get("coins", {}) or int(ep.get("updates", 0)) >= int(_cfg(cfg, "unwind_max_updates", 5)):
            return None                                 # susturulan: bölümün süresini uzatmaz
        ep["coins"][coin] = {"before": v["before"], "side": v["pos_side"]}
        ep["last"] = t
        ep["updates"] = int(ep.get("updates", 0)) + 1
        txt = fmt.unwind_alert(addr, coin, cand, v, vol24, ep=ep)
        kind_key = f"unwind+:{addr}:{coin}:{t // 60}"
        STATS["updates"] += 1
    else:
        other_min = float(_cfg(cfg, "unwind_other_min_usd", 100_000))
        book = {p["coin"]: {"before": p["usd"], "side": p["side"]} for p in positions if p["usd"] >= other_min}
        book[coin] = {"before": max(v["before"], book.get(coin, {}).get("before", 0)), "side": v["pos_side"]}
        eps[addr] = {"start": t, "last": t, "updates": 0, "coins": {coin: book[coin]}, "book": book,
                     "start_coins": sorted({p["coin"] for p in positions} | {coin}), "first_coin": coin, "polled": t}
        txt = fmt.unwind_alert(addr, coin, cand, v, vol24, ep=None)
        kind_key = f"unwind:{addr}:{coin}:{t // 60}"
        STATS["sent"] += 1
        STATS["sent_day"] += 1
    await _save()
    if chat:
        await notifier.send("unwind", txt, priority="high", key=kind_key, chat_id=chat, coin=coin, public=False)
    await alert_log("unwind", kind_key, txt)
    return txt


def _left(book_entry: dict, pos: dict | None) -> float:
    """Kitaptaki pozisyondan kalan $: ters yöne dönmüşse (long → short) kalan sayılmaz."""
    if not pos or pos["side"] != book_entry.get("side"):
        return 0.0
    return pos["usd"]


async def poll_episodes(cfg, client, notifier, t: int | None = None) -> list[str]:
    """Açık bölümler: defter boşaldıysa tek kapanış özeti; uzun süre mesaj yoksa sessizce kapat."""
    from ..telegram import format as fmt
    t = int(t or time.time())
    out = []
    changed = False
    chat = (getattr(cfg, "unwind_chat_id", "") or "").strip()
    eps = STATE.setdefault("episodes", {})
    for addr, ep in list(eps.items()):
        if t - int(ep.get("last", 0)) > EPISODE_END or t - int(ep.get("start", 0)) > EPISODE_MAX:
            eps.pop(addr, None)
            changed = True
            continue
        if t - int(ep.get("polled", 0)) < POLL_SEC:
            continue
        ep["polled"] = t
        changed = True
        book = ep.get("book") or {}
        need = sorted({_dex_of(c) for c in book}) or [""]
        try:
            positions = _positions(await client.clearinghouse_all(addr, need))
        except Exception:                               # noqa: BLE001
            STATS["probe_err"] += 1
            continue
        left = {p["coin"]: p for p in positions}
        if book and all(_left(b, left.get(c)) <= 0.02 * b["before"] for c, b in book.items()):
            try:                                        # özet: bütün dex'ler (yeni açılan / küçük kalanlar için)
                positions = _positions(await client.clearinghouse_all(addr, await _dexes(cfg)))
            except Exception:                           # noqa: BLE001
                pass
            txt = fmt.unwind_done(addr, ep, positions, t)
            eps.pop(addr, None)
            STATS["done"] += 1
            if chat:
                await notifier.send("unwind", txt, priority="high", key=f"unwind=:{addr}:{ep['start']}", chat_id=chat,
                                    public=False)
            out.append(txt)
    if changed:
        await _save()
    return out


async def _save() -> None:
    try:
        await kv_set(STATE_KV, STATE)
    except Exception:                                   # noqa: BLE001
        log.debug("unwind durumu yazılamadı", exc_info=True)


async def _refresh_vol() -> None:
    from .twaplive import volumes_cached
    vols = await volumes_cached(None)
    VOL1M.clear()
    for c, v in vols.items():
        if v and v[0]:
            VOL1M[c] = float(v[0]) / 1440


async def drain(cfg, client, notifier, t: float | None = None) -> int:
    """Bekleyen adaylardan en çok MAX_PROBES_TICK tanesi, en eskisi önce; bayat adaylar atılır."""
    from .. import health
    t = float(t or time.time())
    for k in [k for k, c in PENDING.items() if t - int(c["last_ts"]) > CAND_MAX_AGE]:
        PENDING.pop(k, None)
        STATS["dropped"] += 1
    n = 0
    for k in sorted(PENDING, key=lambda k: PENDING[k]["first_ts"])[:MAX_PROBES_TICK]:
        cand = PENDING.pop(k, None)
        if cand is None:
            continue
        await health.beat("unwind")
        await probe(cfg, client, notifier, cand)
        n += 1
    return n


async def loop(cfg, client, notifier) -> None:
    from .. import health
    chat = (getattr(cfg, "unwind_chat_id", "") or "").strip()
    if not getattr(cfg, "unwind_enabled", True) or not chat or client is None:
        # kapalı ya da kanal yok: HL'ye hiç sorgu yok (boş chat ana sohbete düşerdi), yine de nabız
        while True:
            await health.beat("unwind")
            await asyncio.sleep(60)
    try:
        STATE.update(await kv_get(STATE_KV) or {})
    except Exception:                                   # noqa: BLE001
        log.debug("unwind durumu okunamadı", exc_info=True)
    last_vol = 0.0
    last_stats = 0.0
    ON[0] = True
    while True:
        try:
            await health.beat("unwind")
            tm = time.time()
            if tm - last_vol >= VOL_SEC or not VOL1M:
                await _refresh_vol()
                last_vol = tm
                _prune(tm)
            day = time.strftime("%Y-%m-%d", time.gmtime(tm + 3 * 3600))
            if STATS["day"] != day:
                STATS["day"], STATS["sent_day"] = day, 0
            await drain(cfg, client, notifier)
            await poll_episodes(cfg, client, notifier)
            if tm - last_stats >= 60:
                last_stats = tm
                await kv_set(STATS_KV, {**STATS, "flows": len(FLOW), "coins_vol": len(VOL1M), "pending": len(PENDING),
                                        "active": len(STATE.get("episodes") or {}), "ts": now()})
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            STATS["errors"] += 1
            log.exception("unwind turu")
        await asyncio.sleep(TICK)


async def diag_line(cfg) -> str:
    if not getattr(cfg, "unwind_enabled", True):
        return "🧯 boşaltma radarı: KAPALI (UNWIND_ENABLED=0)"
    if not (getattr(cfg, "unwind_chat_id", "") or "").strip():
        return "🧯 boşaltma radarı: UNWIND_CHAT_ID TANIMSIZ — izleme yok (HL'ye sorgu da yok)"
    s = await kv_get(STATS_KV) or {}
    if not s:
        return "🧯 boşaltma radarı: henüz tur yok"
    rej = s.get("rejected") or {}
    return (f"🧯 boşaltma radarı: {s.get('coins_vol', 0)} coinin hacmi biliniyor · aday {s.get('candidates', 0)}"
            f" · doğrulanan {s.get('probes', 0)} · bugün gönderilen {s.get('sent_day', 0)}"
            f" · devam {s.get('updates', 0)} · boşalan {s.get('done', 0)} · açık bölüm {s.get('active', 0)}"
            + (f" · elenen: " + ", ".join(f"{k} {v}" for k, v in sorted(rej.items(), key=lambda kv: -kv[1])[:4])
               if rej else "")
            + (f" · ⚠️ kuyruktan atılan {s['dropped']}" if s.get("dropped") else "")
            + (f" · ⚠️ sorgu hatası {s['probe_err']}" if s.get("probe_err") else "")
            + (f" · ⚠️ hata {s['errors']}" if s.get("errors") else ""))


def active_rows() -> list[tuple[str, dict]]:
    return sorted((STATE.get("episodes") or {}).items(), key=lambda kv: -int(kv[1].get("last") or 0))
