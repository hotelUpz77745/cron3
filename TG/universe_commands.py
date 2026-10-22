# ==============================================================================
# Path: TG/universe_commands.py
# Role: Интерактивное управление вселенными и стратегиями через Telegram (Фасад и обратная совместимость)
# ==============================================================================

from pathlib import Path
from typing import Any, Tuple, Dict, List, Optional
import json

from aiogram import Dispatcher
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from consts import DATA_DIR
from c_log import UnifiedLogger
from c_utils import Utils
from ANALYTICS.report_service import AnalyticsReportService
from TG.HANDLERS.universe_router import (
    universe_session_manager,
    get_universe_router,
    _build_inline_keyboard,
)

logger = UnifiedLogger("TGUniverseCommands")

# Экспорт бейджей для обратной совместимости
SIZING_MODE_BADGES = AnalyticsReportService.SIZING_MODE_BADGES
CLOSER_MODE_BADGES = AnalyticsReportService.CLOSER_MODE_BADGES
PAGE_SIZE = 6


def get_user_universe_id(user_id: int, bot_core: Any = None) -> str:
    """Возвращает текущую выбранную вселенную пользователя."""
    return universe_session_manager.get_user_universe_id(user_id, bot_core)


def set_user_universe_id(user_id: int, universe_id: str) -> None:
    """Устанавливает активную вселенную для пользователя."""
    universe_session_manager.set_user_universe_id(user_id, universe_id)


def render_universes_page(user_id: int, bot_core: Any, page: int = 1) -> Tuple[str, InlineKeyboardMarkup]:
    """Рендерит страницу со списком вселенных и инлайн-кнопками перехода в панель управления."""
    current_uid = get_user_universe_id(user_id, bot_core)

    active_strats = {}
    if hasattr(bot_core, "strategy_registry") and bot_core.strategy_registry:
        active_strats = bot_core.strategy_registry.get_active_strategies()
    else:
        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path) or {}
        active_strats = app_data.get("active_strategies", {})

    an = getattr(bot_core, "analytics", None)
    if an and hasattr(an, "report_service") and isinstance(an.report_service, AnalyticsReportService):
        rep_svc = an.report_service
    else:
        rep_svc = AnalyticsReportService()

    text, btn_data = rep_svc.render_universes_list(
        current_uid=current_uid,
        active_strats=active_strats,
        page=page,
        page_size=PAGE_SIZE,
    )
    return text, _build_inline_keyboard(btn_data)


def render_strategy_control_panel(user_id: int, bot_core: Any, uid: str, page: int = 1) -> Tuple[str, InlineKeyboardMarkup]:
    """Рендерит детальную панель купола с инлайн-кнопками переключения сайзинга и автозакрытия."""
    set_user_universe_id(user_id, uid)
    app_json_path = DATA_DIR / "app.json"
    app_data = Utils.read_json_file(app_json_path) or {}
    active_strats = app_data.get("active_strategies", {})
    s_cfg = active_strats.get(uid, {})

    an = getattr(bot_core, "analytics", None)
    if an and hasattr(an, "report_service") and isinstance(an.report_service, AnalyticsReportService):
        rep_svc = an.report_service
    else:
        rep_svc = AnalyticsReportService()

    text, btn_data = rep_svc.render_universe_control_panel(
        uid=uid,
        s_cfg=s_cfg,
        page=page,
    )
    return text, _build_inline_keyboard(btn_data)


def setup_universe_handlers(dp: Dispatcher, bot_core: Any) -> None:
    """Регистрирует роутер вселенных в Dispatcher (обратная совместимость)."""
    router = get_universe_router(bot_core)
    dp.include_router(router)
