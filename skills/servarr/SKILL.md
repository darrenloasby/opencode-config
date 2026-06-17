---
name: servarr
description: Control the home media + inference + home-automation constellation (xenon, krypton, neon) — Proxmox LXC containers, systemd services, Docker, Home Assistant, Cloudflare tunnels/Access, Nginx Proxy Manager, *arr APIs, and passage secrets. A code-execution bundle: import the servarr Python package and call functions instead of loading dozens of tool schemas. Use for any servarr infra task — checking status, reading logs, managing containers, or changing routing. Mutations are guarded so you can't wipe a config to change one item.
---

# servarr

Infrastructure control for a three-server constellation, exposed as a Python
package you drive by **code execution** — not a wall of MCP tools.

## The constellation

| Host    | Role      | Kind        | Notes                                            |
|---------|-----------|-------------|--------------------------------------------------|
| xenon   | inference | docker      | Apple Silicon AI runtime, passage store, control-plane |
| krypton | media     | proxmox     | LXC containers + systemd services                |
| neon    | home      | HAOS        | Home Assistant OS + add-ons (`ssh homeassistant.local`) |
| local   | admin     | docker      | this Mac                                          |

## How to use

The whole surface is the `servarr` Python package. Use it one of two ways:

### CLI (any directory)

```bash
servarr infra infra_status
servarr cloudflare ingress_list
servarr hosts constellation
servarr provisioning audit_static_ips
```

### MCP tool (if available)

Use `servarr_exec(code)` — pass Python code that imports servarr and calls
functions.

```python
import servarr.infra as infra
result = infra.infra_status()

from servarr import cloudflare as cf
result = cf.ingress_add("app.529broo.me", "http://192.168.86.88:81")
```

Both work from any directory — no `cd mcp/servarr` or `PYTHONPATH` needed.

## Wrapper fallback

`bin/servarr` and `bin/servarr-mcp` are shell wrappers in this skill directory.
They try `~/.local/bin/<name>` first, then fall back to running from skill
source. That helps when uv tool isn't installed yet, like fresh clone, CI, or
agent sandbox.

Use them like:

```bash
/Users/dlo/.agents/skills/servarr/bin/servarr infra infra_status
```

If `servarr` isn't in `PATH`, agents should use `bin/servarr PATH`.

Pattern: **import the module, call the function, read the result.** Don't ask
for a tool — write the few lines of Python.


## Modules (import what you need)

- `infra` — `infra_status()`, `discover()`, `mount_health()`
- `containers` — Proxmox LXC: `container_status/logs/exec/start/stop/restart`,
  `container_db_query`, `container_config_read`, `container_create/destroy`, templates
- `hosts` — constellation: `run(host, cmd)`, `push/pull`, `constellation()`,
  systemd `service_*`, local docker
- `ha` — Home Assistant OS containers on neon
- `services` — `arr_api(svc, endpoint)`, `overseerr_api`, `tautulli_api`
- `cloudflare` — reads + **safe** tunnel-ingress / Access wrappers (see below)
- `npm` — Nginx Proxy Manager: reads + **safe** `host_upsert` / `host_delete`
- `secrets` — passage `secret_read` / `secret_list`
- `provision` — `container_audit`, `container_provision`, loki shipping checks
- `provisioning` — **new-service workflow**: `new_service(...)` (plan-first),
  `static_ip(ctid)`, `audit_static_ips()`

## Onboarding a new service (READ `references/new-service.md`)

Use `provisioning.new_service(...)` — it plans first, applies only with
`apply=True`. Standing rules it enforces:

- **Deploy as OCI image or plain LXC bundle — never docker-in-LXC.** Local docker
  is not a deploy target.
- **Fixed IP = `192.168.86.<ctid>`** (DHCP is `.10–.99`; containers `.100+`).
  `provisioning.audit_static_ips()` shows current drift.
- **Standard setup** on every container: DNS, passage, auto-login, console via
  Proxmox (`containers.container_provision`).
- **Auth tree** (split-horizon DNS already bypasses login on the LAN):
  `auth="none"` straight to NPM · `auth="external"` login on WAN only (auth2) ·
  `auth="always"` login everywhere (auth1).

### SSL — one consistent policy (stop the redirect loops)

Cloudflare terminates public TLS; the tunnel reaches NPM over **http** (ingress
stays `http://192.168.86.88:80`). NPM holds the wildcard `*.529broo.me` cert for
LAN HTTPS but **never force-redirects** (`ssl_forced=False`, HSTS off) — forcing
the redirect on the tunnel origin is what loops. `npm.add_service()` bakes this in.

Each function is documented in its docstring — read the module file for exact
signatures rather than guessing.

## Mutation safety — READ THIS BEFORE CHANGING ANYTHING

An agent once wiped the Cloudflare tunnel ingress down to a bare 404 while
"adding one host", killing all `*.529broo.me` routing. This bundle makes that
impossible by default. The rules:

1. **Never replace a whole config to change one item.** There is no easy
   "replace all ingress / all hosts" call. Use the intent-level helpers.
2. **Cloudflare tunnel rules are REMOTE** (CT 112 is remotely managed). You edit
   them through the Cloudflare API, never a local `config.yml`. Use:
   - `cloudflare.ingress_list()` — read
   - `cloudflare.ingress_add(hostname, service)` — adds/updates one rule,
     preserves all others and the catch-all, snapshots first. No confirm needed.
   - `cloudflare.ingress_remove(hostname, confirm=True)` — destructive, needs confirm.
3. **NPM:** `npm.host_upsert(payload_json)` updates in place; `npm.host_delete(id,
   confirm=True)` refuses to remove the last route for a domain.
4. Any guarded call raises `DestructiveChange` (with a `.diff`) unless
   `confirm=True`. When you hit one: **show the user the diff, then re-run with
   `confirm=True`** only if they approve.
5. Prefer `dry_run=True` first on `ingress_add` / `host_upsert` to preview.

Every mutation writes a timestamped snapshot to `.snapshots/` first; recover with
`servarr._safe.restore(path)`. Full contract: `references/safety.md`.

## Hard guardrails (do not bypass)

- **FUSE providers** CT 127 (nzbdav) and CT 128 (decypharr): never stop/restart —
  it cascades to every consumer container. `containers` refuses this automatically.
- **Always snapshot before editing** remote config (built into the safe helpers).

## MCP shim (optional)

`server.py` exposes a single `servarr_exec(code)` MCP tool for clients that can't
run code. It runs the same package. Claude Code should just use Bash + the package.

References: `references/safety.md` (mutation contract), `references/manifest.md`
(container manifest + host/alias map).
