# AGENT.md — Институциональная карта и руководство по проекту cron3 (hron)
# Ветка: new_gen | Архитектура: Multi-Universe Modular FSM | 2026-10

---

## 1. ПРОТОКОЛ РАЗРАБОТКИ (Самсонов Протокол)

1. **Директ оператора — абсолютный приоритет**: Любое указание оператора исполняется неукоснительно.
2. **Правило 9: Строжайший запрет `git push`**: Любой `git push` категорически ЗАПРЕЩЕН без явной текстовой команды оператора. Разрешены только локальные коммиты (`git commit`).
3. **Лимиты файлов**: 150–450 строк (абсолютный максимум — 600 строк). При превышении лимита файл обязан быть декомпозирован на модульные сервисы.
4. **Тотальный запрет инлайн-импортов**: Все `import` обязаны находиться строго в заголовке модуля (верхний уровень). Внутри методов инлайн-импорты запрещены (0 инлайн-импортов в проде).
5. **Тестовая готовность**: После любых правок запускать тестовый сгон: `python -m unittest discover tests` (норма: 483 теста, 100% pass, 0 failures, 0 errors).
6. **Шапка каждого файла**: `# Path: ...` + `# Role: ...`.

---

## 2. АРХИТЕКТУРНЫЕ ИНВАРИАНТЫ (Нарушение = Стоп системы)

| # | Инвариант | Описание |
|---|---|---|
| **I1** | **Symbol Confinement** | Каждая монета торгуется строго в одной активной стратегии. Коллизия символов вызывает немедленный `ValueError` Fail-Fast при старте. |
| **I2** | **Strict Dict Access** | Боевые конфигурации читаются через прямой доступ `['key']`, а не скрывающий баги `.get(key, default)`. Дефолт в кодовой базе — строго `None`. Нуль (`0`) допустим только по согласованию. |
| **I3** | **Take-Profit Floor** | Ни один тейк-профит в системе не может опускаться ниже `0.50%` (Правило 11: `tp_indent = max(0.50, val)`), если в паспорте Вселенной не задан индивидуальный гарантированный пол (например, `min_tp_indent: 1.0%`). |
| **I4** | **In-Flight Idempotency** | Институциональная защита от дублирования ордеров и гонок пакетов в `PositionState`: <br>• Вход: `in_position_papper`<br>• Усреднение: `pending_avg`<br>• Тейк: `pending_rolling_tp`<br>• Закрытие: `is_closing`, `is_finished` |
| **I5** | **Heartbeat Window Reset** | Окно оценки счетчиков закрытий жестко привязано к физической свече тика сигнала (`signal_heartbeat_tfr: "5m"`). Наступление новой пятиминутки с рандомизированным сдвигом `shift_interval_sec` детерминированно сбрасывает счетчики закрытий (`recent_closes`) синхронно для `smart_grace`, `smart_size` и `smart_tp`. Плавающие скользящие окна (`eval_window_sec`) категорически запрещены. |
| **I6** | **Knife Blocker SSoT** | `knife_blocker_threshold` является единственным источником истины: целое число `int` (например, `1`) означает активность блокиратора, `null` (в Python `None`) — отключение. Отдельный флаг `knife_blocker_enabled` вырезан из паспортов; в рантайме доступен как динамическое свойство (`threshold is not None`). |
| **I7** | **Strong Hand Fist Rule** | «Правило кулака Сильной руки» (`first_order_weight_regime`: `bullish` 12/7, `neutral` 9/9, `bearish` 7/12, `auto_ema`) управляет ИСКЛЮЧИТЕЛЬНО начальным входом (`grid_0`). Все колена усреднения (`L1..L7`) рассчитываются строго по базовому капиталу. |
| **I8** | **Naming Standards** | Суффиксы: `_pct` = процент от 100 (например, `3.0` для 3%) \| `_ratio` / `_rate` = доля от единицы (например, `0.03`). Никакой нормализации или двойного деления в коде. |

---

## 3. КАРТА РЕПОЗИТОРИЯ И КОМПОНЕНТОВ

```
cron3/
├── main.py                          # Главная точка входа; инициализация Event Loop, BotCore и TGReceiver
├── consts.py                        # Глобальные константы; читает CFG/app.json + .env
├── c_log.py                         # UnifiedLogger: потокобезопасное цветное логирование (консоль + файл)
├── c_utils.py                       # Utils: общие вспомогательные математические и системные функции
├── watchdog.py                      # Watchdog + Heartbeat: перезапуск при зависаниях и мониторинг тредов
├── tech_debt.md                     # ADR-лог, история архитектурных решений, стандарты кодинга
├── notes.md                         # Рабочие заметки, бэктесты Вселенных, исследовательский журнал
├── AGENT.md                         # Этот файл: оперативная институциональная карта для агентов
│
├── CORE/                            # Торговое ядро бота
│   ├── bot.py                       # BotCore: фасад оркестрации торгового цикла, координация сервисов
│   ├── trade_cycle_manager.py       # TradeCycleManager: оркестратор тиков, свечей и вызова торгового цикла
│   ├── strategy_registry.py         # StrategyRegistry: регистрация Вселенных, валидация паспортов
│   ├── strategy_applicator.py       # StrategyApplicator: генерация боевых runtime-конфигов
│   ├── _utils.py                    # Внутренние утилиты ядра (округление лотов, тиков, времени)
│   │
│   ├── CYCLE/                       # Декомпозированные этапы торгового цикла
│   │   ├── signal_validator.py      # Проверка условий входа (фильтры пауз, спреда, купольной готовности)
│   │   ├── entry_size_resolver.py   # Расчет объема стартового входа (FirstOrderRegime + SmartSize)
│   │   ├── order_submitter.py       # Непосредственная отправка ордеров с установкой флагов идемпотентности
│   │   ├── open_position_lifecycle.py # Проверка и сопровождение открытых позиций (DCA/TP/Ratchet)
│   │   └── finished_position_handler.py # Обработка завершенных сделок, ресет флагов, передача в аналитику
│   │
│   ├── STRATEGY/                    # Математика и алгоритмические модули стратегий
│   │   ├── strategy_config.py       # StrategyConfig: типизированный объект конфигурации Вселенной
│   │   ├── strategy_config_parser.py# StrategyConfigParser: валидация и парсинг параметров паспортов
│   │   ├── momentum_engine.py       # MomentumEngine: Smart Grace, Smart Size, Knife Blocker, Smart TP
│   │   ├── brownian_noise.py        # BrownianNoiseController: криптографический рандом задержек между ордерами
│   │   ├── cross_recycler.py        # CrossRecycler: дискретный кросс-ресайклинг прибыли (50%/75%)
│   │   ├── first_order_regime.py    # FirstOrderRegimeManager: «Правило кулака Сильной руки» (12/7, 9/9, 7/12)
│   │   ├── peeling_manager.py       # PeelingManager: разгрузка зависших позиций (Half-Peel 50%)
│   │   ├── toxicity_engine.py       # ToxicityEngine: детектор токсичных импульсов рынка
│   │   ├── trailing_system.py       # TrailingProfitRatchet: Фибо-лестница трейлинга с возрастным цензом
│   │   ├── trend_size_depender.py   # TrendSizeDepender: масштабирование объема по 1d Dual EMA 7/25
│   │   └── auto_closing_builder.py  # Сборка кастомных пайплайнов авто-закрытия
│   │
│   ├── SERVICES/                    # Фоновые сервисы и штурманские подсистемы
│   │   ├── auto_closer.py           # AutoCloser: авто-закрытие по PnL, карте стагнации и токсичности
│   │   ├── bot_bootstrap.py         # BotBootstrap: инициализация зависимостей и предстартовые проверки
│   │   ├── dome_closer_executor.py  # DomeCloserExecutor: пакетный исполнитель закрытия позиций купола
│   │   ├── emergency_closer.py      # EmergencyCloser: аварийное закрытие и Dollar Risk System (/risk_close)
│   │   ├── leverage_adapter.py      # LeverageAdapter: синхронизация и установка кредитных плеч
│   │   ├── notifier.py              # Notifier: диспетчер Telegram-уведомлений по куполам
│   │   ├── portfolio_closer.py      # PortfolioCloser: плавное закрытие портфеля и фиксация профита
│   │   ├── runtime_backup_service.py# RuntimeBackupService: дебаунс-бэкап рантаймов и состояний
│   │   ├── stagnation_evaluator.py  # StagnationEvaluator: оценка глубины и возраста зависших сделок
│   │   ├── symbol_lifecycle_manager.py # SymbolLifecycleManager: ротация символов и аудит обоймы
│   │   ├── toxicity_extractor.py    # ToxicityExtractor: извлечение метрик токсичности из стакана/сделок
│   │   │
│   │   ├── SELECTOR/                # Штурманская ротация и воронки отбора монет
│   │   │   ├── samsonov_funnel.py   # SamsonovFunnelSelector: воронка Самсонова (авторотация 1-к-1)
│   │   │   ├── alpha_coin_selector.py # AlphaCoinSelector: отбор альфа-монет по волатильности и ликвидности
│   │   │   └── coin_selection_service.py # CoinSelectionService: фасад службы ротации монет
│   │   │
│   │   ├── VOLATILITY/              # Скрининг рынка и волатильность
│   │   │   ├── volatility_scanner.py # VolatilityScanner: сканирование тикеров на импульсы
│   │   │   ├── volatility_manager.py # VolatilityManager: адаптивная подстройка шагов сетки
│   │   │   ├── atr_provider.py      # Провайдер ATR по kline-данным
│   │   │   ├── klines_cache.py      # Кэширование свечных массивов
│   │   │   └── scanner_indicators.py# Расчет технических индикаторов скринера
│   │   │
│   │   └── TOOLS/                   # Инженерные утилиты сопровождения
│   │       ├── audit_and_cleanup_orders.py # Аудит и зачистка ордеров-сирот на бирже
│   │       ├── grid_volume_calculator.py   # Математический калькулятор распределения объемов сетки
│   │       └── restore_missing_tp_orders.py # Реконсиляция и восстановление выпавших Take-Profit лимиток
│   │
│   ├── GRID/                        # Механика усреднения и сетки ордеров
│   │   ├── avg_manager.py           # AvgManager: расчет уровней DCA, Super Grid, защита сетки
│   │   ├── fsm_grid_resolver.py     # FSMGridResolver: математическое разрешение уровней и шагов сетки
│   │   └── dynamic_atr_grid.py      # DynamicATRGrid: динамическая сетка с шагами от ATR
│   │
│   ├── TP/                          # Тейк-профит менеджмент
│   │   ├── tp_manager.py            # TPManager: основной Take-Profit, расчет цен и объемов
│   │   ├── fallback_tp_manager.py   # FallbackTPManager: аварийный страховочный тейк
│   │   ├── dynamic_atr_tp_manager.py# ATRTakeProfitManager: адаптивный тейк от волатильности
│   │   └── take_profit_restorer.py  # Восстановление TP лимиток при перезапусках
│   │
│   ├── ENTRY/                       # Сигналы входа
│   │   └── signal.py                # SignalService: проверка 5m сигналов и таймингов
│   │
│   └── APPLICATOR/                  # Сборка односторонних рантаймов
│       └── side_config_builder.py   # SideConfigBuilder: сборка LONG/SHORT секций
│
├── POS_FSM/                         # Модели позиций и WebSocket-стриминг
│   ├── models.py                    # PositionState, PositionRegistry: единая типизированная модель позиции
│   ├── pos_stream.py                # PosStream: прослушивание Binance User Data Stream WebSocket
│   └── pos_stream_monitor.py        # PosStreamMonitor: мониторинг активности listenKey и реконнект
│
├── RUNTIME_FSM/                     # Менеджмент рантайм-файлов конфигураций
│   ├── runtime_manager.py           # RuntimeManager: потокобезопасный доступ к активным рантаймам в памяти
│   └── runtime_builder.py           # RuntimeBuilder: компиляция CFG/runtime/<strat>/<sym>.json
│
├── API/                             # Сетевой слой биржи и IPC
│   ├── BINANCE/
│   │   ├── client_prod.py           # Боевой клиент Binance Futures API (aiohttp + hmac sha256)
│   │   ├── client_paper.py          # Paper-trading клиент с симуляцией баланса
│   │   ├── client_sim.py            # Высокоскоростной симулятор для бэктестов
│   │   ├── price_stream.py          # WebSocket клиент агрегированных потоков тикеров
│   │   ├── leverage_manager.py      # Управление плечом и режимом маржи (CROSSED)
│   │   ├── income_collector.py      # Сборщик комиссий и реализованного PnL
│   │   ├── historical_trades.py     # Выгрузка архивных сделок
│   │   ├── validator.py             # Валидация точности лотов и минимальной нотиональной стоимости
│   │   ├── public.py                # Публичные REST-эндпоинты биржи (klines, exchangeInfo)
│   │   └── mock_exchange_provider.py# Имитатор биржевого шлюза
│   │
│   └── IPC/
│       └── streamer_hub.py          # IPCStreamerHub: межпроцессный обмен котировками через сокеты/память
│
├── TG/                              # Модульный Телеграм-интерфейс
│   ├── tg_receiver.py               # TGReceiver: поллинг Telegram Bot API и диспетчеризация
│   ├── tg_adapter.py                # TGAdapter: форматирование и безопасная отправка сообщений/алертов
│   ├── keyboards.py                 # Инлайн-клавиатуры и меню управления
│   ├── states.py                    # FSM-состояния взаимодействия в чате
│   ├── template_manager.py          # Шаблоны текстовых отчетов и уведомлений
│   ├── strategy_helpers.py          # Помощники форматирования параметров Вселенных
│   ├── universe_commands.py         # Команды управления Вселенными (/universe, /switch)
│   │
│   └── HANDLERS/                    # Декомпозированные роутеры команд TG
│       ├── base_router.py           # Базовые системные команды (/start, /help, /status, /ping)
│       ├── coins_router.py          # Управление монетами обоймы (/coins, /add_coin, /del_coin, /rotations)
│       ├── positions_router.py      # Инспекция позиций (/positions, /close, /close_all, /panic)
│       ├── analytics_router.py      # Запрос аналитики и графиков (/report, /leaderboard, /plot)
│       ├── universe_router.py       # Просмотр паспортов Вселенных и переключение куполов
│       └── screeners_router.py      # Команды скринеров волатильности
│
├── ANALYTICS/                       # Аналитический комплекс и учет эффективности
│   ├── analytics.py                 # Analytics: главный фасад расчета покупольной аналитики
│   ├── strategy_stats.py            # StrategyStats: расчет Winrate, PnL, Profit Factor, Volume
│   ├── drawdown_tracker.py          # DrawdownTracker: трекинг Max Drawdown и пиков эквити
│   ├── circuit_breaker.py           # CircuitBreaker: защита депозита при аномальных просадках
│   ├── ledger_recorder.py           # LedgerRecorder: запись каждого трейда в журналы LEDGERS/
│   ├── leaderboard.py               # Leaderboard: ранжирование стратегий по ROI и коэффициенту восстановления
│   ├── leaderboard.txt              # Актуальная таблица лидеров Вселенных в текстовом виде
│   ├── trade_velocity.py            # TradeVelocity: учет скорости оборачиваемости капитала
│   ├── plotter.py                   # Plotter: построение графиков эквити (matplotlib)
│   ├── iteration_manager.py         # IterationManager: фиксация итераций авто-закрытия
│   ├── portfolio_aggregator.py      # Агрегатор совокупного портфельного PnL
│   ├── report_service.py            # Генератор сводных отчетов
│   ├── reconstruction.py            # Реконструкция истории позиций по трейдам биржи
│   ├── strategy_toxic_defense.py    # Метрики защищенности от токсичных импульсов
│   ├── STATS/                       # JSON-статистика: analytics_<strat_id>.json, analytics_all.json
│   ├── LEDGERS/                     # Журналы трейдов и ротаций: rotations_ledger.txt, trades.log
│   └── BACKUP_SESSIONS/             # Сессионные архивы состояний
│
├── CFG/                             # Конфигурационная фабрика
│   ├── app.json                     # Глобальный конфиг бота (активные стратегии, телеграм, watchdog)
│   ├── volatile_symbols.json / .txt # Списки отобранных волатильных инструментов
│   ├── universes/                   # Боевые паспорта Вселенных стратегий:
│   │   ├── u17_stephensonE.json     # Флагман: Фибо-сетка L3 (-13.5%), Кулак 12/7, Knife Blocker, Ratchet
│   │   └── u96_omni_zenith_apex.json# Универсал: Асимметричный дуальный режим, Knife Blocker, колокол TP
│   ├── artifacts/classic_tribute/   # Архив 22 классических формул Фабрики Звезд (u16, u6, u17 variants)
│   └── runtime/<strat_id>/<sym>.json# Сгенерированные живые рантаймы для каждой монеты
│
├── CACHE/                           # Локальные кэши и персистентные состояния
│   ├── specifications.json          # Спецификации Binance (шаги цены, мин. лоты, лимиты)
│   ├── leverage_cache.json          # Закэшированные плечи по символам ("GRASSUSDT": true)
│   ├── iteration_state.json         # Состояние PnL текущей итерации авто-закрытия
│   └── rotations_log.json           # Лог ротаций монет Штурмана
│
└── tests/                           # 71 тестовый модуль (483 теста, 100% покрытие бизнес-логики)
    ├── test_strategy_system.py      # Комплексные тесты Вселенных, Knife Blocker, Smart Size
    ├── test_stephenson_mechanisms.py# Верификация инвариантов U17 Stephenson-E
    ├── test_lifecycle_idempotency_flags.py # Идемпотентность in-flight флагов
    ├── test_auto_closer_stagnation.py # Карта стагнации и авто-закрытие
    ├── test_clean_architecture_integrity.py # Контроль отсутствия инлайн-импортов
    └── ...                          # 66 дополнительных модульных тестов
```

---

## 4. ЖИЗНЕННЫЙ ЦИКЛ ОРДЕРА И ПОТОК ДАННЫХ (DATA FLOW)

```
[Binance WebSocket / Polling]
             │
             ▼
   [TradeCycleManager] ──(5m тик свечи + jitter shift_interval_sec)
             │          └── сброс прогресса recent_closes (smart_grace, smart_size, smart_tp)
             ▼
      [SignalService] ── Проверка условий сигнала
             │
   ┌─────────┴─────────┐
   ▼                   ▼
(Нет позиции)      (Позиция открыта)
   │                   │
   ▼                   ▼
[SignalValidator]  [OpenPositionLifecycle]
   │                   ├── DCA Усреднение? ──> [FSMGridResolver] ──> [AvgManager]
   ▼                   │   (pending_avg = True)
[EntrySizeResolver]    ├── Take-Profit?    ──> [TPManager] / [FallbackTPManager]
   │                   │   (pending_rolling_tp = True)
   ▼                   └── Авто-закрытие?  ──> [TrailingProfitRatchet] / [AutoCloser]
[OrderSubmitter]           (is_closing = True ──> Market Close)
   │ (in_position_papper = True)
   ▼                   │
[Binance Futures API] ─┘
   │
   ▼
[WebSocket User Data Stream] ──> [PosStream] ──> [PositionState]
                                                     │
                                                     ▼
                                        [FinishedPositionHandler]
                                         • is_finished = True
                                         • Сброс флагов идемпотентности
                                         • [LedgerRecorder] & [Analytics]
```

---

## 5. ОПЕРАТИВНЫЕ КОМАНДЫ ДЛЯ АГЕНТА

### 1. Перекомпиляция рантаймов после правки паспортов:
```bash
python -c "from CORE.strategy_registry import StrategyRegistry; from CORE.strategy_applicator import StrategyApplicator; reg = StrategyRegistry(); StrategyApplicator().build_all_runtimes(reg, force=True); print('OK')"
```

### 2. Запуск полного тестового набора:
```bash
python -m unittest discover tests
```

### 3. Проверка статуса Git:
```bash
git status
```

### 4. Фиксация изменений:
```bash
git add <измененные_файлы>
git commit -m "тип(модуль): описание изменений"
```
*(Строго соблюдать Правило 9: `git push` ЗАПРЕЩЁН без явного приказа оператора!)*