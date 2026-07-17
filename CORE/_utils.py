# ==============================================================================
# Role: Математика для трейдинга (расчет объема, тейк-профитов и прочего)
# ==============================================================================

from typing import Dict, Any
from c_utils import Utils

class TradeMath:
    @staticmethod
    def calculate_order_volume(
        invest_size: float, 
        volume_percent: float, 
        price: float, 
        symbol_info: Dict[str, Any], 
        symbol: str
    ) -> float:
        """
        Рассчитывает количество контрактов (volume) для постановки ордера.
        volume_percent - доля в процентах от invest_size (например, 20 означает 20%).
        """
        precisions = Utils.get_spec_precisions(symbol_info, symbol)
        qty_precision = precisions[0] if precisions else 3
        
        # invest_size (например, 100 USDT). Доля в USDT:
        usdt_volume = invest_size * (volume_percent / 100.0)
        
        # Количество монет:
        coin_qty = usdt_volume / price
        
        # Округляем до нужного количества знаков после запятой (шаг лота)
        return round(coin_qty, qty_precision)

    @staticmethod
    def calculate_take_profit_price(
        avg_entry_price: float, 
        tp_percent_indent: float, 
        side: str, 
        symbol_info: Dict[str, Any], 
        symbol: str
    ) -> float:
        """
        Рассчитывает цену лимитного Take-Profit.
        tp_percent_indent - процент отступа (берем по модулю, т.к. направление определяется логикой).
        Для LONG: TP выше средней цены входа.
        Для SHORT: TP ниже средней цены входа.
        """
        precisions = Utils.get_spec_precisions(symbol_info, symbol)
        price_precision = precisions[1] if precisions else 2
        
        indent_abs = abs(tp_percent_indent)
        
        if side.upper() == "LONG":
            tp_price = avg_entry_price * (1 + indent_abs / 100.0)
        elif side.upper() == "SHORT":
            tp_price = avg_entry_price * (1 - indent_abs / 100.0)
        else:
            tp_price = avg_entry_price
            
        return round(tp_price, price_precision)

    @staticmethod
    def calculate_grid_price(
        initial_price: float,
        indent_pct: float,
        side: str,
        symbol_info: Dict[str, Any],
        symbol: str
    ) -> float:
        """
        Рассчитывает цену следующего уровня сетки на основе отступа.
        Знак indent_pct игнорируется (берется по модулю).
        Для LONG цена усреднения ниже входа: initial_price * (1 - indent_abs / 100.0)
        Для SHORT цена усреднения выше входа: initial_price * (1 + indent_abs / 100.0)
        """
        precisions = Utils.get_spec_precisions(symbol_info, symbol)
        price_precision = precisions[1] if precisions else 2
        
        indent_abs = abs(indent_pct)
        
        if side.upper() == "LONG":
            price = initial_price * (1 - indent_abs / 100.0)
        elif side.upper() == "SHORT":
            price = initial_price * (1 + indent_abs / 100.0)
        else:
            price = initial_price
            
        return round(price, price_precision)

    @staticmethod
    def round_qty(
        qty: float,
        symbol_info: Dict[str, Any],
        symbol: str
    ) -> float:
        """
        Округляет объем до требуемой спецификацией точности.
        """
        precisions = Utils.get_spec_precisions(symbol_info, symbol)
        qty_precision = precisions[0] if precisions else 3
        return round(qty, qty_precision)
# ==============================================================================
# Role: Общие утилиты для расчетов рисков и детерминации уровней
# ==============================================================================

from typing import Dict, Any

class RiskCalculatingUtils:
    @staticmethod
    def get_current_grid_level(grid: dict) -> str:
        """
        Определяет текущий активный уровень сетки на основе флагов is_active.
        Возвращает строковый ключ уровня (например, "0", "1", "2").
        """
        active_levels = [int(k) for k, v in grid.items() if v.get("is_active")]
        return str(max(active_levels)) if active_levels else "0"

class SpecManager:
    """
    Управляет периодическим обновлением спецификаций инструментов (exchangeInfo).
    """
    def __init__(self, bot_core):
        self.bot_core = bot_core
        self.is_running = False
        self._task = None

    async def _specification_task(self):
        import asyncio
        from consts import SPEC_TTL_SEC, DATA_DIR
        from API.BINANCE.public import BinancePublic
        from c_utils import Utils
        from c_log import UnifiedLogger
        logger = UnifiedLogger("SpecManager")
        
        try:
            while self.is_running:
                data = await BinancePublic.get_instruments()
                if data:
                    self.bot_core.spec_data = {"symbols": data}
                    Utils.write_json_file(DATA_DIR / "CACHE" / "specifications.json", self.bot_core.spec_data)
                
                await asyncio.sleep(SPEC_TTL_SEC)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Ошибка в _specification_task: {e}")

    def start(self):
        import asyncio
        self.is_running = True
        self._task = asyncio.create_task(self._specification_task())

    def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()
