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
CREATE TABLE IF NOT EXISTS scans(coin TEXT PRIMARY KEY, ts INTEGER,
  n_addrs INTEGER, n_found INTEGER);   -- son tam taramada sorgulanan / pozisyonlu adres
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
-- Kripto liq yakını takibi: liq_watch'ın kripto ikizi. liq_watch'a YAZILMAZ —
-- liqwatch o tabloyu kendi adres havuzu ve ana-sohbet kademeleri (1/0.5/0.1)
-- için okuyor; buradaki kademeler kripto kanalının (2.5/1/0.5) ve kapanış
-- notu likidasyon teyidi taşıyor (fill'lerde liquidation alanı).
CREATE TABLE IF NOT EXISTS cryptoliq_watch(
  coin TEXT, address TEXT, side TEXT, notional REAL, liq_px REAL,
  entry_px REAL, leverage REAL,
  stage INTEGER DEFAULT 0,         -- 0 yok | 1 ≤dist1 | 2 ≤dist2 | 3 ≤dist3 bildirildi
  last_dist REAL, last_mark REAL,
  first_ts INTEGER, updated_ts INTEGER, probed_ts INTEGER,
  closed_ts INTEGER, closed_kind TEXT, -- liq | close | unknown
  closed_px REAL, notified_ts INTEGER, -- kapanış notu gitti mi (NULL = henüz)
  PRIMARY KEY(coin, address)
);
-- Liq simülasyonu (kâğıt üstü, gerçek emir yok). Bacak 1 = SON UYARI'da
-- balinanın tersine giriş, hedef zincir sonu (limit); bacak 2 = o seviyeden
-- ters yön ("iğneden dönüş"). Satır SİLİNMEZ: sıfırlama yeni tur (run) açar,
-- eski turlar sayfada ?run=all ile görünür.
CREATE TABLE IF NOT EXISTS sim_trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run INTEGER DEFAULT 1,
  coin TEXT, leg INTEGER,           -- 1 ön bacak | 2 ters bacak
  side TEXT,                        -- long | short (bizim yön)
  status TEXT DEFAULT 'open',       -- open | closed | skipped
  entry_px REAL, entry_ts INTEGER, entry_src TEXT,   -- ctx (ana dex fiyatı) | tp (1. bacağın hedefi)
  qty REAL, notional REAL, margin REAL, leverage REAL,
  stop_px REAL, tp_px REAL, tp_src TEXT,             -- cascade | liq | capped | pct (2. bacak)
  exit_px REAL, exit_ts INTEGER, exit_reason TEXT,   -- tp | stop | timeout | void | reset
  pnl_usd REAL, pnl_pct REAL, fee_usd REAL,          -- pnl_pct marjine göre, ücret düşülmüş
  whale_addr TEXT, whale_side TEXT, whale_notional REAL, whale_liq_px REAL,
  casc_end_px REAL, casc_total REAL, casc_note TEXT,
  liq_ts INTEGER,                   -- balina liq fiyatı kesildi / 💀 teyidi
  last_eval_ts INTEGER, hi_px REAL, lo_px REAL,      -- değerlendirme damgası, uç fiyatlar
  parent_id INTEGER,                -- 2. bacak → 1. bacağın id'si
  skip_reason TEXT, note TEXT, created_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sim_open ON sim_trades(status, coin);
CREATE INDEX IF NOT EXISTS idx_sim_run ON sim_trades(run, created_ts DESC);
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
-- Sayım evreni (census): HL'de "coindeki tüm pozisyonlar" API'si yok; kapsamayı
-- zincir geneline yaklaştırmanın tek yolu tanıdığımız HER hesabı (leaderboard ∪
-- addresses ∪ fills) ana dex + kripto dex'lerde tur tur sorgulamak. Sıra bakiye
-- büyükten küçüğe (OI'nin büyüğü önce → kapsama $ olarak hızla yükselir).
-- Worker'lar (ROLE=census-worker) satırı kiralar, sonucu /api/census/ingest ile yollar.
CREATE TABLE IF NOT EXISTS census_accounts(
  address TEXT PRIMARY KEY,
  account_value REAL,                   -- leaderboard accountValue ya da son ölçüm (ana dex)
  src TEXT,                             -- lb | addr | fills | ingest
  seen_ts INTEGER,                      -- listeye girdiği / son tazelendiği an
  scanned_ts INTEGER,                   -- son sayım (NULL = hiç)
  positions INTEGER,                    -- son sayımda bulunan açık pozisyon (ana dex)
  leased_ts INTEGER,                    -- worker kirası (NULL = kirada değil)
  lease_worker TEXT
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_census_value ON census_accounts(account_value DESC);
CREATE INDEX IF NOT EXISTS idx_census_scanned ON census_accounts(scanned_ts);
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
-- Satılabilir bot (DM): Telegram kullanıcıları, katman/kota, abonelik, ödemeler, fan-out kaydı.
-- Sahibin sohbeti/kanalları buraya girmez (env chat id'leri ayrı dünyadır).
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,                -- Telegram user id
  chat_id TEXT, username TEXT, first_name TEXT, lang TEXT DEFAULT 'tr',
  created_ts INTEGER, last_seen_ts INTEGER,
  pro_until INTEGER,                     -- Pro bitişi (NULL/geçmiş = ücretsiz)
  hl_address TEXT,                       -- USDC ödemesinin geldiği HL adresi
  blocked_ts INTEGER,                    -- 403: botu engelledi / hesap silindi
  q_day TEXT, q_used INTEGER DEFAULT 0, q_total INTEGER DEFAULT 0,
  quiet_start INTEGER, quiet_end INTEGER, note TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_pro ON users(pro_until);
CREATE INDEX IF NOT EXISTS idx_users_blocked ON users(blocked_ts);
CREATE TABLE IF NOT EXISTS user_kinds(user_id INTEGER, kind TEXT, PRIMARY KEY(user_id, kind));
CREATE TABLE IF NOT EXISTS user_coins(user_id INTEGER, coin TEXT, PRIMARY KEY(user_id, coin));
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER, method TEXT,          -- hl | stars | nowpay
  plan TEXT, months INTEGER,             -- 1m | 3m | 12m
  amount_usd REAL, amount_raw REAL, currency TEXT,
  ext_id TEXT,                           -- HL tx hash / Telegram charge id / NOWPayments id
  from_addr TEXT, status TEXT,           -- pending | paid | expired | refunded
  created_ts INTEGER, paid_ts INTEGER, raw TEXT,
  UNIQUE(method, ext_id)
);
CREATE INDEX IF NOT EXISTS idx_pay_status ON payments(status);
CREATE INDEX IF NOT EXISTS idx_pay_user ON payments(user_id);
CREATE TABLE IF NOT EXISTS fanout_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER, kind TEXT, coin TEXT, key TEXT,
  n_targets INTEGER, n_sent INTEGER, n_fail INTEGER, n_blocked INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fanout_ts ON fanout_log(ts DESC);
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
    # Liq attack Telegram kapısı: adayın ≤alert_dist içindeki liq toplamı
    # (sayfa 🔔/🔕 için; NULL = kapıdan önceki kayıt, sayfa yeniden hesaplar)
    "ALTER TABLE liq_attack_candidates ADD COLUMN near_usd REAL",
    # Kapı = ≤alert_dist bölgesinin kendi adayı (mesaj onu anlatır): hedef
    # uzaklığı, patlayacak $, oran. NULL = bölge adayından önceki kayıt.
    "ALTER TABLE liq_attack_candidates ADD COLUMN zone_dist REAL",
    "ALTER TABLE liq_attack_candidates ADD COLUMN zone_liq REAL",
    "ALTER TABLE liq_attack_candidates ADD COLUMN zone_score REAL",
    # Takip: son bildirilen liq fiyatı (%X kayınca haber), kaldıraç, komutun
    # geldiği sohbet (kanaldan /takip_N basıldıysa haber oraya gider)
    "ALTER TABLE trackers ADD COLUMN liq_px REAL",
    "ALTER TABLE trackers ADD COLUMN leverage REAL",
    "ALTER TABLE trackers ADD COLUMN chat_id TEXT",
    # Canlı TWAP radarı: hacim/hız, kaynak (live|fills), bildirim ve bitiş damgası, fiyat/adet
    "ALTER TABLE twap_runs ADD COLUMN day_volume REAL",
    "ALTER TABLE twap_runs ADD COLUMN rate_day REAL",
    "ALTER TABLE twap_runs ADD COLUMN src TEXT",
    "ALTER TABLE twap_runs ADD COLUMN alerted_ts INTEGER",
    "ALTER TABLE twap_runs ADD COLUMN ended_ts INTEGER",
    "ALTER TABLE twap_runs ADD COLUMN px_first REAL",
    "ALTER TABLE twap_runs ADD COLUMN px_last REAL",
    "ALTER TABLE twap_runs ADD COLUMN sz_total REAL",
    # HL TWAP emri (userTwapHistory): plan, dolan, kalan, başlangıç, süre, durum, sorgu damgası
    "ALTER TABLE twap_runs ADD COLUMN planned_usd REAL",
    "ALTER TABLE twap_runs ADD COLUMN planned_sz REAL",
    "ALTER TABLE twap_runs ADD COLUMN executed_usd REAL",
    "ALTER TABLE twap_runs ADD COLUMN remaining_usd REAL",
    "ALTER TABLE twap_runs ADD COLUMN order_ts INTEGER",
    "ALTER TABLE twap_runs ADD COLUMN order_min REAL",
    "ALTER TABLE twap_runs ADD COLUMN order_status TEXT",
    "ALTER TABLE twap_runs ADD COLUMN lookup_ts INTEGER",
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
    # Tarama sayımı: son tam taramada kaç adres yanıt verdi, kaçında pozisyon
    # çıktı — coin sayfasındaki kapsama satırı "312 adres → 41 poz" bunu okur.
    "ALTER TABLE scans ADD COLUMN n_addrs INTEGER",
    "ALTER TABLE scans ADD COLUMN n_found INTEGER",
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
