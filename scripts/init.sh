#!/bin/sh
# Copyright (c) 2026 Juan Maria Gomez Lopez <juan-maria.gomez-lopez@tutamail.com>
# SPDX-License-Identifier: MIT
# Prepara el directorio de datos de Harbor. Idempotente: solo genera lo que falta.
set -eu

D=/data
HARBOR_UID=10000
PG_UID=999
REDIS_UID=999

mkdir -p "$D/registry" "$D/database" "$D/redis" "$D/job_logs" "$D/ca_download" \
         "$D/trivy-adapter/trivy" "$D/trivy-adapter/reports" \
         "$D/secret/core" "$D/secret/registry" "$D/secret/keys" "$D/secret/cert" \
         "$D/aptly/public" "$D/secret/aptly"

rand() { tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1"; }

# Par de claves con el que core firma los tokens que valida el registry
if [ ! -s "$D/secret/core/private_key.pem" ] || [ ! -s "$D/secret/registry/root.crt" ]; then
  echo "Generando par de claves del token service..."
  openssl genrsa -traditional -out "$D/secret/core/private_key.pem" 4096 2>/dev/null
  openssl req -new -x509 -key "$D/secret/core/private_key.pem" \
    -out "$D/secret/registry/root.crt" -days 3650 -subj "/" 2>/dev/null
fi

# Clave (16 caracteres) para cifrar credenciales guardadas en la BD
if [ ! -s "$D/secret/keys/secretkey" ]; then
  echo "Generando secretkey..."
  rand 16 > "$D/secret/keys/secretkey"
fi

# Credencial con la que core/jobservice hablan con el registry
# (-i: contraseña por stdin; con -b se vería en la lista de procesos del host)
printf '%s' "$REGISTRY_PASSWORD" | htpasswd -icB "$D/secret/registry/passwd" harbor_registry_user 2>/dev/null

# Usuario con el que nginx protege la API de aptly (subida y publicación de paquetes)
printf '%s' "$APTLY_API_PASSWORD" | htpasswd -icB "$D/secret/aptly/api.htpasswd" "$APTLY_API_USER" 2>/dev/null

# Certificado TLS: si no hay uno propio en secret/cert, se genera uno autofirmado
if [ ! -s "$D/secret/cert/server.crt" ] || [ ! -s "$D/secret/cert/server.key" ]; then
  echo "Generando certificado TLS autofirmado para $HARBOR_HOSTNAME..."
  if echo "$HARBOR_HOSTNAME" | grep -Eq '^[0-9.]+$'; then
    SAN="IP:$HARBOR_HOSTNAME"
  else
    SAN="DNS:$HARBOR_HOSTNAME"
  fi
  openssl req -x509 -newkey rsa:4096 -nodes -sha256 -days 365 \
    -keyout "$D/secret/cert/server.key" -out "$D/secret/cert/server.crt" \
    -subj "/CN=$HARBOR_HOSTNAME" -addext "subjectAltName=$SAN" 2>/dev/null
fi

chown -R $HARBOR_UID:$HARBOR_UID "$D/registry" "$D/job_logs" "$D/ca_download" \
  "$D/trivy-adapter" "$D/secret/core" "$D/secret/registry" "$D/secret/keys" "$D/secret/cert" \
  "$D/aptly" "$D/secret/aptly"
chown -R $PG_UID:$PG_UID "$D/database"
chown -R $REDIS_UID:$REDIS_UID "$D/redis"
chmod 600 "$D/secret/core/private_key.pem" "$D/secret/keys/secretkey" "$D/secret/cert/server.key" \
  "$D/secret/aptly/api.htpasswd"

echo "Directorio de datos listo."
