"""Örüntü eşleştirme — "şu anki şekil geçmişte olduğunda sonra ne olmuş?"

Yöntem yeni değil: **analog (k-en-yakın-komşu) tahmini**. Son N barın şekli
z-normalize edilir, geçmişte aynı şekle yeterince yakın pencereler bulunur,
o pencerelerden SONRA ne olduğu toplanır → yön olasılığı + hareket dağılımı.

BU DOSYA SAF: veritabanı, ağ, config nesnesi yok — girdi dizi, çıktı sayı.
Böylece tek gerçek soru ("bu makine gürültüde kenar uyduruyor mu?") ağa
bağlanmadan sınanabiliyor. `scratchpad/test_analog.py`'deki NULL testi tam
olarak bunu ölçer ve bu özelliğin varlık şartıdır.

DÖRT TUZAK ve kapatılışları — hepsi `match()` içinde:

  1. İLERİYE BAKMA. Eşleşmenin ileri penceresi sorgu anından ÖNCE bitmeli;
     yoksa "geçmişte olanı" değil geleceği okumuş oluruz.
  2. KENDİNE BENZEME. Sorgu penceresiyle çakışan pencereler atılır.
  3. BAĞIMSIZLIK. Bir bar kayan iki pencere neredeyse aynı şeydir; üst üste
     binenler sayılırsa örneklem şişer, güven aralığı yalan söyler. Kabul
     edilenler arasında en az `win` bar aralık şartı var.
  4. "EN YAKIN N" TUZAĞI. Dar bir geçmişte en yakın 50 pencere, geçmişin
     dörtte biri demek olabilir — o benzerlik değil ortalamanın kendisidir.
     Bu yüzden hem BENZERLİK eşiği var hem de mevcut bağımsız pencerelerin
     `MAX_SHARE`'inden fazlası alınmıyor.

BENZERLİK NEDEN KORELASYON: z-normalize vektörlerde Öklid mesafesiyle
korelasyon birebir bağlı (d² = 2(1−r)), ama ham mesafe pencere uzunluğuna
göre anlam değiştirir — 23 boyutta rastgele iki şeklin mesafesi ~1.41'de
yoğunlaşır ve "0.6" gibi bir eşik pratikte HİÇBİR ŞEYİ geçirmez (ölçtük).
Korelasyon ise her uzunlukta aynı şeyi söyler. Ölçüm: 4300 pencerelik bir
geçmişte ulaşılabilen en iyi benzerlik ~0.65-0.70, r≥0.5 olan ~35 pencere.
Varsayılan eşik bu ölçüme dayanıyor, tahmine değil.

TABAN ORAN: "%62 yukarı" tek başına bilgi DEĞİLDİR. Aynı seride koşulsuz
oran %58'se haber yok demektir. `baseline()` her zaman hesaplanır ve
`verdict()` farkın standart hataya oranını (z) döndürür.
"""
import math

import numpy as np

# Bağımsız pencerelerin en fazla bu oranı eşleşme sayılabilir. Aşılırsa
# "benzerler" kümesi geçmişin geneline yaklaşır ve olasılık taban orana yapışır.
MAX_SHARE = 0.15
EPS = 1e-12


def shape(closes) -> np.ndarray | None:
    """Kapanış dizisi → z-normalize log getiri vektörü.

    Seviye (100$ mı 5$ mı) ve oynaklık ÖLÇEĞİ gider, ŞEKİL kalır. Böylece
    NVDA'nın 220$'daki hareketi SHEIN'in 6$'daki hareketiyle kıyaslanabilir.
    Düz çizgide (std=0) şekil tanımsızdır → None.
    """
    a = np.asarray(closes, dtype=float)
    if a.size < 3 or not np.all(np.isfinite(a)) or np.any(a <= 0):
        return None
    r = np.diff(np.log(a))
    sd = r.std()
    if sd < EPS:
        return None
    return (r - r.mean()) / sd


def _sliding(a: np.ndarray, win: int) -> np.ndarray:
    """(n-win+1, win) kayan pencere görünümü — kopya yok."""
    return np.lib.stride_tricks.sliding_window_view(a, win)


def build(closes, win: int, horizon: int):
    """Geçmişteki tüm pencereler + her birinin ileri getirisi.

    Döner: (X, fwd, end_idx)
      X       : (m, win-1) z-normalize şekil matrisi
      fwd     : (m,) pencere bittikten `horizon` bar sonraki % getiri
      end_idx : (m,) pencerenin BİTTİĞİ bar indeksi (çakışma denetimi için)
    """
    a = np.asarray(closes, dtype=float)
    n = a.size
    if n < win + horizon + 1:
        return None, None, None
    r = np.diff(np.log(a))                      # n-1 getiri
    W = _sliding(r, win - 1)                    # (n-win+1, win-1)
    sd = W.std(axis=1)
    ok = sd > EPS
    if not ok.any():
        return None, None, None
    X = (W - W.mean(axis=1, keepdims=True)) / np.where(ok, sd, 1.0)[:, None]
    # Pencere i, fiyat indeksi [i .. i+win-1] arasını kaplar → bitişi i+win-1
    end_idx = np.arange(W.shape[0]) + win - 1
    fut = end_idx + horizon
    valid = ok & (fut < n)                      # ileri penceresi TAMAMLANMIŞ olsun
    if not valid.any():
        return None, None, None
    X, end_idx = X[valid], end_idx[valid]
    fwd = (a[end_idx + horizon] / a[end_idx] - 1.0) * 100.0
    return X, fwd, end_idx


def corr_of(dist):
    """z-normalize vektörlerde mesafe → korelasyon. d² = 2(1−r)."""
    return 1.0 - (np.asarray(dist) ** 2) / 2.0


def _pick(order, corr, end_idx, win, top_k, min_corr):
    """Mesafeye göre sırayla gez; çakışmayanları kabul et (açgözlü seyreltme).

    Bir bar kayan iki pencere neredeyse aynı örnektir — ikisini de saymak
    örneklemi şişirir ve z değerini olduğundan büyük gösterir.
    """
    taken: list[int] = []
    for i in order:
        if corr[i] < min_corr:
            break                               # sıralı: buradan sonrası daha uzak
        e = end_idx[i]
        if any(abs(e - end_idx[j]) < win for j in taken):
            continue
        taken.append(int(i))
        if len(taken) >= top_k:
            break
    return taken


def match(query_closes, hist_closes, hist_ts, win: int, horizon: int,
          top_k: int = 50, min_corr: float = 0.5,
          exclude_after=None) -> dict:
    """Sorgu şeklinin geçmişteki bağımsız benzerleri + onların sonrası.

    `exclude_after`: bu zaman damgasından sonra BİTEN pencereler atılır.
    Sorgunun kendi barlarına ve sonrasına bakmamak için — ileriye bakma ve
    kendine benzeme tuzaklarının ikisi de burada kapanıyor.
    """
    out = {"n": 0, "n_pool": 0, "matches": [], "p_up": None, "med": None,
           "q25": None, "q75": None, "cap": 0, "reason": "", "best_corr": None}
    q = shape(np.asarray(query_closes, dtype=float)[-win:])
    if q is None:
        out["reason"] = "sorgu şekli okunamadı (düz seri ya da eksik veri)"
        return out
    X, fwd, end_idx = build(hist_closes, win, horizon)
    if X is None:
        out["reason"] = "geçmiş pencere üretilemedi (yetersiz bar)"
        return out

    ts = np.asarray(hist_ts, dtype=np.int64)
    if exclude_after is not None:
        keep = ts[end_idx + horizon] <= int(exclude_after)
        if not keep.any():
            out["reason"] = "sorgudan önce tamamlanmış pencere yok"
            return out
        X, fwd, end_idx = X[keep], fwd[keep], end_idx[keep]

    # Bağımsız pencere sayısı ≈ toplam / win. "En yakın 50" bunun büyük bir
    # kısmıysa artık benzerlik değil ortalama seçmiş oluruz.
    n_indep = max(1, X.shape[0] // max(1, win))
    cap = max(1, min(int(top_k), int(n_indep * MAX_SHARE)))
    out["n_pool"], out["cap"] = int(X.shape[0]), cap

    # Öklid mesafesi, uzunluğa göre normalize → korelasyona çevir
    d = np.sqrt(((X - q) ** 2).sum(axis=1) / X.shape[1])
    r = corr_of(d)
    order = np.argsort(-r, kind="stable")       # en benzerden başla
    out["best_corr"] = float(r.max())
    taken = _pick(order, r, end_idx, win, cap, min_corr)
    if not taken:
        # EN YAKIN kaçırılan da yazılsın: eşiğin doğru yerde olup olmadığını
        # ancak neyi ıskaladığımızı görerek anlarız.
        out["reason"] = (f"yeterince benzeyen pencere yok — en yakını "
                         f"{out['best_corr']:.2f} benzerlik, eşik {min_corr:.2f}")
        return out

    f = fwd[taken]
    out["n"] = len(taken)
    out["p_up"] = float((f > 0).mean() * 100)
    out["med"] = float(np.median(f))
    out["q25"], out["q75"] = float(np.percentile(f, 25)), float(np.percentile(f, 75))
    out["matches"] = [
        {"ts": int(ts[end_idx[i]]), "corr": float(r[i]), "fwd": float(fwd[i])}
        for i in taken]
    return out


def baseline(hist_closes, hist_ts, win: int, horizon: int,
             exclude_after=None) -> dict:
    """KOŞULSUZ dağılım: aynı seride, şekle bakmadan, ileri getiri.

    Sinyalin tek anlamlı ölçüsü budur — %62 yukarı, taban %58'se haber değil.
    """
    out = {"n": 0, "p_up": None, "med": None}
    X, fwd, end_idx = build(hist_closes, win, horizon)
    if X is None:
        return out
    if exclude_after is not None:
        ts = np.asarray(hist_ts, dtype=np.int64)
        keep = ts[end_idx + horizon] <= int(exclude_after)
        if not keep.any():
            return out
        fwd, end_idx = fwd[keep], end_idx[keep]
    # Taban da BAĞIMSIZ örneklerden: her `win` barda bir pencere al.
    step = fwd[::max(1, win)]
    if step.size == 0:
        return out
    out["n"] = int(step.size)
    out["p_up"] = float((step > 0).mean() * 100)
    out["med"] = float(np.median(step))
    return out


def verdict(fwd: dict, base: dict, min_matches: int = 20) -> dict:
    """Fark taban orandan ayırt edilebiliyor mu? Etiket + z.

    z = (p − p0) / sqrt(p0(1−p0)/n). Küçük n'de eşik geçilemez; bu bir kusur
    değil, dürüstlüktür — 12 örnekle "%75 ihtimalle yükselir" demek uydurmadır.
    """
    out = {"label": "yetersiz", "z": None, "edge": None, "dir": 0,
           "note": fwd.get("reason") or ""}
    n, p, p0 = fwd.get("n") or 0, fwd.get("p_up"), base.get("p_up")
    if p is None or p0 is None or n < max(1, int(min_matches)):
        out["note"] = out["note"] or f"yeterli benzer örnek yok (n={n})"
        return out
    edge = p - p0
    q0 = p0 / 100.0
    se = math.sqrt(max(q0 * (1 - q0), EPS) / n) * 100.0
    z = edge / se if se > EPS else 0.0
    out["edge"], out["z"] = edge, z
    out["dir"] = 1 if edge > 0 else (-1 if edge < 0 else 0)
    out["label"] = "kayda değer" if abs(z) >= 2 else "zayıf"
    if out["label"] == "zayıf":
        out["note"] = "taban orandan ayırt edilemiyor"
    return out


def analyze(query_closes, hist_closes, hist_ts, win: int, horizon: int,
            top_k: int = 50, min_corr: float = 0.5, min_matches: int = 20,
            exclude_after=None) -> dict:
    """match + baseline + verdict — çağıranın tek ihtiyacı bu."""
    fwd = match(query_closes, hist_closes, hist_ts, win, horizon,
                top_k, min_corr, exclude_after)
    base = baseline(hist_closes, hist_ts, win, horizon, exclude_after)
    v = verdict(fwd, base, min_matches)
    out = {**fwd, "base_up": base.get("p_up"), "base_med": base.get("med"),
           "base_n": base.get("n"), **v}
    # YAPISAL YETERSİZLİK: bağımsızlık tavanı asgari eşleşme sayısının altındaysa
    # bu sembol/vade ASLA sonuç üretemez. "Benzer bulunamadı" gibi görünüp
    # sonsuza kadar sessiz kalmasın — sebebi ve çaresi yazılsın.
    if out["label"] == "yetersiz" and fwd.get("cap") and fwd["cap"] < min_matches:
        out["structural"] = True
        out["note"] = (f"arşiv yapısal olarak yetersiz: bu geçmişten en fazla "
                       f"{fwd['cap']} bağımsız eşleşme alınabiliyor, asgari "
                       f"{min_matches} isteniyor — arşiv derinliğini artır, "
                       f"asgari eşleşmeyi düşür ya da havuzu 'class' yap")
    return out
