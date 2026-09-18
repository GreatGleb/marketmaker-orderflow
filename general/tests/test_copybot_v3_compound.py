"""Компаундирующий копибот v3: счёт, шаг лота и остановка.

Этот бот — единственный в парке, кто ведёт счёт, а не считает каждую сделку на
условную тысячу. Прибыль реинвестируется, в позицию идёт 99% баланса,
количество округляется вниз по шагу лота.

Первые два — множители, и их можно было бы получить пересчётом задним числом
из сделок обычного бота: PnL линеен по балансу. Лот — нет. Именно он
показывает, где реальный бот перестанет торговать, потому что на счёте не
набирается minQty, и ради него отдельный бот и заводится.

Отсюда же тонкость расчёта: PnL считается от номинала позиции
(`количество × цена`), а не от счёта. `calculate_pnl` внутри делает
`amount = balance / open_price`, поэтому передать туда счёт значило бы
посчитать прибыль по дробному количеству, которого на бирже не бывает.

Заглушки, база не нужна:

    python -m tests.test_copybot_v3_compound
"""
import asyncio

from decimal import Decimal

from app.bots.demo_test_bot import StartTestBotsCommand as S
from app.sub_services.logic.price_calculator import PriceCalculator
from app.enums.trade_type import TradeType

# Пара с крупным шагом лота: на ней округление видно глазами.
MARKET = {
    "step_size": Decimal("0.1"),
    "market_step_size": Decimal("0.1"),
    "market_min_qty": Decimal("1"),
    "market_max_qty": Decimal("100000"),
    "min_notional": Decimal("5"),
    "min_qty": Decimal("1"),
    "max_qty": Decimal("1000"),
    "min_price": Decimal("0.01"),
    "max_price": Decimal("100000"),
}

PRICE = Decimal("100")


def check_quantity_is_floored_to_step():
    """Количество округляется вниз по шагу, номинал считается от него."""
    quantity, notional, refusal = S.compound_order_size(
        balance=Decimal("1000"), price=PRICE, market_data=MARKET
    )

    assert refusal is None, f"неожиданный отказ: {refusal}"

    # 1000 * 0.99 / 100 = 9.9 — ровно по шагу 0.1.
    assert quantity == Decimal("9.9"), (
        f"количество {quantity}, ожидалось 9.9 (99% от 1000 по цене 100)"
    )
    assert notional == quantity * PRICE, (
        f"номинал {notional} не равен количеству на цену: PnL посчитается "
        f"по количеству, которого не было"
    )

    # Дробный остаток обязан срезаться вниз, а не округляться к ближайшему:
    # биржа принимает только кратное шагу, и вверх округлить нечем.
    quantity, _, _ = S.compound_order_size(
        balance=Decimal("1007"), price=PRICE, market_data=MARKET
    )

    assert quantity == Decimal("9.9"), (
        f"количество {quantity}: 1007 * 0.99 / 100 = 9.9693 должно стать 9.9, "
        f"а не 10.0 — иначе ордер уйдёт больше, чем есть на счету"
    )

    print("  количество срезается вниз по шагу лота, номинал сходится")


def check_pnl_matches_rounded_position():
    """PnL от номинала — это PnL по округлённому количеству."""
    _, notional, _ = S.compound_order_size(
        balance=Decimal("1000"), price=PRICE, market_data=MARKET
    )

    close_price = Decimal("101")

    pnl = PriceCalculator.calculate_pnl(
        trade_type=TradeType.BUY,
        balance=notional,
        open_price=PRICE,
        close_price=close_price,
        commission_open=Decimal("0"),
        commission_close=Decimal("0"),
    )

    # 9.9 монеты, цена выросла на 1 — прибыль ровно 9.9.
    assert pnl == Decimal("9.9"), (
        f"PnL {pnl}, ожидалось 9.9: прибыль должна считаться по 9.9 монеты, "
        f"а не по дробному количеству от полного баланса"
    )

    open_fee, close_fee = PriceCalculator.calculate_fees(
        balance=notional,
        open_price=PRICE,
        close_price=close_price,
        commission_open=Decimal("0.001"),
        commission_close=Decimal("0.001"),
    )

    pnl_with_fees = PriceCalculator.calculate_pnl(
        trade_type=TradeType.BUY,
        balance=notional,
        open_price=PRICE,
        close_price=close_price,
        commission_open=Decimal("0.001"),
        commission_close=Decimal("0.001"),
    )

    assert pnl_with_fees == pnl - open_fee - close_fee, (
        "комиссии в ордере и в PnL посчитаны от разных величин — поля "
        "open_fee/close_fee перестанут сходиться с profit_loss"
    )

    print(f"  PnL {pnl} по 9.9 монеты, комиссии сходятся с profit_loss")


def check_balance_compounds():
    """Счёт растёт и падает на результат сделки, и размер идёт за ним."""
    balance = Decimal("1000")
    first, _, _ = S.compound_order_size(
        balance=balance, price=PRICE, market_data=MARKET
    )

    balance = balance + Decimal("500")
    grown, _, _ = S.compound_order_size(
        balance=balance, price=PRICE, market_data=MARKET
    )

    assert grown > first, (
        f"после прибыли размер позиции {grown} не вырос относительно {first} "
        f"— реинвестирования не происходит"
    )

    balance = Decimal("200")
    shrunk, _, _ = S.compound_order_size(
        balance=balance, price=PRICE, market_data=MARKET
    )

    assert shrunk < first, (
        f"после убытка размер позиции {shrunk} не уменьшился относительно "
        f"{first} — бот рискует деньгами, которых у него нет"
    )

    print(f"  размер идёт за счётом: {first} → {grown} → {shrunk}")


def check_refusal_kinds_are_distinguished():
    """Нехватка средств отделена от отказов, привязанных к паре.

    Копибот торгует парой донора, а донор меняется от сделки к сделке. Если
    свалить все отказы в один, бот остановится навсегда из-за одной
    экзотической пары, и оборванный прогноз будет неотличим от настоящего
    слива счёта.
    """
    quantity, notional, refusal = S.compound_order_size(
        balance=Decimal("50"), price=PRICE, market_data=MARKET
    )

    assert quantity is None and notional is None, (
        "при нехватке на минимальный лот вернулось количество — бот открыл бы "
        "сделку, которую биржа не приняла бы"
    )
    assert refusal, "отказ без причины: в лог уйдёт пустая строка"
    assert refusal.is_shortage, (
        f"нехватка помечена как {refusal.kind}: бот не остановится, даже "
        f"когда счёт действительно кончится"
    )

    print(f"  баланс 50 по цене 100: {refusal.kind} — {refusal}")

    # Слишком большой размер — отказ, но про пару, а не про счёт: на
    # следующей паре донора он же может пройти.
    _, _, refusal = S.compound_order_size(
        balance=Decimal("200000"), price=PRICE, market_data=MARKET
    )

    assert refusal, "превышение maxQty прошло молча"
    assert not refusal.is_shortage, (
        "превышение maxQty принято за нехватку средств — бот с полным счётом "
        "остановится на первой же дешёвой монете"
    )

    print(f"  превышение максимума: {refusal.kind} — {refusal}")


def check_price_bounds_only_skip():
    """Границы цены — состояние рынка: сделка пропускается, бот живёт."""
    assert S.is_price_within_bounds(PRICE, MARKET), (
        "нормальная цена признана выходящей за границы"
    )
    assert not S.is_price_within_bounds(Decimal("200000"), MARKET), (
        "цена выше maxPrice признана допустимой — ордер ушёл бы в отказ "
        "на бирже"
    )
    assert not S.is_price_within_bounds(Decimal("0.001"), MARKET), (
        "цена ниже minPrice признана допустимой"
    )

    print("  границы цены проверяются отдельно от лота")


def check_missing_market_data_is_refusal():
    """Нет шага лота — отказ, а не торговля наугад."""
    _, _, refusal = S.compound_order_size(
        balance=Decimal("1000"), price=PRICE, market_data={}
    )

    assert refusal, (
        "без шага лота расчёт прошёл: на паре с незасеянными спеками бот "
        "посчитал бы размер, которого биржа не примет"
    )
    assert not refusal.is_shortage, (
        "незасеянные спеки приняты за нехватку средств — бот остановится "
        "навсегда из-за дырки в справочнике пар"
    )

    print(f"  пустые спеки пары: {refusal.kind} — {refusal}")


async def main():
    print("Проверяем компаундирующего копибота v3")

    check_quantity_is_floored_to_step()
    check_pnl_matches_rounded_position()
    check_balance_compounds()
    check_refusal_kinds_are_distinguished()
    check_price_bounds_only_skip()
    check_missing_market_data_is_refusal()

    print("✅ всё сошлось")


if __name__ == "__main__":
    asyncio.run(main())
