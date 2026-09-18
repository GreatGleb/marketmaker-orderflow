# 02. Потоки данных

## Схема

```
        Binance WS / REST
                │
                ▼
  app.scripts.watch_ws_and_save ──────────► таблица asset_history (история тиков)
                │                                      │
                │ Lua: снимок цены                      │ (нужна для «активных пар»)
                ▼                                      ▼
             ┌──────────────────┐          MarketDataBuilder.build()
             │      REDIS       │◄─── candles:{SYMBOL} ── watch_binance_candles
             └──────────────────┘                       (autostart=false)
                │  ▲
   снимки цен    │  │  copy_bot_{id}   ◄─── set_profitable_bot (ProfitableBotUpdaterCommand)
                ▼  │
    ┌───────────────────────────────┐        ┌─────────────┐
    │  start_test_bots (симулятор)  │◄───────│  test_bots  │ (снимок на старте)
    └───────────────────────────────┘        └─────────────┘
                │ RPUSH order_queue
                ▼
    ┌───────────────────────────────┐
    │ insert_test_orders (потребитель)
    └───────────────────────────────┘
                │ INSERT
                ▼
          ┌──────────────┐
          │ test_orders  │──────┬────────────────────────────┐
          └──────────────┘      │                            │
                │ свёртка       │ окна до 48 ч               │ хвост после
                │ раз в 10 мин  ▼ (копиботы)                 ▼ watermark
                │        ┌──────────────────────┐   ┌──────────────────────┐
                │        │ get_sorted_by_profit │   │ top_bots_report.py   │
                ▼        └──────────────────────┘   │ profit_by_bot        │
    ┌─────────────────────┐                         └──────────────────────┘
    │ test_order_rollups  │──────────────────────────────────▲
    └─────────────────────┘   всё, что уже свёрнуто
```

Сырьё живёт 72 часа, свёртки — дольше, поэтому отчёт за неделю и глубже
складывается из двух источников: свёртки до границы свёрнутого и сырые
сделки после неё. Подробнее — [10-retention.md](10-retention.md).

## Ключи Redis

| Ключ | Тип | Пишет | Читает | Смысл |
|---|---|---|---|---|
| `price_snapshot:{SYMBOL}` | string (JSON: price, event_time_ms, source) | `price_snapshot.publish_prices`, атомарный Lua | `PriceCache._refresh_once`, `PriceProvider._read_price` | цена с временем события; просроченную бот не получает |
| `price:{SYMBOL}` | string | тот же Lua вместе со снимком | ручная диагностика | числовая копия; сама по себе не разрешает торговлю |
| `candles:{SYMBOL}` | string (JSON-массив цен закрытия) | `watch_binance_candles.py:41` | `BinanceBot.get_prev_minutes_ma` (`binance_bot.py:1673`) | закрытия минутных свечей для MA |
| `order_queue` | list | `demo_test_bot.py:750` (`RPUSH`) | `bulk_insert_orders.py` (`LPOP`) | завершённые виртуальные сделки. Константа — `app/constants/order.py` |
| `copy_bot_{bot_id}` | string (JSON конфига + `published_at`), TTL 90 с | `ProfitableBotUpdaterCommand.command` | `update_config_from_referral_bot`, `DonorGuard` | выбор донора v1; при отсутствии кандидата ключ удаляется |
| `asset_history:stop` | string (флаг) | вручную / служебные скрипты | `watch_ws_and_save.py` | пауза записи в `asset_history` (обслуживание таблицы) |
| `most_volatile_symbol_{tf}` | string | `app/workers/volatile_pair.py` (TTL 60 с) | `demo_test_bot.py:465` | пара для бота с заполненным `min_timeframe_asset_volatility`. Код живой, но в нынешнем парке таких ботов нет |

Адрес Redis — `Settings.REDIS_URL` (`general/app/config.py`). Умолчание
`redis://redis:6379/0` — это DNS-имя сервиса в сети docker-compose; вне неё
задаётся переменной окружения, например `REDIS_URL=redis://localhost:6380/0`.

Срок цены — 120 секунд **от времени события**, а не от HTTP-ответа.
`parse_snapshot` проверяет положительную конечную цену, целочисленную метку
времени и возраст; допустимое опережение часов — 5 секунд. `PUBLISH_PRICE_LUA`
повторяет проверку возраста по часам Redis, отвергает время не новее
предыдущего и устанавливает абсолютный срок `PXAT`. Только принятые события
пишутся в `asset_history`. Кэш ботов имеет собственный монотонный срок, поэтому
зависший Redis не превращает последнюю цену в вечную.

У спотового `bookTicker` нет времени события: используется время приёма WS.
Для него эта защита ограничивает срок после приёма, но не доказывает свежесть
на бирже. REST и потоки с полем `E` проверяются по метке биржи.

После обновления необходимо перезапустить и питатель, и все потребители цен:
старый числовой `price:*` без снимка не принимается новым провайдером.

## Таблицы

### `test_bots` — конфигурации (модель `general/app/db/models.py:453`)

Одна строка = один вариант стратегии. Заполняется `new_bots.py`.
Симулятор читает только `is_active = true` (`test_bot.py:50`).

### `test_orders` — результаты (модель `general/app/db/models.py:370`)

Одна строка = одна закрытая виртуальная сделка. Пишется **только** через
`order_queue` → `bulk_insert_orders`. Ключевые поля:
`bot_id`, `referral_bot_id`, `profit_loss`, `stop_reason_event`,
`open_price`/`close_price`, `open_time`/`close_time`.

Все сделки записываются уже закрытыми (`is_active=False`): «открытая»
сделка живёт только в памяти корутины и при перезапуске процесса теряется
безвозвратно.

### `asset_history` / `asset_exchange_specs`

`asset_exchange_specs` хранит ещё и ставки комиссии по паре —
`maker_commission_rate` / `taker_commission_rate`. Их заполняет
`app/scripts/seed_commission_rates.py` из Binance (`futures_commission_rate`),
а `MarketDataBuilder` забирает вместе с `tick_size` одним запросом
(`AssetExchangeSpecCrud.get_market_data_by_symbol`, `exchange_pair_spec.py:85`).
`NULL` = симулятор возьмёт константу.

`asset_history` — история тиков; из неё `MarketDataBuilder` берёт список
«активных пар» (`asset_history.event_time` за последние 5 минут,
`asset_history.py:356`). `asset_exchange_specs.filters` — JSON фильтров
Binance, откуда берётся `tick_size` (`PRICE_FILTER.tickSize`,
`exchange_pair_spec.py:34`).

Итог: **пара попадёт в `shared_data` только если по ней есть и свежие
тики в `asset_history`, и запись в `asset_exchange_specs`.**

## Формат сообщения в `order_queue`

`demo_test_bot.py:727-749`. Все Decimal сериализуются в строки,
datetime — через `json_serializer` (:191) в ISO. Потребитель разбирает
даты обратно списком `DATETIME_FIELDS` (`bulk_insert_orders.py:52`).

```json
{
  "asset_symbol": "BIOUSDT", "order_type": "BUY", "balance": "1000",
  "open_price": "...", "open_time": "ISO", "open_fee": "...",
  "stop_loss_price": "...", "bot_id": 42,
  "close_price": "...", "close_time": "ISO", "close_fee": "...",
  "profit_loss": "...", "is_active": false,
  "start_updown_ticks": 10, "stop_loss_ticks": 30, "stop_success_ticks": 20,
  "stop_reason_event": "stop-won", "referral_bot_id": null,
  "created_at": "ISO", "updated_at": "ISO"
}
```

**Добавляете поле в `TestOrder`** — надо трогать три места: миграцию,
словарь `order_data` (`demo_test_bot.py:727`) и, если это дата,
`DATETIME_FIELDS` в `bulk_insert_orders.py:52`.

## Потребитель очереди (`bulk_insert_orders.py`)

Цикл: 10 итераций × до 4000 `LPOP` → батчи по 1000 → `bulk_create`.
Если очередь пуста — сон 60 с. Между витками внешнего цикла — сон до минуты.

При ошибке вставки батч **возвращается в очередь** (`LPUSH`) и повторяется на
следующем витке, а транзакция откатывается. Возврат работает, пока длина
очереди не превысила `QUEUE_MAX_LENGTH = 200_000` (~125 МБ): дальше батч
отбрасывается с предупреждением в лог. Это защита от того, что долгий простой
БД съест всю память Redis — `maxmemory` в `docker-compose.yml` не задан.

Одна сделка в очереди занимает ~626 байт. При 15 000 активных ботов очередь
растёт примерно на 900 МБ в час, если её не вычерпывать.

### Актуальность донора перед входом (2026-09-18)

`read_donor` требует `published_at` моложе 90 секунд; старые бессрочные ключи
без отметки не принимаются. Воркер продлевает TTL при публикации и удаляет
ключ, когда кандидатов нет. Это срок публикации, а не рейтинга:
`WindowCache` по-прежнему живёт `max(30 с, 5% окна)`.

`DonorGuard.wait` проверяет выбранный конфиг во время ожидания цены/MA
раз в секунду и при завершении ожидания. Замена/отзыв отменяет ожидающую
задачу; следующий вызов `simulate_bot` заново выбирает цепочку и уровни.
Новый `published_at` у того же конфига сохраняет текущее ожидание.
Для v2/v3 `select_copybot` перепроверяет цепочку раз в 30 секунд во время
ожидания и непосредственно перед входом. Сессия БД закрывается после
проверки; во время удержания позиции донор больше не меняется.
После финальной проверки через Redis/SQL цена сигнала сверяется с текущей
доступной ценой: сменившийся или истёкший сигнал отменяет попытку входа.

### Объём v3 перед открытием (2026-09-18)

`AssetExchangeSpecCrud.extract_step_sizes` читает ограничения без перехода
через float. `LOT_DATA_KEYS` переносит обычный и рыночный лоты, а также
`MIN_NOTIONAL.notional` через стартовую сборку и догрузку пары.
`compound_order_size` округляет 99% счёта вниз по пересечению сеток,
проверяет обе пары min/maxQty и минимальный номинал после округления.
Нулевой `MARKET_LOT_SIZE.stepSize` отключает только рыночную сетку;
границы рыночного количества остаются обязательными.

Финальная проверка использует цену сигнала до открытия. Номинал фиксируется
для удержания и расчёта PnL. Отказ из-за номинала/минимального лота —
`shortage`; превышение максимума — `pair`; неполные/неверные данные — `data`.
Только `shortage` увеличивает существующий счётчик остановки v3.

Это проверка бумажной позиции по сохранённым фильтрам и цене входа.
Отдельного актуального mark price в симуляторе нет; биржевая проверка
номинала по mark price и реальное исполнение остаются вне этого прогона.
Фильтр добавления отслеживаемых пар использует mark price отдельно,
в `watched_affordability.check_purchase`.
