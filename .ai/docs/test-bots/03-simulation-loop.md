# 03. `simulate_bot` — построчный разбор

> **Изменилось 2026-09-19.** Вход и выход больше не живут в этом методе:
> их держит алгоритм стратегии (`app/strategies/`). Симулятор остался
> владельцем всего общего — выбор донора и пары, рыночные данные,
> комиссии, проверка лота у компаундирующего v3, запись результата в
> очередь, — и на каждом шаге спрашивает алгоритм:
>
> | Что | Где теперь |
> |---|---|
> | пересчёт процентов в тики | `LegacyAlgorithm.prepare` |
> | уровни входа и таймаут ожидания | `LegacyAlgorithm.entry_levels` |
> | ожидание входа | `LegacyAlgorithm.wait_for_entry` |
> | стопы, трейлинг, цена безубытка, `TestOrder` | `LegacyAlgorithm.open_position` |
> | условия выхода и правило 30 секунд | `LegacyAlgorithm.should_exit` |
>
> Алгоритм выбирается по **фактически исполняемой** стратегии: у
> копибота это стратегия донора, а не его собственного парка. Ключ,
> которого нет в реестре (`app/strategies/registry.py`), бота не
> запускает. Разбор ниже описывает порядок шагов — он не менялся, —
> но за телом входа и выхода идти теперь в `app/strategies/legacy/`.

Файл: `general/app/bots/demo_test_bot.py`, метод `simulate_bot` (:430).
Это ядро подсистемы. Один вызов = один полный цикл «выбрать конфиг → дождаться
входа → держать позицию → закрыть → записать». Внутри метода — свой
`while not stop_event.is_set()` (:439), поэтому один вызов крутится
бесконечно, пока не сработает `return` (тогда внешний `_run_loop` вызовет
метод заново).

Вложенность циклов:

```
_run_loop (:152)              while not stop_event   — перезапуск после исключения
 └ simulate_bot (:439)       while not stop_event   — цикл сделок
    ├ ожидание входа (:537)  while True             — повтор попытки входа по таймауту
    └ удержание (:663)       while not stop_event   — тик 0.1 с, проверка условий выхода
```

## Фаза 1. Определение конфига (:440-535)

```
referral_bot_id = None
bot_id = original_bot_config.id       # ID НАСТОЯЩЕГО бота — под ним пишется сделка
bot_config = None                     # конфиг ПАРАМЕТРОВ — может быть чужим
```

Разделение `bot_id` / `bot_config` — ключевая идея копиботов: сделка
приписывается копиботу, а параметры берутся у бота-донора.

0. **Копибот v3** (ветка перед v2), если задано `copybot_v3_time_in_minutes`:
   спускается на два шага сразу — `get_copybot_config` с `donor_version=2`
   даёт лучшего копибота v2, затем тот же метод с `donor_version=1` и окном
   найденного v2 даёт копибота v1. Дальше путь общий с v2. Не нашёл на любом
   шаге → `sleep(60)` + `return`; заглушки, как у боевого бота, здесь нет
   намеренно.

   У компаундирующего бота (`copybot_v3_compound_balance`) перед этим
   проверяется `copybot_v3_stopped_at`: остановленный не торгует и донора не
   считает.
1. **Копибот v2** (`:445-462`), если задано `copybot_v2_time_in_minutes`:
   открывает **новую** сессию БД (`DatabaseSessionManager.create` — синглтон),
   вызывает `ProfitableBotUpdaterCommand.get_copybot_config` — берёт самого
   прибыльного копибота v1 за окно `copybot_v2_time_in_minutes` минут.
   Не нашёл → `sleep(60)` + `return`.
2. Если конфиг не переопределён — `bot_config = original_bot_config` (:464).
3. `is_it_copy = bot_config.copy_bot_min_time_profitability_min` (:466) —
   признак копибота v1. Для копибота v2 он берётся у подобранного донора,
   то есть **v2 всегда проходит и через ветку v1**.
4. **Копибот v1** (`:468-484`): `update_config_from_referral_bot` (:337)
   возвращает `{'config', 'referral_bot_id'}`. Обе версии копиботов читают
   готовый JSON из Redis `copy_bot_{id}`, который раскладывает процесс
   `set_profitable_bot`; у v2 под этим ключом лежит конфиг донора-копибота v1.
   До 2026-08-27 у v2 была отдельная ветка с пересчётом лидеров запросами в
   БД на каждом цикле сделки — см. [08-gotchas.md](08-gotchas.md), пункт 10b.

   Копибот v3 проходит и эту ветку: у v1 нет собственных торговых параметров,
   поэтому конфиг реального бота достаётся из Redis по ключу найденного v1.

Дальше в цикле есть ещё два места, которые работают только у компаундирующего
копибота v3: перед ожиданием входа считается размер позиции
(`compound_order_size`) и проверяются границы лота и цены, а после записи
сделки счёт обновляется на её результат и уезжает в `test_bots.balance`.
Подробности — [04-bot-types.md](04-bot-types.md), раздел про копибота v3.

   Из конфига донора собирается **новый объект `TestBot`** (:362-398) —
   с `balance=1000` жёстко в коде. Переносятся только поля из списка в
   `get_bot_config_by_params`; остальные получают значения по умолчанию.
   Донора нет → `sleep(60)` + `return`.
5. `symbol = bot_config.symbol` (:497). Пусто → `sleep(60)` + `return`.
   Копиботы создаются с `symbol = ''`, поэтому пара у них всегда приходит
   от донора.
6. `tick_size = shared_data[symbol]["tick_size"]` (:504-510). Пары нет в
   снимке → лог `not symbols data`, `sleep(60)`, `return`.
   Оттуда же берётся **ставка комиссии по паре** —
   `taker_commission_rate` (:515). `NULL` → константа `COMMISSION_OPEN`.
   Дальше она передаётся во все расчёты: безубыток, тейк-профит, комиссии, PnL.
7. `update_config_for_percentage` (:526, реализация `profitable_bot_updater.py:216`):
   если у бота заданы все три процента — переводит их в тики по текущей цене
   (`round(price * pct/100 / tick_size)`, минимум 1) и возвращает **клон**
   конфига. Иначе возвращает конфиг как есть.

## Фаза 2. Ожидание входа (:537-588)

```python
initial_price    = await price_provider.get_price(symbol)
entry_price_buy  = initial_price + start_updown_ticks * tick_size
entry_price_sell = initial_price - start_updown_ticks * tick_size
```

Стратегия входа — пробой: ставим два уровня по обе стороны от текущей цены
и входим в ту сторону, куда цена уйдёт первой.

Таймаут ожидания (`:556-565`), приоритет сверху вниз:

1. `12 * 60 * 60` секунд, если `consider_ma_for_open_order` — MA-бот входит
   только по пересечению средних, `time_to_wait` у него игнорируется;
2. `time_to_wait_for_entry_price_to_open_order_in_seconds`, если MA выключен
   и поле задано;
3. `1` секунда по умолчанию.

До 2026-08-26 приоритет был обратным, и `time_to_wait` перекрывал
MA-режим — см. [08-gotchas.md](08-gotchas.md), пункт 5.

`PriceWatcher.wait_for_entry_price` (`price_provider.py:161`) под
`asyncio.wait_for`. Внутри два режима:

* **обычный**: опрос цены каждые 0.1 с, `price >= entry_price_buy` → BUY,
  `price <= entry_price_sell` → SELL;
* **MA** (`consider_ma_for_open_order`): каждые 10 с считает две скользящие
  (быстрая = меньшее из двух `ma_number_of_candles_*`, медленная = большее)
  за 0/1/2 минуты назад и ищет пересечение: золотой крест → BUY,
  крест смерти → SELL. Данных мало → `continue`.

Таймаут → `is_timeout_occurred = True` → `continue` внешнего `while True`
(:586): пересчитываем `initial_price` от новой цены и пробуем снова.
Успех → `break` (:588).

Возвращается `TradeType.BUY.value` — **строка**, не enum. Дальше сравнения
`order.order_type == TradeType.BUY` работают только потому, что
`TradeType(str, enum.Enum)`.

## Фаза 3. Расчёт уровней (:590-656)

```
open_price               = entry_price
price_from_previous_step = entry_price       # для трейлинга
peak_favorable_price     = entry_price       # для трейлинга
close_not_lose_price     = calculate_close_not_lose_price(open_price, trade_type)
```

`close_not_lose_price` (`price_calculator.py:71`) — цена, при которой сделка
выходит в ноль после обеих комиссий. Ставка — та самая, что взята по паре на
шаге 6, поэтому у пар с разной комиссией безубыток и тейк-профит различаются.
Используется как «не закрывать в убыток» фильтр.

Дальше две ветки:

* **`consider_ma_for_close_order = False`** (:603-642): считаются
  `stop_loss_price` (`price_calculator.py:59`) и тейк-профит —
  трейлинговый (`:38`, от `peak_favorable_price`) либо фиксированный
  (`:10`, с поправкой на комиссии). Создаётся `TestOrder` в памяти
  с заполненными тиками и `open_fee = balance * COMMISSION_OPEN`.
* **`consider_ma_for_close_order = True`** (:644-656): `TestOrder` с
  нулями во всех уровнях — выход считает только MA.

`TestOrder` здесь — **объект в памяти**, никогда не добавляется в сессию.
Он служит контейнером состояния и мутируется стратегией выхода
(`order.stop_reason_event`, `order.stop_loss_price`).

## Фаза 4. Удержание позиции (:663-720)

Цикл с шагом `asyncio.sleep(0.1)`. Каждый тик:

1. `updated_price = await price_provider.get_price(symbol)`
2. Условие выхода — одна из трёх стратегий (`exit_strategy.py`):
   * MA (`:104`) — обратное пересечение MA либо возврат к безубытку;
   * трейлинг (`:10`) — подтягивает TP за пиком, стоп-лосс фиксированный;
   * фиксированный (`:78`) — `price <= stop_loss` → `stop-loosed`,
     `price >= take_profit` (и выше безубытка) → `stop-won`.
3. **Правило 30 секунд** (:702-713):

```python
price_diff_from_cnl = updated_price - close_not_lose_price   # для BUY
diff_ticks = price_diff_from_cnl / tick_size
if diff_ticks < 10 and just30sec_elapsed_time >= 30:
    order.stop_reason_event = StopReasonEvent.STOP_LONG_LOSE.value
    break
```

Смысл (из `toDo.yml`): «тестовые ордера не дольше 30 секунд, если нет
прибыльности хотя бы 10 тиков». Отсчёт идёт от безубытка, а не от цены
входа, то есть 10 тиков — это чистая прибыль после обеих комиссий.

До 2026-08-26 здесь было умножение вместо деления, из-за чего порог
срабатывал всегда и **все** сделки закрывались через 30 секунд — см.
[08-gotchas.md](08-gotchas.md), пункт 1.

## Фаза 5. Закрытие и запись (:722-774)

```
close_price = await price_provider.get_price(symbol)   # ЗАНОВО, не цена триггера
pnl = PriceCalculator.calculate_pnl(balance, close_price, open_price, trade_type)
order_data = {...}
await redis.rpush(ORDER_QUEUE_KEY, json.dumps(order_data, default=json_serializer))
```

`close_price` перезапрашивается после выхода из цикла, поэтому записанная
цена закрытия может отличаться от цены, по которой сработало условие —
своего рода имитация проскальзывания. PnL считается по нотионалу
`balance / open_price` с вычетом обеих комиссий (`price_calculator.py:107`).
Комиссии для записи в `test_orders` берутся из той же функции
`calculate_fees` (`price_calculator.py:89`), поэтому поля `open_fee`/`close_fee`
сходятся с `profit_loss`.

После `rpush` управление уходит на начало `while` (:439): конфиг
переопределяется заново (для копиботов — с новым донором), и цикл повторяется.
Пауз между сделками нет.
