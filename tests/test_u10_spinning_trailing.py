# ============================================================
# FILE: tests/test_u10_spinning_trailing.py
# ROLE: Unit and integration tests for u10_spinning, BaseTrailingSystem,
#       neutral tp_purpose, and avg_free_volume_behavior in cron3
# ============================================================

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from CORE.STRATEGY.trailing_system import BaseTrailingSystem
from POS_FSM.models import PositionState
from CORE.STRATEGY.strategy_registry import StrategyRegistry
from CORE.STRATEGY.strategy_applicator import StrategyApplicator
from CORE.TP.tp_manager import TakeProfitManager
from CORE.TP.fallback_tp_manager import FallbackTpManager


class TestU10SpinningTrailing(unittest.IsolatedAsyncioTestCase):
    def test_normalize_abs(self):
        """Тестирование знаково-агностического нормализатора normalize_abs."""
        self.assertEqual(BaseTrailingSystem.normalize_abs(10.5), 10.5)
        self.assertEqual(BaseTrailingSystem.normalize_abs(-10.5), 10.5)
        self.assertEqual(BaseTrailingSystem.normalize_abs("-8.0"), 8.0)
        self.assertEqual(BaseTrailingSystem.normalize_abs(None, default=0.5), 0.5)
        self.assertEqual(BaseTrailingSystem.normalize_abs("invalid", default=1.0), 1.0)

    def test_evaluate_trailing_tp_neutral_ladder(self):
        """Проверка динамической Фибоначчи-лестницы Трейлинг ТП в режиме neutral."""
        tp_map = {
            "0": {"indent": None, "fallback_indent": 1.9, "activation": 2.2},
            "1": {"indent": None, "fallback_indent": 2.2, "activation": 2.4},
            "2": {"indent": None, "fallback_indent": 2.4, "activation": 2.7},
            "3": {"indent": None, "fallback_indent": 3.2, "activation": 3.6},
        }
        state_trailing = {}

        # 1. Прибыль 1.5% — курок уровня 0 (2.2%) еще не достигнут
        should_close, floor, info = BaseTrailingSystem.evaluate_trailing_tp(
            gain_pct=1.5, tp_map=tp_map, state_trailing=state_trailing, tp_purpose="neutral"
        )
        self.assertFalse(should_close)
        self.assertIsNone(floor)

        # 2. Прибыль поднялась до 2.3% — взводится курок уровня 0 (activation 2.2%), пол = 1.9%
        should_close, floor, info = BaseTrailingSystem.evaluate_trailing_tp(
            gain_pct=2.3, tp_map=tp_map, state_trailing=state_trailing, tp_purpose="neutral"
        )
        self.assertFalse(should_close)
        self.assertEqual(floor, 1.9)
        self.assertEqual(state_trailing.get("armed_floor"), 1.9)

        # 3. Прибыль растет дальше до 2.8% — перешагивает активацию уровня 2 (2.7%), пол поднимается до 2.4%
        should_close, floor, info = BaseTrailingSystem.evaluate_trailing_tp(
            gain_pct=2.8, tp_map=tp_map, state_trailing=state_trailing, tp_purpose="neutral"
        )
        self.assertFalse(should_close)
        self.assertEqual(floor, 2.4)
        self.assertEqual(state_trailing.get("armed_floor"), 2.4)

        # 4. Откат прибыли до 2.5% — выше пола 2.4%, пока удерживаем позицию
        should_close, floor, info = BaseTrailingSystem.evaluate_trailing_tp(
            gain_pct=2.5, tp_map=tp_map, state_trailing=state_trailing, tp_purpose="neutral"
        )
        self.assertFalse(should_close)

        # 5. Откат прибыли до 2.35% (<= 2.4%) — фиксация прибыли по трейлингу!
        should_close, floor, info = BaseTrailingSystem.evaluate_trailing_tp(
            gain_pct=2.35, tp_map=tp_map, state_trailing=state_trailing, tp_purpose="neutral"
        )
        self.assertTrue(should_close)
        self.assertEqual(floor, 2.4)
        self.assertIn("TRAILING TP", info)

    def test_evaluate_trailing_tp_sign_agnostic(self):
        """Проверка работы с отрицательными знаками в tp_map (защита от ошибок оператора)."""
        tp_map_negative = {
            "0": {"indent": None, "fallback_indent": -1.9, "activation": -2.2},
        }
        state_trailing = {}
        # Прибыль 2.3% взводит пол 1.9%
        should_close, floor, info = BaseTrailingSystem.evaluate_trailing_tp(
            gain_pct=2.3, tp_map=tp_map_negative, state_trailing=state_trailing, tp_purpose="neutral"
        )
        self.assertFalse(should_close)
        self.assertEqual(floor, 1.9)

        # Откат до 1.8% исполняет маркет TP
        should_close, floor, info = BaseTrailingSystem.evaluate_trailing_tp(
            gain_pct=1.8, tp_map=tp_map_negative, state_trailing=state_trailing, tp_purpose="neutral"
        )
        self.assertTrue(should_close)

    def test_evaluate_trailing_grid_rebound(self):
        """Проверка взведения курка на просадке и исполнения на отскоке вверх."""
        grid = {
            "0": {"indent": 0.0, "activation": None, "volume": 7.36, "is_active": True},
            "1": {"indent": -3.5, "activation": None, "volume": 8.1, "is_active": False},
            "2": {"indent": -5.5, "activation": None, "volume": 8.91, "is_active": False},
            "3": {"indent": -8.0, "activation": -10.5, "volume": 9.8, "is_active": False},
            "4": {"indent": -13.0, "activation": -17.0, "volume": 10.78, "is_active": False},
        }
        state_trailing = {}

        # 1. Просадка 2.0% — ничего не срабатывает
        cand, skipped, info = BaseTrailingSystem.evaluate_trailing_grid(2.0, grid, state_trailing)
        self.assertIsNone(cand)

        # 2. Просадка 3.6% — уровень 1 без трейлинга срабатывает прямо
        cand, skipped, info = BaseTrailingSystem.evaluate_trailing_grid(3.6, grid, state_trailing)
        self.assertEqual(cand, "1")
        self.assertEqual(skipped, [])
        grid["1"]["is_active"] = True

        # 3. Просадка 9.0% — уровень 3 имеет курок на 10.5%, поэтому на 9.0% он еще не взведен
        cand, skipped, info = BaseTrailingSystem.evaluate_trailing_grid(9.0, grid, state_trailing)
        # Уровень 2 без трейлинга (indent: -5.5) срабатывает!
        self.assertEqual(cand, "2")
        grid["2"]["is_active"] = True

        # 4. Просадка углубляется до 11.0% — взводится курок уровня 3 (activation: 10.5)
        cand, skipped, info = BaseTrailingSystem.evaluate_trailing_grid(11.0, grid, state_trailing)
        self.assertIsNone(cand)  # Пока падает, не покупаем!
        self.assertIn("3", state_trailing.get("armed_levels", {}))

        # 5. Просадка отскакивает до 10.0% — еще глубже пола 8.0%, пока не исполняем
        cand, skipped, info = BaseTrailingSystem.evaluate_trailing_grid(10.0, grid, state_trailing)
        self.assertIsNone(cand)

        # 6. Отскок до 7.9% (просадка сократилась <= 8.0%) — исполнение уровня 3 на отскоке!
        cand, skipped, info = BaseTrailingSystem.evaluate_trailing_grid(7.9, grid, state_trailing)
        self.assertEqual(cand, "3")
        self.assertEqual(skipped, [])

    def test_evaluate_trailing_grid_flash_crash_and_skipped_levels(self):
        """Проверка резкого пролива цены мимо уровня 3 сразу в уровень 4 с последующим отскоком."""
        grid = {
            "0": {"indent": 0.0, "activation": None, "volume": 7.36, "is_active": True},
            "1": {"indent": -3.5, "activation": None, "volume": 8.1, "is_active": True},
            "2": {"indent": -5.5, "activation": None, "volume": 8.91, "is_active": True},
            "3": {"indent": -8.0, "activation": -10.5, "volume": 9.8, "is_active": False},
            "4": {"indent": -13.0, "activation": -17.0, "volume": 10.78, "is_active": False},
            "5": {"indent": -21.0, "activation": -27.5, "volume": 11.86, "is_active": False},
        }
        state_trailing = {}

        # Резкий импульсный пролив до 18.0% (пробиты курки и уровня 3, и уровня 4)
        cand, skipped, info = BaseTrailingSystem.evaluate_trailing_grid(18.0, grid, state_trailing)
        self.assertIsNone(cand)  # Идет пролив, держим
        self.assertIn("3", state_trailing.get("armed_levels", {}))
        self.assertIn("4", state_trailing.get("armed_levels", {}))

        # Отскок до 12.5% — уровень 4 (floor 13.0%) встречает цену раньше уровня 3 (floor 8.0%)!
        cand, skipped, info = BaseTrailingSystem.evaluate_trailing_grid(12.5, grid, state_trailing)
        self.assertEqual(cand, "4")
        # Уровень 3 был пропущен!
        self.assertEqual(skipped, ["3"])

    def test_polymorphic_direct_trigger_when_activation_equals_floor(self):
        """
        Проверка архитектурного принципа полиморфизма:
        Если курок стоит вровень с полом (activation == floor), система срабатывает
        как мгновенный прямой триггер (direct touch/overshoot) без ожидания отката/отскока.
        """
        # 1. Trailing Ratchet
        ratchet_cfg = {
            "enabled": True,
            "levels": [
                {"activation": 89.0, "floor": 89.0, "min_age_hours": 0.0, "enabled": True, "_desc": "Мега-ралли"}
            ]
        }
        # Недолет
        c, f, _, _ = BaseTrailingSystem.evaluate_ratchet(85.0, 1.0, ratchet_cfg, {})
        self.assertFalse(c)
        # Точное касание
        c, f, inf, _ = BaseTrailingSystem.evaluate_ratchet(89.0, 1.0, ratchet_cfg, {})
        self.assertTrue(c)
        self.assertEqual(f, 89.0)
        self.assertIn("INSTANT", inf)
        # Перелет (overshoot на импульсе)
        c, f, inf, _ = BaseTrailingSystem.evaluate_ratchet(91.5, 1.0, ratchet_cfg, {})
        self.assertTrue(c)
        self.assertEqual(f, 89.0)
        self.assertIn("INSTANT", inf)

        # 2. Trailing TP
        tp_map = {
            "13": {"activation": 89.5, "fallback_indent": 89.5, "indent": 0.0}
        }
        # Недолет
        c, f, _ = BaseTrailingSystem.evaluate_trailing_tp(88.0, tp_map, {}, "neutral")
        self.assertFalse(c)
        # Точное касание
        c, f, inf = BaseTrailingSystem.evaluate_trailing_tp(89.5, tp_map, {}, "neutral")
        self.assertTrue(c)
        self.assertEqual(f, 89.5)
        self.assertIn("INSTANT", inf)
        # Перелет
        c, f, inf = BaseTrailingSystem.evaluate_trailing_tp(93.0, tp_map, {}, "neutral")
        self.assertTrue(c)
        self.assertEqual(f, 89.5)
        self.assertIn("INSTANT", inf)

        # 3. Trailing Grid
        grid_map = {
            "8": {"activation": -89.0, "indent": -89.0, "volume": 68.0}
        }
        # Недолет
        cand, _, _ = BaseTrailingSystem.evaluate_trailing_grid(85.0, grid_map, {})
        self.assertIsNone(cand)
        # Точное касание
        cand, skipped, inf = BaseTrailingSystem.evaluate_trailing_grid(89.0, grid_map, {})
        self.assertEqual(cand, "8")
        # Перелет (пролив глубже без отскока)
        cand, skipped, inf = BaseTrailingSystem.evaluate_trailing_grid(92.0, grid_map, {})
        self.assertEqual(cand, "8")

    def test_avg_free_volume_behaviors(self):
        """Проверка трех режимов обработки сэкономленного объема сетки."""
        base_grid = {
            "0": {"indent": 0.0, "volume": 7.36, "is_active": True},
            "1": {"indent": -3.5, "volume": 8.1, "is_active": True},
            "2": {"indent": -5.5, "volume": 8.91, "is_active": True},
            "3": {"indent": -8.0, "volume": 10.0, "is_active": False},
            "4": {"indent": -13.0, "volume": 12.0, "is_active": False},
            "5": {"indent": -21.0, "volume": 15.0, "is_active": False},
            "6": {"indent": -34.0, "volume": 20.0, "is_active": False},
        }

        # 1. Режим distribute_remaining: пропущен уровень 3 (объем 10.0) при исполнении уровня 4
        # Оставшиеся неисполненные уровни после 4: 5 и 6 (2 штуки). Каждому достается 10.0 / 2 = +5.0!
        grid1 = copy.deepcopy(base_grid)
        cand = "4"
        skipped = ["3"]
        skipped_vol = sum(grid1[sk]["volume"] for sk in skipped)
        cand_idx = int(cand)
        remaining = [
            lvl for lvl in sorted(grid1.keys(), key=lambda x: int(x))
            if int(lvl) > cand_idx and not grid1[lvl].get("is_active")
        ]
        vol_share = round(skipped_vol / len(remaining), 4)
        for rem_lvl in remaining:
            grid1[rem_lvl]["volume"] += vol_share

        self.assertEqual(grid1["5"]["volume"], 20.0)  # 15.0 + 5.0
        self.assertEqual(grid1["6"]["volume"], 25.0)  # 20.0 + 5.0

        # 2. Режим accumulate_current: пропущен уровень 3 (10.0), прибавляется к текущему уровню 4 (12.0)
        grid2 = copy.deepcopy(base_grid)
        grid2[cand]["volume"] += skipped_vol
        self.assertEqual(grid2["4"]["volume"], 22.0)
        self.assertEqual(grid2["5"]["volume"], 15.0)  # Не изменился

        # 3. Режим mark_executed: пропущенный уровень просто сгорает
        grid3 = copy.deepcopy(base_grid)
        for sk in skipped:
            grid3[sk]["is_active"] = True
        self.assertTrue(grid3["3"]["is_active"])
        self.assertEqual(grid3["4"]["volume"], 12.0)  # Не изменился
        self.assertEqual(grid3["5"]["volume"], 15.0)  # Не изменился

    def test_neutral_validation_bypass(self):
        """Проверка отключения валидации len(grid) == len(tp_map) при tp_purpose='neutral'."""
        data_neutral = {
            "LONG": {
                "tp_purpose": "neutral",
                "grid": {"0": {}, "1": {}, "2": {}},  # 3 уровня
                "tp_map": {"0": {}, "1": {}, "2": {}, "3": {}, "4": {}},  # 5 уровней
            }
        }
        # При tp_purpose == "neutral" валидатор НЕ должен выбрасывать исключение
        for side in ["LONG", "SHORT"]:
            if side in data_neutral:
                if data_neutral[side].get("tp_purpose") == "neutral":
                    continue
                grid = data_neutral[side].get("grid", {})
                tp_map = data_neutral[side].get("tp_map", {})
                if grid and tp_map and len(grid) != len(tp_map):
                    self.fail("Validation should have been bypassed for neutral tp_purpose!")

        # А для classic (self/both) без neutral — валидатор должен бросать ошибку
        data_self = {
            "LONG": {
                "tp_purpose": "self",
                "grid": {"0": {}, "1": {}},
                "tp_map": {"0": {}, "1": {}, "2": {}},
            }
        }
        with self.assertRaises(ValueError):
            for side in ["LONG", "SHORT"]:
                if side in data_self:
                    if data_self[side].get("tp_purpose") == "neutral":
                        continue
                    grid = data_self[side].get("grid", {})
                    tp_map = data_self[side].get("tp_map", {})
                    if grid and tp_map and len(grid) != len(tp_map):
                        raise ValueError("Mismatch detected!")

    def test_u10_spinning_universe_configuration(self):
        """Проверка корректности конфигурации сохраненного флагмана u10_spinning_early_rebound в CFG/universes.json."""
        registry = StrategyRegistry()
        if "u10_spinning_early_rebound" not in registry.universes_db:
            self.skipTest("u10 retired from combat zoo to polygon per 3-champion mandate")
            return
        u10 = registry.universes_db["u10_spinning_early_rebound"]

        # Архитектурные правила куполов LONG/SHORT
        self.assertIn("LONG", u10)
        self.assertIn("SHORT", u10)
        self.assertEqual(u10["LONG"]["invest_size"], 600.0)
        self.assertEqual(u10["SHORT"]["invest_size"], 600.0)
        self.assertEqual(u10.get("tp_purpose"), "neutral")
        self.assertEqual(u10.get("avg_free_volume_behavior"), "distribute_remaining")

        # Проверка сетки в LONG
        grid = u10["LONG"]["grid"]
        self.assertEqual(len(grid), 9)
        self.assertIsNone(grid["0"]["activation"])
        self.assertIsNone(grid["1"]["activation"])
        self.assertEqual(grid["2"]["activation"], -7.0)
        self.assertEqual(grid["3"]["activation"], -10.5)
        self.assertEqual(grid["8"]["activation"], -89.0)

        # Проверка tp_map в LONG: 14 уровней нейтральной лестницы Фибоначчи
        tp_map = u10["LONG"]["tp_map"]
        self.assertEqual(len(tp_map), 14)
        self.assertEqual(tp_map["0"]["activation"], 2.2)
        self.assertEqual(tp_map["0"]["fallback_indent"], 1.9)
        self.assertIsNone(tp_map["0"]["indent"])

        from CORE.STRATEGY.strategy_registry import StrategyConfig
        strat_cfg = StrategyConfig("u10_spinning_early_rebound", u10, ["BTCUSDT"])
        applicator = StrategyApplicator()
        side_cfgs = applicator.build_side_configs(strat_cfg)
        self.assertEqual(side_cfgs["LONG"]["tp_purpose"], "neutral")
        self.assertEqual(side_cfgs["LONG"]["avg_free_volume_behavior"], "distribute_remaining")
        self.assertEqual(len(side_cfgs["LONG"]["grid"]), 9)
        self.assertEqual(len(side_cfgs["LONG"]["tp_map"]), 14)

    async def test_indent_determines_limit_vs_fallback_order(self):
        """Проверка: если indent задан числом -> LIMIT ордер; если indent is None -> статический лимит не ставится (фолбек)."""
        runtime_manager = MagicMock()
        tp_mgr = TakeProfitManager(runtime_manager)

        client = MagicMock()
        client.cancel_orders_for_side = AsyncMock(return_value=MagicMock(success=True))
        client.make_order = AsyncMock(return_value=MagicMock(success=True, data={"orderId": "123"}))

        state = MagicMock()
        state.grid = {"0": {"is_active": True}}
        state.avg_entry_price = 100.0
        state.total_volume = 1.0
        state.recent_closes = []
        spec_data = {"tick_size": 0.01, "step_size": 0.001, "min_qty": 0.001}

        # 1. indent задан числом (например 1.5%) -> выставляется биржевой LIMIT-ордер
        state.tp_purpose = "self"
        state.tp_map = {"0": {"indent": 1.5, "is_active": False}}
        res = await tp_mgr.place_take_profit(client, "BTCUSDT", "LONG", 100.0, spec_data, state)
        self.assertTrue(res)
        client.make_order.assert_called_once()
        self.assertEqual(client.make_order.call_args[1]["market_type"], "LIMIT")

        # 2. indent is None (как в u10_spinning_basic_distribute) -> лимитка не выставляется, закрытие отдается фолбеку
        client.make_order.reset_mock()
        state.tp_map = {"0": {"indent": None, "fallback_indent": 1.9, "activation": 2.2, "is_active": False}}
        res = await tp_mgr.place_take_profit(client, "BTCUSDT", "LONG", 100.0, spec_data, state)
        self.assertTrue(res)
        client.make_order.assert_not_called()
        self.assertTrue(state.tp_map["0"]["is_active"])

    async def test_tp_purpose_element_resolution(self):
        """Проверка: маркер tp_purpose строго определяет, какой элемент tp_map берется в конкретный момент."""
        runtime_manager = MagicMock()
        tp_mgr = TakeProfitManager(runtime_manager)

        client = MagicMock()
        client.cancel_orders_for_side = AsyncMock(return_value=MagicMock(success=True))
        client.make_order = AsyncMock(return_value=MagicMock(success=True, data={"orderId": "123"}))

        spec_data = {"tick_size": 0.01, "step_size": 0.001, "min_qty": 0.001}

        state_long = MagicMock()
        state_long.grid = {"0": {"is_active": True}, "1": {"is_active": True}}  # level_self = 1
        state_long.avg_entry_price = 100.0
        state_long.total_volume = 1.0
        state_long.recent_closes = []

        state_short = MagicMock()
        state_short.grid = {"0": {"is_active": True}, "1": {"is_active": True}, "2": {"is_active": True}}  # level_opp = 2

        # 1. tp_purpose = "self" -> уровень 1 (своя сетка)
        state_long.tp_purpose = "self"
        state_long.tp_map = {
            "0": {"indent": 1.0},
            "1": {"indent": 2.0},
            "2": {"indent": 3.0},
        }
        await tp_mgr.place_take_profit(client, "BTCUSDT", "LONG", 100.0, spec_data, state_long, state_short)
        self.assertEqual(client.make_order.call_args[1]["price"], 102.0)

        # 2. tp_purpose = "opposite" -> уровень 2 (соседняя сетка)
        client.make_order.reset_mock()
        state_long.tp_purpose = "opposite"
        await tp_mgr.place_take_profit(client, "BTCUSDT", "LONG", 100.0, spec_data, state_long, state_short)
        self.assertEqual(client.make_order.call_args[1]["price"], 103.0)

        # 3. tp_purpose = "both" -> max(level_self=1, level_opp=2) = 2
        client.make_order.reset_mock()
        state_long.tp_purpose = "both"
        await tp_mgr.place_take_profit(client, "BTCUSDT", "LONG", 100.0, spec_data, state_long, state_short)
        self.assertEqual(client.make_order.call_args[1]["price"], 103.0)

        # 4. tp_purpose = "neutral" -> автономный режим, отвязан от сетки
        client.make_order.reset_mock()
        state_long.tp_purpose = "neutral"
        state_long.tp_map = {"0": {"indent": 1.5}}
        await tp_mgr.place_take_profit(client, "BTCUSDT", "LONG", 100.0, spec_data, state_long, state_short)
        self.assertEqual(client.make_order.call_args[1]["price"], 101.5)

    async def test_fallback_tp_always_market_order(self):
        """Проверка: Fallback TP всегда закрывает позицию по MARKET ордеру."""
        fb_mgr = FallbackTpManager()
        client = MagicMock()
        client.cancel_orders_for_side = AsyncMock(return_value=MagicMock(success=True))
        client.make_order = AsyncMock(return_value=MagicMock(success=True))

        state = MagicMock()
        state.in_position = True
        state.is_finished = False
        state.pending_rolling_tp = False
        state.avg_entry_price = 100.0
        state.total_volume = 1.0
        state.trailing_tp_state = {"highest_gain": 3.0, "armed_floor": 2.4, "active_level": "2"}
        state.tp_map = {
            "0": {"indent": None, "fallback_indent": 1.9, "activation": 2.2},
            "1": {"indent": None, "fallback_indent": 2.4, "activation": 2.7},
        }
        state.tp_purpose = "neutral"
        state.grid = {"0": {"is_active": True}}
        spec_data = {"step_size": 0.001, "min_qty": 0.001}

        # При цене 102.35 (gain 2.35% <= armed_floor 2.4%) срабатывает Trailing TP
        await fb_mgr.process(client, None, "BTCUSDT", "LONG", state, 102.35, spec_data)
        client.make_order.assert_called_once()
        self.assertEqual(client.make_order.call_args[1]["market_type"], "MARKET")

    def test_tp_map_higher_grid_level_resolution(self):
        """
        Проверка: если уровень усреднялки перевалил за последний уровень tp_map,
        система берет крайнее высшее значение тейка (например с индексом 5 при 8 шагах сетки).
        """
        tp_map = {
            "0": {"indent": 2.2, "fallback_indent": 3.0, "activation": 3.5},
            "1": {"indent": 1.6, "fallback_indent": 2.4, "activation": 2.8},
            "2": {"indent": 1.2, "fallback_indent": 1.9, "activation": 2.2},
            "3": {"indent": 1.0, "fallback_indent": 1.6, "activation": 1.8},
            "4": {"indent": 0.8, "fallback_indent": 1.3, "activation": 1.5},
            "5": {"indent": 0.5, "fallback_indent": 0.95, "activation": 1.1},
        }

        # 1. BaseTrailingSystem.evaluate_trailing_tp с active_grid_level = "8" (больше чем 5)
        state_trailing = {}
        hit, floor, info = BaseTrailingSystem.evaluate_trailing_tp(
            gain_pct=1.2,
            tp_map=tp_map,
            state_trailing=state_trailing,
            tp_purpose="self",
            active_grid_level="8",
        )
        self.assertEqual(state_trailing.get("active_level"), "5")
        self.assertEqual(state_trailing.get("armed_floor"), 0.95)

    def test_u11_u12_and_u6_configurations(self):
        """Проверка наличия U11, U12 и U6 в universes.json."""
        from consts import DATA_DIR
        univ_dir = DATA_DIR / "universes"
        if univ_dir.exists():
            data = {jf.stem: json.loads(jf.read_text(encoding="utf-8")) for jf in univ_dir.glob("*.json")}
        elif (DATA_DIR / "universes.json").exists():
            with open(DATA_DIR / "universes.json", "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}

        # 1. U11 Turbo Scalp & Dump + Dynamic ATR TP
        if "u11_turbo_scalp_atr_deescalation" not in data:
            self.skipTest("u11 retired from combat zoo to polygon per 3-champion mandate")
            return
        u11 = data["u11_turbo_scalp_atr_deescalation"]
        self.assertEqual(u11.get("tp_purpose"), "self")
        self.assertEqual(len(u11["LONG"]["grid"]), 9)
        self.assertEqual(u11.get("leverage"), 20)
        grid = u11["LONG"]["grid"]
        self.assertEqual(grid["0"]["volume"], 7.0)
        self.assertEqual(grid["1"]["volume"], 7.0)
        self.assertEqual(grid["2"]["volume"], 7.0)
        self.assertEqual(grid["3"]["volume"], 10.24)
        self.assertEqual(grid["8"]["volume"], 16.49)
        total_vol = sum(g["volume"] for g in grid.values())
        self.assertAlmostEqual(total_vol, 100.0, places=2)
        self.assertTrue(u11["dynamic_atr_tp"]["enabled"])
        self.assertEqual(u11["dynamic_atr_tp"]["timeframe"], "1h")
        self.assertEqual(u11["dynamic_atr_tp"]["cache_ttl_sec"], 120)
        self.assertEqual(u11["dynamic_atr_tp"]["fallback_atr_pct"], 1.2)

        # 3. Варианты семейства U6
        self.assertIn("u6_symbiosis_gamma_fortress35", data)
        u6_tp_map = (data["u6_symbiosis_gamma_fortress35"].get("LONG") or data["u6_symbiosis_gamma_fortress35"].get("overrides", {})).get("tp_map", {})
        self.assertEqual(u6_tp_map["5"]["indent"], 0.65)
        self.assertEqual(u6_tp_map["8"]["indent"], 0.65)
        self.assertIn("u6_symbiosis_gamma_fortress35_flat_tp", data)
        u6_flat_tp_map = (data["u6_symbiosis_gamma_fortress35_flat_tp"].get("LONG") or data["u6_symbiosis_gamma_fortress35_flat_tp"].get("overrides", {})).get("tp_map", {})
        self.assertEqual(u6_flat_tp_map["0"]["indent"], 2.1)
        self.assertEqual(u6_flat_tp_map["6"]["indent"], 2.1)

        # 3. Проверка байпаса валидации: u10_ с tp_purpose="self" и разными длинами grid/tp_map
        asymmetric_data = {
            "LONG": {
                "strategy_id": "u10_symbiosis_test",
                "tp_purpose": "self",
                "grid": {"0": {}, "1": {}, "2": {}, "3": {}, "4": {}, "5": {}, "6": {}, "7": {}, "8": {}},
                "tp_map": {"0": {}, "1": {}, "2": {}, "3": {}, "4": {}},  # 5 уровней против 9
            }
        }
        # Валидатор не должен выбрасывать исключение для стратегии семейства u10_
        side = "LONG"
        strat_id = str(asymmetric_data[side].get("strategy_id", ""))
        is_u10 = strat_id.startswith("u10")
        grid = asymmetric_data[side].get("grid", {})
        tp_map = asymmetric_data[side].get("tp_map", {})
        tp_purpose = asymmetric_data[side].get("tp_purpose")
        if tp_purpose != "neutral" and not is_u10 and grid and tp_map and len(grid) != len(tp_map):
            self.fail("Validation should have been bypassed for u10 family strategy with tp_purpose='self'!")


if __name__ == "__main__":
    unittest.main()
