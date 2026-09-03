#!/usr/bin/env python3
"""Benchmark repository execution from Markdown, semantic packets, and Capsules."""

from __future__ import annotations

import sys


if __name__ == "__main__" and sys.argv[1:2] == ["validate-release"]:
    print(
        "validate-release requires the attested Git-blob bootstrap documented "
        "in benchmarks/README.md",
        file=sys.stderr,
    )
    raise SystemExit(2)

import argparse
import importlib.util
import json
import math
import random
import re
import statistics
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple


BENCHMARKS = Path(__file__).resolve().parent
ROOT = BENCHMARKS.parent
CASES_DIR = BENCHMARKS / "handoff-cases"
SKILL_DIR = ROOT / "skills" / "semantic-spec-writer"
LIFECYCLE_PROTOCOL_PATH = BENCHMARKS / "capsule-lifecycle-v1.prereg.json"
VARIANTS = {
    "markdown": "baseline.md",
    "semantic": "semantic.spec.ctx",
    "packet": "packet.spec.ctx",
}
CAPSULE_SCRIPT = SKILL_DIR / "scripts" / "context_capsule.py"
CAPSULE_CODE_PATHS = (
    "benchmarks/benchmark.py",
    "benchmarks/capsule-lifecycle-v1.prereg.json",
    "benchmarks/grader.py",
    "benchmarks/handoff.py",
    "benchmarks/lifecycle.py",
    "benchmarks/solution_runtime.py",
    "benchmarks/solution_worker.py",
    "benchmarks/validate_capsule_release.py",
    "skills/semantic-spec-writer/SKILL.md",
    "skills/semantic-spec-writer/references/context-capsules.md",
    "skills/semantic-spec-writer/references/execution-packets.md",
    "skills/semantic-spec-writer/scripts/check_conversion.py",
    "skills/semantic-spec-writer/scripts/check_execution_packet.py",
    "skills/semantic-spec-writer/scripts/context_capsule.py",
)
HISTORICAL_PACKET_RESULT_SHA256 = (
    "259812825fcb6e8d25903b2086f33c78aca3e545ac0838002207374fae5fd821"
)
class ComparisonConfig(NamedTuple):
    """Immutable definition of one reportable benchmark comparison."""

    name: str
    kind: str
    versions: tuple[tuple[str, int], ...]
    variants: tuple[str, ...]
    primary_baseline: str
    primary_candidate: str
    labels: tuple[tuple[str, str], ...]


class HandoffRunDocument(dict[str, Any]):
    """Serializable evidence paired with immutable in-memory starter snapshots."""

    __slots__ = ("starter_snapshots", "grading_snapshots", "artifacts")

    def __init__(
        self,
        payload: dict[str, Any],
        starter_snapshots: dict[str, core.FixtureTreeSnapshot],
        grading_snapshots: dict[str, core.GradingSnapshot],
        artifacts: dict[str, dict[str, str]],
    ) -> None:
        super().__init__(payload)
        self.starter_snapshots = starter_snapshots
        self.grading_snapshots = grading_snapshots
        self.artifacts = artifacts


PACKET_V3 = ComparisonConfig(
    "packet-v3",
    "semantic-execution-packet-comparison",
    (("packet_version", 3),),
    ("markdown", "semantic", "packet"),
    "semantic",
    "packet",
    (("markdown", "Markdown"), ("semantic", "Semantic v1"), ("packet", "Packet v3")),
)
CAPSULE_V6 = ComparisonConfig(
    "capsule-v6",
    "semantic-context-capsule-comparison",
    (("packet_version", 3), ("capsule_version", 6)),
    ("packet", "capsule"),
    "packet",
    "capsule",
    (("packet", "Packet v3"), ("capsule", "Capsule v6")),
)
MARKDOWN_CAPSULE_V6 = ComparisonConfig(
    "markdown-capsule-v6",
    "markdown-context-capsule-comparison",
    (("packet_version", 3), ("capsule_version", 6)),
    ("markdown", "capsule"),
    "markdown",
    "capsule",
    (("markdown", "Ordinary Markdown"), ("capsule", "Capsule v6")),
)
MARKDOWN_CAPSULE_LIFECYCLE_V1 = ComparisonConfig(
    "markdown-capsule-lifecycle-v1",
    "markdown-context-capsule-lifecycle-comparison",
    (("packet_version", 3), ("capsule_version", 6), ("lifecycle_version", 1)),
    ("markdown", "capsule"),
    "markdown",
    "capsule",
    (("markdown", "Ordinary Markdown"), ("capsule", "Capsule v6")),
)
CAPSULE_COMPARISONS = (
    CAPSULE_V6,
    MARKDOWN_CAPSULE_V6,
    MARKDOWN_CAPSULE_LIFECYCLE_V1,
)
COMPARISONS = {
    config.name: config
    for config in (PACKET_V3, *CAPSULE_COMPARISONS)
}
ALL_VARIANTS = tuple((*VARIANTS, "capsule"))
CAPSULE_ACTION_ERROR_CODES = frozenset({
    "capsule_declared_verification_count",
    "capsule_incomplete_routed_edits",
    "capsule_no_routed_edit",
    "capsule_pre_edit_command",
    "capsule_pre_edit_discovery",
    "capsule_pre_edit_read",
    "capsule_pre_edit_verification",
    "capsule_routed_edit_attestation_failed",
    "capsule_tool_sequence",
})
sys.path.insert(0, str(BENCHMARKS))
import benchmark as core  # noqa: E402
import lifecycle as lifecycle_benchmark  # noqa: E402


LIFECYCLE_CLAIM_PROTOCOL = "markdown-capsule-full-lifecycle-dominance-v1"
LIFECYCLE_RELEASE_GATE = (
    "full corpus and exact paired schedule",
    "one successful authoring attempt per fixture with complete token telemetry",
    "Capsule succeeds with every hidden test and acceptance check in every pair",
    "Capsule quality is componentwise no worse in every pair",
    "Capsule action gate passes in every implementation",
    "Capsule one-use lifecycle total tokens are lower in every pair",
    "fixture-cluster bootstrap confidence interval lower bound is greater than zero",
    "measured three-use Capsule lifecycle total tokens are lower",
    "no provider, verification, provenance, privacy, or recorded run errors",
)


def lifecycle_protocol_is_supported(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "protocol_id",
        "comparison",
        "corpus",
        "provider",
        "model",
        "reasoning_effort",
        "timeout_seconds",
        "implementation_repetitions",
        "seed",
        "authoring_attempts_per_fixture",
        "baseline_authoring",
        "candidate_authoring",
        "primary_metric",
        "primary_population",
        "bootstrap",
        "release_gate",
        "retry_policy",
    }:
        return False
    corpus = value.get("corpus")
    bootstrap = value.get("bootstrap")

    def positive_int(item: Any) -> bool:
        return isinstance(item, int) and not isinstance(item, bool) and item > 0

    return bool(
        type(value.get("schema_version")) is int
        and value["schema_version"] == 1
        and value.get("protocol_id") == LIFECYCLE_CLAIM_PROTOCOL
        and value.get("comparison") == MARKDOWN_CAPSULE_LIFECYCLE_V1.name
        and value.get("provider") == "codex"
        and isinstance(value.get("model"), str)
        and value["model"]
        and isinstance(value.get("reasoning_effort"), str)
        and value["reasoning_effort"]
        and positive_int(value.get("timeout_seconds"))
        and positive_int(value.get("implementation_repetitions"))
        and value["implementation_repetitions"] >= 3
        and positive_int(value.get("seed"))
        and type(value.get("authoring_attempts_per_fixture")) is int
        and value["authoring_attempts_per_fixture"] == 1
        and isinstance(corpus, list)
        and corpus
        and all(isinstance(case, str) and case for case in corpus)
        and len(corpus) == len(set(corpus))
        and value.get("baseline_authoring")
        == "source-markdown-direct-zero-model-calls"
        and value.get("candidate_authoring")
        == "model-authored-packet-v3-then-deterministic-capsule-v6"
        and value.get("primary_metric")
        == (
            "authoring_input_tokens + authoring_output_tokens + "
            "implementation_input_tokens + implementation_output_tokens"
        )
        and value.get("primary_population")
        == "all complete paired attempts including quality failures"
        and isinstance(bootstrap, dict)
        and set(bootstrap) == {"unit", "iterations", "seed", "confidence"}
        and bootstrap.get("unit") == "fixture_median"
        and type(bootstrap.get("iterations")) is int
        and bootstrap["iterations"] == 10_000
        and type(bootstrap.get("seed")) is int
        and bootstrap["seed"] == value["seed"]
        and isinstance(bootstrap.get("confidence"), (int, float))
        and not isinstance(bootstrap["confidence"], bool)
        and bootstrap["confidence"] == 0.95
        and value.get("release_gate") == list(LIFECYCLE_RELEASE_GATE)
        and value.get("retry_policy")
        == "no rerun or threshold change after observing outcomes"
    )


def lifecycle_protocol() -> tuple[dict[str, Any], bytes]:
    payload = core.read_stable_regular_file(
        LIFECYCLE_PROTOCOL_PATH,
        "Capsule lifecycle preregistration",
        max_bytes=64 * 1024,
    )
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Capsule lifecycle preregistration is invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Capsule lifecycle preregistration must be an object")
    if not lifecycle_protocol_is_supported(value):
        raise RuntimeError("Capsule lifecycle preregistration is unsupported")
    return value, payload


def comparison_config(value: ComparisonConfig | str | None = None) -> ComparisonConfig:
    """Resolve a comparison name while treating missing legacy metadata as v3."""

    if value is None:
        return PACKET_V3
    if isinstance(value, ComparisonConfig):
        return value
    if not isinstance(value, str) or value not in COMPARISONS:
        raise ValueError(f"unknown comparison: {value!r}")
    return COMPARISONS[value]


def document_comparison(document: dict[str, Any]) -> ComparisonConfig:
    return comparison_config(document.get("comparison"))


def arm_label(config: ComparisonConfig, variant: str) -> str:
    for name, label in config.labels:
        if name == variant:
            return label
    raise ValueError(f"{config.name}: unknown arm {variant!r}")


def is_capsule_comparison(config: ComparisonConfig) -> bool:
    return config in CAPSULE_COMPARISONS


def capsule_schema_version(config: ComparisonConfig) -> int:
    if config == CAPSULE_V6:
        return 4
    if config == MARKDOWN_CAPSULE_V6:
        return 5
    if config == MARKDOWN_CAPSULE_LIFECYCLE_V1:
        return 6
    raise ValueError(f"{config.name}: not a Capsule comparison")


def context_capsule_module() -> Any:
    """Load Capsule v6 by path so the benchmark has no fixture-side artifact."""

    module_name = "_semantic_spec_context_capsule"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, CAPSULE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load Capsule v6 builder: {CAPSULE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def artifact_path(case: core.BenchmarkCase, variant: str) -> Path:
    return case.path / VARIANTS[variant]


def _capsule_source_hashes(
    capsule_module: Any,
    capsule: bytes,
) -> dict[str, str]:
    """Derive routed-source provenance only from validated sealed frames."""

    _, _, _, sources, _, _ = capsule_module._parse_capsule(capsule)
    hashes: dict[str, str] = {}
    for descriptor, payload in sources:
        capsule_module._validate_source_descriptor(descriptor)
        path = descriptor["path"]
        if path in hashes:
            raise RuntimeError(f"Capsule v6 repeats a routed source frame: {path}")
        hashes[path] = core.sha256_bytes(payload)
    return {path: hashes[path] for path in sorted(hashes)}


def _check_capsule_snapshot(
    case: core.BenchmarkCase,
    starter_snapshot: core.FixtureTreeSnapshot,
    packet_bytes: bytes,
    capsule: bytes,
) -> tuple[dict[str, Any], list[tuple[dict[str, Any], bytes]], str]:
    """Validate a Capsule only against one immutable captured workspace."""

    capsule_module = context_capsule_module()
    try:
        packet_text = packet_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"{case.id}: Packet v3 is not UTF-8") from exc
    with tempfile.TemporaryDirectory(prefix="capsule-snapshot-check-") as directory:
        root = Path(directory)
        repository = root / "repo"
        packet_path = root / "packet.spec.ctx"
        core.materialize_fixture_tree(starter_snapshot, repository)
        packet_path.write_bytes(packet_bytes)
        checked = capsule_module.check_capsule(
            repository,
            capsule,
            packet=packet_path,
        )
    errors = checked.get("errors", []) if isinstance(checked, dict) else []
    if (
        not isinstance(checked, dict)
        or checked.get("valid") is not True
        or checked.get("version") != capsule_module.CAPSULE_VERSION
        or checked.get("packet_bound") is not True
        or not isinstance(checked.get("capsule"), dict)
        or not isinstance(checked["capsule"].get("seal_sha256"), str)
        or checked.get("packet_sha256") != core.sha256_bytes(packet_bytes)
        or not isinstance(checked.get("route_sha256"), str)
    ):
        detail = "; ".join(str(item) for item in errors) or "invalid capsule metadata"
        raise RuntimeError(f"{case.id}: Capsule v6 validation failed: {detail}")
    _, _, _, sources, _, seal = capsule_module._parse_capsule(capsule)

    # Recompute every frame directly from the captured workspace bytes.  This
    # independently binds full-file and ranged routes, descriptor metadata, and
    # route indices.  Create routes intentionally produce no frame; the trusted
    # checker above also proved they are absent in this exact materialization.
    normalized_packet = packet_text.replace("\r\n", "\n").replace("\r", "\n")
    targets = capsule_module.packet_checker.parse_routes(normalized_packet)
    route_actions = capsule_module._route_action_groups(normalized_packet, targets)
    expected_sources: list[tuple[dict[str, Any], bytes]] = []
    for index, target in enumerate(targets):
        if target.kind == "create":
            continue
        captured = core.fixture_snapshot_file(
            starter_snapshot,
            target.relative_path,
        ).data
        payload = capsule_module._selected_source(target, captured)
        expected_sources.append(
            (
                capsule_module._source_descriptor(
                    index,
                    target,
                    payload,
                    actions=route_actions[index],
                ),
                payload,
            )
        )
    if sources != expected_sources:
        raise RuntimeError(
            f"{case.id}: Capsule routed frames do not match captured workspace"
        )
    return checked, sources, seal


def capsule_artifact(
    case: core.BenchmarkCase,
    *,
    starter_snapshot: core.FixtureTreeSnapshot | None = None,
    packet_bytes: bytes | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build one Capsule and workspace from the same descriptor-captured bytes."""

    if starter_snapshot is None or packet_bytes is None:
        fixture = core.snapshot_fixture_tree(case.path)
        starter_snapshot = core.fixture_subtree_snapshot(fixture, "starter")
        packet_bytes = core.fixture_snapshot_file(fixture, VARIANTS["packet"]).data
    capsule_module = context_capsule_module()
    with tempfile.TemporaryDirectory(prefix="capsule-snapshot-build-") as directory:
        root = Path(directory)
        repository = root / "repo"
        packet_path = root / "packet.spec.ctx"
        core.materialize_fixture_tree(starter_snapshot, repository)
        packet_path.write_bytes(packet_bytes)
        capsule = capsule_module.build_capsule(repository, packet_path)
    checked, sources, seal = _check_capsule_snapshot(
        case,
        starter_snapshot,
        packet_bytes,
        capsule,
    )
    try:
        text = capsule.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"{case.id}: Capsule v6 is not UTF-8") from exc
    return text, {
        "capsule_sha256": core.sha256_bytes(capsule),
        "capsule_seal_sha256": seal,
        "packet_sha256": checked["packet_sha256"],
        "route_sha256": checked["route_sha256"],
        "source_hashes": _capsule_source_hashes(capsule_module, capsule),
        "source_count": len(sources),
        "source_bytes": sum(len(payload) for _, payload in sources),
    }


def artifact_text(
    case: core.BenchmarkCase,
    variant: str,
    comparison: ComparisonConfig | str | None = None,
) -> str:
    config = comparison_config(comparison)
    if variant not in config.variants:
        raise ValueError(f"{config.name}: arm {variant!r} is not reportable")
    if variant == "capsule":
        return capsule_artifact(case)[0]
    return artifact_path(case, variant).read_text(encoding="utf-8")


def validate_packet(case: core.BenchmarkCase) -> list[str]:
    path = artifact_path(case, "packet")
    if not path.is_file():
        return [f"{case.id}: missing packet.spec.ctx"]
    text = path.read_text(encoding="utf-8")
    errors = core.validate_semantic_text(case, text)
    errors.extend(core.validate_execution_packet_artifact(case, path))
    return errors


def validate_capsule(case: core.BenchmarkCase) -> list[str]:
    try:
        capsule_artifact(case)
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        return [f"{case.id}: Capsule v6 validation failed: {exc}"]
    return []


def validate_cases(
    cases: list[core.BenchmarkCase],
    comparison: ComparisonConfig | str | None = None,
) -> list[str]:
    config = comparison_config(comparison)
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
        if is_capsule_comparison(config):
            errors.extend(validate_capsule(case))
    return errors


def static_rows(
    cases: list[core.BenchmarkCase],
    encoder: Any | None = None,
    comparison: ComparisonConfig | str | None = None,
) -> list[dict[str, Any]]:
    config = comparison_config(comparison)
    rows: list[dict[str, Any]] = []
    for case in cases:
        variants: dict[str, dict[str, int]] = {}
        for variant in config.variants:
            text = artifact_text(case, variant, config)
            metrics = core.text_metrics(text)
            if encoder is not None:
                metrics["tokens"] = len(encoder.encode(text))
            variants[variant] = metrics
        rows.append({"case": case.id, "variants": variants})
    return rows


def static_rows_from_artifacts(
    cases: list[core.BenchmarkCase],
    artifacts: dict[str, dict[str, str]],
    comparison: ComparisonConfig | str | None = None,
) -> list[dict[str, Any]]:
    """Derive run static evidence from the exact captured arm bytes."""

    config = comparison_config(comparison)
    return [
        {
            "case": case.id,
            "variants": {
                variant: core.text_metrics(artifacts[case.id][variant])
                for variant in config.variants
            },
        }
        for case in cases
    ]


def render_static(
    rows: list[dict[str, Any]],
    comparison: ComparisonConfig | str | None = None,
) -> str:
    config = comparison_config(comparison)
    if config == PACKET_V3:
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

    baseline = config.primary_baseline
    candidate = config.primary_candidate
    include_tokens = bool(rows and "tokens" in rows[0]["variants"][baseline])
    unit = "tokens" if include_tokens else "bytes"
    lines = [
        f"| Case | {arm_label(config, baseline)} {unit} | "
        f"{arm_label(config, candidate)} {unit} | "
        f"{arm_label(config, candidate)} size overhead |",
        "|---|---:|---:|---:|---:|",
    ]
    overheads = []
    for row in rows:
        variants = row["variants"]
        left = variants[baseline][unit]
        right = variants[candidate][unit]
        overhead = (right - left) / left * 100 if left else 0.0
        overheads.append(overhead)
        lines.append(
            f"| `{row['case']}` | {left} | {right} | {overhead:+.2f}% |"
        )
    if overheads:
        lines.append(
            f"| **Median** |  |  |  | **{statistics.median(overheads):+.2f}%** |"
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
        "final_message_metadata": core.text_metadata(""),
        "event_errors": [message],
        "stderr_metadata": core.text_metadata(""),
    }


def execution_prompt(
    specification: str,
    variant: str,
    comparison: ComparisonConfig | str | None = None,
) -> str:
    config = comparison_config(comparison)
    if is_capsule_comparison(config) and variant == config.primary_candidate:
        capsule = context_capsule_module()
        return core.benchmark_prompt(
            specification,
            execution_gate=capsule.CAPSULE_HOST_CONTROL,
            require_syntax_check=False,
        )
    return core.benchmark_prompt(specification)


def case_snapshot(
    case: core.BenchmarkCase,
    comparison: ComparisonConfig | str | None = None,
    *,
    fixture_snapshot: core.FixtureTreeSnapshot | None = None,
    starter_snapshot: core.FixtureTreeSnapshot | None = None,
    grading_snapshot: core.GradingSnapshot | None = None,
    packet_bytes: bytes | None = None,
) -> dict[str, Any]:
    config = comparison_config(comparison)
    fixture_snapshot = fixture_snapshot or core.snapshot_fixture_tree(case.path)
    derived_starter = core.fixture_subtree_snapshot(fixture_snapshot, "starter")
    if starter_snapshot is not None and starter_snapshot != derived_starter:
        raise RuntimeError(f"{case.id}: starter snapshot is not from fixture snapshot")
    starter_snapshot = derived_starter
    derived_grading = core.grading_snapshot_from_fixture(case, fixture_snapshot)
    if grading_snapshot is not None and grading_snapshot != derived_grading:
        raise RuntimeError(f"{case.id}: grading snapshot is not from fixture snapshot")
    grading_snapshot = derived_grading
    verification_hash = core.verification_fixture_sha256_from_snapshot(
        case,
        starter_snapshot,
    )
    captured_artifacts = {
        variant: core.fixture_snapshot_file(
            fixture_snapshot,
            VARIANTS[variant],
        ).data.decode("utf-8")
        for variant in config.variants
        if variant != "capsule"
    }
    # Preserve the established v3 snapshot exactly so historical raw results can
    # still be rendered and checked against the current fixture tree.
    if config == PACKET_V3:
        return {
            "fixture_sha256": fixture_snapshot.sha256,
            "starter_sha256": starter_snapshot.sha256,
            "verification_fixture_sha256": verification_hash,
            "variants": {
                variant: core.sha256_bytes(
                    captured_artifacts[variant].encode("utf-8")
                )
                for variant in config.variants
            },
        }

    generated_packet = config == MARKDOWN_CAPSULE_LIFECYCLE_V1
    if packet_bytes is None:
        if generated_packet:
            raise RuntimeError(f"{case.id}: lifecycle run lacks a generated Packet v3")
        packet_bytes = core.fixture_snapshot_file(
            fixture_snapshot,
            VARIANTS["packet"],
        ).data
    capsule_text, capsule_metadata = capsule_artifact(
        case,
        starter_snapshot=starter_snapshot,
        packet_bytes=packet_bytes,
    )
    artifacts = {**captured_artifacts, "capsule": capsule_text}
    snapshot = {
        "fixture_sha256": fixture_snapshot.sha256,
        "starter_sha256": starter_snapshot.sha256,
        "verification_fixture_sha256": verification_hash,
        "grading": core.grading_snapshot_metadata(grading_snapshot),
        "artifacts": {
            variant: core.attest_text(text) for variant, text in artifacts.items()
        },
        "variants": {
            variant: core.sha256_bytes(text.encode("utf-8"))
            for variant, text in artifacts.items()
        },
        "prompts": {
            variant: core.sha256_bytes(
                execution_prompt(text, variant, config).encode("utf-8")
            )
            for variant, text in artifacts.items()
        },
        "capsule": capsule_metadata,
    }
    if generated_packet:
        snapshot["packet"] = core.attest_bytes(packet_bytes)
    return snapshot


def capsule_snapshot_artifacts(
    snapshot: Any,
    comparison: ComparisonConfig | str | None = MARKDOWN_CAPSULE_V6,
) -> dict[str, str]:
    config = comparison_config(comparison)
    if not is_capsule_comparison(config):
        raise ValueError(f"{config.name}: not a Capsule comparison")
    if not isinstance(snapshot, dict):
        raise ValueError("Capsule fixture snapshot must be an object")
    artifacts = snapshot.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(config.variants):
        raise ValueError("Capsule fixture snapshot lacks exact artifact bytes")
    return {
        variant: core.attested_text(
            artifacts[variant],
            f"{variant} handoff artifact",
        )
        for variant in config.variants
    }


def capsule_packet_text(capsule_text: str) -> str:
    """Return the Packet bound inside a validated captured Capsule artifact."""

    try:
        capsule = context_capsule_module()
        header, _, embedded, _, _, _ = capsule._parse_capsule(
            capsule_text.encode("utf-8")
        )
        return capsule._reconstructed_packet(
            embedded.decode("utf-8"),
            header["version"],
        )
    except (
        AttributeError,
        OSError,
        RuntimeError,
        UnicodeError,
        ValueError,
    ) as exc:
        raise ValueError("Capsule fixture snapshot has no valid Packet frame") from exc


def capsule_static_rows_from_snapshots(
    cases: list[core.BenchmarkCase],
    snapshots: dict[str, dict[str, Any]],
    comparison: ComparisonConfig | str | None = MARKDOWN_CAPSULE_V6,
) -> list[dict[str, Any]]:
    config = comparison_config(comparison)
    return [
        {
            "case": case.id,
            "variants": {
                variant: core.text_metrics(text)
                for variant, text in capsule_snapshot_artifacts(
                    snapshots[case.id],
                    config,
                ).items()
            },
        }
        for case in cases
    ]


def require_case_snapshot(
    case: core.BenchmarkCase,
    expected: dict[str, Any],
    comparison: ComparisonConfig | str | None = None,
) -> None:
    config = comparison_config(comparison)
    packet_bytes = None
    if config == MARKDOWN_CAPSULE_LIFECYCLE_V1:
        try:
            packet_bytes = core.attested_bytes(
                expected.get("packet") if isinstance(expected, dict) else None,
                f"{case.id} generated Packet v3",
            )
        except (TypeError, UnicodeError, ValueError) as exc:
            raise RuntimeError(
                f"{case.id}: lifecycle snapshot lacks generated Packet v3"
            ) from exc
    if case_snapshot(case, config, packet_bytes=packet_bytes) != expected:
        raise RuntimeError(f"{case.id}: benchmark fixture changed during run")


def result_provenance(
    snapshot: dict[str, Any],
    variant: str,
    specification: str,
    comparison: ComparisonConfig | str | None = None,
) -> dict[str, Any]:
    config = comparison_config(comparison)
    provenance = {
        "spec_sha256": snapshot["variants"][variant],
        "prompt_sha256": core.sha256_bytes(
            execution_prompt(specification, variant, config).encode("utf-8")
        ),
        "starter_sha256": snapshot["starter_sha256"],
        "fixture_sha256": snapshot["fixture_sha256"],
    }
    if is_capsule_comparison(config):
        capsule = snapshot.get("capsule")
        if not isinstance(capsule, dict):
            raise RuntimeError("Capsule v6 snapshot lacks capsule provenance")
        provenance.update({
            "capsule_sha256": capsule["capsule_sha256"],
            "capsule_seal_sha256": capsule["capsule_seal_sha256"],
            "trusted_packet_sha256": capsule["packet_sha256"],
            "route_sha256": capsule["route_sha256"],
            "source_hashes": capsule["source_hashes"],
        })
        expected_prompt = snapshot.get("prompts", {}).get(variant)
        if provenance["prompt_sha256"] != expected_prompt:
            raise RuntimeError("Capsule v6 prompt does not match its snapshot")
    return provenance


def routed_edit_paths(packet_text: str) -> tuple[str, ...]:
    """Return exactly the Packet edit/create routes for action telemetry.

    Read routes and a manifest entrypoint cannot satisfy the Capsule action gate.
    Keep paths runtime-only: they are unnecessary provenance prose in results.
    """

    checker = context_capsule_module().packet_checker
    paths = {
        "/".join(checker._route_parts(target.relative_path))
        for target in checker.parse_routes(packet_text)
        if target.kind in {"edit", "create"}
    }
    return tuple(sorted(paths))


def routed_edit_progress(
    workspace: Path,
    packet_text: str,
    starter_snapshot: core.FixtureTreeSnapshot,
) -> tuple[int, int]:
    """Return required and changed edit/create route counts from secure reads."""

    capsule_module = context_capsule_module()
    checker = capsule_module.packet_checker
    targets = [
        checker.Target(
            target.kind,
            target.raw,
            "/".join(checker._route_parts(target.relative_path)),
            target.start,
            target.end,
            target.anchor,
        )
        for target in checker.parse_routes(packet_text)
        if target.kind in {"edit", "create"}
    ]
    baseline = {item.relative_path: item.data for item in starter_snapshot.files}
    post_targets = [
        checker.Target(
            "edit",
            target.raw,
            target.relative_path,
            target.start,
            target.end,
            target.anchor,
        )
        for target in targets
    ]
    changed = 0
    with checker.open_route_snapshot(
        workspace,
        post_targets,
        max_total_bytes=capsule_module.MAX_CAPSULE_BYTES,
    ) as snapshot:
        for target, entry in zip(targets, snapshot.entries, strict=True):
            if entry.file is None:
                raise RuntimeError("Capsule post-state route is not a regular file")
            if target.kind == "create" or (
                target.relative_path in baseline
                and entry.file.data != baseline[target.relative_path]
            ):
                changed += 1
        snapshot.revalidate()
    return len(targets), changed


def create_document(
    args: argparse.Namespace,
    cases: list[core.BenchmarkCase],
    variants: list[str],
    comparison: ComparisonConfig | str | None = None,
    code_revision: dict[str, Any] | None = None,
    packet_bytes_by_case: dict[str, bytes] | None = None,
    authoring: dict[str, Any] | None = None,
) -> HandoffRunDocument:
    config = comparison_config(comparison)
    corpus = core.discover_cases(cases_dir=CASES_DIR)
    snapshots: dict[str, dict[str, Any]] = {}
    starter_snapshots: dict[str, core.FixtureTreeSnapshot] = {}
    grading_snapshots: dict[str, core.GradingSnapshot] = {}
    captured_artifacts: dict[str, dict[str, str]] = {}
    for case in cases:
        fixture = core.snapshot_fixture_tree(case.path)
        starter = core.fixture_subtree_snapshot(fixture, "starter")
        grading = core.grading_snapshot_from_fixture(case, fixture)
        snapshot = case_snapshot(
            case,
            config,
            fixture_snapshot=fixture,
            starter_snapshot=starter,
            grading_snapshot=grading,
            packet_bytes=(
                packet_bytes_by_case.get(case.id)
                if packet_bytes_by_case is not None
                else None
            ),
        )
        snapshots[case.id] = snapshot
        starter_snapshots[case.id] = starter
        grading_snapshots[case.id] = grading
        captured_artifacts[case.id] = (
            capsule_snapshot_artifacts(snapshot, config)
            if is_capsule_comparison(config)
            else {
                variant: core.fixture_snapshot_file(
                    fixture,
                    VARIANTS[variant],
                ).data.decode("utf-8")
                for variant in config.variants
            }
        )
    if is_capsule_comparison(config) and code_revision is None:
        try:
            code_revision = core.git_revision_attestation(
                CAPSULE_CODE_PATHS,
                require_clean=False,
            )
        except (OSError, RuntimeError, ValueError):
            code_revision = None
    commit = (
        code_revision.get("commit")
        if isinstance(code_revision, dict)
        else core.git_commit()
    )
    document = HandoffRunDocument({
        "schema_version": (
            capsule_schema_version(config) if is_capsule_comparison(config) else 1
        ),
        "kind": config.kind,
        "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "created_at": datetime.now(UTC).isoformat(),
        "provider": args.provider,
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "timeout_seconds": getattr(args, "timeout_seconds", 600),
        "repetitions": args.repetitions,
        "seed": args.seed,
        "telemetry_attestation": "none",
        "cases": [case.id for case in cases],
        "full_corpus": {case.id for case in cases} == {case.id for case in corpus},
        "comparison": config.name,
        "variants": variants,
        "fixture_snapshot": snapshots,
        "environment": {
            "python": sys.version.split()[0],
            "codex": core.command_version(["codex", "--version"]),
            "git_commit": commit,
        },
        "oracle_exposure": (
            "reduced: descriptor-captured hidden tests are materialized only in a "
            "private grader fixture; visible smoke assertions are shared equally "
            "across arms"
        ),
        "static": (
            capsule_static_rows_from_snapshots(cases, snapshots, config)
            if is_capsule_comparison(config)
            else static_rows_from_artifacts(cases, captured_artifacts, config)
        ),
        "results": [],
    }, starter_snapshots, grading_snapshots, captured_artifacts)
    if is_capsule_comparison(config):
        document["execution_profile"] = (
            context_capsule_module().CAPSULE_EXECUTION_PROFILE
        )
        document["code_revision"] = code_revision
    if config == MARKDOWN_CAPSULE_LIFECYCLE_V1:
        if authoring is None:
            raise RuntimeError("lifecycle comparison requires authoring evidence")
        protocol, protocol_bytes = lifecycle_protocol()
        if protocol.get("protocol_id") != LIFECYCLE_CLAIM_PROTOCOL:
            raise RuntimeError("lifecycle claim protocol id does not match the harness")
        document["claim_protocol"] = LIFECYCLE_CLAIM_PROTOCOL
        document["protocol"] = core.attest_bytes(protocol_bytes)
        document["protocol_sha256"] = core.sha256_bytes(protocol_bytes)
        document["baseline_authoring"] = protocol["baseline_authoring"]
        document["authoring"] = authoring
    for field, version in config.versions:
        document[field] = version
    return document


def generated_packet_bytes(
    authoring: dict[str, Any],
    cases: list[core.BenchmarkCase],
) -> dict[str, bytes]:
    """Extract every selected generated Packet from exact embedded attestations."""

    results = authoring.get("results")
    if not isinstance(results, list):
        raise RuntimeError("authoring result has no generated artifacts")
    by_case = {
        result.get("case"): result
        for result in results
        if isinstance(result, dict) and isinstance(result.get("case"), str)
    }
    if set(by_case) != {case.id for case in cases} or len(by_case) != len(results):
        raise RuntimeError("authoring result does not cover the exact benchmark corpus")
    packets: dict[str, bytes] = {}
    for case in cases:
        result = by_case[case.id]
        if result.get("selected_attempt") != 1 or result.get("error") is not None:
            raise RuntimeError(f"{case.id}: authoring did not select its first attempt")
        attempts = result.get("attempts")
        if not isinstance(attempts, list) or len(attempts) != 1:
            raise RuntimeError(f"{case.id}: authoring must contain exactly one attempt")
        text = core.attested_text(
            result.get("specification"),
            f"{case.id} generated Packet v3",
            max_bytes=lifecycle_benchmark.MAX_GENERATED_ARTIFACT_BYTES,
        )
        if attempts[0].get("specification") != result.get("specification"):
            raise RuntimeError(f"{case.id}: selected Packet evidence does not match")
        packets[case.id] = text.encode("utf-8")
    return packets


def generate_lifecycle_authoring(
    args: argparse.Namespace,
    cases: list[core.BenchmarkCase],
    code_revision: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Run one fully accounted authoring attempt for each benchmark fixture."""

    with tempfile.TemporaryDirectory(prefix="capsule-authoring-bench-") as directory:
        generation_args = argparse.Namespace(
            provider=args.provider,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            case=[case.id for case in cases],
            cases_dir=CASES_DIR,
            timeout_seconds=args.timeout_seconds,
            token_encoding=None,
            max_attempts=1,
            output=Path(directory) / "generated",
        )
        generation_output = lifecycle_benchmark.generate(
            generation_args,
            code_revision=code_revision,
        )
        with core.open_pinned_json(generation_output / "generation.json") as pinned:
            authoring = json.loads(json.dumps(pinned.document))
    return authoring, generated_packet_bytes(authoring, cases)


def paired_job_schedule(
    cases: list[Any],
    repetitions: int,
    variants: list[str],
    seed: int,
) -> list[tuple[Any, int, str]]:
    """Return the seeded schedule from stable fixture identity ordering.

    Callers may hold either ``BenchmarkCase`` objects or case ids.  Sorting
    here keeps both execution and credibility reconstruction independent of a
    mutable result document's presentation order.
    """

    ordered_cases = sorted(
        cases,
        key=lambda case: case.id if isinstance(case, core.BenchmarkCase) else str(case),
    )
    pairs = [
        (case, repetition)
        for case in ordered_cases
        for repetition in range(1, repetitions + 1)
    ]
    random.Random(seed).shuffle(pairs)
    jobs: list[tuple[Any, int, str]] = []
    for case, repetition in pairs:
        case_id = case.id if isinstance(case, core.BenchmarkCase) else str(case)
        order = core.counterbalanced_variant_order(
            case_id,
            repetition,
            variants,
            seed,
        )
        jobs.extend((case, repetition, variant) for variant in order)
    return jobs


def run(args: argparse.Namespace) -> Path:
    config = comparison_config(getattr(args, "comparison", None))
    code_revision = None
    if is_capsule_comparison(config) and args.provider == "codex":
        code_revision = core.git_revision_attestation(
            CAPSULE_CODE_PATHS,
            require_clean=True,
        )
    cases = core.discover_cases(args.case, CASES_DIR)
    errors = validate_cases(cases, config)
    if errors:
        raise RuntimeError("handoff benchmark validation failed:\n" + "\n".join(errors))
    output = core.lexical_output_path(args.output)
    variant_names = getattr(args, "variant", []) or list(config.variants)
    if len(variant_names) != len(set(variant_names)):
        raise ValueError("duplicate --variant values are not allowed")
    unknown = set(variant_names) - set(ALL_VARIANTS)
    if unknown:
        raise ValueError(f"unknown --variant values: {', '.join(sorted(unknown))}")
    unsupported = set(variant_names) - set(config.variants)
    if unsupported:
        raise ValueError(
            f"{config.name}: --variant is limited to "
            f"{', '.join(config.variants)}"
        )
    authoring = None
    packet_bytes_by_case = None
    if config == MARKDOWN_CAPSULE_LIFECYCLE_V1:
        if variant_names != list(config.variants):
            raise ValueError("lifecycle comparison requires both benchmark arms")
        protocol, _ = lifecycle_protocol()
        if args.provider == "codex" and any((
            args.model != protocol.get("model"),
            args.reasoning_effort != protocol.get("reasoning_effort"),
            args.timeout_seconds != protocol.get("timeout_seconds"),
            args.repetitions != protocol.get("implementation_repetitions"),
            args.seed != protocol.get("seed"),
            [case.id for case in cases] != protocol.get("corpus"),
        )):
            raise ValueError("real lifecycle run must exactly match the preregistered protocol")
        authoring, packet_bytes_by_case = generate_lifecycle_authoring(
            args,
            cases,
            code_revision,
        )
        if code_revision is not None:
            core.require_git_worktree_revision(code_revision, CAPSULE_CODE_PATHS)
    document = create_document(
        args,
        cases,
        variant_names,
        config,
        code_revision=code_revision,
        packet_bytes_by_case=packet_bytes_by_case,
        authoring=authoring,
    )
    if code_revision is not None:
        core.require_git_worktree_revision(code_revision, CAPSULE_CODE_PATHS)
    jobs = paired_job_schedule(
        cases,
        args.repetitions,
        variant_names,
        args.seed,
    )

    with (
        tempfile.TemporaryDirectory(prefix="execution-packet-bench-") as directory,
        core.open_result_checkpoint(output, document, force=args.force) as checkpoint,
    ):
        root = Path(directory)
        for index, (case, repetition, variant) in enumerate(jobs, start=1):
            if code_revision is not None:
                core.require_git_worktree_revision(code_revision, CAPSULE_CODE_PATHS)
            expected_snapshot = document["fixture_snapshot"][case.id]
            require_case_snapshot(case, expected_snapshot, config)
            run_root = root / f"{index:03d}-{case.id}-{variant}-r{repetition}"
            run_root.mkdir()
            workspace = core.safe_workspace(
                case,
                run_root,
                starter_snapshot=document.starter_snapshots[case.id],
            )
            artifacts = (
                capsule_snapshot_artifacts(expected_snapshot, config)
                if is_capsule_comparison(config)
                else document.artifacts[case.id]
            )
            specification = artifacts[variant]
            prompt = execution_prompt(specification, variant, config)
            if is_capsule_comparison(config):
                require_case_snapshot(case, expected_snapshot, config)
            print(
                f"[{index}/{len(jobs)}] {case.id} {variant} repetition={repetition}",
                flush=True,
            )
            provider = failed_provider("provider did not start")
            provider_completed = False
            capsule_contract_failed = False
            verification = None
            grading_snapshot = document.grading_snapshots[case.id]
            grade = core.empty_grade(case, grading_snapshot)
            # Persist only stable failure categories.  Provider, verifier, and
            # grader text can carry private fixture values or exception output.
            run_errors: list[str] = []
            try:
                provider = (
                    core.run_codex(
                        workspace,
                        prompt,
                        args.model,
                        args.reasoning_effort,
                        args.timeout_seconds,
                        substantive_edit_paths=(
                            routed_edit_paths(capsule_packet_text(artifacts["capsule"]))
                            if is_capsule_comparison(config)
                            else ()
                        ),
                        declared_verification_command=(
                            str(case.manifest["verification_command"])
                            if is_capsule_comparison(config)
                            else None
                        ),
                    )
                    if args.provider == "codex"
                    else core.run_mock(case, workspace)
                )
                provider["attempt_count"] = 1
                provider_completed = True
            except subprocess.TimeoutExpired:
                provider = failed_provider("provider timeout", args.timeout_seconds)
                run_errors.append("provider_timeout")
            except Exception:  # noqa: BLE001 - record a privacy-safe failure code
                provider = failed_provider("provider exception")
                run_errors.append("provider_exception")

            require_case_snapshot(case, expected_snapshot, config)

            if provider_completed:
                if provider.get("return_code") != 0:
                    run_errors.append("provider_nonzero_exit")
                if provider.get("event_errors"):
                    run_errors.append("provider_event_error")
                if is_capsule_comparison(config) and variant == config.primary_candidate:
                    enforce_action_telemetry = bool(
                        args.provider == "codex"
                        or "pre_edit_telemetry" in provider
                    )
                    routed_edits_complete = False
                    try:
                        required_edits, completed_edits = routed_edit_progress(
                            workspace,
                            capsule_packet_text(artifacts["capsule"]),
                            document.starter_snapshots[case.id],
                        )
                    except (OSError, RuntimeError, UnicodeError, ValueError):
                        capsule_contract_failed = True
                        run_errors.append("capsule_routed_edit_attestation_failed")
                    else:
                        if required_edits == 0 or completed_edits == 0:
                            capsule_contract_failed = True
                            run_errors.append("capsule_no_routed_edit")
                        elif completed_edits != required_edits:
                            capsule_contract_failed = True
                            run_errors.append("capsule_incomplete_routed_edits")
                        else:
                            routed_edits_complete = True
                    telemetry = provider.get("pre_edit_telemetry")
                    if (
                        routed_edits_complete
                        and enforce_action_telemetry
                        and not current_routed_edit_telemetry(telemetry)
                    ):
                        capsule_contract_failed = True
                        run_errors.append("capsule_routed_edit_attestation_failed")
                    if enforce_action_telemetry:
                        if not capsule_has_exact_tool_sequence(provider):
                            capsule_contract_failed = True
                            run_errors.append("capsule_tool_sequence")
                        pre_edit_categories = provider.get(
                            "pre_edit_command_categories",
                            {},
                        )
                        pre_edit_commands = provider.get(
                            "pre_edit_command_executions"
                        )
                        if (
                            type(pre_edit_commands) is not int
                            or pre_edit_commands < 0
                        ):
                            capsule_contract_failed = True
                            run_errors.append(
                                "capsule_routed_edit_attestation_failed"
                            )
                        elif pre_edit_commands > 0:
                            capsule_contract_failed = True
                            run_errors.append("capsule_pre_edit_command")
                        for category, code in (
                            ("discovery", "capsule_pre_edit_discovery"),
                            ("read", "capsule_pre_edit_read"),
                            ("verify", "capsule_pre_edit_verification"),
                        ):
                            count = (
                                pre_edit_categories.get(category)
                                if isinstance(pre_edit_categories, dict)
                                else None
                            )
                            if type(count) is not int or count < 0:
                                capsule_contract_failed = True
                                run_errors.append(
                                    "capsule_routed_edit_attestation_failed"
                                )
                            elif count > 0:
                                capsule_contract_failed = True
                                run_errors.append(code)
                        declared_count = provider.get(
                            "declared_verification_executions"
                        )
                        pre_edit_declared_count = provider.get(
                            "pre_edit_declared_verification_executions"
                        )
                        if (
                            type(declared_count) is not int
                            or declared_count != 1
                        ):
                            capsule_contract_failed = True
                            run_errors.append(
                                "capsule_declared_verification_count"
                            )
                        if (
                            type(pre_edit_declared_count) is not int
                            or pre_edit_declared_count < 0
                        ):
                            capsule_contract_failed = True
                            run_errors.append(
                                "capsule_routed_edit_attestation_failed"
                            )
                        elif pre_edit_declared_count > 0:
                            capsule_contract_failed = True
                            run_errors.append("capsule_pre_edit_verification")
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
                        run_errors.append("verification_failed")
                except subprocess.TimeoutExpired:
                    verification_failed = True
                    run_errors.append("verification_timeout")
                except Exception:  # noqa: BLE001 - record a privacy-safe failure code
                    verification_failed = True
                    run_errors.append("verification_exception")
                try:
                    grade = core.run_grader(
                        case,
                        workspace,
                        trusted=trusted,
                        grading_snapshot=grading_snapshot,
                    )
                except subprocess.TimeoutExpired:
                    run_errors.append("grader_timeout")
                except Exception:  # noqa: BLE001 - record a privacy-safe failure code
                    run_errors.append("grader_exception")
                if verification_failed or capsule_contract_failed:
                    grade["task_success"] = False

            require_case_snapshot(case, expected_snapshot, config)
            error = {"codes": sorted(set(run_errors))} if run_errors else None
            provider = core.redact_provider_telemetry(provider)

            result = core.redact_result_telemetry({
                "case": case.id,
                "pair_id": f"{case.id}:r{repetition}",
                "variant": variant,
                "repetition": repetition,
                "run_order": index,
                "spec": core.text_metrics(specification),
                "provenance": result_provenance(
                    expected_snapshot,
                    variant,
                    specification,
                    config,
                ),
                "provider": provider,
                "verification": verification,
                "grade": grade,
                "error": error,
            })
            document["results"].append(result)
            checkpoint.write_json(document)
            if code_revision is not None:
                core.require_git_worktree_revision(code_revision, CAPSULE_CODE_PATHS)
    return output


def metric(result: dict[str, Any], name: str) -> float | int | None:
    provider = result["provider"]
    if name == "combined_tokens":
        usage = provider.get("usage", {})
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if isinstance(input_tokens, int) and isinstance(output_tokens, int):
            return input_tokens + output_tokens
        return None
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
    if name == "pre_edit_discovery_commands":
        return provider.get("pre_edit_command_categories", {}).get("discovery")
    if name == "pre_edit_read_commands":
        return provider.get("pre_edit_command_categories", {}).get("read")
    if name == "pre_edit_verification_commands":
        return provider.get("pre_edit_command_categories", {}).get("verify")
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
                "combined_tokens",
                "input_tokens",
                "uncached_input_tokens",
                "output_tokens",
                "duration_seconds",
                "tool_calls",
                "command_executions",
                "discovery_commands",
                "read_commands",
                "verification_commands",
                "pre_edit_discovery_commands",
                "pre_edit_read_commands",
                "pre_edit_verification_commands",
            )
        },
        **{
            f"median_{name}": core.median(values(name))
            for name in (
                "combined_tokens",
                "input_tokens",
                "uncached_input_tokens",
                "output_tokens",
                "duration_seconds",
                "tool_calls",
                "command_executions",
                "discovery_commands",
                "read_commands",
                "verification_commands",
                "pre_edit_discovery_commands",
                "pre_edit_read_commands",
                "pre_edit_verification_commands",
            )
        },
    }


def comparison(
    results: list[dict[str, Any]],
    baseline: str,
    candidate: str,
    name: str,
) -> dict[str, Any]:
    pairs = paired_result_index(results)
    if pairs is None:
        return empty_comparison()
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


def empty_comparison() -> dict[str, Any]:
    """Return a fail-closed paired summary for malformed result collections."""

    return {
        "pairs": 0,
        "wins": 0,
        "ties": 0,
        "losses": 0,
        "baseline_total": 0.0,
        "candidate_total": 0.0,
        "median_reduction_percent": None,
        "fixture_medians": {},
        "fixture_cluster_bootstrap_95_ci": None,
    }


def paired_result_index(
    results: list[dict[str, Any]],
) -> dict[tuple[str, int], dict[str, dict[str, Any]]] | None:
    """Index paired results without allowing duplicate arms to overwrite.

    A report may be rendered for old, partial data, but paired totals must not
    select whichever duplicate happened to appear last.  Treat any malformed
    or duplicated (case, repetition, variant) key as unusable paired evidence.
    """

    pairs: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    for result in results:
        if not isinstance(result, dict):
            return None
        case = result.get("case")
        repetition = result.get("repetition")
        variant = result.get("variant")
        if (
            not isinstance(case, str)
            or not isinstance(repetition, int)
            or isinstance(repetition, bool)
            or not isinstance(variant, str)
        ):
            return None
        pair = pairs.setdefault((case, repetition), {})
        if variant in pair:
            return None
        pair[variant] = result
    return pairs


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


_PRIVACY_REDACTED_GRADE_FIELDS = frozenset({
    "passed",
    "total",
    "pass_rate",
    "acceptance_passed",
    "acceptance_total",
    "acceptance_pass_rate",
    "task_success",
    "failures",
})
_PRE_EDIT_TELEMETRY_FIELDS = frozenset({
    "schema_version",
    "status",
    "target_count",
    "observed_target_count",
    "target_paths_sha256",
    "observed_paths_sha256",
    "file_change_events",
    "unclassified_file_change_events",
    "substantive_file_change_events",
})
_PRIVACY_PROVIDER_FIELDS = frozenset({
    "return_code",
    "duration_seconds",
    "usage",
    "tool_calls",
    "tool_call_total",
    "command_categories",
    "pre_edit_command_categories",
    "pre_edit_command_executions",
    "declared_verification_executions",
    "pre_edit_declared_verification_executions",
    "pre_edit_telemetry",
    "command_log",
    "thread_id_metadata",
    "final_message_metadata",
    "stderr_metadata",
    "stdout_metadata",
    "event_errors",
    "attempt_count",
})
_REQUIRED_CAPSULE_PROVIDER_FIELDS = frozenset({
    "return_code",
    "duration_seconds",
    "usage",
    "tool_calls",
    "tool_call_total",
    "command_categories",
    "pre_edit_command_categories",
    "pre_edit_command_executions",
    "declared_verification_executions",
    "pre_edit_declared_verification_executions",
    "pre_edit_telemetry",
    "command_log",
    "event_errors",
    "attempt_count",
})
_PROVIDER_METADATA_FIELDS = frozenset({
    "thread_id_metadata",
    "final_message_metadata",
    "stderr_metadata",
    "stdout_metadata",
})
_COMMAND_LOG_FIELDS = frozenset({
    "categories",
    "command_bytes",
    "command_sha256",
    "exit_code",
    "pre_edit",
    "declared_verification",
})
_PRIVACY_VERIFICATION_FIELDS = frozenset({
    "command_metadata",
    "fixture_sha256",
    "return_code",
    "stdout_metadata",
    "stderr_metadata",
})


def current_text_metadata(value: Any) -> bool:
    """Accept only the exact digest-and-length representation of private text."""

    return bool(
        isinstance(value, dict)
        and set(value) == {"bytes", "sha256"}
        and isinstance(value.get("bytes"), int)
        and not isinstance(value.get("bytes"), bool)
        and value["bytes"] >= 0
        and isinstance(value.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None
    )


def _current_count_map(
    value: Any,
    allowed: frozenset[str],
    required: frozenset[str] = frozenset(),
) -> bool:
    return bool(
        isinstance(value, dict)
        and required <= set(value) <= allowed
        and all(
            isinstance(item, int) and not isinstance(item, bool) and item >= 0
            for item in value.values()
        )
    )


def _current_boolean_map(value: Any, fields: frozenset[str]) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == fields
        and all(isinstance(item, bool) for item in value.values())
    )


def current_privacy_redacted_provider(provider: Any) -> bool:
    """Validate the exact current Capsule provider checkpoint allowlist."""

    if (
        not isinstance(provider, dict)
        or not _REQUIRED_CAPSULE_PROVIDER_FIELDS <= set(provider)
        or not set(provider) <= _PRIVACY_PROVIDER_FIELDS
        or core.redact_provider_telemetry(provider) != provider
    ):
        return False
    duration = provider.get("duration_seconds")
    if (
        not isinstance(provider.get("return_code"), int)
        or isinstance(provider.get("return_code"), bool)
        or not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or (isinstance(duration, float) and not math.isfinite(duration))
        or duration < 0
        or not isinstance(provider.get("tool_call_total"), int)
        or isinstance(provider.get("tool_call_total"), bool)
        or provider["tool_call_total"] < 0
        or not _current_count_map(
            provider.get("usage"),
            core.PROVIDER_USAGE_FIELDS,
            frozenset({
                "input_tokens",
                "uncached_input_tokens",
                "output_tokens",
            }),
        )
        or not _current_count_map(
            provider.get("tool_calls"),
            core.PROVIDER_TOOL_CALL_FIELDS,
            frozenset({"command_execution"}),
        )
    ):
        return False
    category_fields = frozenset(core.COMMAND_CATEGORY_PATTERNS)
    for field in ("command_categories", "pre_edit_command_categories"):
        if not _current_count_map(provider.get(field), category_fields, category_fields):
            return False
    for field in _PROVIDER_METADATA_FIELDS & set(provider):
        if not current_text_metadata(provider[field]):
            return False
    errors = provider.get("event_errors")
    if not isinstance(errors, list) or any(
        not current_text_metadata(error) for error in errors
    ):
        return False
    for field in (
        "pre_edit_command_executions",
        "declared_verification_executions",
        "pre_edit_declared_verification_executions",
    ):
        count = provider[field]
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return False
    if provider["attempt_count"] != 1:
        return False
    command_log = provider["command_log"]
    if not isinstance(command_log, list):
        return False
    for record in command_log:
        if (
            not isinstance(record, dict)
            or set(record) != _COMMAND_LOG_FIELDS
            or not _current_boolean_map(record.get("categories"), category_fields)
            or not isinstance(record.get("command_bytes"), int)
            or isinstance(record.get("command_bytes"), bool)
            or record["command_bytes"] < 0
            or not isinstance(record.get("command_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", record["command_sha256"])
            is None
            or (
                record.get("exit_code") is not None
                and (
                    not isinstance(record["exit_code"], int)
                    or isinstance(record["exit_code"], bool)
                )
            )
            or not isinstance(record.get("pre_edit"), bool)
            or not isinstance(record.get("declared_verification"), bool)
        ):
            return False
    if provider["tool_calls"]["command_execution"] != len(command_log):
        return False
    if provider["tool_call_total"] != sum(provider["tool_calls"].values()):
        return False
    expected_categories = {
        category: sum(record["categories"][category] for record in command_log)
        for category in category_fields
    }
    expected_pre_edit_categories = {
        category: sum(
            record["categories"][category]
            for record in command_log
            if record["pre_edit"]
        )
        for category in category_fields
    }
    if (
        provider["command_categories"] != expected_categories
        or provider["pre_edit_command_categories"]
        != expected_pre_edit_categories
        or provider["pre_edit_command_executions"]
        != sum(record["pre_edit"] for record in command_log)
        or provider["declared_verification_executions"]
        != sum(record["declared_verification"] for record in command_log)
        or provider["pre_edit_declared_verification_executions"]
        != sum(
            record["pre_edit"] and record["declared_verification"]
            for record in command_log
        )
    ):
        return False
    return True


def current_privacy_redacted_verification(verification: Any) -> bool:
    """Reject raw commands/output and unknown current verifier fields."""

    if (
        not isinstance(verification, dict)
        or not {"command_metadata", "fixture_sha256", "return_code"}
        <= set(verification)
        or not set(verification) <= _PRIVACY_VERIFICATION_FIELDS
        or core.redact_verification(verification) != verification
        or not current_text_metadata(verification.get("command_metadata"))
        or not isinstance(verification.get("fixture_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", verification["fixture_sha256"])
        is None
        or not isinstance(verification.get("return_code"), int)
        or isinstance(verification.get("return_code"), bool)
    ):
        return False
    return all(
        current_text_metadata(verification[field])
        for field in ("stdout_metadata", "stderr_metadata")
        if field in verification
    )


def current_privacy_redacted_grade(
    grade: Any,
    grading_snapshot: Any = None,
) -> bool:
    """Return whether ``grade`` is the current aggregate-only checkpoint form.

    Capsule claims are deliberately tied to the result schema emitted by
    ``core.redact_result_telemetry``.  In particular, the historical grader's
    per-acceptance map and prose-bearing failure records are not claim evidence,
    even when their aggregate counts look favorable.  Packet v3 predates that
    checkpoint boundary, so its legacy report path intentionally does not use
    this predicate.
    """

    if (
        not isinstance(grade, dict)
        or set(grade) != _PRIVACY_REDACTED_GRADE_FIELDS
    ):
        return False

    def count(name: str) -> int | None:
        value = grade.get(name)
        return (
            value
            if isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
            else None
        )

    passed = count("passed")
    total = count("total")
    acceptance_passed = count("acceptance_passed")
    acceptance_total = count("acceptance_total")
    if (
        passed is None
        or total is None
        or acceptance_passed is None
        or acceptance_total is None
        or passed > total
        or acceptance_passed > acceptance_total
        or not isinstance(grade.get("task_success"), bool)
        or grade["task_success"]
        != (acceptance_total > 0 and acceptance_passed == acceptance_total)
        or not isinstance(grade.get("failures"), list)
        or (
            grading_snapshot is not None
            and not core.grade_matches_grading_snapshot(grade, grading_snapshot)
        )
    ):
        return False

    for rate_name, numerator, denominator in (
        ("pass_rate", passed, total),
        ("acceptance_pass_rate", acceptance_passed, acceptance_total),
    ):
        rate = grade.get(rate_name)
        if (
            not isinstance(rate, (int, float))
            or isinstance(rate, bool)
            or (isinstance(rate, float) and not math.isfinite(rate))
            or rate < 0
            or rate > 1
            or rate != (numerator / denominator if denominator else 0.0)
        ):
            return False

    # Re-redaction is an inexpensive structural privacy check: it rejects
    # unredacted acceptance maps, raw failure prose, and fields introduced by
    # older producer schemas.  It also means a future redactor change fails
    # Capsule claims closed until this explicit schema gate is reviewed.
    return (
        core.redact_grade(grade) == grade
        and len(grade["failures"]) == total - passed
    )


def current_routed_edit_telemetry(telemetry: Any) -> bool:
    """Return whether a result has the complete current routed-edit attestation."""

    if (
        not isinstance(telemetry, dict)
        or set(telemetry) != _PRE_EDIT_TELEMETRY_FIELDS
        or telemetry.get("schema_version") != 4
        or telemetry.get("status") != "routed_edit_observed"
    ):
        return False
    counts: dict[str, int] = {}
    for field in (
        "target_count",
        "observed_target_count",
        "file_change_events",
        "unclassified_file_change_events",
        "substantive_file_change_events",
    ):
        value = telemetry.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return False
        counts[field] = value
    target_digest = telemetry.get("target_paths_sha256")
    observed_digest = telemetry.get("observed_paths_sha256")
    if (
        not isinstance(target_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", target_digest) is None
        or not isinstance(observed_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", observed_digest) is None
    ):
        return False
    return bool(
        counts["target_count"] > 0
        and counts["observed_target_count"] == counts["target_count"]
        and observed_digest == target_digest
        and counts["substantive_file_change_events"] > 0
        and counts["file_change_events"]
        >= counts["substantive_file_change_events"]
        and counts["file_change_events"]
        >= counts["unclassified_file_change_events"]
    )


def capsule_route_telemetry_matches_snapshot(
    document: Any,
    result: Any,
) -> bool:
    """Bind redacted provider route evidence to the attested Packet path set."""

    if not isinstance(document, dict) or not isinstance(result, dict):
        return False
    try:
        config = document_comparison(document)
        if not is_capsule_comparison(config):
            return False
        snapshot = document["fixture_snapshot"][result["case"]]
        artifacts = capsule_snapshot_artifacts(snapshot, config)
        packet = capsule_packet_text(artifacts["capsule"])
        expected_paths = routed_edit_paths(packet)
    except (KeyError, TypeError, ValueError):
        return False
    provider = result.get("provider")
    telemetry = (
        provider.get("pre_edit_telemetry")
        if isinstance(provider, dict)
        else None
    )
    expected_digest = core.telemetry_path_set_sha256(expected_paths)
    return bool(
        expected_paths
        and current_routed_edit_telemetry(telemetry)
        and telemetry["target_count"] == len(expected_paths)
        and telemetry["observed_target_count"] == len(expected_paths)
        and telemetry["target_paths_sha256"] == expected_digest
        and telemetry["observed_paths_sha256"] == expected_digest
    )


def capsule_has_exact_tool_sequence(provider: Any) -> bool:
    """Require one all-route change followed by one verification command."""

    command_log = provider.get("command_log") if isinstance(provider, dict) else None
    return bool(
        isinstance(provider, dict)
        and provider.get("tool_calls")
        == {"command_execution": 1, "file_change": 1}
        and provider.get("tool_call_total") == 2
        and isinstance(command_log, list)
        and len(command_log) == 1
        and isinstance(command_log[0], dict)
        and command_log[0].get("declared_verification") is True
        and command_log[0].get("pre_edit") is False
        and command_log[0].get("exit_code") == 0
        and current_routed_edit_telemetry(provider.get("pre_edit_telemetry"))
        and provider["pre_edit_telemetry"].get("file_change_events") == 1
        and provider["pre_edit_telemetry"].get(
            "substantive_file_change_events"
        )
        == 1
        and provider["pre_edit_telemetry"].get(
            "unclassified_file_change_events"
        )
        == 0
    )


def _validated_capsule_snapshot(
    case: core.BenchmarkCase,
    snapshot: Any,
    fixture_snapshot: core.FixtureTreeSnapshot,
    starter_snapshot: core.FixtureTreeSnapshot,
    config: ComparisonConfig,
) -> dict[str, str] | None:
    """Validate Capsule evidence while deriving every row from attested bytes."""

    expected_keys = {
        "fixture_sha256",
        "starter_sha256",
        "verification_fixture_sha256",
        "grading",
        "artifacts",
        "variants",
        "prompts",
        "capsule",
    }
    if config == MARKDOWN_CAPSULE_LIFECYCLE_V1:
        expected_keys.add("packet")
    if not isinstance(snapshot, dict) or set(snapshot) != expected_keys:
        return None
    try:
        artifacts = capsule_snapshot_artifacts(snapshot, config)
        packet_bytes = (
            core.attested_bytes(
                snapshot["packet"],
                f"{case.id} generated Packet v3",
            )
            if config == MARKDOWN_CAPSULE_LIFECYCLE_V1
            else core.fixture_snapshot_file(
                fixture_snapshot,
                VARIANTS["packet"],
            ).data
        )
        capsule_bytes = artifacts["capsule"].encode("utf-8")
        for variant in config.variants:
            if variant == "capsule":
                continue
            if artifacts[variant].encode("utf-8") != core.fixture_snapshot_file(
                fixture_snapshot,
                VARIANTS[variant],
            ).data:
                return None
        verification_hash = core.verification_fixture_sha256_from_snapshot(
            case,
            starter_snapshot,
        )
        grading = core.grading_snapshot_from_fixture(case, fixture_snapshot)
        module = context_capsule_module()
        checked, sources, seal = _check_capsule_snapshot(
            case,
            starter_snapshot,
            packet_bytes,
            capsule_bytes,
        )
        source_hashes = _capsule_source_hashes(module, capsule_bytes)
        capsule_metadata = {
            "capsule_sha256": core.sha256_bytes(capsule_bytes),
            "capsule_seal_sha256": seal,
            "packet_sha256": core.sha256_bytes(packet_bytes),
            "route_sha256": checked.get("route_sha256"),
            "source_hashes": source_hashes,
            "source_count": len(sources),
            "source_bytes": sum(len(payload) for _, payload in sources),
        }
        expected_variants = {
            variant: core.sha256_bytes(text.encode("utf-8"))
            for variant, text in artifacts.items()
        }
        expected_prompts = {
            variant: core.sha256_bytes(
                execution_prompt(text, variant, config).encode("utf-8")
            )
            for variant, text in artifacts.items()
        }
        if (
            snapshot["fixture_sha256"] != fixture_snapshot.sha256
            or snapshot["starter_sha256"] != starter_snapshot.sha256
            or snapshot["verification_fixture_sha256"] != verification_hash
            or snapshot["grading"] != core.grading_snapshot_metadata(grading)
            or snapshot["variants"] != expected_variants
            or snapshot["prompts"] != expected_prompts
            or snapshot["capsule"] != capsule_metadata
        ):
            return None
        return artifacts
    except (KeyError, OSError, RuntimeError, TypeError, UnicodeError, ValueError):
        return None


def lifecycle_authoring_is_credible(
    document: dict[str, Any],
    corpus: list[core.BenchmarkCase],
) -> bool:
    """Bind generated Packet bytes and all authoring usage to the clean run."""

    authoring = document.get("authoring")
    snapshots = document.get("fixture_snapshot")
    if (
        not isinstance(authoring, dict)
        or not isinstance(snapshots, dict)
        or set(document) != {
            "schema_version",
            "kind",
            "run_id",
            "created_at",
            "provider",
            "model",
            "reasoning_effort",
            "timeout_seconds",
            "repetitions",
            "seed",
            "telemetry_attestation",
            "cases",
            "full_corpus",
            "comparison",
            "variants",
            "fixture_snapshot",
            "environment",
            "oracle_exposure",
            "static",
            "results",
            "execution_profile",
            "code_revision",
            "claim_protocol",
            "protocol",
            "protocol_sha256",
            "baseline_authoring",
            "authoring",
            "packet_version",
            "capsule_version",
            "lifecycle_version",
        }
        or set(authoring) != {
            "schema_version",
            "kind",
            "run_id",
            "created_at",
            "provider",
            "model",
            "reasoning_effort",
            "timeout_seconds",
            "token_encoding",
            "max_attempts",
            "cases",
            "case_suite",
            "full_corpus",
            "environment",
            "skill_sha256",
            "fixture_snapshot",
            "results",
            "code_revision",
        }
    ):
        return False
    try:
        protocol, protocol_bytes = lifecycle_protocol()
        recorded_protocol = core.attested_bytes(
            document.get("protocol"),
            "Capsule lifecycle preregistration",
            max_bytes=64 * 1024,
        )
        if not lifecycle_benchmark.generation_report_is_credible(
            authoring,
            cases_dir=CASES_DIR,
            skill_dir=SKILL_DIR,
        ):
            return False
        generated = generated_packet_bytes(authoring, corpus)
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError):
        return False
    authoring_environment = authoring.get("environment")
    document_environment = document.get("environment")
    if (
        recorded_protocol != protocol_bytes
        or document.get("protocol_sha256") != core.sha256_bytes(protocol_bytes)
        or protocol.get("schema_version") != 1
        or protocol.get("protocol_id") != LIFECYCLE_CLAIM_PROTOCOL
        or protocol.get("comparison") != MARKDOWN_CAPSULE_LIFECYCLE_V1.name
        or protocol.get("provider") != "codex"
        or protocol.get("authoring_attempts_per_fixture") != 1
        or protocol.get("baseline_authoring")
        != "source-markdown-direct-zero-model-calls"
        or protocol.get("candidate_authoring")
        != "model-authored-packet-v3-then-deterministic-capsule-v6"
        or protocol.get("corpus") != document.get("cases")
        or protocol.get("provider") != document.get("provider")
        or protocol.get("model") != document.get("model")
        or protocol.get("reasoning_effort") != document.get("reasoning_effort")
        or protocol.get("timeout_seconds") != document.get("timeout_seconds")
        or protocol.get("implementation_repetitions")
        != document.get("repetitions")
        or protocol.get("seed") != document.get("seed")
        or document.get("claim_protocol") != LIFECYCLE_CLAIM_PROTOCOL
        or document.get("baseline_authoring")
        != "source-markdown-direct-zero-model-calls"
        or authoring.get("kind") != "semantic-spec-generation"
        or authoring.get("provider") != document.get("provider")
        or authoring.get("model") != document.get("model")
        or authoring.get("reasoning_effort") != document.get("reasoning_effort")
        or authoring.get("timeout_seconds") != document.get("timeout_seconds")
        or authoring.get("max_attempts") != 1
        or authoring.get("token_encoding") is not None
        or authoring.get("full_corpus") is not True
        or authoring.get("cases") != document.get("cases")
        or authoring.get("code_revision") != document.get("code_revision")
        or not isinstance(authoring_environment, dict)
        or not isinstance(document_environment, dict)
        or set(authoring_environment) != {"platform", "python", "codex", "git_commit"}
        or set(document_environment) != {"python", "codex", "git_commit"}
        or authoring_environment.get("git_commit")
        != document_environment.get("git_commit")
    ):
        return False
    by_case = {result["case"]: result for result in authoring["results"]}
    for case in corpus:
        try:
            packet = core.attested_bytes(
                snapshots[case.id]["packet"],
                f"{case.id} lifecycle Packet v3",
            )
            result = by_case[case.id]
            attempt = result["attempts"][0]
            if (
                set(result) != {
                    "case",
                    "artifact",
                    "selected_attempt",
                    "attempts",
                    "specification",
                    "source",
                    "semantic",
                    "compression",
                    "provenance",
                    "provider",
                    "error",
                }
                or set(attempt) != {
                    "attempt",
                    "artifact",
                    "specification",
                    "semantic",
                    "conversion_check",
                    "provenance",
                    "provider",
                    "error",
                }
                or set(result["provenance"]) != {
                    "source_sha256",
                    "spec_sha256",
                    "prompt_sha256",
                    "starter_sha256",
                    "fixture_sha256",
                    "skill_sha256",
                }
                or set(attempt["provenance"]) != set(result["provenance"])
                or core.redact_provider_telemetry(result["provider"])
                != result["provider"]
                or core.redact_provider_telemetry(attempt["provider"])
                != attempt["provider"]
            ):
                return False
            expected_provider = core.redact_provider_telemetry(
                lifecycle_benchmark.aggregate_attempt_providers(
                    result["attempts"],
                    1,
                )
            )
            expected_prompt_sha256 = core.sha256_bytes(
                lifecycle_benchmark.generation_prompt(case, None).encode("utf-8")
            )
            source_metrics = result["source"]
            semantic_metrics = result["semantic"]
            expected_conversion = {
                "encoding": None,
                "source": {
                    "bytes": source_metrics["bytes"],
                    "words": source_metrics["words"],
                },
                "output": {
                    "bytes": semantic_metrics["bytes"],
                    "words": semantic_metrics["words"],
                },
                "smaller_bytes": semantic_metrics["bytes"] < source_metrics["bytes"],
                "smaller_words": semantic_metrics["words"] < source_metrics["words"],
            }
            expected_compression = {
                "bytes_percent": core.compression_percent(
                    source_metrics["bytes"], semantic_metrics["bytes"]
                ),
                "words_percent": core.compression_percent(
                    source_metrics["words"], semantic_metrics["words"]
                ),
            }
            if (
                generated[case.id] != packet
                or result.get("provider") != expected_provider
                or result.get("compression") != expected_compression
                or attempt.get("conversion_check") != expected_conversion
                or result["provenance"].get("prompt_sha256")
                != expected_prompt_sha256
                or attempt["provenance"].get("prompt_sha256")
                != expected_prompt_sha256
            ):
                return False
        except (KeyError, TypeError, UnicodeError, ValueError):
            return False
    return True


def report_run_is_credible(
    document: dict[str, Any],
    results: list[dict[str, Any]],
) -> bool:
    """Validate reproducible run structure and legacy Packet-compatible facts.

    This intentionally remains a structural integrity predicate so callers can
    inspect historical Capsule records without treating them as claim evidence.
    ``capsule_report_is_credible`` adds the current privacy and routed-edit
    schema requirements used by the Capsule report and its product-claim gate.
    """

    try:
        config = document_comparison(document)
    except ValueError:
        return False
    cases = document.get("cases")
    repetitions = document.get("repetitions")
    variants = document.get("variants")
    seed = document.get("seed")
    snapshots = document.get("fixture_snapshot")
    artifacts_by_case: dict[str, dict[str, str]] = {}
    try:
        corpus = core.discover_cases(cases_dir=CASES_DIR)
        if is_capsule_comparison(config):
            if not isinstance(snapshots, dict):
                return False
            for case in corpus:
                fixture = core.snapshot_fixture_tree(case.path)
                starter = core.fixture_subtree_snapshot(fixture, "starter")
                artifacts = _validated_capsule_snapshot(
                    case,
                    snapshots.get(case.id),
                    fixture,
                    starter,
                    config,
                )
                if artifacts is None:
                    return False
                artifacts_by_case[case.id] = artifacts
            current_snapshots = snapshots
            current_static = capsule_static_rows_from_snapshots(
                corpus,
                snapshots,
                config,
            )
        else:
            current_snapshots = {
                case.id: case_snapshot(case, config) for case in corpus
            }
            current_static = static_rows(corpus, comparison=config)
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return False
    canonical_case_ids = [case.id for case in corpus]
    corpus_ids = set(canonical_case_ids)
    if (
        not isinstance(cases, list)
        or not cases
        or any(not isinstance(case, str) for case in cases)
        or cases != canonical_case_ids
        or not isinstance(repetitions, int)
        or isinstance(repetitions, bool)
        or repetitions < 3
        or not isinstance(seed, int)
        or isinstance(seed, bool)
        or not isinstance(variants, list)
        or variants != list(config.variants)
        or document.get("comparison") not in (None, config.name)
        or (
            document.get("kind") is not None
            and document.get("kind") != config.kind
        )
        or any(document.get(field) != version for field, version in config.versions)
        or (
            is_capsule_comparison(config)
            and document.get("schema_version") != capsule_schema_version(config)
        )
        or (
            is_capsule_comparison(config)
            and document.get("execution_profile")
            != context_capsule_module().CAPSULE_EXECUTION_PROFILE
        )
        or (is_capsule_comparison(config) and document.get("full_corpus") is not True)
        or not isinstance(snapshots, dict)
        or set(snapshots) != corpus_ids
        or snapshots != current_snapshots
        or document.get("static") != current_static
        or not results
        or (
            config == MARKDOWN_CAPSULE_LIFECYCLE_V1
            and not lifecycle_authoring_is_credible(document, corpus)
        )
        or (
            is_capsule_comparison(config)
            and (
                document.get("telemetry_attestation") != "none"
                or not isinstance(document.get("environment"), dict)
                or not core.git_revision_attestation_is_valid(
                    document.get("code_revision"),
                    CAPSULE_CODE_PATHS,
                    environment_commit=document.get("environment", {}).get(
                        "git_commit"
                    ),
                )
            )
        )
    ):
        return False

    expected_keys = {
        (case, repetition, variant)
        for case in canonical_case_ids
        for repetition in range(1, repetitions + 1)
        for variant in config.variants
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
    # Results may contain a forged `cases` order.  Reconstruct the seed-based
    # schedule from the fixture discovery order and the comparison's fixed arm
    # order, never from document-controlled ordering.
    expected_order = paired_job_schedule(
        canonical_case_ids,
        repetitions,
        list(config.variants),
        seed,
    )
    actual_order = [
        (result.get("case"), result.get("repetition"), result.get("variant"))
        for result in sorted(results, key=lambda item: item["run_order"])
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
        provider = result.get("provider")
        verification = result.get("verification")
        provenance = result.get("provenance")
        grade = result.get("grade")
        usage = provider.get("usage") if isinstance(provider, dict) else None
        event_errors = provider.get("event_errors") if isinstance(provider, dict) else None
        pre_edit_categories = (
            provider.get("pre_edit_command_categories")
            if isinstance(provider, dict)
            else None
        )
        case = cases_by_id.get(result.get("case"))
        variant = result.get("variant")
        if case is None or variant not in config.variants:
            return False
        try:
            snapshot = current_snapshots[case.id]
            specification = (
                artifacts_by_case[case.id][variant]
                if is_capsule_comparison(config)
                else artifact_text(case, variant, config)
            )
            expected_provenance = result_provenance(
                snapshot,
                variant,
                specification,
                config,
            )
        except (OSError, RuntimeError, UnicodeError, ValueError):
            return False
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
            or (
                is_capsule_comparison(config)
                and (
                    not isinstance(pre_edit_categories, dict)
                    or any(
                        not nonnegative_int(pre_edit_categories.get(field))
                        for field in ("discovery", "read", "verify")
                    )
                )
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
            or not core.text_reference_matches(
                verification,
                "command",
                "command_metadata",
                expected_verification,
            )
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


def capsule_report_is_credible(
    document: dict[str, Any],
    results: list[dict[str, Any]],
) -> bool:
    """Require current privacy and path-attested schemas for Capsule claims.

    Aggregate command counts can be preserved for historical reporting, but
    cannot establish a Capsule claim without the per-result routed-edit
    attestation and the current aggregate-only, privacy-redacted grade form.
    """

    try:
        config = document_comparison(document)
        if not is_capsule_comparison(config):
            return False
    except ValueError:
        return False
    if not report_run_is_credible(document, results):
        return False
    return all(
        isinstance(result, dict)
        and current_privacy_redacted_provider(result.get("provider"))
        and current_privacy_redacted_verification(result.get("verification"))
        and current_privacy_redacted_grade(
            result.get("grade"),
            document.get("fixture_snapshot", {})
            .get(result.get("case"), {})
            .get("grading"),
        )
        and current_routed_edit_telemetry(
            result.get("provider", {}).get("pre_edit_telemetry")
            if isinstance(result.get("provider"), dict)
            else None
        )
        and capsule_route_telemetry_matches_snapshot(document, result)
        and (
            result.get("variant") != config.primary_candidate
            or capsule_has_exact_tool_sequence(result.get("provider"))
        )
        for result in results
    )


def packet_v3_report(document: dict[str, Any]) -> str:
    """Render the established v3 report without changing historical wording."""

    config = PACKET_V3
    results = document["results"]
    aggregates = {variant: aggregate(results, variant) for variant in config.variants}
    primary_uncached = comparison(
        results,
        config.primary_baseline,
        config.primary_candidate,
        "uncached_input_tokens",
    )
    primary_input = comparison(
        results, config.primary_baseline, config.primary_candidate, "input_tokens"
    )
    primary_commands = comparison(
        results,
        config.primary_baseline,
        config.primary_candidate,
        "command_executions",
    )
    primary_discovery = comparison(
        results,
        config.primary_baseline,
        config.primary_candidate,
        "discovery_commands",
    )
    primary_reads = comparison(
        results, config.primary_baseline, config.primary_candidate, "read_commands"
    )
    primary_verification = comparison(
        results,
        config.primary_baseline,
        config.primary_candidate,
        "verification_commands",
    )
    historical = (
        core.canonical_document_sha256(document)
        == HISTORICAL_PACKET_RESULT_SHA256
    )
    credible = historical or report_run_is_credible(document, results)
    preserved = quality_not_worse(
        results, config.primary_baseline, config.primary_candidate
    )
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
        f"- Run: `{document['run_id']}`",
        f"- Provider: `{document['provider']}`",
        f"- Model: `{document.get('model') or 'provider default'}`",
        f"- Reasoning effort: `{document.get('reasoning_effort') or 'provider default'}`",
        f"- Cases: {len(document['cases'])}",
        f"- Repetitions: {document['repetitions']}",
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
    for variant in config.variants:
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
        "Only pairs where both Semantic v1 and Packet v3 passed every acceptance group are included in the primary usage comparison.",
        "",
        "## Static artifacts",
        "",
        render_static(document["static"], config).rstrip(),
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
            f"{expected_primary_pairs} pairs, so this establishes a quality gain for this suite. "
            "The all-run token reduction is an observed result, not an isolated or model-independent token-saving claim."
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


REPORT_METRICS = (
    ("Input tokens", "input_tokens"),
    ("Uncached input", "uncached_input_tokens"),
    ("Output tokens", "output_tokens"),
    ("Wall time", "duration_seconds"),
    ("Tool calls", "tool_calls"),
    ("Shell commands", "command_executions"),
    ("Discovery command events", "discovery_commands"),
    ("Read command events", "read_commands"),
    ("Verification command events", "verification_commands"),
)

CAPSULE_REPORT_METRICS = (
    ("Total model tokens", "combined_tokens"),
    *REPORT_METRICS,
    ("Pre-edit discovery events", "pre_edit_discovery_commands"),
    ("Pre-edit read events", "pre_edit_read_commands"),
    ("Pre-edit verification events", "pre_edit_verification_commands"),
)


def primary_success_coverage(
    document: dict[str, Any],
    results: list[dict[str, Any]],
    config: ComparisonConfig,
) -> tuple[int, int]:
    """Return jointly successful primary pairs independently of telemetry."""

    cases = document.get("cases")
    repetitions = document.get("repetitions")
    if (
        not isinstance(cases, list)
        or any(not isinstance(case, str) for case in cases)
        or len(cases) != len(set(cases))
        or not isinstance(repetitions, int)
        or isinstance(repetitions, bool)
        or repetitions < 1
    ):
        return 0, 0
    pairs = paired_result_index(results)
    if pairs is None:
        return 0, len(cases) * repetitions
    successful = 0
    for case in cases:
        for repetition in range(1, repetitions + 1):
            pair = pairs.get((case, repetition), {})
            baseline = pair.get(config.primary_baseline, {})
            candidate = pair.get(config.primary_candidate, {})
            if (
                isinstance(baseline, dict)
                and isinstance(candidate, dict)
                and isinstance(baseline.get("grade"), dict)
                and isinstance(candidate.get("grade"), dict)
                and baseline["grade"].get("task_success") is True
                and candidate["grade"].get("task_success") is True
            ):
                successful += 1
    return successful, len(cases) * repetitions


def capsule_full_corpus(document: dict[str, Any]) -> bool:
    cases = document.get("cases")
    if not isinstance(cases, list) or document.get("full_corpus") is not True:
        return False
    try:
        corpus = core.discover_cases(cases_dir=CASES_DIR)
    except (OSError, RuntimeError, ValueError):
        return False
    return set(cases) == {case.id for case in corpus} and len(cases) == len(set(cases))


def provider_error_count(results: list[dict[str, Any]]) -> int:
    count = 0
    for result in results:
        provider = result.get("provider") if isinstance(result, dict) else None
        if (
            not isinstance(provider, dict)
            or provider.get("return_code") != 0
            or provider.get("event_errors") != []
        ):
            count += 1
    return count


def verification_error_count(results: list[dict[str, Any]]) -> int:
    count = 0
    for result in results:
        verification = result.get("verification") if isinstance(result, dict) else None
        if not isinstance(verification, dict) or verification.get("return_code") != 0:
            count += 1
    return count


def capsule_pre_edit_failures(
    results: list[dict[str, Any]],
    config: ComparisonConfig,
) -> tuple[int, int, int, int]:
    """Return successful run count plus pre-edit exploration violations."""

    successful = discovery = reads = verification = 0
    for result in results:
        if (
            not isinstance(result, dict)
            or result.get("variant") != config.primary_candidate
            or not isinstance(result.get("grade"), dict)
            or result["grade"].get("task_success") is not True
        ):
            continue
        successful += 1
        discovery_value = metric(result, "pre_edit_discovery_commands")
        read_value = metric(result, "pre_edit_read_commands")
        verification_value = metric(result, "pre_edit_verification_commands")
        if discovery_value != 0:
            discovery += 1
        if read_value != 0:
            reads += 1
        if verification_value != 0:
            verification += 1
    return successful, discovery, reads, verification


def capsule_pre_edit_telemetry_failures(
    results: list[dict[str, Any]],
    config: ComparisonConfig,
) -> tuple[int, int]:
    """Count successful candidate runs lacking a substantive-edit attestation.

    A generic ``file_change`` event is not evidence that implementation began:
    it could describe a scratch file or another irrelevant path.  The parser
    therefore supplies this status only after an event names a routed/target
    file.  Older or pathless event streams are deliberately claim-ineligible.
    """

    successful = unavailable = 0
    for result in results:
        if (
            not isinstance(result, dict)
            or result.get("variant") != config.primary_candidate
            or not isinstance(result.get("grade"), dict)
            or result["grade"].get("task_success") is not True
        ):
            continue
        successful += 1
        provider = result.get("provider")
        telemetry = (
            provider.get("pre_edit_telemetry")
            if isinstance(provider, dict)
            else None
        )
        if not current_routed_edit_telemetry(telemetry):
            unavailable += 1
    return successful, unavailable


def capsule_action_gate_coverage(
    results: list[dict[str, Any]],
    config: ComparisonConfig = MARKDOWN_CAPSULE_V6,
) -> tuple[int, int]:
    """Count candidate runs with both routed-edit telemetry and no gate error."""

    passed = total = 0
    for result in results:
        if not isinstance(result, dict) or result.get("variant") != config.primary_candidate:
            continue
        total += 1
        provider = result.get("provider")
        telemetry = (
            provider.get("pre_edit_telemetry")
            if isinstance(provider, dict)
            else None
        )
        error = result.get("error")
        if error is None:
            action_error = False
        elif (
            isinstance(error, dict)
            and set(error) == {"codes"}
            and isinstance(error["codes"], list)
        ):
            action_error = any(
                not isinstance(code, str)
                or code not in core.PUBLIC_ERROR_CODES
                or code in CAPSULE_ACTION_ERROR_CODES
                for code in error["codes"]
            )
        else:
            action_error = True
        action_protocol = bool(
            isinstance(provider, dict)
            and capsule_has_exact_tool_sequence(provider)
            and provider.get("attempt_count") == 1
            and provider.get("pre_edit_command_executions") == 0
            and provider.get("declared_verification_executions") == 1
            and provider.get("pre_edit_declared_verification_executions") == 0
            and provider.get("pre_edit_command_categories")
            == {"discovery": 0, "read": 0, "verify": 0}
        )
        if (
            current_routed_edit_telemetry(telemetry)
            and action_protocol
            and not action_error
        ):
            passed += 1
    return passed, total


LIFECYCLE_REPORT_METRICS = (
    ("Total model tokens", "combined_tokens"),
    ("Input tokens", "input_tokens"),
    ("Uncached input", "uncached_input_tokens"),
    ("Output tokens", "output_tokens"),
    ("Agent wall time", "duration_seconds"),
    ("Tool calls", "tool_calls"),
    ("Shell commands", "command_executions"),
)


def lifecycle_authoring_by_case(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    authoring = document.get("authoring")
    results = authoring.get("results") if isinstance(authoring, dict) else None
    if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
        return {}
    indexed = {item.get("case"): item for item in results}
    if len(indexed) != len(results) or any(not isinstance(case, str) for case in indexed):
        return {}
    return indexed


def lifecycle_authoring_metric(
    document: dict[str, Any],
    case: str,
    name: str,
) -> float | int | None:
    result = lifecycle_authoring_by_case(document).get(case)
    return metric(result, name) if result is not None else None


def lifecycle_authoring_total(document: dict[str, Any], name: str) -> float | None:
    indexed = lifecycle_authoring_by_case(document)
    cases = document.get("cases")
    if not isinstance(cases, list) or set(indexed) != set(cases):
        return None
    values = [lifecycle_authoring_metric(document, case, name) for case in cases]
    if any(value is None for value in values):
        return None
    return round(sum(float(value) for value in values if value is not None), 3)


def lifecycle_comparison(document: dict[str, Any], name: str) -> dict[str, Any]:
    """Compare one-use workflows, charging full authoring cost to Capsule."""

    config = document_comparison(document)
    pairs = paired_result_index(document.get("results", []))
    if config != MARKDOWN_CAPSULE_LIFECYCLE_V1 or pairs is None:
        return empty_comparison()
    by_case: dict[str, list[float]] = {}
    reductions: list[float] = []
    baseline_total = candidate_total = 0.0
    for (case, _), pair in pairs.items():
        baseline = pair.get(config.primary_baseline)
        candidate = pair.get(config.primary_candidate)
        authoring = lifecycle_authoring_metric(document, case, name)
        if baseline is None or candidate is None or authoring is None:
            continue
        left = metric(baseline, name)
        implementation = metric(candidate, name)
        if not left or implementation is None:
            continue
        right = float(authoring) + float(implementation)
        reduction = (float(left) - right) / float(left) * 100
        reductions.append(reduction)
        baseline_total += float(left)
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


def lifecycle_quality_dominates_every_pair(
    document: dict[str, Any],
) -> bool:
    config = document_comparison(document)
    pairs = paired_result_index(document.get("results", []))
    if config != MARKDOWN_CAPSULE_LIFECYCLE_V1 or pairs is None:
        return False
    cases = document.get("cases")
    repetitions = document.get("repetitions")
    if (
        not isinstance(cases, list)
        or not isinstance(repetitions, int)
        or isinstance(repetitions, bool)
        or repetitions < 1
    ):
        return False
    expected = len(cases) * repetitions
    if len(pairs) != expected:
        return False
    for pair in pairs.values():
        baseline = pair.get(config.primary_baseline, {}).get("grade")
        candidate = pair.get(config.primary_candidate, {}).get("grade")
        if not isinstance(baseline, dict) or not isinstance(candidate, dict):
            return False
        if (
            candidate.get("task_success") is not True
            or candidate.get("passed") != candidate.get("total")
            or candidate.get("acceptance_passed") != candidate.get("acceptance_total")
            or candidate.get("passed", -1) < baseline.get("passed", 0)
            or candidate.get("acceptance_passed", -1)
            < baseline.get("acceptance_passed", 0)
        ):
            return False
    return True


def lifecycle_claim_limitations(
    document: dict[str, Any],
    results: list[dict[str, Any]],
    credible: bool,
    config: ComparisonConfig,
) -> list[str]:
    limitations: list[str] = []
    cases = document.get("cases")
    repetitions = document.get("repetitions")
    expected_pairs = (
        len(cases) * repetitions
        if isinstance(cases, list)
        and isinstance(repetitions, int)
        and not isinstance(repetitions, bool)
        and repetitions >= 1
        else 0
    )
    primary = lifecycle_comparison(document, "combined_tokens")
    if not credible:
        limitations.append("the full authoring and implementation provenance is not credible")
    if not lifecycle_quality_dominates_every_pair(document):
        limitations.append("Capsule v6 did not pass every test without losing pairwise quality")
    if primary["pairs"] != expected_pairs:
        limitations.append(
            f"only {primary['pairs']}/{expected_pairs} lifecycle pairs had complete token telemetry"
        )
    elif primary["wins"] != expected_pairs:
        limitations.append(
            f"Capsule v6 used fewer one-use lifecycle tokens in only "
            f"{primary['wins']}/{expected_pairs} pairs"
        )
    interval = primary["fixture_cluster_bootstrap_95_ci"]
    if not interval or interval[0] <= 0:
        limitations.append("the one-use lifecycle token 95% fixture CI is not above 0%")
    authoring_tokens = lifecycle_authoring_total(document, "combined_tokens")
    if authoring_tokens is None or authoring_tokens <= 0:
        limitations.append("Capsule authoring token cost is missing")
    aggregates = {variant: aggregate(results, variant) for variant in config.variants}
    candidate_measured = aggregates[config.primary_candidate]["total_combined_tokens"]
    if authoring_tokens is not None:
        candidate_measured += authoring_tokens
    baseline_measured = aggregates[config.primary_baseline]["total_combined_tokens"]
    if candidate_measured >= baseline_measured:
        limitations.append("Capsule did not reduce measured three-use lifecycle tokens")
    action_passed, action_total = capsule_action_gate_coverage(results, config)
    if action_total != expected_pairs or action_passed != action_total:
        limitations.append(
            f"the routed action gate passed only {action_passed}/{action_total} Capsule runs"
        )
    if provider_error_count(results):
        limitations.append("implementation provider errors occurred")
    if verification_error_count(results):
        limitations.append("implementation verification errors occurred")
    if any(result.get("error") is not None for result in results):
        limitations.append("recorded implementation errors occurred")
    successful, discovery, reads, verification = capsule_pre_edit_failures(results, config)
    if discovery or reads or verification:
        limitations.append(
            "Capsule violated its zero-command pre-edit gate "
            f"({discovery}/{successful} discovery, {reads}/{successful} reads, "
            f"{verification}/{successful} verification)"
        )
    successful, telemetry_failures = capsule_pre_edit_telemetry_failures(results, config)
    if telemetry_failures:
        limitations.append(
            f"Capsule lacked routed-edit telemetry in {telemetry_failures}/{successful} successful runs"
        )
    return limitations


def input_cache_price_advantage_range(
    aggregates: dict[str, dict[str, Any]],
    config: ComparisonConfig,
) -> tuple[float, float] | None:
    """Return cached/uncached price ratios where Capsule input is cheaper.

    Output-token savings are intentionally excluded, so this is a conservative
    input-only comparison. The cost difference is linear over ratios [0, 1].
    """

    baseline = aggregates[config.primary_baseline]
    candidate = aggregates[config.primary_candidate]
    baseline_uncached = baseline["total_uncached_input_tokens"]
    candidate_uncached = candidate["total_uncached_input_tokens"]
    baseline_cached = max(
        baseline["total_input_tokens"] - baseline_uncached,
        0,
    )
    candidate_cached = max(
        candidate["total_input_tokens"] - candidate_uncached,
        0,
    )
    delta_at_zero = candidate_uncached - baseline_uncached
    delta_at_one = (
        candidate_uncached
        + candidate_cached
        - baseline_uncached
        - baseline_cached
    )
    if delta_at_zero <= 0 and delta_at_one <= 0:
        return (0.0, 1.0)
    if delta_at_zero > 0 and delta_at_one > 0:
        return None
    slope = delta_at_one - delta_at_zero
    if slope == 0:
        return None
    crossing = min(max(-delta_at_zero / slope, 0.0), 1.0)
    if delta_at_zero <= 0:
        return (0.0, crossing)
    return (crossing, 1.0)


def capsule_claim_limitations(
    document: dict[str, Any],
    results: list[dict[str, Any]],
    aggregates: dict[str, dict[str, Any]],
    primary_combined: dict[str, Any],
    credible: bool,
    preserved: bool,
    config: ComparisonConfig = MARKDOWN_CAPSULE_V6,
) -> list[str]:
    """Enumerate every failed measured-corpus claim predicate."""

    if config == MARKDOWN_CAPSULE_LIFECYCLE_V1:
        return lifecycle_claim_limitations(document, results, credible, config)

    limitations: list[str] = []
    baseline_label = arm_label(config, config.primary_baseline)
    candidate_label = arm_label(config, config.primary_candidate)
    successful_pairs, expected_pairs = primary_success_coverage(document, results, config)
    if expected_pairs == 0 or successful_pairs != expected_pairs:
        limitations.append(
            f"only {successful_pairs}/{expected_pairs} "
            f"{baseline_label}/{candidate_label} pairs "
            "were jointly successful"
        )
    if primary_combined["pairs"] != expected_pairs:
        limitations.append(
            f"only {primary_combined['pairs']}/{expected_pairs} pairs had complete "
            "joint-success token telemetry"
        )
    if not preserved:
        limitations.append(
            f"{candidate_label} did not preserve task/test success for every fixture"
        )

    provider_errors = provider_error_count(results)
    if provider_errors:
        limitations.append(f"provider errors occurred in {provider_errors} run(s)")
    verification_errors = verification_error_count(results)
    if verification_errors:
        limitations.append(
            f"verification errors occurred in {verification_errors} run(s)"
        )
    run_errors = sum(
        result.get("error") is not None
        for result in results
        if isinstance(result, dict)
    )
    if run_errors:
        limitations.append(f"recorded run errors occurred in {run_errors} run(s)")
    if document.get("provider") == "mock":
        limitations.append("mock provider runs are non-publishable")
    if not capsule_full_corpus(document):
        limitations.append("the result does not cover the full fixture corpus")
    repetitions = document.get("repetitions")
    if not isinstance(repetitions, int) or isinstance(repetitions, bool) or repetitions < 3:
        limitations.append("the result has fewer than three repetitions")
    if not credible and not provider_errors and not verification_errors and not run_errors:
        limitations.append(
            "the run lacks complete publishable provenance or provider telemetry"
        )

    action_passed, action_total = capsule_action_gate_coverage(results, config)
    if action_total == 0 or action_passed != action_total:
        limitations.append(
            f"the routed action gate passed only {action_passed}/{action_total} "
            "Capsule v6 runs"
        )

    token_ci = primary_combined["fixture_cluster_bootstrap_95_ci"]
    if not token_ci:
        limitations.append("no total-token fixture-cluster confidence interval is available")
    elif token_ci[0] <= 0:
        limitations.append(
            "the total-token fixture-cluster 95% CI lower bound "
            f"is {token_ci[0]:.3f}%, not greater than 0%"
        )

    baseline = aggregates[config.primary_baseline]["total_combined_tokens"]
    candidate = aggregates[config.primary_candidate]["total_combined_tokens"]
    if candidate >= baseline:
        limitations.append(
            "Capsule v6 did not reduce total model tokens across all runs "
            f"({candidate} versus {baseline})"
        )
    (
        successful_runs,
        discovery_failures,
        read_failures,
        verification_failures,
    ) = capsule_pre_edit_failures(results, config)
    if discovery_failures or read_failures or verification_failures:
        limitations.append(
            "Capsule v6 violated its classified pre-edit command budget in successful "
            f"runs ({discovery_failures}/{successful_runs} discovery, "
            f"{read_failures}/{successful_runs} over-budget read, "
            f"{verification_failures}/{successful_runs} verification)"
        )
    successful_runs, telemetry_failures = capsule_pre_edit_telemetry_failures(
        results,
        config,
    )
    if telemetry_failures:
        limitations.append(
            "Capsule v6 pre-edit classification could not verify every routed edit "
            f"in {telemetry_failures}/{successful_runs} successful run(s)"
        )
    return limitations


def _lifecycle_value(name: str, value: float | int | None) -> str:
    if value is None:
        return "n/a"
    if name == "duration_seconds":
        return f"{float(value):.3f}s"
    return str(int(value)) if float(value).is_integer() else f"{float(value):.3f}"


def capsule_lifecycle_report(document: dict[str, Any]) -> str:
    config = MARKDOWN_CAPSULE_LIFECYCLE_V1
    results = document["results"]
    aggregates = {variant: aggregate(results, variant) for variant in config.variants}
    comparisons = {
        name: lifecycle_comparison(document, name)
        for _, name in LIFECYCLE_REPORT_METRICS
    }
    primary = comparisons["combined_tokens"]
    credible = capsule_report_is_credible(document, results)
    limitations = lifecycle_claim_limitations(document, results, credible, config)
    repetitions = document["repetitions"]
    expected_pairs = len(document["cases"]) * repetitions
    authoring = document["authoring"]
    authored = sum(
        result.get("error") is None and result.get("selected_attempt") == 1
        for result in authoring["results"]
    )
    action_passed, action_total = capsule_action_gate_coverage(results, config)
    successful_pairs, _ = primary_success_coverage(document, results, config)
    authoring_tokens = lifecycle_authoring_total(document, "combined_tokens") or 0
    baseline_impl_tokens = aggregates["markdown"]["total_combined_tokens"]
    capsule_impl_tokens = aggregates["capsule"]["total_combined_tokens"]
    measured_capsule_tokens = capsule_impl_tokens + authoring_tokens
    measured_reduction = (
        core.compression_percent(
            baseline_impl_tokens,
            measured_capsule_tokens,
        )
        if baseline_impl_tokens
        else None
    )
    baseline_per_corpus = baseline_impl_tokens / repetitions
    capsule_per_corpus = capsule_impl_tokens / repetitions
    break_even = lifecycle_benchmark.break_even_reuses(
        authoring_tokens,
        baseline_per_corpus,
        capsule_per_corpus,
    )
    interval = primary["fixture_cluster_bootstrap_95_ci"]
    interval_text = (
        f"[{interval[0]:.3f}%, {interval[1]:.3f}%]" if interval else "n/a"
    )
    one_use_reduction = core.compression_percent(
        primary["baseline_total"],
        primary["candidate_total"],
    )
    one_use_text = f"{one_use_reduction:.2f}% fewer" if primary["pairs"] else "n/a"
    median_text = (
        f"{primary['median_reduction_percent']:.2f}%"
        if primary["median_reduction_percent"] is not None
        else "n/a"
    )
    reuse_word = "time" if repetitions == 1 else "times"
    measured_text = (
        f"{abs(measured_reduction):.2f}% "
        f"{'fewer' if measured_reduction >= 0 else 'more'}"
        if measured_reduction is not None
        else "n/a"
    )
    break_even_text = (
        "none"
        if break_even is None
        else f"{break_even} full-corpus "
        f"{'use' if break_even == 1 else 'uses'}"
    )
    lines = [
        "# Ordinary Markdown vs Capsule v6 Full-Lifecycle Benchmark",
        "",
        f"- Run: `{document['run_id']}`",
        f"- Provider/model: `{document['provider']}` / `{document.get('model') or 'provider default'}`",
        f"- Reasoning effort: `{document.get('reasoning_effort') or 'provider default'}`",
        f"- Cases: {len(document['cases'])}",
        f"- Implementation repetitions: {repetitions}",
        "- Authoring attempts: exactly one per fixture",
        f"- Claim protocol: `{document.get('claim_protocol', 'missing')}`",
        f"- Preregistration SHA-256: `{document.get('protocol_sha256', 'missing')}`",
        "- Markdown baseline: source task used directly, with zero preparation model calls",
        "- Capsule workflow: source task -> skill authoring call -> deterministic sealed context -> implementation",
        "",
    ]
    if limitations:
        lines.extend([
            "> Non-publishable run. The preregistered full-lifecycle dominance gate did not pass.",
            "",
        ])
    lines.extend([
        "## Quality",
        "",
        f"Authored and compiled Capsule handoffs: **{authored}/{len(authoring['results'])}**.  ",
        f"Capsule routed action gate: **{action_passed}/{action_total}**.",
        "",
        "| Workflow | Successful implementations | Tests | Acceptance |",
        "|---|---:|---:|---:|",
    ])
    for variant in config.variants:
        item = aggregates[variant]
        lines.append(
            f"| {arm_label(config, variant)} | "
            f"{round(item['task_success_rate'] * item['runs'])}/{item['runs']} | "
            f"{item['test_pass_rate'] * 100:.2f}% | "
            f"{item['acceptance_pass_rate'] * 100:.2f}% |"
        )
    lines.extend([
        "",
        "## Token bill",
        "",
        "Authoring is charged only to Capsule. Markdown receives the complete source task directly, which favors the baseline.",
        "",
        "| Stage | Ordinary Markdown | Capsule v6 | Capsule delta |",
        "|---|---:|---:|---:|",
        f"| Authoring ({len(document['cases'])} artifacts) | 0 | {int(authoring_tokens)} | n/a |",
        f"| Implementation ({expected_pairs} runs/arm) | {int(baseline_impl_tokens)} | {int(capsule_impl_tokens)} | {percent_delta(baseline_impl_tokens, capsule_impl_tokens)} |",
        f"| Measured workload: each artifact reused {repetitions} {reuse_word} | {int(baseline_impl_tokens)} | {int(measured_capsule_tokens)} | {percent_delta(baseline_impl_tokens, measured_capsule_tokens)} |",
        "",
        "## One-use lifecycle",
        "",
        "Every Capsule pair below includes the full authoring cost for that fixture plus one implementation. Failed Markdown implementations remain in this primary comparison.",
        "",
        "| Metric | Markdown average/task | Capsule average/task | Capsule reduction | Pair wins | 95% fixture CI |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for label, name in LIFECYCLE_REPORT_METRICS:
        summary = comparisons[name]
        pairs = summary["pairs"]
        left = summary["baseline_total"] / pairs if pairs else None
        right = summary["candidate_total"] / pairs if pairs else None
        ci = summary["fixture_cluster_bootstrap_95_ci"]
        ci_text = f"[{ci[0]:.3f}%, {ci[1]:.3f}%]" if ci else "n/a"
        reduction = (
            f"{core.compression_percent(summary['baseline_total'], summary['candidate_total']):.2f}%"
            if pairs else "n/a"
        )
        lines.append(
            f"| {label} | {_lifecycle_value(name, left)} | "
            f"{_lifecycle_value(name, right)} | {reduction} | "
            f"{summary['wins']}/{pairs} | {ci_text} |"
        )
    lines.extend([
        "",
        f"One-use total-token result: **{one_use_text}** tokens; "
        f"median reduction **{median_text}**; "
        f"fixture-cluster 95% CI **{interval_text}**.",
        "",
        f"Strict equal-success coverage: **{successful_pairs}/{expected_pairs}** pairs. "
        "This is secondary; the primary outcome comparison retains failed baseline attempts.",
        "",
        "## Reuse",
        "",
        "| Full-corpus uses | Markdown tokens | Capsule tokens including authoring | Capsule delta |",
        "|---:|---:|---:|---:|",
    ])
    for reuses in (1, 3, 5, 10):
        baseline_total = baseline_per_corpus * reuses
        capsule_total = authoring_tokens + capsule_per_corpus * reuses
        lines.append(
            f"| {reuses} | {int(round(baseline_total))} | "
            f"{int(round(capsule_total))} | {percent_delta(baseline_total, capsule_total)} |"
        )
    lines.extend([
        "",
        f"Measured {repetitions}-use workload: **{measured_text}** total model tokens. "
        f"Break-even: **{break_even_text}**.",
        "",
        "## Static size",
        "",
        render_static(document["static"], config).rstrip(),
        "",
        "## Verdict",
        "",
    ])
    if limitations:
        lines.append("Claim rejected: " + "; ".join(limitations) + ".")
    else:
        lines.append(
            "The preregistered gate passed: Capsule v6 completed every implementation, "
            "never lost pairwise quality, and used fewer total one-use lifecycle tokens "
            "in every pair after paying its complete measured authoring bill."
        )
    lines.extend([
        "",
        "## Limits",
        "",
        "- Three synthetic multi-file Python fixtures can validate this workflow, not establish a universal model-independent claim.",
        f"- There is one authoring observation per fixture; implementation is repeated {repetitions} {reuse_word} per arm.",
        "- Provider token and agent-duration telemetry are self-reported. Deterministic Capsule compilation uses no model tokens and is excluded from agent wall time.",
        "- Capsule static bytes can exceed Markdown because it embeds routed source. This benchmark tests total task execution, not file compression.",
        "- Hidden tests remain outside authoring and implementation workspaces. Visible checks are restored from immutable fixtures.",
        "- Results apply only to the recorded model, reasoning effort, fixtures, and cache behavior.",
        "",
    ])
    return "\n".join(lines)


def capsule_v6_report(document: dict[str, Any]) -> str:
    config = document_comparison(document)
    if not is_capsule_comparison(config):
        raise ValueError(f"result is not a Capsule v6 comparison: {config.name}")
    if config == MARKDOWN_CAPSULE_LIFECYCLE_V1:
        return capsule_lifecycle_report(document)
    baseline_label = arm_label(config, config.primary_baseline)
    candidate_label = arm_label(config, config.primary_candidate)
    candidate_delta_label = (
        "Capsule delta"
        if config == CAPSULE_V6
        else f"{candidate_label} delta"
    )
    results = document["results"]
    aggregates = {variant: aggregate(results, variant) for variant in config.variants}
    paired = {
        name: comparison(
            results,
            config.primary_baseline,
            config.primary_candidate,
            name,
        )
        for _, name in CAPSULE_REPORT_METRICS
    }
    primary_combined = paired["combined_tokens"]
    primary_uncached = paired["uncached_input_tokens"]
    credible = capsule_report_is_credible(document, results)
    try:
        preserved = quality_not_worse(
            results, config.primary_baseline, config.primary_candidate
        )
    except (KeyError, TypeError):
        preserved = False
    successful_pairs, expected_pairs = primary_success_coverage(document, results, config)
    limitations = capsule_claim_limitations(
        document,
        results,
        aggregates,
        primary_combined,
        credible,
        preserved,
        config,
    )
    lines = [
        (
            "# Ordinary Markdown vs Capsule v6 Benchmark"
            if config == MARKDOWN_CAPSULE_V6
            else "# Semantic Context Capsule Benchmark"
        ),
        "",
        f"- Run: `{document['run_id']}`",
        f"- Provider: `{document['provider']}`",
        f"- Model: `{document.get('model') or 'provider default'}`",
        f"- Reasoning effort: `{document.get('reasoning_effort') or 'provider default'}`",
        f"- Cases: {len(document['cases'])}",
        f"- Repetitions: {document['repetitions']}",
    ]
    if config == CAPSULE_V6:
        lines.append("- Packet version: 3")
    lines.extend([
        "- Capsule version: 6",
        f"- Execution profile: `{document.get('execution_profile', 'unrecorded')}`",
        f"- Telemetry attestation: `{document.get('telemetry_attestation', 'none')}`",
    ])
    if config == MARKDOWN_CAPSULE_V6:
        lines.append(
            "- Baseline: ordinary Markdown task description without embedded repository source"
        )
    lines.append("")
    if limitations:
        lines.extend([
            "> Non-publishable experimental smoke run. Publishable evidence requires "
            "a named real model, complete single-attempt action telemetry and "
            "provenance, preserved quality, the full suite, three or more "
            "repetitions, and a positive per-fixture token interval.",
            "",
        ])
    authoring_limit = (
        "- Capsule v6 embeds a prebuilt execution plan and routed source snapshot; "
        "this execution comparison excludes Capsule authoring cost and reuse break-even."
        if config == MARKDOWN_CAPSULE_V6
        else "- Capsule v6 embeds Packet v3 and its routed source snapshot; "
        "this comparison excludes authoring cost and reuse break-even."
    )
    lines.extend([
        "## Quality and usage",
        "",
        "| Arm | Runs | Task success | Tests | Total tokens | Input | Uncached input | Output | Wall time | Tool calls | Commands | Discovery | Reads | Pre-edit discovery | Pre-edit reads | Pre-edit verify | Verify |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for variant in config.variants:
        item = aggregates[variant]
        lines.append(
            f"| {arm_label(config, variant)} | {item['runs']} | "
            f"{item['task_success_rate'] * 100:.2f}% | "
            f"{item['test_pass_rate'] * 100:.2f}% | "
            f"{item['total_combined_tokens']} | {item['total_input_tokens']} | "
            f"{item['total_uncached_input_tokens']} | {item['total_output_tokens']} | "
            f"{item['total_duration_seconds']}s | "
            f"{item['total_tool_calls']} | {item['total_command_executions']} | "
            f"{item['total_discovery_commands']} | {item['total_read_commands']} | "
            f"{item['total_pre_edit_discovery_commands']} | "
            f"{item['total_pre_edit_read_commands']} | "
            f"{item['total_pre_edit_verification_commands']} | "
            f"{item['total_verification_commands']} |"
        )
    action_passed, action_total = capsule_action_gate_coverage(results, config)
    lines.extend([
        "",
        f"Routed action gate: **{action_passed}/{action_total}** Capsule v6 runs.",
        "",
        f"## Primary comparison: {candidate_label} vs {baseline_label}",
        "",
        f"Only jointly successful {baseline_label}/{candidate_label} pairs contribute "
        "to paired totals, medians, and confidence intervals.",
        "",
        f"| Metric | {baseline_label} paired total | {candidate_label} paired total | "
        f"{candidate_delta_label} | Paired median reduction | 95% fixture CI |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for label, name in CAPSULE_REPORT_METRICS:
        summary = paired[name]
        left = summary["baseline_total"]
        right = summary["candidate_total"]
        ci = summary["fixture_cluster_bootstrap_95_ci"]
        ci_text = f"[{ci[0]:.3f}%, {ci[1]:.3f}%]" if ci else "n/a"
        median = summary["median_reduction_percent"]
        median_text = f"{median}%" if median is not None else "n/a"
        lines.append(
            f"| {label} | {left} | {right} | {percent_delta(left, right)} | "
            f"{median_text} | {ci_text} |"
        )
    lines.extend([
        "",
        f"Primary comparison coverage: **{successful_pairs}/{expected_pairs}** jointly successful pairs; "
        f"**{primary_combined['pairs']}/{expected_pairs}** have paired total-token telemetry; "
        f"**{primary_uncached['pairs']}/{expected_pairs}** have paired uncached-input telemetry.",
        "",
        "## All-run deltas",
        "",
        f"| Metric | {baseline_label} total | {candidate_label} total | "
        f"{candidate_delta_label} |",
        "|---|---:|---:|---:|",
    ])
    for label, name in CAPSULE_REPORT_METRICS:
        left = aggregates[config.primary_baseline][f"total_{name}"]
        right = aggregates[config.primary_candidate][f"total_{name}"]
        lines.append(
            f"| {label} | {left} | {right} | {percent_delta(left, right)} |"
        )
    cache_advantage = input_cache_price_advantage_range(aggregates, config)
    lines.extend([
        "",
        "## Input cache break-even",
        "",
    ])
    if cache_advantage is None:
        lines.append(
            "Capsule v6 has no input-only cost break-even for cached-input prices "
            "between 0% and 100% of uncached input."
        )
    elif cache_advantage == (0.0, 1.0):
        lines.append(
            "Capsule v6 input is cheaper for every cached-input unit price from 0% "
            "to 100% of uncached input. Output-token savings are excluded from this "
            "conservative comparison."
        )
    elif cache_advantage[0] == 0:
        lines.append(
            "Capsule v6 input is cheaper while cached input costs at most "
            f"**{cache_advantage[1] * 100:.2f}%** of uncached input. Output-token "
            "savings are excluded from this conservative comparison."
        )
    else:
        lines.append(
            "Capsule v6 input is cheaper once cached input costs at least "
            f"**{cache_advantage[0] * 100:.2f}%** of uncached input. Output-token "
            "savings are excluded from this conservative comparison."
        )
    lines.extend([
        "",
        "## Capsule static size",
        "",
        render_static(document["static"], config).rstrip(),
        "",
        "## Verdict",
        "",
    ])
    if not limitations:
        lines.append(
            f"This suite supports a measured token-saving result for {candidate_label} "
            f"versus {baseline_label} on this corpus while preserving behavior in every "
            "fixture. Provider telemetry remains self-reported; this is not a "
            "universal guarantee."
        )
    else:
        observed_baseline = aggregates[config.primary_baseline][
            "total_combined_tokens"
        ]
        observed_candidate = aggregates[config.primary_candidate][
            "total_combined_tokens"
        ]
        observed_reduction = core.compression_percent(
            observed_baseline,
            observed_candidate,
        )
        observed_direction = "fewer" if observed_reduction >= 0 else "more"
        lines.append(
            "Observed full-run result: Capsule v6 used "
            f"**{abs(observed_reduction):.2f}% {observed_direction}** total model "
            f"tokens versus {baseline_label}. The suite "
            "cannot establish a publishable token-saving result across every fixture because "
            + "; ".join(limitations)
            + "."
        )
    lines.extend([
        "",
        "## Limits",
        "",
        "- Three synthetic Python fixtures are enough to reject a weak design, not enough for a broad product claim.",
        "- Provider token telemetry and grades are self-reported and are not independently attested; the Git commit is the publication trust boundary.",
        "- Command classification is directional telemetry, not a filesystem-access audit. One Codex event may contain multiple or indirectly scripted operations.",
        "- Pre-edit classification is claim-eligible only when file-change event paths confirm a routed or target edit; missing or pathless events fail closed.",
        "- The reported fixture-cluster interval bootstraps per-fixture median reductions; it is not an interval for the displayed all-run aggregate reduction.",
        authoring_limit,
        "- Static Capsule size excludes the host prompt; measured model usage includes the complete rendered prompt.",
        "- Hidden tests and hidden expected outputs stay outside the solution process; visible smoke assertions are restored from immutable fixtures for every arm.",
        "- Results apply only to the recorded model, reasoning effort, repository shapes, and cache behavior.",
        "",
    ])
    return "\n".join(lines)


def report(document: dict[str, Any]) -> str:
    config = document_comparison(document)
    if document.get("kind") != config.kind:
        raise ValueError(f"result is not a {config.name} comparison")
    if config == PACKET_V3:
        return packet_v3_report(document)
    return capsule_v6_report(document)


def validate_capsule_release(
    document: dict[str, Any],
    rendered_report: bytes,
) -> list[str]:
    """Validate a newly published current-schema Capsule result/report pair."""

    errors: list[str] = []
    results = document.get("results")
    try:
        config = document_comparison(document)
    except ValueError:
        config = None
    if (
        config != MARKDOWN_CAPSULE_LIFECYCLE_V1
        or not isinstance(results, list)
        or not capsule_report_is_credible(document, results)
    ):
        errors.append("Capsule result does not satisfy current credibility gates")
        return errors
    aggregates = {
        variant: aggregate(results, variant) for variant in config.variants
    }
    primary_combined = comparison(
        results,
        config.primary_baseline,
        config.primary_candidate,
        "combined_tokens",
    )
    try:
        preserved = quality_not_worse(
            results,
            config.primary_baseline,
            config.primary_candidate,
        )
    except (KeyError, TypeError):
        preserved = False
    limitations = capsule_claim_limitations(
        document,
        results,
        aggregates,
        primary_combined,
        True,
        preserved,
        config,
    )
    if limitations:
        errors.append(
            "Capsule result does not satisfy release claim gates: "
            + "; ".join(limitations)
        )
        return errors
    expected = report(document).encode("utf-8")
    if rendered_report != expected:
        errors.append("Capsule report is not the exact rendering of its result")
    return errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate_parser = commands.add_parser("validate")
    validate_parser.add_argument("--case", action="append", default=[])
    static_parser = commands.add_parser("static")
    static_parser.add_argument("--case", action="append", default=[])
    static_parser.add_argument(
        "--comparison",
        choices=list(COMPARISONS),
        default="packet-v3",
    )
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
        "--comparison",
        choices=list(COMPARISONS),
        default="packet-v3",
        help="reportable comparison; default retains the Packet v3 three-arm run",
    )
    run_parser.add_argument(
        "--variant",
        action="append",
        choices=list(ALL_VARIANTS),
        default=[],
        help="development-only arm filter; omit for a complete reportable comparison",
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
            config = comparison_config(args.comparison)
            rows = static_rows(
                cases,
                core.load_token_encoder(args.token_encoding),
                config,
            )
            print(
                json.dumps(rows, indent=2)
                if args.json
                else render_static(rows, config),
                end="",
            )
            return 0
        if args.command == "run":
            if args.repetitions < 1:
                raise ValueError("repetitions must be at least 1")
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
