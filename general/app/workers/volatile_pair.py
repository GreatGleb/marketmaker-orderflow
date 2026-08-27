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

                            if most_volatile:
                                symbol = most_volatile.symbol
                                key = most_volatile_symbol_key(tf_str)
                                # TTL: если воркер умрёт, ключ протухнет и боты
                                # встанут, а не будут торговать старой парой.
                                await redis.set(
                                    key, symbol, ex=VOLATILE_SYMBOL_TTL_SECONDS
                                )
                                logging.info(f"{key} updated: {symbol}")
                    else:
                        now = datetime.now(UTC)
                        time_ago = now - timedelta(hours=1)

                        most_volatiles = await asset_crud.get_most_volatiles_since(
                            since=time_ago
                        )
                        if most_volatiles:
                            i = 1
                            for most_volatile in most_volatiles:
                                logging.info(f"{i} most_volatile_symbol: {most_volatile}")
                                i = i + 1
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
