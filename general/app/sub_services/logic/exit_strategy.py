from app.enums.trade_type import TradeType
from app.enums.event_type import StopReasonEvent


class ExitStrategy:

    @staticmethod
    async def check_exit_conditions(
        trade_type,
        price_from_previous_step,
        updated_price,
        new_tk_p,
        new_sl_p,
        close_not_lose_price,
        take_profit_price,
        order,
        binance_bot,
        symbol,
        consider_ma_for_close_order,
    ):
        if trade_type == TradeType.BUY:
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

            if ma25 and updated_price > ma25:
                return True, take_profit_price

            if updated_price >= order.stop_loss_price:
                order.stop_reason_event = StopReasonEvent.STOP_LOOSED.value
                return True, take_profit_price

            if take_profit_price <= updated_price < close_not_lose_price:
                order.stop_reason_event = StopReasonEvent.STOP_WON.value
                return True, take_profit_price

        return False, take_profit_price
