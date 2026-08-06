# HL Insider Radar — Detaylı Plan

> Hyperliquid HIP-3 hisse perp'lerinde (SNDK, NVDA, TSLA…) **earnings öncesi insider şüpheli balina pozisyonlarını** yakalayan bot + dashboard.
> Örnek senaryo: SNDK earnings'inden saatler önce dev bir short açan balina → biz bunu earnings'e 1 saat kala Telegram'da görüyoruz; adres daha önce de earnings bilmişse "insider şüphelisi" olarak işaretleniyor.

---

## 1. Konsept ve Akış

```
Earnings Calendar ──► HL hisse evreniyle kesiştir ──► T-1h tetikleyici
                                                          │
7/24 WS trade dinleyici ──► adres havuzu (kim bu coini    ▼
     (users alanı)          trade etti?)          Balina pozisyon taraması
                                                          │
Leaderboard + Watchlist ──► aday adresler ────────────────┤
                                                          ▼
                                              İnsider skorlama + rapor
                                                          │
                                        ┌─────────────────┼──────────────┐
                                        ▼                 ▼              ▼
                                   Telegram alert    Dashboard      Hafıza (DB)
                                                                        │
                                              Earnings sonrası değerlendirme
                                              (kim doğru bildi → watchlist)
```

**Kilit içgörü:** Hyperliquid'de her şey public. WebSocket trade akışındaki her mesajda `users: [buyer, seller]` alanı var — yani **her trade'in iki tarafının cüzdan adresini görüyoruz**. Bir coini trade eden adresleri sürekli biriktirip, earnings saati geldiğinde bu adreslerin pozisyonlarını (`clearinghouseState`) sorgulayarak o coindeki en büyük pozları çıkarıyoruz.

---

## 2. Veri Kaynakları

### 2.1 Hyperliquid API (ücretsiz, key gerektirmez)

**REST** — `POST https://api.hyperliquid.xyz/info`:

| İstek | Ne verir | Kullanım |
|---|---|---|
| `{"type":"perpDexs"}` | Tüm HIP-3 builder dex'leri | Hisse perp dex'lerini otomatik keşif (bugün `xyz` = TradeXYZ; yenisi çıkarsa otomatik yakalarız) |
| `{"type":"meta","dex":"xyz"}` | Dex'teki coin listesi | HL hisse evreni: `xyz:SNDK`, `xyz:NVDA`… (HIP-3 coin adları `dex:TICKER` formatında) |
| `{"type":"metaAndAssetCtxs","dex":"xyz"}` | Mark price, funding, **open interest**, günlük hacim | OI/funding/hacim anomali tespiti + rapor başlığı |
| `{"type":"clearinghouseState","user":"0x…","dex":"xyz"}` | Adresin pozisyonları: size, entry, leverage, **likidasyon fiyatı**, uPnL, margin | Balina taraması. `"dex":"ALL_DEXES"` ile tek istekte tüm dex'ler |
| `{"type":"userFillsByTime","user":"0x…"}` | Adresin fill geçmişi | Pozisyon **ne zaman** açıldı (zamanlama skoru) |
| `{"type":"userNonFundingLedgerUpdates","user":"0x…"}` | Depozit/transfer geçmişi | **Taze cüzdan** tespiti (ilk depozit ne zaman, nereden) |
| `{"type":"recentTrades","coin":"xyz:SNDK"}` | Son trade'ler | WS kopması sonrası boşluk doldurma |

**WebSocket** — `wss://api.hyperliquid.xyz/ws`:

```json
{"method":"subscribe","subscription":{"type":"trades","coin":"xyz:SNDK"}}
```
Her trade mesajı: `{coin, side, px, sz, time, tid, users:[buyer, seller]}` → **adres hasadının kalbi**.
`activeAssetCtx` aboneliği ile OI/funding'i de canlı izleyebiliriz.

**Leaderboard** (resmi olmayan): `https://stats-data.hyperliquid.xyz/Mainnet/leaderboard` — top trader adresleri. Adres havuzunu ilk gün "tohumlamak" için (bot yeni açıldığında fills geçmişimiz yokken bile büyük hesapları tarayabilelim).

**Rate limit:** IP başına ~1200 weight/dk. `clearinghouseState` düşük weight'li → dakikada yüzlerce adres taranabilir; yine de tarayıcıya pacing + 429 backoff koyacağız.

### 2.2 Earnings Calendar

| Kaynak | Artı | Eksi |
|---|---|---|
| **Yahoo Finance / yfinance** (birincil) | Keysiz, kurulumsuz çalışır; HL evreni küçük olduğu için ticker başına sorgu yeterli; saat ipucu var | Resmi API değil; datacenter IP'lerinde nadiren engel |
| **Finnhub** `/calendar/earnings` (opsiyonel yedek) | Ücretsiz 60 çağrı/dk, `hour` alanı (bmo/amc/dmh), EPS beklentisi | Key gerekir (ücretsiz) — key girilirse otomatik devreye girer, saat bilgisini iyileştirir |
| **Alpha Vantage / Nasdaq JSON** (yedek 2) | Alternatif çapraz kontrol | İleriki faz |

Günde 2 kez (TSİ sabah + akşam) 14 günlük pencere çekilir, HL hisse evreniyle kesiştirilir, `earnings_events` tablosuna yazılır. İki kaynak çapraz doğrulanır (tarih uyuşmazsa alert'e "tarih belirsiz" notu düşülür).

**Alert zamanlaması** ("earnings'e 1 saat kala"):
- **AMC** (kapanış sonrası, en yaygın): rapor ~16:05 ET → alert **15:00 ET** (TSİ 22:00/23:00)
- **BMO** (açılış öncesi): rapor ~07:00 ET → alert **önceki akşam 21:00 ET** + tekrar **06:00 ET**
- Saat bilinmiyorsa: her iki pencerede de gönder (konservatif)
- Ek: **T-24h "erken uyarı"** taraması — OI anomalisi varsa bir gün önceden haber

---

## 3. Mimari

**Dil/stack:** Python 3.12 — `aiohttp` (HL REST+WS), `APScheduler` (zamanlama), `FastAPI + Jinja2 + HTMX` (dashboard), `python-telegram-bot` yerine düz Bot API (basitlik), `SQLite (WAL)` (hafıza).

**Railway:** başlangıçta **tek service** (tek container: FastAPI + background task'ler aynı process'te) + **Volume `/data`** (SQLite burada yaşar → "hafıza" restart'ta kaybolmaz). Volume tek replica'ya bağlanır; Telegram polling de zaten tek instance ister → mimariyle uyumlu. Büyürsek worker/web ayrımı + Railway Postgres'e geçiş planda hazır.

```
hlearninginsiders/
├── app/
│   ├── main.py            # FastAPI + startup'ta background task'ler
│   ├── config.py          # env vars
│   ├── db.py              # SQLite şema + migration
│   ├── hl/
│   │   ├── client.py      # info endpoint sarmalayıcı (pacing, retry, backoff)
│   │   ├── universe.py    # perpDexs + meta → hisse evreni (6 saatte bir)
│   │   └── collector.py   # WS trades dinleyici (reconnect'li), fills → DB
│   ├── earnings/
│   │   ├── calendar.py    # Finnhub + yedekler, kesişim, event planlama
│   │   └── evaluator.py   # T+2h / T+24h sonuç değerlendirme
│   ├── radar/
│   │   ├── scanner.py     # aday adres → clearinghouseState → pozisyon snapshot
│   │   ├── scorer.py      # insider skoru
│   │   └── anomaly.py     # OI/funding/hacim anomali dedektörü
│   ├── telegram/
│   │   ├── bot.py         # komutlar: /scan SNDK, /watchlist, /upcoming
│   │   └── format.py      # mesaj şablonları
│   └── web/               # dashboard (templates + routes)
├── Dockerfile
├── railway.json
└── requirements.txt
```

---

## 4. İnsider Skoru (0–100)

| Sinyal | Puan | Neden |
|---|---|---|
| Pozisyon earnings'e <48h kala açıldı | +15 (<6h: +25) | Zamanlama şüphesi |
| **Taze cüzdan**: ilk depozit <7 gün ve ilk işlemi bu coin | +25 | Klasik insider paterni (kirli iş temiz cüzdanla yapılır) |
| Hesap **sadece bu coin'de** pozisyon taşıyor | +10 | Konsantrasyon = bilgi |
| Pozisyon, coin OI'sinin >%5'i | +15 | Piyasayı domine eden boyut |
| Yüksek kaldıraç (likidasyona <%15 mesafe) | +10 | Conviction — emin olmayan böyle risk almaz |
| Negatif funding'e rağmen pozisyon tutuyor | +5 | Maliyeti göze almış |
| **Watchlist**: geçmiş earnings'lerde 1+ kez doğru bildi | +20 (2+: +35) | Kanıtlanmış sicil |
| TWAP paterni (düzenli aralıklı eşit fill'ler) | +5 | Sessiz birikim |

Skor ≥50 → mesajda 🚨 "insider şüphelisi" etiketi.

---

## 5. Telegram Çıktıları

**A) T-1h Earnings Raporu** (ana ürün):

```
🎯 SNDK earnings — 1 saat kaldı (AMC, ~23:05 TSİ)
📊 Mark $61.20 │ OI $8.4M (24h: +180% ⚠️) │ Funding -0.042%/h (shortlar ödüyor)
⚖️ Long/Short dengesi: %38 / %62

🐋 En büyük pozisyonlar:
1. 🚨 [87 puan] 0xab..3f SHORT $2.1M @ $63.4 │ 5x │ liq $71.2 │ 14h önce açıldı │ TAZE CÜZDAN
2. ⚠️ [55 puan] 0x71..9c SHORT $840K @ $62.1 │ 3x │ liq $79.0 │ 2g önce │ watchlist: 2/2 doğru
3. [22 puan] 0xe4..a1 LONG $610K @ $58.9 │ 10x │ liq $55.1 │ 6g önce
...
🔍 Detay: dashboard.xyz/t/SNDK
```

**B) Anlık balina alert'i (7/24):** watchlist adresi VEYA >$500K notional yeni pozisyon → dakikalar içinde bildirim (earnings saati beklemeden — SNDK balinasını poz açtığı anda yakalamak için).

**C) Earnings sonrası kapanış raporu (T+24h):** "0xab..3f shortu $310K kârla kapattı ✅ doğru bildi → watchlist'e eklendi (sicil: 1/1)".

**Komutlar:** `/scan SNDK` (anlık tarama), `/upcoming` (bu haftaki HL-eşleşen earnings'ler), `/watchlist`, `/whale 0x...` (adres karnesi).

---

## 6. Dashboard (senin yeni istek)

FastAPI + HTMX, Railway public URL, basit token korumalı (`?key=...`).

- **Arama kutusu:** "SNDK" yaz →
  - **En büyük pozisyonlar** tablosu: side, notional, entry, kaldıraç, uPnL, açılış zamanı, insider skoru — sıralanabilir
  - **Likidasyona en yakın en büyük pozlar:** `liq mesafesi % = |mark − liqPx| / mark` ile sıralı ayrı tablo (senin istediğin görünüm — "kim sıkışmış, nerede stop avı olur" analizi)
  - Coin özeti: OI, funding, hacim trendi (sparkline), long/short oranı, yaklaşan earnings geri sayımı
  - Son büyük fill'ler akışı
  - "Şimdi tara" butonu (on-demand tarama tetikler)
- **Adres sayfası** `/whale/0x...`: tüm pozisyonları, fill geçmişi, earnings karnesi (kaç kez doğru bildi), taze cüzdan bilgisi
- **Earnings takvimi sayfası:** HL-eşleşen yaklaşan earnings'ler + her biri için hazırlık durumu

Not: pozisyon verisi, scanner'ın snapshot'larından gelir (DB); "şimdi tara" canlı tazeler. Böylece dashboard HL API'sini dövmez.

---

## 7. Veri Modeli (SQLite — "hafıza")

```sql
tickers(coin PK, dex, symbol, name, max_leverage, listed_at)
earnings_events(id PK, symbol, coin, date, hour_hint, exact_ts, eps_est, source, status, alerted_t24, alerted_t1, evaluated)
fills(id PK, coin, address, side, px, sz, notional, ts, tid)           -- WS'den; >$5K filtre
addresses(address PK, first_seen, first_deposit_ts, deposit_origin, label, earnings_hits, earnings_misses, watchlist BOOL, notes)
position_snapshots(id PK, event_id?, coin, address, ts, phase, side, szi, entry_px, leverage, liq_px, upnl, notional, score, score_reasons)
asset_metrics(coin, ts, mark_px, oi, funding, day_volume)              -- anomali tespiti için zaman serisi
alerts_log(id, kind, coin, ts, payload)
```

`phase`: `T-24h / T-1h / T+2h / T+24h / ondemand` — aynı eventin evreleri karşılaştırılarak "earnings'ten hemen önce girdi, hemen sonra çıktı" paterni kanıtlanır.

---

## 8. Senin Aklına Gelmeyenler — Ek İnsider Yakalama Yöntemleri

1. **Kazanan adres hafızası (en güçlüsü):** Her earnings sonrası doğru yön alanlar sicile işlenir. 2+ doğru bilen adres yeni poz açtığı **anda** (earnings olmasa bile) alert. Dünkü SNDK balinası bir daha herhangi bir hissede poz açarsa saniyeler içinde haberimiz olur.
2. **Taze cüzdan paterni:** İlk depoziti <7 gün önce yapılmış, ilk işlemi earnings hissesi olan cüzdan = klasik insider davranışı (ana cüzdanını kirletmek istemez). `userNonFundingLedgerUpdates` ile tespit.
3. **OI/funding anomali dedektörü:** Pozisyon sahibini bulamasak bile, earnings'e 24–48h kala OI'de anormal sıçrama veya funding'in aşırı kayması → "birileri birikiyor" erken alarmı. `asset_metrics` zaman serisinden z-score ile.
4. **Bağlantılı cüzdan kümeleri:** Aynı kaynaktan fonlanan çoklu cüzdanlar (transfer geçmişinden) → bölünmüş tek balina pozisyonunu birleştirip gerçek boyutu göster. Pozisyonunu 5 cüzdana bölen insider'ı tek tek küçük sanmayız.
5. **Korele hisse taraması:** SNDK earnings'inde WDC/MU (aynı sektör) pozları da taranır — insider bazen doğrudan hisse yerine sektör komşusuyla oynar. Statik sektör eşleme tablosu ile.
6. **TWAP/birikim tespiti:** Düzenli aralıklı, benzer boyutlu fill serileri = dikkat çekmeden poz biriktiren büyük oyuncu.
7. **(Faz 4+) Options/short interest çapraz kontrolü:** Hisse options IV skew veya unusual activity, HL pozisyonuyla aynı yönü gösteriyorsa sinyal katlanır.
8. **Likidasyon haritası:** Dashboard'daki "en yakın liq" verisi ayrıca toplu görselleştirilir — hangi fiyat seviyesinde kaç $ likidasyon birikmiş (earnings sonrası hareketin nereye "çekileceği" tahmini).

---

## 9. Uygulama Fazları

| Faz | İçerik | Çıktı |
|---|---|---|
| **0 — İskelet** | Repo yapısı, config, DB, HL client, Telegram echo, Dockerfile, Railway deploy + Volume | Bot Railway'de "merhaba" der |
| **1 — MVP** | Universe + calendar + T-1h raporu (leaderboard havuzuyla pozisyon taraması, skorsuz) | İlk gerçek earnings alert'i |
| **2 — Göz** | 7/24 WS collector + fills havuzu + `asset_metrics` + Dashboard v1 (arama, en büyük pozlar, **en yakın liq**) | Kapsama büyür, dashboard açılır |
| **3 — Beyin** | Skorlama, taze cüzdan, evaluator, watchlist, anlık balina alert'leri, /komutlar | "İnsider şüphelisi" etiketi çalışır |
| **4 — Radar+** | Anomali dedektörü, cüzdan kümeleri, korele hisseler, likidasyon haritası, options verisi | Tam radar |

**Env vars (Railway):** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `FINNHUB_API_KEY`, `ALPHAVANTAGE_KEY` (ops.), `DASHBOARD_TOKEN`, `DB_PATH=/data/radar.db`, `MIN_FILL_NOTIONAL=5000`, `WHALE_ALERT_NOTIONAL=500000`.

---

## 10. Riskler ve Önlemler

- **"Coin'deki tüm pozisyonlar" diye tek endpoint yok** → adres havuzu yaklaşımı; ilk günlerde kapsama sınırlı, WS collector çalıştıkça tamamlanır. Hızlandırıcı: leaderboard tohumu + on-demand `recentTrades` backfill.
- **Leaderboard endpoint'i resmi değil** → düşerse sistem fills+watchlist ile çalışmaya devam eder (soft dependency).
- **WS kopmaları** → exponential backoff'lu auto-reconnect + kopukluk penceresi `recentTrades` ile doldurulur.
- **BMO/AMC belirsizliği** → çift pencere gönderimi; iki takvim kaynağı çapraz kontrol.
- **Rate limit** → istek pacing, weight bütçesi, 429'da backoff; tarama büyükse en aktif N adresle sınırla.
- **Railway restart** → tüm state SQLite'ta (/data volume), scheduler job'ları DB'den yeniden kurulur (idempotent).
- **Hukuki not** → bu bir gözlem/istihbarat aracı; yatırım tavsiyesi değil, mesajlara dipnot eklenir.

---

## Kaynaklar

- [HIP-3: Builder-deployed perpetuals (resmi docs)](https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-3-builder-deployed-perpetuals)
- [Info endpoint — Perpetuals (resmi docs)](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)
- [WebSocket Subscriptions (resmi docs)](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions)
- [Trades Stream — users:[buyer,seller] alanı (Dwellir)](https://www.dwellir.com/docs/hyperliquid/trades)
- [clearinghouseState + dex parametresi (Dwellir)](https://www.dwellir.com/docs/hyperliquid/clearinghouse-state)
- [What is HIP-3 (Nansen)](https://nansen.ai/post/what-is-hip-3-hyperliquid)
- [TradeXYZ / xyz dex hisse perp'leri (Datawallet)](https://www.datawallet.com/crypto/hip-3-explained-hyperliquid-upgrade)
- [SNDK-PERP (Hyperdash)](https://hyperdash.com/asset/sndk-perp)
- [Finnhub Earnings Calendar API](https://finnhub.io/docs/api/earnings-calendar)
- [Alpha Vantage API docs](https://www.alphavantage.co/documentation/)
