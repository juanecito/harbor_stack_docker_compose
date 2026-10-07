#!/usr/bin/env bash
# Copyright (c) 2026 Juan Maria Gomez Lopez <juan-maria.gomez-lopez@tutamail.com>
# SPDX-License-Identifier: MIT
# Ejemplo: genera el mapa HTML del stack con compose_map.py y, opcionalmente, lo abre en el
# navegador o lo sirve por HTTP actualizándolo periódicamente.
#
# Uso:
#   ./compose-map.sh                      # genera compose-map.html en la raíz del proyecto
#   ./compose-map.sh --open               # y lo abre en el navegador
#   ./compose-map.sh --serve 8090         # lo sirve en http://127.0.0.1:8090/ y lo regenera cada 30 s
#   ./compose-map.sh --lang es -o /tmp/mapa.html
#
# Opciones:
#   -o, --output FICHERO   página de salida            (por defecto <proyecto>/compose-map.html;
#                          no se usa con --serve, que genera en un directorio temporal)
#   -l, --lang en|es       idioma de la página         (por defecto en)
#   -c, --context NOMBRE   contexto Docker             (por defecto: default si existe, si no el activo)
#   -r, --refresh SEG      regenerar cada SEG segundos (con --serve, por defecto 30)
#   -s, --serve PUERTO     servir la página por HTTP (Ctrl+C para parar)
#       --bind IP          IP en la que escucha --serve (por defecto 127.0.0.1)
#       --open             abrir la página en el navegador
#   -h, --help             esta ayuda
#
# La página muestra la topología interna y el estado de los contenedores (no incluye
# secretos). Con --serve escucha solo en 127.0.0.1 salvo que se indique --bind.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PROJECT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
MAP_PY="$SCRIPT_DIR/compose_map.py"

OUTPUT="$PROJECT_DIR/compose-map.html"
LANG_PAGE=en
CONTEXT=""
REFRESH=""
SERVE_PORT=""
BIND=127.0.0.1
OPEN=0

die() { echo "ERROR: $*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case $1 in
    -o|--output)  OUTPUT=$2; shift 2 ;;
    -l|--lang)    LANG_PAGE=$2; shift 2 ;;
    -c|--context) CONTEXT=$2; shift 2 ;;
    -r|--refresh) REFRESH=$2; shift 2 ;;
    -s|--serve)   SERVE_PORT=$2; shift 2 ;;
    --bind)       BIND=$2; shift 2 ;;
    --open)       OPEN=1; shift ;;
    -h|--help)    usage ;;
    *)            die "opción desconocida: $1 (usa --help)" ;;
  esac
done

command -v python3 >/dev/null || die "falta python3"
command -v docker >/dev/null || die "falta el cliente docker"
[ -f "$MAP_PY" ] || die "no se encuentra $MAP_PY"
[ -f "$PROJECT_DIR/docker-compose.yml" ] || die "no hay docker-compose.yml en $PROJECT_DIR"

# El stack corre en el motor nativo: si Docker Desktop ha cambiado el contexto activo,
# usar "default" para no consultar un motor vacío
if [ -z "$CONTEXT" ]; then
  if docker context inspect default >/dev/null 2>&1; then CONTEXT=default
  else CONTEXT=$(docker context show); fi
fi

OUTPUT=$(realpath -m "$OUTPUT")
mkdir -p "$(dirname "$OUTPUT")"
ARGS=(-f "$PROJECT_DIR/docker-compose.yml" --context "$CONTEXT" --lang "$LANG_PAGE" -o "$OUTPUT")

open_page() {
  local url=$1
  if command -v xdg-open >/dev/null; then xdg-open "$url" >/dev/null 2>&1 &
  elif command -v open >/dev/null; then open "$url"
  else echo "Abre en el navegador: $url"; fi
}

if [ -z "$SERVE_PORT" ]; then
  if [ -n "$REFRESH" ]; then
    # Regeneración periódica en local (la página se recarga sola en el navegador)
    [ "$OPEN" = 1 ] && { python3 "$MAP_PY" "${ARGS[@]}" >/dev/null; open_page "file://$OUTPUT"; }
    exec python3 "$MAP_PY" "${ARGS[@]}" --refresh "$REFRESH"
  fi
  python3 "$MAP_PY" "${ARGS[@]}"
  [ "$OPEN" = 1 ] && open_page "file://$OUTPUT"
  exit 0
fi

# --- Modo servidor: http.server en segundo plano + regeneración en primer plano ---
# http.server publica TODO el directorio que sirve: la página se genera en un directorio
# temporal privado que solo la contiene a ella (nunca en el proyecto, donde están .env y data/)
: "${REFRESH:=30}"
SERVE_DIR=$(mktemp -d)
chmod 755 "$SERVE_DIR"
ARGS=(-f "$PROJECT_DIR/docker-compose.yml" --context "$CONTEXT" --lang "$LANG_PAGE" -o "$SERVE_DIR/index.html")
python3 "$MAP_PY" "${ARGS[@]}" >/dev/null       # primera versión antes de servir
python3 -m http.server "$SERVE_PORT" --bind "$BIND" --directory "$SERVE_DIR" >/dev/null 2>&1 &
SERVER_PID=$!
GEN_PID=""
cleanup() { kill $SERVER_PID $GEN_PID 2>/dev/null || true; rm -rf "$SERVE_DIR"; }
trap cleanup EXIT
trap 'exit 130' INT TERM
sleep 0.5
kill -0 "$SERVER_PID" 2>/dev/null || die "no se pudo escuchar en $BIND:$SERVE_PORT (¿puerto ocupado?)"

URL="http://$BIND:$SERVE_PORT/"
echo "Sirviendo $URL (se regenera cada ${REFRESH}s; Ctrl+C para parar)"
[ "$OPEN" = 1 ] && open_page "$URL"
# En segundo plano + wait: así bash atiende las señales (kill, timeout) y ejecuta cleanup
python3 "$MAP_PY" "${ARGS[@]}" --refresh "$REFRESH" &
GEN_PID=$!
wait "$GEN_PID"
