# ==============================================================================
# Path: CORE/radar_navigator.py
# Role: Штурман автокомплита и ротации боевой обоймы по телеметрии Радара
# ==============================================================================

from __future__ import annotations
import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from consts import BASE_DIR, DATA_DIR, PARENT_DIR, _CFG
from c_log import UnifiedLogger
from CORE.coin_selectors import SamsonovFunnelSelector
from CORE.SELECTOR.alpha_coin_selector import AlphaCoinSelector

if TYPE_CHECKING:
    from CORE.bot import BotCore

logger = UnifiedLogger("RadarNavigator")


class RadarNavigator:
    """
    Класс Штурмана:
    1. Fail-Fast валидация стратегий в Радаре (краш при несовпадении).
    2. Управление телеметрией Радара и предоставление срезов скоринга.
    """

    def __init__(self, bot_core: "BotCore"):
        self.bot_core = bot_core

        # Конфигурация штурмана (ротация демонтирована, режим строго disabled)
        rot_cfg = _CFG.get("radar_synergy", {})
        default_selector = "alpha_coin_selector"

        self.enabled: bool = bool(rot_cfg.get("enabled", False))
        self.mode: str = rot_cfg.get("mode", "disabled")
        self.selector_type: str = str(rot_cfg.get("selector_type", default_selector))

        paths_cfg = _CFG.get("paths", {})
        radar_raw_path: str = paths_cfg.get("radar_dir", "../cron3PapperUniverses")
        p = Path(radar_raw_path)
        self.radar_dir: Path = (BASE_DIR / p).resolve() if not p.is_absolute() else p
        self.radar_stats_dir: Path = self.radar_dir / "ANALYTICS" / "STATS"
        self.radar_cfg_path: Path = self.radar_dir / "CFG" / "universes.json"

        # Инвариант: блокирующий холостой ход и автокомплит при старте удалены.
        # Монеты задаются строго вручную оператором в app.json.
        self.min_warmup_hours: Optional[float] = None
        self.poll_interval_sec: float = 0.0
        self.target_count: int = int(rot_cfg.get("target_coins_count", 4))
        self.min_rotation_interval_hours: float = float(rot_cfg.get("min_universe_age_hours", rot_cfg.get("min_rotation_interval_hours", 23.0)))

        hyst_cfg = rot_cfg.get("hysteresis", {})
        self.min_lead_pct: float = float(rot_cfg.get("min_score_lead_pct", hyst_cfg.get("min_score_lead_pct", 35.0)))

        rot_triggers = rot_cfg.get("rotation_triggers", {})
        self.on_stagnation: bool = bool(rot_triggers.get("on_stagnation_ratchet", True))
        self.excluded_top_rule: str = str(rot_triggers.get("excluded_top_rule", ">5.0%"))

        advisory_cfg = rot_cfg.get("advisory", {})
        self.advisory_timeout_sec: float = float(advisory_cfg.get("timeout_sec", 120.0))
        self.advisory_timeout_action: str = str(advisory_cfg.get("timeout_action", "skip"))

        self.selector = SamsonovFunnelSelector(radar_stats_dir=self.radar_stats_dir)
        self.alpha_selector = AlphaCoinSelector
        self.pending_rotations: Dict[str, Dict[str, Any]] = {}
        self.timeout_tasks: Dict[str, asyncio.Task] = {}
        self.symbols_pending_rotation: set[str] = set()
        self.last_rotation_ts_by_strat: Dict[str, float] = {}

    def is_symbol_pending_rotation(self, symbol: str) -> bool:
        """Проверяет, ожидает ли монета ротации Штурмана."""
        return symbol in self.symbols_pending_rotation

    def cancel_timeout_task(self, req_id: str) -> None:
        """Отменяет фоновый таймаут-таск advisory ротации."""
        task = self.timeout_tasks.pop(req_id, None)
        if task and not task.done():
            task.cancel()

    async def apply_rotation(self, *args, **kwargs) -> None:
        """Хук применения ротации (демонтирован)."""
        logger.info("[RadarNavigator] Запрос на ротацию проигнорирован: ротация демонтирована.")
        return

    def is_enabled(self) -> bool:
        return self.enabled and self.mode != "disabled"

    def validate_strategies_in_radar(self, active_strategy_ids: List[str]) -> None:
        """
        FAIL-FAST: Проверяет, что каждая активная стратегия боевого бота активна в Радаре.
        Если стратегия отсутствует или выключена — возбуждает критическую ошибку и крашит запуск!
        """
        if not self.radar_cfg_path.exists():
            msg = f"КРИТИЧЕСКАЯ ОШИБКА: Конфиг Радара не найден по пути: {self.radar_cfg_path}"
            logger.error(msg)
            raise RuntimeError(msg)

        try:
            with open(self.radar_cfg_path, "r", encoding="utf-8") as f:
                radar_universes = json.load(f)
        except Exception as e:
            msg = f"КРИТИЧЕСКАЯ ОШИБКА чтения конфига Радара: {e}"
            logger.error(msg)
            raise RuntimeError(msg)

        for strat_id in active_strategy_ids:
            if strat_id not in radar_universes:
                msg = (
                    f"🚨 ФАТАЛЬНЫЙ СБОЙ КОНФИГУРАЦИИ!\n"
                    f"Стратегия '{strat_id}' активна в боевом боте, но ПОЛНОСТЬЮ ОТСУТСТВУЕТ в Радаре!\n"
                    f"Запуск остановлен во избежание слепых торгов."
                )
                logger.error(msg)
                raise RuntimeError(msg)

            u_cfg = radar_universes[strat_id]
            is_active = u_cfg.get("is_active", u_cfg.get("enabled", True))
            if not is_active:
                msg = (
                    f"🚨 ФАТАЛЬНЫЙ СБОЙ КОНФИГУРАЦИИ!\n"
                    f"Стратегия '{strat_id}' выключена (enabled=false) в Радаре полигона!\n"
                    f"Штурман не может получать разведданные. Краш."
                )
                logger.error(msg)
                raise RuntimeError(msg)

        logger.info(f"[RadarNavigator] Все стратегии {active_strategy_ids} успешно верифицированы в Радаре.")

    def get_radar_runtime_hours(self, strat_id: str) -> float:
        """Возвращает наработанное время стратегии в Радаре (в часах)."""
        data = self.selector.load_universe_data(strat_id)
        if not data:
            return 0.0
        start_ts = (
            data.get("first_trade_ts")
            or data.get("iteration_start_ts")
            or data.get("start_ts")
            or data.get("created_at_ts")
        )
        if not start_ts:
            return 0.0
        now_ms = int(time.time() * 1000)
        return max(0.0, (now_ms - start_ts) / 3_600_000.0)

    async def wait_radar_warmup_and_init(self) -> None:
        """
        Проверка стратегий при старте.
        ИНВАРИАНТ: Стартовый пул монет задается СТРОГО ВРУЧНУЮ оператором в app.json.
        Никакого автокомплита, никакого «прогрева Радара», никаких ожиданий при старте бота.
        Бот стартует немедленно с тем, что прописано в конфиге.
        """
        if not self.is_enabled():
            return

        active_strats = []
        if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
            active_strats = list(self.bot_core.strategy_registry.active_strategies.keys())
        if not active_strats:
            app_data = _CFG.get("active_strategies", {})
            active_strats = [sid for sid, scfg in app_data.items() if scfg.get("enabled", False)]

        # 1. Строгая проверка стратегий в Радаре (Fail-Fast)
        if active_strats:
            self.validate_strategies_in_radar(active_strats)

        logger.info(
            "[RadarNavigator] Инвариант соблюден: стартовый пул монет задан вручную оператором. "
            "Прогрев Радара не блокирует старт. Боевой бот немедленно приступает к торгам."
        )

    async def on_ratchet_closed(
        self,
        strat_id: str,
        matched_rule: str = "",
        is_stagnation: bool = False
    ) -> None:
        """Хук закрытия итерации: ротация демонтирована (Инвариант №3: статичный пул оператора)."""
        return

    @staticmethod
    def get_rotations_history(limit: int = 20) -> List[Dict[str, Any]]:
        """История ротаций (демонтирована, возвращает пустой список)."""
        return []

    def generate_telemetry_report(self, strat_id: Optional[str] = None) -> str:
        """Формирует текстовый отчет по состоянию Радара и скорам Самсонова."""
        target_strat = strat_id
        if not target_strat:
            if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry.active_strategies:
                target_strat = next(iter(self.bot_core.strategy_registry.active_strategies.keys()))
            else:
                target_strat = "u9_omni_zenith_apex"

        runtime = self.get_radar_runtime_hours(target_strat)
        metrics = self.selector.calculate_coin_metrics(universe_id=target_strat)
        if not metrics:
            return f"⚠️ Данные Радара для стратегии [{target_strat}] недоступны."

        metrics.sort(key=lambda x: x.get("samsonov_score", 0.0), reverse=True)
        strat_cfg = self.bot_core.strategy_registry.active_strategies.get(target_strat) if hasattr(self.bot_core, "strategy_registry") else None
        current_symbols = strat_cfg.symbols if strat_cfg else []

        lines = [
            f"🧭 <b>РАДАР ШТУРМАНА: [{target_strat}]</b>",
            f"⏱ Время непрерывной работы: <b>{runtime:.2f} ч.</b>",
            f"🎯 Текущая боевая обойма: {', '.join(current_symbols) if current_symbols else 'пусто'}",
            "",
            "<b>ТОП КАНДИДАТОВ ВОРОНКИ САМСОНОВА:</b>",
        ]

        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"]
        for idx, m in enumerate(metrics[:8]):
            icon = medals[idx] if idx < len(medals) else "▫️"
            sym = m["symbol"]
            score = m["samsonov_score"]
            real = m["realized_net"]
            tag = " <i>[В СТРОЮ]</i>" if sym in current_symbols else ""
            lines.append(f"{icon} <b>{sym}</b>: Скор <b>{score:.1f}</b> (Realized: {real:+.2f}$){tag}")

        return "\n".join(lines)

