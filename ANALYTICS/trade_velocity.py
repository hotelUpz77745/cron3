# ============================================================
# FILE: ANALYTICS/trade_velocity.py
# ROLE: Trade velocity and market activity volatility tracker
# ============================================================

from __future__ import annotations

import os
import csv
import time
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any

from consts import (
    DATA_DIR,
    ANALYTICS_DIR,
    ANALYTICS_STATS_DIR,
    ANALYTICS_LEDGERS_DIR,
    TRADE_VELOCITY_ENABLED,
    TRADE_VELOCITY_TIMEFRAME_HOURS,
    TRADE_VELOCITY_MIN_ACCUMULATION_HOURS,
    TRADE_VELOCITY_BENCHMARK_TRADES,
    TRADE_VELOCITY_TOP_SLICE,
    _CFG,
)
from c_log import UnifiedLogger
from c_utils import Utils
from API.BINANCE.public import is_valid_symbol

logger = UnifiedLogger("TradeVelocity")


class TradeVelocityEngine:
    """
    Движок для расчета средней скорости сделок (Trade Velocity)
    и коэффициента активности (Ratio) относительно эталона
    с поддержкой мультивселенных (изолированная лента и персональные overrides).
    """

    def __init__(self, universe_id: str = "default", ledger_path: Optional[Path] = None):
        self.universe_id: str = universe_id
        if ledger_path:
            self.ledger_path: Path = Path(ledger_path)
        elif universe_id == "default":
            self.ledger_path: Path = ANALYTICS_LEDGERS_DIR / "trades_ledger.txt"
        else:
            self.ledger_path: Path = ANALYTICS_LEDGERS_DIR / f"trades_ledger_{universe_id}.txt"

        self.enabled: bool = TRADE_VELOCITY_ENABLED
        self.timeframe_hours: float = TRADE_VELOCITY_TIMEFRAME_HOURS
        self.min_accumulation_hours: float = TRADE_VELOCITY_MIN_ACCUMULATION_HOURS
        self.benchmark_trades: float = TRADE_VELOCITY_BENCHMARK_TRADES
        self.top_slice: Optional[int] = TRADE_VELOCITY_TOP_SLICE
        self.low_ratio_threshold: float = 0.25
        self.high_ratio_threshold: float = 1.0
        self.start_ts_ms: int = int(time.time() * 1000)
        self._trades_history: List[Tuple[int, str]] = []  # List of (timestamp_ms, symbol)
        self.earliest_trade_ts_ms: Optional[int] = None
        self.latest_trade_ts_ms: Optional[int] = None
        self._reload_config()
        self._init_history_from_ledger()

    def _init_history_from_ledger(self) -> None:
        """
        Подгружает сделки из изолированной ленты сделок вселенной
        в пределах timeframe_hours с точной привязкой к меткам времени закрытия.
        """
        # Подбираем актуальный леджер: если изолированный леджер вселенной пуст, читаем общий леджер
        eff_ledger_path = self.ledger_path
        if not eff_ledger_path.exists() or eff_ledger_path.stat().st_size <= 60:
            general_ledger = ANALYTICS_LEDGERS_DIR / "trades_ledger.txt"
            if general_ledger.exists() and general_ledger.stat().st_size > 60:
                eff_ledger_path = general_ledger

        if not eff_ledger_path.exists():
            return

        window_ms = int(self.timeframe_hours * 3600 * 1000)
        cutoff_ms = int(time.time() * 1000) - window_ms

        new_trades: List[Tuple[int, str]] = []
        try:
            with open(eff_ledger_path, mode="r", encoding="utf-8") as f:
                reader = csv.reader(f, delimiter=';')
                header = next(reader, None)
                if not header:
                    return

                header_lower = [h.strip().lower() for h in header]
                sym_idx = header_lower.index("symbol") if "symbol" in header_lower else (1 if "id" in header_lower else 0)
                close_idx = header_lower.index("close time") if "close time" in header_lower else (4 if "id" in header_lower else 3)

                for row in reader:
                    if len(row) <= max(sym_idx, close_idx):
                        continue
                    
                    symbol = row[sym_idx].strip()
                    close_time_str = row[close_idx].strip()

                    try:
                        dt = datetime.strptime(close_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                        ts_ms = int(dt.timestamp() * 1000)
                    except Exception:
                        continue

                    if ts_ms >= cutoff_ms:
                        new_trades.append((ts_ms, symbol))

            self._trades_history = new_trades
            if self._trades_history:
                self._trades_history.sort(key=lambda x: x[0])
                self.earliest_trade_ts_ms = self._trades_history[0][0]
                self.latest_trade_ts_ms = self._trades_history[-1][0]
            else:
                self.earliest_trade_ts_ms = None
                self.latest_trade_ts_ms = None

        except Exception as e:
            logger.error(f"[TRADE_VELOCITY:{self.universe_id}] Error loading trades ledger: {e}")

    def on_trade_closed(self, symbol: str, timestamp_ms: Optional[int] = None) -> None:
        """
        Коллбэк при закрытии сделки: инкрементирует счетчик и сдвигает скользящее окно.
        """
        ts = timestamp_ms or int(time.time() * 1000)
        self._trades_history.append((ts, symbol))
        self._purge_old_trades(ts)

    def _purge_old_trades(self, current_ts_ms: int) -> None:
        """Очищает сделки за пределами timeframe_hours."""
        window_ms = int(self.timeframe_hours * 3600 * 1000)
        cutoff_ms = current_ts_ms - window_ms
        self._trades_history = [(t, s) for t, s in self._trades_history if t >= cutoff_ms]
        if self._trades_history:
            self._trades_history.sort(key=lambda x: x[0])
            self.earliest_trade_ts_ms = self._trades_history[0][0]
            self.latest_trade_ts_ms = self._trades_history[-1][0]
        else:
            self.earliest_trade_ts_ms = None
            self.latest_trade_ts_ms = None

    def _reload_config(self) -> None:
        """Перечитывает конфигурацию из CFG/app.json."""
        try:
            cfg_path = DATA_DIR / "app.json"
            if cfg_path.exists():
                cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
                if "trade_velocity" in cfg_data:
                    tv_cfg = cfg_data["trade_velocity"]
                    self.enabled = bool(tv_cfg.get("enabled", self.enabled))
                    self.timeframe_hours = float(tv_cfg.get("timeframe_hours", self.timeframe_hours))
                    self.min_accumulation_hours = float(tv_cfg.get("min_accumulation_hours", self.min_accumulation_hours))
                    self.benchmark_trades = float(tv_cfg.get("benchmark_trades_per_coin", self.benchmark_trades))
                    self.top_slice = tv_cfg.get("top_coins_slice", self.top_slice)
        except Exception as e:
            logger.error(f"[TRADE_VELOCITY:{self.universe_id}] Error reloading config: {e}")

    def calculate_velocity(self, analytics_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Рассчитывает скорость сделок по выбранному срезу, коэффициент активности (Ratio),
        а также детализацию скорости по каждой отдельной монете.
        """
        if analytics_data is None:
            analytics_data = {}
        now_sec = time.time()
        if not hasattr(self, '_last_cfg_reload') or (now_sec - self._last_cfg_reload >= 60.0):
            self._last_cfg_reload = now_sec
            self._reload_config()
        if not hasattr(self, '_last_ledger_sync') or (now_sec - self._last_ledger_sync >= 10.0) or not self._trades_history:
            self._last_ledger_sync = now_sec
            self._init_history_from_ledger()
        now_ms = int(now_sec * 1000)
        self._purge_old_trades(now_ms)

        window_sec = self.timeframe_hours * 3600.0
        min_acc_sec = self.min_accumulation_hours * 3600.0

        history_span_sec = max(0.0, (now_ms - self.start_ts_ms) / 1000.0)
        if self._trades_history and self.earliest_trade_ts_ms:
            elapsed_from_oldest_sec = max(0.0, (now_ms - self.earliest_trade_ts_ms) / 1000.0)
            history_span_sec = max(history_span_sec, elapsed_from_oldest_sec)

        accumulation_hours = round(history_span_sec / 3600.0, 2)
        is_ready = history_span_sec >= min_acc_sec

        per_coin_data = analytics_data.get("per_coin", {})
        configured_symbols: List[str] = []
        app_path = DATA_DIR / "app.json"
        if app_path.exists():
            try:
                ad = json.loads(app_path.read_text(encoding="utf-8"))
                act_strats = ad.get("active_strategies", {})
                if self.universe_id != "default" and self.universe_id in act_strats:
                    scfg = act_strats[self.universe_id]
                    if isinstance(scfg, dict) and scfg.get("enabled", True):
                        configured_symbols = list(Utils.extract_strategy_symbols(scfg))
                else:
                    for sid, scfg in act_strats.items():
                        if sid.startswith("_") or not isinstance(scfg, dict):
                            continue
                        if scfg.get("enabled") is True:
                            configured_symbols.extend(Utils.extract_strategy_symbols(scfg))
            except Exception:
                pass

        if not configured_symbols and self.universe_id != "default":
            if "symbols" in analytics_data:
                configured_symbols = list(analytics_data["symbols"])
            elif per_coin_data:
                configured_symbols = list(per_coin_data.keys())

        if not configured_symbols:
            try:
                from consts import ACTIVE_SYMBOLS
                configured_symbols = list(ACTIVE_SYMBOLS)
            except Exception:
                pass

        if configured_symbols:
            all_symbols = [s for s in dict.fromkeys(configured_symbols) if is_valid_symbol(s)]
        else:
            # Железобетонная защита: если конфиг недоступен/пуст, берем ТОЛЬКО монеты
            # с реальными сделками в текущем активном окне ленты. Мертвый исторический
            # хвост с нулями из per_coin_data ни при каких условиях не попадет в срез!
            recent_trade_symbols = {sym for _, sym in self._trades_history}
            all_symbols = [s for s in recent_trade_symbols if is_valid_symbol(s)]

        if self.top_slice and self.top_slice > 0:
            sorted_by_profit = sorted(
                all_symbols,
                key=lambda s: float(per_coin_data.get(s, {}).get("net_profit_usdt", 0.0)),
                reverse=True
            )
            slice_symbols = sorted_by_profit[:self.top_slice]
            slice_desc = f"Топ {len(slice_symbols)} по Net Profit"
        else:
            slice_symbols = all_symbols
            slice_desc = f"Все монеты ({len(slice_symbols)} шт.)"

        coin_count = len(slice_symbols)
        slice_symbols_set = set(slice_symbols)

        slice_total_trades = sum(1 for _, sym in self._trades_history if sym in slice_symbols_set)
        benchmark = self.benchmark_trades

        # Подсчет сделок по каждой монете
        coin_trades_map: Dict[str, int] = {}
        for _, sym in self._trades_history:
            coin_trades_map[sym] = coin_trades_map.get(sym, 0) + 1

        effective_elapsed_sec = min(window_sec, history_span_sec)
        effective_elapsed_hours = effective_elapsed_sec / 3600.0
        is_interpolated = effective_elapsed_hours < self.timeframe_hours
        interpolation_factor = (self.timeframe_hours / effective_elapsed_hours) if (is_interpolated and effective_elapsed_hours > 0) else 1.0

        # Помонетная скорость
        per_coin_metrics: Dict[str, Any] = {}
        for sym in all_symbols:
            c_trades = coin_trades_map.get(sym, 0)
            c_eff_speed = round(c_trades * interpolation_factor, 2)
            c_ratio = round(c_eff_speed / benchmark, 2) if benchmark > 0 else 1.0
            if c_ratio <= self.low_ratio_threshold:
                c_status = "НЕДОГРЕВ"
            elif c_ratio >= self.high_ratio_threshold:
                c_status = "ВЫСОКАЯ ВОЛАТИЛЬНОСТЬ"
            else:
                c_status = "НОРМА"
            per_coin_metrics[sym] = {
                "trades": c_trades,
                "effective_speed": c_eff_speed,
                "speed_ratio": c_ratio,
                "status_desc": c_status
            }

        if not is_ready:
            status = "ACCUMULATING"
            status_desc = f"Накопление ({accumulation_hours}ч / {self.min_accumulation_hours}ч)"
            return {
                "universe_id": self.universe_id,
                "timeframe_hours": self.timeframe_hours,
                "min_accumulation_hours": self.min_accumulation_hours,
                "accumulation_hours": accumulation_hours,
                "is_ready_for_alerts": False,
                "is_interpolated": False,
                "interpolation_factor": 1.0,
                "benchmark_speed": benchmark,
                "slice_description": slice_desc,
                "slice_coins_count": coin_count,
                "slice_trades_count": slice_total_trades,
                "avg_actual_speed": None,
                "avg_effective_speed": None,
                "speed_ratio": None,
                "deviation_pct": None,
                "status": status,
                "status_desc": status_desc,
                "per_coin": per_coin_metrics,
            }

        avg_actual_speed = (slice_total_trades / coin_count) if coin_count > 0 else 0.0
        avg_effective_speed = round(avg_actual_speed * interpolation_factor, 2)

        if benchmark > 0:
            speed_ratio = round(avg_effective_speed / benchmark, 4)
            deviation_pct = round(((avg_effective_speed - benchmark) / benchmark) * 100.0, 2)
        else:
            speed_ratio = 1.0
            deviation_pct = 0.0

        if speed_ratio <= self.low_ratio_threshold:
            status = "LOW_VOLATILITY"
            status_desc = "НЕДОГРЕВ"
        elif speed_ratio >= self.high_ratio_threshold:
            status = "HIGH_VOLATILITY"
            status_desc = "ВЫСОКАЯ ВОЛАТИЛЬНОСТЬ"
        else:
            status = "NORMAL"
            status_desc = "НОРМА"

        return {
            "universe_id": self.universe_id,
            "timeframe_hours": self.timeframe_hours,
            "min_accumulation_hours": self.min_accumulation_hours,
            "accumulation_hours": accumulation_hours,
            "is_ready_for_alerts": True,
            "is_interpolated": is_interpolated,
            "interpolation_factor": round(interpolation_factor, 2),
            "benchmark_speed": benchmark,
            "slice_description": slice_desc,
            "slice_coins_count": coin_count,
            "slice_trades_count": slice_total_trades,
            "avg_actual_speed": round(avg_actual_speed, 2),
            "avg_effective_speed": avg_effective_speed,
            "speed_ratio": speed_ratio,
            "deviation_pct": deviation_pct,
            "status": status,
            "status_desc": status_desc,
            "per_coin": per_coin_metrics,
        }

    def format_report_block(self, metrics: Dict[str, Any]) -> str:
        """Форматирует табличный блок для отчета аналитики."""
        u_label = f"[{self.universe_id}] " if self.universe_id != "default" else ""
        if not metrics.get("is_ready_for_alerts", False) or metrics.get("avg_effective_speed") is None:
            acc_info = f"{metrics['accumulation_hours']}h / {metrics['min_accumulation_hours']}h (Accumulating)"
            speed_str = f"N/A (Накопление: {metrics['accumulation_hours']}h / {metrics['min_accumulation_hours']}h)"
            ratio_str = "N/A"
        else:
            acc_info = (
                f"{metrics['accumulation_hours']}h / {metrics['timeframe_hours']}h (Interpolated x{metrics['interpolation_factor']})"
                if metrics["is_interpolated"] else
                f"{metrics['accumulation_hours']}h / {metrics['timeframe_hours']}h (Full Window)"
            )
            speed_str = f"{metrics['avg_effective_speed']} trades / coin"
            ratio_str = f"{metrics['speed_ratio']:.2f}x"

        lines = [
            "=" * 50,
            f"{u_label}TRADE VELOCITY & VOLATILITY (Window: {metrics['timeframe_hours']}h)",
            "=" * 50,
            f"  Evaluated Slice:       {metrics['slice_description']}",
            f"  Benchmark Speed:       {metrics['benchmark_speed']} trades / coin",
            f"  Window Accumulation:   {acc_info}",
            f"  Slice Total Trades:    {metrics['slice_trades_count']}",
            f"  Average Current Speed: {speed_str}",
            f"  Activity Ratio (R):    {ratio_str}",
            f"  Volatility Status:     {metrics['status_desc']}\n"
        ]
        return "\n".join(lines)
