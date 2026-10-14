# ============================================================
# Path: tests/test_restart_and_grid_recovery.py
# Role: Unit test suite for verifying system readiness against
#       restart / reconnection false DCA triggers, grid level reconciliation,
#       and operator manual intervention safety.
# ============================================================

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from POS_FSM.models import PositionState
from POS_FSM.pos_stream_monitor import PositionMonitor
from CORE.GRID.avg_manager import AverageManager
from CORE.STRATEGY.strategy_applicator import StrategyApplicator
from CORE.STRATEGY.strategy_registry import StrategyConfig


class TestRestartAndGridRecovery(unittest.TestCase):
    """
    Тестовый комплекс боевой готовности:
    1. Изоляция от ложных усреднений при перезапусках бота с открытыми позициями.
    2. Автоматическое определение и маркировка отработанных уровней по биржевому объему.
    3. Сохранение initial_entry_price при обновлениях стрима и REST.
    4. Предохранитель AvgManager от дублирующих ордеров на покрытых ступенях.
    5. Безопасность при ручных срезках объемов оператором.
    6. Корректность восстановления из бэкапа рантайма без сбоев переменных.
    """

    def setUp(self):
        # Эталонная 7-уровневая сетка Chita ($400 капитал)
        # L0: 12% ($48), L1: 8% ($32, cum $80), L2: 10% ($40, cum $120),
        # L3: 13% ($52, cum $172), L4: 17% ($68, cum $240),
        # L5: 25% ($100, cum $340), L6: 15% ($60, cum $400)
        self.grid_template = {
            "0": {"enabled": True, "indent": 0.0, "volume": 12.0, "is_active": False, "is_armed": False},
            "1": {"enabled": True, "indent": -3.0, "volume": 8.0, "is_active": False, "is_armed": False},
            "2": {"enabled": True, "indent": -8.0, "volume": 10.0, "is_active": False, "is_armed": False},
            "3": {"enabled": True, "indent": -21.0, "volume": 13.0, "is_active": False, "is_armed": False},
            "4": {"enabled": True, "indent": -34.0, "volume": 17.0, "is_active": False, "is_armed": False},
            "5": {"enabled": True, "indent": -55.0, "volume": 25.0, "is_active": False, "is_armed": False},
            "6": {"enabled": True, "indent": -340.0, "volume": 15.0, "is_active": False, "is_armed": False},
        }

        self.mock_strategy = MagicMock(spec=StrategyConfig)
        self.mock_strategy.raw_meta = {
            "fsm_level_resolution": {
                "enabled": True,
                "mode": "cumulative_volume_threshold",
                "volume_tolerance_pct": 10.0,
                "auto_mark_executed": True,
            }
        }
        self.mock_strategy.sides = {
            "LONG": {"invest_size": 400.0},
            "SHORT": {"invest_size": 400.0},
        }
        self.mock_strategy.strategy_id = "u16_chita__classic"
        self.mock_strategy.name = "Chita"
        self.mock_strategy.invest_size = 400.0
        self.mock_strategy.leverage = 20
        self.mock_strategy.super_grid_enabled = False
        self.mock_strategy.overrides = {}
        self.mock_strategy.grid = copy.deepcopy(self.grid_template)
        self.mock_strategy.tp_map = {"0": {"indent": 1.2, "fallback_indent": 1.5}}
        self.mock_strategy.min_tp_floor = 0.5
        self.mock_strategy.sizing_mode = "static"
        self.mock_strategy.trend_size_cfg = {"enabled": False}

    def test_01_restart_with_open_position_reconciles_grid(self):
        """
        Проверка: Бот стартует, на бирже висит позиция 1413 монет по $0.085 ($120.1 USDT).
        В памяти или свежем файле все уровни сетки еще False.
        Резолвер обязан мгновенно пометить L0, L1, L2 как исполненные (is_active=True).
        L3..L6 должны остаться нетронутыми (is_active=False).
        """
        state = PositionState(symbol="STRKUSDT", side="LONG")
        state.total_volume = 1413.0
        state.avg_entry_price = 0.085
        state.in_position = True
        state.eff_invest = 400.0
        state.grid = copy.deepcopy(self.grid_template)
        state.trailing_grid_state = {"armed_levels": {"1": {}, "2": {}}}

        updated = StrategyApplicator.resolve_position_state_grid(state, strategy=self.mock_strategy)

        self.assertTrue(updated)
        self.assertTrue(state.grid["0"]["is_active"])
        self.assertFalse(state.grid["0"]["is_armed"])
        self.assertTrue(state.grid["1"]["is_active"])
        self.assertFalse(state.grid["1"]["is_armed"])
        self.assertTrue(state.grid["2"]["is_active"])
        self.assertFalse(state.grid["2"]["is_armed"])

        # Недостигнутые уровни
        self.assertFalse(state.grid["3"]["is_active"])
        self.assertFalse(state.grid["4"]["is_active"])
        self.assertFalse(state.grid["5"]["is_active"])
        self.assertFalse(state.grid["6"]["is_active"])

        # Вооруженные курки очищены
        self.assertNotIn("1", state.trailing_grid_state["armed_levels"])
        self.assertNotIn("2", state.trailing_grid_state["armed_levels"])

    def test_02_avg_manager_blocks_duplicate_averaging_for_covered_level(self):
        """
        Проверка железного предохранителя AvgManager:
        Если в рантайме L1 остался ошибочно is_active=False, но текущий объем
        позиции ($85 USDT) уже покрывает порог L1 ($80 USDT - допуск 10% = $72 USDT),
        AvgManager.process НЕ ДОЛЖЕН отправлять рыночный ордер!
        Он обязан заблокировать дублирование, пометить L1 как is_active=True и выйти.
        """
        avg_mgr = AverageManager()
        state = PositionState(symbol="STRKUSDT", side="LONG")
        # 1000 монет по $0.085 = $85 USDT (L0 $48 + L1 $32 = $80)
        state.total_volume = 1000.0
        state.avg_entry_price = 0.085
        state.initial_entry_price = 0.090
        state.in_position = True
        state.eff_invest = 400.0
        state.grid = copy.deepcopy(self.grid_template)
        # Ошибочное состояние: L1 не был помечен активным
        state.grid["0"]["is_active"] = True
        state.grid["1"]["is_active"] = False
        state.grid["1"]["indent"] = -3.0

        mock_client = MagicMock()
        mock_client.make_order = AsyncMock()
        mock_client.cancel_orders_for_side = AsyncMock()
        mock_tp_mgr = MagicMock()
        mock_tp_mgr.place_take_profit = AsyncMock()

        spec_data = {
            "STRKUSDT": {
                "step_size": "0.1",
                "tick_size": "0.0001",
                "min_qty": "0.1",
                "min_notional": "5.0",
            }
        }

        # Вызываем process при текущей цене $0.080 (просадка 11%, достаточная для L1)
        asyncio.run(
            avg_mgr.process(
                client=mock_client,
                runtime_manager=None,
                symbol="STRKUSDT",
                side="LONG",
                state=state,
                opposite_state=None,
                current_price=0.080,
                spec_data=spec_data,
                tp_manager=mock_tp_mgr,
            )
        )

        # Ордер на усреднение НЕ ДОЛЖЕН быть отправлен на биржу
        mock_client.make_order.assert_not_called()
        # Уровень L1 должен быть автоматически помечен активным
        self.assertTrue(state.grid["1"]["is_active"])
        self.assertFalse(state.pending_avg)

    def test_03_initial_entry_price_preserved_on_stream_and_rest_update(self):
        """
        Проверка: При перезапуске бота или переподключении стрима update_from_stream
        НЕ ДОЛЖЕН сбрасывать ранее сохраненный initial_entry_price в 0.0.
        """
        states = {
            "STRKUSDT": {
                "LONG": PositionState(symbol="STRKUSDT", side="LONG"),
                "SHORT": PositionState(symbol="STRKUSDT", side="SHORT"),
            }
        }
        monitor = PositionMonitor(states_cache=states, target_symbols=["STRKUSDT"])
        state = states["STRKUSDT"]["LONG"]
        state.initial_entry_price = 0.0950  # Загружено из рантайм-кеша
        state.in_position = False           # До первого пакета стрима

        # Приходит пакет стрима с биржи
        monitor.update_from_stream(
            symbol="STRKUSDT",
            side="LONG",
            pos_amt=1413.0,
            entry_price=0.0850,
        )

        self.assertTrue(state.in_position)
        # Исходная точка входа должна быть сохранена!
        self.assertEqual(state.initial_entry_price, 0.0950)
        self.assertEqual(state.avg_entry_price, 0.0850)
        self.assertEqual(state.total_volume, 1413.0)

    def test_04_manual_trimming_does_not_corrupt_resolved_grid(self):
        """
        Проверка: Оператор вручную срезал часть позиции (например с L4 до L2).
        Уровни, которые уже были отработаны (was_active=True), сохраняют свой статус
        и не пытаются повторно усредниться.
        """
        state = PositionState(symbol="STRKUSDT", side="LONG")
        state.total_volume = 1200.0  # Срезано оператором
        state.avg_entry_price = 0.085
        state.in_position = True
        state.eff_invest = 400.0
        state.grid = copy.deepcopy(self.grid_template)
        # Были отработаны L0, L1, L2, L3
        for k in ("0", "1", "2", "3"):
            state.grid[k]["is_active"] = True

        StrategyApplicator.resolve_position_state_grid(state, strategy=self.mock_strategy)

        # Все ранее отработанные ступени должны остаться активными
        self.assertTrue(state.grid["0"]["is_active"])
        self.assertTrue(state.grid["1"]["is_active"])
        self.assertTrue(state.grid["2"]["is_active"])
        self.assertTrue(state.grid["3"]["is_active"])
        self.assertFalse(state.grid["4"]["is_active"])

    def test_05_build_runtime_backup_fallback_without_unbound_error(self):
        """
        Проверка: Функция build_runtime_for_symbol не падает с NameError: b_raw
        при обращении к бэкапу рантайма CACHE/backups/runtime_backup_latest.json.
        """
        applicator = StrategyApplicator()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            rt_file = applicator.build_runtime_for_symbol(
                symbol="STRKUSDT",
                strategy=self.mock_strategy,
                target_dir=tmp_path,
                force=True,
            )
            self.assertTrue(rt_file.exists())
            self.assertTrue(rt_file.name.endswith(".json"))


if __name__ == "__main__":
    unittest.main()
