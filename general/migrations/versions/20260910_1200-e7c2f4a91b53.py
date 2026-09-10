"""Убрать три неиспользуемые колонки из test_bots и test_orders

Ни одна из трёх не читается и не пишется. Разбор, откуда они взялись и когда
перестали быть нужны, — в `.ai/docs/test-bots/09-roadmap.md`, раздел 4.5.
Коротко:

* `test_bots.copy_bot_max_time_profitability_min` — копибот v1 отбирал донора
  по пересечению двух окон прибыльности, длинного и короткого. Длинное окно
  выкинули из всей логики на следующий день после появления (июль 2025,
  коммиты bf32b5f и cc53888), оставив отбор по одному окну. Идея вернулась
  позже в другом виде — флагом `copybot_v1_check_for_24h_profitability`,
  который прибавляет к окну бота фиксированную проверку за сутки и сейчас
  перебирается по парку. То есть замена в строю, а колонка — её предок.
* `test_orders.referral_bot_from_profit_func` — отладочная сверка: копибот
  брал донора из Redis, а здесь пересчитывал его же на месте, чтобы по данным
  убедиться, что воркер и бот сходятся. Запись закомментирована через четыре
  дня после появления (июль 2025, коммит 89fc413) — она делала на каждое
  открытие сделки те же тяжёлые агрегации, что воркер считает раз в цикл
  сразу для всех. Колонка с тех пор всегда NULL. Тот же вопрос теперь
  закрывает `app/scripts/referral_match_report.py` — постфактум, по свёрткам,
  без цены в рантайме.
* `test_bots.total_profit` — денормализованный кэш суммы P/L. Запись убирали
  трижды независимо (июнь 2025 из бота, август 2025 в отчёте под `if False:`,
  сентябрь 2026 вместе с переписыванием отчёта на свёртки). Хранить в нём
  нечего: за ним нет ни одной строки, которой не было бы в `test_orders` и
  `test_order_rollups`, а `new_bots.py` всё равно делает
  `TRUNCATE test_bots CASCADE` при каждой пересборке парка.

Почему сейчас. `DROP COLUMN` в Postgres не переписывает таблицу, но берёт
`ACCESS EXCLUSIVE` и встаёт в очередь за долгими транзакциями ретеншна и
свёрток. Сегодня в `test_orders` тысяча строк и система в бою не работала;
на полной скорости там 42 миллиона строк в сутки, и то же удаление придётся
делать в окне с остановленным симулятором.

Обратно колонки возвращаются пустыми — данных в них нет и не было:
`total_profit` забит нулями по умолчанию, две другие целиком NULL.
Внешний ключ на `referral_bot_from_profit_func` уходит вместе с колонкой.

Revision ID: e7c2f4a91b53
Revises: d5b3e8f1a06c
Create Date: 2026-09-10 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e7c2f4a91b53"
down_revision = "d5b3e8f1a06c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("test_orders", "referral_bot_from_profit_func")
    op.drop_column("test_bots", "total_profit")
    op.drop_column("test_bots", "copy_bot_max_time_profitability_min")


def downgrade() -> None:
    op.add_column(
        "test_bots",
        sa.Column(
            "copy_bot_max_time_profitability_min",
            sa.Numeric(precision=10, scale=2),
            nullable=True,
            comment=(
                "For copy bot the maximum time it takes for the original bot "
                "being monitored to be profitable"
            ),
        ),
    )
    op.add_column(
        "test_bots",
        sa.Column(
            "total_profit",
            sa.Numeric(precision=20, scale=10),
            server_default="0",
            nullable=False,
            comment="Total profit",
        ),
    )
    op.add_column(
        "test_orders",
        sa.Column("referral_bot_from_profit_func", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "test_orders_referral_bot_from_profit_func_fkey",
        "test_orders",
        "test_bots",
        ["referral_bot_from_profit_func"],
        ["id"],
    )
