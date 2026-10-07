#!/usr/bin/env python3
# Copyright (c) 2026 Juan Maria Gomez Lopez <juan-maria.gomez-lopez@tutamail.com>
# SPDX-License-Identifier: MIT

"""Genera una página HTML con el mapa de un proyecto Docker Compose.

Dibuja los servicios (con icono, contenedor y puertos), los puertos publicados en el
host y las conexiones entre servicios, y muestra el estado real de cada contenedor
(running/healthy/exited, reinicios, tiempo en marcha) consultando a Docker.

Las conexiones se deducen automáticamente de:
  - variables de entorno que apuntan a otro servicio (http://core:8080, redis://redis:6379/2,
    POSTGRESQL_HOST=postgresql...)
  - ficheros de configuración montados en solo lectura (host:puerto, URLs y, en ficheros
    de nginx, las rutas `location` que hacen `proxy_pass` a un `upstream`)
  - `depends_on` (salvo `service_completed_successfully`, que es solo orden de arranque)

Los valores de las variables de entorno (contraseñas incluidas) se usan solo para buscar
referencias: nunca se escriben en la página.

Requisitos: Python >= 3.8 y el cliente docker con el plugin compose (sin librerías extra).

Uso:
  ./compose_map.py                                  # docker-compose.yml del directorio actual
  ./compose_map.py -f ../docker-compose.yml -o mapa.html
  ./compose_map.py --context default --lang es      # motor Docker y idioma de la página
  ./compose_map.py --refresh 30                     # regenera cada 30 s (la página se recarga sola)
"""
import argparse
import datetime as dt
import html
import json
import re
import string
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Textos de la página
# ---------------------------------------------------------------------------
TEXT = {
    "en": {
        "title": "{name} stack map",
        "intro": "Services in the compose file, the ports published on the host and how the "
                 "services call each other, with the live state of each container.",
        "outside": "Outside", "clients": "clients",
        "file": "Compose file", "context": "Docker context", "generated": "Generated",
        "no_net": "no network / no connections",
        "caption": "Arrows point from the caller to the service that answers. Dashed arrows "
                   "come only from depends_on. Connections are inferred from environment "
                   "variables and mounted configuration files.",
        "lg_port": "port published on the host", "lg_ext": "external traffic",
        "lg_call": "detected call", "lg_dep": "depends_on only",
        "exposed": "Ports exposed to the outside", "services": "Services",
        "conns": "Detected connections",
        "h_host": "Host", "h_target": "Service:port", "h_service": "Service",
        "h_container": "Container", "h_image": "Image", "h_state": "State",
        "h_restarts": "Restarts", "h_up": "Up for", "h_ports": "Internal ports",
        "h_from": "From", "h_to": "To", "h_via": "Labels", "h_src": "Detected in",
        "none_exposed": "No service publishes ports.",
        "st_healthy": "healthy", "st_running": "running", "st_starting": "starting",
        "st_unhealthy": "unhealthy", "st_restarting": "restarting", "st_done": "completed",
        "st_exited": "exited ({code})", "st_missing": "not created", "st_unknown": "unknown",
        "sum_ok": "running", "sum_warn": "need attention", "sum_bad": "stopped / failed",
        "sum_off": "not created",
        "src_env": "environment", "src_file": "config file", "src_nginx": "nginx route",
        "src_dep": "depends_on", "src_ports": "ports",
        "docker_err": "Could not query Docker: states are unknown.",
        "networks": "Networks", "details": "Details",
        "details_hint": "Hover over any element for a summary and its connections; click it to pin all its details here.",
        "click_more": "Click to see all details",
        "h_cpu": "CPU", "h_mem": "Memory", "h_driver": "Driver", "h_subnet": "Subnet", "h_gateway": "Gateway",
        "h_members": "Containers",
        "sec_state": "State", "sec_res": "Resources", "sec_container": "Container", "sec_net": "Networks",
        "sec_ports": "Ports", "sec_mounts": "Mounts", "sec_env": "Environment", "sec_deps": "Dependencies",
        "sec_conn": "Connection", "sec_pub": "Published ports", "sec_net_cfg": "Configuration",
        "k_status": "Status", "k_health": "Health", "k_last_check": "Last healthcheck", "k_restarts": "Restarts",
        "k_started": "Started", "k_uptime": "Up for", "k_exit": "Exit code", "k_oom": "OOM killed",
        "k_id": "ID", "k_image": "Image", "k_image_id": "Image ID", "k_image_size": "Image size",
        "k_created": "Created", "k_user": "User", "k_cmd": "Command", "k_hc": "Healthcheck",
        "k_restart": "Restart policy", "k_cap_add": "Caps added", "k_cap_drop": "Caps dropped",
        "k_ro": "Read-only rootfs", "k_log": "Logging", "k_cpu": "CPU", "k_mem": "Memory",
        "k_netio": "Network I/O", "k_blkio": "Disk I/O", "k_pids": "PIDs", "k_internal": "Internal",
        "k_published": "Published", "k_vars": "{n} variables (values hidden)", "k_calls": "Calls",
        "k_called_by": "Called by", "k_depends": "depends_on", "k_from": "From", "k_to": "To",
        "k_labels": "Labels", "k_src": "Detected in", "k_both": "Both directions", "k_driver": "Driver",
        "k_subnet": "Subnet", "k_gateway": "Gateway", "k_internal_net": "Internal", "k_scope": "Scope",
        "k_ip": "IP", "k_aliases": "aliases", "yes": "yes", "no": "no", "out_sub": "clients outside the host",
    },
    "es": {
        "title": "Mapa del stack {name}",
        "intro": "Servicios del compose, puertos publicados en el host y cómo se llaman entre "
                 "sí, con el estado real de cada contenedor.",
        "outside": "Exterior", "clients": "clientes",
        "file": "Fichero compose", "context": "Contexto Docker", "generated": "Generado",
        "no_net": "sin red / sin conexiones",
        "caption": "Las flechas van del servicio que llama al que responde. Las discontinuas "
                   "salen solo de depends_on. Las conexiones se deducen de las variables de "
                   "entorno y de los ficheros de configuración montados.",
        "lg_port": "puerto publicado en el host", "lg_ext": "tráfico externo",
        "lg_call": "llamada detectada", "lg_dep": "solo depends_on",
        "exposed": "Puertos expuestos al exterior", "services": "Servicios",
        "conns": "Conexiones detectadas",
        "h_host": "Host", "h_target": "Servicio:puerto", "h_service": "Servicio",
        "h_container": "Contenedor", "h_image": "Imagen", "h_state": "Estado",
        "h_restarts": "Reinicios", "h_up": "En marcha", "h_ports": "Puertos internos",
        "h_from": "Origen", "h_to": "Destino", "h_via": "Etiquetas", "h_src": "Detectado en",
        "none_exposed": "Ningún servicio publica puertos.",
        "st_healthy": "healthy", "st_running": "running", "st_starting": "arrancando",
        "st_unhealthy": "unhealthy", "st_restarting": "reiniciando", "st_done": "completado",
        "st_exited": "parado ({code})", "st_missing": "no creado", "st_unknown": "desconocido",
        "sum_ok": "en marcha", "sum_warn": "requieren atención", "sum_bad": "parados / con error",
        "sum_off": "no creados",
        "src_env": "entorno", "src_file": "fichero de config", "src_nginx": "ruta nginx",
        "src_dep": "depends_on", "src_ports": "ports",
        "docker_err": "No se pudo consultar Docker: el estado es desconocido.",
        "networks": "Redes", "details": "Detalles",
        "details_hint": "Pasa el ratón por cualquier elemento para ver un resumen y sus conexiones; haz clic para fijar aquí todos sus detalles.",
        "click_more": "Clic para ver todos los detalles",
        "h_cpu": "CPU", "h_mem": "Memoria", "h_driver": "Driver", "h_subnet": "Subred", "h_gateway": "Gateway",
        "h_members": "Contenedores",
        "sec_state": "Estado", "sec_res": "Recursos", "sec_container": "Contenedor", "sec_net": "Redes",
        "sec_ports": "Puertos", "sec_mounts": "Montajes", "sec_env": "Entorno", "sec_deps": "Dependencias",
        "sec_conn": "Conexión", "sec_pub": "Puertos publicados", "sec_net_cfg": "Configuración",
        "k_status": "Estado", "k_health": "Salud", "k_last_check": "Último healthcheck", "k_restarts": "Reinicios",
        "k_started": "Iniciado", "k_uptime": "En marcha", "k_exit": "Código de salida", "k_oom": "Matado por OOM",
        "k_id": "ID", "k_image": "Imagen", "k_image_id": "ID de imagen", "k_image_size": "Tamaño de imagen",
        "k_created": "Creado", "k_user": "Usuario", "k_cmd": "Comando", "k_hc": "Healthcheck",
        "k_restart": "Política de reinicio", "k_cap_add": "Capacidades añadidas", "k_cap_drop": "Capacidades quitadas",
        "k_ro": "Rootfs de solo lectura", "k_log": "Logs", "k_cpu": "CPU", "k_mem": "Memoria",
        "k_netio": "E/S de red", "k_blkio": "E/S de disco", "k_pids": "PIDs", "k_internal": "Internos",
        "k_published": "Publicados", "k_vars": "{n} variables (valores ocultos)", "k_calls": "Llama a",
        "k_called_by": "Llamado por", "k_depends": "depends_on", "k_from": "Origen", "k_to": "Destino",
        "k_labels": "Etiquetas", "k_src": "Detectado en", "k_both": "En los dos sentidos", "k_driver": "Driver",
        "k_subnet": "Subred", "k_gateway": "Gateway", "k_internal_net": "Interna", "k_scope": "Ámbito",
        "k_ip": "IP", "k_aliases": "alias", "yes": "sí", "no": "no", "out_sub": "clientes fuera del host",
    },
}

# ---------------------------------------------------------------------------
# Iconos (trazos en una caja de 24x24). El orden importa: gana la primera coincidencia
# con el nombre del servicio o de la imagen.
# ---------------------------------------------------------------------------
ICONS = [
    (("nginx", "proxy", "traefik", "haproxy", "caddy", "envoy"),
     '<path d="M3 12h6M9 12l4-6h7M9 12l4 6h7M9 12h11"/>'),
    (("registryctl",),
     '<path d="M20 12a8 8 0 1 1-2.3-5.6"/><path d="M20 4v4h-4"/><path d="M9.5 9.5l5 5M14.5 9.5l-5 5"/>'),
    (("registry",),
     '<path d="M12 3l9 4.5-9 4.5-9-4.5z"/><path d="M3 12l9 4.5 9-4.5M3 16.5L12 21l9-4.5"/>'),
    (("postgres", "mysql", "mariadb", "mongo", "-db", "database"),
     '<ellipse cx="12" cy="5.5" rx="7" ry="2.5"/><path d="M5 5.5v13c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5v-13M5 12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5"/>'),
    (("redis", "valkey", "memcache", "keydb"),
     '<path d="M13.5 2.5L5 13.5h6l-1.5 8 8.5-11h-6z"/>'),
    (("trivy", "scan", "clair"),
     '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5L21 21M8 10.5h5M10.5 8v5"/>'),
    (("job", "worker", "queue", "celery", "cron"),
     '<rect x="3" y="4" width="18" height="4" rx="1"/><rect x="3" y="10" width="18" height="4" rx="1"/><rect x="3" y="16" width="11" height="4" rx="1"/><path d="M17 18l1.5 1.5L21 17"/>'),
    (("aptly", "apt", "deb", "pypi", "nexus", "artifactory"),
     '<path d="M4 8l8-4 8 4v9l-8 4-8-4z"/><path d="M12 9v7M9 13l3 3 3-3"/>'),
    (("portal", "frontend", "ui", "web"),
     '<rect x="3" y="4" width="18" height="12" rx="1.5"/><path d="M9 20h6M12 16v4"/>'),
    (("init", "prepare", "setup", "migrat"),
     '<path d="M12 3v8"/><path d="M6.3 6.8a7.5 7.5 0 1 0 11.4 0"/>'),
    (("core", "api", "backend", "server"),
     '<path d="M12 2.5l8.2 4.75v9.5L12 21.5l-8.2-4.75v-9.5z"/><circle cx="12" cy="12" r="3.2"/>'),
]
ICON_DEFAULT = '<path d="M12 2.5l8.5 4.5v10L12 21.5 3.5 17V7z"/><path d="M3.5 7L12 11.5 20.5 7M12 11.5v10"/>'
ICON_OUTSIDE = '<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>'

CONFIG_EXT = {".conf", ".cfg", ".ini", ".yml", ".yaml", ".json", ".toml", ".env", ".properties", ".xml"}
MAX_CONFIG_BYTES = 512 * 1024
HOST_KEY = re.compile(r"HOST|ADDR|SERVER|ENDPOINT|_URL$|_URI$", re.I)

# Geometría del diagrama
NODE_W, NODE_H = 190, 78
GAP_X, GAP_Y = 100, 36
MARGIN = 40


def esc(value):
    return html.escape(str(value), quote=True)


# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------
class Docker:
    def __init__(self, args):
        self.base = ["docker"] + (["--context", args.context] if args.context else [])
        self.compose = self.base + ["compose", "-f", str(args.file)]
        if args.env_file:
            self.compose += ["--env-file", str(args.env_file)]
        if args.project_name:
            self.compose += ["-p", args.project_name]

    @staticmethod
    def run(cmd, check=True):
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[:4])}...: {proc.stderr.strip()}")
        return proc.stdout

    def config(self):
        return json.loads(self.run(self.compose + ["config", "--format", "json"]))

    def context_name(self):
        try:
            return self.run(self.base + ["context", "show"]).strip()
        except Exception:
            return "?"

    def ps(self):
        out = self.run(self.compose + ["ps", "-a", "--format", "json"]).strip()
        if not out:
            return []
        try:
            data = json.loads(out)
            return data if isinstance(data, list) else [data]
        except json.JSONDecodeError:  # Compose reciente: un objeto JSON por línea
            return [json.loads(line) for line in out.splitlines() if line.strip()]

    def inspect(self, ids):
        if not ids:
            return {}
        out = self.run(self.base + ["inspect"] + ids, check=False)
        return {c["Id"]: c for c in json.loads(out or "[]")}

    def images(self, images):
        """{tag: {ports, id, size, created}} de las imágenes locales (las que faltan se ignoran)."""
        if not images:
            return {}
        out = self.run(self.base + ["image", "inspect"] + sorted(images), check=False)
        result = {}
        for img in json.loads(out or "[]"):
            info = {"ports": list(((img.get("Config") or {}).get("ExposedPorts") or {}).keys()),
                    "id": img.get("Id", ""), "size": img.get("Size", 0), "created": img.get("Created", "")}
            for tag in img.get("RepoTags") or []:
                result[tag] = info
        return result

    def stats(self, ids):
        """Consumo instantáneo (CPU, memoria, E/S) de los contenedores en marcha."""
        if not ids:
            return {}
        out = self.run(self.base + ["stats", "--no-stream", "--no-trunc", "--format", "{{json .}}"] + ids, check=False)
        result = {}
        for line in out.splitlines():
            try:
                row = json.loads(line)
                result[row.get("ID", "")] = row
            except json.JSONDecodeError:
                continue
        return result

    def networks(self, names):
        if not names:
            return {}
        out = self.run(self.base + ["network", "inspect"] + sorted(names), check=False)
        return {n["Name"]: n for n in json.loads(out or "[]")}


# ---------------------------------------------------------------------------
# Modelo: servicios, puertos y conexiones
# ---------------------------------------------------------------------------
def full_tag(image):
    """Nombre de imagen con tag explícito (nginx -> nginx:latest), como en RepoTags."""
    return image if ":" in image.rsplit("/", 1)[-1] or "@" in image else f"{image}:latest"


def icon_for(name, image):
    key = f"{name} {image}".lower()
    for words, svg in ICONS:
        if any(w in key for w in words):
            return svg
    return ICON_DEFAULT


def host_aliases(services):
    """Nombres por los que se puede llegar a cada servicio: servicio, container_name, aliases."""
    names = {}
    for svc, conf in services.items():
        names[svc] = svc
        if conf.get("container_name"):
            names[conf["container_name"]] = svc
        for net in (conf.get("networks") or {}).values():
            for alias in (net or {}).get("aliases") or []:
                names[alias] = svc
    return names


def find_refs(text, names, exact=False):
    """Busca referencias a servicios en un texto. Devuelve [(servicio, etiqueta)]."""
    refs = []
    if exact and text.strip() in names:
        refs.append((names[text.strip()], None))
    for host, svc in names.items():
        h = re.escape(host)
        for m in re.finditer(rf"([a-z][a-z0-9+.-]*)://(?:[^@/\s]*@)?{h}(?::(\d+))?(/\d+)?(?![\w.-])", text, re.I):
            scheme, port, path = m.group(1).lower(), m.group(2), m.group(3)
            if scheme.startswith("redis") and path:
                refs.append((svc, f"db{path[1:]}"))
            else:
                refs.append((svc, f":{port}" if port else None))
        for m in re.finditer(rf"(?<![\w./@:-]){h}:(\d{{2,5}})(?![\w.-])", text):
            refs.append((svc, f":{m.group(1)}"))
    return refs


def nginx_routes(text, names):
    """Rutas de nginx: location <ruta> { proxy_pass http://<upstream>... } -> servicio."""
    upstreams = {}
    for m in re.finditer(r"upstream\s+([\w.-]+)\s*\{([^}]*)\}", text):
        server = re.search(r"server\s+([\w.-]+)(?::\d+)?", m.group(2))
        if server and server.group(1) in names:
            upstreams[m.group(1)] = names[server.group(1)]
    routes = []
    for m in re.finditer(r"location\s+(?:[=~^*]+\s*)?(\S+)\s*\{([^}]*)\}", text):
        target = re.search(r"proxy_pass\s+https?://([\w.-]+)", m.group(2))
        if not target:
            continue
        host = target.group(1)
        svc = upstreams.get(host) or names.get(host)
        if svc:
            routes.append((svc, m.group(1)))
    return routes


def read_config_files(conf):
    """Contenido de los ficheros de configuración montados en solo lectura."""
    for vol in conf.get("volumes") or []:
        if vol.get("type") != "bind" or not vol.get("read_only"):
            continue
        path = Path(vol.get("source", ""))
        try:
            if path.is_file() and path.suffix.lower() in CONFIG_EXT and path.stat().st_size <= MAX_CONFIG_BYTES:
                yield path, path.read_text(errors="replace")
        except OSError:
            continue  # sin permisos de lectura (p. ej. ficheros de otro UID)


def build_edges(services):
    names = host_aliases(services)
    edges = {}  # (origen, destino) -> {"labels": [...], "routes": [...], "sources": set(), "dep": bool}

    def add(src, dst, label, source):
        if src == dst:
            return
        e = edges.setdefault((src, dst), {"labels": [], "routes": [], "sources": set(), "dep": False})
        target = e["routes"] if source == "src_nginx" else e["labels"]
        if label and label not in target:
            target.append(label)
        e["sources"].add(source)

    for svc, conf in services.items():
        env = conf.get("environment") or {}
        if isinstance(env, list):
            env = dict(item.split("=", 1) if "=" in item else (item, "") for item in env)
        for key, value in env.items():
            # Valor exacto = nombre de servicio solo si la variable parece un host
            # (evita falsos positivos como APTLY_API_USER=aptly o POSTGRESQL_DATABASE=registry)
            exact = bool(HOST_KEY.search(key))
            for dst, label in find_refs(str(value or ""), names, exact=exact):
                add(svc, dst, label or (key if exact else None), "src_env")
        for _path, text in read_config_files(conf):
            routes = nginx_routes(text, names)
            for dst, route in routes:
                add(svc, dst, route, "src_nginx")
            for dst, label in find_refs(text, names):
                add(svc, dst, label, "src_file")
        deps = conf.get("depends_on") or {}
        if isinstance(deps, list):
            deps = {d: {} for d in deps}
        for dst, opts in deps.items():
            if (opts or {}).get("condition") == "service_completed_successfully":
                continue
            if (svc, dst) not in edges and (dst, svc) not in edges:
                edges[(svc, dst)] = {"labels": [], "routes": [], "sources": {"src_dep"}, "dep": True}
    return edges


def edge_label(e):
    labels = e["routes"] or e["labels"]  # si hay rutas de nginx, mandan sobre los puertos
    text = " ".join(labels[:4]) + (" …" if len(labels) > 4 else "")
    return text if len(text) <= 34 else text[:33] + "…"


# ---------------------------------------------------------------------------
# Estado de los contenedores
# ---------------------------------------------------------------------------
def humanize(seconds):
    seconds = int(seconds)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def container_states(docker, t, services):
    """{servicio: {...}} con estado, salud, reinicios y tiempo en marcha."""
    rows = docker.ps()
    details = docker.inspect([r["ID"] for r in rows if r.get("ID")])
    now = dt.datetime.now(dt.timezone.utc)
    states = {}
    for r in rows:
        d = next((v for k, v in details.items() if k.startswith(r.get("ID", "-"))), {})
        st = d.get("State") or {}
        status = st.get("Status") or r.get("State", "")
        health = (st.get("Health") or {}).get("Status") or r.get("Health") or ""
        code = st.get("ExitCode", r.get("ExitCode", 0))
        if status == "running":
            cls, text = {"healthy": ("ok", t["st_healthy"]), "starting": ("warn", t["st_starting"]),
                         "unhealthy": ("bad", t["st_unhealthy"])}.get(health, ("ok", t["st_running"]))
        elif status == "restarting":
            cls, text = "warn", t["st_restarting"]
        elif status == "exited" and code == 0 and (services.get(r["Service"]) or {}).get("restart") in (None, "no"):
            cls, text = "done", t["st_done"]
        else:
            cls, text = "bad", t["st_exited"].format(code=code)
        uptime = ""
        started = (st.get("StartedAt") or "")[:19]
        if status == "running" and started and not started.startswith("0001"):
            start = dt.datetime.fromisoformat(started).replace(tzinfo=dt.timezone.utc)
            uptime = humanize((now - start).total_seconds())
        states[r["Service"]] = {"cls": cls, "text": text, "name": r.get("Name", ""), "status": status,
                                "health": health, "restarts": d.get("RestartCount", 0), "uptime": uptime,
                                "id": d.get("Id", r.get("ID", "")), "inspect": d}
    return states


# ---------------------------------------------------------------------------
# Fichas de detalle (tooltips y panel)
# ---------------------------------------------------------------------------
# clave=valor / clave: valor (PASSWORD=x, api_key: x) y opciones --password x / -p=x
SECRET_KV = re.compile(r"(\b[\w.-]*(?:pass(?:word)?|passwd|pwd|secret|token|api[_-]?key|private[_-]?key)[\w.-]*\s*[=:]\s*)([^\s,;&]+)", re.I)
SECRET_FLAG = re.compile(r"(--?[\w-]*(?:pass(?:word)?|passwd|secret|token)[\w-]*\s+)([^\s-]\S*)", re.I)


def redact(text, limit=160):
    """Oculta valores que parecen credenciales y recorta textos largos."""
    text = " ".join(str(text).split())
    text = SECRET_FLAG.sub(r"\1***", SECRET_KV.sub(r"\1***", text))
    return text if len(text) <= limit else text[:limit - 1] + "…"


def human_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def local_time(iso):
    iso = (iso or "")[:19]
    if not iso or iso.startswith("0001"):
        return ""
    try:
        return dt.datetime.fromisoformat(iso).replace(tzinfo=dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso


def service_networks(name, conf, project):
    """Redes de un servicio (nombre real en Docker). Sin `networks` usa <proyecto>_default."""
    nets = conf.get("networks")
    if conf.get("network_mode"):
        return []
    return list(nets.keys()) if nets else ["default"]


def build_details(services, edges, states, stats, images, int_ports, net_info, cfg, project, t):
    """Diccionario clave -> ficha. Cada ficha: title, sub, cls, status, sections[{h, brief, rows}].
    Nunca incluye valores de variables de entorno."""
    details = {}
    yn = lambda v: t["yes"] if v else t["no"]
    cfg_nets = cfg.get("networks") or {}

    def real_net(key):
        return (cfg_nets.get(key) or {}).get("name") or f"{project}_{key}"

    for s, conf in services.items():
        st = states.get(s) or {}
        d = st.get("inspect") or {}
        state = d.get("State") or {}
        hcfg = d.get("Config") or {}
        host = d.get("HostConfig") or {}
        img = images.get(full_tag(conf.get("image", ""))) or {}
        sections = []

        rows = [[t["k_status"], st.get("text", t["st_missing"])]]
        if st.get("health"):
            rows.append([t["k_health"], st["health"]])
        log = (state.get("Health") or {}).get("Log") or []
        if log:
            last = log[-1]
            rows.append([t["k_last_check"], f'{local_time(last.get("End"))} · exit {last.get("ExitCode")} · '
                                            f'{redact(last.get("Output", ""), 120) or "—"}'])
        if st:
            rows += [[t["k_restarts"], str(st.get("restarts", 0))],
                     [t["k_started"], local_time(state.get("StartedAt")) or "—"]]
            if st.get("uptime"):
                rows.append([t["k_uptime"], st["uptime"]])
            if state.get("Status") == "exited":
                rows.append([t["k_exit"], str(state.get("ExitCode"))])
            if state.get("OOMKilled"):
                rows.append([t["k_oom"], t["yes"]])
        sections.append({"h": t["sec_state"], "brief": True, "rows": rows})

        sres = next((v for k, v in stats.items() if st.get("id", "-").startswith(k) or k.startswith(st.get("id", "-"))), None)
        if sres:
            sections.append({"h": t["sec_res"], "brief": True, "rows": [
                [t["k_cpu"], sres.get("CPUPerc", "—")],
                [t["k_mem"], f'{sres.get("MemUsage", "—")} ({sres.get("MemPerc", "—")})'],
                [t["k_netio"], sres.get("NetIO", "—")], [t["k_blkio"], sres.get("BlockIO", "—")],
                [t["k_pids"], str(sres.get("PIDs", "—"))]]})

        net_rows = []
        live = (d.get("NetworkSettings") or {}).get("Networks") or {}
        if live:
            for name, n in live.items():
                aliases = [a for a in (n.get("Aliases") or n.get("DNSNames") or []) if len(a) < 40][:4]
                net_rows.append([name, f'{n.get("IPAddress") or "—"}' + (f' · {t["k_aliases"]}: {", ".join(aliases)}' if aliases else "")])
        elif conf.get("network_mode"):
            net_rows.append(["network_mode", conf["network_mode"]])
        else:
            net_rows += [[real_net(k), "—"] for k in service_networks(s, conf, project)]
        sections.append({"h": t["sec_net"], "brief": True, "rows": net_rows})

        pub = [f'{p.get("host_ip") or "0.0.0.0"}:{p.get("published")} → {p.get("target")}/{p.get("protocol", "tcp")}'
               for p in conf.get("ports") or []]
        sections.append({"h": t["sec_ports"], "brief": True, "rows": [
            [t["k_internal"], " ".join(int_ports.get(s, [])) or "—"],
            [t["k_published"], ", ".join(pub) or "—"]]})

        cmd = (hcfg.get("Entrypoint") or []) + (hcfg.get("Cmd") or [])
        hc = (hcfg.get("Healthcheck") or {}).get("Test") or (conf.get("healthcheck") or {}).get("test") or []
        hc = hc[1:] if hc and hc[0] in ("CMD", "CMD-SHELL", "NONE") else hc
        policy = (host.get("RestartPolicy") or {}).get("Name") or conf.get("restart") or "no"
        crow = [[t["k_image"], conf.get("image", "—")]]
        if img:
            crow += [[t["k_image_id"], img["id"].replace("sha256:", "")[:12]],
                     [t["k_image_size"], human_bytes(img["size"])]]
        if d:
            crow += [[t["k_id"], d.get("Id", "")[:12]], [t["k_created"], local_time(d.get("Created"))],
                     [t["k_user"], hcfg.get("User") or "root"]]
        crow += [[t["k_cmd"], redact(" ".join(cmd)) if cmd else "—"],
                 [t["k_hc"], redact(" ".join(hc)) if hc else "—"],
                 [t["k_restart"], policy],
                 [t["k_cap_add"], ", ".join(host.get("CapAdd") or conf.get("cap_add") or []) or "—"],
                 [t["k_cap_drop"], ", ".join(host.get("CapDrop") or conf.get("cap_drop") or []) or "—"],
                 [t["k_ro"], yn(host.get("ReadonlyRootfs") or conf.get("read_only"))]]
        logcfg = host.get("LogConfig") or conf.get("logging") or {}
        if logcfg:
            opts = logcfg.get("Config") or logcfg.get("options") or {}
            crow.append([t["k_log"], (logcfg.get("Type") or logcfg.get("driver", "")) +
                         (" · " + ", ".join(f"{k}={v}" for k, v in opts.items()) if opts else "")])
        sections.append({"h": t["sec_container"], "brief": False, "rows": crow})

        mounts = d.get("Mounts") or [
            {"Destination": v.get("target"), "Source": v.get("source"), "RW": not v.get("read_only"), "Type": v.get("type")}
            for v in conf.get("volumes") or []]
        mrows = [[m.get("Destination", ""), f'{m.get("Source", "")} ({"rw" if m.get("RW") else "ro"})'] for m in mounts[:14]]
        if len(mounts) > 14:
            mrows.append(["…", f"+{len(mounts) - 14}"])
        if mrows:
            sections.append({"h": t["sec_mounts"], "brief": False, "rows": mrows})

        env = conf.get("environment") or {}
        names = sorted(env.keys() if isinstance(env, dict) else [e.split("=", 1)[0] for e in env])
        if names:
            sections.append({"h": t["sec_env"], "brief": False, "rows": [
                [t["k_vars"].format(n=len(names)), ", ".join(names)]]})

        deps = conf.get("depends_on") or {}
        deps = deps if isinstance(deps, dict) else {k: {} for k in deps}
        drows = [[t["k_calls"], ", ".join(sorted(b for (a, b) in edges if a == s and not edges[(a, b)]["dep"])) or "—"],
                 [t["k_called_by"], ", ".join(sorted(a for (a, b) in edges if b == s and not edges[(a, b)]["dep"])) or "—"],
                 [t["k_depends"], ", ".join(f'{k} ({(v or {}).get("condition", "started").replace("service_", "")})'
                                            for k, v in deps.items()) or "—"]]
        sections.append({"h": t["sec_deps"], "brief": False, "rows": drows})

        cname = conf.get("container_name") or st.get("name") or s
        details[f"svc:{s}"] = {"title": s, "sub": f'{cname} · {conf.get("image", "")}',
                               "cls": st.get("cls", "off"), "status": st.get("text", t["st_missing"]),
                               "sections": sections}

    for (a, b), e in edges.items():
        back = edges.get((b, a))
        rows = [[t["k_from"], a], [t["k_to"], b],
                [t["k_labels"], " ".join(e["routes"] + e["labels"]) or "—"],
                [t["k_src"], ", ".join(sorted(t[x] for x in e["sources"]))]]
        if back:
            rows += [[t["k_both"], f'{b} → {a}: ' + (" ".join(back["routes"] + back["labels"]) or "—")]]
        details[f"edge:{a}>{b}"] = {"title": f"{a} → {b}", "sub": t["lg_dep"] if e["dep"] else t["lg_call"],
                                    "cls": "", "status": "", "sections": [{"h": t["sec_conn"], "brief": True, "rows": rows}]}

    pub_rows = []
    for s, conf in services.items():
        for p in conf.get("ports") or []:
            pub_rows.append([f'{p.get("host_ip") or "0.0.0.0"}:{p.get("published")}/{p.get("protocol", "tcp")}',
                             f'{s}:{p.get("target")}'])
            details[f"pub:{s}"] = {"title": f'{t["outside"]} → {s}', "sub": t["sec_pub"], "cls": "", "status": "",
                                   "sections": [{"h": t["sec_pub"], "brief": True,
                                                 "rows": [r for r in pub_rows if r[1].startswith(f"{s}:")]}]}
    details["outside"] = {"title": t["outside"], "sub": t["out_sub"], "cls": "", "status": "",
                          "sections": [{"h": t["sec_pub"], "brief": True, "rows": pub_rows or [["—", "—"]]}]}

    for name, n in net_info.items():
        ipam = ((n.get("IPAM") or {}).get("Config") or [{}])
        members = sorted(c.get("Name", "") for c in (n.get("Containers") or {}).values())
        details[f"net:{name}"] = {"title": name, "sub": f'{t["networks"]} · {n.get("Driver", "")}', "cls": "",
                                  "status": "", "sections": [
            {"h": t["sec_net_cfg"], "brief": True, "rows": [
                [t["k_driver"], n.get("Driver", "—")], [t["k_scope"], n.get("Scope", "—")],
                [t["k_subnet"], ", ".join(c.get("Subnet", "") for c in ipam if c.get("Subnet")) or "—"],
                [t["k_gateway"], ", ".join(c.get("Gateway", "") for c in ipam if c.get("Gateway")) or "—"],
                [t["k_internal_net"], yn(n.get("Internal"))], [t["k_created"], local_time(n.get("Created"))]]},
            {"h": t["h_members"], "brief": True, "rows": [[str(len(members)), ", ".join(members) or "—"]]}]}
    return details


# ---------------------------------------------------------------------------
# Disposición del diagrama
# ---------------------------------------------------------------------------
def layout(services, edges):
    """Columnas por distancia desde el exterior y orden por baricentro."""
    entry = [s for s, c in services.items() if c.get("ports")]
    adj = {s: set() for s in services}
    for (a, b) in edges:
        adj[a].add(b)
    connected = {s for e in edges for s in e}
    # Raíces: los servicios con puertos publicados o, si no hay, los que nadie llama
    roots = entry or [s for s in services if s in connected and not any(b == s for (_, b) in edges)]
    layer = {"__outside__": 0}
    frontier = list(roots)
    for s in frontier:
        layer[s] = 1
    while frontier:
        nxt = []
        for s in frontier:
            for d in sorted(adj[s]):
                if d not in layer:
                    layer[d] = layer[s] + 1
                    nxt.append(d)
        frontier = nxt
    # Conectados pero no alcanzables desde las raíces: a la derecha de sus vecinos
    changed = True
    while changed:
        changed = False
        for s in services:
            if s in layer or s not in connected:
                continue
            near = [layer[x] for e in edges if s in e for x in e if x != s and x in layer]
            if near:
                layer[s] = max(near) + 1
                changed = True
    # Camino más largo: el destino de cada llamada va a la derecha de quien la hace.
    # Los pares que se llaman mutuamente (A<->B) no empujan, y el número de pasadas
    # está acotado para que un ciclo largo no lo haga infinito.
    for _ in range(len(services)):
        moved = False
        for (a, b), e in edges.items():
            if e["dep"] or (b, a) in edges or a not in layer or b not in layer:
                continue
            if layer[b] <= layer[a]:
                layer[b] = layer[a] + 1
                moved = True
        if not moved:
            break
    isolated = [s for s in services if s not in layer]

    cols = {}
    for s, l in layer.items():
        if s != "__outside__":
            cols.setdefault(l, []).append(s)
    pos_index = {"__outside__": 0.0}
    for l in sorted(cols):
        def bary(s):
            preds = [pos_index[a] for (a, b) in edges if b == s and a in pos_index and layer.get(a, 99) < l]
            if s in entry:
                preds.append(0.0)
            return sum(preds) / len(preds) if preds else 1e6
        cols[l].sort(key=lambda s: (bary(s), s))
        for i, s in enumerate(cols[l]):
            pos_index[s] = i - (len(cols[l]) - 1) / 2

    max_rows = max([len(v) for v in cols.values()] + [1])
    col_h = max_rows * NODE_H + (max_rows - 1) * GAP_Y
    top = MARGIN + 60  # sitio para los carriles superiores
    pos = {}
    for l, items in cols.items():
        h = len(items) * NODE_H + (len(items) - 1) * GAP_Y
        y0 = top + (col_h - h) / 2
        for i, s in enumerate(items):
            pos[s] = (MARGIN + l * (NODE_W + GAP_X), y0 + i * (NODE_H + GAP_Y))
    out_h = 64
    if entry:
        pos["__outside__"] = (MARGIN, top + col_h / 2 - out_h / 2)
    n_cols = (max(cols) if cols else 0) + 1
    width = MARGIN * 2 + n_cols * NODE_W + (n_cols - 1) * GAP_X + 80  # +80: bucles de la última columna
    height = top + col_h + MARGIN + 40  # y para los inferiores
    if isolated:
        y = height + 10
        for i, s in enumerate(isolated):
            pos[s] = (MARGIN + (i + 1) * (NODE_W + GAP_X), y)
        height = y + NODE_H + MARGIN
        width = max(width, MARGIN * 2 + (len(isolated) + 1) * (NODE_W + GAP_X))
    return pos, layer, isolated, width, height, out_h


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------
def bezier_mid(p0, p1, p2, p3):
    return tuple(0.125 * a + 0.375 * b + 0.375 * c + 0.125 * d for a, b, c, d in zip(p0, p1, p2, p3))


def render_svg(services, edges, states, pos, layer, isolated, width, height, out_h, t, int_ports):
    parts = [f'<svg viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="{esc(t["caption"])}">',
             '<defs>'
             '<marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="mk" d="M0,0 L10,5 L0,10 z"/></marker>'
             '<marker id="ara" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="mk-acc" d="M0,0 L10,5 L0,10 z"/></marker>'
             '</defs>']
    if isolated:
        y = min(pos[s][1] for s in isolated) - 22
        parts.append(f'<text class="frame-lbl" x="{MARGIN + NODE_W + GAP_X}" y="{y:.0f}">{esc(t["no_net"])}</text>')

    # Fusionar sentidos opuestos en una arista con dos flechas
    drawn, merged = set(), []
    for (a, b), e in edges.items():
        if (a, b) in drawn:
            continue
        back = edges.get((b, a))
        if back:
            drawn.add((b, a))
            if layer.get(a, 0) > layer.get(b, 0):
                a, b, e, back = b, a, back, e
            labels = {"labels": e["labels"] + [x for x in back["labels"] if x not in e["labels"]],
                      "routes": e["routes"] + back["routes"]}
            merged.append((a, b, labels, False, True))
        else:
            merged.append((a, b, e, e["dep"], False))
        merged[-1] = merged[-1] + (f"edge:{merged[-1][0]}>{merged[-1][1]}",)
        drawn.add((a, b))

    # Puertos de anclaje repartidos por el lateral de cada nodo
    outs, ins = {}, {}
    for a, b, *_ in merged:
        outs.setdefault(a, []).append(b)
        ins.setdefault(b, []).append(a)

    def anchor(node, other, side):
        lst = sorted((outs if side == "out" else ins)[node], key=lambda o: pos[o][1])
        i, n = lst.index(other), len(lst)
        x, y = pos[node]
        h = out_h if node == "__outside__" else NODE_H
        return y + h * (i + 1) / (n + 1)

    def detour(a, b, y1, y2):
        """Si una arista salta columnas y hay nodos en medio, la lleva por un carril
        por encima o por debajo de esas columnas. Devuelve (path, x_etiqueta, y_etiqueta)."""
        middle = range(layer[a] + 1, layer[b])
        nodes = [n for n in pos if layer.get(n) in middle and n not in isolated]
        lo, hi = min(y1, y2) - 8, max(y1, y2) + 8
        if not any(pos[n][1] < hi and pos[n][1] + NODE_H > lo for n in nodes):
            return None
        top_y = min(pos[n][1] for n in nodes)
        bot_y = max(pos[n][1] for n in nodes) + NODE_H
        if (y1 + y2) / 2 - top_y < bot_y - (y1 + y2) / 2:
            lanes["top"] += 1
            ly = top_y - 10 - 9 * lanes["top"]
        else:
            lanes["bot"] += 1
            ly = bot_y + 10 + 9 * lanes["bot"]
        x0, x3 = pos[a][0] + NODE_W, pos[b][0]
        g1, g2 = x0 + GAP_X / 2 - 6 * lanes["top" if ly < y1 else "bot"] % 30, x3 - GAP_X / 2
        r1 = 8 if ly > y1 else -8
        r2 = 8 if y2 > ly else -8
        d = (f"M{x0:.1f},{y1:.1f} H{g1 - 8:.1f} Q{g1:.1f},{y1:.1f} {g1:.1f},{y1 + r1:.1f} "
             f"V{ly - r1:.1f} Q{g1:.1f},{ly:.1f} {g1 + 8:.1f},{ly:.1f} H{g2 - 8:.1f} "
             f"Q{g2:.1f},{ly:.1f} {g2:.1f},{ly + r2:.1f} V{y2 - r2:.1f} Q{g2:.1f},{y2:.1f} {g2 + 8:.1f},{y2:.1f} H{x3:.1f}")
        return d, (g1 + g2) / 2, ly - 5

    lanes = {"top": 0, "bot": 0}
    def edge_group(key, a, b, d, cls, markers, label=None, lx=0, ly=0):
        """Arista con zona de ratón ancha e invisible y su etiqueta, agrupadas por clave."""
        out = (f'<g class="edge hot" data-k="{esc(key)}" data-a="{esc(a)}" data-b="{esc(b)}">'
               f'<path class="hit" d="{d}"/><path class="{cls}" d="{d}"{markers}/>')
        if label:
            out += f'<text class="t-lbl" x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle">{esc(label)}</text>'
        return out + "</g>"

    for a, b, e, dep, both, key in merged:
        ax, ay = pos[a]
        bx, by = pos[b]
        y1, y2 = anchor(a, b, "out"), anchor(b, a, "in")
        start = ' marker-start="url(#ar)"' if both else ""
        label = edge_label(e) if not dep else "depends_on"
        routed = detour(a, b, y1, y2) if layer.get(b, 0) - layer.get(a, 0) > 1 else None
        cls = "e dash" if dep else "e"
        markers = f'{start} marker-end="url(#ar)"'
        if routed:
            d, mx, my = routed
            parts.append(edge_group(key, a, b, d, cls, markers, label, mx, my))
            continue
        if layer.get(b, 0) > layer.get(a, 0):
            p0, p3 = (ax + NODE_W, y1), (bx, y2)
            dx = max(40, (p3[0] - p0[0]) / 2)
            p1, p2 = (p0[0] + dx, y1), (p3[0] - dx, y2)
        elif layer.get(b, 0) == layer.get(a, 0):
            p0, p3 = (ax + NODE_W, y1), (bx + NODE_W, y2)
            p1, p2 = (p0[0] + 70, y1), (p3[0] + 70, y2)
        else:
            p0, p3 = (ax, y1), (bx + NODE_W, y2)
            dx = max(40, (p0[0] - p3[0]) / 2)
            p1, p2 = (p0[0] - dx, y1), (p3[0] + dx, y2)
        d = f"M{p0[0]:.1f},{p0[1]:.1f} C{p1[0]:.1f},{p1[1]:.1f} {p2[0]:.1f},{p2[1]:.1f} {p3[0]:.1f},{p3[1]:.1f}"
        mx, my = bezier_mid(p0, p1, p2, p3)
        parts.append(edge_group(key, a, b, d, cls, markers, label, mx, my - 5))

    # Exterior -> servicios con puertos publicados
    entries = [s for s, c in services.items() if c.get("ports")]
    ox, oy = pos.get("__outside__", (0, 0))
    for i, s in enumerate(sorted(entries, key=lambda s: pos[s][1])):
        sx, sy = pos[s]
        y1 = oy + out_h * (i + 1) / (len(entries) + 1)
        y2 = sy + NODE_H / 2
        p0, p3 = (ox + 150, y1), (sx, y2)
        dx = (p3[0] - p0[0]) / 2
        d = f"M{p0[0]:.1f},{p0[1]:.1f} C{p0[0] + dx:.1f},{y1:.1f} {p3[0] - dx:.1f},{y2:.1f} {p3[0]:.1f},{y2:.1f}"
        ports = " ".join(f':{p.get("published")}' for p in services[s]["ports"] if p.get("published"))
        mx, my = bezier_mid(p0, (p0[0] + dx, y1), (p3[0] - dx, y2), p3)
        w = 9 + 7.2 * len(ports)
        parts.append(f'<g class="edge hot" data-k="pub:{esc(s)}" data-a="__outside__" data-b="{esc(s)}">'
                     f'<path class="hit" d="{d}"/><path class="e acc" d="{d}" marker-end="url(#ara)"/>'
                     f'<rect class="port" x="{mx - w / 2:.1f}" y="{my - 11:.1f}" width="{w:.1f}" height="20" rx="4"/>'
                     f'<text class="port-t" x="{mx:.1f}" y="{my + 3:.1f}" text-anchor="middle">{esc(ports)}</text></g>')
    if entries:
        parts.append(f'<g class="node hot" data-k="outside" data-n="__outside__" tabindex="0" transform="translate({ox},{oy:.1f})"><rect class="box ext" width="150" height="{out_h}" rx="9"/>'
                     f'<g class="ic" transform="translate(12,16) scale(1.33)">{ICON_OUTSIDE}</g>'
                     f'<text class="t-name" x="52" y="28">{esc(t["outside"])}</text>'
                     f'<text class="t-sub" x="52" y="45">{esc(t["clients"])}</text></g>')

    # Nodos
    for s, conf in services.items():
        x, y = pos[s]
        st = states.get(s, {"cls": "off", "text": t["st_missing"]})
        cname = conf.get("container_name") or st.get("name") or s
        ports = " ".join(int_ports.get(s, []))
        ports = ports if len(ports) <= 24 else ports[:23] + "…"
        edge_cls = " edge-svc" if conf.get("ports") else ""
        parts.append(
            f'<g class="node hot" data-k="svc:{esc(s)}" data-n="{esc(s)}" tabindex="0" transform="translate({x:.1f},{y:.1f})">'
            f'<rect class="box{edge_cls}" width="{NODE_W}" height="{NODE_H}" rx="9"/>'
            f'<g class="ic" transform="translate(12,20) scale(1.5)">{icon_for(s, conf.get("image", ""))}</g>'
            f'<text class="t-name" x="58" y="24">{esc(s if len(s) <= 18 else s[:17] + "…")}</text>'
            f'<text class="t-sub" x="58" y="41">{esc(cname if len(cname) <= 20 else cname[:19] + "…")}</text>'
            f'<text class="t-port" x="58" y="57">{esc(ports)}</text>'
            f'<circle class="st-{st["cls"]}" cx="64" cy="68" r="3.5"/>'
            f'<text class="t-st" x="72" y="71.5">{esc(st["text"])}</text></g>')
    parts.append("</svg>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Página
# ---------------------------------------------------------------------------
PAGE = string.Template("""<!doctype html>
<html lang="$lang">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
$refresh<title>$title</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+Condensed:wght@600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root {
  --bg: #f3f5f7; --fg: #16202a; --muted: #5a6977; --line: #c3ccd5; --node: #ffffff;
  --accent: #0a66c2; --edge: #6b7a88; --hover: #e8f0f9;
  --ok: #1f8a4c; --warn: #b7791f; --bad: #c53030; --done: #6b7a88; --off: #a0acb8;
  --display: "IBM Plex Sans Condensed", "Arial Narrow", system-ui, sans-serif;
  --sans: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, Menlo, monospace;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0e141a; --fg: #e2e8ee; --muted: #8e9eae; --line: #2f3c49; --node: #161f28;
    --accent: #5aa9f6; --edge: #7f909f; --hover: #1a2a3a;
    --ok: #48bb78; --warn: #ecc94b; --bad: #fc8181; --done: #8e9eae; --off: #4a5868;
    color-scheme: dark;
  }
}
body { margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.55 var(--sans); }
.wrap { max-width: 1400px; margin: 0 auto; padding: 28px 20px 56px; display: grid; gap: 26px; }
header { display: grid; gap: 6px; }
.eyebrow { font: 500 12px var(--mono); letter-spacing: .08em; text-transform: uppercase; color: var(--accent); }
h1 { font: 700 clamp(26px, 4vw, 38px)/1.1 var(--display); margin: 0; }
h2 { font: 600 20px/1.2 var(--display); margin: 0 0 10px; }
p { margin: 0; color: var(--muted); max-width: 80ch; }
code, .m { font: 13px var(--mono); }
.meta { display: flex; flex-wrap: wrap; gap: 6px 22px; font-size: 13px; color: var(--muted); }
.meta b { color: var(--fg); font-weight: 500; }
.summary { display: flex; flex-wrap: wrap; gap: 10px; }
.chip { display: inline-flex; align-items: center; gap: 8px; border: 1px solid var(--line); background: var(--node);
        border-radius: 999px; padding: 4px 12px; font-size: 13px; }
.chip b { font: 600 15px var(--mono); }
.dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.alert { color: var(--bad); }
figure { margin: 0; display: grid; gap: 12px; }
.scroll { overflow-x: auto; border: 1px solid var(--line); border-radius: 10px; background: var(--node); }
.scroll svg { display: block; width: 100%; min-width: 900px; height: auto; }
figcaption, .legend { color: var(--muted); font-size: 13.5px; }
.legend { display: flex; flex-wrap: wrap; gap: 8px 22px; }
.legend span { display: inline-flex; align-items: center; gap: 8px; }
.sw { width: 28px; border-top: 2px solid var(--edge); display: inline-block; }
.sw.dash { border-top-style: dashed; } .sw.acc { border-color: var(--accent); }
.badge { font: 500 11px var(--mono); background: var(--accent); color: var(--node); border-radius: 4px; padding: 1px 6px; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 560px), 1fr)); gap: 28px; }
.grid2 > section, section { min-width: 0; }
.tbl { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th { text-align: left; font: 500 11px var(--mono); text-transform: uppercase; letter-spacing: .07em; color: var(--muted);
     padding: 6px 10px; border-bottom: 1px solid var(--line); }
td { padding: 7px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
td.m { white-space: nowrap; font-variant-numeric: tabular-nums; }
.frame-lbl { font: 500 11px var(--mono); fill: var(--muted); letter-spacing: .06em; text-transform: uppercase; }
.box { fill: var(--node); stroke: var(--line); stroke-width: 1.2; }
.box.ext, .box.edge-svc { stroke: var(--accent); stroke-width: 1.6; }
.ic { fill: none; stroke: var(--accent); stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; }
.t-name { font: 600 13.5px var(--sans); fill: var(--fg); }
.t-sub { font: 11px var(--mono); fill: var(--muted); }
.t-port { font: 500 11px var(--mono); fill: var(--accent); }
.t-st { font: 10.5px var(--mono); fill: var(--muted); }
.t-lbl { font: 10.5px var(--mono); fill: var(--muted); paint-order: stroke; stroke: var(--node); stroke-width: 4px; stroke-linejoin: round; }
.e { fill: none; stroke: var(--edge); stroke-width: 1.4; }
.e.dash { stroke-dasharray: 5 4; } .e.acc { stroke: var(--accent); stroke-width: 1.6; }
.mk { fill: var(--edge); } .mk-acc { fill: var(--accent); }
.port { fill: var(--accent); } .port-t { font: 600 11px var(--mono); fill: var(--node); }
.st-ok { fill: var(--ok); } .st-warn { fill: var(--warn); } .st-bad { fill: var(--bad); }
.st-done { fill: var(--done); } .st-off { fill: var(--off); }
/* Interacción: resaltado, tooltip y panel de detalles */
.hot { cursor: pointer; }
.hit { fill: none; stroke: transparent; stroke-width: 12; }
.node, .edge { transition: opacity .15s; }
svg.focus .node:not(.hl), svg.focus .edge:not(.hl) { opacity: .18; }
.edge.hl .e { stroke: var(--accent); stroke-width: 2.2; }
.node.sel .box, .node:focus-visible .box { stroke: var(--accent); stroke-width: 2.4; }
.node:focus { outline: none; }
tr.hot:hover td, tr.hot.sel td { background: var(--hover); }
#tip { position: fixed; z-index: 10; max-width: min(440px, calc(100vw - 24px)); pointer-events: none;
       background: var(--node); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px;
       box-shadow: 0 10px 28px rgba(0, 0, 0, .22); font-size: 12.5px; line-height: 1.45; }
.tt-h { display: flex; align-items: center; gap: 8px; font-size: 14px; }
.tt-st { font: 11px var(--mono); color: var(--muted); margin-left: auto; padding-left: 12px; }
.tt-sub { font: 11.5px var(--mono); color: var(--muted); margin: 2px 0 4px; overflow-wrap: anywhere; }
.secs { display: grid; gap: 4px 28px; }
.panel .secs { grid-template-columns: repeat(auto-fill, minmax(min(100%, 330px), 1fr)); }
.sec { min-width: 0; }
.tt-sec { font: 500 10.5px var(--mono); text-transform: uppercase; letter-spacing: .07em; color: var(--accent); margin-top: 8px; }
dl { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 2px 12px; margin: 4px 0 0; }
dt { color: var(--muted); } dd { margin: 0; font: 12px var(--mono); overflow-wrap: anywhere; }
.tt-more { margin-top: 8px; color: var(--muted); font-style: italic; font-size: 11.5px; }
.panel { border: 1px solid var(--line); border-radius: 10px; background: var(--node); padding: 14px 16px; font-size: 13px; }
.panel dd { font-size: 12.5px; }
.panel .tt-h { font-size: 17px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="eyebrow">docker compose · $project</div>
    <h1>$title</h1>
    <p>$intro</p>
    <div class="meta"><span>$l_file: <b class="m">$file</b></span><span>$l_context: <b class="m">$context</b></span><span>$l_generated: <b>$generated</b></span></div>
  </header>
  <div class="summary">$summary</div>
  $docker_err
  <figure>
    <div class="scroll">$svg</div>
    <figcaption>$caption</figcaption>
    <div class="legend">
      <span><span class="badge">:443</span> $lg_port</span>
      <span><i class="sw acc"></i> $lg_ext</span>
      <span><i class="sw"></i> $lg_call</span>
      <span><i class="sw dash"></i> $lg_dep</span>
    </div>
  </figure>
  <section><h2>$h_details</h2><div class="panel" id="detail"><p>$details_hint</p></div></section>
  <section><h2>$h_services</h2>$services</section>
  <div class="grid2">
    <section><h2>$h_exposed</h2>$exposed<h2 style="margin-top:26px">$h_networks</h2>$networks</section>
    <section><h2>$h_conns</h2>$conns</section>
  </div>
</div>
<div id="tip" hidden></div>
<script id="details" type="application/json">$details_json</script>
<script>
(() => {
  const D = JSON.parse(document.getElementById("details").textContent);
  const MORE = $click_more;
  const tip = document.getElementById("tip"), panel = document.getElementById("detail");
  const svg = document.querySelector(".scroll svg");
  let cur = null, sel = null;

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  function card(d, full) {
    const root = el("div");
    const h = el("div", "tt-h");
    if (d.cls) { const dot = el("i", "dot"); dot.style.background = "var(--" + d.cls + ")"; h.append(dot); }
    h.append(el("b", null, d.title));
    if (d.status) h.append(el("span", "tt-st", d.status));
    root.append(h);
    if (d.sub) root.append(el("div", "tt-sub", d.sub));
    const secs = el("div", "secs");
    for (const s of d.sections) {
      if (!full && !s.brief) continue;
      const box = el("div", "sec"), dl = el("dl");
      box.append(el("div", "tt-sec", s.h));
      for (const [k, v] of s.rows) dl.append(el("dt", null, k), el("dd", null, v));
      box.append(dl); secs.append(box);
    }
    root.append(secs);
    if (!full && d.sections.some(s => !s.brief)) root.append(el("div", "tt-more", MORE));
    return root;
  }
  function highlight(t, on) {
    if (!svg) return;
    svg.querySelectorAll(".hl").forEach(x => x.classList.remove("hl"));
    svg.classList.toggle("focus", on);
    if (!on) return;
    const k = t.dataset.k, keep = new Set();
    const n = t.dataset.n || (k.startsWith("svc:") ? k.slice(4) : null);
    const edges = svg.querySelectorAll(".edge");
    if (n) {
      keep.add(n);
      edges.forEach(e => { if (e.dataset.a === n || e.dataset.b === n) { e.classList.add("hl"); keep.add(e.dataset.a); keep.add(e.dataset.b); } });
    } else if (k.startsWith("edge:") || k.startsWith("pub:")) {
      const [a, b] = k.startsWith("pub:") ? ["__outside__", k.slice(4)] : k.slice(5).split(">");
      edges.forEach(e => { if ((e.dataset.a === a && e.dataset.b === b) || (e.dataset.a === b && e.dataset.b === a)) e.classList.add("hl"); });
      keep.add(a); keep.add(b);
    } else if (D[k] && D[k].svc) {
      D[k].svc.forEach(x => keep.add(x));
    }
    svg.querySelectorAll(".node").forEach(g => { if (keep.has(g.dataset.n)) g.classList.add("hl"); });
  }
  function place(x, y) {
    const w = tip.offsetWidth, h = tip.offsetHeight;
    let left = x + 16, top = y + 16;
    if (left + w > innerWidth - 8) left = Math.max(8, x - w - 16);
    if (top + h > innerHeight - 8) top = Math.max(8, y - h - 16);
    tip.style.left = left + "px"; tip.style.top = top + "px";
  }
  function show(t, x, y) {
    const d = D[t.dataset.k]; if (!d) return;
    cur = t; tip.replaceChildren(card(d, false)); tip.hidden = false; place(x, y); highlight(t, true);
  }
  function hide() { cur = null; tip.hidden = true; highlight(document.body, false); }

  document.addEventListener("pointerover", e => {
    const t = e.target.closest("[data-k]");
    if (t && t !== cur) show(t, e.clientX, e.clientY);
  });
  document.addEventListener("pointermove", e => { if (cur) place(e.clientX, e.clientY); });
  document.addEventListener("pointerout", e => { if (cur && !cur.contains(e.relatedTarget)) hide(); });
  document.addEventListener("focusin", e => {
    const t = e.target.closest("[data-k]"); if (!t) return;
    const r = t.getBoundingClientRect(); show(t, r.right, r.top);
  });
  document.addEventListener("focusout", hide);
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") hide();
    if ((e.key === "Enter" || e.key === " ") && e.target.matches && e.target.matches("[data-k]")) { e.preventDefault(); pin(e.target); }
  });
  function pin(t) {
    const d = D[t.dataset.k]; if (!d) return;
    if (sel) sel.classList.remove("sel");
    sel = t; t.classList.add("sel");
    panel.replaceChildren(card(d, true));
  }
  document.addEventListener("click", e => { const t = e.target.closest("[data-k]"); if (t) pin(t); });
})();
</script>
</body>
</html>
""")


def table(headers, rows, keys=None):
    """Tabla HTML. `keys` (opcional) asocia cada fila a una ficha de detalle (tooltip)."""
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    keys = keys or [None] * len(rows)
    body = "".join((f'<tr class="hot" data-k="{esc(k)}" tabindex="0">' if k else "<tr>") +
                   "".join(rows_cell(c) for c in r) + "</tr>" for r, k in zip(rows, keys))
    return f'<div class="tbl"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def rows_cell(cell):
    """Una celda puede ser texto, o (texto, clase) para monoespaciado / HTML ya escapado."""
    if isinstance(cell, tuple):
        text, cls = cell
        return f'<td class="{cls}">{text}</td>' if cls == "raw" else f'<td class="{cls}">{esc(text)}</td>'
    return f"<td>{esc(cell)}</td>"


def generate(args):
    t = TEXT[args.lang]
    docker = Docker(args)
    cfg = docker.config()
    services = cfg.get("services") or {}
    project = cfg.get("name") or Path(args.file).resolve().parent.name

    docker_err = ""
    try:
        states = container_states(docker, t, services)
    except Exception as exc:  # p. ej. el daemon no responde
        states = {s: {"cls": "off", "text": t["st_unknown"]} for s in services}
        docker_err = f'<p class="alert">{esc(t["docker_err"])} ({esc(exc)})</p>'

    edges = build_edges(services)

    # Puertos internos: EXPOSE de la imagen, expose/ports del compose y los puertos con
    # los que otros servicios llegan a él (muchas imágenes no declaran EXPOSE)
    images = docker.images({c.get("image") for c in services.values() if c.get("image")})
    reached = {}
    for (_a, b), e in edges.items():
        reached.setdefault(b, set()).update(l[1:] for l in e["labels"] if re.fullmatch(r":\d+", l))
    int_ports = {}
    for s, c in services.items():
        ports = {p.split("/")[0] for p in (images.get(full_tag(c.get("image", ""))) or {}).get("ports", [])}
        ports |= {str(p).split("/")[0] for p in c.get("expose") or []}
        ports |= {str(p.get("target")) for p in c.get("ports") or []}
        ports |= reached.get(s, set())
        int_ports[s] = [f":{p}" for p in sorted(ports, key=lambda p: int(p) if p.isdigit() else 0)]
    # Consumo de los contenedores en marcha y redes del proyecto
    running = [st["id"] for st in states.values() if st.get("status") == "running" and st.get("id")]
    try:
        stats = docker.stats(running)
    except Exception:
        stats = {}
    cfg_nets = cfg.get("networks") or {}
    real_net = lambda k: (cfg_nets.get(k) or {}).get("name") or f"{project}_{k}"
    net_members = {}
    for s, c in services.items():
        for k in service_networks(s, c, project):
            net_members.setdefault(real_net(k), []).append(s)
    try:
        net_info = docker.networks(list(net_members))
    except Exception:
        net_info = {}
    for name in net_members:  # red aún no creada: datos del compose
        if name not in net_info:
            k = next((k for k in cfg_nets if real_net(k) == name), "default")
            net_info[name] = {"Name": name, "Driver": (cfg_nets.get(k) or {}).get("driver") or "bridge",
                              "Scope": "local", "Containers": {}}

    details = build_details(services, edges, states, stats, images, int_ports, net_info, cfg, project, t)
    for name, members in net_members.items():
        details[f"net:{name}"]["svc"] = members

    pos, layer, isolated, width, height, out_h = layout(services, edges)
    svg = render_svg(services, edges, states, pos, layer, isolated, width, height, out_h, t, int_ports)

    # Resumen
    counts = {"ok": 0, "warn": 0, "bad": 0, "off": 0}
    for s in services:
        cls = states.get(s, {"cls": "off"})["cls"]
        counts["ok" if cls in ("ok", "done") else cls] += 1
    summary = "".join(
        f'<span class="chip"><i class="dot" style="background:var(--{k})"></i><b>{n}</b> {esc(t["sum_" + k])}</span>'
        for k, n in counts.items() if n or k == "ok")

    # Tablas
    exp_rows = []
    for s, c in services.items():
        for p in c.get("ports") or []:
            host = f'{p.get("host_ip") or "0.0.0.0"}:{p.get("published", "?")}/{p.get("protocol", "tcp")}'
            exp_rows.append([(host, "m"), (f'{s}:{p.get("target")}', "m")])
    exp_keys = [f"pub:{r[1][0].split(':')[0]}" for r in exp_rows]
    exposed = table([t["h_host"], t["h_target"]], exp_rows, exp_keys) if exp_rows else f"<p>{esc(t['none_exposed'])}</p>"

    net_rows = []
    for name, n in sorted(net_info.items()):
        ipam = (n.get("IPAM") or {}).get("Config") or [{}]
        net_rows.append([(name, "m"), (n.get("Driver", "—"), "m"),
                         (", ".join(c.get("Subnet", "") for c in ipam if c.get("Subnet")) or "—", "m"),
                         (", ".join(c.get("Gateway", "") for c in ipam if c.get("Gateway")) or "—", "m"),
                         (str(len(net_members.get(name, []))), "m")])
    networks = table([t["networks"], t["h_driver"], t["h_subnet"], t["h_gateway"], t["h_members"]],
                     net_rows, [f"net:{r[0][0]}" for r in net_rows])

    conn_rows = []
    for (a, b), e in sorted(edges.items()):
        labels = e["routes"] + e["labels"]
        conn_rows.append([a, b, (" ".join(labels) or "—", "m"),
                          ", ".join(sorted(t[x] for x in e["sources"]))])
    conns = table([t["h_from"], t["h_to"], t["h_via"], t["h_src"]], conn_rows,
                  [f"edge:{a}>{b}" for (a, b) in sorted(edges)])

    svc_rows = []
    for s, c in services.items():
        st = states.get(s, {"cls": "off", "text": t["st_missing"]})
        state_html = (f'<i class="dot" style="background:var(--{st["cls"]})"></i> {esc(st["text"])}', "raw")
        sres = next((v for k, v in stats.items() if st.get("id") and (st["id"].startswith(k) or k.startswith(st["id"]))), {})
        svc_rows.append([s, (c.get("container_name") or st.get("name") or "—", "m"), (c.get("image", "—"), "m"),
                         state_html, (str(st.get("restarts", "—")), "m"), (st.get("uptime") or "—", "m"),
                         (sres.get("CPUPerc") or "—", "m"), ((sres.get("MemUsage") or "—").split(" / ")[0], "m"),
                         (" ".join(int_ports.get(s, [])) or "—", "m")])
    svc_table = table([t["h_service"], t["h_container"], t["h_image"], t["h_state"], t["h_restarts"],
                       t["h_up"], t["h_cpu"], t["h_mem"], t["h_ports"]], svc_rows, [f"svc:{s}" for s in services])

    page = PAGE.substitute(
        lang=args.lang, title=esc(t["title"].format(name=project)), project=esc(project), intro=esc(t["intro"]),
        refresh=f'<meta http-equiv="refresh" content="{args.refresh}">\n' if args.refresh else "",
        l_file=esc(t["file"]), file=esc(Path(args.file).resolve()), l_context=esc(t["context"]),
        context=esc(docker.context_name()), l_generated=esc(t["generated"]),
        generated=esc(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")), summary=summary,
        docker_err=docker_err, svg=svg, caption=esc(t["caption"]), lg_port=esc(t["lg_port"]),
        lg_ext=esc(t["lg_ext"]), lg_call=esc(t["lg_call"]), lg_dep=esc(t["lg_dep"]),
        h_exposed=esc(t["exposed"]), exposed=exposed, h_conns=esc(t["conns"]), conns=conns,
        h_services=esc(t["services"]), services=svc_table, h_networks=esc(t["networks"]), networks=networks,
        h_details=esc(t["details"]), details_hint=esc(t["details_hint"]),
        click_more=json.dumps(t["click_more"]),
        details_json=json.dumps(details, ensure_ascii=False).replace("</", "<\\/"))
    Path(args.output).write_text(page, encoding="utf-8")
    return len(services), len(edges)


def main():
    ap = argparse.ArgumentParser(description="Genera un mapa HTML de un proyecto Docker Compose con el estado de sus contenedores.")
    ap.add_argument("-f", "--file", default="docker-compose.yml", help="fichero compose (por defecto docker-compose.yml)")
    ap.add_argument("--env-file", help="fichero .env alternativo")
    ap.add_argument("-p", "--project-name", help="nombre del proyecto compose")
    ap.add_argument("--context", help="contexto Docker (p. ej. default)")
    ap.add_argument("-o", "--output", default="compose-map.html", help="página de salida (por defecto compose-map.html)")
    ap.add_argument("--lang", choices=sorted(TEXT), default="en", help="idioma de la página")
    ap.add_argument("--refresh", type=int, default=0, metavar="SEG",
                    help="regenera la página cada SEG segundos (la página se recarga sola)")
    args = ap.parse_args()
    if not Path(args.file).is_file():
        sys.exit(f"ERROR: no existe {args.file}")
    while True:
        try:
            n_svc, n_edges = generate(args)
            print(f"{time.strftime('%H:%M:%S')} {args.output}: {n_svc} servicios, {n_edges} conexiones", flush=True)
        except (RuntimeError, json.JSONDecodeError) as exc:
            if not args.refresh:
                sys.exit(f"ERROR: {exc}")
            print(f"{time.strftime('%H:%M:%S')} ERROR: {exc}", file=sys.stderr, flush=True)
        if not args.refresh:
            break
        time.sleep(args.refresh)


if __name__ == "__main__":
    main()
