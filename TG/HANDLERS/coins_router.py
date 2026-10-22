# ==============================================================================
# Path: TG/HANDLERS/coins_router.py
# Role: Роутер управления монетами куполов (добавление, удаление, каталоги)
# ==============================================================================

import re
from typing import Any, List, Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from consts import DATA_DIR
from c_log import UnifiedLogger
from TG.keyboards import get_back_keyboard, get_main_keyboard, get_set_coins_keyboard
from TG.states import TGStates
from TG.strategy_helpers import extract_strategy_symbols, get_strategy_meta
from c_utils import Utils

logger = UnifiedLogger("TGCoinsRouter")


class CoinsRouterHandler:
    """Класс-обработчик управления составом торговых монет куполов стратегий."""

    def __init__(self, bot_core: Any, template_manager: Optional[Any] = None):
        self.bot_core = bot_core
        self.template_manager = template_manager
        self.router = Router(name="coins_router")
        self._register_routes()

    def _register_routes(self) -> None:
        r = self.router
        r.message(F.text == "⚙️ Set Coins")(self.on_set_coins)
        r.callback_query(F.data == "set_coins_cancel")(self.cb_set_coins_cancel)
        r.callback_query(F.data.startswith("set_coins_dome:"))(self.process_set_coins_dome_select)
        r.message(F.text.in_({"➕ Добавить монету", "➕ Add"}))(self.on_add_btn)
        r.message(TGStates.waiting_for_add_symbol)(self.process_add_symbol)
        r.callback_query(F.data.startswith("add_pick_strat_"))(self.process_add_pick_strat)
        r.callback_query(F.data.startswith("add_run_"))(self.process_add_run)
        r.callback_query(F.data.startswith("add_back_strats_"))(self.process_add_back_strats)
        r.callback_query(F.data == "add_cancel")(self.process_add_cancel)
        r.callback_query(F.data.startswith("add_cust_"))(self.process_add_cust)
        r.message(TGStates.waiting_for_add_custom_invest)(self.process_add_custom_invest_input)
        r.message(TGStates.waiting_for_add_custom_lev)(self.process_add_custom_lev_input)
        r.callback_query(F.data.startswith("add_catalog_"))(self.process_add_catalog)
        r.message(F.text.in_({"🗑️ Удалить монету", "🗑️ Del"}))(self.on_del_btn)
        r.callback_query(F.data.startswith("del_dome_coin:"))(self.process_del_dome_coin)
        r.callback_query(F.data == "del_dome_coin_cancel")(self.process_del_dome_coin_cancel)
        r.message(F.text.in_({"🔙 Назад", "🔙 Back"}))(self.on_back_from_coins)

    async def _ask_strategy_for_symbol(self, msg_or_cb: Any, state: FSMContext, symbol: str, edit_msg: bool = False):
        app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
        active_strategies = app_data.get("active_strategies", {})

        buttons = []
        for sid, sdata in active_strategies.items():
            if not sdata.get("enabled", False):
                continue
            meta = get_strategy_meta(sid, self.bot_core)
            s_name = meta.get("name", sid)
            buttons.append([InlineKeyboardButton(text=f"🎯 {s_name[:38]}", callback_data=f"add_pick_strat_{symbol}_{sid}")])

        buttons.append([InlineKeyboardButton(text="🌐 Выбрать другой универсум...", callback_data=f"add_catalog_{symbol}")])
        buttons.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="add_cancel")])

        text = (
            f"🪙 Добавление монеты: <b>{symbol}</b>\n\n"
            f"🎯 <b>Выберите стратегию, под которую назначить монету:</b>\n"
            f"<i>Каждая монета торгуется по правилам выбранной стратегии (сетка, тейк-профиты, плечо, Super Grid).</i>"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        if edit_msg and isinstance(msg_or_cb, CallbackQuery):
            await msg_or_cb.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        elif isinstance(msg_or_cb, CallbackQuery):
            await msg_or_cb.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        else:
            await msg_or_cb.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.set_state(TGStates.waiting_for_add_strategy_choice)

    async def on_set_coins(self, message: Message, state: FSMContext):
        await state.clear()
        active_strats = {}
        if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
            active_strats = self.bot_core.strategy_registry.get_active_strategies()
        else:
            app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
            active_strats = app_data.get("active_strategies", {})

        if not active_strats:
            await message.answer("⚠️ В системе нет активных куполов стратегий.", reply_markup=get_main_keyboard())
            return

        buttons = []
        for sid, scfg in active_strats.items():
            if sid.startswith("_") or not isinstance(scfg, (dict, object)):
                continue
            meta = get_strategy_meta(sid, self.bot_core)
            s_name = meta.get("name", sid)
            clean_title = s_name.encode("ascii", "ignore").decode("ascii").strip() or sid
            symbols = extract_strategy_symbols(scfg)
            cnt = len(symbols)
            btn_text = f"🪐 {clean_title[:28]} ({cnt} мон.)"
            buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"set_coins_dome:{sid}")])

        buttons.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="set_coins_cancel")])
        text = (
            "⚙️ <b>Управление монетами куполов</b>\n\n"
            "<b>Шаг 1: Выберите купол стратегии</b>, в котором хотите настроить список монет:"
        )
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

    async def cb_set_coins_cancel(self, callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        await state.clear()
        try:
            await callback.message.delete()
        except Exception:
            pass
        status = "⏸️ Paused" if getattr(self.bot_core, "is_paused", False) else "▶️ Running"
        await callback.message.answer(f"<b>Control Panel</b>\nСтатус бота: {status}", reply_markup=get_main_keyboard(), parse_mode="HTML")

    async def process_set_coins_dome_select(self, callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        sid = callback.data.split("set_coins_dome:")[1]
        await state.update_data(selected_dome=sid)

        meta = get_strategy_meta(sid, self.bot_core)
        s_name = meta.get("name", sid)

        symbols: List[str] = []
        if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
            active_strats = self.bot_core.strategy_registry.get_active_strategies()
            if sid in active_strats:
                symbols = list(active_strats[sid].symbols)
        if not symbols:
            app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
            symbols = extract_strategy_symbols(app_data.get("active_strategies", {}).get(sid, {}))

        symbols_str = ", ".join(sorted(symbols)) if symbols else "Нет монет"
        try:
            await callback.message.delete()
        except Exception:
            pass

        text = (
            f"🌌 <b>Купол стратегии:</b> <code>{sid}</code>\n"
            f"🏷️ <b>Название:</b> {s_name}\n"
            f"🪙 <b>Текущие монеты:</b> {symbols_str}\n\n"
            f"<i>Выберите действие в меню:</i>\n"
            f"➕ <b>Добавить монету</b> — ввод тикера (старт на 1-м сигнале)\n"
            f"🗑️ <b>Удалить монету</b> — удаление тикера из купола\n"
            f"🔙 <b>Назад</b> — возврат в главное меню"
        )
        await callback.message.answer(text, reply_markup=get_set_coins_keyboard(), parse_mode="HTML")

    async def on_add_btn(self, message: Message, state: FSMContext):
        data = await state.get_data()
        selected_dome = data.get("selected_dome")
        if not selected_dome:
            return await self.on_set_coins(message, state)

        meta = get_strategy_meta(selected_dome, self.bot_core)
        s_name = meta.get("name", selected_dome)

        # 1. Получаем список монет текущего купола
        dome_symbols: List[str] = []
        if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
            active_strats = self.bot_core.strategy_registry.get_active_strategies()
            if selected_dome in active_strats:
                dome_symbols = list(active_strats[selected_dome].symbols)
        if not dome_symbols:
            app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
            dome_symbols = extract_strategy_symbols(app_data.get("active_strategies", {}).get(selected_dome, {}))
        dome_symbols = sorted(list(set(dome_symbols)))

        # 2. Получаем список всех активных монет в системе
        system_symbols: List[str] = []
        if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
            system_symbols = self.bot_core.strategy_registry.get_all_active_symbols()
        if not system_symbols:
            system_symbols = getattr(self.bot_core, "symbols", []) or []
        if not system_symbols:
            app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
            for sid, sdata in app_data.get("active_strategies", {}).items():
                if sdata.get("enabled", True):
                    system_symbols.extend(extract_strategy_symbols(sdata))
        system_symbols = sorted(list(set(system_symbols)))

        dome_coins_str = ", ".join(f"<code>{s}</code>" for s in dome_symbols) if dome_symbols else "<i>нет</i>"
        all_coins_str = ", ".join(f"<code>{s}</code>" for s in system_symbols) if system_symbols else "<i>нет</i>"

        if set(system_symbols) == set(dome_symbols):
            coins_info = (
                f"🪙 <b>Активные монеты в системе ({len(system_symbols)}):</b>\n"
                f"{all_coins_str}\n"
            )
        else:
            coins_info = (
                f"🪙 <b>Монеты купола ({len(dome_symbols)}):</b>\n"
                f"{dome_coins_str}\n\n"
                f"🌐 <b>Все активные монеты в системе ({len(system_symbols)}):</b>\n"
                f"{all_coins_str}\n"
            )

        await state.set_state(TGStates.waiting_for_add_symbol)
        await message.answer(
            f"➕ <b>Добавление монеты в купол</b> <code>{selected_dome}</code>\n"
            f"🏷️ <b>Стратегия:</b> {s_name}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{coins_info}"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"✍️ <b>Введите тикер монеты</b> (например: <code>DOGEUSDT</code>):\n"
            f"<i>Монета автоматически вступит в торговлю при получении первого сигнала.</i>",
            reply_markup=get_set_coins_keyboard(),
            parse_mode="HTML",
        )

    async def process_add_symbol(self, message: Message, state: FSMContext):
        if message.text in {"🔙 Назад", "🔙 Back"}:
            await state.clear()
            status = "⏸️ Paused" if getattr(self.bot_core, "is_paused", False) else "▶️ Running"
            await message.answer(f"<b>Control Panel</b>\nСтатус бота: {status}", reply_markup=get_main_keyboard(), parse_mode="HTML")
            return

        raw_text = (message.text or "").strip()
        symbol = raw_text.upper()

        if not re.match(r"^[A-Z0-9]+USDT$", symbol) or len(symbol) < 5:
            await message.answer(
                "❌ <b>Некорректный тикер!</b>\n"
                "Тикер должен содержать только латинские буквы/цифры и оканчиваться на USDT (например: <code>DOGEUSDT</code>).\n"
                "Попробуйте еще раз или нажмите <b>🔙 Назад</b>:",
                parse_mode="HTML",
            )
            return

        data = await state.get_data()
        selected_dome = data.get("selected_dome")
        if not selected_dome:
            await message.answer("⚠️ Купол стратегии не выбран.", reply_markup=get_main_keyboard())
            await state.clear()
            return

        # Проверка на наличие монеты в системе
        system_symbols = []
        if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
            system_symbols = self.bot_core.strategy_registry.get_all_active_symbols()
        if not system_symbols:
            system_symbols = getattr(self.bot_core, "symbols", []) or []

        if symbol in system_symbols:
            coins_str = ", ".join(f"<code>{s}</code>" for s in sorted(system_symbols))
            await message.answer(
                f"⚠️ Монета <b>{symbol}</b> уже активна в системе!\n\n"
                f"🪙 <b>Текущие активные монеты ({len(system_symbols)}):</b>\n{coins_str}",
                reply_markup=get_set_coins_keyboard(),
                parse_mode="HTML",
            )
            return

        try:
            await self.bot_core.add_symbol(symbol, strategy_id=selected_dome)
            symbols = []
            if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
                active_strats = self.bot_core.strategy_registry.get_active_strategies()
                if selected_dome in active_strats:
                    symbols = list(active_strats[selected_dome].symbols)
            if not symbols:
                app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
                symbols = extract_strategy_symbols(app_data.get("active_strategies", {}).get(selected_dome, {}))
            symbols_str = ", ".join(sorted(symbols)) if symbols else symbol

            await message.answer(
                f"✅ <b>Монета {symbol} успешно добавлена в купол <code>{selected_dome}</code>!</b>\n\n"
                f"⚡️ Рантайм инициализирован. Монета вступит в работу на 1-м сигнале.\n"
                f"🪙 <b>Текущие монеты купола:</b> {symbols_str}",
                reply_markup=get_set_coins_keyboard(),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Error adding symbol {symbol} to {selected_dome}: {e}")
            await message.answer(f"❌ Ошибка добавления монеты: {e}", reply_markup=get_set_coins_keyboard())

        await state.set_state(None)
        await state.update_data(selected_dome=selected_dome)

    async def process_add_pick_strat(self, callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        parts = callback.data.split("_")
        if len(parts) < 5:
            return
        symbol = parts[3]
        strat_id = "_".join(parts[4:])

        await state.update_data(pending_symbol=symbol, pending_strat=strat_id)
        meta = get_strategy_meta(strat_id, self.bot_core)
        sg_str = "✅ Включен" if meta.get("super_grid") else "❌ Выключен"
        def_inv = meta.get("default_invest_size") or meta.get("invest_size") or 0.0

        text = (
            f"⚙️ <b>Параметры запуска {symbol}:</b>\n"
            f"📌 Стратегия: <b>{meta.get('name', strat_id)}</b>\n"
            f"ID: <code>{strat_id}</code>\n"
            f"📝 <i>{meta.get('description', '')[:120]}...</i>\n\n"
            f"• <b>Размер ордера (Invest):</b> <b>{def_inv} USDT</b> (дефолтный из конфига)\n"
            f"• <b>Кредитное плечо (Leverage):</b> <b>{meta.get('leverage', 20)}x</b>\n"
            f"• <b>Режим Take Profit:</b> <b>{meta.get('tp_summary')}</b>\n"
            f"• <b>Super Grid (волатильность):</b> <b>{sg_str}</b>\n\n"
            f"Запустить сразу с параметрами стратегии или настроить параметры кастомно?"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🚀 Запустить с параметрами стратегии", callback_data=f"add_run_{symbol}_{strat_id}")],
                [InlineKeyboardButton(text="⚙️ Настроить параметры (Invest/Lev)", callback_data=f"add_cust_{symbol}_{strat_id}")],
                [InlineKeyboardButton(text="🔙 Назад к стратегиям", callback_data=f"add_back_strats_{symbol}")],
            ]
        )
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

    async def process_add_run(self, callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        parts = callback.data.split("_")
        if len(parts) < 4:
            return
        symbol = parts[2]
        strat_id = "_".join(parts[3:])

        await self.bot_core.add_symbol(symbol, strategy_id=strat_id)
        meta = get_strategy_meta(strat_id, self.bot_core)
        def_inv = meta.get("default_invest_size") or meta.get("invest_size") or 0.0

        await callback.message.edit_text(
            f"✅ <b>Монета {symbol} успешно добавлена и запущена!</b>\n\n"
            f"🎯 Стратегия: <b>{meta.get('name', strat_id)}</b> (<code>{strat_id}</code>)\n"
            f"💰 Депозит: <b>{def_inv} USDT</b> (дефолтный из конфига, Плечо: {meta.get('leverage', 20)}x)\n"
            f"🎯 Тейк-профит: <b>{meta.get('tp_summary')}</b>\n"
            f"📁 Конфиг: <code>CFG/runtime/{strat_id}/{symbol.lower()}.json</code>",
            parse_mode="HTML",
        )
        await callback.message.answer("Монета активна в торговом пуле.", reply_markup=get_set_coins_keyboard())
        await state.clear()

    async def process_add_back_strats(self, callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        symbol = callback.data.replace("add_back_strats_", "")
        await self._ask_strategy_for_symbol(callback, state, symbol, edit_msg=True)

    async def process_add_cancel(self, callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        try:
            await callback.message.delete()
        except Exception:
            pass
        await state.clear()

    async def process_add_cust(self, callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        parts = callback.data.split("_")
        symbol = parts[2]
        strat_id = "_".join(parts[3:])
        await state.update_data(pending_symbol=symbol, pending_strat=strat_id)
        meta = get_strategy_meta(strat_id, self.bot_core)
        def_inv = meta.get("default_invest_size") or meta.get("invest_size") or 0.0

        await callback.message.answer(
            f"⚙️ <b>Кастомная настройка {symbol} под {strat_id}</b>\n\n"
            f"Текущий дефолтный Invest Size (из конфига): <b>{def_inv} USDT</b>.\n"
            f"Введите желаемый размер инвестиции (USDT) для каждого направления (или отправьте 0 чтобы оставить {def_inv}):",
            reply_markup=get_back_keyboard(),
            parse_mode="HTML",
        )
        await state.set_state(TGStates.waiting_for_add_custom_invest)

    async def process_add_custom_invest_input(self, message: Message, state: FSMContext):
        if message.text == "🔙 Back":
            await state.clear()
            return await self.on_set_coins(message, state)
        try:
            val = float(message.text.strip().replace(",", "."))
            data = await state.get_data()
            strat_id = data.get("pending_strat")
            meta = get_strategy_meta(strat_id, self.bot_core)
            def_inv = meta.get("default_invest_size") or meta.get("invest_size") or 0.0
            invest = val if val > 0 else def_inv
            await state.update_data(custom_invest=invest)

            await message.answer(
                f"✅ Invest Size: <b>{invest} USDT</b>.\n\n"
                f"Текущее плечо стратегии: <b>{meta.get('leverage', 20)}x</b>.\n"
                f"Введите кредитное плечо (Leverage от 1 до 50) или отправьте 0 чтобы оставить {meta.get('leverage', 20)}x:",
                reply_markup=get_back_keyboard(),
                parse_mode="HTML",
            )
            await state.set_state(TGStates.waiting_for_add_custom_lev)
        except ValueError:
            await message.answer("❌ Введите корректное число (например, <code>300</code>):", parse_mode="HTML")

    async def process_add_custom_lev_input(self, message: Message, state: FSMContext):
        if message.text == "🔙 Back":
            await state.clear()
            return await self.on_set_coins(message, state)
        try:
            val = int(message.text.strip())
            data = await state.get_data()
            symbol = data.get("pending_symbol")
            strat_id = data.get("pending_strat")
            meta = get_strategy_meta(strat_id, self.bot_core)
            lev = val if 1 <= val <= 50 else meta.get("leverage", 20)
            def_inv = meta.get("default_invest_size") or meta.get("invest_size") or 0.0
            invest = data.get("custom_invest", def_inv)

            await self.bot_core.add_symbol(symbol, strategy_id=strat_id)

            app_json_path = DATA_DIR / "app.json"
            app_data = Utils.read_json_file(app_json_path) or {}
            strat_entry = app_data.get("active_strategies", {}).get(strat_id, {})
            for side in ("LONG", "SHORT"):
                if isinstance(strat_entry.get(side), dict):
                    if not isinstance(strat_entry[side].get("invest_size"), dict):
                        strat_entry[side]["invest_size"] = {}
                    strat_entry[side]["invest_size"][symbol] = float(invest)
            Utils.write_json_file(app_json_path, app_data)

            if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
                self.bot_core.strategy_registry.reload(app_data.get("active_strategies", {}))
                strat_cfg = self.bot_core.strategy_registry.get_strategy(strat_id)
                if strat_cfg:
                    from CORE.strategy_applicator import StrategyApplicator

                    StrategyApplicator().build_runtime_for_symbol(symbol, strat_cfg)

            if self.template_manager and hasattr(self.template_manager, "get_runtime_path"):
                rt_path = self.template_manager.get_runtime_path(symbol, strat_id)
                if rt_path.exists():
                    rt_data = Utils.read_json_file(rt_path) or {}
                    for side in ("LONG", "SHORT"):
                        if side in rt_data:
                            rt_data[side]["invest_size"] = invest
                            rt_data[side]["leverage"] = lev
                    Utils.write_json_file(rt_path, rt_data)
                    if hasattr(self.bot_core, "runtime_configs") and symbol in self.bot_core.runtime_configs:
                        for side in ("LONG", "SHORT"):
                            if side in self.bot_core.runtime_configs[symbol]:
                                self.bot_core.runtime_configs[symbol][side]["invest_size"] = invest
                                self.bot_core.runtime_configs[symbol][side]["leverage"] = lev

            await message.answer(
                f"✅ <b>Монета {symbol} успешно настроена и запущена!</b>\n\n"
                f"🎯 Стратегия: <b>{meta.get('name', strat_id)}</b> (<code>{strat_id}</code>)\n"
                f"💰 Кастомный Invest: <b>{invest} USDT</b>\n"
                f"⚡ Кастомное Плечо: <b>{lev}x</b>\n"
                f"🎯 Тейк-профит: <b>{meta.get('tp_summary')}</b>\n"
                f"📁 Конфиг: <code>CFG/runtime/{strat_id}/{symbol.lower()}.json</code>",
                reply_markup=get_set_coins_keyboard(),
                parse_mode="HTML",
            )
            await state.clear()
        except ValueError:
            await message.answer("❌ Введите корректное целое число (от 1 до 50):", parse_mode="HTML")

    async def process_add_catalog(self, callback: CallbackQuery):
        await callback.answer()
        symbol = callback.data.replace("add_catalog_", "")
        if hasattr(self.bot_core, "strategy_registry") and getattr(self.bot_core.strategy_registry, "universes_db", None):
            univ_data = self.bot_core.strategy_registry.universes_db
        else:
            univ_dir = DATA_DIR / "universes"
            if univ_dir.exists():
                univ_data = {jf.stem: Utils.read_json_file(jf) for jf in sorted(univ_dir.glob("*.json"))}
            else:
                univ_data = Utils.read_json_file(DATA_DIR / "universes.json") or {}

        buttons = []
        for uid, udata in list(univ_data.items())[:12]:
            u_name = udata.get("name", uid)
            buttons.append([InlineKeyboardButton(text=f"🌐 {u_name[:35]}", callback_data=f"add_pick_strat_{symbol}_{uid}")])

        buttons.append([InlineKeyboardButton(text="🔙 Назад к активным", callback_data=f"add_back_strats_{symbol}")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.edit_text(
            f"🌐 <b>Каталог стратегий (Universes) для {symbol}</b>\n\n"
            f"Выберите любую стратегию из базы для активации и запуска:",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    async def on_del_btn(self, message: Message, state: FSMContext):
        data = await state.get_data()
        selected_dome = data.get("selected_dome")
        if not selected_dome:
            return await self.on_set_coins(message, state)

        symbols: List[str] = []
        if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
            active_strats = self.bot_core.strategy_registry.get_active_strategies()
            if selected_dome in active_strats:
                symbols = list(active_strats[selected_dome].symbols)
        if not symbols:
            app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
            symbols = extract_strategy_symbols(app_data.get("active_strategies", {}).get(selected_dome, {}))

        if not symbols:
            await message.answer(
                f"⚠️ В куполе <code>{selected_dome}</code> сейчас нет монет для удаления.",
                reply_markup=get_set_coins_keyboard(),
                parse_mode="HTML",
            )
            return

        buttons = []
        row = []
        for sym in sorted(symbols):
            row.append(InlineKeyboardButton(text=f"❌ {sym}", callback_data=f"del_dome_coin:{selected_dome}:{sym}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="del_dome_coin_cancel")])

        await message.answer(
            f"🗑️ <b>Удаление монеты из купола</b> <code>{selected_dome}</code>\n\n"
            f"<i>Нажмите на монету для удаления:</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML",
        )

    async def process_del_dome_coin(self, callback: CallbackQuery):
        await callback.answer()
        parts = callback.data.split(":")
        dome_id = parts[1]
        sym = parts[2]
        try:
            res = await self.bot_core.delete_symbol(sym)
            if res:
                await callback.message.edit_text(
                    f"✅ Монета <b>{sym}</b> успешно удалена из купола <code>{dome_id}</code>.",
                    parse_mode="HTML",
                )
            else:
                await callback.message.edit_text(
                    f"⚠️ Монета <b>{sym}</b> уже отсутствует в куполе <code>{dome_id}</code>.",
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.error(f"Error deleting symbol {sym}: {e}")
            await callback.message.edit_text(f"❌ Ошибка удаления монеты {sym}: {e}")

    async def process_del_dome_coin_cancel(self, callback: CallbackQuery):
        await callback.answer()
        try:
            await callback.message.delete()
        except Exception:
            pass

    async def on_back_from_coins(self, message: Message, state: FSMContext):
        await state.clear()
        status = "⏸️ Paused" if getattr(self.bot_core, "is_paused", False) else "▶️ Running"
        text = f"<b>Control Panel</b>\nСтатус бота: {status}"
        await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="HTML")


def get_coins_router(bot_core: Any, template_manager: Optional[Any] = None) -> Router:
    """Фабрика роутера управления монетами куполов стратегий."""
    handler = CoinsRouterHandler(bot_core=bot_core, template_manager=template_manager)
    return handler.router
