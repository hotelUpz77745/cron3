# ==============================================================================
# Path: API/BINANCE/mock_exchange_provider.py
# Role: Институциональный симулятор биржи Binance Futures (Mock Exchange Provider)
#       для безопасного тестирования Telegram-интерфейса, кнопок и аналитики
# ==============================================================================

from __future__ import annotations

import copy
import os
import time
from typing import Any, Dict, List, Optional

from c_log import UnifiedLogger
from API.BINANCE.validator import APIResponse

logger = UnifiedLogger("MockExchangeProvider")


class MockExchangeProvider:
    """
    Симулированный провайдер данных и операций биржи Binance Futures.
    Используется при пустых API_KEY/API_SECRET или флаге MOCK_EXCHANGE=1.
    Предоставляет реалистичные псевдо-усредненные позиции, ордера, балансы и доходы,
    а также поддерживает управляемую эмуляцию сетевых сбоев.
    """

    _instance: Optional["MockExchangeProvider"] = None

    def __init__(self):
        self._wallet_balance: float = 450.00
        self._next_order_id: int = 910001
        self.simulate_analytics_network_error: bool = False
        self.simulated_error_msg: str = "Connection reset by peer (Simulated Binance API network failure)"
        self.simulated_error_code: int = -1001

        # Псевдо-усредненные активные позиции (откалиброваны под актуальные котировки рынка)
        # ICNTUSDT: 3 исполненных ступени сетки L0+L1+L2 (12%+8%+9% = 29% от 400$ = 116$ / 0.1895 = 612.14)
        # AVAUSDT:  2 исполненных ступени сетки L0+L1 (7%+6% = 13% от 400$ = 52$ / 0.3725 = 139.60)
        self._positions: Dict[str, Dict[str, Any]] = {
            "ICNTUSDT_LONG": {
                "symbol": "ICNTUSDT",
                "positionSide": "LONG",
                "positionAmt": 612.14,
                "entryPrice": 0.1895,
                "markPrice": 0.1873,
                "unrealizedProfit": -1.35,
                "initialMargin": 5.80,
                "maintMargin": 0.58,
                "isolated": False,
                "leverage": 20,
            },
            "AVAUSDT_SHORT": {
                "symbol": "AVAUSDT",
                "positionSide": "SHORT",
                "positionAmt": -139.60,
                "entryPrice": 0.3725,
                "markPrice": 0.3712,
                "unrealizedProfit": 0.18,
                "initialMargin": 2.60,
                "maintMargin": 0.26,
                "isolated": False,
                "leverage": 20,
            },
            "GTCUSDT_LONG": {
                "symbol": "GTCUSDT",
                "positionSide": "LONG",
                "positionAmt": 0.0,
                "entryPrice": 0.0,
                "markPrice": 0.7200,
                "unrealizedProfit": 0.0,
                "initialMargin": 0.0,
                "maintMargin": 0.0,
                "isolated": False,
                "leverage": 20,
            },
            "GWEIUSDT_LONG": {
                "symbol": "GWEIUSDT",
                "positionSide": "LONG",
                "positionAmt": 0.0,
                "entryPrice": 0.0,
                "markPrice": 0.0350,
                "unrealizedProfit": 0.0,
                "initialMargin": 0.0,
                "maintMargin": 0.0,
                "isolated": False,
                "leverage": 20,
            },
            "SOLVUSDT_LONG": {
                "symbol": "SOLVUSDT",
                "positionSide": "LONG",
                "positionAmt": 0.0,
                "entryPrice": 0.0,
                "markPrice": 1.2500,
                "unrealizedProfit": 0.0,
                "initialMargin": 0.0,
                "maintMargin": 0.0,
                "isolated": False,
                "leverage": 20,
            },
        }

        # Открытые ордера (включая Take Profit для открытых позиций)
        self._open_orders: List[Dict[str, Any]] = [
            {
                "orderId": 900101,
                "symbol": "ICNTUSDT",
                "status": "NEW",
                "clientOrderId": "tp_icnt_900101",
                "price": "0.1923",
                "avgPrice": "0.0",
                "origQty": "612.14",
                "executedQty": "0.0",
                "cumQuote": "0.0",
                "timeInForce": "GTC",
                "type": "LIMIT",
                "reduceOnly": True,
                "closePosition": False,
                "side": "SELL",
                "positionSide": "LONG",
                "stopPrice": "0.0",
                "workingType": "CONTRACT_PRICE",
                "time": int((time.time() - 3600) * 1000),
                "updateTime": int((time.time() - 3600) * 1000),
            },
            {
                "orderId": 900201,
                "symbol": "AVAUSDT",
                "status": "NEW",
                "clientOrderId": "tp_ava_900201",
                "price": "0.3669",
                "avgPrice": "0.0",
                "origQty": "139.60",
                "executedQty": "0.0",
                "cumQuote": "0.0",
                "timeInForce": "GTC",
                "type": "LIMIT",
                "reduceOnly": True,
                "closePosition": False,
                "side": "BUY",
                "positionSide": "SHORT",
                "stopPrice": "0.0",
                "workingType": "CONTRACT_PRICE",
                "time": int((time.time() - 1800) * 1000),
                "updateTime": int((time.time() - 1800) * 1000),
            },
        ]

        # История доходов (реализованный PnL, комиссии, фандинг)
        now_ts = int(time.time() * 1000)
        self._income_records: List[Dict[str, Any]] = [
            {"tranId": 80001, "symbol": "ICNTUSDT", "incomeType": "REALIZED_PNL", "income": "12.4500", "asset": "USDT", "time": now_ts - 7200000},
            {"tranId": 80002, "symbol": "ICNTUSDT", "incomeType": "COMMISSION", "income": "-0.0820", "asset": "USDT", "time": now_ts - 7200000},
            {"tranId": 80003, "symbol": "AVAUSDT", "incomeType": "REALIZED_PNL", "income": "8.3200", "asset": "USDT", "time": now_ts - 5400000},
            {"tranId": 80004, "symbol": "AVAUSDT", "incomeType": "COMMISSION", "income": "-0.0540", "asset": "USDT", "time": now_ts - 5400000},
            {"tranId": 80005, "symbol": "GTCUSDT", "incomeType": "REALIZED_PNL", "income": "5.6000", "asset": "USDT", "time": now_ts - 3600000},
            {"tranId": 80006, "symbol": "GTCUSDT", "incomeType": "COMMISSION", "income": "-0.0380", "asset": "USDT", "time": now_ts - 3600000},
            {"tranId": 80007, "symbol": "ICNTUSDT", "incomeType": "FUNDING_FEE", "income": "-0.0120", "asset": "USDT", "time": now_ts - 1800000},
        ]

    @classmethod
    def get_instance(cls) -> "MockExchangeProvider":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def enable_analytics_network_error(self, error_msg: Optional[str] = None, error_code: int = -1001) -> None:
        """Включает эмуляцию сетевого сбоя при запросах аналитики."""
        self.simulate_analytics_network_error = True
        self.simulated_error_msg = error_msg or "Connection reset by peer (Simulated Binance API network failure)"
        self.simulated_error_code = error_code
        logger.warning(f"[MOCK_EXCHANGE] Simulated network failure enabled for analytics API: {self.simulated_error_msg}")

    def enable_analytics_ip_ban(self, ban_seconds: int = 60) -> None:
        """Включает эмуляцию бана по IP для проверки Circuit Breaker."""
        ban_until = int((time.time() + ban_seconds) * 1000)
        self.enable_analytics_network_error(
            error_msg=f"Way too many requests; IP banned until {ban_until}. Please use the websocket for live updates.",
            error_code=-1003,
        )

    def disable_analytics_network_error(self) -> None:
        """Отключает эмуляцию сетевого сбоя."""
        self.simulate_analytics_network_error = False
        self.simulated_error_msg = "Connection reset by peer (Simulated Binance API network failure)"
        self.simulated_error_code = -1001
        logger.info("[MOCK_EXCHANGE] Simulated network failure disabled.")

    def reset_to_default_state(self) -> None:
        """Сбрасывает состояние мока к начальному эталонному состоянию."""
        self.__init__()

    # =========================================================================
    # БИРЖЕВЫЕ МЕТОДЫ (API-совместимость с BinanceClient)
    # =========================================================================

    async def fetch_account_info(self) -> APIResponse:
        """Возвращает информацию об аккаунте или эмулирует сетевой сбой."""
        if self.simulate_analytics_network_error or os.getenv("MOCK_ANALYTICS_NETWORK_ERROR") == "1":
            return APIResponse(
                success=False,
                error_msg=self.simulated_error_msg,
                error_code=self.simulated_error_code,
                meta={"simulated": True},
            )

        pos_list = []
        total_unrealized = 0.0
        for p in self._positions.values():
            pos_dict = copy.deepcopy(p)
            pos_dict["positionAmt"] = str(pos_dict["positionAmt"])
            pos_dict["entryPrice"] = str(pos_dict["entryPrice"])
            pos_dict["markPrice"] = str(pos_dict["markPrice"])
            pos_dict["unrealizedProfit"] = str(pos_dict["unrealizedProfit"])
            pos_dict["leverage"] = str(pos_dict.get("leverage", 20))
            pos_list.append(pos_dict)
            total_unrealized += float(p["unrealizedProfit"])

        margin_balance = self._wallet_balance + total_unrealized
        data = {
            "feeTier": 0,
            "canTrade": True,
            "canDeposit": True,
            "canWithdraw": True,
            "updateTime": int(time.time() * 1000),
            "totalInitialMargin": "8.40",
            "totalMaintMargin": "0.84",
            "totalWalletBalance": f"{self._wallet_balance:.2f}",
            "totalUnrealizedProfit": f"{total_unrealized:.2f}",
            "totalMarginBalance": f"{margin_balance:.2f}",
            "totalPositionInitialMargin": "8.40",
            "totalOpenOrderInitialMargin": "0.00",
            "maxWithdrawAmount": f"{margin_balance:.2f}",
            "positions": pos_list,
        }
        return APIResponse(success=True, data=data)

    async def fetch_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Возвращает открытые позиции."""
        result = []
        sym_filter = symbol.upper().strip() if symbol else None
        for p in self._positions.values():
            if sym_filter and p["symbol"] != sym_filter:
                continue
            amt = float(p.get("positionAmt", 0.0))
            if abs(amt) > 1e-8:
                pos_dict = copy.deepcopy(p)
                pos_dict["positionAmt"] = str(pos_dict["positionAmt"])
                pos_dict["entryPrice"] = str(pos_dict["entryPrice"])
                pos_dict["markPrice"] = str(pos_dict["markPrice"])
                pos_dict["unrealizedProfit"] = str(pos_dict["unrealizedProfit"])
                result.append(pos_dict)
        return result

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Возвращает список активных ордеров."""
        if not symbol:
            return copy.deepcopy(self._open_orders)
        sym_upper = symbol.upper().strip()
        return [copy.deepcopy(o) for o in self._open_orders if o.get("symbol") == sym_upper]

    async def fetch_income(
        self,
        symbol: Optional[str] = None,
        income_type: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 1000,
    ) -> APIResponse:
        """Возвращает историю доходов или эмулирует сетевой сбой."""
        if self.simulate_analytics_network_error or os.getenv("MOCK_ANALYTICS_NETWORK_ERROR") == "1":
            return APIResponse(
                success=False,
                error_msg=self.simulated_error_msg,
                error_code=self.simulated_error_code,
                meta={"simulated": True},
            )

        records = copy.deepcopy(self._income_records)
        if symbol:
            sym_u = symbol.upper().strip()
            records = [r for r in records if r.get("symbol") == sym_u]
        if income_type:
            records = [r for r in records if r.get("incomeType") == income_type]
        if start_time:
            records = [r for r in records if r.get("time", 0) >= start_time]
        if end_time:
            records = [r for r in records if r.get("time", 0) <= end_time]
        return APIResponse(success=True, data=records[:limit])

    async def fetch_balance(self) -> List[Dict[str, Any]]:
        """Возвращает балансы активов."""
        return [
            {
                "accountAlias": "mock_account",
                "asset": "USDT",
                "balance": f"{self._wallet_balance:.2f}",
                "crossWalletBalance": f"{self._wallet_balance:.2f}",
                "crossUnPnl": "-1.64",
                "availableBalance": f"{(self._wallet_balance - 8.40):.2f}",
                "maxWithdrawAmount": f"{(self._wallet_balance - 8.40):.2f}",
            }
        ]

    async def make_order(
        self,
        symbol: str,
        qty: float,
        side: str,
        position_side: str = "BOTH",
        price: Optional[float] = None,
        market_type: str = "LIMIT",
        reduce_only: bool = False,
    ) -> APIResponse:
        """Создает симулированный ордер."""
        self._next_order_id += 1
        oid = self._next_order_id
        sym = symbol.upper().strip()
        side_u = side.upper().strip()
        pos_side = position_side.upper().strip()
        if pos_side == "BOTH":
            pos_side = "LONG" if side_u == "BUY" else "SHORT"

        is_market = market_type.upper() == "MARKET"
        order_status = "FILLED" if is_market else "NEW"
        exec_qty = str(qty) if is_market else "0.0"

        order_data = {
            "orderId": oid,
            "symbol": sym,
            "status": order_status,
            "clientOrderId": f"mock_order_{oid}",
            "price": str(price or 0.0),
            "origQty": str(qty),
            "executedQty": exec_qty,
            "cumQuote": "0.0",
            "timeInForce": "GTC",
            "type": market_type.upper(),
            "reduceOnly": reduce_only,
            "side": side_u,
            "positionSide": pos_side,
            "time": int(time.time() * 1000),
            "updateTime": int(time.time() * 1000),
        }
        if not is_market:
            self._open_orders.append(order_data)
        else:
            # Немедленное исполнение рыночного ордера и обновление позиции в MockExchange
            pos_key = f"{sym}_{pos_side}"
            pos = self._positions.get(pos_key)
            if not pos:
                pos = {
                    "symbol": sym,
                    "positionSide": pos_side,
                    "positionAmt": 0.0,
                    "entryPrice": 0.0,
                    "markPrice": float(price or 0.0) or 1.0,
                    "unrealizedProfit": 0.0,
                    "initialMargin": 0.0,
                    "maintMargin": 0.0,
                    "isolated": False,
                    "leverage": 20,
                }
                self._positions[pos_key] = pos

            cur_amt = float(pos.get("positionAmt", 0.0))
            cur_p = float(price or pos.get("markPrice") or 1.0)
            if pos_side == "LONG":
                if side_u == "BUY":
                    new_amt = cur_amt + qty
                    pos["positionAmt"] = round(new_amt, 4)
                    pos["entryPrice"] = cur_p
                else:  # SELL (closing / reducing long)
                    new_amt = max(0.0, cur_amt - qty)
                    pos["positionAmt"] = round(new_amt, 4)
                    if new_amt == 0.0:
                        pos["entryPrice"] = 0.0
                        pos["unrealizedProfit"] = 0.0
            elif pos_side == "SHORT":
                if side_u == "SELL":
                    new_amt = cur_amt - qty  # shorts are negative positionAmt
                    pos["positionAmt"] = round(new_amt, 4)
                    pos["entryPrice"] = cur_p
                else:  # BUY (closing / reducing short)
                    new_amt = min(0.0, cur_amt + qty)
                    pos["positionAmt"] = round(new_amt, 4)
                    if new_amt == 0.0:
                        pos["entryPrice"] = 0.0
                        pos["unrealizedProfit"] = 0.0

            pos["markPrice"] = cur_p

        logger.info(f"[MOCK_EXCHANGE] Placed order: {sym} {pos_side} {side_u} {qty} @ {price} (ID: {oid})")
        return APIResponse(success=True, data=order_data)

    async def cancel_order(self, symbol: str, order_id: int) -> APIResponse:
        """Отменяет симулированный ордер по ID."""
        sym = symbol.upper().strip()
        matched = [o for o in self._open_orders if o.get("orderId") == order_id]
        if matched:
            self._open_orders = [o for o in self._open_orders if o.get("orderId") != order_id]
            logger.info(f"[MOCK_EXCHANGE] Canceled order {order_id} for {sym}")
            return APIResponse(success=True, data={"orderId": order_id, "status": "CANCELED"})
        return APIResponse(success=False, error_msg=f"Order {order_id} not found", error_code=-2011)

    async def cancel_orders_for_side(self, symbol: str, position_side: str) -> APIResponse:
        """Отменяет все ордера для указанной стороны позиции."""
        sym = symbol.upper().strip()
        pside = position_side.upper().strip()
        self._open_orders = [
            o for o in self._open_orders
            if not (o.get("symbol") == sym and o.get("positionSide") == pside)
        ]
        return APIResponse(success=True, data=[])

    async def cancel_limit_orders(self, symbol: str, position_side: str) -> APIResponse:
        """Отменяет лимитные ордера для указанной стороны."""
        return await self.cancel_orders_for_side(symbol, position_side)

    async def cancel_all_orders(self, symbol: str) -> APIResponse:
        """Отменяет все ордера по символу."""
        sym = symbol.upper().strip()
        self._open_orders = [o for o in self._open_orders if o.get("symbol") != sym]
        return APIResponse(success=True, data=[])

    async def set_leverage(self, symbol: str, leverage: int) -> APIResponse:
        return APIResponse(success=True, data={"symbol": symbol, "leverage": leverage})

    async def set_margin_type(self, symbol: str, margin_type: str) -> APIResponse:
        return APIResponse(success=True, data={"code": 200, "msg": "success"})

    async def get_position_mode(self) -> dict:
        return {"dualSidePosition": True}

    async def set_position_mode(self, dual_side: bool = True) -> dict:
        return {"code": 200, "msg": "success"}

    async def get_leverage_bracket(self, symbol: Optional[str] = None) -> APIResponse:
        return APIResponse(success=True, data=[])


def get_default_mock_exchange() -> MockExchangeProvider:
    """Возвращает глобальный экземпляр MockExchangeProvider."""
    return MockExchangeProvider.get_instance()
