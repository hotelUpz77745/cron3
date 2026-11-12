# ==============================================================================
# Path: API/BINANCE/sim_liquidation.py
# Role: Загрузчик параметров ликвидации из CFG/backtest.json и страж кросс-маржи Binance Futures
# ==============================================================================

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from c_log import UnifiedLogger

logger = UnifiedLogger("CrossMarginLiquidationGuard")


class BacktestExecutionConfigLoader:
    """
    Единый источник истины (SSOT) для параметров исполнения и ликвидации симулятора.
    Читает cron3TradeBacktest/CFG/backtest.json напрямую через key lookup [''] без скрытых дефолтов.
    """

    _cached_exec_cfg: Optional[Dict[str, Any]] = None

    @classmethod
    def _cfg_path(cls) -> Path:
        return Path(__file__).resolve().parent.parent.parent.parent / "cron3TradeBacktest" / "CFG" / "backtest.json"

    @classmethod
    def load_execution_section(cls) -> Dict[str, Any]:
        if cls._cached_exec_cfg is not None:
            return cls._cached_exec_cfg
        cfg_path = cls._cfg_path()
        if not cfg_path.exists():
            raise FileNotFoundError(f"Missing backtest execution SSOT config: {cfg_path}")
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        cls._cached_exec_cfg = raw["execution"]
        return cls._cached_exec_cfg

    @classmethod
    def resolve_liquidation_params(
        cls,
        liquidation_guard_enabled: Optional[bool],
        maint_margin_rate: Optional[float],
    ) -> Tuple[bool, float]:
        exec_cfg = None
        if liquidation_guard_enabled is None:
            exec_cfg = cls.load_execution_section()
            enabled = bool(exec_cfg["liquidation_guard_enabled"])
        else:
            enabled = bool(liquidation_guard_enabled)

        if maint_margin_rate is None:
            if exec_cfg is None:
                exec_cfg = cls.load_execution_section()
            mmr = float(exec_cfg["maint_margin_rate"])
        else:
            mmr = float(maint_margin_rate)

        if enabled and not (0.0 < mmr < 1.0):
            raise ValueError(f"maint_margin_rate must be in (0, 1) ratio when guard enabled, got {mmr}")
        if not enabled and mmr < 0.0:
            raise ValueError(f"maint_margin_rate cannot be negative, got {mmr}")

        return enabled, mmr


class CrossMarginLiquidationGuard:
    """
    Контроль поддерживающей маржи (Maintenance Margin Ratio, CROSSED) и принудительной ликвидации.
    Общая формула используется как в SimulatedBinanceClient, так и в StrategyStreamTracker / Scanner
    для 0.00$ bit-for-bit паритета.
    """

    def __init__(self, enabled: bool, maint_margin_rate: float):
        self.enabled = bool(enabled)
        self.maint_margin_rate = float(maint_margin_rate)

    @staticmethod
    def compute_maint_margin_usdt(
        positions: Dict[Tuple[str, str], Any],
        current_prices: Dict[str, float],
        maint_margin_rate: float,
    ) -> float:
        """Вычисляет суммарную поддерживающую маржу: sum(|qty| * mark_price * mmr)."""
        if maint_margin_rate <= 0.0:
            return 0.0
        total_mm = 0.0
        for pos in positions.values():
            if pos.qty <= 1e-8:
                continue
            mark_p = current_prices.get(pos.symbol, pos.entry_price)
            if mark_p > 0.0:
                total_mm += pos.qty * mark_p * maint_margin_rate
        return total_mm

    def evaluate_and_liquidate(self, client: Any, timestamp_ms: int) -> bool:
        """
        Проверяет условие ликвидации futures_equity <= total_maint_margin.
        При пробое: снимает все ордера, принудительно закрывает все позиции по маркету,
        устанавливает client.is_liquidated = True. Возвращает True при ликвидации.
        """
        if not self.enabled or getattr(client, "is_liquidated", False):
            return False

        total_mm = self.compute_maint_margin_usdt(
            client.positions,
            client.current_prices,
            self.maint_margin_rate,
        )
        if total_mm <= 0.0:
            return False

        futures_eq = client.get_equity()
        if futures_eq > total_mm:
            return False

        # 1. Снимаем все активные лимитные ордера
        for oid, ord_obj in list(client.open_orders.items()):
            ord_obj.status = "CANCELED"
            del client.open_orders[oid]

        # 2. Рыночное закрытие всех открытых позиций по текущим марк-ценам
        from API.BINANCE.client_sim import SimOrder

        closed_positions = []
        for (sym, pos_side), pos in list(client.positions.items()):
            if pos.qty <= 1e-8:
                continue
            mark_p = client.current_prices.get(sym, pos.entry_price)
            if mark_p <= 0.0:
                mark_p = pos.entry_price
            close_side = "SELL" if pos_side == "LONG" else "BUY"
            fill_price = client._apply_market_slippage(mark_p, close_side)
            qty_before = pos.qty

            client._next_order_id += 1
            liq_order = SimOrder(
                order_id=client._next_order_id,
                symbol=sym,
                side=close_side,
                position_side=pos_side,
                order_type="MARKET",
                qty=qty_before,
                price=mark_p,
                reduce_only=True,
                created_ts=timestamp_ms,
            )
            client._execute_fill(liq_order, fill_price=fill_price, fill_ts=timestamp_ms)
            closed_positions.append({
                "symbol": sym,
                "position_side": pos_side,
                "qty": qty_before,
                "fill_price": fill_price,
            })

        client.is_liquidated = True
        client.liquidation_details = {
            "timestamp_ms": timestamp_ms,
            "futures_equity_before": round(futures_eq, 4),
            "maint_margin_usdt": round(total_mm, 4),
            "maint_margin_rate": self.maint_margin_rate,
            "wallet_after": round(client.balance, 4),
            "closed_positions": closed_positions,
        }
        logger.critical(
            f"[SIM_LIQUIDATION] Кросс-маржа ликвидирована! eq={futures_eq:.2f} <= MM={total_mm:.2f}, "
            f"wallet_after={client.balance:.2f}, positions={len(closed_positions)}"
        )
        return True
