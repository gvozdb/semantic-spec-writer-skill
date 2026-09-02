#!/usr/bin/env python3
"""Deterministic grader for isolated benchmark workspaces."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

import benchmark as core
from solution_runtime import import_entrypoint

WORKER = Path(__file__).resolve().with_name("solution_worker.py")
RUNTIME = Path(__file__).resolve().with_name("solution_runtime.py")


def strict_equal(actual: Any, expected: Any) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            strict_equal(actual[key], expected[key]) for key in expected
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            strict_equal(actual_item, expected_item)
            for actual_item, expected_item in zip(actual, expected, strict=True)
        )
    return actual == expected


def execute_test(
    workspace: Path,
    entrypoint: str,
    function_name: str,
    args: list[Any],
    kwargs: dict[str, Any],
    modules: dict[str, Any],
    untrusted: bool,
) -> dict[str, Any]:
    if untrusted:
        request = json.dumps({
            "workspace": str(workspace),
            "entrypoint": entrypoint,
            "call": function_name,
            "args": args,
            "kwargs": kwargs,
        })
        completed = core.run_restricted_sandbox(
            [sys.executable, str(WORKER)],
            workspace,
            [WORKER, RUNTIME, workspace],
            timeout=10,
            input_text=request,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "solution worker failed: "
                f"{completed.stdout.strip()} {completed.stderr.strip()}"
            )
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if not lines:
            raise RuntimeError("solution worker returned no result")
        if len(lines) != 1:
            raise RuntimeError("solution worker returned ambiguous output")
        response = json.loads(lines[0])
        if "infrastructure_error" in response:
            raise RuntimeError(response["infrastructure_error"])
        return response

    try:
        if entrypoint not in modules:
            modules[entrypoint] = import_entrypoint(workspace, entrypoint)
        function = getattr(modules[entrypoint], function_name)
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


def grade(
    case_dir: Path,
    workspace: Path,
    *,
    untrusted: bool = False,
    expected_case_sha256: str | None = None,
    expected_tests_sha256: str | None = None,
) -> dict[str, Any]:
    """Grade one workspace using stable, optionally hash-pinned oracle bytes."""

    case_dir = case_dir.resolve()
    workspace = workspace.resolve()
    case_bytes = core.read_stable_regular_file(
        case_dir / "case.json",
        "private grader case manifest",
    )
    tests_bytes = core.read_stable_regular_file(
        case_dir / "tests.json",
        "private grader tests",
    )
    if (
        expected_case_sha256 is not None
        and core.sha256_bytes(case_bytes) != expected_case_sha256
    ):
        raise RuntimeError("private grader case manifest does not match its snapshot")
    if (
        expected_tests_sha256 is not None
        and core.sha256_bytes(tests_bytes) != expected_tests_sha256
    ):
        raise RuntimeError("private grader tests do not match their snapshot")
    manifest = json.loads(case_bytes.decode("utf-8"))
    suite = json.loads(tests_bytes.decode("utf-8"))
    modules: dict[str, Any] = {}
    failures: list[dict[str, str]] = []
    acceptance_tests: dict[str, list[bool]] = {}

    for test in suite["tests"]:
        name = test["name"]
        acceptance_id = test["acceptance"]
        acceptance_tests.setdefault(acceptance_id, [])
        args = copy.deepcopy(test.get("args", []))
        kwargs = copy.deepcopy(test.get("kwargs", {}))
        original_args = copy.deepcopy(args)
        original_kwargs = copy.deepcopy(kwargs)

        entrypoint = str(test.get("entrypoint", manifest["entrypoint"]))
        response = execute_test(
            workspace,
            entrypoint,
            str(test["call"]),
            args,
            kwargs,
            modules,
            untrusted,
        )
        expected_error = test.get("expect_error")
        if expected_error is not None:
            if response["ok"]:
                failures.append({
                    "name": name,
                    "reason": f"expected {expected_error}, got success",
                })
                acceptance_tests[acceptance_id].append(False)
                continue
            if response["error_type"] != expected_error:
                failures.append({
                    "name": name,
                    "reason": (
                        f"expected {expected_error}, got {response['error_type']}: "
                        f"{response['error_message']}"
                    ),
                })
                acceptance_tests[acceptance_id].append(False)
                continue
            expected_message = test.get("error_message")
            if (
                expected_message is not None
                and response["error_message"] != expected_message
            ):
                failures.append({
                    "name": name,
                    "reason": (
                        f"expected error {expected_message!r}, got "
                        f"{response['error_message']!r}"
                    ),
                })
                acceptance_tests[acceptance_id].append(False)
                continue
            acceptance_tests[acceptance_id].append(True)
            continue

        if not response["ok"]:
            failures.append({
                "name": name,
                "reason": (
                    f"unexpected {response['error_type']}: "
                    f"{response['error_message']}"
                ),
            })
            acceptance_tests[acceptance_id].append(False)
            continue
        if not strict_equal(response["result"], test.get("expect")):
            failures.append({
                "name": name,
                "reason": (
                    f"expected {test.get('expect')!r}, got "
                    f"{response['result']!r}"
                ),
            })
            acceptance_tests[acceptance_id].append(False)
            continue
        if test.get("preserve_inputs") and (
            response["args"] != original_args
            or response["kwargs"] != original_kwargs
        ):
            failures.append({"name": name, "reason": "input was mutated"})
            acceptance_tests[acceptance_id].append(False)
            continue
        acceptance_tests[acceptance_id].append(True)

    total = len(suite["tests"])
    acceptance = {
        key: all(outcomes) for key, outcomes in sorted(acceptance_tests.items())
    }
    acceptance_passed = sum(acceptance.values())
    acceptance_total = len(acceptance)
    return {
        "passed": total - len(failures),
        "total": total,
        "pass_rate": (total - len(failures)) / total if total else 0.0,
        "acceptance": acceptance,
        "acceptance_passed": acceptance_passed,
        "acceptance_total": acceptance_total,
        "acceptance_pass_rate": (
            acceptance_passed / acceptance_total if acceptance_total else 0.0
        ),
        "task_success": acceptance_total > 0 and acceptance_passed == acceptance_total,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("case_dir", type=Path)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--untrusted", action="store_true")
    parser.add_argument("--case-sha256")
    parser.add_argument("--tests-sha256")
    args = parser.parse_args()

    try:
        result = grade(
            args.case_dir.resolve(),
            args.workspace.resolve(),
            untrusted=args.untrusted,
            expected_case_sha256=args.case_sha256,
            expected_tests_sha256=args.tests_sha256,
        )
    except Exception as exc:  # noqa: BLE001 - produce machine-readable infra errors
        print(json.dumps({"infrastructure_error": f"{type(exc).__name__}: {exc}"}))
        return 2

    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
