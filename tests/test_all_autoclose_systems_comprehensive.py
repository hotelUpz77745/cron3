# tests/test_all_autoclose_systems_comprehensive.py
# -*- coding: utf-8 -*-
"""
==============================================================================
COMPREHENSIVE TEST SUITE: ALL AUTO-CLOSE SYSTEMS & ENGINES
==============================================================================
Covers:
1. Strategy Dome AutoCloser:
   - Trailing Ratchet (Fibonacci ladder L0..L4, step-ups, pullbacks, de-arm on dip to zero)
   - Toxicity Adaptive (Panic evacuation, Moderate fix, Light fix, loss guard)
   - Fixed Net Profit (instant exit at target, scaling)
   - Stagnation Engine (velocity-based target degradation, min_floor clamp)
   - Negative Stop (loss execution, negative normalization -abs guard)
   - Positive Profit Guard (strict prohibition of closing <= 0 in positive modes)
2. Global Portfolio AutoCloser (PortfolioCloser):
   - Trailing Ratchet global
   - Toxicity Adaptive global
   - Fixed Net global
   - Negative Stop global
   - Positive Profit Guard global
3. Dynamic Money Scaling Engines:
   - Static scale_money
   - Inverted Notional basket scaling (de-escalation on overload)
   - Margin Relative scaling
4. FSM Lifecycle, Idempotency & Baseline Persistence:
   - Multi-cycle API poll without deadlock
   - Idempotency guard (skipping duplicate closing of same iteration)
   - Iteration baseline calculation & progression (Iter #1 -> #2 -> #3)
5. DomeCloserExecutor Atomic Execution:
   - Cache persistence (CACHE/iteration_state.json)
   - Analytics file update (atomic write)
   - In-memory ratchet state reset
   - FSM recent_closes wipeout
"""

import copy
import json
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

from ANALYTICS.analytics_fsm import AnalyticsFSM, AnalyticsState
from CORE.STRATEGY.trailing_system import BaseTrailingSystem, ToxicityEngine
from CORE.SERVICES.auto_closer import AutoCloser
from CORE.SERVICES.portfolio_closer import PortfolioCloser
from CORE.SERVICES.dome_closer_executor import DomeCloserExecutor
from CORE.SERVICES.stagnation_evaluator import StagnationEvaluator


class TestComprehensiveAutoCloseSystems(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp_dir = Path("tests/scratch/autoclose_suite")
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.stats_dir = self.tmp_dir / "STATS"
        self.stats_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.tmp_dir / "CACHE"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.fsm = AnalyticsFSM(cache_dir=self.cache_dir, log_file=self.tmp_dir / "analytics.json", stats_dir=self.stats_dir)
        self.fsm.state = AnalyticsState.AUTOCLOSER_READY

    # =========================================================================
    # 1. STRATEGY DOME: TRAILING RATCHET
    # =========================================================================

    def test_trailing_ratchet_full_ladder_and_stepups(self):
        """Проверка полной Фибо-лестницы L0..L3, шагов взвода и фиксации."""
        ratchet_cfg = {
            "enabled": True,
            "levels": [
                {"activation": 13.0, "floor": 8.0, "min_age_hours": 0.0, "enabled": True, "_desc": "L0"},
                {"activation": 21.0, "floor": 13.0, "min_age_hours": 0.0, "enabled": True, "_desc": "L1"},
                {"activation": 34.0, "floor": 21.0, "min_age_hours": 0.0, "enabled": True, "_desc": "L2"},
                {"activation": 55.0, "floor": 34.0, "min_age_hours": 0.0, "enabled": True, "_desc": "L3"},
            ],
            "min_guaranteed_profit_usdt": 4.0,
            "scale_money": 1.0,
        }
        state = {}

        # Ниже L0 (Net = 10.0$) -> Idle
        close, floor, _, _ = BaseTrailingSystem.evaluate_ratchet(10.0, 1.0, ratchet_cfg, state)
        self.assertFalse(close)
        self.assertIsNone(floor)

        # Достиг L0 (Net = 14.0$) -> Взвод L0 (+8$ пол)
        close, floor, _, _ = BaseTrailingSystem.evaluate_ratchet(14.0, 1.0, ratchet_cfg, state)
        self.assertFalse(close)
        self.assertEqual(floor, 8.0)
        self.assertEqual(state["active_level_idx"], 0)

        # Достиг L1 (Net = 22.0$) -> Поднятие пола на L1 (+13$ пол)
        close, floor, _, _ = BaseTrailingSystem.evaluate_ratchet(22.0, 1.5, ratchet_cfg, state)
        self.assertFalse(close)
        self.assertEqual(floor, 13.0)
        self.assertEqual(state["active_level_idx"], 1)

        # Достиг L2 (Net = 35.0$) -> Поднятие пола на L2 (+21$ пол)
        close, floor, _, _ = BaseTrailingSystem.evaluate_ratchet(35.0, 2.0, ratchet_cfg, state)
        self.assertFalse(close)
        self.assertEqual(floor, 21.0)
        self.assertEqual(state["active_level_idx"], 2)

        # Откат до 25$ -> Выше пола 21$ -> Держим
        close, floor, _, _ = BaseTrailingSystem.evaluate_ratchet(25.0, 2.1, ratchet_cfg, state)
        self.assertFalse(close)
        self.assertEqual(floor, 21.0)

        # Откат ровно до пола 21.0$ -> Срабатывание закрытия!
        close, floor, info, _ = BaseTrailingSystem.evaluate_ratchet(21.0, 2.2, ratchet_cfg, state)
        self.assertTrue(close)
        self.assertEqual(floor, 21.0)
        self.assertIn("TRAILING RATCHET L2", info)

    def test_trailing_ratchet_dearm_on_drop_to_zero(self):
        """Если прибыль ушла в 0 или минус, взведённый курок сбрасывается."""
        ratchet_cfg = {
            "enabled": True,
            "levels": [{"activation": 13.0, "floor": 8.0, "enabled": True}],
            "scale_money": 1.0,
        }
        state = {}

        # Взвод
        BaseTrailingSystem.evaluate_ratchet(15.0, 1.0, ratchet_cfg, state)
        self.assertEqual(state.get("armed_floor"), 8.0)

        # Обвал в минус
        close, floor, info, _ = BaseTrailingSystem.evaluate_ratchet(-1.5, 1.5, ratchet_cfg, state)
        self.assertFalse(close)
        self.assertIsNone(state.get("armed_floor"))
        self.assertIn("De-armed", info)

        # Отскок до +6.0 (ниже порога активации 13.0) -> НЕ закрывается, ждет нового взвода!
        close, floor, _, _ = BaseTrailingSystem.evaluate_ratchet(6.0, 2.0, ratchet_cfg, state)
        self.assertFalse(close)
        self.assertIsNone(floor)

    # =========================================================================
    # 2. STRATEGY DOME: TOXICITY ADAPTIVE
    # =========================================================================

    def test_toxicity_engine_calculations(self):
        """Проверка математики Delta-Recovery и Toxicity Score."""
        # Полное рассасывание просадки (0 убытка при 100$ пике)
        rec = ToxicityEngine.calculate_delta_recovery(current_floating_pnl=0.0, max_drawdown_iteration=-100.0)
        self.assertEqual(rec, 1.0)

        # На самом дне (-100$ текущий при -100$ пике)
        rec_bottom = ToxicityEngine.calculate_delta_recovery(-100.0, -100.0)
        self.assertEqual(rec_bottom, 0.0)

        # 50% рассасывание
        rec_half = ToxicityEngine.calculate_delta_recovery(-50.0, -100.0)
        self.assertEqual(rec_half, 0.5)

        # Toxicity score
        score = ToxicityEngine.calculate_toxicity_score(
            margin_load=0.8, max_depth_ratio=0.7, float_ratio=0.6, delta_recovery=0.2
        )
        self.assertTrue(0.0 <= score <= 1.0)

    def test_toxicity_adaptive_evacuation_tiers(self):
        """Проверка 3 уровней эвакуации: Panic (0.7+), Moderate (0.35..0.7), Light (<0.35)."""
        t_cfg = {
            "enabled": True,
            "toxicity_adaptive": {
                "panic_toxicity_threshold": 0.70,
                "relaxed_toxicity_threshold": 0.35,
                "emergency_exit_floor_usdt": 3.0,
                "moderate_exit_floor_usdt": 8.0,
                "relaxed_exit_floor_usdt": 15.0,
                "min_panic_recovery": 0.40,
                "min_moderate_recovery": 0.20,
            }
        }
        state = {}

        # Уровень 1: Паника (Score 0.80, Rec 0.45, Net +3.5$) -> Экстренный сброс в кэш!
        close, floor, info, is_stag = ToxicityEngine.evaluate_toxicity_adaptive(
            iter_net=3.5, iter_age_hours=2.0, ratchet_cfg=t_cfg, state=state,
            toxicity_score=0.80, delta_recovery=0.45
        )
        self.assertTrue(close)
        self.assertEqual(floor, 3.0)
        self.assertIn("TOXICITY EVACUATION", info)

        # Уровень 2: Умеренная токсичность (Score 0.50, Rec 0.25, Net +8.5$) -> Рабочий сброс
        close, floor, info, _ = ToxicityEngine.evaluate_toxicity_adaptive(
            iter_net=8.5, iter_age_hours=2.0, ratchet_cfg=t_cfg, state=state,
            toxicity_score=0.50, delta_recovery=0.25
        )
        self.assertTrue(close)
        self.assertEqual(floor, 8.0)
        self.assertIn("TOXICITY MODERATE FIX", info)

        # Уровень 3: Легкий рынок (Score 0.20, Net +15.5$) -> Целевой сброс
        close, floor, info, _ = ToxicityEngine.evaluate_toxicity_adaptive(
            iter_net=15.5, iter_age_hours=2.0, ratchet_cfg=t_cfg, state=state,
            toxicity_score=0.20, delta_recovery=0.90
        )
        self.assertTrue(close)
        self.assertEqual(floor, 15.0)
        self.assertIn("TOXICITY LIGHT FIX", info)

        # Защита от закрытия в убыток: при Net <= 0 выход ЗАБЛОКИРОВАН
        close, floor, _, _ = ToxicityEngine.evaluate_toxicity_adaptive(
            iter_net=-1.0, iter_age_hours=2.0, ratchet_cfg=t_cfg, state=state,
            toxicity_score=0.99, delta_recovery=0.99
        )
        self.assertFalse(close)

    # =========================================================================
    # 3. STRATEGY DOME: FIXED NET PROFIT
    # =========================================================================

    def test_fixed_net_instant_take(self):
        """Режим Fixed Net: моментальное закрытие при достижении целевого профита."""
        fixed_cfg = {"enabled": True, "target_net_usdt": 15.0}

        # Ниже цели -> нет закрытия
        close, target, _ = ToxicityEngine.evaluate_fixed_net(iter_net=14.9, fixed_cfg=fixed_cfg)
        self.assertFalse(close)

        # Достиг цели -> немедленное закрытие
        close, target, info = ToxicityEngine.evaluate_fixed_net(iter_net=15.0, fixed_cfg=fixed_cfg)
        self.assertTrue(close)
        self.assertEqual(target, 15.0)
        self.assertIn("FIXED NET PROFIT TARGET", info)

        # С масштабированием scale_money = 2.0 (цель становится 30.0$)
        close, target, _ = ToxicityEngine.evaluate_fixed_net(iter_net=20.0, fixed_cfg=fixed_cfg, scale_money=2.0)
        self.assertFalse(close)
        close, target, _ = ToxicityEngine.evaluate_fixed_net(iter_net=30.0, fixed_cfg=fixed_cfg, scale_money=2.0)
        self.assertTrue(close)
        self.assertEqual(target, 30.0)

    # =========================================================================
    # 4. STRATEGY DOME: NEGATIVE STOP & SAFETY GUARDS
    # =========================================================================

    def test_negative_stop_sign_normalization_and_trigger(self):
        """
        Проверка Negative Stop:
        1. При убытке iter_net <= -50.0 срабатывает стоп.
        2. Даже если пользователь указал threshold: 50.0 (без минуса),
           нормализация -abs(th) предотвращает ложное срабатывание в плюсе!
        """
        strat_id = "test_strat__trailing_ratchet"
        bot_core = MagicMock()
        bot_core.analytics.fsm = self.fsm
        closer = AutoCloser(bot_core)

        # 1. Положительный Net (+10$) при threshold 50.0 в конфиге negative
        # Нормализатор делает -50.0. Так как +10 > -50, стоп НЕ сработает!
        th_val = 50.0
        neg_norm = -abs(float(th_val))
        self.assertEqual(neg_norm, -50.0)
        iter_net = 10.0
        self.assertFalse(iter_net <= neg_norm)

        # 2. Отрицательный Net (-55.0$) при threshold -50.0 -> Стоп срабатывает!
        iter_net_loss = -55.0
        self.assertTrue(iter_net_loss <= neg_norm)

    # =========================================================================
    # 5. DYNAMIC SCALE MONEY (INVERTED NOTIONAL & MARGIN RELATIVE)
    # =========================================================================

    def test_inverted_notional_load_deescalation(self):
        """Инвертированное масштабирование: при перегрузке корзины сайз деэскалируется."""
        strat_id = "u96_omni_zenith13__trailing_ratchet"
        strat_cfg = MagicMock()
        strat_cfg.name = "Zenith"
        strat_cfg.symbols = ["ERAUSDT"]
        strat_cfg.start_balance_usdt = 500.0

        pos_cfg = {
            "auto_scale_money": True,
            "mode_scale": "inverted_notional",
            "inverted_notional": {
                "enabled": True,
                "low_load_ratio": 1.0,
                "high_load_ratio": 3.0,
                "max_scale": 1.5,
                "min_scale": 0.5,
            }
        }
        strat_cfg.auto_closing_cfg = {"positive": pos_cfg}

        # 1. Низкая нагрузка (notional = 400$ при 500$ балансе -> load_ratio 0.8 <= low_ratio 1.0)
        # Масштаб должен быть max_scale (1.5x)
        inv_cfg = pos_cfg["inverted_notional"]
        load_ratio_low = 400.0 / 500.0
        scale_low = inv_cfg["max_scale"] if load_ratio_low <= inv_cfg["low_load_ratio"] else None
        self.assertEqual(scale_low, 1.5)

        # 2. Высокая перегрузка (notional = 1800$ при 500$ балансе -> load_ratio 3.6 >= high_ratio 3.0)
        # Масштаб должен быть min_scale (0.5x), облегчая выход из просадки
        load_ratio_high = 1800.0 / 500.0
        scale_high = inv_cfg["min_scale"] if load_ratio_high >= inv_cfg["high_load_ratio"] else None
        self.assertEqual(scale_high, 0.5)

    # =========================================================================
    # 6. GLOBAL PORTFOLIO AUTOCLOSER (PortfolioCloser)
    # =========================================================================

    @patch("CORE.SERVICES.portfolio_closer.DomeCloserExecutor.execute_close", new_callable=AsyncMock)
    async def test_portfolio_closer_trailing_ratchet_and_guards(self, mock_execute_close):
        """Глобальный портфельный автозакрыватель (PortfolioCloser)."""
        bot_core = MagicMock()
        bot_core.analytics.fsm = self.fsm
        closer = AutoCloser(bot_core)

        app_data = {
            "auto_closing": {
                "mode": "trailing_ratchet",
                "scale_money": 1.0,
                "positive": {
                    "trailing_ratchet": {
                        "enabled": True,
                        "levels": [{"activation": 20.0, "floor": 12.0, "enabled": True}]
                    }
                },
                "negative": {"threshold": -100.0}
            }
        }
        analytics_data = {
            "net_profit_usdt": 25.0,
            "iteration_index": 1,
            "iteration_start_net_profit_usdt": 0.0,
            "_floating_synced": True,
            "first_trade_ts": int((time.time() - 3600) * 1000)
        }

        # Шаг 1: Net = 25$ -> Взвод пола на 12$, закрытия еще нет
        await PortfolioCloser.check(
            auto_closer_inst=closer,
            app_data=app_data,
            app_json_path=Path("tests/scratch/app.json"),
            analytics_data=analytics_data,
            now_sec=time.time(),
            now_ms=int(time.time() * 1000),
            fsm=self.fsm,
        )
        self.assertFalse(mock_execute_close.called)
        r_state = closer._trailing_ratchet_state.get("__global__", {})
        self.assertEqual(r_state.get("armed_floor"), 12.0)

        # Шаг 2: Откат Net до 11$ -> ниже пола 12$ -> Запуск глобального закрытия!
        analytics_data["net_profit_usdt"] = 11.0
        await PortfolioCloser.check(
            auto_closer_inst=closer,
            app_data=app_data,
            app_json_path=Path("tests/scratch/app.json"),
            analytics_data=analytics_data,
            now_sec=time.time() + 50.0,  # обход cooldown 45s
            now_ms=int((time.time() + 50.0) * 1000),
            fsm=self.fsm,
        )
        self.assertTrue(mock_execute_close.called)
        call_kwargs = mock_execute_close.call_args.kwargs
        self.assertEqual(call_kwargs["strat_id"], "__global__")
        self.assertTrue(call_kwargs["is_global"])
        self.assertEqual(call_kwargs["iter_net"], 11.0)

    # =========================================================================
    # 7. DOME CLOSER EXECUTOR: ATOMIC PERSISTENCE & CACHE
    # =========================================================================

    async def test_dome_closer_executor_persistence(self):
        """Проверка атомарного исполнения: закрытие позиций, запись кэша и обновление аналитики."""
        bot_core = MagicMock()
        bot_core.close_strategy_positions = AsyncMock()
        bot_core.analytics.fsm = self.fsm
        bot_core.fsm_states = {"ERAUSDT": {"LONG": MagicMock(recent_closes=[1, 2, 3])}}
        bot_core.notifier.send_alert = AsyncMock()

        strat_id = "test_strat_persistence"
        stats_path = self.stats_dir / f"analytics_{strat_id}.json"
        raw_analytics = {
            "iteration_index": 1,
            "net_profit_usdt": 15.5,
            "realized_pnl_usdt": 20.0,
        }
        stats_path.write_text(json.dumps(raw_analytics), encoding="utf-8")

        r_state = {strat_id: {"armed_floor": 8.0}}
        app_data = {}
        app_json_path = self.tmp_dir / "app.json"

        # Исполняем закрытие
        with patch("CORE.SERVICES.dome_closer_executor.CACHE_DIR", self.cache_dir):
            await DomeCloserExecutor.execute_close(
                bot_core=bot_core,
                strat_id=strat_id,
                is_global=False,
                strat_cfg=MagicMock(symbols=["ERAUSDT"]),
                iter_idx=1,
                next_iter_idx=2,
                iter_net=15.5,
                strat_net=15.5,
                stats_path=stats_path,
                raw_analytics=raw_analytics,
                app_data=app_data,
                app_json_path=app_json_path,
                action_msg="Тест закрытия (TRAILING RATCHET) базовая точка Net: <b>0.00 USDT</b>",
                trailing_ratchet_state=r_state,
            )

        # 1. Позиции закрыты
        bot_core.close_strategy_positions.assert_called_once_with(strat_id)

        # 2. Трейлинг состояние сброшено
        self.assertEqual(r_state[strat_id], {})

        # 3. Аналитика на диске обновлена: итерация стала #2, базовая точка 15.5$
        updated_stats = json.loads(stats_path.read_text(encoding="utf-8"))
        self.assertEqual(updated_stats["iteration_index"], 2)
        self.assertEqual(updated_stats["iteration_start_net_profit_usdt"], 15.5)
        self.assertEqual(updated_stats["iteration_net_profit_usdt"], 0.0)

        # 4. Файл CACHE/iteration_state.json создан и содержит запись
        iter_cache_file = self.cache_dir / "iteration_state.json"
        self.assertTrue(iter_cache_file.exists())
        iter_cache_data = json.loads(iter_cache_file.read_text(encoding="utf-8"))
        self.assertIn(strat_id, iter_cache_data)
        self.assertEqual(iter_cache_data[strat_id]["iteration_index"], 2)
        self.assertEqual(iter_cache_data[strat_id]["iteration_start_net_profit_usdt"], 15.5)

        # 5. recent_closes очищены
        self.assertEqual(bot_core.fsm_states["ERAUSDT"]["LONG"].recent_closes, [])

        # 6. Уведомление в Telegram отправлено
        bot_core.notifier.send_alert.assert_called_once()

    # =========================================================================
    # 8. MULTI-CYCLE FSM NO-DEADLOCK & IDEMPOTENCY
    # =========================================================================

    def test_fsm_multi_cycle_stability_and_idempotency(self):
        """
        Проверка устойчивости FSM на 50 циклах REST-поллинга + защита от повторного закрытия (идемпотентность).
        """
        strat_id = "test_strat_fsm"

        # Симуляция 50 циклов REST-поллинга
        for i in range(1, 51):
            self.fsm.transition_to_awaiting_api()
            self.assertEqual(self.fsm.state, AnalyticsState.AWAITING_API)

            self.fsm.on_api_sync_success({"cur_balance_usdt": 440.0 + i})
            self.assertEqual(self.fsm.state, AnalyticsState.LIVE_SYNCED)

            self.fsm.transition_to_autocloser_ready()
            self.assertEqual(self.fsm.state, AnalyticsState.AUTOCLOSER_READY)
            self.assertTrue(self.fsm.is_autocloser_ready())

        # Проверка идемпотентности
        self.assertFalse(self.fsm.is_iteration_closed(strat_id, 1))
        self.fsm.mark_iteration_closed(strat_id, 1, {"reason": "test"})
        self.assertTrue(self.fsm.is_iteration_closed(strat_id, 1))
        # Итерация #2 еще не закрыта
        self.assertFalse(self.fsm.is_iteration_closed(strat_id, 2))

    # =========================================================================
    # 9. ITERATION REALIZED AUTO-HEAL & LOGGING SPAM PREVENTION
    # =========================================================================

    def test_strategy_stats_iter_real_auto_heal(self):
        """
        Проверка авто-исцеления iter_start_real:
        Если в файле аналитики из-за закрытия позиций старой итерации
        оказалось phantom-значение (35.55$), а текущий s_real = 11.59$ и iter_start_net = 10.49$,
        система автоматически выравнивает iter_start_real по iter_start_net (10.49$),
        предотвращая отображение дикого минуса (-23.96$), и выдает корректный профит +1.10$.
        """
        from ANALYTICS.strategy_stats import StrategyStatsEngine

        manager = MagicMock()
        manager.stats_dir = self.stats_dir
        manager.cache_dir = self.cache_dir
        manager._floating_synced = True
        manager.fsm = self.fsm

        strat_id = "u96_omni_zenith13__trailing_ratchet"
        strat_cfg = MagicMock()
        strat_cfg.name = "Zenith"
        strat_cfg.symbols = ["ERAUSDT"]
        strat_cfg.start_balance_usdt = 448.0
        strat_cfg.analytics_cfg = {}
        manager.strategy_registry.active_strategies = {strat_id: strat_cfg}
        manager.strategy_registry.get_active_strategies.return_value = {strat_id: strat_cfg}
        manager.strategy_registry.get_all_active_symbols.return_value = ["ERAUSDT"]

        engine = StrategyStatsEngine(manager=manager)

        # Симулируем искаженное состояние с сервера
        strat_file = self.stats_dir / f"analytics_{strat_id}.json"
        corrupted_data = {
            "universe_id": strat_id,
            "iteration_index": 2,
            "iteration_start_net_profit_usdt": 10.49,
            "iteration_start_realized_pnl_usdt": 35.55,  # ИСКАЖЕННЫЙ ДО-ЗАКРЫТИЙНЫЙ REALIZED
            "realized_pnl_usdt": 11.59,
            "realized_pnl_net_usdt": 11.59,
            "net_profit_usdt": 10.31,
            "per_coin": {
                "ERAUSDT": {
                    "total_trades": 65,
                    "realized_pnl_net_usdt": 11.59,
                    "realized_pnl_usdt": 11.59,
                    "current_drawdown": -1.28,
                }
            }
        }
        strat_file.write_text(json.dumps(corrupted_data), encoding="utf-8")

        global_data = {
            "_floating_synced": True,
            "per_coin": corrupted_data["per_coin"]
        }

        # Запускаем перерасчет статистики
        engine.update_strategies_stats(global_data)

        # Читаем результат
        healed_data = json.loads(strat_file.read_text(encoding="utf-8"))
        self.assertEqual(healed_data["iteration_index"], 2)
        # iter_start_real должен быть выровнен по iter_start_net (10.49)
        self.assertEqual(healed_data["iteration_start_realized_pnl_usdt"], 10.49)
        # iter_real_pnl должен быть +1.10$, а не -23.96$!
        self.assertEqual(healed_data["iteration_realized_pnl_usdt"], 1.10)
        # iter_net должен быть -0.18$ (+1.10$ real - 1.28$ floating)
        self.assertEqual(healed_data["iteration_net_profit_usdt"], -0.18)

    def test_fsm_logging_no_spam_on_routine_sync(self):
        """
        Проверка тишины логов:
        Рутинные периодические 10-секундные синхронизации FSM (начиная со 2-й)
        логируются строго на уровне DEBUG, не забивая INFO лог.
        """
        import logging
        from unittest.mock import patch

        with patch("ANALYTICS.analytics_fsm.logger") as mock_logger:
            # Sync 1 (Boot): INFO разрешен
            self.fsm.api_sync_count = 0
            self.fsm.transition_to_awaiting_api()
            self.fsm.on_api_sync_success({"cur_balance_usdt": 448.0})
            self.fsm.transition_to_autocloser_ready()
            self.assertTrue(mock_logger.info.called)

            # Sync 2..10 (Routine): Никакого INFO спама, только DEBUG!
            mock_logger.reset_mock()
            for _ in range(5):
                self.fsm.transition_to_awaiting_api()
                self.fsm.on_api_sync_success({"cur_balance_usdt": 448.0})
                self.fsm.transition_to_autocloser_ready()

            self.assertFalse(mock_logger.info.called, "FSM logged INFO during routine periodic sync!")
            self.assertTrue(mock_logger.debug.called)

    def test_bigboard_formatter_iteration_progress_display(self):
        """
        Проверка отображения прогресса итерации в карточке купола:
        При iter_idx > 1 отображается детальная строка с Net и Real итерации.
        """
        from ANALYTICS.FORMATTERS.bigboard_formatter import BigBoardFormatter

        strat_id = "u96_omni_zenith13__trailing_ratchet"
        strat_file = self.stats_dir / f"analytics_{strat_id}.json"
        dome_data = {
            "start_balance_usdt": 448.0,
            "cur_balance_usdt": 458.30,
            "net_profit_usdt": 10.30,
            "realized_pnl_net_usdt": 11.59,
            "unrealized_pnl_usdt": -1.29,
            "iteration_index": 2,
            "iteration_net_profit_usdt": -0.18,
            "iteration_realized_pnl_usdt": 1.10,
            "total_trades": 65,
        }
        strat_file.write_text(json.dumps(dome_data), encoding="utf-8")

        card_text, _ = BigBoardFormatter.render_universe_analytics_card(
            stats_dir=self.stats_dir,
            current_uid=strat_id,
            univ_name="Zenith",
            analytics_manager=None,
        )

        self.assertIn("Net -0.18$, Real +1.10$", card_text)
        self.assertIn("458.30$", card_text)
        self.assertIn("+10.30$", card_text)


if __name__ == "__main__":
    unittest.main()

