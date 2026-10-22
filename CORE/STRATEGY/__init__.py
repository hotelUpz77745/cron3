# CORE/STRATEGY/__init__.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.STRATEGY
ROLE: Чистый экспорт сущностей паспорта стратегий HRON_3
==============================================================================
"""

from CORE.STRATEGY.strategy_config import StrategyConfig, deep_merge_dict
from CORE.STRATEGY.first_order_regime import FirstOrderRegimeManager
from CORE.STRATEGY.peeling_manager import PeelingManager
from CORE.STRATEGY.cross_recycler import CrossSideProfitRecycler
from CORE.STRATEGY.trailing_system import BaseTrailingSystem
from CORE.STRATEGY.brownian_noise import BrownianNoiseController
from CORE.STRATEGY.momentum_engine import MomentumEngine
from CORE.STRATEGY.trend_size_depender import TrendSizeDepender, TrendCalculator

__all__ = [
    "StrategyConfig",
    "StrategyRegistry",
    "StrategyApplicator",
    "deep_merge_dict",
    "FirstOrderRegimeManager",
    "PeelingManager",
    "CrossSideProfitRecycler",
    "BaseTrailingSystem",
    "BrownianNoiseController",
    "MomentumEngine",
    "TrendSizeDepender",
    "TrendCalculator",
]


def __getattr__(name: str):
    if name == "StrategyRegistry":
        from CORE.strategy_registry import StrategyRegistry
        return StrategyRegistry
    elif name == "StrategyApplicator":
        from CORE.strategy_applicator import StrategyApplicator
        return StrategyApplicator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

