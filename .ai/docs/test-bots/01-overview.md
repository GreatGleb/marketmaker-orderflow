# 01. Обзор подсистемы

## Зачем это нужно

Задача — перебором найти прибыльную комбинацию параметров стратегии
(«на сколько тиков отойти от цены для входа», «где стоп-лосс», «где
тейк-профит», «ждать ли пересечения MA»). Вместо торговли реальными
деньгами создаются десятки тысяч записей в `test_bots`, каждая — отдельный
набор параметров. Один процесс симулирует их **все одновременно** на одном
и том же потоке живых цен и записывает виртуальные сделки в `test_orders`.

Дальше по `test_orders` считается прибыль каждой конфигурации
(`top_bots_report.py`), а «копиботы» умеют динамически подхватывать
конфиг того бота, который был прибылен за последние N минут.

Реальная торговля — это отдельная подсистема (`general/app/bots/binance_bot.py`),
которая здесь используется только как источник свечей для MA.

## Процессы

Всё крутится внутри контейнера `orderflow_general` под supervisord
(`general/supervisord.conf`). Для тестовых ботов важны пять программ:

| supervisor program | модуль | autostart | роль |
|---|---|---|---|
| `symbols_history` | `app.scripts.watch_ws_and_save` | **true** | питатель цен: Binance → Redis `price:*` + таблица `asset_history` |
| `candles_history` | `app.scripts.watch_binance_candles` | false | питатель свечей: Binance kline 1m → Redis `candles:*` (нужен только MA-ботам) |
| `test_bots` | `app.scripts.start_test_bots` | **true** | сам симулятор |
| `insert_test_orders` | `app.workers.scripts.bull_insert_test_orders` | **true** | потребитель очереди `order_queue` → `INSERT INTO test_orders` |
| `set_profitable_bot` | `app.workers.scripts.set_profitable_bot` | **true** | считает лидеров прибыльности → Redis `copy_bot_{id}` (нужен копиботам v1) |

Минимальный рабочий набор: `symbols_history` + `test_bots` + `insert_test_orders`.

## Карта файлов

### Ядро

| Файл | Роль |
|---|---|
| `general/app/scripts/start_test_bots.py` | точка входа. Поднимает `asyncio`, поток-слушатель `stdin` для команды `stop`, запускает `StartTestBotsCommand` |
| `general/app/bots/demo_test_bot.py` | **вся логика симуляции**. `StartTestBotsCommand.command` (:51) — бутстрап, `simulate_bot` (:196) — цикл одной сделки |

### Логика расчётов (stateless, легко тестировать)

| Файл | Роль |
|---|---|
| `general/app/sub_services/logic/price_calculator.py` | TP/SL/безубыток/PnL. Все формулы с учётом комиссий |
| `general/app/sub_services/logic/exit_strategy.py` | три реализации условия выхода: фиксированный (:78), трейлинг (:10), MA (:104) |
| `general/app/sub_services/logic/market_setup.py` | `MarketDataBuilder.build()` — словарь `symbol → tick_size + ставки комиссии`, по одному запросу на пару |
| `general/app/sub_services/watchers/price_provider.py` | `PriceProvider.get_price` (:42) — цена из Redis с логом и замедлением при отсутствии; `PriceWatcher.wait_for_entry_price` (:77) — ожидание условия входа |

### Конфиги ботов и прибыльность

| Файл | Роль |
|---|---|
| `general/app/workers/profitable_bot_updater.py` | подбор реферального бота для копиботов, пересчёт процентов в тики (:131) |
| `general/app/crud/test_bot.py` | запросы к `test_bots`, главный — `get_sorted_by_profit` (:29) |
| `general/app/crud/test_orders.py` | запросы к `test_orders`, `bulk_create` |
| `general/app/db/models.py:430` | модель `TestBot` |
| `general/app/db/models.py:339` | модель `TestOrder` |

### Обвязка

| Файл | Роль |
|---|---|
| `general/app/scripts/new_bots.py` | **генератор ботов**: перебор параметров → `test_bots`. Начинается с TRUNCATE! |
| `general/app/scripts/top_bots_report.py` | CLI-отчёт «топ прибыльных ботов» |
| `general/app/scripts/seed_commission_rates.py` | заполняет ставки maker/taker по парам из Binance |
| `general/app/workers/bulk_insert_orders.py` | потребитель `order_queue` |
| `general/app/scripts/watch_ws_and_save.py` | питатель цен (три режима: `ws` / `rest` / `spot_ws`) |
| `general/app/scripts/watch_binance_candles.py` | питатель свечей для MA |
| `general/app/utils.py` | `Command` — базовый класс: прогоняет метод `command()` через DI FastAPI вне HTTP-запроса |
| `general/app/dependencies.py` | `get_session`, `get_redis` (адрес Redis захардкожен: `redis://:@redis:6379/0`) |
| `general/app/db/base.py` | синглтоны движка SQLAlchemy и пула Redis |
| `general/app/sub_services/notifications/factory.py` | Telegram-уведомления об упавших ботах |

## Как работает бутстрап (`demo_test_bot.py:51-116`)

```python
command(session, redis, bot_crud)          # всё через DI из app/utils.py:Command
  price_provider = PriceProvider(redis)
  binance_bot    = BinanceBot(is_need_prod_for_data=True, redis=redis)  # только для MA
  await asyncio.sleep(60)                  # :65  ждём, пока питатель цен наполнит Redis
  ждём, пока в test_bots появятся активные боты            # :67-84 опрос раз в 60 с
  shared_data = await MarketDataBuilder(session).build()   # :89 symbol → tick_size, ОДИН РАЗ
  active_bots — снимок конфигов, взятый ОДИН РАЗ
  → превращаются в namedtuple BotObject                    # :79 отвязка от сессии SQLAlchemy
  для каждого бота: asyncio.create_task(_run_loop(bot))    # :83
      _run_loop: while not stop_event: try simulate_bot() except → telegram + sleep(1)
  await asyncio.gather(*tasks)
```

Важные следствия:

* **Снимок конфигов.** Изменили строку в `test_bots` — процесс надо
  перезапустить. Исключение: копиботы каждый цикл перечитывают
  реферальный конфиг из БД/Redis.
* **Снимок `shared_data`.** Новая пара, появившаяся в `asset_history`
  после старта, не получит `tick_size` — бот на ней будет бесконечно
  печатать `not symbols data` и спать по 60 с.
* **`BotObject` — namedtuple, а не `TestBot`.** У него нет методов модели
  (см. [08-gotchas.md](08-gotchas.md), пункт про `.clone()`).
* **Одна задача на бота.** 10 000 активных ботов = 10 000 корутин, каждая
  с `sleep(0.1)` в цикле выхода. Это основной потолок производительности.
* **Падение бота не роняет процесс**: исключение ловится, летит уведомление
  в Telegram, через секунду бот стартует заново с начала цикла.
