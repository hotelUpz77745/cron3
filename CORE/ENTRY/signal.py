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

import time
GRACE_PERIOD_SEC = 87.0 # Буфер времени от начала свечи, в пределах которого происходит проверка новых сигналов
SHIFT_INTERVAL = 13

class TimeControl:    
    def __init__(self, interval="5m"):
        self.interval_seconds = self.interval_to_seconds(interval)
        self.last_fetch_timestamp = None
        self.window_open_until = 0
    
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
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "2h": 7200,
            "4h": 14400,
            "1d": 86400,
        }
        return mapping.get(interval, 60)  # По умолчанию "1m"

    def is_new_interval(self, closed_count: int = 0, smart_grace_cfg: dict = None) -> tuple[bool, float]:
        """
        Возвращает (is_signal, current_grace_period), где is_signal == True, если текущее время 
        находится в пределах динамического окна с начала текущего интервала (свечи).
        """
        if not self.interval_seconds:
            return False, 0.0
            
        grace_period = 89.0
        if smart_grace_cfg:
            grace_period = smart_grace_cfg.get("start_period_sec", 90)
            increments_card = smart_grace_cfg.get("increments_card", {})
            
            # Find the best matching increment
            # Rules: <X, >X, >=X, <=X, ==X
            max_increment = 0
            for rule, inc in increments_card.items():
                try:
                    inc_val = float(inc)
                    matched = False
                    if rule.startswith(">="):
                        matched = closed_count >= int(rule[2:])
                    elif rule.startswith("<="):
                        matched = closed_count <= int(rule[2:])
                    elif rule.startswith("=="):
                        matched = closed_count == int(rule[2:])
                    elif rule.startswith(">"):
                        matched = closed_count > int(rule[1:])
                    elif rule.startswith("<"):
                        matched = closed_count < int(rule[1:])
                    elif rule.isdigit():
                        matched = closed_count == int(rule)
                        
                    if matched and inc_val > max_increment:
                        max_increment = inc_val
                except ValueError:
                    pass
            grace_period += max_increment
            
        now = datetime.now(timezone.utc)
        current_timestamp = int(now.timestamp())
        
        # Вычитаем SHIFT_INTERVAL (т.к. он отрицательный, это прибавит время), 
        # чтобы виртуально "опередить" время и вызвать сигнал раньше.
        virtual_time = current_timestamp + SHIFT_INTERVAL
        nearest_timestamp = (virtual_time // self.interval_seconds) * self.interval_seconds

        return (virtual_time - nearest_timestamp) < grace_period, grace_period

if __name__ == "__main__":
    
    print(f"Запуск тестового цикла (интервал 5m, SHIFT_INTERVAL={SHIFT_INTERVAL}, GRACE_PERIOD_SEC={GRACE_PERIOD_SEC})...")
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