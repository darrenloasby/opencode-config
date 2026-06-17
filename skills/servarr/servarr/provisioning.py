"""New-host / new-service provisioning — the codified, guarded workflow.

This encodes the standing rules so an agent follows them by construction:

* krypton containers are deployed as **OCI images** or a **plain LXC bundle** —
  **never docker-in-LXC** (rejected here). Local docker is not a deploy target.
* every container gets a **fixed IP = 192.168.86.<ctid>** (DHCP pool is .10–.99,
  so containers at .100+ never collide).
* every container gets the **standard setup**: DNS, passage, auto-login, and
  console via the Proxmox host (via ``container_provision``).
* networking follows split-horizon: LAN ``*.529broo.me`` already resolves
  straight to NPM, so local access never needs login. The only per-service
  choices are: NPM proxy host, Cloudflare tunnel ingress (for WAN), and whether
  to gate the WAN path with a Cloudflare Access app.

``new_service`` builds a **plan first** (dry-run) and only mutates when
``apply=True``. Every mutating step delegates to a guarded helper
(``npm.add_service``, ``cloudflare.ingress_add``, ``cloudflare.access_app_create``)
that snapshots and refuses destructive diffs.
"""

from __future__ import annotations

from typing import Any, Literal

from . import _impl, cloudflare, containers, npm
from ._safe import DestructiveChange, snapshot

LAN_NET = "192.168.86"
LAN_GW = "192.168.86.1"
NPM_HOST = "192.168.86.88"  # NPM/CT 107 — tunnel + LAN both terminate here

Auth = Literal["none", "external", "always"]
Deploy = Literal["oci", "lxc", "existing"]


# ── Fixed-IP convention ───────────────────────────────────────────────────────

def static_ip(ctid: int, *, cidr: int = 24, gw: str = LAN_GW) -> str:
    """Container IP config string for the convention: 192.168.86.<ctid>."""
    if not (100 <= ctid <= 254):
        raise ValueError(
            f"ctid {ctid} out of range for last-octet IP convention (100–254). "
            "DHCP clients use .10–.99; containers must be .100+."
        )
    return f"{LAN_NET}.{ctid}/{cidr},gw={gw}"


def audit_static_ips() -> dict:
    """Read-only: report containers whose IP drifts from 192.168.86.<ctid>.

    Surfaces the current static/DHCP mess. Does not change anything.
    """
    rows = []
    for ct in containers.list_containers():
        ctid = ct["id"]
        want = f"{LAN_NET}.{ctid}" if 100 <= ctid <= 254 else "(out-of-range)"
        have = ct.get("ip", "unknown")
        rows.append({"id": ctid, "name": ct["name"], "have": have,
                     "want": want, "ok": have == want})
    drift = [r for r in rows if not r["ok"]]
    return {"total": len(rows), "compliant": len(rows) - len(drift),
            "drift_count": len(drift), "drift": drift, "all": rows}


def _rewrite_net0(net0: str, target_ip: str) -> str:
    """Rebuild a pct net0 string with ip=<target>/24, preserving MAC/bridge/etc."""
    parts = [p for p in net0.split(",") if p]
    kept, have_gw = [], False
    for p in parts:
        key = p.split("=", 1)[0]
        if key == "ip":
            kept.append(f"ip={target_ip}/24")
        elif key == "gw":
            kept.append(f"gw={LAN_GW}")
            have_gw = True
        else:
            kept.append(p)  # name, bridge, hwaddr (keep MAC), ip6, type, firewall...
    if not have_gw:
        kept.append(f"gw={LAN_GW}")
    return ",".join(kept)


def align_ip(
    name_or_ctid: str | int,
    *,
    apply: bool = False,
    confirm: bool = False,
    update_npm: bool = True,
    reboot: bool = True,
) -> dict:
    """Change ONE container's IP to the convention 192.168.86.<ctid>.

    Rewrites the Proxmox ``net0`` (keeping the MAC), reboots so the guest picks
    it up, and updates any NPM proxy host that forwarded to the old IP. The
    container ID does NOT change.

    Disruptive — requires ``confirm=True``. FUSE providers (CT 127/128) reboot
    cascades to consumers, so they need confirm too and are flagged. Plex (CT
    100) has a router port-forward that this CANNOT change — it is surfaced as a
    manual follow-up. ``apply=False`` returns the plan only.
    """
    ctid, name = _impl._resolve_ct(str(name_or_ctid))
    if not (100 <= ctid <= 254):
        raise ValueError(f"CT {ctid} ({name}) is outside the .100–.254 convention range")
    target = f"{LAN_NET}.{ctid}"

    net0 = ""
    for line in _impl._krypton(f"pct config {ctid}").splitlines():
        if line.startswith("net0:"):
            net0 = line.split(":", 1)[1].strip()
            break
    cur_ip = next((kv.split("=", 1)[1].split("/")[0]
                   for kv in net0.split(",") if kv.startswith("ip=")), "unknown")

    npm_updates = []
    if update_npm and cur_ip not in ("unknown", "dhcp"):
        npm_updates = [{"id": h["id"], "domains": h.get("domain_names", [])}
                       for h in npm.list_hosts() if h.get("forward_host") == cur_ip]

    manual = []
    if ctid == 100 or name == "plex":
        manual.append("Plex has a router port-forward to its IP — update the router manually.")
    is_fuse = ctid in _impl.FUSE_PROVIDERS

    plan = {"ctid": ctid, "name": name, "from": cur_ip, "to": target,
            "reboot": reboot, "is_fuse_provider": is_fuse,
            "npm_hosts_to_update": npm_updates, "manual_followups": manual,
            "new_net0": _rewrite_net0(net0, target) if net0 else None}

    if cur_ip == target:
        return {"action": "noop", "reason": f"{name} already at {target}", "plan": plan}
    if not apply:
        return {"dry_run": True, "plan": plan}

    # ── apply ────────────────────────────────────────────────────────────────
    if not net0:
        raise ValueError(f"Could not read net0 for CT {ctid}")
    if not confirm:
        raise DestructiveChange(
            f"Changing CT {ctid} ({name}) IP {cur_ip}->{target} reboots it and "
            f"updates {len(npm_updates)} NPM host(s). Pass confirm=True. {manual}",
            diff=plan,
        )
    if is_fuse and not confirm:
        raise DestructiveChange(
            f"CT {ctid} is a FUSE provider — reboot cascades to consumers.", diff=plan)

    snap = snapshot("container-net", str(ctid), {"net0": net0, "ip": cur_ip})
    _impl._krypton(f"pct set {ctid} -net0 {_rewrite_net0(net0, target)}", timeout=20)
    if reboot:
        _impl._krypton(f"pct reboot {ctid}", timeout=30)
    npm_done = []
    for h in npm_updates:
        full = npm.get_host(h["id"])
        full["forward_host"] = target
        npm_done.append(npm.host_upsert(_impl.json.dumps(full)))
    return {"applied": True, "ctid": ctid, "name": name, "from": cur_ip, "to": target,
            "snapshot": snap, "npm_updated": npm_done, "manual_followups": manual}


def align_all_ips(*, apply: bool = False, confirm: bool = False,
                  skip: tuple[str, ...] = ("plex",)) -> dict:
    """Plan (or apply, one-by-one) IP alignment for every drifting container.

    ``apply=False`` returns the full batch plan. With ``apply=True, confirm=True``
    it aligns each drifting container in turn, SKIPPING ``skip`` (plex by default,
    because of its router port-forward) and continuing past individual failures.
    """
    audit = audit_static_ips()
    targets = [r for r in audit["drift"]
               if r["want"] != "(out-of-range)" and r["name"] not in skip]
    if not apply:
        return {"dry_run": True, "to_align": targets,
                "skipped": [r["name"] for r in audit["drift"] if r["name"] in skip],
                "note": "review, then apply=True confirm=True to run one-by-one"}

    results = []
    for r in targets:
        try:
            results.append(align_ip(r["id"], apply=True, confirm=confirm))
        except Exception as exc:  # keep going; report per-container
            results.append({"ctid": r["id"], "name": r["name"],
                            "error": type(exc).__name__, "message": str(exc)})
    return {"applied": True, "count": len(results), "results": results}


# ── New-service orchestrator ──────────────────────────────────────────────────

def new_service(
    name: str,
    *,
    port: int,
    deployment: Deploy = "oci",
    ctid: int = 0,
    image: str = "",
    template: str = "",
    domain: str = "",
    external: bool = True,
    auth: Auth = "none",
    emails: list[str] | None = None,
    apply: bool = False,
    confirm: bool = False,
) -> dict:
    """Plan (and optionally apply) onboarding of a new service.

    Args:
        name:        Service / container hostname.
        port:        Upstream port NPM forwards to.
        deployment:  'oci' (image), 'lxc' (plain bundle), or 'existing' (already
                     deployed — skip container creation). 'docker' is rejected.
        ctid:        Container ID; the static IP becomes 192.168.86.<ctid>.
        image:       OCI image (deployment='oci'), e.g. 'org/app:latest'.
        template:    LXC/OCI template name (deployment='lxc' or pulled OCI).
        domain:      Public hostname; defaults to '<name>.529broo.me'.
        external:    Add a Cloudflare tunnel ingress rule for WAN access.
        auth:        'none' = straight to NPM, no login (most services);
                     'external' = login on WAN only, local bypassed by DNS (auth2);
                     'always' = require login everywhere; do not rely on local
                     bypass (auth1, identity-linked).
        apply:       False = return the plan only (dry-run). True = execute.
        confirm:     Required passthrough for any destructive sub-step.

    Returns the plan plus, when applied, the result of each step.
    """
    if deployment == "docker":  # type: ignore[comparison-overlap]
        raise ValueError(
            "docker-in-LXC is not allowed on krypton. Use deployment='oci' "
            "(image) or 'lxc' (plain bundle)."
        )
    fqdn = domain or f"{name}.529broo.me"
    plan: list[dict] = []

    # 1) container
    if deployment == "existing":
        ct = _impl._resolve_ct(name)
        ip = next((c["ip"] for c in containers.list_containers() if c["name"] == name), "?")
        plan.append({"step": "container", "action": "reuse", "ctid": ct[0], "ip": ip})
        forward_host = ip
    else:
        if not ctid:
            plan.append({"step": "container", "action": "needs-ctid",
                         "note": "pass ctid so the static IP 192.168.86.<ctid> can be set"})
            forward_host = f"{LAN_NET}.<ctid>"
        else:
            ipcfg = static_ip(ctid)
            forward_host = f"{LAN_NET}.{ctid}"
            plan.append({
                "step": "container", "action": "create", "kind": deployment,
                "ctid": ctid, "ip": ipcfg, "image": image, "template": template,
                "standard_setup": ["dns", "passage", "auto-login", "console-via-proxmox"],
                "note": "deployed as OCI/LXC (never docker-in-LXC); container_provision applies standard setup",
            })

    # 2) NPM proxy host
    plan.append({"step": "npm", "action": "upsert",
                 "domain": fqdn, "forward": f"http://{forward_host}:{port}"})

    # 3) WAN tunnel ingress
    if external:
        plan.append({"step": "ingress", "action": "add",
                     "hostname": fqdn, "service": f"http://{NPM_HOST}:80"})

    # 4) Cloudflare Access (login)
    if auth == "none":
        plan.append({"step": "access", "action": "skip",
                     "note": "no login — straight to NPM (local via split-horizon DNS, WAN via tunnel)"})
    else:
        plan.append({
            "step": "access", "action": "create-app", "hostname": fqdn,
            "mode": auth,
            "lan_bypass_cidr": f"{LAN_NET}.0/24" if auth == "external" else None,
            "note": ("auth2: login on WAN, local bypassed by DNS"
                     if auth == "external" else
                     "auth1: require login everywhere; do NOT rely on local bypass"),
        })

    # 5) AdGuard / LAN DNS
    plan.append({"step": "dns-lan", "action": "wildcard-covers",
                 "note": "*.529broo.me already rewrites to NPM in AdGuard — no per-service entry"})

    if not apply:
        return {"dry_run": True, "service": name, "fqdn": fqdn, "plan": plan}

    # ── apply ────────────────────────────────────────────────────────────────
    results: list[dict] = []
    for step in plan:
        kind = step["step"]
        if kind == "container" and step["action"] == "create":
            if not ctid:
                raise ValueError("ctid required to apply container creation")
            results.append({"container": containers.container_create(
                name, template or image, vmid=ctid, ip=static_ip(ctid),
                ostype="unmanaged" if deployment == "oci" else "debian")})
        elif kind == "npm":
            results.append({"npm": npm.add_service(fqdn, forward_host, port)})
        elif kind == "ingress" and external:
            results.append({"ingress": cloudflare.ingress_add(fqdn, f"http://{NPM_HOST}:80")})
        elif kind == "access" and step["action"] == "create-app":
            results.append({"access": cloudflare.access_app_create(
                fqdn, emails=emails, lan_bypass_cidr=step.get("lan_bypass_cidr"))})
    return {"applied": True, "service": name, "fqdn": fqdn, "results": results}
