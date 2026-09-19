"""Текущий алгоритм: вход по пробою уровня, выход по стопам, MA и 30 секундам.

Код перенесён из `StartTestBotsCommand._simulate_bot` как есть —
поведение не менялось, менялось только место. Это важно: статистика в
`test_orders` копится с августа, и любая «попутная» правка условий входа
или выхода сделала бы старые и новые сделки несравнимыми.

Что здесь живёт и почему именно здесь:

* `12 * 60 * 60` у MA-бота — предохранитель, а не время ожидания: MA-бот
  входит по пересечению средних, и `time_to_wait` к нему не применяется;
* правило `diff_ticks < 10 and held >= 30` — выход по времени удержания.
  Оно принадлежит этому алгоритму, а не симулятору: следующая стратегия
  его не наследует;
* пересчёт процентов в тики (`update_config_for_percentage`) тоже
  относится сюда — в процентах заданы параметры именно этого алгоритма.
"""
import asyncio

from datetime import datetime, timezone
from decimal import Decimal

from app.constants.strategy import LEGACY_ALGORITHM_VERSION, STRATEGY_LEGACY
from app.db.models import TestOrder
from app.enums.event_type import StopReasonEvent
from app.enums.trade_type import TradeType
from app.strategies.base import (
    EntryLevels,
    EntryResult,
    ExitDecision,
    MarketContext,
    Position,
)
from app.sub_services.logic.exit_strategy import ExitStrategy
from app.sub_services.logic.price_calculator import PriceCalculator

UTC = timezone.utc

# Предохранитель MA-бота: по нему цикл уходит на новую итерацию
# ожидания, а не закрывает попытку входа.
MA_ENTRY_TIMEOUT_SECONDS = 12 * 60 * 60

# Выход по времени удержания: если через столько секунд позиция не
# отошла от цены безубытка хотя бы на столько тиков, закрываем.
LONG_LOSE_AFTER_SECONDS = 30
LONG_LOSE_MIN_TICKS = 10


class LegacyAlgorithm:
    key = STRATEGY_LEGACY
    version = LEGACY_ALGORITHM_VERSION

    async def prepare(self, context: MarketContext):
        # Импорт внутри метода: воркер копиботов тянет за собой crud и
        # модели, а стратегии грузятся при старте симулятора.
        from app.workers.profitable_bot_updater import ProfitableBotUpdaterCommand

        return await context.before_entry(
            ProfitableBotUpdaterCommand.update_config_for_percentage(
                bot_config=context.bot_config,
                price_provider=context.price_provider,
                symbol=context.symbol,
                tick_size=context.tick_size,
            )
        )

    async def entry_levels(
        self, context: MarketContext, initial_price
    ) -> EntryLevels:
        config = context.bot_config
        offset = config.start_updown_ticks * context.tick_size

        if config.consider_ma_for_open_order:
            timeout = MA_ENTRY_TIMEOUT_SECONDS
        elif config.time_to_wait_for_entry_price_to_open_order_in_seconds:
            timeout = config.time_to_wait_for_entry_price_to_open_order_in_seconds
        else:
            # Ноль в конфиге означает «не задано»; секунда — прежнее
            # поведение по умолчанию.
            timeout = 1

        return EntryLevels(
            buy=initial_price + offset,
            sell=initial_price - offset,
            timeout_seconds=int(timeout),
        )

    async def wait_for_entry(
        self, context: MarketContext, levels: EntryLevels
    ) -> EntryResult:
        try:
            trade_type, entry_price = await context.before_entry(
                asyncio.wait_for(
                    context.price_watcher.wait_for_entry_price(
                        symbol=context.symbol,
                        entry_price_buy=levels.buy,
                        entry_price_sell=levels.sell,
                        binance_bot=context.binance_bot,
                        bot_config=context.bot_config,
                    ),
                    timeout=levels.timeout_seconds,
                )
            )
        except asyncio.TimeoutError:
            return EntryResult(timed_out=True)

        return EntryResult(trade_type=trade_type, price=entry_price)

    def open_position(
        self, context: MarketContext, trade_type, entry_price
    ) -> Position:
        config = context.bot_config
        commission_rate = context.commission_rate

        close_not_lose_price = PriceCalculator.calculate_close_not_lose_price(
            open_price=entry_price,
            trade_type=trade_type,
            commission_open=commission_rate,
            commission_close=commission_rate,
        )

        state = {
            "close_not_lose_price": close_not_lose_price,
            "price_from_previous_step": entry_price,
            "peak_favorable_price": entry_price,
            "take_profit_price": None,
        }

        if not config.consider_ma_for_close_order:
            stop_loss_price = PriceCalculator.calculate_stop_lose_price(
                stop_loss_ticks=config.stop_loss_ticks,
                tick_size=context.tick_size,
                open_price=entry_price,
                trade_type=trade_type,
            )

            if config.use_trailing_stop:
                take_profit_price = (
                    PriceCalculator.calculate_trailing_take_profit_price(
                        peak_favorable_price=entry_price,
                        stop_success_ticks=config.stop_success_ticks,
                        tick_size=context.tick_size,
                        trade_type=trade_type,
                    )
                )
            else:
                take_profit_price = PriceCalculator.calculate_take_profit_price(
                    stop_success_ticks=config.stop_success_ticks,
                    tick_size=context.tick_size,
                    open_price=entry_price,
                    trade_type=trade_type,
                    commission_open=commission_rate,
                    commission_close=commission_rate,
                )

            state["take_profit_price"] = take_profit_price

            order = TestOrder(
                stop_loss_price=Decimal(stop_loss_price),
                start_updown_ticks=config.start_updown_ticks,
                stop_success_ticks=config.stop_success_ticks,
                stop_loss_ticks=config.stop_loss_ticks,
                open_price=entry_price,
                open_time=datetime.now(UTC),
                open_fee=Decimal(config.balance) * Decimal(commission_rate),
                order_type=trade_type,
            )
        else:
            # У MA-бота уровней нет: выход считает только пересечение
            # средних, и нули здесь означают «не задано».
            order = TestOrder(
                stop_loss_price=0,
                start_updown_ticks=0,
                stop_success_ticks=0,
                stop_loss_ticks=0,
                open_price=entry_price,
                open_time=datetime.now(UTC),
                open_fee=Decimal(config.balance) * Decimal(commission_rate),
                order_type=trade_type,
            )

        return Position(order=order, state=state)

    async def should_exit(
        self,
        context: MarketContext,
        position: Position,
        updated_price,
        held_seconds: float,
    ) -> ExitDecision:
        config = context.bot_config
        order = position.order
        state = position.state
        close_not_lose_price = state["close_not_lose_price"]

        if config.consider_ma_for_close_order:
            should_exit = await ExitStrategy.check_exit_ma_conditions(
                binance_bot=context.binance_bot,
                bot_config=config,
                symbol=context.symbol,
                order_side=order.order_type,
                updated_price=updated_price,
                close_not_lose_price=close_not_lose_price,
            )
        elif config.use_trailing_stop:
            (
                should_exit,
                state["take_profit_price"],
                state["peak_favorable_price"],
            ) = await ExitStrategy.check_exit_conditions_trailing(
                price_calculator=PriceCalculator,
                tick_size=context.tick_size,
                order=order,
                close_not_lose_price=close_not_lose_price,
                take_profit_price=state["take_profit_price"],
                updated_price=updated_price,
                price_from_previous_step=state["price_from_previous_step"],
                peak_favorable_price=state["peak_favorable_price"],
            )
        else:
            should_exit = await ExitStrategy.check_exit_conditions(
                order=order,
                close_not_lose_price=close_not_lose_price,
                take_profit_price=state["take_profit_price"],
                updated_price=updated_price,
            )

        if order.order_type == TradeType.BUY:
            price_diff_from_cnl = updated_price - close_not_lose_price
        else:
            price_diff_from_cnl = close_not_lose_price - updated_price

        diff_ticks = price_diff_from_cnl / context.tick_size

        # Порядок важен: выход по времени удержания проверяется раньше
        # обычного и перекрывает его причиной stop-long-lose.
        if diff_ticks < LONG_LOSE_MIN_TICKS and held_seconds >= LONG_LOSE_AFTER_SECONDS:
            return ExitDecision(
                should_exit=True, reason=StopReasonEvent.STOP_LONG_LOSE.value
            )

        state["price_from_previous_step"] = updated_price

        return ExitDecision(should_exit=bool(should_exit))
