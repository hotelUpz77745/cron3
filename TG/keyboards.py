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
