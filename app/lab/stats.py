"""🧪 Kümeli, dürüst istatistik — kapının (gate) kullandığı saf numpy araçları (Faz 4, B; 08.10).

İlkeler:
  • Birim olay değil KÜME: aynı takvim bloğundaki olaylar (block_key) tek gözlemdir; küme içinde
    eşit ağırlık (cluster_means). Örtüşen ufuklar önce inceltilir (thin_nonoverlap) — aynı coin'in
    ufku bitmeden gelen ikinci olayı sayılmaz.
  • Tek yönlü H1: ortalama > 0. Karar iki testin İKİSİNİ de ister (gate: p = max(t, işaret
    çevirme)) — t kalın kuyrukta, işaret çevirme küçük n'de dürüst kalır; birinin zayıflığı diğerini
    kurtarmaz.
  • scipy yok: Student-t CDF düzenlenmiş eksik betadan (sürekli kesir, Lentz); normal için
    statistics.NormalDist; binom kuyruğu log-uzayda tam toplam.
  • Ardışık bakış: Lan-DeMets O'Brien-Fleming harcaması; her bakışa yalnız ARTIŞ düşer (birleşim
    sınırı — bakışlar arasındaki bağımlılık ne olursa olsun toplam yanlış pozitif ≤ α, temkinli).
  • Betimleme (wilson_ci, bootstrap_ci, effect_summary) karar vermez.
  • Sonlu olmayan değerler (None/NaN/±inf) dizilerden atılır; n onlardan sonra sayılır.
"""
from __future__ import annotations

import math
import statistics

import numpy as np

_N = statistics.NormalDist()
EXACT_MAX = 16                  # işaret çevirme: n ≤ 16 → 2^n tam sayım (≤ 65 536 satır)
MC_CHUNK = 2_000_000            # Monte Carlo parça boyu (eleman) → bool + float64 ≈ 18 MB
OBF_INFLATE = 1.03              # OBF iki bakış için sabit örnekleme göre ~%3 şişirme
_SD_EPS = 1e-12                 # t testi: sd ≤ _SD_EPS·max|x| → sd = 0 sayılır (karar yok)


def _clean(x) -> np.ndarray:
    """Düz float dizi; None/NaN/±inf atılır."""
    a = np.asarray(x if x is not None else [], dtype=float).ravel()
    return a[np.isfinite(a)]


# ── inceltme ve kümeleme ────────────────────────────────────────────────────────

def thin_nonoverlap(ts: list[int], coins: list[str], h: int) -> list[bool]:
    """Coin başına zaman sırasıyla ilkini tut; sonrakini ancak ts ≥ son_tutulan + h ise tut
    (örtüşen ufuklar tek olay). Girdi sırası korunur; aynı ts'de girdi sırası öncedir."""
    if len(ts) != len(coins):
        raise ValueError("ts ve coins aynı uzunlukta olmalı")
    keep = [False] * len(ts)
    last: dict[str, int] = {}
    for i in sorted(range(len(ts)), key=lambda j: (ts[j], j)):
        c = coins[i]
        if c not in last or ts[i] >= last[c] + h:
            keep[i] = True
            last[c] = ts[i]
    return keep


def block_key(ts: int, width_s: int, offset_s: int = 0) -> int:
    """Takvim bloğu numarası: (ts − offset_s) // width_s (genişlik ≥ ana ufuk — kural belirler)."""
    if width_s <= 0:
        raise ValueError("width_s > 0 olmalı")
    return (int(ts) - int(offset_s)) // int(width_s)


def cluster_means(vals, keys) -> tuple[np.ndarray, np.ndarray, list]:
    """Anahtar başına eşit ağırlıklı ortalama, anahtara göre artan: (ortalamalar, boyutlar,
    anahtarlar). Sonlu olmayan değer atılır; hiç değeri kalmayan anahtar listede yer almaz."""
    v = np.asarray(vals if vals is not None else [], dtype=float).ravel()
    keys = list(keys)
    if len(v) != len(keys):
        raise ValueError("vals ve keys aynı uzunlukta olmalı")
    acc: dict = {}
    for x, k in zip(v.tolist(), keys):
        if math.isfinite(x):
            s = acc.setdefault(k, [0.0, 0])
            s[0] += x
            s[1] += 1
    ks = sorted(acc)
    means = np.array([acc[k][0] / acc[k][1] for k in ks], dtype=float)
    sizes = np.array([acc[k][1] for k in ks], dtype=int)
    return means, sizes, ks


# ── Student-t (scipy'siz) ──────────────────────────────────────────────────────

def _betacf(a: float, b: float, x: float) -> float:
    """Eksik beta sürekli kesri (Lentz düzeltmeli)."""
    tiny, eps = 1e-300, 1e-15
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 20_000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        de = d * c
        h *= de
        if abs(de - 1.0) < eps:
            break
    return h


def _betainc(a: float, b: float, x: float, y: float | None = None) -> float:
    """Düzenlenmiş eksik beta I_x(a, b); y = 1 − x ayrıca hassas verilebilir (x ≈ 1'de iptal yok)."""
    y = 1.0 - x if y is None else y
    if x <= 0.0:
        return 0.0
    if y <= 0.0:
        return 1.0
    lbt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(y)
    bt = math.exp(lbt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, y) / b


def _t_tail(t: float, df: float) -> float:
    """P(T ≥ |t|) — tek kuyruk, 1 − CDF'nin iptal hatası olmadan (t ≈ 0'da 1 − x ayrıca hesaplanır)."""
    t2 = t * t
    return 0.5 * _betainc(df / 2.0, 0.5, df / (df + t2), t2 / (df + t2))


def t_cdf(t: float, df: int) -> float:
    """Student-t CDF, df ≥ 1 (kesirli df de olur)."""
    if not df > 0:
        raise ValueError("df > 0 olmalı")
    t = float(t)
    if math.isnan(t):
        return float("nan")
    if math.isinf(t):
        return 1.0 if t > 0 else 0.0
    tail = _t_tail(t, float(df))
    return 1.0 - tail if t > 0 else tail


def _t_sf(t: float, df: float) -> float:
    """1 − t_cdf(t, df), sağ kuyrukta hassas."""
    if math.isinf(t):
        return 0.0 if t > 0 else 1.0
    tail = _t_tail(t, df)
    return tail if t > 0 else 1.0 - tail


def t_test_one(x) -> dict:
    """Tek örneklem, tek yönlü t (H1: ortalama > 0): dict(n, mean, sd, se, t, p); df = n − 1,
    p = 1 − t_cdf(t, n − 1). n < 2 ya da tüm değerler eşit (sd = 0; göreli 1e-12 içinde — 0.3 ile
    0.1+0.2 gibi ulp farkı dev t üretmesin) → p = 1.0 (karar yok)."""
    a = _clean(x)
    n = int(a.size)
    nan = float("nan")
    mean = float(a.mean()) if n else nan
    if n < 2:
        return {"n": n, "mean": mean, "sd": nan, "se": nan, "t": nan, "p": 1.0}
    sd = float(a.std(ddof=1))
    if not sd > _SD_EPS * float(np.abs(a).max()):     # sd = 0 ya da yalnız kayan nokta gürültüsü
        return {"n": n, "mean": mean, "sd": 0.0, "se": 0.0, "t": nan, "p": 1.0}
    se = sd / math.sqrt(n)
    t = mean / se
    return {"n": n, "mean": mean, "sd": sd, "se": se, "t": t, "p": min(1.0, max(0.0, _t_sf(t, n - 1)))}


# ── işaret çevirme ─────────────────────────────────────────────────────────────

def signflip_p(x, reps: int = 100_000, seed: int = 0) -> float:
    """Tek yönlü işaret çevirme p'si (küme ortalamaları, H1: ortalama > 0; H0: simetrik etrafında 0).

    Çevrilmiş toplam = S − 2·(çevrilenlerin toplamı) ≥ S ⟺ çevrilenlerin toplamı ≤ 0 (göreli 1e-12
    tolerans; eşitlik sayılır). n ≤ 16 → 2^n tam sayım (özdeşlik dahil, p ≥ 2^−n); değilse Monte
    Carlo, parçalı (bellek < 100 MB): p = (1 + #{çevrilmiş ≥ gözlenen}) / (1 + reps). n = 0 → 1.0."""
    a = _clean(x)
    n = int(a.size)
    if n == 0:
        return 1.0
    tol = 0.5e-12 * float(np.abs(a).sum())
    if n <= EXACT_MAX:
        idx = np.arange(1 << n, dtype=np.int64)
        flips = ((idx[:, None] >> np.arange(n, dtype=np.int64)) & 1).astype(float)
        hits = int(np.count_nonzero(flips @ a <= tol))
        return hits / float(1 << n)
    reps = max(1, int(reps))
    rng = np.random.default_rng(seed)
    chunk = max(1, MC_CHUNK // n)
    hits, done = 0, 0
    while done < reps:
        m = min(chunk, reps - done)
        flips = rng.random((m, n)) < 0.5
        hits += int(np.count_nonzero(flips @ a <= tol))
        done += m
    return (1 + hits) / (1 + reps)


def boot_t_p(x, reps: int = 100_000, seed: int = 0) -> float:
    """Tek yönlü bootstrap-t p'si (H1: ortalama > 0): yeniden örneklenmiş t* = (m* − m)/(s*/√n),
    p = (1 + #{t* ≥ t_gözlenen}) / (1 + reps). Sola çarpık getiride (seyrek büyük kayıp: TP/SL,
    kısa vadeli satış primi) t ve işaret çevirme iyimserdir — bu ikisinin yanına üçüncü test olarak
    girer (08.10 benzetimi: TP 1 / SL 3 şeklinde boş veride α=0.005'te t+işaret %2.2 geçiriyordu,
    üçü birlikte ≈0). Yozlaşmış yeniden örnek (sd = 0) temkinle "aşan" sayılır. n < 3 ya da sd = 0 → 1.0."""
    a = _clean(x)
    n = int(a.size)
    if n < 3:
        return 1.0
    m = float(a.mean())
    sd = float(a.std(ddof=1))
    eps = _SD_EPS * float(np.abs(a).max())
    if not sd > eps:
        return 1.0
    rn = math.sqrt(n)
    t_obs = m / (sd / rn)
    reps = max(1, int(reps))
    rng = np.random.default_rng(seed)
    chunk = max(1, MC_CHUNK // n)
    hits, done = 0, 0
    while done < reps:
        k = min(chunk, reps - done)
        b = a[rng.integers(0, n, (k, n))]
        mb = b.mean(axis=1)
        sb = b.std(axis=1, ddof=1)
        ok = sb > eps
        tb = (mb - m) / (np.where(ok, sb, 1.0) / rn)
        hits += int(np.count_nonzero(~ok | (tb >= t_obs)))
        done += k
    return (1 + hits) / (1 + reps)


def skewness(x) -> float:
    """Örneklem çarpıklığı g1 (betimleme ve engelleyen koruma). n < 3 ya da sd = 0 → 0.0."""
    a = _clean(x)
    if a.size < 3:
        return 0.0
    d = a - a.mean()
    s = float(np.sqrt((d ** 2).mean()))
    return float((d ** 3).mean() / s ** 3) if s > 0 else 0.0


# ── binom ve aralıklar ─────────────────────────────────────────────────────────

def binom_sf(k: int, n: int, p0: float) -> float:
    """P(X ≥ k), X ~ Bin(n, p0) — tam, log-uzayda toplam. p0 ∉ [0, 1] (NaN dahil) → ValueError."""
    k, n = int(k), int(n)
    if n < 0:
        raise ValueError("n ≥ 0 olmalı")
    if k <= 0:
        return 1.0
    p0 = float(p0)
    if not 0.0 <= p0 <= 1.0:                        # NaN de: sessiz p = 0 "anlamlı" sayılırdı
        raise ValueError(f"p0 [0, 1] içinde olmalı: {p0!r}")
    if k > n:
        return 0.0
    if p0 == 0.0:
        return 0.0
    if p0 == 1.0:
        return 1.0
    lp, lq = math.log(p0), math.log1p(-p0)
    lc = math.lgamma(n + 1)
    terms = [lc - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq
             for i in range(k, n + 1)]
    mx = max(terms)
    s = mx + math.log(sum(math.exp(v - mx) for v in terms))
    return min(1.0, max(0.0, math.exp(s)))


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson skor aralığı (yalnız betimleme). n = 0 → (0, 1): bilgi yok."""
    if n <= 0:
        return 0.0, 1.0
    p = min(1.0, max(0.0, k / n))
    z2 = z * z
    den = 1.0 + z2 / n
    mid = (p + z2 / (2 * n)) / den
    half = z / den * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, mid - half), min(1.0, mid + half)


def bootstrap_ci(x, reps: int = 5000, alpha: float = 0.05, seed: int = 0) -> tuple[float, float]:
    """Ortalama için basit yüzdelik bootstrap (küme ortalamaları bağımsız kabul — küme genişliği ≥
    ufuk). n < 2 → (nan, nan)."""
    a = _clean(x)
    n = int(a.size)
    if n < 2:
        return float("nan"), float("nan")
    reps = max(1, int(reps))
    rng = np.random.default_rng(seed)
    chunk = max(1, MC_CHUNK // n)
    out = np.empty(reps, dtype=float)
    done = 0
    while done < reps:
        m = min(chunk, reps - done)
        out[done:done + m] = a[rng.integers(0, n, size=(m, n))].mean(axis=1)
        done += m
    lo, hi = np.quantile(out, [alpha / 2, 1 - alpha / 2])
    return float(lo), float(hi)


# ── beta ───────────────────────────────────────────────────────────────────────

def beta_dimson(r, rb, lags: int = 1, shrink: float = 1 / 3,
                cap: tuple[float, float] = (0.0, 3.0)) -> float:
    """Dimson betası: Σ_{k=0..lags} cov(r_t, rb_{t−k}) / var(rb) — gecikmeli tepkiyi (seyrek işlem)
    de toplar; 1'e büzülür: β = (1−shrink)·β_ham + shrink·1; cap içine kırpılır.
    r ve rb aynı zaman eksenine hizalı; çiftlerden biri sonlu değilse o çift atılır.
    Sonlu eş zamanlı çift < 10 ya da var(rb) = 0 → 1.0."""
    r = np.asarray(r, dtype=float).ravel()
    rb = np.asarray(rb, dtype=float).ravel()
    if r.size != rb.size:
        raise ValueError("r ve rb aynı uzunlukta olmalı")
    ok0 = np.isfinite(r) & np.isfinite(rb)
    if int(ok0.sum()) < 10:
        return 1.0
    b = rb[np.isfinite(rb)]
    var = float(b.var(ddof=1))
    if not var > 0:
        return 1.0
    raw = 0.0
    for k in range(max(0, int(lags)) + 1):
        y = r[k:]
        z = rb[:rb.size - k]
        ok = np.isfinite(y) & np.isfinite(z)
        if int(ok.sum()) < 3:
            continue
        y, z = y[ok], z[ok]
        raw += float(((y - y.mean()) * (z - z.mean())).sum() / (y.size - 1)) / var
    beta = (1.0 - shrink) * raw + shrink * 1.0
    return float(min(cap[1], max(cap[0], beta)))


# ── ardışık bakış ve güç ───────────────────────────────────────────────────────

def obf_spend(t: float, alpha: float) -> float:
    """Lan-DeMets O'Brien-Fleming tek yönlü harcama: α(t) = 2·(1 − Φ(Φ⁻¹(1 − α/2)/√t)),
    0 < t ≤ 1; α(1) = α. t ≤ 0 → 0; t > 1 → α; α ≤ 0 → 0 (hiç harcama yok)."""
    if t <= 0.0 or alpha <= 0.0:
        return 0.0
    if t >= 1.0:
        return float(alpha)                         # tam: artışlar toplamı tam α
    z = _N.inv_cdf(1.0 - alpha / 2.0)
    return math.erfc(z / math.sqrt(2.0 * t))        # = 2·(1 − Φ(z/√t)), erken t'de iptalsiz


def gs_increments(fracs: list[float], alpha: float) -> list[float]:
    """Her bakışta harcanan artış α(t_k) − α(t_{k−1}) (t_0 = 0). Bakışlar kesin artan, (0, 1].
    Toplam = α(son) = α (son frac 1 ise). Artış o bakışın kendi p eşiğidir (birleşim sınırı)."""
    fr = [float(f) for f in fracs]
    if not fr:
        return []
    if any(not (0.0 < f <= 1.0) for f in fr) or any(b <= a for a, b in zip(fr, fr[1:])):
        raise ValueError(f"bakışlar (0, 1] içinde kesin artan olmalı: {fracs!r}")
    out, prev = [], 0.0
    for f in fr:
        cur = obf_spend(f, alpha)
        out.append(max(0.0, cur - prev))
        prev = cur
    return out


def power_n(sigma: float, delta: float, alpha: float, power: float = 0.8,
            inflate: float = OBF_INFLATE) -> int:
    """Tek yönlü sabit örneklem ⌈((z_{1−α} + z_{power})·σ/δ)²·inflate⌉ küme; inflate = 1.03 OBF
    şişirmesi (sabit örneklem için 1.0). En az 2 (t testi n ≥ 2 ister). δ ≤ 0 → ValueError."""
    if not delta > 0:
        raise ValueError("delta > 0 olmalı")
    if not sigma > 0:
        return 2
    z = _N.inv_cdf(1.0 - alpha) + _N.inv_cdf(power)
    v = (z * sigma / delta) ** 2 * inflate
    return max(2, math.ceil(v - 1e-9))


# ── betimleme ve koruma ─────────────────────────────────────────────────────────

def effect_summary(xc, xe=None) -> dict:
    """Yalnız betimleme: küme ortalamaları xc → n_c, mean, ci (bootstrap), sd, worst (en kötü
    küme), q05; olay düzeyi xe verilirse n_ev, hit (pozitif pay), worst_ev."""
    a = _clean(xc)
    n = int(a.size)
    nan = float("nan")
    out = {
        "n_c": n,
        "mean": float(a.mean()) if n else nan,
        "ci": bootstrap_ci(a),
        "sd": float(a.std(ddof=1)) if n >= 2 else nan,
        "worst": float(a.min()) if n else nan,
        "q05": float(np.quantile(a, 0.05)) if n else nan,
    }
    if xe is not None:
        e = _clean(xe)
        m = int(e.size)
        out["n_ev"] = m
        out["hit"] = float((e > 0).mean()) if m else nan
        out["worst_ev"] = float(e.min()) if m else nan
    return out


def half_split_same_sign(xc) -> bool:
    """Zaman sıralı küme ortalamalarının ilk ve son yarısının ortalamaları aynı (sıfırdan farklı)
    işarette mi. Tek n'de ortadaki küme dışarıda kalır (yarılar eşit). Yalnız ENGELLEYEN koruma;
    n < 4 → False."""
    a = _clean(xc)
    n = int(a.size)
    if n < 4:
        return False
    h = n // 2
    m1, m2 = float(a[:h].mean()), float(a[n - h:].mean())
    return (m1 > 0 and m2 > 0) or (m1 < 0 and m2 < 0)
