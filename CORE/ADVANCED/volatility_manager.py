# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\CORE\ADVANCED\volatility_manager.py
# Role: volatility_manager.py module

# ==============================================================================
# Path: CORE/ADVANCED/volatility_manager.py
# python -m CORE.ADVANCED.volatility_manager
# Role: Менеджер динамической волатильности (Super Indent)
# ==============================================================================

import asyncio
import time
import json
from pathlib import Path
from c_log import UnifiedLogger
from consts import DATA_DIR, _CFG

from c_utils import Utils
import asyncio
import sys
from pathlib import Path
from API.BINANCE.client import BinanceClient
from consts import _CFG
import json
logger = UnifiedLogger("VolatilityManager")

class VolatilityManager:
    def __init__(self, bot_core):
        self.bot_core = bot_core
        self.client = bot_core.client
        self._task = None
        self.is_running = False

    def start(self):
        if self._task is None or self._task.done():
            self.is_running = True
            self._task = asyncio.create_task(self._loop())

    def stop(self):
        self.is_running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _loop(self):
        logger.info("[VolatilityManager] Started background loop.")
        while self.is_running:
            try:
                await self.process_all()
            except Exception as e:
                logger.error(f"[VolatilityManager] Error in loop: {e}")
            
            # Wait for next update interval
            app_data = Utils.read_json_file(DATA_DIR / "app.json")
            if "super_grid" in app_data:
                app_cfg = app_data["super_grid"]
                interval_hours = app_cfg["update_interval_hours"]
            else:
                interval_hours = 12
            wait_sec = interval_hours * 3600
            if wait_sec <= 0:
                wait_sec = 3600 # default 1 hour fallback
                
            await asyncio.sleep(wait_sec)

    async def process_all(self):
        app_data = Utils.read_json_file(DATA_DIR / "app.json")
        if "super_grid" not in app_data:
            return
            
        app_cfg = app_data["super_grid"]
        
        is_enabled = app_cfg["enabled"]

        symbols = list(self.bot_core.symbols)
        if not symbols:
            return

        timeframe = app_cfg.get("timeframe", "1d")
        window = app_cfg.get("window", 14)
        multiplier = app_cfg.get("multiplier", 1.0)
        min_volatility_pct = app_cfg.get("min_volatility_pct", 15.0)
        max_volatility_pct = app_cfg.get("max_volatility_pct", None)

        if is_enabled:
            logger.info(f"[VolatilityManager] Processing {len(symbols)} symbols. TF={timeframe}, window={window}, min_vol={min_volatility_pct}%, max_vol={max_volatility_pct}%")
        else:
            logger.info(f"[VolatilityManager] Super Grid is disabled. Resetting any active super_indents for {len(symbols)} symbols.")

        stats_output = {}

        for symbol in symbols:
            if not self.is_running:
                break
                
            is_advanced = is_enabled
            adjusted_vol = 0.0
            count = 0
            
            if is_advanced:
                safe_window = min(window, 1500)
                try:
                    klines = await self.bot_core.client.get_klines(symbol, timeframe, safe_window)
                    if not klines or len(klines) == 0:
                        logger.warning(f"[VolatilityManager] [{symbol}] Failed to fetch klines or empty.")
                        continue
                except Exception as e:
                    logger.error(f"[VolatilityManager] [{symbol}] API error fetching klines: {e}")
                    continue
                    
                    
                # Calculate average volatility
                total_vol = 0.0
                for k in klines:
                    high = k.get("high", 0.0)
                    low = k.get("low", 0.0)
                    if low > 0:
                        vol = ((high / low) - 1) * 100
                        total_vol += vol
                        count += 1
                
                if count == 0:
                    continue
                    
                if count < window:
                    reason = "Ограничение Binance API 1500 свечей" if window > 1500 and count == 1500 else "монета может быть новой"
                    logger.warning(f"[VolatilityManager] [{symbol}] Запрошено {window} свечей, но получено только {count} ({reason}). Расчет идет по доступным {count} свечам.")
                    
                avg_vol = total_vol / count
                adjusted_vol = avg_vol * multiplier
                
                if min_volatility_pct is not None and adjusted_vol <= min_volatility_pct:
                    logger.info(f"[VolatilityManager] [{symbol}] Игнор: Расчитанная волатильность {adjusted_vol:.2f}% ниже или равна минимуму {min_volatility_pct}%. Возвращаемся к использованию стандартных настроек индента.")
                    is_advanced = False
                elif max_volatility_pct is not None and adjusted_vol >= max_volatility_pct:
                    logger.info(f"[VolatilityManager] [{symbol}] Игнор: Расчитанная волатильность {adjusted_vol:.2f}% выше или равна максимуму {max_volatility_pct}%. Возвращаемся к использованию стандартных настроек индента.")
                    is_advanced = False
                # We will still process the file to erase super_indent
                
            # Need to update runtime configuration
            runtime_path = DATA_DIR / "runtime" / f"{symbol.lower()}.json"
            if not runtime_path.exists():
                continue
                
            try:
                with open(runtime_path, 'r', encoding='utf-8') as f:
                    rt_data = json.load(f)
                    
                modified = False
                for side in ["LONG", "SHORT"]:
                    if side in rt_data and "grid" in rt_data[side]:
                        grid = rt_data[side]["grid"]
                        if not grid:
                            continue
                            
                        # Find the last element (max index)
                        max_idx = str(max(int(idx) for idx in grid.keys()))
                        last_indent = abs(float(grid[max_idx].get("indent", 0.0)))
                        
                        if last_indent == 0:
                            continue
                            
                        ratio = adjusted_vol / last_indent if is_advanced else 1.0
                        
                        # Apply to all elements in grid
                        for idx, el in grid.items():
                            if is_advanced:
                                orig_indent = float(el.get("indent", 0.0))
                                new_indent = orig_indent * ratio
                                el["super_indent"] = round(new_indent, 4)
                                if not el.get("is_active", False):
                                    el["price"] = None
                            else:
                                if el.get("super_indent") is not None:
                                    el["super_indent"] = None
                                    if not el.get("is_active", False):
                                        el["price"] = None
                                        
                        modified = True
                        
                if modified:
                    with open(runtime_path, 'w', encoding='utf-8') as f:
                        json.dump(rt_data, f, indent=4)
                        
                    if is_advanced:
                        logger.info(f"[VolatilityManager] [{symbol}] Переход на Super Grid настройки. Волатильность={adjusted_vol:.2f}%. Коэффициент={ratio:.4f}. Сетка обновлена (super_indent).")
                    else:
                        logger.debug(f"[VolatilityManager] [{symbol}] Сетка сброшена на стандартные настройки (super_indent удален).")
                        
                    stats_output[symbol] = {
                        "is_advanced": is_advanced,
                        "timeframe": timeframe,
                        "window": window,
                        "candles_fetched": count,
                        "volatility_pct": round(adjusted_vol, 2),
                        "ratio": round(ratio, 4) if is_advanced else 1.0
                    }
                    
                    # Пробрасываем изменения прямо в запущенную FSM (сбрасываем price у неактивных уровней)
                    if hasattr(self.bot_core, "fsm_states") and symbol in self.bot_core.fsm_states:
                        for side, state in self.bot_core.fsm_states[symbol].items():
                            if hasattr(state, "grid"):
                                # Находим свежий grid в rt_data
                                fresh_grid = rt_data.get(side, {}).get("grid", {})
                                
                                # Обновляем in-memory state.grid
                                for lvl_str, lvl_data in state.grid.items():
                                    # Подтягиваем новый super_indent всегда
                                    if lvl_str in fresh_grid:
                                        lvl_data["super_indent"] = fresh_grid[lvl_str].get("super_indent")
                                        
                                    if state.in_position and not lvl_data.get("is_active"):
                                        # Если ордер еще не исполнен, сбрасываем цену, чтобы avg_manager пересчитал
                                        lvl_data["price"] = None
                                
                                if state.in_position:
                                    # Сбрасываем кэш следующей цели
                                    state.next_avg_price = None
                                    logger.info(f"[VolatilityManager] [{symbol}] {side} Горячая подгрузка: неактивные прайсы сброшены на None. Ожидание перерасчета сетки.")
                    
            except Exception as e:
                logger.error(f"[VolatilityManager] [{symbol}] Error updating config: {e}")
                
        # Reload configs in memory
        self.bot_core.runtime_manager.load_initial_caches(self.bot_core.symbols)
        self.bot_core.runtime_configs = self.bot_core.runtime_manager.caches
        
        return stats_output


if __name__ == "__main__":
    
    root_dir = Path(__file__).parent.parent.parent
    sys.path.insert(0, str(root_dir))
    

    class MockRuntimeManager:
        def __init__(self):
            self.caches = {}
            
        def load_initial_caches(self, symbols):
            pass

    class MockBotCore:
        def __init__(self):
            self.symbols = _CFG["symbols"]
            # Для получения публичных свечей ключи не нужны, но клиент требует их в конструкторе
            self.client = BinanceClient("", "")
            self.runtime_manager = MockRuntimeManager()
            self.runtime_configs = {}
    
    async def test_volatility():
        print("=== ЗАПУСК БЕЗОПАСНОГО ТЕСТА ВОЛАТИЛЬНОСТИ ===")
        bot = MockBotCore()
        vm = VolatilityManager(bot)
        vm.is_running = True
        
        print(f"Запуск расчетов для {len(bot.symbols)} монет...")
        stats = await vm.process_all()
        
        # Save beautifully to test/
        test_dir = root_dir / "test"
        test_dir.mkdir(exist_ok=True)
        dump_path = test_dir / "volatility_test.json"
        
        with open(dump_path, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=4, ensure_ascii=False)
            
        print(f"\n[OK] Результаты расчетов сохранены в файл: {dump_path}")
        print("=== ТЕСТ ЗАВЕРШЕН ===")
        await bot.client.shutdown()
        
    asyncio.run(test_volatility())
