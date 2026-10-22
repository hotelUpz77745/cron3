# CORE/SERVICES/VOLATILITY/volatility_manager.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.SERVICES.VOLATILITY.volatility_manager
ROLE: Менеджер динамической волатильности сеток (Super Grid Indent)
==============================================================================
"""

import asyncio
import time
import json
import pickle
import sys
from pathlib import Path
from c_log import UnifiedLogger
from consts import DATA_DIR, _CFG
from c_utils import Utils
from API.BINANCE.client_prod import BinanceClient

logger = UnifiedLogger("VolatilityManager")


class VolatilityManager:
    def __init__(self, bot_core):
        self.bot_core = bot_core
        self.client = bot_core.client
        self._task = None
        self.is_running = False

    def start(self):
        if self._task is None or self._task.done():
            self.is_running = True
            self._task = asyncio.create_task(self._loop())

    def stop(self):
        self.is_running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _loop(self):
        logger.info("[VolatilityManager] Started background loop.")
        while self.is_running:
            try:
                await self.process_all()
            except Exception as e:
                logger.error(f"[VolatilityManager] Error in loop: {e}")
            
            # Wait for next update interval from strategy domes
            hours_list = []
            if hasattr(self.bot_core, "strategy_registry"):
                for scfg in self.bot_core.strategy_registry.active_strategies.values():
                    if scfg.super_grid_enabled:
                        hours_list.append(scfg.super_grid_cfg.get("update_interval_hours", 24))
            app_data = Utils.read_json_file(DATA_DIR / "app.json")
            if "super_grid" in app_data:
                hours_list.append(app_data["super_grid"].get("update_interval_hours", 24))
            interval_hours = min(hours_list) if hours_list else 24
            wait_sec = max(3600, interval_hours * 3600)
            await asyncio.sleep(wait_sec)

    async def process_all(self):
        app_data = Utils.read_json_file(DATA_DIR / "app.json")
        symbols = list(self.bot_core.symbols)
        if not symbols:
            return

        stats_output = {}

        for symbol in symbols:
            if not self.is_running:
                break

            # Resolve super_grid config for this coin's strategy dome
            strat = None
            if hasattr(self.bot_core, "strategy_registry"):
                try:
                    strat = self.bot_core.strategy_registry.get_strategy_for_symbol(symbol)
                except Exception:
                    strat = None

            if strat:
                sg_cfg = strat.super_grid_cfg
                is_advanced = strat.super_grid_enabled
            elif "super_grid" in app_data:
                sg_cfg = app_data["super_grid"]
                is_advanced = bool(sg_cfg.get("enabled", False))
            else:
                is_advanced = False
                sg_cfg = {}

            timeframe = sg_cfg.get("timeframe", "1w") or "1w"
            window = int(sg_cfg.get("window") or 12)
            multiplier = float(sg_cfg.get("multiplier") or 2.0)
            min_volatility_pct = sg_cfg.get("min_volatility_pct")
            if min_volatility_pct is not None:
                try:
                    min_volatility_pct = float(min_volatility_pct)
                except (ValueError, TypeError):
                    min_volatility_pct = None
            max_volatility_pct = sg_cfg.get("max_volatility_pct")
            if max_volatility_pct is not None:
                try:
                    max_volatility_pct = float(max_volatility_pct)
                except (ValueError, TypeError):
                    max_volatility_pct = None

            adjusted_vol = 0.0
            count = 0
            
            if is_advanced:
                safe_window = min(window, 1500)
                try:
                    klines = await self.bot_core.client.get_klines(symbol, timeframe, safe_window)
                    if not klines or len(klines) == 0:
                        logger.warning(f"[VolatilityManager] [{symbol}] Failed to fetch klines or empty.")
                        continue
                except Exception as e:
                    logger.error(f"[VolatilityManager] [{symbol}] API error fetching klines: {e}")
                    continue
                    
                # Calculate average volatility
                total_vol = 0.0
                for k in klines:
                    high = float(k.get("high") or 0.0)
                    low = float(k.get("low") or 0.0)
                    if low > 0:
                        vol = ((high / low) - 1) * 100
                        total_vol += vol
                        count += 1
                
                if count == 0:
                    continue
                    
                if count < window:
                    reason = "Ограничение Binance API 1500 свечей" if window > 1500 and count == 1500 else "монета может быть новой"
                    logger.warning(f"[VolatilityManager] [{symbol}] Запрошено {window} свечей, но получено только {count} ({reason}). Расчет идет по доступным {count} свечам.")
                    
                avg_vol = total_vol / count
                adjusted_vol = avg_vol * multiplier
                
                if min_volatility_pct is not None and adjusted_vol <= min_volatility_pct:
                    logger.info(f"[VolatilityManager] [{symbol}] Игнор: Расчитанная волатильность {adjusted_vol:.2f}% ниже или равна минимуму {min_volatility_pct}%. Возвращаемся к использованию стандартных настроек индента.")
                    is_advanced = False
                elif max_volatility_pct is not None and adjusted_vol >= max_volatility_pct:
                    logger.info(f"[VolatilityManager] [{symbol}] Игнор: Расчитанная волатильность {adjusted_vol:.2f}% выше или равна максимуму {max_volatility_pct}%. Возвращаемся к использованию стандартных настроек индента.")
                    is_advanced = False

            # Resolve runtime path under strategy dome (.json prioritized)
            if hasattr(self.bot_core, "runtime_manager") and hasattr(self.bot_core.runtime_manager, "get_runtime_path"):
                runtime_path = self.bot_core.runtime_manager.get_runtime_path(symbol)
            elif strat:
                runtime_path = DATA_DIR / "runtime" / strat.strategy_id / f"{symbol.lower()}.json"
                if not runtime_path.exists():
                    runtime_path = DATA_DIR / "runtime" / f"{symbol.lower()}.json"
            else:
                runtime_path = DATA_DIR / "runtime" / f"{symbol.lower()}.json"

            if not runtime_path.exists():
                cand_pkl = runtime_path.with_suffix(".pkl")
                if cand_pkl.exists():
                    runtime_path = cand_pkl
                else:
                    continue
                
            try:
                if str(runtime_path).endswith(".pkl"):
                    with open(runtime_path, 'rb') as f:
                        rt_data = pickle.load(f)
                else:
                    with open(runtime_path, 'r', encoding='utf-8') as f:
                        rt_data = json.load(f)
                    
                modified = False
                ratio: float = 1.0
                for side in ["LONG", "SHORT"]:
                    if side in rt_data and "grid" in rt_data[side]:
                        grid = rt_data[side]["grid"]
                        if not grid:
                            continue
                            
                        # Find the active levels with non-null indents
                        valid_levels = {
                            int(idx): el for idx, el in grid.items()
                            if el.get("enabled", True) is not False and el.get("indent") is not None
                        }
                        if not valid_levels:
                            continue

                        max_valid_lvl = max(valid_levels.keys())
                        last_indent_raw = valid_levels[max_valid_lvl].get("indent")
                        try:
                            last_indent = abs(float(last_indent_raw)) if last_indent_raw is not None else 0.0
                        except (ValueError, TypeError):
                            last_indent = 0.0
                        
                        if last_indent == 0:
                            continue
                            
                        ratio = adjusted_vol / last_indent if is_advanced else 1.0
                        
                        # Apply to all elements in grid
                        for idx, el in grid.items():
                            is_lvl_enabled = el.get("enabled", True) is not False
                            raw_ind = el.get("indent")

                            if is_advanced and is_lvl_enabled and raw_ind is not None:
                                try:
                                    orig_indent = float(raw_ind)
                                    new_indent = orig_indent * ratio
                                    el["super_indent"] = round(new_indent, 4)
                                    if not el.get("is_active", False):
                                        el["price"] = None
                                except (ValueError, TypeError):
                                    el["super_indent"] = None
                            else:
                                if el.get("super_indent") is not None:
                                    el["super_indent"] = None
                                    if not el.get("is_active", False):
                                        el["price"] = None
                                        
                        modified = True
                        
                if modified:
                    target_save = runtime_path.with_suffix(".json")
                    Utils.atomic_write_json(target_save, rt_data, indent=4)
                    old_pkl = runtime_path.with_suffix(".pkl")
                    if old_pkl.exists():
                        try:
                            old_pkl.unlink()
                        except Exception:
                            pass
                    if hasattr(self.bot_core, "runtime_manager") and hasattr(self.bot_core.runtime_manager, "caches"):
                        if symbol in self.bot_core.runtime_manager.caches:
                            self.bot_core.runtime_manager.caches[symbol] = rt_data
                        
                    if is_advanced:
                        logger.info(f"[VolatilityManager] [{symbol}] Переход на Super Grid настройки. Волатильность={adjusted_vol:.2f}%. Коэффициент={ratio:.4f}. Сетка обновлена (super_indent).")
                    else:
                        logger.debug(f"[VolatilityManager] [{symbol}] Сетка сброшена на стандартные настройки (super_indent удален).")
                        
                    stats_output[symbol] = {
                        "is_advanced": is_advanced,
                        "timeframe": timeframe,
                        "window": window,
                        "candles_fetched": count,
                        "volatility_pct": round(adjusted_vol, 2),
                        "ratio": round(ratio, 4) if is_advanced else 1.0
                    }
                    
                    # Пробрасываем изменения прямо в запущенную FSM (сбрасываем price у неактивных уровней)
                    if hasattr(self.bot_core, "fsm_states") and symbol in self.bot_core.fsm_states:
                        for side, state in self.bot_core.fsm_states[symbol].items():
                            if hasattr(state, "grid"):
                                fresh_grid = rt_data.get(side, {}).get("grid", {})
                                for lvl_str, lvl_data in state.grid.items():
                                    if lvl_str in fresh_grid:
                                        lvl_data["super_indent"] = fresh_grid[lvl_str].get("super_indent")
                                        
                                    if state.in_position and not lvl_data.get("is_active"):
                                        lvl_data["price"] = None
                                
                                if state.in_position:
                                    state.next_avg_price = None
                                    logger.info(f"[VolatilityManager] [{symbol}] {side} Горячая подгрузка: неактивные прайсы сброшены на None. Ожидание перерасчета сетки.")
                    
            except Exception as e:
                logger.error(f"[VolatilityManager] [{symbol}] Error updating config: {e}")
                
        # Reload configs in memory
        self.bot_core.runtime_manager.load_initial_caches(self.bot_core.symbols)
        self.bot_core.runtime_configs = self.bot_core.runtime_manager.caches
        
        return stats_output
