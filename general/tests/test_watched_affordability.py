"""Доступность MARKET-покупки: assert и заглушки, без БД, сети и торговли."""
import asyncio
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from contextlib import asynccontextmanager
import time

from app.services import watched_affordability as wa
from app.scripts import seed_watched_pairs as sw
from app.crud.exchange_pair_spec import AssetExchangeSpecCrud


def spec(minimum='0.001', step='0.001', notional='5', maximum='1000000'):
    return dict(
        contractType='PERPETUAL', quoteAsset='USDT', status='TRADING',
        orderTypes=['MARKET'], filters=[
            dict(filterType=kind, minQty=minimum, maxQty=maximum, stepSize=step)
            for kind in ('LOT_SIZE', 'MARKET_LOT_SIZE')
        ] + [dict(filterType='MIN_NOTIONAL', notional=notional)],
    )


def check(s=None, price='3000', mark=None, fx=None):
    now = time.time()
    return wa.check_purchase(
        s if s is not None else spec(),
        dict(askPrice=price, time=str(now * 1000)),
        dict(markPrice=price if mark is None else mark, time=str(now * 1000)),
        fx, now, Decimal('9'),
    )


def check_arithmetic():
    assert check()[0] == Decimal('0.002')  # Дорогая монета, покупка на 6.
    assert check(spec(notional='10'), price='0.001')[0] is None
    assert check(spec(minimum='1', step='1'), price='4.6')[0] is None
    assert check(spec(notional='9'))[0] == Decimal('0.003')
    assert check(spec(notional='9'), price='3000.00000001')[0] is None
    assert check(spec(maximum='0.001'))[0] is None
    assert check(price='3000', mark='1000')[0] is None  # MIN_NOTIONAL по mark.
    offset = spec(minimum='0.002', step='0.003', notional='5')
    assert check(offset, price='1000')[0] == Decimal('0.005')
    different = spec(minimum='0.001', step='0.001')
    different['filters'][1].update(minQty='0.002', stepSize='0.002')
    assert check(different)[0] == Decimal('0.002')
    for value in (None, '', 'bad', 'NaN', 'Infinity', '0', '-1'):
        assert check(price=value)[0] is None, value
        if value is not None:
            assert check(mark=value)[0] is None, value
        for kind in (0, 1):
            for key in ('minQty', 'maxQty', 'stepSize'):
                broken = spec()
                broken['filters'][kind][key] = value
                assert check(broken)[0] is None, (key, value)
        assert check(spec(notional=value))[0] is None
    for key in ('filters', 'status', 'orderTypes', 'quoteAsset'):
        broken = spec()
        del broken[key]
        assert check(broken)[0] is None
    for index in range(3):
        broken = spec()
        del broken['filters'][index]
        assert check(broken)[0] is None
    usdc = spec(notional='9')
    usdc['quoteAsset'] = 'USDC'
    assert check(usdc)[0] is None
    assert check(usdc, fx=dict(askPrice='1.01', price='0.99', closeTime=time.time()*1000))[0] is None
    assert check(usdc, fx=dict(askPrice='1', closeTime=time.time()*1000))[0] == Decimal('.003')


def snapshot():
    now = time.time()
    return wa.Snapshot(
        {'GOOD': spec(), 'BAD': spec(notional='10')},
        {s: dict(askPrice='3000', time=now*1000) for s in ('GOOD', 'BAD')},
        {s: dict(markPrice='3000', time=now*1000) for s in ('GOOD', 'BAD')},
        {}, now,
    )


def check_freshness():
    snap = snapshot()
    assert snap.keep(['BAD', 'GOOD', 'MISSING']) == ['GOOD']
    for field in ('books', 'marks'):
        bad = deepcopy(snap)
        getattr(bad, field)['GOOD']['time'] -= 61000
        assert bad.keep(['GOOD']) == []
    snap.received_at -= 61
    assert snap.keep(['GOOD']) == []


class Result:
    def __init__(self, rows):
        self.rows = rows
    def all(self):
        return self.rows
    def scalars(self):
        return self
    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None
    def first(self):
        return self.rows[0] if self.rows else None


SPEC_IDS = {'GOOD': 1, 'BAD': 2}


class Session:
    """Справочник, набор пар стратегии и watched_pair в памяти.

    Отвечает по смыслу запроса, а не по порядку вызовов: реализация
    ходит в базу несколько раз и порядок меняется от правки к правке, а
    проверяем мы здесь другое — что недоступная для покупки пара не
    попадает ни в набор стратегии, ни в общий список.
    """

    def __init__(self, replace):
        self.info = {'watched_affordability': snapshot()}
        # До прогона в наборе и в списке лежит 'BAD' — пара, покупка
        # которой не проходит по бюджету. Прогон обязан её убрать.
        self.strategy_pairs = {SPEC_IDS['BAD']}
        self.watched = {SPEC_IDS['BAD']}
        self.replace = replace
        self.deleted = False
        self.added = []
        self.committed = False

    async def execute(self, query):
        if query.is_delete:
            ids = [
                value for value in query.compile().params.values()
                if isinstance(value, list)
            ]
            removed = set(ids[0]) if ids else set()

            if query.table.name == 'watched_pair':
                self.deleted = True
                self.watched -= removed
            else:
                self.strategy_pairs -= removed

            return Result([])

        if query.is_insert:
            for row in query.compile().params.values():
                if isinstance(row, int):
                    self.strategy_pairs.add(row)
            for values in getattr(query, '_values', ()) or ():
                pass
            return Result([])

        columns = [column.name for column in query.selected_columns]
        table = query.get_final_froms()[0].name

        if table == 'strategies':
            return Result([SimpleNamespace(
                id=1, key='legacy', pair_policy='volatility_jumps',
            )])

        if table == 'asset_exchange_specs':
            if columns[:3] == ['symbol', 'quote_asset', 'contract_type']:
                return Result([
                    SimpleNamespace(
                        symbol=symbol, quote_asset='USDT',
                        contract_type='PERPETUAL', status='TRADING',
                    )
                    for symbol in SPEC_IDS
                ])
            if columns == ['id', 'symbol']:
                return Result([
                    (spec_id, symbol)
                    for symbol, spec_id in SPEC_IDS.items()
                ])
            if columns == ['symbol']:
                by_id = {spec_id: symbol for symbol, spec_id in SPEC_IDS.items()}
                return Result([
                    by_id[spec_id] for spec_id in sorted(self.strategy_pairs)
                ])

        if table == 'strategy_pairs':
            return Result(sorted(self.strategy_pairs))

        if table == 'watched_pair':
            return Result(sorted(self.watched))

        raise AssertionError(f'неожиданный запрос: {table}, {columns}')

    def add(self, row):
        self.added.append(row.asset_exchange_id)
        self.watched.add(row.asset_exchange_id)

    async def commit(self):
        self.committed = True


async def check_paths():
    # Закрепление и защита короткого списка не обходят бюджет.
    for replace in (True, False):
        session = Session(replace)
        with patch.object(sw, 'get_pinned_symbols', AsyncMock(return_value=['BAD'])):
            assert await sw.apply_watched_pairs(
                session, ['GOOD', 'BAD'], replace, strategy_id=1
            ) == (1, 1)
        assert session.deleted and session.committed and session.added == [1]
        # Недоступная пара не осталась ни в общем списке, ни в наборе
        # стратегии — при replace её оттуда убирают, при пополнении она
        # туда просто не попадает.
        assert session.watched == {SPEC_IDS['GOOD']}, session.watched
        assert SPEC_IDS['GOOD'] in session.strategy_pairs
    # Снимок переиспользуется; протухший обновляется одним общим вызовом.
    session = SimpleNamespace(info={})
    with patch.object(wa, 'load_snapshot', AsyncMock(side_effect=lambda: snapshot())) as load:
        assert await wa.affordable_symbols(session, ['GOOD']) == ['GOOD']
        assert await wa.affordable_symbols(session, ['BAD']) == []
        assert load.await_count == 1
        session.info['watched_affordability'].received_at -= 61
        assert await wa.affordable_symbols(session, ['GOOD']) == ['GOOD']
        assert load.await_count == 2
    # Seed обновляет старые фильтры и не трогает ставки комиссии.
    row = SimpleNamespace(filters=[], maker_commission_rate=Decimal('.001'))
    session = SimpleNamespace(execute=AsyncMock(return_value=Result([row])))
    updated, created = await AssetExchangeSpecCrud(session).get_or_create(
        dict(asset_pairs_id=1, filters=spec()['filters'], status='TRADING')
    )
    assert not created and updated.filters == spec()['filters']
    assert updated.maker_commission_rate == Decimal('.001')


async def check_rank_and_empty_rebuild():
    from tests.test_tradable_pairs import FakeHttpx
    tickers = [dict(symbol=s, quoteVolume='9000000', highPrice=h, lowPrice='1')
               for s, h in [('BAD', '3'), ('GOOD', '2')]]
    session = SimpleNamespace(info={'watched_affordability': snapshot()})
    with patch.object(sw, 'httpx', FakeHttpx(tickers)):
        assert await sw.rank_from_binance(session, 1, ({'BAD', 'GOOD'}, {'BAD', 'GOOD'})) == ['GOOD']
    rows = [SimpleNamespace(symbol=s, jumps=3, jumps_sum=4, max_jump=2)
            for s in ('BAD', 'GOOD')]
    crud = SimpleNamespace(get_top_jumpy_symbols=AsyncMock(return_value=rows))
    with patch.object(sw, 'AssetHistoryCrud', return_value=crud), \
         patch.object(sw, 'tradable_symbols', AsyncMock(return_value={'GOOD', 'BAD'})):
        assert await sw.rank_by_jumps(session, 24, 1, .5) == ['GOOD']
        assert crud.get_top_jumpy_symbols.call_args.kwargs['limit'] is None

    @asynccontextmanager
    async def context():
        yield session

    # Финальный отбор разгона тоже фильтрует до top и до добора кандидатов.
    with patch.object(sw.DatabaseSessionManager, 'create', return_value=SimpleNamespace(get_session=context)), \
         patch.object(sw, 'AssetHistoryCrud', return_value=crud):
        assert await sw.watch_and_rank(['BAD', 'GOOD'], 1, 0, .5) == ['GOOD']
    with patch.object(sw, 'rank_from_binance', AsyncMock(return_value=['GOOD'])), \
         patch.object(sw, 'apply_watched_pairs', AsyncMock(return_value=(1, 0))) as apply, \
         patch.object(sw, 'restart_feeders'), \
         patch.object(sw, 'watch_and_rank', AsyncMock(return_value=['GOOD'])):
        assert await sw.bootstrap(session, 1, 2, 0, .5, strategy_id=1) == ['GOOD']
        # Кандидаты разгона попадают в набор той стратегии, ради которой
        # он затеян, а не в чужой.
        apply.assert_awaited_once_with(
            session, ['GOOD'], replace=True, strategy_id=1
        )

    # Та же функция вызывается суточным воркером. Пустой отбор не мешает
    # удалить недоступные старые пары и перезапустить подписки.
    with patch.object(sw, 'paused_simulator', context), \
         patch.object(sw.DatabaseSessionManager, 'create', return_value=SimpleNamespace(get_session=context)), \
         patch.object(sw, 'rank_by_jumps', AsyncMock(return_value=[])), \
         patch.object(sw, 'bootstrap', AsyncMock(return_value=None)), \
         patch.object(sw, 'apply_watched_pairs', AsyncMock(return_value=(0, 1))) as apply, \
         patch.object(sw, 'restart_feeders') as restart:
        session.execute = AsyncMock(return_value=Result([SimpleNamespace(
            id=1, key='legacy', pair_policy='volatility_jumps',
        )]))
        await sw.seed_watched_pairs(replace=True)
        apply.assert_awaited_once_with(session, [], False, strategy_id=1)
        restart.assert_called_once()


async def check_bulk_loading():
    now = time.time()
    response = lambda payload: SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
    calls = []

    class Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url):
            calls.append(url)
            if 'bookTicker' in url:
                return response([dict(symbol='GOOD', askPrice='3000', time=now*1000)])
            if 'premiumIndex' in url:
                return response([dict(symbol='GOOD', markPrice='3000', time=now*1000)])
            return response(dict(askPrice='1', closeTime=now*1000))

    data = {'symbols': [dict(symbol='GOOD', **spec())]}
    with patch.object(wa.httpx, 'AsyncClient', return_value=Client()), \
         patch.object(wa, 'fetch_binance_data', AsyncMock(return_value=data)) as fetch, \
         patch.object(wa, 'seed_binance_data', AsyncMock()) as seed:
        loaded = await wa.load_snapshot()
        assert loaded.keep(['GOOD']) == ['GOOD']
        fetch.assert_awaited_once()
        seed.assert_awaited_once_with(data=data)
        assert len(calls) == 3
    for malformed in (None, {}, {'symbols': {}}, {'symbols': [None]},
                      {'symbols': [{'symbol': 'GOOD'}, {'symbol': 'GOOD'}]}):
        with patch.object(wa.httpx, 'AsyncClient', return_value=Client()), \
             patch.object(wa, 'fetch_binance_data', AsyncMock(return_value=malformed)), \
             patch.object(wa, 'seed_binance_data', AsyncMock()) as seed:
            assert (await wa.load_snapshot()).keep(['GOOD']) == []
            seed.assert_not_awaited()
    with patch.object(wa.httpx, 'AsyncClient', return_value=Client()), \
         patch.object(wa, 'fetch_binance_data', AsyncMock(side_effect=OSError('нет сети'))), \
         patch.object(wa, 'seed_binance_data', AsyncMock()) as seed:
        assert (await wa.load_snapshot()).keep(['GOOD']) == []
        seed.assert_not_awaited()


def check_quantity_against_enumeration():
    # Независимый эталон: перебираем количества, а не повторяем алгоритм CRT.
    from itertools import product
    count = 0
    for lo1, lo2, step1, step2, price in product(range(1, 5), repeat=5):
        s = spec(minimum=str(lo1), step=str(step1), maximum='20', notional='5')
        s['filters'][1].update(minQty=str(lo2), stepSize=str(step2))
        possible = [Decimal(q) for q in range(max(lo1, lo2), 21)
                    if (q-lo1) % step1 == 0 and (q-lo2) % step2 == 0
                    and 5 <= q*price <= 9]
        actual, reason = check(s, price=str(price))
        assert actual == (min(possible) if possible else None), (s, price, actual, reason)
        count += 1
    print(f'  Сверка с полным перебором: {count} комбинаций')


async def main():
    check_arithmetic()
    check_quantity_against_enumeration()
    check_freshness()
    await check_paths()
    await check_rank_and_empty_rebuild()
    await check_bulk_loading()
    print('✅ доступность покупки, свежесть, обновление спецификаций и запись списка')


if __name__ == '__main__':
    asyncio.run(main())
