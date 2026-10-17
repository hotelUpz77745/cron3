# CORE/__init__.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE
ROLE: Core orchestration domain and high-level controllers
==============================================================================
"""

from CORE.strategy_registry import StrategyConfig, StrategyRegistry
from CORE.strategy_applicator import StrategyApplicator

__all__ = [
    "StrategyConfig",
    "StrategyRegistry",
    "StrategyApplicator",
]
