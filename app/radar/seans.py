"""🕰 ABD seans karnesi — Asya → Londra → New York (ICT "Power of 3" / AMD iddiasının ölçümü).

Kullanıcı (06.10): "24 saat 3'e ayrılır — accumulation, pump, dump… Londra session'da şu,
Asya session'da şuysa ABD'de şu olur gibi çıkarımlar ve eskiye bakıp örüntü kurmalar."
Kararlar: yalnız ABD tarafı — sayfa + komut (zamanlanmış mesaj YOK); sekmeler XYZ100 ve SP500,
ABD hisse perp'leri komutla / ?sym= ile; seanslar piyasa saatleri, yaz/kış uyumlu.

Seanslar (ET işlem günü d) — 24 saat tam üçe bölünür, gün her zaman 48 yarım saat:
  • Asya    = d'den önceki TAKVİM günü 16:00 ET → d 08:00 Londra (Pazartesi: Pazar 16:00 ET)
  • Londra  = 08:00 Londra → 09:30 ET
  • New York = 09:30 → 16:00 ET
  TSİ: yaz 23:00→10:00 / 10:00→16:30 / 16:30→23:00 · kış 00:00→11:00 / 11:00→17:30 /
  17:30→00:00 · ABD yaz saatindeyken İngiltere kışta olduğu haftalarda (Mart ve Ekim sonu)
  Londra 11, Asya 24 mum (ABD yaz saati İngiltere'ninkini içerdiği için hep bu yönde).

Ölçüm (06.10; 30 dk mum, 71 işlem günü 25.06–05.10; kripto 1h 207 gün): yön kuralları
("Londra Asya dibini süpürdüyse NY yukarı" vb.) taban orandan ayırt edilemiyor; "yükselen günün
dibi Asya'da" saat konumunu koruyan boş modelle aynı çıkıyor (rastgele yürüyüş + oynaklık
profili, AMD yapısı değil); tutarlı olan oynaklık: geniş Asya → geniş NY (XYZ100 ρ +0.42, SP500
+0.33 — düzeltme sonrası da); getiri gece birikmiş (XYZ100 Asya +%3.6, NY −%0.3) ama |t| < 2 —
betimleme, kanıt değil.

Kurallar (tahmin yok): her satırda n, taban, z; alt küme taban oranı oluşturan günlerden
çekildiği için sonlu-örneklem düzeltmeli z; 10 sabit kural (eşik ayarı BİLEREK yok —
ayarlanabilir eşik p-hacking olurdu); Bonferroni eşiği ve şansla beklenen |z|≥2 sayısı;
dip/tepe ham oranı asla boş model olmadan gösterilmez.

Veri: HL candleSnapshot 30m (09:30 sınırı saatlik mumla bölünemez). HL yalnız son 5000 mumu
verir (~104 gün) → ham mumlar `seans_bars`'a yazılır, arşiv her gün büyür. Seans özeti
SAKLANMAZ, her okumada hamdan hesaplanır (tanım hatası bulunursa eski günler de düzelsin).
Getiriler kapanıştan zincirlenir: HL mumunun açılışı o yarım saatin ilk işlemidir, önceki
kapanış değil — Asya, kendinden önceki mumun kapanışından; Londra, Asya kapanışından; NY,
Londra kapanışından ölçülür; üçünün toplamı günün getirisi, hafta sonu/tatil ayrı "Boşluk".
`n` = mumdaki işlem sayısı: HL işlemsiz yarım saatte de son fiyatla düz mum üretir.
"""
from __future__ import annotations

import asyncio
import logging
import math
import statistics
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from .. import assets
from ..db import db, kv_get, kv_set, now
from .hourstats import ET, MKT_CLOSE, MKT_OPEN, TR

log = logging.getLogger("radar.seans")

LON = ZoneInfo("Europe/London")
L_START = dtime(8, 0)            # Londra açılışı (yerel)
BAR = 1800                       # 30 dk: 09:30 ET'yi bölen en kaba HL aralığı
HL_MAX_CANDLES = 5000            # HL aralık başına yalnız son 5000 mumu verir (30 dk ≈ 104 gün)
CLOSED_MARGIN = 60               # mum kapandıktan sonra geç gelen işlem payı
LIVE_TTL = 120                   # canlı kuyruk önbelleği = sayfa yenileme aralığı
LIVE_SPAN = 26 * 3600            # bugünün Asya başlangıcı (≤24 sa önce) + önceki mum
MIN_N = 20                       # /orintu ile aynı: daha az örnekte karar verilmez
Z_MIN = 2.0                      # ~%95 iki yanlı (/orintu ile aynı)
FAMILY_ALPHA = 0.05              # Bonferroni aile düzeyi
P_Z2 = 0.0455                    # P(|Z| ≥ 2): şansla beklenen "anlamlı" payı
NULL_REPS = 200                  # gün başına karıştırma (ortalama hata ≪ %1)
NULL_SEED = 20261006             # aynı veri → aynı sayı (yenilemede titreme yok)
THIN_WARN, THIN_BLOCK = 0.10, 0.33   # işlemsiz mum payı (28 xyz perp ölçümü: likit %0–3, seyrek %18–34)
EXTRA_MAX, EXTRA_DAYS = 12, 30   # sorulan hisseler: en çok 12, 30 gün arşivlenir
REFRESH_MIN = 600
STATS_KV = "seans_stats"
EXTRA_KV = "seans_extra"
SESS = ("asya", "londra", "ny")
SESS_TR = {"asya": "Asya", "londra": "Londra", "ny": "New York"}
GUN_TR = ("Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz")

# ABD'de işlem gören endeks/ETF perp'leri (NON_EQUITY içinde olup nakit seansı ABD'de olanlar)
US_NONEQ_OK = frozenset({"XYZ100", "SP500", "SPX", "NDX", "QQQ", "SPY", "IWM", "SMH", "SOXX", "SOXL",
                         "XLE", "XLF", "XLK", "ARKK", "GDX", "TLT", "LIT", "URNM", "EWY", "EWJ", "EWT",
                         "EWZ", "EWW", "KORU"})
# Dayanağı Asya borsasında işlem gören xyz perp'leri (06.10 xyz evreni): onların asıl seansı "Asya".
# GIGADEV Şanghay'da; takvimsiz (assets.NO_CALENDAR) diğerleri uyarıyla kabul edilir.
NON_US_LISTED = frozenset({"HYUNDAI", "KIOXIA", "SKHX", "SKHY", "SMSN", "SOFTBANK", "GIGADEV"})
# NYSE tatilleri ve yarım günleri (2026–2027). Tatilde ABD seansı yoktur; yarım günde NY 13:00
# ET'de kapanır — ikisi de karneye girmez. HOLIDAYS_UNTIL'den sonrası için sayfa uyarır.
US_HOLIDAYS: dict[date, str] = {
    date(2026, 1, 1): "Yılbaşı", date(2026, 1, 19): "Martin Luther King Günü",
    date(2026, 2, 16): "Başkanlar Günü", date(2026, 4, 3): "Kutsal Cuma",
    date(2026, 5, 25): "Anma Günü", date(2026, 6, 19): "Juneteenth",
    date(2026, 7, 3): "Bağımsızlık Günü (gözlenen)", date(2026, 9, 7): "İşçi Bayramı",
    date(2026, 11, 26): "Şükran Günü", date(2026, 12, 25): "Noel",
    date(2027, 1, 1): "Yılbaşı", date(2027, 1, 18): "Martin Luther King Günü",
    date(2027, 2, 15): "Başkanlar Günü", date(2027, 3, 26): "Kutsal Cuma",
    date(2027, 5, 31): "Anma Günü", date(2027, 6, 18): "Juneteenth (gözlenen)",
    date(2027, 7, 5): "Bağımsızlık Günü (gözlenen)", date(2027, 9, 6): "İşçi Bayramı",
    date(2027, 11, 25): "Şükran Günü", date(2027, 12, 24): "Noel (gözlenen)",
}
US_EARLY_CLOSE: dict[date, str] = {
    date(2026, 11, 27): "Şükran ertesi (13:00 ET kapanış)",
    date(2026, 12, 24): "Noel arifesi (13:00 ET kapanış)",
    date(2027, 11, 26): "Şükran ertesi (13:00 ET kapanış)",
}
HOLIDAYS_UNTIL = date(2027, 12, 31)


# ---------------- takvim ----------------

def is_trading_day(d: date) -> bool:
    """Pzt–Cum ve NYSE tatili değil (yarım gün işlem günüdür; karneye girmez)."""
    return d.weekday() < 5 and d not in US_HOLIDAYS


def next_trading_day(d: date) -> date:
    d = d + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d: date) -> date:
    d = d - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def bounds(d: date) -> dict:
    """ET işlem günü d'nin seans sınırları (UTC sn) ve mum sayıları — sınırlardan, sabit değil."""
    a0 = datetime.combine(d - timedelta(days=1), MKT_CLOSE, ET)
    l0 = datetime.combine(d, L_START, LON)
    n0 = datetime.combine(d, MKT_OPEN, ET)
    n1 = datetime.combine(d, MKT_CLOSE, ET)
    a0, l0, n0, n1 = (int(x.timestamp()) for x in (a0, l0, n0, n1))
    return {"a0": a0, "l0": l0, "n0": n0, "n1": n1,
            "nb": {"asya": (l0 - a0) // BAR, "londra": (n0 - l0) // BAR, "ny": (n1 - n0) // BAR}}


def _windows(b: dict) -> list[tuple[str, int, int]]:
    return [("asya", b["a0"], b["l0"]), ("londra", b["l0"], b["n0"]), ("ny", b["n0"], b["n1"])]


def trading_day_at(ts: int) -> tuple[date, str]:
    """Şu an hangi ET işlem gününün hangi fazındayız: bekleniyor | asya | londra | ny.
    16:00 ET'den sonra ertesi günün Asya'sı başlar; hafta sonu/tatil → sonraki işlem günü."""
    et = datetime.fromtimestamp(int(ts), ET)
    d = et.date() + (timedelta(days=1) if et.time() >= MKT_CLOSE else timedelta(0))
    if not is_trading_day(d):
        d = next_trading_day(d)
    b = bounds(d)
    if ts < b["a0"]:
        return d, "bekleniyor"
    for key, t0, t1 in _windows(b):
        if t0 <= ts < t1:
            return d, key
    return d, "bekleniyor"


def _hm(ts: int, tz) -> str:
    return datetime.fromtimestamp(int(ts), tz).strftime("%H:%M")


def day_label(d: date) -> str:
    return f"{GUN_TR[d.weekday()]} {d:%d.%m}"


def session_times(d: date) -> list[dict]:
    """Sayfadaki saat tablosu: seans, TSİ ve ET aralığı, süre, mum sayısı."""
    b = bounds(d)
    out = []
    for key, t0, t1 in _windows(b):
        out.append({"key": key, "label": SESS_TR[key], "t0": t0, "t1": t1,
                    "tsi0": _hm(t0, TR), "tsi1": _hm(t1, TR), "et0": _hm(t0, ET), "et1": _hm(t1, ET),
                    "hours": (t1 - t0) / 3600, "nb": (t1 - t0) // BAR,
                    "prev_day": key == "asya"})
    return out


def _times_key(d: date) -> tuple:
    return tuple((r["tsi0"], r["tsi1"]) for r in session_times(d))


def times_text(d: date) -> str:
    return " · ".join(f"{r['label']} {r['tsi0']}–{r['tsi1']}" for r in session_times(d))


def upcoming_change(d: date, horizon: int = 21) -> dict | None:
    """Önümüzdeki `horizon` gün içinde TSİ saatleri değişiyor mu (yaz/kış geçişi)."""
    cur = _times_key(d)
    x = d
    for _ in range(horizon):
        x = next_trading_day(x)
        if _times_key(x) != cur:
            return {"day": x, "label": day_label(x), "text": times_text(x)}
    return None


# ---------------- mumlar ve günler ----------------

def parse_bars(raw, now_ts: int) -> list[dict]:
    """candleSnapshot → 30 dk mumlar (TAVAN YOK — pricechart.parse_candles 1200'de keser).
    Bozuk satır atılır; aynı zaman damgasında sonuncu kalır; `n` yoksa −1 (bilinmiyor)."""
    out: dict[int, dict] = {}
    for x in raw or []:
        try:
            t = int(x["t"]) // 1000
            o, h, lo, c = (float(x[k]) for k in ("o", "h", "l", "c"))
            v = float(x.get("v") or 0)
            n = int(x["n"]) if x.get("n") is not None else -1
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
        if t % BAR or min(o, h, lo, c) <= 0 or h < lo:
            continue
        out[t] = {"t": t, "o": o, "h": h, "l": lo, "c": c, "v": v, "n": n,
                  "closed": t + BAR + CLOSED_MARGIN <= int(now_ts)}
    return [out[t] for t in sorted(out)]


def sess_stats(by: dict, t0: int, t1: int, ref: float, upto: int | None = None) -> dict | None:
    """[t0, t1) seansı (ya da `upto`'ya kadarki kısmı): ref = önceki kapanış (zincir).
    Eksik mum varsa None — yarım veriyle ölçüm uydurulmaz."""
    end = t1 if upto is None else min(t1, upto)
    rows = []
    for t in range(t0, end, BAR):
        b = by.get(t)
        if b is None:
            return None
        rows.append(b)
    if not rows or not ref or ref <= 0:
        return None
    hi, lo, c = max(b["h"] for b in rows), min(b["l"] for b in rows), rows[-1]["c"]
    hi_t = next(b["t"] for b in rows if b["h"] == hi)
    lo_t = next(b["t"] for b in rows if b["l"] == lo)
    return {"o": ref, "h": hi, "l": lo, "c": c, "r": math.log(c / ref), "rng": (hi - lo) / ref * 100,
            "nb": len(rows), "zero": sum(1 for b in rows if b["n"] == 0), "last_t": rows[-1]["t"],
            "hi_t": hi_t, "lo_t": lo_t}


def split_days(bars: list[dict], now_ts: int) -> tuple[list[dict], dict]:
    """Kapanmış mumlar → tam işlem günleri. Tam gün: 48 mum + Asya'dan önceki mum. Veri
    aralığının içinde delik varsa o gün düşer ve sayılır; uçtaki eksik günler sessizce atlanır;
    tatil/yarım gün karneye girmez. Boşluk = ln(p0 / önceki işlem gününün 16:00 ET fiyatı)."""
    by = {b["t"]: b for b in bars if b.get("closed")}
    drops = {"eksik": 0, "tatil": 0, "yarim": 0}
    if not by:
        return [], drops
    t_min, t_max = min(by), max(by)
    d = datetime.fromtimestamp(t_min, ET).date()
    end = datetime.fromtimestamp(t_max, ET).date() + timedelta(days=1)
    days: list[dict] = []
    while d <= end:
        if d.weekday() >= 5:
            d += timedelta(days=1)
            continue
        b = bounds(d)
        inside = b["a0"] - BAR >= t_min and b["n1"] - BAR <= t_max
        if d in US_HOLIDAYS:
            drops["tatil"] += 1 if inside else 0
            d += timedelta(days=1)
            continue
        if not inside:
            d += timedelta(days=1)
            continue
        pre = by.get(b["a0"] - BAR)
        need = range(b["a0"], b["n1"], BAR)
        if pre is None or any(t not in by for t in need):
            drops["eksik"] += 1
            d += timedelta(days=1)
            continue
        if d in US_EARLY_CLOSE:
            drops["yarim"] += 1
            d += timedelta(days=1)
            continue
        p0 = pre["c"]
        a = sess_stats(by, b["a0"], b["l0"], p0)
        lo = sess_stats(by, b["l0"], b["n0"], a["c"])
        ny = sess_stats(by, b["n0"], b["n1"], lo["c"])
        # Boşluk yalnız arada işlem günü olmayan takvim günü varsa (Pazartesi, tatil ertesi):
        # ardışık günlerde Asya'dan önceki mum dünkü NY'nin son mumudur, boşluk tanımsızdır.
        pd = prev_trading_day(d)
        prev_close = by.get(bounds(pd)["n1"] - BAR) if (d - pd).days > 1 else None
        gap = math.log(p0 / prev_close["c"]) if prev_close else None
        days.append({"day": d.isoformat(), "d": d, "p0": p0, "asya": a, "londra": lo, "ny": ny,
                     "closes": [by[t]["c"] for t in need], "nb": b["nb"], "gap": gap,
                     "mismatch": b["nb"]["asya"] == 24})
        d += timedelta(days=1)
    return days, drops


def thin_level(bars: list[dict]) -> dict:
    """İşlemsiz mum payı (n == 0; n bilinmiyorsa sayılmaz) → ok | warn | block."""
    known = [b for b in bars if b.get("n", -1) >= 0]
    if not known:
        return {"share": None, "level": "ok", "n_bars": 0}
    share = sum(1 for b in known if b["n"] == 0) / len(known)
    level = "block" if share >= THIN_BLOCK else "warn" if share >= THIN_WARN else "ok"
    return {"share": share, "level": level, "n_bars": len(known)}


# ---------------- istatistik ----------------

def z_bonf(k: int, alpha: float = FAMILY_ALPHA) -> float:
    """k karşılaştırma için Bonferroni |z| eşiği (iki yanlı)."""
    return statistics.NormalDist().inv_cdf(1 - alpha / (2 * max(1, k)))


def verdict(z: float | None, n: int, zb: float | None = None,
            zero_txt: str = "taban orandan ayırt edilemiyor") -> dict:
    """/orintu (analog.verdict) eşik ve sözleriyle: yetersiz | zayıf | kayda değer."""
    if z is None or n < MIN_N:
        return {"label": "yetersiz", "note": f"yetersiz (n={n} < {MIN_N})", "survives": False}
    if abs(z) >= Z_MIN:
        surv = zb is not None and abs(z) >= zb
        return {"label": "kayda değer",
                "note": "kayda değer — düzeltme sonrası da" if surv else
                        "kayda değer (tek test) — düzeltme sonrası değil",
                "survives": surv}
    return {"label": "zayıf", "note": zero_txt, "survives": False}


def z_fpc(k: int, n: int, k_all: int, n_all: int) -> float | None:
    """Alt kümenin yükselme oranı taban orandan farklı mı — alt küme aynı günlerden
    çekildiği için sonlu-örneklem düzeltmeli (permütasyon testinin normal yaklaşımı)."""
    if n <= 0 or n >= n_all or n_all < 2:
        return None
    p, p0 = k / n, k_all / n_all
    if p0 <= 0 or p0 >= 1:
        return None
    se = math.sqrt(p0 * (1 - p0) / n * (n_all - n) / (n_all - 1))
    return (p - p0) / se if se > 0 else None


def _up(x: float) -> bool:
    return x > 0


RULES = (
    ("sweep_hi", "Londra yalnız Asya tepesini aştı",
     lambda d: d["londra"]["h"] > d["asya"]["h"] and d["londra"]["l"] >= d["asya"]["l"]),
    ("sweep_lo", "Londra yalnız Asya dibini kırdı",
     lambda d: d["londra"]["l"] < d["asya"]["l"] and d["londra"]["h"] <= d["asya"]["h"]),
    ("sweep_both", "Londra Asya'nın hem tepesini hem dibini geçti",
     lambda d: d["londra"]["h"] > d["asya"]["h"] and d["londra"]["l"] < d["asya"]["l"]),
    ("inside", "Londra Asya aralığının içinde kaldı",
     lambda d: d["londra"]["h"] <= d["asya"]["h"] and d["londra"]["l"] >= d["asya"]["l"]),
    ("fake_hi", "Londra Asya tepesini aşıp altında kapadı (sahte kırılım ↑)",
     lambda d: d["londra"]["h"] > d["asya"]["h"] and d["londra"]["c"] < d["asya"]["h"]),
    ("fake_lo", "Londra Asya dibini kırıp üstünde kapadı (sahte kırılım ↓)",
     lambda d: d["londra"]["l"] < d["asya"]["l"] and d["londra"]["c"] > d["asya"]["l"]),
    ("uu", "Asya ↑ · Londra ↑", lambda d: d["asya"]["r"] > 0 and d["londra"]["r"] > 0),
    ("ud", "Asya ↑ · Londra ↓", lambda d: d["asya"]["r"] > 0 and d["londra"]["r"] < 0),
    ("du", "Asya ↓ · Londra ↑", lambda d: d["asya"]["r"] < 0 and d["londra"]["r"] > 0),
    ("dd", "Asya ↓ · Londra ↓", lambda d: d["asya"]["r"] < 0 and d["londra"]["r"] < 0),
)


def _acc_row(key: str, label: str, xs: list[float], hours: float | None) -> dict:
    n = len(xs)
    if n == 0:
        return {"key": key, "label": label, "n": 0}
    mean = statistics.mean(xs)
    sd = statistics.stdev(xs) if n > 1 else 0.0
    t = mean / (sd / math.sqrt(n)) if sd > 0 else None
    return {"key": key, "label": label, "n": n, "total": (math.exp(sum(xs)) - 1) * 100,
            "mean": mean * 100, "per_hour": mean * 100 / hours if hours else None,
            "up": sum(1 for x in xs if x > 0) / n * 100, "sd": sd * 100, "t": t,
            "v": verdict(t, n, None, zero_txt="sıfırdan ayırt edilemiyor")}


def accrual(days: list[dict]) -> list[dict]:
    """Getiri hangi seansta birikti: log getiri toplamı, ort./gün, saat başı, t."""
    rows = []
    for key in SESS:
        hours = statistics.mean(d["nb"][key] for d in days) * BAR / 3600 if days else None
        rows.append(_acc_row(key, SESS_TR[key], [d[key]["r"] for d in days], hours))
    rows.append(_acc_row("gun", "Gün (üç seans)", [d["asya"]["r"] + d["londra"]["r"] + d["ny"]["r"]
                                                   for d in days], 24.0))
    rows.append(_acc_row("bosluk", "Boşluk (hafta sonu + tatil)",
                         [d["gap"] for d in days if d.get("gap") is not None], None))
    return rows


def rules_table(days: list[dict]) -> dict:
    """NY açılışında bilinen 10 durum → NY'nin yükselme oranı taban orana karşı."""
    n_all = len(days)
    k_all = sum(1 for d in days if _up(d["ny"]["r"]))
    zb = z_bonf(len(RULES))
    p0 = k_all / n_all * 100 if n_all else None
    rows = []
    for rid, label, fn in RULES:
        sub = [d for d in days if fn(d)]
        n = len(sub)
        k = sum(1 for d in sub if _up(d["ny"]["r"]))
        p = k / n * 100 if n else None
        z = z_fpc(k, n, k_all, n_all)
        rows.append({"id": rid, "label": label, "n": n, "k": k, "p": p,
                     "diff": (p - p0) if (p is not None and p0 is not None) else None, "z": z,
                     "med_ny": statistics.median(d["ny"]["r"] for d in sub) * 100 if sub else None,
                     "v": verdict(z, n, zb)})
    return {"base": {"n": n_all, "k": k_all, "p": p0}, "rows": rows, "K": len(RULES),
            "z_bonf": zb, "exp_fp": len(RULES) * P_Z2}


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for m in range(i, j + 1):
            r[order[m]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3 or len(x) != len(y):
        return None
    rx, ry = _ranks(x), _ranks(y)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return sxy / (sx * sy) if sx > 0 and sy > 0 else None


def _q(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    pos = q * (len(s) - 1)
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def vol_table(days: list[dict]) -> dict:
    """Büyüklük, yön değil: Asya aralığı üç dilim → NY aralığı dağılımı; Spearman ρ."""
    if not days:
        return {"terc": [], "all": None, "asya": None, "londra": None, "cut": None}
    ar = [d["asya"]["rng"] for d in days]
    ny = [d["ny"]["rng"] for d in days]
    q1, q2 = _q(ar, 1 / 3), _q(ar, 2 / 3)
    terc = []
    for name, pick in (("alt", lambda x: x <= q1), ("orta", lambda x: q1 < x <= q2), ("üst", lambda x: x > q2)):
        sub = [d for d in days if pick(d["asya"]["rng"])]
        nys = [d["ny"]["rng"] for d in sub]
        terc.append({"name": name, "n": len(sub),
                     "asia_lo": min((d["asya"]["rng"] for d in sub), default=None),
                     "asia_hi": max((d["asya"]["rng"] for d in sub), default=None),
                     "ny_med": _q(nys, 0.5), "ny_q1": _q(nys, 0.25), "ny_q3": _q(nys, 0.75)})

    def rel(xs):
        rho = spearman(xs, ny)
        n = len(xs)
        z = rho * math.sqrt(n - 1) if rho is not None else None
        return {"rho": rho, "n": n, "z": z, "v": verdict(z, n, z_bonf(2), zero_txt="ilişki ayırt edilemiyor")}
    return {"cut": (q1, q2), "terc": terc,
            "all": {"n": len(ny), "ny_med": _q(ny, 0.5), "ny_q1": _q(ny, 0.25), "ny_q3": _q(ny, 0.75)},
            "asya": rel(ar), "londra": rel([d["londra"]["rng"] for d in days])}


def tercile_of(days_vol: dict, asia_rng: float) -> dict | None:
    cut = (days_vol or {}).get("cut")
    if not cut or cut[0] is None:
        return None
    i = 0 if asia_rng <= cut[0] else 1 if asia_rng <= cut[1] else 2
    return days_vol["terc"][i]


EXT_ROWS = (("up", "low", "Yükselen gün — dip"), ("up", "high", "Yükselen gün — tepe"),
            ("down", "high", "Düşen gün — tepe"), ("down", "low", "Düşen gün — dip"))


def extremes(days: list[dict], reps: int = NULL_REPS, seed: int = NULL_SEED) -> dict:
    """Günün dibi/tepesi hangi seansta — gözlenen vs SAAT KONUMUNU KORUYAN boş model.

    Yol = [p0, 48 kapanış]; k=0 (açılış) Asya'ya sayılır. Boş model: her yarım saat
    konumu için getiri, aynı seans bölmesindeki BAŞKA bir günün aynı konumundan çekilir.
    Bu, gün içi oynaklık profilini (NY en oynak seans) ve konum başı kaymayı korur; AMD'nin
    iddia ettiği gün İÇİ yapıyı (Londra tuzağı → NY ters yön) bozar. Gün içi karıştırma
    YANILTIR (06.10 ölçümü): NY'nin büyük hareketlerini her saate dağıttığı için "uç nokta
    NY'de" oranını yapı gibi gösteriyordu (SP500 düşen gün tepesi |z| 5.1 → konum korununca 1.0).
    Hücre z'si binom: (gözlenen − n·p) / √(n·p·(1−p)). <10 günlük bölme grubu (yaz/kış
    uyumsuz haftalar) karşılaştırmaya girmez, sayısı yazılır."""
    rng = np.random.default_rng(seed)
    groups: dict[tuple, list[dict]] = {}
    for d in sorted(days, key=lambda x: x["day"]):
        groups.setdefault(tuple(d["nb"][k] for k in SESS), []).append(d)
    cells = {(dirn, ext): {"obs": dict.fromkeys(SESS, 0), "exp": dict.fromkeys(SESS, 0.0),
                           "var": dict.fromkeys(SESS, 0.0), "n": 0}
             for dirn, ext, _ in EXT_ROWS}
    skipped = 0
    for nb, ds in sorted(groups.items()):
        rets = []
        for d in ds:
            r = np.diff(np.log(np.asarray([d["p0"]] + list(d["closes"]), dtype=float)))
            if len(r) and np.isfinite(r).all() and r.sum() != 0:
                rets.append(r)
        if len(rets) < 10:
            skipped += len(rets)
            continue
        mat = np.vstack(rets)
        sess_of = np.array([0] * (1 + nb[0]) + [1] * nb[1] + [2] * nb[2])
        n_syn = reps * len(rets)
        pick = rng.integers(0, len(rets), size=(n_syn, mat.shape[1]))
        syn = mat[pick, np.arange(mat.shape[1])]
        paths = np.concatenate([np.zeros((n_syn, 1)), np.cumsum(syn, axis=1)], axis=1)
        syn_up = syn.sum(axis=1) > 0
        null = {}
        for dirn, mask in (("up", syn_up), ("down", ~syn_up)):
            m = int(mask.sum())
            for ext, idx in (("low", np.argmin(paths[mask], axis=1)), ("high", np.argmax(paths[mask], axis=1))):
                null[(dirn, ext)] = (np.bincount(sess_of[idx], minlength=3) / m) if m else np.zeros(3)
        for r in rets:
            path = np.concatenate([[0.0], np.cumsum(r)])
            dirn = "up" if r.sum() > 0 else "down"
            for ext, k in (("low", int(np.argmin(path))), ("high", int(np.argmax(path)))):
                cell = cells[(dirn, ext)]
                cell["n"] += 1
                cell["obs"][SESS[sess_of[k]]] += 1
                for i, sname in enumerate(SESS):
                    p = float(null[(dirn, ext)][i])
                    cell["exp"][sname] += p
                    cell["var"][sname] += p * (1 - p)
    zb = z_bonf(len(EXT_ROWS) * 3)
    rows = []
    for dirn, ext, label in EXT_ROWS:
        c = cells[(dirn, ext)]
        n = c["n"]
        per = {}
        for sname in SESS:
            z = (c["obs"][sname] - c["exp"][sname]) / math.sqrt(c["var"][sname]) if c["var"][sname] > 0 else None
            per[sname] = {"obs": c["obs"][sname] / n * 100 if n else None,
                          "null": c["exp"][sname] / n * 100 if n else None, "z": z}
        zs = [abs(per[x]["z"]) for x in SESS if per[x]["z"] is not None]
        zmax = max(zs) if zs else None
        rows.append({"dir": dirn, "ext": ext, "label": label, "n": n, "per": per, "zmax": zmax,
                     "v": verdict(zmax, n, zb, zero_txt="boş modelden ayırt edilemiyor")})
    return {"rows": rows, "reps": reps, "z_bonf": zb, "skipped": skipped}


def karne(days: list[dict]) -> dict:
    """Saf: tüm paneller + sayfadaki toplam karşılaştırma sayısı ve şansla beklenen |z|≥2."""
    acc = accrual(days)
    rules = rules_table(days)
    vol = vol_table(days)
    ext = extremes(days)
    n_tests = (sum(1 for r in acc if r.get("n")) + rules["K"] + 2
               + sum(3 for r in ext["rows"] if r["n"]))
    return {"accrual": acc, "rules": rules, "vol": vol, "ext": ext,
            "n_tests": n_tests, "exp_fp": n_tests * P_Z2, "n_days": len(days)}


# ---------------- bugün ----------------

def _pct_rank(xs: list[float], x: float) -> float | None:
    return sum(1 for v in xs if v <= x) / len(xs) * 100 if xs else None


def today_view(bars: list[dict], days: list[dict], now_ts: int, k: dict | None) -> dict:
    """Şu anki ET işlem gününün seans durumları + biten seansların arşivdeki yeri +
    eşleşen kural satırları + oynaklık satırı + son tamamlanan gün."""
    d, phase = trading_day_at(now_ts)
    b = bounds(d)
    by = {x["t"]: x for x in bars}
    out = {"day": d, "label": day_label(d), "phase": phase, "sessions": [], "sweep": None,
           "rules": None, "vol": None, "last": None, "next": None, "holiday": None,
           "early": US_EARLY_CLOSE.get(d)}
    pre = by.get(b["a0"] - BAR)
    ref = pre["c"] if pre else None
    st: dict[str, dict | None] = {}
    for key, t0, t1 in _windows(b):
        row = {"key": key, "label": SESS_TR[key], "tsi0": _hm(t0, TR), "tsi1": _hm(t1, TR)}
        if now_ts < t0:
            row["state"] = "başlamadı"
            st[key] = None
        else:
            done = now_ts >= t1
            row["state"] = "bitti" if done else "sürüyor"
            upto = None
            if not done:
                row["left_s"] = t1 - now_ts
                last = max((t for t in by if t0 <= t < t1 and t <= now_ts), default=None)
                upto = last + BAR if last is not None else t0
            s = sess_stats(by, t0, t1, ref, upto=upto) if ref else None
            st[key] = s
            if s:
                row.update(r=s["r"] * 100, rng=s["rng"])
                if not done and s["last_t"] + BAR <= now_ts:      # oluşan mum yok: veri o ana kadar
                    row["upto_tsi"] = _hm(s["last_t"] + BAR, TR)
                if done:
                    row["pct"] = _pct_rank([x[key]["rng"] for x in days], s["rng"])
                ref = s["c"]
            else:
                row["missing"] = True
                ref = None
        out["sessions"].append(row)
    a, lo = st.get("asya"), st.get("londra")
    if a and lo:
        out["sweep"] = {"hi_t": next((x["t"] for x in bars if b["l0"] <= x["t"] < b["n0"] and x["h"] > a["h"]), None),
                        "lo_t": next((x["t"] for x in bars if b["l0"] <= x["t"] < b["n0"] and x["l"] < a["l"]), None),
                        "final": now_ts >= b["n0"]}
        if out["sweep"]["hi_t"]:
            out["sweep"]["hi_tsi"] = _hm(out["sweep"]["hi_t"], TR)
        if out["sweep"]["lo_t"]:
            out["sweep"]["lo_tsi"] = _hm(out["sweep"]["lo_t"], TR)
    if k and a and lo and now_ts >= b["n0"]:
        today = {"asya": a, "londra": lo}
        rows = {r["id"]: r for r in k["rules"]["rows"]}
        out["rules"] = [rows[rid] for rid, _lbl, fn in RULES if _safe_rule(fn, today)]
    if k and a and now_ts >= b["l0"]:
        t = tercile_of(k["vol"], a["rng"])
        if t:
            out["vol"] = {"terc": t, "all": k["vol"]["all"], "rng": a["rng"], "final": now_ts >= b["l0"],
                          "pct": _pct_rank([x["asya"]["rng"] for x in days], a["rng"])}
    if days:
        last = days[-1]
        out["last"] = {"label": day_label(last["d"]), "r": {s: last[s]["r"] * 100 for s in SESS},
                       "ny_up": last["ny"]["r"] > 0,
                       "labels": [lbl for _rid, lbl, fn in RULES if _safe_rule(fn, last)]}
    if phase == "bekleniyor":
        out["next"] = {"tsi": _hm(b["a0"], TR), "et": _hm(b["a0"], ET), "day": day_label(d),
                       "dow": GUN_TR[datetime.fromtimestamp(b["a0"], TR).weekday()]}
        cal = datetime.fromtimestamp(int(now_ts), ET).date()
        out["holiday"] = US_HOLIDAYS.get(cal) or (US_HOLIDAYS.get(cal + timedelta(days=1))
                                                  if datetime.fromtimestamp(int(now_ts), ET).time() >= MKT_CLOSE else None)
    return out


def _safe_rule(fn, d: dict) -> bool:
    try:
        return bool(fn(d))
    except (KeyError, TypeError):
        return False


# ---------------- uygunluk ----------------

async def resolve(cfg, sym: str) -> dict:
    """Sembol → ABD seansına bağlı xyz perp'i mi? Kripto, emtia/FX/ABD dışı endeks ve Asya
    borsasında işlem gören hisseler reddedilir (gerekçesi yazılır); pre-IPO uyarıyla kabul."""
    from ..hl.universe import resolve_coin, similar_names
    s = (sym or "").strip().upper().split(":")[-1]
    if not s:
        return {"ok": False, "sym": s, "reason": "sembol yok"}
    rc = await resolve_coin(s)
    if rc is None:
        return {"ok": False, "sym": s, "reason": "evrende bulunamadı", "similar": await similar_names(s)}
    coin = rc["coin"]
    if assets.klass(coin) == "kripto":
        return {"ok": False, "sym": s, "coin": coin,
                "reason": "kripto 7/24 işlem görür, ABD'ye bağlı bir nakit seansı yok"}
    if s in NON_US_LISTED:
        return {"ok": False, "sym": s, "coin": coin,
                "reason": "dayanağı Asya borsasında işlem görüyor — 'Asya' onun asıl seansı"}
    if assets.kind(coin) == "non_equity" and s not in US_NONEQ_OK:
        return {"ok": False, "sym": s, "coin": coin,
                "reason": "emtia / döviz / ABD dışı endeks — nakit seansı ABD'de değil"}
    caveat = ("bilanço takvimi olmayan enstrüman (pre-IPO / sentetik / sepet) — dayanağının asıl seansı"
              " ABD olmayabilir; seans adları yalnız saat dilimi") if assets.kind(coin) == "no_calendar" else ""
    return {"ok": True, "sym": s, "coin": coin, "caveat": caveat}


def default_symbols(cfg) -> list[str]:
    raw = getattr(cfg, "seans_coins", None) or ["XYZ100", "SP500"]
    if isinstance(raw, str):
        raw = raw.split(",")
    return [str(x).strip().upper().split(":")[-1] for x in raw if str(x).strip()]


# ---------------- arşiv ----------------

_live: dict[str, dict] = {}
_locks: dict[str, asyncio.Lock] = {}


async def last_ts(coin: str) -> int | None:
    async with db() as conn:
        cur = await conn.execute("SELECT MAX(ts) m FROM seans_bars WHERE coin=?", (coin,))
        r = await cur.fetchone()
    return int(r["m"]) if r and r["m"] is not None else None


async def archive_bars(coin: str) -> list[dict]:
    async with db() as conn:
        cur = await conn.execute("SELECT ts, o, h, l, c, v, n FROM seans_bars WHERE coin=? ORDER BY ts",
                                 (coin,))
        rows = await cur.fetchall()
    return [{"t": int(r["ts"]), "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"],
             "n": int(r["n"]) if r["n"] is not None else -1, "closed": True} for r in rows]


async def store_bars(coin: str, bars: list[dict]) -> int:
    rows = [(coin, b["t"], b["o"], b["h"], b["l"], b["c"], b["v"], b["n"]) for b in bars if b.get("closed")]
    if not rows:
        return 0
    async with db() as conn:
        await conn.executemany("INSERT OR REPLACE INTO seans_bars(coin, ts, o, h, l, c, v, n)"
                               " VALUES(?,?,?,?,?,?,?,?)", rows)
        await conn.commit()
    return len(rows)


async def refresh_coin(client, coin: str, now_ts: int | None = None) -> int:
    """Arşivi güncelle: son mumdan iki mum geriden (geç düzeltmeler) ya da ilk kez son 5000 mum."""
    now_ts = int(now_ts or now())
    lt = await last_ts(coin)
    start = (lt - 2 * BAR) if lt else now_ts - HL_MAX_CANDLES * BAR
    raw = await client.candles(coin, "30m", start * 1000, now_ts * 1000)
    return await store_bars(coin, parse_bars(raw, now_ts))


async def sync(client, coin: str, now_ts: int) -> dict:
    """Sayfa/komut için: arşiv yoksa ya da bayatsa doldur, sonra TTL'li canlı kuyruk (oluşan mum
    yalnız bellekte). İstemci yok / hata → arşivle devam, sebebi döner."""
    if client is None:
        return {"ok": False, "err": "canlı veri yok (istemci kapalı)"}
    lock = _locks.setdefault(coin, asyncio.Lock())
    async with lock:
        try:
            lt = await last_ts(coin)
            if lt is None or lt < now_ts - LIVE_SPAN:
                await refresh_coin(client, coin, now_ts)
            hit = _live.get(coin)
            if hit and now_ts - hit["ts"] < LIVE_TTL:
                return {"ok": True, "err": ""}
            raw = await client.candles(coin, "30m", (now_ts - LIVE_SPAN) * 1000, now_ts * 1000)
            bars = parse_bars(raw, now_ts)
            await store_bars(coin, bars)
            # Kapanmamış mumların HEPSİ bellekte: oluşan mum + yeni bitip 60 sn payı dolmamış mum
            # (yalnız sonuncusu tutulsaydı sınırdan sonraki dakikada seansın ortasında delik olurdu)
            _live[coin] = {"ts": now_ts, "forming": [b for b in bars if not b["closed"]]}
            return {"ok": True, "err": ""}
        except Exception as e:                       # noqa: BLE001 — sayfa arşivle açılsın
            log.debug("seans: canlı mum alınamadı %s", coin, exc_info=True)
            return {"ok": False, "err": f"{type(e).__name__}: {e}"[:120]}


async def note_query(cfg, sym: str) -> None:
    """Sekme dışı sorulan sembolü 30 gün arşivle (en çok 12, en yeniler kalır)."""
    if sym in default_symbols(cfg):
        return
    ts = now()
    ex = {k: int(v) for k, v in ((await kv_get(EXTRA_KV)) or {}).items() if ts - int(v) < EXTRA_DAYS * 86400}
    ex[sym] = ts
    keep = dict(sorted(ex.items(), key=lambda kv: -kv[1])[:EXTRA_MAX])
    await kv_set(EXTRA_KV, keep)


async def archive_set(cfg) -> list[str]:
    ts = now()
    ex = (await kv_get(EXTRA_KV)) or {}
    extra = [s for s, t in sorted(ex.items(), key=lambda kv: -int(kv[1])) if ts - int(t) < EXTRA_DAYS * 86400]
    out = default_symbols(cfg)
    out = out + [s for s in extra if s not in out][:EXTRA_MAX]
    if getattr(cfg, "seans_archive_all", False):
        # 🧪 Sekmelerden AYRI arşiv listesi: PROPR'daki ABD hisseleri (açılış evreni). Uygunsuzları
        # (Asya borsası, pre-IPO dışı) refresh_all'daki resolve eler. Sekmeler değişmez.
        try:
            from . import openmove
            syms = sorted(c.split(":")[-1].upper() for c in await openmove.universe(cfg))
            out = out + [s for s in syms if s not in out]
        except Exception:                            # noqa: BLE001 — arşiv listesi sekmelerle sürer
            log.warning("seans arşiv evreni okunamadı", exc_info=True)
    return out


FULL_GAP = 20                     # tam dolumlar (~104 ağırlık) arası en az sn → dakikada ≤ 3
SHARED_MAX = 0.5                  # paylaşılan HL ağırlık penceresinin bu payı doluysa tam dolum bekler


async def _pace_full(client) -> None:
    """Tam dolumdan ÖNCE: düşük şerit 429 sonrası susuyorsa ya da pencere yarıdan doluysa bekle
    (en çok 2 dk). İstemci istek sayar, ağırlığı saymaz — 60 tam dolum art arda 429 doğururdu."""
    for _ in range(24):
        try:
            u = client.usage()
            busy = client.low_paused() > 0 or u["weight"] > SHARED_MAX * u["weight_max"]
        except Exception:                            # noqa: BLE001 — sahte istemci
            return
        if not busy:
            return
        await asyncio.sleep(5)


def calendar_info(now_ts: int) -> dict:
    """Sembolden bağımsız takvim: bugünün seans saatleri, uyumsuz hafta, yaklaşan değişim."""
    d, phase = trading_day_at(now_ts)
    return {"times": session_times(d), "mismatch": bounds(d)["nb"]["asya"] == 24,
            "change": upcoming_change(d), "times_text": times_text(d), "today_label": day_label(d),
            "phase": phase, "holidays_until": HOLIDAYS_UNTIL, "holiday_warn": d > HOLIDAYS_UNTIL}


_karne_cache: dict[str, tuple[tuple, dict]] = {}


async def _karne_cached(coin: str, days: list[dict]) -> dict:
    """Karne yalnız yeni tam gün eklenince değişir — her sayfa yenilemesinde yeniden kurulmaz."""
    key = (len(days), days[0]["day"], days[-1]["day"], days[-1]["closes"][-1])
    hit = _karne_cache.get(coin)
    if hit and hit[0] == key:
        return hit[1]
    k = await asyncio.to_thread(karne, days)
    _karne_cache[coin] = (key, k)
    return k


async def view(cfg, client, sym: str, now_ts: int | None = None) -> dict:
    """Sayfa ve komutun ortak girişi. Beklenen hatalarda istisna atmaz; nedenini yazar."""
    now_ts = int(now_ts or now())
    cal = calendar_info(now_ts)
    r = await resolve(cfg, sym)
    if not r["ok"]:
        return {**cal, **r}
    coin = r["coin"]
    live = await sync(client, coin, now_ts)
    await note_query(cfg, r["sym"])
    bars = await archive_bars(coin)
    last_t = bars[-1]["t"] if bars else -1
    all_bars = bars + [b for b in (_live.get(coin) or {}).get("forming") or [] if b["t"] > last_t]
    thin = thin_level(bars)
    base = {**cal, **r, "thin": thin, "live": live}
    if not bars:
        return {**base, "ok": False, "reason": "arşivde henüz mum yok" + (f" ({live['err']})" if live.get("err") else "")}
    if thin["level"] == "block":
        return {**base, "ok": False,
                "reason": f"30 dk'lık mumların %{thin['share'] * 100:.0f}'ında hiç işlem yok — seans ölçümleri"
                          " bayat fiyatı ölçer, karne üretilmedi"}
    days, drops = split_days(bars, now_ts)
    k = await _karne_cached(coin, days) if days else None
    span = {"n_days": len(days), "first": day_label(days[0]["d"]) if days else None,
            "last": day_label(days[-1]["d"]) if days else None, "drops": drops, "n_bars": len(bars),
            "since": datetime.fromtimestamp(bars[0]["t"], TR).strftime("%d.%m.%Y"),
            "last_t": bars[-1]["t"] + BAR, "stale": now_ts - (bars[-1]["t"] + BAR) > 3 * 3600}
    return {**base, "ok": True, "k": k, "span": span, "today": today_view(all_bars, days, now_ts, k)}


# ---------------- arka plan arşivi ----------------

async def refresh_all(cfg, client) -> dict:
    out = {"coins": [], "rows": 0, "err": 0, "err_msg": "", "skipped": []}
    for sym in await archive_set(cfg):
        try:
            r = await resolve(cfg, sym)
            if not r["ok"]:
                out["skipped"].append(sym)
                continue
            full = await last_ts(r["coin"]) is None
            if full:
                await _pace_full(client)
            out["rows"] += await refresh_coin(client, r["coin"])
            out["coins"].append(sym)
            if full:
                await asyncio.sleep(FULL_GAP)        # tam dolumlar (~104 ağırlık) arası nefes
        except asyncio.CancelledError:
            raise
        except Exception as e:                       # noqa: BLE001 — sembol başına
            out["err"] += 1
            out["err_msg"] = f"{sym}: {type(e).__name__}: {e}"[:160]
            log.warning("seans arşivi okunamadı %s: %s", sym, e)
    return out


async def loop(cfg, client) -> None:
    """Saatte bir arşiv (düşük öncelik). Site ASLA buna bağımlı değil."""
    from ..health import beat
    from ..hl.client import PRIORITY
    PRIORITY.set("low")
    await asyncio.sleep(120)
    while True:
        try:
            if getattr(cfg, "seans_enabled", True):
                out = await refresh_all(cfg, client)
                await kv_set(STATS_KV, {**out, "ts": now()})
            else:
                await kv_set(STATS_KV, {"ts": now(), "disabled": True})
            await beat("seans")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("seans arşivi turu")
        await asyncio.sleep(max(REFRESH_MIN, int(getattr(cfg, "seans_refresh_sec", 3600) or 3600)))
