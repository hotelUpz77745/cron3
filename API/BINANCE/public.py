# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\API\BINANCE\public.py
# Role: public.py module

# ==============================================================================
# Path: API/BINANCE/public.py
# Role: Клиент для работы с публичным API Binance Futures
# ==============================================================================

from __future__ import annotations

import aiohttp

import re
from typing import Optional, Dict, List, Any

VALID_SYMBOL_REGEX = re.compile(r"^[A-Za-z0-9]+$")


def is_valid_symbol(symbol: Any) -> bool:
    """True только для тикеров из латинских букв и цифр (иероглифы и прочее отсекаются)."""
    return isinstance(symbol, str) and VALID_SYMBOL_REGEX.fullmatch(symbol) is not None

class BinancePublic:
    BASE_URL = "https://fapi.binance.com"

    # ==================================================
    # INTERNAL GET
    # ==================================================
    @staticmethod
    async def _get(
        path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Optional[Any]:
        """Внутренний GET для публичных запросов."""

        url = BinancePublic.BASE_URL + path

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        return None
                    return await resp.json()

        except Exception:
            return None

    # ==================================================
    # PUBLIC ENDPOINTS
    # ==================================================

    @staticmethod
    async def get_instruments() -> Optional[List[Dict]]:
        """GET /fapi/v1/exchangeInfo (filtered for Latin alphanumeric symbols)"""
        data = await BinancePublic._get("/fapi/v1/exchangeInfo")

        if isinstance(data, dict) and isinstance(data.get("symbols"), list):
            return [
                s for s in data["symbols"]
                if isinstance(s, dict) and VALID_SYMBOL_REGEX.match(str(s.get("symbol", "")))
            ]

        return None

    @staticmethod
    async def get_24h_ticker() -> Optional[List[Dict]]:
        """GET /fapi/v1/ticker/24hr (filtered for Latin alphanumeric symbols)"""
        data = await BinancePublic._get("/fapi/v1/ticker/24hr")
        if isinstance(data, list):
            return [t for t in data if isinstance(t, dict) and is_valid_symbol(t.get("symbol"))]
        return None

    @staticmethod
    async def get_perp_symbols(quote: str = "USDT", limit: Optional[int] = None) -> List[str]:
        """GET /fapi/v1/exchangeInfo (filtered for PERPETUAL + TRADING + quote + Latin alphanumeric)"""
        data = await BinancePublic._get("/fapi/v1/exchangeInfo")
        if not data or not isinstance(data, dict):
            return []
            
        quote_u = (quote or "USDT").upper()
        out: List[str] = []

        symbols = data.get("symbols", [])
        if not isinstance(symbols, list):
            return []

        for s in symbols:
            if not isinstance(s, dict):
                continue
            if s.get("contractType") != "PERPETUAL":
                continue
            if s.get("status") != "TRADING":
                continue
            if (s.get("quoteAsset") or "").upper() != quote_u:
                continue
            sym = s.get("symbol")
            if sym and VALID_SYMBOL_REGEX.match(str(sym)):
                out.append(str(sym).upper())

        out.sort()
        if limit is not None:
            return out[: int(limit)]
        return out

    @staticmethod
    async def get_mark_price(symbol: str) -> Optional[float]:
        """GET /fapi/v1/premiumIndex (bulk filtered by symbol)"""
        if not symbol:
            return None
        data = await BinancePublic._get("/fapi/v1/premiumIndex")
        if isinstance(data, list):
            target = symbol.upper()
            for item in data:
                if isinstance(item, dict) and (item.get("symbol") or "").upper() == target:
                    try:
                        return float(item["markPrice"])
                    except Exception:
                        return None
        return None

    @staticmethod
    async def get_last_price(symbol: str) -> Optional[float]:
        """GET bulk prices filtered for symbol (без per-symbol запроса)"""
        if not symbol:
            return None
        prices = await BinancePublic.get_prices_bulk([symbol])
        return prices.get(symbol.upper(), None)

    @staticmethod
    async def get_all_prices() -> Optional[List[Dict]]:
        """GET /fapi/v1/ticker/price (без symbol) -> список всех {symbol, price}"""
        data = await BinancePublic._get(
            "/fapi/v1/ticker/price",
            params=None,
        )

        if isinstance(data, list):
            return data

        return None

    @staticmethod
    async def get_prices_bulk(symbols: List[str]) -> Dict[str, float]:
        """Один запрос, возвращает цены только по нужным symbols."""

        out: Dict[str, float] = {}
        if not symbols:
            return out

        data = await BinancePublic.get_all_prices()
        if not data:
            return out

        want = {(s or "").upper() for s in symbols if s}
        for row in data:
            try:
                if not isinstance(row, dict):
                    continue
                sym = (row.get("symbol") or "").upper()
                if sym in want:
                    out[sym] = float(row.get("price"))
            except Exception:
                continue

        return out

    @staticmethod
    async def get_klines(symbol: str, interval: str, limit: int = 100) -> Optional[List[Dict]]:
        """GET /fapi/v1/klines"""
        params = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
        data = await BinancePublic._get("/fapi/v1/klines", params=params)
        if isinstance(data, list):
            out = []
            for k in data:
                try:
                    out.append({
                        "open_time": int(k[0]),
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                    })
                except Exception:
                    continue
            return out
        return None
