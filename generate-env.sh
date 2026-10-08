#!/bin/sh
# Copyright (c) 2026 Juan Maria Gomez Lopez <juan-maria.gomez-lopez@tutamail.com>
# SPDX-License-Identifier: MIT
# Crea .env a partir de .env.example rellenando los secretos con valores aleatorios.
set -eu
cd "$(dirname "$0")"

if [ -f .env ]; then
  echo ".env ya existe; no se sobrescribe." >&2
  exit 1
fi

rand() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1"; }

sed -e "s|^HARBOR_ADMIN_PASSWORD=.*|HARBOR_ADMIN_PASSWORD=$(rand 20)|" \
    -e "s|^DB_PASSWORD=.*|DB_PASSWORD=$(rand 24)|" \
    -e "s|^REGISTRY_PASSWORD=.*|REGISTRY_PASSWORD=$(rand 32)|" \
    -e "s|^CORE_SECRET=.*|CORE_SECRET=$(rand 16)|" \
    -e "s|^JOBSERVICE_SECRET=.*|JOBSERVICE_SECRET=$(rand 16)|" \
    -e "s|^CSRF_KEY=.*|CSRF_KEY=$(rand 32)|" \
    -e "s|^ROBOT_SCANNER_NAME_PREFIX=.*|ROBOT_SCANNER_NAME_PREFIX=$(rand 8)|" \
    -e "s|^APTLY_API_PASSWORD=.*|APTLY_API_PASSWORD=$(rand 24)|" \
    -e "s|^DTRACK_DB_PASSWORD=.*|DTRACK_DB_PASSWORD=$(rand 24)|" \
    .env.example > .env
chmod 600 .env

echo ".env creado. Contraseña de 'admin':"
grep '^HARBOR_ADMIN_PASSWORD=' .env | cut -d= -f2
