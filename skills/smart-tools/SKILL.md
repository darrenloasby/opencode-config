---
name: smart-tools
description: Enforce fast tools and docs-first research. Use fd (not find), rg (not grep), dust (not du); look up docs on Context7/GitHub/web BEFORE trawling the filesystem. This skill ships a hard-blocking PreToolUse hook plus a shell guard so slow recursive scans are refused across every harness and machine.
---

# smart-tools

Stop agents from spinning the fan and triggering keychain popups by recursively
scanning the disk. Fast tools are mandatory; docs come before disk.

## The rules (enforced, not suggested)

| Instead of            | Always use                |
|-----------------------|---------------------------|
| `find …`              | `fd …`                    |
| `grep -r` / `grep PATH` | `rg …`                  |
| `du …`                | `dust …`                  |
| `ls -R`, `tree`       | `fd` / `rg --files`       |

Allowed without complaint: stream filters like `… | grep foo`, and `rg`/`fd`/`dust`
themselves — **broad search is encouraged, aimless disk traversal is not.**

Escape hatch for a genuine edge case: `command find …`, `\find …`, or
`ST_ALLOW=1 <cmd>`.

## Docs-first

Before exploring the filesystem, look it up:

1. **Context7** — libraries, frameworks, APIs, config, code examples.
2. **GitHub** — real-world implementations and patterns.
3. **Web** — recent/breaking facts, versions, new tools.
4. **Filesystem** — only after the above are exhausted, or when local state
   diverges from docs.

Never trawl the disk for something the docs answer.

## How enforcement works (3 layers)

1. **Shell guard** (`bin/smart-tools.sh`) — sourced in every shell on every
   machine; `find`/`grep -r`/`du`/`ls -R`/`tree` are shell functions that block
   and point at the fast tool. Catches *every* harness because they all shell out.
2. **PreToolUse hook** (`bin/guard.py` via `hooks/hooks.json`) — Claude Code and
   Codex deny the Bash call before it runs, with the exact replacement.
3. **Adapters** — opencode plugin + Copilot instructions in `adapters/`.

Install/refresh everything with `bash install.sh` (add `--remote <host>` to push
the shell guard to xenon/krypton/neon). See `policy.md` for the full contract.
