"""🔸 TWAP yan kanalı (TWAP_LOOSE_CHAT_ID) — eşikleri biraz gevşetilmiş TWAP bildirimleri.

Kullanıcı 14.10: "TWAP bildirimleriyle ilgili şartları biraz gevşettiğimiz bir yan kanal da açalım, chat id'yi
değişken olarak ekleyeyim".

Yan kanal = GEVŞEK kapıyı geçen her HL TWAP emri:
  • ana hedefe (kripto kanalı / ana sohbet / ayna) giden her TWAP mesajının birebir kopyası — `copy()`
    (ilk alarm, 🔎 ilk dilim alarmı, ⛔ iptal; açıksa yarı ve 🏁). `TWAP_LOOSE_ONLY_EXTRA=1` → kopya yok.
  • ana kapıdan KALAN ama gevşek kapıyı geçen emir — `fast_extra()` (ilk dilim yolu) / `slow_extra()` (eski yol):
    aynı mesaj + başta "🔸 yan kanal" satırı (ana kanalın hangi eşiğinin altında kaldığı, SAYIYLA). Bu emirlerin
    ⛔ iptal notu (açıksa 🏁) yalnız buraya — `lifecycle()` (HL emri sorulur; tahmin yok).

Ana yolun durumuna (alerted_ts, 'twap' bekleme, twap_o / twap_fast işaretleri, 📣, 🧪 kapı) DOKUNULMAZ: yan kanal
tanımsızken hiçbir davranış değişmez; tanımlıyken ana kanal aynen sürer. Eşikler env-only (kullanıcı ayar sayfası
istemiyor) ve her biri ana eşikten BÜYÜK olamaz (yan kanal ⊇ ana kanal). Ön süzgeç (ilk dilim) gevşek eşikle bakar;
yalnız-yan-kanal adayları dakikalık sorgu tavanının en çok `QUERY_SHARE`'ını kullanır ve ana adaylardan sonra sorulur.
"""
import logging
from collections import deque

from ..db import alert_log, alert_recent, kv_get, kv_set, now
from . import twaplive

log = logging.getLogger("twaploose")

SENT_WINDOW = 7 * 86400     # tekrar kaydı: emir anahtarı (başlangıç anıyla) tekil — uzun emir de bir kez
QUERY_SHARE = 0.6           # ilk dilim yolunda yalnız-yan-kanal adayları dakika tavanının en çok bu payı
OPEN_MAX = 500
END_GRACE = 30              # planlanan bitişten bu kadar sonra emir sorulur
END_GIVEUP = 600            # bitişten bu kadar sonra HL hâlâ 'activated' diyorsa not yazılmadan kapatılır
QUICK_POLL = 60             # ⚡ HIZLI emir dakikada bir sorulur (iptal ⛔ ~1 dk içinde)
OPEN_POLL = 300             # uzun emir 5 dk'da bir
QUICK_SILENT = 45           # dilim bu kadar gelmezse (⚡) erken sorulur
SILENT_SEC = 90
RELOOK_SEC = 30             # iki sorgu arası en az
CACHE_OK = 45               # başka yolun (eski yol / ⚡ yenileme / /twap) bu kadar taze HL cevabı varsa yeniden sorulmaz
LIFE_LOOKUPS = 3            # ömür turu başına en çok bu kadar HL sorgusu (ilk dilim döngüsünü uzun bekletmesin; kalan 10 sn sonra)
FAIL_BACKOFF = 300          # yan kanala gönderilemeyen ek alarm bu kadar sonra yeniden denenir (eski yol her tur bakar)

# cfg alanı → yan kanal eşiğinin cfg alanı (Config: TWAP_LOOSE_* env'leri)
OVERRIDES = {
    "twap_alert_min_usd": "twap_loose_min_usd",
    "twap_alert_min_left_usd": "twap_loose_min_left_usd",
    "twap_alert_vol_pct": "twap_loose_vol_pct",
    "twap_alert_vol_pct_major": "twap_loose_vol_pct_major",
    "twap_alert_vol_pct_sat": "twap_loose_vol_pct_sat",
    "twap_fast_short_min_usd": "twap_loose_fast_min_usd",
    "twap_fast_rate_x": "twap_loose_fast_rate_x",
    "spot_twap_min_day_vol": "twap_loose_spot_min_day_vol",
}

REASON_TR = {"sent": "gönderildi", "dup": "zaten gönderildi", "cooldown": "bekleme içinde",
             "failed": "gönderilemedi", "main": "ana kanala gitmiş", "inflight": "ana yol şu an gönderiyor",
             "error": "hata", "off": "kapalı", "main_later": "ana kapıyı geçiyor (eski yol ana kanala yazar)",
             "backoff": "az önce gönderilemedi (sonra yeniden)", "big_only": "yalnız 🕐 için soruldu"}

KNOWN: dict[tuple, int] = {}            # yalnız-yan-kanal adayının bastırması (ana yolun F.known'u DEĞİL; iki kanal ortak)
RECENT: deque = deque(maxlen=20)        # yalnız-yan-kanal adaylarının son kararları (/tani)
STATS: dict = {}                        # 🔸 kanalın + ortak ilk-dilim katmanının sayaçları


class Side:
    """Bir yan kanalın durumu ve ortak işleri: gönderim (kendi tekrar kaydıyla), bildirdiği emirlerin kaydı (OPEN),
    dilim sayacı, ⛔ ömür turu, kalıcılık, sayaçlar. 🔸 gevşek (LOOSE) ve 🕐 büyük (BIG) kanal aynı kodu kullanır."""

    def __init__(self, name: str, kind: str, chat_attr: str, env: str, open_kv: str, stats_kv: str,
                 stats: dict | None = None):
        self.name, self.kind, self.chat_attr, self.env = name, kind, chat_attr, env
        self.open_kv, self.stats_kv = open_kv, stats_kv
        self.open: dict[str, dict] = {}         # emir anahtarı (coin:adres:yön:başlangıç) → bildirilen emir (⛔ notu)
        self.by_key: dict[tuple, list] = {}     # (coin, adres, yön) → open anahtarları (aynı anda >1 emir olabilir)
        self.failed: dict[str, int] = {}        # gönderilemeyen alarm → bu ana kadar yeniden deneme yok
        self.sending: set[str] = set()          # şu an gönderilen tekrar anahtarları
        self.sent: set[str] = set()             # bu açılışta gönderilen (her turda DB'ye sormamak için)
        self.stats: dict = stats if stats is not None else {}
        self.recent: deque = deque(maxlen=20)
        self.loaded = [False]

    def clear(self) -> None:
        for x in (self.open, self.by_key, self.failed, self.sending, self.sent, self.recent):
            x.clear()
        self.loaded[0] = False

    def inc(self, k: str, n: int = 1) -> None:
        self.stats[k] = self.stats.get(k, 0) + n

    def chat_id(self, cfg) -> str:
        return (getattr(cfg, self.chat_attr, "") or "").strip()

    def enabled(self, cfg) -> bool:
        """Kanal açık mı: chat id tanımlı, TWAP canlı radarı ve TWAP bildirimi (notify_twap) açık."""
        if cfg is None or not self.chat_id(cfg) or not getattr(cfg, "twap_live_enabled", True):
            return False
        from ..notify import kind_enabled
        return kind_enabled(cfg, self.kind)

    async def send(self, cfg, notifier, text: str, dkey: str, coin: str = "", *, primary: str | None = None,
                   is_copy: bool = False) -> bool | None:
        """Kanala tek mesaj, kendi tekrar kaydıyla (alerts_log <kind>, 7 gün): aynı emrin aynı mesajı bir kez.
        None = gönderilmedi (kapalı / aynı sohbet / zaten gitti); False = başarısız. Kopya, kanal ana hedefle ya da
        ayna grubuyla AYNI sohbetse gitmez (orada zaten var). Fan-out yok."""
        chat = self.chat_id(cfg)
        if not chat or notifier is None or not text or not dkey:
            return None
        if is_copy:
            main = (getattr(cfg, "telegram_chat_id", "") or "").strip()
            if primary is not None and chat == ((primary or "").strip() or main):
                return None
            if chat == twaplive.mirror_chat(cfg):
                return None
        if dkey in self.sent or dkey in self.sending:
            self.inc("dup")
            return None
        self.sending.add(dkey)
        ok = False
        try:
            if await alert_recent(self.kind, dkey, SENT_WINDOW):
                self.sent.add(dkey)
                self.inc("dup")
                return None
            ok = bool(await notifier.send(self.kind, text, priority="high", key=f"{self.name}:{dkey}", chat_id=chat,
                                          coin=coin, public=False))
            if ok:
                await alert_log(self.kind, dkey, "")
                self.sent.add(dkey)
                if len(self.sent) > 5000:
                    self.sent.clear()                   # yalnız önbellek: DB kaydı asıl kaynak
        except Exception:                              # noqa: BLE001 — yan kanal asla ana yolu düşürmez
            log.debug("twap %s kanalı gönderimi %s", self.name, dkey, exc_info=True)
            ok = False
        finally:
            self.sending.discard(dkey)
        if ok:
            self.stats["last_ok_ts"] = now()
        else:
            self.inc("failed")
            self.stats["last_fail_ts"] = now()
        return ok

    async def mark_alias(self, alias: str) -> None:
        try:
            if alias not in self.sent:
                await alert_log(self.kind, alias, "")
                self.sent.add(alias)
        except Exception:                              # noqa: BLE001
            log.debug("%s kanalı takma adı %s", self.name, alias, exc_info=True)

    async def announced(self, okey: str | None, alias: str | None = None) -> bool:
        """Emrin ilk alarmı bu kanalda görüldü mü (emir anahtarı ya da eski yolun tur takma adıyla)?"""
        for k in ((f"a:{okey}" if okey else ""), (alias or "")):
            if k and (k in self.sent or await alert_recent(self.kind, k, SENT_WINDOW)):
                return True
        return False

    def register(self, okey: str, coin: str, addr: str, side: str, order: dict, t: int, first_ts: int,
                 last_ts: int, n: int, total: float, sz: float, px_first: float, px_last: float) -> None:
        started = int(order.get("started_ts") or 0)
        if len(self.open) >= OPEN_MAX:
            for k in sorted(self.open, key=lambda k: self.open[k]["alert_ts"])[: OPEN_MAX // 10]:
                self.drop(k)
        self.open[okey] = {"coin": coin, "addr": addr, "side": side, "started": started,
                           "end_at": started + int(float(order.get("minutes") or 0) * 60), "order": order,
                           "alert_ts": t, "first_ts": int(first_ts or t), "last_ts": int(last_ts or t),
                           "last_slice": int(last_ts or t), "n": int(n or 1), "total": float(total or 0),
                           "sz": float(sz or 0), "px_first": float(px_first or 0), "px_last": float(px_last or 0),
                           "lookup_ts": t, "ended": 0}
        self._index(okey, (coin, addr, side))

    def _index(self, okey: str, key: tuple) -> None:
        lst = self.by_key.setdefault(key, [])
        if okey not in lst:
            lst.append(okey)

    def drop(self, okey: str) -> None:
        r = self.open.pop(okey, None)
        if r is None:
            return
        key = (r["coin"], r["addr"], r["side"])
        lst = self.by_key.get(key)
        if lst and okey in lst:
            lst.remove(okey)
            if not lst:
                self.by_key.pop(key, None)

    def observe(self, key: tuple, px: float, sz: float, notional: float, ts: int) -> None:
        """Sıcak yol (collector, sıfır hash'li taker dolumu): bildirilen emrin bitiş notu için skaler sayaç. Aynı
        adres + coin + yönde birden çok emir açıksa dolum, başlangıcı dolumdan önceki EN YENİ ve süresi dolmamış
        emre yazılır (HL alt emirleri emir kimliği taşımıyor)."""
        lst = self.by_key.get(key)
        if not lst:
            return
        ts = int(ts)
        r = None
        for okey in lst:
            x = self.open.get(okey)
            if x is None or x["ended"] or int(x["started"]) > ts + 5 or ts > int(x["end_at"]) + END_GRACE:
                continue
            if r is None or int(x["started"]) > int(r["started"]):
                r = x
        if r is None:
            return
        if ts - r["last_slice"] > 2:
            r["n"] += 1
            r["last_slice"] = ts
        r["total"] += float(notional)
        r["sz"] += float(sz)
        r["px_last"] = float(px)
        r["last_ts"] = max(r["last_ts"], ts)

    async def save_open(self) -> None:
        try:
            await kv_set(self.open_kv, [dict(r) for r in self.open.values() if not r.get("ended")])
        except Exception:                              # noqa: BLE001
            log.debug("%s yazılamadı", self.open_kv, exc_info=True)

    async def load_open(self, t: int) -> int:
        """Açılış: bildirilen emirler geri yüklenir (kapalıyken iptal edilenin ⛔ notu ilk turda yazılır)."""
        if self.loaded[0]:
            return 0
        self.loaded[0] = True
        n = 0
        for r in (await kv_get(self.open_kv) or []):
            try:
                okey = f"{r['coin']}:{r['addr']}:{r['side']}:{int(r['started'])}"
                if okey in self.open or t > int(r["end_at"]) + END_GIVEUP:
                    continue
                r["lookup_ts"], r["ended"] = 0, 0
                self.open[okey] = r
                self._index(okey, (r["coin"], r["addr"], r["side"]))
                n += 1
            except (KeyError, TypeError, ValueError):
                continue
        return n

    async def lifecycle(self, cfg, notifier, collector, t: int, owns=None) -> int:
        """Bildirilen emirler: HL emri sorulur (⚡ dakikada bir, uzun 5 dk'da bir; dilim kesilince erken; bitişte),
        iptal → ⛔ notu bu kanala (🏁 normal bitiş yalnız twap_alert_finish_note açıksa). `owns(okey)` doğruysa notu
        başka yol yazar (🔸: emir ana kanala da gitti → kopyası gelir). Başka yolun taze HL cevabı (≤ 45 sn) varsa
        yeniden sorulmaz; tur başına en çok LIFE_LOOKUPS sorgu. Not gidemezse en çok 3 kez yeniden. Kanal kapalıyken
        (notify_twap) biten emir notsuz kapanır. Dönüş: yazılan not sayısı."""
        from . import twapfast
        from ..telegram import format as fmt
        if not self.open:
            return 0
        sent = 0
        changed = False
        asked = 0
        fn = getattr(collector, "fetch_twap_history", None) if collector is not None else None
        for okey, r in sorted(self.open.items(), key=lambda kv: int(kv[1]["lookup_ts"])):   # en uzun beklenen önce
            if r["ended"]:
                if t - r["ended"] > 600:
                    self.drop(okey)
                continue
            end_at = int(r["end_at"])
            quick = twaplive.is_quick(cfg, r.get("order"))
            due = (t >= end_at + END_GRACE or t - int(r["last_ts"]) > (QUICK_SILENT if quick else SILENT_SEC)
                   or t - int(r["lookup_ts"]) >= (QUICK_POLL if quick else OPEN_POLL))
            if not due:
                continue
            if t - int(r["lookup_ts"]) < RELOOK_SEC and not (t >= end_at + END_GRACE > int(r["lookup_ts"])):
                continue
            if owns is not None and await owns(okey):
                r["ended"], changed = t, True          # notu başka yol yazar (kopyası buraya gelir)
                continue
            hist = None
            cached = twaplive._newest_hist(r["addr"])
            if cached and t - int(cached[0]) <= CACHE_OK and int(cached[0]) > int(r["lookup_ts"]) \
                    and not (t >= end_at + END_GRACE and int(cached[0]) < end_at + END_GRACE):
                hist = cached[1]
                self.inc("end_cached")
                r["lookup_ts"] = int(cached[0])
            else:
                if fn is None or asked >= LIFE_LOOKUPS:
                    continue
                waiters = getattr(collector, "_twap_waiters", None)
                if isinstance(waiters, dict) and len(waiters) >= twapfast.WS_CEILING:
                    continue                            # soket dolu: ana yolun sorguları önce
                self.inc("end_lookups")
                asked += 1
                r["lookup_ts"] = t
                try:
                    hist = await fn(r["addr"])
                except Exception:                      # noqa: BLE001
                    hist = None
                if hist is None:
                    continue
                twaplive.live_q_put(r["addr"], t, hist)
            o = None
            for x in twaplive.parse_twap_orders(hist, r["coin"], r["side"], r["px_last"], t):
                if abs(int(x.get("started_ts") or 0) - int(r["started"])) <= 2:
                    o = x
                    break
            if o is not None:
                r["order"] = o
            st = (o or {}).get("status")
            if st == "activated":
                if t < end_at + END_GIVEUP:
                    continue
                r["ended"], changed = t, True
                continue
            if o is None and t < end_at + END_GRACE:
                continue
            if o is None or not twaplive.end_note_wanted(cfg, st in ("terminated", "error")):
                r["ended"], changed = t, True
                continue
            if not self.enabled(cfg):
                r["ended"], changed = t, True           # bildirim kapalı (notify_twap): not YOK, geç ⛔ de yok
                continue
            vol = (twaplive.VOL_CACHE.get("v") or {}).get(r["coin"])
            ctx = {"order": o, "day_vol": vol[0] if vol else None, "klass": twaplive.klass_of(r["coin"]),
                   "cancelled": st in ("terminated", "error"), "quick_max": twaplive.quick_max(cfg)}
            text = fmt.twap_end(twapfast._end_measure(r), ctx)
            ok = await self.send(cfg, notifier, text, f"e:{okey}", r["coin"])
            if ok is False:
                r["end_tries"] = int(r.get("end_tries") or 0) + 1
                if r["end_tries"] < twaplive.END_MAX_TRIES:
                    r["lookup_ts"] = t - RELOOK_SEC     # işaret yok: sonraki turda yeniden
                    continue
            elif ok:
                self.inc("ends")
                sent += 1
            r["ended"], changed = t, True
        if changed:
            await self.save_open()
        return sent

    def snapshot(self, t: int, recent: list | None = None) -> dict:
        return {**self.stats, "open": sum(1 for r in self.open.values() if not r["ended"]),
                "recent": recent if recent is not None else list(self.recent)[:8], "ts": t}

    async def save_stats(self, t: int, recent: list | None = None) -> None:
        try:
            await kv_set(self.stats_kv, self.snapshot(t, recent))
        except Exception:                              # noqa: BLE001
            log.debug("%s yazılamadı", self.stats_kv, exc_info=True)


LOOSE = Side("loose", "twap_loose", "twap_loose_chat_id", "TWAP_LOOSE_CHAT_ID", "twaploose_open", "twaploose_stats",
             stats=STATS)
OPEN = LOOSE.open                       # 🔸 bildirilen yalnız-yan-kanal emirleri (geri uyum: testler / çağıranlar)
BY_KEY = LOOSE.by_key
FAILED = LOOSE.failed
SENDING = LOOSE.sending
SENT = LOOSE.sent
_LOADED = LOOSE.loaded


def clear_stats() -> None:
    STATS.clear()
    STATS.update({k: 0 for k in ("copies", "extra", "ends", "failed", "dup", "below", "pre_queued",
                                 "pre_share", "end_lookups", "end_cached", "lookups", "mm", "lookup_fail",
                                 "no_order", "strict_later", "known_skip", "orphan_skip")})
    STATS["last_fail_ts"] = STATS["last_ok_ts"] = 0


clear_stats()


def reset() -> None:
    """Testler: tüm bellek durumu (iki kanal + ortak katman)."""
    LOOSE.clear()
    BIG.clear()
    BIG.stats.clear()
    KNOWN.clear()
    RECENT.clear()
    clear_stats()


def chat_id(cfg) -> str:
    return LOOSE.chat_id(cfg)


def enabled(cfg) -> bool:
    """🔸 yan kanal açık mı: chat id tanımlı, TWAP canlı radarı ve TWAP bildirimi (notify_twap) açık."""
    return LOOSE.enabled(cfg)


def any_enabled(cfg) -> bool:
    return LOOSE.enabled(cfg) or BIG.enabled(cfg)


def wants(key: tuple, ts: int) -> bool:
    """İlk dilim ön süzgeci: bu anahtarın emri yalnız-gevşek olarak zaten soruldu mu (KNOWN)? Sıcak yol, G/Ç yok."""
    until = KNOWN.get(key)
    if until is not None and int(ts) <= until:
        STATS["known_skip"] += 1
        return False
    return True


def prune(ref_ts: int) -> None:
    for k in [k for k, u in KNOWN.items() if u < ref_ts]:
        KNOWN.pop(k, None)
    for side in (LOOSE, BIG):
        for k in [k for k, u in side.failed.items() if u < ref_ts]:
            side.failed.pop(k, None)


def note(p: dict, reason: str, **kw) -> None:
    RECENT.appendleft({"coin": p["coin"], "addr": p["addr"], "side": p["side"], "reason": reason,
                       "slice": round(p.get("max") or 0), "ts": now(), **kw})


def only_extra(cfg) -> bool:
    return bool(getattr(cfg, "twap_loose_only_extra", False))


class LooseCfg:
    """Ana ayarın GEVŞEK görünümü: yalnız eşik alanları değişir, her biri min(yan kanal, ana) — ana eşik ayar
    sayfasında değişirse yan kanal da izler; ana kapıda kapalı (0) kural yan kanalda da kapalı. Geri kalan her şey
    (⚡ süre sınırı, büyük emir eşiği, bekleme, uçlar) ana ayardan canlı okunur."""
    __slots__ = ("_cfg",)

    def __init__(self, cfg):
        object.__setattr__(self, "_cfg", cfg)

    def __getattr__(self, name):
        base = getattr(self._cfg, name)
        src = OVERRIDES.get(name)
        if src is None:
            return base
        try:
            b = float(base or 0)
            v = float(getattr(self._cfg, src))
        except (AttributeError, TypeError, ValueError):
            return base
        if b <= 0 or v < 0:
            return base                     # ana kapıda kural kapalı / geçersiz değer → ana ayar
        if name == "twap_alert_vol_pct_sat" and v <= 0:
            return base                     # 0 Cumartesi tabanını kapatırdı (24s'e döner → ana kanaldan SIKI olurdu)
        return min(v, b)

    def __setattr__(self, name, value):
        raise AttributeError("LooseCfg salt okunur")


def loose_cfg(cfg):
    return LooseCfg(cfg)


# ---------------- gönderim ----------------

async def _send(cfg, notifier, text: str, dkey: str, coin: str = "", *, primary: str | None = None,
                is_copy: bool = True) -> bool | None:
    """🔸 kanala tek mesaj (Side.send)."""
    return await LOOSE.send(cfg, notifier, text, dkey, coin, primary=primary, is_copy=is_copy)


async def copy(cfg, notifier, text: str, dkey: str, coin: str = "", primary: str | None = None,
               okey: str | None = None, alias: str | None = None) -> bool | None:
    """Ana hedefe (ya da yalnız aynaya) giden TWAP mesajının kopyası. `dkey`: 'a:<emir>' ilk alarm, 'e:<emir>' bitiş,
    'p:…' yarı — emir anahtarıyla, böylece yalnız-yan-kanal alarmı gitmiş emrin kopyası İKİNCİ kez gitmez.
    Bitiş / yarı notu yalnız ilk alarmı YAN KANALDA görülmüş emir için (`okey`; bilinmiyorsa not yok) — kanal sonradan
    tanımlandıysa ya da ilk alarm buraya gidemediyse "duyurulmamış emrin iptali" yazılmaz. `alias` (eski yol:
    'run:<coin:adres:yön>:<ilk dilim>'): ilk alarmda da yazılır; bitişte emir HL geçmişinden düşmüşse (okey yok)
    duyuru onunla bulunur."""
    if not enabled(cfg) or only_extra(cfg):
        return None
    if not dkey.startswith("a:"):
        if not await announced(okey, alias):
            STATS["orphan_skip"] += 1
            return None
    ok = await _send(cfg, notifier, text, dkey, coin, primary=primary, is_copy=True)
    if ok:
        STATS["copies"] += 1
    if ok is not False and dkey.startswith("a:") and alias and (ok or f"a:{okey}" in SENT):
        await _mark_alias(alias)
    return ok


async def _mark_alias(alias: str) -> None:
    await LOOSE.mark_alias(alias)


async def announced(okey: str | None, alias: str | None = None) -> bool:
    """Emrin ilk alarmı 🔸 kanalda görüldü mü (emir anahtarı ya da eski yolun tur takma adıyla)?"""
    return await LOOSE.announced(okey, alias)


async def main_owns(okey: str) -> bool:
    """Emir ana kanala da bildirildi mi (eski yol 'twap_o' ya da ilk dilim 'twap_fast')? O zaman bitiş notunu ana
    yol yazar ve kopyası buraya gelir — yalnız-yan-kanal ömrü kendi notunu yazmaz."""
    try:
        return bool(await alert_recent("twap_o", okey, SENT_WINDOW) or await alert_recent("twap_fast", okey, SENT_WINDOW))
    except Exception:                              # noqa: BLE001
        return False


async def _cooldown(cfg, ckey: str) -> bool:
    """Aynı adres + coin + yön için bekleme (twap_alert_cooldown, 6 sa): ana yolun işaretleri ('twap', hızlı yolun
    beklemesi — ana kanal bu adresi az önce bildirdi, kopyası buraya geldi) ya da yan kanalın kendi işareti."""
    cool = int(getattr(cfg, "twap_alert_cooldown", 21600) or 0)
    if not cool:
        return False
    from .twapfast import FAST_CD
    for kind in ("twap_lcd", "twap", FAST_CD):
        if await alert_recent(kind, ckey, cool):
            return True
    return False


def why_parts(cfg, g: str, det: dict, short_reason: str = "") -> list[str]:
    """Ana kanal neden göndermedi — ölçü ve eşik, sayıyla (tahmin yok). `g` order_gate nedeni; `short_reason`
    ⚡ hızlı emirde dakika hızı kuralının nedeni (slow / short_small)."""
    from ..telegram.format import usd
    out = []
    sat = bool(det.get("sat"))
    if g == "vol_small" and det.get("vol_pct") is not None:
        base = "geçen Cumartesi hacminin" if sat else "24s hacmin"
        out.append(f"emir {base} %{float(det['vol_pct']):.1f}'i (ana kanal ≥ %{float(det.get('pct_needed') or 0):g})")
    elif g == "order_small":
        both = short_reason == "short_small"            # ⚡ hızlı emirde iki taban da: tek parça
        out.append(f"emir {usd(det.get('planned'))} (ana kanal ≥ "
                   f"{usd(float(getattr(cfg, 'twap_alert_min_usd', 1_000_000) or 0))}"
                   + (f"; ⚡ hızlıda ≥ {usd(float(getattr(cfg, 'twap_fast_short_min_usd', 250_000) or 0))})"
                      if both else ")"))
        if both:
            short_reason = ""
    elif g == "order_left":
        out.append(f"kalan {usd(det.get('left'))} (ana kanal ≥ {usd(det.get('left_need'))})")
    elif g == "spot_thin":
        out.append(f"çiftin 24s hacmi {usd(det.get('day_vol'))} (ana kanal ≥ "
                   f"{usd(float(getattr(cfg, 'spot_twap_min_day_vol', 0) or 0))})")
    elif g == "no_chat":
        out.append("ana kanal tanımlı değil")
    if short_reason == "slow" and det.get("rate_x") is not None:
        out.append(f"⚡ dakika hızı ortalamanın {float(det['rate_x']):.1f} katı (ana kanal ≥ "
                   f"{float(getattr(cfg, 'twap_fast_rate_x', 2) or 0):g} kat)")
    elif short_reason == "short_small":
        out.append(f"⚡ emir {usd(det.get('planned'))} (ana kanal ⚡ için ≥ "
                   f"{usd(float(getattr(cfg, 'twap_fast_short_min_usd', 250_000) or 0))})")
    if not out:
        out.append(twaplive.REASON_TR.get(g, g))
    return out


def _register(okey: str, coin: str, addr: str, side: str, order: dict, t: int, first_ts: int, last_ts: int,
              n: int, total: float, sz: float, px_first: float, px_last: float) -> None:
    LOOSE.register(okey, coin, addr, side, order, t, first_ts, last_ts, n, total, sz, px_first, px_last)


def _drop(okey: str) -> None:
    LOOSE.drop(okey)


def observe(key: tuple, px: float, sz: float, notional: float, ts: int) -> None:
    """Sıcak yol (collector, sıfır hash'li taker dolumu — ilk dilim yolu kapalıyken de): iki kanalın bildirdiği
    emirlerin bitiş notu sayacı."""
    if LOOSE.by_key:
        LOOSE.observe(key, px, sz, notional, ts)
    if BIG.by_key:
        BIG.observe(key, px, sz, notional, ts)


def any_open() -> bool:
    return bool(LOOSE.by_key or BIG.by_key)


async def save_open() -> None:
    await LOOSE.save_open()


async def load_open(t: int) -> int:
    """Açılış: 🔸 kanalın emirleri geri yüklenir (kapalıyken iptal edilenin ⛔ notu ilk turda yazılır)."""
    return await LOOSE.load_open(t)


async def load_all(t: int) -> int:
    return await LOOSE.load_open(t) + await BIG.load_open(t)


# ---------------- yalnız-yan-kanal alarmı ----------------

async def _extra(cfg, notifier, okey: str, ckey: str, coin: str, text_fn, head: str, claimed: bool = False) -> str:
    """Ortak gönderim: tekrar / bekleme / talep → mesaj. Dönüş: 'sent' | 'dup' | 'cooldown' | 'failed'."""
    dkey = f"a:{okey}"
    if dkey in SENT or dkey in SENDING:
        return "dup"                                # eski yol her turda yeniden bakar: sayaç şişmesin
    if okey in twaplive.SENDING and not claimed:
        return "inflight"                           # öteki yol bu emri ŞU AN ana hedefe yazıyor
    if FAILED.get(dkey, 0) > now():
        return "backoff"                            # az önce gidemedi: eski yol her turda takip teklifi / pozisyon sormasın
    if await alert_recent("twap_loose", dkey, SENT_WINDOW):
        SENT.add(dkey)
        return "dup"
    if await main_owns(okey):
        return "main"                               # emir ana kanala zaten gitti (kopya da) — "eşik altı" denmez
    if await _cooldown(cfg, ckey):
        return "cooldown"
    from ..telegram import format as fmt
    text = await text_fn()
    ok = await _send(cfg, notifier, fmt.side_note(text, head), dkey, coin, is_copy=False)
    if ok is None:
        return "dup"
    if not ok:
        FAILED[dkey] = now() + FAIL_BACKOFF
        await alert_log("fail:twap_loose", dkey, "")
        return "failed"
    STATS["extra"] += 1
    if int(getattr(cfg, "twap_alert_cooldown", 21600) or 0):
        await alert_log("twap_lcd", ckey, "")
    return "sent"


async def fast_extra(cfg, client, notifier, key: tuple, p: dict, order: dict, g: str, det: dict, vols: dict, t: int,
                     entity: str = "") -> str:
    """🔎 İlk dilim yolu: ana kapıdan kalan (g) emir gevşek kapıyı geçerse yan kanala. Ana yolun durumuna dokunmaz.
    Dönüş: 'off' | gevşek kapının nedeni | 'sent' | 'dup' | 'cooldown' | 'failed'."""
    if not enabled(cfg):
        return "off"
    import types
    from . import twapfast
    from ..telegram import format as fmt
    coin, addr, side = key
    vol = vols.get(coin)
    day_vol, vol_ts = vol if vol else (None, None)
    lc = loose_cfg(cfg)
    lg, ldet = twapfast.decide_gate(lc, coin, order, day_vol, vol_ts, t)
    if lg not in ("ok", "big", "short"):
        STATS["below"] += 1
        return lg
    started = int(order.get("started_ts") or 0)
    if not started:
        return "no_order"
    okey = f"{coin}:{addr}:{side}:{started}"
    if g == "no_chat":
        head = fmt.twap_loose_head([], no_chat=True)     # ana eşiği GEÇTİ, yalnız ana hedef tanımsız
    else:
        short_reason = twapfast.short_gate(cfg, order, det.get("rate_x"))
        head = fmt.twap_loose_head(why_parts(cfg, det.get("reason"), det,
                                             short_reason if short_reason not in ("short", "") else ""))

    async def text_fn():
        pos = await twapfast._pos(client, coin, addr, wallet=twaplive.wallet_on(cfg))
        wo = twaplive.with_seen(order, coin, addr, side, t)                      # HL süren emirde dolanı 0 yazar
        offer = None
        if getattr(cfg, "twap_follow_enabled", True) and not twaplive.auto_end(cfg, order):
            try:
                offer = await twaplive._twap_offer(types.SimpleNamespace(
                    address=addr, coin=coin, side=side, total=float(order.get("planned_usd") or 0), first_ts=started))
            except Exception:                      # noqa: BLE001
                log.debug("yan kanal takip teklifi (%s)", coin, exc_info=True)
        ctx = {"coin": coin, "address": addr, "side": side, "gate": lg,
               "day_vol": ldet.get("day_vol") if ldet.get("sat") else day_vol, "sat": ldet.get("sat"),
               "vol_pct": ldet.get("vol_pct"), "per_min": ldet.get("per_min"), "rate_x": ldet.get("rate_x"),
               "slice_usd": p.get("max"), "slice_px": p.get("px"), "pos": pos, "klass": twaplive.klass_of(coin),
               "entity": entity, "offer": offer, "now": now(), "quick_max": twaplive.quick_max(cfg),
               "auto_end": twaplive.auto_end(cfg, order), "why": ldet.get("reason"),
               "low_bal": twaplive.low_bal_ctx(cfg, coin, side, wo, pos)}
        box["lb"] = ctx["low_bal"]
        return fmt.twap_fast(wo, ctx)

    box: dict = {}
    res = await _extra(cfg, notifier, okey, f"{coin}:{addr}:{side}", coin, text_fn, head)
    if res == "sent":
        twaplive.low_bal_mark(box.get("lb"), coin)
        _register(okey, coin, addr, side, order, t, int(p.get("t0") or t), int(p.get("ts") or t),
                  int(p.get("n") or 1), float(p.get("tot") or p.get("max") or 0), float(p.get("szt") or 0),
                  float(p.get("px0") or p.get("px") or 0), float(p.get("px") or 0))
        await save_open()
        log.info("yan kanal (ilk dilim): %s %s %s emir %.0f$ (%s; ana: %s)", coin, side, addr[:10],
                 float(order.get("planned_usd") or 0), lg, g)
    return res


async def slow_extra(cfg, notifier, client, run, m: dict, order: dict, g: str, det: dict, day_vol, vol_ts,
                     ents: dict, ts: int, claimed: bool = False) -> str:
    """Eski yol: düzenli dizi + HL emri var, ana kapı geçmedi (g) → gevşek kapı geçerse yan kanala. Ana yolun turuna
    (alerted_ts / bekleme / kayıt) dokunmaz. Dönüş: fast_extra gibi."""
    if not enabled(cfg):
        return "off"
    from ..telegram import format as fmt
    lc = loose_cfg(cfg)
    ldet = twaplive.gate_detail(lc, run.coin, order, day_vol, vol_ts, ts)
    lg = ldet["reason"]
    if lg not in ("ok", "big"):
        return lg                                   # (sayaç yalnız ilk dilim yolunda: eski yol her turda bakar)
    okey = twaplive.order_key(f"{run.coin}:{run.address}:{run.side}", order)
    if not okey:
        return "no_order"
    head = fmt.twap_loose_head([], no_chat=True) if g == "no_chat" else fmt.twap_loose_head(why_parts(cfg, g, det))

    async def text_fn():
        pos = await twaplive.position_ctx(client, run.coin, run.address, wallet=twaplive.wallet_on(cfg))
        offer = None
        if getattr(cfg, "twap_follow_enabled", True) and not twaplive.auto_end(cfg, order):
            try:
                offer = await twaplive._twap_offer(run)
            except Exception:                      # noqa: BLE001
                log.debug("yan kanal takip teklifi (%s)", run.coin, exc_info=True)
        ctx_order = twaplive.with_seen(order, run.coin, run.address, run.side, ts)
        ctx = {"order": ctx_order,
               "day_vol": ldet["day_vol"] if ldet.get("sat") else day_vol, "vol_prec": 1,
               "vol_pct": ldet["vol_pct"], "sat": ldet.get("sat"), "pos": pos, "gate": lg,
               "klass": twaplive.klass_of(run.coin), "entity": ents.get(run.address) or "", "offer": offer,
               "quick_max": twaplive.quick_max(cfg), "auto_end": twaplive.auto_end(cfg, order),
               "low_bal": twaplive.low_bal_ctx(cfg, run.coin, run.side, ctx_order, pos)}
        box["lb"] = ctx["low_bal"]
        return fmt.twap_alert(m, ctx)

    box: dict = {}
    res = await _extra(cfg, notifier, okey, f"{run.coin}:{run.address}:{run.side}", run.coin, text_fn, head,
                       claimed=claimed)
    if res == "sent":
        twaplive.low_bal_mark(box.get("lb"), run.coin)
    if res in ("sent", "dup"):
        await _mark_alias(f"run:{run.coin}:{run.address}:{run.side}:{int(run.first_ts)}")
    if res == "sent":
        _register(okey, run.coin, run.address, run.side, order, ts, run.first_ts, run.last_ts, run.n, run.total,
                  run.sz_total, m.get("px_first") or run.px_first, m.get("px_last") or run.px_last)
        await save_open()
        log.info("yan kanal (eski yol): %s %s %s emir %.0f$ (%s; ana: %s)", run.coin, run.side, run.address[:10],
                 float(order.get("planned_usd") or 0), lg, g)
    return res


# ---------------- ömür: ⛔ iptal (açıksa 🏁) ----------------

async def lifecycle(cfg, notifier, collector, t: int) -> int:
    """🔸 yalnız-yan-kanal emirleri (Side.lifecycle). Emir sonradan ana kanala da bildirildiyse notu ana yol yazar,
    kopyası gelir (yalnız-ek modunda kopya yok → kendi notu)."""
    return await LOOSE.lifecycle(cfg, notifier, collector, t, owns=None if only_extra(cfg) else main_owns)


async def lifecycle_all(cfg, notifier, collector, t: int) -> int:
    n = 0
    if LOOSE.open:
        n += await lifecycle(cfg, notifier, collector, t)
    if BIG.open:
        n += await BIG.lifecycle(cfg, notifier, collector, t)
    return n


def snapshot(t: int) -> dict:
    return LOOSE.snapshot(t, list(RECENT)[:8])


async def save_stats(t: int) -> None:
    await LOOSE.save_stats(t, list(RECENT)[:8])


async def diag_line(cfg) -> str:
    """/tani için tek satır."""
    if not chat_id(cfg):
        return "🔸 twap yan kanalı: tanımsız (TWAP_LOOSE_CHAT_ID boş)"
    lc = loose_cfg(cfg)
    from ..telegram.format import usd
    rule = (f"emir ≥ {usd(lc.twap_alert_min_usd)} · hacmin %{float(lc.twap_alert_vol_pct):g}'i"
            f" (BTC/ETH %{float(lc.twap_alert_vol_pct_major):g}) · ⚡ ≥ {usd(lc.twap_fast_short_min_usd)}"
            f" ve {float(lc.twap_fast_rate_x):g} kat")
    st = await kv_get(LOOSE.stats_kv) or {}
    parts = [rule, "yalnız ek" if only_extra(cfg) else "ana kanalın kopyaları + ek"]
    if st.get("ts"):
        parts.append(f"✓{st.get('copies', 0)} kopya · ✓{st.get('extra', 0)} ek alarm · {st.get('ends', 0)} bitiş notu"
                     f" · {st.get('open', 0)} açık emir")
        for k, lab in (("pre_queued", "ön süzgeçte yalnız gevşekle sıraya"), ("lookups", "yalnız-gevşek sorgu"),
                       ("pre_share", "sorgu payı doldu"), ("dropped_old", "sırada eskidi"),
                       ("below", "ilk dilimde gevşek eşik altı"), ("strict_later", "ana kapılık (eski yola kaldı)"),
                       ("no_order", "emir yok"), ("end_lookups", "bitiş sorgusu"),
                       ("orphan_skip", "duyurulmamış emrin notu atlandı")):
            if st.get(k):
                parts.append(f"{st[k]} {lab}")
        for k, lab in (("lookup_fail", "sorgu başarısız"), ("errors", "hata")):
            if st.get(k):
                parts.append(f"⚠️ {st[k]} {lab}")
        if st.get("failed"):
            parts.append(f"⚠️ {st['failed']} gönderilemedi (grup süper gruba dönüştüyse id değişir: grupta /id yaz,"
                         " TWAP_LOOSE_CHAT_ID'yi güncelle)")
    line = "🔸 twap yan kanalı: " + " · ".join(parts)
    rec = [recent_line(d) for d in (st.get("recent") or [])[:4]]
    if rec:
        line += "\n       yalnız-gevşek son kararlar: " + " · ".join(rec)
    return line


def recent_line(d: dict) -> str:
    from .twapfast import REASON_TR as FR
    from ..telegram.format import short, usd
    from .. import assets
    txt = f"{assets.label(d.get('coin') or '')} {short(d.get('addr') or '')} {d.get('side')}"
    txt += f" {usd(d['planned'])} emir" if d.get("planned") else f" dilim {usd(d.get('slice') or 0)}"
    r = d.get("reason") or "?"
    txt += f" → {FR.get(r, r)}"
    if d.get("loose"):
        txt += f" · 🔸 {REASON_TR.get(d['loose'], FR.get(d['loose'], d['loose']))}"
    return txt



# ================= 🕐 BÜYÜK TWAP kanalı (TWAP_BIG_CHAT_ID) =================
# Kullanıcı 14.10 (HYPE 0xedcd… 150 dk $1,6M satış, hacmin %0,7'si — ana ve 🔸 kanala gelmedi; fiyat düştü): "yeni kanal
# kuralım: max 2-3 saat içinde $1M'dan yüksek TÜM TWAP'ları göndereceğimiz". Kural HACİMDEN BAĞIMSIZ: süren HL TWAP emri,
# süresi ≤ TWAP_BIG_MAX_MIN (180 dk) ve planı (bugünkü fiyatla) ≥ TWAP_BIG_MIN_USD ($1M) → bu kanala, emir başına BİR kez
# (ana / 🔸 kanala gitmiş olsa da; adres beklemesi yok). ⛔ iptal notu (açıksa 🏁) yine buraya (Side.lifecycle). mm/vault
# etiketli adresler ana yoldaki gibi elenir. Ana yolun ve 🔸 kanalın durumuna DOKUNMAZ.

BIG = Side("big", "twap_big", "twap_big_chat_id", "TWAP_BIG_CHAT_ID", "twapbig_open", "twapbig_stats")
BIG_REASON = {"ok": "geçer", "weekend": "📅 hafta sonu hisse kuralıyla geçer", "long": "süre sınırın üstünde",
              "small": "plan sınırın altında", "order_done": "emir bitmiş", "no_order": "emir yok", "off": "kapalı"}
BIG_PASS = ("ok", "weekend")
_WK: dict = {"min": -1, "h": None, "on": False}      # hafta sonu penceresi önbelleği (sıcak yol: dakikada bir hesap)


def big_weekend_on(cfg, ts: int) -> bool:
    """Hafta sonu mu — botun ortak penceresi (hourstats.weekend_window: TSİ Cumartesi 00:00 → Pazartesi 00:00, ABD
    kapalı; `offhours_close_hour` ile kayar). Dakikada bir hesaplanır."""
    h = int(getattr(cfg, "offhours_close_hour", 0) or 0)
    m = int(ts) // 60
    if _WK["min"] != m or _WK["h"] != h:
        from . import hourstats
        _WK.update(min=m, h=h, on=hourstats.weekend_window(int(ts), h) is not None)
    return bool(_WK["on"])


def big_weekend_limits(cfg) -> tuple[float, float]:
    return (float(getattr(cfg, "twap_big_weekend_min_usd", 200_000) or 0),
            float(getattr(cfg, "twap_big_weekend_max_min", 1440) or 0))


def big_weekend_rule(cfg, coin: str, ts: int) -> bool:
    """📅 Hafta sonu kuralı bu coin için şu an geçerli mi: kripto OLMAYAN (HIP-3 hisse, endeks, emtia, FX… —
    assets.trades_24_7 değil; Cumartesi tabanıyla aynı küme) ve hafta sonu ve kural açık."""
    from .. import assets
    mn, mx = big_weekend_limits(cfg)
    if mn <= 0 or mx <= 0 or not coin or assets.trades_24_7(coin) or big_is_major(cfg, coin):
        return False                                    # HIP-3'teki BTC/ETH olsa da $20M tabanı geçerli
    return big_weekend_on(cfg, ts)


def big_slice_floor(cfg, coin: str, ts: int) -> float | None:
    """Hafta sonu hissede ilk dilim yolunun en küçük dilimi: 24 sa'lik $200K emrin dilimi ~$70 — ana yolun $250
    tabanının altında. Kural geçerli değilse None (taban aynen)."""
    if not big_weekend_rule(cfg, coin, ts):
        return None
    mn, mx = big_weekend_limits(cfg)
    return max(1.0, mn * 0.8 / (2 * mx + 1))


def big_min_usd(cfg) -> float:
    return float(getattr(cfg, "twap_big_min_usd", 1_000_000) or 0)


def big_major_min_usd(cfg) -> float:
    return float(getattr(cfg, "twap_big_major_min_usd", 20_000_000) or 0)


def big_majors(cfg) -> set[str]:
    raw = getattr(cfg, "twap_big_majors", "BTC,ETH")
    return {x.strip().upper() for x in (raw.split(",") if isinstance(raw, str) else (raw or [])) if x and x.strip()}


def big_is_major(cfg, coin: str) -> bool:
    """Ayrı (yüksek) tabanlı coin mi — TWAP_BIG_MAJORS (varsayılan BTC,ETH): ana dex, HIP-3'teki aynı sembol ve spot
    sarmalı (UBTC/USDC, UETH/USDC) dahil."""
    ex = big_majors(cfg)
    if not ex or not coin:
        return False
    from .. import assets
    base = assets.label(coin).split("/")[0].upper()
    if assets.is_spot(coin) and base[:1] == "U" and base[1:] in ex:
        return True                                     # HL spot sarmalı: UBTC, UETH
    return base in ex


def big_min_for(cfg, coin: str) -> float:
    """Plan tabanı: BTC/ETH (kullanıcı 14.10: "btc eth kaldırmak yerine 20-30M diyelim") TWAP_BIG_MAJOR_MIN_USD ($20M);
    diğerleri TWAP_BIG_MIN_USD ($1M). Ayrı taban 0 ise genel taban."""
    if big_is_major(cfg, coin):
        m = big_major_min_usd(cfg)
        if m > 0:
            return max(m, big_min_usd(cfg))
    return big_min_usd(cfg)


def big_max_min(cfg) -> float:
    return float(getattr(cfg, "twap_big_max_min", 180) or 0)


def big_pre(cfg, slice_usd: float, coin: str = "", ts: int | None = None) -> bool:
    """İlk dilim ön süzgeci (G/Ç yok): bu dilim ≤ sınır dk'lık ≥ $1M bir emirden olabilir mi? Dilim ≈ plan / (2m+1)
    (randomize ±%20 → 0.8 payı) → plan ≤ dilim · (2m+1) / 0.8. BTC/ETH'de taban $20M → dilim ≳ $44K. 📅 hafta sonu
    hissede ≤ 24 sa'lik ≥ $200K → dilim ≳ $55."""
    mx, mn = big_max_min(cfg), big_min_for(cfg, coin)
    sl = float(slice_usd or 0)
    if mx > 0 and sl * (2 * mx + 1) / 0.8 >= mn:
        return True
    if ts is not None and big_weekend_rule(cfg, coin, ts):
        wmn, wmx = big_weekend_limits(cfg)
        return sl * (2 * wmx + 1) / 0.8 >= wmn
    return False


def big_gate(cfg, order: dict | None, now_ts: int, coin: str = "") -> str:
    """'ok' (genel kural) / 'weekend' (📅 hafta sonu hisse kuralı) geçer; diğerleri neden: no_order · order_done ·
    long (süre > sınır ya da bilinmiyor) · small (plan < taban; BTC/ETH'de ayrı taban) · off."""
    mx = big_max_min(cfg)
    wk = big_weekend_rule(cfg, coin, now_ts)
    if mx <= 0 and not wk:
        return "off"
    if not order:
        return "no_order"
    if order.get("status") != "activated":
        return "order_done"
    mins = float(order.get("minutes") or 0)
    t0 = int(order.get("started_ts") or 0)
    if mins > 0 and t0 and int(now_ts) > t0 + mins * 60:
        return "order_done"
    planned = float(order.get("planned_usd") or 0)
    if mx > 0 and 0 < mins <= mx and planned >= big_min_for(cfg, coin):
        return "ok"
    if wk:
        wmn, wmx = big_weekend_limits(cfg)
        if 0 < mins < wmx and planned >= wmn:            # kullanıcı: "1 günden kısa"
            return "weekend"
        return "long" if (mins <= 0 or mins >= wmx) else "small"
    return "long" if (mins <= 0 or mins > mx) else "small"


def big_head(cfg, coin: str = "", rule: str = "ok") -> str:
    from ..telegram.format import usd
    if rule == "weekend":
        wmn, wmx = big_weekend_limits(cfg)
        return (f"🕐 📅 hafta sonu (ABD kapalı), kripto olmayan · plan ≥ {usd(wmn)} · süre < "
                + (f"{wmx / 60:g} sa" if wmx >= 60 and wmx % 60 == 0 else f"{wmx:g} dk"))
    mx = big_max_min(cfg)
    dur = f"{mx / 60:g} sa" if mx >= 60 and mx % 60 == 0 else f"{mx:g} dk"
    tag = f" ({'/'.join(sorted(big_majors(cfg)))})" if coin and big_is_major(cfg, coin) else ""
    return f"🕐 kanal kuralı: plan ≥ {usd(big_min_for(cfg, coin))}{tag} · süre ≤ {dur}"


def big_note(coin: str, addr: str, side: str, reason: str, order: dict | None, path: str) -> None:
    BIG.recent.appendleft({"coin": coin, "addr": addr, "side": side, "reason": reason, "path": path, "ts": now(),
                           "planned": round(float((order or {}).get("planned_usd") or 0)),
                           "minutes": (order or {}).get("minutes")})


async def _big_send(cfg, notifier, okey: str, coin: str, text_fn, rule: str = "ok") -> str:
    """Tekrar / geri çekilme → mesaj. Dönüş: 'sent' | 'dup' | 'backoff' | 'failed'."""
    dkey = f"a:{okey}"
    if dkey in BIG.sent or dkey in BIG.sending:
        return "dup"
    if BIG.failed.get(dkey, 0) > now():
        return "backoff"
    if await alert_recent(BIG.kind, dkey, SENT_WINDOW):
        BIG.sent.add(dkey)
        return "dup"
    text = await text_fn()
    from ..telegram.format import side_note
    ok = await BIG.send(cfg, notifier, side_note(text, big_head(cfg, coin, rule)), dkey, coin)
    if ok is None:
        return "dup"
    if not ok:
        BIG.failed[dkey] = now() + FAIL_BACKOFF
        await alert_log("fail:twap_big", dkey, "")
        return "failed"
    BIG.inc("alerts")
    return "sent"


async def big_fast(cfg, client, notifier, key: tuple, p: dict, order: dict, vols: dict, t: int,
                   entity: str = "") -> str:
    """🔎 İlk dilim yolu (ana kapının kararı ne olursa olsun, ana karardan SONRA): kural geçerse 🕐 kanala."""
    if not BIG.enabled(cfg):
        return "off"
    coin, addr, side = key
    bg = big_gate(cfg, order, t, coin)
    started = int((order or {}).get("started_ts") or 0)
    if bg not in BIG_PASS or not started:
        return bg if bg not in BIG_PASS else "no_order"
    import types
    from . import twapfast
    from ..telegram import format as fmt
    okey = f"{coin}:{addr}:{side}:{started}"
    vol = vols.get(coin)
    day_vol, vol_ts = vol if vol else (None, None)
    _, det = twapfast.decide_gate(cfg, coin, order, day_vol, vol_ts, t)      # yalnız ölçü (hacme oran, dakika hızı)

    async def text_fn():
        pos = await twapfast._pos(client, coin, addr, wallet=twaplive.wallet_on(cfg))
        wo = twaplive.with_seen(order, coin, addr, side, t)                      # HL süren emirde dolanı 0 yazar
        offer = None
        if getattr(cfg, "twap_follow_enabled", True) and not twaplive.auto_end(cfg, order):
            try:
                offer = await twaplive._twap_offer(types.SimpleNamespace(
                    address=addr, coin=coin, side=side, total=float(order.get("planned_usd") or 0), first_ts=started))
            except Exception:                      # noqa: BLE001
                log.debug("🕐 takip teklifi (%s)", coin, exc_info=True)
        ctx = {"coin": coin, "address": addr, "side": side, "gate": "ok",
               "day_vol": det.get("day_vol") if det.get("sat") else day_vol, "sat": det.get("sat"),
               "vol_pct": det.get("vol_pct"), "per_min": det.get("per_min"), "rate_x": det.get("rate_x"),
               "slice_usd": p.get("max"), "slice_px": p.get("px"), "pos": pos, "klass": twaplive.klass_of(coin),
               "entity": entity, "offer": offer, "now": now(), "quick_max": twaplive.quick_max(cfg),
               "auto_end": twaplive.auto_end(cfg, order),
               "low_bal": twaplive.low_bal_ctx(cfg, coin, side, wo, pos)}
        box["lb"] = ctx["low_bal"]
        return fmt.twap_fast(wo, ctx)

    box: dict = {}
    res = await _big_send(cfg, notifier, okey, coin, text_fn, bg)
    if res == "sent":
        twaplive.low_bal_mark(box.get("lb"), coin)
        BIG.register(okey, coin, addr, side, order, t, int(p.get("t0") or t), int(p.get("ts") or t),
                     int(p.get("n") or 1), float(p.get("tot") or p.get("max") or 0), float(p.get("szt") or 0),
                     float(p.get("px0") or p.get("px") or 0), float(p.get("px") or 0))
        await BIG.save_open()
        log.info("🕐 büyük twap (ilk dilim): %s %s %s emir %.0f$ %s dk", coin, side, addr[:10],
                 float(order.get("planned_usd") or 0), order.get("minutes"))
    big_note(coin, addr, side, res, order, "fast")
    return res


async def big_slow(cfg, notifier, client, run, m: dict, order: dict, det: dict, day_vol, ents: dict,
                   ts: int) -> str:
    """Eski yol (turun sonunda, ana kararlardan SONRA): düzenli dizi + örtüşen HL emri, kural geçerse 🕐 kanala."""
    if not BIG.enabled(cfg):
        return "off"
    bg = big_gate(cfg, order, ts, run.coin)
    okey = twaplive.order_key(f"{run.coin}:{run.address}:{run.side}", order)
    if bg not in BIG_PASS or not okey:
        return bg if bg not in BIG_PASS else "no_order"
    if f"a:{okey}" in BIG.sent:
        return "dup"                                    # her turda bakılır: hızlı çıkış (DB'ye sormadan)
    from ..telegram import format as fmt

    async def text_fn():
        pos = await twaplive.position_ctx(client, run.coin, run.address, wallet=twaplive.wallet_on(cfg))
        offer = None
        if getattr(cfg, "twap_follow_enabled", True) and not twaplive.auto_end(cfg, order):
            try:
                offer = await twaplive._twap_offer(run)
            except Exception:                      # noqa: BLE001
                log.debug("🕐 takip teklifi (%s)", run.coin, exc_info=True)
        ctx_order = twaplive.with_seen(order, run.coin, run.address, run.side, ts)
        ctx = {"order": ctx_order,
               "day_vol": det.get("day_vol") if det.get("sat") else day_vol, "vol_prec": 1,
               "vol_pct": det.get("vol_pct"), "sat": det.get("sat"), "pos": pos, "gate": "ok",
               "klass": twaplive.klass_of(run.coin), "entity": ents.get(run.address) or "", "offer": offer,
               "quick_max": twaplive.quick_max(cfg), "auto_end": twaplive.auto_end(cfg, order),
               "low_bal": twaplive.low_bal_ctx(cfg, run.coin, run.side, ctx_order, pos)}
        box["lb"] = ctx["low_bal"]
        return fmt.twap_alert(m, ctx)

    box: dict = {}
    res = await _big_send(cfg, notifier, okey, run.coin, text_fn, bg)
    if res == "sent":
        twaplive.low_bal_mark(box.get("lb"), run.coin)
        BIG.register(okey, run.coin, run.address, run.side, order, ts, run.first_ts, run.last_ts, run.n, run.total,
                     run.sz_total, m.get("px_first") or run.px_first, m.get("px_last") or run.px_last)
        await BIG.save_open()
        log.info("🕐 büyük twap (eski yol): %s %s %s emir %.0f$ %s dk", run.coin, run.side, run.address[:10],
                 float(order.get("planned_usd") or 0), order.get("minutes"))
    if res in ("sent", "failed", "backoff"):           # eski yol her turda bakar: tekrar ve kural dışı not edilmez
        big_note(run.coin, run.address, run.side, res, order, "slow")
    return res


async def big_save_stats(t: int) -> None:
    await BIG.save_stats(t)


async def big_diag_line(cfg) -> str:
    """/tani için tek satır."""
    from ..telegram.format import usd
    if not BIG.chat_id(cfg):
        return "🕐 büyük twap kanalı: tanımsız (TWAP_BIG_CHAT_ID boş)"
    mx = big_max_min(cfg)
    maj = "/".join(sorted(big_majors(cfg)))
    parts = [f"plan ≥ {usd(big_min_usd(cfg))}"
             + (f" ({maj} ≥ {usd(max(big_major_min_usd(cfg), big_min_usd(cfg)))})" if maj and big_major_min_usd(cfg) > 0
                else "")
             + f" · süre ≤ {mx:g} dk · hacimden bağımsız"]
    wmn, wmx = big_weekend_limits(cfg)
    if wmn > 0 and wmx > 0:
        parts.append(f"📅 hafta sonu kripto olmayan: plan ≥ {usd(wmn)} · süre < {wmx:g} dk"
                     + (" (şu an hafta sonu)" if big_weekend_on(cfg, now()) else ""))
    st = await kv_get(BIG.stats_kv) or {}
    if st.get("ts"):
        parts.append(f"✓{st.get('alerts', 0)} alarm · {st.get('ends', 0)} bitiş notu · {st.get('open', 0)} açık emir")
        for k, lab in (("end_lookups", "bitiş sorgusu"), ("dup", "tekrar atlandı")):
            if st.get(k):
                parts.append(f"{st[k]} {lab}")
        if st.get("failed"):
            parts.append(f"⚠️ {st['failed']} gönderilemedi (grup süper gruba dönüştüyse id değişir: grupta /id yaz,"
                         " TWAP_BIG_CHAT_ID'yi güncelle)")
    tier = await kv_get(LOOSE.stats_kv) or {}
    for k, lab in (("pre_queued", "ön süzgeçte yalnız yan kanallar için sıraya"), ("pre_share", "sorgu payı doldu")):
        if tier.get(k):
            parts.append(f"{tier[k]} {lab} (ortak)")
    line = "🕐 büyük twap kanalı: " + " · ".join(parts)
    rec = [f"{d.get('coin')} {str(d.get('addr') or '')[:10]} {d.get('side')} {usd(d.get('planned') or 0)}"
           f" {d.get('minutes')} dk → {BIG_REASON.get(d.get('reason'), d.get('reason'))}"
           for d in (st.get("recent") or [])[:4]]
    if rec:
        line += "\n       son kararlar: " + " · ".join(rec)
    return line
