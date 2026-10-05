# ============================================================
# FILE: tests/test_side_inheritance.py
# ROLE: Unit and integration tests for side inheritance (LONG -> SHORT)
# ============================================================

import json
import tempfile
import unittest
from pathlib import Path

from c_utils import Utils
from CORE.STRATEGY.strategy_registry import StrategyConfig
from CORE.STRATEGY.strategy_applicator import StrategyApplicator


class TestSideInheritance(unittest.TestCase):
    """Тестирование механизма компактного наследования настроек сторон торговли."""

    def test_deep_merge(self):
        """Проверка рекурсивного слияния словарей."""
        base = {
            "a": 1,
            "b": {"x": 10, "y": 20},
            "c": [1, 2, 3]
        }
        update = {
            "b": {"y": 99, "z": 30},
            "d": "new"
        }
        res = Utils.deep_merge(base, update)
        self.assertEqual(res["a"], 1)
        self.assertEqual(res["b"]["x"], 10)
        self.assertEqual(res["b"]["y"], 99)
        self.assertEqual(res["b"]["z"], 30)
        self.assertEqual(res["c"], [1, 2, 3])
        self.assertEqual(res["d"], "new")

    def test_resolve_side_inheritance_override_dict_empty(self):
        """Наследование со словарем override: {} (полная копия LONG в SHORT)."""
        data = {
            "LONG": {
                "enable": True,
                "invest_size": 500.0,
                "grid": {"0": {"indent": 0.0, "volume": 10.0}},
                "tp_map": {"0": {"indent": 1.5}}
            },
            "SHORT": {
                "enable": False,
                "_comment_override": "Копирование настроек LONG стороны",
                "override": {}
            }
        }
        resolved = Utils.resolve_side_inheritance(data)
        short = resolved["SHORT"]
        self.assertFalse(short["enable"])
        self.assertEqual(short["invest_size"], 500.0)
        self.assertEqual(short["grid"], {"0": {"indent": 0.0, "volume": 10.0}})
        self.assertEqual(short["tp_map"], {"0": {"indent": 1.5}})
        self.assertNotIn("override", short)
        self.assertNotIn("_comment_override", short)

    def test_resolve_side_inheritance_override_dict_with_modifications(self):
        """Наследование со словарем override с точечными переопределениями."""
        data = {
            "LONG": {
                "enable": True,
                "invest_size": 500.0,
                "grid": {"0": {"indent": 0.0, "volume": 10.0}},
                "tp_map": {"0": {"indent": 1.5}}
            },
            "SHORT": {
                "enable": True,
                "override": {
                    "invest_size": 350.0,
                    "tp_map": {"0": {"indent": 2.0}}
                }
            }
        }
        resolved = Utils.resolve_side_inheritance(data)
        short = resolved["SHORT"]
        self.assertTrue(short["enable"])
        self.assertEqual(short["invest_size"], 350.0)
        self.assertEqual(short["grid"], {"0": {"indent": 0.0, "volume": 10.0}})
        self.assertEqual(short["tp_map"]["0"]["indent"], 2.0)
        self.assertNotIn("override", short)

    def test_resolve_side_inheritance_flag_underscore_override_false(self):
        """Наследование по флагу '_override': false (режим по умолчанию)."""
        data = {
            "u_test": {
                "LONG": {
                    "enable": True,
                    "invest_size": 600.0,
                    "leverage": 20,
                    "grid": {"0": {"indent": 0.0, "volume": 15.0}},
                    "tp_map": {"0": {"indent": 1.8}}
                },
                "SHORT": {
                    "enable": False,
                    "_override": False
                }
            }
        }
        resolved = Utils.resolve_side_inheritance(data)
        short = resolved["u_test"]["SHORT"]
        self.assertFalse(short["enable"])
        self.assertEqual(short["invest_size"], 600.0)
        self.assertEqual(short["leverage"], 20)
        self.assertEqual(short["grid"], {"0": {"indent": 0.0, "volume": 15.0}})
        self.assertEqual(short["tp_map"], {"0": {"indent": 1.8}})
        self.assertNotIn("_override", short)

    def test_resolve_side_inheritance_flag_underscore_override_false_with_inline_override(self):
        """Флаг '_override': false плюс прямое переопределение поля в теле SHORT."""
        data = {
            "LONG": {
                "enable": True,
                "invest_size": 600.0,
                "grid": {"0": {"indent": 0.0, "volume": 12.0}}
            },
            "SHORT": {
                "enable": False,
                "_override": False,
                "invest_size": 250.0
            }
        }
        resolved = Utils.resolve_side_inheritance(data)
        short = resolved["SHORT"]
        self.assertFalse(short["enable"])
        self.assertEqual(short["invest_size"], 250.0)
        self.assertEqual(short["grid"], {"0": {"indent": 0.0, "volume": 12.0}})
        self.assertNotIn("_override", short)

    def test_resolve_side_inheritance_flag_override_false(self):
        """Наследование по флагу 'override': false."""
        data = {
            "LONG": {
                "enable": True,
                "invest_size": 450.0,
                "grid": {"0": {"indent": 0.0}}
            },
            "SHORT": {
                "enable": True,
                "override": False
            }
        }
        resolved = Utils.resolve_side_inheritance(data)
        short = resolved["SHORT"]
        self.assertTrue(short["enable"])
        self.assertEqual(short["invest_size"], 450.0)
        self.assertNotIn("override", short)

    def test_resolve_side_inheritance_base_side_flag_true(self):
        """Проверка главного флага '_inheritance_base_side': true."""
        data = {
            "LONG": {
                "enabled": True,
                "invest_size": 600.0,
                "grid": {
                    "0": {"indent": 0.0, "volume": 7.0},
                    "1": {"indent": -3.5, "volume": 7.0},
                    "8": {"indent": -89.0, "volume": 16.49}
                },
                "tp_map": {
                    "0": {"indent": 2.2, "fallback_indent": 3.0},
                    "8": {"indent": 0.65, "fallback_indent": 0.95}
                }
            },
            "SHORT": {
                "enabled": True,
                "_inheritance_base_side": True
            }
        }
        resolved = Utils.resolve_side_inheritance(data)
        short = resolved["SHORT"]
        self.assertTrue(short["enabled"])
        self.assertTrue(short["enable"])
        self.assertEqual(short["invest_size"], 600.0)
        self.assertEqual(len(short["grid"]), 3)
        self.assertEqual(short["grid"]["8"]["indent"], -89.0)
        self.assertEqual(short["tp_map"]["0"]["indent"], 2.2)
        self.assertNotIn("_inheritance_base_side", short)

    def test_resolve_side_inheritance_base_side_flag_with_disabled_side(self):
        """Проверка '_inheritance_base_side': true при явном отключении стороны (enabled: false)."""
        data = {
            "LONG": {
                "enabled": True,
                "invest_size": 500.0,
                "grid": {"0": {"indent": 0.0, "volume": 10.0}}
            },
            "SHORT": {
                "enabled": False,
                "_inheritance_base_side": True
            }
        }
        resolved = Utils.resolve_side_inheritance(data)
        short = resolved["SHORT"]
        self.assertFalse(short["enabled"])
        self.assertFalse(short["enable"])
        self.assertEqual(short["invest_size"], 500.0)
        self.assertEqual(short["grid"], {"0": {"indent": 0.0, "volume": 10.0}})
        self.assertNotIn("_inheritance_base_side", short)

    def test_resolve_side_inheritance_base_side_flag_with_enable_key_false(self):
        """Проверка '_inheritance_base_side': true при явном ключе 'enable': false."""
        data = {
            "LONG": {
                "enable": True,
                "invest_size": 400.0,
                "grid": {"0": {"indent": 0.0, "volume": 5.0}}
            },
            "SHORT": {
                "enable": False,
                "_inheritance_base_side": True
            }
        }
        resolved = Utils.resolve_side_inheritance(data)
        short = resolved["SHORT"]
        self.assertFalse(short["enable"])
        self.assertFalse(short["enabled"])
        self.assertEqual(short["invest_size"], 400.0)
        self.assertNotIn("_inheritance_base_side", short)

    def test_resolve_side_inheritance_ignored_when_override_true(self):
        """Если _override: true или override: true, сторона автономна и не перезаписывается."""
        data = {
            "LONG": {
                "enable": True,
                "invest_size": 600.0,
                "grid": {"0": {"indent": 0.0}}
            },
            "SHORT": {
                "enable": True,
                "_override": True,
                "invest_size": 100.0
            }
        }
        resolved = Utils.resolve_side_inheritance(data)
        short = resolved["SHORT"]
        self.assertEqual(short["invest_size"], 100.0)
        self.assertNotIn("grid", short)

    def test_read_json_file_resolves_inheritance(self):
        """Интеграция: Utils.read_json_file автоматически разрешает side inheritance."""
        raw_json = {
            "u_dynasty": {
                "name": "Dynasty Test",
                "LONG": {
                    "enable": True,
                    "invest_size": 777.0,
                    "grid": {"0": {"indent": 0.0, "volume": 7.0}}
                },
                "SHORT": {
                    "enable": False,
                    "_override": False
                }
            }
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tf:
            json.dump(raw_json, tf)
            tf_path = Path(tf.name)

        try:
            loaded = Utils.read_json_file(tf_path)
            short = loaded["u_dynasty"]["SHORT"]
            self.assertFalse(short["enable"])
            self.assertEqual(short["invest_size"], 777.0)
            self.assertEqual(short["grid"]["0"]["volume"], 7.0)
            self.assertNotIn("_override", short)
        finally:
            if tf_path.exists():
                tf_path.unlink()

    def test_applicator_with_side_inheritance(self):
        """StrategyApplicator строит валидный runtime config со стороной, унаследованной через _override: false."""
        raw_meta = {
            "name": "Apex Test",
            "LONG": {
                "enable": True,
                "invest_size": 550.0,
                "grid": {"0": {"indent": 0.0, "volume": 8.0}},
                "tp_map": {"0": {"indent": 1.2}}
            },
            "SHORT": {
                "enable": False,
                "_override": False
            }
        }
        resolved_meta = Utils.resolve_side_inheritance(raw_meta)
        strat = StrategyConfig(
            strategy_id="u_apex_test",
            raw_meta=resolved_meta,
            symbols=["BTCUSDT"]
        )
        applicator = StrategyApplicator()
        sides = applicator.build_side_configs(strat)

        self.assertIn("LONG", sides)
        self.assertIn("SHORT", sides)
        self.assertTrue(sides["LONG"]["enable"])
        self.assertFalse(sides["SHORT"]["enable"])
        self.assertEqual(sides["SHORT"]["invest_size"], 550.0)
        self.assertEqual(sides["SHORT"]["grid"]["0"]["volume"], 8.0)


if __name__ == "__main__":
    unittest.main()
