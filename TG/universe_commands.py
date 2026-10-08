# ============================================================
# FILE: TG/universe_commands.py
# ROLE: Telegram commands and navigation for Multi-Universe Engine (/leaderboard, /universes, /use)
# ============================================================

from typing import Any, Tuple
from aiogram import Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from c_log import UnifiedLogger

logger = UnifiedLogger("TGUniverseCommands")

# Глобальный контекст выбранной вселенной для каждого пользователя: {user_id: universe_id}
_active_user_universe = {}


def get_user_universe_id(user_id: int, bot_core: Any = None) -> str:
    """Возвращает текущую выбранную вселенную пользователя."""
    uid = _active_user_universe.get(user_id)
    if uid:
        return uid
    if bot_core and hasattr(bot_core, "strategy_registry") and bot_core.strategy_registry:
        try:
            active = bot_core.strategy_registry.get_active_strategies() if callable(getattr(bot_core.strategy_registry, "get_active_strategies", None)) else getattr(bot_core.strategy_registry, "active_strategies", {})
            if active:
                first_uid = next(iter(active.keys()), None)
                if first_uid:
                    _active_user_universe[user_id] = first_uid
                    return first_uid
        except Exception:
            pass
    return "u96_omni_zenith_apex"


def set_user_universe_id(user_id: int, universe_id: str) -> None:
    """Устанавливает активную вселенную для пользователя."""
    _active_user_universe[user_id] = universe_id


PAGE_SIZE = 6


def render_universes_page(user_id: int, bot_core: Any, page: int = 1) -> Tuple[str, InlineKeyboardMarkup]:
    """Рендерит страницу со списком вселенных и инлайн-кнопками выбора."""
    current_uid = get_user_universe_id(user_id, bot_core)

    active_strats = {}
    if hasattr(bot_core, "strategy_registry") and bot_core.strategy_registry:
        active_strats = bot_core.strategy_registry.get_active_strategies()

    engine = getattr(getattr(bot_core, "analytics", None), "leaderboard_engine", None)
    universes_meta = {
        sid: {**getattr(scfg, "raw_meta", {}), "name": scfg.name, "enabled": True}
        for sid, scfg in active_strats.items()
    }
    entries = engine.generate_leaderboard(universes_meta) if engine else []

    if not entries:
        entries = [{"universe_id": sid, "name": scfg.name, "net_profit_usdt": 0.0, "realized_pnl_usdt": 0.0, "unrealized_pnl_usdt": 0.0, "max_drawdown_usdt": 0.0, "total_trades": 0} for sid, scfg in active_strats.items()]

    total_entries = len(entries)
    total_pages = max(1, (total_entries + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))

    start_i = (page - 1) * PAGE_SIZE
    end_i = min(start_i + PAGE_SIZE, total_entries)
    page_entries = entries[start_i:end_i]

    text_lines = [
        f"🌌 <b>СПИСОК ВСЕЛЕННЫХ (Стр. {page}/{total_pages})</b>",
        f"Ранжирование по чистому Net Profit",
        f"Текущий контекст: <code>{current_uid}</code>\n",
    ]

    buttons = []
    row_buttons = []
    for item in page_entries:
        uid = item["universe_id"]
        mark = "👉 " if uid == current_uid else ""
        net_str = f"{item.get('net_profit_usdt', 0.0):+.2f}$"
        real_str = f"{item.get('realized_pnl_usdt', 0.0):+.1f}$"
        float_str = f"{item.get('unrealized_pnl_usdt', 0.0):+.1f}$"
        text_lines.append(
            f"{mark}<code>{uid}</code>: <b>Net: {net_str}</b> (Real: {real_str}, Float: {float_str}) | Tr: <b>{item.get('total_trades', 0)}</b>"
        )
        btn_label = f"👉 {uid}" if uid == current_uid else uid
        row_buttons.append(InlineKeyboardButton(text=btn_label[:30], callback_data=f"use_univ:{uid}:{page}"))
        if len(row_buttons) == 2:
            buttons.append(row_buttons)
            row_buttons = []

    if row_buttons:
        buttons.append(row_buttons)

    # Строка навигации пагинации
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"univ_page:{page - 1}"))
    nav_row.append(InlineKeyboardButton(text=f"📄 {page}/{total_pages}", callback_data="univ_page:noop"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"univ_page:{page + 1}"))

    buttons.append(nav_row)
    buttons.append([InlineKeyboardButton(text="🔙 Назад к аналитике", callback_data="analytics_back_to_univ")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    return "\n".join(text_lines), kb


def setup_universe_handlers(dp: Dispatcher, bot_core: Any) -> None:
    """Регистрирует пагинацию и смену вселенных в aiogram Dispatcher."""

    @dp.callback_query(F.data.startswith("univ_page:"))
    async def cb_universe_page(callback: CallbackQuery):
        target = callback.data.split("univ_page:")[1]
        if target == "noop":
            await callback.answer()
            return
        try:
            page = int(target)
            text, kb = render_universes_page(callback.from_user.id, bot_core, page=page)
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                logger.error(f"Error in cb_universe_page: {e}")
        await callback.answer()

    @dp.callback_query(F.data.startswith("use_univ:"))
    async def cb_select_universe(callback: CallbackQuery):
        parts = callback.data.split(":")
        uid = parts[1]
        current_page = int(parts[2]) if len(parts) > 2 else 1
        set_user_universe_id(callback.from_user.id, uid)

        try:
            text, kb = render_universes_page(callback.from_user.id, bot_core, page=current_page)
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass

        alert_msg = (
            f"✅ Контекст переключен на вселенную {uid}.\n"
            f"Теперь детальная аналитика и метрики относятся к этой стратегии."
        )
        await callback.answer(alert_msg, show_alert=True)
