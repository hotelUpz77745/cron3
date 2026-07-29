import asyncio
import time
from consts import DATA_DIR, UnifiedLogger
from c_utils import Utils

logger = UnifiedLogger("AutoCloser")

class AutoCloser:
    def __init__(self, bot_core):
        self.bot_core = bot_core
        self._is_closing = False

    async def check(self):
        if self._is_closing:
            return
            
        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path)
        if not app_data:
            return
            
        auto_cfg = app_data.get("auto_closing")
        if not auto_cfg:
            return
            
        analytics_data = Utils.read_json_file(DATA_DIR / "analytics.json")
        if not analytics_data:
            return
            
        net_usdt = analytics_data.get("net_profit_usdt", 0.0)
        
        pos_cfg = auto_cfg.get("positive", {})
        neg_cfg = auto_cfg.get("negative", {})
        
        triggered = False
        action_msg = ""
        
        # Check Positive
        th_pos = pos_cfg.get("threshold")
        if th_pos is not None and net_usdt >= th_pos:
            self._is_closing = True
            triggered = True
            logger.info(f"[AUTO_CLOSER] Net Profit ({net_usdt}) >= {th_pos}. Auto-closing!")
            
            inc = pos_cfg.get("threshold_increment")
            if inc:
                new_th = th_pos + inc
                while new_th <= net_usdt:
                    new_th += inc
                new_th = round(new_th, 4)
                app_data["auto_closing"]["positive"]["threshold"] = new_th
                action_msg = f"🟢 <b>AUTO-CLOSE (POSITIVE)</b>\nNet Profit reached <b>{net_usdt} USDT</b> (Threshold: {th_pos}).\nPositions closed. New threshold: <b>{new_th}</b>."
                logger.info(f"[AUTO_CLOSER] Increased positive threshold to {new_th}.")
            else:
                # If no increment, we must turn it off (set to null) to prevent infinite loops
                app_data["auto_closing"]["positive"]["threshold"] = None
                action_msg = f"🟢 <b>AUTO-CLOSE (POSITIVE)</b>\nNet Profit reached <b>{net_usdt} USDT</b> (Threshold: {th_pos}).\nPositions closed. Threshold removed."
                logger.warning("[AUTO_CLOSER] No positive threshold_increment found. Threshold set to null to prevent loop.")
                
        # Check Negative (only if positive didn't trigger)
        th_neg = neg_cfg.get("threshold")
        if not triggered and th_neg is not None and net_usdt <= th_neg:
            self._is_closing = True
            triggered = True
            logger.info(f"[AUTO_CLOSER] Net Profit ({net_usdt}) <= {th_neg}. Auto-closing!")
            
            inc = neg_cfg.get("threshold_increment")
            if inc:
                inc = abs(inc) # Ensure it's a positive subtraction step
                new_th = th_neg - inc
                while new_th >= net_usdt:
                    new_th -= inc
                new_th = round(new_th, 4)
                app_data["auto_closing"]["negative"]["threshold"] = new_th
                action_msg = f"🔴 <b>AUTO-CLOSE (NEGATIVE)</b>\nNet Profit dropped to <b>{net_usdt} USDT</b> (Threshold: {th_neg}).\nPositions closed. New threshold: <b>{new_th}</b>."
                logger.info(f"[AUTO_CLOSER] Decreased negative threshold to {new_th}.")
            else:
                app_data["auto_closing"]["negative"]["threshold"] = None
                action_msg = f"🔴 <b>AUTO-CLOSE (NEGATIVE)</b>\nNet Profit dropped to <b>{net_usdt} USDT</b> (Threshold: {th_neg}).\nPositions closed. Threshold removed."
                logger.warning("[AUTO_CLOSER] No negative threshold_increment found. Threshold set to null to prevent loop.")

        if triggered:
            try:
                # 1. Close positions
                await self.bot_core.close_all_positions()
                
                # 2. Update config file
                Utils.write_json_file(app_json_path, app_data)
                
                # 3. Notify
                if action_msg and getattr(self.bot_core, 'notifier', None):
                    await self.bot_core.notifier.send_alert(action_msg)
            except Exception as e:
                logger.error(f"[AUTO_CLOSER] Error during auto-close sequence: {e}")
            finally:
                self._is_closing = False
