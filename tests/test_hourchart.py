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
