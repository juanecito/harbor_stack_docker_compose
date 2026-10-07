#!/usr/bin/env bash
# Copyright (c) 2026 Juan Maria Gomez Lopez <juan-maria.gomez-lopez@tutamail.com>
# SPDX-License-Identifier: MIT
# Ejemplo: genera un paquete Debian y lo publica en el repositorio aptly de harbor_docker.
#
# Uso:
#   ./publish-deb.sh                    # genera y publica el paquete de ejemplo
#   ./publish-deb.sh mipaquete_1.0_amd64.deb [otro.deb ...]   # publica .deb existentes
#
# Variables de entorno (las que faltan se toman de ../.env si es legible):
#   APTLY_URL        URL de la API          (por defecto $HARBOR_EXTERNAL_URL/aptly/api)
#   APTLY_API_USER   usuario de la API      (por defecto aptly)
#   APTLY_API_PASSWORD  contraseña de la API (obligatoria)
#   REPO             repositorio aptly      (por defecto local-$DIST)
#   DIST             distribución           (por defecto trixie)
#   COMPONENT        componente             (por defecto main)
#   ARCHS            arquitecturas que se publican la primera vez (por defecto amd64,arm64,all)
#   PKG_NAME / PKG_VERSION   nombre y versión del paquete de ejemplo
#   CA_CERT          certificado de la CA para verificar TLS (p. ej. data/secret/cert/server.crt)
#   INSECURE=1       no verificar el certificado TLS (solo para pruebas)
#
# Requiere: bash, curl y dpkg-deb (o docker para construir el .deb en un contenedor).
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ENV_FILE=${ENV_FILE:-$SCRIPT_DIR/../.env}

# Lee una variable de .env sin ejecutar el fichero
env_get() { [ -r "$ENV_FILE" ] && grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }

: "${APTLY_URL:=$(env_get HARBOR_EXTERNAL_URL)/aptly/api}"
: "${APTLY_API_USER:=$(env_get APTLY_API_USER)}"
: "${APTLY_API_USER:=aptly}"
: "${APTLY_API_PASSWORD:=$(env_get APTLY_API_PASSWORD)}"
: "${DIST:=trixie}"
: "${REPO:=local-$DIST}"
: "${COMPONENT:=main}"
: "${ARCHS:=amd64,arm64,all}"
: "${PKG_NAME:=hola-aptly}"
: "${PKG_VERSION:=1.0.$(date +%Y%m%d%H%M%S)}"

die() { echo "ERROR: $*" >&2; exit 1; }
[ "$APTLY_URL" != "/aptly/api" ] || die "define APTLY_URL o HARBOR_EXTERNAL_URL en .env"
[ -n "$APTLY_API_PASSWORD" ] || die "define APTLY_API_PASSWORD"

CURL=(curl -sS --fail-with-body)
if [ "${INSECURE:-0}" = 1 ]; then CURL+=(-k)
elif [ -n "${CA_CERT:-}" ]; then CURL+=(--cacert "$CA_CERT"); fi
JSON=(-H 'Content-Type: application/json')

# Explica por qué falla la conexión con una URL: DNS, conexión o certificado TLS
conn_error() {
  local rc=0
  "${CURL[@]}" -o /dev/null --max-time 10 "$1" 2>/dev/null || rc=$?
  case $rc in
    6)    die "no se resuelve el nombre de $1: revisa la URL (HARBOR_EXTERNAL_URL en .env) o el DNS/hosts" ;;
    7|28) die "no se puede conectar con $1: ¿servidor parado, puerto o cortafuegos?" ;;
    35|51|58|60|77|83|90|91)
          die "error de certificado TLS con $1: usa CA_CERT=<server.crt> (el nombre debe coincidir con el certificado) o INSECURE=1" ;;
    *)    die "no se puede acceder a $1 (código de curl $rc)" ;;
  esac
}

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# Credenciales en un fichero de configuración de curl (permisos 600): con -u la
# contraseña se vería en la lista de procesos (ps) de cualquier usuario del equipo
curl_quote() { local v=${1//\\/\\\\}; printf '"%s"' "${v//\"/\\\"}"; }
(umask 077; printf 'user = %s\n' "$(curl_quote "$APTLY_API_USER:$APTLY_API_PASSWORD")" > "$WORK/curl.cfg")
CURL+=(-K "$WORK/curl.cfg")

# --- 1. Generar el paquete de ejemplo (si no se pasan .deb) -------------------
build_example_deb() {
  local root="$WORK/$PKG_NAME"
  mkdir -p "$root/DEBIAN" "$root/usr/bin"
  cat > "$root/DEBIAN/control" <<EOF
Package: $PKG_NAME
Version: $PKG_VERSION
Architecture: all
Maintainer: Repository maintainer <debian@example.com>
Section: utils
Priority: optional
Description: Paquete de ejemplo del repositorio aptly
 Instala el comando $PKG_NAME, que imprime un saludo.
EOF
  cat > "$root/usr/bin/$PKG_NAME" <<EOF
#!/bin/sh
echo "Hola desde $PKG_NAME $PKG_VERSION"
EOF
  chmod 755 "$root/usr/bin/$PKG_NAME"

  local deb="${PKG_NAME}_${PKG_VERSION}_all.deb"
  if command -v dpkg-deb >/dev/null; then
    dpkg-deb --root-owner-group --build "$root" "$WORK/$deb" >/dev/null
  else
    docker run --rm -u "$(id -u):$(id -g)" -v "$WORK:/w" -w /w debian:trixie-slim \
      dpkg-deb --root-owner-group --build "$PKG_NAME" "$deb" >/dev/null
  fi
  echo "$WORK/$deb"
}

if [ $# -gt 0 ]; then
  DEBS=("$@")
  PKG_NAME="<paquete>"
  for f in "${DEBS[@]}"; do [ -f "$f" ] || die "no existe $f"; done
else
  DEBS=("$(build_example_deb)")
  echo "Paquete generado: $(basename "${DEBS[0]}")"
fi

# --- 2. Crear el repositorio si no existe -------------------------------------
code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$APTLY_URL/version" 2>/dev/null || true)
case $code in
  200) ;;
  401) die "credenciales incorrectas para la API de aptly" ;;
  000) conn_error "$APTLY_URL/version" ;;
  *)   die "respuesta inesperada de $APTLY_URL/version (HTTP $code)" ;;
esac

if ! "${CURL[@]}" -o /dev/null "$APTLY_URL/repos/$REPO" 2>/dev/null; then
  echo "Creando repositorio $REPO ($DIST/$COMPONENT)..."
  "${CURL[@]}" -X POST "${JSON[@]}" -o /dev/null \
    -d "{\"Name\":\"$REPO\",\"DefaultDistribution\":\"$DIST\",\"DefaultComponent\":\"$COMPONENT\"}" \
    "$APTLY_URL/repos"
fi

# --- 3. Subir los .deb a un directorio temporal e importarlos -----------------
UPLOAD_DIR="upload-$$-$(date +%s)"
for f in "${DEBS[@]}"; do
  echo "Subiendo $(basename "$f")..."
  "${CURL[@]}" -X POST -F "file=@$f" -o /dev/null "$APTLY_URL/files/$UPLOAD_DIR"
done

# El directorio de subida se borra al importarlo
RESULT=$("${CURL[@]}" -X POST "$APTLY_URL/repos/$REPO/file/$UPLOAD_DIR")
if ! echo "$RESULT" | grep -q '"FailedFiles":\[\]'; then
  die "fallo al importar: $RESULT"
fi
echo "$RESULT" | grep -o '"Added":\[[^]]*\]' || true
echo "$RESULT" | grep -o '"Warnings":\[[^]]*\]' | grep -v '\[\]' || true

# --- 4. Publicar (primera vez) o actualizar la publicación --------------------
if "${CURL[@]}" "$APTLY_URL/publish" | grep -q "\"Path\":\"\./$DIST\""; then
  echo "Actualizando publicación $DIST..."
  "${CURL[@]}" -X PUT "${JSON[@]}" -o /dev/null \
    -d '{"Signing":{"Batch":true}}' "$APTLY_URL/publish/:./$DIST"
else
  echo "Publicando $REPO como $DIST ($ARCHS)..."
  ARCHS_JSON=$(printf '"%s",' ${ARCHS//,/ }); ARCHS_JSON="[${ARCHS_JSON%,}]"
  "${CURL[@]}" -X POST "${JSON[@]}" -o /dev/null \
    -d "{\"SourceKind\":\"local\",\"Sources\":[{\"Name\":\"$REPO\"}],\"Distribution\":\"$DIST\",\"Architectures\":$ARCHS_JSON,\"Signing\":{\"Batch\":true}}" \
    "$APTLY_URL/publish/:."
fi

BASE=${APTLY_URL%/aptly/api}
cat <<EOF

Publicado. En los clientes (también vale http://<host>/debian si el puerto HTTP es el 80):
  curl -fsSL $BASE/debian/repo-key.asc | sudo tee /etc/apt/keyrings/aptly-repo.asc >/dev/null
  echo "deb [signed-by=/etc/apt/keyrings/aptly-repo.asc] $BASE/debian $DIST $COMPONENT" | sudo tee /etc/apt/sources.list.d/aptly-repo.list
  sudo apt-get update && sudo apt-get install $PKG_NAME
EOF
