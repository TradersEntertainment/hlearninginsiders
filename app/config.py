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
    "track_liq_step_pct": {"type": "float", "label": "Takip: liq fiyatı kayma bildirimi (%)", "group": "Bildirimler",
                           "desc": "Takipteki pozisyonun likidasyon fiyatı son bildirilene göre bu kadar kayınca haber (teminat ekledi/çekti ya da boyut değişti). Kullanıcı kuralı %1; 0 = kapalı"},
    "track_auto_stop": {"type": "bool", "label": "Takip süreyle bitsin (eski davranış)",
                        "group": "Bildirimler",
                        "desc": "Açılırsa takip, süre dolunca poz açık olsa bile BİTER (eskiden böyleydi — balinanın çıkışı kaçıyordu). Kapalıyken takip yalnız pozisyon kapanınca ya da /birak_N ile biter"},
    "track_expire_days": {"type": "int", "label": "Takip yoklama aralığı (gün)", "group": "Bildirimler",
                          "desc": "Poz hâlâ açıkken kaç günde bir 'takipteyim, bırakayım mı?' densin. Takip bu süreyle BİTMEZ — yalnız pozisyon kapanınca ya da /birak_N ile biter"},
    "notify_twap": {"type": "bool", "label": "⏳ TWAP (düşük hacimli coinde büyük)", "group": "Bildirimler",
                    "desc": "Adresin HL TWAP emri sorgulanır: emir ≥ $2M, kalan ≥ $1M ve coinin 24s hacminin ≥ %20'si ise (ör. INJ'e $2M). Tahmin yok. Kripto → CRYPTO_CHAT_ID (tanımsızsa gönderilmez), hisse/endeks → ana sohbet; eşikler 'TWAP radarı' grubunda"},
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
    "notify_cryptoliq": {"type": "bool", "label": "💥 Kripto liq yakını", "group": "Bildirimler",
                         "desc": "Ana dex kriptoda (BTC/ETH hariç) eşik büyüklüğündeki bir pozisyon likidasyon fiyatına eşik mesafe kadar yaklaşınca — ayrı kanala gider (CRYPTO_CHAT_ID); eşikler 'Kripto liq' grubunda"},
    "notify_sim": {"type": "bool", "label": "🧪 Liq simülasyonu mesajları", "group": "Bildirimler",
                   "desc": "Sanal işlem açılış/kapanış/sıfırlama mesajları — ayrı kanala gider (SIM_CHAT_ID, env). İşlem defteri mesajdan bağımsız ilerler; kanal boşsa hiçbir yere gitmez"},
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
    "liq_cluster_big_min_usd": {"type": "float", "label": "Top-10 hisse duvar eşiği ($)",
                                "group": "Likidasyon radarı", "desc": "Hacimce top-10 hisselerde liq duvarı alarmı için gereken TOPLAM — likit hissede küçük küme gürültü. Endeks/emtia/FX (GOLD, CL, XYZ100…) duvarı bunu KULLANMAZ: liq attack kapısı (liq_attack_alert_big_dist_pct / liq_attack_alert_big_min_usd, ≤%1 içinde ≥ $50M) geçerlidir"},
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
                         "desc": "Ana dex'te 24h hacme göre ilk kaç coin canlı dinlensin. 'Kripto hacim evren tavanı' ile EŞİT tutulmalı: alarm çıkan ama dinlenmeyen bir coinde 'kim ne aldı' kırılımı boş gelir. 0 = kripto dinleme kapalı"},
    "liq_attack_min_usd": {"type": "float", "label": "Liq attack: hedef küme tabanı ($)",
                           "group": "Liq attack",
                           "desc": "Şimdiye yakın likidasyon kümesi en az bu kadar $ olmalı ki 'itmeye değer' sayılsın"},
    "liq_attack_max_dist_pct": {"type": "float", "label": "Liq attack: azami hedef uzaklığı (%)",
                                "group": "Liq attack",
                                "desc": "Bu mesafeden uzak kümeler hedef sayılmaz — ince defterde bile çok uzağı itmek pahalı"},
    "liq_attack_min_score": {"type": "float", "label": "Liq attack: aday eşiği (oran)",
                             "group": "Liq attack",
                             "desc": "patlayacak $ / yenmesi gereken defter $ bu oranı geçerse ADAY: sayfada öne çıkar ve Telegram'a düşer. 1 = başabaş, 2 = çekici, 5+ = neredeyse bedava"},
    "liq_attack_alert_dist_pct": {"type": "float", "label": "Liq attack: bildirim kapısı — mesafe (%)",
                                  "group": "Liq attack",
                                  "desc": "Telegram kapısı: fiyatın bu kadar yakınında (aşağıdaki $ kadar) likidasyon yoksa aday Telegram'a DÜŞMEZ — sayfada yine görünür (🔕). Uzak kümeler spam oluyordu"},
    "liq_attack_alert_min_usd": {"type": "float", "label": "Liq attack: bildirim kapısı — asgari liq ($)",
                                 "group": "Liq attack",
                                 "desc": "Yukarıdaki mesafe içinde en az bu kadar $ likidasyon varsa bildirim gider (🔔)"},
    "liq_attack_alert_big_dist_pct": {"type": "float", "label": "Liq attack: endeks/emtia/FX kapısı — mesafe (%)",
                                      "group": "Liq attack",
                                      "desc": "SP500, XYZ100, GOLD, FX gibi likit perp'lerde (assets.NON_EQUITY + 'non_equity_extra') Telegram kapısı bu mesafe içine bakar. Likit perp'te $2-4M'lik küme %2'de kolayca oran verip ana kanalı dolduruyordu — kullanıcı kuralı: %1. 🧲 Likidasyon duvarı bildirimi de aynı kapıyı kullanır"},
    "liq_attack_alert_big_min_usd": {"type": "float", "label": "Liq attack: endeks/emtia/FX kapısı — asgari liq ($)",
                                     "group": "Liq attack",
                                     "desc": "Aynı sınıfta bu mesafe içinde en az bu kadar liq yoksa mesaj GİTMEZ (kullanıcı kuralı $50M). Hisseler normal kapıda (≤%2 / $1M) kalır; oran ≥ eşik şartı ikisinde de geçerli. 🧲 Likidasyon duvarı bildirimi de bu kapıyı kullanır: mesafe içindeki liq ≥ bu tutar değilse duvar mesajı gitmez"},
    "liq_attack_scan_sec": {"type": "int", "label": "Liq attack: tarama aralığı (sn)",
                            "group": "Liq attack",
                            "desc": "Yalnız hafta sonu çalışır; aday coin başına 1 l2Book isteği"},
    "liq_attack_cooldown": {"type": "int", "label": "Liq attack: bildirim bekleme (sn)",
                            "group": "Liq attack",
                            "desc": "Aynı coin+yön için iki bildirim arası"},
    "liq_attack_spike_pct": {"type": "float", "label": "Liq attack: geçmiş sıçrama eşiği (%)",
                             "group": "Liq attack",
                             "desc": "Karne için: 1 dk'lık fiyat serisinde referanstan bu kadar sapıp geri dönen hareket 'saldırı' adayı (liq onayı da şart)"},
    "liq_attack_revert_min": {"type": "int", "label": "Liq attack: geri dönüş süresi (dk)",
                              "group": "Liq attack",
                              "desc": "Sıçrama bu süre içinde referansa dönmezse gerçek fiyat hareketidir, saldırı değil"},
    "alert_forensics": {"type": "bool", "label": "Alarmlara 'ne oldu' detayı",
                        "group": "Bildirimler",
                        "desc": "Hacim, kapalı seans ve whale alarmlarına OI okuması (long mu kapandı, short mu açıldı) ve en büyük adreslerin ne yaptığı eklenir. Kapatılırsa mesajlar eski sade hâline döner"},
    "alert_forensics_top": {"type": "int", "label": "Alarmdaki adres satırı",
                            "group": "Bildirimler",
                            "desc": "Mesajda kaç adres gösterilsin. Telegram'da 3-4 satırdan uzun mesaj okunmuyor; tam kırılım için siteye ('ne oldu' sekmesi) bakılır"},
    "alert_forensics_probe": {"type": "int", "label": "Alarm anı canlı sonda",
                              "group": "Bildirimler",
                              "desc": "Alarm başına kaç adresin defteri ANINDA çekilsin (adres başına 1 istek). Pozisyon verisi normalde keşif turuna bağlı ve 2 saate kadar bayat olabilir; bu olmadan 'long'unu artırdı' çıkarımı eski veriye dayanır. 0 = sonda kapalı, bayatlık ⏳ ile işaretlenir"},
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
                                 "desc": "Telegram'a düşmesi için gereken tutar (kullanıcı kuralı $1M — 500K-1M bandı, hisseyle aynı). Sayfa eşiğinden yüksek: sayfada bağlam olan küçük rekor, kanalda gürültüdür"},
    "crypto_vol_chart": {"type": "bool", "label": "Mesaja grafik ekle", "group": "Kripto hacim",
                         "desc": "Rekor mesajı geniş 5 dk grafikle tek mesaj: fiyat mumları + $ hacim barları, rekor kovası vurgulu, önceki rekor çizgisi. Ek istek yok (mumlar zaten elde)"},
    "crypto_vol_cooldown": {"type": "int", "label": "Coin başına bekleme (sn)", "group": "Kripto hacim",
                            "desc": "Uzun bir yükselişte her yeni kova yeni bir 24s rekoru olabilir; hepsi bildirilmesin"},
    "crypto_vol_max_coins": {"type": "int", "label": "Evren tavanı (coin)", "group": "Kripto hacim",
                             "desc": "Kaç coin taranacak (PROPR ∩ ana dex, hacimce büyükten). Her coin turda 1 istek eder"},
    "crypto_liq_enabled": {"type": "bool", "label": "Kripto liq radarı", "group": "Kripto liq",
                           "desc": "Kapatılırsa tur hiç çalışmaz; istek maliyeti sıfırlanır"},
    "crypto_liq_min_usd": {"type": "float", "label": "Asgari pozisyon ($)", "group": "Kripto liq",
                           "desc": "Bu tutarın altındaki pozisyon bildirilmez (kullanıcı kuralı $500K). Coin sayfasındaki likidasyon haritası her boyutu gösterir — bu yalnız Telegram eşiği"},
    "crypto_liq_dist_pct": {"type": "float", "label": "Liq mesafesi (%)", "group": "Kripto liq",
                            "desc": "Likidasyon fiyatı şimdiye bu kadar ya da daha yakınsa bildirim (kullanıcı kuralı %2,5). BTC ve ETH her zaman hariç"},
    "crypto_liq_dist2_pct": {"type": "float", "label": "2. uyarı mesafesi (%)", "group": "Kripto liq",
                             "desc": "Bildirilen pozisyon likidasyona bu kadar yaklaşınca yeniden mesaj (beklemeye bakmaz — yeni bilgi). Kullanıcı kuralı %1"},
    "crypto_liq_dist3_pct": {"type": "float", "label": "Son uyarı mesafesi (%)", "group": "Kripto liq",
                             "desc": "Üçüncü ve son mesaj; sonrası ya likidasyon ya kapanış notu. Kullanıcı kuralı %0,5. Pozisyon ilk mesafenin 1,5 katına uzaklaşırsa kademeler sıfırlanır"},
    "crypto_liq_notify_close": {"type": "bool", "label": "Kapanış / likidasyon notu", "group": "Kripto liq",
                                "desc": "İzlenen pozisyon yok olunca mesaj: fill'lerde likidasyon kaydı varsa 💀 LİKİDE OLDU (gerçekleşen fiyatla), yoksa 🏁 kapandı; teyit alınamazsa 'doğrulanamadı' der"},
    "crypto_liq_cascade": {"type": "bool", "label": "Zincir simülasyonu", "group": "Kripto liq",
                           "desc": "Mesaja '💣 Zincir' satırı: en yakın pozisyon patlarsa zorunlu emir defteri nereye kadar süpürür, arada kaç pozisyon daha patlar, fiyat en az nereye gider (defter anlık + havuz; alt sınır). Tur başına 1 l2Book isteği"},
    "crypto_liq_chart": {"type": "bool", "label": "Mesaja grafik ekle", "group": "Kripto liq",
                         "desc": "Her kademe mesajının ardından resim: son 48 saatin 30 dk mumları, liq çizgisi, fiyat ve kalan mesafe (Telegram sendPhoto; mum çekilemezse yalnız metin gider)"},
    "crypto_liq_poll_sec": {"type": "int", "label": "Tarama aralığı (sn)", "group": "Kripto liq",
                            "desc": "Turda 1 fiyat isteği (ana dex özeti, metrik döngüsüyle paylaşılır) + bildirilecek her aday adres için 1 canlılık sondası (tur başına en çok 12)"},
    "crypto_liq_cooldown": {"type": "int", "label": "Pozisyon başına bekleme (sn)", "group": "Kripto liq",
                            "desc": "Aynı pozisyon bu süre içinde yeniden bildirilmez — fiyat eşiğin etrafında salınınca her tur mesaj olmasın. Aynı coinde YENİ bir pozisyon eşiğe girerse mesaj yine gider (coin başına tek mesaj)"},
    "sim_enabled": {"type": "bool", "label": "🧪 Liq simülasyonu", "group": "Simülasyon",
                    "desc": "Kripto liq radarının sondayla doğrulanmış SON UYARI'sında kâğıt üstünde işlem: balinanın tersine girer, hedef zincir sonu (limit); iğne gelirse aynı fiyattan ters bacak. Gerçek emir YOK. Sayfa: /sim"},
    "sim_start_balance": {"type": "float", "label": "Başlangıç bakiyesi ($)", "group": "Simülasyon",
                          "desc": "Sanal hesabın başlangıcı; sıfırlamada da buna döner (kullanıcı kuralı $10K)"},
    "sim_leverage": {"type": "float", "label": "Kaldıraç (x)", "group": "Simülasyon",
                     "desc": "İki bacak da bu kaldıraçla girer (kullanıcı kuralı 5x)"},
    "sim_margin_pct": {"type": "float", "label": "Marjin payı (%)", "group": "Simülasyon",
                       "desc": "Bakiyenin yüzde kaçı BİR işleme yatırılsın: 33 = aynı anda üç eşit dilim (kullanıcı kuralı), 50 = iki, 100 = tek işlem. Dilimler doluysa sinyal 'bakiye bağlı' diye atlanır; son dilim kalan bakiyeden küçük olabilir"},
    "sim_stop_pct": {"type": "float", "label": "Ön bacak stop (%)", "group": "Simülasyon",
                     "desc": "Girişten bu kadar ters gidince piyasadan kapanır (kullanıcı kuralı %10). Aynı mumda hedef de görülürse stop sayılır"},
    "sim_min_tp_pct": {"type": "float", "label": "Asgari hedef mesafesi (%)", "group": "Simülasyon",
                       "desc": "Zincir sonu girişe bundan yakınsa işlem açılmaz (ücreti karşılamaz)"},
    "sim_max_tp_pct": {"type": "float", "label": "Azami hedef mesafesi (%)", "group": "Simülasyon",
                       "desc": "0 = kapalı (hedef her zaman zincir sonu). Açılırsa daha uzak hedef burada kırpılır ve ters bacak açılmaz (ince defterde zincir sonu güvenilmez)"},
    "sim_after_liq_min": {"type": "int", "label": "Liq sonrası bekleme (dk)", "group": "Simülasyon",
                          "desc": "Balina patladıktan sonra iğne bu sürede hedefe uzanmazsa ön bacak piyasadan kapanır, ters bacak açılmaz (kullanıcı kuralı 30)"},
    "sim_pre_max_min": {"type": "int", "label": "Ön bacak azami ömür (dk)", "group": "Simülasyon",
                        "desc": "Balina bu sürede patlamazsa (uzaklaştı, teminat ekledi) ön bacak piyasadan kapanır"},
    "sim_post_tp_pct": {"type": "float", "label": "Ters bacak hedefi (%)", "group": "Simülasyon",
                        "desc": "İğneden bu kadar geri çekilince limitle kapanır (kullanıcı kuralı %0,5–1)"},
    "sim_post_stop_pct": {"type": "float", "label": "Ters bacak stop (%)", "group": "Simülasyon",
                          "desc": "Girişten bu kadar ters gidince piyasadan kapanır (kullanıcı kuralı %10)"},
    "sim_post_max_min": {"type": "int", "label": "Ters bacak azami ömür (dk)", "group": "Simülasyon",
                         "desc": "Bu sürede hedef gelmezse piyasadan kapanır (kısa vadeli işlem)"},
    "sim_fee_taker_pct": {"type": "float", "label": "Taker ücreti (%)", "group": "Simülasyon",
                          "desc": "Piyasa emirleri (giriş, stop, süre dolumu), notional üzerinden; HL taban %0,045"},
    "sim_fee_maker_pct": {"type": "float", "label": "Maker ücreti (%)", "group": "Simülasyon",
                          "desc": "Limit emirleri (hedef, ters bacak girişi), notional üzerinden; HL taban %0,015"},
    "sim_poll_sec": {"type": "int", "label": "Değerlendirme aralığı (sn)", "group": "Simülasyon",
                     "desc": "Açık işlem başına 1 mum isteği (1 dk mumlar); açık işlem yoksa istek yok"},
    "equity_vol_enabled": {"type": "bool", "label": "Hisse hacim radarı", "group": "Hisse hacim",
                           "desc": "Kapatılırsa hiç mum çekilmez, istek maliyeti sıfırlanır"},
    "equity_vol_poll_sec": {"type": "int", "label": "Tarama aralığı (sn)", "group": "Hisse hacim",
                            "desc": "5 dakikalık kova için doğal ritim 300 sn — kısaltmak aynı kovayı tekrar taramak olur"},
    "equity_vol_min_usd": {"type": "float", "label": "Asgari 5dk hacim — SAYFA ($)", "group": "Hisse hacim",
                           "desc": "Bu tutarın üstündeki her rekor /hacim sayfasına yazılır. Kriptodakinden DÜŞÜK: hisse perp'leri çok daha ince — SHEIN'in 24 saatlik TOPLAM hacmi $4.2M'ken patlama mumu ~$150K'ydı"},
    "equity_vol_alert_min_usd": {"type": "float", "label": "Asgari 5dk hacim — BİLDİRİM ($)", "group": "Hisse hacim",
                                 "desc": "Telegram'a düşmesi için gereken tutar (kullanıcı kuralı $1M — 500K-1M bandı, kriptoyla aynı; eski $100K kanalı spam'e boğuyordu). Sayfa eşiğinden yüksek tutulur"},
    "equity_vol_chart": {"type": "bool", "label": "Mesaja grafik ekle", "group": "Hisse hacim",
                         "desc": "Rekor mesajı geniş 5 dk grafikle tek mesaj (kripto ile aynı çizim)"},
    "equity_vol_cooldown": {"type": "int", "label": "Hisse başına bekleme (sn)", "group": "Hisse hacim",
                            "desc": "Uzun bir hareket boyunca her yeni kova yeni bir 24s rekoru olabilir; hepsi bildirilmesin"},
    "equity_vol_max_coins": {"type": "int", "label": "Evren tavanı (hisse)", "group": "Hisse hacim",
                             "desc": "Kaç hisse taranacak (PROPR ∩ xyz dex, hacimce büyükten). Her hisse turda 1 istek eder"},
    "twap_min_usd": {"type": "float", "label": "Arşiv TWAP asgari büyüklük ($)",
                     "group": "TWAP radarı",
                     "desc": "Bu tutarın üstündeki düzenli birikimler /twap sekmesinde listelenir (fills arşivinden). Yakalama tabanının altındaki dilimler arşivde görünmez — onları canlı radar sayar"},
    "twap_window_h": {"type": "int", "label": "Arşiv tarama penceresi (saat)",
                      "group": "TWAP radarı",
                      "desc": "Kaç saat geriye bakılıp dilimler birleştirilsin. Uzun TWAP'lar için büyük, gürültü için küçük tutulur"},
    "twap_scan_sec": {"type": "int", "label": "Arşiv tarama aralığı (sn)",
                      "group": "TWAP radarı",
                      "desc": "Tarama tamamen YEREL (fills tablosu) — API maliyeti yoktur"},
    "twap_live_enabled": {"type": "bool", "label": "📡 Canlı TWAP radarı", "group": "TWAP radarı",
                          "desc": "WS akışındaki HER işlemi (yakalama tabanının altındakiler dahil) adres bazında bellekte sayar; ekstra HL isteği yok. Kapatılırsa bildirim ve sayaç durur, arşiv taraması sürer"},
    "twap_alert_min_usd": {"type": "float", "label": "Bildirim tabanı — emir toplamı ($)", "group": "TWAP radarı",
                           "desc": "Adresin HL TWAP EMRİNİN planlanan toplamı (adet × bugünkü fiyat) bu tutarın altındaysa bildirilmez (kullanıcı kuralı $2M). Tahmin yok: emir WS'ten sorgulanır, emir yoksa bildirim yok"},
    "twap_alert_min_left_usd": {"type": "float", "label": "Bildirim tabanı — kalan ($)", "group": "TWAP radarı",
                                "desc": "Emrin henüz dolmamış kısmı bundan azsa bildirilmez — bitmek üzere olan TWAP'ın haberi işe yaramaz (kullanıcı kuralı $1M)"},
    "twap_alert_vol_pct": {"type": "float", "label": "Bildirim kapısı — emir / 24s hacim (%)", "group": "TWAP radarı",
                           "desc": "Emir toplamı coinin 24 saatlik hacminin en az bu yüzdesi olmalı (kullanıcı kuralı %20). INJ: $4.1M emir, hacim $9.9M → %41; BTC'de aynı emir %0,2 → sessiz"},
    "twap_alert_big_usd": {"type": "float", "label": "Hacimden bağımsız bildirim ($)", "group": "TWAP radarı",
                           "desc": "0 = kapalı. Açılırsa emir toplamı bunu geçince hacim oranına bakılmaz (kalan eşiği yine geçerli)"},
    "twap_alert_min_slices": {"type": "int", "label": "Sorgu için asgari dilim", "group": "TWAP radarı",
                              "desc": "Bu kadar düzenli dilim görülünce adresin TWAP emri sorgulanır (30 sn'lik HL TWAP'ta 10 dilim = 5 dk)"},
    "twap_lookup_min_usd": {"type": "float", "label": "Sorgu için asgari gözlenen toplam ($)", "group": "TWAP radarı",
                            "desc": "Gözlenen dilimlerin toplamı bunun altındayken sorgu yapılmaz (küçük botlar için soketi meşgul etmemek)"},
    "twap_lookup_cooldown": {"type": "int", "label": "Aynı adres için sorgu aralığı (sn)", "group": "TWAP radarı",
                             "desc": "Sorgu sonucu bu süre önbellekte kalır; bildirilmiş tur 10 dk'da bir tazelenir"},
    "twap_alert_cooldown": {"type": "int", "label": "Aynı adres+coin+yön için bekleme (sn)", "group": "TWAP radarı",
                            "desc": "Restart'a dayanıklı (alerts_log). İlerleme ve bitiş notları ayrı anahtarla gider"},
    "twap_alert_progress": {"type": "bool", "label": "İlerleme notu", "group": "TWAP radarı",
                            "desc": "Emrin yarısı dolunca tek kısa not (dolan / plan, kalan süre, fiyat)"},
    "twap_alert_end_note": {"type": "bool", "label": "Bitiş notu", "group": "TWAP radarı",
                            "desc": "Emir bitince (🏁) ya da iptal edilince (⛔) gerçek dolan tutar, plan, süre ve fiyat değişimi yazılır"},
    "twap_live_window_min": {"type": "int", "label": "Canlı pencere (dk)", "group": "TWAP radarı",
                             "desc": "Bildirilmemiş bir dizinin bellekte tutulduğu en uzun süre; boşta kalan dizi 30 dk'da düşer"},
    "twap_live_eval_sec": {"type": "int", "label": "Canlı değerlendirme aralığı (sn)", "group": "TWAP radarı",
                           "desc": "Tamamen yerel: bellek + kv + birkaç SQL sorgusu"},
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
    # ---- Satılabilir bot: herkese açık DM akışı, katmanlar, fiyat, kota
    "public_bot_enabled": {"type": "bool", "label": "🛒 Herkese açık bot (DM)", "group": "Satış / Kullanıcılar",
                           "desc": "AÇIKKEN bota özelden yazan herkes kaydolur: coin adı yazıp liq grafiği alır (ücretsizde günlük limit), Pro olabilir. KAPALIYKEN yabancı sohbetlere yalnız chat id cevabı gider (eski davranış). Sahibin sohbeti ve kanalları her iki durumda aynı"},
    "free_daily_queries": {"type": "int", "label": "Ücretsiz günlük sorgu", "group": "Satış / Kullanıcılar",
                           "desc": "Ücretsiz kullanıcı günde bu kadar coin sorgusu yapar (TSİ 00:00'da yenilenir). Her sorgu HL'ye ~3 istek; önbellekten dönenler de sayılır"},
    "pro_query_per_min": {"type": "int", "label": "Pro: dakikada sorgu", "group": "Satış / Kullanıcılar",
                          "desc": "Adil kullanım: Pro kullanıcı dakikada en çok bu kadar sorgu (bellek içi kayan pencere)"},
    "query_global_per_min": {"type": "int", "label": "Toplam sorgu tavanı (dakika)", "group": "Satış / Kullanıcılar",
                             "desc": "TÜM kullanıcıların HL'ye giden sorguları için ortak kova; dolunca 'yoğunluk var' cevabı (önbellekten dönenler sayılmaz). 40 sorgu ≈ 120 HL isteği/dk; radarlara pay kalsın (HL_MAX_RPM 350)"},
    "query_cache_sec": {"type": "int", "label": "Sorgu önbelleği (sn)", "group": "Satış / Kullanıcılar",
                        "desc": "Aynı coin bu süre içinde tekrar sorulursa HL'ye gidilmez, aynı grafik ve metin döner (100 kişi aynı anda HYPE sorsa tek hesap)"},
    "pro_price_usd_1m": {"type": "float", "label": "Pro fiyatı — 1 ay ($)", "group": "Satış / Kullanıcılar",
                         "desc": "Kullanıcı kuralı: çok ucuz (2.99). Stars fiyatı bu tutar × Stars kuru; komisyon kullanıcıya yansır"},
    "pro_price_usd_3m": {"type": "float", "label": "Pro fiyatı — 3 ay ($)", "group": "Satış / Kullanıcılar",
                         "desc": "0 = bu paket satılmaz"},
    "pro_price_usd_12m": {"type": "float", "label": "Pro fiyatı — 12 ay ($)", "group": "Satış / Kullanıcılar",
                          "desc": "0 = bu paket satılmaz"},
    "stars_per_usd": {"type": "float", "label": "Telegram Stars kuru (Star / $)", "group": "Satış / Kullanıcılar",
                      "desc": "Geliştiriciye Star başına ~$0.013 ödenir → 77 Star ≈ $1 net; kullanıcı Star'ı ~$0.02'ye alır, fark Telegram/mağaza komisyonu. $2.99 → 230 Stars"},
    "public_kinds": {"type": "csv", "label": "Satılan bildirim türleri", "group": "Satış / Kullanıcılar",
                     "desc": "Pro kullanıcılara fan-out edilebilen türler (virgülle; notify.PUBLIC_KINDS içinden). Boş = fan-out yok. Sahibin kanalları etkilenmez"},
    "pro_default_kinds": {"type": "csv", "label": "Pro açılınca varsayılan türler", "group": "Satış / Kullanıcılar",
                          "desc": "Yeni Pro kullanıcı /bildirimler'e dokunmadan bu türleri alır; sonradan değiştirir"},
    "support_contact": {"type": "str", "label": "Destek iletişimi", "group": "Satış / Kullanıcılar",
                        "desc": "Mesajlarda gösterilen destek adresi, ör. @kullanici. Boş = satır yok"},
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
        self.track_liq_step_pct = float(os.getenv("TRACK_LIQ_STEP_PCT", "1"))
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
        # Kullanıcı kuralı: 5dk hacim $1M'nin altındaysa kanala düşmesin
        # (500K-1M bandı; hisse rekoru da aynı tabanı kullanır — $500K/$100K spam'di).
        self.crypto_vol_alert_min_usd = float(
            os.getenv("CRYPTO_VOL_ALERT_MIN_USD", "1000000"))
        self.crypto_vol_chart = True
        self.crypto_vol_cooldown = int(os.getenv("CRYPTO_VOL_COOLDOWN", "1800"))
        self.crypto_vol_max_coins = int(os.getenv("CRYPTO_VOL_MAX_COINS", "120"))
        # Kripto bildirimlerinin gideceği AYRI kanal. Diğer chat/kanal id'leri
        # gibi ENV-ONLY: EDITABLE_FIELDS'a girmez.
        self.crypto_chat_id = os.getenv("CRYPTO_CHAT_ID", "")
        # Kripto liq yakını (ana dex, BTC/ETH hariç) → aynı kripto kanalı.
        self.notify_cryptoliq = True
        self.crypto_liq_enabled = True
        self.crypto_liq_min_usd = float(os.getenv("CRYPTO_LIQ_MIN_USD", "500000"))
        self.crypto_liq_dist_pct = float(os.getenv("CRYPTO_LIQ_DIST_PCT", "2.5"))
        self.crypto_liq_dist2_pct = float(os.getenv("CRYPTO_LIQ_DIST2_PCT", "1.0"))
        self.crypto_liq_dist3_pct = float(os.getenv("CRYPTO_LIQ_DIST3_PCT", "0.5"))
        self.crypto_liq_notify_close = True
        self.crypto_liq_chart = True
        self.crypto_liq_cascade = True
        self.crypto_liq_poll_sec = int(os.getenv("CRYPTO_LIQ_POLL_SEC", "120"))
        self.crypto_liq_cooldown = int(os.getenv("CRYPTO_LIQ_COOLDOWN", "14400"))
        # Liq simülasyonu (kâğıt üstü). Kanal env-only (chat id kuralı); boşsa
        # Telegram'a hiçbir şey gitmez, /sim sayfası ve defter yine çalışır.
        self.sim_chat_id = os.getenv("SIM_CHAT_ID", "")
        self.notify_sim = True
        self.sim_enabled = True
        self.sim_start_balance = float(os.getenv("SIM_START_BALANCE", "10000"))
        self.sim_leverage = float(os.getenv("SIM_LEVERAGE", "5"))
        self.sim_margin_pct = float(os.getenv("SIM_MARGIN_PCT", "33"))
        self.sim_stop_pct = float(os.getenv("SIM_STOP_PCT", "10"))
        self.sim_min_tp_pct = float(os.getenv("SIM_MIN_TP_PCT", "0.2"))
        self.sim_max_tp_pct = float(os.getenv("SIM_MAX_TP_PCT", "0"))
        self.sim_after_liq_min = int(os.getenv("SIM_AFTER_LIQ_MIN", "30"))
        self.sim_pre_max_min = int(os.getenv("SIM_PRE_MAX_MIN", "360"))
        self.sim_post_tp_pct = float(os.getenv("SIM_POST_TP_PCT", "0.75"))
        self.sim_post_stop_pct = float(os.getenv("SIM_POST_STOP_PCT", "10"))
        self.sim_post_max_min = int(os.getenv("SIM_POST_MAX_MIN", "120"))
        self.sim_fee_taker_pct = float(os.getenv("SIM_FEE_TAKER_PCT", "0.045"))
        self.sim_fee_maker_pct = float(os.getenv("SIM_FEE_MAKER_PCT", "0.015"))
        self.sim_poll_sec = int(os.getenv("SIM_POLL_SEC", "60"))
        self.notify_equityvol = True
        self.equity_vol_enabled = True
        self.equity_vol_poll_sec = int(os.getenv("EQUITY_VOL_POLL_SEC", "300"))
        self.equity_vol_min_usd = float(os.getenv("EQUITY_VOL_MIN_USD", "10000"))
        self.equity_vol_alert_min_usd = float(              # kullanıcı kuralı: kriptoyla aynı $1M
            os.getenv("EQUITY_VOL_ALERT_MIN_USD", "1000000"))
        self.equity_vol_chart = True
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
        # Canlı TWAP radarı (bkz. app/radar/twaplive.py) — "INJ'e $2M TWAP" alarmı
        self.twap_live_enabled = True
        self.twap_alert_min_usd = float(os.getenv("TWAP_ALERT_MIN_USD", "2000000"))
        self.twap_alert_min_left_usd = float(os.getenv("TWAP_ALERT_MIN_LEFT_USD", "1000000"))
        self.twap_alert_vol_pct = float(os.getenv("TWAP_ALERT_VOL_PCT", "20"))
        self.twap_alert_big_usd = float(os.getenv("TWAP_ALERT_BIG_USD", "0"))
        self.twap_alert_min_slices = int(os.getenv("TWAP_ALERT_MIN_SLICES", "10"))
        self.twap_lookup_min_usd = float(os.getenv("TWAP_LOOKUP_MIN_USD", "50000"))
        self.twap_lookup_cooldown = int(os.getenv("TWAP_LOOKUP_COOLDOWN", "600"))
        self.twap_alert_cooldown = int(os.getenv("TWAP_ALERT_COOLDOWN", "21600"))
        self.twap_alert_progress = True
        self.twap_alert_end_note = True
        self.twap_live_window_min = int(os.getenv("TWAP_LIVE_WINDOW_MIN", "240"))
        self.twap_live_eval_sec = int(os.getenv("TWAP_LIVE_EVAL_SEC", "60"))
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
        # Liq attack: hafta sonu yakın liq kümesini itmek ucuzsa. Kanal env-only;
        # boşsa ana sohbete düşer (kullanıcı tercihi).
        self.liq_attack_chat_id = os.getenv("LIQ_ATTACK_CHAT_ID", "")
        self.liq_attack_min_usd = float(os.getenv("LIQ_ATTACK_MIN_USD", "2000000"))
        self.liq_attack_max_dist_pct = float(os.getenv("LIQ_ATTACK_MAX_DIST_PCT", "4"))
        self.liq_attack_min_score = float(os.getenv("LIQ_ATTACK_MIN_SCORE", "2"))
        self.liq_attack_alert_dist_pct = float(os.getenv("LIQ_ATTACK_ALERT_DIST_PCT", "2"))
        self.liq_attack_alert_min_usd = float(os.getenv("LIQ_ATTACK_ALERT_MIN_USD", "1000000"))
        # Endeks/emtia/FX (SP500, XYZ100, GOLD…): likit perp, sıkı kapı — kullanıcı kuralı
        self.liq_attack_alert_big_dist_pct = float(os.getenv("LIQ_ATTACK_ALERT_BIG_DIST_PCT", "1"))
        self.liq_attack_alert_big_min_usd = float(os.getenv("LIQ_ATTACK_ALERT_BIG_MIN_USD", "50000000"))
        self.liq_attack_scan_sec = int(os.getenv("LIQ_ATTACK_SCAN_SEC", "300"))
        self.liq_attack_cooldown = int(os.getenv("LIQ_ATTACK_COOLDOWN", "14400"))
        self.liq_attack_spike_pct = float(os.getenv("LIQ_ATTACK_SPIKE_PCT", "1.5"))
        self.liq_attack_revert_min = int(os.getenv("LIQ_ATTACK_REVERT_MIN", "90"))
        self.notify_liqattack = True
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
        # Alarm evreniyle EŞİT (crypto_vol_max_coins): 30'da kalınca alarm
        # çıkan coinlerin çoğunda adres kırılımı boş geliyordu — dinlemediğimiz
        # bir coin hakkında "kimse almadı" demek yanlış cevaptır.
        self.crypto_watch_top = int(os.getenv("CRYPTO_WATCH_TOP", "120"))
        self.alert_forensics = True
        self.alert_forensics_top = int(os.getenv("ALERT_FORENSICS_TOP", "3"))
        self.alert_forensics_probe = int(os.getenv("ALERT_FORENSICS_PROBE", "3"))
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
        self.notify_twap = True
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

        # Satılabilir bot (herkese açık DM): varsayılan KAPALI — dal Railway'e
        # otomatik gider, yarım fazlar hiçbir şeyi dışarı açmasın.
        self.public_bot_enabled = os.getenv("PUBLIC_BOT_ENABLED", "0").strip().lower() in ("1", "true", "on")
        self.free_daily_queries = int(os.getenv("FREE_DAILY_QUERIES", "3"))
        self.pro_query_per_min = int(os.getenv("PRO_QUERY_PER_MIN", "6"))
        self.query_global_per_min = int(os.getenv("QUERY_GLOBAL_PER_MIN", "40"))
        self.query_cache_sec = int(os.getenv("QUERY_CACHE_SEC", "60"))
        self.pro_price_usd_1m = float(os.getenv("PRO_PRICE_USD_1M", "2.99"))
        self.pro_price_usd_3m = float(os.getenv("PRO_PRICE_USD_3M", "7.99"))
        self.pro_price_usd_12m = float(os.getenv("PRO_PRICE_USD_12M", "24.99"))
        self.stars_per_usd = float(os.getenv("STARS_PER_USD", "77"))
        self.public_kinds = _csv(os.getenv(
            "PUBLIC_KINDS", "cryptoliq,liqmap,liqattack,twap,cryptovol,equityvol,new_big,whale_fill,"
                            "wall,offhours,lowvol,anomaly,pattern,earnings,liq"))
        self.pro_default_kinds = _csv(os.getenv(
            "PRO_DEFAULT_KINDS", "cryptoliq,liqmap,liqattack,twap,cryptovol,new_big,whale_fill"))
        self.support_contact = os.getenv("SUPPORT_CONTACT", "")
        # Ödeme (env-only, chat id kuralı gibi): botun HL adresi (USDC 'Send' hedefi),
        # NOWPayments anahtarları — boşsa o ödeme yolu menüde görünmez
        self.pay_hl_address = os.getenv("PAY_HL_ADDRESS", "").strip().lower()
        self.nowpayments_api_key = os.getenv("NOWPAYMENTS_API_KEY", "").strip()
        self.nowpayments_ipn_secret = os.getenv("NOWPAYMENTS_IPN_SECRET", "").strip()

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
