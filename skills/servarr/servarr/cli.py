"""Thin CLI dispatcher: `python -m servarr <module> <function> [json-args...]`.

Examples:
    python -m servarr infra infra_status
    python -m servarr cloudflare ingress_list
    python -m servarr cloudflare ingress_add '"app.529broo.me"' '"http://192.168.86.88:81"'
    python -m servarr npm host_delete 12 --kw confirm=true

Positional args are parsed as JSON when possible (else passed as strings).
Keyword args use `--kw name=jsonvalue`. Output is JSON.
"""

from __future__ import annotations

import importlib
import json
import sys


def _coerce(token: str):
    try:
        return json.loads(token)
    except (json.JSONDecodeError, ValueError):
        return token


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        print(__doc__)
        return 2

    module_name, func_name, *rest = argv
    args, kwargs = [], {}
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--kw" and i + 1 < len(rest):
            k, _, v = rest[i + 1].partition("=")
            kwargs[k] = _coerce(v)
            i += 2
        else:
            args.append(_coerce(tok))
            i += 1

    try:
        mod = importlib.import_module(f"servarr.{module_name}")
    except ModuleNotFoundError:
        print(f"No such module: servarr.{module_name}", file=sys.stderr)
        return 2
    func = getattr(mod, func_name, None)
    if func is None or not callable(func):
        print(f"No such function: servarr.{module_name}.{func_name}", file=sys.stderr)
        return 2

    try:
        result = func(*args, **kwargs)
    except Exception as exc:  # surface guard refusals etc. as structured output
        print(json.dumps({"error": type(exc).__name__, "message": str(exc),
                          "diff": getattr(exc, "diff", None)}, indent=2, default=str))
        return 1

    if result is None:
        print("null")
    else:
        print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
