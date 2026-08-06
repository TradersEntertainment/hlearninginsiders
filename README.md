# 🕵️ HL Insider Radar

Hyperliquid HIP-3 **hisse perp'lerinde** (SNDK, NVDA, TSLA…) earnings öncesi
insider şüpheli balina pozisyonlarını yakalayan bot + dashboard.

- 📅 Earnings takvimini izler (Yahoo birincil, Finnhub opsiyonel)
- ⏰ Earnings'e ~1 saat kala o coindeki **en büyük pozisyonları** Telegram'a atar
- 🐋 7/24 trade akışını dinler, **balina adres havuzu** biriktirir, eşik üstü işlemlerde anlık alert
- 🧠 **Hafıza:** earnings sonrası kim doğru bildi → sicil; 2+ doğru bilen otomatik watchlist'e girer ve yeni işlem açtığı anda haber verir
- 🕵️ İnsider skoru: zamanlama, taze cüzdan, **yeni fonlama**, boyut/OI, kaldıraç, sicil…
- 📊 Dashboard: ticker ara → en büyük pozlar + **likidasyona en yakın pozlar** + balina karnesi

Detaylı mimari için: [PLAN.md](PLAN.md)

---

## Railway'e Kurulum

1. **Telegram botu oluştur:** Telegram'da `@BotFather` → `/newbot` → token'ı kopyala.
2. **Railway'de yeni proje:** *Deploy from GitHub repo* → bu repoyu seç. `Dockerfile` otomatik algılanır.
3. **Volume ekle (hafıza!):** Service → *Settings → Volumes → Add Volume*, mount path: **`/data`**.
   Bunu yapmazsan her deploy'da sicil/havuz sıfırlanır.
4. **Env vars** (Service → Variables): `.env.example`'daki değerleri gir.
   Minimum: `TELEGRAM_BOT_TOKEN`, `DB_PATH=/data/radar.db`, `DASHBOARD_TOKEN` (kendin uydur).
5. Deploy bitince bota Telegram'dan **`/id`** yaz → verdiği sayıyı `TELEGRAM_CHAT_ID` olarak ekle → redeploy.
6. **Dashboard:** Service → *Settings → Networking → Generate Domain* → `https://…railway.app/?key=DASHBOARD_TOKEN`

> Not: Tek instance çalıştır (Telegram polling + Volume zaten tek replica ister).

## Telegram Komutları

| Komut | İş |
|---|---|
| `/scan SNDK` | Coini şimdi tara, en büyük pozları ve skorları göster |
| `/upcoming` | Yaklaşan HL-eşleşen earnings'ler |
| `/whale 0x…` | Adres karnesi + canlı pozisyonları |
| `/watch 0x…` / `/unwatch 0x…` | Watchlist'e ekle/çıkar |
| `/watchlist` | Sicilli adresler |
| `/status` | Bot durumu (WS, havuz boyutları, son yenilemeler) |
| `/bildirimler` | Bildirim ayarları + son gönderilenler |
| `/gecmis` · `/winners` | Bilanço arşivi · en iyi biliciler |
| `/settime SHAZ bmo` | Bilanço saatini elle düzelt |
| `/refresh` | Takvimi tüm kaynaklardan yenile |

## Bildirimler

Her bildirim tipi (earnings raporu, yeni büyük pozisyon, likidasyon radarı, büyük işlem,
anomali, sonuç raporu, sabah özeti) dashboard → ⚙️ Ayarlar → **Bildirimler**'den tek tek
açılıp kapatılır. **Sessiz saat** penceresinde (varsayılan 01:00–08:00 TSİ) normal
bildirimler beklemeye alınıp sabah **günlük özet**te toplu gelir; "önemli" olanlar
(earnings, yeni büyük pozisyon, likidasyon) istersen sessiz saatte de düşer, **kritik**
olanlar (son likidasyon uyarısı, 70+ skorlu insider) her zaman geçer.

## Ayarlar

Ayar sayfası **`ADMIN_PASSWORD`** ile korunur: şifresi olmayan sayfayı görür ama değiştiremez
(`/login` ile giriş yapılır; şifre yoksa `DASHBOARD_TOKEN` kullanılır, o da yoksa koruma kapalıdır).

Eşikler ve periyotlar dashboard'daki **⚙️ /settings** sayfasından canlı değiştirilir
(DB'de saklanır → deploy'a dayanıklı, restart gerekmez). Env değişkenleri sadece ilk
varsayılandır; gizli anahtarlar (`TELEGRAM_BOT_TOKEN`, `FINNHUB_API_KEY`,
`DASHBOARD_TOKEN`, `DB_PATH`) güvenlik gereği yalnızca env'den yönetilir.

## Nasıl Çalışıyor?

1. `perpDexs` + `meta` ile HL'deki hisse perp evreni keşfedilir (varsayılan dex: `xyz`).
2. Yahoo'dan (yfinance) evrendeki sembollerin earnings tarihleri çekilir; `FINNHUB_API_KEY` verilirse çapraz doğrulanır.
3. WebSocket `trades` kanalı 7/24 dinlenir — her trade'de alıcı+satıcı adresi gelir; $5K üstü fill'ler ve adresleri DB'ye yazılır.
4. Earnings'e 1 saat kala (AMC→15:00 ET, BMO→önceki akşam 20:00 ET + sabah 06:00 ET):
   aday adresler (fill havuzu ∪ leaderboard ∪ watchlist) `clearinghouseState` ile taranır,
   pozisyonlar skorlanır, top 10 Telegram'a gider, snapshot DB'ye yazılır.
5. Earnings'ten 24 saat sonra fiyat hareketi ölçülür → doğru bilenler sicile işlenir,
   2+ doğru bilen watchlist'e alınır, kapatanlar raporlanır.

## Sınırlar / Bilinmesi Gerekenler

- "Coindeki tüm pozisyonlar" diye bir HL endpoint'i yok; kapsama **adres havuzu** kadardır.
  Havuz, WS collector çalıştıkça günden güne büyür (ilk günlerde leaderboard tohumu taşır).
- Leaderboard endpoint'i resmi değildir; düşerse bot fills+watchlist ile çalışmaya devam eder.
- Yahoo takvimi resmi API değildir; nadiren datacenter IP engeli görülebilir → Finnhub key'i eklemek sağlamlaştırır.
- Bu bir gözlem/istihbarat aracıdır; **yatırım tavsiyesi değildir**.

## Lokal Çalıştırma

```bash
pip install -r requirements.txt
cp .env.example .env   # doldur
uvicorn app.main:app --reload
# http://localhost:8000
```
