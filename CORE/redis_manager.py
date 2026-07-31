# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\CORE\redis_manager.py
# Role: redis_manager.py module

# ==============================================================================
# Path: CORE/redis_manager.py
# Role: Менеджер синхронизации состояния в Redis
# ==============================================================================

import asyncio
import time
import json
try:
    import redis.asyncio as redis
except ImportError:
    redis = None
    
from c_log import UnifiedLogger
from consts import REDIS_ENABLED, REDIS_URL, DATA_DIR, ANALYTICS_DIR

logger = UnifiedLogger("RedisManager")

class RedisManager:
    """
    Независимый модуль для дублирования бекапов в Redis.
    Имеет свой debounce_sec (по умолчанию 1 секунда).
    """
    def __init__(self, debounce_sec: float = 1.0):
        self.debounce_sec = debounce_sec
        self._needs_backup = False
        self._last_change_time = 0.0
        self._last_send_time = time.time()
        
        self.enabled = REDIS_ENABLED
        self.url = REDIS_URL
        self.redis_client = None
        
        if self.enabled and redis:
            try:
                self.redis_client = redis.from_url(self.url, decode_responses=True)
                logger.info(f"RedisManager initialized with URL: {self.url}")
            except Exception as e:
                logger.error(f"Failed to initialize Redis client: {e}")
                self.enabled = False
        elif self.enabled and not redis:
            logger.error("Redis is enabled but 'redis' package is not installed.")
            self.enabled = False

    def mark_changed(self):
        """Вызывается при изменении состояния (синхронно с файловым бекапом)."""
        if not self.enabled:
            return
        self._needs_backup = True
        self._last_change_time = time.time()
        
    async def check_and_backup(self):
        """Проверяет, пришло ли время делать пуш в Redis."""
        if not self.enabled or not self._needs_backup or not self.redis_client:
            return
            
        now = time.time()
        time_since_change = now - self._last_change_time
        
        if time_since_change >= self.debounce_sec:
            self._needs_backup = False
            self._last_send_time = now
            # Запускаем таску асинхронно
            asyncio.create_task(self._push_to_redis())

    async def _push_to_redis(self):
        try:
            runtime_dir = DATA_DIR / "runtime"
            if not runtime_dir.exists():
                return
                
            states_map = {}
            for file_path in runtime_dir.glob("*.json"):
                try:
                    content = file_path.read_text(encoding="utf-8")
                    symbol = file_path.stem
                    states_map[symbol] = content
                except Exception:
                    pass
            
            analytics_map = {}
            analytics_json = ANALYTICS_DIR / "analytics.json"
            if analytics_json.exists():
                try:
                    analytics_map["global"] = analytics_json.read_text(encoding="utf-8")
                except Exception:
                    pass
            
            trades_ledger = ANALYTICS_DIR / "trades_ledger.txt"
            if trades_ledger.exists():
                try:
                    analytics_map["ledger"] = trades_ledger.read_text(encoding="utf-8")
                except Exception:
                    pass
            
            async with self.redis_client.pipeline(transaction=True) as pipe:
                if states_map:
                    pipe.hset("bot:runtime:states", mapping=states_map)
                if analytics_map:
                    pipe.hset("bot:runtime:analytics", mapping=analytics_map)
                
                await pipe.execute()
                
        except Exception as e:
            logger.error(f"Failed to push backup to Redis: {e}")

    async def pull_failover_data(self) -> bool:
        """
        Вызывается при переключении из РЕЗЕРВ в АКТИВНЫЙ (failover).
        Читает свежие стейты и аналитику из Redis и принудительно перезаписывает
        файлы на жестком диске.
        """
        if not self.enabled or not self.redis_client:
            logger.warning("RedisManager is not connected. Cannot pull failover data!")
            return False
            
        try:
            logger.info("Pulling failover data from Redis...")
            # 1. Pull states
            states = await self.redis_client.hgetall("bot:runtime:states")
            if states:
                runtime_dir = DATA_DIR / "runtime"
                runtime_dir.mkdir(parents=True, exist_ok=True)
                for sym, content in states.items():
                    (runtime_dir / f"{sym}.json").write_text(content, encoding="utf-8")
                logger.info(f"Restored {len(states)} symbol states from Redis.")
                
            # 2. Pull analytics
            analytics_data = await self.redis_client.hgetall("bot:runtime:analytics")
            if analytics_data:
                if "global" in analytics_data:
                    (ANALYTICS_DIR / "analytics.json").write_text(analytics_data["global"], encoding="utf-8")
                    logger.info("Restored analytics.json from Redis.")
                if "ledger" in analytics_data:
                    (ANALYTICS_DIR / "trades_ledger.txt").write_text(analytics_data["ledger"], encoding="utf-8")
                    logger.info("Restored trades_ledger.txt from Redis.")
                    
            logger.info("Failover data pull completed successfully.")
            return True
        except Exception as e:
            logger.error(f"Failed to pull failover data from Redis: {e}")
            return False
