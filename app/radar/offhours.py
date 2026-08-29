"""Kapalı seans screener'ı — kapanıştan beri ne kadar saptı?

NEDEN ÖNEMLİ: HIP-3 hisse perp'i 7/24 işlem görür, dayanak hisse görmez. Borsa
kapalıyken oluşan fark bu yüzden SAF PERP/BALİNA AKIŞIDIR — kimse gerçek
hisseyle arbitraj yapıp fiyatı yerine oturtamaz. Yani buradaki sapma, "birileri
kapalıyken pozisyon kuruyor" sinyalinin en temiz hâli.

Ölçüm iki noktalıdır:
  ÇIPA    = hissenin en son işlem gördüğü andaki mark fiyatı (ET 20:00)
  ÖLÇÜM   = şu an (kapalıyken) ya da pre-market açılışı (hisse işlem görüyorken)

"Kapanış" 16:00 DEĞİLDİR: 16:00–20:00 arası after-hours'ta hisse hâlâ işlem
görür, perp ona tutunabilir. Perp'in gerçekten koptuğu pencere 20:00'de başlar.

Bilerek YAPMADIĞIMIZ şey: "geri dönerse şu kadar kazandırır" hesabı. Sapmanın
geçmişte gerçekten geri dönüp dönmediğini ölçmeden o cümle bir temenni olur,
veri değil. Sapmayı gösteriyoruz, kehaneti değil.
"""
import logging

from ..db import db
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


async def screener(limit: int | None = None) -> dict:
    """Her hisse için kapanış çıpasına göre sapma.

    Piyasa AÇIKKEN sayfa boş kalmaz: bir önceki kapalı seansın AÇILIŞA kadarki
    sapması gösterilir — "gece ne oldu" sorusu sabah da geçerlidir.
    """
    ts = hourstats.now()
    # "Kapalı" = hisse HİÇBİR şekilde işlem görmüyor (after-hours dahil bitmiş).
    closed = not hourstats.is_equity_tradable(ts)
    anchor = hourstats.last_close_ts(ts)
    # Kapalıyken şimdiye kadar; hisse işlem görüyorken bir önceki kapalı
    # pencerenin tamamı (after-hours bitişi → pre-market açılışı).
    measure_ts = ts if closed else hourstats.next_open_ts(anchor)

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
