# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\c_utils.py
# Role: c_utils.py module

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
import copy
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

        filters = symbol_data.get("filters", [])
        lot_size_filter = next((f for f in filters if f.get("filterType") == "LOT_SIZE"), None)
        price_filter = next((f for f in filters if f.get("filterType") == "PRICE_FILTER"), None)

        def count_decimal_places(number_str):
            if number_str and '.' in str(number_str):
                return len(str(number_str).rstrip('0').split('.')[-1])
            return 0

        if lot_size_filter and "stepSize" in lot_size_filter:
            qty_precision = count_decimal_places(lot_size_filter["stepSize"])
        else:
            qty_precision = int(symbol_data.get("quantityPrecision", 3))

        if price_filter and "tickSize" in price_filter:
            price_precision = count_decimal_places(price_filter["tickSize"])
        else:
            price_precision = int(symbol_data.get("pricePrecision", 2))

        return qty_precision, price_precision

    @staticmethod
    def deep_merge(base: dict, update: dict) -> dict:
        """
        Рекурсивное объединение двух словарей. Возвращает новый объединенный словарь.
        Значения из update переопределяют значения из base.
        """
        if not isinstance(base, dict):
            return copy.deepcopy(update)
        if not isinstance(update, dict):
            return copy.deepcopy(update)

        result = copy.deepcopy(base)
        for k, v in update.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = Utils.deep_merge(result[k], v)
            else:
                result[k] = copy.deepcopy(v)
        return result

    @staticmethod
    def resolve_side_inheritance(data: Any) -> Any:
        """
        Разрешает компактное наследование настроек сторон (SHORT наследует LONG или наоборот).
        Поддерживает:
        1. Флаг `"_override": false` или `"override": false` — сторона полностью наследует доступную
           базовую сторону (как правило LONG, так как исторически она прописана сверху).
        2. Словарь `override: {}` (или `_override: {}`) — наследование с возможностью точечных
           переопределений параметров.
        3. Сохранение явных полей целевой стороны (например "enable": false) и удаление служебных ключей.
        """
        if not isinstance(data, dict):
            return data

        # Рекурсивный обход вложенных словарей (например, шаблонов или словаря вселенных)
        resolved = copy.deepcopy(data)
        for k, v in list(resolved.items()):
            if isinstance(v, dict):
                resolved[k] = Utils.resolve_side_inheritance(v)

        def _is_inheritance_requested(side_dict: Any) -> bool:
            if not isinstance(side_dict, dict):
                return False
            # Проверяем "_override"
            if "_override" in side_dict:
                v = side_dict["_override"]
                if v is False or isinstance(v, dict):
                    return True
                return False
            # Проверяем "override"
            if "override" in side_dict:
                v = side_dict["override"]
                if v is False or isinstance(v, dict):
                    return True
                return False
            return False

        def _get_overrides_dict(side_dict: dict) -> Optional[dict]:
            if "_override" in side_dict and isinstance(side_dict["_override"], dict):
                return side_dict["_override"]
            if "override" in side_dict and isinstance(side_dict["override"], dict):
                return side_dict["override"]
            return None

        def _apply_side_inheritance(source_side_name: str, target_side_name: str):
            target = resolved[target_side_name]
            source = resolved[source_side_name]
            if not isinstance(target, dict) or not isinstance(source, dict):
                return
            if not _is_inheritance_requested(target):
                return

            overrides_dict = _get_overrides_dict(target)
            # Начинаем с полной копии стороны-источника
            merged = copy.deepcopy(source)

            # Переносим все явные ключи целевой стороны, кроме служебных ключей наследования
            for tk, tv in target.items():
                if tk not in ("override", "_override", "_comment_override"):
                    merged[tk] = copy.deepcopy(tv)

            # Применяем словарь переопределений (overrides), если задан
            if overrides_dict:
                merged = Utils.deep_merge(merged, overrides_dict)

            # Гарантируем отсутствие служебных ключей
            merged.pop("override", None)
            merged.pop("_override", None)
            merged.pop("_comment_override", None)

            resolved[target_side_name] = merged

        if "LONG" in resolved and "SHORT" in resolved:
            short_req = _is_inheritance_requested(resolved["SHORT"])
            long_req = _is_inheritance_requested(resolved["LONG"])

            if short_req and not long_req:
                _apply_side_inheritance("LONG", "SHORT")
            elif long_req and not short_req:
                _apply_side_inheritance("SHORT", "LONG")
            elif short_req and long_req:
                # Если оба заявили наследование, приоритет отдается LONG как верхней/базовой стороне
                _apply_side_inheritance("LONG", "SHORT")

        return resolved

    @staticmethod
    def read_json_file(file_path: Union[str, Path]) -> dict:
        try:
            p = Path(file_path)
            if not p.exists():
                return {}
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            return Utils.resolve_side_inheritance(data)
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
    def atomic_write_text(file_path: Union[str, Path], text: str, retries: int = 7, delay: float = 0.05) -> bool:
        """
        Атомарная запись текста в файл с защитой от WinError 5 / WinError 32 (блокировки файлов на Windows).
        """
        p = Path(file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = p.with_suffix(f".tmp_{os.getpid()}_{int(time.time() * 1000) % 1000000}")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(text)

            for attempt in range(retries):
                try:
                    os.replace(tmp_path, p)
                    return True
                except (PermissionError, OSError) as e:
                    if attempt == retries - 1:
                        try:
                            shutil.copyfile(tmp_path, p)
                            if tmp_path.exists():
                                tmp_path.unlink()
                            return True
                        except Exception:
                            raise e
                    time.sleep(delay * (attempt + 1))
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
        return False

    @staticmethod
    def atomic_write_json(file_path: Union[str, Path], data: Any, indent: int = 4, retries: int = 7, delay: float = 0.05) -> bool:
        json_str = json.dumps(data, indent=indent, ensure_ascii=False)
        return Utils.atomic_write_text(file_path, json_str, retries=retries, delay=delay)

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