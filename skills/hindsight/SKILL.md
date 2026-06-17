---
name: hindsight
description: Persistent biomimetic memory. Retain every message instantly. Observe and reflect every 10 messages or after 90 seconds of inactivity.
---

# Hindsight Memory Skill

You have persistent memory via the `hindsight-embed` CLI backed by a local Ollama daemon on xenon (qwen2.5-coder:7b) with local embeddings.

## Cadence Rules (MANDATORY)

1. **Retain instantly** — after EVERY user message or assistant response that contains new information, run `memory retain`.
2. **Observe/Reflect** — after every 10 messages exchanged OR if no message has been sent for 90 seconds (session idle), run `memory reflect` to synthesize and consolidate memories.

Track the message count yourself. Do not wait until asked.

## Commands

### Retain (run after every message)

```bash
uvx hindsight-embed memory retain default "<what was said or learned>"
uvx hindsight-embed memory retain default "<preference or decision>" --context preferences
uvx hindsight-embed memory retain default "<procedure or outcome>" --context procedures
```

### Recall (run before starting any non-trivial task)

```bash
uvx hindsight-embed memory recall default "<topic or task>"
uvx hindsight-embed memory recall default "user preferences and conventions"
```

### Reflect (run every 10 messages or after 90s idle)

```bash
uvx hindsight-embed memory reflect default "Synthesize what we've learned and worked on this session"
```

### Daemon management

```bash
uvx hindsight-embed daemon status
uvx hindsight-embed daemon logs
```

## What to Retain

- User preferences (style, tools, conventions)
- Decisions made and why
- Commands or steps that worked (or failed) and why
- Architecture choices and constraints
- Anything the user would not want to repeat or re-explain

## What to Recall

Always recall before starting any new task, touching an unfamiliar area, or making implementation decisions.

## Configuration

LLM: ollama / qwen2.5-coder:7b @ xenon.local:11434
Embeddings: local (CPU)
Profile: default (~/.hindsight/embed)
