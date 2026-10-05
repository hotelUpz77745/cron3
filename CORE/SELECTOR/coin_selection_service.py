# ==============================================================================
# Path: CORE/SELECTOR/coin_selection_service.py
# Role: Сервис моментального (on-demand) отбора монет для ротации под купол стратегии
# ==============================================================================

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any, Dict, List, Optional

from consts import DATA_DIR, _CFG
from c_log import UnifiedLogger
from c_utils import Utils
from CORE.SELECTOR.alpha_coin_selector import AlphaCoinSelector
from CORE.coin_selectors import SamsonovFunnelSelector

logger = UnifiedLogger("CoinSelectionService")


class CoinSelectionService:
    """
    On-Demand сервис вызова актуального списка монет для боевого бота.
    Позволяет в любой момент через Telegram или API запросить срез лидеров:
    1. AlphaCoinSelector (Mean-Reversion, Wiggle Density, NATR, Anti-Pump/Dump, Alpha Score)
    2. SamsonovFunnelSelector (3-ступенчатая воронка полигона Радара)
    
    Поддерживает применение отобранного среза под купол указанной стратегии.
    Конфигурация читается строго напрямую через [''] (Правило 2).
    Дефолты в боевом коде — строго None type (Правило 2.2).
    """

    def __init__(self, bot_core: Optional[Any] = None, radar_stats_dir: Optional[Path] = None):
        self.bot_core = bot_core
        if radar_stats_dir is not None:
            self.radar_stats_dir = radar_stats_dir
        else:
            # Прямое чтение пути к радару из app.json
            radar_rel = _CFG["paths"]["radar_dir"]
            self.radar_stats_dir = (DATA_DIR / radar_rel / "ANALYTICS" / "STATS").resolve()

    def get_configured_target_count(self) -> int:
        """Читает target_coins_count строго напрямую из CFG['radar_synergy']['target_coins_count']."""
        val = _CFG["radar_synergy"]["target_coins_count"]
        if val is None:
            raise ValueError("[CoinSelectionService] target_coins_count в app.json не может быть None!")
        return int(val)

    def get_configured_selector_type(self) -> str:
        """Читает selector_type строго напрямую из CFG['radar_synergy']['selector_type']."""
        val = _CFG["radar_synergy"]["selector_type"]
        if val is None:
            raise ValueError("[CoinSelectionService] selector_type в app.json не может быть None!")
        return str(val)

    async def get_alpha_top_coins(
        self,
        target_count: Optional[int] = None,
        universe_filter: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Запрашивает актуальный институциональный Top-N монет через AlphaCoinSelector.
        Обогащает результат текущим статусом активности в боте.
        """
        limit = target_count if target_count is not None else self.get_configured_target_count()
        results = await AlphaCoinSelector.select_top_coins(target_count=limit, universe_filter=universe_filter)
        self._enrich_with_bot_status(results)
        return results

    async def get_samsonov_top_coins(
        self,
        target_count: Optional[int] = None,
        universe_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Запрашивает актуальный Top-N монет по воронке Самсонова из статистики полигона Радара.
        Обогащает результат текущим статусом активности в боте.
        """
        limit = target_count if target_count is not None else self.get_configured_target_count()
        target_universe = universe_id
        if target_universe is None:
            # Выбираем первую активную стратегию или дефолтную u6
            if self.bot_core and hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
                active_strats = list(self.bot_core.strategy_registry.active_strategies.keys())
                target_universe = active_strats[0] if active_strats else "u6_symbiosis_gamma_fortress35"
            else:
                target_universe = "u6_symbiosis_gamma_fortress35"

        selector = SamsonovFunnelSelector(radar_stats_dir=self.radar_stats_dir)
        funnel_data = selector.get_funnel_stages(universe_id=target_universe, target_count=limit)
        targets = funnel_data.get("final_targets", [])

        # Форматируем к единому виду
        formatted = []
        for t in targets:
            formatted.append({
                "symbol": t["symbol"],
                "score": t.get("resilience", 0.0),
                "trades": t.get("trades", 0),
                "realized_net": t.get("realized_net", 0.0),
                "peak_net": t.get("peak_net", 0.0),
                "max_dd": t.get("max_dd", 0.0),
                "resilience": t.get("resilience", 0.0),
                "reason": f"samsonov_funnel_step3 (trades: {t.get('trades', 0)})",
            })

        self._enrich_with_bot_status(formatted)
        return formatted

    async def get_top_coins(
        self,
        selector_type: Optional[str] = None,
        target_count: Optional[int] = None,
        universe_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Универсальный фасад получения среза монет по указанному или настроенному селектору.
        """
        sel = selector_type if selector_type is not None else self.get_configured_selector_type()
        cnt = target_count if target_count is not None else self.get_configured_target_count()

        if sel == "samsonov_funnel":
            return await self.get_samsonov_top_coins(target_count=cnt, universe_id=universe_id)
        return await self.get_alpha_top_coins(target_count=cnt)

    def _enrich_with_bot_status(self, coin_list: List[Dict[str, Any]]) -> None:
        """Добавляет информацию о том, торгуется ли монета прямо сейчас и под какой стратегией."""
        active_symbols = set()
        symbol_to_strat: Dict[str, str] = {}

        if self.bot_core and hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
            active_symbols = set(self.bot_core.strategy_registry.get_all_active_symbols())
            for sym in active_symbols:
                s_id = self.bot_core.strategy_registry.get_strategy_id_for_symbol(sym)
                if s_id:
                    symbol_to_strat[sym] = s_id
        else:
            # Читаем напрямую из app.json
            app_cfg = Utils.read_json_file(DATA_DIR / "app.json") or {}
            for sid, sdata in (app_cfg.get("active_strategies") or {}).items():
                if sdata.get("enabled", False):
                    for sym in sdata.get("symbols", []):
                        s_clean = sym.upper().strip()
                        active_symbols.add(s_clean)
                        symbol_to_strat[s_clean] = sid

        for item in coin_list:
            sym = str(item.get("symbol", "")).upper().strip()
            item["is_active_in_bot"] = sym in active_symbols
            item["current_strategy"] = symbol_to_strat.get(sym)

    async def apply_coins_to_strategy(self, strategy_id: str, symbols: List[str]) -> bool:
        """
        Применяет выбранный срез монет под купол указанной стратегии:
        1. Записывает список symbols в app.json для strategy_id.
        2. Генерирует изолированные рантайм-конфиги через StrategyApplicator.
        3. Перезагружает StrategyRegistry в bot_core и инициализирует клиенты.
        """
        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path)
        if not app_data or "active_strategies" not in app_data:
            logger.error("[CoinSelectionService] Не удалось загрузить active_strategies из app.json!")
            return False

        active_strategies = app_data["active_strategies"]
        if strategy_id not in active_strategies:
            logger.error(f"[CoinSelectionService] Стратегия '{strategy_id}' не найдена в app.json!")
            return False

        clean_symbols = [s.upper().strip() for s in symbols if s and s.strip()]
        active_strategies[strategy_id]["symbols"] = clean_symbols
        active_strategies[strategy_id]["enabled"] = True

        Utils.write_json_file(app_json_path, app_data)
        logger.info(
            f"[CoinSelectionService] Стратегия '{strategy_id}' обновлена новыми монетами: {clean_symbols}"
        )

        # Пересборка рантайм-конфигов
        try:
            from CORE.STRATEGY.strategy_applicator import StrategyApplicator
            applicator = StrategyApplicator()
            for sym in clean_symbols:
                applicator.apply_strategy_to_symbol(strategy_id, sym)
            logger.info(f"[CoinSelectionService] Рантайм-конфиги для {clean_symbols} пересобраны.")
        except Exception as e:
            logger.error(f"[CoinSelectionService] Ошибка сборки рантаймов: {e}")

        # Перезагрузка реестра и пула клиентов в боевом ядре
        if self.bot_core:
            if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
                try:
                    self.bot_core.strategy_registry.reload(active_strategies)
                    if hasattr(self.bot_core, "_init_clients"):
                        self.bot_core._init_clients()
                    self.bot_core.symbols = self.bot_core.strategy_registry.get_all_active_symbols()
                    logger.info(f"[CoinSelectionService] Боевое ядро синхронизировано. Активный пул: {self.bot_core.symbols}")
                except Exception as e:
                    logger.error(f"[CoinSelectionService] Ошибка синхронизации BotCore: {e}")

        return True

    def format_telegram_report(
        self,
        results: List[Dict[str, Any]],
        selector_type: str,
        target_count: int,
    ) -> str:
        """Форматирует красивый Telegram-отчет по срезу отобранных монет."""
        header_title = (
            "⚡ <b>Alpha Coin Selector</b> (Wiggle & NATR)"
            if selector_type == "alpha_coin_selector"
            else "📡 <b>Samsonov Funnel Selector</b> (Радар)"
        )

        lines = [
            f"{header_title}",
            f"🎯 <b>Срез лидеров:</b> {len(results)} из {target_count} запрошенных\n",
        ]

        if not results:
            lines.append("⚠️ <i>Подходящих монет под текущие строгие фильтры не обнаружено.</i>")
            return "\n".join(lines)

        for i, item in enumerate(results, start=1):
            sym = item.get("symbol", "UNKNOWN")
            score = item.get("score", 0.0)
            status_emoji = "🟢 В бою" if item.get("is_active_in_bot") else "⚪ Свободна"
            strat_info = f" ({item['current_strategy']})" if item.get("current_strategy") else ""

            if selector_type == "alpha_coin_selector":
                natr = item.get("natr", 0.0)
                wiggle = item.get("wiggle", 0.0)
                move = item.get("move_pct", 0.0)
                lines.append(
                    f"<b>#{i} {sym}</b> — Score: <b>{score:.3f}</b> | {status_emoji}{strat_info}\n"
                    f"   └ NATR: <code>{natr:.2f}%</code> | Wiggle: <code>{wiggle:.1f}</code> | Shift: <code>{move:+.1f}%</code>"
                )
            else:
                trades = item.get("trades", 0)
                resilience = item.get("resilience", 0.0)
                lines.append(
                    f"<b>#{i} {sym}</b> — Score: <b>{score:.2f}</b> | {status_emoji}{strat_info}\n"
                    f"   └ Сделок: <code>{trades}</code> | Resilience: <code>{resilience:.2f}</code>"
                )

        lines.append("\n💡 <i>Нажмите кнопку ниже, чтобы моментально применить этот срез под купол выбранной стратегии:</i>")
        return "\n".join(lines)
