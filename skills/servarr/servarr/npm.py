"""Nginx Proxy Manager — safe, intent-level wrappers.

Reads (`list_hosts`, `get_host`) pass through. Mutations snapshot first and
guard against losing routes:

- ``host_upsert`` updates a host in place instead of delete-then-create.
- ``host_delete`` requires ``confirm=True`` and refuses to remove the last
  proxy host for a domain.
"""

from __future__ import annotations

import json
from typing import Any

from . import _impl
from ._safe import DestructiveChange, snapshot

# ── Re-exported reads ─────────────────────────────────────────────────────────
list_hosts = _impl.npm_list_hosts
get_host = _impl.npm_get_host
npm_request = _impl.npm_request


def _domains(host: dict) -> list[str]:
    return host.get("domain_names", []) or []


def list_certificates() -> list:
    """All NPM certificates (used to find the wildcard *.529broo.me cert)."""
    return npm_request("GET", "nginx/certificates")


def wildcard_cert_id(domain_suffix: str = "529broo.me") -> int:
    """Return the certificate id for the wildcard *.<suffix> cert (0 if none)."""
    for cert in list_certificates():
        names = cert.get("domain_names", []) or []
        if any(n == f"*.{domain_suffix}" or n.endswith(domain_suffix) for n in names):
            return cert.get("id", 0)
    return 0


def proxy_payload(
    domain: str,
    forward_host: str,
    forward_port: int,
    *,
    forward_scheme: str = "http",
    certificate_id: int | None = None,
    ssl_forced: bool = False,
    websockets: bool = True,
    block_exploits: bool = True,
) -> dict:
    """Build a sane NPM proxy-host payload with the consistent SSL policy.

    SSL policy (stops the recurring redirect loops):
      * Cloudflare terminates public TLS on the WAN/tunnel path; cloudflared
        connects to this NPM host over **http** per the tunnel ingress rule.
      * NPM holds the wildcard *.529broo.me cert (auto-detected) so direct LAN
        HTTPS works, BUT **ssl_forced defaults to False** — NPM must not
        http->https redirect, or the tunnel origin loops. Leave it False unless
        you really know the whole path is HTTPS end-to-end.
      * HSTS stays off for the same reason.

    Pass the dict directly to host_upsert, or use add_service().
    """
    if certificate_id is None:
        certificate_id = wildcard_cert_id() or 0
    return {
        "domain_names": [domain],
        "forward_scheme": forward_scheme,
        "forward_host": forward_host,
        "forward_port": int(forward_port),
        "access_list_id": "0",
        # cert attached for LAN HTTPS, but never force-redirect (loop guard)
        "certificate_id": certificate_id or 0,
        "ssl_forced": bool(ssl_forced),
        "http2_support": bool(certificate_id),
        "block_exploits": block_exploits,
        "caching_enabled": False,
        "allow_websocket_upgrade": websockets,
        "hsts_enabled": False,
        "hsts_subdomains": False,
        "meta": {"letsencrypt_agree": False, "dns_challenge": False},
        "advanced_config": "",
        "locations": [],
        "enabled": True,
    }


def add_service(
    domain: str,
    forward_host: str,
    forward_port: int,
    *,
    dry_run: bool = False,
    **kwargs,
) -> dict:
    """Create-or-update the NPM proxy host for a service (safe upsert).

    Builds the payload (wildcard SSL auto-detected) and routes through
    host_upsert, so an existing host for the domain is updated in place rather
    than deleted/recreated. ``dry_run=True`` previews without writing.
    """
    payload = proxy_payload(domain, forward_host, forward_port, **kwargs)
    return host_upsert(payload, dry_run=dry_run)


def host_upsert(payload: str | dict, *, dry_run: bool = False) -> dict:
    """Create a host, or update the existing one that already serves its domains.

    Accepts a JSON string or dict. Avoids delete-then-create. If any domain
    already maps to a host, that host is PUT-updated; otherwise POST a new one.
    Snapshots the prior host on update.
    """
    payload_dict = _impl._npm_payload(payload)
    if payload_dict is None:
        raise ValueError("payload is required")
    wanted = set(payload_dict.get("domain_names", []) or [])
    if not wanted:
        raise ValueError("payload must include domain_names")

    existing = next(
        (h for h in list_hosts() if wanted & set(_domains(h))), None
    )
    if existing is None:
        if dry_run:
            return {"dry_run": True, "action": "create", "payload": payload_dict}
        return {"action": "created", "result": _impl.npm_create_host(json.dumps(payload_dict))}

    host_id = existing["id"]
    if dry_run:
        return {"dry_run": True, "action": "update", "host_id": host_id,
                "from": existing, "to": payload_dict}
    snap = snapshot("npm-host", str(host_id), existing)
    result = _impl.npm_update_host(host_id, json.dumps(payload_dict))
    return {"action": "updated", "host_id": host_id, "snapshot": snap, "result": result}


def host_delete(host_id: int, *, confirm: bool = False) -> dict:
    """Delete a proxy host. Destructive — requires ``confirm=True``.

    Refuses to delete the last remaining host for any of its domains (would
    leave that domain with no route). Snapshots the host before deleting.
    """
    host = get_host(host_id)
    domains = set(_domains(host))
    if not confirm:
        raise DestructiveChange(
            f"Refusing to delete NPM host {host_id} ({sorted(domains)}) without "
            "confirm=True. Use host_upsert to change a route in place instead.",
        )

    all_hosts = list_hosts()
    for d in domains:
        others = [h for h in all_hosts if h["id"] != host_id and d in set(_domains(h))]
        if not others:
            raise DestructiveChange(
                f"Refusing to delete host {host_id}: it is the only proxy host "
                f"serving domain {d!r}. Deleting it removes that route entirely.",
            )

    snap = snapshot("npm-host", f"{host_id}-deleted", host)
    result = _impl.npm_delete_host(host_id)
    return {"action": "deleted", "host_id": host_id, "snapshot": snap, "result": result}
