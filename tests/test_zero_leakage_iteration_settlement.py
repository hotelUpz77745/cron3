# tests/test_zero_leakage_iteration_settlement.py
# -*- coding: utf-8 -*-
import asyncio
import csv
import json
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from ANALYTICS.strategy_stats import StrategyStatsEngine
from ANALYTICS.analytics_fsm import AnalyticsFSM
from CORE.SERVICES.dome_closer_executor import DomeCloserExecutor


class TestZeroLeakageIterationSettlement(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.stats_dir = self.test_dir / "STATS"
        self.ledgers_dir = self.test_dir / "LEDGERS"
        self.cache_dir = self.test_dir / "CACHE"
        self.stats_dir.mkdir(parents=True, exist_ok=True)
        self.ledgers_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.fsm = AnalyticsFSM(
            stats_dir=self.stats_dir,
            cache_dir=self.cache_dir,
            log_file=self.stats_dir / "analytics.json",
        )

        self.manager = MagicMock()
        self.manager.fsm = self.fsm
        self.manager.ledgers_dir = self.ledgers_dir
        self.manager.txt_file = self.ledgers_dir / "trades_ledger.txt"
        self.manager.stats_dir = self.stats_dir
        self.manager.cache_dir = self.cache_dir
        self.manager._read_data.return_value = {
            "start_balance_usdt": 1435.0,
            "per_coin": {},
        }
        self.manager.strategy_registry = MagicMock()
        self.manager.strategy_registry.get_active_strategies.return_value = {}

        self.engine = StrategyStatsEngine(self.manager)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_zero_leakage_after_autoclose(self):
        """
        Проверка: когда ордера закрытия старой итерации оприходуются на бирже
        с задержкой в несколько секунд (после старта новой итерации), их профит (+17.71$)
        НЕ должен просачиваться на новую итерацию как прибыль третьей итерации.
        """
        strat_id = "u96_omni_zenith13__trailing_ratchet"
        now_ts = int(time.time() * 1000)
        iter2_start_ts = now_ts - 3600000 * 24

        # Имитируем состояние перед автозакрытием Iteration #2
        strat_cfg = MagicMock()
        strat_cfg.name = "Zenith 13"
        strat_cfg.symbols = ["ERAUSDT", "MERLUSDT"]
        strat_cfg.start_balance_usdt = 1435.0

        # Предварительная история автозакрытия: Iteration #1 была закрыта с net 50.07$
        self.fsm._auto_close_history = {
            strat_id: [
                {
                    "iteration_index": 1,
                    "close_type": "TRAILING RATCHET",
                    "strat_net": 50.07,
                    "settled_realized": 50.07,
                    "iter_net": 50.07,
                }
            ]
        }
        self.fsm._iteration_state = {
            strat_id: {
                "iteration_index": 2,
                "iteration_start_net_profit_usdt": 50.07,
                "iteration_start_realized_pnl_usdt": 50.07,
                "iteration_start_ts": iter2_start_ts,
            }
        }

        # Бот исполняет автозакрытие Iteration #2
        bot_core = MagicMock()
        bot_core.close_strategy_positions = AsyncMock()
        bot_core.analytics = self.manager
        bot_core.fsm_states = {}
        bot_core.notifier = MagicMock()
        bot_core.notifier.send_alert = AsyncMock()

        stats_path = self.stats_dir / f"analytics_{strat_id}.json"
        raw_analytics = {
            "iteration_index": 2,
            "iteration_start_net_profit_usdt": 50.07,
            "iteration_start_realized_pnl_usdt": 50.07,
            "net_profit_usdt": 67.65,
            "realized_pnl_net_usdt": 50.07,
            "unrealized_pnl_usdt": 17.58,
            "start_balance_usdt": 1435.0,
        }
        stats_path.write_text(json.dumps(raw_analytics), encoding="utf-8")

        r_state = {strat_id: {}}
        app_data = {}
        app_json = self.test_dir / "app.json"

        with patch("CORE.SERVICES.dome_closer_executor.CACHE_DIR", self.cache_dir):
            await DomeCloserExecutor.execute_close(
                bot_core=bot_core,
                strat_id=strat_id,
                is_global=False,
                strat_cfg=strat_cfg,
                iter_idx=2,
                next_iter_idx=3,
                iter_net=17.58,
                strat_net=67.65,
                stats_path=stats_path,
                raw_analytics=raw_analytics,
                app_data=app_data,
                app_json_path=app_json,
                action_msg="Close iter 2",
                trailing_ratchet_state=r_state,
            )

        # Проверяем, что создана итерация #3
        iter3_stats = json.loads(stats_path.read_text(encoding="utf-8"))
        self.assertEqual(iter3_stats["iteration_index"], 3)
        iter3_start_ts = iter3_stats["iteration_start_ts"]

        # Имитируем приход трейдов ликвидации с биржи в trades_ledger_u96_omni_zenith13__trailing_ratchet.txt
        # Их время закрытия строго ДО момента старта 3-й итерации (now_ts - 2000)
        close_dt_str = datetime.fromtimestamp((iter3_start_ts - 2000) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        strat_ledger = self.ledgers_dir / f"trades_ledger_{strat_id}.txt"
        with open(strat_ledger, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(["Id", "Symbol", "Side", "Open Time", "Close Time", "PnL (USDT)", "Balance"])
            # Старые трейды итерации #1
            writer.writerow([1, "ERAUSDT", "BUY", "2026-11-13 10:00:00", "2026-11-13 11:00:00", 50.07, 1485.07])
            # 12 закрывающих ордеров итерации #2 суммарно на +17.71$
            writer.writerow([2, "ERAUSDT", "SELL", "2026-11-14 02:00:00", close_dt_str, 10.00, 1495.07])
            writer.writerow([3, "MERLUSDT", "BUY", "2026-11-14 02:00:00", close_dt_str, 7.71, 1502.78])

        self.manager.strategy_registry.active_strategies = {
            strat_id: strat_cfg
        }
        self.manager._floating_synced = True
        self.manager.peels_ledger = self.ledgers_dir / "peels_ledger.txt"
        self.manager.recycling_ledger = self.ledgers_dir / "recycling_ledger.txt"

        global_data = {
            "_floating_synced": True,
            "start_balance_usdt": 1435.0,
            "per_coin": {
                "ERAUSDT": {
                    "realized_pnl_net_usdt": 60.07,
                    "current_drawdown": -0.08,
                    "total_trades": 2,
                    "winning_trades": 2,
                },
                "MERLUSDT": {
                    "realized_pnl_net_usdt": 7.71,
                    "current_drawdown": -0.05,
                    "total_trades": 1,
                    "winning_trades": 1,
                },
            },
        }

        # Запускаем расчет аналитики стратегии
        with patch.object(self.engine, "get_iteration_cache", return_value={"iteration_index": 3, "iteration_start_ts": iter3_start_ts}):
            with patch.object(self.engine, "save_iteration_cache"):
                self.engine.update_strategies_stats(global_data)

        # Читаем пересчитанные данные аналитики стратегии с диска
        strat_data = json.loads(stats_path.read_text(encoding="utf-8"))

        # ПРОВЕРКА ГАРАНТИИ ZERO-LEAKAGE:
        # 1. Realized прибыль 3-й итерации строго 0.0$ (старые трейды не утекли в 3-ю итерацию)
        self.assertEqual(strat_data["iteration_realized_pnl_usdt"], 0.0)

        # 2. Net profit 3-й итерации равен strictly плавающему PnL новых позиций (-0.13$), а не +17.58$
        self.assertEqual(strat_data["iteration_net_profit_usdt"], -0.13)

        # 3. Базовые точки итерации поглотили закрывающие трейды второй итерации
        self.assertEqual(strat_data["iteration_start_realized_pnl_usdt"], 67.78)
        self.assertEqual(strat_data["iteration_start_net_profit_usdt"], 67.78)

        # 4. Баланс стратегии равен Start (1435.0) + Net Profit (67.65) = 1502.65$
        self.assertEqual(strat_data["cur_balance_usdt"], 1502.65)

        # Теперь имитируем закрытие первой легитимной сделки ВНУТРИ 3-й итерации (+2.50$)
        future_close_dt = datetime.fromtimestamp((iter3_start_ts + 10000) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with open(strat_ledger, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow([4, "ERAUSDT", "SELL", "2026-11-14 03:05:00", future_close_dt, 2.50, 1505.15])

        global_data["per_coin"]["ERAUSDT"]["realized_pnl_net_usdt"] += 2.50
        global_data["per_coin"]["ERAUSDT"]["current_drawdown"] = 0.0
        global_data["per_coin"]["MERLUSDT"]["current_drawdown"] = 0.0

        with patch.object(self.engine, "get_iteration_cache", return_value={"iteration_index": 3, "iteration_start_ts": iter3_start_ts}):
            with patch.object(self.engine, "save_iteration_cache"):
                self.engine.update_strategies_stats(global_data)

        strat_data = json.loads(stats_path.read_text(encoding="utf-8"))

        # Легитимная прибыль 3-й итерации теперь точно +2.50$
        self.assertEqual(strat_data["iteration_realized_pnl_usdt"], 2.50)
        self.assertEqual(strat_data["iteration_net_profit_usdt"], 2.50)
        self.assertEqual(strat_data["iteration_start_realized_pnl_usdt"], 67.78)


if __name__ == "__main__":
    unittest.main()
