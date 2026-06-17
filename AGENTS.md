# Claude-Mem Memory Context
## Global OpenCode / OpenChamber guidance

- Prefer `build` for implementation work and `plan` for analysis-only sessions.
- Use `openchamber-review` for risk checks, `openchamber-research` for repository exploration, and `openchamber-docs` for documentation edits.
- Keep shared agents narrow, deterministic, and explicit about permissions.
- Favor repo-local `.opencode/` overrides for project-specific behavior when needed.

## Clop image cache (READ THIS EVERY TIME)

When user gives you a filename like `28568.png` or any image name — **search Clop cache first**:

1. `fd "28568" ~/Library/Caches/Clop/`
2. Then use `look_at` tool on the found path

Do NOT tell user you can't see images, do NOT say you're not vision-enabled. Use `look_at`. Clop caches all clipboard images there. The file IS local. Find it. If `fd` returns nothing, search `~/Library/Caches/Clop/` manually with `ls`.

## multimodal-looker agent

The multimodal-looker subagent type has its own system prompt with instructions on where to find files. Read it. Stop saying you can't see attachments. If given a bare filename, check Clop cache, /tmp, and the working directory.
