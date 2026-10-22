# ==============================================================================
# Path: TG/universe_commands.py
# Role: Сервисный класс сессионного управления вселенными Telegram (OOP Service)
# ==============================================================================

from typing import Any, Tuple
from aiogram.types import InlineKeyboardMarkup
from TG.HANDLERS.universe_router import (
    universe_session_manager,
    _build_inline_keyboard,
)
from ANALYTICS.report_service import AnalyticsReportService
from consts import DATA_DIR
from c_utils import Utils


class UniverseCommandsService:
    """Сервис сессионного контекста и рендеринга страниц выбора вселенных Telegram."""

    def __init__(self, session_manager: Any = None):
        self.session_manager = session_manager or universe_session_manager

    def get_user_universe_id(self, user_id: int, bot_core: Any = None) -> str:
        """Возвращает текущую выбранную вселенную пользователя."""
        return self.session_manager.get_user_universe_id(user_id, bot_core)

    def set_user_universe_id(self, user_id: int, universe_id: str) -> None:
        """Устанавливает активную вселенную для пользователя."""
        self.session_manager.set_user_universe_id(user_id, universe_id)

    def render_universes_page(self, user_id: int, bot_core: Any, page: int = 1) -> Tuple[str, InlineKeyboardMarkup]:
        """Рендерит страницу со списком вселенных и инлайн-кнопками тумблеров."""
        current_uid = self.get_user_universe_id(user_id, bot_core)
        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path) or {}
        active_strats = {
            k: v for k, v in (app_data.get("active_strategies") or {}).items()
            if not k.startswith("_") and isinstance(v, dict)
        }
        an = getattr(bot_core, "analytics", None)
        if an and hasattr(an, "report_service") and isinstance(an.report_service, AnalyticsReportService):
            rep_svc = an.report_service
        else:
            rep_svc = AnalyticsReportService()
        text, btn_data = rep_svc.render_universes_list(
            current_uid=current_uid,
            active_strats=active_strats,
            page=page,
        )
        return text, _build_inline_keyboard(btn_data)

    def render_strategy_control_panel(self, user_id: int, bot_core: Any, uid: str, page: int = 1) -> Tuple[str, InlineKeyboardMarkup]:
        """Алиас: прямое перенаправление на чистый тумблер стратегий."""
        self.set_user_universe_id(user_id, uid)
        return self.render_universes_page(user_id, bot_core, page)

    def setup_universe_handlers(self, dp: Any, bot_core: Any) -> None:
        """Регистрирует роутер вселенных в Dispatcher (обратная совместимость)."""
        from TG.HANDLERS.universe_router import get_universe_router
        dp.include_router(get_universe_router(bot_core))


# Синглтон-экземпляр и делегаты для обратной совместимости
universe_commands_service = UniverseCommandsService()
get_user_universe_id = universe_commands_service.get_user_universe_id
set_user_universe_id = universe_commands_service.set_user_universe_id
render_universes_page = universe_commands_service.render_universes_page
render_strategy_control_panel = universe_commands_service.render_strategy_control_panel
setup_universe_handlers = universe_commands_service.setup_universe_handlers
