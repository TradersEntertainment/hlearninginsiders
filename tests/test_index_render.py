"""index.html'i uygulamanın Jinja ortamıyla, DB'siz render et — liq haritası ayrımı.

Pinlenenler:
  • varsayılan görünümde endeks/emtia/FX satırı yok, 📐 çipi sayıyla var, künye "gizli"
  • liqidx açıkken çip `on`, başlık endeks der, liqmin çipleri liqidx'i korur
  • iki görünümün boş durum cümleleri farklı ve dürüst
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-render.db")
from app.web.routes import templates                            # noqa: E402

ADDR = "0x" + "a" * 40


class _Req:
    url = type("U", (), {"path": "/"})()


def row(sym, coin, side, ntl, dist):
    return {"symbol": sym, "coin": coin, "address": ADDR, "side": side, "notional": float(ntl),
            "mark": 100.0, "liq_px": 100.0 * (1 + dist / 100), "dist": dist, "wpct": 50.0, "propr": False}


MAIN = [row("SNDK", "xyz:SNDK", "short", 2e6, 0.8), row("BTC", "BTC", "long", 9e6, 1.4)]
IDX = [row("SP500", "xyz:SP500", "short", 2.4e6, 0.5), row("XYZ100", "xyz:XYZ100", "short", 1.2e6, 1.4)]


def ctx(**over):
    base = dict(request=_Req(), k="", is_admin=False, has_pw=False,
                universe=[], events=[], strip_days=[], ecards=[], crypto_tags=[], crypto_n=0,
                winners=[], archive=[], recent_big=[], suspicious=[], specialists=[],
                liq_map=MAIN, liqmin=250_000.0, liq_walls=[], liqidx=False, liq_idx_n=2, liq_main_n=2,
                book_walls=[], trackers=[], hot_hours=[], tsi_now=12, n_hstats=0, hot_rank=[],
                session_rows=[], has_channel=False, hot_result=None,
                liq_chips=[(100_000, "100K+"), (250_000, "250K+"), (1_000_000, "1M+")],
                stats={"fills": 8, "addrs": 3, "watch": 2, "ws_ok": True, "health_problems": []},
                kpis=[], max_liq=50, wall_min=1_000_000, wall_window_min=45)
    base.update(over)
    return base


def render(**kw):
    return templates.env.get_template("index.html").render(**kw)


def test_default_hides_index_perps():
    html = render(**ctx())
    panel = html.split('id="liqmap"')[1].split("kunye")[0]
    assert 'href="/t/SNDK' in panel and 'href="/t/BTC' in panel and 'href="/t/SP500' not in panel
    assert "📐 endeks/emtia/FX (2)" in html and 'id="liqidx-chip"' in html
    assert "endeks/emtia/FX gizli: 2 pozisyon" in html and "hisse + kripto — patlamaya" in html
    assert 'href="/?liqmin=250000&liqidx=1"' in html and 'class="on" id="liqidx-chip"' not in html
    assert 'href="/?liqmin=100000"' in html, "liqidx kapalıyken liqmin çipleri sade"


def test_index_view():
    html = render(**ctx(liq_map=IDX, liqidx=True))
    assert "SP500" in html and "XYZ100" in html
    assert 'class="on" id="liqidx-chip"' in html and 'href="/?liqmin=250000&liqidx=0"' in html
    assert 'href="/?liqmin=100000&amp;liqidx=1"' in html, "liqmin çipleri liqidx'i korur"
    assert "endeks/emtia/FX (SP500, XYZ100, EUR, CL…) — patlamaya" in html
    assert "hisse + kripto: 2 pozisyon (📐 çipini kapat)" in html


def test_empty_states():
    html = render(**ctx(liq_map=[], liq_main_n=0))
    assert "likidasyona yakın hisse/kripto pozisyonu yok" in html and "e bak (2 pozisyon)" in html
    html2 = render(**ctx(liq_map=[], liq_idx_n=0, liq_main_n=0))
    assert "e bak (" not in html2 and "süpürmenin dolmasını bekle" in html2
    html3 = render(**ctx(liq_map=[], liqidx=True))
    assert "te bu filtrede likidasyona yakın pozisyon yok" in html3 and "hisse + kriptoya dön (2 pozisyon)" in html3
    html4 = render(**ctx(k="?key=abc"))
    assert 'href="/?liqmin=250000&liqidx=1&amp;key=abc"' in html4 and 'href="/?liqmin=100000&amp;key=abc"' in html4


if __name__ == "__main__":
    for f in (test_default_hides_index_perps, test_index_view, test_empty_states):
        f(); print("✓", f.__name__)
