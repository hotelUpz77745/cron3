# ==============================================================================
# Path: TG/ui_utils.py
# Role: Безопасные утилиты взаимодействия с Telegram UI (Safe Edit / Safe Answer)
# ==============================================================================

from typing import Any, Optional, Union
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, Message

from c_log import UnifiedLogger

logger = UnifiedLogger("TGUIUtils")

BENIGN_TELEGRAM_ERROR_PATTERNS = (
    "message can't be edited",
    "message is not modified",
    "message to edit not found",
    "message not found",
    "query is too old",
    "chat not found",
    "bot was blocked by the user",
    "user is deactivated",
)


def is_benign_telegram_error(exc: Exception) -> bool:
    """Проверяет, является ли исключение известным безобидным сбоем UI Telegram."""
    if not isinstance(exc, (TelegramBadRequest, TelegramForbiddenError)):
        return False
    err_str = str(exc).lower()
    return any(p in err_str for p in BENIGN_TELEGRAM_ERROR_PATTERNS)


async def safe_edit_text(message: Message, text: str, **kwargs) -> Optional[Message]:
    """
    Безопасное редактирование сообщения в Telegram.
    1. Пытается вызвать message.edit_text.
    2. Если сообщение не модифицировано ("message is not modified") — возвращает исходное сообщение.
    3. Если сообщение не может быть отредактировано (удалено, ReplyKeyboardMarkup, устарело, 'message can't be edited') —
       автоматически отправляет новое сообщение через message.answer.
    4. Гарантированно не выбрасывает исключений наружу.
    """
    try:
        return await message.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        err_msg = str(e).lower()
        if "message is not modified" in err_msg:
            return message
        logger.warning(f"[safe_edit_text] edit_text failed ({e}), falling back to answer")
        try:
            return await message.answer(text, **kwargs)
        except Exception as inner_e:
            logger.error(f"[safe_edit_text] fallback answer failed: {inner_e}")
            return None
    except Exception as e:
        logger.warning(f"[safe_edit_text] edit_text unexpected error ({e}), falling back to answer")
        try:
            return await message.answer(text, **kwargs)
        except Exception as inner_e:
            logger.error(f"[safe_edit_text] fallback answer failed: {inner_e}")
            return None


async def safe_edit_callback(callback: CallbackQuery, text: str, **kwargs) -> Optional[Message]:
    """
    Безопасное редактирование сообщения из CallbackQuery с автоматическим фоллбеком.
    """
    if callback.message and isinstance(callback.message, Message):
        return await safe_edit_text(callback.message, text, **kwargs)
    elif callback.bot and callback.from_user:
        try:
            return await callback.bot.send_message(chat_id=callback.from_user.id, text=text, **kwargs)
        except Exception as e:
            logger.error(f"[safe_edit_callback] fallback bot.send_message failed: {e}")
            return None
    return None


async def safe_answer(message_or_cb: Union[Message, CallbackQuery], text: str, **kwargs) -> Optional[Message]:
    """
    Безопасная отправка ответа (message.answer) без риска краша обработчика.
    """
    try:
        if isinstance(message_or_cb, CallbackQuery):
            if message_or_cb.message:
                return await message_or_cb.message.answer(text, **kwargs)
            elif message_or_cb.bot:
                return await message_or_cb.bot.send_message(chat_id=message_or_cb.from_user.id, text=text, **kwargs)
            return None
        return await message_or_cb.answer(text, **kwargs)
    except Exception as e:
        logger.error(f"[safe_answer] Failed to send answer: {e}")
        return None


async def safe_callback_answer(callback: CallbackQuery, text: Optional[str] = None, show_alert: bool = False) -> bool:
    """
    Безопасный ответ на CallbackQuery, гасящий ошибку 'query is too old' при задержках сети.
    """
    try:
        await callback.answer(text=text, show_alert=show_alert)
        return True
    except Exception as e:
        err_msg = str(e).lower()
        if "query is too old" in err_msg or "query id is invalid" in err_msg:
            logger.debug(f"[safe_callback_answer] Expired callback answer swallowed: {e}")
            return False
        logger.warning(f"[safe_callback_answer] callback.answer failed: {e}")
        return False
