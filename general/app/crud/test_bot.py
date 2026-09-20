import json

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case, distinct, update
from sqlalchemy.dialects.postgresql import insert
from datetime import datetime, timezone

from app.constants.strategy import (
    BOT_KIND_COPY_V1,
    BOT_KIND_COPY_V2,
    BOT_KIND_COPY_V3,
    BOT_KIND_ORDINARY,
    COPY_BOT_KINDS,
    DONOR_SCOPE_ALL,
)
from app.db.models import TestBot, TestOrder
from app.crud.base import BaseCrud

UTC = timezone.utc


# Колонки-маркеры: окна оценки прибыльности у копиботов каждого уровня.
# Видом бота они больше не заведуют — для этого есть `bot_kind`, — но
# остаются тем, чем были по существу: параметром отбора донора.
#
# Раньше вид выводился перебором этого списка, и забытая в нём колонка
# означала, что новый копибот попадёт в пул доноров обычных ботов:
# цепочка замыкалась в кольцо (v3 → v2 → v1 → v3), донор собирался из
# нулей, и это не падало и никак не проявлялось в логах.
COPYBOT_MARKER_COLUMNS = (
    "copy_bot_min_time_profitability_min",
    "copybot_v2_time_in_minutes",
    "copybot_v3_time_in_minutes",
)


# Поля, которыми конфигурация бота отличается от другой. Баланса здесь
# намеренно нет: у компаундирующего бота v3 он меняется от сделки к
# сделке, и по нему такой бот выглядел бы «новым» при каждом досеве.
BOT_IDENTITY_FIELDS = (
    "symbol",
    "start_updown_percents",
    "stop_loss_percents",
    "stop_win_percents",
    "min_timeframe_asset_volatility",
    "start_updown_ticks",
    "stop_loss_ticks",
    "stop_success_ticks",
    "use_trailing_stop",
    "consider_ma_for_open_order",
    "consider_ma_for_close_order",
    "ma_number_of_candles_for_open_order",
    "ma_number_of_candles_for_close_order",
    "time_to_wait_for_entry_price_to_open_order_in_seconds",
    "copy_bot_min_time_profitability_min",
    "copybot_v1_check_for_24h_profitability",
    "copybot_v1_exclude_losing_donors",
    "copybot_v2_time_in_minutes",
    "copybot_v3_time_in_minutes",
    "copybot_v3_compound_balance",
    # Настройки стратегии — часть конфигурации, а не довесок: у
    # стратегии 0 ими и отличается один бот парка от другого.
    "strategy_config",
)


def bot_identity(bot) -> tuple:
    """Ключ конфигурации: словарь сида и строка базы дают одно и то же.

    Числа сравниваются нормализованными строками, а не как есть: из
    базы numeric приходит с другим числом нулей в хвосте
    (`Decimal('0.0050')` против `Decimal('0.005')`), и прямое сравнение
    объявило бы одинаковых ботов разными — досев завёл бы дубль на
    каждый запуск.
    """
    def value(field):
        if isinstance(bot, dict):
            raw = bot.get(field)
        else:
            raw = getattr(bot, field, None)

        # «Не задано» у строки сида и у строки базы выглядит по-разному:
        # в сиде поля просто нет, а в базе на его месте стоит серверное
        # умолчание — 0 у тиков, false у флагов. Сводим к одному, иначе
        # каждый досев считал бы весь парк новым. Ноль и здесь означает
        # «не задано» — ровно так его трактуют потребители конфига.
        if raw is None or raw is False:
            return None

        if isinstance(raw, bool):
            return True

        if isinstance(raw, (int, float, Decimal)):
            number = Decimal(str(raw))
            return None if number == 0 else str(number.normalize())

        if isinstance(raw, dict):
            # Канонический вид: порядок ключей в JSONB не сохраняется, и
            # два одинаковых по смыслу конфига иначе разъехались бы.
            return json.dumps(raw, sort_keys=True, default=str)

        return raw or None

    return tuple(value(field) for field in BOT_IDENTITY_FIELDS)


def bot_kind_for_row(row: dict) -> str:
    """Вид бота по строке сида.

    Порядок проверки — от старшего к младшему: строка с несколькими
    маркерами ведёт себя как старший из них, и раскладывать её иначе,
    чем это делает симулятор, нельзя.
    """
    if row.get("copybot_v3_time_in_minutes") is not None:
        return BOT_KIND_COPY_V3

    if row.get("copybot_v2_time_in_minutes") is not None:
        return BOT_KIND_COPY_V2

    if row.get("copy_bot_min_time_profitability_min") is not None:
        return BOT_KIND_COPY_V1

    return BOT_KIND_ORDINARY


def is_copybot_row(row: dict) -> bool:
    """Строка парка описывает копибота."""
    return bot_kind_for_row(row) in COPY_BOT_KINDS


def with_strategy(rows: list[dict], strategy_id: int, donor_scope=None) -> list[dict]:
    """Проставляет строкам парка стратегию и копиботам — пул доноров.

    Сиды собирают строки из констант и про базу ничего не знают, а
    `strategy_id` выдаёт база. Поэтому стратегия проставляется одним
    местом перед вставкой, а не размножается по каждому сиду.

    Пул по умолчанию — «любая стратегия» (2026-09-20). До этого дня
    здесь стоял явный список из одной своей стратегии: пока стратегия
    была одна, `all` означал бы, что копибот сменит алгоритм сам собой
    в день подключения второй, и сравнивать его результаты до и после
    стало бы нельзя. Теперь смена сделана осознанно и датирована —
    копибот это уровень копирования, а не торговая стратегия, и пул из
    одной закрывал бы ему всё, кроме `legacy`.

    Сопоставимость от этого всё равно рвётся: сделки копиботов до
    2026-09-20 сделаны только по `legacy`, после — по алгоритму любого
    донора. Разделять их надо не по дате, а по `executed_strategy_id`
    в `test_orders` — он для того и заведён.

    Явный список по-прежнему передаётся аргументом: он нужен там, где
    исполнитель умеет ровно один алгоритм (боевой `binance_bot`).
    """
    if donor_scope is None:
        donor_scope = DONOR_SCOPE_ALL

    prepared = []

    for row in rows:
        row = {**row, "strategy_id": strategy_id}
        row.setdefault("bot_kind", bot_kind_for_row(row))

        if is_copybot_row(row):
            row.setdefault("donor_scope", donor_scope)

        prepared.append(row)

    return prepared


def active_bots_subquery(
    just_copy_bots=False,
    just_copy_bots_v2=False,
    just_copy_bots_v3=False,
    just_not_copy_bots=False,
    symbol=None,
):
    """id активных ботов нужного вида.

    Вынесено из `get_sorted_by_profit`, потому что тот же отбор нужен
    статистике по свёрткам (`TestOrderRollupCrud.profit_by_bot`). Два
    расходящихся определения «какие боты в отчёте» — это два отчёта,
    которые нельзя сравнивать между собой.
    """
    query = select(TestBot.id).where(TestBot.is_active == True)

    if just_copy_bots:
        query = query.where(TestBot.bot_kind == BOT_KIND_COPY_V1)
    elif just_copy_bots_v2:
        query = query.where(TestBot.bot_kind == BOT_KIND_COPY_V2)
    elif just_copy_bots_v3:
        query = query.where(TestBot.bot_kind == BOT_KIND_COPY_V3)
    elif just_not_copy_bots:
        query = query.where(TestBot.bot_kind == BOT_KIND_ORDINARY)

    if symbol:
        query = query.where(TestBot.symbol == symbol)

    return query


class TestBotCrud(BaseCrud[TestBot]):

    def __init__(self, session: AsyncSession):
        super().__init__(session, TestBot)

    async def get_active_bots(self, shard: int = 0, shards: int = 1):
        """Активные боты; при `shards > 1` — только доля своего шарда.

        Симулятор запускается несколькими процессами, и парк делится по
        остатку от деления id. Остаток выбран потому, что не требует ни
        координации между процессами, ни лишнего запроса: каждый бот
        достаётся ровно одному шарду, пропусков и дублей нет при любом
        наборе id. Распределение по шардам при этом равномерное — id идут
        подряд, TRUNCATE в `new_bots.py` начинает нумерацию заново.
        """
        stmt = select(TestBot).where(TestBot.is_active.is_(True))

        if shards > 1:
            stmt = stmt.where(TestBot.id % shards == shard)

        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def existing_identities(self, strategy_id: int) -> set[tuple]:
        """Ключи конфигураций, уже заведённых в парке этой стратегии."""
        result = await self.session.scalars(
            select(TestBot).where(TestBot.strategy_id == strategy_id)
        )
        return {bot_identity(bot) for bot in result.unique().all()}

    async def deactivate_strategy(self, strategy_id: int) -> int:
        """Снимает `is_active` со всех ботов стратегии. Возвращает сколько.

        Замена парка — это деактивация прежних конфигураций, а не
        удаление: сделки остаются на месте, и накопленная статистика
        переживает пересборку.
        """
        result = await self.session.execute(
            update(TestBot)
            .where(TestBot.strategy_id == strategy_id, TestBot.is_active.is_(True))
            .values(is_active=False)
        )
        return result.rowcount

    async def balance_by_bot(self, bot_ids=None) -> dict[int, float]:
        """Номинал сделки: у перечисленных ботов или у всего парка.

        `None` — весь парк, одним запросом. Так её читает воркер
        публикации доноров: пул у копиботов «любая стратегия», списки
        кандидатов у восьмидесяти копиботов почти одинаковы и длиной в
        тысячи id, и запрос на каждого означал бы восемьдесят `IN` по
        всему парку каждые тридцать секунд вместо одного чтения.
        """
        query = select(TestBot.id, TestBot.balance)

        if bot_ids is not None:
            bot_ids = list(bot_ids)

            if not bot_ids:
                return {}

            query = query.where(TestBot.id.in_(bot_ids))

        result = await self.session.execute(query)
        return {bot_id: float(balance) for bot_id, balance in result.all()}

    async def strategy_id_by_bot(self) -> dict[int, int]:
        """id бота -> id его стратегии для всего парка.

        Один запрос на цикл воркера вместо джойна в каждом отборе: парк —
        тысячи строк, а рейтинги считаются по окнам и кэшируются, и
        тащить стратегию через каждый такой запрос дороже, чем один раз
        прочитать карту.
        """
        result = await self.session.execute(
            select(TestBot.id, TestBot.strategy_id)
        )
        return dict(result.all())

    async def bulk_create(self, items: list[dict]) -> None:
        if not items:
            return

        stmt = insert(TestBot).values(items)
        await self.session.execute(stmt)

    async def get_sorted_by_profit(
        self, since=None,
        just_copy_bots=False, just_copy_bots_v2=False,
        just_copy_bots_v3=False, just_not_copy_bots=False,
        symbol=None,
        by_referral_bot_id=False,
    ):
        active_bots = active_bots_subquery(
            just_copy_bots=just_copy_bots,
            just_copy_bots_v2=just_copy_bots_v2,
            just_copy_bots_v3=just_copy_bots_v3,
            just_not_copy_bots=just_not_copy_bots,
            symbol=symbol,
        )

        select_columns = [
            TestOrder.bot_id,
            func.coalesce(func.sum(TestOrder.profit_loss), None).label(
                "total_profit"
            ),
            func.count(TestOrder.id).label("total_orders"),
            func.sum(case((TestOrder.profit_loss > 0, 1), else_=0)).label(
                "successful_orders"
            ),
        ]

        if symbol:
            select_columns.append(func.array_agg(TestOrder.asset_symbol.distinct()).label("symbol"))

        if by_referral_bot_id:
            select_columns[0] = TestOrder.referral_bot_id

        profits_query = select(*select_columns).where(
            TestOrder.bot_id.in_(active_bots)
        )

        if by_referral_bot_id:
            profits_query = select(*select_columns).where(
                TestOrder.referral_bot_id.in_(active_bots)
            )

        if since is not None:
            now = datetime.now(UTC)
            time_ago = now - since

            profits_query = profits_query.where(
                TestOrder.created_at >= time_ago
            )

        if by_referral_bot_id:
            profits_query = profits_query.group_by(TestOrder.referral_bot_id)
        else:
            profits_query = profits_query.group_by(TestOrder.bot_id)

        profits_data = (await self.session.execute(profits_query)).all()

        return profits_data

    async def get_unique_copy_bot_min_time_profitability(self) -> list:
        stmt = select(
            distinct(TestBot.copy_bot_min_time_profitability_min)
        ).where(TestBot.copy_bot_min_time_profitability_min.is_not(None))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_copybots(self):
        stmt = select(TestBot).where(TestBot.bot_kind == BOT_KIND_COPY_V1)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_copybots_v3(self, compound: bool = None):
        """Боты v3, опционально только компаундирующие.

        Нужен и симулятору (через него бот узнаёт свой стартовый баланс после
        перезапуска), и отчёту: тот показывает обе кривые рядом.
        """
        stmt = select(TestBot).where(TestBot.bot_kind == BOT_KIND_COPY_V3)

        if compound is not None:
            stmt = stmt.where(
                TestBot.copybot_v3_compound_balance.is_(compound)
            )

        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def set_balance(self, bot_id: int, balance) -> None:
        """Текущий баланс компаундирующего бота v3.

        Пишется после каждой сделки, потому что иначе кривая счёта не
        переживёт перезапуск симулятора. Прочитать своё состояние из
        `test_orders` бот не может: сделки уезжают в очередь Redis и
        вставляются пачками, то есть с задержкой.
        """
        await self.session.execute(
            update(TestBot).where(TestBot.id == bot_id).values(balance=balance)
        )
        await self.session.commit()

    async def mark_v3_stopped(self, bot_id: int, stopped_at=None) -> None:
        """Отметить, что на балансе перестал набираться минимальный лот.

        `is_active` намеренно остаётся `true`: `active_bots_subquery` отбирает
        только активных, и снятие флага убрало бы бота из всех отчётов — ровно
        там, где факт остановки важнее всего.
        """
        await self.session.execute(
            update(TestBot)
            .where(TestBot.id == bot_id)
            .values(copybot_v3_stopped_at=stopped_at or datetime.now(UTC))
        )
        await self.session.commit()

    async def get_bot_with_volatility_by_id(self, bot_id: int):
        stmt = select(TestBot).where(
            TestBot.id == bot_id,
            TestBot.min_timeframe_asset_volatility.is_not(None),
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_bot_by_id(self, bot_id: int):
        stmt = select(TestBot).where(
            TestBot.id == bot_id,
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_unique_min_timeframe_volatility_values(self):
        stmt = select(distinct(TestBot.min_timeframe_asset_volatility)).where(
            TestBot.min_timeframe_asset_volatility.is_not(None)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_bot_symbols(self):
        stmt = select(distinct(TestBot.symbol)).where(
            TestBot.is_active.is_(True)
        )
        result = await self.session.execute(stmt)
        result = result.scalars().all()
        result = [elem for elem in result if elem]
        return result

    async def deactivate_bot(self, symbol):
        result = await self.session.execute(
            select(TestBot.id)
            .where(
                TestBot.symbol == symbol,
                TestBot.is_active == True
            )
        )

        bot_ids = [row[0] for row in result]

        if not bot_ids:
            print(f"Нет активных ботов для символа '{symbol}'.")
            return

        BATCH_SIZE = 100

        batches = [
            bot_ids[i:i + BATCH_SIZE]
            for i in range(0, len(bot_ids), BATCH_SIZE)
        ]

        for batch_of_ids in batches:
            await self.session.execute(
                update(TestBot)
                .where(TestBot.id.in_(batch_of_ids))
                .values(is_active=False)
            )
            await self.session.commit()