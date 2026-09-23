# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\CORE\dynamic_coin_selector.py
# Role: dynamic_coin_selector.py module

# ==============================================================================
# Path: CORE/dynamic_coin_selector.py
# Role: Динамический отбор монет из аналитики бумажного бота
#       по активности рынка (Trade Velocity) и индивидуальной прибыльности
# ==============================================================================

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from consts import CFG_PATH, DATA_DIR, PAPER_BOT_DIR
from c_log import UnifiedLogger
from c_utils import Utils

logger = UnifiedLogger("DynamicCoinSelector")


class DynamicCoinSelector:
    """
    Модуль динамического отбора и синхронизации списка монет для боевого бота.
    Вычитывает данные из аналитики бумажного бота, валидирует готовность и порог
    активности рынка (trade_velocity.speed_ratio >= min_velocity_ratio),
    проверяет индивидуальный плюс каждой монеты (net_profit_usdt >= min_coin_profit_usdt),
    ранжирует по выбранной метрике и фиксирует отобранные монеты на диск в CFG/app.json.
    """

    RANKING_METRIC_MAP = {
        "net": "net_profit_usdt",
        "net_profit_usdt": "net_profit_usdt",
        "winrate": "winrate_pct",
        "winrate_pct": "winrate_pct",
        "trades": "total_trades",
        "total_trades": "total_trades",
        "recovery": "recovery_factor",
        "recovery_factor": "recovery_factor",
        "realized": "realized_pnl_net_usdt",
        "realized_pnl_net_usdt": "realized_pnl_net_usdt",
    }

    def __init__(self, paper_bot_dir: Optional[Path] = None):
        self.paper_bot_dir: Path = Path(paper_bot_dir) if paper_bot_dir else PAPER_BOT_DIR
        self.poll_interval_sec: float = 30.0

    def _read_config(self) -> Dict[str, Any]:
        """Считывает свежую конфигурацию dynamic_selection из CFG/app.json."""
        app_data = Utils.read_json_file(CFG_PATH) or {}
        paths_cfg = app_data.get("paths", {})
        if "paper_bot_dir" in paths_cfg and paths_cfg["paper_bot_dir"]:
            self.paper_bot_dir = Path(paths_cfg["paper_bot_dir"])

        ds_cfg = app_data.get("dynamic_selection", {})
        self.poll_interval_sec = float(ds_cfg.get("poll_interval_sec", 30.0))
        return ds_cfg

    def _read_paper_analytics(self) -> Optional[Dict[str, Any]]:
        """Вычитывает analytics.json из бумажного бота."""
        analytics_file = self.paper_bot_dir / "ANALYTICS" / "analytics.json"
        if not analytics_file.exists():
            logger.warning(f"[COIN_SELECTOR] Paper analytics file not found at: {analytics_file}")
            return None

        try:
            return json.loads(analytics_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"[COIN_SELECTOR] Error reading paper analytics: {e}")
            return None

    def check_velocity_gate(self, analytics_data: Dict[str, Any], min_ratio: float) -> Tuple[bool, float, str]:
        """
        Проверяет, прогрет ли рынок на основе trade_velocity из бумажного бота.
        Возвращает (is_passed, speed_ratio, status_desc).
        """
        tv_data = analytics_data.get("trade_velocity", {})
        if not tv_data:
            return False, 0.0, "NO_DATA"

        is_ready = tv_data.get("is_ready_for_alerts", False)
        speed_ratio = float(tv_data.get("speed_ratio", 0.0))
        status_desc = str(tv_data.get("status_desc", "UNKNOWN"))

        if not is_ready:
            return False, speed_ratio, f"ACCUMULATING (готовность не наступила)"

        if speed_ratio < min_ratio:
            return False, speed_ratio, status_desc

        return True, speed_ratio, status_desc

    def filter_and_rank_candidates(
        self,
        coins_data: Dict[str, Any],
        min_profit_usdt: float,
        ranking_metric_key: str,
        max_coins: Optional[int]
    ) -> List[str]:
        """
        Отбирает монеты, чей чистый PnL >= min_profit_usdt,
        сортирует по указанной метрике и делает срез до max_coins.
        """
        profitable_candidates = []

        for symbol, metrics in coins_data.items():
            if not isinstance(metrics, dict):
                continue

            net_profit = float(metrics.get("net_profit_usdt", 0.0))
            # Требование: каждая отдельная монета обязана быть в плюсе >= порога
            if net_profit >= min_profit_usdt:
                sort_val = float(metrics.get(ranking_metric_key, 0.0))
                profitable_candidates.append({
                    "symbol": symbol,
                    "sort_val": sort_val,
                    "net_profit": net_profit,
                })

        # Сортировка по убыванию выбранного критерия
        profitable_candidates.sort(key=lambda x: x["sort_val"], reverse=True)

        if max_coins and max_coins > 0:
            selected_slice = profitable_candidates[:max_coins]
        else:
            selected_slice = profitable_candidates

        return [c["symbol"] for c in selected_slice]

    def select_and_lock(self) -> List[str]:
        """
        Главный метод: вычитывает бумажную аналитику, проверяет активность рынка,
        отбирает прибыльные монеты, сохраняет на диск в CFG/app.json и возвращает список.
        """
        ds_cfg = self._read_config()
        if not ds_cfg.get("enabled", True):
            logger.info("[COIN_SELECTOR] Dynamic selection is disabled in config.")
            return []

        analytics_data = self._read_paper_analytics()
        if not analytics_data:
            return []

        min_velocity_ratio = float(ds_cfg.get("min_velocity_ratio", 0.75))
        min_coin_profit = float(ds_cfg.get("min_coin_profit_usdt", 1.0))
        metric_name = str(ds_cfg.get("ranking_metric", "net_profit_usdt")).lower()
        ranking_metric_key = self.RANKING_METRIC_MAP.get(metric_name, "net_profit_usdt")
        max_coins = ds_cfg.get("max_coins_num")
        if max_coins is not None:
            try:
                max_coins = int(max_coins)
            except (ValueError, TypeError):
                max_coins = None

        # ШАГ 1. Проверка активности рынка (Trade Velocity Gate)
        passed, speed_ratio, status_desc = self.check_velocity_gate(analytics_data, min_velocity_ratio)
        if not passed:
            logger.warning(
                f"[COIN_SELECTOR] Market velocity gate BLOCKED: "
                f"ratio {speed_ratio:.2f}x < {min_velocity_ratio:.2f}x [{status_desc}]. "
                f"Waiting for market heat-up."
            )
            return []

        logger.info(
            f"[COIN_SELECTOR] Market velocity gate PASSED: "
            f"ratio {speed_ratio:.2f}x >= {min_velocity_ratio:.2f}x [{status_desc}]."
        )

        # ШАГ 2. Отбор и ранжирование монет
        coins_data = analytics_data.get("per_coin", analytics_data.get("coins", {}))
        selected_symbols = self.filter_and_rank_candidates(
            coins_data=coins_data,
            min_profit_usdt=min_coin_profit,
            ranking_metric_key=ranking_metric_key,
            max_coins=max_coins
        )

        if not selected_symbols:
            logger.warning(
                f"[COIN_SELECTOR] Velocity is sufficient, but NO coins have "
                f"net_profit_usdt >= {min_coin_profit} USDT. Waiting for profitable candidates."
            )
            return []

        # ШАГ 3. Фиксация (Lock) на диск в CFG/app.json
        self._lock_symbols_to_disk(selected_symbols)

        logger.info(
            f"[COIN_SELECTOR] Successfully selected and locked {len(selected_symbols)} coins "
            f"(Ranked by '{ranking_metric_key}', Max: {max_coins}, Min PnL: >={min_coin_profit}$): "
            f"{selected_symbols}"
        )

        return selected_symbols

    def _lock_symbols_to_disk(self, symbols: List[str]) -> bool:
        """Записывает отобранный список монет в CFG/app.json."""
        try:
            app_data = Utils.read_json_file(CFG_PATH) or {}
            app_data["symbols"] = symbols
            Utils.write_json_file(CFG_PATH, app_data)
            logger.info(f"[COIN_SELECTOR] Persisted symbols to disk: {CFG_PATH}")
            return True
        except Exception as e:
            logger.error(f"[COIN_SELECTOR] Failed to persist symbols to {CFG_PATH}: {e}")
            return False
