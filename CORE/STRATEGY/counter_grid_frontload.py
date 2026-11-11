# ==============================================================================
# Path: CORE/STRATEGY/counter_grid_frontload.py
# Role: Контр-разгон L0 за счёт авансового поглощения ближайших колен сетки L1..Lk
#       при глубокой просадке противоположной стороны (counter_grid_frontload).
# ==============================================================================

from typing import Any, Dict, List, Optional, Tuple
from c_log import UnifiedLogger
from CORE._utils import RiskCalculatingUtils
from CORE.ENTRY.signal import TimeControl

logger = UnifiedLogger("CounterGridFrontloader")


class CounterGridFrontloader:
    """
    Изолированный доменный модуль для режима `counter_grid_frontload`:
    при достижении противоположной (страдающей) стороной заданной глубины сетки
    (например, L6 / L7 / L8), профицирующая сторона при входе L0 авансово
    забирает объём ближайших k неисполненных колен усреднения L1..Lk,
    помечая эти колена как исполненные (`is_active=True`, `_absorbed_to_l0=True`).

    Инварианты:
    - Суммарный риск стороны по сетке L0..LN никогда не превышает 100% invest_size.
    - При откате после входа поглощённые уровни L1..Lk не усредняются повторно.
    - При preserve_l0_identity=True поглощённые колена не сдвигают расчётный
      шаг `cur_step` / `tp_map["0"]`, сохраняя поведение L0 (smart_grace / smart_size / smart_tp)
      до сработки следующего реального усреднения.
    """

    @staticmethod
    def resolve_opp_step(
        opp_state: Any,
        runtime_manager: Optional[Any] = None,
        symbol: Optional[str] = None,
        opp_side: Optional[str] = None,
    ) -> int:
        """
        Динамически определяет актуальный шаг усреднения противоположной стороны на лету
        по флагам `is_active` её рантайм-сетки.
        """
        if not opp_state:
            return 0
        opp_in_pos = bool(
            getattr(opp_state, "in_position", False)
            or getattr(opp_state, "in_position_papper", False)
        )
        if not opp_in_pos:
            return 0

        opp_grid = getattr(opp_state, "grid", None)
        has_active_in_state = isinstance(opp_grid, dict) and any(
            isinstance(v, dict) and v.get("is_active") for v in opp_grid.values()
        )
        if not has_active_in_state and runtime_manager and symbol and opp_side:
            caches = getattr(runtime_manager, "caches", None) or {}
            sym_cache = caches.get(symbol, {})
            if isinstance(sym_cache, dict):
                side_cache = sym_cache.get(opp_side, {})
                if isinstance(side_cache, dict) and isinstance(side_cache.get("grid"), dict):
                    opp_grid = side_cache["grid"]

        if isinstance(opp_grid, dict) and opp_grid:
            return int(RiskCalculatingUtils.get_current_grid_level(opp_grid, include_absorbed=False))
        return int(getattr(opp_state, "cur_step", 0))

    @staticmethod
    def validate_config(raw_cfg: Any, strategy_id: str = "") -> Dict[str, Any]:
        """Валидирует секцию паспорта `counter_grid_frontload` (Fail-Fast при enabled=True)."""
        if not raw_cfg or not isinstance(raw_cfg, dict):
            return {"enabled": False}
        if not raw_cfg.get("enabled", False):
            return dict(raw_cfg)

        tiers = raw_cfg.get("absorb_tiers")
        if not isinstance(tiers, dict) or not tiers:
            raise KeyError(
                f"[CounterGridFrontloader][{strategy_id}] Missing required 'absorb_tiers' mapping when enabled=True"
            )
        for rule, val in tiers.items():
            cnt = int(val)
            if cnt < 0:
                raise ValueError(
                    f"[CounterGridFrontloader][{strategy_id}] Invalid negative absorb count for rule '{rule}': {cnt}"
                )
        return dict(raw_cfg)

    @staticmethod
    def resolve_absorb_count(frontload_cfg: Optional[Dict[str, Any]], opp_step: int) -> int:
        """Возвращает количество ближайших уровней L1..Lk для вливания в L0 по карте absorb_tiers."""
        if not frontload_cfg or not frontload_cfg.get("enabled", False):
            return 0
        tiers = frontload_cfg.get("absorb_tiers")
        if not isinstance(tiers, dict) or not tiers:
            return 0

        max_count = 0
        for rule, raw_cnt in tiers.items():
            if TimeControl.match_rule(str(rule), opp_step):
                cnt = int(raw_cnt)
                if cnt > max_count:
                    max_count = cnt
        return max_count

    @classmethod
    def plan_entry_absorption(
        cls,
        frontload_cfg: Optional[Dict[str, Any]],
        side_grid: Optional[Dict[str, Any]],
        base_l0_pct: float,
        smart_size_mult: float,
        opp_in_pos: bool,
        opp_step: int,
    ) -> Tuple[float, List[str]]:
        """
        Рассчитывает итоговый процент входа L0 и список ключей уровней L1..Lk к поглощению.
        Возвращает: (effective_l0_pct, absorbed_level_keys).

        При активном поглощении:
        - Заимствуются первые k свободных уровней (enabled!=False, indent!=None, not is_active) с индексом > 0.
        - Итоговый процент L0 ограничен сверху суммой `base_l0_pct + absorbed_pct`, чтобы
          суммарный капитал стороны по L0..LN никогда не превышал 100% invest_size
          (даже если smart_size_mult > 1.0), но позволяет smart_size_mult < 1.0 уменьшать родную долю L0.
        """
        base_l0 = float(base_l0_pct)
        mult = float(smart_size_mult)
        if not frontload_cfg or not frontload_cfg.get("enabled", False) or not opp_in_pos:
            return base_l0 * mult, []

        absorb_count = cls.resolve_absorb_count(frontload_cfg, opp_step)
        if absorb_count <= 0 or not isinstance(side_grid, dict):
            return base_l0 * mult, []

        candidates = sorted(
            (
                str(k)
                for k, v in side_grid.items()
                if str(k).isdigit()
                and int(k) > 0
                and isinstance(v, dict)
                and v.get("enabled", True) is not False
                and v.get("indent") is not None
                and not v.get("is_active")
            ),
            key=lambda x: int(x),
        )
        absorbed_keys = candidates[:absorb_count]
        if not absorbed_keys:
            return base_l0 * mult, []

        absorbed_pct = sum(float(side_grid[k].get("volume") or 0.0) for k in absorbed_keys)
        max_l0_budget_pct = base_l0 + absorbed_pct
        eff_l0_pct = min(max_l0_budget_pct, base_l0 * mult + absorbed_pct)
        return round(eff_l0_pct, 6), absorbed_keys

    @staticmethod
    def apply_absorbed_levels(
        state: Any,
        absorbed_keys: Optional[List[str]],
        current_time_ms: int,
        preserve_l0_identity: bool = True,
    ) -> List[str]:
        """
        Помечает поглощённые в L0 уровни L1..Lk флагами `is_active=True` (и `_absorbed_to_l0=True`)
        строго ПОСЛЕ успешного исполнения рыночного ордера входа L0.
        """
        if not absorbed_keys or not isinstance(getattr(state, "grid", None), dict):
            return []

        applied: List[str] = []
        now_sec = current_time_ms / 1000.0
        for lvl_k in absorbed_keys:
            lvl_d = state.grid.get(str(lvl_k))
            if isinstance(lvl_d, dict) and not lvl_d.get("is_active"):
                lvl_d["is_active"] = True
                lvl_d["activated_at"] = now_sec
                lvl_d["timestamp"] = current_time_ms
                if preserve_l0_identity:
                    lvl_d["_absorbed_to_l0"] = True
                applied.append(str(lvl_k))

        if applied:
            logger.info(
                f"[{getattr(state, 'symbol', '')}] {getattr(state, 'side', '')} 🛡️ "
                f"[COUNTER FRONTLOAD APPLIED] Absorbed grid levels {applied} into L0 "
                f"(preserve_l0_identity={preserve_l0_identity})"
            )
        return applied

