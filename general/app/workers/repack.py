"""pg_repack — возврат места операционной системе.

Обычный `DELETE` место системе не возвращает: страницы освобождаются внутри
таблицы, и она перестаёт расти, потому что пишет в них заново. Для ежедневной
чистки этого достаточно, и именно так работает `retention.py`.

`pg_repack` нужен в одном случае: таблица разово раздулась (например, чистка
неделю стояла) и место нужно вернуть именно диску. Цена — свободное место в
размер таблицы вместе с индексами: repack строит копию рядом. На заполненном
диске он его добьёт, поэтому включается явно, через `RETENTION_REPACK=true`.
"""
import asyncio
import logging
import os
import time

from dotenv import load_dotenv


async def repack_table(table_name: str) -> bool:
    """Перепаковывает таблицу. Возвращает, получилось ли."""
    started_at = time.monotonic()

    load_dotenv()

    command = [
        "pg_repack",
        f"--host={os.getenv('DB_HOST')}",
        f"--port={os.getenv('DB_PORT')}",
        f"--dbname={os.getenv('DB_NAME')}",
        f"--username={os.getenv('DB_SUPER_USER')}",
        f"--table={table_name}",
        "--no-order",
        "--wait-timeout=300",
        "--echo",
    ]

    env = os.environ.copy()
    env["PGPASSWORD"] = os.getenv("DB_SUPER_PASSWORD")

    logging.info(f"⏳ Запуск pg_repack для таблицы: {table_name}")

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await process.communicate()
    except FileNotFoundError:
        logging.error(
            "❌ pg_repack не найден в контейнере 'general'. Нужен пакет "
            "postgresql-15-repack в general/Dockerfile."
        )
        return False
    except Exception as error:
        logging.error(f"❌ Неожиданная ошибка pg_repack: {error}")
        return False

    elapsed = time.monotonic() - started_at

    if process.returncode != 0:
        logging.error(
            f"❌ pg_repack для {table_name} завершился с кодом "
            f"{process.returncode}\n"
            f"stdout: {stdout.decode().strip()}\n"
            f"stderr: {stderr.decode().strip()}"
        )
        return False

    logging.info(
        f"✅ pg_repack завершён для {table_name} за {elapsed:.1f} с\n"
        f"{stdout.decode().strip()}"
    )

    return True
