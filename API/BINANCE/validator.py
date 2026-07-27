# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\API\BINANCE\validator.py
# Role: validator.py module

# ==============================================================================
# Path: API/BINANCE/validator.py
# Role: Валидатор ответов и формирование APIResponse
# ==============================================================================

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Union, TYPE_CHECKING
from c_utils import now

from c_log import UnifiedLogger
if TYPE_CHECKING:

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
    # CANCEL ORDER
    # ==================================================
    def validate_cancel(self, res: APIResponse) -> APIResponse:
        if not res.success:
            # -2011 = already canceled / not exists
            if res.error_code == -2011:
                self.logger.debug("⚠️ Order already canceled")
                res.success = True
                res.error_msg = None
                res.error_code = None
            else:
                if res.error_code:
                    self.logger.error(f"❌ Binance error code={res.error_code} msg={res.error_msg}")
            return res

        data = res.data
        if not isinstance(data, dict):
            res.success = False
            res.error_msg = "INVALID_RESPONSE_FORMAT"
            return res

        if data.get("status") == "CANCELED":
            return res

        res.success = False
        res.error_msg = "CANCEL_FAILED"
        return res

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
