# 05. Справочник полей

## `TestBot` — `general/app/db/models.py`, `class TestBot`, таблица `test_bots`

| Поле | Тип | Где читается | Смысл |
|---|---|---|---|
| `id` | int | `demo_test_bot.py:735` | под этим ID пишутся сделки (`test_orders.bot_id`) |
| `symbol` | str NOT NULL | `demo_test_bot.py:476` | торговая пара. У копиботов `''` — берётся у донора |
| `balance` | numeric NOT NULL | `demo_test_bot.py:730` | виртуальный размер позиции. Влияет на PnL и `open_fee`. У копиботов подменяется на 1000 (`:386`) |
| `is_active` | bool | `test_bot.py:50` | берётся в работу симулятором. Читается **один раз на старте** |
| `start_updown_ticks` | int | `demo_test_bot.py:519-524` | отступ уровней входа от текущей цены, в тиках |
| `stop_loss_ticks` | int | `price_calculator.py:59` | стоп-лосс в тиках от цены открытия |
| `stop_success_ticks` | int | `price_calculator.py:10` / `:38` | тейк-профит в тиках (или дистанция трейлинга) |
| `time_to_wait_for_entry_price_to_open_order_in_seconds` | numeric | `demo_test_bot.py:542` | таймаут ожидания входа. У MA-ботов игнорируется |
| `use_trailing_stop` | bool NULL | `demo_test_bot.py:408`, `:613` | трейлинговый TP вместо фиксированного |
| `stop_win_percents` | numeric NULL | `profitable_bot_updater.py:224` | TP в процентах от цены (нужны все три `*_percents`) |
| `stop_loss_percents` | numeric NULL | там же | SL в процентах |
| `start_updown_percents` | numeric NULL | там же | отступ входа в процентах |
| `consider_ma_for_open_order` | bool | `price_provider.py:172` | вход по пересечению MA |
| `consider_ma_for_close_order` | bool | `demo_test_bot.py:606`, `:669` | выход по MA (уровни SL/TP обнуляются) |
| `ma_number_of_candles_for_open_order` | numeric NULL | `price_provider.py:173` | период одной MA |
| `ma_number_of_candles_for_close_order` | numeric NULL | там же | период второй MA. Быстрая/медленная определяются сравнением, а не именами полей |
| `copy_bot_min_time_profitability_min` | numeric NULL | `demo_test_bot.py:445` | **признак копибота v1** + окно оценки прибыльности донора, минуты |
| `copybot_v1_check_for_24h_profitability` | bool | `profitable_bot_updater.py:421` | доп. фильтр донора: прибылен и за 24 ч |
| `copybot_v1_exclude_losing_donors` | bool | там же | доп. фильтр: выбросить донора, у которого копиры за сутки в минусе. Отсутствие истории копирования — не причина выбрасывать |
| `copybot_v2_time_in_minutes` | numeric NULL | `demo_test_bot.py:425` | **признак копибота v2** + окно оценки прибыльности копибота-донора |
| `copybot_v3_time_in_minutes` | numeric NULL | `simulate_bot`, ветка перед v2 | **признак копибота v3** + окно, за которое ранжируются копиботы v2. У обоих ботов `720` — те же 12 часов, что зашиты в боевом `_get_best_copy_bot` |
| `copybot_v3_compound_balance` | bool | `simulate_bot`, `compound_order_size` | различает пару ботов v3. `false` — баланс всегда 1000, как у парка; `true` — бот ведёт счёт, в позицию идёт 99% баланса, количество округляется по шагу лота |
| `copybot_v3_stopped_at` | timestamptz NULL | `_v3_stop`, `_v3_is_stopped` | когда на балансе перестал набираться минимальный лот. Бот остаётся `is_active`, иначе выпал бы из отчётов вместе с фактом остановки |
| `min_timeframe_asset_volatility` | numeric NULL | `demo_test_bot.py:465` | окно в минутах, за которое берётся самая волатильная пара. Заполнено → пара из Redis вместо `symbol`. В нынешнем парке не заполнено ни у кого |
| `strategy_id` | int NOT NULL | `with_strategy`, `strategy_id_by_bot` | стратегия, экземпляром которой является бот. У всего нынешнего парка — `legacy` |
| `donor_scope` | jsonb NULL | `donors_within_scope` | пул стратегий-доноров копибота: `{"mode": "all"}` либо `{"mode": "list", "strategies": ["legacy"]}`. `NULL` у обычных ботов и означает «без ограничений». По умолчанию сиды ставят явный список своей стратегии: с `all` копибот сменил бы алгоритм сам собой в день подключения второй стратегии |
| `created_at` / `updated_at` | timestamptz | — | из `BaseId` |

## `Strategy` — `general/app/db/models.py`, `class Strategy`, таблица `strategies`

Регистрация стратегии, а не её реализация: сам алгоритм выбирается по
`key` (`app/constants/strategy.py`, `STRATEGY_LEGACY`), а строка нужна,
чтобы на стратегию ссылались боты и сделки.

| Поле | Тип | Смысл |
|---|---|---|
| `key` | str UNIQUE | технический ключ: `legacy`, дальше `strategy_0`. Код, сиды и конфиги ссылаются на него, а не на числовой `id` — тот в каждом развёртывании свой |
| `title` | str | название для человека |
| `allows_new_entries` | bool | разрешены ли новые входы. Снятый признак не закрывает уже открытые позиции: их доводит тот же алгоритм, с которым открывались |

Метод `TestBot.clone()` (`models.py:555`) — копия строки как нового объекта;
нужен `update_config_for_percentage`, чтобы не мутировать общий конфиг.

## Конфиг донора в Redis — `copy_bot_{id}`

Подмножество тех же полей, сериализованное в JSON
(`profitable_bot_updater.py:169`), из которого копибот собирает себе `TestBot`
(`demo_test_bot.py:362`, `binance_bot.py:229`). Типы — только числа и `bool`:

| В словаре | Тип | Поля |
|---|---|---|
| целые | `int` | тики (могут быть `null`), `ma_number_of_candles_*` |
| дробные | `float` | `*_percents`, `min_timeframe_asset_volatility`, `time_to_wait_...` |
| флаги | `bool` | `use_trailing_stop`, `consider_ma_*` |
| пара | `str` | `symbol` |
| стратегия | `int` | `strategy_id` — по какому алгоритму копибот на самом деле торгует |

`Decimal` в JSON не положить, а строку — нельзя: `'0'` истинна для `not`, и
проверки «поле не задано» на ней ломаются. Обратно в `Decimal` поле поднимает
потребитель, через `Decimal(str(...))` — иначе двоичный хвост float попадёт в
расчёт цены и в ключ `most_volatile_symbol_*`. Подробнее — [08-gotchas.md](08-gotchas.md),
пункт 9.

`NULL` у донора превращается в `0` (кроме тиков) — для потребителей это и
означает «не задано».

## `TestOrder` — `general/app/db/models.py`, `class TestOrder`, таблица `test_orders`

| Поле | Откуда берётся |
|---|---|
| `asset_symbol` | пара, на которой реально шла сделка (у копибота — пара донора) |
| `order_type` | `"BUY"` / `"SELL"`, строка из `TradeType` |
| `balance` | `bot_config.balance` на момент сделки |
| `open_price` | цена, на которой сработало условие входа |
| `open_time` | `datetime.now(UTC)` в момент расчёта уровней |
| `open_fee` | `PriceCalculator.calculate_fees` — объём входа × ставку, то есть `balance * COMMISSION_OPEN` |
| `stop_loss_price` | рассчитанный SL (0 у MA-ботов) |
| `close_price` | цена, **перезапрошенная** после выхода из цикла удержания |
| `close_time` | `datetime.now(UTC)` при записи |
| `close_fee` | `PriceCalculator.calculate_fees` — объём позиции по цене закрытия × ставку. Сходится с `profit_loss` |
| `profit_loss` | `PriceCalculator.calculate_pnl` — нотионал `balance/open_price`, минус обе комиссии |
| `is_active` | всегда `false` |
| `bot_id` | ID бота, которому приписана сделка |
| `referral_bot_id` | ID донора (только у копиботов), иначе `NULL` |
| `start_updown_ticks`, `stop_loss_ticks`, `stop_success_ticks` | фактические тики этой сделки (у процентных ботов — уже пересчитанные) |
| `stop_reason_event` | `stop-won` / `stop-loosed` / `stop-long-lose` (`app/enums/event_type.py`) |
| `strategy_id` | стратегия парка, которому принадлежит бот — `original_bot_config.strategy_id` |
| `executed_strategy_id` | стратегия, по которой сделка исполнена на самом деле. У обычного бота совпадает с предыдущей, у копибота её задаёт конечный донор (`strategy_id` из конфига в Redis). Разрезы по этим двум полям не складываются: одна сделка попадает в оба |
| `algorithm_version` | версия алгоритма на момент открытия позиции, `algorithm_version_for`. `NULL` — версия неизвестна: так помечена вся история до появления поля |
| `donor_chain` | цепочка id доноров от копибота к обычному боту, `[v2_id, v1_id, donor_id]`; `NULL` у обычных ботов |

## Константы

| Константа | Файл | Значение |
|---|---|---|
| `COMMISSION_OPEN` | `app/constants/commissions.py` | `0.0005` (0.05 %) — только запасное значение |
| `COMMISSION_CLOSE` | там же | `0.0005` — только запасное значение |
| `ORDER_QUEUE_KEY` | `app/constants/order.py` | `"order_queue"` |
| шаг цикла удержания |  `demo_test_bot.py:699` | `0.1` с |
| стартовая задержка | `demo_test_bot.py:93` | `60` с |
| правило «30 секунд» | `demo_test_bot.py:690` | 30 с / 10 тиков |
| интервал `set_profitable_bot` | `profitable_bot_updater.py:506` | `30` с |

Комиссии — единственное, что делает симуляцию нетривиальной: `take_profit`
и `close_not_lose_price` считаются так, чтобы после обеих комиссий сделка
давала заданный чистый профит (`price_calculator.py:10-55`).

**Ставка берётся по паре, а не из константы.** Симулятор читает
`asset_exchange_specs.taker_commission_rate` (через `shared_data`,
`demo_test_bot.py:494`) и передаёт её во все расчёты: безубыток, тейк-профит,
`open_fee`, `close_fee`, `profit_loss`. Taker применяется с обеих сторон —
и вход по пробою, и выход по стопу в реальности исполняются по рынку.

Если ставка по паре не заполнена (`NULL`), берётся `COMMISSION_OPEN`. Заполнить:
`python -m app.scripts.seed_commission_rates`.

| Колонка `asset_exchange_specs` | Смысл |
|---|---|
| `taker_commission_rate` | ставка исполнения по рынку, её использует симулятор |
| `maker_commission_rate` | ставка лимитного ордера, сохраняется для будущих сценариев |

## Переменные окружения (`general/.env`)

Симулятору нужны:

| Переменная | Зачем |
|---|---|
| `DB_URL` | `postgresql+asyncpg://postgres:secret@db:5432/postgres` |
| `ENVIRONMENT` | `BinanceBot` смотрит `== "prod"`; для симулятора не критично, ключи всё равно берутся боевые (`is_need_prod_for_data=True`) |
| `BINANCE_API_KEY` / `BINANCE_SECRET_KEY` | нужны конструктору `BinanceBot`, реально используются только для `get_klines` в питателе свечей |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_TEST_BOT_TOPIC_ID` | уведомления об упавших ботах; без токена фабрика вернёт `None` и уведомления просто не уйдут |
| `MARKET_DATA_SOURCE` | режим питателя цен: `ws` / `rest` / `spot_ws` |
| `REDIS_URL` | цены, очередь сделок, ключи копиботов, флаги симуляторов. Умолчание `redis://redis:6379/0` — имя сервиса в сети docker-compose; менять только при запуске вне неё |
| `CELERY_BROKER` | брокер celery и backend результатов. Пусто (умолчание) — тот же `REDIS_URL`. `CELERY_BROKER_URL` и `CELERY_RESULT_BACKEND` в `.env` ни на что не влияют: `app/tasks.py` перезаписывает их этой настройкой |
