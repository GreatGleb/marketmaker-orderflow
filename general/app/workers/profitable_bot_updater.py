import asyncio
import json
import logging
import time

from datetime import timedelta

from fastapi import Depends

from redis.asyncio import Redis

from app.config import settings
from app.constants.copybot import (
    DONOR_HISTORY_MINUTES,
    PROFITABILITY_CHECK_MINUTES,
)
from app.crud.test_bot import TestBotCrud
from app.db.base import DatabaseSessionManager
from app.db.models import TestBot
from app.dependencies import get_redis

from app.utils import Command


# Конфиг донора уезжает копиботам через JSON (ключ copy_bot_{id} в Redis), а в
# JSON нет Decimal. Поэтому дробные поля кладутся float, целые — int, и никогда
# строкой: '0' — истина для `not`, и у потребителей молча ломались бы проверки
# «поле не задано» (binance_bot.py:1448 на строке '0' ставил таймаут ожидания
# входа в int('0') = 0 секунд вместо секунды по умолчанию).
#
# Точность возвращают на стороне потребителя через Decimal(str(...)): так
# двоичная погрешность float не попадает ни в арифметику цен, ни в ключ
# most_volatile_symbol_* (app/constants/volatility.py).
def _as_float(value, default=0.0):
    return default if value is None else float(value)


def _as_int(value, default=0):
    return default if value is None else int(value)


class WindowCache:
    """Кэш рейтингов прибыльности с разным сроком годности по окнам.

    Рейтинг за 48 часов за полминуты практически не меняется, а стоит он
    десятки миллионов строк в test_orders. Пересчитывать его так же часто, как
    десятиминутный, — чистая трата: воркер тратил на это больше ядра
    непрерывно и всё равно не успевал за своим 30-секундным циклом.

    Поэтому окно живёт REFRESH_FRACTION от своей длины, но не меньше
    MIN_REFRESH_SECONDS:

        10 минут  → пересчёт раз в 30 с   (как и было)
        12 часов  → раз в 36 минут
        48 часов  → раз в 2.4 часа

    Данные отстают максимум на те же 5% длины окна — для двухсуточного
    рейтинга это ничто.

    Срок жизни объекта задаёт, кэшируется ли что-то между циклами: воркер
    держит один экземпляр постоянно, разовые вызовы создают свой, и тогда кэш
    работает просто как дедупликация внутри одного обхода.
    """

    REFRESH_FRACTION = 0.05
    MIN_REFRESH_SECONDS = 30

    def __init__(self):
        self._entries: dict[tuple, tuple[float, list]] = {}

    def lifetime_seconds(self, minutes) -> float:
        return max(
            self.MIN_REFRESH_SECONDS,
            float(minutes) * 60 * self.REFRESH_FRACTION,
        )

    def get(self, key, minutes):
        entry = self._entries.get(key)

        if entry is None:
            return None

        stored_at, value = entry

        if time.monotonic() - stored_at >= self.lifetime_seconds(minutes):
            return None

        return value

    def set(self, key, value) -> None:
        self._entries[key] = (time.monotonic(), value)


class ProfitableBotUpdaterCommand(Command):

    def __init__(self, stop_event):
        super().__init__()
        self.stop_event = stop_event

        logging.basicConfig(
            format='%(asctime)s - %(levelname)s - %(message)s',
            level=logging.INFO
        )

    @staticmethod
    async def get_copybot_config(
            bot_crud, copybot_v2_time_in_minutes = 60
    ):
        copy_bot = None
        copy_bot_id = None

        copybot_v2_time_in_minutes = int(copybot_v2_time_in_minutes)

        try:
            profits_data = await bot_crud.get_sorted_by_profit(since=timedelta(minutes=copybot_v2_time_in_minutes), just_copy_bots=True)
            profits_data_filtered_sorted = sorted([item for item in profits_data if item[1] > 0], key=lambda x: x[1], reverse=True)

            try:
                copy_bot_id = profits_data_filtered_sorted[0][0]
            except (IndexError, TypeError):
                pass

            if copy_bot_id:
                copy_bots = await bot_crud.get_bot_by_id(
                    bot_id=copy_bot_id
                )
                if copy_bots:
                    copy_bot = copy_bots[0]
        except Exception as e:
            logging.info('Failed to get copybot data')
            logging.info(e)

        return copy_bot

    @staticmethod
    async def get_bot_config_by_params(
        bot_crud, bot_ids
    ):
        if bot_ids:
            refer_bot = None
            try:
                refer_bot = await bot_crud.get_bot_by_id(
                    bot_id=bot_ids[0]
                )
                logging.info('bot_ids fine')
            except:
                logging.info(bot_ids)
                logging.info('bot_ids error')

            if refer_bot:
                refer_bot = refer_bot[0]

                # Только чтение: refer_bot — строка test_bots, загруженная
                # в живую сессию. Раньше NULL-поля заполнялись нулями
                # присваиванием в сам объект, и autoflush перед следующим
                # запросом отправлял настоящий UPDATE test_bots — то есть
                # RowExclusiveLock на таблицу до конца транзакции, а она у
                # воркера одна на весь процесс. От порчи данных спасал только
                # rollback при закрытии сессии; один commit() в этой сессии — и
                # NULL стали бы нулями. Подстановки живут в словаре, сам
                # объект не трогаем.
                #
                # Ноль вместо NULL — это «не задано»: и Decimal(0), и 0.0
                # ложны для `not`, на чём и стоят проверки у потребителей
                # (update_config_for_percentage, откат таймаута к 1 секунде).
                #
                # Тики — исключение: они nullable и в базе, подстановку нуля
                # делают потребители, поэтому null остаётся null.
                refer_bot_dict = {
                    "id": refer_bot.id,
                    "symbol": refer_bot.symbol,
                    "stop_success_ticks": _as_int(
                        refer_bot.stop_success_ticks, default=None
                    ),
                    "stop_loss_ticks": _as_int(
                        refer_bot.stop_loss_ticks, default=None
                    ),
                    "start_updown_ticks": _as_int(
                        refer_bot.start_updown_ticks, default=None
                    ),
                    "stop_win_percents": _as_float(refer_bot.stop_win_percents),
                    "stop_loss_percents": _as_float(refer_bot.stop_loss_percents),
                    "start_updown_percents": _as_float(
                        refer_bot.start_updown_percents
                    ),
                    "min_timeframe_asset_volatility": _as_float(
                        refer_bot.min_timeframe_asset_volatility
                    ),
                    "time_to_wait_for_entry_price_to_open_order_in_seconds": _as_float(
                        refer_bot.time_to_wait_for_entry_price_to_open_order_in_seconds
                    ),
                    "use_trailing_stop": bool(refer_bot.use_trailing_stop),
                    "consider_ma_for_open_order": bool(
                        refer_bot.consider_ma_for_open_order
                    ),
                    "consider_ma_for_close_order": bool(
                        refer_bot.consider_ma_for_close_order
                    ),
                    # Число свечей — счётчик, в базе он numeric только по
                    # историческим причинам.
                    "ma_number_of_candles_for_open_order": _as_int(
                        refer_bot.ma_number_of_candles_for_open_order
                    ),
                    "ma_number_of_candles_for_close_order": _as_int(
                        refer_bot.ma_number_of_candles_for_close_order
                    ),
                }
            else:
                refer_bot_dict = None
        else:
            refer_bot_dict = None

        return refer_bot_dict

    @staticmethod
    async def update_config_for_percentage(
        bot_config, price_provider, symbol, tick_size
    ):
        if not bot_config.stop_win_percents or not bot_config.stop_loss_percents or not bot_config.start_updown_percents:
            return bot_config

        price = await price_provider.get_price(symbol=symbol)

        stop_success_ticks = round((price * (bot_config.stop_win_percents/100))/tick_size)
        stop_loss_ticks = round((price * (bot_config.stop_loss_percents/100))/tick_size)
        start_updown_ticks = round((price * (bot_config.start_updown_percents/100))/tick_size)

        if stop_success_ticks < 1:
            stop_success_ticks = 1
        if stop_loss_ticks < 1:
            stop_loss_ticks = 1
        if start_updown_ticks < 1:
            start_updown_ticks = 1

        new_ticks = {
            "stop_success_ticks": stop_success_ticks,
            "stop_loss_ticks": stop_loss_ticks,
            "start_updown_ticks": start_updown_ticks,
        }

        # Сюда приходят конфиги двух видов: настоящий TestBot (копиботы,
        # binance_bot) и namedtuple BotObject у обычных тестовых ботов
        # (demo_test_bot.py:79). У namedtuple нет ни clone(), ни присваивания
        # полей, поэтому для него копия делается через _replace.
        if hasattr(bot_config, "clone"):
            ref_bot_config = bot_config.clone()

            for field, value in new_ticks.items():
                setattr(ref_bot_config, field, value)
        else:
            ref_bot_config = bot_config._replace(**new_ticks)

        return ref_bot_config

    @staticmethod
    async def profitable_bot_ids_for_window(
        bot_crud, minutes, by_referral_bot_id=False, window_cache=None
    ):
        """ID прибыльных ботов за окно, от самого прибыльного к менее.

        window_cache — словарь на один цикл воркера. Окон всего два десятка, а
        копиботов 80, и без кэша один и тот же агрегат по test_orders считался
        бы по сорок раз подряд. Кэш обязан быть короткоживущим: данные меняются
        каждую секунду.
        """
        key = (float(minutes), bool(by_referral_bot_id))

        if window_cache is not None:
            cached = window_cache.get(key, minutes)

            if cached is not None:
                return cached

        rows = await bot_crud.get_sorted_by_profit(
            since=timedelta(minutes=float(minutes)),
            just_not_copy_bots=True,
            by_referral_bot_id=by_referral_bot_id,
        )
        profitable = sorted(
            (row for row in rows if row[1] is not None and row[1] > 0),
            key=lambda row: row[1],
            reverse=True,
        )
        bot_ids = [row[0] for row in profitable]

        logging.info(
            f'окно {minutes} мин'
            f'{", по реферальным" if by_referral_bot_id else ""}: '
            f'прибыльных ботов {len(bot_ids)}'
        )

        if window_cache is not None:
            window_cache.set(key, bot_ids)

        return bot_ids

    @staticmethod
    async def losing_donor_ids(bot_crud, window_cache=None):
        """Боты, за которыми копировали в убыток, за фиксированное окно.

        Это не «кто плохой», а «кто уже проверен и провалился». Разница
        принципиальная. Раньше фильтр требовал **положительной** донорской
        прибыли, и тогда «истории нет» приравнивалось к «плохой»: донорская
        история есть только у тех, кого уже выбирали донором, то есть у
        нескольких десятков ботов из пятнадцати тысяч. Фильтр резал пул в
        двести раз и запирал отбор на инкумбентах, вместо того чтобы
        проверять качество.

        Теперь наоборот: возвращается множество тех, у кого копиры **ушли в
        минус**, и вызывающий их выбрасывает. Бот без истории проходит —
        предъявить ему нечего.

        Зачем фильтр вообще нужен, если конфиг переносится корректно: он
        сигнализация на регресс. Поле стратегии добавляется в три места
        (словарь здесь, сборка в `demo_test_bot`, такая же в `binance_bot`),
        и потеря одного из них ничем больше не ловится — так уже терялись
        `use_trailing_stop` и `ma_number_of_candles_*`. Если параметр
        потерялся, копиры торгуют не тем конфигом и уходят в минус, а донор
        по своим сделкам выглядит прекрасно. Видно это только отсюда.
        """
        minutes = DONOR_HISTORY_MINUTES
        key = ("losing_donors", float(minutes))

        if window_cache is not None:
            cached = window_cache.get(key, minutes)

            if cached is not None:
                return cached

        rows = await bot_crud.get_sorted_by_profit(
            since=timedelta(minutes=float(minutes)),
            just_not_copy_bots=True,
            by_referral_bot_id=True,
        )
        losing = {
            row[0] for row in rows
            if row[1] is not None and row[1] < 0
        }

        logging.info(
            f'донорское окно {minutes} мин: '
            f'ботов с убыточными копирами {len(losing)}'
        )

        if window_cache is not None:
            window_cache.set(key, losing)

        return losing

    @staticmethod
    async def get_profitable_bots_id_by_timeframes(
        bot_crud, bot_profitability_timeframes,
        check_24h_profitability=False,
        exclude_losing_donors=False,
    ):
        window_cache = WindowCache()
        tf_bot_ids = {}

        for tf in bot_profitability_timeframes:
            tf_bot_ids[tf] = await ProfitableBotUpdaterCommand.filter_profitable_bots_id(
                bot_crud=bot_crud,
                timeframe=tf,
                check_24h_profitability=check_24h_profitability,
                exclude_losing_donors=exclude_losing_donors,
                window_cache=window_cache,
            )

        return tf_bot_ids

    @staticmethod
    async def get_profitable_bots_id_by_individual_params(
        bot_crud,
        bot_profitability_parameters,
        window_cache=None,
    ):
        # Свой кэш, если не дали общий: у 80 копиботов всего 20 разных окон,
        # и даже внутри одного обхода это экономит четыре пятых запросов.
        if window_cache is None:
            window_cache = WindowCache()

        tf_bot_ids = {}

        for bot_id, parameters in bot_profitability_parameters.items():
            tf_bot_ids[bot_id] = await ProfitableBotUpdaterCommand.filter_profitable_bots_id(
                bot_crud=bot_crud,
                timeframe=parameters["tf"],
                check_24h_profitability=parameters["24h"],
                exclude_losing_donors=parameters["no_losing_donors"],
                window_cache=window_cache,
            )

        return tf_bot_ids

    @staticmethod
    async def filter_profitable_bots_id(
        bot_crud,
        timeframe,
        check_24h_profitability=False,
        exclude_losing_donors=False,
        window_cache=None,
    ):
        """Кандидаты в доноры для одного копибота, лучший первым.

        Порядок задаёт первый шаг — собственная прибыль бота за его окно.
        Оба флага дальше только выбрасывают строки и порядок не трогают:
        побеждает самый прибыльный сам по себе среди уцелевших.

        Ранжировать по донорской прибыли нельзя, хотя соблазн есть: она
        существует только у тех, кого уже выбирали донором, а выбирают тех,
        кто первый по собственной. Получилась бы защёлка — кто выиграл
        первым, тот и донор навсегда. Собственная прибыль от этого свободна:
        она есть у всех пятнадцати тысяч кандидатов, независимо от того,
        трогали их раньше или нет.
        """
        get_ids = ProfitableBotUpdaterCommand.profitable_bot_ids_for_window

        tf_ids = await get_ids(
            bot_crud=bot_crud, minutes=timeframe, window_cache=window_cache
        )

        if check_24h_profitability:
            ids_24h = set(await get_ids(
                bot_crud=bot_crud, minutes=PROFITABILITY_CHECK_MINUTES,
                window_cache=window_cache,
            ))
            # set, а не list: на тысячах ботов проверка вхождения в список
            # превращала фильтр в квадрат.
            tf_ids = [bot_id for bot_id in tf_ids if bot_id in ids_24h]

        if exclude_losing_donors:
            losing = await ProfitableBotUpdaterCommand.losing_donor_ids(
                bot_crud=bot_crud, window_cache=window_cache
            )
            tf_ids = [bot_id for bot_id in tf_ids if bot_id not in losing]

        return tf_ids

    async def command(
        self,
        redis: Redis = Depends(get_redis),
    ):
        first_run_completed = False
        bot_profitability_params = {}
        # Живёт между циклами: длинные окна пересчитываются раз в проценты от
        # своей длины, а не каждые 30 секунд.
        window_cache = WindowCache()

        dsm = DatabaseSessionManager.create(settings.DB_URL)

        while not self.stop_event.is_set():
            # Сессия своя на каждый цикл — так же, как в set_volatile_pairs
            # (volatile_pair.py:57). Через зависимости Command её брать
            # нельзя: они решаются один раз в run_async, а цикл живёт внутри
            # command(), то есть сессия оказалась бы одна на весь процесс.
            # Коммита у воркера нет — он только читает, — поэтому такая
            # сессия держала бы одну транзакцию до остановки процесса, а её
            # снапшот не даёт autovacuum вычистить мёртвые строки, которые
            # появились после начала транзакции. По test_orders это десятки
            # миллионов строк в сутки: ретеншн их удаляет, а место не
            # возвращается (10-retention.md).
            async with dsm.get_session() as session:
                bot_crud = TestBotCrud(session)

                bots = await bot_crud.get_copybots()

                # Пересобираем каждый цикл, а не один раз на старте:
                # копибота могли завести уже после запуска воркера, и тогда
                # ниже tf_bot_ids[bot.id] падало с KeyError, роняя воркер.
                bot_profitability_params = {
                    bot.id: {
                        'tf': bot.copy_bot_min_time_profitability_min,
                        '24h': bot.copybot_v1_check_for_24h_profitability,
                        'no_losing_donors': bot.copybot_v1_exclude_losing_donors,
                    }
                    for bot in bots
                }

                if not first_run_completed:
                    first_run_completed = True

                    logging.info(bot_profitability_params)
                    logging.info('bot_profitability_params')

                tf_bot_ids = (
                    await self.get_profitable_bots_id_by_individual_params(
                        bot_crud=bot_crud,
                        bot_profitability_parameters=bot_profitability_params,
                        window_cache=window_cache,
                    )
                )

                for bot in bots:
                    refer_bot_dict = await self.get_bot_config_by_params(
                        bot_crud=bot_crud,
                        bot_ids=tf_bot_ids[bot.id]
                    )
                    logging.info(refer_bot_dict)
                    logging.info(f"copy_bot_{bot.id}")
                    if refer_bot_dict:
                        await redis.set(
                            f"copy_bot_{bot.id}", json.dumps(refer_bot_dict)
                        )

            # Сон — вне сессии: она закрыта, транзакции нет, снапшот отпущен.
            # window_cache при этом живёт дальше, он к сессии не привязан.
            await asyncio.sleep(30)
