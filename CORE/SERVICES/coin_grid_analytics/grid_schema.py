# ==============================================================================
# Path: CORE/SERVICES/coin_grid_analytics/grid_schema.py
# Role: Единый источник истины (Single Source of Truth) для сеточной геометрии,
#       порогов риска и фабрик структур per-coin записей.
# ==============================================================================

from pathlib import Path
import json
import copy
from typing import Dict, Any

try:
    from c_log import UnifiedLogger
    logger = UnifiedLogger("CoinGridAnalytics")
except ImportError:
    import logging
    logger = logging.getLogger("CoinGridAnalytics")


class RiskThresholds:
    """Институциональные пороговые константы риска сеточных усреднений."""
    L8_THRESHOLD_PCT: float = 89.0
    SL_120_THRESHOLD_PCT: float = 120.0
    SL_130_THRESHOLD_PCT: float = 130.0
    EPSILON: float = 1e-8
    CANONICAL_GRID_LEVELS: int = 9
    SECONDS_PER_HOUR: float = 3600.0
    TOP_WORST_EVENTS_LIMIT: int = 15


# Каноническая схема ступеней сетки (Omen-13 Canon)
DEFAULT_LEVEL_METADATA: Dict[str, Dict[str, Any]] = {
    "L0": {"role": "Стартовый вход", "indent_pct": 0.0, "volume_pct": 7.0},
    "L1": {"role": "Зона 1: Первое колено", "indent_pct": -3.0, "volume_pct": 7.7},
    "L2": {"role": "Зона 1: Второе колено", "indent_pct": -5.5, "volume_pct": 8.47},
    "L3": {"role": "Зона 2: Третье колено", "indent_pct": -9.0, "volume_pct": 9.32},
    "L4": {"role": "Зона 2: Четвертое колено", "indent_pct": -15.0, "volume_pct": 10.25},
    "L5": {"role": "Зона 2: Пятое колено", "indent_pct": -24.0, "volume_pct": 11.27},
    "L6": {"role": "Зона 3: Глубинная броня", "indent_pct": -38.0, "volume_pct": 12.4},
    "L7": {"role": "Зона 3: Глубинная броня", "indent_pct": -58.0, "volume_pct": 13.64},
    "L8": {"role": "Зона 3: Предельная броня", "indent_pct": -89.0, "volume_pct": 19.95},
}


def create_empty_coin_record(symbol: str) -> Dict[str, Any]:
    """Единая фабрика для создания структуры метрик per-coin (устранение дублирования)."""
    return {
        "symbol": symbol,
        "total_cycles": 0,
        "winning_cycles": 0,
        "losing_cycles": 0,
        "realized_pnl": 0.0,
        "fees_paid": 0.0,
        "net_pnl": 0.0,
        "max_level_ever": 0,
        "level_depths_sum": 0,
        "max_adverse_from_entry0_pct": 0.0,
        "max_adverse_from_avg_pct": 0.0,
        "max_floating_dd_usdt": 0.0,
        "total_duration_hours": 0.0,
        "exceeded_l8_count": 0,
        "would_hit_sl_120_count": 0,
        "would_hit_sl_130_count": 0,
    }


def load_level_metadata(strategy_id: str = "") -> Dict[str, Dict[str, Any]]:
    """
    Динамически загружает метаданные ступеней сетки из конфигурационного файла
    вселенной стратегии CFG/universes/{strategy_id}.json относительно корня репозитория cron3.
    Исключает зависимость от CWD (P1) и явным образом логирует предупреждения и ошибки парсинга.
    """
    if not strategy_id:
        return copy.deepcopy(DEFAULT_LEVEL_METADATA)

    cron3_root = Path(__file__).resolve().parents[3]
    cfg_path = cron3_root / "CFG" / "universes" / f"{strategy_id}.json"

    if not cfg_path.is_file():
        logger.warning(
            f"[CoinGridAnalytics] Config for strategy '{strategy_id}' not found at '{cfg_path}'. "
            f"Falling back to institutional DEFAULT_LEVEL_METADATA."
        )
        return copy.deepcopy(DEFAULT_LEVEL_METADATA)

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        grid_cfg = cfg.get("LONG", {}).get("grid", {})
        if not grid_cfg:
            grid_cfg = cfg.get("SHORT", {}).get("grid", {})

        if not grid_cfg:
            logger.warning(
                f"[CoinGridAnalytics] No 'grid' definition found in '{cfg_path}'. "
                f"Falling back to institutional DEFAULT_LEVEL_METADATA."
            )
            return copy.deepcopy(DEFAULT_LEVEL_METADATA)

        result: Dict[str, Dict[str, Any]] = {}
        for lvl_str, item in grid_cfg.items():
            lvl_key = f"L{lvl_str}"
            result[lvl_key] = {
                "role": item.get("role", f"Level {lvl_str}"),
                "indent_pct": float(item.get("indent", 0.0)),
                "volume_pct": float(item.get("volume", 0.0)),
            }
        return result
    except Exception as exc:
        logger.error(
            f"[CoinGridAnalytics] Failed to parse universe config '{cfg_path}': {exc}. "
            f"Falling back to institutional DEFAULT_LEVEL_METADATA.",
            exc_info=True,
        )
        return copy.deepcopy(DEFAULT_LEVEL_METADATA)


class GridLevelSchema:
    """Утилитный класс доступа к сеточной схеме и пороговым значениям."""
    Thresholds = RiskThresholds
    DEFAULT_LEVELS = DEFAULT_LEVEL_METADATA
    load = staticmethod(load_level_metadata)
    empty_coin_record = staticmethod(create_empty_coin_record)
