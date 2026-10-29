# ANALYTICS/analytics_fsm.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: ANALYTICS.analytics_fsm
ROLE: 4-State Analytics Finite State Machine (FSM), Iteration State Preserver,
      and Auto-Close History Idempotency Guard.
==============================================================================
"""

import json
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from c_log import UnifiedLogger
from c_utils import Utils

logger = UnifiedLogger("AnalyticsFSM")


class AnalyticsState(Enum):
    """
    4 состояния машины состояния аналитики:
    1. RESTORED_FROZEN: Инициализация/рестарт бота. С диска загружен замороженный
       кэш аналитики и итераций (или создан с нуля от стартового депозита, если кэш удален).
       Пересчет метрик строго заблокирован (_floating_synced = False).
    2. AWAITING_API: Ожидание первого подтвержденного ответа от биржи по REST API
       (fetch_account_info). Никакие дефолты или обнуления не допускаются.
    3. LIVE_SYNCED: Получен подтвержденный ответ биржи, рассчитан реальный
       плавающий PnL по позициям, пересчитаны метрики и сохранены на диск.
    4. AUTOCLOSER_READY: Аналитика подтверждена и верифицирована. Только в этом
       состоянии разрешается оценка и исполнение логики AutoCloser.
    """
    RESTORED_FROZEN = 1
    AWAITING_API = 2
    LIVE_SYNCED = 3
    AUTOCLOSER_READY = 4


class AnalyticsFSM:
    """
    Координатор состояний аналитики, персистентности итераций и идемпотентности автоклозера.
    - Гарантирует железное правило: AutoCloser разрешен ТОЛЬКО в состоянии 4 (AUTOCLOSER_READY).
    - Предотвращает сброс iteration_index в 1 при рестартах (подтягивает CACHE/iteration_state.json).
    - При отсутствии кэша инициализирует базу от стартового депозита, обнуляя остальные метрики.
    - Ведет историю автозакрытий CACHE/auto_close_history.json для защиты от ложных повторов.
    - Очищает прогресс курков (ratchet triggers) при переходе на новую итерацию.
    """

    def __init__(self, stats_dir: Path, cache_dir: Path, log_file: Path):
        self.stats_dir = Path(stats_dir)
        self.cache_dir = Path(cache_dir)
        self.log_file = Path(log_file)
        self.history_file = self.cache_dir / "auto_close_history.json"
        self.iteration_file = self.cache_dir / "iteration_state.json"

        self.state: AnalyticsState = AnalyticsState.RESTORED_FROZEN
        self.api_sync_count: int = 0
        self.last_api_sync_ts: Optional[float] = None
        self._persisted_global_cache: dict = {}
        self._persisted_strat_cache: dict[str, dict] = {}
        self._auto_close_history: dict[str, list] = {}
        self._iteration_state: dict[str, dict] = {}

    def restore_on_boot(self, initial_start_balance: float = 600.0) -> None:
        """
        Восстановление состояния аналитики при запуске/рестарте (Состояние 1: RESTORED_FROZEN):
        - Если кэш удален или не наработан: создает файл от initial_start_balance, обнуляя PnL.
        - Если кэш существует: считывает замороженное состояние с диска.
        - Подтягивает CACHE/iteration_state.json, гарантируя отсутствие отката итераций к 1.
        - Загружает CACHE/auto_close_history.json.
        - Выставляет _floating_synced = False и блокирует любые обновления до REST-ответа.
        """
        self.state = AnalyticsState.RESTORED_FROZEN
        self._load_iteration_cache()
        self._load_auto_close_history()

        # 1. Проверка наличия или создание холодного кэша analytics.json
        if not self.log_file.exists():
            self._create_cold_start_analytics(initial_start_balance)

        # 2. Загрузка глобальной аналитики из JSON
        try:
            if self.log_file.exists():
                self._persisted_global_cache = json.loads(self.log_file.read_text(encoding="utf-8"))
                self._persisted_global_cache["_floating_synced"] = False
                cached_g_idx = self._iteration_state.get("__global__", {}).get("iteration_index")
                if cached_g_idx and int(cached_g_idx) > int(self._persisted_global_cache.get("iteration_index", 1)):
                    self._persisted_global_cache["iteration_index"] = int(cached_g_idx)
        except Exception as e:
            logger.error(f"[AnalyticsFSM] Error restoring global analytics: {e}")

        # 3. Загрузка аналитики стратегий
        try:
            if self.stats_dir.exists():
                for sf in self.stats_dir.glob("analytics_*.json"):
                    try:
                        strat_id = sf.stem.replace("analytics_", "")
                        strat_d = json.loads(sf.read_text(encoding="utf-8"))
                        strat_d["_floating_synced"] = False
                        cached_s_idx = self._iteration_state.get(strat_id, {}).get("iteration_index")
                        if cached_s_idx and int(cached_s_idx) > int(strat_d.get("iteration_index", 1)):
                            strat_d["iteration_index"] = int(cached_s_idx)
                        self._persisted_strat_cache[strat_id] = strat_d
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"[AnalyticsFSM] Error restoring strategy analytics: {e}")

        cur_bal = self._persisted_global_cache.get("cur_balance_usdt", initial_start_balance)
        net_prof = self._persisted_global_cache.get("net_profit_usdt", 0.0)
        logger.info(
            f"[AnalyticsFSM] 🛡️ [STATE 1: RESTORED_FROZEN] Analytics restored: Balance ${cur_bal}, Net ${net_prof}. "
            f"All metrics locked until first verified REST response."
        )

    def _create_cold_start_analytics(self, initial_start_balance: float) -> None:
        """Создает первичный холодный кэш аналитики от стартового баланса с нулевыми PnL."""
        now_ms = int(time.time() * 1000)
        init_bal = round(float(initial_start_balance), 4)
        cold_data = {
            "start_balance_usdt": init_bal,
            "_rebased_start_balance_usdt": init_bal,
            "first_trade_ts": now_ms,
            "cur_balance_usdt": init_bal,
            "peak_balance_usdt": init_bal,
            "min_balance_usdt": init_bal,
            "_current_trough_usdt": init_bal,
            "total_trades": 0,
            "winning_trades": 0,
            "winrate_pct": 0.0,
            "realized_pnl_usdt": 0.0,
            "net_profit_usdt": 0.0,
            "unrealized_pnl_usdt": 0.0,
            "max_drawdown_usdt": 0.0,
            "max_drawdown_pct": 0.0,
            "recovery_factor": 0.0,
            "iteration_index": 1,
            "iteration_start_net_profit_usdt": 0.0,
            "iteration_net_profit_usdt": 0.0,
            "iteration_start_ts": now_ms,
            "iteration_peak_net_profit_usdt": 0.0,
            "iteration_peak_ts": now_ms,
            "iteration_peak_age_hours": 0.0,
            "iteration_net_velocity": 0.0,
            "per_coin": {},
            "_floating_synced": False,
        }
        self.stats_dir.mkdir(parents=True, exist_ok=True)
        Utils.atomic_write_json(self.log_file, cold_data, indent=4)
        logger.info(
            f"[AnalyticsFSM] 🆕 Cold start analytics created at {self.log_file} "
            f"from start balance ${init_bal}. All metrics zeroed."
        )

    def transition_to_awaiting_api(self) -> None:
        """Переход в Состояние 2: Ожидание ответа REST API биржи."""
        if self.state in (AnalyticsState.RESTORED_FROZEN, AnalyticsState.AUTOCLOSER_READY):
            self.state = AnalyticsState.AWAITING_API
            logger.info("[AnalyticsFSM] ⏳ [STATE 2: AWAITING_API] Waiting for verified Binance REST response.")

    def on_api_sync_success(self, data: Optional[dict] = None) -> None:
        """Переход в Состояние 3: Ответ получен, плавающий PnL рассчитан."""
        self.last_api_sync_ts = time.time()
        self.api_sync_count += 1
        self.state = AnalyticsState.LIVE_SYNCED
        logger.info(f"[AnalyticsFSM] ⚡ [STATE 3: LIVE_SYNCED] Sync #{self.api_sync_count} verified.")

    def transition_to_autocloser_ready(self) -> None:
        """Переход в Состояние 4: Метрики сохранены, AutoCloser разрешен."""
        if self.state in (AnalyticsState.LIVE_SYNCED, AnalyticsState.AWAITING_API):
            self.state = AnalyticsState.AUTOCLOSER_READY
            logger.info("[AnalyticsFSM] 🚀 [STATE 4: AUTOCLOSER_READY] AutoCloser unlocked and armed.")

    def on_api_error(self, err_msg: str) -> None:
        """При ошибке API или потере связи автоклозер разоружается в Состояние 2."""
        if self.state == AnalyticsState.AUTOCLOSER_READY:
            self.state = AnalyticsState.AWAITING_API
            logger.warning(f"[AnalyticsFSM] ⚠️ API error: {err_msg}. Disarming AutoCloser -> AWAITING_API.")

    def is_autocloser_ready(self) -> bool:
        """Строгая проверка: AutoCloser разрешен ТОЛЬКО в 4-м состоянии."""
        return self.state == AnalyticsState.AUTOCLOSER_READY

    # --------------------------------------------------------------------------
    # КЭШ ИТЕРАЦИЙ И АВТОЗАКРЫТИЙ
    # --------------------------------------------------------------------------

    def _load_iteration_cache(self) -> None:
        try:
            if self.iteration_file.exists():
                self._iteration_state = json.loads(self.iteration_file.read_text(encoding="utf-8"))
            else:
                self._iteration_state = {}
        except Exception as e:
            logger.error(f"[AnalyticsFSM] Error reading iteration cache: {e}")
            self._iteration_state = {}

    def _load_auto_close_history(self) -> None:
        try:
            if self.history_file.exists():
                self._auto_close_history = json.loads(self.history_file.read_text(encoding="utf-8"))
            else:
                self._auto_close_history = {}
        except Exception as e:
            logger.error(f"[AnalyticsFSM] Error reading auto-close history: {e}")
            self._auto_close_history = {}

    def get_iteration_index(self, strat_id: str) -> int:
        """Возвращает актуальный номер итерации, гарантируя отсутствие отката к 1."""
        if strat_id in self._iteration_state:
            idx = self._iteration_state[strat_id].get("iteration_index")
            if idx is not None and int(idx) >= 1:
                return int(idx)
        if strat_id in self._persisted_strat_cache:
            idx = self._persisted_strat_cache[strat_id].get("iteration_index")
            if idx is not None and int(idx) >= 1:
                return int(idx)
        return 1

    def is_iteration_closed(self, strat_id: str, iter_idx: int) -> bool:
        """
        Проверка идемпотентности автозакрытия:
        Возвращает True, если итерация iter_idx стратегии strat_id уже была закрыта ранее.
        Предотвращает ложный повторный триггер закрытия при рестартах при наличии накопленного профита.
        """
        records = self._auto_close_history.get(strat_id, [])
        for entry in records:
            if isinstance(entry, dict) and entry.get("iteration_index") == iter_idx:
                return True
        return False

    def get_iteration_baseline_net(self, strat_id: str, current_iter_idx: Optional[int] = None) -> float:
        """
        Возвращает кумулятивный базовый Нет Профит (anchor) начала текущей итерации.
        Гарантирует, что автоклозер считает строго Нет Профит своей текущей итерации,
        а не весь исторический профит портфеля.
        
        Если в истории CACHE/auto_close_history.json закрыто N итераций (например 3 раза по $10 = $30),
        базовый профит 4-й итерации равен $30.0.
        При текущем глобальном Нет профите $33.0, чистый профит 4-й итерации составит строго $3.0 ($33 - $30).
        """
        records = self._auto_close_history.get(strat_id, [])
        baseline_from_history = 0.0
        target_iter = current_iter_idx or self.get_iteration_index(strat_id)
        if records:
            prior_records = [
                r for r in records
                if isinstance(r, dict) and int(r.get("iteration_index", 0)) < target_iter
            ]
            if prior_records:
                prior_records.sort(key=lambda r: int(r.get("iteration_index", 0)))
                last_closed = prior_records[-1]
                if "strat_net" in last_closed and last_closed["strat_net"] is not None:
                    baseline_from_history = float(last_closed["strat_net"])
                elif "iter_net" in last_closed:
                    baseline_from_history = sum(float(r.get("iter_net", 0.0)) for r in prior_records)

        state_entry = self._iteration_state.get(strat_id, {})
        cached_start = state_entry.get("iteration_start_net_profit_usdt")
        baseline_from_state = float(cached_start) if cached_start is not None else 0.0
        return max(baseline_from_history, baseline_from_state)

    def mark_iteration_closed(self, strat_id: str, iter_idx: int, details: dict) -> None:
        """
        Фиксация факта автозакрытия итерации:
        1. Записывает событие в CACHE/auto_close_history.json.
        2. Обновляет CACHE/iteration_state.json со следующим номером итерации (iter_idx + 1).
        3. Защищает от повторного срабатывания закрытия для закрытой итерации.
        """
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        close_record = {
            "iteration_index": iter_idx,
            "closed_at": now_utc,
            "timestamp_ms": int(time.time() * 1000),
            **details
        }
        self._auto_close_history.setdefault(strat_id, []).append(close_record)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            Utils.atomic_write_json(self.history_file, self._auto_close_history, indent=4)
        except Exception as e:
            logger.error(f"[AnalyticsFSM] Error saving auto_close_history.json: {e}")

        if self.iteration_file.exists():
            try:
                disk_state = json.loads(self.iteration_file.read_text(encoding="utf-8"))
                if isinstance(disk_state, dict):
                    for k, v in disk_state.items():
                        if isinstance(v, dict):
                            self._iteration_state.setdefault(k, {}).update(v)
            except Exception:
                pass

        next_idx = iter_idx + 1
        strat_entry = self._iteration_state.setdefault(strat_id, {})
        strat_entry["iteration_index"] = next_idx
        strat_entry["last_closed_iteration"] = iter_idx
        strat_entry["updated_at"] = now_utc
        if "strat_net" in details and details["strat_net"] is not None:
            strat_entry["iteration_start_net_profit_usdt"] = float(details["strat_net"])
        if "settled_realized" in details and details["settled_realized"] is not None:
            strat_entry["iteration_start_realized_pnl_usdt"] = float(details["settled_realized"])
        try:
            Utils.atomic_write_json(self.iteration_file, self._iteration_state, indent=4)
        except Exception as e:
            logger.error(f"[AnalyticsFSM] Error updating iteration_state.json: {e}")

        logger.info(
            f"[AnalyticsFSM] 📝 Marked iteration #{iter_idx} of {strat_id} as closed. "
            f"Next iteration #{next_idx} persisted to cache."
        )
