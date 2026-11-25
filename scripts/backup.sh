#!/usr/bin/env bash
# Respaldo de la BD (en el contenedor PostgreSQL externo) y de los comprobantes -> backups/
# Ejecutar desde la carpeta del proyecto.
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=".env"
[ -f "$ENV_FILE" ] || { echo "ERROR: no existe .env" >&2; exit 1; }
. "$(dirname "$0")/_env.sh"

DB_NAME=$(env_get DB_NAME)
DB_USER=$(env_get DB_USER)
DB_PASS=$(env_get DB_PASS)
DB_PORT=$(env_get DB_PORT 5432)
PG_CONTAINER=$(env_get PG_CONTAINER "$(env_get DB_HOST)")
ALMACEN=$(env_get COMPROBANTE_ALMACEN bd)

for var in DB_NAME DB_USER DB_PASS PG_CONTAINER; do
  if [ -z "${!var}" ]; then
    echo "ERROR: falta $var en .env" >&2
    exit 1
  fi
done

mkdir -p backups
STAMP="$(date +%Y%m%d_%H%M)"
DB_FILE="backups/db_${STAMP}.sql.gz"
COMP_FILE="backups/comprobantes_${STAMP}.tar.gz"
TMP_DB="${DB_FILE}.tmp"
TMP_COMP="${COMP_FILE}.tmp"
cleanup() { rm -f "$TMP_DB" "$TMP_COMP"; }
trap cleanup EXIT

docker exec -e PGPASSWORD="$DB_PASS" "$PG_CONTAINER" \
  pg_dump -h 127.0.0.1 -p "$DB_PORT" -U "$DB_USER" "$DB_NAME" | gzip > "$TMP_DB"
mv "$TMP_DB" "$DB_FILE"
chmod 600 "$DB_FILE"

if docker compose exec -T app sh -c '[ -d /app/data/comprobantes ]' 2>/dev/null; then
  docker compose exec -T app tar czf - -C /app/data comprobantes > "$TMP_COMP"
  mv "$TMP_COMP" "$COMP_FILE"
  chmod 600 "$COMP_FILE"
  echo "Respaldos creados: $DB_FILE y $COMP_FILE"
else
  echo "Respaldo de BD creado: $DB_FILE (comprobantes en almacén '$ALMACEN', sin carpeta en disco)"
fi

for tipo in db comprobantes; do
  ls -1t backups/${tipo}_*.gz 2>/dev/null | tail -n +15 | xargs -r rm --
done
