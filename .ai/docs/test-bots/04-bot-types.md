# 04. Типы ботов

Тип бота не хранится отдельным полем — он **выводится из того, какие поля
заполнены**. Порядок проверок в `simulate_bot` определяет приоритет.

```
copybot_v2_time_in_minutes != NULL          → копибот v2
copy_bot_min_time_profitability_min != NULL → копибот v1
consider_ma_for_open_order = true           → MA-вход
consider_ma_for_close_order = true          → MA-выход
все три *_percents заполнены                → процентный режим
use_trailing_stop = true                    → трейлинг вместо фикс. TP
иначе                                       → обычный тиковый бот
```

Комбинации складываются: копибот v1 наследует режимы донора,
процентный режим применяется поверх любого, трейлинг — поверх тикового.

---

## 1. Обычный тиковый бот

**Поля:** `symbol`, `balance`, `start_updown_ticks`, `stop_loss_ticks`,
`stop_success_ticks`, `time_to_wait_for_entry_price_to_open_order_in_seconds`,
`use_trailing_stop`, `is_active = true`.

Вход — пробой `цена ± start_updown_ticks × tick_size`, выход — фиксированный
SL/TP. Именно такие боты генерирует `new_bots.py` (перебор 15 × 18 × 14 × 2 × 2
≈ 15 000 конфигураций).

## 2. Процентный бот

**Поля:** вместо тиков — `stop_win_percents`, `stop_loss_percents`,
`start_updown_percents` (нужны **все три**, иначе режим не включится:
`profitable_bot_updater.py:133`).

Проценты каждый цикл пересчитываются в тики по текущей цене — это позволяет
одному конфигу одинаково вести себя на дорогих и дешёвых парах.

До 2026-08-26 этот режим падал у обычных ботов — см.
[08-gotchas.md](08-gotchas.md), пункт 3.
`new_bots.py` процентные боты не создаёт (код закомментирован, `new_bots.py:353-367`).

## 3. MA-бот (скользящие средние)

**Поля:** `consider_ma_for_open_order`, `consider_ma_for_close_order`,
`ma_number_of_candles_for_open_order`, `ma_number_of_candles_for_close_order`.

Быстрая MA = меньшее из двух чисел, медленная = большее (порядок нормализуется
в `price_provider.py:94-99` и `exit_strategy.py:112-117`).

* **Вход** (`price_provider.py:93-154`): пересечение за последние 2 минуты.
  Золотой крест → BUY, крест смерти → SELL. Опрос раз в 10 с.
* **Выход** (`exit_strategy.py:104`): обратное пересечение **или** быстрая MA
  ушла за цену при цене выше безубытка.

Таймаут ожидания входа у MA-бота — 12 часов, `time_to_wait...` на него не
влияет (до 2026-08-26 влиял, см. [08-gotchas.md](08-gotchas.md), пункт 5).

**Требует запущенного `candles_history`** (`watch_binance_candles`,
в supervisord `autostart=false`). Без него `candles:{SYMBOL}` пуст,
`get_prev_minutes_ma` возвращает пустые списки → вход никогда не случится.

Число хранимых свечей считается из БД как `max(ma_number_*) + 10`
(`watch_binance_candles.py:19-38`) — добавили бота с большим MA, надо
перезапустить и питатель свечей.

## 4. Копибот v1

**Поля:** `copy_bot_min_time_profitability_min` (окно прибыльности в минутах),
`copybot_v1_check_for_24h_profitability`,
`copybot_v1_check_for_referral_bot_profitability`, `symbol = ''`.

Логика: раз в 30 с процесс `set_profitable_bot`
(`ProfitableBotUpdaterCommand.command`, :244) для каждого копибота считает
самого прибыльного **обычного** бота (`just_not_copy_bots=True`) за его окно,
опционально фильтруя:

* `check_24h_profitability` — оставить только тех, кто прибылен и за 24 ч;
* `check_for_referral_bot_profitability` — оставить только тех, кто был
  прибылен, когда его уже копировали (группировка по `referral_bot_id`).

Победитель сериализуется в Redis `copy_bot_{id}`. Копибот на каждом цикле
читает этот ключ (`demo_test_bot.py:156`) и торгует **чужими параметрами и
чужой парой**, записывая сделку на себя, а ID донора — в
`test_orders.referral_bot_id`.

Смысл: измерить, работает ли стратегия «следовать за текущим лидером» и
какое окно оценки прибыльности оптимально. `new_bots.py` создаёт
20 окон × 2 × 2 = 80 копиботов v1.

## 5. Копибот v2

**Поля:** `copybot_v2_time_in_minutes`, `symbol = ''`.

Мета-уровень: копирует не обычного бота, а **самого прибыльного копибота v1**
за своё окно (`get_copybot_config`, `profitable_bot_updater.py:36`,
`just_copy_bots=True`). Затем проходит и ветку v1 — это не лишний шаг:
у копибота v1 нет собственных торговых параметров, есть только правило
«бери лидера за N минут», поэтому его надо применить, чтобы получить
параметры реального бота.

Второй шаг читает готовый ответ из Redis по ключу донора
(`copy_bot_{id донора}`) — до 2026-08-27 он вместо этого пересчитывал
лидеров запросами в БД на каждом цикле сделки.

Итог — цепочка `бот → копибот v1 → копибот v2`. `new_bots.py` создаёт
20 копиботов v2 (по одному на окно), MA-боты — закомментированы (`new_bots.py:442-477`).

Вопрос из `toDo.yml` («нужна ли в `update_config_from_referral_bot` ветка
`if original_bot_config.copybot_v2_time_in_minutes is not None`») закрыт
2026-08-27: ветка убрана, обе версии копиботов ходят в Redis. Сам двойной
проход v2 через логику v1 остался — он необходим.

## 6. Трейлинг-стоп (модификатор)

`use_trailing_stop = true` меняет только расчёт тейк-профита: вместо
фиксированного уровня TP тянется за пиком цены на `stop_success_ticks`
(`price_calculator.py:37`, `exit_strategy.py:10`).

Стоп-лосс при этом остаётся фиксированным: за ценой тянется только уровень
выхода в плюс. У трейлинговой формулы нет поправки на комиссии, в отличие от
фиксированной, — чистая прибыль выходит меньше номинальных
`stop_success_ticks`.

⚠️ До 2026-08-26 трейлинг не работал вообще (пик не обновлялся), и
трейлинговые боты не могли закрыться с `stop-won` — см.
[08-gotchas.md](08-gotchas.md), пункт 2.

## Что НЕ используется (мёртвые ветки)

* **Выбор пары по волатильности.** `min_timeframe_asset_volatility` и Redis
  `most_volatile_symbol_{tf}`: код в `demo_test_bot.py:269-274` закомментирован,
  `symbol` всегда берётся из конфига. Воркер `volatile_pair.py` и
  `set_volatile_pair_value` существуют, но в supervisord `autostart=false`.
* `copy_bot_max_time_profitability_min` — поле есть в модели, в коде не читается.
* `TestOrder.referral_bot_from_profit_func` — поле есть, запись закомментирована
  (`demo_test_bot.py:533`).
* `TestBot.total_profit` — колонка не пишется и не читается: запись из
  `top_bots_report.py` удалена.
