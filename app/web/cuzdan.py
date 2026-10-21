"""🌐 Cüzdan sayfası verisi (/cuzdan/<adres>/veri.json) ve takipteki cüzdanlar listesi (/takipte).

Kullanıcı (20.10): "bu kanala eklediklerim için websitesi yapar mısın railwaye push edeyim bu geçen sana sorduğum
hypepoztakip sitesini kopyalayabilirsin profillere basınca öyle bi websitesi çıksın".

Kararlar:
  • Görünüm hyptakip'ten (cuzdan.html / takipte.html); veri HL'nin herkese açık defteri, son 24 sa işlemler, bu cüzdanın
    🧯 takipleri ve gönderilmiş takip / 🧯 mesajları (alerts_log). Yalnız 🧯 takipleri (src='unwind'): ana sohbetteki /
    başka kanaldaki /takip mesajları imzalı bağlantıyı alan kanal üyelerine açılmaz.
  • HL yükü radarları geciktirmesin (inceleme 20.10, ölçüldü: sayfa başına ~35 istek/dk, 2–3 açık sekme sıkışık kotada
    radar isteklerini saniyelerce bekletiyordu): adres başına 30 sn önbellek; bütün dex'ler 5 dk'da bir, arada yalnız
    hesabı olan dex'ler; bütün sayfalar toplamı 60 sn'de en çok PAGE_REFRESH_MAX tazeleme ve botun kotasında
    PAGE_MIN_FREE'den az boş varsa tazeleme yok (son veri "gecikmeli" işaretiyle). Dex başına 1 deneme, tazeleme en çok
    STATES_BUDGET sn (tarayıcı 30 sn'de keser). Okunamayan dex → okunanlar + "eksik olabilir".
  • Birleşik hesap / portföy teminatı (userAbstraction): dex'lerin accountValue'su yalnız ayrılan teminattır — hesap
    değeri HL portfolio "day" (twaplive ile aynı kaynak), çekilebilir bilinmiyor (canlı 20.10: 0x05c7…3987 sayfada 20x,
    gerçek ~1,3x).
  • Liste sayfası yalnız veritabanından (HL sorgusu yok): 🧯 takipleri (açık + son 14 gün).
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from collections import deque

from .. import assets, sitelink
from ..db import db, now
from ..propr import is_listed

log = logging.getLogger("web.cuzdan")

FILL_CACHE_SEC = 60
PAGE_CACHE_SEC = 30            # adres başına sayfa verisi (20 sn'lik tazelemenin yarısı önbellekten)
FULL_SCAN_SEC = 300            # bütün dex'ler en çok bu kadar sn'de bir; arada yalnız hesabı olanlar
PAGE_REFRESH_MAX = 6           # bütün sayfalar toplamı: PAGE_WINDOW_SEC'te en çok bu kadar HL tazelemesi
PAGE_WINDOW_SEC = 60
PAGE_MIN_FREE = 40             # botun HL kotasında bundan az boş istek varsa sayfa tazelemez (radarlar önce)
STATES_BUDGET = 12.0           # bir tazeleme en çok bu kadar sn (tarayıcı veri.json'u 30 sn'de keser)
POOLED = ("unifiedAccount", "portfolioMargin")
MODE_CACHE_SEC = 3600
FILL_HOURS = 24
FILL_MAX = 40
ALERT_MAX = 30
KEEP_DAYS = 14
_FILLS: dict[str, tuple[float, list]] = {}
_PAGE: dict[str, dict] = {}    # adres → {ts, states, partial, dexes, full_ts}
_LOCKS: dict[str, asyncio.Lock] = {}
_REFRESH: deque = deque()
_MODES: dict[str, tuple[float, str]] = {}
_POOL: dict[str, tuple[float, float]] = {}
_TRK_KEY = re.compile(r"^(step|flip|closed|checkin|liq|expire|start):(\d+)")


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v


class PageBusy(RuntimeError):
    """Sayfa tazeleme payı ya da botun HL kotası dolu — HL'ye hiç gidilmedi."""


def _may_refresh(client) -> bool:
    t = time.time()
    while _REFRESH and t - _REFRESH[0] >= PAGE_WINDOW_SEC:
        _REFRESH.popleft()
    if len(_REFRESH) >= PAGE_REFRESH_MAX:
        return False
    try:
        if int(client.usage()["free"]) < PAGE_MIN_FREE:
            return False
    except Exception:                                       # noqa: BLE001 — sahte istemci: kota bilinmiyor
        pass
    _REFRESH.append(t)
    return True


def _has_account(st) -> bool:
    st = st or {}
    return bool(st.get("assetPositions")) or (_f((st.get("marginSummary") or {}).get("accountValue")) or 0) > 0


async def _states(cfg, client, addr: str) -> tuple[dict, bool, bool]:
    """Dex defterleri {dex: state} → (defterler, eksik mi, gecikmeli mi). Önbellek / pay / kota kapıları modül
    açıklamasında. Hiçbir dex okunamazsa ve önbellek yoksa hata."""
    from ..radar.unwind import _dexes
    lock = _LOCKS.setdefault(addr, asyncio.Lock())
    async with lock:
        t = time.time()
        e = _PAGE.get(addr)
        if e and t - e["ts"] < PAGE_CACHE_SEC:
            return e["states"], e["partial"], False
        if not _may_refresh(client):
            if e:
                return e["states"], e["partial"], True
            raise PageBusy("HL kotası şu an dolu")
        full = not e or e["partial"] or t - e.get("full_ts", 0) >= FULL_SCAN_SEC
        dexes = await _dexes(cfg) if full else e["dexes"]
        states: dict = {}
        fail = 0
        for d in dexes:
            left = STATES_BUDGET - (time.time() - t)
            if left <= 0.5:
                fail += 1
                continue
            try:
                states[d or "main"] = await asyncio.wait_for(client.clearinghouse(addr, d, retries=1), left)
            except Exception:                               # noqa: BLE001
                fail += 1
        if not states:
            if e:
                return e["states"], e["partial"], True
            raise RuntimeError("hiçbir dex okunamadı")
        keep = [d for d in dexes if not d or _has_account(states.get(d or "main"))] if full else e["dexes"]
        _PAGE[addr] = {"ts": time.time(), "states": states, "partial": fail > 0, "dexes": keep,
                       "full_ts": t if full else e.get("full_ts", t)}
    for a in [a for a, x in _PAGE.items() if time.time() - x["ts"] > 3600]:
        _PAGE.pop(a, None)
    return states, fail > 0, False


async def _mode(client, addr: str) -> str | None:
    """HL hesap kipi (userAbstraction: default / disabled / unifiedAccount / portfolioMargin); okunamazsa None."""
    t = time.time()
    hit = _MODES.get(addr)
    if hit and t - hit[0] < MODE_CACHE_SEC:
        return hit[1]
    try:
        m = await asyncio.wait_for(client.info({"type": "userAbstraction", "user": addr}, retries=1), 6)
    except Exception:                                       # noqa: BLE001
        log.debug("cüzdan sayfası hesap kipi düştü %s", addr, exc_info=True)
        m = None
    if isinstance(m, str) and m:
        _MODES[addr] = (t, m)
        return m
    return hit[1] if hit else None


async def _pooled_value(client, addr: str) -> float | None:
    """Birleşik / portföy teminatlı hesabın değeri: HL portfolio "day" son noktası (twaplive ile aynı kaynak)."""
    from ..radar.twaplive import parse_portfolio_value
    t = time.time()
    hit = _POOL.get(addr)
    if hit and t - hit[0] < PAGE_CACHE_SEC:
        return hit[1]
    try:
        v = parse_portfolio_value(await asyncio.wait_for(client.info({"type": "portfolio", "user": addr}, retries=1), 6))
    except Exception:                                       # noqa: BLE001
        log.debug("cüzdan sayfası portfolio düştü %s", addr, exc_info=True)
        v = None
    if v is not None:
        _POOL[addr] = (t, v)
    return v


async def _fills(client, addr: str) -> tuple[list, bool]:
    t = time.time()
    hit = _FILLS.get(addr)
    if hit and t - hit[0] < FILL_CACHE_SEC:
        return hit[1], False
    try:
        raw = await asyncio.wait_for(client.info({"type": "userFills", "user": addr, "aggregateByTime": True},
                                                 retries=1), 8)
    except Exception:                                       # noqa: BLE001
        log.debug("cüzdan sayfası işlemler düştü %s", addr, exc_info=True)
        return (hit[1] if hit else []), True
    since = (now() - FILL_HOURS * 3600) * 1000
    rows = []
    for x in raw or []:
        try:
            ts = int(x.get("time") or 0)
            if ts < since:
                continue
            px, sz = float(x.get("px") or 0), float(x.get("sz") or 0)
            coin = str(x.get("coin") or "")
            rows.append({"ts": ts // 1000, "coin": coin, "label": assets.label(coin), "dir": str(x.get("dir") or ""),
                         "px": px, "sz": sz, "usd": px * sz, "pnl": _f(x.get("closedPnl")), "fee": _f(x.get("fee")),
                         "taker": bool(x.get("crossed", True)), "spot": bool(assets.is_spot(coin))})
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda r: -r["ts"])
    rows = rows[:FILL_MAX]
    _FILLS[addr] = (t, rows)
    for a in [a for a, (ts, _) in _FILLS.items() if t - ts > 600]:
        _FILLS.pop(a, None)
    return rows, False


async def _trackers(addr: str) -> list[dict]:
    """Yalnız 🧯 takip tuşuyla açılanlar (src='unwind'): sayfa takip kanalının; ana sohbetteki / başka kanaldaki /takip
    mesajları imzalı bağlantıyı alan kanal üyelerine açılmaz (inceleme 20.10)."""
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM trackers WHERE address=? AND src='unwind' AND (active=1 OR created_ts>=?)"
            " ORDER BY created_ts DESC", (addr, now() - KEEP_DAYS * 86400))
        return [dict(r) for r in await cur.fetchall()]


def _cached_mode(addr: str) -> str | None:
    hit = _MODES.get(addr)
    return hit[1] if hit else None


def _chat_label(cfg, chat_id: str) -> str:
    c = (chat_id or "").strip()
    if c and c == (getattr(cfg, "unwind_track_chat_id", "") or "").strip():
        return "takip kanalı"
    return "ana sohbet" if not c else "başka kanal"


def _plain(payload: str) -> str:
    from ..telegram.format import strip_tags
    return html.unescape(strip_tags(payload or "")).strip()


async def _alerts(addr: str, ids: set[int]) -> list[dict]:
    """Bu cüzdana giden takip (👣) ve 🧯 mesajları — yalnız gönderilmiş olanlar (alerts_log 'sent:*')."""
    since = now() - KEEP_DAYS * 86400
    out = []
    async with db() as conn:
        if ids:
            cur = await conn.execute("SELECT key, ts, payload FROM alerts_log WHERE kind='sent:track' AND ts>=?"
                                     " ORDER BY ts DESC LIMIT 3000", (since,))
            for r in await cur.fetchall():
                m = _TRK_KEY.match(r["key"] or "")
                if m and int(m.group(2)) in ids:
                    out.append({"ts": int(r["ts"]), "kind": "takip", "text": _plain(r["payload"])})
        for pre in ("unwind", "unwind+", "unwind="):
            cur = await conn.execute("SELECT ts, payload FROM alerts_log WHERE kind='sent:unwind' AND key GLOB ? AND ts>=?"
                                     " ORDER BY ts DESC LIMIT 50", (f"{pre}:{addr}:*", since))
            out += [{"ts": int(r["ts"]), "kind": "boşaltma", "text": _plain(r["payload"])} for r in await cur.fetchall()]
    out.sort(key=lambda a: -a["ts"])
    return out[:ALERT_MAX]


def _closed_pct(base: float, cur: float | None) -> float | None:
    if not base or cur is None:
        return None
    return round((base - abs(cur)) / base * 100, 1)


async def build(cfg, client, addr: str) -> dict:
    """/cuzdan/<adres>/veri.json — sözleşme: scratchpad site/contract.md (şablon cuzdan.html bunu okur)."""
    addr = addr.lower()
    out = {"address": addr, "ts": now(), "ok": True, "err": "", "partial": False, "account": None,
           "positions": [], "fills": [], "alerts": [], "tracked": []}
    trk = await _trackers(addr)
    active = {t["coin"]: t for t in trk if t.get("active")}
    stale = False
    try:
        states, partial, stale = await _states(cfg, client, addr)
    except PageBusy:
        states, partial = {}, True
        out.update(ok=False, err="Botun Hyperliquid kotası şu an dolu (radarlar önce) — birazdan yeniden denenecek")
    except Exception as e:                                  # noqa: BLE001
        states, partial = {}, True
        out.update(ok=False, err=f"Hyperliquid'e ulaşılamadı ({type(e).__name__}) — birazdan yeniden denenecek")
    if stale and states:
        out["err"] = "Gecikmeli veri: botun Hyperliquid kotası dolu, son okunan defter gösteriliyor"
    acc = {"value": 0.0, "margin_used": 0.0, "withdrawable": None, "ntl": 0.0, "upnl": 0.0, "lev": None, "n": 0}
    live_szi: dict[str, float] = {}
    for key, st in (states or {}).items():
        st = st or {}
        ms = st.get("marginSummary") or {}
        acc["value"] += _f(ms.get("accountValue")) or 0.0
        acc["margin_used"] += _f(ms.get("totalMarginUsed")) or 0.0
        if key == "main":
            acc["withdrawable"] = _f(st.get("withdrawable"))
        for it in st.get("assetPositions") or []:
            p = (it or {}).get("position") or {}
            szi = _f(p.get("szi")) or 0.0
            coin = str(p.get("coin") or "")
            if not szi or not coin:
                continue
            ntl = abs(_f(p.get("positionValue")) or 0.0)
            mark = ntl / abs(szi) if szi else None
            liq = _f(p.get("liquidationPx"))
            lev = p.get("leverage") or {}
            roe = _f(p.get("returnOnEquity"))
            upnl = _f(p.get("unrealizedPnl")) or 0.0
            live_szi[coin] = szi
            t = active.get(coin)
            out["positions"].append({
                "coin": coin, "label": assets.label(coin), "dex": "" if key == "main" else key,
                "side": "long" if szi > 0 else "short", "szi": szi, "ntl": ntl, "entry": _f(p.get("entryPx")),
                "mark": mark, "upnl": upnl, "roe": round(roe * 100, 2) if roe is not None else None,
                "lev": _f(lev.get("value")), "lev_type": str(lev.get("type") or ""), "liq": liq,
                "liq_dist": round(abs(mark - liq) / mark * 100, 2) if (liq and mark) else None,
                # cumFunding.sinceOpen > 0 = ÖDENEN (canlı doğrulandı 20.10: long + pozitif oran → userFunding usdc < 0)
                "margin": _f(p.get("marginUsed")), "funding": _f((p.get("cumFunding") or {}).get("sinceOpen")),
                "propr": bool(is_listed(coin)),
                "track": ({"id": int(t["id"]), "base_ntl": _f(t.get("base_notional")),
                           "closed_pct": _closed_pct(float(t.get("base_szi") or 0), szi), "since": int(t["created_ts"] or 0),
                           "chat": _chat_label(cfg, t.get("chat_id") or "")} if t else None)})
            acc["ntl"] += ntl
            acc["upnl"] += upnl
    out["positions"].sort(key=lambda p: -p["ntl"])
    acc["n"] = len(out["positions"])
    mode = await _mode(client, addr) if (client is not None and states and not stale) else _cached_mode(addr)
    acc["mode"] = mode
    if mode in POOLED:
        acc["value"] = await _pooled_value(client, addr) if not stale else (_POOL.get(addr) or (0, None))[1]
        acc["withdrawable"] = None                          # birleşik teminatta açık bir "çekilebilir" sayısı yok
    v = acc["value"]
    acc["lev"] = round(acc["ntl"] / v, 2) if (mode is not None and v and v > 0) else None
    out["account"] = acc if states else None
    out["partial"] = bool(partial) or bool(states and mode is None)
    if client is not None and states:
        if stale:
            out["fills"] = (_FILLS.get(addr) or (0, []))[1]
        else:
            out["fills"], fills_err = await _fills(client, addr)
            out["partial"] = out["partial"] or fills_err
    for t in trk:
        if (t.get("src") or "") != "unwind":
            continue
        base = float(t.get("base_szi") or 0)
        if t.get("active"):                                 # açık: canlı boyut (HL okunamadıysa bilinmiyor)
            cp = _closed_pct(base, live_szi.get(t["coin"])) if states else None
        elif t.get("end_note") in ("kapandı", "likide oldu"):
            cp = 100.0
        else:                                               # elle bırakıldı / süre doldu: son bildirimdeki boyut (/takipte gibi)
            cp = _closed_pct(base, float(t["last_szi"]) if t.get("last_szi") is not None else None)
        out["tracked"].append({"id": int(t["id"]), "coin": t["coin"], "label": assets.label(t["coin"]),
                               "side": t.get("side") or "", "base_ntl": _f(t.get("base_notional")),
                               "since": int(t.get("created_ts") or 0), "active": bool(t.get("active")),
                               "end_note": t.get("end_note") or "", "closed_pct": cp})
    out["alerts"] = await _alerts(addr, {int(t["id"]) for t in trk})
    return out


async def list_rows(cfg) -> dict:
    """/takipte — 🧯 takipleri (açık + son 14 gün), cüzdan başına; HL sorgusu yok."""
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM trackers WHERE src='unwind' AND (active=1 OR created_ts>=?) ORDER BY created_ts",
            (now() - KEEP_DAYS * 86400,))
        rows = [dict(r) for r in await cur.fetchall()]
    by: dict[str, dict] = {}
    for t in rows:
        a = t["address"]
        w = by.setdefault(a, {"address": a, "short": f"{a[:6]}..{a[-4:]}", "url": sitelink.wallet_path(cfg, a),
                              "active": 0, "ended": 0, "since": int(t["created_ts"] or 0), "last": 0, "coins": []})
        w["active" if t.get("active") else "ended"] += 1
        w["last"] = max(w["last"], int(t["created_ts"] or 0))
        base = float(t.get("base_szi") or 0)
        cp = _closed_pct(base, float(t["last_szi"]) if t.get("last_szi") is not None else None)
        if not t.get("active") and t.get("end_note") in ("kapandı", "likide oldu"):
            cp = 100.0
        w["coins"].append({"id": int(t["id"]), "label": assets.label(t["coin"]), "side": t.get("side") or "",
                           "base_ntl": _f(t.get("base_notional")), "active": bool(t.get("active")),
                           "end_note": t.get("end_note") or "", "closed_pct": cp})
    wallets = sorted(by.values(), key=lambda w: (w["active"] == 0, -w["last"]))
    for w in wallets:
        w["coins"].sort(key=lambda c: (not c["active"], -(c["base_ntl"] or 0)))
    return {"wallets": wallets, "n_active": sum(w["active"] for w in wallets), "ts": now()}
