from decimal import Decimal

# Ключ, под которым воркер set_volatile_pairs держит самую волатильную пару
# за окно в timeframe минут. Пишет app/workers/volatile_pair.py, читает
# app/bots/demo_test_bot.py — формат обязан совпадать с точностью до символа.
#
# Отсюда и функция: min_timeframe_asset_volatility приходит из БД как
# Decimal('2.00'), из JSON копибота — как строка '2', а в сетке создания
# ботов это float 0.5. Без нормализации получались разные ключи для одного и
# того же таймфрейма (в истории это уже ломалось, коммит f59c071).
VOLATILE_SYMBOL_KEY_PREFIX = "most_volatile_symbol_"

# Сколько ключ живёт в Redis. Если воркер умер, ключ протухает и боты
# останавливаются, а не торгуют устаревшей парой (коммит a8ac380).
VOLATILE_SYMBOL_TTL_SECONDS = 60


def most_volatile_symbol_key(timeframe) -> str:
    # format(..., 'f') — чтобы 10 минут не превратились в '1E+1'.
    normalized = format(Decimal(str(timeframe)).normalize(), 'f')

    return f"{VOLATILE_SYMBOL_KEY_PREFIX}{normalized}"
