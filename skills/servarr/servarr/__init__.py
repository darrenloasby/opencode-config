"""servarr — infrastructure control library (minimal-surface, code-execution bundle).

Import the domain module you need and call its functions directly:

    from servarr import cloudflare, npm, containers, infra

    infra.infra_status()
    cloudflare.ingress_add("app.529broo.me", "http://192.168.86.88:81")  # safe, snapshots
    npm.host_delete(12, confirm=True)                                    # guarded

Modules:
    infra       discovery + combined status
    containers  Proxmox LXC lifecycle / logs / exec / db / templates
    hosts       krypton systemd + docker on neon/xenon/local
    ha          Home Assistant OS containers (neon)
    services    *arr / overseerr / tautulli
    cloudflare  Cloudflare API + SAFE tunnel-ingress / access wrappers
    npm         Nginx Proxy Manager + SAFE host upsert/delete
    secrets     passage read / list
    provision   audit / provision / loki shipping

Mutations to cloudflare/npm route through servarr._safe: they snapshot prior
state to .snapshots/ and refuse to remove or replace live config unless you
pass confirm=True. See references/safety.md.
"""

from __future__ import annotations

from . import (  # noqa: F401
    cloudflare,
    containers,
    ha,
    hosts,
    infra,
    npm,
    provision,
    provisioning,
    secrets,
    services,
)
from . import _safe  # noqa: F401

__all__ = [
    "cloudflare", "containers", "ha", "hosts", "infra",
    "npm", "provision", "provisioning", "secrets", "services", "_safe",
]
