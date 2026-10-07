"""Takip kontrolü — bırak / geri al / durum: 👣 pozisyon (trackers), 🧲 yapışkan duvar
(sticky_follows), 👁 TWAP emri (twap_follows).

Kullanıcı (07.10): bildirimlerin içine "takibi bırak" tuşu. Karar: tek dokunuş + ↩️ Geri al
(telefonda yanlışlıkla dokunmak kolay; yeniden başlatılan takipte başlangıç boyutu sıfırlanır,
geri alınan takip kaldığı yerden sürer). `/birak_N` komutları ve tuş AYNI yoldan geçer.

Geri alma yalnız ELLE bırakılmış takipte (end_note = MANUAL): kapanan / likide olan / süresi
dolan / emri biten takip geri alınmaz — o durumda haber zaten gitmiştir.
"""
from __future__ import annotations

from .. import assets
from ..db import db, now

MANUAL = "elle bırakıldı"
KINDS = ("pos", "wall", "twap")
_NAME = {"pos": "👣 Takip", "wall": "🧲 Duvar takibi", "twap": "👁 TWAP takibi"}


def _label(kind: str, fid: int, sym: str) -> str:
    return f"{_NAME[kind]} #{fid}" + (f" {sym}" if sym else "")


async def state(kind: str, fid: int) -> dict | None:
    """{active, end_note, label, ...} ya da None (böyle bir takip yok)."""
    if kind not in KINDS:
        return None
    async with db() as conn:
        if kind == "pos":
            cur = await conn.execute("SELECT id, active, end_note, symbol, coin, address, wake FROM trackers"
                                     " WHERE id=?", (int(fid),))
        elif kind == "wall":
            cur = await conn.execute(
                "SELECT f.id, f.active, f.end_note, f.wall_id, w.coin, w.active wall_active, w.end_ts"
                " FROM sticky_follows f LEFT JOIN sticky_walls w ON w.id=f.wall_id WHERE f.id=?",
                (int(fid),))
        else:
            cur = await conn.execute("SELECT id, active, end_note, coin, address FROM twap_follows"
                                     " WHERE id=?", (int(fid),))
        r = await cur.fetchone()
    if not r:
        return None
    d = dict(r)
    sym = d.get("symbol") or (assets.label(d["coin"]) if d.get("coin") else "")
    return {**d, "active": bool(d.get("active")), "label": _label(kind, int(fid), sym)}


async def stop(kind: str, fid: int) -> bool:
    """Aktif takibi elle bırak. Zaten bitmişse False."""
    if kind == "pos":
        async with db() as conn:
            cur = await conn.execute("UPDATE trackers SET active=0, end_note=? WHERE id=? AND active=1",
                                     (MANUAL, int(fid)))
            return bool(cur.rowcount)
    if kind == "wall":
        from .stickywall import follow_stop
        return await follow_stop(int(fid), MANUAL)
    if kind == "twap":
        from .twapfollow import stop as twap_stop
        return await twap_stop(int(fid), MANUAL)
    return False


async def resume(kind: str, fid: int) -> tuple[bool, str]:
    """Elle bırakılmış takibi kaldığı yerden aç. (başardı mı, değilse neden)."""
    st = await state(kind, fid)
    if st is None:
        return False, "böyle bir takip yok"
    if st["active"]:
        return False, "takip zaten açık"
    if st.get("end_note") != MANUAL:
        return False, f"takip bitmiş ({st.get('end_note') or 'kapandı'}) — geri alınamaz"
    table = {"pos": "trackers", "wall": "sticky_follows", "twap": "twap_follows"}[kind]
    async with db() as conn:
        if kind == "pos":
            cur = await conn.execute("SELECT id FROM trackers WHERE active=1 AND address=? AND coin=? AND id!=?",
                                     (st["address"], st["coin"], int(fid)))
            dup = await cur.fetchone()
            if dup:
                return False, f"bu balina için yeni takip açık (#{dup['id']})"
        if kind == "wall":
            if st.get("wall_active") is None:                 # duvar kaydı budanmış
                return False, "duvar kaydı yok — geri alınacak bir şey kalmadı"
            from .stickywall import REJOIN_SEC
            if not st["wall_active"] and now() - int(st.get("end_ts") or 0) >= REJOIN_SEC:
                return False, "duvar bitti — geri alınacak bir şey kalmadı"
        cur = await conn.execute(f"UPDATE {table} SET active=1, end_note=NULL"
                                 " WHERE id=? AND active=0 AND end_note=?", (int(fid), MANUAL))
        if not cur.rowcount:
            return False, "takip durumu değişti — yeniden dene"
    return True, ""


async def set_wake(fid: int, on: bool) -> bool:
    """👣 pozisyon takibinde "kapanırsa / yön değiştirirse beni uyandır" (yalnız aktif takipte)."""
    async with db() as conn:
        cur = await conn.execute("UPDATE trackers SET wake=? WHERE id=? AND active=1", (1 if on else 0, int(fid)))
        return bool(cur.rowcount)


async def still_active(kind: str, fid: int) -> bool:
    """Göndermeden hemen önce: tur başında okunan takip bu arada bırakıldı mı?"""
    st = await state(kind, fid)
    return bool(st and st["active"])
