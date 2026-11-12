# ==============================================================================
# FILE: ENTITY_PROTOCOL.md
# ROLE: Единый канонический протокол торговых сущностей и базового стенда бэктестов
# ==============================================================================

# Единый Протокол Сущностей и Базовый Стенд Симуляции (`cron3` + `cron3TradeBacktest`)

Документ фиксирует однозначную математику, семантику исполнения и параметры базового стенда для всех стратегий линейки (`u96_omni_zenith13`, `u96_omni_zenith13atr`, `u96_omni_alligator`, `u97_zenith13_hedge_shield`), чтобы исключить разночтения между продом, паспортами вселенных и новыми прогонами бэктестов.

---

## 1. Канонический базовый стенд бэктеста (SSOT: `cron3TradeBacktest/CFG/backtest.json`)

Все сравнительные 180-дневные и декадные прогоны стартуют от единого зафиксированного профиля в [`cron3TradeBacktest/CFG/backtest.json`](../cron3TradeBacktest/CFG/backtest.json):

| Параметр | Каноническое значение | Примечание |
|---|---:|---|
| **Ростер стратегий** | `u96_omni_zenith13`, `u96_omni_zenith13atr`, `u96_omni_alligator`, `u97_zenith13_hedge_shield` | 4 флагманские стратегии |
| **Стартовый баланс (`start_balance_usdt`)** | **`1435.0` USDT** | Базовый депозит на купол |
| **Размер стороны (`invest_size_usdt`)** | **`1200.0` USDT** | На каждую сторону (`LONG` и `SHORT`) |
| **Плечо и маржа** | **`20x` `CROSSED`** | Кросс-маржа |
| **Комиссия Taker (`fee_rate`)** | **`0.0005` (`0.05%`)** | Все `MARKET`-ордера (`L0`, DCA, AutoClose, Rotation, SL) |
| **Комиссия Maker (`maker_fee_rate`)** | **`0.0002` (`0.02%`)** | Лимитные ордера (`Take-Profit`) |
| **Проскальзывание рыночных ордеров (`market_slippage`)** | **`0.0001` (`0.01%`)** | Единая реалистичная модель в `SimulatedBinanceClient`: применяется ко **всем** `MARKET`-ордерам на входе и выходе (L0-вход, усреднения/DCA, Peeling, Cross-Recycling, Fallback TP, `AutoClose`, `Rotation`, `PerCoinStopLoss`); `BUY` дороже на `+0.01%`, `SELL` дешевле на `-0.01%` (`0.02%` на круг поверх тиков `aggTrades` и `0.05%` Taker). Для `LIMIT`-ордеров (TP) — `0%` |
| **Фандинг (8h Binance Funding)** | **Исключен (`funding_modeled: false`)** | Осознанная конвенция симулятора (`client_sim.py`) |
| **Перевод прибыли на Spot** | **`enabled: false` (выкл, пресет `50.0%`)** | Читается строго из SSOT `cron3/CFG/app.json -> active_strategies` |
| **Ротации пула** | **Каждые 10 дней (`alpha_rotations_180d.json`)** | Жесткая ликвидация открытых позиций `ROTATION` в конце каждой декады |
| **Тихий режим логгера** | **`CRON3_BACKTEST=1`** | Нулевой I/O в консоль и на диск (`UnifiedLogger._silent_backtest`, пока `CRON3_BACKTEST_FILE_LOG != "1"`) |
| **Каденс сигнала в бэктесте** | **`shift_interval_sec = 13`**, сброс на 5m свече | Сброс счетчиков `recent_closes` (`smart_grace`, `smart_size`, `smart_tp`) |

---

## 2. Правило Single Source of Truth (SSOT)

1. **`transfer_profit_to_spot` (отключен по умолчанию: `enabled: false`):**
   - Единственный источник истины: `cron3/CFG/app.json -> active_strategies.<strategy_id>.transfer_profit_to_spot`.
   - Дефолт в коде сервиса [`ProfitTransferService`](CORE/SERVICES/profit_transfer_service.py): `enabled: False`, `DEFAULT_TRANSFER_PROFIT_PCT = 50.0`.
   - **Категорически запрещено** дублировать блок `transfer_profit_to_spot` внутри `CFG/universes/*.json`.
   - [`BacktestBotCore`](../cron3TradeBacktest/ENGINE/backtest_core.py) автоматически наследует `transfer_profit_to_spot` из `CFG/app.json`, если параметр не переопределен явно в конструкторе.
   - Учет капитала: выведенный на Spot профит списывается с фьючерсного баланса `balance` (не участвует в доступной марже фьючерсов), начисляется на `spot_balance`, а итоговое состояние считается как `Wealth = Futures Equity + Spot Balance` (без ложных скачков просадки).

2. **Логирование бэктеста:**
   - При `CRON3_BACKTEST=1` (и отсутствии `CRON3_BACKTEST_FILE_LOG=1`) `UnifiedLogger` отключает создание директорий/файлов логов, чтение размера файлов с диска и запись записей на диск/в консоль.

---

## 3. Сущность: `per_coin_stop_loss` (Индивидуальный стоп-лосс монеты)

- **Код прода:** [`PerCoinStopLossManager`](CORE/SERVICES/per_coin_stop_loss.py)
- **Код бэктеста:** [`BacktestBotCore.check_per_coin_stop_loss`](../cron3TradeBacktest/ENGINE/backtest_core.py) -> `_purge_symbol_on_stop_loss`
- **Конфиги паспортов:** секция `per_coin_stop_loss` в каждом файле `CFG/universes/*.json`.

### Обязательный протокол:
1. **База расчета порога:** отклонение считается **строго от цены входа первого колена `L0` (`entry0`)**, НЕ от средней цены позиции (`avg_entry`):
   - для `SHORT`: `(price - entry0) / entry0 * 100.0 >= max_adverse_short_pct` (канон: `130.0%` для всех 4 стратегий);
   - для `LONG`: `(entry0 - price) / entry0 * 100.0 >= max_adverse_long_pct` (канон: `null` — отключено).
2. **Полная ликвидация пары (`BOTH SIDES`) — НЕ одной стороны:**
   При срабатывании стоп-лосса на любой пробитой стороне принудительно закрываются **ОБЕ открытые стороны (`LONG` И `SHORT`)** рыночными ордерами `MARKET` (в симуляторе по единой рыночной ставке `market_slippage = 0.01%` на сторону и `taker_fee = 0.05%`, без двойного начисления). Оставлять противоположную ногу висеть **запрещено**.
3. **Зачистка ордеров и состояний:**
   - снимаются **ВСЕ** открытые лимитные ордера по символу для обеих сторон;
   - сбрасываются FSM-состояния (`PositionState`) и очереди усреднений (`AvgManager`) по обеим сторонам;
   - в `CoinGridRiskAnalytics` (в бэктесте) финализируются циклы с причиной `STOP_LOSS`.
4. **Удаление монеты из торгового списка / вывод «в утиль»:**
   - **На ПРОДЕ** (`PerCoinStopLossManager.execute_stop_loss` -> `bot_core.delete_symbol` / [`SymbolLifecycleManager.delete_symbol`](CORE/SERVICES/symbol_lifecycle_manager.py)): монета **физически удаляется из торгового списка в `CFG/app.json`** (`active_strategies.<strat_id>.symbols`, карты `invest_size`, общий `symbols`), удаляется из `bot.symbols`, `StrategyRegistry`, `fsm_states`, `runtime_configs`, `prices`, отключается от WebSocket-стримов (`pos_stream`, `ws_interpreter`, `pos_monitor`, перезапуск price stream), а ее рантайм-файлы удаляются с диска (квота освобождается).
   - **В БЭКТЕСТЕ** (`BacktestBotCore._purge_symbol_on_stop_loss`): полный эквивалент для текущей декады — монета удаляется из активных списков и помещается в `_purged_symbols` (повторный вход в символ запрещен до конца текущей декады, пока следующая 10-дневная ротация не задаст новый пул).
5. **Инвариант коридора (`CorridorCalculator`):**
   Пороги SL (`entry0 * (1 ± pct / 100)`) принудительно включаются в границы безопасного коридора тиков; при наличии открытой ноги с SL пробой тика проскочить невозможно.

---

## 4. Сущность: `auto_closing` (`trailing_ratchet` + Chronos)

- **Назначение:** фиксация чистой прибыли корзины (`LONG + SHORT`) на импульсе отскока с откатом по Фибо-лестнице храповика (`activation` -> `floor`), либо гарантированная эвакуация зависших позиций по возрасту (`E1: 36h -> +$8`, `E2: 72h -> +$5`), умноженным на `scale_money`, при соблюдении `min_guaranteed_profit_usdt`.
- **Действие при срабатывании:**
  1. Закрытие **ОБЕИХ** открытых сторон (`LONG` И `SHORT`) рыночными ордерами `MARKET` (`0.01%` `market_slippage` на сторону, `0.05%` `taker_fee`).
  2. Отмена всех лимитных ордеров на обеих сторонах и полный сброс `PositionState` / `AvgManager`.
  3. Вызов `ProfitTransferService`: при положительном чистом PnL итерации выше порога `min_transfer_usdt` переводит `transfer_profit_pct` (`50%`) с фьючерсов на Spot (`UMFUTURE_MAIN`).
  4. **В отличие от Stop-Loss**, монета **НЕ** уходит «в утиль» и готова к открытию нового цикла на следующем сигнале.

---

## 5. Сущность: `counter_grid_frontload` (Авансовое усиление L0)

- **Реализация:** [`CounterGridFrontloadManager`](CORE/STRATEGY/counter_grid_frontload.py).
- **Присутствие в паспортах:** схема объявлена во всех 4 стратегиях:
  - `u97_zenith13_hedge_shield.json`: `"enabled": true`
  - `u96_omni_zenith13.json`, `u96_omni_zenith13atr.json`, `u96_omni_alligator.json`: `"enabled": false` (совместимость).
- **Механика:**
  - Если противоположная нога находится в глубокой сетке (`==5 -> 1`, `==6 -> 2`, `>=7 -> 3` колена по `absorb_tiers`), стартовый вход `L0` авансово забирает объем ближайших колен `L1..Lk`, с жестким ограничением суммарного капитала стороны `<= 100% invest_size`.
  - Поглощенные ступени `L1..Lk` помечаются `is_active: true` и `_absorbed_to_l0: true` строго после исполнения `MARKET`-ордера входа.
  - Поле `PositionState.cur_step` является динамическим свойством, а `RiskCalculatingUtils.get_current_grid_level` игнорирует `_absorbed_to_l0`, сохраняя поведение `L0` для TP/grace/smart-модулей до реального добора `L(k+1)+`.

---

## 6. Сущности: `cross_side_recycling`, `peeling`, `directional_trend_gate`

- **`cross_side_recycling`:** включается при достижении противоположной стороной порога ступеней (например `>=3 -> 0.5`), направляя долю прибыли закрытия на разгрузку глубокой противоположной ноги.
- **`peeling` (`PeelingManager`):** частичная разгрузка (`Half-Peel 50%`) глубоких колен с соблюдением железобетонного минимума тейк-профита `>= 0.50%`.
- **`directional_trend_gate`:** присутствует во всех 4 паспортах со значением `"enabled": false` по умолчанию для симметрии схемы.

---

## 7. Инварианты скоростного тикового ядра (`cron3TradeBacktest/ENGINE/`)

1. **Компактный поток тиков (`SymbolArrayView`):** тики хранятся в плотных типизированных NumPy-массивах + `UInt16`-кодах символов вместо списков строковых кортежей (~10x экономия RAM без `MemoryError` на 30–40M тиках).
2. **Обязательная проверка неинициализированного фронтира (`CorridorCalculator`):** если у открытой позиции `entry0 <= 0.0` или граничный уровень `price is None` (первый тик сразу после `L0` до пересчета `avg_manager` / `tp_manager`), пропуск тиков блокируется (`bounds = 0.0`) до полной материализации уровней в коридоре.
3. **Запрет ложного кулдауна при `t=0`:** проверка `last_failed_entry_ts > 0.0` (строго положительный timestamp, а не проверка относительно `0.0`).
4. **Порядок обхода границ в коридоре:** сетка усреднения, точные `%` просадки `dd_pct`, уровни `peel_tp_pct`, `fallback_price`, границы `PerCoinSL`, пороги `trailing_tp`, а также немедленный выход на уже пробитых триггерах.

