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
    tp_price: float = None               # целевая цена основного тейк-профита
    fallback_price: float = None    # кешированная цена страховочного маркета
    eff_invest: float = 0.0              # эффективный invest_size стороны с учетом макро-тренда 4h
    
    in_position: bool = False
    in_position_papper: bool = False
    is_finished: bool = False
    is_closing: bool = False
    pending_avg: bool = False
    pending_rolling_tp: bool = False
    pending_peel: bool = False
    
    grid: dict = field(default_factory=dict)
    tp_map: dict = field(default_factory=dict)
    tp_purpose: str = "self"
    avg_free_volume_behavior: str = "distribute_remaining"
    trailing_tp_state: dict = field(default_factory=dict)
    trailing_grid_state: dict = field(default_factory=dict)
    recent_closes: list = field(default_factory=list)
    last_grace_period: float = 0.0
    last_closed_volume: float = 0.0
    last_closed_avg_price: float = 0.0
    last_closed_timestamp: float = 0.0
    
    def set_in_position(self, status: bool):
        self.in_position = status
        self.in_position_papper = False
        if not status:
            self.is_closing = False
            
    def reset(self):
        """Сброс до дефолта: обнуление всех метрик и флагов."""
        self.total_volume = 0.0
        self.avg_entry_price = 0.0
        self.pre_avg_price = 0.0
        self.initial_entry_price = 0.0
        self.eff_invest = 0.0
        self.open_time = 0
        self.last_closed_volume = 0.0
        self.last_closed_avg_price = 0.0
        self.last_closed_timestamp = 0.0
        self.in_position = False
        self.in_position_papper = False
        self.is_finished = False
        self.is_closing = False
        self.pending_avg = False
        self.pending_rolling_tp = False
        self.pending_peel = False
        self.next_avg_price = None
        self.tp_price = None
        self.fallback_price = None
        self.trailing_tp_state = {}
        self.trailing_grid_state = {}
        
        # Сбрасываем только рантаймовские поля сетки (price, is_active), сохраняя пользовательские настройки
        for k, v in self.grid.items():
            v["is_active"] = False
            v["is_armed"] = False
            v["price"] = None
            v["activation_price"] = None
            v["timestamp"] = None
            v["activated_at"] = None
            v["fill_price"] = None
            v["fill_qty"] = None
            v.pop("armed_at_drawdown", None)
            v.pop("armed_timestamp", None)
            v.pop("peeled", None)
            v.pop("peeled_count", None)
            v.pop("_skipped", None)
            v.pop("_skip_policy", None)
            v.pop("_skip_status", None)
            v.pop("_skip_desc", None)
            v.pop("_skipped_by_level", None)
            v.pop("_skipped_levels_handled", None)
            
        # Аналогично для тейк-профитов
        for k, v in self.tp_map.items():
            v["is_active"] = False
            v["is_armed"] = False
            v.pop("armed_at_gain", None)
            v.pop("armed_floor", None)
            v.pop("armed_timestamp", None)
