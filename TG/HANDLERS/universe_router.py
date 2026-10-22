# ==============================================================================
# Path: TG/HANDLERS/universe_router.py
# Role: Роутер навигации по вселенным, пагинации и интерактивного управления куполами
# ==============================================================================

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from consts import DATA_DIR
from c_log import UnifiedLogger
from c_utils import Utils
from ANALYTICS.report_service import AnalyticsReportService

logger = UnifiedLogger("TGUniverseRouter")


class UniverseSessionManager:
    """Управление сессионным контекстом выбранной вселенной для каждого пользователя Telegram."""

    def __init__(self, default_universe_id: str = "u17_stephensonE__trailing_ratchet"):
        self._active_user_universe: Dict[int, str] = {}
        self.default_universe_id: str = default_universe_id

    def get_user_universe_id(self, user_id: int, bot_core: Any = None) -> str:
        """Возвращает текущую выбранную вселенную пользователя."""
        uid = self._active_user_universe.get(user_id)
        if uid:
            return uid
        if bot_core and hasattr(bot_core, "strategy_registry") and bot_core.strategy_registry:
            try:
                active = (
                    bot_core.strategy_registry.get_active_strategies()
                    if callable(getattr(bot_core.strategy_registry, "get_active_strategies", None))
                    else getattr(bot_core.strategy_registry, "active_strategies", {})
                )
                if active:
                    first_uid = next(iter(active.keys()), None)
                    if first_uid:
                        self._active_user_universe[user_id] = first_uid
                        return first_uid
            except Exception as e:
                logger.debug(f"Could not resolve universe from strategy_registry: {e}")
        return self.default_universe_id

    def set_user_universe_id(self, user_id: int, universe_id: str) -> None:
        """Устанавливает активную вселенную для пользователя."""
        self._active_user_universe[user_id] = universe_id


# Глобальный синглтон сессионного менеджера для обратной совместимости
universe_session_manager = UniverseSessionManager()


def _build_inline_keyboard(buttons_data: List[List[Dict[str, str]]]) -> InlineKeyboardMarkup:
    """Транслирует универсальную структуру кнопок из аналитики в Telegram InlineKeyboardMarkup."""
    rows = []
    for row in buttons_data:
        kb_row = []
        for btn in row:
            kb_row.append(InlineKeyboardButton(text=btn["text"], callback_data=btn["callback_data"]))
        rows.append(kb_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_universe_router(bot_core: Any) -> Router:
    """
    Фабрика aiogram 3 Router для работы со списком вселенных и панелью управления куполом.
    ТГ слой выступает тупым исполнителем (Dumb Executor), делегируя рендеринг в AnalyticsReportService.
    """
    router = Router(name="universe_router")

    def _get_report_service() -> AnalyticsReportService:
        an = getattr(bot_core, "analytics", None)
        if an and hasattr(an, "report_service") and isinstance(an.report_service, AnalyticsReportService):
            return an.report_service
        return AnalyticsReportService()

    def _get_configured_strategies() -> Dict[str, Any]:
        """Возвращает все сконфигурированные стратегии куполов из app.json (включая отключенные)."""
        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path) or {}
        active_raw = app_data.get("active_strategies", {})
        return {
            k: v for k, v in active_raw.items()
            if not k.startswith("_") and isinstance(v, dict)
        }

    @router.message(Command("universes"))
    @router.message(F.text == "🌌 Set Universes")
    async def cmd_universes(message: Message, state: FSMContext):
        """Обработка кнопки главного меню '🌌 Set Universes' и команды /universes."""
        logger.info(f"[TG] 🌌 User {message.from_user.id} requested 'Set Universes' menu.")
        await state.clear()
        page = 1
        current_uid = universe_session_manager.get_user_universe_id(message.from_user.id, bot_core)
        configured_strats = _get_configured_strategies()
        text, btn_data = _get_report_service().render_universes_list(
            current_uid=current_uid,
            active_strats=configured_strats,
            page=page,
        )
        kb = _build_inline_keyboard(btn_data)
        await message.answer(text, reply_markup=kb, parse_mode="HTML")

    @router.callback_query(F.data.startswith("univ_page:"))
    async def cb_universe_page(callback: CallbackQuery):
        target = callback.data.split("univ_page:")[1]
        if target == "noop":
            await callback.answer()
            return
        try:
            page = int(target)
            current_uid = universe_session_manager.get_user_universe_id(callback.from_user.id, bot_core)
            configured_strats = _get_configured_strategies()
            text, btn_data = _get_report_service().render_universes_list(
                current_uid=current_uid,
                active_strats=configured_strats,
                page=page,
            )
            kb = _build_inline_keyboard(btn_data)
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                logger.error(f"Error in cb_universe_page: {e}")
        await callback.answer()

    @router.callback_query(F.data.startswith("toggle_univ:"))
    async def cb_toggle_universe(callback: CallbackQuery):
        """Прямой тумблер включения/отключения стратегии купола [ВКЛ / ВЫКЛ]."""
        parts = callback.data.split(":")
        uid = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 1

        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path) or {}
        active_strats = app_data.get("active_strategies", {})

        if uid in active_strats and isinstance(active_strats[uid], dict):
            current_state = bool(active_strats[uid].get("enabled", True))
            new_state = not current_state
            active_strats[uid]["enabled"] = new_state
            Utils.atomic_write_json(app_json_path, app_data)

            # Перезагружаем реестр стратегий и пересобираем рантаймы
            if hasattr(bot_core, "strategy_registry") and bot_core.strategy_registry:
                try:
                    bot_core.strategy_registry.reload(active_strats)
                    from CORE.strategy_applicator import StrategyApplicator
                    StrategyApplicator().build_all_runtimes(bot_core.strategy_registry)
                except Exception as ex:
                    logger.warning(f"Runtime rebuild notice on universe toggle: {ex}")

            logger.info(f"[TG] 🌌 Стратегия [{uid}] переключена: enabled={new_state}")

            configured_strats = _get_configured_strategies()
            current_uid = universe_session_manager.get_user_universe_id(callback.from_user.id, bot_core)
            text, btn_data = _get_report_service().render_universes_list(
                current_uid=current_uid,
                active_strats=configured_strats,
                page=page,
            )
            kb = _build_inline_keyboard(btn_data)
            try:
                await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            except Exception as e:
                if "message is not modified" not in str(e).lower():
                    logger.error(f"Error editing message on universe toggle: {e}")

            status_toast = f"🟢 Стратегия [{uid}] ВКЛЮЧЕНА!" if new_state else f"⚪ Стратегия [{uid}] ВЫКЛЮЧЕНА!"
            await callback.answer(status_toast, show_alert=False)
        else:
            await callback.answer(f"❌ Стратегия {uid} не найдена в app.json", show_alert=True)

    @router.callback_query(F.data.startswith("manage_univ:") | F.data.startswith("use_univ:"))
    async def cb_legacy_manage_universe(callback: CallbackQuery):
        """Безопасный перехват устаревших инлайн-кнопок управления."""
        parts = callback.data.split(":")
        uid = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 1
        universe_session_manager.set_user_universe_id(callback.from_user.id, uid)

        configured_strats = _get_configured_strategies()
        current_uid = universe_session_manager.get_user_universe_id(callback.from_user.id, bot_core)
        text, btn_data = _get_report_service().render_universes_list(
            current_uid=current_uid,
            active_strats=configured_strats,
            page=page,
        )
        kb = _build_inline_keyboard(btn_data)
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
        await callback.answer("Управление куполами переведено на тумблер [ВКЛ / ВЫКЛ]", show_alert=False)

    @router.callback_query(F.data.startswith("back_to_univ_list:"))
    async def cb_back_to_univ_list(callback: CallbackQuery):
        parts = callback.data.split(":")
        page = int(parts[1]) if len(parts) > 1 else 1
        try:
            current_uid = universe_session_manager.get_user_universe_id(callback.from_user.id, bot_core)
            configured_strats = _get_configured_strategies()
            text, btn_data = _get_report_service().render_universes_list(
                current_uid=current_uid,
                active_strats=configured_strats,
                page=page,
            )
            kb = _build_inline_keyboard(btn_data)
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                logger.error(f"Error returning to universes list: {e}")
        await callback.answer()

    @router.callback_query(F.data.startswith("set_sizing:") | F.data.startswith("set_closer:"))
    async def cb_legacy_sizing_closer(callback: CallbackQuery):
        """Информационный ответ при нажатии устаревших кнопок сайзинга из старых сообщений."""
        await callback.answer(
            "⚠️ Интерактивный сайзинг упразднен.\nДинамика объемов управляется строго паспортом стратегии (first_order_weight_regime / smart_size).",
            show_alert=True,
        )
        try:
            configured_strats = _get_configured_strategies()
            current_uid = universe_session_manager.get_user_universe_id(callback.from_user.id, bot_core)
            text, btn_data = _get_report_service().render_universes_list(
                current_uid=current_uid,
                active_strats=configured_strats,
                page=1,
            )
            kb = _build_inline_keyboard(btn_data)
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass

    return router
