"""Ключ стратегии -> реализация алгоритма.

Реестр живёт в коде, а таблица `strategies` — в базе: строка там нужна,
чтобы на стратегию ссылались боты и сделки, а торгует всегда код.
Отсюда правило: ключ, которого нет в реестре, не запускается. Провести
такого бота по текущему алгоритму «раз уж он похож» значит записать в
историю сделки, которых этот алгоритм не совершал.

Чтобы добавить стратегию, достаточно реализовать `Algorithm`
(`app/strategies/base.py`) и вписать её сюда одной строкой.
"""
from app.strategies.base import Algorithm
from app.strategies.legacy.algorithm import LegacyAlgorithm


class UnknownAlgorithm(LookupError):
    """Стратегия есть в базе, но реализации под её ключ нет."""


_ALGORITHMS: dict[str, Algorithm] = {
    LegacyAlgorithm.key: LegacyAlgorithm(),
}


def get_algorithm(strategy_key: str) -> Algorithm:
    algorithm = _ALGORITHMS.get(strategy_key)

    if algorithm is None:
        raise UnknownAlgorithm(
            f"Стратегия {strategy_key!r} не реализована: "
            f"известны {', '.join(sorted(_ALGORITHMS))}"
        )

    return algorithm


def known_keys() -> list[str]:
    return sorted(_ALGORITHMS)
