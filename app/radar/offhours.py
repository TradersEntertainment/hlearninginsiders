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

# Kümülatif bant tetiğinin adres kırılımı için bakılan pencere. Bandın
# kendisi çıpadan beri (hafta sonu = 60 saat) ölçülür ama "kim aldı"
# sorusunun 60 saatlik cevabı işe yaramaz; son bir saat sorulur.
DEV_BRIEF_SEC = 3600

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
                      t.symbol, a.watchlist, a.hits, a.misses,
                      a.account_value, a.account_ts
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


async def alarm_status(cfg, rows: list[dict], anchor: int) -> dict:
    """Sayfa için: hangi sembol hangi bantta, en son ne oldu.

    "Neden bildirim gelmedi" sorusu bugüne kadar ancak elle SQL ile
    cevaplanabiliyordu: dedupe satırları (`kind='offhours'`) hiçbir arayüzde
    görünmüyor, `Notifier.suppressed` okunmuyor, `skipped` sebebi atılıyordu.

    DÖRT AYRI DURUM, dört ayrı `kind`'dan okunur — hepsini "bildirim yok"a
    indirmek sorunun kendisiydi:
      sent  → gitti · quiet → sessiz saatte bastırıldı
      fail  → gönderilemedi (bir sonraki tur yeniden denenecek)
      ""    → eşiğin altında, denenmedi
    """
    from ..db import kv_get
    band_pct = max(0.01, float(getattr(cfg, "offhours_alert_pct", 0.5)))
    marks = await band_marks(anchor)
    async with db() as conn:
        cur = await conn.execute(
            "SELECT kind, key, MAX(ts) ts FROM alerts_log"
            " WHERE kind IN ('sent:offhours','quiet:offhours','fail:offhours')"
            "   AND ts >= ? GROUP BY kind, key", (int(anchor),))
        seen = [dict(r) for r in await cur.fetchall()]
    last: dict[str, tuple[str, int]] = {}
    for e in seen:
        parsed = parse_dev_key(e["key"] or "")
        if not parsed:
            continue
        coin = parsed[0]
        prev = last.get(coin)
        if not prev or e["ts"] > prev[1]:
            last[coin] = (e["kind"].split(":")[0], int(e["ts"]))
    out = []
    for r in rows:
        if not r.get("propr"):
            continue
        sign = "+" if r["dev"] > 0 else "-"
        band = int(abs(r["dev"]) / band_pct)
        st, when = last.get(r["coin"], ("", 0))
        out.append({"symbol": r["symbol"], "coin": r["coin"], "dev": r["dev"],
                    "band": band, "mark": marks.get((r["coin"], sign), 0),
                    "state": st or ("due" if band else ""), "ts": when})
    out.sort(key=lambda x: (-x["band"], -abs(x["dev"])))
    return {"rows": out, "band_pct": band_pct,
            "stats": await kv_get("offhours_stats") or {}}


async def _stats(out: dict) -> dict:
    """Tur sonucunu kv'ye yaz ve aynen döndür.

    `main.py` bu fonksiyonun dönüşünü ATIYOR; `skipped` sebebi hiçbir yerde
    görünmüyordu. "Alarm neden gelmedi" sorusunun ilk cevabı burası.
    """
    from ..db import kv_set
    try:
        await kv_set("offhours_stats", {**out, "ts": hourstats.now()})
    except Exception:
        log.debug("offhours_stats yazılamadı", exc_info=True)
    return out


def parse_dev_key(k: str) -> tuple[str, str, int] | None:
    """`dev:<coin>:<çıpa>:<+|-><bant>` → (coin, yön, bant). Bozuksa None.

    TEK KAYNAK: hem filigran hem sayfa durumu bunu kullanır. Coin'in kendisinde
    ':' var (`xyz:SNDK`), o yüzden SAĞDAN ayrılıyor — iki ayrı ayrıştırma
    yazıldığında biri baştan kesip yanlış coin üretmişti.
    """
    try:
        head, tail = k.rsplit(":", 1)              # head: dev:<coin>:<çıpa>
        coin = head.split(":", 1)[1].rsplit(":", 1)[0]
        sign, band = tail[0], int(tail[1:])
    except (ValueError, IndexError):
        return None
    return (coin, sign, band) if sign in "+-" and coin else None


async def band_marks(anchor: int) -> dict[tuple[str, str], int]:
    """(coin, yön) -> bu çıpada DUYURULMUŞ en yüksek bant.

    Neden filigran, neden bant başına bağımsız anahtar değil: bant anlık
    hesaplanıyor (`int(|sapma| / eşik)`) ve fiyat iki ölçüm arasında %0.3'ten
    %2.1'e sıçrayabiliyor. Bağımsız anahtarlarda o turda YALNIZ bant 4
    deneniyordu; o da bir kez başarısız olduysa geriye denenecek hiçbir şey
    kalmıyordu. Filigranda soru "bu bant daha önce denendi mi" değil,
    "bugüne kadar duyurduğumuzdan YÜKSEK mi" — sıçrama tek mesajla kapanıyor
    ve başarısız bir gönderim filigranı ilerletmediği için tur tekrar deniyor.

    Tur başına TEK sorgu; `idx_alerts_kind_key` (kind, key, ts) kullanılır.
    """
    out: dict[tuple[str, str], int] = {}
    async with db() as conn:
        cur = await conn.execute(
            "SELECT key FROM alerts_log WHERE kind='offhours' AND ts >= ?"
            " AND key LIKE 'dev:%'", (int(anchor),))
        rows = [r["key"] for r in await cur.fetchall()]
    for k in rows:
        parsed = parse_dev_key(k)
        if not parsed:
            continue
        coin, sign, band = parsed
        if band > out.get((coin, sign), 0):
            out[(coin, sign)] = band
    return out


async def _one(cfg, client, notifier, r, scr, marks, p) -> dict:
    """Tek sembolün iki tetiği. Hata çağırana taşar — orada sembol yalıtılıyor."""
    from .forensics import alert_brief as _brief
    anchor, ts = p["anchor"], p["ts"]
    out = {"dev": 0, "spike": 0, "failed": 0}
    base = {"symbol": r["symbol"], "px": r["px"], "base_px": r["base_px"],
            "anchor_ts": anchor, "oi_chg": r.get("oi_chg"),
            "weekend_left": scr.get("weekend_left") or 0,
            "next_reg_ts": scr.get("next_reg_ts"), "weekend": p["weekend"],
            "spike_thr": p["spike_pct"]}

    # --- 1) kümülatif sapma bandı (yalnız hafta sonu) ---
    band = int(abs(r["dev"]) / p["band_pct"]) if p["bands_on"] else 0
    # YÖN filigranda ayrı tutuluyor: +%1.1'den -%1.1'e savrulan bir hisse aynı
    # bant numarasına düşer ama bu iki AYRI olaydır.
    sign = "+" if r["dev"] > 0 else "-"
    if band > marks.get((r["coin"], sign), 0):
        key = f"dev:{r['coin']}:{anchor}:{sign}{band}"
        # Bant tetiği bütün hafta sonunu kapsar; 60 saatlik bir adres kırılımı
        # hiçbir şey anlatmaz. Son bir saate bakılır ve mesajda AÇIKÇA yazılır.
        brief = await _brief(cfg, client, r["coin"], ts - DEV_BRIEF_SEC, ts)
        text = fmt_move({**base, "kind": "dev", "pct": r["dev"], "brief": brief})
        if await notifier.send("offhours", text, priority="high", key=key):
            # MARKER YALNIZ GİDERSE. Eskiden koşulsuz yazılıyordu: geçici bir
            # HTTP hatası ya da sessiz saat bastırması o bandı 30 GÜN
            # susturuyordu ve çıpa hafta sonu boyunca sabit olduğu için hafta
            # sonunun tamamı sessiz geçiyordu.
            await alert_log("offhours", key, text)
            marks[(r["coin"], sign)] = band
            out["dev"] += 1
        else:
            # Görünürlük satırı — dedupe DEĞİL. Bir sonraki tur yeniden dener.
            await alert_log("fail:offhours", key, text[:200])
            out["failed"] += 1

    # --- 2) ani hareket (ABD kapalı her saatte) ---
    if not p["spikes_on"]:
        return out
    win, cool = p["win"], p["cool"]
    ref = await metrics.metric_at(r["coin"], ts - win)
    # Referans örnek ÇIPADAN ESKİYSE tetikleme: pencere seansın içine taşmış
    # demektir ve ölçülen şey "kapalıyken sıçrama" değil, seansın oynaklığı.
    if not ref or int(ref.get("ts") or 0) < anchor or not ref.get("mark_px"):
        return out
    jump = _pct(r["px"], ref["mark_px"])
    if jump is None or abs(jump) < p["spike_pct"]:
        return out
    key = f"spike:{r['coin']}"
    if await alert_recent("offhours", key, cool):
        return out
    brief = await _brief(cfg, client, r["coin"], ts - win, ts)
    text = fmt_move({**base, "kind": "spike", "pct": jump, "brief": brief,
                     "ref_px": ref["mark_px"], "window_min": win // 60})
    if await notifier.send("offhours", text, priority="high",
                           key=f"{key}:{ts // cool}"):
        await alert_log("offhours", key, text)      # bant dalıyla aynı kural
        out["spike"] += 1
    else:
        await alert_log("fail:offhours", key, text[:200])
        out["failed"] += 1
    return out


async def check_alerts(cfg, notifier, client=None) -> dict:
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

    PENCERE — İKİ TETİĞE İKİ AYRI KAPI:
      • KÜMÜLATİF BANTLAR yalnız hafta sonu (Cuma 24:00 → Pzt 00:00 TSİ).
        Hafta içi de açtığımızda günde ~16 saat %0.5'lik bildirim demekti;
        gürültü oradan geliyordu. `offhours_alert_weekend_only=0` ile açılır.
      • ANİ HAREKET ABD kapalı HER saatte çalışır, ama hafta içi eşiği daha
        yüksektir (`offhours_spike_pct_weekday`, vars. %2) çünkü pencere 16.5
        saat ve pre-market'te %1 sıradan. Sebep: SHEIN Pazartesi sabahı %12
        düştü ve "hafta sonu değil" diye susmuştuk.
    İkisi de kapalıysa sessizce dönmeyiz — `skipped` dolar.

    YALNIZ PROPR'da listeli sembollere bakılır (harekete geçemeyeceğin hisse
    için bildirim gürültüdür).
    """
    from ..propr import is_listed as propr_listed
    out = {"dev": 0, "spike": 0, "failed": 0, "looked": 0, "skipped": ""}
    h = ts_hour(cfg)
    if not hourstats.us_closed(None, h):              # 1. param ref_ts, 2. saat
        out["skipped"] = "ABD açık"
        return await _stats(out)
    weekend = hourstats.weekend_window(None, h) is not None
    bands_on = weekend or not getattr(cfg, "offhours_alert_weekend_only", True)
    spikes_on = weekend or not getattr(cfg, "offhours_spike_weekend_only", False)
    if not bands_on and not spikes_on:
        out["skipped"] = "hafta sonu değil (iki tetik de hafta sonuna kilitli)"
        return await _stats(out)

    scr = await screener(cfg)
    if not scr["closed"]:                     # çıpa/durum tutarsızsa sus
        out["skipped"] = "ABD açık"
        return await _stats(out)
    band_pct = max(0.01, float(getattr(cfg, "offhours_alert_pct", 0.5)))
    # Hafta içi eşiği ayrı: kapalı pencere 16.5 saat, %1 orada sıradan.
    spike_pct = max(0.01, float(
        getattr(cfg, "offhours_spike_pct", 1.0) if weekend
        else getattr(cfg, "offhours_spike_pct_weekday", 2.0)))
    win = max(1, int(getattr(cfg, "offhours_spike_min", 10))) * 60
    cool = max(60, int(getattr(cfg, "offhours_spike_cooldown", 1800)))
    anchor, ts = scr["anchor_ts"], hourstats.now()

    marks = await band_marks(anchor)          # (coin, yön) -> duyurulan en yüksek bant
    for r in scr["rows"]:
        if not propr_listed(r["symbol"]):
            continue
        # SEMBOL BAŞINA YALITIM: eskiden tek bir sembolün biçimlendirme hatası
        # `for` döngüsünü komple düşürüyordu ve o turdaki DİĞER bütün semboller
        # sessizce atlanıyordu (dıştaki try log'a yazıp geçiyor).
        out["looked"] += 1
        try:
            n = await _one(cfg, client, notifier, r, scr, marks, dict(
                band_pct=band_pct, spike_pct=spike_pct, win=win, cool=cool,
                bands_on=bands_on, spikes_on=spikes_on, weekend=weekend,
                anchor=anchor, ts=ts))
        except Exception as e:
            out["failed"] += 1
            log.exception("kapalı seans alarmı (%s) başarısız", r["symbol"])
            await alert_log("fail:offhours", f"dev:{r['coin']}:{anchor}",
                            f"{type(e).__name__}: {e}")
            continue
        out["dev"] += n["dev"]
        out["spike"] += n["spike"]
        out["failed"] += n["failed"]

    if out["dev"] or out["spike"] or out["failed"]:
        log.info("kapalı seans bildirimi: %d sapma, %d ani hareket, %d başarısız",
                 out["dev"], out["spike"], out["failed"])
    out["anchor_ts"] = anchor
    return await _stats(out)


def ts_hour(cfg):
    """cfg'den çıpa saati (yoksa modül varsayılanı)."""
    return None if cfg is None else int(
        getattr(cfg, "offhours_close_hour", hourstats.CLOSE_TSI_HOUR))


def fmt_move(m: dict) -> str:
    from ..telegram import format as fmt
    return fmt.offhours_move(m)
