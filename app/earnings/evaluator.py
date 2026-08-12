"""Earnings sonrası değerlendirme (T+24h).

Fiyat hareketi yönünü ölçer, T-1h snapshot'ındaki her adresi doğru/yanlış
olarak siciline işler, 2+ doğru bilenleri watchlist'e alır, kapananları
raporlar. Botun "öğrenen hafızası" burasıdır.
"""
import logging

from ..config import Config
from ..db import db, now
from ..hl.client import HLClient
from ..radar import metrics, scanner
from ..radar.report import coin_dex
from ..telegram import format as fmt
from .calendar import event_ts_estimate

log = logging.getLogger("earnings.evaluator")


async def evaluate_due(cfg: Config, client: HLClient, notifier) -> None:
    cutoff = now()
    async with db() as conn:
        cur = await conn.execute(
            """SELECT * FROM earnings_events
               WHERE evaluated=0 AND (alerted_t1=1 OR alerted_pre=1)
               AND date_et >= date('now','-6 day')""")
        events = [dict(r) for r in await cur.fetchall()]
    for ev in events:
        est = event_ts_estimate(ev)
        if cutoff < est + 24 * 3600:
            continue
        try:
            await _evaluate(cfg, client, notifier, ev, est)
        except Exception as e:
            log.warning("değerlendirme hatası %s: %s", ev["symbol"], e)


async def _evaluate(cfg: Config, client: HLClient, notifier, ev: dict, est: int) -> None:
    coin = ev["coin"]
    before = await metrics.metric_at(coin, est - 300)
    after = await metrics.latest_metric(coin)

    async with db() as conn:
        cur = await conn.execute(
            """SELECT * FROM position_snapshots WHERE event_id=? AND phase='T-1h'
               ORDER BY notional DESC""", (ev["id"],))
        snaps = [dict(r) for r in await cur.fetchall()]
        if not snaps:
            cur = await conn.execute(
                """SELECT * FROM position_snapshots WHERE event_id=? AND phase='pre'
                   ORDER BY notional DESC""", (ev["id"],))
            snaps = [dict(r) for r in await cur.fetchall()]

    move_pct = None
    if before and after and before["mark_px"]:
        move_pct = (after["mark_px"] - before["mark_px"]) / before["mark_px"] * 100

    # Sadece ANLAMLI pozisyonlar sicile/watchlist'e girer — $27K short "tutturdu"
    # diye insider sayılmasın. Küçük pozisyonlar tamamen elenir.
    qual = [s for s in snaps if (s.get("notional") or 0) >= cfg.eval_min_notional]

    results = []
    promoted = []
    if move_pct is not None and abs(move_pct) >= cfg.eval_move_threshold and qual:
        direction = "up" if move_pct > 0 else "down"
        async with db() as conn:
            for s in qual:
                hit = (s["side"] == "long" and direction == "up") or \
                      (s["side"] == "short" and direction == "down")
                col = "hits" if hit else "misses"
                await conn.execute(
                    f"UPDATE addresses SET {col}={col}+1 WHERE address=?", (s["address"],))
                cur = await conn.execute(
                    "SELECT hits, watchlist FROM addresses WHERE address=?", (s["address"],))
                row = await cur.fetchone()
                if row and row["hits"] >= 2 and not row["watchlist"]:
                    await conn.execute(
                        "UPDATE addresses SET watchlist=1 WHERE address=?", (s["address"],))
                    promoted.append(s["address"])
                results.append({**s, "hit": hit})

    # Kim kapattı? — güncel durumu tekrar tara ve T+24h snapshot'ı al (yalnız anlamlı pozlar)
    closed = []
    try:
        rows_now = await scanner.scan(cfg, client, coin, coin_dex(coin))
        await scanner.snapshot(ev["id"], "T+24h", rows_now)
        still = {p["address"] for p in rows_now}
        closed = [s["address"] for s in qual if s["address"] not in still]
    except Exception as e:
        log.warning("T+24h taraması başarısız %s: %s", coin, e)

    # ---- Arşiv notu: earnings öncesi en büyük ANLAMLI poz kimdi, haklı çıktı mı ----
    note = None
    if qual:
        top = qual[0]
        side_txt = "SHORT" if top["side"] == "short" else "LONG"
        who = f"{top['address'][:8]}..{top['address'][-4:]}"
        size = fmt.usd(top["notional"])
        if move_pct is None:
            note = f"En büyük poz {side_txt} {size} ({who}) — sonuç ölçülemedi"
        elif abs(move_pct) < cfg.eval_move_threshold:
            note = (f"En büyük poz {side_txt} {size} ({who}) — fiyat %{move_pct:+.1f},"
                    " hareket eşiğin altında")
        else:
            hit = (top["side"] == "short" and move_pct < 0) or \
                  (top["side"] == "long" and move_pct > 0)
            verdict = "✅ DOĞRU BİLDİ (insider olabilir)" if hit else "❌ yanılmış"
            n_right = sum(1 for r in results if r["hit"])
            note = (f"En büyük poz {side_txt} {size} ({who}) → fiyat %{move_pct:+.1f}"
                    f" · {verdict} · {n_right}/{len(results)} adres doğru")

    async with db() as conn:
        await conn.execute(
            "UPDATE earnings_events SET evaluated=1, move_pct=?, result_note=? WHERE id=?",
            (move_pct, note, ev["id"]))

    # Anlamlı poz yoksa rapor gönderme — $18K'lık "sonuç" gürültüsü olmasın
    if notifier and qual:
        text = fmt.eval_report(ev, move_pct, results, closed, promoted, cfg)
        await notifier.send("eval", text, key=f"{ev['symbol']}:{ev['date_et']}")
    log.info("%s değerlendirildi: hareket %s, %d anlamlı poz işlendi, %d watchlist'e alındı"
             " (%d küçük poz elendi)",
             ev["symbol"], f"%{move_pct:.1f}" if move_pct is not None else "?",
             len(results), len(promoted), len(snaps) - len(qual))
