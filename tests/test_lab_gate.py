"""🧪 Strateji laboratuvarı — tek kapı (Faz 4, C).

Pinlenenler:
  • durum kodları ASCII, specs.STATUSES ile aynı
  • look_due: eşik ⌈frac·n_max⌉ (0.55·100 → 55, kayan nokta payı), en az min_g (vars. 20); sıradaki
    bakış birer kez, sırayla (eşikler toptan aşılsa da); bakışlar bitince None; log hiç bakmaz
  • stage_a: yalnız frozen_backtest; α_A = bt_alpha_share·α (vars. 0.25); p = max(t, işaret
    çevirme); α_A < p ≤ α → emekli; n_c < min_g → emekli (çok anlamlı olsa da); saf (aynı girdi aynı)
  • stage_b: α_k = gs_increments(looks, α_B), α_B = α − α_A; aynı veri 1. bakışta devam, 2.'de gecti;
    son bakışta geçemeyen emekli; boşunalık frac ≥ 0.5 ve ortalama ≤ 0 (önce değil)
  • korumalar YALNIZ engeller: min_g, ortalama > 0 (barrier'da), iki yarı; gecti ⟺ p ≤ α_k ∧ korumalar
  • barrier: k = #tp, n = #tp + #sl, zaman aşımı ve "open" sayılmaz; p = elle tam binom (kesirli),
    p0 = sl/(tp+sl) = execsim.barrier_p; tp/sl üst düzeyde ya da exit içinde; path_exit sözlükleri;
    tp < sl'de zaman aşımı/open BAŞARISIZ (n = tüm olaylar) — kaymasız yürüyüş sıfırında gecti ≤ α;
    bilinmeyen sonuç etiketi ret
  • DAVRANIŞ (sabit tohum, look_due ile sürülen tam yaşam döngüsü): sıfır veride (normal ve kalın
    kuyruk) gecti oranı ≤ 1.5·α; Aşama A ≤ 1.5·α_A; barrier sıfırında ≤ α; 0.5·sd etkide (n_max 60)
    çoğunlukla geçer; barrier gerçek etkide çoğunlukla geçer
  • demote_check: 0'a karşı tek yönlü t, sıfırda durdu ≈ %5, düşüşte çoğunlukla durdu, est_at_pass
    karara girmez
  • describe: karar yok (decision/p yok), effect_summary + plan + sıradaki bakış sınırı
"""
import math
import os
import sys
import time
from fractions import Fraction

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from app.lab import execsim as ex  # noqa: E402
from app.lab import gate as G  # noqa: E402
from app.lab import stats as S  # noqa: E402

SEED = 20261008
REPS = 500                      # simülasyonda işaret çevirme tekrarı (p tabanı 1/501 < α_1)


def rule(**kw) -> dict:
    r = {"id": "T-1", "evidence": "forward", "metric": "net", "alpha": 0.05, "looks": [0.5, 1.0],
         "n_max": 60, "min_g": 20}
    r.update(kw)
    return r


def close(a, b, tol=1e-12):
    return abs(a - b) <= tol


def exact_t(n, t, seed=0, scale=0.01):
    """t istatistiği tam `t` olan, iki yarısı aynı ortalamalı küme dizisi (yarılar ayrı ayrı sıfır
    ortalamalı gürültü + sabit kayma)."""
    rng = np.random.default_rng(seed)
    h = n // 2
    w1, w2 = rng.standard_normal(h), rng.standard_normal(n - h)
    z = np.concatenate([w1 - w1.mean(), w2 - w2.mean()])
    z /= z.std(ddof=1)
    return (z + t / math.sqrt(n)) * scale


def lifecycle(r, xs, outs=None, reps=REPS, seed=0):
    """İleri kümeler birer birer gelir; look_due her adımda, bakış gelince stage_b.
    Döner: (son karar, yapılan bakışlar, bakış anındaki g'ler)."""
    done, at = [], []
    for g in range(1, len(xs) + 1):
        k = G.look_due(r, g, done)
        if k is None:
            continue
        done.append(k)
        at.append(g)
        o = None if outs is None else [e for c in outs[:g] for e in c]
        d = G.stage_b(r, k, xs[:g], o, reps=reps, seed=seed)["decision"]
        if d != G.DEVAM:
            return d, done, at
    return G.DEVAM, done, at


# ── durum kodları ──────────────────────────────────────────────────────────────

def test_statuses():
    assert G.STATUSES == ("aday", "kagit", "gecti", "canli", "durdu", "emekli")
    assert all(s.isascii() for s in G.STATUSES + (G.DEVAM,))
    assert (G.ADAY, G.KAGIT, G.GECTI, G.CANLI, G.DURDU, G.EMEKLI) == G.STATUSES
    assert G.DEVAM not in G.STATUSES                                       # karar, durum değil
    from app.lab import specs
    assert tuple(specs.STATUSES) == G.STATUSES
    print("✅ durum kodları ASCII, specs ile aynı")


# ── look_due ───────────────────────────────────────────────────────────────────

def test_look_due_thresholds():
    r = rule()
    assert G.thresholds(r) == [30, 60]
    assert G.look_due(r, 0, []) is None and G.look_due(r, 29, []) is None
    assert G.look_due(r, 30, []) == 1 and G.look_due(r, 31, []) == 1
    assert G.look_due(r, 30, [1]) is None and G.look_due(r, 59, [1]) is None   # tekrar yok
    assert G.look_due(r, 60, [1]) == 2 and G.look_due(r, 600, [1]) == 2
    assert G.look_due(r, 600, [1, 2]) is None and G.look_due(r, 10**6, [2]) is None
    # eşikler toptan aşılsa da sıradaki döner (atlama yok, ikinci kez yok)
    assert G.look_due(r, 100, []) == 1 and G.look_due(r, 100, None) == 1
    # min_g tabanı: ⌈0.5·20⌉ = 10 < 20 → 20; eksikse varsayılan 20
    assert G.thresholds(rule(n_max=20)) == [20, 20]
    assert G.look_due(rule(n_max=20), 19, []) is None and G.look_due(rule(n_max=20), 20, []) == 1
    r30 = rule(n_max=30)
    del r30["min_g"]
    assert G.thresholds(r30) == [20, 30]
    assert G.thresholds(rule(n_max=30, min_g=5)) == [15, 30]
    # kayan nokta: 0.55·100 = 55.000…01 → 55, 1/3·9 = 3
    assert 0.55 * 100 > 55
    assert G.thresholds(rule(looks=[0.55, 1.0], n_max=100, min_g=1)) == [55, 100]
    assert G.thresholds(rule(looks=[1 / 3, 2 / 3, 1.0], n_max=9, min_g=1)) == [3, 6, 9]
    # bozuk bakış listesi
    for bad in ([0.5, 0.5], [1.0, 0.5], [0.0, 1.0], [0.5, 1.2]):
        try:
            G.look_due(rule(looks=bad), 100, [])
            raise AssertionError(f"bozuk looks kabul edildi: {bad}")
        except ValueError:
            pass
    print("✅ look_due: ⌈frac·n_max⌉, min_g tabanı, sıradaki bakış bir kez")


def test_look_due_each_look_once_in_order():
    r = rule(looks=[0.25, 0.5, 0.75, 1.0], n_max=80, min_g=10)
    assert G.thresholds(r) == [20, 40, 60, 80]
    done, at = [], []
    for g in range(0, 200):
        for _ in range(3):                                   # aynı g'de tekrar sorulsa da
            k = G.look_due(r, g, done)
            if k is not None:
                done.append(k)
                at.append(g)
    assert done == [1, 2, 3, 4] and at == [20, 40, 60, 80]
    # sıçramalı akış (g 0 → 75): 1, 2, 3 aynı g'de sırayla, 4 eşiğinde
    done, at = [], []
    for g in (0, 75, 75, 75, 75, 79, 80, 81):
        k = G.look_due(r, g, done)
        if k is not None:
            done.append(k)
            at.append(g)
    assert done == [1, 2, 3, 4] and at == [75, 75, 75, 80]
    print("✅ look_due: her bakış tam bir kez, sırayla, eşiğinde")


def test_log_rule_never_looks():
    # specs._log biçimi (looks [], n_max 0, α 0) ve bakış planı olan log kuralı
    log0 = {"id": "LOG-X", "evidence": "log", "metric": "net", "looks": [], "n_max": 0, "min_g": 20,
            "bt_alpha_share": 0.0, "alpha": 0.0}
    log1 = rule(evidence="log")
    for r in (log0, log1):
        assert all(G.look_due(r, g, []) is None for g in (0, 20, 30, 60, 10**6))
        assert G.look_due(r, 60, [1]) is None
        try:
            G.stage_b(r, 1, np.full(40, 0.01))
            raise AssertionError("log kuralına stage_b yapıldı")
        except ValueError:
            pass
        try:
            G.stage_a(r, np.full(40, 0.01))
            raise AssertionError("log kuralına stage_a yapıldı")
        except ValueError:
            pass
        assert G.alpha_split(r) == (0.0, 0.0)
    # log, look_due ile sürülen yaşam döngüsünde hiç karar vermez
    d, done, _ = lifecycle(log1, np.full(80, 0.02))
    assert d == G.DEVAM and done == []
    print("✅ log kuralı hiç bakmaz, stage_a/stage_b reddeder")


# ── Aşama A ────────────────────────────────────────────────────────────────────

def test_stage_a_alpha_share_and_once():
    fb = rule(evidence="frozen_backtest", alpha=0.04, bt_alpha_share=0.25)
    assert close(G.alpha_split(fb)[0], 0.01) and close(G.alpha_split(fb)[1], 0.03)
    fb_def = rule(evidence="frozen_backtest", alpha=0.04)
    assert close(G.alpha_split(fb_def)[0], 0.01)                          # varsayılan pay 0.25
    # α_A < p ≤ α: tam α'da geçerdi, α_A'da geçmez → emekli
    x = exact_t(30, 2.2, seed=3)
    p = max(S.t_test_one(x)["p"], S.signflip_p(x))
    assert 0.01 < p <= 0.04, p
    a = G.stage_a(fb, x)
    assert a["decision"] == G.EMEKLI and a["p"] > 0.01 and close(a["alpha_used"], 0.01)
    assert a["n_c"] == 30 and set(a) >= {"decision", "p", "alpha_used", "n_c"}
    # eşik aşılınca Monte Carlo erken durur: p o zaman alt sınır (karar aynı); t-testi tam
    assert close(a["test"]["p_t"], S.t_test_one(x)["p"]) and a["p"] <= p + 1e-12
    # güçlü etki → kagit
    y = exact_t(30, 4.5, seed=4)
    b = G.stage_a(fb, y)
    assert b["decision"] == G.KAGIT and b["p"] <= 0.01
    # saf: aynı girdi aynı sonuç (durum tutmaz — "bir kez"i kayıt sağlar)
    assert G.stage_a(fb, y) == b and G.stage_a(fb, x) == a
    # n_c < min_g: çok anlamlı olsa da emekli (yetersiz); NaN küme sayılmaz
    z = np.concatenate([exact_t(19, 8.0, seed=5), [np.nan, None]])
    c = G.stage_a(fb, z)
    assert c["n_c"] == 19 and c["p"] <= 0.01 and c["decision"] == G.EMEKLI, c
    assert "yetersiz" in c["why"]
    # yalnız frozen_backtest
    try:
        G.stage_a(rule(), y)
        raise AssertionError("forward kurala stage_a yapıldı")
    except ValueError:
        pass
    print("✅ stage_a: α_A = pay·α, p = max(t, işaret çevirme), min_g, saf")


# ── Aşama B ────────────────────────────────────────────────────────────────────

def test_stage_b_obf_increments():
    fw = rule()
    inc = S.gs_increments([0.5, 1.0], 0.05)
    assert close(G.alpha_increments(fw)[0], inc[0]) and close(G.alpha_increments(fw)[1], inc[1])
    assert close(inc[0], 0.005575, 1e-6) and close(sum(inc), 0.05)
    fb = rule(evidence="frozen_backtest", alpha=0.05)                    # α_B = 0.0375
    inc_b = S.gs_increments([0.5, 1.0], 0.0375)
    assert close(sum(G.alpha_increments(fb)), 0.0375) and close(G.alpha_increments(fb)[0], inc_b[0])
    three = rule(looks=[1 / 3, 2 / 3, 1.0])
    assert close(sum(G.alpha_increments(three)), 0.05)
    # aynı veri: 1. bakışta (α_1 ≈ 0.0056) devam, 2. bakışta (α_2 ≈ 0.044) gecti
    x = exact_t(40, 2.4, seed=11)
    p = max(S.t_test_one(x)["p"], S.signflip_p(x), S.boot_t_p(x))
    assert inc[0] < p <= inc[1] and p <= inc_b[1], p
    r1, r2 = G.stage_b(fw, 1, x), G.stage_b(fw, 2, x)
    assert r1["decision"] == G.DEVAM and close(r1["alpha_k"], inc[0]) and inc[0] < r1["p"] <= p + 1e-12
    assert r2["decision"] == G.GECTI and close(r2["alpha_k"], inc[1])
    assert all(r1["guards"].values()) and r1["n_c"] == 40
    assert set(r1) >= {"decision", "p", "alpha_k", "n_c", "guards"}
    # frozen_backtest'te α_B ile
    assert close(G.stage_b(fb, 2, x)["alpha_k"], inc_b[1])
    # bozuk bakış numarası
    for bad in (0, 3, -1):
        try:
            G.stage_b(fw, bad, x)
            raise AssertionError(f"bakış {bad} kabul edildi")
        except ValueError:
            pass
    print("✅ stage_b: α_k = OBF artışı (α_B = α − α_A), aynı veri 1.'de devam 2.'de gecti")


def test_stage_b_last_look_and_futility():
    r = rule(looks=[0.25, 0.5, 1.0], n_max=80)
    weak = exact_t(40, 0.8, seed=21)                       # pozitif ama anlamsız
    neg = exact_t(40, -0.8, seed=22)
    assert G.stage_b(r, 1, weak)["decision"] == G.DEVAM
    assert G.stage_b(r, 2, weak)["decision"] == G.DEVAM
    assert G.stage_b(r, 3, weak)["decision"] == G.EMEKLI                  # son bakışta geçemedi
    # boşunalık: frac 0.25 < 0.5 → devam; frac 0.5 → emekli
    assert G.stage_b(r, 1, neg)["decision"] == G.DEVAM
    d2 = G.stage_b(r, 2, neg)
    assert d2["decision"] == G.EMEKLI and "boşunalık" in d2["why"]
    assert G.stage_b(r, 3, neg)["decision"] == G.EMEKLI
    # sıfır ortalama da boşuna sayılır (≤ 0)
    zero = exact_t(40, 0.0, seed=23)
    assert abs(zero.mean()) < 1e-15 and G.stage_b(r, 2, zero)["decision"] == G.EMEKLI
    # son bakış frac < 1 olsa da sondur
    r2 = rule(looks=[0.4, 0.8])
    assert G.stage_b(r2, 2, weak)["decision"] == G.EMEKLI
    print("✅ stage_b: son bakışta emekli, boşunalık frac ≥ 0.5'te")


def test_guards_only_block():
    r = rule()
    a1 = G.alpha_increments(r)[0]
    # iki yarı ters işaret: p çok küçük ama geçmez
    rng = np.random.default_rng(SEED)
    x = np.concatenate([-0.001 + rng.standard_normal(20) * 1e-4, 0.02 + rng.standard_normal(20) * 1e-4])
    b = G.stage_b(r, 1, x)
    assert b["p"] <= a1 and not b["guards"]["half_split"] and b["guards"]["mean_pos"]
    assert b["decision"] == G.DEVAM
    assert G.stage_b(r, 2, x)["decision"] == G.EMEKLI
    # n_c < min_g: tam işaret çevirme p = 2^−12 < α_1 ama geçmez
    small = exact_t(12, 9.0, seed=31)
    s = G.stage_b(r, 1, small)
    assert s["p"] <= a1 and not s["guards"]["min_g"] and s["decision"] != G.GECTI
    # korumalar tamam ama p > α_k → geçmez (koruma geçiş vermez)
    ok = exact_t(40, 1.0, seed=32)
    o = G.stage_b(r, 2, ok)
    assert all(o["guards"].values()) and o["p"] > o["alpha_k"] and o["decision"] == G.EMEKLI
    # barrier: binom çok anlamlı, net ortalama < 0 → ortalama koruması engeller
    rb = rule(metric="barrier", tp=0.02, sl=0.01)
    outs = ["tp"] * 30 + ["sl"] * 5
    xn = exact_t(40, -1.0, seed=33)
    bb = G.stage_b(rb, 2, xn, outs)
    assert bb["p"] < 1e-8 and not bb["guards"]["mean_pos"] and bb["decision"] == G.EMEKLI
    # özellik: gecti ⟺ p ≤ α_k ∧ tüm korumalar (rastgele veriler, karışık etkiler)
    seen = set()
    for i in range(150):
        n = int(rng.integers(8, 70))
        mu = float(rng.choice([-0.5, 0.0, 0.3, 0.8, 1.5]))
        xs = (rng.standard_normal(n) + mu) * 0.01
        if i % 5 == 0:
            xs[: n // 2] -= 0.02                           # ilk yarıyı boz
        k = int(rng.integers(1, 3))
        out = G.stage_b(r, k, xs, reps=REPS, seed=i)
        passed = out["alpha_k"] > 0 and out["p"] <= out["alpha_k"] and all(out["guards"].values())
        assert (out["decision"] == G.GECTI) == passed, (i, out)
        assert out["decision"] in (G.GECTI, G.DEVAM, G.EMEKLI)
        seen.add(out["decision"])
    assert seen == {G.GECTI, G.DEVAM, G.EMEKLI}
    print("✅ korumalar yalnız engeller: iki yarı, min_g, ortalama; gecti ⟺ p ≤ α_k ∧ korumalar")


# ── barrier ────────────────────────────────────────────────────────────────────

def binom_tail_exact(k, n, p0: Fraction) -> float:
    return float(sum(math.comb(n, i) * p0 ** i * (1 - p0) ** (n - i) for i in range(k, n + 1)))


def test_barrier_exact_binomial():
    rb = rule(metric="barrier", tp=0.02, sl=0.01)
    assert close(G.barrier_p0(rb), 1 / 3) and close(G.barrier_p0(rb), ex.barrier_p(0.02, 0.01))
    xc = exact_t(30, 3.0, seed=41)
    outs = ["tp"] * 15 + ["sl"] * 10 + ["timeout"] * 7
    want = binom_tail_exact(15, 25, Fraction(1, 3))
    b = G.stage_b(rb, 2, xc, outs)
    assert close(b["p"], want, 1e-13), (b["p"], want)
    assert b["test"]["k"] == 15 and b["test"]["n"] == 25 and b["test"]["timeout"] == 7
    # zaman aşımı ve "open" sayılmaz; sıra önemsiz; path_exit sözlükleri de olur
    more = outs + ["timeout"] * 50 + ["open"] * 3
    assert close(G.stage_b(rb, 2, xc, more[::-1])["p"], want, 1e-13)
    dicts = [{"reason": o, "ret": 0.0} for o in outs]
    assert close(G.stage_b(rb, 2, xc, dicts)["p"], want, 1e-13)
    # tp = sl → p0 = 0.5; tp/sl exit içinde
    re = rule(metric="barrier", exit={"tp": 0.015, "sl": 0.015, "timeout": 3600})
    assert close(G.barrier_p0(re), 0.5)
    assert close(G.stage_b(re, 1, xc, ["tp"] * 9 + ["sl"] * 3)["p"],
                 binom_tail_exact(9, 12, Fraction(1, 2)), 1e-14)
    # p0 = 0.25 (tp = 3·sl): 12/20 tp
    r3 = rule(metric="barrier", tp=0.03, sl=0.01)
    assert close(G.stage_b(r3, 1, xc, ["tp"] * 12 + ["sl"] * 8)["p"],
                 binom_tail_exact(12, 20, Fraction(1, 4)), 1e-14)
    assert b["test"]["timeout_as_fail"] is False                          # tp ≥ sl: sözleşme biçimi
    assert G.stage_b(re, 1, xc, ["tp"] * 9 + ["sl"] * 3)["test"]["timeout_as_fail"] is False
    # hiç sonuçlanan yok → p = 1; geçmez
    z = G.stage_b(rb, 2, xc, ["timeout"] * 40)
    assert z["p"] == 1.0 and z["decision"] == G.EMEKLI
    # Aşama A'da da aynı binom, α_A ile
    fb = rule(evidence="frozen_backtest", metric="barrier", tp=0.02, sl=0.01, alpha=0.05)
    a = G.stage_a(fb, xc, ["tp"] * 20 + ["sl"] * 10)
    assert close(a["p"], binom_tail_exact(20, 30, Fraction(1, 3)), 1e-14)
    assert a["decision"] == (G.KAGIT if a["p"] <= 0.0125 else G.EMEKLI)
    # eksik tp/sl, eksik outcomes, bozuk seviye
    for bad, o in ((rule(metric="barrier"), outs), (rb, None), (rule(metric="barrier", tp=0.02, sl=0), outs)):
        try:
            G.stage_b(bad, 1, xc, o)
            raise AssertionError("bozuk barrier kabul edildi")
        except ValueError:
            pass
    print("✅ barrier: tam binom, p0 = sl/(tp+sl), zaman aşımı sayılmaz")


def test_barrier_tp_below_sl_counts_timeouts_as_fail():
    """tp < sl: zaman aşımı bağlayıcıyken sonuçlananlarda TP (yakın seviye) fazla → yalnız
    sonuçlananlarla binom iyimser. Orada timeout ve open başarısız sayılır (n = tüm olaylar)."""
    rl = rule(metric="barrier", tp=0.01, sl=0.03)                       # p0 = 0.75
    xc = exact_t(60, 1.0, seed=42)                                      # korumaları geçer, anlamsız
    outs = ["tp"] * 15 + ["sl"] * 3 + ["timeout"] * 7 + ["open"] * 2
    b = G.stage_b(rl, 2, xc, outs)
    assert b["test"]["timeout_as_fail"] is True and b["test"]["n"] == 27 and b["test"]["open"] == 2
    assert close(b["p"], binom_tail_exact(15, 27, Fraction(3, 4)), 1e-13)
    # bilinmeyen etiket sessizce atılmaz (yanlış etiketli kayıplar k/n'yi şişirirdi)
    for bad in (["tp", "stop"], ["tp", None], ["TP", "sl"]):
        try:
            G.stage_b(rl, 2, xc, bad)
            raise AssertionError(f"bilinmeyen etiket kabul edildi: {bad}")
        except ValueError:
            pass
    # sıfır: kaymasız rastgele yürüyüş, zaman aşımı bağlayıcı (~%60); look_due ile tam döngü
    rng = np.random.default_rng(SEED + 5)
    runs, per = 300, 2
    steps = rng.standard_normal((runs * 60 * per, 100)) * 0.001
    path = np.cumsum(steps, axis=1)
    up, dn = path > 0.01, path <= -0.03                                  # TP içinden geçmeli
    t_up = np.where(up.any(1), up.argmax(1), 10**6)
    t_dn = np.where(dn.any(1), dn.argmax(1), 10**6)
    reason = np.where(t_dn <= t_up, np.where(t_dn < 10**6, "sl", "timeout"), "tp")
    res = reason != "timeout"
    assert 0.4 < 1 - res.mean() < 0.8                                    # zaman aşımı bağlayıcı
    assert (reason[res] == "tp").mean() > 0.85                          # eski biçimin iyimserliği (p0 0.75)
    passed = 0
    for i in range(runs):
        rs = reason[i * 60 * per:(i + 1) * 60 * per].tolist()
        o = [rs[j * per:(j + 1) * per] for j in range(60)]
        passed += lifecycle(rl, xc, o)[0] == G.GECTI
    assert passed / runs <= rl["alpha"], passed / runs
    print("✅ barrier tp < sl: zaman aşımı/open başarısız sayılır, kaymasız yürüyüşte gecti ≤ α")


# ── davranış: simülasyon ───────────────────────────────────────────────────────

def test_null_false_pass_rate():
    """Sıfır ortalamalı kümelerde look_due ile sürülen tam Aşama B: gecti oranı ≤ 1.5·α."""
    t0 = time.time()
    r = rule()
    rng = np.random.default_rng(SEED + 1)
    runs = 1200
    for name, draw in (("normal", lambda: rng.standard_normal(60) * 0.01),
                       ("t3", lambda: rng.standard_t(3, 60) * 0.01)):
        passed, looks_at = 0, set()
        for i in range(runs):
            d, done, at = lifecycle(r, draw(), seed=i)
            passed += d == G.GECTI
            looks_at.update(at)
            assert done in ([1], [1, 2]), done
        rate = passed / runs
        assert rate <= 1.5 * r["alpha"], (name, rate)
        assert looks_at == {30, 60}, looks_at                   # bakışlar yalnız eşiklerde
    assert time.time() - t0 < 4.0
    print(f"✅ sıfır veride Aşama B gecti oranı ≤ 1.5·α (son: {rate:.3f})")


def test_null_stage_a_and_barrier():
    rng = np.random.default_rng(SEED + 2)
    # Aşama A: α = 0.2, pay 0.25 → α_A = 0.05 (oran ölçülebilsin diye büyük α)
    fb = rule(evidence="frozen_backtest", alpha=0.2)
    runs = 1500
    hits = sum(G.stage_a(fb, rng.standard_normal(30) * 0.01, reps=REPS, seed=i)["decision"] == G.KAGIT
               for i in range(runs))
    assert hits / runs <= 1.5 * 0.05, hits / runs
    # barrier: TP payı tam p0 (martingale), korumalar açık (net pozitif) → yalnız binom karar verir;
    # tam binom ayrık ve temkinli → oran ≤ α
    rb = rule(metric="barrier", tp=0.02, sl=0.01, alpha=0.1)
    p0 = 1 / 3
    xc = exact_t(60, 1.0, seed=51)                           # anlamsız ama korumaları geçer
    passed = 0
    for i in range(runs):
        u = rng.random((60, 2))
        outs = [[("timeout" if v < 0.2 else "tp" if v < 0.2 + 0.8 * p0 else "sl") for v in row] for row in u]
        d, done, _ = lifecycle(rb, xc, outs)
        passed += d == G.GECTI
    assert passed / runs <= rb["alpha"], passed / runs
    print("✅ sıfır veride Aşama A ≤ 1.5·α_A, barrier ≤ α")


def test_real_effect_mostly_passes():
    rng = np.random.default_rng(SEED + 3)
    r = rule()
    runs = 300
    passed = sum(lifecycle(r, (rng.standard_normal(60) + 0.5) * 0.01, seed=i)[0] == G.GECTI
                 for i in range(runs))
    assert passed / runs >= 0.85, passed / runs
    # frozen_backtest: Aşama A (α_A) + Aşama B (α_B) — gerçek etkide çoğunlukla ikisi de
    fb = rule(evidence="frozen_backtest")
    both = 0
    for i in range(200):
        a = G.stage_a(fb, (rng.standard_normal(60) + 0.5) * 0.01, reps=REPS, seed=i)
        if a["decision"] == G.KAGIT:
            both += lifecycle(fb, (rng.standard_normal(60) + 0.5) * 0.01, seed=i)[0] == G.GECTI
    assert both / 200 >= 0.75, both / 200
    # barrier: sonuçlananlarda TP payı 0.5 (p0 = 1/3), küme başı 2 olay
    rb = rule(metric="barrier", tp=0.02, sl=0.01)
    xc = exact_t(60, 3.0, seed=61)
    passed = 0
    for i in range(200):
        u = rng.random((60, 2))
        outs = [[("timeout" if v < 0.1 else "tp" if v < 0.55 else "sl") for v in row] for row in u]
        passed += lifecycle(rb, xc, outs)[0] == G.GECTI
    assert passed / 200 >= 0.85, passed / 200
    print("✅ gerçek etkide (0.5·sd, n_max 60; barrier TP payı 0.5) çoğunlukla geçer")


# ── canlı ──────────────────────────────────────────────────────────────────────

def test_demote_check():
    assert G.demote_check(exact_t(30, -2.5, seed=71), 0.004) == G.DURDU
    assert G.demote_check(exact_t(30, -1.0, seed=72), 0.004) == G.DEVAM
    assert G.demote_check(exact_t(30, 3.0, seed=73), 0.004) == G.DEVAM
    assert G.demote_check([], 0.004) == G.DEVAM and G.demote_check([-0.01], 0.004) == G.DEVAM
    assert G.demote_check([-0.01] * 10, 0.004) == G.DEVAM               # sd = 0 → karar yok
    # eşik tam 0.05 tek yönlü (df = 29: t ≈ −1.699)
    assert G.demote_check(exact_t(30, -1.72, seed=74), 0.0) == G.DURDU
    assert G.demote_check(exact_t(30, -1.68, seed=74), 0.0) == G.DEVAM
    # est_at_pass karara girmez (0'a karşı test): küçük düşüşte de, tahminin çok altında da aynı
    x = exact_t(30, -0.5, seed=75)
    assert {G.demote_check(x, e) for e in (0.0, 0.001, 0.05, -1.0, None)} == {G.DEVAM}
    rng = np.random.default_rng(SEED + 4)
    runs = 2000
    stops = sum(G.demote_check(rng.standard_normal(30) * 0.01, 0.005) == G.DURDU for _ in range(runs))
    assert 0.03 <= stops / runs <= 0.075, stops / runs
    falls = sum(G.demote_check((rng.standard_normal(40) - 0.5) * 0.01, 0.005) == G.DURDU
                for _ in range(300))
    assert falls / 300 >= 0.85, falls / 300
    print("✅ demote_check: 0'a karşı tek yönlü t, sıfırda ≈ %5, düşüşte durdu")


# ── betimleme ──────────────────────────────────────────────────────────────────

def test_describe_has_no_decision():
    r = rule()
    x = exact_t(25, 2.0, seed=81)
    d = G.describe(r, x)
    assert "decision" not in d and "p" not in d
    assert not any(v in G.STATUSES or v == G.DEVAM for v in d.values() if isinstance(v, str))
    for k in ("n_c", "mean", "ci", "sd", "worst", "q05"):
        assert k in d, k
    assert d["n_c"] == 25 and close(d["mean"], float(np.mean(x)))
    assert [p["g"] for p in d["plan"]] == [30, 60] and close(sum(p["alpha_k"] for p in d["plan"]), 0.05)
    assert d["bound"]["look"] == 1 and d["bound"]["g"] == 30 and d["bound"]["due"] is False
    assert close(d["bound"]["alpha_k"], G.alpha_increments(r)[0])
    # sınır look_due ile aynı sıra
    y = exact_t(45, 2.0, seed=82)
    assert G.describe(r, y)["bound"]["due"] is True and G.look_due(r, 45, []) == 1
    assert G.describe(r, y, looks_done=[1])["bound"]["look"] == 2
    assert G.describe(r, y, looks_done=[1])["bound"]["due"] is False and G.look_due(r, 45, [1]) is None
    assert G.describe(r, y, looks_done=[1, 2])["bound"] is None
    # olay düzeyi
    e = G.describe(r, x, xe=[0.01, -0.02, 0.03, 0.0])
    assert e["n_ev"] == 4 and close(e["hit"], 0.5) and close(e["worst_ev"], -0.02)
    # frozen_backtest: α_A / α_B; log: plan yok; α = 0 çökmez; boş veri
    f = G.describe(rule(evidence="frozen_backtest"), x)
    assert close(f["alpha_a"], 0.0125) and close(f["alpha_b"], 0.0375)
    assert close(sum(p["alpha_k"] for p in f["plan"]), 0.0375)
    lg = G.describe({"evidence": "log", "metric": "net", "looks": [], "n_max": 0, "alpha": 0.0}, x)
    assert lg["plan"] == [] and lg["bound"] is None and "decision" not in lg
    z = G.describe(rule(alpha=0.0), [])
    assert z["n_c"] == 0 and all(p["alpha_k"] == 0.0 for p in z["plan"])
    print("✅ describe: yalnız betimleme, plan ve sıradaki bakış sınırı")


def test_deterministic_and_inputs():
    r = rule()
    x = exact_t(40, 2.4, seed=91)
    assert G.stage_b(r, 2, x) == G.stage_b(r, 2, x)                      # sabit tohum
    assert G.stage_b(r, 2, list(x)) == G.stage_b(r, 2, x)                 # liste ya da dizi
    withnan = list(x) + [None, float("nan"), float("inf")]
    assert G.stage_b(r, 2, withnan)["n_c"] == 40
    assert close(G.stage_b(r, 2, withnan)["p"], G.stage_b(r, 2, x)["p"])
    # α = 0 kuralı hiçbir şeyi geçirmez
    strong = exact_t(60, 9.0, seed=92)
    assert G.stage_b(rule(alpha=0.0), 2, strong)["decision"] == G.EMEKLI
    for bad in (rule(alpha=1.0), rule(alpha=-0.1), rule(metric="sharpe"), rule(evidence="paper")):
        try:
            G.stage_b(bad, 1, strong)
            raise AssertionError(f"bozuk kural kabul edildi: {bad}")
        except ValueError:
            pass
    print("✅ belirleyici, liste/dizi/NaN girdisi, bozuk kural reddi")


def test_net_skew_null_rate_and_guard():
    """Sola çarpık boş veri (TP 1 / SL 3 şekli, ortalama 0): üç testin birlikte geçirme oranı α'yı
    aşmaz (t + işaret çevirme ~4 katını geçiriyordu); çarpık küçük örnek koruması yalnız ENGELLER."""
    rng = np.random.default_rng(5)
    fw = rule(looks=[1.0], n_max=40, min_g=20)
    a_k = G.alpha_increments(fw)[0]
    n_pass = n_old = 0
    R = 300
    for r in range(R):
        w = rng.random(40) < 0.75
        x = np.where(w, 0.01, -0.03) + rng.normal(0, 0.002, 40)
        res = G.stage_b(fw, 1, x, reps=2000, seed=r)
        n_pass += res["decision"] == G.GECTI
        n_old += max(S.t_test_one(x)["p"], S.signflip_p(x, reps=2000, seed=r)) <= a_k
    assert n_pass / R <= a_k * 1.5 + 0.01, (n_pass / R, a_k)
    assert n_old > n_pass, "eski ikili daha çok geçiriyordu"
    # koruma: 25 küme, sola çarpık (bir büyük kayıp) ve güçlü ortalama → p küçük olsa da geçemez
    x = np.r_[np.full(24, 0.02) + rng.normal(0, 0.001, 24), [-0.10]]
    assert S.skewness(x) < G.SKEW_MIN
    res = G.stage_b(fw, 1, x, reps=2000)
    assert res["guards"]["skew"] is False and res["decision"] != G.GECTI
    big = np.tile(x, 3)                                                   # 75 küme: koruma kalkar
    assert G.stage_b(fw, 1, big, reps=2000)["guards"]["skew"] is True
    bar = rule(metric="barrier", tp=0.01, sl=0.01)
    assert G._skew_ok(bar, x), "barrier metriği korumaya girmez"
    print("✅ çarpıklık) üç test birlikte boş çarpık veride α içinde; küçük sola çarpık örnek geçemez")


def test_boot_t_basics():
    assert S.boot_t_p([0.1, 0.2]) == 1.0 and S.boot_t_p([0.1] * 10) == 1.0
    rng = np.random.default_rng(3)
    pos = rng.normal(0.5, 1, 60)
    assert S.boot_t_p(pos, reps=5000) < 0.01
    neg = rng.normal(-0.5, 1, 60)
    assert S.boot_t_p(neg, reps=5000) > 0.9
    assert S.boot_t_p(pos, reps=5000, seed=1) == S.boot_t_p(pos, reps=5000, seed=1), "tohum sabit"
    assert abs(S.skewness([1, 2, 3])) < 1e-12 and S.skewness([0, 0, 0, 10]) > 1
    print("✅ bootstrap-t) gerçek etkide küçük, ters etkide büyük p; tohum sabit; çarpıklık işareti")


def test_early_look_reachable_for_registered_rules():
    """İnceleme (08.10): OBF ilk bakış eşiği (~5e-6) Monte Carlo tabanının (1e-5) altındaydı — erken bakış
    hiç geçemiyordu. Tekrar sayısı eşiğe göre büyür; boş veride erken durur."""
    from app.lab import specs
    for r in specs.RULES:
        if r["evidence"] == "log":
            continue
        rule = {**r, "alpha": 0.05 / 40}
        a1 = G.alpha_increments(rule)[0]
        assert 1 / (1 + G.mc_reps(G.SF_REPS, a1)) < a1 / 10, (r["id"], a1)
    rule = {"id": "X", "evidence": "forward", "metric": "net", "alpha": 0.05 / 40, "looks": [0.5, 1.0],
            "n_max": 40, "min_g": 20}
    rng = np.random.default_rng(4)
    t0 = time.time()
    res = G.stage_b(rule, 1, 0.006 + rng.normal(0, 0.003, 20), seed=2)
    assert res["decision"] == G.GECTI, res
    t1 = time.time()
    null = G.stage_b(rule, 1, rng.normal(0, 0.003, 20), seed=3)
    assert null["decision"] != G.GECTI and time.time() - t1 < 1.0, "boş veride erken durur"
    print(f"✅ erken bakış) eşik 1/20 çözünürlükle ulaşılabilir; güçlü etki 1. bakışta geçer ({t1 - t0:.1f} sn);"
          " boş veri erken durur")
