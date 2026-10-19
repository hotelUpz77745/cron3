# tests/test_symbol_lifecycle_manager.py
# -*- coding: utf-8 -*-
"""
==============================================================================
Path: tests/test_symbol_lifecycle_manager.py
Role: Институциональный юнит-тест жизненного цикла торговых символов (SymbolLifecycleManager)
==============================================================================
"""

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from CORE.SERVICES.symbol_lifecycle_manager import SymbolLifecycleManager
from POS_FSM.models import PositionState


class TestSymbolLifecycleManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_sym_lifecycle_"))
        self.app_json_path = self.temp_dir / "app.json"
        self.runtime_dir = self.temp_dir / "runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

        initial_app_data = {
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "active_strategies": {
                "u16_chita__classic": {
                    "enabled": True,
                    "default_invest_size": 400.0,
                    "symbols": ["BTCUSDT", "ETHUSDT"],
                    "super_grid": {"enabled": True},
                }
            },
        }
        self.app_json_path.write_text(json.dumps(initial_app_data, indent=2), encoding="utf-8")

        self.mock_bot = MagicMock()
        self.mock_bot.symbols = ["BTCUSDT", "ETHUSDT"]
        self.mock_bot.fsm_states = {
            "BTCUSDT": {"LONG": PositionState("BTCUSDT", "LONG"), "SHORT": PositionState("BTCUSDT", "SHORT")},
            "ETHUSDT": {"LONG": PositionState("ETHUSDT", "LONG"), "SHORT": PositionState("ETHUSDT", "SHORT")},
        }
        self.mock_bot.runtime_configs = {"BTCUSDT": {"LONG": {"invest_size": 400.0}}, "ETHUSDT": {"LONG": {"invest_size": 400.0}}}
        self.mock_bot.prices = {"BTCUSDT": (60000.0, 1000.0), "ETHUSDT": (3000.0, 1000.0)}

        self.mock_bot.pos_stream = MagicMock()
        self.mock_bot.pos_stream.target_symbols = {"BTCUSDT", "ETHUSDT"}

        self.mock_bot.ws_interpreter = MagicMock()
        self.mock_bot.ws_interpreter.target_symbols = {"BTCUSDT", "ETHUSDT"}

        self.mock_bot.pos_monitor = MagicMock()
        self.mock_bot.pos_monitor.target_symbols = ["BTCUSDT", "ETHUSDT"]

        self.mock_bot.runtime_manager = MagicMock()
        self.mock_bot.runtime_manager.runtime_dir = self.runtime_dir
        self.mock_bot.runtime_manager.get_runtime_path = lambda sym: self.runtime_dir / f"{sym.lower()}.json"
        self.mock_bot.runtime_manager.caches = {}

        self.mock_bot.leverage_manager = MagicMock()
        self.mock_bot.leverage_manager.set_leverage_and_margin = AsyncMock()

        self.mock_bot.volatility_manager = MagicMock()
        self.mock_bot.volatility_manager.is_running = True
        self.mock_bot.volatility_manager.process_all = AsyncMock()

        self.mock_bot._restart_price_stream = AsyncMock()
        self.mock_bot._init_clients = MagicMock()

        self.manager = SymbolLifecycleManager(self.mock_bot)

    def tearDown(self):
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_add_symbol_already_active(self):
        """Попытка добавления уже активного символа возвращает False."""
        res = asyncio.run(self.manager.add_symbol("BTCUSDT"))
        self.assertFalse(res)
        self.assertEqual(len(self.mock_bot.symbols), 2)

    def test_add_symbol_success_with_strategy_id(self):
        """Успешное добавление нового символа с явным указанием стратегии."""
        with patch("CORE.SERVICES.symbol_lifecycle_manager.DATA_DIR", self.temp_dir), \
             patch("CORE.SERVICES.symbol_lifecycle_manager.BinancePublic.get_last_price", new_callable=AsyncMock, return_value=1.55), \
             patch("CORE.SERVICES.symbol_lifecycle_manager.StrategyApplicator") as mock_app_cls:

            mock_applicator = MagicMock()
            mock_app_cls.return_value = mock_applicator

            res = asyncio.run(self.manager.add_symbol("STRKUSDT", strategy_id="u16_chita__classic"))
            self.assertTrue(res)

            # Проверка обновления списков в памяти
            self.assertIn("STRKUSDT", self.mock_bot.symbols)
            self.assertIn("STRKUSDT", self.mock_bot.fsm_states)
            self.assertEqual(self.mock_bot.fsm_states["STRKUSDT"]["LONG"].symbol, "STRKUSDT")
            self.assertEqual(self.mock_bot.fsm_states["STRKUSDT"]["SHORT"].symbol, "STRKUSDT")

            # Проверка обновления мониторов и стримов
            self.assertIn("STRKUSDT", self.mock_bot.pos_stream.target_symbols)
            self.assertIn("STRKUSDT", self.mock_bot.ws_interpreter.target_symbols)
            self.assertIn("STRKUSDT", self.mock_bot.pos_monitor.target_symbols)

            # Проверка цены и перезапуска стрима
            self.assertIn("STRKUSDT", self.mock_bot.prices)
            self.assertEqual(self.mock_bot.prices["STRKUSDT"][0], 1.55)
            self.mock_bot._restart_price_stream.assert_called_once()

            # Проверка app.json
            saved_app = json.loads(self.app_json_path.read_text(encoding="utf-8"))
            self.assertIn("STRKUSDT", saved_app["active_strategies"]["u16_chita__classic"]["symbols"])

    def test_add_symbol_without_strategy_id_defaults_to_first_active(self):
        """Если strategy_id не указан, символ добавляется в первую активную стратегию."""
        with patch("CORE.SERVICES.symbol_lifecycle_manager.DATA_DIR", self.temp_dir), \
             patch("CORE.SERVICES.symbol_lifecycle_manager.BinancePublic.get_last_price", new_callable=AsyncMock, return_value=25.0), \
             patch("CORE.SERVICES.symbol_lifecycle_manager.StrategyApplicator"):

            res = asyncio.run(self.manager.add_symbol("SOLUSDT"))
            self.assertTrue(res)
            self.assertIn("SOLUSDT", self.mock_bot.symbols)

            saved_app = json.loads(self.app_json_path.read_text(encoding="utf-8"))
            self.assertIn("SOLUSDT", saved_app["active_strategies"]["u16_chita__classic"]["symbols"])

    def test_delete_symbol_not_present(self):
        """Удаление отсутствующего символа возвращает False."""
        res = asyncio.run(self.manager.delete_symbol("UNKNOWNUSDT"))
        self.assertFalse(res)

    def test_delete_symbol_success(self):
        """Успешное удаление активного символа с очисткой FSM, кэша и перезапуском стримов."""
        dummy_runtime = self.runtime_dir / "btcusdt.json"
        dummy_runtime.write_text("{}", encoding="utf-8")

        with patch("CORE.SERVICES.symbol_lifecycle_manager.DATA_DIR", self.temp_dir):
            res = asyncio.run(self.manager.delete_symbol("BTCUSDT"))
            self.assertTrue(res)

            # Проверка удаления из структур в памяти
            self.assertNotIn("BTCUSDT", self.mock_bot.symbols)
            self.assertNotIn("BTCUSDT", self.mock_bot.fsm_states)
            self.assertNotIn("BTCUSDT", self.mock_bot.runtime_configs)
            self.assertNotIn("BTCUSDT", self.mock_bot.prices)

            # Проверка отписки от стримов
            self.assertNotIn("BTCUSDT", self.mock_bot.pos_stream.target_symbols)
            self.assertNotIn("BTCUSDT", self.mock_bot.ws_interpreter.target_symbols)
            self.assertNotIn("BTCUSDT", self.mock_bot.pos_monitor.target_symbols)

            # Проверка удаления файла кэша рантайма
            self.assertFalse(dummy_runtime.exists())

            # Проверка перезапуска стрима цен
            self.mock_bot._restart_price_stream.assert_called_once()

            # Проверка app.json
            saved_app = json.loads(self.app_json_path.read_text(encoding="utf-8"))
            self.assertNotIn("BTCUSDT", saved_app["active_strategies"]["u16_chita__classic"]["symbols"])

    def test_add_and_delete_symbol_with_per_symbol_invest_size(self):
        """Проверка добавления и удаления монеты в стратегии с per-symbol invest_size и default_invest_size."""
        app_data = {
            "active_strategies": {
                "u17_stephensonE__classic": {
                    "enabled": True,
                    "default_invest_size": 400.0,
                    "LONG": {
                        "default_invest_size": 400.0,
                        "invest_size": {
                            "ICNTUSDT": 400.0,
                            "AVAUSDT": 400.0
                        }
                    },
                    "SHORT": {
                        "default_invest_size": 400.0,
                        "invest_size": {
                            "ICNTUSDT": 400.0,
                            "AVAUSDT": 400.0
                        }
                    }
                }
            }
        }
        self.app_json_path.write_text(json.dumps(app_data, indent=2), encoding="utf-8")

        with patch("CORE.SERVICES.symbol_lifecycle_manager.DATA_DIR", self.temp_dir), \
             patch("CORE.SERVICES.symbol_lifecycle_manager.BinancePublic.get_last_price", new_callable=AsyncMock, return_value=10.0), \
             patch("CORE.SERVICES.symbol_lifecycle_manager.StrategyApplicator"):

            # 1. Добавляем новую монету GTCUSDT
            res = asyncio.run(self.manager.add_symbol("GTCUSDT", strategy_id="u17_stephensonE__classic"))
            self.assertTrue(res)

            saved_app = json.loads(self.app_json_path.read_text(encoding="utf-8"))
            strat = saved_app["active_strategies"]["u17_stephensonE__classic"]
            self.assertIn("GTCUSDT", strat["LONG"]["invest_size"])
            self.assertIn("GTCUSDT", strat["SHORT"]["invest_size"])
            self.assertEqual(strat["LONG"]["invest_size"]["GTCUSDT"], 400.0)
            self.assertEqual(strat["SHORT"]["invest_size"]["GTCUSDT"], 400.0)

            # 2. Удаляем монету GTCUSDT
            dummy_runtime = self.runtime_dir / "gtcusdt.json"
            dummy_runtime.write_text("{}", encoding="utf-8")

            del_res = asyncio.run(self.manager.delete_symbol("GTCUSDT"))
            self.assertTrue(del_res)

            saved_app_del = json.loads(self.app_json_path.read_text(encoding="utf-8"))
            strat_del = saved_app_del["active_strategies"]["u17_stephensonE__classic"]
            self.assertNotIn("GTCUSDT", strat_del["LONG"]["invest_size"])
    def test_add_symbol_uses_explicit_default_invest_size_from_config(self):
        """Проверка что add_symbol использует именно явный default_invest_size (например 250$), а не хардкод 400$."""
        app_data = {
            "active_strategies": {
                "u17_stephensonE__classic": {
                    "enabled": True,
                    "default_invest_size": 250.0,
                    "LONG": {
                        "default_invest_size": 250.0,
                        "invest_size": {
                            "ICNTUSDT": 250.0
                        }
                    },
                    "SHORT": {
                        "default_invest_size": 150.0,
                        "invest_size": {
                            "ICNTUSDT": 150.0
                        }
                    }
                }
            }
        }
        self.app_json_path.write_text(json.dumps(app_data, indent=2), encoding="utf-8")

        with patch("CORE.SERVICES.symbol_lifecycle_manager.DATA_DIR", self.temp_dir), \
             patch("CORE.SERVICES.symbol_lifecycle_manager.BinancePublic.get_last_price", new_callable=AsyncMock, return_value=10.0), \
             patch("CORE.SERVICES.symbol_lifecycle_manager.StrategyApplicator"):

            res = asyncio.run(self.manager.add_symbol("GWEIUSDT", strategy_id="u17_stephensonE__classic"))
            self.assertTrue(res)

            saved_app = json.loads(self.app_json_path.read_text(encoding="utf-8"))
            strat = saved_app["active_strategies"]["u17_stephensonE__classic"]
            self.assertEqual(strat["LONG"]["invest_size"]["GWEIUSDT"], 250.0)
            self.assertEqual(strat["SHORT"]["invest_size"]["GWEIUSDT"], 150.0)

    def test_delete_symbol_in_app_json_not_in_bot(self):
        """Проверка что delete_symbol удаляет монету из app.json даже если ее не было в bot.symbols."""
        app_data = {
            "active_strategies": {
                "u17_stephensonE__classic": {
                    "enabled": True,
                    "default_invest_size": 400.0,
                    "LONG": {
                        "default_invest_size": 400.0,
                        "invest_size": {
                            "ICNTUSDT": 400.0
                        }
                    },
                    "SHORT": {
                        "default_invest_size": 400.0,
                        "invest_size": {
                            "ICNTUSDT": 400.0
                        }
                    }
                }
            }
        }
        self.app_json_path.write_text(json.dumps(app_data, indent=2), encoding="utf-8")

        # Монета ICNTUSDT есть в app.json, но отсутствует в self.mock_bot.symbols
        self.mock_bot.symbols = []

        with patch("CORE.SERVICES.symbol_lifecycle_manager.DATA_DIR", self.temp_dir), \
             patch("CORE.SERVICES.symbol_lifecycle_manager.BinancePublic.get_last_price", new_callable=AsyncMock, return_value=10.0), \
             patch("CORE.SERVICES.symbol_lifecycle_manager.StrategyApplicator"):

            res = asyncio.run(self.manager.delete_symbol("ICNTUSDT"))
            self.assertTrue(res)

            saved_app = json.loads(self.app_json_path.read_text(encoding="utf-8"))
            strat = saved_app["active_strategies"]["u17_stephensonE__classic"]
            self.assertNotIn("ICNTUSDT", strat["LONG"]["invest_size"])
            self.assertNotIn("ICNTUSDT", strat["SHORT"]["invest_size"])

    def test_delete_symbol_blocked_when_position_open(self):
        """Проверка что delete_symbol блокирует удаление если в FSM открыта позиция."""
        from POS_FSM.models import PositionState
        open_fsm = PositionState(symbol="BTCUSDT", side="LONG")
        open_fsm.in_position = True
        open_fsm.total_volume = 1.5

        self.mock_bot.symbols = ["BTCUSDT"]
        self.mock_bot.fsm_states = {
            "BTCUSDT": {
                "LONG": open_fsm,
                "SHORT": PositionState(symbol="BTCUSDT", side="SHORT")
            }
        }

        with patch("CORE.SERVICES.symbol_lifecycle_manager.DATA_DIR", self.temp_dir):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(self.manager.delete_symbol("BTCUSDT"))
            self.assertIn("active LONG position is currently open", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
