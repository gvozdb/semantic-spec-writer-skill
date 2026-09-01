#!/usr/bin/env python3
"""Measure Semantic Spec Writer authoring cost, reuse cost, and break-even."""

from __future__ import annotations

import argparse
import json
import math
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


BENCHMARKS = Path(__file__).resolve().parent
ROOT = BENCHMARKS.parent
SKILL_DIR = ROOT / "skills" / "semantic-spec-writer"
SKILL = SKILL_DIR / "SKILL.md"
sys.path.insert(0, str(BENCHMARKS))
import benchmark as core  # noqa: E402


def generation_prompt(
    case: core.BenchmarkCase,
    token_encoding: str | None,
    previous_error: str | None = None,
) -> str:
    prompt = (
        "Read skill/SKILL.md and convert source.md into a compact, self-contained "
        "implementation spec. Write only the artifact to result.spec.ctx. "
        f"The implementation target is {case.manifest['entrypoint']}. Preserve every "
        "requirement and every acceptance ID from the source. Do not implement the "
        "task. Apply the SKILL.md quality gate before finishing. Do not access files "
        "outside this workspace or use network access.\n"
    )
    if token_encoding:
        prompt += (
            "For the bounded conversion check required by the skill, pass "
            f"--encoding {token_encoding}.\n"
        )
    if previous_error:
        prompt += (
            "A previous independent attempt failed deterministic validation. "
            "Correct that failure without dropping requirements:\n"
            f"{previous_error[-1200:]}\n"
        )
    return prompt


def mock_provider() -> dict[str, Any]:
    return {
        "return_code": 0,
        "duration_seconds": 0.0,
        "usage": {},
        "tool_calls": {"file_change": 1},
        "tool_call_total": 1,
        "thread_id": None,
        "final_message": "mock provider copied the curated semantic spec",
        "event_errors": [],
        "stderr_tail": "",
    }


def generation_document(args: argparse.Namespace, cases: list[core.BenchmarkCase]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "semantic-spec-generation",
        "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "created_at": datetime.now(UTC).isoformat(),
        "provider": args.provider,
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "token_encoding": args.token_encoding,
        "max_attempts": args.max_attempts,
        "cases": [case.id for case in cases],
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "codex": core.command_version(["codex", "--version"]),
            "git_commit": core.git_commit(),
        },
        "skill_sha256": core.tree_sha256(SKILL_DIR),
        "results": [],
    }


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


def aggregate_attempt_providers(
    attempts: list[dict[str, Any]], selected_attempt: int | None
) -> dict[str, Any]:
    providers = [attempt["provider"] for attempt in attempts]
    selected = (
        providers[selected_attempt - 1]
        if selected_attempt is not None
        else providers[-1]
    )
    usage_keys = {
        key
        for provider in providers
        for key in provider.get("usage", {})
    }
    tool_keys = {
        key
        for provider in providers
        for key in provider.get("tool_calls", {})
    }
    return {
        "return_code": selected.get("return_code"),
        "duration_seconds": round(sum(
            provider.get("duration_seconds") or 0 for provider in providers
        ), 3),
        "usage": {
            key: sum(provider.get("usage", {}).get(key, 0) for provider in providers)
            for key in sorted(usage_keys)
        },
        "tool_calls": {
            key: sum(provider.get("tool_calls", {}).get(key, 0) for provider in providers)
            for key in sorted(tool_keys)
        },
        "tool_call_total": sum(
            provider.get("tool_call_total") or 0 for provider in providers
        ),
        "thread_id": selected.get("thread_id"),
        "final_message": selected.get("final_message", ""),
        "event_errors": [
            error
            for provider in providers
            for error in provider.get("event_errors", [])
        ],
        "stderr_tail": "\n".join(
            provider.get("stderr_tail", "") for provider in providers
            if provider.get("stderr_tail")
        )[-2000:],
        "attempt_count": len(attempts),
    }


def generate(args: argparse.Namespace) -> Path:
    if args.max_attempts < 1:
        raise ValueError("max attempts must be at least 1")
    cases = core.discover_cases(args.case)
    fixture_errors = core.validate(cases)
    if fixture_errors:
        raise RuntimeError("benchmark validation failed:\n" + "\n".join(fixture_errors))
    output = args.output.resolve()
    if output.exists():
        raise RuntimeError(f"refusing to overwrite generation directory: {output}")
    specs = output / "specs"
    specs.mkdir(parents=True)
    attempt_artifacts = output / "attempts"
    attempt_artifacts.mkdir()
    result_path = output / "generation.json"
    document = generation_document(args, cases)

    with tempfile.TemporaryDirectory(prefix="semantic-spec-generate-") as temporary:
        temporary_root = Path(temporary)
        for index, case in enumerate(cases, start=1):
            destination = specs / f"{case.id}.spec.ctx"
            source = case.spec_path("baseline").read_text(encoding="utf-8")
            attempts: list[dict[str, Any]] = []
            selected_attempt = None
            selected_semantic = ""
            previous_error = None
            for attempt_number in range(1, args.max_attempts + 1):
                print(
                    f"[{index}/{len(cases)}] generate {case.id} "
                    f"attempt={attempt_number}/{args.max_attempts}",
                    flush=True,
                )
                workspace = temporary_root / f"{case.id}-attempt-{attempt_number}"
                workspace.mkdir()
                shutil.copytree(SKILL_DIR, workspace / "skill")
                shutil.copy2(case.spec_path("baseline"), workspace / "source.md")
                subprocess.run(
                    ["git", "init", "--quiet"],
                    cwd=workspace,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                artifact_path = workspace / "result.spec.ctx"
                prompt = generation_prompt(
                    case, args.token_encoding, previous_error
                )
                provider = failed_provider("provider did not start")
                semantic = ""
                error = None
                conversion_check = None
                try:
                    if args.provider == "codex":
                        provider = core.run_codex(
                            workspace,
                            prompt,
                            args.model,
                            args.reasoning_effort,
                            args.timeout_seconds,
                        )
                    else:
                        shutil.copy2(case.spec_path("semantic"), artifact_path)
                        provider = mock_provider()
                    if provider.get("return_code") != 0:
                        raise RuntimeError(
                            f"provider exited with {provider.get('return_code')}: "
                            f"{provider.get('stderr_tail', '')}"
                        )
                    if not artifact_path.is_file():
                        raise RuntimeError("provider did not create result.spec.ctx")
                    semantic = artifact_path.read_text(encoding="utf-8")
                    validation_errors = core.validate_semantic_text(case, semantic)
                    check_command = [
                        sys.executable,
                        str(SKILL_DIR / "scripts" / "check_conversion.py"),
                        str(workspace / "source.md"),
                        str(artifact_path),
                    ]
                    if args.token_encoding:
                        check_command.extend(["--encoding", args.token_encoding])
                    check_result = subprocess.run(
                        check_command,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if check_result.stdout.strip():
                        conversion_check = json.loads(check_result.stdout)
                    if check_result.returncode != 0:
                        detail = check_result.stdout.strip() or check_result.stderr.strip()
                        validation_errors.append(
                            f"{case.id}: conversion check failed: {detail}"
                        )
                    if validation_errors:
                        raise RuntimeError("; ".join(validation_errors))
                except subprocess.TimeoutExpired:
                    provider = failed_provider("provider timeout", args.timeout_seconds)
                    semantic = (
                        artifact_path.read_text(encoding="utf-8")
                        if artifact_path.is_file()
                        else ""
                    )
                    error = "provider timeout"
                except Exception as exc:  # noqa: BLE001 - checkpoint failed attempt
                    semantic = (
                        artifact_path.read_text(encoding="utf-8")
                        if artifact_path.is_file()
                        else ""
                    )
                    error = f"{type(exc).__name__}: {exc}"

                attempt_artifact = None
                if semantic:
                    attempt_artifact = (
                        f"attempts/{case.id}-a{attempt_number}.spec.ctx"
                    )
                    (output / attempt_artifact).write_text(semantic, encoding="utf-8")
                attempts.append({
                    "attempt": attempt_number,
                    "artifact": attempt_artifact,
                    "semantic": core.text_metrics(semantic) if semantic else None,
                    "conversion_check": conversion_check,
                    "provenance": {
                        "spec_sha256": (
                            core.sha256_bytes(semantic.encode("utf-8"))
                            if semantic else None
                        ),
                        "prompt_sha256": core.sha256_bytes(prompt.encode("utf-8")),
                    },
                    "provider": provider,
                    "error": error,
                })
                if error is None:
                    selected_attempt = attempt_number
                    selected_semantic = semantic
                    destination.write_text(semantic, encoding="utf-8")
                    break
                previous_error = error

            last_attempt = attempts[-1]
            semantic = selected_semantic or (
                (output / last_attempt["artifact"]).read_text(encoding="utf-8")
                if last_attempt["artifact"] else ""
            )
            selected_record = (
                attempts[selected_attempt - 1]
                if selected_attempt is not None
                else last_attempt
            )

            result = {
                "case": case.id,
                "artifact": f"specs/{case.id}.spec.ctx" if destination.is_file() else None,
                "selected_attempt": selected_attempt,
                "attempts": attempts,
                "source": core.text_metrics(source),
                "semantic": core.text_metrics(semantic) if semantic else None,
                "compression": {
                    "bytes_percent": core.compression_percent(
                        len(source.encode("utf-8")), len(semantic.encode("utf-8"))
                    ) if semantic else None,
                    "words_percent": core.compression_percent(
                        len(source.split()), len(semantic.split())
                    ) if semantic else None,
                },
                "provenance": {
                    "source_sha256": core.sha256_bytes(source.encode("utf-8")),
                    "spec_sha256": core.sha256_bytes(semantic.encode("utf-8")) if semantic else None,
                    "prompt_sha256": selected_record["provenance"]["prompt_sha256"],
                },
                "provider": aggregate_attempt_providers(attempts, selected_attempt),
                "error": None if selected_attempt is not None else last_attempt["error"],
            }
            document["results"].append(result)
            core.write_json_atomic(result_path, document)
    return output


def total_generation_metric(document: dict[str, Any], field: str) -> float | None:
    values = [
        result["provider"].get("usage", {}).get(field)
        for result in document["results"]
    ]
    if not values or any(value is None for value in values):
        return None
    return float(sum(values))


def break_even_reuses(
    authoring: float | None,
    baseline_per_reuse: float | None,
    semantic_per_reuse: float | None,
) -> int | None:
    if authoring is None or baseline_per_reuse is None or semantic_per_reuse is None:
        return None
    saving = baseline_per_reuse - semantic_per_reuse
    if saving <= 0:
        return None
    return max(1, math.ceil(authoring / saving))


def fmt(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def render_report(generation: dict[str, Any], implementation: dict[str, Any]) -> str:
    if set(generation["cases"]) != set(implementation["cases"]):
        raise ValueError("generation and implementation case sets differ")
    if implementation.get("semantic_source") != "generated":
        raise ValueError("implementation run did not use generated semantic specs")
    generated_hashes = {
        result["case"]: result["provenance"].get("spec_sha256")
        for result in generation["results"]
    }
    for result in implementation["results"]:
        if result["variant"] != "semantic":
            continue
        if result["provenance"].get("spec_sha256") != generated_hashes.get(result["case"]):
            raise ValueError(f"generated spec provenance mismatch for {result['case']}")
    repetitions = int(implementation["repetitions"])
    baseline = core.aggregate_variant(implementation["results"], "baseline")
    semantic = core.aggregate_variant(implementation["results"], "semantic")
    metrics = [
        ("Input tokens", "input_tokens", "total_input_tokens"),
        ("Uncached input tokens", "uncached_input_tokens", "total_uncached_input_tokens"),
        ("Output tokens", "output_tokens", "total_output_tokens"),
    ]
    valid_generated = sum(result.get("error") is None for result in generation["results"])
    full_corpus = set(generation["cases"]) == {case.id for case in core.discover_cases()}
    complete_pairs = len(implementation["results"]) == (
        len(implementation["cases"]) * repetitions * len(core.VARIANTS)
    )
    quality_preserved = bool(
        semantic["task_success_rate"] >= baseline["task_success_rate"]
        and semantic["acceptance_pass_rate"] >= baseline["acceptance_pass_rate"]
        and semantic["test_pass_rate"] >= baseline["test_pass_rate"]
    )
    credible = bool(
        generation["provider"] != "mock"
        and generation.get("model")
        and generation.get("reasoning_effort")
        and valid_generated == len(generation["results"])
        and implementation["provider"] != "mock"
        and implementation.get("model")
        and implementation.get("reasoning_effort")
        and repetitions >= 3
        and full_corpus
        and complete_pairs
        and all(result.get("error") is None for result in implementation["results"])
        and all(
            result["provider"].get("return_code") == 0
            for result in implementation["results"]
        )
        and all(
            "input_tokens" in result["provider"].get("usage", {})
            for result in implementation["results"]
        )
    )
    lines = [
        "# Semantic Spec Writer Lifecycle Benchmark",
        "",
        f"Generation run: `{generation['run_id']}`  ",
        f"Implementation run: `{implementation['run_id']}`  ",
        f"Generation model: `{generation.get('model') or 'provider default'}`  ",
        f"Implementation model: `{implementation.get('model') or 'provider default'}`  ",
        f"Cases: {len(generation['cases'])}  ",
        f"Implementation repetitions: {repetitions}",
        "",
    ]
    if not credible:
        lines.extend([
            "> This is a directional smoke run, not publishable lifecycle evidence. "
            "A publishable run requires the complete corpus, named real models and "
            "reasoning efforts, valid generation, three or more implementation "
            "repetitions, and no run errors.",
            "",
        ])
    lines.extend([
        "## Quality",
        "",
        f"Generated valid specs: **{valid_generated}/{len(generation['results'])}**",
        "",
        "| Variant | Task success | Acceptance pass rate |",
        "|---|---:|---:|",
        f"| Markdown | {baseline['task_success_rate'] * 100:.2f}% | {baseline['acceptance_pass_rate'] * 100:.2f}% |",
        f"| Generated semantic | {semantic['task_success_rate'] * 100:.2f}% | {semantic['acceptance_pass_rate'] * 100:.2f}% |",
        "",
        "## Token lifecycle",
        "",
        "Per-reuse values cover one implementation of every case. Authoring creates one semantic spec per case.",
        "",
        "| Metric | Authoring | Markdown per reuse | Semantic per reuse | Break-even reuse |",
        "|---|---:|---:|---:|---:|",
    ])
    calculated: list[tuple[str, float | None, float | None, float | None, int | None]] = []
    for label, usage_key, aggregate_key in metrics:
        authoring = total_generation_metric(generation, usage_key)
        baseline_reuse = baseline[aggregate_key] / repetitions
        semantic_reuse = semantic[aggregate_key] / repetitions
        break_even = (
            break_even_reuses(authoring, baseline_reuse, semantic_reuse)
            if quality_preserved
            else None
        )
        calculated.append((label, authoring, baseline_reuse, semantic_reuse, break_even))
        break_even_text = (
            "not applicable"
            if not quality_preserved
            else str(break_even) if break_even is not None else "none"
        )
        lines.append(
            f"| {label} | {fmt(authoring)} | {fmt(baseline_reuse)} | "
            f"{fmt(semantic_reuse)} | {break_even_text} |"
        )

    if not quality_preserved:
        interpretation = (
            "The generated semantic variant regressed measured implementation quality. "
            "Its token deltas are not product benefits, and break-even is not applicable."
        )
    elif not credible:
        interpretation = (
            "This smoke run is useful for finding regressions, but it does not establish "
            "a reusable token or break-even claim."
        )
    else:
        interpretation = (
            "Measured quality was preserved. Break-even exists only for metrics where "
            "per-reuse savings repay the complete recorded authoring cost."
        )

    lines.extend([
        "",
        "## Reuse scenarios",
        "",
        "Positive semantic delta means generated semantic specs used more tokens, including authoring.",
        "",
        "| Reuses | Metric | Markdown lifecycle | Semantic lifecycle | Semantic delta |",
        "|---:|---|---:|---:|---:|",
    ])
    for reuses in (1, 5, 10, 25):
        for label, authoring, baseline_reuse, semantic_reuse, _ in calculated:
            if authoring is None or baseline_reuse is None or semantic_reuse is None:
                lines.append(f"| {reuses} | {label} | n/a | n/a | n/a |")
                continue
            baseline_total = baseline_reuse * reuses
            semantic_total = authoring + semantic_reuse * reuses
            delta = core.metric_delta_percent(baseline_total, semantic_total)
            lines.append(
                f"| {reuses} | {label} | {fmt(baseline_total)} | "
                f"{fmt(semantic_total)} | {core.format_delta(delta)} |"
            )

    static = implementation["static"]
    lines.extend([
        "",
        "## Generated document size",
        "",
        core.render_static_markdown(static).rstrip(),
        "",
        "## Interpretation",
        "",
        "This report separates the one-time cost of creating semantic specs from the cost "
        "of implementing them repeatedly. " + interpretation,
        "",
        "## Limitations",
        "",
        "- One generated artifact per case is reused across implementation repetitions.",
        "- Provider token totals include the complete agent loop and may vary between runs.",
        "- Input, cached input, and output have different prices; token counts are not a cost estimate.",
        "- The benchmark still needs realistic multi-file fixtures before supporting a broad product claim.",
        "",
    ])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    generate_parser = commands.add_parser("generate", help="generate semantic specs from Markdown fixtures")
    generate_parser.add_argument("--provider", choices=["codex", "mock"], default="mock")
    generate_parser.add_argument("--model")
    generate_parser.add_argument(
        "--reasoning-effort",
        choices=["minimal", "low", "medium", "high", "xhigh"],
    )
    generate_parser.add_argument("--case", action="append", default=[])
    generate_parser.add_argument("--timeout-seconds", type=int, default=600)
    generate_parser.add_argument(
        "--token-encoding",
        help="optional tiktoken encoding enforced by the conversion check",
    )
    generate_parser.add_argument(
        "--max-attempts",
        type=int,
        default=1,
        help="maximum independent generation attempts per case",
    )
    generate_parser.add_argument("--output", type=Path, required=True)

    report_parser = commands.add_parser("report", help="combine generation and implementation results")
    report_parser.add_argument("generation", type=Path)
    report_parser.add_argument("implementation", type=Path)
    report_parser.add_argument("--output", type=Path)
    report_parser.add_argument("--force", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "generate":
            output = generate(args)
            print(f"wrote {output}")
            return 0
        if args.command == "report":
            report = render_report(core.read_json(args.generation), core.read_json(args.implementation))
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
