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

from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends
from redis.asyncio import Redis
from app.dependencies import (
    get_session,
    get_redis,
    resolve_crud,
)

from app.crud.asset_history import AssetHistoryCrud
from app.crud.exchange_pair_spec import AssetExchangeSpecCrud
from app.crud.test_bot import TestBotCrud
from app.crud.test_orders import TestOrderCrud
from app.db.base import DatabaseSessionManager
from app.config import settings
from app.db.models import TestBot, TestOrder
from app.dependencies import redis_context
from app.sub_services.watchers.user_data_websocket_client import UserDataWebSocketClient


from app.db.models import MarketOrder
from app.sub_services.watchers.price_provider import (
    PriceWatcher,
    PriceProvider,
)
from app.utils import Command
from app.workers.profitable_bot_updater import ProfitableBotUpdaterCommand

UTC = timezone.utc

class BinanceBot(Command):
    def __init__(self, stop_event):
        super().__init__()
        self.redis = None
        self.session = None
        self.bot_crud = None
        self.binance_client = None
        self.symbols_characteristics = None
        self.stop_event = stop_event

    async def command(
        self,
        session: AsyncSession = Depends(get_session),
        redis: Redis = Depends(get_redis),
        bot_crud: TestBotCrud = resolve_crud(TestBotCrud),
    ):
        self.session = session
        self.redis = redis
        self.bot_crud = bot_crud

        print('getting tick_size data')
        exchange_crud = AssetExchangeSpecCrud(self.session)
        self.symbols_characteristics = await exchange_crud.get_symbols_characteristics_from_active_pairs()

        print('creating binance client')

        load_dotenv()
        api_key = os.getenv("api_key_testnet")
        api_secret = os.getenv("api_secret_testnet")

        self.binance_client = Client(api_key, api_secret, testnet=True)
        self.binance_client.FUTURES_URL = 'https://testnet.binancefuture.com/fapi'

        is_set_dual_mode = await self.check_and_set_dual_mode()
        if not is_set_dual_mode:
            print('Mod not dual side position, can\'t to create new orders!')
            return

        # position_info = client.futures_account()
        # # Запись в файл JSON
        # file_name = "position_info.json"
        # with open(file_name, 'w', encoding='utf-8') as f:
        #     json.dump(position_info, f, ensure_ascii=False, indent=4)

        print('finished creating binance client')

        tasks = []
        print('tasks')

        async def _run_loop():
            # while not self.stop_event.is_set():
            # try:
            print('before creating orders')
            await self.creating_orders_bot()
            # except Exception as e:
            #     print(f"❌ Ошибка в боте: {e}")
            #     await asyncio.sleep(1)

        tasks.append(asyncio.create_task(_run_loop()))
        await asyncio.gather(*tasks)

    async def check_and_set_dual_mode(self):
        try:
            mode = await self._safe_from_time_err_call_binance(
                self.binance_client.futures_get_position_mode
            )

            if not mode['dualSidePosition']:
                await self._safe_from_time_err_call_binance(
                    self.binance_client.futures_change_position_mode,
                    dualSidePosition=True
                )

            mode = await self._safe_from_time_err_call_binance(
                self.binance_client.futures_get_position_mode
            )

            return mode['dualSidePosition']
        except:
            return False

    async def creating_orders_bot(self):
        print('start function creating_orders_bot')
        copy_bot_min_time_profitability_min = await self._get_copy_bot_tf_params()
        print('finished get_copy_bot_tf_params')

        tf_bot_ids = await ProfitableBotUpdaterCommand.get_profitable_bots_id_by_tf(
            bot_crud=self.bot_crud,
            bot_profitability_timeframes=[copy_bot_min_time_profitability_min],
        )

        print('finished get_profitable_bots_id_by_tf')
        refer_bot = await ProfitableBotUpdaterCommand.get_bot_config_by_params(
            bot_crud=self.bot_crud,
            tf_bot_ids=tf_bot_ids,
            copy_bot_min_time_profitability_min=copy_bot_min_time_profitability_min
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

        # symbol = await self.redis.get(f"most_volatile_symbol_{bot_config.min_timeframe_asset_volatility}")
        symbol = 'BTCUSDT'

        try:
            symbol_characteristics = self.symbols_characteristics.get(symbol)
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
            await self._safe_from_time_err_call_binance(
                self.binance_client.futures_change_margin_type,
                symbol=symbol, marginType='ISOLATED'
            )
        except:
            pass

        print('start get balance')

        balance = await self._safe_from_time_err_call_binance(
                self.binance_client.futures_account_balance
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

        bot_config.start_updown_ticks = 100

        db_order_buy = MarketOrder(
            symbol=symbol,
            exchange_name='BINANCE',
            side='BUY',
            position_side='LONG',
            open_order_type=FUTURE_ORDER_TYPE_STOP_MARKET,
            start_updown_ticks=bot_config.start_updown_ticks,
            trailing_stop_lose_ticks=bot_config.stop_loss_ticks,
            trailing_stop_win_ticks=bot_config.stop_success_ticks,
            status='NEW'
        )

        db_order_sell = MarketOrder(
            symbol=symbol,
            exchange_name='BINANCE',
            side='SELL',
            position_side='SHORT',
            open_order_type=FUTURE_ORDER_TYPE_STOP_MARKET,
            start_updown_ticks=bot_config.start_updown_ticks,
            trailing_stop_lose_ticks=bot_config.stop_loss_ticks,
            trailing_stop_win_ticks=bot_config.stop_success_ticks,
            status='NEW'
        )

        try:
            self.session.add(db_order_buy)
            self.session.add(db_order_sell)
            await self.session.commit()
        except Exception as e:
            self.session.rollback()
            print(f"❌ Error adding market order to DB: {e}")
            return

        db_order_buy.client_order_id = f'buy_{db_order_buy.id}'
        db_order_sell.client_order_id = f'sell_{db_order_sell.id}'

        print(db_order_buy.client_order_id)
        print(db_order_sell.client_order_id)

        order_update_listener = UserDataWebSocketClient(
            self.binance_client,
            waiting_orders=[db_order_buy, db_order_sell]
        )
        await order_update_listener.start()

        # price_provider = PriceProvider(redis=self.redis)
        exchange_orders = await self.create_orders(
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
            creating_orders_type='both',
            db_order_buy=db_order_buy,
            db_order_sell=db_order_sell,
        )

        if not exchange_orders['order_buy'] or not exchange_orders['order_sell']:
            deleting_order = None

            if exchange_orders['order_buy']:
                deleting_order = {
                    'symbol': symbol,
                    'side': 'BUY',
                    'origClientOrderId': exchange_orders['order_buy']['clientOrderId'],
                }

                db_order_buy.status = 'CANCELED'
                db_order_buy.close_reason = f'Can\'t create sell order, cancel both'
            if exchange_orders['order_sell']:
                deleting_order = {
                    'symbol': symbol,
                    'side': 'SELL',
                    'origClientOrderId': exchange_orders['order_sell']['clientOrderId'],
                }

                db_order_sell.status = 'CANCELED'
                db_order_sell.close_reason = f'Can\'t create buy order, cancel both'

            await self.session.commit()

            if deleting_order:
                await self.delete_order(
                    order=deleting_order
                )

            print(f"❌ Один из ордеров не может быть создан, второй ордер был отменён")
            return

        first_order_updating_data = await order_update_listener.get_first_started_order()
        print("✅ Первый ордер получен:", first_order_updating_data)

        await self.delete_second_order(
            symbol=symbol,
            first_order_updating_data=first_order_updating_data,
            exchange_orders=exchange_orders,
            db_order_sell=db_order_sell,
            db_order_buy=db_order_buy
        )

        if first_order_updating_data['c'] == exchange_orders['order_buy']['clientOrderId']:
            db_order = db_order_buy
        else:
            db_order = db_order_sell

        timeoutOccurred = False

        try:
            timeout = Decimal(bot_config.time_to_wait_for_entry_price_to_open_order_in_minutes)
            timeout = int(timeout * 60)

            await asyncio.wait_for(
                self._wait_filling_of_order(db_order),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            timeoutOccurred = True

        if timeoutOccurred:
            print(f"Bot {bot_config.id}; A minute has passed, entry conditions have not been met")

            #delete order

            return False
        else:
            print(f'db_order - activated: {db_order.activation_time}, opened: {db_order.open_time}')

        await asyncio.sleep(60)

        # add model for market order
        # create new db order of first filled order
        # add new stops orders for stop lose, take profit
        # wait for stop, update db order

        # order_update_listener.stop()
        return

        print(
            f"⏳ Бот {bot_config.id} | Ожидаем входа:"
            f" BUY ≥ {entry_price_buy:.4f}, SELL ≤ {entry_price_sell:.4f}"
        )

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

        while not self.stop_event.is_set():
            updated_price = await get_price_from_redis(self.redis, symbol)
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
        close_price = await get_price_from_redis(self.redis, symbol)
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
            await self.session.commit()
        except Exception as e:
            print(f"❌ Ошибка при записи ордера бота {bot_config.id}: {e}")

    async def create_orders(
        self, balanceUSDT, balanceUSDT099, bot_config,
        symbol, tick_size, lot_size, max_price, min_price, max_qty, min_qty, creating_orders_type,
        db_order_buy=None, db_order_sell=None,
        tryCreateOrder=0,
    ):
        futures_mark_price = await self._safe_from_time_err_call_binance(
            self.binance_client.futures_mark_price,
            symbol=symbol
        )
        initial_price = Decimal(futures_mark_price['markPrice'])

        entry_price_buy = initial_price + bot_config.start_updown_ticks * tick_size
        entry_price_buy_str = self._round_price_for_order(price=entry_price_buy, tick_size=tick_size)

        entry_price_sell = initial_price - bot_config.start_updown_ticks * tick_size
        entry_price_sell_str = self._round_price_for_order(price=entry_price_sell, tick_size=tick_size)

        quantityOrder_buy_str = self._calculate_quantity_for_order(amount=balanceUSDT099, price=entry_price_buy, lot_size=lot_size)
        quantityOrder_sell_str = self._calculate_quantity_for_order(amount=balanceUSDT099, price=entry_price_sell, lot_size=lot_size)

        if any([
            entry_price_buy > max_price,
            entry_price_buy < min_price,
            entry_price_sell > max_price,
            entry_price_sell < min_price
        ]):
            print(f'Price bigger or less then maximums for {symbol}')

            db_order_buy.status = 'CANCELED'
            db_order_buy.close_reason = f'Price bigger or less then maximums for {symbol}'
            db_order_sell.status = 'CANCELED'
            db_order_sell.close_reason = f'Price bigger or less then maximums for {symbol}'
            await self.session.commit()

            creating_orders_type = 'canceled'

        if any([
            Decimal(quantityOrder_buy_str) > max_qty,
            Decimal(quantityOrder_buy_str) < min_qty,
            Decimal(quantityOrder_sell_str) > max_qty,
            Decimal(quantityOrder_sell_str) < min_qty
        ]):
            print(f'Quantity bigger or less then maximums for {symbol}')

            db_order_buy.status = 'CANCELED'
            db_order_buy.close_reason = f'Quantity bigger or less then maximums for {symbol}'
            db_order_sell.status = 'CANCELED'
            db_order_sell.close_reason = f'Quantity bigger or less then maximums for {symbol}'
            await self.session.commit()

            creating_orders_type = 'canceled'

        order_buy = None
        order_sell = None

        if creating_orders_type == 'buy' or creating_orders_type == 'both':
            order_buy = await self.create_order(
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
                creating_orders_type='buy',
                futures_order_type=FUTURE_ORDER_TYPE_STOP_MARKET,
                order_side=SIDE_BUY,
                order_position_side="LONG",
                order_quantity=quantityOrder_buy_str,
                order_stop_price=entry_price_buy_str,
                db_order_buy=db_order_buy,
                db_order_sell=db_order_sell,
                tryCreateOrder=tryCreateOrder
            )

        if creating_orders_type == 'sell' or (creating_orders_type == 'both' and order_buy):
            order_sell = await self.create_order(
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
                creating_orders_type='sell',
                futures_order_type=FUTURE_ORDER_TYPE_STOP_MARKET,
                order_side=SIDE_SELL,
                order_position_side="SHORT",
                order_quantity=quantityOrder_sell_str,
                order_stop_price=entry_price_sell_str,
                db_order_buy=db_order_buy,
                db_order_sell=db_order_sell,
                tryCreateOrder=tryCreateOrder
            )

        if creating_orders_type == 'both':
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

            db_order_buy.quote_quantity = balanceUSDT
            db_order_buy.asset_quantity = Decimal(quantityOrder_buy_str)
            db_order_buy.start_price = initial_price
            db_order_buy.activation_price = Decimal(entry_price_buy_str)

            db_order_sell.quote_quantity = balanceUSDT
            db_order_sell.asset_quantity = Decimal(quantityOrder_sell_str)
            db_order_sell.start_price = initial_price
            db_order_sell.activation_price = Decimal(entry_price_sell_str)

            await self.session.commit()

            print(
                f"order_buy: {order_buy}\n\n"
                f"order_sell: {order_sell}\n"
            )

        if creating_orders_type == 'buy':
            return order_buy
        elif creating_orders_type == 'sell':
            return order_sell
        else:
            return {
                'order_buy': order_buy,
                'order_sell': order_sell,
            }

    async def create_order(
        self, balanceUSDT, balanceUSDT099, bot_config,
        symbol, tick_size, lot_size, max_price, min_price, max_qty, min_qty, creating_orders_type,
        futures_order_type, order_side, order_position_side, order_quantity, order_stop_price,
        tryCreateOrder,
        db_order_buy=None, db_order_sell=None,
    ):
        tryCreateOrder = tryCreateOrder + 1

        if creating_orders_type == 'buy':
            db_order = db_order_buy
        else:
            db_order = db_order_sell

        if tryCreateOrder > 10:
            print('Too much tries when stop price like last market price')

            db_order.status = 'CANCELED'
            db_order.close_reason = f'Quantity bigger or less then maximums for {symbol}'
            await self.session.commit()

            return None

        try:
            order = await self._safe_from_time_err_call_binance(
                self.binance_client.futures_create_order,
                symbol=symbol,
                side=order_side,
                positionSide=order_position_side,
                type=futures_order_type,
                quantity=order_quantity,
                stopPrice=order_stop_price,
                newClientOrderId=db_order.client_order_id,
                workingType="MARK_PRICE",
                priceProtect=True,
                newOrderRespType="RESULT",
                recvWindow=3000,
                tryCreateOrder=tryCreateOrder,
            )

            if 'orderId' in order and 'status' in order:
                db_order.exchange_order_id = str(order['orderId'])
                await self.session.commit()

            return order
        except BinanceAPIException as e:
            if e.code == -2021:
                order = await self.create_orders(
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
                    creating_orders_type=creating_orders_type,
                    tryCreateOrder=tryCreateOrder,
                    db_order_buy=db_order_buy,
                    db_order_sell=db_order_sell
                )

                if 'orderId' in order and 'status' in order:
                    db_order.exchange_order_id = str(order['orderId'])
                    await self.session.commit()

                return order
            else:
                raise

    async def delete_order(
            self, order
    ):
        try:
            await self._safe_from_time_err_call_binance(
                self.binance_client.futures_cancel_order,
                symbol=order['symbol'],
                origClientOrderId=order['origClientOrderId']
            )
        except Exception as e:
            print("Не могу удалить ордер, он уже отменён или исполнен:", e)

        if order['side'] == 'BUY':
            order_side = SIDE_SELL
            order_position_side = 'LONG'
        else:
            order_side = SIDE_BUY
            order_position_side = 'SHORT'

        order = await self._safe_from_time_err_call_binance(
            self.binance_client.futures_get_order,
            symbol=order['symbol'],
            origClientOrderId=order['origClientOrderId']
        )
        executed_qty = float(order["executedQty"])

        positions = await self._safe_from_time_err_call_binance(
            self.binance_client.futures_position_information,
            symbol=order['symbol']
        )
        position_amt = 0
        for pos in positions:
            if pos['positionSide'] == order_position_side:
                position_amt = Decimal(pos['positionAmt'])

        if executed_qty > 0 and position_amt != 0:
            try:
                await self._safe_from_time_err_call_binance(
                    self.binance_client.futures_create_order,
                    symbol=order['symbol'],
                    side=order_side,
                    positionSide=order_position_side,
                    type=FUTURE_ORDER_TYPE_MARKET,
                    quantity=executed_qty,
                    reduceOnly=True
                )
            except:
                await self._safe_from_time_err_call_binance(
                    self.binance_client.futures_create_order,
                    symbol=order['symbol'],
                    side=order_side,
                    positionSide=order_position_side,
                    type=FUTURE_ORDER_TYPE_MARKET,
                    quantity=executed_qty
                )

        return

    async def delete_second_order(
            self, symbol, first_order_updating_data, exchange_orders, db_order_sell, db_order_buy
    ):
        deleting_order = None

        if first_order_updating_data['c'] == exchange_orders['order_buy']['clientOrderId']:
            deleting_order = {
                'symbol': symbol,
                'side': 'SELL',
                'origClientOrderId': exchange_orders['order_sell']['clientOrderId'],
            }

            db_order_sell.status = 'CANCELED'
            db_order_sell.close_reason = f'Buy order activated first'
        elif first_order_updating_data['c'] == exchange_orders['order_sell']['clientOrderId']:
            deleting_order = {
                'symbol': symbol,
                'side': 'BUY',
                'origClientOrderId': exchange_orders['order_buy']['clientOrderId'],
            }

            db_order_buy.status = 'CANCELED'
            db_order_buy.close_reason = f'Sell order activated first'

        await self.session.commit()

        if deleting_order:
            await self.delete_order(
                order=deleting_order
            )
        else:
            print('❌ Can\'t get first order. stop.')
            return

    async def _safe_from_time_err_call_binance(self, func, *args, max_retries=20, retry_delay=1, **kwargs):
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

    async def _wait_filling_of_order(self, db_order):
        while True:
            if db_order.open_time is not None:
                return True

            await asyncio.sleep(0.1)

    async def _get_copy_bot_tf_params(self):
        copy_bot_min_time_profitability_min = 180

        profits_data = await self.bot_crud.get_sorted_by_profit(since=timedelta(hours=1),just_copy_bots=True)
        profits_data_filtered_sorted = sorted([item for item in profits_data if item[1] > 0], key=lambda x: x[1], reverse=True)

        refer_bot_id = None

        try:
            refer_bot_id = profits_data_filtered_sorted[0][0]
        except (IndexError, TypeError):
            pass

        if refer_bot_id:
            refer_bot = await self.session.execute(
                select(TestBot)
                .where(
                    TestBot.id == refer_bot_id,
                )
            )
            refer_bot = refer_bot.scalars().all()
            if refer_bot:
                refer_bot = refer_bot[0]
                copy_bot_min_time_profitability_min = refer_bot.copy_bot_min_time_profitability_min

        return copy_bot_min_time_profitability_min

    def _get_precision_by_tick_size(self, tick_size):
        precision = int(round(-math.log10(tick_size), 0))
        return precision

    def _calculate_quantity_for_order(self, amount: Decimal, price: Decimal, lot_size: Decimal):
        raw_quantity = amount / price
        return self._round_price_for_order(raw_quantity, lot_size)

    def _round_price_for_order(self, price: Decimal, tick_size: Decimal):
        precision = self._get_precision_by_tick_size(tick_size)
        rounded_price = f"{price:.{precision}f}"
        return rounded_price
