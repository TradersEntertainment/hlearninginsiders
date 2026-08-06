"""Earnings takvimi — birincil kaynak Yahoo (yfinance, keysiz),
opsiyonel iyileştirici Finnhub (FINNHUB_API_KEY verilirse).
"""
import asyncio
import logging
import time as _time
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import aiohttp

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
TV_HEADERS = {"Content-Type": "application/json",
              "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                             " (KHTML, like Gecko) Chrome/124.0 Safari/537.36")}
TV_TIME_ENUM = {1: "bmo", 2: "amc"}

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
            if r.status != 200:
                log.debug("tradingview HTTP %s", r.status)
                return out
            data = await r.json(content_type=None)
    except Exception as e:
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
    symbols = sorted(sym2coin)
    if not symbols:
        log.info("evren boş, takvim atlandı")
        return 0

    smap = dict(DEFAULT_SYMBOL_MAP)
    smap.update(parse_symbol_map(getattr(cfg, "yahoo_symbol_map", "")))
    yahoo = await asyncio.to_thread(_fetch_yahoo_sync, symbols,
                                    cfg.calendar_horizon_days, smap)
    finnhub = {}
    if cfg.finnhub_api_key:
        fh_all = await _fetch_finnhub(session, cfg.finnhub_api_key, cfg.calendar_horizon_days)
        finnhub = {s: v for s, v in fh_all.items() if s in sym2coin}
    nasdaq_all = await _fetch_nasdaq(session, cfg.calendar_horizon_days)
    nasdaq = {s: v for s, v in nasdaq_all.items() if s in sym2coin}
    tv_all = await _fetch_tradingview(session, symbols, cfg.calendar_horizon_days)
    tradingview = {s: v for s, v in tv_all.items() if s in sym2coin}

    # Dört kaynağı güven sırasına göre birleştir. Tarih çelişkisinde GÜVENİLİR
    # kaynak kazanır (BIRD vakası: zayıf kaynak "bugün" derken TradingView doğru günü verir).
    merged: dict[str, dict] = {}
    for src_name, src in (("finnhub", finnhub), ("nasdaq", nasdaq),
                          ("yahoo", yahoo), ("tradingview", tradingview)):
        rank = SOURCE_RANK.get(src_name, 0)
        for sym, ev in src.items():
            cur = merged.get(sym)
            if cur is None:
                merged[sym] = {**ev, "_rank": rank, "source": src_name}
                continue
            if cur["date_et"] == ev["date_et"]:
                if cur.get("hour_hint") in (None, "", "unknown") and \
                        ev.get("hour_hint") not in (None, "", "unknown"):
                    cur["hour_hint"] = ev["hour_hint"]
                if not cur.get("exact_ts") and ev.get("exact_ts"):
                    cur["exact_ts"] = ev["exact_ts"]
                if cur.get("eps_est") is None:
                    cur["eps_est"] = ev.get("eps_est")
                if src_name not in (cur.get("source") or ""):
                    cur["source"] = f"{cur.get('source')}+{src_name}"
                cur["_rank"] = max(cur.get("_rank", 0), rank)
            elif rank > cur.get("_rank", 0):
                merged[sym] = {**ev, "_rank": rank, "source": src_name,
                               "note": f"⚠️ tarih düzeltildi: {cur.get('source')}"
                                       f" {cur['date_et']} diyordu → {src_name} {ev['date_et']}"}
            else:
                cur["note"] = f"⚠️ kaynak çelişkisi: {src_name} {ev['date_et']} diyor"

    # Saati hâlâ bilinmeyenleri işaretle — "bugün" diye gösterip sabah geçmiş olmasın
    for sym, m in merged.items():
        if m.get("hour_hint") in (None, "", "unknown") and not m.get("exact_ts"):
            m["note"] = (m.get("note") or "") + " ⚠️ saat belirsiz — sabah da olabilir!"

    stats = {"yahoo": len(yahoo), "finnhub": len(finnhub), "nasdaq": len(nasdaq),
             "tradingview": len(tradingview), "merged": len(merged)}
    if not yahoo:
        log.warning("Yahoo takvimi 0 sonuç döndü (engel olabilir) — diğer kaynaklar taşıyor")
    log.info("takvim kaynakları: yahoo=%d tv=%d finnhub=%d nasdaq=%d → birleşik=%d",
             stats["yahoo"], stats["tradingview"], stats["finnhub"], stats["nasdaq"],
             stats["merged"])
    from ..db import kv_set
    await kv_set("calendar_stats", stats)

    n = 0
    async with db() as conn:
        for sym, ev in merged.items():
            if not ev.get("date_et"):
                continue
            await conn.execute(
                """INSERT INTO earnings_events(symbol,coin,date_et,hour_hint,exact_ts,eps_est,source,note,created_ts)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol,date_et) DO UPDATE SET
                     hour_hint=CASE WHEN earnings_events.hour_hint='unknown'
                                    THEN excluded.hour_hint ELSE earnings_events.hour_hint END,
                     exact_ts=COALESCE(excluded.exact_ts, earnings_events.exact_ts),
                     eps_est=COALESCE(excluded.eps_est, earnings_events.eps_est),
                     source=excluded.source,
                     note=COALESCE(excluded.note, earnings_events.note)""",
                (sym, sym2coin[sym], ev["date_et"], ev["hour_hint"], ev.get("exact_ts"),
                 ev.get("eps_est"), ev.get("source"), ev.get("note"), now()),
            )
            n += 1
        # Tarihi düzeltilen kayıtları temizle — bugünkü yanlış kayıt da silinmeli
        # (BIRD vakası: "bugün" diye duran hatalı satır listede kalıyordu)
        today = datetime.now(ET).strftime("%Y-%m-%d")
        for sym in symbols:
            if sym in merged:
                await conn.execute(
                    """DELETE FROM earnings_events
                       WHERE symbol=? AND date_et>=? AND alerted_pre=0 AND alerted_t1=0
                         AND evaluated=0 AND date_et<>?""",
                    (sym, today, merged[sym]["date_et"]),
                )
    log.info("takvim yenilendi: %d HL-eşleşen earnings", n)
    return n


# ---------------- Zamanlama ----------------

def _et_ts(date_str: str, hh: int, mm: int, day_offset: int = 0) -> int:
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=ET) + timedelta(days=day_offset)
    return int(d.replace(hour=hh, minute=mm).timestamp())


def stages(ev: dict) -> list[tuple[str, int]]:
    """Bir event için (aşama, due_ts) listesi. Aşamalar: pre, t1."""
    if ev.get("exact_ts"):
        return [("t1", int(ev["exact_ts"]) - 3600)]
    hint = ev.get("hour_hint") or "unknown"
    d = ev["date_et"]
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
    hint = ev.get("hour_hint") or "unknown"
    if hint == "bmo":
        return _et_ts(ev["date_et"], 7, 0)
    return _et_ts(ev["date_et"], 16, 5)  # amc / unknown


def countdown_str(mins: int) -> str:
    if mins < 0:
        return "AÇIKLANDI"
    if mins < 60:
        return f"{mins}dk"
    if mins < 24 * 60:
        return f"{mins // 60}sa {mins % 60}dk"
    return f"{mins // (24 * 60)} gün"


def annotate(events: list[dict]) -> list[dict]:
    """Her event'e rapor zamanı, geçti mi, geri sayım ve ☀️/🌙 işareti ekle."""
    ts = now()
    for e in events:
        rts = event_ts_estimate(e)
        hint = e.get("hour_hint") or "unknown"
        e["report_ts"] = rts
        e["passed"] = ts > rts
        e["mins_left"] = int((rts - ts) / 60)
        e["countdown"] = countdown_str(e["mins_left"])
        e["exact"] = bool(e.get("exact_ts"))
        e["icon"] = {"bmo": "☀️", "amc": "🌙"}.get(hint, "❓")
        e["when_txt"] = {"bmo": "sabah, açılış öncesi",
                         "amc": "akşam, kapanış sonrası"}.get(hint, "saati belirsiz")
        e["tsi"] = datetime.fromtimestamp(rts, TR).strftime("%d.%m %H:%M")
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
