# Инструкция по работе с Redis в боте Antigravity

В боте реализовано дублирование внутреннего состояния (`PositionState` и аналитики) в базу данных Redis. Это позволяет сторонним сервисам (веб-дашбордам, другим ботам, системам мониторинга) в реальном времени получать актуальные данные без обращения к файловой системе бота.

## Установка и запуск Redis

### Локально (на Windows для тестирования)

Самый простой способ поднять Redis локально — использовать Docker.

1. Установите **Docker Desktop**.
2. В терминале выполните команду:
   ```bash
   docker run --name my-redis -p 6379:6379 -d redis
   ```
3. Redis будет доступен по адресу `redis://localhost:6379/0`.

Если вы не хотите использовать Docker, вы можете установить порт Redis для Windows (например, из репозитория Memurai или устаревшие сборки от Microsoft), но Docker предпочтительнее.

### На сервере (Ubuntu / Debian)

На Linux Redis устанавливается нативно как системная служба:

1. Обновите пакеты и установите Redis:
   ```bash
   sudo apt update
   sudo apt install redis-server -y
   ```
2. Убедитесь, что служба запущена и работает:
   ```bash
   sudo systemctl status redis-server
   sudo systemctl enable redis-server
   ```
3. По умолчанию Redis слушает `localhost:6379`. Если ваш бот работает на этом же сервере, дополнительных настроек сети не требуется.

---

## Настройка бота (Включение Redis)

Чтобы бот начал отправлять данные в Redis, добавьте следующие строки в ваш файл `.env` в корне проекта:

```env
REDIS_ENABLED=true
REDIS_URL=redis://localhost:6379/0
```

> **Важно:** Бот использует библиотеку `redis.asyncio` (пакет `redis` в Python), который сам управляет асинхронными пулами соединений. Бот использует независимый механизм "debounce" (по умолчанию 1 секунда), что означает, что он будет группировать и отправлять пачки изменений в Redis не чаще, чем раз в секунду. Это полностью разгружает диск бота и не замедляет торговые циклы.

---

## Как устроены данные в Redis

Бот сохраняет данные, используя структуру `Hash` (Хэш-таблицы). Это идеальный формат для хранения JSON-документов по ключам.

1. **Ключ `bot:runtime:states`**
   - **Поля (Fields):** Имена символов, например `WIFUSDT`, `BTCUSDT`.
   - **Значения (Values):** Строка в формате JSON (полный дамп файла `runtime/<symbol>.json`).

2. **Ключ `bot:runtime:analytics`**
   - **Поля (Fields):** `global`
   - **Значение (Value):** Строка в формате JSON (полный дамп файла `analytics.json`).

---

## Как читать данные

### Через `redis-cli` (Командная строка)

Подключитесь к Redis:
```bash
redis-cli
```

Посмотреть список всех монет (полей), по которым есть стейты:
```bash
HKEYS bot:runtime:states
```

Получить JSON-стейт конкретной монеты (например, WIFUSDT):
```bash
HGET bot:runtime:states WIFUSDT
```

Получить данные глобальной аналитики:
```bash
HGET bot:runtime:analytics global
```

### Через Python скрипт (Рекомендуется для дашбордов)

В корневой папке проекта есть файл `test_redis_read.py`. Вы можете запустить его, чтобы наглядно убедиться, что данные успешно читаются.

Пример кода для внешнего сервиса (FastAPI / любого скрипта):

```python
import asyncio
import json
import redis.asyncio as redis

async def read_from_redis():
    # Подключаемся к базе
    client = redis.from_url("redis://localhost:6379/0", decode_responses=True)
    
    # Читаем стейт монеты WIFUSDT
    wif_state_str = await client.hget("bot:runtime:states", "WIFUSDT")
    if wif_state_str:
        state_data = json.loads(wif_state_str)
        long_state = state_data.get("LONG", {})
        print(f"WIFUSDT LONG In Position: {long_state.get('in_position')}")
        print(f"Avg Entry Price: {long_state.get('avg_entry_price')}")
        print(f"Total Volume: {long_state.get('total_volume')}")

    # Читаем аналитику
    analytics_str = await client.hget("bot:runtime:analytics", "global")
    if analytics_str:
        analytics_data = json.loads(analytics_str)
        print(f"\nGlobal Net Profit: {analytics_data.get('net_profit_usdt')} USDT")

    await client.close()

if __name__ == "__main__":
    asyncio.run(read_from_redis())
```
