#!/bin/bash

set -o pipefail
cd "$(dirname "$0")" || exit 1

# Docker Compose file and environment file
COMPOSE_FILE="docker-compose.dev.yml"
ENV_FILE="../../.env.dev"
PROJECT_NAME="atenea-dev"

function env_int() {
    local key="$1"
    local fallback="$2"
    local value

    value="$(awk -F= -v key="$key" '$1 == key { print substr($0, index($0, "=") + 1); exit }' "$ENV_FILE")"
    if [[ "$value" =~ ^[1-9][0-9]*$ ]]; then
        printf '%s' "$value"
    else
        printf '%s' "$fallback"
    fi
}

function down() {
    echo "Stopping all screen sessions and Docker containers..."
    
    # Define an array of screen session names
    local screens=("atenea-shell" "atenea-runserver" "atenea-beat" "atenea-queue" "atenea-queue-index" "atenea-queue-telegram-scan" "atenea-queue-telegram-comments" "atenea-queue-telegram-populate" "atenea-queue-ner-live" "atenea-queue-ner-backfill" "atenea-queue-ner-store" "atenea-queue-embed" "atenea-queue-sentiment")

    # Loop through the screen sessions and kill each one
    for screen_name in "${screens[@]}"; do
        if screen -list | grep -q "$screen_name"; then
            screen -S "$screen_name" -X quit
            echo "Stopped screen session: $screen_name"
        fi
    done

    # Stop Docker containers
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" -p "$PROJECT_NAME" down
    echo "Stopped Docker containers."
}


function up() {
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" -p "$PROJECT_NAME" up -d --wait || exit 1
    start_app "${1:-INFO}"
}

function start_app() {
    # Get log level from argument or default to INFO
    local log_level="${1:-INFO}"
    local celery_default_concurrency="$(env_int QUEUE_DEFAULT_CONCURRENCY 4)"
    local celery_index_concurrency="$(env_int QUEUE_INDEX_CONCURRENCY 1)"
    local celery_telegram_scan_concurrency="$(env_int QUEUE_TELEGRAM_SCAN_CONCURRENCY 1)"
    local celery_telegram_scan_comments_concurrency="$(env_int QUEUE_TELEGRAM_SCAN_COMMENTS_CONCURRENCY 1)"
    local celery_telegram_populate_concurrency="$(env_int QUEUE_TELEGRAM_POPULATE_CONCURRENCY 1)"
    local celery_ner_live_concurrency="$(env_int QUEUE_NER_LIVE_CONCURRENCY 1)"
    local celery_ner_backfill_concurrency="$(env_int QUEUE_NER_BACKFILL_CONCURRENCY 1)"
    local celery_ner_store_concurrency="$(env_int QUEUE_NER_STORE_CONCURRENCY 1)"
    local celery_embed_concurrency="$(env_int QUEUE_EMBED_CONCURRENCY 1)"
    local celery_sentiment_concurrency="$(env_int QUEUE_SENTIMENT_CONCURRENCY 1)"

    # Check if screens already exist
    if screen -ls | grep -q "atenea-runserver\|atenea-beat\|atenea-queue\|atenea-queue-index\|atenea-queue-telegram-scan\|atenea-queue-telegram-comments\|atenea-queue-telegram-populate\|atenea-queue-ner-live\|atenea-queue-ner-backfill\|atenea-queue-ner-store\|atenea-queue-embed\|atenea-queue-sentiment"; then
        echo "Error: The servers have already been deployed. Please use the 'down' option before re-deploying."
        exit 1
    fi

    echo "Starting Django server and Celery workers in screen sessions (log level: $log_level)..."

    # Initialize analytical storage before starting writers.
    DJANGO_SETTINGS_MODULE=atenea_api.settings.development uv run python manage.py migrate --noinput || exit 1
    DJANGO_SETTINGS_MODULE=atenea_api.settings.development uv run python manage.py init_clickhouse || exit 1

    # Create each session and run the desired command (updated log levels)
    if ! screen -ls | grep -q '\.atenea-shell[[:space:]]'; then
        screen -dmS atenea-shell bash -c 'export DJANGO_SETTINGS_MODULE=atenea_api.settings.development && uv run python manage.py shell'
    fi
    screen -dmS atenea-runserver bash -c 'export DJANGO_SETTINGS_MODULE=atenea_api.settings.development && uv run python manage.py runserver 0.0.0.0:28000'
    screen -dmS atenea-beat bash -c 'export DJANGO_SETTINGS_MODULE=atenea_api.settings.development && uv run celery -A atenea_api beat -l '"$log_level"
    screen -dmS atenea-queue bash -c "export DJANGO_SETTINGS_MODULE=atenea_api.settings.development && uv run celery -A atenea_api worker -Q default -n AT1@%h -l '$log_level' --concurrency=$celery_default_concurrency"
    screen -dmS atenea-queue-index bash -c "export DJANGO_SETTINGS_MODULE=atenea_api.settings.development && uv run celery -A atenea_api worker -Q index-q -n AT2@%h -l '$log_level' --concurrency=$celery_index_concurrency --prefetch-multiplier=1"
    screen -dmS atenea-queue-telegram-scan bash -c "export DJANGO_SETTINGS_MODULE=atenea_api.settings.development && uv run celery -A atenea_api worker -Q telegram-scan-q -n AT3@%h -l '$log_level' --concurrency=$celery_telegram_scan_concurrency --prefetch-multiplier=1"
    screen -dmS atenea-queue-telegram-comments bash -c "export DJANGO_SETTINGS_MODULE=atenea_api.settings.development && uv run celery -A atenea_api worker -Q telegram-scan-comments-q -n AT4@%h -l '$log_level' --concurrency=$celery_telegram_scan_comments_concurrency --prefetch-multiplier=1"
    screen -dmS atenea-queue-telegram-populate bash -c "export DJANGO_SETTINGS_MODULE=atenea_api.settings.development && uv run celery -A atenea_api worker -Q telegram-populate-q -n AT5@%h -l '$log_level' --concurrency=$celery_telegram_populate_concurrency --prefetch-multiplier=1"
    screen -dmS atenea-queue-ner-live bash -c "export DJANGO_SETTINGS_MODULE=atenea_api.settings.development && uv run celery -A atenea_api worker -Q ner-live-q -n AT6@%h -l '$log_level' --concurrency=$celery_ner_live_concurrency --prefetch-multiplier=1"
    screen -dmS atenea-queue-ner-backfill bash -c "export DJANGO_SETTINGS_MODULE=atenea_api.settings.development && uv run celery -A atenea_api worker -Q ner-backfill-q -n AT7@%h -l '$log_level' --concurrency=$celery_ner_backfill_concurrency --prefetch-multiplier=1"
    screen -dmS atenea-queue-ner-store bash -c "export DJANGO_SETTINGS_MODULE=atenea_api.settings.development && uv run celery -A atenea_api worker -Q ner-store-q -n AT8@%h -l '$log_level' --concurrency=$celery_ner_store_concurrency --prefetch-multiplier=1"
    screen -dmS atenea-queue-embed bash -c "export DJANGO_SETTINGS_MODULE=atenea_api.settings.development && uv run celery -A atenea_api worker -Q embed-q -n AT9@%h -l '$log_level' --concurrency=$celery_embed_concurrency --prefetch-multiplier=1"
    screen -dmS atenea-queue-sentiment bash -c "export DJANGO_SETTINGS_MODULE=atenea_api.settings.development && uv run celery -A atenea_api worker -Q sentiment-q -n AT10@%h -l '$log_level' --concurrency=$celery_sentiment_concurrency --prefetch-multiplier=1"

    echo "Django server and Celery workers started successfully."
}


function stop_app() {
    # Warm Celery shutdown: target main worker processes, retain their children
    # until active tasks finish. PostgreSQL, Redis and Elasticsearch stay running.
    python3 - <<'PY_STOP'
import os
from pathlib import Path
import signal
import time

root = str(Path.cwd())

def processes():
    found = {}
    for item in os.scandir('/proc'):
        if not item.name.isdigit():
            continue
        try:
            pid = int(item.name)
            args = [value.decode() for value in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0') if value]
            if os.readlink(f'/proc/{pid}/cwd') != root or not args or 'python' not in os.path.basename(args[0]):
                continue
            if 'manage.py' in args and 'runserver' in args:
                kind = 'django'
            elif any(os.path.basename(value) == 'celery' for value in args) and '-A' in args and 'atenea_api' in args:
                kind = 'beat' if 'beat' in args else 'worker' if 'worker' in args else None
            else:
                continue
            if kind:
                parent = int(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[1])
                found[pid] = (kind, parent)
        except (OSError, ValueError):
            continue
    return found

found = processes()
for kind in ('beat', 'django', 'worker'):
    for pid, (actual, parent) in found.items():
        if actual == kind and (parent not in found or found[parent][0] != kind):
            try:
                os.kill(pid, signal.SIGTERM)
                print(f'Stopping {kind}: {pid}', flush=True)
            except ProcessLookupError:
                pass
started = time.monotonic()
while processes():
    if time.monotonic() - started > 4200:
        raise SystemExit('Active tasks have not finished; services were not forcibly killed')
    time.sleep(2)
print('Django, beat and Celery stopped. Infrastructure and broker queues retained.')
PY_STOP
}

case "${1:-}" in
    down) down ;;
    up) up "${2:-INFO}" ;;
    start-app) start_app "${2:-INFO}" ;;
    stop-app) stop_app ;;
    *) echo "Usage: $0 [up [LOG_LEVEL]|down|start-app [LOG_LEVEL]|stop-app]"; exit 1 ;;
esac
