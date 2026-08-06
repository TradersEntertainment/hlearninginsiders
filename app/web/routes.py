"""Dashboard — ticker ara, en büyük pozlar + likidasyona en yakın pozlar."""
import json
import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import EDITABLE_FIELDS, convert_value, display_value
from ..db import db, kv_get, kv_set, now
from ..earnings.calendar import upcoming_events
from ..hl.universe import find_ticker, get_universe
from ..radar import autoscan, clusters, metrics

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
    ctx.update({"request": request, "k": f"?key={key}" if key else ""})
    resp = templates.TemplateResponse(request, name, ctx)
    if key and request.query_params.get("key"):
        resp.set_cookie("dbkey", key, max_age=30 * 86400, httponly=True)
    return resp


@router.get("/")
async def index(request: Request):
    q = (request.query_params.get("q") or "").strip()
    if q:
        key = request.query_params.get("key")
        url = f"/t/{q.upper()}" + (f"?key={key}" if key else "")
        _guard(request)
        return RedirectResponse(url)
    universe = await get_universe()
    events = await upcoming_events(14)
    async with db() as conn:
        cur = await conn.execute("SELECT COUNT(*) c FROM fills")
        fills_n = (await cur.fetchone())["c"]
        cur = await conn.execute("SELECT COUNT(*) c FROM addresses")
        addr_n = (await cur.fetchone())["c"]
        cur = await conn.execute("SELECT COUNT(*) c FROM addresses WHERE watchlist=1")
        watch_n = (await cur.fetchone())["c"]
    collector = getattr(request.app.state, "collector", None)
    return _render(request, "index.html", {
        "universe": universe, "events": events,
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
        ev = dict(ev) if ev else None

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
        autoscan.kick(cfg, request.app.state.client, coin, coin_dex(coin))
        scanning = True

    return _render(request, "coin.html", {
        "ticker": t, "symbol": t["symbol"], "coin": coin, "summ": summ,
        "rows": rows[:50], "liq_rows": liq_rows, "fills": fills, "event": ev,
        "long_total": lo, "short_total": sh, "scanned_ts": scanned_ts,
        "scanning": scanning, "max_liq": cfg.max_liq_distance_pct,
        "tg": request.query_params.get("tg"),
        "has_bot": request.app.state.bot is not None,
    })


@router.post("/t/{symbol}/send")
async def coin_send_telegram(request: Request, symbol: str):
    """Mevcut taranmış veriden raporu derleyip Telegram'a yolla (yeniden tarama yok)."""
    key = _guard(request)
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
        fake_event = {"symbol": t["symbol"], "date_et": "dashboard'dan gönderildi",
                      "hour_hint": "", "eps_est": None, "note": None}
        text = fmt.earnings_report(fake_event, "ondemand", summ, rows,
                                   request.app.state.cfg, cluster_list=cluster_list)
        text = text.replace("🎯", "🔍").replace("earnings — 🕐 erken pencere", "anlık rapor")
        if await bot.send(text):
            tg = "ok"
    sep = "&" if key else "?"
    return RedirectResponse(
        f"/t/{t['symbol']}" + (f"?key={key}" if key else "") + f"{sep}tg={tg}",
        status_code=303)


@router.post("/settings/test-telegram")
async def settings_test_telegram(request: Request):
    key = _guard(request)
    bot = request.app.state.bot
    tg = "err"
    if bot and await bot.send("🐋 Test — HL Insider Radar bağlantısı çalışıyor! ✨"):
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

@router.get("/settings")
async def settings_page(request: Request, saved: int = 0):
    _guard(request)
    cfg = request.app.state.cfg
    overrides = await kv_get("settings_overrides") or {}
    fields = []
    for name, spec in EDITABLE_FIELDS.items():
        fields.append({
            "name": name,
            "label": spec["label"],
            "desc": spec["desc"],
            "current": display_value(spec["type"], getattr(cfg, name)),
            "default": display_value(spec["type"], cfg.env_default(name)),
            "overridden": name in overrides,
        })
    return _render(request, "settings.html", {
        "fields": fields, "saved": saved,
        "tg": request.query_params.get("tg"),
        "has_bot": request.app.state.bot is not None,
    })


@router.post("/settings")
async def settings_save(request: Request):
    key = _guard(request)
    cfg = request.app.state.cfg
    form = await request.form()
    overrides = await kv_get("settings_overrides") or {}
    for name, spec in EDITABLE_FIELDS.items():
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
