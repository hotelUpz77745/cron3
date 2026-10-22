# ==============================================================================
# Path: TG/keyboards.py
# Role: Reply клавиатуры главного меню, управления монетами и навигации
# ==============================================================================

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Возвращает основную Reply-клавиатуру бота."""
    keyboard = [
        [
            KeyboardButton(text="▶️ Start"),
            KeyboardButton(text="ℹ️ Status"),
            KeyboardButton(text="⏸️ Stop"),
        ],
        [
            KeyboardButton(text="🌌 Set Universes"),
            KeyboardButton(text="📊 Analytics"),
            KeyboardButton(text="🎯 Скринеры"),
        ],
        [
            KeyboardButton(text="⚙️ Set Coins"),
            KeyboardButton(text="📜 Logs"),
        ],
        [
            KeyboardButton(text="🚨 Close All"),
        ],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def get_set_coins_keyboard() -> ReplyKeyboardMarkup:
    """Возвращает клавиатуру раздела управления монетами."""
    keyboard = [
        [
            KeyboardButton(text="➕ Добавить монету"),
            KeyboardButton(text="🗑️ Удалить монету"),
        ],
        [
            KeyboardButton(text="🔙 Назад"),
        ],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def get_confirm_start_keyboard() -> ReplyKeyboardMarkup:
    """Возвращает клавиатуру подтверждения старта."""
    keyboard = [
        [
            KeyboardButton(text="✅ Confirm Start"),
            KeyboardButton(text="⚙️ Set Coins"),
        ],
        [
            KeyboardButton(text="🔙 Back"),
        ],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def get_back_keyboard() -> ReplyKeyboardMarkup:
    """Возвращает стандартную кнопку Назад."""
    keyboard = [
        [
            KeyboardButton(text="🔙 Back"),
        ],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from typing import Any, List, Dict


def get_strategy_selection_inline_keyboard(active_strategies: Dict[str, Any], symbol: str, bot_core: Any) -> InlineKeyboardMarkup:
    """Инлайн-клавиатура выбора стратегии для монеты."""
    from TG.strategy_helpers import get_strategy_meta
    buttons = []
    for sid, sdata in active_strategies.items():
        if not sdata.get("enabled", False):
            continue
        meta = get_strategy_meta(sid, bot_core)
        s_name = meta.get("name", sid)
        buttons.append([InlineKeyboardButton(text=f"🎯 {s_name[:38]}", callback_data=f"add_pick_strat_{symbol}_{sid}")])

    buttons.append([InlineKeyboardButton(text="🌐 Выбрать другой универсум...", callback_data=f"add_catalog_{symbol}")])
    buttons.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="add_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_set_coins_dome_inline_keyboard(active_strats: Dict[str, Any], bot_core: Any) -> InlineKeyboardMarkup:
    """Инлайн-клавиатура выбора купола стратегии для настройки монет."""
    from TG.strategy_helpers import extract_strategy_symbols, get_strategy_meta
    buttons = []
    for sid, scfg in active_strats.items():
        if sid.startswith("_") or not isinstance(scfg, (dict, object)):
            continue
        meta = get_strategy_meta(sid, bot_core)
        s_name = meta.get("name", sid)
        clean_title = s_name.encode("ascii", "ignore").decode("ascii").strip() or sid
        symbols = extract_strategy_symbols(scfg)
        cnt = len(symbols)
        btn_text = f"🪐 {clean_title[:28]} ({cnt} мон.)"
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"set_coins_dome:{sid}")])

    buttons.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="set_coins_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_launch_params_inline_keyboard(symbol: str, strat_id: str) -> InlineKeyboardMarkup:
    """Инлайн-клавиатура выбора параметров запуска монеты."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Запустить с параметрами стратегии", callback_data=f"add_run_{symbol}_{strat_id}")],
            [InlineKeyboardButton(text="⚙️ Настроить параметры (Invest/Lev)", callback_data=f"add_cust_{symbol}_{strat_id}")],
            [InlineKeyboardButton(text="🔙 Назад к стратегиям", callback_data=f"add_back_strats_{symbol}")],
        ]
    )


def get_catalog_selection_inline_keyboard(univ_data: Dict[str, Any], symbol: str) -> InlineKeyboardMarkup:
    """Инлайн-клавиатура каталога вселенных для назначения монете."""
    buttons = []
    for uid, udata in list(univ_data.items())[:12]:
        u_name = udata.get("name", uid) if isinstance(udata, dict) else uid
        buttons.append([InlineKeyboardButton(text=f"🌐 {u_name[:35]}", callback_data=f"add_pick_strat_{symbol}_{uid}")])

    buttons.append([InlineKeyboardButton(text="🔙 Назад к активным", callback_data=f"add_back_strats_{symbol}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_delete_coins_inline_keyboard(symbols: List[str], selected_dome: str) -> InlineKeyboardMarkup:
    """Инлайн-клавиатура удаления монет из выбранного купола."""
    buttons = []
    row = []
    for sym in sorted(symbols):
        row.append(InlineKeyboardButton(text=f"❌ {sym}", callback_data=f"del_dome_coin:{selected_dome}:{sym}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="del_dome_coin_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

