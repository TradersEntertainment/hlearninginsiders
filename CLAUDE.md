# CLAUDE.md: HL Insider Radar

> Bu dosya her Claude Code oturumunda otomatik yüklenir, ama yalnız commit'lenip `claude/insider-position-tracker-bot-9su8xs`
> dalına push edildiyse (yeni oturum repoyu GitHub'dan klonlar). Önceki sohbetlerin kaydı sende yok; bilmen gerekenler burada.
> Ayrıntı: `docs/handoff/HANDOFF.md`. Açık işin planı: `docs/handoff/kanal-mesaj-duzeni.md`. (Devir: 14.10.2026, `ca2f3bc` üstüne.)
> **Repo herkese açık** (GitHub: private=false). Bu dosyalara, README'ye ve commit'lere sır, chat id ya da kişisel bilgi yazma.

## Yeni oturumda ilk iş
1. `git status` ve `git log --oneline -5`: ağaç temiz mi, CLAUDE.md + HANDOFF.md'yi ekleyen "📋 Devir notu (2/2)" commit'i var mı.
2. `df -h /` ile diske bak.
3. `python tests/run.py` koş; 503/503 beklenir (yaklaşık 2 dk; Bash zaman aşımını en az 5 dk ver).
4. Kullanıcıya Türkçe 2-3 cümleyle nerede kaldığımızı söyle (ZZ1: 35 biçim, hiçbiri yapılmadı). Kullanıcı başka bir şey
   istiyorsa onunla başla; bir şey demediyse ZZ1'e devam etmeyi öner.

ZZ1 bitince bu dosyadaki "Açık iş" bölümünü sil, yerine bir sonraki açık işi yaz.

## Bu proje ne
Hyperliquid (HL) için balina / insider pozisyon, emir defteri ve TWAP radarı. Çıktılar iki yere gider: Telegram (ana sohbet
ve Railway env'iyle tanımlanan ayrı kanal/gruplar) ve FastAPI + Jinja2 web paneli. İlk hedef, HIP-3 hisse perp'lerinde (`xyz`
dex) bilanço öncesi açılan dev pozisyonları yakalamaktı. Bugün ana dex kripto, `para` kripto dex'i ve spot (TWAP dilimleri)
de izleniyor. Uygulama tek process (`uvicorn app.main:app`); `lifespan` ~40 asyncio döngüsünü `supervised()` altında başlatıyor.
Veriler SQLite'ta (WAL) ve `kv` tablosunda. README kurulumuna göre Railway'de Volume `/data`'ya bağlanır ve DB `/data/radar.db`
olur (config.py: /data yoksa `./data/radar.db`; `DB_PATH` env'i önceliklidir). Canlıdaki gerçek ayar repodan doğrulanamaz.

Nerede ne var:
- `app/main.py` görevleri başlatır · `app/config.py` ayarlar (`EDITABLE_FIELDS`, `Config.__init__`) · `app/notify.py` bildirim merkezi (`KINDS`, `PUBLIC_KINDS`)
- `app/health.py` bekçi (`limits()`: yeni döngü ekleyince buraya da satır ekle) · `app/diag.py` `/tani` dökümü
- `app/radar/*.py`: her dosya bir radar. TWAP dosyaları: `twap` (fills'ten düzenli birikim taraması; `all_orders` → `/twaplar` ve
  coin sayfası paneli), `twaplive`, `twapfast`, `twapfollow`, `twaploose` (🔸 yan + 🕐 büyük kanal) ve `hiddentwap`. Hisse bildirim kapısının tek kaynağı `alertgate.py`
- `app/telegram/format.py`: tüm mesaj şablonları ve `COMMANDS` listesi. `app/telegram/bot.py`: komutlar ve `_own_chats()` (yerlerini grep ile bul)
- `app/lab/` strateji laboratuvarı · `app/pay/` ödeme · `app/web/` sayfalar · `tests/`
- `README.md`: her özelliğin bölümü burada, çok uzun. `PLAN.md`: ilk tasarım, kısmen eskimiş

## Kullanıcıyla çalışma
- **Türkçe yaz**, kısa ve sade tut, jargon kullanma. Kullanıcı piyasa konularını iyi bilir (TWAP, liq, HIP-3, propr); altyapıda
  (git, disk, konteyner, /tmp) teknik değildir. Böyle bir konuda ne olduğunu, neyi etkilediğini ve neyi etkilemediğini düz söyle.
- Kullanıcı kısa ve gayriresmî yazar ("3 gruba da yazsam olur dimi") ve kararını çoğu zaman aynı gün inceltir. Bu yüzden küçük,
  geri alınabilir adımlarla ilerle, eşikleri env'e ya da ayara bağla.
- **GitHub'daki kodu ve canlı botu (Railway) asla riske atma.** Altyapıya dokunmadan önce "GitHub ve canlı bot etkilenir mi"
  sorusunu açıkça cevapla. Kullanıcının 14.10 oturumundaki sözü (önceki oturumun aktarımı; repoda yazılı kaydı yok): "disk dolu
  derken anlamadım githubı bozma".
- **Bir şey silmeden önce sor.** Önce neyin silineceğini açıkla, açık onay bekle (14.10 oturumundaki onay, yine yalnız oturum
  aktarımı: "tamam sil eskileri"). Onay gelse de dar kapsamlı sil.
- **Dürüst raporla, kendi hatanı da.** Ne oldu, etkisi ne, ne yaptın: hepsini düz söyle; saklama, süsleme.
- İstek çoğu zaman ekran görüntüsüyle ya da somut bir vakayla gelir ("bu niye gelmedi"). Vakayı canlı veriyle yeniden üret, kök
  nedeni bul, sonucu sayıyla anlat. Aynı şikâyet ikinci kez geliyorsa önceki düzeltme yüzeysel kalmıştır.
- **Neye erişebilirsin:** HL'nin herkese açık API'si konteynerden açık (`POST https://api.hyperliquid.xyz/info`); vakaları buradan
  yeniden üret. Canlı bota erişimin yok: Railway CLI ya da anahtarı yok, panel adresi repoda yazmıyor, web `/tani` `DASHBOARD_TOKEN`
  ister. Botun iç durumunu (`/tani`, `/twap durum`, `/saglik`) kullanıcıdan Telegram çıktısı ya da ekran görüntüsü olarak iste.
- **İş bitince:** `python tests/run.py` → commit → çok-ajanlı inceleme → düzeltme commit'leri → tam test → push. İnceleme Workflow
  aracıyla yapılır (önce `workflow-authoring` becerisini yükle): birkaç bağımsız inceleyici diff'e bakar, her bulgu düzeltilmeden
  önce yeniden üretilerek doğrulanır. Gelenek (git geçmişi): özellik commit'i önce, ayrı "…: inceleme düzeltmeleri" commit'i sonra
  gelir (ör. `4b8786c` → `98b9b83`; `d4c8028` → `b2271cf` → `f52109a` → `a5d8e8a`); gövdeye "İnceleme (yeniden üretildi):"
  maddeleri yazılır (`6ee1789`, `dffdb01`). Push canlıya gittiği için incelemeyi push'tan önce bitir.
- Commit mesajları Türkçe. Başlık çoğunlukla konu emojisiyle başlar (⏳ TWAP, 🕐 büyük, 🔸 yan kanal, 🕵️ gizli, 🔁, 📝, 🧪 lab,
  🧹 temizlik, 📋 devir…). Gövde kısa paragraf ya da madde listesidir; büyük işlerde "Testler: …" / "Tam takım N/N" satırı olur.
  Sonda Co-Authored-By ve Claude-Session satırları yer alır. Her özellikte README bölümü ve `.env.example` satırı da güncellenir.
- Kod ve test docstring'lerinde kullanıcının sözü tarihli alıntılanır: `Kullanıcı (gg.aa): "…"`, ardından "Kararlar:"; testlerde ayrıca "Pinlenenler:".

## Kalıcı kurallar (ürün)
Saatler TSİ (UTC+3). Hafta sonu = TSİ Cumartesi 00:00 → Pazartesi 00:00 (`hourstats.weekend_window`). Commit saatleri UTC.
1. **Ölçülü dil:** Mesajlarda tahmin, olasılık, işlem önerisi ve içeriden bilgi atfı olmaz. Yasak listesi
   `tests/test_wording.py:37-51`'de; yeni metin yazınca bu testi koş. Veri yoksa "bilinmiyor" yaz. Ölçülemeyen dolan hiç yazılmaz,
   "%0" asla yazılmaz (`2fdcb75`). Niyet varsayan etiket yok: "kalktı" yerine ölçüme göre DOLDU / ÇEKİLDİ / KISMEN (`b657ae0`).
   PROPR rozeti birebir `✅ <b>PROPR'da listeli</b>`. Dipnot: "ℹ️ Gözlem aracıdır, yatırım tavsiyesi değildir."
2. **İlk 3-4 satır:** Her kanal mesajında önemli olan her şey ilk 3-4 satırda olur (kullanıcı 14.10: "tüm kanallar için").
   TWAP'ta sıra: 1) ne, 2) hacme göre ne kadar, 3) ne zaman başladı ve ne kadar doldu, 4) kim (`e49e472`). 🔸/🕐 kanal notu
   mesajın başına değil, "PROPR'da listeli" satırının altına konur.
3. **Chat id'leri yalnız Railway env'de** tutulur; `EDITABLE_FIELDS`'a / ayar sayfasına girmez (config.py'deki "ENV-ONLY"
   yorumları). Gizli TWAP ayarları da env-only (kullanıcı 13.10: "gizli twaplar için ayarlara sokma beni, Railway'den değişken
   eklerim", `79b9969`), 🔸/🕐 eşikleri de (twaploose.py:14). Env adlarını değiştirme; kullanıcı onları kendisi ekliyor.
4. **Kanallar.** Temel üç sohbet (kullanıcının deyişiyle "kripto, hisse ve genel"):
   - `CRYPTO_CHAT_ID`: kripto TWAP, kripto hacim rekoru, kripto liq yakını, 🧲 ilk alarm.
   - `CRYPTO_STOCKS_ID`: hisse hacim rekoru, açılış raporu.
   - `TELEGRAM_CHAT_ID` (ana): hisse ve endeks TWAP, earnings, takip, sağlık ve geri kalan her şey.

   Ek gruplar: `ACCOUNT_CHAT_ID` (👤/🔂), `TWAP_MIRROR/LOOSE/BIG_CHAT_ID`, `HIDDEN_TWAP_CHAT_ID`, `LIQ_ATTACK_CHAT_ID`,
   `PATTERN_CHAT_ID`, `SIM_CHAT_ID`, `STRAT_CHAT_ID`, `TELEGRAM_CHANNEL_ID`. Tam tablo HANDOFF §3.6'da.
   **Tanımsız kanalın mesajı ana sohbete düşürülmez** (README:229-231). İstisnalar `LIQ_ATTACK_CHAT_ID` ve `STRAT_CHAT_ID`.
   `ACCOUNT_CHAT_ID` boşsa hesaplar yoklanmaz bile. Yeni kanal gerekiyorsa kullanıcıya env adını ve adımları söyle: botu gruba
   admin ekle → grupta `/id` yaz → çıkan sayıyı Railway Variables'a gir (README:228-231). Kodda `_own_chats()`'e de ekle.
5. **🕵️ Gizli TWAP yalnız `HIDDEN_TWAP_CHAT_ID`'ye** gider; aynaya, ana sohbete ya da kripto kanalına asla gitmez (`1bfb55e`).
6. **TWAP:**
   - İlk uyarıdan sonra kanala yalnız ⛔ iptal notu gider ("iptal etmedikçe bildirim atma", `924950b`).
   - ⚡ HIZLI = süre ≤ 1 sa (`47909ba`). Bu emirlerde takip tuşu yok; iptal kanala kendiliğinden yazılır.
   - Kullanıcı geç gelen bildirimi hata sayar.
   - Ana kapı iki yoldan geçilir: (a) plan ≥ $1M, kalan ≥ min($1M, planın yarısı) ve emir 24 saatlik hacmin ≥ %5'i (BTC/ETH %20);
     (b) ⚡ emirde (süre ≤ 60 dk) plan ≥ $250K, dakika hızı ≥ coinin ortalama dakika hacminin 2 katı ve planın ≥ %30'u kalmış
     (twapfast.py:17-21; `TWAP_FAST_SHORT_MIN_USD`, `TWAP_FAST_RATE_X`). TSİ Cumartesi'leri kripto olmayan coinlerde taban geçen
     Cumartesi'nin hacmi, eşik %10.
   - 🔸 yan kanal: her gevşek eşik ana eşikten küçük ya da eşit.
   - 🕐 büyük kanal: ≥ $1M ve ≤ 3 sa; BTC/ETH için ≥ $20M; hafta sonu kripto olmayan HIP-3 coinlerde ≥ $200K ve < 24 sa.
7. **PROPR listesi yalnız büyür** ve değişiklik için mesaj atılmaz ("sinyaller fazla olsun eksik olmasın", `c6b7ec5`).
8. **Hisse:** "Hacim arttı" tek başına sinyal sayılmaz. Eşikler: yeni pozisyon $8M, tek işlem $5M, OI artışı $5M. NVDA gibi büyük
   hisseler ve endeksler için "büyük pozisyon" bildirimi yok (`alertgate.py`, `b60fdb8`).
9. **Sayfa eşiği ≠ bildirim eşiği:** site çok şey gösterir, kanal seçicidir. Kullanıcının sabitlediği eşikleri (config.py'de
   "kullanıcı" yorumlu satırlar) ona sormadan değiştirme.
10. Strateji laboratuvarı asla telefonla aramaz; uyandırma alarmını yalnız sahip kurar. Takip, pozisyon kapanana kadar sürer.

Kuralların tamamı, eşik tablosu ve kaynakları: HANDOFF.md §2.

## Çalıştırma / test / yayın
- Dal: `claude/insider-position-tracker-bot-9su8xs` (GitHub'da varsayılan ve tek dal). Push: `git push -u origin claude/insider-position-tracker-bot-9su8xs`.
  Remote `TradersEntertainment/hlearninginsiders` (herkese açık). Klon sığ, görünen geçmiş 07.09.2026'da başlıyor.
- Testler:
  - Hepsi: `python tests/run.py`. Son sonuç 503/503 (`ca2f3bc`'de yeniden doğrulandı). Tam koşu yaklaşık 2 dk sürüyor; Bash zaman
    aşımını en az 5 dk ver. Çalışırken filtreli koş, commit'ten önce tam koş.
  - Filtreli: `python tests/run.py twap_big komutlar`. Adında filtre geçen dosyalar koşar.
  - **pytest kullanma.** `pytest` komutu var ama kendi ayrı ortamında çalışıyor ve projenin paketlerini görmüyor
    (`ModuleNotFoundError: aiosqlite`); `python -m pytest` de yok. Tek yol `python tests/run.py`. Konteynerde Python 3.11, Dockerfile'da 3.12.
- Yerel çalıştırma: `uvicorn app.main:app --reload`. Demo akışı README:2484-2500'de (`scripts/seed_demo.py`, `scripts/survey.py`).
- Railway:
  - `Dockerfile`: python:3.12-slim, imaja yalnız `app/` girer. `railway.json`: healthcheck `/health`, ON_FAILURE'da en çok 10
    deneme; `watchPatterns` yok.
  - Tek instance çalışmalı (Telegram polling bunu ister). `ROLE=census-worker` ise `python -m app.worker` çalışır.
- **Her push'u canlıya çıkış ve bot yeniden başlatması say.** Railway ayarı repoda yok, ama geçmiş bunu gösteriyor: 13.10'da
  `ecc2c49`/`fc4fe3d` push'undan (20:33 TSİ) sonraki deploy belleği sıfırladı, kullanıcı 20:39'da `/twap`'ı boş gördü (`b205924`).
  config.py da "dal Railway'e otomatik gider" diyor; bu yüzden yarım fazlar varsayılan KAPALI tutulur. Restart bellekteki sayaçları
  sıfırlar. `watchPatterns` olmadığı için (Railway arayüzünde ayrıca ayarlanmadıysa) yalnız README ya da belge push'u da yeniden
  deploy eder. Gereksiz push'ları birleştir; push'tan önce kullanıcıya "bot birkaç dakika yeniden başlayabilir" de, sonra
  "Telegram'da /saglik yazıp sonucu atar mısın?" diye doğrulat.
- Yeni özellik kablolama listesi:
  1. config (gerekirse `EDITABLE_FIELDS`) · 2. `notify.KINDS` · 3. main `_spawn` · 4. `health.limits`
  5. `/tani`'de özelliğin kendi satırı (diag); README bölümünde "Kör nokta, açıkça:" notu (README:262, 751, 839)
  6. format mesajı ve `COMMANDS`
  7. bot komutu (yeni kanalsa `_own_chats` da)
  8. README ve `.env.example`
  9. test: her özelliğin `test_wiring` testi kablolamayı sabitler (README ve `.env.example` metni, `KINDS`, `_spawn`,
     `health.limits`, `/tani` satırı; örnek tests/test_hiddentwap.py:539). Yeni özellikte de aynısını yaz.

  Yeni bir komut `COMMANDS`'a yazılmazsa `tests/test_komutlar.py` düşer. 28 test dosyası README'yi okuyor: README'de tek cümle
  değiştirmek bile test düşürebilir; README düzenlemesinden sonra tam koşu yap.

## Ortam kuralları (ayrıntı: HANDOFF §6)
- /tmp'de geniş silme yapma. 14.10'da `-mindepth 1` olmadan koşulan bir `find /tmp -maxdepth 1 … -exec rm -rf` /tmp'nin tamamını
  sildi (scratchpad ve commit imzalama yardımcısı `/tmp/code-sign` gitti). Silmeden önce aynı komutu `-print` ile koş, listeyi
  kullanıcıya göster, onay al.
- Commit "cannot exec '/tmp/code-sign'" hatasıyla düşerse yardımcıyı elle kurmaya, imzayı atlamaya ya da değişikliği GitHub API
  ile göndermeye **çalışma**; izin sistemi reddeder (kullanıcı sohbette izin verse bile). Ortam yardımcıyı kendisi yeniden kurar;
  bekle ya da yeni oturum aç, kullanıcıya sade anlat.
- Geçici dosyalar ortamın verdiği scratchpad'e yazılır. Testler artık /tmp'ye çöp bırakmıyor (`a47f0e4`). Disk dolarsa önce
  `df` / `du` ile ölç, sonra kullanıcıya açıkla.
- Paralel worktree açmadan önce `df -h /` (14.10'da disk 5 worktree'li iş sürerken doldu). `.claude/worktrees/` `.gitignore`'da
  yok: orada worktree varken `git add -A` kullanma, dosyaları adıyla ekle, commit'ten önce `git status`'a bak.
- Sohbet kayıtları (`~/.claude`) izin sistemince kapalı; erişmeyi deneme.

## Açık iş: ZZ1, kanal mesajlarında ilk 3-4 satır
- Plan dosyası: **`docs/handoff/kanal-mesaj-duzeni.md`** (`ca2f3bc`). 14.10'daki çok-ajanlı denetim 56 mesaj biçimine baktı;
  35'inin sırası değişmeli ki önemli bilgi ilk 3-4 satıra gelsin, 21'i zaten uygun. TWAP biçimleri `e49e472`'de yapıldı.
- Denetim `e49e472`'deki koda yapıldı ve `app/` o zamandan beri değişmedi (`a47f0e4` yalnız testlere dokundu). Yani plandaki
  dosya:satır atıfları `a47f0e4` / `ca2f3bc`'de geçerli. Yine de düzenlemeden önce kodu açıp doğrula.
- Plan her biçim için sorunları, önerilen ilk 4 satırı, değişiklik planını ve etkilenen testleri veriyor. Alanlar İngilizce.
- Önceki uygulama denemesi biçimleri 5 ayrı worktree grubuna bölmüştü. Yalnız disk olayı yüzünden durduruldu ve **hiçbir şey
  commit'lenmedi**; işe sıfırdan başla (`.claude/worktrees/` boş).
- Akış:
  1. Grubu uygula, `python tests/run.py` koş, her biçim için örnek mesaj yazdırıp ilk 4 satıra bak.
  2. Grubun kendi commit'ini at; her grup ayrı commit olsun ki tek tek geri alınabilsin.
  3. Bütün gruplar bitince çok-ajanlı incelemeyi yap, düzeltmeleri commit'le, tam testi koş, sonra tek seferde push et.
- Dikkat: `new_big`, `liqmap`, `cryptoliq`, `cryptovol`, `equityvol`, `liqattack` gibi türler `notify.PUBLIC_KINDS`'te; satılabilir
  bot açıksa (`PUBLIC_BOT_ENABLED`, varsayılan 0) aynı metin ücretli DM'ye de gider. Plan #20 ve #23 foto altyazısıdır; altyazı
  `CAPTION_MAX=1000` görünür karakteri (Telegram sınırı 1024) aşamaz.
- README:618 ve README:669 hâlâ kanal notunun "başta" olduğunu yazıyor; `e49e472`'den beri not altta. Bunu da düzelt.
