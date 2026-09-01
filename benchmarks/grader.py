#!/usr/bin/env python3
"""Deterministic grader for isolated benchmark workspaces."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def load_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("benchmark_solution", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def grade(case_dir: Path, workspace: Path) -> dict[str, Any]:
    manifest = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    suite = json.loads((case_dir / "tests.json").read_text(encoding="utf-8"))
    module = load_module(workspace / manifest["entrypoint"])
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

        try:
            function = getattr(module, test["call"])
            result = function(*args, **kwargs)
            if "expect_error" in test:
                failures.append({
                    "name": name,
                    "reason": f"expected {test['expect_error']}, got success",
                })
                acceptance_tests[acceptance_id].append(False)
                continue
            if not strict_equal(result, test.get("expect")):
                failures.append({
                    "name": name,
                    "reason": f"expected {test.get('expect')!r}, got {result!r}",
                })
                acceptance_tests[acceptance_id].append(False)
                continue
            if test.get("preserve_inputs") and (
                args != original_args or kwargs != original_kwargs
            ):
                failures.append({"name": name, "reason": "input was mutated"})
                acceptance_tests[acceptance_id].append(False)
                continue
            acceptance_tests[acceptance_id].append(True)
        except Exception as exc:  # noqa: BLE001 - exceptions are benchmark output
            expected = test.get("expect_error")
            if expected is None:
                failures.append({
                    "name": name,
                    "reason": f"unexpected {type(exc).__name__}: {exc}",
                })
                acceptance_tests[acceptance_id].append(False)
                continue
            if type(exc).__name__ != expected:
                failures.append({
                    "name": name,
                    "reason": f"expected {expected}, got {type(exc).__name__}: {exc}",
                })
                acceptance_tests[acceptance_id].append(False)
                continue
            expected_message = test.get("error_message")
            if expected_message is not None and str(exc) != expected_message:
                failures.append({
                    "name": name,
                    "reason": f"expected error {expected_message!r}, got {str(exc)!r}",
                })
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
    args = parser.parse_args()

    try:
        result = grade(args.case_dir.resolve(), args.workspace.resolve())
    except Exception as exc:  # noqa: BLE001 - produce machine-readable infra errors
        print(json.dumps({"infrastructure_error": f"{type(exc).__name__}: {exc}"}))
        return 2

    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
