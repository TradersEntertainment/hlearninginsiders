"""Dashboard — ticker ara, en büyük pozlar + likidasyona en yakın pozlar."""
import hashlib
import hmac
import json
import logging
import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..config import EDITABLE_FIELDS, convert_value, display_value
from ..db import db, kv_get, kv_set, now
from ..earnings.calendar import annotate, upcoming_events
from ..hl.universe import find_ticker, get_universe
from ..propr import is_listed as propr_listed
from ..tvsymbols import tv_symbol
from ..radar import (autoscan, bigpos, clusters, hourstats, lowvol, metrics,
                     pricechart)

log = logging.getLogger("web.routes")

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


def _plain(v) -> str:
    """Birimsiz okunabilir sayı: 5.66M / 12.3K / 180.5 (bilimsel gösterim yok)."""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if abs(f) >= 1_000_000:
        return f"{f / 1_000_000:.2f}M"
    if abs(f) >= 1_000:
        return f"{f / 1_000:.1f}K"
    return f"{f:g}"


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


def _posage(p):
    """Pozisyon yaşı: açılış biliniyorsa yaşı, bilinmiyorsa 'en az' alt sınırı.

    Fill kayıtları emekli olunca açılış anı kaybolabiliyor; pozisyonu İLK
    gördüğümüz an (first_seen_ts) yine de 'en az bu kadar eski' der."""
    opened = (p or {}).get("opened_ts")
    if opened:
        return _age(opened)
    seen = (p or {}).get("first_seen_ts")
    if seen:
        h = (now() - seen) / 3600
        return f"≥{h:.0f}h" if h < 48 else f"≥{h / 24:.0f}g"
    return "?"


templates.env.filters.update(usd=_usd, px=_px, age=_age, dt=_dt, posage=_posage)

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


def _hour_chart(stats: dict | None) -> dict | None:
    """Saatlik getiri barları: sıfır taban çizgisi, + yeşil / − kırmızı (polarite).
    Eksen TSİ sırasında; ABD borsası açık saatleri amber alt bantla işaretli."""
    if not stats or stats.get("empty") or not stats.get("hours"):
        return None
    hs = sorted(stats["hours"], key=lambda h: h["tsi"])
    maxabs = max((abs(h["avg"]) for h in hs), default=0) or 0.01
    W, H, top, bot, left = 820, 220, 16, 34, 46
    span_h = H - top - bot
    y0 = top + span_h / 2
    scale = (span_h / 2 - 6) / maxabs
    bw = (W - left - 16) / 24
    bars = []
    for i, h in enumerate(hs):
        v = h["avg"]
        bh = max(min(abs(v) * scale, span_h / 2 - 4), 1.5)
        bars.append({
            "x": round(left + i * bw + 2, 1), "w": round(bw - 4, 1),
            "y": round((y0 - bh) if v >= 0 else y0, 1), "h": round(bh, 1),
            "cx": round(left + i * bw + bw / 2, 1),
            "pos": v >= 0, "open": 9 <= h["et"] < 16,
            # örneklem güveni: az örnekli saat barı soluk çizilir (aynı görsel
            # ağırlıkta gösterip 12 örnekli barı 90 örnekli gibi okutmayalım)
            "op": round(min(1.0, max(0.35, (h["n"] or 0) / hourstats.MIN_N)), 2),
            # ham değerler: eskiden yalnız hazır 'tip' metni geçiyordu, tarayıcı
            # tooltip'i onu okuyordu. Anlık tooltip/kılavuz bunları ayrı ayrı
            # biçimlendirebilsin diye sayılar da taşınır.
            "tsi": h["tsi"], "et": h["et"], "avg": round(v, 4),
            "win": round(h["win"], 1), "n": h["n"],
            "tip": (f"TSİ {h['tsi']:02d}:00 (ET {h['et']:02d}:00) · ort {v:+.2f}%"
                    f" · %{h['win']:.0f} kazanç · {h['n']} örnek"),
            "label": f"{h['tsi']:02d}" if h["tsi"] % 3 == 0 else "",
        })
    return {"W": W, "H": H, "y0": round(y0, 1), "left": left, "bars": bars,
            # imleç kılavuzu için eksen ölçeği (JS x → saat çevirir)
            "top": top, "bot": bot, "bw": round(bw, 3)}


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
                     # ham fiyat/büyüklük: 'y' aşağıdaki üst üste binme
                     # ayıklamasında kaydırıldığı için ondan geri hesaplanamaz
                     "price": p["liq_px"], "notional": p["notional"],
                     "dist": round(p["_dist"], 2),
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
    _hit_bands(bars)   # hepsi 'left'ten başlıyor → hedefler tek eksende ayrışmalı
    labels = []
    taken = [yf(mark) - 6]      # 'şimdi X' yazısının TABANI da yer kaplıyor
    for b in bars[:3]:
        base = _label_base(b["y"] + 3.5, taken)
        if base is None:
            continue
        taken.append(base)
        txt = b["tip"].split(" · ")[1].split(" → ")[0]
        x = left + b["w"] + 6
        if x + len(txt) * 6.2 > W - 6:          # sığmıyor → bar'ın içine
            labels.append({"y": base, "x": left + b["w"] - 6,
                           "anchor": "end", "text": txt})
        else:
            labels.append({"y": base, "x": x, "anchor": "start", "text": txt})
    ticks = [{"v": _px(lo + (hi - lo) * i / 4), "y": round(yf(lo + (hi - lo) * i / 4), 1)}
             for i in range(5)]
    return {"W": W, "H": H, "left": left, "bars": bars, "ticks": ticks, "labels": labels,
            "mark_y": round(yf(mark), 1), "mark_txt": _px(mark),
            "lo": lo, "hi": hi, "top": top, "bot": bot,
            "c_long": LIQ_LONG, "c_short": LIQ_SHORT,
            "n_long": sum(1 for b in bars if b["side"] == "long"),
            "n_short": sum(1 for b in bars if b["side"] == "short")}


def _label_base(base: float, taken: list[float], gap: float = 13.0) -> float | None:
    """Çakışmayan yazı tabanı: önce olduğu yer, sonra küçük kaydırmalar.

    Eskiden en büyük pozisyonun etiketi 'şimdi X' yazısının üstüne biniyor ve
    ikisi de okunmuyordu. Kaydırma da tutmazsa etiket hiç yazılmaz — ipucu
    zaten üzerine gelince tam bilgiyi veriyor.
    """
    for cand in (base, base - gap + 4, base + gap - 4, base - gap, base + gap):
        if all(abs(cand - t) >= gap for t in taken):
            return cand
    return None


def _hit_bands(bars: list[dict], pad: float = 8.0) -> None:
    """Her bar'a çakışmayan bir fare hedefi (hy/hh) ver.

    Bar'lar 6 birim ince; sabit 16 birimlik hedef koyunca üst üste binen
    hedefler birbirini KAPATIYOR ve sıkışık kümenin üstteki bar'ı hiç
    yakalanamıyordu. Hedef, komşuya olan boşluğun yarısı kadar büyür
    (en çok `pad`), böylece hem geniş hem çakışmasız olur.
    """
    if not bars:
        return
    ys = sorted(bars, key=lambda b: b["y"])
    for i, b in enumerate(ys):
        up = (b["y"] - ys[i - 1]["y"]) / 2 if i else pad
        dn = (ys[i + 1]["y"] - b["y"]) / 2 if i + 1 < len(ys) else pad
        up, dn = min(pad, max(3.0, up)), min(pad, max(3.0, dn))
        b["hy"] = round(b["y"] - up, 1)
        b["hh"] = round(up + dn, 1)


def _levels(rows: list[dict], mark: float | None, field: str,
            band_pct: float = 0.5, top: int = 5) -> list[dict]:
    """Fiyat bandına göre kümelenmiş seviyeler — mum grafiğine yatay çizgi.

    liqwatch.find_clusters GLOBAL ve yön başına TEK kova (duvar alarmı için
    doğru); grafikte "hangi FİYATTA yığılma var" gerekiyor, o yüzden liq/entry
    fiyatları mark'ın %band_pct'i genişliğinde bantlara yuvarlanıp toplanır.
    """
    if not mark or mark <= 0:
        return []
    band = mark * band_pct / 100 or 1e-9
    buckets: dict[tuple[str, int], dict] = {}
    for p in rows:
        v = p.get(field)
        if not v or (p.get("notional") or 0) <= 0:
            continue
        key = (p["side"], round(v / band))
        b = buckets.setdefault(key, {"side": p["side"], "total": 0.0,
                                     "count": 0, "_pxsz": 0.0})
        b["total"] += p["notional"]
        b["count"] += 1
        b["_pxsz"] += v * p["notional"]        # büyüklükle ağırlıklı fiyat
    out = []
    for b in buckets.values():
        out.append({"price": round(b["_pxsz"] / b["total"], 6),
                    "side": b["side"], "total": round(b["total"], 2),
                    "count": b["count"], "label": _usd(b["total"])})
    out.sort(key=lambda x: -x["total"])
    return out[:top]


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
                     "price": p["entry_px"], "notional": p["notional"],
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
    for side in ("short", "long"):
        _hit_bands([b for b in bars if b["side"] == side])
    # en büyük 3 pozisyona seçici doğrudan etiket
    labels = []
    taken = [yf(mark) - 6]      # 'şimdi X' yazısının TABANI da yer kaplıyor
    for b in bars[:3]:
        base = _label_base(b["y"] + 3.5, taken)
        if base is None:
            continue
        taken.append(base)
        short_side = b["side"] == "short"
        txt = b["tip"].split(" · ")[1]
        wpx = len(txt) * 6.2
        if short_side:
            x, anchor = b["x"] - 6, "end"
            if x - wpx < 6:                     # solda sığmıyor → bar'ın içine
                x, anchor = b["x"] + 6, "start"
        else:
            x, anchor = b["x"] + b["w"] + 6, "start"
            if x + wpx > W - 6:                 # sağda sığmıyor → bar'ın içine
                x, anchor = b["x"] + b["w"] - 6, "end"
        labels.append({"y": base, "x": x, "anchor": anchor, "text": txt})
    ticks = [{"v": _px(lo + (hi - lo) * i / 4), "y": round(yf(lo + (hi - lo) * i / 4), 1)}
             for i in range(5)]
    return {"W": W, "H": H, "cx": cx, "left": left, "bars": bars, "ticks": ticks,
            "labels": labels, "mark_y": round(yf(mark), 1), "mark_txt": _px(mark),
            "lo": lo, "hi": hi, "top": top, "bot": bot,
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
    # Açık yönlendirme + token sızıntısını engelle: '//evil' ve '/\evil' gibi
    # protokol-göreli hedefler startswith('/')'i geçip başka origin'e (URL'de
    # key=DASHBOARD_TOKEN ile) gidiyordu. Yalnız tek '/' ile başlayan yol kabul.
    if not nxt.startswith("/") or nxt.startswith("//") or nxt.startswith("/\\"):
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

    # Tek hisse uzmanları + fill sayacı: süpürücü arka planda hesaplayıp kv'ye
    # yazar (sweeper.compute_specialists) — istek başına milyonlarca satırlık
    # fills taraması yapılmaz (ilk açılıştaki 30-40 sn'lik beyaz ekranın nedeni).
    sym_map = {t["coin"]: t["symbol"] for t in universe}
    specialists = await kv_get("specialists_cache") or []
    fills_n = await kv_get("fills_count")
    if fills_n is None:
        fills_n = "…"  # ilk boot: süpürücü daha saymadı

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
                        # güncel fiyat yalnız 'dist' hesabında kullanılıp
                        # düşüyordu; ipucunda "şimdi X → liq Y" diyebilmek için
                        # satırda da dursun (ek sorgu yok)
                        "mark": mark,
                        "liq_px": r["liq_px"], "dist": dist})
    for r in lw_rows:
        if (r["address"], r["coin"]) in seen_keys or r["last_dist"] is None:
            continue
        if r["last_dist"] > cfg.max_liq_distance_pct:
            continue
        liq_map.append({"symbol": r["coin"].split(":")[-1], "coin": r["coin"],
                        "address": r["address"], "side": r["side"],
                        "notional": r["notional"], "liq_px": r["liq_px"],
                        "mark": marks.get(r["coin"]),
                        "dist": r["last_dist"]})
    liq_map.sort(key=lambda x: x["dist"])
    liq_map = liq_map[:20]

    # Likidasyon duvarları (küme özeti) — tweet'teki heatmap okuması
    from ..radar.liqwatch import find_clusters
    liq_walls = (await find_clusters(cfg))[:4]

    # Emir defteri duvarları (bekleyen dev emirler — SPCX $202M tarzı). Tazelik
    # penceresi tarama periyoduna göre ölçeklenir (sabit 900 değil): wall_poll_sec
    # 900'ün üstüne çıkarılınca panel her taramadan 15 dk sonra boşalmasın.
    wall_window = max(900, int(cfg.wall_poll_sec) * 3)
    async with db() as conn:
        cur = await conn.execute(
            """SELECT b.*, t.symbol FROM book_walls b JOIN tickers t ON t.coin=b.coin
               WHERE b.active=1 AND b.last_ts >= ? AND b.notional >= ?
               ORDER BY b.notional DESC LIMIT 10""",
            (ts_now - wall_window, cfg.wall_min_usd))
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
                       "et": e.get("et"),
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

    # 👣 Aktif takipler: kullanıcının izlediği balina pozları (ilerleme ile)
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM trackers WHERE active=1 ORDER BY id DESC LIMIT 8")
        trackers = [dict(r) for r in await cur.fetchall()]
    for t in trackers:
        base = float(t.get("base_szi") or 0)
        last = float(t["last_szi"] if t.get("last_szi") is not None else base)
        closed = ((base - last) / base * 100) if base > 0 else 0
        t["closed_pct"] = closed
        t["prog"] = (f"%{closed:.0f} kapandı" if closed > 1
                     else (f"%{abs(closed):.0f} büyüttü" if closed < -1 else "değişim yok"))
        t["days_left"] = max(0, (int(t.get("expires_ts") or 0) - ts_now) // 86400)
        t["propr"] = propr_listed(t.get("symbol") or "")

    # 🕐 Saati gelenler: şu saati tarihsel olarak güçlü hisseler
    hmap = await hourstats.all_stats()
    et_hour = datetime.now(hourstats.ET).hour
    hot_hours = hourstats.hot_now(hmap, et_hour)[:8]
    for h in hot_hours:
        h["symbol"] = sym_map.get(h["coin"], h["coin"].split(":")[-1])
        h["propr"] = propr_listed(h["symbol"])
    n_hstats = sum(1 for s in hmap.values() if s and not s.get("empty"))
    # 🌙 Seans karnesi: hareketin ne kadarı borsa KAPALIYKEN oldu (yön ayrımlı).
    # Aynı hmap — ek istek yok; yeni şemayla henüz tazelenmemişler atlanır.
    session_rows = hourstats.session_ranking(hmap, sym_map)
    for r in session_rows:
        r["propr"] = propr_listed(r["symbol"])
    session_rows = session_rows[:20]

    collector = getattr(request.app.state, "collector", None)
    health_state = await kv_get("health_state") or {}
    health_state = {n: st for n, st in health_state.items()
                    if not (st or {}).get("pending")}
    return _render(request, "index.html", {
        "universe": universe, "events": events, "strip_days": strip_days, "ecards": ecards,
        "winners": winners, "archive": archive,
        "recent_big": recent_big, "suspicious": suspicious, "specialists": specialists,
        "liq_map": liq_map, "liqmin": liqmin, "liq_walls": liq_walls,
        "book_walls": book_walls,
        "trackers": trackers,
        "hot_hours": hot_hours, "tsi_now": datetime.now(TR).hour,
        "n_hstats": n_hstats, "session_rows": session_rows,
        "has_channel": bool(cfg.telegram_channel_id) and request.app.state.bot is not None,
        "hot_result": request.query_params.get("hot"),
        "liq_chips": [(100_000, "100K+"), (250_000, "250K+"), (1_000_000, "1M+"),
                      (5_000_000, "5M+"), (30_000_000, "30M+")],
        "stats": {"fills": fills_n, "addrs": addr_n, "watch": watch_n,
                  "ws": "🟢 bağlı" if collector and collector.connected else "🔴 kopuk",
                  "health_problems": sorted(health_state.keys())},
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
        # notifier geç (bot DEĞİL): _alert_new_big notifier.send(kind, priority=,
        # key=) çağırır; bot geçilince TypeError yutulup sayfa-tetikli 'yeni büyük
        # poz' alarmı hiç gitmiyordu (arka plan autoscan doğru notifier alıyor).
        autoscan.kick(cfg, request.app.state.client, coin, coin_dex(coin),
                      request.app.state.notifier)
        scanning = True

    # Saat istatistiği: hazırsa göster, yoksa arka planda hazırlat
    hst = await kv_get(f"hstats:{coin}")
    hstats_pending = False
    if not hst or (hst.get("empty") and now() - int(hst.get("ts") or 0) > 86400):
        hourstats.kick(cfg, request.app.state.client, coin)
        hstats_pending = hst is None
    hchart = _hour_chart(hst)
    # Mum grafiği: hazır değilse arka planda hazırlat (hourstats ile aynı akış)
    pxrec = await pricechart.get(coin)
    if not pricechart.fresh(pxrec):
        pricechart.kick(cfg, request.app.state.client, coin)
    pxchart = bool((pxrec or {}).get("candles"))
    now_verdict = None
    if hchart:
        et_h = datetime.now(hourstats.ET).hour
        v, b = hourstats.verdict(hst, et_h)
        now_verdict = {"v": v, "b": b, "tsi_now": datetime.now(TR).hour}

    return _render(request, "coin.html", {
        "hstats": hst if hchart else None, "hchart": hchart,
        "hstats_pending": hstats_pending, "now_verdict": now_verdict,
        "ticker": t, "symbol": t["symbol"], "coin": coin, "summ": summ,
        "rows": rows[:50], "liq_rows": liq_rows, "fills": fills, "event": ev,
        "long_total": lo, "short_total": sh, "scanned_ts": scanned_ts,
        "scanning": scanning, "max_liq": cfg.max_liq_distance_pct,
        "tg": request.query_params.get("tg"),
        "has_bot": request.app.state.bot is not None,
        "chart": _entry_chart(rows, summ.get("mark")),
        "liqchart": _liq_chart(rows, summ.get("mark"), cfg.max_liq_distance_pct),
        "bwalls": bwalls,
        "pxchart": pxchart,
        "tv_sym": tv_symbol(t["symbol"]) if cfg.show_tradingview else None,
        "propr": propr_listed(t["symbol"]),
        "n_long": sum(1 for p in rows if p["side"] == "long"),
        "n_short": sum(1 for p in rows if p["side"] == "short"),
    })


@router.get("/t/{symbol}/chart.json")
async def coin_chart_json(request: Request, symbol: str):
    """Mum grafiği verisi. Sayfa HTML'ine 720 mum gömmek ilk yüklemeyi
    şişirirdi — grafik kendi verisini ayrı çeker."""
    _guard(request)
    t = await find_ticker(symbol)
    if not t:
        raise HTTPException(404, "coin yok")
    cfg = request.app.state.cfg
    coin = t["coin"]
    summ = await metrics.summary(coin)
    mark = summ.get("mark")

    rec = await pricechart.get(coin)
    if not pricechart.fresh(rec):
        client = getattr(request.app.state, "client", None)
        if client:
            pricechart.kick(cfg, client, coin)
    candles = (rec or {}).get("candles") or []
    if not candles:
        return JSONResponse({"pending": not (rec or {}).get("error"),
                             "candles": [], "walls": [], "entries": [],
                             "hours": [], "earnings": None, "mark": mark})

    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM positions_current WHERE coin=? ORDER BY notional DESC LIMIT 100",
            (coin,))
        rows = [dict(r) for r in await cur.fetchall()]
        # Grafiğin kapsadığı aralıktaki AÇIKLANMIŞ bilançolar → mum üstü işareti
        first_day = datetime.fromtimestamp(candles[0]["t"], hourstats.ET).strftime("%Y-%m-%d")
        cur = await conn.execute(
            "SELECT date_et, hour_hint, exact_ts FROM earnings_events"
            " WHERE coin=? AND evaluated=1 AND date_et>=? ORDER BY date_et",
            (coin, first_day))
        past = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT * FROM earnings_events WHERE coin=? AND date_et>=date('now','-1 day')"
            " ORDER BY date_et LIMIT 1", (coin,))
        nxt = await cur.fetchone()

    from ..earnings.calendar import event_ts_estimate
    past_ts = [ts for ts in (event_ts_estimate(e) for e in past) if ts]
    ev = annotate([dict(nxt)])[0] if nxt else None

    # Güçlü/zayıf saat şeridi: mumun ET saatinin tarihsel karnesi
    hst = await kv_get(f"hstats:{coin}")
    hours = []
    if hst and not hst.get("empty") and hst.get("hours"):
        for h in range(24):
            v, _b = hourstats.verdict(hst, h)
            hours.append({"et": h, "v": v})

    # HL'ye ulaşılamadığında eski mumlar korunuyor (pricechart._keep_or_mark).
    # Bunu SÖYLEMEZSEK kullanıcı bayat grafiği taze sanır — son mumun yaşını geç.
    last_ts = candles[-1]["t"] if candles else 0
    return JSONResponse({
        "pending": False,
        "candles": candles,
        "mark": mark,
        "stale": bool((rec or {}).get("stale")),
        "last_ts": last_ts,
        "age_min": max(0, (now() - last_ts) // 60) if last_ts else None,
        "walls": _levels(rows, mark, "liq_px"),
        "entries": _levels(rows, mark, "entry_px"),
        "hours": hours,
        "earnings": ({"tsi": ev["tsi"], "et": ev.get("et"), "icon": ev["icon"],
                      "when": ev["when_txt"], "countdown": ev["countdown"],
                      "passed": ev["passed"], "ts": ev["report_ts"]} if ev else None),
        "past_earnings": past_ts,
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
    live_fail = 0
    for dex in [*cfg.equity_dexes, ""]:
        try:
            state = await client.clearinghouse(addr, dex)
        except Exception as e:
            # Sessizce atlanınca sayfa EKSİK listeyi tam sanıp gösteriyordu;
            # hepsi düşerse "bu balinanın pozu yok" diye okunuyordu.
            live_fail += 1
            log.warning("balina sayfası %s… dex=%r sorgusu düştü: %s", addr[:10], dex, e)
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
    # coin → sembol: balina sayfası ÇIKMAZ SOKAKTI (coin hücreleri düz metin).
    # Ama burada ANA dex de taranıyor (BTC/ETH…) — onları /t/'ye bağlarsak
    # "evrende yok" ölü sayfasına düşer. Yalnız tickers'ta olan bağlanır.
    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol FROM tickers")
        sym_map = {r["coin"]: r["symbol"] for r in await cur.fetchall()}
    return _render(request, "whale.html", {
        "address": addr, "arow": arow, "live": live, "fills": fills, "snaps": snaps,
        "live_fail": live_fail, "live_dex_n": len(cfg.equity_dexes) + 1,
        "linked": linked, "sym_map": sym_map,
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


@router.post("/saatler/gonder")
async def hot_hours_send(request: Request):
    """Panelden seçilen 'saati gelenler'i yayın kanalına gönder (admin)."""
    from ..telegram import format as fmt
    _guard(request)
    _require_admin(request)
    cfg = request.app.state.cfg
    bot = request.app.state.bot
    form = await request.form()
    coins = [c for c in form.getlist("coins") if c]

    def back(flag: str):
        return RedirectResponse(f"/{_keyq(request, f'hot={flag}')}", status_code=303)

    if not cfg.telegram_channel_id:
        return back("err_ch")
    if not bot:
        return back("err_bot")
    if not coins:
        return back("err_empty")
    et_hour = datetime.now(hourstats.ET).hour
    universe = await get_universe()
    sym_map = {t["coin"]: t["symbol"] for t in universe}
    # Verdict GÖNDERİM ANINDA yeniden kontrol edilir (ortak yardımcı; otomatik
    # yayın döngüsü de aynı mantığı kullanıyor)
    entries = await hourstats.channel_entries(sym_map, et_hour, coins, limit=len(coins))
    if not entries:
        return back("err_empty")
    text = fmt.hot_hours_channel(entries, datetime.now(TR).hour)
    ok = await bot.send(text, cfg.telegram_channel_id)
    return back("ok" if ok else "err")


@router.get("/ai")
async def ai_page(request: Request):
    """AI analist: ürettiği hipotezler ve KENDİ sicili.

    Karne en üstte: uyduran model ilk bakışta görünür. Sayfa AI'a bağımlı
    değil — model hiç çalışmasa da boş durum ve hata metni okunur kalır.
    """
    _guard(request)
    cfg = request.app.state.cfg
    from ..ai import analyst as ai_analyst
    from ..ai.schema import METRICS
    rec = await ai_analyst.record()
    budget = await ai_analyst.budget_state(cfg)
    async with db() as conn:
        cur = await conn.execute(
            """SELECT * FROM ai_hypotheses WHERE status='open'
               ORDER BY resolve_ts LIMIT 40""")
        open_h = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute(
            """SELECT * FROM ai_hypotheses WHERE status!='open'
               ORDER BY resolved_ts DESC LIMIT 40""")
        done_h = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT * FROM ai_observations ORDER BY ts DESC LIMIT 25")
        obs = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT * FROM ai_runs ORDER BY ts DESC LIMIT 8")
        runs = [dict(r) for r in await cur.fetchall()]
    for h in open_h + done_h:
        h["symbol"] = (h.get("subject_coin") or "").split(":")[-1]
        h["metric_desc"] = METRICS.get(h.get("metric") or "", ("", ""))[1]
        # Hacim bazı 5.65795e+08 diye görünüyordu; metrik başına birim farklı
        # olduğu için para biçimlendiricisi ($ ekler) uygun değil — sade sayı.
        h["baseline_txt"] = _plain(h.get("baseline"))
        h["measured_txt"] = _plain(h.get("measured"))
    # Sonuç şeridi SORGU parametresinden geliyor; sayfa yenilenince eski sonuç
    # ekranda kalıp yalan söylüyordu ("✗ başarısız" derken son iki tur ✓).
    # Yalnız son turla TUTARLIYSA göster.
    ai_result = request.query_params.get("ai") or ""
    if ai_result and runs:
        claims_err = ai_result.startswith("err")
        if claims_err != (not runs[0]["ok"]):
            ai_result = ""
    return _render(request, "ai.html", {
        "rec": rec, "budget": budget, "open_h": open_h, "done_h": done_h,
        "obs": obs, "runs": runs,
        "ai_result": ai_result,
        "enabled": bool(cfg.ai_enabled), "has_key": bool(cfg.ai_api_key),
        "model": cfg.ai_model,
        "interval_h": round(cfg.ai_interval_sec / 3600, 1),
    })


@router.post("/ai/run")
async def ai_run_now(request: Request):
    """AI turunu ELLE çalıştır (yönetici).

    Kurulumun doğrulanabildiği tek an burası: yanlış anahtar aksi hâlde ancak
    ilk zamanlı turda (en kötü 2 saat sonra) `ai_runs.err`'e düşerdi.

    Bütçe kapısını ATLAMAZ — `run_once` kendi tavanını kontrol eder; aksi hâlde
    düğmeye basa basa bedava katman yakılabilirdi.
    """
    _guard(request)
    _require_admin(request)
    cfg = request.app.state.cfg
    from ..ai import analyst as ai_analyst
    try:
        await ai_analyst.resolve_due()
        r = await ai_analyst.run_once(cfg, request.app.state.session)
    except Exception as e:
        log.exception("elle AI turu hatası")
        return RedirectResponse(f"/ai{_keyq(request, 'ai=err')}", status_code=303)
    if r.get("skipped"):
        note = f"ai=skip:{r['skipped']}"
    elif r.get("error"):
        note = "ai=err"
    else:
        note = f"ai=ok:{r.get('hypotheses', 0)}:{r.get('observations', 0)}"
    return RedirectResponse(f"/ai{_keyq(request, note)}", status_code=303)


@router.get("/tani")
async def diag_page(request: Request):
    """Tanı dökümü — DÜZ METİN, kopyala-yapıştır için.

    Neden HTML değil: bu sayfanın işi okunmak değil, TAŞINMAK. Ekran görüntüsü
    yerine tek blok metin gönderilebilsin diye düz metin; tarayıcıda da
    Ctrl+A/Ctrl+C ile bir kerede alınır.

    `_guard` ile korumalı: içinde ayar değerleri ve hata metinleri var.
    Kimlik doğrulamasız `/health` uç noktasına DOKUNULMUYOR — o kasten açık.
    """
    _guard(request)
    from ..diag import report
    full = request.query_params.get("full") in ("1", "true", "evet")
    try:
        text = await report(request.app.state.cfg, request.app.state, full=full)
    except Exception as e:
        log.exception("tanı dökümü üretilemedi")
        text = f"TANI DÖKÜMÜ ÜRETİLEMEDİ: {type(e).__name__}: {e}\n"
    return Response(text, media_type="text/plain; charset=utf-8")


@router.get("/devler")
async def lowvol_page(request: Request):
    """Sessiz sular: düşük hacimli hisselerdeki dev pozisyonlar + OI hakimleri."""
    _guard(request)
    cfg = request.app.state.cfg
    try:
        minv = float(request.query_params.get("min") or cfg.lowvol_min_notional)
    except ValueError:
        minv = cfg.lowvol_min_notional
    rows = await lowvol.dominants(cfg)
    rows = [p for p in rows if p["notional"] >= minv]
    humans = [p for p in rows if not p["entity"]]
    bots = [p for p in rows if p["entity"]]
    # Tüm Hyperliquid'in en büyükleri (derinlik/hacim filtresi YOK).
    # 'minv' GEÇİLMEZ: o çip sessiz-sular tablosunun filtresi; buraya
    # uygulanınca panel "filtre yok" diyip aslında filtreleniyordu ve boş
    # durum (filtreli listeye bakar) başlıktaki sayıyla (filtresiz) çelişiyordu.
    big_live = await bigpos.live_big(150)
    big_rec = await bigpos.record_big(150)
    big_stats = await bigpos.stats(cfg)
    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol FROM tickers")
        sym_map = {r["coin"]: r["symbol"] for r in await cur.fetchall()}
    return _render(request, "devler.html", {
        "sym_map": sym_map,
        "humans": humans[:400], "bots": bots[:60],
        "big_live": big_live, "big_rec": big_rec, "big_stats": big_stats,
        "n_humans": len(humans), "n_bots": len(bots),
        "minv": minv,
        "min_chips": [(250_000, "250K+"), (1_000_000, "1M+"),
                      (5_000_000, "5M+"), (10_000_000, "10M+")],
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
    bad_raw = request.query_params.get("bad") or ""
    bad_labels = [EDITABLE_FIELDS[n]["label"] for n in bad_raw.split(",")
                  if n in EDITABLE_FIELDS]
    return _render(request, "settings.html", {
        "groups": groups, "saved": saved, "bad": bad_labels,
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
    invalid: list[str] = []
    for name, spec in EDITABLE_FIELDS.items():
        is_str = spec["type"] == "str"
        if spec["type"] == "bool":
            raw = "1" if form.get(name) else "0"
        elif is_str:
            # str alanlar boş da OLABİLİR (ör. exclude_symbols'ı boşaltıp BIRD'ü
            # geri almak). form'da varsa (None değil) değeri — boş dahil — geçerli.
            if name not in form:
                continue
            raw = (form.get(name) or "").strip()
        else:
            raw = (form.get(name) or "").strip()
        try:
            conv = convert_value(spec["type"], raw) if (raw or is_str) else None
        except (TypeError, ValueError):
            invalid.append(name)  # bozuk değer — 'kaydedildi' deme, kullanıcıya söyle
            continue
        default = cfg.env_default(name)
        # str: boş '' geçerli bir değerdir (default'a EŞİT değilse override tut).
        # sayısal/bool: boş → varsayılana dön.
        if (conv is None) or (conv == default):
            if name in overrides:
                del overrides[name]
                setattr(cfg, name, default)
                cfg.overrides.pop(name, None)
            continue
        overrides[name] = raw
    await kv_set("settings_overrides", overrides)
    cfg.apply_overrides(overrides)
    flag = "saved=1" if not invalid else "saved=1&bad=" + ",".join(invalid)
    return RedirectResponse(f"/settings?{flag}" + (f"&key={key}" if key else ""),
                            status_code=303)
