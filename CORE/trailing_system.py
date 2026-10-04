# ============================================================
# FILE: CORE/trailing_system.py
# ROLE: Generic and Reusable Base Trailing System (TP, Grid & Ratchet)
# ============================================================

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("BaseTrailingSystem")


class BaseTrailingSystem:
    """
    Универсальная масштабируемая система трейлинга (Trailing Engine).
    Поддерживает:
      1. Портфельный трейлинг-храповик (Stagnation / Net Ratchet)
      2. Трейлинг Тейк-Профит (Trailing Take-Profit с поддержкой tp_purpose='neutral')
      3. Трейлинг Усреднение (Trailing Grid / Averaging на отскоке)
      4. Полную автонормализацию знаков (+/-): оператор может указывать как
         положительные, так и отрицательные числа для активаций и отступов.
    """

    @staticmethod
    def normalize_abs(val: Any, default: float = 0.0) -> float:
        """Безопасно преобразует значение в положительное число с плавающей точкой."""
        if val is None:
            return default
        try:
            return abs(float(val))
        except (ValueError, TypeError):
            return default

    @classmethod
    def evaluate_ratchet(
        cls,
        iter_net: float,
        iter_age_hours: float,
        ratchet_cfg: dict,
        state: dict,
    ) -> Tuple[bool, Optional[float], str, bool]:
        """
        Ступенчатый Трейлинг-Храповик по чистому Net (Фибо-лестница).
        Уровни задаются парами:
          - activation: целевой порог Net, взводящий гарантированный уровень пола.
          - floor: уровень гарантированной фиксации прибыли при откате.
          - min_age_hours: возрастной ценз (например 24.0ч для 0-го уровня эвакуации).
          - enabled: флаг активности уровня.
        Возвращает (should_close: bool, trigger_floor: Optional[float], info: str, is_stagnation: bool).
        """
        if not ratchet_cfg.get("enabled", True):
            return False, None, "", False
        levels = ratchet_cfg.get("levels", [])
        if not levels:
            return False, None, "", False

        state["highest_net"] = max(state.get("highest_net", 0.0), iter_net)

        for idx, lvl in enumerate(levels):
            if not lvl.get("enabled", True):
                continue
            min_age = float(lvl.get("min_age_hours") or 0.0)
            if iter_age_hours < min_age:
                continue
            act_val = float(lvl["activation"])
            floor_val = float(lvl["floor"])
            if iter_net >= act_val:
                cur_floor = state.get("armed_floor")
                if cur_floor is None or floor_val > cur_floor:
                    state["armed_floor"] = floor_val
                    state["active_level_idx"] = idx
                    state["armed_level_desc"] = lvl.get("_desc", f"L{idx}")

        armed_floor = state.get("armed_floor")
        if armed_floor is None:
            first_act = float(levels[0].get("activation", 5.0))
            return False, None, f"[RATCHET IDLE: Net {iter_net:+.2f} < Act {first_act:.2f}]", False

        idx = int(state.get("active_level_idx", 0))
        desc = state.get("armed_level_desc", f"L{idx}")
        lvl_min_age = float(levels[idx].get("min_age_hours") or 0.0) if idx < len(levels) else 0.0
        is_stag = (lvl_min_age > 0.0 or idx == 0)

        act_threshold = float(levels[idx]["activation"])
        # Полиморфный прямой триггер: если курок равен полу (или ниже), мгновенная фиксация при касании/перелёте
        if act_threshold <= armed_floor and iter_net >= act_threshold:
            high_net = float(state.get("highest_net", 0.0))
            info = (
                f"[TRAILING RATCHET INSTANT L{idx} '{desc}': Net {iter_net:+.2f} >= Target {armed_floor:.2f} "
                f"(High: {high_net:+.2f}, Age: {iter_age_hours:.1f}h)]"
            )
            return True, armed_floor, info, is_stag

        if iter_net <= armed_floor:
            high_net = float(state.get("highest_net", 0.0))
            info = (
                f"[TRAILING RATCHET L{idx} '{desc}': Net {iter_net:+.2f} <= Floor {armed_floor:.2f} "
                f"(Armed at >= {act_threshold:.2f}, High: {high_net:+.2f}, Age: {iter_age_hours:.1f}h)]"
            )
            return True, armed_floor, info, is_stag

        info = f"[ARMED L{idx} '{desc}': Floor {armed_floor:.2f} | Net {iter_net:+.2f} | Peak {state.get('highest_net', 0.0):.2f}]"
        return False, armed_floor, info, is_stag

    @classmethod
    def evaluate_trailing_tp(
        cls,
        gain_pct: float,
        tp_map: Dict[str, dict],
        state_trailing: dict,
        tp_purpose: str = "neutral",
        active_grid_level: str = "0",
    ) -> Tuple[bool, Optional[float], str]:
        """
        Трейлинг Тейк-Профит:
        - При gain_pct >= activation взводится пол (floor = fallback_indent или indent).
        - При откате gain_pct <= armed_floor срабатывает фиксация прибыли.
        - При tp_purpose == 'neutral': оценивает всю лестницу уровней tp_map (0..N).
        - При других tp_purpose: оценивает конкретный уровень active_grid_level.
        Возвращает (should_close: bool, trigger_floor: Optional[float], info: str).
        """
        if not tp_map:
            return False, None, ""

        state_trailing["highest_gain"] = max(state_trailing.get("highest_gain", 0.0), gain_pct)

        if tp_purpose == "neutral":
            levels_to_check = sorted(tp_map.keys(), key=lambda x: int(x) if x.isdigit() else x)
        else:
            if active_grid_level in tp_map:
                levels_to_check = [active_grid_level]
            elif tp_map:
                # Если усреднялка перевалила за последний уровень сетки, берем крайнее высшее значение тейка
                sorted_keys = sorted(tp_map.keys(), key=lambda x: int(x) if x.isdigit() else x)
                levels_to_check = [sorted_keys[-1]]
            else:
                levels_to_check = []

        for lvl_str in levels_to_check:
            lvl_cfg = tp_map.get(lvl_str)
            if not lvl_cfg:
                continue

            raw_act = lvl_cfg.get("activation")
            raw_floor = lvl_cfg.get("fallback_indent")
            if raw_floor is None:
                raw_floor = lvl_cfg.get("indent")

            if raw_act is not None:
                act_val = cls.normalize_abs(raw_act)
                floor_val = cls.normalize_abs(raw_floor, default=0.5)
                # Архитектурное правило: минимальный тейк не менее 0.5%
                floor_val = max(0.5, floor_val)

                if gain_pct >= act_val:
                    cur_floor = state_trailing.get("armed_floor")
                    if cur_floor is None or floor_val > cur_floor:
                        state_trailing["armed_floor"] = floor_val
                        state_trailing["active_level"] = lvl_str
            elif raw_floor is not None and tp_purpose != "neutral":
                # Шаг без трейлинга в классическом режиме: прямое срабатывание при достижении пола
                floor_val = max(0.5, cls.normalize_abs(raw_floor))
                if gain_pct >= floor_val:
                    info = f"[TP MARKET DIRECT: Gain {gain_pct:.2f}% >= Target {floor_val:.2f}%]"
                    return True, floor_val, info

        armed_floor = state_trailing.get("armed_floor")
        if armed_floor is None:
            return False, None, ""

        active_lvl = state_trailing.get("active_level", "0")
        active_lvl_cfg = tp_map.get(active_lvl, {})
        raw_act = active_lvl_cfg.get("activation")
        act_val = cls.normalize_abs(raw_act) if raw_act is not None else None

        # Полиморфный прямой триггер: если курок равен полу (или ниже), мгновенная фиксация при касании/перелёте
        if act_val is not None and act_val <= armed_floor and gain_pct >= act_val:
            info = (
                f"[TRAILING TP INSTANT L{active_lvl}: Gain {gain_pct:.2f}% >= Target {armed_floor:.2f}% "
                f"(Peak: {state_trailing.get('highest_gain', 0.0):.2f}%)]"
            )
            return True, armed_floor, info

        if gain_pct <= armed_floor:
            info = (
                f"[TRAILING TP L{active_lvl}: Gain {gain_pct:.2f}% <= Floor {armed_floor:.2f}% "
                f"(Peak: {state_trailing.get('highest_gain', 0.0):.2f}%)]"
            )
            return True, armed_floor, info

        info = f"[ARMED TP L{active_lvl}: Floor {armed_floor:.2f}% | Gain {gain_pct:.2f}%]"
        return False, armed_floor, info

    @classmethod
    def evaluate_trailing_grid(
        cls,
        drawdown_pct: float,
        grid: Dict[str, dict],
        state_trailing: dict,
    ) -> Tuple[Optional[str], List[str], str]:
        """
        Трейлинг Усреднение (Grid Averaging на отскоке):
        - drawdown_pct: положительная величина просадки от точки входа (|ΔP| / P_entry * 100).
        - Для каждого уровня сетки (начиная с 1):
            indent (floor) = уровень исполнения усреднения (например 8.0%).
            activation = порог более глубокой просадки для взведения курка (например 10.5%).
        - Если activation задан:
            При drawdown_pct >= activation курок взводится (armed_levels.add(lvl)).
            При отскоке цены (drawdown_pct <= floor) для взведенного уровня — уровень готов к исполнению.
        - Если activation равен None (или не задан):
            Обычный уровень: готов к исполнению сразу, как только drawdown_pct >= floor.

        Возвращает:
          (execute_level_str: Optional[str], skipped_levels: List[str], info: str)
        """
        if not grid:
            return None, [], ""

        armed_levels = state_trailing.setdefault("armed_levels", {})

        # Отслеживаем максимальную просадку в рамках позиции
        state_trailing["max_drawdown"] = max(state_trailing.get("max_drawdown", 0.0), drawdown_pct)

        # Сортируем неисполненные уровни (исключая начальный уровень 0)
        unexecuted_levels = [
            lvl_str for lvl_str in sorted(grid.keys(), key=lambda x: int(x) if x.isdigit() else x)
            if lvl_str != "0" and not grid[lvl_str].get("is_active")
        ]

        if not unexecuted_levels:
            return None, [], ""

        # 1. Проверяем взведение курков для уровней с трейлингом
        for lvl_str in unexecuted_levels:
            lvl_data = grid[lvl_str]
            raw_act = lvl_data.get("activation")
            if raw_act is not None:
                act_val = cls.normalize_abs(raw_act)
                if drawdown_pct >= act_val:
                    if lvl_str not in armed_levels:
                        armed_levels[lvl_str] = {
                            "armed_at_drawdown": drawdown_pct,
                            "deepest_drawdown": drawdown_pct,
                        }
                    else:
                        armed_levels[lvl_str]["deepest_drawdown"] = max(
                            armed_levels[lvl_str]["deepest_drawdown"], drawdown_pct
                        )

        # 2. Проверяем, какой уровень готов к исполнению
        # Если несколько уровней взведены и готовы, приоритет отдается уровню с более глубоким исполнением
        # (или уровню с наибольшим индексом), чтобы захватить более глубокую просадку.
        candidate_level = None
        for lvl_str in sorted(unexecuted_levels, key=lambda x: int(x) if x.isdigit() else x, reverse=True):
            lvl_data = grid[lvl_str]
            raw_floor = lvl_data.get("super_indent") or lvl_data.get("indent")
            floor_val = cls.normalize_abs(raw_floor)

            raw_act = lvl_data.get("activation")
            if raw_act is not None:
                act_val = cls.normalize_abs(raw_act)
                # Трейлинг-уровень: должен быть взведен
                if lvl_str in armed_levels:
                    # Полиморфный прямой триггер: если курок равен полу (или ниже), исполняется мгновенно без отскока
                    if act_val <= floor_val and drawdown_pct >= floor_val:
                        candidate_level = lvl_str
                        break
                    # Классический трейлинг: цена должна отскочить до пола (drawdown <= floor)
                    elif drawdown_pct <= floor_val:
                        candidate_level = lvl_str
                        break
            else:
                # Уровень без трейлинга: прямое срабатывание при достижении пола (drawdown >= floor)
                pass

        # Если трейлинговый отскок не найден, проверяем обычные нетрейлинговые уровни
        if candidate_level is None:
            for lvl_str in sorted(unexecuted_levels, key=lambda x: int(x) if x.isdigit() else x):
                lvl_data = grid[lvl_str]
                raw_act = lvl_data.get("activation")
                if raw_act is None:
                    raw_floor = lvl_data.get("super_indent") or lvl_data.get("indent")
                    floor_val = cls.normalize_abs(raw_floor)
                    if drawdown_pct >= floor_val:
                        candidate_level = lvl_str
                        break

        if candidate_level is None:
            return None, [], ""

        # 3. Вычисляем пропущенные (skipped) уровни:
        # Все неисполненные уровни с индексом меньше сработавшего candidate_level
        cand_idx = int(candidate_level) if candidate_level.isdigit() else -1
        skipped_levels = [
            lvl for lvl in unexecuted_levels
            if (int(lvl) if lvl.isdigit() else -1) < cand_idx
        ]

        # Очищаем сработавший уровень из armed_levels
        armed_levels.pop(candidate_level, None)
        for sk in skipped_levels:
            armed_levels.pop(sk, None)

        raw_floor = grid[candidate_level].get("super_indent") or grid[candidate_level].get("indent")
        floor_val = cls.normalize_abs(raw_floor)
        info = (
            f"[GRID TRIGGER L{candidate_level}: DD {drawdown_pct:.2f}% | Floor {floor_val:.2f}% | "
            f"Skipped {skipped_levels}]"
        )
        return candidate_level, skipped_levels, info
