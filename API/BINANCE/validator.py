# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\API\BINANCE\validator.py
# Role: validator.py module

# ==============================================================================
# Path: API/BINANCE/validator.py
# Role: Валидатор ответов и формирование APIResponse
# ==============================================================================

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Union, TYPE_CHECKING
try:
    from c_log import UnifiedLogger
except ImportError:
    UnifiedLogger = Any

@dataclass
class APIResponse:
    success: bool
    data: Any = None
    error_code: Optional[int] = None
    error_msg: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)


class OrderValidator:
    """
    Binance Futures response validator.
    """

    def __init__(self, logger: "UnifiedLogger"):
        self.logger = logger

    # ==================================================
    # MARKET / LIMIT ORDER
    # ==================================================
    def validate_order_create(
        self,
        res: APIResponse,
        *,
        market_type: str,
    ) -> APIResponse:
        if not res.success:
            if res.error_code:
                self.logger.error(f"❌ Binance error code={res.error_code} msg={res.error_msg}")
            return res

        data = res.data
        if not isinstance(data, dict):
            res.success = False
            res.error_msg = "INVALID_RESPONSE_FORMAT"
            return res

        status = data.get("status")
        executed = float(data.get("executedQty", 0))
        avg_price = float(data.get("avgPrice", 0))

        # self.logger.debug(
        #     f"Order create @ {now()} "
        #     f"status={status} executed={executed} avg={avg_price}"
        # )

        # self.logger.debug(f"RAW ORDER RESPONSE: {data}")
        
        if market_type == "MARKET":
            if status != "FILLED" or executed <= 0:
                res.success = False
                res.error_msg = "MARKET_NOT_FILLED"
            return res

        # LIMIT
        if status in ("NEW", "PARTIALLY_FILLED", "FILLED"):
            return res

        res.success = False
        res.error_msg = f"ORDER_STATUS={status}"
        return res

    # ==================================================
    # CANCEL ORDER (SINGLE OR BATCH)
    # ==================================================
    def validate_order_cancel(self, res: APIResponse) -> APIResponse:
        """
        Валидация ответа на отмену ордера (одиночного или пачки batchOrders).
        Обрабатывает:
        - Ошибку верхнего уровня (res.success is False)
        - -2011 (Unknown order / already canceled) -> трактуется как успех
        - Список результатов (DELETE /fapi/v1/batchOrders)
        - Одиночный словарь (DELETE /fapi/v1/order)
        """
        if not res.success:
            # -2011 = already canceled / not exists / unknown order
            if res.error_code == -2011:
                self.logger.debug("⚠️ Order already canceled or not found (-2011)")
                res.success = True
                res.error_msg = None
                res.error_code = None
            else:
                if res.error_code:
                    self.logger.error(f"❌ Binance error code={res.error_code} msg={res.error_msg}")
            return res

        data = res.data

        # 1. Ответ в виде списка (пакетная отмена batchOrders)
        if isinstance(data, list):
            failed_items = []
            for item in data:
                if isinstance(item, dict):
                    # Если есть code ошибки (например -2011 - ордер уже закрыт/не существует)
                    code = item.get("code")
                    if code is not None and code != 200:
                        if code == -2011:
                            self.logger.debug("⚠️ Batch item: order already canceled or not found (-2011)")
                            continue
                        failed_items.append(f"code={code}: {item.get('msg')}")
                        continue

                    # Если это статус ордера
                    status = item.get("status")
                    if status != "CANCELED":
                        failed_items.append(f"status={status}")
                else:
                    failed_items.append(f"unexpected_item={item}")

            if failed_items:
                res.success = False
                res.error_msg = f"CANCEL_BATCH_PARTIAL_FAILED: {'; '.join(failed_items)}"
                self.logger.error(f"❌ Binance batch cancel failed items: {res.error_msg}")
                return res

            return res

        # 2. Ответ в виде словаря (одиночная отмена или спец. ответ)
        if isinstance(data, dict):
            code = data.get("code")
            if code is not None and code != 200:
                if code == -2011:
                    self.logger.debug("⚠️ Order already canceled or not found (-2011)")
                    return res
                res.success = False
                res.error_code = code
                res.error_msg = data.get("msg", "CANCEL_FAILED")
                self.logger.error(f"❌ Binance cancel error code={code} msg={res.error_msg}")
                return res

            if data.get("status") == "CANCELED":
                return res

            res.success = False
            res.error_msg = f"CANCEL_FAILED: status={data.get('status')}"
            return res

        res.success = False
        res.error_msg = "INVALID_RESPONSE_FORMAT"
        return res

    # Алиас для обратной совместимости
    validate_cancel = validate_order_cancel

    # ==================================================
    # CANCEL ALL
    # ==================================================
    def validate_cancel_all(self, res: APIResponse) -> APIResponse:
        if not res.success:
            # -2011 = no open orders
            if res.error_code == -2011:
                res.success = True
                res.error_msg = None
                res.error_code = None
            else:
                if res.error_code:
                    self.logger.error(f"❌ Binance error code={res.error_code} msg={res.error_msg}")
        return res
