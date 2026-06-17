#!/usr/bin/env python3
"""smart-tools PreToolUse guard — blocks slow disk-trawling, suggests fd/rg/dust.

Shared by Claude Code (hooks/hooks.json) and Codex (config.toml [[hooks.PreToolUse]]),
which both deliver a PreToolUse event as JSON on stdin and read a JSON decision on
stdout. The opencode TS adapter execs this too.

Policy ("smart, not stupid"):
  block   find …                          -> fd …
  block   grep -r/-R/--recursive | grep PATH (disk traversal) -> rg …
  block   du PATH                         -> dust PATH
  block   ls -R … / tree …                -> fd …  /  rg --files
  allow   … | grep foo   (stream filter, no path)         — passes untouched
  allow   rg / fd / dust  (broad search is encouraged)
  allow   anything via the escape hatch:  command find / \\find / ST_ALLOW=1

Exit codes / output are written in BOTH the Claude and Codex hook dialects so the
one script works in either harness. A blocked command is denied with a message
naming the exact replacement and the escape hatch.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys


def _fd() -> str | None:
    return shutil.which("fd") or shutil.which("fdfind")


# Replacement -> resolver. Only block a slow tool when its replacement EXISTS,
# so a host without the fast tool isn't trapped (Debian ships fd as `fdfind`).
SUGGEST = {
    "find": _fd,
    "grep": lambda: shutil.which("rg"),
    "egrep": lambda: shutil.which("rg"),
    "fgrep": lambda: shutil.which("rg"),
    "du": lambda: shutil.which("dust"),
    "tree": _fd,
}

ESCAPE_NOTE = (
    "If you truly need the raw tool, prefix with `command ` (e.g. `command find`) "
    "or set `ST_ALLOW=1`."
)


def _segments(command: str) -> list[str]:
    """Split a shell line on |, &&, ||, ; into individual simple commands."""
    return [s.strip() for s in re.split(r"\|\||&&|[|;]", command) if s.strip()]


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _strip_env_and_path(tokens: list[str]) -> list[str]:
    """Drop leading VAR=val assignments and absolute/relative path prefixes."""
    out = list(tokens)
    while out and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", out[0]):
        out.pop(0)
    return out


def _hit(tool: str, key: str) -> tuple[str, str] | None:
    """Block only if the fast replacement for `key` exists; else allow (None)."""
    repl = SUGGEST[key]()
    return (tool, repl) if repl else None


def violation(command: str) -> tuple[str, str] | None:
    """Return (tool, suggestion) if the command trawls the disk, else None."""
    if os.environ.get("ST_ALLOW") == "1":
        return None

    for seg_index, seg in enumerate(_segments(command)):
        toks = _strip_env_and_path(_tokens(seg))
        if not toks:
            continue
        raw = toks[0]
        # explicit escape hatch: `command find ...` or `\find`
        if raw == "command" and len(toks) > 1:
            continue
        name = os.path.basename(raw).lstrip("\\")
        if name.startswith("\\"):
            continue
        args = toks[1:]

        if name == "find":
            return _hit("find", "find")

        if name in ("grep", "egrep", "fgrep"):
            # A grep fed by a pipe (not the first segment) is a stream filter — allow.
            piped_in = seg_index > 0
            recursive = any(a in ("-r", "-R", "--recursive") or
                            (a.startswith("-") and not a.startswith("--") and
                             ("r" in a or "R" in a))
                            for a in args)
            # a non-flag, non-pattern path argument implies reading from disk
            non_flags = [a for a in args if not a.startswith("-")]
            reads_path = len(non_flags) >= 2  # pattern + at least one path/glob
            if recursive or (reads_path and not piped_in):
                return _hit("grep", "grep")
            continue  # stream/inline grep is fine

        if name == "du":
            return _hit("du", "du")

        if name == "tree":
            return _hit("tree", "tree")

        if name == "ls" and any("R" in a for a in args if a.startswith("-")):
            return _hit("ls -R", "find")

    return None


def _extract_command(event: dict) -> str:
    """Pull the bash command string out of a Claude or Codex PreToolUse event."""
    ti = event.get("tool_input") or event.get("toolInput") or {}
    if isinstance(ti, dict):
        cmd = ti.get("command") or ti.get("cmd")
        if cmd:
            return cmd if isinstance(cmd, str) else " ".join(cmd)
    # Codex may nest differently; try a few common spots
    for key in ("command", "arguments", "input"):
        v = event.get(key)
        if isinstance(v, str):
            return v
        if isinstance(v, dict) and isinstance(v.get("command"), str):
            return v["command"]
    return ""


def _deny(tool: str, suggestion: str) -> None:
    reason = (
        f"smart-tools: `{tool}` trawls the disk (slow, spins the fan, triggers "
        f"keychain popups). Use `{suggestion}` instead. {ESCAPE_NOTE}"
    )
    # Claude Code dialect: permissionDecision in hookSpecificOutput
    claude = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
        # Codex/legacy dialect also understood by some harnesses:
        "decision": "block",
        "reason": reason,
    }
    sys.stdout.write(json.dumps(claude))
    # Non-zero exit + stderr is the universal "block" signal for hooks that
    # ignore JSON (Codex command hooks treat non-zero as a failed gate).
    sys.stderr.write(reason + "\n")
    sys.exit(2)


def main() -> int:
    data = sys.stdin.read()
    try:
        event = json.loads(data) if data.strip() else {}
    except json.JSONDecodeError:
        event = {"tool_input": {"command": data}}

    # Only gate shell/bash tool calls
    tool = (event.get("tool_name") or event.get("toolName") or
            event.get("tool") or "")
    command = _extract_command(event)
    if tool and tool.lower() not in ("bash", "shell", "exec", "execute"):
        return 0
    if not command:
        return 0

    v = violation(command)
    if v:
        _deny(*v)  # exits 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
