# servarr — minimal-surface control bundle

Control plane for the three-server constellation (**xenon** = inference,
**krypton** = media, **neon** = home automation): Proxmox LXC, systemd, Docker,
Home Assistant, Cloudflare tunnels/Access, NPM, *arr APIs, passage secrets.

## Background — why this exists

The old surface was a **3,140-line FastMCP server with ~14 fat tools** (plus ~50
dormant ones). It loaded a wall of tool schemas into every agent's context, and
it had drifted into a broken state (a corrupted line that wouldn't even parse, a
wrong manifest path). Worse, its API let an agent **replace a whole config to
change one item** — which is how the Cloudflare *wimba* tunnel ingress once got
wiped to a bare `404`, killing all `*.529broo.me` routing.

This bundle is the rebuild: a **code-execution surface** (per Anthropic's
"code execution with MCP") instead of schema-heavy tools, plus **guard-railed
mutations** so that class of mistake is structurally impossible.

## Shape

A stdlib-only Python package you drive by **importing and calling functions**
(via Bash / code execution), surfaced through `SKILL.md` (progressive
disclosure). Tool schemas no longer sit in context.

```
mcp/servarr/
  SKILL.md            progressive-disclosure entry (how/when to use)
  servarr/            the package
    _impl.py          all verified logic (generated from the old server; FastMCP no-op'd)
    _shim.py          no-op FastMCP so _impl imports as a plain library
    _safe.py          snapshot / guard_destructive / restore — the anti-footgun layer
    infra·containers·hosts·ha·services·cloudflare·npm·secrets·provision·provisioning
    cli.py            `python -m servarr <module> <fn> [json-args]`
  server.py           optional MCP shim: ONE generic `servarr_exec(code)` tool
  references/         safety.md · manifest.md · new-service.md
  pyproject.toml      stdlib-only; `servarr` CLI + optional `servarr-mcp` shim
  _archive/           retired pre-rebuild material (see its README)
```

## Key changes from the old server

- **14 fat tools → a code-exec package + 1 optional shim tool.** Near-zero schema
  cost; the agent writes a few lines of Python instead of calling bespoke tools.
- **Safe mutations (`_safe.py`).** Every cloudflare/npm write snapshots prior
  state to `.snapshots/` and refuses destructive diffs unless `confirm=True`:
  - `cloudflare.ingress_add/remove` — read-modify-write, the catch-all is never
    dropped; tunnel rules are edited via the **remote** Cloudflare API (CT 112).
  - `npm.host_upsert / host_delete(confirm=True)` — update in place; refuse to
    remove the last route for a domain.
- **xenon is first-class.** `hosts.CONSTELLATION` + `hosts.run(host, cmd)`.
- **New-service workflow (`provisioning.py`).** `new_service(...)` plans first
  (dry-run), applies with `apply=True`. Enforces: OCI image or plain LXC (never
  docker-in-LXC), static IP `192.168.86.<ctid>`, standard provision
  (DNS/passage/auto-login/console), NPM + tunnel ingress + optional Cloudflare
  Access (auth tree: none / external / always). Consistent SSL policy (Cloudflare
  terminates TLS; NPM holds the wildcard cert but never force-redirects → no loops).
  `align_ip()` / `align_all_ips()` fix the static-IP drift (plex left manual).
- **Harness wiring.** The old fat MCP is disabled in Claude/Codex/opencode; this
  bundle is surfaced as a skill via `~/.agents/skills/servarr`.

## Usage

```bash
servarr infra infra_status
servarr cloudflare ingress_list
servarr provisioning audit_static_ips
```

Mutating helpers print a diff and a snapshot path; on a `DestructiveChange`,
show the user the diff and only re-run with `confirm=True`. See
`references/safety.md` and `references/new-service.md`.

## Key files

- `servarr/_safe.py` — mutation guardrails (the heart of the safety story).
- `servarr/cloudflare.py`, `servarr/npm.py` — safe wrappers.
- `servarr/provisioning.py` — new-service workflow + IP alignment.
- `references/` — safety contract, manifest/host map, new-service runbook.
- `tests/test_mcp_shipping.py` (repo root) — 21 tests incl. safety-layer coverage.

## Tests

```bash
python3 -m pytest tests/test_mcp_shipping.py -q   # 21 passing
```

## Next steps

- **Commit** this bundle + the smart-tools skill + AGENTS/test changes (currently
  uncommitted for review).
- **IP drift**: 25/30 containers still off-convention. Run `align_all_ips()` in
  batches; **NPM (.88) and AdGuard (.100) need care** (cascading), **plex is manual**
  (router port-forward).
- **dust on hosts**: done (xenon/krypton/neon). No further action.
- Optional: build the **open-terminal** deploy artifact (standardized agent shell
  sandbox) via the OCI `new_service` workflow — reuses `smart-tools.sh`.
- Optional: retire the `servarr-mcp` console-script shim entirely if no non-CLI
  MCP client needs it.
