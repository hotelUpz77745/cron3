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

        advisory_cfg = synergy_cfg["advisory"]
        self.advisory_timeout_sec: float = float(advisory_cfg["timeout_sec"])
        self.advisory_timeout_action: str = str(advisory_cfg["timeout_action"])  # 'skip' or 'apply'

        self.selector = SamsonovFunnelSelector(radar_stats_dir=self.radar_stats_dir)
        self.pending_rotations: Dict[str, Dict[str, Any]] = {}
        self.timeout_tasks: Dict[str, asyncio.Task] = {}
        self.symbols_pending_rotation: set[str] = set()

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
            top_coins = [c["symbol"] for c in stages.get("final_targets", []) if c.get("symbol")]

            if not top_coins:
                current_syms = app_data["active_strategies"][sid].get("symbols", [])
                logger.warning(
                    f"⚠️ [RadarNavigator][{sid}] Радар вернул пустой список кандидатов (нет монет, прошедших критерии воронки Самсонова). "
                    f"Автокомплит отменен во избежание пустого пула. Текущая боевая обойма сохранена без изменений: {current_syms}"
                )
                continue

            # ПРАВИЛО 1 (Инициализация): Обойма формируется ровно из target_coins_count монет
            # Если кандидатов меньше целевого размера, дополняем текущими монетами во избежание обрезания пула
            if len(top_coins) < self.target_count:
                current_syms = app_data["active_strategies"][sid].get("symbols", [])
                for s in current_syms:
                    if s not in top_coins and len(top_coins) < self.target_count:
                        top_coins.append(s)
                logger.info(
                    f"[RadarNavigator][{sid}] Радар вернул {len(stages.get('final_targets', []))} монет "
                    f"(меньше целевых {self.target_count}). Обойма дополнена текущими монетами: {top_coins}"
                )

            # Если монет больше целевого размера — строго обрезаем до target_coins_count
            if len(top_coins) > self.target_count:
                top_coins = top_coins[:self.target_count]

            # Контрольная проверка правила: размер обоймы обязан быть СТРОГО равен target_coins_count
            if len(top_coins) != self.target_count:
                current_syms = app_data["active_strategies"][sid].get("symbols", [])
                logger.warning(
                    f"⚠️ [RadarNavigator][{sid}] Нарушение правила target_coins_count: "
                    f"получено {len(top_coins)} монет вместо {self.target_count}. Автокомплит отклонен. "
                    f"Сохранен текущий боевой пул: {current_syms}"
                )
                continue

            logger.info(f"🏆 [RadarNavigator][{sid}] Отобран первичный топ монет Самсонова (ровно {self.target_count}): {top_coins}")
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
            logger.warning(f"[RadarNavigator][{strat_id}] Радар вернул пустой список метрик. Ротация отменена, боевой пул сохранен.")
            return

        metrics_by_sym = {m["symbol"]: m for m in metrics}
        strat_cfg = self.bot_core.strategy_registry.active_strategies.get(strat_id)
        current_symbols = strat_cfg.symbols if strat_cfg else []

        if not current_symbols:
            logger.warning(f"[RadarNavigator][{strat_id}] Текущий боевой пул стратегии пуст. Ротация невозможна.")
            return

        # ПРАВИЛО 2 (Ротация после срабатывания храповика):
        # Замена производится строго 1-к-1, только если текущий боевой пул полностью укомплектован (target_coins_count)
        if len(current_symbols) != self.target_count:
            logger.warning(
                f"⚠️ [RadarNavigator][{strat_id}] Размер текущего пула ({len(current_symbols)}) "
                f"не равен target_coins_count ({self.target_count}). Ротация 1-к-1 заблокирована до выравнивания пула."
            )
            return

        # Находим слабейшую монету боевого пула
        current_scores = []
        for s in current_symbols:
            m = metrics_by_sym.get(s, {})
            score = float(m.get("samsonov_score", 0.0))
            current_scores.append((s, score, m))
        current_scores.sort(key=lambda x: x[1])
        weakest_sym, weakest_score, weakest_meta = current_scores[0]

        # Находим лучших кандидатов из Радара, не входящих в текущий боевой пул и имеющих положительный скор (>0)
        candidates = [
            m for m in metrics
            if m.get("symbol") and m["symbol"] not in current_symbols and float(m.get("samsonov_score", 0.0)) > 0.0
        ]
        if not candidates:
            logger.info(
                f"[RadarNavigator][{strat_id}] Радар не вернул пригодных кандидатов с положительным скором (>0) вне текущего пула "
                f"(список пуст или ни одна новая монета не показала положительной динамики). Боевой пул сохранен без изменений."
            )
            return

        candidates.sort(key=lambda x: float(x.get("samsonov_score", 0.0)), reverse=True)
        best_cand = candidates[0]
        cand_sym = best_cand["symbol"]
        cand_score = float(best_cand.get("samsonov_score", 0.0))

        # Базовая защита: кандидат обязан быть строго лучше слабейшей боевой монеты
        if cand_score <= weakest_score:
            logger.info(
                f"[RadarNavigator][{strat_id}] Лучший кандидат {cand_sym} (Скор: {cand_score:.2f}) "
                f"не превосходит слабейшую боевую монету {weakest_sym} (Скор: {weakest_score:.2f}). Обойма сохраняется."
            )
            return

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
            self.symbols_pending_rotation.add(weakest_sym)

            # Фоновый НЕБЛОКИРУЮЩИЙ таймаут: основная лупа бота не прерывается ни на миллисекунду
            timeout_task = asyncio.create_task(self._advisory_timeout_worker(req_id))
            self.timeout_tasks[req_id] = timeout_task

            if getattr(self.bot_core, "notifier", None):
                action_desc = "авто-отклонение (возврат в торги)" if self.advisory_timeout_action == "skip" else "авто-замена на лидера"
                msg = (
                    f"🧭 <b>ШТУРМАН: СИГНАЛ РОТАЦИИ МОНЕТ [{strat_id}]</b>\n"
                    f"Сброс цикла по карте стагнации: <code>{matched_rule}</code>.\n"
                    f"Все позиции сброшены в кэш.\n\n"
                    f"Предлагается замена слабейшего актива:\n"
                    f"🔴 <b>{weakest_sym}</b> (Скор: {weakest_score:.2f}, Realized: {weakest_meta.get('realized_net', 0):+.2f}$)\n"
                    f"🟢 <b>{cand_sym}</b> (Скор: {cand_score:.2f}, Realized: {best_cand.get('realized_net', 0):+.2f}$)\n"
                    f"📊 Превосходство: <b>+{lead_pct:.1f}%</b> (Порог гистерезиса >= +{self.min_lead_pct}% пройден)\n\n"
                    f"⏳ <i>Таймаут ожидания ответа: <b>{int(self.advisory_timeout_sec)} сек.</b>\n"
                    f"При отсутствии реакции: {action_desc}.\n"
                    f"Остальные монеты продолжают торги штатно.</i>\n\n"
                    f"Применить замену перед стартом нового цикла?"
                )
                from TG.tg_receiver import build_rotation_inline_keyboard
                if hasattr(self.bot_core.notifier, "send_alert_with_keyboard"):
                    await self.bot_core.notifier.send_alert_with_keyboard(msg, build_rotation_inline_keyboard(req_id))
                else:
                    await self.bot_core.notifier.send_alert(msg)

    def is_symbol_pending_rotation(self, symbol: str) -> bool:
        """Проверяет, заблокирован ли вход для монеты, ожидающей решения по ротации."""
        return symbol in self.symbols_pending_rotation

    def cancel_timeout_task(self, req_id: str) -> None:
        """Отменяет фоновый таймаут-воркер, если пользователь ответил вручную через Telegram."""
        task = self.timeout_tasks.pop(req_id, None)
        if task and not task.done():
            task.cancel()

    async def _advisory_timeout_worker(self, req_id: str) -> None:
        """
        Фоновый неблокирующий воркер таймаута:
        - Не блокирует основную лупу (await asyncio.sleep).
        - По истечении таймаута автоматически исполняет действие (skip или apply)
          и возвращает монету/пул в штатные торги, исключая 'уход в дзен'.
        """
        try:
            await asyncio.sleep(self.advisory_timeout_sec)
        except asyncio.CancelledError:
            return

        if req_id not in self.pending_rotations:
            return

        rot = self.pending_rotations.pop(req_id)
        old_sym = rot["old_symbol"]
        new_sym = rot["new_symbol"]
        strat_id = rot["strat_id"]
        lead_pct = rot["lead_pct"]

        # Снимаем блокировку со старой монеты
        self.symbols_pending_rotation.discard(old_sym)
        self.timeout_tasks.pop(req_id, None)

        if self.advisory_timeout_action == "apply":
            logger.info(
                f"[RadarNavigator][{strat_id}] Таймаут advisory ({self.advisory_timeout_sec:.0f}с) истек. "
                f"Действие 'apply': автоматическое применение ротации {old_sym} -> {new_sym}."
            )
            await self.apply_rotation(strat_id, old_sym, new_sym)
            if getattr(self.bot_core, "notifier", None):
                await self.bot_core.notifier.send_alert(
                    f"⚡ <b>ШТУРМАН: ТАЙМАУТ ADVISORY ({int(self.advisory_timeout_sec)}с) ИСТЕК</b>\n"
                    f"Оператор не ответил. Сработало действие: <code>apply</code>.\n"
                    f"Монета <b>{old_sym}</b> автоматически заменена на <b>{new_sym}</b> (+{lead_pct:.1f}%).\n"
                    f"Торги продолжаются на обновленном составе!"
                )
        else:
            logger.info(
                f"[RadarNavigator][{strat_id}] Таймаут advisory ({self.advisory_timeout_sec:.0f}с) истек. "
                f"Действие 'skip': ротация отменена, боевой пул сохранен без изменений."
            )
            if getattr(self.bot_core, "notifier", None):
                await self.bot_core.notifier.send_alert(
                    f"⌛ <b>ШТУРМАН: ТАЙМАУТ ADVISORY ({int(self.advisory_timeout_sec)}с) ИСТЕК</b>\n"
                    f"Оператор не ответил. Сработало действие: <code>skip</code>.\n"
                    f"Ротация отменена, монета <b>{old_sym}</b> возвращена к торгам.\n"
                    f"Боевой пул сохранен без изменений, бот не простаивает!"
                )

    async def apply_rotation(self, strat_id: str, old_symbol: str, new_symbol: str) -> None:
        """Атомарная замена монеты в боевом боте с защитой от некорректных присвоений."""
        self.symbols_pending_rotation.discard(old_symbol)
        self.symbols_pending_rotation.discard(new_symbol)

        if not old_symbol or not new_symbol or old_symbol == new_symbol:
            logger.warning(
                f"[RadarNavigator] Отклонена некорректная ротация: old='{old_symbol}', new='{new_symbol}'. "
                f"Боевой пул сохранен без изменений."
            )
            return

        strat_cfg = self.bot_core.strategy_registry.active_strategies.get(strat_id) if hasattr(self.bot_core, "strategy_registry") else None
        if strat_cfg:
            if old_symbol not in strat_cfg.symbols:
                logger.warning(f"[RadarNavigator] Монета {old_symbol} отсутствует в стратегии [{strat_id}]. Ротация отменена.")
                return
            if new_symbol in strat_cfg.symbols:
                logger.warning(f"[RadarNavigator] Монета-кандидат {new_symbol} уже активна в стратегии [{strat_id}]. Дублирование исключено.")
                return
            if len(strat_cfg.symbols) != self.target_count:
                logger.warning(
                    f"⚠️ [RadarNavigator][{strat_id}] Текущий размер обоймы {len(strat_cfg.symbols)} != {self.target_count}. "
                    f"Ротация 1-к-1 отклонена во избежание десинхронизации."
                )
                return

        await self.bot_core.delete_symbol(old_symbol)
        await self.bot_core.add_symbol(new_symbol, strat_id)

        # Контрольная проверка инварианта target_coins_count после замены 1-к-1
        if strat_cfg:
            if len(strat_cfg.symbols) == self.target_count:
                logger.info(
                    f"✅ [RadarNavigator] Ротация 1-к-1 успешно завершена: {old_symbol} -> {new_symbol} в стратегии [{strat_id}]. "
                    f"Инвариант target_coins_count={self.target_count} строго соблюден: {strat_cfg.symbols}."
                )
            else:
                logger.error(
                    f"🚨 [RadarNavigator] Нарушение инварианта target_coins_count после ротации [{strat_id}]! "
                    f"В обойме {len(strat_cfg.symbols)} монет вместо {self.target_count}: {strat_cfg.symbols}"
                )
        else:
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
