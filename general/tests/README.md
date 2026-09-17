# Проверки подсистемы тестовых ботов

Обычные скрипты на голых `assert`, без pytest — его нет ни в образе, ни в
зависимостях, а тащить его ради десятка проверок и пересобирать образ дороже,
чем оно того стоит.

Запуск изнутри контейнера:

```bash
docker exec -it orderflow_general python -m tests.run_all
docker exec -it orderflow_general python -m tests.test_price_cache
```

Или разово, без поднятого стека:

```bash
docker run --rm --entrypoint python \
  --network marketmaker-orderflow_redis_network \
  -e PYTHONPATH=/opt/services/app/src \
  -v "$PWD/general:/opt/services/app/src" -w /opt/services/app/src \
  marketmaker-orderflow-general -m tests.run_all
```

Тесты, которым нужна база или Redis, сами скажут об этом и завершатся с
понятной ошибкой. Чистых проверок на заглушках это не касается — они идут
где угодно.

`python -m tests.test_watched_affordability` — фильтр покупки с номиналом
до `WATCHED_PAIR_MAX_BUY_NOTIONAL_USDT`: Decimal, дробные количества,
оба фильтра лота, минимальный номинал по mark price, граница бюджета,
неполные/устаревшие данные, курс USDC, обновление спецификаций и удаление
недоступных старых/закреплённых пар. Только заглушки, сеть и БД не нужны.
