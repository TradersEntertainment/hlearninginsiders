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
    "alert_min_score": {"type": "int", "label": "Bildirim için min şüphe skoru", "group": "Bildirimler",
                        "desc": "Yeni büyük pozisyon bildirimi için gereken minimum skor (0 = hepsi)"},
    "notify_liqmap": {"type": "bool", "label": "🧲 Likidasyon duvarı (küme)", "group": "Bildirimler",
                      "desc": "Fiyata yakın bölgede TOPLAMDA büyük liq yığını birikince haber (cascade/stop avı mıknatısı)"},
    "notify_track": {"type": "bool", "label": "👣 Pozisyon kapanış takibi", "group": "Bildirimler",
                     "desc": "Earnings geçince 'takip edelim mi?' teklifi + takipteki balina pozunu kapadıkça haber"},
    "track_step_pct": {"type": "float", "label": "Takip bildirim adımı (%)", "group": "Bildirimler",
                       "desc": "Takipteki poz, toplam boyutun bu yüzdesi kadar değişmeden bildirim GELMEZ (spam önleyici)"},
    "track_expire_days": {"type": "int", "label": "Takip süresi (gün)", "group": "Bildirimler",
                          "desc": "Takip bu kadar gün sonra kendiliğinden biter (poz hâlâ açıksa haber verir)"},
    "notify_lowvol": {"type": "bool", "label": "🐘 Sessiz su devi", "group": "Bildirimler",
                      "desc": "Düşük hacimli hissede absürt boyutlu YENİ pozisyon açılınca haber (eşik aşağıda ayrı ayarda)"},
    "notify_health": {"type": "bool", "label": "⚕️ Sistem sağlığı", "group": "Bildirimler",
                      "desc": "VARSAYILAN KAPALI — sağlık olayları zaten ana sayfa rozetinde + /saglik + /health'te görünür; Telegram'a da istersen aç"},
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
    "liq_cluster_min_usd": {"type": "float", "label": "Duvar eşiği ($)",
                            "group": "Likidasyon radarı", "desc": "NORMAL hisselerde pencere içi toplam liq bu boyutu aşarsa bildirim"},
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
    "hourstats_days": {"type": "int", "label": "Saat istatistiği penceresi (gün)",
                       "group": "Tarama & performans",
                       "desc": "Saatlik getiri haritası için geriye bakılacak gün sayısı (1h mumlar)"},
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
                           "group": "Tarama & performans", "desc": "Süpürme turları arası bekleme — varsayılanla ~1500 adres 13 dakikada bir tam tur döner"},
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
        self.track_poll_sec = int(os.getenv("TRACK_POLL_SEC", "120"))
        self.hl_max_rpm = int(os.getenv("HL_MAX_RPM", "350"))
        self.hourstats_days = int(os.getenv("HOURSTATS_DAYS", "90"))
        # "Saati gelenler" yayın kanalı — kişisel chat'ten AYRI (bot kanala admin olmalı)
        self.telegram_channel_id = os.getenv("TELEGRAM_CHANNEL_ID", "")
        self.sweep_leaderboard_top = int(os.getenv("SWEEP_LEADERBOARD_TOP", "1500"))
        self.sweep_batch_size = int(os.getenv("SWEEP_BATCH_SIZE", "40"))
        self.sweep_interval_sec = int(os.getenv("SWEEP_INTERVAL_SEC", "90"))
        self.notify_lowvol = True
        self.notify_wall = True
        self.notify_health = False  # bekçi sitede konuşur; Telegram istenirse açılır
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

        # Korele hisseler: "SNDK:WDC|MU;TSLA:RIVN" formatıyla override edilebilir
        self.peers_override = os.getenv("PEERS", "")
        # ABD dışı hisselerin Yahoo sembolleri (varsayılan eşlemeye eklenir)
        self.yahoo_symbol_map = os.getenv("YAHOO_SYMBOL_MAP", "")
        # propr.xyz'de listeli ek semboller (varsayılan listeye eklenir)
        self.propr_symbols = os.getenv("PROPR_SYMBOLS", "")
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
