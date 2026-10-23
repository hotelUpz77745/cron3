# ==============================================================================
# Path: TG/HANDLERS/coins_size_router.py
# Role: Роутер управления размерами инвестиций (Invest Size) куполов стратегий
# ==============================================================================

from typing import Any, List, Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from consts import DATA_DIR
from c_log import UnifiedLogger
from c_utils import Utils
from CORE.strategy_applicator import StrategyApplicator
from TG.keyboards import (
    get_back_keyboard,
    get_coins_sizes_menu_keyboard,
    get_dynamic_coins_keyboard,
    get_set_coins_dome_inline_keyboard,
    get_set_coins_keyboard,
)
from TG.states import TGStates
from TG.strategy_helpers import extract_strategy_symbols, get_strategy_meta

logger = UnifiedLogger("CoinsSizeRouter")


class CoinsSizeRouterHandler:
    """Обработчик меню управления размерами инвестиций (Invest Size) для куполов."""

    def __init__(self, bot_core: Any):
        self.bot_core = bot_core
        self.router = Router(name="coins_size_router")
        self._register_routes()

    def _register_routes(self) -> None:
        r = self.router
        r.message(F.text.in_({"💰 Сайзы (Invest Size)", "💰 Сайзы"}))(self.on_sizes_btn)
        r.message(TGStates.waiting_for_size_side_choice)(self.on_side_choice)
        r.message(TGStates.waiting_for_size_coin_choice)(self.on_coin_choice)
        r.message(TGStates.waiting_for_size_value_input)(self.on_size_value_input)
        r.message(TGStates.waiting_for_default_size_value_input)(self.on_default_size_value_input)

    async def _resolve_current_dome(self, state: FSMContext) -> Optional[str]:
        """Определяет ID текущего купола из FSM или активных стратегий."""
        data = await state.get_data()
        dome_id = data.get("selected_dome")
        if dome_id:
            return dome_id

        active_strats = {}
        if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
            active_strats = self.bot_core.strategy_registry.get_active_strategies()
        if not active_strats:
            app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
            active_strats = app_data.get("active_strategies", {})

        active_keys = [k for k, v in active_strats.items() if not k.startswith("_") and (v.get("enabled", True) if isinstance(v, dict) else getattr(v, "enabled", True))]
        if len(active_keys) == 1:
            dome_id = active_keys[0]
            await state.update_data(selected_dome=dome_id)
            return dome_id
        return None

    def _get_dome_symbols(self, dome_id: str, app_data: dict) -> List[str]:
        """Возвращает динамический список активных монет выбранного купола."""
        symbols: List[str] = []
        if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
            active_strats = self.bot_core.strategy_registry.get_active_strategies()
            if dome_id in active_strats:
                symbols = list(active_strats[dome_id].symbols)
        if not symbols and "active_strategies" in app_data and dome_id in app_data["active_strategies"]:
            symbols = extract_strategy_symbols(app_data["active_strategies"][dome_id])
        return sorted(list(set(symbols)))

    def _render_dome_sizes_summary(self, dome_id: str, app_data: dict) -> str:
        """Формирует текстовую сводку текущих сайзов купола из app.json."""
        strat_entry = app_data["active_strategies"][dome_id]
        meta = get_strategy_meta(dome_id, self.bot_core)
        s_name = meta.get("name", dome_id)
        symbols = self._get_dome_symbols(dome_id, app_data)

        lines = [
            "💰 <b>СВОДКА САЙЗОВ КУПОЛА СТРАТЕГИИ</b>",
            f"🌌 <b>Купол:</b> <code>{dome_id}</code>",
            f"🏷️ <b>Название:</b> {s_name}\n",
        ]

        for side in ("LONG", "SHORT"):
            side_cfg = strat_entry.get(side, {})
            is_enabled = side_cfg.get("enabled", True)
            def_inv = side_cfg.get("default_invest_size", 0.0)
            inv_dict = side_cfg.get("invest_size", {})
            icon = "🟢" if side == "LONG" else "🔴"

            lines.append(f"{icon} <b>{side}</b> (enabled: <code>{is_enabled}</code>):")
            lines.append(f"├ <b>default_invest_size:</b> <code>{def_inv} USDT</code>")
            if symbols:
                lines.append(f"└ <b>Монеты ({len(symbols)}):</b>")
                for s in symbols:
                    val = inv_dict.get(s, def_inv)
                    is_custom = s in inv_dict
                    marker = " <i>(кастом)</i>" if is_custom else " <i>(дефолт)</i>"
                    lines.append(f"   • <code>{s}</code>: <b>{val} USDT</b>{marker}")
            else:
                lines.append("└ <b>Монеты:</b> <i>нет монет</i>")
            lines.append("")

        lines.append("<i>Выберите действие в нижнем меню:</i>")
        return "\n".join(lines)

    async def on_sizes_btn(self, message: Message, state: FSMContext) -> None:
        """Точка входа: вывод сводки сайзов купола и меню выбора действия."""
        dome_id = await self._resolve_current_dome(state)
        app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}

        if not dome_id:
            active_strats = {}
            if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
                active_strats = self.bot_core.strategy_registry.get_active_strategies()
            if not active_strats:
                active_strats = app_data.get("active_strategies", {})
            kb = get_set_coins_dome_inline_keyboard(active_strats, self.bot_core)
            await message.answer("⚙️ <b>Выберите купол для настройки сайзов:</b>", reply_markup=kb, parse_mode="HTML")
            return

        if "active_strategies" not in app_data or dome_id not in app_data["active_strategies"]:
            await message.answer(f"⚠️ Купол <code>{dome_id}</code> не найден в CFG/app.json.", reply_markup=get_set_coins_keyboard(), parse_mode="HTML")
            return

        summary = self._render_dome_sizes_summary(dome_id, app_data)
        await state.update_data(selected_dome=dome_id)
        await state.set_state(TGStates.waiting_for_size_side_choice)
        await message.answer(summary, reply_markup=get_coins_sizes_menu_keyboard(), parse_mode="HTML")

    async def on_side_choice(self, message: Message, state: FSMContext) -> None:
        """Обработка выбора направления (LONG, SHORT, BOTH, дефолт или Назад)."""
        text = (message.text or "").strip()

        if text in {"🔙 Назад в Set Coins", "🔙 Назад", "🔙 Back"}:
            await state.set_state(None)
            await message.answer("⚙️ Меню управления монетами купола:", reply_markup=get_set_coins_keyboard(), parse_mode="HTML")
            return

        data = await state.get_data()
        dome_id = data.get("selected_dome") or await self._resolve_current_dome(state)
        app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}

        if text in {"🟢 Настроить LONG", "🔴 Настроить SHORT", "⚖️ Обе стороны (BOTH)"}:
            if "LONG" in text:
                chosen_side = "LONG"
            elif "SHORT" in text:
                chosen_side = "SHORT"
            else:
                chosen_side = "BOTH"

            symbols = self._get_dome_symbols(dome_id, app_data)
            if not symbols:
                await message.answer(f"⚠️ В куполе <code>{dome_id}</code> нет активных монет.", reply_markup=get_coins_sizes_menu_keyboard(), parse_mode="HTML")
                return

            await state.update_data(target_side=chosen_side, selected_dome=dome_id)
            await state.set_state(TGStates.waiting_for_size_coin_choice)
            await message.answer(
                f"🎯 <b>Настройка сайзов: {chosen_side}</b>\n"
                f"<i>Нажмите на кнопку с тикером монеты в меню ниже, чтобы изменить размер инвестиции:</i>",
                reply_markup=get_dynamic_coins_keyboard(symbols, back_text="🔙 Назад к сайзам"),
                parse_mode="HTML",
            )
            return

        if text == "🌐 Базовый дефолт":
            strat_entry = app_data.get("active_strategies", {}).get(dome_id, {})
            long_def = strat_entry.get("LONG", {}).get("default_invest_size", "н/д")
            short_def = strat_entry.get("SHORT", {}).get("default_invest_size", "н/д")
            await state.update_data(selected_dome=dome_id)
            await state.set_state(TGStates.waiting_for_default_size_value_input)
            await message.answer(
                f"✍️ <b>Изменение базового размера (default_invest_size)</b>\n"
                f"Текущие значения:\n"
                f"• <b>LONG:</b> <code>{long_def} USDT</code>\n"
                f"• <b>SHORT:</b> <code>{short_def} USDT</code>\n\n"
                f"<i>Это базовый сайз для всех монет купола, у которых нет индивидуального переопределения.</i>\n\n"
                f"Отправьте числом новый базовый сайз в USDT (например: <code>250</code>):",
                reply_markup=get_back_keyboard(),
                parse_mode="HTML",
            )
            return

        await message.answer("⚠️ Выберите действие с помощью кнопок в меню ниже:", reply_markup=get_coins_sizes_menu_keyboard(), parse_mode="HTML")

    async def on_coin_choice(self, message: Message, state: FSMContext) -> None:
        """Обработка нажатия на кнопку монеты купола."""
        text = (message.text or "").strip()

        if text in {"🔙 Назад к сайзам", "🔙 Назад", "🔙 Back"}:
            return await self.on_sizes_btn(message, state)

        symbol = text.upper()
        data = await state.get_data()
        dome_id = data.get("selected_dome") or await self._resolve_current_dome(state)
        chosen_side = data.get("target_side", "BOTH")
        app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}

        valid_symbols = self._get_dome_symbols(dome_id, app_data)
        if symbol not in valid_symbols:
            await message.answer(
                f"⚠️ Монета <code>{symbol}</code> не найдена в куполе <code>{dome_id}</code>.\n"
                f"Выберите монету из кнопок в меню ниже:",
                reply_markup=get_dynamic_coins_keyboard(valid_symbols, back_text="🔙 Назад к сайзам"),
                parse_mode="HTML",
            )
            return

        strat_entry = app_data.get("active_strategies", {}).get(dome_id, {})
        ref_side = "LONG" if chosen_side in ("LONG", "BOTH") else "SHORT"
        side_dict = strat_entry.get(ref_side, {})
        cur_size = side_dict.get("invest_size", {}).get(symbol, side_dict.get("default_invest_size", 0.0))
        def_size = side_dict.get("default_invest_size", 0.0)

        await state.update_data(target_symbol=symbol)
        await state.set_state(TGStates.waiting_for_size_value_input)
        await message.answer(
            f"✍️ <b>Изменение Invest Size для {symbol}</b>\n"
            f"📌 <b>Направление:</b> {chosen_side}\n"
            f"💰 <b>Текущий сайз:</b> <code>{cur_size} USDT</code> (базовый дефолт: <code>{def_size} USDT</code>)\n\n"
            f"Отправьте числом новый размер инвестиции в USDT (например: <code>350</code>):",
            reply_markup=get_back_keyboard(),
            parse_mode="HTML",
        )

    async def on_size_value_input(self, message: Message, state: FSMContext) -> None:
        """Обработка числового ввода размера инвестиции для выбранной монеты."""
        text = (message.text or "").strip()

        if text in {"🔙 Back", "🔙 Назад"}:
            data = await state.get_data()
            dome_id = data.get("selected_dome") or await self._resolve_current_dome(state)
            chosen_side = data.get("target_side", "BOTH")
            app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
            symbols = self._get_dome_symbols(dome_id, app_data)
            await state.set_state(TGStates.waiting_for_size_coin_choice)
            await message.answer(
                f"🎯 <b>Настройка сайзов: {chosen_side}</b>\n"
                f"<i>Нажмите на кнопку с тикером монеты в меню ниже, чтобы изменить размер инвестиции:</i>",
                reply_markup=get_dynamic_coins_keyboard(symbols, back_text="🔙 Назад к сайзам"),
                parse_mode="HTML",
            )
            return

        try:
            val = float(text.replace(",", "."))
            if val <= 0:
                raise ValueError("Invest size must be positive")
        except ValueError:
            await message.answer(
                "❌ <b>Некорректный размер!</b>\n"
                "Введите положительное число больше 0 (например: <code>350</code> или <code>250.5</code>):",
                reply_markup=get_back_keyboard(),
                parse_mode="HTML",
            )
            return

        data = await state.get_data()
        dome_id = data.get("selected_dome") or await self._resolve_current_dome(state)
        symbol = data.get("target_symbol")
        chosen_side = data.get("target_side", "BOTH")

        if not symbol or not dome_id:
            await state.clear()
            await message.answer("⚠️ Сессия истекла. Пожалуйста, начните заново.", reply_markup=get_set_coins_keyboard(), parse_mode="HTML")
            return

        try:
            await self._apply_invest_size_update(dome_id, symbol, chosen_side, val)
            app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
            summary = self._render_dome_sizes_summary(dome_id, app_data)
            await message.answer(
                f"✅ <b>Сайз для {symbol} успешно обновлен!</b>\n"
                f"📌 <b>Направление:</b> {chosen_side}\n"
                f"💰 <b>Новый Invest Size:</b> <code>{val} USDT</code>\n"
                f"⚡ <i>Изменения записаны в CFG/app.json и мгновенно применены в рантайме бота.</i>\n\n"
                f"{summary}",
                reply_markup=get_coins_sizes_menu_keyboard(),
                parse_mode="HTML",
            )
            await state.set_state(TGStates.waiting_for_size_side_choice)
        except Exception as e:
            logger.error(f"Error applying invest size update for {symbol} ({chosen_side}): {e}")
            await message.answer(f"❌ Ошибка сохранения сайза: {e}", reply_markup=get_coins_sizes_menu_keyboard(), parse_mode="HTML")

    async def on_default_size_value_input(self, message: Message, state: FSMContext) -> None:
        """Обработка ввода нового базового default_invest_size купола."""
        text = (message.text or "").strip()

        if text in {"🔙 Back", "🔙 Назад"}:
            return await self.on_sizes_btn(message, state)

        try:
            val = float(text.replace(",", "."))
            if val <= 0:
                raise ValueError("Default invest size must be positive")
        except ValueError:
            await message.answer(
                "❌ <b>Некорректный размер!</b>\n"
                "Введите положительное число больше 0 (например: <code>250</code>):",
                reply_markup=get_back_keyboard(),
                parse_mode="HTML",
            )
            return

        data = await state.get_data()
        dome_id = data.get("selected_dome") or await self._resolve_current_dome(state)

        if not dome_id:
            await state.clear()
            await message.answer("⚠️ Купол не выбран. Начните заново.", reply_markup=get_set_coins_keyboard(), parse_mode="HTML")
            return

        try:
            await self._apply_default_invest_size_update(dome_id, val)
            app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
            summary = self._render_dome_sizes_summary(dome_id, app_data)
            await message.answer(
                f"✅ <b>Базовый default_invest_size успешно обновлен!</b>\n"
                f"💰 <b>Новый базовый сайз:</b> <code>{val} USDT</code> (для LONG и SHORT)\n"
                f"⚡ <i>Изменения записаны в CFG/app.json и мгновенно применены в рантайме бота.</i>\n\n"
                f"{summary}",
                reply_markup=get_coins_sizes_menu_keyboard(),
                parse_mode="HTML",
            )
            await state.set_state(TGStates.waiting_for_size_side_choice)
        except Exception as e:
            logger.error(f"Error applying default invest size update for {dome_id}: {e}")
            await message.answer(f"❌ Ошибка сохранения базового сайза: {e}", reply_markup=get_coins_sizes_menu_keyboard(), parse_mode="HTML")

    async def _apply_invest_size_update(self, dome_id: str, symbol: str, side: str, new_invest: float) -> None:
        """
        Физически записывает новый invest_size в CFG/app.json,
        горячо перезагружает StrategyRegistry, пересобирает рантайм через StrategyApplicator,
        и актуализирует кэши и открытые FSM-позиции.
        """
        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path)
        if not app_data or "active_strategies" not in app_data:
            raise KeyError("active_strategies missing in app.json")

        strat_entry = app_data["active_strategies"][dome_id]
        sides = ("LONG", "SHORT") if side == "BOTH" else (side,)
        for s in sides:
            if s not in strat_entry:
                continue
            if not isinstance(strat_entry[s].get("invest_size"), dict):
                strat_entry[s]["invest_size"] = {}
            strat_entry[s]["invest_size"][symbol] = float(new_invest)

        Utils.write_json_file(app_json_path, app_data)
        logger.info(f"[{dome_id}] Updated invest_size for {symbol} ({side}) -> {new_invest} in app.json")

        strat_cfg = None
        if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
            self.bot_core.strategy_registry.reload(app_data["active_strategies"])
            strat_cfg = self.bot_core.strategy_registry.get_strategy(dome_id)
            if strat_cfg:
                StrategyApplicator().build_runtime_for_symbol(symbol, strat_cfg)

        caches = getattr(getattr(self.bot_core, "runtime_manager", None), "caches", None)
        if caches and symbol in caches:
            for s in sides:
                if s in caches[symbol]:
                    caches[symbol][s]["invest_size"] = float(new_invest)
                    caches[symbol][s]["eff_invest"] = float(new_invest)

        rt_cfgs = getattr(self.bot_core, "runtime_configs", None)
        if rt_cfgs and symbol in rt_cfgs:
            for s in sides:
                if s in rt_cfgs[symbol]:
                    rt_cfgs[symbol][s]["invest_size"] = float(new_invest)
                    rt_cfgs[symbol][s]["eff_invest"] = float(new_invest)

        p_states = getattr(getattr(self.bot_core, "position_monitor", None), "states", None)
        if p_states and symbol in p_states:
            for s in sides:
                if s in p_states[symbol]:
                    st = p_states[symbol][s]
                    st.eff_invest = float(new_invest)
                    logger.info(f"[{symbol}] {s} Hot-updated active state.eff_invest -> {new_invest}")

    async def _apply_default_invest_size_update(self, dome_id: str, new_default: float) -> None:
        """
        Физически обновляет default_invest_size для LONG и SHORT в CFG/app.json,
        перезагружает StrategyRegistry, пересобирает рантайм для монет купола без оверрайдов
        и актуализирует кэши.
        """
        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path)
        if not app_data or "active_strategies" not in app_data:
            raise KeyError("active_strategies missing in app.json")

        strat_entry = app_data["active_strategies"][dome_id]
        for s in ("LONG", "SHORT"):
            if s in strat_entry:
                strat_entry[s]["default_invest_size"] = float(new_default)

        Utils.write_json_file(app_json_path, app_data)
        logger.info(f"[{dome_id}] Updated default_invest_size -> {new_default} in app.json")

        strat_cfg = None
        if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
            self.bot_core.strategy_registry.reload(app_data["active_strategies"])
            strat_cfg = self.bot_core.strategy_registry.get_strategy(dome_id)

        symbols = self._get_dome_symbols(dome_id, app_data)
        caches = getattr(getattr(self.bot_core, "runtime_manager", None), "caches", None)
        p_states = getattr(getattr(self.bot_core, "position_monitor", None), "states", None)

        for sym in symbols:
            if strat_cfg:
                StrategyApplicator().build_runtime_for_symbol(sym, strat_cfg)

            for s in ("LONG", "SHORT"):
                overrides = strat_entry.get(s, {}).get("invest_size", {})
                if sym not in overrides:
                    if caches and sym in caches and s in caches[sym]:
                        caches[sym][s]["invest_size"] = float(new_default)
                        caches[sym][s]["eff_invest"] = float(new_default)
                    if p_states and sym in p_states and s in p_states[sym]:
                        p_states[sym][s].eff_invest = float(new_default)


def get_coins_size_router(bot_core: Any) -> Router:
    """Фабрика создания роутера управления размерами инвестиций купола."""
    handler = CoinsSizeRouterHandler(bot_core=bot_core)
    return handler.router
