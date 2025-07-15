import asyncio
import websockets
import json

class UserDataWebSocketClient:
    def __init__(self, binance_client, waiting_orders_id):
        self.client = binance_client
        self.listen_key = self.client.futures_stream_get_listen_key()
        # market
        # domain = "fstream.binance.com"
        # testnet
        domain = "stream.binancefuture.com"
        self.url = f"wss://{domain}/ws/{self.listen_key}"
        self.first_order_started_event = asyncio.Event()
        self.keep_running = True
        self.first_order = None
        self.waiting_orders_id = waiting_orders_id

    async def connect(self):
        while self.keep_running:
            try:
                async with websockets.connect(self.url) as websocket:
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

    def stop(self):
        self.keep_running = False

        try:
            self.client.futures_stream_close(listenKey=self.listen_key)
            print("🛑 listenKey закрыт через REST.")
        except Exception as e:
            print(f"⚠️ Ошибка при закрытии listenKey: {e}")

    async def handle_order_update(self, order):
        if order['c'] not in self.waiting_orders_id:
            return
        if not self.first_order_started_event.is_set() or (self.first_order and order['i'] == self.first_order['i']):
            self.first_order = order

        print("📦 Обновление ордера:")
        print(f"  Статус: {order['X']}")
        print(f"  Тип: {order['o']}")
        print(f"  Side: {order['S']}")
        print(f"  Цена активации: {order.get('sp', '—')}")
        print(f"  Был ли активирован: {order.get('wt')}")
        print(f"  Triggered: {order.get('ps', '—')}")

        if order['X'] == 'EXPIRED' and not self.first_order_started_event.is_set():
            self.first_order_started_event.set()

    async def get_first_started_order(self):
        """Асинхронно возвращает первый полученный ордер"""
        if self.first_order_started_event.is_set():
            return self.first_order

        await self.first_order_started_event.wait()

        return self.first_order