"""passage secret store (read / list).

Thin facade over servarr._impl — re-exports the verified implementation
functions under a clean module namespace.
"""

from __future__ import annotations

from . import _impl

secret_read = _impl.secret_read
secret_list = _impl.secret_list
secret_manage = _impl.secret_manage

__all__ = [
    'secret_read',
    'secret_list',
    'secret_manage',
]
