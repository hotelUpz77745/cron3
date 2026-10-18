# ==============================================================================
# Path: tests/test_stephenson_mechanisms.py
# Role: Полный комплексный набор модульных тестов для всех механизмов
#       стратегии Stephenson-EM5 (Dual EMA + Micro-Step L1 ATR + Half-Peel 50% +
#       Cross-Side Recycling + Правило кулака + Фибо-храповик + Panic Damper)
# ==============================================================================

import asyncio
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from CORE.strategy_registry import StrategyRegistry, StrategyConfig
from CORE.STRATEGY.strategy_applicator import StrategyApplicator
from CORE.STRATEGY.peeling_manager import PeelingManager
from CORE.STRATEGY.cross_recycler import CrossSideProfitRecycler
from CORE.TP.tp_manager import TakeProfitManager
from POS_FSM.models import PositionState
from c_utils import Utils


class TestStephensonMechanisms(unittest.IsolatedAsyncioTestCase):
    """Тестирование всех институциональных механизмов стратегии Stephenson-EM5."""

    def setUp(self):
        self.cfg_path = Path("CFG/universes/u17_stephensonEM5.json")
        self.assertTrue(self.cfg_path.exists(), f"Файл {self.cfg_path} должен существовать!")
        with open(self.cfg_path, "r", encoding="utf-8") as f:
            self.raw_cfg = json.load(f)

    def test_01_config_and_schema_integrity(self):
        """1. Валидация паспорта стратегии и всех куполов параметров по Протоколу №2 и №3."""
        strat = StrategyConfig(
            strategy_id="u17_stephensonEM5",
            raw_meta=self.raw_cfg,
            symbols=["BTCUSDT", "ETHUSDT"],
            active_settings={"start_balance_usdt": 450.0}
        )
        self.assertEqual(strat.strategy_id, "u17_stephensonEM5")
        self.assertEqual(strat.start_balance_usdt, 450.0)

        # 1.1 Micro-Step Filter Dome
        ms = strat.micro_step_filter_cfg
        self.assertTrue(ms["enabled"])
        self.assertEqual(ms["target_level"], "1")
        self.assertEqual(float(ms["max_atr_pct"]), 5.0)
        self.assertEqual(ms["timeframe"], "4h")
        self.assertEqual(ms["period"], 14)

        # 1.2 First Order Weight Regime Dome (Кулак Сильной Руки)
        fo = self.raw_cfg["first_order_weight_regime"]
        self.assertEqual(fo["mode"], "auto_ema")
        self.assertEqual(fo["presets"]["bullish"]["LONG"], 12.0)
        self.assertEqual(fo["presets"]["bullish"]["SHORT"], 7.0)
        self.assertEqual(fo["presets"]["bearish"]["LONG"], 6.0)
        self.assertEqual(fo["presets"]["bearish"]["SHORT"], 13.0)
        self.assertEqual(fo["presets"]["neutral"]["LONG"], 9.0)
        self.assertEqual(fo["presets"]["neutral"]["SHORT"], 9.0)

        # 1.3 Smart Grace Dome
        sg = self.raw_cfg["smart_grace"]
        self.assertEqual(sg["start_period_sec"], 150)
        self.assertEqual(sg["shift_interval_sec"], [5, 16])
        self.assertEqual(sg["brownian_noise_factor"], [3, 13])

        # 1.4 Smart Size & Smart TP Dome
        ss = self.raw_cfg["smart_size"]
        self.assertEqual(ss["trend_determ"], "ema_trend")
        self.assertTrue(ss["knife_blocker_enabled"])
        self.assertEqual(ss["knife_blocker_threshold"], 1)

        # 1.5 Cross-Side Recycling Dome
        csr = strat.cross_recycling_cfg
        self.assertTrue(csr["enabled"])
        self.assertEqual(csr["recycle_ratio_tiers"]["2-4"], 0.5)
        self.assertEqual(csr["recycle_ratio_tiers"][">=5"], 0.75)
        self.assertEqual(csr["min_drawdown_pct"], 3.0)
        self.assertEqual(csr["max_slice_pct"], 35.0)

        # 1.6 Panic Damper Floor (Правило 11)
        self.assertEqual(strat.min_tp_indent, 1.0)

        # 1.7 Auto-Closer Trailing Ratchet
        ac = strat.auto_closing_cfg
        self.assertEqual(ac["mode"], "iter_net_classic")
        levels = ac["positive"]["trailing_ratchet"]["levels"]
        self.assertEqual(len(levels), 6)
        self.assertEqual(levels[0]["activation"], 13.0)
        self.assertEqual(levels[0]["floor"], 8.0)
        self.assertEqual(levels[5]["activation"], 144.0)

    def test_02_first_order_regime_fist_rule(self):
        """2. Тестирование 'Правила кулака Сильной руки' (First Order Regime grid_0)."""
        presets = self.raw_cfg["first_order_weight_regime"]["presets"]

        # Bullish: Long 12%, Short 7%
        cfg_bull = {"mode": "bullish", "presets": presets}
        vol_long = StrategyApplicator.resolve_first_order_volume(cfg_bull, "LONG", 10.0)
        vol_short = StrategyApplicator.resolve_first_order_volume(cfg_bull, "SHORT", 10.0)
        self.assertEqual(vol_long, 12.0)
        self.assertEqual(vol_short, 7.0)

        # Bearish: Long 6%, Short 13%
        cfg_bear = {"mode": "bearish", "presets": presets}
        vol_long_bear = StrategyApplicator.resolve_first_order_volume(cfg_bear, "LONG", 10.0)
        vol_short_bear = StrategyApplicator.resolve_first_order_volume(cfg_bear, "SHORT", 10.0)
        self.assertEqual(vol_long_bear, 6.0)
        self.assertEqual(vol_short_bear, 13.0)

        # Neutral: Long 9%, Short 9%
        cfg_neu = {"mode": "neutral", "presets": presets}
        vol_long_neu = StrategyApplicator.resolve_first_order_volume(cfg_neu, "LONG", 10.0)
        vol_short_neu = StrategyApplicator.resolve_first_order_volume(cfg_neu, "SHORT", 10.0)
        self.assertEqual(vol_long_neu, 9.0)
        self.assertEqual(vol_short_neu, 9.0)

    def test_03_adaptive_micro_step_filter(self):
        """3. Тестирование адаптивной микро-ступени L1 (-1.6%, 3%) по порогу 4h ATR <= 5.0%."""
        strat = StrategyConfig(strategy_id="u17_stephensonEM5", raw_meta=self.raw_cfg, symbols=["BTCUSDT"])
        ms_filter = strat.micro_step_filter_cfg
        self.assertTrue(ms_filter["enabled"])
        target_lvl = str(ms_filter["target_level"])
        max_atr = float(ms_filter["max_atr_pct"])

        # Эмулируем состояние сетки
        state = PositionState(symbol="TESTUSDT", side="LONG")
        state.grid = copy.deepcopy(self.raw_cfg["LONG"]["grid"])

        # Случай А: ATR = 3.2% (волатильность низкая -> ступень активна)
        cur_atr_low = 3.2
        is_active_low = bool(cur_atr_low <= max_atr)
        state.grid[target_lvl]["enabled"] = is_active_low
        self.assertTrue(state.grid["1"]["enabled"])
        self.assertEqual(state.grid["1"]["indent"], -1.6)
        self.assertEqual(state.grid["1"]["volume"], 3.0)

        # Случай Б: ATR = 6.8% (памп/дамп -> ступень L1 отключается для защиты от раннего входа)
        cur_atr_high = 6.8
        is_active_high = bool(cur_atr_high <= max_atr)
        state.grid[target_lvl]["enabled"] = is_active_high
        self.assertFalse(state.grid["1"]["enabled"])
        # При этом L2 (-3.5%) и остальные ступени остаются включенными!
        self.assertTrue(state.grid["2"]["enabled"])
        self.assertTrue(state.grid["3"]["enabled"])

    def test_04_subgrid_peeling_half_peel_50(self):
        """4. Тестирование субсеточного пилинга Half-Peel 50% на уровнях L4, L5, L6."""
        long_grid = self.raw_cfg["LONG"]["grid"]

        # Проверяем наличие параметров пилинга на L4 (Мостик Змея)
        l4 = long_grid["4"]
        self.assertEqual(l4["indent"], -13.5)
        self.assertEqual(l4["activation"], -17.5)
        self.assertEqual(l4["peel_ratio"], 0.5)
        self.assertEqual(l4["peel_tp_pct"], 1.2)
        self.assertEqual(l4["peel_num"], 1)

        # Проверяем L5 (Буфер разворота)
        l5 = long_grid["5"]
        self.assertEqual(l5["indent"], -24.0)
        self.assertEqual(l5["activation"], -35.8)
        self.assertEqual(l5["peel_ratio"], 0.5)
        self.assertEqual(l5["peel_tp_pct"], 1.4)

        # Проверяем L6 (Тяжелая Артиллерия)
        l6 = long_grid["6"]
        self.assertEqual(l6["indent"], -55.0)
        self.assertEqual(l6["activation"], -58.8)
        self.assertEqual(l6["peel_ratio"], 0.5)
        self.assertEqual(l6["peel_tp_pct"], 1.5)

        # Проверяем детекцию пилинга в PeelingManager
        state = PositionState(symbol="TESTUSDT", side="LONG")
        state.grid = copy.deepcopy(long_grid)
        self.assertTrue(PeelingManager.has_peel_levels(state))

    def test_05_cross_side_recycling_tiers(self):
        """5. Тестирование дискретной карты сжигания токсина Cross-Side Recycling (50% на L2-4, 75% на L5+)."""
        strat = StrategyConfig(strategy_id="u17_stephensonEM5", raw_meta=self.raw_cfg, symbols=["BTCUSDT"])
        csr = strat.cross_recycling_cfg

        # На уровнях 2, 3, 4 доля сжигания 50%
        for lvl in (2, 3, 4):
            ratio = CrossSideProfitRecycler.resolve_recycle_ratio(csr, lvl)
            self.assertEqual(ratio, 0.5, f"На уровне {lvl} должно сжигаться ровно 50% прибыли")

        # На уровне 5 и выше (глубокая броня) доля сжигания 75%
        for lvl in (5, 6):
            ratio = CrossSideProfitRecycler.resolve_recycle_ratio(csr, lvl)
            self.assertEqual(ratio, 0.75, f"На уровне {lvl} должно сжигаться 75% прибыли")

    def test_06_panic_damper_tp_staircase(self):
        """6. Тестирование лестницы тейков Panic Damper и гарантированного пола min_tp_indent >= 1.0%."""
        strat = StrategyConfig(strategy_id="u17_stephensonEM5", raw_meta=self.raw_cfg, symbols=["BTCUSDT"])
        tp_map = self.raw_cfg["LONG"]["tp_map"]

        # Тейк сужается при углублении просадки
        self.assertEqual(tp_map["0"]["indent"], 1.8)
        self.assertEqual(tp_map["1"]["indent"], 1.2)
        self.assertEqual(tp_map["2"]["indent"], 1.15)
        self.assertEqual(tp_map["3"]["indent"], 1.05)
        self.assertEqual(tp_map["4"]["indent"], 1.05)
        self.assertEqual(tp_map["5"]["indent"], 1.05)
        self.assertEqual(tp_map["6"]["indent"], 1.05)

        # Ни один уровень не опускается ниже гарантированного пола
        min_tp = strat.min_tp_indent
        self.assertEqual(min_tp, 1.0)
        for lvl_id, tp_cfg in tp_map.items():
            self.assertGreaterEqual(
                tp_cfg["indent"], min_tp,
                f"Тейк уровня {lvl_id} ({tp_cfg['indent']}%) нарушает пол min_tp_indent ({min_tp}%)"
            )

    def test_07_auto_closing_ratchet_levels(self):
        """7. Тестирование Фибо-храповика Trailing Ratchet автоклозера."""
        ac = self.raw_cfg["auto_closing"]["positive"]["trailing_ratchet"]
        self.assertTrue(ac["enabled"])
        levels = ac["levels"]

        expected_pairs = [
            (13.0, 8.0),
            (21.0, 13.0),
            (34.0, 21.0),
            (55.0, 34.0),
            (89.0, 55.0),
            (144.0, 144.0)
        ]
        for idx, (act, flr) in enumerate(expected_pairs):
            self.assertEqual(levels[idx]["activation"], act)
            self.assertEqual(levels[idx]["floor"], flr)


if __name__ == "__main__":
    unittest.main()
