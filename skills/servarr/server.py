#!/usr/bin/env python3
"""Minimal MCP entry point for the servarr bundle.

The whole servarr surface is a Python package (``servarr/``) meant to be driven
by code execution. For Claude Code that means: read SKILL.md, then import the
modules and call functions via Bash. This shim exists only for MCP clients that
cannot run code themselves — it exposes a SINGLE generic tool, ``servarr_exec``,
instead of dozens of tool schemas, keeping context cost near zero.

Run:  servarr-mcp        (stdio MCP server)
"""

from __future__ import annotations

import io
import json
import contextlib

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("servarr")


@mcp.tool()
def servarr_exec(code: str) -> str:
    """Execute Python against the servarr infrastructure library.

    The `servarr` package is importable. Import the module you need and call
    its functions; assign your result to `result` (or print). Reads are free;
    cloudflare/npm mutations snapshot prior state and refuse destructive diffs
    unless you pass confirm=True.

    Examples:
        import servarr.infra as infra; result = infra.infra_status()
        from servarr import cloudflare as cf
        result = cf.ingress_add("app.529broo.me", "http://192.168.86.88:81")

    Returns captured stdout plus the JSON-encoded `result` variable, if set.
    """
    import servarr  # noqa: F401  (make the package available to exec'd code)

    env: dict = {"servarr": servarr}
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exec(code, env)
    except Exception as exc:  # surface guard refusals with their diff
        return json.dumps(
            {"error": type(exc).__name__, "message": str(exc),
             "diff": getattr(exc, "diff", None), "stdout": out.getvalue()},
            indent=2, default=str,
        )

    payload = {"stdout": out.getvalue()}
    if "result" in env:
        payload["result"] = env["result"]
    return json.dumps(payload, indent=2, default=str)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
