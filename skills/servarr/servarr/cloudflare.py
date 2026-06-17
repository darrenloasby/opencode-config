"""Cloudflare API — safe, intent-level wrappers.

Raw access (`cf_api`, `cf_tunnel_status`, `cf_access_apps`) is re-exported for
reads. **Mutations go through the safe helpers below**, which snapshot prior
state and refuse to remove/replace rules unless ``confirm=True``.

This is the module that would have prevented the *wimba* incident: there is no
easy "replace the whole ingress" call. You add or remove one rule at a time,
and the catch-all is always preserved.
"""

from __future__ import annotations

import json
from typing import Any

from . import _impl
from ._safe import DestructiveChange, guard_destructive, snapshot

# ── Re-exported reads ─────────────────────────────────────────────────────────
cf_api = _impl.cf_api
cf_access_apps = _impl.cf_access_apps
cf_tunnel_status = _impl.cf_tunnel_status
cf_tunnel_ingress = _impl.cf_tunnel_ingress  # simplified human view
ingress_list = _impl.cf_tunnel_ingress  # returns a list[dict] — index it, don't .get()


# ── Internal: raw ingress read/write ─────────────────────────────────────────

def _raw_ingress() -> tuple[str, str, list[dict]]:
    """Return (account_id, tunnel_id, raw_ingress_rules) for the active tunnel."""
    account_id, tunnel_id = _impl._cf_active_tunnel()
    resp = cf_api("GET", f"accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations")
    rules = resp.get("result", {}).get("config", {}).get("ingress", []) or []
    return account_id, tunnel_id, rules


def _assert_catch_all(rules: list[dict]) -> None:
    """A tunnel config is only valid if it ends in a hostname-less catch-all."""
    if not rules or "hostname" in rules[-1]:
        raise ValueError(
            "Ingress invariant violated: last rule must be a catch-all with no "
            'hostname (e.g. {"service": "http_status:404"}).'
        )
    if len(rules) == 1:
        raise ValueError(
            "Ingress invariant violated: refusing a config that is ONLY the "
            "catch-all (this is exactly the wimba-nuke failure mode)."
        )


def _put_ingress(account_id: str, tunnel_id: str, rules: list[dict]) -> dict:
    payload = json.dumps({"config": {"ingress": rules}})
    return cf_api(
        "PUT",
        f"accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
        data=payload,
    )


# ── Safe ingress mutations ────────────────────────────────────────────────────

def ingress_add(hostname: str, service: str, *, dry_run: bool = False) -> dict:
    """Add or update a single ingress rule, preserving every other rule.

    Read-modify-write: inserts ``hostname -> service`` just before the catch-all
    (or updates the service if the hostname already exists). Snapshots the prior
    config first. Pure additions/updates never remove rules, so this needs no
    ``confirm``. Set ``dry_run=True`` to preview the diff without applying.
    """
    account_id, tunnel_id, old = _raw_ingress()
    new = [dict(r) for r in old]

    existing = next((r for r in new if r.get("hostname") == hostname), None)
    if existing is not None:
        existing["service"] = service
        action = "updated"
    else:
        # insert before the trailing catch-all
        new.insert(len(new) - 1, {"hostname": hostname, "service": service})
        action = "added"

    _assert_catch_all(new)
    summary = guard_destructive(old, new, id_key="hostname", label="cf tunnel ingress")

    if dry_run:
        return {"dry_run": True, "action": action, "diff": summary, "new_ingress": new}

    snap = snapshot("cloudflare-ingress", tunnel_id, old)
    result = _put_ingress(account_id, tunnel_id, new)
    return {"action": action, "hostname": hostname, "snapshot": snap, "diff": summary,
            "result": result}


def ingress_remove(hostname: str, *, confirm: bool = False) -> dict:
    """Remove a single ingress rule by hostname.

    This is destructive (loses a route), so it requires ``confirm=True``. The
    catch-all can never be removed. Snapshots prior config before applying.
    """
    account_id, tunnel_id, old = _raw_ingress()
    if not any(r.get("hostname") == hostname for r in old):
        return {"action": "noop", "reason": f"no ingress rule for {hostname}"}

    new = [r for r in old if r.get("hostname") != hostname]
    _assert_catch_all(new)
    # protect the catch-all explicitly; removal is destructive -> needs confirm
    summary = guard_destructive(
        old, new, id_key="hostname", min_keep=2, confirm=confirm,
        label="cf tunnel ingress",
    )

    snap = snapshot("cloudflare-ingress", tunnel_id, old)
    result = _put_ingress(account_id, tunnel_id, new)
    return {"action": "removed", "hostname": hostname, "snapshot": snap,
            "diff": summary, "result": result}


def cf_api_write(method: str, endpoint: str, data: str = "", *, confirm: bool = False) -> dict:
    """Guarded escape hatch for arbitrary mutating Cloudflare calls.

    GET passes straight through. Any DELETE — or PUT/PATCH to a tunnel
    *configurations* endpoint (full-replace footgun) — requires ``confirm=True``.
    Prefer ``ingress_add``/``ingress_remove`` for tunnel routing.
    """
    m = method.upper()
    if m == "GET":
        return cf_api("GET", endpoint)

    risky = m == "DELETE" or (m in ("PUT", "PATCH") and "configurations" in endpoint)
    if risky and not confirm:
        raise DestructiveChange(
            f"Refusing {m} {endpoint} without confirm=True. This can replace or "
            "delete live config. For tunnel routing use ingress_add/ingress_remove.",
        )
    if "configurations" in endpoint and m in ("PUT", "PATCH"):
        # snapshot whatever the current tunnel config is before clobbering it
        try:
            _, tunnel_id, old = _raw_ingress()
            snapshot("cloudflare-ingress", f"{tunnel_id}-raw-put", old)
        except Exception:
            pass
    return cf_api(m, endpoint, data)


# ── Access applications (login gating) ────────────────────────────────────────
#
# Local access never reaches Cloudflare Access: split-horizon DNS sends LAN
# clients straight to NPM. So an Access app only gates the WAN/tunnel path.
# Apps attach to the existing hostname — no separate CNAME is needed.

def _account_id() -> str:
    return _impl._passage("servarr/cloudflare/account-id")


def access_apps() -> list:
    """List Access applications (apps attach to hostnames, not separate CNAMEs)."""
    return cf_api("GET", f"accounts/{_account_id()}/access/apps").get("result", [])


def access_app_create(
    hostname: str,
    *,
    name: str = "",
    emails: list[str] | None = None,
    session_duration: str = "24h",
    lan_bypass_cidr: str | None = None,
) -> dict:
    """Create a self-hosted Access app on ``hostname`` requiring login.

    Idempotent: if an app already covers the hostname it is returned unchanged.
    Adds an *allow* policy restricted to ``emails`` (default the account owner)
    — this is the auth1 "always require login, identity linked" case. Pass
    ``lan_bypass_cidr`` (e.g. "192.168.86.0/24") to also add a *bypass* policy
    for LAN source IPs — the auth2 "secure externally, bypass locally" case
    (usually unnecessary since split-horizon DNS already bypasses locally).

    Creation is additive (no existing app is removed), so no confirm is needed;
    the prior app list is snapshotted first.
    """
    account_id = _account_id()
    existing = next((a for a in access_apps() if a.get("domain") == hostname), None)
    if existing:
        return {"action": "exists", "app": existing}

    snapshot("cloudflare-access", "apps", access_apps())
    app = cf_api(
        "POST",
        f"accounts/{account_id}/access/apps",
        data=json.dumps({
            "name": name or hostname,
            "domain": hostname,
            "type": "self_hosted",
            "session_duration": session_duration,
        }),
    ).get("result", {})
    app_id = app.get("id")

    policies = []
    if lan_bypass_cidr:
        policies.append(cf_api(
            "POST",
            f"accounts/{account_id}/access/apps/{app_id}/policies",
            data=json.dumps({
                "name": "lan-bypass", "decision": "bypass",
                "include": [{"ip": {"ip": lan_bypass_cidr}}],
            }),
        ).get("result", {}))

    include = [{"email": {"email": e}} for e in (emails or ["darren.loasby@gmail.com"])]
    policies.append(cf_api(
        "POST",
        f"accounts/{account_id}/access/apps/{app_id}/policies",
        data=json.dumps({"name": "require-login", "decision": "allow", "include": include}),
    ).get("result", {}))

    return {"action": "created", "app": app, "policies": policies}


def access_app_delete(app_id: str, *, confirm: bool = False) -> dict:
    """Delete an Access app. Destructive — requires ``confirm=True``."""
    if not confirm:
        raise DestructiveChange(
            f"Refusing to delete Access app {app_id} without confirm=True.",
        )
    account_id = _account_id()
    snapshot("cloudflare-access", f"app-{app_id}-deleted", access_apps())
    return cf_api("DELETE", f"accounts/{account_id}/access/apps/{app_id}")


def access_app_update(app_id: str, **fields: Any) -> dict:
    """Update an Access application.

    Fetches the current app, merges ``fields``, and PUTs it back.  The
    ``type`` field is always forced to ``"self_hosted"`` (required by the
    Cloudflare API even on updates).  Snapshots prior state.

    Example::

        access_app_update("abc123", session_duration="12h")
    """
    account_id = _account_id()
    current = cf_api("GET", f"accounts/{account_id}/access/apps/{app_id}").get("result", {})
    if not current:
        raise ValueError(f"Access app {app_id} not found")

    snapshot("cloudflare-access", f"app-{app_id}-updated", current)
    merged = {**current, **fields, "type": "self_hosted"}
    return cf_api(
        "PUT",
        f"accounts/{account_id}/access/apps/{app_id}",
        data=json.dumps(merged),
    )
