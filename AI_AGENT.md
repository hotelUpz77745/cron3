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

---


## 4. Architectural Invariants (Methodology)

- **Analytics Integrity**: The bot is isolated from manual Binance withdrawals/deposits. `cur_balance_usdt` is strictly mathematically calculated as `start_balance_usdt + net_profit_usdt`. NEVER sync balance directly from Binance `/fapi/v2/account` to `cur_balance_usdt`.
- **Binance API Limits**: When fetching klines, `limit` MUST NOT exceed 1500 to prevent HTTP 400.
- **Volatility Calculations**: `VolatilityManager` calculates the *average* volatility per candle. If computing weekly volatility, `timeframe` must be `1w`, not `3m`.
- **Analytics Ledger Aggregation**: Binance API `/fapi/v1/income` returns `REALIZED_PNL` as fragmented partial fills. The ledger reconstructor (`deep_sync_analytics`) MUST separate these partial fills using the `info` (tradeId) field to perfectly mirror the Binance history and prevent "lost trade" perception. Do NOT merge them by time-window.
- **Net Profit Priority**: All reporting and daily metric calculations (like `Avg Daily Profit`, `DRME`) MUST be based on `realized_pnl_net_usdt` (which accounts for commissions and funding), rather than gross `realized_pnl_usdt`.

---

## 5. CRITICAL RULE:
- "NEVER run main.py, tests, or any mutating commands without EXPLICIT permission from the user." -- status: DISABLED

## После каждой правки обновляй WORKSPACE/TRADING_SYSTEM/COMMON/wiki/{PROJECT_NAME}
