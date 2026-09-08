"""📐 Grafik etiketleri fiyat sırasında durur ve çizgisine bağlanır.

Kullanıcı sorusu: "1420-1430 liqleri neden 1200'nin altında grafikte?" — sağ oluktaki
etiketler grafiğin fiyat ekseniyle çelişiyordu. İki kusur vardı:

  1. YERLEŞTİRME SIRASI MESAFEYEYDİ. `place()` etiketleri geldikleri sırada ve YALNIZ
     aşağı itiyordu; Plan Y'nin "adı geçen tekler önce" sıralamasıyla birlikte sonra
     gelen ama fiyatı DAHA YÜKSEK olan etiket, önce konmuşun ALTINA düşebiliyordu.
  2. BAĞ YOKTU. Etiket çizgisinden 45px kayıyor ama arada hiçbir şey çizilmiyordu;
     okuyan etiketi bulunduğu yükseklikteki fiyata eşliyordu ("liq 0.1426" yazan
     etiket 0.1319 hizasında duruyordu).

Pinlenenler:
  • `stack_labels` çıktısında fiyat sırası ihlali YOKTUR (rastgele turlarla da)
  • hiçbir etiket fiyat çizgisinin yanlış yakasına geçmez
  • ⭐ ana band kendi çizgisinde kalır — ama SIĞMIYORSA o da kayar (sıra > pivot)
  • çakışmayan çapa hiç oynamaz; çıktı [top, bottom] içinde kalır
  • kayan etiket çizgisine dirsekle bağlanır (`leader`)
"""
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-liqorder.db")
from app.radar import liqchart as lc  # noqa: E402

TOP, BOT, MARK, P = 105.0, 604.0, 416.0, lc.LABEL_PITCH


def _violations(anchors, out):
    """Fiyat sırası ihlali: çapası YUKARIDA olanın etiketi AŞAĞIDA kalmışsa."""
    pr = sorted(zip(anchors, out))
    return [(a, b) for a, b in zip(pr, pr[1:]) if a[1] > b[1] + 1e-9]


def test_price_order_never_inverts():
    # ANSEM ekranı: ⭐ $419K bandı + 0.1430/0.1426/0.1181 tekleri + 0.1088 bandı
    ansem = [441.0, 496.0, 501.0, 502.0, 558.0, 584.0]
    out = lc.stack_labels(ansem, MARK, TOP, BOT + 28, P, pivot=1)
    assert not _violations(ansem, out), out
    assert out[1] == 496.0, "⭐ kendi çizgisinde kaldı"
    assert out[0] > MARK, "fiyatın altındaki liq, fiyat etiketinin altında kalır"
    # SIĞMAZSA ⭐ de kayar: 6 etiket 496'dan aşağı 5×28px ister, şerit vermiyorsa
    # yedek yerleşim fiyat bandından yayılır. Sıra yine bozulmaz, ETİKETLER ÜST ÜSTE
    # BİNMEZ — pivot bir istektir, sıra ve okunurluk kuraldır.
    tight = lc.stack_labels(ansem, MARK, TOP, BOT, P, pivot=1)
    assert not _violations(ansem, tight) and tight[1] != 496.0, tight
    gaps = [b - a for a, b in zip(sorted(tight), sorted(tight)[1:])]
    assert all(g >= P - 1e-9 for g in gaps), gaps
    # Plan Y'nin getirdiği gerileme: adı geçen tek (0.1300) önce konunca, ondan
    # fiyatça YÜKSEK band (0.1310) altına düşüyordu — eski `place()` ile 1 ihlal
    named_first = [355.0, 486.0, 479.0, 564.0]
    assert not _violations(named_first, lc.stack_labels(named_first, MARK, TOP, BOT, P))
    print("✅ sıra) ANSEM ve 'named önce' fikstürlerinde fiyat sırası ihlali yok; ⭐ yerinde")


def test_order_holds_for_random_layouts():
    random.seed(1789)
    piv_kept = piv_total = 0
    for _ in range(2000):
        n = random.randint(1, lc.MAX_LABELS)
        hi = random.choice([BOT, BOT - P, BOT - 2 * P])
        anchors = [round(random.uniform(TOP, hi), 1) for _ in range(n)]
        pivot = random.choice([None] + list(range(n)))
        out = lc.stack_labels(anchors, MARK, TOP, hi, P, pivot=pivot)
        assert not _violations(anchors, out), (anchors, pivot, out)
        assert all(TOP - 1e-6 <= v <= hi + 1e-6 for v in out), (hi, out)
        assert all((a >= MARK) == (o >= MARK) for a, o in zip(anchors, out)), \
            "etiket fiyat çizgisinin yanlış yakasına geçemez"
        if pivot is not None:
            piv_total += 1
            piv_kept += out[pivot] == anchors[pivot]
    assert piv_kept > piv_total * 0.6, "⭐ genelde yerinde kalmalı (sığmadığında kayar)"
    print(f"✅ özellik) 2000 rastgele yerleşim: sıra, sınır ve yaka hep doğru;"
          f" ⭐ {piv_kept}/{piv_total} turda çizgisinde kaldı")


def test_no_needless_drift():
    calm = [150.0, 300.0, 480.0, 580.0]          # hiçbiri 28px içinde değil
    assert lc.stack_labels(calm, MARK, TOP, BOT, P) == calm, "çakışmayan çapa oynamaz"
    # tek etiket: çapası neredeyse hiç kıpırdamaz
    assert lc.stack_labels([520.0], MARK, TOP, BOT, P) == [520.0]
    # üst üste 3 etiket tam pitch kadar açılır, fazlası değil
    out = sorted(lc.stack_labels([500.0, 500.0, 500.0], MARK, TOP, BOT, P))
    assert [round(b - a) for a, b in zip(out, out[1:])] == [P, P]
    print("✅ kayma) gereksiz kayma yok: çakışmayan çapa yerinde, çakışan tam pitch kadar açılır")


def test_leader_elbow_is_drawn():
    src = open(os.path.join(ROOT, "app", "radar", "liqchart.py"), encoding="utf-8").read()
    assert "def leader(" in src, "etiketi çizgisine bağlayan dirsek çizici"
    assert "leader(ay, ty, col)" in src and "abs(ty - ay) >= 6" in src, \
        "kayan etiket dirsekle bağlanır (6px altı kayma için gereksiz)"
    assert "stack_labels([a for a, _, _ in gutter]" in src, "oluk yerleşimi stack_labels'a bağlı"
    assert "pivot=pivot" in src and 'g[0].get("main")' in src, "⭐ ana band eksen"
    assert "x0 = plot_r + TAG_GAP" in src, "dirsek için oluk açıldı"
    print("✅ çizim) etiket çizgisinden ≥6px kaydıysa olukta dirsekle bağlanır; oluk açık")


def test_ansem_end_to_end():
    """Uçtan uca: kullanıcının ekranındaki ANSEM seviyeleri → PNG üretilir ve
    sağ olukta etiketlerin y sırası fiyat sırasıyla BİREBİR aynıdır."""
    mark = 0.17853
    lv = [
        dict(px=0.1452, notional=419000, side="long", main=True, cluster=True,
             px_lo=0.1270, px_hi=0.1452, n=10, mark=mark, dist=18.59),
        dict(px=0.1430, notional=80000, side="long", named=True, mark=mark, dist=19.9),
        dict(px=0.1426, notional=126000, side="long", named=True, mark=mark, dist=20.1),
        dict(px=0.1181, notional=248000, side="long", named=True, mark=mark, dist=33.9),
        dict(px=0.1683, notional=11000, side="long", cluster=True,
             px_lo=0.1651, px_hi=0.1683, n=2, mark=mark, dist=5.7),
        dict(px=0.2047, notional=1000, side="short", mark=mark, dist=14.7),
        dict(px=0.1088, notional=330000, side="long", cluster=True,
             px_lo=0.1068, px_hi=0.1088, n=6, mark=mark, dist=39.0),
    ]
    cs = [{"t": 1757000000 + i * 900, "o": 0.18, "h": 0.19, "l": 0.17, "c": 0.178}
          for i in range(60)]
    plan = lc.plan_levels(cs, mark, lv, far_pct=50.0, fit_all=True)
    lo, hi = plan["lo"], plan["hi"]
    plot_t, plot_b = lc.PAD_T, lc.H - lc.PAD_B

    def y_of(p):
        return plot_b - (float(p) - lo) / (hi - lo) * (plot_b - plot_t)

    draw = sorted(plan["draw"], key=lambda x: (0 if x.get("main") else 1, abs(x["px"] - mark)))
    groups, rest = lc.label_groups(draw, y_of)
    assert not rest, "7 seviye MAX_LABELS içinde — hiçbiri kenar etiketine düşmez"
    texts = [lc._group_text(g) for g in groups]
    for want in ("liq 0.1430", "liq 0.1426", "liq 0.1181"):
        assert any(want in t for t in texts), (want, texts)
    pivot = next(i for i, g in enumerate(groups) if g[0].get("main"))
    anchors = [y_of(g[0]["px"]) for g in groups]
    ys = lc.stack_labels(anchors, y_of(mark), plot_t + 13, plot_b - 13 - lc.LABEL_PITCH,
                         pivot=pivot)
    by_price = sorted(zip((g[0]["px"] for g in groups), ys), key=lambda r: -r[0])
    assert all(a[1] < b[1] for a, b in zip(by_price, by_price[1:])), by_price
    # 0.1426 etiketi 0.1181'inkinin ÜSTÜNDE (kullanıcının sorusu tam buydu)
    y = {round(g[0]["px"], 4): v for g, v in zip(groups, ys)}
    assert y[0.1426] < y[0.1181] < y[0.1088], y
    png = lc.render("para:ANSEM", cs, mark, lv, far_pct=50.0, fit_all=True)
    assert png and len(png) > 10_000, "PNG üretildi"
    print("✅ uçtan uca) ANSEM: oluk sırası fiyat sırasıyla aynı;"
          " 0.1426 etiketi 0.1181'in ÜSTÜNDE, PNG üretiliyor")
