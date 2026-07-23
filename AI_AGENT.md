# AI_AGENT.md — AI Quick Start

PROJECT_NAME = "hron"

Читается первым. Содержит только навигацию и текущий контекст — всё остальное в первоисточниках ниже.

---

## 1. Первоисточники (читать в этом порядке)

| Документ | Что там |
|---|---|
| `../../../WORKSPACE/TRADING_SYSTEM/COMMON/DOCS/manifest.md` | Стандарты кодинга: SRP, лимиты строк (500 max, 150–350 gold), правила деплоя. |

| `../../../WORKSPACE/TRADING_SYSTEM/COMMON/DOCS/{PROJECT_NAME}/tech_debt.md` | Документ описывает основные технические долги и проблемы архитектуры, которые требуют немедленного устранения. |

| `../../../WORKSPACE/TRADING_SYSTEM/COMMON/DOCS/{PROJECT_NAME}/TZ.md` | **Все** бизнес-инварианты системы. Например: алгоритм балансировки, FSM инцидентов, газ-буфер, уведомления, ... |

| `../../../WORKSPACE/TRADING_SYSTEM/COMMON/wiki/{PROJECT_NAME}/` | Obsidian-заметки: архитектура, плейбук... |

---

## 2. Карта файлов (где что)

- ../../../WORKSPACE/TRADING_SYSTEM/COMMON/DOCS/
  - manifest.md
  - {PROJECT_NAME}/
- ../../../WORKSPACE/TRADING_SYSTEM/COMMON/wiki/
- {PROJECT_NAME}/

---

## 3. Режимы запуска

- `USE_TEST_KEYS=1` → `.env.test` + `CONFIG/test/` (те же реальные биржи, ключи разработчика)


Директ -- всегда в приоритете. То что оператор промсит напрямую в окне редактирования с чатом -- в перую очередь.
---

(тут обновы писать пометки и прогресс не надо, пиши в соответствующий файл в "COMMON\\wiki\\{PROJECT_NAME}")

## 4. Architectural Invariants (Methodology)

- **Analytics Integrity**: The bot is isolated from manual Binance withdrawals/deposits. `cur_balance_usdt` is strictly mathematically calculated as `start_balance_usdt + net_profit_usdt`. NEVER sync balance directly from Binance `/fapi/v2/account` to `cur_balance_usdt`.
- **Binance API Limits**: When fetching klines, `limit` MUST NOT exceed 1500 to prevent HTTP 400.
- **Volatility Calculations**: `VolatilityManager` calculates the *average* volatility per candle. If computing weekly volatility, `timeframe` must be `1w`, not `3m`.
- **Analytics Ledger Aggregation**: Binance API `/fapi/v1/income` returns `REALIZED_PNL` as fragmented partial fills. The ledger reconstructor (`deep_sync_analytics`) MUST separate these partial fills using the `info` (tradeId) field to perfectly mirror the Binance history and prevent "lost trade" perception. Do NOT merge them by time-window.
- **Net Profit Priority**: All reporting and daily metric calculations (like `Avg Daily Profit`, `DRME`) MUST be based on `realized_pnl_net_usdt` (which accounts for commissions and funding), rather than gross `realized_pnl_usdt`.
- **Loop Watchdog Isolation**: `watchdog.py` monitors `BotCore` main loop health and handles Telegram alerts/heartbeats. Telegram delivery is handled by `WatchdogTGAdapter` inside `watchdog.py` using `TG_TOKEN` strictly from `.env` without fallbacks to config files.

---

## 5. CRITICAL RULE:
- "NEVER run main.py, tests, or any mutating commands without EXPLICIT permission from the user." -- status: ENABLED

## После каждой правки обновляй WORKSPACE/TRADING_SYSTEM/COMMON/wiki/{PROJECT_NAME}

## 6. Latest Commits
- `09fa888 - feat(core): integrate LoopWatchdog into BotCore with WatchdogTGAdapter and TG_TOKEN from .env`
- `79da157 - fix(analytics): group partial fills within 5 seconds for accurate real trade counting`
- `c2c7833 - fix(CORE/bot.py): enforce idempotency flag immediately after successful market order`
- `5a8a975 - fix: TP zero quantity prevention, resilient spec loader, API rate limits reduction`
- `8006c69 - fix(core): wait for exchange specifications before starting game loop to prevent KeyError`
- `c2385ce - fix(tg): add missing StateFilter import for close all confirmation`
- `970e04d - feat(tg): add confirmation step for Close All command requiring the word ЗАКРЫТЬ`
- `9b0cc47 - fix(analytics): make ledger and json writes atomic to prevent file truncation during abrupt restart`
- `79d66a9 - refactor(analytics): completely eradicate while True, replace with explicit is_fetching flag`
- `3feb52b - fix(analytics): remove MAX_PAGES hard limit in deep sync to fetch full history`
- `b168c84 - feat(tg): add fool-proofing to block non-text media messages in outer middleware`
- `c31441e - refactor(analytics): remove infinite loops, add 3-retries safety to deep sync, change tracker polling to 5s`
- `7328651 - fix(log): resolve KeyError context by using UnifiedLogger instances directly`
- `cf1dbb4 - feat(tg): add Close All button to emergency close all active positions and orders`
- `ee24eb8 - fix(analytics): auto strip symbols during deep sync to prevent duplicates`
- `16b7f54 - fix(analytics): restore legacy ledger symbols tracking for deep sync`
