"""Safe-mutation primitives for internal/external API writes.

The rule this module enforces: **never replace a whole config to change one
item, and never let a write silently shrink or destroy state.**

This exists because an agent once wiped the Cloudflare *wimba* tunnel ingress
down to a bare ``http_status:404`` while "adding one host", killing all
``*.529broo.me`` routing. Every mutating wrapper in cloudflare.py / npm.py
routes through here.

Three primitives:

- ``snapshot(domain, key, state)``  -> write prior state to a timestamped JSON
  file so any change is recoverable (mirrors the repo "always backup" rule).
- ``guard_destructive(old, new, ...)`` -> raise ``DestructiveChange`` if the
  write removes items, drops below a floor, or touches a protected key, unless
  ``confirm=True``. Pure additions are always allowed.
- ``restore(snapshot_path)`` -> read a snapshot back for one-call rollback.

Plus ``diff_summary`` for human-readable "show the user before confirming".
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable, Sequence


SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / ".snapshots"


class DestructiveChange(Exception):
    """Raised when a write would remove or replace existing state.

    Carries a ``diff`` dict so callers/agents can show exactly what would be
    lost and re-issue the call with ``confirm=True`` if it is truly intended.
    """

    def __init__(self, message: str, diff: dict | None = None):
        super().__init__(message)
        self.diff = diff or {}


# ── Snapshots ────────────────────────────────────────────────────────────────

def snapshot(domain: str, key: str, state: Any) -> str:
    """Persist ``state`` under .snapshots/<domain>/<key>-<ts>.json. Returns path."""
    safe_key = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(key)) or "state"
    target_dir = SNAPSHOT_DIR / domain
    target_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = target_dir / f"{safe_key}-{ts}.json"
    path.write_text(json.dumps(state, indent=2, default=str))
    return str(path)


def restore(snapshot_path: str) -> Any:
    """Load a previously written snapshot for rollback."""
    return json.loads(Path(snapshot_path).read_text())


def latest_snapshot(domain: str, key: str | None = None) -> str | None:
    """Path to the most recent snapshot for a domain (optionally a key prefix)."""
    d = SNAPSHOT_DIR / domain
    if not d.exists():
        return None
    files = sorted(d.glob(f"{key}*.json" if key else "*.json"))
    return str(files[-1]) if files else None


# ── Destructive-change guard ─────────────────────────────────────────────────

def _as_id_set(items: Iterable[Any], id_key: str | None) -> set:
    out = set()
    for it in items:
        if id_key and isinstance(it, dict):
            out.add(json.dumps(it.get(id_key), sort_keys=True, default=str))
        else:
            out.add(json.dumps(it, sort_keys=True, default=str))
    return out


def diff_summary(
    old: Sequence[Any],
    new: Sequence[Any],
    *,
    id_key: str | None = None,
) -> dict:
    """Describe an old->new list change without applying it.

    Returns ``{added, removed, kept, old_count, new_count}`` where added/removed
    are counts and the membership is compared by ``id_key`` (or whole-item)."""
    old_ids = _as_id_set(old, id_key)
    new_ids = _as_id_set(new, id_key)
    return {
        "old_count": len(old),
        "new_count": len(new),
        "added": len(new_ids - old_ids),
        "removed": len(old_ids - new_ids),
        "kept": len(old_ids & new_ids),
        "removed_ids": sorted(old_ids - new_ids),
    }


def guard_destructive(
    old: Sequence[Any],
    new: Sequence[Any],
    *,
    id_key: str | None = None,
    min_keep: int | None = None,
    protect: Sequence[Any] = (),
    confirm: bool = False,
    label: str = "config",
) -> dict:
    """Refuse a list write that loses state, unless ``confirm=True``.

    Refuses when the new list removes any existing item, drops the surviving
    count below ``min_keep``, or removes any value in ``protect`` (compared by
    ``id_key``). Pure additions always pass. Returns the diff summary on success.
    """
    summary = diff_summary(old, new, id_key=id_key)

    protected_lost = []
    if protect:
        old_ids = _as_id_set(old, id_key)
        new_ids = _as_id_set(new, id_key)
        for p in protect:
            pid = json.dumps(p, sort_keys=True, default=str)
            if pid in old_ids and pid not in new_ids:
                protected_lost.append(p)

    problems = []
    if summary["removed"] > 0:
        problems.append(f"removes {summary['removed']} existing item(s)")
    if min_keep is not None and summary["new_count"] < min_keep:
        problems.append(f"leaves {summary['new_count']} item(s), below floor of {min_keep}")
    if protected_lost:
        problems.append(f"removes protected item(s): {protected_lost}")

    if problems and not confirm:
        raise DestructiveChange(
            f"Refusing destructive write to {label}: " + "; ".join(problems)
            + ". Show the diff to the user, then pass confirm=True if intended.",
            diff=summary,
        )
    return summary
