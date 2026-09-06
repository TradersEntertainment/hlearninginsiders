"""Hacim patlaması grafiği — geniş 5 dk mum + hacim barları → PNG (Pillow).

"24 saatin en yüksek 5 dakikalık hacmi" mesajının resmi: üstte fiyat mumları,
altta $ hacim barları; REKOR kovası amber vurgulu, önceki 24 saatin rekoru
kesikli çizgi. Mumlar tarama sırasında zaten elde (ek istek yok).

SAF: ağ yok, DB yok. `render()` PNG bayt döner; mum azsa / Pillow yoksa None.
"""
from __future__ import annotations

import logging
from datetime import datetime
from io import BytesIO

from .liqchart import BG, DIM, DOWN, GRID, TEXT, TR, UP, _font, _px, _usd

log = logging.getLogger("radar.volchart")

W, H = 1800, 720                       # geniş: 288 kova sığsın, bar okunsun
PAD_L, PAD_R, PAD_T, PAD_B = 28, 250, 92, 58
AMBER = (255, 206, 138)
SPAN = 24 * 3600


def _mix(a, b, t: float):
    return tuple(int(a[i] * t + b[i] * (1 - t)) for i in range(3))


def render(coin: str, candles: list[dict], rec: dict, *, day_vol: float | None = None,
           span: int = SPAN, interval_txt: str = "5dk") -> bytes | None:
    """PNG bayt. `candles`: [{t,o,h,l,c,v}] (t saniye, artan; h/l yoksa o/c'den).
    `rec`: find_record çıktısı (bucket_ts, notional, ratio, px, chg_pct).
    Grafik rekor kovasında biter; devam eden mum çizilmez."""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    bucket = int(rec.get("bucket_ts") or 0)
    src = [c for c in candles or [] if c.get("t") is not None and (not bucket or c["t"] <= bucket)]
    if src:
        t_last = src[-1]["t"]
        src = [c for c in src if c["t"] >= t_last - span]
    if len(src) < 12:
        return None
    cs = []
    for c in src:
        o, cl = float(c["o"]), float(c["c"])
        cs.append({"t": int(c["t"]), "o": o, "c": cl,
                   "h": float(c.get("h") or max(o, cl)), "l": float(c.get("l") or min(o, cl)),
                   "usd": float(c.get("v") or 0) * cl})
    n = len(cs)
    rec_i = next((i for i in range(n - 1, -1, -1) if cs[i]["t"] == bucket), n - 1)
    prev_usd = max((c["usd"] for i, c in enumerate(cs) if i != rec_i), default=0.0)
    max_usd = max(cs[rec_i]["usd"], prev_usd) or 1.0

    plot_l, plot_r = PAD_L, W - PAD_R
    plot_w = plot_r - plot_l
    total_h = H - PAD_T - PAD_B
    p_t, p_b = PAD_T, PAD_T + int(total_h * 0.58)          # fiyat paneli
    v_t, v_b = p_b + int(total_h * 0.08), H - PAD_B       # hacim paneli
    lo = min(c["l"] for c in cs)
    hi = max(c["h"] for c in cs)
    rng = (hi - lo) or (hi * 0.02) or 1.0
    lo, hi = lo - rng * 0.05, hi + rng * 0.05

    def y_px(p):
        return p_b - (p - lo) / (hi - lo) * (p_b - p_t)

    def y_vol(u):
        return v_b - u / max_usd * (v_b - v_t) * 0.92

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_title, f_sub, f_lab, f_ax = _font(28, True), _font(17), _font(16, True), _font(15)
    sym = coin.split(":")[-1]
    ratio = rec.get("ratio")
    title = f"{sym} · {interval_txt} hacim rekoru {_usd(rec.get('notional'))}"
    if ratio:
        title += f" (önceki rekorun {float(ratio):.1f}×)"
    d.text((PAD_L, 22), title, fill=TEXT, font=f_title)
    chg = float(rec.get("chg_pct") or 0)
    sub = (f"{interval_txt} mumlar · son 24 saat · kova fiyatı {_px(float(rec.get('px') or cs[-1]['c']))}"
           f" ({chg:+.2f}%)")
    if day_vol:
        sub += f" · 24s hacim {_usd(day_vol)}"
    d.text((PAD_L, 60), sub, fill=DIM, font=f_sub)

    # ızgara + sağ eksen: fiyat (5) ve hacim (3)
    for i in range(6):
        p = lo + (hi - lo) * i / 5
        y = y_px(p)
        d.line([(plot_l, y), (plot_r, y)], fill=GRID, width=1)
        d.text((plot_r + 8, y - 9), _px(p), fill=DIM, font=f_ax)
    for i in range(4):
        u = max_usd * i / 3
        y = y_vol(u)
        d.line([(plot_l, y), (plot_r, y)], fill=GRID, width=1)
        d.text((plot_r + 8, y - 9), _usd(u), fill=DIM, font=f_ax)

    # mumlar + barlar; sağda boş pay (son kova etiketlere yapışmasın)
    gap = max(8, n // 10)
    step = plot_w / (n + gap)
    bw = max(2, int(step * 0.66))
    vol_up, vol_dn = _mix(UP, BG, 0.55), _mix(DOWN, BG, 0.55)
    # Rekor kovası vurgusu MUMLARIN ARKASINDA: soluk, geniş şerit — mum üstte
    # net kalır (eskiden mumun üstüne çizilip son hareketi gizliyordu).
    xr = plot_l + step * (rec_i + 0.5)
    band = max(6, bw * 3)
    d.rectangle([xr - band / 2, p_t, xr + band / 2, p_b], fill=_mix(AMBER, BG, 0.18))
    for i, c in enumerate(cs):
        x = plot_l + step * (i + 0.5)
        up = c["c"] >= c["o"]
        col = UP if up else DOWN
        d.line([(x, y_px(c["h"])), (x, y_px(c["l"]))], fill=col, width=1)
        y1, y2 = y_px(max(c["o"], c["c"])), y_px(min(c["o"], c["c"]))
        if y2 - y1 < 1:
            y2 = y1 + 1
        d.rectangle([x - bw / 2, y1, x + bw / 2, y2], fill=col)
        vcol = AMBER if i == rec_i else (vol_up if up else vol_dn)
        yv = y_vol(c["usd"])
        d.rectangle([x - bw / 2, min(yv, v_b - 1), x + bw / 2, v_b], fill=vcol)

    # önceki 24 saatin rekoru: kesikli çizgi + etiket
    if prev_usd > 0:
        yp = y_vol(prev_usd)
        x = plot_l
        while x < plot_r:
            d.line([(x, yp), (min(x + 10, plot_r), yp)], fill=DIM, width=1)
            x += 20
        # etiket SOLDA: sağdaki "rekor $X" etiketiyle üst üste binmesin
        lbl = f"önceki rekor {_usd(prev_usd)}"
        tw = d.textlength(lbl, font=f_ax)
        d.rectangle([plot_l + 2, yp - 20, plot_l + tw + 14, yp - 2], fill=BG)
        d.text((plot_l + 8, yp - 19), lbl, fill=DIM, font=f_ax)

    # rekor kovası etiketi (bar tepesinin üstünde; sığmazsa yanında)
    yr = y_vol(cs[rec_i]["usd"])
    lbl = f"rekor {_usd(cs[rec_i]['usd'])}" + (f" · {float(ratio):.1f}×" if ratio else "")
    tw = d.textlength(lbl, font=f_lab)
    lx = min(max(plot_l, xr - tw / 2), plot_r - tw - 4)
    ly = max(v_t - 26, yr - 30)
    d.rectangle([lx - 6, ly - 3, lx + tw + 6, ly + 20], fill=AMBER)
    d.text((lx, ly - 1), lbl, fill=BG, font=f_lab)
    d.line([(xr, ly + 20), (xr, yr)], fill=AMBER, width=1)
    # fiyat panelinde mumun DIŞINDA işaret: high'ın üstünde ▼, low'un altında ▲
    rc = cs[rec_i]
    yh, ylw = y_px(rc["h"]), y_px(rc["l"])
    tri = max(5, bw)
    d.polygon([(xr - tri, yh - 15), (xr + tri, yh - 15), (xr, yh - 5)], fill=AMBER)
    d.polygon([(xr - tri, ylw + 15), (xr + tri, ylw + 15), (xr, ylw + 5)], fill=AMBER)

    # zaman ekseni: 5 etiket (TSİ)
    last_x = plot_l + step * (n - 0.5)
    for k in range(5):
        i = int(round((n - 1) * k / 4))
        x = plot_l + step * (i + 0.5)
        d.line([(x, v_b), (x, v_b + 5)], fill=GRID, width=1)
        try:
            lbl = datetime.fromtimestamp(cs[i]["t"], TR).strftime("%d.%m %H:%M")
        except Exception:
            lbl = ""
        tw = d.textlength(lbl, font=f_ax)
        d.text((max(plot_l, min(last_x + step * gap / 2 - tw, x - tw / 2)), v_b + 9),
               lbl, fill=DIM, font=f_ax)
    d.text((PAD_L, H - 24), "HL Insider Radar · hacim = mum hacmi × kapanış ($) · gözlem aracıdır,"
           " yatırım tavsiyesi değildir", fill=DIM, font=f_ax)
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
