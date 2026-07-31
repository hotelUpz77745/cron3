import asyncio
import time
from consts import DATA_DIR
from c_log import UnifiedLogger
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
            
        analytics_data = Utils.read_json_file(DATA_DIR / "analytics.json")
        if not analytics_data:
            return
            
        # RISK SYSTEM CHECK
        risk_cfg = app_data.get("stop_bot_rirk_system", {})
        if risk_cfg.get("enabled"):
            roi_pct = analytics_data.get("roi_pct", 0.0)
            
            try:
                raw_critical_roi = float(risk_cfg.get("roi_critical_level_pct", -41.0))
                critical_roi = -abs(raw_critical_roi)
            except (ValueError, TypeError):
                logger.error(f"[RISK_SYSTEM] Invalid roi_critical_level_pct in config: {risk_cfg.get('roi_critical_level_pct')}")
                critical_roi = None
            
            if critical_roi is not None and roi_pct <= critical_roi:
                if not getattr(self.bot_core, "is_paused", False):
                    self._is_closing = True
                    logger.critical(f"[RISK_SYSTEM] ROI {roi_pct}% drops below critical {critical_roi}%! Stopping bot.")
                    
                    # 1. Block new entries
                    self.bot_core.entry_blocked = True
                    
                    try:
                        # 2. Close all positions
                        await self.bot_core.close_all_positions()
                        
                        # 3. Wait for analytics to sync (debounce timeout in AnalyticsManager is 10 sec)
                        logger.info("[RISK_SYSTEM] Waiting for analytics sync (12s)...")
                        await asyncio.sleep(12.0)
                        
                        # 4. Turn off bot
                        self.bot_core.is_paused = True
                        
                        # Turn off the risk system in config so it doesn't loop
                        app_data["stop_bot_rirk_system"]["enabled"] = False
                        Utils.write_json_file(app_json_path, app_data)
                        
                        msg = f"⛔ <b>CRITICAL RISK STOP</b> ⛔\n\nROI dropped to <b>{roi_pct}%</b> (Threshold: {critical_roi}%).\nAll positions closed. Bot is PAUSED."
                        if getattr(self.bot_core, 'notifier', None):
                            await self.bot_core.notifier.send_alert(msg)
                            
                    except Exception as e:
                        logger.error(f"[RISK_SYSTEM] Error during stop sequence: {e}")
                    finally:
                        self.bot_core.entry_blocked = False
                        self._is_closing = False
                    
                    return # Exit check early
        
        auto_cfg = app_data.get("auto_closing")
        if not auto_cfg:
            return
            

            
        net_usdt = analytics_data.get("net_profit_usdt", 0.0)
        
        pos_cfg = auto_cfg.get("positive", {})
        neg_cfg = auto_cfg.get("negative", {})
        
        triggered = False
        action_msg = ""
        
        # Check Positive
        th_pos = pos_cfg.get("threshold")
        if th_pos == 0 or th_pos == 0.0:
            th_pos = None
            
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
        if th_neg == 0 or th_neg == 0.0:
            th_neg = None
            
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
