# ==============================================================================
# Path: CORE/GRID/avg_manager.py
# Role: Менеджер усреднений (Grid Manager)
# ==============================================================================

import asyncio
import time
from typing import Optional, Any, Dict
from c_log import UnifiedLogger
from CORE._utils import TradeMath
from c_utils import Utils

from consts import _CFG, DEFAULT_GRID_VOLUME_TOLERANCE_PCT
from CORE.GRID.dynamic_atr_grid import DynamicAtrGridManager
from CORE.STRATEGY.trailing_system import BaseTrailingSystem
from CORE.STRATEGY.peeling_manager import PeelingManager
from CORE.GRID.smart_avg_manager import SmartAvgManager

logger = UnifiedLogger("AvgManager")

class AverageManager:
    def __init__(self, peeling_manager: Optional[Any] = None, smart_avg_manager: Optional[Any] = None):
        self.peeling_manager = peeling_manager if peeling_manager is not None else PeelingManager()
        self.smart_avg_manager = smart_avg_manager if smart_avg_manager is not None else SmartAvgManager()

    async def process_peel(
        self,
        client: Any,
        runtime_manager: Any,
        symbol: str,
        side: str,
        state: Any,
        current_price: float,
        spec_data: dict,
        tp_manager: Any,
    ) -> bool:
        """
        Проверяет и исполняет частичную разгрузку (LIFO-пилинг) крайнего усреднения сетки.
        Делегирует в выделенный доменный класс PeelingManager.
        """
        return await self.peeling_manager.process_peel(
            client=client,
            runtime_manager=runtime_manager,
            symbol=symbol,
            side=side,
            state=state,
            current_price=current_price,
            spec_data=spec_data,
            tp_manager=tp_manager,
        )

    @staticmethod
    def _current_time(runtime_manager: Any) -> float:
        bot_core = getattr(runtime_manager, "bot_core", None) if runtime_manager is not None else None
        return Utils.get_current_time(getattr(bot_core, "current_sim_time", None))

    def reset(self, symbol: str, side: str):
        pass

    async def _init_grid_prices(self, runtime_manager, symbol: str, side: str, state, spec_data: dict, client: Optional[object] = None) -> bool:
        """Единожды рассчитывает цены для всей сетки на основе initial_entry_price с поддержкой dynamic_atr_grid."""
        grid = state.grid
        initial_price = state.initial_entry_price
        
        # Защита: если WS еще не подтянул initial_price, но есть avg_entry_price
        if initial_price == 0.0 and state.avg_entry_price > 0:
            initial_price = state.avg_entry_price
            state.initial_entry_price = initial_price

        if initial_price <= 0:
            return False # Еще нет цены для расчета

        prices_populated = (
            getattr(state, "_grid_prices_initialized_for", None) == initial_price
            and initial_price > 0
            and all(
                lvl_d.get("price") is not None
                for lvl_k, lvl_d in grid.items()
                if lvl_k != "0"
                and isinstance(lvl_d, dict)
                and lvl_d.get("enabled", True) is not False
                and not lvl_d.get("is_active", False)
            )
        )
        if prices_populated:
            return True

        # 0. Разрешение конфигурации dynamic_atr_grid
        atr_grid_cfg = None
        if getattr(runtime_manager, "strategy_registry", None):
            try:
                strat = runtime_manager.strategy_registry.get_strategy_for_symbol(symbol)
                atr_grid_cfg = getattr(strat, "dynamic_atr_grid_cfg", None)
                if not atr_grid_cfg and hasattr(strat, "raw_meta"):
                    atr_grid_cfg = strat.raw_meta.get("dynamic_atr_grid")
            except Exception:
                atr_grid_cfg = None
        if not atr_grid_cfg:
            atr_grid_cfg = _CFG.get("dynamic_atr_grid") or {
                "enabled": True,
                "timeframe": "1h",
                "period": 14,
                "cache_ttl_sec": 300,
                "fallback_atr_pct": 2.0,
                "ref_atr_pct": 2.0,
                "min_multiplier": 0.75,
                "max_multiplier": 2.25,
                "max_scaled_level": 3
            }

        atr_mult = 1.0
        if atr_grid_cfg and atr_grid_cfg.get("enabled", False):
            atr_mult = await DynamicAtrGridManager.get_grid_atr_multiplier(
                client=client,
                symbol=symbol,
                cfg=atr_grid_cfg
            )
            
        is_sg_enabled = False
        if getattr(runtime_manager, "strategy_registry", None):
            try:
                strat = runtime_manager.strategy_registry.get_strategy_for_symbol(symbol)
                is_sg_enabled = bool(strat.super_grid_enabled)
            except Exception:
                is_sg_enabled = False
        elif "super_grid" in _CFG:
            sg_val = _CFG["super_grid"]
            is_sg_enabled = bool(sg_val.get("enabled", False)) if isinstance(sg_val, dict) else bool(sg_val)

        needs_save = False
        for level_str, level_data in grid.items():
            if level_data.get("enabled", True) is False or (level_str != "0" and level_data.get("indent") is None):
                level_data["price"] = None
                level_data["activation_price"] = None
                level_data["is_armed"] = False
                continue
            if level_str == "0":
                if not level_data.get("is_active") or level_data.get("activated_at") is None:
                    level_data["is_active"] = True
                    level_data["price"] = initial_price
                    sim_ts = getattr(client, "current_sim_time", None) if client is not None else None
                    sim_sec = (float(sim_ts) / 1000.0 if sim_ts > 1e9 else float(sim_ts)) if isinstance(sim_ts, (int, float)) and sim_ts > 0 else None
                    act_time = (state.open_time / 1000.0) if (state.open_time and state.open_time > 0) else (sim_sec if sim_sec is not None else time.time())
                    level_data["activated_at"] = act_time
                    level_data["timestamp"] = int(act_time * 1000)
                    needs_save = True
            else:
                if "_base_indent" not in level_data and "indent" in level_data:
                    level_data["_base_indent"] = level_data["indent"]
                if "_base_activation" not in level_data and "activation" in level_data:
                    level_data["_base_activation"] = level_data["activation"]

                try:
                    lvl_idx = int(level_str)
                except Exception:
                    lvl_idx = 1

                base_indent = level_data.get("_base_indent", level_data.get("indent"))
                sg_indent = (
                    level_data["super_indent"]
                    if (is_sg_enabled and level_data.get("super_indent") is not None)
                    else base_indent
                )
                eff_indent = None
                if sg_indent is not None:
                    eff_indent = DynamicAtrGridManager.calculate_effective_indent(
                        base_indent=float(sg_indent),
                        level_index=lvl_idx,
                        atr_mult=atr_mult,
                        cfg=atr_grid_cfg,
                    )

                if level_data.get("price") is None and eff_indent is not None:
                    level_data["indent"] = eff_indent
                    level_data["effective_indent"] = eff_indent
                    level_data["atr_mult"] = atr_mult

                    price = TradeMath.calculate_grid_price(initial_price, eff_indent, side, spec_data, symbol)
                    level_data["price"] = price
                    needs_save = True

                # Синхронизированный расчёт цены активации (курка): base -> super_grid -> dynamic_atr_grid
                if level_data.get("activation_price") is None:
                    base_act = level_data.get("_base_activation", level_data.get("activation"))
                    if base_act is not None:
                        if is_sg_enabled and level_data.get("super_activation") is not None:
                            sg_act = float(level_data["super_activation"])
                        elif (
                            is_sg_enabled
                            and level_data.get("super_indent") is not None
                            and base_indent is not None
                            and float(base_indent) != 0.0
                        ):
                            sg_ratio = float(level_data["super_indent"]) / float(base_indent)
                            sg_act = round(float(base_act) * sg_ratio, 4)
                        else:
                            sg_act = float(base_act)

                        if eff_indent is not None and sg_indent is not None and float(sg_indent) != 0.0:
                            # Сохраняем пропорцию ширины коридора activation/indent относительно результата dynamic_atr_grid
                            atr_scale = float(eff_indent) / float(sg_indent)
                            eff_act = round(sg_act * atr_scale, 4)
                        else:
                            eff_act = DynamicAtrGridManager.calculate_effective_indent(
                                base_indent=float(sg_act),
                                level_index=lvl_idx,
                                atr_mult=atr_mult,
                                cfg=atr_grid_cfg,
                            )

                        level_data["activation"] = eff_act
                        level_data["effective_activation"] = eff_act
                        act_price = TradeMath.calculate_grid_price(initial_price, eff_act, side, spec_data, symbol)
                        level_data["activation_price"] = act_price
                        needs_save = True
                
        if needs_save:
            # Не вызываем save_cache здесь, это будет сделано в sync_with_fsm
            pass
            
        state._grid_prices_initialized_for = initial_price
        return True # Расчет проведен

    async def process(self, client, runtime_manager, symbol: str, side: str, state, opposite_state, current_price: float, spec_data: dict, tp_manager):
        """Проверяет и выполняет логику усреднения для позиции с поддержкой трейлинга."""
        if hasattr(state, "can_start_averaging"):
            if not state.can_start_averaging() or current_price is None:
                return
        elif not state.in_position or state.pending_avg or current_price is None:
            return

        grid = state.grid

        # 1. Расчет отсутствующих цен сетки (если price == None)
        success = await self._init_grid_prices(runtime_manager, symbol, side, state, spec_data, client=client)
        if not success:
            return

        # 2. Расчет относительной просадки от начальной точки входа
        base_price = state.initial_entry_price if state.initial_entry_price > 0 else state.avg_entry_price
        if base_price <= 0:
            return

        if side == "LONG":
            dd_pct = (base_price - current_price) / base_price * 100.0
        else:
            dd_pct = (current_price - base_price) / base_price * 100.0
        dd_pct = round(max(0.0, dd_pct), 4)

        # Санитизация взведенных уровней: исполненные (is_active=True) или отключенные (enabled=False) уровни никогда не могут быть взведены
        armed_dict = state.trailing_grid_state.get("armed_levels", {})
        if isinstance(armed_dict, dict) and armed_dict:
            for lvl_k, lvl_d in grid.items():
                if isinstance(lvl_d, dict):
                    if lvl_d.get("enabled", True) is False or lvl_d.get("indent") is None or lvl_d.get("is_active"):
                        lvl_d["is_armed"] = False
                        lvl_d.pop("armed_at_drawdown", None)
                        lvl_d.pop("armed_timestamp", None)
                        if lvl_k in armed_dict:
                            armed_dict.pop(lvl_k, None)

        # 3. Трейлинг оценка сетки через BaseTrailingSystem
        armed_map = state.trailing_grid_state.get("armed_levels", {}) if hasattr(state, "trailing_grid_state") and isinstance(state.trailing_grid_state, dict) else {}
        prev_armed = set(armed_map.keys()) if armed_map else None
        candidate_level, skipped_levels, info = BaseTrailingSystem.evaluate_trailing_grid(
            dd_pct, grid, state.trailing_grid_state
        )
        curr_armed_map = state.trailing_grid_state.get("armed_levels", {}) if hasattr(state, "trailing_grid_state") and isinstance(state.trailing_grid_state, dict) else {}
        if curr_armed_map:
            newly_armed = set(curr_armed_map.keys()) - (prev_armed or set())
            if newly_armed:
                for lvl in sorted(newly_armed, key=lambda x: int(x) if x.isdigit() else x):
                    lvl_d = grid.get(lvl, {})
                    raw_act = (
                        lvl_d.get("effective_activation")
                        if lvl_d.get("effective_activation") is not None
                        else (lvl_d.get("super_activation") if lvl_d.get("super_activation") is not None else lvl_d.get("activation"))
                    )
                    raw_flr = (
                        lvl_d.get("effective_indent")
                        if lvl_d.get("effective_indent") is not None
                        else (lvl_d.get("super_indent") if lvl_d.get("super_indent") is not None else lvl_d.get("indent"))
                    )
                    logger.info(
                        f"[{symbol}] {side} ⚡ [GRID TRIGGER ARMED] Level {lvl}: "
                        f"Drawdown {dd_pct:.2f}% >= Trigger {raw_act}% (Waiting for rebound to <= {raw_flr}%)"
                    )

        if not candidate_level:
            # Обновляем ориентировочную next_avg_price для мониторинга
            sorted_keys = getattr(state, "_cached_grid_keys", None)
            if sorted_keys is None or len(sorted_keys) != len(grid):
                sorted_keys = tuple(sorted(grid.keys(), key=lambda x: int(x) if x.isdigit() else x))
                state._cached_grid_keys = sorted_keys
            for lvl_str in sorted_keys:
                if lvl_str == "0":
                    continue
                data = grid[lvl_str]
                if data.get("enabled", True) is False:
                    continue
                if not data.get("is_active"):
                    state.next_avg_price = data.get("price")
                    break
            return

        now_sec = self._current_time(runtime_manager)

        level_data = grid[candidate_level]
        # Защитный кулдаун: если прошлый ордер отклонен или заблокирован Smaragd, не долбим биржу и фильтр на каждом микротике
        if level_data.get("failed_cooldown_until", 0) > now_sec:
            return

        # 3.5. Проверка макро-фильтра Smaragd (Smart Avg)
        if self.smart_avg_manager:
            is_blocked, block_reason, trend_val = await self.smart_avg_manager.should_block_averaging(
                symbol=symbol,
                side=side,
                candidate_level=candidate_level,
                state=state,
                runtime_manager=runtime_manager,
                client=client
            )
            if is_blocked:
                # Восстанавливаем статус взведения в trailing_grid_state только для трейлинговых ступеней (activation != None)
                armed_dict = state.trailing_grid_state.setdefault("armed_levels", {})
                if candidate_level in grid and isinstance(grid[candidate_level], dict):
                    if grid[candidate_level].get("activation") is not None:
                        grid[candidate_level]["is_armed"] = True
                        armed_dict[candidate_level] = {
                            "armed_at_drawdown": grid[candidate_level].get("armed_at_drawdown", dd_pct),
                            "deepest_drawdown": max(grid[candidate_level].get("armed_at_drawdown", dd_pct), dd_pct),
                        }
                    # Защитный кулдаун (60 сек): исключает холостой микротиковый цикл при неизменном дневном тренде
                    grid[candidate_level]["failed_cooldown_until"] = now_sec + 60.0
                for sk in skipped_levels:
                    if sk in grid and isinstance(grid[sk], dict):
                        if grid[sk].get("activation") is not None:
                            grid[sk]["is_armed"] = True
                            armed_dict[sk] = {
                                "armed_at_drawdown": grid[sk].get("armed_at_drawdown", dd_pct),
                                "deepest_drawdown": max(grid[sk].get("armed_at_drawdown", dd_pct), dd_pct),
                            }
                        grid[sk]["failed_cooldown_until"] = now_sec + 60.0
                sm_cfg = self.smart_avg_manager._resolve_cfg(symbol, side, state, runtime_manager)
                actual_set = self.smart_avg_manager.parse_actual_levels(sm_cfg.get("actual_levels", "L3-L8") if isinstance(sm_cfg, dict) else "L3-L8")
                for lk, ld in grid.items():
                    if str(lk).isdigit() and int(lk) in actual_set and isinstance(ld, dict) and not ld.get("is_active"):
                        ld["failed_cooldown_until"] = now_sec + 60.0
                state.next_avg_price = grid.get(candidate_level, {}).get("price")
                return

        # 4. Обработка пропущенных уровней (avg_free_volume_behavior)
        pre_skip_snapshot: Optional[Dict[str, Dict[str, Any]]] = None
        if skipped_levels:
            pre_skip_snapshot = {
                str(k): dict(v) for k, v in grid.items() if isinstance(v, dict)
            }
            raw_behavior = getattr(state, "avg_free_volume_behavior", "distribute_skip")
            if not raw_behavior:
                raw_behavior = "distribute_skip"

            if raw_behavior in ("distribute_skip", "mark_executed", "skip"):
                behavior = "mark_executed"
            elif raw_behavior in ("distribute_nearest", "accumulate_current", "merge"):
                behavior = "accumulate_current"
            elif raw_behavior in ("distribute_remaining", "distribute_within_zone"):
                behavior = "distribute_remaining"
            else:
                behavior = raw_behavior

            skipped_vol = sum(float(grid[sk].get("volume") or 0.0) for sk in skipped_levels if isinstance(grid.get(sk), dict) and grid[sk].get("enabled", True) is not False)
            for sk in skipped_levels:
                grid[sk]["is_active"] = True
                grid[sk]["activated_at"] = now_sec
                grid[sk]["timestamp"] = int(now_sec * 1000)
                grid[sk]["_skipped"] = True
                grid[sk]["_skip_policy"] = raw_behavior
                grid[sk]["_skipped_by_level"] = str(candidate_level)
                if behavior == "mark_executed":
                    grid[sk]["_skip_status"] = "burnt"
                    grid[sk]["_skip_desc"] = (
                        f"Пропущен и сожжен по политике mark_executed/distribute_skip при сработке L{candidate_level} "
                        f"(объем сгорел без маржинальной нагрузки)"
                    )
                elif behavior == "accumulate_current":
                    grid[sk]["_skip_status"] = f"accumulated_into_L{candidate_level}"
                    grid[sk]["_skip_desc"] = (
                        f"Пропущен, объем перенесен в колено L{candidate_level} (политика accumulate_current/distribute_nearest)"
                    )
                elif behavior in ("distribute_remaining", "distribute_within_zone"):
                    grid[sk]["_skip_status"] = "distributed_remaining"
                    grid[sk]["_skip_desc"] = (
                        f"Пропущен, объем распределен по оставшимся ступеням (политика distribute_remaining)"
                    )

            level_data["_skipped_levels_handled"] = list(skipped_levels)

            # Режим 2: accumulate_current (distribute_nearest) — вливаем весь пропущенный объем в ближайший сработавший уровень
            if behavior == "accumulate_current":
                level_data["volume"] = round(float(level_data.get("volume") or 0.0) + skipped_vol, 4)
                logger.info(
                    f"[{symbol}] {side} 📦 [VOLUME ACCUMULATED: distribute_nearest] "
                    f"Added {skipped_vol:.2f}% from skipped levels {skipped_levels} into triggered level {candidate_level} "
                    f"(new vol: {level_data['volume']:.2f}%)"
                )
            # Режим 1: distribute_remaining (и distribute_within_zone) — распределяем равномерно между всеми оставшимися уровнями
            elif behavior in ("distribute_remaining", "distribute_within_zone"):
                cand_idx = int(candidate_level) if candidate_level.isdigit() else -1
                remaining = [
                    lvl for lvl in sorted(grid.keys(), key=lambda x: int(x) if x.isdigit() else x)
                    if (int(lvl) if lvl.isdigit() else -1) > cand_idx
                    and not grid[lvl].get("is_active")
                    and grid[lvl].get("enabled", True) is not False
                    and grid[lvl].get("indent") is not None
                ]
                if remaining:
                    vol_share = round(skipped_vol / len(remaining), 4)
                    for rem_lvl in remaining:
                        grid[rem_lvl]["volume"] = round(float(grid[rem_lvl].get("volume") or 0.0) + vol_share, 4)
                    logger.info(
                        f"[{symbol}] {side} 📦 [VOLUME DISTRIBUTED: {raw_behavior}] "
                        f"Distributed {skipped_vol:.2f}% from skipped levels {skipped_levels} across remaining {remaining} "
                        f"(+{vol_share:.2f}% each)"
                    )
                else:
                    logger.info(
                        f"[{symbol}] {side} 📦 [VOLUME CEILING REACHED] "
                        f"Skipped {skipped_levels} at grid ceiling, remaining volume ignored."
                    )
            # Режим 3: mark_executed (distribute_skip) — аннулирование объема (пропущенные уровни помечаются исполненными без редистрибуции)
            elif behavior == "mark_executed":
                logger.info(
                    f"[{symbol}] {side} 📦 [VOLUME BURNT: distribute_skip] "
                    f"Skipped levels {skipped_levels} marked executed (volume burnt, no redistribution)."
                )

        is_trailing = grid[candidate_level].get("activation") is not None
        trigger_tag = "🌀 [GRID REBOUND EXECUTED]" if is_trailing else "⚡ [GRID STATIC EXECUTED]"
        logger.info(f"[{symbol}] {side} {trigger_tag} Level {candidate_level} triggered at price {current_price} ({info})")

        # 4.5. Институциональный предохранитель от повторного/ложного усреднения:
        # Если позиция на бирже УЖЕ содержит объем сработавшего уровня (например, после перезапуска бота
        # или ручных вмешательств оператора), ордер НЕ отправляется! Уровень маркируется отработанным.
        side_c = {}
        if runtime_manager and hasattr(runtime_manager, "caches") and symbol in runtime_manager.caches:
            side_c = runtime_manager.caches[symbol].get(side, {})
        eff_invest = float(getattr(state, "eff_invest", 0.0) or 0.0)
        cfg_eff = float(side_c.get("eff_invest", 0.0) or 0.0)
        cfg_invest = float(side_c.get("invest_size", 0.0) or 0.0)
        if eff_invest > 0:
            invest_size = eff_invest
        elif cfg_eff > 0:
            invest_size = cfg_eff
        elif cfg_invest > 0:
            invest_size = cfg_invest
        else:
            raise ValueError(
                f"[{symbol}] {side} [Fail-Fast] Cannot reconcile averaging: invest_size is not configured or <= 0 "
                f"(state.eff_invest={eff_invest}, side_cfg.eff_invest={cfg_eff}, side_cfg.invest_size={cfg_invest})."
            )

        if invest_size > 0:
            cand_idx = int(candidate_level) if candidate_level.isdigit() else 999
            sorted_keys = sorted(grid.keys(), key=lambda x: int(x) if x.isdigit() else 999)
            cum_planned_usdt = sum(
                invest_size * (float(grid[k].get("volume", 0.0)) / 100.0)
                for k in sorted_keys if (int(k) if k.isdigit() else 999) <= cand_idx
            )
            # Толерантность объема сетки из паспорта стратегии / app.json, либо канонический дефолт
            res_cfg = side_c.get("fsm_level_resolution", {}) if isinstance(side_c, dict) else {}
            if not res_cfg:
                res_cfg = _CFG.get("fsm_level_resolution", {}) if isinstance(_CFG, dict) else {}
            tol_pct = Utils.safe_float(res_cfg.get("volume_tolerance_pct"), DEFAULT_GRID_VOLUME_TOLERANCE_PCT)
            tolerance_factor = 1.0 - (tol_pct / 100.0)
            cur_entry = state.avg_entry_price if state.avg_entry_price > 0 else current_price
            current_notional = abs(float(state.total_volume)) * cur_entry

            if current_notional >= (cum_planned_usdt * tolerance_factor):
                logger.warning(
                    f"[{symbol}] {side} 🛡️ [GRID RECONCILIATION GUARD] Level {candidate_level} was already covered by position volume "
                    f"(${current_notional:.2f} >= ${cum_planned_usdt * tolerance_factor:.2f} threshold). "
                    f"Marking level active without placing duplicate market order!"
                )
                level_data["is_active"] = True
                level_data["is_armed"] = False
                level_data.pop("armed_at_drawdown", None)
                level_data.pop("armed_timestamp", None)
                armed_dict = state.trailing_grid_state.get("armed_levels", {})
                if isinstance(armed_dict, dict) and candidate_level in armed_dict:
                    armed_dict.pop(candidate_level, None)
                if skipped_levels:
                    for sk in skipped_levels:
                        if sk in grid and isinstance(grid[sk], dict):
                            grid[sk]["is_active"] = True
                            grid[sk]["is_armed"] = False
                return

        # 5. Взводим флаги исполнения
        state.pending_avg = True
        state.pre_avg_price = state.avg_entry_price
        level_data["is_active"] = True
        level_data["is_armed"] = False
        now_sec = self._current_time(runtime_manager)
        level_data["activated_at"] = now_sec
        level_data["timestamp"] = int(now_sec * 1000)

        # 6. Асинхронно ставим ордер и ждем завершения полного пайплайна
        try:
            await self._execute_averaging_order(
                client, runtime_manager, symbol, side, candidate_level, level_data, current_price, spec_data, state, opposite_state, tp_manager,
                pre_skip_snapshot=pre_skip_snapshot, grid=grid,
            )
        except Exception as e:
            logger.error(f"[{symbol}] {side} Critical error during averaging execution: {e}", exc_info=True)
            self._rollback_failed_grid_step(grid, level_data, pre_skip_snapshot, self._current_time(runtime_manager))
            raise
        finally:
            state.pending_avg = False
            state.next_avg_price = None

    @staticmethod
    def _rollback_failed_grid_step(
        grid: Optional[Dict[str, Any]],
        level_data: Dict[str, Any],
        pre_skip_snapshot: Optional[Dict[str, Dict[str, Any]]],
        now_sec: float,
    ) -> None:
        """Откатывает мутации пропущенных уровней и объемов при сбое маржинального ордера."""
        if pre_skip_snapshot and isinstance(grid, dict):
            for lvl_k, snap_v in pre_skip_snapshot.items():
                if lvl_k in grid and isinstance(grid[lvl_k], dict):
                    grid[lvl_k].clear()
                    grid[lvl_k].update(snap_v)
        level_data["is_active"] = False
        level_data["timestamp"] = None
        level_data["failed_cooldown_until"] = now_sec + 30.0

    async def _execute_averaging_order(
        self, client, runtime_manager, symbol, side, level_str, level_data, current_price, spec_data, state, opposite_state, tp_manager,
        pre_skip_snapshot: Optional[Dict[str, Dict[str, Any]]] = None,
        grid: Optional[Dict[str, Any]] = None,
    ):
        side_c = {}
        if runtime_manager and hasattr(runtime_manager, "caches") and symbol in runtime_manager.caches:
            side_c = runtime_manager.caches[symbol].get(side, {})
        eff_invest = float(getattr(state, "eff_invest", 0.0) or 0.0)
        cfg_eff = float(side_c.get("eff_invest", 0.0) or 0.0)
        cfg_invest = float(side_c.get("invest_size", 0.0) or 0.0)
        if eff_invest > 0:
            invest_size = eff_invest
        elif cfg_eff > 0:
            invest_size = cfg_eff
        elif cfg_invest > 0:
            invest_size = cfg_invest
        else:
            raise ValueError(
                f"[{symbol}] {side} [Fail-Fast] Cannot execute averaging order: invest_size is not configured or <= 0 "
                f"(state.eff_invest={eff_invest}, side_cfg.eff_invest={cfg_eff}, side_cfg.invest_size={cfg_invest})."
            )
        volume_pct = float(level_data.get("volume") or 0.0)
        
        # 1. ОТМЕНА СТАРЫХ ЛИМИТНЫХ ОРДЕРОВ (ВКЛЮЧАЯ ТЕЙК ПРОФИТ) ДО УСРЕДНЕНИЯ
        logger.info(f"[{symbol}] {side} Canceling all limit orders before averaging...")
        await client.cancel_orders_for_side(symbol, side)

        volume = TradeMath.calculate_order_volume(invest_size, volume_pct, current_price, spec_data, symbol)
        order_side = "BUY" if side == "LONG" else "SELL"
        
        # 2. ПОКУПКА ПО РЫНКУ
        logger.info(f"[{symbol}] Opening {side} averaging market order. Level: {level_str}, Volume: {volume}")
        res = await client.make_order(
            symbol=symbol,
            qty=volume,
            side=order_side,
            position_side=side,
            market_type="MARKET"
        )
        
        if not res.success:
            logger.error(f"[{symbol}] Failed to open {side} averaging position: {res.error_msg}")
            # Rollback in case of failure
            self._rollback_failed_grid_step(grid, level_data, pre_skip_snapshot, self._current_time(runtime_manager))
            state.pending_avg = False
            state.next_avg_price = None
            return

        level_data.pop("failed_cooldown_until", None)
        logger.info(f"[{symbol}] {side} Averaging order SUCCESS for level {level_str}. Waiting for FSM sync...")
        level_data["fill_price"] = current_price
        level_data["fill_qty"] = volume
        
        # 3. ДОЖИДАЕМСЯ ОБНОВЛЕНИЯ avg_entry_price ОТ ВЕБСОКЕТА
        # Предохранитель от бесконечного зависания: ждем максимум 3 секунды
        sync_success = await Utils.wait_for_fsm_sync(state, timeout_sec=3.0, poll_interval=0.01)
            
        if not sync_success:
            logger.warning(f"[{symbol}] {side} WS did not update avg_entry_price in time! Forcing REST fallback...")
            try:
                positions = await client.fetch_positions(symbol)
                for p in positions:
                    if p.get("positionSide") == side:
                        new_avg = float(p.get("entryPrice", 0.0))
                        new_vol = float(p.get("positionAmt", 0.0))
                        if new_avg > 0:
                            state.avg_entry_price = new_avg
                            state.total_volume = new_vol
                logger.info(f"[{symbol}] {side} REST fallback applied! New avg_entry_price: {state.avg_entry_price}")
            except Exception as e:
                logger.error(f"[{symbol}] {side} REST fallback failed: {e}")
        else:
            logger.info(f"[{symbol}] {side} FSM synced! New avg_entry_price: {state.avg_entry_price}")

        # 4. ПОСТАНОВКА НОВОГО ЛИМИТНОГО ТЕЙК ПРОФИТА
        await tp_manager.place_take_profit(client, symbol, side, current_price, spec_data, state, opposite_state)

        # Освобождаем флаги
        state.pending_avg = False
        state.next_avg_price = None
        state.fallback_price = None
        state.trailing_tp_state = {}

