# ==============================================================================
# Path: TG/HANDLERS/grid_router.py
# Role: Роутер настройки параметров сетки, уровней усреднения, тейк-профитов и Super Grid
# ==============================================================================

import asyncio
import io
import json
import os
from typing import Any, Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from consts import DATA_DIR
from c_log import UnifiedLogger
from TG.keyboards import get_back_keyboard, get_main_keyboard, get_set_coins_keyboard
from TG.states import TGStates
from TG.strategy_helpers import extract_strategy_symbols, get_strategy_meta
from c_utils import Utils

logger = UnifiedLogger("TGGridRouter")


def get_grid_router(bot_core: Any, template_manager: Optional[Any] = None) -> Router:
    """Фабрика роутера параметров сеток, TP, усреднения и Super Grid."""
    router = Router(name="grid_router")

    async def _render_edit_coins_list(message_or_cb: Any, edit: bool = False):
        symbols = getattr(bot_core, "symbols", [])
        if not symbols:
            text = "❌ <b>Нет активных монет в торговле!</b>"
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Close", callback_data="edit_side_cancel")]])
            if edit:
                await message_or_cb.edit_text(text, reply_markup=kb, parse_mode="HTML")
            else:
                await message_or_cb.answer(text, reply_markup=kb, parse_mode="HTML")
            return

        strat_registry = getattr(bot_core, "strategy_registry", None)
        coins_by_strat = {}
        for s in symbols:
            sid = strat_registry.get_strategy_id_for_symbol(s) if strat_registry else None
            sid = sid or "unassigned"
            coins_by_strat.setdefault(sid, []).append(s)

        text_lines = ["⚙️ <b>Активные монеты по стратегиям:</b>\n"]
        buttons = []
        row = []

        for sid, c_list in coins_by_strat.items():
            meta = get_strategy_meta(sid, bot_core)
            s_title = meta.get("name", sid)
            short_sid = sid.replace("u0_base_classic_", "").replace("u9_omni_", "")
            text_lines.append(f"🎯 <b>{s_title}</b> (<code>{sid}</code>):")
            for c in sorted(c_list):
                text_lines.append(f"  • <code>{c}</code>")
                btn_text = f"🪙 {c} [{short_sid[:7]}]"
                row.append(InlineKeyboardButton(text=btn_text, callback_data=f"edit_coin_{c}"))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            text_lines.append("")

        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton(text="🔙 Close", callback_data="edit_side_cancel")])
        text_lines.append("<i>Выберите монету для настройки параметров или смены стратегии:</i>")
        text = "\n".join(text_lines)

        if edit:
            await message_or_cb.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
        else:
            await message_or_cb.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

    async def _render_side_view(callback: CallbackQuery, symbol: str, side: str):
        runtime_path = template_manager.get_runtime_path(symbol) if template_manager else None
        if not runtime_path or not runtime_path.exists():
            await callback.message.answer(f"❌ Конфиг {symbol} не найден.")
            return

        try:
            rt_data = json.loads(runtime_path.read_text(encoding="utf-8"))
            side_data = rt_data.get(side, {})
            en = side_data.get("enable", False)
            sz = side_data.get("invest_size", 0)
            lev = side_data.get("leverage", 0)

            grid_cfg = side_data.get("grid", {})
            grid_lines = ["📊 <b>Grid (Avg):</b>"]
            for k in sorted(grid_cfg.keys(), key=lambda x: int(x) if x.isdigit() else x):
                lvl_data = grid_cfg[k]
                if not isinstance(lvl_data, dict):
                    continue
                en_lvl = lvl_data.get("enabled", True)
                vol = lvl_data.get("volume")
                ind = lvl_data.get("indent")
                si = lvl_data.get("super_indent")
                if en_lvl is False or (ind is None and vol is None):
                    grid_lines.append(f"  • Lvl {k}: [Отключен/Резерв]")
                else:
                    vol_str = f"{vol}" if vol is not None else "-"
                    ind_str = f"{ind}" if ind is not None else "-"
                    si_text = f" (Super: {si})" if si is not None else ""
                    grid_lines.append(f"  • Lvl {k}: Vol <b>{vol_str}</b> | Indent <b>{ind_str}</b>{si_text}")

            tp_cfg = side_data.get("tp_map", {})
            tp_lines = ["🎯 <b>Take Profits (TP):</b>"]
            for k in sorted(tp_cfg.keys(), key=lambda x: int(x) if x.isdigit() else x):
                lvl_data = tp_cfg[k]
                if not isinstance(lvl_data, dict):
                    continue
                ind = lvl_data.get("indent")
                fb = lvl_data.get("fallback_indent")
                ind_str = f"{ind}" if ind is not None else "-"
                fb_str = f"{fb}" if fb is not None else "-"
                tp_lines.append(f"  • Lvl {k}: Indent <b>{ind_str}</b> | Fallback <b>{fb_str}</b>")

            tp_purpose = side_data.get("tp_purpose", "self")
            msg_text = (
                f"⚙️ <b>{symbol} - {side}</b>\n"
                f"Status: {'✅ On' if en else '❌ Off'}\n\n"
                f"💰 Invest Size: <b>{sz}</b> USDT (Lev: {lev}x)\n"
                f"🔄 TP Purpose: <b>{tp_purpose}</b>\n\n"
                + "\n".join(grid_lines)
                + "\n\n"
                + "\n".join(tp_lines)
            )

            toggle_text = "⏸ Отключить (ON)" if en else "▶️ Включить (OFF)"
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=toggle_text, callback_data=f"edit_act_toggle_{symbol}_{side}")],
                    [InlineKeyboardButton(text="💰 Edit Invest Size", callback_data=f"edit_act_size_{symbol}_{side}")],
                    [InlineKeyboardButton(text="📊 Edit Set Avg", callback_data=f"edit_act_avg_{symbol}_{side}")],
                    [InlineKeyboardButton(text="🎯 Edit Set TP", callback_data=f"edit_act_tp_{symbol}_{side}")],
                    [InlineKeyboardButton(text="🔄 Edit TP Purpose", callback_data=f"edit_act_purp_{symbol}_{side}")],
                    [InlineKeyboardButton(text="🔙 Back", callback_data=f"edit_coin_{symbol}")],
                ]
            )

            try:
                await callback.message.edit_text(msg_text, reply_markup=keyboard, parse_mode="HTML")
            except Exception as e:
                if "message is not modified" not in str(e).lower():
                    await callback.message.answer(msg_text, reply_markup=keyboard, parse_mode="HTML")
        except Exception as e:
            await callback.message.answer(f"Ошибка чтения: {e}")

    async def _apply_instant_update(symbol: str, side: str, callback: CallbackQuery):
        if hasattr(bot_core, "fsm_states") and hasattr(bot_core, "runtime_manager"):
            await bot_core.runtime_manager.sync_with_fsm(bot_core.fsm_states, force_save=True)
        await _render_side_view(callback, symbol, side)

    @router.message(F.text == "✏️ Edit")
    async def on_edit_btn(message: Message, state: FSMContext):
        await state.clear()
        await _render_edit_coins_list(message, edit=False)

    @router.callback_query(F.data == "edit_coin_list")
    async def process_edit_coin_list(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        await _render_edit_coins_list(callback.message, edit=True)

    @router.callback_query(F.data.startswith("edit_coin_") & ~F.data.startswith("edit_coin_list"))
    async def process_edit_coin_select(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        symbol = callback.data.replace("edit_coin_", "")
        strat_registry = getattr(bot_core, "strategy_registry", None)
        strat_id = strat_registry.get_strategy_id_for_symbol(symbol) if strat_registry else None
        meta = get_strategy_meta(strat_id, bot_core) if strat_id else {"name": "Не назначена"}
        strat_name = meta.get("name", strat_id or "—")

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🟢 LONG", callback_data=f"edit_side_{symbol}_LONG"),
                    InlineKeyboardButton(text="🔴 SHORT", callback_data=f"edit_side_{symbol}_SHORT"),
                ],
                [InlineKeyboardButton(text="🔄 Сменить стратегию", callback_data=f"reassign_strat_{symbol}")],
                [InlineKeyboardButton(text="🔙 Back", callback_data="edit_coin_list")],
            ]
        )
        text = f"⚙️ <b>{symbol}</b>\n📌 Стратегия: <b>{strat_name}</b>\nID: <code>{strat_id}</code>\n\nВыберите действие:"
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

    @router.callback_query(F.data.startswith("reassign_strat_"))
    async def process_reassign_strat(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        symbol = callback.data.replace("reassign_strat_", "")
        app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
        active_strategies = app_data.get("active_strategies", {})
        strat_registry = getattr(bot_core, "strategy_registry", None)
        current_strat = strat_registry.get_strategy_id_for_symbol(symbol) if strat_registry else None

        buttons = []
        for sid, sdata in active_strategies.items():
            if sid == current_strat or not sdata.get("enabled", False):
                continue
            meta = get_strategy_meta(sid, bot_core)
            s_name = meta.get("name", sid)
            buttons.append([InlineKeyboardButton(text=f"🎯 {s_name[:35]}", callback_data=f"do_reassign_{symbol}_{sid}")])

        buttons.append([InlineKeyboardButton(text="🔙 Назад к монете", callback_data=f"edit_coin_{symbol}")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.edit_text(
            f"🔄 <b>Перевод {symbol} в другую стратегию</b>\n\nТекущая: <code>{current_strat}</code>\nВыберите новую стратегию:",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    @router.callback_query(F.data.startswith("do_reassign_"))
    async def process_do_reassign(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        parts = callback.data.split("_")
        if len(parts) < 4:
            return
        symbol = parts[2]
        new_strat_id = "_".join(parts[3:])

        app_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_path) or {}
        active_strategies = app_data.get("active_strategies", {})

        for sid, sdata in active_strategies.items():
            if "symbols" in sdata and symbol in sdata["symbols"]:
                sdata["symbols"].remove(symbol)

        if new_strat_id in active_strategies:
            active_strategies[new_strat_id].setdefault("symbols", []).append(symbol)
            active_strategies[new_strat_id]["enabled"] = True
            Utils.write_json_file(app_path, app_data)

        if hasattr(bot_core, "strategy_registry") and bot_core.strategy_registry:
            bot_core.strategy_registry.reload(active_strategies)

        try:
            from CORE.strategy_applicator import StrategyApplicator

            strat_cfg = bot_core.strategy_registry.get_strategy_for_symbol(symbol)
            StrategyApplicator().build_runtime_for_symbol(symbol, strat_cfg)
        except Exception as e:
            logger.warning(f"StrategyApplicator rebuild error: {e}")

        if hasattr(bot_core, "runtime_manager"):
            bot_core.runtime_manager.load_initial_caches(bot_core.symbols)
            bot_core.runtime_configs = bot_core.runtime_manager.caches
            bot_core.runtime_manager.populate_fsm_from_cache(bot_core.fsm_states)

        meta = get_strategy_meta(new_strat_id, bot_core)
        await callback.message.edit_text(
            f"✅ Монета <b>{symbol}</b> переведена в <b>{meta.get('name', new_strat_id)}</b>!\nНовые настройки применены.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⚙️ К монете", callback_data=f"edit_coin_{symbol}")],
                    [InlineKeyboardButton(text="📋 К списку монет", callback_data="edit_coin_list")],
                ]
            ),
            parse_mode="HTML",
        )

    @router.callback_query(F.data == "edit_side_cancel")
    async def process_edit_side_cancel(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        await callback.message.delete()

    @router.callback_query(F.data.startswith("edit_side_"))
    async def process_edit_side(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        data_parts = callback.data.split("_")
        if len(data_parts) != 4:
            return
        await _render_side_view(callback, data_parts[2], data_parts[3])

    @router.callback_query(F.data.startswith("edit_act_toggle_"))
    async def process_edit_toggle(callback: CallbackQuery, state: FSMContext):
        data_parts = callback.data.split("_")
        symbol, side = data_parts[3], data_parts[4]
        runtime_cfg = getattr(bot_core, "runtime_configs", {}).get(symbol)
        if runtime_cfg and side in runtime_cfg:
            current = runtime_cfg[side].get("enable", False)
            runtime_cfg[side]["enable"] = not current
            msg = f"{side} Включен! ✅" if not current else f"{side} Отключен! ❌"
            await callback.answer(msg, show_alert=False)
            await _apply_instant_update(symbol, side, callback)
        else:
            await callback.answer("Runtime config not found in RAM")

    @router.callback_query(F.data.startswith("edit_act_purp_"))
    async def process_edit_purp(callback: CallbackQuery, state: FSMContext):
        data_parts = callback.data.split("_")
        symbol, side = data_parts[3], data_parts[4]
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="1. self", callback_data=f"set_purp_{symbol}_{side}_self"),
                    InlineKeyboardButton(text="2. opposite", callback_data=f"set_purp_{symbol}_{side}_opposite"),
                    InlineKeyboardButton(text="3. both", callback_data=f"set_purp_{symbol}_{side}_both"),
                ],
                [InlineKeyboardButton(text="🔙 Back", callback_data=f"edit_side_{symbol}_{side}")],
            ]
        )
        await callback.message.edit_text(
            f"⚙️ <b>{symbol} {side}</b>\nВыберите настройку <b>TP Purpose</b>:\n\n1. self — по своему уровню\n2. opposite — противоположная сторона\n3. both — наибольшая просадка",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    @router.callback_query(F.data.startswith("set_purp_"))
    async def process_set_purp(callback: CallbackQuery, state: FSMContext):
        data_parts = callback.data.split("_")
        symbol, side, purpose = data_parts[2], data_parts[3], data_parts[4]
        runtime_cfg = getattr(bot_core, "runtime_configs", {}).get(symbol)
        if runtime_cfg and side in runtime_cfg:
            runtime_cfg[side]["tp_purpose"] = purpose
            fsm_state = getattr(bot_core, "fsm_states", {}).get(symbol, {}).get(side)
            if fsm_state:
                fsm_state.tp_purpose = purpose
            await callback.answer(f"TP Purpose: {purpose} ✅", show_alert=False)
            await _apply_instant_update(symbol, side, callback)
        else:
            await callback.answer("Runtime config not found in RAM")

    @router.callback_query(F.data.startswith("edit_act_size_"))
    async def process_edit_size(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        data_parts = callback.data.split("_")
        symbol, side = data_parts[3], data_parts[4]
        await state.update_data(edit_symbol=symbol, edit_side=side)
        await callback.message.answer(
            f"Введите новый <b>Invest Size</b> (USDT) для {symbol} {side} (например: 15.5):",
            parse_mode="HTML",
            reply_markup=get_back_keyboard(),
        )
        await state.set_state(TGStates.waiting_for_invest_size)

    @router.message(TGStates.waiting_for_invest_size)
    async def handle_invest_size(message: Message, state: FSMContext):
        if message.text in {"🔙 Back", "🔙 Назад"}:
            await state.clear()
            return await on_edit_btn(message, state)
        try:
            val = float(message.text.replace(",", "."))
            data = await state.get_data()
            symbol, side = data["edit_symbol"], data["edit_side"]
            runtime_cfg = getattr(bot_core, "runtime_configs", {}).get(symbol)
            if runtime_cfg and side in runtime_cfg:
                runtime_cfg[side]["invest_size"] = val
                if hasattr(bot_core, "fsm_states") and hasattr(bot_core, "runtime_manager"):
                    await bot_core.runtime_manager.sync_with_fsm(bot_core.fsm_states, force_save=True)
                await message.answer(f"✅ Invest Size обновлен до {val}.", reply_markup=get_set_coins_keyboard())
            await state.clear()
        except ValueError:
            await message.answer("❌ Некорректное число.")

    @router.callback_query(F.data.startswith("edit_act_avg_"))
    async def process_edit_avg(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        data_parts = callback.data.split("_")
        symbol, side = data_parts[3], data_parts[4]
        fsm_state = getattr(bot_core, "fsm_states", {}).get(symbol, {}).get(side)
        if not fsm_state:
            await callback.message.answer("❌ FSM state не найден.")
            return

        grid = fsm_state.grid
        levels = sorted([int(k) for k in grid.keys()]) if grid else [0]
        buttons, row = [], []
        for lvl in levels:
            is_act = grid.get(str(lvl), {}).get("is_active", False)
            row.append(InlineKeyboardButton(text=f"Уровень {lvl} {'(✅)' if is_act else ''}", callback_data=f"ed_lvl_avg_{symbol}_{side}_{lvl}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data=f"edit_side_{symbol}_{side}")])
        await callback.message.edit_text(
            f"⚙️ <b>{symbol} {side}</b>\nВыберите уровень для <b>Set Avg</b>:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML",
        )

    @router.callback_query(F.data.startswith("ed_lvl_avg_"))
    async def process_ed_lvl_avg(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        data_parts = callback.data.split("_")
        symbol, side, lvl = data_parts[3], data_parts[4], data_parts[5]
        fsm_state = getattr(bot_core, "fsm_states", {}).get(symbol, {}).get(side)
        grid_cfg = fsm_state.grid.get(str(lvl), {}) if fsm_state else {}
        vol_disp = grid_cfg.get("volume", "[None]")
        ind_disp = grid_cfg.get("indent", "[None]")
        await state.update_data(edit_symbol=symbol, edit_side=side, edit_lvl=lvl)
        msg = f"Усреднение (Set Avg) <b>{symbol} {side} | Уровень {lvl}</b>\n\n🔹 Объем: <b>{vol_disp}</b>\n🔹 Индент: <b>{ind_disp}</b>"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔹 Изменить Объем", callback_data=f"ed_prm_avgvol_{symbol}_{side}_{lvl}"),
                    InlineKeyboardButton(text="🔹 Изменить Индент", callback_data=f"ed_prm_avgind_{symbol}_{side}_{lvl}"),
                ],
                [InlineKeyboardButton(text="🔙 Back", callback_data=f"edit_act_avg_{symbol}_{side}")],
            ]
        )
        await callback.message.edit_text(msg, parse_mode="HTML", reply_markup=keyboard)

    @router.callback_query(F.data.startswith("ed_prm_avgvol_"))
    async def process_ed_prm_avgvol(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        parts = callback.data.split("_")
        await state.update_data(edit_symbol=parts[3], edit_side=parts[4], edit_lvl=parts[5], edit_param="avgvol")
        await callback.message.answer(f"Введите новый <b>Объем</b> для уровня {parts[5]}:", parse_mode="HTML", reply_markup=get_back_keyboard())
        await state.set_state(TGStates.waiting_for_avg_params)

    @router.callback_query(F.data.startswith("ed_prm_avgind_"))
    async def process_ed_prm_avgind(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        parts = callback.data.split("_")
        await state.update_data(edit_symbol=parts[3], edit_side=parts[4], edit_lvl=parts[5], edit_param="avgind")
        await callback.message.answer(f"Введите новый <b>Индент</b> для уровня {parts[5]}:", parse_mode="HTML", reply_markup=get_back_keyboard())
        await state.set_state(TGStates.waiting_for_avg_params)

    @router.message(TGStates.waiting_for_avg_params)
    async def handle_avg_params(message: Message, state: FSMContext):
        if message.text in {"🔙 Back", "🔙 Назад"}:
            await state.clear()
            return await on_edit_btn(message, state)
        try:
            val = abs(float(message.text.strip()))
            data = await state.get_data()
            symbol, side, lvl, param = data["edit_symbol"], data["edit_side"], data["edit_lvl"], data["edit_param"]
            fsm_state = getattr(bot_core, "fsm_states", {}).get(symbol, {}).get(side)
            runtime_cfg = getattr(bot_core, "runtime_configs", {}).get(symbol, {}).get(side)

            if fsm_state and runtime_cfg:
                fsm_state.grid.setdefault(lvl, {})
                runtime_cfg.setdefault("grid", {}).setdefault(lvl, {})
                old_ind = fsm_state.grid[lvl].get("indent")

                if param == "avgvol":
                    fsm_state.grid[lvl]["volume"] = val
                    runtime_cfg["grid"][lvl]["volume"] = val
                    await message.answer(f"✅ Объем уровня {lvl} изменен на {val}.", reply_markup=get_set_coins_keyboard())
                elif param == "avgind":
                    v = -val if int(lvl) > 0 else 0.0
                    fsm_state.grid[lvl]["indent"] = v
                    runtime_cfg["grid"][lvl]["indent"] = v
                    if old_ind != v:
                        if not fsm_state.grid[lvl].get("is_active"):
                            fsm_state.grid[lvl]["price"] = None
                            runtime_cfg["grid"][lvl]["price"] = None
                        fsm_state.next_avg_price = None
                    await message.answer(f"✅ Индент уровня {lvl} изменен на {v}.", reply_markup=get_set_coins_keyboard())

                if hasattr(bot_core, "fsm_states") and hasattr(bot_core, "runtime_manager"):
                    await bot_core.runtime_manager.sync_with_fsm(bot_core.fsm_states, force_save=True)
                if hasattr(bot_core, "volatility_manager") and getattr(bot_core.volatility_manager, "is_running", False):
                    asyncio.create_task(bot_core.volatility_manager.process_all())
            await state.clear()
        except ValueError:
            await message.answer("❌ Ошибка: Введите число (например 15.5).")

    @router.callback_query(F.data.startswith("edit_act_tp_"))
    async def process_edit_tp(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        parts = callback.data.split("_")
        symbol, side = parts[3], parts[4]
        fsm_state = getattr(bot_core, "fsm_states", {}).get(symbol, {}).get(side)
        if not fsm_state:
            await callback.message.answer("❌ FSM state не найден.")
            return

        tp_map = fsm_state.tp_map
        levels = sorted([int(k) for k in tp_map.keys()]) if tp_map else [0]
        buttons, row = [], []
        for lvl in levels:
            is_act = tp_map.get(str(lvl), {}).get("is_active", False)
            row.append(InlineKeyboardButton(text=f"Уровень {lvl} {'(✅)' if is_act else ''}", callback_data=f"ed_lvl_tp_{symbol}_{side}_{lvl}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data=f"edit_side_{symbol}_{side}")])
        await callback.message.edit_text(
            f"⚙️ <b>{symbol} {side}</b>\nВыберите уровень для <b>Set TP</b>:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML",
        )

    @router.callback_query(F.data.startswith("ed_lvl_tp_"))
    async def process_ed_lvl_tp(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        parts = callback.data.split("_")
        symbol, side, lvl = parts[3], parts[4], parts[5]
        runtime_cfg = getattr(bot_core, "runtime_configs", {}).get(symbol, {}).get(side, {})
        tp_cfg = runtime_cfg.get("tp_map", {}).get(str(lvl), {})
        ind_disp = tp_cfg.get("indent", "[None]")
        fb_disp = tp_cfg.get("fallback_indent", "[None]")
        await state.update_data(edit_symbol=symbol, edit_side=side, edit_lvl=lvl)
        msg = f"Установка тейк-профита <b>{symbol} {side} | Уровень {lvl}</b>\n\n🔹 Indent: <b>{ind_disp}</b>\n🔹 Fallback: <b>{fb_disp}</b>"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔹 Изменить Indent", callback_data=f"ed_prm_tpind_{symbol}_{side}_{lvl}"),
                    InlineKeyboardButton(text="🔹 Изменить Fallback", callback_data=f"ed_prm_tpfb_{symbol}_{side}_{lvl}"),
                ],
                [InlineKeyboardButton(text="🔙 Back", callback_data=f"edit_act_tp_{symbol}_{side}")],
            ]
        )
        await callback.message.edit_text(msg, parse_mode="HTML", reply_markup=keyboard)

    @router.callback_query(F.data.startswith("ed_prm_tpind_"))
    async def process_ed_prm_tpind(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        parts = callback.data.split("_")
        await state.update_data(edit_symbol=parts[3], edit_side=parts[4], edit_lvl=parts[5], edit_param="tpind")
        await callback.message.answer(f"Введите новый <b>Indent</b> для TP {parts[5]}:", parse_mode="HTML", reply_markup=get_back_keyboard())
        await state.set_state(TGStates.waiting_for_tp_params)

    @router.callback_query(F.data.startswith("ed_prm_tpfb_"))
    async def process_ed_prm_tpfb(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        parts = callback.data.split("_")
        await state.update_data(edit_symbol=parts[3], edit_side=parts[4], edit_lvl=parts[5], edit_param="tpfb")
        await callback.message.answer(f"Введите новый <b>Fallback Indent</b> для TP {parts[5]}:", parse_mode="HTML", reply_markup=get_back_keyboard())
        await state.set_state(TGStates.waiting_for_tp_params)

    @router.message(TGStates.waiting_for_tp_params)
    async def handle_tp_params(message: Message, state: FSMContext):
        if message.text in {"🔙 Back", "🔙 Назад"}:
            await state.clear()
            return await on_edit_btn(message, state)
        try:
            val = abs(float(message.text.strip()))
            data = await state.get_data()
            symbol, side, lvl, param = data["edit_symbol"], data["edit_side"], data["edit_lvl"], data["edit_param"]
            fsm_state = getattr(bot_core, "fsm_states", {}).get(symbol, {}).get(side)
            runtime_cfg = getattr(bot_core, "runtime_configs", {}).get(symbol, {}).get(side)

            if fsm_state and runtime_cfg:
                fsm_state.tp_map.setdefault(lvl, {})
                runtime_cfg.setdefault("tp_map", {}).setdefault(lvl, {})
                if param == "tpind":
                    fsm_state.tp_map[lvl]["indent"] = val
                    runtime_cfg["tp_map"][lvl]["indent"] = val
                    await message.answer(f"✅ Indent тейк-профита {lvl} изменен на {val}.", reply_markup=get_set_coins_keyboard())
                elif param == "tpfb":
                    fsm_state.tp_map[lvl]["fallback_indent"] = val
                    runtime_cfg["tp_map"][lvl]["fallback_indent"] = val
                    await message.answer(f"✅ Fallback тейк-профита {lvl} изменен на {val}.", reply_markup=get_set_coins_keyboard())

                if hasattr(bot_core, "fsm_states") and hasattr(bot_core, "runtime_manager"):
                    await bot_core.runtime_manager.sync_with_fsm(bot_core.fsm_states, force_save=True)
            await state.clear()
        except ValueError:
            await message.answer("❌ Ошибка: Введите число (например 0.6).")

    return router

