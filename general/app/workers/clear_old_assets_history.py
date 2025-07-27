import asyncio
import os
from datetime import datetime, timedelta, UTC

from dotenv import load_dotenv
from fastapi.params import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.asset_history import AssetHistoryCrud
from app.dependencies import get_session, resolve_crud
from app.utils import Command, CommandResult


class ClearOldAssetsHistoryCommand(Command):

    async def command(
        self,
        session: AsyncSession = Depends(get_session),
        asset_crud: AssetHistoryCrud = resolve_crud(AssetHistoryCrud),
    ) -> CommandResult:

        cutoff = datetime.now(UTC) - timedelta(days=1)
        await asset_crud.delete_older_than(cutoff)

        repack_result = await self.clear_disk()
        if not repack_result.success:
            return repack_result

        return CommandResult(success=True)

    async def clear_disk(self) -> CommandResult:
        table_name = "asset_history"

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
                return CommandResult(success=True)
            else:
                error_message = f"pg_repack завершился с ошибкой для {table_name}. Код выхода: {process.returncode}\n"
                error_message += f"stdout: {stdout.decode().strip()}\n"
                error_message += f"stderr: {stderr.decode().strip()}"
                print(f"❌ {error_message}")
                return CommandResult(success=False)

        except FileNotFoundError:
            err_msg = "❌ Ошибка: Команда pg_repack не найдена внутри контейнера 'general'. Пожалуйста, убедитесь, что 'postgresql-15-repack' установлен в вашем 'general/Dockerfile'."
            print(err_msg)
            return CommandResult(success=False)
        except Exception as e:
            err_msg = f"❌ Произошла неожиданная ошибка во время pg_repack: {e}"
            print(err_msg)
            return CommandResult(success=False)

async def main() -> None:
    await ClearOldAssetsHistoryCommand().run_async()


if __name__ == "__main__":
    print("🧹 Starting ClearOldAssetsHistoryCommand...")
    asyncio.run(main())
    print("✅ ClearOldAssetsHistoryCommand finished.")
