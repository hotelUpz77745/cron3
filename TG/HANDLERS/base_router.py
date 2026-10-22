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
    from quant_calculator import QuantCalculator
except ImportError:
    QuantCalculator = None


class BaseRouterHandler:
    """Класс-обработчик базовых команд и управления жизненным циклом бота."""

    def __init__(self, bot_core: Any, template_manager: Any = None):
        self.bot_core = bot_core
        self.template_manager = template_manager
        self.router = Router(name="base_router")
        self._register_routes()

    def _register_routes(self) -> None:
        """Регистрация всех маршрутов aiogram 3 в роутере."""
        self.router.errors()(self.global_error_handler)
        self.router.message(Command("start"))(self.start_cmd)
        self.router.message(Command("quant"))(self.secret_quant_cmd)
        self.router.message(Command("sonnik_restore"))(self.sonnik_restore_cmd)
        self.router.message(Command("radar"))(self.radar_cmd)
        self.router.message(Command("backup"))(self.backup_cmd)
        self.router.callback_query(F.data.startswith("rot_accept:"))(self.on_rot_accept)
        self.router.callback_query(F.data.startswith("rot_reject:"))(self.on_rot_reject)
        self.router.message(F.text == "▶️ Start")(self.on_pre_start)
        self.router.message(F.text == "🔙 Back")(self.on_back)
        self.router.message(F.text == "✅ Confirm Start")(self.on_confirm_start)
        self.router.message(F.text == "⏸️ Stop")(self.on_stop_trade)
        self.router.message(F.text == "ℹ️ Status")(self.on_status)
        self.router.message(F.text == "📜 Logs")(self.on_logs_menu)
        self.router.callback_query(F.data == "logs_get_logs")(self.on_get_logs)
        self.router.callback_query(F.data == "logs_get_runtime_backup")(self.on_get_runtime_backup)

    async def global_error_handler(self, event: ErrorEvent):
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

    async def start_cmd(self, message: Message, state: FSMContext):
        await state.clear()
        status = "⏸️ Paused" if self.bot_core.is_paused else "▶️ Running"
        text = f"<b>Control Panel</b>\nCurrent Status: {status}"
        await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="HTML")

    async def secret_quant_cmd(self, message: Message, state: FSMContext):
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

    async def sonnik_restore_cmd(self, message: Message):
        msg = await message.answer("⏳ Запуск принудительного Deep Sync. Это займет некоторое время...")
        try:
            await self.bot_core.analytics.deep_sync_analytics(self.bot_core.client, self.bot_core.prices)
            await msg.edit_text("✅ Принудительный Deep Sync успешно завершен! Исторические данные восстановлены по первой строке леджера.")
        except Exception as e:
            logger.error(f"Failed to execute manual deep sync: {e}")
            await msg.edit_text(f"❌ Ошибка во время Deep Sync: {e}")

    async def radar_cmd(self, message: Message):
        nav = getattr(self.bot_core, "radar_navigator", None)
        if not nav or not nav.is_enabled():
            await message.answer("ℹ️ Штурман / Синергия с Радаром отключена или не инициализирована.")
            return
        report = nav.generate_telemetry_report()
        await message.answer(report, parse_mode="HTML")

    async def backup_cmd(self, message: Message):
        try:
            from CORE.SERVICES.runtime_backup_service import RuntimeBackupService
            status_msg = await message.answer("⏳ Создание рантайм-бэкапа позиций (.pkl ➔ JSON)...")
            res = await RuntimeBackupService.export_and_send(self.bot_core, chat_ids=[message.chat.id])
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

    async def on_rot_accept(self, callback: CallbackQuery):
        await callback.answer()
        req_id = callback.data.split(":", 1)[1]
        nav = getattr(self.bot_core, "radar_navigator", None)
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

    async def on_rot_reject(self, callback: CallbackQuery):
        await callback.answer()
        req_id = callback.data.split(":", 1)[1]
        nav = getattr(self.bot_core, "radar_navigator", None)
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

    async def on_pre_start(self, message: Message, state: FSMContext):
        await state.clear()
        if not self.bot_core.is_paused:
            await message.answer("⚠️ Trading is already running!", reply_markup=get_main_keyboard())
            return

        # 1. Автоматическое разрешение TemplateManager
        tm = self.template_manager or getattr(self.bot_core, "template_manager", None)
        if not tm:
            try:
                from TG.template_manager import TemplateManager
                tm = TemplateManager(bot_core=self.bot_core)
            except Exception as ex:
                logger.warning(f"Could not initialize TemplateManager fallback: {ex}")

        # 2. Разрешение всех активных символов в системе
        symbols = []
        if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
            symbols = self.bot_core.strategy_registry.get_all_active_symbols()
        if not symbols:
            symbols = getattr(self.bot_core, "symbols", None) or []
        if not symbols:
            app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
            for sdata in (app_data.get("active_strategies") or {}).values():
                if sdata.get("enabled", True):
                    for src in [
                        (sdata.get("LONG") or {}).get("invest_size"),
                        (sdata.get("SHORT") or {}).get("invest_size"),
                        sdata.get("invest_size"),
                        sdata.get("symbols"),
                    ]:
                        if isinstance(src, dict):
                            symbols.extend(src.keys())
                        elif isinstance(src, list):
                            symbols.extend(src)
        if not symbols:
            from consts import ACTIVE_SYMBOLS
            symbols = ACTIVE_SYMBOLS
        symbols = sorted(list(set(s.upper().strip() for s in symbols if s)))

        found_any = False
        for sym in symbols:
            runtime_path = tm.get_runtime_path(sym) if tm else None
            if not runtime_path or not runtime_path.exists():
                rt_dir = DATA_DIR / "runtime"
                sym_lower = sym.lower()
                for p in rt_dir.glob(f"**/{sym_lower}.json"):
                    runtime_path = p
                    break
                if not runtime_path or not runtime_path.exists():
                    for p in rt_dir.glob(f"**/{sym_lower}.pkl"):
                        runtime_path = p
                        break

            if runtime_path and runtime_path.exists():
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
                        cfg = data.get(side)
                        if not cfg or not isinstance(cfg, dict):
                            continue
                        en = cfg.get("enable", cfg.get("enabled", True))
                        sz = cfg.get("invest_size", cfg.get("default_invest_size", 0.0))
                        lev = cfg.get("leverage", 20)
                        margin = cfg.get("margin_type", "CROSSED")

                        lines.append(f"  <b>{side}</b>: {'✅ On' if en else '❌ Off'}")
                        if en:
                            lines.append(f"    ├ Invest: {sz}$ | Lev: {lev}x | {margin}")
                            grid = cfg.get("grid") or {}
                            indents = []
                            for k in sorted(grid.keys(), key=lambda x: int(x) if x.isdigit() else x):
                                lvl_d = grid[k]
                                if isinstance(lvl_d, dict) and (lvl_d.get("enabled", True) is False or lvl_d.get("indent") is None):
                                    indents.append("off")
                                else:
                                    indents.append(str(lvl_d.get("indent") if isinstance(lvl_d, dict) else lvl_d))
                            lines.append(f"    ├ Grid: [{', '.join(indents)}]")

                            tp_map = cfg.get("tp_map") or {}
                            tps = []
                            for k in sorted(tp_map.keys(), key=lambda x: int(x) if x.isdigit() else x):
                                tp_d = tp_map[k]
                                raw_tp_ind = tp_d.get("indent") if isinstance(tp_d, dict) else tp_d
                                tps.append(str(raw_tp_ind) if raw_tp_ind is not None else "-")
                            lines.append(f"    └ TP: [{', '.join(tps)}]")

                    await message.answer("\n".join(lines), parse_mode="HTML")
                    found_any = True
                except Exception as e:
                    await message.answer(f"<b>{sym}</b>: [Ошибка чтения конфигурации: {e}]", parse_mode="HTML")

        if not found_any:
            app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
            active_strats = app_data.get("active_strategies", {})
            strat_lines = ["📋 <b>Активные стратегии и параметры запуска:</b>"]
            for sid, scfg in active_strats.items():
                if scfg.get("enabled", True):
                    s_name = scfg.get("_comment_alias") or scfg.get("name") or sid
                    bal = scfg.get("start_balance_usdt", 0.0)
                    ac = scfg.get("auto_closing_mode") or scfg.get("mode", "trailing_ratchet")
                    strat_lines.append(f"• <b>{s_name}</b> (<code>{sid}</code>)")
                    strat_lines.append(f"  Автоклозер: <code>{ac}</code> | Баланс: {bal}$")
            await message.answer("\n".join(strat_lines), parse_mode="HTML")

        await message.answer("<b>Подтвердите настройки запуска:</b>", reply_markup=get_confirm_start_keyboard(), parse_mode="HTML")

    async def on_back(self, message: Message, state: FSMContext):
        await state.clear()
        status = "⏸️ Paused" if self.bot_core.is_paused else "▶️ Running"
        await message.answer(f"<b>Control Panel</b>\nCurrent Status: {status}", reply_markup=get_main_keyboard(), parse_mode="HTML")

    async def on_confirm_start(self, message: Message, state: FSMContext):
        await state.clear()
        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path) or {}
        if "app" not in app_data:
            app_data["app"] = {}
        app_data["app"]["auto_start"] = True
        Utils.write_json_file(app_json_path, app_data)

        if not self.bot_core.is_paused:
            await message.answer("Trading is already running!", reply_markup=get_main_keyboard())
            return
        self.bot_core.is_paused = False
        logger.info("[TG] User started trading loops. auto_start flag saved to True.")
        await message.answer("<b>Control Panel</b>\nCurrent Status: ▶️ Running", reply_markup=get_main_keyboard(), parse_mode="HTML")

    async def on_stop_trade(self, message: Message, state: FSMContext):
        await state.clear()
        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path) or {}
        if "app" not in app_data:
            app_data["app"] = {}
        app_data["app"]["auto_start"] = False
        Utils.write_json_file(app_json_path, app_data)

        if self.bot_core.is_paused:
            await message.answer("Trading is already paused!", reply_markup=get_main_keyboard())
            return
        self.bot_core.is_paused = True
        logger.info("[TG] User stopped trading loops. auto_start flag saved to False.")
        await message.answer("<b>Control Panel</b>\nCurrent Status: ⏸️ Paused", reply_markup=get_main_keyboard(), parse_mode="HTML")

    async def on_status(self, message: Message, state: FSMContext):
        await state.clear()
        status = "⏸️ Paused" if self.bot_core.is_paused else "▶️ Running"

        strat_info = []
        if hasattr(self.bot_core, "strategy_registry"):
            reg = self.bot_core.strategy_registry
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
        if hasattr(self.bot_core, "analytics") and hasattr(self.bot_core.analytics, "velocity_engine"):
            try:
                data = self.bot_core.analytics._read_data()
                vel_data = self.bot_core.analytics.velocity_engine.calculate_velocity(data)
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

    async def on_logs_menu(self, message: Message, state: FSMContext):
        await state.clear()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📜 Get Logs", callback_data="logs_get_logs")],
            [InlineKeyboardButton(text="📦 Get Runtime Backup", callback_data="logs_get_runtime_backup")],
        ])
        await message.answer("Выберите нужный лог или бэкап рантайма:", reply_markup=keyboard)

    async def on_get_logs(self, callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.answer()
        log_path = os.path.join("logs", "all.log")
        if os.path.exists(log_path):
            await callback.message.answer_document(FSInputFile(log_path))
        else:
            await callback.message.answer("Global log file not found.")

    async def on_get_runtime_backup(self, callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.answer("⏳ Создание бэкапа рантайма...")
        try:
            from CORE.SERVICES.runtime_backup_service import RuntimeBackupService
            res = await RuntimeBackupService.export_and_send(self.bot_core, chat_ids=[callback.message.chat.id])
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


def get_base_router(bot_core: Any, template_manager: Any = None) -> Router:
    """Фабрика роутера базового управления ботом."""
    handler = BaseRouterHandler(bot_core=bot_core, template_manager=template_manager)
    return handler.router
