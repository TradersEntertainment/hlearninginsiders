"""🏓 Dar koridor — PROPR coininde fiyat dar bir koridorda kenardan kenara gidip geliyor (ÖLÇÜM, sinyal değil).

Kullanıcı (19.10, PONS ekranı): "bu tarz bir bot istiyorum böyle coinlerde bi tık hacimli olanlar tabi pons gibi
böyle sürekli gitgel yapanlar dar bi alanda %0.5 çok oluyor ... bunu tarayıp böyle bir fırsat olduğunu bildiren bir
bot mu yapsak". Araştırma sonrası (19.10): "dar bant botunu kuralım".

Araştırma (19.10, 131 PROPR coini, 1 dk mum 3,4 gün + 5 dk mum 17 gün; üç bağımsız dedektör, rastgele-yürüyüş
karşılaştırmalı; scratchpad/range/consolidated.md): bu tür koridorlar rastgele fiyatta da aynı sıklıkta çıkıyor;
gerçek zamanlı fark edildikten sonra ortanca ömür ~25 dk; kenardan alım-satımın komisyon öncesi ortalaması ≈ 0
(rastgele fiyatla aynı). Bu yüzden mesaj YALNIZ ölçüleni yazar (kenarlar, genişlik, temas, yaş, eğim, hacim) ve
geçmiş ölçümü tek satırla söyler; "fırsat", yön ya da giriş/çıkış önerisi yok (ölçülü dil).

Kararlar:
  • Dedektör araştırmadaki C ("pivot"): zigzag dönüş eşiği max(%0,25, 3 × son 300 dk'nın medyan gerçek aralığı);
    son ≤300 dk'nın tepe/dip pivotlarından en küçük kareler çizgileri; her kenara ≥ RANGE_MIN_TOUCHES SIRALI temas
    (alt, üst, alt…; genişliğin %15'i içinde), hiçbir pivot çizgiden %15 W taşmaz, hiçbir kapanış kenarın %25 W
    dışında değil, çizgiler paralel (|eğim farkı| × pencere ≤ %30 W), dakikaların ≥ %70'inde işlem var, yaş ≥
    RANGE_MIN_MINUTES, genişlik RANGE_MIN/MAX_WIDTH_PCT. Kırılış: kapanış kenarın %25 W dışında.
  • Evren: PROPR'da listeli + 24s hacim ≥ RANGE_MIN_VOL_USD (araştırmanın F2 süzgeci: günde ~8–11 bildirim).
  • Mumlar botun zaten aldığı WS işlemlerinden (ek HL isteği yok; kollektör kancası `observe`, senkron, G/Ç yok).
    Açılışta ve WS kopukluğundan sonra son BACKFILL_MIN dakika bir kez HL'den çekilir (düşük şerit) — yoksa her
    deploy'dan sonra saatlerce kör kalırdı.
  • Bir koridor bir kez bildirilir; kırılınca (RANGE_BREAK_NOTE) tek "kırıldı" notu. Açık koridorlar kv'de: restart'ta
    aynı koridor ikinci kez gelmez.
  • Kanal yalnız env: RANGE_CHAT_ID. Boşsa mesaj yok (ana sohbete düşmez); ölçüm ve /koridor sürer.

Kör nokta, açıkça: yalnız WS'te dinlenen coinler; WS kopukken mum yok (yeniden bağlanınca HL'den doldurulur);
xyz hisselerinde seans dışı saatler seyrek işlemlidir (%70 işlem şartı çoğunu eler).
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque

from ..db import alert_log, alert_recent, kv_get, kv_set, now

log = logging.getLogger("radar.koridor")

ACTIVE_KV = "koridor_active"
STATS_KV = "koridor_stats"
TICK = 60                     # tarama (sn) — dakika kapanınca
KEEP = 720                    # coin başına tutulan 1 dk mum
BACKFILL_MIN = 600            # açılışta / kopukluktan sonra HL'den çekilen dakika
UNIVERSE_SEC = 600            # evren (PROPR ∩ hacim) tazeleme
MAX_GAP_FILL = 30             # bundan uzun işlemsiz boşluk doldurulmaz (WS kopukluğu sayılır → yeniden doldurma)
ACTIVE_MAX_SEC = 12 * 3600    # kırılmadan bu kadar süren koridor sessizce düşer
HIST_LINE = ("📏 Geçmiş ölçüm (16–19.10, bu kural): koridorların %32'si 60 dk sonra sağlamdı; kenardan alım-satımın"
             " komisyon öncesi ortalaması ≈ 0 (rastgele fiyatla aynı).")

P_BASE = dict(zz_floor=0.0025, zz_tr_mult=3.0, tr_win=300, max_window=300, touch_tol=0.15, parallel_tol=0.30,
              act_min=0.70, brk=0.25)

BARS: dict[str, deque] = {}        # coin → deque([dk_ts, o, h, l, c, n])
CUR: dict[str, list] = {}          # coin → açık dakika
ELIGIBLE: set[str] = set()
ACTIVE: dict[str, dict] = {}       # coin → bildirilen koridor
STATS: dict = {"errors": 0, "trades": 0, "scans": 0, "found": 0, "sent": 0, "broken": 0, "backfill": 0,
               "backfill_err": 0, "skipped_nochat": 0, "last_scan": 0, "day": "", "sent_day": 0}


def params(cfg) -> dict:
    p = dict(P_BASE)
    p["min_touch"] = int(getattr(cfg, "range_min_touches", 4) or 4)
    p["min_age"] = int(getattr(cfg, "range_min_minutes", 60) or 60)
    p["wmin"] = float(getattr(cfg, "range_min_width_pct", 0.7) or 0.7) / 100
    p["wmax"] = float(getattr(cfg, "range_max_width_pct", 2.0) or 2.0) / 100
    return p


# ---------------- sıcak yol: kollektör kancası ----------------

def observe(coin: str, px: float, ts: int) -> None:
    """Her WS işlemi (kollektör). Yalnız evrendeki coinde 1 dk mum kurar. Senkron, G/Ç yok."""
    if coin not in ELIGIBLE or not (px > 0):
        return
    m = int(ts) // 60 * 60
    cur = CUR.get(coin)
    if cur is not None and cur[0] == m:
        if px > cur[2]:
            cur[2] = px
        if px < cur[3]:
            cur[3] = px
        cur[4] = px
        cur[5] += 1
    elif cur is None or m > cur[0]:
        if cur is not None:
            _append(coin, cur)
        CUR[coin] = [m, px, px, px, px, 1]
    STATS["trades"] += 1


def _append(coin: str, bar: list) -> None:
    dq = BARS.get(coin)
    if dq is None:
        dq = BARS[coin] = deque(maxlen=KEEP)
    if dq and bar[0] <= dq[-1][0]:
        return
    if dq:
        gap = (bar[0] - dq[-1][0]) // 60 - 1
        if gap > MAX_GAP_FILL:
            dq.clear()                                  # uzun boşluk: eski mumlar bu koridora bağlanmaz
        else:
            c = dq[-1][4]
            for k in range(1, gap + 1):
                dq.append([dq[-1][0] + 60, c, c, c, c, 0])
    dq.append(list(bar))


def close_minutes(t: int | None = None) -> None:
    """Biten dakikaları kapat: açık mumu ekle, işlemsiz dakikaları düz (n=0) mumla doldur."""
    t = int(t or time.time())
    m_now = t // 60 * 60
    for coin in list(ELIGIBLE):
        cur = CUR.get(coin)
        if cur is not None and cur[0] < m_now:
            _append(coin, cur)
            CUR.pop(coin, None)
        dq = BARS.get(coin)
        if dq and (m_now - 60) > dq[-1][0] and (m_now - 60 - dq[-1][0]) // 60 <= MAX_GAP_FILL:
            _append(coin, [m_now - 60, dq[-1][4], dq[-1][4], dq[-1][4], dq[-1][4], 0])


# ---------------- dedektör (araştırmanın C tasarımı, yalnız son mumda) ----------------

def _thresholds(h, l, c, P) -> list[float]:
    """Zigzag eşiği her mum için: max(taban, kat × son tr_win mumun medyan gerçek aralığı) — yalnız geçmiş."""
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view
    h, l, c = np.asarray(h, float), np.asarray(l, float), np.asarray(c, float)
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = (np.maximum(h, pc) - np.minimum(l, pc)) / pc
    w, n = P["tr_win"], len(tr)
    med = np.full(n, np.nan)
    if n >= w:
        med[w - 1:] = np.median(sliding_window_view(tr, w), axis=1)
    for i in range(min(n, w - 1)):                     # ısınma: en az 30 mum
        if i + 1 >= min(30, w):
            med[i] = np.median(tr[:i + 1])
    thr = np.where(np.isnan(med), P["zz_floor"], np.fmax(P["zz_floor"], P["zz_tr_mult"] * med))
    return thr.tolist()


def _zigzag(lh, ll, thr):
    N = len(lh)
    pidx, pval, ptyp, pconf = [], [], [], []
    if N < 2:
        return pidx, pval, ptyp, pconf
    hi, hi_i, lo, lo_i = lh[0], 0, ll[0], 0
    d, ext, ext_i, t = 0, None, None, 1
    while t < N and d == 0:
        if lh[t] > hi:
            hi, hi_i = lh[t], t
        if ll[t] < lo:
            lo, lo_i = ll[t], t
        if hi - lo >= thr[t]:
            if hi_i < lo_i:
                pidx.append(hi_i); pval.append(hi); ptyp.append(1); pconf.append(t)         # noqa: E702
                d, ext, ext_i = -1, lo, lo_i
            else:
                pidx.append(lo_i); pval.append(lo); ptyp.append(-1); pconf.append(t)        # noqa: E702
                d, ext, ext_i = 1, hi, hi_i
            if pconf[-1] <= pidx[-1]:
                pidx.pop(); pval.pop(); ptyp.pop(); pconf.pop()                               # noqa: E702
        t += 1
    for t in range(t, N):
        if d == 1:
            if lh[t] > ext:
                ext, ext_i = lh[t], t
            elif ext - ll[t] >= thr[t]:
                pidx.append(ext_i); pval.append(ext); ptyp.append(1); pconf.append(t)       # noqa: E702
                d, ext, ext_i = -1, ll[t], t
        else:
            if ll[t] < ext:
                ext, ext_i = ll[t], t
            elif lh[t] - ext >= thr[t]:
                pidx.append(ext_i); pval.append(ext); ptyp.append(-1); pconf.append(t)      # noqa: E702
                d, ext, ext_i = 1, lh[t], t
    return pidx, pval, ptyp, pconf


def _ls(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return my - b * mx, b


def _fit(pidx, pval, ptyp, s, e, tref, P):
    tol = P["touch_tol"]
    xs = [pidx[k] - tref for k in range(s, e + 1)]
    ys, ty = pval[s:e + 1], ptyp[s:e + 1]
    Hx = [x for x, q in zip(xs, ty) if q == 1]
    Hy = [y for y, q in zip(ys, ty) if q == 1]
    Lx = [x for x, q in zip(xs, ty) if q == -1]
    Ly = [y for y, q in zip(ys, ty) if q == -1]
    if len(Hx) < P["min_touch"] or len(Lx) < P["min_touch"]:
        return None
    fu, fl = _ls(Hx, Hy), _ls(Lx, Ly)
    if fu is None or fl is None:
        return None
    (au, bu), (al, bl) = fu, fl
    W = au - al
    if W <= 0:
        return None
    kH = [i for i in range(len(Hx)) if Hy[i] - (au + bu * Hx[i]) >= -tol * W]
    kL = [i for i in range(len(Lx)) if Ly[i] - (al + bl * Lx[i]) <= tol * W]
    if len(kH) < P["min_touch"] or len(kL) < P["min_touch"]:
        return None
    if len(kH) < len(Hx):
        fu = _ls([Hx[i] for i in kH], [Hy[i] for i in kH])
        if fu is None:
            return None
        au, bu = fu
    if len(kL) < len(Lx):
        fl = _ls([Lx[i] for i in kL], [Ly[i] for i in kL])
        if fl is None:
            return None
        al, bl = fl
    W = au - al
    if W <= 0:
        return None
    seq = []
    for x, y, q in zip(xs, ys, ty):
        if q == 1:
            r = y - (au + bu * x)
            if r > tol * W:
                return None
            seq.append("U" if r >= -tol * W else None)
        else:
            r = y - (al + bl * x)
            if r < -tol * W:
                return None
            seq.append("L" if r <= tol * W else None)
    if seq[0] is None:
        return None
    runs: list[str] = []
    for q in seq:
        if q is not None and (not runs or runs[-1] != q):
            runs.append(q)
    nU, nL = runs.count("U"), runs.count("L")
    if nU < P["min_touch"] or nL < P["min_touch"]:
        return None
    return dict(au=au, bu=bu, al=al, bl=bl, nU=nU, nL=nL, start=pidx[s], npiv=e - s + 1)


def detect(bars, P) -> dict | None:
    """Son mumda (t = N−1) koridor var mı? Yalnız geçmiş mumlar kullanılır. Dönüş: kenarlar (fiyat), genişlik, temas,
    yaş, eğim — ya da None."""
    N = len(bars)
    if N < P["min_age"] + 10:
        return None
    o = [b[1] for b in bars]
    h = [b[2] for b in bars]
    lo_ = [b[3] for b in bars]
    c = [b[4] for b in bars]
    if min(lo_) <= 0:
        return None
    thr = _thresholds(h, lo_, c, P)
    lh, ll, lc = [math.log(x) for x in h], [math.log(x) for x in lo_], [math.log(x) for x in c]
    pidx, pval, ptyp, pconf = _zigzag(lh, ll, thr)
    E = len(pidx)
    if E < 6:
        return None
    t = N - 1
    e = E - 1
    c_e = pconf[e]
    act = [1 if b[5] > 0 else 0 for b in bars]
    best = None
    s = e - 5
    while s >= 0 and pidx[s] >= t - P["max_window"]:
        cand = _fit(pidx, pval, ptyp, s, e, c_e, P)
        s -= 1
        if cand is None:
            continue
        st = cand["start"]
        if t - st < P["min_age"] or t > st + P["max_window"]:
            continue
        dx = t - c_e
        Wt = (cand["au"] - cand["al"]) + (cand["bu"] - cand["bl"]) * dx
        if not (P["wmin"] <= Wt <= P["wmax"]):
            continue
        if abs(cand["bu"] - cand["bl"]) * (t - st) > P["parallel_tol"] * Wt:
            continue
        if sum(act[st:t + 1]) / (t - st + 1) < P["act_min"]:
            continue
        bad = False
        for j in range(st, t + 1):
            uj = cand["au"] + cand["bu"] * (j - c_e)
            lj = cand["al"] + cand["bl"] * (j - c_e)
            wj = uj - lj
            if lc[j] > uj + P["brk"] * wj or lc[j] < lj - P["brk"] * wj:
                bad = True
                break
        if bad:
            continue
        if best is None or cand["npiv"] > best[0]["npiv"]:
            best = (cand, Wt)
    if best is None:
        return None
    cand, _ = best
    u0 = cand["au"] + cand["bu"] * (t - c_e)
    l0 = cand["al"] + cand["bl"] * (t - c_e)
    slope = 0.5 * (cand["bu"] + cand["bl"])          # log / dk
    st = cand["start"]
    return {"t0": int(bars[t][0]), "start_ts": int(bars[st][0]), "u0": u0, "l0": l0, "W": u0 - l0, "slope": slope,
            "upper": math.exp(u0), "lower": math.exp(l0), "close": c[t], "open": o[t], "nU": cand["nU"],
            "nL": cand["nL"], "age_min": t - st,
            "act_pct": round(100 * sum(act[st:t + 1]) / (t - st + 1))}


def broke(cor: dict, bar: list, P) -> str | None:
    """Kapanmış mumda kırılış var mı ('up' / 'down')? Kenarlar bildirim anındaki eğimle uzatılır, genişlik sabit."""
    dx = (int(bar[0]) - int(cor["t0"])) / 60
    if dx <= 0 or not (bar[4] > 0):
        return None
    u = cor["u0"] + cor["slope"] * dx
    lo = cor["l0"] + cor["slope"] * dx
    lc = math.log(bar[4])
    if lc > u + P["brk"] * cor["W"]:
        return "up"
    if lc < lo - P["brk"] * cor["W"]:
        return "down"
    return None


# ---------------- döngü ----------------

async def _refresh_universe(cfg, collector) -> None:
    from .. import propr
    from .twaplive import volumes_cached
    vols = await volumes_cached(None)
    vmin = float(getattr(cfg, "range_min_vol_usd", 5e6) or 0)
    listened = set(getattr(collector, "valid_coins", None) or [])
    new = {c for c in listened if propr.is_listed(c) and (vols.get(c) or (0,))[0] >= vmin}
    for c in ELIGIBLE - new:
        BARS.pop(c, None)
        CUR.pop(c, None)
    ELIGIBLE.clear()
    ELIGIBLE.update(new)


async def _backfill(client, coins, cfg) -> None:
    """Son BACKFILL_MIN dakikayı HL'den çek (düşük şerit, sırayla). WS'ten gelen açık dakika korunur."""
    from ..hl.client import PRIORITY
    PRIORITY.set("low")
    for coin in coins:
        if coin not in ELIGIBLE:
            continue
        end = int(time.time()) // 60 * 60
        try:
            raw = await client.candles(coin, "1m", (end - BACKFILL_MIN * 60) * 1000, end * 1000 - 1)
            rows = []
            for k in raw or []:
                rows.append([int(k["t"]) // 1000, float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]),
                             int(k.get("n") or 0)])
            rows = [r for r in rows if r[0] < end]
            dq = deque(maxlen=KEEP)
            BARS[coin] = dq
            for r in sorted(rows):
                _append(coin, r)
            STATS["backfill"] += 1
        except Exception as e:                              # noqa: BLE001
            STATS["backfill_err"] += 1
            log.debug("koridor geri doldurma %s: %s", coin, e)
        await asyncio.sleep(1.0)


async def _save_active() -> None:
    try:
        await kv_set(ACTIVE_KV, ACTIVE)
    except Exception:                                       # noqa: BLE001
        log.debug("koridor_active yazılamadı", exc_info=True)


async def scan(cfg, notifier, P=None, t: int | None = None) -> list[str]:
    """Bir tur: dakikaları kapat, açık koridorlarda kırılış, diğerlerinde yeni koridor. Dönüş: gönderilen metinler."""
    from ..telegram import format as fmt
    from .twaplive import volumes_cached
    P = P or params(cfg)
    t = int(t or time.time())
    close_minutes(t)
    STATS["scans"] += 1
    STATS["last_scan"] = t
    day = time.strftime("%Y-%m-%d", time.gmtime(t + 3 * 3600))
    if STATS["day"] != day:
        STATS["day"], STATS["sent_day"] = day, 0
    chat = (getattr(cfg, "range_chat_id", "") or "").strip()
    out: list[str] = []
    changed = False
    vols = None
    # tespit saf CPU (~50 coin × 720 mum): olay döngüsünü tıkamasın diye ayrı iş parçacığında, mumların kopyasıyla
    snap = {c: list(BARS[c]) for c in ELIGIBLE if c not in ACTIVE and BARS.get(c)}

    def _detect_all():
        res = {}
        for c, bars in snap.items():
            try:
                res[c] = detect(bars, P)
            except Exception:                               # noqa: BLE001
                STATS["errors"] += 1
                log.debug("koridor tespit %s", c, exc_info=True)
        return res
    finds = await asyncio.to_thread(_detect_all) if snap else {}
    for coin in [c for c, v in ACTIVE.items() if c not in ELIGIBLE and t - int(v.get("t0") or 0) > ACTIVE_MAX_SEC]:
        ACTIVE.pop(coin, None)                          # evrenden çıkmış ve bayatlamış: /koridor'da kalmasın
        changed = True
    for coin in sorted(ELIGIBLE):
        dq = BARS.get(coin)
        if not dq:
            continue
        cor = ACTIVE.get(coin)
        if cor is not None:
            last = dq[-1]
            why = broke(cor, last, P) if last[0] > int(cor.get("checked", cor["t0"])) else None
            cor["checked"] = int(last[0])
            changed = True
            if why:
                ACTIVE.pop(coin, None)
                STATS["broken"] += 1
                if cor.get("sent") and chat and getattr(cfg, "range_break_note", True):
                    txt = fmt.koridor_break(coin, cor, why, last)
                    await notifier.send("koridor", txt, key=f"koridorx:{coin}:{cor['t0']}", chat_id=chat,
                                        coin=coin, public=False)
                    out.append(txt)
            elif t - int(cor["t0"]) > ACTIVE_MAX_SEC:
                ACTIVE.pop(coin, None)
            continue
        found = finds.get(coin)
        if not found:
            continue
        STATS["found"] += 1
        key = f"{coin}:{found['start_ts']}"
        if await alert_recent("koridor", key, 6 * 3600):
            found["sent"] = False
            ACTIVE[coin] = {**found, "checked": found["t0"]}
            changed = True
            continue
        if vols is None:
            vols = await volumes_cached(None)
        vol = (vols.get(coin) or (None,))[0]
        txt = fmt.koridor_alert(coin, found, vol)
        sent = False
        if chat:
            sent = await notifier.send("koridor", txt, key=f"koridor:{key}", chat_id=chat, coin=coin, public=False)
            if sent:
                STATS["sent"] += 1
                STATS["sent_day"] += 1
            out.append(txt)
        else:
            STATS["skipped_nochat"] += 1
        await alert_log("koridor", key, txt)
        ACTIVE[coin] = {**found, "sent": bool(sent), "checked": found["t0"]}
        changed = True
    if changed:
        await _save_active()
    return out


async def loop(cfg, client, notifier, collector) -> None:
    from .. import health
    if not getattr(cfg, "range_enabled", True):
        return
    try:
        ACTIVE.update(await kv_get(ACTIVE_KV) or {})
    except Exception:                                       # noqa: BLE001
        log.debug("koridor_active okunamadı", exc_info=True)
    P = params(cfg)
    last_uni = 0.0
    seen_conn = None
    filled: set[str] = set()
    while True:
        try:
            await health.beat("koridor")
            tm = time.time()
            if tm - last_uni >= UNIVERSE_SEC:
                await _refresh_universe(cfg, collector)
                last_uni = tm
            conn = getattr(collector, "connected_since", None) if getattr(collector, "connected", False) else None
            if conn != seen_conn:                           # (yeniden) bağlandı: aradaki dakikalar HL'den
                if conn is not None:
                    filled.clear()
                seen_conn = conn
            todo = [c for c in sorted(ELIGIBLE) if c not in filled]
            if todo and client is not None and conn is not None:
                await _backfill(client, todo, cfg)
                filled.update(todo)
            if conn is not None:
                await scan(cfg, notifier, P)
            try:
                await kv_set(STATS_KV, {**STATS, "coins": len(ELIGIBLE), "ready": ready_count(P),
                                        "active": len(ACTIVE), "ts": now()})
            except Exception:                               # noqa: BLE001
                pass
        except asyncio.CancelledError:
            raise
        except Exception:                                   # noqa: BLE001
            STATS["errors"] += 1
            log.exception("koridor turu")
        await asyncio.sleep(TICK - time.time() % TICK + 2)


def ready_count(P) -> int:
    return sum(1 for c in ELIGIBLE if len(BARS.get(c) or ()) >= P["min_age"] + 10)


def active_rows() -> list[tuple[str, dict]]:
    return sorted(ACTIVE.items(), key=lambda kv: -int(kv[1].get("t0") or 0))


async def diag_line(cfg) -> str:
    s = await kv_get(STATS_KV) or {}
    chat = (getattr(cfg, "range_chat_id", "") or "").strip()
    if not getattr(cfg, "range_enabled", True):
        return "🏓 dar koridor: KAPALI (RANGE_ENABLED=0)"
    if not s:
        return "🏓 dar koridor: henüz tur yok (açılıştan sonra ~1 dk)"
    return (f"🏓 dar koridor: {s.get('coins', 0)} coin izleniyor (PROPR, 24s hacim ≥"
            f" ${float(getattr(cfg, 'range_min_vol_usd', 5e6)) / 1e6:g}M) · mumu yeterli {s.get('ready', 0)}"
            f" · açık koridor {s.get('active', 0)} · bugün gönderilen {s.get('sent_day', 0)}"
            f" · toplam bulunan {s.get('found', 0)} / kırılan {s.get('broken', 0)}"
            + (f" · ⚠️ hata {s['errors']}" if s.get("errors") else "")
            + (f" · geri doldurma {s.get('backfill', 0)}" + (f" ({s['backfill_err']} okunamadı)" if s.get("backfill_err") else ""))
            + ("" if chat else " · ⚠️ RANGE_CHAT_ID TANIMSIZ (mesaj yok)"))
