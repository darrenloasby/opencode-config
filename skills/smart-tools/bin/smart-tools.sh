# smart-tools shell guard — source me from ~/.zshrc / ~/.bashrc
#
#   source ~/.agents/skills/smart-tools/bin/smart-tools.sh
#
# Hard-blocks disk-trawling invocations (find / recursive grep / du / ls -R / tree)
# and points at the fast tool (fd / rg / dust). Works in zsh and bash, and catches
# EVERY agent harness because they all ultimately run commands through the shell.
#
# Allowed (not disk-trawling): `… | grep foo` stream filters, and rg/fd/dust.
# Escape hatch: `command find …`, `\find …`, or `ST_ALLOW=1 <cmd>`.

# Skip entirely if the user opts out for the whole shell.
[ -n "${SMART_TOOLS_OFF:-}" ] && return 0 2>/dev/null

# Capability detection — only guard a slow tool when its fast replacement EXISTS,
# so we never trap a host that lacks the replacement. Debian ships fd as `fdfind`.
__ST_FD=""; command -v fd >/dev/null 2>&1 && __ST_FD=fd || { command -v fdfind >/dev/null 2>&1 && __ST_FD=fdfind; }
__ST_RG=""; command -v rg >/dev/null 2>&1 && __ST_RG=rg
__ST_DUST=""; command -v dust >/dev/null 2>&1 && __ST_DUST=dust
# Make `fd` work where only `fdfind` exists (Debian). A function — not an alias —
# so it also works in the NON-interactive shells that agent harnesses use.
if [ "$__ST_FD" = "fdfind" ] && ! command -v fd >/dev/null 2>&1; then
  fd() { command fdfind "$@"; }
fi

__st_block() {
  # $1 = tool label, $2 = suggestion
  printf '\033[1;31m✗ smart-tools:\033[0m `%s` trawls the disk (slow; spins the fan; keychain popups).\n' "$1" >&2
  printf '  → use \033[1;32m%s\033[0m instead.  Escape: `command %s …`, `\\%s …`, or `ST_ALLOW=1 …`\n' "$2" "$1" "$1" >&2
  return 1
}

if [ -n "$__ST_FD" ]; then
find() {
  [ "${ST_ALLOW:-}" = "1" ] && { command find "$@"; return; }
  __st_block find "$__ST_FD"
}
fi

if [ -n "$__ST_DUST" ]; then
du() {
  [ "${ST_ALLOW:-}" = "1" ] && { command du "$@"; return; }
  __st_block du dust
}
fi

if [ -n "$__ST_FD" ]; then
tree() {
  [ "${ST_ALLOW:-}" = "1" ] && { command tree "$@"; return; }
  __st_block tree "$__ST_FD (or: rg --files)"
}
fi

if [ -n "$__ST_RG" ]; then
grep() {
  [ "${ST_ALLOW:-}" = "1" ] && { command grep "$@"; return; }
  # Decide on ARGUMENTS, not on tty — agents run non-interactively, so a tty
  # check fails open exactly when it matters. Block recursive grep, or grep
  # given a path to scan (pattern + >=1 path arg). Allow pattern-only grep,
  # which is the genuine stream-filter case (`… | grep foo`).
  local a recursive=0 nonflags=0
  for a in "$@"; do
    case "$a" in
      -r|-R|--recursive) recursive=1 ;;
      --*) : ;;
      -[a-zA-Z]*[rR]*) recursive=1 ;;
      -*) : ;;
      *) nonflags=$((nonflags+1)) ;;
    esac
  done
  if [ "$recursive" = "1" ] || [ "$nonflags" -ge 2 ]; then
    __st_block grep rg
    return 1
  fi
  command grep "$@"
}
fi

if [ -n "$__ST_FD" ]; then
ls() {
  case " $* " in
    *" -R "*|*" -"*"R"*)
      [ "${ST_ALLOW:-}" = "1" ] && { command ls "$@"; return; }
      __st_block "ls -R" "$__ST_FD (or: rg --files)"; return 1 ;;
  esac
  command ls "$@"
}
fi

# One-time docs-first reminder per interactive shell (suppressible).
if [ -z "${SMART_TOOLS_QUIET:-}" ] && [ -t 1 ]; then
  __st_tools="${__ST_FD:-–}/${__ST_RG:-–}/${__ST_DUST:-–}"
  printf '\033[2msmart-tools active (%s) · look up docs (Context7/web) before trawling disk\033[0m\n' "$__st_tools" >&2
fi
