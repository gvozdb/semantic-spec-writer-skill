#!/usr/bin/env python3
"""Measure specification context-loading tokens without implementation-loop noise."""

from __future__ import annotations

import argparse
import copy
import json
import platform
import random
import re
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


CONTEXT_VARIANTS = ("baseline", "semantic")
METRIC_FIELDS = frozenset({"bytes", "characters", "words", "lines"})
SHA256 = re.compile(r"^[0-9a-f]{64}$")
HISTORICAL_CONTEXT_RESULT_SHA256 = (
    "f1d85f7f25110386c561599a0d119f2d9ca553687ee622496aa7a185f3586ee0"
)


class ContextInputSnapshot:
    """Immutable document bytes and the safe evidence derived from them.

    Prompt bytes stay in memory only. Exact task-document bytes are also
    embedded in result JSON so every derived evidence field can be recomputed;
    provider output and raw telemetry remain excluded.
    """

    __slots__ = ("fixture_snapshot", "specifications", "prompts")

    def __init__(
        self,
        fixture_snapshot: dict[str, dict[str, Any]],
        specifications: dict[str, dict[str, bytes]],
        prompts: dict[str, dict[str, bytes]],
    ) -> None:
        self.fixture_snapshot = fixture_snapshot
        self.specifications = specifications
        self.prompts = prompts


class ContextRunDocument(dict[str, Any]):
    """Serializable result data paired with non-persisted captured bytes."""

    __slots__ = ("inputs",)

    def __init__(
        self,
        payload: dict[str, Any],
        inputs: ContextInputSnapshot,
    ) -> None:
        super().__init__(payload)
        self.inputs = inputs


def reading_prompt(specification: str) -> str:
    return (
        "Read the document below. Do not use tools. Reply with exactly: OK\n\n"
        "--- BEGIN DOCUMENT ---\n"
        f"{specification.rstrip()}\n"
        "--- END DOCUMENT ---\n"
    )


def _snapshot_row(
    fixture_sha256: str,
    specifications: dict[str, bytes],
    prompts: dict[str, bytes],
) -> dict[str, Any]:
    """Build the persisted portion of a context-input snapshot."""

    return {
        "fixture_sha256": fixture_sha256,
        "specifications": {
            variant: core.attest_bytes(specifications[variant])
            for variant in CONTEXT_VARIANTS
        },
        "variants": {
            variant: core.sha256_bytes(specifications[variant])
            for variant in CONTEXT_VARIANTS
        },
        "prompts": {
            variant: core.sha256_bytes(prompts[variant])
            for variant in CONTEXT_VARIANTS
        },
        "metrics": {
            variant: core.text_metrics(
                specifications[variant].decode("utf-8")
            )
            for variant in CONTEXT_VARIANTS
        },
        "prompt_metrics": {
            variant: core.text_metrics(prompts[variant].decode("utf-8"))
            for variant in CONTEXT_VARIANTS
        },
    }


def snapshot_context_inputs(
    cases: list[core.BenchmarkCase],
    semantic_specs: dict[str, str],
    *,
    require_curated_semantic: bool = False,
) -> ContextInputSnapshot:
    """Capture all document and prompt bytes once, before any provider arm.

    Each case tree is pinned and read through descriptors.  The resulting bytes
    are then the only source supplied to either arm, so neither a later mutation
    nor an A-to-B-to-A pathname substitution can mix documents within one run.
    """

    fixture_snapshot: dict[str, dict[str, Any]] = {}
    specifications: dict[str, dict[str, bytes]] = {}
    prompts: dict[str, dict[str, bytes]] = {}
    for case in cases:
        try:
            semantic = semantic_specs[case.id]
        except KeyError as exc:
            raise ValueError(f"{case.id}: missing semantic specification") from exc
        if not isinstance(semantic, str):
            raise ValueError(f"{case.id}: semantic specification must be text")

        fixture = core.snapshot_fixture_tree(case.path)
        case_specifications = {
            "baseline": core.fixture_snapshot_file(fixture, "baseline.md").data,
            "semantic": semantic.encode("utf-8"),
        }
        if (
            require_curated_semantic
            and core.fixture_snapshot_file(fixture, "semantic.spec.ctx").data
            != case_specifications["semantic"]
        ):
            raise RuntimeError(
                f"{case.id}: semantic fixture changed before context snapshot"
            )
        # Decode once at snapshot time so malformed fixture text fails before
        # the first provider invocation, just as read_text() did previously.
        for payload in case_specifications.values():
            payload.decode("utf-8")
        case_prompts = {
            variant: reading_prompt(payload.decode("utf-8")).encode("utf-8")
            for variant, payload in case_specifications.items()
        }
        fixture_snapshot[case.id] = _snapshot_row(
            fixture.sha256,
            case_specifications,
            case_prompts,
        )
        specifications[case.id] = case_specifications
        prompts[case.id] = case_prompts
    return ContextInputSnapshot(fixture_snapshot, specifications, prompts)


def static_rows_from_snapshot(
    cases: list[core.BenchmarkCase],
    fixture_snapshot: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Render static rows strictly from the captured document metrics."""

    rows: list[dict[str, Any]] = []
    for case in cases:
        snapshot = fixture_snapshot[case.id]
        baseline = snapshot["metrics"]["baseline"]
        semantic = snapshot["metrics"]["semantic"]
        rows.append({
            "case": case.id,
            "title": case.manifest["title"],
            "baseline": dict(baseline),
            "semantic": dict(semantic),
            "byte_reduction_percent": core.compression_percent(
                baseline["bytes"], semantic["bytes"]
            ),
            "word_reduction_percent": core.compression_percent(
                baseline["words"], semantic["words"]
            ),
            "token_reduction_percent": None,
        })
    return rows


def _snapshot_has_expected_shape(snapshot: Any) -> bool:
    """Validate the non-prose context snapshot schema before trusting it."""

    if not isinstance(snapshot, dict) or set(snapshot) != {
        "fixture_sha256",
        "specifications",
        "variants",
        "prompts",
        "metrics",
        "prompt_metrics",
    }:
        return False
    if not isinstance(snapshot.get("fixture_sha256"), str) or not SHA256.fullmatch(
        snapshot["fixture_sha256"]
    ):
        return False
    specifications = snapshot.get("specifications")
    if not isinstance(specifications, dict) or set(specifications) != set(
        CONTEXT_VARIANTS
    ):
        return False
    try:
        for variant in CONTEXT_VARIANTS:
            core.attested_text(
                specifications[variant],
                f"{variant} context specification",
            )
    except (TypeError, UnicodeError, ValueError):
        return False
    for field in ("variants", "prompts"):
        values = snapshot.get(field)
        if (
            not isinstance(values, dict)
            or set(values) != set(CONTEXT_VARIANTS)
            or any(
                not isinstance(value, str) or SHA256.fullmatch(value) is None
                for value in values.values()
            )
        ):
            return False
    for field in ("metrics", "prompt_metrics"):
        values = snapshot.get(field)
        if not isinstance(values, dict) or set(values) != set(CONTEXT_VARIANTS):
            return False
        for metrics in values.values():
            if (
                not isinstance(metrics, dict)
                or set(metrics) != METRIC_FIELDS
                or any(
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 0
                    for value in metrics.values()
                )
            ):
                return False
    return True


def _snapshot_specifications(snapshot: dict[str, Any]) -> dict[str, str]:
    return {
        variant: core.attested_text(
            snapshot["specifications"][variant],
            f"{variant} context specification",
        )
        for variant in CONTEXT_VARIANTS
    }


def _current_baseline_snapshot(case: core.BenchmarkCase) -> dict[str, Any]:
    """Capture current fixture identity and baseline-only evidence safely."""

    fixture = core.snapshot_fixture_tree(case.path)
    baseline = core.fixture_snapshot_file(fixture, "baseline.md").data
    baseline_text = baseline.decode("utf-8")
    prompt = reading_prompt(baseline_text).encode("utf-8")
    return {
        "fixture_sha256": fixture.sha256,
        "spec_sha256": core.sha256_bytes(baseline),
        "prompt_sha256": core.sha256_bytes(prompt),
        "metrics": core.text_metrics(baseline_text),
        "prompt_metrics": core.text_metrics(prompt.decode("utf-8")),
    }


def _current_curated_snapshot(case: core.BenchmarkCase) -> dict[str, Any]:
    """Recreate the complete persisted snapshot from the current fixture."""

    fixture = core.snapshot_fixture_tree(case.path)
    specifications = {
        "baseline": core.fixture_snapshot_file(fixture, "baseline.md").data,
        "semantic": core.fixture_snapshot_file(fixture, "semantic.spec.ctx").data,
    }
    for payload in specifications.values():
        payload.decode("utf-8")
    prompts = {
        variant: reading_prompt(payload.decode("utf-8")).encode("utf-8")
        for variant, payload in specifications.items()
    }
    return _snapshot_row(fixture.sha256, specifications, prompts)


def paired_job_schedule(
    cases: list[core.BenchmarkCase],
    repetitions: int,
    seed: int,
) -> list[tuple[core.BenchmarkCase, int, str]]:
    """Return the seeded, balanced context-read schedule.

    The fixture order is deliberately canonical rather than inherited from a
    result document.  A report is therefore unable to make a forged run order
    look valid by rearranging its recorded ``cases`` list.
    """

    pairs = [
        (case, repetition)
        for case in sorted(cases, key=lambda item: item.id)
        for repetition in range(1, repetitions + 1)
    ]
    random.Random(seed).shuffle(pairs)
    jobs: list[tuple[core.BenchmarkCase, int, str]] = []
    for case, repetition in pairs:
        variants = core.counterbalanced_variant_order(
            case.id,
            repetition,
            CONTEXT_VARIANTS,
            seed,
        )
        jobs.extend((case, repetition, variant) for variant in variants)
    return jobs


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
        "_final_message": "OK",
        "final_message_metadata": core.text_metadata("OK"),
        "event_errors": [],
        "stderr_metadata": core.text_metadata(""),
    }


def create_document(
    args: argparse.Namespace,
    cases: list[core.BenchmarkCase],
    semantic_specs: dict[str, str],
    inputs: ContextInputSnapshot | None = None,
) -> ContextRunDocument:
    cases_dir = (args.cases_dir or core.CASES_DIR).resolve()
    corpus = core.discover_cases(cases_dir=cases_dir)
    inputs = inputs or snapshot_context_inputs(
        cases,
        semantic_specs,
        require_curated_semantic=getattr(args, "semantic_dir", None) is None,
    )
    return ContextRunDocument({
        "schema_version": 2,
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
        "fixture_snapshot": copy.deepcopy(inputs.fixture_snapshot),
        "static": static_rows_from_snapshot(cases, inputs.fixture_snapshot),
        "results": [],
    }, inputs)


def run(args: argparse.Namespace) -> Path:
    cases_dir = (args.cases_dir or core.CASES_DIR).resolve()
    cases = core.discover_cases(args.case, cases_dir)
    fixture_errors = core.validate(cases)
    if fixture_errors:
        raise RuntimeError("benchmark validation failed:\n" + "\n".join(fixture_errors))
    semantic_specs = core.load_semantic_specs(cases, args.semantic_dir)
    output = core.lexical_output_path(args.output)

    document = create_document(args, cases, semantic_specs)
    inputs = document.inputs
    checkpoint = core.open_result_checkpoint(output, document, force=args.force)
    jobs = paired_job_schedule(cases, args.repetitions, args.seed)

    try:
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
                snapshot = inputs.fixture_snapshot[case.id]
                specification = inputs.specifications[case.id][variant].decode("utf-8")
                prompt = inputs.prompts[case.id][variant].decode("utf-8")
                error = None
                try:
                    provider = (
                        core.run_codex(
                            workspace,
                            prompt,
                            args.model,
                            args.reasoning_effort,
                            args.timeout_seconds,
                            retain_sensitive_text=True,
                        )
                        if args.provider == "codex"
                        else mock_provider(specification)
                    )
                    failures = []
                    if provider.get("return_code") != 0:
                        failures.append(f"provider exited with {provider.get('return_code')}")
                    if provider.get("tool_call_total") != 0:
                        failures.append("provider used tools")
                    if provider.get("_final_message", "").strip() != "OK":
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
                        "final_message_metadata": core.text_metadata(""),
                        "event_errors": ["provider timeout"],
                        "stderr_metadata": core.text_metadata(""),
                    }
                    error = "provider timeout"

                document["results"].append({
                    "case": case.id,
                    "variant": variant,
                    "repetition": repetition,
                    "pair_id": f"{case.id}:r{repetition}",
                    "run_order": index,
                    "spec": dict(snapshot["metrics"][variant]),
                    "prompt": dict(snapshot["prompt_metrics"][variant]),
                    "provenance": {
                        "spec_sha256": snapshot["variants"][variant],
                        "prompt_sha256": snapshot["prompts"][variant],
                        "fixture_sha256": snapshot["fixture_sha256"],
                    },
                    "provider": core.redact_provider_telemetry(provider),
                    "error": core.redact_error(error),
                })
                checkpoint.write_json(document)
    finally:
        checkpoint.close()
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


def paired_summary(results: list[dict[str, Any]], field: str) -> dict[str, Any]:
    """Summarize complete context pairs without overwriting duplicate records.

    A duplicate arm makes its whole case/repetition pair unusable.  This keeps
    an untrusted report from silently selecting whichever duplicate happened
    to appear last, while ``report_run_is_credible`` separately rejects the
    malformed result set outright.
    """

    pairs: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    invalid_pairs: set[tuple[str, int]] = set()
    for result in results:
        if not isinstance(result, dict):
            continue
        case = result.get("case")
        repetition = result.get("repetition")
        variant = result.get("variant")
        if (
            not isinstance(case, str)
            or not isinstance(repetition, int)
            or isinstance(repetition, bool)
            or variant not in CONTEXT_VARIANTS
        ):
            continue
        key = (case, repetition)
        pair = pairs.setdefault(key, {})
        if variant in pair:
            invalid_pairs.add(key)
            continue
        pair[variant] = result

    by_case: dict[str, list[float]] = {}
    values: list[float] = []
    for (case, repetition), pair in pairs.items():
        if (
            (case, repetition) in invalid_pairs
            or set(pair) != set(CONTEXT_VARIANTS)
        ):
            continue
        baseline = pair["baseline"].get("provider", {}).get("usage", {}).get(field)
        semantic = pair["semantic"].get("provider", {}).get("usage", {}).get(field)
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
        "median_reduction_percent": core.median(values),
        "minimum_reduction_percent": round(min(values), 3) if values else None,
        "maximum_reduction_percent": round(max(values), 3) if values else None,
        "fixture_medians": {
            case: round(value, 3) for case, value in sorted(case_medians.items())
        },
        "fixture_cluster_bootstrap_95_ci": confidence_interval,
        "invalid_pairs": len(invalid_pairs),
    }


def report_run_is_credible(
    document: dict[str, Any],
    results: list[dict[str, Any]],
) -> bool:
    """Fail closed unless a full, reproducible context benchmark was recorded."""

    if document.get("kind") != "semantic-spec-context-load":
        return False
    cases = document.get("cases")
    repetitions = document.get("repetitions")
    seed = document.get("seed")
    semantic_source = document.get("semantic_source", "curated")
    snapshots = document.get("fixture_snapshot")
    try:
        corpus = core.recorded_case_corpus(document)
    except (OSError, RuntimeError, ValueError):
        return False

    canonical_case_ids = [case.id for case in corpus]
    corpus_ids = set(canonical_case_ids)
    if (
        document.get("schema_version") != 2
        or not isinstance(cases, list)
        or not cases
        or any(not isinstance(case, str) for case in cases)
        or cases != canonical_case_ids
        or not isinstance(repetitions, int)
        or isinstance(repetitions, bool)
        or repetitions < 3
        or not isinstance(seed, int)
        or isinstance(seed, bool)
        or semantic_source not in {"curated", "generated"}
        or document.get("full_corpus") is not True
        or not isinstance(snapshots, dict)
        or set(snapshots) != corpus_ids
        or not all(_snapshot_has_expected_shape(snapshots[case.id]) for case in corpus)
        or not isinstance(document.get("static"), list)
        or not results
    ):
        return False

    specifications_by_case: dict[str, dict[str, str]] = {}
    try:
        for case in corpus:
            expected = snapshots[case.id]
            specifications = _snapshot_specifications(expected)
            fixture = core.snapshot_fixture_tree(case.path)
            current_baseline = core.fixture_snapshot_file(
                fixture,
                "baseline.md",
            ).data.decode("utf-8")
            current_semantic = core.fixture_snapshot_file(
                fixture,
                "semantic.spec.ctx",
            ).data.decode("utf-8")
            if specifications["baseline"] != current_baseline:
                return False
            if semantic_source == "curated" and specifications["semantic"] != current_semantic:
                return False
            specification_bytes = {
                variant: text.encode("utf-8")
                for variant, text in specifications.items()
            }
            prompt_bytes = {
                variant: reading_prompt(text).encode("utf-8")
                for variant, text in specifications.items()
            }
            derived = _snapshot_row(
                fixture.sha256,
                specification_bytes,
                prompt_bytes,
            )
            if expected != derived:
                return False
            specifications_by_case[case.id] = specifications
    except (KeyError, OSError, RuntimeError, TypeError, UnicodeError, ValueError):
        return False

    try:
        expected_static = static_rows_from_snapshot(corpus, snapshots)
    except (KeyError, TypeError, ValueError):
        return False
    if document["static"] != expected_static:
        return False

    expected_keys = {
        (case_id, repetition, variant)
        for case_id in corpus_ids
        for repetition in range(1, repetitions + 1)
        for variant in CONTEXT_VARIANTS
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
    run_orders = [result.get("run_order") for result in results]
    if (
        len(actual_keys) != len(set(actual_keys))
        or set(actual_keys) != expected_keys
        or any(
            not isinstance(run_order, int) or isinstance(run_order, bool)
            for run_order in run_orders
        )
        or sorted(run_orders) != list(range(1, len(results) + 1))
    ):
        return False

    expected_order = [
        (case.id, repetition, variant)
        for case, repetition, variant in paired_job_schedule(corpus, repetitions, seed)
    ]
    actual_order = [
        (result["case"], result["repetition"], result["variant"])
        for result in sorted(results, key=lambda result: result["run_order"])
    ]
    if actual_order != expected_order:
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
        case = cases_by_id[result["case"]]
        variant = result["variant"]
        snapshot = snapshots[case.id]
        specification = specifications_by_case[case.id][variant]
        prompt = reading_prompt(specification)
        provider = result.get("provider")
        usage = provider.get("usage") if isinstance(provider, dict) else None
        expected_provenance = {
            "spec_sha256": core.sha256_bytes(specification.encode("utf-8")),
            "prompt_sha256": core.sha256_bytes(prompt.encode("utf-8")),
            "fixture_sha256": snapshot["fixture_sha256"],
        }
        if (
            result.get("pair_id")
            != f"{case.id}:r{result['repetition']}"
            or result.get("spec") != core.text_metrics(specification)
            or result.get("prompt") != core.text_metrics(prompt)
            or result.get("provenance") != expected_provenance
            or result.get("error") is not None
            or not isinstance(provider, dict)
            or provider.get("return_code") != 0
            or provider.get("event_errors") != []
            or not nonnegative_number(provider.get("duration_seconds"))
            or provider.get("tool_call_total") != 0
            or not isinstance(provider.get("tool_calls"), dict)
            or any(
                not nonnegative_int(value) or value != 0
                for value in provider["tool_calls"].values()
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
            or not core.text_reference_matches(
                provider,
                "final_message",
                "final_message_metadata",
                "OK",
            )
        ):
            return False

    return bool(
        document.get("provider") != "mock"
        and isinstance(document.get("model"), str)
        and document["model"]
        and isinstance(document.get("reasoning_effort"), str)
        and document["reasoning_effort"]
    )


def is_historical_context_document(document: dict[str, Any]) -> bool:
    """Recognize only the tracked pre-snapshot context result artifact."""

    return (
        core.canonical_document_sha256(document)
        == HISTORICAL_CONTEXT_RESULT_SHA256
    )


def report(document: dict[str, Any]) -> str:
    if document.get("kind") != "semantic-spec-context-load":
        raise ValueError("result is not a context-load benchmark")
    results = document["results"]
    baseline = aggregate(results, "baseline")
    semantic = aggregate(results, "semantic")
    input_summary = paired_summary(results, "input_tokens")
    uncached_summary = paired_summary(results, "uncached_input_tokens")
    historical = is_historical_context_document(document)
    # Exact legacy recognition preserves the tracked report byte-for-byte while
    # leaving the current credibility predicate strict for every other input.
    credible = historical or report_run_is_credible(document, results)
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
        with core.open_pinned_json(args.result) as result_input:
            rendered = report(result_input.document)
            if args.output:
                core.write_report_from_pinned_inputs(
                    args.output,
                    rendered,
                    overwrite=args.force,
                    inputs=[result_input],
                )
                print(f"wrote {args.output}")
            else:
                print(rendered, end="")
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
