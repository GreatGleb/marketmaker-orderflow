"""Заполняет maker/taker комиссии по парам в asset_exchange_specs.

Ставки приходят из Binance (`futures_commission_rate`) и зависят от аккаунта:
VIP-уровень, промо-пары, BNB-скидки. Поэтому это отдельный скрипт с боевыми
ключами, а не часть публичного seed_binance_data (exchangeInfo ставок не
отдаёт).

Эндпоинт запрашивается по одной паре за раз, поэтому по умолчанию
заполняются только пары активных тестовых ботов — их единицы.

    python -m app.scripts.seed_commission_rates            # пары активных ботов
    python -m app.scripts.seed_commission_rates --missing  # все незаполненные
    python -m app.scripts.seed_commission_rates --all      # вообще все пары
    python -m app.scripts.seed_commission_rates -s BTCUSDT ETHUSDT

Пары с не-ASCII именами (мемные перпетуалы вида '龙虾USDT') всегда падают с
APIError -1022 "Signature for this request is not valid": python-binance
подписывает запрос до percent-encoding, и для UTF-8 символов подпись не
сходится. Это ограничение библиотеки, не нашего кода. По таким парам
симулятор берёт константу.
"""
import argparse
import asyncio
import logging

from decimal import Decimal

from app.bots.binance_bot import BinanceBot
from app.config import settings
from app.crud.exchange_pair_spec import AssetExchangeSpecCrud
from app.crud.test_bot import TestBotCrud
from app.db.base import DatabaseSessionManager

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# У /fapi/v1/commissionRate вес 20, а лимит по IP — 2400 веса в минуту.
# 1 запрос в секунду = 1200 веса в минуту, половина лимита: массовый засев
# (--all, ~900 пар) не выбьет ключ из лимитов.
REQUEST_DELAY_SECONDS = 1.0


async def collect_symbols(session, mode, explicit_symbols):
    exchange_crud = AssetExchangeSpecCrud(session)

    if explicit_symbols:
        return explicit_symbols

    if mode == "all":
        symbols = await exchange_crud.get_all_symbols()
        return [row[0] for row in symbols]

    if mode == "missing":
        return await exchange_crud.get_symbols_without_commission_rates()

    bot_crud = TestBotCrud(session)
    return await bot_crud.get_bot_symbols()


async def seed_commission_rates(mode="bots", explicit_symbols=None):
    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        exchange_crud = AssetExchangeSpecCrud(session)
        symbols = await collect_symbols(session, mode, explicit_symbols)

        if not symbols:
            logging.info('Нечего заполнять: список пар пуст.')
            return

        logging.info(f'Запрашиваю ставки по {len(symbols)} парам...')
        binance_bot = BinanceBot(is_need_prod_for_data=True)

        updated = 0
        failed = []

        for symbol in symbols:
            try:
                fees = await binance_bot.fetch_fees_data(symbol)
                maker = Decimal(str(fees["makerCommissionRate"]))
                taker = Decimal(str(fees["takerCommissionRate"]))
            except Exception as e:
                logging.info(f'❌ {symbol}: не удалось получить ставки — {e}')
                failed.append(symbol)
                await asyncio.sleep(REQUEST_DELAY_SECONDS)
                continue

            await exchange_crud.set_commission_rates(
                symbol=symbol, maker_rate=maker, taker_rate=taker
            )
            await session.commit()
            updated += 1

            logging.info(
                f'✅ {symbol}: maker={maker} ({maker * 100}%), '
                f'taker={taker} ({taker * 100}%)'
            )
            await asyncio.sleep(REQUEST_DELAY_SECONDS)

        logging.info(f'Готово. Заполнено пар: {updated}, ошибок: {len(failed)}.')

        if failed:
            logging.info(f'Не заполнены: {failed}')
            logging.info(
                'По этим парам симулятор возьмёт константу из '
                'app/constants/commissions.py.'
            )


def main():
    parser = argparse.ArgumentParser(
        description="Заполняет maker/taker комиссии по парам из Binance."
    )
    parser.add_argument(
        '--all', action='store_true',
        help="Все пары из asset_exchange_specs (долго)."
    )
    parser.add_argument(
        '--missing', action='store_true',
        help="Только пары без заполненной taker-ставки."
    )
    parser.add_argument(
        '-s', '--symbols', nargs='+',
        help="Явный список пар."
    )
    args = parser.parse_args()

    if args.all:
        mode = "all"
    elif args.missing:
        mode = "missing"
    else:
        mode = "bots"

    asyncio.run(seed_commission_rates(mode=mode, explicit_symbols=args.symbols))


if __name__ == "__main__":
    main()
