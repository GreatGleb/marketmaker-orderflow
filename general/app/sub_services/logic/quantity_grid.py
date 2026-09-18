"""Общая сетка количества для сохранённых фильтров USDⓈ-M."""
from decimal import Decimal
from math import gcd, lcm


def quantity_grid(lots):
    """(Начало, шаг) пересечения сеток (q-minQty) % stepSize == 0.

    Нулевой рыночный шаг отключает только сетку, но не min/maxQty.
    Вызвавший код проверяет конечность и границы всех чисел.
    """
    grids = [(lo, step) for lo, _, step in lots if step > 0]
    scale = Decimal(10) ** max(0, *(-v.as_tuple().exponent for g in grids for v in g))
    origin, step = (int(v * scale) for v in grids[0])
    for lo, next_step in grids[1:]:
        other, increment = int(lo * scale), int(next_step * scale)
        common = gcd(step, increment)
        if (other - origin) % common:
            raise ValueError("несовместимые сетки количества")
        modulus = increment // common
        k = ((other - origin) // common * pow(step // common, -1, modulus)) % modulus if modulus > 1 else 0
        period = lcm(step, increment)
        origin, step = (origin + step * k) % period, period
    return Decimal(origin) / scale, Decimal(step) / scale
