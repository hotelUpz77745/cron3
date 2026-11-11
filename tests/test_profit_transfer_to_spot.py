# ==============================================================================
# Path: tests/test_profit_transfer_to_spot.py
# Role: Юнит- и интеграционные тесты автоматического перевода профита trailing_ratchet на Spot
# ==============================================================================

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from API.BINANCE.client_prod import BinanceClient as ProdBinanceClient
from API.BINANCE.client_paper import BinanceClient as PaperBinanceClient
from API.BINANCE.client_sim import SimulatedBinanceClient
from API.BINANCE.transfer_manager import (
    TransferManager,
    TRANSFER_TYPE_FUTURES_TO_SPOT,
    TRANSFER_TYPE_SPOT_TO_FUTURES,
    UNIVERSAL_TRANSFER_ENDPOINT,
)
from API.BINANCE.validator import APIResponse
from CORE.SERVICES.dome_closer_executor import DomeCloserExecutor
from CORE.SERVICES.profit_transfer_service import ProfitTransferService
from CORE.STRATEGY.strategy_config import StrategyConfig


class TestProfitTransferToSpot(unittest.TestCase):
    """Тестирование цепочки перевода профита итерации с USDⓈ-M Futures на Spot при срабатывании trailing_ratchet."""

    def test_universe_templates_have_transfer_enabled_by_default(self):
        """Проверяет, что во всех 4 боевых шаблонах CFG/universes/*.json включен перевод 50% профита на спот."""
        universes_dir = _ROOT / "CFG" / "universes"
        for tpl_name in ("u96_omni_zenith13", "u96_omni_zenith13atr", "u96_omni_alligator", "u97_zenith13_hedge_shield"):
            tpl_path = universes_dir / f"{tpl_name}.json"
            self.assertTrue(tpl_path.exists(), f"Missing template {tpl_path}")
            raw_meta = json.loads(tpl_path.read_text(encoding="utf-8"))

            strat_cfg = StrategyConfig(
                strategy_id=f"{tpl_name}__trailing_ratchet",
                raw_meta=raw_meta,
                symbols=["ERAUSDT", "ZECUSDT"],
                active_settings={"auto_closing_mode": "trailing_ratchet", "default_invest_size": 1200},
            )
            t_cfg = strat_cfg.transfer_profit_to_spot_cfg
            self.assertTrue(t_cfg["enabled"], f"{tpl_name} should have transfer_profit_to_spot.enabled=True")
            self.assertAlmostEqual(t_cfg["transfer_profit_pct"], 50.0)
            self.assertAlmostEqual(t_cfg["min_transfer_usdt"], 1.0)
            self.assertEqual(t_cfg["asset"], "USDT")

    def test_config_override_disable_and_custom_pct(self):
        """Проверяет отключение опции и кастомный процент (например, 50%) через active_settings."""
        tpl_path = _ROOT / "CFG" / "universes" / "u96_omni_zenith13.json"
        raw_meta = json.loads(tpl_path.read_text(encoding="utf-8"))

        # 1. Полное отключение через булев флаг
        strat_disabled = StrategyConfig(
            strategy_id="u96_omni_zenith13__trailing_ratchet",
            raw_meta=raw_meta,
            symbols=["ERAUSDT"],
            active_settings={
                "auto_closing_mode": "trailing_ratchet",
                "default_invest_size": 1200,
                "transfer_profit_to_spot": False,
            },
        )
        self.assertFalse(strat_disabled.transfer_profit_to_spot_cfg["enabled"])

        # 2. Кастомный процент 50.0% через словарь оверрайда
        strat_half = StrategyConfig(
            strategy_id="u96_omni_zenith13__trailing_ratchet",
            raw_meta=raw_meta,
            symbols=["ERAUSDT"],
            active_settings={
                "auto_closing_mode": "trailing_ratchet",
                "default_invest_size": 1200,
                "auto_closing": {
                    "positive": {
                        "trailing_ratchet": {
                            "transfer_profit_to_spot": {
                                "enabled": True,
                                "transfer_profit_pct": 50.0,
                                "min_transfer_usdt": 2.0,
                            }
                        }
                    }
                },
            },
        )
        half_cfg = strat_half.transfer_profit_to_spot_cfg
        self.assertTrue(half_cfg["enabled"])
        self.assertAlmostEqual(half_cfg["transfer_profit_pct"], 50.0)
        self.assertAlmostEqual(half_cfg["min_transfer_usdt"], 2.0)

    def test_calculate_transfer_amount_math_and_guards(self):
        """Проверяет округление вниз (ROUND_DOWN), расчет процентов и фильтр min_transfer_usdt."""
        cfg_100 = {"enabled": True, "transfer_profit_pct": 100.0, "min_transfer_usdt": 1.0, "asset": "USDT"}
        # 26.459 -> строго 26.45 (ROUND_DOWN, чтобы не запросить к выводу больше реализованного остатка)
        self.assertAlmostEqual(ProfitTransferService.calculate_transfer_amount(26.459, cfg_100), 26.45)

        cfg_50 = {"enabled": True, "transfer_profit_pct": 50.0, "min_transfer_usdt": 1.0, "asset": "USDT"}
        self.assertAlmostEqual(ProfitTransferService.calculate_transfer_amount(25.0, cfg_50), 12.50)

        # Ниже min_transfer_usdt -> 0.0
        self.assertAlmostEqual(ProfitTransferService.calculate_transfer_amount(0.95, cfg_100), 0.0)
        # Отрицательный или нулевой профит -> 0.0
        self.assertAlmostEqual(ProfitTransferService.calculate_transfer_amount(-5.0, cfg_100), 0.0)
        self.assertAlmostEqual(ProfitTransferService.calculate_transfer_amount(0.0, cfg_100), 0.0)
        # Выключен -> 0.0
        cfg_off = {"enabled": False, "transfer_profit_pct": 100.0, "min_transfer_usdt": 1.0}
        self.assertAlmostEqual(ProfitTransferService.calculate_transfer_amount(50.0, cfg_off), 0.0)

    def test_prod_client_sapi_transfer_request_signature(self):
        """Проверяет формирование SAPI запроса POST /sapi/v1/asset/transfer (type=UMFUTURE_MAIN) в боевом клиенте."""
        client = ProdBinanceClient(api_key="test_key", api_secret="test_secret")
        client._request = AsyncMock(return_value=APIResponse(success=True, data={"tranId": 987654321}))

        res = asyncio.run(client.transfer_futures_to_spot(amount=26.459, asset="USDT"))
        self.assertTrue(res.success)
        client._request.assert_awaited_once_with(
            "POST",
            UNIVERSAL_TRANSFER_ENDPOINT,
            params={
                "asset": "USDT",
                "amount": "26.45",
                "type": TRANSFER_TYPE_FUTURES_TO_SPOT,
                "recvWindow": 20000,
            },
            signed=True,
        )

        # Обратный перевод со Spot на Futures
        client._request.reset_mock()
        res_back = asyncio.run(client.transfer_spot_to_futures(amount=10.0, asset="USDT"))
        self.assertTrue(res_back.success)
        client._request.assert_awaited_once_with(
            "POST",
            UNIVERSAL_TRANSFER_ENDPOINT,
            params={
                "asset": "USDT",
                "amount": "10.00",
                "type": TRANSFER_TYPE_SPOT_TO_FUTURES,
                "recvWindow": 20000,
            },
            signed=True,
        )

    def test_simulated_and_paper_client_transfer_accounting(self):
        """
        Проверяет эмуляцию вывода на спот в SimulatedBinanceClient:
        - списание с фьючерсного баланса (self.balance) и уменьшение свободной маржи;
        - зачисление на self.spot_balance;
        - отсутствие ложного скачка просадки (max_drawdown_usdt == 0.0) при переводе в свой спот-карман.
        """
        sim = SimulatedBinanceClient(start_balance_usdt=1000.0, fee_rate=0.0, maker_fee_rate=0.0)
        sim.on_tick("ERAUSDT", 100.0, 1.0, 1700000000000)
        asyncio.run(sim.make_order("ERAUSDT", qty=1.0, side="BUY", position_side="LONG", market_type="MARKET"))
        sim.on_tick("ERAUSDT", 150.0, 1.0, 1700000001000)
        asyncio.run(sim.make_order("ERAUSDT", qty=1.0, side="SELL", position_side="LONG", market_type="MARKET"))

        self.assertAlmostEqual(sim.balance, 1050.0)
        self.assertAlmostEqual(sim.peak_balance, 1050.0)
        self.assertAlmostEqual(sim.max_drawdown_usdt, 0.0)

        res = asyncio.run(sim.transfer_futures_to_spot(amount=50.0, asset="USDT"))
        self.assertTrue(res.success)
        self.assertAlmostEqual(sim.balance, 1000.0)
        self.assertAlmostEqual(sim.spot_balance, 50.0)
        self.assertAlmostEqual(sim.total_transferred_to_spot_usdt, 50.0)
        self.assertAlmostEqual(sim.get_free_margin(), 1000.0)
        self.assertAlmostEqual(sim.get_total_equity(), 1050.0)

        # Следующая сделка без убытка не должна считать вывод на спот просадкой!
        sim.on_tick("ERAUSDT", 150.0, 1.0, 1700000002000)
        asyncio.run(sim.make_order("ERAUSDT", qty=1.0, side="BUY", position_side="LONG", market_type="MARKET"))
        asyncio.run(sim.make_order("ERAUSDT", qty=1.0, side="SELL", position_side="LONG", market_type="MARKET"))
        self.assertAlmostEqual(sim.max_drawdown_usdt, 0.0)
        stats = sim.get_performance_stats()
        self.assertAlmostEqual(stats["wallet_balance"], 1000.0)
        self.assertAlmostEqual(stats["spot_balance"], 50.0)
        self.assertAlmostEqual(stats["net_profit"], 50.0)

        # Paper client
        paper = PaperBinanceClient()
        res_p = asyncio.run(paper.transfer_futures_to_spot(amount=18.25, asset="USDT"))
        self.assertTrue(res_p.success)
        self.assertAlmostEqual(paper.spot_balance, 18.25)

    def test_dome_closer_executor_triggers_spot_transfer_on_ratchet_close(self):
        """Проверяет полный пайплайн DomeCloserExecutor -> ProfitTransferFSM -> фоновый перевод на Spot -> TG-алерт."""
        from ANALYTICS.analytics_fsm import AnalyticsState
        from CORE.SERVICES.profit_transfer_fsm import ProfitTransferFSM, ProfitTransferState

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            stats_dir = tmp_path / "STATS"
            stats_dir.mkdir(parents=True, exist_ok=True)
            strat_id = "u96_omni_zenith13__trailing_ratchet"
            stats_path = stats_dir / f"analytics_{strat_id}.json"
            app_json_path = tmp_path / "app.json"
            raw_analytics = {
                "net_profit_usdt": 32.40,
                "realized_pnl_net_usdt": 32.40,
                "unrealized_pnl_usdt": 0.0,
                "iteration_index": 2,
                "_floating_synced": True,
                "per_coin": {"ERAUSDT": {"long_amt": 0.0, "short_amt": 0.0}},
            }
            stats_path.write_text(json.dumps(raw_analytics), encoding="utf-8")
            app_json_path.write_text(json.dumps({"app": {"auto_start": True}}), encoding="utf-8")

            mock_client = MagicMock()
            mock_client.transfer_futures_to_spot = AsyncMock(
                return_value=APIResponse(success=True, data={"tranId": 555777, "amount": 26.40})
            )

            bot_core = MagicMock()
            bot_core.current_sim_time = 2000.0
            bot_core.close_strategy_positions = AsyncMock()
            bot_core.get_client_for_strategy = MagicMock(return_value=mock_client)
            bot_core.strategy_blocked_until = {strat_id: 2305.0}
            bot_core.block_until_timestamp = 0.0
            bot_core.notifier = MagicMock()
            bot_core.notifier.send_alert = AsyncMock()
            bot_core.analytics = MagicMock()
            bot_core.analytics._sync_locks = set()
            bot_core.analytics._sync_in_progress = asyncio.Lock()
            bot_core.analytics.last_deep_sync_ts = 0.0
            bot_core.analytics.last_deep_sync_ts_by_client = {}
            bot_core.analytics.fsm = MagicMock()
            bot_core.analytics.fsm.state = AnalyticsState.AUTOCLOSER_READY
            bot_core.analytics.fsm.is_autocloser_ready = MagicMock(return_value=True)
            bot_core.analytics.fsm._iteration_state = {}
            bot_core.analytics.fsm._auto_close_history = {}
            bot_core.fsm_states = {}

            strat_cfg = MagicMock()
            strat_cfg.name = "U96: Omni Zenith 13"
            strat_cfg.symbols = ["ERAUSDT"]
            strat_cfg.raw_meta = {
                "auto_closing": {
                    "positive": {
                        "min_guaranteed_profit_usdt": 5.0,
                        "trailing_ratchet": {
                            "transfer_profit_to_spot": {
                                "enabled": True,
                                "transfer_profit_pct": 100.0,
                                "min_transfer_usdt": 1.0,
                                "asset": "USDT",
                            }
                        },
                    }
                }
            }
            strat_cfg.overrides = {}
            strat_cfg.auto_closing_cfg = strat_cfg.raw_meta["auto_closing"]
            strat_cfg.active_settings = {}

            pt_fsm = ProfitTransferFSM(stats_dir=stats_dir, cache_dir=tmp_path)
            action_msg = (
                "🎯 <b>АВТО-ЗАКРЫТИЕ КУПОЛА [Omen 13] (TRAILING RATCHET)</b>\n"
                "Цикл <b>#3</b> (базовая точка Net: <b>0.00 USDT</b>)"
            )
            ratchet_state = {strat_id: {"active_level_idx": 1}}

            async def _run():
                with patch("CORE.SERVICES.dome_closer_executor.CACHE_DIR", tmp_path), patch(
                    "CORE.SERVICES.profit_transfer_fsm.time.time", return_value=2000.0
                ):
                    await DomeCloserExecutor.execute_close(
                        bot_core=bot_core,
                        strat_id=strat_id,
                        is_global=False,
                        strat_cfg=strat_cfg,
                        iter_idx=2,
                        next_iter_idx=3,
                        iter_net=26.40,
                        strat_net=32.40,
                        stats_path=stats_path,
                        raw_analytics=raw_analytics,
                        app_data={"app": {"auto_start": True}},
                        app_json_path=app_json_path,
                        action_msg=action_msg,
                        trailing_ratchet_state=ratchet_state,
                        is_emergency_stop=False,
                        profit_transfer_fsm=pt_fsm,
                    )

                # Сразу после закрытия перевод еще НЕ выполнен — ждет сверки аналитики и нового цикла сердцебиения
                mock_client.transfer_futures_to_spot.assert_not_awaited()
                self.assertEqual(pt_fsm.get_state(strat_id), ProfitTransferState.AWAITING_SETTLED_ANALYTICS)

                # До завершения deep_sync_analytics перевод заблокирован
                await pt_fsm.process_tick(bot_core, {strat_id: strat_cfg})
                self.assertEqual(pt_fsm.get_state(strat_id), ProfitTransferState.AWAITING_SETTLED_ANALYTICS)

                # Подтверждаем завершение deep_sync после закрытия (но время сердцебиения еще не наступило: 2100 < 2305)
                bot_core.analytics.last_deep_sync_ts = 2005.0
                bot_core.current_sim_time = 2100.0
                await pt_fsm.process_tick(bot_core, {strat_id: strat_cfg})
                self.assertEqual(pt_fsm.get_state(strat_id), ProfitTransferState.AWAITING_HEARTBEAT_START)
                mock_client.transfer_futures_to_spot.assert_not_awaited()

                # Наступает новый тик сердцебиения (2305 >= 2305) -> фоновый перевод срабатывает строго 1 раз
                bot_core.current_sim_time = 2305.0
                await pt_fsm.process_tick(bot_core, {strat_id: strat_cfg})
                await pt_fsm.wait_for_pending_tasks()

            asyncio.run(_run())

            bot_core.close_strategy_positions.assert_awaited_once_with(strat_id)
            mock_client.transfer_futures_to_spot.assert_awaited_once_with(amount=26.40, asset="USDT")
            self.assertEqual(bot_core.notifier.send_alert.await_count, 2)
            spot_alert = bot_core.notifier.send_alert.call_args_list[1][0][0]
            self.assertIn("Перевод профита на Spot: <b>+26.40 USDT</b>", spot_alert)
            self.assertIn("100%", spot_alert)

    def test_disabled_spot_transfer_keeps_766724b_ratchet_baseline_even_below_start(self):
        """
        При отключенном переводе на Spot (поведение 766724b) храповик продолжает считать
        iter_net строго от предыдущей базы фиксации, не меняет net_profit и не вызывает SAPI.
        """
        from CORE.SERVICES.auto_closer import AutoCloser
        from CORE.SERVICES.profit_transfer_fsm import ProfitTransferFSM, ProfitTransferState

        strat_id = "u96_omni_zenith13__trailing_ratchet"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stats_dir = tmp_path / "stats"
            stats_dir.mkdir(parents=True, exist_ok=True)
            stats_path = stats_dir / f"analytics_{strat_id}.json"
            # Например, старт 1435, общий кумулятивный минус -48.85$,
            # предыдущая итерация закрылась на -78.85$, текущая итерация заработала +30.00$
            raw_analytics = {
                "start_balance_usdt": 1435.0,
                "net_profit_usdt": -48.85,
                "realized_pnl_net_usdt": -48.85,
                "iteration_index": 2,
                "iteration_start_net_profit_usdt": -78.85,
                "_floating_synced": True,
                "per_coin": {"ERAUSDT": {"unrealized_pnl_usdt": 0.0, "total_volume": 0.0}},
            }
            stats_path.write_text(json.dumps(raw_analytics), encoding="utf-8")

            mock_client = MagicMock()
            mock_client.transfer_futures_to_spot = AsyncMock()
            bot_core = MagicMock()
            bot_core.current_sim_time = 2000.0
            bot_core.strategy_blocked_until = {}
            bot_core.block_until_timestamp = 0.0
            bot_core.strategy_registry = MagicMock()
            bot_core.get_client_for_strategy = MagicMock(return_value=mock_client)
            bot_core.close_strategy_positions = AsyncMock()
            bot_core.notifier = MagicMock()
            bot_core.notifier.send_alert = AsyncMock()
            bot_core.fsm_states = {}
            bot_core.analytics = MagicMock()
            bot_core.analytics.last_deep_sync_ts = 2005.0
            bot_core.analytics.last_deep_sync_ts_by_client = {}
            bot_core.analytics._sync_locks = set()
            bot_core.analytics._sync_in_progress = asyncio.Lock()
            bot_core.analytics.fsm = MagicMock()
            bot_core.analytics.fsm.is_autocloser_ready = MagicMock(return_value=True)

            strat_cfg = MagicMock()
            strat_cfg.name = "U96 OFF Spot"
            strat_cfg.symbols = ["ERAUSDT"]
            strat_cfg.raw_meta = {
                "auto_closing": {
                    "positive": {
                        "min_guaranteed_profit_usdt": 5.0,
                        "trailing_ratchet": {
                            "transfer_profit_to_spot": {"enabled": False, "transfer_profit_pct": 50.0, "min_transfer_usdt": 1.0}
                        },
                    }
                }
            }
            strat_cfg.overrides = {}
            strat_cfg.auto_closing_cfg = strat_cfg.raw_meta["auto_closing"]
            strat_cfg.active_settings = {}

            # Проверяем работу самого храповика на +30$ от -78.85$ даже при глобальном Net -48.85$ < 0
            r_state = {}
            ratchet_cfg = {"levels": [{"activation": 26.0, "floor": 16.0, "min_age_hours": 0.0, "enabled": True}]}
            iter_net_now = round(raw_analytics["net_profit_usdt"] - raw_analytics["iteration_start_net_profit_usdt"], 4)
            self.assertAlmostEqual(iter_net_now, 30.0)
            AutoCloser.evaluate_trailing_ratchet(iter_net=30.0, iter_age_hours=1.0, ratchet_cfg=ratchet_cfg, state=r_state)
            triggered, floor, _, _ = AutoCloser.evaluate_trailing_ratchet(iter_net=15.5, iter_age_hours=1.0, ratchet_cfg=ratchet_cfg, state=r_state)
            self.assertTrue(triggered)
            self.assertAlmostEqual(floor, 16.0)

            pt_fsm = ProfitTransferFSM(stats_dir=stats_dir, cache_dir=tmp_path)
            async def _run():
                with patch("CORE.SERVICES.dome_closer_executor.CACHE_DIR", tmp_path), patch("CORE.SERVICES.profit_transfer_fsm.time.time", return_value=2000.0):
                    await DomeCloserExecutor.execute_close(
                        bot_core=bot_core, strat_id=strat_id, is_global=False, strat_cfg=strat_cfg,
                        iter_idx=2, next_iter_idx=3, iter_net=30.0, strat_net=-48.85,
                        stats_path=stats_path, raw_analytics=raw_analytics,
                        app_data={"app": {"auto_start": True}}, app_json_path=tmp_path / "app.json",
                        action_msg="🎯 (TRAILING RATCHET)", trailing_ratchet_state={strat_id: {}},
                        is_emergency_stop=False, profit_transfer_fsm=pt_fsm,
                    )
                    # Выключенный transfer_profit_to_spot даже не взводит FSM и не дергает SAPI
                    self.assertEqual(pt_fsm.get_state(strat_id), ProfitTransferState.IDLE)
                    await pt_fsm.process_tick(bot_core, {strat_id: strat_cfg})
                    await pt_fsm.wait_for_pending_tasks()
                    c_data = json.loads((tmp_path / "iteration_state.json").read_text(encoding="utf-8"))
                    self.assertNotIn("transferred_to_spot_usdt", c_data[strat_id])
                    self.assertAlmostEqual(c_data[strat_id]["iteration_start_net_profit_usdt"], -48.85)

            asyncio.run(_run())
            mock_client.transfer_futures_to_spot.assert_not_awaited()

    def test_50pct_symbolic_transfer_preserves_ratchet_and_builds_margin_buffer(self):
        """
        При transfer_profit_pct=50.0% на каждой прибыльной итерации (при балансе > старт)
        50% уходит на Spot, а 50% остается как маржинальный буфер на фьючерсах, при этом
        храповик все равно считает прибыль следующей итерации строго от базы предыдущего закрытия.
        """
        from CORE.SERVICES.profit_transfer_fsm import ProfitTransferFSM

        strat_id = "u96_omni_zenith13__trailing_ratchet"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stats_dir = tmp_path / "stats"
            stats_dir.mkdir(parents=True, exist_ok=True)
            stats_path = stats_dir / f"analytics_{strat_id}.json"

            # После первой итерации +30$, было переведено 50% = 15.0$.
            # Вторая итерация дала просадку -20$ (Net=+10, на фьючерсе 1435+10-15 = 1430 < 1435),
            # Затем третья итерация закрыла храповик +24$ -> общий Net=+34$, свыше 1435$ = 34 - 15 = +19$
            # Допустимый профит = min(24, 19) = 19$ -> перевод 50% = 9.50$ на Spot, всего 24.50$ на Spot!
            sdata = {
                "net_profit_usdt": 34.0,
                "realized_pnl_net_usdt": 34.0,
                "iteration_index": 4,
                "iteration_start_net_profit_usdt": 10.0,
                "transferred_to_spot_usdt": 15.0,
                "_floating_synced": True,
                "per_coin": {},
            }
            stats_path.write_text(json.dumps(sdata), encoding="utf-8")

            mock_client = MagicMock()
            mock_client.transfer_futures_to_spot = AsyncMock(
                return_value=APIResponse(success=True, data={"tranId": 9901, "amount": 9.50})
            )
            bot_core = MagicMock()
            bot_core.current_sim_time = 3000.0
            bot_core.get_client_for_strategy = MagicMock(return_value=mock_client)
            bot_core.notifier = MagicMock()
            bot_core.notifier.send_alert = AsyncMock()
            bot_core.analytics = MagicMock()
            bot_core.analytics.last_deep_sync_ts = 3005.0
            bot_core.analytics.last_deep_sync_ts_by_client = {}
            bot_core.analytics._sync_locks = set()
            bot_core.analytics._sync_in_progress = asyncio.Lock()
            bot_core.analytics.fsm = MagicMock()
            bot_core.analytics.fsm.is_autocloser_ready = MagicMock(return_value=True)

            strat_cfg = MagicMock()
            strat_cfg.name = "Zenith13"
            strat_cfg.symbols = []
            strat_cfg.raw_meta = {
                "auto_closing": {
                    "positive": {
                        "min_guaranteed_profit_usdt": 5.0,
                        "trailing_ratchet": {
                            "transfer_profit_to_spot": {"enabled": True, "transfer_profit_pct": 50.0, "min_transfer_usdt": 1.0, "asset": "USDT"}
                        },
                    }
                }
            }
            strat_cfg.overrides = {}
            strat_cfg.auto_closing_cfg = strat_cfg.raw_meta["auto_closing"]
            strat_cfg.active_settings = {}

            pt_fsm = ProfitTransferFSM(stats_dir=stats_dir, cache_dir=tmp_path)
            pt_fsm.arm_on_ratchet_trigger(
                strat_id=strat_id, strat_cfg=strat_cfg, close_type="TRAILING RATCHET",
                closed_iter_idx=3, next_iter_idx=4, prev_baseline_net=10.0,
                ready_at_ts=3000.0, stats_path=stats_path,
            )
            pt_fsm.mark_positions_closed(strat_id=strat_id, now_ts=3000.0, ready_at_ts=3000.0)

            async def _run():
                with patch("CORE.SERVICES.profit_transfer_fsm.time.time", return_value=3000.0):
                    await pt_fsm.process_tick(bot_core, {strat_id: strat_cfg})
                    await pt_fsm.wait_for_pending_tasks()

            asyncio.run(_run())
            mock_client.transfer_futures_to_spot.assert_awaited_once_with(amount=9.50, asset="USDT")
            persisted = json.loads(stats_path.read_text(encoding="utf-8"))
            self.assertAlmostEqual(persisted["transferred_to_spot_usdt"], 24.50)


if __name__ == "__main__":
    unittest.main()
