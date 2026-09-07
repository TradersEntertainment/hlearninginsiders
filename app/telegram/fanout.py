"""Fan-out — Notifier'dan gelen olayları abone Pro kullanıcılara DM'ler.

`Notifier.send` / `send_rich` her olayı (tür, coin, metin, resim, anahtar) buraya
YAYINLAR — sahibin kanal gönderimi, toggle'ı ve sessiz saatinden bağımsız; ürünün
kapısı `public_bot_enabled` + `public_kinds` + `PUBLIC_KINDS`. Bellek içi kuyruk
(tavan QUEUE_MAX, taşarsa en eski düşer) + tek tüketici döngü; hız `bot._pace`
(küresel 20 msg/sn, sohbet başına 1/sn); 403 → `bot._note_error` engel kancası.
Tekilleştirme (tür, anahtar) 12 saat: sahibe yeniden denenen gönderim kullanıcıya
ikinci kez gitmez. Kişisel sessiz saatteki kullanıcı ATLANIR (ertelenmez — 5 dk'lık
olay sabah değersiz). Ücretsiz sabah özeti: dünün öne çıkanları, ücretsiz
kullanıcılara günde bir (tadımlık)."""
import asyncio
import hashlib
import logging
import re
from collections import deque
from datetime import datetime

from .. import users
from ..db import alert_log, alert_recent, db, kv_get, kv_set, now
from ..hl.universe import symbol_of
from ..notify import PUBLIC_KINDS, TR

log = logging.getLogger("telegram.fanout")

QUEUE_MAX = 500
DEDUPE_SEC = 12 * 3600
FOOT = "🔕 türleri/coinleri değiştir: /bildirimler"
KV_DIGEST_DAY = "public_digest_day"
KV_DIGEST_STATS = "public_digest_stats"
KV_STATS = "fanout_stats"

_queue: deque = deque()
_wake: asyncio.Event | None = None
_stats = {"queued": 0, "dropped": 0, "events": 0, "dup": 0, "targets": 0, "sent": 0, "fail": 0,
          "blocked": 0, "quiet": 0, "last_ts": None, "last": None}


def reset() -> None:
    _queue.clear()
    for k in _stats:
        _stats[k] = None if k in ("last_ts", "last") else 0


def stats() -> dict:
    return {**_stats, "queue": len(_queue)}


def enabled(cfg, kind: str) -> bool:
    return (bool(getattr(cfg, "public_bot_enabled", False)) and kind in PUBLIC_KINDS
            and kind in users.csv_set(getattr(cfg, "public_kinds", "")))


def publish(cfg, kind: str, coin: str, text: str, png: bytes | None, key: str) -> bool:
    """Sıcak yol (Notifier içinden): kapı + kuyruk. Await yok."""
    if not text or not enabled(cfg, kind):
        return False
    ev = {"kind": kind, "coin": coin or "", "text": text, "png": png, "ts": now(),
          "key": key or hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]}
    if len(_queue) >= QUEUE_MAX:
        _queue.popleft()
        _stats["dropped"] += 1
    _queue.append(ev)
    _stats["queued"] += 1
    if _wake is not None:
        _wake.set()
    return True


async def deliver(bot, cfg, ev: dict) -> dict:
    """Bir olay → abonelere DM. Dönüş sayaçları; fanout_log'a satır."""
    out = {"kind": ev["kind"], "coin": ev.get("coin") or "", "targets": 0, "sent": 0, "fail": 0,
           "blocked": 0, "quiet": 0, "dup": 0}
    kkey = f"fan:{ev['kind']}"
    if await alert_recent(kkey, ev["key"], DEDUPE_SEC):
        out["dup"] = 1
        _stats["dup"] += 1
        return out
    await alert_log(kkey, ev["key"], "")
    sym = symbol_of(ev["coin"]) if ev.get("coin") else ""
    targets = await users.subscribers(ev["kind"], sym)
    text = (ev["text"] or "").rstrip() + "\n" + FOOT
    png = ev.get("png")
    from .bot import _caption_fit
    use_photo = bool(png) and _caption_fit(text)[1]
    for u in targets:
        if users.quiet_now(u):
            out["quiet"] += 1
            continue
        out["targets"] += 1
        chat = u["chat_id"]
        try:
            ok = await bot.send_photo(png, text, chat) if use_photo else await bot.send(text, chat)
            if not ok and use_photo and chat not in getattr(bot, "blocked_chats", set()):
                ok = await bot.send(text, chat)          # resim reddedildi: metin yedek
        except Exception:
            log.debug("fan-out gönderimi", exc_info=True)
            ok = False
        if ok:
            out["sent"] += 1
        elif chat in getattr(bot, "blocked_chats", set()):
            out["blocked"] += 1
        else:
            out["fail"] += 1
    async with db() as conn:
        await conn.execute(
            "INSERT INTO fanout_log(ts, kind, coin, key, n_targets, n_sent, n_fail, n_blocked) VALUES(?,?,?,?,?,?,?,?)",
            (now(), ev["kind"], out["coin"], ev["key"], out["targets"], out["sent"], out["fail"], out["blocked"]))
    for k in ("targets", "sent", "fail", "blocked", "quiet"):
        _stats[k] += out[k]
    _stats["events"] += 1
    _stats["last_ts"] = now()
    _stats["last"] = f"{ev['kind']} {out['coin']} → {out['sent']}/{out['targets']}"
    return out


async def drain(bot, cfg) -> int:
    n = 0
    while _queue:
        ev = _queue.popleft()
        if bot is None:
            continue
        try:
            await deliver(bot, cfg, ev)
            n += 1
        except Exception:
            log.exception("fan-out olayı işlenemedi")
    return n


async def loop(bot, cfg) -> None:
    """Tek tüketici: uyandırılınca kuyruğu boşaltır; 60 sn'de bir nabız."""
    global _wake
    from ..health import beat
    _wake = asyncio.Event()
    while True:
        try:
            await beat("fanout")
            try:
                await asyncio.wait_for(_wake.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            _wake.clear()
            if await drain(bot, cfg):
                await kv_set(KV_STATS, {**stats(), "ts": now()})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("fan-out döngüsü hatası")
            await asyncio.sleep(5)


# ---------------- ücretsiz sabah özeti ----------------

def _headline(payload: str) -> str:
    for line in (payload or "").splitlines():
        t = re.sub(r"<[^>]+>", "", line).strip()
        if t:
            return t[:140]
    return ""


async def digest_items(hours: int = 24, limit: int = 5) -> list[tuple[str, str, int]]:
    """Dünkü gönderilmiş olaylardan (yalnız satılan türler) tekil başlıklar, yeniden eskiye."""
    async with db() as conn:
        cur = await conn.execute(
            "SELECT kind, payload, ts FROM alerts_log WHERE kind LIKE 'sent:%' AND ts > ? ORDER BY ts DESC LIMIT 300",
            (now() - hours * 3600,))
        rows = [dict(r) for r in await cur.fetchall()]
    out, seen = [], set()
    for r in rows:
        k = r["kind"][5:]
        if k not in PUBLIC_KINDS:
            continue
        head = _headline(r.get("payload") or "")
        if not head or head in seen:
            continue
        seen.add(head)
        out.append((k, head, int(r["ts"])))
        if len(out) >= limit:
            break
    return out


def digest_text(items: list[tuple[str, str, int]]) -> str:
    from .format import esc, tr_time
    lines = [f"🌅 <b>Dünün öne çıkanları</b> — {len(items)} olay"]
    for k, head, ts in items:
        lines.append(f"• {tr_time(ts)} {esc(head)}")
    lines.append("⭐ Pro'da bunlar olduğu anda DM'ine gelir, tür ve coin filtresiyle — /pro")
    return "\n".join(lines)


async def send_public_digest(bot, cfg, ts=None) -> dict:
    ts = ts or now()
    items = await digest_items()
    if not items:
        return {"skipped": "olay yok", "targets": 0, "sent": 0}
    text = digest_text(items)
    targets = [u for u in await users.all_active() if not users.is_pro(u, ts)]
    sent = 0
    for u in targets:
        try:
            if await bot.send(text, u["chat_id"]):
                sent += 1
        except Exception:
            log.debug("özet DM", exc_info=True)
    return {"targets": len(targets), "sent": sent, "items": len(items)}


async def digest_loop(bot, cfg) -> None:
    from ..health import beat
    await asyncio.sleep(240)
    while True:
        try:
            await beat("public_digest")
            if (bot is not None and getattr(cfg, "public_bot_enabled", False)
                    and getattr(cfg, "public_digest_enabled", True)):
                today = datetime.now(TR).strftime("%Y-%m-%d")
                if datetime.now(TR).hour == int(getattr(cfg, "public_digest_hour", 9)) \
                        and (await kv_get(KV_DIGEST_DAY)) != today:
                    await kv_set(KV_DIGEST_DAY, today)
                    res = await send_public_digest(bot, cfg)
                    await kv_set(KV_DIGEST_STATS, {**res, "ts": now()})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ücretsiz özet hatası")
        await asyncio.sleep(600)
