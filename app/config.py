"""Uygulama ayarları.

Öncelik sırası: dashboard /settings'te kaydedilen değer (DB'de yaşar)
> ortam değişkeni > kod varsayılanı. Gizli anahtarlar (token/key) sadece env'den.
"""
import os


def _csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


# Dashboard'dan canlı değiştirilebilen alanlar
EDITABLE_FIELDS: dict[str, dict] = {
    # ---- Bildirimler ----
    "notify_earnings": {"type": "bool", "label": "📊 Earnings raporu bildirimi", "group": "Bildirimler",
                        "desc": "Bilançoya ~1 saat kala balina raporu (1 = açık, 0 = kapalı)"},
    "notify_new_big": {"type": "bool", "label": "🆕 Yeni büyük pozisyon", "group": "Bildirimler",
                       "desc": "Eşik üstü yeni pozisyon açılınca anında haber"},
    "notify_liq": {"type": "bool", "label": "💥 Likidasyon radarı", "group": "Bildirimler",
                   "desc": "Dev pozisyonlar likidasyona yaklaşınca kademeli uyarı"},
    "notify_whale_fill": {"type": "bool", "label": "🐋 Büyük işlem / sicilli balina", "group": "Bildirimler",
                          "desc": "Canlı akışta eşik üstü işlem ya da watchlist adresi hareketi"},
    "notify_anomaly": {"type": "bool", "label": "📡 OI / funding anomalisi", "group": "Bildirimler",
                       "desc": "Pozisyon sahibi bilinmese de 'birileri birikiyor' alarmı"},
    "notify_eval": {"type": "bool", "label": "🏁 Earnings sonuç raporu", "group": "Bildirimler",
                    "desc": "Bilanço sonrası kim doğru bildi raporu"},
    "notify_digest": {"type": "bool", "label": "🌅 Günlük sabah özeti", "group": "Bildirimler",
                      "desc": "Sessiz saatte biriken bildirimler + günün gündemi"},
    "quiet_start_hour": {"type": "int", "label": "Sessiz saat başlangıcı (TSİ)", "group": "Bildirimler",
                         "desc": "Bu saatten sonra normal bildirimler beklemeye alınır (başlangıç=bitiş ise kapalı)"},
    "quiet_end_hour": {"type": "int", "label": "Sessiz saat bitişi (TSİ)", "group": "Bildirimler",
                       "desc": "Bu saatte sessizlik biter ve sabah özeti gönderilir"},
    "quiet_allow_high": {"type": "bool", "label": "Sessiz saatte önemli bildirimler geçsin", "group": "Bildirimler",
                         "desc": "1 = earnings/yeni büyük poz/likidasyon sessiz saatte de gelir"},
    "digest_hour": {"type": "int", "label": "Sabah özeti saati (TSİ)", "group": "Bildirimler",
                    "desc": "Günlük özetin gönderileceği saat"},
    "big_alert_index_usd": {"type": "float", "label": "Endeks/emtia bildirim tabanı ($)",
                            "group": "Bildirimler",
                            "desc": "XYZ100, SP500, GOLD, SILVER gibi endeks/emtia/FX/ETF'lerde 'yeni büyük pozisyon' bildirimi için gereken boyut. OI'leri devasa olduğu için hisse eşiği burada gürültü üretiyordu"},
    "big_alert_major_usd": {"type": "float", "label": "Büyük hisse bildirim tabanı ($)",
                            "group": "Bildirimler",
                            "desc": "Hacimce ilk N hissede (NVDA, TSLA…) bildirim için gereken boyut — likit hisselerde küçük poz sinyal değildir. Liste dinamiktir (Emir defteri radarı → 'Büyük sınıf hisse sayısı')"},
    "big_alert_min_usd": {"type": "float", "label": "Normal hisse bildirim tabanı ($)",
                          "group": "Bildirimler",
                          "desc": "Küçük/orta hisselerde (SNDK, CBRS…) bildirim tabanı. Burada $1M bile piyasanın büyük kısmı olabilir — asıl insider sinyali burada, düşük tutulur"},
    "alert_min_score": {"type": "int", "label": "Bildirim için min şüphe skoru", "group": "Bildirimler",
                        "desc": "Yeni büyük pozisyon bildirimi için gereken minimum skor (0 = hepsi)"},
    "notify_liqmap": {"type": "bool", "label": "🧲 Likidasyon duvarı (küme)", "group": "Bildirimler",
                      "desc": "Fiyata yakın bölgede TOPLAMDA büyük liq yığını birikince haber (cascade/stop avı mıknatısı)"},
    "notify_track": {"type": "bool", "label": "👣 Pozisyon kapanış takibi", "group": "Bildirimler",
                     "desc": "Earnings geçince 'takip edelim mi?' teklifi + takipteki balina pozunu kapadıkça haber"},
    "track_step_pct": {"type": "float", "label": "Takip bildirim adımı (%)", "group": "Bildirimler",
                       "desc": "Takipteki poz, toplam boyutun bu yüzdesi kadar değişmeden bildirim GELMEZ (spam önleyici)"},
    "track_auto_stop": {"type": "bool", "label": "Takip süreyle bitsin (eski davranış)",
                        "group": "Bildirimler",
                        "desc": "Açılırsa takip, süre dolunca poz açık olsa bile BİTER (eskiden böyleydi — balinanın çıkışı kaçıyordu). Kapalıyken takip yalnız pozisyon kapanınca ya da /birak_N ile biter"},
    "track_expire_days": {"type": "int", "label": "Takip yoklama aralığı (gün)", "group": "Bildirimler",
                          "desc": "Poz hâlâ açıkken kaç günde bir 'takipteyim, bırakayım mı?' densin. Takip bu süreyle BİTMEZ — yalnız pozisyon kapanınca ya da /birak_N ile biter"},
    "notify_lowvol": {"type": "bool", "label": "🐘 Sessiz su devi", "group": "Bildirimler",
                      "desc": "Düşük hacimli hissede absürt boyutlu YENİ pozisyon açılınca haber (eşik aşağıda ayrı ayarda)"},
    "notify_listing": {"type": "bool", "label": "🆕 Yeni hisse listelendi", "group": "Bildirimler",
                       "desc": "HL yeni bir hisse perp'i listelediğinde haber ver (ilk açılışta susar)"},
    "channel_auto_hours": {"type": "str", "label": "📣 Otomatik kanal yayını saatleri (TSİ)",
                           "group": "Bildirimler",
                           "desc": "Bu saatlerde 'saati gelenler' otomatik olarak yayın kanalına gönderilir. Virgülle: 10,14,17 · boş = kapalı (TELEGRAM_CHANNEL_ID gerekir)"},
    "notify_health": {"type": "bool", "label": "⚕️ Sistem sağlığı", "group": "Bildirimler",
                      "desc": "VARSAYILAN KAPALI — sağlık olayları zaten ana sayfa rozetinde + /saglik + /health'te görünür; Telegram'a da istersen aç"},
    "notify_cryptovol": {"type": "bool", "label": "🚀 Kripto hacim patlaması", "group": "Bildirimler",
                         "desc": "PROPR'da listeli bir kripto coin son 24 saatin en yüksek 5 dakikalık hacmine ulaşınca — ayrı kanala gider (CRYPTO_CHAT_ID)"},
    "notify_equityvol": {"type": "bool", "label": "📈 Hisse hacim patlaması", "group": "Bildirimler",
                         "desc": "PROPR'da listeli bir HİSSE perp'i son 24 saatin en yüksek 5 dakikalık hacmine ulaşınca — ayrı kanala gider (CRYPTO_STOCKS_ID)"},
    "notify_pattern": {"type": "bool", "label": "🔮 Örüntü sinyali", "group": "Bildirimler",
                       "desc": "Geçmiş şekil eşleşmesi taban orandan istatistiksel olarak ayrıştığında — ayrı kanala gider (PATTERN_CHAT_ID)"},
    "notify_offhours": {"type": "bool", "label": "🌙 Kapalı seans hareketi", "group": "Bildirimler",
                        "desc": "ABD kapalıyken (hafta sonu/gece) kapanış fiyatından sapan ya da ani sıçrayan hisseler — yalnız PROPR'da listeli olanlar"},
    "notify_wall": {"type": "bool", "label": "🧱 Emir defteri duvarı", "group": "Bildirimler",
                    "desc": "Deftere fiyatın hemen yanına konan dev bekleyen emir duvarları (ve çekilirse/dolarsa haberi)"},
    "wall_window_pct": {"type": "float", "label": "Duvar penceresi (%)", "group": "Emir defteri radarı",
                        "desc": "Orta fiyata bu kadar yakın bekleyen emirler duvara sayılır (SPCX örneği %0.3'teydi)"},
    "wall_min_usd": {"type": "float", "label": "Sitede gösterim tabanı ($)", "group": "Emir defteri radarı",
                     "desc": "Bu boyutun üstündeki duvarlar ana sayfada listelenir"},
    "wall_alert_min_usd": {"type": "float", "label": "Telegram alarm tabanı ($)", "group": "Emir defteri radarı",
                           "desc": "NORMAL hisselerde Telegram'a düşmesi için gereken duvar boyutu (endeks/top-10 için aşağıdaki taban geçerli)"},
    "wall_alert_big_min_usd": {"type": "float", "label": "Top-10 hisse / endeks alarm tabanı ($)", "group": "Emir defteri radarı",
                               "desc": "XYZ100, GOLD, BTC gibi endeksler ve en likit N hissede (NVDA vb.) alarm için bu boyut gerekir — defterleri zaten kalın"},
    "wall_big_top_n": {"type": "int", "label": "Büyük sınıf hisse sayısı", "group": "Emir defteri radarı",
                       "desc": "24h hacme göre ilk N hisse endeks muamelesi görür (dinamik — likidite değişince liste kendini günceller)"},
    "lowvol_max_day_volume": {"type": "float", "label": "Düşük hacim eşiği ($/gün)", "group": "Sessiz su radarı",
                              "desc": "Günlük hacmi bunun altındaki hisseler 'sessiz su' sayılır"},
    "lowvol_min_oi_share": {"type": "float", "label": "OI payı eşiği (%)", "group": "Sessiz su radarı",
                            "desc": "Pozisyon OI'nin bu yüzdesini tutuyorsa hacme bakılmaksızın listeye girer (tek başına piyasanın yarısı vb.)"},
    "lowvol_min_notional": {"type": "float", "label": "Listeye giriş tabanı ($)", "group": "Sessiz su radarı",
                            "desc": "Sekmede gösterilecek en küçük pozisyon — toz görünmesin"},
    "lowvol_alert_min_usd": {"type": "float", "label": "Telegram alarm tabanı ($)", "group": "Sessiz su radarı",
                             "desc": "Telegram'a SADECE gerçekten absürt boyutlar düşer — bunun altı sitede görünür ama bildirim üretmez"},
    # ---- AI analist ----
    "ai_enabled": {"type": "bool", "label": "🤖 AI analist", "group": "AI analist",
                   "desc": "Veriden hipotez üretip Python'a sınatan arka plan analisti. AI_API_KEY (env) girilmeden çalışmaz. Çıktı YALNIZ sitede görünür, Telegram'a hiçbir şey düşmez"},
    "ai_interval_sec": {"type": "int", "label": "Tur aralığı (sn)", "group": "AI analist",
                        "desc": "Kaç saniyede bir brifing hazırlanıp modele gönderilsin. Groq bedava katmanında günde 100K token var: 7200 (2 saat) ~48K/gün eder, rahat pay bırakır"},
    "ai_daily_token_cap": {"type": "int", "label": "Günlük token tavanı", "group": "AI analist",
                           "desc": "Bir günde harcanabilecek toplam token. Tavan dolunca çağrı HİÇ yapılmaz, ertesi gün sıfırlanır. Sağlayıcının sınırının altında tut (Groq bedava: gpt-oss-120b için 200K/gün)"},
    "ai_max_hypotheses": {"type": "int", "label": "Tur başına hipotez", "group": "AI analist",
                          "desc": "Model her turda en fazla kaç hipotez üretsin. Az tutmak modeli EN İYİ tahminini seçmeye zorlar ve sicili anlamlı kılar"},
    "ai_model": {"type": "str", "label": "Model adı", "group": "AI analist",
                 "desc": "Sağlayıcıdaki model kimliği (ör. openai/gpt-oss-120b). Model adları sağlayıcıda değişir; yanlış ad girersen /ai sayfasındaki hata metni kullanılabilir modelleri listeler"},
    "ai_base_url": {"type": "str", "label": "API adresi", "group": "AI analist",
                    "desc": "OpenAI-uyumlu sohbet tamamlama adresi. Groq/Cerebras/OpenRouter/DeepSeek aynı biçimi konuşur — sağlayıcı değiştirmek için burayı ve model adını değiştir"},
    "min_fill_notional": {"type": "float", "label": "Min fill boyutu ($)",
                          "group": "Skorlama eşikleri", "desc": "Bu boyut üstü işlemler adres havuzuna yazılır"},
    "whale_alert_notional": {"type": "float", "label": "Anlık balina alert eşiği ($)",
                             "group": "Skorlama eşikleri", "desc": "Bu boyut üstü tek işlemde hemen Telegram alert"},
    "min_position_notional": {"type": "float", "label": "Min pozisyon boyutu ($)",
                              "group": "Skorlama eşikleri", "desc": "Bundan küçük pozisyonlar listelenmez (toz filtresi)"},
    "big_position_usd": {"type": "float", "label": "Büyük pozisyon eşiği ($)",
                         "group": "Skorlama eşikleri", "desc": "Bu boyut üstü pozisyona +10 şüphe puanı"},
    "huge_position_usd": {"type": "float", "label": "Dev pozisyon eşiği ($)",
                          "group": "Skorlama eşikleri", "desc": "Bu boyut üstü pozisyona +20 şüphe puanı"},
    "combo_window_hours": {"type": "int", "label": "İnsider paterni penceresi (saat)",
                           "group": "Skorlama eşikleri", "desc": "Büyük pozisyon + bu pencerede açılış = +15 bonus (varsayılan 72h = 3 gün)"},
    "fresh_big_alert_hours": {"type": "int", "label": "Yeni büyük poz alert penceresi (saat)",
                              "group": "Skorlama eşikleri", "desc": "Bu pencerede açılmış büyük pozisyon bulununca anlık Telegram alert (earnings şartı yok)"},
    "mm_max_positions": {"type": "int", "label": "MM eşiği: açık pozisyon sayısı",
                         "group": "Skorlama eşikleri", "desc": "Bu kadar+ açık pozisyonu olan hesap market maker sayılır (skorlama/alert dışı)"},
    "mm_max_fills_24h": {"type": "int", "label": "MM eşiği: 24h fill sayısı",
                         "group": "Skorlama eşikleri", "desc": "24 saatte bu kadar+ büyük fill yapan çift yönlü hesap MM sayılır"},
    "liq_watch_min_notional": {"type": "float", "label": "Liq radarı: min pozisyon ($)",
                               "group": "Likidasyon radarı", "desc": "Bu boyut üstü pozisyonlar TÜM dex'lerde likidasyon radarına girer"},
    "liq_watch_poll_sec": {"type": "int", "label": "Liq radarı periyodu (sn)",
                           "group": "Likidasyon radarı", "desc": "Likidasyon mesafesi kontrol sıklığı (kademeler: %1 → %0.5 → %0.1)"},
    "liq_watch_top_accounts": {"type": "int", "label": "Liq radarı: taranan hesap",
                               "group": "Likidasyon radarı", "desc": "Leaderboard'dan likidasyon radarına alınan hesap sayısı"},
    "max_liq_distance_pct": {"type": "float", "label": "Liq tablosu mesafe sınırı (%)",
                             "group": "Likidasyon radarı", "desc": "Likidasyonu bundan uzak pozisyonlar liq tablosuna girmez"},
    "liq_cluster_window_pct": {"type": "float", "label": "Duvar penceresi (%)",
                               "group": "Likidasyon radarı", "desc": "Fiyatın bu kadar yakınındaki liq'ler 'duvar' sayılır (tweet'teki heatmap mantığı)"},
    "liq_cluster_min_usd": {"type": "float", "label": "Duvar eşiği — SAYFA ($)",
                            "group": "Likidasyon radarı", "desc": "Pencere içi toplam liq bu boyutu aşarsa duvar SAYILIR ve ana sayfadaki likidasyon haritasında görünür. Bildirim için ayrı (daha yüksek) eşik var"},
    "liq_cluster_alert_min_usd": {"type": "float", "label": "Duvar eşiği — BİLDİRİM ($)",
                                  "group": "Likidasyon radarı", "desc": "NORMAL hisselerde Telegram bildirimi için gereken toplam. Sayfa eşiğinden yüksek tutulur: sayfada bağlam olan küçük duvar, bildirimde gürültüdür"},
    "liq_cluster_big_min_usd": {"type": "float", "label": "Endeks/FX/top-10 duvar eşiği ($)",
                                "group": "Likidasyon radarı", "desc": "JPY, GOLD, XYZ100 gibi likit varlıklarda liq duvarı alarmı için gereken TOPLAM — küçük kümeler oralarda gürültü"},
    "fresh_wallet_days": {"type": "int", "label": "Taze cüzdan eşiği (gün)",
                          "group": "Skorlama eşikleri", "desc": "İlk fonlaması bundan yeni hesaplar 'taze' sayılır (+25 puan)"},
    "recent_deposit_hours": {"type": "int", "label": "Yeni fonlama eşiği (saat)",
                             "group": "Skorlama eşikleri", "desc": "Son fonlaması bundan yeni hesaplar şüpheli (+12 puan)"},
    "eval_move_threshold": {"type": "float", "label": "Sicil için min hareket (%)",
                            "group": "Skorlama eşikleri", "desc": "Earnings sonrası bu kadar hareket yoksa doğru/yanlış işlenmez"},
    "eval_min_notional": {"type": "float", "label": "Sicil için min pozisyon ($)",
                          "group": "Skorlama eşikleri", "desc": "Bundan küçük pozisyonlar sicile/watchlist'e girmez (küçük 'tutturdu' insider sayılmaz)"},
    "leaderboard_top": {"type": "int", "label": "Leaderboard tohumu (adres)",
                        "group": "Tarama & performans", "desc": "Havuza eklenen en büyük hesap sayısı"},
    "scan_max_candidates": {"type": "int", "label": "Tarama aday limiti",
                            "group": "Tarama & performans", "desc": "T-1h taramasında sorgulanacak maksimum adres"},
    "scan_concurrency": {"type": "int", "label": "Eşzamanlı API isteği",
                         "group": "Tarama & performans", "desc": "HL API paralellik (rate limit'e dikkat)"},
    "equity_dexes": {"type": "csv", "label": "Hisse dex'leri",
                     "group": "Takvim & semboller", "desc": "HIP-3 hisse perp dex'leri, virgülle (ör: xyz)"},
    "calendar_horizon_days": {"type": "int", "label": "Takvim ufku (gün)",
                              "group": "Takvim & semboller", "desc": "Kaç gün ilerisinin earnings'leri çekilsin"},
    "metrics_poll_sec": {"type": "int", "label": "Metrik periyodu (sn)",
                         "group": "Tarama & performans", "desc": "OI/funding örnekleme sıklığı"},
    "anomaly_poll_sec": {"type": "int", "label": "Anomali kontrol periyodu (sn)",
                         "group": "Anomali dedektörü", "desc": "OI/funding anomali taraması sıklığı"},
    "oi_spike_pct_event": {"type": "float", "label": "OI spike eşiği - earnings yakın (%)",
                           "group": "Anomali dedektörü", "desc": "Earnings <72h iken 24h OI artışı alarmı"},
    "oi_spike_pct_normal": {"type": "float", "label": "OI spike eşiği - normal (%)",
                            "group": "Anomali dedektörü", "desc": "Earnings yokken 24h OI artışı alarmı"},
    "oi_spike_floor_usd": {"type": "float", "label": "OI spike tabanı ($)",
                           "group": "Anomali dedektörü", "desc": "HİSSELERDE bu OI'nin altındaki mikro marketlerde alarm verme"},
    "oi_spike_big_floor_usd": {"type": "float", "label": "OI spike tabanı — endeks/FX ($)",
                               "group": "Anomali dedektörü", "desc": "GBP, GOLD, XYZ100 gibi FX/endeks/emtia/kripto'da OI bu boyutun altındaysa spike alarmı verme (mikro marketten %175 artış anlamsız)"},
    "vol_spike_mult": {"type": "float", "label": "Hacim patlaması katsayısı (×)",
                       "group": "Anomali dedektörü",
                       "desc": "24 saatlik hacim bir gün öncesine göre bu KAT'a çıkarsa alarm (fiyat kıpırdamadan hacmin patlaması = sessiz birikim)"},
    "vol_spike_min_usd": {"type": "float", "label": "Hacim alarmı tabanı ($)",
                          "group": "Anomali dedektörü",
                          "desc": "Bu günlük hacmin altındaki marketlerde hacim patlaması alarm üretmez (mikro hacimde 5x anlamsız)"},
    "funding_extreme": {"type": "float", "label": "Aşırı funding eşiği (saatlik)",
                        "group": "Anomali dedektörü", "desc": "ör: 0.0005 = %0.05/saat"},
    "peers_override": {"type": "str", "label": "Korele hisse override",
                       "group": "Takvim & semboller", "desc": "Format: SNDK:WDC|MU;TSLA:RIVN (varsayılan tabloya eklenir)"},
    "yahoo_symbol_map": {"type": "str", "label": "Yahoo sembol eşleme",
                         "group": "Takvim & semboller", "desc": "ABD dışı hisseler için: SMSN:005930.KS;SOFTBANK:9984.T formatı (varsayılan eşlemeye eklenir)"},
    "exclude_symbols": {"type": "str", "label": "🚫 Takipten çıkarılan hisseler",
                        "group": "Takvim & semboller",
                        "desc": "Bu semboller evrene alınmaz, taranmaz, takvimi aranmaz — tamamen görmezden gelinir. Virgülle: BIRD,XYZ"},
    "non_equity_extra": {"type": "str", "label": "Bilançosuz enstrümanlar (ek)",
                         "group": "Takvim & semboller", "desc": "Endeks/emtia/FX/ETF/kripto — takvim aranmaz. Virgülle: GOLD,EUR"},
    "no_calendar_extra": {"type": "str", "label": "Takvimi olmayan hisseler (ek)",
                          "group": "Takvim & semboller", "desc": "Pre-IPO / sentetik hisseler — takvim aranmaz, elle /settime ile girilir"},
    "propr_symbols": {"type": "str", "label": "propr.xyz ek semboller",
                      "group": "Takvim & semboller", "desc": "propr yeni bir şey listelerse buraya virgülle ekle — alertlere '✅ PROPR'da listeli' düşer"},
    "universe_refresh_sec": {"type": "int", "label": "Evren yenileme (sn)",
                             "group": "Tarama & performans", "desc": "HL coin listesi yenileme sıklığı"},
    "calendar_refresh_sec": {"type": "int", "label": "Takvim yenileme (sn)",
                             "group": "Takvim & semboller", "desc": "Earnings takvimi çekme sıklığı"},
    "auto_scan_interval_sec": {"type": "int", "label": "Oto-tarama periyodu (sn)",
                               "group": "Tarama & performans", "desc": "Arka plan tarayıcısı bu aralıkla sıradaki coini tarar"},
    "scan_stale_min": {"type": "int", "label": "Sayfa bayatlık eşiği (dk)",
                       "group": "Tarama & performans", "desc": "Coin sayfası açıldığında veri bundan eskiyse otomatik tarama başlar"},
    "track_poll_sec": {"type": "int", "label": "Takip kontrol periyodu (sn)",
                       "group": "Tarama & performans", "desc": "Takipteki balina pozlarının kontrol sıklığı"},
    "wall_poll_sec": {"type": "int", "label": "Defter tarama periyodu (sn)",
                      "group": "Tarama & performans", "desc": "Emir defteri duvar radarının tarama sıklığı"},
    "hl_big_min_usd": {"type": "float", "label": "HL en büyükler eşiği ($)",
                       "group": "Tarama & performans",
                       "desc": "Tüm Hyperliquid pozisyonları bu boyutun üstündeyse kaydedilir (/devler'deki 'Hyperliquid'in en büyükleri' paneli). Düşürürsen tablo hızlı büyür"},
    "probe_min_notional": {"type": "float", "label": "Anlık sonda eşiği ($)",
                           "group": "Tarama & performans",
                           "desc": "Canlı akışta bu boyutu aşan işlem görülünce o adresin TÜM defteri hemen çekilir (süpürücünün sırasını beklemeden). 0 = kapalı"},
    "probe_cooldown_sec": {"type": "int", "label": "Anlık sonda bekleme (sn)",
                           "group": "Tarama & performans",
                           "desc": "Aynı adres için iki sonda arası en az bu kadar süre — sürekli işlem yapan bir hesap REST'i boğmasın"},
    "sweep_catchup": {"type": "bool", "label": "Yetişme modu (boş bütçeyi kullan)",
                      "group": "Tarama & performans",
                      "desc": "Derin keşif, diğer görevlerden ARTAN istek bütçesini kullanarak parti boyunu kendisi büyütsün. Kapalıysa hep sabit parti boyu taranır (havuz 17K adresken ilk tam tur saatler sürer)"},
    "sweep_batch_max": {"type": "int", "label": "Yetişme: parti tavanı (adres)",
                        "group": "Tarama & performans",
                        "desc": "Yetişme modunda bir partide en fazla kaç adres taransın. Tavan olmasa tek parti diğer görevleri aç bırakabilir"},
    "sweep_rpm_headroom": {"type": "float", "label": "Yetişme: bütçe tavanı (oran)",
                           "group": "Tarama & performans",
                           "desc": "Küresel istek bütçesinin en fazla bu kadarı doldurulsun (0.85 = %85, kalanı ani işler için pay). Yükseltmek turu hızlandırır ama diğer görevleri geciktirir"},
    "hl_prime_top": {"type": "int", "label": "HL en büyükler: ön tarama hesabı",
                     "group": "Tarama & performans",
                     "desc": "Açılışta (ve günde bir) leaderboard'ın ilk kaç hesabı öncelikli taransın — panel hemen ve EN BÜYÜKTEN dolsun. 0 = kapalı, normal rotasyonu bekle"},
    "hl_crypto_min_usd": {"type": "float", "label": "Kripto eşiği ($)",
                          "group": "Tarama & performans",
                          "desc": "Ana dex'te (BTC/ETH HARİÇ) bu boyutun altındaki pozisyonlar kaydedilmez. Kriptoda $1M gürültüdür, hisse tarafındaki eşik burada işe yaramaz"},
    "hl_major_min_usd": {"type": "float", "label": "BTC/ETH eşiği ($)",
                         "group": "Tarama & performans",
                         "desc": "BTC ve ETH'te bu boyutun altındaki pozisyonlar kaydedilmez — en likit iki markette çıta daha yüksek"},
    "crypto_watch_top": {"type": "int", "label": "Canlı dinlenen kripto coin",
                         "group": "Tarama & performans",
                         "desc": "Ana dex'te 24h hacme göre ilk kaç coin canlı dinlensin (büyük işlem → adresin defterine anında bak). Bu işlemler fills'e YAZILMAZ, yalnız sonda tetikler. 0 = kripto dinleme kapalı"},
    "probe_min_notional_crypto": {"type": "float", "label": "Kripto sonda eşiği ($)",
                                  "group": "Tarama & performans",
                                  "desc": "Ana dex işlemlerinde sondayı tetikleyen boyut. Hisse eşiğinden yüksektir: BTC'de $100K'lık işlem gürültü, ama $2M+ süpüren birinin dev pozisyon taşıması makul"},
    "hl_records_keep": {"type": "int", "label": "HL rekor arşivi (satır)",
                        "group": "Tarama & performans",
                        "desc": "Kapanmış pozisyonlardan kaç tanesi 'gördüğümüz en büyükler' arşivinde tutulsun (zirveye göre ilk N)"},
    "pricechart_days": {"type": "int", "label": "Fiyat grafiği penceresi (gün)",
                        "group": "Tarama & performans",
                        "desc": "Coin sayfasındaki mum grafiğinde kaç günlük 1h mum gösterilsin"},
    "show_tradingview": {"type": "bool", "label": "📈 TradingView gömülüsü",
                         "group": "Tarama & performans",
                         "desc": "Coin sayfasında katlanabilir TradingView grafiği (HİSSE senedi, perp değil). Sitenin TEK üçüncü taraf isteğidir — yalnız sen paneli açarsan yüklenir. Kapatırsan panel hiç basılmaz"},
    "tv_symbol_map": {"type": "str", "label": "TradingView sembol eşlemesi",
                      "group": "Takvim & semboller",
                      "desc": "HL sembolü → TradingView sembolü, ör: 'SMSN:KRX:005930;X:NYSE:X'. Bir sembolde gömülüyü kapatmak için karşılığını boş bırak (ör: 'CXMT:')"},
    "crypto_vol_enabled": {"type": "bool", "label": "Kripto hacim radarı", "group": "Kripto hacim",
                           "desc": "Kapatılırsa hiç mum çekilmez, istek maliyeti sıfırlanır"},
    "crypto_vol_poll_sec": {"type": "int", "label": "Tarama aralığı (sn)", "group": "Kripto hacim",
                            "desc": "5 dakikalık kova için doğal ritim 300 sn — kısaltmak aynı kovayı tekrar taramak olur"},
    "crypto_vol_min_usd": {"type": "float", "label": "Asgari 5dk hacim — SAYFA ($)", "group": "Kripto hacim",
                           "desc": "Bu tutarın üstündeki her rekor /hacim sayfasına yazılır. Düşük tutulur: sayfa dolu olsun ki bildirim eşiğini gerçek rakamlara bakarak ayarlayabilesin"},
    "crypto_vol_alert_min_usd": {"type": "float", "label": "Asgari 5dk hacim — BİLDİRİM ($)", "group": "Kripto hacim",
                                 "desc": "Telegram'a düşmesi için gereken tutar. Sayfa eşiğinden yüksek: sayfada bağlam olan küçük rekor, kanalda gürültüdür"},
    "crypto_vol_cooldown": {"type": "int", "label": "Coin başına bekleme (sn)", "group": "Kripto hacim",
                            "desc": "Uzun bir yükselişte her yeni kova yeni bir 24s rekoru olabilir; hepsi bildirilmesin"},
    "crypto_vol_max_coins": {"type": "int", "label": "Evren tavanı (coin)", "group": "Kripto hacim",
                             "desc": "Kaç coin taranacak (PROPR ∩ ana dex, hacimce büyükten). Her coin turda 1 istek eder"},
    "equity_vol_enabled": {"type": "bool", "label": "Hisse hacim radarı", "group": "Hisse hacim",
                           "desc": "Kapatılırsa hiç mum çekilmez, istek maliyeti sıfırlanır"},
    "equity_vol_poll_sec": {"type": "int", "label": "Tarama aralığı (sn)", "group": "Hisse hacim",
                            "desc": "5 dakikalık kova için doğal ritim 300 sn — kısaltmak aynı kovayı tekrar taramak olur"},
    "equity_vol_min_usd": {"type": "float", "label": "Asgari 5dk hacim — SAYFA ($)", "group": "Hisse hacim",
                           "desc": "Bu tutarın üstündeki her rekor /hacim sayfasına yazılır. Kriptodakinden DÜŞÜK: hisse perp'leri çok daha ince — SHEIN'in 24 saatlik TOPLAM hacmi $4.2M'ken patlama mumu ~$150K'ydı"},
    "equity_vol_alert_min_usd": {"type": "float", "label": "Asgari 5dk hacim — BİLDİRİM ($)", "group": "Hisse hacim",
                                 "desc": "Telegram'a düşmesi için gereken tutar. Sayfa eşiğinden yüksek tutulur"},
    "equity_vol_cooldown": {"type": "int", "label": "Hisse başına bekleme (sn)", "group": "Hisse hacim",
                            "desc": "Uzun bir hareket boyunca her yeni kova yeni bir 24s rekoru olabilir; hepsi bildirilmesin"},
    "equity_vol_max_coins": {"type": "int", "label": "Evren tavanı (hisse)", "group": "Hisse hacim",
                             "desc": "Kaç hisse taranacak (PROPR ∩ xyz dex, hacimce büyükten). Her hisse turda 1 istek eder"},
    "twap_min_usd": {"type": "float", "label": "TWAP asgari büyüklük ($)",
                     "group": "Tarama & performans",
                     "desc": "Bu tutarın üstündeki düzenli birikimler /twap sekmesinde listelenir. Tespit KENDİ kayıtlarımızdan yapılır: yakalama tabanının altındaki dilimler görünmez"},
    "twap_window_h": {"type": "int", "label": "TWAP tarama penceresi (saat)",
                      "group": "Tarama & performans",
                      "desc": "Kaç saat geriye bakılıp dilimler birleştirilsin. Uzun TWAP'lar için büyük, gürültü için küçük tutulur"},
    "twap_scan_sec": {"type": "int", "label": "TWAP tarama aralığı (sn)",
                      "group": "Tarama & performans",
                      "desc": "Tarama tamamen YEREL (fills tablosu) — API maliyeti yoktur"},
    "crypto_fill_min_notional": {"type": "float", "label": "Kripto işlem kaydı tabanı ($)",
                                 "group": "Tarama & performans",
                                 "desc": "İzlenen kripto coinlerde bu tutarın üstündeki işlemler adresiyle KAYDEDİLİR ('ne oldu' ve '/twap' sekmeleri bunu okur). Hisse tabanıyla EŞİT tutuldu: sabırlı bir TWAP'ın dilimleri küçüktür, yüksek eşik onları tamamen görünmez yapar. 0 = kripto kaydı kapalı. Bu işlemler Telegram'a DÜŞMEZ, yalnız arşivlenir"},
    "crypto_metrics_enabled": {"type": "bool", "label": "Kripto OI/funding kaydı",
                               "group": "Tarama & performans",
                               "desc": "Ana dex metrikleri de örneklenir (poll başına +1 istek, yalnız PROPR'daki coinler saklanır). OI olmadan 'long mu kapandı short mu açıldı' ayrımı YAPILAMAZ"},
    "forensics_probe_max": {"type": "int", "label": "'Ne oldu' canlı profil tavanı",
                            "group": "Tarama & performans",
                            "desc": "'Profilleri tazele' düğmesi en fazla bu kadar adresin defterini anında çeker (adres başına 1 istek). Pozisyon verisi derin keşif turuna bağlı olduğu için 2 saate kadar bayat olabilir; bu buton onu düzeltir"},
    "pattern_enabled": {"type": "bool", "label": "Örüntü bulucu", "group": "Örüntü bulucu",
                        "desc": "Kapatılırsa tarama durur; mum arşivi ayrı ayarla yönetilir"},
    "pattern_pool": {"type": "str", "label": "Eşleştirme havuzu", "group": "Örüntü bulucu",
                     "desc": "self = sembol yalnız KENDİ geçmişiyle eşleşir (saf yorum, küçük örneklem — çoğu sembolde 'yeterli veri yok' çıkar) · class = aynı sınıfın (hisse/kripto) tüm geçmişi, örneklem ~80× büyür"},
    "pattern_win_1h": {"type": "int", "label": "Pencere — 1h (bar)", "group": "Örüntü bulucu",
                       "desc": "Şekli kaç barlık parçadan okuyalım. 24 = bir gün"},
    "pattern_win_15m": {"type": "int", "label": "Pencere — 15m (bar)", "group": "Örüntü bulucu",
                        "desc": "15 dakikalık dilimde şeklin uzunluğu. 32 = 8 saat"},
    "pattern_horizons": {"type": "str", "label": "Vadeler (bar)", "group": "Örüntü bulucu",
                         "desc": "Eşleşmeden SONRA kaç bar ileriye bakılsın; virgülle. Her vade ayrı sinyal üretir"},
    "pattern_top_k": {"type": "int", "label": "En fazla eşleşme", "group": "Örüntü bulucu",
                      "desc": "Bir sorguda alınacak azami komşu. Ayrıca mevcut BAĞIMSIZ pencerelerin %15'iyle de sınırlanır: dar havuzda 'en yakın 50' benzerlik değil ortalamanın kendisi olur"},
    "pattern_min_corr": {"type": "float", "label": "Asgari şekil benzerliği", "group": "Örüntü bulucu",
                         "desc": "İki şeklin korelasyonu. 1 = birebir aynı, 0 = alakasız. Sıralamayla 'en yakın 50'yi almak yetmez, gerçekten benzemeli. ÖLÇÜM: 4300 pencerelik bir geçmişte ulaşılabilen en iyi benzerlik ~0.65; 0.5 üstü ~35 pencere çıkıyor"},
    "pattern_min_matches": {"type": "int", "label": "Asgari eşleşme sayısı", "group": "Örüntü bulucu",
                            "desc": "Bundan az bağımsız örnekle olasılık ÜRETİLMEZ; 'yeterli benzer örnek yok' denir. Uydurma güven yerine dürüst boşluk"},
    "pattern_z_alert": {"type": "float", "label": "Bildirim için z eşiği", "group": "Örüntü bulucu",
                        "desc": "Sinyalin taban orandan kaç standart hata uzakta olması gerektiği. 2 ≈ %95; altındakiler sayfada 'zayıf' diye durur"},
    "pattern_edge_min": {"type": "float", "label": "Bildirim için asgari fark (puan)", "group": "Örüntü bulucu",
                         "desc": "Olasılık taban orandan en az bu kadar puan ayrılmalı. z tek başına yeterli değil: çok büyük n'de anlamsız küçük farklar da 'anlamlı' çıkar"},
    "pattern_scan_sec": {"type": "int", "label": "Tarama aralığı (sn)", "group": "Örüntü bulucu",
                         "desc": "Tüm evrenin taranma periyodu. Hesap yereldir (numpy), API maliyeti yoktur"},
    "bars_1h_days": {"type": "int", "label": "1h arşiv derinliği (gün)", "group": "Mum arşivi",
                     "desc": "Eşleştirmenin baktığı geçmiş. Kısaltmak örneklemi küçültür, olasılıkları güvenilmez yapar"},
    "bars_15m_days": {"type": "int", "label": "15m arşiv derinliği (gün)", "group": "Mum arşivi",
                      "desc": "15 dakikalık dilimde geçmiş. Bar sayısı 1h'in 4 katı olduğu için disk buradan büyür"},
    "bars_refresh_sec": {"type": "int", "label": "Arşiv tazeleme (sn)", "group": "Mum arşivi",
                         "desc": "Her turda tüm semboller iki dilimde güncellenir; sembol başına 1 istek (~12 rpm)"},
    "offhours_close_hour": {"type": "int", "label": "ABD kapanış saati (TSİ)",
                            "group": "Kapalı seans",
                            "desc": "Kapalı seans sapmasının çıpası — TSİ'de sabit saat (0 = 24:00, Cuma gece yarısı). Kışın ET 16:00 kapanışına denk gelir, yazın 1 saat kayar"},
    "offhours_alert_weekend_only": {"type": "bool", "label": "Bildirim yalnız hafta sonu",
                                    "group": "Kapalı seans",
                                    "desc": "Açıkken bildirim SADECE hafta sonu penceresinde gelir (Cuma 24:00 → Pzt 00:00 TSİ). Kapatılırsa hafta içi geceler de dahil olur — ABD 00:00–16:30 TSİ arası kapalı olduğu için günde ~16 saat bildirim demektir"},
    "offhours_alert_pct": {"type": "float", "label": "Sapma bildirim bandı (%)",
                           "group": "Kapalı seans",
                           "desc": "Kapanış çıpasından bu kadar sapınca haber gelir; sonra her yeni bantta bir kez daha (%0.5 → %1.0 → %1.5…)"},
    "offhours_spike_pct": {"type": "float", "label": "Ani hareket eşiği (%)",
                           "group": "Kapalı seans",
                           "desc": "Kısa pencerede bu kadar hareket = ayrı bildirim. Yavaş biriken sapmadan farklı bir olay"},
    "offhours_spike_weekend_only": {"type": "bool", "label": "Ani hareket yalnız hafta sonu",
                                    "group": "Kapalı seans",
                                    "desc": "VARSAYILAN KAPALI: ani hareket ABD kapalı HER saatte çalışır (SHEIN Pazartesi sabahı %12 düştü ve susmuştuk). Kümülatif bantlar bundan bağımsız, onlar hafta sonuna özel"},
    "offhours_spike_pct_weekday": {"type": "float", "label": "Ani hareket eşiği — HAFTA İÇİ (%)",
                                   "group": "Kapalı seans",
                                   "desc": "Hafta içi kapalı pencere 16.5 saat ve pre-market'te %1 sıradan; bu yüzden hafta sonundan yüksek tutulur"},
    "offhours_spike_min": {"type": "int", "label": "Ani hareket penceresi (dk)",
                           "group": "Kapalı seans",
                           "desc": "Ani hareket kaç dakikalık pencerede ölçülsün"},
    "offhours_spike_cooldown": {"type": "int", "label": "Ani hareket beklemesi (sn)",
                                "group": "Kapalı seans",
                                "desc": "Aynı hisse için iki ani hareket bildirimi arasındaki asgari süre"},
    "metrics_poll_closed_sec": {"type": "int", "label": "Kapalıyken metrik örnekleme (sn)",
                                "group": "Kapalı seans",
                                "desc": "ABD kapalıyken fiyat ne sıklıkta örneklensin — ani hareket tetiğinin çözünürlüğü budur (tek dex için poll = 1 istek)"},
    "hourstats_days": {"type": "int", "label": "Saat istatistiği penceresi (gün)",
                       "group": "Tarama & performans",
                       "desc": "Saatlik getiri haritası için geriye bakılacak gün sayısı (1h mumlar)"},
    "zombie_silent_hours": {"type": "int", "label": "Sessiz coin uyarı eşiği (saat)",
                            "group": "Tarama & performans",
                            "desc": "Canlı akıştan bu kadar saattir hiç işlem gelmeyen coin'ler /saglik raporunda 'sessiz' olarak listelenir (abone olunduğu sanılan ama veri gelmeyen marketler)"},
    "fills_retention_days": {"type": "int", "label": "Fill kayıt ömrü (gün)",
                             "group": "Tarama & performans",
                             "desc": "Bu kadar günden eski işlem kayıtları her gece silinir (disk + hız). Zaman çizelgesi/uzman analizi en fazla bu kadar geriyi görür"},
    "hl_max_rpm": {"type": "int", "label": "HL API istek bütçesi (istek/dk)",
                   "group": "Tarama & performans",
                   "desc": "Tüm görevlerin paylaştığı toplam tavan — aşınca istekler kuyruklanır, böylece rate-limit cezası yenmez"},
    "sweep_leaderboard_top": {"type": "int", "label": "Derin keşif: leaderboard hesap sayısı",
                              "group": "Tarama & performans",
                              "desc": "Bu kadar en büyük hesabın TÜM pozisyonları sürekli süpürülür — bot kurulmadan önce açılmış uyuyan dev pozlar böyle bulunur"},
    "sweep_batch_size": {"type": "int", "label": "Derin keşif: tur başına adres",
                         "group": "Tarama & performans", "desc": "Her turda bu kadar adresin tüm pozisyonları sorgulanır"},
    "sweep_interval_sec": {"type": "int", "label": "Derin keşif: tur aralığı (sn)",
                           "group": "Tarama & performans", "desc": "Süpürme turları arası bekleme — sıcak havuz (~1500 adres) varsayılanla ~75-80 dakikada bir tam tur döner"},
}


def convert_value(typ: str, v):
    if typ == "bool":
        return str(v).strip().lower() in ("1", "true", "evet", "on", "açık", "yes")
    if typ == "int":
        return int(float(str(v).replace(",", ".")))
    if typ == "float":
        return float(str(v).replace(",", "."))
    if typ == "csv":
        return _csv(str(v))
    return str(v)


def display_value(typ: str, v) -> str:
    if typ == "bool":
        return "1" if v else "0"
    if typ == "csv" and isinstance(v, list):
        return ",".join(v)
    if typ == "float":
        try:
            f = float(v)
            return str(int(f)) if f == int(f) else str(f)
        except (TypeError, ValueError):
            return str(v)
    return str(v)


class Config:
    def __init__(self) -> None:
        self.version = "0.1.0"

        # Telegram
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        # AI analist. Anahtar GİZLİ: yalnız env'den okunur, ayar sayfasında
        # görünmez/düzenlenmez (TELEGRAM_BOT_TOKEN ile aynı kural).
        self.ai_api_key = os.getenv("AI_API_KEY", "")
        self.ai_enabled = convert_value("bool", os.getenv("AI_ENABLED", "0"))
        self.ai_interval_sec = int(os.getenv("AI_INTERVAL_SEC", "7200"))
        self.ai_daily_token_cap = int(os.getenv("AI_DAILY_TOKEN_CAP", "180000"))
        self.ai_max_hypotheses = int(os.getenv("AI_MAX_HYPOTHESES", "3"))
        # Groq eski llama'ları 06/2026'da emekliye ayırdı; model adları
        # sağlayıcıda döner. Yanlış ad girilirse hata metni kullanılabilir
        # listeyi de yazar (bkz. ai/client.py list_models).
        self.ai_model = os.getenv("AI_MODEL", "openai/gpt-oss-120b")
        self.ai_base_url = os.getenv(
            "AI_BASE_URL", "https://api.groq.com/openai/v1/chat/completions")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

        # Bildirim tercihleri (hepsi /settings'ten canlı değişir)
        self.notify_earnings = True
        self.notify_new_big = True
        self.notify_liq = True
        self.notify_whale_fill = True
        self.notify_anomaly = True
        self.notify_eval = True
        self.notify_digest = True
        self.quiet_start_hour = int(os.getenv("QUIET_START_HOUR", "1"))
        self.quiet_end_hour = int(os.getenv("QUIET_END_HOUR", "8"))
        self.quiet_allow_high = True
        self.digest_hour = int(os.getenv("DIGEST_HOUR", "9"))
        self.alert_min_score = int(os.getenv("ALERT_MIN_SCORE", "0"))
        self.notify_liqmap = True
        self.notify_track = True
        self.track_step_pct = float(os.getenv("TRACK_STEP_PCT", "10"))
        self.track_expire_days = int(os.getenv("TRACK_EXPIRE_DAYS", "14"))
        self.track_auto_stop = convert_value("bool", os.getenv("TRACK_AUTO_STOP", "0"))
        self.track_poll_sec = int(os.getenv("TRACK_POLL_SEC", "120"))
        self.hl_max_rpm = int(os.getenv("HL_MAX_RPM", "350"))
        self.notify_cryptovol = True
        self.crypto_vol_enabled = True
        self.crypto_vol_poll_sec = int(os.getenv("CRYPTO_VOL_POLL_SEC", "300"))
        # SAYFA eşiği bilerek DÜŞÜK: eşik altı rekor hiçbir yere yazılmadığı
        # sürece "panel neden boş" sorusu cevapsız kalıyordu. Bildirim eşiği ayrı.
        self.crypto_vol_min_usd = float(os.getenv("CRYPTO_VOL_MIN_USD", "50000"))
        self.crypto_vol_alert_min_usd = float(
            os.getenv("CRYPTO_VOL_ALERT_MIN_USD", "250000"))
        self.crypto_vol_cooldown = int(os.getenv("CRYPTO_VOL_COOLDOWN", "1800"))
        self.crypto_vol_max_coins = int(os.getenv("CRYPTO_VOL_MAX_COINS", "120"))
        # Kripto bildirimlerinin gideceği AYRI kanal. Diğer chat/kanal id'leri
        # gibi ENV-ONLY: EDITABLE_FIELDS'a girmez.
        self.crypto_chat_id = os.getenv("CRYPTO_CHAT_ID", "")
        self.notify_equityvol = True
        self.equity_vol_enabled = True
        self.equity_vol_poll_sec = int(os.getenv("EQUITY_VOL_POLL_SEC", "300"))
        self.equity_vol_min_usd = float(os.getenv("EQUITY_VOL_MIN_USD", "10000"))
        self.equity_vol_alert_min_usd = float(
            os.getenv("EQUITY_VOL_ALERT_MIN_USD", "100000"))
        self.equity_vol_cooldown = int(os.getenv("EQUITY_VOL_COOLDOWN", "1800"))
        self.equity_vol_max_coins = int(os.getenv("EQUITY_VOL_MAX_COINS", "120"))
        # Hisse hacim bildirimlerinin kanalı. Kullanıcı Railway'de bu adla
        # oluşturdu; env-only (chat id'leri EDITABLE_FIELDS'a girmez).
        self.crypto_stocks_id = os.getenv("CRYPTO_STOCKS_ID", "")
        # Hisseyle AYNI taban: sabırlı bir TWAP'ın dilimleri küçük olur
        # ($5M / 6 saat ≈ $7K) ve yüksek eşik onları tamamen görünmez yapardı —
        # üstelik kaçırdığımızı bile bilemezdik.
        self.crypto_fill_min_notional = float(
            os.getenv("CRYPTO_FILL_MIN_NOTIONAL", "5000"))
        self.crypto_metrics_enabled = True
        self.forensics_probe_max = int(os.getenv("FORENSICS_PROBE_MAX", "15"))
        self.twap_min_usd = float(os.getenv("TWAP_MIN_USD", "5000000"))
        self.twap_window_h = int(os.getenv("TWAP_WINDOW_H", "12"))
        self.twap_scan_sec = int(os.getenv("TWAP_SCAN_SEC", "600"))
        self.notify_pattern = True
        self.pattern_enabled = True
        # self = yalnız sembolün kendi geçmişi (kullanıcı tercihi). Dar havuzun
        # bedeli sık sık "yeterli veri yok"; class'a çevirmek tek ayar.
        self.pattern_pool = os.getenv("PATTERN_POOL", "self")
        self.pattern_win_1h = int(os.getenv("PATTERN_WIN_1H", "24"))
        self.pattern_win_15m = int(os.getenv("PATTERN_WIN_15M", "32"))
        self.pattern_horizons = os.getenv("PATTERN_HORIZONS", "4,12,24")
        self.pattern_top_k = int(os.getenv("PATTERN_TOP_K", "50"))
        self.pattern_min_corr = float(os.getenv("PATTERN_MIN_CORR", "0.5"))
        self.pattern_min_matches = int(os.getenv("PATTERN_MIN_MATCHES", "20"))
        self.pattern_z_alert = float(os.getenv("PATTERN_Z_ALERT", "2.0"))
        self.pattern_edge_min = float(os.getenv("PATTERN_EDGE_MIN", "10"))
        self.pattern_scan_sec = int(os.getenv("PATTERN_SCAN_SEC", "1800"))
        self.bars_1h_days = int(os.getenv("BARS_1H_DAYS", "180"))
        self.bars_15m_days = int(os.getenv("BARS_15M_DAYS", "60"))
        self.bars_refresh_sec = int(os.getenv("BARS_REFRESH_SEC", "1800"))
        # Örüntü bildirimlerinin kanalı — env-only (chat id kuralı).
        self.pattern_chat_id = os.getenv("PATTERN_CHAT_ID", "")
        self.offhours_close_hour = int(os.getenv("OFFHOURS_CLOSE_HOUR", "0"))
        self.offhours_alert_weekend_only = True
        self.offhours_alert_pct = float(os.getenv("OFFHOURS_ALERT_PCT", "0.5"))
        self.offhours_spike_pct = float(os.getenv("OFFHOURS_SPIKE_PCT", "1.0"))
        self.offhours_spike_weekend_only = False
        self.offhours_spike_pct_weekday = float(
            os.getenv("OFFHOURS_SPIKE_PCT_WEEKDAY", "2.0"))
        self.offhours_spike_min = int(os.getenv("OFFHOURS_SPIKE_MIN", "10"))
        self.offhours_spike_cooldown = int(os.getenv("OFFHOURS_SPIKE_COOLDOWN", "1800"))
        self.metrics_poll_closed_sec = int(os.getenv("METRICS_POLL_CLOSED_SEC", "60"))
        self.hourstats_days = int(os.getenv("HOURSTATS_DAYS", "90"))
        self.pricechart_days = int(os.getenv("PRICECHART_DAYS", "30"))
        self.hl_big_min_usd = float(os.getenv("HL_BIG_MIN_USD", "1000000"))
        self.hl_records_keep = int(os.getenv("HL_RECORDS_KEEP", "500"))
        self.hl_prime_top = int(os.getenv("HL_PRIME_TOP", "120"))
        self.probe_min_notional = float(os.getenv("PROBE_MIN_NOTIONAL", "100000"))
        self.probe_cooldown_sec = int(os.getenv("PROBE_COOLDOWN_SEC", "600"))
        self.probe_min_notional_crypto = float(os.getenv("PROBE_MIN_NOTIONAL_CRYPTO", "2000000"))
        self.hl_crypto_min_usd = float(os.getenv("HL_CRYPTO_MIN_USD", "20000000"))
        self.hl_major_min_usd = float(os.getenv("HL_MAJOR_MIN_USD", "50000000"))
        self.crypto_watch_top = int(os.getenv("CRYPTO_WATCH_TOP", "30"))
        self.show_tradingview = True
        # "Saati gelenler" yayın kanalı — kişisel chat'ten AYRI (bot kanala admin olmalı)
        self.telegram_channel_id = os.getenv("TELEGRAM_CHANNEL_ID", "")
        self.sweep_leaderboard_top = int(os.getenv("SWEEP_LEADERBOARD_TOP", "1500"))
        self.sweep_batch_size = int(os.getenv("SWEEP_BATCH_SIZE", "40"))
        self.sweep_catchup = convert_value("bool", os.getenv("SWEEP_CATCHUP", "1"))
        self.sweep_batch_max = int(os.getenv("SWEEP_BATCH_MAX", "250"))
        self.sweep_rpm_headroom = float(os.getenv("SWEEP_RPM_HEADROOM", "0.85"))
        self.sweep_interval_sec = int(os.getenv("SWEEP_INTERVAL_SEC", "90"))
        self.notify_lowvol = True
        self.notify_offhours = True
        self.notify_wall = True
        self.notify_health = False  # bekçi sitede konuşur; Telegram istenirse açılır
        self.notify_listing = True
        self.channel_auto_hours = os.getenv("CHANNEL_AUTO_HOURS", "")
        self.zombie_silent_hours = int(os.getenv("ZOMBIE_SILENT_HOURS", "12"))
        self.wall_window_pct = float(os.getenv("WALL_WINDOW_PCT", "2.0"))
        self.wall_min_usd = float(os.getenv("WALL_MIN_USD", "1000000"))
        self.wall_alert_min_usd = float(os.getenv("WALL_ALERT_MIN_USD", "12000000"))
        self.wall_alert_big_min_usd = float(os.getenv("WALL_ALERT_BIG_MIN_USD", "50000000"))
        self.wall_big_top_n = int(os.getenv("WALL_BIG_TOP_N", "10"))
        self.liq_cluster_big_min_usd = float(os.getenv("LIQ_CLUSTER_BIG_MIN_USD", "20000000"))
        self.wall_poll_sec = int(os.getenv("WALL_POLL_SEC", "180"))
        self.lowvol_max_day_volume = float(os.getenv("LOWVOL_MAX_DAY_VOLUME", "5000000"))
        self.lowvol_min_oi_share = float(os.getenv("LOWVOL_MIN_OI_SHARE", "20"))
        self.lowvol_min_notional = float(os.getenv("LOWVOL_MIN_NOTIONAL", "250000"))
        self.lowvol_alert_min_usd = float(os.getenv("LOWVOL_ALERT_MIN_USD", "2500000"))

        # Takvim kaynakları
        self.finnhub_api_key = os.getenv("FINNHUB_API_KEY", "")
        self.calendar_horizon_days = int(os.getenv("CALENDAR_HORIZON_DAYS", "21"))

        # Hyperliquid
        self.api_base = os.getenv("HL_API_BASE", "https://api.hyperliquid.xyz")
        self.ws_url = os.getenv("HL_WS_URL", "wss://api.hyperliquid.xyz/ws")
        self.stats_leaderboard_url = os.getenv(
            "HL_LEADERBOARD_URL", "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
        )
        # Hisse perp'lerinin yaşadığı HIP-3 dex'leri (virgülle ayrık)
        self.equity_dexes = _csv(os.getenv("EQUITY_DEXES", "xyz"))

        # Eşikler
        self.min_fill_notional = float(os.getenv("MIN_FILL_NOTIONAL", "5000"))
        self.min_position_notional = float(os.getenv("MIN_POSITION_NOTIONAL", "10000"))
        self.big_position_usd = float(os.getenv("BIG_POSITION_USD", "1000000"))
        # "Yeni büyük pozisyon" BİLDİRİMİ kademeli (sitede/skorlamada değişmez)
        self.big_alert_index_usd = float(os.getenv("BIG_ALERT_INDEX_USD", "10000000"))
        self.big_alert_major_usd = float(os.getenv("BIG_ALERT_MAJOR_USD", "5000000"))
        self.big_alert_min_usd = float(os.getenv("BIG_ALERT_MIN_USD", "1000000"))
        self.huge_position_usd = float(os.getenv("HUGE_POSITION_USD", "5000000"))
        self.combo_window_hours = int(os.getenv("COMBO_WINDOW_HOURS", "72"))
        self.fresh_big_alert_hours = int(os.getenv("FRESH_BIG_ALERT_HOURS", "24"))
        self.mm_max_positions = int(os.getenv("MM_MAX_POSITIONS", "10"))
        self.mm_max_fills_24h = int(os.getenv("MM_MAX_FILLS_24H", "80"))
        self.liq_watch_min_notional = float(os.getenv("LIQ_WATCH_MIN_NOTIONAL", "70000000"))
        self.liq_watch_poll_sec = int(os.getenv("LIQ_WATCH_POLL_SEC", "300"))
        self.liq_watch_top_accounts = int(os.getenv("LIQ_WATCH_TOP_ACCOUNTS", "300"))
        self.liq_cluster_window_pct = float(os.getenv("LIQ_CLUSTER_WINDOW_PCT", "5"))
        self.liq_cluster_min_usd = float(os.getenv("LIQ_CLUSTER_MIN_USD", "1000000"))
        self.liq_cluster_alert_min_usd = float(
            os.getenv("LIQ_CLUSTER_ALERT_MIN_USD", "5000000"))
        self.max_liq_distance_pct = float(os.getenv("MAX_LIQ_DISTANCE_PCT", "50"))
        self.whale_alert_notional = float(os.getenv("WHALE_ALERT_NOTIONAL", "250000"))
        self.fresh_wallet_days = int(os.getenv("FRESH_WALLET_DAYS", "7"))
        self.recent_deposit_hours = int(os.getenv("RECENT_DEPOSIT_HOURS", "72"))
        self.eval_move_threshold = float(os.getenv("EVAL_MOVE_THRESHOLD", "2.0"))  # %
        self.eval_min_notional = float(os.getenv("EVAL_MIN_NOTIONAL", "300000"))
        self.leaderboard_top = int(os.getenv("LEADERBOARD_TOP", "500"))
        self.scan_max_candidates = int(os.getenv("SCAN_MAX_CANDIDATES", "600"))
        self.scan_concurrency = int(os.getenv("SCAN_CONCURRENCY", "8"))
        self.auto_scan_interval_sec = int(os.getenv("AUTO_SCAN_INTERVAL_SEC", "180"))
        self.scan_stale_min = int(os.getenv("SCAN_STALE_MIN", "10"))
        self.fills_lookback_days = int(os.getenv("FILLS_LOOKBACK_DAYS", "30"))
        self.fills_retention_days = int(os.getenv("FILLS_RETENTION_DAYS", "14"))

        # Anomali dedektörü
        self.anomaly_poll_sec = int(os.getenv("ANOMALY_POLL_SEC", "1800"))
        self.oi_spike_pct_event = float(os.getenv("OI_SPIKE_PCT_EVENT", "50"))    # earnings <72h iken
        self.oi_spike_pct_normal = float(os.getenv("OI_SPIKE_PCT_NORMAL", "150"))
        self.oi_spike_floor_usd = float(os.getenv("OI_SPIKE_FLOOR_USD", "200000"))
        self.oi_spike_big_floor_usd = float(os.getenv("OI_SPIKE_BIG_FLOOR_USD", "20000000"))
        self.funding_extreme = float(os.getenv("FUNDING_EXTREME", "0.0005"))      # saatlik oran (0.05%/h)
        self.vol_spike_mult = float(os.getenv("VOL_SPIKE_MULT", "3.0"))
        self.vol_spike_min_usd = float(os.getenv("VOL_SPIKE_MIN_USD", "500000"))

        # Korele hisseler: "SNDK:WDC|MU;TSLA:RIVN" formatıyla override edilebilir
        self.peers_override = os.getenv("PEERS", "")
        # ABD dışı hisselerin Yahoo sembolleri (varsayılan eşlemeye eklenir)
        self.yahoo_symbol_map = os.getenv("YAHOO_SYMBOL_MAP", "")
        # propr.xyz'de listeli ek semboller (varsayılan listeye eklenir)
        self.propr_symbols = os.getenv("PROPR_SYMBOLS", "")
        self.tv_symbol_map = os.getenv("TV_SYMBOL_MAP", "")
        # Takvim sorgusundan muaf tutulacak ek enstrümanlar
        self.non_equity_extra = os.getenv("NON_EQUITY_EXTRA", "")
        self.no_calendar_extra = os.getenv("NO_CALENDAR_EXTRA", "")
        # Tamamen takip dışı semboller (evren+tarama+takvim yok)
        self.exclude_symbols = os.getenv("EXCLUDE_SYMBOLS", "BIRD")

        # Periyotlar (saniye)
        self.universe_refresh_sec = int(os.getenv("UNIVERSE_REFRESH_SEC", str(6 * 3600)))
        self.calendar_refresh_sec = int(os.getenv("CALENDAR_REFRESH_SEC", str(12 * 3600)))
        self.metrics_poll_sec = int(os.getenv("METRICS_POLL_SEC", "300"))
        self.due_check_sec = int(os.getenv("DUE_CHECK_SEC", "60"))

        # Dashboard
        self.dashboard_token = os.getenv("DASHBOARD_TOKEN", "")
        # Yönetici şifresi: ayar değiştirme / Telegram gönderme gibi yazma işlemleri için.
        # Tanımlı değilse DASHBOARD_TOKEN'a düşer.
        self.admin_password = os.getenv("ADMIN_PASSWORD", "")

        # Depolama ("hafıza") — Railway'de Volume /data'ya mount edilir
        default_db = "/data/radar.db" if os.path.isdir("/data") else "./data/radar.db"
        self.db_path = os.getenv("DB_PATH", default_db)

        # Dashboard'dan kaydedilen override'lar (ad -> ham string)
        self.overrides: dict[str, str] = {}

    def apply_overrides(self, raw: dict) -> None:
        """DB'den gelen override'ları canlı config'e uygula (hatalıyı atla)."""
        for name, val in (raw or {}).items():
            spec = EDITABLE_FIELDS.get(name)
            if not spec:
                continue
            try:
                setattr(self, name, convert_value(spec["type"], val))
                self.overrides[name] = str(val)
            except (TypeError, ValueError):
                pass

    def env_default(self, name: str):
        """Env/kod varsayılanı (override'sız taze instance'tan)."""
        return getattr(Config(), name)


_cfg: Config | None = None


def get_config() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = Config()
    return _cfg
