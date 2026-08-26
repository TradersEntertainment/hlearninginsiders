"""SQLite depolama — botun "hafızası". Railway Volume (/data) üzerinde yaşar."""
import json
import time
from contextlib import asynccontextmanager

import aiosqlite

_DB_PATH = "./data/radar.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickers(
  coin TEXT PRIMARY KEY,           -- ör. "xyz:SNDK"
  dex TEXT, symbol TEXT, name TEXT,
  max_leverage INTEGER, listed_at INTEGER
);
CREATE TABLE IF NOT EXISTS earnings_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT, coin TEXT,
  date_et TEXT,                    -- YYYY-MM-DD (New York günü)
  hour_hint TEXT,                  -- bmo / amc / unknown
  exact_ts INTEGER,                -- biliniyorsa epoch sn
  eps_est REAL, source TEXT, note TEXT,
  alerted_pre INTEGER DEFAULT 0,   -- erken pencere (bmo akşam / unknown sabah)
  alerted_t1 INTEGER DEFAULT 0,    -- ana T-1h raporu
  evaluated INTEGER DEFAULT 0,
  move_pct REAL,                   -- earnings sonrası fiyat hareketi (%)
  result_note TEXT,                -- arşiv notu: en büyük poz kimdi, haklı mıydı
  created_ts INTEGER,
  UNIQUE(symbol, date_et)
);
CREATE TABLE IF NOT EXISTS fills(
  coin TEXT, tid TEXT, address TEXT,
  side TEXT,                       -- buy / sell (adres perspektifi)
  px REAL, sz REAL, notional REAL, ts INTEGER,
  taker INTEGER,                   -- 1 = agresördü (fiyatı süpürdü) | 0 = pasif | NULL bilinmiyor
  PRIMARY KEY(coin, tid, address)
);
CREATE INDEX IF NOT EXISTS idx_fills_coin_ts ON fills(coin, ts);
CREATE INDEX IF NOT EXISTS idx_fills_addr ON fills(address);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
CREATE TABLE IF NOT EXISTS addresses(
  address TEXT PRIMARY KEY,
  first_seen INTEGER, first_deposit_ts INTEGER, last_deposit_ts INTEGER,
  label TEXT, hits INTEGER DEFAULT 0, misses INTEGER DEFAULT 0,
  watchlist INTEGER DEFAULT 0, notes TEXT,
  entity TEXT                      -- NULL=insan | mm | vault | manual (elle elendi)
);
CREATE TABLE IF NOT EXISTS positions_current(
  coin TEXT, address TEXT, ts INTEGER,
  side TEXT, szi REAL, entry_px REAL, leverage REAL,
  liq_px REAL, upnl REAL, notional REAL,
  opened_ts INTEGER, score INTEGER, score_reasons TEXT,
  last_add_ts INTEGER, last_trim_ts INTEGER,
  first_seen_ts INTEGER,           -- pozisyonu ilk görüşümüz ("en az bu kadar eski")
  PRIMARY KEY(coin, address)
);
CREATE TABLE IF NOT EXISTS position_snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER, phase TEXT,    -- pre / T-1h / T+24h / ondemand
  coin TEXT, address TEXT, ts INTEGER,
  side TEXT, szi REAL, entry_px REAL, leverage REAL,
  liq_px REAL, upnl REAL, notional REAL,
  score INTEGER, score_reasons TEXT,
  last_add_ts INTEGER, last_trim_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_snap_event ON position_snapshots(event_id, phase);
CREATE TABLE IF NOT EXISTS asset_metrics(
  coin TEXT, ts INTEGER,
  mark_px REAL, oi REAL, funding REAL, day_volume REAL,
  PRIMARY KEY(coin, ts)
);
CREATE INDEX IF NOT EXISTS idx_metrics_coin_ts ON asset_metrics(coin, ts);
CREATE TABLE IF NOT EXISTS wallet_links(
  a TEXT, b TEXT,                  -- normalize: a < b
  kind TEXT, last_ts INTEGER,
  PRIMARY KEY(a, b)
);
CREATE INDEX IF NOT EXISTS idx_links_a ON wallet_links(a);
CREATE INDEX IF NOT EXISTS idx_links_b ON wallet_links(b);
CREATE TABLE IF NOT EXISTS alerts_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT, key TEXT, ts INTEGER, payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_kind_key ON alerts_log(kind, key, ts);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS scans(coin TEXT PRIMARY KEY, ts INTEGER);
CREATE TABLE IF NOT EXISTS address_wins(
  address TEXT, coin TEXT, event_id INTEGER, notional REAL, ts INTEGER,
  PRIMARY KEY(address, event_id)
);
CREATE INDEX IF NOT EXISTS idx_wins_addr ON address_wins(address);
CREATE TABLE IF NOT EXISTS trackers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  address TEXT, coin TEXT, symbol TEXT, side TEXT,
  base_szi REAL,                   -- takip başındaki boyut (adet — fiyattan etkilenmez)
  last_szi REAL,                   -- son bildirimdeki boyut
  base_notional REAL,              -- takip başındaki $ (gösterim için)
  created_ts INTEGER, expires_ts INTEGER,
  active INTEGER DEFAULT 1, last_check_ts INTEGER, end_note TEXT,
  entry_px REAL                    -- takip başındaki giriş fiyatı (kapanış P&L tahmini)
);
CREATE TABLE IF NOT EXISTS track_offers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  address TEXT, coin TEXT, symbol TEXT, side TEXT, notional REAL,
  created_ts INTEGER, used INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS book_walls(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  coin TEXT, side TEXT,            -- ask = satış duvarı (fiyatın üstünde) / bid = alış (altında)
  px_lo REAL, px_hi REAL, sz REAL, notional REAL,
  dist_pct REAL, mark_px REAL,
  address TEXT,                    -- eşleşen bilinen balina (NULL = bilinmiyor)
  first_ts INTEGER, last_ts INTEGER,
  peak_notional REAL, alerted INTEGER DEFAULT 0, active INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_walls_coin ON book_walls(coin, side, active);
CREATE TABLE IF NOT EXISTS liq_watch(
  address TEXT, coin TEXT,
  side TEXT, notional REAL, liq_px REAL,
  stage INTEGER DEFAULT 0,         -- 0 yok | 1 %1 | 2 %0.5 | 3 %0.1 bildirimi gitti
  last_dist REAL, updated_ts INTEGER,
  PRIMARY KEY(address, coin)
);
-- TÜM Hyperliquid'in büyük pozisyonları (ana dex + HIP-3 hepsi).
-- positions_current'a KARIŞTIRILMAZ: orası hisse skorlama/earnings hattının
-- sahibi ve neredeyse her sorgusu tickers ile JOIN'li — BTC satırı oraya
-- girerse sessizce davranış değişir. Süpürücü zaten tüm dex'leri sorguluyor,
-- yani bu veri ek istek OLMADAN geliyordu ve atılıyordu.
CREATE TABLE IF NOT EXISTS hl_positions(
  coin TEXT, address TEXT, dex TEXT,
  side TEXT, szi REAL, entry_px REAL, leverage REAL,
  liq_px REAL, upnl REAL, notional REAL,
  ts INTEGER, first_seen_ts INTEGER,
  peak_notional REAL, peak_ts INTEGER,  -- gördüğümüz en büyük hâli (rekor arşivi)
  closed_ts INTEGER,                    -- kapandıysa damga; satır SİLİNMEZ
  PRIMARY KEY(coin, address)
);
CREATE INDEX IF NOT EXISTS idx_hlpos_peak ON hl_positions(peak_notional DESC);
CREATE INDEX IF NOT EXISTS idx_hlpos_open ON hl_positions(closed_ts, notional DESC);
"""


def set_db_path(path: str) -> None:
    global _DB_PATH
    _DB_PATH = path


@asynccontextmanager
async def db():
    conn = await aiosqlite.connect(_DB_PATH)
    try:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = aiosqlite.Row
        yield conn
        await conn.commit()
    finally:
        await conn.close()


# Var olan (canlı) DB'lere kolon ekleyen migration'lar — "zaten var" hatası yutulur
MIGRATIONS = [
    "ALTER TABLE positions_current ADD COLUMN last_add_ts INTEGER",
    "ALTER TABLE positions_current ADD COLUMN last_trim_ts INTEGER",
    "ALTER TABLE position_snapshots ADD COLUMN last_add_ts INTEGER",
    "ALTER TABLE position_snapshots ADD COLUMN last_trim_ts INTEGER",
    "ALTER TABLE addresses ADD COLUMN last_deposit_ts INTEGER",
    "ALTER TABLE addresses ADD COLUMN entity TEXT",
    "ALTER TABLE earnings_events ADD COLUMN move_pct REAL",
    "ALTER TABLE earnings_events ADD COLUMN result_note TEXT",
    "ALTER TABLE earnings_events ADD COLUMN offer_sent INTEGER DEFAULT 0",
    # taker: bu adres bu işlemde AGRESÖR müydü (1) yoksa pasif emirle mi doldu (0)?
    # Fiyatı süpüren taraf bilgi taşır — insider sinyalinde maker değil taker önemlidir.
    "ALTER TABLE fills ADD COLUMN taker INTEGER",
    # first_seen_ts: bu pozisyonu İLK gördüğümüz an. opened_ts bilinmese bile
    # "en az şu tarihten beri açık" alt sınırını verir (fill emekliliğinden bağımsız).
    "ALTER TABLE positions_current ADD COLUMN first_seen_ts INTEGER",
    # entry_px: takip başlarkenki giriş fiyatı — kapanışta tahmini kâr/zarar için
    "ALTER TABLE trackers ADD COLUMN entry_px REAL",
]


async def init_db(path: str) -> None:
    set_db_path(path)
    conn = await aiosqlite.connect(path)
    try:
        await conn.executescript(SCHEMA)
        for mig in MIGRATIONS:
            try:
                await conn.execute(mig)
            except Exception:
                pass  # kolon zaten var
        await conn.commit()
    finally:
        await conn.close()


def now() -> int:
    return int(time.time())


# ---------- kv ----------

async def kv_get(key: str):
    async with db() as conn:
        cur = await conn.execute("SELECT v FROM kv WHERE k=?", (key,))
        row = await cur.fetchone()
        return json.loads(row["v"]) if row else None


async def kv_set(key: str, value) -> None:
    async with db() as conn:
        await conn.execute(
            "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, json.dumps(value)),
        )


# ---------- alerts / cooldown ----------

async def alert_recent(kind: str, key: str, within_sec: int) -> bool:
    async with db() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM alerts_log WHERE kind=? AND key=? AND ts>? LIMIT 1",
            (kind, key, now() - within_sec),
        )
        return await cur.fetchone() is not None


async def alert_log(kind: str, key: str, payload: str = "") -> None:
    async with db() as conn:
        await conn.execute(
            "INSERT INTO alerts_log(kind,key,ts,payload) VALUES(?,?,?,?)",
            (kind, key, now(), payload[:2000]),
        )
