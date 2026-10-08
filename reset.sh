#!/usr/bin/env bash
# Copyright (c) 2026 Juan Maria Gomez Lopez <juan-maria.gomez-lopez@tutamail.com>
# SPDX-License-Identifier: MIT
# Borra todo lo que crea docker-compose.yml: contenedores, red y el directorio de datos
# (registry, BD, aptly y su clave GPG, Dependency-Track, secretos y certificado).
# Opcionalmente hace antes una copia, borra también las imágenes y regenera .env.
#
# Uso:
#   ./reset.sh [opciones]
#
# Opciones:
#   --backup FICHERO  guarda antes data/ y .env en FICHERO (.tgz) para poder restaurar
#   --images          borra también las imágenes del compose (se descargan o construyen al arrancar)
#   --env             secretos nuevos en .env (conserva hostname, puertos y demás ajustes)
#   --up              arranca de nuevo el stack al terminar
#   --yes             no pide confirmación (para scripts)
#
# Restaurar una copia:  tar --numeric-owner -xzpf FICHERO  (como root, en este directorio)
set -euo pipefail

cd "$(dirname "$0")"
PROJECT_DIR=$(pwd)

die() { echo "ERROR: $*" >&2; exit 1; }
usage() { sed -n '/^# Uso:/,/^# Restaurar/p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

BACKUP= IMAGES=0 NEW_ENV=0 UP=0 YES=0
while [ $# -gt 0 ]; do
  case $1 in
    --backup) [ -n "${2:-}" ] || die "--backup necesita un fichero"; BACKUP=$2; shift ;;
    --backup=*) BACKUP=${1#*=} ;;
    --images) IMAGES=1 ;;
    --env)    NEW_ENV=1 ;;
    --up)     UP=1 ;;
    --yes|-y) YES=1 ;;
    -h|--help) usage ;;
    *) echo "Opción desconocida: $1" >&2; usage 1 ;;
  esac
  shift
done

# Lee una variable de .env sin ejecutar el fichero
env_get() { [ -r .env ] && grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }

[ -f docker-compose.yml ] || die "no se encuentra docker-compose.yml en $PROJECT_DIR"
[ -f .env ] || [ "$NEW_ENV" = 1 ] || die "no hay .env (usa --env para crearlo)"

HARBOR_VERSION=$(env_get HARBOR_VERSION); : "${HARBOR_VERSION:=v2.15.2}"
DATA_DIR=$(env_get HARBOR_DATA_DIR); : "${DATA_DIR:=./data}"
DATA_DIR=$(realpath -m "$DATA_DIR")
# Imagen auxiliar para borrar y copiar: ya está descargada, trae tar y se ejecuta como
# root, así que puede leer y borrar los ficheros de los UID 10000/999/70/1000 sin sudo
HELPER=goharbor/prepare:$HARBOR_VERSION

# Salvaguarda: no borrar nunca un directorio que no sea claramente el de datos
case $DATA_DIR in
  /|/home|/root|/var|/srv|/opt|/tmp|"$HOME"|"$PROJECT_DIR"|"$(dirname "$PROJECT_DIR")")
    die "HARBOR_DATA_DIR=$DATA_DIR es demasiado general; no se borra" ;;
esac

if [ -n "$BACKUP" ]; then
  BACKUP=$(realpath -m "$BACKUP")
  [ ! -e "$BACKUP" ] || die "$BACKUP ya existe"
  [ -d "$(dirname "$BACKUP")" ] || die "no existe el directorio $(dirname "$BACKUP")"
  case $BACKUP in "$DATA_DIR"/*) die "la copia no puede ir dentro de $DATA_DIR" ;; esac
fi

# --- Resumen y confirmación ----------------------------------------------------
# Nombre del proyecto: COMPOSE_PROJECT_NAME o el "name:" del compose
PROJECT=${COMPOSE_PROJECT_NAME:-$(sed -n 's/^name: *//p' docker-compose.yml | head -1)}
: "${PROJECT:=$(basename "$PROJECT_DIR")}"
mapfile -t CONTAINERS < <(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}}')
# Imágenes: las del compose (si se puede resolver, hace falta .env) y las de los contenedores
IMAGE_LIST=()
if [ "$IMAGES" = 1 ]; then
  mapfile -t IMAGE_LIST < <({ docker compose config --images 2>/dev/null || true
    docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Image}}'; } | sort -u)
fi

echo "Proyecto Compose: $PROJECT"
echo "Se va a borrar:"
echo "  - contenedores: ${CONTAINERS[*]:-(ninguno)}"
echo "  - redes del compose"
if [ -d "$DATA_DIR" ]; then
  size=$(docker run --rm --network none --entrypoint du -v "$DATA_DIR":/data:ro "$HELPER" -sh /data 2>/dev/null | cut -f1) || true
  echo "  - $DATA_DIR (${size:-?}; imágenes, BD, aptly, Dependency-Track, secretos)"
else
  echo "  - $DATA_DIR (no existe)"
fi
[ "$IMAGES" = 0 ] || echo "  - imágenes: ${IMAGE_LIST[*]:-(ninguna)}"
[ "$NEW_ENV" = 0 ] || echo "  - secretos de .env (se generan nuevos; el resto de ajustes se conserva)"
[ -z "$BACKUP" ] || echo "Copia previa en: $BACKUP"
echo

if [ "$YES" != 1 ]; then
  [ -t 0 ] || die "sin terminal para confirmar: usa --yes"
  read -r -p "Esto NO se puede deshacer. Escribe BORRAR para continuar: " answer
  [ "$answer" = BORRAR ] || { echo "Cancelado."; exit 1; }
fi

# --- 1. Contenedores y red -------------------------------------------------------
echo "Parando y eliminando contenedores..."
if [ -f .env ]; then
  docker compose down --remove-orphans --volumes
else
  # Sin .env, compose no valida el fichero (variables obligatorias): se borra por etiquetas
  label=com.docker.compose.project=$PROJECT
  ids=$(docker ps -aq --filter "label=$label"); [ -z "$ids" ] || docker rm -f $ids >/dev/null
  ids=$(docker network ls -q --filter "label=$label"); [ -z "$ids" ] || docker network rm $ids >/dev/null
fi

# --- 2. Copia de seguridad -----------------------------------------------------
if [ -n "$BACKUP" ]; then
  echo "Copiando datos en $BACKUP..."
  args=(-v "$(dirname "$BACKUP")":/backup)
  items=()
  [ -d "$DATA_DIR" ] && { args+=(-v "$DATA_DIR":/src/data:ro); items+=(data); }
  [ -f .env ] && { args+=(-v "$PROJECT_DIR/.env":/src/.env:ro); items+=(.env); }
  [ ${#items[@]} -gt 0 ] || die "no hay nada que copiar"
  # Sin los sockets de gpg-agent (data/aptly/gpg/S.*): tar no los admite y se recrean solos
  docker run --rm --network none --entrypoint sh "${args[@]}" "$HELPER" -c \
    'f=/backup/$1 owner=$2; shift 2; cd /src &&
     find "$@" ! -type s | tar --numeric-owner --no-recursion -czpf "$f" -T - &&
     chown "$owner" "$f" && chmod 600 "$f" || { rm -f "$f"; exit 1; }' \
    sh "$(basename "$BACKUP")" "$(id -u):$(id -g)" "${items[@]}" \
    || die "falló la copia; no se ha borrado nada (docker compose up -d para volver a arrancar)"
  echo "Copia hecha ($(du -h "$BACKUP" | cut -f1))."
fi

# --- 3. Datos ----------------------------------------------------------------------
if [ -d "$DATA_DIR" ]; then
  echo "Borrando $DATA_DIR..."
  # Se monta el directorio padre para poder borrar también data/ (es de root)
  docker run --rm --network none --entrypoint rm -v "$(dirname "$DATA_DIR")":/parent \
    "$HELPER" -rf "/parent/$(basename "$DATA_DIR")"
  [ ! -e "$DATA_DIR" ] || die "no se pudo borrar $DATA_DIR"
fi

# --- 4. Imágenes ---------------------------------------------------------------------
if [ "$IMAGES" = 1 ]; then
  echo "Borrando imágenes..."
  for img in "${IMAGE_LIST[@]}"; do
    if docker image inspect "$img" >/dev/null 2>&1; then
      docker rmi "$img" >/dev/null 2>&1 && echo "  $img" \
        || echo "  Aviso: no se pudo borrar $img (¿la usa otro contenedor?)" >&2
    fi
  done
fi

# --- 5. .env y arranque -------------------------------------------------------------
if [ "$NEW_ENV" = 1 ]; then
  if [ -f .env ]; then
    # generate-env.sh crea un .env desde .env.example; de él solo se toman los secretos
    # (las variables vacías en .env.example) y las variables nuevas que no estuvieran
    old=$(mktemp -p . .env.old.XXXXXX)
    mv .env "$old"
    ./generate-env.sh >/dev/null || { mv "$old" .env; die "falló generate-env.sh; .env sin cambios"; }
    awk -F= '
      FILENAME == ARGV[1] { if ($0 ~ /^[A-Za-z_]+=$/) secret[$1] = 1; next }
      FILENAME == ARGV[2] { if ($0 ~ /^[A-Za-z_]+=/) { new[$1] = $0; order[++n] = $1 }; next }
      /^[A-Za-z_]+=/ { seen[$1] = 1; if ($1 in secret) { print new[$1]; next } }
      { print }
      END { for (i = 1; i <= n; i++) if (!(order[i] in seen)) print new[order[i]] }
    ' .env.example .env "$old" > .env.merged
    chmod 600 .env.merged && mv .env.merged .env && rm -f "$old"
  else
    ./generate-env.sh >/dev/null
  fi
  echo "Secretos nuevos en .env. Contraseña de 'admin': $(env_get HARBOR_ADMIN_PASSWORD)"
fi

echo
echo "Hecho: el stack está borrado."
if [ "$UP" = 1 ]; then
  docker compose up -d --wait
  docker compose ps
else
  echo "Para arrancar de cero:  docker compose up -d --wait"
fi
echo "Recuerda: certificado TLS y clave GPG de aptly nuevos (hay que repartirlos a los clientes);"
echo "Dependency-Track vuelve a admin/admin."
