import asyncio
import os
import time
from datetime import datetime, timedelta, UTC

from dotenv import load_dotenv
from fastapi.params import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis

from app.crud.asset_history import AssetHistoryCrud
from app.utils import Command, CommandResult

from app.dependencies import (
    get_session,
    get_redis,
    resolve_crud,
)


class ClearOldAssetsHistoryCommand(Command):

    async def command(
        self,
        session: AsyncSession = Depends(get_session),
        asset_crud: AssetHistoryCrud = resolve_crud(AssetHistoryCrud),
        redis: Redis = Depends(get_redis),
    ) -> CommandResult:
        self.redis = redis

        cutoff = datetime.now(UTC) - timedelta(days=1)
        await asset_crud.delete_older_than(cutoff)

        repack_result = await self.clear_disk()
        if not repack_result.success:
            return repack_result

        return CommandResult(success=True)

    async def clear_disk(self) -> CommandResult:
        table_name = "asset_history"
        start_time = time.time()

        # await self.redis.set(f"{table_name}:stop", 1)

        load_dotenv()

        pg_repack_command = [
            "pg_repack",
            f"--host={os.getenv("DB_HOST")}",
            f"--port={os.getenv("DB_PORT")}",
            f"--dbname={os.getenv("DB_NAME")}",
            f"--username={os.getenv("DB_SUPER_USER")}",
            f"--table={table_name}",
            "--no-order",
            "--wait-timeout=300",
            "--echo"
        ]

        pg_env = os.environ.copy()
        pg_env["PGPASSWORD"] = os.getenv("DB_SUPER_PASSWORD")

        print(f"⏳ Запуск pg_repack для таблицы: {table_name}")
        success = True
        try:
            process = await asyncio.create_subprocess_exec(
                *pg_repack_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=pg_env
            )

            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                print(f"✅ pg_repack успешно завершен для {table_name}.")
                print("--- Вывод pg_repack ---")
                print(stdout.decode().strip())
                print("------------------------")
            else:
                error_message = f"pg_repack завершился с ошибкой для {table_name}. Код выхода: {process.returncode}\n"
                error_message += f"stdout: {stdout.decode().strip()}\n"
                error_message += f"stderr: {stderr.decode().strip()}"
                print(f"❌ {error_message}")
                success = False
        except FileNotFoundError:
            err_msg = "❌ Ошибка: Команда pg_repack не найдена внутри контейнера 'general'. Пожалуйста, убедитесь, что 'postgresql-15-repack' установлен в вашем 'general/Dockerfile'."
            print(err_msg)
            success = False
        except Exception as e:
            err_msg = f"❌ Произошла неожиданная ошибка во время pg_repack: {e}"
            print(err_msg)
            success = False

        # await self.redis.delete(f"{table_name}:stop")

        end_time = time.time()
        elapsed_time = end_time - start_time

        minutes = int(elapsed_time // 60)
        seconds = elapsed_time % 60

        print(f"Время, чтобы очистить БД от старой истории цен: {minutes} минут и {seconds:.2f} секунд")

        return CommandResult(success=success)

async def main() -> None:
    await ClearOldAssetsHistoryCommand().run_async()


if __name__ == "__main__":
    print("🧹 Starting ClearOldAssetsHistoryCommand...")
    asyncio.run(main())
    print("✅ ClearOldAssetsHistoryCommand finished.")
