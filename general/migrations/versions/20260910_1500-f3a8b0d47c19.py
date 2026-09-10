"""Переименовать copybot_v1_check_for_referral_bot_profitability

Колонка называлась `copybot_v1_check_for_referral_bot_profitability`, то есть
«проверить, что донор прибылен как донор». Читалось это как проверка
качества, а работало иначе: фильтр **требовал положительной** донорской
прибыли, и «истории копирования нет» приравнивалось к «плохой донор».

Донорская история есть только у тех, кого уже выбирали донором. Копиботов v1
восемьдесят, и те, у кого совпадают окно и флаги, берут одного и того же
донора, — значит в пул попадало несколько десятков ботов из пятнадцати тысяч.
Фильтр резал круг кандидатов примерно в двести раз и запирал отбор на
инкумбентах. На свежем парке он вырождался совсем: `TRUNCATE test_bots`,
никто ещё никого не копировал, `referral_bot_id` везде `NULL` — и половина
копиботов v1 не могла начать торговать, пока вторая половина не наработает
историю.

Смысл у проверки есть, но другой: она сигнализация на регресс. Поле стратегии
добавляется в три места (`get_bot_config_by_params`,
`update_config_from_referral_bot`, сборка в `binance_bot`), и потеря одного из
них ничем больше не ловится — так уже терялись `use_trailing_stop` и
`ma_number_of_candles_*`. Донор при этом по своим сделкам выглядит прекрасно,
а копиры торгуют не тем конфигом и уходят в минус.

Поэтому фильтр перевёрнут: теперь он выбрасывает доноров, у которых копиры
**ушли в минус** за сутки, а отсутствие истории считает нейтральным. Пул
остаётся полным, сигнализация работает. Имя приведено к тому, что колонка
делает: `copybot_v1_exclude_losing_donors`.

Данные переименование не трогает: `false` по-прежнему «фильтр выключен»,
`true` — «включён». Смысл `true` при этом поменялся, поэтому статистику,
накопленную до этой миграции, с накопленной после сравнивать нельзя — но её
и нет, парк в бою не работал.

Revision ID: f3a8b0d47c19
Revises: e7c2f4a91b53
Create Date: 2026-09-10 15:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "f3a8b0d47c19"
down_revision = "e7c2f4a91b53"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "test_bots",
        "copybot_v1_check_for_referral_bot_profitability",
        new_column_name="copybot_v1_exclude_losing_donors",
        comment=(
            "Skip donors whose copybots lost money over the last 24 hours. "
            "No copy history is not a reason to skip: the filter excludes "
            "the proven-bad, it does not require the proven-good"
        ),
    )


def downgrade() -> None:
    op.alter_column(
        "test_bots",
        "copybot_v1_exclude_losing_donors",
        new_column_name="copybot_v1_check_for_referral_bot_profitability",
        comment="Checks whether bots are profitable when working from copybots",
    )
