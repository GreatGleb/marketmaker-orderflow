from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.strategy import STRATEGY_LEGACY
from app.crud.base import BaseCrud
from app.db.models import Strategy


class UnknownStrategy(LookupError):
    """Ключа стратегии нет в `strategies`.

    Бот такой стратегии запускать нельзя: провести его по текущему
    алгоритму «раз уж всё равно похоже» — значит записать в историю
    сделки, которых этот алгоритм не совершал.
    """


class StrategyCrud(BaseCrud[Strategy]):
    """Реестр стратегий.

    Числовой `id` выдаёт база и в каждом развёртывании он свой, поэтому
    снаружи стратегия зовётся ключом (`legacy`), а `id` живёт только
    внутри строк таблиц. Перевод между ними — здесь.
    """

    def __init__(self, session: AsyncSession):
        super().__init__(session, Strategy)

    async def get_by_key(self, key: str) -> Strategy | None:
        result = await self.session.execute(
            select(Strategy).where(Strategy.key == key)
        )
        return result.scalar_one_or_none()

    async def id_by_key(self, key: str) -> int:
        strategy = await self.get_by_key(key)

        if strategy is None:
            raise UnknownStrategy(f"Стратегия {key!r} не зарегистрирована")

        return strategy.id

    async def ids_by_keys(self, keys) -> list[int]:
        """id перечисленных стратегий.

        Неизвестный ключ — ошибка, а не пропуск: молча выкинутый ключ
        превратил бы «доноры из legacy и strategy_0» в «доноры из
        legacy», и отбор поехал бы без единого следа в логах.
        """
        keys = list(keys)

        if not keys:
            return []

        result = await self.session.execute(
            select(Strategy.key, Strategy.id).where(Strategy.key.in_(keys))
        )
        found = dict(result.all())

        missing = [key for key in keys if key not in found]
        if missing:
            raise UnknownStrategy(
                "Стратегии не зарегистрированы: " + ", ".join(sorted(missing))
            )

        return [found[key] for key in keys]

    async def keys_by_id(self) -> dict[int, str]:
        """id -> ключ для всех стратегий.

        Симулятор читает карту один раз на старте: сделки пишутся в
        очередь сотнями в секунду, и ходить за ключом стратегии в базу на
        каждую из них незачем.
        """
        result = await self.session.execute(select(Strategy.id, Strategy.key))
        return dict(result.all())

    async def ids_by_key(self) -> dict[str, int]:
        """ключ -> id для всех стратегий."""
        result = await self.session.execute(select(Strategy.key, Strategy.id))
        return dict(result.all())

    async def ensure(self, key: str, title: str) -> int:
        """Регистрирует стратегию, если её ещё нет. Возвращает id.

        Идемпотентно: сиды запускают повторно, и второй запуск не должен
        ни падать, ни заводить дубль ключа.
        """
        await self.session.execute(
            insert(Strategy)
            .values(key=key, title=title)
            .on_conflict_do_nothing(index_elements=[Strategy.key])
        )

        return await self.id_by_key(key)

    async def ensure_legacy(self) -> int:
        return await self.ensure(STRATEGY_LEGACY, "Текущий алгоритм")
