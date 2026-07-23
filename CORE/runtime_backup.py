# ==============================================================================
# Path: CORE/runtime_backup.py
# Role: Автоматическое резервное копирование CFG/runtime в Telegram
# ==============================================================================

import asyncio
import time
import os
import io
import zipfile
import aiohttp
from c_log import UnifiedLogger

logger = UnifiedLogger("RuntimeBackup")

class RuntimeBackupManager:
    """
    Создает ZIP-архив из CFG/runtime и отправляет его в Telegram.
    Использует debounce, чтобы не спамить при частых изменениях.
    """
    def __init__(self, debounce_sec: int = 60, max_interval_sec: int = 300):
        self.debounce_sec = debounce_sec
        self.max_interval_sec = max_interval_sec
        
        self._needs_backup = False
        self._last_change_time = 0.0
        self._last_send_time = time.time()
        self._last_msg_id = None
        
        # Токен и ID чата берем напрямую, чтобы не было внешних зависимостей от бота
        self.token = os.getenv("TG_TOKEN") or ""
        self._chat_id = self._get_chat_id()
        
    def _get_chat_id(self):
        try:
            from consts import TG_ALLOWED_USERS
            if TG_ALLOWED_USERS:
                return TG_ALLOWED_USERS[0]
        except Exception:
            pass
        return os.getenv("TG_ADMIN_ID")
        
    def mark_changed(self):
        """Вызывается при сохранении нового рантайма."""
        self._needs_backup = True
        self._last_change_time = time.time()
        
    async def check_and_backup(self):
        """Вызывается в главном цикле. Если условия выполнены - запускает бекап."""
        if not self._needs_backup:
            return
            
        if not self.token or not self._chat_id:
            return
            
        now = time.time()
        time_since_change = now - self._last_change_time
        time_since_send = now - self._last_send_time
        
        # Условия отправки: прошло время debounce ИЛИ прошло слишком много времени (max_interval_sec)
        if time_since_change >= self.debounce_sec or time_since_send >= self.max_interval_sec:
            self._needs_backup = False
            self._last_send_time = now
            
            # Запускаем отправку как отдельную таску, чтобы не блокировать торговый цикл
            asyncio.create_task(self._create_and_send_backup())
            
    async def _create_and_send_backup(self):
        try:
            from consts import DATA_DIR
            runtime_dir = DATA_DIR / "runtime"
            
            if not runtime_dir.exists():
                return
                
            # 1. Создаем ZIP в оперативной памяти (BytesIO)
            memory_file = io.BytesIO()
            with zipfile.ZipFile(memory_file, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                # Пакуем только json файлы из CFG/runtime/
                for file_path in runtime_dir.glob("*.json"):
                    zf.write(file_path, arcname=file_path.name)
                    
            file_bytes = memory_file.getvalue()
            
            # 2. Отправляем в Telegram
            async with aiohttp.ClientSession() as session:
                # 2.1. Сначала удаляем предыдущее сообщение с бекапом, чтобы не засорять чат
                if self._last_msg_id:
                    delete_url = f"https://api.telegram.org/bot{self.token}/deleteMessage"
                    payload = {"chat_id": self._chat_id, "message_id": self._last_msg_id}
                    try:
                        async with session.post(delete_url, json=payload, timeout=aiohttp.ClientTimeout(total=5)):
                            pass
                    except Exception:
                        pass
                
                # 2.2. Отправляем новый архив
                send_url = f"https://api.telegram.org/bot{self.token}/sendDocument"
                form = aiohttp.FormData()
                form.add_field('chat_id', str(self._chat_id))
                
                import datetime
                import pytz
                import datetime
                utc_now = datetime.datetime.now(datetime.timezone.utc)
                date_str = utc_now.strftime("%Y-%m-%d_%H-%M-%S")
                filename = f"runtime_backup_{date_str}.zip"
                
                form.add_field('document', file_bytes, filename=filename, content_type='application/zip')
                
                # Добавим небольшой caption
                caption = "💾 <b>Runtime Backup</b>\n\nСвежий слепок <code>CFG/runtime/</code>"
                form.add_field('caption', caption)
                form.add_field('parse_mode', 'HTML')
                
                async with session.post(send_url, data=form, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    res = await resp.json()
                    if res.get("ok"):
                        self._last_msg_id = res["result"]["message_id"]
                        logger.info(f"Successfully sent runtime backup to Telegram ({filename}).")
                    else:
                        logger.error(f"Failed to send backup: {res}")
                        
        except Exception as e:
            logger.error(f"Error during runtime backup creation/sending: {e}")
