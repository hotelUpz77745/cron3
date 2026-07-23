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

GRACE_PERIOD_SEC = 89.0 # Буфер времени от начала свечи, в пределах которого происходит проверка новых сигналов
SHIFT_INTERVAL = 11

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
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "2h": 7200,
            "4h": 14400,
            "1d": 86400,
        }
        return mapping.get(interval, 60)  # По умолчанию "1m"

    def is_new_interval(self) -> bool:
        """
        Возвращает True, если текущее время находится в пределах первой минуты (60 сек)
        с начала текущего интервала (свечи).
        """
        if not self.interval_seconds:
            return False
            
        now = datetime.now(timezone.utc)
        current_timestamp = int(now.timestamp())
        
        # Вычитаем SHIFT_INTERVAL (т.к. он отрицательный, это прибавит время), 
        # чтобы виртуально "опередить" время и вызвать сигнал раньше.
        virtual_time = current_timestamp + SHIFT_INTERVAL
        nearest_timestamp = (virtual_time // self.interval_seconds) * self.interval_seconds

        # Если с момента начала свечи прошло меньше 60 секунд — окно открыто
        return (virtual_time - nearest_timestamp) < GRACE_PERIOD_SEC

if __name__ == "__main__":
    import time
    
    print(f"Запуск тестового цикла (интервал 5m, SHIFT_INTERVAL={SHIFT_INTERVAL}, GRACE_PERIOD_SEC={GRACE_PERIOD_SEC})...")
    tc = TimeControl(interval="5m")
    
    try:
        while True:
            if tc.is_new_interval():
                now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                print(f"СИГНАЛ! Точное время (UTC): {now_utc}")
            time.sleep(1)
    except KeyboardInterrupt:
        print("Тест завершен.")