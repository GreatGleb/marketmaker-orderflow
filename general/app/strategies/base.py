"""Контракт торговой стратегии.

Граница проведена там же, где она проходит по смыслу: стратегия решает,
когда входить и когда выходить, а всё остальное — выбор донора и пары,
рыночные данные, комиссии, проверка лота, запись результата — общее для
всех и остаётся в симуляторе.

Из этого следует, что стратегия не ходит в базу и не пишет в очередь
результатов. Ей дают готовый контекст и просят решение.

Чтобы добавить новую стратегию, нужно реализовать `Algorithm` и
зарегистрировать ключ в `registry.py`. Цикл симулятора при этом не
трогается — в этом и была цель разделения.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass
class MarketContext:
    """Всё общее, что стратегия получает готовым.

    `before_entry` оборачивает ожидание до открытия позиции: у копибота
    внутри сидит проверка, не отозван ли выбранный донор. Стратегия про
    доноров ничего не знает — она просто обязана пропускать через эту
    обёртку любое ожидание до входа.
    """

    bot_id: int
    bot_config: Any
    symbol: str
    tick_size: Decimal
    market_data: dict
    commission_rate: Any
    price_provider: Any
    price_watcher: Any
    binance_bot: Any
    before_entry: Any


@dataclass
class EntryLevels:
    """Уровни входа и сколько ждать их достижения."""

    buy: Decimal
    sell: Decimal
    timeout_seconds: int


@dataclass
class EntryResult:
    """Чем закончилось ожидание входа."""

    trade_type: Optional[str] = None
    price: Optional[Decimal] = None
    timed_out: bool = False

    @property
    def entered(self) -> bool:
        return bool(self.trade_type and self.price and not self.timed_out)


@dataclass
class Position:
    """Открытая позиция глазами стратегии.

    `order` — строка `TestOrder`, которую симулятор потом превращает в
    результат сделки. `state` — рабочее состояние алгоритма на время
    удержания: у `legacy` там уровни выхода и пик цены, у следующей
    стратегии будет своё.
    """

    order: Any
    state: dict = field(default_factory=dict)


@dataclass
class ExitDecision:
    """Решение алгоритма на очередном тике удержания."""

    should_exit: bool = False
    reason: Optional[str] = None


@runtime_checkable
class Algorithm(Protocol):
    """Что обязана уметь стратегия.

    Версия объявляется здесь же и попадает в каждую сделку
    (`test_orders.algorithm_version`): по ней потом отделяют результаты
    до правки логики от результатов после.
    """

    key: str
    version: str

    async def prepare(self, context: MarketContext) -> Any:
        """Конфиг бота, приведённый к виду, в котором с ним работают.

        Возвращает конфиг, а не меняет переданный: один и тот же объект
        может использоваться дальше по циклу.
        """

    async def entry_levels(
        self, context: MarketContext, initial_price: Decimal
    ) -> EntryLevels:
        """Уровни входа от текущей цены."""

    async def wait_for_entry(
        self, context: MarketContext, levels: EntryLevels
    ) -> EntryResult:
        """Ждёт достижения уровня. Таймаут — не ошибка, а обычный исход."""

    def open_position(
        self,
        context: MarketContext,
        trade_type: str,
        entry_price: Decimal,
    ) -> Position:
        """Строит позицию и всё, что нужно для решения о выходе."""

    async def should_exit(
        self,
        context: MarketContext,
        position: Position,
        updated_price: Decimal,
        held_seconds: float,
    ) -> ExitDecision:
        """Решение на очередном тике удержания."""
