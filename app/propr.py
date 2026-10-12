"""propr.xyz listesi — kullanıcının trade ettiği platform.

Bir sinyal propr'da da listeli bir enstrümana aitse alert'lere ve dashboard'a
"✅ PROPR'da listeli" işareti eklenir (yalnız bilgi — işlem önerisi değil). PROPR'a bağlı
taramalar (kripto/hisse hacim rekoru, açılış raporu, kapalı seans, ana dex metrik kaydı,
🧪 laboratuvar evreni) da bu listeye bakar.

CANLI LİSTE (12.10, kullanıcı: "bayadır eski listeye bakıyoruz"). Elle kopyalanan liste eskimişti:
propr 12 sembol eklemiş (S, IO, USELESS, RSR, AZTEC, NIL, STX, GRIFFAIN, SAND, MINA, xyz:SNXX,
xyz:IREN), 57'sini kara listeye almıştı (TSM, NFLX, WIF, KAITO…). Artık propr'ın kendi evren
sayfasının okuduğu uç (`/api/universe/config`: asset + status whitelisted/blacklisted) saatte bir
çekilir; `is_listed` bellekteki kümeye bakar (G/Ç yok, senkron — sıcak yoldan da çağrılır).

KİMLİK DEX'Lİ: propr HIP-3 varlıkları dex ön ekiyle yazar ('xyz:IREN' listeli, 'para:IREN' kara
listede; ana dex 'STX' = Stacks listeli, 'para:STX' değil). Ön ekli sorgu TAM eşleşir; ön eksiz
(yalnız sembol) sorgu herhangi bir listeli kimliğin sembolüyle eşleşir — çağıranlar bu yüzden
ellerinde varsa coin kimliğini verir.

GÜVENCE: çekim düşerse, şekil tutmazsa ya da liste şüpheli küçükse (< MIN_IDS ya da öncekinin
yarısından az) son iyi liste (kv `propr_live`) kalır; o da yoksa aşağıdaki DEFAULT_PROPR (12.10
anlık görüntüsü). Şüpheli küçük liste art arda SUS_ACCEPT kez aynı gelirse gerçek sayılır.
Ayarlar → "propr.xyz ek semboller" listeye yine ekler (canlı listeden bağımsız).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging

from .config import get_config

log = logging.getLogger("propr")

LIVE_URL = "https://www.propr.xyz/api/universe/config"
LIVE_KV = "propr_live"          # {"ids": [norm kimlik], "ts"} — son İYİ liste
STATS_KV = "propr_stats"        # son çekim: ok / http / hata / sayılar / şüpheli liste sayacı
CHANGES_KV = "propr_changes"    # [{ts, added, removed}] — liste değişiklik günlüğü (🧪 evren de buna bağlı)
CHANGES_MAX = 50
MIN_IDS = 40                    # bundan küçük beyaz liste kabul edilmez
MIN_SHARE = 0.5                 # öncekinin yarısından küçükse de (toplu silinme = önce hata say)
SUS_ACCEPT = 3                  # şüpheli liste art arda bu kadar kez AYNI gelirse kabul edilir
FIRST_NOTIFY_MAX = 20           # ilk çekimde (taban gömülü liste) fark en çok bu kadarsa bildirilir
START_DELAY = 20                # açılıştan sonra ilk çekim (sn)
HEADERS = {"Accept": "application/json",
           "Referer": "https://www.propr.xyz/universe",
           "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                          " (KHTML, like Gecko) Chrome/124.0 Safari/537.36")}

# propr.xyz/api/universe/config 12.10.2026 — 140 whitelisted (89 ana dex + 51 xyz). Canlı liste yoksa bu.
DEFAULT_PROPR: frozenset[str] = frozenset({
    # CRYPTO (ana dex)
    "AAVE", "ADA", "AERO", "ALGO", "APT", "ARB", "ASTER", "ATOM", "AVAX", "AZTEC", "BCH", "BNB",
    "BOME", "BTC", "CASHCAT", "CC", "CHIP", "CRV", "DASH", "DOGE", "DOT", "EIGEN", "ENA", "ETH",
    "ETHFI", "FARTCOIN", "FET", "FIL", "GRAM", "GRASS", "GRIFFAIN", "HBAR", "HYPE", "ICP", "INJ",
    "IO", "JTO", "JUP", "LDO", "LINK", "LIT", "LTC", "MEGA", "MET", "MINA", "MNT", "MON", "MORPHO",
    "NEAR", "NIL", "ONDO", "OP", "PAXG", "PENDLE", "PENGU", "POL", "PONS", "PUMP", "PURR", "PYTH",
    "RENDER", "RSR", "S", "SAND", "SEI", "SOL", "SPX", "STRK", "STX", "SUI", "SYRUP", "TAO", "TIA",
    "TRUMP", "TRX", "UNI", "USELESS", "VIRTUAL", "VVV", "WLD", "XLM", "XMR", "XPL", "XRP", "ZEC",
    "ZK", "ZRO", "KBONK", "KPEPE",
    # EQ / IDX / CMDTY / FX (xyz dex)
    "xyz:AAPL", "xyz:AMD", "xyz:AMZN", "xyz:AVGO", "xyz:BABA", "xyz:BB", "xyz:BE", "xyz:BRENTOIL",
    "xyz:CBRS", "xyz:CL", "xyz:COIN", "xyz:COPPER", "xyz:CRCL", "xyz:CRWV", "xyz:CXMT", "xyz:DELL",
    "xyz:DRAM", "xyz:EUR", "xyz:EWY", "xyz:GOLD", "xyz:GOOGL", "xyz:HOOD", "xyz:INTC", "xyz:IREN",
    "xyz:JPY", "xyz:KR200", "xyz:LITE", "xyz:META", "xyz:MRNA", "xyz:MRVL", "xyz:MSFT", "xyz:MSTR",
    "xyz:MU", "xyz:NBIS", "xyz:NVDA", "xyz:ORCL", "xyz:PLTR", "xyz:PURRDAT", "xyz:SILVER",
    "xyz:SKHX", "xyz:SKHY", "xyz:SMSN", "xyz:SNDK", "xyz:SNXX", "xyz:SOXL", "xyz:SP500", "xyz:SPCX",
    "xyz:TSLA", "xyz:UNITREE", "xyz:XYZ100", "xyz:ZHIPU",
})


def norm(asset: str) -> str:
    """Kimlik biçimi: ana dex 'KPEPE' (büyük harf), HIP-3 'xyz:IREN' (dex küçük, sembol büyük)."""
    s = (asset or "").strip()
    if ":" in s:
        dex, sym = s.split(":", 1)
        return f"{dex.strip().lower()}:{sym.strip().upper()}"
    return s.upper()


def _syms(ids) -> frozenset[str]:
    return frozenset(i.split(":")[-1] for i in ids)


_DEF_SYMS = _syms(DEFAULT_PROPR)
# Canlı küme YERİNDE değişir (clear+update): format/metrics is_listed'i import anında bağlıyor.
_LIVE: set[str] = set()
_LIVE_SYMS: set[str] = set()
_VER = [0]                                   # canlı liste her değiştiğinde +1
_EXTRA: dict = {"raw": None, "ids": frozenset(), "syms": frozenset(), "id_syms": frozenset()}


def live_on(cfg=None) -> bool:
    return bool(getattr(cfg or get_config(), "propr_live", True))


def _base(cfg=None) -> tuple:
    if _LIVE and live_on(cfg):
        return _LIVE, _LIVE_SYMS
    return DEFAULT_PROPR, _DEF_SYMS


def _extras(cfg=None) -> tuple[frozenset, frozenset, frozenset]:
    """Ayarlar → propr_symbols (virgüllü) → (ön ekli kimlikler, ön eksiz semboller, ön eklilerin sembolleri).
    Ön ekli giriş ön ekli sorguda tam eşleşir; ön eksiz giriş her dex'te sembolle eşleşir."""
    raw = getattr(cfg or get_config(), "propr_symbols", "") or ""
    if raw != _EXTRA["raw"]:
        items = [norm(s) for s in raw.replace(";", ",").split(",") if s.strip()]
        ids = frozenset(i for i in items if ":" in i)
        _EXTRA.update(raw=raw, ids=ids, syms=frozenset(i for i in items if ":" not in i), id_syms=_syms(ids))
    return _EXTRA["ids"], _EXTRA["syms"], _EXTRA["id_syms"]


def is_listed(symbol_or_coin: str) -> bool:
    """'xyz:IREN' / 'para:IREN' → tam kimlik; 'IREN' / 'kPEPE' → sembol. Senkron, G/Ç yok."""
    n = norm(symbol_or_coin)
    if not n:
        return False
    sym = n.split(":")[-1]
    ex_ids, ex_syms, ex_id_syms = _extras()
    if sym in ex_syms or (n in ex_ids if ":" in n else sym in ex_id_syms):
        return True
    ids, syms = _base()
    return n in ids if ":" in n else sym in syms


def version() -> int:
    """Önbellekler (🧪 evren) bunu izler: canlı liste değişince ya da canlı/gömülü geçişinde değişir."""
    return _VER[0] * 2 + (1 if (_LIVE and live_on()) else 0)


def current_ids() -> set[str]:
    """Geçerli liste (canlı ya da gömülü) + elle eklenenler — /tani ve sayfalar için."""
    ids, _ = _base()
    ex_ids, ex_syms, _ = _extras()
    return set(ids) | set(ex_ids) | set(ex_syms)


def current_symbols() -> set[str]:
    return {i.split(":")[-1] for i in current_ids()}


def source() -> str:
    return "canlı" if (_LIVE and live_on()) else "gömülü (12.10)"


def set_live(ids) -> bool:
    """Canlı kümeyi YERİNDE değiştir; değiştiyse True. Boş = canlı yok (gömülü listeye dön)."""
    new = {norm(i) for i in (ids or []) if str(i).strip()}
    if new == _LIVE:
        return False
    _LIVE.clear()
    _LIVE.update(new)
    _LIVE_SYMS.clear()
    _LIVE_SYMS.update(_syms(new))
    _VER[0] += 1
    return True


async def load_live() -> int:
    """Açılış: ilk çekime kadar kv'deki son iyi liste geçerli."""
    from .db import kv_get
    rec = await kv_get(LIVE_KV) or {}
    set_live(rec.get("ids") or [])
    return len(_LIVE)


def parse(payload) -> tuple[list[str], int, int]:
    """API yanıtı → (beyaz listedeki kimlikler (norm, sıralı), kara liste sayısı, tanınmayan durum sayısı).
    Şekil tutmazsa ValueError — 'liste boş' sanılıp radarlar susmasın."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("yanıtta data listesi yok")
    white: set[str] = set()
    black = other = bad = 0
    for r in rows:
        a = r.get("asset") if isinstance(r, dict) else None
        if not isinstance(a, str) or not a.strip():
            bad += 1
            continue
        st = str(r.get("status") or "").strip().lower()
        if st == "whitelisted":
            white.add(norm(a))
        elif st == "blacklisted":
            black += 1
        else:
            other += 1
    if bad * 10 > len(rows):
        raise ValueError(f"{bad}/{len(rows)} satırda varlık adı yok")
    if not white:                 # durum adı değişti / bakım: hiç listeli yok = şekil hatası, 'boş liste' değil
        raise ValueError(f"listeli (whitelisted) satır yok ({len(rows)} satır, {other} tanınmayan durum)")
    if other * 2 > len(rows):
        raise ValueError(f"{other}/{len(rows)} satırın durumu tanınmıyor")
    return sorted(white), black, other


def _suspicious(new: list[str], prev: set[str]) -> str:
    if len(new) < MIN_IDS:
        return f"beyaz liste şüpheli küçük ({len(new)} < {MIN_IDS})"
    if prev and len(new) < len(prev) * MIN_SHARE:
        return f"liste yarıdan fazla küçüldü ({len(prev)} → {len(new)})"
    return ""


async def fetch(session) -> tuple[int, object]:
    import aiohttp
    async with session.get(LIVE_URL, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as r:
        if r.status != 200:
            return r.status, None
        return r.status, await r.json(content_type=None)


async def refresh(session, fetcher=None) -> dict:
    """Bir çekim. Başarılıysa canlı listeyi değiştirir, kv'ye ve değişiklik günlüğüne yazar.
    Dönüş: {ok, err, http, n, n_black, added, removed, notify}. İlk çekimde taban gömülü listedir:
    fark küçükse (≤ FIRST_NOTIFY_MAX — ör. 12.10'da propr RSR'yi bir saat sonra kara listeye aldı)
    bildirilir; büyükse (eskimiş gömülü listeyle yeni kurulum) toplu mesaj atılmaz, günlüğe yazılır."""
    from .db import kv_get, kv_set, now
    ts = now()
    prev_rec = await kv_get(LIVE_KV) or {}
    prev = {norm(i) for i in (prev_rec.get("ids") or [])} or set(DEFAULT_PROPR)
    st = await kv_get(STATS_KV) or {}
    # Şüpheli liste sayacı: farklı ya da şüphesiz liste gelince sıfırlanır; ağ/şekil hatası ne ilerletir
    # ne sıfırlar (aralıklı 403/503 kabulü sonsuza ertelemesin).
    out = {"ts": ts, "ok": False, "http": None, "err": "", "n": len(prev), "n_black": None,
           "added": [], "removed": [], "notify": False,
           "last_ok": st.get("last_ok"), "sus_sig": st.get("sus_sig") or "", "sus_n": int(st.get("sus_n") or 0)}
    try:
        http, payload = await (fetcher or fetch)(session)
        out["http"] = http
        if http != 200 or payload is None:
            raise ValueError(f"HTTP {http}")
        ids, out["n_black"], other = parse(payload)
        if other:
            out["other"] = other
        why = _suspicious(ids, prev) if set(ids) != prev else ""     # uygulanacak bir şey yoksa şüphe de yok
        if why:
            sig = hashlib.sha1(",".join(ids).encode()).hexdigest()[:12]
            n = int(st.get("sus_n") or 0) + 1 if st.get("sus_sig") == sig else 1
            if n < SUS_ACCEPT:
                out.update(err=f"{why} — kabul edilmedi ({n}/{SUS_ACCEPT}), önceki liste geçerli",
                           sus_sig=sig, sus_n=n)
                raise _Hold()
            log.warning("propr: %s — art arda %d kez aynı geldi, kabul edildi", why, n)
        out.update(sus_sig="", sus_n=0)
        new = set(ids)
        added, removed = sorted(new - prev), sorted(prev - new)
        set_live(new)
        await kv_set(LIVE_KV, {"ids": sorted(new), "ts": ts})     # ts = son başarılı çekim (değişmese de)
        if added or removed:
            hist = await kv_get(CHANGES_KV) or []
            hist.append({"ts": ts, "added": added, "removed": removed,
                         "base": "kv" if prev_rec else "gömülü"})
            await kv_set(CHANGES_KV, hist[-CHANGES_MAX:])
            log.info("propr listesi değişti: +%d (%s) −%d (%s)", len(added), ", ".join(added[:12]),
                     len(removed), ", ".join(removed[:12]))
        out.update(ok=True, n=len(new), added=added, removed=removed, last_ok=ts,
                   notify=bool((added or removed)
                               and (prev_rec or len(added) + len(removed) <= FIRST_NOTIFY_MAX)))
    except _Hold:
        pass
    except Exception as e:                       # noqa: BLE001 — ağ/şekil: son iyi liste kalır
        out["err"] = out["err"] or f"{type(e).__name__}: {e}"[:200]
        log.warning("propr listesi çekilemedi (%s) — son iyi liste geçerli", out["err"])
    try:
        await kv_set(STATS_KV, {k: v for k, v in out.items() if k != "notify"})
    except Exception:                            # noqa: BLE001
        log.debug("propr istatistiği yazılamadı", exc_info=True)
    return out


class _Hold(Exception):
    """Şüpheli liste: bu tur uygulanmadı (hata değil, bekleme)."""


_sleep = asyncio.sleep          # testler sahte saatle değiştirir


async def loop(cfg, session, notifier=None) -> None:
    """Saatte bir (propr_sync_sec) çek; liste değiştiyse sahibe tek mesaj (kind 'propr').
    Her tur nabız atar — propr'a ulaşılamaması görev ölümü sayılmaz, /tani'de yazar."""
    from .health import beat
    await _sleep(START_DELAY)
    while True:
        try:
            await beat("propr")
            if live_on(cfg):
                r = await refresh(session)
                if r.get("notify") and notifier is not None:
                    from .telegram import format as fmt
                    await notifier.send("propr", fmt.propr_change(r["added"], r["removed"], r["n"]),
                                        key=f"propr:{r['ts']}", public=False)
        except asyncio.CancelledError:
            raise
        except Exception:                        # noqa: BLE001
            log.warning("propr turu", exc_info=True)
        await _sleep(max(600, int(getattr(cfg, "propr_sync_sec", 3600) or 3600)))


def _ago(sec) -> str:
    sec = max(0, int(sec or 0))
    if sec < 90:
        return f"{sec} sn"
    if sec < 5400:
        return f"{sec // 60} dk"
    if sec < 172800:
        return f"{sec // 3600} sa"
    return f"{sec // 86400} gün"


async def diag_line(cfg) -> str:
    """/tani satırı: kaynak, sayılar, son çekim, son değişiklik, elle eklenenlerin durumu."""
    from .db import kv_get, now
    t = now()
    ids = _base(cfg)[0]
    n_main = sum(1 for i in ids if ":" not in i)
    parts = [f"propr listesi: {source()} · {len(ids)} enstrüman ({n_main} ana dex kripto · "
             f"{len(ids) - n_main} HIP-3)"]
    if not live_on(cfg):
        parts.append("canlı senkron KAPALI (propr_live=0)")
    st = await kv_get(STATS_KV) or {}
    if st.get("ts"):
        if st.get("ok"):
            parts.append(f"son çekim ✓ {_ago(t - int(st['ts']))} önce (kara liste {st.get('n_black')})")
        else:
            lo = st.get("last_ok")
            parts.append(f"⚠ son çekim başarısız {_ago(t - int(st['ts']))} önce: {st.get('err') or '?'}"
                         + (f" · son başarılı {_ago(t - int(lo))} önce" if lo else " · hiç başarılı çekim yok"))
    elif live_on(cfg):
        parts.append("henüz çekilmedi")
    hist = await kv_get(CHANGES_KV) or []
    if hist:
        h = hist[-1]
        parts.append(f"son değişiklik {_ago(t - int(h.get('ts') or t))} önce: +{len(h.get('added') or [])}"
                     f" / −{len(h.get('removed') or [])}")
    ex_ids, ex_syms, _ = _extras(cfg)
    extra = sorted(ex_ids | ex_syms)
    if extra:
        _, syms = _base(cfg)
        dup = [e for e in extra if (e in ids if ":" in e else e in syms)]
        own = [e for e in extra if e not in dup]
        if own:
            parts.append("elle eklenen (propr listesinde yok): " + ", ".join(own[:15]))
        if dup:
            parts.append("elle eklenen ama zaten listede: " + ", ".join(dup[:15]))
    return " · ".join(parts)


PROPR_NOTE = "✅ <b>PROPR'da listeli</b>"
