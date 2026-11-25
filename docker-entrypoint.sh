#!/bin/sh
set -e

# 1) Valida .env, espera a la BD y crea tablas/datos iniciales (idempotente, se ejecuta UNA vez)
python manage.py init-db

# 2) Arranca la aplicación
exec gunicorn run:app \
  --bind "0.0.0.0:${APP_PORT:-5005}" \
  --worker-class gthread \
  --workers "${GUNICORN_WORKERS:-2}" \
  --threads "${GUNICORN_THREADS:-4}" \
  --timeout "${GUNICORN_TIMEOUT:-60}" \
  --graceful-timeout "${GUNICORN_GRACEFUL_TIMEOUT:-30}" \
  --access-logfile - \
  --error-logfile - \
  --log-level "${LOG_LEVEL:-info}"
