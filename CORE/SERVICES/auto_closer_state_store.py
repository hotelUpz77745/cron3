# ==============================================================================
# Path: CORE/SERVICES/auto_closer_state_store.py
# Role: Персистентное хранилище состояния автоклозера (CACHE/auto_closer_state.json) и холодная гидрация из слепков эквити
# ==============================================================================

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ANALYTICS.equity_trajectory import DEFAULT_SNAPSHOT_STORE
from c_log import UnifiedLogger
from c_utils import Utils
from consts import CACHE_DIR
from CORE.STRATEGY.profit_closer_common import BucketedProfitHistorySampler

logger = UnifiedLogger("AutoCloserStateStore")

MAX_PERSISTED_HISTORY_SAMPLES = 5000


class AutoCloserStateStore:
    """
    Потокобезопасное (через atomic_write_json) хранилище оперативного состояния AutoCloser.
    Сохраняет накопленную бакетную историю (profit_history), вооружённый пол (armed_floor),
    Chronos-пол, активный режим (active_regime) и пик цикла (highest_net / peak_profit)
    в разрезе (strat_id, iteration_index), обеспечивая устойчивость к рестартам бота.
    """

    def __init__(
        self,
        state_file: Optional[Path] = None,
        snapshot_store: Optional[Any] = None,
    ):
        self.state_file: Path = Path(state_file) if state_file is not None else (CACHE_DIR / "auto_closer_state.json")
        self.snapshot_store: Any = DEFAULT_SNAPSHOT_STORE if snapshot_store is None else snapshot_store
        self._last_saved_sigs: Dict[str, Tuple[Any, ...]] = {}

    @staticmethod
    def _sanitize_state(state: Dict[str, Any]) -> Dict[str, Any]:
        """Подготавливает JSON-совместимую копию состояния итерации без мутации оригинала."""
        clean: Dict[str, Any] = {}
        for k, v in state.items():
            if k == "profit_history" and isinstance(v, list):
                trimmed = v[-MAX_PERSISTED_HISTORY_SAMPLES:]
                clean[k] = [round(float(x), 6) for x in trimmed]
            elif isinstance(v, (bool, int, float, str)) or v is None:
                clean[k] = v
            elif isinstance(v, (list, dict)):
                clean[k] = copy.deepcopy(v)
        return clean

    @staticmethod
    def _compute_signature(iter_idx: int, state: Dict[str, Any]) -> Tuple[Any, ...]:
        """Сигнатура критичных изменений для троттлинга дисковых записей."""
        hist = state.get("profit_history")
        hist_len = len(hist) if isinstance(hist, list) else 0
        armed_f = state.get("armed_floor")
        chronos_f = state.get("chronos_armed_floor")
        stag_f = state.get("armed_stagnation_floor")
        highest_q = int(float(state.get("highest_net") or 0.0) * 4.0)
        peak_q = int(float(state.get("peak_profit") or 0.0) * 4.0)
        stress_st = state.get("regime_stress_state")
        if not isinstance(stress_st, dict):
            stress_st = {}
        st_armed_f = stress_st.get("armed_floor")
        st_chronos_f = stress_st.get("chronos_armed_floor")
        st_highest_q = int(float(stress_st.get("highest_net") or 0.0) * 4.0)
        return (
            int(iter_idx),
            state.get("last_sample_bucket"),
            hist_len,
            round(float(armed_f), 4) if armed_f is not None else None,
            state.get("active_level_idx"),
            round(float(chronos_f), 4) if chronos_f is not None else None,
            state.get("regime") or state.get("active_regime"),
            state.get("regime_entered_at_sec") or state.get("stress_hold_until_ts"),
            round(float(st_armed_f), 4) if st_armed_f is not None else None,
            round(float(st_chronos_f), 4) if st_chronos_f is not None else None,
            st_highest_q,
            highest_q,
            peak_q,
            round(float(stag_f), 4) if stag_f is not None else None,
        )

    def _read_raw_payload(self) -> Dict[str, Any]:
        if not self.state_file.exists():
            return {"version": 1, "strategies": {}}
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return {"version": 1, "strategies": {}}
            strats = raw.get("strategies")
            if not isinstance(strats, dict):
                raw["strategies"] = {}
            return raw
        except Exception as e:
            logger.warning(f"[AUTO_CLOSER_STORE] Failed to read {self.state_file}: {e}. Starting clean.")
            return {"version": 1, "strategies": {}}

    def load_all(self) -> Dict[str, Dict[str, Any]]:
        """Загружает сохранённые состояния всех стратегий и глобального портфеля при старте."""
        raw = self._read_raw_payload()
        strategies = raw.get("strategies", {})
        restored: Dict[str, Dict[str, Any]] = {}
        for strat_id, entry in strategies.items():
            if not isinstance(entry, dict):
                continue
            st = entry.get("state")
            iter_idx = entry.get("iteration_index")
            if not isinstance(st, dict):
                continue
            st_copy = copy.deepcopy(st)
            if iter_idx is not None:
                st_copy["iteration_index"] = int(iter_idx)
            resolved_idx = int(st_copy.get("iteration_index", 1))
            restored[strat_id] = st_copy
            self._last_saved_sigs[strat_id] = self._compute_signature(resolved_idx, st_copy)
        if restored:
            logger.info(
                f"[AUTO_CLOSER_STORE] Restored persistent state for {len(restored)} scope(s) from {self.state_file.name}."
            )
        return restored

    def maybe_persist(
        self,
        strat_id: str,
        iter_idx: int,
        state: Dict[str, Any],
        force: bool = False,
    ) -> bool:
        """
        Сохраняет состояние на диск только при смене значимой сигнатуры
        (новый бакет, смена пола, смена режима, шаг пика >= $0.25 или смена итерации).
        """
        if not isinstance(state, dict):
            return False
        sig = self._compute_signature(iter_idx, state)
        if not force and self._last_saved_sigs.get(strat_id) == sig:
            return False

        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = self._read_raw_payload()
            payload.setdefault("version", 1)
            strategies = payload.setdefault("strategies", {})
            st_clean = self._sanitize_state(state)
            st_clean["iteration_index"] = int(iter_idx)
            strategies[strat_id] = {
                "iteration_index": int(iter_idx),
                "state": st_clean,
                "updated_ts_ms": int(time.time() * 1000),
            }
            Utils.atomic_write_json(self.state_file, payload, indent=2)
            self._last_saved_sigs[strat_id] = sig
            return True
        except Exception as e:
            logger.error(f"[AUTO_CLOSER_STORE][{strat_id}] Failed to persist state: {e}")
            return False

    def reset_strategy(
        self,
        strat_id: str,
        trailing_ratchet_state: Optional[Dict[str, dict]] = None,
    ) -> None:
        """Синхронно обнуляет состояние стратегии в RAM и на диске при закрытии итерации."""
        if isinstance(trailing_ratchet_state, dict) and strat_id in trailing_ratchet_state:
            trailing_ratchet_state[strat_id] = {}
        self._last_saved_sigs.pop(strat_id, None)

        if not self.state_file.exists():
            return
        try:
            payload = self._read_raw_payload()
            strategies = payload.get("strategies", {})
            if strat_id in strategies:
                strategies.pop(strat_id, None)
                Utils.atomic_write_json(self.state_file, payload, indent=2)
        except Exception as e:
            logger.error(f"[AUTO_CLOSER_STORE][{strat_id}] Failed to reset state on disk: {e}")

    @staticmethod
    def _resolve_interval_sec(mode: str, pos_cfg: Dict[str, Any]) -> Optional[int]:
        m_norm = str(mode).lower().strip()
        sec_cfg = pos_cfg.get(m_norm)
        if isinstance(sec_cfg, dict) and "sample_interval_sec" in sec_cfg:
            val = int(sec_cfg["sample_interval_sec"])
            return val if val > 0 else None
        if m_norm == "regime_adaptive" and isinstance(sec_cfg, dict):
            peace_cfg = sec_cfg.get("peace")
            if isinstance(peace_cfg, dict) and "sample_interval_sec" in peace_cfg:
                val = int(peace_cfg["sample_interval_sec"])
                return val if val > 0 else None
        return None

    def hydrate_from_snapshots(
        self,
        strat_id: str,
        iter_idx: int,
        mode: str,
        pos_cfg: Dict[str, Any],
        state: Dict[str, Any],
        iter_start_ts_ms: int,
        iter_start_equity: Optional[float],
        now_ms: int,
    ) -> int:
        """
        Холодная гидрация бакетной истории из DEFAULT_SNAPSHOT_STORE при первом запуске
        или потере кэша посередине активной итерации. Возвращает число добавленных бакетов.
        """
        if state.get("profit_history") or state.get("hydrated_from_snapshots"):
            return 0
        if not self.snapshot_store or iter_start_ts_ms <= 0 or iter_start_equity is None:
            return 0

        interval_sec = self._resolve_interval_sec(mode, pos_cfg)
        if interval_sec is None:
            return 0

        scope_id = "ALL" if strat_id == "__global__" else strat_id
        try:
            raw_pts = self.snapshot_store.get_snapshots(scope_id)
        except Exception as e:
            logger.debug(f"[AUTO_CLOSER_STORE][{strat_id}] Snapshot read skipped: {e}")
            return 0

        if not raw_pts:
            return 0

        cur_bucket = int((now_ms / 1000.0) // interval_sec)
        iter_pts: List[Tuple[int, float]] = []
        base_eq = float(iter_start_equity)
        for pt in raw_pts:
            if not isinstance(pt, (list, tuple)) or len(pt) < 2:
                continue
            ts_ms = int(pt[0])
            eq_val = float(pt[1])
            if iter_start_ts_ms <= ts_ms < now_ms:
                iter_pts.append((ts_ms, eq_val - base_eq))

        if not iter_pts:
            return 0
        iter_pts.sort(key=lambda item: item[0])

        step_ms = interval_sec * 1000
        added = 0
        # Генерируем сэмплы строго в границах наблюдаемых 5-минутных слепков без выдумывания экстремумов
        for idx, (t0, net0) in enumerate(iter_pts):
            b0 = int((t0 / 1000.0) // interval_sec)
            if b0 >= cur_bucket:
                continue
            did_add, _, _ = BucketedProfitHistorySampler.sample_bucket(
                state=state,
                iter_net=net0,
                now_sec=float(b0 * interval_sec),
                sample_interval_sec=interval_sec,
            )
            if did_add:
                added += 1

            if idx + 1 < len(iter_pts):
                t1, net1 = iter_pts[idx + 1]
                b1 = min(cur_bucket - 1, int((t1 / 1000.0) // interval_sec))
                dt_ms = max(1, t1 - t0)
                for b in range(b0 + 1, b1):
                    if added >= MAX_PERSISTED_HISTORY_SAMPLES:
                        break
                    tb_ms = b * step_ms
                    frac = max(0.0, min(1.0, (tb_ms - t0) / float(dt_ms)))
                    net_interp = net0 + (net1 - net0) * frac
                    did_i, _, _ = BucketedProfitHistorySampler.sample_bucket(
                        state=state,
                        iter_net=net_interp,
                        now_sec=float(b * interval_sec),
                        sample_interval_sec=interval_sec,
                    )
                    if did_i:
                        added += 1

        state["hydrated_from_snapshots"] = True
        state["iteration_index"] = int(iter_idx)
        if added > 0:
            logger.info(
                f"[AUTO_CLOSER_STORE][{strat_id}] Cold-hydrated {added} bucket sample(s) "
                f"from equity snapshots for Iteration #{iter_idx}."
            )
            self.maybe_persist(strat_id, iter_idx, state, force=True)
        return added
