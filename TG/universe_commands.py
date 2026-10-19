# ==============================================================================
# Path: TG/universe_commands.py
# Role: Интерактивное управление вселенными и стратегиями через Telegram:
#       пагинация списка, детальная панель купола, переключение сайзинга
#       ("static", "dyn_inv_16", "dyn_ema_16", "dyn_ema_10") и автозакрытия.
# ==============================================================================

from pathlib import Path
from typing import Any, Tuple, Dict, List, Optional
import json

from aiogram import Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from c_log import UnifiedLogger
from c_utils import Utils
from consts import DATA_DIR

logger = UnifiedLogger("TGUniverseCommands")

# Глобальный контекст выбранной вселенной для каждого пользователя: {user_id: universe_id}
_active_user_universe: Dict[int, str] = {}

SIZING_MODE_BADGES = {
    "static": ("⚡ Static (50/50)", "Фиксированный сайзинг 50/50 (макро-тренд выключен)"),
    "dyn_inv_16": ("🎯 Dyn Inv 16", "Bollinger Inverted Lockout 4h (шкала 16, коридор 33.3%..66.7%)"),
    "dyn_ema_16": ("📈 Dyn EMA 16", "EMA Trend 4h 10/30 (шкала 16, коридор 33.3%..66.7%)"),
    "dyn_ema_10": ("📊 Dyn EMA 10", "EMA Trend 4h 10/30 (мягкая шкала 10, коридор 40.0%..60.0%)"),
}

CLOSER_MODE_BADGES = {
    "classic": ("🎯 Classic", "Фибоначчи кумулятивный трейлинг-тейк"),
    "fixed": ("📌 Fixed", "Плоский тейк-профит по корзине"),
    "toxic": ("☣️ Toxic", "Институциональный динамический токсик-выход"),
    "off": ("⏹ Off", "Авто-закрытие отключено"),
}


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
    return "u16_chita__classic"


def set_user_universe_id(user_id: int, universe_id: str) -> None:
    """Устанавливает активную вселенную для пользователя."""
    _active_user_universe[user_id] = universe_id


PAGE_SIZE = 6


def render_universes_page(user_id: int, bot_core: Any, page: int = 1) -> Tuple[str, InlineKeyboardMarkup]:
    """Рендерит страницу со списком вселенных и инлайн-кнопками перехода в панель управления."""
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
        f"Нажмите на купол для настройки сайзинга и автозакрытия.\n",
    ]

    buttons = []
    row_buttons = []
    for item in page_entries:
        uid = item["universe_id"]
        scfg = active_strats.get(uid)
        sz_mode = getattr(scfg, "sizing_mode", "static") if scfg else "static"
        cl_mode = getattr(scfg, "mode", "classic") if scfg else "classic"
        badge = f"[{sz_mode.upper()}]"

        mark = "👉 " if uid == current_uid else ""
        net_str = f"{item.get('net_profit_usdt', 0.0):+.2f}$"
        text_lines.append(
            f"{mark}<code>{uid}</code>: <b>Net: {net_str}</b> | Sizing: <code>{sz_mode}</code> | Mode: <code>{cl_mode}</code>"
        )
        btn_label = f"⚙️ {uid[:18]} {badge}"
        row_buttons.append(InlineKeyboardButton(text=btn_label, callback_data=f"use_univ:{uid}:{page}"))
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


def render_strategy_control_panel(user_id: int, bot_core: Any, uid: str, page: int = 1) -> Tuple[str, InlineKeyboardMarkup]:
    """Рендерит детальную панель купола с инлайн-кнопками переключения сайзинга и автозакрытия."""
    set_user_universe_id(user_id, uid)
    app_json_path = DATA_DIR / "app.json"
    app_data = Utils.read_json_file(app_json_path) or {}
    active_strats = app_data.get("active_strategies", {})
    s_cfg = active_strats.get(uid, {})

    cur_sizing = str(s_cfg.get("trend_size_depender") or s_cfg.get("sizing_mode", "static")).lower().strip()
    cur_closer = str(s_cfg.get("mode", "classic")).lower().strip()
    raw_inv = (
        (s_cfg.get("LONG") or {}).get("invest_size")
        or (s_cfg.get("SHORT") or {}).get("invest_size")
        or s_cfg.get("invest_size")
    )
    def_inv = (
        (s_cfg.get("LONG") or {}).get("default_invest_size")
        or (s_cfg.get("SHORT") or {}).get("default_invest_size")
        or s_cfg.get("default_invest_size")
    )
    symbols = s_cfg.get("symbols")
    if not symbols:
        if isinstance(raw_inv, dict):
            symbols = list(raw_inv.keys())
        else:
            symbols = []
    sym_str = ", ".join(symbols) if symbols else "нет"

    if isinstance(raw_inv, dict):
        inv_display = ", ".join(f"{k}: {float(v):.0f}$" for k, v in raw_inv.items())
        if def_inv is not None:
            inv_display += f" (дефолт: {float(def_inv):.0f}$)"
    elif raw_inv is not None:
        inv_display = f"{float(raw_inv):.1f} USDT"
    elif def_inv is not None:
        inv_display = f"{float(def_inv):.1f} USDT (дефолт)"
    else:
        inv_display = "400.0 USDT (дефолт)"

    sz_badge, sz_desc = SIZING_MODE_BADGES.get(cur_sizing, (cur_sizing, ""))
    cl_badge, cl_desc = CLOSER_MODE_BADGES.get(cur_closer, (cur_closer, ""))

    text = (
        f"🐆 <b>УПРАВЛЕНИЕ КУПОЛОМ:</b> <code>{uid}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Инвест-сайз:</b> <code>{inv_display}</code>\n"
        f"🎯 <b>Пул монет ({len(symbols)}):</b> <code>{sym_str}</code>\n\n"
        f"⚖️ <b>Сайзинг (trend_size_depender):</b>\n"
        f"• Текущий режим: <b>{sz_badge}</b>\n"
        f"• Описание: <i>{sz_desc}</i>\n\n"
        f"⚙️ <b>Режим автозакрытия (auto-closer):</b>\n"
        f"• Текущий режим: <b>{cl_badge}</b>\n"
        f"• Описание: <i>{cl_desc}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Выберите кнопку ниже для мгновенного переключения:"
    )

    buttons = []

    # Ряд кнопок сайзинга
    sz_row1 = []
    sz_row2 = []
    for mode_key in ("static", "dyn_inv_16"):
        label, _ = SIZING_MODE_BADGES[mode_key]
        prefix = "✅ " if mode_key == cur_sizing else ""
        sz_row1.append(InlineKeyboardButton(text=f"{prefix}{label}", callback_data=f"set_sizing:{uid}:{mode_key}:{page}"))

    for mode_key in ("dyn_ema_16", "dyn_ema_10"):
        label, _ = SIZING_MODE_BADGES[mode_key]
        prefix = "✅ " if mode_key == cur_sizing else ""
        sz_row2.append(InlineKeyboardButton(text=f"{prefix}{label}", callback_data=f"set_sizing:{uid}:{mode_key}:{page}"))

    buttons.append(sz_row1)
    buttons.append(sz_row2)

    # Ряд кнопок автозакрытия
    cl_row = []
    for cl_key in ("classic", "fixed", "toxic", "off"):
        label, _ = CLOSER_MODE_BADGES[cl_key]
        prefix = "✅ " if cl_key == cur_closer else ""
        cl_row.append(InlineKeyboardButton(text=f"{prefix}{label[:9]}", callback_data=f"set_closer:{uid}:{cl_key}:{page}"))
    buttons.append(cl_row)

    # Нижняя навигация
    buttons.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"manage_univ:{uid}:{page}"),
        InlineKeyboardButton(text="🔙 К списку вселенных", callback_data=f"back_to_univ_list:{page}")
    ])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    return text, kb


def setup_universe_handlers(dp: Dispatcher, bot_core: Any) -> None:
    """Регистрирует пагинацию, выбор и интерактивные кнопки управления вселенными."""

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

    @dp.callback_query(F.data.startswith("manage_univ:"))
    async def cb_manage_universe(callback: CallbackQuery):
        parts = callback.data.split(":")
        uid = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 1
        try:
            text, kb = render_strategy_control_panel(callback.from_user.id, bot_core, uid, page=page)
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                logger.error(f"Error opening strategy control panel: {e}")
        await callback.answer()

    @dp.callback_query(F.data.startswith("use_univ:"))
    async def cb_select_universe(callback: CallbackQuery):
        parts = callback.data.split(":")
        uid = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 1
        try:
            text, kb = render_strategy_control_panel(callback.from_user.id, bot_core, uid, page=page)
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                logger.error(f"Error in use_univ: {e}")
        await callback.answer()

    @dp.callback_query(F.data.startswith("back_to_univ_list:"))
    async def cb_back_to_univ_list(callback: CallbackQuery):
        parts = callback.data.split(":")
        page = int(parts[1]) if len(parts) > 1 else 1
        try:
            text, kb = render_universes_page(callback.from_user.id, bot_core, page=page)
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                logger.error(f"Error returning to universes list: {e}")
        await callback.answer()

    @dp.callback_query(F.data.startswith("set_sizing:"))
    async def cb_set_sizing(callback: CallbackQuery):
        parts = callback.data.split(":")
        uid = parts[1]
        new_mode = parts[2]
        page = int(parts[3]) if len(parts) > 3 else 1

        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path) or {}
        if "active_strategies" in app_data and uid in app_data["active_strategies"]:
            app_data["active_strategies"][uid]["trend_size_depender"] = new_mode
            app_data["active_strategies"][uid]["sizing_mode"] = new_mode
            Utils.atomic_write_json(app_json_path, app_data)

            # Перезагружаем реестр и обновляем рантаймы
            if hasattr(bot_core, "strategy_registry") and bot_core.strategy_registry:
                bot_core.strategy_registry.reload(app_data["active_strategies"])
                try:
                    from CORE.STRATEGY.strategy_applicator import StrategyApplicator
                    StrategyApplicator().build_all_runtimes(bot_core.strategy_registry)
                except Exception as ex:
                    logger.warning(f"Runtime rebuild notice on sizing change: {ex}")

            logger.info(f"[{uid}] Сайзинг переключен на '{new_mode}' через Telegram.")
            text, kb = render_strategy_control_panel(callback.from_user.id, bot_core, uid, page=page)
            try:
                await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
            await callback.answer(f"✅ Сайзинг переключен на [{new_mode}]!", show_alert=True)
        else:
            await callback.answer(f"❌ Стратегия {uid} не найдена в active_strategies", show_alert=True)

    @dp.callback_query(F.data.startswith("set_closer:"))
    async def cb_set_closer(callback: CallbackQuery):
        parts = callback.data.split(":")
        uid = parts[1]
        new_closer = parts[2]
        page = int(parts[3]) if len(parts) > 3 else 1

        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path) or {}
        if "active_strategies" in app_data and uid in app_data["active_strategies"]:
            app_data["active_strategies"][uid]["mode"] = new_closer
            Utils.atomic_write_json(app_json_path, app_data)

            if hasattr(bot_core, "strategy_registry") and bot_core.strategy_registry:
                bot_core.strategy_registry.reload(app_data["active_strategies"])
                try:
                    from CORE.STRATEGY.strategy_applicator import StrategyApplicator
                    StrategyApplicator().build_all_runtimes(bot_core.strategy_registry)
                except Exception as ex:
                    logger.warning(f"Runtime rebuild notice on closer mode change: {ex}")

            logger.info(f"[{uid}] Режим автозакрытия переключен на '{new_closer}' через Telegram.")
            text, kb = render_strategy_control_panel(callback.from_user.id, bot_core, uid, page=page)
            try:
                await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
            await callback.answer(f"✅ Автозакрытие переключено на [{new_closer}]!", show_alert=True)
        else:
            await callback.answer(f"❌ Стратегия {uid} не найдена в active_strategies", show_alert=True)
