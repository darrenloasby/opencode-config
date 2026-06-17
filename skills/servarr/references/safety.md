# Mutation-safety contract

Why this exists: an agent wiped the Cloudflare *wimba* tunnel ingress down to a
bare `http_status:404` while "adding one host", killing all `*.529broo.me`
routing to NPM. The lesson generalized into three rules and a small library
(`servarr/_safe.py`).

## Principles

1. **Read-modify-write, never blind-replace.** Mutating helpers fetch current
   state, change one item, and write the whole thing back — they never expose
   "here's a full config, replace everything" as the easy path.
2. **Snapshot before every write.** Prior state is dumped to
   `.snapshots/<domain>/<key>-<timestamp>.json`. Mirrors the repo "always backup
   before editing" rule. Recover with `servarr._safe.restore(path)`.
3. **Refuse destructive diffs unless explicitly confirmed.** A write that removes
   items, drops below a floor count, or touches a protected key raises
   `DestructiveChange` (carrying the `.diff`) unless `confirm=True`.

## Primitives (`servarr/_safe.py`)

- `snapshot(domain, key, state) -> path`
- `restore(path) -> state`
- `guard_destructive(old, new, *, id_key=None, min_keep=None, protect=(), confirm=False, label=...)`
  — returns a diff summary on success, raises `DestructiveChange` otherwise.
- `diff_summary(old, new, *, id_key=None)` — preview a change without applying.

## Cloudflare (remote-managed tunnel)

CT 112 is **remotely managed** — ingress lives in the Cloudflare API
(`accounts/{id}/cfd_tunnel/{id}/configurations`), not a local file. Use
`servarr.cloudflare`:

- `ingress_list()` — current rules (simplified view).
- `ingress_add(hostname, service, dry_run=False)` — insert/update one rule before
  the catch-all; snapshots; never removes anything → no confirm required.
- `ingress_remove(hostname, confirm=True)` — destructive; preserves the catch-all.
- `cf_api_write(method, endpoint, data, confirm=...)` — guarded escape hatch:
  DELETE and config-replacing PUT/PATCH need `confirm=True`.

Invariants enforced: the rule list must always end in a hostname-less catch-all,
and must never collapse to *only* the catch-all (the exact wimba failure).

## NPM (Nginx Proxy Manager)

- `list_hosts()`, `get_host(id)` — reads.
- `host_upsert(payload_json, dry_run=False)` — updates the existing host serving
  the domain in place instead of delete-then-create; snapshots on update.
- `host_delete(id, confirm=True)` — refuses to remove the only host for a domain.

## Working pattern for agents

1. Call the helper without `confirm`. If it returns, you're done.
2. If it raises `DestructiveChange`, show the user `exc.diff` and explain what is
   lost. Only re-run with `confirm=True` after explicit approval.
3. For adds/upserts, optionally `dry_run=True` first to preview.
4. If something still goes wrong, `restore()` the snapshot path the call returned.
