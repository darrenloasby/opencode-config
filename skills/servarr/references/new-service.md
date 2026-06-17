# New-service / new-host runbook

The codified version lives in `servarr.provisioning`. Use
`provisioning.new_service(...)` — it builds a **plan first** (dry-run) and only
mutates with `apply=True`. This doc is the reasoning behind the plan.

## Standing rules (enforced in code)

1. **Deployment:** krypton containers are **OCI image** or **plain LXC bundle** —
   **never docker-in-LXC** (`deployment='docker'` is rejected). Local docker is a
   mess and not a deploy target. (`deployment='oci' | 'lxc' | 'existing'`.)
2. **Fixed IP:** every container's IP is `192.168.86.<ctid>` (`static_ip(ctid)`).
   DHCP clients are confined to `.10–.99`, so containers at `.100+` never clash.
   `provisioning.audit_static_ips()` reports current drift (read-only).

   To FIX drift (change the IP, not the container ID):
   `provisioning.align_ip(name_or_ctid)` plans it (rewrites `net0` keeping the
   MAC, reboots, updates NPM `forward_host`); add `apply=True, confirm=True` to
   run. `align_all_ips()` plans the whole batch and applies one-by-one with
   `apply=True, confirm=True`, **skipping plex** (CT 100 has a router
   port-forward that must be changed by hand). FUSE providers (127/128) reboot
   cascades to consumers — flagged and confirm-gated.
3. **Standard setup:** every container gets DNS, passage, auto-login, and console
   via the Proxmox host — applied by `containers.container_provision()`. Note:
   OCI images don't always inherit what the docker runtime used to maintain
   (esp. DNS), so verify DNS after create.

## The decision tree

```text
new service
 ├─ deploy: OCI image  (preferred)   or  plain LXC bundle      [never docker-in-LXC]
 ├─ assign static IP 192.168.86.<ctid>, run standard provision
 ├─ NPM proxy host:  <name>.529broo.me  ->  http://192.168.86.<ctid>:<port>
 ├─ external (WAN)?  yes -> cloudflare.ingress_add(<fqdn>, http://192.168.86.88:80)
 ├─ login needed?
 │    none      -> straight to NPM, no Access app          (most services)
 │    external  -> Access app on <fqdn>; local bypassed by split-horizon DNS  (auth2)
 │    always    -> Access app; do NOT rely on local bypass (auth1, identity-linked)
 └─ LAN DNS:  *.529broo.me already rewrites to NPM in AdGuard — nothing per-service
```

`none` vs `external` vs `always` = the `auth=` argument.

## SSL — the consistent policy (stops the redirect loops)

The recurring "broken SSL redirect" comes from NPM force-redirecting http→https on
the tunnel origin path. The fixed policy:

- **Cloudflare terminates public TLS.** With a named tunnel, `cloudflared`
  connects to the origin over **http** (`service: http://192.168.86.88:80`) and
  Cloudflare serves HTTPS to the public. Ingress rules stay `http://…:80`,
  consistent with every existing rule.
- **NPM holds the wildcard `*.529broo.me` cert** (auto-detected by
  `npm.wildcard_cert_id()`) so direct LAN HTTPS works — but
  **`ssl_forced=False`** and **HSTS off**. NPM must not redirect, or the tunnel
  origin loops. `npm.proxy_payload()` / `npm.add_service()` default to this.
- Only set `ssl_forced=True` if you have deliberately made the *entire* path
  HTTPS end-to-end (origin https on `:443`).

## Why split-horizon means "no local login fuss"

LAN `*.529broo.me` resolves straight to NPM (`192.168.86.88`) via the AdGuard
rewrite and never touches Cloudflare. So locally there is no login regardless of
the Access app — the app only gates the WAN/tunnel path. That is exactly the
"just go straight to domain → NPM" experience for anyone on the LAN.

## Apply safely

```python
from servarr import provisioning as p
p.new_service("grafana", port=3000, deployment="oci", ctid=141,
              image="grafana/grafana:latest", auth="external")        # plan only
# review the plan, then:
p.new_service("grafana", port=3000, deployment="oci", ctid=141,
              image="grafana/grafana:latest", auth="external",
              apply=True, confirm=True)
```

Every mutating step delegates to a guarded helper (`npm.add_service`,
`cloudflare.ingress_add`, `cloudflare.access_app_create`) that snapshots prior
state and refuses destructive diffs. See `safety.md`.
