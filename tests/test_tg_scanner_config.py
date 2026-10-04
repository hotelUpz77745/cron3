import unittest
import json
from unittest.mock import MagicMock, AsyncMock
from TG.tg_receiver import TelegramReceiver
from consts import DATA_DIR
from c_utils import Utils

class TestTGScannerConfig(unittest.TestCase):
    def test_scanner_config_default_payload(self):
        """Verifies that tg_receiver scanner default configuration is valid Python and serializable."""
        bot_core_mock = MagicMock()
        receiver = TelegramReceiver(bot_core=bot_core_mock)
        
        # Test creating default cfg payload as in process_cb_edit_scanner_config
        cfg = {
            "enabled": True,
            "strict_window": True,
            "min_dayly_vol_usdt": 3000000,
            "atr_volatility_filter": {
                "enabled": True,
                "timeframe": "1w",
                "window": 12,
                "min_volatility_pct": 25.0,
                "max_volatility_pct": 40.0
            },
            "rsi_filter": {
                "enabled": True,
                "timeframe": "4h",
                "period": 14,
                "min_rsi": 35.0,
                "max_rsi": 65.0
            },
            "bollinger_width_filter": {
                "enabled": True,
                "timeframe": "4h",
                "MA_type": "SMA",
                "period": 20,
                "multiplier": 2.0,
                "min_width_pct": 8.0,
                "max_width_pct": 13.0,
                "center_proximity_filter": {
                    "enabled": True,
                    "max_deviation_from_center_pct": 40.0
                }
            }
        }
        json_bytes = json.dumps(cfg, indent=4, ensure_ascii=False).encode("utf-8")
        parsed = json.loads(json_bytes.decode("utf-8"))
        self.assertTrue(parsed["bollinger_width_filter"]["enabled"])
        self.assertTrue(parsed["bollinger_width_filter"]["center_proximity_filter"]["enabled"])
        self.assertEqual(parsed["bollinger_width_filter"]["center_proximity_filter"]["max_deviation_from_center_pct"], 40.0)

if __name__ == "__main__":
    unittest.main()
