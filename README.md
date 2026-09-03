# 🕵️ HL Insider Radar

Hyperliquid HIP-3 **hisse perp'lerinde** (SNDK, NVDA, TSLA…) earnings öncesi
insider şüpheli balina pozisyonlarını yakalayan bot + dashboard.

- 📅 Earnings takvimini izler (Yahoo birincil, TradingView/Nasdaq/Finnhub çapraz doğrulama)
- ⏰ Earnings'e ~1 saat kala o coindeki **en büyük pozisyonları** Telegram'a atar
- 🐋 7/24 trade akışını dinler, **balina adres havuzu** biriktirir, eşik üstü işlemlerde anlık alert
- 🔔 Bildirim eşiği enstrümana göre **kademeli**: endeks/emtia (XYZ100, SP500, GOLD) **$10M**,
  hacimce büyük hisseler (NVDA) **$5M**, küçük/orta hisseler **$1M** — sitede eşik değişmez,
  yalnız Telegram'a düşme kapısı
- 🌍 **HL'nin en büyükleri:** ana dex dahil tüm Hyperliquid'de gördüğümüz dev pozisyonlar —
  kademeli eşik: HIP-3 hisse/emtia **$1M**, kripto **$20M**, BTC/ETH **$50M**
- 🧠 **Hafıza:** earnings sonrası kim doğru bildi → sicil; 2+ doğru bilen otomatik watchlist'e girer ve yeni işlem açtığı anda haber verir
- 🕵️ İnsider skoru: zamanlama, taze cüzdan, **yeni fonlama**, boyut/OI, kaldıraç, sicil, agresif alım (taker)…
- 💥 **Likidasyon radarı:** dev pozisyonlar patlama fiyatına yaklaşınca kademeli uyarı; fiyat bandına göre kümelenmiş **liq duvarları**
- 🧱 **Emir defteri duvarları:** deftere konmuş dev bekleyen emirler; duvar çekilirse (spoof) haber verir
- 🕐 **Saat istatistiği:** hisse hangi saatte yükseliyor/düşüyor (90 günlük 1h mumlar), borsa açık/kapalı seans ayrımı
- 🌙 **Seans karnesi:** hareketin ne kadarı borsa **kapalıyken** geldi, ne kadarı açıkken —
  yükseliş ve düşüş ayrı ayrı. Hisse senedi kapalıyken fiyat gidiyorsa o hareket saf
  perp/balina kaynaklıdır; "kapalı payı" bunu tek sayıda söyler
- 📈 **Fiyat grafiği:** coin sayfasında mum grafiği — liq duvarları, balina giriş seviyeleri ve geçmiş bilanço günleri üzerinde işaretli; isteğe bağlı TradingView gömülüsü
- 🎯 **Pozisyon takibi:** bir balinayı takibe al, kapatınca/eksiltince tahmini P&L ile haber ver
  — takip **pozisyon kapanana kadar** sürer, süreyle düşmez (14 günde bir "bırakayım mı?" diye yoklar)
- 🤖 **AI analist:** veriden **sınanabilir** hipotez üretir ("NVDA 48 saatte %2 düşer"),
  Python vadesi gelince ölçer ve tuttu/tutmadı diye damgalar — modelin **kendi sicili**
  sitede görünür. Bedava Groq ile çalışır; Telegram'a hiçbir şey düşmez
- 📊 Dashboard: ticker ara → en büyük pozlar + **likidasyona en yakın pozlar** + balina karnesi;
  grafiklerin üstüne gelince **anlık ipucu** (fiyat/saat/kim), ✅ PROPR filtresi, iki tema

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
| `/takipler` · `/birak_N` | Aktif pozisyon takipleri · takibi bırak |
| `/watchlist` | Sicilli adresler |
| `/devler` | Hyperliquid'in en büyük açık pozisyonları |
| `/status` | Bot durumu (WS, havuz boyutları, tarama hızı, son yenilemeler) |
| `/tani` | **Tam sistem dökümü** — bir şey ters giderse bunu kopyalayıp gönder |
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

**Sayfa eşiği ≠ bildirim eşiği.** Site bilerek daha çoğunu gösterir, kanal daha
seçicidir: sayfada bağlam olan şey Telegram'da gürültüdür. Likidasyon duvarları
bunun en net örneği — `liq_cluster_min_usd` ($1M) yalnız **ana sayfadaki
likidasyon haritasını** besler, Telegram'a düşmek için duvarın
`liq_cluster_alert_min_usd` ($5M) eşiğini geçmesi gerekir; likit/endeks
varlıklarda (GOLD, XYZ100, FX…) o eşik zaten `liq_cluster_big_min_usd`
($20M)'dir. Aynı ayrım `/saatler` (hepsi listelenir, kanala yalnız "güçlü"
saatler gider), `/hacim` (her rekor kaydedilir, bildirimde coin başına bekleme
var) ve `/kapali` (her bant kaydedilir, kümülatif bant bildirimi yalnız hafta sonu)
için de geçerlidir.

**Kapalı seans, iki tetik iki kapı:** kümülatif %0,5 bantları **yalnız hafta
sonu** çalışır (hafta içi de açıkken günde ~16 saat bildirim demekti). **Ani
hareket** tetiği ise ABD kapalı *her* saatte çalışır — ama hafta içi eşiği
daha yüksektir (vars. %2 / hafta sonu %1), çünkü pre-market'te %1 sıradan.
SHEIN Pazartesi sabahı %12 düştüğünde bot "hafta sonu değil" diye susmuştu.

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
   Ana dex'in hacimce ilk 30 kripto coin'i de dinlenir ama YALNIZ tetikleyici olarak:
   oradan gelen işlemler `fills`'e yazılmaz ve alarm üretmez — sadece $2M+ süpüren
   adresin profiline hemen bakılır (`CRYPTO_WATCH_TOP=0` ile kapatılır).
4. Earnings'e 1 saat kala (AMC→15:00 ET, BMO→önceki akşam 20:00 ET + sabah 06:00 ET):
   aday adresler (fill havuzu ∪ leaderboard ∪ watchlist) `clearinghouseState` ile taranır,
   pozisyonlar skorlanır, top 10 Telegram'a gider, snapshot DB'ye yazılır.
5. Earnings'ten 24 saat sonra fiyat hareketi ölçülür → doğru bilenler sicile işlenir,
   2+ doğru bilen watchlist'e alınır, kapatanlar raporlanır.

## Hacim Patlaması: `/hacim`

PROPR'da listeli bir sembol, son 24 saatin **en yüksek 5 dakikalık hacmine**
ulaştığında olay üretilir: sitede `/hacim` sekmesine düşer, Telegram'da **ayrı
bir kanala** gider. Eşik mutlak bir dolar rakamı değil **sembolün kendi
normali** olduğu için ince perp'te de likit perp'te de çalışır.

**İki evren, iki kanal** — aynı dedektör; her birinde **sayfa eşiği ayrı,
bildirim eşiği ayrı**:

| Panel | Evren | Kanal | Sayfa | Bildirim |
|---|---|---|---|---|
| 📈 Hisseler | PROPR ∩ **xyz dex** | `CRYPTO_STOCKS_ID` | $10K | **$100K** |
| 🚀 Kripto | PROPR ∩ **ana dex** | `CRYPTO_CHAT_ID` | $50K | **$250K** |

Hisse eşiği düşük çünkü hisse perp'leri çok daha ince: SHEIN'in 24 saatlik
**toplam** hacmi $4.2M'ken patlama mumu ~$150K'ydı. Dördü de Ayarlar'dan ayrı
değiştirilir (*Hisse hacim* / *Kripto hacim*).

Eşiği ilk kurduğumuzda **tek** ayar hem kaydı hem bildirimi kesiyordu: eşik
altı rekorlar sayfaya da yazılmadığı için panel boş kalıyor ve "radar mı
çalışmıyor, eşik mi yüksek" sorusu cevapsız kalıyordu. Şimdi sayfa her rekoru
gösteriyor, "Bildirim" kolonu hangisinin kanala gittiğini söylüyor.

**Boşsa neden boş, sayfa söylüyor.** Her tur şunları yazar: kaç kova
incelendi, kaç rekor bulundu, kaçı hangi eşiğe takıldı ve **eşiğin altında
kalan en büyük rekor hangisiydi**. Ayrıca mum verisi hiç gelmeyen semboller
ayrı sayılır (`n_nodata`) — bu bir hata değil veri yokluğudur, ve sayı
yüksekse sorun eşikte değil kaynaktadır. Aynı sayılar `/tani` çıktısında da
vardır.

**Kanal kurulumu:** yeni bir Telegram kanalı/grubu aç, mevcut botu oraya admin
yap, Railway'e ilgili değişkeni ekle. Yeni bot/token gerekmez. Tanımlı değilse
olaylar sitede kaydedilir ama **gönderilmez** — ana kanala düşürmek ayırma
isteğini bozardı.

**PROPR listesi elle tutulur ve eskir.** Liste `app/propr.py` içinde sabit bir
kopya; HL yeni perp ekledikçe geride kalır ve filtre koyduğumuz her yerde
(hacim radarı, kapalı seans bildirimi) o semboller **sessizce** elenir. SHEIN
tam olarak böyle kaçtı. Bu yüzden `/hacim` ve `/tani`, dexte olup listede
olmayan sembolleri **sayıp yazar**; eksik olanı Ayarlar → *propr.xyz ek
semboller* ile eklersin.

**Kripto/TradFi ayrımı** PROPR listesini bölerek değil **ana dex üyeliğiyle**
yapılır: HL'de hisse/emtia perp'leri `xyz:` önekli geldiği için ana dexte kalan
her şey zaten kriptodur. PROPR'a sonradan kripto eklersen otomatik girer.

**Birim meselesi:** HL mumundaki hacim alanının baz mı quote mu olduğuna dair
varsayım yapılmaz. Rekor karşılaştırması **ham mum hacmi** üzerinden yapılır —
aynı coinin kendi kovaları kıyaslandığı için birim sadeleşir. Dolar değeri
yalnız gösterim ve asgari hacim eşiği içindir ve kendini denetler: 24 saatlik
toplam borsanın bildirdiği hacimle 5 kattan fazla saparsa sayfada uyarı çıkar.

İki ayrıntı: (a) devam eden mum hesaba katılmaz (yarım hacmi tam kovayla
kıyaslamak yanıltırdı) — bu yüzden en fazla 5 dakikalık gecikme vardır.
(b) Uzun bir yükselişte her yeni kova yeni bir rekor olabilir; Telegram'a hepsi
gitmez (coin başına bekleme) ama **tablodaki her satır gerçek bir rekordur**.

Maliyet: sembol başına turda 1 istek. İki radar birlikte ~200 sembol / 5 dakika
≈ **40 rpm**, bütçe 350 rpm. Her biri kendi Ayarlar grubundan ayrı ayrı
kapatılabilir (maliyet sıfırlanır).

## Ne Oldu?: `/neoldu`

Grafikte absürt bir hacim gördüğünde: **sembol + saat aralığı gir**, o
pencerede kimin ne aldığını/sattığını ve **açtı mı kapattı mı** çıkarımını gör.
Saatler TSİ ve `18:25` gibi yazılır (epoch değil).

**Neden OI?** Bir işlemin "alış" olması *açtı* demek değildir — bir satış hem
long kapatmak hem short açmak olabilir. İkisini ayıran tek şey **açık
pozisyonun (OI)** yönü:

| OI | Fiyat | Okuma |
|---|---|---|
| ↑ | ↓ | yeni **SHORT** açılmış |
| ↓ | ↓ | **LONG** kapanmış / likide olmuş |
| ↑ | ↑ | yeni LONG açılmış |
| ↓ | ↑ | short kapanmış (squeeze) |
| ~sabit | — | el değiştirmiş, yeni para girmemiş |

Bu okuma **manşet**tir; altında adres kırılımı gelir: kim ne kadar aldı/sattı,
kaç parçada, **taker oranı** neydi (fiyatı süpüren agresör mü, pasif emirle mi
doldu — bilgi taşıyan taraf genelde birincisidir), bilinen pozisyonu ve bakiyesi
ne, ve pozisyonuna göre **ne yaptı**: *long'unu artırdı · long'undan kırptı ·
yeni short açtı · pozisyonu kapattı · aldı ve sattı (scalp)*.

**İki eksik kapatıldı, ikisi de bu sekmenin varlık şartıydı:**

1. **Kripto işlem kaydı.** `collector` kripto akışını "yalnız tetikleyici"
   sayıp tek satır fill yazmıyordu — HYPE'ta kimin ne aldığına dair sıfır kayıt
   vardı. Artık `crypto_fill_min_notional` ($25K) üstü işlemler kaydediliyor.
   Eşik hisseden yüksek çünkü HYPE'ta tek dakikada ~$3.8M dönüyor.
   **Bu işlemler Telegram'a DÜŞMEZ** — alarm kapısı bilerek korundu, yoksa ana
   kanal kripto seline boğulurdu.
2. **Kripto OI kaydı.** `poll_metrics` yalnız `xyz` dex'ini geziyordu; artık
   ana dex de örnekleniyor (poll başına +1 istek) ama **yalnız PROPR'daki
   coinler** saklanıyor — `asset_metrics` 45 gün tutuyor, tüm ana dex gereksiz
   yer kaplardı.

**Dürüstlük kuralları** (`forensics.py`'nin çoğu bunlarla ilgili):

- **Veri yoksa yorum yok.** OI örneği yoksa ya da hareket gürültü sınırındaysa
  "belirsiz" denir. %0.05'lik bir OI değişimine "yeni short açıldı" demek
  uydurmadır.
- Pencerede ölçüm yoksa iki uç aynı örneğe düşer ve fark 0 çıkardı — bu durum
  ayrıca yakalanıp "belirsiz" sayılır, yoksa yanlışlıkla "el değiştirmiş"
  denirdi.
- Pozisyonunu hiç görmediğimiz adres **"bilinmiyor"**dur; yön uydurulmaz.
- Pozisyon verisi derin keşif turuna bağlı (75-125 dk); pencereden çok eskiyse
  çıkarım ⏳ ile işaretlenir. **"Profilleri tazele"** düğmesi rapordaki
  adreslerin defterini anında çeker (`forensics_probe_max`, vars. 15 adres =
  15 istek) ve çıkarımı düzeltir.
- **Kayıt başlangıcından önceki pencere "boş" değildir.** Sayfa "o dönemde
  kayıt tutulmuyordu" der; boş tablo bunu gizlemesin diye.

Sekme ayrıca aynı penceredeki bağlamı toplar: likidasyona yaklaşanlar, emir
duvarları, hacim rekoru ve bizim gönderdiğimiz alarmlar.

## Cüzdan Bakiyesi Kolonu

Pozisyon tablolarında **HL bakiye** ve **Poz/Bakiye** kolonları var. Asıl bilgi
ikincisidir: $890K'lık bir pozisyon $50M'lik hesapta kıpırdanma, $171K'lık bir
pozisyon $200K'lık hesapta **her şeyini ortaya koymak** demektir. Notional tek
başına bu ayrımı yapamaz; insider şüphesinin konusu ikincisidir.

**Ek API maliyeti yok.** Veri zaten çektiğimiz iki yanıtın içindeydi ve
okumadan atıyorduk: `clearinghouseState` her dex için `marginSummary.
accountValue` döndürüyor, `leaderboard` da her satırda `accountValue` taşıyor
(onu yalnız sıralamak için okuyup çöpe atıyorduk). İkisi de artık işleniyor —
leaderboard sayesinde binlerce dev hesabın bakiyesi derin keşfin onlara
uğramasını beklemeden geliyor.

**Bu "net worth" DEĞİLDİR.** `accountValue` = perp teminatı. Spot bakiyesi,
vault'lar ve zincir dışı varlıklar dahil değil; kolon başlığı ve tooltip bunu
söylüyor. Spot dahil etmek adres başına +1 istek demekti (derin keşif turunun
maliyetini ~ikiye katlardı), bilerek yapılmadı.

Üç kural arayüzde:

- **Bilinmiyor ≠ sıfır.** Ölçmediğimiz adres `—` gösterir, 0 değil. 0 yazmak
  hem "hesap boş" izlenimi verirdi hem oranı sonsuza götürürdü.
- **Bayatlık görünür.** Derin keşif bir adrese 75-125 dakikada bir uğruyor;
  3 saatten eski ölçüm `⏳` ile işaretlenir.
- **Kapsama sayılır.** Tablo altında *"N/M satırda bakiye biliniyor"* yazar;
  aynı sayı `/tani`'da da var. Kolonun yarısı baştan boş olacak (yalnız derin
  keşfin uğradığı ve leaderboard'daki adresler) — bu bir arıza değil, ve
  hangisi olduğu ekrandan anlaşılsın diye sayılıyor.

**İnsider skoru bu değişiklikten etkilenmedi**; oran şimdilik yalnız kolon.
İstenirse "pozisyon hesabın %X'i" ayrı bir skor sinyali olarak eklenebilir.

## Örüntü Bulucu: `/orintu`

"Şu anki grafik şekli geçmişte olduğunda sonra ne olmuş?" — **analog
(k-en-yakın-komşu) tahmini**. Son N barın log getirileri z-normalize edilir
(fiyat seviyesi ve oynaklık ölçeği düşer, saf şekil kalır), geçmişte yeterince
benzeyen pencereler bulunur, o pencerelerden **sonra** ne olduğu toplanır.
15m ve 1h dilimlerde çalışır; sekmede hem sıralı sinyal listesi hem
`?sym=NVDA` ile elle sorgu var.

**Olasılık tek başına bir şey söylemez.** "%62 yukarı" ancak **taban oranın**
yanında anlamlıdır: aynı seride şekle bakmadan da %58 yukarı çıkıyorsa haber
yok demektir. Tabloda asıl sayı **Fark** kolonu, **z** ise o farkın
rastlantıyla açıklanıp açıklanamayacağı (2 ≈ %95).

**Dört tuzak bilerek kapatıldı**, her biri `analog.py` içinde:

| Tuzak | Kapatılışı |
|---|---|
| İleriye bakma | eşleşmenin ileri penceresi sorgu anından ÖNCE bitmeli |
| Kendine benzeme | sorgu penceresiyle çakışanlar atılır |
| Sahte örneklem | üst üste binen pencereler tek örnek sayılır (≥ `win` bar aralık) |
| "En yakın 50" | dar geçmişte bu, geçmişin dörtte biri olabilir — bağımsız pencerelerin en fazla %15'i alınır, üstüne **benzerlik eşiği** |

Benzerlik eşiği ham mesafe değil **korelasyon**: z-normalize vektörlerde ikisi
birebir bağlı (d²=2(1−r)) ama ham mesafe pencere uzunluğuna göre anlam
değiştirir — 23 boyutta rastgele iki şeklin mesafesi ~1.41'de yoğunlaşır ve
"0.6" gibi bir eşik pratikte hiçbir şeyi geçirmez (ölçüldü). Korelasyon her
uzunlukta aynı şeyi söyler; 4300 pencerelik bir geçmişte ulaşılabilen en iyi
benzerlik ~0.65, r≥0.5 olan ~35 pencere. Varsayılan bu ölçüme dayanıyor.

**Karne zorunlu.** Üretilen her tahmin kaydedilir ve vadesinde otomatik
notlanır (`ai_hypotheses` deseninin aynısı); ölçüm barı yoksa `unresolvable`
— bayat veriyle "tuttu" denmez. Sekmede kalibrasyon tablosu var: *"%70
dediğimizde %66 oldu (n=41)"*. **Aracın güvenilirliğinin tek dürüst ölçüsü
budur, olasılığın kendisi değil** ve 30-50 kapanmış tahminden önce anlam
taşımaz.

**Havuz** varsayılan olarak `self` — bir sembol yalnız kendi geçmişiyle
eşleşir. En saf yorum bu ("bu hisse böyleyken hep şöyle yapardı") ama örneklem
küçük kalır: 180 günlük 1h arşivi 24 barlık pencerelerde ~180 **bağımsız**
örnek eder ve çoğu satırda dürüstçe "yeterli örnek yok" çıkar. Ayarlardan
`class` yapılınca aynı sınıfın (hisse/kripto) tüm geçmişi havuza girer.
Sekme bu durumu ayrı sayar ve çareyi yazar — sessiz boşluk bırakmaz.

**Bildirim** ayrı kanala gider (`PATTERN_CHAT_ID`) ve **üç koşulu birden**
ister: yeterli örneklem + z eşiği + anlamlı fark. z tek başına yetmez, çok
büyük n'de 3 puanlık fark bile "anlamlı" çıkar. Mesaj taban oranı, n'i ve
aracın o anki sicilini birlikte taşır. Her turda binin üzerinde kombinasyon
taranıyor; z≥2'yi şansa geçenler de olur — mesaj bunu da söyler.

**Mum arşivi** (`bars` tablosu) bunu besler: 1h 180 gün + 15m 60 gün, yalnız
kapanış + hacim. Artımlı çekim, sembol başına turda 1 istek (~12 rpm).
Hangi sembolde kaç bar olduğu sekmede ve `/tani`'da yazar: "yeterli veri yok"
denince sebebin arşiv mi eşik mi olduğu görünsün diye.

## Funding: `/funding`

Bütün hisse perp'leri funding oranına göre sıralı — varsayılan en yüksek üstte,
başlığa tıklayarak istediğin kolona göre.

Funding bir **pozisyonlanma** sinyalidir: pozitif = longlar shortlara ödüyor
(kalabalık long), negatif = tersi. Hyperliquid funding'i **saatlik** öder (çoğu
borsanın 8 saatliği değil), o yüzden yıllık karşılık `oran × 24 × 365`:
%0.05/saat = **%438/yıl**. Tabloda ikisi de var; sıralamayı sezgisel yapan
yıllık olan.

24 saatlik değişim de gösteriliyor ve **işaret değiştirenler 🔄** ile işaretli —
kalabalığın yön değiştirmesi oranın kendisinden daha çok şey söyler.
🔥 aşırı funding demek ve eşiği `funding_extreme`'den gelir, yani sayfa ile
anomali bildirimi aynı şeye "aşırı" der.

**Ayırt edici kısım:** alttaki *"funding'i kim ödüyor"* tablosu — bildiğimiz
balina pozisyonlarının günlük funding gideri/geliri (`büyüklük × oran × 24`).
Funding oranı herkeste var; kimin ödediği için oranla pozisyon sahibini
birleştirmek gerekiyor.

Sınırlar: (a) long/short toplamları ve maliyetler yalnız **adres havuzumuzu**
kapsar — gerçek toplam değil, gördüğümüz kadarının alt sınırıdır. (b) Yalnız
hisse perp'leri; ana dex kripto funding'i toplanmıyor. (c) Yeni bildirim yok —
aşırı funding'i `anomaly` zaten aynı eşikle haber veriyor.

## Kapalı Seans: `/kapali`

HIP-3 perp'i 7/24 işlem görür, dayanak hisse görmez. Borsa kapalıyken oluşan
fiyat farkı bu yüzden **saf perp/balina akışıdır** — kimse gerçek hisseyle
arbitraj yapıp fiyatı yerine oturtamaz.

Sayfa iki şey gösterir:
- **Kapanıştan beri sapma:** her hissenin kapanış anındaki mark fiyatı ile
  şu anki fiyatı arasındaki yüzde fark. Varsayılan sıra mutlak sapma (en çok
  kıpırdayan üstte); başlığa tıklayarak istediğin kolona göre sıralanır.
- **Kapalıyken açılan pozisyonlar:** çıpadan sonra açılmış pozisyonlar,
  long ve short ayrı.

Piyasa açıkken sayfa boş kalmaz: bir önceki kapalı seansın **açılışa kadarki**
sapması gösterilir.

**xyz dex hiç kapanmaz (7/24); kapanan ABD'dir.** Asıl pencere hafta sonu:
**Cuma 24:00 → Pazartesi 00:00 TSİ**, tam 48 saat. Sayfa bu pencerede olduğunu
ve ne kadar kaldığını yazar.

⚠️ **Hafta sonu penceresinin bitişi ABD'nin açılışı DEĞİLDİR.** Pencere Pzt
00:00 TSİ'de biter, ABD ise Pzt **16:30** TSİ'de açılır (09:30 ET) — arada
16,5 saat daha kapalı kalır. Bu yüzden hem sayfa başlığında hem her kapalı
seans bildiriminde **ABD açılışına kalan süre** yazar. Bildirimler ancak ABD
gerçekten açıldığında susar; hafta içi ritim: 00:00 TSİ'de başlar, 16:30
TSİ'de susar.

Çıpa **TSİ'de sabit bir saattir** (Ayarlar → *ABD kapanış saati*, varsayılan
24:00). Türkiye'de yaz saati olmadığı için çıpa yıl boyu aynı saattedir;
ET karşılığı kışın 16:00 (tam kapanış), yazın 17:00'dir. Gözlemin farklıysa
ayardan kaydır — kod değişikliği gerekmez.

### Bildirimler (`🌙 Kapalı seans hareketi`)

**Pencere: yalnız hafta sonu** (Cuma 24:00 → Pzt 00:00 TSİ). Önce "ABD
kapalıyken" diye kurulmuştu, ama ABD hafta içi de 00:00–16:30 TSİ arası
kapalıdır — bu günde ~16 saat bildirim demekti. *Ayarlar → Kapalı seans →
"Bildirim yalnız hafta sonu"* kapatılırsa hafta içi geceler de dahil olur.

⚠️ Hafta sonu penceresinin bitişi (Pzt 00:00 TSİ) **ABD'nin açılışı değildir**
(Pzt 16:30 TSİ). Bu yüzden hem sayfa hem her bildirim **ABD açılışına kalan
süreyi** yazar — sayfa aradaki 16,5 saatte de kapalı olduğunu gösterir, ama
bildirim gelmez.

İki **ayrı** tetik — "hafta sonu boyunca yavaşça %0.8 saptı" ile "10 dakikada
%1.2 sıçradı" farklı olaylardır, birini diğerinin eşiğiyle ölçmek ikisini de
kaçırır:

| Tetik | Ne zaman | Tekrar |
|---|---|---|
| **Kümülatif sapma** | Çıpaya göre \|sapma\| her yeni **%0.5** bandını geçince | Her yeni bantta bir kez (%0.5 → %1.0 → %1.5…), **yön ayrı** |
| **Ani hareket** | **10 dakikada %1** | Hisse başına 30 dk bekleme |

Kurallar: yalnız **hafta sonu penceresinde** ve yalnız **PROPR'da listeli**
hisseler (harekete geçemeyeceğin hisse için bildirim gürültüdür). Eşiklerin
hepsi *Ayarlar → Kapalı seans*'tan değişir.

İki tasarım ayrıntısı: (1) bant anahtarında **çıpa** var, yani yeni pencere
sayaçları kendiliğinden sıfırlar — ayrı durum tablosu yok. (2) Ani hareket
penceresi seansın içine taşıyorsa (referans örnek çıpadan eski) tetiklenmez;
yoksa ölçülen şey "kapalıyken sıçrama" değil seansın normal oynaklığı olurdu.

ABD kapalıyken metrik örnekleme 5 dakikadan **1 dakikaya** iner
(`metrics_poll_closed_sec`) — "10 dakikada %1" tetiğinin çözünürlüğü budur.
Tek dex için poll = 1 istek, yani saatte 60 istek; bütçe 350/dakika.

Sınırlar: (a) ABD tatillerinde borsa kapalı ama hafta içi olduğu için çıpa
yanlış seçilebilir — tatil takvimimiz yok, o yüzden **çıpa zamanı ekranda
yazıyor**, yanlışsa görülür. (b) Çıpa örneği kapanıştan 30 dakikadan eskiyse
satır ⏳ ile işaretlenir. (c) "Geri dönerse şu kadar kazandırır" hesabı
**bilerek yok**: sapmanın geçmişte gerçekten geri döndüğü ölçülmeden o cümle
bir temenni olurdu.

## Saat İstatistikleri: `/saatler`

Her hissenin son ~90 günlük 1 saatlik mumları 24 saat kovasına bölünür; her kova
için ortalama getiri, kazanma oranı ve örnek sayısı tutulur. `/saatler` sayfası
**şu anki saatin** karnesini iki yönde sıralar: yükselmesi ve düşmesi beklenenler.

Bu bir **sıralama**dır, eşik filtresi değil — eskiden yalnız `örnek≥40 &
ortalama≥+%0.10 & kazanç≥%55` olanlar gösteriliyordu ve hiçbiri geçmediğinde
panel bomboş kalıyordu. Şimdi liste hep dolu, eşiği geçenler 💪 rozetli, geçen
yoksa "sinyal zayıf" diye yazıyor. Örneklemi ince olanlar sıralamaya girmez ama
sayılır — liste neden kısa, görünür.

**Kanala yayın kasten daha sıkı:** `/saatler/gonder` yalnız 💪 güçlü olanları
gönderir. Zayıf sinyali sayfada sıralı göstermek başka, kanaldan yayınlamak başka.

Ana sayfadaki "🕐 Şu saatte beklenenler" paneli ilk 5 yükseliş adayını gösterir
ve tam listeye bağlanır.

## Bir Şey Ters Gittiğinde: `/tani`

Telegram'da `/tani`, sitede `/tani` (düz metin, `?full=1` uzun sürüm) tek bir blokta
her şeyi verir: hangi görev ne zaman nabız attı, hangi tabloda kaç satır var ve
kaydın yaşı ne, alt sistemlerin sayaçları ve **hata metinleri**, panelden
değiştirdiğin ayarlar, ve son uyarı/hata satırları (`log_events` tablosu — tekrar
edenler `×N` diye tekilleşir, 7 gün saklanır).

Ekran görüntüsünden iyidir: eksiksiz, aranabilir, Railway log'una gitmeye gerek
bırakmaz. Sırlar (bot token, AI anahtarı, pano jetonu…) dökümde **yoktur** — ayar
bölümü yalnız `EDITABLE_FIELDS` üzerinde döner, sırlar oraya hiç girmez.

## Sınırlar / Bilinmesi Gerekenler

- "Coindeki tüm pozisyonlar" diye bir HL endpoint'i yok; kapsama **adres havuzu** kadardır.
  Havuz, WS collector çalıştıkça günden güne büyür (ilk günlerde leaderboard tohumu taşır).
  Pozisyonlar iki yoldan tazelenir: (a) **anlık sonda** — canlı akışta eşik üstü
  (varsayılan $100K) bir işlem görülen adresin TÜM defteri hemen çekilir; (b) **rotasyon** —
  süpürücü havuzu tur tur gezer. Yani hiç işlem yapmayan sessiz bir balinanın verisi
  rotasyon kadar tazedir; işlem yapan anında güncellenir.
- **Yetişme modu** (varsayılan açık): süpürücü sabit parti yerine diğer görevlerden
  ARTAN istek bütçesini kullanır — ortam sakinken parti 40'tan ~190 adrese çıkar,
  yoğunken kendiliğinden tabana iner. Sıcak tur ~2.5 saatten ~35-60 dakikaya,
  soğuk kuyruk ~37 saatten ~6-10 saate düşer. `SWEEP_CATCHUP=0` ile kapatılır;
  `SWEEP_RPM_HEADROOM` bütçenin ne kadarının doldurulacağını belirler.
- "HL'nin en büyükleri" panelindeki eşikler **kademelidir** (HIP-3 $1M · kripto $20M ·
  BTC/ETH $50M): altında kalan pozisyon hiç kaydedilmez, eşik yükseltilirse eski
  kayıtlar günlük bakımda budanır. Kapsam yine adres havuzu kadardır — "kesin en
  büyüğü" değil, "gördüklerimizin en büyüğü".
- Bir adresin defteri her dex için AYRI sorgulanır (ana dex + her HIP-3 dex'i).
  Tek istekte hepsini veren bir kestirme yok; dex'lerden biri hata verirse o adres
  o tur atlanır — eksik yanıtla "pozisyonu kapatmış" saymak kayıt silerdi.
- Leaderboard endpoint'i resmi değildir; düşerse bot fills+watchlist ile çalışmaya devam eder.
- Yahoo takvimi resmi API değildir; nadiren datacenter IP engeli görülebilir → Finnhub key'i eklemek sağlamlaştırır.
- Fiyat grafiği HL **perp** mumlarını çizer (balinaların gerçekten işlem gördüğü fiyat);
  liq/entry seviyeleri o fiyat uzayına aittir. Katlanabilir TradingView paneli ise
  **hisse senedinin** grafiğidir — seansları ve fiyatı birebir aynı değildir.
- Bu bir gözlem/istihbarat aracıdır; **yatırım tavsiyesi değildir**.

## AI Analist (opsiyonel)

Bir dil modeline ham veri verip "örüntü bul" demek kendinden emin uydurma üretir.
Bu yüzden iş bölünmüştür: **istatistiği Python hesaplar, model yalnız hipotez
önerir, kararı yine Python verir.**

1. Her turda (varsayılan 2 saat) Python kompakt bir brifing hazırlar — içinde
   sitede **hiç göstermediğimiz** veriler de var: `asset_metrics`'in 45 günlük
   serisi, `fills.taker` agresörlük dengesi, çekilmiş emir defteri duvarları,
   `hl_positions` yaşam döngüsü, `alerts_log` (botun kendi gürültü kaydı),
   cüzdan bağlantıları, likidasyona tırmanma kademeleri, listelenme yaşı.
2. Model, **kapalı bir metrik listesinden** seçerek hipotez üretir
   (`price_move_pct`, `oi_change_pct`, `volume_ratio`, `position_closed`,
   `position_grew_pct`). Ölçülebilir bir ölçüte bağlanamayan iddia **gözlem**
   sayılır ve sicile girmez.
3. Kayıt anında baz değer saklanır; vade gelince Python ölçer:
   **tuttu / tutmadı / ölçülemedi**. Ölçülemeyen hiçbirine sayılmaz — istatistiği
   güzelleştirmek için tahmin yürütmek aracın anlamını bozardı.
4. `/ai` sayfasında modelin karnesi en üstte durur. Uyduruyorsa istatistik onu
   ele verir.

**Kurulum (üç adım):**
1. [Groq](https://console.groq.com)'tan bedava anahtar al
2. Railway → Variables: `AI_API_KEY=gsk_…` **ve** `AI_ENABLED=1`
   (ya da anahtarı ekleyip Ayarlar → AI analist'ten aç)
3. `/ai` sayfasında **▶️ Şimdi çalıştır**'a bas — anahtarın doğru olup olmadığını
   beklemeden görürsün. Sonra kendi ritmiyle 2 saatte bir çalışır.

Bedava katmanda `openai/gpt-oss-120b` için günde 200K token sınırı var;
2 saatlik tur ~48K/gün eder, rahat pay kalır.

**Model adları sağlayıcıda değişir** (Groq eski llama'ları 06/2026'da emekliye
ayırdı). Yanlış model adı girilirse `/ai` sayfasındaki hata metni sağlayıcıdan
**kullanılabilir modelleri çekip listeler** — doğrusunu oradan kopyalayıp
`AI_MODEL`'e yaz. Sağlayıcı değiştirmek için `ai_base_url` + `ai_model` yeter;
Groq/Cerebras/OpenRouter/DeepSeek aynı OpenAI-uyumlu biçimi konuşur.

Çıktı **yalnız sitede** görünür: doğrulanmamış AI metni Telegram'a düşmez.

## Üçüncü Taraf

Site kendi kendine yeterlidir: harici CDN'e, yazı tipine ya da script'e istek atmaz.
Tek istisna, coin sayfasındaki **katlanabilir TradingView paneli** — yalnız sen o
paneli açarsan `s3.tradingview.com`'dan yüklenir, ayarlardan tamamen kapatılabilir.

Mum grafiği [TradingView Lightweight Charts](https://github.com/tradingview/lightweight-charts)
(Apache 2.0) ile çizilir; kütüphane depoya gömülüdür
(`app/web/static/lightweight-charts.js`, lisans yanındaki `LICENSE-lightweight-charts.txt`).

## Lokal Çalıştırma

```bash
pip install -r requirements.txt
cp .env.example .env   # doldur
uvicorn app.main:app --reload
# http://localhost:8000
```
