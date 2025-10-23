from datetime import datetime

from app.enums.trade_type import TradeType
from app.enums.event_type import StopReasonEvent


class ExitStrategy:

    @staticmethod
    async def check_exit_conditions_trailing(
        price_calculator,
        tick_size,
        order,
        close_not_lose_price,
        take_profit_price,
        updated_price,
        price_from_previous_step,
        peak_favorable_price
    ):
        if order.order_type == TradeType.BUY:
            if (
                price_from_previous_step < updated_price
                and updated_price > peak_favorable_price
            ):
                peak_favorable_price = price_calculator.get_peak_favorable_price(
                    current_peak_favorable_price=peak_favorable_price,
                    current_price=updated_price,
                    trade_type=order.order_type
                )

                take_profit_price = price_calculator.calculate_trailing_take_profit_price(
                    peak_favorable_price=peak_favorable_price,
                    stop_success_ticks=order.stop_success_ticks,
                    tick_size=tick_size,
                    trade_type=order.order_type
                )
            # elif new_sl_p > order.stop_loss_price:
            #     order.stop_loss_price = new_sl_p

            if updated_price <= order.stop_loss_price:
                order.stop_reason_event = StopReasonEvent.STOP_LOOSED.value
                return True, take_profit_price, peak_favorable_price

            if take_profit_price > close_not_lose_price and updated_price <= take_profit_price and updated_price > close_not_lose_price:
                order.stop_reason_event = StopReasonEvent.STOP_WON.value
                return True, take_profit_price, peak_favorable_price
        else:
            if (
                price_from_previous_step > updated_price
                and updated_price < peak_favorable_price
            ):
                peak_favorable_price = price_calculator.get_peak_favorable_price(
                    current_peak_favorable_price=peak_favorable_price,
                    current_price=updated_price,
                    trade_type=order.order_type
                )

                take_profit_price = price_calculator.calculate_trailing_take_profit_price(
                    peak_favorable_price=peak_favorable_price,
                    stop_success_ticks=order.stop_success_ticks,
                    tick_size=tick_size,
                    trade_type=order.order_type
                )
            # elif new_sl_p < order.stop_loss_price:
            #     order.stop_loss_price = new_sl_p

            if updated_price >= order.stop_loss_price:
                order.stop_reason_event = StopReasonEvent.STOP_LOOSED.value
                return True, take_profit_price, peak_favorable_price

            if take_profit_price < close_not_lose_price and updated_price >= take_profit_price and updated_price < close_not_lose_price:
                order.stop_reason_event = StopReasonEvent.STOP_WON.value
                return True, take_profit_price, peak_favorable_price

        return False, take_profit_price, peak_favorable_price

    @staticmethod
    async def check_exit_conditions(
        order,
        close_not_lose_price,
        take_profit_price,
        updated_price,
    ):
        if order.order_type == TradeType.BUY:
            if updated_price <= order.stop_loss_price:
                order.stop_reason_event = StopReasonEvent.STOP_LOOSED.value
                return True

            if close_not_lose_price < updated_price >= take_profit_price:
                order.stop_reason_event = StopReasonEvent.STOP_WON.value
                return True
        else:
            if updated_price >= order.stop_loss_price:
                order.stop_reason_event = StopReasonEvent.STOP_LOOSED.value
                return True

            if close_not_lose_price > updated_price <= take_profit_price:
                order.stop_reason_event = StopReasonEvent.STOP_WON.value
                return True

        return False

    @staticmethod
    async def check_exit_ma_conditions(
        binance_bot,
        bot_config,
        symbol,
        order_side,
        updated_price,
        close_not_lose_price,
    ):
        if int(bot_config.ma_number_of_candles_for_open_order) < int(bot_config.ma_number_of_candles_for_close_order):
            less_ma_number = int(bot_config.ma_number_of_candles_for_open_order)
            more_ma_number = int(bot_config.ma_number_of_candles_for_close_order)
        else:
            less_ma_number = int(bot_config.ma_number_of_candles_for_close_order)
            more_ma_number = int(bot_config.ma_number_of_candles_for_open_order)

        ma_data = await binance_bot.get_prev_minutes_ma(
            symbol=symbol,
            less_ma_number=less_ma_number,
            more_ma_number=more_ma_number,
            minutes=0,
            current_price=updated_price
        )

        if not ma_data:
            return False

        less_ma_history = ma_data['less']['result']
        more_ma_history = ma_data['more']['result']

        if len(less_ma_history) < 1 or len(more_ma_history) < 1:
            return False

        less_ma_current = less_ma_history[0]
        more_ma_current = more_ma_history[0]

        if None in [less_ma_current, more_ma_current]:
            return False

        if order_side == TradeType.BUY:
            # Проверяем на "Крест смерти" (пересечение вниз)
            # Если быстрая MA пересекла медленную сверху вниз, закрываем позицию
            if less_ma_current < more_ma_current:
                # print(f"Сигнал на закрытие покупки: Крест смерти на {symbol} в {datetime.now().strftime('%H:%M:%S')}")
                return True
            # if less_ma_current > updated_price:
            #     return True
            if less_ma_current > updated_price > close_not_lose_price:
                return True

        # Если открыт ордер на ПРОДАЖУ
        elif order_side == TradeType.SELL:
            # Проверяем на "Золотой крест" (пересечение вверх)
            # Если быстрая MA пересекла медленную снизу вверх, закрываем позицию
            if less_ma_current > more_ma_current:
                # print(f"Сигнал на закрытие продажи: Золотой крест на {symbol} в {datetime.now().strftime('%H:%M:%S')}")
                return True
            # if less_ma_current < updated_price:
            #     return True
            if less_ma_current < updated_price < close_not_lose_price:
                return True

        # Если обратного пересечения не было, оставляем ордер открытым
        return False