# ============================================================
# FILE: CORE/SERVICES/dome_closer_executor.py
# ROLE: Atomic execution of dome and portfolio auto-closes (positions, cache, analytics, alerts)
# ============================================================

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from consts import CACHE_DIR
from c_log import UnifiedLogger
from c_utils import Utils

logger = UnifiedLogger("DomeCloserExecutor")


class DomeCloserExecutor:
    """
    Исполнитель атомарного закрытия торгового купола или портфеля:
    - Закрытие позиций на бирже
    - Сброс состояния трейлинг-храповика
    - Обновление метрик и итераций в файлах аналитики (атомарная запись)
    - Персистентное сохранение итерационного состояния в CACHE/iteration_state.json
    - Оповещение FSM и сброс recent_closes
    - Отправка Telegram-уведомлений
    """

    @staticmethod
    async def execute_close(
        bot_core: Any,
        strat_id: str,
        is_global: bool,
        strat_cfg: Optional[Any],
        iter_idx: int,
        next_iter_idx: int,
        iter_net: float,
        strat_net: float,
        stats_path: Path,
        raw_analytics: dict,
        app_data: dict,
        app_json_path: Path,
        action_msg: str,
        trailing_ratchet_state: dict,
    ) -> None:
        """Атомарно исполняет закрытие купола или глобального портфеля."""
        # 1. Закрытие позиций на бирже
        if is_global:
            await bot_core.close_all_positions()
        else:
            await bot_core.close_strategy_positions(strat_id)

        # 2. Сброс трейлинг-храповика
        if strat_id in trailing_ratchet_state:
            trailing_ratchet_state[strat_id] = {}

        # 3. Чтение свежих данных аналитики после закрытия ордеров
        fresh_analytics = Utils.read_json_file(stats_path) if stats_path.exists() else raw_analytics
        settled_net = round(float(fresh_analytics.get("net_profit_usdt", strat_net)), 4)
        settled_realized = round(
            float(fresh_analytics.get("realized_pnl_net_usdt", fresh_analytics.get("realized_pnl_usdt", 0.0))), 4
        )

        now_ms = int(time.time() * 1000)
        fresh_analytics["iteration_index"] = next_iter_idx
        fresh_analytics["iteration_start_net_profit_usdt"] = settled_net
        fresh_analytics["iteration_net_profit_usdt"] = 0.0
        fresh_analytics["iteration_start_ts"] = now_ms
        fresh_analytics["iteration_peak_net_profit_usdt"] = 0.0
        fresh_analytics["iteration_peak_ts"] = now_ms
        fresh_analytics["iteration_peak_age_hours"] = 0.0
        fresh_analytics["iteration_net_velocity"] = 0.0
        if not is_global:
            fresh_analytics["iteration_start_realized_pnl_usdt"] = settled_realized
            fresh_analytics["iteration_realized_pnl_usdt"] = 0.0

        if stats_path:
            Utils.atomic_write_json(stats_path, fresh_analytics, indent=4)

        # 4. Сохранение итерационного кэша
        try:
            c_file = CACHE_DIR / "iteration_state.json"
            c_dict = json.loads(c_file.read_text(encoding="utf-8")) if c_file.exists() else {}
            prev_entry = c_dict.get(strat_id, {})
            cache_entry: Dict[str, Any] = {
                "iteration_index": next_iter_idx,
                "iteration_start_net_profit_usdt": settled_net,
                "iteration_start_ts": now_ms,
                "iteration_peak_net_profit_usdt": 0.0,
                "iteration_peak_ts": now_ms,
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            }
            if not is_global:
                cache_entry["iteration_start_realized_pnl_usdt"] = settled_realized
                cache_entry["iteration_realized_pnl_usdt"] = 0.0
                cache_entry["historical_peak_net_profit_usdt"] = prev_entry.get("historical_peak_net_profit_usdt", 0.0)
                cache_entry["historical_peak_net_ts"] = prev_entry.get("historical_peak_net_ts", now_ms)
                cache_entry["historical_max_drawdown_usdt"] = prev_entry.get("historical_max_drawdown_usdt", 0.0)

            c_dict[strat_id] = cache_entry
            Utils.atomic_write_json(c_file, c_dict, indent=4)
        except Exception as ce:
            logger.error(f"[DOME_CLOSER_EXECUTOR][{strat_id}] Error saving iteration cache: {ce}")

        # 5. Оповещение FSM
        fsm = getattr(getattr(bot_core, "analytics", None), "fsm", None)
        if fsm:
            c_type = "PORTFOLIO AUTO-CLOSE" if is_global else (
                action_msg.split("(")[1].split(")")[0] if "(" in action_msg else "AUTO-CLOSE"
            )
            fsm.mark_iteration_closed(
                strat_id=strat_id,
                iter_idx=iter_idx,
                details={
                    "close_type": c_type,
                    "iter_net": iter_net,
                    "strat_net": settled_net,
                    "settled_realized": settled_realized,
                },
            )

        # 6. Сброс recent_closes
        if hasattr(bot_core, "fsm_states"):
            symbols = strat_cfg.symbols if (strat_cfg and hasattr(strat_cfg, "symbols")) else list(bot_core.fsm_states.keys())
            for sym in symbols:
                if sym in bot_core.fsm_states:
                    for s_side in ("LONG", "SHORT"):
                        if s_side in bot_core.fsm_states[sym]:
                            bot_core.fsm_states[sym][s_side].recent_closes = []

        # 7. Сохранение конфигурации и уведомление
        Utils.write_json_file(app_json_path, app_data)
        if action_msg and getattr(bot_core, "notifier", None):
            final_msg = action_msg.replace("базовая точка Net: <b>0.00 USDT</b>", f"базовая точка Net: <b>{settled_net:+.2f} USDT</b>")
            await bot_core.notifier.send_alert(final_msg)
