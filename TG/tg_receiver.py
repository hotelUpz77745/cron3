# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\TG\tg_receiver.py
# Role: tg_receiver.py module

# ==============================================================================
# Path: TG/tg_receiver.py
# Role: Telegram-бот для управления торговым ядром (Start/Stop, Настройки)
# ==============================================================================
import asyncio
import os
from pathlib import Path
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, BufferedInputFile, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.types import ErrorEvent

from c_log import UnifiedLogger
from consts import TG_TOKEN, ANALYTICS_DIR, ANALYTICS_STATS_DIR, ANALYTICS_LEDGERS_DIR, TG_ALLOWED_USERS, ACTIVE_SYMBOLS, BASE_DIR
from TG.template_manager import TemplateManager

try:
    from quant_calculator import QuantCalculator
except ImportError:
    QuantCalculator = None
from consts import _CFG
import json
from consts import DATA_DIR
from c_utils import Utils
import json, time, csv, re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from ANALYTICS.plotter import generate_coin_analytics, generate_equity_curve, generate_balance_curve
from TG.universe_commands import setup_universe_handlers, get_user_universe_id, set_user_universe_id, render_universes_page
import io
from typing import Optional, List, Dict, Any, Tuple
from consts import DATA_DIR, _CFG
import subprocess
logger = UnifiedLogger("TGReceiver")

class TGStates(StatesGroup):
    waiting_for_add_symbol = State()
    waiting_for_add_strategy_choice = State()
    waiting_for_add_custom_invest = State()
    waiting_for_add_custom_lev = State()
    waiting_for_reassign_strat = State()
    waiting_for_strat_super_grid_json = State()
    waiting_for_del_symbol = State()
    waiting_for_edit_symbol = State()
    waiting_for_invest_size = State()
    waiting_for_avg_params = State()
    waiting_for_tp_params = State()
    waiting_for_base_json = State()
    waiting_for_super_grid_json = State()
    waiting_for_initial_balance = State()
    waiting_for_reset_confirm = State()
    waiting_for_scanner_json = State()
    waiting_for_close_all_confirm = State()
    waiting_for_notif_neg = State()
    waiting_for_notif_pos = State()
    waiting_for_autoclose_pos_th = State()
    waiting_for_autoclose_pos_inc = State()
    waiting_for_autoclose_neg_th = State()
    waiting_for_autoclose_neg_inc = State()
    waiting_for_risk_roi = State()
    waiting_for_risk_threshold = State()
    waiting_for_risk_max_threshold = State()

class TelegramReceiver:
    def __init__(self, bot_core):
        self.bot_core = bot_core
        self.bot = Bot(token=TG_TOKEN)
        self.dp = Dispatcher(storage=MemoryStorage())
        self.template_manager = TemplateManager(bot_core=self.bot_core)
        self._lock = asyncio.Lock()
        
        # Регистрация простого middleware для проверки прав доступа
        @self.dp.message.outer_middleware()
        async def check_user_message(handler, event: Message, data: dict):
            if TG_ALLOWED_USERS and event.from_user.id not in TG_ALLOWED_USERS:
                logger.warning(f"Unauthorized access attempt by {event.from_user.id}")
                return
                
            if not event.text and not event.document:
                await event.answer("⚠️ Бот принимает только текстовые команды или документы (файлы). Пожалуйста, не отправляйте фото, видео, стикеры или пустые сообщения.")
                return
                
            return await handler(event, data)

        @self.dp.callback_query.outer_middleware()
        async def check_user_callback(handler, event: CallbackQuery, data: dict):
            if TG_ALLOWED_USERS and event.from_user.id not in TG_ALLOWED_USERS:
                logger.warning(f"Unauthorized access attempt by {event.from_user.id}")
                return
            return await handler(event, data)

        setup_universe_handlers(self.dp, self.bot_core)
        self._register_handlers()

    def _get_main_keyboard(self):
        keyboard = [
            [
                KeyboardButton(text="▶️ Start"),
                KeyboardButton(text="ℹ️ Status"),
                KeyboardButton(text="⏸️ Stop")
            ],
            [
                KeyboardButton(text="🌌 Set Universes"),
                KeyboardButton(text="📊 Analytics")
            ],
            [
                KeyboardButton(text="⚙️ Set Coins"),
                KeyboardButton(text="🛡️ Risk System"),
                KeyboardButton(text="📜 Logs")
            ],
            [
                KeyboardButton(text="🚨 Close All")
            ]
        ]
        return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

    def _get_set_coins_keyboard(self):
        keyboard = [
            [
                KeyboardButton(text="➕ Добавить монету"),
                KeyboardButton(text="🗑️ Удалить монету")
            ],
            [
                KeyboardButton(text="🔙 Назад")
            ]
        ]
        return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

    def _get_universe_analytics(self, user_id: int, target_uid: Optional[str] = None):
        current_uid = target_uid or get_user_universe_id(user_id, self.bot_core)
        meta = self._get_strategy_meta(current_uid)
        univ_name = meta.get("name", current_uid)

        analytics_path = ANALYTICS_STATS_DIR / f"analytics_{current_uid}.json"
        if not analytics_path.exists():
            matches = list(ANALYTICS_STATS_DIR.glob(f"analytics_{current_uid}*.json"))
            if matches:
                analytics_path = sorted(matches)[0]
            else:
                pure_id = current_uid.split("__")[0]
                pure_matches = list(ANALYTICS_STATS_DIR.glob(f"analytics_{pure_id}*.json"))
                if pure_matches:
                    analytics_path = sorted(pure_matches)[0]
                else:
                    analytics_path = ANALYTICS_STATS_DIR / f"analytics_{current_uid}.json"

        csv_path = ANALYTICS_LEDGERS_DIR / f"trades_ledger_{current_uid}.txt"
        if not csv_path.exists() or csv_path.stat().st_size <= 60:
            matches_csv = list(ANALYTICS_LEDGERS_DIR.glob(f"trades_ledger_{current_uid}*.txt"))
            found = [m for m in matches_csv if m.stat().st_size > 60]
            if found:
                csv_path = sorted(found)[0]
            else:
                pure_id = current_uid.split("__")[0]
                pure_csv_matches = list(ANALYTICS_LEDGERS_DIR.glob(f"trades_ledger_{pure_id}*.txt"))
                found_pure = [m for m in pure_csv_matches if m.stat().st_size > 60]
                if found_pure:
                    csv_path = sorted(found_pure)[0]
                else:
                    general_ledger = ANALYTICS_LEDGERS_DIR / "trades_ledger.txt"
                    if general_ledger.exists() and general_ledger.stat().st_size > 60:
                        csv_path = general_ledger
                    else:
                        csv_path = ANALYTICS_LEDGERS_DIR / f"trades_ledger_{current_uid}.txt"

        return current_uid, univ_name, analytics_path, csv_path

    def _get_strategy_meta(self, strat_id: str) -> dict:
        if hasattr(self, "bot_core") and getattr(self.bot_core, "strategy_registry", None):
            strat = self.bot_core.strategy_registry.get_strategy(strat_id)
            if strat and isinstance(getattr(strat, "name", None), str):
                return {
                    "strategy_id": strat_id,
                    "name": strat.name,
                    "description": strat.description or "Пользовательская стратегия",
                    "invest_size": strat.invest_size,
                    "leverage": strat.leverage,
                    "super_grid": getattr(strat, "super_grid_enabled", False),
                    "super_grid_cfg": getattr(strat, "super_grid_cfg", {}),
                    "tp_summary": "Настроен",
                    "start_balance_usdt": getattr(strat, "start_balance_usdt", 57.0)
                }

        univ_dir = DATA_DIR / "universes"
        app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
        active_strats = app_data.get("active_strategies", {})
        strat_app = active_strats.get(strat_id, {})

        pure_id = strat_app.get("template") or strat_id
        if not (univ_dir / f"{pure_id}.json").exists():
            if "__" in pure_id:
                pure_id = pure_id.split("__")[0]
            else:
                for sfx in ("__off", "__classic", "__fixed", "__toxic"):
                    if pure_id.endswith(sfx):
                        pure_id = pure_id[:-len(sfx)]
                        break

        jf = univ_dir / f"{pure_id}.json"
        if jf.exists():
            u_meta = Utils.read_json_file(jf)
        else:
            univ_data = Utils.read_json_file(DATA_DIR / "universes.json") or {}
            u_meta = univ_data.get(strat_id, {})
        
        name = u_meta.get("name", strat_id)
        description = u_meta.get("description", "Пользовательская стратегия")
        invest_size = (
            (u_meta.get("LONG") or {}).get("invest_size")
            or (u_meta.get("SHORT") or {}).get("invest_size")
            or u_meta.get("invest_size", 400.0)
        )
        leverage = u_meta.get("leverage", 20)
        start_bal = float((strat_app.get("analytics") or {}).get("start_balance_usdt", u_meta.get("start_balance_usdt", 57.0)))
        
        sg_cfg = strat_app.get("super_grid") or u_meta.get("super_grid") or u_meta.get("overrides", {}).get("super_grid", {})
        sg_enabled = sg_cfg.get("enabled", u_meta.get("super_grid_enabled", False))
        
        tp_map = (
            (u_meta.get("LONG") or {}).get("tp_map")
            or (u_meta.get("SHORT") or {}).get("tp_map")
            or u_meta.get("tp_map")
            or u_meta.get("overrides", {}).get("tp_map", {})
        )
        if tp_map:
            indents = [str(v.get("indent", "?")) for v in tp_map.values() if isinstance(v, dict)]
            if len(set(indents)) == 1:
                tp_summary = f"Плоский {indents[0]}% (все уровни)"
            else:
                tp_summary = f"{', '.join(indents[:3])}%..."
        else:
            tp_summary = "Базовый 0.6%..2.5% (из _base.json)"
            
        return {
            "strategy_id": strat_id,
            "name": name,
            "description": description,
            "invest_size": invest_size,
            "leverage": leverage,
            "super_grid": sg_enabled,
            "super_grid_cfg": sg_cfg,
            "tp_summary": tp_summary,
            "start_balance_usdt": start_bal
        }

    def _get_confirm_start_keyboard(self):
        keyboard = [
            [
                KeyboardButton(text="✅ Confirm Start"),
                KeyboardButton(text="⚙️ Set Coins")
            ],
            [
                KeyboardButton(text="🔙 Back")
            ]
        ]
        return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

    def _get_back_keyboard(self):
        keyboard = [
            [
                KeyboardButton(text="🔙 Back")
            ]
        ]
        return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

    def _get_portfolio_all_analytics_data(self) -> Tuple[dict, dict, str]:
        """
        Агрегирует сводные глобальные данные и данные per_coin со всех активных куполов/стратегий.
        Возвращает: (agg_summary, agg_per_coin, last_trade_time_portfolio)
        """
        agg_summary = {
            "start_balance_usdt": 0.0,
            "cur_balance_usdt": 0.0,
            "peak_balance_usdt": 0.0,
            "min_balance_usdt": 0.0,
            "net_profit_usdt": 0.0,
            "realized_pnl_usdt": 0.0,
            "realized_pnl_net_usdt": 0.0,
            "unrealized_pnl_usdt": 0.0,
            "total_commission_usdt": 0.0,
            "total_funding_usdt": 0.0,
            "max_drawdown_usdt": 0.0,
            "performance_usdt": 0.0,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "total_margin_used_usdt": 0.0,
            "free_margin_usdt": 0.0,
            "total_grid_size_usdt": 0.0,
            "current_grid_load_usdt": 0.0,
            "universes_count": 0,
        }
        agg_per_coin: Dict[str, dict] = {}
        last_trade_dt_str = "Нет сделок"

        active_uids = []
        if hasattr(self.bot_core, "strategy_registry"):
            active_strats = self.bot_core.strategy_registry.get_active_strategies() if hasattr(self.bot_core.strategy_registry, "get_active_strategies") else getattr(self.bot_core.strategy_registry, "active_strategies", {})
            active_uids = list(active_strats.keys())

        files_to_read = []
        if active_uids:
            for uid in active_uids:
                p = ANALYTICS_STATS_DIR / f"analytics_{uid}.json"
                if p.exists():
                    files_to_read.append((uid, p))
                else:
                    matches = list(ANALYTICS_STATS_DIR.glob(f"analytics_{uid}*.json"))
                    if matches:
                        files_to_read.append((uid, sorted(matches)[0]))
        if not files_to_read:
            for p in ANALYTICS_STATS_DIR.glob("analytics_*.json"):
                if p.name in ("analytics_all.json", "analytics.json"):
                    continue
                uid = p.stem.replace("analytics_", "")
                files_to_read.append((uid, p))

        agg_summary["universes_count"] = len(files_to_read)

        for uid, p in files_to_read:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                st_bal = float(data.get("start_balance_usdt", 0.0))
                cu_bal = float(data.get("cur_balance_usdt", st_bal))
                pk_bal = float(data.get("peak_balance_usdt", max(st_bal, cu_bal)))
                mn_bal = float(data.get("min_balance_usdt", min(st_bal, cu_bal)))
                net_p = float(data.get("net_profit_usdt", 0.0))
                rl_p = float(data.get("realized_pnl_usdt", 0.0))
                rl_net = float(data.get("realized_pnl_net_usdt", rl_p))
                unrl = float(data.get("unrealized_pnl_usdt", 0.0))
                comm = float(data.get("total_commission_usdt", 0.0))
                fund = float(data.get("total_funding_usdt", 0.0))
                dd = float(data.get("max_drawdown_usdt", 0.0))
                perf = float(data.get("performance_usdt", 0.0))
                tr_cnt = int(data.get("total_trades", 0))
                w_cnt = int(data.get("winning_trades", 0))
                l_cnt = int(data.get("losing_trades", 0))
                m_used = float(data.get("total_margin_used_usdt", 0.0))
                f_marg = float(data.get("free_margin_usdt", 0.0))
                g_load = float(data.get("current_grid_load_usdt", 0.0))
                g_size = float(data.get("total_grid_size_usdt", 0.0))

                per_coin = data.get("per_coin", {})
                if comm == 0.0 and per_coin:
                    comm = sum(float(c.get("commission_usdt", 0.0)) for c in per_coin.values())
                if fund == 0.0 and per_coin:
                    fund = sum(float(c.get("funding_usdt", 0.0)) for c in per_coin.values())
                if rl_net == 0.0 and per_coin:
                    rl_net = sum(float(c.get("realized_pnl_net_usdt", c.get("realized_pnl_usdt", 0.0))) for c in per_coin.values())
                if g_load == 0.0 and per_coin:
                    g_load = sum(float(c.get("current_grid_load_usdt", 0.0)) for c in per_coin.values())
                if g_size == 0.0 and per_coin:
                    g_size = sum(float(c.get("grid_capacity_usdt", 0.0)) for c in per_coin.values())
                if m_used == 0.0 and per_coin:
                    m_used = sum(float(c.get("current_margin_usdt", 0.0)) for c in per_coin.values())
                if f_marg == 0.0 and cu_bal > 0:
                    f_marg = max(0.0, cu_bal - m_used)

                agg_summary["start_balance_usdt"] += st_bal
                agg_summary["cur_balance_usdt"] += cu_bal
                agg_summary["peak_balance_usdt"] += pk_bal
                agg_summary["min_balance_usdt"] += mn_bal
                agg_summary["net_profit_usdt"] += net_p
                agg_summary["realized_pnl_usdt"] += rl_p
                agg_summary["realized_pnl_net_usdt"] += rl_net
                agg_summary["unrealized_pnl_usdt"] += unrl
                agg_summary["total_commission_usdt"] += comm
                agg_summary["total_funding_usdt"] += fund
                agg_summary["max_drawdown_usdt"] += dd
                agg_summary["performance_usdt"] += perf
                agg_summary["total_trades"] += tr_cnt
                agg_summary["winning_trades"] += w_cnt
                agg_summary["losing_trades"] += l_cnt
                agg_summary["total_margin_used_usdt"] += m_used
                agg_summary["free_margin_usdt"] += f_marg
                agg_summary["current_grid_load_usdt"] += g_load
                agg_summary["total_grid_size_usdt"] += g_size

                for sym, cinfo in per_coin.items():
                    if sym not in agg_per_coin:
                        agg_per_coin[sym] = {
                            "net_profit_usdt": 0.0,
                            "realized_pnl_usdt": 0.0,
                            "realized_pnl_net_usdt": 0.0,
                            "commission_usdt": 0.0,
                            "funding_usdt": 0.0,
                            "total_trades": 0,
                            "winning_trades": 0,
                            "loss_trades": 0,
                            "current_drawdown": 0.0,
                            "max_drawdown": 0.0,
                            "min_drawdown": 0.0,
                            "universes_active": 0,
                            "grid_capacity_usdt": 0.0,
                            "current_grid_load_usdt": 0.0,
                            "current_margin_usdt": 0.0,
                            "peak_net_profit_usdt": 0.0,
                            "max_drawdown_net_usdt": 0.0,
                            "recovery_factor": 0.0,
                            "abs_recovery_factor": 0.0,
                            "risk_reward_ratio": 0.0,
                        }
                    coin_entry = agg_per_coin[sym]
                    coin_entry["net_profit_usdt"] += float(cinfo.get("net_profit_usdt", 0.0))
                    coin_entry["realized_pnl_usdt"] += float(cinfo.get("realized_pnl_usdt", 0.0))
                    coin_entry["realized_pnl_net_usdt"] += float(cinfo.get("realized_pnl_net_usdt", 0.0))
                    coin_entry["commission_usdt"] += float(cinfo.get("commission_usdt", 0.0))
                    coin_entry["funding_usdt"] += float(cinfo.get("funding_usdt", 0.0))
                    coin_entry["total_trades"] += int(cinfo.get("total_trades", cinfo.get("trades", 0)))
                    coin_entry["winning_trades"] += int(cinfo.get("winning_trades", cinfo.get("win_count", 0)))
                    coin_entry["loss_trades"] += int(cinfo.get("loss_trades", cinfo.get("loss_count", 0)))
                    coin_entry["current_drawdown"] += float(cinfo.get("current_drawdown", 0.0))
                    c_dd = float(cinfo.get("max_drawdown", 0.0))
                    coin_entry["max_drawdown"] = min(coin_entry["max_drawdown"], c_dd)
                    c_peak = float(cinfo.get("peak_net_profit_usdt", max(0.0, float(cinfo.get("net_profit_usdt", 0.0)))))
                    coin_entry["peak_net_profit_usdt"] = round(coin_entry.get("peak_net_profit_usdt", 0.0) + c_peak, 4)
                    c_dd_net = float(cinfo.get("max_drawdown_net_usdt", cinfo.get("max_drawdown", 0.0)))
                    coin_entry["max_drawdown_net_usdt"] = min(coin_entry.get("max_drawdown_net_usdt", 0.0), c_dd_net)
                    coin_entry["universes_active"] += 1
                    coin_entry["is_active"] = coin_entry.get("is_active", False) or cinfo.get("is_active_symbol", True)
                    coin_entry["grid_capacity_usdt"] += float(cinfo.get("grid_capacity_usdt", 0.0))
                    coin_entry["current_grid_load_usdt"] += float(cinfo.get("current_grid_load_usdt", 0.0))
                    coin_entry["current_margin_usdt"] += float(cinfo.get("current_margin_usdt", 0.0))
                    if float(cinfo.get("risk_reward_ratio", 0.0)) != 0:
                        coin_entry["risk_reward_ratio"] = round((coin_entry["risk_reward_ratio"] + float(cinfo.get("risk_reward_ratio", 0.0))) / 2.0, 4) if coin_entry["risk_reward_ratio"] != 0 else float(cinfo.get("risk_reward_ratio", 0.0))

            except Exception as e:
                logger.error(f"Error parsing {p}: {e}")

        for sym, coin_entry in agg_per_coin.items():
            abs_dd = abs(coin_entry.get("max_drawdown_net_usdt", coin_entry.get("max_drawdown", 0.0)))
            if abs_dd > 0:
                coin_entry["recovery_factor"] = round(coin_entry.get("net_profit_usdt", 0.0) / abs_dd, 2)
                coin_entry["abs_recovery_factor"] = round(coin_entry.get("peak_net_profit_usdt", 0.0) / abs_dd, 2)
            else:
                coin_entry["recovery_factor"] = 0.0
                coin_entry["abs_recovery_factor"] = 0.0

        for ledger_file in list(ANALYTICS_LEDGERS_DIR.glob("trades_ledger*.txt")):
            try:
                with open(ledger_file, 'r', encoding='utf-8') as f:
                    r = csv.reader(f, delimiter=';')
                    lines = list(r)
                    if len(lines) > 1:
                        row = lines[-1]
                        dt_cols = [c.strip() for c in row if len(c.strip()) == 19 and c.strip()[4] == '-' and c.strip()[7] == '-']
                        if dt_cols:
                            t_str = dt_cols[-1]
                            if t_str > last_trade_dt_str or last_trade_dt_str == "Нет сделок":
                                last_trade_dt_str = t_str
            except Exception:
                pass

        tot_tr = agg_summary["total_trades"]
        win_tr = agg_summary["winning_trades"]
        agg_summary["winrate_pct"] = round((win_tr / tot_tr * 100.0), 2) if tot_tr > 0 else 0.0
        st_b = agg_summary["start_balance_usdt"]
        np_b = agg_summary["net_profit_usdt"]
        agg_summary["roi_pct"] = round((np_b / st_b * 100.0), 2) if st_b > 0 else 0.0
        m_dd = agg_summary["max_drawdown_usdt"]
        agg_summary["recovery_factor"] = round(np_b / abs(m_dd), 2) if m_dd < 0 else 0.0
        
        cur_b = agg_summary["cur_balance_usdt"]
        m_used_tot = agg_summary["total_margin_used_usdt"]
        agg_summary["margin_load_pct"] = round((m_used_tot / cur_b * 100.0), 2) if cur_b > 0 else 0.0
        f_marg_tot = agg_summary["free_margin_usdt"]
        agg_summary["free_margin_pct"] = round((f_marg_tot / cur_b * 100.0), 2) if cur_b > 0 else 0.0
        tot_grid = agg_summary["total_grid_size_usdt"]
        cur_grid = agg_summary["current_grid_load_usdt"]
        agg_summary["grid_load_pct"] = round((cur_grid / tot_grid * 100.0), 2) if tot_grid > 0 else 0.0

        return agg_summary, agg_per_coin, last_trade_dt_str

    @staticmethod
    def _format_trade_velocity_block(vel_dict: Optional[Dict[str, Any]], title: str = "Trade Velocity (Частота сделок)") -> str:
        if not vel_dict:
            return f"⚡ <b>{title}:</b>\n└ <code>Нет данных о скорости сделок</code>\n\n"

        tf_h = float(vel_dict.get("timeframe_hours", 6.0))
        acc_h = float(vel_dict.get("accumulation_hours", 0.0))
        min_acc = float(vel_dict.get("min_accumulation_hours", 1.0))
        benchmark = float(vel_dict.get("benchmark_speed", 10.0))
        slice_trades = int(vel_dict.get("slice_trades_count", 0))
        slice_coins = int(vel_dict.get("slice_coins_count", 0))
        status_desc = str(vel_dict.get("status_desc", "NORMAL"))
        status = str(vel_dict.get("status", "NORMAL"))

        if status == "HIGH_VOLATILITY":
            status_icon = "🔥"
        elif status == "LOW_VOLATILITY":
            status_icon = "❄️"
        elif status == "ACCUMULATING":
            status_icon = "⏳"
        else:
            status_icon = "🟢"

        if not vel_dict.get("is_ready_for_alerts") or vel_dict.get("avg_effective_speed") is None:
            return (
                f"⚡ <b>{title}:</b>\n"
                f"├ <b>Скорость:</b> ⏳ <i>Накопление ({acc_h:.1f}ч из {min_acc:.1f}ч)</i>\n"
                f"├ <b>Сделок в окне:</b> <code>{slice_trades} сд.</code> ({slice_coins} монет)\n"
                f"└ <b>Эталон:</b> <code>{benchmark:.1f} сд/монета за {tf_h:.1f}ч</code>\n\n"
            )

        eff_speed = float(vel_dict.get("avg_effective_speed", 0.0))
        ratio = float(vel_dict.get("speed_ratio", 1.0))
        dev_pct = float(vel_dict.get("deviation_pct", 0.0))
        dev_sign = "+" if dev_pct > 0 else ""

        return (
            f"⚡ <b>{title}:</b>\n"
            f"├ <b>Скорость:</b> <b>{eff_speed:.2f} сд/монета</b> ({status_icon} <b>{status_desc}</b>)\n"
            f"├ <b>Активность:</b> <code>x{ratio:.2f}</code> ({dev_sign}{dev_pct:.1f}% к эталону {benchmark:.1f})\n"
            f"└ <b>Окно ({tf_h:.1f}ч):</b> <code>{slice_trades} сд.</code> на {slice_coins} монет (накоплено <code>{acc_h:.1f}ч</code>)\n\n"
        )

    def _render_universe_analytics(self, user_id: int, target_uid: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
        """Рендерит детальную аналитику выбранного купола стратегии со всеми инструментами."""
        current_uid, univ_name, analytics_path, _ = self._get_universe_analytics(user_id, target_uid=target_uid)
        set_user_universe_id(user_id, current_uid)
        import html
        clean_name = html.escape(univ_name)

        start_bal = 150.0
        cur_bal = 150.0
        peak_bal = 150.0
        min_bal = 150.0
        net_profit = 0.0
        realized = 0.0
        unrealized = 0.0
        max_dd = 0.0
        recovery = 0.0
        trades_cnt = 0
        iter_idx = 1

        dome_vel = None
        if analytics_path.exists():
            try:
                adata = json.loads(analytics_path.read_text(encoding="utf-8"))
                start_bal = float(adata.get("start_balance_usdt", 150.0))
                cur_bal = float(adata.get("cur_balance_usdt", start_bal))
                peak_bal = float(adata.get("peak_balance_usdt", max(start_bal, cur_bal)))
                min_bal = float(adata.get("min_balance_usdt", min(start_bal, cur_bal)))
                net_profit = float(adata.get("net_profit_usdt", 0.0))
                realized = float(adata.get("realized_pnl_net_usdt", adata.get("realized_pnl_usdt", 0.0)))
                unrealized = float(adata.get("unrealized_pnl_usdt", 0.0))
                max_dd = float(adata.get("max_drawdown_usdt", 0.0))
                recovery = float(adata.get("recovery_factor", 0.0))
                trades_cnt = int(adata.get("total_trades", 0))
                iter_idx = int(adata.get("iteration_index", 1))
                dome_vel = adata.get("trade_velocity")
            except Exception:
                pass

        if not dome_vel and hasattr(self.bot_core, "analytics") and hasattr(self.bot_core.analytics, "get_trade_velocity"):
            dome_vel = self.bot_core.analytics.get_trade_velocity(current_uid)
        vel_block = self._format_trade_velocity_block(dome_vel, title="⚡ Trade Velocity (Купол)")

        roi_pct = round((net_profit / start_bal * 100.0), 2) if start_bal > 0 else 0.0
        net_sign = "+" if net_profit > 0 else ""
        roi_sign = "+" if roi_pct > 0 else ""
        real_sign = "+" if realized > 0 else ""
        unreal_sign = "+" if unrealized > 0 else ""

        text = (
            f"📊 <b>АНАЛИТИКА КУПОЛА СТРАТЕГИИ</b>\n"
            f"🌌 <b>{clean_name}</b> (<code>{current_uid}</code>)\n\n"
            f"💰 <b>Баланс и Пики купола:</b>\n"
            f"├ <b>Текущий баланс:</b> <code>{cur_bal:,.2f}$</code>\n"
            f"├ <b>Начальный баланс:</b> <code>{start_bal:,.2f}$</code>\n"
            f"├ <b>ATH (Пик баланса):</b> <code>{peak_bal:,.2f}$</code>\n"
            f"└ <b>Дно (Минимум):</b> <code>{min_bal:,.2f}$</code>\n\n"
            f"📈 <b>Результаты купола:</b>\n"
            f"├ <b>Net Profit:</b> <b>{net_sign}{net_profit:,.2f}$</b> ({roi_sign}{roi_pct:.2f}%)\n"
            f"├ <b>Realized:</b> {real_sign}{realized:,.2f}$ | <b>Floating:</b> {unreal_sign}{unrealized:,.2f}$\n"
            f"├ <b>Max Drawdown:</b> {max_dd:,.2f}$ | <b>Recovery:</b> {recovery:.2f}\n"
            f"└ <b>Сделок:</b> {trades_cnt} сд. (Итерация #{iter_idx})\n\n"
            f"{vel_block}"
            f"<i>Выберите раздел аналитики:</i>"
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🏆 Рейтинг монет", callback_data=f"analytics_ranking:net:{current_uid}:new"),
            ],
            [
                InlineKeyboardButton(text="🪙 Аналитика по монетам", callback_data=f"analytics_coin_menu:{current_uid}"),
            ],
            [
                InlineKeyboardButton(text="📈 График эквити", callback_data=f"analytics_chart:{current_uid}"),
            ],
            [
                InlineKeyboardButton(text="📝 Лента сделок (TXT)", callback_data=f"analytics_txt:{current_uid}"),
            ],
            [
                InlineKeyboardButton(text="📄 Выгрузить весь отчет (TXT)", callback_data=f"analytics_full_report:{current_uid}"),
            ],
            [
                InlineKeyboardButton(text="🔄 Сменить вселенную", callback_data="univ_page:1"),
                InlineKeyboardButton(text="🏆 Лидерборд стратегий", callback_data="analytics_bb_leaderboard"),
            ],
            [
                InlineKeyboardButton(text="📚 Шпаргалка (Cheat Sheet)", callback_data="analytics_cheat_sheet"),
            ]
        ])
        return text, keyboard

    def _render_analytics_big_board(self, user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        """Рендерит портфельный Биг Борд с кумулятивными метриками по всем стратегиям и куполам."""
        portfolio_summary = {}
        engine = getattr(getattr(self.bot_core, "analytics", None), "leaderboard_engine", None)
        if engine and hasattr(engine, "get_portfolio_summary"):
            try:
                active_strats = {}
                if hasattr(self.bot_core, "strategy_registry"):
                    active_strats = self.bot_core.strategy_registry.get_active_strategies() if hasattr(self.bot_core.strategy_registry, "get_active_strategies") else self.bot_core.strategy_registry.active_strategies
                universes_meta = {
                    sid: {**getattr(scfg, "raw_meta", {}), "name": scfg.name, "enabled": True}
                    for sid, scfg in active_strats.items()
                }
                entries = engine.generate_leaderboard(universes_meta)
                portfolio_summary = engine.get_portfolio_summary(entries)
            except Exception as e:
                logger.error(f"Error getting portfolio summary from engine: {e}")

        if not portfolio_summary:
            try:
                p_path = ANALYTICS_STATS_DIR / "analytics_all.json"
                if p_path.exists():
                    portfolio_summary = json.loads(p_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        current_uid, univ_name, _, _ = self._get_universe_analytics(user_id)
        import html
        clean_name = html.escape(univ_name)

        start_bal = float(portfolio_summary.get("aggregate_start_balance_usdt", 0.0))
        cur_bal = float(portfolio_summary.get("aggregate_cur_balance_usdt", start_bal))
        peak_bal = float(portfolio_summary.get("aggregate_peak_balance_usdt", max(start_bal, cur_bal)))
        min_bal = float(portfolio_summary.get("aggregate_min_balance_usdt", min(start_bal, cur_bal)))
        net_profit = float(portfolio_summary.get("aggregate_net_profit_usdt", 0.0))
        realized = float(portfolio_summary.get("aggregate_realized_pnl_usdt", 0.0))
        unrealized = float(portfolio_summary.get("aggregate_unrealized_pnl_usdt", 0.0))
        trades_cnt = int(portfolio_summary.get("aggregate_total_trades", 0))
        active_count = int(portfolio_summary.get(
            "total_universes_active",
            len(getattr(self.bot_core.strategy_registry, "active_strategies", {})) if hasattr(self.bot_core, "strategy_registry") else 1
        ))

        roi_pct = round((net_profit / start_bal * 100.0), 2) if start_bal > 0 else 0.0
        net_sign = "+" if net_profit > 0 else ""
        roi_sign = "+" if roi_pct > 0 else ""
        real_sign = "+" if realized > 0 else ""
        unreal_sign = "+" if unrealized > 0 else ""

        # Trade Velocity портфеля
        port_vel = portfolio_summary.get("trade_velocity")
        if not port_vel and hasattr(self.bot_core, "analytics") and hasattr(self.bot_core.analytics, "get_trade_velocity"):
            port_vel = self.bot_core.analytics.get_trade_velocity("default")
        vel_block = self._format_trade_velocity_block(port_vel, title="Trade Velocity (Частота сделок портфеля)")

        text = (
            f"🏛️ <b>ПОРТФЕЛЬНЫЙ БИГ БОРД (КУМУЛЯТИВНЫЙ БАЛАНС)</b>\n"
            f"🌐 <i>Агрегированная сводка по всем стратегиям ({active_count} куполов)</i>\n\n"
            f"💰 <b>Капитал портфеля:</b>\n"
            f"├ <b>Текущий баланс:</b> <code>{cur_bal:,.2f}$</code>\n"
            f"├ <b>Начальный баланс:</b> <code>{start_bal:,.2f}$</code>\n"
            f"├ <b>ATH (Пик баланса):</b> <code>{peak_bal:,.2f}$</code>\n"
            f"└ <b>Дно (Минимум):</b> <code>{min_bal:,.2f}$</code>\n\n"
            f"📊 <b>Совокупный PnL:</b>\n"
            f"├ <b>Net Profit:</b> <b>{net_sign}{net_profit:,.2f}$</b> ({roi_sign}{roi_pct:.2f}%)\n"
            f"├ <b>Realized:</b> <code>{real_sign}{realized:,.2f}$</code>\n"
            f"├ <b>Floating (Нереализ.):</b> <code>{unreal_sign}{unrealized:,.2f}$</code>\n"
            f"└ <b>Всего сделок:</b> <code>{trades_cnt}</code>\n\n"
            f"{vel_block}"
            f"🌌 <b>Текущий выбранный купол:</b>\n"
            f"▶️ <b>{clean_name}</b> (<code>{current_uid}</code>)\n\n"
            f"<i>Выберите действие или перейдите в детальную аналитику купола:</i>"
        )

        short_uid = (current_uid[:20] + "..") if len(current_uid) > 20 else current_uid
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=f"🌌 Аналитика купола: {short_uid}", callback_data=f"analytics_dome:{current_uid}"),
            ],
            [
                InlineKeyboardButton(text="🔄 Сменить купол", callback_data="univ_page:1"),
                InlineKeyboardButton(text="🏆 Лидерборд стратегий", callback_data="analytics_bb_leaderboard"),
            ]
        ])
        return text, kb

    def _register_handlers(self):
        @self.dp.errors()
        async def global_error_handler(event: ErrorEvent):
            if isinstance(event.exception, TelegramNetworkError):
                logger.warning(f"[TG] Network Error (suppressed): {event.exception}")
                return True
            if isinstance(event.exception, TelegramRetryAfter):
                logger.warning(f"[TG] Rate Limit exceeded, retry after {event.exception.retry_after}s")
                return True
            if "message is not modified" in str(event.exception).lower():
                logger.debug(f"[TG] Message not modified (suppressed): {event.exception}")
                return True
            logger.error(f"[TG] Unhandled exception: {event.exception}")
            return False

        @self.dp.message(Command("start"))
        async def start_cmd(message: Message, state: FSMContext):
            await state.clear()
            status = "⏸️ Paused" if self.bot_core.is_paused else "▶️ Running"
            
            node_status = ""
                    
            text = f"<b>Control Panel</b>\nCurrent Status: {status}{node_status}"
            await message.answer(text, reply_markup=self._get_main_keyboard(), parse_mode="HTML")

        @self.dp.message(Command("quant"))
        async def secret_quant_cmd(message: Message, state: FSMContext):
            msg = await message.answer("⏳ Симуляция ликвидаций запущена...")
            try:
                if QuantCalculator is None:
                    await msg.edit_text("ℹ️ Калькулятор ликвидаций устарел для данной конфигурации.")
                    return
                calc = QuantCalculator()
                report = calc.generate_report()
                await msg.edit_text(f"```text\n{report}\n```", parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Quant calculator error: {e}")
                await msg.edit_text(f"❌ Ошибка калькулятора: {e}")

        @self.dp.message(Command("sonnik_restore"))
        async def sonnik_restore_cmd(message: Message):
            msg = await message.answer("⏳ Запуск принудительного Deep Sync. Это займет некоторое время...")
            try:
                await self.bot_core.analytics.deep_sync_analytics(self.bot_core.client, self.bot_core.prices)
                await msg.edit_text("✅ Принудительный Deep Sync успешно завершен! Исторические данные восстановлены по первой строке леджера.")
            except Exception as e:
                logger.error(f"Failed to execute manual deep sync: {e}")
                await msg.edit_text(f"❌ Ошибка во время Deep Sync: {e}")

        @self.dp.message(Command("radar"))
        async def radar_cmd(message: Message):
            nav = getattr(self.bot_core, "radar_navigator", None)
            if not nav or not nav.is_enabled():
                await message.answer("ℹ️ Штурман / Синергия с Радаром отключена или не инициализирована.")
                return
            report = nav.generate_telemetry_report()
            await message.answer(report, parse_mode="HTML")

        @self.dp.message(Command("backup"))
        async def backup_cmd(message: Message):
            try:
                from CORE.SERVICES.runtime_backup_service import RuntimeBackupService
                status_msg = await message.answer("⏳ Создание рантайм-бэкапа позиций (.pkl ➔ JSON)...")
                res = await RuntimeBackupService.export_and_send(self.bot_core, chat_ids=[message.chat.id])
                p = res.get("file")
                if not res.get("telegram_sent") and p and os.path.exists(p):
                    await message.answer_document(FSInputFile(p), caption=f"📦 <b>Рантайм-бэкап позиций</b>\nФайл: <code>{res.get('filename')}</code>\nРазмер: {res.get('size_bytes', 0) / 1024.0:.1f} KB", parse_mode="HTML")
                await status_msg.delete()
            except Exception as e:
                logger.error(f"Error handling /backup command: {e}")
                await message.answer(f"❌ Ошибка создания бэкапа: {e}")

        @self.dp.callback_query(F.data.startswith("rot_accept:"))
        async def on_rot_accept(callback: CallbackQuery):
            await callback.answer()
            req_id = callback.data.split(":", 1)[1]
            nav = getattr(self.bot_core, "radar_navigator", None)
            if not nav or req_id not in nav.pending_rotations:
                await callback.message.answer("⚠️ Запрос на ротацию устарел (таймаут истек) или уже выполнен.")
                return
            nav.cancel_timeout_task(req_id)
            rot_data = nav.pending_rotations.pop(req_id)
            strat_id = rot_data["strat_id"]
            rotations = rot_data.get("rotations")
            if not rotations and "old_symbol" in rot_data:
                rotations = [rot_data]

            for r in rotations:
                nav.symbols_pending_rotation.discard(r["old_symbol"])

            try:
                applied_lines = []
                for r in rotations:
                    await nav.apply_rotation(
                        strat_id=strat_id,
                        old_symbol=r["old_symbol"],
                        new_symbol=r["new_symbol"],
                        old_score=r.get("old_score", 0.0),
                        new_score=r.get("new_score", 0.0),
                        lead_pct=r.get("lead_pct", 0.0),
                        matched_rule=rot_data.get("matched_rule", ""),
                        mode="advisory_operator_accepted"
                    )
                    applied_lines.append(f"• <b>{r['old_symbol']}</b> ➔ <b>{r['new_symbol']}</b> (+{r['lead_pct']:.1f}%)")

                msg_applied = "\n".join(applied_lines)
                await callback.message.edit_text(
                    f"✅ <b>Ротация выполнена!</b> (Замен: {len(rotations)})\n"
                    f"{msg_applied}\n"
                    f"Новый цикл стартует на обновленном составе!",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"[TG] Ошибка применения ротации: {e}")
                await callback.message.answer(f"❌ Ошибка применения ротации: {e}")

        @self.dp.callback_query(F.data.startswith("rot_reject:"))
        async def on_rot_reject(callback: CallbackQuery):
            await callback.answer()
            req_id = callback.data.split(":", 1)[1]
            nav = getattr(self.bot_core, "radar_navigator", None)
            if nav and req_id in nav.pending_rotations:
                nav.cancel_timeout_task(req_id)
                rot_data = nav.pending_rotations.pop(req_id)
                rotations = rot_data.get("rotations")
                if not rotations and "old_symbol" in rot_data:
                    rotations = [rot_data]
                for r in rotations:
                    nav.symbols_pending_rotation.discard(r["old_symbol"])
                await callback.message.edit_text(
                    f"❌ <b>Ротация отклонена.</b>\n"
                    f"Боевой пул стратегии [{rot_data['strat_id']}] сохранен без изменений.",
                    parse_mode="HTML"
                )
            else:
                await callback.message.answer("ℹ️ Запрос на ротацию не найден или уже закрыт.")

        @self.dp.message(F.text == "🚨 Close All")
        async def on_close_all(message: Message, state: FSMContext):
            await state.clear()
            active_strats = {}
            if hasattr(self.bot_core, "strategy_registry"):
                active_strats = self.bot_core.strategy_registry.get_active_strategies() if hasattr(self.bot_core.strategy_registry, "get_active_strategies") else self.bot_core.strategy_registry.active_strategies

            buttons = []
            for sid, scfg in active_strats.items():
                s_name = scfg.name or sid
                buttons.append([InlineKeyboardButton(
                    text=f"🚨 Закрыть купол [{s_name}]",
                    callback_data=f"close_dome_{sid}"
                )])

            buttons.append([InlineKeyboardButton(text="💥 ЗАКРЫТЬ ВСЁ (Все купола)", callback_data="close_dome_all")])
            buttons.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="close_dome_cancel")])

            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            await message.answer(
                "🚨 <b>УПРАВЛЕНИЕ ЗАКРЫТИЕМ ПОЗИЦИЙ</b>\n\n"
                "Выберите, какой купол стратегии необходимо закрыть, либо выполните закрытие всего портфеля:\n\n"
                "<i>• <b>Закрыть купол</b>: закрывает позиции и снимает ордера только выбранной стратегии, фиксирует цикл в лидерборде.\n"
                "• <b>ЗАКРЫТЬ ВСЁ</b>: глобальный экстренный сброс всех монет и всех стратегий.</i>",
                reply_markup=keyboard,
                parse_mode="HTML"
            )

        @self.dp.callback_query(F.data.startswith("close_dome_"))
        async def on_close_dome_callback(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            cdata = callback.data
            if cdata == "close_dome_cancel":
                await state.clear()
                await callback.message.edit_text("❌ Закрытие отменено.")
                return

            cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="close_dome_cancel")]
            ])

            if cdata == "close_dome_all":
                await state.set_state(TGStates.waiting_for_close_all_confirm)
                await state.update_data(target_close_dome="all")
                await callback.message.edit_text(
                    "🚨 <b>ПОДТВЕРЖДЕНИЕ ЗАКРЫТИЯ ВСЕХ КУПОЛОВ</b>\n\n"
                    "⚠️ <b>ВНИМАНИЕ!</b>\n"
                    "Будут отменены все лимитные ордера и закрыты позиции по рынку <b>ВО ВСЕХ СТРАТЕГИЯХ</b> портфеля, а циклы перезапущены!\n\n"
                    "👉 Для подтверждения отправьте текстовым сообщением слово:\n"
                    "<b>ЗАКРЫТЬ</b>\n\n"
                    "<i>Или нажмите «❌ Отмена» ниже для отмены операции.</i>",
                    reply_markup=cancel_kb,
                    parse_mode="HTML"
                )
                return

            strat_id = cdata.replace("close_dome_", "")
            strat = self.bot_core.strategy_registry.get_strategy(strat_id) if hasattr(self.bot_core, "strategy_registry") else None
            strat_name = strat.name if strat else strat_id
            symbols_str = ", ".join(strat.symbols) if (strat and strat.symbols) else "нет"

            await state.set_state(TGStates.waiting_for_close_all_confirm)
            await state.update_data(target_close_dome=strat_id)
            await callback.message.edit_text(
                f"⚠️ <b>ПОДТВЕРЖДЕНИЕ ЗАКРЫТИЯ КУПОЛА</b>\n\n"
                f"🌌 Купол: <b>{strat_name}</b> (<code>{strat_id}</code>)\n"
                f"🪙 Обойма монет: <b>{symbols_str}</b>\n\n"
                f"Будут отменены все ордера, закрыты позиции по рынку только для монет этого купола и сброшен текущий цикл.\n"
                f"<i>Остальные купола продолжат торговлю без изменений.</i>\n\n"
                f"👉 Для подтверждения отправьте текстовым сообщением слово:\n"
                f"<b>ЗАКРЫТЬ</b>\n\n"
                f"<i>Или нажмите «❌ Отмена» ниже для отмены операции.</i>",
                reply_markup=cancel_kb,
                parse_mode="HTML"
            )

        @self.dp.message(StateFilter(TGStates.waiting_for_close_all_confirm))
        async def process_close_all_confirm(message: Message, state: FSMContext):
            data = await state.get_data()
            target = data.get("target_close_dome")
            await state.clear()

            text_val = (message.text or "").strip().lower()
            if text_val != "закрыть":
                await message.answer("❌ Закрытие отменено.", reply_markup=self._get_main_keyboard())
                return

            if target == "all":
                msg = await message.answer("⏳ Закрытие всех куполов и отмена лимитных ордеров...", reply_markup=self._get_main_keyboard())
                try:
                    reset_report = await self.bot_core.close_all_positions(reset_iterations=True)
                    lines = ["✅ <b>Все купола закрыты, лимитные ордера отменены!</b>"]
                    if reset_report:
                        lines.append("\n🔄 <b>Сброс циклов куполов (Positive Auto-Closing):</b>")
                        for strat_id, info in reset_report.items():
                            meta = self._get_strategy_meta(strat_id)
                            name = meta.get("name", strat_id)
                            lines.append(
                                f"• <b>{name}</b>: Старт <b>Цикла #{info['new_iteration']}</b> (базовая точка Net: <b>{info['base_net']:+.2f} USDT</b>)"
                            )
                    lines.append("\n📊 <b>Лидерборд синхронизирован.</b>")
                    await msg.edit_text("\n".join(lines), parse_mode="HTML")
                except Exception as e:
                    logger.error(f"Failed to Close All: {e}")
                    await msg.edit_text(f"❌ Произошла ошибка при закрытии всех куполов: {e}")
            elif target:
                strat_id = target
                strat = self.bot_core.strategy_registry.get_strategy(strat_id) if hasattr(self.bot_core, "strategy_registry") else None
                strat_name = strat.name if strat else strat_id
                msg = await message.answer(f"⏳ Закрытие позиций купола <b>{strat_name}</b>...", reply_markup=self._get_main_keyboard(), parse_mode="HTML")
                try:
                    await self.bot_core.close_strategy_positions(strat_id)
                    info = await self.bot_core.reset_strategy_iteration(strat_id)
                    lines = [
                        f"✅ <b>Купол [{strat_name}] успешно закрыт!</b>",
                        f"Позиции монет закрыты по рынку, ордера отменены.",
                        f"🔄 Старт <b>Цикла #{info['new_iteration']}</b> (базовая точка Net: <b>{info['base_net']:+.2f} USDT</b>)",
                        "\n📊 Лидерборд и аналитика обновлены."
                    ]
                    await msg.edit_text("\n".join(lines), parse_mode="HTML")
                except Exception as e:
                    logger.error(f"Failed to close strategy dome {strat_id}: {e}")
                    await msg.edit_text(f"❌ Ошибка закрытия купола {strat_name}: {e}")
            else:
                await message.answer("❌ Купол для закрытия не найден.", reply_markup=self._get_main_keyboard())

        @self.dp.message(F.text == "▶️ Start")
        async def on_pre_start(message: Message, state: FSMContext):
            await state.clear()
            
            if not self.bot_core.is_paused:
                await message.answer("⚠️ Trading is already running!", reply_markup=self._get_main_keyboard())
                return
                
            symbols = self.bot_core.symbols if hasattr(self, 'bot_core') and self.bot_core.symbols else ACTIVE_SYMBOLS
            
            for sym in symbols:
                runtime_path = self.template_manager.get_runtime_path(sym)
                if runtime_path.exists():
                    try:
                        if str(runtime_path).endswith(".pkl"):
                            import pickle
                            with open(runtime_path, 'rb') as f:
                                data = pickle.load(f)
                        else:
                            with open(runtime_path, 'r', encoding='utf-8') as f:
                                data = json.load(f)
                            
                        lines = [f"<b>{sym} Settings:</b>"]
                        for side in ["LONG", "SHORT"]:
                            cfg = data[side]
                            en = cfg["enable"]
                            sz = cfg["invest_size"]
                            lev = cfg["leverage"]
                            margin = cfg["margin_type"]
                            
                            lines.append(f"  <b>{side}</b>: {'✅ On' if en else '❌ Off'}")
                            if en:
                                lines.append(f"    ├ Invest: {sz}$ | Lev: {lev}x | {margin}")
                                
                                grid = cfg["grid"]
                                indents = []
                                super_indents = []
                                for k in sorted(grid.keys(), key=lambda x: int(x) if x.isdigit() else x):
                                    lvl_d = grid[k]
                                    if isinstance(lvl_d, dict) and (lvl_d.get("enabled", True) is False or lvl_d.get("indent") is None):
                                        indents.append("off")
                                    else:
                                        indents.append(str(lvl_d.get("indent") if isinstance(lvl_d, dict) else lvl_d))
                                    si = lvl_d.get("super_indent") if isinstance(lvl_d, dict) else None
                                    super_indents.append(str(si) if si is not None else "-")
                                lines.append(f"    ├ Grid: [{', '.join(indents)}]")
                                
                                is_sg = any(scfg.super_grid_enabled for scfg in self.bot_core.strategy_registry.active_strategies.values()) if hasattr(self.bot_core, "strategy_registry") else bool(_CFG.get("super_grid", {}).get("enabled", False))
                                if is_sg and any(si != "-" for si in super_indents):
                                    lines.append(f"    ├ Super: [{', '.join(super_indents)}]")
                                    
                                
                                tp_map = cfg["tp_map"]
                                tps = []
                                for k in sorted(tp_map.keys(), key=lambda x: int(x) if x.isdigit() else x):
                                    tp_d = tp_map[k]
                                    raw_tp_ind = tp_d.get("indent") if isinstance(tp_d, dict) else tp_d
                                    tps.append(str(raw_tp_ind) if raw_tp_ind is not None else "-")
                                lines.append(f"    └ TP: [{', '.join(tps)}]")
                                
                        msg_text = "\n".join(lines)
                        await message.answer(msg_text, parse_mode="HTML")
                    except Exception as e:
                        await message.answer(f"<b>{sym}</b>: [Ошибка чтения конфигурации: {e}]", parse_mode="HTML")
                else:
                    await message.answer(f"<b>{sym}</b>: [Рантайм не создан]", parse_mode="HTML")
            
            await message.answer("<b>Подтвердите настройки запуска:</b>", reply_markup=self._get_confirm_start_keyboard(), parse_mode="HTML")

        @self.dp.message(F.text == "🔙 Back")
        async def on_cancel(message: Message, state: FSMContext):
            await state.clear()
            status = "⏸️ Paused" if self.bot_core.is_paused else "▶️ Running"
            
            node_status = ""
                    
            text = f"<b>Control Panel</b>\nCurrent Status: {status}{node_status}"
            await message.answer(text, reply_markup=self._get_main_keyboard(), parse_mode="HTML")

        @self.dp.message(F.text == "✅ Confirm Start")
        async def on_confirm_start(message: Message, state: FSMContext):
            await state.clear()
            
            # Save auto_start = True to app.json
            app_json_path = DATA_DIR / "app.json"
            app_data = Utils.read_json_file(app_json_path)
            if "app" not in app_data:
                app_data["app"] = {}
            app_data["app"]["auto_start"] = True
            Utils.write_json_file(app_json_path, app_data)
            
            if not self.bot_core.is_paused:
                await message.answer("Trading is already running!", reply_markup=self._get_main_keyboard())
                return
            self.bot_core.is_paused = False
            logger.info("[TG] User started trading loops. auto_start flag saved to True.")
            await message.answer("<b>Control Panel</b>\nCurrent Status: ▶️ Running", reply_markup=self._get_main_keyboard(), parse_mode="HTML")

        @self.dp.message(F.text == "⏸️ Stop")
        async def on_stop_trade(message: Message, state: FSMContext):
            await state.clear()
            
            # Save auto_start = False to app.json
            app_json_path = DATA_DIR / "app.json"
            app_data = Utils.read_json_file(app_json_path)
            if "app" not in app_data:
                app_data["app"] = {}
            app_data["app"]["auto_start"] = False
            Utils.write_json_file(app_json_path, app_data)
            
            if self.bot_core.is_paused:
                await message.answer("Trading is already paused!", reply_markup=self._get_main_keyboard())
                return
            self.bot_core.is_paused = True
            logger.info("[TG] User stopped trading loops. auto_start flag saved to False.")
            await message.answer("<b>Control Panel</b>\nCurrent Status: ⏸️ Paused", reply_markup=self._get_main_keyboard(), parse_mode="HTML")

        @self.dp.message(F.text == "ℹ️ Status")
        async def on_status(message: Message, state: FSMContext):
            await state.clear()
            status = "⏸️ Paused" if self.bot_core.is_paused else "▶️ Running"
            
            # Active strategies and coins info
            strat_info = []
            if hasattr(self.bot_core, "strategy_registry"):
                strats = self.bot_core.strategy_registry.get_active_strategies() if hasattr(self.bot_core.strategy_registry, "get_active_strategies") else self.bot_core.strategy_registry.active_strategies
                for sid, scfg in strats.items():
                    mode_str = getattr(scfg, "mode", getattr(scfg, "auto_closing_mode", "off"))
                    if mode_str == "fixed":
                        pos_ac = getattr(scfg, "auto_closing_cfg", {}).get("positive", {})
                        f_target = pos_ac.get("fixed_net", {}).get("target_net_usdt")
                        badge = f"[FIXED ${float(f_target):g}]" if f_target else "[FIXED]"
                    elif mode_str == "classic":
                        badge = "[CLASSIC TRAILING]"
                    elif mode_str == "toxic":
                        badge = "[TOXIC ADAPTIVE]"
                    elif mode_str == "off":
                        badge = "[OFF]"
                    else:
                        badge = f"[{mode_str.upper()}]"
                    strat_info.append(f"• <b>{scfg.name}</b> <code>{badge}</code>: <code>{', '.join(scfg.symbols)}</code>")
            strat_block = "\n".join(strat_info) if strat_info else "<i>Нет активных стратегий</i>"

            # Trade Velocity Engine
            vel_text = ""
            if hasattr(self.bot_core, "analytics") and hasattr(self.bot_core.analytics, "velocity_engine"):
                try:
                    data = self.bot_core.analytics._read_data()
                    vel_data = self.bot_core.analytics.velocity_engine.calculate_velocity(data)
                    eff_speed = float(vel_data.get("avg_effective_speed") or 0.0)
                    speed_ratio = float(vel_data.get("speed_ratio") or 1.0)
                    status_desc = str(vel_data.get("status_desc") or "NORMAL")
                    vel_text = f"\n\n⚡ <b>Trade Velocity</b>: <b>{eff_speed:.2f} сд/ч</b> (x{speed_ratio:.2f} | {status_desc})"
                    
                    per_coin = vel_data.get("per_coin", {})
                    if per_coin:
                        vel_text += "\n<i>Скорость по монетам:</i>"
                        for sym, c_info in per_coin.items():
                            c_speed = float(c_info.get("effective_speed") or 0.0)
                            c_ratio = float(c_info.get("speed_ratio") or 1.0)
                            vel_text += f"\n  - <b>{sym}</b>: {c_speed:.2f} сд/ч (x{c_ratio:.2f})"
                except Exception as e:
                    logger.error(f"Error calculating velocity for status: {e}")

            # Risk System info
            app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
            risk_cfg = app_data.get("risk_system") or app_data.get("stop_bot_rirk_system") or {}
            risk_en = risk_cfg.get("enabled", False)
            min_th = risk_cfg.get("min_balance_threshold_usdt")
            max_th = risk_cfg.get("max_balance_threshold_usdt")
            th_parts = []
            if min_th is not None:
                th_parts.append(f"стоп: {float(min_th):.2f}$")
            if max_th is not None:
                th_parts.append(f"тейк: {float(max_th):.2f}$")
            if risk_en:
                risk_str = f"🟢 Включен ({', '.join(th_parts)})" if th_parts else "⚠️ Включен, но пороги не заданы"
            else:
                risk_str = "🔴 Выключен"

            text = (
                f"<b>Control Panel</b>\n"
                f"Статус бота: <b>{status}</b>\n"
                f"Ручник Risk System: <b>{risk_str}</b>\n\n"
                f"🌌 <b>Купола стратегий:</b>\n{strat_block}"
                f"{vel_text}"
            )
            await message.answer(text, reply_markup=self._get_main_keyboard(), parse_mode="HTML")

        @self.dp.message(F.text == "📜 Logs")
        async def on_logs_menu(message: Message, state: FSMContext):
            await state.clear()
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📜 Get Logs", callback_data="logs_get_logs")],
                [InlineKeyboardButton(text="📦 Get Runtime Backup", callback_data="logs_get_runtime_backup")]
            ])
            await message.answer("Выберите нужный лог или бэкап рантайма:", reply_markup=keyboard)

        @self.dp.callback_query(F.data == "logs_get_logs")
        async def on_get_logs(callback: CallbackQuery, state: FSMContext):
            await state.clear()
            await callback.answer()
            log_path = os.path.join("logs", "all.log")
            if os.path.exists(log_path):
                await callback.message.answer_document(FSInputFile(log_path))
            else:
                await callback.message.answer("Global log file not found.")

        @self.dp.callback_query(F.data == "logs_get_runtime_backup")
        async def on_get_runtime_backup(callback: CallbackQuery, state: FSMContext):
            await state.clear()
            await callback.answer("⏳ Создание бэкапа рантайма...")
            try:
                from CORE.SERVICES.runtime_backup_service import RuntimeBackupService
                res = await RuntimeBackupService.export_and_send(self.bot_core, chat_ids=[callback.message.chat.id])
                p = res.get("file")
                if not res.get("telegram_sent") and p and os.path.exists(p):
                    await callback.message.answer_document(FSInputFile(p), caption=f"📦 <b>Рантайм-бэкап позиций</b>\nФайл: <code>{res.get('filename')}</code>\nРазмер: {res.get('size_bytes', 0) / 1024.0:.1f} KB", parse_mode="HTML")
            except Exception as e:
                logger.error(f"Error sending runtime backup: {e}")
                await callback.message.answer(f"❌ Ошибка бэкапа: {e}")

        @self.dp.callback_query(F.data.startswith("analytics_reset"))
        async def on_reset_analytics(callback: CallbackQuery, state: FSMContext):
            await state.clear()
            await callback.answer()
            parts = callback.data.split(":", 1)
            target_uid = parts[1] if len(parts) > 1 and parts[1] else None
            current_uid, univ_name, _, _ = self._get_universe_analytics(callback.from_user.id, target_uid=target_uid)
            set_user_universe_id(callback.from_user.id, current_uid)
            await state.update_data(target_dome_uid=current_uid)
            import html
            clean_name = html.escape(univ_name)
            await callback.message.answer(
                f"⚠️ Вы уверены, что хотите полностью удалить историю аналитики для купола <b>{clean_name}</b> (<code>{current_uid}</code>)?\n\n"
                f"Введите слово <b>СБРОС</b> для подтверждения или нажмите Back для отмены.",
                reply_markup=self._get_back_keyboard(),
                parse_mode="HTML"
            )
            await state.set_state(TGStates.waiting_for_reset_confirm)

        @self.dp.message(TGStates.waiting_for_reset_confirm)
        async def process_reset_analytics_confirm(message: Message, state: FSMContext):
            if message.text == "🔙 Back":
                await state.clear()
                await message.answer("Сброс аналитики отменен.", reply_markup=self._get_main_keyboard())
                return
                
            if message.text.strip().upper() == "СБРОС":
                st_data = await state.get_data()
                target_uid = st_data.get("target_dome_uid") or get_user_universe_id(message.from_user.id, self.bot_core)
                current_uid, univ_name, analytics_path, csv_path = self._get_universe_analytics(message.from_user.id, target_uid=target_uid)
                await state.clear()
                deleted_any = False
                try:
                    if analytics_path.exists():
                        os.remove(analytics_path)
                        deleted_any = True
                    if csv_path.exists():
                        os.remove(csv_path)
                        deleted_any = True
                    if deleted_any:
                        await message.answer(
                            f"✅ Файлы аналитики для купола <code>{current_uid}</code> успешно удалены. Они будут созданы заново при следующем обновлении.",
                            reply_markup=self._get_main_keyboard(),
                            parse_mode="HTML"
                        )
                    else:
                        await message.answer(
                            f"⚠️ Файлы аналитики для купола <code>{current_uid}</code> не найдены.",
                            reply_markup=self._get_main_keyboard(),
                            parse_mode="HTML"
                        )
                except Exception as e:
                    await message.answer(f"❌ Ошибка при удалении файлов аналитики купола: {e}", reply_markup=self._get_main_keyboard())
            else:
                await message.answer("❌ Неверное слово подтверждения. Введите <b>СБРОС</b> или нажмите Back.", parse_mode="HTML")

        @self.dp.callback_query(F.data.startswith("analytics_set_balance"))
        async def on_set_initial_balance(callback: CallbackQuery, state: FSMContext):
            await state.clear()
            await callback.answer()
            parts = callback.data.split(":", 1)
            target_uid = parts[1] if len(parts) > 1 and parts[1] else None
            current_uid, univ_name, _, _ = self._get_universe_analytics(callback.from_user.id, target_uid=target_uid)
            set_user_universe_id(callback.from_user.id, current_uid)
            await state.update_data(target_dome_uid=current_uid)
            import html
            clean_name = html.escape(univ_name)
            await callback.message.answer(
                f"Введите новый начальный баланс (start_balance_usdt) для купола <b>{clean_name}</b> (<code>{current_uid}</code>) в USDT (например, 57.0):",
                reply_markup=self._get_back_keyboard(),
                parse_mode="HTML"
            )
            await state.set_state(TGStates.waiting_for_initial_balance)

        @self.dp.message(TGStates.waiting_for_initial_balance)
        async def process_initial_balance(message: Message, state: FSMContext):
            if message.text == "🔙 Back":
                await state.clear()
                await message.answer("Действие отменено.", reply_markup=self._get_main_keyboard())
                return
                
            try:
                new_balance = float(message.text.replace(',', '.'))
                if new_balance < 0:
                    raise ValueError("Баланс не может быть отрицательным.")
            except ValueError:
                await message.answer("❌ Некорректное число. Введите баланс еще раз (например, 57.0) или нажмите Back:")
                return
                
            st_data = await state.get_data()
            target_uid = st_data.get("target_dome_uid") or get_user_universe_id(message.from_user.id, self.bot_core)
            current_uid, univ_name, analytics_path, _ = self._get_universe_analytics(message.from_user.id, target_uid=target_uid)
            await state.clear()
            
            try:
                app_path = DATA_DIR / "app.json"
                if app_path.exists():
                    app_data = Utils.read_json_file(app_path)
                    if "active_strategies" in app_data and current_uid in app_data["active_strategies"]:
                        app_data["active_strategies"][current_uid].setdefault("analytics", {})["start_balance_usdt"] = new_balance
                        Utils.write_json_file(app_path, app_data)

                if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
                    scfg = self.bot_core.strategy_registry.active_strategies.get(current_uid)
                    if scfg:
                        scfg.start_balance_usdt = new_balance
                        scfg.analytics_cfg["start_balance_usdt"] = new_balance

                if analytics_path.exists():
                    sdata = json.loads(analytics_path.read_text(encoding="utf-8"))
                    old_b = float(sdata.get("start_balance_usdt", new_balance))
                    delta = round(new_balance - old_b, 4)
                    sdata["start_balance_usdt"] = round(new_balance, 4)
                    sdata["_rebased_start_balance_usdt"] = round(new_balance, 4)
                    sdata["cur_balance_usdt"] = round(float(sdata.get("cur_balance_usdt", old_b)) + delta, 4)
                    for k in ("peak_balance_usdt", "min_balance_usdt", "_current_trough_usdt"):
                        if k in sdata and sdata[k] is not None:
                            sdata[k] = round(float(sdata[k]) + delta, 4)
                    Utils.atomic_write_json(analytics_path, sdata, indent=4)

                import html
                clean_name = html.escape(univ_name)
                await message.answer(
                    f"✅ Начальный баланс для купола <b>{clean_name}</b> (<code>{current_uid}</code>) успешно установлен на <b>{new_balance:,.2f} USDT</b>.",
                    reply_markup=self._get_main_keyboard(),
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Error updating dome balance: {e}")
                await message.answer(f"❌ Ошибка обновления баланса купола: {e}", reply_markup=self._get_main_keyboard())


        async def _show_universe_analytics_screen(target_msg, user_id: int, edit: bool = False, target_uid: Optional[str] = None):
            try:
                await self.bot_core.analytics.sync_lightweight_unrealized_pnl(self.bot_core.client)
            except Exception as e:
                logger.error(f"Failed to sync lightweight unrealized pnl: {e}")

            text, keyboard = self._render_universe_analytics(user_id, target_uid=target_uid)

            if edit and isinstance(target_msg, CallbackQuery):
                try:
                    await target_msg.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
                except Exception as e:
                    if "message is not modified" not in str(e).lower():
                        await target_msg.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
            elif edit and isinstance(target_msg, Message):
                try:
                    await target_msg.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
                except Exception as e:
                    if "message is not modified" not in str(e).lower():
                        await target_msg.answer(text, reply_markup=keyboard, parse_mode="HTML")
            elif isinstance(target_msg, CallbackQuery):
                await target_msg.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
            else:
                await target_msg.answer(text, reply_markup=keyboard, parse_mode="HTML")

        @self.dp.message(F.text == "📊 Analytics")
        async def on_analytics(message: Message, state: FSMContext):
            await state.clear()
            await _show_universe_analytics_screen(message, message.from_user.id, edit=False)

        @self.dp.callback_query(F.data == "analytics_bigboard")
        async def cb_analytics_bigboard(callback: CallbackQuery):
            await callback.answer()
            await _show_universe_analytics_screen(callback, callback.from_user.id, edit=True)

        @self.dp.callback_query(F.data == "analytics_bb_refresh")
        async def cb_analytics_bb_refresh(callback: CallbackQuery):
            await callback.answer("🔄 Аналитика обновлена")
            await _show_universe_analytics_screen(callback, callback.from_user.id, edit=True)

        async def _send_leaderboard(target, is_callback: bool = False):
            if is_callback:
                await target.answer()
                msg_target = target.message
            else:
                msg_target = target

            engine = getattr(self.bot_core.analytics, "leaderboard_engine", None)
            if not engine:
                await msg_target.answer("⚠️ LeaderboardEngine не инициализирован.")
                return

            data = self.bot_core.analytics._read_data()
            self.bot_core.analytics._update_strategies_stats(data)

            active_strats = self.bot_core.strategy_registry.get_active_strategies() if hasattr(self.bot_core.strategy_registry, "get_active_strategies") else self.bot_core.strategy_registry.active_strategies
            universes_meta = {
                sid: {**getattr(scfg, "raw_meta", {}), "name": scfg.name, "enabled": True}
                for sid, scfg in active_strats.items()
            }
            entries = engine.generate_leaderboard(universes_meta)
            evaluated_coins = len(self.bot_core.strategy_registry.get_all_active_symbols())
            title_prefix = "🏆 <b>ЛИДЕРБОРД СТРАТЕГИЙ</b>"

            # Сохраняем текстовый лидерборд
            try:
                engine.save_portfolio(entries, evaluated_coins_count=evaluated_coins)
            except Exception:
                pass

            clean_entries = [
                e for e in entries
                if not (
                    e.get("sizing_mode") in ("dyn_bb", "dyn_bb_legacy", "dyn_bb_breakout", "dyn_brk_16", "dyn_brk_10")
                    or any(e.get("universe_id", "").endswith(f"__{m}") for m in ("dyn_bb", "dyn_bb_legacy", "dyn_bb_breakout", "dyn_brk_16", "dyn_brk_10"))
                )
            ][:32]

            if not clean_entries:
                await msg_target.answer(f"{title_prefix}\n\n<i>Таблица лидеров пуста.</i>", parse_mode="HTML")
                return

            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            card_texts = []
            for rank, item in enumerate(clean_entries, 1):
                medal = medals.get(rank, f"▫️ #{rank}")
                uid = item.get("universe_id", "")
                mode_b = item.get("mode_badge") or f"[{str(item.get('mode', 'OFF')).upper()}]"
                size_b = item.get("sizing_badge", "[STATIC]")

                net_val = float(item.get("net_profit_usdt", 0.0))
                net_sign = "+" if net_val > 0 else ""
                iter_idx = item.get("iteration_index", 1)
                iter_net = float(item.get("iteration_net_profit_usdt", net_val))
                iter_net_sign = "+" if iter_net > 0 else ""

                real_val = float(item.get("realized_pnl_usdt", 0.0))
                real_sign = "+" if real_val > 0 else ""
                iter_real = float(item.get("iteration_realized_pnl_usdt", real_val))
                iter_real_sign = "+" if iter_real > 0 else ""

                unreal_val = float(item.get("unrealized_pnl_usdt", 0.0))
                unreal_sign = "+" if unreal_val > 0 else ""

                max_dd = float(item.get("max_drawdown_usdt", 0.0))
                peak_net = float(item.get("peak_net_profit_usdt", 0.0))
                peak_age = float(item.get("peak_age_hours", 0.0))
                peak_str = f"+{peak_net:.2f}$ ({peak_age:.1f}ч)" if peak_net > 0 and peak_age > 0 else f"{peak_net:+.2f}$"

                trades = int(item.get("total_trades", 0))
                rec = float(item.get("recovery_factor", 0.0))
                roi_val = float(item.get("roi_pct", 0.0))
                roi_sign = "+" if roi_val > 0 else ""

                card = (
                    f"{medal} <b>{uid}</b>\n"
                    f"├ Режим: <code>{mode_b}</code> <code>{size_b}</code>\n"
                    f"├ Net Profit: <b>{net_sign}{net_val:.2f} USDT</b> ({roi_sign}{roi_val:.2f}% ROI) <i>(Итер #{iter_idx}: {iter_net_sign}{iter_net:.2f}$)</i>\n"
                    f"├ Realized: <b>{real_sign}{real_val:.2f}$</b> <i>(Итер: {iter_real_sign}{iter_real:.2f}$)</i>\n"
                    f"├ Floating: <b>{unreal_sign}{unreal_val:.2f}$</b>\n"
                    f"├ Max DD: <code>{max_dd:.2f}$</code> | ATH Net: <code>{peak_str}</code>\n"
                    f"└ Сделок: <b>{trades}</b> | Rec. Factor: <code>{rec:.2f}</code>"
                )
                card_texts.append(card)

            chunk_size = 8
            num_chunks = (len(card_texts) + chunk_size - 1) // chunk_size
            for ch_idx in range(num_chunks):
                ch_cards = card_texts[ch_idx * chunk_size : (ch_idx + 1) * chunk_size]
                part_title = f" (Часть {ch_idx + 1}/{num_chunks})" if num_chunks > 1 else ""
                header_msg = (
                    f"{title_prefix}{part_title}\n"
                    f"<i>Активных куполов: {len(clean_entries)} | Монет: {evaluated_coins}</i>\n"
                    f"───────────────────────\n"
                )
                body_msg = "\n───────────────────────\n".join(ch_cards)
                full_msg = header_msg + body_msg

                kb = None
                if ch_idx == num_chunks - 1:
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="📄 Скачать leaderboard.txt", callback_data="download_leaderboard_txt")],
                        [InlineKeyboardButton(text="🔙 Назад к аналитике", callback_data="analytics_dome")]
                    ])

                await msg_target.answer(full_msg, parse_mode="HTML", reply_markup=kb)

        @self.dp.callback_query(F.data == "analytics_bb_leaderboard")
        async def cb_analytics_bb_leaderboard(callback: CallbackQuery):
            try:
                await _send_leaderboard(callback, is_callback=True)
            except Exception as e:
                logger.error(f"Error in cb_analytics_bb_leaderboard: {e}")
                await callback.message.answer(f"❌ Ошибка лидерборда: {e}")

        @self.dp.callback_query(F.data == "download_leaderboard_txt")
        async def cb_download_leaderboard_txt(callback: CallbackQuery):
            await callback.answer()
            engine = getattr(self.bot_core.analytics, "leaderboard_engine", None)
            lb_file = getattr(engine, "leaderboard_file", None) if engine else None
            if not lb_file or not os.path.exists(lb_file):
                lb_file = BASE_DIR / "ANALYTICS" / "leaderboard.txt"
            if os.path.exists(lb_file):
                await callback.message.answer_document(
                    FSInputFile(str(lb_file)),
                    caption="🏆 <b>Полный отчет: Leaderboard Стратегий</b>",
                    parse_mode="HTML"
                )
            else:
                await callback.message.answer("⚠️ Файл leaderboard.txt еще не сформирован.")

        @self.dp.callback_query(F.data == "analytics_bb_global")
        async def cb_analytics_bb_global(callback: CallbackQuery):
            await callback.answer()
            agg_summary, _, last_trade_time = self._get_portfolio_all_analytics_data()
            active_cnt = agg_summary.get("universes_count", 0)
            time_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

            header = (
                f"🌐 <b>ПОРТФЕЛЬ: ВСЕ СТРАТЕГИИ ({active_cnt} куполов)</b>\n"
                f"<b>ℹ️ Расчет по состоянию на: {time_str}</b>\n"
                f"<b>⏳ Последняя сделка: {last_trade_time}</b>\n\n"
            )

            msg = "<b>🌍 GLOBAL ANALYTICS (ПОРТФЕЛЬ)</b>\n\n"
            msg += "<b>💰 Balances:</b>\n"
            msg += f"▪️ Start: <b>{agg_summary['start_balance_usdt']:,.2f}</b> USDT\n"
            msg += f"▪️ Current: <b>{agg_summary['cur_balance_usdt']:,.2f}</b> USDT\n"
            msg += f"▪️ Peak: <b>{agg_summary['peak_balance_usdt']:,.2f}</b> USDT\n"
            msg += f"▪️ Min (Bottom): <b>{agg_summary['min_balance_usdt']:,.2f}</b> USDT\n\n"

            msg += "<b>📈 Performance:</b>\n"
            msg += f"▪️ Net Profit: <b>{agg_summary['net_profit_usdt']:,.2f}</b> USDT\n"
            msg += f"▪️ Realized PnL (Gross): <b>{agg_summary['realized_pnl_usdt']:,.2f}</b> USDT\n"
            msg += f"▪️ Realized PnL (Net): <b>{agg_summary['realized_pnl_net_usdt']:,.2f}</b> USDT\n"
            msg += f"▪️ Total Commission: <b>{agg_summary['total_commission_usdt']:,.2f}</b> USDT\n"
            msg += f"▪️ Total Funding: <b>{agg_summary['total_funding_usdt']:,.2f}</b> USDT\n"
            msg += f"▪️ Unrealized PnL: <b>{agg_summary['unrealized_pnl_usdt']:,.2f}</b> USDT\n"
            msg += f"▪️ Max Drawdown: <b>{agg_summary['max_drawdown_usdt']:,.2f}</b> USDT\n"
            msg += f"▪️ Performance (Peak-Start): <b>{agg_summary['performance_usdt']:,.2f}</b> USDT\n"
            msg += f"▪️ ROI: <b>{agg_summary['roi_pct']:.2f}%</b>\n"
            msg += f"▪️ Grid Load: <b>{agg_summary['grid_load_pct']:.2f}%</b> ({agg_summary['current_grid_load_usdt']:,.2f}/{agg_summary['total_grid_size_usdt']:,.2f} USDT)\n"
            msg += f"▪️ Margin Load: <b>{agg_summary['margin_load_pct']:.2f}%</b> (Used: <b>{agg_summary['total_margin_used_usdt']:,.2f}</b> USDT)\n"
            msg += f"▪️ Free Margin: <b>{agg_summary['free_margin_usdt']:,.2f}</b> USDT ({agg_summary['free_margin_pct']:.2f}%)\n"
            msg += f"▪️ Recovery Factor: <b>{agg_summary['recovery_factor']:.2f}</b>\n"
            msg += f"▪️ Trades: <b>{agg_summary['total_trades']}</b> (Wins: <b>{agg_summary['winning_trades']}</b> | <b>{agg_summary['winrate_pct']:.1f}%</b>)\n\n"

            port_vel = agg_summary.get("trade_velocity")
            if not port_vel and hasattr(self.bot_core, "analytics") and hasattr(self.bot_core.analytics, "get_trade_velocity"):
                port_vel = self.bot_core.analytics.get_trade_velocity("default")
            vel_block = self._format_trade_velocity_block(port_vel, title="⚡ Trade Velocity (Портфель)")
            msg += f"{vel_block}\n"

            await callback.message.answer(header + msg, parse_mode="HTML")

        @self.dp.callback_query(F.data.startswith("analytics_bb_ranking"))
        async def cb_analytics_bb_ranking(callback: CallbackQuery):
            await callback.answer()
            parts = callback.data.split(":")
            criterion = parts[1] if len(parts) > 1 else "net"

            _, agg_per_coin, _ = self._get_portfolio_all_analytics_data()
            if not agg_per_coin:
                await callback.message.answer("❌ Нет данных о монетах для портфельного рейтинга.")
                return

            def safe_flt(val, default=0.0):
                try:
                    return float(val) if val is not None else default
                except (ValueError, TypeError):
                    return default

            if criterion == "rec":
                sorted_coins = sorted(agg_per_coin.items(), key=lambda x: safe_flt(x[1].get("recovery_factor", -float('inf'))), reverse=True)
                title = "по Recovery Factor (Тек) (больше = лучше)"
            elif criterion == "abs_rec":
                sorted_coins = sorted(agg_per_coin.items(), key=lambda x: safe_flt(x[1].get("abs_recovery_factor", -float('inf'))), reverse=True)
                title = "по Abs Recovery (Пик/DD) (больше = лучше)"
            else:
                criterion = "net"
                sorted_coins = sorted(agg_per_coin.items(), key=lambda x: safe_flt(x[1].get("net_profit_usdt", -float('inf'))), reverse=True)
                title = "по Net Profit (больше = лучше)"

            lines = [f"<b>🏆 Рейтинг монет [Портфель (Все купола)] ({title})</b>\n"]
            for i, (sym, cdata) in enumerate(sorted_coins, 1):
                if criterion == "rec":
                    rec = safe_flt(cdata.get("recovery_factor", 0.0))
                    net = safe_flt(cdata.get("net_profit_usdt", 0.0))
                    dd = abs(safe_flt(cdata.get("max_drawdown_net_usdt", cdata.get("max_drawdown", 0.0))))
                    val_str = f"Rec: <b>{rec:.2f}</b> <i>(Net: {net:+.2f} / |DD|: {dd:.2f})</i>"
                elif criterion == "abs_rec":
                    abs_rec = safe_flt(cdata.get("abs_recovery_factor", 0.0))
                    peak = safe_flt(cdata.get("peak_net_profit_usdt", max(0.0, safe_flt(cdata.get("net_profit_usdt", 0.0)))))
                    dd = abs(safe_flt(cdata.get("max_drawdown_net_usdt", cdata.get("max_drawdown", 0.0))))
                    val_str = f"Abs Rec: <b>{abs_rec:.2f}</b> <i>(Пик: {peak:+.2f} / |DD|: {dd:.2f})</i>"
                else:
                    net_profit = round(safe_flt(cdata.get("net_profit_usdt", 0)), 2)
                    realized_net = round(safe_flt(cdata.get("realized_pnl_net_usdt", 0)), 2)
                    tr = cdata.get('total_trades', 0)
                    st_bal = safe_flt(agg_summary.get("start_balance_usdt", 151.0))
                    c_roi = round(net_profit / st_bal * 100.0, 2) if st_bal > 0 else 0.0
                    c_roi_sign = "+" if c_roi > 0 else ""
                    val_str = f"{net_profit:+.2f} USDT ({c_roi_sign}{c_roi:.2f}% ROI) <i>({tr} сд., Realized: {realized_net:+.2f})</i>"

                is_act = cdata.get("is_active_symbol", cdata.get("is_active", True))
                active_badge = "" if is_act else " ⏸<i>[неактивна]</i>"
                if i == 1:
                    medal = "🥇"
                elif i == 2:
                    medal = "🥈"
                elif i == 3:
                    medal = "🥉"
                else:
                    medal = "▪️"
                lines.append(f"{medal} <b>{sym}</b>{active_badge}: {val_str}")

            switch_kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="Net Profit", callback_data="analytics_bb_ranking:net"),
                    InlineKeyboardButton(text="Recovery (Тек)", callback_data="analytics_bb_ranking:rec"),
                    InlineKeyboardButton(text="Abs Recovery", callback_data="analytics_bb_ranking:abs_rec")
                ]
            ])

            messages = []
            cur_buf = ""
            for line in lines:
                if len(cur_buf) + len(line) + 1 > 3800:
                    messages.append(cur_buf)
                    cur_buf = line + "\n"
                else:
                    cur_buf += line + "\n"
            if cur_buf:
                messages.append(cur_buf)

            if len(parts) > 1 and len(messages) == 1:
                try:
                    await callback.message.edit_text(messages[0], parse_mode="HTML", reply_markup=switch_kb)
                except Exception:
                    pass
            else:
                for i, msg_text in enumerate(messages):
                    rm = switch_kb if i == len(messages) - 1 else None
                    await callback.message.answer(msg_text, parse_mode="HTML", reply_markup=rm)

        @self.dp.callback_query(F.data == "analytics_bb_coins")
        async def cb_analytics_bb_coins(callback: CallbackQuery):
            await callback.answer()
            _, agg_per_coin, _ = self._get_portfolio_all_analytics_data()
            if not agg_per_coin:
                await callback.message.answer("❌ Нет данных о монетах в портфеле.")
                return

            keyboard_buttons = []
            for coin in sorted(agg_per_coin.keys()):
                net = agg_per_coin[coin].get("net_profit_usdt", 0.0)
                sign = "+" if net > 0 else ""
                icon = "🟢" if net >= 0 else "🔴"
                is_act = agg_per_coin[coin].get("is_active", True)
                status_tag = "" if is_act else " [неактивна]"
                keyboard_buttons.append([InlineKeyboardButton(
                    text=f"🪙 {coin}{status_tag} {icon} ({sign}{net:,.2f}$)",
                    callback_data=f"analytics_bb_coin_{coin}"
                )])

            kb = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
            await callback.message.answer("🪙 <b>АНАЛИТИКА ПО МОНЕТАМ (ПОРТФЕЛЬ)</b>\n\nВыберите монету для просмотра сводных показателей по всем куполам:", reply_markup=kb, parse_mode="HTML")

        @self.dp.callback_query(F.data.startswith("analytics_bb_coin_"))
        async def cb_analytics_bb_coin_detail(callback: CallbackQuery):
            await callback.answer()
            symbol = callback.data.replace("analytics_bb_coin_", "")
            _, agg_per_coin, _ = self._get_portfolio_all_analytics_data()
            if symbol not in agg_per_coin:
                await callback.message.answer(f"❌ Нет данных для {symbol}.")
                return

            cdata = agg_per_coin[symbol]
            u_count = cdata.get("universes_active", 0)
            msg_coins = f"<b>🪙 PORTFOLIO METRICS: {symbol}</b> (Все купола: {u_count})\n\n"
            msg_coins += f"  • Trades: <b>{cdata.get('total_trades', 0)}</b> (Wins: <b>{cdata.get('winning_trades', 0)}</b>)\n"
            msg_coins += f"  • Net Profit: <b>{cdata.get('net_profit_usdt', 0.0):,.2f}</b> USDT\n"
            msg_coins += f"  • Realized (Net): <b>{cdata.get('realized_pnl_net_usdt', 0.0):,.2f}</b> USDT\n"
            msg_coins += f"  • Fees: Comm <b>{cdata.get('commission_usdt', 0.0):,.2f}</b> / Fund <b>{cdata.get('funding_usdt', 0.0):,.2f}</b>\n"
            msg_coins += f"  • Max Drawdown: <b>{cdata.get('max_drawdown', 0.0):,.2f}</b> USDT\n"
            msg_coins += f"  • Rec: <b>{cdata.get('recovery_factor', 0)}</b> | Abs Rec: <b>{cdata.get('abs_recovery_factor', 0)}</b>\n"
            msg_coins += f"  • Risk/Reward Ratio: <b>{cdata.get('risk_reward_ratio', 0)}</b>\n"
            await callback.message.answer(msg_coins, parse_mode="HTML")

        @self.dp.callback_query(F.data.startswith("analytics_dome:"))
        async def cb_analytics_dome(callback: CallbackQuery):
            await callback.answer()
            target_uid = callback.data.split("analytics_dome:")[1]
            set_user_universe_id(callback.from_user.id, target_uid)
            await _show_universe_analytics_screen(callback, callback.from_user.id, edit=True, target_uid=target_uid)

        @self.dp.callback_query(F.data == "analytics_dome")
        async def cb_analytics_dome_current(callback: CallbackQuery):
            await callback.answer()
            await _show_universe_analytics_screen(callback, callback.from_user.id, edit=True)

        @self.dp.callback_query(F.data.startswith("analytics_chart"))
        async def process_analytics_chart(callback: CallbackQuery):
            await callback.answer()
            parts = callback.data.split(":", 1)
            target_uid = parts[1] if len(parts) > 1 and parts[1] else None
            current_uid, univ_name, _, _ = self._get_universe_analytics(callback.from_user.id, target_uid=target_uid)
            set_user_universe_id(callback.from_user.id, current_uid)
            msg = await callback.message.answer(f"⏳ Генерация графика Net Profit для <code>{current_uid}</code>...", parse_mode="HTML")
            try:
                plot_path = generate_equity_curve(universe_id=current_uid)
                if plot_path and os.path.exists(plot_path):
                    await msg.delete()
                    await callback.message.answer_photo(
                        FSInputFile(plot_path),
                        caption=f"📈 <b>Кривая чистого Net Profit & Эквити</b>: <b>{univ_name}</b> (<code>{current_uid}</code>)",
                        parse_mode="HTML"
                    )
                else:
                    await msg.edit_text(f"❌ Не удалось построить график эквити для {current_uid}.")
            except Exception as e:
                logger.error(f"Error generating equity curve: {e}")
                await msg.edit_text(f"❌ Ошибка генерации графика: {e}")

        @self.dp.callback_query(F.data == "analytics_back_to_univ")
        async def cb_analytics_back_to_univ(callback: CallbackQuery):
            await callback.answer()
            await _show_universe_analytics_screen(callback, callback.from_user.id, edit=True)

        @self.dp.message(Command("leaderboard"))
        @self.dp.message(F.text == "🏆 Leaderboard")
        async def cmd_leaderboard(message: Message):
            try:
                await _send_leaderboard(message, is_callback=False)
            except Exception as e:
                logger.error(f"Error in cmd_leaderboard: {e}")
                await message.answer(f"❌ Ошибка формирования лидерборда: {e}")

        @self.dp.callback_query(F.data == "analytics_leaderboard")
        async def cb_analytics_leaderboard(callback: CallbackQuery):
            try:
                await _send_leaderboard(callback, is_callback=True)
            except Exception as e:
                logger.error(f"Error in cb_analytics_leaderboard: {e}")
                await callback.message.answer(f"❌ Ошибка формирования лидерборда: {e}")

        async def _render_strategies_menu(target_msg_or_cb):
            app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
            all_strats = app_data.get("active_strategies", {})
            if not all_strats:
                text = "ℹ️ В app.json нет сконфигурированных стратегий."
                if isinstance(target_msg_or_cb, CallbackQuery):
                    await target_msg_or_cb.message.answer(text)
                else:
                    await target_msg_or_cb.answer(text)
                return

            buttons = []
            for sid, sdata in all_strats.items():
                if sid.startswith("_") or not isinstance(sdata, dict):
                    continue
                is_en = sdata.get("enabled", False)
                status_icon = "🟢" if is_en else "🔴"
                meta = self._get_strategy_meta(sid)
                sname = meta.get("name", sid)
                clean_name = sname.encode("ascii", "ignore").decode("ascii").strip() or sid
                sym_cnt = len(sdata.get("symbols", []))
                btn_txt = f"{status_icon} {clean_name[:28]} ({sym_cnt}м)"
                buttons.append([InlineKeyboardButton(text=btn_txt, callback_data=f"strat_analytics:{sid}")])

            buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="univ_menu_back")])
            text = (
                "🌌 <b>Управление вселенными (Купола):</b>\n\n"
                "🟢 — Включена в торги (Active)\n"
                "🔴 — Отключена (На паузе)\n\n"
                "<i>Нажмите на вселенную для переключения тумблера или просмотра метрик:</i>"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            if isinstance(target_msg_or_cb, CallbackQuery):
                await target_msg_or_cb.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
            else:
                await target_msg_or_cb.answer(text, reply_markup=keyboard, parse_mode="HTML")

        async def _render_strategy_details(callback: CallbackQuery, strat_id: str):
            app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
            sdata = app_data.get("active_strategies", {}).get(strat_id, {})
            is_enabled = sdata.get("enabled", False)
            coins_list = [str(s).upper().strip() for s in sdata.get("symbols", [])]
            coins_str = ", ".join(coins_list) if coins_list else "—"

            meta = self._get_strategy_meta(strat_id)
            strat_name = meta.get("name", strat_id)

            strat_file = ANALYTICS_STATS_DIR / f"analytics_{strat_id}.json"
            analytics_data = {}
            if strat_file.exists():
                try:
                    analytics_data = json.loads(strat_file.read_text(encoding="utf-8"))
                except Exception as e:
                    logger.warning(f"Failed to read analytics file for {strat_id}: {e}")

            net = float(analytics_data.get("net_profit_usdt", 0.0))
            real = float(analytics_data.get("realized_pnl_usdt", 0.0))
            unreal = float(analytics_data.get("unrealized_pnl_usdt", 0.0))
            dd = float(analytics_data.get("max_drawdown_usdt", 0.0))
            rf = float(analytics_data.get("recovery_factor", 0.0))
            trades = int(analytics_data.get("total_trades", 0))
            wins = int(analytics_data.get("winning_trades", 0))
            wr = float(analytics_data.get("winrate_pct", 0.0))
            cur_bal = float(analytics_data.get("cur_balance_usdt", sdata.get("analytics", {}).get("start_balance_usdt", 57.0)))
            start_bal = float(analytics_data.get("start_balance_usdt", sdata.get("analytics", {}).get("start_balance_usdt", 57.0)))

            iter_idx = int(analytics_data.get("iteration_index", 1))
            iter_net = float(analytics_data.get("iteration_net_profit_usdt", net))
            iter_start_net = float(analytics_data.get("iteration_start_net_profit_usdt", 0.0))
            iter_real = float(analytics_data.get("iteration_realized_pnl_usdt", real))
            iter_vel = float(analytics_data.get("iteration_net_velocity", 0.0))
            peak_net = float(analytics_data.get("peak_net_profit_usdt", max(0.0, cur_bal - start_bal)))
            peak_age = float(analytics_data.get("peak_age_hours", 0.0))

            status_str = "🟢 <b>ВКЛЮЧЕНА (В работе)</b>" if is_enabled else "🔴 <b>ВЫКЛЮЧЕНА (На паузе)</b>"
            toggle_btn_text = "🔘 Тумблер: 🟢 ВКЛ (Нажмите для ВЫКЛ)" if is_enabled else "🔘 Тумблер: 🔴 ВЫКЛ (Нажмите для ВКЛ)"

            text = (
                f"🌌 <b>ВСЕЛЕННАЯ: {strat_name}</b>\n"
                f"ID: <code>{strat_id}</code>\n"
                f"Состояние: {status_str}\n"
                f"🪙 Монеты ({len(coins_list)}): <b>{coins_str}</b>\n\n"
                f"🔄 <b>Текущий Цикл: #{iter_idx}</b>\n"
                f"├ Net цикла: <b>{iter_net:+.2f} USDT</b> (База: {iter_start_net:+.2f} $)\n"
                f"├ Realized цикла: <b>{iter_real:+.2f} $</b>\n"
                f"└ Скорость цикла: <b>{iter_vel:.2f} $/ч</b>\n\n"
                f"💰 Баланс: <b>{cur_bal:+.2f} $</b> (Старт: {start_bal:.2f} $)\n"
                f"📈 Кумулятивный Net: <b>{net:+.2f} USDT</b>\n"
                f"├ Realized (сумм): <b>{real:+.2f} $</b>\n"
                f"└ Floating: <b>{unreal:+.2f} $</b>\n\n"
                f"🏔️ Peak Net: <b>+{peak_net:.2f} $</b> ({peak_age:.1f}h)\n"
                f"📉 Max Drawdown: <b>{dd:.2f} $</b>\n"
                f"🛡️ Recovery Factor: <b>{rf:.2f}</b>\n"
                f"🎯 Сделок: <b>{trades}</b> (Побед: {wins} | Winrate: {wr:.1f}%)\n"
            )
            strat_vel = analytics_data.get("trade_velocity")
            if not strat_vel and hasattr(self.bot_core, "analytics") and hasattr(self.bot_core.analytics, "get_trade_velocity"):
                strat_vel = self.bot_core.analytics.get_trade_velocity(strat_id)
            vel_block = self._format_trade_velocity_block(strat_vel, title="⚡ Trade Velocity")
            text += f"\n{vel_block}\n"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=toggle_btn_text, callback_data=f"toggle_strat:{strat_id}")],
                [InlineKeyboardButton(text="📝 Леджер вселенной (TXT)", callback_data=f"strat_ledger:{strat_id}")],
                [InlineKeyboardButton(text="🔙 К списку вселенных", callback_data="analytics_strats_menu")]
            ])
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

        @self.dp.message(Command("universes"))
        @self.dp.message(Command("strategies"))
        @self.dp.message(Command("strats"))
        @self.dp.message(F.text == "🌌 Set Universes")
        @self.dp.message(F.text == "🌌 Strategies")
        @self.dp.message(F.text == "🌌 Стратегии")
        async def on_strategies_cmd(message: Message, state: FSMContext):
            await state.clear()
            await _render_strategies_menu(message)

        @self.dp.callback_query(F.data == "univ_menu_back")
        async def cb_univ_menu_back(callback: CallbackQuery):
            await callback.answer()
            try:
                await callback.message.delete()
            except Exception:
                pass
            status = "⏸️ Paused" if self.bot_core.is_paused else "▶️ Running"
            await callback.message.answer(f"<b>Control Panel</b>\nСтатус бота: {status}", reply_markup=self._get_main_keyboard(), parse_mode="HTML")

        @self.dp.callback_query(F.data == "analytics_strats_menu")
        async def cb_analytics_strats_menu(callback: CallbackQuery):
            await callback.answer()
            await _render_strategies_menu(callback)
            await callback.answer()
            await _render_strategies_menu(callback)

        @self.dp.callback_query(F.data.startswith("strat_analytics:"))
        async def cb_strat_analytics(callback: CallbackQuery):
            await callback.answer()
            strat_id = callback.data.split("strat_analytics:")[1]
            try:
                await _render_strategy_details(callback, strat_id)
            except Exception as e:
                logger.error(f"Error viewing strategy details: {e}")
                await callback.message.answer(f"Ошибка загрузки карточки стратегии: {e}")

        @self.dp.callback_query(F.data.startswith("toggle_strat:"))
        async def cb_toggle_strat(callback: CallbackQuery):
            strat_id = callback.data.split("toggle_strat:")[1]
            app_path = DATA_DIR / "app.json"
            app_data = Utils.read_json_file(app_path) or {}
            active_strats = app_data.get("active_strategies", {})
            if strat_id not in active_strats:
                await callback.answer(f"⚠️ Стратегия {strat_id} не найдена в app.json!", show_alert=True)
                return

            sdata = active_strats[strat_id]
            cur_en = sdata.get("enabled", False)

            # 1. ЗАЩИТА ПРИ ОТКЛЮЧЕНИИ: проверяем открытые позиции
            if cur_en:
                strat_symbols = [str(s).upper().strip() for s in sdata.get("symbols", [])]
                open_positions = []
                if hasattr(self.bot_core, "runtime_manager") and hasattr(self.bot_core.runtime_manager, "caches"):
                    for sym in strat_symbols:
                        c = self.bot_core.runtime_manager.caches.get(sym, {})
                        for side in ("LONG", "SHORT"):
                            side_st = c.get(side)
                            if side_st and (getattr(side_st, "in_position", False) or getattr(side_st, "position_amt", 0) != 0):
                                amt = getattr(side_st, "position_amt", 0)
                                open_positions.append(f"{sym} {side} (объем: {amt})")

                if open_positions:
                    pos_list_str = "\n".join([f"• {p}" for p in open_positions])
                    err_alert = (
                        f"⛔ <b>БЛОКИРОВКА БЕЗОПАСНОСТИ!</b>\n\n"
                        f"Нельзя отключить стратегию <code>{strat_id}</code>: обнаружены открытые позиции на бирже:\n"
                        f"{pos_list_str}\n\n"
                        f"Отключение стратегии оставит открытые позиции без сопровождения сетки и тейк-профита!\n"
                        f"Сначала закройте позиции в кэш через 🚨 <b>Close All</b> или дождитесь закрытия тейка."
                    )
                    await callback.answer(f"⛔ Запрещено: есть открытые позиции ({len(open_positions)} шт)!", show_alert=True)
                    await callback.message.answer(err_alert, parse_mode="HTML")
                    return

            # 2. ЗАЩИТА ПРИ ВКЛЮЧЕНИИ: проверяем конфликты дублирования монет на одном аккаунте
            else:
                my_acc = str(sdata.get("api_key_name") or "API_KEY").strip()
                my_symbols = [str(s).upper().strip() for s in sdata.get("symbols", [])]
                conflicts = []
                for other_id, other_data in active_strats.items():
                    if other_id == strat_id or not isinstance(other_data, dict):
                        continue
                    if not other_data.get("enabled", False):
                        continue
                    other_acc = str(other_data.get("api_key_name") or "API_KEY").strip()
                    if other_acc == my_acc:
                        other_symbols = [str(s).upper().strip() for s in other_data.get("symbols", [])]
                        intersect = set(my_symbols) & set(other_symbols)
                        if intersect:
                            conflicts.append((other_id, list(intersect)))

                if conflicts:
                    conflict_lines = "\n".join([f"• Монеты <code>{', '.join(c_syms)}</code> уже активны в <code>{o_id}</code>" for o_id, c_syms in conflicts])
                    err_alert = (
                        f"⛔ <b>БЛОКИРОВКА БЕЗОПАСНОСТИ!</b>\n\n"
                        f"Нельзя включить стратегию <code>{strat_id}</code> на аккаунте <code>{my_acc}</code>:\n"
                        f"{conflict_lines}\n\n"
                        f"На одном биржевом счете Binance монеты между активными стратегиями обязаны быть строго уникальными!\n"
                        f"Устраните дублирование монет перед включением."
                    )
                    await callback.answer(f"⛔ Конфликт: монеты уже активны в другой стратегии!", show_alert=True)
                    await callback.message.answer(err_alert, parse_mode="HTML")
                    return

            # 3. Применение изменения
            new_en = not cur_en
            sdata["enabled"] = new_en
            Utils.write_json_file(app_path, app_data)

            if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
                try:
                    self.bot_core.strategy_registry.reload(active_strats)
                    if hasattr(self.bot_core, "_init_clients"):
                        self.bot_core._init_clients()
                    self.bot_core.symbols = self.bot_core.strategy_registry.get_all_active_symbols()
                except Exception as e:
                    logger.error(f"Error reloading StrategyRegistry on toggle: {e}")

            action_str = "ВКЛЮЧЕНА 🟢" if new_en else "ВЫКЛЮЧЕНА 🔴"
            await callback.answer(f"✅ Стратегия {strat_id} {action_str}!", show_alert=True)
            await _render_strategy_details(callback, strat_id)

        @self.dp.callback_query(F.data.startswith("strat_ledger:"))
        async def cb_strat_ledger(callback: CallbackQuery):
            await callback.answer()
            strat_id = callback.data.split("strat_ledger:")[1]
            strat_txt = ANALYTICS_LEDGERS_DIR / f"trades_ledger_{strat_id}.txt"
            if not strat_txt.exists() or strat_txt.stat().st_size <= 60:
                matches = list(ANALYTICS_LEDGERS_DIR.glob(f"trades_ledger_{strat_id}*.txt"))
                found = [m for m in matches if m.stat().st_size > 60]
                if found:
                    strat_txt = sorted(found)[0]
                else:
                    general_ledger = ANALYTICS_LEDGERS_DIR / "trades_ledger.txt"
                    if general_ledger.exists() and general_ledger.stat().st_size > 60:
                        strat_txt = general_ledger

            if strat_txt.exists() and strat_txt.stat().st_size > 60:
                await callback.message.answer_document(FSInputFile(str(strat_txt)))
            else:
                await callback.message.answer(f"Леджер для {strat_id} пока пуст.")

        @self.dp.callback_query(F.data == "analytics_cheat_sheet")
        async def process_analytics_cheat_sheet(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            help_str = (
                "<b>📚 Analytics Cheat Sheet (Global)</b>\n"
                "▪️ <b>roi_pct</b>: Return on Investment (%) → <i>(Cur_Balance - Start_Balance) / Start_Balance * 100</i>\n"
                "▪️ <b>grid_load_pct</b>: Grid Capacity Utilization (%) → <i>Current Grid Load / Total Grid Size * 100</i>\n"
                "▪️ <b>margin_load_pct</b>: Margin Usage (%) → <i>Margin Used / Floating Balance * 100</i>\n"
                "▪️ <b>free_margin_usdt</b>: Free Margin → <i>Floating Balance - Margin Used</i>\n"
                "▪️ <b>recovery_factor</b>: Recovery Factor → <i>Net Profit / |Max Drawdown|</i>\n"
                "▪️ <b>abs_recovery_factor</b>: Abs Recovery Factor → <i>Peak Net Profit / |Max Drawdown|</i>\n"
                "▪️ <b>realized_pnl_usdt</b>: Total realized profit from all closed trades\n"
                "▪️ <b>net_profit_usdt</b>: True mathematical growth → <i>Realized PnL + Unrealized PnL</i>\n"
                "▪️ <b>cur_balance_usdt</b>: Current margin balance → <i>Start Balance + Net Profit</i>\n\n"
                "<b>🪙 Per-Coin Metrics</b>\n"
                "▪️ <b>avg_daily_profit</b>: Average profit per active day of trading\n"
                "▪️ <b>max_position_size</b>: Max historical notional size actually reached (real volume * price)\n"
                "▪️ <b>risk_reward_ratio</b>: <i>|Max Drawdown| / Avg Daily Profit</i>\n"
                "▪️ <b>recovery_factor</b> / <b>abs_recovery_factor</b>: Current and absolute recovery factors\n"
                "▪️ <b>max_drawdown</b> / <b>min_drawdown</b>: Max/Min historical floating drawdowns\n"
            )
            await callback.message.answer(help_str, parse_mode="HTML")

        @self.dp.callback_query(F.data.startswith("analytics_ranking"))
        async def process_analytics_ranking(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            # analytics_ranking[:criterion[:target_uid]]
            parts = callback.data.split(":")
            criterion = parts[1] if len(parts) > 1 and parts[1] else "net"
            target_uid = parts[2] if len(parts) > 2 and parts[2] else None
            is_switch = len(parts) > 3 and parts[3] == "switch"
            current_uid, univ_name, analytics_path, _ = self._get_universe_analytics(callback.from_user.id, target_uid=target_uid)
            set_user_universe_id(callback.from_user.id, current_uid)
            import html
            clean_name = html.escape(univ_name)
            
            if not analytics_path.exists():
                await callback.message.answer(f"Analytics JSON для купола <code>{current_uid}</code> не найден.", parse_mode="HTML")
                return
                
            try:
                data = json.loads(analytics_path.read_text(encoding="utf-8"))
                per_coin = data.get("per_coin", {})
                
                # Если в per_coin пока нет монет (новый купол), подтягиваем активные монеты этой стратегии
                if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
                    scfg = self.bot_core.strategy_registry.active_strategies.get(current_uid)
                    if scfg and hasattr(scfg, "symbols"):
                        for s_sym in scfg.symbols:
                            per_coin.setdefault(s_sym, {
                                "net_profit_usdt": 0.0,
                                "realized_pnl_net_usdt": 0.0,
                                "recovery_factor": 0.0,
                                "abs_recovery_factor": 0.0,
                                "total_trades": 0
                            })

                if not per_coin:
                    await callback.message.answer(f"Нет данных по монетам для купола <code>{current_uid}</code>.", parse_mode="HTML")
                    return
                
                def safe_float(v, default=0.0):
                    try:
                        if v is None: return default
                        return float(v)
                    except (ValueError, TypeError):
                        return default

                # Determine sorting logic and title
                if criterion == "rec":
                    sorted_coins = sorted(per_coin.items(), key=lambda x: safe_float(x[1].get("recovery_factor", -float('inf'))), reverse=True)
                    title = "по Recovery Factor (Тек) (больше = лучше)"
                elif criterion == "abs_rec":
                    sorted_coins = sorted(per_coin.items(), key=lambda x: safe_float(x[1].get("abs_recovery_factor", -float('inf'))), reverse=True)
                    title = "по Abs Recovery (Пик/DD) (больше = лучше)"
                else: # Default net
                    criterion = "net"
                    sorted_coins = sorted(per_coin.items(), key=lambda x: safe_float(x[1].get("net_profit_usdt", -float('inf'))), reverse=True)
                    title = "по Net Profit (больше = лучше)"
                    
                lines = [f"<b>🏆 Рейтинг монет [Купол: {clean_name}] ({title})</b>\n"]
                for i, (sym, cdata) in enumerate(sorted_coins, 1):
                    val_str = ""
                    if criterion == "rec":
                        rec = safe_float(cdata.get("recovery_factor", 0.0))
                        net = safe_float(cdata.get("net_profit_usdt", 0.0))
                        dd = abs(safe_float(cdata.get("max_drawdown_net_usdt", cdata.get("max_drawdown", 0.0))))
                        val_str = f"Rec: <b>{rec:.2f}</b> <i>(Net: {net:+.2f} / |DD|: {dd:.2f})</i>"
                    elif criterion == "abs_rec":
                        abs_rec = safe_float(cdata.get("abs_recovery_factor", 0.0))
                        peak = safe_float(cdata.get("peak_net_profit_usdt", max(0.0, safe_float(cdata.get("net_profit_usdt", 0.0)))))
                        dd = abs(safe_float(cdata.get("max_drawdown_net_usdt", cdata.get("max_drawdown", 0.0))))
                        val_str = f"Abs Rec: <b>{abs_rec:.2f}</b> <i>(Пик Net: {peak:+.2f} / |DD|: {dd:.2f})</i>"
                    else:
                        net_profit = round(safe_float(cdata.get("net_profit_usdt", 0)), 2)
                        realized_net = round(safe_float(cdata.get("realized_pnl_net_usdt", 0)), 2)
                        tr = cdata.get('total_trades', cdata.get('trades', 0))
                        st_bal = safe_float(data.get("start_balance_usdt", 151.0))
                        c_roi = round(net_profit / st_bal * 100.0, 2) if st_bal > 0 else 0.0
                        c_roi_sign = "+" if c_roi > 0 else ""
                        val_str = f"{net_profit:+.2f} USDT ({c_roi_sign}{c_roi:.2f}% ROI) <i>({tr} сд., Realized: {realized_net:+.2f})</i>"
                    
                    is_act = cdata.get("is_active_symbol", cdata.get("is_active", True))
                    active_badge = "" if is_act else " ⏸<i>[неактивна]</i>"
                    if i == 1:
                        medal = "🥇"
                    elif i == 2:
                        medal = "🥈"
                    elif i == 3:
                        medal = "🥉"
                    else:
                        medal = "▪️"
                        
                    lines.append(f"{medal} <b>{sym}</b>{active_badge}: {val_str}")
                
                # Add inline keyboard to switch criterion
                switch_kb = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="Net Profit", callback_data=f"analytics_ranking:net:{current_uid}:switch"),
                        InlineKeyboardButton(text="Recovery (Тек)", callback_data=f"analytics_ranking:rec:{current_uid}:switch"),
                        InlineKeyboardButton(text="Abs Recovery", callback_data=f"analytics_ranking:abs_rec:{current_uid}:switch")
                    ],
                    [
                        InlineKeyboardButton(text="❌ Закрыть рейтинг", callback_data="delete_ranking_msg")
                    ]
                ])
                    
                # Split messages if too long
                messages = []
                current_msg = ""
                for line in lines:
                    if len(current_msg) + len(line) + 1 > 4000:
                        messages.append(current_msg)
                        current_msg = line + "\n"
                    else:
                        current_msg += line + "\n"
                if current_msg:
                    messages.append(current_msg)

                if is_switch:
                    try:
                        await callback.message.edit_text(messages[0], parse_mode="HTML", reply_markup=switch_kb)
                    except Exception:
                        pass
                else:
                    for i, msg_text in enumerate(messages):
                        rm = switch_kb if i == len(messages) - 1 else None
                        await callback.message.answer(msg_text, parse_mode="HTML", reply_markup=rm)
            except Exception as e:
                logger.error(f"Error generating ranking for {current_uid}: {e}")
                await callback.message.answer("⚠️ Ошибка отображения рейтинга монет.")

        @self.dp.callback_query(F.data == "delete_ranking_msg")
        async def cb_delete_ranking_msg(callback: CallbackQuery):
            await callback.answer()
            try:
                await callback.message.delete()
            except Exception:
                pass

        @self.dp.callback_query(F.data.startswith("analytics_txt"))
        async def process_analytics_txt(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            parts = callback.data.split(":", 1)
            target_uid = parts[1] if len(parts) > 1 and parts[1] else None
            current_uid, univ_name, _, csv_path = self._get_universe_analytics(callback.from_user.id, target_uid=target_uid)
            set_user_universe_id(callback.from_user.id, current_uid)
            if not csv_path.exists() or csv_path.stat().st_size <= 60:
                general_ledger = ANALYTICS_LEDGERS_DIR / "trades_ledger.txt"
                if general_ledger.exists() and general_ledger.stat().st_size > 60:
                    csv_path = general_ledger

            if csv_path.exists() and csv_path.stat().st_size > 60:
                await callback.message.answer_document(
                    FSInputFile(str(csv_path)),
                    caption=f"📝 Лента сделок (TXT): <b>{univ_name}</b> (<code>{current_uid}</code>)",
                    parse_mode="HTML"
                )
            else:
                await callback.message.answer(f"Trades ledger TXT для купола <code>{current_uid}</code> не найден или пуст.", parse_mode="HTML")

        @self.dp.callback_query(F.data.startswith("analytics_full_report"))
        async def process_analytics_full_report(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            parts = callback.data.split(":", 1)
            target_uid = parts[1] if len(parts) > 1 and parts[1] else None
            current_uid, univ_name, analytics_path, _ = self._get_universe_analytics(callback.from_user.id, target_uid=target_uid)
            set_user_universe_id(callback.from_user.id, current_uid)
            if not analytics_path.exists():
                await callback.message.answer(f"Analytics JSON для купола <code>{current_uid}</code> не найден.", parse_mode="HTML")
                return
                
            try:
                data = json.loads(analytics_path.read_text(encoding="utf-8"))
                
                lines = []
                lines.append("="*50)
                lines.append(f"STRATEGY DOME ANALYTICS REPORT: {univ_name} ({current_uid})")
                lines.append("="*50 + "\n")
                
                start_val = data.get('start_balance_usdt', 0)
                cur_val = data.get('cur_balance_usdt', start_val)
                peak_val = data.get('peak_balance_usdt') if data.get('peak_balance_usdt') is not None else (cur_val if cur_val is not None else start_val)
                min_val = data.get('min_balance_usdt') if data.get('min_balance_usdt') is not None else (cur_val if cur_val is not None else start_val)
                
                lines.append("Balances:")
                lines.append(f"  Start: {start_val:,.2f} USDT")
                lines.append(f"  Current: {cur_val:,.2f} USDT")
                lines.append(f"  Peak: {peak_val:,.2f} USDT")
                lines.append(f"  Min (Bottom): {min_val:,.2f} USDT\n")
                
                per_coin_dict = data.get("per_coin", {})
                realized = float(data.get('realized_pnl_usdt', 0))
                realized_net = float(data.get('realized_pnl_net_usdt', realized))
                comm = float(data.get('total_commission_usdt', 0))
                fund = float(data.get('total_funding_usdt', 0))
                if comm == 0.0 and per_coin_dict:
                    comm = sum(float(c.get("commission_usdt", 0.0)) for c in per_coin_dict.values())
                if fund == 0.0 and per_coin_dict:
                    fund = sum(float(c.get("funding_usdt", 0.0)) for c in per_coin_dict.values())
                if realized_net == 0.0 and per_coin_dict:
                    realized_net = sum(float(c.get("realized_pnl_net_usdt", c.get("realized_pnl_usdt", 0.0))) for c in per_coin_dict.values())
                if realized == 0.0 and per_coin_dict:
                    realized = sum(float(c.get("realized_pnl_usdt", c.get("realized_pnl_net_usdt", 0.0))) for c in per_coin_dict.values())

                g_cap = float(data.get('total_grid_size_usdt') or 0.0)
                g_load = float(data.get('current_grid_load_usdt') or 0.0)
                g_pct = float(data.get('grid_load_pct') or 0.0)
                if g_cap == 0.0 and per_coin_dict:
                    g_cap = sum(float(c.get("grid_capacity_usdt", 0.0)) for c in per_coin_dict.values())
                if g_load == 0.0 and per_coin_dict:
                    g_load = sum(float(c.get("current_grid_load_usdt", 0.0)) for c in per_coin_dict.values())
                if g_pct == 0.0 and g_cap > 0:
                    g_pct = round((g_load / g_cap) * 100.0, 2)

                m_used = float(data.get('total_margin_used_usdt') or 0.0)
                m_pct = float(data.get('margin_load_pct') or 0.0)
                if m_used == 0.0 and per_coin_dict:
                    m_used = sum(float(c.get("current_margin_usdt", 0.0)) for c in per_coin_dict.values())
                if m_pct == 0.0 and cur_val > 0:
                    m_pct = round((m_used / cur_val) * 100.0, 2)

                f_marg = float(data.get('free_margin_usdt') or 0.0)
                f_pct = float(data.get('free_margin_pct') or 0.0)
                if f_marg == 0.0 and cur_val > 0:
                    f_marg = max(0.0, cur_val - m_used)
                    f_pct = round((f_marg / cur_val) * 100.0, 2)

                lines.append("Performance:")
                lines.append(f"  Net Profit: {data.get('net_profit_usdt', 0):,.2f} USDT")
                lines.append(f"  Realized PnL (Gross): {realized:,.2f} USDT")
                lines.append(f"  Realized PnL (Net): {realized_net:,.2f} USDT")
                lines.append(f"  Total Commission: {comm:,.2f} USDT")
                lines.append(f"  Total Funding: {fund:,.2f} USDT")
                lines.append(f"  Unrealized PnL: {data.get('unrealized_pnl_usdt', 0):,.2f} USDT")
                lines.append(f"  Max Drawdown: {data.get('max_drawdown_usdt', 0):,.2f} USDT")
                lines.append(f"  Performance (Peak-Start): {data.get('performance_usdt', 0):,.2f} USDT")
                roi_val = data.get('roi_pct')
                if roi_val is None:
                    roi_val = round(((cur_val - start_val) / start_val * 100.0), 2) if start_val > 0 else 0.0
                else:
                    roi_val = float(roi_val)
                lines.append(f"  ROI: {roi_val:+.2f}%")
                lines.append(f"  Grid Load: {g_pct:.2f}% ({g_load:,.2f} / {g_cap:,.2f} USDT | Max: {data.get('max_grid_load_pct', g_pct):.2f}%)")
                lines.append(f"  Margin Load: {m_pct:.2f}% (Used: {m_used:,.2f} USDT | Max: {data.get('max_margin_load_pct', m_pct):.2f}%)")
                lines.append(f"  Free Margin: {f_marg:,.2f} USDT ({f_pct:.2f}% | Min: {data.get('min_free_margin_usdt', f_marg):,.2f} USDT)")
                lines.append(f"  Recovery Factor: {data.get('recovery_factor', 0):.2f}")
                lines.append(f"  Trades: {data.get('total_trades', 0)} (Wins: {data.get('winning_trades', 0)} | {data.get('winrate_pct', 0):.1f}%)\n")
                
                if "per_coin" in data:
                    lines.append("="*50)
                    lines.append("PER-COIN METRICS")
                    lines.append("="*50 + "\n")
                    
                    for sym, cdata in sorted(data["per_coin"].items()):
                        status_str = "[АКТИВНА]" if cdata.get("is_active_symbol", True) else "[ВЫВЕДЕНА / АРХИВ]"
                        lines.append(f"--- {sym} {status_str} ---")
                        lines.append(f"  Trades: {cdata.get('total_trades', 0)} (Wins: {cdata.get('winning_trades', 0)} | {cdata.get('winrate_pct', 0):.1f}%)")
                        
                        crealized = float(cdata.get('realized_pnl_usdt', 0))
                        crealized_net = float(cdata.get('realized_pnl_net_usdt', crealized))
                        ccomm = float(cdata.get('commission_usdt', 0))
                        cfund = float(cdata.get('funding_usdt', 0))
                        
                        lines.append(f"  Net Profit: {cdata.get('net_profit_usdt', 0):,.2f} USDT")
                        lines.append(f"  Realized (Gross): {crealized:,.2f} USDT")
                        lines.append(f"  Realized (Net): {crealized_net:,.2f} USDT")
                        lines.append(f"  Fees: Comm {ccomm:,.2f} / Fund {cfund:,.2f}")
                        lines.append(f"  Unrealized PnL: {cdata.get('current_drawdown', 0):,.2f} USDT")
                        lines.append(f"  Hist. Drawdown: Max {cdata.get('max_drawdown', 0):,.2f} / Min {cdata.get('min_drawdown', 0):,.2f}")
                        lines.append(f"  Avg Daily Profit (Net): {cdata.get('avg_daily_profit', 0):,.2f} USDT")
                        lines.append(f"  Grid Load: {cdata.get('current_grid_load_pct', 0):.2f}% ({cdata.get('current_grid_load_usdt', 0):,.2f} / {cdata.get('grid_capacity_usdt', 0):,.2f} USDT | Max: {cdata.get('max_grid_load_pct', 0):.2f}%)")
                        lines.append(f"  Margin Used: {cdata.get('current_margin_usdt', 0):,.2f} USDT ({cdata.get('margin_load_pct', 0):.2f}% balance | Max: {cdata.get('max_margin_usdt', 0):,.2f} USDT)")
                        lines.append(f"  Max Position Size: {cdata.get('max_position_size', 0):,.2f} USDT")
                        lines.append(f"  Recovery Factor: {cdata.get('recovery_factor', 0):.2f}")
                        lines.append(f"  Risk/Reward Ratio: {cdata.get('risk_reward_ratio', 0):.2f}")
                        lines.append(f"  Abs Recovery Factor: {cdata.get('abs_recovery_factor', 0):.2f}\n")
                
                report_path = ANALYTICS_DIR / f"full_report_{current_uid}.txt"
                report_path.write_text("\n".join(lines), encoding="utf-8")
                
                await callback.message.answer_document(
                    FSInputFile(str(report_path)),
                    caption=f"📄 Полный отчет (TXT): <b>{univ_name}</b> (<code>{current_uid}</code>)",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Error generating full report for {current_uid}: {e}")
                await callback.message.answer("Error generating report.")

        @self.dp.callback_query(F.data.startswith("analytics_global"))
        async def process_analytics_global(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            parts = callback.data.split(":", 1)
            target_uid = parts[1] if len(parts) > 1 and parts[1] else None
            await _show_universe_analytics_screen(callback, callback.from_user.id, edit=True, target_uid=target_uid)

        @self.dp.callback_query(F.data.startswith("analytics_coin_menu"))
        async def process_analytics_coin_menu(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            parts = callback.data.split(":", 1)
            target_uid = parts[1] if len(parts) > 1 and parts[1] else None
            current_uid, univ_name, analytics_path, _ = self._get_universe_analytics(callback.from_user.id, target_uid=target_uid)
            set_user_universe_id(callback.from_user.id, current_uid)
            
            universe_coins = set()
            if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
                active_strats = self.bot_core.strategy_registry.get_active_strategies()
                if current_uid in active_strats:
                    universe_coins.update(active_strats[current_uid].symbols)
                    
            if analytics_path.exists():
                try:
                    data = json.loads(analytics_path.read_text(encoding="utf-8"))
                    if "per_coin" in data:
                        universe_coins.update(data["per_coin"].keys())
                except Exception:
                    pass
            
            if not universe_coins:
                await callback.message.answer(f"❌ Нет данных о монетах для аналитики вселенной <code>{current_uid}</code>.", parse_mode="HTML")
                return
                
            keyboard_buttons = []
            row = []
            for coin in sorted(universe_coins):
                is_active = coin in self.bot_core.symbols
                status_icon = "🟢" if is_active else "⏸"
                status_tag = "" if is_active else " [неактивна]"
                row.append(InlineKeyboardButton(text=f"🪙 {coin}{status_tag} {status_icon}", callback_data=f"analytics_coin:{coin}:{current_uid}"))
                if len(row) == 2:
                    keyboard_buttons.append(row)
                    row = []
            if row:
                keyboard_buttons.append(row)
            keyboard_buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"analytics_dome:{current_uid}")])
                
            keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
            await callback.message.answer(
                f"🪙 <b>Монеты вселенной:</b> {univ_name} (<code>{current_uid}</code>)\n"
                f"Выберите монету для детальной аналитики баланса:",
                reply_markup=keyboard,
                parse_mode="HTML"
            )

        @self.dp.callback_query(F.data.startswith("analytics_coin:") | (F.data.startswith("analytics_coin_") & ~F.data.startswith("analytics_coin_menu")))
        async def process_analytics_coin_select(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            if callback.data.startswith("analytics_coin:"):
                c_parts = callback.data.split(":")
                symbol = c_parts[1]
                target_uid = c_parts[2] if len(c_parts) > 2 and c_parts[2] else None
            else:
                symbol = callback.data.replace("analytics_coin_", "")
                target_uid = None
            current_uid, univ_name, analytics_path, _ = self._get_universe_analytics(callback.from_user.id, target_uid=target_uid)
            set_user_universe_id(callback.from_user.id, current_uid)
            
            is_active = symbol in self.bot_core.symbols
            status_text = "🟢 Активна" if is_active else "🔴 Отключена"
            
            if analytics_path.exists():
                try:
                    data = json.loads(analytics_path.read_text(encoding="utf-8"))
                    if "per_coin" in data and symbol in data["per_coin"]:
                        cdata = data["per_coin"][symbol]
                        
                        net_profit = float(cdata.get('net_profit_usdt', 0.0))
                        realized = float(cdata.get('realized_pnl_usdt', 0.0))
                        realized_net = float(cdata.get('realized_pnl_net_usdt', realized))
                        max_np = float(cdata.get('max_net_profit', net_profit))
                        min_np = float(cdata.get('min_net_profit', net_profit))
                        cur_dd = float(cdata.get('current_drawdown', 0.0))
                        max_dd = float(cdata.get('max_drawdown', 0.0))
                        trades = int(cdata.get('total_trades', cdata.get('trades', 0)))
                        wins = int(cdata.get('winning_trades', cdata.get('win_count', 0)))
                        wr = float(cdata.get('winrate_pct', 0.0))
                        comm = float(cdata.get('commission_usdt', 0.0))
                        fund = float(cdata.get('funding_usdt', 0.0))
                        gload = float(cdata.get('current_grid_load_pct', 0.0))
                        mused = float(cdata.get('current_margin_usdt', 0.0))
                        
                        start_bal_u = float(data.get("start_balance_usdt", 151.0))
                        coin_roi_pct = round((net_profit / start_bal_u * 100.0), 2) if start_bal_u > 0 else 0.0
                        coin_roi_sign = "+" if coin_roi_pct > 0 else ""

                        msg = (
                            f"🪙 <b>ДЕТАЛИЗАЦИЯ БАЛАНСОВ: {symbol}</b> ({status_text})\n"
                            f"🌌 <b>{univ_name}</b> (<code>{current_uid}</code>)\n\n"
                            f"💰 <b>БАЛАНСЫ И PnL:</b>\n"
                            f"  • <b>Текущий PnL (Floating):</b> <code>{cur_dd:+.2f} USDT</code>\n"
                            f"  • <b>Пиковый Net Profit:</b> <code>{max_np:+.2f} USDT</code>\n"
                            f"  • <b>Самый низкий Net Profit:</b> <code>{min_np:+.2f} USDT</code>\n"
                            f"  • <b>Исторический Max Drawdown:</b> <code>{max_dd:.2f} USDT</code>\n"
                            f"  • <b>Накопленный Net Profit:</b> <code>{net_profit:+.2f} USDT</code> ({coin_roi_sign}{coin_roi_pct:.2f}% ROI)\n"
                            f"  • <b>Realized PnL (Net):</b> <code>{realized_net:+.2f} USDT</code> (Gross: <code>{realized:+.2f} $</code>)\n\n"
                            f"📊 <b>ТОРГОВЫЕ МЕТРИКИ:</b>\n"
                            f"  • Сделок: <b>{trades}</b> (Побед: {wins} | {wr:.1f}%)\n"
                            f"  • Grid Load: <b>{gload:.1f}%</b> | Margin Used: <b>{mused:.2f} USDT</b>\n"
                            f"  • Комиссии / Funding: Comm <b>{comm:.4f} $</b> | Fund <b>{fund:.4f} $</b>\n"
                        )
                        await callback.message.answer(msg, parse_mode="HTML")
                except Exception as e:
                    logger.error(f"Error reading per_coin data for {symbol}: {e}")


        @self.dp.message(F.text == "⚙️ Set Coins")
        async def on_set_coins(message: Message, state: FSMContext):
            await state.clear()
            active_strats = {}
            if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
                active_strats = self.bot_core.strategy_registry.get_active_strategies()
            else:
                app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
                active_strats = app_data.get("active_strategies", {})

            if not active_strats:
                await message.answer("⚠️ В системе нет активных куполов стратегий.", reply_markup=self._get_main_keyboard())
                return

            buttons = []
            for sid, scfg in active_strats.items():
                if sid.startswith("_") or not isinstance(scfg, (dict, object)):
                    continue
                meta = self._get_strategy_meta(sid)
                s_name = meta.get("name", sid)
                clean_title = s_name.encode("ascii", "ignore").decode("ascii").strip() or sid
                symbols = getattr(scfg, "symbols", []) if hasattr(scfg, "symbols") else (scfg.get("symbols", []) if isinstance(scfg, dict) else [])
                cnt = len(symbols)
                btn_text = f"🪐 {clean_title[:28]} ({cnt} мон.)"
                buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"set_coins_dome:{sid}")])

            buttons.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="set_coins_cancel")])
            text = (
                "⚙️ <b>Управление монетами куполов</b>\n\n"
                "<b>Шаг 1: Выберите купол стратегии</b>, в котором хотите настроить список монет:"
            )
            await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

        @self.dp.callback_query(F.data == "set_coins_cancel")
        async def cb_set_coins_cancel(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            await state.clear()
            try:
                await callback.message.delete()
            except Exception:
                pass
            status = "⏸️ Paused" if self.bot_core.is_paused else "▶️ Running"
            await callback.message.answer(f"<b>Control Panel</b>\nСтатус бота: {status}", reply_markup=self._get_main_keyboard(), parse_mode="HTML")

        @self.dp.callback_query(F.data.startswith("set_coins_dome:"))
        async def process_set_coins_dome_select(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            sid = callback.data.split("set_coins_dome:")[1]
            await state.update_data(selected_dome=sid)

            meta = self._get_strategy_meta(sid)
            s_name = meta.get("name", sid)

            symbols = []
            if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
                active_strats = self.bot_core.strategy_registry.get_active_strategies()
                if sid in active_strats:
                    symbols = list(active_strats[sid].symbols)
            else:
                app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
                symbols = list(app_data.get("active_strategies", {}).get(sid, {}).get("symbols", []))

            symbols_str = ", ".join(sorted(symbols)) if symbols else "Нет монет"
            try:
                await callback.message.delete()
            except Exception:
                pass

            text = (
                f"🌌 <b>Купол стратегии:</b> <code>{sid}</code>\n"
                f"🏷️ <b>Название:</b> {s_name}\n"
                f"🪙 <b>Текущие монеты:</b> {symbols_str}\n\n"
                f"<i>Выберите действие в меню:</i>\n"
                f"➕ <b>Добавить монету</b> — ввод тикера (старт на 1-м сигнале)\n"
                f"🗑️ <b>Удалить монету</b> — удаление тикера из купола\n"
                f"🔙 <b>Назад</b> — возврат в главное меню"
            )
            await callback.message.answer(text, reply_markup=self._get_set_coins_keyboard(), parse_mode="HTML")

        # =========================================================
        # ADD SYMBOL WITH STRATEGY ASSIGNMENT
        # =========================================================
        @self.dp.message(F.text.in_({"➕ Добавить монету", "➕ Add"}))
        async def on_add_btn(message: Message, state: FSMContext):
            data = await state.get_data()
            selected_dome = data.get("selected_dome")
            if not selected_dome:
                return await on_set_coins(message, state)

            meta = self._get_strategy_meta(selected_dome)
            s_name = meta.get("name", selected_dome)

            await state.set_state(TGStates.waiting_for_add_symbol)
            await message.answer(
                f"➕ <b>Добавление монеты в купол</b> <code>{selected_dome}</code> ({s_name})\n\n"
                f"✍️ Введите тикер монеты (например: <code>DOGEUSDT</code>):\n"
                f"<i>Монета автоматически вступит в торговлю при получении первого сигнала.</i>",
                reply_markup=self._get_set_coins_keyboard(),
                parse_mode="HTML"
            )

        @self.dp.message(TGStates.waiting_for_add_symbol)
        async def process_add_symbol(message: Message, state: FSMContext):
            if message.text in {"🔙 Назад", "🔙 Back"}:
                await state.clear()
                status = "⏸️ Paused" if self.bot_core.is_paused else "▶️ Running"
                await message.answer(f"<b>Control Panel</b>\nСтатус бота: {status}", reply_markup=self._get_main_keyboard(), parse_mode="HTML")
                return

            raw_text = (message.text or "").strip()
            symbol = raw_text.upper()

            if not re.match(r'^[A-Z0-9]+USDT$', symbol) or len(symbol) < 5:
                await message.answer(
                    "❌ <b>Некорректный тикер!</b>\n"
                    "Тикер должен содержать только латинские буквы/цифры и оканчиваться на USDT (например: <code>DOGEUSDT</code>).\n"
                    "Попробуйте еще раз или нажмите <b>🔙 Назад</b>:",
                    parse_mode="HTML"
                )
                return

            data = await state.get_data()
            selected_dome = data.get("selected_dome")
            if not selected_dome:
                await message.answer("⚠️ Купол стратегии не выбран.", reply_markup=self._get_main_keyboard())
                await state.clear()
                return

            if symbol in self.bot_core.symbols:
                await message.answer(
                    f"⚠️ Монета <b>{symbol}</b> уже активна в системе!",
                    reply_markup=self._get_set_coins_keyboard(),
                    parse_mode="HTML"
                )
                return

            try:
                await self.bot_core.add_symbol(symbol, strategy_id=selected_dome)
                symbols = []
                if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
                    active_strats = self.bot_core.strategy_registry.get_active_strategies()
                    if selected_dome in active_strats:
                        symbols = list(active_strats[selected_dome].symbols)
                symbols_str = ", ".join(sorted(symbols)) if symbols else symbol

                await message.answer(
                    f"✅ <b>Монета {symbol} успешно добавлена в купол <code>{selected_dome}</code>!</b>\n\n"
                    f"⚡️ Рантайм инициализирован. Монета вступит в работу на 1-м сигнале.\n"
                    f"🪙 <b>Текущие монеты купола:</b> {symbols_str}",
                    reply_markup=self._get_set_coins_keyboard(),
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Error adding symbol {symbol} to {selected_dome}: {e}")
                await message.answer(f"❌ Ошибка добавления монеты: {e}", reply_markup=self._get_set_coins_keyboard())

            await state.set_state(None)
            await state.update_data(selected_dome=selected_dome)

        async def _ask_strategy_for_symbol(msg_or_cb, state: FSMContext, symbol: str, edit_msg: bool = False):
            app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
            active_strategies = app_data.get("active_strategies", {})

            buttons = []
            for sid, sdata in active_strategies.items():
                if not sdata.get("enabled", False):
                    continue
                meta = self._get_strategy_meta(sid)
                s_name = meta.get("name", sid)
                buttons.append([InlineKeyboardButton(
                    text=f"🎯 {s_name[:38]}",
                    callback_data=f"add_pick_strat_{symbol}_{sid}"
                )])

            buttons.append([InlineKeyboardButton(text="🌐 Выбрать другой универсум...", callback_data=f"add_catalog_{symbol}")])
            buttons.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="add_cancel")])

            text = (
                f"🪙 Добавление монеты: <b>{symbol}</b>\n\n"
                f"🎯 <b>Выберите стратегию, под которую назначить монету:</b>\n"
                f"<i>Каждая монета торгуется по правилам и оверрайдам выбранной стратегии (сетка, тейк-профиты, плечо, Super Grid).</i>"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            if edit_msg and isinstance(msg_or_cb, CallbackQuery):
                await msg_or_cb.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
            elif isinstance(msg_or_cb, CallbackQuery):
                await msg_or_cb.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
            else:
                await msg_or_cb.answer(text, reply_markup=keyboard, parse_mode="HTML")
            await state.set_state(TGStates.waiting_for_add_strategy_choice)

        @self.dp.callback_query(F.data.startswith("add_pick_strat_"))
        async def process_add_pick_strat(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            parts = callback.data.split("_")
            if len(parts) < 5:
                return
            symbol = parts[3]
            strat_id = "_".join(parts[4:])

            await state.update_data(pending_symbol=symbol, pending_strat=strat_id)
            meta = self._get_strategy_meta(strat_id)
            sg_str = "✅ Включен" if meta.get("super_grid") else "❌ Выключен"

            text = (
                f"⚙️ <b>Параметры запуска {symbol}:</b>\n"
                f"📌 Стратегия: <b>{meta.get('name', strat_id)}</b>\n"
                f"ID: <code>{strat_id}</code>\n"
                f"📝 <i>{meta.get('description', '')[:120]}...</i>\n\n"
                f"• <b>Размер ордера (Invest):</b> <b>{meta.get('invest_size', 400.0)} USDT</b>\n"
                f"• <b>Кредитное плечо (Leverage):</b> <b>{meta.get('leverage', 20)}x</b>\n"
                f"• <b>Режим Take Profit:</b> <b>{meta.get('tp_summary')}</b>\n"
                f"• <b>Super Grid (волатильность):</b> <b>{sg_str}</b>\n\n"
                f"Запустить сразу с параметрами стратегии или настроить параметры кастомно?"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚀 Запустить с параметрами стратегии", callback_data=f"add_run_{symbol}_{strat_id}")],
                [InlineKeyboardButton(text="⚙️ Настроить параметры (Invest/Lev)", callback_data=f"add_cust_{symbol}_{strat_id}")],
                [InlineKeyboardButton(text="🔙 Назад к стратегиям", callback_data=f"add_back_strats_{symbol}")]
            ])
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

        @self.dp.callback_query(F.data.startswith("add_run_"))
        async def process_add_run(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            parts = callback.data.split("_")
            if len(parts) < 4:
                return
            symbol = parts[2]
            strat_id = "_".join(parts[3:])

            await self.bot_core.add_symbol(symbol, strategy_id=strat_id)
            meta = self._get_strategy_meta(strat_id)

            await callback.message.edit_text(
                f"✅ <b>Монета {symbol} успешно добавлена и запущена!</b>\n\n"
                f"🎯 Стратегия: <b>{meta.get('name', strat_id)}</b> (<code>{strat_id}</code>)\n"
                f"💰 Депозит: <b>{meta.get('invest_size', 400.0)} USDT</b> (Плечо: {meta.get('leverage', 20)}x)\n"
                f"🎯 Тейк-профит: <b>{meta.get('tp_summary')}</b>\n"
                f"📁 Конфиг: <code>CFG/runtime/{strat_id}/{symbol.lower()}.json</code>",
                parse_mode="HTML"
            )
            await callback.message.answer(
                "Монета активна в торговом пуле.",
                reply_markup=self._get_set_coins_keyboard()
            )
            await state.clear()

        @self.dp.callback_query(F.data.startswith("add_back_strats_"))
        async def process_add_back_strats(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            symbol = callback.data.replace("add_back_strats_", "")
            await _ask_strategy_for_symbol(callback, state, symbol, edit_msg=True)

        @self.dp.callback_query(F.data == "add_cancel")
        async def process_add_cancel(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            await callback.message.delete()
            await state.clear()

        @self.dp.callback_query(F.data.startswith("add_cust_"))
        async def process_add_cust(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            parts = callback.data.split("_")
            symbol = parts[2]
            strat_id = "_".join(parts[3:])
            await state.update_data(pending_symbol=symbol, pending_strat=strat_id)
            meta = self._get_strategy_meta(strat_id)
            
            await callback.message.answer(
                f"⚙️ <b>Кастомная настройка {symbol} под {strat_id}</b>\n\n"
                f"Текущий базовый Invest Size: <b>{meta.get('invest_size', 400.0)} USDT</b>.\n"
                f"Введите желаемый размер инвестиции (USDT) для каждого направления (или отправьте 0 чтобы оставить {meta.get('invest_size', 400.0)}):",
                reply_markup=self._get_back_keyboard(),
                parse_mode="HTML"
            )
            await state.set_state(TGStates.waiting_for_add_custom_invest)

        @self.dp.message(TGStates.waiting_for_add_custom_invest)
        async def process_add_custom_invest_input(message: Message, state: FSMContext):
            if message.text == "🔙 Back":
                await state.clear()
                return await on_set_coins(message, state)
            try:
                val = float(message.text.strip().replace(',', '.'))
                data = await state.get_data()
                symbol = data.get("pending_symbol")
                strat_id = data.get("pending_strat")
                meta = self._get_strategy_meta(strat_id)
                invest = val if val > 0 else meta.get("invest_size", 400.0)
                await state.update_data(custom_invest=invest)

                await message.answer(
                    f"✅ Invest Size: <b>{invest} USDT</b>.\n\n"
                    f"Текущее плечо стратегии: <b>{meta.get('leverage', 20)}x</b>.\n"
                    f"Введите кредитное плечо (Leverage от 1 до 50) или отправьте 0 чтобы оставить {meta.get('leverage', 20)}x:",
                    reply_markup=self._get_back_keyboard(),
                    parse_mode="HTML"
                )
                await state.set_state(TGStates.waiting_for_add_custom_lev)
            except ValueError:
                await message.answer("❌ Введите корректное число (например, <code>300</code>):", parse_mode="HTML")

        @self.dp.message(TGStates.waiting_for_add_custom_lev)
        async def process_add_custom_lev_input(message: Message, state: FSMContext):
            if message.text == "🔙 Back":
                await state.clear()
                return await on_set_coins(message, state)
            try:
                val = int(message.text.strip())
                data = await state.get_data()
                symbol = data.get("pending_symbol")
                strat_id = data.get("pending_strat")
                meta = self._get_strategy_meta(strat_id)
                lev = val if 1 <= val <= 50 else meta.get("leverage", 20)
                invest = data.get("custom_invest", meta.get("invest_size", 400.0))

                await self.bot_core.add_symbol(symbol, strategy_id=strat_id)

                rt_path = self.template_manager.get_runtime_path(symbol, strat_id)
                if rt_path.exists():
                    rt_data = Utils.read_json_file(rt_path) or {}
                    for side in ("LONG", "SHORT"):
                        if side in rt_data:
                            rt_data[side]["invest_size"] = invest
                            rt_data[side]["leverage"] = lev
                    Utils.write_json_file(rt_path, rt_data)
                    if symbol in self.bot_core.runtime_configs:
                        for side in ("LONG", "SHORT"):
                            if side in self.bot_core.runtime_configs[symbol]:
                                self.bot_core.runtime_configs[symbol][side]["invest_size"] = invest
                                self.bot_core.runtime_configs[symbol][side]["leverage"] = lev

                await message.answer(
                    f"✅ <b>Монета {symbol} успешно настроена и запущена!</b>\n\n"
                    f"🎯 Стратегия: <b>{meta.get('name', strat_id)}</b> (<code>{strat_id}</code>)\n"
                    f"💰 Кастомный Invest: <b>{invest} USDT</b>\n"
                    f"⚡ Кастомное Плечо: <b>{lev}x</b>\n"
                    f"🎯 Тейк-профит: <b>{meta.get('tp_summary')}</b>\n"
                    f"📁 Конфиг: <code>CFG/runtime/{strat_id}/{symbol.lower()}.json</code>",
                    reply_markup=self._get_set_coins_keyboard(),
                    parse_mode="HTML"
                )
                await state.clear()
            except ValueError:
                await message.answer("❌ Введите корректное целое число (от 1 до 50):", parse_mode="HTML")

        @self.dp.callback_query(F.data.startswith("add_catalog_"))
        async def process_add_catalog(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            symbol = callback.data.replace("add_catalog_", "")
            if hasattr(self, "bot") and getattr(self.bot, "strategy_registry", None):
                univ_data = self.bot.strategy_registry.universes_db
            else:
                univ_dir = DATA_DIR / "universes"
                if univ_dir.exists():
                    univ_data = {jf.stem: Utils.read_json_file(jf) for jf in sorted(univ_dir.glob("*.json"))}
                else:
                    univ_data = Utils.read_json_file(DATA_DIR / "universes.json") or {}

            buttons = []
            for uid, udata in list(univ_data.items())[:12]:
                u_name = udata.get("name", uid)
                buttons.append([InlineKeyboardButton(
                    text=f"🌐 {u_name[:35]}",
                    callback_data=f"add_pick_strat_{symbol}_{uid}"
                )])

            buttons.append([InlineKeyboardButton(text="🔙 Назад к активным", callback_data=f"add_back_strats_{symbol}")])
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            await callback.message.edit_text(
                f"🌐 <b>Каталог стратегий (Universes) для {symbol}</b>\n\n"
                f"Выберите любую стратегию из базы для активации и запуска:",
                reply_markup=keyboard,
                parse_mode="HTML"
            )

        # =========================================================
        # УДАЛЕНИЕ МОНЕТЫ ИЗ ВЫБРАННОГО КУПОЛА
        # =========================================================
        @self.dp.message(F.text.in_({"🗑️ Удалить монету", "🗑️ Del"}))
        async def on_del_btn(message: Message, state: FSMContext):
            data = await state.get_data()
            selected_dome = data.get("selected_dome")
            if not selected_dome:
                return await on_set_coins(message, state)

            symbols = []
            if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
                active_strats = self.bot_core.strategy_registry.get_active_strategies()
                if selected_dome in active_strats:
                    symbols = list(active_strats[selected_dome].symbols)
            else:
                app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
                symbols = list(app_data.get("active_strategies", {}).get(selected_dome, {}).get("symbols", []))

            if not symbols:
                await message.answer(
                    f"⚠️ В куполе <code>{selected_dome}</code> сейчас нет монет для удаления.",
                    reply_markup=self._get_set_coins_keyboard(),
                    parse_mode="HTML"
                )
                return

            buttons = []
            row = []
            for sym in sorted(symbols):
                row.append(InlineKeyboardButton(text=f"❌ {sym}", callback_data=f"del_dome_coin:{selected_dome}:{sym}"))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
            buttons.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="del_dome_coin_cancel")])

            await message.answer(
                f"🗑️ <b>Удаление монеты из купола</b> <code>{selected_dome}</code>\n\n"
                f"<i>Нажмите на монету для удаления:</i>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                parse_mode="HTML"
            )

        @self.dp.callback_query(F.data.startswith("del_dome_coin:"))
        async def process_del_dome_coin(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            parts = callback.data.split(":")
            dome_id = parts[1]
            sym = parts[2]
            try:
                await self.bot_core.delete_symbol(sym)
                await callback.message.edit_text(
                    f"✅ Монета <b>{sym}</b> успешно удалена из купола <code>{dome_id}</code>.",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Error deleting symbol {sym}: {e}")
                await callback.message.edit_text(f"❌ Ошибка удаления монеты {sym}: {e}")

        @self.dp.callback_query(F.data == "del_dome_coin_cancel")
        async def process_del_dome_coin_cancel(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            try:
                await callback.message.delete()
            except Exception:
                pass

        @self.dp.message(F.text.in_({"🔙 Назад", "🔙 Back"}))
        async def on_back_from_coins(message: Message, state: FSMContext):
            await state.clear()
            status = "⏸️ Paused" if self.bot_core.is_paused else "▶️ Running"
            text = f"<b>Control Panel</b>\nСтатус бота: {status}"
            await message.answer(text, reply_markup=self._get_main_keyboard(), parse_mode="HTML")

        # =========================================================
        # EDIT SYMBOL WITH STRATEGY GROUPING & REASSIGNMENT
        # =========================================================
        @self.dp.message(F.text == "✏️ Edit")
        async def on_edit_btn(message: Message, state: FSMContext):
            await state.clear()
            await _show_edit_coin_list(message)

        @self.dp.callback_query(F.data == "edit_coin_list")
        async def process_edit_coin_list(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            await _show_edit_coin_list(callback.message, edit=True)

        async def _show_edit_coin_list(message: Message, edit=False):
            active_coins = message.bot.get("bot_core", self.bot_core).symbols if not hasattr(self, 'bot_core') else self.bot_core.symbols
            if not active_coins:
                text = "Нет активных монет для редактирования."
                if edit:
                    await message.edit_text(text)
                else:
                    await message.answer(text)
                return

            strat_registry = getattr(self.bot_core, "strategy_registry", None)
            app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
            active_strategies = app_data.get("active_strategies", {})

            coins_by_strat = {}
            for coin in active_coins:
                sid = strat_registry.get_strategy_id_for_symbol(coin) if strat_registry else None
                if not sid:
                    for s_name, s_cfg in active_strategies.items():
                        if coin in s_cfg.get("symbols", []):
                            sid = s_name
                            break
                sid = sid or "u0_base_classic"
                coins_by_strat.setdefault(sid, []).append(coin)

            text_lines = ["⚙️ <b>Активные монеты по стратегиям:</b>\n"]
            buttons = []
            row = []

            for sid, c_list in coins_by_strat.items():
                meta = self._get_strategy_meta(sid)
                s_title = meta.get("name", sid)
                short_sid = sid.replace("u0_base_classic_", "").replace("u9_omni_", "")
                text_lines.append(f"🎯 <b>{s_title}</b> (<code>{sid}</code>):")
                for c in sorted(c_list):
                    text_lines.append(f"  • <code>{c}</code>")
                    btn_text = f"🪙 {c} [{short_sid[:7]}]"
                    row.append(InlineKeyboardButton(text=btn_text, callback_data=f"edit_coin_{c}"))
                    if len(row) == 2:
                        buttons.append(row)
                        row = []
                text_lines.append("")

            if row:
                buttons.append(row)
            buttons.append([InlineKeyboardButton(text="🔙 Close", callback_data="edit_side_cancel")])

            text_lines.append("<i>Выберите монету для настройки параметров или смены стратегии:</i>")
            text = "\n".join(text_lines)

            if edit:
                await message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
            else:
                await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

        @self.dp.callback_query(F.data.startswith("edit_coin_") & ~F.data.startswith("edit_coin_list"))
        async def process_edit_coin_select(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            symbol = callback.data.replace("edit_coin_", "")

            strat_registry = getattr(self.bot_core, "strategy_registry", None)
            strat_id = strat_registry.get_strategy_id_for_symbol(symbol) if strat_registry else None
            meta = self._get_strategy_meta(strat_id) if strat_id else {"name": "Не назначена"}
            strat_name = meta.get("name", strat_id or "—")

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="🟢 LONG", callback_data=f"edit_side_{symbol}_LONG"),
                    InlineKeyboardButton(text="🔴 SHORT", callback_data=f"edit_side_{symbol}_SHORT")
                ],
                [
                    InlineKeyboardButton(text="🔄 Сменить стратегию", callback_data=f"reassign_strat_{symbol}")
                ],
                [InlineKeyboardButton(text="🔙 Back", callback_data="edit_coin_list")]
            ])
            text = (
                f"⚙️ <b>{symbol}</b>\n"
                f"📌 Стратегия: <b>{strat_name}</b>\n"
                f"ID: <code>{strat_id}</code>\n\n"
                f"Выберите действие:"
            )
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

        @self.dp.callback_query(F.data.startswith("reassign_strat_"))
        async def process_reassign_strat(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            symbol = callback.data.replace("reassign_strat_", "")

            app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
            active_strategies = app_data.get("active_strategies", {})
            strat_registry = getattr(self.bot_core, "strategy_registry", None)
            current_strat = strat_registry.get_strategy_id_for_symbol(symbol) if strat_registry else None

            buttons = []
            for sid, sdata in active_strategies.items():
                if sid == current_strat:
                    continue
                meta = self._get_strategy_meta(sid)
                s_name = meta.get("name", sid)
                buttons.append([InlineKeyboardButton(
                    text=f"🎯 {s_name[:35]}",
                    callback_data=f"do_reassign_{symbol}_{sid}"
                )])

            buttons.append([InlineKeyboardButton(text="🔙 Назад к монете", callback_data=f"edit_coin_{symbol}")])
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            await callback.message.edit_text(
                f"🔄 <b>Перевод {symbol} в другую стратегию</b>\n\n"
                f"Текущая стратегия: <code>{current_strat}</code>\n"
                f"Выберите новую стратегию для назначения:",
                reply_markup=keyboard,
                parse_mode="HTML"
            )

        @self.dp.callback_query(F.data.startswith("do_reassign_"))
        async def process_do_reassign(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            parts = callback.data.split("_")
            if len(parts) < 4:
                return
            symbol = parts[2]
            new_strat_id = "_".join(parts[3:])

            app_path = DATA_DIR / "app.json"
            app_data = Utils.read_json_file(app_path) or {}
            active_strategies = app_data.get("active_strategies", {})

            for sid, sdata in active_strategies.items():
                if "symbols" in sdata and symbol in sdata["symbols"]:
                    sdata["symbols"].remove(symbol)

            if new_strat_id in active_strategies:
                active_strategies[new_strat_id].setdefault("symbols", []).append(symbol)
                active_strategies[new_strat_id]["enabled"] = True
                Utils.write_json_file(app_path, app_data)

            if hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
                self.bot_core.strategy_registry.reload(active_strategies)

            try:
                from CORE.STRATEGY.strategy_applicator import StrategyApplicator
                strat_cfg = self.bot_core.strategy_registry.get_strategy_for_symbol(symbol)
                StrategyApplicator().build_runtime_for_symbol(symbol, strat_cfg)
            except Exception as e:
                logger.warning(f"StrategyApplicator rebuild error: {e}")

            self.bot_core.runtime_manager.load_initial_caches(self.bot_core.symbols)
            self.bot_core.runtime_configs = self.bot_core.runtime_manager.caches
            self.bot_core.runtime_manager.populate_fsm_from_cache(self.bot_core.fsm_states)

            meta = self._get_strategy_meta(new_strat_id)
            await callback.message.edit_text(
                f"✅ Монета <b>{symbol}</b> успешно переведена в стратегию <b>{meta.get('name', new_strat_id)}</b> (<code>{new_strat_id}</code>)!\n"
                f"Новые настройки сетки и тейков применены.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⚙️ К монете", callback_data=f"edit_coin_{symbol}")],
                    [InlineKeyboardButton(text="📋 К списку монет", callback_data="edit_coin_list")]
                ]),
                parse_mode="HTML"
            )

        @self.dp.callback_query(F.data == "edit_side_cancel")
        async def process_edit_side_cancel(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            await callback.message.delete()

        @self.dp.callback_query(F.data.startswith("edit_side_"))
        async def process_edit_side(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            data_parts = callback.data.split("_")
            if len(data_parts) != 4:
                return
            symbol = data_parts[2]
            side = data_parts[3]

            runtime_path = self.template_manager.get_runtime_path(symbol)
            if not runtime_path.exists():
                await callback.message.answer(f"❌ Конфиг {symbol} не найден.")
                return

            try:
                rt_data = json.loads(runtime_path.read_text(encoding="utf-8"))
                side_data = rt_data.get(side, {})
                
                en = side_data.get("enable", False)
                sz = side_data.get("invest_size", 0)
                lev = side_data.get("leverage", 0)
                
                # Fetch all grid levels for display
                grid_cfg = side_data.get("grid", {})
                grid_lines = ["📊 <b>Grid (Avg):</b>"]
                for k in sorted(grid_cfg.keys(), key=lambda x: int(x) if x.isdigit() else x):
                    lvl_data = grid_cfg[k]
                    if not isinstance(lvl_data, dict):
                        continue
                    en_lvl = lvl_data.get("enabled", True)
                    vol = lvl_data.get('volume')
                    ind = lvl_data.get('indent')
                    si = lvl_data.get('super_indent')
                    if en_lvl is False or (ind is None and vol is None):
                        grid_lines.append(f"  • Lvl {k}: [Отключен/Резерв]")
                    else:
                        vol_str = f"{vol}" if vol is not None else "-"
                        ind_str = f"{ind}" if ind is not None else "-"
                        si_text = f" (Super: {si})" if si is not None else ""
                        grid_lines.append(f"  • Lvl {k}: Vol <b>{vol_str}</b> | Indent <b>{ind_str}</b>{si_text}")
                
                # Fetch all TP levels for display
                tp_cfg = side_data.get("tp_map", {})
                tp_lines = ["🎯 <b>Take Profits (TP):</b>"]
                for k in sorted(tp_cfg.keys(), key=lambda x: int(x) if x.isdigit() else x):
                    lvl_data = tp_cfg[k]
                    if not isinstance(lvl_data, dict):
                        continue
                    ind = lvl_data.get('indent')
                    fb = lvl_data.get('fallback_indent')
                    ind_str = f"{ind}" if ind is not None else "-"
                    fb_str = f"{fb}" if fb is not None else "-"
                    tp_lines.append(f"  • Lvl {k}: Indent <b>{ind_str}</b> | Fallback <b>{fb_str}</b>")

                tp_purpose = side_data.get("tp_purpose", "self")
                
                msg_text = (
                    f"⚙️ <b>{symbol} - {side}</b>\n"
                    f"Status: {'✅ On' if en else '❌ Off'}\n\n"
                    f"💰 Invest Size: <b>{sz}</b> USDT (Lev: {lev}x)\n"
                    f"🔄 TP Purpose: <b>{tp_purpose}</b>\n\n"
                    + "\n".join(grid_lines) + "\n\n"
                    + "\n".join(tp_lines)
                )
                
                toggle_text = "⏸ Отключить (ON)" if en else "▶️ Включить (OFF)"
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=toggle_text, callback_data=f"edit_act_toggle_{symbol}_{side}")],
                    [InlineKeyboardButton(text="💰 Edit Invest Size", callback_data=f"edit_act_size_{symbol}_{side}")],
                    [InlineKeyboardButton(text="📊 Edit Set Avg", callback_data=f"edit_act_avg_{symbol}_{side}")],
                    [InlineKeyboardButton(text="🎯 Edit Set TP", callback_data=f"edit_act_tp_{symbol}_{side}")],
                    [InlineKeyboardButton(text=f"🔄 Edit TP Purpose", callback_data=f"edit_act_purp_{symbol}_{side}")],
                    [InlineKeyboardButton(text="🔙 Back", callback_data=f"edit_coin_{symbol}")]
                ])
                
                try:
                    await callback.message.edit_text(msg_text, reply_markup=keyboard, parse_mode="HTML")
                except Exception as e:
                    if "message is not modified" not in str(e).lower():
                        await callback.message.answer(msg_text, reply_markup=keyboard, parse_mode="HTML")
            except Exception as e:
                await callback.message.answer(f"Ошибка чтения: {e}")

        async def _apply_instant_update(symbol: str, side: str, callback: CallbackQuery):
            # Instant sync of RAM to HDD and UI refresh
            if hasattr(self.bot_core, 'fsm_states') and hasattr(self.bot_core, 'runtime_manager'):
                await self.bot_core.runtime_manager.sync_with_fsm(self.bot_core.fsm_states, force_save=True)
            # Re-trigger UI refresh
            await process_edit_side(callback, None)

        @self.dp.callback_query(F.data.startswith("edit_act_toggle_"))
        async def process_edit_toggle(callback: CallbackQuery, state: FSMContext):
            data_parts = callback.data.split("_")
            symbol = data_parts[3]
            side = data_parts[4]
            
            runtime_cfg = self.bot_core.runtime_configs.get(symbol)
            if runtime_cfg and side in runtime_cfg:
                current = runtime_cfg[side].get("enable", False)
                runtime_cfg[side]["enable"] = not current
                msg = f"{side} Включен! ✅" if not current else f"{side} Отключен! ❌"
                await callback.answer(msg, show_alert=False)
                await _apply_instant_update(symbol, side, callback)
            else:
                await callback.answer("Runtime config not found in RAM")

        @self.dp.callback_query(F.data.startswith("edit_act_purp_"))
        async def process_edit_purp(callback: CallbackQuery, state: FSMContext):
            data_parts = callback.data.split("_")
            symbol = data_parts[3]
            side = data_parts[4]
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="1. self", callback_data=f"set_purp_{symbol}_{side}_self"),
                    InlineKeyboardButton(text="2. opposite", callback_data=f"set_purp_{symbol}_{side}_opposite"),
                    InlineKeyboardButton(text="3. both", callback_data=f"set_purp_{symbol}_{side}_both")
                ],
                [InlineKeyboardButton(text="🔙 Back", callback_data=f"edit_side_{symbol}_{side}")]
            ])
            await callback.message.edit_text(f"⚙️ <b>{symbol} {side}</b>\nВыберите настройку <b>TP Purpose</b>:\n\n1. self — по своему уровню\n2. opposite — по уровню противоположной стороны\n3. both — по уровню наибольшей просадки", reply_markup=keyboard, parse_mode="HTML")

        @self.dp.callback_query(F.data.startswith("set_purp_"))
        async def process_set_purp(callback: CallbackQuery, state: FSMContext):
            data_parts = callback.data.split("_")
            symbol = data_parts[2]
            side = data_parts[3]
            purpose = data_parts[4]
            
            runtime_cfg = self.bot_core.runtime_configs.get(symbol)
            if runtime_cfg and side in runtime_cfg:
                runtime_cfg[side]["tp_purpose"] = purpose
                fsm_state = self.bot_core.fsm_states.get(symbol, {}).get(side)
                if fsm_state:
                    fsm_state.tp_purpose = purpose
                await callback.answer(f"TP Purpose изменен на {purpose} ✅", show_alert=False)
                await _apply_instant_update(symbol, side, callback)
            else:
                await callback.answer("Runtime config not found in RAM")

        @self.dp.callback_query(F.data.startswith("edit_act_size_"))
        async def process_edit_size(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            data_parts = callback.data.split("_")
            symbol = data_parts[3]
            side = data_parts[4]
            
            await state.update_data(edit_symbol=symbol, edit_side=side)
            await callback.message.answer(f"Введите новый <b>Invest Size</b> (USDT) для {symbol} {side} (например: 15.5):", parse_mode="HTML", reply_markup=self._get_back_keyboard())
            await state.set_state(TGStates.waiting_for_invest_size)

        @self.dp.message(TGStates.waiting_for_invest_size)
        async def handle_invest_size(message: Message, state: FSMContext):
            if message.text == "🔙 Back":
                await state.clear()
                return await on_set_coins(message, state)
                
            try:
                val = float(message.text.replace(',', '.'))
                data = await state.get_data()
                symbol = data['edit_symbol']
                side = data['edit_side']
                
                runtime_cfg = self.bot_core.runtime_configs.get(symbol)
                if runtime_cfg and side in runtime_cfg:
                    runtime_cfg[side]["invest_size"] = val
                    if hasattr(self.bot_core, 'fsm_states') and hasattr(self.bot_core, 'runtime_manager'):
                        await self.bot_core.runtime_manager.sync_with_fsm(self.bot_core.fsm_states, force_save=True)
                    await message.answer(f"✅ Invest Size обновлен до {val}.", reply_markup=self._get_set_coins_keyboard())
                await state.clear()
            except ValueError:
                await message.answer("❌ Некорректное число.")

        @self.dp.callback_query(F.data.startswith("edit_act_avg_"))
        async def process_edit_avg(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            data_parts = callback.data.split("_")
            symbol = data_parts[3]
            side = data_parts[4]
            
            fsm_state = self.bot_core.fsm_states.get(symbol, {}).get(side)
            if not fsm_state:
                await callback.message.answer("❌ FSM state не найден.")
                return
                
            grid = fsm_state.grid
            levels = sorted([int(k) for k in grid.keys()]) if grid else [0]
            
            buttons = []
            row = []
            for lvl in levels:
                is_act = grid.get(str(lvl), {}).get("is_active", False)
                btn_text = f"Уровень {lvl} {'(✅)' if is_act else ''}"
                row.append(InlineKeyboardButton(text=btn_text, callback_data=f"ed_lvl_avg_{symbol}_{side}_{lvl}"))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
            buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data=f"edit_side_{symbol}_{side}")])
            
            await callback.message.edit_text(f"⚙️ <b>{symbol} {side}</b>\nВыберите уровень для редактирования <b>Set Avg</b>:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

        @self.dp.callback_query(F.data.startswith("ed_lvl_avg_"))
        async def process_ed_lvl_avg(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            data_parts = callback.data.split("_")
            symbol = data_parts[3]
            side = data_parts[4]
            lvl = data_parts[5]
            
            fsm_state = self.bot_core.fsm_states.get(symbol, {}).get(side)
            is_act = fsm_state.grid.get(str(lvl), {}).get("is_active", False) if fsm_state else False
            warn = "\n\n⚠️ <i>Внимание: Этот уровень уже отработал! Сброс цены не произойдет. Новые настройки применятся только для следующих циклов позиции.</i>" if is_act else ""
            
            grid_cfg = runtime_cfg.get("grid", {}).get(str(lvl), {})
            cur_vol = grid_cfg.get("volume")
            cur_ind = grid_cfg.get("indent")
            
            await state.update_data(edit_symbol=symbol, edit_side=side, edit_lvl=lvl)
            vol_disp = cur_vol if cur_vol is not None else "[Отключен/None]"
            ind_disp = cur_ind if cur_ind is not None else "[Отключен/None]"
            msg = f"Усреднение (Set Avg) для <b>{symbol} {side} | Уровень {lvl}</b>.{warn}\n\n"
            msg += f"Текущие значения:\n🔹 Объем: <b>{vol_disp}</b>\n🔹 Индент: <b>{ind_disp}</b>\n\nВыберите параметр для изменения:"
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔹 Изменить Объем", callback_data=f"ed_prm_avgvol_{symbol}_{side}_{lvl}"),
                    InlineKeyboardButton(text="🔹 Изменить Индент", callback_data=f"ed_prm_avgind_{symbol}_{side}_{lvl}")
                ],
                [InlineKeyboardButton(text="🔙 Back", callback_data=f"edit_act_avg_{symbol}_{side}")]
            ])
            await callback.message.edit_text(msg, parse_mode="HTML", reply_markup=keyboard)

        @self.dp.callback_query(F.data.startswith("ed_prm_avgvol_"))
        async def process_ed_prm_avgvol(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            data_parts = callback.data.split("_")
            symbol = data_parts[3]
            side = data_parts[4]
            lvl = data_parts[5]
            await state.update_data(edit_symbol=symbol, edit_side=side, edit_lvl=lvl, edit_param="avgvol")
            msg = f"Введите новый <b>Объем</b> для уровня {lvl} (число):\n<i>(Система берет модуль числа, знаки не важны)</i>"
            await callback.message.answer(msg, parse_mode="HTML", reply_markup=self._get_back_keyboard())
            await state.set_state(TGStates.waiting_for_avg_params)

        @self.dp.callback_query(F.data.startswith("ed_prm_avgind_"))
        async def process_ed_prm_avgind(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            data_parts = callback.data.split("_")
            symbol = data_parts[3]
            side = data_parts[4]
            lvl = data_parts[5]
            await state.update_data(edit_symbol=symbol, edit_side=side, edit_lvl=lvl, edit_param="avgind")
            msg = f"Введите новый <b>Индент</b> для уровня {lvl} (число):\n<i>(Система берет модуль числа, знаки не важны)</i>"
            await callback.message.answer(msg, parse_mode="HTML", reply_markup=self._get_back_keyboard())
            await state.set_state(TGStates.waiting_for_avg_params)

        @self.dp.message(TGStates.waiting_for_avg_params)
        async def handle_avg_params(message: Message, state: FSMContext):
            if message.text == "🔙 Back":
                await state.clear()
                return await on_set_coins(message, state)
                
            try:
                val = abs(float(message.text.strip()))
                
                data = await state.get_data()
                symbol = data['edit_symbol']
                side = data['edit_side']
                lvl = data['edit_lvl']
                param = data['edit_param']
                
                fsm_state = self.bot_core.fsm_states.get(symbol, {}).get(side)
                runtime_cfg = self.bot_core.runtime_configs.get(symbol, {}).get(side)
                
                if fsm_state and runtime_cfg:
                    if lvl not in fsm_state.grid:
                        fsm_state.grid[lvl] = {}
                    if lvl not in runtime_cfg.get("grid", {}):
                        if "grid" not in runtime_cfg: runtime_cfg["grid"] = {}
                        runtime_cfg["grid"][lvl] = {}
                        
                    old_ind = fsm_state.grid[lvl].get("indent")
                    
                    if param == "avgvol":
                        fsm_state.grid[lvl]["volume"] = val
                        runtime_cfg["grid"][lvl]["volume"] = val
                        await message.answer(f"✅ Объем уровня {lvl} изменен на {val}.", reply_markup=self._get_set_coins_keyboard())
                    elif param == "avgind":
                        val = -val if int(lvl) > 0 else 0.0
                        fsm_state.grid[lvl]["indent"] = val
                        runtime_cfg["grid"][lvl]["indent"] = val
                        await message.answer(f"✅ Индент уровня {lvl} изменен на {val}.", reply_markup=self._get_set_coins_keyboard())
                    
                    if param == "avgind" and old_ind != val:
                        if not fsm_state.grid[lvl].get("is_active"):
                            fsm_state.grid[lvl]["price"] = None
                            runtime_cfg["grid"][lvl]["price"] = None
                        fsm_state.next_avg_price = None
                        
                    if hasattr(self.bot_core, 'fsm_states') and hasattr(self.bot_core, 'runtime_manager'):
                        await self.bot_core.runtime_manager.sync_with_fsm(self.bot_core.fsm_states, force_save=True)
                        
                    if hasattr(self.bot_core, 'volatility_manager') and self.bot_core.volatility_manager.is_running:
                        asyncio.create_task(self.bot_core.volatility_manager.process_all())
                        
                await state.clear()
            except ValueError:
                await message.answer(f"❌ Ошибка: Введите число (например 15.5).")

        @self.dp.callback_query(F.data.startswith("edit_act_tp_"))
        async def process_edit_tp(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            data_parts = callback.data.split("_")
            symbol = data_parts[3]
            side = data_parts[4]
            
            fsm_state = self.bot_core.fsm_states.get(symbol, {}).get(side)
            if not fsm_state:
                await callback.message.answer("❌ FSM state не найден.")
                return
                
            tp_map = fsm_state.tp_map
            levels = sorted([int(k) for k in tp_map.keys()]) if tp_map else [0]
            
            buttons = []
            row = []
            for lvl in levels:
                is_act = tp_map.get(str(lvl), {}).get("is_active", False)
                btn_text = f"Уровень {lvl} {'(✅)' if is_act else ''}"
                row.append(InlineKeyboardButton(text=btn_text, callback_data=f"ed_lvl_tp_{symbol}_{side}_{lvl}"))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
            buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data=f"edit_side_{symbol}_{side}")])
            
            await callback.message.edit_text(f"⚙️ <b>{symbol} {side}</b>\nВыберите уровень для редактирования <b>Set TP</b>:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

        @self.dp.callback_query(F.data.startswith("ed_lvl_tp_"))
        async def process_ed_lvl_tp(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            data_parts = callback.data.split("_")
            symbol = data_parts[3]
            side = data_parts[4]
            lvl = data_parts[5]
            
            fsm_state = self.bot_core.fsm_states.get(symbol, {}).get(side)
            is_act = fsm_state.tp_map.get(str(lvl), {}).get("is_active", False) if fsm_state else False
            warn = "\n\n⚠️ <i>Внимание: Этот тейк-профит уже отработал! Новые настройки применятся только для следующих циклов.</i>" if is_act else ""
            
            runtime_cfg = self.bot_core.runtime_configs.get(symbol, {}).get(side, {})
            tp_cfg = runtime_cfg.get("tp_map", {}).get(str(lvl), {})
            cur_ind = tp_cfg.get("indent")
            cur_fb = tp_cfg.get("fallback_indent")
            
            await state.update_data(edit_symbol=symbol, edit_side=side, edit_lvl=lvl)
            ind_disp = cur_ind if cur_ind is not None else "[None]"
            fb_disp = cur_fb if cur_fb is not None else "[None]"
            msg = f"Установка тейк-профита для <b>{symbol} {side} | Уровень {lvl}</b>.{warn}\n\n"
            msg += f"Текущие значения:\n🔹 Indent: <b>{ind_disp}</b>\n🔹 Fallback: <b>{fb_disp}</b>\n\nВыберите параметр для изменения:"
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔹 Изменить Indent", callback_data=f"ed_prm_tpind_{symbol}_{side}_{lvl}"),
                    InlineKeyboardButton(text="🔹 Изменить Fallback", callback_data=f"ed_prm_tpfb_{symbol}_{side}_{lvl}")
                ],
                [InlineKeyboardButton(text="🔙 Back", callback_data=f"edit_act_tp_{symbol}_{side}")]
            ])
            await callback.message.edit_text(msg, parse_mode="HTML", reply_markup=keyboard)

        @self.dp.callback_query(F.data.startswith("ed_prm_tpind_"))
        async def process_ed_prm_tpind(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            data_parts = callback.data.split("_")
            symbol = data_parts[3]
            side = data_parts[4]
            lvl = data_parts[5]
            await state.update_data(edit_symbol=symbol, edit_side=side, edit_lvl=lvl, edit_param="tpind")
            msg = f"Введите новый <b>Indent</b> для тейк-профита {lvl} (число):\n<i>(Система берет модуль числа, знаки не важны)</i>"
            await callback.message.answer(msg, parse_mode="HTML", reply_markup=self._get_back_keyboard())
            await state.set_state(TGStates.waiting_for_tp_params)

        @self.dp.callback_query(F.data.startswith("ed_prm_tpfb_"))
        async def process_ed_prm_tpfb(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            data_parts = callback.data.split("_")
            symbol = data_parts[3]
            side = data_parts[4]
            lvl = data_parts[5]
            await state.update_data(edit_symbol=symbol, edit_side=side, edit_lvl=lvl, edit_param="tpfb")
            msg = f"Введите новый <b>Fallback Indent</b> для тейк-профита {lvl} (число):\n<i>(Система берет модуль числа, знаки не важны)</i>"
            await callback.message.answer(msg, parse_mode="HTML", reply_markup=self._get_back_keyboard())
            await state.set_state(TGStates.waiting_for_tp_params)

        @self.dp.message(TGStates.waiting_for_tp_params)
        async def handle_tp_params(message: Message, state: FSMContext):
            if message.text == "🔙 Back":
                await state.clear()
                return await on_set_coins(message, state)
                
            try:
                val = abs(float(message.text.strip()))
                
                data = await state.get_data()
                symbol = data['edit_symbol']
                side = data['edit_side']
                lvl = data['edit_lvl']
                param = data['edit_param']
                
                fsm_state = self.bot_core.fsm_states.get(symbol, {}).get(side)
                runtime_cfg = self.bot_core.runtime_configs.get(symbol, {}).get(side)
                
                if fsm_state and runtime_cfg:
                    if lvl not in fsm_state.tp_map:
                        fsm_state.tp_map[lvl] = {}
                    if lvl not in runtime_cfg.get("tp_map", {}):
                        if "tp_map" not in runtime_cfg: runtime_cfg["tp_map"] = {}
                        runtime_cfg["tp_map"][lvl] = {}
                        
                    if param == "tpind":
                        fsm_state.tp_map[lvl]["indent"] = val
                        runtime_cfg["tp_map"][lvl]["indent"] = val
                        await message.answer(f"✅ Indent тейк-профита {lvl} изменен на {val}.", reply_markup=self._get_set_coins_keyboard())
                    elif param == "tpfb":
                        fsm_state.tp_map[lvl]["fallback_indent"] = val
                        runtime_cfg["tp_map"][lvl]["fallback_indent"] = val
                        await message.answer(f"✅ Fallback тейк-профита {lvl} изменен на {val}.", reply_markup=self._get_set_coins_keyboard())
                    
                    if hasattr(self.bot_core, 'fsm_states') and hasattr(self.bot_core, 'runtime_manager'):
                        await self.bot_core.runtime_manager.sync_with_fsm(self.bot_core.fsm_states, force_save=True)
                        
                await state.clear()
            except ValueError:
                await message.answer(f"❌ Ошибка: Введите число (например 0.6).")

        # =========================================================
        # EDIT _BASE TEMPLATE
        # =========================================================
        @self.dp.message(F.text == "📄 _base")
        async def on_base_btn(message: Message, state: FSMContext):
            base_data = Utils.read_json_file(self.template_manager.base_file)
            base_str = json.dumps(base_data, indent=4)
            
            dump_path = os.path.join("logs", "_base_edit.json")
            os.makedirs("logs", exist_ok=True)
            with open(dump_path, "w", encoding="utf-8") as f:
                f.write(base_str)
                
            await message.answer("<b>Текущий базовый шаблон _base.json:</b>\nОтредактируйте файл и отправьте обратно документом (или текстом).", reply_markup=self._get_back_keyboard(), parse_mode="HTML")
            await message.answer_document(FSInputFile(dump_path))
            await state.set_state(TGStates.waiting_for_base_json)

        @self.dp.message(TGStates.waiting_for_base_json)
        async def process_base_json(message: Message, state: FSMContext):
            if message.text and message.text == "🔙 Back":
                return await on_set_coins(message, state)
                
            json_str = ""
            if message.document:
                file = await self.bot.get_file(message.document.file_id)
                out = io.BytesIO()
                await self.bot.download_file(file.file_path, out)
                json_str = out.getvalue().decode('utf-8')
            elif message.text:
                json_str = message.text.strip()
            else:
                await message.answer("❌ Пожалуйста, отправьте текстовое сообщение или .json файл.")
                return
                
            try:
                new_base = json.loads(json_str)
                
                # Валидация шаблона
                for side in ("LONG", "SHORT"):
                    if side in new_base:
                        tp_purpose = new_base[side].get("tp_purpose", "self")
                        strat_id = str(new_base[side].get("strategy_id", ""))
                        grid = new_base[side].get("grid", {})
                        tp_map = new_base[side].get("tp_map", {})
                        is_u10 = strat_id.startswith("u10")
                        if tp_purpose != "neutral" and not is_u10 and grid and tp_map and len(grid) != len(tp_map):
                            await message.answer(f"❌ ОШИБКА КОНФИГУРАЦИИ ({side}): Количество уровней grid ({len(grid)}) не совпадает с tp_map ({len(tp_map)}).\nШаблон НЕ сохранен!")
                            return
                            
                Utils.write_json_file(self.template_manager.base_file, new_base)
                await message.answer("✅ Базовый шаблон успешно обновлен!", reply_markup=self._get_set_coins_keyboard())
                await state.clear()
            except Exception as e:
                await message.answer(f"❌ Ошибка JSON: {e}\nИсправьте и отправьте снова.")

        # =========================================================
        # EDIT Super Grid
        # =========================================================
        # =========================================================
        # EDIT Super Grid (Per Strategy & Global)
        # =========================================================
        @self.dp.message(F.text == "🔧 Super Grid")
        async def on_super_grid_btn(message: Message, state: FSMContext):
            await state.clear()
            app_json_path = DATA_DIR / "app.json"
            app_data = Utils.read_json_file(app_json_path) or {}
            active_strategies = app_data.get("active_strategies", {})

            buttons = []
            for sid, sdata in active_strategies.items():
                if not sdata.get("enabled", False):
                    continue
                meta = self._get_strategy_meta(sid)
                sg_en = meta.get("super_grid", False)
                sg_badge = "✅ ON" if sg_en else "❌ OFF"
                s_name = meta.get("name", sid)
                buttons.append([InlineKeyboardButton(
                    text=f"🔧 {s_name[:30]} [{sg_badge}]",
                    callback_data=f"sg_view_{sid}"
                )])

            buttons.append([InlineKeyboardButton(text="🌐 Глобальный Super Grid (_base)", callback_data="sg_global_edit")])
            buttons.append([InlineKeyboardButton(text="🔙 Закрыть", callback_data="edit_side_cancel")])

            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            await message.answer(
                "🔧 <b>Управление Super Grid (динамическая волатильность)</b>\n\n"
                "Выберите стратегию для детальной настройки параметров Super Grid:",
                reply_markup=keyboard,
                parse_mode="HTML"
            )

        @self.dp.callback_query(F.data.startswith("sg_view_"))
        async def process_sg_view(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            sid = callback.data.replace("sg_view_", "")
            app_json_path = DATA_DIR / "app.json"
            app_data = Utils.read_json_file(app_json_path) or {}
            strat_cfg = app_data.get("active_strategies", {}).get(sid, {})
            sg_cfg = strat_cfg.get("super_grid", {})
            en = sg_cfg.get("enabled", False)
            tf = sg_cfg.get("timeframe", "1w")
            win = sg_cfg.get("window", 12)
            mult = sg_cfg.get("multiplier", 2.0)
            min_v = sg_cfg.get("min_volatility_pct", 15.0)

            toggle_text = "⏸ Отключить Super Grid" if en else "▶️ Включить Super Grid"
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=toggle_text, callback_data=f"sg_toggle_{sid}")],
                [InlineKeyboardButton(text="✏️ Редактировать JSON", callback_data=f"sg_edit_json_{sid}")],
                [InlineKeyboardButton(text="🔙 Назад к списку", callback_data="sg_back_list")]
            ])

            syms = ", ".join(strat_cfg.get("symbols", [])) or "нет"
            text = (
                f"🔧 <b>Super Grid для стратегии:</b>\n"
                f"ID: <code>{sid}</code>\n\n"
                f"• Статус: <b>{'✅ Включен' if en else '❌ Отключен'}</b>\n"
                f"• Timeframe: <b>{tf}</b>\n"
                f"• Window: <b>{win} баров</b>\n"
                f"• Multiplier: <b>x{mult}</b>\n"
                f"• Min Volatility: <b>{min_v}%</b>\n\n"
                f"Монеты под этой стратегией: <code>{syms}</code>"
            )
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

        @self.dp.callback_query(F.data.startswith("sg_toggle_"))
        async def process_sg_toggle(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            sid = callback.data.replace("sg_toggle_", "")
            app_json_path = DATA_DIR / "app.json"
            app_data = Utils.read_json_file(app_json_path) or {}
            strat_cfg = app_data.get("active_strategies", {}).get(sid, {})
            if "super_grid" not in strat_cfg:
                strat_cfg["super_grid"] = {
                    "enabled": True,
                    "timeframe": "1w",
                    "window": 12,
                    "multiplier": 2.0,
                    "min_volatility_pct": 15.0,
                    "max_volatility_pct": None,
                    "update_interval_hours": 24
                }
            current = strat_cfg["super_grid"].get("enabled", False)
            strat_cfg["super_grid"]["enabled"] = not current
            Utils.write_json_file(app_json_path, app_data)

            if hasattr(self.bot_core, 'volatility_manager') and self.bot_core.volatility_manager.is_running:
                asyncio.create_task(self.bot_core.volatility_manager.process_all())

            callback.data = f"sg_view_{sid}"
            await process_sg_view(callback, state)

        @self.dp.callback_query(F.data == "sg_back_list")
        async def process_sg_back_list(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            app_json_path = DATA_DIR / "app.json"
            app_data = Utils.read_json_file(app_json_path) or {}
            active_strategies = app_data.get("active_strategies", {})

            buttons = []
            for sid, sdata in active_strategies.items():
                if not sdata.get("enabled", False):
                    continue
                meta = self._get_strategy_meta(sid)
                sg_en = meta.get("super_grid", False)
                sg_badge = "✅ ON" if sg_en else "❌ OFF"
                s_name = meta.get("name", sid)
                buttons.append([InlineKeyboardButton(
                    text=f"🔧 {s_name[:30]} [{sg_badge}]",
                    callback_data=f"sg_view_{sid}"
                )])

            buttons.append([InlineKeyboardButton(text="🌐 Глобальный Super Grid (_base)", callback_data="sg_global_edit")])
            buttons.append([InlineKeyboardButton(text="🔙 Закрыть", callback_data="edit_side_cancel")])
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            await callback.message.edit_text(
                "🔧 <b>Управление Super Grid (динамическая волатильность)</b>\n\n"
                "Выберите стратегию для детальной настройки параметров Super Grid:",
                reply_markup=keyboard,
                parse_mode="HTML"
            )

        @self.dp.callback_query(F.data == "sg_global_edit")
        async def process_sg_global_edit(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            app_json_path = DATA_DIR / "app.json"
            app_data = Utils.read_json_file(app_json_path) or {}
            if "super_grid" not in app_data:
                app_data["super_grid"] = {
                    "enabled": True,
                    "timeframe": "1w",
                    "window": 12,
                    "multiplier": 2.0,
                    "min_volatility_pct": 15.0,
                    "max_volatility_pct": None,
                    "update_interval_hours": 24
                }
            super_grid_str = json.dumps(app_data["super_grid"], indent=4)
            dump_path = os.path.join("logs", "Super Grid_edit.json")
            os.makedirs("logs", exist_ok=True)
            with open(dump_path, "w", encoding="utf-8") as f:
                f.write(super_grid_str)
            await callback.message.answer("<b>Глобальные настройки Super Grid:</b>\nОтредактируйте файл и отправьте обратно документом (или текстом).", reply_markup=self._get_back_keyboard(), parse_mode="HTML")
            await callback.message.answer_document(FSInputFile(dump_path))
            await state.set_state(TGStates.waiting_for_super_grid_json)

        @self.dp.callback_query(F.data.startswith("sg_edit_json_"))
        async def process_sg_edit_json(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            sid = callback.data.replace("sg_edit_json_", "")
            app_json_path = DATA_DIR / "app.json"
            app_data = Utils.read_json_file(app_json_path) or {}
            strat_cfg = app_data.get("active_strategies", {}).get(sid, {})
            sg_cfg = strat_cfg.get("super_grid", {})
            dump_path = os.path.join("logs", f"Super_Grid_{sid}.json")
            os.makedirs("logs", exist_ok=True)
            with open(dump_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(sg_cfg, indent=4))
            await state.update_data(editing_sg_strat=sid)
            await callback.message.answer(
                f"<b>Настройки Super Grid для {sid}:</b>\nОтредактируйте файл и отправьте обратно документом или текстом.",
                reply_markup=self._get_back_keyboard(),
                parse_mode="HTML"
            )
            await callback.message.answer_document(FSInputFile(dump_path))
            await state.set_state(TGStates.waiting_for_strat_super_grid_json)

        @self.dp.message(TGStates.waiting_for_strat_super_grid_json)
        async def process_strat_super_grid_json(message: Message, state: FSMContext):
            if message.text and message.text == "🔙 Back":
                await state.clear()
                return await on_super_grid_btn(message, state)
            json_str = ""
            if message.document:
                file = await self.bot.get_file(message.document.file_id)
                out = io.BytesIO()
                await self.bot.download_file(file.file_path, out)
                json_str = out.getvalue().decode('utf-8')
            elif message.text:
                json_str = message.text.strip()
            else:
                await message.answer("❌ Отправьте текст или .json файл.")
                return
            try:
                new_sg = json.loads(json_str)
                data = await state.get_data()
                sid = data.get("editing_sg_strat")
                app_path = DATA_DIR / "app.json"
                app_data = Utils.read_json_file(app_path) or {}
                if sid in app_data.get("active_strategies", {}):
                    app_data["active_strategies"][sid]["super_grid"] = new_sg
                    Utils.write_json_file(app_path, app_data)
                    if hasattr(self.bot_core, 'volatility_manager') and self.bot_core.volatility_manager.is_running:
                        asyncio.create_task(self.bot_core.volatility_manager.process_all())
                    await message.answer(f"✅ Настройки Super Grid для {sid} успешно обновлены!", reply_markup=self._get_main_keyboard())
                    await state.clear()
                else:
                    await message.answer(f"❌ Стратегия {sid} не найдена в active_strategies.")
            except Exception as e:
                await message.answer(f"❌ Ошибка JSON: {e}\nИсправьте и отправьте снова.")

        # =========================================================
        # /selector - COIN SELECTION SERVICE (ON-DEMAND ROTATION)
        # =========================================================
        @self.dp.message(Command("selector"))
        @self.dp.message(F.text == "🎯 Selector")
        async def on_selector_cmd(message: Message, state: FSMContext):
            await state.clear()
            from CORE.SELECTOR.coin_selection_service import CoinSelectionService
            _sel_svc = CoinSelectionService()
            target_count = _sel_svc.get_configured_target_count()
            configured_type = _sel_svc.get_configured_selector_type()
            text = (
                f"🎯 <b>Coin Selection Service (On-Demand)</b>\n\n"
                f"⚙️ <b>Текущий конфиг app.json:</b>\n"
                f"• Источник: <code>{configured_type}</code>\n"
                f"• Целевой срез: <code>{target_count}</code> монет\n\n"
                f"Выберите алгоритм для сканирования и получения актуального среза:"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="⚡ Alpha Selector (Wiggle & NATR)", callback_data="cb_selector_run:alpha_coin_selector")
                ],
                [
                    InlineKeyboardButton(text="📡 Samsonov Funnel (Радар Полигона)", callback_data="cb_selector_run:samsonov_funnel")
                ],
                [
                    InlineKeyboardButton(text="⚙️ По умолчанию из конфига", callback_data=f"cb_selector_run:{configured_type}")
                ]
            ])
            await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

        @self.dp.callback_query(F.data.startswith("cb_selector_run:"))
        async def process_cb_selector_run(callback: CallbackQuery, state: FSMContext):
            selector_type = callback.data.split(":", 1)[1]
            await callback.answer("Запускаю отбор монет...")
            status_msg = await callback.message.answer(f"⏳ Сканирование ({selector_type}), пожалуйста подождите...")

            try:
                from CORE.SELECTOR.coin_selection_service import CoinSelectionService
                service = CoinSelectionService(bot_core=self.bot_core)
                target_count = service.get_configured_target_count()
                results = await service.get_top_coins(selector_type=selector_type, target_count=target_count)

                if not results:
                    await status_msg.edit_text(f"⚠️ Селектор <code>{selector_type}</code> не вернул подходящих монет под текущие фильтры.")
                    return

                selected_symbols = [x["symbol"] for x in results]
                self._last_selector_results = selected_symbols
                self._last_selector_type = selector_type

                report_text = service.format_telegram_report(results, selector_type, target_count)

                inline_buttons = [
                    [InlineKeyboardButton(text="🔄 Пересканировать", callback_data=f"cb_selector_run:{selector_type}")]
                ]

                keyboard = InlineKeyboardMarkup(inline_keyboard=inline_buttons)
                await status_msg.edit_text(report_text, reply_markup=keyboard, parse_mode="HTML")

            except Exception as e:
                logger.error(f"Error in selector run: {e}", exc_info=True)
                await status_msg.edit_text(f"❌ Ошибка при выполнении селектора: {e}")

        @self.dp.callback_query(F.data.startswith("cb_selector_apply:"))
        async def process_cb_selector_apply(callback: CallbackQuery, state: FSMContext):
            await callback.answer(
                "ℹ️ Прямое применение монет из селектора отключено в целях безопасности баланса.\n"
                "Селектор носит сугубо информационный характер. Для добавления монет используйте меню 'Set Coins' или команды стратегий.",
                show_alert=True
            )

        # =========================================================
        # /sonnik - VOLATILITY SCANNER
        # =========================================================
        @self.dp.message(Command("sonnik"))
        async def on_sonnik_cmd(message: Message, state: FSMContext):
            await state.clear()
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Запустить Скринер волатильности", callback_data="cb_run_scanner")],
                [InlineKeyboardButton(text="⚙️ Настройки Скринера", callback_data="cb_edit_scanner_config")]
            ])
            await message.answer("<b>Сонник (Сканер волатильности)</b>\nВыберите действие:", reply_markup=keyboard, parse_mode="HTML")

        @self.dp.callback_query(F.data == "cb_run_scanner")
        async def process_cb_run_scanner(callback: CallbackQuery, state: FSMContext):
            if getattr(self, '_scanner_running', False):
                await callback.answer("Команда уже запущена дождитесь результата", show_alert=True)
                return
            self._scanner_running = True
            try:
                await callback.answer("Запускаю сканирование... Это займет некоторое время.")
                msg = await callback.message.answer("⏳ Сканирование волатильности запущено, пожалуйста подождите...")
                output_path = DATA_DIR / "volatile_symbols.json"
                if output_path.exists():
                    try:
                        os.remove(output_path)
                    except:
                        pass
                        
                # Запускаем напрямую в текущем процессе с перезагрузкой модуля
                import importlib
                import CORE.ADVANCED.volatility_scanner as vs
                importlib.reload(vs)
                scanner = vs.VolatilityScanner(client=self.bot_core.client)
                await scanner.scan()
                
                output_path = DATA_DIR / "volatile_symbols.json"
                txt_output_path = DATA_DIR / "volatile_symbols.txt"
                if output_path.exists():
                    try:
                        with open(output_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        
                        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        lines = [f"Дата: {now_str}"]
                        lines.append(f"Количество монет: {len(data)}")
                        
                        coin_list = [item["symbol"] for item in data]
                        lines.append("")
                        lines.append("JSON Symbols List (для app.json):")
                        lines.append(json.dumps(coin_list, ensure_ascii=False))
                        lines.append("")
                        lines.append("JSON Symbols List (развернутый):")
                        lines.append(json.dumps(coin_list, indent=4, ensure_ascii=False))
                        lines.append("\nДетализация:")
                        lines.append("_____")
                        
                        for item in data:
                            lines.append(f"Символ: {item['symbol']}")
                            lines.append(f"Волатильность (1w ATR): {item.get('volatility', 0.0)}% (свечей: {item.get('candles', 0)})")
                            if item.get("rsi") is not None:
                                lines.append(f"RSI (4h): {item['rsi']}")
                            if item.get("bb_width_pct") is not None:
                                lines.append(f"BB Width (4h): {item['bb_width_pct']}%")
                            if item.get("bb_center_dev_pct") is not None:
                                lines.append(f"BB Center Dev: {item['bb_center_dev_pct']}% (Middle: {item.get('middle_bb')}, Price: {item.get('current_price')})")
                            lines.append("_____")
                            
                        with open(txt_output_path, "w", encoding="utf-8") as f:
                            f.write("\n".join(lines))
                        
                        await msg.delete()
                        await callback.message.answer_document(FSInputFile(txt_output_path), caption="✅ Сканирование завершено. Результаты в файле.")
                    except Exception as e:
                        logger.error(f"Error formatting scanner output: {e}")
                        await msg.edit_text(f"❌ Ошибка форматирования результатов: {e}")
                else:
                    await msg.edit_text("❌ Ошибка сканирования (файл не создан).")
                    
            except Exception as e:
                logger.error(f"Error running scanner: {e}")
                await msg.edit_text(f"❌ Системная ошибка при запуске сканера: {e}")
            finally:
                self._scanner_running = False

        @self.dp.callback_query(F.data == "cb_edit_scanner_config")
        async def process_cb_edit_scanner_config(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            
            config_path = DATA_DIR / "app.json"
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
            if config_path.exists():
                try:
                    data = Utils.read_json_file(config_path)
                    if "volatility_scanner" in data:
                        cfg = data["volatility_scanner"]
                except:
                    pass
                    
            json_bytes = json.dumps(cfg, indent=4, ensure_ascii=False).encode("utf-8")
            await callback.message.answer("<b>Текущие настройки Скринера:</b>\nОтредактируйте файл и отправьте обратно документом (или текстом).", reply_markup=self._get_back_keyboard(), parse_mode="HTML")
            await callback.message.answer_document(BufferedInputFile(json_bytes, filename="scanner_config.json"))
            await state.set_state(TGStates.waiting_for_scanner_json)

        @self.dp.message(TGStates.waiting_for_scanner_json)
        async def process_scanner_json(message: Message, state: FSMContext):
            if message.text and message.text == "🔙 Back":
                await state.clear()
                await message.answer("Отменено.", reply_markup=self._get_main_keyboard())
                return
                
            json_str = ""
            if message.document:
                file = await self.bot.get_file(message.document.file_id)
                out = io.BytesIO()
                await self.bot.download_file(file.file_path, out)
                json_str = out.getvalue().decode('utf-8')
            elif message.text:
                json_str = message.text.strip()
            else:
                await message.answer("❌ Пожалуйста, отправьте текстовое сообщение или .json файл.")
                return
                
            try:
                new_cfg = json.loads(json_str)
                
                config_path = DATA_DIR / "app.json"
                app_data = {}
                if config_path.exists():
                    app_data = Utils.read_json_file(config_path)
                
                app_data["volatility_scanner"] = new_cfg
                Utils.write_json_file(config_path, app_data)
                
                await message.answer("✅ Настройки Скринера успешно обновлены!", reply_markup=self._get_main_keyboard())
                await state.clear()
            except Exception as e:
                await message.answer(f"❌ Ошибка JSON: {e}\nИсправьте и отправьте снова.")

        @self.dp.message(F.text == "🔔 Notifications")
        async def on_notifications_menu(message: Message, state: FSMContext):
            await state.clear()
            app_cfg = Utils.read_json_file(DATA_DIR / "app.json")
            notif_cfg = app_cfg.get("notifications")
            if notif_cfg:
                enabled = notif_cfg["enabled"]
                neg_th = notif_cfg["negative_threshold"]
                pos_th = notif_cfg["positive_threshold"]
                neg_flag = self.bot_core.notifier.neg_flag if hasattr(self.bot_core, "notifier") else False
                pos_flag = self.bot_core.notifier.pos_flag if hasattr(self.bot_core, "notifier") else False
                status_emoji = "✅ Вкл" if enabled else "❌ Выкл"
                text = (
                    f"<b>Управление уведомлениями (Портфель):</b>\n"
                    f"Статус: {status_emoji}\n"
                    f"Отрицательный порог: <b>{neg_th} USDT</b> (Сработал: {neg_flag})\n"
                    f"Положительный порог: <b>{pos_th} USDT</b> (Сработал: {pos_flag})\n"
                )
            else:
                text = "<b>🔔 Уведомления по куполам стратегий:</b>\n\n"
                strats = self.bot_core.strategy_registry.get_active_strategies() if hasattr(self.bot_core, "strategy_registry") and hasattr(self.bot_core.strategy_registry, "get_active_strategies") else (self.bot_core.strategy_registry.active_strategies if hasattr(self.bot_core, "strategy_registry") else {})
                for sid, scfg in strats.items():
                    sn_cfg = scfg.notifications_cfg
                    en_str = "✅" if sn_cfg.get("enabled", True) else "❌"
                    neg_th = sn_cfg.get("negative_threshold", -20.0)
                    pos_th = sn_cfg.get("positive_threshold", 20.0)
                    sflags = self.bot_core.notifier.strat_flags.get(sid, {}) if hasattr(self.bot_core, "notifier") else {}
                    n_flg = "🔔" if sflags.get("neg_flag") else "ок"
                    p_flg = "🎉" if sflags.get("pos_flag") else "ок"
                    text += (
                        f"• <b>{scfg.name}</b> ({sid}) {en_str}\n"
                        f"  ├ Просадка: <b>{neg_th} USDT</b> [{n_flg}]\n"
                        f"  └ Профицит: <b>{pos_th} USDT</b> [{p_flg}]\n"
                    )

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔕 Сбросить флаги всех куполов", callback_data="notif_reset_flags")]
            ])
            await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

        @self.dp.callback_query(F.data == "notif_reset_flags")
        async def process_notif_reset_flags(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            if hasattr(self.bot_core, "notifier"):
                self.bot_core.notifier.reset_flags()
                await callback.message.answer("✅ Флаги уведомлений успешно сброшены вручную!", reply_markup=self._get_main_keyboard())
            else:
                await callback.message.answer("❌ Модуль уведомлений не найден.")

        @self.dp.callback_query(F.data == "notif_edit_neg")
        async def process_notif_edit_neg(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            await state.set_state(TGStates.waiting_for_notif_neg)
            await callback.message.answer("Введите новый отрицательный порог (например, -150.0):", reply_markup=self._get_back_keyboard())

        @self.dp.callback_query(F.data == "notif_edit_pos")
        async def process_notif_edit_pos(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            await state.set_state(TGStates.waiting_for_notif_pos)
            await callback.message.answer("Введите новый положительный порог (например, 150.0):", reply_markup=self._get_back_keyboard())

        @self.dp.message(TGStates.waiting_for_notif_neg)
        async def process_notif_neg_input(message: Message, state: FSMContext):
            if message.text == "🔙 Back":
                await state.clear()
                await message.answer("Действие отменено.", reply_markup=self._get_main_keyboard())
                return
            try:
                new_th = float(message.text.replace(',', '.'))
                app_path = DATA_DIR / "app.json"
                app_data = Utils.read_json_file(app_path)
                if "notifications" not in app_data:
                    app_data["notifications"] = {}
                app_data["notifications"]["negative_threshold"] = new_th
                Utils.write_json_file(app_path, app_data)
                
                await message.answer(f"✅ Отрицательный порог успешно установлен на {new_th}.", reply_markup=self._get_main_keyboard())
                await state.clear()
            except ValueError:
                await message.answer("❌ Некорректное число. Введите порог еще раз (например, -150.0) или нажмите Back:")

        @self.dp.message(TGStates.waiting_for_notif_pos)
        async def process_notif_pos_input(message: Message, state: FSMContext):
            if message.text == "🔙 Back":
                await state.clear()
                await message.answer("Действие отменено.", reply_markup=self._get_main_keyboard())
                return
            try:
                new_th = float(message.text.replace(',', '.'))
                app_path = DATA_DIR / "app.json"
                app_data = Utils.read_json_file(app_path)
                if "notifications" not in app_data:
                    app_data["notifications"] = {}
                app_data["notifications"]["positive_threshold"] = new_th
                Utils.write_json_file(app_path, app_data)
                
                await message.answer(f"✅ Положительный порог успешно установлен на {new_th}.", reply_markup=self._get_main_keyboard())
                await state.clear()
            except ValueError:
                await message.answer("❌ Некорректное число. Введите порог еще раз (например, 150.0) или нажмите Back:")

        @self.dp.message(F.text == "🛑 Auto Closing")
        async def on_auto_closing_menu(message: Message, state: FSMContext):
            await state.clear()
            app_cfg = Utils.read_json_file(DATA_DIR / "app.json")
            auto_cfg = app_cfg.get("auto_closing")
            def fmt(v): return "Выкл (0/null)" if (v is None or v == 0) else str(v)

            if auto_cfg:
                pos = auto_cfg.get("positive", {})
                neg = auto_cfg.get("negative", {})
                pos_th = pos.get("threshold")
                pos_inc = pos.get("threshold_increment")
                neg_th = neg.get("threshold")
                neg_inc = neg.get("threshold_increment")
                text = (
                    f"<b>Управление Авто-Закрытием (Портфель):</b>\n\n"
                    f"🟢 <b>Positive (Тейк Профит):</b>\n"
                    f"├ Порог: <b>{fmt(pos_th)}</b>\n"
                    f"└ Шаг инкремента: <b>{fmt(pos_inc)}</b>\n\n"
                    f"🔴 <b>Negative (Стоп Лосс):</b>\n"
                    f"├ Порог: <b>{fmt(neg_th)}</b>\n"
                    f"└ Шаг инкремента: <b>{fmt(neg_inc)}</b>\n\n"
                    f"<i>Примечание: отправьте 0 чтобы отключить (null).</i>"
                )
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="🟢 Изменить POS порог", callback_data="autoclose_edit_pos_th"),
                        InlineKeyboardButton(text="🟢 Изменить POS шаг", callback_data="autoclose_edit_pos_inc")
                    ],
                    [
                        InlineKeyboardButton(text="🔴 Изменить NEG порог", callback_data="autoclose_edit_neg_th"),
                        InlineKeyboardButton(text="🔴 Изменить NEG шаг", callback_data="autoclose_edit_neg_inc")
                    ]
                ])
            else:
                text = "<b>🛑 Авто-Закрытие по куполам стратегий:</b>\n\n"
                strats = self.bot_core.strategy_registry.get_active_strategies() if hasattr(self.bot_core, "strategy_registry") and hasattr(self.bot_core.strategy_registry, "get_active_strategies") else (self.bot_core.strategy_registry.active_strategies if hasattr(self.bot_core, "strategy_registry") else {})
                for sid, scfg in strats.items():
                    sac = scfg.auto_closing_cfg
                    pos_th = sac.get("positive", {}).get("threshold")
                    neg_th = sac.get("negative", {}).get("threshold")
                    text += (
                        f"• <b>{scfg.name}</b> ({sid}):\n"
                        f"  ├ 🟢 Take-Profit: <b>{fmt(pos_th)} USDT</b>\n"
                        f"  └ 🔴 Stop-Loss: <b>{fmt(neg_th)} USDT</b>\n"
                    )
                keyboard = self._get_back_keyboard()

            await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

        async def edit_autoclose_field(callback: CallbackQuery, state: FSMContext, state_val: State, msg: str):
            await callback.answer()
            await callback.message.answer(msg)
            await state.set_state(state_val)

        @self.dp.callback_query(F.data == "autoclose_edit_pos_th")
        async def process_autoclose_edit_pos_th(callback: CallbackQuery, state: FSMContext):
            await edit_autoclose_field(callback, state, TGStates.waiting_for_autoclose_pos_th, "Введите новый POSITIVE порог (например, 50. Введите 0 для отключения):")
            
        @self.dp.callback_query(F.data == "autoclose_edit_pos_inc")
        async def process_autoclose_edit_pos_inc(callback: CallbackQuery, state: FSMContext):
            await edit_autoclose_field(callback, state, TGStates.waiting_for_autoclose_pos_inc, "Введите новый POSITIVE шаг инкремента (например, 50. Введите 0 для отключения):")
            
        @self.dp.callback_query(F.data == "autoclose_edit_neg_th")
        async def process_autoclose_edit_neg_th(callback: CallbackQuery, state: FSMContext):
            await edit_autoclose_field(callback, state, TGStates.waiting_for_autoclose_neg_th, "Введите новый NEGATIVE порог (например, -50. Введите 0 для отключения):")
            
        @self.dp.callback_query(F.data == "autoclose_edit_neg_inc")
        async def process_autoclose_edit_neg_inc(callback: CallbackQuery, state: FSMContext):
            await edit_autoclose_field(callback, state, TGStates.waiting_for_autoclose_neg_inc, "Введите новый NEGATIVE шаг инкремента (например, 50. Введите 0 для отключения):")

        async def save_autoclose_field(message: Message, state: FSMContext, section: str, field: str):
            try:
                val = float(message.text.replace(",", "."))
                if val == 0:
                    val = None
                    
                app_data = Utils.read_json_file(DATA_DIR / "app.json")
                if "auto_closing" not in app_data:
                    app_data["auto_closing"] = {"negative": {"threshold": None, "threshold_increment": None}, "positive": {"threshold": 50, "threshold_increment": 50}}
                if section not in app_data["auto_closing"]:
                    app_data["auto_closing"][section] = {}
                    
                app_data["auto_closing"][section][field] = val
                Utils.write_json_file(DATA_DIR / "app.json", app_data)
                
                await message.answer(f"✅ Параметр {section} -> {field} успешно обновлен.", parse_mode="HTML")
                await on_auto_closing_menu(message, state)
            except ValueError:
                await message.answer("❌ Неверный формат числа. Введите число.")

        @self.dp.message(StateFilter(TGStates.waiting_for_autoclose_pos_th))
        async def handle_autoclose_pos_th(message: Message, state: FSMContext):
            await save_autoclose_field(message, state, "positive", "threshold")
            
        @self.dp.message(StateFilter(TGStates.waiting_for_autoclose_pos_inc))
        async def handle_autoclose_pos_inc(message: Message, state: FSMContext):
            await save_autoclose_field(message, state, "positive", "threshold_increment")
            
        @self.dp.message(StateFilter(TGStates.waiting_for_autoclose_neg_th))
        async def handle_autoclose_neg_th(message: Message, state: FSMContext):
            await save_autoclose_field(message, state, "negative", "threshold")
            
        @self.dp.message(StateFilter(TGStates.waiting_for_autoclose_neg_inc))
        async def handle_autoclose_neg_inc(message: Message, state: FSMContext):
            await save_autoclose_field(message, state, "negative", "threshold_increment")

        @self.dp.message(F.text == "🛡️ Risk System")
        async def on_risk_system(message: Message, state: FSMContext):
            await state.clear()
            app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
            risk_cfg = app_data.get("risk_system") or app_data.get("stop_bot_rirk_system") or {}
            enabled = risk_cfg.get("enabled", False)
            min_thresh = risk_cfg.get("min_balance_threshold_usdt")
            max_thresh = risk_cfg.get("max_balance_threshold_usdt")
            
            analytics_path = ANALYTICS_DIR / "analytics.json"
            cur_bal = 0.0
            if analytics_path.exists():
                try:
                    adata = json.loads(analytics_path.read_text(encoding="utf-8"))
                    cur_bal = float(adata.get("cur_balance_usdt", 0.0))
                except Exception:
                    pass

            status_str = "🟢 Включен" if enabled else "🔴 Выключен"
            min_str = f"<b>{min_thresh:.2f} USDT</b>" if min_thresh is not None else "<i>Отключен (null)</i>"
            max_str = f"<b>{max_thresh:.2f} USDT</b>" if max_thresh is not None else "<i>Отключен (null)</i>"

            text = (
                "<b>🛡️ ГЛОБАЛЬНЫЙ РУЧНИК (Risk System & Miami Take)</b>\n\n"
                f"Статус защиты: <b>{status_str}</b>\n"
                f"📉 Минимальный порог (Стоп-лосс): {min_str}\n"
                f"🌴 Максимальный порог (Профицит Miami): {max_str}\n"
                f"Текущий баланс кошелька: <b>{cur_bal:.2f} USDT</b>\n\n"
                "<i>💡 Если баланс упадет ниже минимального порога (просадка) или превысит максимальный (тейк-профицит), "
                "бот немедленно блокирует открытие сделок, закрывает все позиции по рынку в 100% кэш, "
                "снимает лимитные ордера, ожидает синхронизации аналитики и останавливает торговлю (Pause).</i>"
            )

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔴 Выключить" if enabled else "🟢 Включить", callback_data="risk_toggle")],
                [
                    InlineKeyboardButton(text="📉 Стоп-лосс ($)", callback_data="risk_set_threshold"),
                    InlineKeyboardButton(text="🌴 Профицит ($)", callback_data="risk_set_max_threshold")
                ],
                [InlineKeyboardButton(text="❌ Сбросить пороги (null)", callback_data="risk_clear_threshold")]
            ])
            await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

        @self.dp.callback_query(F.data == "risk_toggle")
        async def on_risk_toggle(callback: CallbackQuery, state: FSMContext):
            await state.clear()
            app_json_path = DATA_DIR / "app.json"
            app_data = Utils.read_json_file(app_json_path) or {}
            
            risk_cfg = app_data.get("risk_system", {})
            enabled = not risk_cfg.get("enabled", False)
            
            if "risk_system" not in app_data:
                app_data["risk_system"] = {}
            app_data["risk_system"]["enabled"] = enabled
            
            if "stop_bot_rirk_system" in app_data:
                app_data["stop_bot_rirk_system"]["enabled"] = enabled
                
            Utils.write_json_file(app_json_path, app_data)
            
            await callback.answer(f"Risk System {'включена' if enabled else 'выключена'}!")
            
            min_thresh = app_data["risk_system"].get("min_balance_threshold_usdt")
            max_thresh = app_data["risk_system"].get("max_balance_threshold_usdt")
            min_str = f"<b>{min_thresh:.2f} USDT</b>" if min_thresh is not None else "<i>Отключен (null)</i>"
            max_str = f"<b>{max_thresh:.2f} USDT</b>" if max_thresh is not None else "<i>Отключен (null)</i>"
            status_str = "🟢 Включен" if enabled else "🔴 Выключен"
            
            cur_bal = 0.0
            analytics_path = ANALYTICS_DIR / "analytics.json"
            if analytics_path.exists():
                try:
                    adata = json.loads(analytics_path.read_text(encoding="utf-8"))
                    cur_bal = float(adata.get("cur_balance_usdt", 0.0))
                except Exception:
                    pass

            text = (
                "<b>🛡️ ГЛОБАЛЬНЫЙ РУЧНИК (Risk System & Miami Take)</b>\n\n"
                f"Статус защиты: <b>{status_str}</b>\n"
                f"📉 Минимальный порог (Стоп-лосс): {min_str}\n"
                f"🌴 Максимальный порог (Профицит Miami): {max_str}\n"
                f"Текущий баланс кошелька: <b>{cur_bal:.2f} USDT</b>\n\n"
                "<i>💡 Если баланс упадет ниже минимального порога (просадка) или превысит максимальный (тейк-профицит), "
                "бот немедленно блокирует открытие сделок, закрывает все позиции по рынку в 100% кэш, "
                "снимает лимитные ордера, ожидает синхронизации аналитики и останавливает торговлю (Pause).</i>"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔴 Выключить" if enabled else "🟢 Включить", callback_data="risk_toggle")],
                [
                    InlineKeyboardButton(text="📉 Стоп-лосс ($)", callback_data="risk_set_threshold"),
                    InlineKeyboardButton(text="🌴 Профицит ($)", callback_data="risk_set_max_threshold")
                ],
                [InlineKeyboardButton(text="❌ Сбросить пороги (null)", callback_data="risk_clear_threshold")]
            ])
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

        @self.dp.callback_query(F.data == "risk_clear_threshold")
        async def on_risk_clear_threshold(callback: CallbackQuery, state: FSMContext):
            await state.clear()
            app_json_path = DATA_DIR / "app.json"
            app_data = Utils.read_json_file(app_json_path) or {}
            if "risk_system" in app_data:
                app_data["risk_system"]["min_balance_threshold_usdt"] = None
                app_data["risk_system"]["max_balance_threshold_usdt"] = None
            if "stop_bot_rirk_system" in app_data:
                app_data["stop_bot_rirk_system"]["min_balance_threshold_usdt"] = None
                app_data["stop_bot_rirk_system"]["max_balance_threshold_usdt"] = None
            Utils.write_json_file(app_json_path, app_data)
            await callback.answer("Пороги сброшены на null")
            
            min_str = "<i>Отключен (null)</i>"
            max_str = "<i>Отключен (null)</i>"
            status_str = "🟢 Включен" if app_data.get("risk_system", {}).get("enabled") else "🔴 Выключен"
            cur_bal = 0.0
            analytics_path = ANALYTICS_DIR / "analytics.json"
            if analytics_path.exists():
                try:
                    adata = json.loads(analytics_path.read_text(encoding="utf-8"))
                    cur_bal = float(adata.get("cur_balance_usdt", 0.0))
                except Exception:
                    pass

            text = (
                "<b>🛡️ ГЛОБАЛЬНЫЙ РУЧНИК (Risk System & Miami Take)</b>\n\n"
                f"Статус защиты: <b>{status_str}</b>\n"
                f"📉 Минимальный порог (Стоп-лосс): {min_str}\n"
                f"🌴 Максимальный порог (Профицит Miami): {max_str}\n"
                f"Текущий баланс кошелька: <b>{cur_bal:.2f} USDT</b>\n\n"
                "<i>💡 Если баланс упадет ниже минимального порога (просадка) или превысит максимальный (тейк-профицит), "
                "бот немедленно блокирует открытие сделок, закрывает все позиции по рынку в 100% кэш, "
                "снимает лимитные ордера, ожидает синхронизации аналитики и останавливает торговлю (Pause).</i>"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔴 Выключить" if app_data.get("risk_system", {}).get("enabled") else "🟢 Включить", callback_data="risk_toggle")],
                [
                    InlineKeyboardButton(text="📉 Стоп-лосс ($)", callback_data="risk_set_threshold"),
                    InlineKeyboardButton(text="🌴 Профицит ($)", callback_data="risk_set_max_threshold")
                ],
                [InlineKeyboardButton(text="❌ Сбросить пороги (null)", callback_data="risk_clear_threshold")]
            ])
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

        @self.dp.callback_query(F.data == "risk_set_threshold")
        async def on_risk_set_threshold(callback: CallbackQuery, state: FSMContext):
            await state.clear()
            await callback.answer()
            await callback.message.answer(
                "Введите минимальный порог баланса (стоп-лосс) в USDT (например: <code>100.0</code> или <code>80</code>):\n\n"
                "Для отключения порога введите <code>null</code> или нажмите 🔙 Back:",
                reply_markup=self._get_back_keyboard(),
                parse_mode="HTML"
            )
            await state.set_state(TGStates.waiting_for_risk_threshold)

        @self.dp.message(StateFilter(TGStates.waiting_for_risk_threshold))
        async def process_risk_threshold(message: Message, state: FSMContext):
            if message.text == "🔙 Back":
                await state.clear()
                await message.answer("Изменение порога отменено.", reply_markup=self._get_main_keyboard())
                return
            
            raw = (message.text or "").strip().lower().replace(",", ".")
            app_json_path = DATA_DIR / "app.json"
            app_data = Utils.read_json_file(app_json_path) or {}
            if "risk_system" not in app_data:
                app_data["risk_system"] = {}

            if raw in ["null", "none", "off", "0", "сброс"]:
                app_data["risk_system"]["min_balance_threshold_usdt"] = None
                if "stop_bot_rirk_system" in app_data:
                    app_data["stop_bot_rirk_system"]["min_balance_threshold_usdt"] = None
                Utils.write_json_file(app_json_path, app_data)
                await message.answer("✅ Порог стоп-лосса отключен (установлен в <b>null</b>).", reply_markup=self._get_main_keyboard(), parse_mode="HTML")
                await state.clear()
                return

            try:
                new_val = float(raw)
                if new_val <= 0:
                    raise ValueError()
            except ValueError:
                await message.answer("❌ Введите корректное положительное число в $ (например: 100.0) или <code>null</code>:")
                return

            app_data["risk_system"]["min_balance_threshold_usdt"] = new_val
            app_data["risk_system"]["enabled"] = True
            if "stop_bot_rirk_system" in app_data:
                app_data["stop_bot_rirk_system"]["min_balance_threshold_usdt"] = new_val
                app_data["stop_bot_rirk_system"]["enabled"] = True
            Utils.write_json_file(app_json_path, app_data)

            await message.answer(
                f"✅ Минимальный порог (стоп-лосс) установлен на <b>{new_val:.2f} USDT</b>!\n"
                f"Система защиты автоматически <b>активирована</b>.",
                reply_markup=self._get_main_keyboard(),
                parse_mode="HTML"
            )
            await state.clear()

        @self.dp.callback_query(F.data == "risk_set_max_threshold")
        async def on_risk_set_max_threshold(callback: CallbackQuery, state: FSMContext):
            await state.clear()
            await callback.answer()
            await callback.message.answer(
                "Введите максимальный целевой порог (профицит / Miami Take) в USDT (например: <code>300.0</code> или <code>500</code>):\n\n"
                "Для отключения порога введите <code>null</code> или нажмите 🔙 Back:",
                reply_markup=self._get_back_keyboard(),
                parse_mode="HTML"
            )
            await state.set_state(TGStates.waiting_for_risk_max_threshold)

        @self.dp.message(StateFilter(TGStates.waiting_for_risk_max_threshold))
        async def process_risk_max_threshold(message: Message, state: FSMContext):
            if message.text == "🔙 Back":
                await state.clear()
                await message.answer("Изменение порога отменено.", reply_markup=self._get_main_keyboard())
                return
            
            raw = (message.text or "").strip().lower().replace(",", ".")
            app_json_path = DATA_DIR / "app.json"
            app_data = Utils.read_json_file(app_json_path) or {}
            if "risk_system" not in app_data:
                app_data["risk_system"] = {}

            if raw in ["null", "none", "off", "0", "сброс"]:
                app_data["risk_system"]["max_balance_threshold_usdt"] = None
                if "stop_bot_rirk_system" in app_data:
                    app_data["stop_bot_rirk_system"]["max_balance_threshold_usdt"] = None
                Utils.write_json_file(app_json_path, app_data)
                await message.answer("✅ Порог профицита отключен (установлен в <b>null</b>).", reply_markup=self._get_main_keyboard(), parse_mode="HTML")
                await state.clear()
                return

            try:
                new_val = float(raw)
                if new_val <= 0:
                    raise ValueError()
            except ValueError:
                await message.answer("❌ Введите корректное положительное число в $ (например: 300.0) или <code>null</code>:")
                return

            app_data["risk_system"]["max_balance_threshold_usdt"] = new_val
            app_data["risk_system"]["enabled"] = True
            if "stop_bot_rirk_system" in app_data:
                app_data["stop_bot_rirk_system"]["max_balance_threshold_usdt"] = new_val
                app_data["stop_bot_rirk_system"]["enabled"] = True
            Utils.write_json_file(app_json_path, app_data)

            await message.answer(
                f"🌴 Максимальный целевой порог (профицит Miami Take) установлен на <b>{new_val:.2f} USDT</b>!\n"
                f"Система защиты автоматически <b>активирована</b>.",
                reply_markup=self._get_main_keyboard(),
                parse_mode="HTML"
            )
            await state.clear()

    async def start(self):
        logger.info("Starting Telegram Receiver...")
        try:
            await self.dp.start_polling(self.bot)
        finally:
            await self.stop()

    async def stop(self):
        try:
            await self.dp.stop_polling()
        except Exception:
            pass
        try:
            if self.bot and hasattr(self.bot, "session") and self.bot.session:
                await self.bot.session.close()
        except Exception:
            pass


def build_rotation_inline_keyboard(req_id: str, count: int = 1) -> dict:
    """Генератор инлайн-клавиатуры для запроса подтверждения ротации."""
    accept_text = f"✅ Применить ротацию ({count})" if count > 1 else "✅ Применить ротацию"
    return {
        "inline_keyboard": [
            [
                {"text": accept_text, "callback_data": f"rot_accept:{req_id}"},
                {"text": "❌ Оставить как есть", "callback_data": f"rot_reject:{req_id}"}
            ]
        ]
    }
