"""Pencere adli incelemesi — "şu dakikalarda ne oldu?"

Sembol + zaman aralığı verilir; o pencerede kimin ne aldığı/sattığı ve
**açtı mı kapattı mı** çıkarımı döner.

İKİ KATMAN, ikisi de gerekli:

  1. TOPLAM OKUMA (güvenilir): bir fill'in `buy`/`sell` olması "açtı mı
     kapattı mı"yı SÖYLEMEZ — bir `sell` hem long kapatmak hem short açmak
     olabilir. Bunu ayıran şey açık pozisyon (OI) değişimidir. Klasik okuma:
       OI↑ fiyat↓ = yeni SHORT · OI↓ fiyat↓ = LONG kapanışı/likidasyon
       OI↑ fiyat↑ = yeni LONG  · OI↓ fiyat↑ = short kapanışı (squeeze)
       OI ~sabit  = el değiştirmiş, yeni para girmemiş
  2. ADRES KIRILIMI (detay): penceredeki fill'ler adrese göre toplanır, sonra
     o adresin bilinen pozisyonu + önceki işlemleriyle birleştirilip etiket
     çıkarılır ("artırdı", "kırptı", "kapattı"…).

DÜRÜSTLÜK KURALLARI — bu dosyanın çoğu bunlarla ilgili:
  • Veri yoksa YORUM YOK. OI örneği yoksa "belirsiz" denir, tahmin yürütülmez.
  • Hareket gürültü sınırındaysa yine "belirsiz" — %0.05'lik OI değişimine
    "yeni short açıldı" demek uydurmadır.
  • `hl_positions` satırı olmayan adres "bilinmiyor"dur; yön uydurulmaz.
  • Pozisyon verisi derin keşif turuna bağlı (75-125 dk) — pencereden çok
    eskiyse işaretlenir. Bayat veriyle kesin konuşmak yanıltmaktır.
  • Kripto fill kaydı sonradan açıldı: ondan ÖNCEKİ bir pencere sorulursa
    "boş" değil "o dönemde kayıt tutulmuyordu" denir.
"""
import logging

from ..db import db, now

log = logging.getLogger("radar.forensics")

# OI/fiyat bu eşiklerin altında kalırsa "belirsiz" — gürültüye anlam yüklemeyiz.
OI_NOISE_PCT = 0.15
PX_NOISE_PCT = 0.05
# Pozisyon ölçümü pencereden bu kadar uzaksa çıkarım bayat sayılır.
PROFILE_STALE_SEC = 3 * 3600
MAX_ADDRS = 60
# Net, brütün bu oranından küçükse "scalp": adres alıp satmış, pozisyonunu
# anlamlı biçimde değiştirmemiş. Sıfıra tam eşitlik aramak $95K alıp $92K satan
# birini "long'unu artırdı" diye etiketliyordu — yanıltıcı.
SCALP_MAX_NET = 0.20


def _pct(new, old):
    try:
        new, old = float(new), float(old)
    except (TypeError, ValueError):
        return None
    return (new - old) / old * 100 if old else None


def read_oi(oi_chg, px_chg) -> dict:
    """OI + fiyat değişiminden "ne oldu" okuması. Veri yoksa BELİRSİZ."""
    if oi_chg is None or px_chg is None:
        return {"verdict": "belirsiz", "why": "OI ölçümü yok — kripto OI kaydı "
                "yeni açıldı ya da bu pencerede örnek düşmemiş"}
    if abs(oi_chg) < OI_NOISE_PCT:
        return {"verdict": "el değiştirmiş",
                "why": f"OI neredeyse sabit (%{oi_chg:+.2f}) — pozisyonlar el "
                       "değiştirmiş, piyasaya yeni para girmemiş"}
    if abs(px_chg) < PX_NOISE_PCT:
        return {"verdict": "belirsiz",
                "why": f"OI %{oi_chg:+.2f} kıpırdamış ama fiyat neredeyse sabit "
                       f"(%{px_chg:+.2f}) — yön okunamıyor"}
    up_oi, up_px = oi_chg > 0, px_chg > 0
    if up_oi and not up_px:
        v, w = "yeni SHORT açılmış", "açık pozisyon arttı, fiyat düştü"
    elif not up_oi and not up_px:
        v, w = "LONG kapanmış / likide olmuş", "açık pozisyon azaldı, fiyat düştü"
    elif up_oi and up_px:
        v, w = "yeni LONG açılmış", "açık pozisyon arttı, fiyat yükseldi"
    else:
        v, w = "SHORT kapanmış (squeeze)", "açık pozisyon azaldı, fiyat yükseldi"
    return {"verdict": v,
            "why": f"{w} (OI %{oi_chg:+.2f}, fiyat %{px_chg:+.2f})"}


async def _metrics_around(coin: str, t0: int, t1: int) -> dict:
    """Pencerenin başındaki ve sonundaki metrik örneği."""
    out = {"first": None, "last": None}
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM asset_metrics WHERE coin=? AND ts<=? ORDER BY ts DESC LIMIT 1",
            (coin, t0))
        r = await cur.fetchone()
        out["first"] = dict(r) if r else None
        cur = await conn.execute(
            "SELECT * FROM asset_metrics WHERE coin=? AND ts<=? ORDER BY ts DESC LIMIT 1",
            (coin, t1))
        r = await cur.fetchone()
        out["last"] = dict(r) if r else None
    # İki uç AYNI örneğe düşüyorsa pencere içinde ölçüm yok demektir; fark
    # sıfır çıkar ve "el değiştirmiş" gibi YANLIŞ bir okuma üretirdi.
    if (out["first"] and out["last"]
            and out["first"]["ts"] == out["last"]["ts"]):
        out["last"] = None
    return out


async def _fills(coin: str, t0: int, t1: int) -> list[dict]:
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM fills WHERE coin=? AND ts>=? AND ts<=? ORDER BY ts",
            (coin, t0, t1))
        return [dict(r) for r in await cur.fetchall()]


def group_fills(rows: list[dict]) -> list[dict]:
    """Adrese göre topla: net $, parça sayısı, taker oranı.

    Aynı adres pencerede hem alıp hem satmış olabilir (scalp) — ikisi de
    tutulur ve NET üzerinden yön verilir; yalnız net'e bakmak "hiç işlem
    yapmamış" gibi gösterirdi.
    """
    by: dict[str, dict] = {}
    for r in rows:
        a = by.setdefault(r["address"], {
            "address": r["address"], "buy": 0.0, "sell": 0.0, "n": 0,
            "tk": 0.0, "known": 0.0, "first_ts": r["ts"], "last_ts": r["ts"]})
        a["buy" if r["side"] == "buy" else "sell"] += float(r["notional"] or 0)
        a["n"] += 1
        a["first_ts"] = min(a["first_ts"], r["ts"])
        a["last_ts"] = max(a["last_ts"], r["ts"])
        if r.get("taker") is not None:
            a["known"] += float(r["notional"] or 0)
            if r["taker"]:
                a["tk"] += float(r["notional"] or 0)
    out = []
    for a in by.values():
        a["net"] = a["buy"] - a["sell"]
        a["gross"] = a["buy"] + a["sell"]
        a["dir"] = "buy" if a["net"] > 0 else ("sell" if a["net"] < 0 else "flat")
        a["taker_pct"] = (a["tk"] / a["known"] * 100) if a["known"] > 0 else None
        out.append(a)
    out.sort(key=lambda x: -x["gross"])
    return out[:MAX_ADDRS]


def infer(a: dict, pos: dict | None, t0: int, t1: int) -> dict:
    """Adresin penceredeki hareketi + bilinen pozisyonu → etiket.

    "bilinmiyor" gerçek bir cevaptır: pozisyonunu görmediğimiz adres için
    yön uydurmak, kullanıcıyı olmayan bir kesinliğe inandırmak olurdu.
    """
    if not pos:
        return {"label": "bilinmiyor", "note": "bu adresin defterini hiç "
                "çekmedik — 'Profilleri tazele' ile şimdi çekebilirsin",
                "stale": False}
    meas = int(pos.get("ts") or 0)
    stale = bool(meas) and (t0 - meas) > PROFILE_STALE_SEC
    closed = int(pos.get("closed_ts") or 0)
    if closed and t0 - PROFILE_STALE_SEC <= closed <= t1 + PROFILE_STALE_SEC:
        return {"label": "pozisyonu kapattı", "stale": stale,
                "note": "kapanış damgası bu pencere civarında"}
    side, first = pos.get("side"), int(pos.get("first_seen_ts") or 0)
    if not side:
        return {"label": "bilinmiyor", "stale": stale, "note": "yön okunamadı"}
    fresh_pos = first and first >= t0 - PROFILE_STALE_SEC
    same_dir = (side == "long" and a["dir"] == "buy") or \
               (side == "short" and a["dir"] == "sell")
    gross = a.get("gross") or 0
    if a["dir"] == "flat" or (gross and abs(a["net"]) / gross < SCALP_MAX_NET):
        return {"label": "aldı ve sattı (scalp)", "stale": stale,
                "note": f"brütün yalnız %{abs(a['net']) / gross * 100:.0f}'i net"
                        f" — {side} pozisyonu esasen duruyor" if gross
                        else f"net sıfır, {side} pozisyonu duruyor"}
    if same_dir:
        return {"label": f"yeni {side} açtı" if fresh_pos else f"{side}'unu artırdı",
                "stale": stale,
                "note": "pozisyon bu pencere civarında ilk kez görüldü"
                        if fresh_pos else "pozisyon öncesinden vardı"}
    return {"label": f"{side}'undan kırptı", "stale": stale,
            "note": "pozisyon yönünün TERSİNE işlem yapmış"}


async def _positions(coin: str, addrs: list[str]) -> dict:
    if not addrs:
        return {}
    q = ",".join("?" * len(addrs))
    async with db() as conn:
        cur = await conn.execute(
            f"""SELECT h.*, a.account_value, a.account_ts
                FROM hl_positions h
                LEFT JOIN addresses a ON a.address = h.address
                WHERE h.coin=? AND h.address IN ({q})""", (coin, *addrs))
        return {r["address"]: dict(r) for r in await cur.fetchall()}


async def _context(coin: str, t0: int, t1: int) -> dict:
    """Aynı pencerede başka ne olmuş: likidasyon, duvar, bizim alarmlar."""
    pad = 900
    out = {}
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM liq_watch WHERE coin=? AND updated_ts BETWEEN ? AND ?"
            " AND stage > 0 ORDER BY updated_ts LIMIT 20", (coin, t0 - pad, t1 + pad))
        out["liq"] = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT * FROM book_walls WHERE coin=? AND last_ts BETWEEN ? AND ?"
            " ORDER BY notional DESC LIMIT 10", (coin, t0 - pad, t1 + pad))
        out["walls"] = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT * FROM alerts_log WHERE ts BETWEEN ? AND ? AND key LIKE ?"
            " ORDER BY ts LIMIT 20", (t0 - pad, t1 + pad, f"%{coin}%"))
        out["alerts"] = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT * FROM vol_events WHERE coin=? AND bucket_ts BETWEEN ? AND ?"
            " ORDER BY bucket_ts LIMIT 10", (coin, t0 - pad, t1 + pad))
        out["vol"] = [dict(r) for r in await cur.fetchall()]
    return out


async def fills_since() -> dict:
    """Fill arşivinin BAŞLANGICI — coin sınıfına göre.

    Kripto kaydı sonradan açıldı; ondan öncesi için "boş" demek yanlış olur,
    "o dönemde kayıt tutulmuyordu" demek doğru.
    """
    async with db() as conn:
        cur = await conn.execute(
            "SELECT MIN(ts) t FROM fills WHERE coin NOT LIKE '%:%'")
        crypto = (await cur.fetchone())["t"]
        cur = await conn.execute(
            "SELECT MIN(ts) t FROM fills WHERE coin LIKE '%:%'")
        equity = (await cur.fetchone())["t"]
    return {"crypto": crypto, "equity": equity}


async def report(coin: str, t0: int, t1: int) -> dict:
    """Pencere raporu. `coin` ham HL adı ('HYPE' ya da 'xyz:NVDA')."""
    t0, t1 = int(min(t0, t1)), int(max(t0, t1))
    is_crypto = ":" not in coin
    met = await _metrics_around(coin, t0, t1)
    oi_chg = px_chg = None
    if met["first"] and met["last"]:
        oi_chg = _pct(met["last"]["oi"], met["first"]["oi"])
        px_chg = _pct(met["last"]["mark_px"], met["first"]["mark_px"])

    rows = await _fills(coin, t0, t1)
    grouped = group_fills(rows)
    pos = await _positions(coin, [g["address"] for g in grouped])
    for g in grouped:
        p = pos.get(g["address"])
        g["pos"] = p
        g["infer"] = infer(g, p, t0, t1)

    since = await fills_since()
    start = since["crypto" if is_crypto else "equity"]
    return {
        "coin": coin, "symbol": coin.split(":")[-1], "is_crypto": is_crypto,
        "t0": t0, "t1": t1, "minutes": max(1, (t1 - t0) // 60),
        "metrics": met, "oi_chg": oi_chg, "px_chg": px_chg,
        "oi": read_oi(oi_chg, px_chg),
        "n_fills": len(rows), "rows": grouped,
        "buy": sum(g["buy"] for g in grouped),
        "sell": sum(g["sell"] for g in grouped),
        "n_known": sum(1 for g in grouped if g["pos"]),
        "n_stale": sum(1 for g in grouped if g["infer"].get("stale")),
        "ctx": await _context(coin, t0, t1),
        # Kayıt başlangıcından ÖNCE bir pencere soruldu mu? "Boş" ile
        # "o dönemde tutmuyorduk" apayrı cevaplar.
        "before_records": bool(start and t1 < int(start)) or not start,
        "records_since": start,
    }


async def refresh_profiles(cfg, client, coin: str, addrs: list[str]) -> dict:
    """Rapordaki adreslerin defterini ANINDA çek (kullanıcı tetikli).

    `sweeper.probe_address` yeniden kullanılıyor — aynı yanıt tipi, aynı yazma
    yolu, aynı 'artık tutmuyor' otoritesi. Tavanla sınırlı: adres başına
    1 istek.
    """
    from .sweeper import probe_address
    cap = max(1, int(getattr(cfg, "forensics_probe_max", 15)))
    out = {"tried": 0, "ok": 0, "err": 0, "err_msg": ""}
    for addr in addrs[:cap]:
        out["tried"] += 1
        try:
            await probe_address(cfg, client, addr)
            out["ok"] += 1
        except Exception as e:
            out["err"] += 1
            if not out["err_msg"]:
                out["err_msg"] = f"{type(e).__name__}: {e}"
                log.warning("profil tazelenemedi (%s): %s", addr, e)
    return out
