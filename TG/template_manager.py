# ==============================================================================
# Path: TG/template_manager.py
# Role: Менеджер генерации и слияния шаблонов настроек для Telegram
# ==============================================================================
import json
from pathlib import Path
from c_log import UnifiedLogger
from c_utils import Utils
from consts import DATA_DIR

logger = UnifiedLogger("TemplateManager")

class TemplateManager:
    def __init__(self):
        self.runtime_dir = DATA_DIR / "runtime"
        self.base_file = DATA_DIR / "_base.json"
        
        # Поля, которые мы разрешаем редактировать через TG
        self.visible_keys = {"enable", "invest_size", "leverage"}

    def generate_tg_template(self, symbol: str) -> str:
        """Создает JSON шаблон для отправки в Telegram (чистые настройки)."""
        symbol = symbol.upper()
        base_data = Utils.read_json_file(self.base_file)
        if not base_data:
            return json.dumps({"error": "Base template not found"})

        tg_template = {
            "symbol": symbol,
            "_notes": "Full settings template. Edit the required parameters.",
        }
        for side in ("LONG", "SHORT"):
            if side in base_data:
                tg_template[side] = base_data[side]

        return json.dumps(tg_template, indent=4)

    def apply_tg_template(self, user_json_str: str) -> tuple[bool, str]:
        """Парсит полученный JSON, обновляет настройки (сохраняя рантайм-стейт) и сохраняет в runtime."""
        try:
            user_data = json.loads(user_json_str)
        except json.JSONDecodeError as e:
            return False, f"Ошибка парсинга JSON: {e}"

        symbol = user_data.get("symbol", "").upper()
        if not symbol or not symbol.endswith("USDT"):
            return False, "Неверный или отсутствующий символ. Должен заканчиваться на USDT."

        base_data = Utils.read_json_file(self.base_file)
        if not base_data:
            return False, "Базовый шаблон не найден."

        target_file = self.runtime_dir / f"{symbol.lower()}.json"
        if target_file.exists():
            final_data = Utils.read_json_file(target_file)
        else:
            # Если рантайма еще нет, просто копируем базовый шаблон. 
            # (Рантайм-поля добавятся при старте/ресете, но можно добавить и тут)
            from RUNTIME_FSM.runtime_builder import build_runtime_caches
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

        # Валидация на совпадение количества усреднений и тейков
        for side in ("LONG", "SHORT"):
            if side in final_data:
                grid = final_data[side].get("grid", {})
                tp_map = final_data[side].get("tp_map", {})
                if grid and tp_map and len(grid) != len(tp_map):
                    return False, f"ОШИБКА ({side}): Количество уровней grid ({len(grid)}) не совпадает с tp_map ({len(tp_map)})."

        Utils.write_json_file(target_file, final_data)
        logger.info(f"Updated runtime config for {symbol} via TG.")
        
        return True, f"Настройки для {symbol} успешно применены!"
