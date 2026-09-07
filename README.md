# marketmaker-orderflow

📈 **marketmaker-orderflow** — модульный Python-бот для алгоритмического маркетмейкинга с акцентом на работу с ордер-флоу и управлением лимитными заявками. Спроектирован как биржезависимый, с возможностью подключения к разным торговым платформам через REST API, Websockets

---

## 🚀 Возможности

- Подключения к торговым платформам, запись и хранение истории изменения цен
- Работа с лимитными ордерами и управление стаканом, поддержка стратегий маркетмейкинга (фиксированный, адаптивный спред и др.)
- Хранение истории сделок и ордеров через SQLAlchemy
- Контейнеризация через Docker
- Гибкая настройка через `.env`
- 📱 **Telegram уведомления** — автоматические уведомления об ошибках и успешных операциях

---

## ⚙️ Технологии

- Python 3.10+
- Docker / docker-compose
- SQLAlchemy + PostgreSQL
- REST API, Websockets
- .env конфигурация

---

## 🛠 Установка

```bash
git clone https://github.com/your-username/marketmaker-orderflow.git
cd marketmaker-orderflow
cp .env.example .env
./run.sh start

./run.sh init
```
> ⚠️ **Перед запуском необходимо указать в `.env` данные API и параметры стратегии.**

## 🛠 Скрипты для запуска

```bash
docker exec -it orderflow_general python -m app.scripts.watch_ws_and_save

docker exec -it orderflow_general python -m app.scripts.start_test_bots

# Отчёт по прибыльности ботов — см. раздел ниже
docker exec -it orderflow_general python -m app.scripts.top_bots_report

# Тестирование Telegram уведомлений
docker exec -it orderflow_general python -m app.scripts.test_telegram_notifications
```

---

## 📊 Отчёт по прибыльности ботов

```bash
docker exec -it orderflow_general python -m app.scripts.top_bots_report
```

Без флагов печатает три периода сразу — за сутки, за неделю и за две недели.

### Флаги

| Флаг | Значение | По умолчанию |
|---|---|---|
| `-d`, `--days` | глубина окна в сутках | — |
| `-H`, `--hours` | глубина окна в часах | — |
| `-m`, `--minutes` | глубина окна в минутах | — |
| `-all`, `--all_history` | за всю сохранённую историю | выкл. |
| `-just_copy`, `--just_copy_bots` | только копиботы v1 | все боты |
| `-just_copy_v2`, `--just_copy_bots_v2` | только копиботы v2 | все боты |
| `-just_not_copy`, `--just_not_copy_bots` | только обычные боты | все боты |
| `-ref`, `--by_referral` | считать по донорам (`referral_bot_id`), а не по самим ботам | выкл. |
| `-top_count`, `--top_count` | сколько ботов показать | 10 |

`-d` / `-H` / `-m` складываются в одно окно: `-d 1 -H 12` — это 36 часов.
Если не задан ни один — печатаются три стандартных периода. `-all`
перекрывает их все.

Фильтры по виду ботов взаимоисключающие: если указать несколько, сработает
первый в порядке `-just_copy` → `-just_copy_v2` → `-just_not_copy`. Значение
у них не важно, важно наличие — `-just_copy 1` и `-just_copy yes` одинаковы.

### Примеры

```bash
# топ-20 обычных ботов за последние 2 часа
docker exec -it orderflow_general python -m app.scripts.top_bots_report -H 2 -just_not_copy 1 -top_count 20

# копиботы v1 за 30 минут
docker exec -it orderflow_general python -m app.scripts.top_bots_report -m 30 -just_copy 1

# две недели по копиботам v2
docker exec -it orderflow_general python -m app.scripts.top_bots_report -d 14 -just_copy_v2 1

# кто из доноров кормит копиботов, за неделю
docker exec -it orderflow_general python -m app.scripts.top_bots_report -d 7 -ref
```

### Что показывает

```
📊 Топ-10 по прибыли — за неделю
   окно: 2026-08-31 21:20 → 2026-09-07 21:26 (168.1 ч)
   источник: свёртки до 2026-09-07 20:20, сырые сделки после

  1. Бот 1 — 💰 P/L 1037.0000, 📈 прибыльных 1009/1009 (100.0%), комиссия 101.6000, закрытий [цель 1009 / стоп 0 / время 0]
```

Строка «источник» — не украшение. Сырые сделки живут 72 часа
(`RETENTION_TEST_ORDERS_HOURS`), поэтому окна глубже считаются по свёрткам
(`test_order_rollups`), а последний час добирается из `test_orders`. Если граница свёрнутого застыла в прошлом — встали свёртки,
разбор в [`.ai/docs/test-bots/10-retention.md`](.ai/docs/test-bots/10-retention.md).

Запросить окно глубже, чем есть данные, не ошибка: отчёт урежет его до
имеющегося и скажет об этом.

---

## 💡 Стратегии

- 🔄 **Адаптивный спред** — динамический расчёт спреда в зависимости от волатильности, ликвидности или глубины рынка.
- 🧠 **ML/AI стратегии** — (в планах) использование машинного обучения для прогнозирования оптимальных цен.
- ⚙️ **Плагинная архитектура** — (в планах) вы можете легко добавить свою стратегию в папку `/strategies`.

---

## 🧠 Как работает

1. Получение рыночных данных с выбранной торговой платформы (через REST API)
2. Расчёт mid-цены и целевых bid/ask на основе стратегии
3. Размещение лимитных ордеров
4. Отслеживание исполнения и повторное выставление ордеров
5. Логирование, запись истории в базу данных
6. 📱 Отправка уведомлений в Telegram об ошибках с информацией об environment

## 📱 Telegram уведомления

Система автоматически отправляет уведомления в Telegram о:
- ❌ Ошибках в работе ботов
- 🔧 Ошибках Celery задач
- 🌍 Environment информации (local, staging, production)

### Разделение уведомлений по темам (Topics)

Уведомления отправляются в разные темы одного форум-чата:
- **TEST_BOT** - для ошибок тестовых ботов
- **CELERY_ERROR** - для ошибок фоновых задач Celery

### Настройка Telegram уведомлений

1. Создайте бота через @BotFather в Telegram
2. Создайте форум-чат (группу с темами)
3. Создайте темы для разных типов уведомлений
4. Получите ID чата и ID тем
5. Добавьте в `.env`:
   ```env
   TELEGRAM_BOT_TOKEN=your_bot_token_here
   TELEGRAM_CHAT_ID=your_chat_id_here
   TELEGRAM_TEST_BOT_TOPIC_ID=your_test_bot_topic_id_here
   TELEGRAM_CELERY_TOPIC_ID=your_celery_topic_id_here
   ```
6. Подробная инструкция: [TELEGRAM_SETUP.md](general/TELEGRAM_SETUP.md)

## 📅 Статус разработки

- [x] Создание репозитория, README.md 
- [ ] Подключение к Binance Futures WebSockets - сохранение данных по всем объёмным котировкам за последние 2 суток
- [ ] Анализ валютных пар, котировок, объёмов, волатильности и т.д.
- [ ] Управление ордерами
- [ ] Тестирование без баланса
- [ ] Тестирование с балансом
- [ ] UI-интерфейс для мониторинга  

---

## 📬 Контакты

- Email: [greatgleb@gmail.com](mailto:greatgleb@gmail)  
- Telegram: [https://t.me/greatgleb](https://t.me/greatgleb)
