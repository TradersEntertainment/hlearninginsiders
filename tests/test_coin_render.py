"""coin.html'i uygulamanın Jinja ortamıyla, DB'siz render et.

Şablon hataları (Undefined üstünde aritmetik vb.) bugüne kadar hiçbir testte
yakalanmıyordu: eski şemalı bir saat istatistiği kaydı canlıda /t/GME'yi 500'e
düşürdü. Bu test kayıt şekillerini ve panel hata dallarını sabitler."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-render.db")
from app.radar import hourstats as hs, liqmap                  # noqa: E402
from app.web.routes import templates                            # noqa: E402

ADDR = "0x" + "a" * 40


def hours():
    return [{"et": e, "tsi": (e + 7) % 24, "avg": 0.1 if e % 2 else -0.05,
             "win": 55.0, "n": 70} for e in range(24)]


V1 = {"hours": hours(), "open_ret": 12.0, "closed_ret": 3.0, "days": 60,
      "best": [], "worst": [], "ts": 1}
V2 = {**V1, "open_up": 1, "open_dn": -1, "closed_up": 1, "closed_dn": -1,
      "n_open": 300, "n_closed": 1100, "v": hs.HSTATS_V}
ROWS = [{"address": ADDR, "side": "long", "notional": 1e6, "entry_px": 95.0, "leverage": 5,
         "liq_px": 80.0, "upnl": 100.0, "ts": 1, "score": 10, "reasons": "", "liq_dist": 20.0,
         "account_value": None, "account_ts": None, "opened_ts": None, "first_seen_ts": None,
         "last_add_ts": None, "last_trim_ts": None}]


class _Req:
    url = type("U", (), {"path": "/t/GME"})()


def ctx(hst, **over):
    hchart = hs.chart_cols(hst)
    base = dict(request=_Req(), k="", is_admin=False, has_pw=False, ticker={"coin": "xyz:GME"},
                symbol="GME", coin="xyz:GME", kind="equity", mark_src="metrics", tz=None,
                summ={"mark": 100.0, "oi_ntl": 1e6, "funding": 0.0001, "day_volume": 1e6,
                      "oi_change_pct": None, "px_change_pct": None},
                rows=ROWS, liq_rows=ROWS, fills=[], event=None, long_total=1e6, short_total=0,
                scanned_ts=None, scanning=False, max_liq=50.0, tg=None, has_bot=False,
                entry=liqmap.build_entry(ROWS, 100.0), hs_min_n=hs.MIN_N,
                liq=liqmap.build(ROWS, 100.0, 50.0), bwalls=[], pxchart=False, tv_sym=None,
                propr=False, n_long=1, n_short=0, panel_err={},
                hchart=hchart, hmeta=hs.chart_meta(hst) if hchart else None,
                hstats_pending=hst is None,
                now_verdict=({"v": "nötr", "b": hst["hours"][0], "tsi_now": 7} if hchart else None))
    base.update(over)
    return base


def render(**kw):
    return templates.env.get_template("coin.html").render(**kw)


def test_renders_every_hstats_shape():
    for name, hst in (("v2", V2), ("v1", V1), ("none", None), ("empty", {"empty": True}),
                      ("bare", {"hours": hours()})):
        html = render(**ctx(hst))
        assert "Hangi saatte" in html, name
    html = render(**ctx(V1))
    assert "eski şemada" in html and "1680 mum" in html


def test_panel_error_branches_render():
    html = render(**ctx(V2, panel_err={"hours": "KeyError: 'x'", "entry": "boom", "liq": "bam"},
                        hchart=None, hmeta=None, entry=None, liq=None))
    assert html.count("çizilemedi") == 3 and "KeyError" in html


def test_no_positions_no_mark():
    html = render(**ctx(None, rows=[], liq_rows=[], entry=None, liq=None, long_total=0,
                        summ={"mark": None, "oi_ntl": None, "funding": None, "day_volume": None,
                              "oi_change_pct": None, "px_change_pct": None}))
    assert "Güncel fiyat yok" in html and "ölçüm yok" in html


CROW = [{"address": ADDR, "side": "short", "notional": 7e5, "entry_px": 0.0031, "leverage": 20,
         "liq_px": 0.00328, "upnl": 0.0, "ts": 1, "reasons": "", "liq_dist": 2.5, "score": None,
         "account_value": 2.4e6, "account_ts": 1, "opened_ts": None, "first_seen_ts": None,
         "last_add_ts": None, "last_trim_ts": None, "entity": None}]


def test_crypto_kind_renders():
    """Ana dex coin: skor/tarama kolonları yok, tazele butonu var, duvar paneli nedenli boş."""
    html = render(**ctx(V2, kind="crypto", mark_src="ctx", ticker={"coin": "PUMP", "kind": "crypto"},
                        symbol="PUMP", coin="PUMP", rows=CROW, liq_rows=CROW,
                        entry=liqmap.build_entry(CROW, 0.0032), liq=liqmap.build(CROW, 0.0032, 50.0),
                        summ={"mark": 0.0032, "oi_ntl": 1e6, "funding": 0.0001, "day_volume": 5e6,
                              "oi_change_pct": None, "px_change_pct": 3.1},
                        long_total=0, short_total=7e5, n_long=0, n_short=1, tz="ok", scanned_ts=1))
    assert "ana dex kripto" in html and "Defterleri tazele" in html and "Defterler tazelendi" in html
    assert "<th>Skor</th>" not in html and "Şüphe nedenleri" not in html and "Yeniden tara" not in html
    assert "Ana dex defterleri taranmıyor" in html and "son ölçüm" in html
    assert "kripto 7/24" in html and "Likidasyon haritası" in html and "pxlegend" in html
    html2 = render(**ctx(V2, kind="crypto", mark_src="candle", ticker={"coin": "PUMP", "kind": "crypto"},
                         symbol="PUMP", coin="PUMP", rows=[], liq_rows=[], entry=None, liq=None))
    assert "son mumun kapanışı" in html2


def test_not_found_renders():
    base = dict(request=_Req(), k="", is_admin=False, has_pw=False, ticker=None, symbol="XXX")
    html = render(**base, crypto_n=0)
    assert "bulunamadı" in html and "0 coin biliniyor" in html
    html = render(**base, crypto_n=180)
    assert "180 ana dex coini aranabilir" in html and "kPEPE" in html


if __name__ == "__main__":
    for f in (test_renders_every_hstats_shape, test_panel_error_branches_render, test_no_positions_no_mark,
              test_crypto_kind_renders, test_not_found_renders):
        f(); print("✓", f.__name__)
