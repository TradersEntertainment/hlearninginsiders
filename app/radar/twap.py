"""TWAP / düzenli birikim radarı — "birileri sessizce $5M topluyor".

TESPİT KENDİ KAYITLARIMIZDAN yapılır: aynı adres + coin + yönde, **düzenli
aralıklarla** ve **benzer boyutlarda** gelen fill dizisi. HL'nin yerel TWAP
emri işaretine bakmıyoruz — kendi botuyla dilimleyen biri de aynı şeyi yapıyor
ve aynı derecede ilginç. (Ayrıca o işareti buradan doğrulayamıyoruz.)

KÖR NOKTA — açıkça söylenmeli: yalnız YAKALAMA EŞİĞİNİN üstündeki dilimleri
görürüz. Eşiğin altına inen sabırlı bir TWAP tamamen görünmezdir ve
kaçırdığımızı bile bilemeyiz. Bu yüzden kullanıcı eşiği $5K'ya indirdi ve sayfa
bu körlüğü yazıyor.

`detect()` SAF ve TEK KAYNAK: `scorer._twap_fills` de bunu çağırır. İki ayrı
uygulama olsaydı biri düzeltilip diğeri unutulurdu ve insider skoru sessizce
sekmeden farklı davranırdı.
"""
import asyncio
import logging
import statistics

from .. import assets
from ..db import db, kv_get, kv_set, now

log = logging.getLogger("radar.twap")

MIN_SLICES = 5          # bundan az fill'de "düzenlilik" ölçülemez
CV_GAP_MAX = 0.35       # aralıkların değişkenlik katsayısı tavanı
CV_SIZE_MAX = 0.35      # dilim boyutlarının değişkenlik katsayısı tavanı
RETENTION_D = 30
# Son dilimden bu kadar süre geçmediyse tur HÂLÂ SÜRÜYOR sayılır (ortalama
# aralığın katı olarak). Süren bir TWAP, bitmiş olandan daha ilginçtir.
ACTIVE_GAPS = 3


def detect(rows: list[dict], *, cv_gap_max: float = CV_GAP_MAX,
           cv_size_max: float = CV_SIZE_MAX, min_slices: int = MIN_SLICES) -> dict | None:
    """Zaman sıralı fill dizisi → TWAP ölçümleri. Düzenli değilse None.

    `rows`: [{"ts", "sz", "notional"}] — artan zaman. Eşikler parametre:
    arşiv taraması ve skor varsayılanı kullanır; canlı radar HL'nin
    rastgeleleştirmesi için daha gevşek geçer (twaplive).
    """
    if len(rows) < min_slices:
        return None
    ts = [int(r["ts"]) for r in rows]
    sizes = [float(r["sz"] or 0) for r in rows]
    ntls = [float(r.get("notional") or 0) for r in rows]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    mg, ms = statistics.mean(gaps), statistics.mean(sizes)
    if mg <= 0 or ms <= 0:
        return None
    cv_gap = statistics.pstdev(gaps) / mg
    cv_size = statistics.pstdev(sizes) / ms
    if cv_gap >= cv_gap_max or cv_size >= cv_size_max:
        return None
    return {"n": len(rows), "first_ts": ts[0], "last_ts": ts[-1],
            "total": sum(ntls), "avg_slice": (sum(ntls) / len(rows)),
            "avg_gap": mg, "cv_gap": cv_gap, "cv_size": cv_size,
            "dur": ts[-1] - ts[0]}


def is_active(run: dict, ref_ts: int | None = None) -> bool:
    """Tur hâlâ sürüyor mu? Süren bir birikim bitmiş olandan daha ilginç."""
    ref = int(ref_ts) if ref_ts else now()
    gap = max(1.0, float(run.get("avg_gap") or 0))
    return (ref - int(run["last_ts"])) <= gap * ACTIVE_GAPS


def _pct(p: float) -> str:
    """Site yüzdesi (düz metin; Jinja kaçışlar): "%0" yazılmaz — %1 altı ondalıkla, %0.1 altı "<%0.1"."""
    p = float(p or 0)
    return f"%{p:.0f}" if p >= 1 else (f"%{p:.1f}" if p >= 0.1 else "<%0.1")


# HL emir geçmişinin BİTİŞ durumları ('stopped' de bitiştir: close_stale yazıyordu, sayfa "⌛ HL bitiş kaydı
# alınmadı" diyordu — inceleme 18.10, HL geçmişinde 113 örnek).
DONE_STATES = ("finished", "terminated", "error", "stopped")


def _seen_row(r: dict, ts: int) -> None:
    """DB satırı süren HL emrini taşıyorsa (HL 'activated' kaydı dolanı 0 yazar) radarın gördüğü HL dilimlerini ekle
    (fill_hl0 / fill_seen …) — canlı satırla ve Telegram'la aynı ölçü. Eskiden yalnız henüz kaydedilmemiş canlı satır
    alıyordu; kaydedilen süren emir sitede "bilinmiyor", Telegram'da "≈ %61" çıkıyordu (inceleme 18.10). Senkron."""
    if (r.get("order_status") or "") != "activated" or float(r.get("executed_usd") or 0) > 0 or not r.get("order_ts"):
        return
    try:
        from .twaplive import with_seen
        w = with_seen({"status": "activated", "executed_usd": 0, "started_ts": r["order_ts"],
                       "planned_usd": r.get("planned_usd"), "planned_sz": r.get("planned_sz")},
                      r["coin"], r["address"], r["side"], ts) or {}
    except Exception:                                  # noqa: BLE001 — süs; tablo düşmesin
        return
    for k in ("fill_hl0", "fill_seen", "fill_full", "fill_seen_sz"):
        if k in w:
            r[k] = w[k]


def fill_view(r: dict, ts: int | None = None) -> dict:
    """Web TWAP tablolarının Doldu / Kalan / Durum hücreleri — yalnız ÖLÇÜLEN (kullanıcı 18.10 ekran görüntüsü: bitmiş
    emirlerde "Doldu %0 · Kalan $2.99M · 🏁 bitti"; kural: "%0" yazılmaz, ölçülemeyen dolan yazılmaz, 2fdcb75).

    Kök neden (canlı doğrulandı): HL her durum için ayrı kayıt tutar; emir verildiği anki 'activated' kaydında dolan
    HEP 0'dır, gerçek dolan yalnız 'finished' / 'terminated' kaydında yazar. 0 bir ölçüm değildir:
      • HL bitiş/iptal kaydı → dolan HL'den (yüzde, ya da "dolmadı"); iptalde kalan = dolmayan; HL kaydı süren emirde
        dolanı yazmışsa (> 0) o da ölçümdür (Telegram fill_amount ile aynı)
      • süren emir, radar dilimleri görüyorsa (canlı satır: fill_seen) → "≈ %N" (emrin başından beri) ya da "≥ $X"
      • 'activated' ama HL bitişi henüz alınmadı → "bilinmiyor" (twaplive.close_stale sonradan HL'den çeker)
    Durum da ölçüme göre: 🏁 bitti yalnız HL 'finished' derse; süresi geçmiş 'activated' → ⌛, dilim gelmiyor → ⏸."""
    from ..telegram.format import usd
    ts = int(ts or now())
    st = (r.get("order_status") or "").lower()
    pu = float(r.get("planned_usd") or 0)
    eu = float(r.get("executed_usd") or 0)
    fill, tip, left = "—", "", "—"
    rem_v = r.get("remaining_usd")
    if pu > 0 and st in DONE_STATES:
        # Yüzde ADETLE (kalan, planla aynı fiyattan çevrili): $ oranı fiyat kaydıkça tam dolmuş emri "%101 / %98"
        # gösteriyordu, Telegram aynı emre adetle %100 diyor (inceleme 18.10). Kalan yoksa (eski satır) $ oranı.
        p = ((pu - float(rem_v)) / pu * 100) if rem_v is not None else (eu / pu * 100)
        fill = _pct(min(100.0, max(0.0, p))) if eu > 0 else "dolmadı"
        tip = f"HL bitiş kaydı: {usd(eu)} doldu" if eu > 0 else "HL bitiş kaydı: dolan yok"
        rem = float(r.get("remaining_usd") or 0)
        if st != "finished" and rem >= 1:
            left = usd(rem)
    elif pu > 0 and eu > 0:                             # HL kaydı dolanı yazmışsa (süren emirde de) ölçümdür
        fill, tip = _pct(eu / pu * 100), f"HL kaydı: {usd(eu)} doldu"
        rem = float(r.get("remaining_usd") or 0)
        left = usd(rem) if rem >= 1 else "—"
    elif pu > 0 and r.get("fill_hl0"):
        seen = r.get("fill_seen")
        if seen is None:
            fill, tip = "bilinmiyor", "HL süren emirde dolanı 0 yazar; radarın gördüğü dilim yok"
        elif r.get("fill_full"):
            psz, ssz = float(r.get("planned_sz") or 0), float(r.get("fill_seen_sz") or 0)
            p = (ssz / psz * 100) if (psz and ssz) else (float(seen) / pu * 100)
            fill, tip = "≈ " + _pct(p), f"radarın gördüğü HL dilimleri: {usd(seen)} (emrin başından beri)"
            left = "≈ " + usd(max(0.0, pu - float(seen)))
        else:
            fill, tip = "≥ " + usd(seen), "radar emrin başını görmedi — alt sınır"
    elif pu > 0:
        fill, tip = "bilinmiyor", "HL'nin emir anındaki kaydı dolanı 0 yazar; bitiş kaydı henüz alınmadı"
    end_ts = (int(r["order_ts"]) + int(float(r["order_min"]) * 60)) if r.get("order_ts") and r.get("order_min") else None
    if r.get("forming"):
        state, txt, stip = "forming", "📡 oluşuyor", "Dizi şu anda oluşuyor — henüz emir sorgusu yapılmadı, kayda geçmedi"
    elif r.get("running"):
        state, txt, stip = "running", "▶ sürüyor", "Son dilim yakın zamanda geldi ve emir kapanmadı"
    elif st in ("terminated", "error"):
        state, txt, stip = "cancelled", "⛔ iptal", "HL kaydı: iptal / hata ile bitti"
    elif st == "stopped":
        state, txt, stip = "halted", "⏹ durduruldu", "HL kaydı: emir durduruldu (stopped)"
    elif st == "finished":
        state, txt, stip = "finished", "🏁 bitti", "HL kaydı: tamamlandı"
    elif pu > 0 and end_ts and end_ts < ts:
        state, txt, stip = "expired", "⌛ süresi doldu", "Emrin süresi geçti; HL bitiş kaydı henüz alınmadı"
    elif pu > 0:
        state, txt, stip = "silent", "⏸ dilim gelmiyor", "HL emri açık görünüyor ama son dilimden beri sessiz"
    else:
        state, txt, stip = "stopped", "⏹ dizi durdu", "HL emri sorgulanamadı; dilimler durdu (bitti mi iptal mi bilinmiyor)"
    r.update(fill_txt=fill, fill_tip=tip, left_txt=left, state=state, state_txt=txt, state_tip=stip)
    return r


def merge_slices(rows: list[dict]) -> list[dict]:
    """Aynı saniyedeki satırlar TEK dilimdir (zaman sıralı satırlar → birleşik). Bir emir tek blokta birden çok
    karşı tarafla dolar: tabanı geçen parça + 'rest' kalan satırı (ya da iki büyük parça) aynı ts'i taşır; ayrı
    sayılınca 0 sn'lik aralık ve küçük boy düzenliliği bozuyor, gerçek TWAP'lar kayboluyordu (inceleme 18.10: canlı
    dilimlerle 9 dizinin 3'ü). taker: parçalardan biri agresörse 1."""
    out: list[dict] = []
    for r in rows:
        if out and out[-1]["ts"] == r["ts"]:
            m = out[-1]
            m["sz"] = float(m.get("sz") or 0) + float(r.get("sz") or 0)
            m["notional"] = float(m.get("notional") or 0) + float(r.get("notional") or 0)
            if r.get("taker") is not None:
                m["taker"] = max(int(m.get("taker") or 0), int(r["taker"]))
        else:
            out.append(dict(r))
    return out


async def _candidates(cfg) -> dict:
    """(coin, address, side) → fill dizisi. Pencere içindeki her şey."""
    hours = max(1, int(getattr(cfg, "twap_window_h", 12)))
    since = now() - hours * 3600
    async with db() as conn:
        cur = await conn.execute(
            """SELECT f.coin, f.address, f.side, f.ts, f.sz, f.notional, f.taker,
                      COALESCE(a.entity,'') entity
               FROM fills f LEFT JOIN addresses a ON a.address = f.address
               WHERE f.ts >= ? ORDER BY f.coin, f.address, f.side, f.ts""",
            (since,))
        rows = [dict(r) for r in await cur.fetchall()]
    out: dict = {}
    for r in rows:
        out.setdefault((r["coin"], r["address"], r["side"]),
                       {"entity": r["entity"], "rows": []})["rows"].append(r)
    return out


async def scan(cfg) -> dict:
    """Bir tur: pencere içindeki her (coin, adres, yön) dizisini sına.

    Tamamen YEREL — fills tablosu okunur, API isteği yoktur.
    """
    out = {"groups": 0, "detected": 0, "big": 0, "skipped_mm": 0,
           "best": None, "window_h": int(getattr(cfg, "twap_window_h", 12))}
    min_usd = float(getattr(cfg, "twap_min_usd", 5_000_000))
    out["min_usd"] = min_usd
    cands = await _candidates(cfg)
    out["groups"] = len(cands)
    ts = now()
    keep = []
    for (coin, addr, side), g in cands.items():
        # Market maker / vault düzenli işlem yapar — TWAP gibi görünür ama
        # sinyal değildir. `entity` zaten bu iş için var.
        if g["entity"] in ("mm", "vault"):
            out["skipped_mm"] += 1
            continue
        rows = merge_slices(g["rows"])
        d = detect(rows)
        if not d:
            continue
        out["detected"] += 1
        if d["total"] < min_usd:
            continue
        out["big"] += 1
        known = [r for r in rows if r.get("taker") is not None]
        tk = (sum(1 for r in known if r["taker"]) / len(known) * 100
              if known else None)
        keep.append((coin, addr, side, d["first_ts"], d["last_ts"], d["n"],
                     d["total"], d["avg_slice"], d["avg_gap"], d["cv_gap"],
                     d["cv_size"], tk, ts))
        if not out["best"] or d["total"] > out["best"]["total"]:
            out["best"] = {"coin": coin, "address": addr, "side": side,
                           "total": d["total"], "n": d["n"]}
    if keep:
        async with db() as conn:
            # Aynı PK'ya canlı radar da yazabilir: yalnız KENDİ kolonlarımız güncellenir,
            # canlı kolonları (src/alerted_ts/day_volume…) silinmez.
            await conn.executemany(
                """INSERT INTO twap_runs(coin,address,side,first_ts,
                     last_ts,n_slices,total,avg_slice,avg_gap,cv_gap,cv_size,
                     taker_pct,ts,src) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'fills')
                   ON CONFLICT(coin,address,side,first_ts) DO UPDATE SET
                     last_ts=excluded.last_ts, n_slices=excluded.n_slices, total=excluded.total,
                     avg_slice=excluded.avg_slice, avg_gap=excluded.avg_gap, cv_gap=excluded.cv_gap,
                     cv_size=excluded.cv_size, taker_pct=excluded.taker_pct, ts=excluded.ts,
                     src=COALESCE(src, excluded.src)""", keep)
        log.info("twap: %d grup, %d düzenli, %d büyük (≥%s)",
                 out["groups"], out["detected"], out["big"], min_usd)
    await kv_set("twap_stats", {**out, "ts": ts})
    return out


async def recent(limit: int = 80, hours: int = 48) -> list[dict]:
    """Sayfa: yakın zamanda görülen büyük TWAP turları, süren üstte."""
    async with db() as conn:
        cur = await conn.execute(
            """SELECT t.*, a.account_value, a.account_ts, a.watchlist, a.entity,
                      h.side pos_side, h.notional pos_notional
               FROM twap_runs t
               LEFT JOIN addresses a ON a.address = t.address
               LEFT JOIN hl_positions h ON h.address = t.address AND h.coin = t.coin
               WHERE t.last_ts >= ? ORDER BY t.total DESC LIMIT ?""",
            (now() - hours * 3600, limit))
        rows = [dict(r) for r in await cur.fetchall()]
    ts = now()
    # Aynı tur canlı ve arşivde farklı first_ts ile iki satır olabilir: (coin, adres,
    # yön) başına en çok dilimli olan kalır.
    best: dict[tuple, dict] = {}
    for r in rows:
        k = (r["coin"], r["address"], r["side"])
        if k not in best or int(r.get("n_slices") or 0) > int(best[k].get("n_slices") or 0):
            best[k] = r
    rows = list(best.values())
    for r in rows:
        r["active"] = is_active(r, ts) and not r.get("ended_ts")
        r["spot"] = assets.is_spot(r["coin"] or "")
        r["symbol"] = assets.label(r["coin"] or "")
        r["src"] = r.get("src") or "fills"
        dv, rd = r.get("day_volume"), r.get("rate_day")
        r["rate_pct"] = (float(rd) / float(dv) * 100) if dv and rd else None
        # HL TWAP emri (canlı radar sorguladıysa): plan, dolan %, kalan ($ · süre)
        pu, eu = r.get("planned_usd"), r.get("executed_usd")
        r["order_pct"] = (float(pu) / float(dv) * 100) if pu and dv else None
        r["filled_pct"] = (float(eu or 0) / float(pu) * 100) if pu else None
        left = None
        if pu and r.get("order_ts") and r.get("order_min") and not r.get("ended_ts") \
                and (r.get("order_status") or "activated") == "activated":
            left = int(r["order_ts"]) + int(float(r["order_min"]) * 60) - ts
        r["left_sec"] = max(0, left) if left is not None else None
        r["order_pct_txt"] = _pct(r["order_pct"]) if r["order_pct"] is not None else None
        if r["active"]:
            _seen_row(r, ts)
        v = fill_view({**r, "running": r["active"]}, ts)   # kopya: recent() satırına "running" eklenmesin
        r.update({k: v[k] for k in ("fill_txt", "fill_tip", "left_txt", "state", "state_txt", "state_tip")})
    rows.sort(key=lambda r: (not r["active"], -(r["total"] or 0)))
    return rows


# /twaplar sekmesi: pencere seçenekleri (etiket, saat) — 0 = sınırsız
WINDOWS = ((24, "24 saat"), (72, "3 gün"), (168, "7 gün"), (720, "30 gün"), (0, "hepsi"))
ORDERS_MAX = 500           # sayfa tavanı: tablo tarayıcıda da sıralanabilir olsun


def order_size(r: dict) -> float:
    """Sıralama ölçüsü: HL emrinin PLANLANAN toplamı. Emir sorgulanamadıysa
    ölçülen dilim toplamına düşer — uydurma yok, elimizdeki en iyi rakam."""
    try:
        pu = float(r.get("planned_usd") or 0)
    except (TypeError, ValueError):
        pu = 0.0
    if pu > 0:
        return pu
    try:
        return float(r.get("total") or 0)
    except (TypeError, ValueError):
        return 0.0


async def all_orders(hours: int = 168, status: str = "hepsi", market: str = "hepsi",
                     limit: int = ORDERS_MAX, coin: str = "") -> list[dict]:
    """/twaplar: görülen TÜM TWAP turları, emrin PLANLANAN boyutuna göre büyükten
    küçüğe. `recent()`ten farkı: pencere/durum/piyasa süzgeçleri, süren-önce
    sıralaması YOK (soru "en büyük emir hangisi") ve spot çiftleri de listede.

    status: hepsi | suren | bitmis      market: hepsi | perp | spot | hisse
    hours = 0 → pencere yok (tüm arşiv, `prune` neyi tuttuysa).
    `coin` verilirse yalnız o coin (coin sayfasının TWAP paneli) — PK
    (coin, address, side, first_ts) coin'le başlıyor, ek indeks gerekmez.
    """
    where, args = [], []
    if coin:
        where.append("t.coin = ?")
        args.append(coin)
    if hours:
        where.append("t.last_ts >= ?")
        args.append(now() - int(hours) * 3600)
    if market == "spot":
        where.append("t.coin LIKE '@%'")
    elif market == "perp":
        where.append("t.coin NOT LIKE '@%' AND instr(t.coin, ':') = 0")
    elif market == "hisse":
        where.append("instr(t.coin, ':') > 0")
    q = ("SELECT t.*, a.account_value, a.account_ts, a.watchlist, a.entity"
         " FROM twap_runs t LEFT JOIN addresses a ON a.address = t.address")
    if where:
        q += " WHERE " + " AND ".join(where)
    async with db() as conn:
        cur = await conn.execute(q, tuple(args))
        rows = [dict(r) for r in await cur.fetchall()]
    ts = now()
    # Aynı emir canlı + arşiv olarak iki satır olabilir (farklı first_ts):
    # (coin, adres, yön) başına en çok dilimli olan kalır — `recent()` ile aynı kural.
    best: dict[tuple, dict] = {}
    for r in rows:
        k = (r["coin"], r["address"], r["side"])
        if k not in best or int(r.get("n_slices") or 0) > int(best[k].get("n_slices") or 0):
            best[k] = r
    out = []
    names = await _spot_names()
    for r in best.values():
        coin = r["coin"] or ""
        r["spot"] = assets.is_spot(coin)
        r["symbol"] = names.get(coin) or assets.label(coin)
        r["src"] = r.get("src") or "fills"
        r["size_usd"] = order_size(r)
        r["planned_known"] = bool(r.get("planned_usd"))
        st = (r.get("order_status") or "").lower()
        done = bool(r.get("ended_ts")) or st in DONE_STATES
        r["running"] = (not done) and is_active(r, ts)
        r["cancelled"] = st in ("terminated", "error")
        pu, eu = r.get("planned_usd"), r.get("executed_usd")
        r["filled_pct"] = (float(eu or 0) / float(pu) * 100) if pu else None
        left = None
        if pu and r.get("order_ts") and r.get("order_min") and r["running"]:
            left = int(r["order_ts"]) + int(float(r["order_min"]) * 60) - ts
        r["left_sec"] = max(0, left) if left is not None else None
        if r["running"]:
            _seen_row(r, ts)
        fill_view(r, ts)
        if status == "suren" and not r["running"]:
            continue
        if status == "bitmis" and r["running"]:
            continue
        out.append(r)
    out.sort(key=lambda r: -r["size_usd"])
    return out[:limit]


async def _spot_names() -> dict:
    try:
        from ..hl.universe import spot_names
        return await spot_names()
    except Exception:
        return {}


COVERAGE_KV = "fills_coverage"


async def coverage() -> dict:
    """Fill arşivinin kapsamı — "neden boş" sorusunun cevabı burada.

    Arka planda (sweeper.refresh_fills_count, 10 dk'da bir) hesaplanıp kv'den okunur: istekte iki `LIKE '%:%'` sayımı
    10M satırlık fills'i baştan sona tarıyordu, /twap'ın sunucu süresinin ≈%95'i (canlı boyutta 1,7 sn, soğukta ≈5 sn;
    ölçüm 22.10). Henüz hesaplanmadıysa `pending` (sayfa "hesaplanıyor" der, "arşiv boş" demez)."""
    rec = await kv_get(COVERAGE_KV)
    if not rec:
        return {"crypto": {"n": None, "a": None}, "equity": {"n": None, "a": None}, "pending": True}
    return {"crypto": rec.get("crypto") or {"n": 0, "a": None}, "equity": rec.get("equity") or {"n": 0, "a": None},
            "ts": rec.get("ts")}


async def compute_coverage() -> dict:
    """Tek tarama (coin başına sayım + en eski) → kripto / hisse ayrımı Python'da. Arka plan işi."""
    async with db() as conn:
        cur = await conn.execute("SELECT coin, COUNT(*) n, MIN(ts) a FROM fills GROUP BY coin")
        rows = [dict(r) for r in await cur.fetchall()]
    out = {"crypto": {"n": 0, "a": None}, "equity": {"n": 0, "a": None}}
    for r in rows:
        k = out["equity" if ":" in str(r["coin"]) else "crypto"]
        k["n"] += int(r["n"] or 0)
        if r["a"] is not None and (k["a"] is None or int(r["a"]) < k["a"]):
            k["a"] = int(r["a"])
    out["ts"] = now()
    await kv_set(COVERAGE_KV, out)
    return out


async def prune(days: int = RETENTION_D) -> int:
    async with db() as conn:
        cur = await conn.execute("DELETE FROM twap_runs WHERE last_ts < ?",
                                 (now() - days * 86400,))
        return cur.rowcount or 0


async def loop(cfg) -> None:
    """Denetimli döngü. Site ASLA buna bağımlı değil."""
    from ..health import beat
    await asyncio.sleep(200)
    while True:
        try:
            await beat("twap")
            await scan(cfg)
            await prune()
            await beat("twap")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("twap turu hatası")
        await asyncio.sleep(max(120, int(getattr(cfg, "twap_scan_sec", 600))))
