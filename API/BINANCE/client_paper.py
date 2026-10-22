# ==============================================================================
# Path: API/BINANCE/client_paper.py
# Role: Клиент Binance Futures для режима Paper Trading (симуляция)
# ==============================================================================

from __future__ import annotations

import asyncio
import aiohttp
import time
from typing import Any, Dict, List, Optional
try:
    from consts import REQ_TIMEOUT_SEC, TIME_SLACK_SEC, API_RATE_LIMIT_SEC, API_CONCURRENT_RATE_LIMIT_SEC
except ImportError:
    REQ_TIMEOUT_SEC = 10.0
    TIME_SLACK_SEC = 1.0
    API_RATE_LIMIT_SEC = 0.05
    API_CONCURRENT_RATE_LIMIT_SEC = 0.02
from c_log import UnifiedLogger
from API.BINANCE.validator import OrderValidator, APIResponse

logger = UnifiedLogger("BINANCE_API_PAPER")


class BinanceClient:
    """
    Асинхронный клиент Binance Futures для режима Paper Trading (бумажная торговля).
    Ордера и управление позициями исполняются виртуально через virtual_executor.
    Запросы рыночных данных (свечи, цены) обращаются к публичному REST API Binance.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.validator = OrderValidator(logger)
        self.virtual_executor: Optional[Any] = None

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
    # REQUEST HANDLER (Market / Public Data)
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
    # ORDER (VIRTUAL / PAPER)
    # ==================================================
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
        await asyncio.sleep(0)  # Мгновенная виртуальная эмуляция без задержки

        if hasattr(self, "virtual_executor") and self.virtual_executor:
            self.virtual_executor.execute_virtual_order(symbol, qty, side, position_side, market_type, price)

        return APIResponse(
            success=True,
            data={"orderId": int(time.time() * 1000), "status": "FILLED"},
            meta={"paper_trading": True}
        )

    # ==================================================
    # CANCEL ORDERS (SIMULATED)
    # ==================================================
    async def cancel_limit_orders(
        self,
        symbol: str,
        order_id_list: List[int],
    ) -> APIResponse:
        await asyncio.sleep(0)
        return APIResponse(success=True, data=[], meta={"paper_trading": True})

    async def cancel_all_orders(self, symbol: str) -> APIResponse:
        """Отменить все открытые ордера по символу (симуляция)."""
        await asyncio.sleep(0)
        return APIResponse(success=True, data=[], meta={"paper_trading": True})

    async def cancel_orders_for_side(self, symbol: str, position_side: str):
        """Отменяет открытые ордера по указанной стороне (симуляция)."""
        await asyncio.sleep(0)
        return APIResponse(success=True, data=[], meta={"paper_trading": True})

    # ==================================================
    # ACCOUNT & POSITIONS (SIMULATED)
    # ==================================================
    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[dict]:
        await asyncio.sleep(0)
        return []

    async def fetch_income(
        self,
        symbol: Optional[str] = None,
        income_type: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 1000,
    ) -> APIResponse:
        """Получение истории доходности в бумажном режиме (пустой список)."""
        await asyncio.sleep(0)
        return APIResponse(success=True, data=[])

    async def fetch_positions(self, symbol: Optional[str] = None) -> List[dict]:
        await asyncio.sleep(0)
        res = []
        if hasattr(self, "virtual_executor") and self.virtual_executor:
            owner = getattr(self.virtual_executor, "owner", None)
            fsm_states = getattr(owner, "fsm_states", {}) if owner else {}
            symbols = [symbol] if symbol else list(fsm_states.keys())
            for s in symbols:
                sides = fsm_states.get(s, {})
                for side, st in sides.items():
                    amt = getattr(st, "total_volume", 0.0)
                    if side == "SHORT" and amt > 0:
                        amt = -amt
                    entry = getattr(st, "avg_entry_price", 0.0)
                    res.append({
                        "symbol": s,
                        "positionSide": side,
                        "positionAmt": str(amt),
                        "entryPrice": str(entry),
                    })
        return res

    async def fetch_account_info(self) -> APIResponse:
        await asyncio.sleep(0)
        return APIResponse(success=True, data={"positions": []}, meta={"paper_trading": True})

    async def fetch_balance(self) -> List[dict]:
        await asyncio.sleep(0)
        return [{"asset": "USDT", "balance": "10000.0", "availableBalance": "10000.0"}]

    async def get_position_mode(self) -> dict:
        return {"dualSidePosition": True}

    async def set_position_mode(self, dual_side: bool = True) -> dict:
        return {"code": 200, "msg": "success"}

    # ==================================================
    # LEVERAGE & MARGIN (SIMULATED)
    # ==================================================
    async def set_leverage(self, symbol: str, leverage: int) -> APIResponse:
        await asyncio.sleep(0)
        return APIResponse(success=True, data={"leverage": leverage}, meta={"paper_trading": True})

    async def set_margin_type(self, symbol: str, margin_type: str) -> APIResponse:
        await asyncio.sleep(0)
        return APIResponse(
            success=True,
            data={"marginType": margin_type},
            meta={"paper_trading": True}
        )

    # ==================================================
    # KLINES (CANDLES - REAL PUBLIC API)
    # ==================================================
    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 100
    ) -> Optional[List[dict]]:
        """
        Получение свечей без pandas, сортировка по времени.
        Возвращает список словарей: [{"open_time": int, "high": float, "low": float, ...}, ...]
        """
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
                timeout=aiohttp.ClientTimeout(total=REQ_TIMEOUT_SEC)
            ) as resp:
                if resp.status != 200:
                    logger.error(f"[{symbol}] get_klines error HTTP {resp.status}")
                    return None

                data = await resp.json()
                if not isinstance(data, list):
                    return None

                # Сортируем по Open time
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
            err_msg = str(e) or repr(e)
            logger.error(f"[{symbol}] Exception in get_klines: {type(e).__name__} ({err_msg})")
            return None
