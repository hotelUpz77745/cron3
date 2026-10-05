# ============================================================
# FILE: CORE/STRATEGY/momentum_engine.py
# ROLE: Mathematical engine for Smart Grace, Smart Size, Knife Blocker, and Smart TP
# ============================================================

from typing import Dict, Any, Optional
from c_log import UnifiedLogger

logger = UnifiedLogger("MomentumEngine")


class MomentumEngine:
    """
    Чистый математический движок для расчета импульсных адаптивных параметров:
    - Smart Grace: расчет длительности окна по карточке приращений
    - Smart Size: расчет коэффициента объема позиции
    - Knife Blocker: блокировка входа на противоположном пампе/дампе
    - Smart TP: динамическое расширение тейк-профита при частом исполнении
    """

    @classmethod
    def match_rule(cls, rule: str, count: int) -> bool:
        """
        Проверяет соответствие числа закрытых сделок правилу строки.
        Поддерживает операторы: '<', '<=', '==', '>=', '>'.
        """
        rule = rule.strip()
        try:
            if rule.startswith("<="):
                return count <= int(rule[2:])
            elif rule.startswith(">="):
                return count >= int(rule[2:])
            elif rule.startswith("=="):
                return count == int(rule[2:])
            elif rule.startswith("<"):
                return count < int(rule[1:])
            elif rule.startswith(">"):
                return count > int(rule[1:])
            elif rule.isdigit():
                return count == int(rule)
        except ValueError:
            logger.warning(f"Invalid rule format in MomentumEngine: '{rule}'")
        return False

    @classmethod
    def calculate_smart_grace(cls, closed_count: int, smart_grace_cfg: Dict[str, Any]) -> float:
        """
        Рассчитывает длительность окна Smart Grace по карточке increments_card.
        Базовый период: start_period_sec.
        Если карточка не задана, возвращает start_period_sec.
        """
        start_period = float(smart_grace_cfg["start_period_sec"])
        increments_card = smart_grace_cfg.get("increments_card", {})
        if not increments_card:
            return start_period

        max_extra = 0.0
        for rule, extra_sec in increments_card.items():
            if cls.match_rule(rule, closed_count):
                extra_val = float(extra_sec)
                if extra_val > max_extra:
                    max_extra = extra_val

        return start_period + max_extra

    @classmethod
    def calculate_smart_size_weight(cls, closed_count: int, smart_size_cfg: Optional[Dict[str, Any]]) -> float:
        """
        Рассчитывает множитель объема сетки на основе профитабельности стороны.
        По умолчанию 1.00x.
        Пример карточки: <2 -> 1.00, ==2 -> 1.25, ==3 -> 1.35, >=4 -> 0.75.
        """
        if not smart_size_cfg:
            return 1.00

        weights_card = smart_size_cfg.get("profiting_size_weights", {})
        if not weights_card:
            return 1.00

        for rule, weight in weights_card.items():
            if cls.match_rule(rule, closed_count):
                return float(weight)

        return 1.00

    @classmethod
    def calculate_smart_size_multiplier(cls, closed_count: int, smart_size_cfg: Optional[Dict[str, Any]]) -> float:
        """Алиас для calculate_smart_size_weight для полной совместимости с bot.py."""
        return cls.calculate_smart_size_weight(closed_count, smart_size_cfg)

    @classmethod
    def is_knife_blocked(cls, opposite_closed_count: int, smart_size_cfg: Optional[Dict[str, Any]]) -> bool:
        """
        Проверяет правило Knife Blocker (Cross-Side Awareness).
        Поддерживает форматы:
        - 'knife_blocker_enabled': false / true
        - 'knife_blocker': {'enabled': false / true, 'threshold': 2}
        Если выключено, возвращает False.
        Если включено и opposite_closed_count >= threshold, блокирует вход в Шаг 0.
        """
        if not smart_size_cfg:
            return False

        knife_cfg = smart_size_cfg.get("knife_blocker")
        if isinstance(knife_cfg, dict):
            if not knife_cfg.get("enabled", True):
                return False
            threshold = int(knife_cfg.get("threshold", 2))
        else:
            if not smart_size_cfg.get("knife_blocker_enabled", True):
                return False
            threshold = int(smart_size_cfg.get("knife_blocker_threshold", 2))

        return opposite_closed_count >= threshold

    @classmethod
    def calculate_smart_tp_multiplier(cls, closed_count: int, smart_tp_cfg: Optional[Dict[str, Any]]) -> float:
        """
        Рассчитывает множитель Take-Profit (и страховочного Fallback TP)
        в зависимости от частоты закрытий в пределах eval_window_sec.
        Если closed_count == 0 (спокойный рынок) -> 1.00x.
        """
        if not smart_tp_cfg or not smart_tp_cfg.get("enabled", True) or closed_count == 0:
            return 1.00

        increments = smart_tp_cfg.get("increments_tp", {})
        if not increments:
            return 1.00

        matched_multiplier = 1.00
        for rule, mult in increments.items():
            if cls.match_rule(rule, closed_count):
                mult_val = float(mult)
                if mult_val > matched_multiplier:
                    matched_multiplier = mult_val

        return matched_multiplier
