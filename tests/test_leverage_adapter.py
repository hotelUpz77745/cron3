# tests/test_leverage_adapter.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: tests.test_leverage_adapter
ROLE: Comprehensive unit tests for LeverageAdapter / LeverageManager
==============================================================================
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from CORE.SERVICES.leverage_adapter import LeverageAdapter, LeverageManager


class TestLeverageAdapter(unittest.IsolatedAsyncioTestCase):
    """Тестирование LeverageAdapter и безопасной установки плеча/маржи."""

    def setUp(self):
        self.adapter = LeverageAdapter()
        self.adapter._cache.clear()
        self.adapter._max_leverage_cache.clear()

        self.mock_client = MagicMock()
        self.mock_client.get_max_leverage = AsyncMock(return_value=25)
        self.mock_client.set_margin_type = AsyncMock(return_value=MagicMock(success=True, error_msg=""))
        self.mock_client.set_leverage = AsyncMock(return_value=MagicMock(success=True, error_msg=""))

    def test_alias_parity(self):
        """Проверяет идентичность LeverageManager и LeverageAdapter."""
        self.assertIs(LeverageManager, LeverageAdapter)

    async def test_set_leverage_clamping_to_max_available(self):
        """Проверяет, что плечо безопасно ограничивается максимумом биржи."""
        side_cfg = {"leverage": 50, "margin_type": "CROSSED"}
        # Биржа возвращает максимум 25x -> плечо должно стать 25x
        with patch.object(self.adapter, "_save_cache"):
            await self.adapter.set_leverage_and_margin(self.mock_client, "BTCUSDT", side_cfg)

        self.assertEqual(side_cfg["leverage"], 25)
        self.mock_client.set_leverage.assert_called_once_with("BTCUSDT", 25)
        self.mock_client.set_margin_type.assert_called_once_with("BTCUSDT", "CROSSED")
        self.assertTrue(self.adapter.is_configured("BTCUSDT"))

    async def test_cached_symbol_zero_network_calls(self):
        """Проверяет, что для закэшированного символа сетевые вызовы не производятся (0 requests)."""
        self.adapter._cache["ETHUSDT"] = True
        side_cfg = {"leverage": 20, "margin_type": "CROSSED"}

        await self.adapter.set_leverage_and_margin(self.mock_client, "ETHUSDT", side_cfg)
        self.mock_client.set_leverage.assert_not_called()
        self.mock_client.set_margin_type.assert_not_called()


if __name__ == "__main__":
    unittest.main()
