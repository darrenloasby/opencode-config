"""Fleet hosts — the three-server constellation.

    xenon   inference   Apple Silicon AI runtime + passage store + servarr control-plane
    krypton media       Proxmox virtualization host (LXC containers, systemd services)
    neon    home        Home Assistant OS host (HA add-ons)
    local   this Mac    admin laptop / local docker

All three are first-class: use `run(host, cmd)` for an SSH command anywhere,
`push`/`pull` for files, and the `*_manage` dispatchers for service/docker ops.
"""

from __future__ import annotations

from . import _impl

# ── Canonical constellation ───────────────────────────────────────────────────
CONSTELLATION = {
    "xenon":   {"role": "inference", "ssh": _impl.XENON,   "scp": _impl.XENON,
                "kind": "docker", "desc": "Apple Silicon AI runtime, passage store, control-plane"},
    "krypton": {"role": "media",     "ssh": _impl.KRYPTON, "scp": _impl.KRYPTON,
                "kind": "proxmox", "desc": "Proxmox host: LXC containers + systemd services"},
    "neon":    {"role": "home",      "ssh": _impl.NEON,    "scp": _impl.NEON_SCP,
                "kind": "haos",    "desc": "Home Assistant OS host + add-ons"},
}
HOSTS = tuple(CONSTELLATION)


def _alias(host: str, *, scp: bool = False) -> str:
    h = host.lower()
    if h not in CONSTELLATION:
        raise ValueError(f"Unknown host {host!r}. Constellation: {HOSTS}")
    return CONSTELLATION[h]["scp" if scp else "ssh"]


def run(host: str, command: str, timeout: int = 30) -> str:
    """Run an SSH command on any constellation host (xenon/krypton/neon)."""
    return _impl._ssh(_alias(host), command, timeout=timeout)


def push(host: str, local_path: str, remote_path: str) -> str:
    """Copy a local file to any constellation host."""
    return _impl.host_push(host, local_path, remote_path)


def pull(host: str, remote_path: str, local_path: str) -> str:
    """Copy a file from any constellation host to local."""
    return _impl.host_pull(host, remote_path, local_path)


def constellation() -> dict:
    """Describe the three-server constellation (roles, ssh aliases, kind)."""
    return CONSTELLATION


# ── Re-exported implementation functions ──────────────────────────────────────
service_status = _impl.service_status
service_logs = _impl.service_logs
service_restart = _impl.service_restart
service_start = _impl.service_start
service_stop = _impl.service_stop
local_containers = _impl.local_containers
local_exec = _impl.local_exec
host_push = _impl.host_push
host_pull = _impl.host_pull
host_manage = _impl.host_manage

__all__ = [
    "CONSTELLATION", "HOSTS", "run", "push", "pull", "constellation",
    "service_status", "service_logs", "service_restart", "service_start",
    "service_stop", "local_containers", "local_exec",
    "host_push", "host_pull", "host_manage",
]
