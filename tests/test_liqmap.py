"""Likidasyon haritası kovalayıcısı — saf modül, DB yok.

Eski grafik pozisyon başına bir çizgiydi ve çakışanları aşağı itip fiyatını
yalan söylüyordu. Yeni modül kovalıyor; bu paket şunları pinler:
  • hiçbir dolar kaybolmuyor (kova toplamı = pozisyon toplamı)
  • short'lar üstte, long'lar altta; kenarlar SLOT_EDGES
  • kademeli okuma (%2/%5/%10) kova sınırlarıyla birebir
  • duvar/en yakın işaretleri, dilimler, katlama, uçtaki boşların atılması
  • sınır hâlleri: mark yok, pozisyon yok, tek pozisyon, %50 dışı sayılıyor
"""
import random
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.radar import liqmap as lm

MARK = 100.0


def pos(addr, side, ntl, dist, lev=3.0, ts=1_800_000_000):
    """dist: şimdiye uzaklık % (işaretsiz); liq fiyatı yönden türetilir."""
    liq = MARK * (1 + dist / 100) if side == "short" else MARK * (1 - dist / 100)
    return {"address": addr, "side": side, "notional": float(ntl), "liq_px": liq,
            "leverage": lev, "ts": ts}


def fixture(n=40, seed=3):
    r = random.Random(seed)
    rows = []
    for i in range(n):
        side = "short" if i % 5 else "long"          # 32 short, 8 long
        dist = r.choice([0.3, 0.8, 1.5, 2.5, 4.0, 6.0, 9.0, 12.0, 25.0, 45.0])
        rows.append(pos(f"0x{i:040x}", side, r.randint(50_000, 15_000_000), dist))
    return rows


# --------------------------------------------- 1) dolar kaybolmuyor + sayı
def test_conservation():
    rows = fixture()
    out = lm.build(rows, MARK, 50)
    assert out
    tot_pos = sum(p["notional"] for p in rows)
    tot_slot = sum(s["total"] for s in out["up"] + out["down"])
    assert abs(tot_pos - tot_slot) < 1e-6, (tot_pos, tot_slot)
    assert len(out["up"]) <= 11 and len(out["down"]) <= 11
    assert out["legend"]["up"]["n"] + out["legend"]["down"]["n"] == 40
    print(f"✅ koruma) 40 pozisyon → {len(out['up'])}+{len(out['down'])} kova,"
          f" toplam ${tot_slot:,.0f} = pozisyon toplamı")


# ------------------------------------------------ 2) yön ve kenarlar
def test_sides_edges():
    out = lm.build(fixture(), MARK, 50)
    assert all(s["side"] == "short" for s in out["up"])
    assert all(s["side"] == "long" for s in out["down"])
    # up: en uzak ÜSTTE → hi azalıyor; down: en yakın üstte → lo artıyor
    ups = [s for s in out["up"] if not s["collapsed"]]
    assert all(a["hi"] >= b["hi"] for a, b in zip(ups, ups[1:]))
    downs = [s for s in out["down"] if not s["collapsed"]]
    assert all(a["lo"] <= b["lo"] for a, b in zip(downs, downs[1:]))
    edges = set(lm.SLOT_EDGES)
    for s in ups + downs:
        assert s["lo"] in edges and s["hi"] in edges, (s["lo"], s["hi"])
    near = next(s for s in out["down"] if s["nearest"])
    assert near["label"] == "-0…0.5%", near["label"]
    lab = [s["label"] for s in ups if s["lo"] == 1.0][0]
    assert lab == "+1…2%", lab
    # fiyat aralığı yöne göre doğru tarafta
    for s in ups:
        assert s["px_lo"] >= MARK - 1e-9
    for s in downs:
        assert s["px_hi"] <= MARK + 1e-9
    print("✅ yön) short'lar üstte uzaktan yakına, long'lar altta yakından uzağa;"
          " kenarlar SLOT_EDGES; etiketler '+1…2%' biçiminde")


# ------------------------------------------- 3) kademeli okuma KESİN
def test_cascade_exact():
    rows = fixture()
    out = lm.build(rows, MARK, 50)
    for side, key in (("short", "up"), ("long", "down")):
        for c in out["cascade"][key]:
            want = sum(p["notional"] for p in rows if p["side"] == side
                       and abs(MARK - p["liq_px"]) / MARK * 100 <= c["pct"] + 1e-9)
            assert abs(c["total"] - want) < 1e-6, (side, c, want)
            # kova sınırlarıyla birebir: hi <= pct olan kovaların toplamı
            slots = [s for s in out[key] if not s["collapsed"] and s["hi"] <= c["pct"] + 1e-9]
            assert abs(sum(s["total"] for s in slots) - want) < 1e-6
    print("✅ kademe) %2/%5/%10 toplamları hem pozisyonlarla hem kova"
          " sınırlarıyla birebir — kenarlar bilerek buraya oturuyor")


# ------------------------------------------------- 4) duvar eşiği
def test_wall():
    rows = [pos("0xa", "short", 9_000_000, 1.5),      # tek başına %75 → duvar
            pos("0xb", "short", 2_000_000, 6.0),
            pos("0xc", "short", 1_000_000, 12.0)]
    out = lm.build(rows, MARK, 50)
    walls = [s for s in out["up"] if s["wall"]]
    assert len(walls) == 1 and walls[0]["lo"] == 1.0, walls
    # %25 altı duvar DEĞİL
    rows2 = [pos(f"0x{i}", "short", 1_000_000, d) for i, d in
             enumerate([0.3, 1.5, 2.5, 4.0, 9.0])]
    out2 = lm.build(rows2, MARK, 50)
    assert not any(s["wall"] for s in out2["up"]), "eşit dağılımda duvar olmamalı"
    print("✅ duvar) yalnız yön toplamının ≥%25'ini taşıyan kova 🧲 alıyor")


# --------------------------------------------- 5) dilimler
def test_segments():
    rows = [pos(f"0x{i}", "long", n, 1.2) for i, n in
            enumerate([5_000_000, 3_000_000, 900_000, 800_000, 700_000, 600_000,
                       500_000, 400_000])]          # 8 pozisyon aynı kovada
    out = lm.build(rows, MARK, 50)
    s = [x for x in out["down"] if x["n"] == 8][0]
    assert len(s["segs"]) == lm.MAX_SEGMENTS, len(s["segs"])
    assert s["segs"][0]["notional"] == 5_000_000, "en büyük kökte"
    assert s["segs"][-1].get("n") == 3, "kalan 3 pozisyon tek dilimde"
    assert abs(sum(g["wpct"] for g in s["segs"]) - s["wpct"]) < 1e-6
    assert abs(sum(g["notional"] for g in s["segs"]) - s["total"]) < 1e-6
    assert s["wpct"] == 100.0, "tek kova en geniş"
    print("✅ dilim) en büyük kökte, ≤6 dilim + kalan, dilim toplamı = kova")


# ------------------------------------- 6) katlama + uçtaki boşlar
def test_collapse_trim():
    rows = [pos("0xa", "short", 1_000_000, 0.3),
            pos("0xb", "short", 1_000_000, 25.0)]      # arada 7 boş kova
    out = lm.build(rows, MARK, 50)
    labels = [s["label"] for s in out["up"]]
    coll = [s for s in out["up"] if s["collapsed"]]
    assert len(coll) == 1 and coll[0]["lo"] == 0.5 and coll[0]["hi"] == 20.0, labels
    assert out["up"][0]["lo"] == 20.0, "uçtaki boşlar (30…50) atılmalı"
    assert len(out["up"]) == 3, labels
    # 2 boş kova katlanmaz
    rows2 = [pos("0xa", "long", 1_000_000, 0.3), pos("0xb", "long", 1_000_000, 2.5)]
    out2 = lm.build(rows2, MARK, 50)
    assert not any(s["collapsed"] for s in out2["down"])
    assert len(out2["down"]) == 4, [s["label"] for s in out2["down"]]
    print("✅ katlama) ≥3 ardışık boş kova tek satır; uçtaki boşlar atılıyor;"
          " 2 boş katlanmıyor")


# -------------------------------------------------- 7) sıradaki
def test_next():
    out = lm.build(fixture(), MARK, 50)
    for key in ("up", "down"):
        nx = out["next"][key]
        assert len(nx) <= lm.NEXT_N
        assert all(a["dist"] <= b["dist"] for a, b in zip(nx, nx[1:]))
    assert out["next"]["up"][0]["side"] == "short"
    assert out["next"]["down"][0]["side"] == "long"
    print("✅ sıradaki) yön başına en yakın 4, uzaklığa göre sıralı")


# ------------------------------------------- 8) sınır hâlleri
def test_edges():
    assert lm.build(fixture(), None, 50) is None
    assert lm.build(fixture(), 0, 50) is None
    assert lm.build([], MARK, 50) is None
    one = lm.build([pos("0xa", "long", 500_000, 3.2)], MARK, 50)
    # şimdi ile ilk pozisyon arasındaki 4 boş kova (0…3%) TEK katlanmış satıra
    # iner — mesafe hissi kalır; sonra tek dolu bar. Uçtaki boşlar atılır.
    assert one and one["up"] == [] and len(one["down"]) == 2, one["down"]
    assert one["down"][0]["collapsed"] and one["down"][0]["lo"] == 0.0
    assert one["down"][1]["wpct"] == 100.0 and one["down"][1]["nearest"]
    # liq fiyatı olmayan pozisyon sayılıyor ama çizilmiyor
    nl = lm.build([pos("0xa", "long", 500_000, 3.2),
                   {"address": "0xb", "side": "long", "notional": 1e6, "liq_px": None}],
                  MARK, 50)
    assert nl["meta"]["dropped_noliq"] == 1 and nl["meta"]["n"] == 1
    print("✅ sınır) mark yok/0 → None; pozisyon yok → None; tek pozisyon tek bar;"
          " liq'siz pozisyon sayılıyor, çizilmiyor")


# ------------------------------------------- 9) uzağa düşenler sayılıyor
def test_far_counted():
    rows = fixture() + [pos("0xfar1", "short", 7_000_000, 60.0),
                        pos("0xfar2", "long", 2_000_000, 80.0)]
    out = lm.build(rows, MARK, 50)
    assert out["meta"]["dropped_far"] == 2, out["meta"]
    assert abs(out["meta"]["far_total"] - 9_000_000) < 1e-6
    assert out["meta"]["n"] == 40
    # azami mesafe büyütülünce son kenar ona uzanır, uzaklar içeri girer
    out2 = lm.build(rows, MARK, 90)
    assert out2["meta"]["dropped_far"] == 0
    assert out2["up"][0]["hi"] == 90.0, out2["up"][0]
    print("✅ uzak) %50 dışı 2 pozisyon ($9M) sayılıyor ve künyeye gidiyor;"
          " azami mesafe büyüyünce son kenar uzuyor")


# --------------------------------------- 10) tooltip ve tablo ikizi
def test_tip_table():
    out = lm.build(fixture(), MARK, 50)
    s = next(x for x in out["down"] if not x["empty"])
    assert s["tip"].split(" · ")[0] == s["label"], "ilk parça başlık (itipShow sözleşmesi)"
    assert "buraya kadar toplam" in s["tip"]
    assert "0x" in s["tip"]
    tab = out["table"]
    assert all(not r.get("collapsed") for r in tab)
    assert abs(sum(r["total"] for r in tab)
               - out["legend"]["up"]["total"] - out["legend"]["down"]["total"]) < 1e-6
    # kümülatif tekdüze: aynı yönde dıştan içe azalmaz
    ups = [r for r in tab if r["side"] == "short"]
    assert all(a["cum"] >= b["cum"] for a, b in zip(ups, ups[1:]))
    print("✅ ikiz) tooltip başlığı = etiket; tablo toplamı = legend toplamı;"
          " kümülatif tekdüze")


# ------------------------------------------ 11) bar boyu ölçeği
def test_scale():
    out = lm.build(fixture(), MARK, 50)
    ws = [s["wpct"] for s in out["up"] + out["down"] if not s["empty"]]
    assert max(ws) == 100.0 and min(ws) >= lm.MIN_WPCT
    big = max(out["up"] + out["down"], key=lambda s: s["total"])
    assert big["wpct"] == 100.0, "en büyük kova rayın tamamı"
    print("✅ ölçek) en büyük kova %100, hiçbiri %2'nin altında değil")


test_conservation()
test_sides_edges()
test_cascade_exact()
test_wall()
test_segments()
test_collapse_trim()
test_next()
test_edges()
test_far_counted()
test_tip_table()
test_scale()
print("\n✅ LİKİDASYON HARİTASI TESTLERİ GEÇTİ")
