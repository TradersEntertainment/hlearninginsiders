"""🧯 Sert satış / pozisyon boşaltma — fiyatı oynatacak büyüklükte pozisyonu olan cüzdan onu piyasaya vurarak kapatmaya
başlayınca, aynı cüzdanın HÂLÂ AÇIK öteki pozisyonları (ölçüm; tahmin değil).

Kullanıcı (19.10, 0x0871…e927 "cbrskapatan"): "bir coinin fiyatını etkileyebilecek kadar büyük pozu varsa ve tüm pozları
kapamaya başladıysa bildirim istiyorum yeni kanal" · "adam kapamaya başladı ve yarım saatte çok büyük pozlar kapadı bizim
o yarım saat içinde yakalayıp kapayacağı diğer coinleri olası olarak yakalayacağımız bir şey" · "dünü ayrı bugünü ayrı
yakalamak lazımdı … dün yaptı bugün devam eder gibi bi paterne kayma sakın; mantık: sert satışçının diğer pozları da
kapatabileceği".

Vaka (19.10, scratchpad/unwind/case_checked.md): 20:22–20:58'de 7 long ($10,47M) 80 elle piyasa emriyle (FrontendMarket
reduceOnly; yarım / çeyrek / sabit dilim) kapandı; ilk coin CBRS (24s hacmin %14'ü) 3 dk'da 20 emir; 18.10 20:20'de de
CBRS'nin yarısı piyasaya vuruldu (−%2,8). 21 günde 7 kez bütün defter dakikalar içinde boşaltılmış.

Kararlar:
  • Tetik canlı WS akışından (ek istek yok): TEK adres bir coinde UNWIND_WINDOW_SEC (300) içinde piyasadan (taker)
    ≥ max(UNWIND_MIN_USD $100K, UNWIND_RATE_X (10) × coinin ortalama dakika hacmi) satarsa (short için alırsa) aday.
    Her sert satış kendi başına bir olaydır — "dün başladı bugün sürer" mantığı yok.
  • Doğrulama (1–3 sn): adresin bütün dex'lerdeki pozisyonları + son işlemleri (toplulaştırılmış). Satış KENDİ
    pozisyonunu azaltıyor olmalı ('Close Long/Short', taker, likidasyon değil); azaltılan pozisyon satıştan önce
    "fiyatı etkileyecek" büyüklükte (≥ UNWIND_BIG_USD $250K VE ≥ UNWIND_BIG_MIN (30) dk ortalama hacim); cüzdanda
    ≤ UNWIND_MAX_POSITIONS (15) pozisyon (sürekli ayar yapan sepet defterleri kanalı boğmasın); ≥1 öteki açık pozisyon
    ≥ UNWIND_OTHER_MIN_USD ($100K) — liste olmadan mesajın amacı yok.
  • Mesaj yalnız UNWIND_CHAT_ID (env-only; boşsa hiç sorgu yok, mesaj yok — ana sohbete düşmez). Aynı cüzdan 2 sa
    içinde başka bir coini sert satarsa kısa "devam" mesajı (en çok UNWIND_MAX_UPDATES); defter boşalınca tek kapanış
    özeti. Ölçülü dil: "kapatacak / muhtemelen" yok; "hâlâ açık" listesi büyüklük ve hacme oranıyla.
  • "mm" etiketine güvenilmez (bu cüzdan 24 sa'te 80+ fill kuralıyla mm sayılabiliyordu) — süzgeç pozisyon sayısı.

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
EPISODE_END = 3 * 3600         # bu kadar yeni azaltma yoksa bölüm sessizce kapanır
PROBE_COOL = 900               # aynı adres+coin adayı doğrulandıktan / elendikten sonra bekleme
MIN_FILL_USD = 1_000           # bundan küçük işlem akışa yazılmaz (bellek)
SHOW_OTHERS = 8                # mesajda en çok bu kadar "hâlâ açık"

ON = [False]                   # döngü çalışıyor mu (kapalı / kanalsızken kanca hiçbir şey tutmaz)
FLOW: dict[tuple, deque] = {}  # (adres, coin, 'S'|'B') → deque[(ts, $, px)] — yalnız taker
PENDING: dict[str, dict] = {}  # adres → en büyük aday
COOL: dict[tuple, float] = {}  # (adres, coin) → son aday zamanı
VOL1M: dict[str, float] = {}   # coin → ortalama dakika hacmi ($)
STATE: dict = {"episodes": {}}
STATS: dict = {"errors": 0, "candidates": 0, "probes": 0, "probe_err": 0, "sent": 0, "updates": 0, "done": 0,
               "rejected": {}, "day": "", "sent_day": 0}


def _cfg(cfg, name, default):
    v = getattr(cfg, name, default)
    return default if v is None else v


# ---------------- sıcak yol: kollektör kancası ----------------

def threshold(cfg, coin: str) -> float | None:
    v1 = VOL1M.get(coin)
    if not v1:
        return None
    return max(float(_cfg(cfg, "unwind_min_usd", 100_000)), float(_cfg(cfg, "unwind_rate_x", 10)) * v1)


def observe(cfg, coin: str, aggr: str, buyer: str, seller: str, px: float, sz: float, ts: int) -> None:
    """Her WS işlemi (yeniden yollanan hariç). Yalnız taker tarafı, adres+coin+yön başına kayan pencere. G/Ç yok."""
    if not ON[0]:
        return
    usd = px * sz
    if usd < MIN_FILL_USD or aggr not in ("A", "B"):
        return
    taker, side = (buyer, "B") if aggr == "B" else (seller, "S")
    if not taker:
        return
    key = (taker, coin, side)
    dq = FLOW.get(key)
    if dq is None:
        dq = FLOW[key] = deque()
    dq.append((int(ts), usd, px))
    win = int(_cfg(cfg, "unwind_window_sec", 300))
    while dq and dq[0][0] < int(ts) - win:
        dq.popleft()
    thr = threshold(cfg, coin)
    if thr is None:
        return
    total = sum(x[1] for x in dq)
    if total < thr:
        return
    if time.time() - COOL.get((taker, coin), 0) < PROBE_COOL:
        return
    cur = PENDING.get(taker)
    if cur is None or total > cur["usd"]:
        if cur is None:
            STATS["candidates"] += 1
        PENDING[taker] = {"addr": taker, "coin": coin, "side": side, "usd": total, "first_ts": dq[0][0],
                          "last_ts": int(ts), "px0": dq[0][2], "px1": px, "n": len(dq), "thr": thr}


def _prune(t: float) -> None:
    for key in [k for k, dq in FLOW.items() if not dq or dq[-1][0] < t - 900]:
        FLOW.pop(key, None)
    for key in [k for k, v in COOL.items() if t - v > 6 * 3600]:
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


def reduce_rows(fills, coin: str, side: str, since_ms: int) -> list[dict]:
    """Adayın coininde, pencere içindeki POZİSYON AZALTAN taker satırları (long kapatma = satış 'S')."""
    want = "Close Long" if side == "S" else "Close Short"
    rows = []
    for f in fills or []:
        try:
            if f.get("coin") != coin or int(f.get("time") or 0) < since_ms:
                continue
            if not str(f.get("dir") or "").startswith(want):
                continue
            rows.append(f)
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda f: int(f.get("time") or 0))
    return rows


def judge(cfg, cand: dict, positions: list[dict], fills: list) -> dict:
    """Aday + REST verisi → karar. Dönüş {"ok": bool, "why": str, ...ölçümler}."""
    coin, side = cand["coin"], cand["side"]
    win = int(_cfg(cfg, "unwind_window_sec", 300))
    rows = reduce_rows(fills, coin, side, (cand["first_ts"] - win) * 1000)
    if not rows:
        return {"ok": False, "why": "azaltma değil (açma / ekleme ya da başkasının adına)"}
    if any(r.get("liquidation") for r in rows):
        return {"ok": False, "why": "likidasyon"}
    taker = [r for r in rows if r.get("crossed", True)]
    if not taker:
        return {"ok": False, "why": "piyasadan değil (maker)"}
    first = rows[0]
    px0 = float(first.get("px") or 0)
    before = abs(float(first.get("startPosition") or 0)) * px0
    closed = sum(abs(float(r.get("sz") or 0)) * float(r.get("px") or 0) for r in rows)
    v1 = VOL1M.get(coin) or 0
    big_usd = float(_cfg(cfg, "unwind_big_usd", 250_000))
    big_min = float(_cfg(cfg, "unwind_big_min", 30))
    if before < big_usd or (v1 and before < big_min * v1):
        return {"ok": False, "why": "pozisyon coine göre küçük", "before": before}
    maxp = int(_cfg(cfg, "unwind_max_positions", 15))
    if len(positions) > maxp:
        return {"ok": False, "why": f"sepet defteri ({len(positions)} pozisyon > {maxp})"}
    other_min = float(_cfg(cfg, "unwind_other_min_usd", 100_000))
    others = [p for p in positions if p["coin"] != coin and p["usd"] >= other_min]
    now_pos = next((p for p in positions if p["coin"] == coin), None)
    res = {"ok": True, "why": "", "before": before, "closed": closed, "now": now_pos["usd"] if now_pos else 0.0,
           "pos_side": "LONG" if side == "S" else "SHORT", "orders": len({r.get("oid") for r in rows}),
           "others": others, "positions": positions, "px_first": px0,
           "px_last": float(rows[-1].get("px") or 0), "since": int(first.get("time") or 0) // 1000}
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


async def _fills(client, addr: str, since_ms: int):
    return await client.info({"type": "userFillsByTime", "user": addr, "startTime": since_ms,
                              "aggregateByTime": True})


def _reject(why: str) -> None:
    r = STATS["rejected"]
    k = why.split(" (")[0]
    r[k] = r.get(k, 0) + 1


async def probe(cfg, client, notifier, cand: dict) -> str | None:
    """Adayı doğrula; uygunsa mesajı gönder. Dönüş: gönderilen metin (yoksa None)."""
    from ..telegram import format as fmt
    addr, coin = cand["addr"], cand["coin"]
    COOL[(addr, coin)] = time.time()
    STATS["probes"] += 1
    try:
        states = await client.clearinghouse_all(addr, await _dexes(cfg))
        fills = await _fills(client, addr, (cand["first_ts"] - 3600) * 1000)
    except Exception as e:                              # noqa: BLE001
        STATS["probe_err"] += 1
        log.debug("unwind sorgu %s: %s", addr, e)
        return None
    positions = _positions(states)
    v = judge(cfg, cand, positions, fills)
    if not v["ok"]:
        _reject(v["why"])
        return None
    chat = (getattr(cfg, "unwind_chat_id", "") or "").strip()
    t = int(time.time())
    eps = STATE.setdefault("episodes", {})
    ep = eps.get(addr)
    from .twaplive import volumes_cached
    vols = await volumes_cached(None)
    vol24 = {c: (vols.get(c) or (None,))[0] for c in [coin] + [p["coin"] for p in positions]}
    if ep and t - int(ep.get("last", 0)) < EPISODE_GAP:
        if coin in ep.get("coins", {}) or int(ep.get("updates", 0)) >= int(_cfg(cfg, "unwind_max_updates", 5)):
            ep["last"] = t
            return None
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
        ep = eps[addr] = {"start": t, "last": t, "updates": 0, "coins": {coin: book[coin]}, "book": book,
                          "first_coin": coin, "polled": t}
        txt = fmt.unwind_alert(addr, coin, cand, v, vol24, ep=None)
        kind_key = f"unwind:{addr}:{coin}:{t // 60}"
        STATS["sent"] += 1
        STATS["sent_day"] += 1
    await _save()
    if chat:
        await notifier.send("unwind", txt, priority="high", key=kind_key, chat_id=chat, coin=coin, public=False)
    await alert_log("unwind", kind_key, txt)
    return txt


async def poll_episodes(cfg, client, notifier, t: int | None = None) -> list[str]:
    """Açık bölümler: defter boşaldıysa tek kapanış özeti; uzun süre yeni azaltma yoksa sessizce kapat."""
    from ..telegram import format as fmt
    t = int(t or time.time())
    out = []
    chat = (getattr(cfg, "unwind_chat_id", "") or "").strip()
    eps = STATE.setdefault("episodes", {})
    for addr, ep in list(eps.items()):
        if t - int(ep.get("last", 0)) > EPISODE_END:
            eps.pop(addr, None)
            continue
        if t - int(ep.get("polled", 0)) < POLL_SEC:
            continue
        ep["polled"] = t
        try:
            positions = _positions(await client.clearinghouse_all(addr, await _dexes(cfg)))
        except Exception:                               # noqa: BLE001
            STATS["probe_err"] += 1
            continue
        left = {p["coin"]: p for p in positions}
        book = ep.get("book") or {}
        if book and all(left.get(c, {}).get("usd", 0) <= 0.02 * b["before"] for c, b in book.items()):
            txt = fmt.unwind_done(addr, ep, positions, t)
            eps.pop(addr, None)
            STATS["done"] += 1
            if chat:
                await notifier.send("unwind", txt, priority="high", key=f"unwind=:{addr}:{ep['start']}", chat_id=chat,
                                    public=False)
            out.append(txt)
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
            while PENDING:
                addr, cand = PENDING.popitem()
                await probe(cfg, client, notifier, cand)
            await poll_episodes(cfg, client, notifier)
            if tm - last_stats >= 60:
                last_stats = tm
                await kv_set(STATS_KV, {**STATS, "flows": len(FLOW), "coins_vol": len(VOL1M),
                                        "active": len(STATE.get("episodes") or {}), "ts": now()})
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            STATS["errors"] += 1
            log.exception("unwind turu")
        await asyncio.sleep(TICK)


async def diag_line(cfg) -> str:
    if not getattr(cfg, "unwind_enabled", True):
        return "🧯 sert satış: KAPALI (UNWIND_ENABLED=0)"
    if not (getattr(cfg, "unwind_chat_id", "") or "").strip():
        return "🧯 sert satış: UNWIND_CHAT_ID TANIMSIZ — izleme yok (HL'ye sorgu da yok)"
    s = await kv_get(STATS_KV) or {}
    if not s:
        return "🧯 sert satış: henüz tur yok"
    rej = s.get("rejected") or {}
    return (f"🧯 sert satış: {s.get('coins_vol', 0)} coinin hacmi biliniyor · aday {s.get('candidates', 0)}"
            f" · doğrulanan {s.get('probes', 0)} · bugün gönderilen {s.get('sent_day', 0)}"
            f" · devam {s.get('updates', 0)} · boşalan {s.get('done', 0)} · açık bölüm {s.get('active', 0)}"
            + (f" · elenen: " + ", ".join(f"{k} {v}" for k, v in sorted(rej.items(), key=lambda kv: -kv[1])[:4])
               if rej else "")
            + (f" · ⚠️ sorgu hatası {s['probe_err']}" if s.get("probe_err") else "")
            + (f" · ⚠️ hata {s['errors']}" if s.get("errors") else ""))


def active_rows() -> list[tuple[str, dict]]:
    return sorted((STATE.get("episodes") or {}).items(), key=lambda kv: -int(kv[1].get("last") or 0))
