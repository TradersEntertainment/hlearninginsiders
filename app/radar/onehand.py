"""👑 Tek el — az işlem gören bir hisse perp'inde TEK cüzdan kısa sürede hacmin çoğunu yapıp fiyatı oynatınca bildirim
(ölçüm; tahmin değil).

Kullanıcı (22.10, CRWD ekranı): "abd piyasası kapalıyken mesela crwd longladılar gerçek piyasaya hedge döndüğü anda geri
düştü bunu takip edebilir miyiz … haftasonu haricinde de yapabilir miyiz hacimsiz hisselerde crwd gibi yani googl da
falan çalışmaz … o hacimli dakikada ne olduysa tespit edip şu balina tek başına kaldırdı diye bildirim geçmek" ·
"büyükler kapatıyor kanalına at bu bildirimleri yakalarsan".

Vaka (22.10, canlı HL, yeniden üretildi): xyz:CRWD 16:19–16:22 TSİ dakikada $0 hacim; 16:23'te 0x30af…393e dakikada 2
piyasa emriyle almaya başladı — 16:23 hacmin %94'ü ($261K / $278K), 16:24 %60, 16:25 %82, 16:26 %80, 16:27 %81, 16:28
%53; ~12 dk'da $2,68M LONG (giriş $245,0). Fiyat 243,57 → 246,38 (+%1,15), sonra 16:30 ABD açılışında 241,12'ye indi —
o hâlâ alırken (düşüşü başkası yaptı). HL TWAP emri yok.

Kararlar:
  • Tetik canlı WS akışından (ek istek yok), yalnız hisse perp'leri (`assets.klass == "hisse"`; endeks/emtia/kripto yok).
    Coin başına ONEHAND_WINDOW_SEC (180) kayan pencere. Üçü birden:
      (1) pencere hacmi ≥ max(ONEHAND_MIN_USD $250K, ONEHAND_RATE_X (10) × coinin ortalama dakika hacmi × pencere dk)
          — büyük hisselerde (GOOGL, NVDA) bu taban kendiliğinden ulaşılmaz olur;
      (2) TEK adres o yönde piyasadan (taker) ≥ ONEHAND_WHALE_MIN_USD ($200K) VE pencere hacminin ≥ ONEHAND_SHARE_PCT (%60);
      (3) fiyat o yönde ≥ ONEHAND_MOVE_PCT (%0,5) — pencereden önceki son işlemden şimdiye.
  • Doğrulama (2 istek): adresin pencere işlemleri (açıyor / kapatıyor, emir sayısı) ve o dex'teki pozisyonu. Kendi
    likidasyonuysa yazılmaz.
  • Mesaj UNWIND_CHAT_ID'ye ("büyükler kapatıyor" kanalı); ONEHAND_CHAT_ID verilirse oraya. İkisi de boşsa izleme de
    sorgu da yok (ana sohbete düşmez). Aynı coin+adres+yön ONEHAND_COOLDOWN (2 sa) içinde bir kez.
  • ONEHAND_FOLLOW_MIN (30) dk sonra tek ölçüm notu: fiyat (bildirim fiyatına göre), aradaki tepe/dip, cüzdan bildirimden
    sonra aynı yönde / ters yönde piyasadan ne yaptı. "Geri düşer" gibi tahmin yazılmaz; sonuçlar /tani'de sayılır
    (kaç olayda 30 dk sonra fiyat tetik yönünün tersine geçti).

Kör nokta, açıkça: yalnız WS'te dinlenen hisse perp'leri; limit emirle (maker) yapılan birikim tetiklemez; aynı kişinin
iki cüzdanı iki ayrı el sayılır; coin hacmi bilinmiyorsa (yeni liste) coin tetiklemez.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime

from ..db import alert_log, kv_get, kv_set, now

log = logging.getLogger("radar.onehand")

STATE_KV = "onehand_state"
STATS_KV = "onehand_stats"
TICK = 2
VOL_SEC = 300
MAX_PROBES_TICK = 3
CAND_MAX_AGE = 120
PROBE_RETRY = 60
LOG_MAX = 300                  # son olayların ölçüm kaydı (/tani sayımı)

ON = [False]
WIN: dict[str, deque] = {}     # coin → deque[[ts, $, px, taker, 'B'|'S']]
TOT: dict[str, float] = {}     # coin → penceredeki toplam $
BY: dict[str, dict] = {}       # coin → {(taker, yön): $}
REF: dict[str, float] = {}     # coin → pencereden çıkan son işlemin fiyatı (hareketin çıpası)
LAST: dict[str, float] = {}    # coin → son işlem fiyatı
VOL1M: dict[str, float] = {}
PENDING: dict[tuple, dict] = {}
COOL: dict[tuple, float] = {}
STATE: dict = {"follow": [], "log": []}
STATS: dict = {"errors": 0, "candidates": 0, "probes": 0, "probe_err": 0, "sent": 0, "followed": 0, "rejected": {},
               "dropped": 0}


def _cfg(cfg, name, default):
    v = getattr(cfg, name, default)
    return default if v is None else v


def chat(cfg) -> str:
    return ((getattr(cfg, "onehand_chat_id", "") or "").strip()
            or (getattr(cfg, "unwind_chat_id", "") or "").strip())


def _is_stock(coin: str) -> bool:
    from .. import assets
    try:
        return ":" in coin and assets.klass(coin) == "hisse"
    except Exception:                                   # noqa: BLE001
        return False


def _hl_side(s: str) -> str:
    """İç yön 'B'/'S' → HL işlem kaydındaki 'B'/'A' (satış HL'de 'A'; inceleme 22.10: satış adayı hiç doğrulanmıyordu)."""
    return "A" if s == "S" else s


def floor_usd(cfg, coin: str) -> float | None:
    v1 = VOL1M.get(coin)
    if not v1:
        return None
    win_min = int(_cfg(cfg, "onehand_window_sec", 180)) / 60
    return max(float(_cfg(cfg, "onehand_min_usd", 250_000)), float(_cfg(cfg, "onehand_rate_x", 10)) * v1 * win_min)


# ---------------- sıcak yol: kollektör kancası ----------------

def observe(cfg, coin: str, aggr: str, buyer: str, seller: str, px: float, sz: float, ts: int) -> None:
    """Her WS işlemi (yeniden yollanan hariç). Senkron, G/Ç yok."""
    if not ON[0] or aggr not in ("A", "B") or not _is_stock(coin):
        return
    taker, side = (buyer, "B") if aggr == "B" else (seller, "S")
    usd = float(px) * float(sz)
    ts = int(ts)
    dq = WIN.setdefault(coin, deque())
    if not dq and coin in LAST:
        REF[coin] = LAST[coin]                          # boş / budanmış pencere: çıpa pencereden önceki son işlem
    by = BY.setdefault(coin, {})
    dq.append([ts, usd, float(px), taker, side])
    TOT[coin] = TOT.get(coin, 0.0) + usd
    if taker:
        by[(taker, side)] = by.get((taker, side), 0.0) + usd
    win = int(_cfg(cfg, "onehand_window_sec", 180))
    while dq and dq[0][0] < ts - win:
        o = dq.popleft()
        REF[coin] = o[2]
        TOT[coin] -= o[1]
        if o[3]:
            k = (o[3], o[4])
            left = by.get(k, 0.0) - o[1]
            if left > 1e-6:
                by[k] = left
            else:
                by.pop(k, None)
    LAST[coin] = float(px)
    if not taker:
        return
    thr = floor_usd(cfg, coin)
    tot = TOT.get(coin, 0.0)
    if thr is None or tot < thr:
        return
    mine = by.get((taker, side), 0.0)
    share = mine / tot if tot else 0.0
    if mine < float(_cfg(cfg, "onehand_whale_min_usd", 200_000)) or share * 100 < float(_cfg(cfg, "onehand_share_pct", 60)):
        return
    ref = REF.get(coin) or dq[0][2]
    move = (float(px) / ref - 1) * 100 if ref else 0.0
    if (move if side == "B" else -move) < float(_cfg(cfg, "onehand_move_pct", 0.5)):
        return
    key = (coin, taker, side)
    if time.time() < COOL.get(key, 0):
        return
    if key not in PENDING:
        if len(PENDING) >= 100:
            STATS["dropped"] += 1
            return
        STATS["candidates"] += 1
    first = next((x[0] for x in dq if x[3] == taker and x[4] == side), dq[0][0])
    PENDING[key] = {"coin": coin, "addr": taker, "side": side, "mine": mine, "tot": tot, "share": share,
                    "move": move, "ref": ref, "px": float(px), "first_ts": first, "win_ts": dq[0][0], "last_ts": ts,
                    "thr": thr, "v1": VOL1M.get(coin)}


def _prune(t: float) -> None:
    win = 900
    for c in [c for c, dq in WIN.items() if not dq or dq[-1][0] < t - win]:
        WIN.pop(c, None)
        TOT.pop(c, None)
        BY.pop(c, None)
        if c in LAST:
            REF[c] = LAST[c]                            # saatler önceki çıpa kalmasın (inceleme 22.10)
    for k in [k for k, v in COOL.items() if v < t]:
        COOL.pop(k, None)


# ---------------- doğrulama ----------------

def judge_fills(cand: dict, fills: list) -> dict:
    """Adresin pencere işlemleri: piyasadan mı, açıyor mu kapatıyor mu, kendi likidasyonu mu."""
    coin, side, addr = cand["coin"], cand["side"], str(cand["addr"]).lower()
    since = (int(cand["win_ts"]) - 5) * 1000
    rows = [f for f in fills or [] if f.get("coin") == coin and int(f.get("time") or 0) >= since
            and str(f.get("side")) == _hl_side(side) and f.get("crossed", True)]
    if not rows:
        return {"ok": False, "why": "işlem doğrulanamadı"}
    if any(str((r.get("liquidation") or {}).get("liquidatedUser") or "").lower() == addr for r in rows):
        return {"ok": False, "why": "likidasyon"}
    dirs: dict[str, float] = {}
    for r in rows:
        d = str(r.get("dir") or "")
        dirs[d] = dirs.get(d, 0.0) + abs(float(r.get("sz") or 0)) * float(r.get("px") or 0)
    main = max(dirs, key=dirs.get)
    return {"ok": True, "why": "", "dir": main, "orders": len({r.get("oid") for r in rows}),
            "usd": sum(dirs.values())}


def _position(state, coin: str) -> dict | None:
    for it in (state or {}).get("assetPositions") or []:
        p = (it or {}).get("position") or {}
        if p.get("coin") != coin:
            continue
        try:
            szi = float(p.get("szi") or 0)
        except (TypeError, ValueError):
            return None
        if not szi:
            return None
        liq = p.get("liquidationPx")
        return {"side": "LONG" if szi > 0 else "SHORT", "usd": abs(float(p.get("positionValue") or 0)),
                "entry": float(p.get("entryPx") or 0), "liq": float(liq) if liq else None}
    return None


def us_session(ts: int) -> str:
    """ABD hisse seansına göre an: 'ABD seansı açık' · 'ABD açılış öncesi (açılışa N dk)' · 'ABD seansı kapalı'.
    Yarım günlerde (US_EARLY_CLOSE) kapanış 13:00 ET."""
    from datetime import time as dtime
    from .seans import ET, US_EARLY_CLOSE, bounds, is_trading_day
    d = datetime.fromtimestamp(int(ts), ET).date()
    if not is_trading_day(d):
        return "ABD seansı kapalı (hafta sonu / tatil)"
    b = bounds(d)
    end = int(datetime.combine(d, dtime(13, 0), ET).timestamp()) if d in US_EARLY_CLOSE else b["n1"]
    if b["n0"] <= ts < end:
        return "ABD seansı açık"
    if ts < b["n0"]:
        mins = (b["n0"] - int(ts)) // 60
        return f"ABD açılış öncesi (açılışa {mins} dk)" if mins < 180 else "ABD seansı kapalı (açılış öncesi)"
    return "ABD seansı kapalı (kapanış sonrası)"


def session_txt(coin: str, ts: int) -> str:
    """ABD seansı satırı yalnız ABD'de işlem gören hisselerde (Kore / Japonya hisselerinde yanlış piyasa olurdu)."""
    from ..assets import NO_CALENDAR
    from ..tvsymbols import DEFAULT_TV_MAP
    sym = coin.split(":")[-1].upper()
    tv = DEFAULT_TV_MAP.get(sym, "")
    if tv.split(":")[0] in ("KRX", "TSE"):
        return f"ana borsa {tv.split(':')[0]}"
    if sym in NO_CALENDAR:                              # Çin / halka arz öncesi: ABD seansı anlamsız
        return ""
    return us_session(ts)


def _reject(why: str) -> None:
    STATS["rejected"][why] = STATS["rejected"].get(why, 0) + 1


async def probe(cfg, client, notifier, cand: dict) -> str | None:
    from ..telegram import format as fmt
    from .twaplive import volumes_cached
    key = (cand["coin"], cand["addr"], cand["side"])
    # Aday cüzdanın son işleminde yakalanır; o andan sonra gelen başkalarının işlemleri payı düşürür → mesaj PENCERENİN
    # şimdiki hâlini yazar (cüzdan hâlâ pencerede ise).
    mine, tot = BY.get(cand["coin"], {}).get((cand["addr"], cand["side"]), 0.0), TOT.get(cand["coin"], 0.0)
    if mine > 0 and tot > 0:
        cand = {**cand, "mine": mine, "tot": tot, "share": mine / tot, "last_ts": max(int(cand["last_ts"]),
                int((WIN.get(cand["coin"]) or [[cand["last_ts"]]])[-1][0]))}
    COOL[key] = time.time() + int(_cfg(cfg, "onehand_cooldown", 7200))
    STATS["probes"] += 1
    coin = cand["coin"]
    dex = coin.split(":", 1)[0]
    try:
        fills = await client.info({"type": "userFillsByTime", "user": cand["addr"],
                                   "startTime": (int(cand["win_ts"]) - 5) * 1000, "aggregateByTime": True})
        v = judge_fills(cand, fills)
        if not v["ok"]:
            _reject(v["why"])
            return None
        pos = _position(await client.clearinghouse(cand["addr"], dex), coin)
    except Exception as e:                              # noqa: BLE001 — sorgu düştü: kısa sürede yeniden aday olsun
        COOL[key] = time.time() + PROBE_RETRY
        STATS["probe_err"] += 1
        log.debug("onehand sorgu %s: %s", cand["addr"], e)
        return None
    vols = await volumes_cached(None)
    vol24 = (vols.get(coin) or (None,))[0]
    t = int(time.time())
    txt = fmt.onehand_alert(cand, v, pos, vol24, session_txt(coin, t))
    k = f"onehand:{coin}:{cand['addr']}:{cand['side']}:{t // 60}"
    STATS["sent"] += 1
    STATE.setdefault("follow", []).append({"coin": coin, "addr": cand["addr"], "side": cand["side"], "t": t,
                                           "px": cand["px"], "ref": cand["ref"],
                                           "due": t + int(_cfg(cfg, "onehand_follow_min", 30)) * 60})
    await _save()
    c = chat(cfg)
    if c:
        await notifier.send("onehand", txt, priority="high", key=k, chat_id=c, coin=coin, public=False)
    await alert_log("onehand", k, txt)
    return txt


async def follow_up(cfg, client, notifier, t: int | None = None) -> list[str]:
    """Vadesi gelen ölçüm notları: fiyat, aradaki tepe/dip (1 dk mum), cüzdanın bildirimden sonraki piyasa işlemleri."""
    from ..telegram import format as fmt
    t = int(t or time.time())
    out = []
    keep = []
    for f in STATE.get("follow") or []:
        if t < int(f["due"]):
            keep.append(f)
            continue
        if t - int(f["due"]) > 6 * 3600:               # çok eski (uzun kesinti): ölçüm anlamsız, sessizce düş
            continue
        try:
            bars = await client.candles(f["coin"], "1m", int(f["t"]) * 1000, int(f["due"]) * 1000)
            fills = await client.info({"type": "userFillsByTime", "user": f["addr"], "startTime": int(f["t"]) * 1000,
                                       "endTime": int(f["due"]) * 1000, "aggregateByTime": True})
        except Exception:                               # noqa: BLE001 — sonraki turda yeniden
            STATS["probe_err"] += 1
            keep.append(f)
            continue
        hi = max((float(b.get("h") or 0) for b in bars or []), default=None)
        lo = min((float(b.get("l") or 0) for b in bars or []), default=None)
        last = float((bars or [{}])[-1].get("c") or 0) or LAST.get(f["coin"])
        same = opp = 0.0
        for x in fills or []:
            if x.get("coin") != f["coin"] or not x.get("crossed", True):
                continue
            u = abs(float(x.get("sz") or 0)) * float(x.get("px") or 0)
            if str(x.get("side")) == _hl_side(f["side"]):
                same += u
            else:
                opp += u
        m = {**f, "last": last, "hi": hi, "lo": lo, "same": same, "opp": opp}
        txt = fmt.onehand_follow(m)
        STATS["followed"] += 1
        lg = STATE.setdefault("log", [])
        lg.append({"coin": f["coin"], "side": f["side"], "t": f["t"], "px": f["px"], "ref": f["ref"], "last": last})
        del lg[:-LOG_MAX]
        c = chat(cfg)
        # Kullanıcı (22.10, EBAY/MU notları): "şunun bildirimi gereksiz oluyor" → ölçüm yalnız kayda (/tekel, /tani);
        # kanala ancak ONEHAND_FOLLOW_SEND=1 ise gider.
        if c and txt and getattr(cfg, "onehand_follow_send", False):
            await notifier.send("onehand", txt, key=f"onehand=:{f['coin']}:{f['addr']}:{f['t']}", chat_id=c,
                                coin=f["coin"], public=False)
        out.append(txt)
    if len(keep) != len(STATE.get("follow") or []):
        STATE["follow"] = keep
        await _save()
    return out


def outcome_counts(lg: list[dict]) -> dict:
    """Ölçüm kaydı → kaç olayda 30 dk sonra fiyat tetik ÖNCESİ çıpanın öbür yanına geçti (hareket tamamen silindi),
    kaçında bildirim fiyatının gerisine düştü, kaçında ileride kaldı. Tahmin değil, sayım."""
    n = back = below = ahead = 0
    for e in lg or []:
        try:
            px0, ref, last = float(e["px"]), float(e["ref"]), float(e["last"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (px0 and ref and last):
            continue
        n += 1
        up = e.get("side") == "B"
        if (last <= ref) if up else (last >= ref):
            back += 1
        elif (last < px0) if up else (last > px0):
            below += 1
        else:
            ahead += 1
    return {"n": n, "back": back, "below": below, "ahead": ahead}


async def _save() -> None:
    try:
        await kv_set(STATE_KV, STATE)
    except Exception:                                   # noqa: BLE001
        log.debug("onehand durumu yazılamadı", exc_info=True)


async def _refresh_vol() -> None:
    from .twaplive import volumes_cached
    vols = await volumes_cached(None)
    VOL1M.clear()
    for c, v in vols.items():
        if v and v[0]:
            VOL1M[c] = float(v[0]) / 1440


async def drain(cfg, client, notifier, t: float | None = None) -> int:
    t = float(t or time.time())
    for k in [k for k, c in PENDING.items() if t - int(c["last_ts"]) > CAND_MAX_AGE]:
        PENDING.pop(k, None)
        STATS["dropped"] += 1
    n = 0
    for k in sorted(PENDING, key=lambda k: PENDING[k]["first_ts"])[:MAX_PROBES_TICK]:
        cand = PENDING.pop(k, None)
        if cand is not None:
            await probe(cfg, client, notifier, cand)
            n += 1
    return n


async def loop(cfg, client, notifier) -> None:
    from .. import health
    if not getattr(cfg, "onehand_enabled", True) or not chat(cfg) or client is None:
        while True:                                     # kapalı / kanal yok: hiç sorgu yok, yine de nabız
            await health.beat("onehand")
            await asyncio.sleep(60)
    try:
        STATE.update(await kv_get(STATE_KV) or {})
    except Exception:                                   # noqa: BLE001
        log.debug("onehand durumu okunamadı", exc_info=True)
    last_vol = last_stats = 0.0
    ON[0] = True
    while True:
        try:
            await health.beat("onehand")
            tm = time.time()
            if tm - last_vol >= VOL_SEC or not VOL1M:
                await _refresh_vol()
                last_vol = tm
                _prune(tm)
            await drain(cfg, client, notifier)
            await follow_up(cfg, client, notifier)
            if tm - last_stats >= 60:
                last_stats = tm
                await kv_set(STATS_KV, {**STATS, "coins": len(WIN), "pending": len(PENDING),
                                        "follow": len(STATE.get("follow") or []),
                                        "outcome": outcome_counts(STATE.get("log") or []), "ts": now()})
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            STATS["errors"] += 1
            log.exception("onehand turu")
        await asyncio.sleep(TICK)


async def diag_line(cfg) -> str:
    if not getattr(cfg, "onehand_enabled", True):
        return "👑 tek el: KAPALI (ONEHAND_ENABLED=0)"
    if not chat(cfg):
        return "👑 tek el: kanal TANIMSIZ (ONEHAND_CHAT_ID / UNWIND_CHAT_ID) — izleme yok (HL'ye sorgu da yok)"
    s = await kv_get(STATS_KV) or {}
    if not s:
        return "👑 tek el: henüz tur yok"
    oc = s.get("outcome") or {}
    rej = s.get("rejected") or {}
    return (f"👑 tek el: aday {s.get('candidates', 0)} · doğrulanan {s.get('probes', 0)} · gönderilen {s.get('sent', 0)}"
            f" · ölçüm notu {s.get('followed', 0)} (bekleyen {s.get('follow', 0)})"
            + (f" · 30 dk sonra: {oc['n']} olayın {oc['back']}'inde fiyat tetik öncesine döndü, {oc['below']}'inde"
               f" bildirim fiyatının gerisinde, {oc['ahead']}'inde ileride" if oc.get("n") else "")
            + (" · elenen: " + ", ".join(f"{k} {v}" for k, v in sorted(rej.items(), key=lambda kv: -kv[1])[:3])
               if rej else "")
            + (f" · ⚠️ sorgu hatası {s['probe_err']}" if s.get("probe_err") else "")
            + (f" · ⚠️ hata {s['errors']}" if s.get("errors") else ""))
