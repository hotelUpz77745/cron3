# CORE/STRATEGY/__init__.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.STRATEGY
ROLE: Чистый экспорт сущностей паспорта стратегий HRON_3
==============================================================================
"""

from CORE.strategy_registry import StrategyConfig, StrategyRegistry
from CORE.strategy_applicator import StrategyApplicator
from CORE.STRATEGY.first_order_regime import FirstOrderRegimeManager
from CORE.STRATEGY.peeling_manager import PeelingManager
from CORE.STRATEGY.cross_recycler import CrossSideProfitRecycler, CrossRecycler
from CORE.STRATEGY.trailing_system import BaseTrailingSystem
from CORE.STRATEGY.brownian_noise import BrownianNoiseController
from CORE.STRATEGY.momentum_engine import MomentumEngine
from CORE.STRATEGY.trend_size_depender import TrendSizeDepender, TrendCalculator

__all__ = [
    "StrategyConfig",
    "StrategyRegistry",
    "StrategyApplicator",
    "FirstOrderRegimeManager",
    "PeelingManager",
    "CrossSideProfitRecycler",
    "CrossRecycler",
    "BaseTrailingSystem",
    "BrownianNoiseController",
    "MomentumEngine",
    "TrendSizeDepender",
    "TrendCalculator",
]
