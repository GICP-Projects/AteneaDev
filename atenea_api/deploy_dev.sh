#!/bin/bash

# Docker Compose file and environment file
COMPOSE_FILE="development.yaml"
ENV_FILE="../.env.dev"
PROJECT_NAME="atenea-dev"

function down() {
    echo "Stopping all screen sessions and Docker containers..."
    
    # Define an array of screen session names
    local screens=("atenea-shell" "atenea-runserver" "atenea-beat" "atenea-queue" "atenea-queue-index" "atenea-queue-ner" "atenea-queue-embed" "atenea-queue-sentiment")

    # Loop through the screen sessions and kill each one
    for screen_name in "${screens[@]}"; do
        if screen -list | grep -q "$screen_name"; then
            screen -S "$screen_name" -X quit
            echo "Stopped screen session: $screen_name"
        fi
    done

    # Stop Docker containers
    docker-compose -f $COMPOSE_FILE --env-file $ENV_FILE -p $PROJECT_NAME down
    echo "Stopped Docker containers."
}


function up() {
    echo "Starting Docker containers..."
    
    # Get log level from argument or default to INFO
    local log_level="${1:-INFO}"

    # Start Docker containers
    docker-compose -f $COMPOSE_FILE --env-file $ENV_FILE -p $PROJECT_NAME up -d
    if [[ $? -ne 0 ]]; then
        echo "Error: Failed to start Docker containers."
        exit 1
    fi
    echo "Docker containers started successfully."

    # Check if screens already exist
    if screen -ls | grep -q "atenea-runserver\|atenea-beat\|atenea-queue\|atenea-queue-index\|atenea-queue-ner\|atenea-queue-embed"; then
        echo "Error: The servers have already been deployed. Please use the 'down' option before re-deploying."
        exit 1
    fi

    echo "Starting Django server and Celery workers in screen sessions (log level: $log_level)..."
    
    # Create each session and run the desired command (updated log levels)
    screen -dmS atenea-shell bash -c 'source venv/bin/activate && export DJANGO_SETTINGS_MODULE=atenea_api.settings.development && python manage.py shell'
    screen -dmS atenea-runserver bash -c 'source venv/bin/activate && export DJANGO_SETTINGS_MODULE=atenea_api.settings.development && python manage.py runserver 0.0.0.0:28000'
    screen -dmS atenea-beat bash -c 'source venv/bin/activate && export DJANGO_SETTINGS_MODULE=atenea_api.settings.development && celery -A atenea_api beat -l '"$log_level"
    screen -dmS atenea-queue bash -c 'source venv/bin/activate && export DJANGO_SETTINGS_MODULE=atenea_api.settings.development && celery -A atenea_api worker -Q default -n AT1@%h -l '"$log_level"' --concurrency=10'
    screen -dmS atenea-queue-index bash -c 'source venv/bin/activate && export DJANGO_SETTINGS_MODULE=atenea_api.settings.development && celery -A atenea_api worker -Q index-q -n AT2@%h -l '"$log_level"' --concurrency=1'
    screen -dmS atenea-queue-ner bash -c 'source venv/bin/activate && export DJANGO_SETTINGS_MODULE=atenea_api.settings.development && celery -A atenea_api worker -Q ner-q -n AT3@%h -l '"$log_level"' --concurrency=2'
    screen -dmS atenea-queue-embed bash -c 'source venv/bin/activate && export DJANGO_SETTINGS_MODULE=atenea_api.settings.development && celery -A atenea_api worker -Q embed-q -n AT4@%h -l '"$log_level"' --concurrency=1'
    screen -dmS atenea-queue-sentiment bash -c 'source venv/bin/activate && export DJANGO_SETTINGS_MODULE=atenea_api.settings.development && celery -A atenea_api worker -Q sentiment-q -n AT5@%h -l '"$log_level"' --concurrency=1'

    echo "Django server and Celery workers started successfully."
}


# Check the command-line argument and call the corresponding function
if [[ "$1" == "down" ]]; then
    down
elif [[ "$1" == "up" ]]; then
    up "${2:-INFO}"  # Add optional second parameter for log level
else
    echo "Invalid option. Usage: $0 [down|up [LOG_LEVEL]]"
    echo "Default LOG_LEVEL: INFO"
    exit 1
fi