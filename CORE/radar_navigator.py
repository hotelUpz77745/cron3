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
from CORE.radar_journal import RadarJournal

if TYPE_CHECKING:
    from CORE.bot import BotCore

logger = UnifiedLogger("RadarNavigator")


class RadarNavigator:
    """
    Класс Штурмана:
    1. Fail-Fast валидация стратегий в Радаре (краш при несовпадении).
    2. Управление холостым ходом (если min_radar_warmup_hours не null).
    3. Первичный автокомплит пула монет по воронке Самсонова или Alpha-Селектору.
    4. Оценка ротации по храповику с суточным фильтром (23ч) и гистерезисом.
    """

    def __init__(self, bot_core: "BotCore"):
        self.bot_core = bot_core

        # Чтение конфигурации ротации монет на уровне автозакрытия (как в полигоне)
        if "radar_synergy" in _CFG:
            rot_cfg = _CFG["radar_synergy"]
            default_selector = "samsonov_funnel"
        else:
            rot_cfg = _CFG["auto_closing"]["positive"]["radar_rotation_on_close"]
            default_selector = "alpha_coin_selector"

        self.enabled: bool = bool(rot_cfg.get("enabled", False))
        self.mode: str = rot_cfg.get("mode", "auto" if self.enabled else "disabled")
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

    async def on_ratchet_closed(self, strat_id: str, matched_rule: str, is_stagnation: bool) -> None:
        """
        Хук срабатывания автоклозера (auto_closer).
        Позиции уже сброшены в 100% кэш. Зависших позиций нет.
        Ротация вызывается немедленно при закрытии итерации.
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

        # Разрешение настроек ротации из автоклозера стратегии (radar_rotation_on_close)
        strat_cfg = self.bot_core.strategy_registry.active_strategies.get(strat_id) if hasattr(self.bot_core, "strategy_registry") and hasattr(self.bot_core.strategy_registry, "active_strategies") and isinstance(self.bot_core.strategy_registry.active_strategies, dict) else None
        strat_ac = {}
        if strat_cfg and hasattr(strat_cfg, "auto_closing_cfg") and isinstance(strat_cfg.auto_closing_cfg, dict):
            strat_ac = strat_cfg.auto_closing_cfg
        rot_close_cfg = strat_ac.get("positive", {}).get("radar_rotation_on_close", {}) if isinstance(strat_ac.get("positive"), dict) else {}
        if isinstance(rot_close_cfg, dict) and rot_close_cfg:
            if not rot_close_cfg.get("enabled", False):
                logger.info(f"[RadarNavigator][{strat_id}] radar_rotation_on_close выключен в автоклозере стратегии. Обойма сохраняется.")
                return
        elif self.on_stagnation and not is_stagnation:
            logger.info(f"[RadarNavigator][{strat_id}] Закрытие не по стагнации. Ротация пропущена.")
            return

        selector_type = (rot_close_cfg.get("selector_type") if isinstance(rot_close_cfg, dict) else None) or self.selector_type
        target_count = int(rot_close_cfg.get("target_coins_count") if isinstance(rot_close_cfg, dict) and "target_coins_count" in rot_close_cfg else self.target_count)
        min_lead_pct = float(rot_close_cfg.get("min_score_lead_pct") if isinstance(rot_close_cfg, dict) and "min_score_lead_pct" in rot_close_cfg else self.min_lead_pct)

        now_ts = time.time()
        raw_last_rot = self.last_rotation_ts_by_strat.get(strat_id, 0.0)
        last_rot = float(raw_last_rot) if isinstance(raw_last_rot, (int, float)) else 0.0

        raw_interval = rot_close_cfg.get("min_universe_age_hours", rot_close_cfg.get("min_rotation_interval_hours", 0.0)) if isinstance(rot_close_cfg, dict) else 0.0
        try:
            min_interval_hours = float(raw_interval)
        except (TypeError, ValueError):
            min_interval_hours = 0.0

        if last_rot == 0.0:
            start_t = getattr(strat_cfg, "start_timestamp", None)
            if not isinstance(start_t, (int, float)):
                start_t = None
            if start_t is None:
                stat_file = DATA_DIR / "ANALYTICS" / "STATS" / f"analytics_{strat_id}.json"
                if stat_file.exists():
                    try:
                        st_data = json.loads(stat_file.read_text(encoding="utf-8"))
                        ft = st_data.get("first_trade_ts") or st_data.get("iteration_start_ts")
                        if isinstance(ft, (int, float)):
                            start_t = ft / 1000.0 if ft > 1e11 else ft
                    except Exception:
                        pass
            last_rot = float(start_t) if isinstance(start_t, (int, float)) else (now_ts - 3600.0)
            self.last_rotation_ts_by_strat[strat_id] = last_rot

        if min_interval_hours > 0.0:
            min_interval_sec = min_interval_hours * 3600.0
            if (now_ts - last_rot) < min_interval_sec:
                elapsed_h = max(0.0, (now_ts - last_rot) / 3600.0)
                logger.info(
                    f"[RadarNavigator][{strat_id}] Храповик закрыт ({matched_rule}), но время непрерывной работы обоймы "
                    f"({elapsed_h:.1f}ч < {min_interval_hours:.1f}ч) еще не достигло маст-хэв порога. Обойма сохраняется."
                )
                return

        current_symbols = list(strat_cfg.symbols) if strat_cfg else []
        if not current_symbols:
            logger.warning(f"[RadarNavigator][{strat_id}] Текущий боевой пул стратегии пуст. Ротация невозможна.")
            return

        # 3. ВЕТКА АЛЬФА-СЕЛЕКТОРА (Динамический Wiggle/NATR отбор)
        # 3. ВЕТКА АЛЬФА-СЕЛЕКТОРА (Динамический Wiggle/NATR отбор)
        if selector_type == "alpha_coin_selector":
            logger.info(f"[RadarNavigator][{strat_id}] Запуск AlphaCoinSelector (NATR + Wiggle + Anti-Exhaustion)...")

            full_ranked = await self.alpha_selector.select_top_coins(target_count=30)
            if not full_ranked:
                logger.warning(f"[RadarNavigator][{strat_id}] Alpha-Селектор не вернул кандидатов. Пул сохранен.")
                return

            new_pool, qualified_rotations = self.resolve_multi_coin_rotation(
                current_symbols=current_symbols,
                ranked_top_candidates=full_ranked,
                score_key="score",
                target_count=target_count,
                min_lead_pct=min_lead_pct,
            )

            if not qualified_rotations:
                logger.info(
                    f"[RadarNavigator][{strat_id}] Ни один кандидат Alpha-Селектора не превысил порог гистерезиса (+{min_lead_pct}%). "
                    f"Обойма сохраняется без изменений."
                )
                self.last_rotation_ts_by_strat[strat_id] = now_ts
                return

            await self._execute_rotations(strat_id, qualified_rotations, matched_rule, now_ts, min_lead_pct)
            return

        # 4. ВЕТКА ВОРОНКИ САМСОНОВА (Исторический квантовый фильтр Радара)
        metrics = self.selector.calculate_coin_metrics(universe_id=strat_id)
        if not metrics:
            logger.warning(f"[RadarNavigator][{strat_id}] Радар вернул пустой список метрик. Ротация отменена, боевой пул сохранен.")
            return

        new_pool, qualified_rotations = self.resolve_multi_coin_rotation(
            current_symbols=current_symbols,
            ranked_top_candidates=metrics,
            score_key="samsonov_score",
            target_count=target_count,
            min_lead_pct=min_lead_pct,
        )

        if not qualified_rotations:
            logger.info(
                f"[RadarNavigator][{strat_id}] Ни один кандидат Samsonov не превысил порог гистерезиса (+{min_lead_pct}%). "
                f"Обойма сохраняется без изменений."
            )
            return

        await self._execute_rotations(strat_id, qualified_rotations, matched_rule, now_ts, min_lead_pct)

    @staticmethod
    def resolve_multi_coin_rotation(
        current_symbols: List[str],
        ranked_top_candidates: List[Dict[str, Any]],
        score_key: str = "score",
        target_count: int = 4,
        min_lead_pct: float = 35.0,
    ) -> tuple[List[str], List[Dict[str, Any]]]:
        """
        Универсальный решатель мульти-ротации Top-N Rank & Swap:
        1. Находит идеальный Top-N кандидатов (eligible=True, score > 0).
        2. Совпавшие монеты (уже в обойме и вошли в Top-N) безусловно сохраняются.
        3. Выбывшие монеты обоймы ранжируются от слабейшей к сильнейшей.
        4. Новые претенденты из Top-N ранжируются от сильнейшего к слабейшему.
        5. Попарно сопоставляются: замена происходит ТОЛЬКО если lead_pct >= min_lead_pct (35%).
           Если отрыв < min_lead_pct, старая монета сохраняется (защита гистерезисом).
        Возвращает (new_pool, qualified_rotations).
        """
        score_map = {}
        meta_by_sym = {}
        for item in ranked_top_candidates:
            if not isinstance(item, dict):
                continue
            sym = item.get("symbol")
            if not sym:
                continue
            sc = float(item.get(score_key, item.get("samsonov_score", item.get("score", 0.0))))
            score_map[sym] = sc
            meta_by_sym[sym] = item

        # 1. Идеальный Top-N кандидатов
        eligible_candidates = [
            item for item in ranked_top_candidates
            if isinstance(item, dict) and item.get("symbol") and item.get("eligible", True)
            and float(item.get(score_key, item.get("samsonov_score", item.get("score", 0.0)))) > 0.0
        ]
        eligible_candidates.sort(
            key=lambda item: float(item.get(score_key, item.get("samsonov_score", item.get("score", 0.0)))),
            reverse=True
        )
        effective_target = len(current_symbols) if current_symbols else target_count
        ideal_top_n = [item["symbol"] for item in eligible_candidates][:effective_target]

        # 2. Совпавшие монеты (остаются)
        kept_symbols = [s for s in current_symbols if s in ideal_top_n]

        # 3. Выбывающие монеты (в обойме, но не в Top-N)
        dropouts = [s for s in current_symbols if s not in ideal_top_n]
        dropouts.sort(key=lambda s: score_map.get(s, 0.0))  # слабейшие первыми

        # 4. Новые претенденты из Top-N (которых еще нет в обойме)
        new_candidates = [s for s in ideal_top_n if s not in kept_symbols]
        new_candidates.sort(key=lambda s: score_map.get(s, 0.0), reverse=True)  # сильнейшие первыми

        final_pool = list(kept_symbols)
        qualified_rotations = []

        max_iter = max(len(dropouts), len(new_candidates))
        for i in range(max_iter):
            if i < len(dropouts) and i < len(new_candidates):
                old_sym = dropouts[i]
                cand_sym = new_candidates[i]
                old_sc = score_map.get(old_sym, 0.0)
                cand_sc = score_map.get(cand_sym, 0.0)
                denom = max(old_sc, 0.001)
                lead_pct = ((cand_sc - old_sc) / denom) * 100.0

                logger.info(
                    f"[RadarNavigator] Попарное сравнение #{i+1}: Претендент {cand_sym} (Скор: {cand_sc:.4f}) "
                    f"vs Выбывающий {old_sym} (Скор: {old_sc:.4f}) -> Дельта: {lead_pct:+.1f}% (Порог: +{min_lead_pct}%)"
                )

                if lead_pct >= min_lead_pct:
                    final_pool.append(cand_sym)
                    qualified_rotations.append({
                        "old_symbol": old_sym,
                        "new_symbol": cand_sym,
                        "old_score": old_sc,
                        "new_score": cand_sc,
                        "lead_pct": lead_pct,
                        "old_meta": meta_by_sym.get(old_sym, {}),
                        "new_meta": meta_by_sym.get(cand_sym, {}),
                    })
                else:
                    logger.info(
                        f"[RadarNavigator] Претендент {cand_sym} не пробил порог (+{lead_pct:.1f}% < +{min_lead_pct}%). "
                        f"Монета {old_sym} сохраняется в обойме."
                    )
                    final_pool.append(old_sym)

            elif i < len(dropouts):
                final_pool.append(dropouts[i])

            elif i < len(new_candidates):
                cand_sym = new_candidates[i]
                cand_sc = score_map.get(cand_sym, 0.0)
                final_pool.append(cand_sym)
                qualified_rotations.append({
                    "old_symbol": "<none>",
                    "new_symbol": cand_sym,
                    "old_score": 0.0,
                    "new_score": cand_sc,
                    "lead_pct": 100.0,
                    "old_meta": {},
                    "new_meta": meta_by_sym.get(cand_sym, {}),
                })

        return final_pool[:target_count], qualified_rotations

    async def _execute_rotations(
        self,
        strat_id: str,
        qualified_rotations: List[Dict[str, Any]],
        matched_rule: str,
        now_ts: float,
        min_lead_pct: float
    ) -> None:
        """Исполнение решений ротации (автопилот или advisory) и уведомление."""
        rot_data = {
            "strat_id": strat_id,
            "rotations": qualified_rotations,
            "old_symbol": qualified_rotations[0]["old_symbol"],
            "new_symbol": qualified_rotations[0]["new_symbol"],
            "old_score": qualified_rotations[0]["old_score"],
            "new_score": qualified_rotations[0]["new_score"],
            "lead_pct": qualified_rotations[0]["lead_pct"],
            "matched_rule": matched_rule,
            "timestamp": now_ts,
        }

        if self.mode == "auto":
            alert_lines = []
            for rot in qualified_rotations:
                old_s = rot["old_symbol"]
                new_s = rot["new_symbol"]
                lead_p = rot["lead_pct"]
                logger.info(f"[RadarNavigator][{strat_id}] АВТОПИЛОТ: Замена {old_s} ➔ {new_s} (+{lead_p:.1f}%)...")
                await self.apply_rotation(
                    strat_id=strat_id,
                    old_symbol=old_s,
                    new_symbol=new_s,
                    old_score=rot["old_score"],
                    new_score=rot["new_score"],
                    lead_pct=lead_p,
                    matched_rule=matched_rule,
                    mode="auto"
                )
                alert_lines.append(f"• 🔴 <b>{old_s}</b> ({rot['old_score']:.2f}) ➔ 🟢 <b>{new_s}</b> ({rot['new_score']:.2f}) [+{lead_p:.1f}%]")

            self.last_rotation_ts_by_strat[strat_id] = now_ts

            if getattr(self.bot_core, "notifier", None):
                msg_auto = (
                    f"🔄 <b>АВТО-РОТАЦИЯ МОНЕТ [{strat_id}]</b>\n"
                    f"Сброс цикла по автоклозеру ({matched_rule}). Замен: {len(qualified_rotations)}.\n"
                    + "\n".join(alert_lines) + "\n"
                    f"📈 Все кандидаты превысили порог гистерезиса >= +{min_lead_pct:.1f}%."
                )
                await self.bot_core.notifier.send_alert(msg_auto)
        elif self.mode == "advisory":
            req_id = f"{strat_id}_{int(time.time())}"
            self.pending_rotations[req_id] = rot_data
            for rot in qualified_rotations:
                self.symbols_pending_rotation.add(rot["old_symbol"])

            timeout_task = asyncio.create_task(self._advisory_timeout_worker(req_id))
            self.timeout_tasks[req_id] = timeout_task

            if getattr(self.bot_core, "notifier", None):
                action_desc = "авто-отклонение (возврат в торги)" if self.advisory_timeout_action == "skip" else "авто-замена на лидеров"
                repl_lines = []
                for idx, rot in enumerate(qualified_rotations, 1):
                    repl_lines.append(
                        f"{idx}) 🔴 <b>{rot['old_symbol']}</b> (Скор: {rot['old_score']:.2f}) ➔ "
                        f"🟢 <b>{rot['new_symbol']}</b> (Скор: {rot['new_score']:.2f}) | +{rot['lead_pct']:.1f}%"
                    )

                msg = (
                    f"🧭 <b>ШТУРМАН: СИГНАЛ РОТАЦИИ МОНЕТ [{strat_id}]</b>\n"
                    f"Сброс цикла по автоклозеру: <code>{matched_rule}</code>.\n"
                    f"Все позиции сброшены в кэш.\n\n"
                    f"Предлагается замена затухших активов ({len(qualified_rotations)}):\n"
                    + "\n".join(repl_lines) + "\n\n"
                    f"⏳ <i>Таймаут ожидания ответа: <b>{int(self.advisory_timeout_sec)} сек.</b>\n"
                    f"При отсутствии реакции: {action_desc}.\n"
                    f"Остальные монеты продолжают торги штатно.</i>\n\n"
                    f"Применить замену перед стартом нового цикла?"
                )
                from TG.tg_receiver import build_rotation_inline_keyboard
                kb = build_rotation_inline_keyboard(req_id, count=len(qualified_rotations))
                if hasattr(self.bot_core.notifier, "send_alert_with_keyboard"):
                    await self.bot_core.notifier.send_alert_with_keyboard(msg, kb)
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
          и возвращает монеты/пул в штатные торги, исключая 'уход в дзен'.
        """
        try:
            await asyncio.sleep(self.advisory_timeout_sec)
        except asyncio.CancelledError:
            return

        if req_id not in self.pending_rotations:
            return

        rot_data = self.pending_rotations.pop(req_id)
        self.timeout_tasks.pop(req_id, None)

        strat_id = rot_data["strat_id"]
        rotations = rot_data.get("rotations")
        if not rotations and "old_symbol" in rot_data:
            rotations = [rot_data]

        for r in rotations:
            self.symbols_pending_rotation.discard(r["old_symbol"])

        if self.advisory_timeout_action == "apply":
            logger.info(
                f"[RadarNavigator][{strat_id}] Таймаут advisory ({self.advisory_timeout_sec:.0f}с) истек. "
                f"Действие 'apply': автоматическое применение ротации ({len(rotations)} замен)."
            )
            applied_lines = []
            for r in rotations:
                await self.apply_rotation(
                    strat_id=strat_id,
                    old_symbol=r["old_symbol"],
                    new_symbol=r["new_symbol"],
                    old_score=r.get("old_score", 0.0),
                    new_score=r.get("new_score", 0.0),
                    lead_pct=r.get("lead_pct", 0.0),
                    matched_rule=rot_data.get("matched_rule", ""),
                    mode="advisory_timeout_apply"
                )
                applied_lines.append(f"• <b>{r['old_symbol']}</b> ➔ <b>{r['new_symbol']}</b> (+{r['lead_pct']:.1f}%)")

            if getattr(self.bot_core, "notifier", None):
                await self.bot_core.notifier.send_alert(
                    f"⚡ <b>ШТУРМАН: ТАЙМАУТ ADVISORY ({int(self.advisory_timeout_sec)}с) ИСТЕК</b>\n"
                    f"Оператор не ответил. Сработало действие: <code>apply</code>.\n"
                    f"Автоматически произведено замен: {len(rotations)}:\n"
                    + "\n".join(applied_lines) + "\n"
                    f"Торги продолжаются на обновленном составе!"
                )
        else:
            logger.info(
                f"[RadarNavigator][{strat_id}] Таймаут advisory ({self.advisory_timeout_sec:.0f}с) истек. "
                f"Действие 'skip': ротация отменена, боевой пул сохранен без изменений."
            )
            if getattr(self.bot_core, "notifier", None):
                skipped_syms = ", ".join(r["old_symbol"] for r in rotations)
                await self.bot_core.notifier.send_alert(
                    f"⌛ <b>ШТУРМАН: ТАЙМАУТ ADVISORY ({int(self.advisory_timeout_sec)}с) ИСТЕК</b>\n"
                    f"Оператор не ответил. Сработало действие: <code>skip</code>.\n"
                    f"Ротация отменена, монеты <b>{skipped_syms}</b> возвращены к торгам.\n"
                    f"Боевой пул сохранен без изменений, бот не простаивает!"
                )

    async def apply_rotation(
        self,
        strat_id: str,
        old_symbol: str,
        new_symbol: str,
        old_score: float = 0.0,
        new_score: float = 0.0,
        lead_pct: float = 0.0,
        matched_rule: str = "",
        mode: str = ""
    ) -> None:
        """Атомарная замена монеты в боевом боте с сохранением размера обоймы и записью в журнал."""
        self.symbols_pending_rotation.discard(old_symbol)
        self.symbols_pending_rotation.discard(new_symbol)

        if not new_symbol or (old_symbol and old_symbol == new_symbol):
            logger.warning(
                f"[RadarNavigator] Отклонена некорректная ротация: old='{old_symbol}', new='{new_symbol}'. "
                f"Боевой пул сохранен без изменений."
            )
            return

        strat_cfg = self.bot_core.strategy_registry.active_strategies.get(strat_id) if hasattr(self.bot_core, "strategy_registry") else None
        initial_count = len(strat_cfg.symbols) if strat_cfg else 0
        if strat_cfg:
            if old_symbol and old_symbol != "<none>" and old_symbol not in strat_cfg.symbols:
                logger.warning(f"[RadarNavigator] Монета {old_symbol} отсутствует в стратегии [{strat_id}]. Ротация отменена.")
                return
            if new_symbol in strat_cfg.symbols:
                logger.warning(f"[RadarNavigator] Монета-кандидат {new_symbol} уже активна в стратегии [{strat_id}]. Дублирование исключено.")
                return

        if old_symbol and old_symbol != "<none>":
            await self.bot_core.delete_symbol(old_symbol)
        await self.bot_core.add_symbol(new_symbol, strat_id)

        # Фиксация события смены монет в журнал кэша и текстовый леджер
        RadarJournal.record_rotation(
            strat_id=strat_id,
            old_symbol=old_symbol,
            new_symbol=new_symbol,
            old_score=old_score,
            new_score=new_score,
            lead_pct=lead_pct,
            matched_rule=matched_rule,
            mode=mode or self.mode
        )

        # Контрольная проверка сохранения размера боевой обоймы
        if strat_cfg:
            if len(strat_cfg.symbols) == initial_count:
                logger.info(
                    f"✅ [RadarNavigator] Ротация 1-к-1 успешно завершена: {old_symbol} -> {new_symbol} в стратегии [{strat_id}]. "
                    f"Размер обоймы сохранен ({len(strat_cfg.symbols)}): {strat_cfg.symbols}."
                )
            else:
                logger.warning(
                    f"⚠️ [RadarNavigator] Размер обоймы изменился после ротации [{strat_id}]: "
                    f"было {initial_count}, стало {len(strat_cfg.symbols)}: {strat_cfg.symbols}"
                )
        else:
            logger.info(f"✅ [RadarNavigator] Ротация успешно завершена: {old_symbol} -> {new_symbol} в стратегии {strat_id}.")

    @staticmethod
    def get_rotations_history(limit: int = 20) -> List[Dict[str, Any]]:
        """Возвращает историю ротаций из журнала."""
        return RadarJournal.get_history(limit=limit)

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
        recent_rotations = RadarJournal.get_history(limit=5)

        return RadarJournal.format_telemetry_report(
            strat_id=target_strat,
            runtime_hours=runtime,
            current_symbols=current_symbols,
            top_candidates=metrics,
            recent_rotations=recent_rotations
        )

