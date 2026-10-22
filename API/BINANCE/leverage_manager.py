# API/BINANCE/leverage_manager.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: API.BINANCE.leverage_manager
ROLE: Управление кредитным плечом, маржинальным режимом и спецификациями брекетов (REST)
==============================================================================
"""

from typing import Any, Optional
from API.BINANCE.validator import APIResponse
from c_log import UnifiedLogger

logger = UnifiedLogger("LEVERAGE_MANAGER")


class LeverageManager:
    """Менеджер для запросов плеча, типа маржи и спецификаций кредитных брекетов."""

    @staticmethod
    async def set_leverage(client: Any, symbol: str, leverage: int) -> APIResponse:
        if client.is_mock_mode and client._mock_provider:
            return await client._mock_provider.set_leverage(symbol, leverage)

        return await client._request(
            "POST",
            client.set_leverage_url,
            params={
                "symbol": symbol,
                "leverage": leverage,
                "recvWindow": 20000,
            },
            signed=True,
        )

    @staticmethod
    async def set_margin_type(client: Any, symbol: str, margin_type: str) -> APIResponse:
        if client.is_mock_mode and client._mock_provider:
            return await client._mock_provider.set_margin_type(symbol, margin_type)

        return await client._request(
            "POST",
            client.set_margin_url,
            params={
                "symbol": symbol,
                "marginType": margin_type,
                "recvWindow": 20000,
            },
            signed=True,
        )

    @staticmethod
    async def get_leverage_bracket(client: Any, symbol: Optional[str] = None) -> APIResponse:
        """GET /fapi/v1/leverageBracket - возвращает спецификацию брекетов плеч."""
        if client.is_mock_mode and client._mock_provider:
            return await client._mock_provider.get_leverage_bracket(symbol)

        params = {"recvWindow": 20000}
        if symbol:
            params["symbol"] = symbol.upper()
        return await client._request(
            "GET",
            client.leverage_bracket_url,
            params=params,
            signed=True,
        )

    @staticmethod
    async def get_max_leverage(client: Any, symbol: str) -> Optional[int]:
        """
        Возвращает максимально доступное кредитное плечо для символа (bracket 1 initialLeverage).
        """
        res = await LeverageManager.get_leverage_bracket(client, symbol)
        if not res.success or not res.data:
            return None

        data = res.data
        if isinstance(data, list) and len(data) > 0:
            item = data[0] if isinstance(data[0], dict) else {}
            brackets = item.get("brackets", [])
            if brackets and isinstance(brackets, list):
                val = brackets[0].get("initialLeverage")
                if val is not None:
                    return int(val)
        elif isinstance(data, dict):
            brackets = data.get("brackets", [])
            if brackets and isinstance(brackets, list):
                val = brackets[0].get("initialLeverage")
                if val is not None:
                    return int(val)
        return None
