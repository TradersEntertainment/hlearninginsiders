"""⚡ Hızlı TWAP — HL TWAP emrini İLK dilimde yakalar.

Kullanıcı (12.10): "bazı TWAP'lar 5 dk'lık oluyor ve coini çok sert oynatıyor; 1. dakikada en geç
yakalama şansımız var mı?" Eski yol (twaplive) 10 dilim + 5 dk bekliyordu: 5 dk'lık TWAP o eşiğe
ancak bittiğinde varır ve hiç bildirilmezdi.

Gerçekler (HL dokümanı + canlı kontrol, 12.10):
  • yerel TWAP alt emri ~30 sn'de bir; alt emir sayısı 2 × dakika + 1 (5 dk = 11 dilim); en kısa 5 dk
  • ilk dilim emirden ~1-2 sn sonra basılır; dilim ≈ toplam / (2m + 1) (randomize ±%20)
  • işlem akışında (WS trades) TWAP dilimi SIFIR HASH'le gelir; emrin sahibi TAKER'dır
    (side 'B' → users[0], 'A' → users[1]); dilim birkaç fiyat seviyesine bölünürse aynı `time`
  • "tüm TWAP'lar" diye genel bir akış YOK; adres başına userTwapHistory tek sorguda toplam, süre,
    başlangıç ve durumu verir — 'activated' kaydı ilk dilimden 1-2 sn ÖNCE yazılır

Akış: collector (sıcak yol, G/Ç yok) sıfır hash'li dilimi taker adına biriktirir; dilim ≥ taban →
sıra. Bu döngü öncelik sırasıyla (dilimin dakika hızı ÷ coinin ortalama dakika hacmi), dakika tavanı
ve en çok 3 eşzamanlı sorguyla emri okur → kapı → bildirim (ana grup + ayna grubu). Kapı:
  1) eski kapı (twaplive.order_gate: plan ≥ $1M · kalan · 24s hacmin %5'i, BTC/ETH %20) — aynı eşik,
     yalnız saniyeler içinde
  2) kısa TWAP (≤ twap_fast_short_max_min dk): plan ≥ twap_fast_short_min_usd VE emrin dakika hızı ≥
     coinin 24s dakika ortalamasının twap_fast_rate_x katı VE planın ≥ %30'u kalmış
TAHMİN YOK: emir HL'den okunamazsa bildirim yok (sıfır hash likidasyonda da görülebilir → emir yoksa düşer).

Eski yol aynen çalışır: 🧪 LOG-TWAP kaydı onun kendi anında, kendi emir sorgusuyla yapılır — bu modül
eski yolun emir önbelleğine ve 'twap' bekleme işaretine dokunmaz. Hızlı bildirilen tur eski yola aday
olunca tekrar mesaj gitmez (twaplive: run.fast_ts); kısa TWAP'ın bitiş notunu twaplive._fast_end yazar.
Hızlı karar ayrı laboratuvar ailesi: LOG-TWAPF (geçen + kalan).
"""
import asyncio
import logging
from collections import deque

from ..db import alert_log, alert_recent, kv_get, kv_set, now
from . import twaplive

log = logging.getLogger("radar.twapfast")

STATS_KV = "twapfast_stats"
COALESCE_SEC = 2            # aynı dilimin parçaları (alt emir defterde birkaç seviyeye bölünür)
QUEUE_MAX_AGE = 150         # sırada bu kadar bekleyen dilim düşer (eski yol 5 dk sonra yine bakar)
PENDING_MAX = 20000
PENDING_TTL = 900
CONCURRENCY = 3             # HL: IP başına kullanıcıya özel WS aboneliği en çok 10 farklı kullanıcı
RETRY_NO_ORDER = 4          # emir geçmişte yok → bir kez daha (snapshot gecikmesi)
RETRY_FAIL = 30             # sorgu başarısız → bu kadar sonra yeniden sıraya girebilir
NO_ORDER_IGNORE = 300       # emir bulunmadı (likidasyon/ADL) → bu adres+coin+yön bu kadar yok sayılır
SEND_RETRY = 5
SHORT_LEFT_MIN = 0.3        # kısa TWAP: planın en az bu payı kalmış olmalı
POS_TIMEOUT = 2.5           # pozisyon satırı bildirimi bundan fazla geciktirmez
WS_CEILING = 6              # collector'da bekleyen emir sorgusu bundan azsa başlat (IP başına 10 kullanıcı sınırı)
PRE_SLACK = 0.7             # ilk dilim nominalin 0.8-1.2'si (randomize): ön süzgeç bu payla gevşek
LONG_HORIZON_MIN = 1440     # uzun TWAP ön süzgeci: en çok 24 saatlik emir varsayılır (daha uzunu eski yola kalır)
FAST_CD = "twap_fastcd"     # hızlı yolun bekleme işareti (eski yolun 'twap' işaretinden AYRI)
REASON_TR = {**twaplive.REASON_TR, "short": "kısa TWAP · dakika hızı", "slow": "dakika hızı düşük",
             "short_small": "kısa TWAP eşik altı", "short_left": "kısa TWAP: kalan az",
             "lookup_fail": "emir sorgusu başarısız", "no_order": "HL'de aktif TWAP emri yok",
             "cooldown": "bekleme içinde", "failed": "gönderilemedi", "alerted": "bildirildi"}


def is_twap_hash(h) -> bool:
    """HL TWAP alt emrinin dolumu: hash = '0x' + 64 sıfır."""
    return isinstance(h, str) and len(h) > 2 and h[:2] == "0x" and not h[2:].strip("0")


class Fast:
    def __init__(self):
        self.pending: dict[tuple, dict] = {}
        self.known: dict[tuple, int] = {}       # key → bu ana kadar dilimleri yok say (emir okundu / yok)
        self.inflight: set[tuple] = set()
        self.event: asyncio.Event | None = None
        self.lookup_ts: deque = deque()         # son 60 sn'nin sorgu anları (dakika tavanı)
        self.recent: deque = deque(maxlen=20)   # son kararlar (/twap, /tani)
        self.lat: deque = deque(maxlen=50)      # emir başlangıcı → bildirim (sn)
        self.stats: dict = {}
        self.vols: dict = {}                    # coin → (24s hacim, ts): ön süzgeç (döngü tazeler; G/Ç yok)
        self.cfg = None
        self.clear_stats()

    def clear_stats(self) -> None:
        self.stats = {k: 0 for k in ("zero", "slices", "queued", "known_skip", "dropped_old", "capped", "lookups",
                                     "lookup_fail", "no_order", "mm", "alerts", "failed", "cooldown", "errors",
                                     "pre_small", "pre_novol", "pre_low", "pre_spot_thin", "notify_off", "no_ws")}

    def clear(self) -> None:
        self.pending.clear()
        self.known.clear()
        self.inflight.clear()
        self.lookup_ts.clear()
        self.recent.clear()
        self.lat.clear()
        self.clear_stats()

    def observe(self, coin: str, addr: str, side: str, px: float, sz: float, notional: float, ts: int,
                min_usd: float = 1000.0) -> None:
        """Sıcak yol (collector): sıfır hash'li dolum, emrin sahibi (taker) adına. Await yok, G/Ç yok."""
        st = self.stats
        st["zero"] += 1
        ts = int(ts)
        key = (coin, addr, side)
        until = self.known.get(key)
        if until is not None:
            if ts <= until:
                st["known_skip"] += 1
                return
            self.known.pop(key, None)
        p = self.pending.get(key)
        if p is None:
            if len(self.pending) >= PENDING_MAX:
                self._evict(ts)
            p = self.pending[key] = {"coin": coin, "addr": addr, "side": side, "t0": ts, "ts": ts, "usd": 0.0,
                                     "sz": 0.0, "px": float(px), "n": 0, "max": 0.0, "queued": 0, "tries": 0,
                                     "not_before": 0}
        if p["n"] == 0 or ts - p["ts"] > COALESCE_SEC:
            p["n"] += 1
            p["ts"], p["usd"], p["sz"] = ts, 0.0, 0.0
            st["slices"] += 1
        p["usd"] += float(notional)
        p["sz"] += float(sz)
        p["px"] = float(px)
        p["max"] = max(p["max"], p["usd"])
        if p["queued"] or p["usd"] < float(min_usd or 0) or p.get("pre_ts") == ts:
            return
        if self.cfg is not None:
            why = prefilter(self.cfg, coin, p["usd"], self.vols.get(coin), ts)
            if why:
                p["pre_ts"] = ts                        # bu dilim için bir kez say
                st[why] = st.get(why, 0) + 1
                return
        p["queued"] = now()                             # varış anı (işlem damgası değil: saat farkı sırayı bozmasın)
        st["queued"] += 1
        if self.event is not None:
            self.event.set()

    def _evict(self, ref_ts: int) -> None:
        self.prune(ref_ts)
        if len(self.pending) < PENDING_MAX:
            return
        for k in sorted((k for k, p in self.pending.items() if not p["queued"]),
                        key=lambda k: self.pending[k]["ts"])[: max(1, PENDING_MAX // 10)]:
            self.pending.pop(k, None)

    def prune(self, ref_ts: int) -> None:
        for k in [k for k, p in self.pending.items() if not p["queued"] and ref_ts - p["ts"] > PENDING_TTL]:
            self.pending.pop(k, None)
        for k in [k for k, u in self.known.items() if u < ref_ts]:
            self.known.pop(k, None)

    def note(self, p: dict, reason: str, **kw) -> None:
        self.recent.appendleft({"coin": p["coin"], "addr": p["addr"], "side": p["side"], "reason": reason,
                                "slice": round(p.get("max") or 0), "ts": now(), **kw})

    def snapshot(self, t: int) -> dict:
        lat = sorted(self.lat)
        return {**self.stats, "pending": len(self.pending), "known": len(self.known),
                "inflight": len(self.inflight), "lat_med": lat[len(lat) // 2] if lat else None,
                "lat_max": lat[-1] if lat else None, "recent": list(self.recent)[:10],
                "mirror": dict(twaplive.MIRROR), "ts": t}


F = Fast()


def observe(coin: str, addr: str, side: str, px: float, sz: float, notional: float, ts: int,
            min_usd: float = 1000.0) -> None:
    """Collector kancası (bkz. app/hl/collector.py::_handle)."""
    F.observe(coin, addr, side, px, sz, notional, ts, min_usd)


# ---------------- saf kapı ----------------

def prefilter(cfg, coin: str, slice_usd: float, vol, now_ts: int) -> str:
    """Sorgudan ÖNCE (sıcak yol, G/Ç yok): bu dilim iki kapıdan birini geçebilecek bir emirden mi? '' = sor.
    Dilim ≈ plan / (2m+1) (randomize ±%20 → 0.8 payı): plan ≤ dilim·(2m+1)/0.8; dakika hızı ≈ 2·dilim.
    Hacim bilinmiyorsa ikisi de geçemez (kapı hacimsiz bildirmez) — hacimden bağımsız eşik açıksa hariç."""
    from .. import assets
    big = float(getattr(cfg, "twap_alert_big_usd", 0) or 0)
    if not vol or not vol[0] or float(vol[0]) <= 0 or not vol[1] or now_ts - int(vol[1]) > twaplive.VOL_MAX_AGE:
        if big > 0 and slice_usd * (2 * LONG_HORIZON_MIN + 1) / 0.8 >= big:
            return ""
        return "pre_novol"
    dv = float(vol[0])
    pre_x = 2.0 * slice_usd / (dv / 1440.0)
    short_max = float(getattr(cfg, "twap_fast_short_max_min", 30) or 0)
    short_min = float(getattr(cfg, "twap_fast_short_min_usd", 250_000) or 0)
    rx_min = float(getattr(cfg, "twap_fast_rate_x", 2.0) or 0)
    spot_min = float(getattr(cfg, "spot_twap_min_day_vol", 0) or 0)
    if spot_min > 0 and assets.is_spot(coin) and dv < spot_min:
        return "pre_spot_thin"
    if short_max > 0 and pre_x >= rx_min * PRE_SLACK and slice_usd * (2 * short_max + 1) / 0.8 >= short_min:
        return ""
    need = max(float(getattr(cfg, "twap_alert_min_usd", 1_000_000) or 0), dv * twaplive.vol_pct_for(cfg, coin) / 100)
    if big > 0:
        need = min(need, big)
    if slice_usd * (2 * LONG_HORIZON_MIN + 1) / 0.8 >= need:
        return ""
    return "pre_low"


def rate_x(order: dict, day_vol, vol_ts, now_ts: int) -> tuple[float | None, float | None]:
    """(dakika hızı $, coinin 24s dakika ortalamasına oranı). Hacim bayat/yoksa oran None."""
    minutes = float(order.get("minutes") or 0)
    plan = float(order.get("planned_usd") or 0)
    per_min = plan / minutes if minutes > 0 else None
    if not per_min or not day_vol or float(day_vol) <= 0 or not vol_ts \
            or now_ts - int(vol_ts) > twaplive.VOL_MAX_AGE:
        return per_min, None
    return per_min, per_min / (float(day_vol) / 1440.0)


def short_gate(cfg, order: dict, rx: float | None) -> str:
    """Kısa TWAP kuralı: 'short' geçer; diğerleri neden. Durum/hacim kontrolleri order_gate'te yapılmış olur."""
    minutes = float(order.get("minutes") or 0)
    if not minutes or minutes > float(getattr(cfg, "twap_fast_short_max_min", 30) or 0):
        return ""                                       # kısa değil → eski kapının nedeni geçerli
    plan = float(order.get("planned_usd") or 0)
    if plan < float(getattr(cfg, "twap_fast_short_min_usd", 250_000) or 0):
        return "short_small"
    if rx is None:
        return "no_vol"
    if rx < float(getattr(cfg, "twap_fast_rate_x", 2.0) or 0):
        return "slow"
    if float(order.get("remaining_usd") or 0) < plan * SHORT_LEFT_MIN:
        return "short_left"
    return "short"


def decide_gate(cfg, coin: str, order: dict, day_vol, vol_ts, now_ts: int) -> tuple[str, dict]:
    """(karar, ayrıntı). 'ok' | 'big' | 'short' bildirilir. Spot ince çift ve bitmiş emir kısa yoldan da geçemez."""
    det = twaplive.gate_detail(cfg, coin, order, day_vol, vol_ts, now_ts)
    per_min, rx = rate_x(order, day_vol, vol_ts, now_ts)
    det.update(per_min=per_min, rate_x=rx, minutes=order.get("minutes"))
    g = det["reason"]
    if g in ("ok", "big") or g in ("no_order", "order_done", "spot_thin"):
        return g, det
    sg = short_gate(cfg, order, rx)
    if sg == "short":
        return "short", det
    return (sg or g), det


def pick_order(hist, coin: str, side: str, p: dict, now_ts: int) -> dict | None:
    """Dilimin emri: aktif, bu coin + yön, başlangıcı dilimden önce (+5 sn pay), bitişi dilimden sonra."""
    for o in twaplive.parse_twap_orders(hist, coin, side, p.get("px"), now_ts):
        if o.get("status") != "activated" or not o.get("started_ts"):
            continue
        t0, mins = int(o["started_ts"]), float(o.get("minutes") or 0)
        if t0 <= int(p["ts"]) + 5 and t0 + mins * 60 >= int(p["ts"]) - 60:
            return o
    return None


def priority(p: dict, vols: dict) -> float:
    """Sorgu önceliği: dilimin dakika hızı (2 × dilim) ÷ coinin ortalama dakika hacmi; hacim yoksa dilim $."""
    v = vols.get(p["coin"])
    if v and v[0]:
        return 2.0 * float(p.get("max") or 0) / (float(v[0]) / 1440.0)
    return float(p.get("max") or 0) / 1e4


# ---------------- 🧪 laboratuvar ----------------

def _lab_fast(p: dict, order: dict, det: dict, gate: str, ts: int) -> bool:
    """🧪 LOG-TWAPF: ilk dilimde doğrulanan emir, kapı sonucu ne olursa olsun (kapıdan kalan = kontrol grubu).
    Senkron, G/Ç yok, asla fırlatmaz."""
    try:
        from ..lab.registry import emit
        return emit("LOG-TWAPF", p["coin"], ts, side=1 if p["side"] == "buy" else -1, px_ref=p.get("px"),
                    trig_key=f"{p['addr']}:{int(order.get('started_ts') or 0)}",
                    features={"plan": round(float(order.get("planned_usd") or 0)),
                              "left": order.get("remaining_usd"), "dk": order.get("minutes"),
                              "vol_pct": det.get("vol_pct"), "rate_x": det.get("rate_x"),
                              "kapi": gate, "gec": ts - int(order.get("started_ts") or ts),
                              "dilim": round(float(p.get("max") or 0))},
                    gated=gate in ("ok", "big", "short"))
    except Exception:                                  # noqa: BLE001
        return False


# ---------------- sorgu + karar ----------------

async def _pos(client, coin: str, addr: str):
    try:
        return await asyncio.wait_for(twaplive.position_ctx(client, coin, addr), POS_TIMEOUT)
    except Exception:                                  # noqa: BLE001 — zaman aşımı: satır "bilinmiyor"
        return None


async def resolve(cfg, client, notifier, collector, key: tuple, p: dict, vols: dict) -> str:
    """Tek dilim → emir sorgusu → kapı → bildirim. Dönüş: karar nedeni."""
    coin, addr, side = key
    F.stats["lookups"] += 1
    ents = await twaplive.entities([addr])
    if ents.get(addr) in ("mm", "vault"):
        F.stats["mm"] += 1
        F.known[key] = now() + 3600
        F.pending.pop(key, None)
        F.note(p, "mm")
        return "mm"
    fn = getattr(collector, "fetch_twap_history", None) if collector is not None else None
    hist = None
    if fn is not None:
        try:
            hist = await fn(addr)
        except Exception as e:                         # noqa: BLE001
            log.debug("hızlı twap sorgusu %s: %s", addr[:10], e)
    t = now()
    if hist is None:
        F.stats["lookup_fail"] += 1
        p["queued"], p["not_before"] = 0, t + RETRY_FAIL
        F.note(p, "lookup_fail")
        return "lookup_fail"
    order = pick_order(hist, coin, side, p, t)
    if order is None:
        p["tries"] += 1
        if p["tries"] < 2:
            p["not_before"] = t + RETRY_NO_ORDER        # sırada kalır, bir kez daha
            return "retry"
        F.stats["no_order"] += 1
        F.known[key] = t + NO_ORDER_IGNORE
        F.pending.pop(key, None)
        F.note(p, "no_order")
        return "no_order"
    F.known[key] = int(order["started_ts"]) + int(float(order.get("minutes") or 0) * 60) + 120
    F.pending.pop(key, None)
    return await _decide(cfg, client, notifier, key, p, order, vols, t, ents.get(addr) or "")


async def _decide(cfg, client, notifier, key: tuple, p: dict, order: dict, vols: dict, t: int,
                  entity: str = "") -> str:
    from ..notify import in_quiet_hours
    from ..telegram import format as fmt
    coin, addr, side = key
    vol = vols.get(coin)
    day_vol, vol_ts = vol if vol else (None, None)
    g, det = decide_gate(cfg, coin, order, day_vol, vol_ts, t)
    _lab_fast(p, order, det, g, t)
    started = int(order.get("started_ts") or 0)
    info = {"planned": round(float(order.get("planned_usd") or 0)), "minutes": order.get("minutes"),
            "rate_x": det.get("rate_x"), "vol_pct": det.get("vol_pct"), "lag": t - started if started else None}
    if g not in ("ok", "big", "short"):
        F.stats[g] = F.stats.get(g, 0) + 1
        F.note(p, g, **info)
        return g
    ckey = f"{coin}:{addr}:{side}"
    fkey = f"{ckey}:{started}"
    cool = int(getattr(cfg, "twap_alert_cooldown", 21600) or 0)
    if await alert_recent("twap_fast", fkey, 86400) or (cool and (
            await alert_recent("twap", ckey, cool) or await alert_recent(FAST_CD, ckey, cool))):
        F.stats["cooldown"] += 1
        F.note(p, "cooldown", **info)
        return "cooldown"
    from ..notify import kind_enabled
    if not kind_enabled(cfg, "twap"):
        F.stats["notify_off"] += 1
        F.note(p, "notify_off", **info)
        return "notify_off"
    run = twaplive.REG.runs.get(key)
    if run is None:                                     # eski yolun sayacı budadıysa: bitiş notu için yeniden
        run = twaplive.REG.runs[key] = twaplive.Run(coin, addr, side, int(p["t0"]), float(p.get("px") or 0))
    # yalnız hızlı alanlar — eski yolun order/lookup_ts/alerted_ts'ine DOKUNULMAZ (🧪 LOG-TWAP aynı kalsın)
    run.fast_order, run.fast_lookup_ts = order, t
    pos = await _pos(client, coin, addr)
    offer = None
    if getattr(cfg, "twap_follow_enabled", True):
        try:
            offer = await twaplive._twap_offer(run)
        except Exception:                              # noqa: BLE001
            log.debug("hızlı twap takip teklifi yazılamadı (%s)", coin, exc_info=True)
    ctx = {"coin": coin, "address": addr, "side": side, "gate": g, "day_vol": day_vol,
           "vol_pct": det.get("vol_pct"), "per_min": det.get("per_min"), "rate_x": det.get("rate_x"),
           "slice_usd": p.get("max"), "slice_px": p.get("px"), "pos": pos, "klass": twaplive.klass_of(coin),
           "entity": entity, "offer": offer, "now": now()}
    text = fmt.twap_fast(order, ctx)
    chat, can = twaplive.chat_for(cfg, coin)
    ok = False
    if can:
        ok = await notifier.send("twap", text, priority="high", key=f"twap_fast:{fkey}", chat_id=chat, coin=coin)
        if not ok and chat:                             # kripto kanalı: bir kez daha (sessiz saat yalnız ana sohbette)
            await asyncio.sleep(SEND_RETRY)
            ok = await notifier.send("twap", text, priority="high", key=f"twap_fast:{fkey}", chat_id=chat,
                                     coin=coin)
    mok = await twaplive.mirror(cfg, notifier, text, f"fast:{fkey}", coin, primary=chat if can else None)
    deferred = can and not chat and not ok and in_quiet_hours(cfg)   # hisse yolu sessiz saatte özete düştü
    if not (ok or mok or deferred):
        F.stats["failed"] += 1
        F.note(p, "failed", **info)
        await alert_log("fail:twap_fast", fkey, text)
        return "failed"
    await alert_log("twap_fast", fkey, text)
    if cool:
        await alert_log(FAST_CD, ckey, "")
    run.fast_ts, run.fast_started = t, started
    F.stats["alerts"] += 1
    if started:
        F.lat.append(now() - started)
    F.note(p, "alerted", **info)
    try:
        await twaplive.persist(run, None, order=order)
    except Exception:                                  # noqa: BLE001
        log.debug("hızlı twap kaydı yazılamadı", exc_info=True)
    log.info("hızlı twap: %s %s %s emir %.0f$ %s dk (%s) emirden %s sn sonra bildirildi", coin, side, addr[:10],
             float(order.get("planned_usd") or 0), order.get("minutes"), g, info["lag"])
    return "alerted"


async def dispatch(cfg, client, notifier, collector, t: int, tasks: set) -> int:
    """Sıradaki dilimler → öncelik sırasıyla sorgu (eşzamanlı ≤ CONCURRENCY, dakikada ≤ tavan). Başlatılan sayı."""
    while F.lookup_ts and t - F.lookup_ts[0] >= 60:
        F.lookup_ts.popleft()
    ready = []
    for key, p in list(F.pending.items()):
        if not p["queued"] or key in F.inflight:
            continue
        if t - int(p["queued"]) > QUEUE_MAX_AGE:
            p["queued"] = 0
            F.stats["dropped_old"] += 1
            continue
        if p["not_before"] > t:
            continue
        ready.append((key, p))
    if not ready:
        return 0
    vols = F.vols or await twaplive.volumes_cached(None)
    ready.sort(key=lambda kp: -priority(kp[1], vols))
    cap = int(getattr(cfg, "twap_fast_lookups_min", 30) or 0)
    started = 0
    for key, p in ready:
        if len(F.inflight) >= CONCURRENCY:
            break
        if cap > 0 and len(F.lookup_ts) >= cap:
            F.stats["capped"] += 1
            break
        waiters = getattr(collector, "_twap_waiters", None)
        if isinstance(waiters, dict) and len(waiters) >= WS_CEILING:
            F.stats["no_ws"] += 1                      # eski yol / takip / komut sorguları önce (sınır paylaşılıyor)
            break
        F.inflight.add(key)
        F.lookup_ts.append(t)

        async def _one(key=key, p=p):
            try:
                await resolve(cfg, client, notifier, collector, key, p, vols)
            except asyncio.CancelledError:
                raise
            except Exception:
                F.stats["errors"] += 1
                log.exception("hızlı twap kararı %s", key)
            finally:
                F.inflight.discard(key)
                if F.event is not None:
                    F.event.set()
        task = asyncio.create_task(_one())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        started += 1
    return started


async def loop(cfg, client, notifier, collector=None) -> None:
    """Denetimli döngü: sıraya dilim düştükçe uyanır (en geç 1 sn). İstatistik 30 sn'de bir kv'ye."""
    from ..health import beat
    F.event = asyncio.Event()
    F.cfg = cfg
    tasks: set = set()
    last = vol_last = 0
    await asyncio.sleep(20)
    while True:
        try:
            await asyncio.wait_for(F.event.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        F.event.clear()
        try:
            t = now()
            if t - vol_last >= 60:
                vol_last = t
                F.vols = await twaplive.volumes_cached(None)
            if getattr(cfg, "twap_fast_enabled", True) and getattr(cfg, "twap_live_enabled", True):
                await dispatch(cfg, client, notifier, collector, t, tasks)
            if t - last >= 30:
                last = t
                F.prune(t)
                await beat("twapfast")
                await kv_set(STATS_KV, F.snapshot(t))
        except asyncio.CancelledError:
            raise
        except Exception as e:                         # noqa: BLE001
            log.exception("hızlı twap turu hatası")
            await kv_set(STATS_KV, {"error": f"{type(e).__name__}: {e}"[:200], "ts": now()})


async def diag_line() -> str:
    """/tani ve /twap için tek satır."""
    st = await kv_get(STATS_KV) or {}
    if not st.get("ts"):
        return "⚡ hızlı twap: tur HENÜZ ÇALIŞMADI"
    if st.get("error"):
        return f"⚡ hızlı twap: ⚠️ {st['error']}"
    m = st.get("mirror") or {}
    parts = [f"{st.get('zero', 0)} sıfır hash'li dolum", f"{st.get('queued', 0)} dilim sıraya",
             f"{st.get('lookups', 0)} sorgu", f"{st.get('alerts', 0)} bildirim"]
    if st.get("lat_med") is not None:
        parts.append(f"emirden bildirime ortanca {st['lat_med']} sn (en çok {st.get('lat_max')} sn)")
    for k, lab in (("pre_low", "ön süzgeçte küçük"), ("pre_novol", "hacim bilinmiyor"),
                   ("no_order", "emir yok"), ("slow", "dakika hızı düşük"), ("vol_small", "hacme göre küçük"),
                   ("order_small", "eşik altı"), ("short_small", "kısa eşik altı"), ("cooldown", "bekleme"),
                   ("dropped_old", "sırada eskidi"), ("capped", "dakika tavanı"), ("no_ws", "soket dolu"),
                   ("mm", "mm/vault"), ("notify_off", "TWAP bildirimi kapalı")):
        if st.get(k):
            parts.append(f"{st[k]} {lab}")
    for k, lab in (("lookup_fail", "sorgu başarısız"), ("failed", "gönderilemedi"), ("errors", "hata")):
        if st.get(k):
            parts.append(f"⚠️ {st[k]} {lab}")
    if m.get("sent") or m.get("failed"):
        parts.append(f"ayna ✓{m.get('sent', 0)}" + (f" ⚠️✗{m['failed']}" if m.get("failed") else ""))
    return "⚡ hızlı twap: " + " · ".join(parts)


def recent_line(d: dict) -> str:
    """Hızlı yol kararı, düz metin: 'HYPE 0xab…cd buy 5 dk $1.2M emir · dakika hacminin 6.1 katı → bildirildi'."""
    from ..telegram.format import short, usd
    from .. import assets
    txt = f"{assets.label(d.get('coin') or '')} {short(d.get('addr') or '')} {d.get('side')}"
    if d.get("planned"):
        txt += f" {float(d.get('minutes') or 0):g} dk {usd(d['planned'])} emir"
        if d.get("rate_x") is not None:
            txt += f" · dakika hacminin {float(d['rate_x']):.1f} katı"
        if d.get("lag") is not None:
            txt += f" · emirden {d['lag']} sn sonra"
    else:
        txt += f" dilim {usd(d.get('slice') or 0)}"
    r = d.get("reason") or "?"
    return txt + f" → {REASON_TR.get(r, r)}"
