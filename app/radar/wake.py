"""🚨 Uyandırma alarmı — gece pozisyon açıkken bir şey olursa seni Telegram'dan ARAR.

Kullanıcı (07.10): "bazen uyumadan önce pozum açık oluyor, bir şey olursa uyanmak istiyorum ama
telefonum rahatsız etmede; bildirim gelmez, yalnız aramalara uyanırım — o da 2 kez peş peşe".
Kararlar: kanal yalnız Telegram araması (ücretsiz); tetikler fiyat seviyesi, yüzde hareket, HL
adresindeki pozisyon ve 👣 takip ettiğin balina.

Telegram botları arama yapamaz → CallMeBot (ücretsiz, kişisel kullanım) seni Telegram'dan arar
ve metni Türkçe okur (en çok 256 karakter, ~30 sn çalar). Bir kez izin: @CallMeBot_txtbot'a
/start. Arama açıldı mı bilgisi DÖNMEZ → onay yalnız "✅ Uyandım" tuşu ya da /uyandim ile.
iOS Rahatsız Etme'de Telegram aramasının çalması Odak ayarına bağlı (belgeli değil) — bu yüzden
aramalar İKİŞER gelir (Tekrarlanan Aramalar penceresi 3 dk) ve /alarm_test ile yatmadan önce
denenir.

Akış: tetik yerleri (fiyat/adres denetimi, takip motoru) yalnız `fire()` ile olay satırı yazar;
teslimatı bu modülün döngüsü yapar (mesaj + ✅ tuşu, arama turları, onay). Aynı olay iki kez
çalmaz (`key` tekil). Alarmlar tek seferlik: tetiklenince kapanır.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

import aiohttp

from ..db import db, kv_get, kv_set, now

log = logging.getLogger("radar.wake")

CALLMEBOT_URL = "https://api.callmebot.com/start.php"
AUTH_HINT = "Telegram'da @CallMeBot_txtbot'a /start yaz (arama izni)"
SPEECH_MAX = 256                 # CallMeBot metin sınırı
TICK = 5                         # döngü adımı (sn)
DATA_STALE = 300                 # fiyat/hesap verisi bu kadar okunamazsa tek uyarı
STATS_KV = "wake_stats"
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_NUM_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")
_SCRIPT_RE = re.compile(r"<!--.*?-->|<(script|style)\b[^>]*>.*?</\1\s*>", re.S | re.I)


# ---------------- ayarlar ----------------

def tg_user(cfg) -> str:
    """Aranacak Telegram hesabı: @kullanıcıadı ya da +90… (env WAKE_TELEGRAM_USER)."""
    u = str(getattr(cfg, "wake_telegram_user", "") or "").strip()
    if u and not u.startswith(("@", "+")):
        u = "@" + u
    return u


def plan(cfg) -> dict:
    rounds = max(1, int(getattr(cfg, "wake_call_rounds", 3) or 3))
    return {"rounds": rounds, "max_calls": rounds * 2,
            "gap": min(150, max(40, int(getattr(cfg, "wake_call_gap_sec", 70) or 70))),
            "round_gap": max(60, int(getattr(cfg, "wake_round_gap_sec", 240) or 240)),
            "lang": str(getattr(cfg, "wake_tts_lang", "") or "tr-TR-Standard-A")}


# ---------------- sayı ve metin ----------------

def num(s: str) -> float | None:
    s = (s or "").strip().replace(" ", "")
    if not _NUM_RE.match(s):
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def pct_arg(s: str) -> float | None:
    s = (s or "").strip()
    if s.startswith("%"):
        return num(s[1:])
    if s.endswith("%"):
        return num(s[:-1])
    return None


def fpx(p: float | None) -> str:
    if p is None:
        return "—"
    a = abs(p)
    return f"{p:.0f}" if a >= 1000 else f"{p:.2f}" if a >= 10 else f"{p:.4g}"


def fshort(p: float | None) -> str:
    """Seviye gösterimi: kullanıcının yazdığı gibi '480' / '480.5' (fpx'in '480.00'ı değil)."""
    t = fpx(p)
    return t.rstrip("0").rstrip(".") if "." in t else t


def say_num(p: float) -> str:
    """TTS için: '479,9' (Türkçe virgül — 'nokta' diye okunmasın; sondaki sıfırlar okunmaz)."""
    return fshort(p).replace(".", ",")


def sym_of(coin: str) -> str:
    from .. import assets
    return assets.label(coin or "") or (coin or "").split(":")[-1]


def parse_args(args: list[str]) -> dict:
    """/alarm argümanları → {kind, …} ya da {"error": …}.
    SNDK 480 · SNDK 480 520 · SNDK %3 · 0xADRES · 0xADRES %2"""
    a = [x for x in (args or []) if x.strip()]
    if not a:
        return {"error": "boş"}
    if _ADDR_RE.match(a[0]):
        liq = pct_arg(a[1]) if len(a) > 1 else None
        if len(a) > 1 and (liq is None or not 0 < liq < 50):
            return {"error": "liq eşiği %0–50 arası olmalı (ör. %2)"}
        return {"kind": "addr", "address": a[0].lower(), "liq_pct": liq}
    sym = a[0].upper().lstrip("$")
    if len(a) == 2 and pct_arg(a[1]) is not None:
        p = pct_arg(a[1])
        if not 0 < p < 100:
            return {"error": "yüzde %0–100 arası olmalı (ör. %3)"}
        return {"kind": "pct", "sym": sym, "pct": p}
    levels = [num(x) for x in a[1:]]
    if not levels or len(levels) > 2 or any(v is None or v <= 0 for v in levels):
        return {"error": "seviye sayı olmalı (ör. /alarm SNDK 480 ya da /alarm SNDK 480 520)"}
    return {"kind": "price", "sym": sym, "levels": sorted(levels)}


# ---------------- HL okumaları ----------------

async def mids(client, dex: str) -> dict[str, float]:
    """allMids (ağırlık 2) → {coin: fiyat}; HIP-3'te anahtar 'xyz:SNDK' biçimine getirilir."""
    raw = await client.all_mids(dex) if dex else await client.all_mids()
    out: dict[str, float] = {}
    for k, v in (raw or {}).items():
        k = str(k)
        if k.startswith("@"):
            continue
        try:
            px = float(v)
        except (TypeError, ValueError):
            continue
        out[f"{dex}:{k}" if dex and ":" not in k else k] = px
    return out


async def positions(client, address: str, dexes: list[str]) -> dict[str, dict]:
    """Adresin açık pozisyonları {coin: {side, szi, px, liq, notional}} — dex başına bir istek."""
    out: dict[str, dict] = {}
    for dex in dexes:
        state = await client.clearinghouse(address, dex)
        for ap in (state or {}).get("assetPositions") or []:
            p = ap.get("position") or {}
            try:
                szi = float(p.get("szi") or 0)
                ntl = abs(float(p.get("positionValue") or 0))
            except (TypeError, ValueError):
                continue
            if not szi:
                continue
            coin = str(p.get("coin") or "")
            if dex and ":" not in coin:
                coin = f"{dex}:{coin}"
            liq = p.get("liquidationPx")
            try:
                liq = float(liq) if liq else None
            except (TypeError, ValueError):
                liq = None
            out[coin] = {"side": "long" if szi > 0 else "short", "szi": szi, "notional": ntl,
                         "px": ntl / abs(szi) if ntl else None, "liq": liq}
    return out


def _dexes(cfg) -> list[str]:
    out = [""]
    for d in getattr(cfg, "equity_dexes", None) or ["xyz"]:
        d = str(d).strip()
        if d and d not in out:
            out.append(d)
    return out


# ---------------- alarm kurma ----------------

async def _insert_alarm(cfg, **f) -> int:
    ts = now()
    hours = max(1, int(getattr(cfg, "wake_alarm_hours", 18) or 18))
    f.update(created_ts=ts, expires_ts=ts + hours * 3600, active=1)
    cols = ", ".join(f)
    async with db() as conn:
        cur = await conn.execute(f"INSERT INTO wake_alarms({cols}) VALUES({', '.join('?' * len(f))})",
                                 tuple(f.values()))
        return int(cur.lastrowid)


async def arm(cfg, client, spec: dict, chat_id: str = "") -> dict:
    """parse_args çıktısı → alarm. Dönüş {"ok", "alarm"} ya da {"ok": False, "reason"}."""
    if spec.get("error"):
        return {"ok": False, "reason": spec["error"]}
    if spec["kind"] == "addr":
        try:
            pos = await positions(client, spec["address"], _dexes(cfg))
        except Exception as e:                       # noqa: BLE001 — sebebi kullanıcıya
            return {"ok": False, "reason": f"hesap okunamadı ({type(e).__name__})"}
        liq = spec.get("liq_pct") or float(getattr(cfg, "wake_liq_pct", 2.0) or 2.0)
        seen = {c: p["side"] for c, p in pos.items()}
        aid = await _insert_alarm(cfg, kind="addr", address=spec["address"], liq_pct=liq,
                                  seen=json.dumps(seen), chat_id=chat_id or "")
        return {"ok": True, "alarm": await get(aid), "positions": pos}
    from ..hl.universe import resolve_coin
    from .report import coin_dex
    rc = await resolve_coin(spec["sym"])
    if not rc:
        return {"ok": False, "reason": f"'{spec['sym']}' HL evreninde yok"}
    coin = rc["coin"]
    try:
        px = (await mids(client, coin_dex(coin))).get(coin)
    except Exception as e:                           # noqa: BLE001
        return {"ok": False, "reason": f"fiyat okunamadı ({type(e).__name__})"}
    if not px:
        return {"ok": False, "reason": f"{sym_of(coin)} için anlık fiyat yok"}
    if spec["kind"] == "pct":
        lo, hi = px * (1 - spec["pct"] / 100), px * (1 + spec["pct"] / 100)
        aid = await _insert_alarm(cfg, kind="pct", coin=coin, lo=lo, hi=hi, ref_px=px,
                                  pct=spec["pct"], chat_id=chat_id or "")
        return {"ok": True, "alarm": await get(aid), "px": px}
    levels = spec["levels"]
    if len(levels) == 2:
        lo, hi = levels
        if not lo < px < hi:
            return {"ok": False, "reason": f"şimdiki fiyat {fpx(px)} iki seviyenin ARASINDA olmalı"}
    elif levels[0] < px:
        lo, hi = levels[0], None
    elif levels[0] > px:
        lo, hi = None, levels[0]
    else:
        return {"ok": False, "reason": f"seviye şimdiki fiyatla aynı ({fpx(px)})"}
    aid = await _insert_alarm(cfg, kind="price", coin=coin, lo=lo, hi=hi, ref_px=px,
                              chat_id=chat_id or "")
    return {"ok": True, "alarm": await get(aid), "px": px}


async def get(aid: int) -> dict | None:
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM wake_alarms WHERE id=?", (int(aid),))
        r = await cur.fetchone()
    return dict(r) if r else None


async def active_alarms() -> list[dict]:
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM wake_alarms WHERE active=1 ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


async def cancel(aid: int, note: str = "kaldırıldı") -> bool:
    async with db() as conn:
        cur = await conn.execute("UPDATE wake_alarms SET active=0, end_note=? WHERE id=? AND active=1",
                                 (note, int(aid)))
        return bool(cur.rowcount)


def describe(a: dict) -> str:
    """'SNDK ≤ 480' · 'SNDK ±%3 (≤ 480.3 / ≥ 510.0)' · '0x9e2c..5508 · liq'e ≤ %2 ya da kapanış'."""
    if a["kind"] == "addr":
        ad = a["address"]
        return f"{ad[:6]}..{ad[-4:]} · liq'e ≤ %{a['liq_pct']:g} ya da pozisyon kapanırsa"
    s = sym_of(a["coin"])
    if a["kind"] == "pct":
        return f"{s} ±%{a['pct']:g} (≤ {fpx(a['lo'])} / ≥ {fpx(a['hi'])})"
    parts = ([f"≤ {fshort(a['lo'])}"] if a.get("lo") is not None else []) + \
            ([f"≥ {fshort(a['hi'])}"] if a.get("hi") is not None else [])
    return f"{s} " + " ya da ".join(parts)


# ---------------- olaylar ----------------

async def fire(cfg, title: str, body: str, speech: str, key: str, source: str = "alarm",
               chat_id: str = "") -> int | None:
    """Uyandırma olayı yaz (teslimatı döngü yapar). Aynı key ikinci kez yazılmaz → None.
    `chat_id`: mesaj (✅ tuşuyla) alarmın kurulduğu sohbete gider; boş = ana sohbet."""
    if not getattr(cfg, "wake_enabled", True) and source != "test":
        return None
    ts = now()
    async with db() as conn:
        cur = await conn.execute(
            "INSERT OR IGNORE INTO wake_events(key, source, title, body, speech, created_ts, next_call_ts, chat_id)"
            " VALUES(?,?,?,?,?,?,?,?)", (key, source, title, body, (speech or title)[:SPEECH_MAX], ts, ts,
                                        str(chat_id or "")))
        return int(cur.lastrowid) if cur.rowcount else None


async def ack(event_id: int | None = None, by: str = "tuş") -> list[dict]:
    """Onay: kalan aramalar iptal. event_id None → açık bütün olaylar. Onaylananlar döner."""
    ts = now()
    async with db() as conn:
        q = "SELECT * FROM wake_events WHERE ack_ts IS NULL"
        args: tuple = ()
        if event_id is not None:
            q += " AND id=?"
            args = (int(event_id),)
        cur = await conn.execute(q, args)
        rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            await conn.execute("UPDATE wake_events SET ack_ts=?, ack_by=?, done_ts=COALESCE(done_ts, ?)"
                               " WHERE id=?", (ts, by, ts, r["id"]))
    return [{**r, "ack_ts": ts, "ack_by": by} for r in rows]


async def event(eid: int) -> dict | None:
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM wake_events WHERE id=?", (int(eid),))
        r = await cur.fetchone()
    return dict(r) if r else None


# ---------------- CallMeBot ----------------

async def callmebot_call(session, user: str, text: str, lang: str, rpt: int = 2) -> tuple[bool, str]:
    """Telegram araması (CallMeBot). (başladı mı, not). Sonuç (açıldı mı) dönmez."""
    params = {"user": user, "text": text[:SPEECH_MAX], "lang": lang, "rpt": str(rpt), "cc": "no"}
    try:
        async with session.get(CALLMEBOT_URL, params=params, timeout=aiohttp.ClientTimeout(total=90)) as r:
            body = (await r.text())[:6000]
            status = r.status
    except Exception as e:                           # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"[:160]
    from ..telegram.format import strip_tags
    # Yanıt bir HTML sayfası; içindeki analytics <script>'i not olmasın (07.10 canlı yanıt)
    body = _SCRIPT_RE.sub(" ", body)
    full = " ".join(strip_tags(body).split())
    plain, low = full[:160], full.lower()          # karar TAM metinden (uyarı 160. karakterden sonra)
    if "not authori" in low or "authorize" in low and "not" in low:
        return False, "izin yok — " + AUTH_HINT
    if status != 200:
        return False, f"HTTP {status}: {plain}"[:160]
    return True, plain or "tamam"


# ---------------- denetim (tetikler) ----------------

_data = {"fail_since": None, "warned": False}


async def check_alarms(cfg, client, ts: int, notify) -> dict:
    """Aktif alarmlar → olay. `notify(text)` veri kesintisi uyarısı için (arama yok)."""
    out = {"active": 0, "fired": 0, "expired": 0, "errors": 0}
    rows = await active_alarms()
    for a in [r for r in rows if r["expires_ts"] and ts >= int(r["expires_ts"])]:
        await cancel(a["id"], "süre doldu")
        out["expired"] += 1
    rows = [r for r in rows if not (r["expires_ts"] and ts >= int(r["expires_ts"]))]
    out["active"] = len(rows)
    if not rows:
        _data.update(fail_since=None, warned=False)
        return out
    from .report import coin_dex
    ok_any, fail_any = False, False
    cache: dict[str, dict] = {}
    for a in rows:
        try:
            if a["kind"] in ("price", "pct"):
                dex = coin_dex(a["coin"])
                if dex not in cache:
                    cache[dex] = await mids(client, dex)
                px = cache[dex].get(a["coin"])
                if px is None:
                    fail_any = True
                    continue
                ok_any = True
                if await _check_level(cfg, a, px, ts):
                    out["fired"] += 1
            else:
                pos = await positions(client, a["address"], _dexes(cfg))
                ok_any = True
                if await _check_addr(cfg, client, a, pos, ts):
                    out["fired"] += 1
        except Exception:
            fail_any = True
            out["errors"] += 1
            log.debug("alarm denetimi #%s", a.get("id"), exc_info=True)
    if ok_any and not fail_any:
        _data.update(fail_since=None, warned=False)
    elif fail_any:
        _data["fail_since"] = _data["fail_since"] or ts
        if ts - _data["fail_since"] >= DATA_STALE and not _data["warned"]:
            _data["warned"] = True
            try:
                await notify("⚠️ <b>Uyandırma alarmı</b>: fiyat / hesap verisi "
                             f"{(ts - _data['fail_since']) // 60} dakikadır okunamıyor — alarm şu an KÖR. "
                             "Veri gelince kendiliğinden düzelir.")
            except Exception:
                log.debug("veri uyarısı", exc_info=True)
    return out


async def _close_alarm(a: dict, ts: int) -> None:
    async with db() as conn:
        await conn.execute("UPDATE wake_alarms SET active=0, fired_ts=?, end_note='tetiklendi' WHERE id=?",
                           (ts, a["id"]))


async def _check_level(cfg, a: dict, px: float, ts: int) -> bool:
    lo, hi = a.get("lo"), a.get("hi")
    down, up = lo is not None and px <= lo, hi is not None and px >= hi
    if not (down or up):
        return False
    s = sym_of(a["coin"])
    from ..telegram.format import tr_time
    when = f"kurulduğunda {fpx(a['ref_px'])}, {tr_time(a['created_ts'])}"
    if a["kind"] == "pct":
        chg = (px - a["ref_px"]) / a["ref_px"] * 100
        title = f"{s} %{abs(chg):.1f} {'düştü' if chg < 0 else 'yükseldi'} — şimdi {fpx(px)}"
        speech = (f"Dikkat. {s} yüzde {say_num(round(abs(chg), 1))} {'düştü' if chg < 0 else 'yükseldi'}. "
                  f"Şu an {say_num(px)}.")
    else:
        lvl = lo if down else hi
        title = f"{s} {fshort(lvl)} {'altına indi' if down else 'üstüne çıktı'} — şimdi {fpx(px)}"
        speech = (f"Dikkat. {s} {say_num(lvl)} {'altına indi' if down else 'üstüne çıktı'}. "
                  f"Şu an {say_num(px)}.")
    body = f"⏰ Alarm #{a['id']}: <b>{title}</b>\n<i>{when}</i>"
    await _close_alarm(a, ts)
    await fire(cfg, title, body, speech + " Uyandıysan Telegram'da uyandım tuşuna bas.", f"alarm:{a['id']}",
               chat_id=a.get("chat_id") or "")
    return True


async def _check_addr(cfg, client, a: dict, pos: dict, ts: int) -> bool:
    seen = json.loads(a.get("seen") or "{}")
    ad = a["address"]
    short = f"{ad[:6]}..{ad[-4:]}"
    hit = None
    for coin, p in pos.items():
        if p.get("liq") and p.get("px"):
            dist = abs(p["px"] - p["liq"]) / p["px"] * 100
            if dist <= float(a["liq_pct"]):
                s = sym_of(coin)
                hit = (f"{s} {p['side'].upper()}: likidasyona %{dist:.1f} kaldı",
                       f"fiyat {fpx(p['px'])} · liq {fpx(p['liq'])} · {short}",
                       f"Dikkat. {s} {p['side']} pozisyon likidasyona yüzde {say_num(round(dist, 1))} uzakta.")
                break
    if hit is None:
        gone = [c for c in seen if c not in pos]
        if gone:
            coin = gone[0]
            kind = "unknown"
            try:
                from .cryptoliq import _closure_kind
                kind, _ = await _closure_kind(client, coin, ad, {"first_ts": a.get("created_ts")})
            except Exception:
                log.debug("kapanış türü", exc_info=True)
            s, side = sym_of(coin), seen[coin]
            what = "LİKİDE OLDU" if kind == "liq" else "kapandı"
            hit = (f"{s} {side.upper()} pozisyonu {what}", short,
                   f"Dikkat. {s} {side} pozisyonu {'likide oldu' if kind == 'liq' else 'kapandı'}.")
    if hit is None:
        new = {c: p["side"] for c, p in pos.items()}
        if new != seen:
            async with db() as conn:
                await conn.execute("UPDATE wake_alarms SET seen=? WHERE id=?",
                                   (json.dumps({**seen, **new}), a["id"]))
        return False
    title, detail, speech = hit
    await _close_alarm(a, ts)
    await fire(cfg, title, f"⏰ Alarm #{a['id']}: <b>{title}</b>\n<i>{detail}</i>",
               speech + " Uyandıysan Telegram'da uyandım tuşuna bas.", f"alarm:{a['id']}",
               chat_id=a.get("chat_id") or "")
    return True


# ---------------- teslimat (mesaj + arama turları) ----------------

async def drive(cfg, bot, session, ts: int) -> dict:
    """Açık olaylar: mesaj (✅ tuşuyla), arama turları, bitiş. Gönderilemeyen mesaj sonraki adımda."""
    from ..telegram import format as fmt
    out = {"open": 0, "calls": 0, "msgs": 0, "failed": 0}
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM wake_events WHERE done_ts IS NULL ORDER BY id")
        evs = [dict(r) for r in await cur.fetchall()]
    out["open"] = len(evs)
    pl, user = plan(cfg), tg_user(cfg)
    main = str(getattr(cfg, "telegram_chat_id", "") or "")
    for ev in evs:
        chat = str(ev.get("chat_id") or "") or main          # alarmın kurulduğu sohbet
        if not ev.get("msg_ts") and bot is not None and chat:
            mid = await _send(bot, chat, fmt.wake_event(ev, user, pl), fmt.wake_ack_kb(ev["id"]))
            if mid is not False:
                await _set(ev["id"], msg_id=mid if type(mid) is int else None, msg_chat=chat, msg_ts=ts)
                out["msgs"] += 1
        if not user:
            await _set(ev["id"], done_ts=ts, last_call_note="WAKE_TELEGRAM_USER yok — arama yapılmadı")
            continue
        n = int(ev.get("call_n") or 0)
        cap = 2 if ev.get("source") == "test" else pl["max_calls"]      # deneme: tek tur
        if n >= cap:
            if ts - int(ev.get("last_call_ts") or 0) >= 45:
                await _set(ev["id"], done_ts=ts)
            continue
        if ts < int(ev.get("next_call_ts") or 0):
            continue
        if ((await event(ev["id"])) or {}).get("ack_ts"):
            continue                                        # tur başından beri ✅'e basıldı
        ok, note = await callmebot_call(session, user, ev["speech"], pl["lang"])
        t2 = now()
        n += 1
        fields = {"call_n": n, "last_call_ts": t2, "last_call_note": ("✓ " if ok else "✗ ") + note,
                  "next_call_ts": t2 + (pl["gap"] if n % 2 else pl["round_gap"])}
        if not ok:
            out["failed"] += 1
            if note.startswith("izin yok"):
                fields["call_n"] = cap                      # izin yoksa yeniden denemek boşuna
            if not ev.get("warned") and bot is not None and chat:
                await _send(bot, chat, f"📞✗ <b>Uyandırma araması yapılamadı</b>: {fmt.esc(note)}", None)
                fields["warned"] = 1
        else:
            out["calls"] += 1
        await _set(ev["id"], **fields)
        await kv_set(STATS_KV, {**((await kv_get(STATS_KV)) or {}), "last_call_ts": t2,
                                "last_call_note": fields["last_call_note"]})
    return out


async def _send(bot, chat: str, text: str, kb: dict | None):
    """Mesaj id'si döner (tuşu sonra düzenlemek için); id okunamazsa True; gönderilemezse False."""
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if kb:
        payload["reply_markup"] = kb
    try:
        st, data = await bot.call("sendMessage", payload, timeout=20)
        if st == 200 and data.get("ok"):
            return int(((data.get("result") or {}).get("message_id")) or 0) or True
    except Exception:
        log.debug("uyandırma mesajı (call)", exc_info=True)
    try:
        return True if await bot.send(text, chat, reply_markup=kb) else False
    except Exception:
        log.debug("uyandırma mesajı (send)", exc_info=True)
        return False


async def _set(eid: int, **f) -> None:
    sets = ", ".join(f"{k}=?" for k in f)
    async with db() as conn:
        await conn.execute(f"UPDATE wake_events SET {sets} WHERE id=?", (*f.values(), int(eid)))


# ---------------- döngü ----------------

async def loop(cfg, client, bot, session) -> None:
    """Denetimli döngü: alarm denetimi `wake_poll_sec`'te bir, teslimat 5 sn'de bir."""
    from ..health import beat
    await asyncio.sleep(20)
    last_check = 0

    async def notify(text: str) -> None:
        if bot is not None and getattr(cfg, "telegram_chat_id", ""):
            await bot.send(text, cfg.telegram_chat_id)
    while True:
        try:
            ts = now()
            st = {}
            if getattr(cfg, "wake_enabled", True):
                if ts - last_check >= max(5, int(getattr(cfg, "wake_poll_sec", 15) or 15)):
                    st = await check_alarms(cfg, client, ts, notify)
                    last_check = ts
            dv = await drive(cfg, bot, session, ts)
            if st:
                await kv_set(STATS_KV, {**((await kv_get(STATS_KV)) or {}), **st, "open": dv["open"],
                                        "ts": ts})
            await beat("wake")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("uyandırma turu")
        await asyncio.sleep(TICK)
