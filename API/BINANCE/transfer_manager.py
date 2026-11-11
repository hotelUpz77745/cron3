# ==============================================================================
# Path: API/BINANCE/transfer_manager.py
# Role: Управление переводами активов между кошельками Futures (USDⓈ-M) и Spot (SAPI Universal Transfer)
# ==============================================================================

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional

from API.BINANCE.validator import APIResponse
from c_log import UnifiedLogger

logger = UnifiedLogger("TRANSFER_MANAGER")

TRANSFER_TYPE_FUTURES_TO_SPOT: str = "UMFUTURE_MAIN"
TRANSFER_TYPE_SPOT_TO_FUTURES: str = "MAIN_UMFUTURE"
DEFAULT_TRANSFER_ASSET: str = "USDT"
DEFAULT_TRANSFER_RECV_WINDOW_MS: int = 20000
UNIVERSAL_TRANSFER_ENDPOINT: str = "https://api.binance.com/sapi/v1/asset/transfer"


class TransferManager:
    """
    Менеджер переводов между фьючерсным USDⓈ-M (UMFUTURE) и спотовым (MAIN) кошельками Binance.
    Использует каноничный SAPI эндпоинт: POST https://api.binance.com/sapi/v1/asset/transfer.
    """

    @staticmethod
    def format_transfer_amount(amount: float, decimals: int = 2) -> str:
        """
        Форматирует сумму перевода с округлением вниз (ROUND_DOWN), чтобы не превысить доступный остаток.
        """
        quantizer = Decimal("10") ** (-int(decimals))
        dec_val = Decimal(str(float(amount))).quantize(quantizer, rounding=ROUND_DOWN)
        return f"{dec_val:f}"

    @staticmethod
    async def transfer_futures_to_spot(
        client: Any,
        amount: float,
        asset: str = DEFAULT_TRANSFER_ASSET,
    ) -> APIResponse:
        """
        Переводит средства с USDⓈ-M Futures на Spot (type='UMFUTURE_MAIN').
        """
        return await TransferManager._execute_transfer(
            client=client,
            amount=amount,
            asset=asset,
            transfer_type=TRANSFER_TYPE_FUTURES_TO_SPOT,
        )

    @staticmethod
    async def transfer_spot_to_futures(
        client: Any,
        amount: float,
        asset: str = DEFAULT_TRANSFER_ASSET,
    ) -> APIResponse:
        """
        Переводит средства со Spot на USDⓈ-M Futures (type='MAIN_UMFUTURE').
        """
        return await TransferManager._execute_transfer(
            client=client,
            amount=amount,
            asset=asset,
            transfer_type=TRANSFER_TYPE_SPOT_TO_FUTURES,
        )

    @staticmethod
    async def _execute_transfer(
        client: Any,
        amount: float,
        asset: str,
        transfer_type: str,
    ) -> APIResponse:
        try:
            amt_float = float(amount)
        except (TypeError, ValueError):
            return APIResponse(
                success=False,
                error_code=-1,
                error_msg=f"Invalid transfer amount: {amount}",
            )

        if amt_float <= 0.0:
            return APIResponse(
                success=False,
                error_code=-1,
                error_msg=f"Transfer amount must be positive, got {amt_float}",
            )

        amount_str = TransferManager.format_transfer_amount(amt_float, decimals=2)
        if float(amount_str) <= 0.0:
            return APIResponse(
                success=False,
                error_code=-1,
                error_msg=f"Rounded transfer amount is zero ({amount_str})",
            )

        asset_clean = str(asset or DEFAULT_TRANSFER_ASSET).upper().strip()
        url = getattr(client, "universal_transfer_url", None) or UNIVERSAL_TRANSFER_ENDPOINT

        params = {
            "asset": asset_clean,
            "amount": amount_str,
            "type": transfer_type,
            "recvWindow": DEFAULT_TRANSFER_RECV_WINDOW_MS,
        }

        res = await client._request(
            "POST",
            url,
            params=params,
            signed=True,
        )

        if res.success:
            tran_id: Optional[Any] = res.data.get("tranId") if isinstance(res.data, dict) else None
            logger.info(
                f"✅ [TRANSFER] {transfer_type}: {amount_str} {asset_clean} "
                f"успешно переведено (tranId={tran_id})"
            )
        else:
            logger.error(
                f"❌ [TRANSFER] Ошибка {transfer_type} на сумму {amount_str} {asset_clean}: "
                f"code={res.error_code}, msg={res.error_msg}"
            )
        return res

