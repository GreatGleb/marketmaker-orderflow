"""Общие заглушки стратегий для проверок без базы.

Реальный id стратегии выдаёт база и в каждом развёртывании он свой, но
проверкам нужен какой-то один: важно не число, а то, что симулятор и
воркер видят одну и ту же карту.
"""
from app.constants.strategy import STRATEGY_LEGACY

LEGACY_STRATEGY_ID = 1

STRATEGIES = {LEGACY_STRATEGY_ID: STRATEGY_LEGACY}

# То, что симулятор читает на старте и держит в `_strategy_keys`. Без
# карты он не знает, каким алгоритмом торговать, и бота не запускает —
# так же, как на живой базе со стратегией, которой нет в коде.
STRATEGY_KEYS = dict(STRATEGIES)


class FakeResult:
    """Ответ session.execute: только то, что читают карты стратегий."""

    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return list(self.rows)


def strategy_rows(by_id: bool):
    """Строки для `keys_by_id` (id, key) либо `ids_by_key` (key, id)."""
    if by_id:
        return [(id_, key) for id_, key in STRATEGIES.items()]

    return [(key, id_) for id_, key in STRATEGIES.items()]


def execute_strategy_maps(statement):
    """Подмена session.execute для запросов к `strategies`.

    Различать запросы приходится по порядку колонок: оба читают одну
    таблицу и отличаются только им. Заглушка узкая намеренно — всё
    остальное должно падать, а не отдавать пустой ответ молча.
    """
    columns = [column.name for column in statement.selected_columns]

    if columns == ["id", "key"]:
        return FakeResult(strategy_rows(by_id=True))

    if columns == ["key", "id"]:
        return FakeResult(strategy_rows(by_id=False))

    raise AssertionError(f"Неожиданный запрос к стратегиям: {columns}")
