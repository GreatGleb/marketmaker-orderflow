"""Стратегия 1: точность исполнения единичной сделки и краевые случаи.

Ревизия перед запуском симуляции. Проверяется не «работает ли
стратегия», а то, не приписывает ли симулятор себе исполнение, которого
на бирже не случилось бы:

1. лимитка исполняется на пробитии, а не на касании уровня;
2. рыночный выход исполняется хуже наблюдаемой цены;
3. тайм-стоп отсчитывается от факта входа, а не от выставления заявки,
   и уступает состоявшемуся движению цены;
4. ATR считается только по закрытым свечам и отбраковывает аномалии;
5. сработавший выход закрывает сделку один раз.

Заглушки, база и сеть не нужны.
"""
import asyncio

from decimal import Decimal
from types import SimpleNamespace

from app.enums.event_type import StopReasonEvent
from app.enums.trade_type import TradeType
from app.strategies.base import ConfigError
from app.strategies.strategy_1.algorithm import SIDE_SHORT, Strategy1Algorithm
from tests.test_strategy_1 import context

ALGORITHM = Strategy1Algorithm()
SYMBOL = "ONEUSDT"
TICK = Decimal("0.00001")


def config(**overrides):
    base = {"entry_offset_atr": "2", "take_profit_fraction": "0.5",
            "stop_loss_atr": "3", "time_stop_seconds": "10"}
    base.update(overrides)

    return ALGORITHM.parse_config(base)


# --- 1. Касание против пробития ---------------------------------------

async def test_touch_does_not_fill():
    """Цена дошла ровно до лимитки — это очередь, а не исполнение.

    На своём уровне наша заявка последняя в очереди: чтобы её взяли,
    агрессор должен выбрать весь стоящий там объём. В тиковой ленте цен
    этого не видно, поэтому касание исполнением не считается.
    """
    cfg = config(max_wait_seconds="0.3")
    # ATR 1% от 100 × 2 = отступ 2: заявка на покупку стоит ровно на 98.
    ctx = context(cfg, prices=[Decimal("100"), Decimal("98"), Decimal("98")])
    ctx.tick_size = TICK

    levels = await ALGORITHM.entry_levels(ctx, Decimal("100"))
    result = await ALGORITHM.wait_for_entry(ctx, levels)

    assert result.timed_out, "касание уровня не должно исполнять заявку"


async def test_cross_by_one_tick_fills():
    """Пробитие на тик — исполнение, и по цене заявки, а не рынка."""
    cfg = config()
    ctx = context(cfg, prices=[Decimal("100"), Decimal("98") - TICK])
    ctx.tick_size = TICK

    levels = await ALGORITHM.entry_levels(ctx, Decimal("100"))
    result = await ALGORITHM.wait_for_entry(ctx, levels)

    assert result.entered and result.trade_type == TradeType.BUY.value
    assert result.price == Decimal("98"), (
        f"исполнение по {result.price}: лимитка исполняется на своей цене"
    )


async def test_cross_on_short_side():
    """То же правило сверху: касание не исполняет, пробитие исполняет."""
    cfg = config(side=SIDE_SHORT, max_wait_seconds="0.3")
    touched = context(cfg, prices=[Decimal("100"), Decimal("102")])
    touched.tick_size = TICK

    levels = await ALGORITHM.entry_levels(touched, Decimal("100"))
    assert (await ALGORITHM.wait_for_entry(touched, levels)).timed_out

    crossed = context(config(side=SIDE_SHORT), prices=[
        Decimal("100"), Decimal("102") + TICK,
    ])
    crossed.tick_size = TICK

    levels = await ALGORITHM.entry_levels(crossed, Decimal("100"))
    result = await ALGORITHM.wait_for_entry(crossed, levels)

    assert result.trade_type == TradeType.SELL.value
    assert result.price == Decimal("102")


# --- 2. Проскальзывание рыночного выхода -------------------------------

async def test_exit_carries_slippage():
    """Каждый выход несёт проскальзывание — и прибыльный, и убыточный."""
    cfg = config(exit_slippage_atr="0.2")
    ctx = context(cfg)
    await ALGORITHM.entry_levels(ctx, Decimal("100"))
    position = ALGORITHM.open_position(ctx, TradeType.BUY.value, Decimal("98"))

    expected = Decimal("98") * Decimal("1.0") * Decimal("0.2") / 100
    assert position.state["exit_slippage"] == expected

    take = position.state["take_profit_price"]
    stop = position.state["stop_loss_price"]

    won = await ALGORITHM.should_exit(ctx, position, take, held_seconds=1)
    lost = await ALGORITHM.should_exit(ctx, position, stop, held_seconds=1)
    timed = await ALGORITHM.should_exit(ctx, position, Decimal("98"), held_seconds=99)

    for decision in (won, lost, timed):
        assert decision.should_exit
        assert decision.slippage == expected, (
            f"выход {decision.reason} без проскальзывания: рыночный ордер "
            f"исполняется хуже наблюдаемой цены"
        )


async def test_slippage_is_at_least_one_tick():
    """Ниже шага цены проскальзывания не бывает, даже при нулевой доле."""
    ctx = context(config(exit_slippage_atr="0"))
    ctx.tick_size = TICK
    await ALGORITHM.entry_levels(ctx, Decimal("100"))

    position = ALGORITHM.open_position(ctx, TradeType.BUY.value, Decimal("98"))

    assert position.state["exit_slippage"] == TICK


def test_negative_slippage_is_rejected():
    try:
        ALGORITHM.parse_config({"exit_slippage_atr": "-0.1"})
    except ConfigError:
        return

    raise AssertionError("отрицательное проскальзывание — это подарок от биржи")


# --- 3. Тайм-стоп ------------------------------------------------------

async def test_time_stop_counts_from_fill():
    """Отсчёт идёт от входа: ожидание заявки в него не входит.

    Проверяется в связке с симулятором: тайм-стоп в 10 секунд не должен
    срабатывать оттого, что заявка провисела дольше.
    """
    import inspect
    import app.bots.demo_test_bot as m

    source = inspect.getsource(m.StartTestBotsCommand._simulate_bot)
    opened = source.index("algorithm.open_position(")
    started = source.index("just30sec_start_time = time.time()", opened)
    holding = source.index("while not stop_event.is_set():", started)

    assert opened < started < holding, (
        "отсчёт удержания начинается не между открытием позиции и циклом "
        "выхода: у стратегии с лимитным входом тайм-стоп истекал бы ещё "
        "до сделки"
    )


async def test_price_beats_the_clock():
    """Цель и таймер на одном тике: закрывает движение, а не часы."""
    cfg = config(time_stop_seconds="10")
    ctx = context(cfg)
    await ALGORITHM.entry_levels(ctx, Decimal("100"))
    position = ALGORITHM.open_position(ctx, TradeType.BUY.value, Decimal("98"))

    take = position.state["take_profit_price"]
    decision = await ALGORITHM.should_exit(ctx, position, take, held_seconds=10)

    assert decision.reason == StopReasonEvent.STOP_WON.value, (
        "позиция, дошедшая до цели, не должна сливаться по таймеру"
    )


async def test_stop_beats_the_clock_too():
    """И убыток фиксируется как убыток, а не как выход по времени."""
    ctx = context(config(time_stop_seconds="10"))
    await ALGORITHM.entry_levels(ctx, Decimal("100"))
    position = ALGORITHM.open_position(ctx, TradeType.BUY.value, Decimal("98"))

    stop = position.state["stop_loss_price"]
    decision = await ALGORITHM.should_exit(ctx, position, stop, held_seconds=10)

    assert decision.reason == StopReasonEvent.STOP_LOOSED.value


async def test_time_stop_fires_when_nothing_happened():
    ctx = context(config(time_stop_seconds="10"))
    await ALGORITHM.entry_levels(ctx, Decimal("100"))
    position = ALGORITHM.open_position(ctx, TradeType.BUY.value, Decimal("98"))

    before = await ALGORITHM.should_exit(
        ctx, position, Decimal("98.01"), held_seconds=9.99
    )
    after = await ALGORITHM.should_exit(
        ctx, position, Decimal("98.01"), held_seconds=10
    )

    assert not before.should_exit
    assert after.reason == StopReasonEvent.STOP_LONG_LOSE.value


# --- 4. ATR: только прошлое и без аномалий -----------------------------

def test_atr_uses_closed_candles_only():
    """Незакрытая свеча в `candles:{SYMBOL}` не попадает.

    Проверяются оба пути записи: REST отбрасывает последнюю свечу
    страницы, поток пишет только закрытые (`x=true`).
    """
    import inspect
    from app.scripts import watch_binance_candles as w

    rest = inspect.getsource(w.fetch_rest_candles)
    assert "klines[:-1]" in rest, (
        "REST-путь не отбрасывает текущую свечу: ATR считался бы по свече, "
        "внутри которой идёт тот самый вынос"
    )

    stream = inspect.getsource(w.run_websocket_listener)
    assert "is_closed" in stream and "if is_closed:" in stream


async def test_zero_and_dead_atr_block_entry():
    """Нулевой ATR — заявка встала бы вплотную к спреду."""
    for value in (Decimal("0"), Decimal("0.001"), None):
        ctx = context(config(min_atr_percent="0.05"), atr_percent=value)
        levels = await ALGORITHM.entry_levels(ctx, Decimal("100"))

        assert levels.buy == levels.sell, f"ATR {value} должен блокировать вход"


async def test_absurd_atr_blocks_entry():
    """ATR выше потолка — это сбой данных, а не разгон волатильности."""
    ctx = context(config(max_atr_percent="10"), atr_percent=Decimal("50"))

    levels = await ALGORITHM.entry_levels(ctx, Decimal("100"))

    assert levels.buy == levels.sell


async def test_atr_spike_against_own_median():
    """Скачок относительно собственной медианы тоже блокирует вход.

    Абсолютного потолка мало: на паре с обычным ATR 0.1% значение 2% —
    это разрыв в истории, хотя в потолок 10% оно укладывается.
    """
    cfg = config(atr_spike_guard="5")

    calm = provider_context(cfg, current=Decimal("0.4"), median=Decimal("0.1"))
    spiked = provider_context(cfg, current=Decimal("2.0"), median=Decimal("0.1"))

    calm_levels = await ALGORITHM.entry_levels(calm, Decimal("100"))
    spiked_levels = await ALGORITHM.entry_levels(spiked, Decimal("100"))

    assert calm_levels.buy != calm_levels.sell, (
        "разгон в четыре медианы — рабочая ситуация, вход разрешён"
    )
    assert spiked_levels.buy == spiked_levels.sell, (
        "двадцать медиан — это выброс, вход должен быть заблокирован"
    )


def provider_context(cfg, current: Decimal, median: Decimal):
    """Контекст, где ATR и его медиана заданы явно, в процентах."""
    ctx = context(cfg, atr_percent=current)
    price = Decimal("100")

    async def get_atr_percent(symbol, period):
        return current

    async def get_atr(symbol, period):
        # Абсолютный ATR той же пары: процент от цены 100.
        return current * price / 100

    async def get_atr_median(symbol, period):
        return median * price / 100

    ctx.candle_provider = SimpleNamespace(
        get_atr_percent=get_atr_percent,
        get_atr=get_atr,
        get_atr_median=get_atr_median,
    )

    return ctx


# --- 5. Изоляция сработавшего выхода -----------------------------------

async def test_exit_is_decided_once():
    """Сделка закрывается одним решением, а не копится триггерами.

    Алгоритм не хранит состояния между вызовами: решение принимается по
    переданной цене. Проверяется, что после выхода по цели повторный
    опрос той же позиции не выдаёт другой причины, будто позиция всё
    ещё жива и успела задеть стоп.
    """
    ctx = context(config(time_stop_seconds="10"))
    await ALGORITHM.entry_levels(ctx, Decimal("100"))
    position = ALGORITHM.open_position(ctx, TradeType.BUY.value, Decimal("98"))

    take = position.state["take_profit_price"]
    first = await ALGORITHM.should_exit(ctx, position, take, held_seconds=1)
    second = await ALGORITHM.should_exit(ctx, position, take, held_seconds=1)

    assert first.reason == second.reason == StopReasonEvent.STOP_WON.value
    assert position.state["take_profit_price"] == take, (
        "решение о выходе не должно менять уровни позиции"
    )


async def test_simulator_writes_single_trade():
    """Один круг — одна сделка, даже если после выхода цена идёт дальше."""
    from tests import test_strategy_1_simulation as sim

    redis = await sim.run_bot()

    assert len(redis.pushed) == 1, (
        f"сделок записано {len(redis.pushed)}: сработавший выход должен "
        f"закрывать позицию один раз"
    )


def main():
    asyncio.run(test_touch_does_not_fill())
    asyncio.run(test_cross_by_one_tick_fills())
    asyncio.run(test_cross_on_short_side())
    asyncio.run(test_exit_carries_slippage())
    asyncio.run(test_slippage_is_at_least_one_tick())
    test_negative_slippage_is_rejected()
    asyncio.run(test_time_stop_counts_from_fill())
    asyncio.run(test_price_beats_the_clock())
    asyncio.run(test_stop_beats_the_clock_too())
    asyncio.run(test_time_stop_fires_when_nothing_happened())
    test_atr_uses_closed_candles_only()
    asyncio.run(test_zero_and_dead_atr_block_entry())
    asyncio.run(test_absurd_atr_blocks_entry())
    asyncio.run(test_atr_spike_against_own_median())
    asyncio.run(test_exit_is_decided_once())
    asyncio.run(test_simulator_writes_single_trade())
    print("strategy_1 execution: ок")


if __name__ == "__main__":
    main()
