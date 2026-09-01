#!/usr/bin/env python3
"""Reproducible paired benchmark for Semantic Spec Writer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"
CASES_DIR = BENCHMARKS / "cases"
RESULTS_DIR = BENCHMARKS / "results"
VARIANTS = {
    "baseline": "baseline.md",
    "semantic": "semantic.spec.ctx",
}


@dataclass(frozen=True)
class BenchmarkCase:
    path: Path
    manifest: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.manifest["id"])

    def spec_path(self, variant: str) -> Path:
        return self.path / VARIANTS[variant]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def discover_cases(selected: Iterable[str] | None = None) -> list[BenchmarkCase]:
    wanted = set(selected or [])
    cases: list[BenchmarkCase] = []
    for manifest_path in sorted(CASES_DIR.glob("*/case.json")):
        manifest = read_json(manifest_path)
        case = BenchmarkCase(manifest_path.parent, manifest)
        if not wanted or case.id in wanted:
            cases.append(case)
    missing = wanted - {case.id for case in cases}
    if missing:
        raise ValueError(f"unknown benchmark cases: {', '.join(sorted(missing))}")
    return cases


def document_metrics(path: Path) -> dict[str, int]:
    text = path.read_text(encoding="utf-8")
    return {
        "bytes": len(text.encode("utf-8")),
        "characters": len(text),
        "words": len(text.split()),
        "lines": len(text.splitlines()),
    }


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def tree_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def compression_percent(baseline: int, semantic: int) -> float:
    return round((baseline - semantic) / baseline * 100, 2) if baseline else 0.0


def static_rows(cases: list[BenchmarkCase]) -> list[dict[str, Any]]:
    rows = []
    for case in cases:
        baseline = document_metrics(case.spec_path("baseline"))
        semantic = document_metrics(case.spec_path("semantic"))
        rows.append({
            "case": case.id,
            "title": case.manifest["title"],
            "baseline": baseline,
            "semantic": semantic,
            "byte_reduction_percent": compression_percent(
                baseline["bytes"], semantic["bytes"]
            ),
            "word_reduction_percent": compression_percent(
                baseline["words"], semantic["words"]
            ),
        })
    return rows


def render_static_markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "| Case | Baseline bytes | Semantic bytes | Byte reduction | Word reduction |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| `{row['case']}` | {row['baseline']['bytes']} | "
            f"{row['semantic']['bytes']} | {row['byte_reduction_percent']:.2f}% | "
            f"{row['word_reduction_percent']:.2f}% |"
        )
    if rows:
        lines.append(
            f"| **Median** |  |  | "
            f"**{statistics.median(r['byte_reduction_percent'] for r in rows):.2f}%** | "
            f"**{statistics.median(r['word_reduction_percent'] for r in rows):.2f}%** |"
        )
    return "\n".join(lines) + "\n"


def run_grader(case: BenchmarkCase, workspace: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        str(BENCHMARKS / "grader.py"),
        str(case.path),
        str(workspace),
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"grader failed for {case.id}: {completed.stdout.strip()} "
            f"{completed.stderr.strip()}"
        )
    return json.loads(completed.stdout)


def validate_case(case: BenchmarkCase) -> list[str]:
    errors: list[str] = []
    required = [
        case.path / "case.json",
        case.path / "baseline.md",
        case.path / "semantic.spec.ctx",
        case.path / "tests.json",
        case.path / "starter" / case.manifest.get("entrypoint", ""),
        case.path / "reference" / case.manifest.get("entrypoint", ""),
    ]
    for path in required:
        if not path.is_file():
            errors.append(f"{case.id}: missing {path.relative_to(ROOT)}")
    if errors:
        return errors

    semantic = case.spec_path("semantic").read_text(encoding="utf-8")
    if not semantic.startswith("spec\n"):
        errors.append(f"{case.id}: semantic spec must start with 'spec'")
    if "open_questions:" not in semantic:
        errors.append(f"{case.id}: semantic spec lacks open_questions")

    suite = read_json(case.path / "tests.json")
    if not suite.get("tests"):
        errors.append(f"{case.id}: test suite is empty")
        return errors
    acceptance_ids = {test.get("acceptance") for test in suite["tests"]}
    if None in acceptance_ids:
        errors.append(f"{case.id}: every test must map to an acceptance id")
    baseline = case.spec_path("baseline").read_text(encoding="utf-8")
    defined_acceptance_ids = set(re.findall(r"^\s+(A\d+):", semantic, re.MULTILINE))
    unmapped = defined_acceptance_ids - acceptance_ids
    if unmapped:
        errors.append(
            f"{case.id}: acceptance ids without tests: {', '.join(sorted(unmapped))}"
        )
    for acceptance_id in sorted(item for item in acceptance_ids if item):
        if acceptance_id not in baseline:
            errors.append(f"{case.id}: baseline lacks {acceptance_id}")
        if acceptance_id not in semantic:
            errors.append(f"{case.id}: semantic spec lacks {acceptance_id}")

    reference_grade = run_grader(case, case.path / "reference")
    if reference_grade["passed"] != reference_grade["total"]:
        errors.append(f"{case.id}: reference solution does not pass all tests")
    starter_grade = run_grader(case, case.path / "starter")
    if starter_grade["passed"] == starter_grade["total"]:
        errors.append(f"{case.id}: starter already passes all tests")
    return errors


def validate(cases: list[BenchmarkCase]) -> list[str]:
    errors: list[str] = []
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        errors.append("duplicate case ids")
    for case in cases:
        errors.extend(validate_case(case))
    return errors


def safe_workspace(case: BenchmarkCase, root: Path) -> Path:
    workspace = root / case.id
    if workspace.exists():
        raise RuntimeError(f"refusing to overwrite workspace: {workspace}")
    shutil.copytree(case.path / "starter", workspace)
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    return workspace


def benchmark_prompt(spec: str) -> str:
    return (
        "Implement the specification below in the current workspace.\n"
        "Keep changes scoped to the requested behavior. Do not access files outside "
        "the workspace. Do not use network access. Finish only after checking the "
        "implementation for syntax errors.\n\n"
        "--- BEGIN SPECIFICATION ---\n"
        f"{spec.rstrip()}\n"
        "--- END SPECIFICATION ---\n"
    )


def parse_codex_events(stdout: str) -> dict[str, Any]:
    usage: dict[str, int] = {}
    tool_calls: dict[str, int] = {}
    thread_id: str | None = None
    final_message = ""
    event_errors: list[str] = []

    for raw_line in stdout.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            event_errors.append("non-JSON line in Codex output")
            continue
        event_type = event.get("type")
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
        elif event_type == "turn.completed":
            usage = event.get("usage", {})
        elif event_type in {"turn.failed", "error"}:
            event_errors.append(str(event.get("error") or event.get("message") or event))
        elif event_type == "item.completed":
            item = event.get("item", {})
            item_type = str(item.get("type", "unknown"))
            if item_type == "agent_message":
                final_message = str(item.get("text", ""))
            elif item_type in {
                "command_execution",
                "file_change",
                "mcp_tool_call",
                "web_search",
            }:
                tool_calls[item_type] = tool_calls.get(item_type, 0) + 1

    if usage:
        usage = {key: int(value) for key, value in usage.items() if isinstance(value, int)}
        usage["uncached_input_tokens"] = max(
            usage.get("input_tokens", 0) - usage.get("cached_input_tokens", 0), 0
        )
    return {
        "usage": usage,
        "tool_calls": tool_calls,
        "tool_call_total": sum(tool_calls.values()),
        "thread_id": thread_id,
        "final_message": final_message[-2000:],
        "event_errors": event_errors,
    }


def safe_environment() -> dict[str, str]:
    allowed = {
        "CODEX_HOME",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TERM",
        "TMPDIR",
        "USER",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment["NO_COLOR"] = "1"
    return environment


def stop_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def run_codex(
    workspace: Path,
    prompt: str,
    model: str | None,
    reasoning_effort: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    command = [
        "codex",
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(workspace),
    ]
    if model:
        command.extend(["--model", model])
    if reasoning_effort:
        command.extend([
            "--config",
            f'model_reasoning_effort="{reasoning_effort}"',
        ])
    command.append("-")

    started = time.monotonic()
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=safe_environment(),
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(prompt, timeout=timeout_seconds)
    except BaseException:
        stop_process_group(process)
        raise
    elapsed = time.monotonic() - started
    parsed = parse_codex_events(stdout)
    parsed.update({
        "return_code": process.returncode,
        "duration_seconds": round(elapsed, 3),
        "stderr_tail": stderr[-2000:],
    })
    return parsed


def run_mock(case: BenchmarkCase, workspace: Path) -> dict[str, Any]:
    started = time.monotonic()
    entrypoint = case.manifest["entrypoint"]
    shutil.copy2(case.path / "reference" / entrypoint, workspace / entrypoint)
    return {
        "return_code": 0,
        "duration_seconds": round(time.monotonic() - started, 6),
        "usage": {},
        "tool_calls": {},
        "tool_call_total": None,
        "thread_id": None,
        "final_message": "mock provider copied the reference solution",
        "event_errors": [],
        "stderr_tail": "",
    }


def command_version(command: list[str]) -> str | None:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    output = (result.stdout or result.stderr).strip()
    return output.splitlines()[0] if output else None


def git_commit() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def estimate_cost(usage: dict[str, int], pricing: dict[str, float]) -> float | None:
    if not pricing:
        return None
    input_tokens = usage.get("input_tokens", 0)
    cached_tokens = usage.get("cached_input_tokens", 0)
    cache_write_tokens = usage.get("cache_write_input_tokens", 0)
    if cache_write_tokens and "cache_write_input" not in pricing:
        return None
    uncached_tokens = max(input_tokens - cached_tokens - cache_write_tokens, 0)
    output_tokens = usage.get("output_tokens", 0)
    cost = (
        uncached_tokens * pricing.get("input", 0.0)
        + cached_tokens * pricing.get("cached_input", 0.0)
        + cache_write_tokens * pricing.get("cache_write_input", 0.0)
        + output_tokens * pricing.get("output", 0.0)
    ) / 1_000_000
    return round(cost, 8)


def create_run_document(args: argparse.Namespace, cases: list[BenchmarkCase]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "created_at": datetime.now(UTC).isoformat(),
        "provider": args.provider,
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "repetitions": args.repetitions,
        "seed": args.seed,
        "pricing_usd_per_million": args.pricing,
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "codex": command_version(["codex", "--version"]),
            "git_commit": git_commit(),
        },
        "cases": [case.id for case in cases],
        "oracle_exposure": "possible: grader files are outside the workspace but not container-isolated",
        "static": static_rows(cases),
        "results": [],
    }


def execute_benchmark(args: argparse.Namespace) -> Path:
    cases = discover_cases(args.case)
    errors = validate(cases)
    if errors:
        raise RuntimeError("benchmark validation failed:\n" + "\n".join(errors))

    output = args.output or RESULTS_DIR / (
        datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ") + ".json"
    )
    output = output.resolve()
    if output.exists() and not args.force:
        raise RuntimeError(f"refusing to overwrite result: {output}; pass --force to replace it")
    document = create_run_document(args, cases)
    pairs = [
        (case, repetition)
        for case in cases
        for repetition in range(1, args.repetitions + 1)
    ]
    random.Random(args.seed).shuffle(pairs)
    jobs: list[tuple[BenchmarkCase, int, str]] = []
    for pair_index, (case, repetition) in enumerate(pairs):
        order = ["baseline", "semantic"]
        if (pair_index + args.seed) % 2:
            order.reverse()
        jobs.extend((case, repetition, variant) for variant in order)

    keep_root = RESULTS_DIR / "workspaces" / document["run_id"]
    temporary_context = None
    if args.keep_workspaces:
        keep_root.mkdir(parents=True, exist_ok=False)
        workspace_root = keep_root
    else:
        temporary_context = tempfile.TemporaryDirectory(prefix="semantic-spec-bench-")
        workspace_root = Path(temporary_context.name)

    try:
        for index, (case, repetition, variant) in enumerate(jobs, start=1):
            run_root = workspace_root / f"{index:03d}-{case.id}-{variant}-r{repetition}"
            run_root.mkdir(parents=True, exist_ok=False)
            workspace = safe_workspace(case, run_root)
            spec = case.spec_path(variant).read_text(encoding="utf-8")
            prompt = benchmark_prompt(spec)
            print(
                f"[{index}/{len(jobs)}] {case.id} {variant} repetition={repetition}",
                flush=True,
            )
            try:
                if args.provider == "codex":
                    provider_result = run_codex(
                        workspace,
                        prompt,
                        args.model,
                        args.reasoning_effort,
                        args.timeout_seconds,
                    )
                else:
                    provider_result = run_mock(case, workspace)
                grade = run_grader(case, workspace)
                error = None
            except subprocess.TimeoutExpired:
                provider_result = {
                    "return_code": None,
                    "duration_seconds": args.timeout_seconds,
                    "usage": {},
                    "tool_calls": {},
                    "tool_call_total": None,
                    "event_errors": ["provider timeout"],
                    "stderr_tail": "",
                }
                total = len(read_json(case.path / "tests.json")["tests"])
                grade = {"passed": 0, "total": total, "pass_rate": 0.0, "acceptance_passed": 0, "acceptance_total": 0, "acceptance_pass_rate": 0.0, "task_success": False, "failures": []}
                error = "provider timeout"
            except Exception as exc:  # noqa: BLE001 - preserve failed run and continue
                provider_result = {
                    "return_code": None,
                    "duration_seconds": None,
                    "usage": {},
                    "tool_calls": {},
                    "tool_call_total": None,
                    "event_errors": [],
                    "stderr_tail": "",
                }
                total = len(read_json(case.path / "tests.json")["tests"])
                grade = {"passed": 0, "total": total, "pass_rate": 0.0, "acceptance_passed": 0, "acceptance_total": 0, "acceptance_pass_rate": 0.0, "task_success": False, "failures": []}
                error = f"{type(exc).__name__}: {exc}"

            usage = provider_result.get("usage", {})
            result = {
                "case": case.id,
                "pair_id": f"{case.id}:r{repetition}",
                "variant": variant,
                "repetition": repetition,
                "run_order": index,
                "spec": document_metrics(case.spec_path(variant)),
                "provenance": {
                    "spec_sha256": sha256_bytes(spec.encode("utf-8")),
                    "prompt_sha256": sha256_bytes(prompt.encode("utf-8")),
                    "starter_sha256": tree_sha256(case.path / "starter"),
                    "fixture_sha256": tree_sha256(case.path),
                },
                "provider": provider_result,
                "grade": grade,
                "cost_usd": estimate_cost(usage, args.pricing),
                "error": error,
            }
            document["results"].append(result)
            write_json_atomic(output, document)
    finally:
        if temporary_context is not None:
            temporary_context.cleanup()

    return output


def median(values: list[float | int]) -> float | None:
    return round(float(statistics.median(values)), 3) if values else None


def format_metric(value: Any, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value}{suffix}"


def aggregate_variant(results: list[dict[str, Any]], variant: str) -> dict[str, Any]:
    selected = [result for result in results if result["variant"] == variant]

    def usage_values(key: str) -> list[int]:
        return [
            result["provider"]["usage"][key]
            for result in selected
            if key in result["provider"].get("usage", {})
        ]

    return {
        "runs": len(selected),
        "successful_provider_runs": sum(
            result["provider"].get("return_code") == 0 for result in selected
        ),
        "test_pass_rate": round(
            sum(result["grade"]["passed"] for result in selected)
            / max(sum(result["grade"]["total"] for result in selected), 1),
            4,
        ),
        "acceptance_pass_rate": round(
            sum(result["grade"].get("acceptance_passed", 0) for result in selected)
            / max(sum(result["grade"].get("acceptance_total", 0) for result in selected), 1),
            4,
        ),
        "task_success_rate": round(
            sum(bool(result["grade"].get("task_success")) for result in selected)
            / max(len(selected), 1),
            4,
        ),
        "median_input_tokens": median(usage_values("input_tokens")),
        "median_uncached_input_tokens": median(usage_values("uncached_input_tokens")),
        "median_output_tokens": median(usage_values("output_tokens")),
        "total_input_tokens": sum(usage_values("input_tokens")),
        "total_uncached_input_tokens": sum(usage_values("uncached_input_tokens")),
        "total_output_tokens": sum(usage_values("output_tokens")),
        "median_duration_seconds": median([
            result["provider"]["duration_seconds"]
            for result in selected
            if result["provider"].get("duration_seconds") is not None
        ]),
        "total_duration_seconds": round(sum(
            result["provider"]["duration_seconds"]
            for result in selected
            if result["provider"].get("duration_seconds") is not None
        ), 3),
        "median_tool_calls": median([
            result["provider"]["tool_call_total"]
            for result in selected
            if result["provider"].get("tool_call_total") is not None
        ]),
        "total_cost_usd": round(sum(
            result["cost_usd"] for result in selected if result["cost_usd"] is not None
        ), 6) if any(result["cost_usd"] is not None for result in selected) else None,
    }


def paired_reductions(results: list[dict[str, Any]], field: str) -> list[float]:
    pairs: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    for result in results:
        key = (result["case"], result["repetition"])
        pairs.setdefault(key, {})[result["variant"]] = result
    reductions = []
    for pair in pairs.values():
        if set(pair) != set(VARIANTS):
            continue
        baseline = pair["baseline"]["provider"].get("usage", {}).get(field)
        semantic = pair["semantic"]["provider"].get("usage", {}).get(field)
        if baseline:
            reductions.append((baseline - semantic) / baseline * 100)
    return reductions


def metric_delta_percent(baseline: float | int, semantic: float | int) -> float | None:
    if not baseline:
        return None
    return round((semantic - baseline) / baseline * 100, 3)


def paired_summary(results: list[dict[str, Any]], field: str) -> dict[str, Any]:
    pairs: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    for result in results:
        key = (result["case"], result["repetition"])
        pairs.setdefault(key, {})[result["variant"]] = result

    by_case: dict[str, list[float]] = {}
    values: list[float] = []
    for (case, _), pair in pairs.items():
        if set(pair) != set(VARIANTS):
            continue
        baseline = pair["baseline"]["provider"].get("usage", {}).get(field)
        semantic = pair["semantic"]["provider"].get("usage", {}).get(field)
        if not baseline or semantic is None:
            continue
        reduction = (baseline - semantic) / baseline * 100
        values.append(reduction)
        by_case.setdefault(case, []).append(reduction)

    case_medians = {
        case: float(statistics.median(case_values))
        for case, case_values in by_case.items()
    }
    confidence_interval = None
    if case_medians:
        population = list(case_medians.values())
        randomizer = random.Random(20260901)
        samples = sorted(
            statistics.median(randomizer.choices(population, k=len(population)))
            for _ in range(10_000)
        )
        confidence_interval = [round(samples[249], 3), round(samples[9749], 3)]

    return {
        "pairs": len(values),
        "wins": sum(value > 0 for value in values),
        "ties": sum(value == 0 for value in values),
        "losses": sum(value < 0 for value in values),
        "median_reduction_percent": median(values),
        "minimum_reduction_percent": round(min(values), 3) if values else None,
        "maximum_reduction_percent": round(max(values), 3) if values else None,
        "fixture_medians": {
            case: round(value, 3) for case, value in sorted(case_medians.items())
        },
        "fixture_cluster_bootstrap_95_ci": confidence_interval,
    }


def format_delta(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.3f}%"


def render_report(document: dict[str, Any]) -> str:
    results = document["results"]
    baseline = aggregate_variant(results, "baseline")
    semantic = aggregate_variant(results, "semantic")
    input_reductions = paired_reductions(results, "input_tokens")
    uncached_reductions = paired_reductions(results, "uncached_input_tokens")
    input_summary = paired_summary(results, "input_tokens")
    uncached_summary = paired_summary(results, "uncached_input_tokens")
    complete_pairs = len(results) == (
        len(document["cases"]) * document["repetitions"] * len(VARIANTS)
    )
    credible = (
        document["provider"] != "mock"
        and bool(document.get("model"))
        and bool(document.get("reasoning_effort"))
        and document["repetitions"] >= 3
        and complete_pairs
        and all(result.get("error") is None for result in results)
        and all(result["provider"].get("return_code") == 0 for result in results)
        and all("input_tokens" in result["provider"].get("usage", {}) for result in results)
    )

    lines = [
        "# Semantic Spec Writer Benchmark",
        "",
        f"Run: `{document['run_id']}`  ",
        f"Provider: `{document['provider']}`  ",
        f"Model: `{document.get('model') or 'provider default'}`  ",
        f"Reasoning effort: `{document.get('reasoning_effort') or 'provider default'}`  ",
        f"Cases: {len(document['cases'])}  ",
        f"Repetitions: {document['repetitions']}",
        "",
    ]
    if not credible:
        lines.extend([
            "> This run is a smoke test, not publishable performance evidence. "
            "Use a named real model and reasoning effort, at least three repetitions, "
            "all paired cases, successful provider runs, and complete token telemetry.",
            "",
        ])
    lines.extend([
        "## Implementation results",
        "",
        "| Variant | Runs | Test pass rate | Acceptance pass rate | Task success | Median input tokens | Median uncached input | Median output tokens | Median duration | Median tool calls | Estimated cost |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for name, values in (("Markdown", baseline), ("Semantic", semantic)):
        lines.append(
            f"| {name} | {values['runs']} | {values['test_pass_rate'] * 100:.2f}% | "
            f"{values['acceptance_pass_rate'] * 100:.2f}% | "
            f"{values['task_success_rate'] * 100:.2f}% | "
            f"{format_metric(values['median_input_tokens'])} | "
            f"{format_metric(values['median_uncached_input_tokens'])} | "
            f"{format_metric(values['median_output_tokens'])} | "
            f"{format_metric(values['median_duration_seconds'], 's')} | "
            f"{format_metric(values['median_tool_calls'])} | "
            f"{format_metric(values['total_cost_usd'], ' USD')} |"
        )
    input_reduction = median(input_reductions)
    uncached_reduction = median(uncached_reductions)
    total_metrics = [
        ("Input tokens", "total_input_tokens"),
        ("Uncached input tokens", "total_uncached_input_tokens"),
        ("Output tokens", "total_output_tokens"),
        ("Agent wall time", "total_duration_seconds"),
    ]
    lines.extend([
        "",
        f"Median paired input-token reduction: **{format_metric(input_reduction, '%')}**",
        "",
        f"Median paired uncached-input reduction: **{format_metric(uncached_reduction, '%')}**",
        "",
        "## Corpus totals",
        "",
        "Positive delta means the semantic variant used more of the metric.",
        "",
        "| Metric | Markdown | Semantic | Semantic delta |",
        "|---|---:|---:|---:|",
    ])
    for label, key in total_metrics:
        suffix = "s" if key == "total_duration_seconds" else ""
        lines.append(
            f"| {label} | {format_metric(baseline[key], suffix)} | "
            f"{format_metric(semantic[key], suffix)} | "
            f"{format_delta(metric_delta_percent(baseline[key], semantic[key]))} |"
        )
    input_ci = input_summary["fixture_cluster_bootstrap_95_ci"]
    uncached_ci = uncached_summary["fixture_cluster_bootstrap_95_ci"]
    lines.extend([
        "",
        "## Paired variability",
        "",
        f"Input-token pairs: semantic lower in {input_summary['wins']}/{input_summary['pairs']}, "
        f"higher in {input_summary['losses']}/{input_summary['pairs']}; range "
        f"{format_metric(input_summary['minimum_reduction_percent'], '%')} to "
        f"{format_metric(input_summary['maximum_reduction_percent'], '%')}.",
        "",
        f"Fixture-cluster bootstrap 95% CI for median input-token reduction: "
        f"**[{format_metric(input_ci[0], '%')}, {format_metric(input_ci[1], '%')}]**."
        if input_ci else "Fixture-cluster bootstrap CI: n/a.",
        "",
        f"Uncached-input pairs: semantic lower in {uncached_summary['wins']}/{uncached_summary['pairs']}, "
        f"higher in {uncached_summary['losses']}/{uncached_summary['pairs']}; "
        f"95% CI **[{format_metric(uncached_ci[0], '%')}, {format_metric(uncached_ci[1], '%')}]**."
        if uncached_ci else "Uncached-input paired summary: n/a.",
        "",
        "| Case | Median input-token reduction | Median uncached-input reduction |",
        "|---|---:|---:|",
    ])
    for case in sorted(input_summary["fixture_medians"]):
        lines.append(
            f"| `{case}` | {input_summary['fixture_medians'][case]:.3f}% | "
            f"{uncached_summary['fixture_medians'][case]:.3f}% |"
        )
    lines.extend([
        "",
        "## Static document size",
        "",
        render_static_markdown(document["static"]).rstrip(),
        "",
        "## Interpretation",
        "",
        "Static size reduction proves only that the document is shorter. Acceptance results "
        "test whether implementation-relevant behavior survived compression. Provider token "
        "usage includes the full agent loop, not only the specification text. This run preserves "
        "all tested behavior but does not demonstrate an end-to-end token saving when corpus "
        "totals and the confidence interval are considered.",
        "",
        "## Limitations",
        "",
        "- Eight small, synthetic Python fixtures are not representative of every codebase.",
        f"- Results cover one model (`{document.get('model') or 'provider default'}`) and "
        f"one reasoning effort (`{document.get('reasoning_effort') or 'provider default'}`).",
        "- The benchmark excludes the one-time cost of creating or reviewing a semantic spec.",
        "- Acceptance tests are held outside the agent workspace, but the runner does not use "
        "a container and therefore cannot prove oracle isolation against a hostile agent.",
        "- More fixtures, models, and repetitions are required before making a general token "
        "or latency claim.",
        "",
    ])
    return "\n".join(lines)


def parse_pricing(value: str | None) -> dict[str, float]:
    if value is None:
        return {}
    pricing = read_json(Path(value))
    allowed = {"input", "cached_input", "cache_write_input", "output"}
    unknown = set(pricing) - allowed
    if unknown:
        raise ValueError(f"unknown pricing keys: {', '.join(sorted(unknown))}")
    return {key: float(item) for key, item in pricing.items()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate all cases")
    validate_parser.add_argument("--case", action="append", default=[])

    static_parser = subparsers.add_parser("static", help="measure document sizes")
    static_parser.add_argument("--case", action="append", default=[])
    static_parser.add_argument("--json", action="store_true")
    static_parser.add_argument("--check", action="store_true", help="fail unless every semantic spec is smaller")

    run_parser = subparsers.add_parser("run", help="execute paired implementation runs")
    run_parser.add_argument("--provider", choices=["codex", "mock"], default="mock")
    run_parser.add_argument("--model")
    run_parser.add_argument(
        "--reasoning-effort",
        choices=["minimal", "low", "medium", "high", "xhigh"],
    )
    run_parser.add_argument("--case", action="append", default=[])
    run_parser.add_argument("--repetitions", type=int, default=1)
    run_parser.add_argument("--seed", type=int, default=20260901)
    run_parser.add_argument("--timeout-seconds", type=int, default=600)
    run_parser.add_argument("--output", type=Path)
    run_parser.add_argument("--keep-workspaces", action="store_true")
    run_parser.add_argument("--pricing-file")
    run_parser.add_argument("--force", action="store_true")

    report_parser = subparsers.add_parser("report", help="render Markdown from a JSON run")
    report_parser.add_argument("result", type=Path)
    report_parser.add_argument("--output", type=Path)
    report_parser.add_argument("--force", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "validate":
            cases = discover_cases(args.case)
            errors = validate(cases)
            if errors:
                print("\n".join(errors), file=sys.stderr)
                return 1
            print(f"validated {len(cases)} benchmark cases")
            return 0
        if args.command == "static":
            rows = static_rows(discover_cases(args.case))
            print(json.dumps(rows, indent=2) if args.json else render_static_markdown(rows), end="")
            if args.check and any(
                row["semantic"]["bytes"] >= row["baseline"]["bytes"] for row in rows
            ):
                return 1
            return 0
        if args.command == "run":
            if args.repetitions < 1:
                raise ValueError("repetitions must be at least 1")
            args.pricing = parse_pricing(args.pricing_file)
            output = execute_benchmark(args)
            print(f"wrote {output}")
            return 0
        if args.command == "report":
            report = render_report(read_json(args.result))
            if args.output:
                if args.output.exists() and not args.force:
                    raise RuntimeError(
                        f"refusing to overwrite report: {args.output}; pass --force to replace it"
                    )
                args.output.write_text(report, encoding="utf-8")
                print(f"wrote {args.output}")
            else:
                print(report, end="")
            return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
