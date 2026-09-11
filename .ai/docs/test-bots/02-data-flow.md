# 02. Потоки данных

## Схема

```
        Binance WS / REST
                │
                ▼
  app.scripts.watch_ws_and_save ──────────► таблица asset_history (история тиков)
                │                                      │
                │ MSET price:{SYMBOL}                   │ (нужна для «активных пар»)
                ▼                                      ▼
             ┌──────────────────┐          MarketDataBuilder.build()
             │      REDIS       │◄─── candles:{SYMBOL} ── watch_binance_candles
             └──────────────────┘                       (autostart=false)
                │  ▲
   price:*      │  │  copy_bot_{id}   ◄─── set_profitable_bot (ProfitableBotUpdaterCommand)
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
| `price:{SYMBOL}` | string | `watch_ws_and_save.py:131` (пайплайн `SET` с TTL) | `PriceProvider.get_price` (`price_provider.py:119`) | последняя цена. **Без него симулятор висит молча** |
| `candles:{SYMBOL}` | string (JSON-массив цен закрытия) | `watch_binance_candles.py:41` | `BinanceBot.get_prev_minutes_ma` (`binance_bot.py:1673`) | закрытия минутных свечей для MA |
| `order_queue` | list | `demo_test_bot.py:750` (`RPUSH`) | `bulk_insert_orders.py` (`LPOP`) | завершённые виртуальные сделки. Константа — `app/constants/order.py` |
| `copy_bot_{bot_id}` | string (JSON конфига) | `profitable_bot_updater.py:500` | `demo_test_bot.py:342` | конфиг реферального бота для копибота v1 |
| `asset_history:stop` | string (флаг) | вручную / служебные скрипты | `watch_ws_and_save.py` | пауза записи в `asset_history` (обслуживание таблицы) |
| `most_volatile_symbol_{tf}` | string | `app/workers/volatile_pair.py` (TTL 60 с) | `demo_test_bot.py:465` | пара для бота с заполненным `min_timeframe_asset_volatility`. Код живой, но в нынешнем парке таких ботов нет |

Адрес Redis — `Settings.REDIS_URL` (`general/app/config.py`). Умолчание
`redis://redis:6379/0` — это DNS-имя сервиса в сети docker-compose; вне неё
задаётся переменной окружения, например `REDIS_URL=redis://localhost:6380/0`.

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
