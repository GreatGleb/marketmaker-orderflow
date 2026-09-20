"""Парк стратегии 1: полный крест конфигураций и пар.

Проверяется устройство эксперимента, а не код записи в базу. Если
конфигурация существует в одном экземпляре на одной паре, её результат
неотделим от характера этой пары: выигравший отступ может оказаться
свойством не отступа, а монеты. Такую ошибку легко внести обратно —
раздача пар «по кругу» выглядит безобиднее и короче.
"""
import json

from collections import Counter

from app.constants.strategy_1_seed import strategy_1_bot_rows
from app.strategies.strategy_1.algorithm import Strategy1Algorithm

ROSTER = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
ALGORITHM = Strategy1Algorithm()


def configs_of(rows):
    return [json.dumps(row["strategy_config"], sort_keys=True) for row in rows]


def test_every_config_runs_on_every_pair():
    rows = strategy_1_bot_rows(ROSTER)
    configs = set(configs_of(rows))

    assert len(rows) == len(configs) * len(ROSTER), (
        "число ботов должно быть произведением конфигураций на пары"
    )

    # У каждой конфигурации ровно по одному боту на каждой паре.
    for config in configs:
        pairs = [row["symbol"] for row in rows
                 if json.dumps(row["strategy_config"], sort_keys=True) == config]

        assert sorted(pairs) == sorted(ROSTER), (
            f"конфигурация заведена не на всех парах: {sorted(pairs)}"
        )


def test_pairs_carry_equal_load():
    counts = Counter(row["symbol"] for row in strategy_1_bot_rows(ROSTER))

    assert len(set(counts.values())) == 1, (
        f"пары нагружены неравномерно: {counts}"
    )


def test_every_config_is_valid_for_the_algorithm():
    """Сетка не должна содержать настроек, которые алгоритм отвергнет."""
    for row in strategy_1_bot_rows(ROSTER):
        ALGORITHM.parse_config(row["strategy_config"])


def test_order_is_shuffled_for_sharding():
    """Соседние по сетке боты не должны идти подряд.

    id распределяет ботов по шардам, и при упорядоченной сетке в одном
    процессе окажутся боты с соседними параметрами — на одной паре,
    просыпающиеся в один и тот же момент.
    """
    symbols = [row["symbol"] for row in strategy_1_bot_rows(ROSTER)]
    runs = sum(1 for a, b in zip(symbols, symbols[1:]) if a == b)

    # При раздаче по порядку подряд идут длинные серии одной пары.
    assert runs < len(symbols) / 2, f"порядок похож на неперемешанный: {runs}"


def test_no_pairs_no_bots():
    assert strategy_1_bot_rows([]) == []


def main():
    test_every_config_runs_on_every_pair()
    test_pairs_carry_equal_load()
    test_every_config_is_valid_for_the_algorithm()
    test_order_is_shuffled_for_sharding()
    test_no_pairs_no_bots()
    print("strategy_1 seed: ок")


if __name__ == "__main__":
    main()
