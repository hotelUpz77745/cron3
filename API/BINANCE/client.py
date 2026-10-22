# ==============================================================================
# Path: API/BINANCE/client.py
# Role: Клиент для работы с API Binance Futures (Unified: Prod & Paper)
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

logger = UnifiedLogger("BINANCE_API")


class BinanceClient:
    """
    Единый асинхронный клиент Binance Futures.
    Поддерживает:
      - Боевую торговлю через реальное подписанное REST API Binance Futures
      - Бумажную торговлю при наличии virtual_executor (симуляция исполнения)
      - Мок-режим (MockExchangeProvider) при пустых ключах или MOCK_EXCHANGE=1
      - Загрузку свечей (klines) с ограничением частоты запросов
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        mock_mode: Optional[bool] = None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.validator = OrderValidator(logger)
        self.virtual_executor: Optional[Any] = None

        if mock_mode is not None:
            self.is_mock_mode = bool(mock_mode)
        else:
            from consts import MOCK_MODE
            env_mock = os.getenv("MOCK_EXCHANGE")
            force_mock = False if env_mock == "0" else (True if env_mock == "1" else MOCK_MODE)
            self.is_mock_mode = (
                not self.api_key
                or not self.api_secret
                or force_mock
            )
        if self.is_mock_mode:
            from API.BINANCE.mock_exchange_provider import get_default_mock_exchange
            self._mock_provider = get_default_mock_exchange()
            logger.info("[BINANCE_API] Running in MOCK EXCHANGE mode (credentials empty, app.json mock_mode or MOCK_EXCHANGE=1).")
        else:
            self._mock_provider = None

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

        # ===== SESSION MANAGER =====
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

        # ===== RATE LIMITER =====
        self._api_lock = asyncio.Lock()
        self._last_send_time = 0.0

        # ===== KLINE RATE LIMITER =====
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
        # Мок-режим (MockExchangeProvider)
        if self.is_mock_mode and self._mock_provider:
            return await self._mock_provider.make_order(
                symbol=symbol,
                qty=qty,
                side=side,
                position_side=position_side,
                market_type=market_type,
                price=price,
                reduce_only=reduce_only,
            )

        # Режим симуляции (Paper Trading)
        if getattr(self, "virtual_executor", None):
            await asyncio.sleep(0.1)
            self.virtual_executor.execute_virtual_order(symbol, qty, side, position_side, market_type, price)
            return APIResponse(
                success=True,
                data={"orderId": int(time.time() * 1000), "status": "FILLED"},
                meta={"paper_trading": True}
            )

        # Боевой режим (Prod)
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
        if self.is_mock_mode and self._mock_provider:
            for oid in order_id_list:
                await self._mock_provider.cancel_order(symbol, oid)
            return APIResponse(success=True, data=[])

        if getattr(self, "virtual_executor", None):
            await asyncio.sleep(0.1)
            return APIResponse(success=True, data=[], meta={"paper_trading": True})

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
        if self.is_mock_mode and self._mock_provider:
            return await self._mock_provider.cancel_all_orders(symbol)

        if getattr(self, "virtual_executor", None):
            await asyncio.sleep(0.1)
            return APIResponse(success=True, data=[], meta={"paper_trading": True})

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
        if self.is_mock_mode and self._mock_provider:
            return await self._mock_provider.cancel_orders_for_side(symbol, position_side)

        if getattr(self, "virtual_executor", None):
            await asyncio.sleep(0.1)
            return APIResponse(success=True, data=[], meta={"paper_trading": True})

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
    # ACCOUNT INFO
    # ==================================================
    async def fetch_account_info(self) -> APIResponse:
        if self.is_mock_mode and self._mock_provider:
            return await self._mock_provider.fetch_account_info()

        if getattr(self, "virtual_executor", None):
            await asyncio.sleep(0.1)
            return APIResponse(success=True, data={"positions": []}, meta={"paper_trading": True})

        params = {"recvWindow": 20000}
        return await self._request(
            "GET",
            self.positions_url,
            params=params,
            signed=True,
        )

    # ==================================================
    # POSITIONS
    # ==================================================
    async def fetch_positions(self, symbol: Optional[str] = None) -> List[dict]:
        if self.is_mock_mode and self._mock_provider:
            return await self._mock_provider.fetch_positions(symbol)

        if getattr(self, "virtual_executor", None):
            await asyncio.sleep(0.1)
            return []

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

    # ==================================================
    # OPEN ORDERS
    # ==================================================
    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[dict]:
        if self.is_mock_mode and self._mock_provider:
            return await self._mock_provider.fetch_open_orders(symbol)

        if getattr(self, "virtual_executor", None):
            await asyncio.sleep(0.1)
            return []

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

    # ==================================================
    # INCOME HISTORY
    # ==================================================
    async def fetch_income(
        self,
        symbol: Optional[str] = None,
        income_type: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 1000,
    ) -> APIResponse:
        """Получение истории доходности (income history) с Binance Futures."""
        if self.is_mock_mode and self._mock_provider:
            return await self._mock_provider.fetch_income(
                symbol=symbol,
                income_type=income_type,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
            )

        if getattr(self, "virtual_executor", None):
            await asyncio.sleep(0.01)
            return APIResponse(success=True, data=[])

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

    # ==================================================
    # LEVERAGE
    # ==================================================
    async def set_leverage(self, symbol: str, leverage: int) -> APIResponse:
        if self.is_mock_mode and self._mock_provider:
            return await self._mock_provider.set_leverage(symbol, leverage)

        if getattr(self, "virtual_executor", None):
            await asyncio.sleep(0.1)
            return APIResponse(success=True, data={"leverage": leverage}, meta={"paper_trading": True})

        return await self._request(
            "POST",
            self.set_leverage_url,
            params={
                "symbol": symbol,
                "leverage": leverage,
                "recvWindow": 20000,
            },
            signed=True,
        )

    # ==================================================
    # MARGIN TYPE
    # ==================================================
    async def set_margin_type(self, symbol: str, margin_type: str) -> APIResponse:
        if self.is_mock_mode and self._mock_provider:
            return await self._mock_provider.set_margin_type(symbol, margin_type)

        if getattr(self, "virtual_executor", None):
            await asyncio.sleep(0.05)
            return APIResponse(
                success=True,
                data={"marginType": margin_type},
                meta={"paper_trading": True}
            )

        return await self._request(
            "POST",
            self.set_margin_url,
            params={
                "symbol": symbol,
                "marginType": margin_type,
                "recvWindow": 20000,
            },
            signed=True,
        )

    async def get_leverage_bracket(self, symbol: Optional[str] = None) -> APIResponse:
        """GET /fapi/v1/leverageBracket - возвращает спецификацию брекетов плеч."""
        if self.is_mock_mode and self._mock_provider:
            return await self._mock_provider.get_leverage_bracket(symbol)

        params = {"recvWindow": 20000}
        if symbol:
            params["symbol"] = symbol.upper()
        return await self._request(
            "GET",
            self.leverage_bracket_url,
            params=params,
            signed=True,
        )

    async def get_max_leverage(self, symbol: str) -> Optional[int]:
        """
        Возвращает максимально доступное кредитное плечо для символа (bracket 1 initialLeverage).
        """
        res = await self.get_leverage_bracket(symbol)
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

    # ==================================================
    # INCOME PNL
    # ==================================================
    async def get_income_pnl(
        self,
        symbol: str,
        side: str,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> Optional[dict]:
        if self.is_mock_mode and self._mock_provider:
            if self._mock_provider.simulate_analytics_network_error or os.getenv("MOCK_ANALYTICS_NETWORK_ERROR") == "1":
                return None
            inc_res = await self._mock_provider.fetch_income(
                symbol=symbol,
                start_time=start_time,
                end_time=end_time,
            )
            if not inc_res.success:
                return None
            records = inc_res.data or []
            gross = sum(float(r.get("income", 0.0)) for r in records if r.get("incomeType") == "REALIZED_PNL")
            comm = sum(float(r.get("income", 0.0)) for r in records if r.get("incomeType") == "COMMISSION")
            fund = sum(float(r.get("income", 0.0)) for r in records if r.get("incomeType") == "FUNDING_FEE")
            return {
                "gross_pnl": gross,
                "commission": comm,
                "funding_fee": fund,
                "net_pnl": gross + comm + fund,
            }

        trade_params = {
            "symbol": symbol,
            "recvWindow": 20000,
            "limit": 1000,
        }
        if start_time:
            trade_params["startTime"] = start_time
        if end_time:
            trade_params["endTime"] = end_time

        trade_res = await self._request(
            "GET",
            "https://fapi.binance.com/fapi/v1/userTrades",
            params=trade_params,
            signed=True,
        )

        gross_pnl = 0.0
        commission = 0.0
        bnb_price = 0.0

        if trade_res.success and isinstance(trade_res.data, list):
            for t in trade_res.data:
                if t.get("positionSide") != side:
                    continue

                ts = int(t.get("time", 0))
                if start_time and ts < start_time:
                    continue

                gross_pnl += float(t.get("realizedPnl", 0.0))
                commission_val = float(t.get("commission", 0.0))
                asset = t.get("commissionAsset", "")

                if asset == "BNB":
                    if bnb_price == 0.0:
                        try:
                            from API.BINANCE.public import BinancePublic
                            bulk_p = await BinancePublic.get_prices_bulk(["BNBUSDT"])
                            if "BNBUSDT" in bulk_p:
                                bnb_price = float(bulk_p["BNBUSDT"])
                        except Exception:
                            pass
                    commission -= (commission_val * bnb_price) if bnb_price > 0 else commission_val
                else:
                    commission -= commission_val

        inc_params = {
            "symbol": symbol,
            "incomeType": "FUNDING_FEE",
            "recvWindow": 20000,
            "limit": 1000,
        }
        if start_time:
            inc_params["startTime"] = start_time
        if end_time:
            inc_params["endTime"] = end_time

        inc_res = await self._request(
            "GET",
            self.income_url,
            params=inc_params,
            signed=True,
        )

        funding_fee = 0.0
        if inc_res.success and isinstance(inc_res.data, list):
            for r in inc_res.data:
                ts = int(r.get("time", 0))
                if start_time and ts < start_time:
                    continue
                funding_fee += float(r.get("income", 0.0))

        net_pnl = gross_pnl + commission + funding_fee

        return {
            "gross_pnl": gross_pnl,
            "commission": commission,
            "funding_fee": funding_fee,
            "net_pnl": net_pnl,
        }

    # ==================================================
    # KLINES (CANDLES)
    # ==================================================
    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 100
    ) -> Optional[List[dict]]:
        limit = min(limit, 1500)
        limit_sec = 0.5
        async with self._kline_lock:
            elapsed = time.monotonic() - self._kline_last_send
            if elapsed < limit_sec:
                await asyncio.sleep(limit_sec - elapsed)
            self._kline_last_send = time.monotonic()

        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }

        try:
            async with self._session_lock:
                session = await self._get_session()

            async with session.get(
                url="https://fapi.binance.com/fapi/v1/klines",
                params=params,
                timeout=REQ_TIMEOUT_SEC
            ) as resp:
                if resp.status != 200:
                    logger.error(f"[{symbol}] get_klines error HTTP {resp.status}")
                    return None

                data = await resp.json()
                if not isinstance(data, list):
                    return None

                data.sort(key=lambda x: int(x[0]))

                result = []
                for k in data:
                    result.append({
                        "open_time": int(k[0]),
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                        "close_time": int(k[6])
                    })
                return result

        except Exception as e:
            logger.error(f"[{symbol}] Exception in get_klines: {e}")
            return None
