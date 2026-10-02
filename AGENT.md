# AGENT.md — AI Quick Start
# PROJECT: cron3 (hron) | branch: uni | 2026-10

---

## ПРОТОКОЛ (читай первым)

- Директ оператора — **всегда в приоритете**
- После каждой правки → `git commit` (push — ЗАПРЕЩЁН без команды оператора)
- Серьёзные изменения → план имплементации перед кодом
- Лимиты файлов: **150–450 строк** (абс. макс 600). Превышение = декомпозиция

---

## КАРТА ФАЙЛОВ (не делай list_dir — всё здесь)

```
cron3/
├── main.py                          # точка входа; asyncio.gather(BotCore, TGReceiver)
├── consts.py                        # все константы; читает CFG/app.json + .env
├── c_log.py                         # UnifiedLogger
├── c_utils.py                       # Utils (общие утилиты)
├── quant_calculator.py              # расчёт объёмов ордеров
├── watchdog.py                      # watchdog + heartbeat
├── tech_debt.md                     # ← ADR-лог + протоколы разработки (читать при архитектурных задачах)
│
├── CORE/
│   ├── bot.py                       # BotCore: _on_tick, _process_signal — главный оркестратор
│   ├── auto_closer.py               # авто-закрытие по PnL куполов
│   ├── notifier.py                  # уведомления по куполам
│   ├── coin_selectors.py            # Селекторы монет (AgentMarketSelector + SamsonovFunnelSelector)
│   ├── runtime_backup.py            # дебаунс-бэкап рантаймов
│   ├── _utils.py                    # утилиты ядра
│   ├── STRATEGY/
│   │   ├── strategy_registry.py     # StrategyRegistry: валидация, маппинг symbol→strat
│   │   ├── strategy_applicator.py   # StrategyApplicator: merge base+overrides → CFG/runtime/
│   │   ├── momentum_engine.py       # Smart Grace / Smart Size / Knife Blocker / Smart TP
│   │   └── __init__.py
│   ├── ENTRY/
│   │   ├── signal.py                # сигнал входа (таймфрейм, Smart Grace)
│   │   └── leverage_manager.py      # управление плечом
│   ├── GRID/
│   │   └── avg_manager.py           # DCA-усреднение, Super Grid
│   ├── TP/
│   │   ├── tp_manager.py            # основной TP (tp_map, Smart TP)
│   │   └── fallback_tp_manager.py   # fallback TP
│   └── ADVANCED/
│       ├── volatility_manager.py    # адаптивная сетка по волатильности
│       └── volatility_scanner.py    # скрининг рынка
│
├── POS_FSM/
│   ├── models.py                    # модели позиций
│   ├── pos_stream.py                # WebSocket стрим позиций
│   └── pos_stream_monitor.py        # мониторинг стрима
│
├── RUNTIME_FSM/
│   ├── runtime_builder.py           # генерация CFG/runtime/<strat>/<sym>.json
│   └── runtime_manager.py           # менеджер рантаймов в памяти
│
├── TG/
│   ├── tg_receiver.py               # TG-интерфейс (~158KB — временное исключение лимита)
│   └── template_manager.py          # шаблоны сообщений
│
├── ANALYTICS/
│   ├── analytics.py                 # расчёт метрик по куполам
│   ├── metrics.py                   # peak, drawdown, recovery factor
│   ├── leaderboard.py / .txt        # лидерборд стратегий
│   ├── plotter.py                   # графики
│   ├── STATS/                       # analytics_<strat_id>.json — по куполу
│   ├── LEDGERS/                     # журналы сделок
│   └── BACKUP_SESSIONS/             # бэкапы рантаймов
│
├── CFG/
│   ├── app.json                     # главный конфиг (active_strategies, telegram, watchdog...)
│   ├── universes.json               # библиотека шаблонов стратегий
│   └── runtime/<strat_id>/<sym>.json  # живые рантаймы (генерирует StrategyApplicator)
│
├── CACHE/
│   ├── specifications.json          # спецификации инструментов Binance (~2.5MB)
│   ├── leverage_cache.json          # текущие плечи по символам
│   └── iteration_state.json         # состояние итерации авто-закрытия
│
├── tools/
│   ├── audit_and_cleanup_orders.py  # аудит ордеров
│   └── grid_volume_calculator.py    # расчёт объёмов сетки
│
└── tests/
    ├── test_strategy_system.py      # 12 тестов multi-universe (100% pass)
    └── test_auto_closer_stagnation.py
```

---

## ИНВАРИАНТЫ (нарушение = стоп)

| # | Правило |
|---|---|
| **I1** | Каждая монета — строго в одной активной стратегии. Коллизия → `ValueError` Fail-Fast |
| **I2** | Конфиги читать `['key']`, не `.get(key, default)` |
| **I3** | Дефолт = `None`. Нуль (`0`) — только с согласования оператора |
| **I4** | Любой TP строго `≥ 0.50%`. `tp_indent = max(0.50, val)` — вшито в `StrategyApplicator` |
| **I5** | Суффиксы: `_pct` = % от 100 | `_ratio`/`_rate` = доля от 1. Никакой нормализации в коде |
| **I6** | Шапка каждого файла: `# Path: ...` + `# Role: ...` |
| **I7** | `git push` — ЗАПРЕЩЁН без явной команды оператора |

---

## ТЕКУЩИЙ КОНТЕКСТ (обновляй при изменениях)

```
Ветка:      uni
Активные стратегии:
  u9_omni_zenith_apex   → 3 монеты (enabled: true)
  Остальные             → enabled: false (спящий резерв)

Лидерборд (leaderboard.txt):
  u9_omni_zenith_apex: +8.53 USDT net | Rec.Factor 1.07 | 62 сделки

Ключевые ADR (последние):
  - Купольная изоляция: Analytics / AutoCloser / Notifications / SuperGrid — по стратегии
  - iteration_state.json: персистентность iter PnL и guards авто-закрытия
  - universes.json: per-side overrides, убраны is_active флаги
```

---

## ПЕРВОИСТОЧНИКИ (при глубоких задачах)

| Файл | Когда читать |
|---|---|
| `tech_debt.md` | Архитектурные задачи, новые фичи, ADR, cтандарты кодинга |
| `CFG/app.json` | Параметры конфигурации |
| `CFG/universes.json` | Шаблоны стратегий |
| `../../../../WORKSPACE/COMMON/DOCS/manifest.md` | Стандарты кодинга |