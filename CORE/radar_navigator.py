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

if TYPE_CHECKING:
    from CORE.bot import BotCore

logger = UnifiedLogger("RadarNavigator")


class RadarNavigator:
    """
    Класс Штурмана:
    1. Fail-Fast валидация стратегий в Радаре (краш при несовпадении).
    2. Управление холостым ходом (если min_radar_warmup_hours не null).
    3. Первичный автокомплит пула монет по воронке Самсонова.
    4. Оценка ротации по храповику стагнации с гистерезисом (+20%).
    """

    def __init__(self, bot_core: "BotCore"):
        self.bot_core = bot_core

        # Строгое чтение конфигов через [''] без скрытых дефолтов
        synergy_cfg = _CFG["radar_synergy"]
        self.enabled: bool = synergy_cfg["enabled"]
        self.mode: str = synergy_cfg["mode"]  # 'advisory', 'auto', 'disabled'

        paths_cfg = _CFG["paths"]
        radar_raw_path: str = paths_cfg["radar_dir"]
        p = Path(radar_raw_path)
        self.radar_dir: Path = (BASE_DIR / p).resolve() if not p.is_absolute() else p
        self.radar_stats_dir: Path = self.radar_dir / "ANALYTICS" / "STATS"
        self.radar_cfg_path: Path = self.radar_dir / "CFG" / "universes.json"

        # Если min_radar_warmup_hours равен None / null — первичная инициализация пропускается
        raw_warmup = synergy_cfg["min_radar_warmup_hours"]
        self.min_warmup_hours: Optional[float] = float(raw_warmup) if raw_warmup is not None else None

        self.poll_interval_sec: float = float(synergy_cfg["warmup_poll_interval_sec"])
        self.target_count: int = int(synergy_cfg["target_coins_count"])

        hyst_cfg = synergy_cfg["hysteresis"]
        self.min_lead_pct: float = float(hyst_cfg["min_score_lead_pct"])

        rot_triggers = synergy_cfg["rotation_triggers"]
        self.on_stagnation: bool = bool(rot_triggers["on_stagnation_ratchet"])
        self.excluded_top_rule: str = str(rot_triggers["excluded_top_rule"])

        self.selector = SamsonovFunnelSelector(radar_stats_dir=self.radar_stats_dir)
        self.pending_rotations: Dict[str, Dict[str, Any]] = {}

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
            if not u_cfg.get("enabled", True):
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
        start_ts = data.get("start_ts") or data.get("created_at_ts")
        if not start_ts:
            return 0.0
        now_ms = int(time.time() * 1000)
        return max(0.0, (now_ms - start_ts) / 3_600_000.0)

    async def wait_radar_warmup_and_init(self) -> None:
        """
        Холостой ход и начальная инициализация.
        Если min_radar_warmup_hours равен None — холостой ход пропускается,
        и бот сразу торгует на текущих монетах до первого храповика.
        """
        if not self.is_enabled():
            return

        active_strats = []
        if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
            active_strats = list(self.bot_core.strategy_registry.active_strategies.keys())
        if not active_strats:
            app_data = _CFG["active_strategies"]
            active_strats = [sid for sid, scfg in app_data.items() if scfg.get("enabled", False)]

        # 1. Строгая проверка стратегий в Радаре (Fail-Fast)
        self.validate_strategies_in_radar(active_strats)

        # 2. Проверка: требуется ли холостой ход разгона?
        if self.min_warmup_hours is None:
            logger.info(
                "[RadarNavigator] min_radar_warmup_hours=null -> Холостой ход и первичная инициализация пропущены. "
                "Боевой бот стартует на текущих монетах и ожидает первого закрытия по храповику."
            )
            return

        # Холостой ход: ожидание набора статистики Радаром
        logger.info(f"[RadarNavigator] Старт ожидания разгона Радара (цель: {self.min_warmup_hours}ч)...")
        while self.bot_core.is_running:
            all_warmed = True
            min_current_runtime = 999999.0

            for sid in active_strats:
                rt = self.get_radar_runtime_hours(sid)
                if rt < min_current_runtime:
                    min_current_runtime = rt
                if rt < self.min_warmup_hours:
                    all_warmed = False

            if all_warmed and min_current_runtime >= self.min_warmup_hours:
                logger.info(
                    f"🟢 [RadarNavigator] Радар полностью прогрет! "
                    f"Минимальный рантайм: {min_current_runtime:.2f}ч >= {self.min_warmup_hours}ч."
                )
                break

            logger.info(
                f"⏳ [RadarNavigator] Радар на разгоне: {min_current_runtime:.2f}ч / {self.min_warmup_hours}ч. "
                f"Боевой бот в холостом режиме..."
            )
            if hasattr(self.bot_core, "watchdog"):
                self.bot_core._last_tick = time.time()
            await asyncio.sleep(self.poll_interval_sec)

        # 3. Первичная инициализация пула монет (Автокомплит)
        logger.info("[RadarNavigator] Запуск первичного автокомплита монет по алгоритму Самсонова...")
        app_json_path = DATA_DIR / "app.json"
        app_data = json.loads(app_json_path.read_text(encoding="utf-8"))
        needs_reload = False

        for sid in active_strats:
            stages = self.selector.get_funnel_stages(universe_id=sid, target_count=self.target_count)
            top_coins = [c["symbol"] for c in stages.get("final_targets", [])]

            if top_coins:
                logger.info(f"🏆 [RadarNavigator][{sid}] Отобран первичный топ монет Самсонова: {top_coins}")
                app_data["active_strategies"][sid]["symbols"] = top_coins
                needs_reload = True

                if getattr(self.bot_core, "notifier", None):
                    coins_str = ", ".join(top_coins)
                    await self.bot_core.notifier.send_alert(
                        f"🚀 <b>ШТУРМАН: ПЕРВИЧНАЯ ИНИЦИАЛИЗАЦИЯ [{sid}]</b>\n"
                        f"Радар набрал зрелую статистику ({self.min_warmup_hours}ч).\n"
                        f"Боевая обойма заряжена лидерами воронки Самсонова:\n<b>{coins_str}</b>\n"
                        f"Боевой бот начинает торги!"
                    )

        if needs_reload:
            from c_utils import Utils
            Utils.write_json_file(app_json_path, app_data)
            self.bot_core.strategy_registry.reload(app_data["active_strategies"])
            self.bot_core.symbols = self.bot_core.strategy_registry.get_all_active_symbols()

    async def on_ratchet_closed(self, strat_id: str, matched_rule: str, is_stagnation: bool) -> None:
        """
        Хук срабатывания храповика auto_closer.
        Позиции уже сброшены в 100% кэш. Зависших позиций нет.
        """
        if not self.is_enabled():
            return

        # 1. Проверяем правило закрытия: верхний скоростной импульс исключается
        if matched_rule == self.excluded_top_rule:
            logger.info(
                f"[RadarNavigator][{strat_id}] Храповик закрыт по верхнему правилу '{matched_rule}'. "
                f"Импульс на максимуме, обойма монет сохраняется."
            )
            return

        if self.on_stagnation and not is_stagnation:
            logger.info(f"[RadarNavigator][{strat_id}] Закрытие не по стагнации. Ротация пропущена.")
            return

        # 2. Если задан порог разгона, проверяем наработку
        if self.min_warmup_hours is not None:
            rt = self.get_radar_runtime_hours(strat_id)
            if rt < self.min_warmup_hours:
                logger.warning(f"[RadarNavigator][{strat_id}] Рантайм радара {rt:.2f}ч < {self.min_warmup_hours}ч. Ротация отклонена.")
                return

        # 3. Вызываем расчет скоров Самсонова
        metrics = self.selector.calculate_coin_metrics(universe_id=strat_id)
        if not metrics:
            logger.warning(f"[RadarNavigator][{strat_id}] Нет метрик из Радара.")
            return

        metrics_by_sym = {m["symbol"]: m for m in metrics}
        strat_cfg = self.bot_core.strategy_registry.active_strategies.get(strat_id)
        current_symbols = strat_cfg.symbols if strat_cfg else []

        if not current_symbols:
            return

        # Находим слабейшую монету боевого пула
        current_scores = []
        for s in current_symbols:
            m = metrics_by_sym.get(s, {})
            score = float(m.get("samsonov_score", 0.0))
            current_scores.append((s, score, m))
        current_scores.sort(key=lambda x: x[1])
        weakest_sym, weakest_score, weakest_meta = current_scores[0]

        # Находим лучшего кандидата из Радара, не входящего в текущий боевой пул
        candidates = [m for m in metrics if m["symbol"] not in current_symbols]
        if not candidates:
            return
        candidates.sort(key=lambda x: x.get("samsonov_score", 0.0), reverse=True)
        best_cand = candidates[0]
        cand_sym = best_cand["symbol"]
        cand_score = float(best_cand.get("samsonov_score", 0.0))

        # 4. Проверка фильтра гистерезиса (+20%)
        denom = max(weakest_score, 0.001)
        lead_pct = ((cand_score - weakest_score) / denom) * 100.0

        logger.info(
            f"[RadarNavigator][{strat_id}] Проверка гистерезиса: Кандидат {cand_sym} (Скор: {cand_score:.2f}) "
            f"vs Слабейший {weakest_sym} (Скор: {weakest_score:.2f}) -> Дельта: {lead_pct:+.1f}% (Порог: +{self.min_lead_pct}%)"
        )

        if lead_pct < self.min_lead_pct:
            logger.info(f"[RadarNavigator][{strat_id}] Преимущество {lead_pct:+.1f}% < +{self.min_lead_pct}%. Обойма сохраняется без изменений.")
            return

        # 5. Исполнение решения
        rot_data = {
            "strat_id": strat_id,
            "old_symbol": weakest_sym,
            "new_symbol": cand_sym,
            "old_score": weakest_score,
            "new_score": cand_score,
            "lead_pct": lead_pct,
            "matched_rule": matched_rule,
            "timestamp": time.time(),
        }

        if self.mode == "auto":
            logger.info(f"[RadarNavigator][{strat_id}] АВТОПИЛОТ: Замена {weakest_sym} ➔ {cand_sym} (+{lead_pct:.1f}%)...")
            await self.apply_rotation(strat_id, weakest_sym, cand_sym)
            if getattr(self.bot_core, "notifier", None):
                await self.bot_core.notifier.send_alert(
                    f"🔄 <b>АВТО-РОТАЦИЯ МОНЕТ [{strat_id}]</b>\n"
                    f"Сброс цикла по стагнации ({matched_rule}).\n"
                    f"🔴 Выбыла: <b>{weakest_sym}</b> (Скор: {weakest_score:.2f})\n"
                    f"🟢 Заряжена: <b>{cand_sym}</b> (Скор: {cand_score:.2f})\n"
                    f"📈 Превосходство: <b>+{lead_pct:.1f}%</b> (Гистерезис пройден!)"
                )
        elif self.mode == "advisory":
            req_id = f"{strat_id}_{int(time.time())}"
            self.pending_rotations[req_id] = rot_data
            if getattr(self.bot_core, "notifier", None):
                msg = (
                    f"🧭 <b>ШТУРМАН: СИГНАЛ РОТАЦИИ МОНЕТ [{strat_id}]</b>\n"
                    f"Сброс цикла по карте стагнации: <code>{matched_rule}</code>.\n"
                    f"Все позиции сброшены в кэш.\n\n"
                    f"Предлагается замена слабейшего актива:\n"
                    f"🔴 <b>{weakest_sym}</b> (Скор: {weakest_score:.2f}, Realized: {weakest_meta.get('realized_net', 0):+.2f}$)\n"
                    f"🟢 <b>{cand_sym}</b> (Скор: {cand_score:.2f}, Realized: {best_cand.get('realized_net', 0):+.2f}$)\n"
                    f"📊 Превосходство: <b>+{lead_pct:.1f}%</b> (Порог гистерезиса >= +{self.min_lead_pct}% пройден)\n\n"
                    f"Применить замену перед стартом нового цикла?"
                )
                from TG.tg_receiver import build_rotation_inline_keyboard
                if hasattr(self.bot_core.notifier, "send_alert_with_keyboard"):
                    await self.bot_core.notifier.send_alert_with_keyboard(msg, build_rotation_inline_keyboard(req_id))
                else:
                    await self.bot_core.notifier.send_alert(msg)

    async def apply_rotation(self, strat_id: str, old_symbol: str, new_symbol: str) -> None:
        """Атомарная замена монеты в боевом боте."""
        await self.bot_core.delete_symbol(old_symbol)
        await self.bot_core.add_symbol(new_symbol, strat_id)
        logger.info(f"✅ [RadarNavigator] Ротация успешно завершена: {old_symbol} -> {new_symbol} в стратегии {strat_id}.")

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
        top_candidates = metrics[:8]

        strat_cfg = self.bot_core.strategy_registry.active_strategies.get(target_strat) if hasattr(self.bot_core, "strategy_registry") else None
        current_symbols = strat_cfg.symbols if strat_cfg else []

        lines = [
            f"🧭 <b>РАДАР ШТУРМАНА: [{target_strat}]</b>",
            f"⏱ Время непрерывной работы: <b>{runtime:.2f} ч.</b>",
            f"🎯 Текущая боевая обойма: {', '.join(current_symbols)}",
            "",
            "<b>ТОП КАНДИДАТОВ ВОРОНКИ САМСОНОВА:</b>",
        ]

        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"]
        for idx, m in enumerate(top_candidates):
            icon = medals[idx] if idx < len(medals) else "▫️"
            sym = m["symbol"]
            score = m["samsonov_score"]
            real = m["realized_net"]
            tag = " <i>[В СТРОЮ]</i>" if sym in current_symbols else ""
            lines.append(f"{icon} <b>{sym}</b>: Скор <b>{score:.1f}</b> (Realized: {real:+.2f}$){tag}")

        return "\n".join(lines)
