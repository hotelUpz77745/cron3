# ==============================================================================
# Path: TG/HANDLERS/positions_router.py
# Role: Роутер экстренного закрытия позиций куполов и всего портфеля (Close All)
# ==============================================================================

from typing import Any

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from c_log import UnifiedLogger
from TG.keyboards import get_main_keyboard
from TG.states import TGStates

logger = UnifiedLogger("TGPositionsRouter")


def get_positions_router(bot_core: Any) -> Router:
    """Фабрика роутера закрытия позиций."""
    router = Router(name="positions_router")

    @router.message(F.text == "🚨 Close All")
    async def on_close_all(message: Message, state: FSMContext):
        await state.clear()
        active_strats = {}
        if hasattr(bot_core, "strategy_registry"):
            reg = bot_core.strategy_registry
            active_strats = reg.get_active_strategies() if hasattr(reg, "get_active_strategies") else getattr(reg, "active_strategies", {})

        buttons = []
        for sid, scfg in active_strats.items():
            s_name = getattr(scfg, "name", None) or sid
            buttons.append([InlineKeyboardButton(
                text=f"🚨 Закрыть купол [{s_name}]",
                callback_data=f"close_dome_{sid}",
            )])

        buttons.append([InlineKeyboardButton(text="💥 ЗАКРЫТЬ ВСЁ (Все купола)", callback_data="close_dome_all")])
        buttons.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="close_dome_cancel")])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer(
            "🚨 <b>УПРАВЛЕНИЕ ЗАКРЫТИЕМ ПОЗИЦИЙ</b>\n\n"
            "Выберите, какой купол стратегии необходимо закрыть, либо выполните закрытие всего портфеля:\n\n"
            "<i>• <b>Закрыть купол</b>: закрывает позиции и снимает ордера только выбранной стратегии, фиксирует цикл в лидерборде.\n"
            "• <b>ЗАКРЫТЬ ВСЁ</b>: глобальный экстренный сброс всех монет и всех стратегий.</i>",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    @router.callback_query(F.data.startswith("close_dome_"))
    async def on_close_dome_callback(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        cdata = callback.data
        if cdata == "close_dome_cancel":
            await state.clear()
            await callback.message.edit_text("❌ Закрытие отменено.")
            return

        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="close_dome_cancel")],
        ])

        if cdata == "close_dome_all":
            await state.set_state(TGStates.waiting_for_close_all_confirm)
            await state.update_data(target_close_dome="all")
            await callback.message.edit_text(
                "🚨 <b>ПОДТВЕРЖДЕНИЕ ЗАКРЫТИЯ ВСЕХ КУПОЛОВ</b>\n\n"
                "⚠️ <b>ВНИМАНИЕ!</b>\n"
                "Будут отменены все лимитные ордера и закрыты позиции по рынку <b>ВО ВСЕХ СТРАТЕГИЯХ</b> портфеля, а циклы перезапущены!\n\n"
                "👉 Для подтверждения отправьте текстовым сообщением слово:\n"
                "<b>ЗАКРЫТЬ</b>\n\n"
                "<i>Или нажмите «❌ Отмена» ниже для отмены операции.</i>",
                reply_markup=cancel_kb,
                parse_mode="HTML",
            )
            return

        strat_id = cdata.replace("close_dome_", "")
        strat = bot_core.strategy_registry.get_strategy(strat_id) if hasattr(bot_core, "strategy_registry") else None
        strat_name = getattr(strat, "name", None) or strat_id
        symbols_str = ", ".join(strat.symbols) if (strat and hasattr(strat, "symbols") and strat.symbols) else "нет"

        await state.set_state(TGStates.waiting_for_close_all_confirm)
        await state.update_data(target_close_dome=strat_id)
        await callback.message.edit_text(
            f"⚠️ <b>ПОДТВЕРЖДЕНИЕ ЗАКРЫТИЯ КУПОЛА</b>\n\n"
            f"🌌 Купол: <b>{strat_name}</b> (<code>{strat_id}</code>)\n"
            f"🪙 Обойма монет: <b>{symbols_str}</b>\n\n"
            f"Будут отменены все ордера, закрыты позиции по рынку только для монет этого купола и сброшен текущий цикл.\n"
            f"<i>Остальные купола продолжат торговлю без изменений.</i>\n\n"
            f"👉 Для подтверждения отправьте текстовым сообщением слово:\n"
            f"<b>ЗАКРЫТЬ</b>\n\n"
            f"<i>Или нажмите «❌ Отмена» ниже для отмены операции.</i>",
            reply_markup=cancel_kb,
            parse_mode="HTML",
        )

    @router.message(StateFilter(TGStates.waiting_for_close_all_confirm))
    async def process_close_all_confirm(message: Message, state: FSMContext):
        data = await state.get_data()
        target = data.get("target_close_dome")
        await state.clear()

        text_val = (message.text or "").strip().lower()
        if text_val != "закрыть":
            await message.answer("❌ Закрытие отменено.", reply_markup=get_main_keyboard())
            return

        if target == "all":
            msg = await message.answer("⏳ Закрытие всех куполов и отмена лимитных ордеров...", reply_markup=get_main_keyboard())
            try:
                reset_report = await bot_core.close_all_positions(reset_iterations=True)
                active_tfrs = []
                if hasattr(bot_core, "strategy_registry") and bot_core.strategy_registry:
                    for s in bot_core.strategy_registry.active_strategies.values():
                        t = getattr(s, "signal_heartbeat_tfr", None)
                        if t and t not in active_tfrs:
                            active_tfrs.append(t)
                tfr_all_label = "/".join(active_tfrs) if active_tfrs else "5m"

                lines = [
                    "✅ <b>Все купола закрыты, лимитные ордера отменены!</b>",
                    f"🛡️ <b>Идемпотентный замок входов:</b> активен до открытия новой свечи ({tfr_all_label}) (защита от дергания).",
                ]
                if reset_report:
                    lines.append("\n🔄 <b>Сброс циклов куполов (Positive Auto-Closing):</b>")
                    for strat_id, info in reset_report.items():
                        s_obj = bot_core.strategy_registry.get_strategy(strat_id) if hasattr(bot_core, "strategy_registry") else None
                        name = getattr(s_obj, "name", None) or strat_id
                        lines.append(
                            f"• <b>{name}</b>: Старт <b>Цикла #{info['new_iteration']}</b> (базовая точка Net: <b>{info['base_net']:+.2f} USDT</b>)"
                        )
                lines.append("\n📊 <b>Лидерборд синхронизирован.</b>")
                await msg.edit_text("\n".join(lines), parse_mode="HTML")
            except Exception as e:
                logger.error(f"Failed to Close All: {e}")
                await msg.edit_text(f"❌ Произошла ошибка при закрытии всех куполов: {e}")
        elif target:
            strat_id = target
            strat = bot_core.strategy_registry.get_strategy(strat_id) if hasattr(bot_core, "strategy_registry") else None
            strat_name = getattr(strat, "name", None) or strat_id
            tfr_strat_label = getattr(strat, "signal_heartbeat_tfr", "5m") if strat else "5m"
            msg = await message.answer(f"⏳ Закрытие позиций купола <b>{strat_name}</b>...", reply_markup=get_main_keyboard(), parse_mode="HTML")
            try:
                await bot_core.close_strategy_positions(strat_id)
                info = await bot_core.reset_strategy_iteration(strat_id)
                lines = [
                    f"✅ <b>Купол [{strat_name}] успешно закрыт!</b>",
                    f"Позиции монет закрыты по рынку, ордера отменены.",
                    f"🔄 Старт <b>Цикла #{info['new_iteration']}</b> (базовая точка Net: <b>{info['base_net']:+.2f} USDT</b>)",
                    f"🛡️ <b>Идемпотентный замок входов:</b> активен до открытия новой свечи ({tfr_strat_label}) (защита от дергания).",
                    "\n📊 Лидерборд и аналитика обновлены.",
                ]
                await msg.edit_text("\n".join(lines), parse_mode="HTML")
            except Exception as e:
                logger.error(f"Failed to close strategy dome {strat_id}: {e}")
                await msg.edit_text(f"❌ Ошибка закрытия купола {strat_name}: {e}")
        else:
            await message.answer("❌ Купол для закрытия не найден.", reply_markup=get_main_keyboard())

    return router
