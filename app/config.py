"""Uygulama ayarları.

Öncelik sırası: dashboard /settings'te kaydedilen değer (DB'de yaşar)
> ortam değişkeni > kod varsayılanı. Gizli anahtarlar (token/key) sadece env'den.
"""
import os


def _csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


# Dashboard'dan canlı değiştirilebilen alanlar
EDITABLE_FIELDS: dict[str, dict] = {
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
    if typ == "int":
        return int(float(str(v).replace(",", ".")))
    if typ == "float":
        return float(str(v).replace(",", "."))
    if typ == "csv":
        return _csv(str(v))
    return str(v)


def display_value(typ: str, v) -> str:
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

        # Periyotlar (saniye)
        self.universe_refresh_sec = int(os.getenv("UNIVERSE_REFRESH_SEC", str(6 * 3600)))
        self.calendar_refresh_sec = int(os.getenv("CALENDAR_REFRESH_SEC", str(12 * 3600)))
        self.metrics_poll_sec = int(os.getenv("METRICS_POLL_SEC", "300"))
        self.due_check_sec = int(os.getenv("DUE_CHECK_SEC", "60"))

        # Dashboard
        self.dashboard_token = os.getenv("DASHBOARD_TOKEN", "")

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
