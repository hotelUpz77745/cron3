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
        self.shift_interval_sec = int(self.smart_grace_cfg.get("shift_interval_sec", DEFAULT_SHIFT_INTERVAL_SEC))
        self.start_period_sec = float(self.smart_grace_cfg.get("start_period_sec", DEFAULT_START_PERIOD_SEC))
        self.last_fetch_timestamp = None
        self.window_open_until = 0

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

    def interval_to_seconds(self, interval):
        """
        Преобразует строковый интервал Binance в количество секунд.
        """
        mapping = {
            "1m": 60,
            "2m": 120,
            "3m": 180,
            "4m": 240,
            "5m": 300,
            "6m": 360,
            "7m": 420,
            "10m": 600,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "2h": 7200,
            "4h": 14400,
            "1d": 86400,
        }
        return mapping.get(interval, 300)

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

    def is_new_interval(self, closed_count: int = 0, smart_grace_cfg: Optional[Dict[str, Any]] = None) -> tuple[bool, float]:
        """
        Возвращает (is_signal, current_grace_period), где is_signal == True, если текущее время 
        находится в пределах динамического окна с начала текущего интервала (свечи).
        """
        if not self.interval_seconds:
            return False, 0.0

        cfg = smart_grace_cfg if smart_grace_cfg is not None else self.smart_grace_cfg
        base_period = float(cfg.get("start_period_sec", self.start_period_sec))
        shift_interval = int(cfg.get("shift_interval_sec", self.shift_interval_sec))
        increments = cfg.get("increments_card", {})

        matched_increment = 0.0
        for rule, inc in increments.items():
            if self.match_rule(rule, closed_count):
                inc_val = float(inc)
                if inc_val > matched_increment:
                    matched_increment = inc_val

        grace_period = base_period + matched_increment

        now = datetime.now(timezone.utc)
        current_timestamp = int(now.timestamp())

        virtual_time = current_timestamp + shift_interval
        nearest_timestamp = (virtual_time // self.interval_seconds) * self.interval_seconds

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