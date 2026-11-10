# ==============================================================================
# Path: tests/test_delisting_detector.py
# Role: Comprehensive unit & integration tests for DelistingDetector (Binance USDT-M delisting monitor)
# ==============================================================================

import asyncio
import os
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from CORE.SERVICES.delisting_detector import DelistingDetector
from API.BINANCE.public import BinancePublic
from POS_FSM.models import PositionState


class _FakeBot:
    """Полноценная тестовая обвязка BotCore для проверки как детекции, так и реального исполнения _hard_close/_warn."""

    def __init__(self, symbols):
        self.symbols = [s.upper() for s in symbols]
        self._closing_symbols = set()
        self._locks = {}
        self.client = MagicMock()
        self.client.cancel_all_orders = AsyncMock()
        self.client.fetch_positions = AsyncMock(return_value=[])
        self.client.make_order = AsyncMock()

        self.fsm_states = {
            s: {
                "LONG": PositionState(symbol=s, side="LONG"),
                "SHORT": PositionState(symbol=s, side="SHORT"),
            }
            for s in self.symbols
        }
        self.runtime_manager = MagicMock()
        self.avg_manager = MagicMock()
        self.notifier = MagicMock()
        self.notifier.send_alert = AsyncMock()
        self.delete_symbol = AsyncMock(return_value=True)

    def get_client_for_symbol(self, sym: str):
        return self.client

    def _get_symbol_lock(self, sym: str) -> asyncio.Lock:
        if sym not in self._locks:
            self._locks[sym] = asyncio.Lock()
        return self._locks[sym]


class TestDelistingDetector(unittest.IsolatedAsyncioTestCase):
    def _make(self, symbols=("BTCUSDT",)):
        return DelistingDetector(_FakeBot(symbols))

    def _install_recorders(self, det):
        """Подменяет реакции детектора на записывающие заглушки для изолированных тестов маршрутизации."""
        closed, warned = [], []

        async def fake_close(sym, info, time_left_sec):
            closed.append((sym, info.get("reason"), time_left_sec))

        async def fake_warn(sym, info, time_left_sec):
            warned.append((sym, info.get("reason"), time_left_sec))

        det._hard_close = fake_close  # type: ignore[assignment]
        det._warn = fake_warn  # type: ignore[assignment]
        return closed, warned

    async def test_status_change_triggers_hard_close(self):
        det = self._make(("BTCUSDT",))
        closed, warned = self._install_recorders(det)

        async def fake_signals(force_refresh=False):
            return {"BTCUSDT": {"reason": "STATUS_CHANGE", "status": "SETTLING", "delist_time_sec": time.time()}}

        det.get_delisted_symbols = fake_signals  # type: ignore[assignment]
        await det._check_once()

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0][0], "BTCUSDT")
        self.assertEqual(closed[0][1], "STATUS_CHANGE")
        self.assertEqual(warned, [])

    async def test_delivery_within_threshold_hard_close(self):
        det = self._make(("BTCUSDT",))
        closed, warned = self._install_recorders(det)
        now_sec = time.time()

        async def fake_signals(force_refresh=False):
            return {"BTCUSDT": {"reason": "DELIST_SCHEDULED", "status": "TRADING", "delist_time_sec": now_sec + 3 * 3600}}

        det.get_delisted_symbols = fake_signals  # type: ignore[assignment]
        await det._check_once()

        self.assertEqual(len(closed), 1)
        self.assertEqual(warned, [])
        self.assertLessEqual(closed[0][2], det.hard_close_sec)

    async def test_delivery_beyond_threshold_warn_only(self):
        det = self._make(("BTCUSDT",))
        closed, warned = self._install_recorders(det)
        now_sec = time.time()

        async def fake_signals(force_refresh=False):
            return {"BTCUSDT": {"reason": "DELIST_SCHEDULED", "status": "TRADING", "delist_time_sec": now_sec + 10 * 3600}}

        det.get_delisted_symbols = fake_signals  # type: ignore[assignment]
        await det._check_once()

        self.assertEqual(closed, [])
        self.assertEqual(len(warned), 1)
        self.assertGreater(warned[0][2], det.hard_close_sec)

    async def test_news_triggers_hard_close(self):
        det = self._make(("BTCUSDT",))
        closed, warned = self._install_recorders(det)

        async def fake_signals(force_refresh=False):
            return {"BTCUSDT": {"reason": "NEWS_DELIST", "status": "", "delist_time_sec": time.time()}}

        det.get_delisted_symbols = fake_signals  # type: ignore[assignment]
        await det._check_once()

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0][1], "NEWS_DELIST")
        self.assertIsNone(closed[0][2])
        self.assertEqual(warned, [])

    async def test_hard_close_is_idempotent(self):
        det = self._make(("BTCUSDT",))
        closed, _ = self._install_recorders(det)

        async def fake_signals(force_refresh=False):
            return {"BTCUSDT": {"reason": "STATUS_CHANGE", "status": "CLOSE", "delist_time_sec": time.time()}}

        det.get_delisted_symbols = fake_signals  # type: ignore[assignment]
        await det._check_once()
        await det._check_once()

        self.assertEqual(len(closed), 1)

    async def test_foreign_symbols_filtered_out(self):
        det = self._make(("BTCUSDT",))
        instruments = [
            {"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "deliveryDate": 4133404800000},
            {"symbol": "OTHERUSDT", "status": "CLOSE", "contractType": "PERPETUAL", "deliveryDate": 0},
        ]
        with patch.object(BinancePublic, "get_instruments", new=AsyncMock(return_value=instruments)):
            with patch.object(det, "_fetch_news_delistings", new=AsyncMock(return_value=set())):
                res = await det.get_delisted_symbols(force_refresh=True)

        self.assertNotIn("OTHERUSDT", res)
        self.assertNotIn("BTCUSDT", res)

    async def test_news_signal_detected_from_scraper(self):
        det = self._make(("BTCUSDT", "ERAUSDT"))
        instruments = [
            {"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "deliveryDate": 4133404800000},
            {"symbol": "ERAUSDT", "status": "TRADING", "contractType": "PERPETUAL", "deliveryDate": 4133404800000},
        ]
        with patch.object(BinancePublic, "get_instruments", new=AsyncMock(return_value=instruments)):
            with patch.object(det, "_fetch_news_delistings", new=AsyncMock(return_value={"ERAUSDT"})):
                res = await det.get_delisted_symbols(force_refresh=True)

        self.assertIn("ERAUSDT", res)
        self.assertEqual(res["ERAUSDT"]["reason"], "NEWS_DELIST")
        self.assertNotIn("BTCUSDT", res)

    async def test_news_fetch_uses_adapter_and_extracts_tickers(self):
        det = self._make(("ERAUSDT",))
        articles = [{"code": "ARTICLE1"}]
        detail = {"title": "Binance Futures Will Delist ERAUSDT Perpetual", "body": "Trading stops."}
        with patch.object(BinancePublic, "get_cms_articles", new=AsyncMock(return_value=articles)) as m_cat:
            with patch.object(BinancePublic, "get_cms_article_detail", new=AsyncMock(return_value=detail)):
                found = await det._fetch_news_delistings()

        m_cat.assert_awaited_once()
        self.assertEqual(found, {"ERAUSDT"})

    async def test_news_fetch_caches_parsed_articles_and_skips_non_futures_titles(self):
        """Проверяет дедупликацию _parsed_article_codes (отсутствие повторных HTTP-запросов) и пред-фильтр заголовков."""
        det = self._make(("ERAUSDT", "VVVUSDT"))
        articles = [
            {"code": "SPOT_ONLY_1", "title": "Binance Spot Will Delist XYZ on 2026-11-15"},
            {"code": "FUT_DELIST_2", "title": "Binance Futures Will Delist ERAUSDT and VVV Perpetual Contracts"},
        ]
        detail = {
            "title": "Binance Futures Will Delist ERAUSDT and VVV Perpetual Contracts",
            "body": "Users are advised to close positions before UTC.",
        }
        with patch.object(BinancePublic, "get_cms_articles", new=AsyncMock(return_value=articles)):
            with patch.object(BinancePublic, "get_cms_article_detail", new=AsyncMock(return_value=detail)) as m_det:
                found_1 = await det._fetch_news_delistings()
                found_2 = await det._fetch_news_delistings()

        # SPOT_ONLY_1 отсеян по заголовку каталога без запроса detail; FUT_DELIST_2 скачан ровно 1 раз за 2 цикла!
        m_det.assert_awaited_once_with("FUT_DELIST_2", 10.0)
        self.assertEqual(found_1, {"ERAUSDT", "VVVUSDT"})
        self.assertEqual(found_2, {"ERAUSDT", "VVVUSDT"})

    async def test_real_hard_close_executes_market_liquidation_fsm_reset_and_pool_removal(self):
        """
        Интеграционный тест реального тела _hard_close (без заглушек):
        - отмена всех лимитных ордеров;
        - рыночное закрытие LONG (SELL) и SHORT (BUY) в Hedge Mode;
        - сброс FSM и AvgManager;
        - удаление монеты из обоймы через bot.delete_symbol;
        - отправка критического Telegram-алерта.
        """
        det = self._make(("ERAUSDT", "ZECUSDT"))
        bot = det.bot
        bot.fsm_states["ERAUSDT"]["LONG"].in_position = True
        bot.fsm_states["ERAUSDT"]["SHORT"].in_position = True

        bot.client.fetch_positions = AsyncMock(
            return_value=[
                {"symbol": "ERAUSDT", "positionSide": "LONG", "positionAmt": "150.0"},
                {"symbol": "ERAUSDT", "positionSide": "SHORT", "positionAmt": "-75.5"},
                {"symbol": "ZECUSDT", "positionSide": "LONG", "positionAmt": "10.0"},
            ]
        )

        info = {"reason": "STATUS_CHANGE", "status": "SETTLING", "delist_time_sec": time.time()}
        await det._escalate("ERAUSDT", info, time_left_sec=120.0)

        # 1. Отмена лимиток только по ERAUSDT
        bot.client.cancel_all_orders.assert_awaited_once_with("ERAUSDT")

        # 2. Рыночная ликвидация обеих сторон ERAUSDT (ZECUSDT не тронут)
        self.assertEqual(bot.client.make_order.await_count, 2)
        calls = [c.kwargs for c in bot.client.make_order.await_args_list]
        self.assertIn(
            {"symbol": "ERAUSDT", "qty": 150.0, "side": "SELL", "position_side": "LONG", "market_type": "MARKET"},
            calls,
        )
        self.assertIn(
            {"symbol": "ERAUSDT", "qty": 75.5, "side": "BUY", "position_side": "SHORT", "market_type": "MARKET"},
            calls,
        )

        # 3. Сброс FSM и AvgManager для обеих сторон
        self.assertEqual(bot.runtime_manager.reset_state_to_default.call_count, 2)
        self.assertEqual(bot.avg_manager.reset.call_count, 2)

        # 4. Вызов delete_symbol и очистка _closing_symbols
        bot.delete_symbol.assert_awaited_once_with("ERAUSDT")
        self.assertNotIn("ERAUSDT", bot._closing_symbols)

        # 5. Telegram-уведомление
        bot.notifier.send_alert.assert_awaited_once()
        alert_text = bot.notifier.send_alert.await_args[0][0]
        self.assertIn("DELISTING → HARD CLOSE", alert_text)
        self.assertIn("ERAUSDT", alert_text)

    async def test_phase1_warn_to_phase2_hard_close_escalation_lifecycle(self):
        """
        Проверяет полный жизненный цикл:
        Фаза 1 (за 10ч до делистинга -> 1 предупреждение, без повторов) ->
        Фаза 2 (осталось 2ч <= 4ч порога -> жёсткое закрытие и удаление из _warned).
        """
        det = self._make(("LITUSDT",))
        bot = det.bot
        now_sec = time.time()

        # Шаг 1: 10 часов до делистинга -> Фаза 1 (Warning)
        signals_10h = {
            "LITUSDT": {"reason": "DELIST_SCHEDULED", "status": "TRADING", "delist_time_sec": now_sec + 10 * 3600}
        }
        with patch.object(det, "get_delisted_symbols", new=AsyncMock(return_value=signals_10h)):
            await det._check_once()
            await det._check_once()  # повторный тик в Фазе 1 не должен слать дубль алерта

        self.assertIn("LITUSDT", det._warned)
        self.assertNotIn("LITUSDT", det._closed)
        self.assertEqual(bot.notifier.send_alert.await_count, 1)
        warn_msg = bot.notifier.send_alert.await_args[0][0]
        self.assertIn("DELISTING WARNING", warn_msg)
        bot.delete_symbol.assert_not_awaited()

        # Шаг 2: Время подошло, осталось 2 часа (<= 4ч hard_close_sec) -> переход в Фазу 2 (Hard Close)
        signals_2h = {
            "LITUSDT": {"reason": "DELIST_SCHEDULED", "status": "TRADING", "delist_time_sec": time.time() + 2 * 3600}
        }
        with patch.object(det, "get_delisted_symbols", new=AsyncMock(return_value=signals_2h)):
            await det._check_once()

        self.assertNotIn("LITUSDT", det._warned)
        self.assertIn("LITUSDT", det._closed)
        bot.delete_symbol.assert_awaited_once_with("LITUSDT")
        self.assertEqual(bot.notifier.send_alert.await_count, 2)
        hard_msg = bot.notifier.send_alert.await_args[0][0]
        self.assertIn("DELISTING → HARD CLOSE", hard_msg)

    async def test_ttl_cache_avoids_redundant_network_calls(self):
        """Проверяет, что get_delisted_symbols(force_refresh=False) возвращает кэш в пределах poll_interval_sec."""
        det = self._make(("ERAUSDT",))
        now_ms = int((time.time() + 7200) * 1000)
        instruments = [
            {"symbol": "ERAUSDT", "status": "TRADING", "contractType": "PERPETUAL", "deliveryDate": now_ms}
        ]
        with patch.object(BinancePublic, "get_instruments", new=AsyncMock(return_value=instruments)) as m_inst:
            with patch.object(det, "_fetch_news_delistings", new=AsyncMock(return_value=set())):
                res1 = await det.get_delisted_symbols(force_refresh=False)
                res2 = await det.get_delisted_symbols(force_refresh=False)

        m_inst.assert_awaited_once()
        self.assertEqual(res1, res2)
        self.assertIn("ERAUSDT", res2)

    async def test_start_and_stop_lifecycle(self):
        """Проверяет корректный запуск и остановку фоновой задачи монитора."""
        det = self._make(("BTCUSDT",))
        with patch("CORE.SERVICES.delisting_detector._is_backtest", return_value=False):
            with patch.object(det, "_check_once", new=AsyncMock()):
                await det.start()
                self.assertIsNotNone(det._task)
                await asyncio.sleep(0.01)
                await det.stop()
                self.assertIsNone(det._task)

    async def test_backtest_is_noop(self):
        det = self._make(("BTCUSDT",))
        with patch.dict(os.environ, {"CRON3_BACKTEST": "1"}):
            res = await det.get_delisted_symbols(force_refresh=True)
        self.assertEqual(res, {})


if __name__ == "__main__":
    unittest.main()
