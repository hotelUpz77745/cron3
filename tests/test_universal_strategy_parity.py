# ==============================================================================
# Path: tests/test_universal_strategy_parity.py
# Role: Проверка канонической совместимости tp_map, отмены валидации длин сеток
#       и полиморфного чтения фичей во всех трех системах экосистемы HRON_3
# ==============================================================================

import json
import unittest
from pathlib import Path

from CORE._utils import TakeProfitConfigResolver
from CORE.STRATEGY.canonical_strategy_processor import CanonicalStrategyProcessor


class TestUniversalStrategyParity(unittest.TestCase):
    """
    Тестирование канонической совместимости:
    1. Сигнатура tp_map (8 обязательных полей, неиспользуемые строго null).
    2. Полиморфный резолвер ближайшего уровня (при несовпадении длин сетки и карты тейков).
    3. Отсутствие валидации равенства длин len(grid) == len(tp_map).
    4. Безопасное полиморфное чтение фичей с мягким скипом без скрытых дефолтов.
    5. Защита системного пола TP (>= 0.50%) и соблюдение персонального min_tp_indent.
    6. Самодостаточность боевого бота: импорт API прямо из cron3/API.
    """

    @classmethod
    def setUpClass(cls):
        cls.cron3_dir = Path(__file__).resolve().parent.parent
        cls.universes_dir = cls.cron3_dir / "CFG" / "universes"
        cls.canonical_tp_keys = {
            "indent", "fallback_indent", "activation", "is_atr",
            "atr_mult", "min_floor", "max_ceil", "fallback_atr_pct"
        }
        cls.canonical_grid_keys = {
            "role", "enabled", "indent", "activation", "volume",
            "super_indent", "activated_at", "timestamp", "peel_tp_pct",
            "peel_ratio", "peel_num", "zone"
        }

    def test_tp_map_signature_across_all_universes(self):
        """Проверка строгого соответствия всех уровней tp_map 8 каноническим полям."""
        json_files = list(self.universes_dir.glob("*.json"))
        self.assertGreater(len(json_files), 0, "Папка вселенных не должна быть пустой")

        for jf in json_files:
            with open(jf, "r", encoding="utf-8") as f:
                data = json.load(f)

            for side in ("LONG", "SHORT"):
                s_cfg = data.get(side, {})
                tp_map = s_cfg.get("tp_map")
                if not tp_map:
                    continue

                for lvl_key, lvl_val in tp_map.items():
                    self.assertIsInstance(lvl_val, dict, f"[{jf.name}] {side} L{lvl_key} must be dict")
                    self.assertEqual(
                        set(lvl_val.keys()),
                        self.canonical_tp_keys,
                        f"[{jf.name}] {side} L{lvl_key} tp_map signature mismatch! Found: {set(lvl_val.keys())}"
                    )
                    # Если стратегия не использует ATR, поле is_atr должно быть null (None)
                    if lvl_val.get("atr_mult") is None:
                        self.assertIsNone(
                            lvl_val.get("is_atr"),
                            f"[{jf.name}] {side} L{lvl_key}: unused 'is_atr' must be null"
                        )

    def test_nearest_tp_level_resolution(self):
        """Проверка полиморфного выбора ближайшего уровня при несовпадении шага сетки и карты тейков."""
        sample_tp_map = {
            "0": {"indent": 1.2, "fallback_indent": 1.5, "activation": None, "is_atr": None, "atr_mult": None, "min_floor": None, "max_ceil": None, "fallback_atr_pct": None},
            "1": {"indent": 1.6, "fallback_indent": 2.0, "activation": None, "is_atr": None, "atr_mult": None, "min_floor": None, "max_ceil": None, "fallback_atr_pct": None},
            "2": {"indent": 1.35, "fallback_indent": 1.7, "activation": None, "is_atr": None, "atr_mult": None, "min_floor": None, "max_ceil": None, "fallback_atr_pct": None},
            "5": {"indent": 1.05, "fallback_indent": 1.2, "activation": None, "is_atr": None, "atr_mult": None, "min_floor": None, "max_ceil": None, "fallback_atr_pct": None},
        }

        # 1. Точное совпадение
        self.assertEqual(TakeProfitConfigResolver.get_nearest_level_key(1, sample_tp_map), "1")
        self.assertEqual(TakeProfitConfigResolver.get_nearest_level_key("2", sample_tp_map), "2")

        # 2. Шаг перевалил за максимум (шаг 6 или 8 -> должен взять ближайший "5")
        self.assertEqual(TakeProfitConfigResolver.get_nearest_level_key(6, sample_tp_map), "5")
        self.assertEqual(TakeProfitConfigResolver.get_nearest_level_key(9, sample_tp_map), "5")

        # 3. Шаг меньше минимума (-1 -> "0")
        self.assertEqual(TakeProfitConfigResolver.get_nearest_level_key(-1, sample_tp_map), "0")

        # 4. Шаг в пропуске (шаг 4 ближе к 5 чем к 2 -> "5", шаг 3 ближе к 2 -> "2")
        self.assertEqual(TakeProfitConfigResolver.get_nearest_level_key(4, sample_tp_map), "5")
        self.assertEqual(TakeProfitConfigResolver.get_nearest_level_key(3, sample_tp_map), "2")

    def test_tp_floor_system_vs_strategy(self):
        """Проверка системного пола 0.50% и персонального min_tp_indent."""
        sample_tp_map = {
            "0": {"indent": 0.20, "fallback_indent": 0.30, "activation": None, "is_atr": None, "atr_mult": None, "min_floor": None, "max_ceil": None, "fallback_atr_pct": None},
        }

        # Без персонального min_tp_indent: пол строго 0.50%
        res_sys = TakeProfitConfigResolver.get_resolved_level(sample_tp_map, 0, min_tp_indent=None)
        self.assertEqual(res_sys["indent"], 0.50)
        self.assertEqual(res_sys["fallback_indent"], 0.50)

        # С персональным min_tp_indent = 1.05: пол поднимается до 1.05%
        res_strat = TakeProfitConfigResolver.get_resolved_level(sample_tp_map, 0, min_tp_indent=1.05)
        self.assertEqual(res_strat["indent"], 1.05)
        self.assertEqual(res_strat["fallback_indent"], 1.05)

    def test_soft_skip_when_feature_absent(self):
        """Проверка полиморфного чтения фичей: отсутствие поля приводит к безопасному None без дефолтов."""
        minimal_strat = {
            "name": "u_test_minimal",
            "invest_size": 400.0,
            "leverage": 20,
            "min_tp_indent": 1.0,
            "avg_free_volume_behavior": "mark_executed",
            "tp_purpose": "self",
            "LONG": {
                "grid": {"0": {"enabled": True, "volume": 10.0, "indent": 0.0}},
                "tp_map": {"0": {"indent": 1.2, "fallback_indent": 1.5}}
            }
        }
        resolved = CanonicalStrategyProcessor.resolve_universe(minimal_strat)
        # Отсутствующие фичи безопасны и строго равны None (Правило 2.2 Протокола)
        self.assertIsNone(resolved["first_order_weight_regime"])
        self.assertIsNone(resolved["cross_side_recycling"])
        self.assertIsNone(resolved["smart_size"])
        self.assertIsNone(resolved["smart_tp"])
        self.assertIsNone(resolved["smart_grace"])

    def test_internal_api_self_containment(self):
        """Проверка самодостаточности боевого бота: API импортируется напрямую из cron3/API."""
        from API.BINANCE.public import BinancePublic
        from API.BINANCE.price_stream import BinanceHotPriceStream
        from API.IPC.streamer_hub import CrossProcessHotPriceStream
        self.assertIsNotNone(BinancePublic)
        self.assertIsNotNone(BinanceHotPriceStream)
        self.assertIsNotNone(CrossProcessHotPriceStream)


if __name__ == "__main__":
    unittest.main()
