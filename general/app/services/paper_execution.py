"""Бумажное исполнение: результат сделки и его публикация.

Общее для всех стратегий. Алгоритм решает, когда войти и когда выйти;
во что это превращается — PnL, комиссии, поля сделки, очередь на
вставку — одинаково у любого из них, и переписывать это в каждой новой
стратегии значит заводить второй источник правды о деньгах.

Запись идёт в очередь Redis, а не в базу: сделок сотни в секунду, и
вставка пачками — отдельный процесс (`bulk_insert_orders.py`).
"""
import json
import logging

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from app.constants.order import ORDER_QUEUE_KEY
from app.sub_services.logic.price_calculator import PriceCalculator

UTC = timezone.utc


@dataclass
class TradeResult:
    """Закрытая сделка в том виде, в каком она уходит на запись."""

    pnl: Decimal
    open_fee: Decimal
    close_fee: Decimal
    payload: dict


def json_serializer(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


def build_trade(
    *,
    order,
    bot_id: int,
    symbol: str,
    trade_type: str,
    balance,
    open_price,
    close_price,
    commission_rate,
    referral_bot_id: Optional[int],
    strategy_id: int,
    executed_strategy_id: int,
    algorithm_version: Optional[str],
    donor_chain: Optional[list],
) -> TradeResult:
    """Считает результат и собирает поля сделки.

    `balance` — номинал позиции, а не счёт бота: у компаундирующего
    копибота v3 количество округлено по шагу лота, и в расчёт уходит
    именно номинал. Иначе PnL считался бы по дробному количеству,
    которого на бирже не бывает.
    """
    pnl = PriceCalculator.calculate_pnl(
        balance=balance,
        close_price=close_price,
        open_price=open_price,
        trade_type=trade_type,
        commission_open=commission_rate,
        commission_close=commission_rate,
    )

    # Тем же расчётом, что и внутри calculate_pnl, иначе поля
    # open_fee/close_fee не сходятся с profit_loss.
    open_fee, close_fee = PriceCalculator.calculate_fees(
        balance=balance,
        open_price=open_price,
        close_price=close_price,
        commission_open=commission_rate,
        commission_close=commission_rate,
    )

    now = datetime.now(UTC)

    payload = {
        "asset_symbol": symbol,
        "order_type": trade_type,
        "balance": str(balance),
        "open_price": str(open_price),
        "open_time": order.open_time,
        "open_fee": str(open_fee),
        "stop_loss_price": str(order.stop_loss_price),
        "bot_id": bot_id,
        "close_price": str(close_price),
        "close_time": now,
        "close_fee": str(close_fee),
        "profit_loss": str(pnl),
        "is_active": False,
        "start_updown_ticks": int(order.start_updown_ticks),
        "stop_loss_ticks": int(order.stop_loss_ticks),
        "stop_success_ticks": int(order.stop_success_ticks),
        "stop_reason_event": order.stop_reason_event,
        "referral_bot_id": referral_bot_id,
        "strategy_id": strategy_id,
        "executed_strategy_id": executed_strategy_id,
        "algorithm_version": algorithm_version,
        "donor_chain": donor_chain,
        "created_at": now,
        "updated_at": now,
    }

    return TradeResult(
        pnl=pnl, open_fee=open_fee, close_fee=close_fee, payload=payload
    )


async def publish_trade(redis, trade: TradeResult) -> None:
    """Кладёт сделку в очередь на пакетную вставку."""
    await redis.rpush(
        ORDER_QUEUE_KEY, json.dumps(trade.payload, default=json_serializer)
    )
