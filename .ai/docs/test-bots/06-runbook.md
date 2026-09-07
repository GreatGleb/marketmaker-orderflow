# 06. Runbook — как это запускать и наблюдать

Все команды — из корня репозитория. Контейнер приложения:
`orderflow_general`.

## Полный старт с нуля

```bash
cp general/.env.example general/.env      # прописать BINANCE_* и TELEGRAM_*
./run.sh start                            # docker-compose up --build -d
./run.sh init                             # сид пар и спецификаций Binance
```

`./run.sh init` выполняет `seed_binance_data`, `seed_watched_pairs_usdt`,
`set_isolate_mode_and_leverage_for_binance_pairs`. Без `seed_binance_data`
в `asset_exchange_specs` не будет `filters`, а значит не будет `tick_size`
и `shared_data` окажется пустым.

Отдельно — ставки комиссии по парам (в `init` не входят, нужны боевые ключи):

```bash
./run.sh scripts commissions                          # пары активных ботов
docker exec -it orderflow_general python -m app.scripts.seed_commission_rates --missing
docker exec -it orderflow_general python -m app.scripts.seed_commission_rates -s BIOUSDT
```

Без этого шага симулятор работает, но по всем парам берёт константу 0.05 %
из `app/constants/commissions.py` вместо реальной ставки аккаунта.
Запускать после `new_bots.py` (скрипт по умолчанию берёт пары активных ботов)
и перезапускать симулятор, чтобы он перечитал `shared_data`.

Миграции применяются автоматически: `general/boot.sh` → `alembic upgrade head`
→ `supervisord`.

## Что уже запущено само

`general/supervisord.conf`, `autostart=true`: `fastapi`, `symbols_history`
(цены), `test_bots` (симулятор), `insert_test_orders`, `set_profitable_bot`.
То есть после `./run.sh start` тестовые боты **уже работают**.

Отдельно от supervisord, по расписанию Celery (контейнеры
`orderflow_celery_beat` и `orderflow_celery_worker`), идёт чистка базы:
`app.tasks.retention` раз в час и `app.tasks.test_order_rollup` раз в десять
минут. Без них диск кончается примерно за неделю —
[10-retention.md](10-retention.md).

```bash
docker exec -it orderflow_general supervisorctl status
docker exec -it orderflow_general supervisorctl restart test_bots
docker exec -it orderflow_general supervisorctl start candles_history   # для MA-ботов
docker exec -it orderflow_general tail -f /var/log/test_bots.log
```

Логи внутри контейнера: `/var/log/test_bots.log`, `symbols_history.log`,
`insert_test_orders.log`, `set_profitable_bot.log`, `candles_history.log`
(+ соответствующие `.err`).

## Ручной запуск (для отладки)

```bash
docker exec -it orderflow_general supervisorctl stop test_bots   # чтобы не было двух копий
docker exec -it orderflow_general python -m app.scripts.start_test_bots
```

Скрипт поднимает поток, читающий `stdin`: ввод `stop` ставит `stop_event` и
корректно тушит боты (`start_test_bots.py:15-26`). Поэтому запускать нужно с
`-it`. Первые 60 секунд — тишина: `asyncio.sleep(60)` ждёт наполнения Redis.

## Пересоздание парка ботов

```bash
docker exec -it orderflow_general python -m app.scripts.new_bots
```

⚠️ **Скрипт начинается с `TRUNCATE TABLE test_bots RESTART IDENTITY CASCADE`**
(`new_bots.py:340`). `CASCADE` затрагивает и `test_orders` (там FK на
`test_bots`) — **вся накопленная статистика стирается**. Перед запуском
делайте дамп или закомментируйте блок `if 1:` с TRUNCATE.

Что создаётся текущей версией (`create_bots`, `new_bots.py:328`):

* пара жёстко задана в коде: `symbol = "BIOUSDT"` (:313) — менять здесь;
* ~15 120 тиковых ботов: перебор `start × stop_lose × stop_win × trailing × wait`
  (:329-333);
* 80 копиботов v1 (20 окон × 24h-фильтр × ref-фильтр) (:382-385);
* 20 копиботов v2 (:406-414);
* MA-боты и процентные боты — закомментированы.

После пересоздания перезапустите симулятор — иначе он работает по старому
снимку: `supervisorctl restart test_bots`.

## Отчёты

```bash
# топ-10 за всё время
docker exec -it orderflow_general python -m app.scripts.top_bots_report

# топ-20 обычных ботов за последние 2 часа
docker exec -it orderflow_general python -m app.scripts.top_bots_report -H 2 -just_not_copy 1 -top_count 20

# только копиботы v1 за 30 минут
docker exec -it orderflow_general python -m app.scripts.top_bots_report -m 30 -just_copy 1

# только копиботы v2
docker exec -it orderflow_general python -m app.scripts.top_bots_report -just_copy_v2 1
```

Флаги `-just_copy` / `-just_copy_v2` / `-just_not_copy` — строковые,
значение не важно, важно наличие (проверка на truthy).

## Проверка живости (диагностика по порядку)

```bash
# 1. Цены идут?
docker exec -it orderflow_redis redis-cli keys 'price:*' | head
docker exec -it orderflow_redis redis-cli get price:BIOUSDT

# 2. Очередь не забита? (значит потребитель работает)
docker exec -it orderflow_redis redis-cli llen order_queue

# 3. Сделки пишутся?
docker exec -it orderflow_postgres psql -U postgres -c \
  "select count(*), max(created_at) from test_orders;"

# 4. Копиботы получили доноров?
docker exec -it orderflow_redis redis-cli keys 'copy_bot_*' | head

# 5. Свечи для MA (если нужны)
docker exec -it orderflow_redis redis-cli keys 'candles:*' | head

# 6. Ставки комиссии засеяны?
docker exec -it orderflow_postgres psql -U postgres -c \
  "select symbol, taker_commission_rate from asset_exchange_specs
    where taker_commission_rate is not null limit 10;"
```

```bash
# 7. Чистка работает? (иначе диск кончится за неделю)
docker exec -it orderflow_postgres psql -U postgres -c \
  "select min(created_at) as самая_старая_сделка from test_orders;"
docker exec -it orderflow_postgres psql -U postgres -c \
  "select max(bucket_start), now() - max(bucket_start) as отставание_свёрток
     from test_order_rollups;"
```

Типичные диагнозы:

| Симптом | Причина |
|---|---|
| `test_orders` не растёт, `order_queue` растёт | лежит `insert_test_orders` |
| `test_orders` растёт и не чистится | встали Celery-задачи или отстают свёртки, см. [10-retention.md](10-retention.md) |
| `order_queue` = 0 и `test_orders` не растёт | симулятор висит: нет `price:{SYMBOL}` или пара не попала в `shared_data` |
| в логе `not symbols data` | пары нет в `shared_data`: нет свежих тиков в `asset_history` либо нет `filters` в `asset_exchange_specs` |
| в логе `there no symbol` | у бота пустой `symbol` и не нашёлся донор |
| в логе `there no copybot_v2 ref` | нет прибыльных копиботов v1 в окне |
| `❌ Не удалось найти реферального бота` | пуст ключ `copy_bot_{id}` — не работает `set_profitable_bot` |
| все сделки `stop-long-lose` | это ожидаемо, см. [08-gotchas.md](08-gotchas.md) пункт 1 |

## Питатель цен: три режима (`general/.env`)

| `MARKET_DATA_SOURCE` | Что делает |
|---|---|
| `ws` | `wss://fstream.binance.com/ws/!ticker@arr` — боевой USDT-M поток |
| `rest` | поллинг `fapi/v1/ticker/24hr` раз в `MARKET_DATA_REST_INTERVAL_SEC` — для регионов, где push-поток недоступен |
| `spot_ws` | спотовый WS по watched-парам, максимальная частота. **Спотовые цены отличаются от фьючерсных** |

Известная проблема окружения: фьючерсный WS коннектится, но не отдаёт
фреймы — тогда `rest` или `spot_ws`.
