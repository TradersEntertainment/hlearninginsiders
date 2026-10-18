"""📟 Coin sayfası canlı akışı — terminal düzeninin veri hattı (SSE: /t/{coin}/live).

Kullanıcı (18.10): "grafik ve liq falan her şey tam tek bakışta sığacak şekilde tasarım yapman lazım, terminal gibi
akmalı canlı veriler de ben o sayfadaysam, iyice uğraş".

Kararlar (ölçümler: sayfa haritası 18.10):
  • İşlem akışı kollektörün ZATEN aldığı WS `trades`'ten gelir — ek HL isteği yok. Kanca tape.observe'un hemen
    arkası: yalnız bantta sayılan (tekrar / açılış öncesi süzülmüş) işlem yayılır. Sıcak yol: senkron, I/O yok.
  • Bellek: işlem halkası yalnız İZLENEN coinde (sayfa son WATCH_TTL içinde açıldı) tutulur; her dinlenen coin için
    yalnız son fiyat. İzleyici yokken kanca tek dict bakışı.
  • 15 dk toplamları (tape.summary 25–88 ms CPU, olay döngüsünü bloklar) izleyici başına değil coin başına STATS_TTL
    önbellekle; defter (l2Book, ağırlık 2) coin başına DEPTH_TTL önbellek + tek süren istek (düşük şerit); defter
    sahipleri bookowners'ın kendi 30 sn önbelleğiyle (pahalı: aday başına ağırlık 20).
  • SSE bağlantısı en çok STREAM_LIFE yaşar (EventSource kendiliğinden yeniden bağlanır, `seq` ile kaldığı yerden).
    Açık akış uvicorn'un kapanışını en çok STREAM_LIFE bekletir (yerelde `--timeout-graceful-shutdown 5`); Railway
    boş HTTP'yi 5 dk'da keser → HEARTBEAT. Eşzamanlı akış MAX_STREAMS ile sınırlı (tek process: WS kollektörü ve
    Telegram aynı döngüde). Sunucu yeniden başlarsa istemcinin `seq`'i yeni sayaçtan büyük kalır → sıfırlanır (rota).
  • Canlı fiyat yalnız TAZE son işlemden (`fresh_px`, PX_MAX_AGE): dinlenmeyen ya da akışı kopuk coinde LAST donar,
    bayat fiyat taze mark'ın önüne geçip son muma sahte fitil çiziyordu (inceleme 18.10).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

log = logging.getLogger("radar.livefeed")

WATCH_TTL = 900              # sayfa bu kadar sn içinde açıldıysa coinin işlem halkası tutulur
RING = 300                   # izlenen coin başına son işlem
MAX_STREAMS = 24             # eşzamanlı SSE akışı (fazlası 503: istemci yoklamaya düşmez, yeniden dener)
STREAM_LIFE = 240            # tek akışın ömrü (sn); istemci `seq` ile kaldığı yerden yeniden bağlanır
TICK = 0.3                   # akış döngüsü (sn) — işlemler en çok bu gecikmeyle düşer
STATS_TTL = 5                # 15 dk toplamları (coin başına önbellek)
DEPTH_TTL = 4                # canlı defter (coin başına önbellek)
DEPTH_TIMEOUT = 4.0
HEARTBEAT = 15               # veri yokken yorum satırı (proxy bağlantıyı kesmesin)
DEPTH_LEVELS = 12            # yön başına gönderilen defter kademesi
PX_MAX_AGE = 120             # son işlem fiyatı bundan eskiyse canlı fiyat sayılmaz (sayfa mark'a düşer)

WATCH: dict[str, float] = {}
BUF: dict[str, deque] = {}
LAST: dict[str, tuple[int, float]] = {}          # coin → (ts_ms, px): son işlem (dinlenen her coin)
SEQ = [0]
STATS: dict = {"streams": 0, "opened": 0, "rejected": 0, "pushed": 0, "depth_req": 0, "depth_err": 0, "errors": 0}
_SUMM: dict[str, tuple[float, dict]] = {}
_DEPTH: dict[str, tuple[float, dict]] = {}
_DEPTH_TASK: dict[str, asyncio.Task] = {}


def watch(coin: str) -> None:
    """Sayfa açıldı: bu coinin işlem halkası tutulmaya başlar (ya da süresi uzar)."""
    if not coin:
        return
    WATCH[coin] = time.time()
    if coin not in BUF:
        BUF[coin] = deque(maxlen=RING)
    prune()


def prune(t: float | None = None) -> None:
    t = time.time() if t is None else t
    for c in [c for c, ts in WATCH.items() if t - ts > WATCH_TTL]:
        WATCH.pop(c, None)
        BUF.pop(c, None)
        _SUMM.pop(c, None)
        _DEPTH.pop(c, None)


def push(coin: str, ts_ms: int, px: float, sz: float, aggr: str, buyer: str, seller: str, twap: bool) -> None:
    """Kollektörün sıcak yolu (tape.observe işlemi saydıysa). Senkron, I/O yok."""
    LAST[coin] = (int(ts_ms), float(px))
    q = BUF.get(coin)
    if q is None:
        return
    SEQ[0] += 1
    taker = buyer if aggr == "B" else (seller if aggr == "A" else "")
    maker = seller if aggr == "B" else (buyer if aggr == "A" else "")
    q.append((SEQ[0], int(ts_ms), float(px), float(sz), aggr, taker, maker, 1 if twap else 0))
    STATS["pushed"] += 1


def fresh_px(coin: str, listened=None, max_age: int = PX_MAX_AGE) -> tuple[float | None, int | None]:
    """(fiyat, ts_ms) — son işlem PX_MAX_AGE'den tazeyse ve coin dinleniyorsa; değilse (None, None)."""
    last = LAST.get(coin)
    if listened is False or not last or time.time() * 1000 - last[0] > max_age * 1000:
        return None, None
    return last[1], last[0]


def since(coin: str, seq: int, limit: int = 120) -> list[tuple]:
    """`seq`'ten sonraki işlemler (eskiden yeniye), en çok `limit` (en yeniler)."""
    q = BUF.get(coin)
    if not q or q[-1][0] <= seq:
        return []
    out = [r for r in q if r[0] > seq]
    return out[-limit:]


def last_seq(coin: str) -> int:
    q = BUF.get(coin)
    return q[-1][0] if q else 0


def row(r: tuple) -> list:
    """İşlem satırı (JSON): [seq, ts_ms, px, $, yön 'B'/'A', piyasadan, pasif, twap]."""
    seq, ts_ms, px, sz, aggr, taker, maker, twap = r
    return [seq, ts_ms, px, round(px * sz, 2), aggr, taker, maker, twap]


def stats(coin: str) -> dict:
    """15 dk toplamları + kapsama (tape.summary, coin başına STATS_TTL önbellek)."""
    from . import tape
    hit = _SUMM.get(coin)
    t = time.time()
    if hit and t - hit[0] < STATS_TTL:
        return hit[1]
    s = tape.summary(coin, top=0, top_addr=8)
    cv = tape.cover(s)
    pick = lambda fs: [[f["addr"], round(f["buy"]), round(f["sell"]), round(f["net"]),   # noqa: E731
                        round(f["taker"] / (f["buy"] + f["sell"]) * 100) if (f["buy"] + f["sell"]) else 0,
                        1 if f["twap"] else 0] for f in fs]
    out = {"n": s["n"], "buy": round(s["buy"]), "sell": round(s["sell"]), "net": round(s["net"]),
           "n_addr": s["n_addr"], "twap": round(s["twap"]), "label": cv["label"], "warn": cv["warn"],
           "unknown": cv["unknown"], "buyers": pick(s["buyers"]), "sellers": pick(s["sellers"])}
    _SUMM[coin] = (t, out)
    return out


def _levels(book) -> tuple[list, list]:
    lv = (book or {}).get("levels") if isinstance(book, dict) else None
    if not isinstance(lv, list) or len(lv) != 2:
        return [], []
    out = []
    for side in lv:
        rows = []
        for x in (side or [])[:DEPTH_LEVELS]:
            try:
                rows.append([float(x["px"]), float(x["sz"]), int(x.get("n") or 0)])
            except (KeyError, TypeError, ValueError):
                continue
        out.append(rows)
    return out[0], out[1]


async def _fetch_depth(client, coin: str) -> None:
    STATS["depth_req"] += 1
    try:
        book = await asyncio.wait_for(client.info({"type": "l2Book", "coin": coin}, retries=1, priority="low"),
                                      DEPTH_TIMEOUT)
        bids, asks = _levels(book)
        if bids or asks:
            _DEPTH[coin] = (time.time(), {"bids": bids, "asks": asks, "ts": int(time.time())})
        else:
            STATS["depth_err"] += 1
    except Exception as e:                              # noqa: BLE001
        STATS["depth_err"] += 1
        log.debug("canlı defter %s: %s", coin, e)
    finally:
        _DEPTH_TASK.pop(coin, None)


def depth(client, coin: str) -> dict | None:
    """Önbellekteki defter (hemen döner); bayatsa arka planda tek istek başlatır. Akış döngüsü HL'yi beklemez."""
    hit = _DEPTH.get(coin)
    t = time.time()
    if client is not None and (not hit or t - hit[0] >= DEPTH_TTL) and coin not in _DEPTH_TASK:
        if float(getattr(client, "low_paused", lambda: 0.0)() or 0) <= 0:
            _DEPTH_TASK[coin] = asyncio.ensure_future(_fetch_depth(client, coin))
    return hit[1] if hit and t - hit[0] < 60 else None


def owners(book: dict | None) -> dict | None:
    """bookowners sonucunun akışa giden kısa hâli: en kalın kademeler + sahipler (✅ / dolan / emri yok)."""
    if not book:
        return None
    lv = []
    for view in ("grouped", "raw"):
        for side in ("bid", "ask"):
            for x in (book.get(view) or {}).get(side) or []:
                lv.append({"v": view[0], "s": side, "px": x["px"], "lo": x.get("lo", x["px"]), "hi": x.get("hi", x["px"]),
                           "usd": round(x["usd"]), "n": x.get("n") or 0,
                           "own": [[a, round(u)] for a, u in (x.get("owners") or [])[:3]],
                           "mk": [[a, round(u)] for a, u in (x.get("makers") or [])[:3]],
                           "gone": [[a, round(u)] for a, u in (x.get("makers_gone") or [])[:3]],
                           "unk": round(x.get("unknown_usd") or 0)})
    return {"ts": book.get("ts"), "lv": lv, "unknown": bool(book.get("owners_unknown")), "cands": book.get("cands", 0),
            "failed": book.get("failed", 0), "unit": book.get("unit_txt") or "", "err": book.get("err") or []}


def diag_line() -> str:
    s = STATS
    return (f"📟 canlı sayfa akışı: {s['streams']} açık akış · {s['opened']} açıldı (açılıştan beri)"
            + (f" · ⚠️ {s['rejected']} reddedildi (tavan {MAX_STREAMS})" if s["rejected"] else "")
            + f" · {len(WATCH)} coin izleniyor · {s['pushed']:,} işlem yayıldı"
            + f" · defter {s['depth_req']} istek" + (f" ({s['depth_err']} okunamadı)" if s["depth_err"] else "")
            + (f" · ⚠️ {s['errors']} hata" if s["errors"] else ""))
