"""`get_all_active_pairs` работает на сессии из конструктора.

Раньше метод открывал внутри себя новую сессию и присваивал её
`self.session`, игнорируя переданную вызывающим кодом. Все семь вызовов уже
находятся внутри `async with dsm.get_session()`, так что запрос уходил в
отдельную транзакцию, а подмена оставалась на объекте CRUD и после возврата:
следующий метод того же объекта работал бы уже на закрытой сессии.

Заодно проверяется, что в ветке без `is_need_full_info` запрос выбирает
ровно одну колонку. Там было `select(symbol, last_price)`, а результат
читается через `.scalars()` — то есть берётся только первая колонка, и
`last_price` молча терялся. Цену никто из вызывающих не забирал.

И третье: сорванный запрос не должен оставлять чужую сессию непригодной.
Соединение выбрасывается (`invalidate`) и по таймауту, и по ошибке БД —
`rollback` не годится ни там, ни там: по сорванному запросу он не пройдёт, а
по ошибке БД экспайрит ORM-объекты, которые вызывающий код загрузил до
вызова.

Проверки на заглушках, база не нужна.
"""
import asyncio

from app.crud.asset_history import AssetHistoryCrud


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class FakeSession:
    """Сессия вызывающего кода: помнит, что её просили сделать."""

    def __init__(self, rows=None, fail_with=None, hang=False):
        self.rows = rows if rows is not None else ["BTCUSDT", "ETHUSDT"]
        self.fail_with = fail_with
        self.hang = hang
        self.queries = []
        self.invalidated = 0
        self.rollbacks = 0

    async def execute(self, query):
        self.queries.append(query)

        if self.hang:
            await asyncio.sleep(3600)

        if self.fail_with:
            raise self.fail_with

        return FakeResult(self.rows)

    async def invalidate(self):
        self.invalidated += 1

    async def rollback(self):
        self.rollbacks += 1


def selected_columns(query):
    return [d["name"] for d in query.column_descriptions]


async def main():
    # 1. Сессия из конструктора — та же самая до и после вызова.
    session = FakeSession()
    crud = AssetHistoryCrud(session)

    symbols = await crud.get_all_active_pairs()

    assert symbols == ["BTCUSDT", "ETHUSDT"], f"вернулось {symbols}"
    assert len(session.queries) == 1, (
        f"запросов на сессии вызывающего кода: {len(session.queries)} — "
        f"значит метод ушёл в свою сессию"
    )
    assert crud.session is session, (
        "self.session подменена: вызывающий код передал одну сессию, "
        "а объект CRUD унёс другую"
    )
    print("  запрос ушёл в сессию из конструктора, подмены нет")

    # 2. Ветка symbol_price выбирает одну колонку — ровно ту, которую
    #    потом читает .scalars().
    columns = selected_columns(session.queries[0])
    assert columns == ["symbol"], (
        f"колонки запроса: {columns}; вторая колонка теряется в .scalars()"
    )
    print(f"  ветка без full_info выбирает {columns}")

    # 2a. И выбирает без повторов: на одну (symbol, event_time) в
    #     asset_history приходится несколько строк — два источника плюс тики
    #     с совпадающей меткой. Пока в выборке была цена, дубли хоть чем-то
    #     отличались; без неё это повтор символа.
    assert "SELECT DISTINCT" in str(session.queries[0]), (
        "запрос без distinct: одна пара вернётся столько раз, сколько строк "
        "у неё с максимальным event_time"
    )
    print("  и без повторов (distinct)")

    # 3. Ветка full_info по-прежнему отдаёт сущности целиком — там .scalars()
    #    к месту, и quote_asset_volume_24h из new_bots.py:261 доступен.
    session_full = FakeSession(rows=[])
    await AssetHistoryCrud(session_full).get_all_active_pairs(
        is_need_full_info=True
    )
    columns_full = selected_columns(session_full.queries[0])
    assert columns_full == ["AssetHistory"], f"колонки full_info: {columns_full}"
    print(f"  ветка full_info выбирает {columns_full}")

    # 4. Ветка only_symbols не изменилась.
    session_only = FakeSession(rows=[])
    await AssetHistoryCrud(session_only).get_all_active_pairs(
        only_symbols_in_period=True
    )
    assert len(session_only.queries) == 1
    print("  ветка only_symbols тоже идёт по сессии из конструктора")

    # 5. Таймаут: запрос снят на полпути, соединение выбрасываем. ROLLBACK по
    #    соединению с недочитанным ответом не пройдёт.
    session_slow = FakeSession(hang=True)
    result = await AssetHistoryCrud(session_slow).get_all_active_pairs(
        timeout=0.05
    )
    assert result == [], f"по таймауту вернулось {result}, ждали пустой список"
    assert session_slow.invalidated == 1, (
        "после таймаута соединение не выброшено — вызывающий код продолжит "
        "работать на сессии с недочитанным ответом"
    )
    assert session_slow.rollbacks == 0, "после отмены запроса нужен не откат"
    print("  таймаут: пустой список, соединение выброшено")

    # 6. Ошибка БД: транзакция сломана, её тоже надо сбросить — иначе
    #    следующий запрос вызывающего кода упадёт на InFailedSqlTransaction.
    #    И тоже через invalidate, а не rollback: rollback экспайрит
    #    ORM-объекты, уже загруженные вызывающим кодом на этой сессии, а
    #    demo_test_bot.py:144 читает bot.__dict__ у списка ботов,
    #    загруженного до MarketDataBuilder.build(). Проверено на живой базе:
    #    после rollback там пусто и атрибуты падают с MissingGreenlet, после
    #    invalidate объекты просто отцеплены и читаются.
    session_bad = FakeSession(fail_with=RuntimeError("boom"))
    result = await AssetHistoryCrud(session_bad).get_all_active_pairs()
    assert result == [], f"по ошибке вернулось {result}, ждали пустой список"
    assert session_bad.invalidated == 1, "после ошибки БД сессия не сброшена"
    assert session_bad.rollbacks == 0, (
        "rollback здесь нельзя: он экспайрит ORM-объекты вызывающего кода"
    )
    print("  ошибка БД: пустой список, соединение выброшено")

    # 7. Уборка не должна перебивать исходную ошибку своей.
    session_worst = FakeSession(fail_with=RuntimeError("boom"))

    async def invalidate_fails():
        raise RuntimeError("и уборка не прошла")

    session_worst.invalidate = invalidate_fails
    result = await AssetHistoryCrud(session_worst).get_all_active_pairs()
    assert result == [], "падение уборки не должно вылетать наружу"
    print("  сбой самой уборки не выходит наружу")

    print("\nвсё сошлось")


if __name__ == "__main__":
    asyncio.run(main())
