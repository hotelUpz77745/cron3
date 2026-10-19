# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\TG\template_manager.py
# Role: template_manager.py module

# ==============================================================================
# Path: TG/template_manager.py
# Role: Менеджер генерации и слияния шаблонов настроек для Telegram
# ==============================================================================
import json
from pathlib import Path
from typing import Any, Optional, Dict, List, Union
from c_log import UnifiedLogger
from c_utils import Utils
from consts import DATA_DIR

from RUNTIME_FSM.runtime_builder import build_runtime_caches
logger = UnifiedLogger("TemplateManager")

class TemplateManager:
    def __init__(self, bot_core: Any = None):
        self.bot_core = bot_core
        self.runtime_dir = DATA_DIR / "runtime"
        self.base_file = DATA_DIR / "_base.json"
        
        # Поля, которые мы разрешаем редактировать через TG
        self.visible_keys = {"enable", "invest_size", "leverage"}

    def get_runtime_path(self, symbol: str, strategy_id: Optional[str] = None) -> Path:
        sym_lower = symbol.lower()
        if strategy_id:
            strat_dir = self.runtime_dir / strategy_id
            strat_dir.mkdir(parents=True, exist_ok=True)
            cand_json = strat_dir / f"{sym_lower}.json"
            if cand_json.exists():
                return cand_json
            cand_pkl = strat_dir / f"{sym_lower}.pkl"
            if cand_pkl.exists():
                return cand_pkl
            return cand_json
        if self.bot_core and hasattr(self.bot_core, "runtime_manager"):
            return self.bot_core.runtime_manager.get_runtime_path(symbol)
        if self.runtime_dir.exists():
            for sub in self.runtime_dir.iterdir():
                if sub.is_dir():
                    cand_json = sub / f"{sym_lower}.json"
                    if cand_json.exists():
                        return cand_json
                    cand_pkl = sub / f"{sym_lower}.pkl"
                    if cand_pkl.exists():
                        return cand_pkl
        cand_root_json = self.runtime_dir / f"{sym_lower}.json"
        if cand_root_json.exists():
            return cand_root_json
        cand_root_pkl = self.runtime_dir / f"{sym_lower}.pkl"
        if cand_root_pkl.exists():
            return cand_root_pkl
        return cand_root_json

    def generate_tg_template(self, symbol: str, strategy_id: Optional[str] = None) -> str:
        """Создает JSON шаблон для отправки в Telegram (чистые настройки)."""
        symbol = symbol.upper()
        target_file = self.get_runtime_path(symbol, strategy_id)
        if target_file.exists():
            if str(target_file).endswith(".pkl"):
                import pickle
                try:
                    with open(target_file, "rb") as fp:
                        rt_data = pickle.load(fp)
                except Exception:
                    rt_data = {}
            else:
                rt_data = Utils.read_json_file(target_file)
            if rt_data:
                resolved_strat = strategy_id
                if not resolved_strat and hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
                    resolved_strat = self.bot_core.strategy_registry.get_strategy_id_for_symbol(symbol)
                tg_template = {
                    "symbol": symbol,
                    "strategy_id": resolved_strat or "",
                    "_notes": "Full settings template. Edit the required parameters."
                }
                for side in ("LONG", "SHORT"):
                    if side in rt_data:
                        tg_template[side] = {
                            k: v for k, v in rt_data[side].items()
                            if k in ("enable", "invest_size", "leverage", "grid", "tp_map", "tp_purpose")
                        }
                return json.dumps(tg_template, indent=4)

        base_data = Utils.read_json_file(self.base_file)
        if not base_data:
            return json.dumps({"error": "Base template not found"})

        resolved_strat = strategy_id
        if not resolved_strat and hasattr(self.bot_core, "strategy_registry") and self.bot_core.strategy_registry:
            resolved_strat = self.bot_core.strategy_registry.get_strategy_id_for_symbol(symbol)

        tg_template = {
            "symbol": symbol,
            "strategy_id": resolved_strat or "",
            "_notes": "Full settings template. Edit the required parameters.",
        }
        for side in ("LONG", "SHORT"):
            if side in base_data:
                tg_template[side] = base_data[side]

        return json.dumps(tg_template, indent=4)

    def apply_tg_template(self, user_json_str: str, strategy_id: Optional[str] = None) -> tuple[bool, str]:
        """Парсит полученный JSON, обновляет настройки (сохраняя рантайм-стейт) и сохраняет в runtime."""
        try:
            user_data = json.loads(user_json_str)
        except json.JSONDecodeError as e:
            return False, f"Ошибка парсинга JSON: {e}"

        symbol = user_data.get("symbol", "").upper()
        if not symbol or not symbol.endswith("USDT"):
            return False, "Неверный или отсутствующий символ. Должен заканчиваться на USDT."

        strat_id = strategy_id or user_data.get("strategy_id")

        base_data = Utils.read_json_file(self.base_file)
        if not base_data:
            return False, "Базовый шаблон не найден."

        target_file = self.get_runtime_path(symbol, strat_id)
        if target_file.exists():
            final_data = Utils.read_json_file(target_file)
        else:
            # Если рантайма еще нет, просто копируем базовый шаблон. 
            # (Рантайм-поля добавятся при старте/ресете, но можно добавить и тут)
            final_data = json.loads(json.dumps(base_data))

        changes_applied = 0
        for side in ("LONG", "SHORT"):
            if side in user_data and side in final_data:
                # Обновляем только те ключи, которые есть в чистом _base.json (настройки)
                for key in base_data[side].keys():
                    if key in user_data[side]:
                        if isinstance(base_data[side][key], dict):
                            # Для вложенных словарей (grid, tp_map)
                            if key not in final_data[side]:
                                final_data[side][key] = {}
                            for subkey in user_data[side][key]:
                                if subkey in final_data[side][key] and isinstance(final_data[side][key][subkey], dict):
                                    if key == "grid":
                                        old_indent = final_data[side][key][subkey].get("indent")
                                        new_indent = user_data[side][key][subkey].get("indent")
                                        old_super_indent = final_data[side][key][subkey].get("super_indent")
                                        
                                        if new_indent is not None and old_indent is not None:
                                            try:
                                                if float(old_indent) != float(new_indent):
                                                    final_data[side][key][subkey]["price"] = None
                                                    final_data[side]["next_avg_price"] = None
                                            except ValueError:
                                                pass
                                    
                                    final_data[side][key][subkey].update(user_data[side][key][subkey])
                                    
                                    if key == "grid":
                                        new_super_indent = user_data[side][key][subkey].get("super_indent")
                                        if old_super_indent is not None and new_super_indent is None:
                                            final_data[side][key][subkey]["super_indent"] = old_super_indent
                                else:
                                    final_data[side][key][subkey] = user_data[side][key][subkey]
                        else:
                            final_data[side][key] = user_data[side][key]
                        changes_applied += 1

        if changes_applied == 0:
            return False, "Нет валидных изменений для применения."


        Utils.write_json_file(target_file, final_data)
        logger.info(f"Updated runtime config for {symbol} via TG at {target_file}.")
        
        return True, f"Настройки для {symbol} успешно применены!"
