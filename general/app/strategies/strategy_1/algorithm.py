"""Стратегия 1: лимитка на отступе, заработок на возврате к средней.

Отличие от [стратегии 0](../strategy_0/algorithm.py) — в моменте входа.
Там вход по факту состоявшегося движения, здесь заявка стоит заранее:
цену выносит за пределы справедливого диапазона, заявка исполняется на
этом выносе, и заработок приходит с возврата к средней.

Отступ считается в ATR пары, а не в процентах и не в тиках. Причина та
же, по которой стратегия 0 считает в процентах, только сильнее: 0.5% на
паре с ATR 0.15% — вынос раз в сутки, а на паре с ATR 2.6% — обычная
минута. В ATR один и тот же множитель означает одно и то же на любой
паре, и подобранное на одной значение имеет смысл переносить на другие.

Сетка из нескольких уровней здесь **не реализована**: это один уровень,
одна позиция, одна сделка за круг — ровно то, что умеет симулятор.
Усреднение, средняя цена позиции и общий стоп за сеткой будут отдельным
шагом, и только если этот покажет, что возврат вообще отбивает комиссию.

Вход лимитный, поэтому комиссия открытия — maker (`entry_is_maker`).
Выход по рынку, там taker. На целях в десятые доли процента разница
между ставками — это заметная часть результата, и считать обе стороны
по taker значило бы занижать стратегию.
"""
import asyncio
import time

from dataclasses import dataclass
from decimal import Decimal, DecimalException
from datetime import datetime, timezone

from app.db.models import TestOrder
from app.enums.event_type import StopReasonEvent
from app.enums.trade_type import TradeType
from app.strategies.base import (
    ConfigError,
    EntryLevels,
    EntryResult,
    ExitDecision,
    MarketContext,
    Position,
)

UTC = timezone.utc

STRATEGY_1 = "strategy_1"
STRATEGY_1_ALGORITHM_VERSION = "strategy_1-1"

# Такт опроса цены — тот же, что у остальных стратегий: чаще нет смысла,
# частота обновления цен в Redis здесь потолок.
POLL_SECONDS = 0.1

# Как часто перепроверять ATR, пока заявка ждёт исполнения. Свеча
# минутная, кэш обновляется раз в пять секунд — чаще незачем.
ATR_REFRESH_SECONDS = 5.0

SIDE_BOTH = "both"
SIDE_LONG = "long"
SIDE_SHORT = "short"
SIDES = (SIDE_BOTH, SIDE_LONG, SIDE_SHORT)


@dataclass
class Strategy1Config:
    """Правила входа на выносе и выхода по возврату."""

    # Период ATR. Перебирается вместе с остальным: на минутных данных
    # «правильного» периода не существует.
    atr_period: int = 14
    # Отступ заявки от цены, в ATR.
    entry_offset_atr: Decimal = Decimal("2.5")
    # Цель — доля пройденного отступа. 0.4 означает «вернуть 40% того,
    # на сколько цену вынесло», то есть половину пути к средней.
    take_profit_fraction: Decimal = Decimal("0.4")
    # Стоп от цены входа, в ATR. Считается от входа, а не от исходной
    # цены: позиция уже открыта, и рискует она именно отсюда.
    stop_loss_atr: Decimal = Decimal("2.5")
    # Сколько ждать возврата, прежде чем выйти по рынку.
    time_stop_seconds: float = 15.0
    # Сколько ждать исполнения заявки, прежде чем пересчитать отступ.
    max_wait_seconds: float = 300.0
    # Стороны: both / long / short. Отдельный параметр, а не фильтр
    # тренда: так видно, работает ли стратегия на одну сторону, прежде
    # чем строить определение тренда.
    side: str = SIDE_BOTH
    # Ниже этой волатильности не входим: цель в 0.4 отступа от почти
    # нулевого ATR не покроет и комиссии.
    min_atr_percent: Decimal = Decimal("0.05")
    # Выше — тоже не входим. Такой ATR на минутках означает не «пара
    # разогналась», а сбой данных: разрыв в истории, ошибочная печать,
    # пустая свеча после остановки торгов.
    max_atr_percent: Decimal = Decimal("10")
    # Во сколько раз текущий ATR может превышать свою же медиану.
    # Абсолютного порога мало: на паре с обычным ATR 0.1% значение 2% —
    # это аномалия, а на паре с обычными 2% — рабочий день.
    atr_spike_guard: Decimal = Decimal("5")
    # Проскальзывание выхода, в долях ATR. Выход рыночный, и на паре,
    # которую только что вынесло, стакан разрежен: закрытие исполняется
    # хуже той цены, по которой сработал триггер. Ноль означал бы, что
    # закрытие всегда идеально, — это завысило бы результат стратегии.
    exit_slippage_atr: Decimal = Decimal("0.1")


FIELD_TYPES = {
    "atr_period": int,
    "entry_offset_atr": Decimal,
    "take_profit_fraction": Decimal,
    "stop_loss_atr": Decimal,
    "time_stop_seconds": float,
    "max_wait_seconds": float,
    "side": str,
    "min_atr_percent": Decimal,
    "max_atr_percent": Decimal,
    "atr_spike_guard": Decimal,
    "exit_slippage_atr": Decimal,
}


class Strategy1Algorithm:
    key = STRATEGY_1
    version = STRATEGY_1_ALGORITHM_VERSION
    config_schema_version = 1
    # Вход стоит лимитной заявкой, комиссия открытия — maker.
    entry_is_maker = True

    def parse_config(self, raw) -> Strategy1Config:
        raw = dict(raw or {})
        version = raw.pop("schema_version", self.config_schema_version)

        if version != self.config_schema_version:
            raise ConfigError(
                f"версия схемы настроек {version} не совпадает с "
                f"{self.config_schema_version}"
            )

        extra = sorted(set(raw) - set(FIELD_TYPES))

        if extra:
            raise ConfigError(
                f"неизвестные настройки: {', '.join(extra)}. Известны: "
                f"{', '.join(sorted(FIELD_TYPES))}"
            )

        values = {}

        for field, cast in FIELD_TYPES.items():
            if field not in raw:
                continue

            try:
                values[field] = cast(str(raw[field]))
            except (TypeError, ValueError, DecimalException) as error:
                raise ConfigError(
                    f"настройка {field}={raw[field]!r} не разбирается: {error}"
                ) from error

        config = Strategy1Config(**values)

        if config.side not in SIDES:
            raise ConfigError(
                f"side={config.side!r}: ожидается одно из {', '.join(SIDES)}"
            )

        positives = (
            ("atr_period", config.atr_period),
            ("entry_offset_atr", config.entry_offset_atr),
            ("take_profit_fraction", config.take_profit_fraction),
            ("stop_loss_atr", config.stop_loss_atr),
            ("time_stop_seconds", config.time_stop_seconds),
            ("max_wait_seconds", config.max_wait_seconds),
            ("min_atr_percent", config.min_atr_percent),
            ("max_atr_percent", config.max_atr_percent),
            ("atr_spike_guard", config.atr_spike_guard),
        )

        for name, value in positives:
            if value <= 0:
                raise ConfigError(f"настройка {name} должна быть больше нуля")

        if config.exit_slippage_atr < 0:
            raise ConfigError("exit_slippage_atr не может быть отрицательной")

        if config.max_atr_percent <= config.min_atr_percent:
            raise ConfigError(
                "max_atr_percent должна быть больше min_atr_percent: иначе "
                "торговое окно по волатильности пусто"
            )

        if config.take_profit_fraction >= 1:
            raise ConfigError(
                "take_profit_fraction должна быть меньше единицы: цель — "
                "часть пройденного отступа, а не весь он целиком"
            )

        return config

    async def prepare(self, context: MarketContext):
        # Всё считается от ATR в момент выставления заявки, пересчитывать
        # заранее нечего.
        return context.bot_config

    async def _atr_percent(self, context: MarketContext) -> Decimal | None:
        """ATR пары в процентах от цены. None — входить не на чем.

        Берётся только по закрытым свечам: в `candles:{SYMBOL}` текущая
        минута не попадает (`fetch_rest_candles` отбрасывает последнюю
        свечу, поток пишет только `x=true`). Иначе отступ считался бы по
        свече, внутри которой прямо сейчас идёт тот самый вынос, — и
        стратегия подглядывала бы в собственное будущее.

        Аномалии отбраковываются здесь же. Ноль и почти ноль означают,
        что сделок не было, и заявка встала бы вплотную к спреду;
        значение в разы выше собственной медианы — что в истории разрыв
        или ошибочная печать, и заявка ушла бы на нереальную глубину.
        """
        provider = getattr(context, "candle_provider", None)

        if provider is None:
            return None

        config = context.strategy_config
        value = await provider.get_atr_percent(context.symbol, config.atr_period)

        if value is None:
            return None

        if value < config.min_atr_percent or value > config.max_atr_percent:
            return None

        median = await self._atr_median_percent(context, provider)

        if median and value > median * config.atr_spike_guard:
            return None

        return value

    @staticmethod
    async def _atr_median_percent(context: MarketContext, provider):
        """Медиана ATR в процентах. None — сравнивать не с чем.

        Медиана приходит в абсолюте, а порог сравнивается с процентом,
        поэтому переводить надо здесь: делить на ту же цену, по которой
        посчитан текущий ATR в процентах, нельзя — её тут нет, поэтому
        берём отношение через абсолютный ATR той же пары.
        """
        getter = getattr(provider, "get_atr_median", None)

        if getter is None:
            return None

        config = context.strategy_config
        median = await getter(context.symbol, config.atr_period)
        current = await provider.get_atr(context.symbol, config.atr_period)
        current_percent = await provider.get_atr_percent(
            context.symbol, config.atr_period
        )

        if not median or not current or not current_percent:
            return None

        # Медиана в процентах от той же цены: median / current — это
        # отношение, current_percent — текущий в процентах.
        return current_percent * median / current

    async def entry_levels(
        self, context: MarketContext, initial_price
    ) -> EntryLevels:
        """Заявки по обе стороны от цены, на отступе в `entry_offset_atr`.

        Уровни здесь настоящие, а не справочные: именно их и ждёт
        `wait_for_entry`. Если ATR посчитать не на чем, уровни выходят
        нулевыми — ожидание всё равно пересчитает их, когда свечи
        появятся, а общий цикл получит осмысленный таймаут.
        """
        config = context.strategy_config
        price = Decimal(str(initial_price))
        atr_percent = await self._atr_percent(context)

        # ATR кладётся в состояние круга: по нему же считаются цель и
        # стоп при открытии позиции, и пересчитывать его там заново
        # значило бы взять другое число — кэш успевает обновиться.
        context.strategy_state["atr_percent"] = atr_percent

        if atr_percent is None:
            return EntryLevels(
                buy=price, sell=price,
                timeout_seconds=int(config.max_wait_seconds),
            )

        offset = price * atr_percent * config.entry_offset_atr / 100

        return EntryLevels(
            buy=price - offset,
            sell=price + offset,
            timeout_seconds=int(config.max_wait_seconds),
        )

    async def wait_for_entry(
        self, context: MarketContext, levels: EntryLevels
    ) -> EntryResult:
        """Ждёт, пока вынос дотянется до заявки.

        Уровни пересчитываются на ходу: пока заявка висит, ATR меняется,
        и держать отступ, посчитанный пять минут назад, значит стоять не
        там, где задумано. Точка отсчёта — цена на момент пересчёта, как
        и было бы у живой сетки, которая переставляется за рынком.

        **Заявка исполняется на пробитии, а не на касании.** Цена,
        дошедшая ровно до лимитки, ничего не гарантирует: на этом уровне
        стоит очередь, и наша заявка в ней последняя. Исполнение — это
        либо проход цены насквозь (минимум на тик), либо агрессор,
        выбравший весь объём уровня, чего в тиковой ленте цен не видно.
        Поэтому засчитывается только строгое пробитие: `price` должна
        уйти за уровень хотя бы на один тик. Касание, засчитанное за
        исполнение, дало бы стратегии сделки, которых на бирже не было
        бы, — и тем больше, чем тоньше пара.
        """
        config = context.strategy_config
        deadline = time.monotonic() + config.max_wait_seconds
        buy_level = levels.buy
        sell_level = levels.sell
        recalculated_at = time.monotonic()

        while time.monotonic() < deadline:
            price = await context.before_entry(
                context.price_provider.get_price(symbol=context.symbol)
            )

            if price is None:
                await asyncio.sleep(POLL_SECONDS)
                continue

            price = Decimal(str(price))
            now = time.monotonic()

            if now - recalculated_at >= ATR_REFRESH_SECONDS or not buy_level:
                fresh = await self.entry_levels(context, price)
                buy_level, sell_level = fresh.buy, fresh.sell
                recalculated_at = now

            if buy_level == sell_level:
                # ATR ещё неизвестен: входить не от чего.
                await asyncio.sleep(POLL_SECONDS)
                continue

            tick = Decimal(str(context.tick_size or 0))

            if config.side != SIDE_SHORT and price <= buy_level - tick:
                return EntryResult(
                    trade_type=TradeType.BUY.value,
                    # Исполнение по цене заявки, а не по текущей: лимитка
                    # стоит на своём уровне, и проскочившая цена достаётся
                    # тому, кто был в очереди раньше.
                    price=buy_level,
                )

            if config.side != SIDE_LONG and price >= sell_level + tick:
                return EntryResult(
                    trade_type=TradeType.SELL.value,
                    price=sell_level,
                )

            await asyncio.sleep(POLL_SECONDS)

        return EntryResult(timed_out=True)

    async def entry_still_valid(
        self, context: MarketContext, entry: EntryResult, current_price
    ) -> bool:
        """Заявка исполнилась — исполнение не отменяется движением цены.

        Общая для рыночных стратегий проверка «цена та же» здесь не
        годится и молча запрещала бы копиботам сделки этой стратегии
        вовсе. Цена входа у лимитки — цена заявки, а срабатывает она
        как раз тогда, когда рынок эту цену **пробил**: расхождение с
        текущей ценой тут не признак устаревшего сигнала, а условие
        самого исполнения.

        Что сигнал не устарел, обеспечено раньше: заявка считается
        исполненной в момент пробоя, а он уже случился. Выбор донора к
        этому моменту перепроверен отдельно (`DonorGuard.check`), и
        отменяет вход именно он, а не цена.
        """
        return True

    def open_position(
        self, context: MarketContext, trade_type, entry_price
    ) -> Position:
        config = context.strategy_config
        entry_price = Decimal(str(entry_price))

        # ATR тот же, по которому посчитан отступ входа: он положен в
        # состояние круга в `entry_levels`.
        atr_percent = Decimal(str(context.strategy_state.get("atr_percent") or 0))

        offset = entry_price * atr_percent * config.entry_offset_atr / 100
        take = offset * config.take_profit_fraction
        stop = entry_price * atr_percent * config.stop_loss_atr / 100

        if trade_type == TradeType.BUY.value:
            take_profit_price = entry_price + take
            stop_loss_price = entry_price - stop
        else:
            take_profit_price = entry_price - take
            stop_loss_price = entry_price + stop

        # Вход лимитный — ставка maker. Её может не быть (не засеяна);
        # тогда считаем по общей, как остальные стратегии.
        commission = context.maker_commission_rate or context.commission_rate

        order = TestOrder(
            stop_loss_price=stop_loss_price,
            # Тиковых уровней у стратегии нет: всё считается от ATR.
            start_updown_ticks=0,
            stop_success_ticks=0,
            stop_loss_ticks=0,
            open_price=entry_price,
            open_time=datetime.now(UTC),
            open_fee=Decimal(context.bot_config.balance) * Decimal(commission),
            order_type=trade_type,
        )

        # Проскальзывание выхода считается здесь же, от того же ATR:
        # в цикле удержания его пересчёт стоил бы обращения к кэшу на
        # каждом тике каждому боту.
        slippage = entry_price * atr_percent * config.exit_slippage_atr / 100
        tick = Decimal(str(context.tick_size or 0))

        # Меньше тика проскальзывания не бывает: цена дискретна, и
        # рыночный ордер забирает как минимум следующий уровень.
        if tick and slippage < tick:
            slippage = tick

        return Position(
            order=order,
            state={
                "take_profit_price": take_profit_price,
                "stop_loss_price": stop_loss_price,
                "exit_slippage": slippage,
            },
        )

    async def should_exit(
        self,
        context: MarketContext,
        position: Position,
        updated_price,
        held_seconds: float,
    ) -> ExitDecision:
        config = context.strategy_config
        order = position.order
        price = Decimal(str(updated_price))

        take_profit_price = position.state["take_profit_price"]
        stop_loss_price = position.state["stop_loss_price"]
        slippage = position.state.get("exit_slippage") or Decimal(0)

        if order.order_type == TradeType.BUY.value:
            hit_take = price >= take_profit_price
            hit_stop = price <= stop_loss_price
        else:
            hit_take = price <= take_profit_price
            hit_stop = price >= stop_loss_price

        # Стоп раньше тейка: если за один тик цена прошла оба уровня,
        # порядок внутри тика неизвестен, и засчитывается худший исход.
        if hit_stop:
            return ExitDecision(
                should_exit=True,
                reason=StopReasonEvent.STOP_LOOSED.value,
                slippage=slippage,
            )

        # Тейк проверяется раньше таймера намеренно: если на том же тике
        # цена дошла до цели и истёк тайм-стоп, сделку закрывает
        # состоявшееся движение, а не часы. Обратный порядок сливал бы по
        # таймеру уже отработавшую позицию.
        if hit_take:
            return ExitDecision(
                should_exit=True,
                reason=StopReasonEvent.STOP_WON.value,
                slippage=slippage,
            )

        # Возврата не случилось. Ждать дальше — значит держать позицию,
        # открытую ради быстрого отскока, в надежде на тренд.
        if held_seconds >= config.time_stop_seconds:
            return ExitDecision(
                should_exit=True,
                reason=StopReasonEvent.STOP_LONG_LOSE.value,
                slippage=slippage,
            )

        return ExitDecision(should_exit=False)
