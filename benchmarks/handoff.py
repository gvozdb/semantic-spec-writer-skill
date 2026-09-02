#!/usr/bin/env python3
"""Three-arm benchmark for repository-grounded execution packets."""

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
VARIANTS = {
    "markdown": "baseline.md",
    "semantic": "semantic.spec.ctx",
    "packet": "packet.spec.ctx",
}
CAPSULE_SCRIPT = ROOT / "skills" / "semantic-spec-writer" / "scripts" / "context_capsule.py"
CAPSULE_CODE_PATHS = (
    "benchmarks/benchmark.py",
    "benchmarks/grader.py",
    "benchmarks/handoff.py",
    "benchmarks/solution_runtime.py",
    "benchmarks/solution_worker.py",
    "benchmarks/validate_capsule_release.py",
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
CAPSULE_V5 = ComparisonConfig(
    "capsule-v5",
    "semantic-context-capsule-comparison",
    (("packet_version", 3), ("capsule_version", 5)),
    ("packet", "capsule"),
    "packet",
    "capsule",
    (("packet", "Packet v3"), ("capsule", "Capsule v5")),
)
COMPARISONS = {config.name: config for config in (PACKET_V3, CAPSULE_V5)}
ALL_VARIANTS = tuple((*VARIANTS, "capsule"))
CAPSULE_ACTION_ERROR_CODES = frozenset({
    "capsule_incomplete_routed_edits",
    "capsule_no_routed_edit",
    "capsule_pre_edit_verification",
    "capsule_routed_edit_attestation_failed",
})
sys.path.insert(0, str(BENCHMARKS))
import benchmark as core  # noqa: E402


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


def context_capsule_module() -> Any:
    """Load Capsule v5 by path so the benchmark has no fixture-side artifact."""

    module_name = "_semantic_spec_context_capsule"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, CAPSULE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load Capsule v5 builder: {CAPSULE_SCRIPT}")
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

    _, _, _, sources, _ = capsule_module._parse_capsule(capsule)
    hashes: dict[str, str] = {}
    for descriptor, payload in sources:
        capsule_module._validate_source_descriptor(descriptor)
        path = descriptor["path"]
        if path in hashes:
            raise RuntimeError(f"Capsule v5 repeats a routed source frame: {path}")
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
        or checked.get("version") != 5
        or checked.get("packet_bound") is not True
        or not isinstance(checked.get("capsule"), dict)
        or not isinstance(checked["capsule"].get("seal_sha256"), str)
        or checked.get("packet_sha256") != core.sha256_bytes(packet_bytes)
        or not isinstance(checked.get("route_sha256"), str)
    ):
        detail = "; ".join(str(item) for item in errors) or "invalid capsule metadata"
        raise RuntimeError(f"{case.id}: Capsule v5 validation failed: {detail}")
    _, _, _, sources, seal = capsule_module._parse_capsule(capsule)

    # Recompute every frame directly from the captured workspace bytes.  This
    # independently binds full-file and ranged routes, descriptor metadata, and
    # route indices.  Create routes intentionally produce no frame; the trusted
    # checker above also proved they are absent in this exact materialization.
    normalized_packet = packet_text.replace("\r\n", "\n").replace("\r", "\n")
    targets = capsule_module.packet_checker.parse_routes(normalized_packet)
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
            (capsule_module._source_descriptor(index, target, payload), payload)
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
        raise RuntimeError(f"{case.id}: Capsule v5 is not UTF-8") from exc
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
        return [f"{case.id}: Capsule v5 validation failed: {exc}"]
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
        if config == CAPSULE_V5:
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
    gate = (
        context_capsule_module().CAPSULE_CONTROL
        if config == CAPSULE_V5 and variant == config.primary_candidate
        else None
    )
    return core.benchmark_prompt(specification, execution_gate=gate)


def case_snapshot(
    case: core.BenchmarkCase,
    comparison: ComparisonConfig | str | None = None,
    *,
    fixture_snapshot: core.FixtureTreeSnapshot | None = None,
    starter_snapshot: core.FixtureTreeSnapshot | None = None,
    grading_snapshot: core.GradingSnapshot | None = None,
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

    packet_bytes = captured_artifacts["packet"].encode("utf-8")
    capsule_text, capsule_metadata = capsule_artifact(
        case,
        starter_snapshot=starter_snapshot,
        packet_bytes=packet_bytes,
    )
    artifacts = {**captured_artifacts, "capsule": capsule_text}
    return {
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


def capsule_snapshot_artifacts(snapshot: Any) -> dict[str, str]:
    if not isinstance(snapshot, dict):
        raise ValueError("Capsule fixture snapshot must be an object")
    artifacts = snapshot.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(CAPSULE_V5.variants):
        raise ValueError("Capsule fixture snapshot lacks exact artifact bytes")
    return {
        variant: core.attested_text(
            artifacts[variant],
            f"{variant} handoff artifact",
        )
        for variant in CAPSULE_V5.variants
    }


def capsule_static_rows_from_snapshots(
    cases: list[core.BenchmarkCase],
    snapshots: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "case": case.id,
            "variants": {
                variant: core.text_metrics(text)
                for variant, text in capsule_snapshot_artifacts(
                    snapshots[case.id]
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
    if case_snapshot(case, comparison) != expected:
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
    if config == CAPSULE_V5:
        capsule = snapshot.get("capsule")
        if not isinstance(capsule, dict):
            raise RuntimeError("Capsule v5 snapshot lacks capsule provenance")
        provenance.update({
            "capsule_sha256": capsule["capsule_sha256"],
            "capsule_seal_sha256": capsule["capsule_seal_sha256"],
            "trusted_packet_sha256": capsule["packet_sha256"],
            "route_sha256": capsule["route_sha256"],
            "source_hashes": capsule["source_hashes"],
        })
        expected_prompt = snapshot.get("prompts", {}).get(variant)
        if provenance["prompt_sha256"] != expected_prompt:
            raise RuntimeError("Capsule v5 prompt does not match its snapshot")
    return provenance


def routed_target_paths(
    case: core.BenchmarkCase,
    packet_text: str | None = None,
) -> tuple[str, ...]:
    """Return the Packet routes that make a file change substantive.

    Capsule pre-edit telemetry is meaningful only after the provider changes a
    routed file (or the declared implementation entrypoint).  Keep this
    runtime-only: route paths are deliberately not copied into the result
    document, where they would be unnecessary provenance prose.
    """

    paths = {str(case.manifest["entrypoint"])}
    if packet_text is None:
        packet_text = artifact_path(case, "packet").read_text(encoding="utf-8")
    for target in context_capsule_module().packet_checker.parse_routes(packet_text):
        relative_path = getattr(target, "relative_path", None)
        if isinstance(relative_path, str) and relative_path:
            paths.add(relative_path)
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
        target
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
        )
        snapshots[case.id] = snapshot
        starter_snapshots[case.id] = starter
        grading_snapshots[case.id] = grading
        captured_artifacts[case.id] = (
            capsule_snapshot_artifacts(snapshot)
            if config == CAPSULE_V5
            else {
                variant: core.fixture_snapshot_file(
                    fixture,
                    VARIANTS[variant],
                ).data.decode("utf-8")
                for variant in config.variants
            }
        )
    if config == CAPSULE_V5 and code_revision is None:
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
        "schema_version": 2 if config == CAPSULE_V5 else 1,
        "kind": config.kind,
        "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "created_at": datetime.now(UTC).isoformat(),
        "provider": args.provider,
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
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
            capsule_static_rows_from_snapshots(cases, snapshots)
            if config == CAPSULE_V5
            else static_rows_from_artifacts(cases, captured_artifacts, config)
        ),
        "results": [],
    }, starter_snapshots, grading_snapshots, captured_artifacts)
    if config == CAPSULE_V5:
        document["code_revision"] = code_revision
    for field, version in config.versions:
        document[field] = version
    return document


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
    if config == CAPSULE_V5 and args.provider == "codex":
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
    document = create_document(
        args,
        cases,
        variant_names,
        config,
        code_revision=code_revision,
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
                capsule_snapshot_artifacts(expected_snapshot)
                if config == CAPSULE_V5
                else document.artifacts[case.id]
            )
            specification = artifacts[variant]
            prompt = execution_prompt(specification, variant, config)
            if config == CAPSULE_V5:
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
                            routed_target_paths(case, artifacts["packet"])
                            if config == CAPSULE_V5
                            else ()
                        ),
                    )
                    if args.provider == "codex"
                    else core.run_mock(case, workspace)
                )
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
                if config == CAPSULE_V5 and variant == config.primary_candidate:
                    try:
                        required_edits, completed_edits = routed_edit_progress(
                            workspace,
                            artifacts["packet"],
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
                    pre_edit_categories = provider.get(
                        "pre_edit_command_categories",
                        {},
                    )
                    pre_edit_verify = (
                        pre_edit_categories.get("verify")
                        if isinstance(pre_edit_categories, dict)
                        else None
                    )
                    if isinstance(pre_edit_verify, int) and pre_edit_verify > 0:
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
    "pre_edit_telemetry",
    "event_errors",
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
    if "pre_edit_command_executions" in provider:
        count = provider["pre_edit_command_executions"]
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return False
    if "attempt_count" in provider:
        count = provider["attempt_count"]
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return False
    if "command_log" in provider:
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
        or telemetry.get("schema_version") != 2
        or telemetry.get("status") != "routed_edit_observed"
    ):
        return False
    counts: dict[str, int] = {}
    for field in _PRE_EDIT_TELEMETRY_FIELDS - {"schema_version", "status"}:
        value = telemetry.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return False
        counts[field] = value
    return bool(
        counts["target_count"] > 0
        and counts["substantive_file_change_events"] > 0
        and counts["file_change_events"]
        >= counts["substantive_file_change_events"]
        and counts["file_change_events"]
        >= counts["unclassified_file_change_events"]
    )


def _validated_capsule_snapshot(
    case: core.BenchmarkCase,
    snapshot: Any,
    fixture_snapshot: core.FixtureTreeSnapshot,
    starter_snapshot: core.FixtureTreeSnapshot,
) -> dict[str, str] | None:
    """Validate Capsule evidence while deriving every row from attested bytes."""

    if not isinstance(snapshot, dict) or set(snapshot) != {
        "fixture_sha256",
        "starter_sha256",
        "verification_fixture_sha256",
        "grading",
        "artifacts",
        "variants",
        "prompts",
        "capsule",
    }:
        return None
    try:
        artifacts = capsule_snapshot_artifacts(snapshot)
        packet_bytes = artifacts["packet"].encode("utf-8")
        capsule_bytes = artifacts["capsule"].encode("utf-8")
        if packet_bytes != core.fixture_snapshot_file(
            fixture_snapshot,
            VARIANTS["packet"],
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
                execution_prompt(text, variant, CAPSULE_V5).encode("utf-8")
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
        if config == CAPSULE_V5:
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
                )
                if artifacts is None:
                    return False
                artifacts_by_case[case.id] = artifacts
            current_snapshots = snapshots
            current_static = capsule_static_rows_from_snapshots(corpus, snapshots)
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
        or (config == CAPSULE_V5 and document.get("schema_version") != 2)
        or (config == CAPSULE_V5 and document.get("full_corpus") is not True)
        or not isinstance(snapshots, dict)
        or set(snapshots) != corpus_ids
        or snapshots != current_snapshots
        or document.get("static") != current_static
        or not results
        or (
            config == CAPSULE_V5
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
                if config == CAPSULE_V5
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
                config == CAPSULE_V5
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
        if document_comparison(document) != CAPSULE_V5:
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
        if read_value is None or read_value > 1:
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
    config: ComparisonConfig = CAPSULE_V5,
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
        if current_routed_edit_telemetry(telemetry) and not action_error:
            passed += 1
    return passed, total


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
    config: ComparisonConfig = CAPSULE_V5,
) -> list[str]:
    """Enumerate every failed product-claim predicate for a Capsule result."""

    limitations: list[str] = []
    successful_pairs, expected_pairs = primary_success_coverage(document, results, config)
    if expected_pairs == 0 or successful_pairs != expected_pairs:
        limitations.append(
            f"only {successful_pairs}/{expected_pairs} Packet v3/Capsule v5 pairs "
            "were jointly successful"
        )
    if primary_combined["pairs"] != expected_pairs:
        limitations.append(
            f"only {primary_combined['pairs']}/{expected_pairs} pairs had complete "
            "joint-success token telemetry"
        )
    if not preserved:
        limitations.append("Capsule v5 did not preserve task/test success for every fixture")

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
    if document.get("telemetry_attestation") != "externally-verified":
        limitations.append(
            "provider telemetry and grades are not independently attested"
        )
    if not capsule_full_corpus(document):
        limitations.append("the result does not cover the full fixture corpus")
    repetitions = document.get("repetitions")
    if not isinstance(repetitions, int) or isinstance(repetitions, bool) or repetitions < 3:
        limitations.append("the result has fewer than three repetitions")
    if not credible and not provider_errors and not verification_errors and not run_errors:
        limitations.append(
            "the run lacks complete publishable provenance or provider telemetry"
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
            "Capsule v5 did not reduce total model tokens across all runs "
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
            "Capsule v5 violated its classified pre-edit command budget in successful "
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
            "Capsule v5 pre-edit classification could not verify a routed/target "
            f"edit in {telemetry_failures}/{successful_runs} successful run(s)"
        )
    return limitations


def capsule_v5_report(document: dict[str, Any]) -> str:
    config = CAPSULE_V5
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
        "# Semantic Context Capsule Benchmark",
        "",
        f"- Run: `{document['run_id']}`",
        f"- Provider: `{document['provider']}`",
        f"- Model: `{document.get('model') or 'provider default'}`",
        f"- Reasoning effort: `{document.get('reasoning_effort') or 'provider default'}`",
        f"- Cases: {len(document['cases'])}",
        f"- Repetitions: {document['repetitions']}",
        "- Packet version: 3",
        "- Capsule version: 5",
        f"- Telemetry attestation: `{document.get('telemetry_attestation', 'none')}`",
        "",
    ]
    if not credible:
        lines.extend([
            "> Non-publishable experimental smoke run. Publishable evidence requires "
            "a named real model, complete telemetry and provenance, the full suite, "
            "and three or more repetitions.",
            "",
        ])
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
        f"Routed action gate: **{action_passed}/{action_total}** Capsule v5 runs.",
        "",
        "## Primary comparison: Capsule v5 vs Packet v3",
        "",
        "Only jointly successful Packet v3/Capsule v5 pairs contribute to paired totals, medians, and confidence intervals.",
        "",
        "| Metric | Packet v3 paired total | Capsule v5 paired total | Capsule delta | Paired median reduction | 95% fixture CI |",
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
        "| Metric | Packet v3 total | Capsule v5 total | Capsule delta |",
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
            "Capsule v5 has no input-only cost break-even for cached-input prices "
            "between 0% and 100% of uncached input."
        )
    elif cache_advantage == (0.0, 1.0):
        lines.append(
            "Capsule v5 input is cheaper for every cached-input unit price from 0% "
            "to 100% of uncached input. Output-token savings are excluded from this "
            "conservative comparison."
        )
    elif cache_advantage[0] == 0:
        lines.append(
            "Capsule v5 input is cheaper while cached input costs at most "
            f"**{cache_advantage[1] * 100:.2f}%** of uncached input. Output-token "
            "savings are excluded from this conservative comparison."
        )
    else:
        lines.append(
            "Capsule v5 input is cheaper once cached input costs at least "
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
            "This suite supports a product token-saving claim for Capsule v5 versus "
            "Packet v3 while preserving measured behavior."
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
            "Observed full-run result: Capsule v5 used "
            f"**{abs(observed_reduction):.2f}% {observed_direction}** total model "
            "tokens versus Packet v3. The suite "
            "cannot establish a product token-saving claim across every fixture because "
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
        "- Capsule v5 embeds Packet v3 and its routed source snapshot; this comparison excludes authoring cost and reuse break-even.",
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
    return capsule_v5_report(document)


def validate_capsule_release(
    document: dict[str, Any],
    rendered_report: bytes,
) -> list[str]:
    """Validate a newly published current-schema Capsule result/report pair."""

    errors: list[str] = []
    results = document.get("results")
    if not isinstance(results, list) or not capsule_report_is_credible(
        document,
        results,
    ):
        errors.append("Capsule result does not satisfy current credibility gates")
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
