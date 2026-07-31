import asyncio
import json
import logging
try:
    import redis.asyncio as redis
except ImportError:
    print("Ошибка: пакет 'redis' не установлен. Установите его: pip install redis")
    exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

async def read_from_redis():
    # URL должен совпадать с тем, что в .env (по умолчанию redis://localhost:6379/0)
    redis_url = "redis://localhost:6379/0"
    
    try:
        client = redis.from_url(redis_url, decode_responses=True)
        # Проверка соединения
        await client.ping()
        logging.info(f"Успешное подключение к Redis: {redis_url}")
    except Exception as e:
        logging.error(f"Не удалось подключиться к Redis. Убедитесь, что сервер запущен. Ошибка: {e}")
        return

    # Чтение списка доступных монет (ключей в Hash)
    symbols = await client.hkeys("bot:runtime:states")
    logging.info(f"Найдены сохраненные монеты: {symbols}")
    
    if not symbols:
        logging.warning("В Redis пока нет данных по монетам (bot:runtime:states пуст).")
    
    # Чтение состояния конкретной монеты (возьмем первую попавшуюся)
    if symbols:
        test_symbol = symbols[0]
        state_str = await client.hget("bot:runtime:states", test_symbol)
        if state_str:
            state_data = json.loads(state_str)
            long_state = state_data.get("LONG", {})
            logging.info(f"--- Детали по {test_symbol} (LONG) ---")
            logging.info(f"В позиции (in_position): {long_state.get('in_position')}")
            logging.info(f"Средняя цена (avg_entry_price): {long_state.get('avg_entry_price')}")
            logging.info(f"Объем (total_volume): {long_state.get('total_volume')}")
            logging.info("-" * 30)

    # Чтение аналитики
    analytics_str = await client.hget("bot:runtime:analytics", "global")
    if analytics_str:
        analytics_data = json.loads(analytics_str)
        logging.info("--- Глобальная аналитика ---")
        logging.info(f"Net Profit: {analytics_data.get('net_profit_usdt', 0)} USDT")
        logging.info(f"Winrate: {analytics_data.get('winrate_pct', 0)} %")
        logging.info("-" * 30)
    else:
        logging.warning("В Redis пока нет глобальной аналитики.")

    await client.close()

if __name__ == "__main__":
    asyncio.run(read_from_redis())
