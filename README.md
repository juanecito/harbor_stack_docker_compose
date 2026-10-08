<!-- Copyright (c) 2026 Juan Maria Gomez Lopez <juan-maria.gomez-lopez@tutamail.com>
     SPDX-License-Identifier: MIT -->

# Harbor v2.15.2 con Docker Compose

Despliegue autocontenido de [Harbor](https://goharbor.io), un registry privado de imágenes
de contenedores (OCI) con UI web, control de acceso por proyectos, cuentas robot,
escaneo de vulnerabilidades (Trivy), replicación y proxy-cache. Incluye además un
**repositorio Debian** (aptly) para distribuir paquetes `.deb` propios con `apt` y
**Dependency-Track** para analizar SBOM (CycloneDX) y vigilar las vulnerabilidades de las
dependencias.

A diferencia del instalador oficial, **no hace falta ejecutar `install.sh` ni `prepare`**:
la configuración de cada componente ya está en `config/` (renderizada a partir de las
plantillas oficiales de `goharbor/prepare:v2.15.2`) y un contenedor `init` prepara el
directorio de datos en cada arranque.

---

## Índice

1. [Requisitos](#requisitos)
2. [Puesta en marcha](#puesta-en-marcha)
3. [Arquitectura](#arquitectura)
4. [Estructura de ficheros](#estructura-de-ficheros)
5. [Variables de `.env`](#variables-de-env)
6. [Certificado TLS](#certificado-tls)
7. [Configurar los clientes Docker](#configurar-los-clientes-docker)
8. [Acceso desde otras máquinas (túnel SSH)](#acceso-desde-otras-máquinas-túnel-ssh)
9. [Uso básico](#uso-básico)
10. [Repositorio Debian (aptly)](#repositorio-debian-aptly)
11. [Dependency-Track](#dependency-track)
12. [Operación del stack](#operación-del-stack)
13. [Copias de seguridad y restauración](#copias-de-seguridad-y-restauración)
14. [Actualizar Harbor](#actualizar-harbor)
15. [Cambios de configuración habituales](#cambios-de-configuración-habituales)
16. [Solución de problemas](#solución-de-problemas)
17. [Seguridad](#seguridad)
18. [Diferencias con el instalador oficial](#diferencias-con-el-instalador-oficial)
19. [Licencia](#licencia)

---

## Requisitos

| Recurso | Mínimo | Recomendado |
|---|---|---|
| CPU | 2 | 4 |
| RAM | 8 GB | 16 GB (Dependency-Track tiene un límite de 6 GB) |
| Disco | 50 GB | 160 GB+ (depende del volumen de imágenes) |

- Docker Engine **25 o superior** (se usa `healthcheck.start_interval`) y el plugin
  Docker Compose v2.
- Puertos libres en el host: `80` y `443`, y `8081`/`8082` para Dependency-Track (configurables).
- Un nombre DNS (o IP) por el que los clientes accederán a Harbor.
- Salida a internet hacia `ghcr.io` para que Trivy descargue su base de datos de
  vulnerabilidades (ver [modo offline](#trivy-sin-acceso-a-internet)), y hacia las fuentes
  de Dependency-Track (NVD, GitHub Advisories, OSV...).

## Puesta en marcha

```bash
# 1. Generar .env con contraseñas y secretos aleatorios (muestra la contraseña de admin)
./generate-env.sh

# 2. (Opcional) Por defecto solo escucha en 127.0.0.1 y se accede por túnel SSH con
#    la URL https://localhost (ver "Acceso desde otras máquinas"). Para abrirlo a la red:
#    HARBOR_HOSTNAME=harbor.midominio.local
#    HARBOR_EXTERNAL_URL=https://harbor.midominio.local
#    HARBOR_HTTP_PORT=80   HARBOR_HTTPS_PORT=443
vi .env

# 3. Arrancar y esperar a que todos los servicios estén healthy (~20 s)
docker compose up -d --wait

# 4. Comprobar
docker compose ps
curl -sk https://<HARBOR_HOSTNAME>/api/v2.0/health
```

UI: `https://<HARBOR_HOSTNAME>`. Usuario `admin`; la contraseña es `HARBOR_ADMIN_PASSWORD`
de `.env`. Cámbiala desde la UI tras el primer acceso.

## Arquitectura

```
                     :80 (redirige a HTTPS)  :443
                                 │
                        ┌────────▼────────┐
  clientes docker ────► │  proxy (nginx)  │  TLS, enruta por ruta
  navegador             └──┬───────────┬──┘
              /  (UI)      │           │  /api/  /v2/  /service/  /c/
                  ┌────────▼──┐   ┌────▼─────┐
                  │  portal   │   │   core   │──── token service, API, auth
                  └───────────┘   └┬──┬───┬─┬┘
                                   │  │   │ └─────────────┐
                  ┌────────────────┘  │   └──────┐        │
            ┌─────▼────┐      ┌───────▼──┐  ┌────▼─────┐  │
            │postgresql│      │  redis   │  │ registry │◄─┘ (blobs y manifiestos)
            └──────────┘      └──▲────▲──┘  └────▲─────┘
                                 │    │          │
                  ┌──────────────┘    │   ┌──────┴──────┐
            ┌─────┴──────┐  ┌─────────┴─┐ │ registryctl │ (garbage collection)
            │ jobservice │  │   trivy   │ └─────────────┘
            └────────────┘  └───────────┘
            (replicación, GC,  (escaneo de
             escaneos, ...)    vulnerabilidades)
```

| Servicio | Imagen | Función |
|---|---|---|
| `init` | `goharbor/prepare` | One-shot: crea `data/`, claves, htpasswd, certificado y permisos |
| `postgresql` | `goharbor/harbor-db` | Base de datos (`registry`) |
| `redis` | `goharbor/valkey-photon` | Caché y colas (BD 0 core, 1 registry, 2 jobservice, 5 trivy) |
| `registry` | `goharbor/registry-photon` | Registry OCI (Distribution) que almacena las imágenes |
| `registryctl` | `goharbor/harbor-registryctl` | Controla el registry (garbage collection) |
| `core` | `goharbor/harbor-core` | API, autenticación, token service, lógica de negocio |
| `portal` | `goharbor/harbor-portal` | UI web (Angular servida por nginx) |
| `jobservice` | `goharbor/harbor-jobservice` | Ejecuta trabajos asíncronos: replicación, GC, escaneos, retención |
| `trivy-adapter` | `goharbor/trivy-adapter-photon` | Escáner de vulnerabilidades |
| `aptly` | `generic_aptly` (se construye desde `aptly/`) | Repositorio Debian: API para subir y publicar paquetes `.deb` firmados con GPG |
| `proxy` | `goharbor/nginx-photon` | Punto de entrada HTTP/HTTPS. Sirve también `/debian/` (repositorio publicado) y `/aptly/api/` |
| `dtrack-db` | `postgres:16.15-alpine` | Base de datos de Dependency-Track (`dtrack`), independiente de la de Harbor |
| `dtrack-apiserver` | `dependencytrack/apiserver` | API de Dependency-Track: recibe SBOM, descarga las fuentes de vulnerabilidades y analiza |
| `dtrack-frontend` | `dependencytrack/frontend` | UI web de Dependency-Track (la API la llama el navegador, no el contenedor) |

Orden de arranque (controlado con `depends_on` + healthchecks):
`init` → `postgresql`, `redis`, `aptly` → `registry` → `core` → `jobservice`, `proxy`;
y en paralelo `init` → `dtrack-db` → `dtrack-apiserver` → `dtrack-frontend`.

Dependency-Track va en su propia red (`dtrack`), sin acceso a los servicios de Harbor, y
no pasa por el proxy: publica la API y la UI directamente en `127.0.0.1:8081` y
`127.0.0.1:8082`.

## Estructura de ficheros

```
harbor_docker/
├── docker-compose.yml          # definición de todos los servicios
├── .env.example                # plantilla de variables
├── .env                        # (generado, NO versionar) variables y secretos
├── generate-env.sh             # crea .env con secretos aleatorios
├── reset.sh                    # ⚠ borra todo el stack (contenedores, red, data/) para empezar de cero
├── scripts/init.sh             # lo ejecuta el servicio init en cada `up`
├── aptly/                      # imagen del repositorio Debian (Dockerfile + entrypoint.sh)
├── examples/
│   ├── publish-deb.sh          # ejemplo: genera un .deb y lo publica en aptly
│   ├── push-image.sh           # ejemplo: construye una imagen y la sube a Harbor
│   ├── upload-sbom.sh          # ejemplo: genera el SBOM de una imagen y lo sube a Dependency-Track
│   ├── compose_map.py          # genera un mapa HTML del compose con el estado de los contenedores
│   └── compose-map.sh          # lanzador de compose_map.py: generar, abrir o servir por HTTP
├── config/
│   ├── aptly/aptly.conf        # configuración de aptly (rootDir, firma gpg2)
│   ├── core/app.conf
│   ├── jobservice/config.yml   # workers, loggers de jobs, redis
│   ├── nginx/nginx.conf        # proxy: TLS, rutas, redirección HTTP→HTTPS
│   ├── portal/nginx.conf
│   ├── registry/config.yml     # almacenamiento, redis, purgado de uploads
│   └── registryctl/config.yml
├── data/                       # (generado, NO versionar) datos persistentes
│   ├── registry/               # blobs de las imágenes  (uid 10000)
│   ├── database/               # PostgreSQL             (uid 999)
│   ├── redis/                  #                        (uid 999)
│   ├── job_logs/  ca_download/  trivy-adapter/           (uid 10000)
│   ├── aptly/                  # BD, pool de paquetes y clave GPG de aptly (uid 10000)
│   │   ├── gpg/                # ⚠ clave privada de firma del repositorio Debian
│   │   └── public/             # repositorios publicados (servidos en /debian/)
│   ├── dtrack/
│   │   ├── database/           # PostgreSQL de Dependency-Track (uid 70)
│   │   └── apiserver/          # claves, índices y réplica de NVD (uid 1000; varios GB)
│   └── secret/
│       ├── cert/server.{crt,key}       # certificado TLS del proxy
│       ├── core/private_key.pem        # firma de tokens del registry
│       ├── registry/root.crt           # verificación de esos tokens
│       ├── registry/passwd             # htpasswd de harbor_registry_user
│       ├── keys/secretkey              # cifra credenciales guardadas en la BD
│       └── aptly/api.htpasswd          # usuario de la API de aptly (lo usa nginx)
├── LICENSE                     # licencia MIT del proyecto
├── NOTICE                      # atribución a Harbor de los ficheros derivados (Apache-2.0)
├── LICENSES/Apache-2.0.txt     # texto de la licencia Apache 2.0
├── CLAUDE.md                   # contexto del proyecto para Claude Code
└── .claude/skills/harbor-docker/SKILL.md   # skill de gestión de imágenes y contenedores
```

Ejemplo lanzar script:  APTLY_URL=https://localhost/aptly/api INSECURE=1 ./examples/publish-deb.sh

Para ver paquetes de una distribución
http://localhost/debian/dists/trixie/main/binary-amd64/Packages

HARBOR_URL=https://localhost./examples/push-image.sh  ./examples/push-image.sh 

## Variables de `.env`

| Variable | Descripción |
|---|---|
| `HARBOR_VERSION` | Tag de las imágenes `goharbor/*` (`v2.15.2`) |
| `HARBOR_HOSTNAME` | Nombre DNS o IP del servidor. Se usa para el certificado autofirmado |
| `HARBOR_EXTERNAL_URL` | URL pública (`https://host[:puerto]`). La usa core para tokens y enlaces |
| `HARBOR_HTTP_PORT` / `HARBOR_HTTPS_PORT` | Puertos publicados. Por defecto `127.0.0.1:80` / `127.0.0.1:443` (solo localhost). Sin IP escuchan en todas las interfaces |
| `HARBOR_DATA_DIR` | Directorio de datos persistentes (por defecto `./data`) |
| `LOG_LEVEL` | `debug`, `info`, `warning`, `error` |
| `HARBOR_ADMIN_PASSWORD` | Contraseña inicial de `admin` (**solo se aplica en el primer arranque**) |
| `DB_PASSWORD` | Contraseña de PostgreSQL (**fijada en el primer arranque**, ver [cambiarla](#cambiar-la-contraseña-de-la-base-de-datos)) |
| `REGISTRY_PASSWORD` | Credencial interna de core/jobservice hacia el registry |
| `CORE_SECRET`, `JOBSERVICE_SECRET` | Secretos compartidos entre componentes (16 caracteres) |
| `CSRF_KEY` | Clave CSRF de la UI (32 caracteres) |
| `ROBOT_SCANNER_NAME_PREFIX` | Prefijo de las cuentas robot internas del escáner |
| `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` | Proxy de salida para core, jobservice y trivy (opcional) |
| `TRIVY_GITHUB_TOKEN` | Token de GitHub para evitar límites al descargar la BD de Trivy (opcional) |
| `APTLY_API_USER` / `APTLY_API_PASSWORD` | Credenciales de la API de aptly (`/aptly/api/`). Se aplican en cada `up` |
| `APTLY_GPG_NAME` / `APTLY_GPG_EMAIL` | Identidad de la clave GPG de firma (**solo al generarla**, en el primer arranque) |
| `DTRACK_VERSION` | Tag de las imágenes `dependencytrack/*` (`4.14.5`) |
| `DTRACK_DB_PASSWORD` | Contraseña de la BD de Dependency-Track (**fijada en el primer arranque**) |
| `DTRACK_API_PORT` / `DTRACK_FRONTEND_PORT` | Puertos publicados de la API y la UI. Por defecto `127.0.0.1:8081` / `127.0.0.1:8082` |
| `DTRACK_API_BASE_URL` | URL de la API **vista desde el navegador** (por defecto `http://localhost:8081`) |

## Certificado TLS

En el primer arranque, `init` genera un certificado **autofirmado** (RSA 4096, 1 año,
SAN = `HARBOR_HOSTNAME`) en `data/secret/cert/`. Solo se genera si no existe.

**Usar un certificado propio** (de tu CA interna o público):

```bash
sudo cp fullchain.pem data/secret/cert/server.crt   # certificado + cadena intermedia
sudo cp privkey.pem   data/secret/cert/server.key
sudo chown 10000:10000 data/secret/cert/server.*
sudo chmod 600 data/secret/cert/server.key
docker compose restart proxy
```

Para **renovar el autofirmado**, borra los dos ficheros y ejecuta `docker compose up -d`
y luego `docker compose restart proxy`.

## Configurar los clientes Docker

Con la configuración por defecto (`https://localhost`, en el servidor o a través del túnel
SSH) no hace falta nada: Docker acepta sin verificar el certificado los registries que
resuelven a una dirección de loopback. Lo que sigue aplica cuando Harbor se publica con
un nombre de red.

Si usas el certificado autofirmado o de una CA interna, **cada host que haga
`docker pull/push`** debe confiar en él (no hace falta reiniciar Docker):

```bash
# en el cliente; añade :puerto al nombre del directorio si no usas el 443
sudo mkdir -p /etc/docker/certs.d/<HARBOR_HOSTNAME>
sudo scp <servidor>:<ruta>/harbor_docker/data/secret/cert/server.crt \
     /etc/docker/certs.d/<HARBOR_HOSTNAME>/ca.crt
```

Otros clientes:
- **containerd / Kubernetes**: añade la CA al almacén del sistema
  (`/usr/local/share/ca-certificates/` + `update-ca-certificates`) o configura
  `hosts.toml` de containerd.
- **Podman / Buildah / Skopeo**: `/etc/containers/certs.d/<HARBOR_HOSTNAME>/ca.crt`.

## Acceso desde otras máquinas (túnel SSH)

Por defecto el proxy solo escucha en `127.0.0.1:80` y `127.0.0.1:443`: ningún otro equipo
llega a Harbor directamente. Cada cliente abre un túnel SSH al servidor y usa la **misma
URL que el servidor, `https://localhost`**. Tiene que ser exactamente esa: es la que Harbor
devuelve a `docker` para pedir el token, y con otra el `docker login` falla.

```bash
# En cada cliente (sudo: los puertos locales < 1024 requieren root)
sudo ssh -N -o ExitOnForwardFailure=yes \
  -L 443:127.0.0.1:443 -L 80:127.0.0.1:80 \
  -L 8081:127.0.0.1:8081 -L 8082:127.0.0.1:8082 usuario@servidor
```

Con el túnel abierto, en el cliente:

| Uso | Cómo |
|---|---|
| UI web | `https://localhost` (el navegador avisa del certificado autofirmado) |
| Docker | `docker login localhost` · `docker push localhost/<proyecto>/<imagen>:<tag>` |
| apt | `deb [signed-by=/etc/apt/keyrings/aptly-repo.asc] http://localhost/debian trixie main` |
| Scripts de ejemplo | `HARBOR_URL=https://localhost` / `APTLY_URL=https://localhost/aptly/api` |
| Dependency-Track | UI `http://localhost:8082` (necesita también el túnel del 8081: el navegador llama a la API) |

Notas:
- Los puertos 80 y 443 del cliente tienen que estar libres. Para no usar `sudo`, se puede
  permitir abrir puertos bajos sin root: `sudo sysctl net.ipv4.ip_unprivileged_port_start=80`.
- Para un túnel permanente, usa `autossh` o una entrada en `~/.ssh/config` con
  `LocalForward 443 127.0.0.1:443`, `LocalForward 80 127.0.0.1:80` y `ExitOnForwardFailure yes`.
- Para dar acceso a alguien solo al túnel, sin shell en el servidor, restringe su clave en
  `~/.ssh/authorized_keys`:
  `restrict,port-forwarding,permitopen="127.0.0.1:443",permitopen="127.0.0.1:80",permitopen="127.0.0.1:8081",permitopen="127.0.0.1:8082" ssh-ed25519 AAAA...`
  y conecta con `ssh -N`.

Comprobar en el servidor que solo escucha en localhost:

```bash
ss -ltn | grep -E ':(80|443|8081|8082) '   # todos en 127.0.0.1 (no 0.0.0.0)
docker port nginx                  # 8080/tcp -> 127.0.0.1:80   8443/tcp -> 127.0.0.1:443
curl -sk --max-time 3 https://<IP de la LAN>/ || echo "OK: no accesible por la red"
```

## Uso básico

```bash
docker login <HARBOR_HOSTNAME>                      # usuario de Harbor o cuenta robot

docker tag  nginx:1.27 <HARBOR_HOSTNAME>/library/nginx:1.27
docker push <HARBOR_HOSTNAME>/library/nginx:1.27
docker pull <HARBOR_HOSTNAME>/library/nginx:1.27
```

- Las imágenes se organizan en **proyectos** (`<host>/<proyecto>/<repo>:<tag>`).
  El proyecto `library` existe por defecto y es público.
- Para CI/CD usa **cuentas robot** (Proyecto → Robot Accounts), no la cuenta `admin`.
- **Proxy cache**: crea un endpoint en *Registries* (p. ej. Docker Hub) y un proyecto
  de tipo proxy cache. Después: `docker pull <host>/<proyecto-proxy>/library/nginx`.
- **Escaneo**: cada artefacto se puede escanear desde la UI o la API. Activa
  "Automatically scan images on push" en la configuración del proyecto.

Las recetas detalladas (API REST, robots, limpieza, GC, etc.) están en
[`.claude/skills/harbor-docker/SKILL.md`](.claude/skills/harbor-docker/SKILL.md).

### Script de ejemplo: construir y subir una imagen

[`examples/push-image.sh`](examples/push-image.sh) construye una imagen (de ejemplo o el
`Dockerfile` del directorio indicado), crea el proyecto si no existe, sube `:<tag>` y
`:latest` y comprueba el artefacto por API. El `docker login` se hace con un
`DOCKER_CONFIG` temporal, así que la credencial no se guarda en el equipo.

```bash
HARBOR_USER='robot$demo+ci' HARBOR_PASSWORD=<secreto> PROJECT=demo IMAGE=mi-app TAG=1.0 \
  ./examples/push-image.sh ./mi-app
SCAN=1 ./examples/push-image.sh                                   # y escanear con Trivy
CA_CERT=server.crt PLATFORMS=linux/amd64,linux/arm64 ./examples/push-image.sh   # multi-arquitectura
```

- Sin `HARBOR_USER` usa `admin` y `HARBOR_ADMIN_PASSWORD` de `.env` (avisa de ello).
- Crear proyectos requiere un usuario con permiso o un robot **de sistema**; un robot de
  proyecto solo puede subir a proyectos existentes.
- `PLATFORMS` crea un builder buildx `docker-container` temporal; necesita QEMU/binfmt
  para emular otras arquitecturas y no admite `INSECURE=1` (usa `CA_CERT`).

## Repositorio Debian (aptly)

El servicio `aptly` aloja repositorios Debian propios. En el primer arranque genera una
clave GPG (RSA 4096, sin passphrase, en `data/aptly/gpg/`) con la que firma todo lo que
publica, y exporta la clave pública a `/debian/repo-key.asc`.

| URL | Acceso | Uso |
|---|---|---|
| `https://<host>/debian/` y `http://<host>/debian/` | Público, solo lectura | Clientes `apt` |
| `https://<host>/aptly/api/` | Autenticación básica (`APTLY_API_USER`/`APTLY_API_PASSWORD`) | Subir paquetes, crear repos, publicar |

`/debian/` se sirve también por HTTP (sin redirigir a HTTPS) porque `apt` verifica la
firma GPG de cada índice y paquete; así los clientes no necesitan confiar en el
certificado autofirmado. Si prefieres solo HTTPS, quita ese `location` del servidor
`listen 8080` en `config/nginx/nginx.conf`.

### Publicar paquetes (API)

```bash
A=https://harbor.midominio.local/aptly/api
AUTH="aptly:<APTLY_API_PASSWORD>"

# 1. Crear un repositorio local (una vez por distribución)
curl -sk -u $AUTH -X POST -H 'Content-Type: application/json' \
  -d '{"Name":"local-trixie","DefaultDistribution":"trixie","DefaultComponent":"main"}' $A/repos

# 2. Subir uno o varios .deb a un directorio temporal y añadirlos al repo
curl -sk -u $AUTH -X POST -F file=@mipaquete_1.0.0_amd64.deb $A/files/subida1
curl -sk -u $AUTH -X POST $A/repos/local-trixie/file/subida1   # el directorio se borra al importarlo

# 3a. Primera publicación (firma con la clave del repositorio)
curl -sk -u $AUTH -X POST -H 'Content-Type: application/json' \
  -d '{"SourceKind":"local","Sources":[{"Name":"local-trixie"}],"Distribution":"trixie",
       "Architectures":["amd64","arm64","all"],"Signing":{"Batch":true}}' $A/publish/:.

# 3b. Siguientes veces: actualizar la publicación tras añadir paquetes
curl -sk -u $AUTH -X PUT -H 'Content-Type: application/json' \
  -d '{"Signing":{"Batch":true}}' $A/publish/:./trixie
```

Las arquitecturas se fijan en la primera publicación; incluye todas las que vayas a usar.
Otras operaciones: `GET $A/repos`, `GET $A/repos/<repo>/packages`,
`DELETE $A/repos/<repo>/packages` (cuerpo `{"PackageRefs":[...]}`), snapshots y mirrors
(ver la [documentación de la API](https://www.aptly.info/doc/api/)).

También se puede usar la CLI dentro del contenedor:

```bash
docker compose exec aptly aptly repo list
docker compose exec aptly aptly repo show -with-packages local-trixie
docker compose exec aptly aptly publish list
```

### Configurar los clientes apt

```bash
sudo install -d /etc/apt/keyrings
curl -fsSL http://harbor.midominio.local/debian/repo-key.asc | sudo tee /etc/apt/keyrings/aptly-repo.asc >/dev/null
echo "deb [signed-by=/etc/apt/keyrings/aptly-repo.asc] http://harbor.midominio.local/debian trixie main" \
  | sudo tee /etc/apt/sources.list.d/aptly-repo.list
sudo apt-get update && sudo apt-get install mipaquete
```

Con HTTPS y certificado autofirmado, instala antes `data/secret/cert/server.crt` en
`/usr/local/share/ca-certificates/harbor.crt` y ejecuta `sudo update-ca-certificates`.

### Script de ejemplo

[`examples/publish-deb.sh`](examples/publish-deb.sh) hace todo el ciclo: genera un paquete
de ejemplo (o usa los `.deb` que le pases), crea el repositorio si no existe, sube,
importa y publica (o actualiza la publicación). Lee `HARBOR_EXTERNAL_URL` y
`APTLY_API_PASSWORD` de `.env` si no se definen en el entorno.

```bash
CA_CERT=server.crt ./examples/publish-deb.sh                      # paquete de ejemplo
CA_CERT=server.crt DIST=bookworm ./examples/publish-deb.sh dist/*.deb
```

Otras variables: `REPO`, `COMPONENT`, `ARCHS`, `PKG_NAME`, `PKG_VERSION`, `INSECURE=1`
(ver la cabecera del script).

La imagen `generic_aptly` se construye en local desde `aptly/` (no existe en
ningún registry). Tiene `pull_policy: build`: `docker compose pull` la omite y `up` la
construye si hace falta. Para actualizarla con los últimos parches de Debian o tras
cambiar `aptly/`: `docker compose build --pull aptly && docker compose up -d aptly`.

## Dependency-Track

[Dependency-Track](https://dependencytrack.org) recibe SBOM (CycloneDX) de cada proyecto y
avisa de las vulnerabilidades conocidas de sus dependencias. Es independiente de Harbor
(BD, red y usuarios propios).

- UI: `http://localhost:8082` (en el servidor o por el [túnel](#acceso-desde-otras-máquinas-túnel-ssh)).
- API: `http://localhost:8081/api/v1/`.
- Primer acceso: usuario `admin`, contraseña `admin`; obliga a cambiarla en el primer login.
- Tras el primer arranque descarga las fuentes de vulnerabilidades (la réplica de NVD tarda
  un rato y ocupa varios GB en `data/dtrack/apiserver`). Las primeras auditorías pueden
  salir vacías hasta que termine.

### Script de ejemplo: subir un SBOM

[`examples/upload-sbom.sh`](examples/upload-sbom.sh) genera con Trivy el SBOM de una imagen
(o usa un SBOM CycloneDX que ya tengas), lo sube a Dependency-Track creando el proyecto si
no existe, espera a que se analice y muestra las vulnerabilidades por severidad. Si no hay
`trivy` instalado, lo ejecuta en un contenedor (`aquasec/trivy:0.74.0`) que lee la imagen del
Docker local.

Para CI, crea un equipo en Administración → Access Management → Teams con los permisos
`BOM_UPLOAD`, `PROJECT_CREATION_UPLOAD`, `VIEW_PORTFOLIO` y `VIEW_VULNERABILITY`, y genera
una clave de API en él.

```bash
export DTRACK_API_KEY=odt_...             # o DTRACK_USER=admin DTRACK_PASSWORD=...
./examples/upload-sbom.sh                                   # SBOM de ejemplo (log4j 2.14.1, ...)
./examples/upload-sbom.sh localhost/demo/hola-harbor:1.0    # imagen de Harbor: proyecto demo/hola-harbor, versión 1.0
./examples/upload-sbom.sh bom.json                          # SBOM existente
FAIL_ON=HIGH ./examples/upload-sbom.sh localhost/demo/app:2.3   # código de salida 2 si hay HIGH o CRITICAL
```

```
Dependency-Track 4.14.5 en http://localhost:8081
SBOM: 3 componentes → proyecto ejemplo-sbom 1.0
Procesando.
Vulnerabilidades: 10 (high 4, medium 6)
  HIGH       CVE-2026-28387         openssl 1.1.1k
  HIGH       CVE-2026-28388         openssl 1.1.1k
  ...
  MEDIUM     CVE-2026-34477         log4j-core 2.14.1
  ...
Proyecto: http://localhost:8082/projects/21dd96e3-...
```

En una instalación nueva el resultado sale vacío hasta que termina la primera descarga de NVD.

Con `curl` directamente:

```bash
# SBOM de una imagen de Harbor generado con Trivy (también vale syft, cdxgen...)
trivy image --format cyclonedx -o bom.json localhost/<proyecto>/<imagen>:<tag>

curl -s -X POST http://localhost:8081/api/v1/bom \
  -H "X-Api-Key: $DTRACK_API_KEY" \
  -F autoCreate=true -F projectName=<imagen> -F projectVersion=<tag> -F bom=@bom.json
```

Notas:
- `DTRACK_API_BASE_URL` es la URL con la que el **navegador** llega a la API. Si cambias
  `DTRACK_API_PORT` o abres el servicio a la red, actualízala.
- Proxy de salida: Dependency-Track no usa `HTTP_PROXY`; configúralo con las variables
  `ALPINE_HTTP_PROXY_ADDRESS`, `ALPINE_HTTP_PROXY_PORT` y `ALPINE_NO_PROXY` en `dtrack-apiserver`.
- Actualizar: cambia `DTRACK_VERSION` en `.env` (lee antes las release notes; la 5.x cambia
  la arquitectura y la configuración) y `docker compose up -d --wait`. El apiserver migra
  el esquema de la BD al arrancar.

## Operación del stack

**Mapa del stack**: [`examples/compose_map.py`](examples/compose_map.py) (Python 3, sin
dependencias) genera una página HTML con los servicios, los puertos publicados, las
conexiones entre servicios (deducidas de las variables de entorno, los ficheros de
configuración montados y `depends_on`) y el estado de cada contenedor. Al pasar el ratón
por un servicio, una conexión, una red o una fila de tabla muestra un resumen (estado,
último healthcheck, CPU/memoria, IP y alias de red, puertos) y resalta sus conexiones; al
hacer clic fija la ficha completa (imagen, comando, capacidades, montajes, dependencias...).
No escribe los valores de las variables de entorno y redacta credenciales en comandos.

```bash
python3 examples/compose_map.py --context default -o mapa.html            # una vez
python3 examples/compose_map.py --context default --refresh 30 --lang es  # se actualiza cada 30 s
```

O con el lanzador [`examples/compose-map.sh`](examples/compose-map.sh), que usa el contexto
`default` aunque Docker Desktop haya cambiado el activo:

```bash
./examples/compose-map.sh --open              # genera compose-map.html y lo abre
./examples/compose-map.sh --serve 8090        # http://127.0.0.1:8090/ (solo la página), regenerado cada 30 s
```


```bash
docker compose up -d --wait           # arrancar (ejecuta también init)
docker compose ps                     # estado y health de cada servicio
docker compose logs -f core           # logs de un servicio (core, registry, jobservice...)
docker compose logs --since 10m       # logs recientes de todo el stack
docker compose restart proxy          # reiniciar un servicio
docker compose stop                   # parar sin borrar contenedores
docker compose down                   # parar y borrar contenedores y red (los datos en data/ se conservan)
docker stats $(docker compose ps -q)  # consumo de CPU/RAM
```

Logs: driver `json-file` con rotación (5 ficheros de 50 MB por contenedor).

## Copias de seguridad y restauración

Lo imprescindible es **`data/` completo y `.env`**: sin `data/secret/` y los secretos
de `.env` no se pueden descifrar las credenciales guardadas en la BD. Sin `data/aptly/gpg/`
los clientes `apt` dejarían de confiar en el repositorio Debian (habría que repartir una
clave nueva).

**Backup en frío** (consistente, con unos minutos de parada):

```bash
docker compose stop
sudo tar --numeric-owner -czpf /backups/harbor-$(date +%F).tgz data .env
docker compose start
```

**Backup de la base de datos en caliente** (complemento, no sustituye a `data/registry`):

```bash
docker exec harbor-db pg_dump -U postgres registry | gzip > /backups/harbor-db-$(date +%F).sql.gz
docker exec dtrack-db pg_dump -U dtrack dtrack | gzip > /backups/dtrack-db-$(date +%F).sql.gz
```

**Restauración**:

```bash
docker compose down
sudo rm -rf data
sudo tar --numeric-owner -xzpf /backups/harbor-AAAA-MM-DD.tgz   # restaura data/ y .env
docker compose up -d --wait
```

`--numeric-owner` y `-p` conservan los UID (10000, 999, 70, 1000) que necesitan los contenedores.

### Empezar de cero

[`reset.sh`](reset.sh) borra todo lo que crea el compose: contenedores, redes y el directorio
de datos (imágenes, BD de Harbor, repositorio Debian y su clave GPG, Dependency-Track,
secretos y certificado). Pide escribir `BORRAR` para confirmar.

```bash
./reset.sh --backup ~/harbor-$(date +%F).tgz     # copia previa de data/ y .env, y borra
./reset.sh --env --up                            # secretos nuevos y arranca de nuevo
./reset.sh --images --yes                        # también las imágenes; sin preguntar
```

- `--env` solo renueva los secretos (las variables vacías en `.env.example`): conserva
  hostname, puertos y demás ajustes. Sin `--env`, un `.env` ya usado sirve igual para una
  instalación nueva: `HARBOR_ADMIN_PASSWORD` y `DB_PASSWORD` se aplican en el primer arranque.
- La copia se restaura con `sudo tar --numeric-owner -xzpf <copia>.tgz` en este directorio.
- Después hay que repartir de nuevo el certificado TLS a los clientes Docker y la clave GPG
  a los clientes apt; Dependency-Track vuelve a `admin`/`admin`.

> **No borres `.env` a mano en una instalación en uso** (ni lo recrees con `generate-env.sh`):
> la BD conserva la contraseña antigua y `core` no arranca (`password authentication failed`).
> Ver [Cambiar la contraseña de la base de datos](#cambiar-la-contraseña-de-la-base-de-datos).

## Actualizar Harbor

1. Lee las release notes y la
   [guía oficial de actualización](https://goharbor.io/docs/main/administration/upgrade/).
   Revisa desde qué versiones se puede actualizar directamente.
2. Haz un [backup en frío](#copias-de-seguridad-y-restauración).
3. Compara las plantillas de configuración de la nueva versión con `config/`:

   ```bash
   NEW=v2.16.0   # ejemplo
   id=$(docker create goharbor/prepare:$NEW)
   docker cp $id:/usr/src/app/templates /tmp/harbor-templates-$NEW
   docker rm $id
   # revisa sobre todo docker_compose/, core/env.jinja, jobservice/, registry/ y nginx/
   ```

   Aplica a `docker-compose.yml` y `config/` los cambios relevantes (variables nuevas,
   imágenes renombradas como `redis-photon` → `valkey-photon`, etc.).
4. Cambia `HARBOR_VERSION` en `.env` y despliega:

   ```bash
   docker compose pull                    # aptly se omite: es una imagen local
   docker compose build --pull aptly      # actualiza la base debian y aptly
   docker compose up -d --wait
   ```

   `core` aplica las migraciones del esquema de la base de datos al arrancar.
5. Comprueba `/api/v2.0/health`, `/api/v2.0/systeminfo` y haz un pull/push de prueba.

## Cambios de configuración habituales

### Cambiar hostname o URL pública

```bash
vi .env                                   # HARBOR_HOSTNAME y HARBOR_EXTERNAL_URL
sudo rm data/secret/cert/server.*         # solo si usas el autofirmado
docker compose up -d --wait               # init regenera el certificado; core se recrea
docker compose restart proxy              # recarga el nuevo certificado
```

### Puerto HTTPS distinto de 443

1. `HARBOR_HTTPS_PORT=8443` y `HARBOR_EXTERNAL_URL=https://<host>:8443` en `.env`.
2. En `config/nginx/nginx.conf`, cambia la redirección a `return 308 https://$host:8443$request_uri;`.
3. `docker compose up -d --wait && docker compose restart proxy`.
4. En los clientes Docker, el directorio pasa a ser `/etc/docker/certs.d/<host>:8443/`.

### Detrás de otro proxy inverso o balanceador con TLS

Si el proxy externo envía `X-Forwarded-Proto: https`, el nginx de Harbor lo respeta.
Lo más sencillo es que el proxy externo apunte al puerto HTTPS de Harbor. Para recibir HTTP
plano en el puerto 8080 del contenedor, sustituye en `config/nginx/nginx.conf` el bloque de
redirección por el mismo bloque `location` del servidor 8443 sin TLS.

### Cambiar la contraseña de la base de datos

`POSTGRES_PASSWORD` solo se usa al inicializar la BD. Para cambiarla después:

```bash
docker exec -it harbor-db psql -U postgres -c "ALTER USER postgres PASSWORD 'nueva'"
# poner la misma en DB_PASSWORD de .env
docker compose up -d --wait
```

### Rotar `REGISTRY_PASSWORD`

Cámbiala en `.env` y ejecuta `docker compose up -d --force-recreate --wait`.
`init` regenera el htpasswd en cada arranque.

### Ajustar workers de jobs, logs o purgado de uploads

- `config/jobservice/config.yml` → `workers`, `job_loggers`, `sweeper.duration`.
- `config/registry/config.yml` → `maintenance.uploadpurging`.
- Tras editar: `docker compose restart jobservice` o `docker compose restart registry registryctl`.

### Trivy sin acceso a internet

En el servicio `trivy-adapter`, pon `SCANNER_TRIVY_SKIP_UPDATE: "true"` y
`SCANNER_TRIVY_OFFLINE_SCAN: "true"`, y copia manualmente la BD de Trivy en
`data/trivy-adapter/trivy/db/`.

## Solución de problemas

| Síntoma | Causa probable / solución |
|---|---|
| `x509: certificate signed by unknown authority` en `docker login/push` | El cliente no confía en el certificado: ver [Configurar los clientes Docker](#configurar-los-clientes-docker) |
| `unauthorized: unauthorized to access repository` en push | El proyecto no existe o el usuario/robot no tiene permiso de push en él |
| Push de capas grandes cortado / `413` | Revisa proxies intermedios; el nginx de Harbor tiene `client_max_body_size 0` |
| `core` no llega a healthy | `docker compose logs core`: suele ser la contraseña de BD (`DB_PASSWORD` cambiada después del primer arranque, p. ej. al regenerar `.env`): ver [cambiarla](#cambiar-la-contraseña-de-la-base-de-datos) |
| `permission denied` en logs de registry/jobservice/trivy | Permisos de `data/` alterados (p. ej. tras copiar a mano): `docker compose up -d` vuelve a ejecutar `init`, que los corrige |
| `bind: address already in use` | Los puertos 80/443 están ocupados: cambia `HARBOR_HTTP_PORT`/`HARBOR_HTTPS_PORT` |
| La redirección HTTP lleva al puerto incorrecto | Ver [Puerto HTTPS distinto de 443](#puerto-https-distinto-de-443) |
| Escaneos en `Error` | `docker compose logs trivy-adapter`: normalmente no puede descargar la BD desde `ghcr.io` (proxy, firewall o límite de peticiones → `TRIVY_GITHUB_TOKEN`) |
| El disco no se libera tras borrar imágenes | Hace falta un **Garbage Collection** (Administración → Clean Up, o la API) |
| `.env` falta o tiene secretos vacíos | `docker compose` falla con `ejecuta ./generate-env.sh` |
| La UI de Dependency-Track carga pero no inicia sesión | El navegador no llega a `DTRACK_API_BASE_URL`: abre también el túnel del 8081 o corrige la URL |
| `dtrack-apiserver` se reinicia (`OOMKilled`) | Sube el límite `memory` del servicio (6 GB) o libera RAM en el host |

Diagnóstico rápido:

```bash
docker compose ps
curl -sk https://<HARBOR_HOSTNAME>/api/v2.0/health     # estado por componente
docker compose logs --since 15m | grep -iE 'error|fatal|panic'
```

**`pull access denied for generic_aptly`**: algo intenta descargar la imagen de aptly
de Docker Hub, donde no existe (`docker compose pull` sin `pull_policy: build`, o
`docker compose up --pull always`). Constrúyela con `docker compose build aptly`. Si usas
otro contexto de Docker (p. ej. `desktop-linux`), la imagen hay que construirla en ese
motor: `docker context show`.

## Seguridad

- **No versiones** `.env` ni `data/` (incluidos en `.gitignore`).
- `.env` se crea con permisos `600`. Restringe también el acceso a `data/secret/`.
- Cambia la contraseña de `admin` y crea usuarios o integra LDAP/OIDC
  (Administración → Configuración → Autenticación).
- Usa cuentas robot con permisos mínimos y caducidad para CI/CD.
- Entra cuanto antes en Dependency-Track: hasta el primer login, `admin`/`admin` funciona
  para cualquiera que llegue al puerto 8081.
- Activa el escaneo automático en push y, si procede, impide el pull de imágenes con
  vulnerabilidades graves (configuración del proyecto).
- La clave privada GPG de aptly (`data/aptly/gpg/`) no tiene passphrase: quien la
  obtenga puede firmar paquetes que los clientes aceptarán. Protégela como un secreto.
- Por defecto los puertos solo escuchan en `127.0.0.1` (acceso por túnel SSH). Con Docker
  Engine < 28, comprueba con el stack en marcha que `route_localnet` sigue a 0
  (`grep -l 1 /proc/sys/net/ipv4/conf/*/route_localnet` no debe devolver nada). Si alguna
  interfaz lo tiene a 1, un equipo de la misma red podría llegar a esos puertos enviando
  paquetes a 127.0.0.1; actualiza Docker o bloquéalo con
  `sudo iptables -t raw -I PREROUTING ! -i lo -d 127.0.0.0/8 -j DROP`.
- Todos los contenedores usan `cap_drop: ALL` y solo añaden las capacidades mínimas
  del compose oficial.

## Diferencias con el instalador oficial

| Instalador oficial | Este despliegue |
|---|---|
| `harbor.yml` + `prepare` generan compose y configuración | Configuración ya renderizada en `config/` y variables en `.env` |
| Contenedor `harbor-log` con rsyslog y driver `syslog` | Driver `json-file` con rotación; `docker compose logs` |
| `depends_on` simple: jobservice se reinicia varias veces hasta que core responde | `depends_on` con `service_healthy`: arranque ordenado sin reinicios |
| HTTPS opcional con certificado aportado por el usuario | HTTPS siempre activo; autofirmado si no se aporta uno |
| Opciones de TLS interno, métricas, tracing y BD/Redis externos | No incluidas (se pueden añadir siguiendo las plantillas oficiales) |

## Licencia

Copyright (c) 2026 Juan Maria Gomez Lopez <juan-maria.gomez-lopez@tutamail.com>.

El proyecto se distribuye bajo la [licencia MIT](LICENSE). Excepción: `docker-compose.yml`
y los ficheros de `config/` de Harbor (`core`, `jobservice`, `nginx`, `portal`, `registry`,
`registryctl`) derivan de las plantillas oficiales de Harbor (© Project Harbor Authors) y
siguen bajo la [licencia Apache 2.0](LICENSES/Apache-2.0.txt), con las modificaciones
indicadas en [NOTICE](NOTICE). Cada fichero lleva su cabecera `SPDX-License-Identifier`.

