---
name: harbor-docker
description: Gestionar imágenes y contenedores Docker y el registry Harbor desplegado en harbor_docker. Cubre el ciclo de vida del stack (arrancar, parar, logs, salud, actualizar, backup), las imágenes (build, tag, push, pull, multi-arquitectura, copiar entre registries, limpieza local) la administración de Harbor por API (proyectos, robots, usuarios, artefactos, escaneos, garbage collection) el repositorio Debian integrado (aptly: subir y publicar paquetes .deb, configurar clientes apt) y Dependency-Track (subir SBOM, consultar vulnerabilidades). Úsala cuando el usuario pida subir, bajar, listar, borrar o escanear imágenes, crear proyectos o cuentas robot, publicar paquetes Debian, subir SBOM, revisar o reiniciar contenedores, liberar espacio o diagnosticar Harbor.
---

<!-- Copyright (c) 2026 Juan Maria Gomez Lopez <juan-maria.gomez-lopez@tutamail.com>
     SPDX-License-Identifier: MIT -->

# Gestión de Docker y Harbor (harbor_docker)

Despliegue: `docker-compose.yml` en la raíz del proyecto, Harbor v2.15.2, secretos en `.env`.
Contexto y decisiones de diseño: `CLAUDE.md`. Documentación de usuario: `README.md`.

## Reglas

- Ejecuta los comandos `docker compose` desde la raíz del proyecto (donde está `docker-compose.yml`).
- **No muestres en el chat** valores de `.env` (contraseñas, secretos) ni secretos de robots,
  salvo que el usuario lo pida expresamente. Cárgalos en variables de entorno.
- **Pide confirmación** antes de cualquier operación destructiva o irreversible: borrar
  artefactos, repositorios, proyectos o robots; lanzar un GC; `docker compose down` en
  producción; `docker system prune`; borrar `data/`; cambiar secretos.
- Usa cuentas robot para automatizaciones y `admin` solo para administrar.
- Las recetas marcadas con ✅ se han verificado contra Harbor v2.15.2.

## 0. Preparar variables (hazlo al empezar)

```bash
# desde la raíz del proyecto (donde está docker-compose.yml)
set -a; . ./.env; set +a
H="$HARBOR_EXTERNAL_URL"                              # p. ej. https://harbor.midominio.local
REG="${H#https://}"                                   # host[:puerto] para docker
CA="--cacert ${HARBOR_DATA_DIR}/secret/cert/server.crt"   # o -k si no está accesible
AUTH="-u admin:$HARBOR_ADMIN_PASSWORD"                # si cambiaron la contraseña de admin, pídela
JSON='-H Content-Type:application/json'
API="$H/api/v2.0"
```

`HARBOR_ADMIN_PASSWORD` solo vale mientras no se haya cambiado la contraseña de `admin` desde la UI.
Si la API devuelve 401, pide al usuario las credenciales o un robot con permisos de sistema.

---

## 1. Ciclo de vida del stack (contenedores de Harbor)

```bash
docker compose up -d --wait                  # ✅ arranca todo (~20 s) y espera a healthy; ejecuta init
docker compose ps                            # ✅ estado + health
docker compose ps --format '{{.Name}}\t{{.Status}}'
docker compose stop | start | restart <svc>  # svc: proxy core portal jobservice registry registryctl redis postgresql trivy-adapter
docker compose down                          # ✅ borra contenedores y red; data/ se conserva
docker compose up -d --force-recreate <svc>  # recrear tras cambiar .env
docker compose config -q                     # ✅ validar compose + .env
```

Salud y diagnóstico:

```bash
curl -s $CA $API/health                      # ✅ estado por componente (core, database, jobservice, portal, redis, registry, registryctl, trivy)
curl -s $CA $AUTH $API/systeminfo            # ✅ versión (harbor_version)
for c in $(docker compose ps -aq); do
  docker inspect $c --format '{{.Name}} {{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{end}} restarts={{.RestartCount}}'
done                                          # ✅ reinicios > 0 indica un problema
docker inspect harbor-core --format '{{json .State.Health.Log}}'   # salida de los últimos healthchecks
docker compose logs --since 15m <svc>
docker compose logs --since 15m | grep -iE 'error|fatal|panic'
docker stats --no-stream $(docker compose ps -q)
docker compose exec core sh                   # shell dentro de un contenedor (si tiene sh)
docker compose top
```

Qué mirar según el síntoma:

| Síntoma | Servicio y log |
|---|---|
| Login/push/pull fallan | `proxy` (accesos), `core` (auth/token), `registry` |
| UI en blanco o 502 | `proxy`, `portal`, `core` |
| Jobs (replicación, GC, retención) atascados | `jobservice`, `redis` |
| Escaneos en Error | `trivy-adapter` (descarga de la BD desde ghcr.io) |
| `core` unhealthy | `core` + `postgresql` (contraseña de BD), `redis` |
| Dependency-Track no inicia sesión / no arranca | `dtrack-apiserver` (BD, `OOMKilled`), `dtrack-db`; la UI necesita llegar a `DTRACK_API_BASE_URL` |
| `permission denied` | `docker compose up -d` (init corrige los permisos de `data/`) |

Después de editar ficheros de `config/`, reinicia el servicio afectado:
`nginx.conf` → `proxy`; `registry/config.yml` → `registry registryctl`;
`jobservice/config.yml` → `jobservice`; `portal/nginx.conf` → `portal`.

## 2. Imágenes con el cliente Docker

### Login

```bash
echo "$HARBOR_ADMIN_PASSWORD" | docker login "$REG" -u admin --password-stdin   # ✅
echo "$ROBOT_SECRET" | docker login "$REG" -u 'robot$proyecto+nombre' --password-stdin   # ✅ (comillas simples por el $)
docker logout "$REG"
```

Error `x509: certificate signed by unknown authority`: copia `data/secret/cert/server.crt` a
`/etc/docker/certs.d/<REG>/ca.crt` en el cliente (requiere sudo, así que pídeselo al usuario).
Para pruebas sin tocar `~/.docker/config.json`, usa `export DOCKER_CONFIG=$(mktemp -d)`.

### Etiquetar, subir y bajar

Formato: `<REG>/<proyecto>/<repositorio>[/<subruta>]:<tag>`. El proyecto debe existir (sección 3).

```bash
docker tag  app:1.0 "$REG/demo/app:1.0"            # ✅
docker push "$REG/demo/app:1.0"                     # ✅
docker pull "$REG/demo/app:1.0"                     # ✅
docker push "$REG/demo/team/app:1.0"                # ✅ repositorios anidados permitidos
docker pull "$REG/demo/app@sha256:<digest>"         # por digest (inmutable)
docker inspect --format '{{json .RepoDigests}}' "$REG/demo/app:1.0"   # digest tras push/pull
```

### Construir y subir

```bash
docker build -t "$REG/demo/app:1.0" .
docker push "$REG/demo/app:1.0"

# Multi-arquitectura (buildx): sube un índice OCI con varias plataformas
docker buildx create --use --name harbor-builder 2>/dev/null || docker buildx use harbor-builder
docker buildx build --platform linux/amd64,linux/arm64 -t "$REG/demo/app:1.0" --push .
```

Con certificado autofirmado, buildx (BuildKit en contenedor) no lee `/etc/docker/certs.d`:
crea el builder con `--config buildkitd.toml` declarando la CA en `[registry."<REG>"] ca=[...]`.

### Copiar o migrar imágenes entre registries

```bash
# Con el cliente Docker (solo la arquitectura local)
docker pull docker.io/library/nginx:1.27
docker tag  docker.io/library/nginx:1.27 "$REG/library/nginx:1.27"
docker push "$REG/library/nginx:1.27"

# Todas las arquitecturas, sin almacenar en local
docker buildx imagetools create -t "$REG/library/nginx:1.27" docker.io/library/nginx:1.27
docker buildx imagetools inspect "$REG/library/nginx:1.27"
```

Para copias periódicas usa **replicación** de Harbor (UI → Administración → Replicaciones)
o un proyecto **proxy cache** (UI → Registries + proyecto de tipo proxy cache).

### Entornos sin red (air-gapped)

```bash
docker save -o app_1.0.tar "$REG/demo/app:1.0"     # exportar (puede incluir varias imágenes)
docker load -i app_1.0.tar                          # importar en el otro host
docker push "$REG/demo/app:1.0"
```

### Inspección y limpieza locales (en el host Docker, no en Harbor)

```bash
docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Size}}'
docker images --filter dangling=true
docker image inspect <img>          # capas, entrypoint, labels, arquitectura
docker history <img>                # capas y tamaño de cada una
docker system df                    # espacio usado por imágenes, contenedores, volúmenes y caché
docker image prune                  # imágenes dangling                ⚠ confirmar
docker image prune -a --filter "until=168h"   # sin uso y > 7 días     ⚠ confirmar
docker builder prune --filter "until=168h"    # caché de build          ⚠ confirmar
docker container prune              # contenedores parados             ⚠ confirmar
docker rmi "$REG/demo/app:1.0"      # quitar una imagen local (no afecta a Harbor)
```

**No** uses `docker system prune -a --volumes` en el servidor de Harbor sin confirmación: las
imágenes `goharbor/*` se tendrían que descargar de nuevo. Los datos están en `data/`, no en volúmenes.

## 3. Administración de Harbor por API (v2.0)

- Paginación: `?page=1&page_size=100` (máx. 100). El total va en la cabecera `X-Total-Count`.
- **Repositorios con `/`** en el nombre: codifica la barra **dos veces** (`team/app` → `team%252Fapp`). ✅
- `reference` de un artefacto = tag o digest `sha256:...`.

### Proyectos

```bash
curl -s $CA $AUTH "$API/projects?page_size=100" | jq -r '.[].name'
curl -s $CA $AUTH $JSON -X POST "$API/projects" \
     -d '{"project_name":"demo","public":false}'                         # ✅ 201
curl -s $CA $AUTH $JSON -X POST "$API/projects" \
     -d '{"project_name":"demo","public":false,"storage_limit":10737418240}'   # con cuota de 10 GiB
curl -s $CA $AUTH "$API/projects/demo/summary"                             # ✅ uso y cuota
curl -s $CA $AUTH -H 'X-Is-Resource-Name: true' -X DELETE "$API/projects/demo"   # ⚠ debe estar vacío
```

Ajustes del proyecto (escaneo automático, bloquear imágenes vulnerables):

```bash
curl -s $CA $AUTH $JSON -X PUT "$API/projects/demo" -d '{"metadata":{
  "auto_scan":"true","prevent_vul":"true","severity":"high"}}'
```

### Miembros y usuarios

```bash
curl -s $CA $AUTH $JSON -X POST "$API/users" -d '{"username":"ana","email":"ana@ejemplo.com",
  "realname":"Ana","password":"<Contraseña1>"}'        # solo con autenticación de BD local
# role_id: 1=ProjectAdmin 2=Developer 3=Guest 4=Maintainer 5=Limited Guest
curl -s $CA $AUTH $JSON -X POST "$API/projects/demo/members" \
     -d '{"role_id":2,"member_user":{"username":"ana"}}'
curl -s $CA $AUTH "$API/projects/demo/members"
```

### Cuentas robot (CI/CD)

```bash
R=$(curl -s $CA $AUTH $JSON -X POST "$API/robots" -d '{
  "name":"ci","duration":-1,"level":"project",
  "permissions":[{"kind":"project","namespace":"demo","access":[
    {"resource":"repository","action":"push"},
    {"resource":"repository","action":"pull"}]}]}')                     # ✅
ROBOT_NAME=$(echo "$R" | jq -r .name)      # robot$demo+ci
ROBOT_SECRET=$(echo "$R" | jq -r .secret)  # solo se muestra una vez: guárdalo en el gestor de secretos del CI
curl -s $CA $AUTH "$API/robots?page_size=100" | jq -r '.[] | "\(.id) \(.name) expira=\(.expires_at)"'
curl -s $CA $AUTH -X DELETE "$API/robots/<id>"                           # ⚠
```

`duration` en días (`-1` = sin caducidad; mejor 90-365). Las acciones más habituales sobre
`repository` son `pull`, `push`, `delete` y `list`; `artifact`: `read`, `delete`, `list`; `tag`: `create`, `delete`, `list`.

### Repositorios, artefactos y tags

```bash
curl -s $CA $AUTH "$API/projects/demo/repositories?page_size=100" | jq -r '.[].name'   # ✅
curl -s $CA $AUTH "$API/projects/demo/repositories/team%252Fapp/artifacts?with_tag=true&page_size=100" \
  | jq -r '.[] | "\(.digest) \([.tags[]?.name] | join(",")) \(.size)"'              # ✅
curl -s $CA $AUTH "$API/projects/demo/repositories/app/artifacts/1.0"              # detalle
# añadir un tag a un artefacto existente (sin re-push)
curl -s $CA $AUTH $JSON -X POST "$API/projects/demo/repositories/app/artifacts/1.0/tags" -d '{"name":"stable"}'
# ⚠ quitar un tag (el artefacto se mantiene)
curl -s $CA $AUTH -X DELETE "$API/projects/demo/repositories/team%252Fapp/artifacts/1.1/tags/1.0"   # ✅ 200
# ⚠ borrar un artefacto (todas sus tags)
curl -s $CA $AUTH -X DELETE "$API/projects/demo/repositories/team%252Fapp/artifacts/1.1"           # ✅ 200
# ⚠ borrar un repositorio completo
curl -s $CA $AUTH -X DELETE "$API/projects/demo/repositories/team%252Fapp"
```

Borrar artefactos **no libera disco** hasta que se ejecuta un GC (sección 4).

### Escaneo de vulnerabilidades (Trivy)

```bash
curl -s $CA $AUTH -X POST "$API/projects/demo/repositories/app/artifacts/1.0/scan" -w '%{http_code}\n'   # ✅ 202
curl -s $CA $AUTH "$API/projects/demo/repositories/app/artifacts/1.0?with_scan_overview=true" \
  | jq '.scan_overview[] | {scan_status, severity, summary: .summary.summary}'     # ✅ scan_status: Success
curl -s $CA $AUTH "$API/projects/demo/repositories/app/artifacts/1.0/additions/vulnerabilities" \
  | jq '.[].vulnerabilities[] | select(.severity=="Critical") | {id, package, version, fix_version}'
curl -s $CA $AUTH $JSON -X POST "$API/system/scanAll/schedule" -d '{"schedule":{"type":"Manual"}}'   # escanear todo
curl -s $CA $AUTH "$API/scanners" | jq -r '.[] | "\(.name) default=\(.is_default)"'                     # ✅ Trivy
```

## 4. Liberar espacio en Harbor

1. Borrar lo que sobra: artefactos o tags (sección 3) o, mejor, **políticas de retención**
   (UI → Proyecto → Policy → Tag Retention, p. ej. "conservar los 10 más recientes").
2. Lanzar el **garbage collection** ⚠ (durante el GC conviene no hacer push):

```bash
curl -s $CA $AUTH $JSON -X POST "$API/system/gc/schedule" -d '{"schedule":{"type":"Manual"},
  "parameters":{"delete_untagged":true,"dry_run":false,"workers":1}}' -w '%{http_code}\n'   # ✅ 201
curl -s $CA $AUTH "$API/system/gc?page_size=1" | jq '.[0] | {id, job_status, creation_time}'   # ✅ Success
curl -s $CA $AUTH "$API/system/gc/<id>/log"                                                   # log del GC
```

Usa `"dry_run":true` para ver primero qué se borraría. Para programarlo:
`{"schedule":{"type":"Weekly","cron":"0 0 3 * * 6"}}` (cron de 6 campos con segundos).

3. Comprobar el resultado: `du -sh data/registry` (con sudo) y `$API/projects/<p>/summary`.

## 4b. Repositorio Debian (aptly)

API en `$H/aptly/api` con auth básica; repos publicados en `$H/debian/` (también por HTTP).
Detalle y configuración de clientes: sección "Repositorio Debian (aptly)" del README.

```bash
A="$H/aptly/api"; AA="-u ${APTLY_API_USER:-aptly}:$APTLY_API_PASSWORD"
curl -s $CA $AA $A/version                                                    # ✅
curl -s $CA $AA $A/repos                                                      # listar repos
curl -s $CA $AA -X POST $JSON -d '{"Name":"local-trixie","DefaultDistribution":"trixie","DefaultComponent":"main"}' $A/repos   # ✅ crear repo
curl -s $CA $AA -X POST -F file=@pkg_1.0_amd64.deb $A/files/up1               # ✅ subir
curl -s $CA $AA -X POST $A/repos/local-trixie/file/up1                          # ✅ añadir al repo
curl -s $CA $AA -X POST $JSON -d '{"SourceKind":"local","Sources":[{"Name":"local-trixie"}],"Distribution":"trixie","Architectures":["amd64","arm64","all"],"Signing":{"Batch":true}}' $A/publish/:.   # ✅ 1ª publicación
curl -s $CA $AA -X PUT $JSON -d '{"Signing":{"Batch":true}}' $A/publish/:./trixie   # republicar tras añadir paquetes
curl -s $CA $AA "$A/repos/local-trixie/packages"                                # refs de paquetes
# ⚠ quitar paquetes: -X DELETE $JSON -d '{"PackageRefs":["Pall hola 1.0 abcd..."]}' $A/repos/local-trixie/packages (y republicar)
docker compose exec aptly aptly publish list                                  # ✅ CLI
```

- Script listo para usar: `examples/publish-deb.sh [ficheros.deb]` (crea repo, sube y publica). ✅
- Para imágenes: `examples/push-image.sh [contexto]` (proyecto, build, push, `SCAN=1`, `PLATFORMS=`). ✅
- Nunca borres ni regeneres `data/aptly/gpg/`: los clientes dejarían de aceptar el repositorio.
- Tras cambiar `aptly/`: `docker compose build aptly && docker compose up -d aptly`.

## 4c. Dependency-Track

Servicios `dtrack-db`, `dtrack-apiserver` (API en `http://localhost:8081`) y `dtrack-frontend`
(UI en `http://localhost:8082`), en su propia red `dtrack`. Primer login `admin`/`admin`
(obliga a cambiarla). Detalle: sección "Dependency-Track" del README.

```bash
D=http://localhost:8081
curl -s $D/api/version                                                          # ✅
T=$(curl -s -X POST $D/api/v1/user/login -d "username=admin&password=$DT_PASS")  # ✅ JWT (o usa -H "X-Api-Key: ...")
curl -s -X POST $D/api/v1/bom -H "Authorization: Bearer $T" \
  -F autoCreate=true -F projectName=app -F projectVersion=1.0 -F bom=@bom.json  # ✅ subir SBOM CycloneDX
curl -s "$D/api/v1/project?name=app" -H "Authorization: Bearer $T"              # ✅ buscar proyecto
curl -s "$D/api/v1/vulnerability/project/<uuid>" -H "Authorization: Bearer $T"  # vulnerabilidades del proyecto
docker exec dtrack-db psql -U dtrack -d dtrack -tAc 'select "NAME","VERSION" from "PROJECT"'   # ✅
```

- SBOM de una imagen de Harbor: `trivy image --format cyclonedx -o bom.json localhost/<p>/<img>:<tag>`.
- Script listo para usar: `examples/upload-sbom.sh [imagen|bom.json]` (genera el SBOM con Trivy, sube,
  espera el análisis y lista hallazgos; `FAIL_ON=HIGH` para CI; `DTRACK_API_KEY` o `DTRACK_USER`/`DTRACK_PASSWORD`). ✅
- `data/dtrack/apiserver` guarda las claves con las que DT cifra secretos: no lo borres.

## 5. Base de datos, backups y actualización

```bash
docker exec harbor-db psql -U postgres -d registry -tAc "select name from project"      # ✅
docker exec harbor-db pg_dump -U postgres registry | gzip > harbor-db-$(date +%F).sql.gz # ✅ backup en caliente de la BD
docker exec dtrack-db pg_dump -U dtrack dtrack | gzip > dtrack-db-$(date +%F).sql.gz      # backup de Dependency-Track
```

- **Backup completo** ⚠ (parada breve): `docker compose stop && sudo tar --numeric-owner -czpf
  /backups/harbor-$(date +%F).tgz data .env && docker compose start`.
- **Restaurar** ⚠: `docker compose down`, sustituir `data/` y `.env` desde el tar
  (`--numeric-owner -p`), y `docker compose up -d --wait`.
- **Actualizar**: sigue la sección "Actualizar Harbor" del README. Antes de cambiar
  `HARBOR_VERSION`, extrae las plantillas de la nueva versión
  (`docker create goharbor/prepare:<v>` + `docker cp <id>:/usr/src/app/templates ...`) y compáralas
  con `config/` y `docker-compose.yml`. Después: `docker compose pull && docker compose build --pull aptly && docker compose up -d --wait`
  (aptly es una imagen local con `pull_policy: build`; `pull` la omite),
  y comprueba health, versión y un push/pull.

## 6. Comprobación final tras cualquier cambio

```bash
docker compose ps -a                      # todos healthy, init "Exited (0)"
curl -s $CA $API/health | jq -r .status   # healthy
# 0 reinicios
for c in $(docker compose ps -aq); do docker inspect $c --format '{{.Name}} {{.RestartCount}}'; done
```
