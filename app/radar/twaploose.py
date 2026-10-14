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

OPEN_KV = "twaploose_open"
STATS_KV = "twaploose_stats"
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
             "backoff": "az önce gönderilemedi (sonra yeniden)"}

OPEN: dict[str, dict] = {}              # emir anahtarı (coin:adres:yön:başlangıç) → yalnız-yan-kanal emri (⛔ notu için)
BY_KEY: dict[tuple, list] = {}          # (coin, adres, yön) → OPEN anahtarları (sıcak yol dilim sayacı; aynı anda >1 emir)
KNOWN: dict[tuple, int] = {}            # yalnız-gevşek adayın bastırması (ana yolun F.known'u DEĞİL)
RECENT: deque = deque(maxlen=20)        # yalnız-gevşek adayların son kararları (/tani)
FAILED: dict[str, int] = {}             # gönderilemeyen ek alarm → bu ana kadar yeniden deneme yok
SENDING: set[str] = set()               # şu an gönderilen tekrar anahtarları (iki yol aynı anda yazmasın)
SENT: set[str] = set()                  # bu açılışta gönderilen (her turda DB'ye sormamak için)
STATS: dict = {}
_LOADED = [False]


def clear_stats() -> None:
    STATS.clear()
    STATS.update({k: 0 for k in ("copies", "extra", "ends", "failed", "dup", "below", "pre_queued",
                                 "pre_share", "end_lookups", "end_cached", "lookups", "mm", "lookup_fail",
                                 "no_order", "strict_later", "known_skip", "orphan_skip")})
    STATS["last_fail_ts"] = STATS["last_ok_ts"] = 0


clear_stats()


def reset() -> None:
    """Testler: tüm bellek durumu."""
    OPEN.clear()
    BY_KEY.clear()
    KNOWN.clear()
    RECENT.clear()
    FAILED.clear()
    SENDING.clear()
    SENT.clear()
    _LOADED[0] = False
    clear_stats()


def chat_id(cfg) -> str:
    return (getattr(cfg, "twap_loose_chat_id", "") or "").strip()


def enabled(cfg) -> bool:
    """Yan kanal açık mı: chat id tanımlı, TWAP canlı radarı ve TWAP bildirimi (notify_twap) açık."""
    if cfg is None or not chat_id(cfg) or not getattr(cfg, "twap_live_enabled", True):
        return False
    from ..notify import kind_enabled
    return kind_enabled(cfg, "twap_loose")


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
    for k in [k for k, u in FAILED.items() if u < ref_ts]:
        FAILED.pop(k, None)


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
    """Yan kanala tek mesaj, kendi tekrar kaydıyla (alerts_log 'twap_loose', 7 gün): aynı emrin aynı mesajı (kopya ya
    da yalnız-yan-kanal) bir kez. None = gönderilmedi (kapalı / aynı sohbet / zaten gitti); False = başarısız.
    Kopya, yan kanal ana hedefle ya da ayna grubuyla AYNI sohbetse gitmez (orada zaten var). Fan-out yok."""
    chat = chat_id(cfg)
    if not chat or notifier is None or not text or not dkey:
        return None
    if is_copy:
        main = (getattr(cfg, "telegram_chat_id", "") or "").strip()
        if primary is not None and chat == ((primary or "").strip() or main):
            return None
        if chat == twaplive.mirror_chat(cfg):
            return None
    if dkey in SENT or dkey in SENDING:
        STATS["dup"] += 1
        return None
    SENDING.add(dkey)
    ok = False
    try:
        if await alert_recent("twap_loose", dkey, SENT_WINDOW):
            SENT.add(dkey)
            STATS["dup"] += 1
            return None
        ok = bool(await notifier.send("twap_loose", text, priority="high", key=f"loose:{dkey}", chat_id=chat,
                                      coin=coin, public=False))
        if ok:
            await alert_log("twap_loose", dkey, "")
            SENT.add(dkey)
            if len(SENT) > 5000:
                SENT.clear()                        # yalnız önbellek: DB kaydı asıl kaynak
    except Exception:                              # noqa: BLE001 — yan kanal asla ana yolu düşürmez
        log.debug("twap yan kanal gönderimi %s", dkey, exc_info=True)
        ok = False
    finally:
        SENDING.discard(dkey)
    if ok:
        STATS["last_ok_ts"] = now()
    else:
        STATS["failed"] += 1
        STATS["last_fail_ts"] = now()
    return ok


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
    try:
        if alias not in SENT:
            await alert_log("twap_loose", alias, "")
            SENT.add(alias)
    except Exception:                              # noqa: BLE001
        log.debug("yan kanal takma adı %s", alias, exc_info=True)


async def announced(okey: str | None, alias: str | None = None) -> bool:
    """Emrin ilk alarmı yan kanalda görüldü mü (emir anahtarı ya da eski yolun tur takma adıyla)?"""
    for k in ((f"a:{okey}" if okey else ""), (alias or "")):
        if k and (k in SENT or await alert_recent("twap_loose", k, SENT_WINDOW)):
            return True
    return False


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
    started = int(order.get("started_ts") or 0)
    if len(OPEN) >= OPEN_MAX:
        for k in sorted(OPEN, key=lambda k: OPEN[k]["alert_ts"])[: OPEN_MAX // 10]:
            _drop(k)
    OPEN[okey] = {"coin": coin, "addr": addr, "side": side, "started": started,
                  "end_at": started + int(float(order.get("minutes") or 0) * 60), "order": order,
                  "alert_ts": t, "first_ts": int(first_ts or t), "last_ts": int(last_ts or t),
                  "last_slice": int(last_ts or t), "n": int(n or 1), "total": float(total or 0), "sz": float(sz or 0),
                  "px_first": float(px_first or 0), "px_last": float(px_last or 0), "lookup_ts": t, "ended": 0}
    _index(okey, (coin, addr, side))


def _index(okey: str, key: tuple) -> None:
    lst = BY_KEY.setdefault(key, [])
    if okey not in lst:
        lst.append(okey)


def _drop(okey: str) -> None:
    r = OPEN.pop(okey, None)
    if r is None:
        return
    key = (r["coin"], r["addr"], r["side"])
    lst = BY_KEY.get(key)
    if lst and okey in lst:
        lst.remove(okey)
        if not lst:
            BY_KEY.pop(key, None)


def observe(key: tuple, px: float, sz: float, notional: float, ts: int) -> None:
    """Sıcak yol (collector, sıfır hash'li taker dolumu — ilk dilim yolu kapalıyken de): yalnız-yan-kanal emrinin
    bitiş notu için skaler sayaç. Aynı adres + coin + yönde birden çok emir açıksa dolum, başlangıcı dolumdan önceki
    EN YENİ ve süresi dolmamış emre yazılır (HL alt emirleri emir kimliği taşımıyor)."""
    lst = BY_KEY.get(key)
    if not lst:
        return
    ts = int(ts)
    r = None
    for okey in lst:
        x = OPEN.get(okey)
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


async def save_open() -> None:
    try:
        await kv_set(OPEN_KV, [dict(r) for r in OPEN.values() if not r.get("ended")])
    except Exception:                              # noqa: BLE001
        log.debug("twaploose_open yazılamadı", exc_info=True)


async def load_open(t: int) -> int:
    """Açılış: yalnız-yan-kanal emirleri geri yüklenir (kapalıyken iptal edilenin ⛔ notu ilk turda yazılır)."""
    if _LOADED[0]:
        return 0
    _LOADED[0] = True
    n = 0
    for r in (await kv_get(OPEN_KV) or []):
        try:
            okey = f"{r['coin']}:{r['addr']}:{r['side']}:{int(r['started'])}"
            if okey in OPEN or t > int(r["end_at"]) + END_GIVEUP:
                continue
            r["lookup_ts"], r["ended"] = 0, 0
            OPEN[okey] = r
            _index(okey, (r["coin"], r["addr"], r["side"]))
            n += 1
        except (KeyError, TypeError, ValueError):
            continue
    return n


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
    text = await text_fn()
    ok = await _send(cfg, notifier, head + "\n" + text, dkey, coin, is_copy=False)
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
        pos = await twapfast._pos(client, coin, addr)
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
               "auto_end": twaplive.auto_end(cfg, order), "why": ldet.get("reason")}
        return fmt.twap_fast(twaplive.with_seen(order, coin, addr, side, t), ctx)

    res = await _extra(cfg, notifier, okey, f"{coin}:{addr}:{side}", coin, text_fn, head)
    if res == "sent":
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
        pos = await twaplive.position_ctx(client, run.coin, run.address)
        offer = None
        if getattr(cfg, "twap_follow_enabled", True) and not twaplive.auto_end(cfg, order):
            try:
                offer = await twaplive._twap_offer(run)
            except Exception:                      # noqa: BLE001
                log.debug("yan kanal takip teklifi (%s)", run.coin, exc_info=True)
        ctx = {"order": twaplive.with_seen(order, run.coin, run.address, run.side, ts),
               "day_vol": ldet["day_vol"] if ldet.get("sat") else day_vol,
               "vol_pct": ldet["vol_pct"], "sat": ldet.get("sat"), "pos": pos, "gate": lg,
               "klass": twaplive.klass_of(run.coin), "entity": ents.get(run.address) or "", "offer": offer,
               "quick_max": twaplive.quick_max(cfg), "auto_end": twaplive.auto_end(cfg, order)}
        return fmt.twap_alert(m, ctx)

    res = await _extra(cfg, notifier, okey, f"{run.coin}:{run.address}:{run.side}", run.coin, text_fn, head,
                       claimed=claimed)
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
    """Yalnız-yan-kanal emirleri: HL emri sorulur (⚡ dakikada bir, uzun 5 dk'da bir; dilim kesilince erken; bitişte),
    iptal → ⛔ notu yan kanala (🏁 normal bitiş yalnız twap_alert_finish_note açıksa). Emir sonradan ana kanala da
    bildirildiyse notu ana yol yazar, kopyası gelir. Başka yolun taze HL cevabı (≤ 45 sn) varsa yeniden sorulmaz.
    Not gidemezse sonraki turlarda yeniden (en çok 3 kez). Dönüş: yazılan not sayısı."""
    from . import twapfast
    from ..telegram import format as fmt
    if not OPEN:
        return 0
    sent = 0
    changed = False
    asked = 0
    fn = getattr(collector, "fetch_twap_history", None) if collector is not None else None
    for okey, r in sorted(OPEN.items(), key=lambda kv: int(kv[1]["lookup_ts"])):   # en uzun beklenen önce
        if r["ended"]:
            if t - r["ended"] > 600:
                _drop(okey)
            continue
        end_at = int(r["end_at"])
        quick = twaplive.is_quick(cfg, r.get("order"))
        due = (t >= end_at + END_GRACE or t - int(r["last_ts"]) > (QUICK_SILENT if quick else SILENT_SEC)
               or t - int(r["lookup_ts"]) >= (QUICK_POLL if quick else OPEN_POLL))
        if not due:
            continue
        if t - int(r["lookup_ts"]) < RELOOK_SEC and not (t >= end_at + END_GRACE > int(r["lookup_ts"])):
            continue
        if not only_extra(cfg) and await main_owns(okey):
            r["ended"], changed = t, True          # ana yol yazar (kopyası buraya gelir)
            continue
        hist = None
        cached = twaplive._newest_hist(r["addr"])
        if cached and t - int(cached[0]) <= CACHE_OK and int(cached[0]) > int(r["lookup_ts"]) \
                and not (t >= end_at + END_GRACE and int(cached[0]) < end_at + END_GRACE):
            hist = cached[1]
            STATS["end_cached"] += 1
            r["lookup_ts"] = int(cached[0])
        else:
            if fn is None or asked >= LIFE_LOOKUPS:
                continue
            waiters = getattr(collector, "_twap_waiters", None)
            if isinstance(waiters, dict) and len(waiters) >= twapfast.WS_CEILING:
                continue                            # soket dolu: ana yolun sorguları önce
            STATS["end_lookups"] += 1
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
        vol = (twaplive.VOL_CACHE.get("v") or {}).get(r["coin"])
        ctx = {"order": o, "day_vol": vol[0] if vol else None, "klass": twaplive.klass_of(r["coin"]),
               "cancelled": st in ("terminated", "error"), "quick_max": twaplive.quick_max(cfg)}
        if not enabled(cfg):
            r["ended"], changed = t, True               # bildirim kapalı (notify_twap): not YOK, geç ⛔ de yok
            continue
        text = fmt.twap_end(twapfast._end_measure(r), ctx)
        ok = await _send(cfg, notifier, text, f"e:{okey}", r["coin"], is_copy=False)
        if ok is False:
            r["end_tries"] = int(r.get("end_tries") or 0) + 1
            if r["end_tries"] < twaplive.END_MAX_TRIES:
                r["lookup_ts"] = t - RELOOK_SEC     # işaret yok: sonraki turda yeniden
                continue
        elif ok:
            STATS["ends"] += 1
            sent += 1
        r["ended"], changed = t, True
    if changed:
        await save_open()
    return sent


def snapshot(t: int) -> dict:
    return {**STATS, "open": sum(1 for r in OPEN.values() if not r["ended"]), "recent": list(RECENT)[:8], "ts": t}


async def save_stats(t: int) -> None:
    try:
        await kv_set(STATS_KV, snapshot(t))
    except Exception:                              # noqa: BLE001
        log.debug("twaploose_stats yazılamadı", exc_info=True)


async def diag_line(cfg) -> str:
    """/tani için tek satır."""
    if not chat_id(cfg):
        return "🔸 twap yan kanalı: tanımsız (TWAP_LOOSE_CHAT_ID boş)"
    lc = loose_cfg(cfg)
    from ..telegram.format import usd
    rule = (f"emir ≥ {usd(lc.twap_alert_min_usd)} · hacmin %{float(lc.twap_alert_vol_pct):g}'i"
            f" (BTC/ETH %{float(lc.twap_alert_vol_pct_major):g}) · ⚡ ≥ {usd(lc.twap_fast_short_min_usd)}"
            f" ve {float(lc.twap_fast_rate_x):g} kat")
    st = await kv_get(STATS_KV) or {}
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

