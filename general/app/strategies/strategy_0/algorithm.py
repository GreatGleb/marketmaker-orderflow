"""Стратегия 0: вход на простреле.

Прострел — резкое движение цены за короткое окно. Что считать резким, за
какое окно смотреть и куда входить — не зашито: правила задаются
настройками бота (`test_bots.strategy_config`), потому что именно их и
предстоит перебирать. Значения по умолчанию — не рекомендация, а
отправная точка: они выбраны так, чтобы стратегия вообще срабатывала на
живых данных, и подлежат проверке экспериментом.

Отличия от `legacy`, которые видно сразу:

* уровни входа не выставляются заранее. Вход происходит по факту
  движения, а не по пробою заранее посчитанной цены, поэтому
  `wait_for_entry` сам ведёт окно цен и следит за ним;
* выход считается в процентах от цены входа, а не в тиках: прострелы
  разной величины на парах с разным тиком иначе несравнимы;
* правила 30 секунд здесь нет. Вместо него — `max_hold_seconds`,
  честный параметр, а не общая для всех константа.

Состояние детектора живёт в памяти бота на время ожидания: окно цен —
это deque, а не запрос в базу. Сетевые запросы на каждом тике сделали бы
стратегию непригодной для секундных движений.
"""
import asyncio
import time

from collections import deque
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

STRATEGY_0 = "strategy_0"
STRATEGY_0_ALGORITHM_VERSION = "strategy_0-1"

# Как часто опрашивается цена при слежении за прострелом. Тот же такт,
# что и в цикле удержания legacy: чаще смысла нет — цены в Redis
# обновляет питатель, и его частота здесь потолок.
POLL_SECONDS = 0.1

ENTRY_CONTINUATION = "continuation"
ENTRY_REVERSAL = "reversal"
ENTRY_MODES = (ENTRY_CONTINUATION, ENTRY_REVERSAL)


@dataclass
class Strategy0Config:
    """Правила прострела. Все проценты — от цены, а не от баланса."""

    window_seconds: float = 5.0
    move_percent: Decimal = Decimal("0.5")
    entry_mode: str = ENTRY_CONTINUATION
    take_profit_percent: Decimal = Decimal("0.3")
    stop_loss_percent: Decimal = Decimal("0.3")
    max_hold_seconds: float = 60.0
    max_wait_seconds: float = 300.0


FIELD_TYPES = {
    "window_seconds": float,
    "move_percent": Decimal,
    "entry_mode": str,
    "take_profit_percent": Decimal,
    "stop_loss_percent": Decimal,
    "max_hold_seconds": float,
    "max_wait_seconds": float,
}


class Strategy0Algorithm:
    key = STRATEGY_0
    version = STRATEGY_0_ALGORITHM_VERSION
    config_schema_version = 1

    def parse_config(self, raw) -> Strategy0Config:
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

        config = Strategy0Config(**values)

        if config.entry_mode not in ENTRY_MODES:
            raise ConfigError(
                f"entry_mode={config.entry_mode!r}: ожидается одно из "
                f"{', '.join(ENTRY_MODES)}"
            )

        positives = (
            ("window_seconds", config.window_seconds),
            ("move_percent", config.move_percent),
            ("take_profit_percent", config.take_profit_percent),
            ("stop_loss_percent", config.stop_loss_percent),
            ("max_hold_seconds", config.max_hold_seconds),
            ("max_wait_seconds", config.max_wait_seconds),
        )

        for name, value in positives:
            if value <= 0:
                raise ConfigError(f"настройка {name} должна быть больше нуля")

        return config

    async def prepare(self, context: MarketContext):
        # Пересчитывать нечего: все параметры стратегии заданы в
        # процентах и не зависят от тика пары.
        return context.bot_config

    async def entry_levels(
        self, context: MarketContext, initial_price
    ) -> EntryLevels:
        """Границы прострела от текущей цены.

        Уровни здесь справочные: вход решает `wait_for_entry` по факту
        движения. Возвращаются они, чтобы общий цикл мог показать их в
        логах и проверить на границы пары — так же, как у legacy.
        """
        config = context.strategy_config
        move = initial_price * config.move_percent / 100

        return EntryLevels(
            buy=initial_price + move,
            sell=initial_price - move,
            timeout_seconds=int(config.max_wait_seconds),
        )

    async def wait_for_entry(
        self, context: MarketContext, levels: EntryLevels
    ) -> EntryResult:
        """Ждёт прострела и входит по направлению из настроек.

        Окно скользящее: цена сравнивается с самой старой в пределах
        `window_seconds`. Так движение «набранное за пять секунд» не
        путается с медленным трендом той же величины за час.
        """
        config = context.strategy_config
        deadline = time.monotonic() + config.max_wait_seconds
        window = deque()

        while time.monotonic() < deadline:
            price = await context.before_entry(
                context.price_provider.get_price(symbol=context.symbol)
            )

            now = time.monotonic()

            if price is not None:
                window.append((now, Decimal(str(price))))

            while window and now - window[0][0] > config.window_seconds:
                window.popleft()

            if len(window) >= 2:
                oldest = window[0][1]
                current = window[-1][1]

                if oldest > 0:
                    change = (current - oldest) / oldest * 100

                    if abs(change) >= config.move_percent:
                        return EntryResult(
                            trade_type=self._direction(config, change),
                            price=current,
                        )

            await asyncio.sleep(POLL_SECONDS)

        return EntryResult(timed_out=True)

    @staticmethod
    def _direction(config: Strategy0Config, change: Decimal) -> str:
        """Куда входить при движении такого знака."""
        up = change > 0

        if config.entry_mode == ENTRY_REVERSAL:
            up = not up

        return TradeType.BUY.value if up else TradeType.SELL.value

    def open_position(
        self, context: MarketContext, trade_type, entry_price
    ) -> Position:
        config = context.strategy_config
        entry_price = Decimal(str(entry_price))

        take = entry_price * config.take_profit_percent / 100
        stop = entry_price * config.stop_loss_percent / 100

        if trade_type == TradeType.BUY.value:
            take_profit_price = entry_price + take
            stop_loss_price = entry_price - stop
        else:
            take_profit_price = entry_price - take
            stop_loss_price = entry_price + stop

        order = TestOrder(
            stop_loss_price=stop_loss_price,
            # Тиковые поля стратегия не использует: её уровни
            # процентные. Нули здесь означают «не задано» — так же, как
            # у MA-ботов legacy.
            start_updown_ticks=0,
            stop_success_ticks=0,
            stop_loss_ticks=0,
            open_price=entry_price,
            open_time=datetime.now(UTC),
            open_fee=(
                Decimal(context.bot_config.balance)
                * Decimal(context.commission_rate)
            ),
            order_type=trade_type,
        )

        return Position(
            order=order,
            state={
                "take_profit_price": take_profit_price,
                "stop_loss_price": stop_loss_price,
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

        if order.order_type == TradeType.BUY.value:
            hit_take = price >= take_profit_price
            hit_stop = price <= stop_loss_price
        else:
            hit_take = price <= take_profit_price
            hit_stop = price >= stop_loss_price

        # Стоп проверяется раньше тейка: если за один тик цена прошла
        # оба уровня, на бирже сработал бы тот, до которого она дошла
        # первым, а этого мы не знаем — считаем худший исход.
        if hit_stop:
            return ExitDecision(
                should_exit=True, reason=StopReasonEvent.STOP_LOOSED.value
            )

        if hit_take:
            return ExitDecision(
                should_exit=True, reason=StopReasonEvent.STOP_WON.value
            )

        if held_seconds >= config.max_hold_seconds:
            return ExitDecision(
                should_exit=True, reason=StopReasonEvent.STOP_LONG_LOSE.value
            )

        return ExitDecision(should_exit=False)
