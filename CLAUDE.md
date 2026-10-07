<!-- Copyright (c) 2026 Juan Maria Gomez Lopez <juan-maria.gomez-lopez@tutamail.com>
     SPDX-License-Identifier: MIT -->

# CLAUDE.md — harbor_docker

Contexto del proyecto para Claude Code. Responde y documenta **en español**.

## Qué es

Despliegue autocontenido de **Harbor v2.15.2** (registry privado de imágenes OCI) con
Docker Compose, **sin** el instalador oficial (`install.sh` / `prepare`). Ver
[README.md](README.md) para la documentación de usuario.

Para tareas de gestión de imágenes, contenedores o del propio Harbor, usa la skill
[`.claude/skills/harbor-docker/SKILL.md`](.claude/skills/harbor-docker/SKILL.md).

## Historia (sesión del 2026-10-05)

1. Se pidió "un docker compose para desplegar Harbor". Harbor no se despliega con un único
   compose: cada componente necesita ficheros de configuración, claves y certificados que
   normalmente genera `prepare` a partir de `harbor.yml`.
2. Se eligió crear un despliegue autocontenido: compose + `config/` + `.env` + un servicio
   `init`. Las configuraciones se obtuvieron de las **plantillas Jinja oficiales** dentro
   de la imagen `goharbor/prepare:v2.15.2` (`/usr/src/app/templates`), extraídas con
   `docker create` + `docker cp`. También se leyó `utils/configs.py`, `utils/cert.py` y
   `g.py` para obtener los valores por defecto. No se ejecutó `prepare`: necesita
   `--privileged` y montar `/` del host, y el modo automático lo denegó.
3. Se probó localmente (puertos en `127.0.0.1:18080/18443`, datos en un directorio temporal):
   los 9 servicios healthy, `/api/v2.0/health` OK, push/pull de `busybox`, escaneo Trivy en
   `Success`. Después se borró todo lo de prueba.
4. Los ficheros se movieron a su ubicación definitiva.
5. Al documentar se detectó la condición de carrera del compose oficial: `jobservice` se
   arranca antes de que `core` responda, entra en pánico (`failed to load rest config`) y
   se reinicia unas 4 veces. Se corrigió con `depends_on: condition: service_healthy` y
   `healthcheck.start_interval: 3s` (el `interval` de 30 s de las imágenes alargaba el
   arranque a ~2 min). Resultado verificado: arranque limpio en ~17 s y 0 reinicios.
6. Se verificaron contra la instancia de prueba todas las recetas de API de la skill:
   proyectos, robots, push con robot, repos anidados (`%252F`), borrado de tag y de
   artefacto, GC, `pg_dump`, `psql` y persistencia tras `down`/`up`.

7. (2026-10-06) Se integró un **repositorio Debian con aptly** (servicio `aptly`, imagen
   construida desde `aptly/` sobre `debian:trixie-slim`, aptly 1.6.1). Verificado en copia
   temporal: 10 servicios healthy en ~16 s, 0 reinicios, API con auth básica (401 sin
   credenciales), subida + publicación firmada, `apt install` por HTTP y HTTPS, rechazo de
   firma con otra clave, y clave GPG conservada tras `down`/`up`.

## Decisiones de diseño (y por qué)

| Decisión | Motivo |
|---|---|
| Configuración pre-renderizada en `config/` y secretos en `.env` | Evitar `prepare` (privilegiado) y poder versionar la configuración |
| Servicio `init` con la imagen `goharbor/prepare` y `entrypoint` `/init.sh` | Ya trae `openssl` y `htpasswd` y es la misma versión de Harbor; se ejecuta como root para hacer `chown` |
| `init` idempotente; se ejecuta en cada `docker compose up` | Solo genera lo que falta (claves, cert, secretkey) y siempre regenera el htpasswd y los permisos |
| HTTPS siempre activo, con certificado autofirmado si no hay uno | Docker exige TLS (o `insecure-registries`) para registries remotos |
| Sin contenedor `harbor-log` ni driver `syslog`; `json-file` con rotación | Menos piezas; `docker compose logs` funciona |
| `depends_on` con `service_healthy` + `start_interval` | Elimina los reinicios de jobservice sin ralentizar el arranque |
| Solo `healthcheck` con tiempos en `x-harbor-common` (sin `test`) | Docker hereda el `test` del `HEALTHCHECK` de cada imagen (verificado con `docker inspect`) |
| aptly (no reprepro) con imagen propia mínima | API REST para CI, firma GPG, snapshots y mirrors; no hay imagen oficial |
| API de aptly tras nginx con `auth_basic` (htpasswd bcrypt del `init`) | aptly no tiene autenticación propia |
| `/debian/` servido por nginx desde `data/aptly/public` y también por HTTP | apt verifica las firmas GPG; evita repartir el certificado autofirmado |
| Clave GPG generada por el entrypoint de aptly, sin passphrase | Firma desatendida por API (`"Signing":{"Batch":true}`) |
| Puertos en `127.0.0.1:80/443` y URL `https://localhost` por defecto | Acceso solo por túnel SSH (`-L 443:127.0.0.1:443 -L 80:127.0.0.1:80`); la URL debe ser igual en servidor y clientes porque Harbor la usa como realm del token |
| `container_name` iguales al oficial (`harbor-core`, `harbor-db`, `redis`, `nginx`...; excepción: el registry se llama `registry_harbor`) | Compatibilidad con documentación y scripts de Harbor; `registry_harbor` por petición del equipo (2026-10-06) |

## Datos técnicos clave

- Imágenes: `goharbor/<componente>:${HARBOR_VERSION}`. Redis es **`valkey-photon`** (no `redis-photon`).
- UID/GID: componentes Harbor y trivy = **10000**; PostgreSQL y Valkey = **999**.
- Índices Redis: 0 core, 1 registry, 2 jobservice, 5 trivy. Las URL llevan `?idle_timeout_seconds=30`.
- URLs internas: `core:8080`, `registry:5000`, `registryctl:8080`, `portal:8080`,
  `jobservice:8080`, `trivy-adapter:8080`, `postgresql:5432` (BD `registry`, usuario `postgres`).
- Usuario interno del registry: `harbor_registry_user` (htpasswd bcrypt en `data/secret/registry/passwd`).
- Par de claves del token service: `data/secret/core/private_key.pem` (core) y
  `data/secret/registry/root.crt` (registry). `data/secret/keys/secretkey` (16 caracteres) cifra
  credenciales en la BD: **no regenerar** en una instalación en uso.
- `HARBOR_ADMIN_PASSWORD` y `DB_PASSWORD` solo se aplican en el primer arranque.
- `nginx.conf` del proxy es estático (no se interpola `.env`): la redirección HTTP→HTTPS usa
  `$host` sin puerto.
- aptly: `rootDir` `/var/lib/aptly` = `data/aptly` (UID 10000), `gpgProvider: gpg2`,
  `GNUPGHOME=/var/lib/aptly/gpg`, API en `aptly:8080` (`aptly api serve -no-lock`).
  Rutas públicas: `/debian/` (repo) y `/aptly/api/` (API con auth).
- Requiere Docker Engine ≥ 25 (`start_interval`). Probado con Docker 27.1.1 y Compose v5.5.1.

## Convenciones al modificar este proyecto

- Ante cualquier cambio de configuración, **consulta primero la plantilla oficial** de la versión
  correspondiente (`docker cp` desde `goharbor/prepare:<versión>`) en vez de inventar claves.
- Mantén el estilo del compose: anclas `x-harbor-common` / `x-core-urls`, `cap_drop: [ALL]`,
  comentarios en español.
- Valida con `docker compose --env-file <env> config -q` antes de dar algo por terminado.
- Para probar sin tocar el despliegue real: copia el proyecto a un directorio temporal, usa un
  `.env` con `HARBOR_HOSTNAME=localhost`, puertos `127.0.0.1:18080/18443` y `HARBOR_DATA_DIR`
  temporal. Comprueba healthy y 0 reinicios
  (`docker inspect <c> --format '{{.RestartCount}}'`). Si el despliegue real está en marcha,
  quita `container_name` y el `name:` de la red en la copia y usa `-p harbortest`. Borra `data/` con un contenedor
  (los ficheros son de los UID 10000/999).
- Licencia: MIT (`LICENSE`), salvo `docker-compose.yml` y los `config/` de Harbor, que derivan de
  las plantillas oficiales y siguen bajo Apache-2.0 (`NOTICE`). Todo fichero nuevo lleva la cabecera
  `Copyright (c) 2026 Juan Maria Gomez Lopez <juan-maria.gomez-lopez@tutamail.com>` +
  `SPDX-License-Identifier: MIT` (en JSON, que no admite comentarios, basta con `LICENSE`).
- **Nunca** subas `.env` ni `data/` a git ni los muestres en el chat: contienen secretos.
- Operaciones destructivas (borrar `data/`, borrar proyectos o artefactos, GC, `down` en
  producción): pide confirmación al usuario antes de ejecutarlas.

## Comandos rápidos

```bash
./generate-env.sh                          # crear .env (una vez)
docker compose config -q                   # validar
docker compose up -d --wait                # arrancar
docker compose ps                          # estado
docker compose logs -f core                # logs
curl -sk https://<host>/api/v2.0/health    # salud por componente
```
