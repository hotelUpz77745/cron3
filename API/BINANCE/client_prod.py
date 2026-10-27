# ==============================================================================
# Path: API/BINANCE/client_prod.py
# Role: Клиент Binance Futures для боевого режима (Production REST API)
# ==============================================================================

from __future__ import annotations

import asyncio
import aiohttp
import os
import time
import hmac
import hashlib
import urllib.parse
from typing import Any, Dict, List, Optional
from decimal import Decimal
try:
    from consts import REQ_TIMEOUT_SEC, TIME_SLACK_SEC, API_RATE_LIMIT_SEC, API_CONCURRENT_RATE_LIMIT_SEC
except ImportError:
    REQ_TIMEOUT_SEC = 10.0
    TIME_SLACK_SEC = 1.0
    API_RATE_LIMIT_SEC = 0.05
    API_CONCURRENT_RATE_LIMIT_SEC = 0.02
from c_log import UnifiedLogger
from API.BINANCE.validator import OrderValidator, APIResponse
from API.BINANCE.leverage_manager import LeverageManager
from API.BINANCE.income_collector import IncomeCollector, KlineFetcher

logger = UnifiedLogger("BINANCE_API_PROD")


class BinanceClient:
    """
    Асинхронный клиент Binance Futures для боевой торговли (Production).
    Все операции подписываются HMAC SHA256 и отправляются на боевой endpoint Binance.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.validator = OrderValidator(logger)

        self.create_order_url = "https://fapi.binance.com/fapi/v1/order"
        self.cancel_order_url = self.create_order_url
        self.cancel_all_url   = "https://fapi.binance.com/fapi/v1/allOpenOrders"
        self.positions_url    = "https://fapi.binance.com/fapi/v2/account"
        self.position_mode_url = "https://fapi.binance.com/fapi/v1/positionSide/dual"
        self.balance_url      = "https://fapi.binance.com/fapi/v2/balance"
        self.user_trades_url  = "https://fapi.binance.com/fapi/v1/userTrades"
        self.set_leverage_url = "https://fapi.binance.com/fapi/v1/leverage"
        self.set_margin_url   = "https://fapi.binance.com/fapi/v1/marginType"
        self.leverage_bracket_url = "https://fapi.binance.com/fapi/v1/leverageBracket"
        self.open_orders_url  = "https://fapi.binance.com/fapi/v1/openOrders"
        self.income_url       = "https://fapi.binance.com/fapi/v1/income"

        # Session manager
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

        # Rate limiters
        self._api_lock = asyncio.Lock()
        self._last_send_time = 0.0

        self._kline_lock = asyncio.Lock()
        self._kline_last_send = 0.0

    # ==================================================
    # SESSION MANAGER
    # ==================================================
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            return self._session

        timeout_cfg = aiohttp.ClientTimeout(total=REQ_TIMEOUT_SEC)
        self._session = aiohttp.ClientSession(timeout=timeout_cfg)
        return self._session

    async def _close_session(self):
        async with self._session_lock:
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None

    async def shutdown(self):
        """Корректное закрытие aiohttp сессии при остановке бота."""
        await self._close_session()

    # ==================================================
    # SIGNATURE
    # ==================================================
    def _sign(self, query: str) -> str:
        if not self.api_secret:
            return ""
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    # ==================================================
    # CORE REQUEST HANDLER
    # ==================================================
    async def _request(
        self,
        method: str,
        url: str,
        params: Optional[dict] = None,
        signed: bool = False,
        concurrent_mode: bool = False,
    ) -> APIResponse:
        rate_limit = API_CONCURRENT_RATE_LIMIT_SEC if concurrent_mode else API_RATE_LIMIT_SEC

        async with self._api_lock:
            elapsed = time.monotonic() - self._last_send_time
            if elapsed < rate_limit:
                await asyncio.sleep(rate_limit - elapsed)
            self._last_send_time = time.monotonic()

        safe_params = dict(params) if params else {}

        if signed:
            if not self.api_key or not self.api_secret:
                return APIResponse(success=False, error_msg="API keys not configured (empty API_KEY or API_SECRET)")
            ts = int((time.time() - TIME_SLACK_SEC) * 1000)
            safe_params["timestamp"] = ts
            query = urllib.parse.urlencode(safe_params, safe=',')
            signature = self._sign(query)
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{query}&signature={signature}"
            safe_params = None

        headers = {}
        if self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key

        try:
            async with self._session_lock:
                session = await self._get_session()

            req_coro = (
                session.post(url, headers=headers, params=safe_params)
                if method == "POST" else
                session.delete(url, headers=headers, params=safe_params)
                if method == "DELETE" else
                session.get(url, headers=headers, params=safe_params)
            )

            async with req_coro as resp:
                http_status = resp.status
                text = await resp.text()

                try:
                    data = await resp.json()
                except Exception:
                    data = text

                if http_status != 200:
                    code_val = None
                    msg_val = None
                    if isinstance(data, dict):
                        code_val = data.get("code")
                        msg_val = data.get("msg")
                    return APIResponse(
                        success=False,
                        data=data,
                        error_code=code_val,
                        error_msg=msg_val or text,
                        meta={
                            "http_status": http_status,
                            "method": method,
                            "url": url,
                            "params": safe_params,
                            "text": (text or "")[:300],
                        }
                    )

                is_success = True
                error_code = None
                error_msg = None

                if isinstance(data, dict) and "code" in data:
                    try:
                        code_val = int(data["code"])
                        if code_val < 0:
                            is_success = False
                            error_code = code_val
                            error_msg = data.get("msg")
                    except ValueError:
                        pass

                return APIResponse(
                    success=is_success,
                    data=data,
                    error_code=error_code,
                    error_msg=error_msg,
                    meta={
                        "http_status": http_status,
                        "method": method,
                        "url": url,
                        "params": safe_params,
                    }
                )

        except asyncio.TimeoutError:
            return APIResponse(
                success=False,
                error_code=-1,
                error_msg="TIMEOUT",
                meta={"method": method, "url": url, "params": safe_params}
            )

        except Exception as e:
            return APIResponse(
                success=False,
                error_code=-1,
                error_msg=f"EXCEPTION {type(e).__name__}: {e}",
                meta={"method": method, "url": url, "params": safe_params}
            )

    # ==================================================
    # ORDER
    # ==================================================
    @staticmethod
    def _format_decimal(val: Any) -> Optional[str]:
        """
        Преобразует числовое значение (float, Decimal, int, str) в строковый вид без научной экспоненты (e/E).
        Binance REST API строго требует фиксированный десятичный формат (напр. '0.00005053' вместо '5.053e-05').
        """
        if val is None:
            return None
        if isinstance(val, int):
            return str(val)
        try:
            d = Decimal(str(val)).normalize()
            if d == d.to_integral():
                return format(d, 'f')
            else:
                return format(d, 'f').rstrip('0').rstrip('.')
        except Exception:
            return str(val)

    async def make_order(
        self,
        symbol: str,
        qty: float,
        side: str,
        position_side: str,
        market_type: str = "MARKET",
        price: Optional[float] = None,
        reduce_only: bool = False,
        concurrent_mode: bool = False,
    ) -> APIResponse:
        params = {
            "symbol": symbol,
            "side": side,
            "type": market_type,
            "quantity": self._format_decimal(abs(qty)),
            "positionSide": position_side,
            "recvWindow": 20000,
            "newOrderRespType": "RESULT",
        }

        if market_type == "LIMIT":
            params.update({
                "price": self._format_decimal(price),
                "timeInForce": "GTC",
            })

        data = await self._request(
            "POST",
            self.create_order_url,
            params=params,
            signed=True,
            concurrent_mode=concurrent_mode,
        )

        return self.validator.validate_order_create(
            data,
            market_type=market_type,
        )

    # ==================================================
    # CANCEL BY IDS
    # ==================================================
    async def cancel_limit_orders(
        self,
        symbol: str,
        order_id_list: List[int],
    ) -> APIResponse:
        if not order_id_list:
            return APIResponse(success=True, data=[])

        params = {
            "symbol": symbol,
            "orderIdList": f"[{','.join(str(i) for i in order_id_list)}]",
            "recvWindow": 20000,
        }

        data = await self._request(
            "DELETE",
            "https://fapi.binance.com/fapi/v1/batchOrders",
            params=params,
            signed=True,
        )

        return self.validator.validate_order_cancel(data)

    # ==================================================
    # CANCEL ALL
    # ==================================================
    async def cancel_all_orders(self, symbol: str) -> APIResponse:
        """Отменить все открытые ордера по символу."""
        params = {
            "symbol": symbol,
            "recvWindow": 20000,
        }

        data = await self._request(
            "DELETE",
            self.cancel_all_url,
            params=params,
            signed=True,
        )

        return self.validator.validate_cancel_all(data)

    # ==================================================
    # CANCEL FOR SPECIFIC SIDE
    # ==================================================
    async def cancel_orders_for_side(self, symbol: str, position_side: str):
        """Отменяет все открытые ордера по символу для указанной стороны (LONG или SHORT)."""
        open_orders = await self.fetch_open_orders(symbol)
        if not open_orders:
            return APIResponse(success=True, data=[])

        ids_to_cancel = [
            order.get("orderId") for order in open_orders
            if order.get("positionSide") == position_side
        ]

        if not ids_to_cancel:
            return APIResponse(success=True, data=[])

        logger.info(f"[{symbol}] {position_side} Found {len(ids_to_cancel)} active orders. Canceling...")
        return await self.cancel_limit_orders(symbol, ids_to_cancel)

    # ==================================================
    # ACCOUNT INFO & POSITIONS
    # ==================================================
    async def fetch_account_info(self) -> APIResponse:
        params = {"recvWindow": 20000}
        return await self._request(
            "GET",
            self.positions_url,
            params=params,
            signed=True,
        )

    async def fetch_positions(self, symbol: Optional[str] = None) -> List[dict]:
        params = {"recvWindow": 20000}
        res = await self._request(
            "GET",
            self.positions_url,
            params=params,
            signed=True,
        )

        if res.success and isinstance(res.data, dict) and "positions" in res.data:
            positions = res.data.get("positions", [])
            if symbol:
                sym = (symbol or "").upper()
                try:
                    return [p for p in positions if (p.get("symbol") or "").upper() == sym]
                except Exception:
                    return positions
            return positions
        return []

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[dict]:
        params = {"recvWindow": 20000}
        if symbol:
            params["symbol"] = symbol

        res = await self._request(
            "GET",
            self.open_orders_url,
            params=params,
            signed=True,
        )

        if res.success and isinstance(res.data, list):
            return res.data
        return []

    async def fetch_income(
        self,
        symbol: Optional[str] = None,
        income_type: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 1000,
    ) -> APIResponse:
        """Получение истории доходности (income history) с Binance Futures."""
        params: Dict[str, Any] = {"limit": limit, "recvWindow": 20000}
        if symbol:
            params["symbol"] = symbol
        if income_type:
            params["incomeType"] = income_type
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time

        return await self._request(
            "GET",
            self.income_url,
            params=params,
            signed=True,
        )

    async def fetch_balance(self) -> List[dict]:
        params = {"recvWindow": 20000}
        res = await self._request(
            "GET",
            self.balance_url,
            params=params,
            signed=True,
        )
        if res.success and isinstance(res.data, list):
            return res.data
        return []

    async def get_position_mode(self) -> dict:
        params = {"recvWindow": 20000}
        res = await self._request(
            "GET",
            self.position_mode_url,
            params=params,
            signed=True,
        )
        return res.data if res.success and isinstance(res.data, dict) else {}

    async def set_position_mode(self, dual_side: bool = True) -> dict:
        params = {
            "dualSidePosition": "true" if dual_side else "false",
            "recvWindow": 20000,
        }
        res = await self._request(
            "POST",
            self.position_mode_url,
            params=params,
            signed=True,
        )
        return res.data if res.success and isinstance(res.data, dict) else {}

    # ==================================================
    # LEVERAGE & MARGIN
    # ==================================================
    async def set_leverage(self, symbol: str, leverage: int) -> APIResponse:
        return await LeverageManager.set_leverage(self, symbol, leverage)

    async def set_margin_type(self, symbol: str, margin_type: str) -> APIResponse:
        return await LeverageManager.set_margin_type(self, symbol, margin_type)

    async def get_leverage_bracket(self, symbol: Optional[str] = None) -> APIResponse:
        """GET /fapi/v1/leverageBracket - возвращает спецификацию брекетов плеч."""
        return await LeverageManager.get_leverage_bracket(self, symbol)

    async def get_max_leverage(self, symbol: str) -> Optional[int]:
        """Возвращает максимально доступное кредитное плечо для символа."""
        return await LeverageManager.get_max_leverage(self, symbol)


    # ==================================================
    # INCOME & REALIZED PNL
    # ==================================================
    async def get_income_pnl(
        self,
        symbol: str,
        side: str,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> Optional[dict]:
        """Получение REALIZED_PNL и комиссий из userTrades и FUNDING_FEE из income."""
        return await IncomeCollector.get_income_pnl(self, symbol, side, start_time, end_time)

    # ==================================================
    # KLINES (CANDLES)
    # ==================================================
    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 100
    ) -> Optional[List[dict]]:
        """Загрузка исторических свечей (klines)."""
        return await KlineFetcher.get_klines(self, symbol, interval, limit)
