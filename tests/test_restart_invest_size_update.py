# ============================================================
# FILE: tests/test_restart_invest_size_update.py
# ROLE: Unit test for dynamic invest_size update on restart with existing open positions
# ============================================================

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from CORE.strategy_applicator import StrategyApplicator
from CORE.strategy_registry import StrategyConfig
from CORE.GRID.avg_manager import AverageManager
from POS_FSM.models import PositionState
from c_utils import Utils


class TestRestartInvestSizeUpdate(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.test_dir.name)
        self.runtime_dir = self.temp_path / "runtime" / "u17_stephensonE"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.test_dir.cleanup()

    def test_dynamic_invest_size_applied_to_existing_active_runtime(self):
        """
        Проверка: если позиция УЖЕ открыта в рантайме (старый invest_size: 400.0),
        а оператор поменял сайз в конфиге на 800.0 и перезапустил бота:
        1. FSM состояние (in_position, total_volume, avg_entry_price) сохраняется.
        2. invest_size и eff_invest в рантайме актуализируются до 800.0.
        3. Следующий ордер усреднения рассчитывается строго от нового сайза 800.0.
        """
        symbol = "ADAUSDT"
        target_file = self.runtime_dir / f"{symbol.lower()}.json"

        # 1. Существующий рантайм со старым invest_size = 400.0 и активной позицией в LONG
        old_runtime_data = {
            "LONG": {
                "invest_size": 400.0,
                "eff_invest": 400.0,
                "leverage": 20,
                "in_position": True,
                "total_volume": 50.0,
                "avg_entry_price": 0.40,
                "grid": {
                    "0": {"is_active": True, "volume": 12.0, "price": 0.40},
                    "1": {"is_active": False, "volume": 14.0, "price": None, "indent": -5.0}
                },
                "tp_map": {
                    "0": {"is_active": True}
                }
            },
            "SHORT": {
                "invest_size": 400.0,
                "eff_invest": 400.0,
                "leverage": 20,
                "in_position": False,
                "total_volume": 0.0,
                "grid": {
                    "0": {"is_active": False, "volume": 12.0}
                }
            }
        }
        target_file.write_text(json.dumps(old_runtime_data, indent=2), encoding="utf-8")

        # 2. Новая конфигурация стратегии с invest_size = 800.0
        new_strat_cfg = StrategyConfig(
            strategy_id="u17_stephensonE",
            raw_meta={
                "name": "u17_stephensonE",
                "grid": {
                    "0": {"volume": 12.0, "indent": 0.0},
                    "1": {"volume": 14.0, "indent": -5.0}
                },
                "tp_map": {"0": {"take_profit": 1.0}}
            },
            symbols=[symbol],
            active_settings={
                "enabled": True,
                "LONG": {
                    "default_invest_size": 800.0,
                    "invest_size": {symbol: 800.0}
                },
                "SHORT": {
                    "default_invest_size": 800.0,
                    "invest_size": {symbol: 800.0}
                }
            }
        )

        applicator = StrategyApplicator()
        applicator.build_runtime_for_symbol(
            symbol=symbol,
            strategy=new_strat_cfg,
            target_dir=self.runtime_dir
        )

        # 3. Проверяем обновленный JSON рантайма на диске
        saved_data = json.loads(target_file.read_text(encoding="utf-8"))
        long_saved = saved_data["LONG"]

        # FSM состояние бережно сохранено
        self.assertTrue(long_saved["in_position"])
        self.assertEqual(long_saved["total_volume"], 50.0)
        self.assertEqual(long_saved["avg_entry_price"], 0.40)
        self.assertTrue(long_saved["grid"]["0"]["is_active"])
        self.assertFalse(long_saved["grid"]["1"]["is_active"])

        # Сайзы актуализированы на 800.0!
        self.assertEqual(long_saved["invest_size"], 800.0)
        self.assertEqual(long_saved["eff_invest"], 800.0)

        # 4. Проверяем AvgManager: при расчете усреднения следующего ордера
        # объем должен рассчитываться строго от нового invest_size 800.0 (14% от 800 = $112 USDT)
        state = PositionState(symbol=symbol, side="LONG")
        state.in_position = True
        state.total_volume = 50.0
        state.avg_entry_price = 0.40
        state.eff_invest = 800.0

        mock_rm = MagicMock()
        mock_rm.caches = {symbol: saved_data}

        mock_client = AsyncMock()
        mock_order_res = MagicMock()
        mock_order_res.success = True
        mock_order_res.data = {"orderId": 12345}
        mock_client.make_order = AsyncMock(return_value=mock_order_res)
        mock_client.cancel_orders_for_side = AsyncMock()

        spec_data = {symbol: {"stepSize": "0.1", "minQty": "0.1", "tickSize": "0.0001"}}

        avg_mgr = AverageManager()
        level_1_data = long_saved["grid"]["1"]
        current_price = 0.38

        asyncio.run(avg_mgr._execute_averaging_order(
            client=mock_client,
            runtime_manager=mock_rm,
            symbol=symbol,
            side="LONG",
            level_str="1",
            level_data=level_1_data,
            current_price=current_price,
            spec_data=spec_data,
            state=state,
            opposite_state=PositionState(symbol=symbol, side="SHORT"),
            tp_manager=AsyncMock()
        ))

        # Ожидаемый объем: 800 * (14 / 100) / 0.38 = 112 / 0.38 = 294.7 контрактов
        mock_client.make_order.assert_called_once()
        called_kwargs = mock_client.make_order.call_args[1]
        self.assertEqual(called_kwargs["symbol"], symbol)
        self.assertEqual(called_kwargs["side"], "BUY")
        self.assertAlmostEqual(called_kwargs["qty"], 294.7, places=1)


if __name__ == "__main__":
    unittest.main()
