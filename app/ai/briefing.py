"""Brifing — modele giden kompakt veri özeti.

İstatistiği BURASI hesaplar, model değil. Bir LLM sayı toplamada kötü, örüntü
önermede iyidir; ham satır verip "hesapla" demek uydurma üretir. Model
hazır rakamları okur ve yalnız yorum yapar.

Brifingin değerli yarısı, sitede HİÇ GÖSTERMEDİĞİMİZ veridir:
  · asset_metrics'in 45 günlük serisi (site yalnız son değeri gösteriyor)
  · fills.taker — agresörlük dengesi (skorda var, ekranda yok)
  · alerts_log — neyi ne zaman bildirdik (botun kendi gürültü kaydı)
  · book_walls'un pasif satırları — çekilen duvar (spoof) geçmişi
  · hl_positions yaşam döngüsü (zirve → kapanış)
  · wallet_links — adres kümeleme, hiçbir sayfada yok
  · liq_watch.stage — likidasyona tırmanma geçmişi
  · tickers.listed_at — "yeni listelenme" yaşı

Boyut disiplini: her bölüm satırla sınırlı, sonunda toplam karakter tavanı
uygulanır. Bedava katmanda günlük token bütçesi asıl kısıttır.
"""
import logging

from ..db import db, kv_get, now

log = logging.getLogger("ai.briefing")

# ~4 karakter ≈ 1 token (kaba ama bu iş için yeterli; tek amacı tavanı korumak)
CHARS_PER_TOKEN = 4
DEFAULT_MAX_TOKENS = 2500


def _fmt(v, nd=1) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "?"
    if abs(f) >= 1_000_000:
        return f"{f / 1_000_000:.{nd}f}M"
    if abs(f) >= 1_000:
        return f"{f / 1_000:.0f}K"
    return f"{f:.{nd}f}"


def _addr(a: str) -> str:
    a = a or ""
    return f"{a[:8]}..{a[-4:]}" if len(a) > 14 else a


async def _universe(conn, ts) -> list[str]:
    cur = await conn.execute(
        "SELECT coin, symbol, listed_at FROM tickers ORDER BY symbol")
    rows = [dict(r) for r in await cur.fetchall()]
    out = [f"Evren: {len(rows)} hisse perp'i."]
    # tickers.listed_at hiçbir yerde okunmuyordu — "yeni listelendi" yaşı
    # tek başına bir sinyal (yeni market = ince defter = kolay itilen fiyat).
    fresh = [r for r in rows if r["listed_at"] and ts - r["listed_at"] < 14 * 86400]
    if fresh:
        out.append("Son 14 günde listelenen: " + ", ".join(
            f"{r['symbol']}({(ts - r['listed_at']) // 86400}g)" for r in fresh[:10]))
    return out


async def _metrics_moves(conn, ts, limit=12) -> list[str]:
    """asset_metrics 45 gün saklıyor ama site yalnız son değeri gösteriyor.
    24 saatlik fiyat/OI/hacim değişimini burada hesaplayıp veriyoruz."""
    cur = await conn.execute(
        "SELECT coin, MAX(ts) mt FROM asset_metrics WHERE ts >= ? GROUP BY coin",
        (ts - 2 * 86400,))
    latest = {r["coin"]: r["mt"] for r in await cur.fetchall()}
    if not latest:
        return []
    out = []
    for coin, mt in latest.items():
        cur = await conn.execute(
            "SELECT mark_px, oi, funding, day_volume FROM asset_metrics"
            " WHERE coin=? AND ts=?", (coin, mt))
        nowr = await cur.fetchone()
        cur = await conn.execute(
            "SELECT mark_px, oi, day_volume FROM asset_metrics"
            " WHERE coin=? AND ts<=? ORDER BY ts DESC LIMIT 1", (coin, mt - 86400))
        old = await cur.fetchone()
        if not nowr or not old or not old["mark_px"] or not old["oi"]:
            continue
        dpx = (nowr["mark_px"] - old["mark_px"]) / old["mark_px"] * 100
        doi = (nowr["oi"] - old["oi"]) / old["oi"] * 100 if old["oi"] else 0.0
        out.append({"coin": coin, "dpx": dpx, "doi": doi,
                    "vol": nowr["day_volume"] or 0, "fund": nowr["funding"] or 0})
    if not out:
        return []
    out.sort(key=lambda r: -abs(r["doi"]))
    lines = ["24s değişim (coin | fiyat% | OI% | hacim$ | funding):"]
    for r in out[:limit]:
        lines.append(f"  {r['coin']} | {r['dpx']:+.1f} | {r['doi']:+.1f} |"
                     f" {_fmt(r['vol'])} | {r['fund']:+.5f}")
    return lines


async def _new_positions(conn, ts, limit=10) -> list[str]:
    cur = await conn.execute(
        """SELECT coin, address, side, notional, score, score_reasons, opened_ts
           FROM positions_current
           WHERE COALESCE(opened_ts, first_seen_ts, 0) >= ?
           ORDER BY notional DESC LIMIT ?""", (ts - 3 * 86400, limit))
    rows = [dict(r) for r in await cur.fetchall()]
    if not rows:
        return []
    lines = ["Son 72s açılan pozisyonlar (coin | adres | yön | $ | skor):"]
    for r in rows:
        lines.append(f"  {r['coin']} | {_addr(r['address'])} | {r['side']} |"
                     f" {_fmt(r['notional'])} | {r['score'] or 0}")
    return lines


async def _taker_balance(conn, ts, limit=8) -> list[str]:
    """fills.taker günde ~700K satırda toplanıyor ama sitede HİÇ gösterilmiyor.
    Agresör/pasif dengesi, "kim fiyatı süpürüyor" sorusunun cevabı."""
    cur = await conn.execute(
        """SELECT coin,
                  SUM(CASE WHEN taker=1 AND side='buy'  THEN notional ELSE 0 END) tb,
                  SUM(CASE WHEN taker=1 AND side='sell' THEN notional ELSE 0 END) tsl,
                  SUM(notional) tot, COUNT(*) n
           FROM fills WHERE ts >= ? GROUP BY coin
           HAVING tot > 0 ORDER BY tot DESC LIMIT ?""", (ts - 86400, limit))
    rows = [dict(r) for r in await cur.fetchall()]
    if not rows:
        return []
    lines = ["24s agresör dengesi (coin | taker alış$ | taker satış$ | işlem):"]
    for r in rows:
        lines.append(f"  {r['coin']} | {_fmt(r['tb'])} | {_fmt(r['tsl'])} | {r['n']}")
    return lines


async def _walls(conn, ts, limit=6) -> list[str]:
    """book_walls pasif satırları hiç budanmıyor ve hiç gösterilmiyor:
    zirvesinden çekilmiş duvar = spoof şüphesi."""
    cur = await conn.execute(
        """SELECT coin, side, notional, peak_notional, dist_pct, active, last_ts
           FROM book_walls WHERE last_ts >= ?
           ORDER BY peak_notional DESC LIMIT ?""", (ts - 2 * 86400, limit))
    rows = [dict(r) for r in await cur.fetchall()]
    if not rows:
        return []
    lines = ["Emir defteri duvarları (coin | yön | şu an$ | zirve$ | mesafe% | durum):"]
    for r in rows:
        st = "aktif" if r["active"] else "çekildi"
        lines.append(f"  {r['coin']} | {r['side']} | {_fmt(r['notional'])} |"
                     f" {_fmt(r['peak_notional'])} | {r['dist_pct']:.2f} | {st}")
    return lines


async def _liq(conn, ts, limit=6) -> list[str]:
    cur = await conn.execute(
        """SELECT coin, address, side, notional, last_dist, stage FROM liq_watch
           ORDER BY stage DESC, last_dist ASC LIMIT ?""", (limit,))
    rows = [dict(r) for r in await cur.fetchall()]
    if not rows:
        return []
    lines = ["Likidasyona yakınlar (coin | adres | yön | $ | mesafe% | kademe):"]
    for r in rows:
        lines.append(f"  {r['coin']} | {_addr(r['address'])} | {r['side']} |"
                     f" {_fmt(r['notional'])} | {r['last_dist']:.2f} | {r['stage']}")
    return lines


async def _hl_lifecycle(conn, ts, limit=8) -> list[str]:
    """hl_positions zirve/kapanış geçmişi: /devler yalnız ilk 150'yi listeliyor,
    yaşam döngüsünü (açıldı→zirve→kapandı) hiçbir yer göstermiyor."""
    cur = await conn.execute(
        """SELECT coin, address, side, notional, peak_notional, peak_ts,
                  first_seen_ts, closed_ts FROM hl_positions
           WHERE COALESCE(closed_ts, ts) >= ?
           ORDER BY peak_notional DESC LIMIT ?""", (ts - 3 * 86400, limit))
    rows = [dict(r) for r in await cur.fetchall()]
    if not rows:
        return []
    lines = ["HL dev pozisyon hareketleri (coin | adres | yön | şu an$ | zirve$ | durum):"]
    for r in rows:
        st = "kapandı" if r["closed_ts"] else "açık"
        lines.append(f"  {r['coin']} | {_addr(r['address'])} | {r['side']} |"
                     f" {_fmt(r['notional'])} | {_fmt(r['peak_notional'])} | {st}")
    return lines


async def _alerts(conn, ts, limit=10) -> list[str]:
    """alerts_log hiçbir web rotasından okunmuyor. Modelin kendi gürültümüzü
    görmesi önemli: neyi ne zaman bildirdiğimiz, "bu sinyal işe yaradı mı"
    sorusunun ilk yarısı."""
    cur = await conn.execute(
        """SELECT kind, COUNT(*) n, MAX(ts) last FROM alerts_log
           WHERE ts >= ? AND kind NOT LIKE 'probe%' GROUP BY kind
           ORDER BY n DESC LIMIT ?""", (ts - 2 * 86400, limit))
    rows = [dict(r) for r in await cur.fetchall()]
    if not rows:
        return []
    return ["48s içinde ürettiğimiz bildirimler (tip | adet): "
            + ", ".join(f"{r['kind']}×{r['n']}" for r in rows)]


async def _records(conn, ts) -> list[str]:
    cur = await conn.execute(
        "SELECT COUNT(*) n FROM addresses WHERE watchlist=1")
    n_watch = (await cur.fetchone())["n"]
    cur = await conn.execute(
        "SELECT SUM(hits) h, SUM(misses) m FROM addresses WHERE COALESCE(entity,'')=''")
    r = await cur.fetchone()
    cur = await conn.execute(
        """SELECT symbol, date_et, hour_hint FROM earnings_events
           WHERE evaluated=0 AND date_et >= date('now','-1 day')
           ORDER BY date_et LIMIT 8""")
    up = [dict(x) for x in await cur.fetchall()]
    out = [f"Sicil: {n_watch} watchlist adresi, toplam {r['h'] or 0} doğru /"
           f" {r['m'] or 0} yanlış bilanço tahmini."]
    if up:
        out.append("Yaklaşan bilançolar: " + ", ".join(
            f"{e['symbol']}({e['date_et']} {e['hour_hint']})" for e in up))
    # wallet_links yalnız /whale sayfasında görünüyor; küme büyüklüğü hiç yok
    cur = await conn.execute("SELECT COUNT(*) n FROM wallet_links")
    n_links = (await cur.fetchone())["n"]
    if n_links:
        out.append(f"Bilinen cüzdan bağlantısı (aynı fonlama kaynağı): {n_links} çift.")
    return out


async def _sessions(limit=6) -> list[str]:
    from ..radar.hourstats import all_stats, session_ranking
    rows = session_ranking(await all_stats())
    if not rows:
        return []
    lines = ["Seans karnesi (coin | kapalı↑% | açık↑% | yükselişin kapalı payı%):"]
    for r in rows[:limit]:
        share = "—" if r["up_share"] is None else f"{r['up_share']:.0f}"
        lines.append(f"  {r['symbol']} | {r['closed_up']:+.1f} |"
                     f" {r['open_up']:+.1f} | {share}")
    return lines


async def _own_record(conn) -> list[str]:
    """Modelin KENDİ sicili brifinge girer: geçmiş hipotezlerinin nasıl gittiğini
    görsün ki aynı hatayı tekrarlamasın."""
    cur = await conn.execute(
        "SELECT status, COUNT(*) n FROM ai_hypotheses GROUP BY status")
    st = {r["status"]: r["n"] for r in await cur.fetchall()}
    if not st:
        return []
    hit, miss = st.get("hit", 0), st.get("miss", 0)
    tot = hit + miss
    line = (f"Senin sicilin: {hit} tuttu / {miss} tutmadı"
            + (f" (%{hit / tot * 100:.0f})" if tot else "")
            + f", {st.get('open', 0)} beklemede, {st.get('unresolvable', 0)} ölçülemedi.")
    out = [line]
    cur = await conn.execute(
        """SELECT claim, status FROM ai_hypotheses
           WHERE status IN ('hit','miss') ORDER BY resolved_ts DESC LIMIT 4""")
    recent = [dict(r) for r in await cur.fetchall()]
    if recent:
        out.append("Son sonuçlanan hipotezlerin:")
        for r in recent:
            mark = "TUTTU" if r["status"] == "hit" else "TUTMADI"
            out.append(f"  [{mark}] {r['claim'][:120]}")
    cur = await conn.execute(
        "SELECT claim, resolve_ts FROM ai_hypotheses WHERE status='open'"
        " ORDER BY resolve_ts LIMIT 5")
    open_h = [dict(r) for r in await cur.fetchall()]
    if open_h:
        out.append("Halen açık hipotezlerin (aynısını tekrar önerme):")
        for r in open_h:
            out.append(f"  {r['claim'][:120]}")
    return out


async def build(max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
    """Brifing metnini üret. Bölümler ÖNEM SIRASINDA: tavan aşılırsa sondan
    kesilir, yani en değerli kısım her zaman içeride kalır."""
    ts = now()
    async with db() as conn:
        sections: list[list[str]] = []
        for fn in (_universe, _metrics_moves, _new_positions, _taker_balance,
                   _hl_lifecycle, _walls, _liq, _records, _alerts):
            try:
                sections.append(await fn(conn, ts))
            except Exception:
                log.exception("brifing bölümü atlandı: %s", fn.__name__)
                sections.append([])
        try:
            sections.append(await _own_record(conn))
        except Exception:
            log.exception("brifing: kendi sicili okunamadı")
    try:
        sections.insert(-1, await _sessions())
    except Exception:
        log.exception("brifing: seans karnesi okunamadı")

    budget = max(500, int(max_tokens)) * CHARS_PER_TOKEN
    parts, used = [], 0
    for sec in sections:
        if not sec:
            continue
        block = "\n".join(sec)
        if used + len(block) + 2 > budget:
            continue          # bu bölüm sığmadı — sonrakiler daha küçük olabilir
        parts.append(block)
        used += len(block) + 2
    return "\n\n".join(parts)


async def subjects() -> tuple[set[str], set[str]]:
    """Doğrulayıcıya verilecek geçerli konu kümeleri: (coin'ler, adresler).

    Adres kümesi büyük olabilir; yalnız brifingte geçmesi muhtemel olanları
    (pozisyonu/takibi olanlar) alıyoruz — modelin uydurduğu adres reddedilsin
    ama 17 bin satırı belleğe çekmeyelim.
    """
    async with db() as conn:
        cur = await conn.execute("SELECT coin FROM tickers")
        coins = {r["coin"] for r in await cur.fetchall()}
        cur = await conn.execute(
            "SELECT DISTINCT address FROM positions_current"
            " UNION SELECT DISTINCT address FROM hl_positions WHERE closed_ts IS NULL"
            " UNION SELECT address FROM addresses WHERE watchlist=1")
        addrs = {(r["address"] or "").lower() for r in await cur.fetchall()}
    return coins, addrs
