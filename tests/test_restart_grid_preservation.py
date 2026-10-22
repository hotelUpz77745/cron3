import sys
import unittest
import copy
import tempfile
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
from unittest.mock import AsyncMock, MagicMock
from POS_FSM.models import PositionState
from CORE.GRID.avg_manager import AverageManager
from RUNTIME_FSM.runtime_manager import RuntimeFsmManager
from CORE.strategy_applicator import StrategyApplicator
from CORE.strategy_registry import StrategyConfig
from CORE.STRATEGY.trailing_system import BaseTrailingSystem


class TestRestartGridPreservation(unittest.IsolatedAsyncioTestCase):
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

    async def test_03_averaging_flow_then_restart_prevents_duplicate_re_averaging(self):
        """
        ЖЕЛЕЗОБЕТОННАЯ ПРОВЕРКА:
        1. Позиция открыта, усреднилась по L1 (L0 и L1 активны: is_active=True).
        2. Происходит рестарт: кэш с диска загружается через build_runtime_for_symbol и load_cache_to_fsm.
        3. Проверяем, что все флаги состояний (is_active, peeled, peeled_count) загружены в рантайм.
        4. Повторный тик цены на уровне L1 НЕ вызывает повторного ордера усреднения.
        """
        applicator = StrategyApplicator()

        with tempfile.TemporaryDirectory() as tmp_dir:
            target_dir = Path(tmp_dir) / "u17_test"
            target_dir.mkdir(parents=True, exist_ok=True)
            runtime_file = target_dir / "strkusdt.json"

            # Состояние после усреднения L1 на бирже:
            # L0 ($48, 12%) + L1 ($32, 8%) = $80 total notional, entry 0.098
            active_cache = {
                "LONG": {
                    "in_position": True,
                    "total_volume": 816.3,
                    "avg_entry_price": 0.098,
                    "initial_entry_price": 0.100,
                    "invest_size": 400.0,
                    "eff_invest": 400.0,
                    "grid": {
                        "0": {"indent": 0.0, "volume": 12.0, "is_active": True, "fill_price": 0.100, "fill_qty": 480.0, "peeled": False, "peeled_count": 0, "enabled": True},
                        "1": {"indent": -3.5, "volume": 8.0, "is_active": True, "fill_price": 0.0965, "fill_qty": 336.3, "peeled": False, "peeled_count": 0, "enabled": True},
                        "2": {"indent": -7.5, "volume": 9.0, "is_active": False, "peeled": False, "peeled_count": 0, "enabled": True},
                        "3": {"indent": -13.5, "activation": -15.0, "volume": 11.0, "is_active": False, "peeled": False, "peeled_count": 0, "enabled": True},
                    },
                    "tp_map": {
                        "0": {"indent": 1.2, "fallback_indent": 1.5, "is_active": True, "is_armed": False},
                        "1": {"indent": 1.4, "fallback_indent": 1.8, "is_active": True, "is_armed": False},
                    },
                    "trailing_grid_state": {"armed_levels": {}}
                },
                "SHORT": {"in_position": False, "total_volume": 0.0, "grid": {}, "tp_map": {}}
            }
            runtime_file.write_text(json.dumps(active_cache, indent=4), encoding="utf-8")

            # 1. Симулируем рестарт бота и запуск аппликатора
            applicator.build_runtime_for_symbol("STRKUSDT", self.strat, target_dir=target_dir)

            # 2. Инициализируем RuntimeFsmManager и восстанавливаем FSM
            runtime_mgr = RuntimeFsmManager()
            runtime_mgr.caches["STRKUSDT"] = json.loads(runtime_file.read_text(encoding="utf-8"))

            fresh_fsm_states = {"STRKUSDT": {"LONG": PositionState(symbol="STRKUSDT", side="LONG"), "SHORT": PositionState(symbol="STRKUSDT", side="SHORT")}}
            runtime_mgr.populate_fsm_from_cache(fresh_fsm_states)

            restored_state = fresh_fsm_states["STRKUSDT"]["LONG"]

            # Проверяем подтягивание флагов состояний в рантайм:
            self.assertTrue(restored_state.in_position)
            self.assertEqual(restored_state.total_volume, 816.3)
            self.assertTrue(restored_state.grid["0"]["is_active"])
            self.assertTrue(restored_state.grid["1"]["is_active"])
            self.assertFalse(restored_state.grid["1"]["peeled"])
            self.assertEqual(restored_state.grid["1"]["peeled_count"], 0)
            self.assertFalse(restored_state.grid["2"]["is_active"])

            # 3. Моделируем проверку сетки на текущей цене 0.0965 (просадка -3.5%, уровень L1)
            mock_client = MagicMock()
            mock_client.cancel_orders_for_side = AsyncMock(return_value=True)
            mock_client.make_order = AsyncMock(return_value=MagicMock(success=True, order_id="AVG_FAIL"))

            mock_tp = MagicMock()
            mock_tp.place_take_profit = AsyncMock(return_value=True)

            avg_mgr = AverageManager()
            await avg_mgr.process(
                client=mock_client,
                runtime_manager=runtime_mgr,
                symbol="STRKUSDT",
                side="LONG",
                state=restored_state,
                opposite_state=None,
                current_price=0.0965,
                spec_data={"stepSize": "0.1", "tickSize": "0.0001"},
                tp_manager=mock_tp
            )

            # ЖЕЛЕЗОБЕТОННЫЙ АССЕРТ: Никаких ордеров выставлено не было!
            mock_client.make_order.assert_not_called()
            self.assertFalse(restored_state.pending_avg)
            # L1 остался активным, L2 не активирован ложно
            self.assertTrue(restored_state.grid["1"]["is_active"])
            self.assertFalse(restored_state.grid["2"]["is_active"])


if __name__ == "__main__":
    unittest.main()
