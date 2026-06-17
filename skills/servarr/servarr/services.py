"""Service APIs — *arr (radarr/sonarr/prowlarr), Overseerr, Tautulli, AdGuard, generic HTTP.

Thin facade over servarr._impl — re-exports the verified implementation
functions under a clean module namespace.  ``direct_api()`` and
``adguard_api()`` are new additions for broad HTTP access not covered by
dedicated helpers.

These are read-mostly.  Any write you add should go through _safe like
cloudflare/npm do.
"""

from __future__ import annotations

import json
from typing import Any

from . import _impl

arr_api = _impl.arr_api
overseerr_api = _impl.overseerr_api
tautulli_api = _impl.tautulli_api
api_call = _impl.api_call


def direct_api(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    data: dict | None = None,
    timeout: int = 10,
) -> Any:
    """Make a generic HTTP request to any service (stdlib only).

    Tries to parse the response as JSON; falls back to raw text.

    Args:
        url:     Full URL (e.g. ``http://192.168.86.100/control/status``).
        method:  HTTP method (GET, POST, PUT, DELETE).
        headers: Optional dict of extra headers.
        data:    Optional dict body for POST/PUT (JSON-encoded).
        timeout: Seconds before giving up (default 10).
    """
    raw = _impl._http(url, method=method, headers=headers, data=data, timeout=timeout)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def adguard_api(
    endpoint: str,
    method: str = "GET",
    data: dict | None = None,
    timeout: int = 10,
) -> Any:
    """Call the AdGuard Home API.

    Auth via passage (``servarr/adguard/admin-password``).  Uses basic auth
    with the admin credentials.

    Args:
        endpoint: API path after ``/control/``, e.g. ``status`` or
                  ``filtering/status``.
        method:   HTTP method (GET, POST, PUT, DELETE).
        data:     Optional dict body for write operations.
        timeout:  Seconds before giving up (default 10).
    """
    password = _impl._passage("servarr/adguard/admin-password")
    base = "http://192.168.86.100"
    headers = {"Authorization": password}  # AdGuard accepts bare password as Authorization header
    raw = _impl._http(
        f"{base}/control/{endpoint.lstrip('/')}",
        method=method,
        headers=headers,
        data=data,
        timeout=timeout,
    )
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


__all__ = [
    "arr_api",
    "overseerr_api",
    "tautulli_api",
    "api_call",
    "direct_api",
    "adguard_api",
]
