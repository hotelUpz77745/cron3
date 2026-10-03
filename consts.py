# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\consts.py
# Role: consts.py module

# ==============================================================================
# Path: consts.py
# Role: Глобальные константы и настройки конфигурации
# ==============================================================================

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "CFG"
CFG_PATH = DATA_DIR / "app.json"

load_dotenv(BASE_DIR / ".env")

def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # намеренно без логгера, чтобы не было циклов импорта
        return {}

_CFG: Dict[str, Any] = _read_json(CFG_PATH)

import sys
PARENT_DIR = BASE_DIR.parent

# Гарантируем наличие BASE_DIR и PARENT_DIR в sys.path (запуск изнутри и извне)
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

# Динамически подключаем внешнюю папку API (с поддержкой относительных путей и автопоиска)
_paths_cfg = _CFG["paths"]
_shared_api_cfg = _paths_cfg["shared_api_dir"]
if _shared_api_cfg:
    _p = Path(_shared_api_cfg)
    if not _p.is_absolute():
        _p = (BASE_DIR / _p).resolve()
    SHARED_API_DIR = _p if _p.exists() else (PARENT_DIR / "API").resolve()
else:
    SHARED_API_DIR = (PARENT_DIR / "API").resolve()

_api_parent = str(SHARED_API_DIR.parent)
if _api_parent not in sys.path:
    sys.path.insert(0, _api_parent)

ANALYTICS_DIR = BASE_DIR / "ANALYTICS"
ANALYTICS_STATS_DIR = ANALYTICS_DIR / "STATS"
ANALYTICS_LEDGERS_DIR = ANALYTICS_DIR / "LEDGERS"
CACHE_DIR = BASE_DIR / "CACHE"
RUNTIME_DIR = DATA_DIR / "runtime"

# Создаем директории если их нет
ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
ANALYTICS_STATS_DIR.mkdir(parents=True, exist_ok=True)
ANALYTICS_LEDGERS_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# SECRETS (.env only)
# ============================================================
API_KEY: str = os.getenv("API_KEY") or ""
API_SECRET: str = os.getenv("API_SECRET") or ""
TG_TOKEN: str = os.getenv("TG_TOKEN") or ""



# ============================================================
# APP / UTILS
# ============================================================
TG_ENABLED: bool = bool(_CFG["telegram"]["enabled"])
TG_ALLOWED_USERS: list[int] = _CFG["telegram"]["allowed_users"]
TIME_ZONE: str = str(_CFG["app"]["time_zone"])
PRECISION: int = int(_CFG["app"]["precision"])
SPEC_TTL_SEC: float = float(_CFG["app"]["spec_ttl_sec"])
TIME_SLACK_SEC: float = float(_CFG["app"]["time_slack_sec"])
REQ_TIMEOUT_SEC: float = float(_CFG["app"]["req_timeout_sec"])
AVOID_CHECK_RUNTIME_CFG: bool = bool(_CFG["app"]["avoid_check_runtime_cfg"])
API_RATE_LIMIT_SEC: float = float(_CFG["app"]["api_rate_limit_sec"])
ACTIVE_STRATEGIES: Dict[str, Any] = _CFG["active_strategies"]

def _resolve_active_symbols() -> list[str]:
    active_syms: list[str] = []
    seen: Dict[str, str] = {}
    for strat_id, sdata in ACTIVE_STRATEGIES.items():
        if sdata.get("enabled", False):
            for sym in sdata.get("symbols", []):
                sym_clean = str(sym).upper().strip()
                if not sym_clean:
                    continue
                if sym_clean in seen:
                    raise ValueError(
                        f"[FATAL CONFIG ERROR] КОНФЛИКТ ИНВАРИАНТА 1:1: Монета '{sym_clean}' "
                        f"одновременно закреплена за двумя активными стратегиями: '{seen[sym_clean]}' и '{strat_id}'. "
                        f"Между активными вселенными НЕ МОЖЕТ быть одинаковых монет! Запуск бота аварийно остановлен."
                    )
                seen[sym_clean] = strat_id
                active_syms.append(sym_clean)
    return active_syms

ACTIVE_SYMBOLS: list[str] = _resolve_active_symbols()
_num_symbols: int = len(ACTIVE_SYMBOLS)
_concurrent_multiplier: int = max(1, (_num_symbols - 1) // 5 + 1)
API_CONCURRENT_RATE_LIMIT_SEC: float = min(0.05, _concurrent_multiplier * 0.01)
REST_FAILSAFE_SEC: float = float(_CFG["app"]["rest_failsafe_sec"])
INCOME_PAGINATION_DELAY_SEC: float = float(_CFG["app"]["income_pagination_delay_sec"])
POST_CLOSE_SYNC_DEBOUNCE_SEC: float = float(_CFG["app"]["post_close_sync_debounce_sec"])
BG_UNREALIZED_POLL_FREQ_SEC: float = float(_CFG["app"]["bg_unrealized_poll_freq_sec"])

def _resolve_analytics_start_balance() -> float:
    if "analytics" in _CFG and "start_balance_usdt" in _CFG["analytics"]:
        return float(_CFG["analytics"]["start_balance_usdt"])
    total = 0.0
    for strat_id, sdata in _CFG.get("active_strategies", {}).items():
        if isinstance(sdata, dict) and sdata.get("enabled"):
            if "analytics" in sdata and "start_balance_usdt" in sdata["analytics"]:
                total += float(sdata["analytics"]["start_balance_usdt"])
            elif "start_balance_usdt" in sdata:
                total += float(sdata["start_balance_usdt"])
    return total if total > 0 else 150.0

ANALYTICS_START_BALANCE_USDT: float = _resolve_analytics_start_balance()
WATCHDOG_TIMEOUT_SEC: int = int(_CFG["watchdog"]["timeout_sec"])
WATCHDOG_CHECK_INTERVAL_SEC: int = int(_CFG["watchdog"]["check_interval_sec"])
WATCHDOG_HEARTBEAT_INTERVAL_SEC: int = int(_CFG["watchdog"]["heartbeat_interval_sec"])
WATCHDOG_HEARTBEAT_AUTODELETE_SEC: int = int(_CFG["watchdog"]["heartbeat_autodelete_sec"])
BACKUP_ENABLED: bool = bool(_CFG["backup"]["enabled"])
BACKUP_DEBOUNCE_SEC: int = int(_CFG["backup"]["debounce_sec"])
BACKUP_MAX_INTERVAL_SEC: int = int(_CFG["backup"]["max_interval_sec"])
STREAMER_PORT: int = int(_CFG["streamer"]["port"])
STREAMER_LOCK_NAME: str = str(_CFG["streamer"]["lock_name"])

# ============================================================
# SIGNAL, SMART GRACE, SMART SIZE & SMART TP FALLBACK DECLARATIONS
# ============================================================
_SIG_CFG: Dict[str, Any] = _CFG.get("signal") or {}
DEFAULT_TIMEFRAME: str = str(_SIG_CFG.get("timeframe", "5m"))
DEFAULT_SMART_GRACE: Dict[str, Any] = _SIG_CFG.get("smart_grace") or {
    "start_period_sec": 57,
    "shift_interval_sec": 13,
    "eval_window_sec": 300,
    "increments_card": {
        "<2": 0,
        ">=2": 120,
        ">4": 150
    }
}
DEFAULT_START_PERIOD_SEC: float = float(DEFAULT_SMART_GRACE.get("start_period_sec", 57.0))
DEFAULT_SHIFT_INTERVAL_SEC: int = int(DEFAULT_SMART_GRACE.get("shift_interval_sec", 13))
DEFAULT_SMART_SIZE: Dict[str, Any] = _SIG_CFG.get("smart_size") or {
    "eval_window_sec": 300,
    "knife_blocker_enabled": False,
    "knife_blocker": {"enabled": False, "threshold": 2},
    "profiting_size_weights": {"<2": 1.0, ">=2": 1.0, ">4": 1.0}
}
DEFAULT_SMART_TP: Dict[str, Any] = _SIG_CFG.get("smart_tp") or {
    "eval_window_sec": 300,
    "increments_tp": {"<2": 1.0, ">=2": 1.0, ">4": 1.0}
}

# ============================================================
# LOGGING
# ============================================================
LOG_DEBUG: bool = bool(_CFG["logging"]["debug"])
LOG_INFO: bool = bool(_CFG["logging"]["info"])
LOG_WARNING: bool = bool(_CFG["logging"]["warning"])
LOG_ERROR: bool = bool(_CFG["logging"]["error"])
MAX_LOG_LINES: int = int(_CFG["logging"]["max_log_lines"])
LOG_TO_CONSOLE: bool = bool(_CFG["logging"]["log_to_console"])
LOG_TO_FILE: bool = bool(_CFG["logging"]["log_to_file"])

# ============================================================
# TRADE VELOCITY
# ============================================================
TRADE_VELOCITY_CFG: dict = _CFG.get("trade_velocity", {})
TRADE_VELOCITY_ENABLED: bool = bool(TRADE_VELOCITY_CFG.get("enabled", True))
TRADE_VELOCITY_TIMEFRAME_HOURS: float = float(TRADE_VELOCITY_CFG.get("timeframe_hours", 6.0))
TRADE_VELOCITY_MIN_ACCUMULATION_HOURS: float = float(TRADE_VELOCITY_CFG.get("min_accumulation_hours", 1.0))
TRADE_VELOCITY_BENCHMARK_TRADES: float = float(TRADE_VELOCITY_CFG.get("benchmark_trades_per_coin", 10.0))
TRADE_VELOCITY_TOP_SLICE: Optional[int] = TRADE_VELOCITY_CFG.get("top_coins_slice")
TRADE_VELOCITY_REPORT_INTERVAL_HOURS: float = float(TRADE_VELOCITY_CFG.get("report_interval_hours", 6.0))
_tv_notif = TRADE_VELOCITY_CFG.get("notifications", {})
TRADE_VELOCITY_NOTIF_ENABLED: bool = bool(_tv_notif.get("enabled", True))
TRADE_VELOCITY_LOW_RATIO: float = float(_tv_notif.get("low_ratio_threshold", 0.25))
TRADE_VELOCITY_LOW_RECOVERY_RATIO: float = float(_tv_notif.get("low_recovery_ratio", 0.75))
TRADE_VELOCITY_HIGH_RATIO: float = float(_tv_notif.get("high_ratio_threshold", 1.0))
TRADE_VELOCITY_HIGH_RESET_RATIO: float = float(_tv_notif.get("high_reset_ratio", 0.75))
TRADE_VELOCITY_NOTIF_NEG_FLAG: bool = bool(_tv_notif.get("negative_flag", False))
TRADE_VELOCITY_NOTIF_POS_FLAG: bool = bool(_tv_notif.get("positive_flag", False))

# ============================================================
# RISK SYSTEM
# ============================================================
RISK_SYSTEM_CFG: dict = _CFG.get("risk_system", {})
RISK_SYSTEM_ENABLED: bool = bool(RISK_SYSTEM_CFG.get("enabled", False))
RISK_SYSTEM_MIN_BALANCE: Optional[float] = float(RISK_SYSTEM_CFG["min_balance_threshold_usdt"]) if RISK_SYSTEM_CFG.get("min_balance_threshold_usdt") is not None else None

