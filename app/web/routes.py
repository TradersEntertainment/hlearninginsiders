"""Dashboard — ticker ara, en büyük pozlar + likidasyona en yakın pozlar."""
import hashlib
import hmac
import json
import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..config import EDITABLE_FIELDS, convert_value, display_value
from ..db import db, kv_get, kv_set, now
from ..earnings.calendar import annotate, upcoming_events
from ..hl.universe import find_ticker, get_universe
from ..propr import is_listed as propr_listed
from ..radar import autoscan, clusters, lowvol, metrics

TR = ZoneInfo("Europe/Istanbul")

templates = Jinja2Templates(directory=str(pathlib.Path(__file__).parent / "templates"))


def _usd(n):
    if n is None:
        return "-"
    a = abs(n)
    if a >= 1_000_000:
        return f"${n / 1_000_000:.2f}M"
    if a >= 1_000:
        return f"${n / 1_000:.0f}K"
    return f"${n:.0f}"


def _px(p):
    if not p:
        return "-"
    return f"{p:.0f}" if p >= 1000 else (f"{p:.2f}" if p >= 10 else f"{p:.4f}")


def _age(ts):
    if not ts:
        return "?"
    h = (now() - ts) / 3600
    if h < 1:
        return f"{h * 60:.0f}dk"
    return f"{h:.0f}h" if h < 48 else f"{h / 24:.0f}g"


def _dt(ts):
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, TR).strftime("%d.%m %H:%M")


templates.env.filters.update(usd=_usd, px=_px, age=_age, dt=_dt)

router = APIRouter()

# Sekme ikonu: radar halkaları + yeşil balina sinyali (16px'te de okunur)
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="15" fill="#1d1730"/>
<circle cx="30" cy="34" r="21" fill="none" stroke="#8f7bff" stroke-width="3.5" opacity=".45"/>
<circle cx="30" cy="34" r="12" fill="none" stroke="#8f7bff" stroke-width="3.5" opacity=".8"/>
<path d="M30 34 L47 17" stroke="#c3b3ff" stroke-width="3.5" stroke-linecap="round"/>
<circle cx="46" cy="18" r="8" fill="#8be9b8"/>
</svg>"""


@router.get("/favicon.svg")
async def favicon_svg():
    return Response(FAVICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/favicon.ico")
async def favicon_ico():
    return Response(FAVICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


# Grafik renklerinin tek kaynağı base.html :root değişkenleri (tema ile uyumlu).
# Gerçek hex'ler orada: chart #e5484d/#26997b, liq #c47216/#2b8cbe (validator'dan geçen çiftler)
CHART_SHORT = "var(--chart-short)"
CHART_LONG = "var(--chart-long)"
LIQ_LONG = "var(--liq-long)"      # long liq (fiyatın altında)
LIQ_SHORT = "var(--liq-short)"    # short liq (fiyatın üstünde)


def _liq_chart(rows: list[dict], mark: float | None, max_dist_pct: float) -> dict | None:
    """Likidasyon haritası: her pozisyon, likide olacağı fiyat seviyesinde
    soldan sağa bir bar (boy = notional). Long liq'ler fiyatın altında (turuncu),
    short liq'ler üstünde (mavi)."""
    if not mark:
        return None
    pts = []
    for p in rows:
        liq = p.get("liq_px")
        if not liq or p["notional"] <= 0:
            continue
        dist = abs(mark - liq) / mark * 100
        if dist <= max_dist_pct:
            pts.append({**p, "_dist": dist})
    pts = pts[:40]
    if not pts:
        return None
    prices = [p["liq_px"] for p in pts] + [mark]
    lo, hi = min(prices), max(prices)
    pad = (hi - lo) * 0.07 or lo * 0.01 or 1.0
    lo, hi = lo - pad, hi + pad
    W, H, top, bot, left, right = 820, 300, 16, 26, 64, 16
    span = W - left - right

    def yf(v: float) -> float:
        return top + (hi - v) / (hi - lo) * (H - top - bot)

    maxn = max(p["notional"] for p in pts)
    bars = []
    for p in sorted(pts, key=lambda r: -r["notional"]):
        w = max(6.0, p["notional"] / maxn * span)
        bars.append({"y": round(yf(p["liq_px"]), 1), "w": round(w, 1),
                     "side": p["side"], "addr": p["address"],
                     "tip": f"{p['address'][:8]}..{p['address'][-4:]} · "
                            f"{'LONG' if p['side'] == 'long' else 'SHORT'} "
                            f"{_usd(p['notional'])} → liq {_px(p['liq_px'])}"
                            f" (mesafe %{p['_dist']:.1f})"})
    for side in ("short", "long"):
        prev = None
        for b in sorted((b for b in bars if b["side"] == side), key=lambda b: b["y"]):
            if prev is not None and b["y"] - prev < 7:
                b["y"] = round(prev + 7, 1)
            prev = b["y"]
    labels = [{"y": b["y"] + 3.5, "x": left + b["w"] + 6, "text": b["tip"].split(" · ")[1].split(" → ")[0]}
              for b in bars[:3]]
    ticks = [{"v": _px(lo + (hi - lo) * i / 4), "y": round(yf(lo + (hi - lo) * i / 4), 1)}
             for i in range(5)]
    return {"W": W, "H": H, "left": left, "bars": bars, "ticks": ticks, "labels": labels,
            "mark_y": round(yf(mark), 1), "mark_txt": _px(mark),
            "c_long": LIQ_LONG, "c_short": LIQ_SHORT,
            "n_long": sum(1 for b in bars if b["side"] == "long"),
            "n_short": sum(1 for b in bars if b["side"] == "short")}


def _entry_chart(rows: list[dict], mark: float | None) -> dict | None:
    """Entry haritası: her pozisyon, açıldığı fiyat seviyesinde yatay bir bar.
    Shortlar merkez ekseninin solunda, longlar sağında; bar boyu = notional."""
    pts = [p for p in rows if p.get("entry_px") and p["notional"] > 0][:40]
    if not pts or not mark:
        return None
    prices = [p["entry_px"] for p in pts] + [mark]
    lo, hi = min(prices), max(prices)
    pad = (hi - lo) * 0.07 or lo * 0.01 or 1.0
    lo, hi = lo - pad, hi + pad
    W, H, top, bot, left, right = 820, 320, 16, 26, 64, 16
    cx = left + (W - left - right) / 2
    half = (W - left - right) / 2 - 10

    def yf(v: float) -> float:
        return top + (hi - v) / (hi - lo) * (H - top - bot)

    maxn = max(p["notional"] for p in pts)
    bars = []
    for p in sorted(pts, key=lambda r: -r["notional"]):
        w = max(6.0, p["notional"] / maxn * half)
        y = yf(p["entry_px"])
        bars.append({"y": round(y, 1), "w": round(w, 1), "side": p["side"],
                     "x": round(cx - w, 1) if p["side"] == "short" else round(cx, 1),
                     "addr": p["address"],
                     "tip": f"{p['address'][:8]}..{p['address'][-4:]} · "
                            f"{'SHORT' if p['side'] == 'short' else 'LONG'} "
                            f"{_usd(p['notional'])} @{_px(p['entry_px'])}"})
    # aynı seviyeye yığılanları dikeyde hafifçe ayır
    for side in ("short", "long"):
        prev = None
        for b in sorted((b for b in bars if b["side"] == side), key=lambda b: b["y"]):
            if prev is not None and b["y"] - prev < 7:
                b["y"] = round(prev + 7, 1)
            prev = b["y"]
    # en büyük 3 pozisyona seçici doğrudan etiket
    labels = []
    for b in bars[:3]:
        short_side = b["side"] == "short"
        labels.append({"y": b["y"] + 3.5,
                       "x": b["x"] - 6 if short_side else b["x"] + b["w"] + 6,
                       "anchor": "end" if short_side else "start",
                       "text": b["tip"].split(" · ")[1]})
    ticks = [{"v": _px(lo + (hi - lo) * i / 4), "y": round(yf(lo + (hi - lo) * i / 4), 1)}
             for i in range(5)]
    return {"W": W, "H": H, "cx": cx, "left": left, "bars": bars, "ticks": ticks,
            "labels": labels, "mark_y": round(yf(mark), 1), "mark_txt": _px(mark),
            "c_short": CHART_SHORT, "c_long": CHART_LONG,
            "n_short": sum(1 for b in bars if b["side"] == "short"),
            "n_long": sum(1 for b in bars if b["side"] == "long")}


def _guard(request: Request) -> str:
    tok = request.app.state.cfg.dashboard_token
    if not tok:
        return ""
    k = request.query_params.get("key") or request.cookies.get("dbkey")
    if k != tok:
        raise HTTPException(401, "Erişim için ?key=TOKEN ekle (DASHBOARD_TOKEN)")
    return tok


def _render(request: Request, name: str, ctx: dict):
    key = _guard(request)
    ctx.update({"request": request, "k": f"?key={key}" if key else "",
                "is_admin": _is_admin(request), "has_pw": bool(_admin_secret(request))})
    resp = templates.TemplateResponse(request, name, ctx)
    if key and request.query_params.get("key"):
        resp.set_cookie("dbkey", key, max_age=30 * 86400, httponly=True)
    return resp


# ---------------- yönetici (yazma işlemleri) koruması ----------------

def _admin_secret(request: Request) -> str:
    cfg = request.app.state.cfg
    return cfg.admin_password or cfg.dashboard_token


def _admin_cookie(secret: str) -> str:
    return hashlib.sha256(f"hlir-admin::{secret}".encode()).hexdigest()


def _is_admin(request: Request) -> bool:
    secret = _admin_secret(request)
    if not secret:
        return True  # şifre tanımlı değil → koruma yok (arayüzde uyarı gösterilir)
    c = request.cookies.get("admin") or ""
    return bool(c) and hmac.compare_digest(c, _admin_cookie(secret))


def _require_admin(request: Request) -> None:
    if not _is_admin(request):
        raise HTTPException(403, "Bu işlem yönetici şifresi ister — /login sayfasından giriş yap")


def _keyq(request: Request, extra: str = "") -> str:
    """Mevcut ?key= parametresini koruyarak query string üret."""
    tok = request.app.state.cfg.dashboard_token
    parts = []
    if tok and (request.query_params.get("key") or request.cookies.get("dbkey")):
        parts.append(f"key={tok}")
    if extra:
        parts.append(extra)
    return ("?" + "&".join(parts)) if parts else ""


@router.get("/login")
async def login_page(request: Request, err: int = 0):
    _guard(request)
    return _render(request, "login.html", {"err": err, "nxt": request.query_params.get("nxt", "/")})


@router.post("/login")
async def login_submit(request: Request):
    _guard(request)
    form = await request.form()
    secret = _admin_secret(request)
    nxt = (form.get("nxt") or "/").strip()
    if not nxt.startswith("/"):
        nxt = "/"
    if not secret or not hmac.compare_digest((form.get("password") or "").strip(), secret):
        return RedirectResponse(f"/login{_keyq(request, 'err=1')}", status_code=303)
    resp = RedirectResponse(f"{nxt}{_keyq(request)}", status_code=303)
    resp.set_cookie("admin", _admin_cookie(secret), max_age=30 * 86400,
                    httponly=True, samesite="lax")
    return resp


@router.post("/logout")
async def logout(request: Request):
    resp = RedirectResponse(f"/{_keyq(request)}", status_code=303)
    resp.delete_cookie("admin")
    return resp


@router.get("/")
async def index(request: Request):
    q = (request.query_params.get("q") or "").strip()
    if q:
        key = request.query_params.get("key")
        url = f"/t/{q.upper()}" + (f"?key={key}" if key else "")
        _guard(request)
        return RedirectResponse(url)
    cfg = request.app.state.cfg
    universe = await get_universe()
    events = annotate(await upcoming_events(14))
    # Açıklanmış olanlar listenin sonuna — "bugün" sanıp geç kalınmasın
    events.sort(key=lambda e: (e["passed"], e["report_ts"]))
    ts_now = now()
    async with db() as conn:
        cur = await conn.execute("SELECT COUNT(*) c FROM fills")
        fills_n = (await cur.fetchone())["c"]
        cur = await conn.execute("SELECT COUNT(*) c FROM addresses")
        addr_n = (await cur.fetchone())["c"]
        cur = await conn.execute("SELECT COUNT(*) c FROM addresses WHERE watchlist=1")
        watch_n = (await cur.fetchone())["c"]
        # Son 72 saatte açılmış büyük pozisyonlar (earnings şartı yok; MM/vault hariç)
        cur = await conn.execute(
            """SELECT p.*, t.symbol FROM positions_current p
               JOIN tickers t ON t.coin = p.coin
               LEFT JOIN addresses a ON a.address = p.address
               WHERE p.notional >= ? AND p.opened_ts IS NOT NULL AND p.opened_ts >= ?
                 AND COALESCE(a.entity, '') = ''
               ORDER BY p.opened_ts DESC LIMIT 12""",
            (cfg.big_position_usd, ts_now - 72 * 3600))
        recent_big = [dict(r) for r in await cur.fetchall()]
        # En şüpheli açık pozisyonlar (skora göre; MM/vault hariç)
        cur = await conn.execute(
            """SELECT p.*, t.symbol FROM positions_current p
               JOIN tickers t ON t.coin = p.coin
               LEFT JOIN addresses a ON a.address = p.address
               WHERE COALESCE(p.score, 0) >= 40 AND COALESCE(a.entity, '') = ''
               ORDER BY p.score DESC, p.notional DESC LIMIT 10""")
        suspicious = [dict(r) for r in await cur.fetchall()]
        # Tek hisse uzmanları: 30 günde >=5 fill, >=%90'ı tek coin'de
        cur = await conn.execute(
            "SELECT address, coin, COUNT(*) c, SUM(notional) v FROM fills"
            " WHERE ts >= ? GROUP BY address, coin", (ts_now - 30 * 86400,))
        fillagg = [dict(r) for r in await cur.fetchall()]

    by_addr: dict[str, list[dict]] = {}
    for r in fillagg:
        by_addr.setdefault(r["address"], []).append(r)
    sym_map = {t["coin"]: t["symbol"] for t in universe}
    specialists = []
    for addr, lst in by_addr.items():
        total = sum(r["c"] for r in lst)
        if total < 5:
            continue
        top = max(lst, key=lambda r: r["c"])
        if top["c"] / total < 0.9:
            continue
        specialists.append({"address": addr, "coin": top["coin"],
                            "symbol": sym_map.get(top["coin"], top["coin"]),
                            "n": top["c"], "vol": top["v"]})
    specialists.sort(key=lambda s: -s["vol"])
    specialists = specialists[:10]
    if specialists:
        addrs = [s["address"] for s in specialists]
        qm = ",".join("?" * len(addrs))
        async with db() as conn:
            cur = await conn.execute(
                f"SELECT address, hits, misses, watchlist, entity FROM addresses"
                f" WHERE address IN ({qm})", addrs)
            rec = {r["address"]: dict(r) for r in await cur.fetchall()}
            cur = await conn.execute(
                f"SELECT address, coin, side, notional FROM positions_current"
                f" WHERE address IN ({qm})", addrs)
            posmap: dict[str, list[dict]] = {}
            for r in await cur.fetchall():
                posmap.setdefault(r["address"], []).append(dict(r))
        for s in specialists:
            s.update(rec.get(s["address"], {}))
            open_pos = [p for p in posmap.get(s["address"], []) if p["coin"] == s["coin"]]
            s["open"] = open_pos[0] if open_pos else None
        specialists = [s for s in specialists if not s.get("entity")]  # MM/vault hariç

    # ---- Likidasyon haritası (tüm coin'ler; equity taraması ∪ dev-poz radarı) ----
    try:
        liqmin = float(request.query_params.get("liqmin") or 250_000)
    except ValueError:
        liqmin = 250_000
    async with db() as conn:
        cur = await conn.execute(
            """SELECT a.coin, a.mark_px FROM asset_metrics a
               JOIN (SELECT coin, MAX(ts) mts FROM asset_metrics GROUP BY coin) b
                 ON a.coin=b.coin AND a.ts=b.mts""")
        marks = {r["coin"]: r["mark_px"] for r in await cur.fetchall()}
        cur = await conn.execute(
            """SELECT p.coin, p.address, p.side, p.notional, p.liq_px, t.symbol
               FROM positions_current p JOIN tickers t ON t.coin=p.coin
               WHERE p.liq_px IS NOT NULL AND p.notional>=?""", (liqmin,))
        eq_rows = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT address, coin, side, notional, liq_px, last_dist FROM liq_watch"
            " WHERE notional>=?", (liqmin,))
        lw_rows = [dict(r) for r in await cur.fetchall()]
    liq_map = []
    seen_keys = set()
    for r in eq_rows:
        mark = marks.get(r["coin"])
        if not mark:
            continue
        dist = abs(mark - r["liq_px"]) / mark * 100
        if dist > cfg.max_liq_distance_pct:
            continue
        seen_keys.add((r["address"], r["coin"]))
        liq_map.append({"symbol": r["symbol"], "coin": r["coin"], "address": r["address"],
                        "side": r["side"], "notional": r["notional"],
                        "liq_px": r["liq_px"], "dist": dist})
    for r in lw_rows:
        if (r["address"], r["coin"]) in seen_keys or r["last_dist"] is None:
            continue
        if r["last_dist"] > cfg.max_liq_distance_pct:
            continue
        liq_map.append({"symbol": r["coin"].split(":")[-1], "coin": r["coin"],
                        "address": r["address"], "side": r["side"],
                        "notional": r["notional"], "liq_px": r["liq_px"],
                        "dist": r["last_dist"]})
    liq_map.sort(key=lambda x: x["dist"])
    liq_map = liq_map[:20]

    # Likidasyon duvarları (küme özeti) — tweet'teki heatmap okuması
    from ..radar.liqwatch import find_clusters
    liq_walls = (await find_clusters(cfg))[:4]

    # Emir defteri duvarları (bekleyen dev emirler — SPCX $202M tarzı)
    async with db() as conn:
        cur = await conn.execute(
            """SELECT b.*, t.symbol FROM book_walls b JOIN tickers t ON t.coin=b.coin
               WHERE b.active=1 AND b.last_ts >= ? AND b.notional >= ?
               ORDER BY b.notional DESC LIMIT 10""",
            (ts_now - 900, cfg.wall_min_usd))
        book_walls = [dict(r) for r in await cur.fetchall()]
    bw_maxn = max((x["notional"] for x in book_walls), default=1)
    for x in book_walls:
        x["wpct"] = max(4, x["notional"] / bw_maxn * 100)
        x["propr"] = propr_listed(x["symbol"])
        x["age_ts"] = x["first_ts"]
    liq_maxn = max((x["notional"] for x in liq_map), default=1)
    for x in liq_map:
        x["wpct"] = max(4, x["notional"] / liq_maxn * 100)
        x["propr"] = propr_listed(x["symbol"])

    # propr.xyz'de işlem görebildiklerimizi her listede işaretle
    for _row in (*recent_big, *suspicious, *events, *specialists, *universe):
        _row["propr"] = propr_listed(_row.get("symbol") or _row.get("coin") or "")

    # ---- Earnings radarı kartları: yaklaşan her bilanço için balina verisi + yön okuması ----
    from datetime import date as _date
    ecards = []
    today = _date.today()
    live_events = [e for e in events if not e["passed"]]
    for e in (live_events or events)[:6]:
        coin = e["coin"]
        s = await metrics.summary(coin)
        async with db() as conn:
            cur = await conn.execute(
                "SELECT side, SUM(notional) t FROM positions_current WHERE coin=? GROUP BY side",
                (coin,))
            sums = {r["side"]: r["t"] or 0 for r in await cur.fetchall()}
            cur = await conn.execute(
                """SELECT p.* FROM positions_current p
                   LEFT JOIN addresses a ON a.address=p.address
                   WHERE p.coin=? AND COALESCE(a.entity,'')='' AND COALESCE(p.score,0)>0
                   ORDER BY p.score DESC, p.notional DESC LIMIT 1""", (coin,))
            top = await cur.fetchone()
        lo, sh = sums.get("long", 0), sums.get("short", 0)
        tot = lo + sh
        shp = sh / tot * 100 if tot else None
        if shp is None:
            verdict = ("❔", "Henüz pozisyon verisi yok — tarama sürüyor")
        elif shp >= 65:
            verdict = ("🐻", f"Balinalar DÜŞÜŞ tarafında (%{shp:.0f} short)")
        elif shp <= 35:
            verdict = ("🐂", f"Balinalar YÜKSELİŞ tarafında (%{100 - shp:.0f} long)")
        else:
            verdict = ("⚖️", f"Kararsız — %{100 - shp:.0f} long / %{shp:.0f} short")
        flags = []
        if s.get("oi_change_pct") is not None and s["oi_change_pct"] >= 50:
            flags.append(f"⚠️ OI 24 saatte +%{s['oi_change_pct']:.0f} — birileri birikiyor")
        if top and (top["score"] or 0) >= 50:
            tside = "SHORT" if top["side"] == "short" else "LONG"
            flags.append(f"🚨 {top['score']} puanlık şüpheli {tside} {_usd(top['notional'])} var")
        try:
            days_left = (_date.fromisoformat(e["date_et"]) - today).days
        except ValueError:
            days_left = None
        ecards.append({"symbol": e["symbol"], "date_et": e["date_et"],
                       "propr": propr_listed(e["symbol"]),
                       "icon": e["icon"], "when_txt": e["when_txt"], "tsi": e["tsi"],
                       "exact": e["exact"], "countdown": e["countdown"],
                       "passed": e["passed"], "note": e.get("note"),
                       "maybe_passed": e["maybe_passed"], "alt_tsi": e["alt_tsi"],
                       "uncertain": e["uncertain"],
                       "hour": e.get("hour_hint"), "days_left": days_left,
                       "mark": s.get("mark"), "px_change": s.get("px_change_pct"),
                       "oi_ntl": s.get("oi_ntl"), "oi_change": s.get("oi_change_pct"),
                       "lo": lo, "sh": sh, "shp": shp,
                       "verdict": verdict, "flags": flags,
                       "top": dict(top) if top else None})

    # Durum şeridi için: yaklaşan earnings'ler tarihe göre gruplu (ör. 11.08: CRWV LITE QNT)
    strip_days: dict[str, list[dict]] = {}
    n = 0
    for e in live_events:
        if n >= 8:
            break
        d = f"{e['date_et'][8:10]}.{e['date_et'][5:7]}"
        strip_days.setdefault(d, []).append({"symbol": e["symbol"], "icon": e["icon"]})
        n += 1

    # ---- Hafıza: en iyi biliciler + son arşiv kayıtları ----
    async with db() as conn:
        cur = await conn.execute(
            """SELECT address, hits, misses, watchlist FROM addresses
               WHERE hits > 0 AND COALESCE(entity,'')=''
               ORDER BY hits DESC, misses ASC LIMIT 8""")
        winners = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute(
            """SELECT symbol, date_et, hour_hint, move_pct, result_note FROM earnings_events
               WHERE evaluated=1 AND result_note IS NOT NULL
               ORDER BY date_et DESC LIMIT 6""")
        archive = [dict(r) for r in await cur.fetchall()]

    collector = getattr(request.app.state, "collector", None)
    return _render(request, "index.html", {
        "universe": universe, "events": events, "strip_days": strip_days, "ecards": ecards,
        "winners": winners, "archive": archive,
        "recent_big": recent_big, "suspicious": suspicious, "specialists": specialists,
        "liq_map": liq_map, "liqmin": liqmin, "liq_walls": liq_walls,
        "book_walls": book_walls,
        "liq_chips": [(100_000, "100K+"), (250_000, "250K+"), (1_000_000, "1M+"),
                      (5_000_000, "5M+"), (30_000_000, "30M+")],
        "stats": {"fills": fills_n, "addrs": addr_n, "watch": watch_n,
                  "ws": "🟢 bağlı" if collector and collector.connected else "🔴 kopuk"},
    })


@router.get("/t/{symbol}")
async def coin_page(request: Request, symbol: str):
    _guard(request)
    t = await find_ticker(symbol)
    if not t:
        return _render(request, "coin.html", {"ticker": None, "symbol": symbol.upper()})
    coin = t["coin"]
    summ = await metrics.summary(coin)
    mark = summ.get("mark")
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM positions_current WHERE coin=? ORDER BY notional DESC LIMIT 100",
            (coin,))
        rows = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT * FROM fills WHERE coin=? ORDER BY ts DESC LIMIT 15", (coin,))
        fills = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT * FROM earnings_events WHERE coin=? AND date_et>=date('now','-1 day')"
            " ORDER BY date_et LIMIT 1", (coin,))
        ev = await cur.fetchone()
        ev = annotate([dict(ev)])[0] if ev else None
        cur = await conn.execute(
            "SELECT * FROM book_walls WHERE coin=? AND active=1 AND last_ts>=?"
            " ORDER BY notional DESC", (coin, now() - 900))
        bwalls = [dict(r) for r in await cur.fetchall()]

    for p in rows:
        p["reasons"] = ", ".join(json.loads(p.get("score_reasons") or "[]"))
        if mark and p.get("liq_px"):
            p["liq_dist"] = abs(mark - p["liq_px"]) / mark * 100
        else:
            p["liq_dist"] = None
    cfg = request.app.state.cfg
    liq_rows = sorted(
        [p for p in rows
         if p["liq_dist"] is not None and p["liq_dist"] <= cfg.max_liq_distance_pct],
        key=lambda p: p["liq_dist"])[:30]
    lo = sum(p["notional"] for p in rows if p["side"] == "long")
    sh = sum(p["notional"] for p in rows if p["side"] == "short")

    async with db() as conn:
        cur = await conn.execute("SELECT ts FROM scans WHERE coin=?", (coin,))
        srow = await cur.fetchone()
    scanned_ts = srow["ts"] if srow else None

    # Bayatsa arka planda otomatik tara — kullanıcı butona basmak zorunda kalmasın
    scanning = autoscan.is_scanning(coin)
    stale = scanned_ts is None or (now() - scanned_ts) > cfg.scan_stale_min * 60
    if stale and not scanning:
        from ..radar.report import coin_dex
        autoscan.kick(cfg, request.app.state.client, coin, coin_dex(coin),
                      request.app.state.bot)
        scanning = True

    return _render(request, "coin.html", {
        "ticker": t, "symbol": t["symbol"], "coin": coin, "summ": summ,
        "rows": rows[:50], "liq_rows": liq_rows, "fills": fills, "event": ev,
        "long_total": lo, "short_total": sh, "scanned_ts": scanned_ts,
        "scanning": scanning, "max_liq": cfg.max_liq_distance_pct,
        "tg": request.query_params.get("tg"),
        "has_bot": request.app.state.bot is not None,
        "chart": _entry_chart(rows, summ.get("mark")),
        "liqchart": _liq_chart(rows, summ.get("mark"), cfg.max_liq_distance_pct),
        "bwalls": bwalls,
        "propr": propr_listed(t["symbol"]),
        "n_long": sum(1 for p in rows if p["side"] == "long"),
        "n_short": sum(1 for p in rows if p["side"] == "short"),
    })


@router.post("/t/{symbol}/send")
async def coin_send_telegram(request: Request, symbol: str):
    """Mevcut taranmış veriden raporu derleyip Telegram'a yolla (yeniden tarama yok)."""
    key = _guard(request)
    _require_admin(request)
    t = await find_ticker(symbol)
    if not t:
        raise HTTPException(404, "coin yok")
    bot = request.app.state.bot
    coin = t["coin"]
    tg = "err"
    if bot:
        from ..telegram import format as fmt
        summ = await metrics.summary(coin)
        async with db() as conn:
            cur = await conn.execute(
                "SELECT * FROM positions_current WHERE coin=? ORDER BY notional DESC LIMIT 50",
                (coin,))
            rows = [dict(r) for r in await cur.fetchall()]
        cluster_list = await clusters.find_clusters(rows)
        text = fmt.earnings_report({"symbol": t["symbol"]}, "ondemand", summ, rows,
                                   request.app.state.cfg, cluster_list=cluster_list)
        notifier = getattr(request.app.state, "notifier", None)
        if notifier and await notifier.send("earnings", text, priority="critical",
                                            key=f"manual:{t['symbol']}"):
            tg = "ok"
    sep = "&" if key else "?"
    return RedirectResponse(
        f"/t/{t['symbol']}" + (f"?key={key}" if key else "") + f"{sep}tg={tg}",
        status_code=303)


@router.post("/settings/test-telegram")
async def settings_test_telegram(request: Request):
    key = _guard(request)
    _require_admin(request)
    notifier = getattr(request.app.state, "notifier", None)
    tg = "err"
    if notifier and await notifier.send(
            "test", "🐋 Test — HL Insider Radar bağlantısı çalışıyor! ✨",
            priority="critical", key="manual-test"):
        tg = "ok"
    sep = "&" if key else "?"
    return RedirectResponse(
        "/settings" + (f"?key={key}" if key else "") + f"{sep}tg={tg}", status_code=303)


@router.post("/t/{symbol}/scan")
async def coin_scan(request: Request, symbol: str):
    key = _guard(request)
    t = await find_ticker(symbol)
    if not t:
        raise HTTPException(404, "coin yok")
    from ..radar.report import build_scan, coin_dex
    cfg = request.app.state.cfg
    client = request.app.state.client
    await build_scan(cfg, client, t["coin"], coin_dex(t["coin"]), quick=True)
    return RedirectResponse(f"/t/{t['symbol']}" + (f"?key={key}" if key else ""),
                            status_code=303)


@router.get("/whale/{address}")
async def whale_page(request: Request, address: str):
    _guard(request)
    addr = address.lower()
    cfg = request.app.state.cfg
    client = request.app.state.client
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM addresses WHERE address=?", (addr,))
        row = await cur.fetchone()
        arow = dict(row) if row else {}
        cur = await conn.execute(
            "SELECT * FROM fills WHERE address=? ORDER BY ts DESC LIMIT 20", (addr,))
        fills = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute(
            """SELECT s.*, e.symbol AS ev_symbol, e.date_et AS ev_date
               FROM position_snapshots s LEFT JOIN earnings_events e ON e.id=s.event_id
               WHERE s.address=? ORDER BY s.ts DESC LIMIT 20""", (addr,))
        snaps = [dict(r) for r in await cur.fetchall()]

    live = []
    for dex in [*cfg.equity_dexes, ""]:
        try:
            state = await client.clearinghouse(addr, dex)
        except Exception:
            continue
        for ap in (state or {}).get("assetPositions") or []:
            p = ap.get("position") or {}
            try:
                szi = float(p.get("szi") or 0)
            except (TypeError, ValueError):
                continue
            if szi == 0:
                continue
            live.append({"coin": p.get("coin"), "side": "short" if szi < 0 else "long",
                         "notional": float(p.get("positionValue") or 0),
                         "entry_px": float(p.get("entryPx") or 0),
                         "leverage": float((p.get("leverage") or {}).get("value") or 0),
                         "liq_px": float(p["liquidationPx"]) if p.get("liquidationPx") else None,
                         "upnl": float(p.get("unrealizedPnl") or 0)})
    live.sort(key=lambda p: p["notional"], reverse=True)
    linked = await clusters.linked_addresses(addr)
    return _render(request, "whale.html", {
        "address": addr, "arow": arow, "live": live, "fills": fills, "snaps": snaps,
        "linked": linked,
    })


# ---------------- ayarlar ----------------

@router.get("/gecmis")
async def history_page(request: Request):
    _guard(request)
    async with db() as conn:
        cur = await conn.execute(
            """SELECT * FROM earnings_events WHERE evaluated=1
               ORDER BY date_et DESC LIMIT 60""")
        rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            cur = await conn.execute(
                """SELECT s.*, COALESCE(a.hits,0) hits, COALESCE(a.misses,0) misses,
                          COALESCE(a.watchlist,0) watchlist
                   FROM position_snapshots s LEFT JOIN addresses a ON a.address=s.address
                   WHERE s.event_id=? AND s.phase IN ('T-1h','pre')
                   ORDER BY s.notional DESC LIMIT 5""", (r["id"],))
            r["pre"] = [dict(x) for x in await cur.fetchall()]
            move = r.get("move_pct")
            for p in r["pre"]:
                p["hit"] = (None if move is None else
                            ((p["side"] == "short" and move < 0) or
                             (p["side"] == "long" and move > 0)))
        cur = await conn.execute(
            """SELECT address, hits, misses, watchlist, first_deposit_ts, last_deposit_ts
               FROM addresses WHERE hits > 0 AND COALESCE(entity,'')=''
               ORDER BY hits DESC, misses ASC LIMIT 30""")
        winners = [dict(r) for r in await cur.fetchall()]
    return _render(request, "history.html", {"rows": rows, "winners": winners})


@router.get("/devler")
async def lowvol_page(request: Request):
    """Sessiz sular: düşük hacimli hisselerdeki dev pozisyonlar + OI hakimleri."""
    _guard(request)
    cfg = request.app.state.cfg
    rows = await lowvol.dominants(cfg)
    humans = [p for p in rows if not p["entity"]]
    bots = [p for p in rows if p["entity"]]
    return _render(request, "devler.html", {
        "humans": humans, "bots": bots,
        "max_vol": cfg.lowvol_max_day_volume,
        "min_share": cfg.lowvol_min_oi_share,
        "min_ntl": cfg.lowvol_min_notional,
        "alert_min": cfg.lowvol_alert_min_usd,
        "propr": {p["symbol"] for p in rows if propr_listed(p["symbol"])},
    })


@router.get("/settings")
async def settings_page(request: Request, saved: int = 0):
    _guard(request)
    cfg = request.app.state.cfg
    overrides = await kv_get("settings_overrides") or {}
    groups: dict[str, list[dict]] = {}
    for name, spec in EDITABLE_FIELDS.items():
        groups.setdefault(spec.get("group", "Radar ayarları"), []).append({
            "name": name,
            "label": spec["label"],
            "desc": spec["desc"],
            "type": spec["type"],
            "current": display_value(spec["type"], getattr(cfg, name)),
            "on": bool(getattr(cfg, name)) if spec["type"] == "bool" else False,
            "default": display_value(spec["type"], cfg.env_default(name)),
            "overridden": name in overrides,
        })
    return _render(request, "settings.html", {
        "groups": groups, "saved": saved,
        "tg": request.query_params.get("tg"),
        "has_bot": request.app.state.bot is not None,
    })


@router.post("/settings")
async def settings_save(request: Request):
    key = _guard(request)
    _require_admin(request)
    cfg = request.app.state.cfg
    form = await request.form()
    overrides = await kv_get("settings_overrides") or {}
    for name, spec in EDITABLE_FIELDS.items():
        if spec["type"] == "bool":
            # işaretsiz checkbox form'a gelmez → "0"
            raw = "1" if form.get(name) else "0"
        else:
            raw = (form.get(name) or "").strip()
        try:
            conv = convert_value(spec["type"], raw) if raw else None
        except (TypeError, ValueError):
            continue  # bozuk değer — sessizce atla
        if conv is None or conv == cfg.env_default(name):
            # boş bırakıldı ya da varsayılana döndü → override'ı kaldır
            if name in overrides:
                del overrides[name]
                setattr(cfg, name, cfg.env_default(name))
                cfg.overrides.pop(name, None)
            continue
        overrides[name] = raw
    await kv_set("settings_overrides", overrides)
    cfg.apply_overrides(overrides)
    return RedirectResponse(f"/settings?saved=1" + (f"&key={key}" if key else ""),
                            status_code=303)
