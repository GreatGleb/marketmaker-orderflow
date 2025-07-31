import asyncio
import math
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging

from binance.client import Client
from binance.enums import FUTURE_ORDER_TYPE_STOP_MARKET, FUTURE_ORDER_TYPE_MARKET, FUTURE_ORDER_TYPE_TRAILING_STOP_MARKET, SIDE_SELL, SIDE_BUY
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv
from fastapi import Depends
from redis.asyncio import Redis
from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.exchange_pair_spec import AssetExchangeSpecCrud
from app.crud.test_bot import TestBotCrud
from app.crud.test_orders import TestOrderCrud
from app.db.models import MarketOrder, TestBot, TestOrder
from app.dependencies import get_redis, get_session, resolve_crud, redis_context
from app.sub_services.logic.price_calculator import PriceCalculator
from app.sub_services.watchers.price_provider import PriceProvider
from app.sub_services.watchers.user_data_websocket_client import UserDataWebSocketClient
from app.utils import Command
from app.workers.profitable_bot_updater import ProfitableBotUpdaterCommand

UTC = timezone.utc

class BinanceBot(Command):
    def __init__(self, stop_event = None):
        super().__init__()

        logging.basicConfig(
            format='%(asctime)s - %(levelname)s - %(message)s',
            level=logging.INFO
        )

        self.redis = None
        self.session = None
        self.bot_crud = None
        self.symbols_characteristics = None
        self.stop_event = stop_event

        load_dotenv()
        self.is_prod = os.getenv("ENVIRONMENT") == "prod"
        logging.info(f'is_prod = {self.is_prod}, {os.getenv("ENVIRONMENT")}')

        if self.is_prod:
            api_key = os.getenv("BINANCE_API_KEY")
            api_secret = os.getenv("BINANCE_SECRET_KEY")
            self.binance_client = Client(api_key, api_secret)
        else:
            api_key = os.getenv("BINANCE_API_KEY_TESTNET")
            api_secret = os.getenv("BINANCE_SECRET_KEY_TESTNET")
            self.binance_client = Client(api_key, api_secret, testnet=True)
            self.binance_client.FUTURES_URL = 'https://testnet.binancefuture.com/fapi'

    async def command(
        self,
        session: AsyncSession = Depends(get_session),
        redis: Redis = Depends(get_redis),
        bot_crud: TestBotCrud = resolve_crud(TestBotCrud),
    ):
        self.session = session
        self.redis = redis
        self.bot_crud = bot_crud
        self.price_provider = PriceProvider(redis=self.redis)

        logging.info('getting tick_size data')
        exchange_crud = AssetExchangeSpecCrud(self.session)
        self.symbols_characteristics = await exchange_crud.get_symbols_characteristics_from_active_pairs()

        is_set_dual_mode = await self.check_and_set_dual_mode()
        if not is_set_dual_mode:
            logging.info('Mod not dual side position, can\'t to create new orders!')
            return

        logging.info('finished creating binance client')

        # time_sleep = 60*60
        # await asyncio.sleep(time_sleep)

        tasks = []
        logging.info('tasks')

        async def _run_loop():
            while not self.stop_event.is_set():
                # try:
                logging.info('before creating orders')
                await self.creating_orders_bot()
                # except Exception as e:
                logging.info(f"❌ Ошибка в боте: {e}")
                await asyncio.sleep(60)
                break

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
        # logging.info('start function creating_orders_bot')
        # copy_bot = await self._get_copy_bot_tf_params()
        # logging.info('finished get_copy_bot_tf_params')
        #
        # refer_bot = None
        # if copy_bot:
        #     tf_bot_ids = await ProfitableBotUpdaterCommand.get_profitable_bots_id_by_tf(
        #         bot_crud=self.bot_crud,
        #         bot_profitability_timeframes=[copy_bot.copy_bot_min_time_profitability_min],
        #     )
        #
        #     logging.info('finished get_profitable_bots_id_by_tf')
        #     refer_bot = await ProfitableBotUpdaterCommand.get_bot_config_by_params(
        #         bot_crud=self.bot_crud,
        #         tf_bot_ids=tf_bot_ids,
        #         copy_bot_min_time_profitability_min=copy_bot.copy_bot_min_time_profitability_min
        #     )
        #     logging.info('finished get_bot_config_by_params')
        #
        # test_order_crud = TestOrderCrud(self.session)
        # are_bots_currently_active = await test_order_crud.are_bots_currently_active()
        #
        # if not are_bots_currently_active:
        #     logging.info('not are_bots_currently_active')
        #     await asyncio.sleep(60)
        #     return
        #
        # if not refer_bot:
        #     logging.info('not refer_bot')
        #     await asyncio.sleep(60)
        #     return
        #
        # if self.is_prod:
        #     symbol = await self.redis.get(f"most_volatile_symbol_{refer_bot['min_timeframe_asset_volatility']}")
        #     if not symbol:
        #         logging.info(f"❌ Не найдено самую волатильную пару")
        #         await asyncio.sleep(60)
        #         return
        #
        #     bot_config = TestBot(
        #         symbol=refer_bot['symbol'],
        #         stop_success_ticks=refer_bot['stop_success_ticks'],
        #         stop_loss_ticks = refer_bot['stop_loss_ticks'],
        #         start_updown_ticks = refer_bot['start_updown_ticks'],
        #         stop_win_percents = Decimal(refer_bot['stop_win_percents']),
        #         stop_loss_percents = Decimal(refer_bot['stop_loss_percents']),
        #         start_updown_percents = Decimal(refer_bot['start_updown_percents']),
        #         min_timeframe_asset_volatility = refer_bot['min_timeframe_asset_volatility'],
        #         time_to_wait_for_entry_price_to_open_order_in_minutes = refer_bot['time_to_wait_for_entry_price_to_open_order_in_minutes'],
        #         consider_ma_for_open_order=copy_bot.consider_ma_for_open_order,
        #         consider_ma_for_close_order=copy_bot.consider_ma_for_close_order,
        #     )
        # else:
        #     symbol = 'BTCUSDT'
        #     bot_config = TestBot(
        #         symbol='BTCUSDT',
        #         stop_success_ticks = 40,
        #         stop_loss_ticks = 70,
        #         start_updown_ticks = 10,
        #         min_timeframe_asset_volatility = 3,
        #         time_to_wait_for_entry_price_to_open_order_in_minutes = 0.5
        #     )

        symbol = 'BTCUSDT'
        bot_config = TestBot(
            symbol='BTCUSDT',
            consider_ma_for_open_order=True,
            consider_ma_for_close_order=True,
        )

        try:
            symbol_characteristics = self.symbols_characteristics.get(symbol)
            tick_size = symbol_characteristics['price']['tickSize']
            max_price = symbol_characteristics['price']['maxPrice']
            min_price = symbol_characteristics['price']['minPrice']
            lot_size = symbol_characteristics['lot_size']['stepSize']
            max_qty = symbol_characteristics['lot_size']['maxQty']
            min_qty = symbol_characteristics['lot_size']['minQty']
        except:
            logging.info(f"❌ Ошибка при получении symbol_characteristics по {symbol}")
            await asyncio.sleep(60)
            return

        if not tick_size or not max_price or not min_price or not lot_size or not min_qty or not max_qty:
            logging.info(f"❌ Нет symbol_characteristics по {symbol}")
            await asyncio.sleep(60)
            return

        bot_config = await ProfitableBotUpdaterCommand.update_config_for_percentage(
            bot_config=bot_config,
            price_provider=self.price_provider,
            symbol=symbol,
            tick_size=tick_size
        )

        logging.info(f'current symbol: {symbol}')

        logging.info('close_all_open_positions')
        is_closed_all_positions = await self.close_all_open_positions()
        if not is_closed_all_positions:
            logging.info(f"❌ Can\'t close all open positions")
            await asyncio.sleep(60)
            return

        logging.info('start get balance')

        balance = await self._safe_from_time_err_call_binance(
                self.binance_client.futures_account_balance
        )
        logging.info('finish get balance')
        balanceUSDT = 0

        for accountAlias in balance:
            if accountAlias['asset'] == 'USDT':
                balanceUSDT = Decimal(accountAlias['balance'])

        if not balanceUSDT:
            logging.info('not balanceUSDT')
            await asyncio.sleep(60)
            return

        if not self.is_prod:
            balanceUSDT = 100

        logging.info(balanceUSDT)
        logging.info('balanceUSDT')

        balanceUSDT099 = Decimal(balanceUSDT) * Decimal(0.99)

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
            await self.session.rollback()
            logging.info(f"❌ Error adding market order to DB: {e}")
            await asyncio.sleep(60)
            return

        db_order_buy.client_order_id = f'buy_{db_order_buy.id}'
        db_order_sell.client_order_id = f'sell_{db_order_sell.id}'

        logging.info(db_order_buy.client_order_id)
        logging.info(db_order_sell.client_order_id)

        self.order_update_listener = UserDataWebSocketClient(
            self.binance_client,
            waiting_orders=[db_order_buy, db_order_sell]
        )
        await self.order_update_listener.start()

        exchange_orders = await self.create_orders(
            balanceUSDT=balanceUSDT099,
            bot_config=bot_config,
            symbol=symbol,
            tick_size=tick_size,
            lot_size=lot_size,
            max_price=max_price,
            min_price=min_price,
            max_qty=max_qty,
            min_qty=min_qty,
            db_order_buy=db_order_buy,
            db_order_sell=db_order_sell,
        )

        # if not exchange_orders['order_buy'] or not exchange_orders['order_sell']:
        #     if exchange_orders['order_buy'] and 'orderId' in exchange_orders['order_buy']:
        #         await self.delete_order(
        #             db_order=db_order_buy,
        #             status='CANCELED',
        #             close_reason=f'Can\'t create sell order, cancel both'
        #         )
        #     if exchange_orders['order_sell'] and 'orderId' in exchange_orders['order_sell']:
        #         await self.delete_order(
        #             db_order=db_order_sell,
        #             status='CANCELED',
        #             close_reason=f'Can\'t create buy order, cancel both'
        #         )
        #
        #     logging.info(f"❌ Один из ордеров не может быть создан, второй ордер был отменён")
        #     return
        if exchange_orders['order_buy'] is None and exchange_orders['order_sell'] is None:
            await asyncio.sleep(60)
            logging.info(f"Both orders was canceled")
            return

        wait_for_order = await self._wait_until_order_activated(bot_config, db_order_buy, db_order_sell)
        if not wait_for_order['timeout_missed']:
            logging.info(f"A minute has passed, entry conditions have not been met")
            return

        if wait_for_order['first_order_updating_data']['c'] == db_order_buy.client_order_id:
            db_order = db_order_buy
            second_order = db_order_sell
        else:
            db_order = db_order_sell
            second_order = db_order_buy

        delete_task = asyncio.create_task(
            self.delete_second_order(second_order)
        )

        wait_filled_task = asyncio.create_task(
            self._wait_until_order_filled(bot_config, db_order)
        )

        wait_db_commit_task = asyncio.create_task(
            self._db_commit()
        )

        wait_filled = await wait_filled_task
        if not wait_filled['timeout_missed']:
            logging.info(f"A minute has passed, order did\'nt fill")
            await delete_task
            await wait_db_commit_task
            return

        logging.info("✅ Первый ордер получен:", wait_for_order['first_order_updating_data'])

        if not bot_config.consider_ma_for_close_order:
            setting_sl_sw_to_order_task = asyncio.create_task(
                self.setting_sl_sw_to_order(db_order, bot_config, tick_size)
            )
            await setting_sl_sw_to_order_task
        else:
            close_order_by_ma25_task = asyncio.create_task(
                self.close_order_by_ma25(db_order)
            )
            await close_order_by_ma25_task

        await delete_task
        await wait_db_commit_task

        self.order_update_listener.stop()
        return

    async def _db_commit(self):
        try:
            await self.session.commit()
        except Exception as e:
            await self.session.rollback()
            logging.info(f"❌ Error DB: {e}")
        return

    async def create_orders(
        self, balanceUSDT, bot_config,
        symbol, tick_size, lot_size, max_price, min_price, max_qty, min_qty,
        db_order_buy, db_order_sell
    ):
        order_buy_create_task = asyncio.create_task(
            self.create_order(
                balanceUSDT=balanceUSDT,
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
                db_order=db_order_buy,
            )
        )

        order_sell_create_task = asyncio.create_task(
            self.create_order(
                balanceUSDT=balanceUSDT,
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
                db_order=db_order_sell,
            )
        )

        order_buy = await order_buy_create_task
        order_sell = await order_sell_create_task

        logging.info(
            f"order_buy: {order_buy}\n\n"
            f"order_sell: {order_sell}\n"
        )

        return {
            'order_buy': order_buy,
            'order_sell': order_sell,
        }

    async def create_order(
        self, balanceUSDT, bot_config,
        symbol, tick_size, lot_size, max_price, min_price, max_qty, min_qty, creating_orders_type,
        futures_order_type, order_side, order_position_side,
        db_order
    ):
        order = None
        order_params = None
        order_quantity = None
        order_stop_price = None
        try_create_order = 0

        current_price = await self.price_provider.get_price(symbol=db_order.symbol)

        if bot_config.consider_ma_for_open_order:
            ma25 = await self.get_ma(db_order.symbol, 25, current_price)

            if not ma25:
                db_order.status = 'CANCELED'
                db_order.close_reason = f'MA25 can\'t find klines for {symbol}'
                logging.info(f'MA25 can\'t find klines for {symbol}')

                return order
            else:
                if creating_orders_type == 'buy':
                    if current_price < ma25:
                        db_order.status = 'CANCELED'
                        db_order.close_reason = f'MA25 bigger then current price for {symbol}'
                        logging.info(f'MA25 bigger then current price for {symbol}')

                        return order
                elif creating_orders_type == 'sell':
                    if current_price > ma25:
                        db_order.status = 'CANCELED'
                        db_order.close_reason = f'MA25 less then current price for {symbol}'
                        logging.info(f'MA25 less then current price for {symbol}')

                        return order

            order_params = await self._get_order_params(
                bot_config, balanceUSDT,
                symbol, tick_size, lot_size, max_price, min_price, max_qty, min_qty,
                db_order
            )

            if not order_params:
                db_order.status = 'CANCELED'
                db_order.close_reason = f'Can\'t get order params for {symbol}'
                logging.info(f'Can\'t get order params for {creating_orders_type} {symbol}')
                return order

            if creating_orders_type == 'buy':
                order_quantity = order_params['quantityOrder_buy_str']
                order_stop_price = 0
            else:
                order_quantity = order_params['quantityOrder_sell_str']
                order_stop_price = 0

            try:
                order = await self._safe_from_time_err_call_binance(
                    self.binance_client.futures_create_order,
                    symbol=symbol,
                    side=order_side,
                    positionSide=order_position_side,
                    type=FUTURE_ORDER_TYPE_MARKET,
                    quantity=order_quantity,
                    newClientOrderId=db_order.client_order_id,
                    newOrderRespType="RESULT",
                    recvWindow=3000,
                )
            except BinanceAPIException as e:
                db_order.status = 'CANCELED'
                db_order.close_reason = f'Binance error while creating order: {e}'
                logging.info(f'Binance error while creating order {creating_orders_type}: {e}')
            except Exception as e:
                db_order.status = 'CANCELED'
                db_order.close_reason = f'Error while creating Binance order: {e}'
                logging.info(f"Error while creating binance order {creating_orders_type}: {e}")

        else:
            while True:
                if try_create_order > 10:
                    db_order.status = 'CANCELED'
                    db_order.close_reason = f'Quantity bigger or less then maximums for {symbol}'
                    logging.info('Too much tries when stop price like last market price')
                    break

                order_params = await self._get_order_params(
                    bot_config, balanceUSDT,
                    symbol, tick_size, lot_size, max_price, min_price, max_qty, min_qty,
                    db_order
                )

                if not order_params:
                    db_order.status = 'CANCELED'
                    db_order.close_reason = f'Can\'t get order params for {symbol}'
                    logging.info(f'Can\'t get order params for {creating_orders_type} {symbol}')
                    return order

                if creating_orders_type == 'buy':
                    order_quantity = order_params['quantityOrder_buy_str']
                    order_stop_price = order_params['entry_price_buy_str']
                else:
                    order_quantity = order_params['quantityOrder_sell_str']
                    order_stop_price = order_params['entry_price_sell_str']

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
                    )

                    break
                except BinanceAPIException as e:
                    if e.code == -2021:
                        logging.info(f'try_create_order {creating_orders_type}: {e}')
                        try_create_order = try_create_order + 1
                        continue
                    else:
                        db_order.status = 'CANCELED'
                        db_order.close_reason = f'Binance error while creating order: {e}'
                        logging.info(f'Binance error while creating order {creating_orders_type}: {e}')
                        break
                except Exception as e:
                    db_order.status = 'CANCELED'
                    db_order.close_reason = f'Error while creating Binance order: {e}'
                    logging.info(f"Error while creating binance order {creating_orders_type}: {e}")
                    break

        if order_params and order_quantity:
            db_order.asset_quantity = Decimal(order_quantity)
            db_order.start_price = order_params['initial_price']
        if order_stop_price:
            db_order.activation_price = Decimal(order_stop_price)

        db_order.quote_quantity = balanceUSDT

        if order and 'orderId' in order and 'status' in order:
            db_order.exchange_order_id = str(order['orderId'])

        return order

    async def delete_second_order(self, second_order):
        if second_order.close_reason is None and second_order.exchange_order_id:
            await self.delete_order(
                db_order=second_order,
                status='CANCELED',
                close_reason=f'Another order activated first'
            )

    async def delete_order(self, db_order, status=None, close_reason=None, deleting_order_id=None):
        if status:
            db_order.status = status
        if close_reason:
            db_order.close_reason = close_reason

        await self.delete_binance_order(
            db_order=db_order,
            deleting_order_id=deleting_order_id
        )

    async def delete_binance_order(
            self, db_order, deleting_order_id=None
    ):
        if db_order.close_order_type is None:
            try:
                await self._safe_from_time_err_call_binance(
                    self.binance_client.futures_cancel_order,
                    symbol=db_order.symbol,
                    origClientOrderId=db_order.client_order_id
                )
            except Exception as e:
                logging.info(f"Не могу удалить ордер, он уже отменён или исполнен: {e}")

        if not (
            (db_order.side == 'BUY' and db_order.position_side == 'SHORT') or (db_order.side == 'SELL' and db_order.position_side == 'LONG')
        ):
            if db_order.open_time is not None and db_order.asset_quantity > 0:
                executed_qty = str(db_order.asset_quantity)
            else:
                binance_deleting_order = await self._safe_from_time_err_call_binance(
                    self.binance_client.futures_get_order,
                    symbol=db_order.symbol,
                    origClientOrderId=db_order.client_order_id
                )

                executed_qty = binance_deleting_order["executedQty"]
            logging.info(f'Deleting open position executed_qty: {executed_qty} at symbol: {db_order.symbol}')

            if db_order.side == 'BUY':
                order_side = SIDE_SELL
                order_position_side = 'LONG'
            else:
                order_side = SIDE_BUY
                order_position_side = 'SHORT'

            if Decimal(executed_qty) > 0:
                try:
                    if deleting_order_id:
                        await self._safe_from_time_err_call_binance(
                            self.binance_client.futures_create_order,
                            symbol=db_order.symbol,
                            side=order_side,
                            positionSide=order_position_side,
                            type=FUTURE_ORDER_TYPE_MARKET,
                            quantity=executed_qty,
                            # closePosition=True,
                            reduceOnly=True,
                            newClientOrderId=deleting_order_id
                        )
                    else:
                        await self._safe_from_time_err_call_binance(
                            self.binance_client.futures_create_order,
                            symbol=db_order.symbol,
                            side=order_side,
                            positionSide=order_position_side,
                            type=FUTURE_ORDER_TYPE_MARKET,
                            quantity=executed_qty,
                            # closePosition=True,
                            reduceOnly=True
                        )
                except:
                    try:
                        if deleting_order_id:
                            await self._safe_from_time_err_call_binance(
                                self.binance_client.futures_create_order,
                                symbol=db_order.symbol,
                                side=order_side,
                                positionSide=order_position_side,
                                type=FUTURE_ORDER_TYPE_MARKET,
                                quantity=executed_qty,
                                # closePosition=True,
                                newClientOrderId=deleting_order_id
                            )
                        else:
                            await self._safe_from_time_err_call_binance(
                                self.binance_client.futures_create_order,
                                symbol=db_order.symbol,
                                side=order_side,
                                positionSide=order_position_side,
                                type=FUTURE_ORDER_TYPE_MARKET,
                                quantity=executed_qty,
                                # closePosition=True,
                            )
                    except:
                        logging.info('Can\'t delete binance order')

        return

    async def close_all_open_positions(self):
        account_info = await self._safe_from_time_err_call_binance(
            self.binance_client.futures_account
        )
        if not account_info:
            return False

        open_positions = [
            p for p in account_info['positions']
            if Decimal(p['positionAmt']) != 0
        ]

        logging.info(f'open_positions {open_positions}')

        i = 0
        for pos in open_positions:
            position_amt = Decimal(pos['positionAmt'])
            position_side = pos['positionSide']
            symbol = pos['symbol']

            if position_amt > 0:
                side = 'BUY'
            else:
                side = 'SELL'

            db_order = MarketOrder(
                symbol=symbol,
                side=side,
                position_side=position_side,
                open_time=1,
                asset_quantity=abs(position_amt),
                close_order_type=1
            )

            logging.info(f'i {i} deleting open position')

            await self.delete_binance_order(db_order)

            i = i + 1
        logging.info('Closed all open position')

        return True

    async def setting_sl_sw_to_order(self, db_order, bot_config, tick_size):
        sl_sw_params = self._get_sl_sw_params(db_order, bot_config, tick_size)

        sl_custom_trailing = sl_sw_params['sl']['is_need_custom_callback']
        sw_custom_trailing = sl_sw_params['sw']['is_need_custom_callback']

        db_order.close_order_type = FUTURE_ORDER_TYPE_MARKET

        if sl_custom_trailing == sw_custom_trailing:
            if sl_custom_trailing:
                logging.info('Creating custom trailing')
                await self.creating_custom_trailing(db_order, bot_config, tick_size, sl_sw_params)
            else:
                logging.info('Creating binance trailing')
                await self.creating_binance_trailing_order(db_order, bot_config, tick_size, sl_sw_params)
        else:
            logging.info('Creating binance and custom trailings')
            await self.creating_binance_n_custom_trailing(db_order, bot_config, tick_size, sl_sw_params)

        logging.info('order closed')

    async def close_order_by_ma25(self, db_order):
        deleting_order_id = db_order.client_order_id + f'_stop_ma25'

        while db_order.close_time is None:
            current_price = await self.price_provider.get_price(symbol=db_order.symbol)
            ma25 = await self.get_ma(db_order.symbol, 25, current_price)

            is_need_to_close_order = False

            if db_order.side == 'BUY':
                if current_price < ma25:
                    db_order.status = 'CLOSED BY MA25'
                    db_order.close_reason = f'closed MA25 bigger then current price for {db_order.symbol}'
                    logging.info(f'closed MA25 bigger then current price for {db_order.symbol}')

                    is_need_to_close_order = True
            elif db_order.side == 'SELL':
                if current_price > ma25:
                    db_order.status = 'CLOSED BY MA25'
                    db_order.close_reason = f'closed MA25 less then current price for {db_order.symbol}'
                    logging.info(f'closed MA25 less then current price for {db_order.symbol}')

            if is_need_to_close_order:
                await self.delete_order(
                    db_order=db_order,
                    deleting_order_id=deleting_order_id
                )
                break
        return

    def _get_sl_sw_params(self, db_order, bot_config, tick_size):
        if db_order.side == 'BUY':
            side = 'SELL'
            order_position_side = 'LONG'
        else:
            side = 'BUY'
            order_position_side = 'SHORT'

        params = {
            'side': side,
            'order_position_side': order_position_side,
        }

        for key, tick_count in {'sl': bot_config.stop_loss_ticks, 'sw': bot_config.stop_success_ticks}.items():
            tick_value = tick_size * tick_count

            is_need_custom_callback = False

            callback_rate = (tick_value / db_order.activation_price) * 100
            binance_callback_rate = callback_rate
            if callback_rate < 0.1:
                logging.info(f"Минимальный callbackRate на Binance — 0.1%. У тебя {callback_rate}%, увеличь количество тиков.")
                binance_callback_rate = 0.1
                is_need_custom_callback = True
            elif callback_rate > 10:
                binance_callback_rate = 10
                is_need_custom_callback = True

            binance_callback_rate = round(binance_callback_rate, 2)

            params[key] = {
                'callback_rate': callback_rate,
                'binance_callback_rate': binance_callback_rate,
                'is_need_custom_callback': is_need_custom_callback
            }

        return params

    async def creating_custom_trailing(self, db_order, bot_config, tick_size, sl_sw_params, base_order_name=None):
        close_not_lose_price = (
            PriceCalculator.calculate_close_not_lose_price(
                open_price=db_order.open_price, trade_type=db_order.side
            )
        )

        current_stop_type = None
        last_custom_stop_order_name = None
        current_stop_client_order_id_number = 0

        max_price = await self.price_provider.get_price(symbol=db_order.symbol)
        min_price = await self.price_provider.get_price(symbol=db_order.symbol)

        while db_order.close_time is None:
            is_need_so_set_new_sl_sw = False
            updated_price = await self.price_provider.get_price(symbol=db_order.symbol)

            if db_order.side == 'BUY':
                if updated_price > close_not_lose_price:
                    if updated_price > max_price or current_stop_type != 'stop_win':
                        current_stop_client_order_id_number += 1
                        current_stop_type = 'stop_win'
                        is_need_so_set_new_sl_sw = True
                    if updated_price > max_price:
                        max_price = updated_price
                else:
                    if updated_price > max_price or current_stop_type != 'stop_lose':
                        current_stop_client_order_id_number += 1
                        current_stop_type = 'stop_lose'
                        is_need_so_set_new_sl_sw = True
                    if updated_price > max_price:
                        max_price = updated_price
            else:
                if updated_price < close_not_lose_price:
                    if updated_price < min_price or current_stop_type != 'stop_win':
                        current_stop_client_order_id_number += 1
                        current_stop_type = 'stop_win'
                        is_need_so_set_new_sl_sw = True
                    if updated_price < min_price:
                        min_price = updated_price
                else:
                    if updated_price < min_price or current_stop_type != 'stop_lose':
                        current_stop_client_order_id_number += 1
                        current_stop_type = 'stop_lose'
                        is_need_so_set_new_sl_sw = True
                    if updated_price < min_price:
                        min_price = updated_price

            if not base_order_name:
                base_order_name = db_order.client_order_id + f'_{current_stop_type}'

            current_custom_stop_order_name = f'{base_order_name}_custom_{current_stop_client_order_id_number}'

            if current_stop_type == 'stop_win':
                callback = sl_sw_params['sw']
            else:
                callback = sl_sw_params['sl']

            if is_need_so_set_new_sl_sw and callback['is_need_custom_callback']:
                logging.info(f'custom trailing: is need to set new sl sw')
                if last_custom_stop_order_name:
                    logging.info(f'custom trailing: deleting old sl sw')
                    await self.delete_old_sl_sw(
                        db_order=db_order,
                        old_stop_order_id=last_custom_stop_order_name,
                        sl_sw_params=sl_sw_params
                    )
                    last_custom_stop_order_name = None

                is_need_to_stop_order = await self._check_if_price_less_then_stops(
                    close_not_lose_price=close_not_lose_price,
                    bot_config=bot_config,
                    tick_size=tick_size,
                    db_order=db_order
                )

                if not is_need_to_stop_order:
                    logging.info(f'custom trailing: creating new sl sw')
                    order_stop_price = await self._get_order_stop_price_for_custom_trailing(
                        db_order=db_order,
                        stop_type=current_stop_type,
                        prices={'max': max_price, 'min': min_price},
                        bot_config=bot_config,
                        tick_size=tick_size
                    )

                    await self.create_new_sl_sw_order_custom_trailing(
                        db_order=db_order,
                        sl_sw_params=sl_sw_params,
                        client_order_id=current_custom_stop_order_name,
                        order_stop_price=order_stop_price
                    )

                    last_custom_stop_order_name = current_custom_stop_order_name
                else:
                    logging.info(f'custom trailing: When was process of changing stop lose/win - after deleting stop lose/win - price came to stop levels')
                    await self.delete_order(
                        db_order=db_order,
                        status='CLOSED',
                        close_reason=f'When was process of changing stop lose/win - after deleting stop lose/win - price came to stop levels',
                        deleting_order_id=current_custom_stop_order_name
                    )
                    break
            elif is_need_so_set_new_sl_sw and not callback['is_need_custom_callback']:
                logging.info(f'custom trailing: is need to change trailing mode')
                if last_custom_stop_order_name:
                    logging.info(f'custom trailing: deleting old sl sw')
                    await self.delete_old_sl_sw(
                        db_order=db_order,
                        old_stop_order_id=last_custom_stop_order_name,
                        sl_sw_params=sl_sw_params
                    )
                break

        logging.info(f'custom trailing: stopped custom trailing')

        return

    async def creating_binance_trailing_order(self, db_order, bot_config, tick_size, sl_sw_params):
        close_not_lose_price = (
            PriceCalculator.calculate_close_not_lose_price(
                open_price=db_order.open_price, trade_type=db_order.side
            )
        )

        current_stop_type = None
        current_stop_client_order_id_number = 0
        last_stop_order_name = None

        while db_order.close_time is None:
            updated_price = await self.price_provider.get_price(symbol=db_order.symbol)
            is_need_so_set_new_sl_sw = False

            if db_order.side == 'BUY':
                if updated_price > close_not_lose_price and current_stop_type != 'stop_win':
                    current_stop_client_order_id_number += 1
                    current_stop_type = 'stop_win'
                    is_need_so_set_new_sl_sw = True
                elif updated_price < close_not_lose_price and current_stop_type != 'stop_lose':
                    current_stop_client_order_id_number += 1
                    current_stop_type = 'stop_lose'
                    is_need_so_set_new_sl_sw = True
            else:
                if updated_price < close_not_lose_price and current_stop_type != 'stop_win':
                    current_stop_client_order_id_number += 1
                    current_stop_type = 'stop_win'
                    is_need_so_set_new_sl_sw = True
                elif updated_price > close_not_lose_price and current_stop_type != 'stop_lose':
                    current_stop_client_order_id_number += 1
                    current_stop_type = 'stop_lose'
                    is_need_so_set_new_sl_sw = True

            current_stop_order_name = db_order.client_order_id + f'_{current_stop_type}_{current_stop_client_order_id_number}'

            if is_need_so_set_new_sl_sw:
                logging.info(f'binance trailing: is_need_so_set_new_sl_sw')
                if last_stop_order_name:
                    logging.info(f'binance trailing: delete_old_sl_sw')
                    await self.delete_old_sl_sw(
                        db_order=db_order,
                        old_stop_order_id=last_stop_order_name,
                        sl_sw_params=sl_sw_params
                    )

                logging.info(f'binance trailing: _check_if_price_less_then_stops')
                is_need_to_stop_order = await self._check_if_price_less_then_stops(
                    close_not_lose_price=close_not_lose_price,
                    bot_config=bot_config,
                    tick_size=tick_size,
                    db_order=db_order
                )

                if not is_need_to_stop_order:
                    logging.info(f'binance trailing: create_new_sl_sw_order_binance_trailing')
                    await self.create_new_sl_sw_order_binance_trailing(
                        db_order=db_order,
                        sl_sw_params=sl_sw_params,
                        client_order_id=current_stop_order_name,
                        stop_type=current_stop_type
                    )
                    last_stop_order_name = current_stop_order_name
                else:
                    logging.info(f'binance trailing: When was process of changing stop lose/win - after deleting stop lose/win - price came to stop levels')
                    await self.delete_order(
                        db_order=db_order,
                        status='CLOSED',
                        close_reason=f'When was process of changing stop lose/win - after deleting stop lose/win - price came to stop levels',
                        deleting_order_id=current_stop_order_name,
                    )
                    logging.info('When was process of changing stop lose/win - after deleting stop lose/win - price came to stop levels')
                    break

    async def creating_binance_n_custom_trailing(self, db_order, bot_config, tick_size, sl_sw_params):
        close_not_lose_price = (
            PriceCalculator.calculate_close_not_lose_price(
                open_price=db_order.open_price, trade_type=db_order.side
            )
        )

        current_param = None
        current_stop_client_order_id_number = 0
        last_binance_stop_order_name = None

        while db_order.close_time is None:
            updated_price = await self.price_provider.get_price(symbol=db_order.symbol)
            is_need_so_change_mode = False

            if db_order.side == 'BUY':
                if updated_price > close_not_lose_price and current_param != 'sw':
                    current_stop_client_order_id_number += 1
                    current_param = 'sw'
                    is_need_so_change_mode = True
                elif updated_price < close_not_lose_price and current_param != 'sl':
                    current_stop_client_order_id_number += 1
                    current_param = 'sl'
                    is_need_so_change_mode = True
            else:
                if updated_price < close_not_lose_price and current_param != 'sw':
                    current_stop_client_order_id_number += 1
                    current_param = 'sw'
                    is_need_so_change_mode = True
                elif updated_price > close_not_lose_price and current_param != 'sl':
                    current_stop_client_order_id_number += 1
                    current_param = 'sl'
                    is_need_so_change_mode = True

            if current_param == 'sl':
                current_stop_type = 'stop_lose'
            else:
                current_stop_type = 'stop_win'

            current_stop_order_name = db_order.client_order_id + f'_{current_stop_type}_{current_stop_client_order_id_number}'

            if is_need_so_change_mode:
                logging.info(f'2 mods: is_need_so_change_mode')
                if sl_sw_params[current_param]['is_need_custom_callback']:
                    logging.info(f'2 mods: to custom')
                    if last_binance_stop_order_name:
                        logging.info(f'2 mods: delete_old_sl_sw')
                        await self.delete_old_sl_sw(
                            db_order=db_order,
                            old_stop_order_id=last_binance_stop_order_name,
                            sl_sw_params=sl_sw_params
                        )
                        last_binance_stop_order_name = None
                else:
                    logging.info(f'2 mods: to binance')

                logging.info(f'2 mods: _check_if_price_less_then_stops')
                is_need_to_stop_order = await self._check_if_price_less_then_stops(
                    close_not_lose_price=close_not_lose_price,
                    bot_config=bot_config,
                    tick_size=tick_size,
                    db_order=db_order
                )

                if is_need_to_stop_order:
                    logging.info(f'2 mods: is_need_to_stop_order')
                    await self.delete_order(
                        db_order=db_order,
                        status='CLOSED',
                        close_reason=f'When was process of changing stop lose/win - after deleting stop lose/win - price came to stop levels',
                        deleting_order_id=current_stop_order_name,
                    )
                    logging.info('When was process of changing stop lose/win - after deleting stop lose/win - price came to stop levels')
                    break

                if sl_sw_params[current_param]['is_need_custom_callback']:
                    logging.info(f'2 mods: creating_custom_trailing')
                    await self.creating_custom_trailing(db_order, bot_config, tick_size, sl_sw_params, base_order_name=current_stop_order_name)
                else:
                    logging.info(f'2 mods: create_new_sl_sw_order_binance_trailing')
                    await self.create_new_sl_sw_order_binance_trailing(
                        db_order=db_order,
                        sl_sw_params=sl_sw_params,
                        client_order_id=current_stop_order_name,
                        stop_type=current_stop_type
                    )
                    last_binance_stop_order_name = current_stop_order_name

        return

    async def _get_order_stop_price_for_custom_trailing(self, db_order, stop_type, prices, bot_config, tick_size):
        if stop_type == 'stop_win':
            tick_count = bot_config.stop_success_ticks * tick_size
        else:
            tick_count = bot_config.stop_loss_ticks * tick_size

        if db_order.side == 'BUY':
            stop_price = prices['max'] - tick_count
        else:
            stop_price = prices['min'] + tick_count

        return stop_price

    async def create_new_sl_sw_order_custom_trailing(self, db_order, sl_sw_params, client_order_id, order_stop_price):
        trailing_order = MarketOrder(
            symbol=db_order.symbol,
            side=sl_sw_params['side'],
            position_side=sl_sw_params['order_position_side'],
            open_order_type=FUTURE_ORDER_TYPE_TRAILING_STOP_MARKET,
            status='NEW',
            client_order_id=client_order_id
        )

        self.order_update_listener.add_waiting_order(trailing_order)

        try:
            await self._safe_from_time_err_call_binance(
                self.binance_client.futures_create_order,
                symbol=db_order.symbol,
                side=trailing_order.side,
                positionSide=trailing_order.position_side,
                type=FUTURE_ORDER_TYPE_STOP_MARKET,
                # quantity=db_order.asset_quantity,
                closePosition=True,
                stopPrice=order_stop_price,
                reduceOnly=True,
                newClientOrderId=trailing_order.client_order_id,
                workingType="MARK_PRICE",
                priceProtect=True,
            )
        except:
            try:
                await self._safe_from_time_err_call_binance(
                    self.binance_client.futures_create_order,
                    symbol=db_order.symbol,
                    side=trailing_order.side,
                    positionSide=trailing_order.position_side,
                    type=FUTURE_ORDER_TYPE_STOP_MARKET,
                    # quantity=db_order.asset_quantity,
                    closePosition=True,
                    stopPrice=order_stop_price,
                    newClientOrderId=trailing_order.client_order_id,
                    workingType="MARK_PRICE",
                    priceProtect=True,
                )
            except BinanceAPIException as e:
                if e.code == -2021:
                    await self.delete_order(
                        db_order=db_order,
                        status='CLOSED',
                        close_reason=f'When was process of changing custom stop lose/win - price came to stop levels',
                        deleting_order_id=trailing_order.client_order_id
                    )

    async def create_new_sl_sw_order_binance_trailing(self, db_order, sl_sw_params, client_order_id, stop_type):
        trailing_order = MarketOrder(
            symbol=db_order.symbol,
            side=sl_sw_params['side'],
            position_side=sl_sw_params['order_position_side'],
            open_order_type=FUTURE_ORDER_TYPE_TRAILING_STOP_MARKET,
            status='NEW',
            client_order_id=client_order_id
        )

        self.order_update_listener.add_waiting_order(trailing_order)

        if stop_type == 'stop_win':
            callback = sl_sw_params['sw']
        else:
            callback = sl_sw_params['sl']

        try:
            await self._safe_from_time_err_call_binance(
                self.binance_client.futures_create_order,
                symbol=db_order.symbol,
                side=trailing_order.side,
                positionSide=trailing_order.position_side,
                type=FUTURE_ORDER_TYPE_TRAILING_STOP_MARKET,
                quantity=db_order.asset_quantity,
                callbackRate=callback['binance_callback_rate'],
                reduceOnly=True,
                newClientOrderId=trailing_order.client_order_id
            )
        except:
            await self._safe_from_time_err_call_binance(
                self.binance_client.futures_create_order,
                symbol=db_order.symbol,
                side=trailing_order.side,
                positionSide=trailing_order.position_side,
                type=FUTURE_ORDER_TYPE_TRAILING_STOP_MARKET,
                quantity=db_order.asset_quantity,
                callbackRate=callback['binance_callback_rate'],
                newClientOrderId=trailing_order.client_order_id
            )

    async def delete_old_sl_sw(self, db_order, old_stop_order_id, sl_sw_params):
        old_trailing_order = MarketOrder(
            symbol=db_order.symbol,
            side=sl_sw_params['side'],
            position_side=sl_sw_params['order_position_side'],
            open_order_type=FUTURE_ORDER_TYPE_TRAILING_STOP_MARKET,
            client_order_id=old_stop_order_id
        )

        await self.delete_order(db_order=old_trailing_order)

        return

    async def _check_if_price_less_then_stops(self, close_not_lose_price, bot_config, tick_size, db_order):
        current_price = await self.price_provider.get_price(symbol=db_order.symbol)

        sw_tick_value = bot_config.stop_success_ticks * tick_size
        sl_tick_value = bot_config.stop_loss_ticks * tick_size

        if db_order.side == 'BUY':
            sw_price = current_price - sw_tick_value
            sl_price = current_price - sl_tick_value
        else:
            sw_price = current_price + sw_tick_value
            sl_price = current_price + sl_tick_value

        is_need_to_stop_order = False

        if db_order.side == 'BUY':
            if current_price > close_not_lose_price:
                if current_price < sw_price:
                    is_need_to_stop_order = True
            else:
                if current_price < sl_price:
                    is_need_to_stop_order = True
        else:
            if current_price < close_not_lose_price:
                if current_price > sw_price:
                    is_need_to_stop_order = True
            else:
                if current_price > sl_price:
                    is_need_to_stop_order = True

        return is_need_to_stop_order

    async def _get_order_params(
            self, bot_config, balanceUSDT,
            symbol, tick_size, lot_size, max_price, min_price, max_qty, min_qty,
            db_order
    ):
        initial_price = await self.price_provider.get_price(symbol=symbol)

        if bot_config.start_updown_ticks:
            entry_price_buy = initial_price + bot_config.start_updown_ticks * tick_size
            entry_price_buy_str = self._round_price_for_order(price=entry_price_buy, tick_size=tick_size)

            entry_price_sell = initial_price - bot_config.start_updown_ticks * tick_size
            entry_price_sell_str = self._round_price_for_order(price=entry_price_sell, tick_size=tick_size)

            if any([
                entry_price_buy > max_price,
                entry_price_buy < min_price,
                entry_price_sell > max_price,
                entry_price_sell < min_price
            ]):
                logging.info(f'Price bigger or less then maximums for {symbol}')

                db_order.status = 'CANCELED'
                db_order.close_reason = f'Price bigger or less then maximums for {symbol}'

                return None

            quantityOrder_buy_str = self._calculate_quantity_for_order(amount=balanceUSDT, price=entry_price_buy, lot_size=lot_size)
            quantityOrder_sell_str = self._calculate_quantity_for_order(amount=balanceUSDT, price=entry_price_sell, lot_size=lot_size)

            if any([
                Decimal(quantityOrder_buy_str) > max_qty,
                Decimal(quantityOrder_buy_str) < min_qty,
                Decimal(quantityOrder_sell_str) > max_qty,
                Decimal(quantityOrder_sell_str) < min_qty
            ]):
                logging.info(f'Quantity bigger or less then maximums for {symbol}')

                db_order.status = 'CANCELED'
                db_order.close_reason = f'Quantity bigger or less then maximums for {symbol}'

                return None
            return {
                'initial_price': initial_price,
                'entry_price_buy_str': entry_price_buy_str,
                'entry_price_sell_str': entry_price_sell_str,
                'quantityOrder_buy_str': quantityOrder_buy_str,
                'quantityOrder_sell_str': quantityOrder_sell_str,
            }
        else:
            quantityOrder_buy_str = self._calculate_quantity_for_order(amount=balanceUSDT, price=initial_price, lot_size=lot_size)
            quantityOrder_sell_str = self._calculate_quantity_for_order(amount=balanceUSDT, price=initial_price, lot_size=lot_size)

            return {
                'initial_price': initial_price,
                'quantityOrder_buy_str': quantityOrder_buy_str,
                'quantityOrder_sell_str': quantityOrder_sell_str,
            }



    async def _safe_from_time_err_call_binance(self, func, *args, max_retries=20, retry_delay=1, **kwargs):
        for attempt in range(1, max_retries + 1):
            try:
                return func(*args, **kwargs)
            except BinanceAPIException as e:
                if e.code == -1021:
                    logging.info(f"Try {attempt}/{max_retries}: Error Binance API: {e}")
                    if attempt == max_retries:
                        raise
                    time.sleep(retry_delay)
                else:
                    raise

    async def _wait_until_order_activated(self, bot_config, db_order_buy, db_order_sell):
        timeout_missed = True
        first_order_updating_data = None

        if not bot_config.consider_ma_for_open_order:
            try:
                timeout = Decimal(bot_config.time_to_wait_for_entry_price_to_open_order_in_minutes)
                timeout = int(timeout * 60)

                first_order_updating_data = await asyncio.wait_for(
                    self._wait_activating_of_order(),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                timeout_missed = False

            if not timeout_missed:
                for db_order in [db_order_buy, db_order_sell]:
                    if db_order.close_reason is None and db_order.exchange_order_id:
                        await self.delete_order(
                            db_order=db_order,
                            status='CANCELED',
                            close_reason=f'A minute has passed, entry conditions have not been met'
                        )

        return {
            'timeout_missed': timeout_missed,
            'first_order_updating_data': first_order_updating_data,
        }

    async def _wait_until_order_filled(self, bot_config, db_order):
        timeout_missed = True
        first_order_updating_data = None

        try:
            timeout = Decimal(bot_config.time_to_wait_for_entry_price_to_open_order_in_minutes)
            timeout = int(timeout * 60)

            first_order_updating_data = await asyncio.wait_for(
                self._wait_filling_of_order(),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            timeout_missed = False

        if not timeout_missed:
            await self.delete_order(
                db_order=db_order,
                status='CANCELED',
                close_reason=f'A minute has passed, order did\'nt fill'
            )

        return {
            'timeout_missed': timeout_missed,
            'first_order_updating_data': first_order_updating_data,
        }

    async def _wait_activating_of_order(self):
        first_order_updating_data = await self.order_update_listener.get_first_started_order()

        return first_order_updating_data

    async def _wait_filling_of_order(self):
        first_order_updating_data = await self.order_update_listener.get_first_order_filled()

        return first_order_updating_data

    async def _get_copy_bot_tf_params(self):
        copy_bot = None
        copy_bot_id = None

        profits_data = await self.bot_crud.get_sorted_by_profit(since=timedelta(hours=1), just_copy_bots=True)
        profits_data_filtered_sorted = sorted([item for item in profits_data if item[1] > 0], key=lambda x: x[1], reverse=True)

        try:
            copy_bot_id = profits_data_filtered_sorted[0][0]
        except (IndexError, TypeError):
            pass

        if copy_bot_id:
            copy_bots = await self.session.execute(
                select(TestBot)
                .where(
                    TestBot.id == copy_bot_id,
                )
            )
            copy_bots = copy_bots.scalars().all()
            if copy_bots:
                copy_bot = copy_bots[0]

        return copy_bot

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

    async def get_ma(self, symbol, ma_number, current_price = None):
        try:
            klines = await self._safe_from_time_err_call_binance(
                self.binance_client.futures_klines,
                symbol=symbol, interval=Client.KLINE_INTERVAL_1MINUTE, limit=ma_number
            )
        except Exception as e:
            logging.info(f'Symbol: {symbol}, error when get klines: {e}')
            logging.error(f'Symbol: {symbol}, error when get klines: {e}')
            return None

        if not current_price:
            if not hasattr(self, 'price_provider'):
                async with redis_context() as redis:
                    self.price_provider = PriceProvider(redis)

            current_price = await self.price_provider.get_price(symbol=symbol)

        closes = [Decimal(kline[4]) for kline in klines]
        closes.append(current_price)
        ma_window = ma_number + 1
        ma = sum(closes) / Decimal(ma_window)

        return ma
