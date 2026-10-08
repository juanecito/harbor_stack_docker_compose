#!/usr/bin/env bash
# Copyright (c) 2026 Juan Maria Gomez Lopez <juan-maria.gomez-lopez@tutamail.com>
# SPDX-License-Identifier: MIT
# Ejemplo: sube un SBOM (CycloneDX) a Dependency-Track y muestra las vulnerabilidades.
#
# Uso:
#   ./upload-sbom.sh                                   # sube un SBOM de ejemplo (log4j 2.14.1...)
#   ./upload-sbom.sh localhost/demo/hola-harbor:1.0    # genera el SBOM de una imagen con Trivy
#   ./upload-sbom.sh bom.json                          # sube un SBOM CycloneDX existente
#
# Variables de entorno (las que faltan se toman de ../.env si es legible):
#   DTRACK_URL       URL de la API          (por defecto $DTRACK_API_BASE_URL o http://localhost:8081)
#   DTRACK_UI_URL    URL de la UI, para el enlace final (por defecto http://localhost:8082)
#   DTRACK_API_KEY   clave de API de un equipo con BOM_UPLOAD, PROJECT_CREATION_UPLOAD y
#                    VIEW_VULNERABILITY (recomendado: Administración → Access Management → Teams)
#   DTRACK_USER / DTRACK_PASSWORD   alternativa a la clave: usuario y contraseña (p. ej. admin)
#   PROJECT          proyecto de Dependency-Track; se crea si no existe
#                    (por defecto: el repositorio de la imagen, o "ejemplo-sbom")
#   VERSION          versión del proyecto   (por defecto: el tag de la imagen, o "1.0")
#   FAIL_ON          CRITICAL, HIGH, MEDIUM o LOW: termina con código 2 si hay hallazgos de
#                    esa severidad o mayor (para usarlo como control en CI)
#   TRIVY_IMAGE      imagen de Trivy si no hay `trivy` instalado (por defecto aquasec/trivy:0.74.0)
#
# Las imágenes se leen del Docker local; si no están, se hace `docker pull` (docker debe
# tener hecho el login en Harbor y confiar en su certificado).
# Requiere: bash, curl, python3 y, para imágenes, trivy o docker.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ENV_FILE=${ENV_FILE:-$SCRIPT_DIR/../.env}

# Lee una variable de .env sin ejecutar el fichero
env_get() { [ -r "$ENV_FILE" ] && grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }

: "${DTRACK_URL:=$(env_get DTRACK_API_BASE_URL)}"
: "${DTRACK_URL:=http://localhost:8081}"
: "${DTRACK_UI_URL:=http://localhost:8082}"
: "${TRIVY_IMAGE:=aquasec/trivy:0.74.0}"
FAIL_ON=$(echo "${FAIL_ON:-}" | tr '[:lower:]' '[:upper:]')

die() { echo "ERROR: $*" >&2; exit 1; }
[ -n "${DTRACK_API_KEY:-}" ] || [ -n "${DTRACK_USER:-}" ] \
  || die "define DTRACK_API_KEY (recomendado) o DTRACK_USER y DTRACK_PASSWORD"
case $FAIL_ON in ""|CRITICAL|HIGH|MEDIUM|LOW) ;; *) die "FAIL_ON debe ser CRITICAL, HIGH, MEDIUM o LOW" ;; esac
command -v python3 >/dev/null || die "hace falta python3 para leer las respuestas JSON"

API="${DTRACK_URL%/}/api"
CURL=(curl -sS)

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# Extrae un campo de un JSON leído por stdin: json_get campo [campo...]
json_get() { python3 -c 'import json,sys
d=json.load(sys.stdin)
for k in sys.argv[1:]: d=d[k]
print(d)' "$@"; }

# --- 1. Autenticación ----------------------------------------------------------
# La clave o el token van en un fichero de configuración de curl (permisos 600): en la
# línea de comandos se verían en la lista de procesos (ps) de cualquier usuario del equipo
curl_quote() { local v=${1//\\/\\\\}; printf '"%s"' "${v//\"/\\\"}"; }
code=$("${CURL[@]}" -o "$WORK/resp" -w '%{http_code}' --max-time 10 "$API/version" 2>/dev/null) || code=000
# La UI también responde 200 en /api/version (con su HTML): se comprueba que sea la API
grep -q '"application":"Dependency-Track"' "$WORK/resp" 2>/dev/null || [ "$code" != 200 ] || code=html
case $code in
  200) echo "Dependency-Track $(json_get version < "$WORK/resp") en $DTRACK_URL" ;;
  html) die "$DTRACK_URL no es la API de Dependency-Track: ¿es la URL de la UI? (la API va en el 8081)" ;;
  000) die "no se puede conectar con $DTRACK_URL: ¿servicio parado, URL o túnel SSH del 8081?" ;;
  *)   die "$API/version respondió HTTP $code: ¿es la URL de la API (no la de la UI)?" ;;
esac

if [ -n "${DTRACK_API_KEY:-}" ]; then
  (umask 077; printf 'header = %s\n' "$(curl_quote "X-Api-Key: $DTRACK_API_KEY")" > "$WORK/curl.cfg")
else
  [ -n "${DTRACK_PASSWORD:-}" ] || die "define DTRACK_PASSWORD"
  echo "Aviso: usando usuario y contraseña; en CI usa una clave de API (DTRACK_API_KEY)." >&2
  (umask 077; printf '%s' "$DTRACK_PASSWORD" > "$WORK/pass")
  code=$("${CURL[@]}" -o "$WORK/token" -w '%{http_code}' -X POST "$API/v1/user/login" \
    --data-urlencode "username=$DTRACK_USER" --data-urlencode "password@$WORK/pass")
  rm -f "$WORK/pass"
  case $code in
    200) ;;
    401) [ "$(cat "$WORK/token")" = FORCE_PASSWORD_CHANGE ] \
           && die "hay que cambiar la contraseña inicial: entra antes en la UI" \
           || die "usuario o contraseña incorrectos" ;;
    *)   die "login falló (HTTP $code): $(cat "$WORK/token")" ;;
  esac
  (umask 077; printf 'header = %s\n' "$(curl_quote "Authorization: Bearer $(cat "$WORK/token")")" > "$WORK/curl.cfg")
fi
CURL+=(-K "$WORK/curl.cfg")

# --- 2. Obtener el SBOM ----------------------------------------------------------
BOM=$WORK/bom.json
if [ $# -eq 0 ]; then
  # SBOM mínimo con componentes que tienen vulnerabilidades conocidas (CVE-2021-44228...)
  : "${PROJECT:=ejemplo-sbom}"
  cat > "$BOM" <<'EOF'
{
  "bomFormat": "CycloneDX",
  "specVersion": "1.5",
  "version": 1,
  "components": [
    {"type": "library", "group": "org.apache.logging.log4j", "name": "log4j-core", "version": "2.14.1",
     "purl": "pkg:maven/org.apache.logging.log4j/log4j-core@2.14.1",
     "cpe": "cpe:2.3:a:apache:log4j:2.14.1:*:*:*:*:*:*:*"},
    {"type": "library", "group": "com.fasterxml.jackson.core", "name": "jackson-databind", "version": "2.9.8",
     "purl": "pkg:maven/com.fasterxml.jackson.core/jackson-databind@2.9.8",
     "cpe": "cpe:2.3:a:fasterxml:jackson-databind:2.9.8:*:*:*:*:*:*:*"},
    {"type": "library", "name": "openssl", "version": "1.1.1k",
     "purl": "pkg:generic/openssl@1.1.1k",
     "cpe": "cpe:2.3:a:openssl:openssl:1.1.1k:*:*:*:*:*:*:*"}
  ]
}
EOF
elif [ -f "$1" ]; then
  : "${PROJECT:=$(basename "$1" .json)}"
  cp "$1" "$BOM"
  grep -q '"bomFormat" *: *"CycloneDX"' "$BOM" || die "$1 no parece un SBOM CycloneDX en JSON"
else
  IMG=$1
  # Proyecto = repositorio sin registry ni tag (localhost/demo/app:1.0 -> demo/app); versión = tag
  repo=${IMG%@*}; tag=latest
  [[ ${repo##*/} == *:* ]] && { tag=${repo##*:}; repo=${repo%:*}; }
  [[ $repo == */* && ${repo%%/*} =~ [.:]|^localhost$ ]] && repo=${repo#*/}
  : "${PROJECT:=$repo}"; : "${VERSION:=$tag}"

  command -v docker >/dev/null || command -v trivy >/dev/null || die "hace falta trivy o docker"
  if command -v docker >/dev/null && ! docker image inspect "$IMG" >/dev/null 2>&1; then
    echo "Descargando $IMG..."
    docker pull -q "$IMG" >/dev/null || die "no se pudo descargar $IMG (¿docker login? ¿certificado?)"
  fi
  echo "Generando el SBOM de $IMG con Trivy..."
  # Solo inventario de paquetes (sin escaneo de vulnerabilidades): lo analiza Dependency-Track
  if command -v trivy >/dev/null; then
    trivy image --quiet --format cyclonedx --output "$BOM" "$IMG"
  else
    docker run --rm -v /var/run/docker.sock:/var/run/docker.sock:ro \
      -v trivy-cache:/root/.cache/trivy "$TRIVY_IMAGE" \
      image --quiet --format cyclonedx "$IMG" > "$BOM"
  fi
fi
: "${VERSION:=1.0}"
ncomp=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("components",[])))' "$BOM")
echo "SBOM: $ncomp componentes → proyecto $PROJECT $VERSION"

# --- 3. Subir el SBOM y esperar a que se procese ------------------------------
code=$("${CURL[@]}" -o "$WORK/resp" -w '%{http_code}' -X POST "$API/v1/bom" \
  -F autoCreate=true -F "projectName=$PROJECT" -F "projectVersion=$VERSION" -F "bom=@$BOM")
case $code in
  200) ;;
  401) die "credenciales incorrectas" ;;
  403) die "sin permiso: la clave necesita BOM_UPLOAD y PROJECT_CREATION_UPLOAD" ;;
  *)   die "la subida falló (HTTP $code): $(cat "$WORK/resp")" ;;
esac
TOKEN=$(json_get token < "$WORK/resp")

echo -n "Procesando"
for i in $(seq 60); do
  "${CURL[@]}" "$API/v1/event/token/$TOKEN" | json_get processing | grep -q True || break
  echo -n "."; sleep 2
done
echo
[ "$i" -lt 60 ] || echo "Aviso: el análisis sigue en curso; el resultado puede estar incompleto." >&2

UUID=$("${CURL[@]}" -G "$API/v1/project/lookup" --data-urlencode "name=$PROJECT" \
  --data-urlencode "version=$VERSION" | json_get uuid) || die "el proyecto no aparece tras la subida"

# --- 4. Resumen de vulnerabilidades -------------------------------------------
# El token de la subida cubre también el análisis: los hallazgos ya están calculados
code=$("${CURL[@]}" -o "$WORK/findings.json" -w '%{http_code}' "$API/v1/finding/project/$UUID")
[ "$code" = 200 ] || die "no se pudieron leer los hallazgos (HTTP $code): la clave necesita VIEW_VULNERABILITY"

rc=0
python3 - "$WORK/findings.json" "$FAIL_ON" <<'EOF' || rc=$?
import json, sys
findings = json.load(open(sys.argv[1]))
fail_on = sys.argv[2]
order = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO", "UNASSIGNED"]
count = {s: 0 for s in order}
rows = []
for f in findings:
    v, c = f["vulnerability"], f["component"]
    sev = v.get("severity", "UNASSIGNED")
    count[sev] = count.get(sev, 0) + 1
    rows.append((order.index(sev) if sev in order else 99, sev, v["vulnId"],
                 f'{c["name"]} {c.get("version", "")}'.strip()))
summary = ", ".join(f"{s.lower()} {n}" for s, n in count.items() if n)
print(f"Vulnerabilidades: {len(findings)}" + (f" ({summary})" if summary else ""))
for _, sev, vid, comp in sorted(rows)[:20]:
    print(f"  {sev:<10} {vid:<22} {comp}")
if len(rows) > 20:
    print(f"  ... y {len(rows) - 20} más (ver la UI)")
if not findings:
    print("  (en una instalación nueva, espera a que termine la descarga de NVD y repite)")
if fail_on and any(count.get(s) for s in order[:order.index(fail_on) + 1]):
    print(f"Hay hallazgos de severidad {fail_on} o mayor", file=sys.stderr)
    sys.exit(2)
EOF

echo "Proyecto: ${DTRACK_UI_URL%/}/projects/$UUID"
exit $rc
