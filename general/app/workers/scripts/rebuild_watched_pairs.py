"""Раз в сутки пересобирает список отслеживаемых пар по резким скачкам.

Живёт в контейнере приложения, а не в celery: пересборка останавливает
симулятор через supervisorctl, а supervisord у каждого контейнера свой —
из celery-воркера процессы orderflow_general не остановить.

Первый прогон — не сразу после старта, а через сутки: список уже собран при
init, и дёргать симулятор на каждом перезапуске контейнера незачем.
"""
import asyncio
import logging

from app.scripts.seed_watched_pairs import seed_watched_pairs

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

REBUILD_INTERVAL_SECONDS = 24 * 60 * 60
# Сколько ждать после старта контейнера до первой пересборки.
FIRST_RUN_DELAY_SECONDS = REBUILD_INTERVAL_SECONDS
# По какому окну истории считать скачки.
HISTORY_HOURS = 24
WATCHED_PAIRS_TOP = 50


async def main():
    logging.info(
        f"Пересборка watched_pair раз в {REBUILD_INTERVAL_SECONDS // 3600} ч. "
        f"Первая — через {FIRST_RUN_DELAY_SECONDS // 3600} ч."
    )
    await asyncio.sleep(FIRST_RUN_DELAY_SECONDS)

    while True:
        try:
            await seed_watched_pairs(
                top=WATCHED_PAIRS_TOP, hours=HISTORY_HOURS, replace=True
            )
        except Exception as e:
            # Не даём процессу умереть: supervisor его перезапустит, и первая
            # пересборка снова уедет на сутки вперёд.
            logging.info(f"❌ Пересборка watched_pair не удалась: {e}")

        await asyncio.sleep(REBUILD_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
