# ==============================================================================
# Path: TG/HANDLERS/settings_router.py
# Role: Роутер настроек уведомлений и порогов авто-закрытия (Auto Closing)
# ==============================================================================

from typing import Any

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from consts import DATA_DIR
from c_log import UnifiedLogger
from TG.keyboards import get_back_keyboard, get_main_keyboard
from TG.states import TGStates
from c_utils import Utils

logger = UnifiedLogger("TGSettingsRouter")


def get_settings_router(bot_core: Any) -> Router:
    """Фабрика роутера настроек уведомлений и авто-закрытия."""
    router = Router(name="settings_router")

    # =========================================================
    # NOTIFICATIONS
    # =========================================================
    @router.message(F.text == "🔔 Notifications")
    async def on_notifications_menu(message: Message, state: FSMContext):
        await state.clear()
        app_cfg = Utils.read_json_file(DATA_DIR / "app.json") or {}
        notif_cfg = app_cfg.get("notifications")
        if notif_cfg:
            enabled = notif_cfg.get("enabled", False)
            neg_th = notif_cfg.get("negative_threshold", 0.0)
            pos_th = notif_cfg.get("positive_threshold", 0.0)
            neg_flag = getattr(bot_core.notifier, "neg_flag", False) if hasattr(bot_core, "notifier") else False
            pos_flag = getattr(bot_core.notifier, "pos_flag", False) if hasattr(bot_core, "notifier") else False
            status_emoji = "✅ Вкл" if enabled else "❌ Выкл"
            text = (
                f"<b>Управление уведомлениями (Портфель):</b>\n"
                f"Статус: {status_emoji}\n"
                f"Отрицательный порог: <b>{neg_th} USDT</b> (Сработал: {neg_flag})\n"
                f"Положительный порог: <b>{pos_th} USDT</b> (Сработал: {pos_flag})\n"
            )
        else:
            text = "<b>🔔 Уведомления по куполам стратегий:</b>\n\n"
            reg = getattr(bot_core, "strategy_registry", None)
            strats = reg.get_active_strategies() if hasattr(reg, "get_active_strategies") else getattr(reg, "active_strategies", {})
            for sid, scfg in strats.items():
                sn_cfg = getattr(scfg, "notifications_cfg", {})
                en_str = "✅" if sn_cfg.get("enabled", True) else "❌"
                neg_th = sn_cfg.get("negative_threshold", -20.0)
                pos_th = sn_cfg.get("positive_threshold", 20.0)
                sflags = getattr(bot_core.notifier, "strat_flags", {}).get(sid, {}) if hasattr(bot_core, "notifier") else {}
                n_flg = "🔔" if sflags.get("neg_flag") else "ок"
                p_flg = "🎉" if sflags.get("pos_flag") else "ок"
                text += (
                    f"• <b>{getattr(scfg, 'name', sid)}</b> ({sid}) {en_str}\n"
                    f"  ├ Просадка: <b>{neg_th} USDT</b> [{n_flg}]\n"
                    f"  └ Профицит: <b>{pos_th} USDT</b> [{p_flg}]\n"
                )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔕 Сбросить флаги всех куполов", callback_data="notif_reset_flags")]]
        )
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

    @router.callback_query(F.data == "notif_reset_flags")
    async def process_notif_reset_flags(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        if hasattr(bot_core, "notifier") and hasattr(bot_core.notifier, "reset_flags"):
            bot_core.notifier.reset_flags()
            await callback.message.answer("✅ Флаги уведомлений успешно сброшены вручную!", reply_markup=get_main_keyboard())
        else:
            await callback.message.answer("❌ Модуль уведомлений не найден.")

    @router.callback_query(F.data == "notif_edit_neg")
    async def process_notif_edit_neg(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        await state.set_state(TGStates.waiting_for_notif_neg)
        await callback.message.answer("Введите новый отрицательный порог (например, -150.0):", reply_markup=get_back_keyboard())

    @router.callback_query(F.data == "notif_edit_pos")
    async def process_notif_edit_pos(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        await state.set_state(TGStates.waiting_for_notif_pos)
        await callback.message.answer("Введите новый положительный порог (например, 150.0):", reply_markup=get_back_keyboard())

    @router.message(TGStates.waiting_for_notif_neg)
    async def process_notif_neg_input(message: Message, state: FSMContext):
        if message.text in {"🔙 Back", "🔙 Назад"}:
            await state.clear()
            await message.answer("Действие отменено.", reply_markup=get_main_keyboard())
            return
        try:
            new_th = float(message.text.replace(",", "."))
            app_path = DATA_DIR / "app.json"
            app_data = Utils.read_json_file(app_path) or {}
            app_data.setdefault("notifications", {})["negative_threshold"] = new_th
            Utils.write_json_file(app_path, app_data)
            await message.answer(f"✅ Отрицательный порог успешно установлен на {new_th}.", reply_markup=get_main_keyboard())
            await state.clear()
        except ValueError:
            await message.answer("❌ Некорректное число. Введите порог еще раз (например, -150.0) или нажмите Back:")

    @router.message(TGStates.waiting_for_notif_pos)
    async def process_notif_pos_input(message: Message, state: FSMContext):
        if message.text in {"🔙 Back", "🔙 Назад"}:
            await state.clear()
            await message.answer("Действие отменено.", reply_markup=get_main_keyboard())
            return
        try:
            new_th = float(message.text.replace(",", "."))
            app_path = DATA_DIR / "app.json"
            app_data = Utils.read_json_file(app_path) or {}
            app_data.setdefault("notifications", {})["positive_threshold"] = new_th
            Utils.write_json_file(app_path, app_data)
            await message.answer(f"✅ Положительный порог успешно установлен на {new_th}.", reply_markup=get_main_keyboard())
            await state.clear()
        except ValueError:
            await message.answer("❌ Некорректное число. Введите порог еще раз (например, 150.0) или нажмите Back:")

    # =========================================================
    # AUTO CLOSING
    # =========================================================
    def _fmt(v):
        return "Выкл (0/null)" if (v is None or v == 0) else str(v)

    @router.message(F.text == "🛑 Auto Closing")
    async def on_auto_closing_menu(message: Message, state: FSMContext):
        await state.clear()
        app_cfg = Utils.read_json_file(DATA_DIR / "app.json") or {}
        auto_cfg = app_cfg.get("auto_closing")

        if auto_cfg:
            pos = auto_cfg.get("positive", {})
            neg = auto_cfg.get("negative", {})
            text = (
                f"<b>Управление Авто-Закрытием (Портфель):</b>\n\n"
                f"🟢 <b>Positive (Тейк Профит):</b>\n"
                f"├ Порог: <b>{_fmt(pos.get('threshold'))}</b>\n"
                f"└ Шаг инкремента: <b>{_fmt(pos.get('threshold_increment'))}</b>\n\n"
                f"🔴 <b>Negative (Стоп Лосс):</b>\n"
                f"├ Порог: <b>{_fmt(neg.get('threshold'))}</b>\n"
                f"└ Шаг инкремента: <b>{_fmt(neg.get('threshold_increment'))}</b>\n\n"
                f"<i>Примечание: отправьте 0 чтобы отключить (null).</i>"
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="🟢 Изменить POS порог", callback_data="autoclose_edit_pos_th"),
                        InlineKeyboardButton(text="🟢 Изменить POS шаг", callback_data="autoclose_edit_pos_inc"),
                    ],
                    [
                        InlineKeyboardButton(text="🔴 Изменить NEG порог", callback_data="autoclose_edit_neg_th"),
                        InlineKeyboardButton(text="🔴 Изменить NEG шаг", callback_data="autoclose_edit_neg_inc"),
                    ],
                ]
            )
        else:
            text = "<b>🛑 Авто-Закрытие по куполам стратегий:</b>\n\n"
            reg = getattr(bot_core, "strategy_registry", None)
            strats = reg.get_active_strategies() if hasattr(reg, "get_active_strategies") else getattr(reg, "active_strategies", {})
            for sid, scfg in strats.items():
                sac = getattr(scfg, "auto_closing_cfg", {})
                pos_th = sac.get("positive", {}).get("threshold")
                neg_th = sac.get("negative", {}).get("threshold")
                text += (
                    f"• <b>{getattr(scfg, 'name', sid)}</b> ({sid}):\n"
                    f"  ├ 🟢 Take-Profit: <b>{_fmt(pos_th)} USDT</b>\n"
                    f"  └ 🔴 Stop-Loss: <b>{_fmt(neg_th)} USDT</b>\n"
                )
            keyboard = get_back_keyboard()

        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

    async def _edit_autoclose_field(callback: CallbackQuery, state: FSMContext, state_val: State, msg: str):
        await callback.answer()
        await callback.message.answer(msg, reply_markup=get_back_keyboard())
        await state.set_state(state_val)

    @router.callback_query(F.data == "autoclose_edit_pos_th")
    async def process_autoclose_edit_pos_th(callback: CallbackQuery, state: FSMContext):
        await _edit_autoclose_field(callback, state, TGStates.waiting_for_autoclose_pos_th, "Введите новый POSITIVE порог (или 0):")

    @router.callback_query(F.data == "autoclose_edit_pos_inc")
    async def process_autoclose_edit_pos_inc(callback: CallbackQuery, state: FSMContext):
        await _edit_autoclose_field(callback, state, TGStates.waiting_for_autoclose_pos_inc, "Введите новый POSITIVE шаг инкремента (или 0):")

    @router.callback_query(F.data == "autoclose_edit_neg_th")
    async def process_autoclose_edit_neg_th(callback: CallbackQuery, state: FSMContext):
        await _edit_autoclose_field(callback, state, TGStates.waiting_for_autoclose_neg_th, "Введите новый NEGATIVE порог (или 0):")

    @router.callback_query(F.data == "autoclose_edit_neg_inc")
    async def process_autoclose_edit_neg_inc(callback: CallbackQuery, state: FSMContext):
        await _edit_autoclose_field(callback, state, TGStates.waiting_for_autoclose_neg_inc, "Введите новый NEGATIVE шаг инкремента (или 0):")

    async def _save_autoclose_field(message: Message, state: FSMContext, section: str, field: str):
        if message.text in {"🔙 Back", "🔙 Назад"}:
            await state.clear()
            return await on_auto_closing_menu(message, state)
        try:
            val = float(message.text.replace(",", "."))
            if val == 0:
                val = None
            app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
            app_data.setdefault("auto_closing", {"negative": {}, "positive": {}})
            app_data["auto_closing"].setdefault(section, {})[field] = val
            Utils.write_json_file(DATA_DIR / "app.json", app_data)

            await message.answer(f"✅ Параметр {section} -> {field} успешно обновлен.", parse_mode="HTML")
            await on_auto_closing_menu(message, state)
        except ValueError:
            await message.answer("❌ Неверный формат числа. Введите число:")

    @router.message(StateFilter(TGStates.waiting_for_autoclose_pos_th))
    async def handle_autoclose_pos_th(message: Message, state: FSMContext):
        await _save_autoclose_field(message, state, "positive", "threshold")

    @router.message(StateFilter(TGStates.waiting_for_autoclose_pos_inc))
    async def handle_autoclose_pos_inc(message: Message, state: FSMContext):
        await _save_autoclose_field(message, state, "positive", "threshold_increment")

    @router.message(StateFilter(TGStates.waiting_for_autoclose_neg_th))
    async def handle_autoclose_neg_th(message: Message, state: FSMContext):
        await _save_autoclose_field(message, state, "negative", "threshold")

    @router.message(StateFilter(TGStates.waiting_for_autoclose_neg_inc))
    async def handle_autoclose_neg_inc(message: Message, state: FSMContext):
        await _save_autoclose_field(message, state, "negative", "threshold_increment")

    return router
