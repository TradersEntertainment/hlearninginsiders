"""Emir defteri duvar radarı.

Birinin fiyatın hemen üstüne/altına koyduğu absürt boyutlu bekleyen emir
duvarlarını yakalar (ör. SPCX'te fiyatın %0.3 üstüne $202M'lık satış
merdiveni). Defter anonimdir; sahibi, tanıdığımız adayların (o coindeki
pozisyon sahipleri + watchlist + kazananlar) açık emirleriyle eşleştirilerek
bulunmaya çalışılır. Alarm verilen duvar sonradan kaybolursa o da bildirilir
(çekildi = muhtemel spoof, ya da doldu).

Felsefe diğer radarlarla aynı: sitede wall_min_usd üstü her şey görünür,
Telegram'a SADECE wall_alert_min_usd üstü "gerçekten absürt" olanlar düşer.
"""
import asyncio
import logging

from .. import assets
from ..config import Config
from ..db import alert_log, alert_recent, db, now
from ..hl.client import HLClient
from ..telegram import format as fmt
from .lowvol import latest_metrics_all
from .report import coin_dex

log = logging.getLogger("radar.bookwall")

WALL_COOLDOWN = 12 * 3600     # aynı coin+side için tekrar alarm aralığı
ATTRIB_MAX_CANDIDATES = 25    # sahiplik denemesinde en fazla sorgulanacak adres
GONE_GRACE = 2                # duvar bu kadar tarama üst üste görünmezse "kalktı"
BIG_REC_MIN = 5_000_000       # büyük sınıfta panel/kayıt tabanı (daimi derinlik gizlensin)


def floors(cfg: Config, symbol: str, coin: str, big_coins: set[str]) -> tuple[float, float]:
    """(kayıt tabanı, alarm tabanı). Endeks/emtia/FX/kripto ya da hacimce top-N
    hisse 'büyük sınıf'tır: defterleri zaten kalın, ancak absürt boyut sinyaldir.
    (SPCX gibi takvimsiz pre-IPO'lar 'no_calendar'dır — HİSSE sayılır, normal kademe.)"""
    big = assets.kind(symbol) == "non_equity" or coin in big_coins
    if big:
        return max(cfg.wall_min_usd, BIG_REC_MIN), cfg.wall_alert_big_min_usd
    return cfg.wall_min_usd, cfg.wall_alert_min_usd


def _parse_book(book: dict) -> tuple[list[dict], list[dict]]:
    """l2Book yanıtı → (bids, asks); her seviye {px, sz} float."""
    levels = (book or {}).get("levels") or []
    if len(levels) != 2:
        return [], []
    out = []
    for side_levels in levels:
        parsed = []
        for lv in side_levels or []:
            try:
                parsed.append({"px": float(lv.get("px")), "sz": float(lv.get("sz"))})
            except (TypeError, ValueError, AttributeError):
                continue
        out.append(parsed)
    return out[0], out[1]


def find_wall(bids: list[dict], asks: list[dict], window_pct: float,
              min_usd: float) -> list[dict]:
    """Pencere içindeki tek taraflı yoğunlaşmaları bul. [{side, px_lo, px_hi,
    sz, notional, dist_pct, mark, opp_notional}]"""
    if not bids or not asks:
        return []
    best_bid, best_ask = bids[0]["px"], asks[0]["px"]
    if best_bid <= 0 or best_ask <= 0:
        return []
    mid = (best_bid + best_ask) / 2
    win = mid * window_pct / 100

    def side_sum(levels):
        sel = [lv for lv in levels if abs(lv["px"] - mid) <= win]
        ntl = sum(lv["px"] * lv["sz"] for lv in sel)
        return sel, ntl

    bid_sel, bid_ntl = side_sum(bids)
    ask_sel, ask_ntl = side_sum(asks)
    walls = []
    for side, sel, ntl, opp in (("bid", bid_sel, bid_ntl, ask_ntl),
                                ("ask", ask_sel, ask_ntl, bid_ntl)):
        if ntl < min_usd or not sel:
            continue
        pxs = [lv["px"] for lv in sel]
        nearest = min(abs(p - mid) for p in pxs)
        walls.append({
            "side": side, "px_lo": min(pxs), "px_hi": max(pxs),
            "sz": sum(lv["sz"] for lv in sel), "notional": ntl,
            "dist_pct": nearest / mid * 100, "mark": mid, "opp_notional": opp,
        })
    return walls


async def _attribute(cfg: Config, client: HLClient, coin: str, side: str,
                     px_lo: float, px_hi: float, wall_ntl: float) -> str | None:
    """Duvarın sahibini tanıdığımız adaylar arasında ara.
    side: 'ask' → satış emirleri (A), 'bid' → alış (B)."""
    # Adaylar ÖNCELİK sırasıyla: (0) bu coin'de pozisyonu olanlar — duvarın en
    # olası sahibi, (1) watchlist, (2) geçmiş kazananlar. Eskiden sırasız UNION +
    # LIMIT 25 vardı; address_wins büyüdükçe alakasız adresler dilimi doldurup
    # gerçek sahibi dışarıda bırakıyordu (her duvar 'sahibi bilinmiyor' + 25 boş
    # API isteği). Öncelik + dedup ile gerçek sahip hep ilk 25'e girer.
    async with db() as conn:
        cur = await conn.execute(
            """SELECT a, MIN(pri) pri FROM (
                 SELECT p.address a, 0 pri FROM positions_current p
                   LEFT JOIN addresses ad ON ad.address=p.address
                   WHERE p.coin=? AND COALESCE(ad.entity,'')=''
                 UNION ALL SELECT address a, 1 pri FROM addresses
                   WHERE watchlist=1 AND COALESCE(entity,'')=''
                 UNION ALL SELECT w.address a, 2 pri FROM address_wins w
                   LEFT JOIN addresses ad ON ad.address=w.address
                   WHERE COALESCE(ad.entity,'')=''
               ) GROUP BY a ORDER BY pri LIMIT ?""",
            (coin, ATTRIB_MAX_CANDIDATES))
        cands = [r["a"] for r in await cur.fetchall()]
    want = "A" if side == "ask" else "B"
    dex = coin_dex(coin)
    pad = (px_hi - px_lo) * 0.1 + px_hi * 0.001  # küçük tolerans
    for addr in cands:
        try:
            orders = await client.frontend_open_orders(addr, dex)
        except Exception as e:
            log.debug("openOrders %s: %s", addr, e)
            continue
        total = 0.0
        for o in orders or []:
            try:
                if (o.get("coin") or "") != coin:
                    continue
                oside = o.get("side") or ""
                px = float(o.get("limitPx") or 0)
                sz = float(o.get("sz") or o.get("origSz") or 0)
            except (TypeError, ValueError):
                continue
            if oside != want or px <= 0 or sz <= 0:
                continue
            if px_lo - pad <= px <= px_hi + pad:
                total += px * sz
        if wall_ntl > 0 and total >= wall_ntl * 0.5:
            log.info("duvar sahibi bulundu: %s %s %s (emirleri %s)",
                     coin, side, addr, fmt.usd(total))
            return addr
    return None


async def big_coin_set(cfg: Config) -> set[str]:
    """Hacimce top-N hisse — 'büyük sınıf'ın hisse ayağı (endeks/emtia/kripto
    assets.kind == 'non_equity' ile ayrıca yakalanır). Liq kümesi kademesi de
    bunu kullanır."""
    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol FROM tickers")
        coins = [(r["coin"], r["symbol"]) for r in await cur.fetchall()]
    mets = await latest_metrics_all()
    # HACMİ BİLİNMEYEN coin "büyük sınıf" SAYILMAZ. Eskiden yalnız sıralanıyordu:
    # metrik tablosu boşken (ilk açılış ya da metrik görevi düşükken) hepsinin
    # hacmi 0 olur, sıralama rastgeleleşir ve gelişigüzel 10 coin dev muamelesi
    # görüp yüksek alarm tabanına düşerdi — küçük hissedeki gerçek sinyal
    # sessizce kısılırdı.
    eq_by_vol = sorted(
        (c for c, s in coins
         if assets.kind(s) != "non_equity" and ((mets.get(c) or {}).get("day_volume") or 0) > 0),
        key=lambda c: (mets.get(c) or {}).get("day_volume") or 0, reverse=True)
    return set(eq_by_vol[: int(cfg.wall_big_top_n)])


async def scan_walls(cfg: Config, client: HLClient, notifier) -> int:
    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol FROM tickers")
        coins = [(r["coin"], r["symbol"]) for r in await cur.fetchall()]
    mets = await latest_metrics_all()
    big_coins = await big_coin_set(cfg)

    ts = now()
    n_alerts = 0
    n_bookerr = 0
    first_err = ""
    for coin, symbol in coins:
        from ..health import beat
        await beat("bookwall")  # ilerleme nabzı
        try:
            book = await client.l2_book(coin)
        except Exception as e:
            # debug seviyesinde SESSİZDİ: defter hiç alınamazsa duvar radarı
            # sıfır sonuç üretir ama nabız attığı için sağlık YEŞİL kalır —
            # "çalışıyor ama hiçbir şey bulmuyor" ile ayırt edilemezdi.
            n_bookerr += 1
            if not first_err:
                first_err = f"{type(e).__name__}: {e}"[:160]
                log.warning("l2Book alınamadı (%s): %s", coin, e)
            continue
        rec_min, alert_min = floors(cfg, symbol, coin, big_coins)
        bids, asks = _parse_book(book)
        walls = find_wall(bids, asks, cfg.wall_window_pct, rec_min)
        found_sides = {w["side"] for w in walls}
        day_vol = (mets.get(coin) or {}).get("day_volume")

        for w in walls:
            n_alerts += await _upsert_and_alert(cfg, client, notifier, coin, symbol,
                                                w, ts, alert_min, day_vol)

        # görünmeyen aktif duvarlar: last_ts eskidiyse kapat + gerekirse "kalktı" de
        async with db() as conn:
            cur = await conn.execute(
                "SELECT * FROM book_walls WHERE coin=? AND active=1", (coin,))
            actives = [dict(r) for r in await cur.fetchall()]
        for a in actives:
            if a["side"] in found_sides:
                continue
            if ts - (a["last_ts"] or 0) < GONE_GRACE * cfg.wall_poll_sec:
                continue  # tek taramalık boşluk olabilir — sabret
            async with db() as conn:
                await conn.execute(
                    "UPDATE book_walls SET active=0 WHERE id=?", (a["id"],))
            if a.get("alerted") and (a.get("peak_notional") or 0) >= alert_min:
                await notifier.send("wall", fmt.wall_gone({**a, "symbol": symbol}),
                                    priority="normal", key=f"gone:{coin}:{a['side']}")
            log.info("duvar kalktı: %s %s (tepe %s)", symbol, a["side"],
                     fmt.usd(a.get("peak_notional")))
    if coins and n_bookerr >= len(coins):
        log.warning("duvar taraması TAMAMEN başarısız (%d/%d coin) — defter"
                    " alınamadı. İlk hata: %s", n_bookerr, len(coins), first_err)
    from ..db import kv_set
    await kv_set("wall_stats", {"coins": len(coins), "book_err": n_bookerr,
                                "err_msg": first_err, "alerts": n_alerts, "ts": ts})
    return n_alerts


async def _upsert_and_alert(cfg: Config, client: HLClient, notifier,
                            coin: str, symbol: str, w: dict, ts: int,
                            alert_min: float, day_vol: float | None) -> int:
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM book_walls WHERE coin=? AND side=? AND active=1",
            (coin, w["side"]))
        row = await cur.fetchone()
        if row:
            peak = max(row["peak_notional"] or 0, w["notional"])
            await conn.execute(
                """UPDATE book_walls SET px_lo=?, px_hi=?, sz=?, notional=?,
                     dist_pct=?, mark_px=?, last_ts=?, peak_notional=? WHERE id=?""",
                (w["px_lo"], w["px_hi"], w["sz"], w["notional"], w["dist_pct"],
                 w["mark"], ts, peak, row["id"]))
            wall_id, alerted = row["id"], row["alerted"]
        else:
            cur = await conn.execute(
                """INSERT INTO book_walls(coin,side,px_lo,px_hi,sz,notional,dist_pct,
                     mark_px,first_ts,last_ts,peak_notional,alerted,active)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,0,1)""",
                (coin, w["side"], w["px_lo"], w["px_hi"], w["sz"], w["notional"],
                 w["dist_pct"], w["mark"], ts, ts, w["notional"]))
            wall_id, alerted = cur.lastrowid, 0

    if alerted or not notifier or w["notional"] < alert_min:
        return 0
    key = f"{coin}:{w['side']}"
    # Cooldown'da alerted=1 YAZMA: eskiden cooldown'a takılan duvar kalıcı olarak
    # alerted işaretlenip bir daha değerlendirilmiyor, kalkınca da hiç anons
    # edilmemiş duvara sahte '🧱❌ Duvar kalktı' gidiyordu (spoof çek-tekrar-koy
    # paterni kaçıyordu). Cooldown sadece bu turu atlar; duvar aktif kalır.
    if await alert_recent("wall", key, WALL_COOLDOWN):
        return 0

    addr = None
    try:
        addr = await _attribute(cfg, client, coin, w["side"],
                                w["px_lo"], w["px_hi"], w["notional"])
    except Exception:
        log.exception("duvar sahipliği aranamadı: %s", coin)
    text = fmt.wall_alert({**w, "symbol": symbol, "address": addr}, day_vol)
    # alerted=1'i YALNIZ başarılı gönderimden sonra yaz — 429/400'de duvar aktif
    # kaldığı sürece bir daha bildirilmiyordu (send'den ÖNCE işaretleniyordu).
    if await notifier.send("wall", text, key=key):
        async with db() as conn:
            await conn.execute(
                "UPDATE book_walls SET alerted=1, address=? WHERE id=?", (addr, wall_id))
        await alert_log("wall", key, text)
        log.info("🧱 duvar alarmı: %s %s %s", symbol, w["side"], fmt.usd(w["notional"]))
        return 1
    # gönderilemedi ama sahibi bulduysak kaydet (bir dahakine tekrar aramayalım)
    if addr:
        async with db() as conn:
            await conn.execute("UPDATE book_walls SET address=? WHERE id=?", (addr, wall_id))
    return 0


async def loop(cfg: Config, client: HLClient, notifier) -> None:
    await asyncio.sleep(60)  # evren keşfini bekle
    log.info("duvar radarı başladı (pencere %%%.1f, alarm tabanı $%.1fM)",
             cfg.wall_window_pct, cfg.wall_alert_min_usd / 1e6)
    while True:
        try:
            from ..health import beat
            await beat("bookwall")  # turbaşı: uzun tarama sahte alarm üretmesin
            await scan_walls(cfg, client, notifier)
            await beat("bookwall")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("duvar taraması hatası")
        await asyncio.sleep(cfg.wall_poll_sec)
