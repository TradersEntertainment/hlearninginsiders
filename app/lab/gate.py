"""🚪 Tek kapı — kuralın durumu yalnız buradan değişir (saf karar: DB yok, ağ yok, asyncio yok; Faz 4, C; 08.10).

Durumlar (ASCII): aday (Aşama A bekliyor) → kagit (ileri veride ölçülüyor) → gecti (sahibin onayını
bekler) → canli → durdu; her yerden emekli. "devam" durum değil, "değişiklik yok" kararıdır.

Akış:
  • evidence 'log': hiç bakılmaz, karar yok (yalnız describe).
  • 'frozen_backtest': Aşama A donmuş geçmişte BİR kez, α_A = bt_alpha_share·α. Geçerse kagit;
    geçmezse ya da küme < min_g ise emekli — yeniden denenmez. Kalan α_B = α − α_A ileri bakışlara.
  • 'forward': Aşama A yok, α_B = α.
  • Aşama B: kayıttan SONRAKİ küme sayısı her eşiği (⌈frac·n_max⌉, en az min_g) geçince o bakış bir
    kez yapılır; k. bakışın p eşiği O'Brien-Fleming harcamasının ARTIŞI α_k (birleşim sınırı —
    bakışlar arası bağımlılık ne olursa olsun toplam yanlış pozitif ≤ α_B, temkinli).
  • net: p = max(t, işaret çevirme) — ikisi de geçmeli. barrier: tam binom, k = #tp, n = #tp + #sl
    (zaman aşımı ayrı sınıf, sayılmaz), p0 = sl/(tp+sl) = execsim.barrier_p (martingale altında TP'nin
    önce gelme olasılığı). İSTİSNA tp < sl: zaman aşımı bağlayıcıyken sonuçlananlar arasında YAKIN
    seviye (TP) fazla temsil edilir → yalnız sonuçlananlarla p iyimser (rastgele yürüyüşte α = 0.05'te
    ~%25 yanlış geçiş). Orada zaman aşımı ve "open" BAŞARISIZ sayılır (n = tüm olaylar): "TP önce VE
    süre içinde/şimdiye dek" ⊂ "TP önce" → olasılık ≤ p0, test geçerli (temkinli). tp ≥ sl'de
    sözleşmedeki biçim zaten temkinli (yakın seviye SL). Bilinmeyen sonuç etiketi → ValueError.
  • Korumalar YALNIZ engeller, geçiş vermez: n_c ≥ min_g; ortalama net > 0; iki yarı aynı işaret.
  • Son bakışta geçemeyen emekli. Boşunalık bağlayıcı değil (yalnız erken bırakır, α'ya dokunmaz):
    frac ≥ 0.5 ve ortalama ≤ 0 → emekli.
  • Canlıda düşüş: tek yönlü t (H1: ortalama < 0), p ≤ 0.05 → durdu. Geçişteki tahmine karşı DEĞİL
    0'a karşı: geçişteki tahmin seçilim yüzünden şişkindir (kazananın laneti), ona karşı test haksız
    durdururdu.
xc = zaman sıralı küme ortalamaları (stats.cluster_means); sonlu olmayanlar atılır, n_c onlardan sonra.
"""
from __future__ import annotations

import math

import numpy as np

from app.lab import execsim
from app.lab import stats as S

STATUSES = ("aday", "kagit", "gecti", "canli", "durdu", "emekli")
ADAY, KAGIT, GECTI, CANLI, DURDU, EMEKLI = STATUSES
DEVAM = "devam"                 # karar: durum değişmez
EVIDENCE = ("forward", "frozen_backtest", "log")
METRICS = ("net", "barrier")
MIN_G = 20                      # bakış / Aşama A için en az küme (kural vermezse)
BT_ALPHA_SHARE = 0.25           # Aşama A'nın α payı (kural vermezse)
FUTILITY_FRAC = 0.5             # boşunalık bu orandan itibaren
DEMOTE_ALPHA = 0.05             # canlıda düşüş testi
SF_REPS = 100_000               # işaret çevirme Monte Carlo tekrarı (n > 16)
SKEW_MIN = -1.0                 # net: örnek bundan sola çarpıksa (seyrek büyük kayıp) ...
SKEW_N = 60                     # ... en az bu kadar küme olmadan geçemez (yalnız ENGELLER)
REASONS = ("tp", "sl", "timeout", "open")       # barrier sonuç etiketleri (execsim.path_exit reason)


def _arr(x) -> np.ndarray:
    """Düz float dizi; None/NaN/±inf atılır (stats ile aynı kural)."""
    a = np.asarray(x if x is not None else [], dtype=float).ravel()
    return a[np.isfinite(a)]


# ── kural alanları ─────────────────────────────────────────────────────────────

def _evidence(rule: dict) -> str:
    ev = rule.get("evidence", "forward")
    if ev not in EVIDENCE:
        raise ValueError(f"bilinmeyen evidence: {ev!r}")
    return ev


def _metric(rule: dict) -> str:
    m = rule.get("metric", "net")
    if m not in METRICS:
        raise ValueError(f"bilinmeyen metric: {m!r}")
    return m


def _alpha(rule: dict) -> float:
    a = float(rule.get("alpha") or 0.0)
    if not 0.0 <= a < 1.0:
        raise ValueError(f"alpha [0, 1) içinde olmalı: {a!r}")
    return a


def _min_g(rule: dict) -> int:
    v = rule.get("min_g")
    return MIN_G if v is None else max(1, int(v))


def _looks(rule: dict) -> list[float]:
    fr = [float(f) for f in (rule.get("looks") or ())]
    if any(not (0.0 < f <= 1.0) for f in fr) or any(b <= a for a, b in zip(fr, fr[1:])):
        raise ValueError(f"bakışlar (0, 1] içinde kesin artan olmalı: {rule.get('looks')!r}")
    return fr


def alpha_split(rule: dict) -> tuple[float, float]:
    """(α_A, α_B): frozen_backtest → (bt_alpha_share·α, α − α_A); forward → (0, α); log → (0, 0)."""
    ev, a = _evidence(rule), _alpha(rule)
    if ev == "log":
        return 0.0, 0.0
    if ev != "frozen_backtest":
        return 0.0, a
    share = rule.get("bt_alpha_share")
    share = BT_ALPHA_SHARE if share is None else float(share)
    if not 0.0 <= share < 1.0:
        raise ValueError(f"bt_alpha_share [0, 1) içinde olmalı: {share!r}")
    a_a = share * a
    return a_a, a - a_a


def alpha_increments(rule: dict) -> list[float]:
    """Aşama B'nin bakış başına p eşikleri: gs_increments(looks, α_B); α_B = 0 → hepsi 0."""
    fr = _looks(rule)
    a_b = alpha_split(rule)[1]
    if not fr:
        return []
    return S.gs_increments(fr, a_b) if a_b > 0 else [0.0] * len(fr)


def thresholds(rule: dict) -> list[int]:
    """Her bakışın ileri küme eşiği: ⌈frac·n_max⌉ (0.55·100 = 55.000…01 → 55), en az min_g."""
    n_max = int(rule.get("n_max") or 0)
    mg = _min_g(rule)
    return [max(mg, math.ceil(f * n_max - 1e-9)) for f in _looks(rule)]


def barrier_p0(rule: dict) -> float:
    """p0 = sl/(tp+sl); tp/sl kuralın üst düzeyinde ya da exit={tp, sl, timeout} içinde."""
    ex = rule.get("exit") or {}
    tp = rule["tp"] if rule.get("tp") is not None else ex.get("tp")
    sl = rule["sl"] if rule.get("sl") is not None else ex.get("sl")
    if tp is None or sl is None:
        raise ValueError("barrier kuralı tp ve sl ister")
    return execsim.barrier_p(float(tp), float(sl))


def _reason(o) -> str | None:
    """Sonuç: "tp"|"sl"|"timeout" ya da execsim.path_exit sözlüğü (reason alanı)."""
    return o.get("reason") if isinstance(o, dict) else o


def _p(rule: dict, a: np.ndarray, outcomes, reps: int, seed: int) -> tuple[float, dict]:
    """Kuralın metriğine göre tek yönlü p ve ayrıntısı (barrier: tp < sl'de zaman aşımı/open başarısız)."""
    if _metric(rule) == "net":
        p_t = float(S.t_test_one(a)["p"])
        p_sf = float(S.signflip_p(a, reps=reps, seed=seed))
        p_bt = float(S.boot_t_p(a, reps=reps, seed=seed))
        return max(p_t, p_sf, p_bt), {"p_t": p_t, "p_sf": p_sf, "p_bt": p_bt, "skew": S.skewness(a)}
    if outcomes is None:
        raise ValueError("barrier: outcomes (tp/sl/timeout listesi) gerekli")
    p0 = barrier_p0(rule)
    rs = [_reason(o) for o in outcomes]
    bad = sorted({str(r) for r in rs} - set(REASONS))
    if bad:
        raise ValueError(f"barrier: bilinmeyen sonuç etiketi {bad!r} (beklenen {REASONS})")
    k, n_sl, n_to, n_open = (rs.count(r) for r in REASONS)
    as_fail = p0 > 0.5                              # tp < sl: zaman aşımı/open başarısız (modül belgesi)
    n = k + n_sl + (n_to + n_open if as_fail else 0)
    return float(S.binom_sf(k, n, p0)), {"k": k, "n": n, "timeout": n_to, "open": n_open, "p0": p0,
                                         "timeout_as_fail": as_fail}


def _skew_ok(rule: dict, a: np.ndarray) -> bool:
    """Engelleyen koruma (net): sola çarpık küçük örnek geçemez — büyük kayıp henüz görülmemişken
    ortalama şişkin görünür. Barrier metriği binom testindedir, korumaya girmez."""
    if _metric(rule) != "net" or a.size >= SKEW_N:
        return True
    return S.skewness(a) >= SKEW_MIN


# ── bakış zamanı ───────────────────────────────────────────────────────────────

def look_due(rule: dict, g_forward: int, looks_done: list[int]) -> int | None:
    """Sıradaki bakış (son yapılanın bir sonrakisi, 1'den) eşiğine ulaşıldıysa numarası, yoksa None.
    Bakışlar sırayla ve birer kez döner: eşikler toptan aşılsa da önce sıradaki döner. log → hep None."""
    if _evidence(rule) == "log":
        return None
    thr = thresholds(rule)
    nxt = max((int(k) for k in (looks_done or ())), default=0) + 1
    if nxt > len(thr):
        return None
    return nxt if int(g_forward) >= thr[nxt - 1] else None


# ── Aşama A ────────────────────────────────────────────────────────────────────

def stage_a(rule: dict, xc, outcomes=None, *, reps: int = SF_REPS, seed: int = 0) -> dict:
    """Donmuş geçmişte BİR kez (yalnız frozen_backtest): p ≤ α_A → kagit; değilse ya da n_c < min_g
    → emekli (yeniden denenmez). Döner: decision, p, alpha_used, n_c (+ why, test)."""
    if _evidence(rule) != "frozen_backtest":
        raise ValueError("Aşama A yalnız frozen_backtest kuralları içindir")
    a = _arr(xc)
    n_c = int(a.size)
    a_a = alpha_split(rule)[0]
    p, test = _p(rule, a, outcomes, reps, seed)
    if n_c < _min_g(rule):
        dec, why = EMEKLI, f"yetersiz küme ({n_c} < {_min_g(rule)})"
    elif not _skew_ok(rule, a):
        dec, why = EMEKLI, f"sola çarpık örnek (g1 {S.skewness(a):.2f} < {SKEW_MIN:g}) ve {n_c} < {SKEW_N} küme"
    elif a_a > 0 and p <= a_a:
        dec, why = KAGIT, f"p {p:.4g} ≤ α_A {a_a:.4g}"
    else:
        dec, why = EMEKLI, f"p {p:.4g} > α_A {a_a:.4g}"
    return {"decision": dec, "p": p, "alpha_used": a_a, "n_c": n_c, "why": why, "test": test}


# ── Aşama B ────────────────────────────────────────────────────────────────────

def stage_b(rule: dict, look_no: int, xc, outcomes=None, *, reps: int = SF_REPS, seed: int = 0) -> dict:
    """İleri veride look_no. bakış: p ≤ α_k VE korumalar → gecti; son bakışta geçemedi → emekli;
    boşunalık (frac ≥ 0.5, ortalama ≤ 0) → emekli; aksi devam.
    Döner: decision, p, alpha_k, n_c, guards (+ frac, mean, why, test)."""
    if _evidence(rule) == "log":
        raise ValueError("log kuralına bakılmaz (karar gücü yok)")
    fr = _looks(rule)
    if not 1 <= int(look_no) <= len(fr):
        raise ValueError(f"bakış numarası 1..{len(fr)} olmalı: {look_no!r}")
    k = int(look_no)
    a_k = alpha_increments(rule)[k - 1]
    a = _arr(xc)
    n_c = int(a.size)
    mean = float(a.mean()) if n_c else float("nan")
    p, test = _p(rule, a, outcomes, reps, seed)
    guards = {"min_g": n_c >= _min_g(rule), "mean_pos": mean > 0,
              "half_split": bool(S.half_split_same_sign(a)), "skew": _skew_ok(rule, a)}
    frac, last = fr[k - 1], k == len(fr)
    if a_k > 0 and p <= a_k and all(guards.values()):
        dec, why = GECTI, f"p {p:.4g} ≤ α_{k} {a_k:.4g}, korumalar tamam"
    elif last:
        dec, why = EMEKLI, "son bakışta geçemedi"
    elif frac >= FUTILITY_FRAC and not mean > 0:
        dec, why = EMEKLI, f"boşunalık: frac {frac:g} ≥ {FUTILITY_FRAC:g}, ortalama ≤ 0"
    else:
        dec, why = DEVAM, "sonraki bakışa"
    return {"decision": dec, "p": p, "alpha_k": a_k, "n_c": n_c, "guards": guards,
            "frac": frac, "mean": mean, "why": why, "test": test}


# ── canlı ──────────────────────────────────────────────────────────────────────

def demote_check(xc_live, est_at_pass) -> str:
    """Canlıda düşüş: tek yönlü t (H1: ortalama < 0), p ≤ 0.05 → "durdu"; değilse "devam".
    est_at_pass yalnız künyedir — karar 0'a karşı (geçişteki tahmin seçilimle şişkindir)."""
    a = _arr(xc_live)
    return DURDU if S.t_test_one(-a)["p"] <= DEMOTE_ALPHA else DEVAM


# ── betimleme ──────────────────────────────────────────────────────────────────

def describe(rule: dict, xc, looks_done: list[int] | None = None, xe=None) -> dict:
    """Yalnız betimleme (karar gücü YOK): effect_summary + bakış planı + şimdiki bakış sınırı.
    bound = sıradaki (yapılmamış) bakış {look, frac, g, alpha_k, due}; due = n_c ≥ g (look_due ile
    aynı sıra). log ya da bakışlar bitti → None."""
    a = _arr(xc)
    out = S.effect_summary(a, xe)
    ev = _evidence(rule)
    a_a, a_b = alpha_split(rule)
    plan: list[dict] = []
    if ev != "log":
        fr = _looks(rule)
        plan = [{"look": i + 1, "frac": f, "g": g, "alpha_k": ak}
                for i, (f, g, ak) in enumerate(zip(fr, thresholds(rule), alpha_increments(rule)))]
    nxt = max((int(k) for k in (looks_done or ())), default=0) + 1
    bound = dict(plan[nxt - 1], due=out["n_c"] >= plan[nxt - 1]["g"]) if nxt <= len(plan) else None
    out.update({"evidence": ev, "metric": _metric(rule), "alpha": _alpha(rule), "alpha_a": a_a,
                "alpha_b": a_b, "plan": plan, "bound": bound})
    return out
