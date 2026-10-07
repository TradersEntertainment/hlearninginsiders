"""⏱ /5dk · /15dk — "şimdiden N dakika ölç, en çok oynayan hisseleri söyle" (haber saatleri).

Kullanıcı (07.10): "/5dk yazdığımda gelecek 5 dk en çok hangi hissenin oynadığını söylesin, 15dk
yazarsam 15 dk — herhangi bir an; haber saatlerinde kullanacağım." Kararlar: hemen başlar; istersen
saat de verilir (`/5dk 15:30` → tam 15:30:00, haberin ilk saniyesi kaçmaz; en çok 3 dk geriye —
canlı akışın tamponundan); PROPR hisseleri (açılış raporuyla aynı evren); ölçerken 📊 tuşu.

Ölçüm açılış motorunun canlı akışından (openmove.observe → REG.wins) — HL'ye ek istek yok:
  • referans = başlangıçtan önceki son işlem (tampondan; ileri saatli pencerede başlangıca dek canlı)
  • değişim / aralık / hacim / 24s katı açılış raporuyla aynı hesap (openmove.rank)
  • taban: move_window_min_usd (5 dk başına) × süre / 5 — ince hisse tek işlemle "en hareketli"
    olmasın; kaç hissenin elendiği künyede
  • referansı başlangıçtan 5 dk'dan eski satır ° ile (haber öncesi seyrek işlem)
Kapsam dürüst: görülmeyen aralıklar raporda; yarıdan azı görüldüyse rapor yerine açıklama. Yeniden
başlatmada başlamamış pencere geri yüklenir; yarıda kalan için tek not — referansı yeni süreçte
yok, † satırları haber sıçramasından sonrasını ölçerdi (yanıltıcı).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta

from ..db import kv_get, kv_set, now
from . import openmove as om          # om.REG'e HEP modül üzerinden (testler REG'i değiştirir)

log = logging.getLogger("radar.movewin")

MIN_MIN, MAX_MIN = 1, 60
BACK = 180                       # geriye dönük başlangıç sınırı (tampon om.KEEP = BACK + saat payı)
AHEAD = 12 * 3600                # ileri saat sınırı
MAX_TOTAL, MAX_CHAT = 5, 2       # aynı anda en çok (toplam / sohbet başına)
GRACE = 5                        # canlı akışın gecikme payı
RETRY_SEC = 30                   # gönderim hatasında yeniden deneme aralığı
GIVE_UP = 1800                   # gönderilemeyen rapor bu kadar sonra bırakılır
NOTE_MAX = 1800                  # kapalıyken biten pencereye not: ancak bu kadar yeniyse
STALE = 300                      # referans işlemi başlangıçtan bu kadar eskiyse °
PEEK_TOP = 5
PEEK_MAX = 200                   # answerCallbackQuery açılır pencere sınırı
KV = "movewin"                   # {"wins": [{chat, t0, mins}], "last": {...}, "done": n}

_CMD = re.compile(r"^(\d{1,2})(dk|dak|dakika)?$")
_TIME = re.compile(r"^(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?$")
_LOCK: tuple | None = None


# ---------------- komut ----------------

def parse(cmd: str, args: list[str]) -> dict | None:
    """'/5dk' '/15dk' '/5' '/5 dk' '/10dakika' (+ isteğe bağlı TSİ saati '15:30' / '15.30' /
    '15:30:00'). Pencere komutu değilse None; hatalıysa {"error"}; değilse {"mins", "at"}."""
    m = _CMD.match(cmd or "")
    if not m:
        return None
    rest = list(args or [])
    if not m.group(2) and rest and rest[0].lower() in ("dk", "dak", "dakika"):
        rest = rest[1:]
    mins = int(m.group(1))
    if not MIN_MIN <= mins <= MAX_MIN:
        return {"error": "süre 1–60 dk arası olmalı: /5dk, /15dk"}
    at = None
    if rest:
        t = _TIME.match(rest[0])
        if not t or int(t.group(1)) > 23 or int(t.group(2)) > 59 or int(t.group(3) or 0) > 59:
            return {"error": "saat biçimi: /5dk 15:30 (TSİ)"}
        at = (int(t.group(1)), int(t.group(2)), int(t.group(3) or 0))
    return {"mins": mins, "at": at}


def resolve_start(at: tuple | None, now_ts: int) -> tuple[int | None, str]:
    """TSİ saat → başlangıç: dün / bugün / yarın denenir, yalnız [şimdi − BACK, şimdi + AHEAD]
    içindeki kabul. Geçmiş saat sessizce yarına KAYMAZ (reddedilen her saat yakın geçmiştedir)."""
    if at is None:
        return int(now_ts), ""
    from .hourstats import TR
    base = datetime.fromtimestamp(int(now_ts), TR).date()
    for dd in (-1, 0, 1):
        d = base + timedelta(days=dd)
        t0 = int(datetime(d.year, d.month, d.day, at[0], at[1], at[2], tzinfo=TR).timestamp())
        if now_ts - BACK <= t0 <= now_ts + AHEAD:
            return t0, ""
    return None, "başlangıç saati geçmiş — en çok 3 dk geriye gidilebilir (saatsiz /5dk hemen başlar)"


def find(chat: str, t0: int, mins: int) -> dict | None:
    for w in om.REG.wins:
        if w["chat"] == str(chat) and w["t0"] == int(t0) and w["mins"] == int(mins):
            return w
    return None


def _new(chat: str, t0: int, mins: int, created: int) -> dict:
    return {"chat": str(chat), "t0": int(t0), "t1": int(t0) + int(mins) * 60, "mins": int(mins),
            "created": int(created), "ref": {}, "agg": {}, "fail_ts": 0}


def seed(w: dict) -> None:
    """Referans = t0'dan önceki son işlem (tampon; çapa sayesinde seyrek hissede de), t0 ve
    sonrasındaki tampon işlemleri deftere (geriye dönük başlangıç). İleri saatli pencerede
    referans t0'a dek observe ile güncellenir. Tampon coin başına sıralı (tekrar teslim koruması)."""
    t0, t1 = w["t0"], w["t1"]
    for coin, dq in list(om.REG.recent.items()):
        for ts, px, sz in list(dq):
            if ts < t0:
                w["ref"][coin] = (ts, px)
            elif ts < t1:
                om._add(w["agg"], coin, px, sz, ts)


def start(mins: int, chat: str, now_ts: int, t0: int) -> dict:
    """SENKRON tek blok — tohumlama + kayıt arasına canlı akış giremez (çift sayım yok).
    Aynı (sohbet, başlangıç, süre) ikinci kez yazılırsa birleşir."""
    r = om.REG
    if not r.coins:
        return {"ok": False, "error": "ölçüm henüz hazır değil (bot yeni açıldı) — birkaç saniye sonra tekrar dene"}
    w = find(chat, t0, mins)
    if w is not None:
        return {"ok": True, "win": w, "merged": True}
    if len(r.wins) >= MAX_TOTAL:
        return {"ok": False, "error": f"aynı anda en çok {MAX_TOTAL} ölçüm — biri bitince yeniden dene"}
    if sum(1 for x in r.wins if x["chat"] == str(chat)) >= MAX_CHAT:
        return {"ok": False, "error": f"bu sohbette aynı anda en çok {MAX_CHAT} ölçüm — biri bitince yeniden dene"}
    w = _new(chat, t0, mins, now_ts)
    seed(w)
    r.wins.append(w)
    return {"ok": True, "win": w, "merged": False}


def live_down() -> bool:
    try:
        from ..hl import collector as col
        live = col.LIVE
        return bool(live is not None and float(getattr(live, "down_since", 0) or 0))
    except Exception:
        return False


async def command(cfg, cmd: str, args: list[str], chat: str) -> tuple[str, dict | None] | None:
    """Telegram komutu → (cevap metni, tuş) ya da None (pencere komutu değil)."""
    from ..telegram import format as fmt
    spec = parse(cmd, args)
    if spec is None:
        return None
    if spec.get("error"):
        return f"⏱ {spec['error']}", None
    ts = int(now())
    await om.ensure_universe(cfg, ts)
    t0, why = resolve_start(spec["at"], ts)
    if t0 is None:
        return f"⏱ {why}", None
    res = start(spec["mins"], chat, ts, t0)
    if not res["ok"]:
        return f"⏱ {res['error']}", None
    w = res["win"]
    if not res["merged"]:
        await save()
    return (fmt.movewin_ack(w, ts, len(om.REG.coins), merged=res["merged"], down=live_down()),
            fmt.movewin_kb(w))


# ---------------- ölçüm / rapor ----------------

def _rate(cfg) -> float:
    return max(0.0, float(getattr(cfg, "move_window_min_usd", 10_000) or 0))


async def build(cfg, w: dict, ts: int) -> dict:
    """Bitmiş pencerenin raporu. 24s hacimleri ÖNCE beklenir, defter sonra senkron okunur."""
    end, span = w["t1"], max(60, w["t1"] - w["t0"])
    floor = _rate(cfg) * span / 300
    top = max(1, int(getattr(cfg, "open_movers_top", 10) or 10))
    vols = await om._day_volumes()
    rows, below = om.rank(w["agg"], w["ref"], vols, span, floor, top, t0=w["t0"], stale=STALE)
    return {"mins": w["mins"], "t0": w["t0"], "t1": w["t1"], "ts": int(ts), "rows": rows,
            "n_universe": len(om.REG.coins), "n_traded": len(w["agg"]), "n_below": below, "floor": floor,
            "cover": om.coverage(w["t0"], end, ts, since=om.REG.obs_since),
            "late": int(ts) - w["t1"] if int(ts) - w["t1"] > 60 else 0}


def peek(cfg, chat: str, t0: int, mins: int, now_ts: int) -> str:
    """📊 açılır pencere: SENKRON, I/O yok (24s katı yok) — düz metin, ≤ PEEK_MAX (UTF-16)."""
    from ..telegram import format as fmt
    w = find(chat, t0, mins)
    if w is None:
        if now_ts >= int(t0) + int(mins) * 60:
            return "Ölçüm bitti — rapor bu sohbette."
        return "Bu ölçüm bulunamadı (bot yeniden başlamış) — /5dk ile yeniden başlat."
    if now_ts < w["t0"]:
        return (f"{w['mins']} dk ölçüm henüz başlamadı — başlangıç {fmt.tr_time_s(w['t0'])} TSİ"
                f" (kalan {fmt.dur_s(w['t0'] - now_ts)}).")
    end = min(int(now_ts), w["t1"])
    span = max(60, end - w["t0"])
    floor = _rate(cfg) * span / 300
    rows, below = om.rank(w["agg"], w["ref"], {}, span, floor, PEEK_TOP, t0=w["t0"], stale=STALE)
    return fmt.movewin_peek(w, rows, floor, end - w["t0"], done=now_ts >= w["t1"], limit=PEEK_MAX)


def peek_data(cfg, data: str, chat: str, now_ts: int | None = None) -> str:
    """Tuş verisi 'mvw:<t0>:<dk>' → açılır pencere metni (sohbet tuşun mesajından)."""
    parts = str(data or "").split(":")
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        return "Geçersiz tuş."
    return peek(cfg, chat, int(parts[1]), int(parts[2]), int(now_ts if now_ts is not None else now()))


# ---------------- kayıt (yeniden başlatmaya dayanıklı) ----------------

def _lock() -> asyncio.Lock:
    """Bot komutu ve döngü aynı olay döngüsünde kv'ye yazar: sıralı olsun (eski anlık görüntü
    yenisinin üstüne yazılmasın). Kilit olay döngüsüne bağlı → döngü değişince yenisi."""
    global _LOCK
    loop = asyncio.get_running_loop()
    if _LOCK is None or _LOCK[0] is not loop:
        _LOCK = (loop, asyncio.Lock())
    return _LOCK[1]


def _same(e: dict, w: dict) -> bool:
    try:
        return (str(e.get("chat")) == w["chat"] and int(e.get("t0") or 0) == w["t0"]
                and int(e.get("mins") or 0) == w["mins"])
    except (TypeError, ValueError):                # bozuk kayıt: eşleşmez, geri yüklemede elenir
        return False


async def save(last: dict | None = None) -> None:
    """Pencere tanımları kv'ye (anlık görüntü kilidin İÇİNDE alınır). Geri yükleme bu süreçte henüz
    olmadıysa eski kayıtlar korunur (yeniden başlatmadan hemen sonra yazılan komut onları silmesin)."""
    async with _lock():
        doc = await kv_get(KV) or {}
        mine = [{"chat": w["chat"], "t0": w["t0"], "mins": w["mins"]} for w in om.REG.wins]
        if not om.REG.wins_loaded:
            mine = [e for e in doc.get("wins") or [] if isinstance(e, dict)
                    and not any(_same(e, w) for w in om.REG.wins)] + mine
        doc["wins"] = mine
        if last is not None:
            doc["last"] = last
            doc["done"] = int(doc.get("done") or 0) + 1
        await kv_set(KV, doc)


async def _send(notifier, chat: str, text: str, key: str) -> bool:
    if notifier is None:
        return False
    try:
        return bool(await notifier.send("movewin", text, chat_id=chat, key=key, public=False))
    except Exception:
        log.warning("⏱ pencere raporu gönderilemedi", exc_info=True)
        return False


async def _restore(cfg, notifier, ts: int) -> int:
    """Bu süreçte ilk adım: kv'deki pencereler. Başlamamış → geri yüklenir (referans t0'a dek canlı
    toplanır); yarıda kalan / kapalıyken biten (NOTE_MAX içinde) → tek not. Kayıtlar doğrulanır."""
    from ..telegram import format as fmt
    r = om.REG
    doc = await kv_get(KV) or {}
    notes = []
    for e in doc.get("wins") or []:
        try:
            chat, t0, mins = str(e["chat"]).strip(), int(e["t0"]), int(e["mins"])
        except (KeyError, TypeError, ValueError):
            continue
        if not chat or not MIN_MIN <= mins <= MAX_MIN or find(chat, t0, mins) is not None:
            continue
        if t0 > ts:
            if len(r.wins) < MAX_TOTAL:
                w = _new(chat, t0, mins, ts)
                seed(w)
                r.wins.append(w)
        elif ts - (t0 + mins * 60) <= NOTE_MAX:
            notes.append((chat, t0, mins))
    r.wins_loaded = True
    last = None
    for chat, t0, mins in notes:
        await _send(notifier, chat, fmt.movewin_cut(t0, mins), key=f"movewin:{chat}:{t0}:{mins}:cut")
        last = {"ts": int(ts), "mins": mins, "t0": t0, "result": "yarıda kaldı (bot yeniden başladı)"}
    await save(last)
    return len(notes)


async def tick(cfg, notifier, ts: int) -> dict:
    """openmove.loop her adımda çağırır: geri yükleme (bir kez), zamanı gelen raporlar."""
    from ..telegram import format as fmt
    out = {"sent": 0, "short": 0, "notes": 0, "dropped": 0}
    r = om.REG
    if not r.wins_loaded:
        await om.ensure_universe(cfg, ts)
        out["notes"] = await _restore(cfg, notifier, ts)
    for w in list(r.wins):
        if ts < w["t1"] + GRACE or ts < w["fail_ts"] + RETRY_SEC:
            continue
        rep = await build(cfg, w, ts)
        short = rep["cover"]["frac"] < om.MIN_COVER
        text = fmt.movewin_short(rep) if short else fmt.movewin_report(rep)
        ok = await _send(notifier, w["chat"], text, key=f"movewin:{w['chat']}:{w['t0']}:{w['mins']}")
        if not ok and ts - w["t1"] <= GIVE_UP:
            w["fail_ts"] = int(ts)            # 30 sn sonra yeniden (ölçüm bayatlamaz)
            continue
        if w in r.wins:
            r.wins.remove(w)
        result = ("yetersiz kapsam" if short else "gönderildi") if ok else "gönderilemedi (Telegram)"
        out["sent" if ok and not short else "short" if ok else "dropped"] += 1
        await save({"ts": int(ts), "mins": w["mins"], "t0": w["t0"], "result": result})
    return out


async def diag_line(cfg) -> str:
    """/tani: aktif pencere, tamamlanan, son sonuç — sohbet id'si YOK."""
    from ..telegram import format as fmt
    doc = await kv_get(KV) or {}
    last = doc.get("last") or {}
    tail = ""
    if last:
        t0 = int(last.get("t0") or 0)
        tail = (f" · son: {last.get('mins')} dk {fmt.tr_time_s(t0)}–{fmt.tr_time_s(t0 + int(last.get('mins') or 0) * 60)}"
                f" → {last.get('result')}")
    return (f"⏱ pencere ölçümü (/5dk): aktif {len(om.REG.wins)} · kayıtlı {len(doc.get('wins') or [])}"
            f" · tamamlanan {int(doc.get('done') or 0)}{tail}"
            + (f" · ⚠️ kanca hatası {om.REG.win_errors}" if om.REG.win_errors else ""))
