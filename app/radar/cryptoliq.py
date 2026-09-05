"""Kripto liq yakını — "ana dexte büyük bir pozisyon patlamak üzere".

Kullanıcı kuralı: BTC/ETH HARİÇ ana dex kriptoda notional ≥ $500K ve
likidasyon fiyatı şimdiye ≤ %2,5 → kripto kanalına (CRYPTO_CHAT_ID) mesaj.

KAYNAK `addr_positions`: süpürme her adresin TÜM pozisyonlarını her boyutta
yazıyor (`hl_positions` yalnız ≥$20M kripto tutar, $500K için kördür). Tur
75–125 dk; bu yüzden mesaj gitmeden önce adayın defteri CANLI çekilir
(`sweeper.probe_address`) — bir saat önce kapanmış pozisyon için "patlamak
üzere" demek yalan olur. Sonda başarısızsa mesaj ölçümün yaşını yazar.

MESAJ COİN BAŞINA TEK: bir çöküşte 30 pozisyon aynı anda eşiğe girer, 30 ayrı
mesaj spam'dir (liq attack dersi). Bekleme POZİSYON başına: aynı pozisyon 4 saat
yeniden yazılmaz, ama aynı coinde YENİ bir pozisyon eşiğe girerse mesaj gider
ve eskiler "daha önce bildirildi" diye toplamla anılır.

FİYAT `main_dex_ctx` kv'si: metrik döngüsü 5 dk'da bir (ABD kapalıyken 60 sn)
yazıyor; bayatsa bu modül kendisi çeker — tek istek, ~200 coin.

Kanal boşsa / bot yoksa / tip kapalıysa hesap yine yapılır (kv `cryptoliq_stats`
→ /tani "neden gelmedi"yi söyler) ama sonda atılmaz, gönderim yapılmaz.
Marker YALNIZ gönderim başarılıysa yazılır (kapalı seans dersi).
"""
import asyncio
import logging

from ..db import alert_log, alert_recent, db, kv_set, now
from .bigpos import MAJORS

log = logging.getLogger("radar.cryptoliq")

PROBE_MAX = 12        # tur başına canlılık sondası (adres); kalanlar sonraki tura
LIST_MAX = 6          # mesajda tek tek yazılan pozisyon; fazlası toplamla


# ─────────────────────────────────────────────── saf hesap (test edilir)

def near_liq(rows: list[dict], marks: dict, dist_pct: float, min_usd: float,
             exclude=MAJORS) -> dict[str, list[dict]]:
    """coin → likidasyona ≤ dist_pct uzaklıkta, yönü doğru pozisyonlar (yakından uzağa).

    Yön sağlaması `liqattack.liq_within` ile aynı: long'un liq'i markın ALTINDA,
    short'unki ÜSTÜNDE olmalı; tersi veri tutarsızlığıdır ve atlanır (sessizce
    yön değiştirmekten iyi). HIP-3 (':' içeren) ve `exclude` coinleri girmez.
    Dönen satırlar girdinin kopyası + `dist`, `mark`.
    """
    out: dict[str, list[dict]] = {}
    for p in rows:
        coin = p.get("coin") or ""
        if not coin or ":" in coin or coin.upper() in exclude:
            continue
        try:
            mark = float(marks.get(coin) or 0)
            liq = float(p.get("liq_px") or 0)
            ntl = float(p.get("notional") or 0)
        except (TypeError, ValueError):
            continue
        side = p.get("side")
        if mark <= 0 or liq <= 0 or ntl < min_usd or side not in ("long", "short"):
            continue
        if (side == "long" and liq >= mark) or (side == "short" and liq <= mark):
            continue
        dist = abs(mark - liq) / mark * 100
        if dist > dist_pct:
            continue
        out.setdefault(coin, []).append({**p, "notional": ntl, "liq_px": liq,
                                         "dist": dist, "mark": mark})
    for lst in out.values():
        lst.sort(key=lambda q: q["dist"])
    return out


# ─────────────────────────────────────────────── veri

async def _rows(min_usd: float) -> list[dict]:
    async with db() as conn:
        cur = await conn.execute(
            """SELECT a.coin, a.address, a.side, a.notional, a.liq_px, a.leverage,
                      a.entry_px, a.ts, ad.entity
               FROM addr_positions a LEFT JOIN addresses ad ON ad.address = a.address
               WHERE a.closed_ts IS NULL AND a.liq_px > 0 AND a.notional >= ?
                 AND instr(a.coin, ':') = 0""", (min_usd,))
        return [dict(r) for r in await cur.fetchall()]


async def _reread(coin: str, addrs: list[str]) -> dict[str, dict]:
    """Sondadan sonra aynı satırları yeniden oku (kapanmışsa closed_ts dolu)."""
    if not addrs:
        return {}
    q = ",".join("?" * len(addrs))
    async with db() as conn:
        cur = await conn.execute(
            f"""SELECT a.coin, a.address, a.side, a.notional, a.liq_px, a.leverage,
                       a.entry_px, a.ts, a.closed_ts, ad.entity
                FROM addr_positions a LEFT JOIN addresses ad ON ad.address = a.address
                WHERE a.coin=? AND a.address IN ({q})""", (coin, *addrs))
        return {r["address"]: dict(r) for r in await cur.fetchall()}


def send_gate(cfg, notifier) -> str:
    """Gönderim mümkün değilse NEDENİ (boş = gönderilebilir)."""
    from ..notify import kind_enabled
    if not (getattr(cfg, "crypto_chat_id", "") or "").strip():
        return "CRYPTO_CHAT_ID yok"
    if notifier is None or getattr(notifier, "bot", None) is None:
        return "bot yok"
    if not kind_enabled(cfg, "cryptoliq"):
        return "bildirim kapalı (notify_cryptoliq)"
    return ""


# ─────────────────────────────────────────────── tarama

async def scan(cfg, client, notifier=None) -> dict:
    out = {"coins": 0, "positions": 0, "candidates": 0, "fresh": 0, "probed": 0,
           "probe_deferred": 0, "probe_err": 0, "dropped_stale": 0, "alerted": 0,
           "failed": 0, "skipped": "", "chat": False, "top": [], "ctx_age": None}
    if not getattr(cfg, "crypto_liq_enabled", True):
        out["skipped"] = "kapalı"
        return await _stats(out)
    min_usd = float(getattr(cfg, "crypto_liq_min_usd", 500_000))
    dist_pct = float(getattr(cfg, "crypto_liq_dist_pct", 2.5))
    cool = max(60, int(getattr(cfg, "crypto_liq_cooldown", 4 * 3600)))
    chat = (getattr(cfg, "crypto_chat_id", "") or "").strip()
    out["chat"] = bool(chat)
    gate = send_gate(cfg, notifier)
    out["skipped"] = gate

    rows = await _rows(min_usd)
    out["positions"] = len(rows)
    out["coins"] = len({r["coin"] for r in rows})
    if not rows:
        return await _stats(out)
    from ..hl.universe import main_dex_ctx
    try:
        # Kapı kapalıysa istek de atma: yalnız kv (metrik döngüsü zaten yazıyor).
        ctx = await main_dex_ctx(client, fetch=not gate)
    except Exception as e:
        out["skipped"] = f"fiyat alınamadı: {type(e).__name__}: {e}"
        log.warning("kripto liq: ana dex fiyatları alınamadı: %s", e)
        return await _stats(out)
    marks = {c: v.get("m") for c, v in (ctx.get("c") or {}).items()}
    if ctx.get("ts"):
        out["ctx_age"] = max(0, now() - int(ctx["ts"]))

    by = near_liq(rows, marks, dist_pct, min_usd)
    out["candidates"] = sum(len(v) for v in by.values())
    out["top"] = sorted(({"coin": c, "n": len(v), "total": sum(p["notional"] for p in v)}
                         for c, v in by.items()), key=lambda x: -x["total"])[:3]
    if gate or not by:
        return await _stats(out)               # hesap yapıldı; sonda ve gönderim yok

    from ..telegram import format as fmt
    from .sweeper import probe_address
    ts = now()
    budget = PROBE_MAX
    for coin, cands in sorted(by.items(), key=lambda kv: -sum(p["notional"] for p in kv[1])):
        fresh, old = [], []
        for p in cands:
            (old if await alert_recent("cryptoliq", f"{coin}:{p['address']}", cool)
             else fresh).append(p)
        if not fresh:
            continue
        out["fresh"] += len(fresh)
        # Canlılık: taze adayların defterini ŞİMDİ çek — sırayla (sonda
        # semaforu ilk kullanımda döngüye bağlanır; eşzamanlılık burada gereksiz),
        # tur bütçesi dahilinde. Bütçeye sığmayan aday bu tur yazılmaz; marker
        # da yazılmadığı için sonraki tur yeniden sıraya girer.
        todo, deferred = fresh[:budget], fresh[budget:]
        out["probe_deferred"] += len(deferred)
        budget -= len(todo)
        probed_ok: set[str] = set()
        for p in todo:
            try:
                await probe_address(cfg, client, p["address"])
                probed_ok.add(p["address"])
                out["probed"] += 1
            except Exception as e:
                out["probe_err"] += 1
                log.debug("sonda %s: %s", p["address"], e)
        if probed_ok:
            live = await _reread(coin, list(probed_ok))
            keep = []
            for p in todo:
                if p["address"] not in probed_ok:
                    keep.append(p)              # sonda düştü: eldeki satır, yaşı yazılır
                    continue
                q = live.get(p["address"])
                if not q or q.get("closed_ts"):
                    out["dropped_stale"] += 1   # kapanmış: "patlamak üzere" DEĞİL
                    continue
                again = near_liq([q], {coin: marks.get(coin)}, dist_pct, min_usd).get(coin)
                if not again:
                    out["dropped_stale"] += 1   # küçülmüş ya da uzaklaşmış
                    continue
                keep.append({**again[0], "verified": True})
            todo = keep
        if not todo:
            continue
        text = fmt.crypto_liq_alert(coin, marks.get(coin), todo, old, dist_pct)
        key = f"cryptoliq:{coin}:{ts}"
        # MARKER YALNIZ GİDERSE: başarısız gönderim bekleme süresini yakmasın,
        # bir sonraki tur yeniden denesin.
        if await notifier.send("cryptoliq", text, priority="high", key=key, chat_id=chat):
            for p in todo:
                await alert_log("cryptoliq", f"{coin}:{p['address']}", text)
            out["alerted"] += 1
        else:
            await alert_log("fail:cryptoliq", key, text[:200])
            out["failed"] += 1
    if out["alerted"] or out["dropped_stale"]:
        log.info("kripto liq: %d aday, %d bildirim, %d bayat düştü, %d gönderilemedi",
                 out["candidates"], out["alerted"], out["dropped_stale"], out["failed"])
    return await _stats(out)


async def _stats(out: dict) -> dict:
    try:
        await kv_set("cryptoliq_stats", {**out, "ts": now()})
    except Exception:
        log.debug("cryptoliq_stats yazılamadı", exc_info=True)
    return out


async def loop(cfg, client, notifier) -> None:
    """Denetimli döngü. Site buna bağımlı değil: patlarsa yalnız bildirim durur
    ve /tani sebebini yazar."""
    from ..health import beat
    await asyncio.sleep(150)               # açılışta süpürme/metrik otursun
    while True:
        try:
            await beat("cryptoliq")
            await scan(cfg, client, notifier)
            await beat("cryptoliq")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("kripto liq turu hatası")
            await _stats({"error": f"{type(e).__name__}: {e}"[:200]})
        await asyncio.sleep(max(60, int(getattr(cfg, "crypto_liq_poll_sec", 120))))
