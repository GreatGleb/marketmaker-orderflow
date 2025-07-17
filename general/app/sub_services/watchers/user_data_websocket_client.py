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
        # market
        # domain = "fstream.binance.com"
        # testnet
        domain = "stream.binancefuture.com"
        self.url = f"wss://{domain}/ws/{self.listen_key}"
        self.first_order_started_event = asyncio.Event()
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

        self.waiting_orders[order['c']].exchange_status = order['X']

        self.waiting_orders[order['c']].status = order['X']
        if Decimal(order['n']) > 0:
            if self.waiting_orders[order['c']].open_order_type:
                if self.waiting_orders[order['c']].open_commission is None:
                    self.waiting_orders[order['c']].open_commission = 0
                self.waiting_orders[order['c']].open_commission = self.waiting_orders[order['c']].open_commission + Decimal(order['n'])
            elif self.waiting_orders[order['c']].close_order_type:
                if self.waiting_orders[order['c']].close_commission is None:
                    self.waiting_orders[order['c']].close_commission = 0
                self.waiting_orders[order['c']].close_commission = self.waiting_orders[order['c']].close_commission + Decimal(order['n'])

        if order['X'] == 'FILLED' or order['X'] == 'PARTIALLY_FILLED':
            if '_close' in order['c']:
                self.waiting_orders[order['c']].close_price = Decimal(order['L'])
                self.waiting_orders[order['c']].close_time = datetime.now(UTC).replace(tzinfo=None)
            else:
                self.waiting_orders[order['c']].open_price = Decimal(order['L'])
                self.waiting_orders[order['c']].open_time = datetime.now(UTC).replace(tzinfo=None)

        if order['X'] == 'EXPIRED':
            self.waiting_orders[order['c']].activation_price = Decimal(order['sp'])
            self.waiting_orders[order['c']].activation_time = datetime.now(UTC).replace(tzinfo=None)

        if order['X'] == 'EXPIRED' and not self.first_order_started_event.is_set():
            self.first_order_started_event.set()

    async def get_first_started_order(self):
        if self.first_order_started_event.is_set():
            return self.first_order

        await self.first_order_started_event.wait()

        return self.first_order