"""Окно отчёта: как оно делится между свёртками и сырьём.

Ошибка здесь тихая и потому дорогая. Сдвинутая на блок граница не роняет
отчёт — она даёт цифру, которая выглядит правдоподобно и при этом врёт:
либо сделки на стыке посчитаны дважды, либо потеряны. Проверяем, что ветки
стыкуются ровно и что колонки объединения не разъезжаются.

Заглушки, база не нужна:

    python -m tests.test_report_windows
"""
import asyncio

from datetime import datetime, timedelta

from app.crud.test_order_rollup import (
    TestOrderRollupCrud,
    floor_to_bucket,
    split_window,
)
from app.scripts.top_bots_report import build_window, describe

BUCKET = timedelta(minutes=10)
NOW = datetime.fromisoformat("2026-09-08T12:37:00+00:00")


def check_branches_tile_exactly():
    """Свёртки кончаются ровно там, где начинается сырьё."""
    watermark = datetime.fromisoformat("2026-09-08T11:30:00+00:00")
    since = NOW - timedelta(days=7)

    rollup_from, rollup_to, raw_from = split_window(since, watermark, BUCKET)

    assert rollup_to == raw_from, "между ветками щель или нахлёст"
    assert rollup_to == watermark, rollup_to
    assert rollup_from < rollup_to, "свёртки читаются задом наперёд"

    print("  ветки стыкуются по границе свёрнутого — без нахлёста и щели")


def check_start_snaps_down_to_bucket():
    """Начало окна округляется вниз: половины блока не существует."""
    watermark = NOW - timedelta(hours=1)

    for since, expected in (
        ("2026-09-07T12:37:00+00:00", "2026-09-07T12:30:00+00:00"),
        ("2026-09-07T12:30:00+00:00", "2026-09-07T12:30:00+00:00"),
        ("2026-09-07T12:39:59+00:00", "2026-09-07T12:30:00+00:00"),
    ):
        rollup_from, _, _ = split_window(
            datetime.fromisoformat(since), watermark, BUCKET
        )
        assert rollup_from.isoformat() == expected, rollup_from

    print("  начало окна выравнивается по блоку вниз — окно шире, не уже")


def check_window_without_rollups_reads_raw():
    """Свёрток нет — весь отчёт по сырью, и ни строки по свёрткам."""
    since = NOW - timedelta(days=14)

    rollup_from, rollup_to, raw_from = split_window(since, None, BUCKET)

    assert rollup_from is None and rollup_to is None, "нечего читать в свёртках"
    assert raw_from == floor_to_bucket(since, BUCKET), raw_from

    print("  без свёрток отчёт целиком по сырым сделкам")


def check_fresh_window_skips_rollups():
    """Окно короче отставания свёрток — сырьё покрывает его целиком.

    Свёртки отстают на `ROLLUP_LAG_MINUTES`, поэтому отчёт за последний час
    обязан читать только сырьё: в свёртках этого часа ещё нет, а лезть в них
    значило бы получить пустоту вместо цифр.
    """
    watermark = NOW - timedelta(hours=2)
    since = NOW - timedelta(hours=1)

    rollup_from, rollup_to, raw_from = split_window(since, watermark, BUCKET)

    assert rollup_from is None and rollup_to is None, "свёртки тут не нужны"
    assert raw_from == floor_to_bucket(since, BUCKET), raw_from

    print("  свежее окно читается только по сырью")


def check_union_columns_line_up():
    """Обе ветки объединения — одни и те же колонки в одном порядке.

    Имена подзапроса UNION Postgres берёт из первой ветки. Разъехавшийся
    порядок не вызовет ошибки: числа просто сложатся не с теми числами.
    """
    expected = ("bot_id", *TestOrderRollupCrud.STAT_COLUMNS)

    for by_referral in (False, True):
        rollup = TestOrderRollupCrud.rollup_part(
            [], NOW - timedelta(days=1), NOW, by_referral
        )
        raw = TestOrderRollupCrud.raw_part([], NOW, by_referral)

        assert tuple(rollup.selected_columns.keys()) == expected, (
            tuple(rollup.selected_columns.keys())
        )
        assert tuple(raw.selected_columns.keys()) == expected, (
            tuple(raw.selected_columns.keys())
        )

    print("  колонки свёрток и сырья совпадают по именам и порядку")


def check_cli_window():
    assert build_window() is None, "без флагов окно не задано"
    assert build_window(days=0, hours=0) is None, "нулевое окно — то же самое"
    assert build_window(days=14) == timedelta(days=14)
    assert build_window(hours=2, minutes=30) == timedelta(hours=2, minutes=30)

    assert describe(timedelta(days=14)) == "14 сут", describe(timedelta(days=14))
    assert describe(timedelta(hours=2)) == "2 ч"
    assert describe(timedelta(minutes=30)) == "30 мин"

    print("  флаги окна складываются, нулевое окно не выдаётся за настоящее")


async def main():
    print("Окно отчёта:")
    check_branches_tile_exactly()
    check_start_snaps_down_to_bucket()
    check_window_without_rollups_reads_raw()
    check_fresh_window_skips_rollups()
    check_union_columns_line_up()
    check_cli_window()
    print("\nвсё сходится")


if __name__ == "__main__":
    asyncio.run(main())
