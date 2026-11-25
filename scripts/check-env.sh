#!/bin/sh
# Compara tu archivo .env contra .env.example para verificar si faltan variables.
# No muestra ningún valor secreto, solo los nombres de las claves.
# Uso:  ./scripts/check-env.sh   (o con sudo si .env tiene permisos 600 de root)

set -e

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${BASE_DIR}/.env"
EXAMPLE_FILE="${BASE_DIR}/.env.example"

if [ ! -f "$ENV_FILE" ]; then
  echo "❌ No se encontró el archivo .env en ${BASE_DIR}"
  exit 1
fi

if [ ! -f "$EXAMPLE_FILE" ]; then
  echo "❌ No se encontró .env.example en ${BASE_DIR}"
  exit 1
fi

echo "=================================================="
echo "  Verificación de variables de entorno (.env)"
echo "=================================================="

# Extrae nombres de variables (ignora comentarios y líneas en blanco)
extraer_claves() {
  grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$1" | cut -d= -f1 | sort -u
}

TMP_EX="$(mktemp)"
TMP_ENV="$(mktemp)"
trap 'rm -f "$TMP_EX" "$TMP_ENV"' EXIT

extraer_claves "$EXAMPLE_FILE" > "$TMP_EX"
extraer_claves "$ENV_FILE" > "$TMP_ENV"

FALTANTES="$(comm -23 "$TMP_EX" "$TMP_ENV")"
SOBRANTES="$(comm -13 "$TMP_EX" "$TMP_ENV")"

if [ -z "$FALTANTES" ]; then
  echo "✅ Tu .env tiene todas las variables definidas en .env.example"
else
  echo "⚠️  Variables que están en .env.example pero FALTAN en tu .env:"
  echo "$FALTANTES" | while read -r var; do
    echo "    - $var"
  done
  echo ""
  echo "   (Nota: las variables nuevas agregadas en esta actualización tienen valores por defecto seguros en config.py)"
fi

echo ""
if [ -n "$SOBRANTES" ]; then
  echo "ℹ️  Variables presentes en tu .env que no están en .env.example:"
  echo "$SOBRANTES" | while read -r var; do
    echo "    - $var"
  done
fi

echo "=================================================="
