import asyncio
import logging
import time

from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Depends

from redis.asyncio import Redis

from app.crud.asset_history import AssetHistoryCrud
from app.crud.test_bot import TestBotCrud
from app.dependencies import (
    get_session,
    get_redis,
    resolve_crud,
)
from app.config import settings
from app.constants.volatility import (
    MIN_QUOTE_VOLUME_24H,
    MIN_TICKS_IN_WINDOW,
    VOLATILE_SYMBOL_TTL_SECONDS,
    most_volatile_symbol_key,
)
from app.db.base import DatabaseSessionManager

from app.utils import Command

UTC = timezone.utc


class VolatilePairCommand(Command):

    def __init__(self, stop_event, is_need_list_of_symbols=False):
        super().__init__()
        self.stop_event = stop_event
        self.is_need_list_of_symbols = is_need_list_of_symbols

    async def command(
        self,
        redis: Redis = Depends(get_redis),
    ):

        logging.basicConfig(
            format='%(asctime)s - %(levelname)s - %(message)s',
            level=logging.INFO
        )

        dsm = DatabaseSessionManager.create(settings.DB_URL)

        while not self.stop_event.is_set():
            start_time = time.time()

            try:
                async with dsm.get_session() as session:
                    bot_crud = TestBotCrud(session)
                    asset_crud = AssetHistoryCrud(session)

                    # Перечитываем каждый цикл, а не один раз на старте:
                    # иначе боты с новым таймфреймом, заведённые уже после
                    # запуска воркера, никогда бы не получили свою пару.
                    asset_volatility_timeframes = list(
                        await bot_crud.get_unique_min_timeframe_volatility_values()
                    )

                    most_volatile = None
                    tf_str = None
                    symbol = None

                    logging.info(f'self.is_need_list_of_symbols {self.is_need_list_of_symbols}')

                    if not self.is_need_list_of_symbols:
                        for tf_str in asset_volatility_timeframes:
                            tf = float(tf_str)
                            now = datetime.now(UTC)
                            time_ago = now - timedelta(minutes=tf)

                            most_volatile = await asset_crud.get_most_volatile_since(
                                since=time_ago
                            )

                            key = most_volatile_symbol_key(tf_str)

                            if most_volatile:
                                symbol = most_volatile.symbol
                                # TTL: если воркер умрёт, ключ протухнет и боты
                                # встанут, а не будут торговать старой парой.
                                await redis.set(
                                    key, symbol, ex=VOLATILE_SYMBOL_TTL_SECONDS
                                )
                                # Метрики в логе — чтобы было видно, что
                                # именно выбрано: по одному размаху не понять,
                                # мусор это или настоящее движение.
                                spread = float(most_volatile.volatility) * 100
                                volume = float(most_volatile.quote_volume_24h)
                                logging.info(
                                    f"{key} updated: {symbol} "
                                    f"(размах {spread:.2f}%, "
                                    f"тиков {most_volatile.ticks}, "
                                    f"оборот за сутки {volume:,.0f})"
                                )
                            else:
                                # Ключ намеренно не трогаем: пусть протухнет и
                                # боты встанут. Торговать неликвидом хуже, чем
                                # простоять окно.
                                logging.info(
                                    f"{key}: ни одна пара не прошла отбор "
                                    f"(нужно от {MIN_TICKS_IN_WINDOW} тиков в "
                                    f"окне и оборот от "
                                    f"{MIN_QUOTE_VOLUME_24H:,} USDT)"
                                )
                    else:
                        now = datetime.now(UTC)
                        time_ago = now - timedelta(hours=1)

                        most_volatiles = await asset_crud.get_most_volatiles_since(
                            since=time_ago
                        )
                        if most_volatiles:
                            for i, row in enumerate(most_volatiles, start=1):
                                spread = float(row.volatility) * 100
                                volume = float(row.quote_volume_24h)
                                logging.info(
                                    f"{i} most_volatile_symbol: {row.symbol} "
                                    f"размах={spread:.2f}% тиков={row.ticks} "
                                    f"оборот={volume:,.0f}"
                                )
                        else:
                            logging.info(
                                f"За час ни одна пара не прошла отбор (нужно "
                                f"от {MIN_TICKS_IN_WINDOW} тиков в окне и "
                                f"оборот от {MIN_QUOTE_VOLUME_24H:,} USDT)"
                            )
                        logging.info('\n')

                    # if most_volatile and tf_str and symbol:
                    #     print(f"most_volatile_symbol_{tf_str} updated: {symbol}")
            except Exception as e:
                logging.info(f'Error while set volatile pairs: {e}')
                await asyncio.sleep(60)

            end_time = time.time()
            elapsed_time = end_time - start_time
            wait_time = 30 - elapsed_time
            await asyncio.sleep(max(wait_time, 1))
