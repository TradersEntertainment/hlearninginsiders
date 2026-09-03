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
# OI okuması bu kadar geniş bir pencereye kadar genişletilebilir. Örnekleme
# 300 sn olduğu için 5 dakikalık bir olayda iki uç sık sık aynı örneğe düşüyor;
# bir örnek geri gitmek sorunu çözer, ama 30 dakikayı aşan bir farkı olayın
# kendisine yormak uydurma olur.
OI_MAX_SPAN = 1800
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


async def _sample_after(conn, coin: str, ts: int) -> dict | None:
    cur = await conn.execute(
        "SELECT * FROM asset_metrics WHERE coin=? AND ts>? ORDER BY ts LIMIT 1",
        (coin, ts))
    r = await cur.fetchone()
    return dict(r) if r else None


async def _sample_at(conn, coin: str, ts: int) -> dict | None:
    cur = await conn.execute(
        "SELECT * FROM asset_metrics WHERE coin=? AND ts<=? ORDER BY ts DESC LIMIT 1",
        (coin, ts))
    r = await cur.fetchone()
    return dict(r) if r else None


async def _metrics_around(coin: str, t0: int, t1: int,
                          max_span: int = OI_MAX_SPAN) -> dict:
    """Pencerenin iki ucundaki metrik örneği — gerekirse GENİŞLETİLMİŞ.

    OI 300 saniyede bir örnekleniyor, hacim alarmının kovası da 300 saniye:
    pencerenin içine hiç örnek düşmediğinde iki uç AYNI satıra çakılıyor ve
    okuma her seferinde "belirsiz" çıkıyordu. Böyle bir çakışmada pencereden
    SONRAKİ ilk örnek `last` olarak alınır (`widened`) — böylece iki uç olayı
    gerçekten kuşatır. Geriye gitmek işe yaramaz: olaydan önce biten bir
    aralığı ölçmüş olurduk.

    Ama genişletme sınırsız DEĞİL. Üç red kuralı, üçü de aynı sebeple:
    ölçmediğimiz bir şeyi ölçmüş gibi göstermemek.
      • `last.ts <= first.ts` → ortada fark yok
      • `span > max_span`     → 2 saatlik bir OI hareketini 5 dakikalık olaya
                                yormak, "belirsiz" demekten DAHA KÖTÜ bir yalan
      • `last.ts < t0`        → ölçüm olayın tamamen ÖNCESİNDE kalmış
    Reddedilirse `last = None` olur ve `read_oi` "belirsiz" der.

    Alarm anında pencereden sonraki örnek HENÜZ GELMEMİŞ olabilir; o zaman
    okuma dürüstçe "belirsiz" kalır — uydurmaktansa susmak.
    """
    out = {"first": None, "last": None, "widened": False, "span": None}
    async with db() as conn:
        out["first"] = await _sample_at(conn, coin, t0)
        out["last"] = await _sample_at(conn, coin, t1)
        if (out["first"] and out["last"]
                and out["first"]["ts"] == out["last"]["ts"]):
            # İLERİ doğru genişletiyoruz, geri değil: geri gitmek pencerenin
            # ÖNÜNDE biten bir aralığı ölçerdi ve olayı hiç kapsamazdı.
            # Olaydan SONRAKİ ilk örnek, iki ucu birlikte olayı kuşatır.
            out["last"] = await _sample_after(conn, coin, t1)
            out["widened"] = bool(out["last"])
    f, l = out["first"], out["last"]
    if not f or not l:
        out["last"] = None if not f else out["last"]
        return out
    span = int(l["ts"]) - int(f["ts"])
    out["span"] = span
    if span <= 0 or span > int(max_span) or int(l["ts"]) < int(t0):
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


def infer(a: dict, pos: dict | None, t0: int, t1: int,
          probed_ts: int = 0) -> dict:
    """Adresin penceredeki hareketi + bilinen pozisyonu → etiket.

    "bilinmiyor" gerçek bir cevaptır: pozisyonunu görmediğimiz adres için
    yön uydurmak, kullanıcıyı olmayan bir kesinliğe inandırmak olurdu. Ama
    "bilmiyoruz" ile "baktık, taşımıyor" da AYRI cevaplar — `probed_ts` ikisini
    ayırır ve ikincisi gerçek bir bilgidir.
    """
    if not pos:
        # ÜÇ AYRI CEVAP. Eskiden üçü de "bilinmiyor" diyordu ve not, eşik altı
        # bir pozisyon için YANLIŞ tavsiye veriyordu ("tazele ile çekebilirsin"
        # — tazelesen de yazılmıyordu).
        if not probed_ts:
            return {"label": "bilinmiyor", "stale": False,
                    "note": "bu adresin defterini hiç çekmedik —"
                            " 'Profilleri tazele' ile şimdi çekebilirsin"}
        return {"label": "pozisyon taşımıyor", "stale": False,
                "note": "defterine baktık, bu coinde açık pozisyonu yok —"
                        " alıp satmış ve düz kalmış olabilir"}
    meas = int(pos.get("ts") or 0)
    # BAYATLIK İKİ YÖNLÜ: eskiden yalnız `t0 - meas` bakılıyordu, yani
    # pencereden SONRA ölçülmüş bir pozisyon asla bayat sayılmıyordu — üç gün
    # önceki bir pencereyi bugünkü fotoğrafla ölçüp emin konuşuyorduk.
    stale = bool(meas) and not (t0 - PROFILE_STALE_SEC <= meas
                                <= t1 + PROFILE_STALE_SEC)
    closed = int(pos.get("closed_ts") or 0)
    if closed and t0 - PROFILE_STALE_SEC <= closed <= t1 + PROFILE_STALE_SEC:
        return {"label": "pozisyonu kapattı", "stale": stale,
                "note": "kapanış damgası bu pencere civarında"}
    if closed and closed < t0 - PROFILE_STALE_SEC:
        # HAYALET ETİKET: eskiden buradan alt dallara düşüp ARTIK OLMAYAN bir
        # pozisyonun yönüyle "long'unu artırdı" diyordu. Pozisyon pencereden
        # önce kapanmıştı; o yönle konuşmak uydurma olur.
        return {"label": "pozisyon taşımıyor", "stale": stale,
                "note": "bilinen pozisyonu bu pencereden ÖNCE kapanmış"}
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
    """Adres -> bu coindeki son bilinen pozisyon. İKİ TABLO birleşir.

    `addr_positions` TABAN'dır: her boyutta ve daha eksiksiz. `hl_positions`
    yalnız kademe üstünü tutar ama `first_seen_ts`/`peak_notional` gibi TARİHÇE
    orada — "yeni açtı" mı "artırdı" mı ayrımı bunlara bakıyor. İkisi ayrı
    tablo, tek okuma.
    """
    if not addrs:
        return {}
    q = ",".join("?" * len(addrs))
    out: dict[str, dict] = {}
    async with db() as conn:
        cur = await conn.execute(
            f"""SELECT p.*, a.account_value, a.account_ts
                FROM addr_positions p
                LEFT JOIN addresses a ON a.address = p.address
                WHERE p.coin=? AND p.address IN ({q})""", (coin, *addrs))
        for r in await cur.fetchall():
            out[r["address"]] = dict(r)
        cur = await conn.execute(
            f"""SELECT h.*, a.account_value, a.account_ts
                FROM hl_positions h
                LEFT JOIN addresses a ON a.address = h.address
                WHERE h.coin=? AND h.address IN ({q})""", (coin, *addrs))
        for r in await cur.fetchall():
            h = dict(r)
            base = out.get(h["address"])
            if not base:
                out[h["address"]] = h
                continue
            # Tarihçeyi rekor arşivinden al; anlık değerler tazeyi taşıyan
            # addr_positions'tan kalsın (o her turda yazılıyor).
            for k in ("first_seen_ts", "peak_notional", "peak_ts"):
                if h.get(k) is not None:
                    base[k] = h[k]
            if base.get("closed_ts") is None and h.get("closed_ts") is not None \
                    and int(h["ts"] or 0) > int(base.get("ts") or 0):
                base["closed_ts"] = h["closed_ts"]
    return out


async def probed(addrs: list[str]) -> dict[str, int]:
    """Adres -> defterini en son ne zaman çektik (yoksa 0).

    "Hiç uğramadık" ile "uğradık ama bu coinde pozisyonu yok" APAYRI cevaplar;
    ayıran tek şey bu. `addresses` satırının VARLIĞI yetmez — collector her
    fill için satır açıyor, o yüzden ölçüt `probed_ts`.
    """
    if not addrs:
        return {}
    q = ",".join("?" * len(addrs))
    async with db() as conn:
        cur = await conn.execute(
            f"SELECT address, probed_ts FROM addresses WHERE address IN ({q})",
            tuple(addrs))
        return {r["address"]: int(r["probed_ts"] or 0)
                for r in await cur.fetchall()}


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
    addrs = [g["address"] for g in grouped]
    pos = await _positions(coin, addrs)
    prb = await probed(addrs)
    for g in grouped:
        p = pos.get(g["address"])
        g["pos"] = p
        g["probed_ts"] = prb.get(g["address"], 0)
        g["infer"] = infer(g, p, t0, t1, g["probed_ts"])

    since = await fills_since()
    start = since["crypto" if is_crypto else "equity"]
    return {
        "coin": coin, "symbol": coin.split(":")[-1], "is_crypto": is_crypto,
        "t0": t0, "t1": t1, "minutes": max(1, (t1 - t0) // 60),
        "metrics": met, "oi_chg": oi_chg, "px_chg": px_chg,
        "oi": read_oi(oi_chg, px_chg),
        # Ölçüm penceresi genişletildiyse SÖYLENİR: okuma 5 dakikanın değil
        # bu kadar saniyenin okuması demektir.
        "widened": bool(met.get("widened")), "span": met.get("span"),
        "n_fills": len(rows), "rows": grouped,
        "buy": sum(g["buy"] for g in grouped),
        "sell": sum(g["sell"] for g in grouped),
        "n_known": sum(1 for g in grouped if g["pos"]),
        # Baktığımız ama pozisyon taşımayan adresler: "bilmiyoruz" DEĞİL,
        # bilgi. Künye ikisini ayrı saymazsa sayfa hâlâ eksik görünür.
        "n_flat": sum(1 for g in grouped
                      if not g["pos"] and g.get("probed_ts")),
        "n_unseen": sum(1 for g in grouped
                        if not g["pos"] and not g.get("probed_ts")),
        "n_stale": sum(1 for g in grouped if g["infer"].get("stale")),
        "ctx": await _context(coin, t0, t1),
        # Kayıt başlangıcından ÖNCE bir pencere soruldu mu? "Boş" ile
        # "o dönemde tutmuyorduk" apayrı cevaplar.
        "before_records": bool(start and t1 < int(start)) or not start,
        "records_since": start,
    }


async def refresh_profiles(cfg, client, coin: str, addrs: list[str],
                           cap: int | None = None) -> dict:
    """Rapordaki adreslerin defterini ANINDA çek (kullanıcı ya da alarm tetikli).

    `sweeper.probe_address` yeniden kullanılıyor — aynı yanıt tipi, aynı yazma
    yolu, aynı 'artık tutmuyor' otoritesi. Tavanla sınırlı: adres başına
    1 istek. `cap` verilmezse sayfa tavanı (`forensics_probe_max`) geçerli;
    alarm yolu kendi (çok daha küçük) tavanını geçirir.

    BÜTÇE BİLİNMEYENE HARCANIR. Eskiden gelen listenin ilk N'i alınıyordu, yani
    hacimce en büyükler — ki onlar zaten bildiğimiz satırlardı; alt sıradaki
    bilinmeyenlere hiç sıra gelmiyordu. `coin` verilmişse önce o coinde
    pozisyonu bilinmeyen/bayat olanlar sıraya girer.
    """
    from .sweeper import probe_address
    cap = int(cap if cap is not None else getattr(cfg, "forensics_probe_max", 15))
    if cap <= 0:
        return {"tried": 0, "ok": 0, "err": 0, "err_msg": "", "wrote": 0,
                "flat": 0, "skipped": 0}
    todo = list(addrs)
    if coin:
        known = await _positions(coin, todo)
        prb = await probed(todo)
        cut = now() - PROFILE_STALE_SEC
        def rank(a):
            if a not in known:
                return (0, 0) if prb.get(a, 0) < cut else (2, 0)
            return (1, 0) if int(known[a].get("ts") or 0) < cut else (3, 0)
        todo.sort(key=rank)                 # 0: hiç bilinmeyen → 3: taze bilinen
    out = {"tried": 0, "ok": 0, "err": 0, "err_msg": "", "wrote": 0,
           "flat": 0, "skipped": max(0, len(addrs) - cap)}
    for addr in todo[:cap]:
        out["tried"] += 1
        try:
            await probe_address(cfg, client, addr)
            out["ok"] += 1
        except Exception as e:
            out["err"] += 1
            if not out["err_msg"]:
                out["err_msg"] = f"{type(e).__name__}: {e}"
                log.warning("profil tazelenemedi (%s): %s", addr, e)
    # DÜRÜST SONUÇ: "15/15 tazelendi" deyip satırların yine "bilinmiyor"
    # kalması en kafa karıştırıcı hâldi. Kaçında gerçekten pozisyon çıktığını
    # sayıp söylüyoruz.
    if coin and out["ok"]:
        after = await _positions(coin, todo[:cap])
        out["wrote"] = len(after)
        out["flat"] = out["ok"] - len(after)
    return out


async def listening(coin: str) -> bool | None:
    """Bu coin'in canlı akışını dinliyor muyuz?

    ÜÇ CEVAP, üçü de farklı: `True` dinliyoruz · `False` dinlemiyoruz ·
    **`None` bilmiyoruz** (collector henüz evrenini yazmadı). `None`'ı `False`
    saymak "bu coini dinlemiyoruz" diye kesin konuşmak olurdu — oysa tek
    bildiğimiz, bilmediğimiz.

    Hisse (`xyz:`) perp'lerinin hepsine abone olunuyor; soru yalnız kripto
    tarafında anlamlı.
    """
    if ":" in coin:
        return True
    from ..db import kv_get
    u = await kv_get("ws_universe") or {}
    coins = u.get("crypto")
    if not isinstance(coins, list):
        return None
    return coin in coins


async def window_brief(cfg, client, coin: str, t0: int, t1: int, *,
                       top: int = 3, probe: bool = True,
                       focus: str | None = None) -> dict:
    """Alarm mesajına gömülecek "ne oldu" özeti. Saf VERİ döner, metin değil.

    `report()`'un üstüne iki şey ekler:
      • CANLI SONDA — en büyük adreslerden yalnız pozisyonu EKSİK ya da BAYAT
        olanların defteri anında çekilir. Taze olanı yeniden çekmek bedava
        değil ve hiçbir şey kazandırmaz. Sondadan sonra çıkarım yeniden koşar,
        yani "long'unu artırdı" 2 saat eski veriye değil işlem sonrası gerçek
        pozisyona dayanır.
      • BOŞ KIRILIMIN SEBEBİ — "adres yok" tek başına yanıltıcı: dinlemediğimiz
        bir coin mi, eşik altı işlemler mi, yoksa bilmiyor muyuz? Üçü ayrı.

    Sonda hatası mesajı ASLA düşürmez; `probe_err` dolar, özet yine üretilir.
    """
    rep = await report(coin, t0, t1)
    rows = list(rep["rows"])
    if focus:
        # Whale alarmının konusu olan adres, büyüklüğüne bakılmaksızın başta:
        # mesaj onun hakkında, listenin en büyüğü hakkında değil.
        rows.sort(key=lambda r: (r["address"] != focus, -r["gross"]))
    top = max(0, int(top))
    head = rows[:top]

    probed, probe_err = 0, ""
    if probe and client and head:
        cap = int(getattr(cfg, "alert_forensics_probe", 3) or 0)
        # Yalnız GERÇEKTEN eksik olanı sonda: pozisyonu taze bilinen ya da
        # "baktık, taşımıyor" diye taze bilinen adresi yeniden çekmek istek
        # harcar, hiçbir şey kazandırmaz.
        cut = now() - PROFILE_STALE_SEC
        stale = [r["address"] for r in head
                 if (r.get("infer") or {}).get("stale")
                 or (not r.get("pos") and int(r.get("probed_ts") or 0) < cut)]
        if cap > 0 and stale:
            try:
                res = await refresh_profiles(cfg, client, coin, stale, cap=cap)
                probed, probe_err = res["ok"], res["err_msg"]
                if res["ok"]:
                    ha = [r["address"] for r in head]
                    fresh = await _positions(coin, ha)
                    fprb = await probed(ha)
                    for r in head:
                        r["pos"] = fresh.get(r["address"])
                        r["probed_ts"] = fprb.get(r["address"], 0)
                        r["infer"] = infer(r, r["pos"], t0, t1, r["probed_ts"])
            except Exception as e:
                probe_err = f"{type(e).__name__}: {e}"
                log.warning("alarm sondası düştü (%s): %s", coin, e)

    listen = await listening(coin)
    empty = ""
    if not rows:
        empty = ("below_floor" if listen else
                 ("unknown" if listen is None else "not_listening"))
    floor = (getattr(cfg, "min_fill_notional", 0) if ":" in coin
             else getattr(cfg, "crypto_fill_min_notional", 0))
    tk = sum(r["tk"] for r in rows), sum(r["known"] for r in rows)
    return {
        "coin": coin, "symbol": rep["symbol"], "t0": t0, "t1": t1,
        "minutes": rep["minutes"],
        "verdict": rep["oi"]["verdict"], "why": rep["oi"]["why"],
        "oi_chg": rep["oi_chg"], "px_chg": rep["px_chg"],
        "widened": rep["widened"], "span": rep["span"],
        "n_addr": len(rows), "buy": rep["buy"], "sell": rep["sell"],
        "net": rep["buy"] - rep["sell"],
        "taker_pct": (tk[0] / tk[1] * 100) if tk[1] > 0 else None,
        "top": head, "listening": listen, "floor": floor,
        "probed": probed, "probe_err": probe_err, "empty_reason": empty,
        "before_records": rep["before_records"],
    }


async def alert_brief(cfg, client, coin: str, t0: int, t1: int,
                      **kw) -> dict | None:
    """Alarm yollarının TEK giriş noktası: ayar kapalıysa None, hata da None.

    Zenginleştirme bir SÜS; alarmın kendisi ondan daha önemli. Bir SQL hatası
    ya da düşen bir sonda yüzünden "PUMP'ta $3.2M döndü" mesajının hiç
    gitmemesi kabul edilemez — o yüzden burada geniş bir except var ve
    çağıranlar `None` gördüklerinde sessizce sade mesajı yollar.
    """
    if not getattr(cfg, "alert_forensics", True):
        return None
    try:
        kw.setdefault("top", int(getattr(cfg, "alert_forensics_top", 3)))
        return await window_brief(cfg, client, coin, t0, t1, **kw)
    except Exception as e:
        log.warning("alarm özeti çıkarılamadı (%s): %s", coin, e)
        return None
