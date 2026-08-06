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
    "min_fill_notional": {"type": "float", "label": "Min fill boyutu ($)",
                          "desc": "Bu boyut üstü işlemler adres havuzuna yazılır"},
    "whale_alert_notional": {"type": "float", "label": "Anlık balina alert eşiği ($)",
                             "desc": "Bu boyut üstü tek işlemde hemen Telegram alert"},
    "min_position_notional": {"type": "float", "label": "Min pozisyon boyutu ($)",
                              "desc": "Bundan küçük pozisyonlar listelenmez (toz filtresi)"},
    "big_position_usd": {"type": "float", "label": "Büyük pozisyon eşiği ($)",
                         "desc": "Bu boyut üstü pozisyona +10 şüphe puanı"},
    "huge_position_usd": {"type": "float", "label": "Dev pozisyon eşiği ($)",
                          "desc": "Bu boyut üstü pozisyona +20 şüphe puanı"},
    "combo_window_hours": {"type": "int", "label": "İnsider paterni penceresi (saat)",
                           "desc": "Büyük pozisyon + bu pencerede açılış = +15 bonus (varsayılan 72h = 3 gün)"},
    "fresh_big_alert_hours": {"type": "int", "label": "Yeni büyük poz alert penceresi (saat)",
                              "desc": "Bu pencerede açılmış büyük pozisyon bulununca anlık Telegram alert (earnings şartı yok)"},
    "mm_max_positions": {"type": "int", "label": "MM eşiği: açık pozisyon sayısı",
                         "desc": "Bu kadar+ açık pozisyonu olan hesap market maker sayılır (skorlama/alert dışı)"},
    "mm_max_fills_24h": {"type": "int", "label": "MM eşiği: 24h fill sayısı",
                         "desc": "24 saatte bu kadar+ büyük fill yapan çift yönlü hesap MM sayılır"},
    "liq_watch_min_notional": {"type": "float", "label": "Liq radarı: min pozisyon ($)",
                               "desc": "Bu boyut üstü pozisyonlar TÜM dex'lerde likidasyon radarına girer"},
    "liq_watch_poll_sec": {"type": "int", "label": "Liq radarı periyodu (sn)",
                           "desc": "Likidasyon mesafesi kontrol sıklığı (kademeler: %1 → %0.5 → %0.1)"},
    "liq_watch_top_accounts": {"type": "int", "label": "Liq radarı: taranan hesap",
                               "desc": "Leaderboard'dan likidasyon radarına alınan hesap sayısı"},
    "max_liq_distance_pct": {"type": "float", "label": "Liq tablosu mesafe sınırı (%)",
                             "desc": "Likidasyonu bundan uzak pozisyonlar liq tablosuna girmez"},
    "fresh_wallet_days": {"type": "int", "label": "Taze cüzdan eşiği (gün)",
                          "desc": "İlk fonlaması bundan yeni hesaplar 'taze' sayılır (+25 puan)"},
    "recent_deposit_hours": {"type": "int", "label": "Yeni fonlama eşiği (saat)",
                             "desc": "Son fonlaması bundan yeni hesaplar şüpheli (+12 puan)"},
    "eval_move_threshold": {"type": "float", "label": "Sicil için min hareket (%)",
                            "desc": "Earnings sonrası bu kadar hareket yoksa doğru/yanlış işlenmez"},
    "leaderboard_top": {"type": "int", "label": "Leaderboard tohumu (adres)",
                        "desc": "Havuza eklenen en büyük hesap sayısı"},
    "scan_max_candidates": {"type": "int", "label": "Tarama aday limiti",
                            "desc": "T-1h taramasında sorgulanacak maksimum adres"},
    "scan_concurrency": {"type": "int", "label": "Eşzamanlı API isteği",
                         "desc": "HL API paralellik (rate limit'e dikkat)"},
    "equity_dexes": {"type": "csv", "label": "Hisse dex'leri",
                     "desc": "HIP-3 hisse perp dex'leri, virgülle (ör: xyz)"},
    "calendar_horizon_days": {"type": "int", "label": "Takvim ufku (gün)",
                              "desc": "Kaç gün ilerisinin earnings'leri çekilsin"},
    "metrics_poll_sec": {"type": "int", "label": "Metrik periyodu (sn)",
                         "desc": "OI/funding örnekleme sıklığı"},
    "anomaly_poll_sec": {"type": "int", "label": "Anomali kontrol periyodu (sn)",
                         "desc": "OI/funding anomali taraması sıklığı"},
    "oi_spike_pct_event": {"type": "float", "label": "OI spike eşiği - earnings yakın (%)",
                           "desc": "Earnings <72h iken 24h OI artışı alarmı"},
    "oi_spike_pct_normal": {"type": "float", "label": "OI spike eşiği - normal (%)",
                            "desc": "Earnings yokken 24h OI artışı alarmı"},
    "oi_spike_floor_usd": {"type": "float", "label": "OI spike tabanı ($)",
                           "desc": "Bu OI'nin altındaki mikro marketlerde alarm verme"},
    "funding_extreme": {"type": "float", "label": "Aşırı funding eşiği (saatlik)",
                        "desc": "ör: 0.0005 = %0.05/saat"},
    "peers_override": {"type": "str", "label": "Korele hisse override",
                       "desc": "Format: SNDK:WDC|MU;TSLA:RIVN (varsayılan tabloya eklenir)"},
    "yahoo_symbol_map": {"type": "str", "label": "Yahoo sembol eşleme",
                         "desc": "ABD dışı hisseler için: SMSN:005930.KS;SOFTBANK:9984.T formatı (varsayılan eşlemeye eklenir)"},
    "non_equity_extra": {"type": "str", "label": "Bilançosuz enstrümanlar (ek)",
                         "desc": "Endeks/emtia/FX/ETF/kripto — takvim aranmaz. Virgülle: GOLD,EUR"},
    "no_calendar_extra": {"type": "str", "label": "Takvimi olmayan hisseler (ek)",
                          "desc": "Pre-IPO / sentetik hisseler — takvim aranmaz, elle /settime ile girilir"},
    "propr_symbols": {"type": "str", "label": "propr.xyz ek semboller",
                      "desc": "propr yeni bir şey listelerse buraya virgülle ekle — alertlere '✅ PROPR'da listeli' düşer"},
    "universe_refresh_sec": {"type": "int", "label": "Evren yenileme (sn)",
                             "desc": "HL coin listesi yenileme sıklığı"},
    "calendar_refresh_sec": {"type": "int", "label": "Takvim yenileme (sn)",
                             "desc": "Earnings takvimi çekme sıklığı"},
    "auto_scan_interval_sec": {"type": "int", "label": "Oto-tarama periyodu (sn)",
                               "desc": "Arka plan tarayıcısı bu aralıkla sıradaki coini tarar"},
    "scan_stale_min": {"type": "int", "label": "Sayfa bayatlık eşiği (dk)",
                       "desc": "Coin sayfası açıldığında veri bundan eskiyse otomatik tarama başlar"},
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
        self.liq_watch_poll_sec = int(os.getenv("LIQ_WATCH_POLL_SEC", "120"))
        self.liq_watch_top_accounts = int(os.getenv("LIQ_WATCH_TOP_ACCOUNTS", "300"))
        self.max_liq_distance_pct = float(os.getenv("MAX_LIQ_DISTANCE_PCT", "50"))
        self.whale_alert_notional = float(os.getenv("WHALE_ALERT_NOTIONAL", "250000"))
        self.fresh_wallet_days = int(os.getenv("FRESH_WALLET_DAYS", "7"))
        self.recent_deposit_hours = int(os.getenv("RECENT_DEPOSIT_HOURS", "72"))
        self.eval_move_threshold = float(os.getenv("EVAL_MOVE_THRESHOLD", "2.0"))  # %
        self.leaderboard_top = int(os.getenv("LEADERBOARD_TOP", "500"))
        self.scan_max_candidates = int(os.getenv("SCAN_MAX_CANDIDATES", "600"))
        self.scan_concurrency = int(os.getenv("SCAN_CONCURRENCY", "8"))
        self.auto_scan_interval_sec = int(os.getenv("AUTO_SCAN_INTERVAL_SEC", "180"))
        self.scan_stale_min = int(os.getenv("SCAN_STALE_MIN", "10"))
        self.fills_lookback_days = int(os.getenv("FILLS_LOOKBACK_DAYS", "30"))

        # Anomali dedektörü
        self.anomaly_poll_sec = int(os.getenv("ANOMALY_POLL_SEC", "1800"))
        self.oi_spike_pct_event = float(os.getenv("OI_SPIKE_PCT_EVENT", "50"))    # earnings <72h iken
        self.oi_spike_pct_normal = float(os.getenv("OI_SPIKE_PCT_NORMAL", "150"))
        self.oi_spike_floor_usd = float(os.getenv("OI_SPIKE_FLOOR_USD", "200000"))
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
