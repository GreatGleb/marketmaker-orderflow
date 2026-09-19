import asyncio
import json
import logging
import time
import traceback
from collections import namedtuple

from datetime import datetime, timezone
from decimal import Decimal, DecimalException, ROUND_FLOOR, localcontext

from fastapi import Depends

from redis.asyncio import Redis

from app.config import settings
from app.bots.binance_bot import BinanceBot
from app.constants.order import ORDER_QUEUE_KEY
from app.constants.volatility import most_volatile_symbol_key
from app.crud.exchange_pair_spec import AssetExchangeSpecCrud
from app.crud.test_bot import TestBotCrud
from app.db.models import TestBot, TestOrder
from app.dependencies import get_redis
from app.db.base import DatabaseSessionManager
from app.constants.commissions import COMMISSION_OPEN
from app.enums.event_type import StopReasonEvent
from app.enums.trade_type import TradeType
from app.sub_services.logic.market_setup import (
    LOT_DATA_KEYS,
    MarketDataBuilder,
)
from app.sub_services.logic.price_calculator import PriceCalculator
from app.sub_services.logic.quantity_grid import quantity_grid
from app.constants.strategy import algorithm_version_for
from app.crud.strategy import StrategyCrud
from app.sub_services.logic.donor_selection import DonorChanged, DonorGuard, read_donor
from app.sub_services.watchers.price_provider import (
    PriceCache,
    PriceWatcher,
    PriceProvider,
)
from app.utils import Command
from app.sub_services.logic.exit_strategy import ExitStrategy
from app.workers.profitable_bot_updater import ProfitableBotUpdaterCommand
from app.sub_services.notifications.factory import NotificationServiceFactory

UTC = timezone.utc


class Refusal:
    """Почему компаундирующий копибот v3 не может поставить ордер.

    Вид важнее текста. `shortage` — на счёте не набирается лот, и только это
    говорит что-то о состоянии бота. `pair` и `data` — про конкретную пару:
    копибот торгует парой донора, донор меняется от сделки к сделке, поэтому
    отказ на одной паре ничего не значит для следующей.

    Без этого различения бот останавливался бы навсегда из-за одной экзотической
    пары или незасеянных спеков, а прогноз обрывался бы на ровном месте — и
    выглядело бы это как настоящий слив счёта.
    """

    def __init__(self, kind: str, text: str):
        self.kind = kind
        self.text = text

    @property
    def is_shortage(self) -> bool:
        return self.kind == "shortage"

    def __str__(self) -> str:
        return self.text


class StartTestBotsCommand(Command):

    # Замер 2026-08-27: голый цикл держит около 70 тысяч пробуждений корутин
    # в секунду на ядро, то есть 7 тысяч ботов с тактом 100 мс. Настоящий бот
    # ещё считает условия выхода на Decimal, поэтому рабочее правило — 4–7
    # тысяч на процесс, а это порог, за которым такт точно поедет.
    MAX_BOTS_PER_SHARD = 7_000

    def __init__(self, stop_event, shard: int = 0, shards: int = 1):
        """shard/shards — доля парка, которую ведёт этот процесс.

        Один событийный цикл вытягивает около 70 тысяч пробуждений корутин
        в секунду на ядро, а 17 236 ботов с тактом 100 мс требуют 172 тысяч.
        Поэтому симулятор запускается несколькими процессами, каждый берёт
        ботов с `id % shards == shard`. Подробности — пункт 1.2 в
        .ai/docs/test-bots/09-roadmap.md.
        """
        super().__init__()
        self.stop_event = stop_event
        self.shard = shard
        self.shards = shards
        # Счета компаундирующих ботов v3 и те из них, кому уже не хватает на
        # минимальный лот. В конфиге их держать нельзя: `_to_bot_objects`
        # отдаёт namedtuple. Состояние дублируется в `test_bots`, чтобы
        # пережить перезапуск процесса.
        self._v3_balances: dict[int, Decimal] = {}
        self._v3_stopped: set[int] = set()
        # Сколько попыток подряд упёрлись в нехватку. Обнуляется удачной
        # сделкой: подряд — значит подряд.
        self._v3_shortages: dict[int, int] = {}
        # id стратегии -> ключ. Нужна, чтобы записать версию алгоритма в
        # сделку: id выдаёт база, а версия привязана к ключу.
        self._strategy_keys: dict[int, str] = {}
        # Логи всех шардов лежат в разных файлах, но при чтении их вместе
        # (или в консоли ручного запуска) без пометки не разобрать, чей это.
        self.log_prefix = f'[шард {shard}/{shards}] ' if shards > 1 else ''

    @staticmethod
    def _to_bot_objects(active_bots):
        """ORM-объекты -> namedtuple'ы с теми же полями.

        Отвязывает конфиг бота от сессии: боты живут весь процесс, а сессия
        закрывается сразу после старта.
        """
        active_bots_dicts = [
            {
                key: value
                for key, value in bot.__dict__.items()
                if key != '_sa_instance_state'
            }
            for bot in active_bots
        ]

        BotObject = namedtuple('BotObject', active_bots_dicts[0].keys())

        return [BotObject(**bot) for bot in active_bots_dicts]

    async def command(
        self,
        redis: Redis = Depends(get_redis),
    ):
        logging.basicConfig(
            format='%(asctime)s - %(levelname)s - %(message)s',
            level=logging.INFO
        )

        # Один MGET раз в 50 мс на весь процесс вместо GET на каждого бота
        # каждые 100 мс. Без этого такт цикла удержания растягивается в разы:
        # см. .ai/docs/test-bots/09-roadmap.md, пункт 1.1.
        price_cache = PriceCache(redis=redis)
        price_cache.start()

        price_provider = PriceProvider(redis=redis, cache=price_cache)
        binance_bot = BinanceBot(is_need_prod_for_data=True, redis=redis)

        await asyncio.sleep(60)

        dsm = DatabaseSessionManager.create(settings.DB_URL)

        active_bots_tuples = []
        shared_data = {}

        # Без активных ботов собирать namedtuple не из чего, и раньше здесь
        # падал IndexError, роняя весь процесс: supervisor перезапускал его
        # по кругу. Ждём, пока боты появятся в test_bots.
        while not self.stop_event.is_set():
            # Сессия — на одну попытку, а не из зависимостей. Зависимости
            # решаются один раз в Command.run_async, а command() кончается
            # только на gather ниже: сессия из Depends жила бы, сколько живёт
            # процесс, и держала бы открытую транзакцию с первого же чтения.
            # Снапшот такой транзакции не даёт autovacuum вычистить мёртвые
            # строки, и весь ретеншн по test_orders отменяется — в каждом
            # шарде свой такой снапшот. См. 08-gotchas.md, пункт 19.
            async with dsm.get_session() as session:
                active_bots = await TestBotCrud(session).get_active_bots(
                    shard=self.shard, shards=self.shards
                )
                self._strategy_keys = await StrategyCrud(session).keys_by_id()

                if active_bots:
                    logging.info(
                        f'{self.log_prefix}Ботов в этом процессе: '
                        f'{len(active_bots)}.'
                    )

                    if len(active_bots) > self.MAX_BOTS_PER_SHARD:
                        logging.warning(
                            f'{self.log_prefix}Это больше '
                            f'{self.MAX_BOTS_PER_SHARD} ботов на процесс: '
                            f'событийный цикл не успеет обойти их за 100 мс, '
                            f'условия выхода начнут проверяться реже, чем '
                            f'задумано. Увеличьте TEST_BOTS_SHARDS в .env и '
                            f'пересоздайте контейнер.'
                        )

                    # Пока сессия жива: дальше боты работают на копиях
                    # конфига, а не на ORM-объектах, привязанных к ней.
                    active_bots_tuples = self._to_bot_objects(active_bots)

                    # Остановленные боты v3 остаются активными, чтобы не
                    # выпасть из отчётов, — значит после перезапуска они снова
                    # попадут в парк. Отметку времени читаем сразу, иначе
                    # такой бот молча возобновил бы торговлю с нуля.
                    self._v3_stopped = {
                        bot.id
                        for bot in active_bots_tuples
                        if getattr(bot, 'copybot_v3_stopped_at', None)
                    }

                    if self._v3_stopped:
                        logging.info(
                            f'{self.log_prefix}Копиботы v3 остановлены ранее '
                            f'и торговать не будут: '
                            f'{sorted(self._v3_stopped)}.'
                        )

                    shared_data = await MarketDataBuilder(session).build()

                    logging.info(shared_data)

                    break

            # У шарда своя доля парка: боты могут существовать, но не
            # попадать в его остаток. Иначе сообщение врёт.
            scope = (
                'в своей доле парка' if self.shards > 1 else 'в test_bots'
            )

            logging.info(
                f'{self.log_prefix}Нет активных ботов {scope}, жду 60 с. '
                f'Создать их: python -m app.scripts.new_bots'
            )
            # Сон вне сессии: в паузе процесс не держит ни соединения из
            # пула, ни транзакции.
            await asyncio.sleep(60)

        if self.stop_event.is_set():
            logging.info(f'{self.log_prefix}Остановлено до запуска ботов.')
            return

        tasks = []

        for bot in active_bots_tuples:
            async def _run_loop(bot_config):
                while not self.stop_event.is_set():
                    try:
                        await self.simulate_bot(
                            original_bot_config=bot_config,
                            shared_data=shared_data,
                            redis=redis,
                            stop_event=self.stop_event,
                            price_provider=price_provider,
                            binance_bot=binance_bot,
                        )
                    except Exception as e:
                        error_traceback = traceback.format_exc()
                        logging.info(error_traceback)
                        await self._notify_bot_error(
                            bot_id=bot_config.id,
                            error=e,
                            error_traceback=error_traceback,
                        )
                        await asyncio.sleep(1)

            tasks.append(asyncio.create_task(_run_loop(bot)))

        await asyncio.gather(*tasks)

    # Уведомления об ошибках ботов. Всё общее на процесс: при системном
    # сбое (упал Redis, отвалилась база) в одну и ту же ошибку утыкаются все
    # боты сразу, и без троттлинга это тысячи одинаковых сообщений в минуту —
    # Telegram забанит бота, а причину сбоя в потоке будет не найти.
    # Тот же приём, что в PriceProvider._log_missing_price.
    ERROR_NOTIFY_INTERVAL_SECONDS = 300
    # Сколько разных ошибок помним. Текст ошибки может содержать меняющиеся
    # данные (id, цены), поэтому ключей бывает много — при переполнении
    # выкидываем те, чей интервал уже истёк.
    ERROR_NOTIFY_MAX_KEYS = 500

    # подпись ошибки -> когда последний раз отправляли
    _last_error_notification: dict[str, float] = {}
    # подпись ошибки -> сколько сообщений подавили с прошлой отправки
    _suppressed_error_notifications: dict[str, int] = {}

    @classmethod
    def _forget_expired_error_notifications(cls, now: float) -> None:
        expired = [
            key
            for key, last in cls._last_error_notification.items()
            if now - last >= cls.ERROR_NOTIFY_INTERVAL_SECONDS
        ]

        for key in expired:
            cls._last_error_notification.pop(key, None)
            cls._suppressed_error_notifications.pop(key, None)

    @classmethod
    async def _notify_bot_error(
        cls,
        bot_id: int,
        error: Exception,
        error_traceback: str,
    ) -> None:
        # Ключ — тип и текст ошибки, но не id бота: при системном сбое
        # интересна сама ошибка, а не каждый из ботов, который в неё попал.
        key = f"{type(error).__name__}: {error}"
        now = time.monotonic()
        last = cls._last_error_notification.get(key)

        if last is not None and now - last < cls.ERROR_NOTIFY_INTERVAL_SECONDS:
            cls._suppressed_error_notifications[key] = (
                cls._suppressed_error_notifications.get(key, 0) + 1
            )
            return

        if len(cls._last_error_notification) >= cls.ERROR_NOTIFY_MAX_KEYS:
            cls._forget_expired_error_notifications(now)

        cls._last_error_notification[key] = now
        suppressed = cls._suppressed_error_notifications.pop(key, 0)

        additional_info = f"Полный стек ошибки:\n{error_traceback}"

        if suppressed:
            interval_minutes = cls.ERROR_NOTIFY_INTERVAL_SECONDS / 60
            additional_info = (
                f"🔁 Подавлено таких же сообщений за последние "
                f"{interval_minutes:.0f} мин: {suppressed}\n\n"
                f"{additional_info}"
            )

        try:
            telegram_service = NotificationServiceFactory.get_telegram_service()

            if telegram_service:
                await telegram_service.send_bot_error_notification(
                    bot_id=bot_id,
                    error_message=str(error),
                    additional_info=additional_info,
                )
        except Exception as telegram_error:
            logging.info(
                f"❌ Ошибка при отправке уведомления в Telegram: {telegram_error}"
            )

    # Догрузка рыночных данных по паре, которой не оказалось в снимке.
    # Всё общее на процесс: в волатильном режиме сотни ботов утыкаются в одну
    # и ту же новую пару одновременно.
    _market_data_lock = asyncio.Lock()
    # symbol -> когда последний раз не нашли. Служит и кэшем «не искать
    # заново», и throttle-ом для лога.
    _market_data_misses: dict[str, float] = {}
    MARKET_DATA_MISS_TTL_SECONDS = 300

    @staticmethod
    def _has_tick_size(data) -> bool:
        return bool(data) and data.get("tick_size") is not None

    @classmethod
    async def get_market_data(cls, symbol, shared_data):
        """tick_size и ставки комиссии по паре.

        shared_data строится один раз на старте, а волатильный режим выбирает
        пару динамически и может наткнуться на появившуюся позже. Тогда
        догружаем и кладём в тот же словарь — платит только первый бот.
        """
        data = shared_data.get(symbol)

        if cls._has_tick_size(data):
            return data

        now = time.monotonic()
        last_miss = cls._market_data_misses.get(symbol)

        if (
            last_miss is not None
            and now - last_miss < cls.MARKET_DATA_MISS_TTL_SECONDS
        ):
            # Уже искали и не нашли — не ходим в БД снова.
            return None

        async with cls._market_data_lock:
            # Пока ждали блокировку, пару мог догрузить другой бот...
            data = shared_data.get(symbol)

            if cls._has_tick_size(data):
                return data

            # ...либо уже сходить за ней и не найти. Без этой проверки все
            # ожидавшие блокировку по очереди повторили бы один и тот же
            # бесполезный запрос.
            last_miss = cls._market_data_misses.get(symbol)

            if (
                last_miss is not None
                and time.monotonic() - last_miss
                < cls.MARKET_DATA_MISS_TTL_SECONDS
            ):
                return None

            dsm = DatabaseSessionManager.create(settings.DB_URL)

            async with dsm.get_session() as session:
                market_data = await AssetExchangeSpecCrud(
                    session
                ).get_market_data_by_symbol(symbol)

            if not cls._has_tick_size(market_data):
                cls._market_data_misses[symbol] = now
                logging.info(
                    f"❌ Нет рыночных данных по паре {symbol}: не найден "
                    f"tick_size в asset_exchange_specs. Боты на этой паре "
                    f"стоят. Проверьте app.scripts.seed_binance_data."
                )

                return None

            data = {
                "tick_size": Decimal(str(market_data["tick_size"])),
                "maker_commission_rate": market_data["maker_commission_rate"],
                "taker_commission_rate": market_data["taker_commission_rate"],
                # Те же ключи, что кладёт MarketDataBuilder.build на старте:
                # пара, догруженная на ходу, должна выглядеть так же, иначе
                # копибот v3 упал бы на отсутствующем ключе именно на ней.
                **{
                    key: (
                        Decimal(str(market_data[key]))
                        if market_data.get(key) is not None
                        else None
                    )
                    for key in LOT_DATA_KEYS
                },
            }
            shared_data[symbol] = data
            cls._market_data_misses.pop(symbol, None)

            return data

    @staticmethod
    async def update_config_from_referral_bot(bot_config: TestBot, redis):
        # Лидера уже посчитал воркер set_profitable_bot и разложил по ключам
        # copy_bot_{id} (profitable_bot_updater.py:420). Для копибота v1 здесь
        # его собственный id, для v2 — id донора-копибота v1; ключ есть в обоих
        # случаях, поэтому отдельная ветка с пересчётом через БД не нужна.
        refer_bot = await read_donor(redis, bot_config.id)

        if not refer_bot:
            logging.info(
                f"❌ Не удалось найти реферального бота для ID: {bot_config.id}"
            )
            return {
                'config': False,
                'referral_bot_id': 0
            }

        # Типы приводятся здесь, а не по месту использования: конфиг обычного
        # бота приезжает из базы с числами в полях, и копибот обязан выглядеть
        # так же — иначе сравнение или арифметика где-нибудь дальше упадёт на
        # строке. Decimal(str(...)) — потому что в JSON дробные поля едут
        # float: str() даёт короткое представление ('0.1'), а Decimal(0.1)
        # затащил бы в цену двоичный хвост. Для min_timeframe_asset_volatility
        # это ещё и вопрос ключа most_volatile_symbol_* — от хвоста он
        # перестал бы совпадать с ключом воркера.
        ref_bot_config = TestBot(
            balance=1000,
            symbol=refer_bot["symbol"],
            # Тики в конфиге донора могут быть null: у бота либо тиковые
            # уровни, либо процентные. Ноль здесь и означает «не задано».
            stop_success_ticks=int(refer_bot['stop_success_ticks'] or 0),
            stop_loss_ticks=int(refer_bot['stop_loss_ticks'] or 0),
            start_updown_ticks=int(refer_bot['start_updown_ticks'] or 0),
            stop_win_percents=Decimal(str(refer_bot['stop_win_percents'])),
            stop_loss_percents=Decimal(str(refer_bot['stop_loss_percents'])),
            start_updown_percents=Decimal(
                str(refer_bot['start_updown_percents'])
            ),
            min_timeframe_asset_volatility=Decimal(
                str(refer_bot['min_timeframe_asset_volatility'])
            ),
            time_to_wait_for_entry_price_to_open_order_in_seconds=Decimal(
                str(refer_bot[
                    'time_to_wait_for_entry_price_to_open_order_in_seconds'
                ])
            ),
            # .get(), а не [...]: ключи copy_bot_*, записанные в Redis до
            # добавления поля, его ещё не содержат.
            use_trailing_stop=bool(refer_bot.get('use_trailing_stop')),
            consider_ma_for_open_order=bool(refer_bot['consider_ma_for_open_order']),
            consider_ma_for_close_order=bool(refer_bot['consider_ma_for_close_order']),
            # Число свечей — целое. Через Decimal(str(...)), а не int()
            # напрямую: ключ copy_bot_*, записанный прошлой версией воркера,
            # живёт в Redis до его следующего цикла, а там строка ('5.00'), на
            # которой int() падает.
            ma_number_of_candles_for_open_order=int(Decimal(
                str(refer_bot['ma_number_of_candles_for_open_order'] or 0)
            )),
            ma_number_of_candles_for_close_order=int(Decimal(
                str(refer_bot['ma_number_of_candles_for_close_order'] or 0)
            )),
        )

        return {
            'config': ref_bot_config,
            'referral_bot_id': refer_bot['id'],
            'selection': refer_bot,
        }

    @staticmethod
    def json_serializer(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Type {type(obj)} not serializable")

    # --- копибот v3 с компаундингом ---------------------------------------
    #
    # Единственный бот парка, который ведёт счёт, а не считает на условную
    # тысячу: прибыль реинвестируется, в позицию идёт доля баланса, количество
    # округляется по шагу лота. Первые два — множители, и их можно было бы
    # получить пересчётом задним числом; лот — нет. Именно он показывает, где
    # реальный бот перестанет торговать, потому что на счёте не набирается
    # minQty.

    # Столько же берёт боевой бот (`balanceUSDT099` в binance_bot): остаток —
    # запас на комиссию и на движение цены между расчётом и постановкой ордера.
    COMPOUND_BALANCE_SHARE = Decimal("0.99")

    # Сколько попыток подряд должны упереться в нехватку, прежде чем считать
    # счёт слитым. Одной мало: копибот торгует парой донора, а у пар разные
    # minQty — на дорогой монете лот набирается, на дешёвой нет. Остановка по
    # первой же неудаче означала бы, что прогноз обрывается из-за одной
    # неудачной пары, и выглядело бы это неотличимо от настоящего слива.
    #
    # Между попытками бот заново подбирает донора (и вместе с ним пару), так
    # что десять подряд — это десять разных шансов, а не один повторённый.
    COMPOUND_SHORTAGE_LIMIT = 10

    def _v3_balance(self, bot_config) -> Decimal:
        """Текущий счёт бота v3, начиная со значения из `test_bots`.

        Живёт в словаре на команде, а не в конфиге: `_to_bot_objects` отдаёт
        namedtuple, присвоить в него нельзя. В базу значение уезжает после
        каждой сделки, поэтому перезапуск симулятора кривую не рвёт.
        """
        return self._v3_balances.setdefault(
            bot_config.id, Decimal(str(bot_config.balance))
        )

    def _v3_is_stopped(self, bot_id: int) -> bool:
        return bot_id in self._v3_stopped

    async def _v3_note_shortage(self, bot_id: int, refusal) -> None:
        """Учесть неудачу и остановить бота, если их накопилось достаточно.

        Нехватка средств — единственный отказ, который что-то говорит о самом
        боте. Остальные привязаны к паре, и их сюда приносить нельзя.
        """
        if not refusal.is_shortage:
            logging.info(
                f'копибот v3 {bot_id}: пропускаем сделку на паре — {refusal}'
            )
            return

        count = self._v3_shortages.get(bot_id, 0) + 1
        self._v3_shortages[bot_id] = count

        if count < self.COMPOUND_SHORTAGE_LIMIT:
            logging.info(
                f'копибот v3 {bot_id}: не хватает на лот ({refusal}), '
                f'попытка {count} из {self.COMPOUND_SHORTAGE_LIMIT}'
            )
            return

        await self._v3_stop(bot_id=bot_id, reason=str(refusal))

    async def _v3_stop(self, bot_id: int, reason: str) -> None:
        """Остановить бота: на балансе больше не набирается лот.

        `is_active` не трогаем — `active_bots_subquery` отбирает только
        активных, и снятие флага убрало бы бота из всех отчётов ровно там, где
        факт остановки важнее всего. Вместо этого проставляется
        `copybot_v3_stopped_at`, и он же переживает перезапуск процесса.
        """
        if bot_id in self._v3_stopped:
            return

        self._v3_stopped.add(bot_id)

        logging.info(
            f"🛑 Копибот v3 {bot_id} остановлен: {reason}. "
            f"Дальше он не торгует; в отчёте останется с датой остановки."
        )

        dsm = DatabaseSessionManager.create(settings.DB_URL)

        async with dsm.get_session() as session:
            await TestBotCrud(session).mark_v3_stopped(bot_id=bot_id)

    @classmethod
    def compound_order_size(cls, balance, price, market_data):
        """(количество, номинал, отказ) для компаундирующего бота.

        Количество округляется вниз по шагу лота — именно так считает биржа, и
        именно поэтому номинал позиции не равен доле баланса. PnL потом
        считается от номинала, а не от счёта: `calculate_pnl` внутри делает
        `amount = balance / open_price`, так что номинал даёт ровно то
        количество, которое получилось после округления.

        Отказ — объект `Refusal`, а не просто текст: вызывающий код обязан
        различать нехватку средств от всего остального. Копибот торгует парой
        донора, а донор меняется, поэтому «на этой паре лот не набрался» и
        «счёт кончился» — разные утверждения.
        """
        try:
            with localcontext() as ctx:
                ctx.prec = 60

                def number(value):
                    value = Decimal(str(value))
                    if not value.is_finite():
                        raise ValueError("неконечное число")
                    return value

                price, balance = number(price), number(balance)
                if price <= 0:
                    raise ValueError("нет положительной цены")
                lots = [tuple(number(market_data[key]) for key in keys) for keys in (
                    ("min_qty", "max_qty", "step_size"),
                    ("market_min_qty", "market_max_qty", "market_step_size"),
                )]
                minimum = number(market_data["min_notional"])
                if minimum <= 0 or lots[0][2] <= 0 or any(
                    lo < 0 or hi <= 0 or lo > hi or step < 0
                    for lo, hi, step in lots
                ):
                    raise ValueError("некорректные границы лота/номинала")
                lower, upper = max(l[0] for l in lots), min(l[1] for l in lots)
                if lower > upper:
                    raise ValueError("границы лотов не пересекаются")
                origin, step = quantity_grid(lots)
                amount = balance * cls.COMPOUND_BALANCE_SHARE
                quantity = origin + ((amount / price - origin) / step).to_integral_value(
                    rounding=ROUND_FLOOR
                ) * step
                notional = quantity * price
                # Страховка от округления деления на самой границе бюджета.
                if notional > amount:
                    quantity -= step
                    notional = quantity * price
                if quantity <= 0 or quantity < lower:
                    return None, None, Refusal("shortage", "не хватает на минимальный лот")
                if quantity > upper:
                    return None, None, Refusal("pair", f"количество {quantity} больше максимального {upper}")
                if notional < minimum:
                    return None, None, Refusal(
                        "shortage", f"номинал {notional} меньше минимального {minimum}",
                    )
                return quantity, notional, None
        except (KeyError, TypeError, ValueError, DecimalException) as error:
            return None, None, Refusal("data", f"нет или некорректны ограничения/цена: {error}")

    @staticmethod
    def is_price_within_bounds(price, market_data) -> bool:
        """Цена входа в границах пары.

        В отличие от лота это состояние рынка, а не счёта: бот не
        останавливается, а пропускает попытку — так же, как боевой отменяет
        ордер и заходит на новый круг.
        """
        min_price = market_data.get("min_price")
        max_price = market_data.get("max_price")

        if min_price is not None and price < Decimal(str(min_price)):
            return False

        if max_price is not None and price > Decimal(str(max_price)):
            return False

        return True

    @staticmethod
    async def select_copybot(original):
        """Текущий v1 и вся цепочка выбора; сессия закрыта до ожидания цены."""
        if not (original.copybot_v3_time_in_minutes or original.copybot_v2_time_in_minutes):
            return original, ()
        dsm = DatabaseSessionManager.create(settings.DB_URL)
        async with dsm.get_session() as session:
            crud = TestBotCrud(session)
            parent = original
            chain = []
            if original.copybot_v3_time_in_minutes:
                parent = await ProfitableBotUpdaterCommand.get_copybot_config(
                    bot_crud=crud,
                    copybot_v2_time_in_minutes=original.copybot_v3_time_in_minutes,
                    donor_version=2,
                )
                if not parent:
                    return None, ()
                chain.append(parent.id)
            selected = await ProfitableBotUpdaterCommand.get_copybot_config(
                bot_crud=crud,
                copybot_v2_time_in_minutes=parent.copybot_v2_time_in_minutes,
                donor_version=1,
            )
            if not selected:
                return None, ()
            return selected, (*chain, selected.id)

    async def simulate_bot(self, redis, original_bot_config, shared_data,
                           stop_event, price_provider, binance_bot):
        try:
            await self._simulate_bot(redis, original_bot_config, shared_data,
                                     stop_event, price_provider, binance_bot)
        except DonorChanged:
            # Позиция ещё не открыта. Следующий вызов из _run_loop заново
            # выберет донора; старые уровни и ожидающие задачи не сохраняются.
            logging.info("Выбор донора отозван для %s", original_bot_config.id)

    async def _simulate_bot(
        self,
        redis,
        original_bot_config: TestBot,
        shared_data,
        stop_event,
        price_provider,
        binance_bot,
    ):
        while not stop_event.is_set():
            referral_bot_id = None
            # Стратегия парка и та, по которой сделка исполнена. У
            # обычного бота это одно и то же; у копибота вторую задаёт
            # конечный донор, и она может оказаться чужой.
            executed_strategy_id = original_bot_config.strategy_id
            donor_chain = None
            bot_id = original_bot_config.id
            bot_config = None

            is_compound_v3 = bool(
                original_bot_config.copybot_v3_time_in_minutes
                and original_bot_config.copybot_v3_compound_balance
            )

            if is_compound_v3 and self._v3_is_stopped(bot_id):
                # Баланса уже не хватает на минимальный лот. Проверка стоит
                # до отбора донора: считать рейтинги ради бота, который всё
                # равно не поставит ордер, незачем.
                await asyncio.sleep(60)
                return

            bot_config, chain = await self.select_copybot(original_bot_config)
            if not bot_config:
                await asyncio.sleep(60)
                return

            is_it_copy = bot_config.copy_bot_min_time_profitability_min
            donor_guard = None
            copybot_id = bot_config.id

            if is_it_copy:
                # Сессия к БД здесь больше не нужна: конфиг донора берётся
                # из Redis, а не пересчитывается запросами.
                updating_config_res = (
                    await self.update_config_from_referral_bot(
                        bot_config=bot_config,
                        redis=redis,
                    )
                )
                bot_config = updating_config_res['config']
                referral_bot_id = updating_config_res['referral_bot_id']

                if not bot_config:
                    await asyncio.sleep(60)
                    return

                # Только после проверки: донора могло не найтись, и тогда
                # в ответе нет ни 'selection', ни осмысленного донора —
                # сделки всё равно не будет.
                donor_chain = [*chain, referral_bot_id]
                # .get(): ключ copy_bot_*, записанный воркером до этой
                # правки, живёт в Redis до его следующего цикла и поля
                # ещё не содержит. Тогда исполненной считается своя
                # стратегия — прежнее поведение.
                executed_strategy_id = (
                    updating_config_res['selection'].get('strategy_id')
                    or executed_strategy_id
                )

                async def chain_is_current():
                    selected, current_chain = await self.select_copybot(original_bot_config)
                    return selected is not None and current_chain == chain

                donor_guard = DonorGuard(
                    redis, copybot_id, updating_config_res['selection'],
                    chain_check=chain_is_current if chain else None,
                )

                logging.info(f'found ref for {bot_id}')

            if bot_config.min_timeframe_asset_volatility:
                # Старый режим: пара за ботом не закреплена, каждый цикл берём
                # самую волатильную за своё окно. Ключ пишет воркер
                # set_volatile_pairs с TTL 60 с, поэтому мёртвый воркер
                # означает остановку ботов, а не торговлю по старой паре.
                symbol = await redis.get(
                    most_volatile_symbol_key(
                        bot_config.min_timeframe_asset_volatility
                    )
                )
            else:
                symbol = bot_config.symbol

            if not symbol:
                logging.info('there no symbol')
                await asyncio.sleep(60)
                return

            data = await self.get_market_data(symbol, shared_data)

            if not data:
                await asyncio.sleep(60)
                return

            tick_size = data["tick_size"]

            # Taker с обеих сторон: и вход по пробою уровня, и выход по
            # стоп-лоссу/тейк-профиту в реальности исполняются по рынку.
            # None = ставка не засеяна (seed_commission_rates.py) → константа.
            commission_rate = data.get("taker_commission_rate")
            if commission_rate is None:
                commission_rate = COMMISSION_OPEN

            if bot_id == 1:
                logging.info('bot_id 1 started work')

            async def before_entry(awaitable):
                if donor_guard:
                    return await donor_guard.wait(awaitable)
                return await awaitable

            bot_config = (
                await before_entry(ProfitableBotUpdaterCommand.update_config_for_percentage(
                    bot_config=bot_config,
                    price_provider=price_provider,
                    symbol=symbol,
                    tick_size=tick_size,
                ))
            )

            # Один на сделку, а не на каждую попытку входа: он лёгкий, но
            # главное — переиспользует price_provider с общим кэшем цен.
            price_watcher = PriceWatcher(
                redis=redis, price_provider=price_provider
            )

            while True:
                initial_price = await before_entry(price_provider.get_price(symbol=symbol))

                entry_price_buy = (
                    initial_price + bot_config.start_updown_ticks * tick_size
                )
                entry_price_sell = (
                    initial_price - bot_config.start_updown_ticks * tick_size
                )

                if is_compound_v3:
                    # Проверяем до ожидания входа, как это делает боевой бот в
                    # `_get_order_params`: ждать пробоя уровня ради ордера,
                    # который биржа не примет, незачем.
                    _, _, refusal = self.compound_order_size(
                        balance=self._v3_balance(original_bot_config),
                        price=initial_price,
                        market_data=data,
                    )

                    if not refusal and not all(
                        self.is_price_within_bounds(price, data)
                        for price in (entry_price_buy, entry_price_sell)
                    ):
                        refusal = Refusal(
                            "pair", f'цена входа вне границ пары {symbol}'
                        )

                    if refusal:
                        await self._v3_note_shortage(
                            bot_id=bot_id, refusal=refusal
                        )

                        if self._v3_is_stopped(bot_id):
                            return

                        # Выходим из ожидания входа, а не крутимся здесь:
                        # и пара, и её границы приходят от донора, поэтому
                        # шанс на следующей попытке даёт только переподбор
                        # донора во внешнем цикле.
                        await asyncio.sleep(60)
                        return

                is_timeout_occurred = False

                trade_type = None
                entry_price = None

                if is_it_copy or bot_id == 1:
                    logging.info(f'waiting for {bot_id}')

                try:
                    if bot_config.consider_ma_for_open_order:
                        # MA-бот входит только по пересечению средних, поэтому
                        # time_to_wait здесь не применяется: 12 часов — это
                        # предохранитель, по нему цикл просто уходит на новую
                        # итерацию ожидания.
                        wait_seconds = 12 * 60 * 60
                    elif bot_config.time_to_wait_for_entry_price_to_open_order_in_seconds:
                        wait_seconds = bot_config.time_to_wait_for_entry_price_to_open_order_in_seconds
                    else:
                        wait_seconds = 1

                    timeout = int(wait_seconds)

                    trade_type, entry_price = await before_entry(asyncio.wait_for(
                        price_watcher.wait_for_entry_price(
                            symbol=symbol,
                            entry_price_buy=entry_price_buy,
                            entry_price_sell=entry_price_sell,
                            binance_bot=binance_bot,
                            bot_config=bot_config,
                        ),
                        timeout=timeout,
                    ))
                except asyncio.TimeoutError:
                    is_timeout_occurred = True

                if is_it_copy or bot_id == 1:
                    logging.info(f'is_timeout_occurred: {is_timeout_occurred} for {bot_id}')

                if is_timeout_occurred or not trade_type or not entry_price:
                    continue
                else:
                    break

            if donor_guard:
                await donor_guard.check(final=True)
                # Проверка донора ждала Redis/SQL: за это время цена сигнала
                # могла измениться или истечь. Старый сигнал не переносим
                # на новую цену; следующий проход заново ждёт входа.
                current_price = await price_provider._read_price(symbol)
                if current_price is None or current_price != entry_price:
                    logging.info("Цена сигнала изменилась до входа бота %s", bot_id)
                    return

            # Фиксируем объём до открытия, по фактической цене сигнала.
            # В удержании и на закрытии его уже нельзя пересчитывать.
            if is_compound_v3:
                account_balance = self._v3_balance(original_bot_config)
                _, notional, refusal = self.compound_order_size(
                    account_balance, entry_price, data,
                )
                if not refusal and not self.is_price_within_bounds(entry_price, data):
                    refusal = Refusal("pair", f"цена входа вне границ пары {symbol}")
                if refusal:
                    await self._v3_note_shortage(bot_id=bot_id, refusal=refusal)
                    if not self._v3_is_stopped(bot_id):
                        await asyncio.sleep(60)
                    return

            open_price = entry_price
            price_from_previous_step = entry_price
            peak_favorable_price = entry_price

            close_not_lose_price = (
                PriceCalculator.calculate_close_not_lose_price(
                    open_price=open_price,
                    trade_type=trade_type,
                    commission_open=commission_rate,
                    commission_close=commission_rate,
                )
            )

            if not bot_config.consider_ma_for_close_order:
                stop_loss_price = PriceCalculator.calculate_stop_lose_price(
                    stop_loss_ticks=bot_config.stop_loss_ticks,
                    tick_size=tick_size,
                    open_price=open_price,
                    trade_type=trade_type,
                )
                if bot_config.use_trailing_stop:
                    original_take_profit_price = (
                        PriceCalculator.calculate_trailing_take_profit_price(
                            peak_favorable_price=open_price,
                            stop_success_ticks=bot_config.stop_success_ticks,
                            tick_size=tick_size,
                            trade_type=trade_type,
                        )
                    )
                else:
                    original_take_profit_price = (
                        PriceCalculator.calculate_take_profit_price(
                            stop_success_ticks=bot_config.stop_success_ticks,
                            tick_size=tick_size,
                            open_price=open_price,
                            trade_type=trade_type,
                            commission_open=commission_rate,
                            commission_close=commission_rate,
                        )
                    )
                take_profit_price = original_take_profit_price

                order = TestOrder(
                    stop_loss_price=Decimal(stop_loss_price),
                    start_updown_ticks=bot_config.start_updown_ticks,
                    stop_success_ticks=bot_config.stop_success_ticks,
                    stop_loss_ticks=bot_config.stop_loss_ticks,
                    open_price=open_price,
                    open_time=datetime.now(UTC),
                    open_fee=(
                        Decimal(bot_config.balance) * Decimal(commission_rate)
                    ),
                    order_type=trade_type
                )
            else:
                order = TestOrder(
                    stop_loss_price=0,
                    start_updown_ticks=0,
                    stop_success_ticks=0,
                    stop_loss_ticks=0,
                    open_price=open_price,
                    open_time=datetime.now(UTC),
                    open_fee=(
                        Decimal(bot_config.balance) * Decimal(commission_rate)
                    ),
                    order_type=trade_type
                )

            if is_it_copy or bot_id == 1:
                logging.info(f'wait for should_exit for {bot_id}')

            just30sec_start_time = time.time()

            while not stop_event.is_set():
                updated_price = await price_provider.get_price(symbol=symbol)

                if bot_config.consider_ma_for_close_order:
                    should_exit = (
                        await ExitStrategy.check_exit_ma_conditions(
                            binance_bot=binance_bot,
                            bot_config=bot_config,
                            symbol=symbol,
                            order_side=order.order_type,
                            updated_price=updated_price,
                            close_not_lose_price=close_not_lose_price,
                        )
                    )
                else:
                    if bot_config.use_trailing_stop:
                        should_exit, take_profit_price, peak_favorable_price = (
                            await ExitStrategy.check_exit_conditions_trailing(
                                price_calculator=PriceCalculator,
                                tick_size=tick_size,
                                order=order,
                                close_not_lose_price=close_not_lose_price,
                                take_profit_price=take_profit_price,
                                updated_price=updated_price,
                                price_from_previous_step=price_from_previous_step,
                                peak_favorable_price=peak_favorable_price
                            )
                        )
                    else:
                        should_exit = (
                            await ExitStrategy.check_exit_conditions(
                                order=order,
                                close_not_lose_price=close_not_lose_price,
                                take_profit_price=take_profit_price,
                                updated_price=updated_price,
                            )
                        )

                just30sec_current_time = time.time()
                just30sec_elapsed_time = just30sec_current_time - just30sec_start_time

                if order.order_type == TradeType.BUY:
                    price_diff_from_cnl = updated_price - close_not_lose_price
                else:
                    price_diff_from_cnl = close_not_lose_price - updated_price

                diff_ticks = price_diff_from_cnl / tick_size

                if diff_ticks < 10 and just30sec_elapsed_time >= 30:
                    order.stop_reason_event = StopReasonEvent.STOP_LONG_LOSE.value
                    break

                if should_exit:
                    break

                price_from_previous_step = updated_price

                await asyncio.sleep(0.1)

            if is_it_copy or bot_id == 1:
                logging.info(f'end wait for {bot_id}')

            close_price = await price_provider.get_price(symbol=symbol)

            balance = bot_config.balance

            if is_compound_v3:
                # В расчёт уходит номинал позиции, а не счёт: количество уже
                # округлено по шагу лота, и `calculate_pnl` с
                # `amount = balance / open_price` даёт ровно его. Иначе PnL
                # считался бы по дробному количеству, которого на бирже не
                # бывает.
                balance = notional

            pnl = PriceCalculator.calculate_pnl(
                balance=balance,
                close_price=close_price,
                open_price=open_price,
                trade_type=trade_type,
                commission_open=commission_rate,
                commission_close=commission_rate,
            )

            # Тем же расчётом, что и внутри calculate_pnl, иначе поля
            # open_fee/close_fee не сходятся с profit_loss.
            open_fee, close_fee = PriceCalculator.calculate_fees(
                balance=balance,
                open_price=open_price,
                close_price=close_price,
                commission_open=commission_rate,
                commission_close=commission_rate,
            )

            order_data = {
                "asset_symbol": symbol,
                "order_type": trade_type,
                "balance": str(balance),
                "open_price": str(open_price),
                "open_time": order.open_time,
                "open_fee": str(open_fee),
                "stop_loss_price": str(order.stop_loss_price),
                "bot_id": bot_id,
                "close_price": str(close_price),
                "close_time": datetime.now(UTC),
                "close_fee": str(close_fee),
                "profit_loss": str(pnl),
                "is_active": False,
                "start_updown_ticks": int(order.start_updown_ticks),
                "stop_loss_ticks": int(order.stop_loss_ticks),
                "stop_success_ticks": int(order.stop_success_ticks),
                "stop_reason_event": order.stop_reason_event,
                "referral_bot_id": referral_bot_id,
                "strategy_id": original_bot_config.strategy_id,
                "executed_strategy_id": executed_strategy_id,
                "algorithm_version": algorithm_version_for(
                    self._strategy_keys.get(executed_strategy_id)
                ),
                "donor_chain": donor_chain,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
            await redis.rpush(
                ORDER_QUEUE_KEY,
                json.dumps(order_data, default=self.json_serializer),
            )

            if is_compound_v3:
                # Счёт растёт и падает на результат сделки — в этом и смысл
                # компаундирующего варианта. Пишем сразу в `test_bots`:
                # прочитать своё состояние из `test_orders` бот не может,
                # сделки уезжают в очередь и вставляются пачками.
                account_balance = account_balance + pnl
                self._v3_balances[bot_id] = account_balance
                # Сделка прошла — значит серия неудач прервана.
                self._v3_shortages.pop(bot_id, None)

                dsm = DatabaseSessionManager.create(settings.DB_URL)

                async with dsm.get_session() as session:
                    await TestBotCrud(session).set_balance(
                        bot_id=bot_id, balance=account_balance
                    )

                logging.info(
                    f'копибот v3 {bot_id}: счёт {account_balance:.4f} '
                    f'после сделки на {pnl:.4f}'
                )
