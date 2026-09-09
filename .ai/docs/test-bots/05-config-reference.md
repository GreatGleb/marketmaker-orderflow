# 05. Справочник полей

## `TestBot` — `general/app/db/models.py:461`, таблица `test_bots`

| Поле | Тип | Где читается | Смысл |
|---|---|---|---|
| `id` | int | `demo_test_bot.py:756` | под этим ID пишутся сделки (`test_orders.bot_id`) |
| `symbol` | str NOT NULL | `demo_test_bot.py:497` | торговая пара. У копиботов `''` — берётся у донора |
| `balance` | numeric NOT NULL | `demo_test_bot.py:751` | виртуальный размер позиции. Влияет на PnL и `open_fee`. У копиботов подменяется на 1000 (`:363`) |
| `is_active` | bool | `test_bot.py:50` | берётся в работу симулятором. Читается **один раз на старте** |
| `start_updown_ticks` | int | `demo_test_bot.py:540-545` | отступ уровней входа от текущей цены, в тиках |
| `stop_loss_ticks` | int | `price_calculator.py:59` | стоп-лосс в тиках от цены открытия |
| `stop_success_ticks` | int | `price_calculator.py:10` / `:38` | тейк-профит в тиках (или дистанция трейлинга) |
| `time_to_wait_for_entry_price_to_open_order_in_seconds` | numeric | `demo_test_bot.py:563` | таймаут ожидания входа. У MA-ботов игнорируется |
| `use_trailing_stop` | bool NULL | `demo_test_bot.py:385`, `:610` | трейлинговый TP вместо фиксированного |
| `stop_win_percents` | numeric NULL | `profitable_bot_updater.py:219` | TP в процентах от цены (нужны все три `*_percents`) |
| `stop_loss_percents` | numeric NULL | там же | SL в процентах |
| `start_updown_percents` | numeric NULL | там же | отступ входа в процентах |
| `consider_ma_for_open_order` | bool | `price_provider.py:172` | вход по пересечению MA |
| `consider_ma_for_close_order` | bool | `demo_test_bot.py:603`, `:666` | выход по MA (уровни SL/TP обнуляются) |
| `ma_number_of_candles_for_open_order` | numeric NULL | `price_provider.py:173` | период одной MA |
| `ma_number_of_candles_for_close_order` | numeric NULL | там же | период второй MA. Быстрая/медленная определяются сравнением, а не именами полей |
| `copy_bot_min_time_profitability_min` | numeric NULL | `demo_test_bot.py:466` | **признак копибота v1** + окно оценки прибыльности донора, минуты |
| `copybot_v1_check_for_24h_profitability` | bool | `profitable_bot_updater.py:355` | доп. фильтр донора: прибылен и за 24 ч |
| `copybot_v1_check_for_referral_bot_profitability` | bool | там же | доп. фильтр: прибылен как донор копиботов |
| `copybot_v2_time_in_minutes` | numeric NULL | `demo_test_bot.py:445` | **признак копибота v2** + окно оценки прибыльности копибота-донора |
| `min_timeframe_asset_volatility` | numeric NULL | `demo_test_bot.py:486` | окно в минутах, за которое берётся самая волатильная пара. Заполнено → пара из Redis вместо `symbol`. В нынешнем парке не заполнено ни у кого |
| `copy_bot_max_time_profitability_min` | numeric NULL | — | **не используется** |
| `total_profit` | numeric NOT NULL | — | **не используется**: не пишется и не читается |
| `created_at` / `updated_at` | timestamptz | — | из `BaseId` |

Метод `TestBot.clone()` (`models.py:566`) — копия строки как нового объекта;
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

`Decimal` в JSON не положить, а строку — нельзя: `'0'` истинна для `not`, и
проверки «поле не задано» на ней ломаются. Обратно в `Decimal` поле поднимает
потребитель, через `Decimal(str(...))` — иначе двоичный хвост float попадёт в
расчёт цены и в ключ `most_volatile_symbol_*`. Подробнее — [08-gotchas.md](08-gotchas.md),
пункт 9.

`NULL` у донора превращается в `0` (кроме тиков) — для потребителей это и
означает «не задано».

## `TestOrder` — `general/app/db/models.py:370`, таблица `test_orders`

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
| `referral_bot_from_profit_func` | всегда `NULL`, запись закомментирована |

## Константы

| Константа | Файл | Значение |
|---|---|---|
| `COMMISSION_OPEN` | `app/constants/commissions.py` | `0.0005` (0.05 %) — только запасное значение |
| `COMMISSION_CLOSE` | там же | `0.0005` — только запасное значение |
| `ORDER_QUEUE_KEY` | `app/constants/order.py` | `"order_queue"` |
| шаг цикла удержания |  `demo_test_bot.py:720` | `0.1` с |
| стартовая задержка | `demo_test_bot.py:93` | `60` с |
| правило «30 секунд» | `demo_test_bot.py:711` | 30 с / 10 тиков |
| интервал `set_profitable_bot` | `profitable_bot_updater.py:436` | `30` с |

Комиссии — единственное, что делает симуляцию нетривиальной: `take_profit`
и `close_not_lose_price` считаются так, чтобы после обеих комиссий сделка
давала заданный чистый профит (`price_calculator.py:10-55`).

**Ставка берётся по паре, а не из константы.** Симулятор читает
`asset_exchange_specs.taker_commission_rate` (через `shared_data`,
`demo_test_bot.py:515`) и передаёт её во все расчёты: безубыток, тейк-профит,
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

Адрес Redis в `.env` **не задаётся** — он захардкожен в `app/dependencies.py`.
