import asyncio
import os

import websockets
import json
from decimal import Decimal
from datetime import datetime, timedelta, timezone
import logging

from dotenv import load_dotenv

UTC = timezone.utc

class UserDataWebSocketClient:
    def __init__(self, binance_client, waiting_orders=None):
        if waiting_orders is None:
            waiting_orders = []
        self.client = binance_client
        self.listen_key = self.client.futures_stream_get_listen_key()

        load_dotenv()
        self.is_prod = os.getenv("ENVIRONMENT") == "prod"
        if self.is_prod:
            # market
            domain = "fstream.binance.com"
        else:
            # testnet
            domain = "stream.binancefuture.com"
        self.url = f"wss://{domain}/ws/{self.listen_key}"

        self.first_order_started_event = asyncio.Event()
        self.first_order_filled_event = asyncio.Event()
        self.connected_event = asyncio.Event()
        self.keep_running = False
        self.first_order = None
        self.waiting_orders = {}
        self.waiting_orders_id = []

        for order in waiting_orders:
            self.waiting_orders_id.append(order.client_order_id)
            self.waiting_orders[order.client_order_id] = order

        logging.basicConfig(
            format='%(asctime)s - %(levelname)s - %(message)s',
            level=logging.INFO
        )

    async def connect(self):
        while self.keep_running:
            try:
                async with websockets.connect(self.url) as websocket:
                    self.connected_event.set()
                    logging.info("🔌 Подключено к USER DATA STREAM")
                    while self.keep_running:
                        msg = await websocket.recv()
                        data = json.loads(msg)

                        if data.get("e") == "ORDER_TRADE_UPDATE":
                            await self.handle_order_update(data["o"])
            except Exception as e:
                logging.info(f"❌ Ошибка WebSocket: {e}")
                await asyncio.sleep(5)

    async def keep_alive(self):
        """Периодически обновляем listenKey, чтобы не истек"""
        try:
            while self.keep_running:
                await asyncio.sleep(30 * 60)
                self.client.futures_stream_keepalive(listenKey=self.listen_key)
        except Exception as e:
            logging.info(f"⚠️ Ошибка в keep-alive: {e}")
            self.listen_key = self.client.futures_stream_get_listen_key()

    async def start(self):
        self.keep_running = True
        asyncio.create_task(self.keep_alive())
        asyncio.create_task(self.connect())

        await self.connected_event.wait()

    def stop(self):
        self.keep_running = False

        try:
            self.client.futures_stream_close(listenKey=self.listen_key)
            logging.info("🛑 listenKey закрыт через REST.")
        except Exception as e:
            logging.info(f"⚠️ Ошибка при закрытии listenKey: {e}")

    async def handle_order_update(self, order):
        logging.info("📦 Обновление ордера:")
        logging.info(
            " | ".join(
                [
                    f"Символ: {order.get('s')}",
                    f"ClientOrderID: {order.get('c')}",
                    f"OrderID: {order.get('i')}",
                    f"Статус: {order.get('X')}",
                    f"Событие: {order.get('x')}",
                    f"Тип: {order.get('o')}",
                    f"Side: {order.get('S')}",
                    f"PositionSide: {order.get('ps')}",
                    f"Кол-во: {order.get('q')}",
                    f"Цена: {order.get('p')}",
                    f"StopPrice: {order.get('sp')}",
                    f"ActivationPrice(AP): {order.get('AP')}",
                    f"CallbackRate: {order.get('cr')}",
                    f"LastPrice: {order.get('L')}",
                    f"reduceOnly: {order.get('R')}",
                    f"PostOnly: {order.get('pP')}",
                    f"RealizedProfit: {order.get('rp')}",
                    f"MarginType: {order.get('pm')}",
                    f"Time: {order.get('T')}",
                ]
            )
        )

        if order['c'] not in self.waiting_orders_id:
            return
        if not self.first_order_started_event.is_set() or (self.first_order and order['i'] == self.first_order['i']):
            self.first_order = order

        if (
                (order['X'] == 'EXPIRED' or order['X'] == 'PARTIALLY_FILLED' or order['X'] == 'FILLED')
                and not self.first_order_started_event.is_set()
        ):
            self.first_order_started_event.set()

        if order['X'] == 'FILLED':
            if self.first_order and order['i'] == self.first_order['i']:
                self.first_order_filled_event.set()

        current_order = self.waiting_orders[order['c']]

        current_order.exchange_status = order['X']
        current_order.status = order['X']

        if order['X'] == 'EXPIRED':
            current_order.activation_price = Decimal(order['sp'])
            current_order.activation_time = datetime.now(UTC).replace(tzinfo=None)

        original_order = None

        # if 'stop' in order['c']:
        #     first_underscore_index = order['c'].find('_')
        #     second_underscore_index = order['c'].find('_', first_underscore_index + 1)
        #     if second_underscore_index != -1:
        #         original_order_id = order['c'][:second_underscore_index]
        #         order['c'] = f'{original_order_id}_stop_ma25'

        if 'stop' in order['c']:
            first_underscore_index = order['c'].find('_')
            second_underscore_index = order['c'].find('_', first_underscore_index + 1)
            if second_underscore_index != -1:
                original_order_id = order['c'][:second_underscore_index]
            else:
                original_order_id = order['c']

            logging.info(f'order[\'c\'] {order['c']}')
            logging.info(f'original_order_id {original_order_id}')

            original_order = self.waiting_orders.get(original_order_id)

        if order['X'] == 'FILLED':
            if original_order:
                original_order.close_price = Decimal(order['L'])
                original_order.close_time = datetime.now(UTC).replace(tzinfo=None)
                logging.info(f'In user data stream order {order['c']} closed')

                if 'win' in order['c']:
                    original_order.close_reason = 'Stop win'
                elif 'lose' in order['c']:
                    original_order.close_reason = 'Stop lose'

                if 'custom' in order['c']:
                    original_order.close_reason = f'{original_order.close_reason} custom'
            else:
                current_order.open_price = Decimal(order['L'])
                current_order.open_time = datetime.now(UTC).replace(tzinfo=None)

        if Decimal(order['rp']) and original_order:
            if original_order.profit_loss is None:
                original_order.profit_loss = 0
            original_order.profit_loss = original_order.profit_loss + Decimal(order['rp'])

        if Decimal(order['n']) > 0:
            if original_order:
                if original_order.close_commission is None:
                    original_order.close_commission = 0
                original_order.close_commission = original_order.close_commission + Decimal(order['n'])
            elif current_order.open_order_type:
                if current_order.open_commission is None:
                    current_order.open_commission = 0
                current_order.open_commission = current_order.open_commission + Decimal(order['n'])

    async def get_first_started_order(self):
        if self.first_order_started_event.is_set():
            return self.first_order

        await self.first_order_started_event.wait()

        return self.first_order

    async def get_first_order_filled(self):
        if self.first_order_filled_event.is_set():
            return self.first_order

        await self.first_order_filled_event.wait()

        return self.first_order

    def add_waiting_order(self, order):
        if order.client_order_id not in self.waiting_orders:
            self.waiting_orders_id.append(order.client_order_id)
            self.waiting_orders[order.client_order_id] = order
            logging.info(f"✅ Ордер с client_order_id '{order.client_order_id}' добавлен в ожидающие.")
        else:
            logging.info(f"⚠️ Ордер с client_order_id '{order.client_order_id}' уже существует в ожидающих ордерах. Игнорируем добавление.")