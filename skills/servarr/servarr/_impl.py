#!/usr/bin/env python3
"""Servarr infrastructure implementation (pure functions).

Generated from the original FastMCP server; the `mcp` object here is a no-op
shim (servarr/_shim.py) so all functions are importable as a plain library.

Wraps servarr-discover and .servarr_functions.sh for Claude Code.
Runs as a stdio subprocess — no ports, no auth, uses existing SSH keys.

SSH aliases expected in ~/.ssh/config:
  Host krypton_root -> root@192.168.86.7
  Host neon         -> root@neon (port 2222)
  Host xenon        -> ai@xenon.local
"""

import base64
import json
import os
import re
import shlex
import subprocess
import sys
import time
import ssl
from typing import Literal, Any

import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path
from servarr._shim import FastMCP

_MCP_TRANSPORT = os.environ.get("SERVARR_MCP_TRANSPORT", "stdio")
_MCP_HOST = os.environ.get("SERVARR_MCP_HOST", "127.0.0.1")
_MCP_PORT = int(os.environ.get("SERVARR_MCP_PORT", "8000"))

mcp = FastMCP("servarr", host=_MCP_HOST, port=_MCP_PORT)


# ── Resources ─────────────────────────────────────────────────────────────────

@mcp.resource("servarr://manifest")
def resource_manifest() -> str:
    """The full container-manifest.json defining paths and metadata."""
    return json.dumps(_load_manifest(), indent=2)

@mcp.resource("servarr://discovery")
def resource_discovery() -> str:
    """The full infrastructure discovery snapshot (cached)."""
    return json.dumps(_cached_discover(), indent=2)

# ── Prompts ───────────────────────────────────────────────────────────────────

@mcp.prompt("troubleshoot-logs")
def prompt_logs(name: str) -> str:
    """Workflow to debug logs for a container."""
    return f"I need to debug the logs for container {name}. Please check its status first, then try to retrieve recent journal logs or application log files."

@mcp.prompt("provision-container")
def prompt_provision(name: str) -> str:
    """Workflow to provision a new or existing container.""" 
    return f"Please audit container {name} to check for compliance, then run the provision tool to fix any issues found."

KRYPTON = "krypton_root"
NEON = "root@neon"
NEON_SCP = "neon"  # SSH config alias — resolves Port 2222; "root@neon" won't match it
XENON = "ai@xenon.local"

def _find_manifest() -> Path:
    """Locate container-manifest.json: env override, else walk up from this file."""
    env = os.environ.get("SERVARR_MANIFEST")
    if env:
        return Path(env).expanduser()
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "container-manifest.json"
        if candidate.exists():
            return candidate
    # Fall back to repo-root guess (mcp/servarr/servarr.py -> repo root)
    return here.parent.parent.parent / "container-manifest.json"

MANIFEST_PATH = _find_manifest()
_manifest_cache: dict | None = None

# FUSE provider containers — stopping these kills the mount chain for all consumers.
# See MEMORY.md: CT 127 (nzbdav), CT 128 (decypharr)
FUSE_PROVIDERS = {127, 128}
FUSE_PROVIDER_NAMES = {"nzbdav", "decypharr"}

_CT_CACHE_TTL = 60  # seconds
_ct_cache: dict[str, tuple[int, str]] = {}
_ct_cache_time: float = 0


# ── Manifest ──────────────────────────────────────────────────────────────────


def _load_manifest() -> dict:
    global _manifest_cache
    if _manifest_cache is None:
        try:
            with open(MANIFEST_PATH) as f:
                _manifest_cache = json.load(f)
        except Exception:
            _manifest_cache = {"containers": {}}
    return _manifest_cache


def _manifest_entry(name: str) -> dict:
    """Return manifest entry for a container by name, or {} if not found."""
    return _load_manifest().get("containers", {}).get(name.lower(), {})


def _resolve_db_path(entry: dict, db: str) -> str:
    """Resolve a DB path from a manifest entry.

    db can be a key in entry['db_paths'] (e.g. 'main', 'library'), a full
    absolute path starting with '/', or empty to auto-select the only DB.
    Raises ValueError if the path cannot be resolved.
    """
    db_paths = entry.get("db_paths", {})

    if db.startswith("/"):
        return db  # explicit absolute path passthrough

    if db:
        if db not in db_paths:
            raise ValueError(
                f"DB key '{db}' not in manifest. Available: {list(db_paths)}"
            )
        return db_paths[db]

    # Auto-select
    if not db_paths:
        raise ValueError(
            "No DB paths in manifest for this container. Pass db_path explicitly."
        )
    if len(db_paths) == 1:
        return next(iter(db_paths.values()))
    raise ValueError(f"Multiple DBs in manifest — specify db key: {list(db_paths)}")


# ── SSH helpers ───────────────────────────────────────────────────────────────


def _ssh(host: str, command: str | list[str], timeout: int = 15) -> str:
    """Run a command on a remote host via SSH. Returns stdout. Raises on failure.
    
    Automatically retries once on rc=7 (connection refused — transient).
    """
    if isinstance(command, list):
        cmd_str = shlex.join(command)
    else:
        cmd_str = command

    def _run(retry: int = 0) -> str:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", host, cmd_str],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        ok = result.returncode == 0 or result.stdout.strip()
        if ok:
            return result.stdout.strip()
        if result.returncode == 7 and retry < 1:
            time.sleep(2)
            return _run(retry + 1)
        raise RuntimeError(
            result.stderr.strip() or f"SSH to {host} failed (rc={result.returncode})"
            + (" — connection refused, may be transient DNS or key issue" if result.returncode == 7 else "")
        )

    return _run()


def _ssh_input(host: str, command: str | list[str], payload: bytes, timeout: int = 30) -> None:
    """Run a remote command with stdin payload."""
    if isinstance(command, list):
        cmd_str = shlex.join(command)
    else:
        cmd_str = command

    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", host, cmd_str],
        input=payload,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode().strip()
        raise RuntimeError(stderr or f"SSH to {host} failed (rc={result.returncode})")

def _ssh_output(host: str, command: str | list[str], timeout: int = 30) -> bytes:
    """Run a remote command and capture stdout as bytes."""
    if isinstance(command, list):
        cmd_str = shlex.join(command)
    else:
        cmd_str = command

    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", host, cmd_str],
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode().strip()
        raise RuntimeError(stderr or f"SSH to {host} failed (rc={result.returncode})")
    return result.stdout


def _krypton(command: str | list[str], timeout: int = 15) -> str:
    return _ssh(KRYPTON, command, timeout)


def _neon(command: str | list[str], timeout: int = 15) -> str:
    return _ssh(NEON, command, timeout)


def _scp(source: str, dest: str, timeout: int = 30) -> None:
    """Copy a file via scp. source/dest are local paths or 'ssh-alias:/path'."""
    result = subprocess.run(
        ["scp", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", "-q", source, dest],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or f"scp failed (rc={result.returncode})"
        )


def _resolve_ct(name_or_id: str) -> tuple[int, str]:
    """Resolve container name or numeric ID -> (ctid, name). Cached for 60s."""
    global _ct_cache, _ct_cache_time
    now = time.monotonic()
    if not _ct_cache or (now - _ct_cache_time) > _CT_CACHE_TTL:
        output = _krypton(["pct", "list"])
        _ct_cache.clear()
        for line in output.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 3:
                ctid = int(parts[0])
                ct_name = parts[-1]
                _ct_cache[str(ctid)] = (ctid, ct_name)
                _ct_cache[ct_name.lower()] = (ctid, ct_name)
        _ct_cache_time = now

    key = name_or_id if name_or_id.isdigit() else name_or_id.lower()
    if key in _ct_cache:
        return _ct_cache[key]
    raise ValueError(
        f"Container '{name_or_id}' not found. Run list_containers() to see available containers."
    )


def _guard_fuse_provider(ctid: int, name: str) -> None:
    """Raise if this container is a FUSE mount provider (stopping it cascades to all consumers)."""
    if ctid in FUSE_PROVIDERS or name in FUSE_PROVIDER_NAMES:
        raise PermissionError(
            f"CT {ctid} ({name}) is a FUSE mount provider. Stopping it will cascade to all consumer "
            "containers (100/102/103/104) via hung bind mounts. Use the Proxmox web UI if you are "
            "certain you need to stop it."
        )


_ct_type_cache: dict[int, dict] = {}

_SERVICE_NAME_ALIASES = {
    "decypharr": "Decypharr",
    "filebrowser": "File Browser",
    "jellyfin": "Jellyfin",
    "litellm": "LiteLLM",
    "nginx proxy manager": "Nginx Proxy Manager",
    "npm": "Nginx Proxy Manager",
    "notifiarr": "Notifiarr",
    "nzbdav": "NZBDAV",
    "overseerr": "Overseerr",
    "plex": "Plex",
    "prowlarr": "Prowlarr",
    "qbittorrent": "qBittorrent",
    "radarr": "Radarr",
    "sabnzbd": "SABnzbd",
    "sonarr": "Sonarr",
    "tautulli": "Tautulli",
}


def _yaml_scalar(value: str | int) -> str:
    """Render a YAML-safe scalar using JSON quoting rules."""
    return json.dumps(str(value))


def _humanize_service_name(name: str) -> str:
    """Return a cleaner display name for a container/service."""
    normalized = re.sub(r"[\s._-]+", " ", name).strip()
    if not normalized:
        return name

    alias = _SERVICE_NAME_ALIASES.get(normalized.lower())
    if alias:
        return alias

    parts = []
    for token in normalized.split():
        lower = token.lower()
        if lower in _SERVICE_NAME_ALIASES:
            parts.append(_SERVICE_NAME_ALIASES[lower])
        elif token.isupper():
            parts.append(token)
        else:
            parts.append(token[:1].upper() + token[1:].lower())
    return " ".join(parts)


def _render_promtail_config(
    ctid: int,
    ct_name: str,
    entry: dict,
    grafana_user: str,
    grafana_token: str,
) -> str:
    """Render the promtail config used by all containers."""
    service_name = _humanize_service_name(ct_name)
    log_paths = entry.get("log_paths", {})
    app_scrape = ""

    if log_paths:
        path_items = list(log_paths.items())
        for idx, (log_key, log_path) in enumerate(path_items, start=1):
            job_name = f"{ct_name}_app" if idx == 1 else f"{ct_name}_app_{idx}"
            app_scrape += f"""
- job_name: {_yaml_scalar(job_name)}
  static_configs:
    - targets: [localhost]
      labels:
        job: {_yaml_scalar(ct_name)}
        service_name: {_yaml_scalar(service_name)}
        container: {_yaml_scalar(ct_name)}
        ctid: {_yaml_scalar(ctid)}
        host: {_yaml_scalar("krypton")}
        log_source: {_yaml_scalar(log_key)}
        __path__: {_yaml_scalar(log_path)}"""

    return f"""server:
  http_listen_port: 9080
  grpc_listen_port: 0
  log_level: warn

positions:
  filename: /tmp/positions.yaml

clients:
  - url: https://logs-prod-026.grafana.net/loki/api/v1/push
    basic_auth:
      username: {grafana_user}
      password: {grafana_token}

scrape_configs:
- job_name: journal
  journal:
    max_age: 12h
    labels:
      job: {_yaml_scalar("journal")}
      service_name: {_yaml_scalar(service_name)}
      container: {_yaml_scalar(ct_name)}
      ctid: {_yaml_scalar(ctid)}
      host: {_yaml_scalar("krypton")}
  relabel_configs:
    - source_labels: ['__journal__systemd_unit']
      target_label: 'unit'
  pipeline_stages:
    - match:
        selector: '{{job=\"journal\"}}'
        stages:
          - drop:
              expression: '.*'
              source: ''
              older_than: 24h

- job_name: system
  static_configs:
    - targets: [localhost]
      labels:
        job: {_yaml_scalar("system")}
        service_name: {_yaml_scalar(service_name)}
        container: {_yaml_scalar(ct_name)}
        ctid: {_yaml_scalar(ctid)}
        host: {_yaml_scalar("krypton")}
        __path__: {_yaml_scalar("/var/log/*.log")}
  pipeline_stages:
    - drop:
        expression: '.*(DEBUG|TRACE).*'
{app_scrape}"""


def _render_promtail_unit() -> str:
    return (
        "[Unit]\n"
        "Description=Promtail log agent\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        "ExecStart=/usr/local/bin/promtail -config.file=/etc/promtail/config.yaml\n"
        "Restart=always\n"
        "RestartSec=10\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target"
    )


def _write_remote_text(ctid: int, remote_path: str, content: str) -> None:
    """Write text into a container without shell-quoting issues."""
    encoded = base64.b64encode(content.encode()).decode()
    _krypton(
        f"pct exec {ctid} -- bash -lc 'printf %s \"{encoded}\" | base64 -d > {remote_path}'",
        timeout=15,
    )


def _ensure_pve_config_line(ctid: int, key: str, value: str) -> bool:
    """Ensure a Proxmox container config line exists with the desired value."""
    path = f"/etc/pve/lxc/{ctid}.conf"
    needle = f"{key}: {value}"
    try:
        current = _krypton(
            f"grep -aE '^{re.escape(key)}: ' {shlex.quote(path)} 2>/dev/null || true",
            timeout=10,
        )
    except Exception:
        current = ""

    if current.strip() == needle:
        return False

    script = (
        f"path={shlex.quote(path)}; "
        f"key={shlex.quote(key)}; "
        f"value={shlex.quote(value)}; "
        "tmp=$(mktemp); "
        'if grep -aEv "^${key}: " "$path" > "$tmp"; then '
        'printf \'%s: %s\\n\' "$key" "$value" >> "$tmp"; '
        'mv "$tmp" "$path"; '
        'else rm -f "$tmp"; exit 1; fi'
    )
    _krypton(["bash", "-lc", script], timeout=15)
    return True


def _provision_outcome(detail: str, *, fallback: str = "error") -> tuple[str, str]:
    """Map known container-state issues to skipped provisioning steps."""
    text = detail.strip()
    nonfatal_markers = (
        "No space left on device",
        "CT is locked (mounted)",
        "Unknown option: lxc.console.logfile",
        "File promtail.service: Bad message",
        "Unit promtail.service failed to load properly",
    )
    if any(marker in text for marker in nonfatal_markers):
        return "skipped", text
    return fallback, text


def _ensure_promtail_config(
    ctid: int, ct_name: str, entry: dict
) -> tuple[list[dict], bool]:
    """Ensure promtail config/unit are up to date; returns actions + restart hint."""
    actions: list[dict] = []
    needs_restart = False

    try:
        grafana_user, grafana_token = _grafana_loki_auth("write")
    except Exception:
        grafana_user = ""
        grafana_token = ""

    if not grafana_user or not grafana_token:
        actions.append(
            {
                "action": "promtail_config",
                "status": "error",
                "detail": "missing Grafana Cloud Loki write credentials",
            }
        )
        return actions, needs_restart

    desired_config = _render_promtail_config(
        ctid, ct_name, entry, grafana_user, grafana_token
    )
    desired_unit = _render_promtail_unit()

    try:
        current_config = _krypton(
            f"pct exec {ctid} -- bash -lc 'test -f /etc/promtail/config.yaml && cat /etc/promtail/config.yaml || true'",
            timeout=15,
        )
    except Exception:
        current_config = ""

    if current_config.strip() != desired_config.strip():
        try:
            _krypton(f"pct exec {ctid} -- mkdir -p /etc/promtail", timeout=10)
            _write_remote_text(ctid, "/etc/promtail/config.yaml", desired_config)
            actions.append(
                {
                    "action": "promtail_config",
                    "status": "applied",
                    "detail": f"labels: service_name={_humanize_service_name(ct_name)}, container={ct_name}, ctid={ctid}, host=krypton",
                }
            )
            needs_restart = True
        except Exception as e:
            status, detail = _provision_outcome(str(e))
            actions.append(
                {
                    "action": "promtail_config",
                    "status": status,
                    "detail": f"{detail}; labels: service_name={_humanize_service_name(ct_name)}, container={ct_name}, ctid={ctid}, host=krypton",
                }
            )
            return actions, False
    else:
        actions.append(
            {
                "action": "promtail_config",
                "status": "ok",
                "detail": f"labels: service_name={_humanize_service_name(ct_name)}, container={ct_name}, ctid={ctid}, host=krypton",
            }
        )

    try:
        current_unit = _krypton(
            f"pct exec {ctid} -- bash -lc 'test -f /etc/systemd/system/promtail.service && cat /etc/systemd/system/promtail.service || true'",
            timeout=15,
        )
    except Exception:
        current_unit = ""

    if current_unit.strip() != desired_unit.strip():
        try:
            _write_remote_text(
                ctid, "/etc/systemd/system/promtail.service", desired_unit
            )
            actions.append(
                {
                    "action": "promtail_unit",
                    "status": "applied",
                    "detail": "updated service unit",
                }
            )
            needs_restart = True
        except Exception as e:
            status, detail = _provision_outcome(str(e))
            actions.append(
                {"action": "promtail_unit", "status": status, "detail": detail}
            )
            return actions, False
    else:
        actions.append(
            {"action": "promtail_unit", "status": "ok", "detail": "already up to date"}
        )

    return actions, needs_restart


def _promtail_is_active(ctid: int) -> bool:
    try:
        pt_status = _krypton(
            f"pct exec {ctid} -- systemctl is-active promtail 2>/dev/null || echo missing",
            timeout=10,
        )
        return _service_is_active(pt_status)
    except Exception:
        return False


def _detect_container_type(ctid: int) -> dict:
    """Detect container type by reading its Proxmox config.

    Returns dict with type ("oci" | "systemd"), ostype, cmode, features, entrypoint.
    Uses pct config (excludes snapshots). Cached per session.
    """
    if ctid in _ct_type_cache:
        return _ct_type_cache[ctid]

    raw = _krypton(["pct", "config", str(ctid)], timeout=10)
    config: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            config[key.strip()] = val.strip()

    ostype = config.get("ostype", "unknown")
    cmode = config.get("cmode", "tty")
    features = config.get("features", "")
    entrypoint = config.get("entrypoint")

    ct_type = "oci" if (cmode == "console" and entrypoint) else "systemd"
    result = {
        "type": ct_type,
        "ostype": ostype,
        "cmode": cmode,
        "features": features,
        "entrypoint": entrypoint,
    }
    _ct_type_cache[ctid] = result
    return result


# ── Local + HTTP helpers ──────────────────────────────────────────────────────

_secret_cache: dict[str, str] = {}


def _passage(path: str) -> str:
    """Read a secret from the local passage store. Cached for session lifetime."""
    if path not in _secret_cache:
        result = subprocess.run(
            ["passage", "show", path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            raise RuntimeError(f"passage show {path}: {result.stderr.strip()}")
        _secret_cache[path] = result.stdout.strip()
    return _secret_cache[path]


def _decode_grafana_loki_token(token: str) -> tuple[str, str]:
    user = ""
    secret = token.strip()
    if secret.startswith("glc_"):
        try:
            payload = json.loads(base64.b64decode(secret[4:]).decode())
            user = str(payload.get("o") or "")
        except Exception:
            pass
    return user.strip(), secret.strip()


def _grafana_loki_auth(purpose: str = "read") -> tuple[str, str]:
    """Return (username, token) for Grafana Cloud Loki.

    The current passage token is a compact glc_* JSON payload that carries the
    tenant ID in `o` and the secret token in `k`.
    """
    user = ""

    def _user_fallback() -> str:
        try:
            explicit = _passage("servarr/grafana/cloud-loki-user")
        except Exception:
            explicit = ""
        if explicit:
            return explicit.strip()
        try:
            decoded_user, _ = _decode_grafana_loki_token(
                _passage("servarr/grafana/cloud-loki-token")
            )
            if decoded_user:
                return decoded_user
        except Exception:
            pass
        try:
            decoded_user, _ = _decode_grafana_loki_token(
                _passage("servarr/grafana/cloud-logs-read-token")
            )
            if decoded_user:
                return decoded_user
        except Exception:
            pass
        return ""

    user = _user_fallback()

    token_paths = (
        (
            "write",
            ["servarr/grafana/cloud-loki-token", "servarr/grafana/cloud-admin-token"],
        ),
        (
            "read",
            [
                "servarr/grafana/cloud-logs-read-token",
                "servarr/grafana/cloud-loki-token",
                "servarr/grafana/cloud-admin-token",
            ],
        ),
    )
    selected_paths = next(paths for name, paths in token_paths if name == purpose)
    for path in selected_paths:
        try:
            token = _passage(path)
        except Exception:
            continue
        if token:
            return user.strip(), token.strip()

    return user.strip(), ""


def _grafana_loki_base() -> str:
    return "https://logs-prod-026.grafana.net/loki/api/v1"


def _grafana_loki_query(
    query: str, start_ns: int, end_ns: int, limit: int = 100
) -> dict:
    params = {
        "query": query,
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": str(limit),
        "direction": "backward",
    }
    url = f"{_grafana_loki_base()}/query_range?{urllib.parse.urlencode(params)}"

    cafile = None
    try:
        import certifi  # type: ignore

        cafile = certifi.where()
    except Exception:
        cafile = (
            ssl.get_default_verify_paths().cafile
            or os.environ.get("SSL_CERT_FILE")
            or "/etc/ssl/cert.pem"
        )

    context = ssl.create_default_context(cafile=cafile)

    def _request_with_auth(purpose: str) -> dict:
        user, token = _grafana_loki_auth(purpose)
        if not user or not token:
            raise RuntimeError("missing Grafana Cloud Loki credentials")

        request = urllib.request.Request(url)
        auth = base64.b64encode(f"{user}:{token}".encode()).decode()
        request.add_header("Authorization", f"Basic {auth}")
        request.add_header("Accept", "application/json")

        with urllib.request.urlopen(request, timeout=10, context=context) as response:
            payload = response.read().decode()
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise RuntimeError("unexpected Loki response")
        return data

    try:
        return _request_with_auth("read")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return _request_with_auth("write")
        raise


def _grafana_loki_push(marker: str, source: str) -> tuple[bool, str]:
    """Push a single marker line to Loki using Passage-backed write credentials."""
    user, token = _grafana_loki_auth("write")
    if not user or not token:
        return False, "missing Grafana Cloud Loki write credentials"

    payload = {
        "streams": [
            {
                "stream": {
                    "job": "servarr-grafana-auth-test",
                    "source": source,
                    "host": "krypton",
                    "service_name": "Grafana Auth Test",
                },
                "values": [[str(int(time.time() * 1_000_000_000)), marker]],
            }
        ]
    }
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{_grafana_loki_base()}/push",
        data=body,
        method="POST",
    )
    auth = base64.b64encode(f"{user}:{token}".encode()).decode()
    request.add_header("Authorization", f"Basic {auth}")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")

    cafile = None
    try:
        import certifi  # type: ignore

        cafile = certifi.where()
    except Exception:
        cafile = (
            ssl.get_default_verify_paths().cafile
            or os.environ.get("SSL_CERT_FILE")
            or "/etc/ssl/cert.pem"
        )

    context = ssl.create_default_context(cafile=cafile)

    try:
        with urllib.request.urlopen(request, timeout=15, context=context) as response:
            if response.status not in (200, 204):
                return False, f"push returned HTTP {response.status}"
        return True, "push accepted"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read().decode()[:200]}"
    except Exception as exc:
        return False, str(exc)


def _loki_find_marker(
    ctid: int, ct_name: str, marker: str, timeout: int = 30
) -> tuple[bool, str]:
    end_ns = int(time.time() * 1_000_000_000)
    start_ns = end_ns - (10 * 60 * 1_000_000_000)
    query = f'{{container="{ct_name}",ctid="{ctid}",host="krypton"}} |= "{marker}"'
    deadline = time.monotonic() + timeout
    last_error = ""

    while time.monotonic() < deadline:
        try:
            data = _grafana_loki_query(query, start_ns, end_ns, limit=20)
            results = ((data or {}).get("data") or {}).get("result") or []
            for stream in results:
                if not isinstance(stream, dict):
                    continue
                for value in stream.get("values") or []:
                    if (
                        isinstance(value, list)
                        and len(value) >= 2
                        and marker in str(value[1])
                    ):
                        return True, "found in Loki"
            last_error = f"marker not found yet ({len(results)} stream(s))"
        except Exception as e:
            last_error = str(e)
        time.sleep(2)

    return False, last_error or "marker not found"


# @mcp.tool()
def grafana_loki_auth_test(timeout: int = 30) -> dict:
    """Validate the Passage-backed Grafana Cloud Loki credential pair.

    This is a direct Loki write/read probe, independent of any container. It is
    the quickest way to prove whether the Passage secret is still usable before
    we try to fan the same auth out to all container shippers.
    """
    marker = f"servarr-grafana-auth-{int(time.time())}"
    push_ok, push_detail = _grafana_loki_push(marker, "passage")
    if not push_ok:
        return {
            "status": "fail",
            "detail": f"push auth failed: {push_detail}",
            "marker": marker,
        }

    end_ns = int(time.time() * 1_000_000_000)
    start_ns = end_ns - (10 * 60 * 1_000_000_000)
    try:
        data = _grafana_loki_query(
            f'{{job="servarr-grafana-auth-test"}} |= "{marker}"',
            start_ns,
            end_ns,
            limit=5,
        )
        results = ((data or {}).get("data") or {}).get("result") or []
        for stream in results:
            if not isinstance(stream, dict):
                continue
            for value in stream.get("values") or []:
                if (
                    isinstance(value, list)
                    and len(value) >= 2
                    and marker in str(value[1])
                ):
                    return {
                        "status": "pass",
                        "detail": "push + query succeeded",
                        "marker": marker,
                    }
        return {
            "status": "fail",
            "detail": "push succeeded but query could not find the marker",
            "marker": marker,
        }
    except Exception as exc:
        return {
            "status": "fail",
            "detail": f"query auth failed: {exc}",
            "marker": marker,
        }


def _service_is_active(raw: str) -> bool:
    return raw.strip() == "active"


def _local(command: str, timeout: int = 10) -> str:
    """Run a local shell command. Returns stdout."""
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0 and not result.stdout.strip():
        raise RuntimeError(
            result.stderr.strip() or f"Command failed (rc={result.returncode})"
        )
    return result.stdout.strip()


_discover_cache: dict | None = None
_discover_cache_time: float = 0


def _cached_discover() -> dict:
    """Get cached discovery JSON (5 min TTL, matches server-side cache)."""
    global _discover_cache, _discover_cache_time
    now = time.monotonic()
    if _discover_cache is None or (now - _discover_cache_time) > 300:
        raw = _krypton(["servarr-discover"], timeout=20)
        _discover_cache = json.loads(raw)
        _discover_cache_time = now
    return _discover_cache


def _service_url(name: str) -> str:
    """Resolve service name -> http://ip:port from cached discovery."""
    data = _cached_discover()
    search = name.lower()

    for ct in data.get("proxmox", {}).get("containers", []):
        if ct.get("name", "").lower() == search:
            ip, port = ct.get("ip", ""), ct.get("port", 0)
            if ip and port:
                return f"http://{ip}:{port}"

    for key in ("addons", "services", "containers"):
        for svc in data.get("homeassistant", {}).get(key, []):
            if search in svc.get("name", "").lower():
                if svc.get("url"):
                    return svc["url"]
                ip, port = svc.get("ip", ""), svc.get("port", 0)
                if ip and port:
                    return f"http://{ip}:{port}"

    raise ValueError(
        f"Service '{name}' not found in discovery. Run discover() to see available services."
    )


def _http(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    data: dict | None = None,
    timeout: int = 10,
) -> str:
    """Make an HTTP request (stdlib only). Returns response body."""
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode()
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if e.fp else ""
        raise RuntimeError(f"HTTP {e.code}: {err_body[:500]}")


_npm_token: str = ""
_npm_token_time: float = 0
NPM_BASE_URL = "http://192.168.86.88:81"
NPM_EMAIL = "darren.loasby@gmail.com"


def _npm_api(endpoint: str, method: str = "GET", data: dict | None = None):
    """NPM API helper with auto-auth. Returns parsed JSON."""
    global _npm_token, _npm_token_time

    base = NPM_BASE_URL

    now = time.monotonic()
    if not _npm_token or (now - _npm_token_time) > 3600:
        secret = _passage("servarr/npm/admin-secret")
        auth_resp = _http(
            f"{base}/api/tokens",
            method="POST",
            data={
                "identity": NPM_EMAIL,
                "secret": secret,
            },
        )
        _npm_token = json.loads(auth_resp).get("token", "")
        _npm_token_time = now
        if not _npm_token:
            raise RuntimeError("NPM auth failed — no token returned")

    headers = {"Authorization": f"Bearer {_npm_token}"}
    resp = _http(f"{base}/api/{endpoint}", method=method, headers=headers, data=data)
    return json.loads(resp)


def _npm_payload(payload: str | dict) -> dict | None:
    """Normalize a JSON payload string or dict for NPM write operations."""
    if isinstance(payload, dict):
        return payload
    if not payload:
        return None
    return json.loads(payload)


# ── Discovery ─────────────────────────────────────────────────────────────────


# @mcp.tool()
def discover(force: bool = False) -> str:
    """Full infrastructure snapshot: all containers, services, mounts, HA addons.

    Returns the raw JSON from servarr-discover on krypton. Cached 5 min on the
    Mac client side (see servarr-discover in .servarr_functions.sh). Use force=True
    to bypass the local cache and re-run on krypton.
    """
    command = ["servarr-discover"]
    if force:
        command.append("--force")
    return _krypton(command, timeout=20)


# @mcp.tool()
def list_containers() -> list:
    """List all Proxmox containers with status, IP, port, URL, and known config/DB paths.

    Faster than discover() when you only need container inventory. Enriches
    live Proxmox data with config_files, db_paths, and deploy info from the
    local container-manifest.json.
    """
    raw = _krypton(["servarr-discover"], timeout=20)
    data = json.loads(raw)
    containers = data.get("proxmox", {}).get("containers", [])
    manifest = _load_manifest().get("containers", {})
    return [
        {
            "id": ct["id"],
            "name": ct["name"],
            "status": ct["status"],
            "ip": ct.get("ip", "unknown"),
            "port": ct.get("port", 0),
            "url": ct.get("url", ""),
            **{
                k: v
                for k, v in manifest.get(ct["name"], {}).items()
                if k in ("config_files", "db_paths", "log_paths", "template", "deploy")
            },
        }
        for ct in containers
    ]


# ── Container operations ──────────────────────────────────────────────────────


# @mcp.tool()
def container_status(name: str = "") -> dict | list[dict]:
    """Show status of one or all Proxmox containers.

    Args:
        name: Container name/CT ID. Empty or "all" returns status for every container.
    """
    if not name or name.lower() == "all":
        raw = _krypton(["pct", "list"])
        results: list[dict] = []
        for line in raw.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 3:
                ctid = parts[0]
                ct_name = parts[-1]
                status_raw = _krypton(["pct", "status", ctid])
                st = status_raw.split(":", 1)[-1].strip() if ":" in status_raw else status_raw
                results.append({"ctid": int(ctid), "name": ct_name, "status": st})
        return results
    ctid, ct_name = _resolve_ct(name)
    raw = _krypton(["pct", "status", str(ctid)])
    status = raw.split(":", 1)[-1].strip() if ":" in raw else raw
    return {"ctid": ctid, "name": ct_name, "status": status}


# @mcp.tool()
def container_logs(name: str, lines: int = 100, unit: str = "") -> str:
    """Show recent journal logs from inside a container.

    Args:
        name:  Container name (e.g. 'radarr') or CT ID.
        lines: Number of log lines to return (default 100).
        unit:  Optional systemd unit to filter by (e.g. 'radarr.service').
               If omitted, returns all journal entries.

    Automatically detects container type (systemd vs OCI) and uses the best
    available log source: journalctl for systemd, app log files or host-side
    console logfile for OCI containers. FUSE providers (CT 127/128) are read
    via host-side console logfile only — never pct exec.
    """
    ctid, ct_name = _resolve_ct(name)
    ct_info = _detect_container_type(ctid)
    is_fuse = ctid in FUSE_PROVIDERS or ct_name in FUSE_PROVIDER_NAMES
    entry = _manifest_entry(ct_name)
    log_paths = entry.get("log_paths", {})
    errors: list[str] = []

    # --- Strategy 1: journalctl (systemd containers only, never FUSE) ---
    if ct_info["type"] == "systemd" and not is_fuse:
        try:
            journal_cmd = ["journalctl", "-n", str(lines), "--no-pager"]
            if unit:
                journal_cmd.extend(["-u", unit])
            cmd = ["pct", "exec", str(ctid), "--", *journal_cmd]
            result = _krypton(cmd, timeout=20)
            if (
                result
                and "No journal files were found" not in result
                and "No entries" not in result
            ):
                return result
            errors.append(f"journalctl: {result[:200] if result else 'empty'}")
        except Exception as e:
            errors.append(f"journalctl: {e}")

    # --- Strategy 2: app log files from manifest (not for FUSE providers) ---
    if log_paths and not is_fuse:
        for key, path in log_paths.items():
            try:
                result = _krypton(
                    ["pct", "exec", str(ctid), "--", "tail", "-n", str(lines), path],
                    timeout=15,
                )
                if result:
                    return f"[source: {key} log @ {path}]\n{result}"
            except Exception as e:
                errors.append(f"{key} log ({path}): {e}")

    # --- Strategy 3: host-side console logfile (OCI + FUSE providers) ---
    try:
        # Check pct config for lxc.console.logfile
        config_raw = _krypton(["pct", "config", str(ctid)], timeout=10)
        for line in config_raw.splitlines():
            if "lxc.console.logfile" in line:
                logfile = line.split(":", 1)[-1].strip().split(",")[0].strip()
                if "=" in logfile:
                    logfile = logfile.split("=", 1)[-1].strip()
                result = _krypton(["tail", "-n", str(lines), logfile], timeout=10)
                if result:
                    return f"[source: console logfile @ {logfile}]\n{result}"
                errors.append(f"console logfile ({logfile}): empty")
                break
        else:
            errors.append("no lxc.console.logfile configured")
    except Exception as e:
        errors.append(f"console logfile: {e}")

    # --- All strategies failed ---
    sources_tried = "; ".join(errors)
    hint = ""
    if ct_info["type"] == "oci":
        hint = " This is an OCI container — consider adding lxc.console.logfile via container_provision()."
    elif "journalctl" in sources_tried and "No entries" in sources_tried:
        hint = " journalctl returned no entries — the container may need nesting=1 in features. Use container_provision() to fix."
    return f"No logs found for CT {ctid} ({ct_name}). Tried: {sources_tried}.{hint}"


# @mcp.tool()
def container_exec(name: str, command: str, raw: bool = False) -> str:
    """Run a command inside a container and return its output.

    Args:
        name:    Container name or CT ID.
        command: Shell command to run inside the container.
        raw:     When True, pass command as-is to ``bash -lc`` instead of
                 shlex.split. Use for pipes, ``&&`` chains, multi-line scripts.
    """
    ctid, ct_name = _resolve_ct(name)
    if raw:
        return _krypton(["pct", "exec", str(ctid), "--", "bash", "-lc", command], timeout=30)
    return _krypton(["pct", "exec", str(ctid), "--"] + shlex.split(command), timeout=30)


# @mcp.tool()
def container_start(name: str) -> str:
    """Start a stopped Proxmox container."""
    ctid, ct_name = _resolve_ct(name)
    _krypton(["pct", "start", str(ctid)])
    return f"Started CT {ctid} ({ct_name})"


# @mcp.tool()
def container_stop(name: str) -> str:
    """Stop a running Proxmox container.

    NOTE: CT 127 (nzbdav) and CT 128 (decypharr) are FUSE mount providers and
    cannot be stopped via this tool — doing so would cascade to all consumer
    containers. This tool will refuse those requests.
    """
    ctid, ct_name = _resolve_ct(name)
    _guard_fuse_provider(ctid, ct_name)
    _krypton(["pct", "stop", str(ctid)])
    return f"Stopped CT {ctid} ({ct_name})"


# @mcp.tool()
def container_restart(name: str) -> str:
    """Reboot a Proxmox container (pct reboot).

    NOTE: CT 127 (nzbdav) and CT 128 (decypharr) are FUSE mount providers —
    rebooting them will temporarily unmount cloud storage for all consumer
    containers. This tool will refuse those requests.
    """
    ctid, ct_name = _resolve_ct(name)
    _guard_fuse_provider(ctid, ct_name)
    _krypton(["pct", "reboot", str(ctid)])
    return f"Rebooted CT {ctid} ({ct_name})"


# ── Host service operations (krypton systemd) ────────────────────────────────


# @mcp.tool()
def service_status(name: str) -> str:
    """Show systemd service status on krypton.

    Args:
        name: Service name, with or without .service suffix (e.g. 'ip-sync' or 'ip-sync.service').
    """
    svc = name if name.endswith(".service") else f"{name}.service"
    return _krypton(["systemctl", "status", "--no-pager", svc], timeout=10)


# @mcp.tool()
def service_logs(name: str, lines: int = 100) -> str:
    """Show recent journalctl logs for a krypton systemd service.

    Args:
        name:  Service name (e.g. 'ip-sync', 'decypharr-mount').
        lines: Number of log lines (default 100).
    """
    svc = name if name.endswith(".service") else f"{name}.service"
    return _krypton(["journalctl", "-u", svc, "-n", str(lines), "--no-pager"])


# @mcp.tool()
def service_restart(name: str) -> str:
    """Restart a krypton systemd service.

    Args:
        name: Service name (e.g. 'ip-sync', 'adguard-dns-sync').
    """
    svc = name if name.endswith(".service") else f"{name}.service"
    _krypton(["systemctl", "restart", svc])
    return f"Restarted {svc} on krypton"


# @mcp.tool()
def service_start(name: str) -> str:
    """Start a krypton systemd service."""
    svc = name if name.endswith(".service") else f"{name}.service"
    _krypton(["systemctl", "start", svc])
    return f"Started {svc} on krypton"


# @mcp.tool()
def service_stop(name: str) -> str:
    """Stop a krypton systemd service."""
    svc = name if name.endswith(".service") else f"{name}.service"
    _krypton(["systemctl", "stop", svc])
    return f"Stopped {svc} on krypton"


# ── Home Assistant (neon docker) ──────────────────────────────────────────────


# @mcp.tool()
def ha_list() -> list:
    """List all Docker containers on neon (HA OS addons)."""
    raw = _neon(["docker", "ps", "--format", "{{json .}}"])
    containers = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        ct = json.loads(line)
        containers.append(
            {
                "name": ct.get("Names", ""),
                "image": ct.get("Image", ""),
                "status": ct.get("Status", ""),
                "ports": ct.get("Ports", ""),
            }
        )
    return containers


# @mcp.tool()
def ha_logs(name: str, lines: int = 100) -> str:
    """Show logs from a Home Assistant addon container on neon.

    Args:
        name:  Partial container name (e.g. 'overseerr', 'prowlarr').
        lines: Number of log lines (default 100).
    """
    containers = _neon(
        ["docker", "ps", "--format", "{{.Names}}", "--filter", f"name={name}"]
    )
    if not containers:
        raise ValueError(
            f"No container matching '{name}' found on neon. Run ha_list() to see available containers."
        )
    results = []
    for container in containers.splitlines():
        output = _neon(["docker", "logs", container, f"--tail={lines}", "2>&1"])
        results.append(f"=== {container} ===\n{output}")
    return "\n\n".join(results)


# @mcp.tool()
def ha_restart(name: str) -> str:
    """Restart a Home Assistant addon container on neon.

    Args:
        name: Partial container name (e.g. 'overseerr', 'prowlarr').
    """
    containers = _neon(
        ["docker", "ps", "--format", "{{.Names}}", "--filter", f"name={name}"]
    )
    if not containers:
        raise ValueError(f"No container matching '{name}' found on neon.")
    restarted = []
    for container in containers.splitlines():
        _neon(["docker", "restart", container])
        restarted.append(container)
    return f"Restarted on neon: {', '.join(restarted)}"


# @mcp.tool()
def ha_exec(name: str, command: str) -> str:
    """Run a command inside a Home Assistant addon container on neon.

    Args:
        name:    Partial container name.
        command: Command to run inside the container.
    """
    containers = _neon(
        ["docker", "ps", "--format", "{{.Names}}", "--filter", f"name={name}"]
    )
    if not containers:
        raise ValueError(f"No container matching '{name}' found on neon.")
    container = containers.splitlines()[0]
    return _neon(["docker", "exec", container] + shlex.split(command))


# ── Container database + config access ────────────────────────────────────────


# @mcp.tool()
def container_db_query(name: str, query: str, db: str = "") -> str:
    """Run a SQLite query inside a container database.

    Looks up the DB path from container-manifest.json — no need to know the
    path. Useful for services that store config/state in SQLite (huntarr,
    wa-requestrr, wizarr, nzbdav, radarr, sonarr, npm, plex ...).

    Args:
        name:  Container name (e.g. 'huntarr') or CT ID.
        query: SQL query to execute (SELECT recommended for inspection).
        db:    DB key from manifest (e.g. 'main', 'library', 'logs') or a
               full absolute path. Omit if the container has exactly one DB.
    """
    ctid, ct_name = _resolve_ct(name)
    entry = _manifest_entry(ct_name)
    db_path = _resolve_db_path(entry, db)
    return _krypton(
        [
            "pct",
            "exec",
            str(ctid),
            "--",
            "sqlite3",
            "-column",
            "-header",
            db_path,
            query,
        ],
        timeout=15,
    )


# @mcp.tool()
def container_config_read(name: str, config: str = "") -> str:
    """Read a config file from inside a container using known paths from the manifest.

    Looks up the file path from container-manifest.json — no need to know
    where the config lives. Use list_containers() to see available config keys.

    Args:
        name:   Container name (e.g. 'radarr') or CT ID.
        config: Config key from manifest (e.g. 'config', 'preferences').
                Omit if the container has exactly one config file.
    """
    ctid, ct_name = _resolve_ct(name)
    entry = _manifest_entry(ct_name)
    config_files = entry.get("config_files", {})

    if config.startswith("/"):
        file_path = config  # explicit path passthrough
    elif config:
        if config not in config_files:
            raise ValueError(
                f"Config key '{config}' not in manifest. Available: {list(config_files)}"
            )
        file_path = config_files[config]
    else:
        if not config_files:
            raise ValueError(
                "No config files in manifest for this container. Pass a path explicitly."
            )
        if len(config_files) == 1:
            file_path = next(iter(config_files.values()))
        else:
            raise ValueError(
                f"Multiple configs in manifest — specify key: {list(config_files)}"
            )

    return _krypton(["pct", "exec", str(ctid), "--", "cat", file_path], timeout=15)


# ── Mount health ──────────────────────────────────────────────────────────────


# @mcp.tool()
def mount_health() -> str:
    """Check health of FUSE mounts on krypton via rclone RC (safe — no stat/ls).

    Checks the rclone remote control endpoint rather than statting the mount path,
    which would hang if the mount is stuck.
    """
    results = {}

    # Check decypharr-mount rclone RC (port 5572)
    try:
        rc = _krypton(
            ["curl", "-s", "--max-time", "3", "localhost:5572/rc/noop"], timeout=8
        )
        results["decypharr-mount rclone RC"] = "healthy" if rc else "no response"
    except Exception as e:
        results["decypharr-mount rclone RC"] = f"error: {e}"

    # Check nzbdav-mount rclone RC (port 5573 if configured, else check process)
    try:
        procs = _krypton(["pgrep", "-a", "rclone"], timeout=5)
        results["rclone processes"] = procs or "none running"
    except Exception as e:
        results["rclone processes"] = f"error: {e}"

    # Systemd unit states
    for unit in ("decypharr-mount.service", "nzbdav-mount.service", "storage.service"):
        try:
            state = _krypton(["systemctl", "is-active", unit], timeout=5)
            results[unit] = state
        except Exception as e:
            results[unit] = f"error: {e}"

    return results


# ── Container audit & provisioning ────────────────────────────────────────────


# @mcp.tool()
def container_audit(name: str = "") -> list:
    """Read-only compliance check against container standards.

    Checks features, journald, auto-login, console logfile, Promtail, timezone,
    description, IP tag, and manifest entry. Returns per-container results.

    Runs on all running containers if name is empty, or a single container by name/ID.
    FUSE providers (CT 127/128) are checked via config only — never exec'd into.
    """
    if name:
        ctid, ct_name = _resolve_ct(name)
        targets = [(ctid, ct_name)]
    else:
        output = _krypton(["pct", "list"])
        targets = []
        for line in output.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 3:
                targets.append((int(parts[0]), parts[-1]))

    results = []
    for ctid, ct_name in targets:
        # Get status
        try:
            status_raw = _krypton(["pct", "status", str(ctid)], timeout=5)
            status = (
                status_raw.split(":", 1)[-1].strip()
                if ":" in status_raw
                else status_raw
            )
        except Exception:
            status = "unknown"

        ct_info = _detect_container_type(ctid)
        is_oci = ct_info["type"] == "oci"
        is_fuse = ctid in FUSE_PROVIDERS or ct_name in FUSE_PROVIDER_NAMES
        is_running = status == "running"
        entry = _manifest_entry(ct_name)
        checks: dict[str, str] = {}

        # features_nesting — systemd only
        if is_oci:
            checks["features_nesting"] = "skip"
        elif "nesting=1" in ct_info.get("features", ""):
            checks["features_nesting"] = "pass"
        else:
            checks["features_nesting"] = "fail: missing nesting=1"

        # journald — systemd + running only, never FUSE
        if is_oci or not is_running or is_fuse:
            checks["journald"] = "skip"
        else:
            try:
                jd = _krypton(
                    [
                        "pct",
                        "exec",
                        str(ctid),
                        "--",
                        "systemctl",
                        "is-active",
                        "systemd-journald",
                    ],
                    timeout=10,
                )
                checks["journald"] = "pass" if "active" in jd else f"fail: {jd}"
            except Exception as e:
                checks["journald"] = f"fail: {e}"

        # auto_login — systemd + running, never FUSE
        if is_oci or not is_running or is_fuse:
            checks["auto_login"] = "skip"
        else:
            try:
                _krypton(
                    [
                        "pct",
                        "exec",
                        str(ctid),
                        "--",
                        "test",
                        "-f",
                        "/etc/systemd/system/console-getty.service.d/autologin.conf",
                    ],
                    timeout=10,
                )
                checks["auto_login"] = "pass"
            except Exception:
                checks["auto_login"] = "fail: no getty autologin override"

        # console_logfile — OCI only
        if not is_oci:
            checks["console_logfile"] = "skip"
        else:
            config_raw = ct_info  # already have config parsed
            try:
                raw = _krypton(["pct", "config", str(ctid)], timeout=10)
                if "lxc.console.logfile" in raw:
                    checks["console_logfile"] = "pass"
                else:
                    checks["console_logfile"] = "fail: no lxc.console.logfile"
            except Exception as e:
                checks["console_logfile"] = f"fail: {e}"

        # hosts_file — OCI only (Docker auto-manages; pct unmanaged doesn't)
        if not is_oci:
            checks["hosts_file"] = "skip"
        elif not is_running:
            checks["hosts_file"] = "skip"
        else:
            try:
                has_lh = _krypton(
                    f"pct exec {ctid} -- sh -c 'grep -qs \"^127\\.0\\.0\\.1[[:space:]]\\+localhost\" /etc/hosts 2>/dev/null && echo yes || echo no'",
                    timeout=10,
                )
                checks["hosts_file"] = "pass" if has_lh == "yes" else "fail: no 127.0.0.1 localhost"
            except Exception as e:
                checks["hosts_file"] = f"fail: {e}"

        # promtail — systemd + running, never FUSE
        if is_oci or not is_running or is_fuse:
            checks["promtail"] = "skip"
        else:
            try:
                pt = _krypton(
                    [
                        "pct",
                        "exec",
                        str(ctid),
                        "--",
                        "systemctl",
                        "is-active",
                        "promtail",
                    ],
                    timeout=10,
                )
                checks["promtail"] = (
                    "pass"
                    if _service_is_active(pt)
                    else f"fail: {pt.strip() or 'inactive'}"
                )
            except Exception as e:
                checks["promtail"] = f"fail: {e}"

        # timezone — running, not FUSE
        if not is_running or is_fuse:
            checks["timezone"] = "skip"
        else:
            try:
                tz = ""
                try:
                    tz = _krypton(
                        ["pct", "exec", str(ctid), "--", "cat", "/etc/timezone"],
                        timeout=10,
                    )
                except Exception:
                    pass  # Ignore error, try next method

                if not tz.strip():
                    try:
                        tz = _krypton(
                            [
                                "pct",
                                "exec",
                                str(ctid),
                                "--",
                                "readlink",
                                "/etc/localtime",
                            ],
                            timeout=10,
                        )
                    except Exception:
                        pass  # Ignore error, tz will be empty

                if "Australia/Sydney" in tz:
                    checks["timezone"] = "pass"
                else:
                    checks["timezone"] = f"fail: {tz.strip() or 'unknown'}"
            except Exception as e:
                checks["timezone"] = f"fail: {e}"

        # description — check from host
        try:
            raw = _krypton(["pct", "config", str(ctid)], timeout=10)
            has_desc = any(line.startswith("description:") for line in raw.splitlines())
            checks["description"] = "pass" if has_desc else "fail: no description"
        except Exception as e:
            checks["description"] = f"fail: {e}"

        # ip_tagged — check tags for numeric IP
        try:
            raw = _krypton(["pct", "config", str(ctid)], timeout=10)
            tags_line = ""
            for line in raw.splitlines():
                if line.startswith("tags:"):
                    tags_line = line.split(":", 1)[-1].strip()
                    break
            if tags_line and re.search(r"\d+\.\d+\.\d+\.\d+", tags_line):
                checks["ip_tagged"] = "pass"
            else:
                checks["ip_tagged"] = f"fail: tags={tags_line or 'none'}"
        except Exception as e:
            checks["ip_tagged"] = f"fail: {e}"

        # manifest — check entry exists
        checks["manifest"] = "pass" if entry else "fail: not in container-manifest.json"

        pass_count = sum(1 for v in checks.values() if v == "pass")
        fail_count = sum(1 for v in checks.values() if v.startswith("fail"))
        skip_count = sum(1 for v in checks.values() if v == "skip")

        results.append(
            {
                "ctid": ctid,
                "name": ct_name,
                "status": status,
                "type": ct_info["type"],
                "summary": f"{pass_count} pass, {fail_count} fail, {skip_count} skip",
                "checks": checks,
            }
        )

    return results


# @mcp.tool()
def container_log_shipping_test(name: str, timeout: int = 30) -> dict:
    """Run an end-to-end Loki shipping test for a running container.

    This writes a unique marker into the container's journal and then checks
    Grafana Cloud Loki for the same marker via the configured logs-read token.
    It is the real proof that promtail is shipping logs, not just that the
    service is active.

    Args:
        name: Container name (e.g. 'litellm') or CT ID.
        timeout: How long to wait for the marker to appear in Loki.
    """
    ctid, ct_name = _resolve_ct(name)
    ct_info = _detect_container_type(ctid)
    is_fuse = ctid in FUSE_PROVIDERS or ct_name in FUSE_PROVIDER_NAMES
    if ct_info["type"] != "systemd" or is_fuse:
        return {
            "ctid": ctid,
            "name": ct_name,
            "status": "skip",
            "detail": "live Loki test only runs for running systemd containers",
        }

    try:
        status_raw = _krypton(["pct", "status", str(ctid)], timeout=5)
        status = (
            status_raw.split(":", 1)[-1].strip() if ":" in status_raw else status_raw
        )
    except Exception as e:
        return {
            "ctid": ctid,
            "name": ct_name,
            "status": "error",
            "detail": f"failed to read status: {e}",
        }
    if status != "running":
        return {
            "ctid": ctid,
            "name": ct_name,
            "status": "skip",
            "detail": f"container is {status}",
        }

    promtail_state = "unknown"
    try:
        pt = _krypton(
            ["pct", "exec", str(ctid), "--", "systemctl", "is-active", "promtail"],
            timeout=10,
        )
        promtail_state = (
            "pass" if _service_is_active(pt) else f"fail: {pt.strip() or 'inactive'}"
        )
    except Exception as e:
        promtail_state = f"fail: {e}"

    if promtail_state != "pass":
        return {
            "ctid": ctid,
            "name": ct_name,
            "status": "fail",
            "detail": f"promtail not healthy: {promtail_state}",
        }

    marker = f"servarr-loki-{ct_name}-{ctid}-{int(time.time())}"
    try:
        _krypton(
            [
                "pct",
                "exec",
                str(ctid),
                "--",
                "sh",
                "-lc",
                f"logger -t servarr-audit {shlex.quote(marker)}",
            ],
            timeout=10,
        )
    except Exception as e:
        return {
            "ctid": ctid,
            "name": ct_name,
            "status": "error",
            "detail": f"failed to emit journal marker: {e}",
        }

    ok, detail = _loki_find_marker(
        ctid, ct_name, marker, timeout=max(5, int(timeout or 30))
    )
    return {
        "ctid": ctid,
        "name": ct_name,
        "status": "pass" if ok else "fail",
        "marker": marker,
        "detail": detail,
    }


# @mcp.tool()
def container_provision(name: str) -> dict:
    """Idempotent fix-up for a container to meet infrastructure standards.

    For systemd containers: ensures nesting=1, getty autologin, journald,
    timezone, Promtail installation + config, description refresh, ip-sync.

    For OCI containers: ensures console logfile, timezone, hosts_file (Docker
    auto-manages /etc/hosts but pct with --ostype unmanaged does not).

    Refuses FUSE providers (CT 127/128). Returns structured action results.
    """
    ctid, ct_name = _resolve_ct(name)
    _guard_fuse_provider(ctid, ct_name)
    ct_info = _detect_container_type(ctid)
    is_oci = ct_info["type"] == "oci"
    entry = _manifest_entry(ct_name)
    actions: list[dict] = []
    needs_restart = False

    # Check if running
    try:
        status_raw = _krypton(f"pct status {ctid}", timeout=5)
        is_running = "running" in status_raw
    except Exception:
        is_running = False

    try:
        config_raw = _krypton(["pct", "config", str(ctid)], timeout=10)
    except Exception:
        config_raw = ""
    is_locked = "lock: mounted" in config_raw

    if not is_running:
        return {
            "ctid": ctid,
            "name": ct_name,
            "type": ct_info["type"],
            "actions": [
                {
                    "action": "skip_all",
                    "status": "skipped",
                    "detail": "container not running",
                }
            ],
            "needs_restart": False,
        }

    if is_oci:
        # --- OCI container provisioning ---

        # Console logfile
        if is_locked:
            actions.append(
                {
                    "action": "console_logfile",
                    "status": "skipped",
                    "detail": "container is mounted/locked",
                }
            )
        else:
            try:
                raw = config_raw
                logfile = f"/var/log/lxc/ct{ctid}-console.log"
                if "lxc.console.logfile" not in raw or logfile not in raw:
                    if _ensure_pve_config_line(ctid, "lxc.console.logfile", logfile):
                        # Note: lxc.console.rotate is not a valid LXC directive in all versions
                        actions.append(
                            {
                                "action": "console_logfile",
                                "status": "applied",
                                "detail": logfile,
                            }
                        )
                        needs_restart = True
                    else:
                        actions.append(
                            {
                                "action": "console_logfile",
                                "status": "ok",
                                "detail": "already set",
                            }
                        )
                else:
                    actions.append(
                        {
                            "action": "console_logfile",
                            "status": "ok",
                            "detail": "already set",
                        }
                    )
            except Exception as e:
                status, detail = _provision_outcome(str(e))
                actions.append(
                    {"action": "console_logfile", "status": status, "detail": detail}
                )

        # Timezone via pct set
        if is_locked:
            actions.append(
                {
                    "action": "timezone",
                    "status": "skipped",
                    "detail": "container is mounted/locked",
                }
            )
        else:
            try:
                _krypton(f"pct set {ctid} -timezone Australia/Sydney", timeout=10)
                actions.append(
                    {
                        "action": "timezone",
                        "status": "applied",
                        "detail": "Australia/Sydney",
                    }
                )
            except Exception as e:
                status, detail = _provision_outcome(str(e))
                actions.append(
                    {"action": "timezone", "status": status, "detail": detail}
                )

        try:
            has_localhost = _krypton(
                f"pct exec {ctid} -- sh -c 'grep -qs \"^127\\.0\\.0\\.1[[:space:]]\\+localhost\" /etc/hosts 2>/dev/null && echo yes || echo no'",
                timeout=10,
            )
            if has_localhost == "yes":
                actions.append(
                    {"action": "hosts_file", "status": "ok", "detail": "already configured"}
                )
            else:
                hosts_content = (
                    "127.0.0.1 localhost\\n"
                    "::1 localhost ip6-localhost ip6-loopback\\n"
                    "127.0.1.1 {name}\\n"
                    "192.168.86.{ctid} {name}\\n\\n"
                    "fe00::0 ip6-localnet\\n"
                    "ff00::0 ip6-mcastprefix\\n"
                    "ff02::1 ip6-allnodes\\n"
                    "ff02::2 ip6-allrouters\\n"
                ).format(name=ct_name, ctid=ctid)
                _krypton(
                    f"pct exec {ctid} -- sh -c 'cat > /etc/hosts << \"HOSTSEOF\"\\n{hosts_content}HOSTSEOF'",
                    timeout=10,
                )
                actions.append(
                    {"action": "hosts_file", "status": "applied", "detail": "written"}
                )
        except Exception as e:
            status, detail = _provision_outcome(str(e))
            actions.append({"action": "hosts_file", "status": status, "detail": detail})

    else:
        # --- Systemd container provisioning ---

        # Features nesting
        features = ct_info.get("features", "")
        if "nesting=1" not in features:
            try:
                new_features = (
                    "nesting=1,keyctl=1"
                    if "keyctl=1" not in features
                    else f"nesting=1,{features}"
                )
                if features and "nesting=1" not in features:
                    # Merge: add nesting to existing
                    parts = [f.strip() for f in features.split(",") if f.strip()]
                    if "nesting=1" not in parts:
                        parts.insert(0, "nesting=1")
                    new_features = ",".join(parts)
                _krypton(f"pct set {ctid} -features {new_features}", timeout=10)
                actions.append(
                    {
                        "action": "features_nesting",
                        "status": "applied",
                        "detail": new_features,
                    }
                )
                needs_restart = True
            except Exception as e:
                actions.append(
                    {"action": "features_nesting", "status": "error", "detail": str(e)}
                )
        else:
            actions.append(
                {"action": "features_nesting", "status": "ok", "detail": features}
            )

        # Getty autologin
        try:
            _krypton(
                f"pct exec {ctid} -- test -f /etc/systemd/system/console-getty.service.d/autologin.conf",
                timeout=10,
            )
            actions.append(
                {"action": "auto_login", "status": "ok", "detail": "already configured"}
            )
        except Exception:
            try:
                _krypton(
                    f"pct exec {ctid} -- mkdir -p /etc/systemd/system/console-getty.service.d",
                    timeout=10,
                )
                override = (
                    "[Service]\\n"
                    "ExecStart=\\n"
                    "ExecStart=-/sbin/agetty --autologin root --noclear --keep-baud console 115200,38400,9600 $TERM"
                )
                _krypton(
                    f"pct exec {ctid} -- bash -c 'printf \"{override}\" > /etc/systemd/system/console-getty.service.d/autologin.conf'",
                    timeout=10,
                )
                _krypton(f"pct exec {ctid} -- systemctl daemon-reload", timeout=10)
                _krypton(
                    f"pct exec {ctid} -- systemctl restart console-getty", timeout=10
                )
                actions.append(
                    {
                        "action": "auto_login",
                        "status": "applied",
                        "detail": "getty autologin configured",
                    }
                )
            except Exception as e:
                status, detail = _provision_outcome(str(e))
                actions.append(
                    {"action": "auto_login", "status": status, "detail": detail}
                )

        # Restart journald if failed
        try:
            jd_status = _krypton(
                f"pct exec {ctid} -- systemctl is-active systemd-journald", timeout=10
            )
            if "active" not in jd_status:
                _krypton(
                    f"pct exec {ctid} -- systemctl restart systemd-journald", timeout=10
                )
                actions.append(
                    {"action": "journald", "status": "applied", "detail": "restarted"}
                )
            else:
                actions.append(
                    {"action": "journald", "status": "ok", "detail": "active"}
                )
        except Exception as e:
            actions.append({"action": "journald", "status": "error", "detail": str(e)})

        # Timezone
        try:
            tz = _krypton(
                f"pct exec {ctid} -- cat /etc/timezone 2>/dev/null || echo unknown",
                timeout=10,
            )
            if "Australia/Sydney" not in tz:
                _krypton(
                    f"pct exec {ctid} -- ln -sf /usr/share/zoneinfo/Australia/Sydney /etc/localtime",
                    timeout=10,
                )
                _krypton(
                    f"pct exec {ctid} -- bash -c 'echo Australia/Sydney > /etc/timezone'",
                    timeout=10,
                )
                actions.append(
                    {
                        "action": "timezone",
                        "status": "applied",
                        "detail": "Australia/Sydney",
                    }
                )
            else:
                actions.append(
                    {"action": "timezone", "status": "ok", "detail": "Australia/Sydney"}
                )
        except Exception as e:
            status, detail = _provision_outcome(str(e))
            actions.append({"action": "timezone", "status": status, "detail": detail})

        # Promtail
        try:
            pt_status = _krypton(
                f"pct exec {ctid} -- systemctl is-active promtail 2>/dev/null || echo missing",
                timeout=10,
            )
            if _service_is_active(pt_status):
                actions.append(
                    {"action": "promtail", "status": "ok", "detail": "active"}
                )
            else:
                actions.append(
                    {"action": "promtail", "status": "ok", "detail": "inactive"}
                )

            try:
                _krypton(
                    f"pct exec {ctid} -- test -s /usr/local/bin/promtail", timeout=10
                )
                actions.append(
                    {
                        "action": "promtail_binary",
                        "status": "ok",
                        "detail": "already installed",
                    }
                )
            except Exception:
                _krypton(
                    f"pct exec {ctid} -- bash -c '"
                    "cd /tmp && "
                    "curl -sLO https://github.com/grafana/loki/releases/download/v3.4.2/promtail-linux-amd64.zip && "
                    "unzip -o promtail-linux-amd64.zip && "
                    "mv promtail-linux-amd64 /usr/local/bin/promtail && "
                    "chmod +x /usr/local/bin/promtail && "
                    "rm -f promtail-linux-amd64.zip'",
                    timeout=120,
                )
                actions.append(
                    {
                        "action": "promtail_binary",
                        "status": "applied",
                        "detail": "downloaded v3.4.2",
                    }
                )

            promtail_actions, promtail_needs_restart = _ensure_promtail_config(
                ctid, ct_name, entry
            )
            actions.extend(promtail_actions)
            promtail_skipped = any(
                item.get("action") in {"promtail_config", "promtail_unit"}
                and item.get("status") == "skipped"
                for item in promtail_actions
            )
            if promtail_needs_restart:
                _krypton(f"pct exec {ctid} -- systemctl daemon-reload", timeout=10)
                if _service_is_active(pt_status):
                    _krypton(
                        f"pct exec {ctid} -- systemctl restart promtail", timeout=15
                    )
                    actions.append(
                        {
                            "action": "promtail_service",
                            "status": "applied",
                            "detail": "restarted",
                        }
                    )
                else:
                    _krypton(
                        f"pct exec {ctid} -- systemctl enable --now promtail",
                        timeout=15,
                    )
                    actions.append(
                        {
                            "action": "promtail_service",
                            "status": "applied",
                            "detail": "enabled + started",
                        }
                    )
            elif promtail_skipped:
                actions.append(
                    {
                        "action": "promtail_service",
                        "status": "skipped",
                        "detail": "promtail not updated due to container state",
                    }
                )
            else:
                actions.append(
                    {
                        "action": "promtail_service",
                        "status": "ok",
                        "detail": "already up to date",
                    }
                )
        except Exception as e:
            status, detail = _provision_outcome(str(e))
            actions.append({"action": "promtail", "status": status, "detail": detail})

    # --- Common: description refresh + ip-sync ---
    if is_locked:
        actions.append(
            {
                "action": "description",
                "status": "skipped",
                "detail": "container is mounted/locked",
            }
        )
    else:
        try:
            _krypton(
                f"/opt/lxc-description-refresh/bin/lxc-description-refresh --ct {ctid}",
                timeout=60,
            )
            actions.append(
                {"action": "description", "status": "applied", "detail": "refreshed"}
            )
        except Exception as e:
            status, detail = _provision_outcome(str(e))
            actions.append(
                {"action": "description", "status": status, "detail": detail}
            )

    try:
        _krypton("systemctl start ip-sync.service", timeout=15)
        actions.append(
            {"action": "ip_sync", "status": "applied", "detail": "triggered"}
        )
    except Exception as e:
        actions.append({"action": "ip_sync", "status": "error", "detail": str(e)})

    return {
        "ctid": ctid,
        "name": ct_name,
        "type": ct_info["type"],
        "actions": actions,
        "needs_restart": needs_restart,
    }


# @mcp.tool()
def container_provision_all() -> dict:
    """Run container_provision() across all running Proxmox containers.

    This is the fleet-level sync that pushes the Passage-backed Loki credentials
    and the standard promtail/service-label setup into every container we manage.
    FUSE providers are skipped automatically.
    """
    raw = _krypton(["servarr-discover"], timeout=20)
    data = json.loads(raw)
    containers = data.get("proxmox", {}).get("containers", [])

    results: list[dict] = []
    for ct in containers:
        name = ct.get("name", "")
        ctid = ct.get("id", 0)
        status = ct.get("status", "unknown")
        if (
            not name
            or status != "running"
            or ctid in FUSE_PROVIDERS
            or name in FUSE_PROVIDER_NAMES
        ):
            detail = f"container status: {status}"
            if ctid in FUSE_PROVIDERS or name in FUSE_PROVIDER_NAMES:
                detail = "FUSE provider skipped"
            results.append(
                {
                    "ctid": ctid,
                    "name": name,
                    "status": "skipped",
                    "detail": detail,
                }
            )
            continue
        try:
            result = container_provision(name)
            results.append(
                {
                    "ctid": result.get("ctid", ctid),
                    "name": result.get("name", name),
                    "status": "ok",
                    "needs_restart": bool(result.get("needs_restart")),
                    "actions": result.get("actions", []),
                }
            )
        except Exception as exc:
            results.append(
                {
                    "ctid": ctid,
                    "name": name,
                    "status": "error",
                    "detail": str(exc),
                }
            )

    return {
        "count": len(results),
        "results": results,
    }


# ── Container CRUD (Proxmox) ─────────────────────────────────────────────────


# @mcp.tool()
def list_templates() -> str:
    """List CT templates available on krypton's local storage.

    Includes both standard distribution templates and OCI images pulled via skopeo.
    Use the NAME column value as the `template` argument to container_create().
    """
    return _krypton("pveam list local 2>/dev/null", timeout=10)


# @mcp.tool()
def pull_oci_template(image: str, tag: str = "latest", template_name: str = "") -> str:
    """Pull a Docker/OCI image to krypton's template storage via skopeo.

    Downloads the image as an OCI archive tarball so it can be used with container_create().

    Args:
        image:         Full image reference, e.g. 'ghcr.io/hotio/notifiarr' or 'linuxserver/sonarr'.
                       For Docker Hub images omit the registry prefix.
        tag:           Image tag (default 'latest').
        template_name: Output filename without extension, e.g. 'notifiarr_latest'.
                       Defaults to the image basename + '_' + tag.
    """
    if not template_name:
        basename = image.split("/")[-1].replace(":", "_")
        template_name = f"{basename}_{tag}"

    dest = f"/var/lib/vz/template/cache/{template_name}.tar"

    # Normalise image reference — bare names are Docker Hub
    src = image if "/" in image and "." in image.split("/")[0] else f"docker.io/{image}"
    src_ref = f"docker://{src}:{tag}"

    cmd = f"skopeo copy --override-os linux --override-arch amd64 {src_ref} oci-archive:{dest}"
    result = _krypton(cmd, timeout=300)  # large images can take a while
    return f"Pulled {src_ref} → {dest}\n{result}"


# @mcp.tool()
def container_create(
    name: str,
    template: str,
    memory: int = 512,
    cores: int = 1,
    disk_gb: int = 8,
    vmid: int = 0,
    bridge: str = "vmbr0",
    ip: str = "dhcp",
    unprivileged: bool = True,
    features: str = "nesting=1",
    ostype: str = "unmanaged",
    storage: str = "local-lvm",
    start: bool = True,
    provision: bool = True,
) -> str:
    """Create a new Proxmox LXC container from a CT template or OCI image.

    Use list_templates() to see available templates. For OCI images not yet
    downloaded, run pull_oci_template() first.

    After creation, auto-detects OCI vs systemd and applies sensible defaults:
    - OCI: sets cmode=console, lxc.signal.halt=SIGTERM, lxc.console.logfile
    - Systemd: ensures nesting=1,keyctl=1 in features, sets timezone

    If start=True, starts the container. If provision=True (requires start),
    runs container_provision() to install Promtail, autologin, etc.

    Args:
        name:         Hostname for the new container (also used for discovery).
        template:     Template name from list_templates(), e.g. 'notifiarr_latest.tar'
                      or 'debian-12-standard_12.12-1_amd64.tar.zst'. Include the extension.
        memory:       RAM in MB (default 512).
        cores:        vCPU count (default 1).
        disk_gb:      Root filesystem size in GB (default 8).
        vmid:         Container ID. 0 = auto-select next available ID.
        bridge:       Network bridge (default 'vmbr0').
        ip:           IP config — 'dhcp' or CIDR e.g. '192.168.86.150/24,gw=192.168.86.1'.
        unprivileged: Run unprivileged (default True, recommended).
        features:     LXC feature flags (default 'nesting=1').
        ostype:       OS type hint — 'unmanaged' for OCI images, 'alpine', 'debian', etc.
        storage:      Storage for rootfs (default 'local-lvm').
        start:        Start the container after creation (default True).
        provision:    Run container_provision() after start (default True, requires start).
    """
    if vmid == 0:
        vmid = int(_krypton("pvesh get /cluster/nextid", timeout=5))

    # Resolve template to full storage path if not already prefixed
    tmpl = template if ":" in template else f"local:vztmpl/{template}"

    ip_config = f"ip={ip}" if ip == "dhcp" else f"ip={ip}"
    unpriv_flag = "--unprivileged 1" if unprivileged else "--unprivileged 0"

    cmd = (
        f"pct create {vmid} {tmpl} "
        f"--hostname {name} "
        f"--memory {memory} "
        f"--cores {cores} "
        f"--rootfs {storage}:{disk_gb} "
        f"--net0 name=eth0,bridge={bridge},{ip_config},type=veth "
        f"--ostype {ostype} "
        f"--features {features} "
        f"{unpriv_flag}"
    )
    result = _krypton(cmd, timeout=60)
    output = [f"Created CT {vmid} ({name})"]
    if result:
        output.append(result)

    # Detect if this is an OCI image and apply defaults
    is_oci = ostype in ("unmanaged",) and template.endswith(".tar")
    if is_oci:
        try:
            _krypton(f"pct set {vmid} -cmode console", timeout=10)
            _krypton(f"pct set {vmid} -lxc.signal.halt SIGTERM", timeout=10)
            logfile = f"/var/log/lxc/ct{vmid}-console.log"
            _krypton(f"pct set {vmid} -lxc.console.logfile {logfile}", timeout=10)
            output.append(
                f"OCI defaults applied: cmode=console, SIGTERM halt, console logfile={logfile}"
            )
        except Exception as e:
            output.append(f"Warning: OCI defaults partially failed: {e}")
    else:
        # Systemd: ensure good features and timezone
        if "nesting=1" not in features:
            try:
                _krypton(f"pct set {vmid} -features nesting=1,keyctl=1", timeout=10)
                output.append("Features updated: nesting=1,keyctl=1")
            except Exception as e:
                output.append(f"Warning: features update failed: {e}")
        try:
            _krypton(f"pct set {vmid} -timezone Australia/Sydney", timeout=10)
        except Exception:
            pass

    # Start if requested
    if start:
        try:
            _krypton(f"pct start {vmid}", timeout=30)
            output.append(f"Started CT {vmid}")
            # Clear caches so new container is discoverable
            _ct_type_cache.pop(vmid, None)
            global _ct_cache_time
            _ct_cache_time = 0
        except Exception as e:
            output.append(f"Start failed: {e}")
            start = False  # can't provision if not started

    # Provision if requested
    if provision and start:
        try:
            # Need cache cleared to resolve the new container
            prov_result = container_provision(name)
            prov_actions = prov_result.get("actions", [])
            applied = [a["action"] for a in prov_actions if a["status"] == "applied"]
            errors = [
                f"{a['action']}: {a['detail']}"
                for a in prov_actions
                if a["status"] == "error"
            ]
            if applied:
                output.append(f"Provisioned: {', '.join(applied)}")
            if errors:
                output.append(f"Provision errors: {'; '.join(errors)}")
            if prov_result.get("needs_restart"):
                output.append(
                    "Note: some changes require a container restart to take effect."
                )
        except Exception as e:
            output.append(f"Provision failed: {e}")

    return "\n".join(output)


# @mcp.tool()
def container_destroy(name: str, purge: bool = True) -> str:
    """Destroy a Proxmox container permanently. This is irreversible.

    The container must be stopped first. FUSE provider containers (CT 127, 128)
    are blocked — use the Proxmox web UI if you are certain.

    Args:
        name:  Container name or CT ID.
        purge: Also remove from replication jobs and HA config (default True).
    """
    ctid, ct_name = _resolve_ct(name)
    _guard_fuse_provider(ctid, ct_name)

    # Ensure stopped before destroy
    status = _krypton(f"pct status {ctid}", timeout=5)
    if "running" in status:
        raise RuntimeError(
            f"CT {ctid} ({ct_name}) is still running. Stop it first with container_stop()."
        )

    purge_flag = "--purge" if purge else ""
    _krypton(f"pct destroy {ctid} {purge_flag}", timeout=30)
    return f"Destroyed CT {ctid} ({ct_name})"


# ── Container CRUD (neon Docker) ──────────────────────────────────────────────


# @mcp.tool()
def ha_container_run(
    image: str,
    name: str,
    ports: list[str] | None = None,
    env: list[str] | None = None,
    volumes: list[str] | None = None,
    restart: str = "unless-stopped",
) -> str:
    """Start a new Docker container on neon.

    Args:
        image:    Docker image, e.g. 'ghcr.io/hotio/overseerr:latest'.
        name:     Container name.
        ports:    Port mappings, e.g. ['8080:8080', '443:443'].
        env:      Environment variables, e.g. ['PUID=1000', 'TZ=Australia/Sydney'].
        volumes:  Volume mounts, e.g. ['/config:/config'].
        restart:  Restart policy (default 'unless-stopped').
    """
    port_flags = " ".join(f"-p {p}" for p in (ports or []))
    env_flags = " ".join(f"-e {e}" for e in (env or []))
    vol_flags = " ".join(f"-v {v}" for v in (volumes or []))

    cmd = f"docker run -d --name {name} --restart {restart} {port_flags} {env_flags} {vol_flags} {image}"
    result = _neon(cmd, timeout=60)
    return f"Started {name} on neon: {result}"


# @mcp.tool()
def ha_container_remove(name: str) -> str:
    """Remove a Docker container from neon (stops and deletes it).

    Args:
        name: Exact or partial container name.
    """
    containers = _neon(f"docker ps -a --format '{{{{.Names}}}}' | grep -i {name}")
    if not containers:
        raise ValueError(
            f"No container matching '{name}' found on neon (including stopped)."
        )
    removed = []
    for container in containers.splitlines():
        _neon(f"docker rm -f {container}")
        removed.append(container)
    return f"Removed from neon: {', '.join(removed)}"


# ── File transfer ─────────────────────────────────────────────────────────────


@mcp.tool()
def container_push(name: str, local_path: str, remote_path: str) -> str:
    """Copy a local file into a Proxmox container.

    Args:
        name:        Container name (e.g. 'radarr') or CT ID.
        local_path:  Path to the file on this machine.
        remote_path: Destination path inside the container.
    """
    ctid, ct_name = _resolve_ct(name)
    remote_parent = Path(remote_path).parent.as_posix()
    payload = Path(local_path).read_bytes()
    remote_cmd = f"mkdir -p {shlex.quote(remote_parent)} && cat > {shlex.quote(remote_path)}"
    _ssh_input(KRYPTON, ["pct", "exec", str(ctid), "--", "bash", "-lc", remote_cmd], payload)
    return f"Pushed {local_path} → CT {ctid} ({ct_name}):{remote_path}"


@mcp.tool()
def container_pull(name: str, remote_path: str, local_path: str) -> str:
    """Copy a file from a Proxmox container to local.

    Args:
        name:        Container name (e.g. 'radarr') or CT ID.
        remote_path: Path to the file inside the container.
        local_path:  Destination path on this machine.
    """
    ctid, ct_name = _resolve_ct(name)
    local_file = Path(local_path)
    local_file.parent.mkdir(parents=True, exist_ok=True)
    payload = _ssh_output(KRYPTON, ["pct", "exec", str(ctid), "--", "cat", remote_path])
    local_file.write_bytes(payload)
    return f"Pulled CT {ctid} ({ct_name}):{remote_path} → {local_path}"


@mcp.tool()
def ha_push(name: str, local_path: str, remote_path: str) -> str:
    """Copy a local file into a neon Docker container.

    Args:
        name:        Partial container name (e.g. 'overseerr').
        local_path:  Path to the file on this machine.
        remote_path: Destination path inside the container.
    """
    containers = _neon(
        ["docker", "ps", "--format", "{{.Names}}", "--filter", f"name={name}"]
    )
    if not containers:
        raise ValueError(f"No container matching '{name}' found on neon.")
    container = containers.splitlines()[0]
    remote_parent = Path(remote_path).parent.as_posix()
    payload = Path(local_path).read_bytes()
    remote_cmd = f"mkdir -p {shlex.quote(remote_parent)} && cat > {shlex.quote(remote_path)}"
    _ssh_input(NEON, ["docker", "exec", "-i", container, "sh", "-lc", remote_cmd], payload)
    return f"Pushed {local_path} → neon:{container}:{remote_path}"


@mcp.tool()
def ha_pull(name: str, remote_path: str, local_path: str) -> str:
    """Copy a file from a neon Docker container to local.

    Args:
        name:        Partial container name (e.g. 'overseerr').
        remote_path: Path to the file inside the container.
        local_path:  Destination path on this machine.
    """
    containers = _neon(
        ["docker", "ps", "--format", "{{.Names}}", "--filter", f"name={name}"]
    )
    if not containers:
        raise ValueError(f"No container matching '{name}' found on neon.")
    container = containers.splitlines()[0]
    local_file = Path(local_path)
    local_file.parent.mkdir(parents=True, exist_ok=True)
    payload = _ssh_output(NEON, ["docker", "exec", container, "cat", remote_path])
    local_file.write_bytes(payload)
    return f"Pulled neon:{container}:{remote_path} → {local_path}"


@mcp.tool()
def host_push(host: str, local_path: str, remote_path: str) -> str:
    """Copy a local file to a host (krypton, neon, or xenon).

    Args:
        host:        Target host — 'krypton', 'neon', or 'xenon'.
        local_path:  Path to the file on this machine.
        remote_path: Destination path on the host.
    """
    aliases = {"krypton": KRYPTON, "neon": NEON_SCP, "xenon": XENON}
    if host not in aliases:
        raise ValueError(f"Host must be 'krypton', 'neon', or 'xenon', got '{host}'")
    remote_parent = Path(remote_path).parent.as_posix()
    _ssh(aliases[host], f"mkdir -p {shlex.quote(remote_parent)}", timeout=10)
    _scp(local_path, f"{aliases[host]}:{remote_path}")
    return f"Pushed {local_path} → {host}:{remote_path}"


@mcp.tool()
def host_pull(host: str, remote_path: str, local_path: str) -> str:
    """Copy a file from a host (krypton, neon, or xenon) to local.

    Args:
        host:        Source host — 'krypton', 'neon', or 'xenon'.
        remote_path: Path to the file on the host.
        local_path:  Destination path on this machine.
    """
    aliases = {"krypton": KRYPTON, "neon": NEON_SCP, "xenon": XENON}
    if host not in aliases:
        raise ValueError(f"Host must be 'krypton', 'neon', or 'xenon', got '{host}'")
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
    _scp(f"{aliases[host]}:{remote_path}", local_path)
    return f"Pulled {host}:{remote_path} → {local_path}"


# ── Secrets (passage) ─────────────────────────────────────────────────────────


# @mcp.tool()
def secret_read(path: str) -> str:
    """Read a secret from passage (age-encrypted, git-synced via iCloud).

    Args:
        path: Passage path, e.g. 'servarr/radarr/api-key'.
    """
    return _passage(path)


# @mcp.tool()
def secret_list(prefix: str = "") -> list:
    """List secrets in passage.

    Args:
        prefix: Optional prefix to filter, e.g. 'servarr/'. Omit for all.
    """
    store = Path.home() / ".passage" / "store"
    paths = sorted(
        str(p.relative_to(store)).removesuffix(".age") for p in store.rglob("*.age")
    )
    if prefix:
        paths = [p for p in paths if p.startswith(prefix)]
    return paths


# ── Cloudflare API ────────────────────────────────────────────────────────────


# @mcp.tool()
def cf_api(method: str, endpoint: str, data: str = "") -> dict:
    """Call the Cloudflare API. Auth via passage (servarr/cloudflare/api-key-global).

    For Access operations, ``cf_access_apps()`` in cloudflare.py is a shorter
    shorthand that injects the account ID automatically.

    Args:
        method:   HTTP method (GET, POST, PUT, DELETE).
        endpoint: Path after /client/v4/, e.g. 'zones' or 'accounts/{id}/access/apps'.
        data:     Optional JSON string body for POST/PUT.
    """
    api_key = _passage("servarr/cloudflare/api-key-global")
    url = f"https://api.cloudflare.com/client/v4/{endpoint}"
    headers = {"X-Auth-Email": "darren.loasby@gmail.com", "X-Auth-Key": api_key}
    body = json.loads(data) if data else None
    resp = _http(url, method=method, headers=headers, data=body, timeout=15)
    return json.loads(resp)


# @mcp.tool()
def cf_access_apps() -> dict:
    """List all Cloudflare Access applications for the account."""
    account_id = _passage("servarr/cloudflare/account-id")
    return cf_api("GET", f"accounts/{account_id}/access/apps")


def _cf_active_tunnel() -> tuple[str, str]:
    """Find the active (remotely-managed) tunnel. Returns (account_id, tunnel_id)."""
    account_id = _passage("servarr/cloudflare/account-id")
    resp = cf_api("GET", f"accounts/{account_id}/cfd_tunnel?is_deleted=false")
    tunnels = resp.get("result", [])
    for t in tunnels:
        if t.get("remote_config") and t.get("status") == "healthy":
            return account_id, t["id"]
    # Fallback: any remotely-managed tunnel
    for t in tunnels:
        if t.get("remote_config"):
            return account_id, t["id"]
    raise ValueError(
        "No remotely-managed tunnel found. Available: "
        + ", ".join(
            f"{t['name']} ({t['id']}, config_src={t.get('config_src')})"
            for t in tunnels
        )
    )


# @mcp.tool()
def cf_tunnel_status() -> dict:
    """Show Cloudflare tunnel health, connections, and basic info."""
    account_id, tunnel_id = _cf_active_tunnel()
    resp = cf_api("GET", f"accounts/{account_id}/cfd_tunnel/{tunnel_id}")
    tunnel = resp.get("result", {})
    connections = tunnel.get("connections", [])
    return {
        "id": tunnel.get("id"),
        "name": tunnel.get("name"),
        "status": tunnel.get("status"),
        "config_src": tunnel.get("config_src"),
        "connections": [
            {
                "colo": c.get("colo_name"),
                "opened_at": c.get("opened_at"),
                "client_version": c.get("client_version"),
            }
            for c in connections
        ],
    }


# @mcp.tool()
def cf_tunnel_ingress() -> list:
    """List all Cloudflare tunnel ingress rules (hostname -> service mappings).

    Returns the current remotely-managed tunnel configuration including
    all hostname-to-service routing rules.
    """
    account_id, tunnel_id = _cf_active_tunnel()
    resp = cf_api("GET", f"accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations")
    config = resp.get("result", {}).get("config", {})
    ingress = config.get("ingress", [])
    return [
        {
            "hostname": rule.get("hostname", "(catch-all)"),
            "service": rule.get("service", ""),
            "path": rule.get("path", ""),
            **(
                {"originRequest": rule["originRequest"]}
                if rule.get("originRequest")
                else {}
            ),
        }
        for rule in ingress
    ]


# @mcp.tool()
def cf_tunnel_ingress_update(ingress: str) -> dict:
    """Update Cloudflare tunnel ingress rules (full replacement).

    Args:
        ingress: JSON string of ingress rules array. Each rule has 'hostname' and 'service'.
                 The last rule MUST be a catch-all with no hostname (e.g. {"service": "http_status:404"}).

    Example ingress JSON:
        [{"hostname": "app.529broo.me", "service": "http://192.168.86.88:81"},
         {"service": "http_status:404"}]

    WARNING: This replaces ALL ingress rules. Use cf_tunnel_ingress() first to get
    the current rules, modify them, then pass the full updated list.
    """
    account_id, tunnel_id = _cf_active_tunnel()
    rules = json.loads(ingress)
    if not rules or "hostname" in rules[-1]:
        raise ValueError(
            'Last ingress rule must be a catch-all with no hostname (e.g. {"service": "http_status:404"})'
        )
    payload = json.dumps({"config": {"ingress": rules}})
    return cf_api(
        "PUT",
        f"accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
        data=payload,
    )


# ── NPM (Nginx Proxy Manager) ────────────────────────────────────────────────


# @mcp.tool()
def npm_list_hosts() -> list:
    """List all NPM proxy hosts with domain names, forward targets, and SSL status.

    Requires passage secret: servarr/npm/admin-secret
    """
    return _npm_api("nginx/proxy-hosts")


# @mcp.tool()
def npm_get_host(host_id: int) -> dict:
    """Get a specific NPM proxy host configuration.

    Args:
        host_id: Proxy host ID (from npm_list_hosts).
    """
    return _npm_api(f"nginx/proxy-hosts/{host_id}")


# @mcp.tool()
def npm_create_host(payload_json: str) -> dict:
    """Create an NPM proxy host.

    Args:
        payload_json: Full JSON body for the POST request.
    """
    payload = _npm_payload(payload_json)
    if payload is None:
        raise ValueError("payload_json is required")
    return _npm_api("nginx/proxy-hosts", method="POST", data=payload)


# @mcp.tool()
def npm_update_host(host_id: int, payload_json: str) -> dict:
    """Update an NPM proxy host.

    Args:
        host_id: Proxy host ID.
        payload_json: Full JSON body for the PUT request.
    """
    payload = _npm_payload(payload_json)
    if payload is None:
        raise ValueError("payload_json is required")
    return _npm_api(f"nginx/proxy-hosts/{host_id}", method="PUT", data=payload)


# @mcp.tool()
def npm_delete_host(host_id: int) -> dict:
    """Delete an NPM proxy host."""
    return _npm_api(f"nginx/proxy-hosts/{host_id}", method="DELETE")


# @mcp.tool()
def npm_request(method: str, endpoint: str, payload_json: str = "") -> dict:
    """Call an authenticated NPM API endpoint.

    Use this for routes not covered by a dedicated helper.
    """
    payload = _npm_payload(payload_json)
    return _npm_api(endpoint.lstrip("/"), method=method.upper(), data=payload)


# ── Service APIs (*arr, Overseerr, Tautulli) ─────────────────────────────────


# @mcp.tool()
def arr_api(service: str, endpoint: str) -> dict:
    """Call a *arr API (Radarr, Sonarr, Prowlarr). URL from discovery, key from passage.

    Args:
        service:  Service name: 'radarr', 'sonarr', or 'prowlarr'.
        endpoint: API endpoint, e.g. 'movie', 'series', 'indexer', 'qualityProfile'.
    """
    allowed = {"radarr", "sonarr", "prowlarr"}
    svc = service.lower()
    if svc not in allowed:
        raise ValueError(f"Service must be one of {allowed}")

    base = _service_url(svc)
    api_key = _passage(f"servarr/{svc}/api-key")
    api_ver = "v1" if svc == "prowlarr" else "v3"
    prefix = "" if svc == "prowlarr" else "/primary"
    url = f"{base}{prefix}/api/{api_ver}/{endpoint}"
    resp = _http(url, headers={"X-Api-Key": api_key}, timeout=15)
    return json.loads(resp)


# @mcp.tool()
def overseerr_api(endpoint: str) -> dict:
    """Call the Overseerr API. URL from discovery, key from passage.

    Args:
        endpoint: API endpoint, e.g. 'request', 'status', 'search?query=batman'.
    """
    base = _service_url("overseerr")
    api_key = _passage("servarr/overseerr/api-key")
    resp = _http(
        f"{base}/api/v1/{endpoint}", headers={"X-Api-Key": api_key}, timeout=15
    )
    return json.loads(resp)


# @mcp.tool()
def tautulli_api(cmd: str) -> dict:
    """Call the Tautulli API. URL from discovery, key from passage.

    Args:
        cmd: API command, e.g. 'get_activity', 'get_history', 'get_libraries'.
    """
    base = _service_url("tautulli")
    api_key = _passage("servarr/tautulli/api-key")
    resp = _http(f"{base}/api/v2?apikey={api_key}&cmd={cmd}", timeout=15)
    return json.loads(resp)


# ── Unified containers ────────────────────────────────────────────────────────


# @mcp.tool()
def all_containers() -> list:
    """Unified view of all containers across krypton (Proxmox LXC), neon (HA Docker), and local Mac Docker.

    Returns a list with host, type, name, status for every container.
    """
    results = []

    try:
        for ct in _cached_discover().get("proxmox", {}).get("containers", []):
            results.append(
                {
                    "host": "krypton",
                    "type": "lxc",
                    "id": ct["id"],
                    "name": ct["name"],
                    "status": ct["status"],
                    "ip": ct.get("ip", ""),
                }
            )
    except Exception as e:
        results.append({"host": "krypton", "error": str(e)})

    try:
        for line in _neon("docker ps -a --format '{{json .}}'").splitlines():
            if line.strip():
                ct = json.loads(line)
                results.append(
                    {
                        "host": "neon",
                        "type": "docker",
                        "name": ct.get("Names", ""),
                        "image": ct.get("Image", ""),
                        "status": ct.get("Status", ""),
                    }
                )
    except Exception as e:
        results.append({"host": "neon", "error": str(e)})

    try:
        for line in _local(
            "docker ps -a --format '{{json .}}' 2>/dev/null"
        ).splitlines():
            if line.strip():
                ct = json.loads(line)
                results.append(
                    {
                        "host": "local",
                        "type": "docker",
                        "name": ct.get("Names", ""),
                        "image": ct.get("Image", ""),
                        "status": ct.get("Status", ""),
                    }
                )
    except Exception:
        pass  # no local Docker is fine

    return results


# @mcp.tool()
def local_containers() -> list:
    """List Docker containers on the local Mac (Docker Desktop or OrbStack)."""
    containers = []
    for line in _local("docker ps -a --format '{{json .}}'").splitlines():
        if line.strip():
            ct = json.loads(line)
            containers.append(
                {
                    "name": ct.get("Names", ""),
                    "image": ct.get("Image", ""),
                    "status": ct.get("Status", ""),
                    "ports": ct.get("Ports", ""),
                }
            )
    return containers


# @mcp.tool()
def local_exec(name: str, command: str) -> str:
    """Run a command inside a local Docker container.

    Args:
        name:    Container name or ID.
        command: Command to run inside the container.
    """
    return _local(f"docker exec {shlex.quote(name)} {command}")


# ── Entry point ───────────────────────────────────────────────────────────────



# ── Consolidated Tools ────────────────────────────────────────────────────────

@mcp.tool()
def infra_status(force: bool = False) -> dict:
    """Full infrastructure status: containers, services, mounts, and HA addons.
    Combines discovery, container listing, and mount health checks.
    """
    raw = _krypton(["servarr-discover"] + (["--force"] if force else []), timeout=20)
    data = json.loads(raw)
    
    # Add mount health
    try:
        rc = _krypton(["curl", "-s", "--max-time", "3", "localhost:5572/rc/noop"], timeout=8)
        data["mounts"] = {"decypharr": "healthy" if rc else "no response"}
    except: data["mounts"] = {"decypharr": "error"}
    
    return data

@mcp.tool()
def container_manage(
    name: str,
    action: Literal["status", "start", "stop", "restart", "logs", "exec", "db_query", "config_read"],
    param: str = ""
) -> Any:
    """Unified Proxmox LXC container management.
    
    Actions:
    - status, start, stop, restart: Lifecycle management.
    - logs: param = lines (default 100).
    - exec: param = command.
    - db_query: param = SQL query. Prefix with 'key:' for specific DB (e.g., 'library:SELECT...').
    - config_read: param = config key or path.
    """
    ctid, ct_name = _resolve_ct(name)
    if action in ("stop", "restart"): _guard_fuse_provider(ctid, ct_name)
    
    if action == "status":
        raw = _krypton(f"pct status {ctid}")
        return {"ctid": ctid, "name": ct_name, "status": raw.split(":")[-1].strip()}
    
    if action in ("start", "stop", "restart"):
        cmd = "reboot" if action == "restart" else action
        return _krypton(f"pct {cmd} {ctid}")

    if action == "exec":
        return _krypton(["pct", "exec", str(ctid), "--"] + shlex.split(param), timeout=30)

    if action == "logs":
        return container_logs(name, int(param or 100))

    if action == "db_query":
        db_key, _, sql = param.partition(":")
        if not sql: sql, db_key = db_key, ""
        return container_db_query(name, sql, db_key)

    if action == "config_read":
        return container_config_read(name, param)

@mcp.tool()
def host_manage(
    host: Literal["krypton", "neon", "xenon", "local"],
    name: str,
    action: Literal["status", "start", "stop", "restart", "logs", "exec", "run", "remove"],
    param: str = ""
) -> Any:
    """Fleet-wide service and Docker management.
    
    - host 'krypton': name = systemd service. Actions: status, start, stop, restart, logs.
    - host 'neon'/'xenon'/'local': name = Docker container. All actions supported.
    - 'run' action: param = JSON with 'image', 'ports', 'env', 'volumes', 'restart'.
    """
    if host == "krypton":
        if action == "logs": return service_logs(name, int(param or 100))
        if action == "status": return service_status(name)
        func = {"start": service_start, "stop": service_stop, "restart": service_restart}.get(action)
        if not func: raise ValueError(f"Action {action} not supported for krypton")
        return func(name)

    # Docker hosts
    if host == "local":
        if action == "status": return local_containers()
        if action == "exec": return local_exec(name, param)
        # Fallback to local docker CLI for others
        return _local(f"docker {action} {name} {param}")

    target = NEON if host == "neon" else XENON
    if action == "run":
        p = json.loads(param)
        return ha_container_run(p["image"], name, p.get("ports"), p.get("env"), p.get("volumes"), p.get("restart", "unless-stopped"))
    if action == "remove": return ha_container_remove(name)
    if action == "logs": return ha_logs(name, int(param or 100))
    if action == "exec": return ha_exec(name, param)
    if action == "restart": return ha_restart(name)
    
    return _ssh(target, f"docker {action} {name}")

@mcp.tool()
def provision_manage(
    name: str = "",
    action: Literal["audit", "provision", "test_logs", "auth_test"] = "audit"
) -> Any:
    """Infrastructure compliance and maintenance.
    
    - audit: Check standards (name=empty for all).
    - provision: Fix container standards (name=empty for all).
    - test_logs: End-to-end Loki shipping test.
    - auth_test: Global Loki credential check.
    """
    if action == "auth_test": return grafana_loki_auth_test()
    if action == "audit": return container_audit(name)
    if action == "provision": return container_provision(name) if name else container_provision_all()
    if action == "test_logs": return container_log_shipping_test(name)

@mcp.tool()
def api_call(
    provider: Literal["radarr", "sonarr", "prowlarr", "overseerr", "tautulli", "cloudflare", "npm"],
    endpoint: str,
    method: str = "GET",
    data: str = ""
) -> Any:
    """Unified API client for all services.
    
    - *arr: provider='radarr', endpoint='movie', etc.
    - cloudflare: endpoint='zones', data=JSON string.
    - npm: endpoint='nginx/proxy-hosts', data=JSON string.
    """
    if provider in ("radarr", "sonarr", "prowlarr"): return arr_api(provider, endpoint)
    if provider == "overseerr": return overseerr_api(endpoint)
    if provider == "tautulli": return tautulli_api(endpoint)
    if provider == "cloudflare": return cf_api(method, endpoint, data)
    if provider == "npm": return _npm_api(endpoint, method, json.loads(data) if data else None)

@mcp.tool()
def secret_manage(action: Literal["read", "list"], path: str = "") -> Any:
    """Access the passage secret store (read or list)."""
    return secret_list(path) if action == "list" else _passage(path)

@mcp.tool()
def template_manage(
    action: Literal["list", "pull", "create", "destroy"],
    name: str = "",
    template: str = "",
    **kwargs
) -> Any:
    """LXC template and container lifecycle management.

    `kwargs` is a JSON object string carrying the action-specific options, e.g.
    create: '{"vmid":110,"ip":"192.168.86.110/24","gw":"192.168.86.1","image":"sureshfizzy/cinesync:latest","mounts":[...],"env":{...}}'
    pull:   '{"image":"sureshfizzy/cinesync","tag":"latest"}'
    destroy:'{"purge":true}'
    """
    # FastMCP exposes **kwargs as a single string param; decode it into a dict.
    if "kwargs" in kwargs and isinstance(kwargs["kwargs"], str):
        try:
            kwargs = json.loads(kwargs["kwargs"]) if kwargs["kwargs"].strip() else {}
        except (json.JSONDecodeError, ValueError) as e:
            return {"error": f"kwargs must be a valid JSON object string: {e}"}
    if not isinstance(kwargs, dict):
        kwargs = {}
    if action == "list": return list_templates()
    if action == "pull": return pull_oci_template(kwargs.get("image"), kwargs.get("tag", "latest"), name)
    if action == "create": return container_create(name, template, **kwargs)
    if action == "destroy": return container_destroy(name, kwargs.get("purge", True))

@mcp.tool()
def file_transfer(
    source: str,
    dest: str,
    direction: Literal["push", "pull"] = "push"
) -> str:
    """Fleet-wide file transfer.
    
    - For LXC: name = container name or CTID.
    - For Docker: name = container name (neon only).
    - For Host: name = 'krypton', 'neon', 'xenon'.
    
    Example: container_push(name, local_path, remote_path)
    """
    return "Use container_push/container_pull, ha_push/ha_pull, or host_push/host_pull."

def main():
    """Entry point for uv tool."""
    import sys
    import os
    import subprocess

    transport = os.environ.get("SERVARR_MCP_TRANSPORT", "stdio")
    if transport != "stdio":
        mcp.run(transport=transport)
        return

    # If running interactively from terminal (both stdin and stdout are TTY),
    # launch in inspector instead of running as stdio server
    if sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty():
        try:
            source_file = os.path.abspath(__file__)
            subprocess.run(
                ["mcp", "dev", source_file],
                check=False
            )
        except Exception:
            # Fall back to running as MCP server if inspector fails
            mcp.run()
    else:
        # Being piped or run by MCP config, run as server
        mcp.run()


if __name__ == "__main__":
    main()
