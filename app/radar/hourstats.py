"""Saat istatistiği — hisse hangi saatte yükseliyor, hangi saatte düşüyor?

Kullanıcı gözlemi ("overnight effect"): bazı hisselerde (MU, NVDA) getirinin
çoğu ABD borsası KAPALIYKEN gelir. HIP-3 perp'ler 7/24 işlem gördüğü için bu
ölçülebilir: 90 günlük 1 saatlik mumlardan saat-of-day getiri haritası +
borsa açık/kapalı seans ayrımı çıkarılır. Tamamen site özelliği — Telegram
bildirimi üretmez.

Veri: HL candleSnapshot (coin başına günde 1 istek — bütçede önemsiz).
"""
import asyncio
import json
import logging
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from ..config import Config
from ..db import db, kv_get, kv_set, now
from ..hl.client import HLClient

log = logging.getLogger("radar.hourstats")

ET = ZoneInfo("America/New_York")
TR = ZoneInfo("Europe/Istanbul")

# HL'nin xyz dex'i HİÇ KAPANMAZ (7/24). Kapanan ABD borsasıdır — kapalı seans
# sapması da tam bu yüzden anlamlıdır: perp işlemeye devam ederken dayanak
# hisse durur, aradaki fark saf perp/balina akışıdır.
#
# KAPANIŞ ÇIPASI TSİ'DE SABİT BİR SAATTİR (varsayılan 24:00 = gece yarısı).
# Kullanıcının gözlemi bu; kışın tam olarak ET 16:00 kapanışına denk gelir,
# yazın bir saat kayar. ET'ye bağlamak yerine TSİ'de sabitliyoruz ÇÜNKÜ
# kullanıcı böyle görüyor ve ayardan değiştirilebilir — yanlışsa kod değil
# ayar değişir. Çıpa zamanı ayrıca ekranda yazıyor.
#
# DİKKAT: `compute_stats` içindeki `9 <= dt.hour < 16` yaklaşıklaması BİLEREK
# ayrı duruyor. O, 1 saatlik mumları kovalıyor (09:00 mumunun yarısı kapalıdır)
# ve 90 günlük istatistikleri üretiyor; dakika hassasiyetine çevirmek kv'deki
# tüm hstats kayıtlarını sessizce değiştirirdi. İki farklı soru, iki fonksiyon.
MKT_OPEN = dtime(9, 30)          # ABD normal seans açılışı (ET)
MKT_CLOSE = dtime(16, 0)         # ABD normal seans kapanışı (ET)
EXT_OPEN = dtime(4, 0)           # pre-market başlangıcı (ET)
EXT_CLOSE = dtime(20, 0)         # after-hours bitişi (ET)
CLOSE_TSI_HOUR = 0               # kapalı seans çıpası: TSİ saati (0 = 24:00)


def _et(ref_ts: int | None = None) -> datetime:
    return datetime.fromtimestamp(int(ref_ts) if ref_ts else now(), ET)


def is_market_open(ref_ts: int | None = None) -> bool:
    """NORMAL seans (09:30–16:00 ET) açık mı."""
    d = _et(ref_ts)
    return d.weekday() < 5 and MKT_OPEN <= d.time() < MKT_CLOSE


def is_equity_tradable(ref_ts: int | None = None) -> bool:
    """Hisse HERHANGİ bir şekilde işlem görüyor mu (pre-market + seans +
    after-hours, 04:00–20:00 ET).

    Kapalı seans screener'ının sorduğu soru budur: perp gerçekten koptu mu?
    Normal seans kapalı ama after-hours açıkken kopmuş sayılmaz.

    Tatiller HESABA KATILMAZ — tatil takvimimiz yok. Bir tatil gününde bu
    fonksiyon "işlem görüyor" der ve çıpa yanlış seçilir. Uydurmak yerine çıpa
    zamanını ekranda gösteriyoruz ki yanlışsa görünsün.
    """
    d = _et(ref_ts)
    return d.weekday() < 5 and EXT_OPEN <= d.time() < EXT_CLOSE


def _walk(d: datetime, at: dtime, back: bool) -> int:
    for i in range(0, 9):
        day = (d + timedelta(days=-i if back else i)).date()
        cand = datetime.combine(day, at, ET)
        if day.weekday() < 5 and ((cand <= d) if back else (cand > d)):
            return int(cand.timestamp())
    return int(d.timestamp())          # ulaşılamaz; sessiz None'dan iyidir


def last_close_ts(ref_ts: int | None = None, tsi_hour: int | None = None) -> int:
    """En son ABD kapanışı — kapalı seans sapmasının ÇIPASI.

    TSİ'de sabit saat (varsayılan gece yarısı = "Cuma 24:00"). Hafta sonu
    boyunca Cuma'nınkinde kalır: Cumartesi ve Pazar gece yarıları bir ABD
    seansını KAPATMIYOR, o yüzden çıpa olamazlar.
    """
    h = CLOSE_TSI_HOUR if tsi_hour is None else int(tsi_hour) % 24
    d = datetime.fromtimestamp(int(ref_ts) if ref_ts else now(), TR)
    for back in range(0, 9):
        day = (d - timedelta(days=back)).date()
        cand = datetime.combine(day, dtime(h), TR)
        # Bu kapanış hangi ABD gününe ait? Gece yarısı civarı bir kapanış,
        # BİR ÖNCEKİ günün seansını kapatır.
        sess_day = day - timedelta(days=1) if h < 12 else day
        if cand <= d and sess_day.weekday() < 5:
            return int(cand.timestamp())
    return int(d.timestamp())          # ulaşılamaz; sessiz None'dan iyidir


def last_regular_open_ts(ref_ts: int | None = None) -> int:
    """En son NORMAL seans açılışı (ET 09:30)."""
    return _walk(_et(ref_ts), MKT_OPEN, back=True)


def us_closed(ref_ts: int | None = None, tsi_hour: int | None = None) -> bool:
    """ABD borsası kapalı mı.

    "Son kapanıştan sonrayız" DEĞİL, "son kapanış son açılıştan daha yeni"
    diye sorulur. İlk hâli Pazartesi seans içinde bile 'kapalı' diyordu:
    hafta sonu çıpası duruyor ama sıradaki açılış Salı'ya kayıyordu.

    (xyz dex'in kendisi HİÇ kapanmaz — kapanan ABD. O yüzden 'piyasa kapalı'
    değil 'ABD kapalı' diyoruz.)
    """
    ts = int(ref_ts) if ref_ts else now()
    return last_close_ts(ts, tsi_hour) > last_regular_open_ts(ts)


def weekend_window(ref_ts: int | None = None,
                   tsi_hour: int | None = None) -> tuple[int, int] | None:
    """Hafta sonu penceresi (başlangıç, bitiş) — içinde değilsek None.

    Botun asıl işi bu pencere: Cuma 24:00 TSİ'den Pazartesi 00:00 TSİ'ye.
    48 saat boyunca ABD tamamen kapalı, xyz dex ise işlemeye devam ediyor.
    """
    h = CLOSE_TSI_HOUR if tsi_hour is None else int(tsi_hour) % 24
    ts = int(ref_ts) if ref_ts else now()
    d = datetime.fromtimestamp(ts, TR)
    for back in range(0, 8):
        day = (d - timedelta(days=back)).date()
        if day.weekday() != 5:            # pencere CUMARTESİ başlar
            continue
        start = datetime.combine(day, dtime(h), TR)
        end = start + timedelta(days=2)   # Pazartesi aynı saat
        if start.timestamp() <= ts < end.timestamp():
            return int(start.timestamp()), int(end.timestamp())
        break
    return None


def next_open_ts(ref_ts: int | None = None) -> int:
    """Hissenin yeniden işlem göreceği an (ET 04:00, pre-market başlangıcı)."""
    return _walk(_et(ref_ts), EXT_OPEN, back=False)


def next_regular_open_ts(ref_ts: int | None = None) -> int:
    """Sıradaki NORMAL seans açılışı (ET 09:30) — "asıl açılış ne zaman"."""
    return _walk(_et(ref_ts), MKT_OPEN, back=False)


MIN_N = 40         # bir saat kovasının "anlamlı" sayılması için asgari örnek
HOT_MEAN = 0.10    # güçlü saat: ortalama getiri eşiği (%/saat)
HOT_WIN = 55.0     # güçlü saat: kazanma oranı eşiği (%)
MIN_CANDLES = 24 * 20  # en az ~20 günlük veri yoksa istatistik üretme
REFRESH_SEC = 600  # döngü periyodu
PER_CYCLE = 2      # her turda yenilenecek coin sayısı
STALE_SEC = 24 * 3600
# Çıktı şeması sürümü. Artınca kv'deki eski kayıtlar BAYAT sayılır ve rotasyon
# onları yeniden hesaplar — yoksa yeni alanlar aylarca boş kalırdı.
HSTATS_V = 2

_inflight: set[str] = set()


def parse_candles(raw) -> list[dict]:
    out = []
    if not isinstance(raw, list):
        return out
    for c in raw:
        try:
            out.append({"t": int(c["t"]) // 1000,
                        "o": float(c["o"]), "c": float(c["c"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def compute_stats(candles: list[dict]) -> dict | None:
    """Saat haritası + borsa açık/kapalı seans ayrımı. Yetersiz veri → None."""
    if len(candles) < MIN_CANDLES:
        return None
    hours = [{"n": 0, "s": 0.0, "w": 0} for _ in range(24)]
    open_c = closed_c = 1.0
    # Yön ayrımı: net getiri "%20 çıkıp %18 düştü" ile "%2 tek yönde gitti"yi
    # aynı gösteriyor. Yukarı ve aşağı hareketi AYRI bileşikliyoruz.
    up = {"open": 1.0, "closed": 1.0}
    dn = {"open": 1.0, "closed": 1.0}
    n_sess = {"open": 0, "closed": 0}
    t_min = t_max = candles[0]["t"]
    for c in candles:
        if c["o"] <= 0:
            continue
        r = c["c"] / c["o"] - 1
        if abs(r) > 0.5:
            continue  # bozuk mum
        dt = datetime.fromtimestamp(c["t"], ET)
        b = hours[dt.hour]
        b["n"] += 1
        b["s"] += r
        b["w"] += 1 if r > 0 else 0
        # NYSE normal seansı ~09:30-16:00 ET; 1h mumda 9-16 başlangıçları
        # "açık" sayılır (09:00 mumunun yarısı kapalıdır — yaklaşıklama)
        sess = "open" if (dt.weekday() < 5 and 9 <= dt.hour < 16) else "closed"
        n_sess[sess] += 1
        if sess == "open":
            open_c *= 1 + r
        else:
            closed_c *= 1 + r
        if r > 0:
            up[sess] *= 1 + r
        elif r < 0:
            dn[sess] *= 1 + r
        t_min, t_max = min(t_min, c["t"]), max(t_max, c["t"])

    ref = datetime.fromtimestamp(t_max, ET)
    out_hours = []
    for h, b in enumerate(hours):
        tsi = ref.replace(hour=h, minute=0).astimezone(TR).hour
        out_hours.append({
            "et": h, "tsi": tsi,
            "avg": (b["s"] / b["n"] * 100) if b["n"] else 0.0,
            "win": (b["w"] / b["n"] * 100) if b["n"] else 0.0,
            "n": b["n"],
        })
    ranked = [h for h in out_hours if h["n"] >= MIN_N]
    best = sorted(ranked, key=lambda x: -x["avg"])[:3]
    worst = sorted(ranked, key=lambda x: x["avg"])[:3]
    return {
        "hours": out_hours,
        "open_ret": (open_c - 1) * 100, "closed_ret": (closed_c - 1) * 100,
        # seans × yön: borsa kapalıyken ne kadar çıktı / ne kadar düştü
        "open_up": (up["open"] - 1) * 100, "open_dn": (dn["open"] - 1) * 100,
        "closed_up": (up["closed"] - 1) * 100, "closed_dn": (dn["closed"] - 1) * 100,
        "n_open": n_sess["open"], "n_closed": n_sess["closed"],
        "days": round((t_max - t_min) / 86400),
        "best": best, "worst": worst,
        "v": HSTATS_V,
        "ts": now(),
    }


def verdict(stats: dict, et_hour: int) -> tuple[str, dict]:
    """'güçlü' / 'zayıf' / 'nötr' — şu saatin tarihsel karnesi."""
    b = stats["hours"][et_hour % 24]
    if b["n"] >= MIN_N and b["avg"] >= HOT_MEAN and b["win"] >= HOT_WIN:
        return "güçlü", b
    if b["n"] >= MIN_N and b["avg"] <= -HOT_MEAN and b["win"] <= 100 - HOT_WIN:
        return "zayıf", b
    return "nötr", b


def hour_ranking(stats_map: dict[str, dict], et_hour: int,
                 limit: int | None = None, min_n: int = MIN_N) -> dict:
    """Bu saatin tarihsel SIRALAMASI — eşikle kesmez.

    Eskiden yalnız `hot_now()` vardı ve o bir sıralama değil bir FİLTREydi:
    "güçlü" eşiğini (n/ortalama/kazanç) geçmeyen hiç görünmüyordu. Hiçbiri
    geçmediğinde panel tamamen boş kalıyor, özellik yokmuş gibi duruyordu.
    Oysa sorulan şey sıralama: "şu saatte en çok hangisi yükselir".

    Burada `verdict()` ELEMEK için değil ETİKETLEMEK için çağrılıyor: her satır
    kendi damgasını taşır, arayüz güçlüyü rozetler, zayıfı zayıf gösterir.

    Örneklemi ince olanlar (n < min_n) sıralamaya girmez ama SAYILIR (`thin`):
    "liste kısa çünkü veri yok" ile "liste kısa çünkü öyle" ayrı şeylerdir ve
    kullanıcı hangisi olduğunu görebilmeli.
    """
    rows, thin, n_strong = [], 0, 0
    for coin, s in stats_map.items():
        if not s or s.get("empty"):
            continue
        v, b = verdict(s, et_hour)
        if (b.get("n") or 0) < min_n:
            thin += 1
            continue
        if v == "güçlü":
            n_strong += 1
        rows.append({"coin": coin, "avg": b["avg"], "win": b["win"], "n": b["n"],
                     "verdict": v,
                     "closed_heavy": s["closed_ret"] > s["open_ret"],
                     "closed_ret": s["closed_ret"], "open_ret": s["open_ret"]})
    up = sorted((r for r in rows if r["avg"] > 0), key=lambda x: -x["avg"])
    down = sorted((r for r in rows if r["avg"] < 0), key=lambda x: x["avg"])
    if limit:
        up, down = up[:limit], down[:limit]
    return {"up": up, "down": down, "n_stats": len(rows),
            "n_strong": n_strong, "thin": thin}


def hot_now(stats_map: dict[str, dict], et_hour: int) -> list[dict]:
    """ŞU SAATİ tarihsel olarak güçlü olan hisseler.

    `hour_ranking()` üstünde ince bir süzgeç: tek sıralama yolu olsun, aynı
    mantık iki yerde durmasın. Anlamı değişmedi — "güçlü" hâlâ eşiği geçendir.
    """
    return [r for r in hour_ranking(stats_map, et_hour)["up"]
            if r["verdict"] == "güçlü"]


def _share(closed: float, opened: float) -> float | None:
    """Hareketin yüzde kaçı KAPALI seansta oldu. İkisi de sıfırsa None —
    'hiç hareket yok' ile '%0 kapalıda' aynı şey değil, hücre '—' göstersin."""
    total = abs(closed) + abs(opened)
    if total <= 0:
        return None
    return abs(closed) / total * 100


def session_ranking(stats_map: dict[str, dict],
                    sym_map: dict[str, str] | None = None) -> list[dict]:
    """Seans karnesi: hangi hisse ne kadarını borsa KAPALIYKEN yapmış.

    HIP-3 perp'i 7/24 işlem görür, hisse senedi kapalıdır: kapalı seanstaki
    hareket saf perp/balina kaynaklıdır. Yükseliş ve düşüş AYRI verilir —
    net getiri "%20 çıkıp %18 düştü"yü "%2 gitti" gibi gösteriyordu.

    Kapalı seans açıktan ~4 kat uzun olduğu için toplamın yanında saat başına
    ortalama da hesaplanır; kıyas ancak öyle dürüst olur.

    Henüz yeni şemayla tazelenmemiş kayıtlar ATLANIR (yarım veriyle yanlış
    sıralama üretmektense listede hiç görünmesin).
    """
    sym_map = sym_map or {}
    out = []
    for coin, st in (stats_map or {}).items():
        if not st or st.get("empty") or int(st.get("v") or 0) < HSTATS_V:
            continue
        n_cl, n_op = int(st.get("n_closed") or 0), int(st.get("n_open") or 0)
        cu, cd = float(st.get("closed_up") or 0), float(st.get("closed_dn") or 0)
        ou, od = float(st.get("open_up") or 0), float(st.get("open_dn") or 0)
        out.append({
            "coin": coin,
            "symbol": sym_map.get(coin) or coin.split(":")[-1].upper(),
            "closed_up": cu, "closed_dn": cd, "open_up": ou, "open_dn": od,
            "closed_ret": float(st.get("closed_ret") or 0),
            "open_ret": float(st.get("open_ret") or 0),
            "up_share": _share(cu, ou), "dn_share": _share(cd, od),
            # saat başına ortalama — seans uzunluğu farkını nötrler
            "rate_closed_up": (cu / n_cl) if n_cl else 0.0,
            "rate_open_up": (ou / n_op) if n_op else 0.0,
            "rate_closed_dn": (cd / n_cl) if n_cl else 0.0,
            "rate_open_dn": (od / n_op) if n_op else 0.0,
            "closed_heavy": float(st.get("closed_ret") or 0) > float(st.get("open_ret") or 0),
            "n_closed": n_cl, "n_open": n_op,
            "days": int(st.get("days") or 0),
        })
    # Varsayılan: yükselişinin en çok kapalı seansta oluştuğu hisse başta
    out.sort(key=lambda r: (-(r["up_share"] if r["up_share"] is not None else -1),
                            -r["closed_up"]))
    return out


async def channel_entries(sym_map: dict[str, str], et_hour: int,
                          coins: list[str] | None = None,
                          limit: int = 8) -> list[dict]:
    """Kanala yayınlanacak "şu saat güçlü" satırları. Hem panelden elle gönderim
    hem otomatik yayın bunu kullanır — verdict GÖNDERİM ANINDA yeniden kontrol
    edilir (sayfa 13:5x'te açılıp 14:0x'te gönderilirse eski saat yayınlanmasın)."""
    hmap = await all_stats()
    want = coins if coins is not None else list(hmap)
    out = []
    for coin in want:
        st = hmap.get(coin)
        if not st or st.get("empty"):
            continue
        v, b = verdict(st, et_hour)
        if v != "güçlü":
            continue
        out.append({"coin": coin, "symbol": sym_map.get(coin, coin.split(":")[-1]),
                    "avg": b["avg"], "win": b["win"], "n": b["n"],
                    "closed_heavy": st["closed_ret"] > st["open_ret"]})
    out.sort(key=lambda x: -x["avg"])
    return out[:limit]


async def all_stats(only_universe: bool = True) -> dict[str, dict]:
    """kv'deki hazır istatistikler: coin -> stats. only_universe=True iken
    evrende OLMAYAN (exclude/delist) coin'lerin hayalet kaydını eler — yoksa
    exclude edilen hisse aylarca 'Saati gelenler'de donmuş rakamla kalıyor,
    linki 'bulunamadı'ya gidiyor, kanala bile yayınlanabiliyordu."""
    out = {}
    async with db() as conn:
        valid = None
        if only_universe:
            cur = await conn.execute("SELECT coin FROM tickers")
            valid = {r["coin"] for r in await cur.fetchall()}
        cur = await conn.execute("SELECT k, v FROM kv WHERE k LIKE 'hstats:%'")
        for r in await cur.fetchall():
            coin = r["k"][7:]
            if valid is not None and coin not in valid:
                continue
            try:
                out[coin] = json.loads(r["v"])
            except (ValueError, TypeError):
                continue
    return out


async def refresh_coin(cfg: Config, client: HLClient, coin: str) -> dict | None:
    end_ms = now() * 1000
    # hourstats_days panelden MIN_CANDLES/20 günün altına çekilirse özellik
    # sessizce ölüyordu (compute_stats None döner). En az 20 güne kelepçele.
    days = max(int(cfg.hourstats_days), MIN_CANDLES // 24)
    start_ms = end_ms - days * 86400 * 1000
    try:
        raw = await client.candles(coin, "1h", start_ms, end_ms)
    except Exception as e:
        log.debug("candles %s: %s", coin, e)
        # Hata da olsa kv'ye taze ts yaz — yoksa alfabetik baştaki 2 bozuk coin
        # (ör. delist) her turda yeniden denenip TÜM rotasyonu kilitliyordu.
        await kv_set(f"hstats:{coin}", {"empty": True, "error": True, "ts": now()})
        return None
    stats = compute_stats(parse_candles(raw))
    await kv_set(f"hstats:{coin}", stats or {"empty": True, "ts": now()})
    if stats:
        log.info("saat istatistiği hazır: %s (%d gün, en iyi saat TSİ %02d:00)",
                 coin.split(":")[-1], stats["days"],
                 stats["best"][0]["tsi"] if stats["best"] else -1)
    return stats


def kick(cfg: Config, client: HLClient, coin: str) -> None:
    """Coin sayfası açıldığında istatistik yoksa arka planda hazırla."""
    if coin in _inflight:
        return

    async def run():
        try:
            await refresh_coin(cfg, client, coin)
        finally:
            _inflight.discard(coin)

    _inflight.add(coin)
    asyncio.create_task(run())


async def refresh_loop(cfg: Config, client: HLClient) -> None:
    await asyncio.sleep(150)
    log.info("saat istatistiği döngüsü başladı (%d coin / %ds)", PER_CYCLE, REFRESH_SEC)
    while True:
        try:
            from ..health import beat
            await beat("hourstats")
            async with db() as conn:
                cur = await conn.execute("SELECT coin FROM tickers ORDER BY coin")
                coins = [r["coin"] for r in await cur.fetchall()]
            if coins:
                have = await all_stats()
                ts = now()
                todo = [c for c in coins
                        if not have.get(c)
                        or int(have[c].get("v") or 0) < HSTATS_V     # eski şema
                        or ts - int(have[c].get("ts") or 0) > STALE_SEC]
                for coin in todo[:PER_CYCLE]:
                    await refresh_coin(cfg, client, coin)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("saat istatistiği hatası")
        await asyncio.sleep(REFRESH_SEC)
