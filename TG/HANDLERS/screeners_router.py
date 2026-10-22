# ==============================================================================
# Path: TG/HANDLERS/screeners_router.py
# Role: Роутер сервиса скринеров и селекторов (Alpha Selector, Volatility Scanner / Сонник)
# ==============================================================================

import io
import json
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from consts import DATA_DIR
from c_log import UnifiedLogger
from TG.keyboards import get_back_keyboard, get_main_keyboard
from TG.states import TGStates
from c_utils import Utils

logger = UnifiedLogger("TGScreenersRouter")


def _get_screener_main_markup() -> InlineKeyboardMarkup:
    """Главная клавиатура сервиса рыночных скринеров."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚡ 1. Alpha Selector (Wiggle & NATR)", callback_data="cb_selector_run:alpha_coin_selector")],
            [InlineKeyboardButton(text="🔍 2. Volatility Scanner (Сонник)", callback_data="cb_selector_run:volatility_scanner")],
            [InlineKeyboardButton(text="⚙️ Настройки скринеров", callback_data="cb_screener_settings_menu")],
        ]
    )


def get_screeners_router(bot_core: Any) -> Router:
    """Фабрика роутера скринеров и селекторов волатильности."""
    router = Router(name="screeners_router")
    scanner_running = False

    @router.message(Command("screener"))
    @router.message(Command("selector"))
    @router.message(Command("sonnik"))
    @router.message(F.text == "🎯 Скринеры")
    @router.message(F.text == "🎯 Selector")
    async def on_screener_cmd(message: Message, state: FSMContext):
        await state.clear()
        text = (
            "🎯 <b>Единый сервис рыночных скринеров</b>\n\n"
            "Выберите инструмент для сканирования и анализа рынка:\n"
            "1️⃣ ⚡ <b>Alpha Selector</b> — Institutional Wiggle & NATR ранжирование (поверх пула волатильности).\n"
            "2️⃣ 🔍 <b>Volatility Scanner (Сонник)</b> — Мультифакторный фильтр волатильности (ATR, RSI, BB)."
        )
        await message.answer(text, reply_markup=_get_screener_main_markup(), parse_mode="HTML")

    @router.callback_query(F.data == "cb_screener_main_menu")
    async def process_cb_screener_main_menu(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.answer()
        text = (
            "🎯 <b>Единый сервис рыночных скринеров</b>\n\n"
            "Выберите инструмент для сканирования и анализа рынка:\n"
            "1️⃣ ⚡ <b>Alpha Selector</b> — Institutional Wiggle & NATR ранжирование (поверх пула волатильности).\n"
            "2️⃣ 🔍 <b>Volatility Scanner (Сонник)</b> — Мультифакторный фильтр волатильности (ATR, RSI, BB)."
        )
        try:
            await callback.message.edit_text(text, reply_markup=_get_screener_main_markup(), parse_mode="HTML")
        except Exception:
            await callback.message.answer(text, reply_markup=_get_screener_main_markup(), parse_mode="HTML")

    @router.callback_query(F.data == "cb_screener_settings_menu")
    async def process_cb_screener_settings_menu(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Настройки Volatility Scanner", callback_data="cb_edit_screener_cfg:volatility_scanner")],
                [InlineKeyboardButton(text="⚡ Настройки Alpha Selector", callback_data="cb_edit_screener_cfg:alpha_coin_selector")],
                [InlineKeyboardButton(text="🔙 Назад в меню скринеров", callback_data="cb_screener_main_menu")],
            ]
        )
        text = "⚙️ <b>Настройки сервиса скринеров</b>\n\nВыберите компонент для редактирования параметров в <code>app.json</code>:"
        try:
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")

    @router.callback_query(F.data == "cb_edit_scanner_config")
    async def process_cb_edit_scanner_config_compat(callback: CallbackQuery, state: FSMContext):
        await process_cb_screener_settings_menu(callback, state)

    @router.callback_query(F.data.startswith("cb_edit_screener_cfg:"))
    async def process_cb_edit_screener_cfg(callback: CallbackQuery, state: FSMContext):
        target_key = callback.data.split(":", 1)[1]
        await callback.answer()
        config_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(config_path) if config_path.exists() else {}
        cfg = app_data.get(target_key, {})

        names_map = {
            "volatility_scanner": "Volatility Scanner (Сонник)",
            "alpha_coin_selector": "Alpha Selector (Wiggle & NATR)",
        }
        comp_name = names_map.get(target_key, target_key)

        await state.update_data(screener_cfg_key=target_key)
        json_bytes = json.dumps(cfg, indent=4, ensure_ascii=False).encode("utf-8")
        await callback.message.answer(
            f"<b>Текущие настройки {comp_name}:</b>\nОтредактируйте файл и отправьте обратно документом (или текстом).",
            reply_markup=get_back_keyboard(),
            parse_mode="HTML",
        )
        await callback.message.answer_document(BufferedInputFile(json_bytes, filename=f"{target_key}_config.json"))
        await state.set_state(TGStates.waiting_for_scanner_json)

    @router.callback_query(F.data == "cb_run_scanner")
    async def process_cb_run_scanner_compat(callback: CallbackQuery, state: FSMContext):
        callback.data = "cb_selector_run:volatility_scanner"
        await process_cb_selector_run(callback, state)

    @router.callback_query(F.data.startswith("cb_selector_run:"))
    async def process_cb_selector_run(callback: CallbackQuery, state: FSMContext):
        nonlocal scanner_running
        selector_type = callback.data.split(":", 1)[1]
        if scanner_running:
            await callback.answer("Команда уже запущена, дождитесь результата", show_alert=True)
            return
        scanner_running = True
        await callback.answer("Запускаю отбор монет...")
        status_msg = await callback.message.answer(f"⏳ Сканирование ({selector_type}), пожалуйста подождите...")

        try:
            from CORE.SERVICES.SELECTOR.coin_selection_service import CoinSelectionService

            service = CoinSelectionService(bot_core=bot_core)
            target_count = service.get_configured_target_count(selector_type=selector_type)

            if selector_type == "volatility_scanner":
                import importlib
                import CORE.SERVICES.VOLATILITY.volatility_scanner as vs

                importlib.reload(vs)
                scanner = vs.VolatilityScanner(client=getattr(bot_core, "client", None))
                await scanner.scan()

            results = await service.get_top_coins(selector_type=selector_type, target_count=target_count)
            report_text = service.format_telegram_report(results, selector_type, target_count)
            inline_buttons = [
                [InlineKeyboardButton(text="🔄 Пересканировать", callback_data=f"cb_selector_run:{selector_type}")],
                [InlineKeyboardButton(text="⚙️ Настройки скринеров", callback_data="cb_screener_settings_menu")],
                [InlineKeyboardButton(text="🔙 В меню скринеров", callback_data="cb_screener_main_menu")],
            ]
            keyboard = InlineKeyboardMarkup(inline_keyboard=inline_buttons)
            await status_msg.edit_text(report_text, reply_markup=keyboard, parse_mode="HTML")

            if selector_type == "volatility_scanner":
                txt_output_path = DATA_DIR / "volatile_symbols.txt"
                if txt_output_path.exists():
                    await callback.message.answer_document(
                        FSInputFile(str(txt_output_path)), caption="✅ Детализированный отчет Volatility Scanner (свечи, тени, тело)."
                    )
        except Exception as e:
            logger.error(f"Error in screener run: {e}", exc_info=True)
            await status_msg.edit_text(f"❌ Ошибка при выполнении скринера: {e}")
        finally:
            scanner_running = False

    @router.callback_query(F.data.startswith("cb_selector_apply:"))
    async def process_cb_selector_apply(callback: CallbackQuery, state: FSMContext):
        await callback.answer(
            "ℹ️ Прямое применение монет из селектора отключено в целях безопасности баланса.\n"
            "Селектор носит сугубо информационный характер. Для добавления монет используйте меню 'Set Coins' или команды стратегий.",
            show_alert=True,
        )

    @router.message(TGStates.waiting_for_scanner_json)
    async def process_scanner_json(message: Message, state: FSMContext):
        if message.text and message.text in ("🔙 Back", "🔙 Назад"):
            await state.clear()
            await message.answer("Отменено.", reply_markup=get_main_keyboard())
            return

        state_data = await state.get_data()
        cfg_key = state_data.get("screener_cfg_key", "volatility_scanner")

        json_str = ""
        if message.document:
            bot = getattr(bot_core, "bot", None)
            if bot:
                file = await bot.get_file(message.document.file_id)
                out = io.BytesIO()
                await bot.download_file(file.file_path, out)
                json_str = out.getvalue().decode("utf-8")
        elif message.text:
            json_str = message.text.strip()
        else:
            await message.answer("❌ Пожалуйста, отправьте текстовое сообщение или .json файл.")
            return

        try:
            new_cfg = json.loads(json_str)
            config_path = DATA_DIR / "app.json"
            app_data = Utils.read_json_file(config_path) if config_path.exists() else {}
            app_data[cfg_key] = new_cfg
            Utils.write_json_file(config_path, app_data)

            await message.answer(f"✅ Настройки для <code>{cfg_key}</code> успешно обновлены!", reply_markup=get_main_keyboard(), parse_mode="HTML")
            await state.clear()
        except Exception as e:
            await message.answer(f"❌ Ошибка JSON: {e}\nИсправьте и отправьте снова.")

    return router
