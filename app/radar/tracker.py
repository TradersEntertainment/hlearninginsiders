"""Pozisyon kapanış takibi ("exit tracker").

Earnings saati geçer geçmez Telegram'dan sorar: "balinanın çıkışını takip
edelim mi?" — /takip_N'e basınca CANLI pozisyon baz alınır ve balina toplam
boyutun her %X'ini kapattıkça / ekledikçe bildirim gelir. Her transaction
değil, anlamlı adımlar haber verilir (spam yok). Tam kapanış ve yön değişimi
kritik önceliklidir (sessiz saatte bile gelir).

Takip ADET (szi) üzerinden yürür, $ üzerinden değil — fiyat oynayınca
notional değişir ama balina bir şey kapatmış olmaz.
"""
import logging

from ..config import Config
from ..db import db, now
from ..earnings.calendar import event_ts_estimate
from ..hl.client import HLClient
from ..hl.universe import symbol_of
from ..telegram import format as fmt
from .report import coin_dex

log = logging.getLogger("radar.tracker")

# earnings tahmini saatinden bu kadar sonra teklif gider ("earning bitti geçti")
OFFER_DELAY_SEC = 20 * 60
# bundan eski earnings için artık teklif üretme (bot uzun süre kapalıymış)
OFFER_MAX_AGE_SEC = 2 * 86400
# teklif başına en fazla bu kadar balina listelenir
OFFER_LIMIT = 4
# base'in bu oranının altına inen poz "tamamen kapandı" sayılır
CLOSED_EPS = 0.02


async def live_position(client: HLClient, address: str, coin: str) -> dict | None:
    """Adresin bu coindeki canlı pozisyonu; yoksa None."""
    state = await client.clearinghouse(address, coin_dex(coin))
    target_sym = symbol_of(coin)
    for ap in (state or {}).get("assetPositions") or []:
        p = ap.get("position") or {}
        pcoin = p.get("coin") or ""
        if pcoin != coin and symbol_of(pcoin) != target_sym:
            continue
        try:
            szi = float(p.get("szi") or 0)
        except (TypeError, ValueError):
            continue
        if szi == 0:
            continue
        return {
            "szi": szi,
            "side": "long" if szi > 0 else "short",
            "notional": abs(float(p.get("positionValue") or 0)),
            "entry_px": float(p.get("entryPx") or 0),
            "upnl": float(p.get("unrealizedPnl") or 0),
        }
    return None


# ---------------- teklif: "takip edelim mi?" ----------------

async def make_offers(cfg: Config, client: HLClient, notifier) -> int:
    """Raporu gitmiş, saati geçmiş earnings'ler için takip teklifi gönder."""
    async with db() as conn:
        cur = await conn.execute(
            """SELECT * FROM earnings_events
               WHERE (alerted_t1=1 OR alerted_pre=1) AND COALESCE(offer_sent,0)=0""")
        events = [dict(r) for r in await cur.fetchall()]
    ts = now()
    sent = 0
    for ev in events:
        est = event_ts_estimate(ev)
        if not est or ts < est + OFFER_DELAY_SEC:
            continue  # daha açıklanmadı / yeni açıklandı
        try:
            sent += await _offer_for_event(
                cfg, client, notifier, ev, stale=(ts - est > OFFER_MAX_AGE_SEC))
        except Exception:
            log.exception("takip teklifi hazırlanamadı: %s", ev.get("symbol"))
    return sent


async def _offer_for_event(cfg: Config, client: HLClient, notifier,
                           ev: dict, stale: bool) -> int:
    # Önce işaretle — hata olsa bile 60 sn'de bir tekrar tekrar denenmesin
    async with db() as conn:
        await conn.execute(
            "UPDATE earnings_events SET offer_sent=1 WHERE id=?", (ev["id"],))
    if stale:
        return 0

    # Adaylar: earnings öncesi snapshot'taki anlamlı insan pozları (skor sırasıyla)
    async with db() as conn:
        cur = await conn.execute(
            """SELECT s.address, s.side, s.notional, s.score
               FROM position_snapshots s
               LEFT JOIN addresses a ON a.address = s.address
               WHERE s.event_id=? AND s.phase IN ('T-1h','pre')
                 AND s.notional >= ? AND COALESCE(a.entity,'')=''
               ORDER BY COALESCE(s.score,0) DESC, s.notional DESC""",
            (ev["id"], cfg.eval_min_notional))
        rows = [dict(r) for r in await cur.fetchall()]
    seen: set[str] = set()
    cands = []
    for r in rows:
        if r["address"] in seen:
            continue
        seen.add(r["address"])
        cands.append(r)
        if len(cands) >= OFFER_LIMIT:
            break
    if not cands:
        return 0

    # Canlı durumlarına bak: hâlâ açık olanlara teklif, kapatmış olanlara not
    offers, already_closed = [], []
    for c in cands:
        try:
            live = await live_position(client, c["address"], ev["coin"])
        except Exception as e:
            log.warning("canlı poz okunamadı %s: %s", c["address"], e)
            offers.append({**c, "live": None})  # tap anında tekrar denenir
            continue
        if live is None:
            already_closed.append(c)
        else:
            # CANLI yönü kullan — earnings öncesi snapshot'ın side'ı, balina rapor
            # sonrası flip ettiyse yanlış olur (teklif '🟢LONG … şu an $3.2M' derken
            # gerçekte SHORT). Kullanıcı takip kararını bu mesajla veriyor.
            offers.append({**c, "side": live["side"], "live": live})

    if offers:
        async with db() as conn:
            for o in offers:
                cur = await conn.execute(
                    """INSERT INTO track_offers(address,coin,symbol,side,notional,created_ts)
                       VALUES(?,?,?,?,?,?)""",
                    (o["address"], ev["coin"], ev["symbol"], o["side"],
                     o["notional"], now()))
                o["offer_id"] = cur.lastrowid

    if not offers and not already_closed:
        return 0
    text = fmt.track_offer(ev["symbol"], offers, already_closed, cfg)
    await notifier.send("track", text, key=f"offer:{ev['symbol']}:{ev['date_et']}")
    log.info("%s: %d takip teklifi gönderildi (%d zaten kapatmış)",
             ev["symbol"], len(offers), len(already_closed))
    return 1


# ---------------- aktif takip kontrolü ----------------

async def check_trackers(cfg: Config, client: HLClient, notifier) -> None:
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM trackers WHERE active=1")
        trackers = [dict(r) for r in await cur.fetchall()]
    ts = now()
    for t in trackers:
        try:
            await _check_one(cfg, client, notifier, t, ts)
        except Exception:
            log.exception("takip kontrol hatası #%s %s", t.get("id"), t.get("symbol"))


async def _check_one(cfg: Config, client: HLClient, notifier, t: dict, ts: int) -> None:
    live = await live_position(client, t["address"], t["coin"])
    base = float(t["base_szi"] or 0)
    last = float(t["last_szi"] if t["last_szi"] is not None else base)
    # track_step_pct ≤0 girilirse adım bildirimleri sessizce tamamen kapanıyordu
    # (panelde alt sınır yok) — varsayılan %10'a düş.
    step_pct = float(cfg.track_step_pct)
    if step_pct <= 0:
        step_pct = 10.0
    step = base * step_pct / 100 if base > 0 else 0

    async with db() as conn:
        await conn.execute(
            "UPDATE trackers SET last_check_ts=? WHERE id=?", (ts, t["id"]))

    # NOT: state (active=0 / yön / last_szi) YALNIZ başarılı gönderimden SONRA
    # yazılır. Eskiden önce yazılıp send sonucu yok sayılıyordu: Telegram anlık
    # 502/429 verirse tracker kapanıp kritik 'TAMAMEN KAPATTI'/flip KALICI
    # kayboluyordu (bir daha taranmaz). Şimdi gönderilemezse state korunur,
    # sonraki turda yeniden denenir.

    # 1) Tamamen kapanmış → kritik bildirim, takip biter
    if live is None or (base > 0 and abs(live["szi"]) <= base * CLOSED_EPS):
        ok = await notifier.send("track", fmt.track_closed(t, base, last),
                                 priority="critical", key=f"closed:{t['id']}")
        if ok:
            async with db() as conn:
                await conn.execute(
                    "UPDATE trackers SET active=0, last_szi=0, end_note='kapandı' WHERE id=?",
                    (t["id"],))
            log.info("takip #%s: %s pozisyonu TAMAMEN kapandı", t["id"], t["symbol"])
        return

    cur_szi = abs(live["szi"])

    # 2) Yön değişimi (long→short / short→long) → kritik, takip yeni yönle sürer
    if live["side"] != t["side"]:
        ok = await notifier.send("track", fmt.track_flip(t, live), priority="critical",
                                 key=f"flip:{t['id']}:{live['side']}")
        if ok:
            async with db() as conn:
                await conn.execute(
                    """UPDATE trackers SET side=?, base_szi=?, last_szi=?, base_notional=?
                       WHERE id=?""",
                    (live["side"], cur_szi, cur_szi, live["notional"], t["id"]))
        return

    # 3) Anlamlı adım: son bildirimden beri TOPLAM boyutun %X'i kadar değişim.
    #    Spam önleyici — her transaction değil, birikmiş anlamlı fark bildirir.
    if step > 0 and abs(cur_szi - last) >= step:
        ok = await notifier.send("track", fmt.track_step(t, live, base, last, cur_szi),
                                 key=f"step:{t['id']}:{cur_szi:.4f}")
        if ok:  # last_szi yalnız gönderilince ilerler (kaçan adım tekrar denenir)
            async with db() as conn:
                await conn.execute(
                    "UPDATE trackers SET last_szi=? WHERE id=?", (cur_szi, t["id"]))
        return

    # 4) Süre doldu (poz hâlâ açık) → bilgilendir, devam için yeni teklif bırak
    if ts > int(t["expires_ts"] or 0):
        async with db() as conn:
            await conn.execute(
                "UPDATE trackers SET active=0, end_note='süre doldu' WHERE id=?",
                (t["id"],))
            cur = await conn.execute(
                """INSERT INTO track_offers(address,coin,symbol,side,notional,created_ts)
                   VALUES(?,?,?,?,?,?)""",
                (t["address"], t["coin"], t["symbol"], live["side"],
                 live["notional"], ts))
            offer_id = cur.lastrowid
        await notifier.send("track", fmt.track_expired(t, live, base, offer_id),
                            priority="normal", key=f"expire:{t['id']}")
