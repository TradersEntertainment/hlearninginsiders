"""👤 İzlenen hesaplar — adı konmuş birkaç hesabın pozisyonları ve emirleri.

Kullanıcı isteği (02.10): drkmttr vault'u için "pozları için mesaj gelsin, yine
benzer şeyi (yapışkan duvar) yapıyorsa"; kararlar: dört olayın hepsi (yeni duvar /
kampanya, pozisyon aç-kapa-ters çevir, büyüdü-küçüldü, kâr al-stop merdiveni),
'adres:isim' listesi (başlangıçta drkmttr) ve AYRI GRUP — kripto kanalı spam olmasın
(`ACCOUNT_CHAT_ID`, env; boşsa hiçbir yere gitmez, yoklama da yapılmaz).

NEDEN AYRI HAT (mevcut takipler yetmiyor):
  • `/takip_N` (tracker) TEK coindeki TEK pozisyonu izler. Burada izlenen HESABIN
    TAMAMIDIR: yeni coinde açılan pozisyon da, defterdeki emirler de.
  • HL arayüzü vault sayfasında açık emirleri GÖSTERMİYOR (yalnız işlemler), ama
    `frontendOpenOrders` veriyor: post-only duvar, reduce-only kapatma emirleri, stop'lar.

Ölçüm (hesap başına her `acct_poll_sec`): `clearinghouseState` (ağırlık 2) +
`frontendOpenOrders` (20); liste 100 emirde kesilmiş olabilirse sınırsız `openOrders`
(20) ile canlı oid kümesi doğrulanır. Tur başına bir `allMids` (2) — taze fiyat
(150 sn'lik önbellek, duvarı izlerken "uzaklaştı/kalktı" yanılgısı üretiyordu).
İzlenen duvar okumada yoksa en çok RECHECK_N hızlı yeniden bakış (frontendOpenOrders).
"Takip başladı" özetinde bir kez son 1 saatin dolumları (`userFillsByTime`, ≤3 sayfa).
Durum kv'de (`acctwatch:<adres>`, sürümlü — STATE_V).

Olay kuralları (hepsi ÖLÇÜLEN; tahmin yok):
  • pozisyon: açtı / kapattı / ters çevirdi; son BİLDİRİLEN boyuta göre ≥ step_pct
    VE ≥ step_min_usd adım (adet üzerinden — fiyat oynaması tetiklemez). Taban altı
    (toz) pozisyon da durumda tutulur ama sessizdir: fiyatla tabanı geçmesi "açtı" değildir.
  • duvar: tetiksiz, fiyata %1 içinde tek emir ≥ `acct_wall_min_usd` (reduce-only ise
    yalnız post-only: pozisyonu aynı yapışkan yolla kapatan duvar), iki yoklama üst
    üste. Kimlik önce cloid (yeniden koymada oid değişir, cloid değişmez). Devam için
    histerezis: %3 bant ve tabanın yarısı; bilinen emri defterde duruyorsa "fiyat
    uzaklaştı" kalkış sayılmaz. Kalkış: okumada yoksa hızlı yeniden bakışlar (02.10:
    duvar okumaların %38'inde yeniden koyma boşluğundaydı), 2 doğrulanmış kaçırma VE
    150 sn. Bitince süre ve o sürede pozisyon (işaretli). 15 dk içinde dönerse "geri geldi".
  • emir grupları (adlar niyet varsaymaz): kapatma emirleri (Gtc reduce-only limit),
    post-only kapatma kotasyonu (olay ÜRETMEZ — sürekli yeniden konur), tetikli kâr al
    (HL'nin adı) / stop, tetikli giriş — her biri ayrı grup. Pozisyona bağlı TP/SL
    (`isPositionTpsl`, sz 0) "tüm pozisyon". Kuruldu / ÖNEMLİ değişiklik (yeni emirler
    iki yoklama sabit VE sayı, aralık ya da $ belirgin değişti) / tamamen kalktı.
    Basamak dolumu ve aynı yere yeniden koyma mesaj ÜRETMEZ — dolum, pozisyonun
    küçülmesi olarak görünür.
Bir yoklamanın olayları TEK mesajda. Gönderilemezse durum yazılmaz: sonraki yoklama aynı
farkı yeniden bulur. Bildirim kapalıysa ya da kanal yoksa yoklama da yapılmaz; uzun
aradan / kanal değişiminden sonra eski fark "şimdi oldu" diye yazılmaz, takip yeniden başlar.
"""
from __future__ import annotations

import asyncio
import logging
import re

from ..db import kv_get, kv_set, now

log = logging.getLogger("radar.acctwatch")

STATE_KV = "acctwatch:"          # + adres
STATS_KV = "acctwatch_stats"
ADDRS_KV = "acctwatch_addrs"     # son turdaki liste (çıkarılan hesabın durumu silinsin)
NEAR_PCT = 1.0                   # duvar girişi: fiyata bu kadar yakın
WIDE_PCT = 3.0                   # izlenen duvarın devamı (histerezis)
KEEP_SHARE = 0.5                 # devam için en az taban × bu
WALL_GONE_POLLS = 2              # duvar bu kadar yoklama üst üste yoksa bitti …
WALL_GONE_SEC = 150              # … VE son görüşten bu kadar sn geçtiyse
RECHECK_N = 4                    # izlenen duvar yoklamada yoksa: bu kadar hızlı yeniden bakış …
RECHECK_GAP = 2.0                # … bu aralıkla. 02.10 ölçümleri: drkmttr duvarı okumaların %38'inde,
                                 # akşam 15'te 10'unda YOK — sürekli iptal edip yeniden koyuyor; 15
                                 # bakışın hepsi ≤3 yeniden bakışta bulundu. Tek okumayla "kalktı"
                                 # demek sahte bildirim üretiyordu.
WALL_BACK_SEC = 900              # kalkan duvar bu süre içinde dönerse "geri geldi"
LADDER_MIN_N = 2                 # merdiven: en az bu kadar emir …
LADDER_MIN_USD = 100_000         # … ya da bu kadar $ (tek büyük TP/stop da sayılır)
CHANGE_N = 3                     # merdiven değişikliği önemli mi: emir sayısı farkı
CHANGE_RANGE_PCT = 1.0           # … ya da alt/üst fiyat bu kadar kaydı
CHANGE_NTL_PCT = 25.0            # … ya da $ bu kadar (ve CHANGE_NTL_USD) değişti
CHANGE_NTL_USD = 100_000
OIDS_KEEP = 400                  # duvar başına hatırlanan oid (yeniden koyma sayacı)
FRONT_CAP = 100                  # frontendOpenOrders bu kadar dönerse openOrders ile doğrula
STALE_MIN_SEC = 600              # durum bundan (ve 10 yoklamadan) eskiyse takip yeniden başlar
STATE_V = 2                      # durum sürümü: değişirse takip "yeniden başladı" özetiyle sıfırlanır
FLOW_WINDOW = 3600               # "son 1 saat gerçekleşen dolumlar" penceresi
FLOW_PAGES = 3                   # userFillsByTime en çok bu kadar sayfa (2000'er dolum)
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Tür adları NİYET varsaymaz (02.10: "kâr al" denen alışlar, piyasa yapıcının fiyatın
# %0.2 altındaki post-only geri alım kotasyonuydu). 'tptrig' HL'nin kendi orderType adı.
KIND_LABEL = {"close": "kapatma emirleri", "quote": "post-only kapatma kotasyonu",
              "tptrig": "kâr al (tetik)", "stop": "stop", "entry": "tetikli giriş"}
SILENT_KINDS = ("quote",)        # yeniden koyma/dolum doğası gereği sürekli: olay üretmez


def parse_watch_list(raw: str) -> list[tuple[str, str]]:
    """'0xADRES:isim, 0xADRES2:isim2' → [(adres, isim)]. Geçersiz giriş atlanır."""
    out: list[tuple[str, str]] = []
    seen = set()
    for part in re.split(r"[,\s;]+", raw or ""):
        if not part:
            continue
        addr, _, name = part.partition(":")
        addr = addr.strip().lower()
        if not ADDR_RE.match(addr) or addr in seen:
            continue
        seen.add(addr)
        out.append((addr, (name.strip() or addr[:8])[:30]))
    return out


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def positions_of(state: dict | None) -> dict[str, dict]:
    """clearinghouseState → {coin: {szi, side, ntl, entry, liq, lev, mark}} (sıfırlar atlanır).
    `mark` = positionValue/|szi|: emirlerle AYNI anın işaret fiyatı."""
    out = {}
    for p in (state or {}).get("assetPositions") or []:
        pp = (p or {}).get("position") or {}
        try:
            szi = float(pp.get("szi") or 0)
            if not szi or not pp.get("coin"):
                continue
            lev = pp.get("leverage") or {}
            ntl = abs(float(pp.get("positionValue") or 0))
            out[pp["coin"]] = {
                "szi": szi, "side": "long" if szi > 0 else "short", "ntl": ntl,
                "entry": float(pp.get("entryPx") or 0) or None,
                "liq": float(pp["liquidationPx"]) if pp.get("liquidationPx") else None,
                "lev": lev.get("value") if isinstance(lev, dict) else None,
                "mark": ntl / abs(szi) if ntl else None}
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def classify_orders(orders, marks: dict, pos: dict | None = None,
                    wall_min_usd: float = 0.0) -> tuple[dict, dict, dict, dict]:
    """(dar duvar adayları, geniş duvar adayları, emir grupları, cloid → duvar adayı).

    cloid: yeniden koymada oid değişir ama CLOID değişmez (02.10 canlı ölçüm) — izlenen
    duvarın kimliği budur; fiyatı/boyutu değişse de aynı cloid aynı duvardır.

    duvar: tetiksiz limit; (coin|side) → fiyata NEAR_PCT (dar) / WIDE_PCT (geniş)
    içindeki EN BÜYÜK emir. Reduce-only olmayan her limit aday olabilir; reduce-only
    olan YALNIZ post-only (Alo) ve tabanı geçiyorsa — pozisyonu aynı yapışkan yolla
    kapatan duvar. Gtc reduce-only emir duvar sayılmaz (kapatma merdiveninin
    basamağıdır; fiyat değince sınıf değiştirip sahte "kalktı/değişti" üretmesin).
    grup: (coin|side|tür) → 'close' (Gtc reduce-only limit: bekleyen kapatma emirleri),
    'quote' (tabanın altındaki post-only reduce-only limit: piyasa yapıcının kapatma
    kotasyonu — olay üretmez), 'tptrig' (tetikli kâr al), 'stop' (reduce-only /
    pozisyona bağlı stop), 'entry' (reduce-only olmayan tetik). Pozisyona bağlı TP/SL
    (sz 0) pozisyonun tamamı sayılır ('whole'). Tetik emrinin $'ı tetik fiyatıyla
    (piyasa tetiğinde limitPx ~%10 kaydırılmış koruma fiyatıdır)."""
    pos = pos or {}
    near: dict[str, dict] = {}
    wide: dict[str, dict] = {}
    groups: dict[str, dict] = {}
    by_cloid: dict[str, dict] = {}
    for o in orders or []:
        if not isinstance(o, dict):
            continue
        coin = o.get("coin") or ""
        side = "ask" if o.get("side") == "A" else "bid"
        px, sz = _f(o.get("limitPx")), _f(o.get("sz"))
        trig, ro = bool(o.get("isTrigger")), bool(o.get("reduceOnly"))
        if not coin:
            continue
        whole = False
        if trig:
            if sz <= 0:
                if not (o.get("isPositionTpsl") and coin in pos):
                    continue
                sz, whole = abs(pos[coin]["szi"]), True
            ot = (o.get("orderType") or "").lower()
            kind = "tptrig" if "take profit" in ot else ("stop" if (ro or whole) else "entry")
            ref = _f(o.get("triggerPx")) or px
        else:
            if sz <= 0 or px <= 0:
                continue
            alo = (o.get("tif") or "").lower() == "alo"
            if not ro or alo:
                mark = _f(marks.get(coin))
                ntl = px * sz
                d = abs(px - mark) / mark * 100 if mark > 0 else None
                if d is not None and d <= WIDE_PCT:
                    cand = {"coin": coin, "side": side, "px": px, "sz": sz, "ntl": ntl,
                            "oid": o.get("oid"), "cloid": o.get("cloid") or "",
                            "tif": o.get("tif") or "", "ro": ro, "dist": (px - mark) / mark * 100}
                    k = f"{coin}|{side}"
                    if k not in wide or ntl > wide[k]["ntl"]:
                        wide[k] = cand
                    if d <= NEAR_PCT and (not ro or ntl >= wall_min_usd) \
                            and (k not in near or ntl > near[k]["ntl"]):
                        near[k] = cand
                    if cand["cloid"] and (cand["cloid"] not in by_cloid
                                          or ntl > by_cloid[cand["cloid"]]["ntl"]):
                        by_cloid[cand["cloid"]] = cand
                if not ro:
                    continue                 # reduce-only olmayan limit asla merdiven değil
                if d is not None and d <= NEAR_PCT and ntl >= wall_min_usd:
                    continue                 # reduce-only post-only DUVAR (kapatma duvarı)
                kind, ref = "quote", px      # tabanın altındaki post-only kapatma kotasyonu
            else:
                kind, ref = "close", px
        if ref <= 0:
            continue
        k = f"{coin}|{side}|{kind}"
        G = groups.setdefault(k, {"coin": coin, "side": side, "kind": kind, "n": 0, "lo": None,
                                  "hi": None, "sz": 0.0, "ntl": 0.0, "oids": [], "whole": False,
                                  "ro": True})
        G["n"] += 1
        G["lo"] = ref if G["lo"] is None else min(G["lo"], ref)
        G["hi"] = ref if G["hi"] is None else max(G["hi"], ref)
        G["sz"] += sz
        G["ntl"] += ref * sz
        G["whole"] = G["whole"] or whole
        G["ro"] = G["ro"] and (ro or whole)
        if o.get("oid") is not None:
            G["oids"].append(o.get("oid"))
    return near, wide, groups, by_cloid


def position_events(prev: dict, cur: dict, marks: dict, step_pct: float, step_min_usd: float,
                    min_pos_usd: float) -> tuple[list[dict], dict]:
    """Pozisyon olayları + yeni durum. Durum coin → {szi, base, ntl, side, quiet}; `base`
    son BİLDİRİLEN boyut. `quiet` = taban altı (toz): durumda tutulur, mesaj üretmez;
    fiyatla tabanı geçmesi "açtı" sayılmaz (adet değişmedi)."""
    ev: list[dict] = []
    nxt: dict = {}
    for coin in sorted(set(prev) | set(cur)):
        p, c = prev.get(coin), cur.get(coin)
        mark = _f((c or {}).get("mark")) or _f(marks.get(coin))
        if c and not p:
            quiet = c["ntl"] < min_pos_usd
            if not quiet:
                ev.append({"t": "open", "coin": coin, "cur": c})
            nxt[coin] = {"szi": c["szi"], "base": c["szi"], "ntl": c["ntl"], "side": c["side"],
                         "quiet": quiet}
            continue
        if p and not c:
            if not p.get("quiet"):
                ev.append({"t": "close", "coin": coin, "prev": p})
            continue
        base = _f(p.get("base") or p.get("szi"))
        loud = (not p.get("quiet")) or c["ntl"] >= min_pos_usd
        if (base > 0) != (c["szi"] > 0):
            if loud:
                ev.append({"t": "flip", "coin": coin, "prev": p, "cur": c})
            nxt[coin] = {"szi": c["szi"], "base": c["szi"], "ntl": c["ntl"], "side": c["side"],
                         "quiet": not loud}
            continue
        d = abs(c["szi"]) - abs(base)
        pct = d / abs(base) * 100 if base else 0.0
        quiet = bool(p.get("quiet"))
        if base and abs(pct) >= step_pct and abs(d) * mark >= step_min_usd and loud:
            ev.append({"t": "grow" if d > 0 else "shrink", "coin": coin, "prev": p, "cur": c,
                       "base": base, "pct": pct, "dusd": d * mark})
            base, quiet = c["szi"], False
        elif quiet and c["ntl"] >= min_pos_usd:
            quiet = False                   # yalnız fiyatla tabanı geçti: sessizce izlemeye al
        nxt[coin] = {"szi": c["szi"], "base": base, "ntl": c["ntl"], "side": c["side"], "quiet": quiet}
    return ev, nxt


def wall_cand(k: str, s: dict | None, near: dict, wide: dict, by_cloid: dict,
              wall_min_usd: float) -> dict | None:
    """Bu okumada (coin|side) duvarı. Önce izlenen duvarın CLOID'i (yeniden koymada oid
    değişir, cloid değişmez; eriyen kuyruk da aynı duvardır), sonra boyut kuralları:
    izlenen duvar dar bantta ya da geniş bantta tabanın yarısıyla, aday dar bantta
    tabanla."""
    coin, side = k.split("|")[:2]
    cl = (s or {}).get("cloid")
    if cl and cl in by_cloid:
        c = by_cloid[cl]
        if c["coin"] == coin and c["side"] == side and (
                (s or {}).get("alerted") or c["ntl"] >= wall_min_usd):
            return c
    if s and s.get("alerted"):
        c = near.get(k)
        if c is None or c["ntl"] < KEEP_SHARE * wall_min_usd:
            w2 = wide.get(k)
            c = w2 if (w2 and w2["ntl"] >= KEEP_SHARE * wall_min_usd) else None
        return c
    c = near.get(k)
    return c if (c is not None and c["ntl"] >= wall_min_usd) else None


def wall_events(prev: dict, near: dict, wide: dict, by_cloid: dict, pos: dict, marks: dict,
                wall_min_usd: float, ts: int, alive: set | None, ended: dict,
                unsure: set | None = None) -> tuple[list[dict], dict, dict]:
    """Duvar olayları + yeni durum + yakın zamanda bitenler.

    Aday: dar bantta ≥ taban, iki yoklama üst üste → 'kurdu' (15 dk içinde bitmiş
    aynı coin+yön ise 'geri geldi'). İzlenen duvar: aynı cloid, dar bant, ya da GENİŞ
    bantta tabanın yarısını geçen emir devam sayılır (fiyat oynaması kalkış değildir);
    bilinen emri hâlâ açıksa, o coinin fiyatı bu tur yoksa ya da yeniden bakış okunamadıysa
    (`unsure`) kaçırma SAYILMAZ. Kalkış: WALL_GONE_POLLS doğrulanmış kaçırma VE son
    görüşten WALL_GONE_SEC (poll_account her kaçırmayı hızlı yeniden bakışlarla doğrular).
    Görülen farklı oid = kaç kez yeniden konduğu (en az)."""
    ev: list[dict] = []
    nxt: dict = {}
    ended = {k: t for k, t in (ended or {}).items() if ts - int(t) < WALL_BACK_SEC}
    for k in sorted(set(prev) | set(near)):
        p = prev.get(k)
        coin = k.split("|")[0]
        c = wall_cand(k, p, near, wide, by_cloid, wall_min_usd)
        if c is None and p is None:
            continue
        if c is not None:
            s = dict(p) if p else {"first_ts": ts, "seen": 0, "oids": [], "alerted": False,
                                   "px_min": c["px"], "px_max": c["px"], "ntl_max": 0.0,
                                   "start_szi": _f((pos.get(coin) or {}).get("szi"))}
            oids = list(s.get("oids") or [])
            if c.get("oid") is not None and c["oid"] not in oids:
                oids.append(c["oid"])
            s.update(seen=int(s.get("seen") or 0) + 1, miss=0, oids=oids[-OIDS_KEEP:],
                     cloid=c.get("cloid") or s.get("cloid") or "",
                     last_ts=ts, coin=c["coin"], side=c["side"], px=c["px"], sz=c["sz"],
                     ntl=c["ntl"], tif=c["tif"], ro=bool(c.get("ro")), dist=c["dist"],
                     px_min=min(_f(s.get("px_min"), c["px"]), c["px"]),
                     px_max=max(_f(s.get("px_max"), c["px"]), c["px"]),
                     ntl_max=max(_f(s.get("ntl_max")), c["ntl"]))
            if not s["alerted"] and s["seen"] >= 2:
                back = k in ended
                ev.append({"t": "wall_back" if back else "wall", "w": dict(s)})
                s["alerted"] = True
                ended.pop(k, None)
            nxt[k] = s
            continue
        s = dict(p)
        last_oid = (s.get("oids") or [None])[-1]
        if (_f(marks.get(coin)) <= 0 or alive is None or k in (unsure or ())
                or (last_oid is not None and last_oid in alive)):
            nxt[k] = s                       # fiyat yok / liste ya da yeniden bakış belirsiz /
            continue                         # emri hâlâ açık: kaçırma değil
        s["miss"] = int(s.get("miss") or 0) + 1
        if s["miss"] < WALL_GONE_POLLS or ts - int(s.get("last_ts") or ts) < WALL_GONE_SEC:
            nxt[k] = s
            continue
        if s.get("alerted"):
            ev.append({"t": "wall_end", "w": s, "end_szi": _f((pos.get(coin) or {}).get("szi"))})
            ended[k] = ts
    return ev, nxt, ended


def _view(L: dict) -> dict:
    return {"n": L["n"], "lo": L["lo"], "hi": L["hi"], "sz": L["sz"], "ntl": L["ntl"]}


def material(was: dict | None, cur: dict) -> bool:
    """Merdiven değişikliği okuyana bir şey söyler mi? Aynı yere yeniden koyma, tek
    basamak eklenip çıkması ya da küçük $ farkı söylemez."""
    if not was:
        return True
    if abs(int(cur["n"]) - int(was.get("n") or 0)) >= CHANGE_N:
        return True
    for a, b in ((cur["lo"], was.get("lo")), (cur["hi"], was.get("hi"))):
        if b and abs(_f(a) - _f(b)) / _f(b) * 100 > CHANGE_RANGE_PCT:
            return True
    d = abs(_f(cur["ntl"]) - _f(was.get("ntl")))
    return d >= CHANGE_NTL_USD and d / max(_f(was.get("ntl")), 1.0) * 100 >= CHANGE_NTL_PCT


def ladder_events(prev: dict, cur: dict, alive: set | None) -> tuple[list[dict], dict]:
    """Emir grubu olayları + yeni durum.

    • kuruldu: iki yoklama üst üste (taban: ≥2 emir, ya da ≥$100K, ya da pozisyon TP/SL)
    • değişti: BİLİNMEYEN oid'ler iki yoklama sabit kalınca VE görünüm ÖNEMLİ değiştiyse
      (önceki = değişiklikten hemen önceki CANLI hal; arada dolan basamaklar sahibin
      değişikliği gibi gösterilmez). Önemsizse sessizce benimsenir.
    • kalktı: iki yoklama hiç yok — bilinen emirlerinden biri hâlâ açıksa (kesik liste) değil
    Basamak dolumu (oid'in kaybolması) mesaj ÜRETMEZ; pozisyon küçülmesi olarak görünür.
    'quote' (post-only kapatma kotasyonu) hiç olay üretmez: fiyatla birlikte sürekli
    yeniden konur — durumda tutulur, yalnız özet ve /hesaplar'da görünür."""
    ev: list[dict] = []
    nxt: dict = {}
    for k in sorted(set(prev) | set(cur)):
        p, c = prev.get(k), cur.get(k)
        if c is None:
            if p is None:
                continue
            s = dict(p)
            if alive is None or any(o in alive for o in (p.get("oids") or [])):
                nxt[k] = s                   # liste belirsiz / emirleri açık ama görünmüyor
                continue
            s["miss"] = int(s.get("miss") or 0) + 1
            if s["miss"] >= 2:
                if s.get("alerted"):
                    ev.append({"t": "ladder_gone", "L": s})
                continue                     # durumdan düşer
            nxt[k] = s
            continue
        cur_oids = set(c["oids"])
        s = {k2: v for k2, v in c.items() if k2 != "oids"}
        s["miss"] = 0
        if p is None:
            if not (c["n"] >= LADDER_MIN_N or c["ntl"] >= LADDER_MIN_USD or c.get("whole")):
                continue                     # tek küçük emir: grup değil
            s.update(oids=sorted(cur_oids), alerted=False, pending=[], view=None)
            nxt[k] = s
            continue
        if not p.get("alerted"):
            s.update(oids=sorted(cur_oids), alerted=True, pending=[], view=_view(c))
            ev.append({"t": "ladder_new", "L": dict(s)})
            nxt[k] = s
            continue
        known = set(p.get("oids") or [])
        if alive is not None:
            known &= alive                   # yalnız gerçekten kapananlar unutulur
        new = cur_oids - known
        s.update(alerted=True)
        if new and sorted(new) == sorted(p.get("pending") or []):
            if material(p.get("view"), _view(c)):
                ev.append({"t": "ladder_change", "L": dict(s), "was": p.get("view") or {}})
            s.update(oids=sorted(cur_oids | known), pending=[], view=_view(c))
        elif new:
            s.update(oids=sorted(known), pending=sorted(new), view=p.get("view"))
        else:
            s.update(oids=sorted(cur_oids | known), pending=[], view=_view(c))
        nxt[k] = s
    ev = [e for e in ev if (e.get("L") or {}).get("kind") not in SILENT_KINDS]
    return ev, nxt


async def poll_account(cfg, client, addr: str, name: str, marks: dict, ts: int,
                       chat: str = "") -> dict:
    """Bir hesabın bir yoklaması: {events, state, first, gap}."""
    st = await client.clearinghouse(addr)
    if not isinstance(st, dict) or "assetPositions" not in st:
        raise RuntimeError("clearinghouseState beklenmeyen yanıt")
    orders = await client.frontend_open_orders(addr)
    if not isinstance(orders, list):
        raise RuntimeError("frontendOpenOrders beklenmeyen yanıt")
    alive: set | None = {o.get("oid") for o in orders if isinstance(o, dict)}
    truncated = False
    if len(orders) >= FRONT_CAP:
        try:
            full = await client.open_orders(addr)
            alive = {o.get("oid") for o in full if isinstance(o, dict)}
            truncated = len(full) > len(orders)
        except Exception:
            alive = None                     # belirsiz: hiçbir şey "kalktı" sayılmaz
    pos = positions_of(st)
    m = dict(marks)
    m.update({c: p["mark"] for c, p in pos.items() if p.get("mark")})
    wall_min = float(getattr(cfg, "acct_wall_min_usd", 250_000) or 0)
    near, wide, groups, by_cloid = classify_orders(orders, m, pos, wall_min)
    prev = await kv_get(STATE_KV + addr) or {}
    poll = int(getattr(cfg, "acct_poll_sec", 60) or 60)
    gap = ts - int(prev.get("ts") or 0) if prev else None
    reason = None
    if prev:
        if int(prev.get("v") or 1) != STATE_V:
            reason = "version"               # tür adları/anahtarları değişti: eski durumla kıyaslanmaz
        elif (prev.get("chat") or "") != chat:
            reason = "chat"                  # yeni grup: özetle başlasın
        elif gap > max(STALE_MIN_SEC, 10 * poll):
            reason = "stale"
    restart = reason is not None
    if restart:
        prev = {}
    live = None if truncated else alive
    rechecks, unsure = await _recheck_walls(client, addr, prev.get("walls") or {}, near, wide,
                                            by_cloid, m, pos, wall_min, live)
    min_pos = float(getattr(cfg, "acct_min_pos_usd", 100_000) or 0)
    pev, pstate = position_events(prev.get("pos") or {}, pos, m,
                                  float(getattr(cfg, "acct_step_pct", 25) or 25),
                                  float(getattr(cfg, "acct_step_min_usd", 500_000) or 0), min_pos)
    # Kesik listede HANGİ emirlerin görüneceği belirsiz (en yeni 100 değil): görünür pencere
    # kaydıkça hiç görülmemiş emirler "yeni", gizlenenler "kalktı" gibi okunurdu. Bu turda
    # merdiven olayları ve duvar kalkışı askıda; durum korunur, altbilgi bunu söyler.
    wev, wstate, ended = wall_events(prev.get("walls") or {}, near, wide, by_cloid, pos, m,
                                     wall_min, ts, live, prev.get("wall_ended") or {}, unsure)
    if truncated and prev:
        lev, lstate = [], dict(prev.get("ladders") or {})
    else:
        lev, lstate = ladder_events(prev.get("ladders") or {}, groups, alive)
    ms = st.get("marginSummary") or {}
    snap = {"v": STATE_V, "ts": ts, "name": name, "chat": chat, "pos": pstate, "walls": wstate,
            "wall_ended": ended, "ladders": lstate, "orders_n": len(orders),
            "truncated": truncated, "acct": _f(ms.get("accountValue")),
            "ntl": _f(ms.get("totalNtlPos")), "live": pos}
    if not prev:
        # İlk tur (ya da uzun ara / kanal değişimi): mevcut her şey TABAN — özet atılır,
        # sonra "yeni" diye bildirilmez. Gruplar ilk turda benimsenir (iki yoklama beklemez).
        for L in lstate.values():
            L.update(alerted=True, view=_view(L))
        for w in wstate.values():
            w["alerted"] = True
        return {"events": [], "state": snap, "first": True, "gap": gap if restart else None,
                "reason": reason, "rechecks": rechecks}
    return {"events": pev + wev + lev, "state": snap, "first": False, "gap": None, "reason": None,
            "rechecks": rechecks}


async def _recheck_walls(client, addr: str, prev_walls: dict, near: dict, wide: dict,
                         by_cloid: dict, marks: dict, pos: dict, wall_min: float,
                         alive: set | None) -> tuple[int, set]:
    """İzlenen bir duvar bu okumada yoksa RECHECK_GAP arayla RECHECK_N kez yeniden bak.
    Bulunan aday sözlüklere işlenir (aynı cloid / boyut kuralı); hiçbir bakışta
    bulunamayan DOĞRULANMIŞ kaçırmadır. Yeniden bakış okunamazsa kalanlar belirsizdir
    (kaçırma sayılmaz). Yalnız kaçırma sayılacak duvarlara bakılır (fiyatı var, bilinen
    emri kapalı); liste belirsizse (alive None — kesik liste) hiç bakılmaz, orada kalkış
    zaten askıda. Dönüş: (ek istek sayısı, belirsiz anahtarlar)."""
    if alive is None:
        return 0, set()
    missing = []
    for k, s in prev_walls.items():
        last = (s.get("oids") or [None])[-1]
        if _f(marks.get(k.split("|")[0])) <= 0 or (last is not None and last in alive):
            continue
        if wall_cand(k, s, near, wide, by_cloid, wall_min) is None:
            missing.append(k)
    n = 0
    for _ in range(RECHECK_N if missing else 0):
        await asyncio.sleep(RECHECK_GAP)
        try:
            o2 = await client.frontend_open_orders(addr)
        except Exception:
            log.debug("duvar yeniden bakışı okunamadı %s", addr[:10], exc_info=True)
            return n, set(missing)
        n += 1
        if not isinstance(o2, list):
            return n, set(missing)
        n2, w2, _g, c2 = classify_orders(o2, marks, pos, wall_min)
        still = []
        for k in missing:
            c = wall_cand(k, prev_walls[k], n2, w2, c2, wall_min)
            if c is None:
                still.append(k)
                continue
            near[k] = c
            wide[k] = c
            if c.get("cloid"):
                by_cloid[c["cloid"]] = c
            if c.get("oid") is not None:
                alive.add(c["oid"])
        missing = still
        if not missing:
            break
    return n, set()


def _flow_add(agg: dict, f: dict) -> None:
    try:
        coin, d = f.get("coin") or "", f.get("dir") or "?"
        usd = float(f.get("px") or 0) * float(f.get("sz") or 0)
    except (TypeError, ValueError, AttributeError):
        return
    if not coin:
        return
    a = agg.setdefault(coin, {}).setdefault(d, {"usd": 0.0, "n": 0, "maker_usd": 0.0})
    a["usd"] += usd
    a["n"] += 1
    if not f.get("crossed"):
        a["maker_usd"] += usd


async def fill_flow(client, addr: str, since: int, until: int | None = None,
                    max_pages: int = FLOW_PAGES) -> dict:
    """Hesabın [since, until] aralığında GERÇEKLEŞEN dolumları, coin → yön → {$, dolum,
    maker $}. "Geri alıyor mu?" sorusunu ölçerek cevaplar (emir listesi bekleyeni
    söyler, bu olanı). Sayfalama HL'nin önerdiği gibi: sonraki sayfa son dolumun
    zamanından (dahil) başlar, aynı milisaniyedeki tekrarlar `tid` ile atılır — tek
    taker'ın süpürdüğü birden çok dolum sayfa sınırında kaybolmasın. Sayfa sınırına
    takılırsa `complete=False` — mesaj eksik der."""
    end = int(until or now())
    start = int(since) * 1000
    agg: dict = {}
    seen: set = set()
    n, complete = 0, True
    for _ in range(max_pages):
        fills = await client.user_fills_by_time(addr, start, end * 1000)
        if not isinstance(fills, list) or not fills:
            break
        for f in fills:
            if not isinstance(f, dict):
                continue
            key = f.get("tid") or (f.get("hash"), f.get("oid"), f.get("time"), f.get("px"),
                                   f.get("sz"), f.get("side"))
            if key in seen:
                continue
            seen.add(key)
            _flow_add(agg, f)
            n += 1
        if len(fills) < 2000:
            break
        last = int(fills[-1].get("time") or 0)
        if last <= start:                    # tek milisaniyede 2000+ dolum: ilerlenemez
            complete = False
            break
        start = last
    else:
        complete = False
    return {"coins": agg, "n": n, "complete": complete, "since": int(since), "until": end}


_FLOW_CACHE: dict[str, tuple[int, dict]] = {}


async def fill_flow_cached(client, addr: str, ttl: int = 60) -> dict | None:
    """/hesaplar için: son 1 saatin akışı, 60 sn önbellekli (komut tekrarında HL'ye yüklenmesin)."""
    ts = now()
    hit = _FLOW_CACHE.get(addr)
    if hit and ts - hit[0] < ttl:
        return hit[1]
    try:
        flow = await fill_flow(client, addr, ts - FLOW_WINDOW, ts)
    except Exception:
        log.debug("dolum akışı alınamadı %s", addr[:10], exc_info=True)
        return None
    _FLOW_CACHE[addr] = (ts, flow)
    return flow


async def _marks(client) -> dict:
    """Taze fiyatlar: allMids (ağırlık 2); olmazsa ana dex özeti (önbellekli)."""
    try:
        mids = await client.all_mids()
        if isinstance(mids, dict) and mids:
            return {c: _f(v) for c, v in mids.items() if not str(c).startswith("@")}
    except Exception:
        log.debug("allMids alınamadı", exc_info=True)
    try:
        from ..hl.universe import main_dex_ctx
        return {c: _f(v.get("m")) for c, v in ((await main_dex_ctx(client)).get("c") or {}).items()}
    except Exception:
        return {}


async def run_once(cfg, client, notifier, ts: int | None = None) -> dict:
    """Bir tur: listedeki her hesap → olaylar → tek mesaj → durum."""
    from ..notify import kind_enabled
    from ..telegram import format as fmt
    ts = ts or now()
    out = {"accounts": 0, "polled": 0, "events": 0, "sent": 0, "failed": 0, "errors": 0,
           "no_chat": 0, "muted": 0, "first": 0, "restart": 0, "truncated": 0}
    accts = parse_watch_list(getattr(cfg, "acct_watch_list", "") or "")
    out["accounts"] = len(accts)
    # Listeden çıkarılan hesabın durumu silinir: yeniden eklenince "takip başladı" özeti gelsin.
    prev_addrs = await kv_get(ADDRS_KV) or []
    keep = {a for a, _ in accts}
    for a in prev_addrs:
        if a not in keep:
            await kv_set(STATE_KV + a, None)
    if sorted(prev_addrs) != sorted(keep):
        await kv_set(ADDRS_KV, sorted(keep))
    chat = (getattr(cfg, "account_chat_id", "") or "").strip()
    if not accts:
        return out
    if not chat:
        out["no_chat"] = len(accts)          # kanal yok → yoklama da yok (bütçe harcanmaz)
        return out
    if notifier is None or not kind_enabled(cfg, "acct"):
        out["muted"] = len(accts)            # bildirim kapalı → yoklama yok (durum donup bayatlamasın)
        return out
    marks = await _marks(client)
    for addr, name in accts:
        try:
            r = await poll_account(cfg, client, addr, name, marks, ts, chat)
        except Exception as e:
            out["errors"] += 1
            log.warning("izlenen hesap okunamadı %s (%s): %s", name, addr[:10], e)
            continue
        out["polled"] += 1
        st = r["state"]
        out["truncated"] += 1 if st["truncated"] else 0
        if r["first"]:
            reason = r.get("reason")
            if reason == "version":
                title = "takip yeniden başladı (emir etiketleri düzeltildi)"
            elif reason == "stale":
                title = (f"takip yeniden başladı (son yoklama {fmt.dur_txt(r['gap'])} önce —"
                         " aradaki değişiklikler zamanı bilinmediği için yazılmadı)")
            else:
                title = "takip başladı"
            try:
                flow = await fill_flow(client, addr, ts - FLOW_WINDOW, ts)
            except Exception:
                log.debug("dolum akışı alınamadı %s", addr[:10], exc_info=True)
                flow = None
            text = fmt.acct_started(addr, st, marks, title=title, flow=flow)
            ok = await notifier.send("acct", text, priority="high", key=f"acct:{addr}:start:{ts}",
                                     chat_id=chat, public=False)
            if ok:
                await kv_set(STATE_KV + addr, st)
                out["first"] += 1
                out["restart"] += 0 if r["gap"] is None else 1
                out["sent"] += 1
            else:
                out["failed"] += 1
            continue
        ev = r["events"]
        if not ev:
            await kv_set(STATE_KV + addr, st)
            continue
        text = fmt.acct_events(addr, st, ev, marks)
        ok = await notifier.send("acct", text, priority="high", key=f"acct:{addr}:{ts}",
                                 chat_id=chat, public=False)
        if ok:
            await kv_set(STATE_KV + addr, st)
            out["sent"] += 1
            out["events"] += len(ev)          # yalnız GİDEN olay sayılır (yeniden deneme değil)
        else:
            out["failed"] += 1                # durum YAZILMAZ: sonraki yoklama aynı farkı bulur
    return out


async def snapshots(cfg) -> list[dict]:
    """/hesaplar için: listedeki hesapların son anlık görüntüsü (kv; HL'ye istek yok)."""
    out = []
    for addr, name in parse_watch_list(getattr(cfg, "acct_watch_list", "") or ""):
        out.append({"address": addr, "name": name, "state": await kv_get(STATE_KV + addr) or {}})
    return out


async def loop(cfg, client, notifier) -> None:
    """Denetimli döngü. Site ASLA buna bağımlı değil."""
    from ..health import beat
    await asyncio.sleep(45)
    tot: dict[str, int] = {}
    while True:
        try:
            if getattr(cfg, "acct_watch_enabled", True):
                out = await run_once(cfg, client, notifier)
                for k in ("polled", "events", "sent", "failed", "errors", "first", "restart"):
                    tot[k] = tot.get(k, 0) + int(out.get(k) or 0)
                await kv_set(STATS_KV, {**out, "tot": tot, "ts": now()})
                if out["sent"] or out["errors"]:
                    log.info("izlenen hesaplar: %d olay, %d mesaj, %d hata", out["events"],
                             out["sent"], out["errors"])
            else:
                await kv_set(STATS_KV, {"ts": now(), "disabled": True})
            await beat("acctwatch")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("izlenen hesap turu")
        await asyncio.sleep(max(20, int(getattr(cfg, "acct_poll_sec", 60) or 60)))
