# opencode-config

Distributed OpenCode configuration for the constellation:
- **argon** — Mac (primary), runs engram server + mcp-gateway
- **krypton** — Proxmox LXC (Linux), remote MCP via argon
- **xenon** — Docker (macOS), remote MCP via argon

## Structure

```
├── AGENTS.md                  # Shared: synced to ~/.config/opencode/AGENTS.md
├── skills/                    # Shared: synced to ~/.agents/skills/
├── machine/
│   ├── argon/                 # Primary — engram server + mcp-gateway on localhost
│   ├── krypton/               # Linux LXC — remote MCP via argon.local:3100/mcp
│   └── xenon/                 # Docker/macOS — remote MCP via argon.local:3100/mcp
└── deploy/
    ├── sync-opencode-config   # Sync script → ~/.local/bin/
    ├── com.user.sync-opencode-config.plist  # launchd for Mac hosts
    ├── sync-opencode-config.service         # systemd for Linux hosts
    └── sync-opencode-config.timer           # systemd timer
```

## Setup

```bash
# install sync script
cp deploy/sync-opencode-config ~/.local/bin/
chmod +x ~/.local/bin/sync-opencode-config

# Mac — install launchd agent
cp deploy/com.user.sync-opencode-config.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.user.sync-opencode-config.plist

# Linux — install systemd timer
sudo cp deploy/sync-opencode-config.{service,timer} /etc/systemd/system/
sudo systemctl enable --now sync-opencode-config.timer
```

## Per-machine config

Each machine keeps its own `~/.config/opencode/opencode.jsonc`.
The machine/ directory holds reference configs — copy the relevant one:

```bash
cp machine/$(hostname -s)/opencode.jsonc ~/.config/opencode/opencode.jsonc
```

Then edit as needed (add your own API keys, agent config, etc.).
