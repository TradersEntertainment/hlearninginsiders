"""Kripto hacim patlaması — 24 saatin en yüksek 5 dakikası.

PROPR'da listeli bir kripto coin, son 24 saatin EN YÜKSEK 5 dakikalık hacmine
ulaştığında olay üretir. Bildirim ayrı bir Telegram kanalına gider
(`CRYPTO_CHAT_ID`), sitede `/hacim` sekmesinde listelenir.

KRİPTO/TRADFİ AYRIMI: PROPR listesini bölmüyoruz. HL'nin ana dex'i yalnız
kripto içerir; HIP-3 hisse/emtia perp'leri `xyz:` önekli gelir. Yani
"PROPR ∩ ana dex" kendiliğinden kriptodur ve kullanıcı sonradan PROPR'a bir
kripto eklerse otomatik girer.

BİRİM MESELESİ — ÖNEMLİ: HL mumundaki `v` alanının baz mı quote mu olduğuna
dair varsayım yapmıyoruz. REKOR KARŞILAŞTIRMASI HAM `v` ÜZERİNDEN yapılır;
aynı coinin kendi kovalarını kıyasladığımız için birim sadeleşir. Dolar değeri
(`v × kapanış`) yalnız GÖSTERİM ve alt sınır içindir ve kendi kendini
denetler: 24 saatlik toplamı `dayNtlVlm` ile kabaca uyuşmuyorsa uyarı üretilir.
"""
import asyncio
import logging

from ..db import alert_recent, alert_log, db, now

log = logging.getLogger("radar.cryptovol")

INTERVAL = "5m"
BUCKET_SEC = 300
LOOKBACK_SEC = 25 * 3600      # 24 saat taban + biraz pay
UNIT_CHECK_RATIO = 5.0        # notional toplamı dayNtlVlm'den bu kat saparsa uyar
RETENTION_D = 14
MIN_BUCKETS = 12              # en az bir saatlik taban olmadan rekor aranmaz


def n_closed(candles: list[dict], ref_ts: int | None = None) -> int:
    """Kaç KAPANMIŞ kova elimizde?

    Boş/eksik mum listesi bir HATA DEĞİL, veri yokluğudur — `err` sayacına
    girmez. İkisini ayırt etmezsek "panel neden boş" sorusu cevapsız kalır:
    borsa mum vermiyor mu, yoksa rekor mu çıkmadı, aynı görünür.
    """
    ts = int(ref_ts) if ref_ts else now()
    return sum(1 for c in candles if c["t"] + BUCKET_SEC <= ts)


def note_miss(out: dict, coin: str, rec: dict) -> None:
    """Sayfa eşiğini geçemeyen EN BÜYÜK rekoru sakla.

    Eşiğin doğru yerde olup olmadığını ancak kaçırdıklarımızı görerek
    anlayabiliriz; yoksa eşiği tahminle ayarlıyoruz.
    """
    best = out.get("best_miss")
    if not best or rec["notional"] > best["notional"]:
        out["best_miss"] = {"sym": coin.split(":")[-1],
                            "notional": rec["notional"],
                            "ratio": rec.get("ratio")}


def parse_vol_candles(raw) -> list[dict]:
    """5dk mumları — `hourstats.parse_candles` hacmi ATMIYOR ama tutmuyor da;
    burada `v` şart olduğu için ayrı ayrıştırıcı."""
    out = []
    if not isinstance(raw, list):
        return out
    for c in raw:
        try:
            out.append({"t": int(c["t"]) // 1000, "o": float(c["o"]),
                        "c": float(c["c"]), "v": float(c.get("v") or 0)})
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda x: x["t"])
    return out


def find_record(candles: list[dict], ref_ts: int | None = None) -> dict | None:
    """Son KAPANMIŞ kova, önceki 24 saatin hepsinden büyük mü?

    Devam eden mum atlanır: yarım hacmi tam kovalarla karşılaştırmak elmayla
    armut olurdu. `ref_ts` verilmezse şimdi.
    """
    ts = int(ref_ts) if ref_ts else now()
    closed = [c for c in candles if c["t"] + BUCKET_SEC <= ts]
    if len(closed) < MIN_BUCKETS:        # en az bir saatlik taban olsun
        return None
    last, prev = closed[-1], closed[:-1]
    prev_max = max((c["v"] for c in prev), default=0.0)
    if last["v"] <= prev_max or last["v"] <= 0:
        return None
    return {
        "bucket_ts": last["t"], "vol": last["v"], "prev_max": prev_max,
        "ratio": (last["v"] / prev_max) if prev_max else None,
        "px": last["c"], "notional": last["v"] * last["c"],
        "chg_pct": (last["c"] / last["o"] - 1) * 100 if last["o"] else 0.0,
        "n_buckets": len(prev),
    }


def unit_sane(candles: list[dict], day_ntl_vlm: float | None) -> bool | None:
    """`v × fiyat` gerçekten dolar hacmi mi? None = kıyaslanacak veri yok.

    Birim varsayımı yanlışsa alt sınır ($250K) anlamsız olur ve sayfa yanlış
    rakam gösterir — sessiz kalmasın diye ölçüyoruz.
    """
    if not day_ntl_vlm or day_ntl_vlm <= 0 or not candles:
        return None
    total = sum(c["v"] * c["c"] for c in candles)
    if total <= 0:
        return None
    r = total / day_ntl_vlm
    return (1 / UNIT_CHECK_RATIO) <= r <= UNIT_CHECK_RATIO


async def universe(cfg, client) -> tuple[list[str], dict]:
    """(taranacak coin'ler, coin→24s hacim). PROPR ∩ ana dex, hacimce büyükten."""
    from ..hl.universe import main_dex_volumes
    from ..propr import is_listed as propr_listed
    vols = await main_dex_volumes(client)
    cap = max(1, int(getattr(cfg, "crypto_vol_max_coins", 120)))
    coins = sorted((c for c in vols if propr_listed(c)),
                   key=lambda c: -vols.get(c, 0))[:cap]
    return coins, vols


async def scan(cfg, client, notifier=None) -> dict:
    """Bir tur: her coin için 5dk mumlarını çek, rekoru bul, kaydet, bildir."""
    out = {"checked": 0, "events": 0, "alerted": 0, "err": 0,
           "unit_bad": [], "skipped": "", "coins": 0,
           # TEŞHİS: "0 rekor" üç ayrı sebepten olabilir ve üçü de ekranda
           # aynı görünüyordu — mum gelmiyor / rekor çıkmadı / eşik eledi.
           "n_nodata": 0, "n_bucket": 0, "n_record": 0,
           "below_page": 0, "below_alert": 0, "best_miss": None}
    if not getattr(cfg, "crypto_vol_enabled", True):
        out["skipped"] = "kapalı"
        return out
    try:
        coins, vols = await universe(cfg, client)
    except Exception as e:
        out["skipped"] = f"evren alınamadı: {type(e).__name__}: {e}"
        log.warning("kripto hacim evreni alınamadı: %s", e)
        return out
    out["coins"] = len(coins)
    min_usd = float(getattr(cfg, "crypto_vol_min_usd", 50000))
    alert_min = max(min_usd,
                    float(getattr(cfg, "crypto_vol_alert_min_usd", 250000)))
    cool = max(60, int(getattr(cfg, "crypto_vol_cooldown", 1800)))
    chat = (getattr(cfg, "crypto_chat_id", "") or "").strip()
    ts = now()
    end_ms, start_ms = ts * 1000, (ts - LOOKBACK_SEC) * 1000

    for coin in coins:
        try:
            candles = parse_vol_candles(
                await client.candles(coin, INTERVAL, start_ms, end_ms))
        except Exception as e:
            out["err"] += 1
            if out["err"] == 1:
                log.warning("5dk mum alınamadı (%s): %s", coin, e)
            continue
        out["checked"] += 1
        if n_closed(candles, ts) < MIN_BUCKETS:
            out["n_nodata"] += 1           # borsa mum vermiyor — hata değil
            continue
        out["n_bucket"] += 1
        if unit_sane(candles, vols.get(coin)) is False:
            out["unit_bad"].append(coin)
        rec = find_record(candles, ts)
        if not rec:
            continue
        out["n_record"] += 1
        if rec["notional"] < min_usd:
            out["below_page"] += 1
            note_miss(out, coin, rec)
            continue

        # UNIQUE(coin,bucket_ts): aynı kova iki kez taransa da tek satır kalır.
        async with db() as conn:
            cur = await conn.execute(
                """INSERT OR IGNORE INTO vol_events(coin,ts,bucket_ts,vol,notional,
                     prev_max,ratio,px,chg_pct,alerted,market)
                   VALUES(?,?,?,?,?,?,?,?,?,0,'crypto')""",
                (coin, ts, rec["bucket_ts"], rec["vol"], rec["notional"],
                 rec["prev_max"], rec["ratio"], rec["px"], rec["chg_pct"]))
            fresh = (cur.rowcount or 0) > 0
        if not fresh:
            continue                       # bu kovayı zaten kaydetmiştik
        out["events"] += 1

        # SAYFA eşiği ≠ BİLDİRİM eşiği: satır kaydedildi (sayfada bağlam),
        # ama kanala düşmesi için daha yüksek eşiği geçmesi gerekiyor.
        if rec["notional"] < alert_min:
            out["below_alert"] += 1
            continue
        if notifier is None or not chat:
            continue                       # kanal yoksa GÖNDERME (ana kanalı kirletme)
        key = f"cryptovol:{coin}"
        if await alert_recent("cryptovol", key, cool):
            continue                       # olay kaydedildi ama bildirilmiyor
        from ..telegram import format as fmt
        text = fmt.crypto_vol_alert({**rec, "coin": coin,
                                     "day_vol": vols.get(coin)})
        if await notifier.send("cryptovol", text, priority="high",
                               key=f"{key}:{rec['bucket_ts']}", chat_id=chat):
            await alert_log("cryptovol", key, text)
            async with db() as conn:
                await conn.execute(
                    "UPDATE vol_events SET alerted=1 WHERE coin=? AND bucket_ts=?",
                    (coin, rec["bucket_ts"]))
            out["alerted"] += 1

    if out["unit_bad"]:
        log.warning("hacim birimi şüpheli (%d coin, ör. %s): v×fiyat toplamı "
                    "dayNtlVlm ile uyuşmuyor — $ değerleri ve alt sınır yanıltıcı olabilir",
                    len(out["unit_bad"]), ", ".join(out["unit_bad"][:5]))
    if out["events"]:
        log.info("kripto hacim: %d coin tarandı, %d rekor, %d bildirim",
                 out["checked"], out["events"], out["alerted"])
    from ..db import kv_set
    await kv_set("cryptovol_stats", {**out, "ts": ts,
                                     "unit_bad": out["unit_bad"][:10],
                                     "chat": bool(chat)})
    return out


async def recent(limit: int = 60, hours: int = 48,
                 market: str = "crypto") -> list[dict]:
    """Site sekmesi: yakın zamanda hacim rekoru kıranlar.

    `market` boş bırakılırsa ikisi birden döner. Eski satırlarda kolon NULL —
    hepsi kripto turundan geldiği için COALESCE ile 'crypto' okunur.
    """
    q = ("SELECT * FROM vol_events WHERE ts >= ?"
         + (" AND COALESCE(market,'crypto')=?" if market else "")
         + " ORDER BY ts DESC LIMIT ?")
    args = [now() - hours * 3600] + ([market] if market else []) + [limit]
    async with db() as conn:
        cur = await conn.execute(q, tuple(args))
        return [dict(r) for r in await cur.fetchall()]


async def prune(days: int = RETENTION_D) -> int:
    async with db() as conn:
        cur = await conn.execute("DELETE FROM vol_events WHERE ts < ?",
                                 (now() - days * 86400,))
        return cur.rowcount or 0


async def loop(cfg, client, notifier) -> None:
    """Denetimli döngü. Site ASLA buna bağımlı değil: patlasa da yalnız kendi
    sekmesi boş kalır."""
    from ..health import beat
    await asyncio.sleep(120)               # açılışta evren/önbellek otursun
    while True:
        try:
            await beat("cryptovol")
            await scan(cfg, client, notifier)
            await beat("cryptovol")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("kripto hacim turu hatası")
        await asyncio.sleep(max(60, int(getattr(cfg, "crypto_vol_poll_sec", 300))))
