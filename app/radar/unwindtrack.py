"""👣 Boşaltma takibi — 🧯 mesajlarındaki takip tuşu ve ayrı takip kanalı (UNWIND_TRACK_CHAT_ID).

Kullanıcı (20.10, "büyükler kapatıyor" ekranı): "bu botun attığı pozlar için takip tuşu istiyorum poz kapanana kadar
bildirim atacak her %10 kapanışta adamın diğer top 10 pozunu da listelesin sorsun hepsini takip için şu buton sadece
bildirim gelen X için şu buton, bunlar için yeni chatid ayarlıcam railwayden bu kanala değil, o kanala şunu takibe
başladık cüzdan şu falan diye".

Kararlar:
  • 🧯 ilk ve devam mesajının altında iki tuş: [👣 Takip et: X] · [👣 Hepsini takip et (X + n)]. Tuşlar yalnız
    UNWIND_TRACK_CHAT_ID tanımlıysa (env-only; boşsa tuş yok, takip yok — ana sohbete düşmez).
  • Takip mevcut 👣 pozisyon takibidir (tracker.py, src='unwind'): adet üzerinden, toplamın TRACK_STEP_PCT (%10) her
    değişiminde, tam kapanışta (💀 likidasyon teyidiyle) ve yön değişiminde haber — hepsi UNWIND_TRACK_CHAT_ID'ye,
    pozisyon kapanana kadar. Liq fiyatı kayması notu bu takiplerde yok (istenen yalnız kapanış adımları; çapraz
    teminatta sık oynar).
  • Başlangıç, adım, kapanış ve yön mesajlarında cüzdanın öteki açık pozisyonları (≥ UNWIND_OTHER_MIN_USD, büyükten ilk
    10; 👣 = takipte) ve soru tuşları: [👣 Hepsini takip et (+k)] · [🎯 Yalnız X] (cüzdanın öteki 🧯 takiplerini
    bırakır, mesajın soru tuşlarını kaldırır).
  • "Hepsi" = X + öteki ilk 10 (≥ $100K). Aynı cüzdan + coin zaten takipteyse (hangi yoldan olursa olsun) yeniden açılmaz.
  • Cüzdan sorgusu (bütün dex'ler, ~11 istek) adres başına 15 sn önbellekli: aynı turdaki birden çok adım tek sorgu.
    Takip mesajı liste için en çok EXTRAS_TIMEOUT (8 sn) bekler; aşılırsa liste olmadan gider (kapanış geç kalmasın).
  • Takip bildirimleri (notify_track) kapalıysa tuş takip açmaz (yoksa ilk mesajdan sonra kanal sessiz kalırdı).

İnceleme (20.10, yeniden üretildi): sorgu düşünce "Hepsini" tuşu yine kalkıyordu ("tekrar dene" denip tuş yok);
"Yalnız X", X kapanmış / bırakılmışken öteki takipleri bırakıp "X takipte" diyordu; X tamamen satılmışken de "Takip et: X"
çıkıyordu; ↩️ geri alınca soru satırı dönmüyordu; takip kanalına yazılamazsa takipler sessizce birikiyordu.

Kör nokta, açıkça: takip TRACK_POLL_SEC'de (120 sn) bir bakar — iki bakış arasında %10'dan fazla kapanırsa tek mesajda
toplu yazılır; öteki pozisyon listesi mesaj anındaki durumdur (15 sn önbellek); X zaten başka yoldan takipteyse (ör. ana
sohbetten /takip) haberleri o takibin sohbetine gider.
"""
from __future__ import annotations

import logging
import time

from ..db import db

log = logging.getLogger("radar.unwindtrack")

TOP = 10                       # listelenen / "hepsi"ne giren öteki pozisyon sayısı
CACHE_SEC = 15                 # cüzdan sorgusu önbelleği (adres başına): aynı turdaki adımlar tek sorgu; tuştan sonraki
                               # tur bayat liste görmesin (inceleme 20.10)
EXTRAS_TIMEOUT = 8.0           # takip mesajı liste için en çok bu kadar bekler (kapanış / yön kritik: geç kalmasın)
_CACHE: dict[str, tuple[float, dict]] = {}


def chat(cfg) -> str:
    return (getattr(cfg, "unwind_track_chat_id", "") or "").strip()


def _other_min(cfg) -> float:
    v = getattr(cfg, "unwind_other_min_usd", 100_000)
    return float(100_000 if v is None else v)


def _key(dex: str) -> str:
    return dex or "main"                                # clearinghouse_all anahtarı


async def wallet(cfg, client, addr: str, fresh: bool = False) -> dict:
    """Cüzdanın bütün dex defterleri {dex: state}; 60 sn önbellek (tuşta `fresh`)."""
    from .unwind import _dexes
    t = time.time()
    hit = _CACHE.get(addr)
    if hit and not fresh and t - hit[0] < CACHE_SEC:
        return hit[1]
    states = await client.clearinghouse_all(addr, await _dexes(cfg))
    _CACHE[addr] = (t, states)
    for a in [a for a, (ts, _) in _CACHE.items() if t - ts > 10 * CACHE_SEC]:
        _CACHE.pop(a, None)
    return states


def positions(states: dict) -> list[dict]:
    from .unwind import _positions
    return _positions(states)


def others(cfg, pos: list[dict], coin: str) -> list[dict]:
    """X dışındaki açık pozisyonlar (≥ UNWIND_OTHER_MIN_USD), büyükten."""
    m = _other_min(cfg)
    return [p for p in pos if p["coin"] != coin and p["usd"] >= m]


def live_of(states: dict, coin: str) -> dict | None:
    from .tracker import parse_position
    from .unwind import _dex_of
    return parse_position(states.get(_key(_dex_of(coin))), coin)


async def tracked(addr: str, full: bool = False) -> dict:
    """Cüzdanın AÇIK takipleri (her kaynaktan): coin → takip id (`full`: coin → satır)."""
    async with db() as conn:
        cur = await conn.execute("SELECT id, coin, chat_id FROM trackers WHERE active=1 AND address=?", (addr,))
        rows = [dict(r) for r in await cur.fetchall()]
    return {r["coin"]: r for r in rows} if full else {r["coin"]: int(r["id"]) for r in rows}


async def follow(cfg, addr: str, coins: list[str], states: dict) -> tuple[list[dict], list[str]]:
    """Sırayla: canlı pozisyon varsa ve takipte değilse 👣 takip aç (kanal UNWIND_TRACK_CHAT_ID, src='unwind')."""
    from .. import assets
    from .tracker import start_tracker
    have = await tracked(addr, full=True)
    started, notes = [], []
    tchat = chat(cfg)
    for c in coins:
        sym = assets.label(c)
        if c in have:
            hc = (have[c].get("chat_id") or "").strip()
            where = "" if hc == tchat else (", ana sohbette" if not hc else ", başka kanalda")
            notes.append(f"{sym} zaten takipte (#{have[c]['id']}{where})")
            continue
        live = live_of(states, c)
        if not live:
            notes.append(f"{sym} kapanmış")
            continue
        tid = await start_tracker(cfg, addr, c, sym, live, chat_id=tchat, src="unwind")
        have[c] = {"id": tid, "chat_id": tchat}
        started.append({"id": tid, "coin": c, "symbol": sym, "live": live})
    return started, notes


async def _vol24(coins) -> dict:
    from .twaplive import volumes_cached
    try:
        vols = await volumes_cached(None)
    except Exception:                                       # noqa: BLE001 — hacim yoksa oran "bilinmiyor"
        vols = {}
    return {c: (vols.get(c) or (None,))[0] for c in coins}


async def block(cfg, addr: str, coin: str, pos: list[dict]) -> tuple[str, int]:
    """Öteki pozisyonlar satırı + takipte olmayanların sayısı ("Hepsini takip et (+k)")."""
    from ..telegram import format as fmt
    oth = others(cfg, pos, coin)
    have = await tracked(addr)
    new = sum(1 for p in oth[:TOP] if p["coin"] not in have)
    vol24 = await _vol24([p["coin"] for p in oth[:TOP]])
    return fmt.unwind_others_line(oth, vol24, set(have), _other_min(cfg), TOP), new


async def extras(cfg, client, t: dict, closed: bool = False) -> tuple[str, dict | None]:
    """Takip mesajının eki (tracker._check_one, src='unwind'): öteki pozisyonlar + tuşlar. Sorgu düşerse liste yok,
    yalnız takip tuşları (mesaj gecikmez)."""
    from ..telegram import format as fmt
    import asyncio
    try:
        pos = positions(await asyncio.wait_for(wallet(cfg, client, t["address"]), EXTRAS_TIMEOUT))
        txt, new = await block(cfg, t["address"], t["coin"], pos)
    except Exception:                                       # noqa: BLE001 — zaman aşımı dahil: liste yok, mesaj gider
        log.debug("boşaltma takibi: öteki pozisyonlar okunamadı %s", t.get("address"), exc_info=True)
        txt, new = "", None                                 # sayı bilinmiyor: "Hepsini takip et" sayısız
    kb = fmt.unwind_track_kb(int(t["id"]), t.get("symbol") or "", new, wake=bool(t.get("wake")), closed=closed)
    return txt, kb


async def from_offer(cfg, client, offer_id: int, all_: bool) -> dict:
    """🧯 mesajındaki tuş. Dönüş: {toast, text (takip kanalına | None), kb, ids (açılan takipler)}."""
    from .. import assets
    from ..notify import kind_enabled
    from ..telegram import format as fmt
    out = {"toast": "", "text": None, "kb": None, "ids": []}
    if not chat(cfg):
        return {**out, "toast": "UNWIND_TRACK_CHAT_ID tanımsız — takip kanalı yok"}
    if not kind_enabled(cfg, "track"):
        return {**out, "toast": "👣 Takip bildirimleri kapalı (ayarlar: takip) — takip açılmadı"}
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM track_offers WHERE id=? AND kind='unwind'", (int(offer_id),))
        row = await cur.fetchone()
    if not row:
        return {**out, "toast": "Takip teklifi bulunamadı (14 günden eskiyse silinir)"}
    addr, coin = row["address"], row["coin"]
    try:
        states = await wallet(cfg, client, addr, fresh=True)
    except Exception as e:                                  # noqa: BLE001
        return {**out, "toast": f"Pozisyon okunamadı, tekrar dene ({type(e).__name__})"}
    pos = positions(states)
    coins = [coin] + ([p["coin"] for p in others(cfg, pos, coin)[:TOP]] if all_ else [])
    started, notes = await follow(cfg, addr, coins, states)
    if not started:
        return {**out, "toast": "👣 Yeni takip yok: " + " · ".join(notes)}
    txt, new = await block(cfg, addr, coin, pos)
    xt = (await tracked(addr)).get(coin)                # "Yalnız X" = mesajdaki coin (önceden takipte olsa da)
    if xt:
        kb = fmt.unwind_track_kb(xt, assets.label(coin), new, single=len(started) == 1 and started[0]["id"] == xt)
    else:                                               # X kapanmış: yalnız "hepsini" (takipte olmayan öteki varsa)
        kb = fmt.unwind_track_kb(started[0]["id"], started[0]["symbol"], new, closed=True)
    toast = (f"👣 {started[0]['symbol']} takibe alındı" if len(started) == 1
             else f"👣 {len(started)} pozisyon takibe alındı") + " → takip kanalı"
    return {"toast": toast, "text": fmt.unwind_track_started(addr, started, notes, txt, cfg), "kb": kb,
            "ids": [s["id"] for s in started]}


async def follow_rest(cfg, client, tid: int) -> dict:
    """[👣 Hepsini takip et] (takip mesajında): aynı cüzdanın öteki ilk 10'undan takipte olmayanlar.
    Dönüş: {toast, text, ids, done}; cüzdan sorgusu düşerse done=False → tuş kalır, yeniden basılır."""
    from ..notify import kind_enabled
    from ..telegram import format as fmt
    out = {"toast": "", "text": None, "kb": None, "ids": [], "done": False}
    if not chat(cfg):
        return {**out, "toast": "UNWIND_TRACK_CHAT_ID tanımsız — takip kanalı yok"}
    if not kind_enabled(cfg, "track"):
        return {**out, "toast": "👣 Takip bildirimleri kapalı (ayarlar: takip) — takip açılmadı"}
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM trackers WHERE id=?", (int(tid),))
        row = await cur.fetchone()
    if not row:
        return {**out, "toast": f"Takip #{tid} bulunamadı", "done": True}
    addr, coin = row["address"], row["coin"]
    try:
        states = await wallet(cfg, client, addr, fresh=True)
    except Exception as e:                                  # noqa: BLE001
        return {**out, "toast": f"Pozisyon okunamadı, tekrar dene ({type(e).__name__})"}
    pos = positions(states)
    started, notes = await follow(cfg, addr, [p["coin"] for p in others(cfg, pos, coin)[:TOP]], states)
    if not started:
        return {**out, "done": True,
                "toast": "👣 Yeni takip yok" + (": " + " · ".join(notes) if notes else " (öteki pozisyon kalmadı)")}
    txt, _ = await block(cfg, addr, coin, pos)
    return {**out, "done": True, "toast": f"👣 {len(started)} pozisyon daha takibe alındı",
            "text": fmt.unwind_track_started(addr, started, notes, txt, cfg), "ids": [s["id"] for s in started]}


async def only(tid: int) -> tuple[str, int]:
    """[🎯 Yalnız X]: aynı cüzdanın öteki AÇIK 🧯 takiplerini elle bırak — yalnız X'in takibi hâlâ açıksa (X kapanmış /
    bırakılmışken öteki takipleri bırakmak cüzdanı tümden takipsiz bırakırdı). Dönüş: (toast, bırakılan sayı)."""
    from .trackctl import MANUAL
    async with db() as conn:
        cur = await conn.execute("SELECT address, symbol, active, end_note FROM trackers WHERE id=?", (int(tid),))
        row = await cur.fetchone()
        if not row:
            return f"Takip #{tid} bulunamadı", 0
        if not row["active"]:
            return (f"{row['symbol']} takibi açık değil ({row['end_note'] or 'bitti'}) — öteki takipler"
                    " bırakılmadı"), 0
        cur = await conn.execute("UPDATE trackers SET active=0, end_note=? WHERE active=1 AND address=? AND id!=?"
                                 " AND src='unwind'", (MANUAL, row["address"], int(tid)))
        n = int(cur.rowcount or 0)
    return (f"🎯 Yalnız {row['symbol']} takipte" + (f" · {n} takip bırakıldı (geri almak: 👣 Hepsini takip et)"
                                                       if n else "")), n


async def drop(ids: list[int], why: str) -> None:
    """Takip kanalına yazılamadı: az önce açılan takipleri kapat (yoksa her turda boşa sorgu + gönderim)."""
    if not ids:
        return
    async with db() as conn:
        await conn.execute(f"UPDATE trackers SET active=0, end_note=? WHERE id IN ({','.join('?' * len(ids))})",
                           (why, *[int(i) for i in ids]))


async def open_count() -> int:
    async with db() as conn:
        cur = await conn.execute("SELECT COUNT(*) n FROM trackers WHERE active=1 AND src='unwind'")
        r = await cur.fetchone()
    return int(r["n"] or 0) if r else 0
