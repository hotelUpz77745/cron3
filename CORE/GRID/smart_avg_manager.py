# -*- coding: utf-8 -*-
# ==============================================================================
# Path: CORE/GRID/smart_avg_manager.py
# Role: Smart Avg (Smaragd) macro-trend grid averaging blocker & volume manager
# ==============================================================================

import copy
import re
import time
from typing import Dict, Any, Optional, Set, Union, List, Tuple
from c_log import UnifiedLogger
from CORE.STRATEGY.momentum_engine import MomentumEngine

logger = UnifiedLogger("SmartAvgManager")


class SmartAvgManager:
    """
    Доменный менеджер механики Smart Avg (Smaragd).
    Контролирует глобальный макро-фильтр усреднений по дневным свечам (1d Dual EMA):
    - Блокирует ступени усреднения (по умолчанию L3-L8) при враждебном тренде:
        * SHORT блокируется при UP (восходящий тренд)
        * LONG блокируется при DOWN (нисходящий тренд)
    - Разрешает усреднение при FLAT, UNSTABLE, либо сонаправленном тренде.
    - Колена вне диапазона actual_levels (например, L1 и L2) никогда не блокируются,
      собирая локальный рыночный шум.
    - Ведет институциональную телеметрию заблокированных попыток и спасенной маржи.
    """

    def __init__(self, default_cfg: Optional[Dict[str, Any]] = None):
        self.default_cfg: Dict[str, Any] = copy.deepcopy(default_cfg) if isinstance(default_cfg, dict) else {}
        self._last_logged: Dict[Tuple[str, str, int, str], float] = {}
        self.stats: Dict[str, Any] = {
            "total_blocks": 0,
            "blocked_by_symbol": {},
            "blocked_by_level": {},
        }

    @staticmethod
    def parse_actual_levels(actual_levels: Any) -> Set[int]:
        """
        Парсит конфигурацию фильтруемых ступеней actual_levels.
        Поддерживает:
        - Списки/кортежи: ["L3", "L4", "L5", "L6", "L7", "L8"], [3, 4, 5, 6, 7, 8]
        - Диапазон-строку: "L3-L8", "3-8", "L3..L8"
        - Открытый диапазон: "L3+", "3+" (все уровни от 3 до 99)
        - Перечисление через запятую: "L3, L4, L5"
        - Число: 3 -> {3}
        """
        levels: Set[int] = set()
        if actual_levels is None:
            return {3, 4, 5, 6, 7, 8}

        if isinstance(actual_levels, (list, tuple, set)):
            for item in actual_levels:
                try:
                    s = str(item).upper().replace("L", "").strip()
                    if s.isdigit():
                        levels.add(int(s))
                except Exception:
                    continue
            return levels if levels else {3, 4, 5, 6, 7, 8}

        if isinstance(actual_levels, (int, float)):
            return {int(actual_levels)}

        if isinstance(actual_levels, str):
            clean = actual_levels.upper().replace("L", "").strip()
            # 1. Открытый диапазон "3+"
            if "+" in clean:
                base_s = clean.replace("+", "").strip()
                if base_s.isdigit():
                    base_idx = int(base_s)
                    return set(range(base_idx, 100))

            # 2. Диапазон "3-8" или "3..8"
            match_range = re.match(r"^(\d+)\s*[-.]{1,2}\s*(\d+)$", clean)
            if match_range:
                start_lvl = int(match_range.group(1))
                end_lvl = int(match_range.group(2))
                if start_lvl <= end_lvl:
                    return set(range(start_lvl, end_lvl + 1))
                return set(range(end_lvl, start_lvl + 1))

            # 3. Перечисление через запятую "3, 4, 5"
            if "," in clean:
                for part in clean.split(","):
                    p = part.strip()
                    if p.isdigit():
                        levels.add(int(p))
                return levels if levels else {3, 4, 5, 6, 7, 8}

            # 4. Одиночное число "3"
            if clean.isdigit():
                return {int(clean)}

        return {3, 4, 5, 6, 7, 8}

    def _resolve_cfg(
        self,
        symbol: str,
        side: str,
        state: Optional[Any] = None,
        runtime_manager: Optional[Any] = None
    ) -> Dict[str, Any]:
        """Разрешает конфигурацию Smaragd с учетом иерархии FSM state -> caches -> registry -> default_cfg."""
        if state is not None and getattr(state, "smaragd", None) and isinstance(state.smaragd, dict):
            return state.smaragd

        if runtime_manager and hasattr(runtime_manager, "caches") and symbol in runtime_manager.caches:
            side_dict = runtime_manager.caches[symbol].get(side, {})
            if "smaragd" in side_dict and isinstance(side_dict["smaragd"], dict):
                return side_dict["smaragd"]
            if getattr(runtime_manager, "strategy_registry", None):
                try:
                    strat = runtime_manager.strategy_registry.get_strategy_for_symbol(symbol)
                    if getattr(strat, "smaragd_cfg", None):
                        return strat.smaragd_cfg
                    if getattr(strat, "raw_meta", None) and "smaragd" in strat.raw_meta:
                        return strat.raw_meta["smaragd"]
                except Exception:
                    pass

        return self.default_cfg

    async def should_block_averaging(
        self,
        symbol: str,
        side: str,
        candidate_level: Union[int, str],
        state: Optional[Any] = None,
        runtime_manager: Optional[Any] = None,
        client: Optional[Any] = None,
    ) -> Tuple[bool, str, str]:
        """
        Оценивает возможность исполнения усреднения candidate_level с учетом 1d EMA тренда.
        Возвращает: (is_blocked: bool, reason: str, trend_state: str).
        """
        cfg = self._resolve_cfg(symbol, side, state, runtime_manager)
        if not cfg or not cfg.get("enabled", False):
            return False, "smaragd_disabled", "NONE"

        # Извлекаем номер колена
        try:
            lvl_clean = str(candidate_level).upper().replace("L", "").strip()
            lvl_idx = int(lvl_clean)
        except Exception:
            return False, f"invalid_candidate_level_{candidate_level}", "NONE"

        # Проверяем вхождение уровня в actual_levels (например, L3-L8)
        actual_set = self.parse_actual_levels(cfg.get("actual_levels", "L3-L8"))
        if lvl_idx not in actual_set:
            return False, f"level_L{lvl_idx}_out_of_smaragd_scope", "NONE"

        # Конфигурация дневного макро-тренда
        trend_cfg = cfg.get("trend_ema") or {
            "timeframe": "1d",
            "sma_fast": 10,
            "sma_slow": 30,
            "confirmation_candles": 3,
            "require_rising": True,
            "trend_positive": True
        }

        # Опрос тренда (O(1) в симуляторе через предрасчитанный кэш)
        trend_state = await MomentumEngine.get_ema_trend_state(
            symbol=symbol,
            ema_cfg=trend_cfg,
            client=client
        )

        side_u = str(side).upper().strip()
        trend_u = str(trend_state).upper().strip()
        tf = str(trend_cfg.get("timeframe", "1d")).strip()

        # Применение матрицы решений
        if side_u == "SHORT":
            if trend_u == "UP":
                self._record_block(symbol, side_u, lvl_idx, trend_u, tf)
                return True, f"SHORT averaging L{lvl_idx} blocked by hostile {tf} EMA trend ({trend_u})", trend_u
            return False, f"SHORT averaging L{lvl_idx} allowed on {trend_u} trend", trend_u
        elif side_u == "LONG":
            if trend_u == "DOWN":
                self._record_block(symbol, side_u, lvl_idx, trend_u, tf)
                return True, f"LONG averaging L{lvl_idx} blocked by hostile {tf} EMA trend ({trend_u})", trend_u
            return False, f"LONG averaging L{lvl_idx} allowed on {trend_u} trend", trend_u

        return False, f"unknown_side_{side_u}", trend_u

    def _record_block(self, symbol: str, side: str, lvl_idx: int, trend_u: str, tf: str = "1d") -> None:
        """Регистрирует статистику блокировки и логирует с троттлингом (не чаще 1 раза в минуту)."""
        self.stats["total_blocks"] += 1
        sym_key = f"{symbol}:{side}"
        self.stats["blocked_by_symbol"][sym_key] = self.stats["blocked_by_symbol"].get(sym_key, 0) + 1
        lvl_key = f"L{lvl_idx}"
        self.stats["blocked_by_level"][lvl_key] = self.stats["blocked_by_level"].get(lvl_key, 0) + 1

        throttle_key = (symbol, side, lvl_idx, trend_u)
        now_ts = time.time()
        last_logged = self._last_logged.get(throttle_key, 0.0)
        if now_ts - last_logged >= 60.0:
            self._last_logged[throttle_key] = now_ts
            logger.info(
                f"[{symbol}] {side} 🛑 [SMARAGD BLOCKED] Averaging L{lvl_idx} suppressed "
                f"by hostile {tf} EMA trend ({trend_u})"
            )

    def get_telemetry(self) -> Dict[str, Any]:
        """Возвращает агрегированную статистику работы Smaragd."""
        return copy.deepcopy(self.stats)

    def reset_stats(self) -> None:
        """Сбрасывает счетчики телеметрии."""
        self.stats["total_blocks"] = 0
        self.stats["blocked_by_symbol"].clear()
        self.stats["blocked_by_level"].clear()
        self._last_logged.clear()
