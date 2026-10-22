# ==============================================================================
# Path: TG/HANDLERS/analytics_router.py
# Role: Роутер аналитики Telegram (Dumb Executor: делегирует отчеты и графики в ANALYTICS)
# ==============================================================================

import html
import os
from pathlib import Path
from typing import Any, List, Optional

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from consts import ANALYTICS_DIR, ANALYTICS_LEDGERS_DIR, ANALYTICS_STATS_DIR
from c_log import UnifiedLogger
from TG.keyboards import get_back_keyboard, get_main_keyboard
from TG.states import TGStates
from TG.universe_commands import get_user_universe_id, set_user_universe_id

logger = UnifiedLogger("TGAnalyticsRouter")


def _buttons_to_markup(button_rows: List[List[dict]]) -> InlineKeyboardMarkup:
    """Конвертирует список словарей кнопок в InlineKeyboardMarkup."""
    rows = []
    for r in button_rows:
        rows.append([InlineKeyboardButton(text=b["text"], callback_data=b["callback_data"]) for b in r])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_analytics_router(bot_core: Any) -> Router:
    """Фабрика роутера аналитики (Dumb Executor)."""
    router = Router(name="analytics_router")

    def _get_universe_meta(user_id: int, target_uid: Optional[str] = None):
        current_uid = target_uid or get_user_universe_id(user_id, bot_core)
        univ_name = current_uid
        if hasattr(bot_core, "strategy_registry"):
            reg = bot_core.strategy_registry
            strat = reg.get_strategy(current_uid) if hasattr(reg, "get_strategy") else None
            if strat and getattr(strat, "name", None):
                univ_name = strat.name
        return current_uid, univ_name

    @router.message(Command("analytics"))
    @router.message(F.text == "📊 Analytics")
    async def cmd_analytics(message: Message, state: FSMContext):
        await state.clear()
        current_uid, univ_name = _get_universe_meta(message.from_user.id)
        report_svc = getattr(getattr(bot_core, "analytics", None), "report_service", None)
        if not report_svc:
            from ANALYTICS.report_service import AnalyticsReportService
            report_svc = AnalyticsReportService()

        text, buttons = report_svc.render_universe_analytics_card(current_uid, univ_name)
        await message.answer(text, reply_markup=_buttons_to_markup(buttons), parse_mode="HTML")

    @router.callback_query(F.data.startswith("analytics_dome:") | (F.data == "analytics_dome") | (F.data == "analytics_back_to_univ"))
    async def cb_analytics_dome(callback: CallbackQuery):
        await callback.answer()
        target_uid = None
        if callback.data.startswith("analytics_dome:"):
            target_uid = callback.data.split("analytics_dome:")[1]
            set_user_universe_id(callback.from_user.id, target_uid)

        current_uid, univ_name = _get_universe_meta(callback.from_user.id, target_uid=target_uid)
        report_svc = getattr(getattr(bot_core, "analytics", None), "report_service", None)
        if not report_svc:
            from ANALYTICS.report_service import AnalyticsReportService
            report_svc = AnalyticsReportService()

        text, buttons = report_svc.render_universe_analytics_card(current_uid, univ_name)
        try:
            await callback.message.edit_text(text, reply_markup=_buttons_to_markup(buttons), parse_mode="HTML")
        except Exception:
            await callback.message.answer(text, reply_markup=_buttons_to_markup(buttons), parse_mode="HTML")

    @router.callback_query(F.data == "analytics_bb_global")
    async def cb_analytics_bb_global(callback: CallbackQuery):
        await callback.answer()
        current_uid, univ_name = _get_universe_meta(callback.from_user.id)
        report_svc = getattr(getattr(bot_core, "analytics", None), "report_service", None)
        if not report_svc:
            from ANALYTICS.report_service import AnalyticsReportService
            report_svc = AnalyticsReportService()

        active_strats = {}
        if hasattr(bot_core, "strategy_registry"):
            reg = bot_core.strategy_registry
            active_strats = reg.get_active_strategies() if hasattr(reg, "get_active_strategies") else getattr(reg, "active_strategies", {})

        text, buttons = report_svc.render_portfolio_big_board(current_uid, univ_name, active_strats=active_strats)
        try:
            await callback.message.edit_text(text, reply_markup=_buttons_to_markup(buttons), parse_mode="HTML")
        except Exception:
            await callback.message.answer(text, reply_markup=_buttons_to_markup(buttons), parse_mode="HTML")

    @router.callback_query(F.data.startswith("analytics_chart"))
    async def cb_analytics_chart(callback: CallbackQuery):
        await callback.answer()
        parts = callback.data.split(":", 1)
        target_uid = parts[1] if len(parts) > 1 and parts[1] else None
        current_uid, univ_name = _get_universe_meta(callback.from_user.id, target_uid=target_uid)
        set_user_universe_id(callback.from_user.id, current_uid)

        msg = await callback.message.answer(f"⏳ Генерация графика Net Profit для <code>{current_uid}</code>...", parse_mode="HTML")
        report_svc = getattr(getattr(bot_core, "analytics", None), "report_service", None)
        if not report_svc:
            from ANALYTICS.report_service import AnalyticsReportService
            report_svc = AnalyticsReportService()

        chart_path = report_svc.get_equity_chart_path(universe_id=current_uid)
        if chart_path and chart_path.exists():
            await msg.delete()
            await callback.message.answer_photo(
                FSInputFile(str(chart_path)),
                caption=f"📈 <b>Кривая чистого Net Profit & Эквити</b>: <b>{univ_name}</b> (<code>{current_uid}</code>)",
                parse_mode="HTML",
            )
        else:
            await msg.edit_text(f"❌ Не удалось построить график эквити для {current_uid}.")

    @router.callback_query(F.data.startswith("analytics_txt"))
    async def cb_analytics_txt(callback: CallbackQuery):
        await callback.answer()
        parts = callback.data.split(":", 1)
        target_uid = parts[1] if len(parts) > 1 and parts[1] else None
        current_uid, _ = _get_universe_meta(callback.from_user.id, target_uid=target_uid)

        report_svc = getattr(getattr(bot_core, "analytics", None), "report_service", None)
        if not report_svc:
            from ANALYTICS.report_service import AnalyticsReportService
            report_svc = AnalyticsReportService()

        ledger_path = report_svc.get_ledger_file_path(universe_id=current_uid)
        if ledger_path and ledger_path.exists():
            await callback.message.answer_document(
                FSInputFile(str(ledger_path)),
                caption=f"📝 <b>Лента сделок (Trades Ledger)</b>: <code>{current_uid}</code>",
                parse_mode="HTML",
            )
        else:
            await callback.message.answer(f"Trades ledger TXT для купола <code>{current_uid}</code> не найден или пуст.", parse_mode="HTML")

    @router.callback_query(F.data.startswith("analytics_full_report"))
    async def cb_analytics_full_report(callback: CallbackQuery):
        await callback.answer()
        parts = callback.data.split(":", 1)
        target_uid = parts[1] if len(parts) > 1 and parts[1] else None
        current_uid, univ_name = _get_universe_meta(callback.from_user.id, target_uid=target_uid)

        report_svc = getattr(getattr(bot_core, "analytics", None), "report_service", None)
        if not report_svc:
            from ANALYTICS.report_service import AnalyticsReportService
            report_svc = AnalyticsReportService()

        full_text = report_svc.render_full_report_text(current_uid, univ_name)
        report_file = ANALYTICS_STATS_DIR / f"full_report_{current_uid}.txt"
        report_file.write_text(full_text, encoding="utf-8")
        await callback.message.answer_document(
            FSInputFile(str(report_file)),
            caption=f"📄 <b>Полный аналитический отчет</b>: <code>{current_uid}</code>",
            parse_mode="HTML",
        )

    @router.message(Command("leaderboard"))
    @router.message(F.text == "🏆 Leaderboard")
    @router.callback_query(F.data == "analytics_bb_leaderboard")
    @router.callback_query(F.data == "analytics_leaderboard")
    async def show_leaderboard(event: Any):
        is_cb = isinstance(event, CallbackQuery)
        if is_cb:
            await event.answer()
        target_msg = event.message if is_cb else event

        engine = getattr(getattr(bot_core, "analytics", None), "leaderboard_engine", None)
        if not engine:
            from ANALYTICS.leaderboard import LeaderboardEngine
            engine = LeaderboardEngine(analytics_dir=ANALYTICS_DIR, stats_dir=ANALYTICS_STATS_DIR)

        active_strats = {}
        if hasattr(bot_core, "strategy_registry"):
            reg = bot_core.strategy_registry
            active_strats = reg.get_active_strategies() if hasattr(reg, "get_active_strategies") else getattr(reg, "active_strategies", {})

        universes_meta = {
            sid: {**getattr(scfg, "raw_meta", {}), "name": getattr(scfg, "name", sid), "enabled": True}
            for sid, scfg in active_strats.items()
        }
        entries = engine.generate_leaderboard(universes_meta)
        evaluated_coins = len(getattr(bot_core.strategy_registry, "get_all_active_symbols", lambda: [])()) if hasattr(bot_core, "strategy_registry") else 0
        chunks = engine.format_table_chunks(entries, evaluated_coins_count=evaluated_coins, chunk_size=8, top_limit=32)

        for idx, chunk in enumerate(chunks):
            kb = None
            if idx == len(chunks) - 1:
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📄 Скачать leaderboard.txt", callback_data="download_leaderboard_txt")],
                    [InlineKeyboardButton(text="🔙 Назад к аналитике", callback_data="analytics_dome")],
                ])
            await target_msg.answer(f"```text\n{chunk}\n```", parse_mode="Markdown", reply_markup=kb)

    @router.callback_query(F.data == "download_leaderboard_txt")
    async def cb_download_leaderboard(callback: CallbackQuery):
        await callback.answer()
        lb_file = ANALYTICS_DIR / "leaderboard.txt"
        if lb_file.exists():
            await callback.message.answer_document(FSInputFile(str(lb_file)), caption="🏆 <b>Leaderboard TXT</b>", parse_mode="HTML")
        else:
            await callback.message.answer("Leaderboard file not found.")

    @router.callback_query(F.data.startswith("analytics_ranking") | F.data.startswith("arank:"))
    async def cb_analytics_ranking(callback: CallbackQuery):
        await callback.answer()
        parts = callback.data.split(":")
        criterion = parts[1] if len(parts) > 1 and parts[1] else "net"
        target_uid = parts[2] if len(parts) > 2 and parts[2] else None
        is_switch = len(parts) > 3 and parts[3] in ("switch", "s")
        current_uid, univ_name = _get_universe_meta(callback.from_user.id, target_uid=target_uid)
        set_user_universe_id(callback.from_user.id, current_uid)

        report_svc = getattr(getattr(bot_core, "analytics", None), "report_service", None)
        if not report_svc:
            from ANALYTICS.report_service import AnalyticsReportService
            report_svc = AnalyticsReportService()

        active_symbols = []
        if hasattr(bot_core, "strategy_registry") and bot_core.strategy_registry:
            active_strats = bot_core.strategy_registry.get_active_strategies()
            if current_uid in active_strats:
                active_symbols = list(getattr(active_strats[current_uid], "symbols", []))

        msgs, buttons = report_svc.render_ranking_report(current_uid, univ_name, criterion=criterion, active_symbols=active_symbols)
        switch_kb = _buttons_to_markup(buttons) if buttons else None

        if is_switch and msgs:
            try:
                await callback.message.edit_text(msgs[0], parse_mode="HTML", reply_markup=switch_kb)
                return
            except Exception:
                pass

        for i, text in enumerate(msgs):
            rm = switch_kb if i == len(msgs) - 1 else None
            await callback.message.answer(text, parse_mode="HTML", reply_markup=rm)

    @router.callback_query(F.data == "delete_ranking_msg")
    async def cb_delete_ranking(callback: CallbackQuery):
        await callback.answer()
        try:
            await callback.message.delete()
        except Exception:
            pass

    @router.callback_query(F.data.startswith("analytics_coin_menu"))
    async def cb_analytics_coin_menu(callback: CallbackQuery):
        await callback.answer()
        parts = callback.data.split(":", 1)
        target_uid = parts[1] if len(parts) > 1 and parts[1] else None
        current_uid, univ_name = _get_universe_meta(callback.from_user.id, target_uid=target_uid)
        set_user_universe_id(callback.from_user.id, current_uid)

        report_svc = getattr(getattr(bot_core, "analytics", None), "report_service", None)
        if not report_svc:
            from ANALYTICS.report_service import AnalyticsReportService
            report_svc = AnalyticsReportService()

        active_symbols = []
        if hasattr(bot_core, "strategy_registry") and bot_core.strategy_registry:
            active_strats = bot_core.strategy_registry.get_active_strategies()
            if current_uid in active_strats:
                active_symbols = list(getattr(active_strats[current_uid], "symbols", []))

        text, buttons = report_svc.render_coin_menu(current_uid, univ_name, active_symbols)
        kb = _buttons_to_markup(buttons) if buttons else None
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")

    @router.callback_query(F.data.startswith("analytics_coin:") | (F.data.startswith("analytics_coin_") & ~F.data.startswith("analytics_coin_menu")))
    async def cb_analytics_coin_detail(callback: CallbackQuery):
        await callback.answer()
        if callback.data.startswith("analytics_coin:"):
            c_parts = callback.data.split(":")
            symbol = c_parts[1]
            target_uid = c_parts[2] if len(c_parts) > 2 and c_parts[2] else None
        else:
            symbol = callback.data.replace("analytics_coin_", "")
            target_uid = None

        current_uid, univ_name = _get_universe_meta(callback.from_user.id, target_uid=target_uid)
        set_user_universe_id(callback.from_user.id, current_uid)

        report_svc = getattr(getattr(bot_core, "analytics", None), "report_service", None)
        if not report_svc:
            from ANALYTICS.report_service import AnalyticsReportService
            report_svc = AnalyticsReportService()

        is_active = symbol in getattr(bot_core, "symbols", [])
        detail_msg = report_svc.render_coin_detail_report(current_uid, univ_name, symbol, is_active)
        if detail_msg:
            chart_path = report_svc.get_coin_chart_path(symbol=symbol, universe_id=current_uid)
            if chart_path and chart_path.exists():
                await callback.message.answer_photo(FSInputFile(str(chart_path)), caption=detail_msg, parse_mode="HTML")
            else:
                await callback.message.answer(detail_msg, parse_mode="HTML")
        else:
            await callback.message.answer(f"⚠️ Нет данных аналитики для монеты <b>{symbol}</b> в куполе <code>{current_uid}</code>.", parse_mode="HTML")

    @router.callback_query(F.data == "analytics_cheat_sheet")
    async def cb_analytics_cheat_sheet(callback: CallbackQuery):
        await callback.answer()
        report_svc = getattr(getattr(bot_core, "analytics", None), "report_service", None)
        if not report_svc:
            from ANALYTICS.report_service import AnalyticsReportService
            report_svc = AnalyticsReportService()
        sheet_text = report_svc.render_cheat_sheet()
        await callback.message.answer(sheet_text, parse_mode="HTML")

    @router.callback_query(F.data.startswith("strat_ledger:"))
    async def cb_strat_ledger(callback: CallbackQuery):
        await callback.answer()
        strat_id = callback.data.split("strat_ledger:")[1]
        report_svc = getattr(getattr(bot_core, "analytics", None), "report_service", None)
        if not report_svc:
            from ANALYTICS.report_service import AnalyticsReportService
            report_svc = AnalyticsReportService()
        cand = report_svc.get_ledger_file_path(universe_id=strat_id)
        if cand and cand.exists():
            await callback.message.answer_document(FSInputFile(str(cand)), caption=f"📝 Леджер для {strat_id}", parse_mode="HTML")
        else:
            await callback.message.answer(f"Леджер для {strat_id} пока пуст.")

    @router.callback_query(F.data.startswith("analytics_reset"))
    async def cb_analytics_reset(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.answer()
        parts = callback.data.split(":", 1)
        target_uid = parts[1] if len(parts) > 1 and parts[1] else None
        current_uid, univ_name = _get_universe_meta(callback.from_user.id, target_uid=target_uid)
        set_user_universe_id(callback.from_user.id, current_uid)
        await state.update_data(target_dome_uid=current_uid)
        clean_name = html.escape(univ_name)
        await callback.message.answer(
            f"⚠️ Вы уверены, что хотите полностью удалить историю аналитики для купола <b>{clean_name}</b> (<code>{current_uid}</code>)?\n\n"
            f"Введите слово <b>СБРОС</b> для подтверждения или нажмите Back для отмены.",
            reply_markup=get_back_keyboard(),
            parse_mode="HTML",
        )
        await state.set_state(TGStates.waiting_for_reset_confirm)

    @router.message(TGStates.waiting_for_reset_confirm)
    async def process_reset_confirm(message: Message, state: FSMContext):
        if message.text == "🔙 Back":
            await state.clear()
            await message.answer("Сброс аналитики отменен.", reply_markup=get_main_keyboard())
            return

        if message.text.strip().upper() == "СБРОС":
            st_data = await state.get_data()
            target_uid = st_data.get("target_dome_uid") or get_user_universe_id(message.from_user.id, bot_core)
            await state.clear()
            deleted_any = False
            for p in [ANALYTICS_STATS_DIR / f"analytics_{target_uid}.json", ANALYTICS_LEDGERS_DIR / f"trades_ledger_{target_uid}.txt"]:
                if p.exists():
                    os.remove(p)
                    deleted_any = True
            if deleted_any:
                await message.answer(
                    f"✅ Файлы аналитики для купола <code>{target_uid}</code> успешно удалены.",
                    reply_markup=get_main_keyboard(),
                    parse_mode="HTML",
                )
            else:
                await message.answer(f"⚠️ Файлы аналитики для купола <code>{target_uid}</code> не найдены.", reply_markup=get_main_keyboard(), parse_mode="HTML")
        else:
            await message.answer("❌ Неверное слово подтверждения. Введите <b>СБРОС</b> или нажмите Back.", parse_mode="HTML")

    @router.callback_query(F.data.startswith("analytics_set_balance"))
    async def cb_analytics_set_balance(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.answer()
        parts = callback.data.split(":", 1)
        target_uid = parts[1] if len(parts) > 1 and parts[1] else None
        current_uid, univ_name = _get_universe_meta(callback.from_user.id, target_uid=target_uid)
        set_user_universe_id(callback.from_user.id, current_uid)
        await state.update_data(target_dome_uid=current_uid)
        clean_name = html.escape(univ_name)
        await callback.message.answer(
            f"Введите новый начальный баланс (start_balance_usdt) для купола <b>{clean_name}</b> (<code>{current_uid}</code>) в USDT (например, 100.0):",
            reply_markup=get_back_keyboard(),
            parse_mode="HTML",
        )
        await state.set_state(TGStates.waiting_for_initial_balance)

    @router.message(TGStates.waiting_for_initial_balance)
    async def process_initial_balance(message: Message, state: FSMContext):
        if message.text == "🔙 Back":
            await state.clear()
            await message.answer("Действие отменено.", reply_markup=get_main_keyboard())
            return
        try:
            new_balance = float(message.text.replace(",", "."))
            if new_balance < 0:
                raise ValueError("Баланс не может быть отрицательным.")
        except ValueError:
            await message.answer("❌ Некорректное число. Введите баланс еще раз (например, 100.0) или нажмите Back:")
            return

        st_data = await state.get_data()
        target_uid = st_data.get("target_dome_uid") or get_user_universe_id(message.from_user.id, bot_core)
        await state.clear()

        if hasattr(bot_core, "analytics") and hasattr(bot_core.analytics, "rebase_start_balance"):
            try:
                bot_core.analytics.rebase_start_balance(new_balance)
            except Exception as e:
                logger.error(f"[TG] Error rebasing balance: {e}")

        await message.answer(f"✅ Начальный баланс для <code>{target_uid}</code> установлен: <b>{new_balance:.2f} USDT</b>", reply_markup=get_main_keyboard(), parse_mode="HTML")

    return router
