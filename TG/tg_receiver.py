# ==============================================================================
# Path: TG/tg_receiver.py
# Role: Оркестратор Telegram-слоя бота (инициализация aiogram, middleware, роутеры)
# ==============================================================================

import asyncio
from pathlib import Path
from typing import Any, Optional, Tuple

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup

from ANALYTICS.report_service import AnalyticsReportService
from ANALYTICS.portfolio_aggregator import PortfolioAggregator

from consts import (
    ANALYTICS_LEDGERS_DIR,
    ANALYTICS_STATS_DIR,
    TG_ALLOWED_USERS,
    TG_TOKEN,
)
from c_log import UnifiedLogger
from TG.HANDLERS.analytics_router import get_analytics_router
from TG.HANDLERS.base_router import get_base_router
from TG.HANDLERS.coins_router import get_coins_router
from TG.HANDLERS.coins_size_router import get_coins_size_router
from TG.HANDLERS.positions_router import get_positions_router
from TG.HANDLERS.screeners_router import get_screeners_router
from TG.keyboards import (
    get_back_keyboard,
    get_confirm_start_keyboard,
    get_main_keyboard,
    get_set_coins_keyboard,
)
from TG.states import TGStates
from TG.strategy_helpers import extract_strategy_symbols, get_strategy_meta
from TG.template_manager import TemplateManager
from TG.universe_commands import get_user_universe_id
from TG.HANDLERS.universe_router import get_universe_router

logger = UnifiedLogger("TGReceiver")


class TelegramReceiver:
    """
    Главный оркестратор Telegram-интерфейса.
    Выступает в роли чистого презентационного слоя (Dumb Executor),
    делегируя обработку команд и FSM-сценариев в изолированные Router-модули,
    а расчет метрик и графиков — в домен ANALYTICS.
    """

    def __init__(self, bot_core: Any):
        self.bot_core = bot_core
        self.bot = Bot(token=TG_TOKEN)
        self.dp = Dispatcher(storage=MemoryStorage())
        self.template_manager = TemplateManager(bot_core=self.bot_core)
        self._lock = asyncio.Lock()

        self._setup_middlewares()
        self._setup_routes()

    def _setup_middlewares(self):
        """Регистрирует внешние middleware для валидации прав доступа и типов сообщений."""

        async def check_user_message(handler, event: Message, data: dict):
            if TG_ALLOWED_USERS and event.from_user.id not in TG_ALLOWED_USERS:
                logger.warning(f"[TG] Unauthorized access attempt by {event.from_user.id} ({getattr(event.from_user, 'username', 'no_user')}): '{event.text}'")
                return
            if not event.text and not event.document:
                await event.answer(
                    "⚠️ Бот принимает только текстовые команды или документы (файлы). "
                    "Пожалуйста, не отправляйте фото, видео, стикеры или пустые сообщения."
                )
                return
            logger.info(f"[TG] 📩 Message from user {event.from_user.id}: '{event.text}'")
            return await handler(event, data)

        self.dp.message.outer_middleware.register(check_user_message)

        async def check_user_callback(handler, event: CallbackQuery, data: dict):
            if TG_ALLOWED_USERS and event.from_user.id not in TG_ALLOWED_USERS:
                logger.warning(f"[TG] Unauthorized callback attempt by {event.from_user.id} ({getattr(event.from_user, 'username', 'no_user')}): '{event.data}'")
                return
            logger.info(f"[TG] 🔘 Callback from user {event.from_user.id}: '{event.data}'")
            return await handler(event, data)

        self.dp.callback_query.outer_middleware.register(check_user_callback)

    def _setup_routes(self):
        """Подключает подсистемы вселенных и все модульные aiogram 3 роутеры."""
        self.dp.include_router(get_universe_router(self.bot_core))
        self.dp.include_router(get_base_router(self.bot_core, self.template_manager))
        self.dp.include_router(get_positions_router(self.bot_core))
        self.dp.include_router(get_analytics_router(self.bot_core))
        self.dp.include_router(get_coins_router(self.bot_core, self.template_manager))
        self.dp.include_router(get_coins_size_router(self.bot_core))
        self.dp.include_router(get_screeners_router(self.bot_core))

    # =========================================================
    # COMPATIBILITY HELPERS FOR TESTS AND LEGACY CALLERS
    # =========================================================
    def _get_main_keyboard(self):
        return get_main_keyboard()

    def _get_set_coins_keyboard(self):
        return get_set_coins_keyboard()

    def _get_back_keyboard(self):
        return get_back_keyboard()

    def _get_confirm_start_keyboard(self):
        return get_confirm_start_keyboard()

    def _get_strategy_meta(self, strat_id: str) -> dict:
        return get_strategy_meta(strat_id, self.bot_core)

    @staticmethod
    def _extract_strategy_symbols(sdata) -> list:
        return extract_strategy_symbols(sdata)

    def _get_universe_analytics(self, user_id: int, target_uid: Optional[str] = None) -> Tuple[str, str, Path, Path]:
        """Возвращает метаданные и пути к файлам аналитики и леджера для купола пользователя."""
        current_uid = target_uid or get_user_universe_id(user_id, self.bot_core)
        meta = self._get_strategy_meta(current_uid)
        univ_name = meta.get("name", current_uid)

        analytics_path = ANALYTICS_STATS_DIR / f"analytics_{current_uid}.json"
        if not analytics_path.exists():
            matches = list(ANALYTICS_STATS_DIR.glob(f"analytics_{current_uid}*.json"))
            if matches:
                analytics_path = sorted(matches)[0]
            else:
                pure_id = current_uid.split("__")[0]
                pure_matches = list(ANALYTICS_STATS_DIR.glob(f"analytics_{pure_id}*.json"))
                analytics_path = sorted(pure_matches)[0] if pure_matches else (ANALYTICS_STATS_DIR / f"analytics_{current_uid}.json")

        csv_path = ANALYTICS_LEDGERS_DIR / f"trades_ledger_{current_uid}.txt"
        if not csv_path.exists() or csv_path.stat().st_size <= 60:
            matches_csv = list(ANALYTICS_LEDGERS_DIR.glob(f"trades_ledger_{current_uid}*.txt"))
            found = [m for m in matches_csv if m.stat().st_size > 60]
            if found:
                csv_path = sorted(found)[0]
            else:
                pure_id = current_uid.split("__")[0]
                pure_csv_matches = list(ANALYTICS_LEDGERS_DIR.glob(f"trades_ledger_{pure_id}*.txt"))
                found_pure = [m for m in pure_csv_matches if m.stat().st_size > 60]
                if found_pure:
                    csv_path = sorted(found_pure)[0]
                else:
                    general_ledger = ANALYTICS_LEDGERS_DIR / "trades_ledger.txt"
                    if general_ledger.exists() and general_ledger.stat().st_size > 60:
                        csv_path = general_ledger

        return current_uid, univ_name, analytics_path, csv_path

    def _render_analytics_big_board(self, user_id: int):
        current_uid, univ_name, _, _ = self._get_universe_analytics(user_id)
        report_svc = getattr(self.bot_core.analytics, "report_service", None) if hasattr(self.bot_core, "analytics") else None
        if not report_svc:
            report_svc = AnalyticsReportService()
        active_strats = {}
        if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
            active_strats = self.bot_core.strategy_registry.get_active_strategies()
        text, buttons = report_svc.render_portfolio_big_board(current_uid, univ_name, active_strats=active_strats)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=b["text"], callback_data=b["callback_data"]) for b in r] for r in buttons])
        return text, kb

    def _render_universe_analytics(self, user_id: int, target_uid: Optional[str] = None):
        current_uid, univ_name, _, _ = self._get_universe_analytics(user_id, target_uid=target_uid)
        report_svc = getattr(self.bot_core.analytics, "report_service", None) if hasattr(self.bot_core, "analytics") else None
        if not report_svc:
            report_svc = AnalyticsReportService()
        text, buttons = report_svc.render_universe_analytics_card(current_uid, univ_name)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=b["text"], callback_data=b["callback_data"]) for b in r] for r in buttons])
        return text, kb

    def _get_portfolio_all_analytics_data(self):
        agg = getattr(self.bot_core.analytics, "portfolio_aggregator", None) if hasattr(self.bot_core, "analytics") else None
        if not agg:
            agg = PortfolioAggregator(analytics_manager=getattr(self.bot_core, "analytics", None))
        return agg.get_portfolio_all_data()


    # =========================================================
    # LIFECYCLE
    # =========================================================
    async def start(self):
        """Запуск цикла поллинга Telegram бота."""
        logger.info("Starting Telegram Receiver...")
        try:
            await self.dp.start_polling(self.bot)
        finally:
            await self.stop()

    async def stop(self):
        """Корректная остановка поллинга и закрытие сетевой сессии."""
        try:
            await self.dp.stop_polling()
        except Exception:
            pass
        try:
            if self.bot and hasattr(self.bot, "session") and self.bot.session:
                await self.bot.session.close()
        except Exception:
            pass


def build_rotation_inline_keyboard(req_id: str, count: int = 1) -> dict:
    """Генератор инлайн-клавиатуры для подтверждения ротации."""
    accept_text = f"✅ Применить ротацию ({count})" if count > 1 else "✅ Применить ротацию"
    return {
        "inline_keyboard": [
            [
                {"text": accept_text, "callback_data": f"rot_accept:{req_id}"},
                {"text": "❌ Оставить как есть", "callback_data": f"rot_reject:{req_id}"},
            ]
        ]
    }
