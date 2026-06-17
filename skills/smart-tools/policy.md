# smart-tools policy (shared contract)

This is the single source of truth that every harness adapter references.

## Fast tools are mandatory

| Forbidden (slow, disk-trawling) | Required          | Why                                   |
|---------------------------------|-------------------|---------------------------------------|
| `find`                          | `fd`              | orders of magnitude faster, sane defaults |
| `grep -r` / `grep <path>`       | `rg`              | ripgrep skips .git/ignored, much faster |
| `du`                            | `dust`            | instant tree sizes, no full stat storm |
| `ls -R`, `tree`                 | `fd` / `rg --files` | bounded, ignore-aware                |

**Allowed:** `… | grep foo` (stream filter, not disk), and `rg`/`fd`/`dust`
(broad search is good). The target is *aimless recursive traversal*, not search.

**Escape hatch:** `command find …`, `\find …`, or `ST_ALLOW=1 <cmd>` for the rare
legitimate case. The block message always names it.

## Docs before disk

Discover facts in this order; only fall through when the previous source can't
answer:

1. Context7 (libraries/APIs/config/examples)
2. GitHub (implementations/patterns)
3. Web search (recent/breaking)
4. Filesystem exploration (local state, or docs diverge)

Do not trawl `~/` or large trees to find something a doc lookup answers. That is
what spins the fan and triggers the macOS keychain/permission popups.

## Enforcement layers

- `bin/smart-tools.sh` — shell functions (zsh/bash), sourced everywhere.
- `bin/guard.py` — PreToolUse gate for Claude Code & Codex (JSON on stdin,
  deny on stdout + exit 2).
- `adapters/opencode-plugin.ts` — opencode `tool.execute.before` → execs guard.py.
- `adapters/copilot-instructions.md` — soft policy for VS Code Copilot.
