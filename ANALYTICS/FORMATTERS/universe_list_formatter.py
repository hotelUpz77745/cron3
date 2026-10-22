# ==============================================================================
# Path: ANALYTICS/FORMATTERS/universe_list_formatter.py
# Role: Рендеринг списков куполов стратегий и кнопок-тумблеров переключения
# ==============================================================================

from __future__ import annotations

from typing import Any, Dict, List, Tuple


class UniverseListFormatter:
    """Форматтер презентационного слоя для списков куполов и кнопок-тумблеров."""

    @staticmethod
    def render_universes_list(
        current_uid: str,
        active_strats: Dict[str, Any],
        page: int = 1,
        page_size: int = 5,
    ) -> Tuple[str, List[List[Dict[str, str]]]]:
        """
        Формирует презентационный текст и чистую структуру кнопок-тумблеров [ВКЛ / ВЫКЛ] для куполов стратегий.
        Сеттер стратегий является прямым бинарным переключателем.
        """
        strats_items = [
            (sid, scfg)
            for sid, scfg in active_strats.items()
            if not sid.startswith("_") and (isinstance(scfg, dict) or hasattr(scfg, "raw_meta"))
        ]

        total_entries = len(strats_items)
        total_pages = max(1, (total_entries + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))

        start_i = (page - 1) * page_size
        end_i = min(start_i + page_size, total_entries)
        page_entries = strats_items[start_i:end_i]

        text_lines = [
            f"🌌 <b>Управление стратегиями (куполами)</b> (Стр. {page}/{total_pages})",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        ]

        buttons: List[List[Dict[str, str]]] = []

        for sid, scfg in page_entries:
            is_enabled = (
                bool(scfg.get("enabled", True))
                if isinstance(scfg, dict)
                else bool(getattr(scfg, "enabled", True))
            )

            raw_alias = ""
            if isinstance(scfg, dict):
                raw_alias = scfg.get("_comment_alias") or scfg.get("name") or sid
            else:
                raw_alias = getattr(scfg, "name", sid)

            short_name = raw_alias.split("[")[0].strip() if "[" in raw_alias else raw_alias
            if not short_name:
                short_name = sid

            status_icon = "🟢" if is_enabled else "⚪"
            status_text = "ВКЛЮЧЕНА" if is_enabled else "ОТКЛЮЧЕНА"
            text_lines.append(f"{status_icon} <b>{short_name}</b> — <b>{status_text}</b>")

            btn_label = f"🟢 ВКЛ | {short_name}" if is_enabled else f"⚪ ВЫКЛ | {short_name}"
            if len(btn_label) > 38:
                btn_label = btn_label[:36] + "…"
            buttons.append([{"text": btn_label, "callback_data": f"toggle_univ:{sid}:{page}"}])

        text_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        if total_pages > 1:
            nav_row = []
            if page > 1:
                nav_row.append({"text": "⬅️ Назад", "callback_data": f"univ_page:{page - 1}"})
            nav_row.append({"text": f"📄 {page}/{total_pages}", "callback_data": "univ_page:noop"})
            if page < total_pages:
                nav_row.append({"text": "Вперед ➡️", "callback_data": f"univ_page:{page + 1}"})
            buttons.append(nav_row)

        buttons.append([
            {"text": "🔄 Обновить", "callback_data": f"univ_page:{page}"},
            {"text": "🔙 В меню аналитики", "callback_data": "analytics_back_to_univ"},
        ])
        return "\n".join(text_lines), buttons
