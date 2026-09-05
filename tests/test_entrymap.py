"""liqmap.build_entry — entry haritası (iki yönlü kovalı satırlar)."""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.radar import liqmap as lm

MARK = 100.0


def pos(addr, side, ntl, dist, lev=3.0, ts=1_800_000_000):
    """dist: girişin şimdiye göre İŞARETLİ uzaklığı (%): + üstte, − altta."""
    return {"address": addr, "side": side, "notional": float(ntl),
            "entry_px": MARK * (1 + dist / 100), "leverage": lev, "ts": ts}


def fixture(n=30, seed=5):
    r = random.Random(seed)
    rows = []
    for i in range(n):
        side = "long" if i % 3 else "short"
        d = r.choice([-12, -6, -3.5, -1.5, -0.3, 0.2, 1.2, 2.5, 4, 8, 18, 34])
        rows.append(pos(f"0x{i:040x}", side, r.randint(50_000, 3_000_000), d))
    return rows


def test_conservation_and_sides():
    rows = fixture()
    e = lm.build_entry(rows, MARK)
    total = sum(p["notional"] for p in rows)
    got = sum(s["long_total"] + s["short_total"] for s in e["up"] + e["down"])
    assert abs(got - total) < 1e-6
    assert e["legend"]["long"]["total"] + e["legend"]["short"]["total"] == total
    # yön = girişin konumu, taraf değil: üst satırlarda long da short da olabilir
    assert any(s["n_long"] for s in e["up"]) and any(s["n_short"] for s in e["up"])


def test_direction_by_entry_not_side():
    rows = [pos("a", "long", 1e6, +3), pos("b", "short", 1e6, +3),
            pos("c", "long", 1e6, -3), pos("d", "short", 1e6, -3)]
    e = lm.build_entry(rows, MARK)
    up_members = sum(s["n"] for s in e["up"])
    down_members = sum(s["n"] for s in e["down"])
    assert up_members == 2 and down_members == 2
    # üstten giren long zararda, alttan giren short zararda
    assert e["meta"]["long_under"] == 1e6 and e["meta"]["n_long_under"] == 1
    assert e["meta"]["short_under"] == 1e6 and e["meta"]["n_short_under"] == 1
    tl = {t["address"]: t["under"] for t in e["top"]["long"]}
    assert tl == {"a": True, "c": False}


def test_edges_and_labels():
    e = lm.build_entry(fixture(), MARK)
    for s in e["up"]:
        assert s["label"].startswith("+") and s["lo"] < s["hi"]
    for s in e["down"]:
        assert s["label"].startswith("-")
    # up sayfada uzaktan yakına, down yakından uzağa
    assert [s["lo"] for s in e["up"]] == sorted([s["lo"] for s in e["up"]], reverse=True)
    assert [s["lo"] for s in e["down"]] == sorted([s["lo"] for s in e["down"]])


def test_far_entries_kept():
    rows = [pos("a", "long", 1e6, -83), pos("b", "short", 5e5, +2)]
    e = lm.build_entry(rows, MARK)
    assert e["meta"]["n"] == 2 and e["meta"]["max_dist"] == 83
    assert sum(s["n"] for s in e["down"]) == 1
    assert e["down"][-1]["hi"] == 83


def test_segments_share_scale():
    rows = [pos("a", "long", 4e6, +1.5), pos("b", "long", 1e6, +1.5),
            pos("c", "short", 2e6, +1.5), pos("d", "short", 5e5, -1.5)]
    e = lm.build_entry(rows, MARK)
    s = [x for x in e["up"] if not x["empty"]][0]
    wl = sum(g["wpct"] for g in s["segs_long"])
    ws = sum(g["wpct"] for g in s["segs_short"])
    assert abs(wl - 100) < 1e-6                 # en büyük kova tarafı = tam ray
    assert abs(ws - 40) < 1e-6                  # ortak $ ölçeği
    assert s["segs_long"][0]["notional"] == 4e6  # en büyük kökte


def test_top_sorted_limited():
    e = lm.build_entry(fixture(40), MARK)
    for side in ("long", "short"):
        t = e["top"][side]
        assert len(t) <= lm.TOP_N
        assert [x["notional"] for x in t] == sorted([x["notional"] for x in t], reverse=True)


def test_collapse_only_when_both_sides_empty():
    rows = [pos("a", "long", 1e6, +0.2), pos("b", "short", 1e6, +40)]
    e = lm.build_entry(rows, MARK)
    assert any(s["collapsed"] for s in e["up"])
    assert all(s["n"] == 0 for s in e["up"] if s["collapsed"])


def test_single_none_and_no_entry():
    assert lm.build_entry([], MARK) is None
    assert lm.build_entry([pos("a", "long", 1e6, 1)], None) is None
    e = lm.build_entry([pos("a", "long", 1e6, -1), {"address": "b", "side": "long",
                                                       "notional": 5, "entry_px": None}], MARK)
    assert e["meta"]["n"] == 1 and e["meta"]["no_entry"] == 1
    full = [s for s in e["down"] if not s["empty"]]
    assert len(full) == 1 and full[0]["segs_long"][0]["wpct"] == 100   # tek pozisyon = tam ray


if __name__ == "__main__":
    for f in (test_conservation_and_sides, test_direction_by_entry_not_side, test_edges_and_labels,
              test_far_entries_kept, test_segments_share_scale, test_top_sorted_limited,
              test_collapse_only_when_both_sides_empty, test_single_none_and_no_entry):
        f(); print("✓", f.__name__)
