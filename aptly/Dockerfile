# Copyright (c) 2026 Juan Maria Gomez Lopez <juan-maria.gomez-lopez@tutamail.com>
# SPDX-License-Identifier: MIT

# Repositorio Debian (aptly) para el stack de Harbor.
# aptly 1.6 de Debian trixie + gnupg para firmar los repositorios publicados.
FROM debian:trixie-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends aptly gnupg ca-certificates curl bzip2 xz-utils \
 && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh

# Mismo UID que el resto de componentes de Harbor
ENV HOME=/var/lib/aptly \
    GNUPGHOME=/var/lib/aptly/gpg
USER 10000:10000
WORKDIR /var/lib/aptly
EXPOSE 8080

HEALTHCHECK CMD curl -fsS http://127.0.0.1:8080/api/version || exit 1

ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
