import sys
import unittest
import copy
import tempfile
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from CORE.strategy_applicator import StrategyApplicator
from CORE.strategy_registry import StrategyConfig
from CORE.STRATEGY.trailing_system import BaseTrailingSystem


class TestRestartGridPreservation(unittest.TestCase):
    """
    Юнит-тесты на защиту FSM-состояния сетки при перезапусках:
    1. Геометрия сетки открытой позиции на диске сохраняется и не затирается шаблоном.
    2. Флаг is_active=True является необратимым: перезапуск не сбрасывает исполненные уровни в False.
    3. При глубокой просадке перезапуск не вызывает ложных срабатываний усреднения.
    """

    def setUp(self):
        self.raw_meta = {
            "name": "u17_stephensonE_test",
            "signal_heartbeat_tfr": "5m",
            "LONG": {
                "grid": {
                    "0": {"indent": 0.0, "volume": 12.0, "enabled": True},
                    "1": {"indent": -3.5, "volume": 8.0, "enabled": True},
                    "2": {"indent": -7.5, "volume": 9.0, "enabled": True},
                    "3": {"indent": -13.5, "activation": -15.0, "volume": 11.0, "enabled": True},
                    "4": {"indent": -24.0, "activation": -27.0, "volume": 15.0, "enabled": True},
                    "5": {"indent": -55.0, "activation": -60.0, "volume": 25.0, "enabled": True}
                }
            },
            "SHORT": {
                "grid": {
                    "0": {"indent": 0.0, "volume": 7.0, "enabled": True},
                    "1": {"indent": -3.0, "volume": 6.0, "enabled": True},
                    "2": {"indent": -8.0, "volume": 8.0, "enabled": True},
                    "3": {"indent": -21.0, "activation": -26.0, "volume": 10.0, "enabled": True},
                    "4": {"indent": -55.0, "activation": -65.0, "volume": 14.0, "enabled": True},
                    "5": {"indent": -144.0, "activation": -155.0, "volume": 19.0, "enabled": True}
                }
            }
        }
        self.strat = StrategyConfig(
            strategy_id="u17_test",
            raw_meta=self.raw_meta,
            active_settings={
                "enabled": True,
                "LONG": {"invest_size": 400.0},
                "SHORT": {"invest_size": 400.0}
            }
        )

    def test_active_position_grid_preserved_on_restart(self):
        """Проверяет, что сетка открытой позиции с диска не перезаписывается дефолтами шаблона."""
        applicator = StrategyApplicator()

        with tempfile.TemporaryDirectory() as tmp_dir:
            target_dir = Path(tmp_dir) / "u17_test"
            target_dir.mkdir(parents=True, exist_ok=True)
            runtime_file = target_dir / "icntusdt.json"

            # Имитируем активную SHORT-позицию с уже исполненными L0, L1, L2
            existing_data = {
                "LONG": {"in_position": False, "total_volume": 0.0, "grid": {}},
                "SHORT": {
                    "in_position": True,
                    "total_volume": -437.0,
                    "avg_entry_price": 0.18035,
                    "initial_entry_price": 0.18035,
                    "invest_size": 400.0,
                    "eff_invest": 400.0,
                    "grid": {
                        "0": {"indent": 0.0, "volume": 7.0, "is_active": True, "enabled": True},
                        "1": {"indent": -3.0, "volume": 6.0, "is_active": True, "enabled": True},
                        "2": {"indent": -8.0, "volume": 8.0, "is_active": True, "enabled": True},
                        "3": {"indent": -21.0, "activation": -26.0, "volume": 10.0, "is_active": False, "enabled": True},
                        "4": {"indent": -55.0, "activation": -65.0, "volume": 14.0, "is_active": False, "enabled": True},
                    },
                    "trailing_grid_state": {"armed_levels": {}}
                }
            }
            runtime_file.write_text(json.dumps(existing_data, indent=4), encoding="utf-8")

            # Вызываем build_runtime_for_symbol (симуляция рестарта)
            applicator.build_runtime_for_symbol("ICNTUSDT", self.strat, target_dir=target_dir)

            # Читаем результат
            rebuilt_data = json.loads(runtime_file.read_text(encoding="utf-8"))
            short_grid = rebuilt_data["SHORT"]["grid"]

            # Проверяем:
            # 1. Исполненные ступени 0, 1, 2 остались строго is_active=True
            self.assertTrue(short_grid["0"]["is_active"])
            self.assertTrue(short_grid["1"]["is_active"])
            self.assertTrue(short_grid["2"]["is_active"])

            # 2. Неисполненные ступени остались is_active=False
            self.assertFalse(short_grid["3"]["is_active"])
            self.assertFalse(short_grid["4"]["is_active"])

            # 3. Геометрия сетки сохранена строго (-21%/-26% для L3, -55%/-65% для L4)
            self.assertEqual(short_grid["3"]["indent"], -21.0)
            self.assertEqual(short_grid["3"]["activation"], -26.0)
            self.assertEqual(short_grid["4"]["indent"], -55.0)
            self.assertEqual(short_grid["4"]["activation"], -65.0)

            # 4. Проверяем, что evaluate_trailing_grid при DD = 21.76% НЕ СРАБАТЫВАЕТ ложно!
            # Курок для L3 (-26.0%) не взведен, а L4 далеко внизу (-65.0%)
            candidate_level, skipped, info = BaseTrailingSystem.evaluate_trailing_grid(
                drawdown_pct=21.76,
                grid=short_grid,
                state_trailing=rebuilt_data["SHORT"].get("trailing_grid_state", {})
            )
            self.assertIsNone(candidate_level, "Усреднение не должно срабатывать ложно при просадке 21.76%!")

    def test_is_active_is_irreversible_high_water_mark(self):
        """Проверяет, что уровень, уже помеченный is_active=True, никогда не сбрасывается в False."""
        applicator = StrategyApplicator()

        exist_side = {
            "in_position": True,
            "total_volume": -100.0,
            "avg_entry_price": 1.0,
            "invest_size": 400.0,
            "grid": {
                "1": {"is_active": True, "volume": 6.0},
                "2": {"is_active": True, "volume": 8.0},
                "3": {"is_active": False, "volume": 30.0}
            }
        }
        new_side = {
            "invest_size": 400.0,
            "grid": {
                "1": {"is_active": True, "volume": 6.0},
                "2": {"is_active": False, "volume": 8.0},
                "3": {"is_active": False, "volume": 30.0}
            }
        }

        applicator.resolve_executed_grid_levels(self.strat, "ICNTUSDT", "SHORT", new_side, exist_side)

        # L1 и L2 обязаны остаться True
        self.assertTrue(new_side["grid"]["1"]["is_active"])
        self.assertTrue(new_side["grid"]["2"]["is_active"])
        self.assertFalse(new_side["grid"]["3"]["is_active"])


if __name__ == "__main__":
    unittest.main()
