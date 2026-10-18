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
                twaps=[], twap_keep_d=30, twap_lookup_min=50_000,
                propr=False, n_long=1, n_short=0, panel_err={},
                hchart=hchart, hmeta=hs.chart_meta(hst) if hchart else None,
                hstats_pending=hst is None, coverage=None,
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
    assert "Ana dex defterleri taranmıyor" not in html and "son ölçüm" in html   # 18.10: kripto defteri de okunuyor
    assert "En kalın emirler ve sahipleri" in html and "Son 15 dakika" in html
    assert html.index("Son 15 dakika") < html.index("En kalın emirler") < html.index("<h2>📋 TWAP emirleri") \
        < html.index("Hangi saatte"), "üç panel en üstte (kullanıcı 18.10)"
    assert "kripto 7/24" in html and "Likidasyon haritası" in html and "pxlegend" in html
    html2 = render(**ctx(V2, kind="crypto", mark_src="candle", ticker={"coin": "PUMP", "kind": "crypto"},
                         symbol="PUMP", coin="PUMP", rows=[], liq_rows=[], entry=None, liq=None))
    assert "son mumun kapanışı" in html2


def test_coverage_tile_renders():
    """Kapsama kutusu: oran + havuz/OI/tarama sayımı; %100 üstü uyarısı; oran yoksa dürüst boş."""
    cov = {"long": 240000.0, "short": 100000.0, "oi_ntl": 2e6, "pct_long": 12.0, "pct_short": 5.0,
           "n": 3, "latest_ts": 1, "over": False, "scan": {"ts": 1, "n_addrs": 312, "n_found": 41}}
    html = render(**ctx(V2, coverage=cov))
    assert "Kapsama (havuz / HL OI)" in html and "L %12 · S %5" in html and "312 adres → 41 poz" in html
    assert "kapsama: long %12 / short %5 (havuz / HL OI)" in html and "bayat satır" not in html
    html = render(**ctx(V2, coverage={**cov, "pct_short": 125.0, "over": True}))
    assert "⚠️ %100 üstü = bayat satır" in html
    html = render(**ctx(V2, coverage={**cov, "oi_ntl": None, "pct_long": None, "pct_short": None}))
    assert "HL OI bilinmiyor" in html and "kapsama: long" not in html
    assert "Kapsama (havuz / HL OI)" in render(**ctx(V2))          # coverage=None → kutu var, oran yok


def test_not_found_renders():
    base = dict(request=_Req(), k="", is_admin=False, has_pw=False, ticker=None, symbol="XXX")
    html = render(**base, crypto_n=0)
    assert "bulunamadı" in html and "0 coin biliniyor" in html
    html = render(**base, crypto_n=180)
    assert "180 ana dex coini aranabilir" in html and "kPEPE" in html


if __name__ == "__main__":
    for f in (test_renders_every_hstats_shape, test_panel_error_branches_render, test_no_positions_no_mark,
              test_crypto_kind_renders, test_coverage_tile_renders, test_not_found_renders):
        f(); print("✓", f.__name__)


def test_top_panels_with_data():
    """Kullanıcı 18.10: coin sayfasının üstünde son 15 dk'nın alım-satımları (adresiyle), en kalın emirler ve
    sahipleri, TWAP'lar. Bant ve defter verisi doluyken çizim; boşken / dinlenmiyorken / kopukken / defter
    okunamamışken nedenli boş (inceleme 18.10: "0.0 adım" + boş tablo, "işlem görülmedi" yerine bilinmiyor).

    Pinlenenler: kapsam (fiyata %) başlıkta; "dolan, şu an emri yok" ayrı; sahip yoksa "sahip bulunamadı";
    emir verisinin yaşı künyede; hisse duvar radarı aynı panelde; bant hatasında çelişkili ikinci metin yok."""
    import time as _t
    from app.hl import collector as col
    from app.radar import tape as tp
    tp.REG.clear()
    tp.HWM.clear()
    t = int(_t.time())
    old_started, old_live = tp.STARTED, col.LIVE
    tp.STARTED, col.LIVE = t - 10_000, None
    try:
        B, S, G = "0x" + "b" * 40, "0x" + "c" * 40, "0x" + "f" * 40
        tp.observe("PUMP", "B", B, S, 0.0042, 2_000_000, t - 30)            # $8.4K alış, satıcı ask'ta pasif
        tp.observe("PUMP", "A", B, S, 0.0041, 3_000_000, t - 20, twap=True)  # $12.3K satış (TWAP), alıcı bid'de pasif
        summ = tp.summary("PUMP", t)
        book = {"coin": "PUMP", "ts": t - 12, "mid": 0.004118, "unit": 1e-05, "unit_txt": "0.00001", "age": 12,
                "cands": 4, "failed": 0, "partial": 1, "err": [], "orders_ts": t - 40,
                "depth": {"reach": {"raw": (-0.47, 0.47), "grouped": (-4.7, 4.66)}},
                "raw": {"bid": [{"px": 0.0041, "usd": 7.78e6, "n": 20, "owners": [(B, 1.2e6)], "unknown_usd": 6.58e6,
                                 "makers": [(B, 12_300.0)], "makers_gone": [(G, 6_000.0)]}], "ask": []},
                "grouped": {"bid": [{"px": 0.0041, "usd": 9.8e6, "n": 197, "owners": [], "unknown_usd": 9.8e6,
                                     "makers": [], "makers_gone": []}], "ask": []},
                "orders": [{"addr": B, "side": "bid", "px": 0.0041, "usd": 1.2e6, "ro": False, "dist_pct": -0.44}]}
        base = dict(kind="crypto", mark_src="ctx", ticker={"coin": "PUMP", "kind": "crypto"}, symbol="PUMP",
                    coin="PUMP", entry=None, liq=None, pxchart=True)
        html = render(**ctx(V2, **base, rows=CROW, liq_rows=CROW,
                            summ={"mark": 0.0041, "oi_ntl": 1e6, "funding": 0.0001, "day_volume": 5e6,
                                  "oi_change_pct": None, "px_change_pct": 3.1},
                            tape=summ, tape_cover=tp.cover(summ), book=book, book_pending=False, listened=True))
        assert "Piyasadan alış" in html and "$8K" in html and "🔹 TWAP" in html and "En çok net alan" in html
        assert "HL arayüzü gibi gruplu · 0.00001 adım" in html and "Tam fiyat · ilk 20 kademe" in html
        assert "$9.80M" in html and "kapsam: fiyatın %4.70 altı – %4.66 üstü" in html, "l2Book kapsamı yazılı"
        assert "bulunamayan $6.58M" in html and "✅" in html and "Bulunan büyük emirler" in html and "dolan:" in html
        assert "dolan, şu an emri yok:" in html and "sahip bulunamadı" in html
        assert "Defter 12 sn önce okundu" in html and "4 aday adresin" in html and "en eskisi 40 sn önce" in html
        assert "HL 100'de kesti" in html and "görülenlerin hepsi sayıldı" in html and "Son 15 dk" in html
        assert "(%-0.44)" in html and "%-0.00" not in html
        # dinlenmiyor / hazırlanıyor
        empty = {"n": 0, "window": 900, "span": 900, "covered_sec": 900, "gaps": [], "gap_sec": 0, "full": True,
                 "restart": False, "live_down": False}
        html2 = render(**ctx(V2, **base, rows=[], liq_rows=[], tape=empty, tape_cover=tp.cover(empty), book=None,
                             book_pending=True, listened=False))
        assert "canlı akışta dinlenmiyor" in html2 and "hazırlanıyor" in html2 and 'data-refresh="60"' in html2
        # dinlenmeyen coinin akış paneli $0 yazmaz (inceleme 18.10): "—" ve nedeni
        flow2 = html2[html2.index('class="tp t-flow"'):html2.index('class="tp t-twap"')]
        assert 'id="fbuy">—<' in flow2 and "dinlenmiyor — bilinmiyor" in flow2 and "$0" not in flow2, flow2[:900]
        # mum önbelleği yoksa grafik beklenir: sayfa 10 sn'de yenilenir (eskiden 60 sn boş kalıyordu)
        nochart = render(**ctx(V2, **{**base, "pxchart": False}, rows=[], liq_rows=[], tape=empty,
                               tape_cover=tp.cover(empty), book=None, book_pending=False, listened=True))
        assert 'data-refresh="10"' in nochart
        # akış şu an kopuk: "işlem görülmedi" değil bilinmiyor
        down = {**empty, "live_down": True, "full": False}
        html3 = render(**ctx(V2, **base, rows=[], liq_rows=[], tape=down, tape_cover=tp.cover(down),
                             book=None, book_pending=False, listened=True))
        assert "Son 15 dk bilinmiyor: canlı akış şu an kopuk" in html3 and "işlem görülmedi" not in html3
        # defter okunamadı (hata sonucu): boş tablo ve "0.0 adım" yok, döngü yok
        bad = {"coin": "PUMP", "ts": t, "age": 3, "err": ["defter okunamadı"], "raw": {"bid": [], "ask": []},
               "grouped": {"bid": [], "ask": []}, "orders": [], "cands": 0, "failed": 0, "partial": 0, "mid": None,
               "unit": 0.0, "unit_txt": "", "depth": {}, "orders_ts": None}
        html4 = render(**ctx(V2, **base, rows=[], liq_rows=[], tape=summ, tape_cover=tp.cover(summ), book=bad,
                             book_pending=False, listened=True))
        assert "Defter okunamadı (defter okunamadı)" in html4 and " adım" not in html4
        assert 'data-refresh="60"' in html4 and "Alış (fiyatın altı)" not in html4
        # bant hatası: uyarı var, "işlem görülmedi" çelişkisi yok
        html5 = render(**ctx(V2, **base, rows=[], liq_rows=[], tape=None, tape_cover=tp.cover(None), book=None,
                             book_pending=False, listened=True, panel_err={"tape": "bozuk"}))
        assert "bant okunamadı: bozuk" in html5 and "işlem görülmedi" not in html5
        # kopukluk bandın içinde: "hepsi sayıldı" denmez
        gap = {**summ, "gaps": [[t - 300, t - 120]], "gap_sec": 180, "full": False}
        html6 = render(**ctx(V2, **base, rows=[], liq_rows=[], tape=gap, tape_cover=tp.cover(gap), book=None,
                             book_pending=False, listened=True))
        assert "canlı akış bu aralıkta 3 dk kopuktu" in html6 and "hepsi sayıldı" not in html6
        # hisse: duvar radarı aynı panelde
        wall = {"side": "bid", "notional": 2.5e6, "sz": 10_000, "px_lo": 99.0, "px_hi": 99.5, "peak_notional": 3e6,
                "dist_pct": -1.2, "address": ADDR, "first_ts": t - 600}
        html7 = render(**ctx(V2, tape=empty, tape_cover=tp.cover(empty), book=None, book_pending=False,
                             listened=True, bwalls=[wall]))
        assert "🧱 Duvar radarı (hisse)" in html7 and "ALIŞ <b>$2.50M</b>" in html7 and "sahibi" in html7
    finally:
        tp.STARTED, col.LIVE = old_started, old_live
        tp.REG.clear()
        tp.HWM.clear()
    print("✅ üst paneller) 15 dk bant + en kalın emirler/sahipler + TWAP en üstte; dinlenmiyor / hazırlanıyor /"
          " kopuk (bilinmiyor) / defter okunamadı / bant hatası / hisse duvarı")
