import asyncio
import copy
import time
from consts import DATA_DIR, ANALYTICS_DIR
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
            
        analytics_data = Utils.read_json_file(ANALYTICS_DIR / "analytics.json")
        if not analytics_data:
            return
            
        # RISK SYSTEM CHECK (если задана)
        risk_cfg = app_data.get("stop_bot_rirk_system")
        if risk_cfg and risk_cfg.get("enabled"):
            roi_pct = analytics_data.get("roi_pct", 0.0)
            
            try:
                raw_critical_roi = float(risk_cfg["roi_critical_level_pct"])
                critical_roi = -abs(raw_critical_roi)
            except (ValueError, TypeError):
                logger.error(f"[RISK_SYSTEM] Invalid roi_critical_level_pct in config: {risk_cfg['roi_critical_level_pct']}")
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
             # 1. СТРАТЕГИЧЕСКИЕ КУПОЛА (PER-STRATEGY AUTO-CLOSING)
        active_strats = {}
        if hasattr(self.bot_core, "strategy_registry"):
            active_strats = self.bot_core.strategy_registry.active_strategies

        for strat_id, strat_cfg in active_strats.items():
            strat_app_cfg = app_data.get("active_strategies", {}).get(strat_id, {})
            strat_auto_cfg = strat_app_cfg.get("auto_closing") or strat_cfg.auto_closing_cfg
            if not strat_auto_cfg:
                continue

            strat_stats_path = ANALYTICS_DIR / "STATS" / f"analytics_{strat_id}.json"
            if strat_stats_path.exists():
                strat_analytics = Utils.read_json_file(strat_stats_path) or {}
            else:
                strat_analytics = analytics_data

            strat_net = float(strat_analytics.get("net_profit_usdt", 0.0))
            pos_cfg = strat_auto_cfg.get("positive", {})
            neg_cfg = strat_auto_cfg.get("negative", {})

            strat_triggered = False
            strat_msg = ""

            # Check Positive for Strategy Dome
            th_pos = pos_cfg.get("threshold")
            if th_pos == 0 or th_pos == 0.0:
                th_pos = None

            if th_pos is not None and strat_net >= th_pos:
                strat_triggered = True
                logger.info(f"[AUTO_CLOSER][{strat_id}] Net Profit ({strat_net}) >= {th_pos}. Auto-closing strategy dome!")
                inc = pos_cfg.get("threshold_increment")
                if inc:
                    new_th = th_pos + inc
                    while new_th <= strat_net:
                        new_th += inc
                    new_th = round(new_th, 4)
                    if "active_strategies" in app_data and strat_id in app_data["active_strategies"]:
                        if "auto_closing" not in app_data["active_strategies"][strat_id]:
                            app_data["active_strategies"][strat_id]["auto_closing"] = copy.deepcopy(strat_auto_cfg)
                        app_data["active_strategies"][strat_id]["auto_closing"]["positive"]["threshold"] = new_th
                    strat_msg = (
                        f"🟢 <b>AUTO-CLOSE КУПОЛА [{strat_cfg.name}]</b>\n"
                        f"Профит стратегии достиг <b>{strat_net} USDT</b> (Порог: {th_pos}).\n"
                        f"Закрыты позиции монет: {strat_cfg.symbols}.\n"
                        f"Новый порог купола: <b>{new_th} USDT</b>."
                    )
                else:
                    if "active_strategies" in app_data and strat_id in app_data["active_strategies"]:
                        if "auto_closing" not in app_data["active_strategies"][strat_id]:
                            app_data["active_strategies"][strat_id]["auto_closing"] = copy.deepcopy(strat_auto_cfg)
                        app_data["active_strategies"][strat_id]["auto_closing"]["positive"]["threshold"] = None
                    strat_msg = (
                        f"🟢 <b>AUTO-CLOSE КУПОЛА [{strat_cfg.name}]</b>\n"
                        f"Профит стратегии достиг <b>{strat_net} USDT</b> (Порог: {th_pos}).\n"
                        f"Закрыты позиции монет: {strat_cfg.symbols}.\n"
                        f"Порог купола снят (null)."
                    )

            # Check Negative for Strategy Dome
            th_neg = neg_cfg.get("threshold")
            if th_neg == 0 or th_neg == 0.0:
                th_neg = None

            if not strat_triggered and th_neg is not None and strat_net <= th_neg:
                strat_triggered = True
                logger.info(f"[AUTO_CLOSER][{strat_id}] Net Profit ({strat_net}) <= {th_neg}. Auto-closing strategy dome!")
                inc = neg_cfg.get("threshold_increment")
                if inc:
                    inc = abs(inc)
                    new_th = th_neg - inc
                    while new_th >= strat_net:
                        new_th -= inc
                    new_th = round(new_th, 4)
                    if "active_strategies" in app_data and strat_id in app_data["active_strategies"]:
                        if "auto_closing" not in app_data["active_strategies"][strat_id]:
                            app_data["active_strategies"][strat_id]["auto_closing"] = copy.deepcopy(strat_auto_cfg)
                        app_data["active_strategies"][strat_id]["auto_closing"]["negative"]["threshold"] = new_th
                    strat_msg = (
                        f"🔴 <b>AUTO-CLOSE КУПОЛА [{strat_cfg.name}]</b>\n"
                        f"Профит стратегии опустился до <b>{strat_net} USDT</b> (Порог: {th_neg}).\n"
                        f"Закрыты позиции монет: {strat_cfg.symbols}.\n"
                        f"Новый порог купола: <b>{new_th} USDT</b>."
                    )
                else:
                    if "active_strategies" in app_data and strat_id in app_data["active_strategies"]:
                        if "auto_closing" not in app_data["active_strategies"][strat_id]:
                            app_data["active_strategies"][strat_id]["auto_closing"] = copy.deepcopy(strat_auto_cfg)
                        app_data["active_strategies"][strat_id]["auto_closing"]["negative"]["threshold"] = None
                    strat_msg = (
                        f"🔴 <b>AUTO-CLOSE КУПОЛА [{strat_cfg.name}]</b>\n"
                        f"Профит стратегии опустился до <b>{strat_net} USDT</b> (Порог: {th_neg}).\n"
                        f"Закрыты позиции монет: {strat_cfg.symbols}.\n"
                        f"Порог купола снят (null)."
                    )

            if strat_triggered:
                try:
                    self._is_closing = True
                    await self.bot_core.close_strategy_positions(strat_id)
                    Utils.write_json_file(app_json_path, app_data)
                    if strat_msg and getattr(self.bot_core, 'notifier', None):
                        await self.bot_core.notifier.send_alert(strat_msg)
                except Exception as e:
                    logger.error(f"[AUTO_CLOSER][{strat_id}] Error in strategy auto-close: {e}")
                finally:
                    self._is_closing = False

        # 2. ГЛОБАЛЬНЫЙ ПОРТФЕЛЬНЫЙ AUTO-CLOSING (если задан на верхнем уровне)
        auto_cfg = app_data.get("auto_closing")
        if not auto_cfg:
            return

        net_usdt = analytics_data.get("net_profit_usdt", 0.0)
        pos_cfg = auto_cfg.get("positive", {})
        neg_cfg = auto_cfg.get("negative", {})
        
        triggered = False
        action_msg = ""
        
        # Check Positive Portfolio
        th_pos = pos_cfg.get("threshold")
        if th_pos == 0 or th_pos == 0.0:
            th_pos = None
            
        if th_pos is not None and net_usdt >= th_pos:
            self._is_closing = True
            triggered = True
            logger.info(f"[AUTO_CLOSER] Net Profit ({net_usdt}) >= {th_pos}. Auto-closing portfolio!")
            
            inc = pos_cfg.get("threshold_increment")
            if inc:
                new_th = th_pos + inc
                while new_th <= net_usdt:
                    new_th += inc
                new_th = round(new_th, 4)
                app_data["auto_closing"]["positive"]["threshold"] = new_th
                action_msg = f"🟢 <b>AUTO-CLOSE (PORTFOLIO POSITIVE)</b>\nNet Profit reached <b>{net_usdt} USDT</b> (Threshold: {th_pos}).\nPositions closed. New threshold: <b>{new_th}</b>."
                logger.info(f"[AUTO_CLOSER] Increased positive threshold to {new_th}.")
            else:
                app_data["auto_closing"]["positive"]["threshold"] = None
                action_msg = f"🟢 <b>AUTO-CLOSE (PORTFOLIO POSITIVE)</b>\nNet Profit reached <b>{net_usdt} USDT</b> (Threshold: {th_pos}).\nPositions closed. Threshold removed."
                
        # Check Negative Portfolio
        th_neg = neg_cfg.get("threshold")
        if th_neg == 0 or th_neg == 0.0:
            th_neg = None
            
        if not triggered and th_neg is not None and net_usdt <= th_neg:
            self._is_closing = True
            triggered = True
            logger.info(f"[AUTO_CLOSER] Net Profit ({net_usdt}) <= {th_neg}. Auto-closing portfolio!")
            
            inc = neg_cfg.get("threshold_increment")
            if inc:
                inc = abs(inc)
                new_th = th_neg - inc
                while new_th >= net_usdt:
                    new_th -= inc
                new_th = round(new_th, 4)
                app_data["auto_closing"]["negative"]["threshold"] = new_th
                action_msg = f"🔴 <b>AUTO-CLOSE (PORTFOLIO NEGATIVE)</b>\nNet Profit dropped to <b>{net_usdt} USDT</b> (Threshold: {th_neg}).\nPositions closed. New threshold: <b>{new_th}</b>."
                logger.info(f"[AUTO_CLOSER] Decreased negative threshold to {new_th}.")
            else:
                app_data["auto_closing"]["negative"]["threshold"] = None
                action_msg = f"🔴 <b>AUTO-CLOSE (PORTFOLIO NEGATIVE)</b>\nNet Profit dropped to <b>{net_usdt} USDT</b> (Threshold: {th_neg}).\nPositions closed. Threshold removed."

        if triggered:
            try:
                await self.bot_core.close_all_positions()
                Utils.write_json_file(app_json_path, app_data)
                if action_msg and getattr(self.bot_core, 'notifier', None):
                    await self.bot_core.notifier.send_alert(action_msg)
            except Exception as e:
                logger.error(f"[AUTO_CLOSER] Error during portfolio auto-close: {e}")
            finally:
                self._is_closing = False
