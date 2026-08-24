"""Earnings takvimi — birincil kaynak Yahoo (yfinance, keysiz),
opsiyonel iyileştirici Finnhub (FINNHUB_API_KEY verilirse).
"""
import asyncio
import logging
import time as _time
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import aiohttp

from ..assets import excluded_set, has_earnings
from ..config import Config
from ..db import db, now

log = logging.getLogger("earnings.calendar")

ET = ZoneInfo("America/New_York")
TR = ZoneInfo("Europe/Istanbul")

# HL sembolü → Yahoo sembolü (ABD dışı listelemeler için).
# Kore/Japonya hisseleri Yahoo'da borsa son ekiyle yaşar; earnings verisi oradan gelir.
# /settings'teki "yahoo_symbol_map" ile genişletilebilir: "SMSN:005930.KS;X:Y.T"
DEFAULT_SYMBOL_MAP: dict[str, str] = {
    "SMSN": "005930.KS",      # Samsung Electronics (KRX)
    "SKHX": "000660.KS",      # SK Hynix (KRX)
    "HYUNDAI": "005380.KS",   # Hyundai Motor (KRX)
    "SOFTBANK": "9984.T",     # SoftBank Group (TSE)
    "KIOXIA": "285A.T",       # Kioxia Holdings (TSE)
}


def parse_symbol_map(s: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (s or "").split(";"):
        part = part.strip()
        if ":" in part:
            k, v = part.split(":", 1)
            if k.strip() and v.strip():
                out[k.strip().upper()] = v.strip()
    return out


# ---------------- Yahoo (birincil) ----------------

def _fetch_yahoo_sync(symbols: list[str], horizon_days: int,
                      symbol_map: dict[str, str] | None = None) -> dict[str, dict]:
    """Senkron — thread'de çalışır. symbol -> {date_et, hour_hint, eps_est}"""
    import yfinance as yf  # ağır import, sadece burada

    # yfinance "No earnings dates found" durumunu ERROR olarak basıyor — susturalım,
    # bizim için normal bir sonuç (kaynak bilmiyor, diğerleri devrede)
    for noisy in ("yfinance", "yfinance.data", "yfinance.utils"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)

    smap = symbol_map or {}
    out: dict[str, dict] = {}
    now_et = datetime.now(ET)
    horizon = now_et + timedelta(days=horizon_days)
    for sym in symbols:
        yahoo_sym = smap.get(sym, sym)
        try:
            df = yf.Ticker(yahoo_sym).get_earnings_dates(limit=12)
            if df is None or df.empty:
                continue
            future = []
            for ts_idx, row in df.iterrows():
                ts_et = ts_idx.tz_convert(ET) if ts_idx.tzinfo else ts_idx.tz_localize(ET)
                if now_et - timedelta(hours=12) <= ts_et <= horizon:
                    future.append((ts_et, row))
            if not future:
                continue
            ts_et, row = min(future, key=lambda x: x[0])
            t = ts_et.time()
            if t <= time(9, 30):
                hint = "bmo"
            elif t >= time(15, 0):
                hint = "amc"
            else:
                hint = "unknown"
            # Yahoo saat de verir; 00:00 gibi yer tutucu değilse dakikası dakikasına kullan
            exact = int(ts_et.timestamp()) if (t.hour or t.minute) else None
            eps = None
            try:
                v = row.get("EPS Estimate")
                if v is not None and v == v:  # NaN kontrolü
                    eps = float(v)
            except Exception:
                pass
            out[sym] = {"date_et": ts_et.strftime("%Y-%m-%d"), "hour_hint": hint,
                        "exact_ts": exact, "eps_est": eps, "source": "yahoo"}
        except Exception as e:
            log.debug("yahoo %s: %s", sym, e)
        _time.sleep(0.5)  # Yahoo'ya nazik davran
    return out


# ---------------- Finnhub (opsiyonel) ----------------

async def _fetch_finnhub(session: aiohttp.ClientSession, key: str,
                         horizon_days: int) -> dict[str, dict]:
    frm = datetime.now(ET).strftime("%Y-%m-%d")
    to = (datetime.now(ET) + timedelta(days=horizon_days)).strftime("%Y-%m-%d")
    url = (f"https://finnhub.io/api/v1/calendar/earnings?from={frm}&to={to}&token={key}")
    out: dict[str, dict] = {}
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                log.warning("finnhub HTTP %s", r.status)
                return out
            data = await r.json()
        for e in data.get("earningsCalendar", []):
            sym = (e.get("symbol") or "").upper()
            hour = {"bmo": "bmo", "amc": "amc"}.get(e.get("hour") or "", "unknown")
            out[sym] = {"date_et": e.get("date"), "hour_hint": hour,
                        "eps_est": e.get("epsEstimate"), "source": "finnhub"}
    except Exception as e:
        log.warning("finnhub alınamadı: %s", e)
    return out


# ---------------- TradingView (tam saat için) ----------------

TV_URL = "https://scanner.tradingview.com/america/scan"
# Origin/Referer olmadan datacenter IP'lerden (Railway) gelen çıplak POST'lar
# TradingView bot korumasınca 4xx'leniyor → kalıcı 'tradingview:0'. Tarayıcı
# başlıkları ekle. Son HTTP durumu kv calendar_stats'a yazılır (görünürlük).
TV_HEADERS = {"Content-Type": "application/json",
              "Origin": "https://www.tradingview.com",
              "Referer": "https://www.tradingview.com/",
              "Accept": "application/json",
              "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                             " (KHTML, like Gecko) Chrome/124.0 Safari/537.36")}
TV_TIME_ENUM = {1: "bmo", 2: "amc"}
_tv_last_status = {"status": None}

# Tarih çelişkisinde hangi kaynak kazanır (büyük = daha güvenilir)
SOURCE_RANK = {"tradingview": 4, "yahoo": 3, "nasdaq": 2, "finnhub": 1}


async def _fetch_tradingview(session: aiohttp.ClientSession, symbols: list[str],
                             horizon_days: int) -> dict[str, dict]:
    """TradingView screener — bilanço saatini dakikasına kadar verebilir."""
    out: dict[str, dict] = {}
    payload = {
        "filter": [{"left": "name", "operation": "in_range", "right": symbols[:400]}],
        "options": {"lang": "en"},
        "markets": ["america"],
        "symbols": {"query": {"types": []}, "tickers": []},
        "columns": ["name", "earnings_release_next_date", "earnings_release_next_time"],
        "range": [0, 500],
    }
    try:
        async with session.post(TV_URL, json=payload, headers=TV_HEADERS,
                                timeout=aiohttp.ClientTimeout(total=25)) as r:
            _tv_last_status["status"] = r.status
            if r.status != 200:
                log.warning("tradingview HTTP %s (Origin/Referer'a rağmen reddedildi?)",
                            r.status)
                return out
            data = await r.json(content_type=None)
    except Exception as e:
        _tv_last_status["status"] = f"err: {type(e).__name__}"
        log.debug("tradingview: %s", e)
        return out

    now_ts = int(datetime.now(ET).timestamp())
    horizon_ts = now_ts + horizon_days * 86400
    for row in (data.get("data") or []):
        d = row.get("d") or []
        if len(d) < 2 or not d[0] or not d[1]:
            continue
        sym = str(d[0]).upper()
        try:
            ts = int(float(d[1]))
        except (TypeError, ValueError):
            continue
        if not (now_ts - 2 * 86400 <= ts <= horizon_ts):
            continue
        dt_et = datetime.fromtimestamp(ts, ET)
        # 3. kolon ya enum (1/2) ya da unix saat damgası olabilir — ikisini de karşıla
        hint, exact = "unknown", None
        raw_t = d[2] if len(d) > 2 else None
        try:
            v = float(raw_t) if raw_t is not None else None
        except (TypeError, ValueError):
            v = None
        if v is not None and v > 100000:                 # zaman damgası
            dt_et = datetime.fromtimestamp(int(v), ET)
            exact = int(v)
        elif v is not None:
            hint = TV_TIME_ENUM.get(int(v), "unknown")
        t = dt_et.time()
        if hint == "unknown":
            if t.hour or t.minute:
                hint = "bmo" if t <= time(9, 30) else ("amc" if t >= time(15, 0) else "unknown")
        if exact is None and (t.hour or t.minute):
            exact = int(dt_et.timestamp())
        out[sym] = {"date_et": dt_et.strftime("%Y-%m-%d"), "hour_hint": hint,
                    "exact_ts": exact, "eps_est": None, "source": "tradingview"}
    return out


# ---------------- Nasdaq (yedek 2 — keysiz, tam ABD kapsamı) ----------------

NASDAQ_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                   " (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

NASDAQ_TIME_MAP = {"time-after-hours": "amc", "time-pre-market": "bmo"}


async def _fetch_nasdaq(session: aiohttp.ClientSession, horizon_days: int) -> dict[str, dict]:
    """Nasdaq'ın halka açık takvimi — gün gün çekilir (sadece hafta içi)."""
    out: dict[str, dict] = {}
    today = datetime.now(ET).date()
    for i in range(min(horizon_days, 14) + 1):
        d = today + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        ds = d.strftime("%Y-%m-%d")
        url = f"https://api.nasdaq.com/api/calendar/earnings?date={ds}"
        try:
            async with session.get(url, headers=NASDAQ_HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    log.debug("nasdaq %s HTTP %s", ds, r.status)
                    continue
                data = await r.json(content_type=None)
        except Exception as e:
            log.debug("nasdaq %s: %s", ds, e)
            continue
        for row in ((data.get("data") or {}).get("rows") or []):
            sym = (row.get("symbol") or "").upper().strip()
            if not sym or sym in out:
                continue
            eps = None
            try:
                raw = (row.get("epsForecast") or "").replace("$", "").replace("(", "-").replace(")", "")
                if raw:
                    eps = float(raw)
            except ValueError:
                pass
            out[sym] = {"date_et": ds,
                        "hour_hint": NASDAQ_TIME_MAP.get(row.get("time") or "", "unknown"),
                        "eps_est": eps, "source": "nasdaq"}
        await asyncio.sleep(0.4)
    return out


# ---------------- Yenileme ----------------

async def refresh_calendar(cfg: Config, session: aiohttp.ClientSession) -> int:
    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol FROM tickers")
        rows = await cur.fetchall()
    sym2coin = {r["symbol"]: r["coin"] for r in rows}
    all_symbols = sorted(sym2coin)
    # Endeks/emtia/FX/ETF/kripto ve pre-IPO'ların bilançosu yok — sorgulama.
    # Takipten çıkarılanlar (exclude_symbols) hiç sorgulanmaz.
    exc = excluded_set()
    symbols = [s for s in all_symbols
               if has_earnings(s) and s.upper() not in exc]
    skipped = len(all_symbols) - len(symbols)
    if not symbols:
        log.info("takvim: sorgulanacak hisse yok (evren %d, hepsi muaf)", len(all_symbols))
        return 0
    if skipped:
        log.info("takvim: %d hisse sorgulanacak, %d enstrüman muaf (endeks/emtia/FX/ETF/pre-IPO)",
                 len(symbols), skipped)

    smap = dict(DEFAULT_SYMBOL_MAP)
    smap.update(parse_symbol_map(getattr(cfg, "yahoo_symbol_map", "")))
    yahoo = await asyncio.to_thread(_fetch_yahoo_sync, symbols,
                                    cfg.calendar_horizon_days, smap)
    finnhub = {}
    if cfg.finnhub_api_key:
        fh_all = await _fetch_finnhub(session, cfg.finnhub_api_key, cfg.calendar_horizon_days)
        finnhub = {s: v for s, v in fh_all.items() if s in symbols}
    eligible = set(symbols)
    nasdaq_all = await _fetch_nasdaq(session, cfg.calendar_horizon_days)
    nasdaq = {s: v for s, v in nasdaq_all.items() if s in eligible}
    tv_all = await _fetch_tradingview(session, symbols, cfg.calendar_horizon_days)
    tradingview = {s: v for s, v in tv_all.items() if s in eligible}

    # Dört kaynağı güven sırasına göre birleştir. Tarih çelişkisinde GÜVENİLİR
    # kaynak kazanır (BIRD vakası: zayıf kaynak "bugün" derken TradingView doğru günü verir).
    merged: dict[str, dict] = {}
    for src_name, src in (("finnhub", finnhub), ("nasdaq", nasdaq),
                          ("yahoo", yahoo), ("tradingview", tradingview)):
        rank = SOURCE_RANK.get(src_name, 0)
        for sym, ev in src.items():
            cur = merged.get(sym)
            has_hint = ev.get("hour_hint") not in (None, "", "unknown")
            if cur is None:
                merged[sym] = {**ev, "_rank": rank, "source": src_name,
                               "_hint_rank": rank if has_hint else 0}
                continue
            if cur["date_et"] == ev["date_et"]:
                # hour_hint ÇELİŞKİSİNDE en güvenilir kaynak kazanır (yalnız 'boş
                # ise doldur' değil). Eskiden zayıf kaynağın 'amc'si sonraki
                # refresh'te güçlü kaynağın 'bmo'sunu geri çeviremiyordu (SHAZ).
                if has_hint and rank >= cur.get("_hint_rank", 0):
                    cur["hour_hint"] = ev["hour_hint"]
                    cur["_hint_rank"] = rank
                if not cur.get("exact_ts") and ev.get("exact_ts"):
                    cur["exact_ts"] = ev["exact_ts"]
                if cur.get("eps_est") is None:
                    cur["eps_est"] = ev.get("eps_est")
                if src_name not in (cur.get("source") or ""):
                    cur["source"] = f"{cur.get('source')}+{src_name}"
                cur["_rank"] = max(cur.get("_rank", 0), rank)
            elif rank > cur.get("_rank", 0):
                merged[sym] = {**ev, "_rank": rank, "source": src_name,
                               "_hint_rank": rank if has_hint else 0,
                               "note": f"⚠️ tarih düzeltildi: {cur.get('source')}"
                                       f" {cur['date_et']} diyordu → {src_name} {ev['date_et']}"}
            else:
                cur["note"] = f"⚠️ kaynak çelişkisi: {src_name} {ev['date_et']} diyor"

    # Saati hâlâ bilinmeyenleri işaretle — "bugün" diye gösterip sabah geçmiş olmasın
    for sym, m in merged.items():
        if m.get("hour_hint") in (None, "", "unknown") and not m.get("exact_ts"):
            m["note"] = (m.get("note") or "") + " ⚠️ saat belirsiz — sabah da olabilir!"

    stats = {"yahoo": len(yahoo), "finnhub": len(finnhub), "nasdaq": len(nasdaq),
             "tradingview": len(tradingview), "merged": len(merged),
             "tv_http": _tv_last_status["status"]}
    if not yahoo:
        log.warning("Yahoo takvimi 0 sonuç döndü (engel olabilir) — diğer kaynaklar taşıyor")
    log.info("takvim kaynakları: yahoo=%d tv=%d finnhub=%d nasdaq=%d → birleşik=%d",
             stats["yahoo"], stats["tradingview"], stats["finnhub"], stats["nasdaq"],
             stats["merged"])
    from ..db import kv_set
    await kv_set("calendar_stats", stats)

    n = 0
    async with db() as conn:
        # Geçersiz tarihli eski kayıtları temizle (due_loop'u çökertirlerdi).
        # Elle girilenleri (manual) İSTİSNA tut — /settime tarih doğrulaması
        # yapıyor ama eski bozuk manual satır varsa da 'değiştirilmez' vaadini
        # koru (kullanıcı /settime ile düzeltebilir).
        await conn.execute(
            "DELETE FROM earnings_events WHERE evaluated=0"
            " AND COALESCE(source,'') NOT LIKE '%manual%'"
            " AND (date_et IS NULL OR length(date_et) < 10 OR date_et NOT GLOB"
            " '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')")
        # Takipten çıkarılan hisselerin BEKLEYEN earnings kayıtları da silinir
        # (BIRD vakası) — değerlendirilmiş arşiv satırları korunur
        if exc:
            qx = ",".join("?" * len(exc))
            await conn.execute(
                f"DELETE FROM earnings_events WHERE evaluated=0"
                f" AND UPPER(symbol) IN ({qx})", tuple(exc))
        for sym, ev in merged.items():
            if not valid_date_et(ev.get("date_et")):
                continue
            # ÖNEMLİ: hour_hint artık "yapışkan" değil — daha güvenilir kaynak (merge
            # sırasında seçildi) eski değeri düzeltebilmeli. Yoksa yanlış bir 'amc'
            # sonsuza kadar kalıyor (SHAZ vakası). 'unknown' asla bilineni ezmez.
            # Elle girilen (manual) kayıtlara hiçbir kaynak dokunamaz.
            await conn.execute(
                """INSERT INTO earnings_events(symbol,coin,date_et,hour_hint,exact_ts,eps_est,source,note,created_ts)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol,date_et) DO UPDATE SET
                     hour_hint=CASE
                       WHEN COALESCE(excluded.hour_hint,'unknown')='unknown'
                         THEN earnings_events.hour_hint
                       ELSE excluded.hour_hint END,
                     exact_ts=CASE
                       WHEN excluded.exact_ts IS NOT NULL THEN excluded.exact_ts
                       WHEN COALESCE(excluded.hour_hint,'unknown')<>'unknown'
                            AND excluded.hour_hint<>earnings_events.hour_hint THEN NULL
                       ELSE earnings_events.exact_ts END,
                     eps_est=COALESCE(excluded.eps_est, earnings_events.eps_est),
                     source=excluded.source,
                     note=excluded.note
                   WHERE COALESCE(earnings_events.source,'') NOT LIKE '%manual%'""",
                (sym, sym2coin[sym], ev["date_et"], ev["hour_hint"], ev.get("exact_ts"),
                 ev.get("eps_est"), ev.get("source"), ev.get("note"), now()),
            )
            n += 1
        # Tarihi düzeltilen kayıtları temizle. Alert bayrağı işaretlenmiş olsa bile
        # sil — "pencere kaçtı" diye otomatik işaretlenen hatalı satırlar listede
        # kalıyordu (BIRD hem 06.08 hem 12.08 görünüyordu). Arşiv değeri olanlara
        # (snapshot alınmış ya da değerlendirilmiş) ve elle girilenlere dokunma.
        today = datetime.now(ET).strftime("%Y-%m-%d")
        for sym in symbols:
            if sym not in merged:
                continue
            new_date = merged[sym]["date_et"]
            # Tarih düzeltilince eski BEKLEYEN satır çift rapor + çift sicil
            # üretiyordu. Snapshot'sız eski satırları (geçmiş tarihli DAHİL) sil;
            # snapshot'lı olanları silme (arşiv değeri var) ama evaluated=1 +
            # 'tarih düzeltildi' notuyla KAPAT ki tekrar rapor/değerlendirme
            # üretmesin. Elle girilenlere dokunma.
            await conn.execute(
                """DELETE FROM earnings_events
                   WHERE symbol=? AND date_et<>? AND evaluated=0
                     AND COALESCE(source,'') NOT LIKE '%manual%'
                     AND id NOT IN (SELECT event_id FROM position_snapshots
                                    WHERE event_id IS NOT NULL)""",
                (sym, new_date))
            await conn.execute(
                """UPDATE earnings_events
                   SET evaluated=1, result_note=COALESCE(result_note,
                       '↪️ tarih düzeltildi → ' || ?)
                   WHERE symbol=? AND date_et<>? AND evaluated=0
                     AND COALESCE(source,'') NOT LIKE '%manual%'
                     AND id IN (SELECT event_id FROM position_snapshots
                                WHERE event_id IS NOT NULL)""",
                (new_date, sym, new_date))
    log.info("takvim yenilendi: %d HL-eşleşen earnings", n)
    return n


# ---------------- Zamanlama ----------------

def valid_date_et(date_str) -> bool:
    """date_et sağlam bir YYYY-MM-DD mi?"""
    if not isinstance(date_str, str):
        return False
    try:
        datetime.strptime(date_str.strip()[:10], "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _et_ts(date_str: str, hh: int, mm: int, day_offset: int = 0) -> int:
    # Bozuk tarih tüm döngüyü çökertmesin (tek kötü earnings satırı yüzünden)
    d = datetime.strptime((date_str or "").strip()[:10], "%Y-%m-%d").replace(tzinfo=ET) \
        + timedelta(days=day_offset)
    return int(d.replace(hour=hh, minute=mm).timestamp())


def stages(ev: dict) -> list[tuple[str, int]]:
    """Bir event için (aşama, due_ts) listesi. Aşamalar: pre, t1."""
    if ev.get("exact_ts"):
        t1 = ("t1", int(ev["exact_ts"]) - 3600)
        # bmo (sabah) exact'te insider'ın asıl giriş gecesi (önceki akşam) pre
        # taraması eskiden kayboluyordu — daha iyi veri, daha az gözetim. Koru.
        if (ev.get("hour_hint") or "") == "bmo" and valid_date_et(ev.get("date_et")):
            return [("pre", _et_ts(ev["date_et"], 20, 0, day_offset=-1)), t1]
        return [t1]
    d = ev.get("date_et")
    if not valid_date_et(d):
        return []  # geçersiz tarih → bu event zamanlanamaz, sessizce atla
    hint = ev.get("hour_hint") or "unknown"
    if hint == "amc":
        # rapor ~16:05 ET → kapanışa 1 saat kala
        return [("t1", _et_ts(d, 15, 0))]
    if hint == "bmo":
        # rapor ~07:00 ET → önceki akşam + sabah erken
        return [("pre", _et_ts(d, 20, 0, day_offset=-1)), ("t1", _et_ts(d, 6, 0))]
    # bilinmiyor → sabah + kapanış öncesi iki pencere
    return [("pre", _et_ts(d, 8, 30)), ("t1", _et_ts(d, 15, 0))]


def event_ts_estimate(ev: dict) -> int:
    if ev.get("exact_ts"):
        return int(ev["exact_ts"])
    d = ev.get("date_et")
    if not valid_date_et(d):
        return 0  # geçersiz → 0 (annotate bunu 'çok eski/geçmiş' sayar, alarm üretmez)
    hint = ev.get("hour_hint") or "unknown"
    if hint == "bmo":
        return _et_ts(d, 7, 0)
    return _et_ts(d, 16, 5)  # amc / unknown


def countdown_str(mins: int) -> str:
    if mins < 0:
        return "AÇIKLANDI"
    if mins < 60:
        return f"{mins}dk"
    if mins < 24 * 60:
        return f"{mins // 60}sa {mins % 60}dk"
    return f"{mins // (24 * 60)} gün"


# Saati doğrulanmış sayılan kaynaklar — bunlar demiyorsa "kesin" deme
STRONG_SOURCES = ("manual", "tradingview", "yahoo")


def annotate(events: list[dict]) -> list[dict]:
    """Her event'e rapor zamanı, geçti mi, geri sayım, ☀️/🌙 ve belirsizlik uyarısı ekle."""
    ts = now()
    for e in events:
        rts = event_ts_estimate(e)
        hint = e.get("hour_hint") or "unknown"
        src = (e.get("source") or "").lower()
        bad_date = rts == 0  # geçersiz tarih
        e["report_ts"] = rts
        e["bad_date"] = bad_date
        e["passed"] = (not bad_date) and ts > rts
        e["mins_left"] = 0 if bad_date else int((rts - ts) / 60)
        e["countdown"] = "?" if bad_date else countdown_str(e["mins_left"])
        e["exact"] = bool(e.get("exact_ts"))
        e["icon"] = {"bmo": "☀️", "amc": "🌙"}.get(hint, "❓")
        e["when_txt"] = {"bmo": "sabah, açılış öncesi",
                         "amc": "akşam, kapanış sonrası"}.get(hint, "saati belirsiz")
        e["tsi"] = (e.get("date_et") or "?") if bad_date \
            else datetime.fromtimestamp(rts, TR).strftime("%d.%m %H:%M")

        # SHAZ dersi: zayıf kaynak "akşam" derken hisse sabah açıklamış olabilir.
        # Saat güçlü bir kaynakla doğrulanmadıysa ve sabah penceresi geçtiyse uyar.
        strong = e["exact"] or any(s in src for s in STRONG_SOURCES)
        e["uncertain"] = not strong
        try:
            earliest = _et_ts(e["date_et"], 7, 0)
        except (ValueError, TypeError, KeyError):
            earliest = rts
        e["maybe_passed"] = bool(not bad_date and not e["passed"]
                                 and e["uncertain"] and ts > earliest)
        e["alt_tsi"] = "" if bad_date else datetime.fromtimestamp(earliest, TR).strftime("%H:%M")
    return events


async def upcoming_events(days: int = 14) -> list[dict]:
    frm = datetime.now(ET).strftime("%Y-%m-%d")
    to = (datetime.now(ET) + timedelta(days=days)).strftime("%Y-%m-%d")
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM earnings_events WHERE date_et>=? AND date_et<=? ORDER BY date_et",
            (frm, to),
        )
        return [dict(r) for r in await cur.fetchall()]
