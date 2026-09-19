from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import BaseCrud
from app.db.models import AssetExchangeSpec, StrategyPair


class StrategyPairCrud(BaseCrud[StrategyPair]):
    """Наборы пар по стратегиям.

    `watched_pair` собирается как объединение этих наборов, поэтому здесь
    важно одно: правка набора одной стратегии не должна трогать чужие
    строки. Пара, нужная двум стратегиям, лежит двумя строками — иначе
    удаление её у одной лишило бы котировок другую.
    """

    def __init__(self, session: AsyncSession):
        super().__init__(session, StrategyPair)

    async def spec_ids_for(self, strategy_id: int) -> set[int]:
        result = await self.session.execute(
            select(StrategyPair.asset_exchange_id).where(
                StrategyPair.strategy_id == strategy_id
            )
        )
        return set(result.scalars().all())

    async def union_spec_ids(self) -> set[int]:
        """Пары, нужные хоть одной стратегии."""
        result = await self.session.execute(
            select(StrategyPair.asset_exchange_id).distinct()
        )
        return set(result.scalars().all())

    async def symbols_for(self, strategy_id: int) -> list[str]:
        result = await self.session.execute(
            select(AssetExchangeSpec.symbol)
            .join(
                StrategyPair,
                StrategyPair.asset_exchange_id == AssetExchangeSpec.id,
            )
            .where(StrategyPair.strategy_id == strategy_id)
        )
        return list(result.scalars().all())

    async def add(self, strategy_id: int, spec_ids) -> int:
        """Досыпает пары в набор. Возвращает, сколько появилось новых.

        Идемпотентно: повторный запуск сида не должен ни падать, ни
        заводить вторую строку на ту же пару.
        """
        spec_ids = set(spec_ids)

        if not spec_ids:
            return 0

        existing = await self.spec_ids_for(strategy_id)
        missing = spec_ids - existing

        if not missing:
            return 0

        await self.session.execute(
            insert(StrategyPair)
            .values([
                {"strategy_id": strategy_id, "asset_exchange_id": spec_id}
                for spec_id in sorted(missing)
            ])
            .on_conflict_do_nothing(
                index_elements=[
                    StrategyPair.strategy_id, StrategyPair.asset_exchange_id
                ]
            )
        )

        return len(missing)

    async def replace(self, strategy_id: int, spec_ids) -> tuple[int, int]:
        """Делает набор стратегии равным `spec_ids`. Возвращает (+, −).

        Трогает только строки этой стратегии: наборы остальных остаются
        как были, и их пары не исчезают из общего списка питателя.
        """
        spec_ids = set(spec_ids)
        existing = await self.spec_ids_for(strategy_id)

        to_remove = existing - spec_ids
        if to_remove:
            await self.session.execute(
                delete(StrategyPair).where(
                    StrategyPair.strategy_id == strategy_id,
                    StrategyPair.asset_exchange_id.in_(to_remove),
                )
            )

        added = await self.add(strategy_id, spec_ids - existing)

        return added, len(to_remove)
