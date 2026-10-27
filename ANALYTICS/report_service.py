# ==============================================================================
# Path: ANALYTICS/report_service.py
# Role: Сервис формирования аналитических отчетов, сводок и графиков для презентационного слоя
# ==============================================================================

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from consts import (
    ANALYTICS_DIR,
    ANALYTICS_LEDGERS_DIR,
    ANALYTICS_START_BALANCE_USDT,
    ANALYTICS_STATS_DIR,
    _CFG,
)
from c_log import UnifiedLogger
from ANALYTICS.plotter import generate_equity_curve, generate_balance_curve
from ANALYTICS.FORMATTERS.bigboard_formatter import BigBoardFormatter
from ANALYTICS.FORMATTERS.coin_card_formatter import CoinCardFormatter
from ANALYTICS.FORMATTERS.universe_list_formatter import UniverseListFormatter

logger = UnifiedLogger("AnalyticsReportService")


class AnalyticsReportService:
    """
    Канонический сервис формирования аналитических карточек, лидерборда,
    ранжирования монет и выгрузки графиков. Вся бизнес-логика и рендеринг аналитики
    сосредоточены здесь, освобождая Telegram-слой от вычислений (Dumb Executor).
    Делегирует специализированное форматирование модулям из ANALYTICS/FORMATTERS/.
    """

    def __init__(self, analytics_manager=None, stats_dir: Optional[Path] = None, ledgers_dir: Optional[Path] = None):
        self.analytics_manager = analytics_manager
        self.stats_dir: Path = stats_dir or ANALYTICS_STATS_DIR
        self.ledgers_dir: Path = ledgers_dir or ANALYTICS_LEDGERS_DIR

    def get_equity_chart_path(self, universe_id: Optional[str] = None) -> Optional[Path]:
        """Генерирует график эквити купола или портфеля и возвращает путь к файлу."""
        try:
            chart_file_str = generate_equity_curve(universe_id=universe_id)
            if chart_file_str and Path(chart_file_str).exists():
                return Path(chart_file_str)
        except Exception as e:
            logger.error(f"[REPORT_SERVICE] Error generating equity chart: {e}")
        return None

    async def get_equity_chart_path_async(self, universe_id: Optional[str] = None) -> Optional[Path]:
        """Неблокирующий вызов генератора графика эквити через asyncio.to_thread (Протокол Самсонова)."""
        return await asyncio.to_thread(self.get_equity_chart_path, universe_id=universe_id)

    def get_coin_chart_path(self, symbol: str, universe_id: Optional[str] = None) -> Optional[Path]:
        """Генерирует график баланса по конкретной монете и возвращает путь к файлу."""
        try:
            chart_file_str = generate_balance_curve(symbol=symbol, universe_id=universe_id)
            if chart_file_str and Path(chart_file_str).exists():
                return Path(chart_file_str)
        except Exception as e:
            logger.error(f"[REPORT_SERVICE] Error generating coin chart for {symbol}: {e}")
        return None

    async def get_coin_chart_path_async(self, symbol: str, universe_id: Optional[str] = None) -> Optional[Path]:
        """Неблокирующий вызов генератора графика баланса монеты через asyncio.to_thread (Протокол Самсонова)."""
        return await asyncio.to_thread(self.get_coin_chart_path, symbol=symbol, universe_id=universe_id)

    def get_ledger_file_path(self, universe_id: Optional[str] = None) -> Optional[Path]:
        """Возвращает путь к текстовому леджеру сделок, если он существует и не пуст."""
        if universe_id:
            cand = self.ledgers_dir / f"trades_ledger_{universe_id}.txt"
            if cand.exists() and cand.stat().st_size > 60:
                return cand
            matches = list(self.ledgers_dir.glob(f"trades_ledger_{universe_id}*.txt"))
            found = [m for m in matches if m.stat().st_size > 60]
            if found:
                return found[0]

        general = self.ledgers_dir / "trades_ledger.txt"
        if general.exists() and general.stat().st_size > 60:
            return general
        return None

    @staticmethod
    def format_trade_velocity_block(vel_dict: Optional[Dict[str, Any]], title: str = "Trade Velocity (Частота сделок)") -> str:
        """Форматирует информационный блок Trade Velocity строго по данным без скрытого хардкода."""
        return BigBoardFormatter.format_trade_velocity_block(vel_dict, title=title)

    def render_universe_analytics_card(self, current_uid: str, univ_name: str) -> Tuple[str, List[List[Dict[str, str]]]]:
        """Формирует HTML-текст и кнопки карточки детальной аналитики купола."""
        return BigBoardFormatter.render_universe_analytics_card(
            stats_dir=self.stats_dir,
            current_uid=current_uid,
            univ_name=univ_name,
            analytics_manager=self.analytics_manager,
        )

    def render_portfolio_big_board(
        self, current_uid: str, univ_name: str, active_strats: Optional[dict] = None
    ) -> Tuple[str, List[List[Dict[str, str]]]]:
        """Формирует HTML-текст и кнопки портфельного Биг Борда."""
        return BigBoardFormatter.render_portfolio_big_board(
            stats_dir=self.stats_dir,
            current_uid=current_uid,
            univ_name=univ_name,
            active_strats=active_strats,
            analytics_manager=self.analytics_manager,
        )

    def render_full_report_text(self, current_uid: str, univ_name: str) -> str:
        """Формирует полный детальный текстовый отчет для выгрузки в файл TXT со всеми глобальными и per-coin метриками."""
        return BigBoardFormatter.render_full_report_text(
            stats_dir=self.stats_dir,
            current_uid=current_uid,
            univ_name=univ_name,
        )

    def render_ranking_report(
        self, current_uid: str, univ_name: str, criterion: str = "net", active_symbols: Optional[List[str]] = None
    ) -> Tuple[List[str], List[List[Dict[str, str]]]]:
        """Формирует отчет рейтинга монет по выбранному критерию (net, rec, abs_rec)."""
        return CoinCardFormatter.render_ranking_report(
            stats_dir=self.stats_dir,
            current_uid=current_uid,
            univ_name=univ_name,
            criterion=criterion,
            active_symbols=active_symbols,
        )

    def render_coin_detail_report(self, current_uid: str, univ_name: str, symbol: str, is_active: bool) -> Optional[str]:
        """Формирует текстовый отчет детализации по конкретной монете."""
        return CoinCardFormatter.render_coin_detail_report(
            stats_dir=self.stats_dir,
            current_uid=current_uid,
            univ_name=univ_name,
            symbol=symbol,
            is_active=is_active,
        )

    def render_coin_menu(
        self, current_uid: str, univ_name: str, active_symbols: List[str]
    ) -> Tuple[str, List[List[Dict[str, str]]]]:
        """Формирует меню выбора монеты купола со статусами активности."""
        return CoinCardFormatter.render_coin_menu(
            stats_dir=self.stats_dir,
            current_uid=current_uid,
            univ_name=univ_name,
            active_symbols=active_symbols,
        )

    def render_universes_list(
        self,
        current_uid: Optional[str],
        active_strats: Dict[str, Any],
        page: int = 1,
        page_size: int = 5,
    ) -> Tuple[str, List[List[Dict[str, str]]]]:
        """Формирует презентационный текст и кнопки-тумблеры [ВКЛ / ВЫКЛ] для куполов стратегий."""
        return UniverseListFormatter.render_universes_list(
            current_uid=current_uid,
            active_strats=active_strats,
            page=page,
            page_size=page_size,
        )

    @classmethod
    def render_universe_control_panel(
        cls,
        uid: str,
        s_cfg: Dict[str, Any],
        page: int = 1,
    ) -> Tuple[str, List[List[Dict[str, str]]]]:
        """Алиас: прямое перенаправление на чистый список стратегий с тумблерами."""
        return cls().render_universes_list(current_uid=uid, active_strats={uid: s_cfg}, page=page)

    @staticmethod
    def render_cheat_sheet() -> str:
        """Возвращает шпаргалку (Cheat Sheet) по метрикам аналитики."""
        return BigBoardFormatter.render_cheat_sheet()
