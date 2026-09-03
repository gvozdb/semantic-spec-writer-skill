#!/usr/bin/env python3
"""Measure Semantic Spec Writer authoring cost, reuse cost, and break-even."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import stat
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
MAX_GENERATED_ARTIFACT_BYTES = 2_000_000
# These canonical-document hashes identify the pre-snapshot published lifecycle
# evidence.  It remains reproducible as a historical report, but is not accepted
# by generation_report_is_credible(), which intentionally requires current
# evidence fields.
HISTORICAL_GENERATION_SHA256 = (
    "caf58a65f4597961aa3f6193ede4b513a65b510d720b6b488b92e8db197d6ae1"
)
HISTORICAL_IMPLEMENTATION_SHA256 = (
    "d894f466c445eaa56e961042b064f9fcc30f953472ea0562181c27b4ae20a826"
)
sys.path.insert(0, str(BENCHMARKS))
import benchmark as core  # noqa: E402


def generation_prompt(
    case: core.BenchmarkCase,
    token_encoding: str | None,
    previous_error: str | None = None,
) -> str:
    packet_instruction = ""
    if case.manifest.get("execution_packet"):
        packet_instruction = (
            "This case requires a repository execution packet. Inspect the isolated "
            "starter repository under ../inputs/repo/, follow the execution-packet "
            "reference linked from ../inputs/skill/SKILL.md, and ground the route in "
            "existing files and source anchors. Validate the final packet against "
            "../inputs/repo/ with the skill checker. "
        )
    prompt = (
        "Read ../inputs/skill/SKILL.md and convert ../inputs/source.md into a compact, "
        "self-contained implementation spec. The ../inputs tree is immutable. Write only "
        "the artifact to result.spec.ctx in the current workspace. "
        f"The implementation target is {case.manifest['entrypoint']}. Preserve every "
        "requirement and every acceptance ID from the source. Do not implement the "
        "task. Apply the SKILL.md quality gate before finishing. Do not access files "
        "outside the current workspace and ../inputs, and do not use network access. "
        f"{packet_instruction}\n"
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
        "final_message_metadata": core.text_metadata(
            "mock provider copied the curated semantic spec"
        ),
        "event_errors": [],
        "stderr_metadata": core.text_metadata(""),
    }


def generation_case_snapshot(
    case: core.BenchmarkCase,
    *,
    fixture_snapshot: core.FixtureTreeSnapshot | None = None,
    starter_snapshot: core.FixtureTreeSnapshot | None = None,
) -> dict[str, Any]:
    """Derive generation evidence from one descriptor-captured case tree."""

    fixture_snapshot = fixture_snapshot or core.snapshot_fixture_tree(case.path)
    derived_starter = core.fixture_subtree_snapshot(fixture_snapshot, "starter")
    if starter_snapshot is not None and starter_snapshot != derived_starter:
        raise RuntimeError(f"{case.id}: starter snapshot is not from fixture snapshot")
    starter_snapshot = derived_starter
    source = core.fixture_snapshot_file(fixture_snapshot, "baseline.md").data
    source.decode("utf-8")
    return {
        "fixture_sha256": fixture_snapshot.sha256,
        "starter_sha256": starter_snapshot.sha256,
        "source": core.attest_bytes(source),
        "source_sha256": core.sha256_bytes(source),
    }


def generation_snapshot_source(snapshot: Any, case_id: str) -> bytes:
    """Decode one bounded exact baseline input from generation evidence."""

    if not isinstance(snapshot, dict):
        raise ValueError(f"{case_id}: generation snapshot must be an object")
    return core.attested_bytes(
        snapshot.get("source"),
        f"{case_id} generation source",
        max_bytes=MAX_GENERATED_ARTIFACT_BYTES,
    )


def require_generation_snapshot(
    case: core.BenchmarkCase,
    expected: dict[str, Any],
    skill_sha256: str,
) -> None:
    if generation_case_snapshot(case) != expected:
        raise RuntimeError(f"{case.id}: generation fixture changed during run")
    if core.snapshot_fixture_tree(SKILL_DIR).sha256 != skill_sha256:
        raise RuntimeError("semantic-spec-writer skill changed during run")


class GenerationRunDocument(dict[str, Any]):
    """Serializable generation evidence plus immutable input byte snapshots."""

    __slots__ = ("fixture_snapshots", "starter_snapshots", "skill_snapshot")

    def __init__(
        self,
        payload: dict[str, Any],
        fixture_snapshots: dict[str, core.FixtureTreeSnapshot],
        starter_snapshots: dict[str, core.FixtureTreeSnapshot],
        skill_snapshot: core.FixtureTreeSnapshot,
    ) -> None:
        super().__init__(payload)
        self.fixture_snapshots = fixture_snapshots
        self.starter_snapshots = starter_snapshots
        self.skill_snapshot = skill_snapshot


def generation_document(
    args: argparse.Namespace,
    cases: list[core.BenchmarkCase],
    code_revision: dict[str, Any] | None = None,
) -> GenerationRunDocument:
    cases_dir = (args.cases_dir or core.CASES_DIR).resolve()
    corpus = core.discover_cases(cases_dir=cases_dir)
    skill_snapshot = core.snapshot_fixture_tree(SKILL_DIR)
    fixture_snapshots: dict[str, core.FixtureTreeSnapshot] = {}
    starter_snapshots: dict[str, core.FixtureTreeSnapshot] = {}
    snapshots: dict[str, dict[str, Any]] = {}
    for case in cases:
        fixture = core.snapshot_fixture_tree(case.path)
        starter = core.fixture_subtree_snapshot(fixture, "starter")
        fixture_snapshots[case.id] = fixture
        starter_snapshots[case.id] = starter
        snapshots[case.id] = generation_case_snapshot(
            case,
            fixture_snapshot=fixture,
            starter_snapshot=starter,
        )
    document = GenerationRunDocument({
        "schema_version": 2,
        "kind": "semantic-spec-generation",
        "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "created_at": datetime.now(UTC).isoformat(),
        "provider": args.provider,
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "timeout_seconds": getattr(args, "timeout_seconds", 600),
        "token_encoding": args.token_encoding,
        "max_attempts": args.max_attempts,
        "cases": [case.id for case in cases],
        "case_suite": cases_dir.name,
        "full_corpus": {case.id for case in cases} == {case.id for case in corpus},
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "codex": core.command_version(["codex", "--version"]),
            "git_commit": (
                code_revision.get("commit")
                if isinstance(code_revision, dict)
                else core.git_commit()
            ),
        },
        "skill_sha256": skill_snapshot.sha256,
        "fixture_snapshot": snapshots,
        "results": [],
    }, fixture_snapshots, starter_snapshots, skill_snapshot)
    if code_revision is not None:
        document["code_revision"] = code_revision
    return document


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


def provider_failure(provider: dict[str, Any]) -> str | None:
    if provider.get("return_code") != 0:
        return (
            f"provider exited with {provider.get('return_code')}: "
            f"{provider.get('stderr_tail', '')}"
        )
    event_errors = provider.get("event_errors", [])
    if event_errors:
        return "provider reported errors: " + "; ".join(event_errors)
    return None


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
        "final_message_metadata": selected.get(
            "final_message_metadata",
            core.text_metadata(""),
        ),
        "event_errors": [
            error
            for provider in providers
            for error in provider.get("event_errors", [])
        ],
        "stderr_metadata": selected.get("stderr_metadata", core.text_metadata("")),
        "attempt_count": len(attempts),
    }


def prepare_generation_workspace(
    attempt_root: Path,
    case: core.BenchmarkCase,
    *,
    source_bytes: bytes | None = None,
    starter_snapshot: core.FixtureTreeSnapshot | None = None,
    skill_snapshot: core.FixtureTreeSnapshot | None = None,
) -> tuple[Path, Path]:
    """Materialize every provider input from already captured immutable bytes."""

    if source_bytes is None or starter_snapshot is None:
        fixture = core.snapshot_fixture_tree(case.path)
        source_bytes = core.fixture_snapshot_file(fixture, "baseline.md").data
        starter_snapshot = core.fixture_subtree_snapshot(fixture, "starter")
    if skill_snapshot is None:
        skill_snapshot = core.snapshot_fixture_tree(SKILL_DIR)
    inputs = attempt_root / "inputs"
    workspace = attempt_root / "workspace"
    inputs.mkdir(parents=True)
    workspace.mkdir()
    core.materialize_fixture_tree(skill_snapshot, inputs / "skill")
    (inputs / "source.md").write_bytes(source_bytes)
    core.materialize_fixture_tree(starter_snapshot, inputs / "repo")
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    return workspace, inputs / "repo"


def read_generation_artifact(path: Path, workspace: Path) -> str:
    try:
        workspace_root = workspace.resolve(strict=True)
        before = path.lstat()
    except OSError as exc:
        raise RuntimeError("provider did not create result.spec.ctx") from exc
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise RuntimeError("provider artifact must be a single regular file")
    if before.st_size > MAX_GENERATED_ARTIFACT_BYTES:
        raise RuntimeError("provider artifact exceeds size limit")
    try:
        path.resolve(strict=True).relative_to(workspace_root)
    except (OSError, ValueError) as exc:
        raise RuntimeError("provider artifact escapes generation workspace") from exc

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError("provider artifact cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size > MAX_GENERATED_ARTIFACT_BYTES
        ):
            raise RuntimeError("provider artifact changed during validation")
        payload = bytearray()
        while len(payload) <= MAX_GENERATED_ARTIFACT_BYTES:
            chunk = os.read(
                descriptor,
                min(65_536, MAX_GENERATED_ARTIFACT_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > MAX_GENERATED_ARTIFACT_BYTES:
            raise RuntimeError("provider artifact exceeds size limit")
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or after.st_size != opened.st_size
            or after.st_mode != opened.st_mode
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
            or len(payload) != after.st_size
        ):
            raise RuntimeError("provider artifact changed while being captured")
    finally:
        os.close(descriptor)
    try:
        return bytes(payload).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("provider artifact is not valid UTF-8") from exc


def generate(
    args: argparse.Namespace,
    code_revision: dict[str, Any] | None = None,
) -> Path:
    if args.max_attempts < 1:
        raise ValueError("max attempts must be at least 1")
    cases_dir = (args.cases_dir or core.CASES_DIR).resolve()
    cases = core.discover_cases(args.case, cases_dir)
    fixture_errors = core.validate(cases)
    if fixture_errors:
        raise RuntimeError("benchmark validation failed:\n" + "\n".join(fixture_errors))
    output = Path(os.path.abspath(args.output))
    output.mkdir(parents=True, exist_ok=False)
    specs = output / "specs"
    specs.mkdir()
    attempt_artifacts = output / "attempts"
    attempt_artifacts.mkdir()
    result_path = core.lexical_output_path(output / "generation.json")
    document = generation_document(args, cases, code_revision)

    def require_code_revision() -> None:
        if code_revision is None:
            return
        required = code_revision.get("required_paths")
        if not isinstance(required, dict):
            raise RuntimeError("generation code attestation lacks required paths")
        core.require_git_worktree_revision(code_revision, tuple(sorted(required)))

    require_code_revision()

    with (
        tempfile.TemporaryDirectory(prefix="semantic-spec-generate-") as temporary,
        core.open_result_checkpoint(result_path, document, force=False) as checkpoint,
    ):
        temporary_root = Path(temporary)
        for index, case in enumerate(cases, start=1):
            require_code_revision()
            expected_snapshot = document["fixture_snapshot"][case.id]
            require_generation_snapshot(
                case, expected_snapshot, document["skill_sha256"]
            )
            destination = specs / f"{case.id}.spec.ctx"
            source_bytes = generation_snapshot_source(expected_snapshot, case.id)
            source = source_bytes.decode("utf-8")
            starter_hash = expected_snapshot["starter_sha256"]
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
                attempt_root = (
                    temporary_root / f"{case.id}-attempt-{attempt_number}"
                )
                workspace, starter_snapshot = prepare_generation_workspace(
                    attempt_root,
                    case,
                    source_bytes=source_bytes,
                    starter_snapshot=document.starter_snapshots[case.id],
                    skill_snapshot=document.skill_snapshot,
                )
                if core.tree_sha256(starter_snapshot) != starter_hash:
                    raise RuntimeError(f"{case.id}: starter snapshot mismatch")
                input_hash = core.tree_sha256(starter_snapshot.parent)
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
                        source_artifact = (
                            "packet.spec.ctx"
                            if case.manifest.get("execution_packet")
                            else "semantic.spec.ctx"
                        )
                        artifact_path.write_bytes(
                            core.fixture_snapshot_file(
                                document.fixture_snapshots[case.id],
                                source_artifact,
                            ).data
                        )
                        provider = mock_provider()
                    if (
                        core.tree_sha256(starter_snapshot) != starter_hash
                        or core.tree_sha256(starter_snapshot.parent) != input_hash
                    ):
                        raise RuntimeError(
                            f"{case.id}: immutable generation input was modified"
                        )
                    if failure := provider_failure(provider):
                        raise RuntimeError(failure)
                    semantic = read_generation_artifact(artifact_path, workspace)
                    validated_artifact = attempt_root / "candidate.spec.ctx"
                    with validated_artifact.open("x", encoding="utf-8") as handle:
                        handle.write(semantic)
                    validation_errors = core.validate_semantic_text(case, semantic)
                    verification_command = case.manifest.get("verification_command")
                    if (
                        not case.manifest.get("execution_packet")
                        and verification_command
                        and str(verification_command)
                        not in core.verification_commands(semantic)
                    ):
                        validation_errors.append(
                            f"{case.id}: generated spec lacks exact verification command"
                        )
                    if case.manifest.get("execution_packet"):
                        validation_errors.extend(
                            core.validate_execution_packet_artifact(
                                case,
                                validated_artifact,
                                starter_path=starter_snapshot,
                            )
                        )
                    check_command = [
                        sys.executable,
                        str(SKILL_DIR / "scripts" / "check_conversion.py"),
                        str(starter_snapshot.parent / "source.md"),
                        str(validated_artifact),
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
                    if not semantic:
                        try:
                            semantic = read_generation_artifact(artifact_path, workspace)
                        except RuntimeError:
                            semantic = ""
                    error = "provider timeout"
                except Exception as exc:  # noqa: BLE001 - checkpoint failed attempt
                    if not semantic:
                        try:
                            semantic = read_generation_artifact(artifact_path, workspace)
                        except RuntimeError:
                            semantic = ""
                    error = f"{type(exc).__name__}: {exc}"

                try:
                    if (
                        core.tree_sha256(starter_snapshot) != starter_hash
                        or core.tree_sha256(starter_snapshot.parent) != input_hash
                    ):
                        raise RuntimeError(
                            f"{case.id}: immutable generation input was modified"
                        )
                except (OSError, RuntimeError, ValueError) as exc:
                    semantic = ""
                    error = f"{type(exc).__name__}: {exc}"

                require_generation_snapshot(
                    case, expected_snapshot, document["skill_sha256"]
                )
                require_code_revision()
                provider = core.redact_provider_telemetry(provider)

                attempt_artifact = None
                if semantic:
                    attempt_artifact = (
                        f"attempts/{case.id}-a{attempt_number}.spec.ctx"
                    )
                    (output / attempt_artifact).write_text(semantic, encoding="utf-8")
                attempts.append({
                    "attempt": attempt_number,
                    "artifact": attempt_artifact,
                    "specification": (
                        core.attest_text(semantic) if semantic else None
                    ),
                    "semantic": core.text_metrics(semantic) if semantic else None,
                    "conversion_check": conversion_check,
                    "provenance": {
                        "spec_sha256": (
                            core.sha256_bytes(semantic.encode("utf-8"))
                            if semantic else None
                        ),
                        "prompt_sha256": core.sha256_bytes(prompt.encode("utf-8")),
                        "source_sha256": expected_snapshot["source_sha256"],
                        "starter_sha256": expected_snapshot["starter_sha256"],
                        "fixture_sha256": expected_snapshot["fixture_sha256"],
                        "skill_sha256": document["skill_sha256"],
                    },
                    "provider": provider,
                    # Retry diagnostics can contain fixture content, provider
                    # stderr, or exception text.  They remain available only
                    # in-process for the next prompt; checkpoints retain an
                    # auditable digest and length.
                    "error": core.redact_error(error),
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
                "specification": core.attest_text(semantic) if semantic else None,
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
                    "source_sha256": expected_snapshot["source_sha256"],
                    "spec_sha256": core.sha256_bytes(semantic.encode("utf-8")) if semantic else None,
                    "prompt_sha256": selected_record["provenance"]["prompt_sha256"],
                    "starter_sha256": expected_snapshot["starter_sha256"],
                    "fixture_sha256": expected_snapshot["fixture_sha256"],
                    "skill_sha256": document["skill_sha256"],
                },
                "provider": core.redact_provider_telemetry(
                    aggregate_attempt_providers(attempts, selected_attempt)
                ),
                "error": None if selected_attempt is not None else last_attempt["error"],
            }
            document["results"].append(result)
            checkpoint.write_json(document)
            require_code_revision()
    require_code_revision()
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


def generation_static_rows(generation: dict[str, Any]) -> list[dict[str, Any]]:
    """Render legacy conversion-check rows for historical evidence only."""

    rows = []
    for result in generation["results"]:
        selected_attempt = result.get("selected_attempt")
        if selected_attempt is None:
            continue
        check = result["attempts"][selected_attempt - 1].get("conversion_check")
        if not check:
            continue
        source = check["source"]
        semantic = check["output"]
        rows.append({
            "case": result["case"],
            "baseline": source,
            "semantic": semantic,
            "byte_reduction_percent": core.compression_percent(
                source["bytes"], semantic["bytes"]
            ),
            "word_reduction_percent": core.compression_percent(
                source["words"], semantic["words"]
            ),
            "token_reduction_percent": (
                core.compression_percent(source["tokens"], semantic["tokens"])
                if "tokens" in source and "tokens" in semantic
                else None
            ),
        })
    return rows


def current_generation_static_rows(
    generation: dict[str, Any],
) -> list[dict[str, Any]]:
    """Recompute byte/word size from attestations without tokenizer claims."""

    snapshots = generation.get("fixture_snapshot")
    results = generation.get("results")
    if not isinstance(snapshots, dict) or not isinstance(results, list):
        return []
    rows: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, dict) or result.get("selected_attempt") is None:
            continue
        case_id = result.get("case")
        if not isinstance(case_id, str) or case_id not in snapshots:
            return []
        try:
            source = generation_snapshot_source(snapshots[case_id], case_id).decode(
                "utf-8"
            )
            semantic = core.attested_text(
                result.get("specification"),
                f"{case_id} generated specification",
                max_bytes=MAX_GENERATED_ARTIFACT_BYTES,
            )
        except (TypeError, UnicodeError, ValueError):
            return []
        baseline_metrics = core.text_metrics(source)
        semantic_metrics = core.text_metrics(semantic)
        rows.append({
            "case": case_id,
            "baseline": baseline_metrics,
            "semantic": semantic_metrics,
            "byte_reduction_percent": core.compression_percent(
                baseline_metrics["bytes"],
                semantic_metrics["bytes"],
            ),
            "word_reduction_percent": core.compression_percent(
                baseline_metrics["words"],
                semantic_metrics["words"],
            ),
            "token_reduction_percent": None,
        })
    return rows


def size_only_static_rows(rows: Any) -> list[dict[str, Any]]:
    """Strip unverified tokenizer fields from current fallback static rows."""

    if not isinstance(rows, list):
        return []
    sanitized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            return []
        baseline = row.get("baseline")
        semantic = row.get("semantic")
        if not isinstance(baseline, dict) or not isinstance(semantic, dict):
            return []
        kept_baseline = {
            key: value
            for key, value in baseline.items()
            if key in {"bytes", "characters", "words", "lines"}
        }
        kept_semantic = {
            key: value
            for key, value in semantic.items()
            if key in {"bytes", "characters", "words", "lines"}
        }
        if not all(
            isinstance(metrics.get(field), int)
            and not isinstance(metrics.get(field), bool)
            and metrics[field] >= 0
            for metrics in (kept_baseline, kept_semantic)
            for field in ("bytes", "words")
        ):
            return []
        sanitized.append({
            "case": row.get("case", "unknown"),
            "baseline": kept_baseline,
            "semantic": kept_semantic,
            "byte_reduction_percent": core.compression_percent(
                kept_baseline["bytes"],
                kept_semantic["bytes"],
            ),
            "word_reduction_percent": core.compression_percent(
                kept_baseline["words"],
                kept_semantic["words"],
            ),
            "token_reduction_percent": None,
        })
    return sanitized


def canonical_document_sha256(document: dict[str, Any]) -> str | None:
    """Return the stable hash used by historical JSON artifacts, if serializable."""

    try:
        payload = json.dumps(
            document,
            indent=2,
            ensure_ascii=True,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError):
        return None
    return core.sha256_bytes(payload)


def is_historical_lifecycle_pair(
    generation: dict[str, Any], implementation: dict[str, Any]
) -> bool:
    """Recognize only the immutable published pre-snapshot evidence pair.

    The report is retained byte-for-byte for historical reproducibility.  A
    schema-shaped or modified legacy document cannot take this path, and legacy
    evidence never passes the current credibility gates.
    """

    return (
        canonical_document_sha256(generation) == HISTORICAL_GENERATION_SHA256
        and canonical_document_sha256(implementation)
        == HISTORICAL_IMPLEMENTATION_SHA256
    )


def generation_report_is_credible(
    generation: dict[str, Any],
    *,
    cases_dir: Path | None = None,
    skill_dir: Path | None = None,
) -> bool:
    cases = generation.get("cases")
    snapshots = generation.get("fixture_snapshot")
    results = generation.get("results")
    try:
        corpus = (
            core.discover_cases(cases_dir=cases_dir)
            if cases_dir is not None
            else core.recorded_case_corpus(generation)
        )
        current_snapshots = {
            case.id: generation_case_snapshot(case) for case in corpus
        }
        current_skill_hash = core.snapshot_fixture_tree(
            skill_dir or SKILL_DIR
        ).sha256
    except (OSError, RuntimeError, ValueError):
        return False
    corpus_ids = {case.id for case in corpus}
    if (
        generation.get("schema_version") != 2
        or (
            cases_dir is not None
            and generation.get("case_suite") != cases_dir.name
        )
        or not isinstance(cases, list)
        or not cases
        or any(not isinstance(case, str) for case in cases)
        or len(cases) != len(set(cases))
        or set(cases) != corpus_ids
        or not isinstance(snapshots, dict)
        or snapshots != current_snapshots
        or generation.get("skill_sha256") != current_skill_hash
        or not isinstance(results, list)
        or any(not isinstance(result, dict) for result in results)
        or len(results) != len(corpus_ids)
    ):
        return False
    result_cases = [result.get("case") for result in results]
    if (
        any(not isinstance(case, str) for case in result_cases)
        or len(result_cases) != len(set(result_cases))
        or set(result_cases) != corpus_ids
    ):
        return False

    def nonnegative_int(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    hash_pattern = re.compile(r"^[0-9a-f]{64}$")
    cases_by_id = {case.id: case for case in corpus}
    for result in results:
        case = cases_by_id[result["case"]]
        snapshot = snapshots[case.id]
        attempts = result.get("attempts")
        selected_attempt = result.get("selected_attempt")
        provenance = result.get("provenance")
        try:
            source = generation_snapshot_source(snapshot, case.id).decode("utf-8")
            specification = core.attested_text(
                result.get("specification"),
                f"{case.id} generated specification",
                max_bytes=MAX_GENERATED_ARTIFACT_BYTES,
            )
        except (TypeError, UnicodeError, ValueError):
            return False
        specification_sha256 = core.sha256_bytes(specification.encode("utf-8"))
        if (
            result.get("error") is not None
            or not isinstance(selected_attempt, int)
            or isinstance(selected_attempt, bool)
            or not isinstance(attempts, list)
            or selected_attempt < 1
            or selected_attempt > len(attempts)
            or result.get("artifact") != f"specs/{case.id}.spec.ctx"
            or snapshot.get("source_sha256")
            != core.sha256_bytes(source.encode("utf-8"))
            or result.get("source") != core.text_metrics(source)
            or not isinstance(provenance, dict)
            or provenance.get("source_sha256") != snapshot["source_sha256"]
            or provenance.get("starter_sha256") != snapshot["starter_sha256"]
            or provenance.get("fixture_sha256") != snapshot["fixture_sha256"]
            or provenance.get("skill_sha256") != current_skill_hash
            or provenance.get("spec_sha256") != specification_sha256
            or not hash_pattern.fullmatch(str(provenance.get("prompt_sha256", "")))
            or result.get("semantic") != core.text_metrics(specification)
        ):
            return False
        if [attempt.get("attempt") for attempt in attempts] != list(
            range(1, len(attempts) + 1)
        ):
            return False
        for attempt in attempts:
            attempt_provenance = attempt.get("provenance")
            provider = attempt.get("provider")
            usage = provider.get("usage") if isinstance(provider, dict) else None
            attempt_attestation = attempt.get("specification")
            try:
                attempt_specification = (
                    core.attested_text(
                        attempt_attestation,
                        f"{case.id} attempt specification",
                        max_bytes=MAX_GENERATED_ARTIFACT_BYTES,
                    )
                    if attempt_attestation is not None
                    else None
                )
            except (TypeError, UnicodeError, ValueError):
                return False
            attempt_sha256 = (
                core.sha256_bytes(attempt_specification.encode("utf-8"))
                if attempt_specification is not None
                else None
            )
            if (
                not isinstance(attempt_provenance, dict)
                or attempt_provenance.get("source_sha256")
                != snapshot["source_sha256"]
                or attempt_provenance.get("starter_sha256")
                != snapshot["starter_sha256"]
                or attempt_provenance.get("fixture_sha256")
                != snapshot["fixture_sha256"]
                or attempt_provenance.get("skill_sha256") != current_skill_hash
                or not hash_pattern.fullmatch(
                    str(attempt_provenance.get("prompt_sha256", ""))
                )
                or attempt_provenance.get("spec_sha256") != attempt_sha256
                or attempt.get("semantic")
                != (
                    core.text_metrics(attempt_specification)
                    if attempt_specification is not None
                    else None
                )
                or not isinstance(provider, dict)
                or not isinstance(provider.get("event_errors"), list)
                or not isinstance(usage, dict)
                or any(
                    not nonnegative_int(usage.get(field))
                    for field in (
                        "input_tokens",
                        "uncached_input_tokens",
                        "output_tokens",
                    )
                )
            ):
                return False
        selected = attempts[selected_attempt - 1]
        if (
            selected.get("error") is not None
            or selected["provider"].get("return_code") != 0
            or selected["provider"].get("event_errors") != []
            or selected["provenance"].get("spec_sha256")
            != provenance["spec_sha256"]
            or selected["provenance"].get("prompt_sha256")
            != provenance["prompt_sha256"]
            or selected.get("semantic") != result.get("semantic")
            or selected.get("specification") != result.get("specification")
        ):
            return False

    return bool(
        generation.get("provider") != "mock"
        and generation.get("model")
        and generation.get("reasoning_effort")
    )


def render_report(generation: dict[str, Any], implementation: dict[str, Any]) -> str:
    historical_pair = is_historical_lifecycle_pair(generation, implementation)
    if set(generation["cases"]) != set(implementation["cases"]):
        raise ValueError("generation and implementation case sets differ")
    if implementation.get("semantic_source") != "generated":
        raise ValueError("implementation run did not use generated semantic specs")
    exact_spec_evidence = not historical_pair
    if historical_pair:
        generated_evidence = {
            result["case"]: result["provenance"].get("spec_sha256")
            for result in generation["results"]
        }
    else:
        try:
            generated_evidence = {
                result["case"]: core.attested_bytes(
                    result["specification"],
                    f"{result['case']} generated specification",
                    max_bytes=MAX_GENERATED_ARTIFACT_BYTES,
                )
                for result in generation["results"]
            }
        except (KeyError, TypeError, UnicodeError, ValueError):
            exact_spec_evidence = False
            generated_evidence = {
                result["case"]: result["provenance"].get("spec_sha256")
                for result in generation["results"]
            }
    for result in implementation["results"]:
        if result["variant"] != "semantic":
            continue
        if historical_pair:
            implementation_evidence: Any = result["provenance"].get("spec_sha256")
        elif exact_spec_evidence:
            try:
                implementation_evidence = core.attested_bytes(
                    implementation["fixture_snapshot"][result["case"]][
                        "specifications"
                    ]["semantic"],
                    f"{result['case']} implementation specification",
                    max_bytes=MAX_GENERATED_ARTIFACT_BYTES,
                )
            except (KeyError, TypeError, UnicodeError, ValueError) as exc:
                raise ValueError(
                    "implementation result lacks exact generated-spec evidence"
                ) from exc
        else:
            implementation_evidence = result["provenance"].get("spec_sha256")
        if implementation_evidence != generated_evidence.get(result["case"]):
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
    quality_preserved = core.quality_preserved(implementation["results"])
    current_credible = not historical_pair and exact_spec_evidence and bool(
        generation.get("case_suite", "cases")
        == implementation.get("case_suite", "cases")
        and generation_report_is_credible(generation)
        and core.implementation_report_is_credible(
            implementation, implementation["results"]
        )
    )
    credible = current_credible or historical_pair
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

    if historical_pair:
        static = generation_static_rows(generation) or implementation["static"]
        tokenizer_note = (
            f"Tokenizer: `{generation.get('token_encoding') or 'not recorded'}`."
        )
    else:
        static = current_generation_static_rows(generation) or size_only_static_rows(
            implementation.get("static")
        )
        tokenizer_note = (
            "Generated document token reduction: `not claimed` for current evidence; "
            "byte and word counts are recomputed from attested source/spec bytes."
        )
    lines.extend([
        "",
        "## Generated document size",
        "",
        core.render_static_markdown(static).rstrip(),
        "",
        tokenizer_note,
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
    generate_parser.add_argument("--cases-dir", type=Path)
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
            with (
                core.open_pinned_json(args.generation) as generation_input,
                core.open_pinned_json(args.implementation) as implementation_input,
            ):
                report = render_report(
                    generation_input.document,
                    implementation_input.document,
                )
                if args.output:
                    core.write_report_from_pinned_inputs(
                        args.output,
                        report,
                        overwrite=args.force,
                        inputs=[generation_input, implementation_input],
                    )
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
