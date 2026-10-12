"""propr.xyz listesi — kullanıcının trade ettiği platform.

Bir sinyal propr'da da listeli bir enstrümana aitse alert'lere ve dashboard'a
"✅ PROPR'da listeli" işareti eklenir (yalnız bilgi — işlem önerisi değil). PROPR'a bağlı
taramalar (kripto/hisse hacim rekoru, açılış raporu, kapalı seans, ana dex metrik kaydı,
🧪 laboratuvar evreni) da bu listeye bakar.

LİSTE YALNIZ BÜYÜR (kullanıcı 12.10: "canlı takip etmene gerek yok, arada sırada çıkardıklarını
geri eklerler; sinyaller fazla olsun eksik olmasın"). Liste = propr'ın bugüne kadar listelediği her
şeyin birleşimi: 07.09 elle kopyası + 12.10 listesi (12 yeni) + aynı gün eklenenler (TSM, RKLB, CRWD).
propr bir şeyi kara listeye alsa da (12.10'da 57 sembol, sonra RSR) biz ÇIKARMAYIZ ve mesaj atmayız.
propr'ın evren ucu (`/api/universe/config`) saatte bir sessizce okunur — yalnız YENİ listelenen
eklenir (kv `propr_live` = birleşik küme). `is_listed` bellekteki kümeye bakar (G/Ç yok, senkron).

KİMLİK DEX'Lİ: propr HIP-3 varlıkları dex ön ekiyle yazar ('xyz:IREN' listeli, 'para:IREN' değil;
ana dex 'STX' = Stacks, 'para:STX' = Seagate değil). Ön ekli sorgu TAM eşleşir; ön eksiz (yalnız
sembol) sorgu herhangi bir listeli kimliğin sembolüyle eşleşir — çağıranlar elinde varsa coin
kimliğini verir.

GÜVENCE: çekim düşerse / şekil tutmazsa hiçbir şey değişmez. Tek seferde MAX_ADD'den fazla yeni kimlik
gelirse (API hatası her şeyi listeli gösterirse kalıcı çöp olmasın) art arda SUS_ACCEPT kez aynı
gelmeden eklenmez. Ayarlar → "propr.xyz ek semboller" listeye elle ekler.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging

from .config import get_config

log = logging.getLogger("propr")

LIVE_URL = "https://www.propr.xyz/api/universe/config"
LIVE_KV = "propr_live"          # {"ids": [norm kimlik], "ts"} — BİRLEŞİK küme (yalnız büyür)
STATS_KV = "propr_stats"        # son çekim: ok / http / hata / sayılar / şüpheli ekleme sayacı
CHANGES_KV = "propr_changes"    # [{ts, added}] — ekleme günlüğü (🧪 evren de buna bağlı)
CHANGES_MAX = 50
MAX_ADD = 40                    # tek çekimde bundan fazla YENİ kimlik = şüpheli (3 kez aynı gelmeden eklenmez)
SUS_ACCEPT = 3                  # şüpheli ekleme art arda bu kadar kez AYNI gelirse kabul edilir
START_DELAY = 20                # açılıştan sonra ilk çekim (sn)
HEADERS = {"Accept": "application/json",
           "Referer": "https://www.propr.xyz/universe",
           "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                          " (KHTML, like Gecko) Chrome/124.0 Safari/537.36")}

# propr'ın bugüne kadar listelediği her şey (07.09 kopyası ∪ 12.10 listesi ∪ 12.10 14:38 eklenenleri):
# 114 ana dex + 83 xyz + 1 para = 198. Kara listeye alınanlar BİLEREK içeride (geri ekleniyorlar).
DEFAULT_PROPR: frozenset[str] = frozenset({
    # CRYPTO (ana dex)
    "2Z", "AAVE", "ACE", "ADA", "AERO", "ALGO", "APT", "ARB", "ASTER", "ATOM", "AVAX", "AZTEC",
    "BCH", "BERA", "BNB", "BOME", "BTC", "CASHCAT", "CC", "CFX", "CHIP", "CRV", "DASH", "DOGE",
    "DOT", "DYDX", "EIGEN", "ENA", "ENS", "ETC", "ETH", "ETHFI", "FARTCOIN", "FET", "FIL",
    "GRAM", "GRASS", "GRIFFAIN", "HBAR", "HEMI", "HYPE", "ICP", "INJ", "IO", "JTO", "JUP",
    "KAITO", "KAS", "KBONK", "KPEPE", "KSHIB", "LDO", "LINEA", "LINK", "LIT", "LTC", "MEGA",
    "MET", "MINA", "MNT", "MON", "MORPHO", "NEAR", "NIL", "ONDO", "OP", "PAXG", "PENDLE",
    "PENGU", "PEOPLE", "POL", "PONS", "POPCAT", "PUMP", "PURR", "PYTH", "RENDER", "RSR", "RUNE",
    "S", "SAGA", "SAND", "SEI", "SKR", "SKY", "SOL", "SPX", "STABLE", "STBL", "STRK", "STX",
    "SUI", "SUSHI", "SYRUP", "TAO", "TIA", "TRUMP", "TRX", "UNI", "USELESS", "VIRTUAL", "VVV",
    "W", "WIF", "WLD", "WLFI", "XLM", "XMR", "XPL", "XRP", "ZEC", "ZEN", "ZK", "ZRO",
    # EQ / IDX / CMDTY / FX / ETF (xyz dex)
    "xyz:AAPL", "xyz:AMD", "xyz:AMZN", "xyz:ARM", "xyz:ASML", "xyz:AVGO", "xyz:BABA", "xyz:BB",
    "xyz:BE", "xyz:BOT", "xyz:BRENTOIL", "xyz:CBRS", "xyz:CL", "xyz:COIN", "xyz:COPPER",
    "xyz:CRCL", "xyz:CRWD", "xyz:CRWV", "xyz:CXMT", "xyz:DELL", "xyz:DRAM", "xyz:EUR",
    "xyz:EWY", "xyz:GIGADEV", "xyz:GME", "xyz:GOLD", "xyz:GOOGL", "xyz:HIMS", "xyz:HOOD",
    "xyz:HYUNDAI", "xyz:IBM", "xyz:INTC", "xyz:IREN", "xyz:JP225", "xyz:JPY", "xyz:KIOXIA",
    "xyz:KR200", "xyz:LITE", "xyz:LLY", "xyz:META", "xyz:MINIMAX", "xyz:MRNA", "xyz:MRVL",
    "xyz:MSFT", "xyz:MSTR", "xyz:MU", "xyz:NATGAS", "xyz:NBIS", "xyz:NFLX", "xyz:NOK",
    "xyz:NOW", "xyz:NVDA", "xyz:ORCL", "xyz:PALLADIUM", "xyz:PLATINUM", "xyz:PLTR",
    "xyz:PURRDAT", "xyz:QCOM", "xyz:QNT", "xyz:RKLB", "xyz:SHAZ", "xyz:SHEIN", "xyz:SILVER",
    "xyz:SKHX", "xyz:SKHY", "xyz:SMH", "xyz:SMSN", "xyz:SNDK", "xyz:SNXX", "xyz:SOFTBANK",
    "xyz:SOXL", "xyz:SP500", "xyz:SPCX", "xyz:STRC", "xyz:TSLA", "xyz:TSM", "xyz:UNITREE",
    "xyz:URNM", "xyz:USAR", "xyz:WDC", "xyz:XLE", "xyz:XYZ100", "xyz:ZHIPU",
    # HIP-3 kripto dex
    "para:ANSEM",
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
    return "birleşik (yalnız büyür)" if (_LIVE and live_on()) else "gömülü (12.10 birleşimi)"


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
    """Açılış: kv'deki birleşik küme ∪ gömülü liste (koddaki ekleme hemen geçerli; hiçbir şey düşmez)."""
    from .db import kv_get
    rec = await kv_get(LIVE_KV) or {}
    ids = {norm(i) for i in (rec.get("ids") or [])}
    set_live(ids | set(DEFAULT_PROPR) if ids else [])
    return len(_LIVE)


def parse(payload) -> tuple[list[str], int, int]:
    """API yanıtı → (beyaz listedeki kimlikler (norm, sıralı), kara liste sayısı, tanınmayan durum sayısı).
    Şekil tutmazsa ValueError — durum adı değişip hiçbir şey listeli görünmezse de."""
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
    if not white:
        raise ValueError(f"listeli (whitelisted) satır yok ({len(rows)} satır, {other} tanınmayan durum)")
    if other * 2 > len(rows):
        raise ValueError(f"{other}/{len(rows)} satırın durumu tanınmıyor")
    return sorted(white), black, other


async def fetch(session) -> tuple[int, object]:
    import aiohttp
    async with session.get(LIVE_URL, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as r:
        if r.status != 200:
            return r.status, None
        return r.status, await r.json(content_type=None)


async def refresh(session, fetcher=None) -> dict:
    """Bir çekim — YALNIZ EKLER. propr'ın şu an listelediği ∪ önceki birleşik küme ∪ gömülü liste;
    propr'ın kara listeye aldığı hiçbir şey çıkmaz, mesaj yok. Dönüş: {ok, err, http, n, n_white,
    n_black, added}. Tek seferde MAX_ADD'den fazla yeni kimlik şüphelidir: art arda SUS_ACCEPT kez aynı
    gelmeden eklenmez (sayaç ağ/şekil hatasında ne ilerler ne sıfırlanır)."""
    from .db import kv_get, kv_set, now
    ts = now()
    prev_rec = await kv_get(LIVE_KV) or {}
    prev = {norm(i) for i in (prev_rec.get("ids") or [])} | set(DEFAULT_PROPR)
    st = await kv_get(STATS_KV) or {}
    out = {"ts": ts, "ok": False, "http": None, "err": "", "n": len(prev), "n_white": st.get("n_white"),
           "n_black": st.get("n_black"), "added": [], "last_ok": st.get("last_ok"),
           "sus_sig": st.get("sus_sig") or "", "sus_n": int(st.get("sus_n") or 0)}
    try:
        http, payload = await (fetcher or fetch)(session)
        out["http"] = http
        if http != 200 or payload is None:
            raise ValueError(f"HTTP {http}")
        white, out["n_black"], other = parse(payload)
        out["n_white"] = len(white)
        if other:
            out["other"] = other
        added = sorted(set(white) - prev)
        if len(added) > MAX_ADD:
            sig = hashlib.sha1(",".join(added).encode()).hexdigest()[:12]
            n = int(out["sus_n"]) + 1 if out["sus_sig"] == sig else 1
            if n < SUS_ACCEPT:
                out.update(err=f"tek seferde {len(added)} yeni kimlik — eklenmedi ({n}/{SUS_ACCEPT})",
                           sus_sig=sig, sus_n=n)
                raise _Hold()
            log.warning("propr: %d yeni kimlik art arda %d kez aynı geldi, eklendi", len(added), n)
        out.update(sus_sig="", sus_n=0)
        union = prev | set(white)
        set_live(union)
        await kv_set(LIVE_KV, {"ids": sorted(union), "ts": ts})     # ts = son başarılı çekim
        if added:
            hist = await kv_get(CHANGES_KV) or []
            hist.append({"ts": ts, "added": added})
            await kv_set(CHANGES_KV, hist[-CHANGES_MAX:])
            log.info("propr listesine eklendi (%d): %s", len(added), ", ".join(added[:20]))
        out.update(ok=True, n=len(union), added=added, last_ok=ts)
    except _Hold:
        pass
    except Exception as e:                       # noqa: BLE001 — ağ/şekil: liste değişmez
        out["err"] = out["err"] or f"{type(e).__name__}: {e}"[:200]
        log.warning("propr listesi çekilemedi (%s) — liste değişmedi", out["err"])
    try:
        await kv_set(STATS_KV, {k: v for k, v in out.items() if k != "added"} | {"n_added": len(out["added"])})
    except Exception:                            # noqa: BLE001
        log.debug("propr istatistiği yazılamadı", exc_info=True)
    return out


class _Hold(Exception):
    """Şüpheli ekleme: bu tur uygulanmadı (hata değil, bekleme)."""


_sleep = asyncio.sleep          # testler sahte saatle değiştirir


async def loop(cfg, session) -> None:
    """Saatte bir (propr_sync_sec) sessizce çek; yalnız yeni listelenen eklenir. Mesaj YOK (kullanıcı
    12.10). Her tur nabız atar — propr'a ulaşılamaması görev ölümü sayılmaz, /tani'de yazar."""
    from .health import beat
    await _sleep(START_DELAY)
    while True:
        try:
            await beat("propr")
            if live_on(cfg):
                await refresh(session)
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
    """/tani satırı: kaynak, sayılar (propr'ın şu an listelediği / tutulan), son çekim, son ekleme."""
    from .db import kv_get, now
    t = now()
    ids = _base(cfg)[0]
    n_main = sum(1 for i in ids if ":" not in i)
    parts = [f"propr listesi: {source()} · {len(ids)} enstrüman ({n_main} ana dex kripto · "
             f"{len(ids) - n_main} HIP-3)"]
    if not live_on(cfg):
        parts.append("propr'dan okuma KAPALI (propr_live=0)")
    st = await kv_get(STATS_KV) or {}
    if st.get("n_white") is not None:
        parts.append(f"propr şu an {st['n_white']} listeliyor — kara listeye aldıkları da tutulur")
    if st.get("ts"):
        if st.get("ok"):
            parts.append(f"son çekim ✓ {_ago(t - int(st['ts']))} önce")
        else:
            lo = st.get("last_ok")
            parts.append(f"⚠ son çekim başarısız {_ago(t - int(st['ts']))} önce: {st.get('err') or '?'}"
                         + (f" · son başarılı {_ago(t - int(lo))} önce" if lo else " · hiç başarılı çekim yok"))
    elif live_on(cfg):
        parts.append("henüz çekilmedi")
    hist = [h for h in (await kv_get(CHANGES_KV) or []) if h.get("added")]
    if hist:
        h = hist[-1]
        parts.append(f"son ekleme {_ago(t - int(h.get('ts') or t))} önce: "
                     + ", ".join(str(x).split(":")[-1] for x in h["added"][:8])
                     + (f" … +{len(h['added']) - 8}" if len(h["added"]) > 8 else ""))
    ex_ids, ex_syms, _ = _extras(cfg)
    extra = sorted(ex_ids | ex_syms)
    if extra:
        _, syms = _base(cfg)
        dup = [e for e in extra if (e in ids if ":" in e else e in syms)]
        own = [e for e in extra if e not in dup]
        if own:
            parts.append("elle eklenen: " + ", ".join(own[:15]))
        if dup:
            parts.append("elle eklenen ama zaten listede: " + ", ".join(dup[:15]))
    return " · ".join(parts)


PROPR_NOTE = "✅ <b>PROPR'da listeli</b>"
