"""🧪 Strateji laboratuvarı — yürütme benzetimi, ileriye bakışsız (Faz 4, A).

Pinlenenler:
  • giriş: t* ortasına düşen mum (açılışı t*'dan önce) KULLANILMAZ; t = t* olan mum kullanılır;
    n = 0 mum atlanır, n None / alan yok / −1 (seans_bars bilinmeyeni) = işlem var; max_wait
    sınırı dahil, ötesi None; NaN/inf girdi reddedilir
  • exit_at aynı kural; dönüş (int, float)
  • TP içinden geçmeli (h = tp_px dokunma dolum değil); stop dokunmayla dolar; açılış stopun
    ötesindeyse (boşluk) açılıştan; açılış TP'nin ötesindeyse açılıştan
  • aynı mumda stop + TP → stop (boşluklu açılış TP'nin ötesinde olsa da)
  • zaman aşımı: vadeden sonraki ilk İŞLEMLİ mumun açılışı; o mumun h/l'si sayılmaz; seviye
    denetiminden önce gelir; giriş mumu h/l'si denetime dahil; girişten önceki mumlar yok sayılır
  • veri biterse "open": çıkış / ret None, mfe/mae yine dolu
  • short = long'un toplamsal aynası (2e − p) — rastgele yollarda birebir aynı sonuç
  • mfe ≥ 0 ≥ mae; kapanmışta mae ≤ ret ≤ mfe; TP'de mfe = tp, stopta mae = −sl (çıkış sonrası yok)
  • davranış: martingale yürüyüşte TP payı ≈ barrier_p; zaman aşımı bağlayıcıyken yakın seviye
    fazla temsil edilir (tp < sl → pay > barrier_p, iyimser; tp > sl → temkinli) — belgedeki uyarı
  • davranış: t* ortasındaki mumun açılışıyla girmek (sızıntı) boş yürüyüşte kâr üretir; entry() ile 0
"""
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from app.lab import execsim as ex  # noqa: E402

BAR = 60
T0 = 1_760_000_000 - 1_760_000_000 % BAR
SEED = 20261008


def cd(t, o, h=None, l=None, c=None, n=5):  # noqa: E741
    """Tek mum (HL biçimi)."""
    h = o if h is None else h
    l = o if l is None else l  # noqa: E741
    return {"t": t, "o": o, "h": max(h, o), "l": min(l, o), "c": o if c is None else c,
            "v": 1.0, "n": n, "closed": True}


def flat(k, px=100.0, t0=T0):
    return [cd(t0 + i * BAR, px) for i in range(k)]


def sim_paths(n_paths, k, m, sig, seed, full=False):
    """Martingale fiyat yolları → (o, h, l[, p]) dizileri (n_paths, k). Her mum m alt adım; o = ilk
    işlem, h/l = mum içi uçlar, sonraki mumun o'su yeni bir işlem (kapanışın kopyası değil).
    Bellek için 256'lık parçalarla üretilir."""
    rng = np.random.default_rng(seed)
    o, h, l, ps = [], [], [], []  # noqa: E741
    for a in range(0, n_paths, 256):
        b = min(n_paths, a + 256)
        z = rng.standard_normal((b - a, k * m))
        p = 100.0 * np.exp(np.cumsum(sig * z - sig * sig / 2, axis=1))
        p = np.concatenate([np.full((b - a, 1), 100.0), p[:, :-1]], axis=1).reshape(b - a, k, m)
        o.append(p[:, :, 0]), h.append(p.max(axis=2)), l.append(p.min(axis=2))
        if full:
            ps.append(p)
    out = (np.concatenate(o), np.concatenate(h), np.concatenate(l))
    return out + (np.concatenate(ps),) if full else out


def to_cands(o, h, l, t0=T0):  # noqa: E741
    return [{"t": t0 + i * BAR, "o": a, "h": b, "l": c, "c": a, "v": 1.0, "n": 3, "closed": True}
            for i, (a, b, c) in enumerate(zip(o, h, l))]


# ───────────────────────── giriş / sabit vadeli çıkış ─────────────────────────

def test_entry_skips_candle_open_before_t_star():
    cs = [cd(T0, 100.0, 103, 99), cd(T0 + BAR, 102.5), cd(T0 + 2 * BAR, 101.0)]
    # t* mum 0'ın ortasında: mum 0'ın açılışı t*'dan önce → kullanılmaz
    assert ex.entry(cs, T0 + 30, 600) == (T0 + BAR, 102.5)
    assert ex.entry(cs, T0 + 1, 600) == (T0 + BAR, 102.5)
    # t* tam mum açılışında → o mum (açılışı t*'dan sonraki ilk işlem)
    assert ex.entry(cs, T0, 600) == (T0, 100.0)
    assert ex.entry(cs, T0 + BAR, 600) == (T0 + BAR, 102.5)
    t, px = ex.entry(cs, T0 + 30, 600)
    assert type(t) is int and type(px) is float
    # hepsi t*'dan önce → None; boş liste → None
    assert ex.entry(cs, T0 + 2 * BAR + 1, 10_000) is None
    assert ex.entry([], T0, 600) is None
    print("✅ giriş: t* ortasındaki mum kullanılmıyor; t = t* olan kullanılıyor")


def test_entry_skips_zero_trade_candles():
    cs = [cd(T0, 100.0), cd(T0 + BAR, 100.0, n=0), cd(T0 + 2 * BAR, 100.4, n=0), cd(T0 + 3 * BAR, 100.7)]
    assert ex.entry(cs, T0 + 10, 600) == (T0 + 3 * BAR, 100.7)
    # n None ya da alan yok → işlem var kabul
    cs[1]["n"] = None
    assert ex.entry(cs, T0 + 10, 600) == (T0 + BAR, 100.0)
    del cs[1]["n"]
    assert ex.entry(cs, T0 + 10, 600) == (T0 + BAR, 100.0)
    # seans_bars bilinmeyen işlem sayısını −1 yazar → bilinmiyor = işlem var (sessizce atlanmaz)
    cs[1]["n"] = -1
    assert ex.entry(cs, T0 + 10, 600) == (T0 + BAR, 100.0)
    r = ex.path_exit([cd(T0, 100.0, n=-1), cd(T0 + BAR, 97.0, n=-1)], T0, 100.0, 1, 0.02, 0.02, 3600)
    assert r["reason"] == "sl" and r["bars"] == 2, r
    # yalnız işlemsiz mumlar → None
    assert ex.entry([cd(T0, 1.0, n=0), cd(T0 + BAR, 1.0, n=0)], T0, 600) is None
    print("✅ giriş: n = 0 atlanır, n None / yok = işlem var")


def test_entry_max_wait():
    cs = [cd(T0, 100.0), cd(T0 + 5 * BAR, 101.0)]
    assert ex.entry(cs, T0 + 1, 4 * BAR) is None                     # ilk işlem çok geç
    assert ex.entry(cs, T0 + BAR, 4 * BAR) == (T0 + 5 * BAR, 101.0)  # sınır dahil
    assert ex.entry(cs, T0 + BAR, 4 * BAR - 1) is None
    assert ex.entry(cs, T0, 0) == (T0, 100.0)
    # işlemsiz mumlar beklemeyi tüketir
    cs2 = [cd(T0 + i * BAR, 100.0, n=0) for i in range(5)] + [cd(T0 + 5 * BAR, 100.2)]
    assert ex.entry(cs2, T0, 4 * BAR) is None
    assert ex.entry(cs2, T0, 5 * BAR) == (T0 + 5 * BAR, 100.2)
    print("✅ giriş: max_wait sınırı dahil, ötesi None")


def test_exit_at_same_rule():
    cs = [cd(T0, 100.0, 101, 99), cd(T0 + BAR, 100.5, n=0), cd(T0 + 2 * BAR, 100.9), cd(T0 + 9 * BAR, 98.0)]
    assert ex.exit_at(cs, T0 + 20, 600) == (T0 + 2 * BAR, 100.9)   # ortadaki mum ve n=0 atlanır
    assert ex.exit_at(cs, T0 + 2 * BAR, 0) == (T0 + 2 * BAR, 100.9)
    assert ex.exit_at(cs, T0 + 2 * BAR + 1, 6 * BAR) is None       # sonraki işlem max_wait ötesinde
    assert ex.exit_at(cs, T0 + 2 * BAR + 1, 7 * BAR) == (T0 + 9 * BAR, 98.0)
    print("✅ exit_at: entry ile aynı kural")


# ───────────────────────── yol üzerinde çıkış ─────────────────────────

def test_tp_needs_trade_through():
    e = 100.0
    touch, through = e * (1 + 0.02), e * (1 + 0.02) + 0.01
    cs = [cd(T0, e, 101, 99.5), cd(T0 + BAR, 101, touch, 100.5), cd(T0 + 2 * BAR, 101.5, through, 101)]
    r = ex.path_exit(cs, T0, e, 1, 0.02, 0.02, 3600)
    assert r["reason"] == "tp" and r["exit_ts"] == T0 + 2 * BAR, r          # dokunma dolum değil
    assert math.isclose(r["exit_px"], touch) and math.isclose(r["ret"], 0.02)
    assert r["bars"] == 3
    # short aynası: l = tp_px dokunma, altı dolum
    s_touch = e * (1 - 0.02)
    cs = [cd(T0, e, 100.5, 99), cd(T0 + BAR, 99, 99.5, s_touch), cd(T0 + 2 * BAR, 98.5, 99, s_touch - 0.01)]
    r = ex.path_exit(cs, T0, e, -1, 0.02, 0.02, 3600)
    assert r["reason"] == "tp" and r["exit_ts"] == T0 + 2 * BAR and math.isclose(r["ret"], 0.02), r
    # açılış TP'nin ötesinde (boşluk) → açılıştan (seviyeden iyi)
    cs = [cd(T0, e, 101, 99.5), cd(T0 + BAR, 103.0, 103.5, 102.5)]
    r = ex.path_exit(cs, T0, e, 1, 0.02, 0.02, 3600)
    assert r["reason"] == "tp" and r["exit_px"] == 103.0 and math.isclose(r["ret"], 0.03), r
    # giriş mumunun h'si girişten sonradır → denetime dahil
    r = ex.path_exit([cd(T0, e, 102.5, 99.9)], T0, e, 1, 0.02, 0.02, 3600)
    assert r["reason"] == "tp" and r["exit_ts"] == T0 and r["bars"] == 1, r
    print("✅ TP içinden geçmeli; boşlukta açılıştan; giriş mumu dahil")


def test_stop_touch_and_gap_through():
    e = 100.0
    sl_px = e * (1 - 0.02)
    cs = [cd(T0, e, 100.5, 99), cd(T0 + BAR, 99.0, 99.5, sl_px)]
    r = ex.path_exit(cs, T0, e, 1, 0.05, 0.02, 3600)
    assert r["reason"] == "sl" and r["exit_px"] == sl_px and math.isclose(r["ret"], -0.02), r  # dokunma yeter
    # boşluk: açılış stopun altında → açılıştan dolar (seviyeden kötü)
    cs = [cd(T0, e, 100.5, 99), cd(T0 + BAR, 95.0, 96, 94)]
    r = ex.path_exit(cs, T0, e, 1, 0.05, 0.02, 3600)
    assert r["reason"] == "sl" and r["exit_px"] == 95.0 and math.isclose(r["ret"], -0.05), r
    assert math.isclose(r["mae"], -0.05)                         # 94 çıkıştan sonra → sayılmaz
    # short: açılış stopun üstünde → açılıştan
    cs = [cd(T0, e, 100.5, 99.5), cd(T0 + BAR, 104.0, 104.5, 103)]
    r = ex.path_exit(cs, T0, e, -1, 0.05, 0.02, 3600)
    assert r["reason"] == "sl" and r["exit_px"] == 104.0 and math.isclose(r["ret"], -0.04), r
    # bozuk mum (açılış l'nin altında): h/l açılışı kapsayacak şekilde genişler → boşluk stopu kaçmaz
    cs = [cd(T0, e, 100.5, 99.5), {"t": T0 + BAR, "o": 97.0, "h": 99.5, "l": 98.5, "c": 99.0, "n": 4}]
    r = ex.path_exit(cs, T0, e, 1, 0.05, 0.02, 3600)
    assert r["reason"] == "sl" and r["exit_px"] == 97.0, r
    # girişten önceki mumlar yok sayılır (oradaki çöküş stop değildir)
    cs = [cd(T0 - BAR, 100.0, 100, 50), cd(T0, e, 100.5, 99.5), cd(T0 + BAR, 100.2)]
    r = ex.path_exit(cs, T0, e, 1, 0.05, 0.02, 3600)
    assert r["reason"] == "open" and r["bars"] == 2, r
    print("✅ stop: dokunma yeter, boşlukta açılıştan; girişten önceki mum yok sayılır")


def test_same_candle_stop_and_tp_is_stop():
    e = 100.0
    cs = [cd(T0, e, 100.5, 99.5), cd(T0 + BAR, 100.0, 103.0, 97.0)]
    r = ex.path_exit(cs, T0, e, 1, 0.02, 0.02, 3600)
    assert r["reason"] == "sl" and math.isclose(r["exit_px"], 98.0) and math.isclose(r["ret"], -0.02), r
    r = ex.path_exit(cs, T0, e, -1, 0.02, 0.02, 3600)
    assert r["reason"] == "sl" and math.isclose(r["exit_px"], 102.0) and math.isclose(r["ret"], -0.02), r
    # giriş mumunda ikisi birden → yine stop
    r = ex.path_exit([cd(T0, e, 103.0, 97.0)], T0, e, 1, 0.02, 0.02, 3600)
    assert r["reason"] == "sl" and r["exit_ts"] == T0, r
    # açılış TP'nin ötesinde ama mum stopa da değiyor → temkinli kural: stop
    cs = [cd(T0, e, 100.5, 99.5), cd(T0 + BAR, 102.5, 102.6, 97.5)]
    r = ex.path_exit(cs, T0, e, 1, 0.02, 0.02, 3600)
    assert r["reason"] == "sl" and math.isclose(r["exit_px"], 98.0), r
    print("✅ aynı mumda stop + TP → stop")


def test_timeout_first_traded_open_at_or_after_due():
    e = 100.0
    cs = [cd(T0, e, 100.3, 99.8), cd(T0 + BAR, 100.2, 100.6, 99.9), cd(T0 + 2 * BAR, 100.4, 100.5, 100.1),
          cd(T0 + 3 * BAR, 100.4, n=0), cd(T0 + 4 * BAR, 100.7, 110.0, 90.0)]
    r = ex.path_exit(cs, T0, e, 1, 0.02, 0.02, 3 * BAR)
    # vade T0+3BAR'daki mum işlemsiz → ilk işlemli mum T0+4BAR, açılışından; o mumun h/l'si çıkıştan sonra
    assert r["reason"] == "timeout" and r["exit_ts"] == T0 + 4 * BAR and r["exit_px"] == 100.7, r
    assert math.isclose(r["ret"], 0.007) and math.isclose(r["mfe"], 0.007) and math.isclose(r["mae"], -0.002)
    assert r["bars"] == 4                                         # işlemsiz mum sayılmaz
    # vadeye tam denk gelen işlemli mum → onun açılışı; seviye denetiminden önce gelir
    cs[3]["n"] = 7
    r = ex.path_exit(cs, T0, e, -1, 0.02, 0.02, 3 * BAR)
    assert r["reason"] == "timeout" and r["exit_ts"] == T0 + 3 * BAR and math.isclose(r["ret"], -0.004), r
    cs[3].update(h=150.0, l=50.0)
    r2 = ex.path_exit(cs, T0, e, -1, 0.02, 0.02, 3 * BAR)
    assert r2 == r, (r2, r)
    # vade aradaki boşlukta: sonraki ilk işlemli mum
    r = ex.path_exit(cs, T0, e, 1, 0.02, 0.02, 2 * BAR + 30)
    assert r["reason"] == "timeout" and r["exit_ts"] == T0 + 3 * BAR, r
    # timeout_s None → zaman aşımı yok, veri biter → open
    r = ex.path_exit(cs[:3], T0, e, 1, 0.02, 0.02, None)
    assert r["reason"] == "open", r
    print("✅ zaman aşımı: vadeden sonraki ilk işlemli mumun açılışı, o mumun h/l'si sayılmaz")


def test_open_when_data_ends():
    e = 100.0
    cs = [cd(T0, e, 100.8, 99.4), cd(T0 + BAR, 100.3, 101.1, 99.7), cd(T0 + 2 * BAR, 100.1, n=0)]
    r = ex.path_exit(cs, T0, e, 1, 0.02, 0.02, 3600)
    assert r["reason"] == "open" and r["exit_ts"] is None and r["exit_px"] is None and r["ret"] is None, r
    assert math.isclose(r["mfe"], 0.011) and math.isclose(r["mae"], -0.006) and r["bars"] == 2
    assert set(r) == {"exit_ts", "exit_px", "reason", "ret", "mfe", "mae", "bars"}
    r = ex.path_exit([], T0, e, 1, 0.02, 0.02, 60)
    assert r == {"exit_ts": None, "exit_px": None, "reason": "open", "ret": None, "mfe": 0.0, "mae": 0.0,
                 "bars": 0}
    # tp/sl None → yalnız zaman aşımı
    r = ex.path_exit(cs[:2] + [cd(T0 + 5 * BAR, 130.0)], T0, e, 1, None, None, 4 * BAR)
    assert r["reason"] == "timeout" and math.isclose(r["ret"], 0.30), r
    print("✅ veri biterse open: çıkış yok, mfe/mae dolu")


def test_short_side_is_exact_mirror():
    """Toplamsal ayna p' = 2e − p: long tp_px = e(1+tp) ↔ short e(1−tp); getiri birebir aynı."""
    o, h, l = sim_paths(300, 40, 6, 0.004, SEED)
    rng = np.random.default_rng(SEED + 1)
    reasons = set()
    for i in range(o.shape[0]):
        e = float(o[i, 0])
        tp = (0.01, 0.02, 0.03, None)[int(rng.integers(4))]
        sl = (0.01, 0.02, None)[int(rng.integers(3))]
        tmo = int(rng.integers(5, 80)) * BAR                     # 40 mumdan uzunsa "open" da olur
        cl = to_cands(o[i].tolist(), h[i].tolist(), l[i].tolist())
        cs = to_cands((2 * e - o[i]).tolist(), (2 * e - l[i]).tolist(), (2 * e - h[i]).tolist())
        a = ex.path_exit(cl, T0, e, 1, tp, sl, tmo)
        b = ex.path_exit(cs, T0, e, -1, tp, sl, tmo)
        assert (a["reason"], a["exit_ts"], a["bars"]) == (b["reason"], b["exit_ts"], b["bars"]), (i, a, b)
        for k in ("ret", "mfe", "mae"):
            if a[k] is None:
                assert b[k] is None
            else:
                assert abs(a[k] - b[k]) < 1e-12, (i, k, a, b)
        reasons.add(a["reason"])
    assert reasons == {"tp", "sl", "timeout", "open"}, reasons
    print("✅ short = long'un aynası (300 rastgele yol, dört çıkış türü)")


def test_mfe_mae_signs_and_bounds():
    e = 100.0
    # TP çıkışı: mfe = tp (çıkış sonrası 105 sayılmaz), mae mum içi diptir
    cs = [cd(T0, e, 100.5, 99.0), cd(T0 + BAR, 100.4, 105.0, 99.5)]
    r = ex.path_exit(cs, T0, e, 1, 0.02, 0.03, 3600)
    assert r["reason"] == "tp" and math.isclose(r["mfe"], 0.02) and math.isclose(r["mae"], -0.01), r
    # stop çıkışı: mae = −sl (çıkış sonrası 90 sayılmaz); stop mumundaki tepe stoptan sonra varsayılır
    cs = [cd(T0, e, 100.5, 99.5), cd(T0 + BAR, 100.0, 101.5, 90.0)]
    r = ex.path_exit(cs, T0, e, 1, 0.02, 0.03, 3600)
    assert r["reason"] == "sl" and math.isclose(r["mae"], -0.03) and math.isclose(r["mfe"], 0.005), r
    # TP boşlukla açılıştan dolduysa o mumun aleyhte ucu çıkıştan sonradır → sayılmaz
    cs = [cd(T0, e, 100.5, 99.5), cd(T0 + BAR, 103.0, 104.0, 97.5)]
    r = ex.path_exit(cs, T0, e, 1, 0.02, 0.05, 3600)
    assert r["reason"] == "tp" and r["exit_px"] == 103.0 and math.isclose(r["mae"], -0.005), r
    assert math.isclose(r["mfe"], 0.03), r
    # short: işaretler yön düzeltmeli
    cs = [cd(T0, e, 101.0, 99.0), cd(T0 + BAR, 100.0)]
    r = ex.path_exit(cs, T0, e, -1, 0.05, 0.05, 3600)
    assert math.isclose(r["mfe"], 0.01) and math.isclose(r["mae"], -0.01), r
    # rastgele yollarda: mfe ≥ 0 ≥ mae, kapanmışta mae ≤ ret ≤ mfe
    o, h, l = sim_paths(400, 30, 6, 0.004, SEED + 2)
    for i in range(o.shape[0]):
        cl = to_cands(o[i].tolist(), h[i].tolist(), l[i].tolist())
        for side in (1, -1):
            r = ex.path_exit(cl, T0, float(o[i, 0]), side, 0.02, 0.015, 20 * BAR)
            assert r["mfe"] >= 0.0 >= r["mae"], r
            if r["ret"] is not None:
                assert r["mae"] - 1e-12 <= r["ret"] <= r["mfe"] + 1e-12, r
    print("✅ mfe ≥ 0 ≥ mae; mae ≤ ret ≤ mfe; çıkış sonrası fiyat sayılmaz")


def test_ret_and_input_checks():
    assert math.isclose(ex.ret(1, 100.0, 101.0), 0.01)
    assert math.isclose(ex.ret(-1, 100.0, 101.0), -0.01)
    assert math.isclose(ex.ret(-1, 100.0, 97.0), 0.03)
    for bad in ({"side": 0}, {"tp": 0.0}, {"sl": -0.01}, {"entry_px": 0.0}, {"entry_px": float("nan")},
                {"tp": float("nan")}, {"sl": float("inf")}, {"entry_px": float("inf")}):
        kw = {"cands": [], "entry_ts": T0, "entry_px": 100.0, "side": 1, "tp": 0.02, "sl": 0.02,
              "timeout_s": 60} | bad
        try:
            ex.path_exit(**kw)
        except ValueError:
            continue
        raise AssertionError(f"geçersiz girdi kabul edildi: {bad}")
    print("✅ ret = side·(p1/p0 − 1); geçersiz girdi reddedilir")


# ───────────────────────── bariyer olasılığı ─────────────────────────

def test_barrier_p_formula():
    assert math.isclose(ex.barrier_p(0.01, 0.02), 2 / 3)
    assert ex.barrier_p(0.02, 0.02) == 0.5
    assert math.isclose(ex.barrier_p(0.03, 0.01), 0.25)
    # martingale denetimi: p·(1+tp) + (1−p)·(1−sl) = 1
    for tp, sl in ((0.01, 0.02), (0.05, 0.01), (0.007, 0.007)):
        p = ex.barrier_p(tp, sl)
        assert math.isclose(p * (1 + tp) + (1 - p) * (1 - sl), 1.0)
    for bad in ((0, 0.01), (0.01, 0), (-0.01, 0.02), (float("nan"), 0.01), (0.01, float("inf"))):
        try:
            ex.barrier_p(*bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    print("✅ barrier_p = sl/(tp+sl)")


def _tp_share(tp, sl, timeout_s, seed, n_paths=1500, k=200, m=16, sig=0.0006):
    o, h, l = sim_paths(n_paths, k, m, sig, seed)
    cnt = {"tp": 0, "sl": 0, "timeout": 0, "open": 0}
    for i in range(n_paths):
        cnt[ex.path_exit(to_cands(o[i].tolist(), h[i].tolist(), l[i].tolist()), T0, float(o[i, 0]), 1,
                         tp, sl, timeout_s)["reason"]] += 1
    return cnt


def test_barrier_p_matches_martingale_simulation():
    """Bağlayıcı zaman aşımı yokken martingale yürüyüşte TP payı ≈ sl/(tp+sl)."""
    for tp, sl, seed in ((0.01, 0.02, SEED + 3), (0.02, 0.01, SEED + 4)):
        cnt = _tp_share(tp, sl, None, seed)
        n = cnt["tp"] + cnt["sl"]
        assert cnt["open"] <= 0.02 * (n + cnt["open"]), cnt
        p0 = ex.barrier_p(tp, sl)
        z = (cnt["tp"] / n - p0) / math.sqrt(p0 * (1 - p0) / n)
        assert abs(z) < 3.5, (tp, sl, cnt, z)
    print("✅ martingale simülasyonu: TP payı ≈ barrier_p")


def test_barrier_p_timeout_caveat():
    """Zaman aşımı bağlayıcıyken sonuçlananlarda YAKIN seviye fazla: tp < sl → pay > p0 (p0 iyimser,
    kapı için yanlış pozitif riski); tp > sl → pay < p0 (temkinli). Belgedeki uyarıyı pinler."""
    near = _tp_share(0.01, 0.03, 30 * BAR, SEED + 5, n_paths=800, k=40)
    far = _tp_share(0.03, 0.01, 30 * BAR, SEED + 6, n_paths=800, k=40)
    assert near["timeout"] > 0 and far["timeout"] > 0, (near, far)
    sh_near = near["tp"] / (near["tp"] + near["sl"])
    sh_far = far["tp"] / (far["tp"] + far["sl"])
    assert sh_near > ex.barrier_p(0.01, 0.03) + 0.10, (near, sh_near)
    assert sh_far < ex.barrier_p(0.03, 0.01) - 0.10, (far, sh_far)
    print(f"✅ zaman aşımı uyarısı: tp<sl pay {sh_near:.2f} > 0.75; tp>sl pay {sh_far:.2f} < 0.25")


# ───────────────────────── ileriye bakış ─────────────────────────

def test_no_lookahead_null_mean():
    """Boş yürüyüşte, t*'a kadar yükselmiş (t*'da BİLİNEN) olayları seç. t*'ı içeren mumun
    açılışıyla girmek bu yükselişi getiriye katar (sızıntı → sahte kâr); entry() ile ortalama ≈ 0."""
    m, n_paths = 6, 4000
    sub = BAR // m
    _, _, _, p = sim_paths(n_paths, 4, m, 0.001, SEED + 7, full=True)
    rng = np.random.default_rng(SEED + 8)
    ours, leaky = [], []
    for i in range(n_paths):
        j = int(rng.integers(1, m))                       # t* mum 0'ın içinde (açılışından sonra)
        if p[i, 0, j] <= p[i, 0, 0]:
            continue                                      # sinyal: t*'a dek yükseldi
        t_star = T0 + j * sub
        cs = to_cands(p[i, :, 0].tolist(), p[i].max(axis=1).tolist(), p[i].min(axis=1).tolist())
        et, epx = ex.entry(cs, t_star, 2 * BAR)
        xt, xpx = ex.exit_at(cs, t_star + 2 * BAR, 2 * BAR)
        assert et == T0 + BAR and xt == T0 + 3 * BAR
        ours.append(ex.ret(1, epx, xpx))
        leaky.append(ex.ret(1, cs[0]["o"], xpx))          # select_candles tarzı: mum 0 dahil
    ours, leaky = np.array(ours), np.array(leaky)
    t_ours = ours.mean() / (ours.std(ddof=1) / math.sqrt(len(ours)))
    t_leak = leaky.mean() / (leaky.std(ddof=1) / math.sqrt(len(leaky)))
    assert len(ours) > 1500
    assert abs(t_ours) < 3.0, t_ours
    assert t_leak > 8.0, t_leak
    print(f"✅ ileriye bakış yok: entry() t = {t_ours:+.2f}, sızıntılı giriş t = {t_leak:+.1f}")
