# ==============================================================================
# Path: tests/test_tg_ui_resilience.py
# Role: Тестирование отказоустойчивости UI Telegram (safe_edit_text, fallback, error handling)
# ==============================================================================

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, Message, ErrorEvent

from TG.ui_utils import (
    is_benign_telegram_error,
    safe_answer,
    safe_callback_answer,
    safe_edit_callback,
    safe_edit_text,
)


class TestTGUIResilience(unittest.TestCase):

    def test_is_benign_telegram_error(self):
        """Проверка классификации безвредных Telegram API ошибок."""
        err_cant_edit = TelegramBadRequest(method=MagicMock(), message="Telegram server says - Bad Request: message can't be edited")
        self.assertTrue(is_benign_telegram_error(err_cant_edit))

        err_not_modified = TelegramBadRequest(method=MagicMock(), message="Bad Request: message is not modified")
        self.assertTrue(is_benign_telegram_error(err_not_modified))

        err_query_old = TelegramBadRequest(method=MagicMock(), message="Bad Request: query is too old and response timeout expired or query id is invalid")
        self.assertTrue(is_benign_telegram_error(err_query_old))

        err_blocked = TelegramForbiddenError(method=MagicMock(), message="Forbidden: bot was blocked by the user")
        self.assertTrue(is_benign_telegram_error(err_blocked))

        err_general = ValueError("Critical code error")
        self.assertFalse(is_benign_telegram_error(err_general))

    def test_safe_edit_text_success(self):
        """Проверка нормального успешного редактирования через safe_edit_text."""
        msg = MagicMock(spec=Message)
        msg.edit_text = AsyncMock(return_value="edited_msg")

        res = asyncio.run(safe_edit_text(msg, "New text"))
        self.assertEqual(res, "edited_msg")
        msg.edit_text.assert_awaited_once_with("New text")

    def test_safe_edit_text_not_modified(self):
        """Проверка подавления 'message is not modified' без выброса исключения."""
        msg = MagicMock(spec=Message)
        err = TelegramBadRequest(method=MagicMock(), message="Bad Request: message is not modified")
        msg.edit_text = AsyncMock(side_effect=err)

        res = asyncio.run(safe_edit_text(msg, "Same text"))
        self.assertEqual(res, msg)

    def test_safe_edit_text_fallback_on_cant_edit(self):
        """
        Проверка сценария баги из прода:
        когда edit_text падает с 'message can't be edited', вызывается fallback msg.answer.
        """
        msg = MagicMock(spec=Message)
        err = TelegramBadRequest(method=MagicMock(), message="Bad Request: message can't be edited")
        msg.edit_text = AsyncMock(side_effect=err)
        fallback_msg = MagicMock(spec=Message)
        msg.answer = AsyncMock(return_value=fallback_msg)

        res = asyncio.run(safe_edit_text(msg, "Updated text", parse_mode="HTML"))
        self.assertEqual(res, fallback_msg)
        msg.answer.assert_awaited_once_with("Updated text", parse_mode="HTML")

    def test_safe_edit_text_suppresses_when_both_fail(self):
        """Когда и edit_text, и fallback answer терпят неудачу, метод не должен крашить приложение."""
        msg = MagicMock(spec=Message)
        msg.edit_text = AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message="message can't be edited"))
        msg.answer = AsyncMock(side_effect=Exception("Network drop"))

        res = asyncio.run(safe_edit_text(msg, "Text"))
        self.assertIsNone(res)

    def test_safe_edit_callback(self):
        """Проверка safe_edit_callback при штатной работе и при ошибке редактирования."""
        cb = MagicMock(spec=CallbackQuery)
        cb.message = MagicMock(spec=Message)
        cb.message.edit_text = AsyncMock(return_value="cb_edited")

        res = asyncio.run(safe_edit_callback(cb, "CB Text"))
        self.assertEqual(res, "cb_edited")

        # Fallback когда edit_text падает
        cb.message.edit_text = AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message="message can't be edited"))
        cb.message.answer = AsyncMock(return_value="cb_answered")
        res2 = asyncio.run(safe_edit_callback(cb, "CB Text 2"))
        self.assertEqual(res2, "cb_answered")

    def test_safe_answer(self):
        """Проверка safe_answer: отправляет сообщение или безопасно глушит сбой."""
        msg = MagicMock(spec=Message)
        msg.answer = AsyncMock(return_value="sent")
        res = asyncio.run(safe_answer(msg, "Hello"))
        self.assertEqual(res, "sent")

        msg.answer = AsyncMock(side_effect=Exception("Some send error"))
        res2 = asyncio.run(safe_answer(msg, "Hello again"))
        self.assertIsNone(res2)

    def test_safe_callback_answer(self):
        """Проверка safe_callback_answer: гасит expired query."""
        cb = MagicMock(spec=CallbackQuery)
        cb.answer = AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message="query is too old"))
        res = asyncio.run(safe_callback_answer(cb))
        self.assertFalse(res)


if __name__ == "__main__":
    unittest.main()
