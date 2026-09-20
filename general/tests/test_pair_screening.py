"""Отбор пар: запас цели над издержками и жёсткие отсечки скринера.

Главная проверка здесь — что пара, на которой стратегия убыточна по
арифметике, не проходит отбор, как бы хорошо ни выглядели остальные её
метрики. Такую пару легко принять за лучшую: на BTCUSDT выносы частые и
возвращаются охотно, но цель сделки там меньше комиссии.

Заглушки, база и сеть не нужны.
"""
from decimal import Decimal

from app.scripts.pair_screener import (
    DEFAULT_MIN_EDGE, edge_ratio, passes, score,
)

# Стандартные ставки фьючерсов: maker 0.02%, taker 0.05%.
RATES = (Decimal("0.0002"), Decimal("0.0005"))


def row(**overrides):
    base = {
        "symbol": "TESTUSDT", "atr": Decimal("0.5"), "squeezes": Decimal(50),
        "events": 100, "rebound": Decimal(80), "trades": Decimal(500),
        "dry": Decimal(0), "spread": Decimal("0.01"), "edge": Decimal(5),
        "turnover": Decimal("50000000"), "spread_atr": Decimal("0.02"),
    }
    base.update(overrides)

    return base


def test_edge_counts_both_commissions_spread_and_slippage():
    """Издержки — это комиссии обеих сторон, спред и проскальзывание."""
    atr = Decimal("1")
    spread = Decimal("0.01")

    value = edge_ratio(atr, spread, RATES)

    # Цель: 1% × 2.5 × 0.4 = 1%. Издержки: 0.07% комиссий + 0.01% спреда
    # + 0.1% проскальзывания = 0.18%.
    assert value == Decimal("1") / Decimal("0.18")


def test_quiet_major_is_rejected_despite_perfect_metrics():
    """BTC-подобная пара: выносы частые, возвраты есть, торговать нечем.

    ATR 0.03% даёт цель 0.03% при издержках около 0.08% — каждая сделка
    отдаёт бирже втрое больше, чем приносит рынок.
    """
    quiet = row(atr=Decimal("0.03"), spread=Decimal("0.0001"),
                rebound=Decimal(70), squeezes=Decimal(70))
    quiet["edge"] = edge_ratio(quiet["atr"], quiet["spread"], RATES)

    assert quiet["edge"] < 1, quiet["edge"]
    assert not passes(quiet, min_trades=30, max_dry=Decimal(2), min_events=20), (
        "пара с целью ниже издержек не должна проходить отбор"
    )


def test_volatile_pair_passes():
    fit = row(atr=Decimal("0.8"), spread=Decimal("0.02"))
    fit["edge"] = edge_ratio(fit["atr"], fit["spread"], RATES)

    assert fit["edge"] > DEFAULT_MIN_EDGE
    assert passes(fit, min_trades=30, max_dry=Decimal(2), min_events=20)


def test_dry_and_thin_pairs_are_rejected():
    """Сухие минуты и редкие сделки — заявка там просто не исполнится."""
    dry = row(dry=Decimal(5))
    thin = row(trades=Decimal(5))
    rare = row(events=3)
    never_returns = row(rebound=None)

    for candidate, why in (
        (dry, "минуты без сделок"),
        (thin, "мало сделок в минуту"),
        (rare, "выносов слишком мало, доля возврата случайна"),
        (never_returns, "возвратов нет вовсе"),
    ):
        assert not passes(
            candidate, min_trades=30, max_dry=Decimal(2), min_events=20
        ), why


def test_score_prefers_returning_squeezes_over_frequent_ones():
    """Частота без возврата ничего не стоит — и наоборот."""
    frequent_knife = row(squeezes=Decimal(200), rebound=Decimal(10))
    rare_reverting = row(squeezes=Decimal(40), rebound=Decimal(90))

    assert score(rare_reverting) > score(frequent_knife)


def test_score_prefers_cheaper_pair_when_equal():
    """При равных частоте и возврате лучше та, где остаётся больше."""
    cheap = row(edge=Decimal(8))
    costly = row(edge=Decimal(3))

    assert score(cheap) > score(costly)


def test_edge_is_unknown_without_atr():
    assert edge_ratio(None, Decimal("0.01"), RATES) is None
    assert edge_ratio(Decimal(0), Decimal("0.01"), RATES) is None


def main():
    test_edge_counts_both_commissions_spread_and_slippage()
    test_quiet_major_is_rejected_despite_perfect_metrics()
    test_volatile_pair_passes()
    test_dry_and_thin_pairs_are_rejected()
    test_score_prefers_returning_squeezes_over_frequent_ones()
    test_score_prefers_cheaper_pair_when_equal()
    test_edge_is_unknown_without_atr()
    print("pair screening: ок")


if __name__ == "__main__":
    main()
