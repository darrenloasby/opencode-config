"""Infrastructure discovery and combined status.

Thin facade over servarr._impl — re-exports the verified implementation
functions under a clean module namespace.
"""

from __future__ import annotations

from . import _impl

discover = _impl.discover
infra_status = _impl.infra_status
list_containers = _impl.list_containers
all_containers = _impl.all_containers
mount_health = _impl.mount_health

__all__ = [
    'discover',
    'infra_status',
    'list_containers',
    'all_containers',
    'mount_health',
]
