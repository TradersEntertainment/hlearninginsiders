"""🧪 Kâğıt hesap (paper.replay, saf) — sim ayarıyla ($10K / 5x / %33) kuralın işlemlerini yeniden oynatır.

Pinlenenler:
  • marjin = min(bakiye × pay, serbest bakiye); serbest kalmazsa işlem "yer yok" sayılır (atlanmaz gizlice)
  • kâr/zarar = marjin × kaldıraç × net; zarar marjini aşamaz (likidasyon = marjinin tamamı)
  • aynı anda kapanış ve giriş → önce kapanış (serbest marjin geri gelir)
  • en kötü işlem / en kötü gün / en büyük düşüş; net ya da çıkışı olmayan satır sayılmaz
  • yol üstünde en kötü an × kaldıraç ≤ −%100 → likidasyon (son net kazanç olsa da marjin gider)
  • eğri sim.svg_curve ile çizilir; çok işlemde seyreltilir (özet tüm işlemlerden); lab işlemi kendi etiketini (coin · net · $) taşır, HTML kaçışlı
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from app.lab import paper  # noqa: E402
from app.radar import sim  # noqa: E402

D = 86400
T0 = 1_791_000_000 // D * D


def _t(i, a, b, net, coin="BTC"):
    return {"entry_ts": T0 + a, "exit_ts": T0 + b, "net": net, "coin": coin, "i": i}


def test_single_and_liquidation():
    r = paper.replay([_t(1, 0, 3600, 0.01)], 10_000, 5, 33, T0 - 1)
    assert r["n"] == 1 and abs(r["balance"] - 10_165) < 1e-9, r["balance"]       # 3300 × 5 × %1
    assert abs(r["ret_pct"] - 1.65) < 1e-9 and r["pos_share"] == 1.0 and r["skipped"] == 0
    liq = paper.replay([_t(1, 0, 3600, -0.30)], 10_000, 5, 33)
    assert abs(liq["balance"] - 6_700) < 1e-9, "zarar marjini aşmaz (3300 × 5 × −%30 = −4950 → −3300)"
    assert abs(liq["worst_trade"] + 3_300) < 1e-9 and abs(liq["max_dd_pct"] + 33.0) < 1e-9
    print("✅ kâğıt) $10K × %33 × 5x: +%1 → +165$; −%30 → marjinin tamamı (−3300$), daha fazla değil")


def test_overlap_margin_cap_and_skip():
    trades = [_t(i, i, 10_000, 0.0) for i in range(5)]                 # beşi aynı anda açık
    r = paper.replay(trades, 10_000, 5, 33)
    # 3300, 3300, 3300 → serbest 100 → 4. işlem 100$ marjin; 5. işlemde serbest 0 → yer yok
    assert r["n"] == 4 and r["skipped"] == 1, r
    margins = sorted(round(p[2]["margin"]) for p in r["points"] if p[2])
    assert margins == [100, 3300, 3300, 3300], margins
    seq = [_t(1, 0, 100, 0.0), _t(2, 100, 200, 0.0), _t(3, 100, 200, 0.0), _t(4, 100, 200, 0.0),
           _t(5, 100, 200, 0.0)]
    s = paper.replay(seq, 10_000, 5, 33)
    # kapanış önce: t=100'de 1. işlem kapanır → 3300 ×3 + 100 sığar (kapanış sonra olsaydı 5. işlem atlanırdı)
    assert s["skipped"] == 0 and s["n"] == 5, s
    print("✅ kâğıt) örtüşen işlemler serbest marjinle sınırlı; yer kalmazsa 'yer yok' sayılır; kapanış girişten önce")


def test_worst_day_drawdown_and_skip_null():
    trades = [_t(1, 0, 3600, 0.02), _t(2, D, D + 3600, -0.02), _t(3, D + 7200, D + 9000, -0.01),
              _t(4, 2 * D, 2 * D + 60, None), {"entry_ts": T0 + 3 * D, "exit_ts": None, "net": 0.05, "coin": "X"}]
    r = paper.replay(trades, 10_000, 5, 33)
    assert r["n"] == 3, "net ya da çıkışı olmayan satır sayılmaz"
    b1 = 10_000 + 3300 * 5 * 0.02
    m2 = b1 * 0.33
    b2 = b1 - m2 * 5 * 0.02
    m3 = b2 * 0.33
    b3 = b2 - m3 * 5 * 0.01
    assert abs(r["balance"] - b3) < 1e-6
    day, pnl = r["worst_day"]
    assert abs(pnl - (b3 - b1)) < 1e-6, (day, pnl)
    assert abs(r["max_dd_pct"] - (b3 / b1 - 1) * 100) < 1e-9
    print("✅ kâğıt) en kötü gün TSİ gününe göre toplanır; en büyük düşüş zirveden; eksik satır sayılmaz")


def test_svg_curve_with_lab_titles():
    r = paper.replay([_t(1, 0, 3600, 0.01, "xyz:<b>"), _t(2, 7200, 9000, -0.004, "ETH")], 10_000, 5, 33, T0 - 1)
    svg = sim.svg_curve(r["points"], 10_000)
    assert svg.startswith("<svg") and "&lt;b&gt; · net %+1.00 · +165$" in svg and "<b>" not in svg
    assert "ETH · net %-0.40" in svg
    assert paper.replay([], 10_000, 5, 33)["n"] == 0
    print("✅ kâğıt) eğri sim.svg_curve ile; lab etiketi coin · net · $ ve HTML kaçışlı")


def test_path_liquidation_and_thin():
    win_after_liq = dict(_t(1, 0, 3600, 0.02), mae=-0.25)          # 5x × −%25 = −%125 → marjin bitti
    near = dict(_t(2, 7200, 9000, 0.02), mae=-0.15)                # 5x × −%15 = −%75 → yaşar
    r = paper.replay([win_after_liq, near], 10_000, 5, 33)
    assert r["liq"] == 1 and abs(r["worst_trade"] + 3_300) < 1e-9, r
    liq_t = next(p[2] for p in r["points"] if p[2] and p[2]["liq"])
    assert "likidasyon" in liq_t["title"]
    many = paper.replay([_t(i, i * 100, i * 100 + 50, 0.001 if i % 3 else -0.002) for i in range(2000)],
                        10_000, 5, 33, T0 - 1)
    th = paper.thin(many["points"])
    assert len(th) <= 302 and th[0][1] == many["points"][0][1] and th[-1][1] == many["points"][-1][1]
    assert min(p[1] for p in th) == min(p[1] for p in many["points"]), "kova en düşüğü korunur"
    assert all(p[2] is None for p in th), "seyreltilmiş eğride işlem işareti yok"
    svg = sim.svg_curve(th, 10_000)
    assert svg.count("<circle") == 0 and len(svg) < 20_000
    print("✅ kâğıt) yol üstü likidasyon; çok işlemde eğri seyreltilir (en düşük/yüksek korunur)")


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
