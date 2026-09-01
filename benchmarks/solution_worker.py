#!/usr/bin/env python3
"""Execute one benchmark call without exposing its expected result."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

from solution_runtime import import_entrypoint


RAW_EXIT = os._exit
RAW_WRITE = os.write


def execute(request: dict[str, Any]) -> dict[str, Any]:
    workspace = Path(request["workspace"]).resolve()
    args = request.get("args", [])
    kwargs = request.get("kwargs", {})
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            module = import_entrypoint(workspace, str(request["entrypoint"]))
            function = getattr(module, str(request["call"]))
            result = function(*args, **kwargs)
        return {
            "ok": True,
            "result": result,
            "args": args,
            "kwargs": kwargs,
        }
    except Exception as exc:  # noqa: BLE001 - exception identity is grader data
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "args": args,
            "kwargs": kwargs,
        }


def main() -> int:
    status = 0
    try:
        request = json.loads(sys.stdin.read())
        response = execute(request)
        output = json.dumps(response, ensure_ascii=True, sort_keys=True)
    except Exception as exc:  # noqa: BLE001 - machine-readable infrastructure error
        output = json.dumps({
            "infrastructure_error": f"{type(exc).__name__}: {exc}"
        })
        status = 2
    payload = (output + "\n").encode("utf-8")
    written = 0
    while written < len(payload):
        written += RAW_WRITE(1, payload[written:])
    RAW_EXIT(status)


if __name__ == "__main__":
    sys.exit(main())
