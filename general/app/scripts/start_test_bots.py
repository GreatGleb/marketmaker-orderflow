"""Точка входа симулятора. Один процесс ведёт один шард парка ботов.

Зачем шарды. Событийный цикл одного процесса вытягивает около 70 тысяч
пробуждений корутин в секунду на ядро, а 17 236 ботов с тактом 100 мс
требуют 172 тысяч — один процесс физически не успевает, и такт цикла
удержания растягивается. Поэтому симулятор запускается несколькими
процессами, каждый берёт ботов с `id % shards == shard`. Правило —
4–7 тысяч ботов на процесс.

Число процессов задаёт `TEST_BOTS_SHARDS`: supervisord поднимает столько же
копии программы `test_bots` (`numprocs`) и передаёт каждой свой `--shard`.
Вручную:

    python -m app.scripts.start_test_bots                  # весь парк
    python -m app.scripts.start_test_bots --shard 0 --shards 4

Шардировать можно только сам симулятор: `set_profitable_bot` и
`set_volatile_pairs` пишут общие ключи Redis и должны остаться в одном
экземпляре.
"""
import argparse
import asyncio
import sys
import threading

from datetime import timezone

from app.bots.demo_test_bot import StartTestBotsCommand

# from app.workers.bulk_insert_orders import OrderBulkInsertCommand
# from app.workers.profitable_bot_updater import ProfitableBotUpdaterCommand
# from app.workers.volatile_pair import VolatilePairCommand

UTC = timezone.utc


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Симулятор тестовых ботов (один шард парка).",
    )
    parser.add_argument(
        "--shards", type=int, default=1,
        help="на сколько процессов разделён парк (по умолчанию 1 — весь парк)",
    )
    parser.add_argument(
        "--shard", type=int, default=0,
        help="номер этого процесса, от 0 до shards-1",
    )
    args = parser.parse_args(argv)

    if args.shards < 1:
        parser.error("--shards должно быть не меньше 1")

    # Иначе процесс молча возьмёт пустую долю парка и будет вечно писать
    # «нет активных ботов», а часть настоящих ботов останется без симулятора.
    if not 0 <= args.shard < args.shards:
        parser.error(
            f"--shard должен быть от 0 до {args.shards - 1}, "
            f"получено {args.shard}"
        )

    return args


def input_listener(loop, stop_event):
    while True:
        try:
            cmd = (
                input("👉 Введите 'stop' чтобы остановить бота:\n")
                .strip().lower()
            )
        except (EOFError, OSError):
            # Ввода в этом запуске не будет: stdin закрыт или кончился.
            # Останавливают тогда через supervisorctl, а поток тут не нужен.
            return

        if cmd == "stop":
            print("🛑 Останавливаем бота...")
            loop.call_soon_threadsafe(stop_event.set)
            break


async def main(argv=None):
    args = parse_args(argv)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    # daemon=True: под supervisord вводить 'stop' некому (там останавливают
    # через `supervisorctl stop test_bots:*`), и поток навсегда остаётся
    # висеть на чтении stdin. Обычный поток в таком состоянии не дал бы
    # процессу завершиться — с шардами это N процессов, которые нечем
    # погасить, кроме SIGKILL.
    if sys.stdin is not None:
        threading.Thread(
            target=input_listener, args=(loop, stop_event), daemon=True
        ).start()

    await asyncio.gather(
        # VolatilePairCommand(stop_event=stop_event).run_async(),
        StartTestBotsCommand(
            stop_event=stop_event, shard=args.shard, shards=args.shards
        ).run_async(),
        # ProfitableBotUpdaterCommand(stop_event=stop_event).run_async(),
        # OrderBulkInsertCommand(stop_event=stop_event).run_async(),
    )

    print("✅ Все боты завершены.")


if __name__ == "__main__":
    asyncio.run(main())
