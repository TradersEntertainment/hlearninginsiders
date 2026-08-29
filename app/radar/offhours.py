"""Kapalı seans screener'ı — kapanıştan beri ne kadar saptı?

NEDEN ÖNEMLİ: HIP-3 hisse perp'i 7/24 işlem görür, dayanak hisse görmez. Borsa
kapalıyken oluşan fark bu yüzden SAF PERP/BALİNA AKIŞIDIR — kimse gerçek
hisseyle arbitraj yapıp fiyatı yerine oturtamaz. Yani buradaki sapma, "birileri
kapalıyken pozisyon kuruyor" sinyalinin en temiz hâli.

xyz dex HİÇ KAPANMAZ (7/24); kapanan ABD'dir. Ölçüm iki noktalıdır:
  ÇIPA    = son ABD kapanışındaki mark fiyatı (TSİ'de sabit saat, vars. 24:00)
  ÖLÇÜM   = şu an (ABD kapalıyken) ya da son seans açılışı (ABD açıkken)

Botun asıl penceresi HAFTA SONU: Cuma 24:00 TSİ → Pazartesi 00:00 TSİ. 48 saat
boyunca ABD tamamen kapalı, perp işlemeye devam ediyor.

Bilerek YAPMADIĞIMIZ şey: "geri dönerse şu kadar kazandırır" hesabı. Sapmanın
geçmişte gerçekten geri dönüp dönmediğini ölçmeden o cümle bir temenni olur,
veri değil. Sapmayı gösteriyoruz, kehaneti değil.
"""
import logging

from ..db import alert_log, alert_recent, db
from . import hourstats, metrics

log = logging.getLogger("radar.offhours")

# Çıpa örneği kapanıştan bu kadar eskiyse "bayat" sayılır: metrik görevi o
# sırada düşmüş olabilir ve sapma yanlış bir taban üstünden hesaplanır.
# Sessizce yanlış rakam göstermektense satırı işaretliyoruz.
STALE_ANCHOR_SEC = 1800
PLAYERS_LIMIT = 15


def _pct(new, old) -> float | None:
    try:
        new, old = float(new), float(old)
    except (TypeError, ValueError):
        return None
    return (new - old) / old * 100 if old else None


async def screener(cfg=None, limit: int | None = None) -> dict:
    """Her hisse için kapanış çıpasına göre sapma.

    ABD AÇIKKEN sayfa boş kalmaz: bir önceki kapalı pencerenin açılışa kadarki
    sapması gösterilir — "hafta sonu ne oldu" sorusu Pazartesi de geçerlidir.
    """
    ts = hourstats.now()
    h = int(getattr(cfg, "offhours_close_hour", hourstats.CLOSE_TSI_HOUR)) if cfg else None
    closed = hourstats.us_closed(ts, h)
    anchor = hourstats.last_close_ts(ts, h)
    weekend = hourstats.weekend_window(ts, h)
    # ABD kapalıyken şimdiye kadar; açıkken son kapanış→son açılış penceresi
    # ("gece/hafta sonu ne oldu" sorusu seans başladıktan sonra da geçerli).
    measure_ts = ts if closed else hourstats.last_regular_open_ts(ts)

    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol FROM tickers ORDER BY symbol")
        tickers = [dict(r) for r in await cur.fetchall()]
        # Açık pozisyon sayısı tek sorguda — coin başına sorgu açmıyoruz.
        cur = await conn.execute(
            "SELECT coin, COUNT(*) n, SUM(notional) ntl FROM positions_current"
            " GROUP BY coin")
        pos = {r["coin"]: (r["n"], r["ntl"] or 0) for r in await cur.fetchall()}

    from ..propr import is_listed as propr_listed
    rows, n_stale, n_nodata = [], 0, 0
    for t in tickers:
        coin = t["coin"]
        base = await metrics.metric_at(coin, anchor)
        cur_m = await metrics.metric_at(coin, measure_ts)
        if not base or not cur_m or not base.get("mark_px") or not cur_m.get("mark_px"):
            n_nodata += 1
            continue
        dev = _pct(cur_m["mark_px"], base["mark_px"])
        if dev is None:
            n_nodata += 1
            continue
        stale = (anchor - int(base["ts"] or 0)) > STALE_ANCHOR_SEC
        if stale:
            n_stale += 1
        n_pos, ntl = pos.get(coin, (0, 0))
        rows.append({
            "coin": coin, "symbol": t["symbol"], "propr": propr_listed(t["symbol"]),
            "base_px": base["mark_px"], "px": cur_m["mark_px"], "dev": dev,
            "oi_chg": _pct(cur_m.get("oi"), base.get("oi")),
            "volume": cur_m.get("day_volume") or 0,
            "n_pos": n_pos, "pos_ntl": ntl,
            "base_ts": int(base["ts"] or 0), "stale": stale,
        })
    # Varsayılan sıra: MUTLAK sapma — yönü fark etmeksizin "en çok kıpırdayan"
    # üstte olsun. Kullanıcı başlığa tıklayıp işaretli sıraya geçebiliyor
    # (base.html'deki global sortTable; ek JS yok).
    rows.sort(key=lambda r: -abs(r["dev"]))
    if limit:
        rows = rows[:limit]
    return {"rows": rows, "closed": closed, "anchor_ts": anchor,
            "measure_ts": measure_ts, "closed_for": max(0, ts - anchor),
            "weekend": weekend,                                      # (başlangıç, bitiş)
            "weekend_left": max(0, weekend[1] - ts) if weekend else 0,
            "next_open_ts": hourstats.next_open_ts(ts),              # pre-market
            "next_reg_ts": hourstats.next_regular_open_ts(ts),       # normal seans
            "n_stale": n_stale, "n_nodata": n_nodata, "n_tickers": len(tickers)}


async def players(anchor_ts: int, limit: int = PLAYERS_LIMIT) -> dict:
    """Kapanıştan BERİ açılmış pozisyonlar, yön ayrımlı.

    "Hafta sonu kimler oynuyor, shortla longla" sorusunun doğrudan cevabı.
    `opened_ts` yoksa `first_seen_ts`e düşülür — ikisi de yoksa satır girmez:
    ne zaman açıldığını bilmediğimiz pozisyonu "kapalıyken açıldı" saymak,
    listeyi eski pozisyonlarla doldururdu.
    """
    async with db() as conn:
        cur = await conn.execute(
            """SELECT p.coin, p.address, p.side, p.notional, p.score, p.leverage,
                      p.entry_px, COALESCE(p.opened_ts, p.first_seen_ts) op_ts,
                      t.symbol, a.watchlist, a.hits, a.misses
               FROM positions_current p
               LEFT JOIN tickers t ON t.coin = p.coin
               LEFT JOIN addresses a ON a.address = p.address
               WHERE COALESCE(p.opened_ts, p.first_seen_ts, 0) >= ?
               ORDER BY p.notional DESC LIMIT ?""", (int(anchor_ts), limit * 2))
        rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        r["symbol"] = r["symbol"] or (r["coin"] or "").split(":")[-1]
    return {"long": [r for r in rows if r["side"] == "long"][:limit],
            "short": [r for r in rows if r["side"] == "short"][:limit],
            "total": len(rows)}


async def check_alerts(cfg, notifier) -> dict:
    """Kapalı seans hareket bildirimleri. İKİ AYRI TETİK:

      1) KÜMÜLATİF SAPMA — çıpaya göre |sapma| her yeni `offhours_alert_pct`
         bandını geçtiğinde bir kez (%0.5 → %1.0 → %1.5…). Anahtarda ÇIPA var,
         yani yeni pencere sayaçları kendiliğinden sıfırlar; ayrı durum tablosu
         gerekmiyor.
      2) ANİ HAREKET — kısa pencerede (`offhours_spike_min`) gelen sıçrama.
         Coin başına sabit bekleme; bant mantığı burada işlemez çünkü sıçrama
         tekrarlanabilir bir olaydır.

    İkisi ayrı çünkü "hafta sonu boyunca yavaşça %0.8 saptı" ile "10 dakikada
    %1.2 sıçradı" farklı olaylar; birini diğerinin eşiğiyle ölçmek ikisini de
    kaçırır.

    YALNIZ ABD kapalıyken çalışır (açıkken %0.5 sürekli olur, bildirim
    gürültüye gömülür) ve YALNIZ PROPR'da listeli sembollere bakar (harekete
    geçemeyeceğin hisse için bildirim gürültüdür).
    """
    from ..propr import is_listed as propr_listed
    out = {"dev": 0, "spike": 0, "skipped": "" }
    if not hourstats.us_closed(None, ts_hour(cfg)):   # 1. param ref_ts, 2. saat
        out["skipped"] = "ABD açık"
        return out

    scr = await screener(cfg)
    if not scr["closed"]:                     # çıpa/durum tutarsızsa sus
        out["skipped"] = "ABD açık"
        return out
    band_pct = max(0.01, float(getattr(cfg, "offhours_alert_pct", 0.5)))
    spike_pct = max(0.01, float(getattr(cfg, "offhours_spike_pct", 1.0)))
    win = max(1, int(getattr(cfg, "offhours_spike_min", 10))) * 60
    cool = max(60, int(getattr(cfg, "offhours_spike_cooldown", 1800)))
    anchor, ts = scr["anchor_ts"], hourstats.now()

    for r in scr["rows"]:
        if not propr_listed(r["symbol"]):
            continue
        base = {"symbol": r["symbol"], "px": r["px"], "base_px": r["base_px"],
                "anchor_ts": anchor, "oi_chg": r.get("oi_chg"),
                "weekend_left": scr.get("weekend_left") or 0}

        # --- 1) kümülatif sapma bandı ---
        band = int(abs(r["dev"]) / band_pct)
        if band >= 1:
            # YÖN anahtara giriyor: +%1.1'den -%1.1'e savrulan bir hisse aynı
            # bant numarasına düşer ama bu iki AYRI olaydır — yön anahtarda
            # olmasaydı savrulma sessizce bastırılırdı.
            key = f"dev:{r['coin']}:{anchor}:{'+' if r['dev'] > 0 else '-'}{band}"
            if not await alert_recent("offhours", key, 30 * 86400):
                text = fmt_move({**base, "kind": "dev", "pct": r["dev"]})
                await notifier.send("offhours", text, priority="high", key=key)
                await alert_log("offhours", key, text)
                out["dev"] += 1

        # --- 2) ani hareket ---
        ref = await metrics.metric_at(r["coin"], ts - win)
        # Referans örnek ÇIPADAN ESKİYSE tetikleme: pencere seansın içine
        # taşmış demektir ve ölçülen şey "kapalıyken sıçrama" değil, seansın
        # normal oynaklığı olur.
        if not ref or int(ref.get("ts") or 0) < anchor or not ref.get("mark_px"):
            continue
        jump = _pct(r["px"], ref["mark_px"])
        if jump is None or abs(jump) < spike_pct:
            continue
        key = f"spike:{r['coin']}"
        if await alert_recent("offhours", key, cool):
            continue
        text = fmt_move({**base, "kind": "spike", "pct": jump,
                         "ref_px": ref["mark_px"], "window_min": win // 60})
        await notifier.send("offhours", text, priority="high",
                            key=f"{key}:{ts // cool}")
        await alert_log("offhours", key, text)
        out["spike"] += 1

    if out["dev"] or out["spike"]:
        log.info("kapalı seans bildirimi: %d sapma, %d ani hareket",
                 out["dev"], out["spike"])
    return out


def ts_hour(cfg):
    """cfg'den çıpa saati (yoksa modül varsayılanı)."""
    return None if cfg is None else int(
        getattr(cfg, "offhours_close_hour", hourstats.CLOSE_TSI_HOUR))


def fmt_move(m: dict) -> str:
    from ..telegram import format as fmt
    return fmt.offhours_move(m)
