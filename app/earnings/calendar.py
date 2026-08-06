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
            eps = None
            try:
                v = row.get("EPS Estimate")
                if v is not None and v == v:  # NaN kontrolü
                    eps = float(v)
            except Exception:
                pass
            out[sym] = {"date_et": ts_et.strftime("%Y-%m-%d"), "hour_hint": hint,
                        "eps_est": eps, "source": "yahoo"}
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

    merged: dict[str, dict] = dict(yahoo)
    for sym, fh in finnhub.items():
        if sym in merged:
            m = merged[sym]
            if m["date_et"] == fh["date_et"]:
                if m["hour_hint"] == "unknown" and fh["hour_hint"] != "unknown":
                    m["hour_hint"] = fh["hour_hint"]
                m["source"] = "yahoo+finnhub"
            else:
                m["note"] = f"kaynak çelişkisi: finnhub {fh['date_et']} diyor"
        else:
            merged[sym] = fh

    n = 0
    async with db() as conn:
        for sym, ev in merged.items():
            if not ev.get("date_et"):
                continue
            await conn.execute(
                """INSERT INTO earnings_events(symbol,coin,date_et,hour_hint,eps_est,source,note,created_ts)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol,date_et) DO UPDATE SET
                     hour_hint=CASE WHEN earnings_events.hour_hint='unknown'
                                    THEN excluded.hour_hint ELSE earnings_events.hour_hint END,
                     eps_est=COALESCE(excluded.eps_est, earnings_events.eps_est),
                     source=excluded.source,
                     note=COALESCE(excluded.note, earnings_events.note)""",
                (sym, sym2coin[sym], ev["date_et"], ev["hour_hint"], ev.get("eps_est"),
                 ev.get("source"), ev.get("note"), now()),
            )
            n += 1
        # Tarihi kaymış eski (henüz alert edilmemiş, gelecekteki) kayıtları temizle
        today = datetime.now(ET).strftime("%Y-%m-%d")
        for sym in symbols:
            if sym in merged:
                await conn.execute(
                    """DELETE FROM earnings_events
                       WHERE symbol=? AND date_et>? AND alerted_pre=0 AND alerted_t1=0
                         AND date_et<>?""",
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


async def upcoming_events(days: int = 14) -> list[dict]:
    frm = datetime.now(ET).strftime("%Y-%m-%d")
    to = (datetime.now(ET) + timedelta(days=days)).strftime("%Y-%m-%d")
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM earnings_events WHERE date_et>=? AND date_et<=? ORDER BY date_et",
            (frm, to),
        )
        return [dict(r) for r in await cur.fetchall()]
