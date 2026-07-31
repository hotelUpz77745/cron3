# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\POS_FSM\models.py
# Role: models.py module

# ==============================================================================
# Path: FSM/models.py
# Role: Модели состояний FSM
# ==============================================================================

from dataclasses import dataclass, field

@dataclass
class PositionState:
    symbol: str
    side: str  # "LONG" или "SHORT"
    
    total_volume: float = 0.0            # суммарный объем позиции
    avg_entry_price: float = 0.0         # Средняя точка входа
    pre_avg_price: float = 0.0           # Цена входа ДО усреднения (для синхронизации)
    initial_entry_price: float = 0.0     # начальная точка входа позиции
    open_time: int = 0                   # время открытия первой сделки (unix ms)
    next_avg_price: float = None         # кешированная цена ближайшего усреднения
    fallback_price: float = None    # кешированная цена страховочного маркета
    
    in_position: bool = False
    in_position_papper: bool = False
    is_finished: bool = False
    pending_avg: bool = False
    pending_rolling_tp: bool = False
    
    grid: dict = field(default_factory=dict)
    tp_map: dict = field(default_factory=dict)
    tp_purpose: str = "self"
    recent_closes: list = field(default_factory=list)
    last_grace_period: float = 0.0
    
    def set_in_position(self, status: bool):
        self.in_position = status
        self.in_position_papper = False
            
    def reset(self):
        """Сброс до дефолта: обнуление всех метрик и флагов."""
        self.total_volume = 0.0
        self.avg_entry_price = 0.0
        self.pre_avg_price = 0.0
        self.initial_entry_price = 0.0
        self.open_time = 0
        self.in_position = False
        self.in_position_papper = False
        self.is_finished = False
        self.pending_avg = False
        self.pending_rolling_tp = False
        self.next_avg_price = None
        self.fallback_price = None
        
        # Сбрасываем только рантаймовские поля сетки (price, is_active), сохраняя пользовательские настройки
        for k, v in self.grid.items():
            v["is_active"] = False
            v["price"] = None
            v["timestamp"] = None
            
        # Аналогично для тейк-профитов
        for k, v in self.tp_map.items():
            v["is_active"] = False
