# ==============================================================================
# Path: CORE/SERVICES/runtime_backup_service.py
# Role: Сервис On-Demand бэкапа рантайма позиций (.pkl -> JSON + Telegram)
# ==============================================================================

import asyncio
import aiohttp
import math
import os
import sys
import json
import pickle
import datetime
from pathlib import Path
from decimal import Decimal
from typing import Dict, Any, Optional, List

# Корректировка sys.path при прямом запуске модуля
BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from c_log import UnifiedLogger
from c_utils import Utils
from consts import DATA_DIR, CACHE_DIR, RUNTIME_DIR, TG_ALLOWED_USERS

logger = UnifiedLogger("RuntimeBackupService")


def sanitize_for_json(obj: Any) -> Any:
    """Рекурсивная санитизация объектов рантайма для безопасной сериализации в JSON."""
    if isinstance(obj, dict):
        return {str(k): sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple, set)):
        return [sanitize_for_json(v) for v in obj]
    elif isinstance(obj, Decimal):
        return float(obj)
    elif isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    elif isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    elif isinstance(obj, Path):
        return str(obj)
    elif isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return obj


class RuntimeBackupService:
    """
    On-Demand сервис бэкапа рантайма позиций.
    Считывает актуальные бинарные .pkl файлы из CFG/runtime/, 
    санитизирует данные, формирует структурированный JSON 
    и отправляет сформированный документ в Telegram.
    """

    @classmethod
    def export_runtime(cls, runtime_dir: Optional[Path] = None, output_dir: Optional[Path] = None) -> Path:
        """
        Сканирует runtime_dir, десериализует .pkl (или .json) и сохраняет чистый JSON в output_dir.
        """
        runtime_path = runtime_dir or RUNTIME_DIR
        backups_path = output_dir or (CACHE_DIR / "backups")
        backups_path.mkdir(parents=True, exist_ok=True)

        strategies_data: Dict[str, Dict[str, Any]] = {}
        total_symbols = 0

        if runtime_path.exists():
            # 1. Поиск по подпапкам стратегий
            for item in runtime_path.iterdir():
                if item.is_dir():
                    strat_name = item.name
                    strategies_data[strat_name] = {}
                    for f in item.iterdir():
                        if f.suffix in (".pkl", ".json"):
                            sym = f.stem.lower()
                            if sym in strategies_data[strat_name] and f.suffix == ".pkl":
                                # Приоритет за .json, pkl игнорируем если json уже загружен
                                continue
                            try:
                                if f.suffix == ".pkl":
                                    with open(f, "rb") as fp:
                                        data = pickle.load(fp)
                                else:
                                    data = Utils.read_json_file(f)
                                strategies_data[strat_name][sym] = sanitize_for_json(data)
                                total_symbols += 1
                            except Exception as e:
                                logger.error(f"Failed to read runtime file {f}: {e}")
                elif item.is_file() and item.suffix in (".pkl", ".json"):
                    # Корневые рантаймы (без подпапки стратегии)
                    sym = item.stem.lower()
                    if "_root" not in strategies_data:
                        strategies_data["_root"] = {}
                    try:
                        if item.suffix == ".pkl":
                            with open(item, "rb") as fp:
                                data = pickle.load(fp)
                        else:
                            data = Utils.read_json_file(item)
                        strategies_data["_root"][sym] = sanitize_for_json(data)
                        total_symbols += 1
                    except Exception as e:
                        logger.error(f"Failed to read root runtime file {item}: {e}")

        now = datetime.datetime.now()
        timestamp_str = now.strftime("%Y%m%d_%H%M%S")
        export_payload = {
            "metadata": {
                "created_at": now.isoformat(),
                "created_at_epoch": now.timestamp(),
                "total_strategies": len(strategies_data),
                "total_symbols": total_symbols,
                "runtime_source": str(runtime_path),
                "service": "RuntimeBackupService"
            },
            "strategies": strategies_data
        }

        target_file = backups_path / f"runtime_backup_{timestamp_str}.json"
        latest_file = backups_path / "runtime_backup_latest.json"

        Utils.write_json_file(target_file, export_payload)
        Utils.write_json_file(latest_file, export_payload)

        file_size_kb = target_file.stat().st_size / 1024.0
        logger.info(f"Runtime backup created: {target_file.name} ({file_size_kb:.1f} KB, strats={len(strategies_data)}, symbols={total_symbols})")
        return target_file

    @classmethod
    async def send_to_telegram(cls, json_path: Path, chat_ids: Optional[List[int]] = None) -> bool:
        """Отправляет сгенерированный JSON-файл в Telegram как документ."""
        token = os.getenv("TG_TOKEN")
        if not token:
            logger.warning("TG_TOKEN not configured in environment. Skipping Telegram send.")
            return False

        targets = []
        if chat_ids:
            targets.extend(chat_ids)
        elif TG_ALLOWED_USERS:
            targets.extend(TG_ALLOWED_USERS)
        
        if not targets:
            admin_env = os.getenv("TG_ADMIN_ID")
            if admin_env:
                targets.append(admin_env)

        if not targets:
            logger.warning("No Telegram target chat IDs found for backup sending.")
            return False

        file_size_kb = json_path.stat().st_size / 1024.0
        caption = (
            f"📦 <b>Рантайм-Бэкап Позиций (On-Demand)</b>\n"
            f"📄 Файл: <code>{json_path.name}</code>\n"
            f"💾 Размер: <code>{file_size_kb:.1f} KB</code>\n"
            f"🕒 Время: <code>{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>"
        )

        url = f"https://api.telegram.org/bot{token}/sendDocument"
        success = False

        async with aiohttp.ClientSession() as session:
            for uid in targets:
                try:
                    with open(json_path, "rb") as fp:
                        form = aiohttp.FormData()
                        form.add_field("chat_id", str(uid))
                        form.add_field("caption", caption)
                        form.add_field("parse_mode", "HTML")
                        form.add_field("document", fp, filename=json_path.name, content_type="application/json")
                        
                        async with session.post(url, data=form, timeout=30) as resp:
                            if resp.status == 200:
                                logger.info(f"Successfully sent backup document to Telegram chat {uid}")
                                success = True
                            else:
                                err_text = await resp.text()
                                logger.error(f"Failed to send backup to {uid}: {resp.status} - {err_text}")
                except Exception as e:
                    logger.error(f"Error sending backup document to {uid}: {e}")

        return success

    @classmethod
    async def export_and_send(cls, bot_instance: Any = None, chat_ids: Optional[List[int]] = None) -> Dict[str, Any]:
        """
        Комплексная процедура:
        1. Если передан bot_instance, принудительно синхронизирует FSM в кэш.
        2. Экспортирует рантайм в JSON.
        3. Отправляет файл в Telegram.
        """
        if bot_instance and hasattr(bot_instance, "runtime_manager") and hasattr(bot_instance, "fsm_states"):
            try:
                await bot_instance.runtime_manager.sync_with_fsm(bot_instance.fsm_states, force_save=True)
            except Exception as e:
                logger.error(f"Pre-backup FSM sync failed: {e}")

        backup_file = cls.export_runtime()
        tg_sent = await cls.send_to_telegram(backup_file, chat_ids=chat_ids)

        return {
            "status": "success",
            "file": str(backup_file),
            "filename": backup_file.name,
            "size_bytes": backup_file.stat().st_size,
            "telegram_sent": tg_sent
        }


if __name__ == "__main__":
    print("=== Запуск On-Demand Сервиса Бэкапа Рантайма ===")
    result = asyncio.run(RuntimeBackupService.export_and_send())
    print(f"Результат: {json.dumps(result, indent=2, ensure_ascii=False)}")
