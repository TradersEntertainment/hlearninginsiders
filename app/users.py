"""Satılabilir bot — kullanıcı katmanı (DM): kayıt, katman, kota, abonelik, engel.

Tek kiracılı bot dokunulmadan ÜSTÜNE gelir: sahibin sohbeti ve kanalları bot.py'de
kalır; buradaki her şey `users` tablosu (Telegram user id) etrafındadır.
Pro = `pro_until` gelecekte. Kota: ücretsizde TSİ gününe göre günlük sorgu, Pro'da
dakikalık kayan pencere (bellek içi, restart'ta sıfırlanır — zararsız). Sel
koruması: kullanıcı başına dakikada FLOOD_PER_MIN mesaj, üstü sessizce yok sayılır.
Abonelik (S3): `user_kinds` (bildirim türleri) + `user_coins` (boş = tüm coinler)."""
import re
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from .db import db, now

TR = ZoneInfo("Europe/Istanbul")
FLOOD_PER_MIN = 20
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

_minute: dict[int, deque] = {}     # user id → son 60 sn'deki sorgu damgaları (Pro dakika limiti)
_flood: dict[int, deque] = {}      # user id → son 60 sn'deki mesaj damgaları (sel)


def tr_day(ts=None) -> str:
    return datetime.fromtimestamp(ts or now(), TR).strftime("%Y-%m-%d")


def tr_dt(ts) -> str:
    return datetime.fromtimestamp(int(ts), TR).strftime("%d.%m.%Y %H:%M")


def csv_set(v) -> set[str]:
    """Ayar değeri liste (csv tipi) ya da 'a,b,c' string olabilir."""
    if not v:
        return set()
    items = v if isinstance(v, (list, tuple, set)) else str(v).split(",")
    return {str(x).strip() for x in items if str(x).strip()}


def valid_address(s) -> str | None:
    s = (s or "").strip()
    return s.lower() if ADDR_RE.match(s) else None


def is_pro(u, ts=None) -> bool:
    return bool(u and u.get("pro_until") and int(u["pro_until"]) > (ts or now()))


def tier(u, ts=None) -> str:
    return "pro" if is_pro(u, ts) else "free"


def quiet_now(u, ts=None) -> bool:
    """Kullanıcının kendi sessiz saati (TSİ). Ayarsız ya da start==end → kapalı."""
    if not u:
        return False
    s, e = u.get("quiet_start"), u.get("quiet_end")
    if s is None or e is None or int(s) == int(e):
        return False
    s, e = int(s), int(e)
    h = datetime.fromtimestamp(ts or now(), TR).hour
    return (s <= h < e) if s < e else (h >= s or h < e)


def _window(store: dict, uid: int, ts: int, sec: int = 60) -> deque:
    dq = store.setdefault(int(uid), deque())
    while dq and dq[0] <= ts - sec:
        dq.popleft()
    return dq


def flood_ok(uid: int, ts=None) -> bool:
    """Dakikada FLOOD_PER_MIN mesajın üstü sessizce düşer (spam / döngü)."""
    ts = ts or now()
    dq = _window(_flood, uid, ts)
    if len(dq) >= FLOOD_PER_MIN:
        return False
    dq.append(ts)
    return True


def reset_memory() -> None:
    _minute.clear()
    _flood.clear()


# ---------------- kayıt ----------------

async def get(uid: int) -> dict | None:
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM users WHERE id=?", (int(uid),))
        r = await cur.fetchone()
        return dict(r) if r else None


async def get_by_chat(chat_id: str) -> dict | None:
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM users WHERE chat_id=?", (str(chat_id),))
        r = await cur.fetchone()
        return dict(r) if r else None


async def upsert_from_update(frm: dict, chat_id: str) -> dict:
    """Her mesajda: yeni kullanıcı kaydı ya da son görülme + ad güncellemesi.
    Engel işareti kalkar (yazabiliyorsa engel kalkmıştır)."""
    uid = int(frm["id"])
    ts = now()
    async with db() as conn:
        await conn.execute(
            """INSERT INTO users(id, chat_id, username, first_name, lang, created_ts, last_seen_ts)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET chat_id=excluded.chat_id, username=excluded.username,
                 first_name=excluded.first_name, last_seen_ts=excluded.last_seen_ts, blocked_ts=NULL""",
            (uid, str(chat_id), (frm.get("username") or "")[:64], (frm.get("first_name") or "")[:64],
             (frm.get("language_code") or "tr")[:8], ts, ts))
    return await get(uid)


async def mark_blocked(chat_id: str) -> int:
    """403 / hesap silinmiş / sohbet yok → fan-out ve hatırlatmalardan düşer."""
    async with db() as conn:
        cur = await conn.execute(
            "UPDATE users SET blocked_ts=? WHERE chat_id=? AND blocked_ts IS NULL", (now(), str(chat_id)))
        return cur.rowcount or 0


async def grant(uid: int, days: int, src: str = "") -> int:
    """Pro süresi: şimdi ya da mevcut bitişten (hangisi ilerideyse) + gün. Dönüş: yeni bitiş."""
    ts = now()
    u = await get(uid)
    base = max(ts, int((u or {}).get("pro_until") or 0))
    until = base + int(days) * 86400
    async with db() as conn:
        await conn.execute(
            "UPDATE users SET pro_until=?, note=substr(COALESCE(note,'') || ?, -400) WHERE id=?",
            (until, f"[{tr_day(ts)} +{int(days)}g {src}]".strip(), int(uid)))
    return until


async def set_address(uid: int, addr: str | None) -> None:
    async with db() as conn:
        await conn.execute("UPDATE users SET hl_address=? WHERE id=?", (addr, int(uid)))


async def set_quiet(uid: int, start, end) -> None:
    async with db() as conn:
        await conn.execute("UPDATE users SET quiet_start=?, quiet_end=? WHERE id=?", (start, end, int(uid)))


async def get_kinds(uid: int) -> set[str]:
    async with db() as conn:
        cur = await conn.execute("SELECT kind FROM user_kinds WHERE user_id=?", (int(uid),))
        return {r["kind"] for r in await cur.fetchall()}


async def set_kinds(uid: int, kinds) -> None:
    async with db() as conn:
        await conn.execute("DELETE FROM user_kinds WHERE user_id=?", (int(uid),))
        await conn.executemany("INSERT OR IGNORE INTO user_kinds(user_id, kind) VALUES(?,?)",
                               [(int(uid), k) for k in sorted(csv_set(kinds))])


async def get_coins(uid: int) -> set[str]:
    async with db() as conn:
        cur = await conn.execute("SELECT coin FROM user_coins WHERE user_id=?", (int(uid),))
        return {r["coin"] for r in await cur.fetchall()}


async def set_coins(uid: int, coins) -> None:
    async with db() as conn:
        await conn.execute("DELETE FROM user_coins WHERE user_id=?", (int(uid),))
        await conn.executemany("INSERT OR IGNORE INTO user_coins(user_id, coin) VALUES(?,?)",
                               [(int(uid), c) for c in sorted(csv_set(coins))])


# ---------------- kota ----------------

def check_query(u: dict, cfg, ts=None) -> tuple[bool, int | None, str]:
    """(izin, ücretsizde kalan hak, neden). Sayaç İLERLEMEZ — sorgu başarılıysa
    `consume_query`. Pro: dakikalık kayan pencere; ücretsiz: TSİ günü sayacı."""
    ts = ts or now()
    if is_pro(u, ts):
        lim = max(1, int(getattr(cfg, "pro_query_per_min", 6) or 6))
        n = len(_window(_minute, int(u["id"]), ts))
        return (n < lim, None, "" if n < lim else "minute")
    lim = int(getattr(cfg, "free_daily_queries", 3) or 0)
    used = int(u.get("q_used") or 0) if u.get("q_day") == tr_day(ts) else 0
    left = max(0, lim - used)
    return (left > 0, left, "" if left > 0 else "daily")


async def consume_query(u: dict, cfg, ts=None) -> int | None:
    """Sorgu başarılı: sayaçlar ilerler. Dönüş: ücretsizde kalan hak, Pro'da None."""
    ts = ts or now()
    uid = int(u["id"])
    day = tr_day(ts)
    used = (int(u.get("q_used") or 0) if u.get("q_day") == day else 0) + 1
    async with db() as conn:
        await conn.execute(
            "UPDATE users SET q_total=q_total+1, q_used=CASE WHEN q_day=? THEN q_used+1 ELSE 1 END,"
            " q_day=? WHERE id=?", (day, day, uid))
    u["q_day"], u["q_used"] = day, used
    u["q_total"] = int(u.get("q_total") or 0) + 1
    if is_pro(u, ts):
        _window(_minute, uid, ts).append(ts)
        return None
    return max(0, int(getattr(cfg, "free_daily_queries", 3) or 0) - used)


# ---------------- listeler / istatistik ----------------

async def pro_users(ts=None) -> list[dict]:
    ts = ts or now()
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM users WHERE pro_until > ? AND blocked_ts IS NULL ORDER BY id", (ts,))
        return [dict(r) for r in await cur.fetchall()]


async def subscribers(kind: str, coin: str | None = None, ts=None) -> list[dict]:
    """Fan-out hedefleri: Pro + engelsiz + türe abone + (coin filtresi yok ya da eşleşiyor)."""
    ts = ts or now()
    async with db() as conn:
        cur = await conn.execute(
            """SELECT u.* FROM users u JOIN user_kinds k ON k.user_id = u.id AND k.kind = ?
               WHERE u.pro_until > ? AND u.blocked_ts IS NULL
                 AND (NOT EXISTS(SELECT 1 FROM user_coins c WHERE c.user_id = u.id)
                      OR EXISTS(SELECT 1 FROM user_coins c WHERE c.user_id = u.id AND c.coin = ?))
               ORDER BY u.id""", (kind, ts, coin or ""))
        return [dict(r) for r in await cur.fetchall()]


async def all_active(ts=None) -> list[dict]:
    """Duyuru hedefleri: engelsiz herkes."""
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM users WHERE blocked_ts IS NULL ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


async def stats(ts=None) -> dict:
    ts = ts or now()
    day = tr_day(ts)
    async with db() as conn:
        async def one(q, p=()):
            cur = await conn.execute(q, p)
            return (await cur.fetchone())[0]
        return {"total": await one("SELECT COUNT(*) FROM users"),
                "pro": await one("SELECT COUNT(*) FROM users WHERE pro_until > ?", (ts,)),
                "blocked": await one("SELECT COUNT(*) FROM users WHERE blocked_ts IS NOT NULL"),
                "new_24h": await one("SELECT COUNT(*) FROM users WHERE created_ts > ?", (ts - 86400,)),
                "active_7d": await one("SELECT COUNT(*) FROM users WHERE last_seen_ts > ?", (ts - 7 * 86400,)),
                "with_addr": await one("SELECT COUNT(*) FROM users WHERE hl_address IS NOT NULL"),
                "q_today": await one("SELECT COALESCE(SUM(q_used),0) FROM users WHERE q_day=?", (day,)),
                "q_total": await one("SELECT COALESCE(SUM(q_total),0) FROM users"),
                "expiring_3d": await one("SELECT COUNT(*) FROM users WHERE pro_until > ? AND pro_until <= ?",
                                         (ts, ts + 3 * 86400))}
