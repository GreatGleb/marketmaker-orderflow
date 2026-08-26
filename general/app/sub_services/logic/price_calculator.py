from decimal import ROUND_HALF_UP, Decimal

from app.constants.commissions import COMMISSION_OPEN, COMMISSION_CLOSE
from app.enums.trade_type import TradeType


class PriceCalculator:

    @staticmethod
    def calculate_take_profit_price(
        stop_success_ticks, tick_size, open_price, trade_type,
        commission_open=COMMISSION_OPEN, commission_close=COMMISSION_CLOSE,
    ):
        desired_net_profit_value = Decimal(stop_success_ticks) * tick_size

        if trade_type == TradeType.BUY:
            commission_open_cost = 1 + commission_open
            commission_close_cost = 1 - commission_close
            base_take_profit = (
                open_price * commission_open_cost + desired_net_profit_value
            )
            take_profit_price = base_take_profit / commission_close_cost
        else:
            commission_open_cost = 1 - commission_open
            commission_close_cost = 1 + commission_close
            base_take_profit = (
                open_price * commission_open_cost - desired_net_profit_value
            )
            take_profit_price = base_take_profit / commission_close_cost

        take_profit_price = take_profit_price.quantize(
            tick_size, rounding=ROUND_HALF_UP
        )

        return take_profit_price

    @staticmethod
    def calculate_trailing_take_profit_price(
        peak_favorable_price,
        stop_success_ticks,
        tick_size,
        trade_type
    ):
        trail_value = Decimal(stop_success_ticks) * tick_size
        peak_price = Decimal(str(peak_favorable_price))

        if trade_type == TradeType.BUY:
            take_profit_price = peak_price - trail_value
        else:
            take_profit_price = peak_price + trail_value

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
    def calculate_close_not_lose_price(
        open_price, trade_type,
        commission_open=COMMISSION_OPEN, commission_close=COMMISSION_CLOSE,
    ):
        if trade_type == TradeType.BUY:
            commission_open_cost = 1 + commission_open
            commission_close_cost = 1 - commission_close
        else:
            commission_open_cost = 1 - commission_open
            commission_close_cost = 1 + commission_close

        close_not_lose_price = (
            open_price * commission_open_cost
        ) / commission_close_cost

        return close_not_lose_price

    @staticmethod
    def calculate_fees(
        balance, open_price, close_price,
        commission_open=COMMISSION_OPEN, commission_close=COMMISSION_CLOSE,
    ):
        """Комиссии сделки в деньгах.

        Обе считаются от объёма позиции: количество берётся по цене входа,
        комиссия открытия — от объёма входа, комиссия закрытия — от объёма
        выхода (то же количество, но по цене закрытия). Единственный источник
        истины для profit_loss и для полей open_fee/close_fee в test_orders.
        """
        amount = Decimal(balance) / Decimal(open_price)
        open_fee = amount * open_price * commission_open
        close_fee = amount * close_price * commission_close

        return open_fee, close_fee

    @staticmethod
    def calculate_pnl(
        trade_type, balance, open_price, close_price,
        commission_open=COMMISSION_OPEN, commission_close=COMMISSION_CLOSE,
    ):
        pnl = Decimal("0.0")

        amount = Decimal(balance) / Decimal(open_price)
        open_fee, close_fee = PriceCalculator.calculate_fees(
            balance=balance,
            open_price=open_price,
            close_price=close_price,
            commission_open=commission_open,
            commission_close=commission_close,
        )
        total_commission = open_fee + close_fee

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

    @staticmethod
    def get_peak_favorable_price(
        current_peak_favorable_price,
        current_price,
        trade_type
    ):
        peak_favorable_price = current_peak_favorable_price
        if trade_type == TradeType.BUY:
            if current_price > current_peak_favorable_price:
                peak_favorable_price = current_price
        else:
            if current_price < current_peak_favorable_price:
                peak_favorable_price = current_price

        return peak_favorable_price
