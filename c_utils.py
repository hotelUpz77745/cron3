# ==============================================================================
# Path: c_utils.py
# Role: Утилитарные и вспомогательные функции
# ==============================================================================

from typing import *
from datetime import datetime
import asyncio
import re
import time
import hashlib
import json
import os
import shutil
from pathlib import Path
from decimal import Decimal, getcontext

from consts import PRECISION



getcontext().prec = PRECISION  # точность Decimal

# Разрешаем только латиницу и цифры
_SYMBOL_REGEX = re.compile(r"^[A-Z0-9]+$")


def now() -> int:
    """Return current timestamp in milliseconds."""
    return int(time.time() * 1000)


class Utils:        
    @staticmethod
    def safe_float(value: Any, default: float = 0.0) -> float:
        """Преобразует значение в float, если не удалось — возвращает default"""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
        
    @staticmethod
    def safe_int(value: Any, default: int = 0) -> int:
        """Преобразует значение в int, если не удалось — возвращает default"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
        
    @staticmethod
    def safe_round(value: Any, ndigits: int = 2, default: float = 0.0) -> float:
        """Безопасный round для None или нечисловых значений"""
        try:
            return round(float(value), ndigits)
        except (TypeError, ValueError):
            return default    

    @staticmethod
    def milliseconds_to_datetime(milliseconds):
        if milliseconds is None:
            return "N/A"
        try:
            ms = int(milliseconds)   # <-- приведение к int
            if milliseconds < 0: return "N/A"
        except (ValueError, TypeError):
            return "N/A"

        if ms > 1e10:  # похоже на миллисекунды
            seconds = ms / 1000
        else:
            seconds = ms

        dt = datetime.fromtimestamp(seconds, timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod    
    def format_duration(ms: int) -> str:
        """
        Конвертирует миллисекундную разницу в формат "Xh Ym" или "Xm" или "Xs".
        :param ms: длительность в миллисекундах
        """
        if ms is None:
            return ""
        
        total_seconds = ms // 1000
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60

        if hours > 0 and minutes > 0:
            return f"{hours}h {minutes}m"
        elif minutes > 0 and seconds > 0:
            return f"{minutes}m {seconds}s"
        elif minutes > 0:
            return f"{minutes}m"
        else:
            return f"{seconds}s"
        
    @staticmethod
    def to_human_digit(value):
        if value is None:
            # return "N/A"
            return None
        getcontext().prec = PRECISION
        dec_value = Decimal(str(value)).normalize()
        if dec_value == dec_value.to_integral():
            return format(dec_value, 'f')
        else:
            return format(dec_value, 'f').rstrip('0').rstrip('.')  
    
    @staticmethod
    def normalize_symbol(raw: str) -> Optional[str]:
        """
        Нормализует и валидирует символ.

        • strip
        • upper
        • запрещает кириллицу
        • разрешает только A-Z0-9
        """

        if not raw or not isinstance(raw, str):
            return None

        sym = raw.strip().upper()

        if not sym:
            return None

        # Проверка на кириллицу
        for ch in sym:
            if "А" <= ch <= "Я" or "а" <= ch <= "я":
                return None

        # Проверка на допустимые символы
        if not _SYMBOL_REGEX.match(sym):
            return None

        return sym

    @staticmethod
    def get_spec_precisions(symbol_info, symbol):
        if not symbol_info or not isinstance(symbol_info, dict) or "symbols" not in symbol_info:
            return None
            
        symbol_data = next((item for item in symbol_info.get("symbols", []) if item.get('symbol') == symbol), None)
        if not symbol_data:
            return None

        lot_size_filter = next((f for f in symbol_data["filters"] if f["filterType"] == "LOT_SIZE"), None)
        price_filter = next((f for f in symbol_data["filters"] if f["filterType"] == "PRICE_FILTER"), None)

        if not lot_size_filter or not price_filter:
            return

        def count_decimal_places(number_str):
            if '.' in number_str:
                return len(number_str.rstrip('0').split('.')[-1])
            return 0

        qty_precission = count_decimal_places(lot_size_filter['stepSize'])
        price_precision = count_decimal_places(price_filter['tickSize'])

        return qty_precission, price_precision

    @staticmethod
    def read_json_file(file_path: str) -> dict:
        try:
            p = Path(file_path)
            if not p.exists():
                return {}
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    @staticmethod
    def write_json_file(file_path: str, data: dict):
        try:
            p = Path(file_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
        except Exception:
            pass

    @staticmethod
    async def wait_for_fsm_sync(state, timeout_sec: float = 3.0, poll_interval: float = 0.01) -> bool:
        """
        Ожидает обновления средней цены входа от вебсокета (FSM sync).
        Возвращает True если цена обновилась (sync success), False если произошел таймаут.
        """
        max_cycles = int(timeout_sec / poll_interval)
        wait_cycles = 0
        while state.avg_entry_price == state.pre_avg_price and wait_cycles < max_cycles:
            await asyncio.sleep(poll_interval)
            wait_cycles += 1
        
        return state.avg_entry_price != state.pre_avg_price

# можно и нужно добавить метод расчета количества контракта для ордеров с соблюдением округлителей.