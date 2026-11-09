# ==============================================================================
# Path: CORE/SERVICES/coin_grid_analytics/__init__.py
# Role: Институциональный пакет трекинга и аналитики сеточных усреднений.
#       Публичный фасад сервиса CoinGridRiskAnalytics.
# ==============================================================================

from .cycle_tracker import CoinGridRiskAnalytics
from .grid_schema import (
    GridLevelSchema,
    RiskThresholds,
    DEFAULT_LEVEL_METADATA,
    load_level_metadata,
    create_empty_coin_record,
)
from .summary_metrics import build_summary
from .merge_service import merge_analytics
from .table_formatters import (
    format_grid_distribution_table,
    format_per_coin_table,
    format_worst_drawdowns_table,
    format_ascii_table,
)

__all__ = [
    "CoinGridRiskAnalytics",
    "GridLevelSchema",
    "RiskThresholds",
    "DEFAULT_LEVEL_METADATA",
    "load_level_metadata",
    "create_empty_coin_record",
    "build_summary",
    "merge_analytics",
    "format_grid_distribution_table",
    "format_per_coin_table",
    "format_worst_drawdowns_table",
    "format_ascii_table",
]

