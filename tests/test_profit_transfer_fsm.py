# ==============================================================================
# Path: tests/test_profit_transfer_fsm.py
# Role: Матрица 100+ тестов конечного автомата ProfitTransferFSM, анти-лага аналитики,
#       отсутствия fetch_balance, идемпотентности и пер-купольной маршрутизации
# ==============================================================================

import ast
import asyncio
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ANALYTICS.analytics_fsm import AnalyticsFSM, AnalyticsState
from API.BINANCE.client_paper import BinanceClient as PaperBinanceClient
from API.BINANCE.client_prod import BinanceClient as ProdBinanceClient
from API.BINANCE.validator import APIResponse
from CORE.SERVICES.auto_closer import AutoCloser
from CORE.SERVICES.dome_closer_executor import DomeCloserExecutor
from CORE.SERVICES.profit_transfer_fsm import ProfitTransferFSM, ProfitTransferState
from CORE.SERVICES.profit_transfer_service import ProfitTransferService
from CORE.STRATEGY.strategy_config import StrategyConfig


def _make_strat_cfg(
    strat_id: str = "u96_omni_zenith13__trailing_ratchet",
    name: str = "Zenith 13 Dome",
    symbols: list[str] | None = None,
    enabled: bool = True,
    pct: float = 100.0,
    min_transfer: float = 1.0,
    min_guaranteed: float = 5.0,
) -> StrategyConfig:
    raw_meta = {
        "name": name,
        "auto_closing": {
            "positive": {
                "scale_money": 2.0,
                "min_guaranteed_profit_usdt": min_guaranteed,
                "trailing_ratchet": {
                    "transfer_profit_to_spot": {
                        "enabled": enabled,
                        "transfer_profit_pct": pct,
                        "min_transfer_usdt": min_transfer,
                        "asset": "USDT",
                    },
                    "levels": [{"activation": 13.0, "floor": 8.0, "enabled": True}],
                },
            }
        },
    }
    return StrategyConfig(
        strategy_id=strat_id,
        raw_meta=raw_meta,
        symbols=symbols or ["ERAUSDT", "ZECUSDT"],
        active_settings={
            "auto_closing_mode": "trailing_ratchet",
            "default_invest_size": 1200.0,
            "start_balance_usdt": 1435.0,
        },
    )


def _write_analytics_file(
    stats_dir: Path,
    strat_id: str,
    net_profit: float,
    iter_idx: int = 2,
    floating_synced: bool = True,
    unrealized: float = 0.0,
    per_coin: dict | None = None,
    transferred_to_spot: float = 0.0,
) -> Path:
    stats_dir.mkdir(parents=True, exist_ok=True)
    path = stats_dir / f"analytics_{strat_id}.json"
    payload = {
        "universe_id": strat_id,
        "start_balance_usdt": 1435.0,
        "net_profit_usdt": net_profit,
        "realized_pnl_net_usdt": round(net_profit - unrealized, 4),
        "unrealized_pnl_usdt": unrealized,
        "iteration_index": iter_idx,
        "iteration_start_net_profit_usdt": net_profit if unrealized == 0 else 0.0,
        "_floating_synced": floating_synced,
        "per_coin": per_coin
        if per_coin is not None
        else {
            "ERAUSDT": {"long_amt": 0.0, "short_amt": 0.0},
            "ZECUSDT": {"long_amt": 0.0, "short_amt": 0.0},
        },
    }
    if transferred_to_spot > 0.0:
        payload["transferred_to_spot_usdt"] = transferred_to_spot
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _make_bot_core(client: MagicMock, sim_time: float = 1000.0, stats_dir: Path | None = None, cache_dir: Path | None = None):
    bot = MagicMock()
    bot.current_sim_time = sim_time
    bot.client = client
    bot.clients = {}
    bot.get_client_for_strategy = MagicMock(side_effect=lambda sid: bot.clients.get(sid, client))
    bot.close_strategy_positions = AsyncMock()
    bot.notifier = MagicMock()
    bot.notifier.send_alert = AsyncMock()
    bot.prices = {"ERAUSDT": (1.0, sim_time), "ZECUSDT": (50.0, sim_time)}
    bot.strategy_blocked_until = {}
    bot.block_until_timestamp = 0.0
    bot.fsm_states = {}

    analytics = MagicMock()
    analytics._sync_locks = set()
    analytics._sync_in_progress = asyncio.Lock()
    analytics.last_deep_sync_ts = 0.0
    analytics.last_deep_sync_ts_by_client = {}
    analytics.deep_sync_analytics = AsyncMock()

    if stats_dir is not None and cache_dir is not None:
        afsm = AnalyticsFSM(stats_dir=stats_dir, cache_dir=cache_dir, log_file=stats_dir / "analytics.json")
        afsm.state = AnalyticsState.AUTOCLOSER_READY
        analytics.fsm = afsm
    else:
        afsm = MagicMock()
        afsm.state = AnalyticsState.AUTOCLOSER_READY
        afsm.is_autocloser_ready = MagicMock(return_value=True)
        afsm._iteration_state = {}
        afsm._auto_close_history = {}
        analytics.fsm = afsm

    bot.analytics = analytics
    return bot


class TestProfitTransferFSM(unittest.TestCase):
    """Comprehensive 100+ case test matrix for ProfitTransferFSM & ADR-27 invariants."""

    def test_no_fetch_balance_method_or_calls_anywhere(self):
        """ADR-27: fetch_balance полностью выпилен из боевых клиентов и сервисов автоперевода (AST-проверка)."""
        self.assertFalse(hasattr(ProdBinanceClient, "fetch_balance"), "ProdBinanceClient must not expose fetch_balance")
        self.assertFalse(hasattr(PaperBinanceClient, "fetch_balance"), "PaperBinanceClient must not expose fetch_balance")

        forbidden = {"fetch_balance", "totalWalletBalance", "availableBalance"}
        for mod in (ProfitTransferFSM, ProfitTransferService, DomeCloserExecutor, AutoCloser):
            src = inspect.getsource(mod)
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in forbidden:
                    self.fail(f"Forbidden attribute {node.attr} found in {mod.__name__}")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in forbidden:
                    self.fail(f"Forbidden call {node.func.id} found in {mod.__name__}")

    def test_manual_deposit_on_futures_ignored_completely(self):
        """Ручное пополнение +1000$ или +50000$ на кошелек биржи не вызывает ни закрытия, ни ложного перевода."""
        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp)
            sdir = tpath / "STATS"
            strat_id = "u96_omni_zenith13__trailing_ratchet"
            strat_cfg = _make_strat_cfg(strat_id=strat_id)
            client = MagicMock()
            client.wallet_balance = 51435.0  # фиктивный огромный баланс на кошельке
            client.balance = 51435.0
            client.transfer_futures_to_spot = AsyncMock()
            bot = _make_bot_core(client, sim_time=1500.0, stats_dir=sdir, cache_dir=tpath)

            # Но в аналитике сделки в сумме в убытке -10$ относительно уже переведенных 20$
            _write_analytics_file(sdir, strat_id, net_profit=-10.0, iter_idx=2, transferred_to_spot=20.0)
            bot.analytics.last_deep_sync_ts = 1005.0

            fsm = ProfitTransferFSM(stats_dir=sdir, cache_dir=tpath)
            fsm.arm_on_ratchet_trigger(strat_id, strat_cfg, "TRAILING RATCHET", 1, 2, prev_baseline_net=-35.0, ready_at_ts=1300.0)
            fsm.mark_positions_closed(strat_id, now_ts=1000.0, ready_at_ts=1300.0)

            asyncio.run(fsm.process_tick(bot, {strat_id: strat_cfg}))
            asyncio.run(fsm.wait_for_pending_tasks())
            client.transfer_futures_to_spot.assert_not_awaited()

    def test_close_strategy_recheck_closes_hanging_positions(self):
        """EmergencyCloserService.close_strategy должен через 0.3с дозакрыть зависшую на бирже позицию."""
        from CORE.SERVICES.emergency_closer import EmergencyCloserService

        strat_id = "u96_omni_zenith13__trailing_ratchet"
        strat_cfg = _make_strat_cfg(strat_id=strat_id, symbols=["ERAUSDT"])
        mock_client = MagicMock()
        # Первый fetch_positions видит 10, ре-чек видит остаток 2, третий 0
        mock_client.fetch_positions = AsyncMock(
            side_effect=[
                [{"symbol": "ERAUSDT", "positionSide": "LONG", "positionAmt": "10.0"}],
                [{"symbol": "ERAUSDT", "positionSide": "LONG", "positionAmt": "2.0"}],
            ]
        )
        mock_client.make_order = AsyncMock(return_value=APIResponse(success=True, data={}))
        mock_client.cancel_all_orders = AsyncMock(return_value=APIResponse(success=True, data={}))

        bot = MagicMock()
        bot.strategy_registry = MagicMock()
        bot.strategy_registry.active_strategies = {strat_id: strat_cfg}
        bot.strategy_registry.get_all_active_symbols = MagicMock(return_value=["ERAUSDT"])
        bot.get_strategy_heartbeat_seconds = MagicMock(return_value=300)
        bot.get_client_for_strategy = MagicMock(return_value=mock_client)
        bot.calc_heartbeat_window_cooldown = MagicMock(return_value=1205.0)
        bot.strategy_blocked_until = {}
        bot.symbol_blocked_until = {}
        bot.block_until_timestamp = 0.0
        bot.entry_blocked = False
        bot._close_lock = asyncio.Lock()
        bot._closing_strategies = set()
        bot._closing_symbols = set()
        bot._get_symbol_lock = MagicMock(return_value=asyncio.Lock())
        bot.fsm_states = {}
        bot.runtime_manager = MagicMock()

        asyncio.run(EmergencyCloserService.close_strategy(bot, strat_id))
        self.assertEqual(mock_client.fetch_positions.await_count, 2)
        self.assertEqual(mock_client.make_order.await_count, 2)
        qtys = [c.kwargs["qty"] for c in mock_client.make_order.call_args_list]
        self.assertEqual(qtys, [10.0, 2.0])

    def test_stale_flags_block_transition_matrix(self):
        """Матрица анти-лаг проверок: ни один сбойный или несинхронизированный флаг не пропускает перевод."""
        bad_scenarios = [
            ("fsm_restored_frozen", {"fsm_state": AnalyticsState.RESTORED_FROZEN}),
            ("fsm_awaiting_api", {"fsm_state": AnalyticsState.AWAITING_API}),
            ("fsm_live_synced", {"fsm_state": AnalyticsState.LIVE_SYNCED}),
            ("floating_unsynced", {"floating_synced": False}),
            ("iter_not_incremented", {"iter_idx": 1}),
            ("residual_long_open", {"per_coin": {"ERAUSDT": {"long_amt": 12.0, "short_amt": 0.0}}}),
            ("residual_short_open", {"per_coin": {"ERAUSDT": {"long_amt": 0.0, "short_amt": -5.5}}}),
            ("residual_negative_long", {"per_coin": {"ZECUSDT": {"long_amt": -0.01, "short_amt": 0.0}}}),
            ("nonzero_unrealized_pos", {"unrealized": 1.25}),
            ("nonzero_unrealized_neg", {"unrealized": -0.75}),
            ("tiny_unrealized_leak", {"unrealized": 0.02}),
            ("pending_sync_lock_era", {"sync_locks": {("ERAUSDT", "LONG")}}),
            ("pending_sync_lock_zec", {"sync_locks": {("ZECUSDT", "SHORT")}}),
            ("deep_sync_older_than_close", {"last_deep_sync_ts": 999.0}),
            ("deep_sync_equal_zero", {"last_deep_sync_ts": 0.0}),
            ("heartbeat_not_reached", {"sim_time": 1200.0, "ready_at": 1205.0}),
        ]

        for label, params in bad_scenarios:
            with self.subTest(scenario=label), tempfile.TemporaryDirectory() as tmp:
                tpath = Path(tmp)
                sdir = tpath / "STATS"
                strat_id = "u96_omni_zenith13__trailing_ratchet"
                strat_cfg = _make_strat_cfg(strat_id=strat_id)
                client = MagicMock()
                client.transfer_futures_to_spot = AsyncMock(return_value=APIResponse(success=True, data={"amount": 25.0}))

                sim_t = params.get("sim_time", 1500.0)
                bot = _make_bot_core(client, sim_time=sim_t, stats_dir=sdir, cache_dir=tpath)
                bot.analytics.fsm.state = params.get("fsm_state", AnalyticsState.AUTOCLOSER_READY)
                bot.analytics._sync_locks = params.get("sync_locks", set())
                bot.analytics.last_deep_sync_ts = params.get("last_deep_sync_ts", 1005.0)

                _write_analytics_file(
                    sdir,
                    strat_id,
                    net_profit=25.0,
                    iter_idx=params.get("iter_idx", 2),
                    floating_synced=params.get("floating_synced", True),
                    unrealized=params.get("unrealized", 0.0),
                    per_coin=params.get("per_coin"),
                )

                fsm = ProfitTransferFSM(stats_dir=sdir, cache_dir=tpath, sync_debounce_sec=10.0)
                ready_at = params.get("ready_at", 1300.0)
                armed = fsm.arm_on_ratchet_trigger(
                    strat_id=strat_id,
                    strat_cfg=strat_cfg,
                    close_type="TRAILING RATCHET",
                    closed_iter_idx=1,
                    next_iter_idx=2,
                    prev_baseline_net=0.0,
                    ready_at_ts=ready_at,
                )
                self.assertTrue(armed)
                fsm.mark_positions_closed(strat_id, now_ts=1000.0, ready_at_ts=ready_at)

                async def _run():
                    await fsm.process_tick(bot, {strat_id: strat_cfg})
                    await fsm.wait_for_pending_tasks()

                asyncio.run(_run())
                client.transfer_futures_to_spot.assert_not_awaited()

    def test_sync_in_progress_lock_blocks_settlement(self):
        """Активный глубокий синк _sync_in_progress обязан тормозить перевод до освобождения лока."""
        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp)
            sdir = tpath / "STATS"
            strat_id = "u96_omni_zenith13__trailing_ratchet"
            strat_cfg = _make_strat_cfg(strat_id=strat_id)
            client = MagicMock()
            client.transfer_futures_to_spot = AsyncMock(return_value=APIResponse(success=True, data={"amount": 20.0}))

            bot = _make_bot_core(client, sim_time=1500.0, stats_dir=sdir, cache_dir=tpath)
            bot.analytics.last_deep_sync_ts = 1005.0
            _write_analytics_file(sdir, strat_id, net_profit=20.0, iter_idx=2)

            fsm = ProfitTransferFSM(stats_dir=sdir, cache_dir=tpath)
            fsm.arm_on_ratchet_trigger(strat_id, strat_cfg, "TRAILING RATCHET", 1, 2, 0.0, ready_at_ts=1300.0)
            fsm.mark_positions_closed(strat_id, now_ts=1000.0, ready_at_ts=1300.0)

            async def _run():
                async with bot.analytics._sync_in_progress:
                    await fsm.process_tick(bot, {strat_id: strat_cfg})
                    self.assertEqual(fsm.get_state(strat_id), ProfitTransferState.AWAITING_SETTLED_ANALYTICS)
                await fsm.process_tick(bot, {strat_id: strat_cfg})
                await fsm.wait_for_pending_tasks()

            asyncio.run(_run())
            client.transfer_futures_to_spot.assert_awaited_once_with(amount=20.0, asset="USDT")

    def test_unrelated_sync_lock_does_not_block_dome(self):
        """Лок чужой монеты другого купола не должен тормозить перевод текущего купола."""
        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp)
            sdir = tpath / "STATS"
            strat_id = "u96_omni_zenith13__trailing_ratchet"
            strat_cfg = _make_strat_cfg(strat_id=strat_id, symbols=["ERAUSDT"])
            client = MagicMock()
            client.transfer_futures_to_spot = AsyncMock(return_value=APIResponse(success=True, data={"amount": 18.5}))

            bot = _make_bot_core(client, sim_time=1500.0, stats_dir=sdir, cache_dir=tpath)
            bot.analytics._sync_locks = {("BTCUSDT", "LONG")}
            bot.analytics.last_deep_sync_ts = 1005.0
            _write_analytics_file(
                sdir, strat_id, net_profit=18.5, iter_idx=2, per_coin={"ERAUSDT": {"long_amt": 0.0, "short_amt": 0.0}}
            )

            fsm = ProfitTransferFSM(stats_dir=sdir, cache_dir=tpath)
            fsm.arm_on_ratchet_trigger(strat_id, strat_cfg, "TRAILING RATCHET", 1, 2, 0.0, ready_at_ts=1300.0)
            fsm.mark_positions_closed(strat_id, now_ts=1000.0, ready_at_ts=1300.0)

            asyncio.run(fsm.process_tick(bot, {strat_id: strat_cfg}))
            asyncio.run(fsm.wait_for_pending_tasks())
            client.transfer_futures_to_spot.assert_awaited_once_with(amount=18.5, asset="USDT")

    def test_eligibility_and_pct_rounding_matrix(self):
        """Матрица 60+ комбинаций: 2 главных условия (профит итерации + маржа > старта), pct, min_guard, округление."""
        pcts = [0.0, 25.0, 50.0, 75.0, 100.0]
        # (iter_profit, net_profit, transferred_total, min_guard, pct -> expected_amount or 0.0)
        cases = [
            (26.459, 26.459, 0.0, 5.0),     # стандартная итерация в плюс от нуля
            (26.459, 60.0, 20.0, 5.0),      # маржа выше старта с запасом (+40), переводим от iter_profit
            (26.0, 40.0, 26.0, 5.0),        # овер (+14) < iter_profit (+26) после частичного восстановления -> режем до 14
            (26.0, 10.0, 20.0, 5.0),        # маржа НИЖЕ старта (net 10 < transferred 20 -> овер -10) -> БЛОК!
            (26.0, 20.0, 20.0, 5.0),        # маржа РОВНО на старте (овер 0) -> БЛОК!
            (4.99, 50.0, 0.0, 5.0),         # проскальзывание съело профит итерации ниже min_guard 5$ -> БЛОК!
            (5.00, 50.0, 0.0, 5.0),         # ровно на пороге min_guard 5$
            (-3.20, 50.0, 0.0, 5.0),        # итерация закрылась в убыток при наличии старого овера -> БЛОК!
            (0.0, 50.0, 0.0, 5.0),          # нулевой профит (ручной сброс в ноль) -> БЛОК!
            (0.80, 0.80, 0.0, 1.0),         # ниже min_transfer_usdt=1.0 -> БЛОК!
            (1.99, 1.99, 0.0, 1.0),         # 50% от 1.99 = 0.99 (<1.0) -> БЛОК при 25/50%, OK при 75/100%
        ]

        for iter_p, net_p, trans_tot, min_g in cases:
            for pct in pcts:
                with self.subTest(iter_p=iter_p, net_p=net_p, trans_tot=trans_tot, pct=pct):
                    cfg = {
                        "enabled": True,
                        "transfer_profit_pct": pct,
                        "min_transfer_usdt": 1.0,
                        "asset": "USDT",
                    }
                    eligible, reason = ProfitTransferService.resolve_eligible_profit_usdt(
                        actual_iter_profit_usdt=iter_p,
                        net_profit_usdt=net_p,
                        transferred_to_spot_usdt=trans_tot,
                        min_profit_guard_usdt=max(1.0, min_g),
                    )
                    amt = ProfitTransferService.calculate_transfer_amount(eligible, cfg)

                    # Инвариант 1: при pct == 0 или iter_p < guard или (net_p - trans_tot <= 0) перевод всегда 0
                    if pct <= 0.0 or iter_p < max(1.0, min_g) or (net_p - trans_tot) <= 0.0:
                        self.assertEqual(amt, 0.0)
                    else:
                        # Инвариант 2: переведенная сумма никогда не превышает ни iter_p, ни овер над стартом
                        self.assertLessEqual(amt, iter_p + 1e-9)
                        self.assertLessEqual(amt, (net_p - trans_tot) + 1e-9)
                        if amt > 0:
                            self.assertGreaterEqual(amt, 1.0)
                            # Проверка точности до 2 знаков вниз
                            self.assertAlmostEqual(round(amt, 2), amt, places=6)

    def test_concurrency_strict_single_execution_stress(self):
        """Стресс-тест гонок: 50 параллельных тиков при медленном HTTP API дают СТРОГО 1 перевод на итерацию."""
        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp)
            sdir = tpath / "STATS"
            strat_id = "u96_omni_zenith13__trailing_ratchet"
            strat_cfg = _make_strat_cfg(strat_id=strat_id)

            async def _slow_transfer(amount: float, asset: str = "USDT"):
                await asyncio.sleep(0.05)
                return APIResponse(success=True, data={"tranId": 999, "amount": amount})

            client = MagicMock()
            client.transfer_futures_to_spot = AsyncMock(side_effect=_slow_transfer)
            bot = _make_bot_core(client, sim_time=1500.0, stats_dir=sdir, cache_dir=tpath)
            bot.analytics.last_deep_sync_ts = 1005.0
            _write_analytics_file(sdir, strat_id, net_profit=30.0, iter_idx=5)

            fsm = ProfitTransferFSM(stats_dir=sdir, cache_dir=tpath)
            fsm.arm_on_ratchet_trigger(strat_id, strat_cfg, "TRAILING RATCHET", 4, 5, 0.0, ready_at_ts=1300.0)
            fsm.mark_positions_closed(strat_id, now_ts=1000.0, ready_at_ts=1300.0)

            async def _run():
                # 50 параллельных конкурентных вызовов в одном тике старта новой свечи
                await asyncio.gather(*(fsm.process_tick(bot, {strat_id: strat_cfg}) for _ in range(50)))
                await fsm.wait_for_pending_tasks()
                # Повторный вызов для той же итерации #4 после завершения
                rearm = fsm.arm_on_ratchet_trigger(strat_id, strat_cfg, "TRAILING RATCHET", 4, 5, 0.0, ready_at_ts=1300.0)
                self.assertFalse(rearm)

            asyncio.run(_run())
            client.transfer_futures_to_spot.assert_awaited_once_with(amount=30.0, asset="USDT")
            self.assertTrue(fsm.is_claimed(strat_id, 4))
            self.assertEqual(fsm.get_state(strat_id), ProfitTransferState.IDLE)

    def test_slippage_reanchor_and_margin_recovery_across_iterations(self):
        """
        Мульти-итерационный сценарий:
        1) Iter #1: до закрытия 26.40$, проскальзывание снизило итог до 24.15$ -> перевелось ровно 24.15$, якорь поправлен;
        2) Внешнее ручное пополнение +1000$ на биржу -> полностью игнорируется;
        3) Iter #2: убыток -40$ (перевода нет, transferred остается 24.15, net становится -15.85);
        4) Iter #3: храповик сработал +18$ (net стал +2.15), но маржа 1435 + (2.15 - 24.15) = 1413 < 1435 -> БЛОК!
        5) Iter #4: храповик сработал +30$ (net стал +32.15, овер над стартом +8.00$) -> переводится строго 8.00$!
        """
        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp)
            sdir = tpath / "STATS"
            strat_id = "u96_omni_zenith13__trailing_ratchet"
            strat_cfg = _make_strat_cfg(strat_id=strat_id, min_guaranteed=5.0)
            client = MagicMock()
            client.transfer_futures_to_spot = AsyncMock(side_effect=lambda amount, asset="USDT": APIResponse(success=True, data={"amount": amount}))

            bot = _make_bot_core(client, sim_time=1000.0, stats_dir=sdir, cache_dir=tpath)
            fsm = ProfitTransferFSM(stats_dir=sdir, cache_dir=tpath, sync_debounce_sec=10.0)

            async def _run_cycle(closed_i: int, next_i: int, prev_base: float, pre_net: float, settled_net: float, close_t: float):
                bot.current_sim_time = close_t
                fsm.arm_on_ratchet_trigger(strat_id, strat_cfg, "TRAILING RATCHET", closed_i, next_i, prev_base, ready_at_ts=close_t + 60.0)
                # Пишем предварительный кэш + историю как при реальном DomeCloserExecutor
                bot.analytics.fsm.mark_iteration_closed(strat_id, closed_i, {"strat_net": pre_net, "iter_net": pre_net - prev_base})
                fsm.mark_positions_closed(strat_id, now_ts=close_t, ready_at_ts=close_t + 60.0)
                # Приходит реальный deep sync со settled_net
                _write_analytics_file(sdir, strat_id, net_profit=settled_net, iter_idx=next_i, transferred_to_spot=fsm._get_transferred_total(strat_id))
                bot.analytics.last_deep_sync_ts = close_t + 12.0
                bot.current_sim_time = close_t + 65.0
                await fsm.process_tick(bot, {strat_id: strat_cfg})
                await fsm.wait_for_pending_tasks()

            # Iter #1: pre 26.40 -> settled 24.15
            asyncio.run(_run_cycle(1, 2, 0.0, 26.40, 24.15, 1000.0))
            self.assertAlmostEqual(fsm._get_transferred_total(strat_id), 24.15)
            self.assertAlmostEqual(bot.analytics.fsm.get_iteration_baseline_net(strat_id, 2), 24.15)

            # Iter #2: убыток -40$ -> овера нет -> не переводим
            asyncio.run(_run_cycle(2, 3, 24.15, -15.85, -15.85, 2000.0))
            self.assertAlmostEqual(fsm._get_transferred_total(strat_id), 24.15)

            # Iter #3: храповик +18$, но net_profit = +2.15 < transferred 24.15 -> маржа еще в минусе -> БЛОК!
            asyncio.run(_run_cycle(3, 4, -15.85, 2.15, 2.15, 3000.0))
            self.assertAlmostEqual(fsm._get_transferred_total(strat_id), 24.15)

            # Iter #4: храповик +30$, net_profit = 32.15 -> овер = 32.15 - 24.15 = 8.00$ -> переводится ровно 8.00$!
            asyncio.run(_run_cycle(4, 5, 2.15, 32.15, 32.15, 4000.0))
            self.assertAlmostEqual(fsm._get_transferred_total(strat_id), 32.15)
            self.assertEqual(client.transfer_futures_to_spot.await_count, 2)
            calls = [c.kwargs["amount"] for c in client.transfer_futures_to_spot.call_args_list]
            self.assertEqual(calls, [24.15, 8.0])

    def test_non_ratchet_and_manual_closes_never_arm_or_transfer(self):
        """Стопы, _off, ручной Close All и ротация НЕ триггерят Spot-перевод; все позитивные автоклозеры триггерят."""
        disallowed_types = [
            "NEGATIVE STOP",
            "BASKET EQUITY CUTOFF",
            "EMERGENCY STOP",
            "OFF",
            "u96_OFF",
            "MANUAL CLOSE",
            "ROTATION",
        ]
        allowed_positive_types = [
            "TRAILING RATCHET",
            "MA_CROSSOVER",
            "SMOOTHED_DRAWDOWN",
            "RATCHET_SMOOTHED_DRAWDOWN",
            "REGIME_ADAPTIVE",
            "FIXED NET PROFIT",
            "TOXICITY ADAPTIVE",
            "STAGNATION RATCHET",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp)
            sdir = tpath / "STATS"
            strat_id = "u96_omni_zenith13__trailing_ratchet"
            strat_cfg = _make_strat_cfg(strat_id=strat_id)

            for idx, ctype in enumerate(disallowed_types, start=1):
                with self.subTest(disallowed_close_type=ctype):
                    fsm = ProfitTransferFSM(stats_dir=sdir, cache_dir=tpath / f"dis_{idx}")
                    armed = fsm.arm_on_ratchet_trigger(strat_id, strat_cfg, ctype, idx, idx + 1, 0.0, ready_at_ts=100.0)
                    self.assertFalse(armed)
                    self.assertEqual(fsm.get_state(strat_id), ProfitTransferState.IDLE)

            for idx, ctype in enumerate(allowed_positive_types, start=1):
                with self.subTest(allowed_close_type=ctype):
                    fsm = ProfitTransferFSM(stats_dir=sdir, cache_dir=tpath / f"ok_{idx}")
                    armed = fsm.arm_on_ratchet_trigger(strat_id, strat_cfg, ctype, idx, idx + 1, 0.0, ready_at_ts=100.0)
                    self.assertTrue(armed, f"{ctype} must be eligible for Spot transfer when enabled")
                    self.assertEqual(fsm.get_state(strat_id), ProfitTransferState.CLOSING_POSITIONS)

    def test_per_dome_isolation_and_multi_client_routing(self):
        """
        Пер-купольная изоляция: два купола на разных Binance-аккаунтах (client_A, client_B).
        deep_sync первого купола не разблокирует второй; каждый шлет профит строго через свой клиент.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp)
            sdir = tpath / "STATS"
            strat_a = "dome_alpha__trailing_ratchet"
            strat_b = "dome_beta__trailing_ratchet"
            cfg_a = _make_strat_cfg(strat_id=strat_a, name="Alpha Dome", symbols=["ERAUSDT"], pct=100.0)
            cfg_b = _make_strat_cfg(strat_id=strat_b, name="Beta Dome", symbols=["ZECUSDT"], pct=50.0)

            client_a = MagicMock()
            client_a.transfer_futures_to_spot = AsyncMock(return_value=APIResponse(success=True, data={"amount": 25.0, "tranId": 101}))
            client_b = MagicMock()
            client_b.transfer_futures_to_spot = AsyncMock(return_value=APIResponse(success=True, data={"amount": 20.0, "tranId": 202}))
            default_client = MagicMock()
            default_client.transfer_futures_to_spot = AsyncMock()

            bot = _make_bot_core(default_client, sim_time=1500.0, stats_dir=sdir, cache_dir=tpath)
            bot.clients = {strat_a: client_a, strat_b: client_b}

            _write_analytics_file(sdir, strat_a, net_profit=25.0, iter_idx=2, per_coin={"ERAUSDT": {"long_amt": 0.0, "short_amt": 0.0}})
            _write_analytics_file(sdir, strat_b, net_profit=40.0, iter_idx=2, per_coin={"ZECUSDT": {"long_amt": 0.0, "short_amt": 0.0}})

            fsm = ProfitTransferFSM(stats_dir=sdir, cache_dir=tpath)
            fsm.arm_on_ratchet_trigger(strat_a, cfg_a, "TRAILING RATCHET", 1, 2, 0.0, ready_at_ts=1200.0)
            fsm.arm_on_ratchet_trigger(strat_b, cfg_b, "TRAILING RATCHET", 1, 2, 0.0, ready_at_ts=1200.0)
            fsm.mark_positions_closed(strat_a, now_ts=1000.0, ready_at_ts=1200.0)
            fsm.mark_positions_closed(strat_b, now_ts=1000.0, ready_at_ts=1200.0)

            # Прошел deep_sync ТОЛЬКО для client_A
            bot.analytics.last_deep_sync_ts = 1010.0
            bot.analytics.last_deep_sync_ts_by_client[id(client_a)] = 1010.0

            async def _step1():
                await fsm.process_tick(bot, {strat_a: cfg_a, strat_b: cfg_b})
                await fsm.wait_for_pending_tasks()

            asyncio.run(_step1())
            client_a.transfer_futures_to_spot.assert_awaited_once_with(amount=25.0, asset="USDT")
            client_b.transfer_futures_to_spot.assert_not_awaited()
            default_client.transfer_futures_to_spot.assert_not_awaited()
            self.assertEqual(fsm.get_state(strat_b), ProfitTransferState.AWAITING_SETTLED_ANALYTICS)

            # Теперь прошел deep_sync для client_B
            bot.analytics.last_deep_sync_ts_by_client[id(client_b)] = 1020.0
            asyncio.run(_step1())
            client_b.transfer_futures_to_spot.assert_awaited_once_with(amount=20.0, asset="USDT")
            default_client.transfer_futures_to_spot.assert_not_awaited()

    def test_forced_deep_sync_after_debounce_and_api_error_resilience(self):
        """Проверяет принудительный deep_sync через debounce и устойчивость при ошибке SAPI API."""
        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp)
            sdir = tpath / "STATS"
            strat_id = "u96_omni_zenith13__trailing_ratchet"
            strat_cfg = _make_strat_cfg(strat_id=strat_id)

            client = MagicMock()
            client.transfer_futures_to_spot = AsyncMock(
                return_value=APIResponse(success=False, error_code=-3005, error_msg="API error test")
            )
            bot = _make_bot_core(client, sim_time=1015.0, stats_dir=sdir, cache_dir=tpath)
            _write_analytics_file(sdir, strat_id, net_profit=22.0, iter_idx=2)

            async def _do_deep_sync(cl, _prices):
                bot.analytics.last_deep_sync_ts = 1015.0

            bot.analytics.deep_sync_analytics = AsyncMock(side_effect=_do_deep_sync)
            fsm = ProfitTransferFSM(stats_dir=sdir, cache_dir=tpath, sync_debounce_sec=10.0)
            fsm.arm_on_ratchet_trigger(strat_id, strat_cfg, "TRAILING RATCHET", 1, 2, 0.0, ready_at_ts=1030.0)
            fsm.mark_positions_closed(strat_id, now_ts=1000.0, ready_at_ts=1030.0)

            async def _run():
                # Т=1015 (15s > 10s debounce) -> вызывает принудительный deep_sync
                await fsm.process_tick(bot, {strat_id: strat_cfg})
                bot.analytics.deep_sync_analytics.assert_awaited_once()
                # Т=1016 -> settled & ждет ready_at_ts=1030
                bot.current_sim_time = 1016.0
                await fsm.process_tick(bot, {strat_id: strat_cfg})
                self.assertEqual(fsm.get_state(strat_id), ProfitTransferState.AWAITING_HEARTBEAT_START)
                # Т=1030 -> запускает перевод, который падает с ошибкой API
                bot.current_sim_time = 1030.0
                await fsm.process_tick(bot, {strat_id: strat_cfg})
                await fsm.wait_for_pending_tasks()

            asyncio.run(_run())
            client.transfer_futures_to_spot.assert_awaited_once()
            # При ошибке API transferred_to_spot не накручивается
            self.assertAlmostEqual(fsm._get_transferred_total(strat_id), 0.0)
            # Но итерация отмечена обработанной, зависания нет
            self.assertEqual(fsm.get_state(strat_id), ProfitTransferState.IDLE)

    def test_autocloser_addon_tick_and_untouched_ratchet_behavior(self):
        """Проверяет интеграцию AutoCloser + ProfitTransferFSM как чистой надстройки."""
        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp)
            sdir = tpath / "STATS"
            strat_id = "u96_omni_zenith13__trailing_ratchet"
            strat_cfg = _make_strat_cfg(strat_id=strat_id)
            client = MagicMock()
            client.transfer_futures_to_spot = AsyncMock(return_value=APIResponse(success=True, data={"amount": 16.0}))
            bot = _make_bot_core(client, sim_time=1500.0, stats_dir=sdir, cache_dir=tpath)
            bot.strategy_registry = MagicMock()
            bot.strategy_registry.active_strategies = {strat_id: strat_cfg}

            with patch("CORE.SERVICES.auto_closer.ANALYTICS_STATS_DIR", sdir), patch("CORE.SERVICES.auto_closer.CACHE_DIR", tpath):
                ac = AutoCloser(bot)
                self.assertIsInstance(ac.profit_transfer_fsm, ProfitTransferFSM)
                bot.auto_closer = ac

                _write_analytics_file(sdir, strat_id, net_profit=16.0, iter_idx=2)
                ac.profit_transfer_fsm.arm_on_ratchet_trigger(strat_id, strat_cfg, "TRAILING RATCHET", 1, 2, 0.0, ready_at_ts=1300.0)
                ac.profit_transfer_fsm.mark_positions_closed(strat_id, now_ts=1000.0, ready_at_ts=1300.0)
                bot.analytics.last_deep_sync_ts = 1005.0

                with patch("CORE.SERVICES.auto_closer.DATA_DIR", tpath), patch("CORE.SERVICES.auto_closer.GLOBAL_ANALYTICS_JSON", sdir / "analytics.json"):
                    (tpath / "app.json").write_text("{}", encoding="utf-8")
                    (sdir / "analytics.json").write_text("{}", encoding="utf-8")
                    asyncio.run(ac.check())
                    asyncio.run(ac.profit_transfer_fsm.wait_for_pending_tasks())

            client.transfer_futures_to_spot.assert_awaited_once_with(amount=16.0, asset="USDT")


if __name__ == "__main__":
    unittest.main()
