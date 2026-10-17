import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import numpy as np
from consts import DATA_DIR
from c_utils import Utils
from CORE.SERVICES.VOLATILITY.klines_cache import KlinesCache
from CORE.SERVICES.VOLATILITY.volatility_scanner import VolatilityScanner
from CORE.SERVICES.SELECTOR.alpha_coin_selector import AlphaCoinSelector
from CORE.SERVICES.SELECTOR.coin_selection_service import CoinSelectionService
from CORE.SERVICES.SELECTOR.samsonov_funnel import SamsonovFunnelSelector


class TestScreenersUnifiedService(unittest.TestCase):
    """
    Комплексные тесты единого сервиса скринеров:
    1. VolatilityScanner: расчет по базам H-L и O-C, детектирование наибольшей свечи (тени, тело, цвет).
    2. KlinesCache: 5-минутный TTL, in-memory кэширование свечей.
    3. AlphaCoinSelector: каскадное чтение пула монет строго из VolatilityScanner.
    4. SamsonovFunnelSelector: безопасное уведомление 'Пусто, выросла капуста' при отсутствии сырья/радара.
    5. CoinSelectionService: универсальный фасад и форматирование Telegram отчетов для всех 3 скринеров.
    """

    def setUp(self):
        KlinesCache.clear()

    def test_volatility_scanner_hl_and_oc_and_extreme_candle(self):
        # 1. Тестовый свечной ряд: 3 свечи
        # Свеча 1: Open=100, High=110, Low=95, Close=105 -> Зеленая, H-L=((110/95)-1)*100 = 15.79%, O-C=((105-100)/100)*100 = 5.0%
        # Свеча 2: Open=120, High=125, Low=90, Close=95  -> Красная, H-L=((125/90)-1)*100 = 38.89%, O-C=((120-95)/95)*100 = 26.32%
        # Свеча 3: Open=100, High=105, Low=98, Close=102 -> Зеленая, H-L=((105/98)-1)*100 = 7.14%,  O-C=((102-100)/100)*100 = 2.0%
        klines = [
            {"open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0},
            {"open": 120.0, "high": 125.0, "low": 90.0, "close": 95.0},
            {"open": 100.0, "high": 105.0, "low": 98.0, "close": 102.0},
        ]

        # База H-L
        passed_hl, avg_hl, count_hl, max_c_hl = VolatilityScanner.compute_atr_volatility(
            klines, window=3, strict_window=True, calc_base="H-L"
        )
        self.assertTrue(passed_hl)
        self.assertEqual(count_hl, 3)
        self.assertAlmostEqual(avg_hl, (15.789 + 38.888 + 7.142) / 3.0, delta=0.1)
        self.assertIsNotNone(max_c_hl)
        self.assertEqual(max_c_hl["color"], "red")
        self.assertEqual(max_c_hl["color_label"], "🔴 Красная")
        self.assertEqual(max_c_hl["shadow_span_usdt"], 35.0)  # 125 - 90
        self.assertEqual(max_c_hl["body_span_usdt"], 25.0)    # 120 - 95
        self.assertEqual(max_c_hl["calc_base"], "H-L")

        # База O-C
        passed_oc, avg_oc, count_oc, max_c_oc = VolatilityScanner.compute_atr_volatility(
            klines, window=3, strict_window=True, calc_base="O-C"
        )
        self.assertTrue(passed_oc)
        self.assertEqual(count_oc, 3)
        self.assertIsNotNone(max_c_oc)
        self.assertEqual(max_c_oc["color"], "red")
        self.assertEqual(max_c_oc["calc_base"], "O-C")
        self.assertAlmostEqual(max_c_oc["atr_pct"], 26.32, places=1)

    def test_alpha_selector_takes_pool_from_volatility_scanner(self):
        # Эмулируем volatile_symbols.json с пулом монет (например, 3 монеты)
        mock_volatile_coins = [
            {"symbol": "AAAUSDT", "volatility": 32.5, "candles": 12},
            {"symbol": "BBBUSDT", "volatility": 28.1, "candles": 12},
            {"symbol": "CCCUSDT", "volatility": 26.0, "candles": 12},
        ]
        test_file = DATA_DIR / "volatile_symbols.json"
        original_content = None
        if test_file.exists():
            original_content = Utils.read_json_file(test_file)

        try:
            Utils.write_json_file(test_file, mock_volatile_coins)

            # Мокаем получение свечей для этих трех монет
            async def run_test():
                KlinesCache.clear()
                mock_klines = [
                    {"open": 10.0 + (i % 2), "high": 12.0, "low": 9.0, "close": 10.5, "volume": 1000.0}
                    for i in range(30)
                ]
                with patch("CORE.SERVICES.SELECTOR.alpha_coin_selector.BinancePublic.get_klines", new_callable=AsyncMock) as mock_fetch:
                    mock_fetch.return_value = mock_klines
                    results = await AlphaCoinSelector.select_top_coins(
                        target_count=2, use_volatility_pool=True, volatile_file_path=test_file
                    )

                    # Проверяем, что отобраны монеты строго из mock_volatile_coins
                    self.assertTrue(len(results) > 0)
                    for r in results:
                        self.assertIn(r["symbol"], ["AAAUSDT", "BBBUSDT", "CCCUSDT"])

            asyncio.run(run_test())

        finally:
            if original_content is not None:
                Utils.write_json_file(test_file, original_content)
            elif test_file.exists():
                try:
                    test_file.unlink()
                except Exception:
                    pass

    def test_samsonov_funnel_empty_notice_cabbage(self):
        # Создаем селектор Самсонова с несуществующей директорией радара
        fake_dir = Path("CACHE/NON_EXISTENT_RADAR_DIR_12345")
        selector = SamsonovFunnelSelector(radar_stats_dir=fake_dir)
        funnel_data = selector.get_funnel_stages(universe_id="u0_test", target_count=4)

        self.assertEqual(funnel_data["total_input"], 0)
        self.assertEqual(len(funnel_data["final_targets"]), 0)
        self.assertEqual(funnel_data.get("empty_notice"), "Пусто, выросла капуста")

        # Проверяем форматирование Telegram отчета в CoinSelectionService
        svc = CoinSelectionService(radar_stats_dir=fake_dir)
        report = svc.format_telegram_report([], selector_type="samsonov_funnel", target_count=4)
        self.assertIn("Пусто, выросла капуста", report)
        self.assertIn("🥬", report)

    def test_coin_selection_service_facade_all_three(self):
        svc = CoinSelectionService()

        # 1. Форматирование отчета Alpha
        alpha_items = [
            {"symbol": "SOLUSDT", "score": 1.45, "natr": 1.85, "wiggle": 14.2, "move_pct": 2.1, "is_active_in_bot": False}
        ]
        alpha_rep = svc.format_telegram_report(alpha_items, "alpha_coin_selector", 1)
        self.assertIn("Alpha Coin Selector", alpha_rep)
        self.assertIn("SOLUSDT", alpha_rep)
        self.assertIn("Wiggle", alpha_rep)

        # 2. Форматирование отчета Samsonov
        sam_items = [
            {"symbol": "DOGEUSDT", "score": 2.85, "trades": 45, "resilience": 2.85, "is_active_in_bot": True, "current_strategy": "u16_chita__classic"}
        ]
        sam_rep = svc.format_telegram_report(sam_items, "samsonov_funnel", 1)
        self.assertIn("Samsonov Funnel Selector", sam_rep)
        self.assertIn("DOGEUSDT", sam_rep)
        self.assertIn("Сделок: <code>45</code>", sam_rep)

        # 3. Форматирование отчета Volatility Scanner
        vol_items = [
            {
                "symbol": "ETHUSDT",
                "score": 28.5,
                "volatility": 28.5,
                "calc_base": "H-L",
                "candles": 12,
                "is_active_in_bot": False,
                "max_candle": {
                    "atr_pct": 36.2,
                    "color_label": "🟢 Зеленая",
                    "shadow_span_pct": 36.2,
                    "body_span_pct": 22.1,
                }
            }
        ]
        vol_rep = svc.format_telegram_report(vol_items, "volatility_scanner", 1)
        self.assertIn("Volatility Scanner", vol_rep)
        self.assertIn("ETHUSDT", vol_rep)
        self.assertIn("Max свеча", vol_rep)
        self.assertIn("🟢 Зеленая", vol_rep)

    def test_klines_cache_ttl_and_reuse(self):
        KlinesCache.clear()
        mock_candles = [{"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}]

        # Сохраняем в кэш
        KlinesCache.set("BTCUSDT", "15m", 96, mock_candles)

        # Повторное получение в пределах 300 сек
        res = KlinesCache.get("BTCUSDT", "15m", 96, ttl_sec=300)
        self.assertEqual(res, mock_candles)

        # Истекший TTL
        res_expired = KlinesCache.get("BTCUSDT", "15m", 96, ttl_sec=0.0)
        self.assertIsNone(res_expired)


if __name__ == "__main__":
    unittest.main()
