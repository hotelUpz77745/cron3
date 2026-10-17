# tests/test_binance_validator.py
# -*- coding: utf-8 -*-
"""
==============================================================================
Path: tests/test_binance_validator.py
Role: Институциональный юнит-тест валидатора ответов биржи Binance (OrderValidator & APIResponse)
==============================================================================
"""

import unittest
from unittest.mock import MagicMock
from API.BINANCE.validator import APIResponse, OrderValidator


class TestBinanceOrderValidator(unittest.TestCase):
    def setUp(self):
        self.mock_logger = MagicMock()
        self.validator = OrderValidator(self.mock_logger)

    def test_api_response_dataclass(self):
        """Проверка структуры и дефолтных полей APIResponse."""
        resp = APIResponse(success=True, data={"orderId": 123})
        self.assertTrue(resp.success)
        self.assertEqual(resp.data["orderId"], 123)
        self.assertIsNone(resp.error_code)
        self.assertIsNone(resp.error_msg)
        self.assertEqual(resp.meta, {})

    def test_validate_order_create_upstream_error(self):
        """Если ответ биржи изначально содержит ошибку, он возвращается без изменений."""
        resp = APIResponse(success=False, error_code=-2019, error_msg="Margin is insufficient.")
        validated = self.validator.validate_order_create(resp, market_type="MARKET")
        self.assertFalse(validated.success)
        self.assertEqual(validated.error_code, -2019)
        self.mock_logger.error.assert_called_once()

    def test_validate_order_create_invalid_format(self):
        """Если data не является словарем, возвращается INVALID_RESPONSE_FORMAT."""
        resp = APIResponse(success=True, data="Not a dictionary")
        validated = self.validator.validate_order_create(resp, market_type="MARKET")
        self.assertFalse(validated.success)
        self.assertEqual(validated.error_msg, "INVALID_RESPONSE_FORMAT")

    def test_validate_order_create_market_filled(self):
        """Успешное исполнение рыночного ордера (status=FILLED, executedQty > 0)."""
        resp = APIResponse(
            success=True,
            data={"status": "FILLED", "executedQty": "0.150", "avgPrice": "50000.0"}
        )
        validated = self.validator.validate_order_create(resp, market_type="MARKET")
        self.assertTrue(validated.success)
        self.assertIsNone(validated.error_msg)

    def test_validate_order_create_market_not_filled(self):
        """Рыночный ордер с нулевым исполнением или статусом != FILLED отклоняется."""
        resp = APIResponse(
            success=True,
            data={"status": "NEW", "executedQty": "0.0", "avgPrice": "0.0"}
        )
        validated = self.validator.validate_order_create(resp, market_type="MARKET")
        self.assertFalse(validated.success)
        self.assertEqual(validated.error_msg, "MARKET_NOT_FILLED")

    def test_validate_order_create_limit_valid_statuses(self):
        """Лимитный ордер валидируется для статусов NEW, PARTIALLY_FILLED, FILLED."""
        for valid_st in ("NEW", "PARTIALLY_FILLED", "FILLED"):
            resp = APIResponse(
                success=True,
                data={"status": valid_st, "executedQty": "0.0", "avgPrice": "0.0"}
            )
            validated = self.validator.validate_order_create(resp, market_type="LIMIT")
            self.assertTrue(validated.success, f"Failed for valid limit status {valid_st}")

    def test_validate_order_create_limit_rejected_statuses(self):
        """Лимитный ордер с невалидным статусом (CANCELED, REJECTED, EXPIRED) отклоняется."""
        for invalid_st in ("CANCELED", "REJECTED", "EXPIRED"):
            resp = APIResponse(
                success=True,
                data={"status": invalid_st, "executedQty": "0.0", "avgPrice": "0.0"}
            )
            validated = self.validator.validate_order_create(resp, market_type="LIMIT")
            self.assertFalse(validated.success)
            self.assertEqual(validated.error_msg, f"ORDER_STATUS={invalid_st}")

    def test_validate_order_cancel_2011_unknown_order_converted_to_success(self):
        """Код ошибки -2011 (Unknown order / already canceled) конвертируется в успех."""
        resp = APIResponse(success=False, error_code=-2011, error_msg="Unknown order sent.")
        validated = self.validator.validate_order_cancel(resp)
        self.assertTrue(validated.success)
        self.assertIsNone(validated.error_msg)
        self.assertIsNone(validated.error_code)

    def test_validate_order_cancel_other_error(self):
        """Любая другая ошибка отмены ордера остается ошибкой."""
        resp = APIResponse(success=False, error_code=-1021, error_msg="Timestamp for this request is outside recvWindow.")
        validated = self.validator.validate_order_cancel(resp)
        self.assertFalse(validated.success)
        self.assertEqual(validated.error_code, -1021)
        self.mock_logger.error.assert_called_once()

    def test_validate_order_cancel_batch_list(self):
        """Валидация списка результатов отмены пачки batchOrders."""
        # Успешный список: один отменен, второй с -2011
        resp = APIResponse(
            success=True,
            data=[
                {"status": "CANCELED", "orderId": 1},
                {"code": -2011, "msg": "Unknown order sent"}
            ]
        )
        validated = self.validator.validate_order_cancel(resp)
        self.assertTrue(validated.success)

        # Список с реальной ошибкой (code != -2011)
        resp_err = APIResponse(
            success=True,
            data=[
                {"status": "CANCELED", "orderId": 1},
                {"code": -1102, "msg": "Mandatory parameter was not sent"}
            ]
        )
        validated_err = self.validator.validate_order_cancel(resp_err)
        self.assertFalse(validated_err.success)
        self.assertIn("CANCEL_BATCH_PARTIAL_FAILED", validated_err.error_msg)

    def test_validate_order_cancel_single_dict(self):
        """Валидация одиночного словаря ответа на отмену ордера."""
        resp_ok = APIResponse(success=True, data={"status": "CANCELED", "orderId": 999})
        validated_ok = self.validator.validate_order_cancel(resp_ok)
        self.assertTrue(validated_ok.success)

        resp_fail = APIResponse(success=True, data={"status": "FILLED", "orderId": 999})
        validated_fail = self.validator.validate_order_cancel(resp_fail)
        self.assertFalse(validated_fail.success)
        self.assertEqual(validated_fail.error_msg, "CANCEL_FAILED: status=FILLED")


if __name__ == "__main__":
    unittest.main()
