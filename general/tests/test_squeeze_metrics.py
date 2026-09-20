"""Метрики отбора пар: вынос, возврат, плотность сделок.

По ним решается, на каких парах вообще запускать стратегию, поэтому
проверяются на свечах с заранее известным ответом.

Главное здесь — отличить пару, где вынос откупают, от «падающего ножа»,
где тот же вынос продолжается трендом. Для стратегии это разница между
сделкой и убытком, а по одной частоте выносов они неразличимы.
"""
from decimal import Decimal

from app.sub_services.watchers.candle_store import (
    rebound_share,
    squeeze_events,
    trade_density,
)

MINUTE = 60_000
START = 1_700_000_000_000
PERIOD = 14


def candle(index, open_, high, low, close, trades=100):
    return {"t": START + index * MINUTE, "o": str(open_), "h": str(high),
            "l": str(low), "c": str(close), "v": "10", "n": trades}


def calm(count, start_index=0):
    """Спокойный фон: ATR выходит ровно 0.4."""
    return [candle(start_index + i, 100, 100.2, 99.8, 100) for i in range(count)]


def test_squeeze_is_distance_from_previous_close():
    """Событие — уход цены от прошлого закрытия, а не длина тени.

    Через тень «падающий нож» неотличим от обычной свечи: тень
    отсчитывается от тела и сама по себе означает возврат.
    """
    knife = calm(20) + [candle(20, 100, 100.2, 97, 97.1)]
    spike = calm(20) + [candle(20, 100, 100.2, 97, 99.9)]

    for candles in (knife, spike):
        events = squeeze_events(candles, PERIOD)
        assert len(events) == 1, "оба случая — выносы: цена дошла до уровня"
        assert events[0]["side"] == "down"
        assert events[0]["length"] == Decimal(3), "от закрытия 100 до 97"

    # Спокойная свеча событием не является.
    assert squeeze_events(calm(20), PERIOD) == []


def test_rebound_separates_reversion_from_falling_knife():
    """Один и тот же вынос: откупленный и продолжившийся вниз."""
    # Провал с 100 до 97 и возврат внутри той же минуты: закрытие 99.9
    # выше цели 97 + 30% × 3 = 97.9.
    reverted = calm(20) + [candle(20, 100, 100.2, 97, 99.9)]
    # Тот же вынос, но цена продолжает падать — возврата нет.
    knife = calm(20) + [
        candle(20, 100, 100.2, 97, 97.1),
        candle(21, 97.1, 97.2, 95, 95.1),
        candle(22, 95.1, 95.2, 93, 93.1),
    ]

    events, share = rebound_share(reverted, PERIOD)
    assert events == 1 and share == Decimal(100)


    events, share = rebound_share(knife, PERIOD)
    assert events and share == Decimal(0), (
        "«падающий нож» не должен считаться возвратом: стратегия на нём "
        "набирает убыточную позицию"
    )


def test_rebound_horizon_is_limited():
    """Возврат через полчаса стратегии не поможет — у неё тайм-стоп."""
    late = calm(20) + [
        candle(20, 100, 100.2, 97, 97.1),
        candle(21, 97.1, 97.3, 97.0, 97.2),
        candle(22, 97.2, 97.4, 97.1, 97.3),
        # Возврат случился, но на третьей свече — за горизонтом.
        candle(23, 97.3, 98.5, 97.2, 98.4),
    ]

    # Событие одно: дальше цена уже не уходит на два ATR от закрытия.
    first_events, share = rebound_share(late, PERIOD, horizon=2)
    assert share == Decimal(0)

    _, share = rebound_share(late, PERIOD, horizon=4)
    assert share > Decimal(0), "с горизонтом в четыре свечи возврат виден"


def test_upper_shadow_rebounds_downward():
    """Вынос вверх возвращается вниз — направление учитывается."""
    # Рывок с 100 до 103 и закрытие у вершины; цель возврата —
    # 103 - 30% × 3 = 102.1, она достигается следующей свечой. Сам спуск
    # при этом слишком мал, чтобы стать новым событием.
    candles = calm(20) + [
        candle(20, 100, 103, 99.8, 102.9),
        candle(21, 102.9, 103, 102.0, 102.05),
    ]

    events = squeeze_events(candles, PERIOD)
    assert len(events) == 1 and events[0]["side"] == "up"
    assert events[0]["length"] == Decimal(3)

    count, share = rebound_share(candles, PERIOD)
    assert count == 1 and share == Decimal(100)


def test_rebound_inside_the_squeeze_candle_counts():
    """Вынос, откупленный внутри той же минуты, — лучший случай.

    Позиция стратегии живёт секунды: если цена ушла в тень и вернулась
    до закрытия свечи, сделка уже закрыта по цели. Не засчитывать такой
    возврат значило бы занижать именно те пары, которые нужны.
    """
    candles = calm(20) + [candle(20, 100, 100.2, 97, 99.9)]

    events, share = rebound_share(candles, PERIOD)

    assert events == 1 and share == Decimal(100)


def test_few_events_are_reported_as_such():
    """Доля без числа событий обманчива — возвращается и то, и другое."""
    assert rebound_share(calm(20), PERIOD) == (0, None)


def test_trade_density_counts_dry_minutes():
    candles = [candle(i, 100, 100.1, 99.9, 100, trades=n)
               for i, n in enumerate([0, 0, 50, 100, 200])]

    trades, dry = trade_density(candles)

    # Медиана ряда 0, 0, 50, 100, 200 — это 50. Среднее (70) завысило бы
    # активность пары, где две минуты из пяти пустые.
    assert trades == Decimal(50)
    assert dry == Decimal(40), "две пустые минуты из пяти"


def test_density_is_unknown_without_field():
    """Старые свечи без числа сделок не притворяются активными."""
    candles = [{"t": START, "o": "1", "h": "1", "l": "1", "c": "1"}]

    assert trade_density(candles) == (None, None)


def main():
    test_squeeze_is_distance_from_previous_close()
    test_rebound_separates_reversion_from_falling_knife()
    test_rebound_horizon_is_limited()
    test_upper_shadow_rebounds_downward()
    test_rebound_inside_the_squeeze_candle_counts()
    test_few_events_are_reported_as_such()
    test_trade_density_counts_dry_minutes()
    test_density_is_unknown_without_field()
    print("squeeze metrics: ок")


if __name__ == "__main__":
    main()
