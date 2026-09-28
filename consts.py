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

_paper_bot_cfg = _paths_cfg["paper_bot_dir"]
if _paper_bot_cfg:
    _p = Path(_paper_bot_cfg)
    if not _p.is_absolute():
        _p = (BASE_DIR / _p).resolve()
    PAPER_BOT_DIR = _p if _p.exists() else (PARENT_DIR / "cron3Papper").resolve()
else:
    PAPER_BOT_DIR = (PARENT_DIR / "cron3Papper").resolve()
ANALYTICS_DIR = BASE_DIR / "ANALYTICS"
CACHE_DIR = BASE_DIR / "CACHE"

# Создаем директории если их нет
ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

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
_num_symbols: int = len(_CFG["symbols"])
_concurrent_multiplier: int = max(1, (_num_symbols - 1) // 5 + 1)
API_CONCURRENT_RATE_LIMIT_SEC: float = min(0.05, _concurrent_multiplier * 0.01)
REST_FAILSAFE_SEC: float = float(_CFG["app"]["rest_failsafe_sec"])
INCOME_PAGINATION_DELAY_SEC: float = float(_CFG["app"]["income_pagination_delay_sec"])
POST_CLOSE_SYNC_DEBOUNCE_SEC: float = float(_CFG["app"]["post_close_sync_debounce_sec"])
BG_UNREALIZED_POLL_FREQ_SEC: float = float(_CFG["app"]["bg_unrealized_poll_freq_sec"])
ANALYTICS_START_BALANCE_USDT: float = float(_CFG["analytics"]["start_balance_usdt"])
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
# SIGNAL & SMART GRACE DECLARATIONS
# ============================================================
DEFAULT_TIMEFRAME: str = str(_CFG["signal"]["timeframe"])
DEFAULT_SMART_GRACE: Dict[str, Any] = _CFG["signal"]["smart_grace"]
DEFAULT_START_PERIOD_SEC: float = float(DEFAULT_SMART_GRACE["start_period_sec"])
DEFAULT_SHIFT_INTERVAL_SEC: int = int(DEFAULT_SMART_GRACE["shift_interval_sec"])

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
