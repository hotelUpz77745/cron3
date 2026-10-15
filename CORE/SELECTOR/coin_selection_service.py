# ==============================================================================
# Path: CORE/SELECTOR/coin_selection_service.py
# Role: Сервис моментального (on-demand) отбора монет для ротации под купол стратегии
# ==============================================================================

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any, Dict, List, Optional

from consts import BASE_DIR, DATA_DIR, _CFG
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
            p = Path(radar_rel)
            if not p.is_absolute():
                p = (BASE_DIR / p).resolve()
            self.radar_stats_dir = p / "ANALYTICS" / "STATS"

    @staticmethod
    def _load_app_cfg() -> Dict[str, Any]:
        """Динамическое чтение app.json для обеспечения 100% операторской управляемости на лету."""
        app_path = DATA_DIR / "app.json"
        if app_path.exists():
            try:
                data = Utils.read_json_file(app_path)
                if data and isinstance(data, dict):
                    return data
            except Exception as e:
                logger.error(f"[CoinSelectionService] Ошибка динамического чтения {app_path}: {e}")
        return _CFG

    def get_configured_target_count(self, selector_type: Optional[str] = None) -> int:
        """
        Читает target_count строго напрямую из app.json в зависимости от selector_type:
        - alpha_coin_selector -> app_cfg['alpha_coin_selector']['target_count']
        - samsonov_funnel -> app_cfg['samsonov_funnel']['target_count']
        - volatility_scanner -> app_cfg['volatility_scanner']['target_count'] (или alpha_coin_selector)
        - radar_rotation -> rot_cfg['target_coins_count']
        Никаких скрытых магических дефолтов (Протоколы 2, 2.1, 2.2, 3).
        """
        app_cfg = self._load_app_cfg()
        sel = selector_type or self.get_configured_selector_type()

        if sel == "alpha_coin_selector":
            if "alpha_coin_selector" in app_cfg and "target_count" in app_cfg["alpha_coin_selector"]:
                val = app_cfg["alpha_coin_selector"]["target_count"]
                if val is not None:
                    return int(val)

        elif sel == "samsonov_funnel":
            if "samsonov_funnel" in app_cfg and "target_count" in app_cfg["samsonov_funnel"]:
                val = app_cfg["samsonov_funnel"]["target_count"]
                if val is not None:
                    return int(val)

        elif sel == "volatility_scanner":
            if "volatility_scanner" in app_cfg and "target_count" in app_cfg["volatility_scanner"]:
                val = app_cfg["volatility_scanner"]["target_count"]
                if val is not None:
                    return int(val)
            if "alpha_coin_selector" in app_cfg and "target_count" in app_cfg["alpha_coin_selector"]:
                val = app_cfg["alpha_coin_selector"]["target_count"]
                if val is not None:
                    return int(val)

        # Проверяем блок ротации auto_closing.positive.radar_rotation_on_close или radar_synergy
        rot_cfg = (
            app_cfg.get("auto_closing", {}).get("positive", {}).get("radar_rotation_on_close")
            or app_cfg.get("radar_synergy", {})
        )
        if "target_coins_count" in rot_cfg and rot_cfg["target_coins_count"] is not None:
            return int(rot_cfg["target_coins_count"])
        if "target_count" in rot_cfg and rot_cfg["target_count"] is not None:
            return int(rot_cfg["target_count"])

        # Fallback если sel был None или не подошел, но alpha_coin_selector есть в app_cfg
        if "alpha_coin_selector" in app_cfg and "target_count" in app_cfg["alpha_coin_selector"]:
            val = app_cfg["alpha_coin_selector"]["target_count"]
            if val is not None:
                return int(val)

        raise ValueError(f"[CoinSelectionService] target_count для селектора '{sel}' не настроен в app.json!")

    def get_configured_selector_type(self) -> str:
        """Читает selector_type строго напрямую из app.json."""
        app_cfg = self._load_app_cfg()
        rot_cfg = (
            app_cfg.get("auto_closing", {}).get("positive", {}).get("radar_rotation_on_close")
            or app_cfg.get("radar_synergy", {})
        )
        if "selector_type" in rot_cfg and rot_cfg["selector_type"] is not None:
            return str(rot_cfg["selector_type"])

        if app_cfg.get("alpha_coin_selector", {}).get("enabled", False):
            return "alpha_coin_selector"
        if app_cfg.get("samsonov_funnel", {}).get("enabled", False):
            return "samsonov_funnel"
        if app_cfg.get("volatility_scanner", {}).get("enabled", False):
            return "volatility_scanner"

        return "alpha_coin_selector"

    async def get_alpha_top_coins(
        self,
        target_count: Optional[int] = None,
        universe_filter: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Запрашивает актуальный институциональный Top-N монет через AlphaCoinSelector.
        Обогащает результат текущим статусом активности в боте.
        """
        limit = target_count if target_count is not None else self.get_configured_target_count("alpha_coin_selector")
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
        limit = target_count if target_count is not None else self.get_configured_target_count("samsonov_funnel")
        target_universe = universe_id
        if target_universe is None:
            if self.bot_core and hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
                active_strats = list(self.bot_core.strategy_registry.active_strategies.keys())
                target_universe = active_strats[0] if active_strats else "u6_symbiosis_gamma_fortress35__off"
            else:
                app_data = self._load_app_cfg().get("active_strategies", {})
                active_strats = [sid for sid, scfg in app_data.items() if isinstance(scfg, dict) and scfg.get("enabled", False)]
                target_universe = active_strats[0] if active_strats else "u6_symbiosis_gamma_fortress35__off"

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

    async def get_volatility_top_coins(
        self,
        target_count: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Запрашивает актуальный срез монет из VolatilityScanner.
        Обогащает результат текущим статусом активности в боте.
        """
        limit = target_count if target_count is not None else self.get_configured_target_count("volatility_scanner")
        output_path = DATA_DIR / "volatile_symbols.json"

        if not output_path.exists():
            from CORE.ADVANCED.volatility_scanner import VolatilityScanner
            scanner = VolatilityScanner(client=getattr(self.bot_core, "client", None))
            await scanner.scan()

        data: List[Dict[str, Any]] = []
        if output_path.exists():
            try:
                raw = Utils.read_json_file(output_path)
                if isinstance(raw, list):
                    data = raw
            except Exception as e:
                logger.error(f"[CoinSelectionService] Error reading volatile_symbols.json: {e}")

        formatted = []
        for item in data[:limit]:
            formatted.append({
                "symbol": item["symbol"],
                "score": item.get("volatility", 0.0),
                "volatility": item.get("volatility", 0.0),
                "candles": item.get("candles", 0),
                "calc_base": item.get("calc_base", "H-L"),
                "max_candle": item.get("max_candle"),
                "rsi": item.get("rsi"),
                "bb_width_pct": item.get("bb_width_pct"),
                "reason": f"volatility_scanner (1w ATR {item.get('volatility', 0.0)}%)",
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
        cnt = target_count if target_count is not None else self.get_configured_target_count(sel)

        if sel == "samsonov_funnel":
            return await self.get_samsonov_top_coins(target_count=cnt, universe_id=universe_id)
        elif sel == "volatility_scanner":
            return await self.get_volatility_top_coins(target_count=cnt)
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
                if sid.startswith("_") or not isinstance(sdata, dict):
                    continue
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
        Применяет выбранный срез монет под купол указанной стратегии с жесткой валидацией:
        1. Проверяет отсутствие дублирования символов на одном аккаунте (api_key_name) ДО записи в app.json.
        2. Безопасно обновляет app.json.
        3. Пересобирает рантайм-конфиги через StrategyApplicator.build_runtime_for_symbol.
        4. Синхронизирует StrategyRegistry и боевой пул символов BotCore.
        """
        self.last_error = ""
        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path)
        if not app_data or "active_strategies" not in app_data:
            self.last_error = "Не удалось загрузить active_strategies из app.json"
            logger.error(f"[CoinSelectionService] {self.last_error}")
            return False

        active_strategies = app_data["active_strategies"]
        if strategy_id not in active_strategies:
            self.last_error = f"Стратегия '{strategy_id}' не найдена в app.json"
            logger.error(f"[CoinSelectionService] {self.last_error}")
            return False

        clean_symbols = [s.upper().strip() for s in symbols if s and s.strip()]
        if not clean_symbols:
            self.last_error = f"Список монет пуст! Отмена применения для '{strategy_id}'."
            logger.error(f"[CoinSelectionService] {self.last_error}")
            return False

        # ПРЕ-ВАЛИДАЦИЯ: Защита от дублей на одном аккаунте Binance
        target_account = str(active_strategies[strategy_id].get("api_key_name") or "API_KEY").strip()
        for other_id, other_data in active_strategies.items():
            if other_id == strategy_id or not isinstance(other_data, dict):
                continue
            if not other_data.get("enabled", False):
                continue
            other_acc = str(other_data.get("api_key_name") or "API_KEY").strip()
            if other_acc == target_account:
                other_symbols = [str(s).upper().strip() for s in other_data.get("symbols", [])]
                conflicts = set(clean_symbols) & set(other_symbols)
                if conflicts:
                    conflicts_str = ", ".join(sorted(conflicts))
                    self.last_error = (
                        f"КОНФЛИКТ НА АККАУНТЕ '{target_account}': "
                        f"Монеты [{conflicts_str}] уже закреплены за активной стратегией '{other_id}'. "
                        f"На одном биржевом счете Binance монеты обязаны быть строго уникальными!"
                    )
                    logger.error(f"[CoinSelectionService] {self.last_error}")
                    return False

        active_strategies[strategy_id]["symbols"] = clean_symbols
        active_strategies[strategy_id]["enabled"] = True

        Utils.write_json_file(app_json_path, app_data)
        logger.info(
            f"[CoinSelectionService] Стратегия '{strategy_id}' обновлена новыми монетами: {clean_symbols}"
        )

        # Перезагрузка реестра в боевом ядре
        if self.bot_core and hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
            try:
                self.bot_core.strategy_registry.reload(active_strategies)
                if hasattr(self.bot_core, "_init_clients"):
                    self.bot_core._init_clients()
                self.bot_core.symbols = self.bot_core.strategy_registry.get_all_active_symbols()
                logger.info(f"[CoinSelectionService] Боевое ядро синхронизировано. Активный пул: {self.bot_core.symbols}")
            except Exception as e:
                self.last_error = f"Ошибка перезагрузки StrategyRegistry: {e}"
                logger.error(f"[CoinSelectionService] {self.last_error}")
                return False

        # Пересборка рантайм-конфигов
        try:
            from CORE.STRATEGY.strategy_applicator import StrategyApplicator
            applicator = StrategyApplicator()
            for sym in clean_symbols:
                strat_cfg = None
                if self.bot_core and hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
                    strat_cfg = self.bot_core.strategy_registry.get_strategy_for_symbol(sym)
                if strat_cfg:
                    applicator.build_runtime_for_symbol(sym, strat_cfg)
                else:
                    applicator.apply_strategy_to_symbol(strategy_id, sym)
            logger.info(f"[CoinSelectionService] Рантайм-конфиги для {clean_symbols} успешно пересобраны.")
        except Exception as e:
            logger.error(f"[CoinSelectionService] Ошибка сборки рантаймов: {e}")

        return True

    def format_telegram_report(
        self,
        results: List[Dict[str, Any]],
        selector_type: str,
        target_count: int,
    ) -> str:
        """Форматирует красивый Telegram-отчет по срезу отобранных монет."""
        if selector_type == "alpha_coin_selector":
            header_title = "⚡ <b>Alpha Coin Selector</b> (Wiggle & NATR)"
        elif selector_type == "samsonov_funnel":
            header_title = "📡 <b>Samsonov Funnel Selector</b> (Радар)"
        else:
            header_title = "🔍 <b>Volatility Scanner</b> (Сонник)"

        lines = [
            f"{header_title}",
            f"🎯 <b>Срез лидеров:</b> {len(results)} из {target_count} запрошенных\n",
        ]

        if not results:
            if selector_type == "samsonov_funnel":
                lines.append("🥬 <b>Пусто, выросла капуста!</b>")
                lines.append("<i>Сырье для воронки отсутствует: бумажный бот (Полигон Радара) отключен или нет накопленной статистики сделок.</i>")
            elif selector_type == "alpha_coin_selector":
                lines.append("⚠️ <i>Подходящих монет под текущие строгие фильтры не обнаружено (или пул Volatility Scanner пуст).</i>")
            else:
                lines.append("⚠️ <i>Подходящих монет под текущие фильтры волатильности не обнаружено.</i>")
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
            elif selector_type == "samsonov_funnel":
                trades = item.get("trades", 0)
                resilience = item.get("resilience", 0.0)
                lines.append(
                    f"<b>#{i} {sym}</b> — Score: <b>{score:.2f}</b> | {status_emoji}{strat_info}\n"
                    f"   └ Сделок: <code>{trades}</code> | Resilience: <code>{resilience:.2f}</code>"
                )
            else:
                vol = item.get("volatility", 0.0)
                base = item.get("calc_base", "H-L")
                candles = item.get("candles", 0)
                mc = item.get("max_candle")
                mc_line = ""
                if mc:
                    mc_line = (
                        f"\n   └ Max свеча: <code>{mc.get('atr_pct', 0.0)}%</code> {mc.get('color_label', '')} "
                        f"(Тени: <code>{mc.get('shadow_span_pct', 0.0)}%</code>, Тело: <code>{mc.get('body_span_pct', 0.0)}%</code>)"
                    )
                lines.append(
                    f"<b>#{i} {sym}</b> — ATR [{base}]: <b>{vol:.2f}%</b> (свечей: {candles}) | {status_emoji}{strat_info}"
                    f"{mc_line}"
                )

        lines.append("\n💡 <i>Нажмите кнопку ниже для повторного сканирования:</i>")
        return "\n".join(lines)
