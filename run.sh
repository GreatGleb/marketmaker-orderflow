#!/bin/bash

CONTAINER_NAME="orderflow_general"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

run_script_in_container() {
    local script_module=$1
    echo -e "${CYAN}--- Выполняю в контейнере '$CONTAINER_NAME': python -m $script_module ---${NC}"
    docker exec -it "$CONTAINER_NAME" python -m "$script_module"
}

show_help() {
    echo -e "${YELLOW}Использование:${NC}"
    echo "  ./run.sh [команда] [аргументы]"
    echo ""
    echo -e "${YELLOW}Основные команды:${NC}"
    echo "  start         - Собрать и запустить проект в фоновом режиме (docker-compose up --build -d)"
    echo "  stop          - Остановить проект (docker-compose down)"
    echo "  restart       - Перезапустить проект"
    echo "  logs          - Показать логи контейнеров (в реальном времени)"
    echo "  shell         - Открыть командную строку (bash) внутри контейнера"
    echo "  init          - Полная настройка с нуля: контейнеры, миграции, сиды, боты"
    echo "                  Флаги: --skip-commissions (быстрее), --recreate-bots (пересоздать парк)"
    echo "  scripts [имя] - Запустить указанный python-скрипт"
    echo ""
    echo -e "${YELLOW}Доступные скрипты для команды 'scripts':${NC}"
    echo "  watch         - app.scripts.watch_ws_and_save"
    echo "  demo          - app.bots.demo_test_bot"
    echo "  report        - app.scripts.top_bots_report"
    echo "  refmatch      - app.scripts.referral_match_report (совпадал ли донор копибота)"
    echo "  seed_data     - app.scripts.seed_binance_data"
    echo "  seed_pairs    - app.scripts.seed_watched_pairs (топ пар по резким скачкам)"
    echo "  commissions   - app.scripts.seed_commission_rates (ставки maker/taker по парам)"
    echo ""
    echo -e "${YELLOW}Примеры:${NC}"
    echo "  ./run.sh start"
    echo "  ./run.sh scripts watch"
    echo "  ./run.sh init"
    echo "  ./run.sh init --skip-commissions"
    echo ""
    echo -e "Если запустить без команд (${YELLOW}./run.sh${NC}), будет показана эта справка."
}


if [ -z "$1" ]; then
    show_help
    exit 0
fi

case "$1" in
    start)
        echo -e "${GREEN}Собираю и запускаю проект в фоновом режиме (docker-compose up --build -d)...${NC}"
        docker-compose up --build -d
        ;;
    stop)
        echo -e "${YELLOW}Останавливаю проект (docker-compose down)...${NC}"
        docker-compose down
        ;;
    restart)
        echo -e "${YELLOW}Перезапускаю проект...${NC}"
        docker-compose down
        docker-compose up --build -d
        ;;
    init)
        echo -e "${GREEN}Полная настройка проекта. Это займёт до 20 минут.${NC}"

        if [ ! -f general/.env ]; then
            echo -e "${YELLOW}Нет файла general/.env — скопируйте general/.env.example и заполните ключи.${NC}" >&2
            exit 1
        fi

        echo -e "${CYAN}[1/4] Собираю и запускаю контейнеры...${NC}"
        docker-compose up --build -d || exit 1

        echo -e "${CYAN}[2/4] Жду базу данных...${NC}"
        for _ in $(seq 1 60); do
            docker exec orderflow_postgres pg_isready -U postgres >/dev/null 2>&1 && break
            sleep 2
        done
        if ! docker exec orderflow_postgres pg_isready -U postgres >/dev/null 2>&1; then
            echo -e "${YELLOW}База не поднялась. Смотрите: docker-compose logs db${NC}" >&2
            exit 1
        fi

        # boot.sh внутри контейнера сначала прогоняет alembic upgrade head и
        # только потом запускает supervisord — значит его отклик означает,
        # что миграции уже применены.
        echo -e "${CYAN}[3/4] Жду миграции и запуск процессов...${NC}"
        for _ in $(seq 1 90); do
            docker exec "$CONTAINER_NAME" supervisorctl status >/dev/null 2>&1 && break
            sleep 2
        done
        if ! docker exec "$CONTAINER_NAME" supervisorctl status >/dev/null 2>&1; then
            echo -e "${YELLOW}Приложение не поднялось. Смотрите: ./run.sh logs${NC}" >&2
            exit 1
        fi

        echo -e "${CYAN}[4/4] Сиды, ожидание цен, создание ботов...${NC}"
        shift
        docker exec -it "$CONTAINER_NAME" python -m app.scripts.init_setup "$@" || exit 1

        echo -e "${GREEN}Готово. Логи: ./run.sh logs${NC}"
        ;;
    logs)
        echo -e "${CYAN}Показываю логи... (Нажмите Ctrl+C для выхода)${NC}"
        docker-compose logs -f
        ;;
    shell | bash)
        echo -e "${CYAN}Открываю shell в контейнере '$CONTAINER_NAME'...${NC}"
        docker exec -it "$CONTAINER_NAME" bash
        ;;
    scripts)
        if [ -z "$2" ]; then
            echo "Ошибка: не указано имя скрипта." >&2
            show_help
            exit 1
        fi
        
        case "$2" in
            watch | watch_ws_and_save)
                run_script_in_container "app.scripts.watch_ws_and_save"
                ;;
            demo | demo_test_bot)
                run_script_in_container "app.bots.demo_test_bot"
                ;;
            report | top_bots_report)
                run_script_in_container "app.scripts.top_bots_report"
                ;;
            refmatch | referral_match_report)
                run_script_in_container "app.scripts.referral_match_report"
                ;;
            seed_data | seed_binance_data)
                run_script_in_container "app.scripts.seed_binance_data"
                ;;
            seed_pairs | seed_watched_pairs)
                run_script_in_container "app.scripts.seed_watched_pairs"
                ;;
            commissions | seed_commission_rates)
                run_script_in_container "app.scripts.seed_commission_rates"
                ;;
            *)
                echo "Ошибка: неизвестный скрипт '$2'." >&2
                show_help
                exit 1
                ;;
        esac
        ;;
    -h|--help)
        show_help
        ;;
    *)
        echo "Ошибка: неизвестная команда '$1'." >&2
        show_help
        exit 1
        ;;
esac