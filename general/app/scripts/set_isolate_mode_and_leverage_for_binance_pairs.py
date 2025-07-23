from app.db.base import DatabaseSessionManager
from app.config import settings
import asyncio
from app.crud.exchange_pair_spec import AssetExchangeSpecCrud
from app.bots.binance_bot import BinanceBot


async def run():
    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        binance_bot = BinanceBot()

        # print('start get balance')
        #
        # balance = await binance_bot._safe_from_time_err_call_binance(
        #     binance_bot.binance_client.futures_account_balance
        # )
        # print('finish get balance')
        # balanceUSDT = 0
        #
        # for accountAlias in balance:
        #     if accountAlias['asset'] == 'USDT':
        #         balanceUSDT = accountAlias['balance']
        # print(f'balanceUSDT: {balanceUSDT}')

        exchange_crud = AssetExchangeSpecCrud(session)
        await exchange_crud.set_isolate_mode_and_leverage(binance_bot)


if __name__ == "__main__":
    asyncio.run(run())
