# ==============================================================================
# Path: API/BINANCE/client_sim.py
# Role: Высокоточный симулированный клиент Binance Futures (In-Memory Match Engine для бэктестов)
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Tuple

from c_log import UnifiedLogger
from API.BINANCE.validator import APIResponse
from API.BINANCE.sim_history_store import SimHistoricalDataStore
from API.BINANCE.sim_liquidation import BacktestExecutionConfigLoader, CrossMarginLiquidationGuard

logger = UnifiedLogger("SimulatedBinanceClient")


@dataclass
class SimOrder:
    order_id: int
    symbol: str
    side: str            # 'BUY' | 'SELL'
    position_side: str   # 'LONG' | 'SHORT'
    order_type: str      # 'MARKET' | 'LIMIT'
    qty: float
    price: float
    reduce_only: bool = False
    status: str = "NEW"  # 'NEW' | 'FILLED' | 'CANCELED'
    created_ts: int = 0
    filled_ts: int = 0
    filled_price: float = 0.0
    fee_paid: float = 0.0


@dataclass
class SimPosition:
    symbol: str
    position_side: str   # 'LONG' | 'SHORT'
    qty: float = 0.0
    entry_price: float = 0.0
    leverage: int = 20
    margin_type: str = "CROSSED"  # 'CROSSED' | 'ISOLATED'
    realized_pnl: float = 0.0
    total_fee: float = 0.0


class SimulatedBinanceClient:
    """
    Симулированный брокер/клиент Binance Futures для бэктестера 100% паритета.
    Реализует полный контракт BinanceClient:
    - make_order (MARKET, LIMIT, ReduceOnly)
    - cancel_orders_for_side
    - cancel_limit_orders
    - fetch_positions
    - fetch_open_orders
    - on_tick: высокопроизводительный матчинг лимиток по реальным тикам,
      списание комиссии (0.05% Taker по регламенту) и пересчет эквити/маржи.
    """

    def __init__(
        self,
        start_balance_usdt: float = 449.0,
        fee_rate: float = 0.0005,  # 0.05% Taker по регламенту лаборатории
        leverage: int = 20,
        maker_fee_rate: float = 0.0002,  # 0.02% Maker для лимитных ордеров (Take-Profit)
        slippage_rate: float = 0.0001,  # 0.01% (1 bps) реалистичное проскальзывание на всех MARKET-ордерах (вход и выход)
        liquidation_guard_enabled: Optional[bool] = None,
        maint_margin_rate: Optional[float] = None,
    ):
        self.initial_balance = float(start_balance_usdt)
        self.balance = float(start_balance_usdt)
        self.fee_rate = float(fee_rate)
        self.maker_fee_rate = float(maker_fee_rate)
        self.slippage_rate = max(0.0, float(slippage_rate))
        self.default_leverage = int(leverage)

        liq_enabled, mmr = BacktestExecutionConfigLoader.resolve_liquidation_params(
            liquidation_guard_enabled, maint_margin_rate
        )
        self.liquidation_guard_enabled: bool = liq_enabled
        self.maint_margin_rate: float = mmr
        self.liquidation_guard = CrossMarginLiquidationGuard(enabled=liq_enabled, maint_margin_rate=mmr)
        self.is_liquidated: bool = False
        self.liquidation_details: Optional[Dict[str, Any]] = None
        self._has_active_positions: bool = False

        self._next_order_id = 100000
        self.positions: Dict[Tuple[str, str], SimPosition] = {}
        self.open_orders: Dict[int, SimOrder] = {}
        self.order_history: List[SimOrder] = []
        self.current_prices: Dict[str, float] = {}
        self.current_sim_time: int = 0
        self._completed_hourly_bars: Dict[str, List[Tuple[int, float]]] = {}
        self._cur_hour_bar: Dict[str, Tuple[int, float]] = {}

        # Статистика портфеля
        self.total_fees_paid: float = 0.0
        self.peak_balance: float = float(start_balance_usdt)
        self.max_drawdown_usdt: float = 0.0
        self.trade_fills_count: int = 0
        self.spot_balance: float = 0.0
        self.total_transferred_to_spot_usdt: float = 0.0
        self.on_fill_callback: Optional[Any] = None

    @property
    def wallet_balance(self) -> float:
        return self.balance

    def _get_position(self, symbol: str, position_side: str) -> SimPosition:
        key = (symbol.upper(), position_side.upper())
        if key not in self.positions:
            self.positions[key] = SimPosition(
                symbol=symbol.upper(),
                position_side=position_side.upper(),
                leverage=self.default_leverage
            )
        return self.positions[key]

    # ==================================================
    # CONTRACT METHODS FOR CORE/TradeCycleManager
    # ==================================================

    async def set_leverage(self, symbol: str, leverage: int) -> APIResponse:
        pos_long = self._get_position(symbol, "LONG")
        pos_short = self._get_position(symbol, "SHORT")
        pos_long.leverage = int(leverage)
        pos_short.leverage = int(leverage)
        return APIResponse(success=True, data={"symbol": symbol, "leverage": leverage})

    async def change_margin_type(self, symbol: str, margin_type: str) -> APIResponse:
        pos_long = self._get_position(symbol, "LONG")
        pos_short = self._get_position(symbol, "SHORT")
        pos_long.margin_type = margin_type.upper()
        pos_short.margin_type = margin_type.upper()
        return APIResponse(success=True, data={"symbol": symbol, "marginType": margin_type})

    async def set_margin_type(self, symbol: str, margin_type: str) -> APIResponse:
        return await self.change_margin_type(symbol, margin_type)

    async def get_max_leverage(self, symbol: str) -> int:
        return 50

    async def fetch_positions(self, symbol: Optional[str] = None) -> List[dict]:
        res = []
        for (sym, p_side), pos in self.positions.items():
            if symbol and sym.upper() != symbol.upper():
                continue
            signed_qty = pos.qty if p_side == "LONG" else -pos.qty
            res.append({
                "symbol": sym,
                "positionSide": p_side,
                "positionAmt": str(signed_qty),
                "entryPrice": str(pos.entry_price),
                "leverage": str(pos.leverage),
                "unrealizedProfit": str(self._calc_unrealized_pnl(pos)),
            })
        return res

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[dict]:
        res = []
        for o in self.open_orders.values():
            if symbol and o.symbol.upper() != symbol.upper():
                continue
            res.append({
                "orderId": o.order_id,
                "symbol": o.symbol,
                "positionSide": o.position_side,
                "side": o.side,
                "type": o.order_type,
                "price": str(o.price),
                "origQty": str(o.qty),
                "status": o.status,
            })
        return res

    async def cancel_orders_for_side(self, symbol: str, position_side: str) -> APIResponse:
        sym_u = symbol.upper()
        side_u = position_side.upper()
        canceled_ids = []
        for oid, o in list(self.open_orders.items()):
            if o.symbol == sym_u and o.position_side == side_u:
                o.status = "CANCELED"
                canceled_ids.append(oid)
                del self.open_orders[oid]
        return APIResponse(success=True, data={"canceled_ids": canceled_ids})

    async def cancel_limit_orders(self, symbol: str, order_ids: List[int]) -> APIResponse:
        sym_u = symbol.upper()
        canceled = []
        for oid in order_ids:
            if oid in self.open_orders and self.open_orders[oid].symbol == sym_u:
                self.open_orders[oid].status = "CANCELED"
                canceled.append(oid)
                del self.open_orders[oid]
        return APIResponse(success=True, data={"canceled": canceled})

    async def cancel_all_orders(self, symbol: str) -> APIResponse:
        sym_u = symbol.upper()
        canceled = []
        for oid, o in list(self.open_orders.items()):
            if o.symbol == sym_u:
                o.status = "CANCELED"
                canceled.append(oid)
                del self.open_orders[oid]
        return APIResponse(success=True, data={"canceled": canceled})

    def _apply_market_slippage(self, base_price: float, side: str) -> float:
        """Применяет однонаправленное рыночное проскальзывание (BUY дороже, SELL дешевле)."""
        if self.slippage_rate <= 0.0 or base_price <= 0.0:
            return base_price
        if side.upper() == "BUY":
            return base_price * (1.0 + self.slippage_rate)
        return base_price * (1.0 - self.slippage_rate)

    async def make_order(
        self, symbol: str, qty: float, side: str, position_side: str,
        market_type: str = "MARKET", price: Optional[float] = None,
        reduce_only: bool = False, concurrent_mode: bool = False
    ) -> APIResponse:
        if self.is_liquidated:
            return APIResponse(success=False, error_code=-2010, error_msg="Account liquidated")
        sym_u, side_u, pos_side_u, qty_abs = symbol.upper(), side.upper(), position_side.upper(), abs(float(qty))
        if qty_abs <= 0:
            return APIResponse(success=False, error_code=-1013, error_msg="Filter failure: LOT_SIZE qty <= 0")

        self._next_order_id += 1
        oid = self._next_order_id
        order = SimOrder(
            order_id=oid, symbol=sym_u, side=side_u, position_side=pos_side_u,
            order_type=market_type.upper(), qty=qty_abs, price=float(price or 0.0),
            reduce_only=reduce_only, created_ts=self.current_sim_time,
        )

        if market_type.upper() == "MARKET":
            # Базовая цена: явно переданная (например, триггер SL/AutoClose) либо текущая рыночная, с единым slippage_rate
            base_price = order.price if order.price > 0 else float(self.current_prices.get(sym_u, 0.0) or 0.0)
            if base_price <= 0.0:
                return APIResponse(success=False, error_code=-1013, error_msg=f"Missing market price for {sym_u}")
            fill_price = self._apply_market_slippage(base_price, side_u)
            success, err = self._execute_fill(order, fill_price=fill_price, fill_ts=self.current_sim_time)
            if not success:
                return APIResponse(success=False, error_code=-2010, error_msg=err or "Insufficient margin")
            return APIResponse(
                success=True,
                data={"orderId": oid, "symbol": sym_u, "status": "FILLED", "executedQty": str(qty_abs), "avgPrice": str(fill_price)}
            )
        else:
            # LIMIT ордер ставится в стакан
            self.open_orders[oid] = order
            return APIResponse(
                success=True,
                data={"orderId": oid, "symbol": sym_u, "status": "NEW", "price": str(order.price), "origQty": str(qty_abs)}
            )

    @classmethod
    def _ensure_historical_klines(cls):
        SimHistoricalDataStore.ensure_loaded()

    def has_sim_hourly_bars(self, symbol: str) -> bool:
        return SimHistoricalDataStore.has_sim_hourly_bars(symbol)

    def get_precomputed_atr(self, symbol: str, timeframe: str = "1h", period: int = 14) -> Optional[float]:
        return SimHistoricalDataStore.get_precomputed_atr(
            symbol=symbol,
            cur_sim_time_ms=int(self.current_sim_time),
            timeframe=timeframe,
            period=period,
        )

    async def get_klines(self, symbol: str, interval: str, limit: int = 35) -> List[Dict[str, Any]]:
        """Возвращает исторические свечи (OHLCV) строго на виртуальное время симуляции за O(log N)."""
        sym_u = symbol.upper().strip()
        return SimHistoricalDataStore.get_klines(
            symbol=sym_u,
            interval=interval,
            cur_ts_ms=int(self.current_sim_time),
            cur_price=float(self.current_prices.get(sym_u, 0.0) or 0.0),
            limit=limit,
            dyn_bars=self._completed_hourly_bars.get(sym_u),
        )

    # ==================================================
    # MATCH ENGINE & TICK INGESTION
    # ==================================================

    def on_tick(self, symbol: str, price: float, qty: float, timestamp_ms: int) -> None:
        """
        Высокоскоростная обработка входящего тика:
        1. Обновление текущей цены и времени.
        2. Формирование закрытых часовых баров на лету.
        3. Матчинг активных лимитных ордеров (Take-Profit / DCA Limit).
        4. Контроль ликвидации кросс-маржи и пересчет пика эквити/просадки.
        """
        sym_u = symbol.upper()
        self.current_prices[sym_u] = price
        self.current_sim_time = timestamp_ms

        hour_ts = (timestamp_ms // 3600000) * 3600000
        chb = self._cur_hour_bar.get(sym_u)
        if chb is None:
            self._cur_hour_bar[sym_u] = (hour_ts, price)
        elif hour_ts > chb[0]:
            if sym_u not in self._completed_hourly_bars:
                self._completed_hourly_bars[sym_u] = []
            self._completed_hourly_bars[sym_u].append(chb)
            self._cur_hour_bar[sym_u] = (hour_ts, price)
        else:
            self._cur_hour_bar[sym_u] = (chb[0], price)

        if not self.open_orders and not self._has_active_positions:
            return

        if self.open_orders:
            for oid, order in list(self.open_orders.items()):
                if order.symbol != sym_u:
                    continue

                is_hit = (order.side == "BUY" and price <= order.price) or (order.side == "SELL" and price >= order.price)
                if is_hit:
                    success, _ = self._execute_fill(order, fill_price=order.price, fill_ts=timestamp_ms)
                    if success or order.reduce_only:
                        # При успехе филл выполнен; зависший reduce_only без позиции также снимается
                        if oid in self.open_orders:
                            del self.open_orders[oid]

        if self.liquidation_guard_enabled and self._has_active_positions:
            self.liquidation_guard.evaluate_and_liquidate(self, timestamp_ms)

        if self.open_orders or self._has_active_positions:
            cur_total_equity = self.get_total_equity()
            if cur_total_equity > self.peak_balance:
                self.peak_balance = cur_total_equity
            dd = self.peak_balance - cur_total_equity
            if dd > self.max_drawdown_usdt:
                self.max_drawdown_usdt = dd

    def _execute_fill(self, order: SimOrder, fill_price: float, fill_ts: int) -> Tuple[bool, Optional[str]]:
        pos = self._get_position(order.symbol, order.position_side)
        notional = order.qty * fill_price

        is_opening = (pos.position_side == "LONG" and order.side == "BUY") or (pos.position_side == "SHORT" and order.side == "SELL")

        if not is_opening and pos.qty <= 1e-8:
            return False, f"No open {pos.position_side} position on {pos.symbol} to reduce"

        # Лимитные ордера закрытия позиции (Take-Profit / ReduceOnly) рассчитываются по ставке Maker (0.02%),
        # рыночные ордера (Market входы, аварийный AutoCloser, срез ротации) — по ставке Taker (0.05%).
        if order.order_type == "LIMIT" and (order.reduce_only or not is_opening):
            effective_fee_rate = getattr(self, "maker_fee_rate", 0.0002)
        else:
            effective_fee_rate = self.fee_rate

        fee = notional * effective_fee_rate

        if is_opening:
            req_margin = notional / pos.leverage
            free_margin = self.get_free_margin()
            if free_margin < req_margin:
                return False, f"Margin is insufficient (required {req_margin:.2f}, free {free_margin:.2f})"

            new_qty = pos.qty + order.qty
            pos.entry_price = ((pos.entry_price * pos.qty) + notional) / new_qty
            pos.qty = new_qty
            self._has_active_positions = True
        else:
            close_qty = min(pos.qty, order.qty)
            if pos.position_side == "LONG":
                realized = (fill_price - pos.entry_price) * close_qty
            else:
                realized = (pos.entry_price - fill_price) * close_qty

            pos.realized_pnl += realized
            self.balance += realized
            pos.qty = max(0.0, pos.qty - close_qty)
            if pos.qty <= 1e-8:
                pos.qty = 0.0
                pos.entry_price = 0.0
            self._has_active_positions = any(p.qty > 1e-8 for p in self.positions.values())

        self.balance -= fee
        pos.total_fee += fee
        self.total_fees_paid += fee
        self.trade_fills_count += 1

        order.status = "FILLED"
        order.filled_price = fill_price
        order.filled_ts = fill_ts
        order.fee_paid = fee
        self.order_history.append(order)

        if self.on_fill_callback:
            try:
                self.on_fill_callback(order, fill_price, pos)
            except Exception as e:
                logger.error(f"[SimulatedBinanceClient] on_fill_callback error: {e}")

        cur_total_equity = self.get_total_equity()
        if cur_total_equity > self.peak_balance:
            self.peak_balance = cur_total_equity
        dd = self.peak_balance - cur_total_equity
        if dd > self.max_drawdown_usdt:
            self.max_drawdown_usdt = dd

        return True, None

    def _calc_unrealized_pnl(self, pos: SimPosition) -> float:
        if pos.qty <= 0 or pos.entry_price <= 0:
            return 0.0
        cur_p = self.current_prices.get(pos.symbol, pos.entry_price)
        if pos.position_side == "LONG":
            return (cur_p - pos.entry_price) * pos.qty
        else:
            return (pos.entry_price - cur_p) * pos.qty

    def get_equity(self) -> float:
        unrealized = sum(self._calc_unrealized_pnl(p) for p in self.positions.values())
        return self.balance + unrealized

    def get_total_equity(self) -> float:
        """Совокупное эквити (фьючерсный баланс + нереализованный PnL + переведенный на спот профит)."""
        return self.get_equity() + self.spot_balance

    def get_free_margin(self) -> float:
        margin_used = sum((p.qty * p.entry_price) / p.leverage for p in self.positions.values() if p.qty > 0)
        return max(0.0, self.get_equity() - margin_used)

    async def transfer_futures_to_spot(self, amount: float, asset: str = "USDT") -> APIResponse:
        """
        Эмуляция перевода средств с USDⓈ-M Futures на Spot в симуляторе бэктеста.
        Реально списывает средства с фьючерсного баланса (self.balance) и зачисляет на self.spot_balance.
        """
        amt = round(float(amount), 2)
        if amt <= 0.0:
            return APIResponse(success=False, error_code=-1, error_msg=f"Invalid transfer amount: {amount}")

        avail = round(min(self.balance, self.get_free_margin()), 2)
        if avail <= 0.0:
            return APIResponse(
                success=False,
                error_code=-3005,
                error_msg=f"Insufficient free margin on Futures for transfer (requested {amt:.2f}, available {avail:.2f})",
            )

        actual_amt = min(amt, avail)
        self.balance = round(self.balance - actual_amt, 4)
        self.spot_balance = round(self.spot_balance + actual_amt, 4)
        self.total_transferred_to_spot_usdt = round(self.total_transferred_to_spot_usdt + actual_amt, 4)

        self._next_order_id += 1
        tran_id = self._next_order_id
        logger.info(
            f"✅ [SIM_TRANSFER] UMFUTURE_MAIN: {actual_amt:.2f} {asset} "
            f"(futures_balance={self.balance:.2f}, spot_balance={self.spot_balance:.2f})"
        )
        return APIResponse(
            success=True,
            data={
                "tranId": tran_id,
                "amount": actual_amt,
                "asset": asset,
                "futures_balance": self.balance,
                "spot_balance": self.spot_balance,
            },
            meta={"simulated": True},
        )

    async def transfer_spot_to_futures(self, amount: float, asset: str = "USDT") -> APIResponse:
        """Эмуляция обратного перевода со Spot на USDⓈ-M Futures."""
        amt = round(float(amount), 2)
        if amt <= 0.0 or amt > self.spot_balance:
            return APIResponse(
                success=False,
                error_code=-1,
                error_msg=f"Insufficient spot balance (requested {amt:.2f}, spot {self.spot_balance:.2f})",
            )
        self.spot_balance = round(self.spot_balance - amt, 4)
        self.balance = round(self.balance + amt, 4)
        self._next_order_id += 1
        return APIResponse(
            success=True,
            data={
                "tranId": self._next_order_id,
                "amount": amt,
                "asset": asset,
                "futures_balance": self.balance,
                "spot_balance": self.spot_balance,
            },
            meta={"simulated": True},
        )

    def get_performance_stats(self) -> Dict[str, Any]:
        futures_equity = self.get_equity()
        total_equity = self.get_total_equity()
        realized_pnl = (self.balance + self.spot_balance) - self.initial_balance
        floating_pnl = futures_equity - self.balance
        net_profit = total_equity - self.initial_balance
        roi_pct = (net_profit / self.initial_balance) * 100.0 if self.initial_balance > 0 else 0.0
        peak_gain = max(0.0, self.peak_balance - self.initial_balance)
        max_dd = self.max_drawdown_usdt
        abs_recovery = round(peak_gain / max_dd, 2) if max_dd > 0 else 0.0
        last_recovery = round(net_profit / max_dd, 2) if max_dd > 0 else 0.0
        return {
            "initial_balance": self.initial_balance,
            "wallet_balance": round(self.balance, 2),
            "spot_balance": round(self.spot_balance, 2),
            "total_transferred_to_spot_usdt": round(self.total_transferred_to_spot_usdt, 2),
            "final_balance": round(self.balance + self.spot_balance, 2),
            "realized_pnl": round(realized_pnl, 2),
            "floating_pnl": round(floating_pnl, 2),
            "equity": round(total_equity, 2),
            "futures_equity": round(futures_equity, 2),
            "peak_balance": round(self.peak_balance, 2),
            "net_profit": round(net_profit, 2),
            "roi_pct": round(roi_pct, 2),
            "max_drawdown_usdt": round(self.max_drawdown_usdt, 2),
            "abs_recovery": abs_recovery,
            "last_recovery": last_recovery,
            "total_fees_paid": round(self.total_fees_paid, 2),
            "trade_fills_count": self.trade_fills_count,
            "is_liquidated": self.is_liquidated,
            "liquidation_details": self.liquidation_details,
        }

    def get_precomputed_trend(
        self, symbol: str, timeframe: str = "4h", sma_fast: int = 10, sma_slow: int = 30
    ) -> Optional[str]:
        """Мгновенный O(1) доступ к предрасчитанным EMA трендам через SimHistoricalDataStore."""
        return SimHistoricalDataStore.get_precomputed_trend(
            symbol=symbol,
            cur_sim_time=self.current_sim_time,
            timeframe=timeframe,
            sma_fast=sma_fast,
            sma_slow=sma_slow,
        )


