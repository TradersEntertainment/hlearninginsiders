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
  entity TEXT,                     -- NULL=insan | mm | vault | manual (elle elendi)
  account_value REAL,              -- HL perp teminatı (tüm dex) — net worth DEĞİL
  account_ts INTEGER               -- ölçüm anı (bayatlık göstergesi)
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
-- Son bilinen pozisyon — HER BOYUTTA. hl_positions bir REKOR ARŞİVİ'dir ve
-- kademe altını ($1M hisse / $20M kripto / $50M BTC-ETH) hiç yazmaz; o yüzden
-- "ne oldu" raporunda adreslerin çoğu "bilinmiyor" çıkıyordu. Bu tablo o boşluğu
-- doldurur: süpürme zaten her adresin TÜM pozisyonlarını ayrıştırıyor, biz
-- eşik altını çöpe atıyorduk. İki tablo AYRI kalmalı — birleştirmek /devler'in
-- "devler" tanımını bozar.
-- Zamanla değil ADRES EVRENİ kadar büyür: (coin,adres) anahtarı üzerine yazılır.
CREATE TABLE IF NOT EXISTS addr_positions(
  coin TEXT, address TEXT, dex TEXT,
  side TEXT, szi REAL, entry_px REAL, leverage REAL,
  liq_px REAL, upnl REAL, notional REAL,
  ts INTEGER,                           -- son ölçüm (bayatlık göstergesi)
  closed_ts INTEGER,                    -- artık tutmuyorsa damga; SİLİNMEZ
  PRIMARY KEY(coin, address)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_addrpos_addr ON addr_positions(address);
CREATE INDEX IF NOT EXISTS idx_addrpos_ts ON addr_positions(ts);
-- Liq attack radarı: hafta sonu yakın liq kümesini itmenin maliyeti (defter)
-- karşısında patlayacak $. Adaylar tur damgasıyla saklanır (karne için), gerçek
-- saldırılar sonradan tespit edilip 'önceden işaretlemiş miydik' diye ölçülür.
CREATE TABLE IF NOT EXISTS liq_attack_candidates(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  coin TEXT, direction TEXT,       -- down = long'ları patlat | up = short'ları
  ts INTEGER, weekend_ts INTEGER,  -- tur anı, hafta sonu çıpası
  mark REAL, dist_pct REAL, liq_usd REAL, cost_usd REAL, score REAL,
  book_thin INTEGER,               -- 1 = görünen defter hedefe varmadan bitiyor
  n_pos INTEGER, target_px REAL, dev_close REAL,
  hot INTEGER DEFAULT 0            -- 1 = skor eşiği geçti (aday)
);
CREATE INDEX IF NOT EXISTS idx_liqatk_ts ON liq_attack_candidates(ts DESC);
CREATE INDEX IF NOT EXISTS idx_liqatk_key ON liq_attack_candidates(coin, direction, weekend_ts);
CREATE TABLE IF NOT EXISTS liq_attacks(
  coin TEXT, ts_start INTEGER, ts_peak INTEGER, ts_end INTEGER,
  direction TEXT, ref_px REAL, extreme_px REAL, move_pct REAL,
  liq_usd REAL, n_liq INTEGER,
  predicted_score REAL,            -- olaydan ÖNCE o hafta sonu verdiğimiz en yüksek skor (NULL = hiç)
  weekend_ts INTEGER, found_ts INTEGER,
  PRIMARY KEY(coin, ts_start)
);
-- ---- AI analist ----
-- LLM ÖNERİR, Python KARAR VERİR. Model hipotez üretir; vadesi gelince aynı
-- veriden ölçülüp tuttu/tutmadı diye damgalanır. Böylece modelin kendi sicili
-- oluşur ve uyduruyorsa istatistik onu ele verir (balina sicilinin aynısı).
CREATE TABLE IF NOT EXISTS ai_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER, model TEXT,
  ok INTEGER,                      -- 1 = tur başarılı
  tokens_in INTEGER, tokens_out INTEGER,
  n_obs INTEGER, n_hyp INTEGER,
  err TEXT                         -- hata metni (panelde görünür, log'a gömülmez)
);
CREATE INDEX IF NOT EXISTS idx_airuns_ts ON ai_runs(ts DESC);
CREATE TABLE IF NOT EXISTS ai_observations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, ts INTEGER,
  subject_kind TEXT, subject TEXT, -- coin | address | global
  text TEXT                        -- ölçülemeyen (sicile girmeyen) serbest gözlem
);
CREATE INDEX IF NOT EXISTS idx_aiobs_ts ON ai_observations(ts DESC);
CREATE TABLE IF NOT EXISTS ai_hypotheses(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, created_ts INTEGER,
  claim TEXT, rationale TEXT, confidence REAL,
  subject_kind TEXT,                  -- coin | position | global
  subject TEXT,                       -- coin adı, ya da position ise adres
  subject_coin TEXT,                  -- position hipotezlerinde coin
  metric TEXT, op TEXT, value REAL,   -- KAPALI enum; Python bunu ölçebilmeli
  horizon_h INTEGER,
  baseline REAL, baseline_ts INTEGER, -- kayıt anındaki ölçüm (vadede belirsizlik olmasın)
  resolve_ts INTEGER,
  status TEXT DEFAULT 'open',         -- open | hit | miss | unresolvable
  measured REAL, resolved_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_aihyp_due ON ai_hypotheses(status, resolve_ts);
CREATE INDEX IF NOT EXISTS idx_aihyp_new ON ai_hypotheses(created_ts DESC);

-- Uyarı/hata halkası: bir şey patladığında metni yalnız Railway log'undaydı ve
-- pratikte kimse oraya bakmıyordu. /tani dökümünün en değerli parçası bu.
-- KALICI olması şart: asıl merak edilen an, yeniden başlatmadan hemen öncesi.
CREATE TABLE IF NOT EXISTS log_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER, logger TEXT, level TEXT, msg TEXT,
  n INTEGER DEFAULT 1                 -- aynı mesaj tekrarladıysa sayaç (satır değil)
);
-- Kripto hacim patlaması: bir coin son 24 saatin en yüksek 5 dakikalık
-- hacmine ulaştığında bir satır. UNIQUE(coin,bucket_ts) tekilliği DOĞAL olarak
-- sağlıyor — aynı kova iki kez taransa da tek satır kalır, ayrı dedupe durumu
-- tutmaya gerek yok.
CREATE TABLE IF NOT EXISTS vol_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  coin TEXT, ts INTEGER, bucket_ts INTEGER,
  vol REAL,            -- ham mum hacmi (rekor karşılaştırması bunun üzerinden)
  notional REAL,       -- ≈ vol × kapanış (yalnız gösterim + alt sınır)
  prev_max REAL, ratio REAL,
  px REAL, chg_pct REAL,
  alerted INTEGER DEFAULT 0,
  market TEXT,         -- 'crypto' | 'equity' (eski satırlar NULL = crypto)
  UNIQUE(coin, bucket_ts)
);
-- Mum arşivi — örüntü bulucunun ham verisi. hourstats zaten 1h mumları
-- ÇEKİYORDU ama saat-bazlı özete indirip atıyordu; şekil eşleştirmesi ham
-- seriyi istiyor. Yalnız kapanış + hacim: eşleştirmenin ve sparkline'ın
-- ihtiyacı bu (OHLC zaten pricechart kv önbelleğinde, coin sayfası için).
-- WITHOUT ROWID: bileşik anahtarlı bu tabloda gözle görülür yer kazandırır.
CREATE TABLE IF NOT EXISTS bars(
  coin TEXT, tf TEXT, ts INTEGER,     -- tf: '1h' | '15m'
  c REAL, v REAL,
  PRIMARY KEY(coin, tf, ts)
) WITHOUT ROWID;

-- Örüntü sinyali VE sonucu aynı satırda (ai_hypotheses deseni): tahmini
-- kaydetmeden "bu araç tutuyor mu" sorusunun cevabı olmaz.
CREATE TABLE IF NOT EXISTS pattern_signals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER, coin TEXT, tf TEXT, win INTEGER, horizon INTEGER,
  n_match INTEGER, p_up REAL, base_up REAL, edge REAL, z REAL,
  med_move REAL, q25 REAL, q75 REAL,
  px REAL, resolve_ts INTEGER,
  status TEXT DEFAULT 'open',         -- open | hit | miss | unresolvable
  measured REAL, resolved_ts INTEGER, alerted INTEGER DEFAULT 0,
  UNIQUE(coin, tf, horizon, ts)
);
CREATE INDEX IF NOT EXISTS idx_psig_due ON pattern_signals(status, resolve_ts);
CREATE INDEX IF NOT EXISTS idx_psig_new ON pattern_signals(ts DESC);
-- TWAP / düzenli birikim turları. Tespit KENDİ fill kayıtlarımızdan yapılır
-- (düzenli aralık + benzer dilim boyutu); HL'nin yerel TWAP emri işaretine
-- bakılmıyor çünkü kendi botuyla dilimleyen biri de aynı şeyi yapıyor ve
-- aynı derecede ilginç.
CREATE TABLE IF NOT EXISTS twap_runs(
  coin TEXT, address TEXT, side TEXT,
  first_ts INTEGER, last_ts INTEGER,
  n_slices INTEGER, total REAL, avg_slice REAL, avg_gap REAL,
  cv_gap REAL, cv_size REAL,
  taker_pct REAL, ts INTEGER,
  PRIMARY KEY(coin, address, side, first_ts)
);
CREATE INDEX IF NOT EXISTS idx_twap_total ON twap_runs(total DESC);
CREATE INDEX IF NOT EXISTS idx_twap_last ON twap_runs(last_ts DESC);
CREATE INDEX IF NOT EXISTS idx_volev_ts ON vol_events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_logev_ts ON log_events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_logev_dedupe ON log_events(logger, level, ts DESC);
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
    # Defteri GERÇEKTEN çektiğimiz an. "bakmadık" ile "baktık ama bu coinde
    # pozisyonu yok"u ayıran tek güvenilir işaret; account_ts proxy olurdu ama
    # o yalnız marginSummary gelirse yazılıyor.
    "ALTER TABLE addresses ADD COLUMN probed_ts INTEGER",
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
    # market: hacim rekoru kripto taramasından mı hisse taramasından mı geldi.
    # Eski satırların hepsi kripto turundan geldiği için NULL = 'crypto' okunur
    # (MIGRATIONS yalnız ALTER — geriye dönük UPDATE buraya konmaz).
    "ALTER TABLE vol_events ADD COLUMN market TEXT",
    # account_value: adresin TÜM dex'lerdeki perp teminatı toplamı. "Net worth"
    # DEĞİL — spot, vault ve zincir dışı varlıklar dahil değil; başlıkta da öyle
    # yazıyor. Veri zaten çektiğimiz clearinghouseState/leaderboard yanıtlarının
    # içindeydi, okumadan atıyorduk: ek API maliyeti yok.
    "ALTER TABLE addresses ADD COLUMN account_value REAL",
    # account_ts OPSİYONEL DEĞİL: derin keşif bir adrese 75-125 dakikada bir
    # uğruyor, yani rakam 2 saate kadar bayat olabilir. Yaşını göstermeden
    # bakiye yazmak sessizce yanlış bilgi vermektir.
    "ALTER TABLE addresses ADD COLUMN account_ts INTEGER",
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
