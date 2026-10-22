# ==============================================================================
# Path: TG/HANDLERS/base_router.py
# Role: Роутер базовых команд и управления жизненным циклом бота (Start, Stop, Status, Logs, Backup)
# ==============================================================================

import json
import os
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from consts import ACTIVE_SYMBOLS, DATA_DIR, _CFG
from c_log import UnifiedLogger
from c_utils import Utils
from TG.keyboards import get_confirm_start_keyboard, get_main_keyboard

logger = UnifiedLogger("TGBaseRouter")

try:
    from API.BINANCE.quant_calc import QuantCalculator
except ImportError:
    QuantCalculator = None


def get_base_router(bot_core: Any, template_manager: Any = None) -> Router:
    """Фабрика роутера базового управления ботом."""
    router = Router(name="base_router")

    @router.errors()
    async def global_error_handler(event: ErrorEvent):
        if isinstance(event.exception, TelegramNetworkError):
            logger.warning(f"[TG] Network Error (suppressed): {event.exception}")
            return True
        if isinstance(event.exception, TelegramRetryAfter):
            logger.warning(f"[TG] Rate Limit exceeded, retry after {event.exception.retry_after}s")
            return True
        if "message is not modified" in str(event.exception).lower():
            logger.debug(f"[TG] Message not modified (suppressed): {event.exception}")
            return True
        logger.error(f"[TG] Unhandled exception: {event.exception}")
        return False

    @router.message(Command("start"))
    async def start_cmd(message: Message, state: FSMContext):
        await state.clear()
        status = "⏸️ Paused" if bot_core.is_paused else "▶️ Running"
        text = f"<b>Control Panel</b>\nCurrent Status: {status}"
        await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="HTML")

    @router.message(Command("quant"))
    async def secret_quant_cmd(message: Message, state: FSMContext):
        msg = await message.answer("⏳ Симуляция ликвидаций запущена...")
        try:
            if QuantCalculator is None:
                await msg.edit_text("ℹ️ Калькулятор ликвидаций устарел для данной конфигурации.")
                return
            calc = QuantCalculator()
            report = calc.generate_report()
            await msg.edit_text(f"```text\n{report}\n```", parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Quant calculator error: {e}")
            await msg.edit_text(f"❌ Ошибка калькулятора: {e}")

    @router.message(Command("sonnik_restore"))
    async def sonnik_restore_cmd(message: Message):
        msg = await message.answer("⏳ Запуск принудительного Deep Sync. Это займет некоторое время...")
        try:
            await bot_core.analytics.deep_sync_analytics(bot_core.client, bot_core.prices)
            await msg.edit_text("✅ Принудительный Deep Sync успешно завершен! Исторические данные восстановлены по первой строке леджера.")
        except Exception as e:
            logger.error(f"Failed to execute manual deep sync: {e}")
            await msg.edit_text(f"❌ Ошибка во время Deep Sync: {e}")

    @router.message(Command("radar"))
    async def radar_cmd(message: Message):
        nav = getattr(bot_core, "radar_navigator", None)
        if not nav or not nav.is_enabled():
            await message.answer("ℹ️ Штурман / Синергия с Радаром отключена или не инициализирована.")
            return
        report = nav.generate_telemetry_report()
        await message.answer(report, parse_mode="HTML")

    @router.message(Command("backup"))
    async def backup_cmd(message: Message):
        try:
            from CORE.SERVICES.runtime_backup_service import RuntimeBackupService
            status_msg = await message.answer("⏳ Создание рантайм-бэкапа позиций (.pkl ➔ JSON)...")
            res = await RuntimeBackupService.export_and_send(bot_core, chat_ids=[message.chat.id])
            p = res.get("file")
            if not res.get("telegram_sent") and p and os.path.exists(p):
                await message.answer_document(
                    FSInputFile(p),
                    caption=f"📦 <b>Рантайм-бэкап позиций</b>\nФайл: <code>{res.get('filename')}</code>\nРазмер: {res.get('size_bytes', 0) / 1024.0:.1f} KB",
                    parse_mode="HTML",
                )
            await status_msg.delete()
        except Exception as e:
            logger.error(f"Error handling /backup command: {e}")
            await message.answer(f"❌ Ошибка создания бэкапа: {e}")

    @router.callback_query(F.data.startswith("rot_accept:"))
    async def on_rot_accept(callback: CallbackQuery):
        await callback.answer()
        req_id = callback.data.split(":", 1)[1]
        nav = getattr(bot_core, "radar_navigator", None)
        if not nav or req_id not in nav.pending_rotations:
            await callback.message.answer("⚠️ Запрос на ротацию устарел (таймаут истек) или уже выполнен.")
            return
        nav.cancel_timeout_task(req_id)
        rot_data = nav.pending_rotations.pop(req_id)
        strat_id = rot_data["strat_id"]
        rotations = rot_data.get("rotations")
        if not rotations and "old_symbol" in rot_data:
            rotations = [rot_data]

        for r in rotations:
            nav.symbols_pending_rotation.discard(r["old_symbol"])

        try:
            applied_lines = []
            for r in rotations:
                await nav.apply_rotation(
                    strat_id=strat_id,
                    old_symbol=r["old_symbol"],
                    new_symbol=r["new_symbol"],
                    old_score=r.get("old_score", 0.0),
                    new_score=r.get("new_score", 0.0),
                    lead_pct=r.get("lead_pct", 0.0),
                    matched_rule=rot_data.get("matched_rule", ""),
                    mode="advisory_operator_accepted",
                )
                applied_lines.append(f"• <b>{r['old_symbol']}</b> ➔ <b>{r['new_symbol']}</b> (+{r['lead_pct']:.1f}%)")

            msg_applied = "\n".join(applied_lines)
            await callback.message.edit_text(
                f"✅ <b>Ротация выполнена!</b> (Замен: {len(rotations)})\n"
                f"{msg_applied}\n"
                f"Новый цикл стартует на обновленном составе!",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"[TG] Ошибка применения ротации: {e}")
            await callback.message.answer(f"❌ Ошибка применения ротации: {e}")

    @router.callback_query(F.data.startswith("rot_reject:"))
    async def on_rot_reject(callback: CallbackQuery):
        await callback.answer()
        req_id = callback.data.split(":", 1)[1]
        nav = getattr(bot_core, "radar_navigator", None)
        if nav and req_id in nav.pending_rotations:
            nav.cancel_timeout_task(req_id)
            rot_data = nav.pending_rotations.pop(req_id)
            rotations = rot_data.get("rotations")
            if not rotations and "old_symbol" in rot_data:
                rotations = [rot_data]
            for r in rotations:
                nav.symbols_pending_rotation.discard(r["old_symbol"])
            await callback.message.edit_text(
                f"❌ <b>Ротация отклонена.</b>\n"
                f"Боевой пул стратегии [{rot_data['strat_id']}] сохранен без изменений.",
                parse_mode="HTML",
            )
        else:
            await callback.message.answer("ℹ️ Запрос на ротацию не найден или уже закрыт.")

    @router.message(F.text == "▶️ Start")
    async def on_pre_start(message: Message, state: FSMContext):
        await state.clear()
        if not bot_core.is_paused:
            await message.answer("⚠️ Trading is already running!", reply_markup=get_main_keyboard())
            return

        symbols = bot_core.symbols if hasattr(bot_core, "symbols") and bot_core.symbols else ACTIVE_SYMBOLS
        tm = template_manager or getattr(bot_core, "template_manager", None)

        if tm:
            for sym in symbols:
                runtime_path = tm.get_runtime_path(sym)
                if runtime_path.exists():
                    try:
                        if str(runtime_path).endswith(".pkl"):
                            import pickle
                            with open(runtime_path, "rb") as f:
                                data = pickle.load(f)
                        else:
                            with open(runtime_path, "r", encoding="utf-8") as f:
                                data = json.load(f)

                        lines = [f"<b>{sym} Settings:</b>"]
                        for side in ["LONG", "SHORT"]:
                            cfg = data[side]
                            en = cfg["enable"]
                            sz = cfg["invest_size"]
                            lev = cfg["leverage"]
                            margin = cfg["margin_type"]

                            lines.append(f"  <b>{side}</b>: {'✅ On' if en else '❌ Off'}")
                            if en:
                                lines.append(f"    ├ Invest: {sz}$ | Lev: {lev}x | {margin}")
                                grid = cfg["grid"]
                                indents = []
                                super_indents = []
                                for k in sorted(grid.keys(), key=lambda x: int(x) if x.isdigit() else x):
                                    lvl_d = grid[k]
                                    if isinstance(lvl_d, dict) and (lvl_d.get("enabled", True) is False or lvl_d.get("indent") is None):
                                        indents.append("off")
                                    else:
                                        indents.append(str(lvl_d.get("indent") if isinstance(lvl_d, dict) else lvl_d))
                                    si = lvl_d.get("super_indent") if isinstance(lvl_d, dict) else None
                                    super_indents.append(str(si) if si is not None else "-")
                                lines.append(f"    ├ Grid: [{', '.join(indents)}]")

                                tp_map = cfg["tp_map"]
                                tps = []
                                for k in sorted(tp_map.keys(), key=lambda x: int(x) if x.isdigit() else x):
                                    tp_d = tp_map[k]
                                    raw_tp_ind = tp_d.get("indent") if isinstance(tp_d, dict) else tp_d
                                    tps.append(str(raw_tp_ind) if raw_tp_ind is not None else "-")
                                lines.append(f"    └ TP: [{', '.join(tps)}]")

                        await message.answer("\n".join(lines), parse_mode="HTML")
                    except Exception as e:
                        await message.answer(f"<b>{sym}</b>: [Ошибка чтения конфигурации: {e}]", parse_mode="HTML")

        await message.answer("<b>Подтвердите настройки запуска:</b>", reply_markup=get_confirm_start_keyboard(), parse_mode="HTML")

    @router.message(F.text == "🔙 Back")
    async def on_back(message: Message, state: FSMContext):
        await state.clear()
        status = "⏸️ Paused" if bot_core.is_paused else "▶️ Running"
        await message.answer(f"<b>Control Panel</b>\nCurrent Status: {status}", reply_markup=get_main_keyboard(), parse_mode="HTML")

    @router.message(F.text == "✅ Confirm Start")
    async def on_confirm_start(message: Message, state: FSMContext):
        await state.clear()
        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path) or {}
        if "app" not in app_data:
            app_data["app"] = {}
        app_data["app"]["auto_start"] = True
        Utils.write_json_file(app_json_path, app_data)

        if not bot_core.is_paused:
            await message.answer("Trading is already running!", reply_markup=get_main_keyboard())
            return
        bot_core.is_paused = False
        logger.info("[TG] User started trading loops. auto_start flag saved to True.")
        await message.answer("<b>Control Panel</b>\nCurrent Status: ▶️ Running", reply_markup=get_main_keyboard(), parse_mode="HTML")

    @router.message(F.text == "⏸️ Stop")
    async def on_stop_trade(message: Message, state: FSMContext):
        await state.clear()
        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path) or {}
        if "app" not in app_data:
            app_data["app"] = {}
        app_data["app"]["auto_start"] = False
        Utils.write_json_file(app_json_path, app_data)

        if bot_core.is_paused:
            await message.answer("Trading is already paused!", reply_markup=get_main_keyboard())
            return
        bot_core.is_paused = True
        logger.info("[TG] User stopped trading loops. auto_start flag saved to False.")
        await message.answer("<b>Control Panel</b>\nCurrent Status: ⏸️ Paused", reply_markup=get_main_keyboard(), parse_mode="HTML")

    @router.message(F.text == "ℹ️ Status")
    async def on_status(message: Message, state: FSMContext):
        await state.clear()
        status = "⏸️ Paused" if bot_core.is_paused else "▶️ Running"

        strat_info = []
        if hasattr(bot_core, "strategy_registry"):
            reg = bot_core.strategy_registry
            strats = reg.get_active_strategies() if hasattr(reg, "get_active_strategies") else getattr(reg, "active_strategies", {})
            for sid, scfg in strats.items():
                mode_str = getattr(scfg, "mode", getattr(scfg, "auto_closing_mode", "off"))
                if mode_str == "fixed":
                    pos_ac = getattr(scfg, "auto_closing_cfg", {}).get("positive", {})
                    f_target = pos_ac.get("fixed_net", {}).get("target_net_usdt")
                    badge = f"[FIXED ${float(f_target):g}]" if f_target else "[FIXED]"
                elif mode_str == "classic":
                    badge = "[CLASSIC TRAILING]"
                elif mode_str == "toxic":
                    badge = "[TOXIC ADAPTIVE]"
                elif mode_str == "off":
                    badge = "[OFF]"
                else:
                    badge = f"[{mode_str.upper()}]"
                strat_info.append(f"• <b>{scfg.name}</b> <code>{badge}</code>: <code>{', '.join(scfg.symbols)}</code>")
        strat_block = "\n".join(strat_info) if strat_info else "<i>Нет активных стратегий</i>"

        vel_text = ""
        if hasattr(bot_core, "analytics") and hasattr(bot_core.analytics, "velocity_engine"):
            try:
                data = bot_core.analytics._read_data()
                vel_data = bot_core.analytics.velocity_engine.calculate_velocity(data)
                eff_speed = float(vel_data.get("avg_effective_speed") or 0.0)
                speed_ratio = float(vel_data.get("speed_ratio") or 1.0)
                status_desc = str(vel_data.get("status_desc") or "NORMAL")
                vel_text = f"\n\n⚡ <b>Trade Velocity</b>: <b>{eff_speed:.2f} сд/ч</b> (x{speed_ratio:.2f} | {status_desc})"
            except Exception as e:
                logger.error(f"Error calculating velocity for status: {e}")

        text = (
            f"<b>Control Panel</b>\n"
            f"Статус бота: <b>{status}</b>\n\n"
            f"🌌 <b>Купола стратегий:</b>\n{strat_block}"
            f"{vel_text}"
        )
        await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="HTML")

    @router.message(F.text == "📜 Logs")
    async def on_logs_menu(message: Message, state: FSMContext):
        await state.clear()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📜 Get Logs", callback_data="logs_get_logs")],
            [InlineKeyboardButton(text="📦 Get Runtime Backup", callback_data="logs_get_runtime_backup")],
        ])
        await message.answer("Выберите нужный лог или бэкап рантайма:", reply_markup=keyboard)

    @router.callback_query(F.data == "logs_get_logs")
    async def on_get_logs(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.answer()
        log_path = os.path.join("logs", "all.log")
        if os.path.exists(log_path):
            await callback.message.answer_document(FSInputFile(log_path))
        else:
            await callback.message.answer("Global log file not found.")

    @router.callback_query(F.data == "logs_get_runtime_backup")
    async def on_get_runtime_backup(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.answer("⏳ Создание бэкапа рантайма...")
        try:
            from CORE.SERVICES.runtime_backup_service import RuntimeBackupService
            res = await RuntimeBackupService.export_and_send(bot_core, chat_ids=[callback.message.chat.id])
            p = res.get("file")
            if not res.get("telegram_sent") and p and os.path.exists(p):
                await callback.message.answer_document(
                    FSInputFile(p),
                    caption=f"📦 <b>Рантайм-бэкап позиций</b>\nФайл: <code>{res.get('filename')}</code>\nРазмер: {res.get('size_bytes', 0) / 1024.0:.1f} KB",
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.error(f"Error sending runtime backup: {e}")
            await callback.message.answer(f"❌ Ошибка бэкапа: {e}")

    return router
