"""Ortam değişkenlerinden okunan uygulama ayarları."""
import os


def _csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


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
        self.whale_alert_notional = float(os.getenv("WHALE_ALERT_NOTIONAL", "250000"))
        self.fresh_wallet_days = int(os.getenv("FRESH_WALLET_DAYS", "7"))
        self.recent_deposit_hours = int(os.getenv("RECENT_DEPOSIT_HOURS", "72"))
        self.eval_move_threshold = float(os.getenv("EVAL_MOVE_THRESHOLD", "2.0"))  # %
        self.leaderboard_top = int(os.getenv("LEADERBOARD_TOP", "500"))
        self.scan_max_candidates = int(os.getenv("SCAN_MAX_CANDIDATES", "600"))
        self.scan_concurrency = int(os.getenv("SCAN_CONCURRENCY", "8"))
        self.fills_lookback_days = int(os.getenv("FILLS_LOOKBACK_DAYS", "30"))

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


_cfg: Config | None = None


def get_config() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = Config()
    return _cfg
