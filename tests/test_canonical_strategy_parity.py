# ==============================================================================
# Path: tests/test_canonical_strategy_parity.py
# Role: Comprehensive Unit Tests for Canonical Strategy Parity across
#       Combat Bot (cron3), Paper Universes, and Backtesting
# ==============================================================================

import unittest
from pathlib import Path

from CORE.STRATEGY.canonical_strategy_processor import CanonicalStrategyProcessor
from CORE.HEDGE.cross_recycler import CrossSideProfitRecycler


class TestCanonicalStrategyParity(unittest.TestCase):
    """
    Гарантирует 100% паритет и одинаковые сигнатуры между боевым ботом,
    бумажным полигоном и бэктестом. Исключает ситуацию 'тестируем одно, торгуем другое'.
    """

    def setUp(self):
        self.chita36_cfg = CanonicalStrategyProcessor.resolve_universe("u16_chita_v36")
        self.chita31_cfg = CanonicalStrategyProcessor.resolve_universe("u16_chita_v31")

    def test_chita36_canonical_keys_existence(self):
        """Проверяет наличие всех канонических нормализованных ключей в Чите 3.6."""
        for key in [
            "name", "invest_size", "leverage", "min_tp_indent",
            "grid_long", "grid_short", "tp_map_long", "tp_map_short",
            "cross_side_recycling", "first_order_weight_regime"
        ]:
            self.assertIn(key, self.chita36_cfg, f"Ключ '{key}' обязан быть в каноническом конфиге")

    def test_recycling_tier_resolution_parity_with_live_bot(self):
        """
        Проверяет, что канонический процессор и боевой CrossSideProfitRecycler
        возвращают идентичный burn_ratio на всех уровнях сетки.
        """
        rec_cfg = self.chita36_cfg["cross_side_recycling"]
        self.assertTrue(rec_cfg.get("enabled", True))

        for step in range(0, 8):
            live_ratio = CrossSideProfitRecycler.resolve_recycle_ratio(rec_cfg, step)
            canonical_ratio = CanonicalStrategyProcessor.resolve_recycle_ratio(rec_cfg, step)
            self.assertEqual(
                live_ratio,
                canonical_ratio,
                f"Несовпадение recycle_ratio на шаге {step}: live={live_ratio}, canonical={canonical_ratio}"
            )

        # Проверка канонических значений Читы 3.6:
        # L0, L1: 0.0 (до L2 не сжигаем)
        # L2, L3, L4: 0.50 (50% прибыли)
        # L5, L6, L7: 0.75 (75% прибыли)
        self.assertEqual(CanonicalStrategyProcessor.resolve_recycle_ratio(rec_cfg, 0), 0.0)
        self.assertEqual(CanonicalStrategyProcessor.resolve_recycle_ratio(rec_cfg, 1), 0.0)
        self.assertEqual(CanonicalStrategyProcessor.resolve_recycle_ratio(rec_cfg, 2), 0.50)
        self.assertEqual(CanonicalStrategyProcessor.resolve_recycle_ratio(rec_cfg, 3), 0.50)
        self.assertEqual(CanonicalStrategyProcessor.resolve_recycle_ratio(rec_cfg, 4), 0.50)
        self.assertEqual(CanonicalStrategyProcessor.resolve_recycle_ratio(rec_cfg, 5), 0.75)
        self.assertEqual(CanonicalStrategyProcessor.resolve_recycle_ratio(rec_cfg, 6), 0.75)

    def test_min_burn_level_parity(self):
        """Проверка паритета минимального уровня для начала рециркуляции."""
        rec_cfg = self.chita36_cfg["cross_side_recycling"]
        live_min = CrossSideProfitRecycler.resolve_min_burn_level(rec_cfg)
        canonical_min = CanonicalStrategyProcessor.resolve_min_burn_level(rec_cfg)
        self.assertEqual(live_min, canonical_min)
        self.assertEqual(canonical_min, 2, "В Чите 3.6 минимальный уровень сжигания обязан быть L2")

    def test_burn_slice_calculation_exact_match(self):
        """
        Проверяет математику расчета среза:
        Прибыль +$100, страдающая нога SHORT: avg=1.0, current_price=1.20 (DD -20%), vol=100.
        """
        rec_cfg = self.chita36_cfg["cross_side_recycling"]
        # На шаге 3 burn_ratio = 0.50 -> бюджет $50
        burn_qty, spent, burned_notional = CanonicalStrategyProcessor.calculate_burn_slice(
            net_profit=100.0,
            opp_side="SHORT",
            opp_avg_price=1.0,
            opp_vol_qty=100.0,
            opp_cur_step=3,
            current_price=1.20,
            recycling_cfg=rec_cfg,
            taker_fee=0.0005
        )

        loss_per_unit = (1.20 - 1.0) + (1.20 * 0.0005)  # 0.2006
        expected_qty = min(50.0 / loss_per_unit, 100.0 * 0.35)  # max_slice 35%
        self.assertAlmostEqual(burn_qty, expected_qty, places=4)
        self.assertAlmostEqual(spent, burn_qty * loss_per_unit, places=4)
        self.assertAlmostEqual(burned_notional, burn_qty * 1.20, places=4)

    def test_trailing_golden_cut_triggers_presence(self):
        """Проверяет наличие курков Golden-Cut (38.2%) в канонической Чите 3.6."""
        grid_long = self.chita36_cfg["grid_long"]
        grid_short = self.chita36_cfg["grid_short"]

        # Long L3: floor -13.5%, trigger -17.5%
        self.assertEqual(float(grid_long["3"]["indent"]), -13.5)
        self.assertEqual(float(grid_long["3"]["activation"]), -17.5)

        # Long L4: floor -24.0%, trigger -35.8%
        self.assertEqual(float(grid_long["4"]["indent"]), -24.0)
        self.assertEqual(float(grid_long["4"]["activation"]), -35.8)

        # Short L3: floor -21.0%, trigger -34.0%
        self.assertEqual(float(grid_short["3"]["indent"]), -21.0)
        self.assertEqual(float(grid_short["3"]["activation"]), -34.0)

    def test_half_peel_parameters_presence(self):
        """Проверяет параметры Half-Peel 50% на мостике Змея L3."""
        l3_long = self.chita36_cfg["grid_long"]["3"]
        self.assertEqual(float(l3_long["peel_ratio"]), 0.50)
        self.assertEqual(float(l3_long["peel_tp_pct"]), 1.20)
        self.assertEqual(int(l3_long["peel_num"]), 1)

    def test_base_and_common_template_alias_identity(self):
        """Проверяет, что шаблоны _common и _base разрешаются идентично в каноническом процессоре."""
        common_cfg = CanonicalStrategyProcessor.resolve_universe("_common")
        base_cfg = CanonicalStrategyProcessor.resolve_universe("_base")
        self.assertEqual(common_cfg["invest_size"], base_cfg["invest_size"])
        self.assertEqual(common_cfg["leverage"], base_cfg["leverage"])
        self.assertEqual(common_cfg["min_tp_indent"], base_cfg["min_tp_indent"])
        self.assertEqual(common_cfg["cross_side_recycling"], base_cfg["cross_side_recycling"])
        self.assertEqual(common_cfg["first_order_weight_regime"], base_cfg["first_order_weight_regime"])
        self.assertEqual(common_cfg["grid_long"], base_cfg["grid_long"])
        self.assertEqual(common_cfg["grid_short"], base_cfg["grid_short"])

    def test_paper_bridge_parity(self):
        """Проверяет паритет через модуль-мост бумажного полигона."""
        import importlib.util
        paper_bridge_path = Path(__file__).resolve().parent.parent.parent / "cron3PapperUniverses" / "CORE" / "STRATEGY" / "canonical_strategy_processor.py"
        spec = importlib.util.spec_from_file_location("paper_canonical_bridge", paper_bridge_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        PaperProcessor = mod.CanonicalStrategyProcessor

        paper_chita = PaperProcessor.resolve_universe("u16_chita_v36")
        self.assertEqual(paper_chita["invest_size"], self.chita36_cfg["invest_size"])
        self.assertEqual(paper_chita["grid_long"], self.chita36_cfg["grid_long"])
        self.assertEqual(paper_chita["cross_side_recycling"], self.chita36_cfg["cross_side_recycling"])


if __name__ == "__main__":
    unittest.main()

