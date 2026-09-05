"""Palet sözleşmesi — base.html'deki token'lar WCAG kontrast eşiklerini geçiyor mu.

    • buton yazısı (--btn-fg) buton dolgusu (--btn) üstünde ≥ 4.5:1 (AA, normal metin)
    • metin token'ları (--fg --head --dim --green --red --amber --accent) panel üstünde ≥ 4.5:1
    • çizgi/dolgu token'ları (--btn-line --liq-* --chart-*) panel üstünde ≥ 3:1 (UI bileşeni)

Her iki tema için (gece + okyanus). Panel = --panel rgba'sının --bg1 üstüne bindirilmişi."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "app", "web", "templates", "base.html")


def _tokens(block: str) -> dict:
    return dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;}]+)", block))


def themes() -> dict:
    s = open(BASE, encoding="utf-8").read()
    root = re.search(r":root\{(.*?)\n\}", s, re.S).group(1)
    oky = re.search(r"html\[data-theme=okyanus\]\{(.*?)\n\}", s, re.S).group(1)
    base = _tokens(root)
    out = {"gece": base, "okyanus": {**base, **_tokens(oky)}}
    return out


def _hex(h: str):
    h = h.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgba(v: str):
    m = re.match(r"rgba?\(([^)]+)\)", v.strip())
    parts = [float(x) for x in m.group(1).split(",")]
    return tuple(parts[:3]), (parts[3] if len(parts) > 3 else 1.0)


def _lum(rgb):
    def f(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a, b) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def panel_rgb(t: dict):
    bg = _hex(t["--bg1"])
    (r, g, b), a = _rgba(t["--panel"])
    return tuple(round(c * a + bc * (1 - a)) for c, bc in zip((r, g, b), bg))


TEXT = ("--fg", "--head", "--dim", "--green", "--red", "--red-fg", "--amber", "--accent")
LINES = ("--btn-line", "--liq-long", "--liq-short", "--chart-long", "--chart-short")


def test_button_text_aa():
    for name, t in themes().items():
        c = contrast(_hex(t["--btn"]), _hex(t["--btn-fg"]))
        assert c >= 4.5, f"{name}: --btn-fg/--btn {c:.2f} < 4.5"


def test_text_tokens_on_panel():
    for name, t in themes().items():
        p = panel_rgb(t)
        for tok in TEXT:
            c = contrast(_hex(t[tok]), p)
            assert c >= 4.5, f"{name}: {tok} panel üstünde {c:.2f} < 4.5"


def test_line_and_fill_tokens_on_panel():
    for name, t in themes().items():
        p = panel_rgb(t)
        for tok in LINES:
            c = contrast(_hex(t[tok]), p)
            assert c >= 3.0, f"{name}: {tok} panel üstünde {c:.2f} < 3"


def test_fill_pairs_distinct():
    """Yan yana duran dolgu çiftleri birbirinden ayrılsın (kaba ΔE yerine
    kanal farkı: en az bir kanalda ≥ 60/255)."""
    for name, t in themes().items():
        for a, b in (("--liq-long", "--liq-short"), ("--chart-long", "--chart-short")):
            ca, cb = _hex(t[a]), _hex(t[b])
            assert max(abs(x - y) for x, y in zip(ca, cb)) >= 60, f"{name}: {a}/{b} çok benzer"


if __name__ == "__main__":
    for fn in (test_button_text_aa, test_text_tokens_on_panel,
               test_line_and_fill_tokens_on_panel, test_fill_pairs_distinct):
        fn(); print("✓", fn.__name__)
