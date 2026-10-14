# ============================================================
# FILE: tests/test_u16_chita_and_cross_recycling.py
# ROLE: Comprehensive unit tests for u16_chita configuration,
#       _avg_free_volume_behavior_modes documentation integrity,
#       CrossSideProfitRecycler (including negative SHORT volume
#       and min_burn_level filter), all 3 volume modes
#       (distribute_remaining, accumulate_current, mark_executed),
#       multiple skipped levels, ceiling boundary, and analytics
#       retention of historical coins.
# ============================================================

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

from CORE.HEDGE.cross_recycler import CrossSideProfitRecycler, CrossRecycler
from POS_FSM.models import PositionState
from CORE.GRID.avg_manager import AverageManager
from ANALYTICS.analytics import AnalyticsManager
from c_utils import Utils


class TestCrossRecyclerAndChitaFeatures(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self._orig_wait_sync = Utils.wait_for_fsm_sync
        Utils.wait_for_fsm_sync = AsyncMock(return_value=True)

    def tearDown(self):
        Utils.wait_for_fsm_sync = self._orig_wait_sync

    def test_u16_chita_config_structure_and_role_markers(self):
        """Проверка конфигурации Chita в live: 7 уровней, роли/маркеры, 97% объема, distribute_remaining, min_burn_level 2, и чистота документации режимов."""
        cfg_path = Path("CFG/universes/u16_chita.json")
        self.assertTrue(cfg_path.exists(), "u16_chita.json must exist")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        # 1. Основное поведение по умолчанию
        self.assertIn(cfg["avg_free_volume_behavior"], ("distribute_remaining", "mark_executed", "accumulate_current"))

        # 2. Документация режимов (_avg_free_volume_behavior_modes)
        modes = cfg.get("_avg_free_volume_behavior_modes", {})
        self.assertEqual(len(modes), 3, "Должно быть ровно 3 документированных режима")
        self.assertIn("distribute_remaining", modes)
        self.assertIn("accumulate_current", modes)
        self.assertIn("mark_executed", modes)
        self.assertNotIn("distribute_within_zone", modes, "distribute_within_zone должен быть удален из документации")

        # Проверка формулировок
        self.assertEqual(
            modes["distribute_remaining"],
            "(Распределение) Равномерно делим пропущенный объем между всеми оставшимися неисполненными ступенями сетки."
        )
        self.assertEqual(
            modes["accumulate_current"],
            "(Слияние с ближайшим) Объем пропущенных уровней суммируется и переносится в сработавшее колено, а пропущенные помечаются исполненными."
        )
        self.assertEqual(
            modes["mark_executed"],
            "(Сжигание объема) Пропущенные уровни помечаются исполненными при отработке ближайшего колена, их объем сгорает и никуда не переносится."
        )

        # 3. Cross-Side Profit Recycling (дискретная карта L2..L4: 0.50, L5+: 0.75 — единый источник истины)
        self.assertNotIn("recycle_ratio", cfg["cross_side_recycling"])
        self.assertNotIn("min_burn_level", cfg["cross_side_recycling"])
        self.assertIn("recycle_ratio_tiers", cfg["cross_side_recycling"])
        self.assertEqual(cfg["cross_side_recycling"]["recycle_ratio_tiers"]["2-4"], 0.50)
        self.assertEqual(cfg["cross_side_recycling"]["recycle_ratio_tiers"][">=5"], 0.75)
        self.assertEqual(CrossRecycler.resolve_min_burn_level(cfg["cross_side_recycling"]), 2)

        # 4. Сетка: 7 уровней и маркеры
        grid = cfg["LONG"]["grid"]
        self.assertGreaterEqual(len(grid), 7, "Grid must have at least 7 levels (0..6+)")

        for lvl_str in grid:
            self.assertIn("role", grid[lvl_str], f"Level {lvl_str} must have role marker")

        self.assertIn("L0: Скальп-ядро", grid["0"]["role"])
        self.assertIn("L1: Скальп", grid["1"]["role"])
        self.assertIn("L2: Скальп", grid["2"]["role"])
        self.assertIn("L3: Буфер", grid["3"]["role"])
        self.assertIn("L4: Буфер", grid["4"]["role"])
        self.assertIn("L5: Тяжелая Артиллерия", grid["5"]["role"])
        self.assertIn("L6: Бункер", grid["6"]["role"])

        total_vol = sum(float(grid[lvl]["volume"]) for lvl in grid)
        self.assertTrue(50.0 <= total_vol <= 100.0, f"Total volume must be within 50..100%, got {total_vol}%")

    async def test_cross_recycler_short_negative_volume_burn(self):
        """Проверка: рециркуляция корректно работает для тонущей SHORT позиции с отрицательным total_volume."""
        mock_universe = MagicMock()
        mock_universe.cross_recycling_cfg = {
            "enabled": True,
            "recycle_ratio": 0.50,
            "min_burn_level": 2,
            "min_drawdown_pct": 3.0,
            "max_slice_pct": 25.0
        }
        mock_universe.universe_id = "u16_chita"
        mock_universe.client = MagicMock()
        mock_universe.client.make_order = AsyncMock(return_value=MagicMock(success=True))
        mock_universe.client.fetch_positions = AsyncMock(return_value=[
            {"symbol": "STRKUSDT", "positionSide": "SHORT", "positionAmt": "-3000.0"}
        ])

        spec = {"tick_size": 0.0001, "step_size": 0.1, "min_qty": 0.1}
        short_state = PositionState("STRKUSDT", "SHORT")
        short_state.set_in_position(True)
        short_state.avg_entry_price = 0.0860
        short_state.total_volume = -4000.0
        short_state.grid = {
            "0": {"is_active": True},
            "1": {"is_active": True},
            "2": {"is_active": True},
            "3": {"is_active": False}
        }

        mock_universe.fsm_states = {"STRKUSDT": {"SHORT": short_state}}

        res = await CrossSideProfitRecycler.maybe_recycle(
            universe=mock_universe,
            symbol="STRKUSDT",
            winning_side="LONG",
            realized_profit=20.0,
            current_price=0.0900,
            spec_data=spec
        )

        self.assertIsNotNone(res, "Рециркуляция должна сработать для тонущего SHORT с отрицательным объемом!")
        self.assertEqual(res["symbol"], "STRKUSDT")
        self.assertEqual(res["losing_side"], "SHORT")
        self.assertGreater(res["burned_qty"], 0.0)
        mock_universe.client.make_order.assert_called_once()
        call_kwargs = mock_universe.client.make_order.call_args[1]
        self.assertEqual(call_kwargs["side"], "BUY")
        self.assertEqual(call_kwargs["position_side"], "SHORT")
        self.assertEqual(short_state.total_volume, -3000.0)

    async def test_cross_recycler_min_burn_level_filter(self):
        """Проверка: рециркуляция пропускается, если страдающая сторона набрала сетку ниже min_burn_level (например L1 < 2)."""
        mock_universe = MagicMock()
        mock_universe.cross_recycling_cfg = {
            "enabled": True,
            "recycle_ratio": 0.50,
            "min_burn_level": 2,
            "min_drawdown_pct": 3.0,
            "max_slice_pct": 25.0
        }
        mock_universe.universe_id = "u16_chita"

        spec = {"tick_size": 0.01, "step_size": 0.1, "min_qty": 0.1}
        short_state = PositionState("STRKUSDT", "SHORT")
        short_state.set_in_position(True)
        short_state.avg_entry_price = 0.0860
        short_state.total_volume = -1000.0
        short_state.grid = {
            "0": {"is_active": True},
            "1": {"is_active": True},
            "2": {"is_active": False}
        }

        mock_universe.fsm_states = {"STRKUSDT": {"SHORT": short_state}}

        res = await CrossSideProfitRecycler.maybe_recycle(
            universe=mock_universe,
            symbol="STRKUSDT",
            winning_side="LONG",
            realized_profit=20.0,
            current_price=0.0900,
            spec_data=spec
        )

        self.assertIsNone(res, "Рециркуляция обязана отсекаться, если уровень 1 < min_burn_level 2!")

    async def test_cross_recycler_multiaccount_and_full_burn_cleanup(self):
        """Проверка: рециркуляция использует get_client_for_symbol и отменяет ордера при полном сжигании объема."""
        mock_universe = MagicMock()
        mock_universe.cross_recycling_cfg = {
            "enabled": True,
            "recycle_ratio": 0.50,
            "min_burn_level": 2,
            "min_drawdown_pct": 3.0,
            "max_slice_pct": 100.0  # разрешаем 100% срезку для проверки полного сжигания
        }
        mock_universe.universe_id = "u16_chita"

        sym_client = MagicMock()
        sym_client.make_order = AsyncMock(return_value=MagicMock(success=True))
        sym_client.fetch_positions = AsyncMock(return_value=[
            {"symbol": "STRKUSDT", "positionSide": "SHORT", "positionAmt": "0.0"}
        ])
        sym_client.cancel_orders_for_side = AsyncMock(return_value=MagicMock(success=True))
        mock_universe.get_client_for_symbol = MagicMock(return_value=sym_client)

        spec = {"tick_size": 0.0001, "step_size": 1.0, "min_qty": 1.0}
        short_state = PositionState("STRKUSDT", "SHORT")
        short_state.set_in_position(True)
        short_state.avg_entry_price = 1.00
        short_state.total_volume = -10.0
        short_state.grid = {
            "0": {"is_active": True},
            "1": {"is_active": True},
            "2": {"is_active": True},
        }

        mock_universe.fsm_states = {"STRKUSDT": {"SHORT": short_state}}

        res = await CrossSideProfitRecycler.maybe_recycle(
            universe=mock_universe,
            symbol="STRKUSDT",
            winning_side="LONG",
            realized_profit=20.0,
            current_price=2.00,
            spec_data=spec
        )

        self.assertIsNotNone(res)
        mock_universe.get_client_for_symbol.assert_called_with("STRKUSDT")
        sym_client.make_order.assert_called_once()
        self.assertFalse(short_state.in_position)
        self.assertTrue(short_state.is_finished)
        self.assertEqual(short_state.total_volume, 0.0)
        # Проверяем, что при полном сжигании вызвана отмена ордеров
        sym_client.cancel_orders_for_side.assert_called_once_with("STRKUSDT", "SHORT")

    async def test_chita_cross_recycling_l3_skip_and_l4_execution(self):
        """Проверка: в Chita с min_burn_level=4 шорт на L3 пропускается, а на L4 кормится прибылью лонга."""
        mock_universe = MagicMock()
        mock_universe.cross_recycling_cfg = {
            "enabled": True,
            "recycle_ratio": 0.50,
            "min_burn_level": 4,
            "min_drawdown_pct": 3.0,
            "max_slice_pct": 25.0
        }
        mock_universe.universe_id = "u16_chita"

        client = MagicMock()
        client.make_order = AsyncMock(return_value=MagicMock(success=True))
        client.fetch_positions = AsyncMock(return_value=[
            {"symbol": "STRKUSDT", "positionSide": "SHORT", "positionAmt": "-750.0"}
        ])
        mock_universe.client = client
        mock_universe.tp_manager = MagicMock()
        mock_universe.tp_manager.place_take_profit = AsyncMock()

        spec = {"tick_size": 0.0001, "step_size": 1.0, "min_qty": 1.0}
        short_state = PositionState("STRKUSDT", "SHORT")
        short_state.set_in_position(True)
        short_state.avg_entry_price = 1.00
        short_state.total_volume = -1000.0
        # Сетка на уровне 3
        short_state.grid = {
            "0": {"is_active": True},
            "1": {"is_active": True},
            "2": {"is_active": True},
            "3": {"is_active": True},
            "4": {"is_active": False},
        }

        mock_universe.fsm_states = {"STRKUSDT": {"SHORT": short_state}}

        # 1. Попытка рециркуляции на L3 -> должна быть пропущена (min_burn_level=4)
        res_l3 = await CrossSideProfitRecycler.maybe_recycle(
            universe=mock_universe,
            symbol="STRKUSDT",
            winning_side="LONG",
            realized_profit=200.0,
            current_price=1.20,
            spec_data=spec
        )
        self.assertIsNone(res_l3, "На уровне L3 при min_burn_level=4 рециркуляция должна быть отсечена!")

        # 2. Активируем колено L4
        short_state.grid["4"]["is_active"] = True

        # Попытка рециркуляции на L4 -> должна сработать!
        # Прибыль лонга = $200.0, бюджет = 50% = $100.0
        # loss_per_unit = 1.20 - 1.00 = 0.20
        # raw_qty = 100 / 0.20 = 500
        # max_allowed = 1000 * 25% = 250
        # actual_qty = min(500, 250) = 250
        res_l4 = await CrossSideProfitRecycler.maybe_recycle(
            universe=mock_universe,
            symbol="STRKUSDT",
            winning_side="LONG",
            realized_profit=200.0,
            current_price=1.20,
            spec_data=spec
        )
        self.assertIsNotNone(res_l4, "На уровне L4 рециркуляция обязана исполниться!")
        self.assertEqual(res_l4["burned_qty"], 250.0)
        self.assertEqual(res_l4["burn_budget"], 100.0)
        self.assertAlmostEqual(res_l4["burn_loss"], 50.0, places=4)  # 250 * 0.20
        client.make_order.assert_called_once_with(
            symbol="STRKUSDT",
            qty=250.0,
            side="BUY",
            position_side="SHORT",
            market_type="MARKET"
        )
        mock_universe.tp_manager.place_take_profit.assert_called_once()

    def _create_mock_grid_and_client(self, behavior: str):
        avg_mgr = AverageManager()
        state = PositionState("STRKUSDT", "LONG")
        state.set_in_position(True)
        state.avg_free_volume_behavior = behavior
        state.initial_entry_price = 100.0
        state.avg_entry_price = 100.0

        state.grid = {
            "0": {"indent": 0.0, "volume": 5.0, "is_active": True, "zone": 1, "price": 100.0},
            "1": {"indent": -3.0, "volume": 5.0, "is_active": True, "zone": 1, "price": 97.0},
            "2": {"indent": -8.0, "volume": 6.0, "is_active": True, "zone": 1, "price": 92.0},
            "3": {"indent": -21.0, "activation": -26.0, "volume": 9.0, "is_active": False, "zone": 2, "price": 79.0, "activation_price": 74.0},
            "4": {"indent": -55.0, "activation": -65.0, "volume": 12.0, "is_active": False, "zone": 2, "price": 45.0, "activation_price": 35.0},
            "5": {"indent": -144.0, "activation": -155.0, "volume": 25.0, "is_active": False, "zone": 3, "price": 10.0, "activation_price": 5.0},
            "6": {"indent": -340.0, "activation": -360.0, "volume": 35.0, "is_active": False, "zone": 3, "price": 1.0, "activation_price": 0.5},
        }

        client = MagicMock()
        client.make_order = AsyncMock(return_value=MagicMock(success=True))
        client.fetch_positions = AsyncMock(return_value=[{"positionSide": "LONG", "entryPrice": "45.0", "positionAmt": "20.0"}])
        client.cancel_orders_for_side = AsyncMock()

        rt_mgr = MagicMock()
        rt_mgr.market_data_manager = None
        tp_mgr = MagicMock()
        tp_mgr.place_take_profit = AsyncMock()

        spec = {"tick_size": 0.01, "step_size": 0.1, "min_qty": 0.1}

        return avg_mgr, state, client, rt_mgr, tp_mgr, spec

    async def test_avg_free_volume_distribute_remaining_single_skipped(self):
        """Режим 1: distribute_remaining (1 пропущенный уровень L3): 9.0% делится поровну между L5 и L6 (+4.5% каждому). L4 сохраняет свой сайз."""
        avg_mgr, state, client, rt_mgr, tp_mgr, spec = self._create_mock_grid_and_client("distribute_remaining")

        # 1. Провал цены до 34.0 (DD 66%): взводятся L3 (-26%) и L4 (-65%)
        await avg_mgr.process(
            client=client, runtime_manager=rt_mgr, symbol="STRKUSDT", side="LONG",
            state=state, opposite_state=PositionState("STRKUSDT", "SHORT"),
            current_price=34.0, spec_data=spec, tp_manager=tp_mgr
        )
        # 2. Отскок до 45.0 (DD 55% <= L4 floor 55%): срабатывает L4, L3 пропущен (объем 9.0%)
        await avg_mgr.process(
            client=client, runtime_manager=rt_mgr, symbol="STRKUSDT", side="LONG",
            state=state, opposite_state=PositionState("STRKUSDT", "SHORT"),
            current_price=45.0, spec_data=spec, tp_manager=tp_mgr
        )

        # L3 помечен исполненным
        self.assertTrue(state.grid["3"]["is_active"])
        # L4 сохранил свой базовый объем 12.0%
        self.assertEqual(state.grid["4"]["volume"], 12.0)
        # Оставшиеся уровни после L4: L5 и L6. 9.0% / 2 = 4.5% каждому!
        self.assertEqual(state.grid["5"]["volume"], 29.5)
        self.assertEqual(state.grid["6"]["volume"], 39.5)

    async def test_avg_free_volume_distribute_remaining_multiple_skipped(self):
        """Режим 1: distribute_remaining (несколько пропущенных уровней L3 и L4): суммарный объем 21.0% переносится в единственный оставшийся уровень L6."""
        avg_mgr, state, client, rt_mgr, tp_mgr, spec = self._create_mock_grid_and_client("distribute_remaining")

        # Настраиваем реалистичные пороги для теста множественного пропуска
        state.grid["3"] = {"indent": -21.0, "activation": -26.0, "volume": 9.0, "is_active": False, "price": 79.0}
        state.grid["4"] = {"indent": -35.0, "activation": -40.0, "volume": 12.0, "is_active": False, "price": 65.0}
        state.grid["5"] = {"indent": -50.0, "activation": -55.0, "volume": 25.0, "is_active": False, "price": 50.0}
        state.grid["6"] = {"indent": -70.0, "activation": -75.0, "volume": 35.0, "is_active": False, "price": 30.0}

        # 1. Провал цены до 40.0 (DD 60%): взводятся L3 (-26%), L4 (-40%), L5 (-55%)
        await avg_mgr.process(
            client=client, runtime_manager=rt_mgr, symbol="STRKUSDT", side="LONG",
            state=state, opposite_state=PositionState("STRKUSDT", "SHORT"),
            current_price=40.0, spec_data=spec, tp_manager=tp_mgr
        )
        self.assertTrue(state.grid["3"].get("is_armed"))
        self.assertTrue(state.grid["4"].get("is_armed"))
        self.assertTrue(state.grid["5"].get("is_armed"))

        # 2. Отскок цены до 50.0 (DD 50% <= floor L5 50%): L5 исполняется! L3 (9.0%) и L4 (12.0%) пропущены (сумма 21.0%)
        await avg_mgr.process(
            client=client, runtime_manager=rt_mgr, symbol="STRKUSDT", side="LONG",
            state=state, opposite_state=PositionState("STRKUSDT", "SHORT"),
            current_price=50.0, spec_data=spec, tp_manager=tp_mgr
        )

        # L3 и L4 помечены исполненными
        self.assertTrue(state.grid["3"]["is_active"])
        self.assertTrue(state.grid["4"]["is_active"])
        # L5 сохранил свой базовый объем 25.0%
        self.assertEqual(state.grid["5"]["volume"], 25.0)
        # Оставшийся уровень после L5: только L6 (1 уровень). Получает все 21.0%: 35.0 + 21.0 = 56.0%!
        self.assertEqual(state.grid["6"]["volume"], 56.0)

    async def test_avg_free_volume_distribute_remaining_ceiling_boundary(self):
        """Режим 1: distribute_remaining (граница потолка сетки): когда срабатывает последний уровень L6 и L5 пропущен, оставшихся уровней нет, обработка проходит без ошибок."""
        avg_mgr, state, client, rt_mgr, tp_mgr, spec = self._create_mock_grid_and_client("distribute_remaining")

        # L0..L4 уже исполнены
        state.grid["3"]["is_active"] = True
        state.grid["4"]["is_active"] = True
        state.grid["5"] = {"indent": -50.0, "activation": -55.0, "volume": 25.0, "is_active": False, "price": 50.0}
        state.grid["6"] = {"indent": -70.0, "activation": -75.0, "volume": 35.0, "is_active": False, "price": 30.0}

        # 1. Провал цены до 20.0 (DD 80%): взводятся L5 (-55%) и L6 (-75%)
        await avg_mgr.process(
            client=client, runtime_manager=rt_mgr, symbol="STRKUSDT", side="LONG",
            state=state, opposite_state=PositionState("STRKUSDT", "SHORT"),
            current_price=20.0, spec_data=spec, tp_manager=tp_mgr
        )
        self.assertTrue(state.grid["5"].get("is_armed"))
        self.assertTrue(state.grid["6"].get("is_armed"))

        # 2. Отскок цены до 30.0 (DD 70% <= floor L6 70%): L6 исполняется, L5 пропущен
        await avg_mgr.process(
            client=client, runtime_manager=rt_mgr, symbol="STRKUSDT", side="LONG",
            state=state, opposite_state=PositionState("STRKUSDT", "SHORT"),
            current_price=30.0, spec_data=spec, tp_manager=tp_mgr
        )

        self.assertTrue(state.grid["5"]["is_active"])
        self.assertTrue(state.grid["6"]["is_active"])
        # На потолке объем L6 не изменился, ошибок и делений на 0 нет
        self.assertEqual(state.grid["6"]["volume"], 35.0)

    async def test_avg_free_volume_accumulate_current(self):
        """Режим 2: accumulate_current — объем пропущенного уровня (L3 9.0%) суммируется и переносится в текущее сработавшее колено (L4 12.0% -> 21.0%)."""
        avg_mgr, state, client, rt_mgr, tp_mgr, spec = self._create_mock_grid_and_client("accumulate_current")

        await avg_mgr.process(
            client=client, runtime_manager=rt_mgr, symbol="STRKUSDT", side="LONG",
            state=state, opposite_state=PositionState("STRKUSDT", "SHORT"),
            current_price=34.0, spec_data=spec, tp_manager=tp_mgr
        )
        await avg_mgr.process(
            client=client, runtime_manager=rt_mgr, symbol="STRKUSDT", side="LONG",
            state=state, opposite_state=PositionState("STRKUSDT", "SHORT"),
            current_price=45.0, spec_data=spec, tp_manager=tp_mgr
        )

        # L3 помечен исполненным
        self.assertTrue(state.grid["3"]["is_active"])
        # Объем L4 = 12.0 + 9.0 = 21.0% (слияние с ближайшим!)
        self.assertEqual(state.grid["4"]["volume"], 21.0)
        # L5 и L6 строго не изменились
        self.assertEqual(state.grid["5"]["volume"], 25.0)
        self.assertEqual(state.grid["6"]["volume"], 35.0)

    async def test_avg_free_volume_mark_executed(self):
        """Режим 3: mark_executed — сжигание объема: пропущенные уровни помечаются исполненными при отработке ближайшего колена, их объем сгорает."""
        avg_mgr, state, client, rt_mgr, tp_mgr, spec = self._create_mock_grid_and_client("mark_executed")

        await avg_mgr.process(
            client=client, runtime_manager=rt_mgr, symbol="STRKUSDT", side="LONG",
            state=state, opposite_state=PositionState("STRKUSDT", "SHORT"),
            current_price=34.0, spec_data=spec, tp_manager=tp_mgr
        )
        await avg_mgr.process(
            client=client, runtime_manager=rt_mgr, symbol="STRKUSDT", side="LONG",
            state=state, opposite_state=PositionState("STRKUSDT", "SHORT"),
            current_price=45.0, spec_data=spec, tp_manager=tp_mgr
        )

        # L3 помечен исполненным со специальными метаполями рантайма
        self.assertTrue(state.grid["3"]["is_active"])
        self.assertTrue(state.grid["3"].get("_skipped"))
        self.assertEqual(state.grid["3"].get("_skip_policy"), "mark_executed")
        self.assertEqual(state.grid["3"].get("_skip_status"), "burnt")
        self.assertEqual(state.grid["3"].get("_skipped_by_level"), "4")
        # L4 сохранил свой объем 12.0%
        self.assertEqual(state.grid["4"]["volume"], 12.0)
        # L5 и L6 не изменились (объем сгорел)
        self.assertEqual(state.grid["5"]["volume"], 25.0)
        self.assertEqual(state.grid["6"]["volume"], 35.0)

    def test_analytics_retention_when_coin_removed_from_strategy(self):
        """Проверка: монета, удаленная из активного списка стратегии, сохраняется в per_coin и не теряет статистику."""
        mgr = AnalyticsManager()
        strat_id = "u16_test_dome"

        strat_data = {
            "universe_id": strat_id,
            "total_trades": 50,
            "winning_trades": 45,
            "realized_pnl_usdt": 30.0,
            "unrealized_pnl_usdt": -5.0,
            "net_profit_usdt": 25.0,
            "peak_net_profit_usdt": 25.0,
            "max_drawdown_usdt": -5.0,
            "per_coin": {
                "COIN_ACTIVE": {
                    "total_trades": 30,
                    "winning_trades": 28,
                    "realized_pnl_net_usdt": 20.0,
                    "current_drawdown": -5.0,
                    "is_active_symbol": True
                },
                "COIN_REMOVED": {
                    "total_trades": 20,
                    "winning_trades": 17,
                    "realized_pnl_net_usdt": 10.0,
                    "current_drawdown": 0.0,
                    "long_amt": 0.0,
                    "short_amt": 0.0,
                    "is_active_symbol": True
                }
            }
        }

        global_data = {
            "per_coin": {
                "COIN_ACTIVE": {
                    "total_trades": 30,
                    "winning_trades": 28,
                    "realized_pnl_net_usdt": 20.0,
                    "current_drawdown": -5.0,
                    "long_amt": 100.0,
                    "short_amt": 0.0
                },
                "COIN_REMOVED": {
                    "total_trades": 20,
                    "winning_trades": 17,
                    "realized_pnl_net_usdt": 10.0,
                    "current_drawdown": 0.0,
                    "long_amt": 0.0,
                    "short_amt": 0.0
                }
            }
        }

        strat_cfg = MagicMock()
        strat_cfg.symbols = ["COIN_ACTIVE"]
        strat_cfg.name = "Test Dome"
        strat_cfg.start_balance_usdt = 150.0
        strat_cfg.analytics_cfg = {}

        mock_reg = MagicMock()
        mock_reg.active_strategies = {strat_id: strat_cfg}
        mgr.strategy_registry = mock_reg

        test_file = mgr.stats_dir / f"analytics_{strat_id}.json"
        test_file.write_text(json.dumps(strat_data), encoding="utf-8")

        try:
            mgr._update_strategies_stats(global_data)

            updated_data = json.loads(test_file.read_text(encoding="utf-8"))

            self.assertIn("COIN_REMOVED", updated_data["per_coin"])
            self.assertIn("COIN_ACTIVE", updated_data["per_coin"])

            self.assertTrue(updated_data["per_coin"]["COIN_ACTIVE"]["is_active_symbol"])
            self.assertFalse(updated_data["per_coin"]["COIN_REMOVED"]["is_active_symbol"])

            self.assertEqual(updated_data["total_trades"], 50)
            self.assertEqual(updated_data["winning_trades"], 45)
            self.assertEqual(updated_data["realized_pnl_usdt"], 30.0)
            self.assertEqual(updated_data["net_profit_usdt"], 25.0)
        finally:
            if test_file.exists():
                test_file.unlink()

    def test_disabled_grid_levels_handling(self):
        """Проверка: уровни с enabled: false корректно игнорируются трейлингом и не падают при None отступах."""
        from CORE.trailing_system import BaseTrailingSystem
        grid = {
            "0": {"enabled": True, "indent": 0.0, "volume": 5.0, "is_active": True},
            "1": {"enabled": True, "indent": -3.0, "volume": 5.0, "is_active": False},
            "6": {"enabled": False, "indent": None, "activation": None, "volume": 17.0, "is_active": False},
            "7": {"enabled": False, "indent": None, "activation": None, "volume": 18.0, "is_active": False},
        }
        tr_state = {}

        # 1. При просадке 0% отключенные уровни L6/L7 не взводятся и не исполняются
        cand, skipped, info = BaseTrailingSystem.evaluate_trailing_grid(0.0, grid, tr_state)
        self.assertIsNone(cand)
        self.assertEqual(skipped, [])

        # 2. Даже при катастрофической просадке 99% отключенные уровни L6/L7 не исполняются
        cand, skipped, info = BaseTrailingSystem.evaluate_trailing_grid(99.0, grid, tr_state)
        self.assertEqual(cand, "1")
        self.assertNotIn("6", skipped)
        self.assertNotIn("7", skipped)

    def test_defense_bookkeeping_and_leaderboard(self):
        """Проверка бухгалтерского учета: peels_ledger, recycling_ledger, JSON и витрины leaderboard."""
        from ANALYTICS.analytics import AnalyticsManager
        from ANALYTICS.leaderboard import LeaderboardEngine
        import tempfile
        import shutil

        temp_dir = Path(tempfile.mkdtemp())
        try:
            stats_d = temp_dir / "STATS"
            ledgers_d = temp_dir / "LEDGERS"
            stats_d.mkdir(parents=True)
            ledgers_d.mkdir(parents=True)

            mgr = AnalyticsManager()
            mgr.stats_dir = stats_d
            mgr.ledgers_dir = ledgers_d
            mgr.log_file = stats_d / "analytics.json"
            mgr.txt_file = ledgers_d / "trades_ledger.txt"
            mgr.peels_ledger = ledgers_d / "peels_ledger.txt"
            mgr.recycling_ledger = ledgers_d / "recycling_ledger.txt"
            mgr._ensure_files()
            # Чистый изолированный стейт для теста
            mgr._write_data({"start_balance_usdt": 150.0, "total_trades": 0, "net_profit_usdt": 0.0, "per_coin": {}})

            # 1. Записываем Sub-grid Peel
            mgr.record_subgrid_peel(
                strategy_id="u16_test",
                symbol="STRKUSDT",
                side="LONG",
                level="3",
                fill_price=0.08946,
                peel_price=0.09055,
                rebound_pct=1.22,
                peel_qty=268.3,
                rem_pos_qty=1674.9
            )

            self.assertTrue(mgr.peels_ledger.exists())
            peel_content = mgr.peels_ledger.read_text(encoding="utf-8")
            self.assertIn("STRKUSDT", peel_content)
            self.assertIn("L3", peel_content)

            # 2. Записываем Cross-Side Recycling
            mgr.record_cross_recycling(
                strategy_id="u16_test",
                symbol="STRKUSDT",
                winning_side="LONG",
                realized_profit=20.0,
                losing_side="SHORT",
                losing_level=4,
                losing_dd_pct=-15.0,
                burn_qty=100.0,
                current_price=1.10,
                actual_burn_loss=5.0,
                burn_budget=10.0,
                rem_pos_qty=900.0
            )

            self.assertTrue(mgr.recycling_ledger.exists())
            rec_content = mgr.recycling_ledger.read_text(encoding="utf-8")
            self.assertIn("STRKUSDT", rec_content)
            self.assertIn("L4", rec_content)

            # 3. Проверяем JSON аналитику
            data = mgr._read_data()
            self.assertIn("toxic_defense", data)
            self.assertEqual(data["toxic_defense"]["subgrid_peeling"]["events_count"], 1)
            self.assertEqual(data["toxic_defense"]["cross_recycling"]["events_count"], 1)

            # 4. Проверяем LeaderboardEngine
            engine = LeaderboardEngine(analytics_dir=temp_dir, stats_dir=stats_d)
            meta = {"name": "Test Strategy", "enabled": True}
            summary = engine.calculate_universe_summary("u16_test", meta=meta, analytics_data=data)
            self.assertEqual(summary["peel_events"], 1)
            self.assertEqual(summary["rec_events"], 1)
            self.assertEqual(summary["defense_badge"], "1P/1R")

            tbl = engine.format_table([summary], evaluated_coins_count=1)
            self.assertIn("Def (P/R)", tbl)
            self.assertIn("1P/1R", tbl)
            self.assertIn("[DEFENSE BOOKKEEPING]", tbl)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_discrete_recycle_ratio_tiers(self):
        """Институциональная проверка: дискретная карта рециркуляции прибыли по коленам сетки."""
        cfg = {
            "recycle_ratio": 0.50,
            "recycle_ratio_tiers": {
                "2-4": 0.33,
                ">=5": 0.66
            }
        }
        # L0 и L1: вне карты -> fallback 0.50
        self.assertEqual(CrossRecycler.resolve_recycle_ratio(cfg, 0), 0.50)
        self.assertEqual(CrossRecycler.resolve_recycle_ratio(cfg, 1), 0.50)
        # L2..L4: 0.33
        self.assertEqual(CrossRecycler.resolve_recycle_ratio(cfg, 2), 0.33)
        self.assertEqual(CrossRecycler.resolve_recycle_ratio(cfg, 3), 0.33)
        self.assertEqual(CrossRecycler.resolve_recycle_ratio(cfg, 4), 0.33)
        # L5+: 0.66
        self.assertEqual(CrossRecycler.resolve_recycle_ratio(cfg, 5), 0.66)
        self.assertEqual(CrossRecycler.resolve_recycle_ratio(cfg, 6), 0.66)
        self.assertEqual(CrossRecycler.resolve_recycle_ratio(cfg, 7), 0.66)

        # Проверка прямого map словаря {"2": 0.25, "3": 0.40}
        cfg_direct = {
            "recycle_ratio": 0.50,
            "recycle_ratio_map": {"2": 0.25, "3": 0.40}
        }
        self.assertEqual(CrossRecycler.resolve_recycle_ratio(cfg_direct, 2), 0.25)
        self.assertEqual(CrossRecycler.resolve_recycle_ratio(cfg_direct, 3), 0.40)
        self.assertEqual(CrossRecycler.resolve_recycle_ratio(cfg_direct, 4), 0.50)

        # Проверка ЕДИНОГО ИСТОЧНИКА ИСТИНЫ: только tiers, без полей recycle_ratio и min_burn_level
        cfg_tiers_only = {
            "recycle_ratio_tiers": {
                "2-4": 0.50,
                ">=5": 0.75
            }
        }
        self.assertEqual(CrossRecycler.resolve_min_burn_level(cfg_tiers_only), 2)
        self.assertEqual(CrossRecycler.resolve_recycle_ratio(cfg_tiers_only, 2), 0.50)
        self.assertEqual(CrossRecycler.resolve_recycle_ratio(cfg_tiers_only, 4), 0.50)
        self.assertEqual(CrossRecycler.resolve_recycle_ratio(cfg_tiers_only, 5), 0.75)
        self.assertEqual(CrossRecycler.resolve_recycle_ratio(cfg_tiers_only, 6), 0.75)
        self.assertEqual(CrossRecycler.resolve_recycle_ratio(cfg_tiers_only, 0), 0.0)  # L0 вне карты -> 0.0
        self.assertEqual(CrossRecycler.resolve_recycle_ratio(cfg_tiers_only, 1), 0.0)  # L1 вне карты -> 0.0

        # Проверка автоопределения min_burn_level при других картах
        self.assertEqual(CrossRecycler.resolve_min_burn_level({"recycle_ratio_tiers": {"3-4": 0.50, ">=5": 0.75}}), 3)
        self.assertEqual(CrossRecycler.resolve_min_burn_level({"recycle_ratio_tiers": {">=4": 0.75}}), 4)
        self.assertEqual(CrossRecycler.resolve_min_burn_level({"recycle_ratio_map": {"5": 0.60}}), 5)


if __name__ == "__main__":
    unittest.main()

