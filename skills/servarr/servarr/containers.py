"""Proxmox LXC containers — lifecycle, logs, exec, DB, config, templates.

Thin facade over servarr._impl — re-exports the verified implementation
functions under a clean module namespace.

Stop/restart of FUSE providers (CT 127 nzbdav, CT 128 decypharr) is refused
by _guard_fuse_provider — never bypass it.
"""

from __future__ import annotations

from . import _impl

list_containers = _impl.list_containers
all_containers = _impl.all_containers
container_status = _impl.container_status
container_logs = _impl.container_logs
container_exec = _impl.container_exec
container_start = _impl.container_start
container_stop = _impl.container_stop
container_restart = _impl.container_restart
container_db_query = _impl.container_db_query
container_config_read = _impl.container_config_read
container_create = _impl.container_create
container_destroy = _impl.container_destroy
list_templates = _impl.list_templates
pull_oci_template = _impl.pull_oci_template
container_manage = _impl.container_manage
template_manage = _impl.template_manage

__all__ = [
    'list_containers',
    'all_containers',
    'container_status',
    'container_logs',
    'container_exec',
    'container_start',
    'container_stop',
    'container_restart',
    'container_db_query',
    'container_config_read',
    'container_create',
    'container_destroy',
    'list_templates',
    'pull_oci_template',
    'container_manage',
    'template_manage',
]
