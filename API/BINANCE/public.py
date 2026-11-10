# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\API\BINANCE\public.py
# Role: public.py module

# ==============================================================================
# Path: API/BINANCE/public.py
# Role: Клиент для работы с публичным API Binance Futures
# ==============================================================================

from __future__ import annotations

import aiohttp

import os
import re
from typing import Optional, Dict, List, Any

VALID_SYMBOL_REGEX = re.compile(r"^[A-Za-z0-9]+$")


def is_valid_symbol(symbol: Any) -> bool:
    """True только для тикеров из латинских букв и цифр (иероглифы и прочее отсекаются)."""
    return isinstance(symbol, str) and VALID_SYMBOL_REGEX.fullmatch(symbol) is not None

class BinancePublic:
    BASE_URL = "https://fapi.binance.com"

    # Новостной CMS Binance (домен www, не fapi) — константы адаптера, не конфига.
    CMS_BASE_URL = "https://www.binance.com"
    CMS_CATALOG_PATH = "/bapi/composite/v1/public/cms/article/catalog/list/query"
    CMS_DETAIL_PATH = "/bapi/composite/v1/public/cms/article/detail/query"

    # ==================================================
    # INTERNAL GET
    # ==================================================
    @staticmethod
    async def _get_url(
        url: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_sec: float = 10,
    ) -> Optional[Any]:
        """Внутренний GET по абсолютному URL для публичных запросов."""
        if os.environ.get("CRON3_BACKTEST") == "1":
            return None

        try:
            timeout = aiohttp.ClientTimeout(total=timeout_sec)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        return None
                    return await resp.json()

        except Exception:
            return None

    @staticmethod
    async def _get(
        path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Optional[Any]:
        """Внутренний GET для публичных запросов из BASE_URL."""
        return await BinancePublic._get_url(BinancePublic.BASE_URL + path, params=params)

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

    # ==================================================
    # PUBLIC CMS (news) ENDPOINTS
    # ==================================================
    @staticmethod
    async def get_cms_articles(
        catalog_id: int,
        page_no: int,
        page_size: int,
        timeout_sec: float,
    ) -> Optional[List[Dict]]:
        """GET CMS Binance: список анонсов каталога. Возвращает сырой список статей или None."""
        data = await BinancePublic._get_url(
            BinancePublic.CMS_BASE_URL + BinancePublic.CMS_CATALOG_PATH,
            params={"catalogId": catalog_id, "pageNo": page_no, "pageSize": page_size},
            timeout_sec=timeout_sec,
        )
        if isinstance(data, dict):
            articles = (data.get("data") or {}).get("articles")
            if isinstance(articles, list):
                return articles
        return None

    @staticmethod
    async def get_cms_article_detail(article_code: str, timeout_sec: float) -> Optional[Dict]:
        """GET CMS Binance: детальная статья анонса по коду. Возвращает payload статьи или None."""
        data = await BinancePublic._get_url(
            BinancePublic.CMS_BASE_URL + BinancePublic.CMS_DETAIL_PATH,
            params={"articleCode": article_code},
            timeout_sec=timeout_sec,
        )
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            return data["data"]
        return None
