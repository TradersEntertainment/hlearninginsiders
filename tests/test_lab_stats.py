"""🧪 Strateji laboratuvarı — kümeli, dürüst istatistik (Faz 4, B).

Pinlenenler:
  • t_cdf: t_cdf(2, 10) ≈ 0.96330, t_cdf(1, 1) = 0.75, t_cdf(−2.228, 10) ≈ 0.025; df = 1 ve 2'nin
    kapalı biçimleri (t ≈ 0 dahil); simetri; büyük df'de Normal; ±inf / NaN; df ≤ 0 ret
  • t_test_one: elle hesapla aynı; p = 1 − t_cdf; n < 2 ve sd = 0 (ulp gürültüsü dahil) → p = 1
    (mean yine döner); NaN atılır
  • işaret çevirme: küçük n'de elle sayım (eşitlik dahil), itertools ile tam = fonksiyonun tamı,
    n = 17'de Monte Carlo ≈ kaba kuvvet tam; aynı tohum aynı p; p ≥ 1/(1+reps)
  • binom_sf: tam kesirler, kenarlar, sf farkı = pmf, büyük n kararlı; geçersiz p0 (NaN) ret
  • OBF: α(1) = α, α(0.5) = 0.005575, artan; artışlar toplamı α; α = 0 → 0; bozuk bakış listesi ret
  • DAVRANIŞ: sıfır veride (normal ve kalın kuyruk, sabit tohum) max(t, işaret çevirme) yanlış pozitif
    ≤ ~0.06; iki bakışlı OBF ailesi de; 0.5·sd etkide çoğunlukla geçer; power_n kadar örnekte güç ≈ hedef
  • thin_nonoverlap (sınır dahil, son TUTULANA göre, coin başına, girdi sırası), block_key,
    cluster_means (anahtar sıralı, eşit ağırlık, NaN atılır)
  • beta_dimson: sentetik β = 1.5 (büzülmesiz ≈ 1.5, varsayılan ≈ 1.33), gecikmeli tepkiyi toplar,
    kırpma, kısa veri → 1
  • half_split_same_sign, wilson_ci, bootstrap_ci, effect_summary, power_n
"""
import itertools
import math
import os
import statistics
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from app.lab import stats as S  # noqa: E402

SEED = 20261008
PHI = statistics.NormalDist()


def close(a, b, tol=1e-9):
    return abs(a - b) <= tol


def gate_p(x, reps=2000, seed=0):
    """Kapının "net" kuralı: ikisi de geçmeli → p = max(t, işaret çevirme)."""
    return max(S.t_test_one(x)["p"], S.signflip_p(x, reps=reps, seed=seed))


# ── Student-t ──────────────────────────────────────────────────────────────────

def test_t_cdf_known_values():
    assert close(S.t_cdf(2.0, 10), 0.96330, 1e-5) and close(S.t_cdf(2.0, 10), 0.9633060, 1e-6)
    assert close(S.t_cdf(1.0, 1), 0.75, 1e-12)
    assert close(S.t_cdf(-2.228, 10), 0.025, 5e-5)
    assert close(S.t_cdf(2.228, 10), 0.975, 5e-5)
    assert S.t_cdf(0.0, 7) == 0.5
    # t ≈ 0'da 1 − x iptali yok: df = 200, t = 1e-8 → 0.5 + t·f(0) (eskiden tam 0.5 dönüyordu)
    f0 = math.exp(math.lgamma(100.5) - math.lgamma(100.0)) / math.sqrt(200 * math.pi)
    assert close(S.t_cdf(1e-8, 200), 0.5 + 1e-8 * f0, 1e-15) and S.t_cdf(1e-8, 200) > 0.5
    # kapalı biçimler: df=1 Cauchy, df=2
    for t in (-50.0, -3.3, -1.0, -0.2, -1e-6, 1e-8, 0.05, 0.7, 1.5, 4.0, 120.0):
        assert close(S.t_cdf(t, 1), 0.5 + math.atan(t) / math.pi, 1e-12), t
        assert close(S.t_cdf(t, 2), 0.5 + t / (2 * math.sqrt(2 + t * t)), 1e-12), t
        for df in (1, 3, 9, 30):
            assert close(S.t_cdf(-t, df), 1 - S.t_cdf(t, df), 1e-12)       # simetri
    print("✅ t_cdf: bilinen değerler, kapalı biçimler, simetri")


def test_t_cdf_tends_to_normal_and_edges():
    for t in (-3.0, -1.96, -0.5, 0.3, 1.645, 2.5):
        gap = [abs(S.t_cdf(t, df) - PHI.cdf(t)) for df in (5, 50, 500, 10_000, 10**6)]
        assert all(b < a for a, b in zip(gap, gap[1:])), (t, gap)          # df büyüdükçe yaklaşır
        assert gap[-1] < 1e-6, (t, gap)
    xs = np.linspace(-6, 6, 61)
    for df in (1, 4, 25):
        v = [S.t_cdf(float(t), df) for t in xs]
        assert all(b > a for a, b in zip(v, v[1:]))                         # kesin artan
        assert 0 < v[0] < v[-1] < 1
    assert S.t_cdf(float("inf"), 3) == 1.0 and S.t_cdf(float("-inf"), 3) == 0.0
    assert math.isnan(S.t_cdf(float("nan"), 3))
    for bad in (0, -2):
        try:
            S.t_cdf(1.0, bad)
            raise AssertionError("df ≤ 0 kabul edildi")
        except ValueError:
            pass
    print("✅ t_cdf: büyük df'de Normal, monoton, kenarlar")


def test_t_test_one():
    x = [0.012, -0.004, 0.021, 0.007, 0.0, 0.015, 0.009, 0.011]
    r = S.t_test_one(x)
    n = len(x)
    m = sum(x) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in x) / (n - 1))
    assert r["n"] == n and close(r["mean"], m, 1e-15) and close(r["sd"], sd, 1e-15)
    assert close(r["se"], sd / math.sqrt(n), 1e-15) and close(r["t"], m / (sd / math.sqrt(n)), 1e-12)
    assert close(r["p"], 1 - S.t_cdf(r["t"], n - 1), 1e-12)
    assert 0 < r["p"] < 0.05                                                # bu örnekte anlamlı
    neg = S.t_test_one([-v for v in x])
    assert close(neg["p"], 1 - r["p"], 1e-12) and neg["p"] > 0.95           # tek yönlü: negatif geçmez
    # sağ kuyruk hassas: 1 − CDF iptaline düşmez
    big = S.t_test_one([1.0 + 1e-3 * k for k in range(-5, 6)])
    assert 0 < big["p"] < 1e-15
    # kenarlar: karar yok ama mean döner
    one = S.t_test_one([0.03])
    assert one["n"] == 1 and one["mean"] == 0.03 and one["p"] == 1.0
    empty = S.t_test_one([])
    assert empty["n"] == 0 and math.isnan(empty["mean"]) and empty["p"] == 1.0
    flat = S.t_test_one([0.1] * 6)
    assert flat["p"] == 1.0 and close(flat["mean"], 0.1, 1e-15) and flat["sd"] == 0.0
    # yalnız ulp gürültüsü (0.3 ≠ 0.1+0.2) de sd = 0 sayılır — dev t / p ≈ 0 üretmez
    ulp = S.t_test_one([0.3, 0.1 + 0.2, 0.3, 0.1 + 0.2])
    assert ulp["p"] == 1.0 and ulp["sd"] == 0.0 and math.isnan(ulp["t"])
    assert S.t_test_one([1e10, 1e10 + 1, 1e10 + 2])["p"] < 1e-15         # gerçek (göreli küçük) yayılım
    # None / NaN / inf atılır
    d = S.t_test_one(x + [None, float("nan"), float("inf")])
    assert d["n"] == n and close(d["p"], r["p"], 1e-15)
    assert close(S.t_test_one(np.array(x))["p"], r["p"], 1e-15)
    print("✅ t_test_one: elle hesapla aynı, tek yönlü, kenarlar")


# ── işaret çevirme ─────────────────────────────────────────────────────────────

def brute_signflip(x):
    """Bağımsız kaba kuvvet: 2^n işaret, çevrilmiş ortalama ≥ gözlenen payı."""
    x = list(x)
    obs = sum(x)
    tol = 1e-12 * sum(abs(v) for v in x)
    hits = tot = 0
    for signs in itertools.product((1, -1), repeat=len(x)):
        tot += 1
        hits += sum(s * v for s, v in zip(signs, x)) >= obs - tol
    return hits / tot


def test_signflip_exact_small():
    assert S.signflip_p([1, 2, 3]) == 1 / 8                    # yalnız özdeşlik
    assert S.signflip_p([1, -1]) == 3 / 4                      # eşitlikler sayılır (0, 0, 2 ≥ 0)
    assert S.signflip_p([0.0, 0.0, 0.0]) == 1.0
    assert S.signflip_p([]) == 1.0 and S.signflip_p([None, float("nan")]) == 1.0
    assert S.signflip_p([-1, -2, -3]) == 1.0                   # ters yön: hep ≥
    rng = np.random.default_rng(SEED)
    for n in (1, 4, 7, 10):
        x = (rng.standard_normal(n) * 0.01 + 0.003).tolist()
        p = S.signflip_p(x)
        assert close(p, brute_signflip(x), 1e-15), n
        assert close(p * 2 ** n, round(p * 2 ** n), 1e-9)      # tam sayım: 2^−n katları
    # tamsayılı (bol eşitlikli) veri
    x = [1, 1, -1, 2, -2, 1, 0, 3]
    assert close(S.signflip_p(x), brute_signflip(x), 1e-15)
    print("✅ signflip_p: tam sayım, eşitlikler, kenarlar")


def test_signflip_mc_matches_exact():
    rng = np.random.default_rng(SEED + 1)
    x = rng.standard_normal(17) * 0.01 + 0.004                 # n = 17 → Monte Carlo yolu
    xs = np.asarray(x)
    idx = np.arange(1 << 17)
    flips = ((idx[:, None] >> np.arange(17)) & 1).astype(float)   # kaba kuvvet tam (bağımsız)
    signed = (1 - 2 * flips) @ xs
    exact = float(np.mean(signed >= xs.sum() - 1e-12 * np.abs(xs).sum()))
    assert 0.005 < exact < 0.5, exact
    t0 = time.time()
    mc = S.signflip_p(x, reps=100_000, seed=0)
    assert time.time() - t0 < 1.0
    assert abs(mc - exact) < 0.004, (mc, exact)                # MC sd ≈ 0.0007
    assert S.signflip_p(x, reps=100_000, seed=0) == mc         # aynı tohum → aynı p
    assert S.signflip_p(x, reps=100_000, seed=1) != mc
    # parça sınırı (MC_CHUNK // n) aşılsa da aynı sayım düzeni; p tabanı 1/(1+reps)
    strong = np.full(40, 0.01) + rng.standard_normal(40) * 1e-4
    assert S.signflip_p(strong, reps=999) == 1 / 1000
    # n = 16 tam yolu da bağımsız kaba kuvvetle aynı
    y = rng.standard_normal(16) * 0.01 + 0.002
    idx = np.arange(1 << 16)
    fl = ((idx[:, None] >> np.arange(16)) & 1).astype(float)
    ex16 = float(np.mean((1 - 2 * fl) @ y >= y.sum() - 1e-12 * np.abs(y).sum()))
    assert close(S.signflip_p(y), ex16, 1e-15)
    print("✅ signflip_p: Monte Carlo ≈ tam, tohum belirleyici, taban 1/(1+reps)")


# ── binom, aralıklar ───────────────────────────────────────────────────────────

def test_binom_sf_exact():
    assert close(S.binom_sf(8, 10, 0.5), 56 / 1024, 1e-14)
    assert close(S.binom_sf(10, 10, 0.5), 1 / 1024, 1e-15)
    assert close(S.binom_sf(3, 3, 0.2), 0.008, 1e-15)
    assert close(S.binom_sf(1, 5, 0.1), 1 - 0.9 ** 5, 1e-14)
    assert close(S.binom_sf(2, 4, 0.25), 1 - 0.75 ** 4 - 4 * 0.25 * 0.75 ** 3, 1e-14)
    assert S.binom_sf(0, 7, 0.3) == 1.0 and S.binom_sf(-1, 7, 0.3) == 1.0
    assert S.binom_sf(8, 7, 0.3) == 0.0
    assert S.binom_sf(0, 0, 0.3) == 1.0 and S.binom_sf(1, 0, 0.3) == 0.0
    assert S.binom_sf(3, 5, 0.0) == 0.0 and S.binom_sf(3, 5, 1.0) == 1.0
    # sf(k) − sf(k+1) = pmf(k); toplam pmf = 1
    n, p = 23, 0.37
    tot = 0.0
    for k in range(n + 1):
        pmf = math.comb(n, k) * p ** k * (1 - p) ** (n - k)
        assert close(S.binom_sf(k, n, p) - S.binom_sf(k + 1, n, p), pmf, 1e-13), k
        tot += pmf
    assert close(tot, 1.0, 1e-12)
    # büyük n kararlı: simetrik binomda P(X ≥ n/2) = 0.5 + pmf/2
    n = 2000
    mid = math.exp(math.lgamma(n + 1) - 2 * math.lgamma(n // 2 + 1) - n * math.log(2))
    assert close(S.binom_sf(1000, n, 0.5), 0.5 + mid / 2, 1e-12)
    tail = S.binom_sf(1100, n, 0.5)                                         # z ≈ 4.45
    assert 1e-7 < tail < 1e-5, tail
    assert S.binom_sf(1900, n, 0.5) >= 0.0                                  # taşma yok
    # geçersiz p0 sessizce p = 0 ("anlamlı") vermez
    for bad in (float("nan"), -0.1, 1.5, float("inf")):
        try:
            S.binom_sf(5, 10, bad)
            raise AssertionError(f"p0 {bad} kabul edildi")
        except ValueError:
            pass
    print("✅ binom_sf: tam kesirler, kenarlar, pmf farkı, büyük n")


def test_wilson_and_bootstrap():
    lo, hi = S.wilson_ci(5, 10)
    assert close(lo, 0.236593, 1e-5) and close(hi, 0.763407, 1e-5)
    lo, hi = S.wilson_ci(0, 20)
    assert lo == 0.0 and 0 < hi < 0.2
    lo, hi = S.wilson_ci(20, 20)
    assert hi == 1.0 and 0.8 < lo < 1
    assert S.wilson_ci(0, 0) == (0.0, 1.0)
    rng = np.random.default_rng(SEED + 2)
    x = rng.standard_normal(50) * 0.01 + 0.002
    lo, hi = S.bootstrap_ci(x)
    assert lo < x.mean() < hi
    se = x.std(ddof=1) / math.sqrt(len(x))
    assert 0.8 < (hi - lo) / (2 * 1.96 * se) < 1.2                          # ≈ normal aralık
    assert S.bootstrap_ci(x) == (lo, hi)                                    # tohum belirleyici
    assert S.bootstrap_ci(x, seed=5) != (lo, hi)
    for bad in ([], [0.01], [None, 0.02]):
        a, b = S.bootstrap_ci(bad)
        assert math.isnan(a) and math.isnan(b)
    print("✅ wilson_ci / bootstrap_ci")


# ── OBF ────────────────────────────────────────────────────────────────────────

def test_obf_spend_and_increments():
    for a in (0.01, 0.025, 0.05, 0.1):
        assert close(S.obf_spend(1.0, a), a, 1e-12)
        assert S.obf_spend(0.0, a) == 0.0 and S.obf_spend(-0.3, a) == 0.0
        assert close(S.obf_spend(1.7, a), a, 1e-12)
        v = [S.obf_spend(t, a) for t in np.linspace(0.05, 1.0, 40)]
        assert all(b > c for c, b in zip(v, v[1:]))                         # kesin artan
        assert v[0] < a / 100                                               # erken bakış çok cimri
    assert close(S.obf_spend(0.5, 0.05), 0.005575, 2e-6)
    for looks in ([1.0], [0.5, 1.0], [0.25, 0.5, 0.75, 1.0], [0.3, 0.31, 0.9, 1.0]):
        inc = S.gs_increments(looks, 0.05)
        assert len(inc) == len(looks) and all(i >= 0 for i in inc)
        assert close(sum(inc), 0.05, 1e-12), looks
        assert close(inc[0], S.obf_spend(looks[0], 0.05), 1e-15)
    inc = S.gs_increments([0.5, 0.8], 0.05)                                 # son bakış < 1
    assert close(sum(inc), S.obf_spend(0.8, 0.05), 1e-15) and sum(inc) < 0.05
    assert S.gs_increments([], 0.05) == []
    assert S.obf_spend(0.5, 0.0) == 0.0 and S.gs_increments([0.5, 1.0], 0.0) == [0.0, 0.0]  # α = 0
    for bad in ([0.5, 0.5], [1.0, 0.5], [0.0, 1.0], [0.5, 1.2], [-0.1, 1.0]):
        try:
            S.gs_increments(bad, 0.05)
            raise AssertionError(f"bozuk bakış kabul edildi: {bad}")
        except ValueError:
            pass
    print("✅ obf_spend / gs_increments: α(1) = α, artan, toplam = α")


# ── davranış: yanlış pozitif ve güç ────────────────────────────────────────────

def test_null_false_positive_rate():
    """Sıfır ortalamalı veride max(t, işaret çevirme) α = 0.05'te ≈ α kez geçer (fazla değil)."""
    rng = np.random.default_rng(SEED + 3)
    sims = 1500
    for n, draw in ((20, lambda: rng.standard_normal(20) * 0.01),           # Monte Carlo yolu
                    (12, lambda: rng.standard_t(3, 12) * 0.01)):            # kalın kuyruk, tam yol
        hits = sum(gate_p(draw(), reps=2000, seed=i) <= 0.05 for i in range(sims))
        rate = hits / sims
        assert 0.02 <= rate <= 0.06, (n, rate)
    print("✅ sıfır veride max(t, işaret çevirme) yanlış pozitif ≤ ~α")


def test_group_sequential_null_fpr():
    """İki bakış (0.5, 1.0), n_max = 40, her bakış kendi OBF artışıyla: aile yanlış pozitifi ≤ ~α."""
    rng = np.random.default_rng(SEED + 4)
    inc = S.gs_increments([0.5, 1.0], 0.05)
    sims, hits, early = 1500, 0, 0
    for i in range(sims):
        x = rng.standard_normal(40) * 0.01
        if gate_p(x[:20], reps=1000, seed=i) <= inc[0]:
            hits += 1
            early += 1
        elif gate_p(x, reps=1000, seed=i) <= inc[1]:
            hits += 1
    assert hits / sims <= 0.06, hits / sims
    assert early / sims <= 0.015, early / sims                              # erken bakış cimri
    print("✅ OBF iki bakış: aile yanlış pozitifi ≤ ~α")


def test_power_on_real_effect():
    rng = np.random.default_rng(SEED + 5)
    sims = 800
    hits = sum(gate_p(rng.standard_normal(40) * 0.01 + 0.005, reps=1000, seed=i) <= 0.05
               for i in range(sims))
    assert hits / sims >= 0.85, hits / sims                                 # 0.5·sd, n = 40 → ~0.93
    # power_n kadar küme → güç ≈ hedef (0.8)
    n = S.power_n(0.01, 0.005, 0.05, 0.8)
    assert n == 26
    hits = sum(gate_p(rng.standard_normal(n) * 0.01 + 0.005, reps=1000, seed=i) <= 0.05
               for i in range(sims))
    assert 0.70 <= hits / sims <= 0.92, hits / sims
    # etki ters yöndeyse tek yönlü test geçmez
    hits = sum(gate_p(rng.standard_normal(40) * 0.01 - 0.005, reps=500, seed=i) <= 0.05
               for i in range(200))
    assert hits == 0
    print("✅ gerçek etkide güç: 0.5·sd'de çoğunlukla geçer, power_n ≈ hedef güç")


def test_power_n():
    z = PHI.inv_cdf(0.95) + PHI.inv_cdf(0.8)
    assert S.power_n(1.0, 0.5, 0.05, inflate=1.0) == math.ceil((z / 0.5) ** 2)
    assert S.power_n(1.0, 0.5, 0.05) == math.ceil((z / 0.5) ** 2 * 1.03)
    assert S.power_n(1.0, 0.25, 0.05) > S.power_n(1.0, 0.5, 0.05)          # küçük etki → çok örnek
    assert S.power_n(1.0, 0.5, 0.01) > S.power_n(1.0, 0.5, 0.05)           # sıkı α → çok örnek
    assert S.power_n(1.0, 0.5, 0.05, 0.9) > S.power_n(1.0, 0.5, 0.05, 0.8)
    assert S.power_n(0.0, 0.5, 0.05) == 2 and S.power_n(1e-6, 10.0, 0.05) == 2
    for bad in (0.0, -0.1):
        try:
            S.power_n(1.0, bad, 0.05)
            raise AssertionError("δ ≤ 0 kabul edildi")
        except ValueError:
            pass
    print("✅ power_n: formül, ×1.03, yönler")


# ── inceltme ve kümeleme ───────────────────────────────────────────────────────

def test_thin_nonoverlap():
    h = 100
    # zincir: son TUTULANA göre (0 tut, 50 at, 100 tut [sınır dahil], 150 at, 199 at, 200 tut)
    assert S.thin_nonoverlap([0, 50, 100, 150, 199, 200], ["A"] * 6, h) == \
        [True, False, True, False, False, True]
    # girdi sırası korunur, sıralama içeride yapılır; coin'ler bağımsız
    ts = [150, 0, 30, 100, 40, 260]
    cs = ["A", "A", "B", "A", "B", "B"]
    assert S.thin_nonoverlap(ts, cs, h) == [False, True, True, True, False, True]
    # aynı ts: ilk gelen tutulur
    assert S.thin_nonoverlap([10, 10, 10], ["X", "X", "Y"], h) == [True, False, True]
    assert S.thin_nonoverlap([], [], h) == []
    assert S.thin_nonoverlap([5, 6, 7], ["A", "A", "A"], 0) == [True, True, True]   # h = 0 → hepsi
    try:
        S.thin_nonoverlap([1, 2], ["A"], h)
        raise AssertionError("uzunluk farkı kabul edildi")
    except ValueError:
        pass
    # rastgele: tutulan ardışık olaylar arası ≥ h, atılan her olay bir tutulanın ufkunda
    rng = np.random.default_rng(SEED + 6)
    ts = rng.integers(0, 5000, 400).tolist()
    cs = rng.choice(["A", "B", "C"], 400).tolist()
    keep = S.thin_nonoverlap(ts, cs, h)
    for c in "ABC":
        kept = sorted(t for t, k, cc in zip(ts, keep, cs) if k and cc == c)
        assert all(b - a >= h for a, b in zip(kept, kept[1:]))
        for t, k, cc in zip(ts, keep, cs):
            if cc == c and not k:
                assert any(0 <= t - kt < h for kt in kept)
    print("✅ thin_nonoverlap: sınır dahil, son tutulana göre, coin başına, girdi sırası")


def test_block_key():
    W = 86_400
    assert S.block_key(0, W) == 0 and S.block_key(W - 1, W) == 0 and S.block_key(W, W) == 1
    assert S.block_key(-1, W) == -1                                          # taban bölme
    assert S.block_key(W + 3600, W, offset_s=3600) == 1 and S.block_key(W + 3599, W, 3600) == 0
    ts = 1_760_000_000
    assert S.block_key(ts, 4 * 3600) == ts // (4 * 3600)
    try:
        S.block_key(5, 0)
        raise AssertionError("width 0 kabul edildi")
    except ValueError:
        pass
    print("✅ block_key")


def test_cluster_means_ordering():
    vals = [0.03, 0.01, -0.02, 0.05, 0.02, 0.04]
    keys = [7, 3, 7, 1, 3, 3]
    m, n, k = S.cluster_means(vals, keys)
    assert k == [1, 3, 7]                                                    # anahtara göre artan
    assert n.tolist() == [1, 3, 2] and n.dtype.kind == "i"
    assert np.allclose(m, [0.05, (0.01 + 0.02 + 0.04) / 3, (0.03 - 0.02) / 2], atol=1e-15)
    # eşit ağırlık: 3 olaylı küme ile 1 olaylı küme sonraki ortalamada eşit sayılır
    assert close(float(m.mean()), (0.05 + 0.07 / 3 + 0.005) / 3, 1e-15)
    # NaN / None atılır; değeri kalmayan anahtar düşer
    m2, n2, k2 = S.cluster_means([0.01, float("nan"), None, 0.03], ["b", "a", "a", "b"])
    assert k2 == ["b"] and n2.tolist() == [2] and close(float(m2[0]), 0.02, 1e-15)
    # block_key ile birlikte
    ts = [10, 20, 3600 * 4 + 5, 3600 * 9, 3600 * 4 + 7]
    m3, n3, k3 = S.cluster_means([1.0, 3.0, 5.0, 7.0, 9.0], [S.block_key(t, 3600 * 4) for t in ts])
    assert k3 == [0, 1, 2] and n3.tolist() == [2, 2, 1] and m3.tolist() == [2.0, 7.0, 7.0]
    m4, n4, k4 = S.cluster_means([], [])
    assert m4.size == 0 and n4.size == 0 and k4 == []
    try:
        S.cluster_means([1.0], [1, 2])
        raise AssertionError("uzunluk farkı kabul edildi")
    except ValueError:
        pass
    print("✅ cluster_means: anahtar sıralı, eşit ağırlık, NaN atılır")


# ── beta, koruma, özet ─────────────────────────────────────────────────────────

def test_beta_dimson():
    rng = np.random.default_rng(SEED + 7)
    T = 20_000                                                               # gecikme terimi gürültülü
    rb = rng.standard_normal(T) * 0.01
    r = 1.5 * rb + rng.standard_normal(T) * 0.005
    raw = S.beta_dimson(r, rb, shrink=0.0)
    assert abs(raw - 1.5) < 0.05, raw
    b = S.beta_dimson(r, rb)                                                 # 1'e üçte bir büzülür
    assert close(b, (2 / 3) * raw + 1 / 3, 1e-12) and abs(b - (2 / 3 * 1.5 + 1 / 3)) < 0.04
    # gecikmeli tepki (seyrek işlem): r_t = 1.0·rb_t + 0.5·rb_{t−1} → Dimson 1.5'i toplar, lags=0 kaçırır
    rl = rb.copy()
    rl[1:] += 0.5 * rb[:-1]
    rl += rng.standard_normal(T) * 0.003
    assert abs(S.beta_dimson(rl, rb, lags=1, shrink=0.0) - 1.5) < 0.05
    assert abs(S.beta_dimson(rl, rb, lags=0, shrink=0.0) - 1.0) < 0.05
    # kırpma ve kenarlar
    assert S.beta_dimson(6.0 * rb, rb, shrink=0.0) == 3.0
    assert S.beta_dimson(-2.0 * rb, rb, shrink=0.0) == 0.0
    assert S.beta_dimson(r[:9], rb[:9]) == 1.0                               # veri < 10
    assert S.beta_dimson(r[:10], rb[:10]) != 1.0
    assert S.beta_dimson(r[:50], np.zeros(50)) == 1.0                        # var(rb) = 0
    rn = r.copy()
    rn[::7] = np.nan                                                         # NaN çiftler atılır
    assert abs(S.beta_dimson(rn, rb, shrink=0.0) - 1.5) < 0.06
    try:
        S.beta_dimson(r[:20], rb[:21])
        raise AssertionError("uzunluk farkı kabul edildi")
    except ValueError:
        pass
    print("✅ beta_dimson: β = 1.5 yakalanır, büzülme, gecikme, kırpma, kenarlar")


def test_half_split_same_sign():
    assert S.half_split_same_sign([0.01, 0.02, 0.01, 0.03]) is True
    assert S.half_split_same_sign([-0.01, -0.02, -0.01, -0.03]) is True     # yalnız işaret (mean>0 ayrı)
    assert S.half_split_same_sign([0.05, 0.04, -0.01, -0.02]) is False      # etki ikinci yarıda yok
    assert S.half_split_same_sign([0.01, 0.02, 0.03]) is False              # n < 4
    assert S.half_split_same_sign([]) is False
    assert S.half_split_same_sign([0.0, 0.0, 0.01, 0.02]) is False          # sıfır işaret sayılmaz
    # tek n: ortadaki dışarıda (−1 her iki yarıya da girmez)
    assert S.half_split_same_sign([0.01, 0.01, -1.0, 0.01, 0.01]) is True
    assert S.half_split_same_sign([0.02, -0.01, 0.0, 0.03, -0.01]) is True  # 0.005, 0.01
    print("✅ half_split_same_sign")


def test_effect_summary():
    xc = [0.01, -0.02, 0.03, 0.005, 0.0, 0.012]
    e = S.effect_summary(xc)
    assert set(e) == {"n_c", "mean", "ci", "sd", "worst", "q05"}
    assert e["n_c"] == 6 and close(e["mean"], sum(xc) / 6, 1e-15)
    assert e["worst"] == -0.02 and close(e["sd"], statistics.stdev(xc), 1e-15)
    assert close(e["q05"], float(np.quantile(xc, 0.05)), 1e-15) and e["worst"] <= e["q05"] <= e["mean"]
    assert e["ci"] == S.bootstrap_ci(xc) and e["ci"][0] < e["mean"] < e["ci"][1]
    xe = [0.02, -0.01, 0.0, 0.03, -0.04]
    f = S.effect_summary(xc, xe)
    assert f["n_ev"] == 5 and close(f["hit"], 0.4, 1e-15) and f["worst_ev"] == -0.04
    z = S.effect_summary([], [])
    assert z["n_c"] == 0 and math.isnan(z["mean"]) and math.isnan(z["ci"][0])
    assert z["n_ev"] == 0 and math.isnan(z["hit"])
    print("✅ effect_summary")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
