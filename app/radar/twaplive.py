"""Canlı TWAP radarı — WS akışındaki HER işlemi adres bazında sayar ve coinin
24 saatlik hacmine göre BÜYÜK olan düzenli dilimlemeyi Telegram'a yazar.
Ekstra HL isteği yok; her şey collector'ın zaten aldığı trade'lerden.

Neden ayrı bir sayaç: HL'nin yerel TWAP emri 30 sn'de bir dilim atar. $1.6M /
6 saat ≈ 720 × $2.2K dilim; kripto yakalama tabanı ($5K) bunları `fills`'e hiç
yazmaz, arşiv radarı (`twap.py`) kördür. Collector ise abone olduğu her coinin
her trade'ini alıcı/satıcı adresiyle alıyor — burada tabanın ALTINDAKİ
dilimler de sayılır ("INJ'e $2M TWAP" tam bu).

Dilim birleştirme: HL alt-emri IOC'dir, ince defterde 3-8 karşı tarafa bölünür
→ aynı saniyede birkaç trade. 2 sn içinde gelen parçalar tek dilim sayılır;
yoksa aralık 0 olur ve düzenlilik hiç tutmaz.

TAHMİN YOK (kullanıcı kuralı): düzenli dizi görülünce adresin HL TWAP EMRİ
WS'ten sorgulanır (`userTwapHistory` snapshot — hypurrscan'in Orders sekmesi):
planlanan boyut, dolan kısım, kalan süre. Kapı: emir ≥ twap_alert_min_usd ($1M)
VE kalan ≥ min(twap_alert_min_left_usd $1M, planın yarısı) VE emir 24s hacmin ≥
twap_alert_vol_pct'i (%5; BTC/ETH %20). Emir yok / bitmiş / iptal / hacim bilinmiyor →
bildirim YOK. "Bu hızla 24 saatte…" gibi ekstrapolasyon mesajlarda yer almaz.
Kripto → CRYPTO_CHAT_ID (boşsa gönderilmez), hisse/endeks → ana sohbet; her TWAP
mesajının kopyası TWAP_MIRROR_CHAT_ID'ye (mirror). Emir süresi ≤ twap_fast_short_max_min (1 sa) →
mesajlarda ⚡ HIZLI. 🔎 İlk dilimde yakalama ayrı modülde (twapfast.py); bu yolun kararına ve 🧪 LOG-TWAP
kaydına dokunmaz.
İlerleme notu emrin yarısı dolunca (tek sefer), bitiş/iptal notu emir
finished/terminated olunca — gerçek dolan tutarla.

Dedupe DB'de (`alerts_log`, restart'a dayanıklı); işaret GÖNDERİMDEN ÖNCE
yazılır — 60 sn'lik döngü + sessiz saat her dakika yeni özet kaydı üretmesin.
Bellek: tur başına son 64 dilim (düzenlilik onlardan ölçülür), toplamlar
skaler; boşta kalan diziler budanır; en çok MAX_KEYS dizi.
"""
import asyncio
import logging
import statistics
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .. import assets
from ..db import alert_log, alert_recent, db, kv_get, kv_set, now
from ..notify import in_quiet_hours
from . import twap as twapmod

log = logging.getLogger("radar.twaplive")

COALESCE_SEC = 2            # aynı dilimin parçaları (IOC alt-emir defterde bölünür)
KEEP_SLICES = 64            # düzenlilik son bu kadar dilimden ölçülür
IDLE_SEC = 1800             # dilim gelmeyen dizi bu kadar sonra düşer
IDLE_SHORT_SEC = 300        # 5 dilimden az olan (tekil işlem) çok daha çabuk düşer
MAX_KEYS = 20000            # bellek tavanı (BTC/ETH tek başına binlerce adres üretir)
MIN_DUR_SEC = 600           # bildirim için en az bu kadar sürmüş olmalı
CV_GAP_LIVE = 0.5           # HL "randomize" ve dolmayan dilimin taşınması: arşivden gevşek
CV_SIZE_LIVE = 0.6
END_MIN_SEC = 300           # bitiş: en az 5 dk VE 3 aralık dilim yok
END_GAPS = 3
PROGRESS_STEPS = (250e3, 500e3, 1e6, 2e6, 5e6, 10e6, 25e6, 50e6, 100e6)
PROGRESS_MIN_GAP = 1800     # tur başına ilerleme notu en çok 30 dk'da bir
VOL_MAX_AGE = 3600          # hacim ölçümü bundan eskiyse kapı geçmez (tahmin yok)
LOOKUP_MIN_DUR = 300        # sorgudan önce dizi en az 5 dk sürmüş olmalı
LOOKUP_MAX_PER_EVAL = 20    # bir turda en çok bu kadar WS sorgusu
REFRESH_SEC = 600           # bildirilmiş tur: emir 10 dk'da bir yeniden sorgulanır
QUICK_REFRESH = 60          # ⚡ HIZLI (≤ 1 sa) bildirilmiş emir: dakikada bir — iptal ⛔ kanala hızla yazılsın
HALF_PCT = 50               # ilerleme notu: emrin yarısı dolunca
NATIVE_GAP = (25, 35)       # ortanca aralık bu banttaysa "HL TWAP düzenine uyuyor"
STATS_KV = "twaplive_stats"
LAST_KV = "twaplive_last"   # son 50 aday kararı (tur tur ezilmez): "niye gelmedi" sorusunun kaydı
LAST_MAX = 50
DECISIONS_MAX = 30
REASON_TR = {"irregular": "düzensiz dizi", "mm": "mm/vault", "cooldown": "bekleme içinde",
             "no_lookup": "collector yok", "lookup_fail": "emir sorgusu başarısız",
             "no_order": "HL'de TWAP emri yok", "order_done": "emir bitmiş/iptal",
             "order_small": "emir eşik altı", "order_left": "kalan az", "no_vol": "hacim bilinmiyor",
             "spot_thin": "spot çifti ince (24s hacim taban altı)",
             "vol_small": "hacme göre küçük", "no_chat": "kanal yok", "alerted": "bildirildi",
             "failed": "gönderilemedi", "ok": "geçer", "big": "hacimden bağımsız büyük",
             "lookup_capped": "bu turun sorgu tavanı doldu (sonraki tur)"}


class Run:
    __slots__ = ("coin", "address", "side", "first_ts", "last_ts", "n", "total", "sz_total",
                 "pxsz", "px_first", "px_last", "tk_n", "known_n", "slices", "alerted_ts",
                 "alert_total", "progress_ts", "progress_step", "ended_ts", "day_volume",
                 "rate_day", "gate", "order", "lookup_ts", "half_ts", "nat_n", "nat_ts", "nat_first_ts", "nat_usd",
                 "nat_sz")

    def __init__(self, coin: str, address: str, side: str, ts: int, px: float):
        self.coin, self.address, self.side = coin, address, side
        self.first_ts = self.last_ts = int(ts)
        self.n = 0
        self.total = self.sz_total = self.pxsz = 0.0
        self.px_first = self.px_last = float(px)
        self.tk_n = self.known_n = 0
        self.slices: deque = deque(maxlen=KEEP_SLICES)   # (ts, sz, ntl, px)
        self.alerted_ts = None
        self.alert_total = 0.0
        self.progress_ts = None
        self.progress_step = 0.0
        self.ended_ts = None
        self.day_volume = None
        self.rate_day = None
        self.gate = ""
        self.order = None          # son sorgudaki HL TWAP emri (parse_twap_orders çıktısı)
        self.lookup_ts = None
        self.half_ts = None
        self.nat_n = 0             # HL TWAP alt emri dolumu (sıfır hash, taker) sayısı — 🕵️ gizli TWAP'ı dışlar
        self.nat_ts = 0            # son HL alt emri dolumunun anı (/twap: emir hâlâ dilim atıyor mu)
        self.nat_first_ts = 0      # bu HL alt emri "bölümünün" ilk dolumu (öncesi LIVE_SILENT'ten uzun sustuysa yeni)
        self.nat_usd = 0.0         # bu bölümün HL alt emri dolumları ($) — süren emrin DOLANI (HL kaydı 0 der, bkz. seen_fill)
        self.nat_sz = 0.0          # aynısı adet olarak (doluluk % = adet / emrin adedi — fiyattan bağımsız)

    @property
    def key(self) -> tuple:
        return (self.coin, self.address, self.side)


class Registry:
    def __init__(self):
        self.runs: dict[tuple, Run] = {}
        self.errors = 0
        self.observed = 0

    def observe(self, coin: str, addr: str, side: str, px: float, sz: float,
                notional: float, ts: int, taker=None, native: bool = False) -> None:
        """Sıcak yol: dict araması + birkaç float. Await yok, istisna yok. `native`: bu dolum HL TWAP alt
        emrinin (sıfır hash) ve adres taker — dizi HL emriyle yürüyor demektir (gizli TWAP değil)."""
        self.observed += 1
        key = (coin, addr, side)
        r = self.runs.get(key)
        if r is None:
            if len(self.runs) >= MAX_KEYS:
                self._evict(int(ts))
            r = self.runs[key] = Run(coin, addr, side, int(ts), px)
        ts = int(ts)
        if r.slices and ts - r.slices[-1][0] <= COALESCE_SEC:
            t0, s0, n0, _ = r.slices[-1]
            s1, n1 = s0 + sz, n0 + notional
            r.slices[-1] = (t0, s1, n1, (n1 / s1) if s1 else px)
        else:
            r.slices.append((ts, sz, notional, px))
            r.n += 1
            if taker is not None:
                r.known_n += 1
                if taker:
                    r.tk_n += 1
        if native:
            r.nat_n += 1
            if not r.nat_ts or ts - r.nat_ts > LIVE_SILENT:
                r.nat_first_ts = ts                    # yeni bölüm: önceki emir susmuştu (yeni TWAP olabilir)
                r.nat_usd = r.nat_sz = 0.0
            r.nat_usd += notional
            r.nat_sz += sz
            if ts > r.nat_ts:
                r.nat_ts = ts
        r.total += notional
        r.sz_total += sz
        r.pxsz += px * sz
        if ts >= r.last_ts:
            r.last_ts = ts
            r.px_last = px

    def _evict(self, ref_ts: int) -> None:
        self.prune(ref_ts, IDLE_SEC)
        if len(self.runs) < MAX_KEYS:
            return
        # Hâlâ doluysa en eski %10 (bildirilmemişler) gider
        victims = sorted((r for r in self.runs.values() if not r.alerted_ts),
                         key=lambda r: r.last_ts)[: max(1, MAX_KEYS // 10)]
        for r in victims:
            self.runs.pop(r.key, None)

    def prune(self, ref_ts: int, window_sec: int = 4 * 3600) -> int:
        """Boşta kalan (tekil işlem 5 dk, dizi 30 dk), bitmiş ya da penceresini
        aşmış bildirilmemiş diziler düşer. Bildirilmiş süren tur pencereye takılmaz."""
        dead = []
        for key, r in self.runs.items():
            idle = ref_ts - r.last_ts
            if idle > (IDLE_SHORT_SEC if r.n < 5 else IDLE_SEC):
                dead.append(key)
            elif r.ended_ts and ref_ts - r.ended_ts > IDLE_SEC:
                dead.append(key)
            elif not r.alerted_ts and ref_ts - r.first_ts > window_sec:
                dead.append(key)
        for key in dead:
            self.runs.pop(key, None)
        return len(dead)

    def clear(self) -> None:
        self.runs.clear()
        self.errors = 0
        self.observed = 0


REG = Registry()


def observe(coin: str, addr: str, side: str, px: float, sz: float, notional: float,
            ts: int, taker=None, native: bool = False) -> None:
    """Collector kancası (bkz. app/hl/collector.py::_handle)."""
    REG.observe(coin, addr, side, px, sz, notional, ts, taker, native)


# ---------------- saf ölçüm / kapı ----------------

def measure(run: Run) -> dict | None:
    """Son dilimlerden düzenlilik (twap.detect, gevşek CV) + turun skaler
    toplamları. Düzensizse None. `rate_day`: pencere içindeki hızın güne yayılmışı."""
    rows = [{"ts": t, "sz": s, "notional": n} for (t, s, n, _) in run.slices]
    d = twapmod.detect(rows, cv_gap_max=CV_GAP_LIVE, cv_size_max=CV_SIZE_LIVE)
    if not d:
        return None
    span = rows[-1]["ts"] - rows[0]["ts"]
    win_total = sum(r["notional"] for r in rows)
    gaps = [b["ts"] - a["ts"] for a, b in zip(rows, rows[1:])]
    med_gap = statistics.median(gaps) if gaps else d["avg_gap"]
    rate_day = (win_total / (span + d["avg_gap"]) * 86400) if span > 0 else 0.0
    dur = run.last_ts - run.first_ts
    return {"coin": run.coin, "address": run.address, "side": run.side,
            "n": run.n, "total": run.total, "sz_total": run.sz_total,
            "first_ts": run.first_ts, "last_ts": run.last_ts, "dur": dur,
            "avg_slice": run.total / run.n if run.n else 0.0,
            "avg_gap": d["avg_gap"], "median_gap": med_gap,
            "cv_gap": d["cv_gap"], "cv_size": d["cv_size"],
            "rate_day": rate_day,
            "native_like": NATIVE_GAP[0] <= med_gap <= NATIVE_GAP[1],
            "taker_pct": (run.tk_n / run.known_n * 100) if run.known_n else None,
            "avg_px": (run.pxsz / run.sz_total) if run.sz_total else run.px_last,
            "px_first": run.px_first, "px_last": run.px_last,
            "px_chg_pct": ((run.px_last - run.px_first) / run.px_first * 100) if run.px_first else 0.0}


def latest_per_twap(history) -> list:
    """HL emir geçmişi her DURUM DEĞİŞİMİ için ayrı kayıt tutar: aynı emrin (twapId) 'activated' kaydı başlangıçta
    yazılır ve bitince/iptalde SİLİNMEZ, yanına 'finished'/'terminated' kaydı eklenir (canlı, 12.10: FIL 2308272 —
    activated 08:10:16, terminated 08:11:39). Emir başına yalnız EN SON kayıt kalır; yoksa eski 'activated' kaydı
    iptal edilmiş emri sürüyor gösterirdi (⛔ notu hiç gelmezdi). twapId yoksa (coin, yön, başlangıç, adet) anahtarı."""
    if not isinstance(history, (list, tuple)):
        return []
    best: dict = {}
    for i, h in enumerate(history):
        try:
            st = h.get("state") or {}
            tid = h.get("twapId")
            k = ("id", tid) if tid is not None else ("st", st.get("coin"), st.get("side"), st.get("timestamp"),
                                                        st.get("sz"))
            tm = float(h.get("time") or 0)
            terminal = str(((h.get("status") or {}).get("status")) or "").lower() != "activated"
        except (AttributeError, TypeError, ValueError):
            continue
        cur = best.get(k)
        # daha yeni kayıt kazanır; aynı anda yazılmışsa bitiş/iptal kaydı
        if cur is None or (tm, terminal) > (cur[0], cur[1]):
            best[k] = (tm, terminal, i, h)
    return [v[3] for v in sorted(best.values(), key=lambda v: v[2])]


def parse_twap_orders(history, coin: str, side: str, mark, now_ts: int) -> list[dict]:
    """HL `userTwapHistory` snapshot'ından bu coin + yön için TWAP emirleri. SAF ve
    savunmacı: şekil tutmayan kayıt atlanır. `side` bizim 'buy'/'sell'; HL
    state.side 'B'/'A' (ya da 'Buy'/'Sell') olabilir. Aktif (activated) emirler
    önce, sonra en yeni. planned_usd = adet × bugünkü fiyat (dönüşüm, tahmin değil)."""
    out: list[dict] = []
    if not isinstance(history, (list, tuple)):
        return out
    try:
        mark = float(mark or 0)
    except (TypeError, ValueError):
        mark = 0.0
    for h in latest_per_twap(history):
        try:
            st = h.get("state") or {}
            status = str(((h.get("status") or {}).get("status")) or "").lower()
            if (st.get("coin") or "") != coin:
                continue
            s = str(st.get("side") or "").upper()
            is_buy = s.startswith("B") or s.startswith("L")
            is_sell = s.startswith("A") or s.startswith("S")
            if (side == "buy" and not is_buy) or (side == "sell" and not is_sell):
                continue
            sz = float(st.get("sz") or 0)
            ex_sz = float(st.get("executedSz") or 0)
            ex_ntl = float(st.get("executedNtl") or 0)
            minutes = float(st.get("minutes") or 0)
            t0 = int(float(st.get("timestamp") or 0))
            if t0 > 10 ** 11:
                t0 //= 1000
            px = mark or ((ex_ntl / ex_sz) if ex_sz else 0.0)
            planned_usd = sz * px
            remaining_sz = max(0.0, sz - ex_sz)
            left = int(t0 + minutes * 60 - now_ts) if (t0 and minutes) else None
            out.append({"status": status or "?", "planned_sz": sz, "planned_usd": planned_usd,
                        "executed_sz": ex_sz, "executed_usd": ex_ntl,
                        "remaining_usd": remaining_sz * px, "remaining_sz": remaining_sz,
                        "minutes": minutes, "started_ts": t0,
                        "left_sec": max(0, left) if left is not None else None,
                        "filled_pct": (ex_sz / sz * 100) if sz > 0 else None,
                        "reduce_only": bool(st.get("reduceOnly")), "randomize": bool(st.get("randomize"))})
        except (TypeError, ValueError, AttributeError):
            continue
    out.sort(key=lambda o: (o["status"] != "activated", -(o["started_ts"] or 0)))
    return out


# ---------------- süren emrin dolanı ----------------
# HL'nin süren emir kaydı (twapHistory / userTwapHistory 'activated') emir VERİLDİĞİ anın görüntüsüdür: executedSz /
# executedNtl HEP 0 — HL dolanı yalnız bitiş / iptal kaydında yazar (14.10: 0xedcd… HYPE 150 dk emri listede "doldu %0"
# görünüyordu, HL dilim dolumlarına göre %71'i dolmuştu). Süren emrin dolanı radarın KENDİ gördüğü HL TWAP alt emri
# dolumlarından ölçülür (sıfır hash, taker = emir sahibi; Run.nat_usd). Kapı (kalan kuralı) bu ölçüyü KULLANMAZ — HL
# kaydıyla bugünkü gibi karar verir; yalnız mesaj ve listelerdeki "doldu" doğru olsun.
SEEN_SLACK = 90             # dilim bölümü emrin başlangıcından en çok bu kadar sapıyorsa "emrin başından beri görülüyor"


def seen_fill(coin: str, addr: str, side: str, started_ts, now_ts: int | None = None) -> tuple | None:
    """Süren emrin radarca görülen dolanı: (usd, tam, adet). tam = dilim bölümü emrin başından beri görülüyor (≈ HL'nin
    dolanı); değilse ALT SINIR (radar emrin başını görmedi — yeniden başlatma, geç dinleme). None = ölçü yok (coin
    dinlenmiyor, dilim yok / sustu) ya da bölüm bu emirden önce başlamış (önceki emirle karışık)."""
    r = REG.runs.get((coin, (addr or "").lower(), side)) or REG.runs.get((coin, addr, side))
    if r is None or not r.nat_n or not r.nat_first_ts or not started_ts or r.nat_usd <= 0:
        return None
    if now_ts and int(now_ts) - int(r.nat_ts) > LIVE_SILENT:
        return None
    f, s = int(r.nat_first_ts), int(started_ts)
    if f < s - SEEN_SLACK:
        return None
    if _concurrent(coin, r.address, side, now_ts or r.nat_ts) > 1:
        return None                                     # aynı coin + yönde aynı anda iki emir: dilimler ayrışmaz
    return float(r.nat_usd), f <= s + SEEN_SLACK, float(r.nat_sz)


def _concurrent(coin: str, addr: str, side: str, ts: int) -> int:
    """Adresin son HL emir geçmişinde (önbellek, sorgu yok) bu coin + yönde şu an süren emir sayısı."""
    h = _newest_hist(addr)
    if not h:
        return 0
    n = 0
    for o in parse_twap_orders(h[1], coin, side, None, int(ts)):
        t0, mins = int(o.get("started_ts") or 0), float(o.get("minutes") or 0)
        if o.get("status") == "activated" and t0 and t0 <= int(ts) <= t0 + mins * 60 + 60:
            n += 1
    return n


def with_seen(o: dict | None, coin: str, addr: str, side: str, now_ts: int | None = None) -> dict | None:
    """Mesaj / liste için emir kopyası: HL'nin 'activated' kaydı 0 diyorsa (yerleştirme görüntüsü) `fill_hl0` ve radarın
    gördüğü dolan (`fill_seen`, `fill_full`). Bitmiş / iptal kaydı ya da dolanı yazılı kayıt aynen döner."""
    if not o or o.get("status") != "activated" or float(o.get("executed_usd") or 0) > 0 or not o.get("started_ts"):
        return o
    sf = seen_fill(coin, addr, side, o.get("started_ts"), now_ts)
    w = {**o, "fill_hl0": True, "fill_seen": sf[0] if sf else None, "fill_full": bool(sf and sf[1]),
         "fill_seen_sz": sf[2] if sf else None}
    if sf and sf[2] > 0 and not float(o.get("planned_usd") or 0) and float(o.get("planned_sz") or 0) > 0:
        px = sf[0] / sf[2]                              # fiyat verilmeden ayrıştırıldı (takip): dolum ortalamasıyla dönüşüm
        w["planned_usd"] = float(o["planned_sz"]) * px
    return w


def vol_pct_for(cfg, coin: str, sat: bool = False) -> float:
    """Hacim kuralı sınıfa göre: BTC/ETH `twap_alert_vol_pct_major` (%20 — $1-3M gürültü),
    diğerleri `twap_alert_vol_pct` (%5 — PUMP'ta $3M emir hacmin %4,8'iydi, %20 susturuyordu)."""
    if sat:                                       # Cumartesi hisse: geçen Cumartesi hacmine göre (aşağıda)
        return sat_pct(cfg)
    from .bigpos import MAJORS
    sym = (coin or "").split(":")[-1].upper()
    if sym in MAJORS:
        return float(getattr(cfg, "twap_alert_vol_pct_major", 20) or 0)
    return float(getattr(cfg, "twap_alert_vol_pct", 5) or 0)


# ---------------- Cumartesi hisse tabanı ----------------
# Kullanıcı 13.10: "hafta sonu 24 saat baz alırsa yanlış olur, hafta sonu gelecek yüksek TWAP'ları kaçırırız — bunu
# sadece hisseler için yapalım; geçen hafta Cumartesi'ye kıyasla %10'dan büyük TWAP; Pazar değişmeden kalabilir".
# Ölçüm (13.10, 10 HIP-3 hisse/emtia, 5 hafta saatlik mum): Cumartesi sabahı son 24s hacim, TWAP'ın yürüyeceği sonraki
# 24 saatin ~10 katı (Cuma ABD seansı pencerede) → hafta sonu TWAP'ı 10 kat küçük görünüyordu; geçen hafta aynı saat
# ×1.04. Kripto bu kuraldan HARİÇ (orada geçen hafta ×1.86 yanıltıyordu). Pazar: son 24s zaten hafta sonu → aynen.
# Cumartesi = TSİ Cumartesi (Cuma 21:00 – Cmt 21:00 UTC ≈ ABD kapanışından sonraki 24 saat).
TR_TZ = ZoneInfo("Europe/Istanbul")
SAT_KV = "sat_vol"
SAT_CACHE: dict[str, tuple[int, float]] = {}     # coin → (geçen Cumartesi TSİ başlangıcı, o günün toplam $ hacmi)
_SAT_TRY: dict[str, int] = {}                     # coin → son deneme (veri yoksa 10 dk'da bir yeniden)
_SAT_LOADED: list = [False]
SAT_RETRY = 600
SAT_TIMEOUT = 6.0                                 # taban okuma bütçesi (çağıranlar wait_for ile sarar)
_SAT_INFLIGHT: dict[str, asyncio.Future] = {}     # coin → süren mum isteği (diğer çağıranlar onu bekler)


def sat_pct(cfg) -> float:
    return float(getattr(cfg, "twap_alert_vol_pct_sat", 10) or 0)


def sat_window(now_ts: int) -> tuple[int, int] | None:
    """Bugün TSİ Cumartesi ise GEÇEN haftanın Cumartesi'si (TSİ 00:00–24:00) → (başlangıç, bitiş) UTC sn."""
    d = datetime.fromtimestamp(int(now_ts), TR_TZ)
    if d.weekday() != 5:
        return None
    prev = d.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=7)
    return int(prev.timestamp()), int((prev + timedelta(days=1)).timestamp())


def sat_applies(cfg, coin: str, now_ts: int) -> bool:
    """Kripto OLMAYAN her şey (kullanıcı 13.10: "sadece hisseler değil, kripto olmayan her şeye"): HIP-3'te hisse,
    endeks, emtia, FX, tahvil faizi, ETF, pre-IPO — `para:` dex'indeki AVGO/NET/10Y dahil; kriptoya bağlı olanlar
    (TOTAL2, BTCD, ANSEM… assets.trades_24_7), ana dex kripto ve spot HARİÇ. TSİ Cumartesi ve kural açık (% > 0)."""
    return (sat_pct(cfg) > 0 and bool(coin) and not assets.trades_24_7(coin) and sat_window(now_ts) is not None)


def eff_vol(cfg, coin: str, day_vol, vol_ts, now_ts: int) -> tuple:
    """Kapının kullanacağı hacim: Cumartesi hissede geçen Cumartesi'nin hacmi (önbellekte varsa) → (hacim, an, True);
    aksi halde son 24s (dayNtlVlm) aynen → (day_vol, vol_ts, False). Geçmiş günün sabit ölçümü olduğu için 'an' şimdi."""
    if cfg is not None and sat_applies(cfg, coin, now_ts):
        w = sat_window(now_ts)
        c = SAT_CACHE.get(coin)
        if c and w and int(c[0]) == w[0] and float(c[1] or 0) > 0:
            return float(c[1]), int(now_ts), True
    return day_vol, vol_ts, False


def sat_missing(cfg, coin: str, now_ts: int) -> bool:
    """Cumartesi hissede taban henüz okunamadı (önbellekte yok): ön süzgeç 24s'e göre ELEMESİN."""
    return sat_applies(cfg, coin, now_ts) and not eff_vol(cfg, coin, None, None, now_ts)[2]


async def ensure_sat(cfg, client, coins, now_ts: int, max_fetch: int = 12) -> int:
    """Cumartesi hisse coinleri için geçen Cumartesi hacmini HL saatlik mumlarından oku (coin başına haftada 1 istek;
    kv'de saklanır, restart'ta yeniden istenmez). Dönüş: okunan coin sayısı. Hata fırlatmaz."""
    w = sat_window(now_ts)
    if w is None or sat_pct(cfg) <= 0:
        return 0
    if not _SAT_LOADED[0]:
        try:
            for k, v in (await kv_get(SAT_KV) or {}).items():
                SAT_CACHE[k] = (int(v[0]), float(v[1]))
            _SAT_LOADED[0] = True
        except Exception:                              # noqa: BLE001
            log.debug("sat_vol okunamadı", exc_info=True)
    if client is None or not hasattr(client, "candles"):
        return 0
    uniq = list(dict.fromkeys(coins or []))
    pend = [f for c in uniq if (f := _SAT_INFLIGHT.get(c)) is not None]   # başka çağıran şu an okuyor: bekle
    todo = [c for c in uniq if c not in _SAT_INFLIGHT and sat_applies(cfg, c, now_ts)
            and int((SAT_CACHE.get(c) or (0, 0))[0]) != w[0] and int(now_ts) - _SAT_TRY.get(c, 0) >= SAT_RETRY]
    got = 0
    for c in todo[:max(0, int(max_fetch))]:
        _SAT_TRY[c] = int(now_ts)
        fut = _SAT_INFLIGHT[c] = asyncio.get_running_loop().create_future()
        tot = 0.0
        try:
            rows = await client.candles(c, "1h", w[0] * 1000, w[1] * 1000 - 1)
            for x in rows or []:
                t = int(x.get("t") or 0) // 1000
                if w[0] <= t < w[1]:
                    tot += float(x.get("v") or 0) * (float(x.get("o") or 0) + float(x.get("c") or 0)) / 2
        except Exception:                              # noqa: BLE001
            log.debug("geçen Cumartesi hacmi okunamadı %s", c, exc_info=True)
            tot = 0.0
        finally:
            if tot > 0:
                SAT_CACHE[c] = (w[0], tot)
            _SAT_INFLIGHT.pop(c, None)
            if not fut.done():
                fut.set_result(tot)
        if tot > 0:
            got += 1
    for f in pend:
        try:
            await asyncio.shield(f)
        except Exception:                              # noqa: BLE001
            pass
    if got:
        try:
            cut = w[0] - 7 * 86400
            await kv_set(SAT_KV, {k: [v[0], v[1]] for k, v in SAT_CACHE.items() if v[0] > cut})
        except Exception:                              # noqa: BLE001
            log.debug("sat_vol yazılamadı", exc_info=True)
    return got


def left_floor(cfg, planned: float) -> float:
    """Kalan kuralı: min(min_left, planın yarısı) — $1M'lik emir yarısı dolana kadar bildirilebilsin."""
    min_left = float(getattr(cfg, "twap_alert_min_left_usd", 1_000_000) or 0)
    return min(min_left, planned * 0.5) if planned > 0 else min_left


def order_gate(cfg, order: dict | None, day_vol, vol_ts, now_ts: int, coin: str = "", sat: bool = False) -> str:
    """Emir verisiyle kapı: 'ok' | 'big' geçer; diğerleri nedenidir:
    no_order · order_done · order_small · order_left · no_vol · spot_thin · vol_small.
    `coin` hacim yüzdesinin sınıfını seçer (vol_pct_for) ve spot tabanını uygular."""
    if not order:
        return "no_order"
    if order.get("status") != "activated":
        return "order_done"
    planned = float(order.get("planned_usd") or 0)
    left_usd = float(order.get("remaining_usd") or 0)
    min_left = left_floor(cfg, planned)
    big = float(getattr(cfg, "twap_alert_big_usd", 0) or 0)
    if big > 0 and planned >= big and left_usd >= min_left:
        return "big"
    if planned < float(getattr(cfg, "twap_alert_min_usd", 1_000_000) or 0):
        return "order_small"
    if left_usd < min_left:
        return "order_left"
    if not day_vol or float(day_vol) <= 0 or not vol_ts or now_ts - int(vol_ts) > VOL_MAX_AGE:
        return "no_vol"
    # Spot'ta yüzde kuralı ölü: ince çiftte oran küçülmez, PATLAR ($238K hacimli
    # çiftte $1,9M emir = %799). Tek anlamlı fren çiftin kendi hacmidir.
    spot_min_vol = float(getattr(cfg, "spot_twap_min_day_vol", 0) or 0)
    if spot_min_vol > 0 and assets.is_spot(coin) and float(day_vol) < spot_min_vol:
        return "spot_thin"
    if planned / float(day_vol) * 100 < vol_pct_for(cfg, coin, sat):
        return "vol_small"
    return "ok"


def gate_detail(cfg, coin: str, order: dict | None, day_vol, vol_ts, now_ts: int) -> dict:
    """Kapı kararı SAYILARLA (teşhis): {reason, planned, left, vol_pct, pct_needed, need_usd,
    left_need, day_vol}. need_usd = bildirim için gereken emir = max(min_usd, yüzde × hacim).
    Cumartesi hissede hacim = geçen Cumartesi'ninki (eff_vol) ve yüzde onun eşiği; `sat` ve ham 24s `day_vol_24h`."""
    raw = day_vol
    day_vol, vol_ts, sat = eff_vol(cfg, coin, day_vol, vol_ts, now_ts)
    reason = order_gate(cfg, order, day_vol, vol_ts, now_ts, coin, sat)
    planned = float((order or {}).get("planned_usd") or 0)
    left = float((order or {}).get("remaining_usd") or 0)
    pct = vol_pct_for(cfg, coin, sat)
    dv = float(day_vol or 0)
    min_usd = float(getattr(cfg, "twap_alert_min_usd", 1_000_000) or 0)
    spot_min_vol = float(getattr(cfg, "spot_twap_min_day_vol", 0) or 0) if assets.is_spot(coin) else 0.0
    return {"reason": reason, "planned": planned, "left": left, "day_vol": dv or None,
            "vol_pct": (planned / dv * 100) if dv else None, "pct_needed": pct,
            "need_usd": max(min_usd, dv * pct / 100) if dv else min_usd,
            "vol_need": spot_min_vol or None,
            "left_need": left_floor(cfg, planned), "status": (order or {}).get("status"), "sat": sat,
            "day_vol_24h": raw}


_lookup_cache: dict[str, tuple[int, list]] = {}


async def lookup_order(collector, run: Run, now_ts: int, cfg, out: dict, force: bool = False) -> dict | None:
    """Adresin HL TWAP emrini sorgula (collector.fetch_twap_history → snapshot).
    Adres başına önbellek (`twap_lookup_cooldown`), tur başına sorgu tavanı.
    Dönüş: bu coin+yön için en uygun emir ya da None; `run.order` güncellenir."""
    addr = run.address
    cool = int(getattr(cfg, "twap_lookup_cooldown", 600) or 0)
    cached = _lookup_cache.get(addr)
    hist = None
    q_ts = now_ts
    if cached and run.nat_first_ts and int(cached[0]) < int(run.nat_first_ts):
        cached = None                               # önbellek bu dizinin ilk HL alt emrinden ÖNCE: yeni emri içermez
        #                                             (aynı adresin başka coin'deki erken sorgusu — 'no_order' olmasın)
    if cached and not force and now_ts - cached[0] < cool:
        hist, q_ts = cached[1], int(cached[0])
    else:
        if out.get("lookups", 0) >= LOOKUP_MAX_PER_EVAL:
            out["lookup_capped"] = out.get("lookup_capped", 0) + 1
            return run.order
        fn = getattr(collector, "fetch_twap_history", None)
        if fn is None:
            out["lookup_fail"] = out.get("lookup_fail", 0) + 1
            return None
        out["lookups"] = out.get("lookups", 0) + 1
        try:
            hist = await fn(addr)
        except Exception as e:
            log.debug("twap sorgusu %s: %s", addr[:10], e)
            hist = None
        if hist is None:
            out["lookup_fail"] = out.get("lookup_fail", 0) + 1
            return None
        _lookup_cache[addr] = (now_ts, hist)
        if len(_lookup_cache) > 2000:
            for k in sorted(_lookup_cache, key=lambda k: _lookup_cache[k][0])[:500]:
                _lookup_cache.pop(k, None)
    orders = parse_twap_orders(hist, run.coin, run.side, run.px_last, now_ts)
    for o in orders:
        o["q_ts"] = q_ts                            # HL cevabının anı (önbellekten geldiyse önbelleğinki)
    run.lookup_ts = now_ts
    run.order = orders[0] if orders else None
    return run.order


HIDDEN_SLACK = 120          # 🕵️ bitmiş HL emri dizinin başından bu kadar önce bitmediyse "örtüşüyor" sayılır
HIDDEN_FRESH_SKEW = 30      # emir geçmişi son dilimden en çok bu kadar önce alınmışsa taze sayılır (saat farkı)


def native_overlap(orders, run: Run) -> bool:
    """Bu dizinin süresiyle örtüşebilen HL TWAP emri var mı: süren (activated) ya da planlanan bitişi dizinin
    başından sonraya uzanan. Dizi başlamadan çok önce bitmiş eski emir "emir var" sayılmaz (yoksa adresin
    geçmişteki tek bir TWAP'ı onu gizli TWAP radarından ömür boyu silerdi). Planlanan bitiş kullanılır —
    iptal daha erken bitirse de — yani şüphede "örtüşüyor" (gizli TWAP iddiası yok)."""
    for o in orders or ():
        if o.get("status") == "activated":
            return True
        t0 = int(o.get("started_ts") or 0)
        if not t0 or t0 + float(o.get("minutes") or 0) * 60 + HIDDEN_SLACK >= run.first_ts:
            return True
    return False


async def verify_hidden(collector, run: Run, now_ts: int, cfg, out: dict) -> bool | None:
    """🕵️ "HL'de TWAP emri yok" iddiası için TAZE kanıt. True = dizinin son diliminden SONRA alınmış emir
    geçmişinde bu coin + yönde örtüşen emir yok; False = HL TWAP emri var (ya da dizide HL alt emri dolumu
    görüldü); None = taze veri alınamadı (tavan / sorgu düştü) → karar yok, sonraki tur.
    Adres önbelleği diziden eskiyse zorla sorulur: önbellek alındıktan sonra açılan emir onda görünmez."""
    if run.nat_n:
        return False
    c = _lookup_cache.get(run.address)
    if not (c and c[0] >= run.last_ts - HIDDEN_FRESH_SKEW):
        if collector is None:
            return None
        lf, lc = out.get("lookup_fail", 0), out.get("lookup_capped", 0)
        await lookup_order(collector, run, now_ts, cfg, out, force=True)
        if out.get("lookup_fail", 0) > lf or out.get("lookup_capped", 0) > lc:
            return None
        c = _lookup_cache.get(run.address)
        if not c:
            return None
    return not native_overlap(parse_twap_orders(c[1], run.coin, run.side, run.px_last, now_ts), run)


def is_ended(run: Run, m: dict | None, ref_ts: int) -> bool:
    gap = float((m or {}).get("avg_gap") or run.rate_day and 30 or 30)
    return ref_ts - run.last_ts > max(END_GAPS * gap, END_MIN_SEC)


def next_progress_step(run: Run) -> float | None:
    """Alarm toplamının üstünde geçilen EN BÜYÜK basamak (sıçrama olursa tepe yazılır)."""
    floor = max(float(run.progress_step or 0), float(run.alert_total or 0))
    crossed = [s for s in PROGRESS_STEPS if s > floor and run.total >= s]
    return max(crossed) if crossed else None


def klass_of(coin: str) -> str:
    """kripto | hisse | endeks (mesaj rozeti ve yönlendirme) — assets.klass (para:ANSEM → kripto)."""
    from .. import assets
    return assets.klass(coin)


# ---------------- DB yardımcıları ----------------

async def volumes(client=None) -> dict[str, tuple[float, int]]:
    """{coin: (24s hacim $, ölçüm ts)} — kripto kv özetinden, hisse/endeks asset_metrics'ten.
    `client` verilirse kv bayatken (≥30 dk) tek istekle tazelenir: metrik turu dursa
    bile kripto TWAP radarı topluca `no_vol`a düşmesin."""
    out: dict[str, tuple[float, int]] = {}
    try:
        from ..hl.universe import main_dex_ctx
        ctx = await main_dex_ctx(client, ttl=VOL_MAX_AGE // 2, fetch=client is not None)
        ts = int(ctx.get("ts") or 0)
        for c, v in (ctx.get("c") or {}).items():
            if v.get("v") is not None:
                out[c] = (float(v["v"]), ts)
    except Exception:
        log.debug("ana dex hacmi okunamadı", exc_info=True)
    try:
        async with db() as conn:
            cur = await conn.execute(
                """SELECT m.coin, m.day_volume, m.ts FROM asset_metrics m
                   JOIN (SELECT coin, MAX(ts) mts FROM asset_metrics GROUP BY coin) x
                     ON x.coin = m.coin AND x.mts = m.ts
                   WHERE m.coin LIKE '%:%' AND m.day_volume IS NOT NULL""")
            for r in await cur.fetchall():
                out[r["coin"]] = (float(r["day_volume"]), int(r["ts"]))
    except Exception:
        log.debug("hisse hacmi okunamadı", exc_info=True)
    try:
        # Spot çiftleri (@107…): hacim spot evreni kv'sinde. Olmazsa kapı
        # `no_vol` der ve spot TWAP'ı hiç değerlendirilmez.
        from ..db import kv_get as _kv
        sp = await _kv("spot_top_coins") or {}
        sts = int(sp.get("ts") or 0)
        for c, v in (sp.get("vols") or {}).items():
            try:
                out[str(c)] = (float(v), sts)
            except (TypeError, ValueError):
                continue
    except Exception:
        log.debug("spot hacmi okunamadı", exc_info=True)
    return out


async def entities(addrs: list[str]) -> dict[str, str]:
    if not addrs:
        return {}
    async with db() as conn:
        q = ",".join("?" * len(addrs))
        cur = await conn.execute(
            f"SELECT address, COALESCE(entity,'') entity FROM addresses WHERE address IN ({q})", addrs)
        return {r["address"]: r["entity"] for r in await cur.fetchall()}


async def position_ctx(client, coin: str, addr: str, wallet: bool = False,
                       timeout: float | None = None) -> dict | None:
    """Adresin bu coindeki pozisyonu: canlı (1 istek) — düşerse / `timeout`'ta gelmezse süpürme kaydı.

    `wallet` (⚠️ düşük bakiye satırı açıksa): dönüşte 'wallet': {"total": HL cüzdan değeri | None, "locked": kasa +
    stake $ | None}. Adres önbelleği tazeyse istek yok; değilse cüzdan partisi (normal şerit, tekrar denemesiz): kota
    rahatsa (boş ≥ WALLET_PARALLEL_FREE) pozisyon isteğiyle AYNI ANDA (yarış yok, ⚡ yetişir), sıkışıksa pozisyon
    yanıtı GELDİKTEN SONRA (3. inceleme 15.10: kota doluyken parti pozisyon isteğinin önüne geçiyor, ⚡ pozisyon satırı
    kayboluyordu), doluysa (boş < WALLET_MIN_FREE) hiç (satır yok — o anda zaten yetişmezdi). Sonra en çok ⚡ yolunda (timeout verilmiş) WALLET_GRACE, yavaş
    yollarda WALLET_GRACE_SLOW; yetişmeyen parti arka planda biter ve önbelleği doldurur. 2. inceleme: düşük şerit
    429'dan sonra 60 sn ve arka plan işi %70'i doldururken hiç çıkamıyordu."""
    if client is not None:
        try:
            from .tracker import parse_position
            from .report import coin_dex
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            main = asyncio.ensure_future(client.clearinghouse(addr, coin_dex(coin)))
            wtask, free = None, None
            if wallet and wallet_cached(addr) is None:
                free = _budget_free(client)
                if free is None or free >= WALLET_PARALLEL_FREE:   # kota rahat: aynı anda (sıra yarışı yok)
                    wtask = wallet_task(client, addr)
            state = await (asyncio.wait_for(main, timeout) if timeout else main)
            live = parse_position(state, coin)
            out = ({"side": live["side"], "notional": live["notional"], "src": "canlı", "ts": now()} if live
                   else {"none": True, "src": "canlı"})
            if wallet:
                w = wallet_cached(addr)
                if w is None and (wtask is not None or not _budget_tight(client)):
                    wtask = wtask or wallet_task(client, addr)  # kota sıkışıkken: pozisyon yanıtından SONRA
                    grace = WALLET_GRACE if timeout else WALLET_GRACE_SLOW
                    if timeout:
                        grace = min(grace, max(0.0, t0 + timeout - loop.time()))
                    if wtask is not None:
                        await asyncio.wait([wtask], timeout=grace)   # zaman aşımı iptal ETMEZ: arka planda biter
                        if wtask.done() and not wtask.cancelled() and wtask.exception() is None:
                            w = wtask.result()          # yarım sonuç da bu uyarı için kullanılır (önbellek kısa tutar)
                    w = w or wallet_cached(addr)
                elif w is None:
                    LOW_BAL["tight"] += 1
                out["wallet"] = w or {"total": None, "locked": None}
            return out
        except Exception as e:
            log.debug("twap pozisyon sorgusu %s: %s", addr[:10], e)
    async with db() as conn:
        if ":" in coin:
            cur = await conn.execute(
                "SELECT side, notional, ts FROM positions_current WHERE coin=? AND address=?", (coin, addr))
        else:
            cur = await conn.execute(
                "SELECT side, notional, ts FROM addr_positions WHERE coin=? AND address=?"
                " AND closed_ts IS NULL ORDER BY ts DESC LIMIT 1", (coin, addr))
        r = await cur.fetchone()
    if r:
        return {"side": r["side"], "notional": r["notional"], "src": "süpürme", "ts": r["ts"]}
    return None


async def persist(run: Run, m: dict | None, src: str = "live", order: dict | None = None) -> None:
    """twap_runs'a yaz/güncelle (PK coin,address,side,first_ts). Arşiv taraması
    aynı PK'ya rastlarsa kendi kolonlarını günceller, bizimkilere dokunmaz. `order`: hızlı yolun
    emri (eski yolun run.order'ına yazılmaz)."""
    m = m or {}
    o = order or run.order or {}
    async with db() as conn:
        await conn.execute(
            """INSERT INTO twap_runs(coin,address,side,first_ts,last_ts,n_slices,total,avg_slice,
                 avg_gap,cv_gap,cv_size,taker_pct,ts,day_volume,rate_day,src,alerted_ts,ended_ts,
                 px_first,px_last,sz_total,planned_usd,planned_sz,executed_usd,remaining_usd,
                 order_ts,order_min,order_status,lookup_ts,fast_ts)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(coin,address,side,first_ts) DO UPDATE SET
                 last_ts=excluded.last_ts, n_slices=excluded.n_slices, total=excluded.total,
                 avg_slice=excluded.avg_slice, avg_gap=excluded.avg_gap, cv_gap=excluded.cv_gap,
                 cv_size=excluded.cv_size, taker_pct=excluded.taker_pct, ts=excluded.ts,
                 day_volume=excluded.day_volume, rate_day=excluded.rate_day, src=excluded.src,
                 alerted_ts=excluded.alerted_ts, ended_ts=excluded.ended_ts,
                 px_first=excluded.px_first, px_last=excluded.px_last, sz_total=excluded.sz_total,
                 planned_usd=excluded.planned_usd, planned_sz=excluded.planned_sz,
                 executed_usd=excluded.executed_usd, remaining_usd=excluded.remaining_usd,
                 order_ts=excluded.order_ts, order_min=excluded.order_min,
                 order_status=excluded.order_status, lookup_ts=excluded.lookup_ts,
                 fast_ts=COALESCE(excluded.fast_ts, twap_runs.fast_ts)""",
            (run.coin, run.address, run.side, run.first_ts, run.last_ts, run.n, run.total,
             (run.total / run.n) if run.n else 0.0, m.get("avg_gap"), m.get("cv_gap"), m.get("cv_size"),
             m.get("taker_pct"), now(), run.day_volume, run.rate_day, src, run.alerted_ts,
             run.ended_ts, run.px_first, run.px_last, run.sz_total,
             o.get("planned_usd"), o.get("planned_sz"), o.get("executed_usd"), o.get("remaining_usd"),
             o.get("started_ts"), o.get("minutes"), o.get("status"), int(o.get("q_ts") or 0) or run.lookup_ts,
             None))


async def _rehydrate(window_sec: int) -> int:
    """Restart: bildirilmiş, bitmemiş canlı turlar skaler olarak geri yüklenir —
    bitiş/ilerleme notu kaybolmasın. Dilim dizisi boş başlar (düzenlilik yeniden ölçülür)."""
    ts = now()
    async with db() as conn:
        cur = await conn.execute(
            """SELECT * FROM twap_runs WHERE src='live' AND alerted_ts IS NOT NULL
               AND ended_ts IS NULL AND last_ts >= ?""", (ts - window_sec,))
        rows = [dict(r) for r in await cur.fetchall()]
    n = 0
    for r in rows:
        key = (r["coin"], r["address"], r["side"])
        cur = REG.runs.get(key)
        if cur is not None and cur.alerted_ts:
            continue
        if cur is not None:
            # Açılışta yeni dilimler bu anahtar için bildirilmemiş taze bir tur kurmuş: kayıtlı (bildirilmiş) turu
            # onun ÜZERİNE yükle — yoksa bitiş/iptal notu (⚡ HIZLI'da kanala otomatik) kaybolurdu.
            run = cur
            run.first_ts = min(int(run.first_ts), int(r["first_ts"]))
            run.n += int(r.get("n_slices") or 0)
            run.total += float(r.get("total") or 0)
            run.sz_total += float(r.get("sz_total") or 0)
            run.px_first = float(r.get("px_first") or run.px_first)
        else:
            run = Run(r["coin"], r["address"], r["side"], int(r["first_ts"]), float(r.get("px_first") or 0))
            run.last_ts = int(r["last_ts"])
            run.n = int(r.get("n_slices") or 0)
            run.total = float(r.get("total") or 0)
            run.sz_total = float(r.get("sz_total") or 0)
            run.px_last = float(r.get("px_last") or run.px_first)
        run.alerted_ts = int(r["alerted_ts"])
        run.alert_total = run.total
        run.day_volume = r.get("day_volume")
        run.rate_day = r.get("rate_day")
        run.lookup_ts = r.get("lookup_ts")
        if r.get("planned_usd"):
            psz = float(r.get("planned_sz") or 0)
            exu = float(r.get("executed_usd") or 0)
            run.order = {"status": r.get("order_status") or "activated", "planned_sz": psz,
                         "planned_usd": float(r["planned_usd"]), "executed_sz": None, "executed_usd": exu,
                         "remaining_usd": float(r.get("remaining_usd") or 0), "remaining_sz": None,
                         "minutes": float(r.get("order_min") or 0), "started_ts": int(r.get("order_ts") or 0),
                         "left_sec": None,
                         "filled_pct": (exu / float(r["planned_usd"]) * 100) if float(r["planned_usd"]) else None,
                         "reduce_only": False, "randomize": False, "q_ts": int(r.get("lookup_ts") or 0)}
        REG.runs[key] = run
        n += 1
    return n


def chat_for(cfg, coin: str) -> tuple[str, bool]:
    """(chat_id, gönderilebilir mi): kripto (ana dex ya da kripto dex) → CRYPTO_CHAT_ID
    (boşsa gönderme), hisse/endeks → ana sohbet."""
    if klass_of(coin) == "kripto":
        chat = (getattr(cfg, "crypto_chat_id", "") or "").strip()
        return chat, bool(chat)
    return "", True


# ⚠️ Düşük bakiye (kullanıcı 15.10: "milyonluk twaplarda iptal gelebiliyor eğer cüzdanda 200-300k varsa … twapı atanın
# cüzdanı düşük bütçeliyse uyarı versin, ilk satırların oralarda; düşükten kastım 500k altındaysa"). Ölçüm, tahmin değil:
# mesaj HL'nin kendi cüzdan değerini ve planın ona oranını yazar. İnceleme (15.10, canlı veriyle yeniden üretildi):
# perp/spot parçalarını kendimiz toplamak birleşik hesapta çift sayıyor, portföy marjinde (borçlu USDC) $8.5M'lık
# cüzdana "$0" diyor, izlenmeyen HIP-3 dex'lerini ve spot tokenleri kaçırıyordu → tek kaynak HL `portfolio`. 2. tur:
# HL toplamı kasa (HLP…) ve stake HYPE'ı da sayıyor → düşülür (teminat değil); düşük şerit satırı kaybettiriyordu →
# normal şerit + adres önbelleği. 3. tur: parti pozisyon YANITINDAN sonra (kotaya yarışmasın), kota doluysa hiç; önbellek
# 2 dk (yarım sonuç 30 sn); sayaç emir başına tekil; "kilitli" denmez (HLP kilitlerinin çoğu açık).
LOW_BAL: dict = {"checked": 0, "unknown": 0, "closing": 0, "tight": 0, "warned": 0, "last": None}
LOW_BAL_KEYS: dict[str, int] = {}   # uyarısı sayılmış emirler (aynı emir ana + ayna + 🕐 + yeniden deneme → tek sayım)
WALLET_GRACE = 0.5          # ⚡ yolu: pozisyon yanıtından sonra bakiye için en çok bu kadar (sn); gelmezse satır yok
WALLET_GRACE_SLOW = 2.0     # yavaş yollar (eski yol, 🔸/🕐 eski yol): gecikme bütçeleri yok
WALLET_TTL = 120            # adres önbelleği: ⚡ + 🕐 + eski yol aynı emri ardışık yazar (3. inceleme: 10 dk, yatırılan
#                             parayı görmeyip yeni emirde eski "düşük" değeri yazabiliyordu)
WALLET_TTL_PARTIAL = 30     # kasa/stake okunamamış yarım sonuç: kısa — bir 429 adresi dakikalarca satırsız bırakmasın
WALLET_MIN_FREE = 8         # kotada bundan az boş istek varsa parti başlamaz (pozisyon / diğer işler önce)
WALLET_PARALLEL_FREE = 20   # bundan çok boş istek varsa parti pozisyon isteğiyle AYNI ANDA (yarış yok, ⚡ yetişir);
#                             arada (8–19) pozisyon yanıtından sonra
PORTFOLIO_MAX_AGE = 1800    # "day" serisinin son noktası bundan eskiyse bakiye bilinmiyor sayılır
CLOSING_UNSURE = 0.15       # radar emri baştan görmediyse ve emrin bu kadarı geçtiyse kalan bilinmez (ters pozisyonda)
WALLET_CACHE: dict[str, tuple[int, dict]] = {}
WALLET_INFLIGHT: dict[str, asyncio.Future] = {}


def _retrieved(f) -> None:
    """Arka plan isteğinin hatası "Task exception was never retrieved" diye loglanmasın."""
    if not f.cancelled():
        f.exception()


def _num(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_vault_equity(resp) -> float | None:
    """userVaultEquities → kasa paylarının $ toplamı; şekil tutmazsa None."""
    if not isinstance(resp, list):
        return None
    tot = 0.0
    for v in resp:
        e = _num((v or {}).get("equity")) if isinstance(v, dict) else None
        if e is None:
            return None
        tot += e
    return tot


def parse_staked_hype(resp) -> float | None:
    """delegatorSummary → stake'teki HYPE adedi (delege + delege edilmemiş + çözülmekte); şekil tutmazsa None."""
    if not isinstance(resp, dict):
        return None
    parts = [_num(resp.get(k)) for k in ("delegated", "undelegated", "totalPendingWithdrawal")]
    return None if any(x is None for x in parts) else sum(parts)


async def fetch_wallet(client, addr: str) -> dict:
    """Tek parti, normal şerit, tekrar denemesiz, aynı anda: portfolio + kasa + stake + HYPE fiyatı (allMids, 2) →
    {"total", "locked"}. locked = kasa $ + stake HYPE × fiyat; bir parçası okunamazsa None (low_bal_ctx karar verir).
    Fiyat stake olmasa da istenir: ardışık sorgu ⚡ yolunda 0,5 sn'ye yetişmiyordu (canlı 15.10)."""
    async def opt(name, *a):
        fn = getattr(client, name, None)
        return await fn(*a, priority="normal", retries=0) if fn else None
    pf, ve, ds, mids = await asyncio.gather(opt("portfolio", addr), opt("vault_equities", addr),
                                            opt("delegator_summary", addr), opt("all_mids"),
                                            return_exceptions=True)
    ok = lambda x: None if isinstance(x, BaseException) else x            # noqa: E731
    total = parse_portfolio_value(ok(pf))
    vault, staked = parse_vault_equity(ok(ve)), parse_staked_hype(ok(ds))
    px = _num(ok(mids).get("HYPE")) if isinstance(ok(mids), dict) else None
    stake_usd = 0.0 if staked == 0 else (staked * px if staked is not None and px else None)
    locked = vault + stake_usd if vault is not None and stake_usd is not None else None
    return {"total": total, "locked": locked}


def _budget_free(client) -> int | None:
    """HL kotasında şu an boş istek sayısı; bilinmiyorsa (sahte istemci) None."""
    try:
        return int(client.usage()["free"])
    except Exception:                                       # noqa: BLE001
        return None


def _budget_tight(client) -> bool:
    """HL kotası dolu mu (boş istek < WALLET_MIN_FREE) — o zaman cüzdan partisi hiç başlamaz."""
    free = _budget_free(client)
    return free is not None and free < WALLET_MIN_FREE


def wallet_cached(addr: str) -> dict | None:
    hit = WALLET_CACHE.get(addr)
    if not hit:
        return None
    ttl = WALLET_TTL if hit[1].get("locked") is not None else WALLET_TTL_PARTIAL
    return hit[1] if now() - hit[0] < ttl else None


def wallet_task(client, addr: str):
    """Önbellek tazeyse None; değilse adres başına TEK süren parti (aynı anda iki yol sorarsa paylaşılır). Parti
    bitince (çağıran beklemeyi bıraksa da) toplam okunduysa önbelleğe yazılır."""
    if wallet_cached(addr) is not None:
        return None
    t = WALLET_INFLIGHT.get(addr)
    if t is not None and not t.done():
        return t
    t = asyncio.ensure_future(fetch_wallet(client, addr))
    WALLET_INFLIGHT[addr] = t

    def _done(f, addr=addr):
        if WALLET_INFLIGHT.get(addr) is f:
            WALLET_INFLIGHT.pop(addr, None)
        if not f.cancelled() and f.exception() is None and f.result().get("total") is not None:
            WALLET_CACHE[addr] = (now(), f.result())
            if len(WALLET_CACHE) > 5000:                            # bellek tavanı: en eskiler düşer
                for k, _ in sorted(WALLET_CACHE.items(), key=lambda kv: kv[1][0])[:1000]:
                    WALLET_CACHE.pop(k, None)
    t.add_done_callback(_done)
    t.add_done_callback(_retrieved)
    return t


def low_balance(cfg) -> float:
    """⚠️ Düşük bakiye eşiği ($): TWAP'ı atan cüzdan bunun altındaysa mesajın 2. satırı. 0 = kapalı (ek istek de yok)."""
    return max(0.0, float(getattr(cfg, "twap_low_balance_usd", 500_000) or 0))


def wallet_on(cfg) -> bool:
    return low_balance(cfg) > 0


def parse_account_value(state) -> float | None:
    """clearinghouseState → bu dex'in perp hesap bakiyesi (marginSummary.accountValue); okunamazsa None."""
    ms = (state or {}).get("marginSummary") if isinstance(state, dict) else None
    try:
        return float(ms["accountValue"]) if isinstance(ms, dict) and ms.get("accountValue") is not None else None
    except (TypeError, ValueError):
        return None


def parse_portfolio_value(resp, now_ts: int | None = None) -> float | None:
    """`portfolio` yanıtı → HL'nin şimdiki toplam cüzdan değeri ("day" serisinin son noktası). Şekil tutmaz, seri boş
    ya da son nokta PORTFOLIO_MAX_AGE'den eskiyse None ("bilmiyoruz" ≠ "$0")."""
    try:
        series = dict(resp or [])
    except (TypeError, ValueError):
        return None
    avh = (series.get("day") or {}).get("accountValueHistory") if isinstance(series.get("day"), dict) else None
    if not isinstance(avh, list) or not avh:
        return None
    try:
        ts_ms, val = avh[-1][0], float(avh[-1][1])
    except (TypeError, ValueError, IndexError):
        return None
    if abs((now_ts or now()) - int(ts_ms) / 1000) > PORTFOLIO_MAX_AGE:
        return None
    return val


def _left_usd(o: dict) -> tuple[float, bool]:
    """Emrin kalanı ($) ve kesin mi. HL süren emirde dolanı 0 yazar (with_seen → fill_seen): radar baştan gördüyse
    kalan = plan − gördüğü; görmediyse gördüğü alt sınırdır → kalan üst sınır (kesin değil)."""
    plan = float(o.get("planned_usd") or 0)
    if o.get("fill_hl0"):
        seen = o.get("fill_seen")
        return max(0.0, plan - float(seen or 0)), bool(o.get("fill_full"))
    if o.get("remaining_usd") is not None:
        return float(o.get("remaining_usd") or 0), True
    return plan, True


def wallet_budget(coin: str, side: str, order: dict | None, pos: dict | None,
                  now_ts: int | None = None) -> dict | None:
    """TWAP'ı atan cüzdanın değeri — ölçüm (HL `portfolio` toplamı + kasa/stake kısmı). None = bilinmiyor.

    • Kapatan emir uyarılmaz ('closing'): reduce-only ya da ters yöndeki perp pozisyonu emrin KALANININ en az yarısı
      (yeni teminat istemez). Kalan, emir süren bir emirde ilk uyarı gecikse de (restart, kapı sonradan açıldı) planla
      değil kalanla kıyaslanır. Radar emri baştan görmediyse ve emrin CLOSING_UNSURE'ü geçtiyse ters pozisyon varken
      kalan bilinmez → None (tahmin yok)."""
    w = (pos or {}).get("wallet")
    if not isinstance(w, dict) or w.get("total") is None:
        return None
    o = order or {}
    plan = float(o.get("planned_usd") or 0)
    if o.get("reduce_only"):
        return {"kind": "closing"}
    if pos and not pos.get("none") and not assets.is_spot(coin):
        opp = (side == "buy" and pos.get("side") == "short") or (side != "buy" and pos.get("side") == "long")
        if opp:
            left, sure = _left_usd(o)
            mins = float(o.get("minutes") or 0)
            t0 = int(o.get("started_ts") or 0)
            gone = ((now_ts or now()) - t0) / (mins * 60) if mins > 0 and t0 else 0.0
            if not sure and gone > CLOSING_UNSURE:
                return None
            if float(pos.get("notional") or 0) >= left / 2:
                return {"kind": "closing"}
    return {"kind": "wallet", "total": float(w["total"]), "locked": None if w.get("locked") is None
            else max(0.0, float(w["locked"])), "plan": plan}


def low_bal_ctx(cfg, coin: str, side: str, order: dict | None, pos: dict | None) -> dict | None:
    """Mesajın ⚠️ satırı için bağlam: cüzdan eşiğin altındaysa {usd, plan, limit}, değilse None. Gönderim sayacı
    YAZMAZ (gönderildikten sonra low_bal_mark); hazırlık sayaçları /tani'de "hazırlıkta" diye ayrı."""
    lim = low_balance(cfg)
    if lim <= 0:
        return None
    LOW_BAL["checked"] += 1
    b = wallet_budget(coin, side, order, pos)
    if b is None:
        LOW_BAL["unknown"] += 1
        return None
    if b["kind"] == "closing":
        LOW_BAL["closing"] += 1
        return None
    total, locked = b["total"], b["locked"]
    if locked is None:                              # kasa/stake okunamadı: kullanılabilir ≤ toplam
        if total >= lim:
            LOW_BAL["unknown"] += 1                 # toplam eşiği geçiyor ama kasa/stake bilinmiyor → karar yok
            return None
        usd = total
    else:
        usd = max(0.0, total - locked)              # kasa (HLP…) ve stake HYPE TWAP'a teminat olamaz
    if usd >= lim:
        return None
    return {**b, "usd": usd, "limit": lim}


def low_bal_mark(lb: dict | None, coin: str, key: str = "") -> None:
    """⚠️ satırlı mesaj GERÇEKTEN gönderildi: sayaç + son uyarı (/tani). `key` = emir anahtarı (coin:adres:yön:başlangıç):
    aynı emir ana kanal + ayna + 🕐 + yeniden deneme turunda bir kez sayılır (3. inceleme 15.10)."""
    if not lb:
        return
    if key:
        if key in LOW_BAL_KEYS:
            return
        LOW_BAL_KEYS[key] = now()
        if len(LOW_BAL_KEYS) > 2000:
            for k, _ in sorted(LOW_BAL_KEYS.items(), key=lambda kv: kv[1])[:500]:
                LOW_BAL_KEYS.pop(k, None)
    LOW_BAL["warned"] += 1
    LOW_BAL["last"] = {"coin": coin, "usd": lb.get("usd"), "plan": lb.get("plan"), "ts": now()}


def low_bal_diag(cfg) -> str:
    """/tani satırı: eşik, gönderilen uyarılar, son uyarı ve hazırlık sayaçları (bu açılıştan beri)."""
    lim = low_balance(cfg)
    if lim <= 0:
        return "⚠️ düşük bakiye satırı: kapalı (TWAP_LOW_BALANCE_USD=0)"
    s = LOW_BAL
    line = f"⚠️ düşük bakiye satırı: eşik ${lim:,.0f} · bu açılıştan beri {s['warned']} emirde uyarı gönderildi"
    last = s.get("last")
    if last:
        line += (f" (son: {assets.label(last['coin'])} cüzdan ${float(last['usd'] or 0):,.0f}"
                 f" / plan ${float(last.get('plan') or 0):,.0f}, {max(0, now() - int(last['ts'])) // 60} dk önce)")
    line += f" · hazırlıkta {s['checked']} bakış"
    if s["unknown"]:
        line += f", {s['unknown']} cüzdan okunamadı (yazılmaz)"
    if s["closing"]:
        line += f", {s['closing']} kapatan emir (yazılmaz)"
    if s["tight"]:
        line += f", {s['tight']} kez HL kotası doluydu (sorulmadı)"
    return line


def quick_max(cfg) -> float:
    """⚡ HIZLI TWAP sınırı (dk) — mesaj rozeti ve hızlı TWAP dakika hızı kuralı aynı sınırı kullanır."""
    return float(getattr(cfg, "twap_fast_short_max_min", 60) or 0)


def is_quick(cfg, order: dict | None) -> bool:
    """⚡ HIZLI: emir süresi ≤ 1 sa. Bunlarda takip tuşu yok — iptal ⛔ / bitiş 🏁 kanala kendiliğinden yazılır
    (kullanıcı 12.10: "hızlılar için iptal tuşuna basmama gerek kalmasın")."""
    mins = float((order or {}).get("minutes") or 0)
    qm = quick_max(cfg)
    return 0 < mins <= qm if qm > 0 else False


def auto_end(cfg, order: dict | None) -> str:
    """⚡ HIZLI emirde kanala kendiliğinden ne yazılır: '' (hiçbiri → takip tuşu kalır) | 'cancel' | 'both'.
    Mesajdaki 🔔 satırı ve takip tuşunun yokluğu buna göre — yalnız gerçekten yazılacak olan vaat edilir."""
    if not is_quick(cfg, order) or not getattr(cfg, "twap_alert_end_note", True):
        return ""
    return "both" if getattr(cfg, "twap_alert_finish_note", False) else "cancel"


def end_note_wanted(cfg, cancelled: bool) -> bool:
    """Kanala bitiş notu gidecek mi: iptal → ⛔ (twap_alert_end_note); normal bitiş → 🏁 yalnız
    twap_alert_finish_note açıksa (kullanıcı 12.10: 'iptal etmedikçe bildirim atma')."""
    if not getattr(cfg, "twap_alert_end_note", True):
        return False
    return True if cancelled else bool(getattr(cfg, "twap_alert_finish_note", False))


async def _quick_refresh(collector, run: "Run", ts: int, out: dict) -> None:
    """Bildirilmiş ⚡ HIZLI emrin durumu (iptal mi?): eski yolun emir önbelleğine YAZMADAN tek sorgu (önbellek
    ilk alarm kararlarını ve 🧪 LOG-TWAP'ı besler — sıklaşan sorgu onu değiştirmesin)."""
    fn = getattr(collector, "fetch_twap_history", None)
    run.lookup_ts = ts
    if fn is None:
        return
    out["quick_lookups"] = out.get("quick_lookups", 0) + 1
    try:
        hist = await fn(run.address)
    except Exception:                                  # noqa: BLE001
        hist = None
    if hist is None:
        return
    live_q_put(run.address, ts, hist)                  # /twap: aynı emrin öteki kopyaları da iptali görsün
    orders = parse_twap_orders(hist, run.coin, run.side, run.px_last, ts)
    started = int((run.order or {}).get("started_ts") or 0)
    got = next((o for o in orders if started and abs(int(o.get("started_ts") or 0) - started) <= 2), None)
    if got is not None:
        got["q_ts"] = ts
        run.order = got


def mirror_chat(cfg) -> str:
    return (getattr(cfg, "twap_mirror_chat_id", "") or "").strip()


MIRROR: dict = {"sent": 0, "failed": 0, "last_fail_ts": 0, "last_ok_ts": 0}


async def mirror(cfg, notifier, text: str, key: str, coin: str = "", out: dict | None = None,
                 primary: str | None = None) -> bool | None:
    """TWAP ayna grubu (TWAP_MIRROR_CHAT_ID): her TWAP mesajının kopyası. Ana gönderimden BAĞIMSIZ —
    sonucu ana yolun işaretini, tekrar denemesini, sayaçlarını değiştirmez. Kendi tekrar kaydı
    (alerts_log 'twap_mirror', 24 sa): ana yol başarısız olup her tur yeniden denese de kopya BİR kez
    gider. Boş id → hiçbir şey (bot.send boş id'de ana sohbete düşerdi). Fan-out yok (public=False)."""
    chat = mirror_chat(cfg)
    if not chat or notifier is None or not text:
        return None
    if primary is not None and chat == ((primary or "").strip() or (getattr(cfg, "telegram_chat_id", "") or "").strip()):
        return None                                    # ayna = ana hedef → aynı sohbete ikinci kopya olmaz
    ok = False
    try:
        if await alert_recent("twap_mirror", key, 86400):
            return None
        ok = bool(await notifier.send("twap_mirror", text, priority="high", key=f"mirror:{key}",
                                      chat_id=chat, coin=coin, public=False))
        if ok:
            await alert_log("twap_mirror", key, "")
    except Exception:                                  # noqa: BLE001 — ayna asla ana yolu düşürmez
        log.debug("twap ayna gönderimi %s", key, exc_info=True)
        ok = False
    MIRROR["sent" if ok else "failed"] += 1
    MIRROR["last_ok_ts" if ok else "last_fail_ts"] = now()
    if out is not None:
        k = "mirror_sent" if ok else "mirror_failed"
        out[k] = out.get(k, 0) + 1
    return ok


VOL_CACHE: dict = {"ts": 0, "v": {}}       # son tur hacimleri — hızlı yol aynı sorguyu tekrarlamasın


async def volumes_cached(client=None, max_age: int = 300) -> dict:
    if VOL_CACHE["v"] and 0 <= now() - VOL_CACHE["ts"] < max_age:
        return VOL_CACHE["v"]
    v = await volumes(None)          # SALT OKUNUR: hızlı yol paylaşılan hacim kaydını (kv) tazelemez
    VOL_CACHE.update(ts=now(), v=v)
    return v


SENDING: set[str] = set()   # emir anahtarı (coin:adres:yön:başlangıç) — şu an gönderiliyor (iki yol aynı emri iki kez yazmasın)
ENDING: set[str] = set()    # aynı emrin 🏁/⛔ notu şu an yazılıyor (iki yol aynı anda yazmasın)
END_TRIES: dict[str, int] = {}
END_MAX_TRIES = 3           # bitiş/iptal notu ana hedefe gidemezse sonraki turlarda bu kadar dene


def order_key(key: str, order: dict | None) -> str | None:
    started = int((order or {}).get("started_ts") or 0)
    return f"{key}:{started}" if started else None


# ---------------- değerlendirme turu ----------------

def _slice_could_pass(cfg, run: "Run", ts: int, sat_need: set | None = None) -> bool:
    """Gözlenen toplam `twap_lookup_min_usd`'nin ($50K) altındaki HL TWAP dizisi de 10 dilim / 5 dk sonra sorulsun mu:
    ortalama dilim, ≤ 24 sa'lik bir emirde ana / 🔸 / 🕐 kapılarından birini geçebilecek büyüklükte mi (ilk dilim
    yolunun ön süzgeciyle aynı hesap; G/Ç yok, hacim son turun önbelleğinden). Kullanıcı 14.10: TRX 6,5 sa $564K emir
    (~$721 dilim) $50K'yı bekleyip 36 dk geç geldi. İlk dilim tabanının altındaki dilim de (ilk dilim yolu onu hiç
    sormaz) buradan sorulur — yalnız Cumartesi hissede taban henüz okunmamışken ön süzgeç her dilimi geçirir: o zaman
    taban altı ortalama yalnız 🕐 hafta sonu kuralıyla (`sat_need`: tabanı bu turda okunacak coinler — kısıt geçici)."""
    if not run.nat_n or run.n <= 0:
        return False                                   # HL TWAP alt emri değil (🕐 gizli TWAP kendi tabanıyla)
    from . import twapfast, twaploose
    avg = run.total / run.n
    if sat_missing(cfg, run.coin, ts) and avg < float(getattr(cfg, "twap_fast_min_slice_usd", 250) or 0):
        if sat_need is not None:
            sat_need.add(run.coin)
        fl = twaploose.big_slice_floor(cfg, run.coin, ts) if twaploose.BIG.enabled(cfg) else None
        return fl is not None and avg >= fl and twaploose.big_pre(cfg, avg, run.coin, ts)
    vol = (VOL_CACHE.get("v") or {}).get(run.coin)
    ok = twapfast.prefilter(cfg, run.coin, avg, vol, ts) == ""
    if not ok and twaploose.enabled(cfg):
        ok = twapfast.prefilter(twaploose.loose_cfg(cfg), run.coin, avg, vol, ts) == ""
    if not ok and twaploose.BIG.enabled(cfg):
        ok = twaploose.big_pre(cfg, avg, run.coin, ts)
    return ok


def _vol_pct_24h(det: dict):
    """Emir / ham 24s hacim (%) — Cumartesi tabanından bağımsız (🧪 özellikleri için)."""
    dv = det.get("day_vol_24h") if "day_vol_24h" in det else det.get("day_vol")
    try:
        return (float(det.get("planned") or 0) / float(dv) * 100) if dv and float(dv) > 0 else None
    except (TypeError, ValueError):
        return None


def _lab_twap(run: "Run", order: dict, det: dict, gate: str, ts: int, lookup_min: float = 0.0,
              min_slices: int = 0) -> bool:
    """🧪 Laboratuvar kaydı: HL'de doğrulanmış TWAP emri, kapı sonucu ne olursa olsun (kapıdan kalan
    = kontrol grubu). Senkron, G/Ç yok, asla fırlatmaz. Bekleme içindeki tekrar emir kayda girmez
    (emir sorgusu yapılmıyor) — sayfada yazılı sınır."""
    try:
        from ..lab.registry import emit
        return emit("LOG-TWAP", run.coin, ts, side=1 if run.side == "buy" else -1, px_ref=run.px_last,
                    trig_key=f"{run.address}:{int(run.first_ts)}",
                    # 🧪 kural eşikleri ham 24s ölçüsüne bağlı (AKI-TWAP-F "24s hacmin ≥ %5'i"): Cumartesi hisse tabanı
                    # özelliği DEĞİŞTİRMEZ, ayrı alanda (sat, vol_pct_sat)
                    features={"plan": round(det.get("planned") or 0), "left": order.get("remaining_usd"),
                              "vol_pct": _vol_pct_24h(det), "sat": int(bool(det.get("sat"))),
                              "vol_pct_sat": det.get("vol_pct") if det.get("sat") else None,
                              "n": run.n, "dur": int(run.last_ts - run.first_ts),
                              "seen": round(run.total), "kapi": gate, "st": order.get("status"),
                              "lk_min": lookup_min, "min_sl": min_slices},
                    gated=gate in ("ok", "big"))
    except Exception:                                  # noqa: BLE001
        return False


async def evaluate(cfg, notifier, client=None, collector=None) -> dict:
    """Bir tur: budama → adaylar → düzenlilik → EMİR SORGUSU → kapı → bildirim;
    bildirilmiş turlar: 10 dk'da bir yeniden sorgu → yarı dolunca ilerleme,
    bitince/iptalde bitiş notu (gerçek tutarla). Tahmin yok."""
    ts = now()
    window = int(getattr(cfg, "twap_live_window_min", 240) or 240) * 60
    out = {"keys": len(REG.runs), "observed": REG.observed, "errors": REG.errors, "cands": 0,
           "regular": 0, "irregular": 0, "lookups": 0, "lookup_fail": 0, "alerted": 0, "progress": 0,
           "ended": 0, "skipped_mm": 0, "no_chat": 0, "no_order": 0, "order_done": 0, "order_small": 0,
           "order_left": 0, "no_vol": 0, "spot_thin": 0, "vol_small": 0, "no_lookup": 0, "failed": 0, "best": None,
           "decisions": []}
    decisions: list[dict] = out["decisions"]
    big_q: list[tuple] = []         # 🕐 büyük kanal adayları — turun SONUNDA (ana kararlardan sonra, onlardan bağımsız)
    from . import twaploose as _tlo
    big_on = _tlo.BIG.enabled(cfg)

    def decide(run: "Run", reason: str, detail: dict | None = None) -> None:
        """Bu turun aday kararı — /tani "son elenenler" ve /twap komutu bunu okur."""
        d = {"coin": run.coin, "addr": run.address, "side": run.side, "n": run.n,
             "dur": int(run.last_ts - run.first_ts), "total": round(run.total),
             "reason": reason, "ts": ts}
        if detail:
            d.update({k: detail.get(k) for k in ("planned", "left", "vol_pct", "pct_needed", "need_usd", "status",
                                                  "sat")})
        decisions.append(d)
    if not getattr(cfg, "twap_live_enabled", True):
        out["skipped"] = "kapalı"
        await kv_set(STATS_KV, {**out, "ts": ts})
        return out
    out["pruned"] = REG.prune(ts, window)
    out["keys"] = len(REG.runs)
    min_slices = int(getattr(cfg, "twap_alert_min_slices", 10) or 0)
    lookup_min = float(getattr(cfg, "twap_lookup_min_usd", 50_000) or 0)
    early: set = set()                                 # $50K beklenmeden aday olanlar (/tani: gerçekten sorulanlar)
    sat_need: set = set()                              # Cumartesi tabanı yüzünden bu tur elenenlerin coinleri

    def _early(r) -> bool:
        if _slice_could_pass(cfg, r, ts, sat_need):
            early.add(r.key)
            return True
        return False

    cands = [r for r in REG.runs.values()
             if r.alerted_ts or (r.n >= min_slices and r.last_ts - r.first_ts >= LOOKUP_MIN_DUR
                                 and (r.total >= lookup_min or _early(r)))]
    out["cands"] = len(cands)
    sample = getattr(collector, "twap_sample", None) if collector is not None else None
    if sample:
        out["sample"] = str(sample)[:600]
    if collector is not None:
        for k in ("ok", "timeout", "err"):
            out[f"lookups_{k}"] = int(getattr(collector, f"twap_lookups_{k}", 0) or 0)
    if sat_need:
        # aday olmasa da tabanı okunsun: yoksa Cumartesi boyunca taban hiç gelmez, taban altı dizi hiç erken sorulmaz
        try:
            await asyncio.wait_for(ensure_sat(cfg, client, sorted(sat_need), ts), SAT_TIMEOUT * 2)
        except Exception:                              # noqa: BLE001
            log.debug("Cumartesi tabanı (elenen diziler)", exc_info=True)
    if not cands:
        await kv_set(STATS_KV, {**out, "ts": ts})
        return out
    vols = await volumes(client)
    VOL_CACHE.update(ts=ts, v=vols)
    try:
        await asyncio.wait_for(ensure_sat(cfg, client, [r.coin for r in cands], ts),   # Cumartesi hisse: geçen Cmt
                               SAT_TIMEOUT * 2)
    except Exception:                                  # noqa: BLE001
        log.debug("Cumartesi tabanı", exc_info=True)
    ents = await entities(list({r.address for r in cands}))
    from ..telegram import format as fmt
    for run in cands:
        try:
            m = measure(run) if run.slices else None
            key = f"{run.coin}:{run.address}:{run.side}"
            # --- bildirilmiş tur: yeniden sorgu → ilerleme / bitiş ---
            if run.alerted_ts and not run.ended_ts:
                cur = run.order or {}
                if collector is not None and cur.get("status") == "activated" and is_quick(cfg, cur) \
                        and ts - int(run.lookup_ts or 0) >= QUICK_REFRESH:
                    await _quick_refresh(collector, run, ts, out)
                elif collector is not None and ts - int(run.lookup_ts or 0) >= REFRESH_SEC:
                    prev = (cur.get("started_ts"), cur.get("status"))
                    await lookup_order(collector, run, ts, cfg, out, force=True)
                    if run.order and (run.order.get("started_ts"), run.order.get("status")) != prev:
                        await persist(run, m)             # twap_runs iptal edilmiş eski emri 'activated' tutmasın
                order = run.order
                mm = m or _scalar_measure(run)
                if big_on and order and order.get("status") == "activated" and native_overlap([order], run):
                    big_q.append((run, mm, order, None, None))   # 🕐: ana kanalın bildirdiği adresin (yeni) emri de
                active = bool(order and order.get("status") == "activated")
                done = bool(order and order.get("status") in ("finished", "terminated", "error"))
                if done or (is_ended(run, mm, ts) and not active):
                    # Sessizlikten "bitti" sayıyorsak elimizdeki emir BAYAT olabilir:
                    # araya giren bir iptali "🏁 bitti" diye yazmak yanlış olurdu.
                    # Bitiş notundan ÖNCE bir kez daha zorla sor (adres önbelleğini
                    # atlar). Emir geçmişten tamamen düştüyse eski davranış sürer:
                    # başlık 🏁, gövde "gördüğümüz toplam" — plan iddia edilmez.
                    if not done and collector is not None:
                        await lookup_order(collector, run, ts, cfg, out, force=True)
                        order = run.order
                        st = (order or {}).get("status")
                        done = st in ("finished", "terminated", "error")
                    run.ended_ts = ts
                    out["ended"] += 1
                    vol = vols.get(run.coin)
                    if vol:
                        run.day_volume = vol[0]
                    eo = order_key(key, order)
                    ekey = f"{key}:{run.first_ts}"
                    if end_note_wanted(cfg, (order or {}).get("status") in ("terminated", "error")):
                        claim = eo or ekey
                        if claim in ENDING:               # hızlı yol bu emrin notunu ŞU AN yazıyor — sonraki tur bakar
                            run.ended_ts = None
                            out["ended"] -= 1
                            continue
                        ENDING.add(claim)
                        try:
                            if not await alert_recent("twap_end", ekey, 86400) and not (
                                    eo and await alert_recent("twap_endo", eo, 86400)):   # hızlı yol yazdıysa yok
                                chat, can = chat_for(cfg, run.coin)
                                ctx = {"order": order, "day_vol": run.day_volume, "klass": klass_of(run.coin),
                                       "cancelled": (order or {}).get("status") in ("terminated", "error"),
                                       "quick_max": quick_max(cfg)}
                                text = fmt.twap_end(mm, ctx)
                                if can:
                                    ok = await notifier.send("twap", text, priority="high", coin=run.coin,
                                                             key=f"twap_end:{key}", chat_id=chat)
                                    if not ok and not (not chat and in_quiet_hours(cfg)):
                                        out["failed"] += 1
                                        tries = END_TRIES[ekey] = END_TRIES.get(ekey, 0) + 1
                                        if tries < END_MAX_TRIES:   # işaret YOK: sonraki tur yeniden dener
                                            run.ended_ts = None
                                            out["ended"] -= 1
                                            continue
                                END_TRIES.pop(ekey, None)
                                await alert_log("twap_end", ekey, "")
                                if eo:
                                    await alert_log("twap_endo", eo, "")
                                await mirror(cfg, notifier, text, f"end:{eo}" if eo else f"end:{ekey}", run.coin, out,
                                             primary=chat if can else None)
                                await _loose_copy(cfg, notifier, text, f"e:{eo or ekey}", run.coin,
                                                  chat if can else None, okey=eo, alias=f"run:{key}:{run.first_ts}")
                        finally:
                            ENDING.discard(claim)
                    await persist(run, m)
                    continue
                if (order and active and not run.half_ts and getattr(cfg, "twap_alert_progress", True)
                        and (order.get("filled_pct") or 0) >= HALF_PCT):
                    run.half_ts = ts
                    pkey = f"{key}:{run.first_ts}:{HALF_PCT}"
                    if not await alert_recent("twap_prog", pkey, 86400):
                        await alert_log("twap_prog", pkey, "")
                        chat, can = chat_for(cfg, run.coin)
                        ctx = {"order": order, "day_vol": run.day_volume, "klass": klass_of(run.coin),
                               "quick_max": quick_max(cfg)}
                        text = fmt.twap_progress(mm, ctx)
                        if can:
                            ok = await notifier.send("twap", text, priority="high", coin=run.coin,
                                                     key=f"twap_prog:{key}", chat_id=chat)
                            out["progress"] += 1
                            if not ok and not (not chat and in_quiet_hours(cfg)):
                                out["failed"] += 1
                        await mirror(cfg, notifier, text, f"prog:{pkey}", run.coin, out,
                                     primary=chat if can else None)
                        await _loose_copy(cfg, notifier, text, f"p:{pkey}", run.coin, chat if can else None,
                                          okey=order_key(key, order), alias=f"run:{key}:{run.first_ts}")
                    await persist(run, m)
                continue
            # --- ilk alarm: düzenli dizi → emir sorgusu → kapı ---
            if not m:
                out["irregular"] += 1
                decide(run, "irregular")
                continue
            out["regular"] += 1
            if ents.get(run.address) in ("mm", "vault"):
                out["skipped_mm"] += 1
                decide(run, "mm")
                continue
            # Bekleme ('twap' adres + coin + yön, 6 sa): dizi HL TWAP'ına aitse benimsenir, tekrar sayılmaz. Adres
            # aynı yönde KENDİ BOTUYLA dilimliyorsa (HL alt emri dolumu yok) önce emir sorulur: örtüşen HL emri
            # varsa eskisi gibi benimse; yoksa dizi kapıya / 🕵️ gizli TWAP'a gider — eski emir ona bağlanmaz.
            cool = await alert_recent("twap", key, int(getattr(cfg, "twap_alert_cooldown", 21600) or 0))
            if cool and (run.nat_n or collector is None):
                run.alerted_ts = run.alerted_ts or ts        # bekleme içinde: tekrar sayılmaz
                run.alert_total = run.alert_total or run.total
                decide(run, "cooldown")
                continue
            if collector is None:
                out["no_lookup"] += 1                     # emir doğrulanamaz → tahminle bildirim YOK
                decide(run, "no_lookup")
                continue
            lf_before = out.get("lookup_fail", 0)
            lc_before = out.get("lookup_capped", 0)
            lk_before = out.get("lookups", 0)
            order = await lookup_order(collector, run, ts, cfg, out)
            if run.key in early and out.get("lookups", 0) > lk_before:
                out["early"] = out.get("early", 0) + 1       # önbellekten değil: bu tur HL'ye soruldu
            if cool and order and native_overlap([order], run):
                # Bu turun sorgusu (taze ya da önbellek) örtüşen HL emri gösteriyor: benimse, tekrar sayılmaz. Sorgu
                # düştü / tavana takıldıysa BENİMSEME (alerted_ts kalıcıdır — eski emir bot dizisine yapışırdı):
                # kapı lookup_fail / lookup_capped yazar, gizli iddia da yok; sonraki tur yeniden sorar.
                run.alerted_ts = run.alerted_ts or ts
                run.alert_total = run.alert_total or run.total
                decide(run, "cooldown")
                continue
            vol = vols.get(run.coin)
            day_vol, vol_ts = (vol if vol else (None, None))
            det = gate_detail(cfg, run.coin, order, day_vol, vol_ts, ts)
            g = det["reason"]
            planned = det["planned"]
            if order and ((not out["best"]) or planned > out["best"]["planned"]):
                out["best"] = {"coin": run.coin, "side": run.side, "planned": planned,
                               "left": (order or {}).get("remaining_usd"), "status": order.get("status"),
                               "vol_pct": det["vol_pct"]}
            # Dizi başlamadan çok önce bitmiş eski emir BU dizinin emri değil (🕵️ gizli TWAP olabilir): ne 🧪
            # LOG-TWAP ne /twaplar kaydı ona bağlanır — yoksa aynı dizi hem "emirsiz" hem "$4M HL TWAP" görünürdü.
            own = bool(order) and native_overlap([order], run)
            if own:
                _lab_twap(run, order, det, g, ts, lookup_min, min_slices)   # 🧪 kapıdan ÖNCE: geçen + kalan
                big_q.append((run, m, order, det, day_vol))
            if g not in ("ok", "big"):
                out[g] = out.get(g, 0) + 1
                failed_lookup = g == "no_order" and out.get("lookup_fail", 0) > lf_before
                capped = out.get("lookup_capped", 0) > lc_before     # bu tur sorulmadı: emir YOK sayılamaz
                decide(run, "lookup_fail" if failed_lookup else ("lookup_capped" if capped and not order else g), det)
                if own and not failed_lookup and not capped:
                    lres = await _loose_extra(cfg, notifier, client, run, m, order, g, det, day_vol, vol_ts, ents, ts)
                    if lres != "off":
                        decisions[-1]["loose"] = lres     # 🔸 yan kanal (gevşek eşik) — ana yolun durumu değişmez
                if (g == "no_order" or (g == "order_done" and not own)) and not failed_lookup and not capped \
                        and not run.nat_n:
                    # 🕵️ Emir YOK ya da dizi başlamadan bitmiş eski emir (sorgu başarılı): kendi botuyla dilimleme —
                    # gizli TWAP radarı devralır; iddiadan önce TAZE geçmişle doğrular (verify_hidden).
                    # Bu dalın eski davranışı (karar, sayaç, 'twap' işareti yok) DEĞİŞMEZ.
                    try:
                        from . import hiddentwap
                        decisions[-1]["hidden"] = await hiddentwap.consider(
                            cfg, notifier, client, run, m, day_vol, ents, ts, out, vol_ts=vol_ts,
                            verify=lambda: verify_hidden(collector, run, ts, cfg, out))
                    except Exception:
                        log.exception("gizli twap değerlendirme hatası %s", key)
                # HL'de GERÇEK bir TWAP emri bulunduysa kapıdan geçmese de kaydet:
                # /twaplar sekmesi "tüm TWAP emirleri"ni gösteriyor, yalnız kanala
                # düşenleri değil. Bildirim alanları (alerted_ts) boş kalır — sayfa
                # bildirilmiş ile yalnız görülmüşü böyle ayırır.
                if run.order and native_overlap([run.order], run):   # verify_hidden'ın zorla sorgusu tazelemiş olabilir
                    run.day_volume = day_vol
                    await persist(run, m)
                    out["seen"] = out.get("seen", 0) + 1
                continue
            chat, can = chat_for(cfg, run.coin)
            run.alerted_ts, run.alert_total, run.gate = ts, run.total, g
            run.day_volume = day_vol
            okey = order_key(key, order)
            if okey and okey in SENDING:
                # ⚡ hızlı yol TAM ŞU AN bu emri gönderiyor: karar sonraki turda (gitti → aşağıdaki dal; düştü → biz)
                run.alerted_ts = None
                out["fast_inflight"] = out.get("fast_inflight", 0) + 1
                continue
            if okey:
                SENDING.add(okey)                         # talep: hızlı yol bu emri şimdi gönderemez (await yok arada)
            try:
                if okey and await alert_recent("twap_fast", okey, 86400):
                    # ⚡ BU EMİR ilk dilimde zaten ana hedefe bildirildi (+ ayna). 🧪 kayıt yukarıda eski yolun kendi
                    # anında yapıldı; tekrar mesaj yok. 'twap' işareti eski yolun gönderseydi yazacağı gibi YALNIZ
                    # ana kanal varken. Bundan sonra yarı/bitiş notları eski yoldan (bitiş emir anahtarıyla tekil).
                    if can:
                        await alert_log("twap", key, "")
                    out["fast_already"] = out.get("fast_already", 0) + 1
                    decide(run, "fast", det)
                    await persist(run, m)
                    continue
                mirror_on = bool(mirror_chat(cfg))
                if not can and not mirror_on:
                    out["no_chat"] += 1
                    decide(run, "no_chat", det)
                    # 🔸 ana eşiği geçen emir, ana hedef / ayna tanımsız → yan kanala (ilk dilim yolundaki gibi)
                    lres = await _loose_extra(cfg, notifier, client, run, m, order, "no_chat", det, day_vol, vol_ts,
                                              ents, ts, claimed=True)
                    if lres != "off":
                        decisions[-1]["loose"] = lres
                    if lres in ("failed", "backoff", "inflight", "error"):
                        run.alerted_ts = None             # yan kanala gidemedi: sonraki turlarda yeniden (5 dk geri çekilme)
                    await persist(run, m)
                    continue
                if can and not chat:
                    await alert_log("twap", key, "")          # hisse yolu: sessiz saat özeti için işaret ÖNCE
                pos = await position_ctx(client, run.coin, run.address, wallet=wallet_on(cfg))
                # Takip teklifi (/takip_N): kullanıcı basarsa BU EMİR izlenir ve iptal
                # edilince haber gider. Teklif yazılamazsa mesaj eskisi gibi gider —
                # düğme süs değil ama mesajı bloke de etmez.
                offer = None
                if getattr(cfg, "twap_follow_enabled", True) and not auto_end(cfg, order):   # ⚡: kanala otomatik
                    try:
                        offer = await _twap_offer(run)
                    except Exception:
                        log.debug("twap takip teklifi yazılamadı (%s)", run.coin, exc_info=True)
                wo = with_seen(order, run.coin, run.address, run.side, ts)
                ctx = {"order": wo,
                       "day_vol": det["day_vol"] if det.get("sat") else day_vol,
                       "vol_pct": det["vol_pct"], "sat": det.get("sat"),
                       "pos": pos, "gate": g, "klass": klass_of(run.coin), "entity": ents.get(run.address) or "",
                       "offer": offer, "quick_max": quick_max(cfg), "auto_end": auto_end(cfg, order),
                       "low_bal": low_bal_ctx(cfg, run.coin, run.side, wo, pos)}
                text = fmt.twap_alert(m, ctx)
                mkey = f"alert:{key}:{run.first_ts}"
                if not can:                                   # ana kanal tanımsız (kripto) — yalnız ayna; durum eskisi gibi
                    out["no_chat"] += 1
                    if await mirror(cfg, notifier, text, mkey, run.coin, out):
                        low_bal_mark(ctx.get("low_bal"), run.coin, okey or mkey)
                    await _loose_copy(cfg, notifier, text, f"a:{okey or mkey}", run.coin, None, okey=okey,
                                      alias=f"run:{key}:{run.first_ts}")
                    decide(run, "no_chat", det)
                    await persist(run, m)
                    continue
                ok = await notifier.send("twap", text, priority="high", key=f"twap:{key}", chat_id=chat, coin=run.coin)
                if ok:
                    await alert_log("twap", key, text)        # talep bırakılmadan önce: hızlı yol bekleme işaretini görsün
                    if okey:
                        await alert_log("twap_o", okey, "")   # 📣 (/twap): bildirilen EMİR (aynı anahtarın başka emri değil)
                mok = await mirror(cfg, notifier, text, mkey, run.coin, out, primary=chat)
                if ok or mok:                                 # ⚠️ bir hedefe gerçekten ulaştı (sessiz saat ertelemesi değil)
                    low_bal_mark(ctx.get("low_bal"), run.coin, okey or mkey)
                await _loose_copy(cfg, notifier, text, f"a:{okey or mkey}", run.coin, chat, okey=okey,
                                  alias=f"run:{key}:{run.first_ts}")
                if ok:
                    out["alerted"] += 1
                    decide(run, "alerted", det)
                    log.info("canlı twap: %s %s %s emir %.0f$ kalan %.0f$ (%s) bildirildi", run.coin, run.side,
                             run.address[:10], planned, float(order.get("remaining_usd") or 0), g)
                elif not chat and in_quiet_hours(cfg):
                    out["quiet"] = out.get("quiet", 0) + 1          # sabah özetine bırakıldı, hata değil
                    decide(run, "alerted", det)
                else:
                    # Kripto yolu: işaret yazılmadı → 6 saatlik bekleme yanmaz, sonraki tur yeniden dener
                    out["failed"] += 1
                    run.alerted_ts = None
                    decide(run, "failed", det)
                    await alert_log("fail:twap", key, text)
                await persist(run, m)
            finally:
                if okey:
                    SENDING.discard(okey)
        except Exception:
            out["errors"] += 1
            log.exception("canlı twap değerlendirme hatası %s", getattr(run, "key", "?"))
    if big_q:
        from . import twaploose
        if twaploose.BIG.enabled(cfg):
            for run, m, order, det, day_vol in big_q:
                try:
                    if twaploose.big_gate(cfg, order, ts, run.coin) not in twaploose.BIG_PASS:
                        continue                        # her tur bakılır: kural dışı emirde ölçü hesaplanmaz
                    okey = order_key(f"{run.coin}:{run.address}:{run.side}", order)
                    if okey and f"a:{okey}" in twaploose.BIG.sent:
                        continue
                    if det is None:                     # bildirilmiş turdan: ölçü (hacme oran) burada
                        vol = vols.get(run.coin)
                        day_vol = vol[0] if vol else None
                        det = gate_detail(cfg, run.coin, order, day_vol, vol[1] if vol else None, ts)
                    await twaploose.big_slow(cfg, notifier, client, run, m, order, det, day_vol, ents, ts)
                except Exception:                      # noqa: BLE001
                    log.exception("🕐 büyük twap (eski yol) %s:%s", run.coin, run.address[:10])
    try:
        from . import hiddentwap
        await hiddentwap.save_stats()
    except Exception:
        log.debug("gizli twap istatistiği", exc_info=True)
    decisions.sort(key=lambda d: -(d.get("planned") or d.get("total") or 0))
    del decisions[DECISIONS_MAX:]
    if decisions:
        try:
            await _remember(decisions)
        except Exception:
            log.debug("twaplive_last yazılamadı", exc_info=True)
    await kv_set(STATS_KV, {**out, "ts": ts})
    return out


async def _loose_copy(cfg, notifier, text: str, dkey: str, coin: str, primary, okey: str | None = None,
                      alias: str | None = None) -> None:
    """🔸 yan kanala kopya (twaploose.copy) — asla fırlatmaz, ana yolun sonucunu değiştirmez. Bitiş / yarı notunda
    `okey`: yalnız ilk alarmı yan kanalda görülmüş emir."""
    try:
        from . import twaploose
        await twaploose.copy(cfg, notifier, text, dkey, coin, primary=primary, okey=okey, alias=alias)
    except Exception:                                  # noqa: BLE001
        log.debug("twap yan kanal kopyası %s", dkey, exc_info=True)


async def _loose_extra(cfg, notifier, client, run: "Run", m, order, g, det, day_vol, vol_ts, ents, ts,
                       claimed: bool = False) -> str:
    """🔸 ana kapıdan kalan (ya da ana hedefi tanımsız, g='no_chat') emir → gevşek kapı → yan kanal
    (twaploose.slow_extra). `claimed`: emrin SENDING talebi bu turda bizde. Asla fırlatmaz."""
    from . import twaploose
    if not twaploose.enabled(cfg) or not order or not m:
        return "off"
    try:
        return await twaploose.slow_extra(cfg, notifier, client, run, m, order, g, det, day_vol, vol_ts, ents, ts,
                                          claimed=claimed)
    except Exception:                                  # noqa: BLE001
        log.exception("twap yan kanalı (eski yol) %s:%s", run.coin, run.address[:10])
        return "error"


async def _remember(decisions: list[dict]) -> None:
    """Son kararları biriktir (aynı coin+adres+yön için en yenisi kalır, en çok LAST_MAX)."""
    last = await kv_get(LAST_KV) or []
    seen = set()
    merged = []
    for d in list(decisions) + list(last):
        k = (d.get("coin"), d.get("addr"), d.get("side"))
        if k in seen:
            continue
        seen.add(k)
        merged.append(d)
    await kv_set(LAST_KV, merged[:LAST_MAX])


def decision_line(d: dict) -> str:
    """Tek karar, düz metin: 'PUMP 0x60b0…991e buy $3.0M emir, hacmin %4.8'i → hacme göre küçük (%5 gerekir)'."""
    from ..telegram.format import short, usd
    r = d.get("reason") or "?"
    txt = f"{assets.label(d.get('coin') or '')} {short(d.get('addr') or '')} {d.get('side')}"
    if d.get("planned"):
        txt += f" {usd(d['planned'])} emir"
        if d.get("vol_pct") is not None:
            txt += f", hacmin %{float(d['vol_pct']):.1f}'i"
    else:
        txt += f" {d.get('n', 0)} dilim {usd(d.get('total') or 0)}"
    txt += f" → {REASON_TR.get(r, r)}"
    if r == "vol_small" and d.get("pct_needed") is not None:
        txt += f" (%{float(d['pct_needed']):g} gerekir)"
    elif r == "order_left" and d.get("left") is not None:
        txt += f" (kalan {usd(d['left'])})"
    if d.get("sat") and r != "order_left":
        txt += " · taban: geçen Cumartesi hacmi"
    if d.get("hidden"):
        from .hiddentwap import REASON_TR as HR
        txt += f" · 🕵️ gizli twap: {HR.get(d['hidden'], d['hidden'])}"
    if d.get("loose"):
        from .twaploose import REASON_TR as LR
        txt += f" · 🔸 yan kanal: {LR.get(d['loose'], REASON_TR.get(d['loose'], d['loose']))}"
    return txt


# ---------------- teşhis metinleri (/twap komutu — bot'tan bağımsız, test edilir)

async def _marks() -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        from ..hl.universe import main_dex_ctx
        ctx = await main_dex_ctx(None, fetch=False)
        for c, v in (ctx.get("c") or {}).items():
            if v.get("m"):
                out[c] = float(v["m"])
    except Exception:
        pass
    return out


async def _twap_offer(run: "Run") -> int:
    """Mesajdaki /takip_N için teklif satırı. `track_offers` tablosu paylaşılır;
    `kind='twap'` bunun POZİSYON değil EMİR takibi olduğunu söyler (bot komutu
    ona göre dallanır). `ref_ts` = turun first_ts'i — takip edilecek tur bu."""
    async with db() as conn:
        cur = await conn.execute(
            """INSERT INTO track_offers(address,coin,symbol,side,notional,created_ts,kind,ref_ts)
               VALUES(?,?,?,?,?,?,'twap',?)""",
            (run.address, run.coin, assets.label(run.coin), run.side,
             float(run.total or 0), now(), int(run.first_ts)))
        return int(cur.lastrowid)


def _runs_for(pred) -> list["Run"]:
    return sorted((r for r in REG.runs.values() if pred(r)), key=lambda r: -r.total)


def live_runs(coin: str) -> list[dict]:
    """Bellekte OLUŞMAKTA olan turlar (coin sayfası paneli için) — SENKRON, I/O yok.

    `twap_runs` tablosu bir turu ancak emir sorgusu yapıldığında (alarm / kapı /
    bitiş yollarında `persist`) yazar. Henüz o eşiğe gelmemiş dizi yalnız bellekte
    (`REG`) durur; panel onları da göstersin ki "canlı TWAP" gerçekten canlı olsun.
    Satır şekli `twap.all_orders` ile uyumlu tutulur; çağıran DB satırlarıyla
    (coin, address, side) üzerinden tekleştirir.

    DİKKAT: `twap_live_enabled` kapalıysa ya da yeniden başlatmadan hemen sonra
    bu liste BOŞTUR — sayfa bunu künyede söyler, "TWAP yok" diye yalan söylemez."""
    out = []
    for r in _runs_for(lambda x: x.coin == coin):
        o = r.order if (r.order and native_overlap([r.order], r)) else {}   # dizi öncesi bitmiş emir onun değil
        out.append({
            "coin": r.coin, "address": r.address, "side": r.side,
            "first_ts": r.first_ts, "last_ts": r.last_ts,
            "n_slices": r.n, "total": r.total, "sz_total": r.sz_total,
            "avg_slice": (r.total / r.n) if r.n else 0.0,
            "avg_gap": ((r.last_ts - r.first_ts) / (r.n - 1)) if r.n > 1 else 0,
            "day_volume": r.day_volume, "src": "live",
            "planned_usd": o.get("planned_usd"), "executed_usd": o.get("executed_usd"),
            "remaining_usd": o.get("remaining_usd"), "order_status": o.get("status"),
            "alerted_ts": r.alerted_ts, "ended_ts": r.ended_ts,
            "symbol": assets.label(r.coin), "spot": assets.is_spot(r.coin),
            "size_usd": float(o.get("planned_usd") or 0) or r.total,
            "planned_known": bool(o.get("planned_usd")),
            "filled_pct": ((float(o.get("executed_usd") or 0) / float(o["planned_usd"]) * 100)
                           if o.get("planned_usd") else None),
            "left_sec": None, "account_value": None, "account_ts": None,
            "watchlist": 0, "entity": None,
            "running": not r.ended_ts, "cancelled": False,
            "forming": True,          # henüz kaydedilmedi — tabloda "📡 oluşuyor"
        })
    return out


def _run_line(r: "Run") -> str:
    from ..telegram.format import short, usd
    m = measure(r) if r.slices else None
    dur = int(r.last_ts - r.first_ts)
    reg = f"düzenli ✓ (aralık ~{int((m or {}).get('median_gap') or 0)} sn)" if m else "düzensiz/az"
    flag = " · bildirildi" if r.alerted_ts else ""
    return f"{assets.label(r.coin)} {short(r.address)} {r.side} · {r.n} dilim · {dur // 60} dk · {usd(r.total)} · {reg}{flag}"


async def diag_address(cfg, collector, addr: str) -> str:
    """/twap 0xADRES: bellekteki diziler, HL'deki TWAP emirleri (şimdi sorulur) ve her emrin
    kapı kararı sayılarla, son kararlar."""
    from ..telegram.format import alink, esc, usd
    addr = (addr or "").lower()
    ts = now()
    lines = [f"📡 <b>Canlı TWAP</b> · 👤 {alink(addr)}"]
    runs = _runs_for(lambda r: r.address == addr)
    lines.append("Bellekteki diziler: " + ("\n".join("• " + esc(_run_line(r)) for r in runs[:8]) if runs
                                            else "yok (son 4 saatte bu adresten düzenli akış görmedik)"))
    fn = getattr(collector, "fetch_twap_history", None) if collector is not None else None
    if fn is None:
        lines.append("HL TWAP emirleri: sorgulanamadı (collector/soket yok)")
    else:
        try:
            hist = await fn(addr)
        except Exception as e:
            hist = None
            lines.append(f"HL TWAP emirleri: sorgu hatası ({esc(e)})")
        if hist is None:
            lines.append("HL TWAP emirleri: sorgu başarısız (zaman aşımı / soket)")
        elif not hist:
            lines.append("HL TWAP emirleri: yok (userTwapHistory boş)")
        else:
            marks = await _marks()
            vols = await volumes()
            lines.append(f"HL TWAP emirleri ({len(hist)} kayıt, şimdi soruldu):")
            shown = 0
            for h in latest_per_twap(hist):
                st = (h or {}).get("state") or {}
                coin = str(st.get("coin") or "")
                s = str(st.get("side") or "").upper()
                side = "buy" if (s.startswith("B") or s.startswith("L")) else "sell"
                o = next(iter(parse_twap_orders([h], coin, side, marks.get(coin), ts)), None)
                if not o:
                    continue
                vol = vols.get(coin)
                from . import twapfast
                g, det = twapfast.decide_gate(cfg, coin, o, vol[0] if vol else None, vol[1] if vol else None, ts)
                from ..telegram.format import fill_short
                fill = fill_short(with_seen(o, coin, addr, side, ts))
                lines.append(f"• {esc(assets.label(coin))} {side.upper()} {usd(o['planned_usd'])} · {fill}"
                             f" · {int(o['minutes'])} dk · {esc(o['status'])}")
                kap = f"  kapı: {twapfast.REASON_TR.get(g, g)}"
                if g in ("short", "slow", "short_small", "short_left") and det.get("per_min"):
                    kap += (f" — ⚡ ≤ {quick_max(cfg):g} dk emir: dakikada ~{usd(det['per_min'])}"
                            + (f", ortalama dakika hacminin {det['rate_x']:.1f} katı (gerek"
                               f" {float(getattr(cfg, 'twap_fast_rate_x', 2) or 0):g})" if det.get("rate_x") is not None else ""))
                if g == "short_small":
                    kap += f" — emir < {usd(getattr(cfg, 'twap_fast_short_min_usd', 0))}"
                if det["day_vol"]:
                    kap += f" — hacim {usd(det['day_vol'])}, emir hacmin %{det['vol_pct']:.1f}'i (gerek %{det['pct_needed']:g}, ≥ {usd(det['need_usd'])})"
                if det["reason"] == "order_left":
                    kap += f" — kalan {usd(det['left'])} < {usd(det['left_need'])}"
                if det["reason"] == "spot_thin":
                    kap += f" — spot çiftinin 24s hacmi {usd(det['day_vol'])} < {usd(det['vol_need'])}"
                lines.append(esc(kap))
                shown += 1
                if shown >= 6:
                    break
    last = [d for d in (await kv_get(LAST_KV) or []) if d.get("addr") == addr]
    if last:
        lines.append("Radarın son kararları: " + " · ".join(esc(decision_line(d)) for d in last[:5]))
    return "\n".join(lines)


async def diag_coin(cfg, coin: str) -> str:
    """/twap COIN: dinleniyor mu, 24s hacim, bildirim için gereken emir, coindeki diziler, son kararlar."""
    from ..telegram.format import esc, usd
    _d, _s = (coin or "").strip().rpartition(":")[::2]
    coin = f"{_d.lower()}:{_s.upper()}" if _d else _s.upper()      # 'xyz:TSLA' (dex öneki küçük harf)
    # '/twap PURR/USDC' → '@107': kullanıcı borsa kimliğini bilmek zorunda değil
    coin = assets.spot_coin_of(coin) or coin
    ws = await kv_get("ws_universe") or {}
    spot = assets.is_spot(coin)
    listened = coin in set(ws.get("spot" if spot else "crypto") or [])
    top = int(getattr(cfg, "spot_watch_top" if spot else "crypto_watch_top", 120) or 0)
    where = "spot, hacimce ilk" if spot else "ana dex, hacimce ilk"
    lines = [f"📡 <b>{esc(assets.label(coin))}</b> · " + (f"dinleniyor ✓ ({where} {top})" if listened
                                           else f"dinlenMİyor ✗ (ana dex ilk {top} listesinde değil ya da hisse dex'i)")]
    vols = await volumes()
    vol = vols.get(coin)
    pct = vol_pct_for(cfg, coin)
    min_usd = float(getattr(cfg, "twap_alert_min_usd", 1_000_000) or 0)
    spot_min_vol = float(getattr(cfg, "spot_twap_min_day_vol", 0) or 0)
    if spot and spot_min_vol > 0:
        thin = bool(vol) and vol[0] < spot_min_vol
        lines.append(f"🪙 spot çifti · çiftin 24s hacmi ≥ {usd(spot_min_vol)} olmalı"
                     + (f" — şu an {usd(vol[0])}, <b>taban altı: alarm yok</b>" if thin else " ✓"))
    if sat_applies(cfg, coin, now()):
        sb = eff_vol(cfg, coin, None, None, now())
        lines.append(f"📅 Cumartesi (kripto olmayan): taban geçen Cumartesi hacmi, eşik %{sat_pct(cfg):g}"
                     + (f" — {usd(sb[0])} → bildirim için emir ≥ {usd(max(min_usd, sb[0] * sat_pct(cfg) / 100))}"
                        if sb[2] else " — henüz okunmadı (ilk adayda okunur)"))
    if vol:
        lines.append(f"24s hacim {usd(vol[0])} · bildirim için emir ≥ {usd(max(min_usd, vol[0] * pct / 100))}"
                     f" (max({usd(min_usd)}, %{pct:g} × hacim)) · kalan ≥ min({usd(getattr(cfg, 'twap_alert_min_left_usd', 1e6))}, planın yarısı)")
    else:
        lines.append(f"24s hacim bilinmiyor → kapı geçmez (no_vol); taban {usd(min_usd)}, yüzde %{pct:g}")
    runs = _runs_for(lambda r: r.coin == coin)
    lines.append(f"Bellekteki diziler ({len(runs)}):" + ("\n" + "\n".join("• " + esc(_run_line(r)) for r in runs[:6]) if runs else " yok"))
    last = [d for d in (await kv_get(LAST_KV) or []) if d.get("coin") == coin]
    if last:
        lines.append("Radarın son kararları: " + " · ".join(esc(decision_line(d)) for d in last[:5]))
    return "\n".join(lines)


LIVE_SILENT = 300           # /twap canlı listesi: bu kadar HL alt emri dolumu gelmeyen emir "süren" sayılmaz (~30 sn'de bir)
LIVE_LOOKUPS = 8            # /twap başına en çok bu kadar anlık emir sorgusu (dilimi görülen ama emri bilinmeyen)
LIVE_ASK_PAR = 3            # anlık sorgular aynı anda en çok (HL: IP başına 10 kullanıcı aboneliği; radar yer bulsun)
LIVE_Q_TTL = 120            # anlık sorgu önbelleği (art arda /twap aynı adresi yeniden sormasın)
LIVE_DB_MAX_AGE = 6 * 3600  # kayıtlı (twap_runs) emir: son HL cevabı bundan eskiyse kullanılmaz
_LIVE_Q: dict[str, tuple[int, list]] = {}


def live_q_put(addr: str, ts: int, hist) -> None:
    """/twap'ın emir geçmişi önbelleği (eski yolun `_lookup_cache`'ine DOKUNMAZ — o ilk alarm kararlarını besler).
    ⚡ yenileme, hızlı yolun lifecycle sorgusu ve anlık sorgular yazar: bir kopyanın gördüğü iptali öteki kopya da görür."""
    if hist is None or not addr:
        return
    cur = _LIVE_Q.get(addr)
    if cur is None or int(ts) >= int(cur[0]):
        _LIVE_Q[addr] = (int(ts), hist)
    if len(_LIVE_Q) > 500:
        for k in sorted(_LIVE_Q, key=lambda k: _LIVE_Q[k][0])[:250]:
            _LIVE_Q.pop(k, None)


def _newest_hist(addr: str):
    return max((x for x in (_LIVE_Q.get(addr), _lookup_cache.get(addr)) if x), key=lambda x: int(x[0]), default=None)


def _live_add(out: dict, ts: int, vols: dict, coin, addr, side, o, last_ts, q_ts, alerted, n, seen,
              src: str, cfg=None) -> None:
    """Süren emir satırı: HL 'activated', planlanan bitişi gelmemiş, son LIVE_SILENT sn içinde dilim görülmüş.
    Aynı emir (coin, adres, yön, başlangıç) birden çok kaynakta: en taze sorgu kalır, 📣 birleşir."""
    if not o or o.get("status") != "activated":
        return
    t0 = int(o.get("started_ts") or 0)
    mins = float(o.get("minutes") or 0)
    end = t0 + int(mins * 60) if (t0 and mins) else None
    if (end is not None and end <= ts) or ts - int(last_ts or 0) > LIVE_SILENT:
        return
    planned = float(o.get("planned_usd") or 0)
    dv0, vts0 = vols.get(coin) or (None, None)
    dv, _, sat = eff_vol(cfg, coin, dv0, vts0, ts)
    k = (coin, addr, side, t0)
    if k not in out and t0:                            # aynı emir: başlangıç kaynaktan kaynağa ±2 sn oynayabilir
        k = next((k2 for k2 in out if k2[:3] == k[:3] and abs(int(k2[3]) - t0) <= 2), k)
    o = with_seen(o, coin, addr, side, ts)
    e = {"coin": coin, "addr": addr, "side": side, "planned_usd": planned,
         "executed_usd": o.get("executed_usd"), "filled_pct": o.get("filled_pct"), "minutes": mins,
         "fill_hl0": bool(o.get("fill_hl0")), "fill_seen": o.get("fill_seen"), "fill_full": bool(o.get("fill_full")),
         "fill_seen_sz": o.get("fill_seen_sz"), "planned_sz": o.get("planned_sz"),
         "started_ts": t0, "left_sec": max(0, end - ts) if end is not None else None, "q_ts": int(q_ts or 0),
         "last_ts": int(last_ts or 0), "alerted": bool(alerted), "n": int(n or 0), "seen_usd": float(seen or 0),
         "vol_pct": (planned / float(dv) * 100) if dv else None, "spot": assets.is_spot(coin), "src": src,
         "sat": bool(sat)}
    cur = out.get(k)
    if cur is None:
        out[k] = e
    elif e["q_ts"] > cur["q_ts"] or (e["q_ts"] == cur["q_ts"] and cur["src"] == "db" and src != "db"):
        e["alerted"] = e["alerted"] or cur["alerted"]
        e["last_ts"] = max(cur["last_ts"], e["last_ts"])
        out[k] = e
    else:
        cur["alerted"] = cur["alerted"] or e["alerted"]
        cur["last_ts"] = max(cur["last_ts"], e["last_ts"])


def _fresher(o: dict | None, addr: str, coin: str, side: str, px, ts: int) -> dict | None:
    """Adresin en taze emir geçmişi (eski yolun önbelleği ya da /twap önbelleği) bu emrin son sorgusundan TAZEYSE
    aynı emri (başlangıç ±2 sn) oradan al — iptal edilmiş emir (aynı adres + coin + yönün başka TWAP'ı dilim
    atarken, ya da kayıtta 'activated' kalmış) 'süren' görünmesin. Geçmişte bulunmazsa olduğu gibi. G/Ç yok."""
    if not o:
        return o
    c = _newest_hist(addr)
    if not c or int(c[0]) < int(o.get("q_ts") or 0):     # eşitse de oku: aynı anın geçmişi kaydın kopyasından iyidir
        return o
    t0 = int(o.get("started_ts") or 0)
    for x in parse_twap_orders(c[1], coin, side, px, ts):
        if t0 and abs(int(x.get("started_ts") or 0) - t0) <= 2:
            x["q_ts"] = int(c[0])
            return x
    return o


def _live_memory_cands(ts: int) -> list[tuple]:
    """(coin, adres, yön, emir, son dilim, 📣 geçici, n, görülen, kaynak) — bellekteki kopyalar."""
    out = []
    for r in list(REG.runs.values()):
        o = r.order
        if o and native_overlap([o], r):
            # 'süren' kapısı HL alt emri dolumunun anı: elle / maker işlem iptal edilmiş emri canlı tutmasın
            out.append((r.coin, r.address, r.side, o, r.nat_ts if r.nat_n else 0, r.alerted_ts, r.n, r.total, "mem",
                        r.px_last, r))
    try:
        from . import twapfast
        for _k, f, _old in twapfast.F.records():      # aynı anahtarın süren eski emri de (F.older)
            if not f.get("ended"):
                out.append((f["coin"], f["addr"], f["side"], f.get("order"),
                            max(int(f.get("last_ts") or 0), int(f.get("last_slice") or 0)), True, f.get("n"),
                            f.get("total"), "fast", f.get("px_last"), None))
    except Exception:                                  # noqa: BLE001
        log.debug("hızlı yol emirleri okunamadı", exc_info=True)
    return out


def _add_cands(out: dict, cands: list[tuple], ts: int, vols: dict, cfg=None) -> None:
    for coin, addr, side, o, last_ts, alerted, n, seen, src, px, run in cands:
        o = _fresher(o, addr, coin, side, px, ts)
        if run is not None and o and not native_overlap([o], run):
            continue
        _live_add(out, ts, vols, coin, addr, side, o, last_ts, int((o or {}).get("q_ts") or 0), alerted, n, seen,
                  src, cfg)


def _live_sorted(out: dict) -> list[dict]:
    return sorted(out.values(), key=lambda e: (-e["planned_usd"], e["coin"], e["addr"]))


def live_twaps(now_ts: int | None = None) -> list[dict]:
    """Yalnız bellek (canlı sayaç REG + ilk dilim yolu F.open) — G/Ç yok, 📣 geçici. /twap `live_twaps_full`."""
    ts = int(now_ts or now())
    out: dict[tuple, dict] = {}
    _add_cands(out, _live_memory_cands(ts), ts, VOL_CACHE.get("v") or {})
    return _live_sorted(out)


async def _announced(rows: list[dict]) -> None:
    """📣 = bu EMİR ana hedefe gerçekten gönderildi (kullanıcıya 'kanala bildirildi' diyoruz): ilk dilim yolunun
    gönderim kaydı ('sent:twap', 'twap_fast:' + emir anahtarı — Notifier yalnız başarılı gönderimde yazar) ya da
    eski yolun emir işareti ('twap_o', adres:coin:yön:başlangıç). Eski sürümün emirsiz 'twap' kaydı yalnız o
    anahtarda hiç 'twap_o' yoksa (bu sürümden önce gönderilmiş) ve emrin başlangıcından sonraysa sayılır.
    Bekleme benimsemesi / kanalsız tur / sessiz saatte özete kalan / aynı anahtarın başka emri 📣 değil."""
    if not rows:
        return
    try:
        async with db() as conn:
            for e in rows:
                key = f"{e['coin']}:{e['addr']}:{e['side']}"
                okey = f"{key}:{int(e['started_ts'])}"
                cur = await conn.execute(
                    """SELECT 1 FROM alerts_log WHERE (kind='sent:twap' AND key=?) OR (kind='twap_o' AND key=?)
                         OR (kind='twap' AND key=? AND ts >= ? AND COALESCE(payload,'') <> ''
                             AND NOT EXISTS (SELECT 1 FROM alerts_log o WHERE o.kind='twap_o'
                                             AND o.key >= ? AND o.key < ?)) LIMIT 1""",
                    (f"twap_fast:{okey}", okey, key, int(e["started_ts"]) - 5, key + ":", key + ";"))
                e["alerted"] = (await cur.fetchone()) is not None
    except Exception:                                  # noqa: BLE001
        log.debug("📣 işareti okunamadı", exc_info=True)


def _native_active(ts: int) -> dict[tuple, Run]:
    """Son LIVE_SILENT sn içinde HL TWAP alt emri dolumu (sıfır hash, adres taker) görülen diziler — emir sürüyor.
    Elle (imzalı) işlem bunu tazelemez."""
    return {r.key: r for r in list(REG.runs.values()) if r.nat_n and ts - int(r.nat_ts or 0) <= LIVE_SILENT}


async def live_twaps_full(collector=None, now_ts: int | None = None,
                          max_lookups: int = LIVE_LOOKUPS, cfg=None, client=None,
                          coin=None) -> tuple[list[dict], dict]:
    """/twap (kullanıcı 13.10: "hangi grupta yazarsam yazayım en büyük canlı TWAP'ları sıralasın"): şu an SÜREN
    HL TWAP emirleri, planlanan büyüklüğe göre. Bellek yeniden başlatmada sıfırlanır (13.10 20:39 deploy
    sonrası liste boş geldi) — kaynaklar:
      1) bellek: canlı sayacın emri sorulmuş dizileri + ilk dilim yolunun bildirdiği emirler
      2) kayıt: twap_runs'taki süren emir (activated, bitişi gelmemiş, son HL cevabı ≤ 6 sa) — YALNIZ aynı adres +
         coin + yönde son 5 dk'da HL TWAP alt emri dolumu görülüyorsa; kayıt tek başına güvenilmez (iptal sonrası
         'activated' kalmış olabilir) → anahtarı yine de anlık sorulur
      3) anlık sorgu: alt emir dolumu görülen ama emri bellekte taze bilinmeyen en büyük dizilerin adresleri şimdi
         sorulur (en çok `max_lookups`, aynı anda LIVE_ASK_PAR ve hızlı yolun soket tavanı içinde, 2 dk önbellek)
    Her kopya en taze emir geçmişiyle süzülür (_fresher): iptal edilmiş emir düşer. Tahmin yok: dolan = son
    HL cevabındaki rakam (`q_ts`), kalan süre = planlanan bitiş − şimdi. Dönüş: (satırlar, {asked, failed, skipped})."""
    ts = int(now_ts or now())
    info = {"asked": 0, "failed": 0, "skipped": 0}
    try:
        vols = await volumes_cached()
    except Exception:                                  # noqa: BLE001
        vols = VOL_CACHE.get("v") or {}
    cands = _live_memory_cands(ts)
    active = _native_active(ts)
    # /xpltwap: yalnız bu coin(ler) (perp + aynı token'ın spot çiftleri) — anlık sorgular da hep onlara gitsin
    coins = ({coin} if isinstance(coin, str) else set(coin)) if coin else set()
    if coins:
        cands = [c for c in cands if c[0] in coins]
        active = {k: r for k, r in active.items() if k[0] in coins}
    # 2) kayıt
    if active:
        try:
            async with db() as conn:
                cur = await conn.execute(
                    """SELECT coin, address, side, planned_usd, planned_sz, executed_usd, remaining_usd, order_ts,
                              order_min, order_status, lookup_ts, ts, alerted_ts, n_slices, total
                       FROM twap_runs WHERE order_status='activated' AND ended_ts IS NULL AND planned_usd > 0
                         AND (? = '' OR instr(?, '|' || coin || '|') > 0)
                         AND order_ts IS NOT NULL AND order_min IS NOT NULL
                         AND order_ts + order_min * 60 > ? AND COALESCE(lookup_ts, ts) >= ?""",
                    ("1" if coins else "", "|" + "|".join(sorted(coins)) + "|", ts, ts - LIVE_DB_MAX_AGE))
                rows = [dict(x) for x in await cur.fetchall()]
        except Exception:                              # noqa: BLE001
            log.debug("twap_runs süren emirler okunamadı", exc_info=True)
            rows = []
        for x in rows:
            r = active.get((x["coin"], x["address"], x["side"]))
            if r is None:
                continue
            pu = float(x.get("planned_usd") or 0)
            o = {"status": "activated", "planned_usd": pu, "planned_sz": x.get("planned_sz"),
                 "executed_usd": x.get("executed_usd"), "remaining_usd": x.get("remaining_usd"),
                 "filled_pct": (float(x.get("executed_usd") or 0) / pu * 100) if pu else None,
                 "minutes": float(x.get("order_min") or 0), "started_ts": int(x.get("order_ts") or 0),
                 "q_ts": int(x.get("lookup_ts") or x.get("ts") or 0)}
            cands.append((x["coin"], x["address"], x["side"], o, r.nat_ts, x.get("alerted_ts"), r.n, r.total, "db",
                          r.px_last, None))
    # 3) anlık sorgu: bellekte taze emri olmayan (kayıttan gelen ya da sorgu anı bilinmeyen) alt emir dizileri
    # "bilinen" = bellekte TAZE ve LİSTELENECEK kopya (en taze geçmişle süzülmüş, activated, bitişi gelmemiş): biten /
    # iptal edilen kopya aynı anahtarın yeni TWAP'ının sorulmasını engellemesin
    # "bilinen" = bellekte LİSTELENECEK kopya (en taze geçmişle süzülmüş, activated, bitişi gelmemiş). Yalnız son
    # LIVE_Q_TTL içinde HL'den gelmişse sorgu gerekmez ("taze"); daha eskiyse listelenir AMA yine sorulur (hiç
    # bilinmeyenlerden sonra): aynı adres + coin + yönde iptal-yeniden-koy ya da eşzamanlı ikinci TWAP dilimleri
    # ayırt edilemez, eski kopya yeni emri 10–20 dk gizlemesin (inceleme 4).
    known, fresh, stale = set(), set(), {}
    for c in cands:
        if c[8] == "db":
            continue
        o = _fresher(c[3], c[1], c[0], c[2], c[9], ts)
        q = int((o or {}).get("q_ts") or 0)
        if not o or o.get("status") != "activated" or not q or ts - q >= REFRESH_SEC * 2:
            continue
        t0, mins = int(o.get("started_ts") or 0), float(o.get("minutes") or 0)
        if t0 and mins and t0 + int(mins * 60) <= ts:
            continue
        k = (c[0], c[1], c[2])
        known.add(k)
        if ts - q < LIVE_Q_TTL:
            fresh.add(k)
        else:
            stale[k] = max(stale.get(k, 0.0), float(o.get("planned_usd") or 0))
    unknown = sorted((r for k, r in active.items() if k not in known), key=lambda r: -r.total)
    todo = unknown + sorted((active[k] for k in stale if k in active and k not in fresh), key=lambda r: -stale[r.key])
    unk_addrs = {r.address for r in unknown}
    fn = getattr(collector, "fetch_twap_history", None) if collector is not None else None
    addrs: list[str] = []
    for r in todo:
        if r.address not in addrs:
            addrs.append(r.address)
    if fn is None:
        info["skipped"] = len([a for a in addrs if a in unk_addrs])   # eski kopyası listelenen uyarı sayılmaz
        addrs = []
    # önbellek yalnız tazeyse (2 dk) VE adresin en yeni dizisinden sonra alınmışsa yeter — sonra açılan emir onda yok
    since = {}
    for r in todo:
        since[r.address] = max(since.get(r.address, 0), int(r.nat_first_ts or r.first_ts))
    need = [a for a in addrs
            if not ((c := _newest_hist(a)) and ts - int(c[0]) < LIVE_Q_TTL and int(c[0]) >= since[a] - 5)]
    cap = max(0, int(max_lookups))                   # tavan önbellek süzgecinden SONRA: taze önbellekli adres yer
    if len(need) > cap:                              # kaplamaz, art arda /twap sıradakilere geçer
        info["skipped"] += len([a for a in need[cap:] if a in unk_addrs])
        need = need[:cap]
    if need:
        from . import twapfast
        sem = asyncio.Semaphore(LIVE_ASK_PAR)

        async def ask(a):
            async with sem:
                waiters = getattr(collector, "_twap_waiters", None)
                if isinstance(waiters, dict) and len(waiters) >= twapfast.WS_CEILING:
                    return "skip"                        # soket tavanı: radarın sorgularına yer kalsın
                try:
                    try:
                        return await fn(a, timeout=5.0)
                    except TypeError:
                        return await fn(a)
                except Exception:                      # noqa: BLE001
                    return None
        res = await asyncio.gather(*(ask(a) for a in need))
        for a, h in zip(need, res):
            if isinstance(h, str):
                info["skipped"] += int(a in unk_addrs)
                continue
            info["asked"] += 1
            if h is None:
                info["failed"] += 1
                continue
            live_q_put(a, ts, h)
    # taze geçmişten bu coin + yönün TÜM süren emirleri (aynı anahtarda eşzamanlı birden çok TWAP olabilir; bellek
    # yalnız birini tutar): sorulanlar REFRESH_SEC*2'den, diğerleri yalnız LIVE_Q_TTL içinde değil — bilinen taze
    asked = set(need)
    for k in set(active) | known:
        r = REG.runs.get(k) or active.get(k)
        if r is None:
            continue
        c = _newest_hist(r.address)
        if not c or ts - int(c[0]) >= (LIVE_Q_TTL if r.address in asked or k not in known else REFRESH_SEC * 2):
            continue
        for o in parse_twap_orders(c[1], r.coin, r.side, r.px_last, ts):
            if o.get("status") == "activated":
                o["q_ts"] = int(c[0])
                cands.append((r.coin, r.address, r.side, o, r.nat_ts if r.nat_n else 0, False, r.n, r.total,
                              "ask" if r.address in asked else "mem", r.px_last, r))
    if cfg is not None:
        try:                                          # Cumartesi hisse: hacim oranı geçen Cumartesi'ye göre
            await asyncio.wait_for(ensure_sat(cfg, client, [c[0] for c in cands], ts), SAT_TIMEOUT)
        except Exception:                              # noqa: BLE001
            log.debug("Cumartesi tabanı", exc_info=True)
    out: dict[tuple, dict] = {}
    _add_cands(out, cands, ts, vols, cfg)            # her kopya en taze geçmişle süzülür (iptal → düşer)
    rows = [e for e in _live_sorted(out) if not coins or e["coin"] in coins]
    await _announced(rows)
    return rows, info


async def diag_summary(cfg) -> str:
    """/twap durum: son tur özeti + son kararlar."""
    from ..telegram.format import esc
    st = await kv_get(STATS_KV) or {}
    if not st.get("ts"):
        return "📡 Canlı TWAP: tur henüz çalışmadı."
    lines = [f"📡 <b>Canlı TWAP</b> · {st.get('keys', 0)} dizi bellekte · {st.get('cands', 0)} aday · "
             f"{st.get('regular', 0)} düzenli · {st.get('irregular', 0)} düzensiz · {st.get('alerted', 0)} bildirim"
             + (f" · sorgu ✓{st.get('lookups_ok', 0)}/⏱{st.get('lookups_timeout', 0)}/✗{st.get('lookups_err', 0)}"
                if "lookups_ok" in st else "")]
    try:
        from . import twapfast
        lines.append(esc(await twapfast.diag_line()))
        fs = await kv_get(twapfast.STATS_KV) or {}
        rec = [d for d in (fs.get("recent") or []) if d.get("reason") != "retry"][:5]
        if rec:
            lines.append("🔎 İlk dilim son kararlar:\n" + "\n".join("• " + esc(twapfast.recent_line(d)) for d in rec))
    except Exception:                                  # noqa: BLE001
        log.debug("hızlı twap özeti", exc_info=True)
    last = await kv_get(LAST_KV) or []
    if last:
        lines.append("Son kararlar:\n" + "\n".join("• " + esc(decision_line(d)) for d in last[:10]))
    lines.append("Kullanım: /twap → en büyük canlı TWAP'lar · /twap 0xADRES · /twap COIN")
    return "\n".join(lines)


def _scalar_measure(run: Run) -> dict:
    """Dilim dizisi yokken (restart sonrası) bitiş notu için skaler ölçü."""
    dur = run.last_ts - run.first_ts
    n = max(1, run.n)
    return {"coin": run.coin, "address": run.address, "side": run.side, "n": run.n,
            "total": run.total, "sz_total": run.sz_total, "first_ts": run.first_ts,
            "last_ts": run.last_ts, "dur": dur, "avg_slice": run.total / n,
            "avg_gap": (dur / (n - 1)) if n > 1 else 0, "median_gap": (dur / (n - 1)) if n > 1 else 0,
            "cv_gap": None, "cv_size": None, "rate_day": run.rate_day or 0.0, "native_like": False,
            "taker_pct": (run.tk_n / run.known_n * 100) if run.known_n else None,
            "avg_px": (run.pxsz / run.sz_total) if run.sz_total else run.px_last,
            "px_first": run.px_first, "px_last": run.px_last,
            "px_chg_pct": ((run.px_last - run.px_first) / run.px_first * 100) if run.px_first else 0.0}


async def loop(cfg, client, notifier, collector=None) -> None:
    """Denetimli döngü: 60 sn'de bir değerlendirme. Kanca ekstra istek yapmaz;
    aday başına bir WS emir sorgusu (kısa abonelik)."""
    from ..health import beat
    await asyncio.sleep(90)
    try:
        n = await _rehydrate(int(getattr(cfg, "twap_live_window_min", 240) or 240) * 60)
        if n:
            log.info("canlı twap: %d bildirilmiş tur geri yüklendi", n)
    except Exception:
        log.debug("canlı twap geri yükleme", exc_info=True)
    while True:
        try:
            await beat("twaplive")
            await evaluate(cfg, notifier, client, collector)
            await beat("twaplive")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("canlı twap turu hatası")
            await kv_set(STATS_KV, {"error": f"{type(e).__name__}: {e}"[:200], "ts": now()})
        await asyncio.sleep(max(20, int(getattr(cfg, "twap_live_eval_sec", 60) or 60)))
