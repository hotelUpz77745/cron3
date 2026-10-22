# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\API\BINANCE\client_sim.py
# Role: client_sim.py module

# ==============================================================================
# Path: API/BINANCE/client_sim.py
# Role: Высокоточный симулированный клиент Binance Futures (In-Memory Match Engine для бэктестов)
# ==============================================================================

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

from c_log import UnifiedLogger
from API.BINANCE.validator import APIResponse

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
        leverage: int = 20
    ):
        self.initial_balance = float(start_balance_usdt)
        self.balance = float(start_balance_usdt)
        self.fee_rate = float(fee_rate)
        self.default_leverage = int(leverage)

        self._next_order_id = 100000
        self.positions: Dict[Tuple[str, str], SimPosition] = {}
        self.open_orders: Dict[int, SimOrder] = {}
        self.order_history: List[SimOrder] = []
        self.current_prices: Dict[str, float] = {}
        self.current_sim_time: int = 0

        # Статистика портфеля
        self.total_fees_paid: float = 0.0
        self.peak_balance: float = float(start_balance_usdt)
        self.max_drawdown_usdt: float = 0.0
        self.trade_fills_count: int = 0
        self.on_fill_callback: Optional[Any] = None

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

    async def make_order(
        self,
        symbol: str,
        qty: float,
        side: str,
        position_side: str,
        market_type: str = "MARKET",
        price: Optional[float] = None,
        reduce_only: bool = False,
        concurrent_mode: bool = False
    ) -> APIResponse:
        sym_u = symbol.upper()
        side_u = side.upper()
        pos_side_u = position_side.upper()
        qty_abs = abs(float(qty))

        if qty_abs <= 0:
            return APIResponse(success=False, error_code=-1013, error_msg="Filter failure: LOT_SIZE qty <= 0")

        self._next_order_id += 1
        oid = self._next_order_id

        order = SimOrder(
            order_id=oid,
            symbol=sym_u,
            side=side_u,
            position_side=pos_side_u,
            order_type=market_type.upper(),
            qty=qty_abs,
            price=float(price or 0.0),
            reduce_only=reduce_only,
            created_ts=self.current_sim_time,
        )

        if market_type.upper() == "MARKET":
            # Исполнение немедленно по текущей рыночной цене
            cur_price = self.current_prices.get(sym_u, 0.0)
            if cur_price <= 0:
                cur_price = order.price if order.price > 0 else 100.0

            success, err = self._execute_fill(order, fill_price=cur_price, fill_ts=self.current_sim_time)
            if not success:
                return APIResponse(success=False, error_code=-2010, error_msg=err or "Insufficient margin")

            return APIResponse(
                success=True,
                data={
                    "orderId": oid,
                    "symbol": sym_u,
                    "status": "FILLED",
                    "executedQty": str(qty_abs),
                    "avgPrice": str(cur_price),
                }
            )
        else:
            # LIMIT ордер ставится в стакан
            self.open_orders[oid] = order
            return APIResponse(
                success=True,
                data={
                    "orderId": oid,
                    "symbol": sym_u,
                    "status": "NEW",
                    "price": str(order.price),
                    "origQty": str(qty_abs),
                }
            )

    _historical_klines_loaded = False
    _warmup_1h: Dict[str, List[float]] = {}
    _warmup_4h: Dict[str, List[float]] = {}
    _warmup_1d: Dict[str, List[float]] = {}
    _sim_1h: Dict[str, List[Dict[str, Any]]] = {}
    _precomputed_trends: Dict[str, str] = {}
    _precomputed_atr: Dict[str, float] = {}

    @classmethod
    def _ensure_historical_klines(cls):
        if cls._historical_klines_loaded:
            return
        cls._historical_klines_loaded = True
        backtest_data = Path(__file__).resolve().parent.parent.parent.parent / "cron3TradeBacktest" / "DATA"
        for name, target in [
            ("warmup_klines_1h.json", "_warmup_1h"),
            ("warmup_klines_4h.json", "_warmup_4h"),
            ("warmup_4h.json", "_warmup_4h"),
            ("warmup_klines_1d.json", "_warmup_1d"),
            ("warmup_1d.json", "_warmup_1d"),
            ("sim_klines_1h.json", "_sim_1h"),
            ("precomputed_ema_trends.json", "_precomputed_trends"),
            ("precomputed_atr.json", "_precomputed_atr"),
        ]:
            p = backtest_data / name
            if p.exists():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        setattr(cls, target, json.load(f))
                except Exception as e:
                    logger.warning(f"[SimulatedBinanceClient] Error loading {name}: {e}")

    def get_precomputed_atr(self, symbol: str, timeframe: str = "1h", period: int = 14) -> Optional[float]:
        self._ensure_historical_klines()
        if not self._precomputed_atr:
            return None
        cur_ts = int(self.current_sim_time)
        if not cur_ts:
            return None
        tf = str(timeframe).lower().strip()
        bucket_ts = (cur_ts // 3600000) * 3600000
        sym_u = symbol.upper().strip()
        key_p = f"{sym_u}|{tf}_{period}|{bucket_ts}"
        if key_p in self._precomputed_atr:
            return float(self._precomputed_atr[key_p])
        key_s = f"{sym_u}|{tf}|{bucket_ts}"
        val = self._precomputed_atr.get(key_s)
        return float(val) if val is not None else None

    def get_precomputed_trend(self, symbol: str, timeframe: str, sma_fast: int = 10, sma_slow: int = 30) -> Optional[str]:
        self._ensure_historical_klines()
        if not self._precomputed_trends:
            return None
        cur_ts = int(self.current_sim_time)
        if not cur_ts:
            return None
        tf = str(timeframe).lower().strip()
        if "4h" in tf:
            bucket_ts = (cur_ts // 14400000) * 14400000
        elif "1d" in tf or tf == "d":
            bucket_ts = (cur_ts // 86400000) * 86400000
        else:
            bucket_ts = (cur_ts // 3600000) * 3600000
        if sma_fast != 10 or sma_slow != 30:
            key_custom = f"{symbol.upper().strip()}|{tf}_{sma_fast}_{sma_slow}|{bucket_ts}"
            if key_custom in self._precomputed_trends:
                return self._precomputed_trends[key_custom]
        key = f"{symbol.upper().strip()}|{tf}|{bucket_ts}"
        return self._precomputed_trends.get(key)

    async def get_klines(self, symbol: str, interval: str, limit: int = 35) -> List[Dict[str, Any]]:
        """
        Возвращает исторические свечи (OHLCV) строго на виртуальное время симуляции.
        Исключает заглядывание в будущее и обеспечивает 100% паритет с боевым ботом.
        """
        self._ensure_historical_klines()
        sym_u = symbol.upper().strip()
        cur_ts = int(self.current_sim_time)
        cur_p = self.current_prices.get(sym_u, 0.0)

        # 1. Свечи, сформированные на лету из входящего потока тиков
        dyn_bars = getattr(self, "_completed_hourly_bars", {}).get(sym_u, [])
        # 2. Свечи из precomputed файла (если доступны)
        file_bars = [
            (int(b.get("open_time", 0)), float(b["close"]))
            for b in self._sim_1h.get(sym_u, [])
            if int(b.get("open_time", 0)) + 3600000 <= cur_ts
        ] if self._sim_1h.get(sym_u) else []
        
        sim_bars = file_bars if file_bars else dyn_bars

        interval_str = str(interval).lower().strip()
        if interval_str == "1h":
            warm = list(self._warmup_1h.get(sym_u, []))
            closes = warm + [b[1] for b in sim_bars]
            if cur_p > 0:
                closes.append(cur_p)
            return [{"close": float(c)} for c in closes[-limit:]]

        elif interval_str == "4h":
            warm = list(self._warmup_4h.get(sym_u, []))
            sim_4h = [b[1] for b in sim_bars if (b[0] + 3600000) % 14400000 == 0]
            closes = warm + sim_4h
            if cur_p > 0:
                closes.append(cur_p)
            return [{"close": float(c)} for c in closes[-limit:]]

        elif interval_str in ("1d", "d"):
            warm = list(self._warmup_1d.get(sym_u, []))
            sim_1d = [b[1] for b in sim_bars if (b[0] + 3600000) % 86400000 == 0]
            closes = warm + sim_1d
            if cur_p > 0:
                closes.append(cur_p)
            return [{"close": float(c)} for c in closes[-limit:]]

        return []

    # ==================================================
    # MATCH ENGINE & TICK INGESTION
    # ==================================================

    def on_tick(self, symbol: str, price: float, qty: float, timestamp_ms: int) -> None:
        """
        Высокоскоростная обработка входящего тика:
        1. Обновление текущей цены и времени.
        2. Формирование закрытых часовых баров на лету.
        3. Матчинг активных лимитных ордеров (Take-Profit / DCA Limit).
        4. Обновление плавающей прибыли и просадки.
        """
        sym_u = symbol.upper()
        self.current_prices[sym_u] = price
        self.current_sim_time = timestamp_ms

        # Формирование часовых баров
        if not hasattr(self, "_completed_hourly_bars"):
            self._completed_hourly_bars = {}
            self._cur_hour_bar = {}
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

        if not self.open_orders:
            return

        for oid, order in list(self.open_orders.items()):
            if order.symbol != sym_u:
                continue

            # Проверка условий исполнения лимитного ордера
            is_hit = False
            if order.side == "BUY" and price <= order.price:
                is_hit = True
            elif order.side == "SELL" and price >= order.price:
                is_hit = True

            if is_hit:
                # Лимитка коснулась цены -> исполнение
                success, _ = self._execute_fill(order, fill_price=order.price, fill_ts=timestamp_ms)
                if success:
                    del self.open_orders[oid]

        # Непрерывный мониторинг пика эквити и максимальной просадки
        cur_equity = self.get_equity()
        if cur_equity > self.peak_balance:
            self.peak_balance = cur_equity
        dd = self.peak_balance - cur_equity
        if dd > self.max_drawdown_usdt:
            self.max_drawdown_usdt = dd

    def _execute_fill(self, order: SimOrder, fill_price: float, fill_ts: int) -> Tuple[bool, Optional[str]]:
        pos = self._get_position(order.symbol, order.position_side)
        notional = order.qty * fill_price
        fee = notional * self.fee_rate

        is_opening = (pos.position_side == "LONG" and order.side == "BUY") or (pos.position_side == "SHORT" and order.side == "SELL")

        if is_opening:
            req_margin = notional / pos.leverage
            # Проверка доступной маржи
            free_margin = self.get_free_margin()
            if free_margin < req_margin:
                return False, f"Margin is insufficient (required {req_margin:.2f}, free {free_margin:.2f})"

            # Добор позиции
            new_qty = pos.qty + order.qty
            pos.entry_price = ((pos.entry_price * pos.qty) + notional) / new_qty
            pos.qty = new_qty
        else:
            # Закрытие или частичное закрытие
            close_qty = min(pos.qty, order.qty)
            if close_qty > 0:
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

        # Списание комиссии
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

        # Обновление пика баланса и просадки
        cur_equity = self.get_equity()
        if cur_equity > self.peak_balance:
            self.peak_balance = cur_equity
        dd = self.peak_balance - cur_equity
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

    def get_free_margin(self) -> float:
        margin_used = sum((p.qty * p.entry_price) / p.leverage for p in self.positions.values() if p.qty > 0)
        return max(0.0, self.get_equity() - margin_used)

    def get_performance_stats(self) -> Dict[str, Any]:
        equity = self.get_equity()
        realized_pnl = self.balance - self.initial_balance
        floating_pnl = equity - self.balance
        net_profit = equity - self.initial_balance
        roi_pct = (net_profit / self.initial_balance) * 100.0 if self.initial_balance > 0 else 0.0
        peak_gain = max(0.0, self.peak_balance - self.initial_balance)
        max_dd = self.max_drawdown_usdt
        abs_recovery = round(peak_gain / max_dd, 2) if max_dd > 0 else 0.0
        last_recovery = round(net_profit / max_dd, 2) if max_dd > 0 else 0.0
        return {
            "initial_balance": self.initial_balance,
            "wallet_balance": round(self.balance, 2),
            "final_balance": round(self.balance, 2),
            "realized_pnl": round(realized_pnl, 2),
            "floating_pnl": round(floating_pnl, 2),
            "equity": round(equity, 2),
            "peak_balance": round(self.peak_balance, 2),
            "net_profit": round(net_profit, 2),
            "roi_pct": round(roi_pct, 2),
            "max_drawdown_usdt": round(self.max_drawdown_usdt, 2),
            "abs_recovery": abs_recovery,
            "last_recovery": last_recovery,
            "total_fees_paid": round(self.total_fees_paid, 2),
            "trade_fills_count": self.trade_fills_count,
        }

