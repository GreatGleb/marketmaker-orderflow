"""Регрессии финального аудита: номинал, оба лота и цена самого входа."""
import asyncio
from decimal import Decimal as D
from unittest.mock import AsyncMock, patch

import app.bots.demo_test_bot as m
from app.crud.exchange_pair_spec import AssetExchangeSpecCrud as Specs
from app.sub_services.logic.market_setup import MarketDataBuilder
from tests.test_copybot_v3_simulation import (
    FakeCrud, FakeRedis, FakeSessionManager, MARKET, SYMBOL, V3_BOT,
)

S = m.StartTestBotsCommand
# Сохранённые фильтры BMTUSDT из финальной БД, 2026-09-18.
BMT_FILTERS = [
    {"filterType": "PRICE_FILTER", "minPrice": "0.0001000", "maxPrice": "200", "tickSize": "0.0000100"},
    {"filterType": "LOT_SIZE", "minQty": "1", "maxQty": "40000000", "stepSize": "1"},
    {"filterType": "MARKET_LOT_SIZE", "minQty": "1", "maxQty": "4000000", "stepSize": "1"},
    {"filterType": "MIN_NOTIONAL", "notional": "5"},
]


def check_limits():
    market = Specs.extract_step_sizes(BMT_FILTERS)
    for balance in (5, 4, 1):
        q, n, refusal = S.compound_order_size(balance, D('.01894'), market)
        assert q is n is None and refusal.is_shortage
    q, n, refusal = S.compound_order_size(10, D('.01894'), market)
    assert refusal is None and q == 522 and n == D('9.88668')

    # Граница номинала после округления: бюджет достаточен, но шаг режет вниз.
    boundary = dict(market, min_notional=D('9.89'))
    assert S.compound_order_size(10, D('.01894'), boundary)[2].is_shortage
    boundary['min_notional'] = D('9.88668')
    assert S.compound_order_size(10, D('.01894'), boundary)[2] is None

    # Сохранённые разные максимумы AVAX; около границы ошибки float видны сразу.
    avax = dict(market, max_qty=D(500000), market_max_qty=D(50000))
    assert S.compound_order_size(D(50001) / D('.99'), D(1), avax)[2].kind == 'pair'
    assert S.compound_order_size(D(50000) / D('.99'), D(1), avax)[2] is None

    # Две сетки, включая смещённые minQty. Нельзя просто взять больший шаг.
    grids = dict(market, min_qty=D('.2'), step_size=D('.2'),
                 market_min_qty=D('.3'), market_step_size=D('.3'), min_notional=D('.1'))
    assert S.compound_order_size(10, 1, grids)[:2] == (D('9.6'), D('9.6'))
    grids.update(min_qty=D('.1'), market_min_qty=D('.1'))
    assert S.compound_order_size(10, 1, grids)[0] == D('9.7')
    grids.update(market_step_size=D(0), market_min_qty=D('9.8'))
    assert S.compound_order_size(10, 1, grids)[0] == D('9.9')
    grids['market_min_qty'] = D(10)
    assert S.compound_order_size(10, 1, grids)[2].is_shortage

    for key in ('step_size', 'market_step_size', 'min_qty', 'market_min_qty',
                'max_qty', 'market_max_qty', 'min_notional'):
        for value in (None, 'NaN', 'Infinity', '-1', 'broken'):
            assert S.compound_order_size(10, 1, dict(market, **{key: value}))[2].kind == 'data', (key, value)
    for value in ('NaN', 'Infinity', None):
        assert S.compound_order_size(value, 1, market)[2].kind == 'data'
        assert S.compound_order_size(10, value, market)[2].kind == 'data'
    assert S.compound_order_size(0, 1, market)[2].is_shortage
    assert S.compound_order_size(-1, 1, market)[2].is_shortage
    assert S.compound_order_size(10, 0, market)[2].kind == 'data'
    broken = [*BMT_FILTERS[:-1], {'filterType': 'MIN_NOTIONAL', 'notional': 'NaN'}]
    assert Specs.extract_step_sizes(broken)['min_notional'] is None

    precise = [dict(f) for f in BMT_FILTERS]
    precise[1]['stepSize'] = '0.123456789012345678901'
    assert Specs.extract_step_sizes(precise)['step_size'] == D('0.123456789012345678901')
    print('  BMT, AVAX, границы номинала, пересечение сеток и некорректные данные')


def check_grids_against_enumeration():
    # Независимый перебор маленьких целых сеток, без повторения алгоритма CRT.
    for a in range(1, 5):
        for b in range(1, 5):
            for step_a in range(1, 5):
                for step_b in range(1, 5):
                    valid = [q for q in range(max(a, b), 100)
                             if (q - a) % step_a == 0 and (q - b) % step_b == 0]
                    market = dict(MARKET, min_qty=D(a), market_min_qty=D(b),
                                  step_size=D(step_a), market_step_size=D(step_b),
                                  min_notional=D('.1'))
                    q, n, refusal = S.compound_order_size(100, 1, market)
                    if valid:
                        assert refusal is None and q == max(valid), (market, q, refusal)
                        assert n <= 99
                    else:
                        assert refusal and refusal.kind == 'data'
    print('  256 сочетаний сеток сверены с независимым перебором')


async def check_both_data_paths():
    raw = Specs(None)._build_market_data(BMT_FILTERS, None, None)
    builder = MarketDataBuilder(None)
    builder.asset_crud = AsyncMock()
    builder.asset_crud.get_all_active_pairs.return_value = [SYMBOL]
    builder.exchange_crud = AsyncMock()
    builder.exchange_crud.get_market_data_by_symbols.return_value = {SYMBOL: raw}
    startup = await builder.build()
    with patch.object(m, 'DatabaseSessionManager', FakeSessionManager), \
            patch.object(Specs, 'get_market_data_by_symbol', AsyncMock(return_value=raw)):
        lazy = await S.get_market_data(SYMBOL, {})
    assert lazy == startup[SYMBOL]
    assert lazy['market_max_qty'] == 4000000 and lazy['min_notional'] == 5
    print('  ограничения доходят и при старте, и при догрузке пары')


async def check_signal_price_before_open():
    redis, stop = FakeRedis(), asyncio.Event()
    provider = AsyncMock()
    provider.get_price.return_value = D(100)
    provider._read_price.return_value = D(101)
    command = S(stop_event=stop)

    async def signal(*args, **kwargs):
        return m.TradeType.BUY.value, D(101)

    # До сигнала 9.9*100=990 допустимо; по сигналу 9.8*101=989.8 уже нет.
    with patch.object(m, 'DatabaseSessionManager', FakeSessionManager), \
            patch.object(m, 'TestBotCrud', FakeCrud), \
            patch.object(m.PriceWatcher, 'wait_for_entry_price', signal), \
            patch.object(m.asyncio, 'sleep', AsyncMock()), \
            patch.object(m.PriceCalculator, 'calculate_close_not_lose_price') as opened:
        await command.simulate_bot(redis, V3_BOT, {SYMBOL: dict(MARKET, min_notional=D(990))}, stop, provider, None)
    assert not opened.called and not redis.pushed
    assert command._v3_shortages[V3_BOT.id] == 1
    assert provider.get_price.await_count == 1, 'отказ должен произойти до удержания/закрытия'
    print('  сигнал с недопустимым объёмом отклонён до открытия')


async def check_notional_shortage_stop():
    _, _, shortage = S.compound_order_size(5, D('.01894'), Specs.extract_step_sizes(BMT_FILTERS))
    command = S(stop_event=asyncio.Event())
    FakeCrud.stopped.clear()
    with patch.object(m, 'DatabaseSessionManager', FakeSessionManager), \
            patch.object(m, 'TestBotCrud', FakeCrud):
        for _ in range(9):
            await command._v3_note_shortage(V3_BOT.id, shortage)
        assert not command._v3_is_stopped(V3_BOT.id)
        for kind in ('pair', 'data'):
            await command._v3_note_shortage(V3_BOT.id, m.Refusal(kind, 'пропуск'))
        assert command._v3_shortages[V3_BOT.id] == 9
        await command._v3_note_shortage(V3_BOT.id, shortage)
        assert command._v3_is_stopped(V3_BOT.id)
        assert FakeCrud.stopped == [V3_BOT.id]
    print('  нехватка номинала останавливает v3 на десятой попытке; pair/data счётчик не увеличивают')


async def main():
    check_limits()
    check_grids_against_enumeration()
    await check_both_data_paths()
    await check_signal_price_before_open()
    await check_notional_shortage_stop()


if __name__ == '__main__':
    asyncio.run(main())
