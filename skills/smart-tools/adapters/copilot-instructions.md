# smart-tools policy (VS Code Copilot)

Copilot cannot hard-block commands, so this is a binding instruction. Follow it.

## Always use fast tools

- Use `fd` instead of `find`.
- Use `rg` (ripgrep) instead of `grep -r` or grep over a path. (`… | grep foo`
  stream filters are fine.)
- Use `dust` instead of `du`.
- Use `rg --files` / `fd` instead of `ls -R` or `tree`.

Never run recursive disk traversals — they are slow, spin the fan, and trigger
keychain/permission popups.

## Docs before disk

Look facts up before exploring the filesystem, in this order: Context7 →
GitHub → web search → filesystem (only if those can't answer). Do not trawl the
disk for anything documentation already provides.

Escape hatch for a genuine raw-tool need: `command find …` or `ST_ALLOW=1 <cmd>`.
