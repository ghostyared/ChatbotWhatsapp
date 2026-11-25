#!/bin/sh
# Genera valores aleatorios para pegar en tu .env
rnd() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }
echo "SECRET_KEY=$(rnd 48)"
echo "DB_PASS=$(rnd 24)"
echo "WEBHOOK_TOKEN=$(rnd 24)"
echo "ADMIN_PASS=$(rnd 18)"
