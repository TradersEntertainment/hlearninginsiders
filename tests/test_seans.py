"""🕰 ABD seans karnesi — Asya → Londra → New York (ICT "Power of 3" / AMD iddiasının ölçümü).

Kullanıcı (06.10): "24 saat 3'e ayrılır — accumulation, pump, dump… Londra session'da şu, Asya
session'da şuysa ABD'de şu olur gibi çıkarımlar ve eskiye bakıp örüntü kurmalar." Kararlar: yalnız
ABD — sayfa + komut (zamanlanmış mesaj yok); sekmeler XYZ100, SP500; hisse perp'leri komutla /
?sym= ile; seanslar piyasa saatleri, yaz/kış uyumlu.

Pinlenenler:
  • takvim: 2026'nın her işlem günü 48 mum, (22,13,13) ya da 20 uyumsuz günde (24,11,13); TSİ
    saatleri yaz / uyumsuz hafta / kış; yaklaşan değişim 26.10; faz sınırları (16:00 ET, Cuma →
    Pazartesi, Pazar akşamı, tatil)
  • mumlar: 5000 → 5000 (tavan yok); bozuk / kaymış / ters satır atılır; n yoksa −1
  • günler: zincir (üç seans = gün), Boşluk yalnız Pazartesi / tatil ertesi, Σseans + Σboşluk =
    toplam; delik "eksik", tatil, yarım gün ayrı sayılır
  • istatistik: sonlu-örneklem z (N=40, n=20, 15/20 → 3.122), Bonferroni 2.807, /orintu ile aynı
    karar sözleri; ilk dört kural günleri bölüşür; ρ = ±1; boş model aynı tohumda aynı, homojen
    yürüyüşte "düzeltme sonrası" yok — ama gerçek gün içi yapıyı (Londra tuzağı → NY) yakalıyor
  • bugün: yaz / kış / uyumsuz hafta / hafta sonu / Pazar gecesi / tatil; Londra bitmeden kural yok
  • uygunluk: XYZ100 / SP500 / NVDA kabul; GOLD / KIOXIA / BTC / DAX / ZZZZ gerekçeyle ret;
    takvimsiz uyarıyla; seyrek mum blok / uyarı
  • arşiv: ilk tam dolum, TTL içinde istek yok, kuyruk, yeniden yazma idempotent, sorulan hisse 12 / 30 gün
  • sayfa (ham ASGI, istemci yok): /seans, ?sym=SP500|GOLD|ZZZZ, anahtar linkleri, hata 500 değil
  • komut: sahip tek mesaj; hesap grubundan üye; yabancı grup sessiz; hafta sonu; linkte key= yok
  • kablolama: spawn, tablo, sağlık, tanı, 3 ayar künyeli, nav, rota, README, .env, yardım
"""
import asyncio
import datetime as dt
import math
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-seans.db")
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.hl import universe as uni  # noqa: E402
from app.radar import seans  # noqa: E402

UTC = dt.timezone.utc
BAR = seans.BAR
BANNED = ("muhtemel", "olası", "olabilir", "bekleniyor", "yükselecek", "düşecek")


def ts(y, m, d, hh=0, mm=0):
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=UTC).timestamp())


def walk(t0, t1, seed=7, px=100.0, vol=0.002, n=5, step=None, zero_every=0):
    """[t0, t1) yarım saatlik sentetik mumlar, HL ham biçiminde. step(t, rng) → log getiri."""
    rng = random.Random(seed)
    out, p = [], px
    t0 -= t0 % BAR
    for i, t in enumerate(range(t0, t1, BAR)):
        r = step(t, rng) if step else rng.gauss(0, vol)
        o, c = p, p * math.exp(r)
        h = max(o, c) * (1 + abs(rng.gauss(0, vol / 4)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, vol / 4)))
        out.append({"t": t * 1000, "T": (t + BAR) * 1000 - 1, "s": "X", "i": "30m",
                    "o": f"{o:.8f}", "h": f"{h:.8f}", "l": f"{lo:.8f}", "c": f"{c:.8f}", "v": "10.0",
                    "n": 0 if (zero_every and i % zero_every == 0) else n})
        p = c
    return out


def upto(now_ts):
    """Oluşan mum dahil son mumun bitişi."""
    return now_ts - now_ts % BAR + BAR


class Client:
    """candleSnapshot taklidi: [start, end] içinde açılan mumlar, en yeni 5000."""

    def __init__(self, series):
        self.series = series
        self.calls = []

    async def candles(self, coin, interval, start_ms, end_ms):
        self.calls.append((coin, interval, start_ms // 1000, end_ms // 1000))
        assert interval == "30m"
        rows = [x for x in self.series.get(coin, []) if start_ms <= x["t"] <= end_ms]
        return rows[-seans.HL_MAX_CANDLES:]


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "seans.db"))
    async with dbm.db() as c:
        for sym in ("XYZ100", "SP500", "NVDA", "GOLD", "KIOXIA", "SPCX", "DAX", "THIN", "WARNY"):
            await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES(?,?,?)", (f"xyz:{sym}", "xyz", sym))
        await c.commit()
    await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"BTC": {"m": 60000.0, "oi": 1, "v": 1.0}}, "ts": dbm.now()})
    for cache in (seans._live, seans._karne_cache, seans._locks):
        cache.clear()
    cfg = Config()
    cfg.seans_archive_all = False          # lab arşiv listesi ayrı testte
    return cfg


def days_for(now_ts, start=None, seed=7, step=None, drop=()):
    raw = walk(start or ts(2026, 6, 20), upto(now_ts), seed=seed, step=step)
    raw = [x for x in raw if x["t"] // 1000 not in drop]
    bars = seans.parse_bars(raw, now_ts)
    days, drops = seans.split_days(bars, now_ts)
    return bars, days, drops


# ---------------- takvim ----------------

def test_calendar_dst_and_sessions():
    d, n_mm, mm = dt.date(2026, 1, 1), 0, []
    while d.year == 2026:
        if seans.is_trading_day(d):
            nb = seans.bounds(d)["nb"]
            assert sum(nb.values()) == 48, (d, nb)
            key = (nb["asya"], nb["londra"], nb["ny"])
            assert key in ((22, 13, 13), (24, 11, 13)), (d, key)
            if key == (24, 11, 13):
                mm.append(d)
        d += dt.timedelta(days=1)
    assert len(mm) == 20 and mm[0] == dt.date(2026, 3, 9) and mm[14] == dt.date(2026, 3, 27), mm
    assert mm[15:] == [dt.date(2026, 10, x) for x in (26, 27, 28, 29, 30)], mm[15:]
    # TSİ: yaz / uyumsuz hafta / kış
    assert seans.times_text(dt.date(2026, 10, 6)) == "Asya 23:00–10:00 · Londra 10:00–16:30 · New York 16:30–23:00"
    assert seans.times_text(dt.date(2026, 10, 27)) == "Asya 23:00–11:00 · Londra 11:00–16:30 · New York 16:30–23:00"
    assert seans.times_text(dt.date(2026, 11, 3)) == "Asya 00:00–11:00 · Londra 11:00–17:30 · New York 17:30–00:00"
    rows = seans.session_times(dt.date(2026, 10, 6))
    assert [(r["et0"], r["et1"], r["hours"], r["nb"]) for r in rows] == [
        ("16:00", "03:00", 11.0, 22), ("03:00", "09:30", 6.5, 13), ("09:30", "16:00", 6.5, 13)], rows
    assert rows[0]["prev_day"] and not rows[1]["prev_day"]
    ch = seans.upcoming_change(dt.date(2026, 10, 6))
    assert ch["label"] == "Pzt 26.10" and "Londra 11:00–16:30" in ch["text"], ch
    assert seans.upcoming_change(dt.date(2026, 10, 27))["label"] == "Pzt 02.11"
    assert seans.upcoming_change(dt.date(2026, 7, 1)) is None
    # tatil / yarım gün
    assert not seans.is_trading_day(dt.date(2026, 9, 7)) and not seans.is_trading_day(dt.date(2026, 10, 10))
    assert seans.is_trading_day(dt.date(2026, 11, 27)) and dt.date(2026, 11, 27) in seans.US_EARLY_CLOSE
    assert seans.next_trading_day(dt.date(2026, 9, 4)) == dt.date(2026, 9, 8)
    assert seans.prev_trading_day(dt.date(2026, 9, 8)) == dt.date(2026, 9, 4)
    print("✅ takvim) 48 mum; 20 uyumsuz gün (9–27.03, 26–30.10) Londra 11 / Asya 24; TSİ yaz/uyumsuz/kış;"
          " yaklaşan değişim 26.10 → 02.11; tatil/yarım gün")


def test_trading_day_phases():
    cases = [
        (ts(2026, 10, 6, 4, 0), (dt.date(2026, 10, 6), "asya")),       # 00:00 ET
        (ts(2026, 10, 6, 7, 0), (dt.date(2026, 10, 6), "londra")),     # 10:00 TSİ = 08:00 Londra
        (ts(2026, 10, 6, 13, 29), (dt.date(2026, 10, 6), "londra")),
        (ts(2026, 10, 6, 13, 30), (dt.date(2026, 10, 6), "ny")),       # 09:30 ET
        (ts(2026, 10, 6, 19, 59), (dt.date(2026, 10, 6), "ny")),
        (ts(2026, 10, 6, 20, 0), (dt.date(2026, 10, 7), "asya")),      # 16:00 ET → ertesi gün Asya
        (ts(2026, 10, 9, 20, 0), (dt.date(2026, 10, 12), "bekleniyor")),   # Cuma 16:00 ET
        (ts(2026, 10, 10, 9, 0), (dt.date(2026, 10, 12), "bekleniyor")),   # Cumartesi
        (ts(2026, 10, 11, 20, 0), (dt.date(2026, 10, 12), "asya")),    # Pazar 16:00 ET
        (ts(2026, 9, 6, 20, 30), (dt.date(2026, 9, 8), "bekleniyor")),     # İşçi Bayramı arifesi
        (ts(2026, 9, 7, 20, 0), (dt.date(2026, 9, 8), "asya")),
        (ts(2026, 10, 27, 7, 30), (dt.date(2026, 10, 27), "asya")),    # uyumsuz hafta: Asya 11:00 TSİ'ye kadar
        (ts(2026, 10, 27, 8, 0), (dt.date(2026, 10, 27), "londra")),
        (ts(2026, 11, 3, 7, 30), (dt.date(2026, 11, 3), "asya")),      # kış: 10:30 TSİ hâlâ Asya
        (ts(2026, 11, 3, 14, 30), (dt.date(2026, 11, 3), "ny")),       # kış: NY 17:30 TSİ
    ]
    for t, want in cases:
        assert seans.trading_day_at(t) == want, (dt.datetime.fromtimestamp(t, UTC), seans.trading_day_at(t), want)
    print("✅ faz) 16:00 ET sınırı, Cuma → Pazartesi, Pazar akşamı Asya, tatil, uyumsuz hafta, kış")


# ---------------- mumlar ve günler ----------------

def test_parse_bars_no_cap_and_cleaning():
    t0 = ts(2026, 6, 1)
    raw = walk(t0, t0 + 5000 * BAR)
    bars = seans.parse_bars(raw, t0 + 5001 * BAR)
    assert len(bars) == 5000 and all(b["closed"] for b in bars), len(bars)     # pricechart 1200'de keser
    t1 = t0 + 5000 * BAR
    junk = [{"t": "x"}, {"t": (t1 + 60) * 1000, "o": 1, "h": 1, "l": 1, "c": 1},       # kaymış
            {"t": t1 * 1000, "o": 1, "h": 0.5, "l": 1, "c": 1},                          # h < l
            {"t": t1 * 1000, "o": 0, "h": 1, "l": 0, "c": 1},                            # sıfır fiyat
            {"t": (t1 + BAR) * 1000, "o": "2", "h": "2", "l": "2", "c": "2"},            # n yok
            {"t": (t1 + BAR) * 1000, "o": "3", "h": "3", "l": "3", "c": "3", "n": 4}]    # aynı t: sonuncu
    out = seans.parse_bars(junk, t1 + 2 * BAR + 30)
    assert [(b["t"], b["c"], b["n"]) for b in out] == [(t1 + BAR, 3.0, 4)], out
    assert out[0]["closed"] is False, "kapanış payı (60 sn) dolmadan kapanmış sayılmaz"
    assert seans.parse_bars([{"t": (t1 + BAR) * 1000, "o": 2, "h": 2, "l": 2, "c": 2}], t1 + 9 * BAR)[0]["n"] == -1
    print("✅ mumlar) 5000 → 5000 (tavan yok); bozuk/kaymış/ters/sıfır atılır; aynı t sonuncu; n yoksa −1; kapanış payı")


def test_split_days_chain_gap_identity():
    now_ts = ts(2026, 10, 6, 9, 0)
    bars, days, drops = days_for(now_ts, start=ts(2026, 8, 20))
    assert drops == {"eksik": 0, "tatil": 1, "yarim": 0}, drops                  # 07.09 İşçi Bayramı
    assert days[0]["day"] == "2026-08-21" and days[-1]["day"] == "2026-10-05", (days[0]["day"], days[-1]["day"])
    assert "2026-09-07" not in {d["day"] for d in days}
    by = {b["t"]: b for b in bars}
    for d in days:
        assert len(d["closes"]) == 48 and sum(d["nb"].values()) == 48
        tot = d["asya"]["r"] + d["londra"]["r"] + d["ny"]["r"]
        assert abs(tot - math.log(d["ny"]["c"] / d["p0"])) < 1e-12, d["day"]
        b = seans.bounds(d["d"])
        assert d["p0"] == by[b["a0"] - BAR]["c"] and d["ny"]["c"] == by[b["n1"] - BAR]["c"]
        gap_expected = (d["d"] - seans.prev_trading_day(d["d"])).days > 1
        assert (d["gap"] is not None) == gap_expected, (d["day"], d["gap"])
    tue = next(d for d in days if d["day"] == "2026-09-08")
    assert tue["gap"] is not None, "tatil ertesi: boşluk Cuma kapanışından"
    # Σ seans + Σ boşluk = ilk günün p0'ından son günün NY kapanışına toplam log getiri
    total = sum(d["asya"]["r"] + d["londra"]["r"] + d["ny"]["r"] for d in days)
    total += sum(d["gap"] for d in days if d["gap"] is not None)
    assert abs(total - math.log(days[-1]["ny"]["c"] / days[0]["p0"])) < 1e-9
    # delik → o gün "eksik" düşer; yarım gün ve tatil ayrı
    hole = seans.bounds(dt.date(2026, 9, 16))["l0"] + 3 * BAR
    _, days2, drops2 = days_for(now_ts, start=ts(2026, 8, 20), drop={hole})
    assert drops2["eksik"] == 1 and "2026-09-16" not in {d["day"] for d in days2} and len(days2) == len(days) - 1
    _, days3, drops3 = days_for(ts(2026, 12, 2, 12, 0), start=ts(2026, 11, 16))
    assert drops3 == {"eksik": 0, "tatil": 1, "yarim": 1}, drops3                # 26.11 Şükran, 27.11 yarım gün
    assert not {"2026-11-26", "2026-11-27"} & {d["day"] for d in days3}
    acc = {r["key"]: r for r in seans.accrual(days)}
    assert acc["bosluk"]["n"] == sum(1 for d in days if d["gap"] is not None) and acc["gun"]["n"] == len(days)
    print("✅ günler) zincir: üç seans = gün; boşluk yalnız Pazartesi/tatil ertesi; Σseans+Σboşluk = toplam;"
          " delik 'eksik', tatil, yarım gün ayrı sayılır")


# ---------------- istatistik ----------------

def _day(a, lo, ny_r, a_r=0.001, l_r=0.001):
    """Kural tablosu için el yapımı gün: a=(tepe, dip), lo=(tepe, dip, kapanış)."""
    return {"asya": {"h": a[0], "l": a[1], "c": 100.0, "r": a_r},
            "londra": {"h": lo[0], "l": lo[1], "c": lo[2], "r": l_r},
            "ny": {"r": ny_r}}


def test_rule_stats_and_verdicts():
    from app.radar import analog
    assert abs(seans.z_fpc(15, 20, 20, 40) - 3.1225) < 1e-3
    assert abs(seans.z_bonf(10) - 2.807) < 1e-3 and abs(seans.z_bonf(12) - 2.865) < 1e-3
    assert seans.z_fpc(5, 40, 20, 40) is None and seans.z_fpc(0, 0, 20, 40) is None
    v = seans.verdict(None, 5)
    assert v["label"] == "yetersiz" and v["note"] == "yetersiz (n=5 < 20)"
    assert seans.verdict(1.5, 19)["note"] == "yetersiz (n=19 < 20)"
    assert seans.verdict(1.0, 30)["note"] == "taban orandan ayırt edilemiyor"
    assert seans.verdict(2.5, 30, 2.807)["note"] == "kayda değer (tek test) — düzeltme sonrası değil"
    v = seans.verdict(-3.0, 30, 2.807)
    assert v["note"] == "kayda değer — düzeltme sonrası da" and v["survives"]
    # /orintu ile aynı eşik ve sözler
    a = analog.verdict({"n": 30, "p_up": 55.0}, {"p_up": 50.0})
    assert a["label"] == seans.verdict(0.55, 30)["label"] == "zayıf" and a["note"] == seans.verdict(0.55, 30)["note"]
    assert analog.verdict({"n": 19, "p_up": 90.0}, {"p_up": 50.0})["label"] == "yetersiz" and seans.MIN_N == 20
    # 30 gün "Londra yalnız Asya dibini kırdı" → NY hep ↑; 30 gün "içeride" → NY hep ↓
    days = [_day((101, 99), (100.5, 98.5, 99.5), 0.004) for _ in range(30)]
    days += [_day((101, 99), (100.5, 99.5, 100.0), -0.004, a_r=-0.001, l_r=-0.001) for _ in range(30)]
    rt = seans.rules_table(days)
    rows = {r["id"]: r for r in rt["rows"]}
    assert rt["base"] == {"n": 60, "k": 30, "p": 50.0} and rt["K"] == 10 and abs(rt["exp_fp"] - 0.455) < 1e-9
    assert sum(rows[x]["n"] for x in ("sweep_hi", "sweep_lo", "sweep_both", "inside")) == 60, "ilk dört kural bölüşür"
    assert rows["sweep_lo"]["p"] == 100.0 and rows["sweep_lo"]["v"]["note"] == "kayda değer — düzeltme sonrası da"
    assert abs(rows["sweep_lo"]["z"] - 0.5 / math.sqrt(0.25 / 30 * 30 / 59)) < 1e-9
    assert rows["inside"]["z"] < -7 and rows["fake_lo"]["n"] == 30 and rows["sweep_hi"]["v"]["note"] == "yetersiz (n=0 < 20)"
    assert rows["uu"]["n"] == 30 and rows["dd"]["n"] == 30 and rows["ud"]["n"] == 0
    print("✅ istatistik) sonlu-örneklem z 3.122, Bonferroni 2.807/2.865; karar sözleri /orintu ile aynı;"
          " ilk dört kural bölüşür; kurgulanmış yapı 'düzeltme sonrası da'")


def test_volatility_relation():
    days = [{"asya": {"rng": float(i)}, "londra": {"rng": 1.0 + (i * 7) % 5}, "ny": {"rng": 2.0 * i}}
            for i in range(1, 31)]
    vt = seans.vol_table(days)
    assert abs(vt["asya"]["rho"] - 1) < 1e-12 and abs(vt["asya"]["z"] - math.sqrt(29)) < 1e-9
    assert vt["asya"]["v"]["note"] == "kayda değer — düzeltme sonrası da"
    assert [t["n"] for t in vt["terc"]] == [10, 10, 10]
    assert vt["terc"][0]["ny_med"] < vt["terc"][1]["ny_med"] < vt["terc"][2]["ny_med"]
    assert seans.tercile_of(vt, 0.5)["name"] == "alt" and seans.tercile_of(vt, 99)["name"] == "üst"
    for d in days:
        d["ny"]["rng"] = 100.0 - d["asya"]["rng"]
    assert abs(seans.vol_table(days)["asya"]["rho"] + 1) < 1e-12
    rng = random.Random(11)
    for d in days:
        d["ny"]["rng"] = rng.random()
    v = seans.vol_table(days)["asya"]
    assert abs(v["z"]) < 2 and v["v"]["note"] == "ilişki ayırt edilemiyor", v
    print("✅ oynaklık) ρ = +1 / −1, z = ρ√(n−1); üç dilim 10/10/10; bağımsızda 'ilişki ayırt edilemiyor'")


def _amd_step(t, rng):
    """Gerçek gün içi yapı: Londra bir yöne (tuzak), NY tersine ve güçlü — yön gün gün değişir."""
    d, ph = seans.trading_day_at(t)
    sign = 1 if d.toordinal() % 2 == 0 else -1
    if ph == "londra":
        return -sign * 0.002 + rng.gauss(0, 0.0005)
    if ph == "ny":
        return sign * 0.004 + rng.gauss(0, 0.0005)
    return rng.gauss(0, 0.0005)


def test_extremes_null_model():
    now_ts = ts(2026, 10, 6, 9, 0)
    _, days, _ = days_for(now_ts)
    e1, e2 = seans.extremes(days), seans.extremes(days)
    assert e1 == e2, "aynı tohum → aynı sayı (yenilemede titreme yok)"
    assert e1["reps"] == 200 and abs(e1["z_bonf"] - seans.z_bonf(12)) < 1e-12 and e1["skipped"] == 0
    for r in e1["rows"]:
        assert not r["v"]["survives"], r
        tot_obs = sum(r["per"][s]["obs"] for s in seans.SESS)
        tot_null = sum(r["per"][s]["null"] for s in seans.SESS)
        assert abs(tot_obs - 100) < 1e-9 and abs(tot_null - 100) < 1e-6, r
    assert sum(r["n"] for r in e1["rows"]) == 2 * len(days)
    # Gerçek yapı yakalanır: yükselen günün dibi Londra sonunda, düşen günün tepesi Londra sonunda
    _, amd, _ = days_for(now_ts, step=_amd_step)
    rows = {(r["dir"], r["ext"]): r for r in seans.extremes(amd)["rows"]}
    up_lo, dn_hi = rows[("up", "low")], rows[("down", "high")]
    assert up_lo["per"]["londra"]["obs"] > 90 and up_lo["per"]["londra"]["null"] < 60, up_lo
    assert up_lo["v"]["note"] == "kayda değer — düzeltme sonrası da" and dn_hi["v"]["survives"], (up_lo, dn_hi)
    k = seans.karne(days)
    assert k["n_tests"] == 29 and abs(k["exp_fp"] - 29 * 0.0455) < 1e-9 and k["n_days"] == len(days)
    print("✅ dip/tepe) aynı tohum aynı sonuç; homojen yürüyüşte hiçbiri 'düzeltme sonrası' değil;"
          " gerçek Londra-tuzağı yapısı boş modelden ayrışıyor; 29 karşılaştırma")


# ---------------- bugün ----------------

def _today(now_ts, forming=True, start=None):
    bars, days, _ = days_for(now_ts, start=start or now_ts - 80 * 86400)
    if not forming:
        bars = [b for b in bars if b["closed"]]
    k = seans.karne(days) if days else None
    return seans.today_view(bars, days, now_ts, k), days


def test_today_view_states():
    t, days = _today(ts(2026, 10, 6, 9, 0))                         # Sal 12:00 TSİ: Londra sürüyor
    st = {r["key"]: r for r in t["sessions"]}
    assert t["label"] == "Sal 06.10" and t["phase"] == "londra"
    assert st["asya"]["state"] == "bitti" and st["asya"]["pct"] is not None and "r" in st["asya"]
    assert st["londra"]["state"] == "sürüyor" and st["londra"]["left_s"] == 16200 and "upto_tsi" not in st["londra"]
    assert st["ny"]["state"] == "başlamadı" and "r" not in st["ny"]
    assert t["sweep"] is not None and t["sweep"]["final"] is False
    assert t["rules"] is None, "Londra bitmeden kural satırı yok"
    assert t["vol"]["terc"]["name"] in ("alt", "orta", "üst") and t["vol"]["rng"] == st["asya"]["rng"]
    assert t["last"]["label"] == "Pzt 05.10" and set(t["last"]["r"]) == set(seans.SESS)
    # Londra bitti → bugünün şekline uyan kurallar (bölüşen dörtten tam biri)
    t2, _ = _today(ts(2026, 10, 6, 14, 0))
    assert t2["phase"] == "ny" and t2["sweep"]["final"] is True and t2["rules"]
    part = [r for r in t2["rules"] if r["id"] in ("sweep_hi", "sweep_lo", "sweep_both", "inside")]
    assert len(part) == 1, t2["rules"]
    # oluşan mum yok (istemci kapalı) → son mumla, "veri HH:MM'e kadar"
    t3, _ = _today(ts(2026, 10, 6, 9, 10), forming=False)
    lon = t3["sessions"][1]
    assert lon["state"] == "sürüyor" and lon["upto_tsi"] == "12:00" and "r" in lon, lon
    # hafta sonu → sıradaki Asya Pazar 23:00 TSİ
    t4, _ = _today(ts(2026, 10, 10, 9, 0))
    assert t4["phase"] == "bekleniyor" and t4["next"] == {"tsi": "23:00", "et": "16:00", "day": "Pzt 12.10", "dow": "Paz"}
    assert t4["holiday"] is None and t4["last"]["label"] == "Cum 09.10"
    # Pazar gecesi 23:30 TSİ → Pazartesi'nin Asya'sı sürüyor
    t5, _ = _today(ts(2026, 10, 11, 20, 30))
    assert t5["label"] == "Pzt 12.10" and t5["phase"] == "asya" and t5["sessions"][0]["state"] == "sürüyor"
    # kış: Asya 00:00–11:00, Londra 11:00–17:30
    t6, _ = _today(ts(2026, 11, 3, 9, 0))
    assert t6["phase"] == "londra" and (t6["sessions"][0]["tsi0"], t6["sessions"][1]["tsi1"]) == ("00:00", "17:30")
    # uyumsuz hafta: 10:30 TSİ hâlâ Asya (11:00'e kadar)
    t7, _ = _today(ts(2026, 10, 27, 7, 30))
    assert t7["phase"] == "asya" and t7["sessions"][0]["tsi1"] == "11:00" and t7["vol"] is None
    # tatil (İşçi Bayramı) → ABD seansı yok, adıyla
    t8, _ = _today(ts(2026, 9, 7, 9, 0))
    assert t8["phase"] == "bekleniyor" and t8["holiday"] == "İşçi Bayramı" and t8["next"]["day"] == "Sal 08.09"
    print("✅ bugün) Londra sürüyor (kalan, kesinleşmedi, kural yok), Londra bitti (tek bölme kuralı),"
          " oluşan mum yok, hafta sonu, Pazar gecesi, kış, uyumsuz hafta, tatil")


# ---------------- uygunluk, arşiv, görünüm ----------------

def test_eligibility_and_thin():
    async def run():
        cfg = await _fresh()
        for sym in ("XYZ100", "SP500", "NVDA", "xyz:SP500"):
            r = await seans.resolve(cfg, sym)
            assert r["ok"] and r["caveat"] == "" and r["coin"].startswith("xyz:"), r
        reasons = {s: (await seans.resolve(cfg, s)) for s in ("GOLD", "KIOXIA", "BTC", "DAX", "ZZZZ", "")}
        assert "emtia" in reasons["GOLD"]["reason"] and "emtia" in reasons["DAX"]["reason"]
        assert "Asya borsasında" in reasons["KIOXIA"]["reason"]
        assert "kripto 7/24" in reasons["BTC"]["reason"]
        assert reasons["ZZZZ"]["reason"] == "evrende bulunamadı" and reasons[""]["reason"] == "sembol yok"
        assert not any(r["ok"] for r in reasons.values())
        pre = await seans.resolve(cfg, "SPCX")
        assert pre["ok"] and "bilanço takvimi olmayan" in pre["caveat"]
        # seyrek: işlemsiz mum payı ≥ %33 → karne yok; %10–33 → uyarı
        now_ts = ts(2026, 10, 6, 9, 0)
        cl = Client({"xyz:THIN": walk(ts(2026, 8, 1), upto(now_ts), zero_every=2),
                     "xyz:WARNY": walk(ts(2026, 8, 1), upto(now_ts), zero_every=6)})
        v = await seans.view(cfg, cl, "THIN", now_ts=now_ts)
        assert not v["ok"] and "hiç işlem yok" in v["reason"] and v["thin"]["level"] == "block", v.get("reason")
        assert v["times"] and v["today_label"] == "Sal 06.10", "ret durumunda da takvim var"
        v = await seans.view(cfg, cl, "WARNY", now_ts=now_ts)
        assert v["ok"] and v["thin"]["level"] == "warn" and 0.1 <= v["thin"]["share"] < 0.33
        v = await seans.view(cfg, cl, "GOLD", now_ts=now_ts)
        assert not v["ok"] and v["times"] and not [c for c in cl.calls if c[0] == "xyz:GOLD"], "ret → HL'ye istek yok"
        print("✅ uygunluk) XYZ100/SP500/NVDA kabul; GOLD/DAX emtia-endeks, KIOXIA Asya borsası, BTC kripto,"
              " ZZZZ yok — gerekçeli ret, istek yok; SPCX uyarıyla; seyrek blok/uyarı")
    asyncio.run(run())


def test_archive_sync_ttl_and_extras():
    async def run():
        cfg = await _fresh()
        now_ts = ts(2026, 10, 6, 9, 0)
        raw = walk(ts(2026, 6, 1), upto(now_ts))
        cl = Client({"xyz:SP500": raw, "xyz:NVDA": raw})
        v = await seans.view(cfg, cl, "SP500", now_ts=now_ts)
        assert v["ok"] and v["live"] == {"ok": True, "err": ""} and v["k"]["n_days"] >= 60
        assert len(cl.calls) == 2, cl.calls
        assert cl.calls[0][2] == now_ts - seans.HL_MAX_CANDLES * BAR, "ilk kez: son 5000 mum"
        assert cl.calls[1][2] == now_ts - seans.LIVE_SPAN, "sonra canlı kuyruk"
        async with dbm.db() as c:
            n1 = (await (await c.execute("SELECT COUNT(*) n FROM seans_bars WHERE coin='xyz:SP500'")).fetchone())["n"]
        # oluşan mum + az önce bitip 60 sn payı dolmamış mum yazılmaz — ama bellekte: Londra'da delik yok
        assert n1 == seans.HL_MAX_CANDLES - 2, n1
        lon = v["today"]["sessions"][1]
        assert v["span"]["n_bars"] == n1 and lon["state"] == "sürüyor" and "r" in lon and not lon.get("missing"), lon
        assert len(seans._live["xyz:SP500"]["forming"]) == 2
        # TTL içinde yeni istek yok; sonra yalnız kuyruk
        await seans.view(cfg, cl, "SP500", now_ts=now_ts + 60)
        assert len(cl.calls) == 2
        await seans.view(cfg, cl, "SP500", now_ts=now_ts + seans.LIVE_TTL + 5)
        assert len(cl.calls) == 3 and cl.calls[2][2] == now_ts + seans.LIVE_TTL + 5 - seans.LIVE_SPAN
        # kuyruk, payı dolan mumu yazdı (+1); yeniden yazma idempotent; artımlı güncelleme iki mum geriden
        async def count():
            async with dbm.db() as c:
                return (await (await c.execute("SELECT COUNT(*) n FROM seans_bars WHERE coin='xyz:SP500'")).fetchone())["n"]
        n2 = await count()
        assert n2 == n1 + 1, (n1, n2)
        await seans.store_bars("xyz:SP500", seans.parse_bars(raw[-300:], now_ts + 3600))
        assert await count() == n2 + 1, "yalnız yeni kapanan (09:00) eklenir, eskiler üzerine yazılır"
        await seans.store_bars("xyz:SP500", seans.parse_bars(raw[-300:], now_ts + 3600))
        assert await count() == n2 + 1
        lt = await seans.last_ts("xyz:SP500")
        await seans.refresh_coin(cl, "xyz:SP500", now_ts)
        assert cl.calls[-1][2] == lt - 2 * BAR
        # istemci yok → arşivle, sebebiyle
        v = await seans.view(cfg, None, "SP500", now_ts=now_ts + 900)
        assert v["ok"] and v["live"] == {"ok": False, "err": "canlı veri yok (istemci kapalı)"}
        # sekme dışı sorulan hisse 30 gün arşivlenir (en çok 12); sekmeler eklenmez
        await seans.view(cfg, cl, "NVDA", now_ts=now_ts)
        ex = await dbm.kv_get(seans.EXTRA_KV)
        assert set(ex) == {"NVDA"}, ex
        t = dbm.now()
        await dbm.kv_set(seans.EXTRA_KV, {**{f"S{i:02d}": t - (i + 1) * 3600 for i in range(14)},
                                          "OLD": t - 31 * 86400})
        await seans.note_query(cfg, "NEWX")
        ex = await dbm.kv_get(seans.EXTRA_KV)
        assert len(ex) == 12 and "NEWX" in ex and "S10" in ex and not {"OLD", "S11", "S13"} & set(ex), sorted(ex)
        aset = await seans.archive_set(cfg)
        assert aset[:2] == ["XYZ100", "SP500"] and len(aset) == 14 and aset[2] == "NEWX", aset
        # arka plan turu: uygun olmayan sorulan sembol atlanır, sayılır
        await dbm.kv_set(seans.EXTRA_KV, {"GOLD": t, "NVDA": t - 60})
        out = await seans.refresh_all(cfg, cl)
        assert out["coins"] == ["XYZ100", "SP500", "NVDA"] and out["skipped"] == ["GOLD"] and out["err"] == 0, out
        print("✅ arşiv) ilk tam dolum + kuyruk; TTL içinde istek yok; oluşan mum yazılmaz; idempotent;"
              " artımlı −2 mum; istemcisiz arşivle; sorulan hisse 12/30 gün; tur uygun olmayanı atlar")
    asyncio.run(run())


def test_archive_all_separate_from_tabs():
    """🧪 Lab arşivi: PROPR ABD hisseleri sekmelerden AYRI arşivlenir; sekmeler değişmez; uygunsuz
    olanı resolve eler; tam dolum öncesi paylaşılan ağırlık penceresi yarıdan doluysa beklenir."""
    async def run():
        cfg = await _fresh()
        cfg.seans_archive_all = True
        got = await seans.archive_set(cfg)
        assert got[:2] == ["XYZ100", "SP500"] and seans.default_symbols(cfg) == ["XYZ100", "SP500"]
        assert {"NVDA", "KIOXIA", "SPCX"} <= set(got) and "GOLD" not in got and "THIN" not in got, got
        assert len(got) == len(set(got))

        class Busy:
            n = 0

            def low_paused(self):
                return 0

            def usage(self):
                Busy.n += 1
                return {"weight": 900 if Busy.n < 3 else 100, "weight_max": 1200}
        real_sleep, waits = asyncio.sleep, []

        async def fake_sleep(sec, *a, **k):
            waits.append(sec)
            await real_sleep(0)
        seans.asyncio.sleep = fake_sleep
        try:
            await seans._pace_full(Busy())
        finally:
            seans.asyncio.sleep = real_sleep
        assert waits == [5, 5], waits
        print("✅ lab arşivi) PROPR ABD hisseleri sekmelerden ayrı; emtia/PROPR dışı yok; tam dolum"
              " paylaşılan pencere yarıdan doluyken bekler")
    asyncio.run(run())


def test_loop_one_turn():
    """Arka plan turu: ısınma beklemesi → arşiv → kv seans_stats + nabız; kapalıyken yalnız nabız."""
    async def run():
        cfg = await _fresh()
        now_ts = dbm.now()
        cl = Client({c: walk(now_ts - 20 * 86400, upto(now_ts)) for c in ("xyz:XYZ100", "xyz:SP500")})
        real_sleep, waits = asyncio.sleep, []

        async def fake_sleep(sec, *a, **k):
            waits.append(sec)
            if sec >= seans.REFRESH_MIN:                 # tur sonu beklemesi → döngüden çık
                raise asyncio.CancelledError
            await real_sleep(0)
        for enabled in (True, False):
            cfg.seans_enabled = enabled
            waits.clear()
            seans.asyncio.sleep = fake_sleep
            try:
                await seans.loop(cfg, cl)
            except asyncio.CancelledError:
                pass
            finally:
                seans.asyncio.sleep = real_sleep
            st = await dbm.kv_get(seans.STATS_KV)
            assert waits == ([120, seans.FULL_GAP, seans.FULL_GAP, 3600] if enabled else [120, 3600]), waits
            if enabled:
                assert st["coins"] == ["XYZ100", "SP500"] and st["rows"] > 1500 and st["err"] == 0, st
            else:
                assert st.get("disabled") is True, st
        assert await dbm.kv_get("hb:seans"), "nabız"
        print("✅ döngü) 120 sn ısınma → iki sembol tam dolum (arada 20 sn), seans_stats + nabız → 3600 sn;"
              " kapalıyken yalnız durum")
    asyncio.run(run())


def test_card_text():
    async def run():
        from app.telegram import format as fmt
        cfg = await _fresh()
        now_ts = ts(2026, 10, 6, 14, 0)                                # Londra bitti, NY sürüyor
        raw = walk(ts(2026, 6, 1), upto(now_ts))
        cl = Client({"xyz:XYZ100": raw, "xyz:SP500": walk(ts(2026, 6, 1), upto(now_ts), seed=9)})
        views = [await seans.view(cfg, cl, s, now_ts=now_ts) for s in ("XYZ100", "SP500", "GOLD")]
        text = fmt.seans_card(views, base_url="https://x.app")
        for x in ("🕰 <b>ABD seans karnesi</b> · Sal 06.10 işlem günü",
                  "<i>Asya 23:00–10:00 · Londra 10:00–16:30 · New York 16:30–23:00 (TSİ)</i>",
                  "⏰ <i>Pzt 26.10 işlem gününden itibaren:", "<b>XYZ100</b> — şu an New York", "<b>SP500</b>",
                  "✓ Asya ", "✓ Londra ", "▶ New York ", "🧭 «", "💰 Getiri (toplam): Asya ",
                  "📐 Oynaklık: Asya aralığı → NY aralığı ρ", "🔗 https://x.app/seans?sym=XYZ100",
                  "<b>GOLD</b>: emtia / döviz / ABD dışı endeks", "karşılaştırma — şansla ~",
                  "geçmiş ölçüm, tahmin değil · yatırım tavsiyesi değildir"):
            assert x in text, (x, text)
        assert "key=" not in text and "<b>XYZ100</b>" in text
        assert not any(w in text.lower() for w in BANNED), [w for w in BANNED if w in text.lower()]
        assert fmt._pct_place(0) == ", arşivin en darı" and fmt._pct_place(100) == ", arşivin en genişi"
        # hafta sonu: tek satır "ABD seansı yok · sıradaki Asya"
        sat = ts(2026, 10, 10, 9, 0)
        cl2 = Client({"xyz:XYZ100": walk(ts(2026, 6, 1), upto(sat))})
        for cache in (seans._live, seans._karne_cache):
            cache.clear()
        w = await seans.view(cfg, cl2, "XYZ100", now_ts=sat)
        text = fmt.seans_card([w])
        assert "🌙 ABD seansı yok · sıradaki Asya <b>Paz 23:00</b> TSİ (Pzt 12.10 işlem günü)" in text, text
        assert "<b>XYZ100</b>\n🗓 Son tam gün Cum 09.10" in text and "ayrıntı: sayfada 🕰 seans" in text
        print("✅ kart) başlık + saatler + yaklaşan değişim; seans durumları; kural/getiri/oynaklık özeti;"
              " ret gerekçesi; çoklu test notu; linkte key yok; hafta sonu satırı")
    asyncio.run(run())


# ---------------- sayfa ----------------

async def _get(app, path, query=b""):
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query,
             "root_path": "", "headers": [(b"host", b"test")], "client": ("127.0.0.1", 1),
             "server": ("127.0.0.1", 80)}
    status, body = {}, []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            status["code"] = msg["status"]
        elif msg["type"] == "http.response.body":
            body.append(msg.get("body", b""))
    await app(scope, receive, send)
    return status.get("code"), b"".join(body).decode("utf-8", "replace")


def test_page_smoke():
    async def run():
        cfg = await _fresh()
        t = dbm.now()
        await seans.store_bars("xyz:XYZ100", seans.parse_bars(walk(t - 70 * 86400, upto(t)), t))
        from app.main import app
        app.state.cfg, app.state.client, app.state.session = cfg, None, None
        app.state.bot, app.state.notifier = None, None
        st, body = await _get(app, "/seans")
        assert st == 200, (st, body[-400:])
        for x in ("🕰 ABD seans karnesi", "📍 Bugün", "💰 Getiri hangi seansta birikti", "🧭 Londra / Asya → New York",
                  "📐 Oynaklık", "🎯 Günün dibi / tepesi", "ℹ️ Yöntem ve kör noktalar",
                  "Geçmiş ölçüm, tahmin değil", "Canlı mum alınamadı (canlı veri yok (istemci kapalı))",
                  'class="on">XYZ100</a>', 'href="/seans?sym=SP500"', "boş model"):
            assert x in body, x
        assert body.count('class="on"') >= 2                             # nav + sekme
        st, body = await _get(app, "/seans", b"sym=SP500")
        assert st == 200 and "arşivde henüz mum yok" in body and 'class="on">SP500</a>' in body
        st, body = await _get(app, "/seans", b"sym=GOLD")
        assert st == 200 and "emtia / döviz / ABD dışı endeks" in body and "Seans" in body
        st, body = await _get(app, "/seans", b"sym=ZZZZ")
        assert st == 200 and "evrende bulunamadı" in body
        # anahtar korunur (name="key"; orintu'daki k[3:] hatası yok); anahtarsız 401
        cfg.dashboard_token = "abc"
        st, body = await _get(app, "/seans", b"key=abc&sym=XYZ100")
        assert st == 200 and 'href="/seans?sym=SP500&amp;key=abc"' in body and 'name="key" value="abc"' in body
        assert 'name="k"' not in body.split("<main>")[1]
        st, _ = await _get(app, "/seans")
        assert st == 401
        cfg.dashboard_token = ""
        # görünüm patlarsa sayfa yine açılır, sebebi yazar
        real = seans.view

        async def boom(*a, **k):
            raise RuntimeError("deneme")
        seans.view = boom
        try:
            st, body = await _get(app, "/seans")
        finally:
            seans.view = real
        assert st == 200 and "hesaplanamadı — RuntimeError: deneme" in body
        st, body = await _get(app, "/tani", b"full=1")
        assert st == 200 and "seans karnesi: XYZ100" in body, body[:300]
        print("✅ sayfa) /seans (6 panel, istemcisiz uyarı), ?sym=SP500 arşiv yok, GOLD/ZZZZ gerekçe;"
              " anahtar linkleri; anahtarsız 401; hata 500 değil; /tani satırı")
    asyncio.run(run())


# ---------------- komut ----------------

def test_command_routing():
    async def run():
        cfg = await _fresh()
        cfg.telegram_chat_id, cfg.telegram_owner_id, cfg.account_chat_id = "111", "", "-500"
        cfg.public_base_url = "https://x.app"
        now_ts = ts(2026, 10, 6, 9, 0)
        cl = Client({c: walk(ts(2026, 6, 1), upto(now_ts), seed=i) for i, c in
                     enumerate(("xyz:XYZ100", "xyz:SP500", "xyz:NVDA"))})
        from app.telegram.bot import TelegramBot
        bot = TelegramBot(cfg, None, cl, {})
        sent = []

        async def fake_send(text, chat_id=None, reply_markup=None):
            sent.append((chat_id, text))
            return True
        bot.send = fake_send
        real_now = seans.now
        seans.now = lambda: now_ts
        try:
            await bot._handle_update({"message": {"chat": {"id": 111, "type": "private"},
                                                  "from": {"id": 111}, "text": "/seans"}})
            assert len(sent) == 1 and sent[0][0] == "111", sent
            text = sent[0][1]
            assert "<b>XYZ100</b> — şu an Londra" in text and "<b>SP500</b> — şu an Londra" in text
            assert "🔗 https://x.app/seans?sym=SP500" in text and "key=" not in text
            # hesap grubundan sahip olmayan üye → o gruba, tek sembol
            await bot._handle_update({"message": {"chat": {"id": -500, "type": "group"},
                                                  "from": {"id": 42}, "text": "/seans nvda"}})
            assert len(sent) == 2 and sent[1][0] == "-500" and "<b>NVDA</b>" in sent[1][1] and "XYZ100" not in sent[1][1]
            # yabancı grup → sessiz
            await bot._handle_update({"message": {"chat": {"id": -999, "type": "group"},
                                                  "from": {"id": 42}, "text": "/seans"}})
            assert len(sent) == 2
            # eş anlamlı + ret
            await bot._handle_update({"message": {"chat": {"id": 111, "type": "private"},
                                                  "from": {"id": 111}, "text": "/seanslar KIOXIA"}})
            assert "<b>KIOXIA</b>: dayanağı Asya borsasında" in sent[-1][1]
        finally:
            seans.now = real_now
        from app.telegram.format import help_text
        assert "/seans" in help_text()
        print("✅ komut) sahip: XYZ100 + SP500 tek mesaj, link anahtarsız; hesap grubundan üye /seans nvda;"
              " yabancı grup sessiz; /seanslar KIOXIA gerekçeli ret; yardımda")
    asyncio.run(run())


# ---------------- kablolama ----------------

def test_wiring():
    from app import diag, health
    from app.telegram import format as fmt
    src = lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read()   # noqa: E731
    assert '_spawn("seans", lambda: seans_loop(cfg, client), notifier)' in src("app/main.py")
    assert "CREATE TABLE IF NOT EXISTS seans_bars" in dbm.SCHEMA and "WITHOUT ROWID" in dbm.SCHEMA
    cfg = Config()
    assert cfg.seans_enabled is True and cfg.seans_coins == ["XYZ100", "SP500"] and cfg.seans_refresh_sec == 3600
    assert health.periods(cfg)["seans"] == 3600 and health.limits(cfg)["seans"] == 3600 * 3 + 600
    cfg.seans_refresh_sec = 60
    assert health.periods(cfg)["seans"] == 600, "en az 600"
    for f in ("seans_enabled", "seans_coins", "seans_refresh_sec"):
        meta = EDITABLE_FIELDS[f]
        assert meta["group"] == "🕰 ABD seans karnesi" and meta["label"] and len(meta["desc"]) > 40, f
    assert EDITABLE_FIELDS["seans_coins"]["type"] == "csv"
    assert "nl('/seans'" in src("app/web/templates/base.html")
    assert '@router.get("/seans")' in src("app/web/routes.py")
    readme = src("README.md")
    assert "## ABD Seans Karnesi: `/seans`" in readme and "| `/seans` · `/seans NVDA` |" in readme
    env = src(".env.example")
    assert "SEANS_COINS=XYZ100,SP500" in env and "SEANS_REFRESH_SEC=3600" in env
    assert fmt.TASK_TR["seans"] and "/seans" in fmt.help_text()
    assert "seans_stats" in src("app/diag.py") and hasattr(diag, "report")
    tpl = src("app/web/templates/seans.html")
    assert 'name="key" value="{{ k[5:] }}"' in tpl and "k[3:]" not in tpl and 'data-refresh="120"' in tpl
    print("✅ kablolama) spawn, tablo, sağlık (≥600 sn), 3 ayar künyeli, nav, rota, README, .env, yardım, tanı")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
