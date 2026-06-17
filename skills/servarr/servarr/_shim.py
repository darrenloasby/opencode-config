"""No-op FastMCP shim.

`_impl.py` was generated from the original FastMCP server. We keep the
`@mcp.tool()/@mcp.resource()/@mcp.prompt()` decorators inert so the entire
implementation imports as a plain library with no MCP SDK dependency.

The real MCP entry point (a single ``servarr_exec`` tool) lives in
``server.py`` at the package root and uses the genuine SDK.
"""

from __future__ import annotations


class _Noop:
    """A decorator factory whose decorators return the function unchanged."""

    def __init__(self, *args, **kwargs):
        pass

    def _passthrough(self, *args, **kwargs):
        def deco(func):
            return func
        return deco

    tool = _passthrough
    resource = _passthrough
    prompt = _passthrough

    def run(self, *args, **kwargs):  # pragma: no cover - never used as a server
        raise RuntimeError(
            "servarr._impl is a library, not a server. Use `servarr` package "
            "functions directly, or the servarr_exec MCP tool in server.py."
        )


FastMCP = _Noop
