#!/usr/bin/env python3
"""Three-arm benchmark for repository-grounded execution packets."""

from __future__ import annotations

import argparse
import json
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


BENCHMARKS = Path(__file__).resolve().parent
ROOT = BENCHMARKS.parent
CASES_DIR = BENCHMARKS / "handoff-cases"
VARIANTS = {
    "markdown": "baseline.md",
    "semantic": "semantic.spec.ctx",
    "packet": "packet.spec.ctx",
}
sys.path.insert(0, str(BENCHMARKS))
import benchmark as core  # noqa: E402


def artifact_path(case: core.BenchmarkCase, variant: str) -> Path:
    return case.path / VARIANTS[variant]


def artifact_text(case: core.BenchmarkCase, variant: str) -> str:
    return artifact_path(case, variant).read_text(encoding="utf-8")


def validate_packet(case: core.BenchmarkCase) -> list[str]:
    path = artifact_path(case, "packet")
    if not path.is_file():
        return [f"{case.id}: missing packet.spec.ctx"]
    text = path.read_text(encoding="utf-8")
    errors = core.validate_semantic_text(case, text)
    errors.extend(core.validate_execution_packet_artifact(case, path))
    return errors


def validate_cases(cases: list[core.BenchmarkCase]) -> list[str]:
    errors = core.validate(cases)
    for case in cases:
        verification_command = case.manifest.get("verification_command")
        if not isinstance(verification_command, str) or not verification_command.strip():
            errors.append(f"{case.id}: handoff case requires verification_command")
        verification_files = case.manifest.get("verification_files")
        if not isinstance(verification_files, list) or not verification_files:
            errors.append(f"{case.id}: handoff case requires verification_files")
        else:
            try:
                core.verification_fixtures(case)
            except ValueError as exc:
                errors.append(str(exc))
        errors.extend(validate_packet(case))
    return errors


def static_rows(
    cases: list[core.BenchmarkCase],
    encoder: Any | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        variants: dict[str, dict[str, int]] = {}
        for variant in VARIANTS:
            text = artifact_text(case, variant)
            metrics = core.text_metrics(text)
            if encoder is not None:
                metrics["tokens"] = len(encoder.encode(text))
            variants[variant] = metrics
        rows.append({"case": case.id, "variants": variants})
    return rows


def render_static(rows: list[dict[str, Any]]) -> str:
    include_tokens = bool(
        rows and "tokens" in rows[0]["variants"]["markdown"]
    )
    unit = "tokens" if include_tokens else "bytes"
    lines = [
        f"| Case | Markdown {unit} | Semantic v1 {unit} | Packet v3 {unit} | Packet vs v1 |",
        "|---|---:|---:|---:|---:|",
    ]
    reductions = []
    for row in rows:
        variants = row["variants"]
        markdown = variants["markdown"][unit]
        semantic = variants["semantic"][unit]
        packet = variants["packet"][unit]
        reduction = core.compression_percent(semantic, packet)
        reductions.append(reduction)
        lines.append(
            f"| `{row['case']}` | {markdown} | {semantic} | {packet} | {reduction:+.2f}% |"
        )
    if reductions:
        lines.append(
            f"| **Median** |  |  |  | **{statistics.median(reductions):+.2f}%** |"
        )
    return "\n".join(lines) + "\n"


def failed_provider(message: str, duration: float | int | None = None) -> dict[str, Any]:
    return {
        "return_code": None,
        "duration_seconds": duration,
        "usage": {},
        "tool_calls": {},
        "tool_call_total": None,
        "thread_id": None,
        "final_message": "",
        "event_errors": [message],
        "stderr_tail": "",
    }


def case_snapshot(case: core.BenchmarkCase) -> dict[str, Any]:
    fixtures = core.verification_fixtures(case)
    return {
        "fixture_sha256": core.tree_sha256(case.path),
        "starter_sha256": core.tree_sha256(case.path / "starter"),
        "verification_fixture_sha256": core.verification_fixture_sha256(fixtures),
        "variants": {
            variant: core.sha256_bytes(artifact_text(case, variant).encode("utf-8"))
            for variant in VARIANTS
        },
    }


def require_case_snapshot(case: core.BenchmarkCase, expected: dict[str, Any]) -> None:
    if case_snapshot(case) != expected:
        raise RuntimeError(f"{case.id}: benchmark fixture changed during run")


def create_document(
    args: argparse.Namespace,
    cases: list[core.BenchmarkCase],
    variants: list[str],
) -> dict[str, Any]:
    corpus = core.discover_cases(cases_dir=CASES_DIR)
    return {
        "schema_version": 1,
        "kind": "semantic-execution-packet-comparison",
        "packet_version": 3,
        "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "created_at": datetime.now(UTC).isoformat(),
        "provider": args.provider,
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "repetitions": args.repetitions,
        "seed": args.seed,
        "cases": [case.id for case in cases],
        "full_corpus": {case.id for case in cases} == {case.id for case in corpus},
        "variants": variants,
        "fixture_snapshot": {case.id: case_snapshot(case) for case in cases},
        "environment": {
            "python": sys.version.split()[0],
            "codex": core.command_version(["codex", "--version"]),
            "git_commit": core.git_commit(),
        },
        "oracle_exposure": (
            "reduced: hidden tests and hidden expected outputs stay in the grader "
            "parent; visible smoke assertions are shared equally across arms"
        ),
        "static": static_rows(cases),
        "results": [],
    }


def run(args: argparse.Namespace) -> Path:
    cases = core.discover_cases(args.case, CASES_DIR)
    errors = validate_cases(cases)
    if errors:
        raise RuntimeError("handoff benchmark validation failed:\n" + "\n".join(errors))
    output = args.output.resolve()
    if output.exists() and not args.force:
        raise RuntimeError(f"refusing to overwrite result: {output}; pass --force")
    variant_names = args.variant or list(VARIANTS)
    if len(variant_names) != len(set(variant_names)):
        raise ValueError("duplicate --variant values are not allowed")
    document = create_document(args, cases, variant_names)
    pairs = [
        (case, repetition)
        for case in cases
        for repetition in range(1, args.repetitions + 1)
    ]
    random.Random(args.seed).shuffle(pairs)
    jobs: list[tuple[core.BenchmarkCase, int, str]] = []
    for index, (case, repetition) in enumerate(pairs):
        offset = (index + args.seed) % len(variant_names)
        order = variant_names[offset:] + variant_names[:offset]
        jobs.extend((case, repetition, variant) for variant in order)

    with tempfile.TemporaryDirectory(prefix="execution-packet-bench-") as directory:
        root = Path(directory)
        for index, (case, repetition, variant) in enumerate(jobs, start=1):
            expected_snapshot = document["fixture_snapshot"][case.id]
            require_case_snapshot(case, expected_snapshot)
            run_root = root / f"{index:03d}-{case.id}-{variant}-r{repetition}"
            run_root.mkdir()
            workspace = core.safe_workspace(case, run_root)
            specification = artifact_text(case, variant)
            prompt = core.benchmark_prompt(specification)
            print(
                f"[{index}/{len(jobs)}] {case.id} {variant} repetition={repetition}",
                flush=True,
            )
            provider = failed_provider("provider did not start")
            provider_completed = False
            verification = None
            grade = core.empty_grade(case)
            run_errors: list[str] = []
            try:
                provider = (
                    core.run_codex(
                        workspace,
                        prompt,
                        args.model,
                        args.reasoning_effort,
                        args.timeout_seconds,
                    )
                    if args.provider == "codex"
                    else core.run_mock(case, workspace)
                )
                provider_completed = True
            except subprocess.TimeoutExpired:
                provider = failed_provider("provider timeout", args.timeout_seconds)
                run_errors.append("provider timeout")
            except Exception as exc:  # noqa: BLE001 - preserve every run
                provider = failed_provider(f"{type(exc).__name__}: {exc}")
                run_errors.append(f"provider {type(exc).__name__}: {exc}")

            require_case_snapshot(case, expected_snapshot)

            if provider_completed:
                if provider.get("return_code") != 0:
                    run_errors.append(
                        f"provider exited with {provider.get('return_code')}"
                    )
                run_errors.extend(provider.get("event_errors", []))
                trusted = args.provider == "mock"
                verification_failed = False
                try:
                    verification = core.run_verification(
                        case, workspace, trusted=trusted
                    )
                    verification_failed = bool(
                        verification and verification["return_code"] != 0
                    )
                    if verification_failed:
                        run_errors.append("verification command failed")
                except subprocess.TimeoutExpired:
                    verification_failed = True
                    run_errors.append("verification timeout")
                except Exception as exc:  # noqa: BLE001 - preserve provider telemetry
                    verification_failed = True
                    run_errors.append(f"verification {type(exc).__name__}: {exc}")
                try:
                    grade = core.run_grader(case, workspace, trusted=trusted)
                except subprocess.TimeoutExpired:
                    run_errors.append("grader timeout")
                except Exception as exc:  # noqa: BLE001 - preserve provider telemetry
                    run_errors.append(f"grader {type(exc).__name__}: {exc}")
                if verification_failed:
                    grade["task_success"] = False

            require_case_snapshot(case, expected_snapshot)
            error = "; ".join(run_errors) if run_errors else None

            result = {
                "case": case.id,
                "pair_id": f"{case.id}:r{repetition}",
                "variant": variant,
                "repetition": repetition,
                "run_order": index,
                "spec": core.text_metrics(specification),
                "provenance": {
                    "spec_sha256": expected_snapshot["variants"][variant],
                    "prompt_sha256": core.sha256_bytes(prompt.encode("utf-8")),
                    "starter_sha256": expected_snapshot["starter_sha256"],
                    "fixture_sha256": expected_snapshot["fixture_sha256"],
                },
                "provider": provider,
                "verification": verification,
                "grade": grade,
                "error": error,
            }
            document["results"].append(result)
            core.write_json_atomic(output, document)
    return output


def metric(result: dict[str, Any], name: str) -> float | int | None:
    provider = result["provider"]
    if name in provider.get("usage", {}):
        return provider["usage"][name]
    if name == "duration_seconds":
        return provider.get("duration_seconds")
    if name == "tool_calls":
        return provider.get("tool_call_total")
    if name == "command_executions":
        return provider.get("tool_calls", {}).get("command_execution")
    if name == "discovery_commands":
        return provider.get("command_categories", {}).get("discovery")
    if name == "read_commands":
        return provider.get("command_categories", {}).get("read")
    if name == "verification_commands":
        return provider.get("command_categories", {}).get("verify")
    return None


def aggregate(results: list[dict[str, Any]], variant: str) -> dict[str, Any]:
    selected = [result for result in results if result["variant"] == variant]

    def values(name: str) -> list[float | int]:
        return [value for result in selected if (value := metric(result, name)) is not None]

    return {
        "runs": len(selected),
        "task_success_rate": sum(
            bool(result["grade"].get("task_success")) for result in selected
        ) / max(len(selected), 1),
        "test_pass_rate": sum(result["grade"]["passed"] for result in selected)
        / max(sum(result["grade"]["total"] for result in selected), 1),
        "acceptance_pass_rate": sum(
            result["grade"].get("acceptance_passed", 0) for result in selected
        ) / max(
            sum(result["grade"].get("acceptance_total", 0) for result in selected),
            1,
        ),
        **{
            f"total_{name}": round(sum(values(name)), 3)
            for name in (
                "input_tokens",
                "uncached_input_tokens",
                "output_tokens",
                "duration_seconds",
                "tool_calls",
                "command_executions",
                "discovery_commands",
                "read_commands",
                "verification_commands",
            )
        },
        **{
            f"median_{name}": core.median(values(name))
            for name in (
                "input_tokens",
                "uncached_input_tokens",
                "output_tokens",
                "duration_seconds",
                "tool_calls",
                "command_executions",
                "discovery_commands",
                "read_commands",
                "verification_commands",
            )
        },
    }


def comparison(
    results: list[dict[str, Any]],
    baseline: str,
    candidate: str,
    name: str,
) -> dict[str, Any]:
    pairs: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    for result in results:
        key = (result["case"], result["repetition"])
        pairs.setdefault(key, {})[result["variant"]] = result
    by_case: dict[str, list[float]] = {}
    reductions: list[float] = []
    baseline_total = 0.0
    candidate_total = 0.0
    for (case, _), pair in pairs.items():
        if baseline not in pair or candidate not in pair:
            continue
        if not (
            pair[baseline]["grade"].get("task_success")
            and pair[candidate]["grade"].get("task_success")
        ):
            continue
        left = metric(pair[baseline], name)
        right = metric(pair[candidate], name)
        if not left or right is None:
            continue
        reduction = (left - right) / left * 100
        reductions.append(reduction)
        baseline_total += left
        candidate_total += right
        by_case.setdefault(case, []).append(reduction)
    fixture_medians = {
        case: float(statistics.median(values)) for case, values in by_case.items()
    }
    confidence_interval = None
    if fixture_medians:
        population = list(fixture_medians.values())
        randomizer = random.Random(20260901)
        samples = sorted(
            statistics.median(randomizer.choices(population, k=len(population)))
            for _ in range(10_000)
        )
        confidence_interval = [round(samples[249], 3), round(samples[9749], 3)]
    return {
        "pairs": len(reductions),
        "wins": sum(value > 0 for value in reductions),
        "ties": sum(value == 0 for value in reductions),
        "losses": sum(value < 0 for value in reductions),
        "baseline_total": round(baseline_total, 3),
        "candidate_total": round(candidate_total, 3),
        "median_reduction_percent": core.median(reductions),
        "fixture_medians": {
            case: round(value, 3) for case, value in sorted(fixture_medians.items())
        },
        "fixture_cluster_bootstrap_95_ci": confidence_interval,
    }


def quality_not_worse(
    results: list[dict[str, Any]],
    baseline: str,
    candidate: str,
) -> bool:
    def preserved(selected: list[dict[str, Any]]) -> bool:
        left = aggregate(selected, baseline)
        right = aggregate(selected, candidate)
        return bool(
            right["task_success_rate"] >= left["task_success_rate"]
            and right["test_pass_rate"] >= left["test_pass_rate"]
            and right["acceptance_pass_rate"] >= left["acceptance_pass_rate"]
        )

    if not preserved(results):
        return False
    return all(
        preserved([result for result in results if result["case"] == case])
        for case in {result["case"] for result in results}
    )


def percent_delta(left: float | int, right: float | int) -> str:
    if not left:
        return "n/a"
    return f"{(right - left) / left * 100:+.2f}%"


def report_run_is_credible(
    document: dict[str, Any],
    results: list[dict[str, Any]],
) -> bool:
    cases = document.get("cases")
    repetitions = document.get("repetitions")
    variants = document.get("variants")
    snapshots = document.get("fixture_snapshot")
    try:
        corpus = core.discover_cases(cases_dir=CASES_DIR)
        current_snapshots = {case.id: case_snapshot(case) for case in corpus}
        current_static = static_rows(corpus)
    except (OSError, RuntimeError, ValueError):
        return False
    corpus_ids = {case.id for case in corpus}
    if (
        not isinstance(cases, list)
        or not cases
        or any(not isinstance(case, str) for case in cases)
        or len(cases) != len(set(cases))
        or set(cases) != corpus_ids
        or not isinstance(repetitions, int)
        or isinstance(repetitions, bool)
        or repetitions < 3
        or not isinstance(variants, list)
        or len(variants) != len(VARIANTS)
        or len(variants) != len(set(variants))
        or set(variants) != set(VARIANTS)
        or document.get("packet_version") != 3
        or not isinstance(snapshots, dict)
        or set(snapshots) != corpus_ids
        or snapshots != current_snapshots
        or document.get("static") != current_static
        or not results
    ):
        return False

    expected_keys = {
        (case, repetition, variant)
        for case in cases
        for repetition in range(1, repetitions + 1)
        for variant in VARIANTS
    }
    if any(not isinstance(result, dict) for result in results):
        return False
    actual_keys = [
        (result.get("case"), result.get("repetition"), result.get("variant"))
        for result in results
    ]
    if any(
        not isinstance(case, str)
        or not isinstance(repetition, int)
        or isinstance(repetition, bool)
        or not isinstance(variant, str)
        for case, repetition, variant in actual_keys
    ):
        return False
    if len(actual_keys) != len(set(actual_keys)) or set(actual_keys) != expected_keys:
        return False
    run_orders = [result.get("run_order") for result in results]
    if (
        any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in run_orders
        )
        or sorted(run_orders) != list(range(1, len(results) + 1))
    ):
        return False

    def nonnegative_int(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    def nonnegative_number(value: Any) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value >= 0
        )

    cases_by_id = {case.id: case for case in corpus}
    for result in results:
        provider = result.get("provider")
        verification = result.get("verification")
        provenance = result.get("provenance")
        grade = result.get("grade")
        usage = provider.get("usage") if isinstance(provider, dict) else None
        event_errors = provider.get("event_errors") if isinstance(provider, dict) else None
        case = cases_by_id.get(result.get("case"))
        variant = result.get("variant")
        if case is None or variant not in VARIANTS:
            return False
        snapshot = current_snapshots[case.id]
        specification = artifact_text(case, variant)
        prompt = core.benchmark_prompt(specification)
        expected_provenance = {
            "spec_sha256": snapshot["variants"][variant],
            "prompt_sha256": core.sha256_bytes(prompt.encode("utf-8")),
            "starter_sha256": snapshot["starter_sha256"],
            "fixture_sha256": snapshot["fixture_sha256"],
        }
        expected_verification = case.manifest.get("verification_command")
        if (
            result.get("error") is not None
            or result.get("pair_id")
            != f"{case.id}:r{result.get('repetition')}"
            or result.get("spec") != core.text_metrics(specification)
            or not isinstance(provider, dict)
            or provider.get("return_code") != 0
            or event_errors != []
            or not nonnegative_number(provider.get("duration_seconds"))
            or not nonnegative_int(provider.get("tool_call_total"))
            or not isinstance(provider.get("tool_calls"), dict)
            or not nonnegative_int(
                provider["tool_calls"].get("command_execution")
            )
            or not isinstance(provider.get("command_categories"), dict)
            or any(
                not nonnegative_int(provider["command_categories"].get(field))
                for field in ("discovery", "read", "verify")
            )
            or not isinstance(usage, dict)
            or any(
                not nonnegative_int(usage.get(field))
                for field in (
                    "input_tokens",
                    "uncached_input_tokens",
                    "output_tokens",
                )
            )
            or provenance != expected_provenance
            or not isinstance(verification, dict)
            or not isinstance(verification.get("command"), str)
            or not verification["command"].strip()
            or verification.get("command") != expected_verification
            or verification.get("fixture_sha256")
            != snapshot["verification_fixture_sha256"]
            or verification.get("return_code") != 0
            or not isinstance(grade, dict)
            or not isinstance(grade.get("task_success"), bool)
            or not nonnegative_int(grade.get("passed"))
            or not nonnegative_int(grade.get("total"))
            or not nonnegative_int(grade.get("acceptance_passed"))
            or not nonnegative_int(grade.get("acceptance_total"))
        ):
            return False
    return bool(
        document.get("provider") != "mock"
        and document.get("model")
        and document.get("reasoning_effort")
    )


def report(document: dict[str, Any]) -> str:
    if document.get("kind") != "semantic-execution-packet-comparison":
        raise ValueError("result is not an execution-packet comparison")
    results = document["results"]
    aggregates = {variant: aggregate(results, variant) for variant in VARIANTS}
    primary_uncached = comparison(
        results, "semantic", "packet", "uncached_input_tokens"
    )
    primary_input = comparison(results, "semantic", "packet", "input_tokens")
    primary_commands = comparison(
        results, "semantic", "packet", "command_executions"
    )
    primary_discovery = comparison(
        results, "semantic", "packet", "discovery_commands"
    )
    primary_reads = comparison(results, "semantic", "packet", "read_commands")
    primary_verification = comparison(
        results, "semantic", "packet", "verification_commands"
    )
    credible = report_run_is_credible(document, results)
    preserved = quality_not_worse(results, "semantic", "packet")
    semantic_successes = sum(
        result["grade"].get("task_success", False)
        for result in results
        if result["variant"] == "semantic"
    )
    packet_successes = sum(
        result["grade"].get("task_success", False)
        for result in results
        if result["variant"] == "packet"
    )
    quality_improved = bool(
        packet_successes > semantic_successes
        and aggregates["packet"]["test_pass_rate"]
        >= aggregates["semantic"]["test_pass_rate"]
        and aggregates["packet"]["acceptance_pass_rate"]
        >= aggregates["semantic"]["acceptance_pass_rate"]
    )
    uncached_ci = primary_uncached["fixture_cluster_bootstrap_95_ci"]
    expected_primary_pairs = len(document["cases"]) * document["repetitions"]
    full_primary_coverage = primary_uncached["pairs"] == expected_primary_pairs
    supported = bool(
        credible
        and preserved
        and full_primary_coverage
        and uncached_ci
        and uncached_ci[0] > 0
        and primary_uncached["candidate_total"] < primary_uncached["baseline_total"]
    )
    lines = [
        "# Execution Packet Benchmark",
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
            "> Experimental smoke run. Publishable evidence requires a named real model, "
            "three or more repetitions, the full suite, successful runs, and complete telemetry.",
            "",
        ])
    lines.extend([
        "## Quality and usage",
        "",
        "| Arm | Runs | Task success | Tests | Input tokens | Uncached input | Output tokens | Wall time | Discovery | Reads | Verify | Commands | Tool calls |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    labels = {"markdown": "Markdown", "semantic": "Semantic v1", "packet": "Packet v3"}
    for variant in VARIANTS:
        item = aggregates[variant]
        lines.append(
            f"| {labels[variant]} | {item['runs']} | {item['task_success_rate'] * 100:.2f}% | "
            f"{item['test_pass_rate'] * 100:.2f}% | {item['total_input_tokens']} | "
            f"{item['total_uncached_input_tokens']} | {item['total_output_tokens']} | "
            f"{item['total_duration_seconds']}s | {item['total_discovery_commands']} | "
            f"{item['total_read_commands']} | "
            f"{item['total_verification_commands']} | "
            f"{item['total_command_executions']} | "
            f"{item['total_tool_calls']} |"
        )
    lines.extend([
        "",
        "## Primary comparison: Packet v3 vs Semantic v1",
        "",
        "| Metric | Semantic v1 | Packet v3 | Packet delta | Paired median reduction | 95% fixture CI |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    comparisons = {
        "Input tokens": ("input_tokens", primary_input),
        "Uncached input": ("uncached_input_tokens", primary_uncached),
        "Shell commands": ("command_executions", primary_commands),
        "Discovery command events": ("discovery_commands", primary_discovery),
        "Read command events": ("read_commands", primary_reads),
        "Verification command events": (
            "verification_commands",
            primary_verification,
        ),
    }
    for label, (name, summary) in comparisons.items():
        left = summary["baseline_total"]
        right = summary["candidate_total"]
        ci = summary["fixture_cluster_bootstrap_95_ci"]
        ci_text = f"[{ci[0]:.3f}%, {ci[1]:.3f}%]" if ci else "n/a"
        paired_median = summary["median_reduction_percent"]
        paired_text = f"{paired_median}%" if paired_median is not None else "n/a"
        lines.append(
            f"| {label} | {left} | {right} | {percent_delta(left, right)} | "
            f"{paired_text} | {ci_text} |"
        )
    lines.extend([
        "",
        f"Packet used fewer uncached-input tokens in **{primary_uncached['wins']}/{primary_uncached['pairs']}** paired runs.",
        f"Primary comparison coverage: **{primary_uncached['pairs']}/{expected_primary_pairs}** jointly successful pairs.",
        "Only pairs where both v1 and v2 passed every acceptance group are included in the primary usage comparison.",
        "",
        "## Static artifacts",
        "",
        render_static(document["static"]).rstrip(),
        "",
        "## Verdict",
        "",
    ])
    if not preserved:
        lines.append(
            "Packet v3 regressed measured behavior. Any token or tool reduction is not a product benefit."
        )
    elif supported:
        lines.append(
            "This suite supports lower uncached-input usage for Packet v3 versus Semantic v1 while preserving measured behavior."
        )
    elif credible and quality_improved:
        uncached_delta = core.metric_delta_percent(
            aggregates["semantic"]["total_uncached_input_tokens"],
            aggregates["packet"]["total_uncached_input_tokens"],
        )
        uncached_usage = (
            f"{abs(uncached_delta):.2f}% fewer uncached-input tokens"
            if uncached_delta is not None and uncached_delta < 0
            else f"{abs(uncached_delta or 0):.2f}% more uncached-input tokens"
        )
        lines.append(
            f"Packet v3 improved measured task success ({packet_successes}/{len(document['cases']) * document['repetitions']} "
            f"versus {semantic_successes}/{len(document['cases']) * document['repetitions']}) and used "
            f"{uncached_usage} across all runs. "
            f"The strict equal-success token comparison covered only {primary_uncached['pairs']}/"
            f"{expected_primary_pairs} pairs, so this establishes a quality gain for this suite, "
            "not an isolated full-suite token-saving claim."
        )
    elif credible:
        lines.append(
            "Packet v3 preserved measured behavior but did not establish a reliable uncached-input reduction over Semantic v1."
        )
    else:
        lines.append(
            "Directional result only; this run cannot establish a Packet v3 token-saving claim."
        )
    lines.extend([
        "",
        "## Limits",
        "",
        "- Three synthetic Python fixtures are enough to reject a weak design, not enough for a broad product claim.",
        "- Command executions are coarse Codex events; one event may contain multiple shell operations.",
        "- Curated artifacts isolate execution behavior but exclude packet-authoring cost and reuse break-even.",
        "- Hidden tests and hidden expected outputs stay outside the solution process; "
        "visible smoke assertions are restored from immutable fixtures for every arm. "
        "Implementation agents use workspace-write; hidden solution calls use a "
        "network-disabled read-only sandbox. This is not a full VM boundary.",
        "- Results apply only to the recorded model, reasoning effort, repository shapes, and cache behavior.",
        "",
    ])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate_parser = commands.add_parser("validate")
    validate_parser.add_argument("--case", action="append", default=[])
    static_parser = commands.add_parser("static")
    static_parser.add_argument("--case", action="append", default=[])
    static_parser.add_argument("--token-encoding")
    static_parser.add_argument("--json", action="store_true")
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--provider", choices=["codex", "mock"], default="mock")
    run_parser.add_argument("--model")
    run_parser.add_argument(
        "--reasoning-effort",
        choices=["minimal", "low", "medium", "high", "xhigh"],
    )
    run_parser.add_argument("--case", action="append", default=[])
    run_parser.add_argument(
        "--variant",
        action="append",
        choices=list(VARIANTS),
        default=[],
        help="development-only arm filter; omit for a reportable three-arm run",
    )
    run_parser.add_argument("--repetitions", type=int, default=1)
    run_parser.add_argument("--seed", type=int, default=20260901)
    run_parser.add_argument("--timeout-seconds", type=int, default=600)
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--force", action="store_true")
    report_parser = commands.add_parser("report")
    report_parser.add_argument("result", type=Path)
    report_parser.add_argument("--output", type=Path)
    report_parser.add_argument("--force", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "validate":
            cases = core.discover_cases(args.case, CASES_DIR)
            errors = validate_cases(cases)
            if errors:
                print("\n".join(errors), file=sys.stderr)
                return 1
            print(f"validated {len(cases)} execution-packet cases")
            return 0
        if args.command == "static":
            cases = core.discover_cases(args.case, CASES_DIR)
            rows = static_rows(cases, core.load_token_encoder(args.token_encoding))
            print(json.dumps(rows, indent=2) if args.json else render_static(rows), end="")
            return 0
        if args.command == "run":
            if args.repetitions < 1:
                raise ValueError("repetitions must be at least 1")
            output = run(args)
            print(f"wrote {output}")
            return 0
        rendered = report(core.read_json(args.result))
        if args.output:
            if args.output.exists() and not args.force:
                raise RuntimeError(f"refusing to overwrite report: {args.output}; pass --force")
            args.output.write_text(rendered, encoding="utf-8")
            print(f"wrote {args.output}")
        else:
            print(rendered, end="")
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
