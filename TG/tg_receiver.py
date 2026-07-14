# ==============================================================================
# Path: TG/tg_receiver.py
# Role: Telegram-бот для управления торговым ядром (Start/Stop, Настройки)
# ==============================================================================
import asyncio
import os
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.types import ErrorEvent

from c_log import UnifiedLogger
from consts import TG_TOKEN, ANALYTICS_DIR, TG_ALLOWED_USERS
from TG.template_manager import TemplateManager

logger = UnifiedLogger("TGReceiver")

class TGStates(StatesGroup):
    waiting_for_add_symbol = State()
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
    waiting_for_notif_neg = State()
    waiting_for_notif_pos = State()
    waiting_for_flatness_val = State()

class TelegramReceiver:
    def __init__(self, bot_core):
        self.bot_core = bot_core
        self.bot = Bot(token=TG_TOKEN)
        self.dp = Dispatcher(storage=MemoryStorage())
        self.template_manager = TemplateManager()
        self._lock = asyncio.Lock()
        
        # Регистрация простого middleware для проверки прав доступа
        @self.dp.message.outer_middleware()
        async def check_user_message(handler, event: Message, data: dict):
            if TG_ALLOWED_USERS and event.from_user.id not in TG_ALLOWED_USERS:
                logger.warning(f"Unauthorized access attempt by {event.from_user.id}")
                return
            return await handler(event, data)

        @self.dp.callback_query.outer_middleware()
        async def check_user_callback(handler, event: CallbackQuery, data: dict):
            if TG_ALLOWED_USERS and event.from_user.id not in TG_ALLOWED_USERS:
                logger.warning(f"Unauthorized access attempt by {event.from_user.id}")
                return
            return await handler(event, data)

        self._register_handlers()

    def _get_main_keyboard(self):
        keyboard = [
            [
                KeyboardButton(text="▶️ Start"),
                KeyboardButton(text="⏸️ Stop")
            ],
            [
                KeyboardButton(text="📊 Analytics"),
                KeyboardButton(text="⚙️ Set Coins")
            ],
            [
                KeyboardButton(text="📜 Get Logs"),
                KeyboardButton(text="ℹ️ Status")
            ],
            [
                KeyboardButton(text="📂 Get CFG"),
                KeyboardButton(text="🔧 Super Grid")
            ],
            [
                KeyboardButton(text="💰 Задать нач. баланс"),
                KeyboardButton(text="🗑️ Сбросить аналитику")
            ],
            [
                KeyboardButton(text="🔍 ATR SCREENER"),
                KeyboardButton(text="📉 FLAT SCREENER"),
                KeyboardButton(text="📉 FLAT V2 (Боковик)")
            ],
            [
                KeyboardButton(text="🔔 SET INFO")
            ]
        ]
        return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

    def _get_set_coins_keyboard(self):
        keyboard = [
            [
                KeyboardButton(text="➕ Add"),
                KeyboardButton(text="✏️ Edit"),
                KeyboardButton(text="🗑️ Del")
            ],
            [
                KeyboardButton(text="📄 _base"),
                KeyboardButton(text="❓ Help")
            ],
            [
                KeyboardButton(text="🔙 Back")
            ]
        ]
        return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

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

    def _register_handlers(self):
        @self.dp.errors()
        async def global_error_handler(event: ErrorEvent):
            if isinstance(event.exception, TelegramNetworkError):
                logger.warning(f"[TG] Network Error (suppressed): {event.exception}")
                return True
            if isinstance(event.exception, TelegramRetryAfter):
                logger.warning(f"[TG] Rate Limit exceeded, retry after {event.exception.retry_after}s")
                return True
            logger.error(f"[TG] Unhandled exception: {event.exception}")
            return False

        @self.dp.message(Command("start"))
        async def start_cmd(message: Message, state: FSMContext):
            await state.clear()
            status = "⏸️ Paused" if self.bot_core.is_paused else "▶️ Running"
            text = f"<b>Control Panel</b>\nCurrent Status: {status}"
            await message.answer(text, reply_markup=self._get_main_keyboard(), parse_mode="HTML")

        @self.dp.message(F.text == "▶️ Start")
        async def on_pre_start(message: Message, state: FSMContext):
            await state.clear()
            
            if not self.bot_core.is_paused:
                await message.answer("⚠️ Trading is already running!", reply_markup=self._get_main_keyboard())
                return
                
            from consts import _CFG
            symbols = _CFG.get("symbols", [])
            
            for sym in symbols:
                sym_lower = sym.lower()
                runtime_path = self.template_manager.runtime_dir / f"{sym_lower}.json"
                if runtime_path.exists():
                    import json
                    try:
                        with open(runtime_path, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            
                        lines = [f"<b>{sym} Settings:</b>"]
                        for side in ["LONG", "SHORT"]:
                            cfg = data.get(side, {})
                            en = cfg.get("enable", False)
                            sz = cfg.get("invest_size", 0)
                            lev = cfg.get("leverage", 0)
                            margin = cfg.get("margin_type", "CROSSED")
                            
                            lines.append(f"  <b>{side}</b>: {'✅ On' if en else '❌ Off'}")
                            if en:
                                lines.append(f"    ├ Invest: {sz}$ | Lev: {lev}x | {margin}")
                                
                                grid = cfg.get("grid", {})
                                indents = []
                                super_indents = []
                                for k in sorted(grid.keys(), key=lambda x: int(x)):
                                    indents.append(str(grid[k].get("indent", 0)))
                                    si = grid[k].get("super_indent")
                                    super_indents.append(str(si) if si is not None else "-")
                                lines.append(f"    ├ Grid: [{', '.join(indents)}]")
                                
                                from consts import _CFG
                                if _CFG.get("super_grid", {}).get("enabled", False) and any(si != "-" for si in super_indents):
                                    lines.append(f"    ├ Super: [{', '.join(super_indents)}]")
                                    
                                
                                tp_map = cfg.get("tp_map", {})
                                tps = []
                                for k in sorted(tp_map.keys(), key=lambda x: int(x)):
                                    tps.append(str(tp_map[k].get("indent", 0)))
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
            text = f"<b>Control Panel</b>\nCurrent Status: {status}"
            await message.answer(text, reply_markup=self._get_main_keyboard(), parse_mode="HTML")

        @self.dp.message(F.text == "✅ Confirm Start")
        async def on_confirm_start(message: Message, state: FSMContext):
            await state.clear()
            
            # Save auto_start = True to app.json
            from consts import DATA_DIR
            from c_utils import Utils
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
            from consts import DATA_DIR
            from c_utils import Utils
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
            from consts import _CFG
            super_grid_enabled = _CFG.get("super_grid", {}).get("enabled", False)
            super_grid_status = "✅ On" if super_grid_enabled else "❌ Off"
            text = f"<b>Control Panel</b>\nCurrent Status: {status}\nSuper Grid (Volatility): {super_grid_status}"
            await message.answer(text, reply_markup=self._get_main_keyboard(), parse_mode="HTML")

        @self.dp.message(F.text == "📜 Get Logs")
        async def on_get_logs(message: Message, state: FSMContext):
            await state.clear()
            log_path = os.path.join("logs", "all.log")
            if os.path.exists(log_path):
                await message.answer_document(FSInputFile(log_path))
            else:
                await message.answer("Global log file not found.")

        @self.dp.message(F.text == "📂 Get CFG")
        async def on_get_cfg(message: Message, state: FSMContext):
            await state.clear()
            from consts import DATA_DIR
            from c_utils import Utils
            import json
            
            dump_data = {}
            
            # Read app.json
            app_json = DATA_DIR / "app.json"
            if app_json.exists():
                dump_data["app.json"] = Utils.read_json_file(app_json)
                
            # Read _base.json
            base_json = DATA_DIR / "_base.json"
            if base_json.exists():
                dump_data["_base.json"] = Utils.read_json_file(base_json)
                
            # Read runtimes
            dump_data["runtime"] = {}
            runtime_dir = DATA_DIR / "runtime"
            if runtime_dir.exists():
                for file_path in runtime_dir.glob("*.json"):
                    dump_data["runtime"][file_path.name] = Utils.read_json_file(file_path)
                    
            dump_path = os.path.join("logs", "all_configs.json")
            os.makedirs("logs", exist_ok=True)
            with open(dump_path, "w", encoding="utf-8") as f:
                json.dump(dump_data, f, indent=4)
                
            await message.answer_document(FSInputFile(dump_path))

        @self.dp.message(F.text == "🗑️ Сбросить аналитику")
        async def on_reset_analytics(message: Message, state: FSMContext):
            await state.clear()
            await message.answer("⚠️ Вы уверены, что хотите полностью удалить историю аналитики?\n\nВведите слово <b>СБРОС</b> для подтверждения или нажмите Back для отмены.", reply_markup=self._get_back_keyboard(), parse_mode="HTML")
            await state.set_state(TGStates.waiting_for_reset_confirm)

        @self.dp.message(TGStates.waiting_for_reset_confirm)
        async def process_reset_analytics_confirm(message: Message, state: FSMContext):
            if not message.text:
                await message.answer("❌ Пожалуйста, отправьте текстовое сообщение.")
                return
            if message.text == "🔙 Back":
                await state.clear()
                await message.answer("Сброс аналитики отменен.", reply_markup=self._get_main_keyboard())
                return
                
            if message.text.strip().upper() == "СБРОС":
                await state.clear()
                analytics_path = ANALYTICS_DIR / "analytics.json"
                if analytics_path.exists():
                    try:
                        os.remove(analytics_path)
                        csv_path = ANALYTICS_DIR / "trades_ledger.txt"
                        if csv_path.exists():
                            os.remove(csv_path)
                        await message.answer("✅ Файл аналитики успешно удален. Он будет создан заново при следующем обновлении.", reply_markup=self._get_main_keyboard())
                    except Exception as e:
                        await message.answer(f"❌ Ошибка при удалении файла аналитики: {e}", reply_markup=self._get_main_keyboard())
                else:
                    await message.answer("⚠️ Файл аналитики не найден.", reply_markup=self._get_main_keyboard())
            else:
                await message.answer("❌ Неверное слово подтверждения. Введите <b>СБРОС</b> или нажмите Back.", parse_mode="HTML")
        @self.dp.message(F.text == "💰 Задать нач. баланс")
        async def on_set_initial_balance(message: Message, state: FSMContext):
            await state.clear()
            await message.answer("Введите новый начальный баланс (start_balance_usdt) в USDT (например, 100.5):", reply_markup=self._get_back_keyboard())
            await state.set_state(TGStates.waiting_for_initial_balance)

        @self.dp.message(TGStates.waiting_for_initial_balance)
        async def process_initial_balance(message: Message, state: FSMContext):
            if not message.text:
                await message.answer("❌ Пожалуйста, отправьте текстовое сообщение.")
                return
            if message.text == "🔙 Back":
                await state.clear()
                await message.answer("Действие отменено.", reply_markup=self._get_main_keyboard())
                return
                
            try:
                new_balance = float(message.text.replace(',', '.'))
                if new_balance < 0:
                    raise ValueError("Баланс не может быть отрицательным.")
            except ValueError:
                await message.answer("❌ Некорректное число. Введите баланс еще раз (например, 100.5) или нажмите Back:")
                return
                
            import json
            analytics_path = ANALYTICS_DIR / "analytics.json"
            if analytics_path.exists():
                try:
                    with open(analytics_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    
                    data["start_balance_usdt"] = round(new_balance, 4)
                    
                    # При сбросе баланса также сбрасываем пики и просадки, 
                    # чтобы не было искусственной просадки на разницу балансов
                    data["peak_balance_usdt"] = round(new_balance, 4)
                    data["_current_trough_usdt"] = round(new_balance, 4)
                    data["min_balance_usdt"] = round(new_balance, 4)
                    data["max_drawdown_usdt"] = 0.0
                    data["performance_usdt"] = 0.0
                    data["recovery_factor"] = 0.0
                    data["roi_pct"] = 0.0
                    

                    with open(analytics_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=4)
                        
                    await message.answer(f"✅ Начальный баланс успешно установлен на {new_balance} USDT.", reply_markup=self._get_main_keyboard())
                except Exception as e:
                    await message.answer(f"❌ Ошибка обновления файла аналитики: {e}", reply_markup=self._get_main_keyboard())
            else:
                await message.answer("⚠️ Файл аналитики пока не существует. Подождите, пока бот создаст его.", reply_markup=self._get_main_keyboard())
                
            await state.clear()

        @self.dp.message(F.text == "📊 Analytics")
        async def on_analytics(message: Message, state: FSMContext):
            await state.clear()
            
            # Level 2 Protection: Deep Sync Analytics before showing dashboard
            msg = await message.answer("⏳ Синхронизация с Binance...")
            try:
                await self.bot_core.analytics.deep_sync_analytics(self.bot_core.client)
                await msg.delete()
            except Exception as e:
                logger.error(f"Failed to deep sync: {e}")
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌍 Global Analytics", callback_data="analytics_global")],
                [InlineKeyboardButton(text="🪙 Аналитика по монетам", callback_data="analytics_coin_menu")],
                [InlineKeyboardButton(text="🏆 Рейтинг монет", callback_data="analytics_ranking")],
                [InlineKeyboardButton(text="📝 Лента сделок (TXT)", callback_data="analytics_txt")],
                [InlineKeyboardButton(text="📄 Выгрузить весь отчет (TXT)", callback_data="analytics_full_report")],
                [InlineKeyboardButton(text="📚 Шпаргалка (Cheat Sheet)", callback_data="analytics_cheat_sheet")]
            ])
            await message.answer("Выберите раздел аналитики:", reply_markup=keyboard)

        @self.dp.callback_query(F.data == "analytics_cheat_sheet")
        async def process_analytics_cheat_sheet(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            help_str = (
                "<b>📚 Analytics Cheat Sheet (Global)</b>\n"
                "▪️ <b>roi_pct</b>: Return on Investment (%) → <i>(Cur_Balance - Start_Balance) / Start_Balance * 100</i>\n"
                "▪️ <b>load_ratio</b>: Grid Load Ratio → <i>|Unrealized PnL| / Realized PnL</i>\n"
                "▪️ <b>recovery_factor</b>: Recovery Factor → <i>Realized PnL / |Max Drawdown|</i>\n"
                "▪️ <b>realized_pnl_usdt</b>: Total realized profit from all closed trades\n"
                "▪️ <b>net_profit_usdt</b>: True mathematical growth → <i>Realized PnL + Unrealized PnL</i>\n"
                "▪️ <b>cur_balance_usdt</b>: Current margin balance → <i>Start Balance + Net Profit</i>\n\n"
                "<b>🪙 Per-Coin Metrics</b>\n"
                "▪️ <b>avg_daily_profit</b>: Average profit per active day of trading\n"
                "▪️ <b>max_position_size</b>: Max historical notional size actually reached (real volume * price)\n"
                "▪️ <b>risk_reward_ratio</b>: <i>|Max Drawdown| / Avg Daily Profit</i>\n"
                "▪️ <b>DRME</b> (Daily Return on Max Exposure): <i>Avg Daily Profit / Max Position Size</i>\n"
                "▪️ <b>MDME</b> (Max Drawdown on Max Exposure): <i>|Max Drawdown| / Max Position Size</i>\n"
                "▪️ <b>max_drawdown</b> / <b>min_drawdown</b>: Max/Min historical floating drawdowns\n"
            )
            await callback.message.answer(help_str, parse_mode="HTML")

        @self.dp.callback_query(F.data.startswith("analytics_ranking"))
        async def process_analytics_ranking(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            import json
            
            # Parse criterion
            parts = callback.data.split(":")
            criterion = parts[1] if len(parts) > 1 else "net"
            
            analytics_path = ANALYTICS_DIR / "analytics.json"
            if not analytics_path.exists():
                await callback.message.answer("Analytics JSON not found.")
                return
                
            try:
                data = json.loads(analytics_path.read_text(encoding="utf-8"))
                per_coin = data.get("per_coin", {})
                if not per_coin:
                    await callback.message.answer("Нет данных по монетам.")
                    return
                
                # Determine sorting logic and title
                if criterion == "rr":
                    sorted_coins = sorted(per_coin.items(), key=lambda x: float(x[1].get("risk_reward_ratio", float('inf'))))
                    title = "по Risk/Reward (меньше = лучше)"
                elif criterion == "drme":
                    sorted_coins = sorted(per_coin.items(), key=lambda x: float(x[1].get("DRME", -float('inf'))), reverse=True)
                    title = "по DRME (больше = лучше)"
                elif criterion == "mdme":
                    sorted_coins = sorted(per_coin.items(), key=lambda x: float(x[1].get("MDME", float('inf'))))
                    title = "по MDME (меньше = лучше)"
                else: # Default net
                    sorted_coins = sorted(per_coin.items(), key=lambda x: float(x[1].get("net_profit_usdt", -float('inf'))), reverse=True)
                    title = "по Net Profit"
                    
                lines = [f"<b>🏆 Рейтинг монет ({title})</b>\n"]
                for i, (sym, cdata) in enumerate(sorted_coins, 1):
                    val_str = ""
                    if criterion == "rr":
                        val_str = f"R/R: {cdata.get('risk_reward_ratio', 0)}"
                    elif criterion == "drme":
                        val_str = f"DRME: {cdata.get('DRME', 0)}"
                    elif criterion == "mdme":
                        val_str = f"MDME: {cdata.get('MDME', 0)}"
                    else:
                        net_profit = cdata.get("net_profit_usdt", 0)
                        realized_net = cdata.get("realized_pnl_net_usdt", 0)
                        val_str = f"{net_profit} USDT <i>(Realized: {realized_net})</i>"
                    
                    if i == 1:
                        medal = "🥇"
                    elif i == 2:
                        medal = "🥈"
                    elif i == 3:
                        medal = "🥉"
                    else:
                        medal = "▪️"
                        
                    lines.append(f"{medal} <b>{sym}</b>: {val_str}")
                
                # Add inline keyboard to switch criterion
                switch_kb = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="Net Profit", callback_data="analytics_ranking:net"),
                        InlineKeyboardButton(text="Risk/Reward", callback_data="analytics_ranking:rr")
                    ],
                    [
                        InlineKeyboardButton(text="DRME", callback_data="analytics_ranking:drme"),
                        InlineKeyboardButton(text="MDME", callback_data="analytics_ranking:mdme")
                    ]
                ])
                    
                # If callback was a direct button press from the main menu, we send a new message.
                # If it was a switch from the ranking menu itself, we can edit the message.
                if len(parts) > 1:
                    try:
                        await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=switch_kb)
                    except Exception as e:
                        if "message is not modified" not in str(e).lower():
                            raise
                else:
                    await callback.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=switch_kb)
            except Exception as e:
                logger.error(f"Error generating ranking: {e}")
                await callback.message.answer("Ошибка генерации рейтинга.")

        @self.dp.callback_query(F.data == "analytics_txt")
        async def process_analytics_txt(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            txt_path = ANALYTICS_DIR / "trades_ledger.txt"
            if txt_path.exists():
                await callback.message.answer_document(FSInputFile(str(txt_path)))
            else:
                await callback.message.answer("Trades ledger TXT not found.")

        @self.dp.callback_query(F.data == "analytics_full_report")
        async def process_analytics_full_report(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            import json
            analytics_path = ANALYTICS_DIR / "analytics.json"
            if not analytics_path.exists():
                await callback.message.answer("Analytics JSON not found.")
                return
                
            try:
                data = json.loads(analytics_path.read_text(encoding="utf-8"))
                
                lines = []
                lines.append("="*50)
                lines.append("GLOBAL ANALYTICS REPORT")
                lines.append("="*50 + "\n")
                
                lines.append("Balances:")
                lines.append(f"  Start: {data.get('start_balance_usdt', 0)} USDT")
                lines.append(f"  Current: {data.get('cur_balance_usdt', 0)} USDT")
                lines.append(f"  Peak: {data.get('peak_balance_usdt', 0)} USDT")
                lines.append(f"  Min (Bottom): {data.get('min_balance_usdt', 0)} USDT\n")
                
                realized = float(data.get('realized_pnl_usdt', 0))
                realized_net = float(data.get('realized_pnl_net_usdt', 0))
                comm = float(data.get('total_commission_usdt', 0))
                fund = float(data.get('total_funding_usdt', 0))
                
                lines.append("Performance:")
                lines.append(f"  Net Profit: {data.get('net_profit_usdt', 0)} USDT")
                lines.append(f"  Realized PnL (Gross): {realized} USDT")
                lines.append(f"  Realized PnL (Net): {realized_net} USDT")
                lines.append(f"  Total Commission: {comm} USDT")
                lines.append(f"  Total Funding: {fund} USDT")
                lines.append(f"  Unrealized PnL: {data.get('unrealized_pnl_usdt', 0)} USDT")
                lines.append(f"  Max Drawdown: {data.get('max_drawdown_usdt', 0)} USDT")
                lines.append(f"  Performance (Peak-Start): {data.get('performance_usdt', 0)} USDT")
                lines.append(f"  ROI: {data.get('roi_pct', 0)}%")
                lines.append(f"  Load Ratio: {data.get('load_ratio', 0)}")
                lines.append(f"  Recovery Factor: {data.get('recovery_factor', 0)}")
                lines.append(f"  Trades: {data.get('total_trades', 0)} (Wins: {data.get('winning_trades', 0)} | {data.get('winrate_pct', 0)}%)\n")
                
                if "per_coin" in data:
                    lines.append("="*50)
                    lines.append("PER-COIN METRICS")
                    lines.append("="*50 + "\n")
                    
                    for sym, cdata in sorted(data["per_coin"].items()):
                        lines.append(f"--- {sym} ---")
                        lines.append(f"  Trades: {cdata.get('total_trades', 0)} (Wins: {cdata.get('winning_trades', 0)} | {cdata.get('winrate_pct', 0)}%)")
                        
                        crealized = float(cdata.get('realized_pnl_usdt', 0))
                        crealized_net = float(cdata.get('realized_pnl_net_usdt', 0))
                        ccomm = float(cdata.get('commission_usdt', 0))
                        cfund = float(cdata.get('funding_usdt', 0))
                        
                        lines.append(f"  Net Profit: {cdata.get('net_profit_usdt', 0)} USDT")
                        lines.append(f"  Realized (Gross): {crealized} USDT")
                        lines.append(f"  Realized (Net): {crealized_net} USDT")
                        lines.append(f"  Fees: Comm {ccomm} / Fund {cfund}")
                        lines.append(f"  Unrealized PnL: {cdata.get('current_drawdown', 0)} USDT")
                        lines.append(f"  Hist. Drawdown: Max {cdata.get('max_drawdown', 0)} / Min {cdata.get('min_drawdown', 0)}")
                        lines.append(f"  Avg Daily Profit (Net): {cdata.get('avg_daily_profit', 0)} USDT")
                        lines.append(f"  Max Position Size: {cdata.get('max_position_size', 0)} USDT")
                        lines.append(f"  Risk/Reward Ratio: {cdata.get('risk_reward_ratio', 0)}")
                        lines.append(f"  DRME: {cdata.get('DRME', 0)}")
                        lines.append(f"  MDME: {cdata.get('MDME', 0)}\n")
                
                report_path = ANALYTICS_DIR / "full_analytics_report.txt"
                report_path.write_text("\n".join(lines), encoding="utf-8")
                
                await callback.message.answer_document(FSInputFile(str(report_path)))
            except Exception as e:
                logger.error(f"Error generating full report: {e}")
                await callback.message.answer("Error generating report.")

        @self.dp.callback_query(F.data == "analytics_global")
        async def process_analytics_global(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            message = callback.message
            
            import json
            from datetime import datetime, timezone
            from zoneinfo import ZoneInfo
            from consts import _CFG
            
            analytics_path = ANALYTICS_DIR / "analytics.json"
            if analytics_path.exists():
                text = analytics_path.read_text(encoding="utf-8")
                
                try:
                    data = json.loads(text)
                    ts = data.get("last_updated_ts")
                    if ts:
                        tz_str = _CFG.get("app", {}).get("time_zone", "UTC")
                        try:
                            dt = datetime.fromtimestamp(ts / 1000.0, tz=ZoneInfo(tz_str))
                        except Exception:
                            dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
                        time_str = dt.strftime('%Y-%m-%d %H:%M:%S %Z')
                        
                        last_trade_time = "Нет сделок"
                        csv_path = ANALYTICS_DIR / "trades_ledger.txt"
                        if csv_path.exists():
                            import csv
                            try:
                                with open(csv_path, 'r', encoding='utf-8') as f:
                                    reader = csv.reader(f, delimiter=';')
                                    lines = list(reader)
                                    if len(lines) > 1:
                                        csv_header = lines[0]
                                        close_idx = csv_header.index('Close Time') if 'Close Time' in csv_header else 3
                                        last_trade_time = lines[-1][close_idx]
                            except Exception:
                                pass
                                
                        header = f"<b>ℹ️ Расчет по состоянию на: {time_str}</b>\n"
                        header += f"<b>⏳ Последняя сделка: {last_trade_time}</b>\n\n"
                    else:
                        header = "<b>ℹ️ Аналитика рассчитана по состоянию на: [неизвестно]</b>\n\n"
                        
                    msg = "<b>🌍 GLOBAL ANALYTICS</b>\n\n"
                    
                    msg += "<b>💰 Balances:</b>\n"
                    msg += f"▪️ Start: <b>{data.get('start_balance_usdt', 0)}</b> USDT\n"
                    msg += f"▪️ Current: <b>{data.get('cur_balance_usdt', 0)}</b> USDT\n"
                    msg += f"▪️ Peak: <b>{data.get('peak_balance_usdt', 0)}</b> USDT\n"
                    msg += f"▪️ Min (Bottom): <b>{data.get('min_balance_usdt', 0)}</b> USDT\n\n"
                    
                    realized = float(data.get('realized_pnl_usdt', 0))
                    realized_net = float(data.get('realized_pnl_net_usdt', 0))
                    comm = float(data.get('total_commission_usdt', 0))
                    fund = float(data.get('total_funding_usdt', 0))
                    
                    msg += "<b>📈 Performance:</b>\n"
                    msg += f"▪️ Net Profit: <b>{data.get('net_profit_usdt', 0)}</b> USDT\n"
                    msg += f"▪️ Realized PnL (Gross): <b>{realized}</b> USDT\n"
                    msg += f"▪️ Realized PnL (Net): <b>{realized_net}</b> USDT\n"
                    msg += f"▪️ Total Commission: <b>{comm}</b> USDT\n"
                    msg += f"▪️ Total Funding: <b>{fund}</b> USDT\n"
                    msg += f"▪️ Unrealized PnL: <b>{data.get('unrealized_pnl_usdt', 0)}</b> USDT\n"
                    msg += f"▪️ Max Drawdown: <b>{data.get('max_drawdown_usdt', 0)}</b> USDT\n"
                    msg += f"▪️ Performance (Peak-Start): <b>{data.get('performance_usdt', 0)}</b> USDT\n"
                    msg += f"▪️ ROI: <b>{data.get('roi_pct', 0)}%</b>\n"
                    msg += f"▪️ Load Ratio: <b>{data.get('load_ratio', 0)}</b>\n"
                    msg += f"▪️ Recovery Factor: <b>{data.get('recovery_factor', 0)}</b>\n"
                    msg += f"▪️ Trades: <b>{data.get('total_trades', 0)}</b> (Wins: <b>{data.get('winning_trades', 0)}</b> | <b>{data.get('winrate_pct', 0)}%</b>)\n\n"
                    
                    await message.answer(header + msg, parse_mode="HTML")
                except Exception as e:
                    logger.error(f"Error formatting analytics text: {e}")
                    await message.answer("Error formatting analytics data.")
            else:
                await message.answer("Analytics JSON not found in ANALYTICS_DIR.")

        @self.dp.callback_query(F.data == "analytics_coin_menu")
        async def process_analytics_coin_menu(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            message = callback.message
            
            import json
            all_coins = set(self.bot_core.symbols)
            analytics_path = ANALYTICS_DIR / "analytics.json"
            if analytics_path.exists():
                try:
                    data = json.loads(analytics_path.read_text(encoding="utf-8"))
                    if "per_coin" in data:
                        all_coins.update(data["per_coin"].keys())
                except Exception:
                    pass
            
            if not all_coins:
                await message.answer("❌ Нет данных о монетах для аналитики.")
                return
                
            keyboard_buttons = []
            for coin in sorted(all_coins):
                is_active = coin in self.bot_core.symbols
                status_icon = "🟢" if is_active else "🔴"
                keyboard_buttons.append([InlineKeyboardButton(text=f"🪙 {coin} {status_icon}", callback_data=f"analytics_coin_{coin}")])
                
            keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
            await message.answer("Выберите монету для детальной аналитики:", reply_markup=keyboard)

        @self.dp.callback_query(F.data.startswith("analytics_coin_"))
        async def process_analytics_coin_select(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            symbol = callback.data.replace("analytics_coin_", "")
            
            is_active = symbol in self.bot_core.symbols
            status_text = "🟢 Активна" if is_active else "🔴 Отключена"
            
            import json
            analytics_path = ANALYTICS_DIR / "analytics.json"
            if analytics_path.exists():
                try:
                    data = json.loads(analytics_path.read_text(encoding="utf-8"))
                    if "per_coin" in data and symbol in data["per_coin"]:
                        cdata = data["per_coin"][symbol]
                        msg_coins = "<b>🪙 PER-COIN METRICS</b>\n\n"
                        msg_coins += f"🔹 <b>{symbol}</b> ({status_text})\n"
                        msg_coins += f"  • Trades: <b>{cdata.get('total_trades', 0)}</b> (Wins: <b>{cdata.get('winning_trades', 0)}</b> | <b>{cdata.get('winrate_pct', 0)}%</b>)\n"
                        crealized = float(cdata.get('realized_pnl_usdt', 0))
                        crealized_net = float(cdata.get('realized_pnl_net_usdt', 0))
                        ccomm = float(cdata.get('commission_usdt', 0))
                        cfund = float(cdata.get('funding_usdt', 0))
                        
                        msg_coins += f"  • Net Profit: <b>{cdata.get('net_profit_usdt', 0)}</b> USDT\n"
                        msg_coins += f"  • Realized (Gross): <b>{crealized}</b> USDT\n"
                        msg_coins += f"  • Realized (Net): <b>{crealized_net}</b> USDT\n"
                        msg_coins += f"  • Profit Range: Max <b>{cdata.get('max_net_profit', 0)}</b> / Min <b>{cdata.get('min_net_profit', 0)}</b>\n"
                        msg_coins += f"  • Fees: Comm <b>{ccomm}</b> / Fund <b>{cfund}</b>\n"
                        msg_coins += f"  • Unrealized PnL: <b>{cdata.get('current_drawdown', 0)}</b> USDT\n"
                        msg_coins += f"  • Hist. Drawdown: Max <b>{cdata.get('max_drawdown', 0)}</b> / Min <b>{cdata.get('min_drawdown', 0)}</b>\n"
                        msg_coins += f"  • Avg Daily Profit (Net): <b>{cdata.get('avg_daily_profit', 0)}</b> USDT\n"
                        msg_coins += f"  • Max Position Size: <b>{cdata.get('max_position_size', 0)}</b> USDT\n"
                        msg_coins += f"  • Risk/Reward Ratio: <b>{cdata.get('risk_reward_ratio', 0)}</b>\n"
                        msg_coins += f"  • DRME: <b>{cdata.get('DRME', 0)}</b>\n"
                        msg_coins += f"  • MDME: <b>{cdata.get('MDME', 0)}</b>\n\n"
                        await callback.message.answer(msg_coins, parse_mode="HTML")
                except Exception as e:
                    logger.error(f"Error reading per_coin data for {symbol}: {e}")

            from ANALYTICS.plotter import generate_coin_analytics
            try:
                plot_path = generate_coin_analytics(symbol)
                if plot_path and os.path.exists(plot_path):
                    await callback.message.answer_photo(FSInputFile(plot_path), caption=f"📈 Analytics: {symbol}")
                else:
                    await callback.message.answer(f"❌ Не удалось сгенерировать график для {symbol}.")
            except Exception as e:
                logger.error(f"Error generating coin analytics: {e}")
                await callback.message.answer(f"❌ Ошибка генерации графика для {symbol}: {e}")

        @self.dp.message(F.text == "⚙️ Set Coins")
        async def on_set_coins(message: Message, state: FSMContext):
            await state.clear()
            await message.answer("<b>Управление монетами:</b>\nВыберите действие в меню.", reply_markup=self._get_set_coins_keyboard(), parse_mode="HTML")

        @self.dp.message(F.text == "🔙 Back")
        async def on_back_from_coins(message: Message, state: FSMContext):
            await state.clear()
            status = "⏸️ Paused" if self.bot_core.is_paused else "▶️ Running"
            text = f"<b>Control Panel</b>\nCurrent Status: {status}"
            await message.answer(text, reply_markup=self._get_main_keyboard(), parse_mode="HTML")

        @self.dp.message(F.text == "❓ Help")
        async def on_help_coins(message: Message, state: FSMContext):
            help_text = (
                "<b>Гайд по управлению монетами:</b>\n\n"
                "➕ <b>Add</b>: Добавить новую монету. Бот возьмет шаблон из <code>_base.json</code> и сразу запустит монету в работу.\n"
                "✏️ <b>Edit</b>: Редактировать настройки запущенной монеты. Бот пришлет текущий конфиг, вы его правите и отправляете обратно.\n"
                "🗑️ <b>Del</b>: Удалить монету. Бот моментально удалит её из всех систем и перестанет следить за ней (ордера на бирже останутся).\n"
                "📄 <b>_base</b>: Отредактировать глобальный шаблон (применяется при добавлении новых монет)."
            )
            await message.answer(help_text, parse_mode="HTML")

        # =========================================================
        # ADD SYMBOL
        # =========================================================
        @self.dp.message(F.text == "➕ Add")
        async def on_add_btn(message: Message, state: FSMContext):
            await message.answer("Введите символ монеты для добавления (например: WIFUSDT):", reply_markup=self._get_back_keyboard())
            await state.set_state(TGStates.waiting_for_add_symbol)

        @self.dp.message(TGStates.waiting_for_add_symbol)
        async def process_add_symbol(message: Message, state: FSMContext):
            if not message.text:
                await message.answer("❌ Пожалуйста, отправьте текстовое сообщение.")
                return
            if message.text == "🔙 Back":
                return await on_set_coins(message, state)
                
            symbol = message.text.strip().upper()
            if not symbol.endswith("USDT"):
                await message.answer("Символ должен заканчиваться на USDT. Попробуйте еще раз:")
                return

            # Apply base template automatically and add to BotCore
            import json
            base_data = self.template_manager.apply_tg_template(json.dumps({"symbol": symbol}))
            # Wait, apply_tg_template parses JSON, but if we don't pass full JSON it might fail or reset.
            # We need to manually construct the config or use a helper.
            # Actually, `template_manager.generate_tg_template` gets base.
            base_str = self.template_manager.generate_tg_template(symbol)
            success, msg = self.template_manager.apply_tg_template(base_str)
            
            if success:
                if symbol not in self.bot_core.symbols:
                    await self.bot_core.add_symbol(symbol)
                await message.answer(f"✅ Монета {symbol} успешно добавлена и запущена в работу на основе базового шаблона!", reply_markup=self._get_set_coins_keyboard())
                await state.clear()
            else:
                await message.answer(f"❌ Ошибка добавления: {msg}", reply_markup=self._get_set_coins_keyboard())
                await state.clear()

        # =========================================================
        # DEL SYMBOL
        # =========================================================
        @self.dp.message(F.text == "🗑️ Del")
        async def on_del_btn(message: Message, state: FSMContext):
            active_coins = ", ".join(self.bot_core.symbols)
            if not active_coins:
                active_coins = "Нет активных монет"
            await message.answer(f"Активные монеты: <b>{active_coins}</b>\n\nВведите символ монеты для УДАЛЕНИЯ (например: WIFUSDT):", parse_mode="HTML", reply_markup=self._get_back_keyboard())
            await state.set_state(TGStates.waiting_for_del_symbol)

        @self.dp.message(TGStates.waiting_for_del_symbol)
        async def process_del_symbol(message: Message, state: FSMContext):
            if not message.text:
                await message.answer("❌ Пожалуйста, отправьте текстовое сообщение.")
                return
            if message.text == "🔙 Back":
                return await on_set_coins(message, state)
                
            symbol = message.text.strip().upper()
            if symbol not in self.bot_core.symbols:
                await message.answer(f"❌ Монета {symbol} не найдена в активных.", reply_markup=self._get_set_coins_keyboard())
                await state.clear()
                return

            await self.bot_core.delete_symbol(symbol)
            await message.answer(f"✅ Монета {symbol} успешно удалена из всех систем бота (ордера на бирже оставлены без изменений).", reply_markup=self._get_set_coins_keyboard())
            await state.clear()

        # =========================================================
        # EDIT SYMBOL
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
            
            buttons = []
            row = []
            for coin in sorted(active_coins):
                row.append(InlineKeyboardButton(text=f"🪙 {coin}", callback_data=f"edit_coin_{coin}"))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
            buttons.append([InlineKeyboardButton(text="🔙 Close", callback_data="edit_side_cancel")])
            
            text = "Выберите монету для редактирования:"
            if edit:
                await message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
            else:
                await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

        @self.dp.callback_query(F.data.startswith("edit_coin_") & ~F.data.startswith("edit_coin_list"))
        async def process_edit_coin_select(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            symbol = callback.data.replace("edit_coin_", "")
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="🟢 LONG", callback_data=f"edit_side_{symbol}_LONG"),
                    InlineKeyboardButton(text="🔴 SHORT", callback_data=f"edit_side_{symbol}_SHORT")
                ],
                [InlineKeyboardButton(text="🔙 Back", callback_data="edit_coin_list")]
            ])
            await callback.message.edit_text(f"⚙️ <b>{symbol}</b>\nВыберите направление для редактирования:", reply_markup=keyboard, parse_mode="HTML")

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

            runtime_path = self.template_manager.runtime_dir / f"{symbol.lower()}.json"
            if not runtime_path.exists():
                await callback.message.answer(f"❌ Конфиг {symbol} не найден.")
                return

            import json
            try:
                rt_data = json.loads(runtime_path.read_text(encoding="utf-8"))
                side_data = rt_data.get(side, {})
                
                en = side_data.get("enable", False)
                sz = side_data.get("invest_size", 0)
                lev = side_data.get("leverage", 0)
                
                # Fetch all grid levels for display
                grid_cfg = side_data.get("grid", {})
                grid_lines = ["📊 <b>Grid (Avg):</b>"]
                for k in sorted(grid_cfg.keys(), key=lambda x: int(x)):
                    lvl_data = grid_cfg[k]
                    vol = lvl_data.get('volume', 0)
                    ind = lvl_data.get('indent', 0)
                    si = lvl_data.get('super_indent')
                    si_text = f" (Super: {si})" if si is not None else ""
                    grid_lines.append(f"  • Lvl {k}: Vol <b>{vol}</b> | Indent <b>{ind}</b>{si_text}")
                
                # Fetch all TP levels for display
                tp_cfg = side_data.get("tp_map", {})
                tp_lines = ["🎯 <b>Take Profits (TP):</b>"]
                for k in sorted(tp_cfg.keys(), key=lambda x: int(x)):
                    lvl_data = tp_cfg[k]
                    ind = lvl_data.get('indent', 0)
                    fb = lvl_data.get('fallback_indent', 0)
                    tp_lines.append(f"  • Lvl {k}: Indent <b>{ind}</b> | Fallback <b>{fb}</b>")

                msg_text = (
                    f"⚙️ <b>{symbol} - {side}</b>\n"
                    f"Status: {'✅ On' if en else '❌ Off'}\n\n"
                    f"💰 Invest Size: <b>{sz}</b> USDT (Lev: {lev}x)\n\n"
                    + "\n".join(grid_lines) + "\n\n"
                    + "\n".join(tp_lines)
                )
                
                toggle_text = "⏸ Отключить (ON)" if en else "▶️ Включить (OFF)"
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=toggle_text, callback_data=f"edit_act_toggle_{symbol}_{side}")],
                    [InlineKeyboardButton(text="💰 Edit Invest Size", callback_data=f"edit_act_size_{symbol}_{side}")],
                    [InlineKeyboardButton(text="📊 Edit Set Avg", callback_data=f"edit_act_avg_{symbol}_{side}")],
                    [InlineKeyboardButton(text="🎯 Edit Set TP", callback_data=f"edit_act_tp_{symbol}_{side}")],
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
            if not message.text:
                await message.answer("❌ Пожалуйста, отправьте текстовое сообщение.")
                return
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
            
            runtime_cfg = self.bot_core.runtime_configs.get(symbol, {}).get(side, {})
            grid_cfg = runtime_cfg.get("grid", {}).get(str(lvl), {})
            cur_vol = grid_cfg.get("volume", 0)
            cur_ind = grid_cfg.get("indent", 0)
            
            await state.update_data(edit_symbol=symbol, edit_side=side, edit_lvl=lvl)
            msg = f"Усреднение (Set Avg) для <b>{symbol} {side} | Уровень {lvl}</b>.{warn}\n\n"
            msg += f"Текущие значения:\n🔹 Объем: <b>{cur_vol}</b>\n🔹 Индент: <b>{cur_ind}</b>\n\nВыберите параметр для изменения:"
            
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
            if not message.text:
                await message.answer("❌ Пожалуйста, отправьте текстовое сообщение.")
                return
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
                        import asyncio
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
            cur_ind = tp_cfg.get("indent", 0)
            cur_fb = tp_cfg.get("fallback_indent", 0)
            
            await state.update_data(edit_symbol=symbol, edit_side=side, edit_lvl=lvl)
            msg = f"Установка тейк-профита для <b>{symbol} {side} | Уровень {lvl}</b>.{warn}\n\n"
            msg += f"Текущие значения:\n🔹 Indent: <b>{cur_ind}</b>\n🔹 Fallback: <b>{cur_fb}</b>\n\nВыберите параметр для изменения:"
            
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
            if not message.text:
                await message.answer("❌ Пожалуйста, отправьте текстовое сообщение.")
                return
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
            from c_utils import Utils
            base_data = Utils.read_json_file(self.template_manager.base_file)
            import json
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
                import io
                file = await self.bot.get_file(message.document.file_id)
                out = io.BytesIO()
                await self.bot.download_file(file.file_path, out)
                json_str = out.getvalue().decode('utf-8')
            elif message.text:
                json_str = message.text.strip()
            else:
                await message.answer("❌ Пожалуйста, отправьте текстовое сообщение или .json файл.")
                return
                
            import json
            try:
                new_base = json.loads(json_str)
                
                # Валидация шаблона
                for side in ("LONG", "SHORT"):
                    if side in new_base:
                        grid = new_base[side].get("grid", {})
                        tp_map = new_base[side].get("tp_map", {})
                        if grid and tp_map and len(grid) != len(tp_map):
                            await message.answer(f"❌ ОШИБКА КОНФИГУРАЦИИ ({side}): Количество уровней grid ({len(grid)}) не совпадает с tp_map ({len(tp_map)}).\nШаблон НЕ сохранен!")
                            return
                            
                from c_utils import Utils
                Utils.write_json_file(self.template_manager.base_file, new_base)
                await message.answer("✅ Базовый шаблон успешно обновлен!", reply_markup=self._get_set_coins_keyboard())
                await state.clear()
            except Exception as e:
                await message.answer(f"❌ Ошибка JSON: {e}\nИсправьте и отправьте снова.")

        # =========================================================
        # EDIT Super Grid
        # =========================================================
        @self.dp.message(F.text == "🔧 Super Grid")
        async def on_super_grid_btn(message: Message, state: FSMContext):
            from consts import DATA_DIR
            from c_utils import Utils
            import json
            app_json_path = DATA_DIR / "app.json"
            app_data = Utils.read_json_file(app_json_path)
            
            # Если нет секции Super Grid, создадим базовую
            if "super_grid" not in app_data:
                app_data["super_grid"] = {
                    "enabled": True,
                    "_comment_timeframe": "Valid values: 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d, 3d, 1w, 1M",
                    "timeframe": "1d",
                    "window": 14,
                    "multiplier": 1.0,
                    "min_volatility_pct": 5.0,
                    "update_interval_hours": 12
                }
            
            super_grid_str = json.dumps(app_data["super_grid"], indent=4)
            dump_path = os.path.join("logs", "Super Grid_edit.json")
            os.makedirs("logs", exist_ok=True)
            with open(dump_path, "w", encoding="utf-8") as f:
                f.write(super_grid_str)
                
            await message.answer("<b>Текущие настройки Super Grid (Volatility):</b>\nОтредактируйте файл и отправьте обратно документом (или текстом).", reply_markup=self._get_back_keyboard(), parse_mode="HTML")
            await message.answer_document(FSInputFile(dump_path))
            await state.set_state(TGStates.waiting_for_super_grid_json)

        @self.dp.message(TGStates.waiting_for_super_grid_json)
        async def process_super_grid_json(message: Message, state: FSMContext):
            if message.text and message.text == "🔙 Back":
                await state.clear()
                status = "⏸️ Paused" if self.bot_core.is_paused else "▶️ Running"
                await message.answer(f"<b>Control Panel</b>\nCurrent Status: {status}", reply_markup=self._get_main_keyboard(), parse_mode="HTML")
                return
                
            json_str = ""
            if message.document:
                import io
                file = await self.bot.get_file(message.document.file_id)
                out = io.BytesIO()
                await self.bot.download_file(file.file_path, out)
                json_str = out.getvalue().decode('utf-8')
            elif message.text:
                json_str = message.text.strip()
            else:
                await message.answer("❌ Пожалуйста, отправьте текстовое сообщение или .json файл.")
                return
                
            import json
            try:
                new_super_grid = json.loads(json_str)
                from c_utils import Utils
                from consts import DATA_DIR, _CFG
                app_json_path = DATA_DIR / "app.json"
                app_data = Utils.read_json_file(app_json_path)
                app_data["super_grid"] = new_super_grid
                Utils.write_json_file(app_json_path, app_data)
                
                # Обновляем в памяти _CFG
                _CFG["super_grid"] = new_super_grid
                
                # Заставляем пересчитаться
                if hasattr(self.bot_core, 'volatility_manager') and self.bot_core.volatility_manager.is_running:
                    asyncio.create_task(self.bot_core.volatility_manager.process_all())
                    
                await message.answer("✅ Настройки Super Grid успешно обновлены! Перерасчет запущен.", reply_markup=self._get_main_keyboard())
                await state.clear()
            except Exception as e:
                await message.answer(f"❌ Ошибка JSON: {e}\nИсправьте и отправьте снова.")

        # =========================================================
        # FLAT SCREENER
        # =========================================================
        @self.dp.message(F.text == "📉 FLAT SCREENER")
        async def on_flat_screener_cmd(message: Message, state: FSMContext):
            await state.clear()
            
            from c_utils import Utils
            from consts import DATA_DIR
            app_cfg = Utils.read_json_file(DATA_DIR / "app.json")
            flat_cfg = app_cfg.get("flat_scanner", {})
            max_flat = flat_cfg.get("max_flatness_pct", 35.0)
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📉 Запустить Flat Screener", callback_data="cb_run_flat_scanner")],
                [InlineKeyboardButton(text="⚙️ Изменить порог (Flatness)", callback_data="cb_edit_flatness")]
            ])
            text = (
                "<b>Flat Screener (Поиск боковика)</b>\n\n"
                f"🎚 <b>Текущий порог отсечения:</b> {max_flat}%\n\n"
                "⚠️ <b>ВНИМАНИЕ:</b> Перед запуском этого скринера рекомендуется сперва обновить слепок данных, "
                "запустив скринер волатильности (ATR SCREENER).\n\n"
                "💡 <i>Подсказка по Flatness:</i>\n"
                "Значение от 0.0 до 100.0. Чем ближе к 0, тем идеальнее пара стоит в волатильном коридоре (боковике) "
                "без направленного тренда. Значение показывает отношение чистого смещения цены к ее полному размаху.\n\n"
                "Например, 15.0 задаст очень жесткие рамки для идеального боковика."
            )
            await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

        @self.dp.callback_query(F.data == "cb_run_flat_scanner")
        async def process_cb_run_flat_scanner(callback: CallbackQuery, state: FSMContext):
            from consts import DATA_DIR, CACHE_DIR
            if not (CACHE_DIR / "volatile_symbols.txt").exists() and not (CACHE_DIR / "volatile_symbols.json").exists():
                await callback.answer("🛑 Нет данных от ATR Screener! Сперва запустите его.", show_alert=True)
                return

            await callback.answer("Запускаю Flat Screener...")
            msg = await callback.message.answer("⏳ Анализ плоских коридоров запущен, пожалуйста подождите...")
            
            try:
                import sys
                import subprocess
                from consts import DATA_DIR
                output_path = DATA_DIR / "flat_symbols.txt"
                if output_path.exists():
                    try:
                        os.remove(output_path)
                    except:
                        pass
                        
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "test_flat_screeners.py",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=os.getcwd()
                )
                stdout, stderr = await process.communicate()
                
                from consts import CACHE_DIR
                output_path = CACHE_DIR / "flat_symbols.txt"
                if output_path.exists():
                    await msg.delete()
                    await callback.message.answer_document(FSInputFile(output_path), caption="✅ Flat Screener завершен. Результаты в файле.")
                else:
                    await msg.edit_text(f"❌ Ошибка сканирования (файл не создан).\n\nЛоги:\n{stderr.decode('utf-8')}")
                    
            except Exception as e:
                logger.error(f"Error running flat scanner: {e}")
                await msg.edit_text(f"❌ Системная ошибка при запуске Flat скринера: {e}")

        # =========================================================
        # FLAT SCREENER V2
        # =========================================================
        @self.dp.message(F.text == "📉 FLAT V2 (Боковик)")
        async def on_flat_v2_cmd(message: Message, state: FSMContext):
            await state.clear()
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📉 Запустить Flat Screener V2", callback_data="cb_run_flat_v2")]
            ])
            text = (
                "<b>Flat Screener V2 (Истинный Боковик)</b>\n\n"
                "⚠️ <b>ВНИМАНИЕ:</b> Использует данные от скринера волатильности (ATR SCREENER).\n\n"
                "💡 <i>Логика:</i>\n"
                "Этот скринер ищет настоящие боковики, используя два метода одновременно:\n"
                "1. <b>Пинг-Понг:</b> Монета должна коснуться верхних 25% диапазона и нижних 25% как минимум 2 раза по очереди.\n"
                "2. <b>Пила:</b> Цена должна пересечь свою среднюю скользящую (SMA) как минимум 3 раза за окно.\n\n"
                "Это отлично отсеивает 'V-образные' шпильки и гарантирует пилообразное движение цены."
            )
            await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

        @self.dp.callback_query(F.data == "cb_run_flat_v2")
        async def process_cb_run_flat_v2(callback: CallbackQuery, state: FSMContext):
            from consts import CACHE_DIR
            if not (CACHE_DIR / "volatile_symbols.txt").exists() and not (CACHE_DIR / "volatile_symbols.json").exists():
                await callback.answer("🛑 Нет данных от ATR Screener! Сперва запустите его.", show_alert=True)
                return

            await callback.answer("Запускаю Flat Screener V2...")
            msg = await callback.message.answer("⏳ Анализ плоских коридоров V2 запущен, пожалуйста подождите...")
            
            try:
                import sys
                import subprocess
                import os
                from aiogram.types import FSInputFile
                
                output_path = CACHE_DIR / "flat_symbols_v2.txt"
                if output_path.exists():
                    try:
                        os.remove(output_path)
                    except:
                        pass
                        
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "test_flat_screeners_v2.py",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=os.getcwd()
                )
                stdout, stderr = await process.communicate()
                
                if output_path.exists():
                    await msg.delete()
                    await callback.message.answer_document(FSInputFile(output_path), caption="✅ Flat Screener V2 завершен. Результаты в файле.")
                else:
                    await msg.edit_text(f"❌ Ошибка сканирования (файл не создан).\n\nЛоги:\n{stderr.decode('utf-8')}")
            except Exception as e:
                await msg.edit_text(f"❌ Ошибка выполнения скринера: {e}")

        @self.dp.callback_query(F.data == "cb_edit_flatness")
        async def process_cb_edit_flatness(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            await callback.message.answer(
                "Введите новое значение порога (max_flatness_pct) в процентах, например 15.0 или 35.0:\n\n"
                "<i>Для отмены нажмите кнопку Back.</i>",
                reply_markup=self._get_back_keyboard(),
                parse_mode="HTML"
            )
            await state.set_state(TGStates.waiting_for_flatness_val)

        @self.dp.message(TGStates.waiting_for_flatness_val)
        async def process_flatness_val(message: Message, state: FSMContext):
            if not message.text:
                await message.answer("❌ Пожалуйста, отправьте текстовое сообщение.")
                return
            if message.text and message.text == "🔙 Back":
                await state.clear()
                await message.answer("Отменено.", reply_markup=self._get_main_keyboard())
                return
                
            try:
                val = float(message.text.strip())
                if val < 0.0 or val > 100.0:
                    raise ValueError("Значение должно быть от 0 до 100")
                    
                from c_utils import Utils
                from consts import DATA_DIR
                app_json_path = DATA_DIR / "app.json"
                app_data = Utils.read_json_file(app_json_path)
                
                if "flat_scanner" not in app_data:
                    app_data["flat_scanner"] = {}
                app_data["flat_scanner"]["max_flatness_pct"] = val
                
                Utils.write_json_file(app_json_path, app_data)
                
                await message.answer(f"✅ Порог Flat Screener успешно обновлен на {val}%!", reply_markup=self._get_main_keyboard())
                await state.clear()
            except Exception as e:
                await message.answer(f"❌ Ошибка: {e}. Пожалуйста, введите корректное число (например, 20.0).")

        # =========================================================
        # /sonnik - VOLATILITY SCANNER
        # =========================================================
        @self.dp.message(Command("sonnik"))
        @self.dp.message(F.text == "🔍 ATR SCREENER")
        async def on_sonnik_cmd(message: Message, state: FSMContext):
            await state.clear()
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Запустить фильтр (Run Scanner)", callback_data="cb_run_scanner")],
                [InlineKeyboardButton(text="⚙️ Изменить настройки фильтра", callback_data="cb_edit_scanner_config")]
            ])
            await message.answer("<b>Сонник (Volatility Scanner)</b>\nВыберите действие:", reply_markup=keyboard, parse_mode="HTML")

        @self.dp.callback_query(F.data == "cb_run_scanner")
        async def process_cb_run_scanner(callback: CallbackQuery, state: FSMContext):
            await callback.answer("Запускаю сканирование... Это займет некоторое время.")
            msg = await callback.message.answer("⏳ Сканирование волатильности запущено, пожалуйста подождите...")
            
            try:
                import sys
                import subprocess
                from consts import CACHE_DIR
                output_path = CACHE_DIR / "volatile_symbols.txt"
                if output_path.exists():
                    try:
                        os.remove(output_path)
                    except:
                        pass
                        
                # Запускаем как модуль из корня, чтобы корректно подтянулись импорты (c_log и др.)
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "CORE.ADVANCED.volatility_scanner",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=os.getcwd()
                )
                stdout, stderr = await process.communicate()
                
                from consts import CACHE_DIR
                output_path = CACHE_DIR / "volatile_symbols.txt"
                if output_path.exists():
                    await msg.delete()
                    await callback.message.answer_document(FSInputFile(output_path), caption="✅ Сканирование завершено. Результаты в файле.")
                else:
                    await msg.edit_text(f"❌ Ошибка сканирования (файл не создан).\n\nЛоги:\n{stderr.decode('utf-8')}")
                    
            except Exception as e:
                logger.error(f"Error running scanner: {e}")
                await msg.edit_text(f"❌ Системная ошибка при запуске сканера: {e}")

        @self.dp.callback_query(F.data == "cb_edit_scanner_config")
        async def process_cb_edit_scanner_config(callback: CallbackQuery, state: FSMContext):
            await callback.answer()
            from c_utils import Utils
            from consts import DATA_DIR
            import json
            
            config_path = DATA_DIR / "app.json"
            cfg = {
                "timeframe": "1w",
                "window": 8,
                "min_volatility_pct": 15.0,
                "max_volatility_pct": None,
                "strict_window": True
            }
            if config_path.exists():
                try:
                    data = Utils.read_json_file(config_path)
                    if "volatility_scanner" in data:
                        cfg.update(data["volatility_scanner"])
                except:
                    pass
                    
            dump_path = os.path.join("logs", "scanner_app.json")
            os.makedirs("logs", exist_ok=True)
            with open(dump_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4)
                
            await callback.message.answer("<b>Текущие настройки сканера:</b>\nОтредактируйте файл и отправьте обратно документом (или текстом).", reply_markup=self._get_back_keyboard(), parse_mode="HTML")
            await callback.message.answer_document(FSInputFile(dump_path))
            await state.set_state(TGStates.waiting_for_scanner_json)

        @self.dp.message(TGStates.waiting_for_scanner_json)
        async def process_scanner_json(message: Message, state: FSMContext):
            if message.text and message.text == "🔙 Back":
                await state.clear()
                await message.answer("Отменено.", reply_markup=self._get_main_keyboard())
                return
                
            json_str = ""
            if message.document:
                import io
                file = await self.bot.get_file(message.document.file_id)
                out = io.BytesIO()
                await self.bot.download_file(file.file_path, out)
                json_str = out.getvalue().decode('utf-8')
            elif message.text:
                json_str = message.text.strip()
            else:
                await message.answer("❌ Пожалуйста, отправьте текстовое сообщение или .json файл.")
                return
                
            import json
            try:
                new_cfg = json.loads(json_str)
                from c_utils import Utils
                from consts import DATA_DIR
                
                config_path = DATA_DIR / "app.json"
                app_data = {}
                if config_path.exists():
                    app_data = Utils.read_json_file(config_path)
                
                app_data["volatility_scanner"] = new_cfg
                Utils.write_json_file(config_path, app_data)
                
                await message.answer("✅ Настройки сканера успешно обновлены!", reply_markup=self._get_main_keyboard())
                await state.clear()
            except Exception as e:
                await message.answer(f"❌ Ошибка JSON: {e}\nИсправьте и отправьте снова.")

        @self.dp.message(F.text == "🔔 SET INFO")
        async def on_set_info(message: Message, state: FSMContext):
            await state.clear()
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Сбросить флаги", callback_data="reset_notif_flags")],
                [
                    InlineKeyboardButton(text="✏️ Ред. Отриц. порог", callback_data="edit_notif_neg"),
                    InlineKeyboardButton(text="✏️ Ред. Полож. порог", callback_data="edit_notif_pos")
                ]
            ])
            
            from consts import DATA_DIR
            from c_utils import Utils
            app_cfg = Utils.read_json_file(DATA_DIR / "app.json")
            notif_cfg = app_cfg.get("notifications", {})
            enabled = notif_cfg.get("enabled", False)
            neg_th = notif_cfg.get("negative_threshold", -100.0)
            pos_th = notif_cfg.get("positive_threshold", 100.0)
            
            neg_flag = self.bot_core.notifier.neg_flag if hasattr(self.bot_core, "notifier") else False
            pos_flag = self.bot_core.notifier.pos_flag if hasattr(self.bot_core, "notifier") else False
            
            text = (
                f"<b>Управление уведомлениями:</b>\n"
                f"Статус: {'✅ Вкл' if enabled else '❌ Выкл'}\n"
                f"Отрицательный порог: {neg_th} USDT (Сработал: {neg_flag})\n"
                f"Положительный порог: {pos_th} USDT (Сработал: {pos_flag})"
            )
            await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

        @self.dp.callback_query(F.data == "reset_notif_flags")
        async def process_reset_notif_flags(callback: CallbackQuery, state: FSMContext):
            if hasattr(self.bot_core, "notifier"):
                self.bot_core.notifier.reset_flags()
                await callback.message.edit_text("✅ Флаги уведомлений успешно сброшены!")
            else:
                await callback.answer("Notifier module not found.", show_alert=True)

        @self.dp.callback_query(F.data == "edit_notif_neg")
        async def process_edit_notif_neg(callback: CallbackQuery, state: FSMContext):
            await state.set_state(TGStates.waiting_for_notif_neg)
            await callback.message.answer("Введите новый отрицательный порог (например, -150.0):")
            await callback.answer()

        @self.dp.callback_query(F.data == "edit_notif_pos")
        async def process_edit_notif_pos(callback: CallbackQuery, state: FSMContext):
            await state.set_state(TGStates.waiting_for_notif_pos)
            await callback.message.answer("Введите новый положительный порог (например, 200.0):")
            await callback.answer()

        @self.dp.message(TGStates.waiting_for_notif_neg)
        async def process_notif_neg_val(message: Message, state: FSMContext):
            if not message.text:
                await message.answer("❌ Пожалуйста, отправьте текстовое сообщение.")
                return
            try:
                val = float(message.text)
                val = -abs(val)  # Validation: always negative or zero
                
                from consts import DATA_DIR
                from c_utils import Utils
                app_cfg = Utils.read_json_file(DATA_DIR / "app.json")
                if "notifications" not in app_cfg: app_cfg["notifications"] = {}
                app_cfg["notifications"]["negative_threshold"] = val
                Utils.write_json_file(DATA_DIR / "app.json", app_cfg)
                
                await message.answer(f"✅ Отрицательный порог применен со значением {val} USDT.")
                await state.clear()
            except ValueError:
                await message.answer("❌ Ошибка! Введите числовое значение.")

        @self.dp.message(TGStates.waiting_for_notif_pos)
        async def process_notif_pos_val(message: Message, state: FSMContext):
            if not message.text:
                await message.answer("❌ Пожалуйста, отправьте текстовое сообщение.")
                return
            try:
                val = float(message.text)
                val = abs(val)  # Validation: always positive or zero
                
                from consts import DATA_DIR
                from c_utils import Utils
                app_cfg = Utils.read_json_file(DATA_DIR / "app.json")
                if "notifications" not in app_cfg: app_cfg["notifications"] = {}
                app_cfg["notifications"]["positive_threshold"] = val
                Utils.write_json_file(DATA_DIR / "app.json", app_cfg)
                
                await message.answer(f"✅ Положительный порог применен со значением {val} USDT.")
                await state.clear()
            except ValueError:
                await message.answer("❌ Ошибка! Введите числовое значение.")

    async def start(self):
        logger.info("Starting Telegram Receiver...")
        await self.dp.start_polling(self.bot)
