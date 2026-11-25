#!/bin/sh
# =============================================================================
# Prepara la BASE DE DATOS del chatbot dentro del contenedor PostgreSQL existente
# (PROD_PostgreSQL, misma red proxy-net). Idempotente: se puede ejecutar varias veces.
#
#   - Lee la conexión del .env:  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASS
#                                PG_CONTAINER, PG_ADMIN_USER (y opcional PG_ADMIN_PASS)
#   - Crea el usuario y la BD (o actualiza la contraseña si ya existen) y revoca acceso público.
#   - Prueba la conexión con las credenciales de la app y muestra la cadena resultante.
#
# Uso:  ./scripts/desplegar-bd.sh        (desde /Proyectos_Dockers/deployments/QA_CHATBOT)
# Las tablas las crea la app al iniciar (manage.py init-db en docker-entrypoint.sh).
# =============================================================================
set -eu
cd "$(dirname "$0")/.."
ENV_FILE=".env"
[ -f "$ENV_FILE" ] || { echo "ERROR: no existe .env (cp .env.example .env y complétalo)"; exit 1; }
. "$(dirname "$0")/_env.sh"

DB_HOST=$(env_get DB_HOST); DB_PORT=$(env_get DB_PORT 5432); DB_NAME=$(env_get DB_NAME)
DB_USER=$(env_get DB_USER); DB_PASS=$(env_get DB_PASS)
PG_CONTAINER=$(env_get PG_CONTAINER "$DB_HOST"); PG_ADMIN_USER=$(env_get PG_ADMIN_USER postgres)
PG_ADMIN_PASS=$(env_get PG_ADMIN_PASS)

falta=""
for k in DB_HOST DB_NAME DB_USER DB_PASS PG_CONTAINER; do
  eval "v=\${$k}"
  case "$v" in ""|CAMBIAR*) falta="$falta $k";; esac
done
[ -z "$falta" ] || { echo "ERROR: completa en .env:$falta"; exit 1; }
for k in DB_NAME DB_USER; do
  eval "v=\${$k}"
  printf '%s' "$v" | grep -Eq '^[A-Za-z_][A-Za-z0-9_]*$' || { echo "ERROR: $k solo admite letras, números y _ (sin espacios)"; exit 1; }
done

command -v docker >/dev/null || { echo "ERROR: docker no está instalado"; exit 1; }
[ "$(docker inspect -f '{{.State.Running}}' "$PG_CONTAINER" 2>/dev/null || true)" = "true" ] || {
  echo "ERROR: el contenedor '$PG_CONTAINER' no existe o no está corriendo (revisa: docker ps)"; exit 1; }
redes=$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$PG_CONTAINER")
case " $redes " in *" proxy-net "*) ;; *) echo "AVISO: '$PG_CONTAINER' no está en proxy-net (redes: $redes); la app no lo alcanzará.";; esac

echo ">> Creando/actualizando usuario '$DB_USER' y base '$DB_NAME' en '$PG_CONTAINER'..."
PGPW="$DB_PASS" PGADMINPW="$PG_ADMIN_PASS" docker exec -i -e PGPW -e PGADMINPW "$PG_CONTAINER" sh -c '
  [ -n "$PGADMINPW" ] && export PGPASSWORD="$PGADMINPW"
  psql -U "'"$PG_ADMIN_USER"'" -d postgres -v ON_ERROR_STOP=1 -q -v u="'"$DB_USER"'" -v d="'"$DB_NAME"'" -v p="$PGPW"' <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'u', :'p')
  WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'u') \gexec
SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'u', :'p') \gexec
SELECT format('CREATE DATABASE %I OWNER %I ENCODING ''UTF8''', :'d', :'u')
  WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'d') \gexec
SELECT format('ALTER DATABASE %I OWNER TO %I', :'d', :'u') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'d') \gexec
SELECT format('GRANT ALL PRIVILEGES ON DATABASE %I TO %I', :'d', :'u') \gexec
SQL

echo ">> Dando permisos sobre el esquema public de '$DB_NAME'..."
docker exec -i -e PGADMINPW "$PG_CONTAINER" sh -c '
  [ -n "$PGADMINPW" ] && export PGPASSWORD="$PGADMINPW"
  psql -U "'"$PG_ADMIN_USER"'" -d "'"$DB_NAME"'" -v ON_ERROR_STOP=1 -q -v u="'"$DB_USER"'"' <<'SQL'
SELECT format('ALTER SCHEMA public OWNER TO %I', :'u') \gexec
SELECT format('GRANT ALL ON SCHEMA public TO %I', :'u') \gexec
SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO %I', :'u') \gexec
SQL

echo ">> Probando conexión con las credenciales de la app..."
if docker exec -e PGPASSWORD="$DB_PASS" "$PG_CONTAINER" psql -h 127.0.0.1 -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -qc 'CREATE TABLE _prueba_permisos (id int); DROP TABLE _prueba_permisos;' >/dev/null 2>&1; then
  echo "   OK: conexión válida y con permiso para crear tablas."
else
  echo "AVISO: no se pudo verificar la conexión por TCP (¿pg_hba.conf exige otro método o el puerto es distinto a $DB_PORT?)."
fi

echo
echo "Cadena de conexión que usará la app (definida por las variables DB_* del .env):"
echo "  postgresql://$DB_USER:********@$DB_HOST:$DB_PORT/$DB_NAME"
echo
echo "Siguiente paso:  docker compose up -d --build && docker compose logs -f app"
