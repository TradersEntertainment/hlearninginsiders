"""Bildirim grafiği — mumlar + likidasyon çizgisi + kalan mesafe → PNG (Pillow).

Telegram'a giden liq mesajının yanına küçük bir resim: fiyat nerede, liq
seviyesi nerede, arada ne kadar var. Grafik kütüphanesi yok (sunucuda
tarayıcı yok); dikdörtgen ve çizgi yeter — okunurluk süsten önemli.

SAF: ağ yok, DB yok. `render()` mum listesi ve seviyelerle PNG bayt döner;
Pillow yoksa None döner (mesaj metni yine gider, resim bonus).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

log = logging.getLogger("radar.liqchart")

TR = ZoneInfo("Europe/Istanbul")
W, H = 1200, 675
PAD_L, PAD_R, PAD_T, PAD_B = 28, 262, 92, 58
BG = (24, 20, 38)
GRID = (48, 42, 66)
TEXT = (236, 232, 248)
DIM = (170, 162, 198)
UP = (38, 153, 123)
DOWN = (229, 72, 77)
MARK = (200, 196, 220)
LIQ = {"long": (224, 155, 78), "short": (95, 176, 217)}
AMBER = (255, 206, 138)
BAND = {"long": (224, 155, 78, 40), "short": (95, 176, 217, 40)}
FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "web", "static", "fonts")


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    path = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()


def _px(p: float) -> str:
    if p >= 1000:
        return f"{p:,.0f}"
    if p >= 10:
        return f"{p:.2f}"
    if p >= 1:
        return f"{p:.3f}"
    return f"{p:.5f}".rstrip("0").rstrip(".") if p < 0.01 else f"{p:.4f}"


def _usd(v: float) -> str:
    v = float(v or 0)
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def render(coin: str, candles: list[dict], mark: float | None, levels: list[dict],
           *, interval: str = "15dk", span_txt: str = "son 48 saat",
           target: tuple | None = None) -> bytes | None:
    """PNG bayt. `candles`: [{t,o,h,l,c}] (t saniye, artan). `levels`:
    [{px, side, notional, dist, main}] — `main` olan seviyeye kalan mesafe
    köprüsü çizilir, en çok 4 seviye. `target=(px, label)`: zincir hedefi
    (noktalı amber çizgi + etiket). Mum yoksa ya da Pillow yoksa None."""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    cs = [c for c in candles or [] if c.get("h") and c.get("l")]
    if len(cs) < 3 or not levels:
        return None
    lv = [dict(x) for x in levels if x.get("px")]
    if not lv:
        return None
    main = next((x for x in lv if x.get("main")), lv[0])
    mark = float(mark or cs[-1]["c"])

    # y ekseni: mumlar ∪ seviyeler ∪ fiyat ∪ hedef, %6 pay
    tpx = float(target[0]) if target and target[0] else None
    lo = min(min(c["l"] for c in cs), min(x["px"] for x in lv), mark, *([tpx] if tpx else []))
    hi = max(max(c["h"] for c in cs), max(x["px"] for x in lv), mark, *([tpx] if tpx else []))
    rng = (hi - lo) or (hi * 0.02) or 1.0
    lo, hi = lo - rng * 0.06, hi + rng * 0.06
    plot_l, plot_r, plot_t, plot_b = PAD_L, W - PAD_R, PAD_T, H - PAD_B
    plot_w, plot_h = plot_r - plot_l, plot_b - plot_t

    def y_of(p: float) -> float:
        return plot_b - (p - lo) / (hi - lo) * plot_h

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_title, f_sub, f_lab, f_ax = _font(28, True), _font(17), _font(16, True), _font(15)

    def side_of(x) -> str:
        return "SHORT" if x.get("side") == "short" else "LONG"

    # başlık
    sym = coin.split(":")[-1]
    side_txt = side_of(main)
    where = "üstte" if main.get("side") == "short" else "altta"
    title = f"{sym} · {side_txt} {_usd(main.get('notional'))}"
    d.text((PAD_L, 22), title, fill=TEXT, font=f_title)
    sub = (f"liq {_px(main['px'])} · fiyat {_px(mark)} · %{float(main.get('dist') or 0):.2f} {where}"
           f" · {interval} mumlar · {span_txt}")
    d.text((PAD_L, 60), sub, fill=DIM, font=f_sub)

    # ızgara + sağ eksen etiketleri
    for i in range(6):
        p = lo + (hi - lo) * i / 5
        y = y_of(p)
        d.line([(plot_l, y), (plot_r, y)], fill=GRID, width=1)
        d.text((plot_r + 8, y - 9), _px(p), fill=DIM, font=f_ax)

    # fiyat ↔ liq bandı (yarı saydam)
    over = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(over)
    y_m, y_l = y_of(mark), y_of(main["px"])
    od.rectangle([plot_l, min(y_m, y_l), plot_r, max(y_m, y_l)],
                 fill=BAND.get(main.get("side"), BAND["long"]))
    img.paste(Image.alpha_composite(img.convert("RGBA"), over).convert("RGB"))
    d = ImageDraw.Draw(img)

    # mumlar — sağda boş slot bırakılır: son mum fiyat etiketlerine yapışmasın,
    # "şimdi"den sonrası boş görünsün (TradingView'ın sağ payı gibi)
    n = len(cs)
    gap = max(6, n // 10)
    step = plot_w / (n + gap)
    bw = max(2, int(step * 0.62))
    for i, c in enumerate(cs):
        x = plot_l + step * (i + 0.5)
        col = UP if c["c"] >= c["o"] else DOWN
        d.line([(x, y_of(c["h"])), (x, y_of(c["l"]))], fill=col, width=1)
        y1, y2 = y_of(max(c["o"], c["c"])), y_of(min(c["o"], c["c"]))
        if y2 - y1 < 1:
            y2 = y1 + 1
        d.rectangle([x - bw / 2, y1, x + bw / 2, y2], fill=col)

    # zaman ekseni: 5 etiket (TSİ), mumların kapladığı genişlik üzerinde
    last_x = plot_l + step * (n - 0.5)
    for k in range(5):
        i = int(round((n - 1) * k / 4))
        x = plot_l + step * (i + 0.5)
        d.line([(x, plot_b), (x, plot_b + 5)], fill=GRID, width=1)
        try:
            lbl = datetime.fromtimestamp(int(cs[i]["t"]), TR).strftime("%d.%m %H:%M")
        except Exception:
            lbl = ""
        tw = d.textlength(lbl, font=f_ax)
        d.text((max(plot_l, min(last_x + step * gap / 2 - tw, x - tw / 2)), plot_b + 9),
               lbl, fill=DIM, font=f_ax)

    def dashed(y: float, col, dash: int = 10, width: int = 2):
        x = plot_l
        while x < plot_r:
            d.line([(x, y), (min(x + dash, plot_r), y)], fill=col, width=width)
            x += dash * 2

    def tag(y: float, text: str, col, fg=BG):
        while len(text) > 4 and d.textlength(text, font=f_lab) > PAD_R - 24:
            text = text[:-2] + "…"          # sığmıyorsa kısalt, taşırma
        tw = d.textlength(text, font=f_lab)
        x0 = plot_r + 4
        d.rectangle([x0, y - 13, x0 + tw + 14, y + 13], fill=col)
        d.text((x0 + 7, y - 11), text, fill=fg, font=f_lab)
        return y + 13

    # fiyat çizgisi
    dashed(y_m, MARK, dash=4, width=1)
    used = [tag(y_m, f"fiyat {_px(mark)}", MARK)]

    # liq seviyeleri (en yakın önce çizilir ki etiketi üstte kalsın)
    lv.sort(key=lambda x: (0 if x.get("main") else 1, abs(x["px"] - mark)))
    for x in lv[:4]:
        y = y_of(x["px"])
        col = LIQ.get(x.get("side"), LIQ["long"])
        dashed(y, col, dash=12, width=3 if x.get("main") else 2)
        # etiket çakışmasın: önceki etiketlerle 28px içindeyse aşağı kaydır
        ty = y
        for u in sorted(used):
            if abs(ty - u) < 28:
                ty = u + 28
        used.append(ty)
        tag(ty, f"{side_of(x)} {_usd(x.get('notional'))} · liq {_px(x['px'])}", col)

    # zincir hedefi: noktalı amber çizgi + etiket (defter + havuzla "en az buraya")
    if tpx:
        yt = y_of(tpx)
        dashed(yt, AMBER, dash=4, width=2)
        ty = yt
        for u in sorted(used):
            if abs(ty - u) < 28:
                ty = u + 28
        used.append(ty)
        tag(ty, str(target[1] or f"zincir hedefi {_px(tpx)}"), AMBER)

    # kalan mesafe köprüsü: fiyat ile ana liq arasında dikey çizgi + etiket
    xb = plot_l + 18
    d.line([(xb, y_m), (xb, y_l)], fill=TEXT, width=2)
    for yy in (y_m, y_l):
        d.line([(xb - 6, yy), (xb + 6, yy)], fill=TEXT, width=2)
    txt = f"%{float(main.get('dist') or 0):.2f} kaldı"
    tw = d.textlength(txt, font=f_lab)
    ty = (y_m + y_l) / 2 - 12
    d.rectangle([xb + 10, ty - 4, xb + 10 + tw + 12, ty + 22], fill=BG)
    d.text((xb + 16, ty), txt, fill=TEXT, font=f_lab)

    d.text((PAD_L, H - 24), "HL Insider Radar · gözlem aracıdır, yatırım tavsiyesi değildir",
           fill=DIM, font=f_ax)
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
