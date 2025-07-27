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

docker exec -it orderflow_general python -m app.scripts.top_bots_report

# Тестирование Telegram уведомлений
docker exec -it orderflow_general python -m app.scripts.test_telegram_notifications
```

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
6. 📱 Отправка уведомлений в Telegram об ошибках и успешных операциях

## 📱 Telegram уведомления

Система автоматически отправляет уведомления в Telegram о:
- ❌ Ошибках в работе ботов
- 🔧 Ошибках Celery задач

### Разделение уведомлений по чатам

Вы можете настроить разные чаты для разных типов уведомлений:
- **Чат ошибок ботов** - только для ошибок в работе ботов
- **Чат ошибок Celery** - только для ошибок в фоновых задачах

### Настройка Telegram уведомлений

1. Создайте бота через @BotFather в Telegram
2. Получите ID чатов/каналов для уведомлений
3. Добавьте в `.env`:
   ```env
   TELEGRAM_BOT_TOKEN=your_bot_token_here
   TELEGRAM_BOT_ERRORS_CHAT_ID=your_bot_errors_chat_id_here
   TELEGRAM_CELERY_ERRORS_CHAT_ID=your_celery_errors_chat_id_here
   ```
4. Подробная инструкция: [TELEGRAM_SETUP.md](general/TELEGRAM_SETUP.md)

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
