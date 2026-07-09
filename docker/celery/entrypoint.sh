#!/bin/sh

if [ "$DATABASE" = "postgres" ]
then
    while ! nc -z $DB_HOST $DB_PORT; do
        echo "Waiting for postgres..."
        sleep 0.1
    done
fi

exec "$@"
