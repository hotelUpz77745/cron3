# ============================================================
# FILE: ANALYTICS/trade_velocity.py
# ROLE: Trade velocity and market activity volatility tracker
# ============================================================

from __future__ import annotations

import os
import csv
import time
import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any

import aiohttp
from consts import (
    DATA_DIR,
    ANALYTICS_DIR,
    ANALYTICS_STATS_DIR,
    ANALYTICS_LEDGERS_DIR,
    TG_ENABLED,
    TG_ALLOWED_USERS,
    TRADE_VELOCITY_ENABLED,
    TRADE_VELOCITY_TIMEFRAME_HOURS,
    TRADE_VELOCITY_MIN_ACCUMULATION_HOURS,
    TRADE_VELOCITY_BENCHMARK_TRADES,
    TRADE_VELOCITY_TOP_SLICE,
    TRADE_VELOCITY_REPORT_INTERVAL_HOURS,
    TRADE_VELOCITY_NOTIF_ENABLED,
    TRADE_VELOCITY_LOW_RATIO,
    TRADE_VELOCITY_LOW_RECOVERY_RATIO,
    TRADE_VELOCITY_HIGH_RATIO,
    TRADE_VELOCITY_HIGH_RESET_RATIO,
    TRADE_VELOCITY_NOTIF_NEG_FLAG,
    TRADE_VELOCITY_NOTIF_POS_FLAG,
    _CFG,
)
from c_log import UnifiedLogger
from c_utils import Utils

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
        self.low_ratio_threshold: float = TRADE_VELOCITY_LOW_RATIO
        self.low_recovery_ratio: float = TRADE_VELOCITY_LOW_RECOVERY_RATIO
        self.high_ratio_threshold: float = TRADE_VELOCITY_HIGH_RATIO
        self.high_reset_ratio: float = TRADE_VELOCITY_HIGH_RESET_RATIO
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
        if not self.ledger_path.exists():
            return

        window_ms = int(self.timeframe_hours * 3600 * 1000)
        cutoff_ms = int(time.time() * 1000) - window_ms

        new_trades: List[Tuple[int, str]] = []
        try:
            with open(self.ledger_path, mode="r", encoding="utf-8") as f:
                reader = csv.reader(f, delimiter=';')
                header = next(reader, None)
                if not header:
                    return

                for row in reader:
                    if len(row) < 4:
                        continue
                    
                    sym: Optional[str] = None
                    close_ts_ms: Optional[int] = None
                    
                    dt_candidates: List[int] = []
                    for col in row:
                        val = col.strip()
                        if not sym and val.endswith("USDT"):
                            sym = val
                        elif len(val) == 19 and val[4] == '-' and val[7] == '-':
                            try:
                                dt = datetime.strptime(val, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                                dt_candidates.append(int(dt.timestamp() * 1000))
                            except Exception:
                                pass

                    if dt_candidates:
                        close_ts_ms = dt_candidates[-1]

                    if sym and close_ts_ms is not None:
                        if close_ts_ms >= cutoff_ms:
                            new_trades.append((close_ts_ms, sym))

            new_trades.sort(key=lambda x: x[0])
            self._trades_history = new_trades
            if self._trades_history:
                self.earliest_trade_ts_ms = self._trades_history[0][0]
                self.latest_trade_ts_ms = self._trades_history[-1][0]
                logger.info(
                    f"[TRADE_VELOCITY:{self.universe_id}] Loaded {len(self._trades_history)} trades from {self.ledger_path.name}. "
                    f"Earliest: {datetime.fromtimestamp(self.earliest_trade_ts_ms / 1000.0, tz=timezone.utc)}, "
                    f"Latest: {datetime.fromtimestamp(self.latest_trade_ts_ms / 1000.0, tz=timezone.utc)}"
                )
            else:
                self.earliest_trade_ts_ms = None
                self.latest_trade_ts_ms = None
        except Exception as e:
            logger.error(f"[TRADE_VELOCITY:{self.universe_id}] Error reading ledger for history: {e}")

    def on_trade_closed(self, symbol: str, ts_ms: Optional[int] = None) -> None:
        """Регистрирует факт закрытия сделки."""
        current_ms = ts_ms if ts_ms is not None else int(time.time() * 1000)
        self._trades_history.append((current_ms, symbol))
        self._purge_old_trades(current_ms)

    def _purge_old_trades(self, now_ms: int) -> None:
        """Удаляет сделки старше скользящего окна timeframe_hours."""
        window_ms = int(self.timeframe_hours * 3600 * 1000)
        cutoff_ms = now_ms - window_ms
        self._trades_history = [item for item in self._trades_history if item[0] >= cutoff_ms]
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
                    n_cfg = tv_cfg.get("notifications", {})
                    self.low_ratio_threshold = float(n_cfg.get("low_ratio_threshold", self.low_ratio_threshold))
                    self.low_recovery_ratio = float(n_cfg.get("low_recovery_ratio", self.low_recovery_ratio))
                    self.high_ratio_threshold = float(n_cfg.get("high_ratio_threshold", self.high_ratio_threshold))
                    self.high_reset_ratio = float(n_cfg.get("high_reset_ratio", self.high_reset_ratio))
        except Exception as e:
            logger.error(f"[TRADE_VELOCITY:{self.universe_id}] Error reloading config: {e}")

    def calculate_velocity(self, analytics_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Рассчитывает скорость сделок по выбранному срезу, коэффициент активности (Ratio),
        а также детализацию скорости по каждой отдельной монете.
        """
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
        is_ready_for_alerts = history_span_sec >= min_acc_sec

        per_coin_data = analytics_data.get("per_coin", {})
        configured_symbols = []
        app_path = DATA_DIR / "app.json"
        if app_path.exists():
            try:
                ad = json.loads(app_path.read_text(encoding="utf-8"))
                for sid, scfg in ad.get("active_strategies", {}).items():
                    if scfg.get("enabled", True):
                        configured_symbols.extend(scfg.get("symbols", []))
            except Exception:
                pass

        all_symbols = configured_symbols if configured_symbols else list(per_coin_data.keys())
        for s in per_coin_data:
            if s not in all_symbols:
                all_symbols.append(s)

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
                c_status = "ВОЛАТИЛЬНОСТЬ ЗАШИБИСЬ"
            else:
                c_status = "НОРМА"
            per_coin_metrics[sym] = {
                "trades": c_trades,
                "effective_speed": c_eff_speed,
                "speed_ratio": c_ratio,
                "status_desc": c_status
            }

        if not is_ready_for_alerts:
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
                "low_ratio_threshold": self.low_ratio_threshold,
                "low_recovery_ratio": self.low_recovery_ratio,
                "high_ratio_threshold": self.high_ratio_threshold,
                "high_reset_ratio": self.high_reset_ratio,
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
            status_desc = "ВОЛАТИЛЬНОСТЬ ЗАШИБИСЬ"
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
            "low_ratio_threshold": self.low_ratio_threshold,
            "low_recovery_ratio": self.low_recovery_ratio,
            "high_ratio_threshold": self.high_ratio_threshold,
            "high_reset_ratio": self.high_reset_ratio,
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
            f"    - Low Ratio Thresh:  <{metrics['low_ratio_threshold']}",
            f"    - Low Recovery:      >={metrics['low_recovery_ratio']}",
            f"    - High Ratio Thresh: >={metrics['high_ratio_threshold']}",
            f"    - High Reset:        <={metrics['high_reset_ratio']}",
            f"  Volatility Status:     {metrics['status_desc']}\n"
        ]
        return "\n".join(lines)


class TradeVelocityNotifier:
    """Менеджер отчетов и пороговых уведомлений в Telegram для вселенной."""

    def __init__(self, engine: TradeVelocityEngine, universe_id: str = "default"):
        self.engine: TradeVelocityEngine = engine
        self.universe_id: str = universe_id
        self.is_running: bool = False
        self._task: Optional[asyncio.Task] = None
        self.low_vol_flag: bool = TRADE_VELOCITY_NOTIF_NEG_FLAG
        self.high_vol_flag: bool = TRADE_VELOCITY_NOTIF_POS_FLAG
        self._sync_flags_from_disk()
        self.last_periodic_report_ts: float = time.time()
        self.token: str = os.getenv("TG_TOKEN") or ""

    def _sync_flags_from_disk(self) -> None:
        try:
            cfg_path = DATA_DIR / "app.json"
            if cfg_path.exists():
                cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
                if "trade_velocity" in cfg_data and "notifications" in cfg_data["trade_velocity"]:
                    n_cfg = cfg_data["trade_velocity"]["notifications"]
                    disk_neg = bool(n_cfg.get("negative_flag", TRADE_VELOCITY_NOTIF_NEG_FLAG))
                    disk_pos = bool(n_cfg.get("positive_flag", TRADE_VELOCITY_NOTIF_POS_FLAG))
                    if disk_neg != self.low_vol_flag:
                        self.low_vol_flag = disk_neg
                    if disk_pos != self.high_vol_flag:
                        self.high_vol_flag = disk_pos
        except Exception as e:
            logger.error(f"[TRADE_VELOCITY:{self.universe_id}] Error syncing flags from disk: {e}")

    def _save_flags_to_disk(self, neg: bool, pos: bool) -> None:
        try:
            cfg_path = DATA_DIR / "app.json"
            if cfg_path.exists():
                cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
                n_cfg = cfg_data.setdefault("trade_velocity", {}).setdefault("notifications", {})
                n_cfg["negative_flag"] = neg
                n_cfg["positive_flag"] = pos
                Utils.atomic_write_json(cfg_path, cfg_data, indent=4)
        except Exception as e:
            logger.error(f"[TRADE_VELOCITY:{self.universe_id}] Error saving flags to disk: {e}")

    async def start(self) -> None:
        if not TRADE_VELOCITY_NOTIF_ENABLED or not TG_ENABLED:
            return
        self.is_running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(f"[TRADE_VELOCITY:{self.universe_id}] Notifier loop started.")

    async def stop(self) -> None:
        self.is_running = False
        if self._task:
            self._task.cancel()
        logger.info(f"[TRADE_VELOCITY:{self.universe_id}] Notifier loop stopped.")

    async def _loop(self) -> None:
        while self.is_running:
            try:
                await self.check_and_notify()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[TRADE_VELOCITY:{self.universe_id}] Error in notifier loop: {e}")
            await asyncio.sleep(60)

    async def check_and_notify(self) -> None:
        self._sync_flags_from_disk()

        if self.universe_id == "default":
            analytics_path = ANALYTICS_STATS_DIR / "analytics.json"
        else:
            analytics_path = ANALYTICS_STATS_DIR / f"analytics_{self.universe_id}.json"

        if not analytics_path.exists():
            return

        analytics_data = Utils.read_json_file(analytics_path)
        metrics = self.engine.calculate_velocity(analytics_data)

        if not metrics.get("is_ready_for_alerts", False) or metrics.get("speed_ratio") is None:
            return

        speed_ratio = float(metrics["speed_ratio"])
        low_th = float(self.engine.low_ratio_threshold)
        low_rec = float(self.engine.low_recovery_ratio)
        high_th = float(self.engine.high_ratio_threshold)
        high_reset = float(self.engine.high_reset_ratio)

        u_prefix = f"[{self.universe_id.upper()}] " if self.universe_id != "default" else ""

        if speed_ratio <= low_th and not self.low_vol_flag:
            self.low_vol_flag = True
            self._save_flags_to_disk(neg=True, pos=self.high_vol_flag)
            msg = (
                f"❄️ <b>{u_prefix}ВНИМАНИЕ! НЕДОГРЕВ РЫНКА</b>\n\n"
                f"Скорость сделок упала ниже допустимого критерия:\n"
                f"• Срез: <b>{metrics['slice_description']}</b>\n"
                f"• Скорость: <b>{metrics['avg_effective_speed']}</b> сд/монету (Эталон: <b>{metrics['benchmark_speed']}</b> сд/монету)\n"
                f"• Коэффициент: <b>{speed_ratio:.2f}x</b> (Порог: &lt;= {low_th:.2f}x)"
            )
            await self._send_tg(msg)
        elif self.low_vol_flag and speed_ratio >= low_rec:
            self.low_vol_flag = False
            self._save_flags_to_disk(neg=False, pos=self.high_vol_flag)
            logger.info(f"[TRADE_VELOCITY:{self.universe_id}] Low volatility flag cleared (ratio reached {speed_ratio:.2f}x >= {low_rec:.2f}x)")

        if speed_ratio >= high_th and not self.high_vol_flag:
            self.high_vol_flag = True
            self._save_flags_to_disk(neg=self.low_vol_flag, pos=True)
            msg = (
                f"🔥 <b>{u_prefix}ВНИМАНИЕ! ВОЛАТИЛЬНОСТЬ ЗАШИБИСЬ</b>\n\n"
                f"Скорость сделок достигла/превысила эталон:\n"
                f"• Срез: <b>{metrics['slice_description']}</b>\n"
                f"• Скорость: <b>{metrics['avg_effective_speed']}</b> сд/монету (Эталон: <b>{metrics['benchmark_speed']}</b> сд/монету)\n"
                f"• Коэффициент: <b>{speed_ratio:.2f}x</b> (Порог: &gt;= {high_th:.2f}x)"
            )
            await self._send_tg(msg)
        elif self.high_vol_flag and speed_ratio <= high_reset:
            self.high_vol_flag = False
            self._save_flags_to_disk(neg=self.low_vol_flag, pos=False)
            logger.info(f"[TRADE_VELOCITY:{self.universe_id}] High volatility flag cleared (ratio dropped to {speed_ratio:.2f}x <= {high_reset:.2f}x)")

        now = time.time()
        interval_sec = TRADE_VELOCITY_REPORT_INTERVAL_HOURS * 3600.0
        if now - self.last_periodic_report_ts >= interval_sec:
            self.last_periodic_report_ts = now
            report_msg = (
                f"📊 <b>{u_prefix}Отчет по скорости сделок ({metrics['timeframe_hours']}h)</b>\n\n"
                f"• Срез: <b>{metrics['slice_description']}</b>\n"
                f"• Скорость: <b>{metrics['avg_effective_speed']}</b> сд/монету (Эталон: <b>{metrics['benchmark_speed']}</b> сд/монету)\n"
                f"• Коэффициент активности: <b>{speed_ratio:.2f}x</b> [{metrics['status_desc']}]"
            )
            await self._send_tg(report_msg)

    async def _send_tg(self, text: str) -> None:
        if not self.token or not TG_ALLOWED_USERS:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        async with aiohttp.ClientSession() as session:
            for uid in TG_ALLOWED_USERS:
                try:
                    payload = {"chat_id": uid, "text": text, "parse_mode": "HTML"}
                    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        pass
                except Exception as e:
                    logger.error(f"[TRADE_VELOCITY:{self.universe_id}] Failed to send TG message to {uid}: {e}")
