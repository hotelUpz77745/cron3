# ==============================================================================
# Path: TG/HANDLERS/super_grid_router.py
# Role: Роутер управления параметрами Super Grid (волатильность стратегий)
# ==============================================================================

import asyncio
import io
import json
import os
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from consts import DATA_DIR
from c_log import UnifiedLogger
from TG.keyboards import get_back_keyboard, get_main_keyboard
from TG.states import TGStates
from TG.strategy_helpers import extract_strategy_symbols, get_strategy_meta
from c_utils import Utils

logger = UnifiedLogger("TGSuperGridRouter")


def get_super_grid_router(bot_core: Any) -> Router:
    """Фабрика роутера управления динамической волатильностью Super Grid."""
    router = Router(name="super_grid_router")

    @router.message(F.text == "🔧 Super Grid")
    async def on_super_grid_btn(message: Message, state: FSMContext):
        await state.clear()
        app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
        active_strategies = app_data.get("active_strategies", {})

        buttons = []
        for sid, sdata in active_strategies.items():
            if not sdata.get("enabled", False):
                continue
            meta = get_strategy_meta(sid, bot_core)
            sg_badge = "✅ ON" if meta.get("super_grid", False) else "❌ OFF"
            s_name = meta.get("name", sid)
            buttons.append([InlineKeyboardButton(text=f"🔧 {s_name[:30]} [{sg_badge}]", callback_data=f"sg_view_{sid}")])

        buttons.append([InlineKeyboardButton(text="🌐 Глобальный Super Grid (app.json)", callback_data="sg_global_edit")])
        buttons.append([InlineKeyboardButton(text="🔙 Закрыть", callback_data="edit_side_cancel")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer("🔧 <b>Управление Super Grid (волатильность)</b>\n\nВыберите стратегию:", reply_markup=keyboard, parse_mode="HTML")

    @router.callback_query(F.data.startswith("sg_view_"))
    async def process_sg_view(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        sid = callback.data.replace("sg_view_", "")
        app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
        strat_cfg = app_data.get("active_strategies", {}).get(sid, {})
        sg_cfg = strat_cfg.get("super_grid", {})
        en = sg_cfg.get("enabled", False)
        toggle_text = "⏸ Отключить Super Grid" if en else "▶️ Включить Super Grid"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=toggle_text, callback_data=f"sg_toggle_{sid}")],
                [InlineKeyboardButton(text="✏️ Редактировать JSON", callback_data=f"sg_edit_json_{sid}")],
                [InlineKeyboardButton(text="🔙 Назад к списку", callback_data="sg_back_list")],
            ]
        )
        syms = ", ".join(extract_strategy_symbols(strat_cfg)) or "нет"
        text = (
            f"🔧 <b>Super Grid:</b> <code>{sid}</code>\n\n"
            f"• Статус: <b>{'✅ Включен' if en else '❌ Отключен'}</b>\n"
            f"• Timeframe: <b>{sg_cfg.get('timeframe', '1w')}</b>\n"
            f"• Window: <b>{sg_cfg.get('window', 12)} баров</b>\n"
            f"• Multiplier: <b>x{sg_cfg.get('multiplier', 2.0)}</b>\n"
            f"• Min Volatility: <b>{sg_cfg.get('min_volatility_pct', 15.0)}%</b>\n\n"
            f"Монеты: <code>{syms}</code>"
        )
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

    @router.callback_query(F.data.startswith("sg_toggle_"))
    async def process_sg_toggle(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        sid = callback.data.replace("sg_toggle_", "")
        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path) or {}
        strat_cfg = app_data.get("active_strategies", {}).get(sid, {})
        strat_cfg.setdefault(
            "super_grid",
            {"enabled": True, "timeframe": "1w", "window": 12, "multiplier": 2.0, "min_volatility_pct": 15.0, "update_interval_hours": 24},
        )
        curr = strat_cfg["super_grid"].get("enabled", False)
        strat_cfg["super_grid"]["enabled"] = not curr
        Utils.write_json_file(app_json_path, app_data)

        if hasattr(bot_core, "volatility_manager") and getattr(bot_core.volatility_manager, "is_running", False):
            asyncio.create_task(bot_core.volatility_manager.process_all())

        callback.data = f"sg_view_{sid}"
        await process_sg_view(callback, state)

    @router.callback_query(F.data == "sg_back_list")
    async def process_sg_back_list(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
        active_strategies = app_data.get("active_strategies", {})
        buttons = []
        for sid, sdata in active_strategies.items():
            if not sdata.get("enabled", False):
                continue
            meta = get_strategy_meta(sid, bot_core)
            sg_badge = "✅ ON" if meta.get("super_grid", False) else "❌ OFF"
            s_name = meta.get("name", sid)
            buttons.append([InlineKeyboardButton(text=f"🔧 {s_name[:30]} [{sg_badge}]", callback_data=f"sg_view_{sid}")])
        buttons.append([InlineKeyboardButton(text="🌐 Глобальный Super Grid (app.json)", callback_data="sg_global_edit")])
        buttons.append([InlineKeyboardButton(text="🔙 Закрыть", callback_data="edit_side_cancel")])
        await callback.message.edit_text("🔧 <b>Управление Super Grid</b>\n\nВыберите стратегию:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

    @router.callback_query(F.data == "sg_global_edit")
    async def process_sg_global_edit(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
        app_data.setdefault(
            "super_grid",
            {"enabled": True, "timeframe": "1w", "window": 12, "multiplier": 2.0, "min_volatility_pct": 15.0, "update_interval_hours": 24},
        )
        dump_path = os.path.join("logs", "Super_Grid_edit.json")
        os.makedirs("logs", exist_ok=True)
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(app_data["super_grid"], indent=4))
        await callback.message.answer("<b>Глобальные настройки Super Grid:</b>\nОтредактируйте файл и отправьте обратно.", reply_markup=get_back_keyboard(), parse_mode="HTML")
        await callback.message.answer_document(FSInputFile(dump_path))
        await state.set_state(TGStates.waiting_for_super_grid_json)

    @router.callback_query(F.data.startswith("sg_edit_json_"))
    async def process_sg_edit_json(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        sid = callback.data.replace("sg_edit_json_", "")
        app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
        strat_cfg = app_data.get("active_strategies", {}).get(sid, {})
        sg_cfg = strat_cfg.get("super_grid", {})
        dump_path = os.path.join("logs", f"Super_Grid_{sid}.json")
        os.makedirs("logs", exist_ok=True)
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(sg_cfg, indent=4))
        await state.update_data(editing_sg_strat=sid)
        await callback.message.answer(f"<b>Настройки Super Grid для {sid}:</b>\nОтредактируйте файл и отправьте обратно.", reply_markup=get_back_keyboard(), parse_mode="HTML")
        await callback.message.answer_document(FSInputFile(dump_path))
        await state.set_state(TGStates.waiting_for_strat_super_grid_json)

    @router.message(TGStates.waiting_for_strat_super_grid_json)
    async def process_strat_super_grid_json(message: Message, state: FSMContext):
        if message.text in {"🔙 Back", "🔙 Назад"}:
            await state.clear()
            return await on_super_grid_btn(message, state)
        json_str = ""
        if message.document:
            bot = getattr(bot_core, "bot", None)
            if bot:
                file = await bot.get_file(message.document.file_id)
                out = io.BytesIO()
                await bot.download_file(file.file_path, out)
                json_str = out.getvalue().decode("utf-8")
        elif message.text:
            json_str = message.text

        try:
            parsed = json.loads(json_str)
            st_data = await state.get_data()
            sid = st_data.get("editing_sg_strat")
            if sid:
                app_path = DATA_DIR / "app.json"
                app_data = Utils.read_json_file(app_path) or {}
                if sid in app_data.get("active_strategies", {}):
                    app_data["active_strategies"][sid]["super_grid"] = parsed
                    Utils.write_json_file(app_path, app_data)
                    await message.answer(f"✅ Super Grid для <code>{sid}</code> успешно обновлен!", reply_markup=get_main_keyboard(), parse_mode="HTML")
            await state.clear()
        except Exception as e:
            await message.answer(f"❌ Ошибка JSON: {e}")

    @router.message(TGStates.waiting_for_super_grid_json)
    async def process_global_super_grid_json(message: Message, state: FSMContext):
        if message.text in {"🔙 Back", "🔙 Назад"}:
            await state.clear()
            return await on_super_grid_btn(message, state)
        json_str = ""
        if message.document:
            bot = getattr(bot_core, "bot", None)
            if bot:
                file = await bot.get_file(message.document.file_id)
                out = io.BytesIO()
                await bot.download_file(file.file_path, out)
                json_str = out.getvalue().decode("utf-8")
        elif message.text:
            json_str = message.text

        try:
            parsed = json.loads(json_str)
            app_path = DATA_DIR / "app.json"
            app_data = Utils.read_json_file(app_path) or {}
            app_data["super_grid"] = parsed
            Utils.write_json_file(app_path, app_data)
            await message.answer("✅ Глобальный Super Grid успешно обновлен!", reply_markup=get_main_keyboard(), parse_mode="HTML")
            await state.clear()
        except Exception as e:
            await message.answer(f"❌ Ошибка JSON: {e}")

    return router
