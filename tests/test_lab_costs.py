"""🧪 Strateji laboratuvarı — donmuş maliyet tablosu (Faz 4, D).

Pinlenenler:
  • tablo: kripto 0.0009 ücret / 0.0010 kayma (seans dışı aynı); hisse = endeks = emtia = doviz
    0.0018 / 0.0010 / 0.0030; funding sınırı hepsinde 0.00002/sa; satırlar ayrı nesne
  • COSTS_SHA: kanonik JSON'un sha256'sı, anahtar sırasından ve süreçten bağımsız, sabit değer;
    kopyada tek alan / sınıf değişirse hash değişir, asıl tablo dokunulmaz
  • funding işareti: pozitif oranda long öder, short alır; ikisinin ortalaması = ücret + kayma
  • funding bilinmezse sınır·saat iki yön için de MALİYET; listedeki boş saat de sınırla dolar
  • bilinmeyen sınıf (büyük harf, boş, None dahil) → hisse; seans dışı kayma yalnız HIP-3'te pahalı
  • net = ret_adj − total; geçersiz yön reddedilir
"""
import copy
import hashlib
import json
import math
import os
import random
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from app.lab import costs  # noqa: E402

PINNED_SHA = "b133ef3b45b1174f3cd11f09256458abfbc529b09405979870d55f523f3aa59b"
HIP3 = {"fee_rt": 0.0018, "slip_rt_on": 0.0010, "slip_rt_off": 0.0030, "funding_bound_h": 0.00002}


def close(a, b, tol=1e-12):
    return abs(a - b) <= tol


def test_table_values():
    T = costs.COSTS
    assert set(T) == {"kripto", "hisse", "endeks", "emtia", "doviz"}
    assert T["kripto"] == {"fee_rt": 0.0009, "slip_rt_on": 0.0010, "slip_rt_off": 0.0010,
                           "funding_bound_h": 0.00002}
    assert close(T["kripto"]["fee_rt"], 0.00045 * 2)               # taker %0.045 × 2
    assert close(T["kripto"]["slip_rt_on"], 2 * 5e-4)              # 5 bp/taraf
    for k in ("hisse", "endeks", "emtia", "doviz"):
        assert T[k] == HIP3, k
    assert close(HIP3["fee_rt"], 0.0009 * 2)                       # taker %0.09 × 2
    assert close(HIP3["slip_rt_off"], 2 * 15e-4)                   # 15 bp/taraf
    # her alan pozitif kesir, seans dışı kayma seans içinden az değil
    for k, row in T.items():
        assert all(0 < v < 0.01 for v in row.values()), k
        assert row["slip_rt_off"] >= row["slip_rt_on"], k
    # satırlar ortak nesne değil: biri değişse diğerleri (ve hash) kayıt dışı kaymaz
    ids = {id(r) for r in T.values()}
    assert len(ids) == len(T)
    assert costs.FALLBACK == "hisse"
    print("✅ tablo: kripto 9/10/10 bp, HIP-3 18/10/30 bp, funding sınırı 0.2 bp/sa")


def test_costs_sha_stable():
    canon = json.dumps(costs.COSTS, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert costs.COSTS_SHA == hashlib.sha256(canon.encode()).hexdigest()
    assert costs.COSTS_SHA == costs.table_sha(costs.COSTS) == costs.table_sha(copy.deepcopy(costs.COSTS))
    assert len(costs.COSTS_SHA) == 64 and all(c in "0123456789abcdef" for c in costs.COSTS_SHA)
    # anahtar sırası (dış ve iç) hash'i etkilemez
    rev = {k: dict(reversed(list(v.items()))) for k, v in reversed(list(costs.COSTS.items()))}
    assert list(rev) != list(costs.COSTS)
    assert costs.table_sha(rev) == costs.COSTS_SHA
    # sabit değer: biçim ya da tablo değişirse bu test kırılır → bilinçli yeni sürüm
    assert costs.COSTS_SHA == PINNED_SHA, costs.COSTS_SHA
    # başka süreçte, farklı hash tohumuyla aynı
    for seed in ("0", "12345"):
        out = subprocess.run([sys.executable, "-c", "from app.lab import costs; print(costs.COSTS_SHA)"],
                             cwd=ROOT, capture_output=True, text=True, timeout=30,
                             env={**os.environ, "PYTHONHASHSEED": seed})
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == PINNED_SHA
    print("✅ COSTS_SHA: kanonik, sıra/süreç bağımsız, sabit")


def test_costs_sha_changes_on_modified_copy():
    before = copy.deepcopy(costs.COSTS)
    seen = {costs.COSTS_SHA}
    for k in costs.COSTS:
        for f in costs.COSTS[k]:
            t = copy.deepcopy(costs.COSTS)
            t[k][f] = t[k][f] * 1.5
            h = costs.table_sha(t)
            assert h not in seen, (k, f)           # her tek alan değişikliği farklı hash
            seen.add(h)
    t = copy.deepcopy(costs.COSTS); t["kripto"]["fee_rt"] = 0.00090000001
    assert costs.table_sha(t) != costs.COSTS_SHA   # en küçük kayma bile
    t = copy.deepcopy(costs.COSTS); t["hisse"]["fee_rt"] = 0.0007   # PROPR ücreti gelince
    assert costs.table_sha(t) != costs.COSTS_SHA
    t = copy.deepcopy(costs.COSTS); t["spot"] = dict(HIP3)
    assert costs.table_sha(t) != costs.COSTS_SHA
    t = copy.deepcopy(costs.COSTS); del t["doviz"]
    assert costs.table_sha(t) != costs.COSTS_SHA
    t = copy.deepcopy(costs.COSTS); t["hisse"]["yeni_alan"] = 0.0
    assert costs.table_sha(t) != costs.COSTS_SHA
    # asıl tablo ve hash dokunulmadı
    assert costs.COSTS == before and costs.table_sha(costs.COSTS) == costs.COSTS_SHA == PINNED_SHA
    print(f"✅ COSTS_SHA: {len(seen) - 1} tek alan değişikliğinin her biri yeni hash")


def test_funding_sign_long_short():
    fh = [0.0001, 0.00005, -0.00002]               # Σ = 0.00013
    lo = costs.cost("kripto", False, 3 * 3600, +1, fh)
    sh = costs.cost("kripto", False, 3 * 3600, -1, fh)
    assert close(lo["funding"], 0.00013) and close(sh["funding"], -0.00013)
    assert close(lo["total"], 0.0009 + 0.0010 + 0.00013)
    assert close(sh["total"], 0.0009 + 0.0010 - 0.00013)
    assert lo["fee"] == sh["fee"] == 0.0009 and lo["slip"] == sh["slip"] == 0.0010
    # negatif oranda yön tersine: short öder, long alır
    neg = [-0.0003, -0.0001]
    assert close(costs.cost("hisse", False, 7200, +1, neg)["funding"], -0.0004)
    assert close(costs.cost("hisse", False, 7200, -1, neg)["funding"], 0.0004)
    # bilinen funding'de hold_s önemsiz, boş liste = funding yok
    assert close(costs.cost("kripto", False, 99 * 3600, +1, fh)["funding"], 0.00013)
    assert costs.cost("kripto", False, 1800, +1, [])["funding"] == 0.0
    # davranış: rastgele funding yollarında long + short funding'i her zaman sıfırlanır;
    # pozitif ortalamalı yolda long'un ortalama maliyeti short'unkinden yüksek
    rng = random.Random(20261008)
    d_sum = 0.0
    for _ in range(500):
        path = [rng.gauss(0.00001, 0.00003) for _ in range(rng.randint(1, 48))]
        a = costs.cost("endeks", True, 3600 * len(path), +1, path)
        b = costs.cost("endeks", True, 3600 * len(path), -1, path)
        assert close(a["funding"] + b["funding"], 0.0)
        assert close((a["total"] + b["total"]) / 2, 0.0018 + 0.0030)
        d_sum += a["total"] - b["total"]
    assert d_sum > 0
    print("✅ funding: pozitif oranda long öder / short alır, iki yön toplamı sıfır")


def test_unknown_funding_uses_bound_both_sides():
    for k in costs.COSTS:
        b = costs.COSTS[k]["funding_bound_h"]
        for hold_h in (0, 0.5, 1, 6, 72):
            hs = int(hold_h * 3600)
            lo = costs.cost(k, False, hs, +1)
            sh = costs.cost(k, False, hs, -1, None)
            assert close(lo["funding"], b * hold_h) and close(sh["funding"], b * hold_h), (k, hold_h)
            assert lo == sh                         # yön fark etmez: ikisi de öder
            assert lo["funding"] >= 0
    assert close(costs.cost("kripto", False, 6 * 3600, -1)["funding"], 0.00012)
    assert close(costs.cost("kripto", False, 6 * 3600, -1)["total"], 0.0009 + 0.0010 + 0.00012)
    assert costs.cost("kripto", False, -3600, +1)["funding"] == 0.0   # bozuk süre negatif olmaz
    # bozuk süre (NaN/inf/None) sessizce sıfır funding'e dönmez → reddedilir
    for bad in (float("nan"), float("inf"), None):
        try:
            costs.cost("hisse", False, bad, +1)
        except ValueError:
            pass
        else:
            raise AssertionError(f"hold_s={bad!r} kabul edildi")
    # funding biliniyorsa hold_s kullanılmaz (bozuk olsa da)
    assert close(costs.cost("kripto", False, float("nan"), -1, [0.0001])["funding"], -0.0001)
    # bilinmeyen sınır temkinli: short'un bilinen (alacağı) funding'inden her zaman pahalı
    fh = [0.00001] * 6
    assert costs.cost("hisse", False, 6 * 3600, -1)["total"] > costs.cost("hisse", False, 6 * 3600, -1, fh)["total"]
    # listede eksik saat (None/NaN) → o saat sınırla, yön fark etmeksizin maliyet
    gap = [0.0001, None, float("nan")]
    assert close(costs.cost("kripto", False, 3 * 3600, +1, gap)["funding"], 0.0001 + 2 * 0.00002)
    assert close(costs.cost("kripto", False, 3 * 3600, -1, gap)["funding"], -0.0001 + 2 * 0.00002)
    print("✅ funding bilinmez: sınır·saat iki yön için maliyet; boş saat sınırla dolar")


def test_unknown_class_falls_back_to_hisse():
    ref_on = costs.cost("hisse", False, 3600, +1)
    ref_off = costs.cost("hisse", True, 3600, -1)
    for k in ("xyz", "", None, "KRIPTO", "Kripto", "spot", "hisse "):
        assert costs.cost(k, False, 3600, +1) == ref_on, k
        assert costs.cost(k, True, 3600, -1) == ref_off, k
    assert ref_on["fee"] == 0.0018 and ref_on["slip"] == 0.0010
    assert ref_off["slip"] == 0.0030
    # yanlış yazılmış kripto ucuz satıra kaçmaz
    assert costs.cost("KRIPTO", False, 0, +1)["total"] > costs.cost("kripto", False, 0, +1)["total"]
    # bilinen sınıflar kendi satırını kullanır
    for k in costs.COSTS:
        c = costs.cost(k, False, 0, +1)
        assert c["fee"] == costs.COSTS[k]["fee_rt"] and c["slip"] == costs.COSTS[k]["slip_rt_on"]
    print("✅ bilinmeyen sınıf → hisse (temkinli)")


def test_off_hours_slip():
    for k in ("hisse", "endeks", "emtia", "doviz"):
        on, off = costs.cost(k, False, 0, +1), costs.cost(k, True, 0, +1)
        assert on["slip"] == 0.0010 and off["slip"] == 0.0030, k
        assert on["fee"] == off["fee"] == 0.0018
        assert close(off["total"] - on["total"], 0.0020)
        assert close(on["total"], 0.0028) and close(off["total"], 0.0048)
    on, off = costs.cost("kripto", False, 0, +1), costs.cost("kripto", True, 0, +1)
    assert on == off and close(on["total"], 0.0019)  # kripto 7/24: seans dışı aynı
    # kayma yalnız off_hours'a bağlı: süre ve yön değiştirmez
    for hs in (0, 1800, 86400):
        for s in (+1, -1):
            assert costs.cost("hisse", True, hs, s)["slip"] == 0.0030
            assert costs.cost("hisse", False, hs, s)["slip"] == 0.0010
    # seans bilinmiyor (None) → seans dışı (temkinli); kripto yine aynı
    assert costs.cost("hisse", None, 3600, +1) == costs.cost("hisse", True, 3600, +1)
    assert costs.cost("kripto", None, 3600, +1) == costs.cost("kripto", False, 3600, +1)
    print("✅ seans dışı kayma: HIP-3 30 bp, kripto 10 bp (aynı)")


def test_net_and_side():
    c = costs.cost("hisse", True, 2 * 3600, +1)
    assert close(c["total"], c["fee"] + c["slip"] + c["funding"])
    assert close(costs.net(0.01, c), 0.01 - c["total"])
    assert close(costs.net(c["total"], c), 0.0)          # başa baş = maliyet kadar brüt getiri
    assert costs.net(0.0, c) < 0                          # sıfır brüt → net zarar
    assert set(c) == {"fee", "slip", "funding", "total"}
    # davranış: sıfır ortalamalı brüt getirilerde net ortalama ≈ −maliyet (maliyet kenarı yer)
    rng = random.Random(20261008)
    xs = [costs.net(rng.gauss(0.0, 0.01), costs.cost("kripto", False, 3600, rng.choice((1, -1))))
          for _ in range(20000)]
    m = sum(xs) / len(xs)
    assert abs(m - (-(0.0019 + 0.00002))) < 4 * 0.01 / math.sqrt(len(xs))
    for bad in (0, 2, -2, 0.5):
        try:
            costs.cost("kripto", False, 3600, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"side={bad} kabul edildi")
    print("✅ net = ret_adj − total; yön ±1 dışı reddedilir")
