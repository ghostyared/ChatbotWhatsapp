# Utilidades compartidas por los scripts (se cargan con:  . "$(dirname "$0")/_env.sh").
# Lee valores del .env SIN ejecutarlo (el .env puede tener espacios y comentarios).
env_get() {  # env_get CLAVE [defecto]
  v=$(grep -E "^$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2- | sed -E 's/[[:space:]]+#.*$//; s/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/')
  [ -n "$v" ] && printf '%s' "$v" || printf '%s' "${2:-}"
}
