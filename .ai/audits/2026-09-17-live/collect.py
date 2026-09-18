"""Сохраняет наблюдения живого прогона, не меняя БД и процессы приложения.

Запуск с хоста: python3 collect.py --minutes 90 --output /tmp/папка-прогона
Для docker exec нужен доступ к Docker. Приложение после сбора остаётся работать.
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import time


def capture(path, args, stdin=None):
    try:
        result = subprocess.run(args, input=stdin, text=True,
                                capture_output=True, timeout=45)
        path.write_text(result.stdout + result.stderr)
        return result.returncode
    except subprocess.TimeoutExpired:
        path.write_text("Сбор этого показателя превысил 45 секунд.\n")
        return 124


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--minutes', type=float, default=90)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--database', default='postgres')
    parser.add_argument('--redis-db', default='0')
    parser.add_argument('--started', default='2026-09-17 18:46:45+00')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    sql = Path(__file__).with_name('checks.sql').read_text()
    deadline = time.monotonic() + args.minutes * 60
    while True:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        sample = args.output / stamp
        sample.mkdir()
        commands = {
            'checks.txt': (['docker', 'exec', '-i', 'orderflow_postgres',
                            'psql', '-U', 'postgres', '-d', args.database,
                            '-v', 'started=' + args.started], sql),
            'processes.txt': (['docker', 'exec', 'orderflow_general',
                               'supervisorctl', 'status'], None),
            'queue.txt': (['docker', 'exec', 'orderflow_redis',
                           'redis-cli', '-n', args.redis_db, 'LLEN', 'order_queue'], None),
            'resources.txt': (['docker', 'stats', '--no-stream', '--format',
                '{{.Name}} {{.CPUPerc}} {{.MemUsage}}', 'orderflow_general',
                'orderflow_postgres', 'orderflow_redis', 'orderflow_celery_worker'], None),
            'celery.txt': (['docker', 'logs', '--tail', '30',
                            'orderflow_celery_worker'], None),
        }
        statuses = {name: capture(sample / name, cmd, stdin)
                    for name, (cmd, stdin) in commands.items()}
        print(stamp, statuses, flush=True)
        if time.monotonic() >= deadline:
            print('Сбор завершён; приложение продолжает работать.', flush=True)
            break
        time.sleep(min(60, max(0, deadline - time.monotonic())))


if __name__ == '__main__':
    main()
