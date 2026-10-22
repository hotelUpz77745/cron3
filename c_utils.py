# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\c_utils.py
# Role: c_utils.py module

# ==============================================================================
# Path: c_utils.py
# Role: Утилитарные и вспомогательные функции
# ==============================================================================

from typing import Dict, Any, List, Optional, Tuple, Union, Set
from datetime import datetime, timezone
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
    def strict_float(value: Any, field_name: str = "value") -> float:
        """
        Строгое преобразование в float по Протоколу Самсонова (Rule 2: Fail-Fast).
        Выбрасывает ValueError при None или неконвертируемом значении.
        Предотвращает фальшивые нули в финансовых расчетах (объемы, цены, invest_size).
        """
        if value is None:
            raise ValueError(f"Strict float validation error: field '{field_name}' is None")
        try:
            return float(value)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"Strict float validation error: field '{field_name}' has invalid value {value!r}: {e}"
            ) from e

    @staticmethod
    def strict_int(value: Any, field_name: str = "value") -> int:
        """
        Строгое преобразование в int по Протоколу Самсонова (Rule 2: Fail-Fast).
        Выбрасывает ValueError при None или неконвертируемом значении.
        """
        if value is None:
            raise ValueError(f"Strict int validation error: field '{field_name}' is None")
        try:
            return int(value)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"Strict int validation error: field '{field_name}' has invalid value {value!r}: {e}"
            ) from e

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
    def get_current_time(sim_time: Optional[float] = None) -> float:
        """Возвращает текущее время: виртуальное время симуляции/бэктеста или системное time.time()."""
        return float(sim_time) if sim_time is not None else time.time()

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
        
        Принцип:
        1. Утилита определяет сторону, у которой НЕТ поля наследования ('_inheritance_base_side',
           '__inheritance_base_side', '_override', 'override'), и принимает ее за базовую (LONG или SHORT).
        2. Вторая сторона, у которой задано это поле (например, "_inheritance_base_side": true
           или "_override": false / "override": {}), наследует конфигурацию базовой стороны.
        3. Сохраняются все явные поля целевой стороны (например, "enabled": false / "enable": false).
        4. Служебные ключи наследования удаляются из результирующего конфига.
        5. Ключи активности 'enable' и 'enabled' синхронизируются.
        """
        if not isinstance(data, dict):
            return data

        # Рекурсивный обход вложенных словарей (например, шаблонов или словаря вселенных)
        resolved = copy.deepcopy(data)
        for k, v in list(resolved.items()):
            if isinstance(v, dict):
                resolved[k] = Utils.resolve_side_inheritance(v)

        INHERITANCE_KEYS = {
            "_inheritance_base_side",
            "__inheritance_base_side",
            "_override",
            "override",
            "_comment_override",
        }

        def _has_inheritance_marker(side_dict: Any) -> bool:
            if not isinstance(side_dict, dict):
                return False
            if "_inheritance_base_side" in side_dict:
                v = side_dict["_inheritance_base_side"]
                return v is not False and v is not None
            if "__inheritance_base_side" in side_dict:
                v = side_dict["__inheritance_base_side"]
                return v is not False and v is not None
            if "_override" in side_dict:
                v = side_dict["_override"]
                return v is False or isinstance(v, dict)
            if "override" in side_dict:
                v = side_dict["override"]
                return v is False or isinstance(v, dict)
            return False

        def _get_overrides_dict(side_dict: dict) -> Optional[dict]:
            for k in ("_inheritance_base_side", "__inheritance_base_side", "_override", "override"):
                if k in side_dict and isinstance(side_dict[k], dict):
                    return side_dict[k]
            return None

        def _apply_side_inheritance(source_side_name: str, target_side_name: str):
            target = resolved[target_side_name]
            source = resolved[source_side_name]
            if not isinstance(target, dict) or not isinstance(source, dict):
                return
            if not _has_inheritance_marker(target):
                return

            overrides_dict = _get_overrides_dict(target)
            # Начинаем с полной копии базовой стороны
            merged = copy.deepcopy(source)

            # Переносим все явные ключи целевой стороны, кроме служебных ключей наследования
            for tk, tv in target.items():
                if tk not in INHERITANCE_KEYS:
                    merged[tk] = copy.deepcopy(tv)

            # Применяем словарь переопределений (overrides), если задан
            if overrides_dict:
                merged = Utils.deep_merge(merged, overrides_dict)

            # Гарантируем отсутствие служебных ключей
            for k in INHERITANCE_KEYS:
                merged.pop(k, None)

            # Синхронизируем enable и enabled для 100% совместимости
            if "enabled" in target and "enable" not in target:
                merged["enable"] = bool(target["enabled"])
                merged["enabled"] = bool(target["enabled"])
            elif "enable" in target and "enabled" not in target:
                merged["enable"] = bool(target["enable"])
                merged["enabled"] = bool(target["enable"])
            elif "enabled" in merged and "enable" not in merged:
                merged["enable"] = bool(merged["enabled"])
            elif "enable" in merged and "enabled" not in merged:
                merged["enabled"] = bool(merged["enable"])

            resolved[target_side_name] = merged

        if "LONG" in resolved and "SHORT" in resolved:
            short_req = _has_inheritance_marker(resolved["SHORT"])
            long_req = _has_inheritance_marker(resolved["LONG"])

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
    def write_json_file(file_path: Union[str, Path], data: dict):
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
        if getattr(state, "_is_simulated", False) or getattr(state, "is_simulated", False):
            return True

        max_cycles = int(timeout_sec / poll_interval)
        wait_cycles = 0
        while state.avg_entry_price == state.pre_avg_price and wait_cycles < max_cycles:
            await asyncio.sleep(poll_interval)
            wait_cycles += 1
        
        return state.avg_entry_price != state.pre_avg_price

    @staticmethod
    def extract_strategy_symbols(sdata: Any) -> List[str]:
        """
        Универсально и надежно извлекает список торговых символов стратегии:
        1. Если у объекта есть атрибут .symbols -> возвращает его.
        2. Если в словаре есть ключ 'symbols' (список) -> возвращает его.
        3. Если монеты заданы как ключи словарей LONG.invest_size / SHORT.invest_size / invest_size -> извлекает их.
        """
        if hasattr(sdata, "symbols"):
            syms = getattr(sdata, "symbols")
            if isinstance(syms, (list, tuple, set)):
                return [str(s).upper().strip() for s in syms if s and str(s).strip()]
        if not isinstance(sdata, dict):
            return []
        if "symbols" in sdata and isinstance(sdata["symbols"], list) and sdata["symbols"]:
            return [str(s).upper().strip() for s in sdata["symbols"] if s and str(s).strip()]
        res: List[str] = []
        for side in ("LONG", "SHORT"):
            side_inv = (sdata.get(side) or {}).get("invest_size")
            if isinstance(side_inv, dict):
                for k in side_inv.keys():
                    k_clean = str(k).upper().strip()
                    if k_clean and k_clean not in res and k_clean not in ("DEFAULT", "LONG", "SHORT"):
                        res.append(k_clean)
        if not res and isinstance(sdata.get("invest_size"), dict):
            for k in sdata["invest_size"].keys():
                k_clean = str(k).upper().strip()
                if k_clean and k_clean not in res and k_clean not in ("DEFAULT", "LONG", "SHORT"):
                    res.append(k_clean)
        return res

# можно и нужно добавить метод расчета количества контракта для ордеров с соблюдением округлителей.