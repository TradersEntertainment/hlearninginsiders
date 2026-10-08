"""🧪 Yürütme benzetimi — giriş/çıkış ileriye bakışsız (saf: DB yok, ağ yok, asyncio yok).

Mum: dict(t=açılış ts, o, h, l, c, v, n, closed); liste t'ye göre artan sıralı. HL'de mumun `o`'su
o mumun İLK işlemidir: t ≥ t* olan ilk işlemli mumun açılışı "t*'dan sonraki ilk işlenebilir fiyat",
h/l'si ise girişten SONRA. t*'ın ortasına düşen mum (t < t*) KULLANILMAZ — sim.select_candles'ın
giriş mumunu dahil eden sızıntısı burada yok. n None ya da < 0 ise (seans_bars bilinmeyeni −1
yazar) işlem var sayılır; yalnız n = 0 işlemsiz mumdur (o/h/l önceki kapanışın kopyası) — ne
giriş/çıkış ne seviye denetimi yapılır. Bozuk mumda h/l açılışı kapsayacak şekilde genişletilir.

Yol kuralları (path_exit), temkinli yönde:
  • zaman aşımı: t ≥ giriş + timeout olan ilk işlemli mumun AÇILIŞINDA çıkılır (o mumun h/l'si
    çıkıştan sonradır, bakılmaz); aynı mumda seviye denetiminden ÖNCE gelir
  • stop: dokunma yeter (long l ≤ sl_px); açılış zaten ötesindeyse (boşluk) açılıştan dolar
  • TP: seviyenin İÇİNDEN geçilmeli (long h > tp_px kesin) — dokunma dolum değildir; açılış zaten
    ötesindeyse açılıştan
  • aynı mumda ikisi de mümkünse STOP (mum içi sıra bilinmez)
  • veri biter, zaman aşımı gelmezse "open": çıkış yok, ret None
mfe/mae: yön düzeltmeli en iyi/en kötü ara getiri, 0'dan başlar (mfe ≥ 0 ≥ mae). Çıkış mumunda
çıkıştan sonraki fiyatlar sayılmaz: timeout → yalnız açılış; sl → açılış + çıkış fiyatı (lehte uç
stoptan sonra varsayılır, temkinli); tp → açılış + çıkış fiyatı + aleyhte uç (boşlukta açılıştan
dolduysa aleyhte uç çıkıştan sonradır, sayılmaz). Böylece kapanmış pozisyonda mae ≤ ret ≤ mfe.
"""
from __future__ import annotations

import bisect
import math


def _traded(c: dict) -> bool:
    """İşlem içeren mum: yalnız n = 0 işlemsizdir. n None / alan yok / < 0 (seans_bars'ın
    bilinmeyen işaretçisi −1) → bilinmiyor → işlem var say (sözleşme: bilinmeyen = işlemli)."""
    n = c.get("n")
    return n is None or n != 0


def _pos(x) -> bool:
    """Sonlu ve > 0 (NaN / inf reddedilir)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v > 0


def _first_traded(cands: list[dict], t0: int, t1: int) -> tuple[int, float] | None:
    """t0 ≤ t ≤ t1 aralığındaki ilk işlemli mumun (t, o)'su."""
    i = bisect.bisect_left(cands, t0, key=lambda c: int(c["t"]))
    for j in range(i, len(cands)):                                   # kopyasız (uzun listeler)
        c = cands[j]
        t = int(c["t"])
        if t > t1:
            break
        if t >= t0 and _traded(c):
            return t, float(c["o"])
    return None


def entry(cands: list[dict], t_star: int, max_wait: int) -> tuple[int, float] | None:
    """Karar anı t*'dan sonraki ilk işlenebilir fiyat: t* ≤ t ≤ t* + max_wait olan ilk işlemli
    mumun (t, o)'su; yoksa None. t*'dan önce açılan mum (t* onun ortasında) asla kullanılmaz."""
    return _first_traded(cands, int(t_star), int(t_star) + int(max_wait))


def exit_at(cands: list[dict], t_due: int, max_wait: int) -> tuple[int, float] | None:
    """Sabit vadeli çıkış: entry ile aynı kural, t_due için."""
    return _first_traded(cands, int(t_due), int(t_due) + int(max_wait))


def ret(side: int, p0: float, p1: float) -> float:
    """Yön düzeltmeli aritmetik getiri: side·(p1/p0 − 1)."""
    return side * (p1 / p0 - 1.0)


def barrier_p(tp: float, sl: float) -> float:
    """Martingale altında TP'nin stoptan önce gelme olasılığı ≈ sl/(tp+sl).

    Fiyat martingale ve yol sürekliyse isteğe bağlı durdurma: E[P_τ] = P_0 →
    p·(1+tp) + (1−p)·(1−sl) = 1 → p = sl/(tp+sl) (aritmetik seviyeler; kumarbazın iflası).
    Yaklaşımdır: mum içi ayrık gözlem, boşluklar, TP'de içinden geçme şartı (TP'yi biraz
    zorlaştırır → temkinli). DİKKAT — zaman aşımı bağlayıcıysa "sonuçlananlar" (tp+sl) arasında
    YAKIN seviye fazla temsil edilir: tp < sl iken P(tp | zaman aşımından önce sonuçlandı) bu
    değerden BÜYÜKTÜR (p0 olarak kullanmak iyimser → yanlış pozitif); tp ≥ sl iken temkinli
    (tp = sl'de simetriden tam 0.5)."""
    if not (_pos(tp) and _pos(sl)):
        raise ValueError("tp ve sl sonlu, pozitif olmalı")
    return sl / (tp + sl)


def path_exit(cands: list[dict], entry_ts: int, entry_px: float, side: int,
              tp: float | None, sl: float | None, timeout_s: int | None) -> dict:
    """Girişten (t ≥ entry_ts) itibaren mumları sırayla gezip ilk çıkışı bulur (kurallar modül
    belgesinde). Dönüş {"exit_ts", "exit_px", "reason", "ret", "mfe", "mae", "bars"};
    reason ∈ {"tp", "sl", "timeout", "open"}; bars = bakılan işlemli mum sayısı (çıkış mumu
    dahil). timeout_s None → zaman aşımı yok."""
    if side not in (1, -1):
        raise ValueError("side +1 ya da -1 olmalı")
    e = float(entry_px)
    if not _pos(e):
        raise ValueError("entry_px sonlu, pozitif olmalı")
    for name, x in (("tp", tp), ("sl", sl)):
        if x is not None and not _pos(x):
            raise ValueError(f"{name} sonlu pozitif kesir ya da None olmalı")
    tp_px = None if tp is None else e * (1 + side * tp)
    sl_px = None if sl is None else e * (1 - side * sl)
    due = None if timeout_s is None else int(entry_ts) + int(timeout_s)
    mfe = mae = 0.0
    bars = 0

    def mark(*pxs: float) -> None:
        nonlocal mfe, mae
        for p in pxs:
            r = ret(side, e, p)
            mfe, mae = max(mfe, r), min(mae, r)

    def done(t: int, px: float, reason: str) -> dict:
        return {"exit_ts": t, "exit_px": px, "reason": reason, "ret": ret(side, e, px),
                "mfe": mfe, "mae": mae, "bars": bars}

    i = bisect.bisect_left(cands, int(entry_ts), key=lambda c: int(c["t"]))
    for j in range(i, len(cands)):
        c = cands[j]
        if not _traded(c):
            continue
        t = int(c["t"])
        o = float(c["o"])
        h, lo = max(o, float(c["h"])), min(o, float(c["l"]))         # bozuk mum: açılışı kapsa
        bars += 1
        if due is not None and t >= due:
            mark(o)
            return done(t, o, "timeout")
        # aleyhte / lehte uç (yön düzeltmeli)
        adv, fav = (lo, h) if side > 0 else (h, lo)
        hit_sl = sl_px is not None and side * (adv - sl_px) <= 0       # dokunma yeter
        hit_tp = tp_px is not None and side * (fav - tp_px) > 0        # içinden geçmeli
        if hit_sl:
            px = o if side * (o - sl_px) <= 0 else sl_px                # boşluk → açılış
            mark(o, px)
            return done(t, px, "sl")
        if hit_tp:
            if side * (o - tp_px) > 0:                                  # boşluk → açılış; sonrası yok
                mark(o)
                return done(t, o, "tp")
            mark(o, tp_px, adv)
            return done(t, tp_px, "tp")
        mark(h, lo)
    return {"exit_ts": None, "exit_px": None, "reason": "open", "ret": None,
            "mfe": mfe, "mae": mae, "bars": bars}
