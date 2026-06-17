#!/usr/bin/env bash
# smart-tools installer — wire every harness on this machine to the one skill dir.
#
#   bash install.sh                 # local: shell + Claude + Codex + opencode + Copilot
#   bash install.sh --remote HOST   # push the shell guard to a constellation host
#
# Idempotent. Backs up any file it edits to <file>.bak.smart-tools.
set -euo pipefail

SKILL_DIR="$HOME/.agents/skills/smart-tools"
SRC_LINE="source $SKILL_DIR/bin/smart-tools.sh"
GUARD="$SKILL_DIR/bin/guard.py"

note() { printf '  \033[1;34m·\033[0m %s\n' "$*"; }
ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$*"; }

backup() { [ -f "$1" ] && cp "$1" "$1.bak.smart-tools" || true; }

wire_shell() {
  for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
    [ -e "$rc" ] || continue
    if grep -qF "smart-tools/bin/smart-tools.sh" "$rc"; then
      ok "shell guard already in $(basename "$rc")"
    else
      backup "$rc"
      printf '\n# smart-tools: enforce fd/rg/dust over slow disk-trawling\n%s\n' "$SRC_LINE" >> "$rc"
      ok "sourced shell guard in $(basename "$rc")"
    fi
  done
}

wire_claude() {
  local link="$HOME/.claude/skills/smart-tools"
  mkdir -p "$HOME/.claude/skills"
  if [ -L "$link" ] || [ -e "$link" ]; then
    ok "Claude plugin link already present"
  else
    ln -s "$SKILL_DIR" "$link"
    ok "linked Claude plugin -> ~/.agents/skills/smart-tools (run /reload-plugins)"
  fi
}

wire_codex() {
  local hj="$HOME/.codex/hooks.json"
  [ -f "$hj" ] || { note "no ~/.codex/hooks.json — skipping Codex"; return; }
  backup "$hj"
  GUARD="$GUARD" python3 - "$hj" <<'PY'
import json, os, sys
path = sys.argv[1]
guard = os.environ["GUARD"]
d = json.load(open(path))
hooks = d.setdefault("hooks", {})
pre = hooks.setdefault("PreToolUse", [])
cmd = f'python3 "{guard}"'
already = any(
    cmd in (hh.get("command", "")) for entry in pre for hh in entry.get("hooks", [])
)
if not already:
    pre.append({"matcher": "Bash|shell|exec", "hooks": [{"type": "command", "command": cmd}]})
    json.dump(d, open(path, "w"), indent=2)
    print("  \033[1;32m✓\033[0m added PreToolUse guard to ~/.codex/hooks.json")
else:
    print("  \033[1;32m✓\033[0m Codex PreToolUse guard already present")
PY
}

wire_opencode() {
  local dir="$HOME/.config/opencode/plugin"
  mkdir -p "$dir"
  local dst="$dir/smart-tools.ts"
  if [ -e "$dst" ] || [ -L "$dst" ]; then
    ok "opencode plugin already present"
  else
    ln -s "$SKILL_DIR/adapters/opencode-plugin.ts" "$dst"
    ok "linked opencode plugin"
  fi
}

wire_copilot() {
  note "Copilot is soft-only. Reference this in your workspace .github/copilot-instructions.md:"
  note "  $SKILL_DIR/adapters/copilot-instructions.md"
}

remote() {
  local host="$1" alias
  case "$host" in
    krypton) alias="krypton_root" ;;
    neon)    alias="neon" ;;
    xenon)   alias="ai@xenon.local" ;;
    *)       alias="$host" ;;
  esac
  note "checking fast tools on $host ($alias)…"
  ssh "$alias" 'for t in fd rg dust; do command -v $t >/dev/null || echo "MISSING: $t"; done' || true
  ssh "$alias" 'mkdir -p ~/.agents/skills/smart-tools/bin'
  scp "$SKILL_DIR/bin/smart-tools.sh" "$alias:~/.agents/skills/smart-tools/bin/smart-tools.sh"
  ssh "$alias" 'rc=~/.zshrc; [ -e "$rc" ] || rc=~/.bashrc; [ -e "$rc" ] || rc=~/.profile; \
    grep -qF "smart-tools/bin/smart-tools.sh" "$rc" 2>/dev/null || \
    printf "\n# smart-tools\nsource ~/.agents/skills/smart-tools/bin/smart-tools.sh\n" >> "$rc"; \
    echo "  sourced in $rc"'
  ok "pushed shell guard to $host"
}

if [ "${1:-}" = "--remote" ]; then
  [ -n "${2:-}" ] || { echo "usage: install.sh --remote HOST"; exit 1; }
  remote "$2"
  exit 0
fi

echo "smart-tools: wiring local harnesses →"
wire_shell
wire_claude
wire_codex
wire_opencode
wire_copilot
echo "done. open a new shell (or: source ~/.zshrc) and run /reload-plugins in Claude."
