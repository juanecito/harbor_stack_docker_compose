#!/usr/bin/env bash
# Copyright (c) 2026 Juan Maria Gomez Lopez <juan-maria.gomez-lopez@tutamail.com>
# SPDX-License-Identifier: MIT
# Ejemplo: construye una imagen Docker y la sube al registry Harbor de harbor_docker.
#
# Uso:
#   ./push-image.sh                 # genera y sube la imagen de ejemplo
#   ./push-image.sh ./mi-proyecto   # construye el Dockerfile de ese directorio
#
# Variables de entorno (las que faltan se toman de ../.env si es legible):
#   HARBOR_URL       URL de Harbor                    (por defecto $HARBOR_EXTERNAL_URL)
#   HARBOR_USER      usuario o cuenta robot (recomendado: robot con permiso de push)
#   HARBOR_PASSWORD  contraseña o secreto del robot
#                    (si no se definen, usa admin / HARBOR_ADMIN_PASSWORD de .env)
#   PROJECT          proyecto de Harbor; se crea si no existe   (por defecto demo)
#   IMAGE            repositorio dentro del proyecto            (por defecto hola-harbor)
#   TAG              tag de la imagen                           (por defecto fecha y hora)
#   PLATFORMS        p. ej. linux/amd64,linux/arm64: construye y sube multi-arquitectura con buildx
#   SCAN=1           lanza el escaneo de vulnerabilidades (Trivy) y espera el resultado
#   CA_CERT          certificado de la CA para verificar TLS en las llamadas a la API
#   INSECURE=1       no verificar el certificado TLS en la API (solo para pruebas)
#
# Docker debe confiar en el certificado de Harbor: con uno autofirmado, cópialo a
# /etc/docker/certs.d/<host[:puerto]>/ca.crt (ver README, "Configurar los clientes Docker").
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ENV_FILE=${ENV_FILE:-$SCRIPT_DIR/../.env}

# Lee una variable de .env sin ejecutar el fichero
env_get() { [ -r "$ENV_FILE" ] && grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }

: "${HARBOR_URL:=$(env_get HARBOR_EXTERNAL_URL)}"
if [ -z "${HARBOR_USER:-}" ]; then
  HARBOR_USER=admin
  : "${HARBOR_PASSWORD:=$(env_get HARBOR_ADMIN_PASSWORD)}"
  echo "Aviso: usando 'admin'; en CI usa una cuenta robot (HARBOR_USER/HARBOR_PASSWORD)." >&2
fi
: "${HARBOR_PASSWORD:=}"
: "${PROJECT:=demo}"
: "${IMAGE:=hola-harbor}"
: "${TAG:=$(date +%Y%m%d-%H%M%S)}"

die() { echo "ERROR: $*" >&2; exit 1; }
[ -n "$HARBOR_URL" ] || die "define HARBOR_URL o HARBOR_EXTERNAL_URL en .env"
[ -n "$HARBOR_PASSWORD" ] || die "define HARBOR_PASSWORD"

REG=${HARBOR_URL#https://}; REG=${REG%/}            # host[:puerto] para docker
API="${HARBOR_URL%/}/api/v2.0"
REF="$REG/$PROJECT/$IMAGE"

CURL=(curl -sS)
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
(umask 077; printf 'user = %s\n' "$(curl_quote "$HARBOR_USER:$HARBOR_PASSWORD")" > "$WORK/curl.cfg")
CURL+=(-K "$WORK/curl.cfg")

# Configuración de Docker temporal: el login no se guarda en ~/.docker ni en el
# almacén de credenciales del sistema y desaparece al terminar el script
mkdir -p "$WORK/docker"
[ -d "${DOCKER_CONFIG:-$HOME/.docker}/cli-plugins" ] && \
  ln -s "${DOCKER_CONFIG:-$HOME/.docker}/cli-plugins" "$WORK/docker/cli-plugins"
[ -d "${DOCKER_CONFIG:-$HOME/.docker}/buildx" ] && \
  ln -s "${DOCKER_CONFIG:-$HOME/.docker}/buildx" "$WORK/docker/buildx"
export DOCKER_CONFIG="$WORK/docker"

# --- 1. Contexto de construcción: el indicado o una imagen de ejemplo ---------
if [ $# -gt 0 ]; then
  CONTEXT=$1
  [ -f "$CONTEXT/Dockerfile" ] || die "no hay Dockerfile en $CONTEXT"
else
  CONTEXT=$WORK
  cat > "$WORK/Dockerfile" <<EOF
FROM busybox:stable
LABEL org.opencontainers.image.title="$IMAGE" \\
      org.opencontainers.image.version="$TAG"
RUN adduser -D -u 10001 app
USER app
CMD ["echo", "Hola desde $IMAGE:$TAG"]
EOF
fi

# --- 2. Crear el proyecto si no existe ----------------------------------------
code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' -I "$API/projects?project_name=$PROJECT" 2>/dev/null || true)
case $code in
  200) ;;
  404)
    echo "Creando proyecto privado $PROJECT..."
    code=$("${CURL[@]}" -o "$WORK/resp" -w '%{http_code}' -X POST "${JSON[@]}" \
      -d "{\"project_name\":\"$PROJECT\",\"metadata\":{\"public\":\"false\"}}" "$API/projects")
    [ "$code" = 201 ] || die "no se pudo crear el proyecto (HTTP $code): $(cat "$WORK/resp")" ;;
  401) die "credenciales incorrectas para la API de Harbor" ;;
  000) conn_error "$API/ping" ;;
  *)   echo "Aviso: no se pudo comprobar el proyecto (HTTP $code); se intenta el push igualmente." >&2 ;;
esac

# --- 3. Login, construcción y push ---------------------------------------------
out=$(echo "$HARBOR_PASSWORD" | docker login "$REG" -u "$HARBOR_USER" --password-stdin 2>&1) \
  || die "docker login falló: $out"

if [ -n "${PLATFORMS:-}" ]; then
  # Multi-arquitectura: el driver "docker" no lo admite, así que se usa un builder
  # docker-container temporal que construye y sube directamente un índice OCI.
  # Para emular otras arquitecturas el host necesita QEMU/binfmt
  # (docker run --privileged --rm tonistiigi/binfmt --install all).
  BUILDER="push-image-$$"
  # buildx no permite omitir la verificación TLS (el token lo pide el propio cliente)
  [ "${INSECURE:-0}" != 1 ] || die "PLATFORMS no admite INSECURE=1; usa CA_CERT o un certificado de confianza"
  # http = false: sin ello buildkit cae a HTTP plano con registries en localhost
  printf '[registry."%s"]\n  http = false\n' "$REG" > "$WORK/buildkitd.toml"
  BUILDX_ENV=()
  if [ -n "${CA_CERT:-}" ]; then
    # La CA la necesitan buildkitd (push de capas) y el cliente buildx (token de Harbor)
    printf '  ca = ["%s"]\n' "$(realpath "$CA_CERT")" >> "$WORK/buildkitd.toml"
    for b in "${SSL_CERT_FILE:-}" /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt; do
      [ -n "$b" ] && [ -r "$b" ] && { cat "$b"; break; }
    done > "$WORK/ca-bundle.crt"
    cat "$CA_CERT" >> "$WORK/ca-bundle.crt"
    BUILDX_ENV=(env SSL_CERT_FILE="$WORK/ca-bundle.crt")
  fi
  docker buildx create --name "$BUILDER" --driver docker-container \
    --driver-opt network=host --buildkitd-config "$WORK/buildkitd.toml" >/dev/null
  trap 'docker buildx rm -f "$BUILDER" >/dev/null 2>&1; rm -rf "$WORK"' EXIT
  "${BUILDX_ENV[@]}" docker buildx build --builder "$BUILDER" --platform "$PLATFORMS" \
    -t "$REF:$TAG" -t "$REF:latest" --push "$CONTEXT"
else
  docker build -t "$REF:$TAG" -t "$REF:latest" "$CONTEXT"
  docker push -q "$REF:$TAG"
  docker push -q "$REF:latest"
fi

# --- 4. Comprobar el artefacto en Harbor (y escanearlo si se pide) -------------
REPO_ENC=${IMAGE//\//%252F}                        # repos anidados: "a/b" -> "a%252Fb"
ART="$API/projects/$PROJECT/repositories/$REPO_ENC/artifacts/$TAG"
DIGEST=$("${CURL[@]}" "$ART" | grep -o '"digest":"sha256:[0-9a-f]*"' | head -1 | cut -d'"' -f4)
[ -n "$DIGEST" ] || die "el artefacto $PROJECT/$IMAGE:$TAG no aparece en Harbor"
echo "Subida: $REF:$TAG (y :latest)"
echo "Digest: $DIGEST"

if [ "${SCAN:-0}" = 1 ]; then
  echo "Escaneando con Trivy..."
  "${CURL[@]}" -o /dev/null -X POST "$ART/scan"
  for _ in $(seq 60); do
    sleep 5
    resp=$("${CURL[@]}" -H 'X-Accept-Vulnerabilities: application/vnd.security.vulnerability.report; version=1.1' \
      "$ART?with_scan_overview=true")
    status=$(echo "$resp" | grep -o '"scan_status":"[A-Za-z]*"' | head -1 | cut -d'"' -f4)
    case $status in
      Success)
        total=$(echo "$resp" | grep -o '"total":[0-9]*' | head -1 | cut -d: -f2)
        sev=$(echo "$resp" | grep -o '"summary":{[^{}]*}' | head -1 | sed 's/"summary"://; s/[{}"]//g')
        echo "Escaneo completado: ${total:-0} vulnerabilidades ${sev:+($sev)}"
        break ;;
      Error|Stopped) die "el escaneo terminó con estado $status" ;;
    esac
  done
  [ "$status" = Success ] || die "el escaneo no terminó a tiempo (estado: ${status:-desconocido})"
fi

echo
echo "Para usarla:  docker pull $REF:$TAG && docker run --rm $REF:$TAG"
