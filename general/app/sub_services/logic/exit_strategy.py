from app.enums.trade_type import TradeType
from app.enums.event_type import StopReasonEvent


class ExitStrategy:

    @staticmethod
    async def check_exit_ticks_conditions(
        bot_config,
        price_calculator,
        tick_size,
        order,
        close_not_lose_price,
        take_profit_price,
        updated_price,
        price_from_previous_step,
    ):
        new_tk_p = price_calculator.calculate_take_profit_price(
            stop_success_ticks=bot_config.stop_success_ticks,
            tick_size=tick_size,
            open_price=updated_price,
            trade_type=order.order_type,
        )
        new_sl_p = price_calculator.calculate_stop_lose_price(
            stop_loss_ticks=bot_config.stop_loss_ticks,
            tick_size=tick_size,
            trade_type=order.order_type,
            open_price=updated_price,
        )

        if order.order_type == TradeType.BUY:
            if (
                price_from_previous_step < updated_price
                and new_tk_p > take_profit_price
            ):
                take_profit_price = new_tk_p
            elif new_sl_p > order.stop_loss_price:
                order.stop_loss_price = new_sl_p

            if updated_price <= order.stop_loss_price:
                order.stop_reason_event = StopReasonEvent.STOP_LOOSED.value
                return True, take_profit_price

            if close_not_lose_price < updated_price <= take_profit_price:
                order.stop_reason_event = StopReasonEvent.STOP_WON.value
                return True, take_profit_price
        else:
            if (
                price_from_previous_step > updated_price
                and new_tk_p < take_profit_price
            ):
                take_profit_price = new_tk_p
            elif new_sl_p < order.stop_loss_price:
                order.stop_loss_price = new_sl_p

            if updated_price >= order.stop_loss_price:
                order.stop_reason_event = StopReasonEvent.STOP_LOOSED.value
                return True, take_profit_price

            if take_profit_price <= updated_price < close_not_lose_price:
                order.stop_reason_event = StopReasonEvent.STOP_WON.value
                return True, take_profit_price

        return False, take_profit_price

    @staticmethod
    async def check_exit_ma_conditions(
        binance_bot,
        symbol,
        order,
        updated_price,
    ):
        ma25 = await binance_bot.get_ma(symbol=symbol, ma_number=25, current_price=updated_price)

        if order.order_type == TradeType.BUY:
            if ma25 and updated_price < ma25:
                return True
        else:
            if ma25 and updated_price > ma25:
                return True

        return False