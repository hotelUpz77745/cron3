# ============================================================
# FILE: POS_FSM/models.py
# ROLE: Position state data models for FSM
# ============================================================

import copy
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
    order_id: Optional[Union[int, str]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GridLevel":
        if not isinstance(data, dict):
            return cls()
        return cls(
            indent=float(data.get("indent") or 0.0),
            volume=float(data.get("volume") or data.get("vol") or 0.0),
            is_active=bool(data.get("is_active", False)),
            is_armed=bool(data.get("is_armed", False)),
            enabled=bool(data.get("enabled", True)),
            activation=float(data["activation"]) if data.get("activation") is not None else None,
            price=float(data["price"]) if data.get("price") is not None else None,
            activation_price=float(data["activation_price"]) if data.get("activation_price") is not None else None,
            zone=int(data["zone"]) if data.get("zone") is not None else None,
            peeled=bool(data.get("peeled", False)),
            peeled_count=int(data.get("peeled_count", data.get("peel_count", 0))),
            armed_at_drawdown=float(data["armed_at_drawdown"]) if data.get("armed_at_drawdown") is not None else None,
            armed_timestamp=float(data["armed_timestamp"]) if data.get("armed_timestamp") is not None else None,
            order_id=data.get("order_id"),
        )

    @property
    def peel_count(self) -> int:
        return self.peeled_count

    @peel_count.setter
    def peel_count(self, val: int):
        self.peeled_count = int(val)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "indent": self.indent,
            "volume": self.volume,
            "is_active": self.is_active,
            "is_armed": self.is_armed,
            "enabled": self.enabled,
            "activation": self.activation,
            "price": self.price,
            "activation_price": self.activation_price,
            "zone": self.zone,
            "peeled": self.peeled,
            "peeled_count": self.peeled_count,
            "armed_at_drawdown": self.armed_at_drawdown,
            "armed_timestamp": self.armed_timestamp,
            "order_id": self.order_id,
        }


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
    order_id: Optional[Union[int, str]] = None

    @property
    def profit(self) -> float:
        return self.indent

    @profit.setter
    def profit(self, val: float):
        self.indent = float(val)

    @property
    def close(self) -> float:
        return self.volume

    @close.setter
    def close(self, val: float):
        self.volume = float(val)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TakeProfitLevel":
        if not isinstance(data, dict):
            return cls()
        indent_val = data.get("indent") if data.get("indent") is not None else (data.get("profit") if data.get("profit") is not None else data.get("profit_pct"))
        vol_val = data.get("volume") if data.get("volume") is not None else (data.get("close") if data.get("close") is not None else (data.get("vol") if data.get("vol") is not None else data.get("close_pct")))
        return cls(
            indent=float(indent_val or 0.0),
            volume=float(vol_val or 0.0),
            is_active=bool(data.get("is_active", False)),
            is_armed=bool(data.get("is_armed", False)),
            enabled=bool(data.get("enabled", True)),
            activation=float(data["activation"]) if data.get("activation") is not None else None,
            price=float(data["price"]) if data.get("price") is not None else None,
            activation_price=float(data["activation_price"]) if data.get("activation_price") is not None else None,
            armed_at_gain=float(data["armed_at_gain"]) if data.get("armed_at_gain") is not None else None,
            armed_floor=float(data["armed_floor"]) if data.get("armed_floor") is not None else None,
            armed_timestamp=float(data["armed_timestamp"]) if data.get("armed_timestamp") is not None else None,
            order_id=data.get("order_id"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "indent": self.indent,
            "volume": self.volume,
            "is_active": self.is_active,
            "is_armed": self.is_armed,
            "enabled": self.enabled,
            "activation": self.activation,
            "price": self.price,
            "activation_price": self.activation_price,
            "armed_at_gain": self.armed_at_gain,
            "armed_floor": self.armed_floor,
            "armed_timestamp": self.armed_timestamp,
            "order_id": self.order_id,
        }


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

    def to_dict(self) -> Dict[str, Any]:
        """Сериализует состояние позиции в словарь (канонический дамп FSM рантайма)."""
        return {
            "total_volume": self.total_volume,
            "avg_entry_price": self.avg_entry_price,
            "pre_avg_price": self.pre_avg_price,
            "initial_entry_price": self.initial_entry_price,
            "open_time": self.open_time,
            "next_avg_price": self.next_avg_price,
            "fallback_price": self.fallback_price,
            "in_position": self.in_position,
            "in_position_papper": self.in_position_papper,
            "is_finished": self.is_finished,
            "pending_avg": self.pending_avg,
            "pending_rolling_tp": self.pending_rolling_tp,
            "pending_peel": self.pending_peel,
            "grid": copy.deepcopy(self.grid),
            "tp_map": copy.deepcopy(self.tp_map),
            "tp_purpose": self.tp_purpose,
            "avg_free_volume_behavior": getattr(self, "avg_free_volume_behavior", "distribute_remaining"),
            "trailing_tp_state": copy.deepcopy(getattr(self, "trailing_tp_state", {})),
            "trailing_grid_state": copy.deepcopy(getattr(self, "trailing_grid_state", {})),
            "recent_closes": list(getattr(self, "recent_closes", [])),
            "eff_invest": float(getattr(self, "eff_invest", 0.0)),
            "tp_placed_at": getattr(self, "tp_placed_at", None),
            "tp_current_indent": getattr(self, "tp_current_indent", None),
            "tp_price": getattr(self, "tp_price", None),
            "is_closing": getattr(self, "is_closing", False),
            "fees": float(getattr(self, "fees", 0.0)),
            "cur_step": int(getattr(self, "cur_step", 0)),
            "last_grace_period": float(getattr(self, "last_grace_period", 0.0)),
            "last_closed_volume": float(getattr(self, "last_closed_volume", 0.0)),
            "last_closed_avg_price": float(getattr(self, "last_closed_avg_price", 0.0)),
            "last_closed_timestamp": float(getattr(self, "last_closed_timestamp", 0.0)),
        }

    def update_from_dict(self, data: Dict[str, Any]) -> "PositionState":
        """Загружает поля состояния позиции из словаря с безопасным deepcopy."""
        if not isinstance(data, dict):
            return self
        self.total_volume = float(data.get("total_volume", 0.0))
        self.avg_entry_price = float(data.get("avg_entry_price", 0.0))
        self.pre_avg_price = float(data.get("pre_avg_price", 0.0))
        self.initial_entry_price = float(data.get("initial_entry_price", 0.0))
        self.open_time = int(data.get("open_time", 0))
        self.next_avg_price = float(data["next_avg_price"]) if data.get("next_avg_price") is not None else None
        self.fallback_price = float(data["fallback_price"]) if data.get("fallback_price") is not None else None
        self.in_position = bool(data.get("in_position", False))
        self.in_position_papper = bool(data.get("in_position_papper", False))
        self.is_finished = bool(data.get("is_finished", False))
        self.pending_avg = bool(data.get("pending_avg", False))
        self.pending_rolling_tp = bool(data.get("pending_rolling_tp", False))
        self.pending_peel = bool(data.get("pending_peel", False))
        self.eff_invest = float(data.get("eff_invest", 0.0))
        self.tp_placed_at = float(data["tp_placed_at"]) if data.get("tp_placed_at") is not None else None
        self.tp_current_indent = float(data["tp_current_indent"]) if data.get("tp_current_indent") is not None else None
        self.tp_price = float(data["tp_price"]) if data.get("tp_price") is not None else None
        self.is_closing = bool(data.get("is_closing", False))

        if "grid" in data and isinstance(data["grid"], dict):
            self.grid = copy.deepcopy(data["grid"])
        if "tp_map" in data and isinstance(data["tp_map"], dict):
            self.tp_map = copy.deepcopy(data["tp_map"])
        self.tp_purpose = data.get("tp_purpose", "self")
        self.avg_free_volume_behavior = data.get("avg_free_volume_behavior", "distribute_remaining")
        if "trailing_tp_state" in data and isinstance(data["trailing_tp_state"], dict):
            self.trailing_tp_state = copy.deepcopy(data["trailing_tp_state"])
        if "trailing_grid_state" in data and isinstance(data["trailing_grid_state"], dict):
            self.trailing_grid_state = copy.deepcopy(data["trailing_grid_state"])
        if "recent_closes" in data and isinstance(data["recent_closes"], list):
            self.recent_closes = copy.deepcopy(data["recent_closes"])

        if "fees" in data:
            self.fees = float(data["fees"])
        if "cur_step" in data:
            self.cur_step = int(data["cur_step"])
        if "last_grace_period" in data:
            self.last_grace_period = float(data["last_grace_period"])
        if "last_closed_volume" in data:
            self.last_closed_volume = float(data["last_closed_volume"])
        if "last_closed_avg_price" in data:
            self.last_closed_avg_price = float(data["last_closed_avg_price"])
        if "last_closed_timestamp" in data:
            self.last_closed_timestamp = float(data["last_closed_timestamp"])
        return self

    @classmethod
    def from_dict(cls, data: Dict[str, Any], symbol: str = "", side: str = "") -> "PositionState":
        """Создает новый инстанс PositionState из словаря."""
        sym = data.get("symbol", symbol)
        s_side = data.get("side", side)
        inst = cls(symbol=sym, side=s_side)
        inst.update_from_dict(data)
        return inst

            
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

    def mark_tp_active(self, level_key: Union[str, int], is_active: bool = True) -> None:
        """Помечает уровень карты тейк-профитов как исполненный/активный."""
        k = str(level_key)
        if k in self.tp_map and isinstance(self.tp_map[k], dict):
            self.tp_map[k]["is_active"] = bool(is_active)
            if is_active:
                self.tp_map[k]["is_armed"] = False

    def get_typed_grid_level(self, level_key: Union[str, int]) -> Optional[GridLevel]:
        """Возвращает типизированную структуру GridLevel для указанного уровня (или None если не найден)."""
        k = str(level_key)
        if k not in self.grid or not isinstance(self.grid[k], dict):
            return None
        return GridLevel.from_dict(self.grid[k])

    def get_typed_tp_level(self, level_key: Union[str, int]) -> Optional[TakeProfitLevel]:
        """Возвращает типизированную структуру TakeProfitLevel для указанного уровня (или None если не найден)."""
        k = str(level_key)
        if k not in self.tp_map or not isinstance(self.tp_map[k], dict):
            return None
        return TakeProfitLevel.from_dict(self.tp_map[k])

    def iter_grid_levels(self) -> list:
        """Итерирует по всем уровням сетки, возвращая список пар (str_key, GridLevel)."""
        items = []
        for k in sorted(self.grid.keys(), key=lambda x: int(x) if str(x).isdigit() else str(x)):
            lvl = self.grid[k]
            if isinstance(lvl, dict):
                items.append((str(k), GridLevel.from_dict(lvl)))
        return items

    def iter_tp_levels(self) -> list:
        """Итерирует по всем уровням TP-карты, возвращая список пар (str_key, TakeProfitLevel)."""
        items = []
        for k in sorted(self.tp_map.keys(), key=lambda x: int(x) if str(x).isdigit() else str(x)):
            lvl = self.tp_map[k]
            if isinstance(lvl, dict):
                items.append((str(k), TakeProfitLevel.from_dict(lvl)))
        return items

    def active_grid_keys(self) -> list:
        """Возвращает список ключей исполненных уровней сетки."""
        return [
            str(k) for k, v in self.grid.items()
            if isinstance(v, dict) and v.get("is_active") and v.get("enabled", True) is not False
        ]

    def can_start_averaging(self) -> bool:
        """
        Проверяет соблюдение инвариантов FSM для запуска усреднения:
        - Позиция открыта (in_position == True).
        - Предыдущее усреднение не находится в процессе исполнения (pending_avg == False).
        - Позиция не ликвидируется / не закрывается (is_closing == False).
        - Позиция не помечена на сброс (is_finished == False).
        """
        return bool(self.in_position and not self.pending_avg and not self.is_closing and not self.is_finished)

    def can_peel(self) -> bool:
        """Проверяет соблюдение инвариантов FSM для частичной разгрузки (пилинга)."""
        return bool(self.in_position and not self.pending_peel and not self.is_closing and not self.is_finished)

    def can_place_tp(self) -> bool:
        """Проверяет соблюдение инвариантов FSM для выставления / подтяжки TP."""
        return bool(self.in_position and not self.pending_rolling_tp and not self.is_closing and not self.is_finished)

