from decimal import ROUND_HALF_UP, Decimal

from app.constants.commissions import COMMISSION_OPEN, COMMISSION_CLOSE
from app.enums.trade_type import TradeType


class PriceCalculator:

    @staticmethod
    def calculate_take_profit_price(
        stop_success_ticks, tick_size, open_price, trade_type
    ):
        desired_net_profit_value = Decimal(stop_success_ticks) * tick_size

        if trade_type == TradeType.BUY:
            commission_open_cost = 1 + COMMISSION_OPEN
            commission_close_cost = 1 - COMMISSION_CLOSE
            base_take_profit = (
                open_price * commission_open_cost + desired_net_profit_value
            )
            take_profit_price = base_take_profit / commission_close_cost
        else:
            commission_open_cost = 1 - COMMISSION_OPEN
            commission_close_cost = 1 + COMMISSION_CLOSE
            base_take_profit = (
                open_price * commission_open_cost - desired_net_profit_value
            )
            take_profit_price = base_take_profit / commission_close_cost

        take_profit_price = take_profit_price.quantize(
            tick_size, rounding=ROUND_HALF_UP
        )

        return take_profit_price

    @staticmethod
    def calculate_stop_lose_price(
        stop_loss_ticks, tick_size, open_price, trade_type
    ):
        stop_loss_price = (
            open_price - stop_loss_ticks * tick_size
            if trade_type == TradeType.BUY
            else open_price + stop_loss_ticks * tick_size
        )

        return stop_loss_price

    @staticmethod
    def calculate_close_not_lose_price(open_price, trade_type):
        if trade_type == TradeType.BUY:
            commission_open_cost = 1 + COMMISSION_OPEN
            commission_close_cost = 1 - COMMISSION_CLOSE
        else:
            commission_open_cost = 1 - COMMISSION_OPEN
            commission_close_cost = 1 + COMMISSION_CLOSE

        close_not_lose_price = (
            open_price * commission_open_cost
        ) / commission_close_cost

        return close_not_lose_price

    @staticmethod
    def calculate_pnl(trade_type, balance, open_price, close_price):
        pnl = Decimal("0.0")

        amount = Decimal(balance) / Decimal(open_price)
        commission_open = amount * open_price * COMMISSION_OPEN
        commission_close = amount * close_price * COMMISSION_CLOSE
        total_commission = commission_open + commission_close

        if trade_type == TradeType.BUY:
            pnl = (
                (amount * close_price)
                - (amount * open_price)
                - total_commission
            )
        elif trade_type == TradeType.SELL:
            pnl = (
                (amount * open_price)
                - (amount * close_price)
                - total_commission
            )

        return pnl
