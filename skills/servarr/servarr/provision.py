"""Compliance — audit, provision, Loki log-shipping checks.

Thin facade over servarr._impl — re-exports the verified implementation
functions under a clean module namespace.
"""

from __future__ import annotations

from . import _impl

container_audit = _impl.container_audit
container_provision = _impl.container_provision
container_provision_all = _impl.container_provision_all
container_log_shipping_test = _impl.container_log_shipping_test
grafana_loki_auth_test = _impl.grafana_loki_auth_test
provision_manage = _impl.provision_manage
mount_health = _impl.mount_health

__all__ = [
    'container_audit',
    'container_provision',
    'container_provision_all',
    'container_log_shipping_test',
    'grafana_loki_auth_test',
    'provision_manage',
    'mount_health',
]
