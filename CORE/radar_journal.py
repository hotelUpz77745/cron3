# ==============================================================================
# Path: CORE/radar_journal.py
# Role: Журналирование ротаций монет и формирование отчетов телеметрии Радара
# ==============================================================================

from __future__ import annotations
import contextlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from consts import CACHE_DIR, ANALYTICS_LEDGERS_DIR
from c_log import UnifiedLogger
from c_utils import Utils

logger = UnifiedLogger("RadarJournal")


class RadarJournal:
    """
    Класс журнала ротаций:
    1. Ведение структурированного JSON-кэша (CACHE/rotations_log.json).
    2. Ведение аудиторского текстового леджера (ANALYTICS/LEDGERS/rotations_ledger.txt).
    3. Формирование сводок и телеметрических отчетов для Telegram.
    """

    @staticmethod
    def record_rotation(
        strat_id: str,
        old_symbol: str,
        new_symbol: str,
        old_score: float,
        new_score: float,
        lead_pct: float,
        matched_rule: str,
        mode: str
    ) -> Dict[str, Any]:
        """Логирует смену монет в CACHE/rotations_log.json и текстовый леджер ANALYTICS/LEDGERS/rotations_ledger.txt."""
        now_dt = datetime.now(timezone.utc)
        ts_utc = now_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        entry = {
            "timestamp_utc": ts_utc,
            "timestamp_ms": int(now_dt.timestamp() * 1000),
            "strat_id": strat_id,
            "old_symbol": old_symbol,
            "new_symbol": new_symbol,
            "old_score": round(float(old_score), 2),
            "new_score": round(float(new_score), 2),
            "lead_pct": round(float(lead_pct), 1),
            "matched_rule": matched_rule,
            "mode": mode
        }
        try:
            cache_file = CACHE_DIR / "rotations_log.json"
            history = []
            if cache_file.exists():
                with contextlib.suppress(Exception):
                    history = json.loads(cache_file.read_text(encoding="utf-8"))
            if not isinstance(history, list):
                history = []
            history.append(entry)
            Utils.atomic_write_json(cache_file, history, indent=4)

            ledger_file = ANALYTICS_LEDGERS_DIR / "rotations_ledger.txt"
            if not ledger_file.exists():
                ledger_file.parent.mkdir(parents=True, exist_ok=True)
                ledger_file.write_text("Timestamp (UTC);Strategy;Old Symbol;Old Score;New Symbol;New Score;Lead %;Rule;Mode\n", encoding="utf-8")
            with open(ledger_file, "a", encoding="utf-8") as f:
                f.write(f"{ts_utc};{strat_id};{old_symbol};{old_score:.2f};{new_symbol};{new_score:.2f};{lead_pct:+.1f}%;{matched_rule};{mode}\n")
            logger.info(f"📜 [RadarJournal] Зафиксирована смена монет: {old_symbol} -> {new_symbol} (Скор: {old_score:.2f} -> {new_score:.2f}, {lead_pct:+.1f}%)")
        except Exception as e:
            logger.error(f"[RadarJournal] Ошибка сохранения журнала ротаций: {e}")

        return entry

    @staticmethod
    def get_history(limit: int = 20) -> List[Dict[str, Any]]:
        """Возвращает историю ротаций из CACHE/rotations_log.json."""
        cache_file = CACHE_DIR / "rotations_log.json"
        if not cache_file.exists():
            return []
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            return data[-limit:] if isinstance(data, list) else []
        except Exception:
            return []

    @staticmethod
    def format_telemetry_report(
        strat_id: str,
        runtime_hours: float,
        current_symbols: List[str],
        top_candidates: List[Dict[str, Any]],
        recent_rotations: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        """Формирует текстовый отчет по состоянию Радара и скорам Самсонова."""
        lines = [
            f"🧭 <b>РАДАР ШТУРМАНА: [{strat_id}]</b>",
            f"⏱ Время непрерывной работы: <b>{runtime_hours:.2f} ч.</b>",
            f"🎯 Текущая боевая обойма: {', '.join(current_symbols) if current_symbols else 'пусто'}",
            "",
            "<b>ТОП КАНДИДАТОВ ВОРОНКИ САМСОНОВА:</b>",
        ]

        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"]
        for idx, m in enumerate(top_candidates[:8]):
            icon = medals[idx] if idx < len(medals) else "▫️"
            sym = m["symbol"]
            score = m["samsonov_score"]
            real = m["realized_net"]
            tag = " <i>[В СТРОЮ]</i>" if sym in current_symbols else ""
            lines.append(f"{icon} <b>{sym}</b>: Скор <b>{score:.1f}</b> (Realized: {real:+.2f}$){tag}")

        if recent_rotations:
            lines.append("")
            lines.append("<b>ИСТОРИЯ РОТАЦИЙ ОБОЙМЫ:</b>")
            for r in reversed(recent_rotations[-3:]):
                lines.append(f"• <code>{r['timestamp_utc']}</code>: {r['old_symbol']} ➔ {r['new_symbol']} (+{r['lead_pct']:.1f}%)")

        return "\n".join(lines)
