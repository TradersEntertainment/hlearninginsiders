"""TWAP emri takibi — "bu emri izle, iptal edilirse haber ver".

Kullanıcı isteği: "bir twap emri geldiğinde takip butonu olsun, iptal edildiğinde
de bildirim istiyorum" — ve bildirim **yalnız takip ettikleri** için gelsin, kanal
sessiz kalsın.

NEDEN AYRI BİR HAT (mevcut `/takip_N` pozisyon takibi yetmiyor):
  • `tracker` bir POZİSYONU izler ve `live_position` ile canlı perp defterine
    bakar. TWAP'ta izlenecek şey EMRİN KENDİSİDİR; spot TWAP'ta (@107) pozisyon
    diye bir şey yoktur ve o akış "zaten kapatmış" derdi.
  • Emrin durumu bellekteki `Run`'dan değil HL EMİR GEÇMİŞİNDEN öğrenilir:
    `twaplive.REG` 30 dk boşta kalan turu düşürür (`IDLE_SEC`), iptal ondan sonra
    gelirse bellekte karşılığı kalmaz. Burası DB'den okur, belleğe bağımlı değil.

Kanal notunun (`twap_alert_end_note`) aksine İŞARET GÖNDERİMDEN SONRA yazılır:
başarısız gönderim takibi kapatmaz, bir sonraki tur yeniden dener.

SAF DEĞİL: DB okur/yazar ve `collector.fetch_twap_history` ile HL'ye sorar —
ama yalnız AKTİF takipler için, adres başına TEK istek.
"""
from __future__ import annotations

import logging

from ..db import db, now

log = logging.getLogger("radar.twapfollow")

HALF_PCT = 50.0           # "yarılandı" eşiği — twaplive ile aynı
DONE = ("finished", "terminated", "error")
CANCELLED = ("terminated", "error")


async def start(cfg, coin: str, address: str, side: str, first_ts: int,
                chat_id: str = "") -> int:
    """Takibi aç, id döner. Aynı tur ikinci kez takip edilmez (UNIQUE)."""
    ts = now()
    days = int(getattr(cfg, "twap_follow_expire_days", 7) or 7)
    async with db() as conn:
        cur = await conn.execute(
            "SELECT id, active FROM twap_follows WHERE coin=? AND address=? AND side=? AND first_ts=?",
            (coin, address, side, int(first_ts)))
        row = await cur.fetchone()
        if row:
            # Yeniden basıldı: kapanmışsa uyandır, süreyi uzat (yeni kayıt açma).
            await conn.execute(
                "UPDATE twap_follows SET active=1, expires_ts=?, end_note=NULL WHERE id=?",
                (ts + days * 86400, row["id"]))
            return int(row["id"])
        cur = await conn.execute(
            """INSERT INTO twap_follows(coin,address,side,first_ts,chat_id,created_ts,
                 expires_ts,active,last_check_ts) VALUES(?,?,?,?,?,?,?,1,?)""",
            (coin, address, side, int(first_ts), chat_id or "", ts,
             ts + days * 86400, ts))
        return int(cur.lastrowid)


async def stop(follow_id: int, note: str = "bırakıldı") -> bool:
    async with db() as conn:
        cur = await conn.execute(
            "UPDATE twap_follows SET active=0, end_note=? WHERE id=? AND active=1",
            (note, int(follow_id)))
        return bool(cur.rowcount)


async def active(chat_id: str | None = None) -> list[dict]:
    q = "SELECT * FROM twap_follows WHERE active=1"
    args: list = []
    if chat_id is not None:
        q += " AND chat_id=?"
        args.append(chat_id)
    async with db() as conn:
        cur = await conn.execute(q + " ORDER BY created_ts DESC", tuple(args))
        return [dict(r) for r in await cur.fetchall()]


async def check(cfg, collector, notifier) -> dict:
    """Bir tur: aktif takiplerin emirlerini sor, durum değiştiyse haber ver.

    Dönüş sayaçları `/tani` ve `/twap` teşhisinde görünür."""
    out = {"active": 0, "checked": 0, "asked": 0, "ended": 0, "cancelled": 0,
           "progress": 0, "expired": 0, "failed": 0, "no_order": 0}
    if not getattr(cfg, "twap_follow_enabled", True):
        return out
    from ..telegram import format as fmt
    from . import twaplive
    ts = now()
    rows = await active()
    out["active"] = len(rows)
    if not rows:
        return out
    # Süresi dolanlar sessizce kapanır (emir çoktan bitmiş de olabilir).
    for f in [r for r in rows if r["expires_ts"] and ts >= int(r["expires_ts"])]:
        await stop(f["id"], "süre doldu")
        out["expired"] += 1
    rows = [r for r in rows if not (r["expires_ts"] and ts >= int(r["expires_ts"]))]
    if collector is None:
        return out                      # emir sorulamaz → tahminle bildirim YOK

    by_addr: dict[str, list[dict]] = {}
    for f in rows:
        by_addr.setdefault(f["address"], []).append(f)
    for addr, fl in by_addr.items():
        try:
            hist = await collector.fetch_twap_history(addr)
        except Exception:
            log.debug("emir geçmişi alınamadı (%s)", addr[:10], exc_info=True)
            hist = None
        out["asked"] += 1
        if hist is None:
            continue
        for f in fl:
            out["checked"] += 1
            try:
                await _one(cfg, f, hist, ts, notifier, fmt, twaplive, out)
            except Exception:
                log.exception("takip kontrolü %s %s", f["coin"], addr[:10])
    return out


async def _one(cfg, f: dict, hist: list, ts: int, notifier, fmt, twaplive, out: dict) -> None:
    orders = twaplive.parse_twap_orders(hist, f["coin"], f["side"], None, ts)
    # Aynı coin+yönde birden çok emir olabilir: takibin turuna zamanca en yakını.
    o = min(orders, key=lambda x: abs(int(x.get("started_ts") or 0) - int(f["first_ts"])),
            default=None) if orders else None
    if not o:
        out["no_order"] += 1
        return
    status = (o.get("status") or "").lower()
    filled = float(o.get("filled_pct") or 0)

    async def _mark(**fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        async with db() as conn:
            await conn.execute(f"UPDATE twap_follows SET {sets}, last_check_ts=? WHERE id=?",
                               (*fields.values(), ts, f["id"]))

    m = await _measure(f, o)
    if status in DONE:
        cancelled = status in CANCELLED
        ctx = {"order": o, "day_vol": m.get("day_volume"), "cancelled": cancelled,
               "klass": twaplive.klass_of(f["coin"]), "follow": True}
        # Başlık `twap_end`'in kendisinde (⛔ / 🏁) — burada yalnız "neden bu mesajı
        # aldın" satırı. İkisini birden yazmak başlığı çiftliyordu.
        text = "👁 <b>takip ettiğin emir</b>\n" + fmt.twap_end(m, ctx)
        ok = await notifier.send("track", text, priority="critical",
                                 key=f"twapfollow:{f['id']}:{status}", chat_id=f["chat_id"] or "")
        if not ok:
            out["failed"] += 1
            return                      # İŞARET YOK: sonraki tur yeniden dener
        await _mark(active=0, last_status=status,
                    end_note="iptal" if cancelled else "bitti")
        out["cancelled" if cancelled else "ended"] += 1
        return
    if (getattr(cfg, "twap_follow_progress", True) and not f.get("half_ts")
            and filled >= HALF_PCT and status == "activated"):
        ctx = {"order": o, "day_vol": m.get("day_volume"), "klass": twaplive.klass_of(f["coin"])}
        text = "👁 <b>takip ettiğin emir</b>\n" + fmt.twap_progress(m, ctx)
        if await notifier.send("track", text, priority="high",
                               key=f"twapfollow:{f['id']}:half", chat_id=f["chat_id"] or ""):
            await _mark(half_ts=ts, last_status=status)
            out["progress"] += 1
        else:
            out["failed"] += 1
        return
    await _mark(last_status=status)


async def _measure(f: dict, o: dict) -> dict:
    """Mesaj için skaler ölçüm — `twap_runs` satırından (bellek gerekmez)."""
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM twap_runs WHERE coin=? AND address=? AND side=? AND first_ts=?",
            (f["coin"], f["address"], f["side"], f["first_ts"]))
        r = dict(await cur.fetchone() or {})
    px_a, px_b = r.get("px_first"), r.get("px_last")
    return {"coin": f["coin"], "address": f["address"], "side": f["side"],
            "n": r.get("n_slices") or 0, "total": r.get("total") or 0.0,
            "sz_total": r.get("sz_total") or 0.0,
            "dur": int((r.get("last_ts") or 0) - (r.get("first_ts") or 0)),
            "avg_slice": r.get("avg_slice"), "median_gap": r.get("avg_gap"),
            "taker_pct": r.get("taker_pct"), "day_volume": r.get("day_volume"),
            "px_first": px_a, "px_last": px_b,
            "px_chg_pct": ((px_b - px_a) / px_a * 100) if px_a and px_b else 0.0}


async def prune() -> int:
    """Kapanmış takipler 30 gün sonra silinir (twap_runs budamasıyla aynı ufuk)."""
    async with db() as conn:
        cur = await conn.execute(
            "DELETE FROM twap_follows WHERE active=0 AND created_ts < ?", (now() - 30 * 86400,))
        return cur.rowcount or 0


async def loop(cfg, collector, notifier) -> None:
    """Denetimli döngü. Site ASLA buna bağımlı değil."""
    import asyncio

    from ..db import kv_set
    from ..health import beat
    await asyncio.sleep(90)
    while True:
        try:
            out = await check(cfg, collector, notifier)
            await kv_set("twapfollow_stats", {**out, "ts": now()})
            if out["cancelled"] or out["ended"] or out["progress"]:
                log.info("twap takip: %d iptal, %d bitti, %d yarılandı (%d aktif)",
                         out["cancelled"], out["ended"], out["progress"], out["active"])
            await prune()
            beat("twapfollow")
        except Exception:
            log.exception("twap takip turu")
        await asyncio.sleep(max(30, int(getattr(cfg, "twap_follow_poll_sec", 120) or 120)))
