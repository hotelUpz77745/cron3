# ============================================================
# FILE: POS_FSM/models.py
# ROLE: Position state data models for FSM
# ============================================================

from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Union


@dataclass
class GridLevel:
    """Типизированное представление отдельного колена сетки усреднения."""
    indent: float = 0.0
    volume: float = 0.0
    is_active: bool = False
    is_armed: bool = False
    enabled: bool = True
    activation: Optional[float] = None
    price: Optional[float] = None
    activation_price: Optional[float] = None
    zone: Optional[int] = None
    peeled: bool = False
    peeled_count: int = 0
    armed_at_drawdown: Optional[float] = None
    armed_timestamp: Optional[float] = None


@dataclass
class TakeProfitLevel:
    """Типизированное представление уровня карты тейк-профитов."""
    indent: float = 0.0
    volume: float = 0.0
    is_active: bool = False
    is_armed: bool = False
    enabled: bool = True
    activation: Optional[float] = None
    price: Optional[float] = None
    activation_price: Optional[float] = None
    armed_at_gain: Optional[float] = None
    armed_floor: Optional[float] = None
    armed_timestamp: Optional[float] = None


@dataclass
class PositionState:
    symbol: str = ""
    side: str = ""  # "LONG" или "SHORT"
    
    total_volume: float = 0.0            # суммарный объем позиции
    avg_entry_price: float = 0.0         # Средняя точка входа
    pre_avg_price: float = 0.0           # Цена входа ДО усреднения (для синхронизации)
    initial_entry_price: float = 0.0     # начальная точка входа позиции
    open_time: int = 0                   # время открытия первой сделки (unix ms)
    next_avg_price: Optional[float] = None         # кешированная цена ближайшего усреднения
    tp_price: Optional[float] = None               # целевая цена основного тейк-профита
    fallback_price: Optional[float] = None    # кешированная цена страховочного маркета
    tp_placed_at: Optional[float] = None      # Временная метка выставления последнего TP ордера (сек)
    tp_current_indent: Optional[float] = None # Текущий процентный отступ установленного TP ордера
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
    tp_purpose: Optional[str] = None
    avg_free_volume_behavior: Optional[str] = None
    trailing_tp_state: dict = field(default_factory=dict)
    trailing_grid_state: dict = field(default_factory=dict)
    recent_closes: list = field(default_factory=list)
    last_grace_period: float = 0.0
    last_closed_volume: float = 0.0
    last_closed_avg_price: float = 0.0
    last_closed_timestamp: float = 0.0
    cur_step: int = 0
    fees: float = 0.0
    peel_filled: bool = False
    next_grid_p: float = 0.0
    next_grid_q: float = 0.0
    next_grid_step: int = 0
    peel_p: float = 0.0
    peel_q: float = 0.0
    _last_resolved_vol: Optional[float] = None
    _last_resolved_price: Optional[float] = None

    @property
    def in_pos(self) -> bool:
        return self.in_position

    @in_pos.setter
    def in_pos(self, val: bool):
        self.in_position = val
        if not val:
            self.is_closing = False

    @property
    def vol_qty(self) -> float:
        return self.total_volume

    @vol_qty.setter
    def vol_qty(self, val: float):
        self.total_volume = val

    @property
    def avg_price(self) -> float:
        return self.avg_entry_price

    @avg_price.setter
    def avg_price(self, val: float):
        self.avg_entry_price = val

    @property
    def entry_price(self) -> float:
        return self.initial_entry_price

    @entry_price.setter
    def entry_price(self, val: float):
        self.initial_entry_price = val

    @property
    def recent_close_ts(self) -> float:
        return self.last_closed_timestamp

    @recent_close_ts.setter
    def recent_close_ts(self, val: float):
        self.last_closed_timestamp = val

    @property
    def trailing_grid_armed(self) -> dict:
        return self.trailing_grid_state.setdefault("armed_levels", {})

    def set_in_position(self, status: bool):
        self.in_position = status
        self.in_position_papper = False
        if not status:
            self.is_closing = False
            
    def reset(self, ts_ms: float = 0.0):
        """Сброс до дефолта: обнуление всех метрик и флагов."""
        self.total_volume = 0.0
        self.avg_entry_price = 0.0
        self.pre_avg_price = 0.0
        self.initial_entry_price = 0.0
        self.eff_invest = 0.0
        self.open_time = 0
        self.last_closed_volume = 0.0
        self.last_closed_avg_price = 0.0
        self.last_closed_timestamp = ts_ms
        self.cur_step = 0
        self.fees = 0.0
        self.peel_filled = False
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
        self.tp_placed_at = None
        self.tp_current_indent = None
        self.trailing_tp_state = {}
        self.trailing_grid_state = {}
        self._last_resolved_vol = None
        self._last_resolved_price = None
        
        # Сбрасываем только рантаймовские поля сетки (price, is_active), сохраняя пользовательские настройки
        for k, v in self.grid.items():
            if "_base_indent" in v:
                v["indent"] = v.pop("_base_indent")
            if "_base_activation" in v:
                v["activation"] = v.pop("_base_activation")
            v.pop("effective_indent", None)
            v.pop("effective_activation", None)
            v.pop("atr_mult", None)
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

    def get_grid_level(self, level_key: Union[str, int]) -> Dict[str, Any]:
        """Возвращает словарь уровня сетки усреднения."""
        k = str(level_key)
        if k not in self.grid:
            raise KeyError(f"[PositionState][{self.symbol}][{self.side}] Level '{k}' not found in grid")
        return self.grid[k]

    def is_level_active(self, level_key: Union[str, int]) -> bool:
        """Проверяет, исполнен ли уровень сетки."""
        k = str(level_key)
        lvl = self.grid.get(k)
        return bool(lvl.get("is_active", False)) if isinstance(lvl, dict) else False

    def mark_level_active(self, level_key: Union[str, int], price: Optional[float] = None) -> None:
        """Помечает уровень сетки как исполненный (необратимый high-water mark)."""
        k = str(level_key)
        if k in self.grid and isinstance(self.grid[k], dict):
            self.grid[k]["is_active"] = True
            self.grid[k]["is_armed"] = False
            if price is not None:
                self.grid[k]["price"] = float(price)

    def is_level_peeled(self, level_key: Union[str, int], limit: int = 1) -> bool:
        """Проверяет, исчерпан ли лимит пилинга для данного уровня."""
        k = str(level_key)
        lvl = self.grid.get(k)
        if not isinstance(lvl, dict):
            return False
        return bool(lvl.get("peeled", False)) or int(lvl.get("peeled_count", 0)) >= limit

    def increment_peel_count(self, level_key: Union[str, int], limit: int = 1) -> int:
        """Инкрементирует счетчик пилинга уровня и при достижении лимита фиксирует peeled=True."""
        k = str(level_key)
        lvl = self.get_grid_level(k)
        cnt = int(lvl.get("peeled_count", 0)) + 1
        lvl["peeled_count"] = cnt
        if cnt >= limit:
            lvl["peeled"] = True
        return cnt

    def get_tp_level(self, level_key: Union[str, int]) -> Dict[str, Any]:
        """Возвращает словарь уровня карты тейк-профитов."""
        k = str(level_key)
        if k not in self.tp_map:
            raise KeyError(f"[PositionState][{self.symbol}][{self.side}] TP level '{k}' not found in tp_map")
        return self.tp_map[k]

    def is_tp_active(self, level_key: Union[str, int]) -> bool:
        """Проверяет, исполнен ли уровень карты тейк-профитов."""
        k = str(level_key)
        lvl = self.tp_map.get(k)
        return bool(lvl.get("is_active", False)) if isinstance(lvl, dict) else False

    def mark_tp_active(self, level_key: Union[str, int]) -> None:
        """Помечает уровень карты тейк-профитов как исполненный."""
        k = str(level_key)
        if k in self.tp_map and isinstance(self.tp_map[k], dict):
            self.tp_map[k]["is_active"] = True
            self.tp_map[k]["is_armed"] = False

