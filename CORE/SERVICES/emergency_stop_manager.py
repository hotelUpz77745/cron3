# CORE/SERVICES/emergency_stop_manager.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.SERVICES.emergency_stop_manager
ROLE: Управление аварийным стоп-краном портфеля (Basket Equity Cutoff) и спящим режимом
==============================================================================
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from consts import CACHE_DIR
from c_log import UnifiedLogger
from c_utils import Utils

logger = UnifiedLogger("EmergencyStopManager")


class EmergencyStopManager:
    """
    Управляет состоянием аварийного стоп-крана корзины (Basket Equity Cutoff):
    - Персистентное хранение статуса в CACHE/iteration_state.json под ключом __emergency_stop__
    - Проверка активности стоп-крана
    - Нормализация и валидация порога просадки (max_drawdown_from_start_pct)
    - Сброс стоп-крана оператором
    """

    CACHE_FILE = CACHE_DIR / "iteration_state.json"
    STOP_KEY = "__emergency_stop__"

    @classmethod
    def normalize_dd_pct(cls, val: Any) -> Optional[float]:
        """
        Нормализует значение допустимой просадки от стартового баланса в процентах.
        Поддерживает:
        - Положительные и отрицательные числа (например, 45.0, -45.0, 37.5, -37.5) -> abs(float(val))
        - Строковые представления ("45.0", "-45")
        - null / None / некорректные значения -> None (опция отключена)
        """
        if val is None:
            return None
        try:
            val_f = float(val)
            pct = round(abs(val_f), 2)
            if pct <= 0.0 or pct >= 100.0:
                return None
            return pct
        except (ValueError, TypeError):
            return None

    @classmethod
    def is_active(cls) -> Tuple[bool, Dict[str, Any]]:
        """
        Проверяет, активен ли аварийный стоп-кран.
        Возвращает: (is_active: bool, stop_details: dict)
        """
        if not cls.CACHE_FILE.exists():
            return False, {}
        try:
            data = json.loads(cls.CACHE_FILE.read_text(encoding="utf-8"))
            st = data.get(cls.STOP_KEY, {})
            if isinstance(st, dict) and st.get("active", False) is True:
                return True, st
        except Exception as e:
            logger.warning(f"[EMERGENCY_STOP] Ошибка чтения {cls.CACHE_FILE.name}: {e}")
        return False, {}

    @classmethod
    def set_active(cls, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Активирует аварийный стоп-кран и сохраняет детали в CACHE/iteration_state.json.
        """
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        payload = {
            "active": True,
            "triggered_at": now_utc,
        }
        if details and isinstance(details, dict):
            payload.update(details)

        try:
            cls.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = json.loads(cls.CACHE_FILE.read_text(encoding="utf-8")) if cls.CACHE_FILE.exists() else {}
            data[cls.STOP_KEY] = payload
            Utils.atomic_write_json(cls.CACHE_FILE, data, indent=4)
            logger.critical(
                f"🚨 [EMERGENCY_STOP] Аварийный стоп-кран АКТИВИРОВАН! "
                f"Стратегия: {payload.get('strategy_id', 'Unknown')} | "
                f"Просадка: {payload.get('cur_dd_pct', 0.0):.2f}% (Лимит: {payload.get('limit_pct', 0.0)}%)"
            )
        except Exception as e:
            logger.error(f"[EMERGENCY_STOP] Ошибка записи состояния стоп-крана: {e}")

        return payload

    @classmethod
    def clear(cls) -> None:
        """
        Сбрасывает аварийный стоп-кран (снимает стоп-кран по решению оператора).
        """
        if not cls.CACHE_FILE.exists():
            return
        try:
            data = json.loads(cls.CACHE_FILE.read_text(encoding="utf-8"))
            if cls.STOP_KEY in data:
                data[cls.STOP_KEY]["active"] = False
                data[cls.STOP_KEY]["cleared_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                Utils.atomic_write_json(cls.CACHE_FILE, data, indent=4)
                logger.info("✅ [EMERGENCY_STOP] Аварийный стоп-кран успешно СБРОШЕН оператором.")
        except Exception as e:
            logger.error(f"[EMERGENCY_STOP] Ошибка сброса стоп-крана: {e}")
