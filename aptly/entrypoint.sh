#!/bin/sh
# Copyright (c) 2026 Juan Maria Gomez Lopez <juan-maria.gomez-lopez@tutamail.com>
# SPDX-License-Identifier: MIT
# Arranca la API de aptly. La primera vez genera la clave GPG con la que se firman
# los repositorios publicados y exporta la clave pública a public/ para los clientes.
set -eu

ROOT=/var/lib/aptly
mkdir -p "$GNUPGHOME" "$ROOT/public"
chmod 700 "$GNUPGHOME"

if ! gpg --batch --list-secret-keys 2>/dev/null | grep -q '^sec'; then
  echo "Generando clave GPG de firma: $APTLY_GPG_NAME <$APTLY_GPG_EMAIL>..."
  gpg --batch --pinentry-mode loopback --passphrase '' \
    --quick-gen-key "$APTLY_GPG_NAME <$APTLY_GPG_EMAIL>" rsa4096 sign never
fi

# Clave pública (ASCII y binaria) en la raíz del repositorio publicado
gpg --batch --armor --export > "$ROOT/public/repo-key.asc"
gpg --batch --export > "$ROOT/public/repo-key.gpg"

# -no-lock: permite usar la CLI (docker compose exec aptly aptly ...) con la API en marcha
exec aptly api serve -listen=:8080 -no-lock
