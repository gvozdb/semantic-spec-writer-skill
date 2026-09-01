#!/usr/bin/env python3
"""Measure specification context-loading tokens without implementation-loop noise."""

from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


BENCHMARKS = Path(__file__).resolve().parent
ROOT = BENCHMARKS.parent
sys.path.insert(0, str(BENCHMARKS))
import benchmark as core  # noqa: E402


def reading_prompt(specification: str) -> str:
    return (
        "Read the document below. Do not use tools. Reply with exactly: OK\n\n"
        "--- BEGIN DOCUMENT ---\n"
        f"{specification.rstrip()}\n"
        "--- END DOCUMENT ---\n"
    )


def mock_provider(specification: str) -> dict[str, Any]:
    estimated_document_tokens = max(1, len(specification.encode("utf-8")) // 4)
    input_tokens = 10_000 + estimated_document_tokens
    return {
        "return_code": 0,
        "duration_seconds": 0.0,
        "usage": {
            "input_tokens": input_tokens,
            "cached_input_tokens": 8_192,
            "cache_write_input_tokens": 0,
            "output_tokens": 1,
            "reasoning_output_tokens": 0,
            "uncached_input_tokens": input_tokens - 8_192,
        },
        "tool_calls": {},
        "tool_call_total": 0,
        "thread_id": None,
        "final_message": "OK",
        "event_errors": [],
        "stderr_tail": "",
    }


def create_document(
    args: argparse.Namespace,
    cases: list[core.BenchmarkCase],
    semantic_specs: dict[str, str],
) -> dict[str, Any]:
    cases_dir = (args.cases_dir or core.CASES_DIR).resolve()
    corpus = core.discover_cases(cases_dir=cases_dir)
    return {
        "schema_version": 1,
        "kind": "semantic-spec-context-load",
        "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "created_at": datetime.now(UTC).isoformat(),
        "provider": args.provider,
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "repetitions": args.repetitions,
        "seed": args.seed,
        "cases": [case.id for case in cases],
        "case_suite": cases_dir.name,
        "full_corpus": {case.id for case in cases} == {case.id for case in corpus},
        "semantic_source": "generated" if args.semantic_dir else "curated",
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "codex": core.command_version(["codex", "--version"]),
            "git_commit": core.git_commit(),
        },
        "static": core.static_rows(cases, semantic_specs),
        "results": [],
    }


def run(args: argparse.Namespace) -> Path:
    cases_dir = (args.cases_dir or core.CASES_DIR).resolve()
    cases = core.discover_cases(args.case, cases_dir)
    fixture_errors = core.validate(cases)
    if fixture_errors:
        raise RuntimeError("benchmark validation failed:\n" + "\n".join(fixture_errors))
    semantic_specs = core.load_semantic_specs(cases, args.semantic_dir)
    output = args.output.resolve()
    if output.exists() and not args.force:
        raise RuntimeError(f"refusing to overwrite result: {output}; pass --force to replace it")

    document = create_document(args, cases, semantic_specs)
    pairs = [
        (case, repetition)
        for case in cases
        for repetition in range(1, args.repetitions + 1)
    ]
    random.Random(args.seed).shuffle(pairs)
    jobs: list[tuple[core.BenchmarkCase, int, str]] = []
    for pair_index, (case, repetition) in enumerate(pairs):
        variants = ["baseline", "semantic"]
        if (pair_index + args.seed) % 2:
            variants.reverse()
        jobs.extend((case, repetition, variant) for variant in variants)

    with tempfile.TemporaryDirectory(prefix="semantic-spec-context-") as temporary:
        workspace_root = Path(temporary)
        for index, (case, repetition, variant) in enumerate(jobs, start=1):
            print(
                f"[{index}/{len(jobs)}] {case.id} {variant} repetition={repetition}",
                flush=True,
            )
            workspace = workspace_root / f"{index:03d}-{case.id}-{variant}-r{repetition}"
            workspace.mkdir()
            subprocess.run(
                ["git", "init", "--quiet"],
                cwd=workspace,
                check=True,
                capture_output=True,
                text=True,
            )
            specification = (
                case.spec_path("baseline").read_text(encoding="utf-8")
                if variant == "baseline"
                else semantic_specs[case.id]
            )
            prompt = reading_prompt(specification)
            error = None
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
                    else mock_provider(specification)
                )
                failures = []
                if provider.get("return_code") != 0:
                    failures.append(f"provider exited with {provider.get('return_code')}")
                if provider.get("tool_call_total") != 0:
                    failures.append("provider used tools")
                if provider.get("final_message", "").strip() != "OK":
                    failures.append("provider did not return exactly OK")
                if provider.get("event_errors"):
                    failures.append("provider emitted event errors")
                if failures:
                    error = "; ".join(failures)
            except subprocess.TimeoutExpired:
                provider = {
                    "return_code": None,
                    "duration_seconds": args.timeout_seconds,
                    "usage": {},
                    "tool_calls": {},
                    "tool_call_total": None,
                    "thread_id": None,
                    "final_message": "",
                    "event_errors": ["provider timeout"],
                    "stderr_tail": "",
                }
                error = "provider timeout"

            document["results"].append({
                "case": case.id,
                "variant": variant,
                "repetition": repetition,
                "pair_id": f"{case.id}:r{repetition}",
                "run_order": index,
                "spec": core.text_metrics(specification),
                "provenance": {
                    "spec_sha256": core.sha256_bytes(specification.encode("utf-8")),
                    "prompt_sha256": core.sha256_bytes(prompt.encode("utf-8")),
                },
                "provider": provider,
                "error": error,
            })
            core.write_json_atomic(output, document)
    return output


def aggregate(results: list[dict[str, Any]], variant: str) -> dict[str, Any]:
    selected = [result for result in results if result["variant"] == variant]

    def usage_values(field: str) -> list[int]:
        return [
            result["provider"]["usage"][field]
            for result in selected
            if field in result["provider"].get("usage", {})
        ]

    input_tokens = usage_values("input_tokens")
    uncached_input_tokens = usage_values("uncached_input_tokens")
    output_tokens = usage_values("output_tokens")
    return {
        "runs": len(selected),
        "successful_reads": sum(result.get("error") is None for result in selected),
        "median_input_tokens": core.median(input_tokens),
        "median_uncached_input_tokens": core.median(uncached_input_tokens),
        "median_output_tokens": core.median(output_tokens),
        "total_input_tokens": sum(input_tokens),
        "total_uncached_input_tokens": sum(uncached_input_tokens),
        "total_output_tokens": sum(output_tokens),
    }


def report(document: dict[str, Any]) -> str:
    if document.get("kind") != "semantic-spec-context-load":
        raise ValueError("result is not a context-load benchmark")
    results = document["results"]
    baseline = aggregate(results, "baseline")
    semantic = aggregate(results, "semantic")
    input_summary = core.paired_summary(results, "input_tokens")
    uncached_summary = core.paired_summary(results, "uncached_input_tokens")
    complete = len(results) == len(document["cases"]) * document["repetitions"] * 2
    full_corpus = bool(document.get("full_corpus", (
        set(document["cases"]) == {case.id for case in core.discover_cases()}
    )))
    credible = bool(
        document["provider"] != "mock"
        and document.get("model")
        and document.get("reasoning_effort")
        and document["repetitions"] >= 3
        and full_corpus
        and complete
        and all(result.get("error") is None for result in results)
        and all("input_tokens" in result["provider"].get("usage", {}) for result in results)
    )
    total_saved = baseline["total_input_tokens"] - semantic["total_input_tokens"]
    saved_per_corpus_read = total_saved / document["repetitions"]
    input_ci = input_summary["fixture_cluster_bootstrap_95_ci"]
    supported = bool(
        credible
        and total_saved > 0
        and input_ci
        and input_ci[0] > 0
    )

    lines = [
        "# Semantic Spec Writer Context Benchmark",
        "",
        f"Run: `{document['run_id']}`  ",
        f"Provider: `{document['provider']}`  ",
        f"Model: `{document.get('model') or 'provider default'}`  ",
        f"Reasoning effort: `{document.get('reasoning_effort') or 'provider default'}`  ",
        f"Cases: {len(document['cases'])}  ",
        f"Repetitions: {document['repetitions']}  ",
        f"Semantic source: `{document.get('semantic_source', 'curated')}`",
        "",
    ]
    if not credible:
        lines.extend([
            "> This is a smoke run. Publishable context evidence requires the complete corpus, "
            "a named real model and reasoning effort, at least three repetitions, complete "
            "telemetry, exact `OK` responses, and zero tool calls.",
            "",
        ])
    lines.extend([
        "## Results",
        "",
        "Every run loads one document, performs no implementation, uses no tools, and returns the same fixed output.",
        "",
        "| Variant | Successful reads | Median input | Median uncached input | Median output | Total input | Total uncached input |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| Markdown | {baseline['successful_reads']}/{baseline['runs']} | {baseline['median_input_tokens']} | {baseline['median_uncached_input_tokens']} | {baseline['median_output_tokens']} | {baseline['total_input_tokens']} | {baseline['total_uncached_input_tokens']} |",
        f"| Semantic | {semantic['successful_reads']}/{semantic['runs']} | {semantic['median_input_tokens']} | {semantic['median_uncached_input_tokens']} | {semantic['median_output_tokens']} | {semantic['total_input_tokens']} | {semantic['total_uncached_input_tokens']} |",
        "",
        f"Semantic input saved across the run: **{total_saved} tokens**.",
        "",
        f"Semantic input saved per complete corpus read: **{saved_per_corpus_read:.1f} tokens**.",
        "",
        f"Median paired input-token reduction: **{input_summary['median_reduction_percent']}%**.",
        "",
        f"Fixture-cluster bootstrap 95% CI: **[{input_ci[0]}%, {input_ci[1]}%]**."
        if input_ci else "Fixture-cluster bootstrap 95% CI: n/a.",
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
        core.render_static_markdown(document["static"]).rstrip(),
        "",
        "## Interpretation",
        "",
        (
            "This run supports a context-loading token reduction for this corpus and setup. "
            "It does not imply that a complete implementation run will use fewer tokens."
            if supported else
            "This run does not establish a reliable context-loading token reduction."
        ),
        "",
        "## Limits",
        "",
        "- Shared system and tool instructions remain in both variants; paired deltas isolate the document contribution under this setup.",
        "- Fixed-output reads measure context loading, not comprehension or implementation quality.",
        "- Implementation quality and full-loop usage must be measured by the separate implementation benchmark.",
        "- Results apply to this corpus, model, and reasoning effort.",
        "",
    ])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="execute paired fixed-output reads")
    run_parser.add_argument("--provider", choices=["codex", "mock"], default="mock")
    run_parser.add_argument("--model")
    run_parser.add_argument(
        "--reasoning-effort",
        choices=["minimal", "low", "medium", "high", "xhigh"],
    )
    run_parser.add_argument("--case", action="append", default=[])
    run_parser.add_argument("--cases-dir", type=Path)
    run_parser.add_argument("--semantic-dir", type=Path)
    run_parser.add_argument("--repetitions", type=int, default=1)
    run_parser.add_argument("--seed", type=int, default=20260901)
    run_parser.add_argument("--timeout-seconds", type=int, default=180)
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--force", action="store_true")

    report_parser = commands.add_parser("report", help="render Markdown from a JSON run")
    report_parser.add_argument("result", type=Path)
    report_parser.add_argument("--output", type=Path)
    report_parser.add_argument("--force", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "run":
            output = run(args)
            print(f"wrote {output}")
            return 0
        rendered = report(core.read_json(args.result))
        if args.output:
            if args.output.exists() and not args.force:
                raise RuntimeError(
                    f"refusing to overwrite report: {args.output}; pass --force to replace it"
                )
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
