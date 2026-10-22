# CORE/STRATEGY/toxicity_engine.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.STRATEGY.toxicity_engine
ROLE: Институциональный движок оценки токсичности инвентаря портфеля (Inventory Toxicity Engine)
==============================================================================
"""

from typing import Dict, Any, Optional, Tuple
from c_log import UnifiedLogger

logger = UnifiedLogger("ToxicityEngine")


class ToxicityEngine:
    """
    Институциональный движок оценки токсичности инвентаря портфеля (Inventory Toxicity Engine).
    Поддерживает:
      1. Расчет Delta-Recovery Factor (степень рассасывания ямы просадки).
      2. Расчет нормированного Toxicity Score S in [0.0, 1.0].
      3. Адаптивное управление порогами выхода (Toxicity Adaptive - Режим 2).
      4. Фиксацию по фиксированному значению чистого Net профита (Fixed Net - Режим 4).
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
        scale_money: float = 1.0,
    ) -> Tuple[bool, Optional[float], str, bool]:
        """
        Адаптивный Фикс по СКОРУ и СПАДУ токсичности инвентаря.
        Прямой сброс всей корзины в 100% USDT кэш без ожидания отката трейлинга:
        - ЖЕЛЕЗНЫЙ ИНВАРИАНТ: iter_net ОБЯЗАН быть строго положительным (iter_net >= floor > 0). В минус не закрываем никогда!
        1. Высокая токсичность (S >= panic_th, дефолт 0.70):
           - При рассасывании ямы (delta_recovery >= min_panic_recovery, дефолт 0.40)
             и iter_net >= emergency_floor (дефолт +3.0 USDT * scale_money) -> экстренный сброс в кэш!
        2. Умеренная токсичность (relaxed_th <= S < panic_th, дефолт 0.35..0.70):
           - При рассасывании ямы (delta_recovery >= min_moderate_recovery, дефолт 0.20)
             и iter_net >= moderate_floor (дефолт +8.0 USDT * scale_money) -> рабочий сброс в кэш!
        3. Низкая токсичность / Легкий рынок (S < relaxed_th, дефолт < 0.35):
           - Инвентарь не давит, маржа свободна.
             При iter_net >= relaxed_floor (дефолт +15.0 USDT * scale_money) -> целевой сброс в кэш!
        """
        if iter_net <= 0.0:
            return False, None, "", False

        eff_scale = scale_factor if scale_factor != 1.0 else scale_money
        eff_scale = float(ratchet_cfg.get("scale_money", ratchet_cfg.get("scale_factor", eff_scale)))
        if eff_scale <= 0:
            eff_scale = 1.0

        t_cfg = ratchet_cfg.get("toxicity_adaptive", {})
        panic_th = float(t_cfg.get("panic_toxicity_threshold", 0.70))
        relaxed_th = float(t_cfg.get("relaxed_toxicity_threshold", 0.35))

        emergency_floor = round(float(t_cfg.get("emergency_exit_floor_usdt", 3.0)) * eff_scale, 2)
        moderate_floor = round(float(t_cfg.get("moderate_exit_floor_usdt", 8.0)) * eff_scale, 2)
        relaxed_floor = round(float(t_cfg.get("relaxed_exit_floor_usdt", 15.0)) * eff_scale, 2)

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
        scale_money: float = 1.0,
    ) -> Tuple[bool, Optional[float], str]:
        """
        Режим 4: Фиксация по фиксированному значению чистого Net профита текущей итерации.
        Без трейлинга, без ожидания отката: как только iter_net >= target_net_usdt,
        немедленно фиксируем прибыль и сбрасываем корзину позиций в 100% USDT кэш.
        """
        if not fixed_cfg.get("enabled", True):
            return False, None, ""

        if "target_net_usdt" not in fixed_cfg:
            raise KeyError("[ToxicityEngine] В конфигурации fixed_net отсутствует обязательный параметр 'target_net_usdt'!")
        eff_scale = scale_factor if scale_factor != 1.0 else scale_money
        eff_scale = float(fixed_cfg.get("scale_money", fixed_cfg.get("scale_factor", eff_scale)))
        if eff_scale <= 0:
            eff_scale = 1.0

        raw_target = float(fixed_cfg["target_net_usdt"])
        target_fixed = round(raw_target * eff_scale, 2)

        if iter_net >= target_fixed:
            info = f"[FIXED NET PROFIT TARGET: Net {iter_net:+.2f} >= Target {target_fixed:.2f} USDT]"
            return True, target_fixed, info

        return False, None, ""
