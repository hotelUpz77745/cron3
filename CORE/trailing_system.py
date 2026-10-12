# ============================================================
# FILE: CORE/trailing_system.py
# ROLE: Generic and Reusable Base Trailing System (TP, Grid & Ratchet)
# ============================================================

import logging
import time
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
        Поддерживает автоматическое квантование (scale_factor) при масштабировании сайзов.
        Возвращает (should_close: bool, trigger_floor: Optional[float], info: str, is_stagnation: bool).
        """
        if not ratchet_cfg.get("enabled", True):
            return False, None, "", False
        levels = ratchet_cfg.get("levels", [])
        if not levels:
            return False, None, "", False

        scale_factor = float(ratchet_cfg.get("scale_factor", 1.0))
        if scale_factor <= 0:
            scale_factor = 1.0

        state["highest_net"] = max(state.get("highest_net", 0.0), iter_net)

        for idx, lvl in enumerate(levels):
            if not lvl.get("enabled", True):
                continue
            min_age = float(lvl.get("min_age_hours") or 0.0)
            if iter_age_hours < min_age:
                continue
            act_val = float(lvl["activation"]) * scale_factor
            floor_val = float(lvl["floor"]) * scale_factor
            if iter_net >= act_val:
                cur_floor = state.get("armed_floor")
                if cur_floor is None or floor_val > cur_floor:
                    state["armed_floor"] = floor_val
                    state["active_level_idx"] = idx
                    state["armed_level_desc"] = lvl.get("_desc", f"L{idx}")

        armed_floor = state.get("armed_floor")
        if armed_floor is None:
            first_act = float(levels[0].get("activation", 5.0)) * scale_factor
            return False, None, f"[RATCHET IDLE: Net {iter_net:+.2f} < Act {first_act:.2f}]", False

        idx = int(state.get("active_level_idx", 0))
        desc = state.get("armed_level_desc", f"L{idx}")
        lvl_min_age = float(levels[idx].get("min_age_hours") or 0.0) if idx < len(levels) else 0.0
        is_stag = (lvl_min_age > 0.0 or idx == 0)

        act_threshold = float(levels[idx]["activation"]) * scale_factor
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

            from CORE._utils import TakeProfitConfigResolver
            resolved_lvl = TakeProfitConfigResolver.resolve_level(lvl_cfg)
            raw_act = resolved_lvl.get("activation")
            raw_floor = resolved_lvl.get("fallback_indent")
            if raw_floor is None:
                raw_floor = resolved_lvl.get("indent")

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

                    # Маркируем курок прямо в структуре tp_map для прозрачной видимости и FSM
                    lvl_cfg["is_armed"] = True
                    lvl_cfg["armed_at_gain"] = round(gain_pct, 4)
                    lvl_cfg["armed_floor"] = floor_val
                    if "armed_timestamp" not in lvl_cfg or lvl_cfg["armed_timestamp"] is None:
                        lvl_cfg["armed_timestamp"] = int(time.time() * 1000)
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
        from CORE._utils import TakeProfitConfigResolver
        resolved_act_lvl = TakeProfitConfigResolver.resolve_level(active_lvl_cfg)
        raw_act = resolved_act_lvl.get("activation")
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

        # Сортируем неисполненные уровни (исключая начальный уровень 0 и отключенные уровни enabled=False)
        unexecuted_levels = [
            lvl_str for lvl_str in sorted(grid.keys(), key=lambda x: int(x) if x.isdigit() else x)
            if lvl_str != "0" and not grid[lvl_str].get("is_active") and grid[lvl_str].get("enabled", True) is not False
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
                    # Маркируем курок прямо в структуре сетки
                    lvl_data["is_armed"] = True
                    lvl_data["armed_at_drawdown"] = round(drawdown_pct, 4)
                    if "armed_timestamp" not in lvl_data or lvl_data["armed_timestamp"] is None:
                        lvl_data["armed_timestamp"] = int(time.time() * 1000)

        # 2. Проверяем, какой уровень готов к исполнению
        # Если несколько уровней взведены и готовы, приоритет отдается уровню с более глубоким исполнением
        # (или уровню с наибольшим индексом), чтобы захватить более глубокую просадку.
        candidate_level = None
        for lvl_str in sorted(unexecuted_levels, key=lambda x: int(x) if x.isdigit() else x, reverse=True):
            lvl_data = grid[lvl_str]
            raw_floor = lvl_data.get("super_indent") or lvl_data.get("indent")
            if raw_floor is None:
                continue
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
                    if raw_floor is None:
                        continue
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
        if candidate_level in grid and isinstance(grid[candidate_level], dict):
            grid[candidate_level]["is_armed"] = False
        for sk in skipped_levels:
            armed_levels.pop(sk, None)
            if sk in grid and isinstance(grid[sk], dict):
                grid[sk]["is_armed"] = False

        raw_floor = grid[candidate_level].get("super_indent") or grid[candidate_level].get("indent")
        floor_val = cls.normalize_abs(raw_floor)
        info = (
            f"[GRID TRIGGER L{candidate_level}: DD {drawdown_pct:.2f}% | Floor {floor_val:.2f}% | "
            f"Skipped {skipped_levels}]"
        )
        return candidate_level, skipped_levels, info


class ToxicityEngine:
    """
    Институциональный движок оценки токсичности инвентаря портфеля (Inventory Toxicity Engine).
    Поддерживает:
      1. Расчет Delta-Recovery Factor (степень рассасывания ямы просадки).
      2. Расчет нормированного Toxicity Score S in [0.0, 1.0].
      3. Адаптивное управление порогами выхода (Toxicity Adaptive - Режим 2).
      4. Синхронизацию с 23-часовым окном radar_synergy (Radar Sync 23h - Режим 3).
    """

    @staticmethod
    def calculate_delta_recovery(current_floating_pnl: float, max_drawdown_iteration: float) -> float:
        """
        Delta-Recovery Factor R_delta in [0.0, 1.0].
        Показывает, какая часть максимальной просадки итерации рассосалась.
        R_delta = 1.0 -> просадка полностью рассосалась (0$ убытка).
        R_delta = 0.0 -> сейчас находимся на самом дне ямы (пик просадки).
        """
        cur_loss = abs(min(0.0, float(current_floating_pnl)))
        peak_loss = abs(min(0.0, float(max_drawdown_iteration)))
        if peak_loss <= 1e-4:
            return 1.0 if cur_loss <= 1e-4 else 0.0
        rec = 1.0 - (cur_loss / peak_loss)
        return round(max(0.0, min(1.0, rec)), 4)

    @staticmethod
    def calculate_toxicity_score(
        margin_load: float,
        max_depth_ratio: float,
        float_ratio: float,
        delta_recovery: float,
        weights: Optional[Dict[str, float]] = None,
    ) -> float:
        """
        Нормированный Toxicity Score S in [0.0, 1.0].
        Чем выше S, тем токсичнее портфель и тем быстрее требуется эвакуация.
        """
        w = weights or {
            "margin_load": 0.35,
            "grid_depth": 0.30,
            "float_ratio": 0.25,
            "delta_recovery": 0.15,
        }
        m_load = max(0.0, min(1.0, float(margin_load)))
        depth = max(0.0, min(1.0, float(max_depth_ratio)))
        f_ratio = max(0.0, min(1.0, float(float_ratio)))
        d_rec = max(0.0, min(1.0, float(delta_recovery)))

        raw_score = (
            w.get("margin_load", 0.35) * m_load
            + w.get("grid_depth", 0.30) * depth
            + w.get("float_ratio", 0.25) * f_ratio
            - w.get("delta_recovery", 0.15) * d_rec
        )
        return round(max(0.0, min(1.0, raw_score)), 4)

    @classmethod
    def evaluate_toxicity_adaptive(
        cls,
        iter_net: float,
        iter_age_hours: float,
        ratchet_cfg: dict,
        state: dict,
        toxicity_score: float,
        delta_recovery: float,
        scale_factor: float = 1.0,
    ) -> Tuple[bool, Optional[float], str, bool]:
        """
        Способ 2: Адаптивный Фикс по СКОРУ и СПАДУ токсичности инвентаря.
        Прямой сброс всей корзины в 100% USDT кэш без ожидания отката трейлинга:
        - ЖЕЛЕЗНЫЙ ИНВАРИАНТ: iter_net ОБЯЗАН быть строго положительным (iter_net >= floor > 0). В минус не закрываем никогда!
        1. Высокая токсичность (S >= panic_th, дефолт 0.70):
           - При рассасывании ямы (delta_recovery >= min_panic_recovery, дефолт 0.40)
             и iter_net >= emergency_floor (дефолт +3.0 USDT * scale) -> экстренный сброс в кэш!
        2. Умеренная токсичность (relaxed_th <= S < panic_th, дефолт 0.35..0.70):
           - При рассасывании ямы (delta_recovery >= min_moderate_recovery, дефолт 0.20)
             и iter_net >= moderate_floor (дефолт +8.0 USDT * scale) -> рабочий сброс в кэш!
        3. Низкая токсичность / Легкий рынок (S < relaxed_th, дефолт < 0.35):
           - Инвентарь не давит, маржа свободна.
             При iter_net >= relaxed_floor (дефолт +15.0 USDT * scale) -> целевой сброс в кэш!
        """
        # Железная защита от минуса: в положительной ветке автоклозера минус запрещен абсолютно
        if iter_net <= 0.0:
            return False, None, "", False

        t_cfg = ratchet_cfg.get("toxicity_adaptive", {})
        panic_th = float(t_cfg.get("panic_toxicity_threshold", 0.70))
        relaxed_th = float(t_cfg.get("relaxed_toxicity_threshold", 0.35))

        # Минимальные целевые пороги чистого профита (с учетом масштаба портфеля)
        emergency_floor = max(1.0, round(float(t_cfg.get("emergency_exit_floor_usdt", 3.0)) * scale_factor, 2))
        moderate_floor = max(2.0, round(float(t_cfg.get("moderate_exit_floor_usdt", 8.0)) * scale_factor, 2))
        relaxed_floor = max(3.0, round(float(t_cfg.get("relaxed_exit_floor_usdt", 15.0)) * scale_factor, 2))

        # 1. ВЫСОКАЯ ТОКСИЧНОСТЬ (ПАНИКА / НАВЕС МАРЖИ)
        if toxicity_score >= panic_th:
            min_panic_rec = float(t_cfg.get("min_panic_recovery", 0.40))
            if delta_recovery >= min_panic_rec and iter_net >= emergency_floor:
                info = (
                    f"[TOXICITY EVACUATION: Score {toxicity_score:.2f} >= {panic_th:.2f} | "
                    f"Recovery {delta_recovery:.0%} >= {min_panic_rec:.0%} | "
                    f"Net {iter_net:+.2f} >= Floor {emergency_floor:.2f} USDT]"
                )
                return True, emergency_floor, info, True

        # 2. УМЕРЕННАЯ ТОКСИЧНОСТЬ (РАБОЧАЯ СЕТКА)
        elif toxicity_score >= relaxed_th:
            min_mod_rec = float(t_cfg.get("min_moderate_recovery", 0.20))
            if delta_recovery >= min_mod_rec and iter_net >= moderate_floor:
                info = (
                    f"[TOXICITY MODERATE FIX: Score {toxicity_score:.2f} | "
                    f"Recovery {delta_recovery:.0%} >= {min_mod_rec:.0%} | "
                    f"Net {iter_net:+.2f} >= Floor {moderate_floor:.2f} USDT]"
                )
                return True, moderate_floor, info, False

        # 3. НИЗКАЯ ТОКСИЧНОСТЬ (ЛЕГКИЙ РЫНОК / 0-1 ШАГИ)
        else:
            if iter_net >= relaxed_floor:
                info = (
                    f"[TOXICITY LIGHT FIX: Score {toxicity_score:.2f} < {relaxed_th:.2f} | "
                    f"Net {iter_net:+.2f} >= Floor {relaxed_floor:.2f} USDT]"
                )
                return True, relaxed_floor, info, False

        return False, None, "", False

    @classmethod
    def evaluate_fixed_net(
        cls,
        iter_net: float,
        fixed_cfg: dict,
        scale_factor: float = 1.0,
    ) -> Tuple[bool, Optional[float], str]:
        """
        Режим 4: Фиксация по фиксированному значению чистого Net профита текущей итерации.
        Без трейлинга, без ожидания отката: как только iter_net >= target_net_usdt,
        немедленно фиксируем прибыль и сбрасываем корзину позиций в 100% USDT кэш.
        """
        if not fixed_cfg.get("enabled", True):
            return False, None, ""

        if "target_net_usdt" not in fixed_cfg:
            raise KeyError("[TrailingSystem] В конфигурации fixed_net отсутствует обязательный параметр 'target_net_usdt'!")
        raw_target = float(fixed_cfg["target_net_usdt"])
        if fixed_cfg.get("scale_by_invest_size", False):
            target_fixed = round(raw_target * scale_factor, 2)
        else:
            target_fixed = raw_target

        if iter_net >= target_fixed:
            info = f"[FIXED NET PROFIT TARGET: Net {iter_net:+.2f} >= Target {target_fixed:.2f} USDT]"
            return True, target_fixed, info

        return False, None, ""

