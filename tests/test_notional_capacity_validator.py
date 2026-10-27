# ==============================================================================
# Path: tests/test_notional_capacity_validator.py
# Role: Unit-тесты институционального пре-трейд валидатора достаточности капитала (min notional >= 7.0 USDT)
# ==============================================================================

import copy
import unittest
from unittest.mock import MagicMock

from CORE.strategy_registry import StrategyRegistry
from CORE.SERVICES.notional_validator import NotionalCapacityValidator


class TestNotionalCapacityValidator(unittest.TestCase):
    """
    Тестирование пре-трейд валидатора NotionalCapacityValidator:
    - Проверка текущей боевой конфигурации (200.0 USDT) -> PASS (0 ошибок)
    - Проверка пониженного капитала (100.0 USDT) -> FAIL (нарушение min notional 7.0 USDT на L0 и L3-пилинге)
    - Проверка прерывания запуска (validate_or_abort -> ValueError)
    - Проверка флага отключения (check_min_notional=False)
    - Проверка разрешения L0 объема из пресетов паспорта без скрытых дефолтов
    """

    def setUp(self):
        self.registry = StrategyRegistry()
        active = self.registry.get_active_strategies()
        self.assertTrue(len(active) > 0, "Должна быть как минимум одна активная стратегия")
        self.strategy = next(iter(active.values()))

    def test_current_configuration_passes(self):
        """Текущая конфигурация с invest_size=200.0 USDT обязана проходить без ошибок."""
        errors = NotionalCapacityValidator.validate_strategy(self.strategy, min_notional=7.0)
        self.assertEqual(len(errors), 0, f"Ожидалось 0 ошибок, получено: {errors}")

    def test_100_usdt_capital_fails(self):
        """
        При установке капитала в 100.0 USDT система ДОЛЖНА обнаружить нарушения:
        - LONG L0: 6.0% = 6.00 USDT < 7.00 USDT
        - LONG L3 Half-Peel: 11.0% * 0.5 = 5.50 USDT < 7.00 USDT
        - SHORT L1: 6.0% = 6.00 USDT < 7.00 USDT
        - SHORT L3 Half-Peel: 10.0% * 0.5 = 5.00 USDT < 7.00 USDT
        """
        strat_copy = copy.deepcopy(self.strategy)
        strat_copy.active_settings["LONG"]["default_invest_size"] = 100.0
        strat_copy.active_settings["LONG"]["invest_size"] = {sym: 100.0 for sym in strat_copy.symbols}
        strat_copy.active_settings["SHORT"]["default_invest_size"] = 100.0
        strat_copy.active_settings["SHORT"]["invest_size"] = {sym: 100.0 for sym in strat_copy.symbols}
        strat_copy.invest_size_long = 100.0
        strat_copy.invest_size_short = 100.0

        errors = NotionalCapacityValidator.validate_strategy(strat_copy, min_notional=7.0)
        self.assertGreater(len(errors), 0, "Капитал 100 USDT обязан не пройти валидацию!")

        # Проверяем наличие ошибок по уровню со сниженным объемом
        err_text = "\n".join(errors)
        self.assertTrue("6.50 USDT" in err_text or "6.00 USDT" in err_text)
        self.assertTrue("SHORT" in err_text or "LONG" in err_text)

    def test_validate_or_abort_raises_value_error(self):
        """Метод validate_or_abort обязан выбрасывать ValueError при недостаточном капитале."""
        strat_copy = copy.deepcopy(self.strategy)
        strat_copy.active_settings["LONG"]["default_invest_size"] = 50.0
        strat_copy.active_settings["LONG"]["invest_size"] = {sym: 50.0 for sym in strat_copy.symbols}
        strat_copy.invest_size_long = 50.0

        mock_registry = MagicMock()
        mock_registry.get_active_strategies.return_value = {"mock_strat": strat_copy}

        with self.assertRaises(ValueError) as ctx:
            NotionalCapacityValidator.validate_or_abort(
                mock_registry,
                min_notional=7.0,
                is_enabled=True,
            )
        self.assertIn("INSUFFICIENT CAPITAL FOR MIN NOTIONAL", str(ctx.exception))

    def test_disabled_validation_bypasses(self):
        """Если is_enabled=False, валидация пропускается без исключений."""
        mock_registry = MagicMock()
        mock_registry.get_active_strategies.side_effect = Exception("Should not be called")
        # Не должно вызывать исключений
        NotionalCapacityValidator.validate_or_abort(
            mock_registry,
            min_notional=7.0,
            is_enabled=False,
        )

    def test_resolve_l0_min_volume_from_passport_presets(self):
        """Проверка чистого извлечения минимального объема L0 из паспорта стратегии."""
        passport_regime = {
            "enabled": True,
            "presets": {
                "bullish": {"LONG": 12.0, "SHORT": 7.0},
                "neutral": {"LONG": 9.0, "SHORT": 9.0},
                "bearish": {"LONG": 6.0, "SHORT": 13.0},
            },
        }
        min_long = NotionalCapacityValidator.resolve_l0_min_volume(passport_regime, "LONG")
        min_short = NotionalCapacityValidator.resolve_l0_min_volume(passport_regime, "SHORT")
        self.assertEqual(min_long, 6.0)
        self.assertEqual(min_short, 7.0)

        # Без пресетов возвращается None (без скрытых дефолтов)
        self.assertIsNone(NotionalCapacityValidator.resolve_l0_min_volume({}, "LONG"))
        self.assertIsNone(NotionalCapacityValidator.resolve_l0_min_volume(None, "SHORT"))


if __name__ == "__main__":
    unittest.main()
