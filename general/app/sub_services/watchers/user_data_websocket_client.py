import asyncio
import websockets
import json
from decimal import Decimal
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

class UserDataWebSocketClient:
    def __init__(self, binance_client, waiting_orders):
        self.client = binance_client
        self.listen_key = self.client.futures_stream_get_listen_key()
        # testnet
        # domain = "stream.binancefuture.com"
        # market
        domain = "fstream.binance.com"
        self.url = f"wss://{domain}/ws/{self.listen_key}"
        self.first_order_started_event = asyncio.Event()
        self.first_order_filled_event = asyncio.Event()
        self.connected_event = asyncio.Event()
        self.keep_running = True
        self.first_order = None
        self.waiting_orders = {}
        self.waiting_orders_id = []

        for order in waiting_orders:
            self.waiting_orders_id.append(order.client_order_id)
            self.waiting_orders[order.client_order_id] = order

    async def connect(self):
        while self.keep_running:
            try:
                async with websockets.connect(self.url) as websocket:
                    self.connected_event.set()
                    print("🔌 Подключено к USER DATA STREAM")
                    while self.keep_running:
                        msg = await websocket.recv()
                        data = json.loads(msg)

                        if data.get("e") == "ORDER_TRADE_UPDATE":
                            await self.handle_order_update(data["o"])
            except Exception as e:
                print("❌ Ошибка WebSocket:", e)
                await asyncio.sleep(5)

    async def keep_alive(self):
        """Периодически обновляем listenKey, чтобы не истек"""
        try:
            while self.keep_running:
                await asyncio.sleep(30 * 60)
                self.client.futures_stream_keepalive(listenKey=self.listen_key)
        except Exception as e:
            print("⚠️ Ошибка в keep-alive:", e)

    async def start(self):
        asyncio.create_task(self.keep_alive())
        asyncio.create_task(self.connect())

        await self.connected_event.wait()

    def stop(self):
        self.keep_running = False

        try:
            self.client.futures_stream_close(listenKey=self.listen_key)
            print("🛑 listenKey закрыт через REST.")
        except Exception as e:
            print(f"⚠️ Ошибка при закрытии listenKey: {e}")

    async def handle_order_update(self, order):
        print("📦 Обновление ордера:")
        print(f"  id: {order['c']}")
        print(f"  Статус: {order['X']}")
        print(f"  Тип: {order['o']}")
        print(f"  Side: {order['S']}")
        print(f"  Цена активации: {order.get('sp', '—')}")
        print(f"  Цена последней сделки: {order.get('L', '—')}")
        print(f"  Triggered: {order.get('ps', '—')}")

        if order['c'] not in self.waiting_orders_id:
            return
        if not self.first_order_started_event.is_set() or (self.first_order and order['i'] == self.first_order['i']):
            self.first_order = order

        if order['X'] == 'EXPIRED' and not self.first_order_started_event.is_set():
            self.first_order_started_event.set()

        current_order = self.waiting_orders[order['c']]

        current_order.exchange_status = order['X']
        current_order.status = order['X']

        if order['X'] == 'EXPIRED':
            current_order.activation_price = Decimal(order['sp'])
            current_order.activation_time = datetime.now(UTC).replace(tzinfo=None)

        original_order = None

        if 'stop' in order['c']:
            first_underscore_index = order['c'].find('_')
            second_underscore_index = order['c'].find('_', first_underscore_index + 1)
            if second_underscore_index != -1:
                original_order_id = order['c'][:second_underscore_index]
            else:
                original_order_id = order['c']

            original_order = self.waiting_orders.get(original_order_id)

        if order['X'] == 'FILLED':
            if self.first_order and order['i'] == self.first_order['i']:
                self.first_order_filled_event.set()

            if original_order:
                original_order.close_price = Decimal(order['L'])
                original_order.close_time = datetime.now(UTC).replace(tzinfo=None)

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
            print(f"✅ Ордер с client_order_id '{order.client_order_id}' добавлен в ожидающие.")
        else:
            print(f"⚠️ Ордер с client_order_id '{order.client_order_id}' уже существует в ожидающих ордерах. Игнорируем добавление.")