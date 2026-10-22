# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\CORE\ENTRY\signal.py
# Role: signal.py module

# ==============================================================================
# Path: CORE/ENTRY/signal.py
# Role: Обработка сигналов и логика первичного входа в позицию
# ==============================================================================
# Role: Планировщик
# Responsibilities:
# - Конвертация интервалов
# - Получение текущего времени в UTC
# - Проверка новой свечи 

from datetime import datetime, timezone
import pytz

import copy
from datetime import datetime, timezone
import time
import secrets
from typing import Dict, Any, Optional

from consts import (
    DEFAULT_SMART_GRACE,
    DEFAULT_START_PERIOD_SEC,
    DEFAULT_SHIFT_INTERVAL_SEC,
)

# Backward compatibility aliases
SHIFT_INTERVAL = DEFAULT_SHIFT_INTERVAL_SEC
GRACE_PERIOD_SEC = DEFAULT_START_PERIOD_SEC

class TimeControl:    
    def __init__(self, interval: str = "5m", smart_grace_cfg: Optional[Dict[str, Any]] = None):
        self.interval = interval
        self.interval_seconds = self.interval_to_seconds(interval)
        self.smart_grace_cfg = copy.deepcopy(smart_grace_cfg) if smart_grace_cfg is not None else copy.deepcopy(DEFAULT_SMART_GRACE)
        self.shift_interval_sec = self.smart_grace_cfg.get("shift_interval_sec", DEFAULT_SHIFT_INTERVAL_SEC)
        self.start_period_sec = float(self.smart_grace_cfg.get("start_period_sec", DEFAULT_START_PERIOD_SEC))
        self.last_fetch_timestamp = None
        self.window_open_until = 0
        self._candle_shifts: Dict[tuple, float] = {}

    @property
    def interval_sec(self) -> int:
        return self.interval_seconds
    
    def get_date_time_now(self):
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%d %H:%M:%S UTC")

    def milliseconds_to_datetime(self, milliseconds):
        seconds = milliseconds / 1000
        dt = datetime.fromtimestamp(seconds, timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S") + f".{int(milliseconds % 1000):03d} UTC"

    @staticmethod
    def interval_to_seconds(interval: str, default: int = 300) -> int:
        """
        Преобразует строковый интервал в количество секунд.
        Поддерживает произвольные таймфреймы (1m, 5m, 7m, 10m, 1h, 1d и т.д.)
        динамически вычисляя количество секунд из единицы измерения.
        """
        if not interval:
            return default
        s = str(interval).strip().lower()
        try:
            if s.endswith("s"):
                return int(s[:-1])
            elif s.endswith("m"):
                return int(s[:-1]) * 60
            elif s.endswith("h"):
                return int(s[:-1]) * 3600
            elif s.endswith("d"):
                return int(s[:-1]) * 86400
            elif s.endswith("w"):
                return int(s[:-1]) * 604800
            elif s.isdigit():
                return int(s)
        except (ValueError, TypeError):
            pass
        return default

    @staticmethod
    def match_rule(rule: str, count: int) -> bool:
        """
        Проверяет совпадение правила с количеством закрытых сделок.
        Поддерживает: '>=X', '<=X', '==X', '>X', '<X', 'X'.
        """
        rule = rule.strip()
        try:
            if rule.startswith(">="):
                return count >= int(rule[2:])
            if rule.startswith("<="):
                return count <= int(rule[2:])
            if rule.startswith("=="):
                return count == int(rule[2:])
            if rule.startswith(">"):
                return count > int(rule[1:])
            if rule.startswith("<"):
                return count < int(rule[1:])
            if rule.isdigit():
                return count == int(rule)
        except ValueError:
            pass
        return False

    @classmethod
    def resolve_shift_interval(cls, shift_cfg: Any) -> float:
        """
        Разрешает интервал сдвига сердцебиения (shift_interval_sec).
        Поддерживает:
        1. Рандомизированный диапазон [low, high] или [high, low] (например, [5, 16] или [16, 5]):
           генерирует криптографически стойкое равномерное случайное число (uniform random)
           с дробными квантами (до 2 знаков) через аппаратную энтропию secrets.SystemRandom().
        2. Фиксированное число (int / float, например, 13).
        """
        if isinstance(shift_cfg, (list, tuple)) and len(shift_cfg) == 2:
            try:
                low = float(min(shift_cfg))
                high = float(max(shift_cfg))
                if high > 0 and high >= low:
                    return round(secrets.SystemRandom().uniform(low, high), 2)
            except (ValueError, TypeError):
                pass
        try:
            return float(shift_cfg)
        except (ValueError, TypeError):
            return float(DEFAULT_SHIFT_INTERVAL_SEC)

    def is_new_interval(
        self,
        closed_count: int = 0,
        smart_grace_cfg: Optional[Dict[str, Any]] = None,
        current_timestamp: Optional[float] = None,
    ) -> tuple[bool, float]:
        """
        Возвращает (is_signal, current_grace_period), где is_signal == True, если текущее время 
        находится в пределах динамического окна с начала текущего интервала (свечи).
        """
        if not self.interval_seconds:
            return False, 0.0

        cfg = smart_grace_cfg if smart_grace_cfg is not None else self.smart_grace_cfg
        base_period = float(cfg.get("start_period_sec", self.start_period_sec))
        shift_cfg = cfg.get("shift_interval_sec", self.shift_interval_sec)
        increments = cfg.get("increments_card", {})

        matched_increment = 0.0
        for rule, inc in increments.items():
            if self.match_rule(rule, closed_count):
                inc_val = float(inc)
                if inc_val > matched_increment:
                    matched_increment = inc_val

        grace_period = base_period + matched_increment

        if current_timestamp is None:
            now = datetime.now(timezone.utc)
            current_timestamp = now.timestamp()

        # Кэширование сдвига на ID текущей свечи (интервала) для стабильного порога внутри одной свечи
        candle_id = int(current_timestamp // self.interval_seconds)
        cfg_key = (candle_id, tuple(shift_cfg) if isinstance(shift_cfg, (list, tuple)) else shift_cfg)

        if cfg_key in self._candle_shifts:
            shift_interval = self._candle_shifts[cfg_key]
        else:
            if len(self._candle_shifts) > 20:
                self._candle_shifts.clear()
            shift_interval = self.resolve_shift_interval(shift_cfg)
            self._candle_shifts[cfg_key] = shift_interval

        virtual_time = current_timestamp + shift_interval
        nearest_timestamp = (int(virtual_time) // self.interval_seconds) * self.interval_seconds

        return (virtual_time - nearest_timestamp) < grace_period, grace_period

if __name__ == "__main__":
    print(f"Запуск тестового цикла (интервал 5m, SHIFT={DEFAULT_SHIFT_INTERVAL_SEC}s, START_GRACE={DEFAULT_START_PERIOD_SEC}s)...")
    tc = TimeControl(interval="5m")
    
    try:
        while True:
            is_signal, grace = tc.is_new_interval()
            if is_signal:
                now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                print(f"СИГНАЛ! Точное время (UTC): {now_utc}")
            time.sleep(1)
    except KeyboardInterrupt:
        print("Тест завершен.")