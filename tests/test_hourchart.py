"""hourstats.chart_cols — saat grafiğinin saf sütun verisi."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.radar import hourstats as hs


def stats(avgs=None, ns=None):
    hours = []
    for et in range(24):
        tsi = (et + 7) % 24
        hours.append({"et": et, "tsi": tsi, "avg": (avgs or {}).get(et, 0.05 if et % 2 else -0.03),
                      "win": 55.0, "n": (ns or {}).get(et, 80)})
    return {"hours": hours, "days": 60}


def test_24_cols_tsi_order():
    cols = hs.chart_cols(stats())
    assert len(cols) == 24
    assert [c["tsi"] for c in cols] == list(range(24))


def test_pct_scale_and_sign():
    cols = hs.chart_cols(stats({3: 0.40, 4: -0.20}))
    by = {c["et"]: c for c in cols}
    assert by[3]["pct"] == 100.0 and by[3]["pos"]
    assert by[4]["pct"] == 50.0 and not by[4]["pos"]
    assert all(0 <= c["pct"] <= 100 for c in cols)


def test_opacity_floor_and_cap():
    cols = hs.chart_cols(stats(ns={0: 5, 1: 400}))
    by = {c["et"]: c for c in cols}
    assert by[0]["op"] == 0.35 and "az örnek" in by[0]["tip"]
    assert by[1]["op"] == 1.0 and "az örnek" not in by[1]["tip"]


def test_open_band_and_labels():
    cols = hs.chart_cols(stats())
    assert [c["et"] for c in cols if c["open"]] == list(range(9, 16))
    labels = [c for c in cols if c["label"]]
    assert len(labels) == 8 and all(c["tsi"] % 3 == 0 for c in labels)
    assert sum(1 for c in cols if c["l6"]) == 4


def test_empty_or_missing_is_none():
    assert hs.chart_cols(None) is None
    assert hs.chart_cols({"empty": True}) is None
    assert hs.chart_cols({"hours": []}) is None


if __name__ == "__main__":
    for f in (test_24_cols_tsi_order, test_pct_scale_and_sign, test_opacity_floor_and_cap,
              test_open_band_and_labels, test_empty_or_missing_is_none):
        f(); print("✓", f.__name__)


def test_chart_meta_old_schema():
    """HSTATS_V bump'ından önceki kayıt: n_open/n_closed/v yok — sayfa yine çizilmeli."""
    v1 = {"hours": stats()["hours"], "open_ret": 12.0, "closed_ret": 3.0, "days": 60,
          "best": [], "worst": [], "ts": 1}
    m = hs.chart_meta(v1)
    assert m["old_schema"] is True
    assert m["n_candles"] == 24 * 80 and m["n_open"] + m["n_closed"] == m["n_candles"]
    assert m["n_open"] == 7 * 80                 # 09–16 ET = 7 saat kovası
    assert m["days"] == 60 and m["best"] == [] and m["closed_ret"] == 3.0
    # daha da eksik kayıt: seans toplamları yok → None (şablon "ölçüm yok" yazar)
    bare = {"hours": stats()["hours"]}
    m = hs.chart_meta(bare)
    assert m["open_ret"] is None and m["closed_heavy"] is False and m["days"] == 0


def test_chart_meta_current_schema():
    v2 = {"hours": stats()["hours"], "open_ret": 1.0, "closed_ret": 5.0, "days": 70,
          "n_open": 500, "n_closed": 1180, "best": [{"tsi": 1, "avg": 0.2}], "worst": [],
          "v": hs.HSTATS_V, "ts": 5}
    m = hs.chart_meta(v2)
    assert m["old_schema"] is False and m["n_open"] == 500 and m["n_closed"] == 1180
    assert m["closed_heavy"] is True and m["best"][0]["tsi"] == 1
    assert hs.chart_meta(None) is None and hs.chart_meta({"empty": True}) is None
