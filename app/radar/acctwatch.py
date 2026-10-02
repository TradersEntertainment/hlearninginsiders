"""👤 İzlenen hesaplar — adı konmuş birkaç hesabın pozisyonları ve emirleri.

Kullanıcı isteği (02.10): drkmttr vault'u için "pozları için mesaj gelsin, yine
benzer şeyi (yapışkan duvar) yapıyorsa"; kararlar: dört olayın hepsi (yeni duvar /
kampanya, pozisyon aç-kapa-ters çevir, büyüdü-küçüldü, kâr al-stop merdiveni),
'adres:isim' listesi (başlangıçta drkmttr) ve AYRI GRUP — kripto kanalı spam olmasın
(`ACCOUNT_CHAT_ID`, env; boşsa hiçbir yere gitmez, yoklama da yapılmaz).

NEDEN AYRI HAT (mevcut takipler yetmiyor):
  • `/takip_N` (tracker) TEK coindeki TEK pozisyonu izler ve haber komutun geldiği
    sohbete gider. Burada izlenen HESABIN TAMAMIDIR: yeni coinde açılan pozisyon da,
    defterdeki emirler de.
  • HL arayüzü vault sayfasında açık emirleri GÖSTERMİYOR (yalnız işlemler), ama
    `frontendOpenOrders` veriyor: post-only duvar, reduce-only kâr al merdiveni, stop'lar.

Ölçüm: hesap başına her `acct_poll_sec`'te İKİ istek (`clearinghouseState` ağırlık 2,
`frontendOpenOrders` ağırlık 20). Durum kv'de (`acctwatch:<adres>`) — yeniden
başlatma eski olayları yeniden bildirmez.

Olay kuralları (hepsi ÖLÇÜLEN; tahmin yok):
  • pozisyon: açtı / kapattı / ters çevirdi; son BİLDİRİLEN boyuta göre ≥ step_pct
    VE ≥ step_min_usd adım (fiyat oynaması tetiklemez — adet üzerinden).
  • duvar: en iyi fiyata yakın (mark'a %1) tek emir ≥ `acct_wall_min_usd`, iki
    yoklama üst üste görülünce; bitince (iki yoklama yok) süre + o coindeki pozisyon değişimi.
  • merdiven: reduce-only limit (kâr al) ya da tetik (stop/TP) emir grubu. Kuruldu /
    YENİ emir eklendi-yeniden fiyatlandı (iki yoklama sabit) / tamamen kalktı. Tek tek
    basamak dolumu mesaj ÜRETMEZ — o, pozisyonun küçülmesi olarak görünür.
Bir yoklamadaki olaylar TEK mesajda gider. Gönderilemezse durum yazılmaz: sonraki
yoklama aynı farkı yeniden bulur.
"""
from __future__ import annotations

import asyncio
import logging
import re

from ..db import kv_get, kv_set, now

log = logging.getLogger("radar.acctwatch")

STATE_KV = "acctwatch:"          # + adres
STATS_KV = "acctwatch_stats"
NEAR_PCT = 1.0                   # duvar: mark'a bu kadar yakın
WALL_GONE_POLLS = 2              # duvar bu kadar yoklama üst üste yoksa bitti
LADDER_MIN_N = 2                 # merdiven: en az bu kadar emir …
LADDER_MIN_USD = 100_000         # … ya da bu kadar $ (tek büyük TP/stop da sayılır)
OIDS_KEEP = 400                  # duvar başına hatırlanan oid (yeniden koyma sayacı)
FRONT_CAP = 100                  # frontendOpenOrders bu kadar dönerse liste KESİK olabilir
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


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


def positions_of(state: dict | None) -> dict[str, dict]:
    """clearinghouseState → {coin: {szi, side, ntl, entry, liq, lev}} (sıfırlar atlanır)."""
    out = {}
    for p in (state or {}).get("assetPositions") or []:
        pp = (p or {}).get("position") or {}
        try:
            szi = float(pp.get("szi") or 0)
            if not szi or not pp.get("coin"):
                continue
            lev = pp.get("leverage") or {}
            out[pp["coin"]] = {
                "szi": szi, "side": "long" if szi > 0 else "short",
                "ntl": abs(float(pp.get("positionValue") or 0)),
                "entry": float(pp.get("entryPx") or 0) or None,
                "liq": float(pp["liquidationPx"]) if pp.get("liquidationPx") else None,
                "lev": lev.get("value") if isinstance(lev, dict) else None}
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def classify_orders(orders, marks: dict, wall_min_usd: float = 0.0) -> tuple[dict, dict]:
    """(duvar adayları, merdivenler).

    duvar: (coin|side) → o yanın mark'a %1 içindeki EN BÜYÜK tetiksiz limit emri.
    Reduce-only emir fiyata yakın olsa da ancak duvar tabanını geçerse duvardır;
    yoksa merdivenin en yakın basamağıdır (fiyat merdivene değmiş olabilir).
    merdiven: (coin|side|tür) → reduce-only tetiksiz limitler ('tp') ya da tetik
    emirleri ('stop'); n, fiyat aralığı, toplam adet/$, oid kümesi."""
    walls: dict[str, dict] = {}
    ladders: dict[str, dict] = {}
    for o in orders or []:
        try:
            coin, side = o.get("coin") or "", "ask" if o.get("side") == "A" else "bid"
            px = _f(o.get("limitPx"))
            sz = _f(o.get("sz"))
            trig = bool(o.get("isTrigger"))
            ro = bool(o.get("reduceOnly"))
        except AttributeError:
            continue
        if not coin or sz <= 0:
            continue
        ref_px = _f(o.get("triggerPx")) if trig and _f(o.get("triggerPx")) > 0 else px
        ntl = (px or ref_px) * sz
        mark = _f(marks.get(coin))
        if (not trig and px > 0 and mark > 0 and abs(px - mark) / mark * 100 <= NEAR_PCT
                and (not ro or ntl >= wall_min_usd)):
            k = f"{coin}|{side}"
            if k not in walls or ntl > walls[k]["ntl"]:
                walls[k] = {"coin": coin, "side": side, "px": px, "sz": sz, "ntl": ntl,
                            "oid": o.get("oid"), "tif": o.get("tif") or "", "ro": ro,
                            "dist": (px - mark) / mark * 100}
            continue                         # en iyi fiyattaki emir merdiven sayılmaz
        if not (trig or ro):
            continue
        kind = "stop" if trig else "tp"
        k = f"{coin}|{side}|{kind}"
        L = ladders.setdefault(k, {"coin": coin, "side": side, "kind": kind, "n": 0, "lo": None,
                                   "hi": None, "sz": 0.0, "ntl": 0.0, "oids": []})
        L["n"] += 1
        L["lo"] = ref_px if L["lo"] is None else min(L["lo"], ref_px)
        L["hi"] = ref_px if L["hi"] is None else max(L["hi"], ref_px)
        L["sz"] += sz
        L["ntl"] += ntl
        if o.get("oid") is not None:
            L["oids"].append(o.get("oid"))
    ladders = {k: v for k, v in ladders.items() if v["n"] >= LADDER_MIN_N or v["ntl"] >= LADDER_MIN_USD}
    return walls, ladders


def position_events(prev: dict, cur: dict, marks: dict, step_pct: float, step_min_usd: float,
                    min_pos_usd: float) -> tuple[list[dict], dict]:
    """Pozisyon olayları + yeni durum. Durum coin → {szi, base, ntl, side}; `base` son
    BİLDİRİLEN boyut (adım ona göre ölçülür; fiyat oynaması adım sayılmaz)."""
    ev: list[dict] = []
    nxt: dict = {}
    for coin in sorted(set(prev) | set(cur)):
        p, c = prev.get(coin), cur.get(coin)
        mark = _f(marks.get(coin)) or (c["ntl"] / abs(c["szi"]) if c and c["szi"] else 0.0)
        if c and c["ntl"] < min_pos_usd and not p:
            continue                         # toz: hiç izlenmemişse atla
        if c and not p:
            ev.append({"t": "open", "coin": coin, "cur": c})
            nxt[coin] = {"szi": c["szi"], "base": c["szi"], "ntl": c["ntl"], "side": c["side"]}
            continue
        if p and not c:
            ev.append({"t": "close", "coin": coin, "prev": p})
            continue
        base = _f(p.get("base") or p.get("szi"))
        if (base > 0) != (c["szi"] > 0):
            ev.append({"t": "flip", "coin": coin, "prev": p, "cur": c})
            nxt[coin] = {"szi": c["szi"], "base": c["szi"], "ntl": c["ntl"], "side": c["side"]}
            continue
        d = abs(c["szi"]) - abs(base)
        pct = d / abs(base) * 100 if base else 0.0
        if base and abs(pct) >= step_pct and abs(d) * mark >= step_min_usd:
            ev.append({"t": "grow" if d > 0 else "shrink", "coin": coin, "prev": p, "cur": c,
                       "base": base, "pct": pct, "dusd": d * mark})
            base = c["szi"]
        nxt[coin] = {"szi": c["szi"], "base": base, "ntl": c["ntl"], "side": c["side"]}
    return ev, nxt


def wall_events(prev: dict, cur: dict, pos: dict, wall_min_usd: float, ts: int) -> tuple[list[dict], dict]:
    """Duvar olayları + yeni durum. İki yoklama üst üste görülünce 'kurdu';
    WALL_GONE_POLLS yoklama yoksa 'bitti' (yalnız bildirilmişse mesaj). Görülen
    farklı oid sayısı = kaç kez yeniden konduğu (en az; yoklama arası görünmez)."""
    ev: list[dict] = []
    nxt: dict = {}
    for k in sorted(set(prev) | set(cur)):
        p, c = prev.get(k), cur.get(k)
        if c is not None and c["ntl"] < wall_min_usd and not (p and p.get("alerted")):
            c = None                         # taban altı ve henüz izlenmiyor
        if c is None and p is None:
            continue
        if c is not None:
            s = dict(p) if p else {"first_ts": ts, "seen": 0, "oids": [], "alerted": False,
                                   "px_min": c["px"], "px_max": c["px"], "ntl_max": 0.0,
                                   "start_szi": _f((pos.get(c["coin"]) or {}).get("szi"))}
            oids = list(s.get("oids") or [])
            if c.get("oid") is not None and c["oid"] not in oids:
                oids.append(c["oid"])
            s.update(seen=int(s.get("seen") or 0) + 1, miss=0, oids=oids[-OIDS_KEEP:],
                     last_ts=ts, coin=c["coin"], side=c["side"], px=c["px"], sz=c["sz"],
                     ntl=c["ntl"], tif=c["tif"], ro=c["ro"], dist=c["dist"],
                     px_min=min(_f(s.get("px_min"), c["px"]), c["px"]),
                     px_max=max(_f(s.get("px_max"), c["px"]), c["px"]),
                     ntl_max=max(_f(s.get("ntl_max")), c["ntl"]))
            if not s["alerted"] and s["seen"] >= 2 and c["ntl"] >= wall_min_usd:
                ev.append({"t": "wall", "w": dict(s)})
                s["alerted"] = True
            nxt[k] = s
            continue
        s = dict(p)
        s["miss"] = int(s.get("miss") or 0) + 1
        if s["miss"] < WALL_GONE_POLLS:
            nxt[k] = s
            continue
        if s.get("alerted"):
            ev.append({"t": "wall_end", "w": s,
                       "end_szi": _f((pos.get(s.get("coin")) or {}).get("szi"))})
    return ev, nxt


def _view(L: dict) -> dict:
    return {"n": L["n"], "lo": L["lo"], "hi": L["hi"], "sz": L["sz"], "ntl": L["ntl"]}


def ladder_events(prev: dict, cur: dict) -> tuple[list[dict], dict]:
    """Merdiven olayları + yeni durum.

    • kuruldu: iki yoklama üst üste görülünce (kurulurken yarım görünmesin)
    • değişti: BİLİNMEYEN oid'ler (yeni emir / yeniden fiyatlama) iki yoklama sabit
      kalınca — eskisi → yenisi
    • kalktı: iki yoklama hiç yok
    Basamak dolumu (oid'in kaybolması) mesaj ÜRETMEZ; pozisyon küçülmesi olarak görünür."""
    ev: list[dict] = []
    nxt: dict = {}
    for k in sorted(set(prev) | set(cur)):
        p, c = prev.get(k), cur.get(k)
        if c is None:
            s = dict(p)
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
            s.update(oids=sorted(cur_oids), alerted=False, pending=[], view=None)
            nxt[k] = s
            continue
        if not p.get("alerted"):
            s.update(oids=sorted(cur_oids), alerted=True, pending=[], view=_view(c))
            ev.append({"t": "ladder_new", "L": dict(s)})
            nxt[k] = s
            continue
        known = set(p.get("oids") or [])
        new = cur_oids - known
        s.update(alerted=True, view=p.get("view"))
        if new and sorted(new) == sorted(p.get("pending") or []):
            ev.append({"t": "ladder_change", "L": dict(s), "was": p.get("view") or {}})
            s.update(oids=sorted(cur_oids), pending=[], view=_view(c))
        elif new:
            s.update(oids=sorted(known & cur_oids), pending=sorted(new))
        else:
            s.update(oids=sorted(cur_oids), pending=[])
        nxt[k] = s
    return ev, nxt


async def poll_account(cfg, client, addr: str, name: str, marks: dict, ts: int) -> dict:
    """Bir hesabın bir yoklaması: (olaylar, yeni durum, ilk tur mu, ham anlık görüntü)."""
    st = await client.clearinghouse(addr)
    orders = await client.frontend_open_orders(addr)
    pos = positions_of(st)
    wall_min = float(getattr(cfg, "acct_wall_min_usd", 250_000) or 0)
    walls, ladders = classify_orders(orders, marks, wall_min)
    prev = await kv_get(STATE_KV + addr) or {}
    first = not prev
    min_pos = float(getattr(cfg, "acct_min_pos_usd", 100_000) or 0)
    pev, pstate = position_events(prev.get("pos") or {}, pos, marks,
                                  float(getattr(cfg, "acct_step_pct", 25) or 25),
                                  float(getattr(cfg, "acct_step_min_usd", 500_000) or 0), min_pos)
    wev, wstate = wall_events(prev.get("walls") or {}, walls, pos, wall_min, ts)
    lev, lstate = ladder_events(prev.get("ladders") or {}, ladders)
    ms = (st or {}).get("marginSummary") or {}
    snap = {"ts": ts, "name": name, "pos": pstate, "walls": wstate, "ladders": lstate,
            "orders_n": len(orders or []), "truncated": len(orders or []) >= FRONT_CAP,
            "acct": _f(ms.get("accountValue")), "ntl": _f(ms.get("totalNtlPos")),
            "live": {c: {k: v for k, v in p.items()} for c, p in pos.items()}}
    if first:
        # İlk tur: mevcut her şey "olay" değil, TABAN — açılış özetinde listelenir,
        # bir sonraki yoklamada "yeni kuruldu" diye yeniden bildirilmez.
        for w in wstate.values():
            w["alerted"] = True
        for L in lstate.values():
            L.update(alerted=True, view=_view(L))
        return {"events": [], "state": snap, "first": True}
    return {"events": pev + wev + lev, "state": snap, "first": False}


async def run_once(cfg, client, notifier, ts: int | None = None) -> dict:
    """Bir tur: listedeki her hesap → olaylar → tek mesaj → durum."""
    from ..telegram import format as fmt
    from ..hl.universe import main_dex_ctx
    ts = ts or now()
    out = {"accounts": 0, "polled": 0, "events": 0, "sent": 0, "failed": 0, "errors": 0,
           "no_chat": 0, "first": 0, "truncated": 0}
    accts = parse_watch_list(getattr(cfg, "acct_watch_list", "") or "")
    out["accounts"] = len(accts)
    chat = (getattr(cfg, "account_chat_id", "") or "").strip()
    if not accts:
        return out
    if not chat:
        out["no_chat"] = len(accts)          # kanal yok → yoklama da yok (bütçe harcanmaz)
        return out
    try:
        marks = {c: _f(v.get("m")) for c, v in ((await main_dex_ctx(client)).get("c") or {}).items()}
    except Exception:
        marks = {}
    for addr, name in accts:
        try:
            r = await poll_account(cfg, client, addr, name, marks, ts)
        except Exception as e:
            out["errors"] += 1
            log.warning("izlenen hesap okunamadı %s (%s): %s", name, addr[:10], e)
            continue
        out["polled"] += 1
        st = r["state"]
        out["truncated"] += 1 if st["truncated"] else 0
        if r["first"]:
            text = fmt.acct_started(addr, st, marks)
            ok = notifier is not None and await notifier.send(
                "acct", text, priority="high", key=f"acct:{addr}:start", chat_id=chat, public=False)
            if ok:
                await kv_set(STATE_KV + addr, st)
                out["first"] += 1
                out["sent"] += 1
            else:
                out["failed"] += 1
            continue
        ev = r["events"]
        out["events"] += len(ev)
        if not ev:
            await kv_set(STATE_KV + addr, st)
            continue
        text = fmt.acct_events(addr, st, ev, marks)
        ok = notifier is not None and await notifier.send(
            "acct", text, priority="high", key=f"acct:{addr}:{ts}", chat_id=chat, public=False)
        if ok:
            await kv_set(STATE_KV + addr, st)
            out["sent"] += 1
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
                for k, v in out.items():
                    tot[k] = tot.get(k, 0) + int(v or 0) if k not in ("accounts",) else int(v or 0)
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
