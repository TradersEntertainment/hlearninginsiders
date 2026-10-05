"""🔂 Dilimli alım-satım — TWAP emri olmadan dilim dilim alan / satan hesabın dizileri.

Kullanıcı isteği (05.10): "hisse tarafına adam TWAP emri vermeden TWAP atıyor, nasıl
yapıyor bilmiyorum; almaya başladığında ve bitirdiğinde bildirim istiyorum — sadece
CBRS" (0x30afce2f…393e). Kararlar: hesap takip grubu (`ACCOUNT_CHAT_ID`), alış + satış,
mesajda aynı dakikalardaki diğer hisse işlemleri tek satır (bildirim yalnız CBRS için).

Ölçüm (03–05.10, HL info API):
  • her dilim TEK Limit IOC emri (orderStatus: Limit/Ioc, cloid yok; dolumlarda twapId
    boş) — limit dolum ortalamasının ~%0.28 üstü: market gibi 3–4 seviye süpürür, tam
    dolar, %100 taker. HL TWAP'ı değil; istemci tarafı bot. Bir emrin bütün dolumları
    aynı milisaniyede (4,217 emirde istisna yok).
  • ritim: emirler arası 21.4–50.3 sn (medyan 32), dilim medyan ~$8K (p10 $3.5–5K).
    Diziler saatlerce sürer, aralarında ≥5.5 saat; aynı dakikalarda DRAM/INTC/MU'da aynı
    ritimle Close Long satış (rotasyon). 04.10 15–16 UTC CBRS hacminin %16.9'u.
  • mevcut TWAP radarı (twaplive) bu dilimleri görür ama HL'de TWAP emri olmadığı için
    `no_order` ile bırakır — bu modül o boşluğu hesap bazında kapatır.

Neden REST (userFillsByTime), WS değil: kesin emir sayısı (oid), yön (dir), taker
(crossed), twapId ve startPosition gerekir; kesintide kaçan dolumlar geçmişten yeniden
okunur. Tek istek hesabın TÜM coinlerini döndürür → durum hesap başına; diziler tam coin
+ yön anahtarıyla (`xyz:CBRS|B`), listedeki sembol yalnız süzgeç.

Kurallar (hepsi ÖLÇÜLEN; tahmin yok):
  • dizi: aynı coin + yönde emirler, aralar ≤ quiet (300 sn; ölçülen en uzun ara 50 sn)
  • başladı: ≥ start_orders (3) emir ve ≥ start_usd ($10K) — bir kez
  • bitti: son emirden > quiet geçti VE tur TAM okundu (okuma hatası / yetişme / gecikmeli
    düğüm → bitti DENMEZ: veri yok ≠ sessizlik); karşı yön dizisi yerleşir ve eski dizinin
    ondan sonra emri yoksa hemen ("ardından SATIŞ dizisi başladı"). Tek ters emir bitirmez.
  • aynı dakikalardaki diğer coinler: dizinin kuyruğuna yazılır, dizinin bir SONRAKİ kendi
    emriyle kesinleşir — son dilimden sonraki işlem diziye yazılmaz
  • süreklilik: imleç ms'indeki tid'ler (edge) sonraki okumada yeniden gelmeli (HL'de
    başlangıç dahil); gelmezse HL geçmişi kaymış (gap) → yeniden başla; boş gelirse
    gecikmeli düğüm → belirsiz (UNSURE_MAX tur sonra boşluk kabul edilir)
  • ilk tur / >2 sa kesinti / gap: son 12 saat okunur (tur başına ≤4 sayfa, birkaç tura
    yayılır, düşük öncelik), "izleme (yeniden) başladı" özeti; ≤2 sa kesintide geçmiş yeniden
    oynatılır. 12 saat: ölçülen en uzun dizi 12.5 sa — süren dizinin başı da ölçülsün
    ("kaç saattir alıyor?" sorusu)
Bütçe: hesap başına 30 sn'de bir userFillsByTime (ağırlık 20 + 20 dolumda 1; medyan 13
dolum/tur) ≈ 45/dk; başladı: +clearinghouseState +orderStatus; bitti: +clearinghouseState
+candleSnapshot. ACCOUNT_CHAT_ID boşsa ya da tür kapalıysa hiç istek yok.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import statistics

from ..db import kv_get, kv_set, now
from ..hl.universe import symbol_of
from . import acctwatch as acct

log = logging.getLogger("radar.slicewatch")

STATE_KV = "slicewatch:"         # + adres
STATS_KV = "slicewatch_stats"
ADDRS_KV = "slicewatch_addrs"    # son turdaki liste (çıkarılan adresin durumu silinsin)
STATE_V = 1
LOOKBACK_SEC = 12 * 3600         # ilk tur / yeniden başlatma: süren dizinin başını bulmak için geriye
LOOKBACK_PAGES = 4               # tur başına (≤480 ağırlık); 12 sa ≈ 17–22K dolum → 3 tur (~1.5 dk)
CATCHUP_PAGES = 4                # canlı turda en çok (8K dolum ≈ 4.4 sa > REPLAY_MAX)
REPLAY_MAX_SEC = 2 * 3600        # bundan uzun kesinti yeniden oynatılmaz (HL geçmişi kayan pencere)
LATE_SEC = 120                   # olay bundan geç fark edildiyse mesaj söyler (normal ≤ ~35 sn)
QUIET_MIN_SEC = 90               # ayar tabanı (ölçülen en uzun ara 50.3 sn)
UNSURE_MAX = 10                  # imleç dolumları gelmeyen (boş) ardışık tur sınırı (~5 dk)
SAMPLES_MAX = 3000               # dizi başına saklanan ara / dilim örneği
HIST_MAX = 6                     # son biten diziler
BACKOFF_MAX_SEC = 900            # gönderilemeyen mesaj: yeniden deneme aralığı tavanı
ADDR_RE = acct.ADDR_RE

_BACKOFF: dict[str, tuple[int, int]] = {}    # adres → (bu zamandan önce deneme, kaçıncı)


def parse_slice_list(raw: str) -> dict[str, dict]:
    """'0xADRES:SEMBOL[:isim], …' → {adres: {name, syms}}; aynı adresin sembolleri
    gruplanır, geçersiz giriş atlanır. Sembol dex'siz yazılır (CBRS → xyz:CBRS dolumu)."""
    out: dict[str, dict] = {}
    for part in re.split(r"[,\s;]+", raw or ""):
        bits = [b.strip() for b in part.split(":")]
        if len(bits) < 2:
            continue
        addr, sym = bits[0].lower(), bits[1].upper()
        if not ADDR_RE.match(addr) or not sym:
            continue
        e = out.setdefault(addr, {"name": "", "syms": []})
        if sym not in e["syms"]:
            e["syms"].append(sym)
        name = ":".join(bits[2:]).strip()[:30]
        if name and not e["name"]:
            e["name"] = name
    return out


def orders_from_fills(fills: list[dict]) -> list[dict]:
    """Dolumlar → emirler ((coin, oid)): zaman, yön, dir, adet, $, ortalama, dolum ve fiyat
    seviyesi sayısı, hepsi taker mı, twapId, emirden önce/sonra pozisyon (ilk dolumun
    `startPosition`'ı ± adet). Zamana göre sıralı."""
    by: dict[tuple, dict] = {}
    for f in fills or []:
        if not isinstance(f, dict):
            continue
        try:
            coin, oid = f.get("coin") or "", f.get("oid")
            t = int(f.get("time") or 0)
            px, sz = float(f.get("px") or 0), float(f.get("sz") or 0)
            sp = float(f.get("startPosition") or 0)
        except (TypeError, ValueError):
            continue
        side = f.get("side") or ""
        if not coin or oid is None or t <= 0 or px <= 0 or sz <= 0 or side not in ("B", "A"):
            continue
        o = by.get((coin, oid))
        if o is None:
            o = by[(coin, oid)] = {"t": t, "coin": coin, "oid": oid, "side": side,
                                   "dir": f.get("dir") or "?", "sz": 0.0, "usd": 0.0,
                                   "n_fills": 0, "pxs": set(), "taker": True, "twap": None,
                                   "sp": []}
        o["t"] = min(o["t"], t)
        o["sz"] += sz
        o["usd"] += px * sz
        o["n_fills"] += 1
        o["pxs"].add(px)
        o["taker"] = o["taker"] and bool(f.get("crossed"))
        if f.get("twapId") is not None:
            o["twap"] = f.get("twapId")
        o["sp"].append(sp)
    out = []
    for o in by.values():
        # Alışta her dolum pozisyonu artırır: emirden önceki = en küçük startPosition;
        # satışta tersi. Sonraki = önceki ± toplam adet (ölçülen dolumlardan).
        before = min(o["sp"]) if o["side"] == "B" else max(o["sp"])
        after = before + o["sz"] if o["side"] == "B" else before - o["sz"]
        out.append({"t": o["t"], "coin": o["coin"], "oid": o["oid"], "side": o["side"],
                    "dir": o["dir"], "sz": o["sz"], "usd": o["usd"], "vwap": o["usd"] / o["sz"],
                    "n_fills": o["n_fills"], "levels": len(o["pxs"]), "taker": o["taker"],
                    "twap": o["twap"], "pos_before": before, "pos_after": after})
    out.sort(key=lambda o: (o["t"], o["coin"], str(o["oid"])))
    return out


# ---------------- dizi (run) ----------------

def _new_run(o: dict, pre: bool) -> dict:
    return {"coin": o["coin"], "side": o["side"], "first": o["t"], "last": o["t"], "at": None,
            "n": 1, "sz": o["sz"], "usd": o["usd"], "px0": o["vwap"], "px1": o["vwap"],
            "pos0": o["pos_before"], "pos1": o["pos_after"], "fills": o["n_fills"],
            "taker": 1 if o["taker"] else 0, "twap": 1 if o["twap"] is not None else 0,
            "twap_id": o["twap"], "gaps": [], "sl": [round(o["usd"], 2)],
            "fl": {str(o["n_fills"]): 1}, "lv": {str(o["levels"]): 1},
            "dirs": {o["dir"]: o["usd"]}, "oid": o["oid"], "oid_vwap": o["vwap"],
            "oid_sz": o["sz"], "oid_fills": o["n_fills"], "oid_lv": o["levels"],
            "oth": {}, "tail": {}, "alerted": False, "pre": bool(pre)}


def _bump(d: dict, k: str, v: float = 1) -> None:
    d[k] = d.get(k, 0) + v


def _add(r: dict, o: dict) -> None:
    r["gaps"] = (r["gaps"] + [round((o["t"] - r["last"]) / 1000, 1)])[-SAMPLES_MAX:]
    r["sl"] = (r["sl"] + [round(o["usd"], 2)])[-SAMPLES_MAX:]
    r["last"] = o["t"]
    r["n"] += 1
    r["sz"] += o["sz"]
    r["usd"] += o["usd"]
    r["px1"] = o["vwap"]
    r["pos1"] = o["pos_after"]
    r["fills"] += o["n_fills"]
    r["taker"] += 1 if o["taker"] else 0
    if o["twap"] is not None:
        r["twap"] += 1
        r["twap_id"] = o["twap"]
    _bump(r["fl"], str(o["n_fills"]))
    _bump(r["lv"], str(o["levels"]))
    _bump(r["dirs"], o["dir"], o["usd"])
    r.update(oid=o["oid"], oid_vwap=o["vwap"], oid_sz=o["sz"], oid_fills=o["n_fills"],
             oid_lv=o["levels"])
    # Kuyruk kesinleşir: bu emirle dizinin İÇİNDE kaldığı kanıtlandı.
    for k, v in (r.get("tail") or {}).items():
        a = r["oth"].setdefault(k, {"usd": 0.0, "n": 0})
        a["usd"] += v["usd"]
        a["n"] += v["n"]
    r["tail"] = {}


def _tail_add(r: dict, o: dict) -> None:
    a = r["tail"].setdefault(f"{o['coin']}|{o['dir']}", {"usd": 0.0, "n": 0})
    a["usd"] += o["usd"]
    a["n"] += 1


def _view(r: dict, reason: str) -> dict:
    return {"coin": r["coin"], "side": r["side"], "first": r["first"], "last": r["last"],
            "n": r["n"], "usd": r["usd"], "sz": r["sz"], "reason": reason, "pre": r.get("pre")}


def _pctl(xs: list, q: float):
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def _cmed(counter: dict):
    """{'5': 10, '7': 3} sayaçlı dağılımın medyanı."""
    items = sorted((int(k), int(v)) for k, v in (counter or {}).items())
    tot = sum(v for _, v in items)
    acc = 0
    for k, v in items:
        acc += v
        if acc * 2 >= tot:
            return k
    return None


def run_stats(r: dict) -> dict:
    """Dizinin ölçülen özeti (mesajlar bundan yazılır)."""
    sz, usd_, n = float(r["sz"]), float(r["usd"]), int(r["n"])
    gaps, sl = r.get("gaps") or [], r.get("sl") or []
    oth = []
    for k, v in (r.get("oth") or {}).items():
        coin, _, d = k.partition("|")
        oth.append({"coin": coin, "dir": d, "usd": float(v["usd"]), "n": int(v["n"])})
    oth.sort(key=lambda x: -x["usd"])
    return {"n": n, "sz": sz, "usd": usd_, "vwap": usd_ / sz if sz else None,
            "px0": r.get("px0"), "px1": r.get("px1"),
            "chg": (float(r["px1"]) / float(r["px0"]) - 1) * 100 if r.get("px0") else None,
            "dur": max(0.0, (int(r["last"]) - int(r["first"])) / 1000),
            "gap_med": statistics.median(gaps) if gaps else None, "gap_min": min(gaps) if gaps else None,
            "gap_max": max(gaps) if gaps else None,
            "sl_med": statistics.median(sl) if sl else None, "sl_p10": _pctl(sl, 0.1),
            "sl_p90": _pctl(sl, 0.9),
            "taker": int(r.get("taker") or 0) / n * 100 if n else None,
            "fills_med": _cmed(r.get("fl")), "lv_med": _cmed(r.get("lv")), "others": oth}


def _close(st: dict, key: str, reason: str, now_ms: int, quiet_ms: int, silent: bool,
           due: int | None = None) -> list[dict]:
    r = (st.get("runs") or {}).pop(key, None)
    if r is None:
        return []
    if not r.get("alerted"):
        st["short"] = int(st.get("short") or 0) + 1      # eşiğe varmayan kısa seri: sessiz
        return []
    st["hist"] = ((st.get("hist") or []) + [_view(r, reason)])[-HIST_MAX:]
    if silent:
        return []
    due_ms = due if due is not None else int(r["last"]) + quiet_ms
    return [{"t": "end", "key": key, "run": r, "reason": reason,
             "late": max(0, (now_ms - due_ms) // 1000)}]


def _collapse(ev: list[dict]) -> list[dict]:
    """Aynı turda başlayıp biten dizi (kesintiden sonra yeniden oynatma) → tek 'bitti'."""
    ended = {(e["key"], e["run"]["first"]) for e in ev if e["t"] == "end"}
    started = {(e["key"], e["run"]["first"]) for e in ev if e["t"] == "start"}
    out = []
    for e in ev:
        k = (e["key"], e["run"]["first"])
        if e["t"] == "start" and k in ended:
            continue
        if e["t"] == "end" and k in started:
            e = {**e, "also_start": True}
        out.append(e)
    return out


def step(st: dict, orders: list[dict], syms: set, now_ms: int, complete: bool, quiet_ms: int,
         start_n: int, start_usd: float, silent: bool = False) -> list[dict]:
    """Bir turun emirleri → olaylar (saf; `st`'yi günceller). Yön başına AYRI dizi."""
    runs = st.setdefault("runs", {})
    ev: list[dict] = []
    for o in orders:
        key = f"{o['coin']}|{o['side']}"
        for k, r in runs.items():
            if k != key:
                _tail_add(r, o)
        if symbol_of(o["coin"]) not in syms:
            continue
        r = runs.get(key)
        if r is not None and o["t"] - int(r["last"]) > quiet_ms:
            ev += _close(st, key, "quiet", now_ms, quiet_ms, silent)
            r = None
        if r is None:
            pre = bool(silent and st.get("window") and o["t"] - int(st["window"]) <= quiet_ms)
            r = runs[key] = _new_run(o, pre)
        else:
            _add(r, o)
        if not r["alerted"] and r["n"] >= start_n and r["usd"] >= start_usd:
            r["alerted"], r["at"] = True, o["t"]
            okey = f"{o['coin']}|{'A' if o['side'] == 'B' else 'B'}"
            opp = runs.get(okey)
            if opp is not None and opp.get("alerted") and int(opp["last"]) < int(r["first"]):
                ev += _close(st, okey, "flip", now_ms, quiet_ms, silent, due=o["t"])
            if not silent:
                # Dizinin kendisi (kopya değil): mesaj turun sonunda yazılır, aynı turdaki
                # sonraki dilimler de sayılara girer.
                ev.append({"t": "start", "key": key, "run": r,
                           "late": max(0, (now_ms - o["t"]) // 1000)})
    if complete:
        for k in sorted(runs):
            if now_ms - int(runs[k]["last"]) > quiet_ms:
                ev += _close(st, k, "quiet", now_ms, quiet_ms, silent)
    return _collapse(ev)


# ---------------- okuma ----------------

def _t(f: dict) -> int:
    try:
        return int(f.get("time") or 0)
    except (TypeError, ValueError):
        return 0


async def fetch_fills(client, addr: str, cursor_ms: int, edge: list, max_pages: int,
                      low: bool = False, trust_empty: bool = False) -> dict:
    """İmleçten (dahil) bu yana YENİ dolumlar + süreklilik sondası.

    {"unsure": True}: imleç ms'indeki dolumlar (edge) gelmedi, sayfa boş → gecikmeli düğüm.
    {"gap": True}: sayfa dolu ama edge yok → HL geçmişi imlecin ötesine kaymış.
    Aksi halde {"fills", "complete", "cursor", "edge", "pages"}; sayfa sınırına takılınca
    son milisaniye grubu sonraki tura bırakılır (bir IOC'nin dolumları bölünmesin)."""
    from ..hl.client import PRIORITY
    tok = PRIORITY.set("low") if low else None
    try:
        fills, complete = await acct.fills_between(client, addr, int(cursor_ms), None, max_pages)
    finally:
        if tok is not None:
            PRIORITY.reset(tok)
    fills.sort(key=lambda f: (_t(f), str(acct.fill_key(f))))
    edge_s = {str(x) for x in edge or []}
    if edge_s:
        if not fills:
            if not trust_empty:
                return {"unsure": True}
            edge_s = set()                   # uzun süre boş: boşluk kabul edilir
        elif not edge_s <= {str(acct.fill_key(f)) for f in fills if _t(f) == int(cursor_ms)}:
            return {"gap": True}
    new = [f for f in fills if str(acct.fill_key(f)) not in edge_s]
    pages = max(1, -(-len(fills) // acct.FILL_PAGE))

    def _edge_of(rows: list[dict], at: int) -> list[str]:
        e = {str(acct.fill_key(f)) for f in rows if _t(f) == at}
        return sorted(e | edge_s) if at == int(cursor_ms) else sorted(e)

    if not complete and new:
        hold = _t(new[-1])
        keep = [f for f in new if _t(f) < hold]
        if keep:
            last = _t(keep[-1])
            return {"fills": keep, "complete": False, "cursor": last,
                    "edge": _edge_of(keep, last), "pages": pages}
        # Tek milisaniyede 2000+ dolum: ilerlenemez — o milisaniye atlanır.
        return {"fills": new, "complete": False, "cursor": hold + 1, "edge": [], "pages": pages}
    if new:
        last = _t(new[-1])
        return {"fills": new, "complete": complete, "cursor": last, "edge": _edge_of(new, last),
                "pages": pages}
    return {"fills": [], "complete": complete, "cursor": int(cursor_ms), "edge": sorted(edge_s),
            "pages": pages}


# ---------------- durum ----------------

def restart_reason(prev: dict, chat: str, syms: list, ts: int) -> str | None:
    if not prev:
        return "first"
    if int(prev.get("v") or 0) != STATE_V:
        return "version"
    if (prev.get("chat") or "") != chat:
        return "chat"
    if sorted(prev.get("syms") or []) != sorted(syms):
        return "list"
    if ts - int(prev.get("ts") or 0) > REPLAY_MAX_SEC:
        return "stale"
    return None


def fresh(prev: dict, reason: str, ts: int, chat: str, ent: dict, now_ms: int) -> dict:
    """Yeniden başlangıç: son LOOKBACK_SEC sessizce okunur (ısınma), sonra özet. Kesinti
    (stale / gap) öncesi süren diziler özette "kesintiden önce" diye yazılır."""
    prev = prev or {}
    window = now_ms - LOOKBACK_SEC * 1000
    cut = reason in ("stale", "gap")
    return {"v": STATE_V, "ts": int(ts), "chat": chat, "name": ent.get("name") or "",
            "syms": sorted(ent.get("syms") or []), "cursor": window, "edge": [], "warm": True,
            "window": window, "reason": reason, "runs": {}, "unsure": 0, "pages": 0,
            "fills_n": 0, "short": 0, "summary_due": False,
            "hist": [] if reason == "version" else list(prev.get("hist") or []),
            "prev_gap": (int(ts) - int(prev.get("ts") or ts)) if cut and prev.get("ts") else None,
            "prev_open": [_view(r, "open") for r in (prev.get("runs") or {}).values()
                          if r.get("alerted")] if cut else []}


def rules(cfg) -> tuple[int, int, float]:
    quiet = max(QUIET_MIN_SEC, int(getattr(cfg, "slice_quiet_sec", 300) or 300))
    start_n = max(2, int(getattr(cfg, "slice_start_orders", 3) or 3))
    start_usd = max(0.0, float(getattr(cfg, "slice_start_usd", 10_000) or 0))
    return quiet * 1000, start_n, start_usd


async def poll_address(cfg, client, addr: str, ent: dict, ts: int, chat: str) -> dict:
    """Bir adresin bir turu: {"kind": warm|unsure|summary|events, "state", "events", "fills"}."""
    prev = await kv_get(STATE_KV + addr) or {}
    syms = sorted(ent.get("syms") or [])
    now_ms = int(ts) * 1000
    reason = restart_reason(prev, chat, syms, ts)
    st = fresh(prev, reason, ts, chat, ent, now_ms) if reason else json.loads(json.dumps(prev))
    if not reason and st.get("summary_due"):
        return {"kind": "summary", "state": st, "events": [], "fills": 0}
    if (st.get("name") or "") != (ent.get("name") or ""):
        st["name"] = ent.get("name") or ""
    quiet_ms, start_n, start_usd = rules(cfg)
    r: dict = {}
    for attempt in (0, 1):
        warm = bool(st.get("warm"))
        r = await fetch_fills(client, addr, int(st.get("cursor") or 0), st.get("edge") or [],
                              LOOKBACK_PAGES if warm else CATCHUP_PAGES, low=warm,
                              trust_empty=int(st.get("unsure") or 0) >= UNSURE_MAX)
        if r.get("gap") and attempt == 0:
            log.info("dilimli: %s dolum geçmişi imlecin ötesine kaymış — yeniden başlıyor", addr[:10])
            st = fresh(st, "gap", ts, chat, ent, now_ms)
            continue
        break
    if r.get("gap"):
        raise RuntimeError("dolum sürekliliği kurulamadı")
    if r.get("unsure"):
        st["unsure"] = int(st.get("unsure") or 0) + 1
        return {"kind": "unsure", "state": st, "events": [], "fills": 0}
    warm = bool(st.get("warm"))
    orders = orders_from_fills(r["fills"])
    ev = step(st, orders, set(syms), now_ms, bool(r["complete"]), quiet_ms, start_n, start_usd,
              silent=warm)
    st.update(cursor=int(r["cursor"]), edge=list(r["edge"]), ts=int(ts), unsure=0,
              pages=int(st.get("pages") or 0) + int(r.get("pages") or 0),
              fills_n=int(st.get("fills_n") or 0) + len(r["fills"]))
    if warm:
        if not r["complete"]:
            return {"kind": "warm", "state": st, "events": [], "fills": len(r["fills"])}
        st["warm"] = False
        st["summary_due"] = True
        return {"kind": "summary", "state": st, "events": [], "fills": len(r["fills"])}
    return {"kind": "events", "state": st, "events": ev, "fills": len(r["fills"])}


# ---------------- zenginleştirme (yalnız mesaj anında) ----------------

async def _position(client, addr: str, coin: str) -> dict:
    """{"ok": okundu mu, "pos": canlı pozisyon ya da None}."""
    from .tracker import live_position
    try:
        return {"ok": True, "pos": await live_position(client, addr, coin)}
    except Exception:
        log.debug("dilimli: pozisyon okunamadı %s %s", addr[:10], coin, exc_info=True)
        return {"ok": False, "pos": None}


async def _method(client, addr: str, r: dict) -> dict | None:
    """Son dilimin emri (orderStatus): tür, tif, limitin dolum ortalamasına uzaklığı, ne
    kadar doldu. twapId'li dizide sorulmaz (HL TWAP'ı zaten belli)."""
    if int(r.get("twap") or 0) > 0 or r.get("oid") is None:
        return None
    try:
        resp = await client.order_status(addr, int(r["oid"]))
    except Exception:
        log.debug("dilimli: orderStatus okunamadı", exc_info=True)
        return None
    o = (((resp or {}).get("order") or {}).get("order")) or {}
    try:
        lim, orig = float(o.get("limitPx") or 0), float(o.get("origSz") or 0)
    except (TypeError, ValueError):
        return None
    if not o or lim <= 0:
        return None
    vw, osz = float(r.get("oid_vwap") or 0), float(r.get("oid_sz") or 0)
    off = None
    if vw > 0:
        off = (lim - vw) / vw * 100 if r["side"] == "B" else (vw - lim) / vw * 100
    return {"order_type": o.get("orderType") or "", "tif": o.get("tif") or "", "limit": lim,
            "off_pct": off, "filled_pct": osz / orig * 100 if orig > 0 else None,
            "cloid": o.get("cloid"), "fills": r.get("oid_fills"), "levels": r.get("oid_lv")}


async def _share(client, r: dict) -> dict | None:
    """Dizinin aynı dakikalardaki coin hacmindeki payı (adet; 1 dk mumlar, >3 gün 15 dk)."""
    first, last = int(r["first"]), int(r["last"])
    interval, step_ms = ("1m", 60_000) if last - first <= 3 * 86400_000 else ("15m", 900_000)
    try:
        cs = await client.candles(r["coin"], interval, first // step_ms * step_ms, last)
    except Exception:
        log.debug("dilimli: mum okunamadı %s", r["coin"], exc_info=True)
        return None
    vol = 0.0
    for c in cs or []:
        try:
            vol += float((c or {}).get("v") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
    his = float(r["sz"])
    if vol <= 0 or his > vol * 1.0001:
        return None
    return {"share": his / vol * 100, "his": his, "vol": vol, "interval": interval}


async def _coin_for(st: dict, sym: str) -> str | None:
    for r in list((st.get("runs") or {}).values()) + list(reversed(st.get("hist") or [])):
        if symbol_of(r.get("coin") or "") == sym:
            return r["coin"]
    try:
        from ..hl.universe import resolve_coin
        rc = await resolve_coin(sym)
        return rc["coin"] if rc else None
    except Exception:
        return None


async def _summary_text(client, addr: str, ent: dict, st: dict, ts: int, rl: tuple) -> str:
    from ..telegram import format as fmt
    coins = {}
    pos = {}
    for sym in st.get("syms") or []:
        coin = await _coin_for(st, sym)
        coins[sym] = coin
        if coin:
            pos[coin] = await _position(client, addr, coin)
    meth = {}
    stats = {}
    for k, r in (st.get("runs") or {}).items():
        stats[k] = run_stats(r)
        if r.get("alerted"):
            meth[k] = await _method(client, addr, r)
    ctx = {"now_ms": ts * 1000, "coins": coins, "pos": pos, "method": meth, "stats": stats,
           "quiet_s": rl[0] // 1000, "start_n": rl[1], "start_usd": rl[2],
           "lookback_s": LOOKBACK_SEC}
    return fmt.slice_summary(addr, ent, st, ctx)


async def _events_text(client, addr: str, ent: dict, st: dict, ev: list[dict], ts: int,
                       rl: tuple) -> str:
    from ..telegram import format as fmt
    pos = {}
    for e in ev:
        r = e["run"]
        e["stats"] = run_stats(r)
        if r["coin"] not in pos:
            pos[r["coin"]] = await _position(client, addr, r["coin"])
        if e["t"] == "start":
            e["method"] = await _method(client, addr, r)
        else:
            e["share"] = await _share(client, r)
    ctx = {"now_ms": ts * 1000, "pos": pos, "quiet_s": rl[0] // 1000, "late_sec": LATE_SEC,
           "window": st.get("window")}
    return fmt.slice_events(addr, ent, ev, st, ctx)


def _active(st: dict) -> list[dict]:
    return [{"coin": r["coin"], "side": r["side"], "first": r["first"], "last": r["last"],
             "n": r["n"], "usd": r["usd"]}
            for r in (st.get("runs") or {}).values() if r.get("alerted")]


async def run_once(cfg, client, notifier, ts: int | None = None) -> dict:
    """Bir tur: listedeki her adres → olaylar → tek mesaj → durum."""
    from ..notify import kind_enabled
    ts = int(ts or now())
    out = {"accounts": 0, "polled": 0, "fills": 0, "warm": 0, "unsure": 0, "summary": 0,
           "started": 0, "ended": 0, "sent": 0, "failed": 0, "errors": 0, "backoff": 0,
           "no_chat": 0, "muted": 0, "active": []}
    watches = parse_slice_list(getattr(cfg, "slice_watch_list", "") or "")
    out["accounts"] = len(watches)
    # Listeden çıkarılan adresin durumu silinir: geri eklenince "izleme başladı" özeti gelsin.
    prev_addrs = await kv_get(ADDRS_KV) or []
    for a in prev_addrs:
        if a not in watches:
            await kv_set(STATE_KV + a, None)
            _BACKOFF.pop(a, None)
    if sorted(prev_addrs) != sorted(watches):
        await kv_set(ADDRS_KV, sorted(watches))
    chat = (getattr(cfg, "account_chat_id", "") or "").strip()
    if not watches:
        return out
    if not chat:
        out["no_chat"] = len(watches)          # kanal yok → yoklama da yok (bütçe harcanmaz)
        return out
    if notifier is None or not kind_enabled(cfg, "slice"):
        out["muted"] = len(watches)            # bildirim kapalı → yoklama yok
        return out
    rl = rules(cfg)
    poll = max(10, int(getattr(cfg, "slice_poll_sec", 30) or 30))
    for addr, ent in watches.items():
        b = _BACKOFF.get(addr)
        if b and ts < b[0]:
            out["backoff"] += 1
            continue
        try:
            r = await poll_address(cfg, client, addr, ent, ts, chat)
        except Exception as e:
            out["errors"] += 1
            log.warning("dilimli alım-satım okunamadı %s: %s", addr[:10], e)
            continue
        out["polled"] += 1
        out["fills"] += int(r.get("fills") or 0)
        st, kind = r["state"], r["kind"]
        if kind in ("warm", "unsure"):
            out[kind] += 1
            await kv_set(STATE_KV + addr, st)
            out["active"] += _active(st)
            continue
        if kind == "summary":
            await kv_set(STATE_KV + addr, st)      # ısınma emeği korunur; bayrak gönderimde iner
            text = await _summary_text(client, addr, ent, st, ts, rl)
        else:
            ev = r["events"]
            if not ev:
                await kv_set(STATE_KV + addr, st)
                out["active"] += _active(st)
                continue
            text = await _events_text(client, addr, ent, st, ev, ts, rl)
        ok = await notifier.send("slice", text, priority="high", key=f"slice:{addr}:{ts}",
                                 chat_id=chat, public=False)
        if ok:
            _BACKOFF.pop(addr, None)
            out["sent"] += 1
            if kind == "summary":
                st["summary_due"] = False
                out["summary"] += 1
            else:
                out["started"] += sum(1 for e in r["events"] if e["t"] == "start")
                out["ended"] += sum(1 for e in r["events"] if e["t"] == "end")
            await kv_set(STATE_KV + addr, st)
        else:
            # Durum YAZILMAZ (özette ısınma zaten yazıldı): sonraki deneme aynı olayı yeniden
            # bulur. Her deneme imleçten yeniden okuduğu için aralık katlanarak açılır.
            out["failed"] += 1
            k = (_BACKOFF.get(addr) or (0, 0))[1] + 1
            _BACKOFF[addr] = (ts + min(BACKOFF_MAX_SEC, poll * 2 ** k), k)
        out["active"] += _active(st)
    return out


async def snapshots(cfg) -> list[dict]:
    """/hesaplar için: listedeki adreslerin son durumu (kv; HL'ye istek yok)."""
    out = []
    for addr, ent in parse_slice_list(getattr(cfg, "slice_watch_list", "") or "").items():
        out.append({"address": addr, "name": ent["name"], "syms": ent["syms"],
                    "state": await kv_get(STATE_KV + addr) or {}})
    return out


async def loop(cfg, client, notifier) -> None:
    """Denetimli döngü. Site ASLA buna bağımlı değil."""
    from ..health import beat
    await asyncio.sleep(50)
    tot: dict[str, int] = {}
    while True:
        try:
            if getattr(cfg, "slice_watch_enabled", True):
                out = await run_once(cfg, client, notifier)
                for k in ("polled", "fills", "summary", "started", "ended", "sent", "failed",
                          "errors", "unsure", "warm"):
                    tot[k] = tot.get(k, 0) + int(out.get(k) or 0)
                await kv_set(STATS_KV, {**out, "tot": tot, "ts": now()})
                if out["sent"] or out["errors"]:
                    log.info("dilimli alım-satım: %d başladı, %d bitti, %d mesaj, %d hata",
                             out["started"], out["ended"], out["sent"], out["errors"])
            else:
                await kv_set(STATS_KV, {"ts": now(), "disabled": True})
            await beat("slicewatch")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("dilimli alım-satım turu")
        await asyncio.sleep(max(10, int(getattr(cfg, "slice_poll_sec", 30) or 30)))
