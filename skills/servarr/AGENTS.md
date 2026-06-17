# AGENTS.md — servarr bundle

Guidance for AI agents working on or with the servarr control bundle.

## What this is

A stdlib-only Python package (`servarr/`) driven by **code execution**, not a
wall of MCP tools. Read `SKILL.md` first; details live in module docstrings and
`references/`. The old FastMCP server is retired in `_archive/` — do not use it.

## How to use it

```bash
servarr infra infra_status
servarr <module> <function> [args] [--kw k=v]
```

Modules: `infra · containers · hosts · ha · services · cloudflare · npm ·
secrets · provision · provisioning`.

## Rules you must follow

1. **Never replace a whole config to change one item.** Use the intent-level
   helpers (`cloudflare.ingress_add/remove`, `npm.host_upsert/host_delete`). They
   snapshot to `.snapshots/` and raise `DestructiveChange` unless `confirm=True`.
   On a refusal: show the user `exc.diff`, get approval, then re-run with
   `confirm=True`. Prefer `dry_run=True` first.
2. **Cloudflare tunnel rules are remote-managed (CT 112)** — edit via the API
   helpers only; there is no local `config.yml`. The ingress catch-all is sacred.
3. **FUSE providers CT 127 (nzbdav) / 128 (decypharr): never stop/restart** —
   `containers` enforces this; don't bypass it.
4. **New containers**: OCI image or plain LXC — **never docker-in-LXC**. Static IP
   `192.168.86.<ctid>`. Standard provision (DNS/passage/auto-login/console). Use
   `provisioning.new_service(...)` (plans first; `apply=True` to execute).
5. **SSL**: Cloudflare terminates public TLS; NPM holds the wildcard cert but
   `ssl_forced=False` (no redirect loops). Don't flip it without an end-to-end
   HTTPS path.
6. **Fast tools / docs-first**: `fd`/`rg`/`dust` are enforced (smart-tools);
   look up docs before trawling the filesystem.

## Editing the package

- Logic lives in `servarr/_impl.py`; domain modules are thin facades + new safe
  wrappers. `_safe.py` is the guard layer — keep new mutations routed through it.
- Run `python3 -m pytest tests/test_mcp_shipping.py -q` (21 tests) after changes.
- Always back up before editing remote/host config (the safe helpers do this).

See `README.md` for background, shape, and next steps.
