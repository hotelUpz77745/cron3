# tests/test_clean_architecture_integrity.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: tests.test_clean_architecture_integrity
ROLE: System integrity tests verifying clean architectural decomposition
==============================================================================
"""

import unittest
from pathlib import Path

CRON3_DIR = Path(__file__).resolve().parent.parent


class TestCleanArchitectureIntegrity(unittest.TestCase):
    """
    Проверка архитектурной чистоты проекта:
    1. Исключены зомби-файлы, прокладки и папки обратной совместимости.
    2. Все доменные сущности стратегии импортируются строго из CORE.STRATEGY.
    3. Все сервисы импортируются строго из CORE.SERVICES.
    4. Топология директорий строго соответствует утвержденному плану Тим Лида Самсонова.
    """

    def test_obsolete_files_and_directories_do_not_exist(self):
        """Проверяет полное отсутствие старых путей и зомби-модулей."""
        forbidden_paths = [
            CRON3_DIR / "CORE" / "ADVANCED",
            CRON3_DIR / "tools",
            CRON3_DIR / "CORE" / "auto_closer.py",
            CRON3_DIR / "CORE" / "notifier.py",
            CRON3_DIR / "CORE" / "ENTRY" / "brownian_noise.py",
            CRON3_DIR / "CORE" / "ENTRY" / "leverage_manager.py",
            CRON3_DIR / "CORE" / "GRID" / "peeling_manager.py",
            CRON3_DIR / "CORE" / "STRATEGY" / "canonical_strategy_processor.py",
        ]
        for p in forbidden_paths:
            self.assertFalse(p.exists(), f"Запрещенный устаревший путь не должен существовать: {p}")

    def test_strategy_domain_clean_exports(self):
        """Проверяет экспорт всех сущностей паспорта стратегий из CORE.STRATEGY."""
        import CORE.STRATEGY as strat_domain

        expected_symbols = [
            "StrategyRegistry", "StrategyConfig", "StrategyApplicator",
            "FirstOrderRegimeManager", "PeelingManager", "CrossSideProfitRecycler",
            "BaseTrailingSystem", "BrownianNoiseController", "MomentumEngine",
            "TrendSizeDepender"
        ]
        for sym in expected_symbols:
            self.assertTrue(
                hasattr(strat_domain, sym),
                f"CORE.STRATEGY обязан экспортировать {sym}"
            )

    def test_services_domain_clean_exports(self):
        """Проверяет экспорт ключевых сервисов из CORE.SERVICES."""
        import CORE.SERVICES as serv_domain

        expected_services = [
            "AutoCloser", "NotifierManager", "RuntimeBackupService", "LeverageAdapter"
        ]
        for serv in expected_services:
            self.assertTrue(
                hasattr(serv_domain, serv),
                f"CORE.SERVICES обязан экспортировать {serv}"
            )

    def test_volatility_services_imports(self):
        """Проверяет модули домена волатильности CORE.SERVICES.VOLATILITY."""
        from CORE.SERVICES.VOLATILITY import (
            VolatilityManager, VolatilityScanner, ATRProvider, KlinesCache
        )
        self.assertIsNotNone(VolatilityManager)
        self.assertIsNotNone(VolatilityScanner)
        self.assertIsNotNone(ATRProvider)
        self.assertIsNotNone(KlinesCache)

    def test_tools_services_imports(self):
        """Проверяет модули вспомогательных инструментов CORE.SERVICES.TOOLS."""
        from CORE.SERVICES.TOOLS import (
            calculate_grid_volumes, audit_and_cleanup, restore_missing_tp
        )
        self.assertTrue(callable(calculate_grid_volumes))
        self.assertTrue(callable(audit_and_cleanup))
        self.assertTrue(callable(restore_missing_tp))


if __name__ == "__main__":
    unittest.main()
