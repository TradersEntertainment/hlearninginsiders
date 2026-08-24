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
        # Pencere 6 gün değil 30 gün: bot birkaç gün kapalı kaldıysa (Railway
        # restart) o dönemin event'leri 6 günü aşınca sonsuza dek evaluated=0
        # 'zombi' kalıyordu — sicile hiç işlenmez, /gecmis'te görünmez, check_due
        # her 60 sn hepsini yeniden tarardı. _evaluate metrik olmasa bile kapatır.
        cur = await conn.execute(
            """SELECT * FROM earnings_events
               WHERE evaluated=0 AND (alerted_t1=1 OR alerted_pre=1)
               AND date_et >= date('now','-30 day')""")
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
    # 'Sonra' fiyatı T+24h olmalı — 'en güncel' değil. Değerlendirme geç koşarsa
    # (bot kapalıydı) latest_metric T+2..6 günün fiyatını alıp yanlış hüküm
    # arşivliyordu (gap sonrası dönüş → doğru isabetçiler 'yanıldı' sayılır).
    after = await metrics.metric_at(coin, est + 24 * 3600)
    # T+24h civarında ölçüm yoksa (veri boşluğu) 'sonra' fiyatı earnings ÖNCESİNE
    # düşebilir → geçersiz. En az 12h sonrası olmalı; yoksa latest'e düş.
    if not after or (after.get("ts") or 0) < est + 12 * 3600:
        latest = await metrics.latest_metric(coin)
        after = latest if latest and (latest.get("ts") or 0) >= est + 12 * 3600 else None

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

    # Market maker / vault'lar asla sicile girmez (insider değil)
    entity_addrs: set[str] = set()
    if snaps:
        addrs = list({s["address"] for s in snaps})
        q = ",".join("?" * len(addrs))
        async with db() as conn:
            cur = await conn.execute(
                f"SELECT address FROM addresses WHERE COALESCE(entity,'')<>''"
                f" AND address IN ({q})", addrs)
            entity_addrs = {r["address"] for r in await cur.fetchall()}

    # Sadece ANLAMLI + insan pozisyonları sicile/watchlist'e girer. $27K short
    # "tutturdu" diye insider sayılmaz; MM/vault hiç sayılmaz.
    qual = [s for s in snaps
            if (s.get("notional") or 0) >= cfg.eval_min_notional
            and s["address"] not in entity_addrs]

    results = []
    promoted = []
    if move_pct is not None and abs(move_pct) >= cfg.eval_move_threshold and qual:
        direction = "up" if move_pct > 0 else "down"
        async with db() as conn:
            for s in qual:
                hit = (s["side"] == "long" and direction == "up") or \
                      (s["side"] == "short" and direction == "down")
                col = "hits" if hit else "misses"
                # Adres satırı yoksa oluştur — leaderboard/sweeper yoluyla (fill'siz)
                # gelen balinaların addresses satırı olmadığından UPDATE no-op'tu:
                # tam da 'uyuyan dev' insider'ların sicili hiç oluşmuyordu.
                await conn.execute(
                    "INSERT INTO addresses(address, first_seen) VALUES(?,?)"
                    " ON CONFLICT(address) DO NOTHING", (s["address"], now()))
                await conn.execute(
                    f"UPDATE addresses SET {col}={col}+1 WHERE address=?", (s["address"],))
                cur = await conn.execute(
                    "SELECT hits, watchlist FROM addresses WHERE address=?", (s["address"],))
                row = await cur.fetchone()
                if row and row["hits"] >= 2 and not row["watchlist"]:
                    await conn.execute(
                        "UPDATE addresses SET watchlist=1 WHERE address=?", (s["address"],))
                    promoted.append(s["address"])
                if hit:
                    # bu adres BU hissede kazandı — tekrar dönerse bildirmek için sakla
                    await conn.execute(
                        "INSERT OR REPLACE INTO address_wins(address,coin,event_id,notional,ts)"
                        " VALUES(?,?,?,?,?)",
                        (s["address"], coin, ev["id"], s["notional"], now()))
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
