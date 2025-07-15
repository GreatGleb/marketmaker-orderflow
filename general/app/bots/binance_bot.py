import asyncio
import enum
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import distinct, select
import json
from dotenv import load_dotenv
import os
import time
import math

from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException

from app.crud.asset_history import AssetHistoryCrud
from app.crud.exchange_pair_spec import AssetExchangeSpecCrud
from app.crud.test_bot import TestBotCrud
from app.crud.test_orders import TestOrderCrud
from app.db.base import DatabaseSessionManager
from app.config import settings
from app.db.models import TestBot, TestOrder
from app.dependencies import redis_context
from app.sub_services.watchers.user_data_websocket_client import UserDataWebSocketClient

load_dotenv()
UTC = timezone.utc
COMMISSION_OPEN  = Decimal("0.0005")# 0.0002
COMMISSION_CLOSE = Decimal("0.0005")


class TradeType(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"

async def get_price_from_redis(redis, symbol: str) -> Decimal:
    while True:
        try:
            price_str = await redis.get(f"price:{symbol}")
            if price_str is not None:
                return Decimal(price_str)
        except Exception as e:
            print(f"❌ Redis error: {e}")
        await asyncio.sleep(0.1)

async def _wait_for_entry_price(redis_conn, symbol, entry_price_buy, entry_price_sell):
    while True:
        current_price = await get_price_from_redis(redis_conn, symbol)

        if current_price >= entry_price_buy:
            return (TradeType.BUY, current_price)
        elif current_price <= entry_price_sell:
            return (TradeType.SELL, current_price)

        await asyncio.sleep(0.1)

def calculate_take_profit_price(bot_config, tick_size, open_price, trade_type):
    desired_net_profit_value = Decimal(bot_config.stop_success_ticks) * tick_size

    if trade_type == TradeType.BUY:
        commission_open_cost = 1 + COMMISSION_OPEN
        commission_close_cost = 1 - COMMISSION_CLOSE
        base_take_profit = open_price * commission_open_cost - desired_net_profit_value
        take_profit_price = base_take_profit / commission_close_cost
    else:
        commission_open_cost = 1 - COMMISSION_OPEN
        commission_close_cost = 1 + COMMISSION_CLOSE
        base_take_profit = open_price * commission_open_cost + desired_net_profit_value
        take_profit_price = base_take_profit / commission_close_cost

    take_profit_price = take_profit_price.quantize(tick_size, rounding=ROUND_HALF_UP)

    return take_profit_price

def calculate_stop_lose_price(bot_config, tick_size, open_price, trade_type):
    stop_loss_price = (
        open_price - bot_config.stop_loss_ticks * tick_size
        if trade_type == TradeType.BUY
        else open_price + bot_config.stop_loss_ticks * tick_size
    )

    return stop_loss_price

def calculate_close_not_lose_price(open_price, trade_type):
    if trade_type == TradeType.BUY:
        commission_open_cost = 1 + COMMISSION_OPEN
        commission_close_cost = 1 - COMMISSION_CLOSE
    else:
        commission_open_cost = 1 - COMMISSION_OPEN
        commission_close_cost = 1 + COMMISSION_CLOSE

    close_not_lose_price = (open_price * commission_open_cost) / commission_close_cost

    return close_not_lose_price

async def get_copy_bot_tf_params(session):
    copy_bot_min_time_profitability_min = 180

    bot_crud = TestBotCrud(session)
    profits_data = await bot_crud.get_sorted_by_profit(since=timedelta(hours=1),just_copy_bots=True)
    profits_data_filtered_sorted = sorted([item for item in profits_data if item[1] > 0], key=lambda x: x[1], reverse=True)

    refer_bot_id = None

    try:
        refer_bot_id = profits_data_filtered_sorted[0][0]
    except (IndexError, TypeError):
        pass

    if refer_bot_id:
        refer_bot = await session.execute(
            select(TestBot)
            .where(
                TestBot.id == refer_bot_id,
            )
        )
        refer_bot = refer_bot.scalars().all()
        if refer_bot:
            refer_bot = refer_bot[0]
            copy_bot_min_time_profitability_min = float(refer_bot.copy_bot_min_time_profitability_min)

    return copy_bot_min_time_profitability_min

def get_precision_by_tick_size(tick_size):
    precision = int(round(-math.log10(tick_size), 0))
    return precision

def calculate_quantity_for_order(amount: Decimal, price: Decimal, lot_size: Decimal):
    raw_quantity = amount / price
    return round_price_for_order(raw_quantity, lot_size)

def round_price_for_order(price: Decimal, tick_size: Decimal):
    precision = get_precision_by_tick_size(tick_size)
    rounded_price = f"{price:.{precision}f}"
    return rounded_price

async def create_order(
    client, balanceUSDT, balanceUSDT099, bot_config,
    symbol, tick_size, lot_size, max_price, min_price, max_qty, min_qty, order_type,
    futures_order_type, order_side, order_position_side, order_quantity, order_stop_price, order_id,
    tryCreateOrder, buy_order_id, sell_order_id
):
    tryCreateOrder = tryCreateOrder + 1

    if tryCreateOrder > 10:
        print('Too much tries when stop price ')
        return None

    try:
        order = await safe_from_time_err_call_binance(
            client.futures_create_order,
            symbol=symbol,
            side=order_side,
            positionSide=order_position_side,
            type=futures_order_type,
            quantity=order_quantity,
            stopPrice=order_stop_price,
            newClientOrderId=order_id,
            workingType="MARK_PRICE",
            priceProtect=True,
            newOrderRespType="RESULT",
            recvWindow=3000,
            tryCreateOrder=tryCreateOrder,
            buy_order_id=buy_order_id,
            sell_order_id=sell_order_id
        )

        return order
    except BinanceAPIException as e:
        if e.code == -2021:
            return await create_orders(
                client=client,
                balanceUSDT=balanceUSDT,
                balanceUSDT099=balanceUSDT099,
                bot_config=bot_config,
                symbol=symbol,
                tick_size=tick_size,
                lot_size=lot_size,
                max_price=max_price,
                min_price=min_price,
                max_qty=max_qty,
                min_qty=min_qty,
                type_order=order_type,
                tryCreateOrder=tryCreateOrder,
                buy_order_id=buy_order_id,
                sell_order_id=sell_order_id
            )
        else:
            raise

async def create_orders(
    client, balanceUSDT, balanceUSDT099, bot_config,
    symbol, tick_size, lot_size, max_price, min_price, max_qty, min_qty, type_order,
    buy_order_id=None, sell_order_id=None,
    tryCreateOrder=0,
):
    futures_mark_price = await safe_from_time_err_call_binance(
        client.futures_mark_price,
        symbol=symbol
    )
    initial_price = Decimal(futures_mark_price['markPrice'])

    entry_price_buy = initial_price + bot_config.start_updown_ticks * tick_size
    entry_price_buy_str = round_price_for_order(price=entry_price_buy, tick_size=tick_size)

    entry_price_sell = initial_price - bot_config.start_updown_ticks * tick_size
    entry_price_sell_str = round_price_for_order(price=entry_price_sell, tick_size=tick_size)

    if any([
        entry_price_buy > max_price,
        entry_price_buy < min_price,
        entry_price_sell > max_price,
        entry_price_sell < min_price
    ]):
        print(f'Price bigger or less then maximums for {symbol}')
        return

    quantityOrder_buy_str = calculate_quantity_for_order(amount=balanceUSDT099, price=entry_price_buy, lot_size=lot_size)
    quantityOrder_sell_str = calculate_quantity_for_order(amount=balanceUSDT099, price=entry_price_sell, lot_size=lot_size)

    if any([
        Decimal(quantityOrder_buy_str) > max_qty,
        Decimal(quantityOrder_buy_str) < min_qty,
        Decimal(quantityOrder_sell_str) > max_qty,
        Decimal(quantityOrder_sell_str) < min_qty
    ]):
        print(f'Quantity bigger or less then maximums for {symbol}')
        return

    if type_order == 'buy' or type_order == 'both':
        if not buy_order_id:
            buy_order_id='buy_1'

        order_buy = await create_order(
            client=client,
            balanceUSDT=balanceUSDT,
            balanceUSDT099=balanceUSDT099,
            bot_config=bot_config,
            symbol=symbol,
            tick_size=tick_size,
            lot_size=lot_size,
            max_price=max_price,
            min_price=min_price,
            max_qty=max_qty,
            min_qty=min_qty,
            order_type='buy',
            futures_order_type=FUTURE_ORDER_TYPE_STOP_MARKET,
            order_side=SIDE_BUY,
            order_position_side="LONG",
            order_quantity=quantityOrder_buy_str,
            order_stop_price=entry_price_buy_str,
            order_id=buy_order_id,
            tryCreateOrder=tryCreateOrder,
            buy_order_id=buy_order_id,
            sell_order_id=sell_order_id
        )

    if type_order == 'sell' or (type_order == 'both' and order_buy):
        if not sell_order_id:
            sell_order_id='sell_2'

        order_sell = await create_order(
            client=client,
            balanceUSDT=balanceUSDT,
            balanceUSDT099=balanceUSDT099,
            bot_config=bot_config,
            symbol=symbol,
            tick_size=tick_size,
            lot_size=lot_size,
            max_price=max_price,
            min_price=min_price,
            max_qty=max_qty,
            min_qty=min_qty,
            order_type='sell',
            futures_order_type=FUTURE_ORDER_TYPE_STOP_MARKET,
            order_side=SIDE_SELL,
            order_position_side="SHORT",
            order_quantity=quantityOrder_sell_str,
            order_stop_price=entry_price_sell_str,
            order_id=sell_order_id,
            tryCreateOrder=tryCreateOrder,
            buy_order_id=buy_order_id,
            sell_order_id=sell_order_id
        )

    if type_order == 'both':
        print(
            f"balanceUSDT: {balanceUSDT}\n"
            f"symbol: {symbol}\n"
            f"bot_config.start_updown_ticks: {bot_config.start_updown_ticks}\n"
            f"futures_mark_price: {futures_mark_price}\n"
            f"initial_price: {initial_price}\n"
            f"entry_price_buy: {entry_price_buy_str}\n"
            f"entry_price_sell: {entry_price_sell_str}\n"
            f"quantityOrder_buy: {quantityOrder_buy_str}\n"
            f"quantityOrder_sell: {quantityOrder_sell_str}\n"
            f"step size: {str(tick_size)}\n"
        )

        print(
            f"order_buy: {order_buy}\n\n"
            f"order_sell: {order_sell}\n"
        )

    if type_order == 'buy':
        return order_buy
    elif type_order == 'sell':
        return order_sell
    else:
        return {
            'order_buy': order_buy,
            'order_sell': order_sell,
        }

async def creating_orders_bot(session, redis, symbols_characteristics, client, stop_event):
    print('start function creating_orders_bot')
    copy_bot_min_time_profitability_min = await get_copy_bot_tf_params(session)
    print('finished get_copy_bot_tf_params')

    tf_bot_ids = await get_profitable_bots_id_by_tf(session, [copy_bot_min_time_profitability_min])

    print('finished get_profitable_bots_id_by_tf')
    refer_bot = await get_bot_config_by_params(
        session,
        tf_bot_ids,
        copy_bot_min_time_profitability_min
    )
    print('finished get_bot_config_by_params')

    if not refer_bot:
        print('not refer_bot')
        return

    bot_config = TestBot(
        symbol=refer_bot['symbol'],
        stop_success_ticks=refer_bot['stop_success_ticks'],
        stop_loss_ticks = refer_bot['stop_loss_ticks'],
        start_updown_ticks = refer_bot['start_updown_ticks'],
        min_timeframe_asset_volatility = refer_bot['min_timeframe_asset_volatility'],
        time_to_wait_for_entry_price_to_open_order_in_minutes = refer_bot['time_to_wait_for_entry_price_to_open_order_in_minutes']
    )

    # symbol = await redis.get(f"most_volatile_symbol_{bot_config.min_timeframe_asset_volatility}")
    symbol = 'BTCUSDT'

    try:
        symbol_characteristics = symbols_characteristics.get(symbol)
        tick_size = symbol_characteristics['price']['tickSize']
        max_price = symbol_characteristics['price']['maxPrice']
        min_price = symbol_characteristics['price']['minPrice']
        lot_size = symbol_characteristics['lot_size']['stepSize']
        max_qty = symbol_characteristics['lot_size']['maxQty']
        min_qty = symbol_characteristics['lot_size']['minQty']
    except:
        print(f"❌ Ошибка при получении symbol_characteristics по {symbol}")
        return

    if not tick_size or not max_price or not min_price or not lot_size or not min_qty or not max_qty:
        print(f"❌ Нет symbol_characteristics по {symbol}")
        return

    print(f'current symbol: {symbol}')

    try:
        await safe_from_time_err_call_binance(
            client.futures_change_margin_type,
            symbol=symbol, marginType='ISOLATED'
        )
    except:
        pass

    print('start get balance')

    balance = await safe_from_time_err_call_binance(
            client.futures_account_balance
    )
    print('finish get balance')
    balanceUSDT = 0

    for accountAlias in balance:
        if accountAlias['asset'] == 'USDT':
            balanceUSDT = Decimal(accountAlias['balance'])

    if not balanceUSDT:
        return

    print(balanceUSDT)
    print('balanceUSDT')

    if balanceUSDT > 100:
        balanceUSDT = 100

    balanceUSDT099 = balanceUSDT * Decimal(0.99)

    order_update_listener = UserDataWebSocketClient(client)
    await order_update_listener.start()

    bot_config.start_updown_ticks = 100

    orders = await create_orders(
        client=client,
        balanceUSDT=balanceUSDT,
        balanceUSDT099=balanceUSDT099,
        bot_config=bot_config,
        symbol=symbol,
        tick_size=tick_size,
        lot_size=lot_size,
        max_price=max_price,
        min_price=min_price,
        max_qty=max_qty,
        min_qty=min_qty,
        type_order='both',
    )

    # TO DO
    # if not orders['order_buy'] or not orders['order_sell']:
    #     if orders['order_buy']:
    #         # delete order buy
    #     if orders['order_sell']:
    #         # delete order sell
    #
    #         return

    first_order_updating_data = await order_update_listener.get_first_started_order()

    print("✅ Первый ордер получен:", first_order_updating_data)
    # order_update_listener.stop()

    # delete 2 order
    # add updating 2 orders - with orderId to redis
    # if two orders started - close 2 order
    # if just 1 order started - delete 2 order

    # add model for market order
    # create new db order of first filled order
    # add new stops orders for stop lose, take profit
    # wait for stop, update db order

    return

    print(
        f"⏳ Бот {bot_config.id} | Ожидаем входа:"
        f" BUY ≥ {entry_price_buy:.4f}, SELL ≤ {entry_price_sell:.4f}"
    )

    timeoutOccurred = False

    try:
        timeout = int(bot_config.time_to_wait_for_entry_price_to_open_order_in_minutes * 60)

        trade_type, entry_price = await asyncio.wait_for(
            _wait_for_entry_price(
                redis, symbol, entry_price_buy, entry_price_sell
            ),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        timeoutOccurred = True

    if timeoutOccurred or not trade_type or not entry_price:
        print(f"Bot {bot_config.id}; A minute has passed, entry conditions have not been met")
        return False

    open_price = entry_price
    priceFromPreviousStep = entry_price
    close_not_lose_price = calculate_close_not_lose_price(open_price, trade_type)
    stop_loss_price = calculate_stop_lose_price(bot_config, tick_size, open_price, trade_type)
    original_take_profit_price = calculate_take_profit_price(bot_config, tick_size, open_price, trade_type)
    take_profit_price = original_take_profit_price

    print(
        f"🔎 Бот {bot_config.id} | {trade_type} | Вход: {open_price:.4f} | "
        f"SL: {stop_loss_price:.4f} | TP: {take_profit_price:.4f}"
    )

    order = TestOrder(
        stop_loss_price=Decimal(stop_loss_price),
        stop_success_ticks=bot_config.stop_success_ticks,
        open_price=open_price,
        open_time=datetime.now(UTC),
        open_fee=(Decimal(bot_config.balance) * Decimal(COMMISSION_OPEN)),
    )

    while not stop_event.is_set():
        updated_price = await get_price_from_redis(redis, symbol)
        new_tk_p = calculate_take_profit_price(bot_config, tick_size, updated_price, trade_type)
        new_sl_p = calculate_stop_lose_price(bot_config, tick_size, updated_price, trade_type)

        if trade_type == TradeType.BUY:
            if priceFromPreviousStep < updated_price and new_tk_p > take_profit_price:
                take_profit_price = new_tk_p
            elif new_sl_p > order.stop_loss_price:
                order.stop_loss_price = new_sl_p
            if updated_price <= order.stop_loss_price:
                order.stop_reason_event = 'stop-losed'
                # print(f"Бот {bot_config.id} | 📉⛔ BUY order closed by STOP-LOSE at {updated_price}")
                break
            if updated_price > close_not_lose_price and updated_price <= take_profit_price:
                order.stop_reason_event = 'stop-won'
                print(f"Бот {bot_config.id} | 📈✅ BUY order closed by STOP-WIN at {updated_price}, Take profit: {take_profit_price}")
                break
        else:
            if priceFromPreviousStep > updated_price and new_tk_p < take_profit_price:
                take_profit_price = new_tk_p
            elif new_sl_p < order.stop_loss_price:
                order.stop_loss_price = new_sl_p
            if updated_price >= order.stop_loss_price:
                order.stop_reason_event = 'stop-losed'
                # print(f"Бот {bot_config.id} | 📉⛔ SELL order closed by STOP-LOSE at {updated_price}")
                break
            if updated_price < close_not_lose_price and updated_price >= take_profit_price:
                order.stop_reason_event = 'stop-won'
                print(f"Бот {bot_config.id} | 📈✅ SELL order closed by STOP-WIN at {updated_price}, Take profit: {take_profit_price}")
                break

        priceFromPreviousStep = updated_price

        await asyncio.sleep(0.1)

    # Закрытие сделки
    close_price = await get_price_from_redis(redis, symbol)
    balance = bot_config.balance
    amount = Decimal(balance) / Decimal(open_price)
    commission_open = amount * open_price * COMMISSION_OPEN
    commission_close = amount * close_price * COMMISSION_CLOSE
    total_commission = commission_open + commission_close

    if trade_type == TradeType.BUY:
        pnl = (amount * close_price) - (amount * open_price) - total_commission
    elif trade_type == TradeType.SELL:
        pnl = (amount * open_price) - (amount * close_price) - total_commission

    # print(
    #     f"💬 Бот {bot_config.id} | {trade_type} "
    #     f"| Entry: {open_price:.4f} | "
    #     f"Close: {close_price:.4f} | PnL: {pnl:.4f}"
    # )
    try:
        # await TestOrderCrud(session).create(
        #     {
        #         "asset_symbol": symbol,
        #         "order_type": trade_type,
        #         "balance": balance,
        #         "open_price": open_price,
        #         "open_time": order.open_time,
        #         "open_fee": order.open_fee,
        #         "stop_loss_price": order.stop_loss_price,
        #         "bot_id": bot_config.id,
        #         "close_price": close_price,
        #         "close_time": datetime.now(UTC),
        #         "close_fee": order.open_price * Decimal(COMMISSION_CLOSE),
        #         "profit_loss": pnl,
        #         "is_active": False,
        #         "start_updown_ticks": bot_config.start_updown_ticks,
        #         "stop_loss_ticks": bot_config.stop_loss_ticks,
        #         "stop_success_ticks": bot_config.stop_success_ticks,
        #     }
        # )
        await session.commit()
    except Exception as e:
        print(f"❌ Ошибка при записи ордера бота {bot_config.id}: {e}")

async def launch_bot(stop_event):
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    symbols_characteristics = {}

    print('getting tick_size data')

    async with dsm.get_session() as session:
        exchange_crud = AssetExchangeSpecCrud(session)
        symbols_characteristics = await exchange_crud.get_symbols_characteristics_from_active_pairs()

    print('creating binance client')

    api_key = os.getenv("api_key_testnet")
    api_secret = os.getenv("api_secret_testnet")

    client = Client(api_key, api_secret, testnet=True)
    client.FUTURES_URL = 'https://testnet.binancefuture.com/fapi'

    is_set_dual_mode = await check_and_set_dual_mode(client)
    if not is_set_dual_mode:
        print('Mod not dual side position, can\'t to create new orders!')
        return


    # position_info = client.futures_account()
    # # Запись в файл JSON
    # file_name = "position_info.json"
    # with open(file_name, 'w', encoding='utf-8') as f:
    #     json.dump(position_info, f, ensure_ascii=False, indent=4)

    print('finished creating binance client')

    async with redis_context() as redis:
        tasks = []
        print('tasks')
        async def _run_loop():
            async with dsm.get_session() as session:
                # try:
                print('before creating orders')
                await creating_orders_bot(
                    session=session,
                    symbols_characteristics=symbols_characteristics,
                    redis=redis,
                    client=client,
                    stop_event=stop_event,
                )
                # except Exception as e:
                #     print(f"❌ Ошибка в боте: {e}")
                #     await asyncio.sleep(1)

        tasks.append(asyncio.create_task(_run_loop()))
        await asyncio.gather(*tasks)

async def check_and_set_dual_mode(client):
    try:
        mode = await safe_from_time_err_call_binance(
            client.futures_get_position_mode
        )

        if not mode['dualSidePosition']:
            await safe_from_time_err_call_binance(
                client.futures_change_position_mode,
                dualSidePosition=True
            )

        mode = await safe_from_time_err_call_binance(
            client.futures_get_position_mode
        )

        return mode['dualSidePosition']
    except:
        return False

async def safe_from_time_err_call_binance(func, *args, max_retries=20, retry_delay=1, **kwargs):
    for attempt in range(1, max_retries + 1):
        try:
            return func(*args, **kwargs)
        except BinanceAPIException as e:
            if e.code == -1021:
                print(f"Try {attempt}/{max_retries}: Error Binance API: {e}")
                if attempt == max_retries:
                    raise
                time.sleep(retry_delay)
            else:
                raise

async def set_volatile_pairs(stop_event):
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    first_run_completed = False
    asset_volatility_timeframes = []

    async with dsm.get_session() as session:
        async with redis_context() as redis:
            while not stop_event.is_set():
                if not first_run_completed:
                    result = await session.execute(
                        select(distinct(TestBot.min_timeframe_asset_volatility))
                        .where(
                            TestBot.min_timeframe_asset_volatility.is_not(None)
                        )
                    )
                    unique_values = result.scalars().all()
                    asset_volatility_timeframes = list(unique_values)
                    first_run_completed = True

                most_volatile = None
                tf = None
                symbol = None

                for tf in asset_volatility_timeframes:
                    tf = float(tf)
                    now = datetime.now(UTC)
                    time_ago = now - timedelta(minutes=tf)

                    asset_crud = AssetHistoryCrud(session)
                    most_volatile = await asset_crud.get_most_volatile_since(
                        since=time_ago
                    )

                    if most_volatile:
                        symbol = most_volatile.symbol
                        await redis.set(f"most_volatile_symbol_{tf}", symbol)

                # if most_volatile and tf and symbol:
                #     print(f"most_volatile_symbol_{tf} updated: {symbol}")

                await asyncio.sleep(30)

async def get_profitable_bots_id_by_tf(session, bot_profitability_timeframes):
    bot_crud = TestBotCrud(session)

    tf_bot_ids = {}
    # profits_data = await bot_crud.get_sorted_by_profit(just_not_copy_bots=True)
    # filtered_sorted = sorted([item for item in profits_data if item[1] > 0], key=lambda x: x[1], reverse=True)
    # tf_bot_ids['time'] = [item[0] for item in filtered_sorted]

    for tf in bot_profitability_timeframes:
        time_ago = timedelta(minutes=float(tf))

        profits_data = await bot_crud.get_sorted_by_profit(since=time_ago, just_not_copy_bots=True)
        filtered_sorted = sorted([item for item in profits_data if item[1] > 0], key=lambda x: x[1], reverse=True)
        tf_bot_ids[tf] = [item[0] for item in filtered_sorted]

    return tf_bot_ids

async def get_bot_config_by_params(session, tf_bot_ids, copy_bot_min_time_profitability_min):
    min_bot_ids = tf_bot_ids[copy_bot_min_time_profitability_min]

    # tf_time_set = set(tf_bot_ids['time'])
    min_bot_set = set(min_bot_ids)
    min_bot_ids = list(min_bot_set)

    if min_bot_ids:
        refer_bot = await session.execute(
            select(TestBot)
            .where(
                TestBot.id == min_bot_ids[0],
                TestBot.min_timeframe_asset_volatility.is_not(None),
            )
        )
        refer_bot = refer_bot.scalars().all()
        if refer_bot:
            refer_bot = refer_bot[0]
            refer_bot_dict = {
                'id': refer_bot.id,
                'symbol': refer_bot.symbol,
                'stop_success_ticks': refer_bot.stop_success_ticks,
                'stop_loss_ticks': refer_bot.stop_loss_ticks,
                'start_updown_ticks': refer_bot.start_updown_ticks,
                'min_timeframe_asset_volatility': float(refer_bot.min_timeframe_asset_volatility),
                'time_to_wait_for_entry_price_to_open_order_in_minutes': float(refer_bot.time_to_wait_for_entry_price_to_open_order_in_minutes)
            }
        else:
            refer_bot_dict = None
    else:
        refer_bot_dict = None

    return refer_bot_dict

async def set_profitable_bots_for_copy_bots(stop_event):
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    first_run_completed = False
    bot_profitability_timeframes = []

    async with dsm.get_session() as session:
        async with redis_context() as redis:
            while not stop_event.is_set():
                if not first_run_completed:
                    first_run_completed = True

                    result = await session.execute(
                        select(distinct(TestBot.copy_bot_min_time_profitability_min))
                        .where(
                            TestBot.copy_bot_min_time_profitability_min.is_not(None)
                        )
                    )
                    unique_values = result.scalars().all()
                    bot_profitability_timeframes = list(unique_values)

                tf_bot_ids = await get_profitable_bots_id_by_tf(session, bot_profitability_timeframes)

                bots = await session.execute(
                    select(TestBot)
                    .where(
                        TestBot.copy_bot_min_time_profitability_min.is_not(None)
                    )
                )
                bots = bots.scalars().all()

                for bot in bots:
                    refer_bot_dict = await get_bot_config_by_params(
                        session,
                        tf_bot_ids,
                        bot.copy_bot_min_time_profitability_min
                    )
                    await redis.set(f"copy_bot_{bot.id}", json.dumps(refer_bot_dict))

                await asyncio.sleep(30)

def input_listener(loop, stop_event):
    while True:
        cmd = (
            input("👉 Введите 'stop' чтобы остановить бота:\n").strip().lower()
        )
        if cmd == "stop":
            print("🛑 Останавливаем бота...")
            loop.call_soon_threadsafe(stop_event.set)
            break

async def main():
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    input_thread = threading.Thread(
        target=input_listener, args=(loop, stop_event)
    )
    input_thread.start()

    await asyncio.gather(
        launch_bot(stop_event),
        set_volatile_pairs(stop_event),
        set_profitable_bots_for_copy_bots(stop_event),
    )

    print("✅ Все боты завершены.")

if __name__ == "__main__":
    asyncio.run(main())
