# Container manifest & host map

## Hosts / SSH aliases (expected in `~/.ssh/config`)

| Name    | Role      | SSH alias        | Notes                          |
|---------|-----------|------------------|--------------------------------|
| xenon   | inference | `ai@xenon.local` | docker host                    |
| krypton | media     | `krypton_root`   | Proxmox; LXC + systemd         |
| neon    | home      | `root@neon` / `neon` (scp, port 2222) | HAOS; also `ssh homeassistant.local` |

`servarr.hosts.CONSTELLATION` is the canonical source for these. Override the
manifest path with `SERVARR_MANIFEST`; otherwise it is auto-located by walking up
to the repo-root `container-manifest.json`.

## Manifest shape (`container-manifest.json`)

```jsonc
{
  "_comment": "Container metadata: config paths, DB paths, deploy info.",
  "containers": {
    "plex": {
      "ctid": 100,
      "config_files": { "preferences": "/var/lib/.../Preferences.xml" },
      "db_paths":     { "library": "/var/lib/.../com.plexapp.plugins.library.db" },
      "log_paths":    { "app": "/var/lib/.../Plex Media Server.log" },
      "template":     "hosts/krypton/ct/plex/Preferences.xml"
    }
    // ... 24 containers
  }
}
```

Used by `containers.container_db_query` (resolves `db_paths` by key),
`containers.container_config_read`, and `infra` discovery enrichment. The
`servarr-discover` command on krypton provides the live Proxmox snapshot that the
manifest annotates.

## FUSE providers (never stop/restart)

CT 127 (nzbdav) and CT 128 (decypharr) back the FUSE mount chain. The
`containers` module refuses stop/restart on them via `_guard_fuse_provider`.
