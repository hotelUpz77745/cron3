# ANALYTICS/iteration_manager.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: ANALYTICS.iteration_manager
ROLE: Управление жизненным циклом и телеметрией итераций стратегий и портфеля
==============================================================================
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

from c_log import UnifiedLogger
from c_utils import Utils

logger = UnifiedLogger("IterationManager")


class StrategyIterationManager:
    """
    Управляет состоянием итерационных циклов:
    - Чтение и запись iteration_state.json
    - Ручной и автоматический сброс итерации стратегии
    - Глобальный сброс итерации портфеля
    """

    @staticmethod
    def get_iteration_cache(cache_dir: Path, strat_id: str) -> dict:
        """Retrieves iteration telemetry cache for a specific strategy."""
        try:
            cache_file = cache_dir / "iteration_state.json"
            if cache_file.exists():
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                return data.get(strat_id, {})
        except Exception:
            pass
        return {}

    @staticmethod
    def save_iteration_cache(cache_dir: Path, strat_id: str, state: dict) -> None:
        """Saves iteration telemetry state to persistent cache."""
        try:
            cache_file = cache_dir / "iteration_state.json"
            data = {}
            if cache_file.exists():
                try:
                    data = json.loads(cache_file.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            data[strat_id] = state
            Utils.atomic_write_json(cache_file, data, indent=4)
        except Exception as e:
            logger.error(f"[ANALYTICS] Error saving iteration cache for {strat_id}: {e}")

    @classmethod
    def reset_strategy_iteration(cls, manager: Any, cache_dir: Path, strategy_id: str) -> dict:
        """
        Принудительный сброс итерации стратегии и переход на новый цикл.
        Синхронизирует stats, cache и возвращает метаданные сброса.
        """
        now_ms = int(time.time() * 1000)
        strat_stats_path = manager.stats_dir / f"analytics_{strategy_id}.json"
        strat_analytics = {}
        if strat_stats_path.exists():
            strat_analytics = Utils.read_json_file(strat_stats_path) or {}
        else:
            strat_analytics = manager._read_data() or {}

        strat_net = float(strat_analytics.get("net_profit_usdt", 0.0))
        cur_iter_idx = int(strat_analytics.get("iteration_index", 1))
        next_iter_idx = cur_iter_idx + 1
        new_iter_start_net = round(strat_net, 4)
        # При закрытии позиций плавающий PnL фиксируется, поэтому стартовый realized равен стартовому net
        new_iter_start_real = new_iter_start_net

        strat_analytics.update({
            "iteration_index": next_iter_idx,
            "iteration_start_net_profit_usdt": new_iter_start_net,
            "iteration_net_profit_usdt": 0.0,
            "iteration_start_realized_pnl_usdt": new_iter_start_real,
            "iteration_realized_pnl_usdt": 0.0,
            "iteration_start_ts": now_ms,
            "iteration_peak_net_profit_usdt": 0.0,
            "iteration_peak_ts": now_ms,
            "iteration_peak_age_hours": 0.0,
            "iteration_net_velocity": 0.0,
        })

        if strat_stats_path.exists():
            Utils.atomic_write_json(strat_stats_path, strat_analytics, indent=4)

        # Синхронизация CACHE/iteration_state.json
        try:
            c_file = cache_dir / "iteration_state.json"
            c_dict = {}
            if c_file.exists():
                try:
                    c_dict = json.loads(c_file.read_text(encoding="utf-8"))
                except Exception:
                    c_dict = {}
            prev_entry = c_dict.get(strategy_id, {})
            c_dict[strategy_id] = {
                "iteration_index": next_iter_idx,
                "iteration_start_net_profit_usdt": new_iter_start_net,
                "iteration_start_realized_pnl_usdt": new_iter_start_real,
                "iteration_realized_pnl_usdt": 0.0,
                "iteration_start_ts": now_ms,
                "iteration_peak_net_profit_usdt": 0.0,
                "iteration_peak_ts": now_ms,
                "historical_peak_net_profit_usdt": prev_entry.get("historical_peak_net_profit_usdt", strat_net),
                "historical_peak_net_ts": prev_entry.get("historical_peak_net_ts", now_ms),
                "historical_max_drawdown_usdt": prev_entry.get("historical_max_drawdown_usdt", 0.0),
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            }
            Utils.atomic_write_json(c_file, c_dict, indent=4)
        except Exception as ce:
            logger.error(f"[{strategy_id}] Error saving iteration cache on manual reset: {ce}")

        return {
            "strategy_id": strategy_id,
            "prev_iteration": cur_iter_idx,
            "new_iteration": next_iter_idx,
            "base_net": new_iter_start_net,
            "base_realized": new_iter_start_real,
        }

    @classmethod
    def reset_global_iteration(cls, manager: Any, cache_dir: Path) -> dict:
        """Сброс глобальной итерации портфеля в analytics.json и CACHE/iteration_state.json."""
        try:
            g_path = manager.log_file
            g_data = Utils.read_json_file(g_path) or {}
            g_net = float(g_data.get("net_profit_usdt", 0.0))
            g_idx = int(g_data.get("iteration_index", 1))
            next_g_idx = g_idx + 1
            now_ms = int(time.time() * 1000)

            g_data["iteration_index"] = next_g_idx
            g_data["iteration_start_net_profit_usdt"] = round(g_net, 4)
            g_data["iteration_net_profit_usdt"] = 0.0
            g_data["iteration_start_ts"] = now_ms
            g_data["iteration_peak_net_profit_usdt"] = 0.0
            g_data["iteration_peak_ts"] = now_ms
            g_data["iteration_peak_age_hours"] = 0.0
            g_data["iteration_net_velocity"] = 0.0
            Utils.atomic_write_json(g_path, g_data, indent=4)

            c_file = cache_dir / "iteration_state.json"
            c_dict = {}
            if c_file.exists():
                try:
                    c_dict = json.loads(c_file.read_text(encoding="utf-8"))
                except Exception:
                    c_dict = {}
            c_dict["__global__"] = {
                "iteration_index": next_g_idx,
                "iteration_start_net_profit_usdt": round(g_net, 4),
                "iteration_start_ts": now_ms,
                "iteration_peak_net_profit_usdt": 0.0,
                "iteration_peak_ts": now_ms,
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            }
            Utils.atomic_write_json(c_file, c_dict, indent=4)
            return {"iteration_index": next_g_idx, "base_net": round(g_net, 4)}
        except Exception as ge:
            logger.error(f"Error resetting global portfolio iteration: {ge}")
            return {}
