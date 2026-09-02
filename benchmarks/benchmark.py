#!/usr/bin/env python3
"""Reproducible paired benchmark for Semantic Spec Writer."""

from __future__ import annotations

import argparse
import base64
import binascii
import ctypes
import errno
import hashlib
import json
import os
import platform
import random
import re
import secrets
import shutil
import shlex
import signal
import stat
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

try:  # Result checkpoints require an advisory lease on POSIX hosts.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"
CASES_DIR = BENCHMARKS / "cases"
RESULTS_DIR = BENCHMARKS / "results"
VARIANTS = {
    "baseline": "baseline.md",
    "semantic": "semantic.spec.ctx",
}
COMMAND_CATEGORY_PATTERNS = {
    "discovery": re.compile(
        r"(?:^|[;&|()]|\s)(?:rg|grep|find|fd|fdfind|ls|tree)(?:\s|$)"
        r"|(?:^|[;&|()]|\s)git\s+(?:status|ls-files|grep|show|diff)(?:\s|$)",
        re.IGNORECASE | re.DOTALL,
    ),
    "read": re.compile(
        r"(?:^|[;&|()]|\s)(?:cat|head|tail|sed|nl|awk|less|more|bat)(?:\s|$)"
        r"|(?:^|[;&|()]|\s)git\s+(?:show|diff)(?:\s|$)"
        r"|(?:^|[;&|()]|\s)python(?:[0-9.]+)?(?:\s|$).*?"
        r"(?:\bopen\s*\(|\.read_(?:text|bytes)\s*\()",
        re.IGNORECASE | re.DOTALL,
    ),
    "verify": re.compile(
        r"(?:pytest|unittest|py_compile|npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test)",
        re.IGNORECASE | re.DOTALL,
    ),
}
PROVIDER_USAGE_FIELDS = frozenset({
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "uncached_input_tokens",
})
PROVIDER_TOOL_CALL_FIELDS = frozenset({
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "web_search",
})
PUBLIC_ERROR_CODES = frozenset({
    "capsule_declared_verification_count",
    "capsule_incomplete_routed_edits",
    "capsule_no_routed_edit",
    "capsule_pre_edit_command",
    "capsule_pre_edit_discovery",
    "capsule_pre_edit_read",
    "capsule_pre_edit_verification",
    "capsule_routed_edit_attestation_failed",
    "capsule_tool_sequence",
    "grader_exception",
    "grader_timeout",
    "provider_event_error",
    "provider_exception",
    "provider_nonzero_exit",
    "provider_timeout",
    "verification_exception",
    "verification_failed",
    "verification_timeout",
})
VERIFY_LINE = re.compile(r"^  V\d+:\s*`([^`]+)`\s*$")
CASE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
EXECUTION_PACKET_CHECK = (
    ROOT
    / "skills"
    / "semantic-spec-writer"
    / "scripts"
    / "check_execution_packet.py"
)
# This immutable pre-snapshot implementation result is rendered through its
# historical format only.  It remains intentionally ineligible for the current
# credibility gate, which requires fresh fixture snapshots and privacy schema.
HISTORICAL_IMPLEMENTATION_SHA256 = (
    "d894f466c445eaa56e961042b064f9fcc30f953472ea0562181c27b4ae20a826"
)
MAX_ATTESTED_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_BENCHMARK_RESULT_BYTES = 256 * 1024 * 1024
CAPSULE_RELEASE_DOCUMENT_PATHS = frozenset({
    "CAPSULE_BENCHMARK.md",
    "README.md",
    "benchmarks/README.md",
})
CAPSULE_RELEASE_ARTIFACT_PREFIX = (
    "benchmarks",
    "results",
    "published",
)
CAPSULE_RELEASE_ARTIFACT_FILES = frozenset({"capsule-r3.json", "CAPSULE.md"})
CAPSULE_RELEASE_RUN_NAME = re.compile(
    r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z"
)


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


def write_bytes_atomic(
    path: Path,
    payload: bytes,
    *,
    overwrite: bool = True,
    protected_paths: Iterable[Path | str | tuple[int, int]] = (),
    protected_input: Path | str | None = None,
    protected_identity: tuple[int, int] | None = None,
    protected_input_identity: tuple[int, int] | None = None,
) -> None:
    """Publish bytes through descriptor-pinned Linux/POSIX primitives.

    ``protected_paths`` may contain source paths or ``(st_dev, st_ino)`` pairs.
    It prevents a report from replacing one of its own inputs, including through
    a hard link.  The singular arguments are compatibility conveniences for
    callers that already hold one source path or identity.
    """

    target = _open_publication_target(
        path,
        protected_paths=protected_paths,
        protected_input=protected_input,
        protected_identity=protected_identity,
        protected_input_identity=protected_input_identity,
    )
    try:
        _write_bytes_secure(target, payload, overwrite=overwrite)
    finally:
        target.close()


def write_text_atomic(
    path: Path,
    text: str,
    *,
    overwrite: bool = True,
    protected_paths: Iterable[Path | str | tuple[int, int]] = (),
    protected_input: Path | str | None = None,
    protected_identity: tuple[int, int] | None = None,
    protected_input_identity: tuple[int, int] | None = None,
) -> None:
    write_bytes_atomic(
        path,
        text.encode("utf-8"),
        overwrite=overwrite,
        protected_paths=protected_paths,
        protected_input=protected_input,
        protected_identity=protected_identity,
        protected_input_identity=protected_input_identity,
    )


def write_json_atomic(
    path: Path,
    value: Any,
    *,
    protected_paths: Iterable[Path | str | tuple[int, int]] = (),
) -> None:
    write_text_atomic(
        path,
        json.dumps(value, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
        protected_paths=protected_paths,
    )


def text_metadata(value: str) -> dict[str, Any]:
    payload = value.encode("utf-8")
    return {
        "bytes": len(payload),
        "sha256": sha256_bytes(payload),
    }


def attest_bytes(value: bytes) -> dict[str, str]:
    """Return the canonical JSON representation of exact, non-telemetry bytes."""

    return {
        "encoding": "base64",
        "data": base64.b64encode(value).decode("ascii"),
    }


def attested_bytes(
    value: Any,
    label: str = "attested bytes",
    *,
    max_bytes: int | None = None,
) -> bytes:
    """Decode a canonical byte attestation and reject ambiguous encodings."""

    if max_bytes is None:
        max_bytes = MAX_ATTESTED_ARTIFACT_BYTES
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or max_bytes < 0
    ):
        raise ValueError("max_bytes must be a nonnegative integer")

    if (
        not isinstance(value, dict)
        or set(value) != {"encoding", "data"}
        or value.get("encoding") != "base64"
        or not isinstance(value.get("data"), str)
    ):
        raise ValueError(f"{label} is not a canonical base64 attestation")
    encoded = value["data"]
    # Bound allocation before either ASCII encoding or base64 decoding.  Merely
    # checking the decoded payload afterwards permits an attacker-controlled
    # JSON string to cause a much larger transient allocation first.
    if len(encoded) % 4:
        raise ValueError(f"{label} has invalid base64 data")
    padding = 2 if encoded.endswith("==") else 1 if encoded.endswith("=") else 0
    decoded_length = (len(encoded) // 4) * 3 - padding
    if decoded_length > max_bytes:
        raise ValueError(f"{label} exceeds the {max_bytes}-byte limit")
    try:
        payload = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise ValueError(f"{label} has invalid base64 data") from exc
    if base64.b64encode(payload).decode("ascii") != encoded:
        raise ValueError(f"{label} base64 data is not canonical")
    if len(payload) > max_bytes:
        raise ValueError(f"{label} exceeds the {max_bytes}-byte limit")
    return payload


def attest_text(value: str) -> dict[str, str]:
    """Attest the exact UTF-8 bytes of a task document."""

    if not isinstance(value, str):
        raise TypeError("attested text must be a string")
    return attest_bytes(value.encode("utf-8"))


def attested_text(
    value: Any,
    label: str = "attested text",
    *,
    max_bytes: int | None = None,
) -> str:
    """Decode exact attested UTF-8 task-document bytes."""

    payload = attested_bytes(value, label, max_bytes=max_bytes)
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not valid UTF-8") from exc


def _is_text_metadata(value: Any) -> bool:
    """Return whether value is the canonical non-prose text representation."""

    return (
        isinstance(value, dict)
        and set(value) == {"bytes", "sha256"}
        and isinstance(value["bytes"], int)
        and not isinstance(value["bytes"], bool)
        and value["bytes"] >= 0
        and isinstance(value["sha256"], str)
        and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None
    )


def _text_for_metadata(value: Any) -> str:
    """Render untrusted data only long enough to hash it, never to persist it."""

    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="surrogateescape")
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - hostile telemetry must not abort redaction
        return f"<{type(value).__name__}: unprintable>"


def redact_text(value: Any) -> dict[str, Any]:
    """Return only byte length and digest for untrusted text-like telemetry."""

    if _is_text_metadata(value):
        return {"bytes": value["bytes"], "sha256": value["sha256"]}
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": sha256_bytes(value)}
    return text_metadata(_text_for_metadata(value))


def redact_error(value: Any) -> dict[str, Any] | None:
    """Redact an error while preserving whether a result failed."""

    if value is None:
        return None
    if (
        isinstance(value, dict)
        and set(value) == {"codes"}
        and isinstance(value["codes"], list)
        and value["codes"]
        and all(
            isinstance(code, str) and code in PUBLIC_ERROR_CODES
            for code in value["codes"]
        )
    ):
        return {"codes": sorted(set(value["codes"]))}
    return redact_text(value)


def _redact_count_map(
    value: Any,
    allowed_keys: Iterable[str],
) -> dict[str, int]:
    """Keep only non-negative integer telemetry counts from a mapping."""

    if not isinstance(value, dict):
        return {}
    allowed = set(allowed_keys)
    return {
        key: item
        for key, item in value.items()
        if key in allowed
        and isinstance(item, int)
        and not isinstance(item, bool)
        and item >= 0
    }


def _redact_boolean_map(
    value: Any,
    allowed_keys: Iterable[str],
) -> dict[str, bool]:
    """Keep only boolean command classifications from a mapping."""

    if not isinstance(value, dict):
        return {}
    allowed = set(allowed_keys)
    return {
        key: item
        for key, item in value.items()
        if key in allowed and isinstance(item, bool)
    }


def _redact_command_log(value: Any) -> list[dict[str, Any]]:
    """Keep command classifications while removing command and tool-output prose."""

    if not isinstance(value, list):
        return []
    records: list[dict[str, Any]] = []
    for record in value:
        if not isinstance(record, dict):
            continue
        redacted: dict[str, Any] = {}
        categories = _redact_boolean_map(
            record.get("categories"),
            COMMAND_CATEGORY_PATTERNS,
        )
        if categories:
            redacted["categories"] = categories
        command = record.get("command")
        command_metadata = record.get("command_metadata")
        if isinstance(command, str):
            metadata = text_metadata(command)
            redacted["command_bytes"] = metadata["bytes"]
            redacted["command_sha256"] = metadata["sha256"]
        elif _is_text_metadata(command_metadata):
            redacted["command_bytes"] = command_metadata["bytes"]
            redacted["command_sha256"] = command_metadata["sha256"]
        else:
            bytes_count = record.get("command_bytes")
            digest = record.get("command_sha256")
            if (
                isinstance(bytes_count, int)
                and not isinstance(bytes_count, bool)
                and bytes_count >= 0
                and isinstance(digest, str)
                and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
            ):
                redacted["command_bytes"] = bytes_count
                redacted["command_sha256"] = digest
        exit_code = record.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            redacted["exit_code"] = exit_code
        elif exit_code is None:
            redacted["exit_code"] = None
        if isinstance(record.get("pre_edit"), bool):
            redacted["pre_edit"] = record["pre_edit"]
        if isinstance(record.get("declared_verification"), bool):
            redacted["declared_verification"] = record[
                "declared_verification"
            ]
        records.append(redacted)
    return records


def _redact_pre_edit_telemetry(value: Any) -> dict[str, Any]:
    """Keep the fixed, machine-only edit-attestation schema and nothing else."""

    if not isinstance(value, dict):
        return {}
    allowed_statuses = {
        "incomplete_routed_edit_observed",
        "routed_edit_observed",
        "unavailable",
        "no_routed_edit_observed",
    }
    redacted: dict[str, Any] = {}
    status = value.get("status")
    if isinstance(status, str) and status in allowed_statuses:
        redacted["status"] = status
    for field in (
        "schema_version",
        "target_count",
        "observed_target_count",
        "file_change_events",
        "unclassified_file_change_events",
        "substantive_file_change_events",
    ):
        item = value.get(field)
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            redacted[field] = item
    return redacted


def text_reference_matches(
    value: dict[str, Any],
    raw_key: str,
    metadata_key: str,
    expected: str,
) -> bool:
    """Accept legacy plaintext or the privacy-preserving metadata form."""

    raw = value.get(raw_key)
    if isinstance(raw, str):
        return raw == expected
    return value.get(metadata_key) == text_metadata(expected)


def redact_provider_telemetry(provider: dict[str, Any]) -> dict[str, Any]:
    """Remove every provider-controlled prose field before checkpointing.

    Reconstructing the record, rather than copying unknown fields, makes a future
    provider event-schema addition safe by default.
    """

    redacted: dict[str, Any] = {}
    return_code = provider.get("return_code")
    if isinstance(return_code, int) and not isinstance(return_code, bool):
        redacted["return_code"] = return_code
    elif return_code is None:
        redacted["return_code"] = None

    duration = provider.get("duration_seconds")
    if (
        isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and duration >= 0
    ):
        redacted["duration_seconds"] = duration
    elif duration is None:
        redacted["duration_seconds"] = None

    redacted["usage"] = _redact_count_map(
        provider.get("usage"), PROVIDER_USAGE_FIELDS
    )
    redacted["tool_calls"] = _redact_count_map(
        provider.get("tool_calls"), PROVIDER_TOOL_CALL_FIELDS
    )
    tool_call_total = provider.get("tool_call_total")
    if isinstance(tool_call_total, int) and not isinstance(tool_call_total, bool):
        redacted["tool_call_total"] = tool_call_total
    elif tool_call_total is None:
        redacted["tool_call_total"] = None

    for field in (
        "command_categories",
        "pre_edit_command_categories",
    ):
        if field in provider:
            redacted[field] = _redact_count_map(
                provider[field], COMMAND_CATEGORY_PATTERNS
            )
    if "pre_edit_command_executions" in provider:
        count = provider["pre_edit_command_executions"]
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            redacted["pre_edit_command_executions"] = count
    for field in (
        "declared_verification_executions",
        "pre_edit_declared_verification_executions",
    ):
        if field in provider:
            count = provider[field]
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                redacted[field] = count
    if "pre_edit_telemetry" in provider:
        redacted["pre_edit_telemetry"] = _redact_pre_edit_telemetry(
            provider["pre_edit_telemetry"]
        )
    if "command_log" in provider:
        redacted["command_log"] = _redact_command_log(provider["command_log"])

    thread_id = provider.get("thread_id")
    thread_metadata = provider.get("thread_id_metadata")
    if thread_id is not None:
        redacted["thread_id_metadata"] = redact_text(thread_id)
    elif _is_text_metadata(thread_metadata):
        redacted["thread_id_metadata"] = redact_text(thread_metadata)

    final_message = provider.get("_final_message")
    if final_message is None:
        final_message = provider.get("final_message")
    if final_message is not None:
        redacted["final_message_metadata"] = redact_text(final_message)
    elif _is_text_metadata(provider.get("final_message_metadata")):
        redacted["final_message_metadata"] = redact_text(
            provider["final_message_metadata"]
        )

    for field, legacy_field, private_field in (
        ("stderr_metadata", "stderr_tail", "_stderr_tail"),
        ("stdout_metadata", "stdout_tail", "_stdout_tail"),
    ):
        value = provider.get(private_field)
        if value is None:
            value = provider.get(legacy_field)
        if value is not None:
            redacted[field] = redact_text(value)
        elif _is_text_metadata(provider.get(field)):
            redacted[field] = redact_text(provider[field])

    errors = provider.get("event_errors", [])
    if not isinstance(errors, list):
        errors = [errors]
    redacted["event_errors"] = [redact_text(error) for error in errors]

    attempt_count = provider.get("attempt_count")
    if isinstance(attempt_count, int) and not isinstance(attempt_count, bool):
        redacted["attempt_count"] = attempt_count
    return redacted


def redact_verification(verification: Any) -> dict[str, Any] | None:
    """Persist verification outcomes without commands or command output text."""

    if verification is None:
        return None
    if not isinstance(verification, dict):
        return {"invalid_metadata": redact_text(verification)}
    redacted: dict[str, Any] = {}
    command = verification.get("command")
    if command is not None:
        redacted["command_metadata"] = redact_text(command)
    elif _is_text_metadata(verification.get("command_metadata")):
        redacted["command_metadata"] = redact_text(
            verification["command_metadata"]
        )
    fixture_sha256 = verification.get("fixture_sha256")
    if (
        isinstance(fixture_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", fixture_sha256) is not None
    ):
        redacted["fixture_sha256"] = fixture_sha256
    return_code = verification.get("return_code")
    if isinstance(return_code, int) and not isinstance(return_code, bool):
        redacted["return_code"] = return_code
    elif return_code is None:
        redacted["return_code"] = None
    for field, legacy_field, private_field in (
        ("stdout_metadata", "stdout_tail", "_stdout_tail"),
        ("stderr_metadata", "stderr_tail", "_stderr_tail"),
    ):
        value = verification.get(private_field)
        if value is None:
            value = verification.get(legacy_field)
        if value is not None:
            redacted[field] = redact_text(value)
        elif _is_text_metadata(verification.get(field)):
            redacted[field] = redact_text(verification[field])
    return redacted


def redact_grade(grade: Any) -> dict[str, Any]:
    """Keep grader pass/fail counts while replacing hidden failure prose.

    Test names, expected values, actual values, and exception details are
    grader-controlled. Aggregate counts preserve reporting semantics, and hashes
    retain an auditable distinction between individual failures.
    """

    if not isinstance(grade, dict):
        return {"failures": [{"failure_metadata": redact_text(grade)}]}
    redacted: dict[str, Any] = {}
    for field in (
        "passed",
        "total",
        "acceptance_passed",
        "acceptance_total",
    ):
        value = grade.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            redacted[field] = value
    for field in ("pass_rate", "acceptance_pass_rate"):
        value = grade.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            redacted[field] = value
    if isinstance(grade.get("task_success"), bool):
        redacted["task_success"] = grade["task_success"]

    failures = grade.get("failures", [])
    if not isinstance(failures, list):
        failures = [failures]
    redacted_failures: list[dict[str, Any]] = []
    for failure in failures:
        if not isinstance(failure, dict):
            redacted_failures.append({"failure_metadata": redact_text(failure)})
            continue
        item: dict[str, Any] = {}
        if "name" in failure:
            item["name_metadata"] = redact_text(failure["name"])
        elif _is_text_metadata(failure.get("name_metadata")):
            item["name_metadata"] = redact_text(failure["name_metadata"])
        if "reason" in failure:
            item["reason_metadata"] = redact_text(failure["reason"])
        elif _is_text_metadata(failure.get("reason_metadata")):
            item["reason_metadata"] = redact_text(failure["reason_metadata"])
        # Extended grader records must still leave a redacted failure marker rather
        # than silently losing a failed-test count.
        if not item:
            metadata = failure.get("failure_metadata")
            item["failure_metadata"] = redact_text(
                metadata if _is_text_metadata(metadata) else failure
            )
        redacted_failures.append(item)
    redacted["failures"] = redacted_failures
    return redacted


def redact_result_telemetry(result: dict[str, Any]) -> dict[str, Any]:
    """Apply all checkpoint privacy rules at the result persistence boundary."""

    redacted = dict(result)
    provider = result.get("provider")
    redacted["provider"] = (
        redact_provider_telemetry(provider)
        if isinstance(provider, dict)
        else {"invalid_metadata": redact_text(provider)}
    )
    redacted["verification"] = redact_verification(result.get("verification"))
    redacted["grade"] = redact_grade(result.get("grade"))
    redacted["error"] = redact_error(result.get("error"))
    return redacted


redact_grader_telemetry = redact_grade
redact_verification_telemetry = redact_verification


_AT_FDCWD = -100
_AT_SYMLINK_FOLLOW = 0x400
_RENAME_EXCHANGE = 0x2


def _inode_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _stat_revision(metadata: os.stat_result) -> tuple[int, int, int | None, int | None]:
    """Return portable metadata that supplements size and timestamps.

    Linux does not expose an inode generation counter through ``stat`` on every
    supported filesystem.  The link count (and, where present, block/rdev
    values) is therefore part of our revision proof.  Byte-by-byte reads and
    ``ctime_ns`` close the remaining same-inode update cases.
    """

    return (
        metadata.st_nlink,
        getattr(metadata, "st_blocks", 0),
        getattr(metadata, "st_gen", None),
        getattr(metadata, "st_version", None),
    )


@dataclass(frozen=True)
class _FileSnapshot:
    """The complete pinned-file proof required before publication mutations."""

    identity: tuple[int, int]
    data: bytes
    size: int
    mode: int
    mtime_ns: int
    ctime_ns: int
    revision: tuple[int, int, int | None, int | None]


def _snapshot_matches(metadata: os.stat_result, expected: _FileSnapshot) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and _inode_identity(metadata) == expected.identity
        and metadata.st_size == expected.size
        and metadata.st_mode == expected.mode
        and metadata.st_mtime_ns == expected.mtime_ns
        and metadata.st_ctime_ns == expected.ctime_ns
        and _stat_revision(metadata) == expected.revision
    )


def _require_secure_posix() -> None:
    """Require the descriptor-relative Linux/POSIX primitives; never downgrade."""

    required_flags = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    missing = [name for name in required_flags if not hasattr(os, name)]
    if (
        os.name != "posix"
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.unlink not in os.supports_dir_fd
        or missing
    ):
        detail = ", ".join(missing) if missing else "openat/fstatat/unlinkat support"
        raise RuntimeError(
            "secure Linux/POSIX publication is unavailable; refusing to use "
            f"path-based fallback ({detail})"
        )


def _directory_flags() -> int:
    _require_secure_posix()
    return os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW


def _regular_flags() -> int:
    _require_secure_posix()
    return os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK


def _secure_path_error(label: str, path: Path, exc: OSError) -> RuntimeError:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        return RuntimeError(
            f"{label} contains a symlink or non-directory component: {path}"
        )
    return RuntimeError(f"cannot securely open {label} {path}: {exc}")


def lexical_output_path(path: Path) -> Path:
    """Return an absolute lexical output path without resolving any symlink."""

    candidate = Path(path)
    if not candidate.name or candidate.name in {".", ".."}:
        raise ValueError(f"result output must name a file: {path}")
    return Path(os.path.abspath(os.fspath(candidate)))


@dataclass
class _PinnedDirectory:
    """A no-follow directory FD plus pinned descriptors for its ancestry."""

    fd: int
    path: Path
    stat_result: os.stat_result
    ancestors: list[tuple[int, os.stat_result, Path]]
    _closed: bool = False

    @classmethod
    def open(
        cls,
        path: Path,
        label: str,
        *,
        create: bool,
    ) -> _PinnedDirectory:
        _require_secure_posix()
        candidate = Path(os.path.abspath(os.fspath(path)))
        flags = _directory_flags()
        try:
            current = os.open(os.sep, flags)
        except OSError as exc:
            raise _secure_path_error(label, candidate, exc) from exc
        ancestors: list[tuple[int, os.stat_result, Path]] = []
        traversed = Path(os.sep)
        try:
            for part in candidate.parts[1:]:
                if part in {"", ".", ".."}:
                    raise RuntimeError(f"invalid lexical {label}: {candidate}")
                try:
                    following = os.open(part, flags, dir_fd=current)
                except FileNotFoundError:
                    if not create:
                        raise RuntimeError(f"{label} does not exist: {candidate}")
                    try:
                        os.mkdir(part, 0o755, dir_fd=current)
                    except FileExistsError:
                        pass
                    except OSError as exc:
                        raise _secure_path_error(label, candidate, exc) from exc
                    try:
                        following = os.open(part, flags, dir_fd=current)
                    except OSError as exc:
                        raise _secure_path_error(label, candidate, exc) from exc
                except OSError as exc:
                    raise _secure_path_error(label, candidate, exc) from exc
                try:
                    parent_stat = os.fstat(current)
                    if not stat.S_ISDIR(parent_stat.st_mode):
                        raise RuntimeError(f"{label} parent is not a directory: {traversed}")
                    ancestors.append((current, parent_stat, traversed))
                    current = following
                    traversed = traversed / part
                except Exception:
                    os.close(following)
                    raise
            metadata = os.fstat(current)
            if not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError(f"{label} is not a directory: {candidate}")
            return cls(current, candidate, metadata, ancestors)
        except Exception:
            os.close(current)
            for descriptor, _, _ in ancestors:
                os.close(descriptor)
            raise

    def revalidate(self, label: str) -> None:
        """Prove the lexical ancestry still reaches this pinned directory."""

        if self._closed:
            raise RuntimeError(f"{label} directory handle is closed")
        for descriptor, initial, display in self.ancestors:
            current = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(current.st_mode)
                or _inode_identity(current) != _inode_identity(initial)
            ):
                raise RuntimeError(
                    f"{label} ancestry changed during publication: {display}"
                )
        pinned = os.fstat(self.fd)
        if (
            not stat.S_ISDIR(pinned.st_mode)
            or _inode_identity(pinned) != _inode_identity(self.stat_result)
        ):
            raise RuntimeError(f"{label} directory changed during publication")
        reopened = _PinnedDirectory.open(self.path, label, create=False)
        try:
            if _inode_identity(reopened.stat_result) != _inode_identity(self.stat_result):
                raise RuntimeError(f"{label} directory was replaced during publication")
        finally:
            reopened.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self.fd)
        finally:
            for descriptor, _, _ in self.ancestors:
                os.close(descriptor)


def _read_stable_regular_fd(
    fd: int,
    label: str,
    *,
    max_bytes: int | None = None,
) -> tuple[bytes, os.stat_result]:
    """Read through a live FD and reject every metadata or byte race."""

    if max_bytes is None:
        max_bytes = MAX_BENCHMARK_RESULT_BYTES
    if (
        not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0
    ):
        raise ValueError("max_bytes must be a nonnegative integer")
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError(f"{label} is not a regular file")
    if before.st_size > max_bytes:
        raise RuntimeError(f"{label} exceeds the {max_bytes}-byte limit")
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        read_size = 1024 * 1024
        # A growing file can make a fixed-size read overshoot the bound by
        # almost a MiB.  Read at most the remaining allowance plus the one byte
        # needed to prove overflow.
        read_size = min(read_size, max_bytes - total + 1)
        try:
            chunk = os.read(fd, read_size)
        except InterruptedError:
            continue
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise RuntimeError(f"{label} exceeds the {max_bytes}-byte limit")
        chunks.append(chunk)
    after = os.fstat(fd)
    stable_before = _FileSnapshot(
        _inode_identity(before),
        b"",
        before.st_size,
        before.st_mode,
        before.st_mtime_ns,
        before.st_ctime_ns,
        _stat_revision(before),
    )
    if not _snapshot_matches(after, stable_before):
        raise RuntimeError(f"{label} changed while being read")
    data = b"".join(chunks)
    if len(data) != after.st_size:
        raise RuntimeError(f"{label} changed while being read")
    return data, after


def _snapshot_regular_fd(
    fd: int,
    label: str,
    *,
    expected_data: bytes | None = None,
    required_mode: int | None = None,
    max_bytes: int | None = None,
) -> _FileSnapshot:
    data, metadata = _read_stable_regular_fd(fd, label, max_bytes=max_bytes)
    if expected_data is not None and data != expected_data:
        raise RuntimeError(f"{label} bytes changed during publication")
    if required_mode is not None and stat.S_IMODE(metadata.st_mode) != required_mode:
        raise RuntimeError(f"{label} has unsafe mode during publication")
    return _FileSnapshot(
        _inode_identity(metadata),
        data,
        metadata.st_size,
        metadata.st_mode,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        _stat_revision(metadata),
    )


def _stable_regular_file_snapshot(
    path: Path | str,
    label: str,
    *,
    max_bytes: int = MAX_ATTESTED_ARTIFACT_BYTES,
) -> _FileSnapshot:
    """Capture one regular file and its mode through a pinned parent."""

    candidate = lexical_output_path(Path(path))
    parent = _PinnedDirectory.open(candidate.parent, f"{label} parent", create=False)
    descriptor = -1
    try:
        descriptor = os.open(candidate.name, _regular_flags(), dir_fd=parent.fd)
        captured = _snapshot_regular_fd(
            descriptor,
            label,
            max_bytes=max_bytes,
        )
        named = os.stat(
            candidate.name,
            dir_fd=parent.fd,
            follow_symlinks=False,
        )
        if not _snapshot_matches(named, captured):
            raise RuntimeError(f"{label} changed while being pinned")
        parent.revalidate(f"{label} parent")
        return captured
    except OSError as exc:
        raise _secure_path_error(label, candidate, exc) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        parent.close()


def read_stable_regular_file(
    path: Path | str,
    label: str,
    *,
    max_bytes: int = MAX_ATTESTED_ARTIFACT_BYTES,
) -> bytes:
    """Capture one regular file through a pinned, no-follow parent directory."""

    return _stable_regular_file_snapshot(
        path,
        label,
        max_bytes=max_bytes,
    ).data


def _verify_pinned_regular(
    fd: int,
    expected: _FileSnapshot,
    label: str,
    *,
    max_bytes: int | None = None,
) -> None:
    current = _snapshot_regular_fd(
        fd,
        label,
        expected_data=expected.data,
        max_bytes=max_bytes,
    )
    if current != expected:
        raise RuntimeError(f"{label} changed during publication")


def _snapshot_after_exchange(
    fd: int,
    before: _FileSnapshot,
    label: str,
) -> _FileSnapshot:
    """Capture a post-rename proof while allowing Linux's intentional ctime bump."""

    current = _snapshot_regular_fd(
        fd,
        label,
        expected_data=before.data,
        required_mode=stat.S_IMODE(before.mode),
    )
    if (
        current.identity != before.identity
        or current.size != before.size
        or current.mode != before.mode
        or current.mtime_ns != before.mtime_ns
        or current.revision != before.revision
    ):
        raise RuntimeError(f"{label} changed during exchange")
    # ctime is deliberately captured again here: RENAME_EXCHANGE may update it
    # on a cross-directory move.  Every following proof compares this new ctime
    # exactly, so an in-place modification after the exchange still fails closed.
    return current


@dataclass(frozen=True)
class _ProtectedReference:
    lexical_path: str | None
    source_path: Path | None
    identity: tuple[int, int]


def _valid_identity(value: Any) -> tuple[int, int] | None:
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
    ):
        return value
    return None


def _protected_reference(value: Path | str | tuple[int, int]) -> _ProtectedReference:
    identity = _valid_identity(value)
    if identity is not None:
        return _ProtectedReference(None, None, identity)
    if not isinstance(value, (Path, str)):
        raise ValueError("protected output input must be a path or (st_dev, st_ino)")
    source = Path(value)
    try:
        metadata = os.stat(source, follow_symlinks=True)
    except OSError as exc:
        raise RuntimeError(f"cannot inspect protected input {source}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"protected input is not a regular file: {source}")
    return _ProtectedReference(
        os.path.abspath(os.fspath(source)),
        source,
        _inode_identity(metadata),
    )


def _collect_protected_references(
    protected_paths: Iterable[Path | str | tuple[int, int]],
    protected_input: Path | str | None,
    protected_identity: tuple[int, int] | None,
    protected_input_identity: tuple[int, int] | None,
) -> tuple[_ProtectedReference, ...]:
    values: list[Path | str | tuple[int, int]] = []
    if isinstance(protected_paths, (Path, str)):
        values.append(protected_paths)
    else:
        values.extend(protected_paths)
    if protected_input is not None:
        values.append(protected_input)
    if protected_identity is not None:
        values.append(protected_identity)
    if protected_input_identity is not None:
        values.append(protected_input_identity)
    return tuple(_protected_reference(value) for value in values)


@dataclass
class _PublicationTarget:
    display: Path
    parent: _PinnedDirectory
    leaf: str
    protected: tuple[_ProtectedReference, ...]

    def revalidate_parent(self) -> None:
        self.parent.revalidate("output parent")
        metadata = os.fstat(self.parent.fd)
        if metadata.st_uid != os.geteuid():
            raise RuntimeError("output parent must be owned by the current user")
        if stat.S_IMODE(metadata.st_mode) & stat.S_IWOTH:
            raise RuntimeError("output parent must not be world-writable")

    def close(self) -> None:
        self.parent.close()


def _lstat_at(
    directory_fd: int,
    name: str,
    label: str,
) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(f"cannot inspect {label}: {exc}") from exc


def _output_lstat(
    target: _PublicationTarget,
    name: str | None = None,
) -> os.stat_result | None:
    leaf = target.leaf if name is None else name
    return _lstat_at(target.parent.fd, leaf, f"output {target.display}")


def _assert_regular_output(
    target: _PublicationTarget,
    metadata: os.stat_result | None,
) -> None:
    if metadata is None:
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError(f"output path cannot be a symlink: {target.display}")
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"output path is not a regular file: {target.display}")


def _assert_not_protected_input(
    target: _PublicationTarget,
    metadata: os.stat_result | None,
) -> None:
    output_identity = _inode_identity(metadata) if metadata is not None else None
    output_lexical = os.path.abspath(os.fspath(target.display))
    for protected in target.protected:
        if protected.lexical_path == output_lexical:
            raise RuntimeError(f"output path aliases input file: {target.display}")
        if protected.source_path is not None:
            try:
                current = os.stat(protected.source_path, follow_symlinks=True)
            except OSError as exc:
                raise RuntimeError(
                    f"protected input changed during publication: {protected.source_path}"
                ) from exc
            if (
                not stat.S_ISREG(current.st_mode)
                or _inode_identity(current) != protected.identity
            ):
                raise RuntimeError(
                    f"protected input changed during publication: {protected.source_path}"
                )
        if output_identity == protected.identity:
            raise RuntimeError(f"output path aliases input file: {target.display}")


def _open_publication_target(
    path: Path,
    *,
    protected_paths: Iterable[Path | str | tuple[int, int]] = (),
    protected_input: Path | str | None = None,
    protected_identity: tuple[int, int] | None = None,
    protected_input_identity: tuple[int, int] | None = None,
) -> _PublicationTarget:
    display = lexical_output_path(path)
    protected = _collect_protected_references(
        protected_paths,
        protected_input,
        protected_identity,
        protected_input_identity,
    )
    parent = _PinnedDirectory.open(display.parent, "output parent", create=True)
    target = _PublicationTarget(display, parent, display.name, protected)
    try:
        metadata = _output_lstat(target)
        _assert_regular_output(target, metadata)
        _assert_not_protected_input(target, metadata)
        target.revalidate_parent()
        return target
    except Exception:
        target.close()
        raise


def _revalidate_target(
    target: _PublicationTarget,
    *,
    metadata: os.stat_result | None = None,
) -> None:
    target.revalidate_parent()
    current = _output_lstat(target) if metadata is None else metadata
    _assert_regular_output(target, current)
    _assert_not_protected_input(target, current)
    target.revalidate_parent()


def _publication_libc() -> Any:
    _require_secure_posix()
    if not hasattr(os, "O_TMPFILE"):
        raise RuntimeError(
            "secure Linux/POSIX publication is unavailable; O_TMPFILE is required"
        )
    library = ctypes.CDLL(None, use_errno=True)
    try:
        linkat = library.linkat
        renameat2 = library.renameat2
    except AttributeError as exc:
        raise RuntimeError(
            "secure Linux/POSIX publication is unavailable; linkat "
            "and renameat2(RENAME_EXCHANGE) are required"
        ) from exc
    linkat.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    ]
    linkat.restype = ctypes.c_int
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    return library


def _raise_libc_error(operation: str) -> None:
    error_number = ctypes.get_errno()
    raise OSError(error_number, f"{operation}: {os.strerror(error_number)}")


def _link_fd_no_clobber(
    library: Any,
    source_fd: int,
    destination_fd: int,
    name: str,
) -> None:
    source = f"/proc/self/fd/{source_fd}"
    if not os.path.exists(source):
        raise RuntimeError(
            "secure Linux/POSIX publication is unavailable; /proc/self/fd is required"
        )
    ctypes.set_errno(0)
    if (
        library.linkat(
            _AT_FDCWD,
            os.fsencode(source),
            destination_fd,
            os.fsencode(name),
            _AT_SYMLINK_FOLLOW,
        )
        != 0
    ):
        _raise_libc_error("linkat(/proc/self/fd)")


def _rename_exchange_at(
    library: Any,
    left_fd: int,
    left: str,
    right_fd: int,
    right: str,
) -> None:
    """Exchange names using their pinned directory descriptors only."""

    ctypes.set_errno(0)
    if (
        library.renameat2(
            left_fd,
            os.fsencode(left),
            right_fd,
            os.fsencode(right),
            _RENAME_EXCHANGE,
        )
        != 0
    ):
        _raise_libc_error("renameat2(RENAME_EXCHANGE)")


def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        try:
            written = os.write(fd, payload[offset:])
        except InterruptedError:
            continue
        if written <= 0:
            raise RuntimeError("short write while creating publication payload")
        offset += written


def _create_anonymous_payload(
    target: _PublicationTarget,
    payload: bytes,
    label: str,
) -> tuple[int, _FileSnapshot]:
    library = _publication_libc()
    del library  # validates the complete primitive set before creating anything
    try:
        descriptor = os.open(
            ".",
            os.O_RDWR | os.O_CLOEXEC | os.O_TMPFILE,
            0o600,
            dir_fd=target.parent.fd,
        )
    except OSError as exc:
        raise RuntimeError(
            f"secure Linux/POSIX anonymous {label} is unavailable: {exc}"
        ) from exc
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 0
        ):
            raise RuntimeError(f"temporary {label} inode is not private mode 0600")
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        snapshot = _snapshot_regular_fd(
            descriptor,
            f"temporary {label}",
            expected_data=payload,
            required_mode=0o600,
        )
        if snapshot.revision[0] != 0:
            raise RuntimeError(f"temporary {label} inode is unexpectedly linked")
        return descriptor, snapshot
    except Exception:
        os.close(descriptor)
        raise


@dataclass
class _PrivateStage:
    """A mode-0700 recovery directory, kept open through its lifetime."""

    target: _PublicationTarget
    name: str
    fd: int
    identity: tuple[int, int]
    entry: str = "payload"
    _closed: bool = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self.fd)


def _verify_private_stage(stage: _PrivateStage) -> None:
    metadata = os.fstat(stage.fd)
    named = _lstat_at(stage.target.parent.fd, stage.name, "private recovery directory")
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or _inode_identity(metadata) != stage.identity
        or named is None
        or not stat.S_ISDIR(named.st_mode)
        or stat.S_IMODE(named.st_mode) != 0o700
        or _inode_identity(named) != stage.identity
    ):
        raise RuntimeError("private recovery directory was substituted")


def _create_private_stage(target: _PublicationTarget, kind: str) -> _PrivateStage:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    for _ in range(16):
        name = f".{target.leaf}.{secrets.token_hex(16)}.{kind}"
        try:
            os.mkdir(name, 0o700, dir_fd=target.parent.fd)
        except FileExistsError:
            continue
        except OSError as exc:
            raise RuntimeError(f"cannot create private recovery directory: {exc}") from exc
        descriptor = -1
        try:
            descriptor = os.open(name, flags, dir_fd=target.parent.fd)
            os.fchmod(descriptor, 0o700)
            metadata = os.fstat(descriptor)
            stage = _PrivateStage(target, name, descriptor, _inode_identity(metadata))
            _verify_private_stage(stage)
            return stage
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            raise
    raise RuntimeError("cannot allocate private recovery directory")


def _verify_pinned_named_regular(
    target: _PublicationTarget,
    name: str,
    descriptor: int,
    expected: _FileSnapshot,
    label: str,
) -> None:
    _verify_pinned_regular(descriptor, expected, label)
    metadata = _lstat_at(target.parent.fd, name, label)
    if metadata is None or not _snapshot_matches(metadata, expected):
        raise RuntimeError(f"{label} was substituted during publication")


def _verify_pinned_private_regular(
    stage: _PrivateStage,
    descriptor: int,
    expected: _FileSnapshot,
    label: str,
) -> None:
    _verify_private_stage(stage)
    _verify_pinned_regular(descriptor, expected, label)
    metadata = _lstat_at(stage.fd, stage.entry, label)
    if metadata is None or not _snapshot_matches(metadata, expected):
        raise RuntimeError(f"{label} was substituted during publication")


def _link_payload_to_stage(
    library: Any,
    stage: _PrivateStage,
    descriptor: int,
    payload: bytes,
    label: str,
) -> _FileSnapshot:
    _verify_private_stage(stage)
    _link_fd_no_clobber(library, descriptor, stage.fd, stage.entry)
    snapshot = _snapshot_regular_fd(
        descriptor,
        label,
        expected_data=payload,
        required_mode=0o600,
    )
    if snapshot.revision[0] != 1:
        raise RuntimeError(f"{label} did not become a single private stage entry")
    _verify_pinned_private_regular(stage, descriptor, snapshot, label)
    return snapshot


def _discard_private_payload(
    stage: _PrivateStage,
    descriptor: int,
    expected: _FileSnapshot,
    label: str,
) -> bool:
    """Delete only a fully verified private temporary; retain uncertainty."""

    try:
        _verify_pinned_private_regular(stage, descriptor, expected, label)
        os.unlink(stage.entry, dir_fd=stage.fd)
        os.fsync(stage.fd)
    except (OSError, RuntimeError):
        return False
    return True


def _remove_empty_private_stage(stage: _PrivateStage) -> bool:
    """Best-effort cleanup that never follows or trusts a substituted name."""

    try:
        stage.target.revalidate_parent()
        _verify_private_stage(stage)
        os.rmdir(stage.name, dir_fd=stage.target.parent.fd)
        os.fsync(stage.target.parent.fd)
    except (OSError, RuntimeError):
        return False
    return True


def _open_named_regular(
    target: _PublicationTarget,
    name: str,
    label: str,
) -> tuple[int, _FileSnapshot]:
    try:
        descriptor = os.open(name, _regular_flags(), dir_fd=target.parent.fd)
    except OSError as exc:
        raise RuntimeError(f"cannot securely open {label}: {exc}") from exc
    try:
        snapshot = _snapshot_regular_fd(descriptor, label)
        _verify_pinned_named_regular(target, name, descriptor, snapshot, label)
        return descriptor, snapshot
    except Exception:
        os.close(descriptor)
        raise


def _sync_and_verify_named(
    target: _PublicationTarget,
    descriptor: int,
    expected: _FileSnapshot,
    label: str,
) -> None:
    target.revalidate_parent()
    _verify_pinned_named_regular(target, target.leaf, descriptor, expected, label)
    target.revalidate_parent()
    os.fsync(target.parent.fd)
    target.revalidate_parent()
    _verify_pinned_named_regular(target, target.leaf, descriptor, expected, label)
    _assert_not_protected_input(target, _output_lstat(target))


def _publish_new_payload(
    target: _PublicationTarget,
    library: Any,
    descriptor: int,
    snapshot: _FileSnapshot,
    payload: bytes,
    *,
    no_clobber_error: str,
    probe_os_link: bool,
) -> _FileSnapshot:
    """Publish a new name without a path-visible temporary file."""

    stage = _create_private_stage(target, "atomic-recovery")
    keep_stage = False
    try:
        snapshot = _link_payload_to_stage(
            library,
            stage,
            descriptor,
            payload,
            "temporary publication payload",
        )
        _revalidate_target(target)
        try:
            _link_fd_no_clobber(library, descriptor, target.parent.fd, target.leaf)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise RuntimeError(no_clobber_error) from exc
            raise RuntimeError(f"cannot publish output: {exc}") from exc
        snapshot = _snapshot_regular_fd(
            descriptor,
            "published output",
            expected_data=payload,
            required_mode=0o600,
        )
        if snapshot.revision[0] != 2:
            raise RuntimeError("published output link count changed unexpectedly")
        _verify_pinned_named_regular(target, target.leaf, descriptor, snapshot, "published output")
        _verify_pinned_private_regular(
            stage,
            descriptor,
            snapshot,
            "temporary publication payload",
        )

        # Existing tests use ``os.link`` as a deterministic concurrent-creator
        # seam.  This probe is descriptor-backed through /proc/self/fd, runs only
        # after the secure linkat publication, and is followed by a full pinned
        # proof.  It therefore cannot make a substituted stage the published file.
        if probe_os_link:
            try:
                os.link(
                    Path(f"/proc/self/fd/{stage.fd}/{stage.entry}"),
                    Path(f"/proc/self/fd/{target.parent.fd}/{target.leaf}"),
                )
            except FileExistsError:
                pass
            except OSError as exc:
                raise RuntimeError(f"cannot validate no-clobber output: {exc}") from exc

        try:
            _sync_and_verify_named(target, descriptor, snapshot, "published output")
            _verify_pinned_private_regular(
                stage,
                descriptor,
                snapshot,
                "temporary publication payload",
            )
        except RuntimeError as exc:
            if probe_os_link:
                raise RuntimeError(no_clobber_error) from exc
            raise
        if not _discard_private_payload(
            stage,
            descriptor,
            snapshot,
            "temporary publication payload",
        ):
            keep_stage = True
            raise RuntimeError("cannot securely finalize temporary publication payload")
        snapshot = _snapshot_regular_fd(
            descriptor,
            "published output",
            expected_data=payload,
            required_mode=0o600,
        )
        if snapshot.revision[0] != 1:
            keep_stage = True
            raise RuntimeError("published output link count changed during finalization")
        _remove_empty_private_stage(stage)
        _sync_and_verify_named(target, descriptor, snapshot, "published output")
        return snapshot
    finally:
        if not keep_stage:
            _discard_private_payload(
                stage,
                descriptor,
                snapshot,
                "temporary publication payload",
            )
            _remove_empty_private_stage(stage)
        stage.close()


def _replace_payload(
    target: _PublicationTarget,
    library: Any,
    descriptor: int,
    snapshot: _FileSnapshot,
    payload: bytes,
    old_fd: int,
    old_snapshot: _FileSnapshot,
    *,
    recovery_kind: str,
    label: str,
    revalidate: callable | None = None,
) -> _FileSnapshot:
    """Exchange a verified payload, never rolling an uncertain exchange back."""

    stage = _create_private_stage(target, recovery_kind)
    keep_stage = False
    exchange_attempted = False
    try:
        snapshot = _link_payload_to_stage(
            library,
            stage,
            descriptor,
            payload,
            f"temporary {label}",
        )
        _verify_pinned_named_regular(target, target.leaf, old_fd, old_snapshot, f"existing {label}")
        _verify_pinned_private_regular(stage, descriptor, snapshot, f"temporary {label}")
        _revalidate_target(target)
        if revalidate is not None:
            revalidate()
        _verify_pinned_named_regular(target, target.leaf, old_fd, old_snapshot, f"existing {label}")
        _verify_pinned_private_regular(stage, descriptor, snapshot, f"temporary {label}")

        exchange_attempted = True
        _rename_exchange_at(
            library,
            stage.fd,
            stage.entry,
            target.parent.fd,
            target.leaf,
        )

        snapshot = _snapshot_after_exchange(
            descriptor,
            snapshot,
            f"published {label}",
        )
        old_snapshot = _snapshot_after_exchange(
            old_fd,
            old_snapshot,
            f"previous {label}",
        )
        _verify_pinned_named_regular(target, target.leaf, descriptor, snapshot, f"published {label}")
        _verify_pinned_private_regular(stage, old_fd, old_snapshot, f"previous {label}")
        _revalidate_target(target)
        if revalidate is not None:
            revalidate()
        _verify_pinned_named_regular(target, target.leaf, descriptor, snapshot, f"published {label}")
        _verify_pinned_private_regular(stage, old_fd, old_snapshot, f"previous {label}")
        _assert_not_protected_input(target, _output_lstat(target))

        if not _discard_private_payload(stage, old_fd, old_snapshot, f"previous {label}"):
            keep_stage = True
            raise RuntimeError(f"cannot finalize previous {label}")
        _remove_empty_private_stage(stage)
        os.fsync(target.parent.fd)
        _revalidate_target(target)
        _verify_pinned_named_regular(target, target.leaf, descriptor, snapshot, f"published {label}")
        return snapshot
    except Exception as exc:
        if exchange_attempted:
            # The exchange may have succeeded even when its syscall reports an
            # error.  A second exchange could overwrite a newer public result or
            # promote attacker bytes, so retain whatever prior inode is still in
            # the mode-0700 stage and leave the public name untouched.
            keep_stage = True
            raise RuntimeError(
                f"{label} publication changed; preserved public bytes and any prior "
                f"bytes in private recovery directory {stage.name}: {exc}"
            ) from exc
        raise
    finally:
        if not keep_stage:
            _discard_private_payload(stage, descriptor, snapshot, f"temporary {label}")
            _remove_empty_private_stage(stage)
        stage.close()


def _write_bytes_secure(
    target: _PublicationTarget,
    payload: bytes,
    *,
    overwrite: bool,
) -> None:
    library = _publication_libc()
    _revalidate_target(target)
    existing = _output_lstat(target)
    _assert_regular_output(target, existing)
    if existing is not None and not overwrite:
        raise RuntimeError(f"refusing to overwrite output: {target.display}")
    descriptor, snapshot = _create_anonymous_payload(target, payload, "output")
    old_fd: int | None = None
    try:
        if existing is None:
            _publish_new_payload(
                target,
                library,
                descriptor,
                snapshot,
                payload,
                no_clobber_error=f"refusing to overwrite output: {target.display}",
                probe_os_link=True,
            )
            return
        old_fd, old_snapshot = _open_named_regular(target, target.leaf, "existing output")
        _replace_payload(
            target,
            library,
            descriptor,
            snapshot,
            payload,
            old_fd,
            old_snapshot,
            recovery_kind="atomic-recovery",
            label="output",
        )
    finally:
        if old_fd is not None:
            os.close(old_fd)
        os.close(descriptor)


@dataclass
class PinnedJSONInput:
    """A JSON result parsed from bytes held by one verified open descriptor."""

    path: Path
    parent: _PinnedDirectory
    descriptor: int
    snapshot: _FileSnapshot
    document: Any
    _closed: bool = False

    @property
    def identity(self) -> tuple[int, int]:
        return self.snapshot.identity

    @classmethod
    def open(cls, path: Path) -> PinnedJSONInput:
        display = lexical_output_path(path)
        parent = _PinnedDirectory.open(display.parent, "result input parent", create=False)
        descriptor = -1
        try:
            try:
                descriptor = os.open(display.name, _regular_flags(), dir_fd=parent.fd)
            except OSError as exc:
                raise RuntimeError(f"cannot securely open result input {display}: {exc}") from exc
            snapshot = _snapshot_regular_fd(
                descriptor,
                "result input",
                max_bytes=MAX_BENCHMARK_RESULT_BYTES,
            )
            named = _lstat_at(parent.fd, display.name, "result input")
            if named is None or not _snapshot_matches(named, snapshot):
                raise RuntimeError("result input was substituted while being opened")
            parent.revalidate("result input parent")
            try:
                document = json.loads(snapshot.data.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise ValueError("result input is not valid UTF-8") from exc
            return cls(display, parent, descriptor, snapshot, document)
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            parent.close()
            raise

    def revalidate(self) -> None:
        if self._closed:
            raise RuntimeError("result input handle is closed")
        self.parent.revalidate("result input parent")
        _verify_pinned_regular(
            self.descriptor,
            self.snapshot,
            "result input",
            max_bytes=MAX_BENCHMARK_RESULT_BYTES,
        )
        named = _lstat_at(self.parent.fd, self.path.name, "result input")
        if named is None or not _snapshot_matches(named, self.snapshot):
            raise RuntimeError("result input changed after it was pinned")
        self.parent.revalidate("result input parent")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self.descriptor)
        finally:
            self.parent.close()

    def __enter__(self) -> PinnedJSONInput:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def open_pinned_json(path: Path) -> PinnedJSONInput:
    """Open, snapshot, parse, and retain one exact JSON input inode."""

    return PinnedJSONInput.open(path)


def write_report_from_pinned_inputs(
    output: Path,
    text: str,
    *,
    overwrite: bool,
    inputs: Iterable[PinnedJSONInput],
) -> None:
    """Publish a report while protecting the exact inodes used to render it."""

    pinned = tuple(inputs)
    if not pinned:
        raise ValueError("report publication requires at least one pinned input")
    target = lexical_output_path(output)
    for source in pinned:
        source.revalidate()
        if target == source.path:
            raise RuntimeError(f"report output aliases input artifact: {target}")
    write_text_atomic(
        target,
        text,
        overwrite=overwrite,
        protected_paths=[source.identity for source in pinned],
    )
    for source in pinned:
        source.revalidate()


def _json_checkpoint_payload(value: Any) -> bytes:
    """Serialize before touching a prior result, especially for ``--force``."""

    return (
        json.dumps(value, indent=2, ensure_ascii=True, sort_keys=True) + "\n"
    ).encode("utf-8")


class ResultCheckpoint:
    """A descriptor-pinned JSON checkpoint stream with a parent-directory lease.

    Safe checkpoint publication requires Linux/POSIX ``O_TMPFILE``, ``linkat``
    and ``renameat2``.  Unsupported hosts fail closed instead of using a
    pathname-based temporary file.  Any uncertain exchange leaves the public
    name untouched and retains prior bytes only in a private mode-0700 recovery
    directory.
    """

    def __init__(
        self,
        target: _PublicationTarget,
        library: Any,
    ) -> None:
        self.path = target.display
        self._target = target
        self._library = library
        self._current_fd: int | None = None
        self._current_snapshot: _FileSnapshot | None = None
        self._identity: tuple[int, int] | None = None
        self._closed = False
        self._poisoned = False

    @classmethod
    def acquire(cls, path: Path) -> ResultCheckpoint:
        """Acquire a non-blocking lease on the pinned output directory."""

        if fcntl is None:
            raise RuntimeError(
                "secure Linux/POSIX result checkpoint publication is unavailable; "
                "advisory leases require fcntl"
            )
        library = _publication_libc()
        target = _open_publication_target(path)
        try:
            try:
                fcntl.flock(target.parent.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise RuntimeError(
                        f"result checkpoint directory is already leased: {target.display.parent}"
                    ) from exc
                raise RuntimeError(
                    f"cannot lock result checkpoint directory: {exc}"
                ) from exc
            target.revalidate_parent()
            return cls(target, library)
        except Exception:
            target.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._current_fd is not None:
                os.close(self._current_fd)
                self._current_fd = None
            if fcntl is not None:
                fcntl.flock(self._target.parent.fd, fcntl.LOCK_UN)
        finally:
            self._target.close()

    def __enter__(self) -> ResultCheckpoint:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _assert_live_lease(self) -> None:
        if self._closed:
            raise RuntimeError("result checkpoint lease is closed")
        if self._poisoned:
            raise RuntimeError("result checkpoint is closed after uncertain publication")
        self._target.revalidate_parent()

    def _checkpoint_revalidate(self) -> None:
        self._assert_live_lease()

    def _publish_new(self, payload: bytes) -> None:
        descriptor, snapshot = _create_anonymous_payload(self._target, payload, "result checkpoint")
        transferred = False
        try:
            self._assert_live_lease()
            snapshot = _publish_new_payload(
                self._target,
                self._library,
                descriptor,
                snapshot,
                payload,
                no_clobber_error=f"result output was created concurrently: {self.path}",
                probe_os_link=False,
            )
            self._assert_live_lease()
            self._current_fd = descriptor
            self._current_snapshot = snapshot
            self._identity = snapshot.identity
            transferred = True
        finally:
            if not transferred:
                os.close(descriptor)

    def _publish_exchange(
        self,
        payload: bytes,
        old_fd: int,
        old_snapshot: _FileSnapshot,
    ) -> None:
        descriptor, snapshot = _create_anonymous_payload(self._target, payload, "result checkpoint")
        transferred = False
        try:
            snapshot = _replace_payload(
                self._target,
                self._library,
                descriptor,
                snapshot,
                payload,
                old_fd,
                old_snapshot,
                recovery_kind="checkpoint-recovery",
                label="result checkpoint",
                revalidate=self._checkpoint_revalidate,
            )
            self._assert_live_lease()
            self._current_fd = descriptor
            self._current_snapshot = snapshot
            self._identity = snapshot.identity
            transferred = True
        except Exception:
            self._poisoned = True
            raise
        finally:
            if not transferred:
                os.close(descriptor)

    def publish_initial(self, value: Any, *, force: bool) -> None:
        self._publish(_json_checkpoint_payload(value), force_initial=force)

    def write_json(self, value: Any) -> None:
        self._publish(_json_checkpoint_payload(value), force_initial=False)

    def _publish(self, payload: bytes, *, force_initial: bool) -> None:
        self._assert_live_lease()
        if self._current_fd is not None and self._current_snapshot is not None:
            old_fd = self._current_fd
            old_snapshot = self._current_snapshot
            _verify_pinned_named_regular(
                self._target,
                self._target.leaf,
                old_fd,
                old_snapshot,
                "current result checkpoint",
            )
            self._publish_exchange(payload, old_fd, old_snapshot)
            # _publish_exchange replaced ``_current_fd`` only after the old private
            # recovery entry was safely removed, so it is now safe to close the old
            # descriptor.  The ordering also leaves no closed pathname stage.
            if old_fd != self._current_fd:
                os.close(old_fd)
            return

        existing = _output_lstat(self._target)
        _assert_regular_output(self._target, existing)
        if existing is None:
            self._publish_new(payload)
            return
        if not force_initial:
            raise RuntimeError(
                f"refusing to overwrite result: {self.path}; pass --force to replace it"
            )
        old_fd, old_snapshot = _open_named_regular(
            self._target,
            self._target.leaf,
            "existing result checkpoint",
        )
        try:
            self._publish_exchange(payload, old_fd, old_snapshot)
        finally:
            os.close(old_fd)


def open_result_checkpoint(
    path: Path,
    initial_document: Any,
    *,
    force: bool,
) -> ResultCheckpoint:
    """Open a result stream only after a complete initial document is durable."""

    # Serializing before acquisition/publication is deliberate: an invalid new
    # document must leave a forced target intact.
    payload = _json_checkpoint_payload(initial_document)
    checkpoint = ResultCheckpoint.acquire(path)
    try:
        checkpoint._publish(payload, force_initial=force)
        return checkpoint
    except Exception:
        checkpoint.close()
        raise


def discover_cases(
    selected: Iterable[str] | None = None,
    cases_dir: Path | None = None,
) -> list[BenchmarkCase]:
    cases_dir = (cases_dir or CASES_DIR).resolve()
    if not cases_dir.is_dir():
        raise ValueError(f"benchmark cases directory does not exist: {cases_dir}")
    manifest_paths = sorted(cases_dir.glob("*/case.json"))
    if not manifest_paths:
        raise ValueError(f"benchmark cases directory is empty: {cases_dir}")
    wanted = set(selected or [])
    cases: list[BenchmarkCase] = []
    for manifest_path in manifest_paths:
        if manifest_path.is_symlink() or manifest_path.parent.is_symlink():
            raise ValueError(f"benchmark case path cannot be a symlink: {manifest_path}")
        try:
            manifest_path.resolve(strict=True).relative_to(cases_dir)
        except ValueError as exc:
            raise ValueError(
                f"benchmark case path escapes cases directory: {manifest_path}"
            ) from exc
        manifest = read_json(manifest_path)
        case_id = manifest.get("id") if isinstance(manifest, dict) else None
        if not isinstance(case_id, str) or CASE_ID.fullmatch(case_id) is None:
            raise ValueError(
                f"invalid benchmark case id in {manifest_path}: {case_id!r}"
            )
        if manifest_path.parent.name != case_id:
            raise ValueError(
                f"benchmark case directory {manifest_path.parent.name!r} "
                f"does not match id {case_id!r}"
            )
        case = BenchmarkCase(manifest_path.parent, manifest)
        if not wanted or case.id in wanted:
            cases.append(case)
    missing = wanted - {case.id for case in cases}
    if missing:
        raise ValueError(f"unknown benchmark cases: {', '.join(sorted(missing))}")
    return cases


def resolve_relative_file(root: Path, value: str, label: str) -> Path:
    root = root.resolve()
    candidate = Path(value)
    if not value or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"{label} must be a repository-relative file: {value}")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes its workspace: {value}") from exc
    return resolved


def display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def verification_commands(text: str) -> list[str]:
    commands: list[str] = []
    in_verify = False
    for line in text.splitlines():
        if line and not line[0].isspace():
            in_verify = line.strip() == "verify:"
            continue
        if in_verify:
            match = VERIFY_LINE.fullmatch(line)
            if match:
                commands.append(match.group(1))
    return commands


def text_metrics(text: str) -> dict[str, int]:
    return {
        "bytes": len(text.encode("utf-8")),
        "characters": len(text),
        "words": len(text.split()),
        "lines": len(text.splitlines()),
    }


def document_metrics(path: Path) -> dict[str, int]:
    return text_metrics(path.read_text(encoding="utf-8"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def tree_sha256(path: Path) -> str:
    assert_no_symlinks(path, "hashed tree")
    digest = hashlib.sha256()
    files = (
        candidate
        for candidate in path.rglob("*")
        if candidate.is_file()
        and "__pycache__" not in candidate.parts
        and candidate.suffix != ".pyc"
    )
    for item in sorted(files):
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class FixtureFileSnapshot:
    relative_path: str
    data: bytes
    mode: int


@dataclass(frozen=True)
class FixtureTreeSnapshot:
    """An immutable, descriptor-captured fixture tree used for every arm."""

    directories: tuple[str, ...]
    files: tuple[FixtureFileSnapshot, ...]
    sha256: str


@dataclass(frozen=True)
class GradingSnapshot:
    """Private grader inputs derived from the run's captured fixture tree."""

    case_id: str
    fixture: FixtureTreeSnapshot
    case_sha256: str
    tests_sha256: str
    test_total: int
    acceptance_total: int


def _fixture_records_sha256(files: Iterable[FixtureFileSnapshot]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda record: record.relative_path):
        digest.update(item.relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.data)
        digest.update(b"\0")
    return digest.hexdigest()


def fixture_snapshot_file(
    snapshot: FixtureTreeSnapshot,
    relative_path: str,
) -> FixtureFileSnapshot:
    """Return one exact regular file from a captured fixture tree."""

    candidate = PurePosixPath(relative_path)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ValueError(f"invalid fixture snapshot path: {relative_path!r}")
    normalized = candidate.as_posix()
    matches = [item for item in snapshot.files if item.relative_path == normalized]
    if len(matches) != 1:
        raise ValueError(f"fixture snapshot lacks regular file: {normalized}")
    return matches[0]


def grading_snapshot_from_fixture(
    case: BenchmarkCase,
    fixture_snapshot: FixtureTreeSnapshot,
) -> GradingSnapshot:
    """Derive exact private grading bytes without consulting live case paths."""

    case_file = fixture_snapshot_file(fixture_snapshot, "case.json")
    tests_file = fixture_snapshot_file(fixture_snapshot, "tests.json")
    try:
        manifest = json.loads(case_file.data.decode("utf-8"))
        suite = json.loads(tests_file.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{case.id}: captured grading inputs are invalid JSON") from exc
    if manifest != case.manifest:
        raise RuntimeError(f"{case.id}: case manifest changed before fixture capture")
    tests = suite.get("tests") if isinstance(suite, dict) else None
    if not isinstance(tests, list) or any(not isinstance(test, dict) for test in tests):
        raise ValueError(f"{case.id}: captured tests.json lacks a tests list")
    acceptances = [test.get("acceptance") for test in tests]
    if any(not isinstance(value, str) or not value for value in acceptances):
        raise ValueError(f"{case.id}: captured tests lack acceptance ids")
    private_files = tuple(sorted(
        (case_file, tests_file),
        key=lambda item: item.relative_path,
    ))
    private_fixture = FixtureTreeSnapshot(
        (),
        private_files,
        _fixture_records_sha256(private_files),
    )
    return GradingSnapshot(
        case.id,
        private_fixture,
        sha256_bytes(case_file.data),
        sha256_bytes(tests_file.data),
        len(tests),
        len(set(acceptances)),
    )


def grading_snapshot_metadata(snapshot: GradingSnapshot) -> dict[str, Any]:
    """Return the public hashes/counts that bind a private grading snapshot."""

    return {
        "fixture_sha256": snapshot.fixture.sha256,
        "case_sha256": snapshot.case_sha256,
        "tests_sha256": snapshot.tests_sha256,
        "test_total": snapshot.test_total,
        "acceptance_total": snapshot.acceptance_total,
    }


def grade_matches_grading_snapshot(
    grade: Any,
    snapshot: GradingSnapshot | dict[str, Any],
) -> bool:
    """Bind aggregate grade counts to the exact captured hidden-test suite."""

    metadata = (
        grading_snapshot_metadata(snapshot)
        if isinstance(snapshot, GradingSnapshot)
        else snapshot
    )
    if not isinstance(grade, dict) or not isinstance(metadata, dict):
        return False
    test_total = metadata.get("test_total")
    acceptance_total = metadata.get("acceptance_total")
    passed = grade.get("passed")
    acceptance_passed = grade.get("acceptance_passed")
    return bool(
        isinstance(test_total, int)
        and not isinstance(test_total, bool)
        and test_total >= 0
        and isinstance(acceptance_total, int)
        and not isinstance(acceptance_total, bool)
        and acceptance_total >= 0
        and isinstance(passed, int)
        and not isinstance(passed, bool)
        and 0 <= passed <= test_total
        and isinstance(acceptance_passed, int)
        and not isinstance(acceptance_passed, bool)
        and 0 <= acceptance_passed <= acceptance_total
        and grade.get("total") == test_total
        and grade.get("acceptance_total") == acceptance_total
    )


def fixture_subtree_snapshot(
    snapshot: FixtureTreeSnapshot,
    relative_path: str,
) -> FixtureTreeSnapshot:
    """Derive an immutable subtree without consulting a live pathname."""

    prefix_path = PurePosixPath(relative_path)
    if (
        prefix_path.is_absolute()
        or not prefix_path.parts
        or any(part in {"", ".", ".."} for part in prefix_path.parts)
    ):
        raise ValueError(f"invalid fixture subtree path: {relative_path!r}")
    prefix = prefix_path.as_posix()
    marker = prefix + "/"
    if prefix not in snapshot.directories:
        raise ValueError(f"fixture snapshot lacks directory: {prefix}")
    directories = tuple(
        directory[len(marker) :]
        for directory in snapshot.directories
        if directory.startswith(marker)
    )
    files = tuple(
        FixtureFileSnapshot(
            item.relative_path[len(marker) :],
            item.data,
            item.mode,
        )
        for item in snapshot.files
        if item.relative_path.startswith(marker)
    )
    return FixtureTreeSnapshot(
        tuple(sorted(directories)),
        tuple(sorted(files, key=lambda item: item.relative_path)),
        _fixture_records_sha256(files),
    )


def verification_fixture_sha256_from_snapshot(
    case: BenchmarkCase,
    starter_snapshot: FixtureTreeSnapshot,
) -> str:
    """Hash declared verification fixtures from captured starter bytes."""

    values = case.manifest.get("verification_files", [])
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError(f"{case.id}: verification_files must be a list of paths")
    if len(values) != len(set(values)):
        raise ValueError(f"{case.id}: verification_files contains duplicates")
    digest = hashlib.sha256()
    for relative_path in sorted(values):
        item = fixture_snapshot_file(starter_snapshot, relative_path)
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.data)
        digest.update(b"\0")
    return digest.hexdigest()


def snapshot_fixture_tree(
    source: Path,
    *,
    expected_sha256: str | None = None,
) -> FixtureTreeSnapshot:
    """Capture a symlink-free fixture through pinned directory descriptors."""

    root = _PinnedDirectory.open(source, "fixture snapshot", create=False)
    directories: list[str] = []
    files: list[FixtureFileSnapshot] = []

    def unchanged(before: os.stat_result, after: os.stat_result) -> bool:
        return (
            stat.S_ISDIR(after.st_mode)
            and _inode_identity(before) == _inode_identity(after)
            and before.st_mode == after.st_mode
            and before.st_size == after.st_size
            and before.st_mtime_ns == after.st_mtime_ns
            and before.st_ctime_ns == after.st_ctime_ns
        )

    def visit(directory_fd: int, prefix: PurePosixPath) -> None:
        before = os.fstat(directory_fd)
        if not stat.S_ISDIR(before.st_mode):
            raise RuntimeError("fixture snapshot contains a non-directory")
        with os.scandir(directory_fd) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
        for entry in entries:
            name = entry.name
            relative = prefix / name
            relative_text = relative.as_posix()
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"fixture tree cannot contain symlinks: {relative_text}")
            if stat.S_ISDIR(metadata.st_mode):
                if name == "__pycache__":
                    continue
                flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
                child_fd = os.open(name, flags, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if _inode_identity(opened) != _inode_identity(metadata):
                        raise RuntimeError("fixture directory changed while being pinned")
                    directories.append(relative_text)
                    visit(child_fd, relative)
                finally:
                    os.close(child_fd)
                continue
            if stat.S_ISREG(metadata.st_mode):
                if relative.suffix == ".pyc":
                    continue
                descriptor = os.open(name, _regular_flags(), dir_fd=directory_fd)
                try:
                    captured = _snapshot_regular_fd(descriptor, "fixture file")
                    named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if not _snapshot_matches(named, captured):
                        raise RuntimeError("fixture file changed while being pinned")
                    files.append(
                        FixtureFileSnapshot(
                            relative_text,
                            captured.data,
                            stat.S_IMODE(captured.mode),
                        )
                    )
                finally:
                    os.close(descriptor)
                continue
            raise ValueError(
                f"fixture tree contains a non-regular entry: {relative_text}"
            )
        after = os.fstat(directory_fd)
        if not unchanged(before, after):
            raise RuntimeError("fixture directory changed while being captured")

    try:
        visit(root.fd, PurePosixPath())
        root.revalidate("fixture snapshot")
    finally:
        root.close()
    digest = _fixture_records_sha256(files)
    if expected_sha256 is not None and digest != expected_sha256:
        raise RuntimeError("fixture snapshot does not match its declared SHA-256")
    return FixtureTreeSnapshot(
        tuple(sorted(directories)),
        tuple(sorted(files, key=lambda item: item.relative_path)),
        digest,
    )


def materialize_fixture_tree(
    snapshot: FixtureTreeSnapshot,
    destination: Path,
) -> None:
    """Create one private workspace copy from immutable captured bytes."""

    if destination.exists() or destination.is_symlink():
        raise RuntimeError(f"refusing to overwrite workspace: {destination}")
    destination.mkdir(parents=True)
    for relative in snapshot.directories:
        (destination / relative).mkdir(parents=True, exist_ok=True)
    for item in snapshot.files:
        target = destination / item.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags, item.mode & 0o777)
        try:
            _write_all(descriptor, item.data)
            os.fchmod(descriptor, item.mode & 0o777)
        finally:
            os.close(descriptor)
    if tree_sha256(destination) != snapshot.sha256:
        raise RuntimeError("materialized fixture does not match its immutable snapshot")


def symlink_paths(root: Path) -> list[Path]:
    if root.is_symlink():
        return [root]
    if not root.exists():
        return []
    links: list[Path] = []
    for directory, names, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in (*names, *files):
            candidate = parent / name
            if candidate.is_symlink():
                links.append(candidate)
    return sorted(links)


def assert_no_symlinks(root: Path, label: str) -> None:
    links = symlink_paths(root)
    if links:
        rendered = ", ".join(str(path) for path in links[:5])
        raise ValueError(f"{label} cannot contain symlinks: {rendered}")


def copy_fixture_tree(source: Path, destination: Path, **kwargs: Any) -> None:
    assert_no_symlinks(source, "fixture tree")
    shutil.copytree(source, destination, symlinks=True, **kwargs)
    assert_no_symlinks(destination, "copied fixture tree")


def compression_percent(baseline: int, semantic: int) -> float:
    return round((baseline - semantic) / baseline * 100, 2) if baseline else 0.0


def load_token_encoder(name: str | None) -> Any | None:
    if not name:
        return None
    try:
        import tiktoken

        return tiktoken.get_encoding(name)
    except (ImportError, OSError, ValueError) as exc:
        raise RuntimeError(f"cannot load tokenizer {name}: {exc}") from exc


def static_rows(
    cases: list[BenchmarkCase],
    semantic_specs: dict[str, str] | None = None,
    token_encoder: Any | None = None,
) -> list[dict[str, Any]]:
    rows = []
    for case in cases:
        baseline = document_metrics(case.spec_path("baseline"))
        semantic = (
            text_metrics(semantic_specs[case.id])
            if semantic_specs is not None
            else document_metrics(case.spec_path("semantic"))
        )
        if token_encoder is not None:
            baseline_text = case.spec_path("baseline").read_text(encoding="utf-8")
            semantic_text = (
                semantic_specs[case.id]
                if semantic_specs is not None
                else case.spec_path("semantic").read_text(encoding="utf-8")
            )
            baseline["tokens"] = len(token_encoder.encode(baseline_text))
            semantic["tokens"] = len(token_encoder.encode(semantic_text))
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
            "token_reduction_percent": (
                compression_percent(baseline["tokens"], semantic["tokens"])
                if token_encoder is not None
                else None
            ),
        })
    return rows


def render_static_markdown(rows: list[dict[str, Any]]) -> str:
    include_tokens = bool(rows and rows[0].get("token_reduction_percent") is not None)
    if include_tokens:
        lines = [
            "| Case | Baseline tokens | Semantic tokens | Token reduction | Byte reduction | Word reduction |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for row in rows:
            lines.append(
                f"| `{row['case']}` | {row['baseline']['tokens']} | "
                f"{row['semantic']['tokens']} | {row['token_reduction_percent']:.2f}% | "
                f"{row['byte_reduction_percent']:.2f}% | {row['word_reduction_percent']:.2f}% |"
            )
        lines.append(
            f"| **Median** |  |  | "
            f"**{statistics.median(r['token_reduction_percent'] for r in rows):.2f}%** | "
            f"**{statistics.median(r['byte_reduction_percent'] for r in rows):.2f}%** | "
            f"**{statistics.median(r['word_reduction_percent'] for r in rows):.2f}%** |"
        )
        return "\n".join(lines) + "\n"

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


def run_process_capture(
    command: list[str],
    cwd: Path,
    timeout: int,
    environment: dict[str, str],
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input_text, timeout=timeout)
    except BaseException:
        stop_process_group(process)
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def run_restricted_sandbox(
    command: list[str],
    cwd: Path,
    readable_roots: Iterable[Path],
    writable_roots: Iterable[Path] = (),
    timeout: int = 30,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    cwd = cwd.resolve()
    unshare = shutil.which("unshare")
    if unshare is None:
        raise RuntimeError("restricted benchmark execution requires unshare")
    access = {
        path.resolve(): "read"
        for path in (cwd, *readable_roots)
    }
    access.update({path.resolve(): "write" for path in writable_roots})
    with tempfile.TemporaryDirectory(prefix="semantic-spec-sandbox-") as directory:
        codex_home = Path(directory)
        filesystem = [
            '":minimal" = "read"',
            '"/proc" = "none"',
            '"/sys" = "none"',
            *(
                f"{json.dumps(str(path))} = {json.dumps(mode)}"
                for path, mode in sorted(access.items())
            ),
        ]
        (codex_home / "config.toml").write_text(
            "default_permissions = \"benchmark-grader\"\n\n"
            "[permissions.benchmark-grader.filesystem]\n"
            + "\n".join(filesystem)
            + "\n\n[permissions.benchmark-grader.network]\n"
            "enabled = false\n",
            encoding="utf-8",
        )
        environment = safe_environment()
        environment.update({
            "CODEX_HOME": str(codex_home),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        return run_process_capture(
            [
                unshare,
                "--mount",
                "--pid",
                "--net",
                "--fork",
                "--kill-child",
                "--mount-proc",
                "--",
                "codex",
                "sandbox",
                "--permission-profile",
                "benchmark-grader",
                "--cd",
                str(cwd),
                "--",
                *command,
            ],
            cwd,
            timeout,
            environment,
            input_text,
        )


def run_grader(
    case: BenchmarkCase,
    workspace: Path,
    *,
    trusted: bool = False,
    grading_snapshot: GradingSnapshot | None = None,
) -> dict[str, Any]:
    """Grade against a private materialization of descriptor-captured bytes."""

    workspace = workspace.resolve()
    if grading_snapshot is None:
        grading_snapshot = grading_snapshot_from_fixture(
            case,
            snapshot_fixture_tree(case.path),
        )
    if grading_snapshot.case_id != case.id:
        raise ValueError(f"{case.id}: grading snapshot belongs to another case")
    with tempfile.TemporaryDirectory(prefix="semantic-spec-grader-fixture-") as directory:
        private_case = Path(directory) / case.id
        materialize_fixture_tree(grading_snapshot.fixture, private_case)
        command = [
            sys.executable,
            str(BENCHMARKS / "grader.py"),
            str(private_case),
            str(workspace),
            "--case-sha256",
            grading_snapshot.case_sha256,
            "--tests-sha256",
            grading_snapshot.tests_sha256,
        ]
        if not trusted:
            command.append("--untrusted")
        environment = safe_environment()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = run_process_capture(command, workspace, 120, environment)
    if completed.returncode != 0:
        raise RuntimeError(
            f"grader failed for {case.id}: {completed.stdout.strip()} "
            f"{completed.stderr.strip()}"
        )
    return json.loads(completed.stdout)


def run_verification(
    case: BenchmarkCase,
    workspace: Path,
    *,
    trusted: bool = False,
    retain_sensitive_text: bool = False,
) -> dict[str, Any] | None:
    declared = case.manifest.get("verification_command")
    if not declared:
        return None
    command = shlex.split(str(declared))
    if not command:
        raise ValueError(f"{case.id}: verification command is empty")
    fixtures = verification_fixtures(case)
    with tempfile.TemporaryDirectory(prefix="semantic-spec-verify-") as directory:
        verification_workspace = Path(directory) / "workspace"
        copy_fixture_tree(
            workspace.resolve(),
            verification_workspace,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        for relative_path, source in fixtures:
            destination = resolve_relative_file(
                verification_workspace,
                relative_path,
                f"{case.id} verification fixture",
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        environment = safe_environment()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = (
            run_process_capture(command, verification_workspace, 30, environment)
            if trusted
            else run_restricted_sandbox(
                command,
                verification_workspace,
                [verification_workspace],
                [verification_workspace],
            )
        )
    result = {
        "command_metadata": text_metadata(str(declared)),
        "fixture_sha256": verification_fixture_sha256(fixtures),
        "return_code": completed.returncode,
        "stdout_metadata": text_metadata(completed.stdout),
        "stderr_metadata": text_metadata(completed.stderr),
    }
    if retain_sensitive_text:
        result.update({
            "_stdout_tail": completed.stdout[-2000:],
            "_stderr_tail": completed.stderr[-2000:],
        })
    return result


def verification_fixtures(case: BenchmarkCase) -> list[tuple[str, Path]]:
    values = case.manifest.get("verification_files", [])
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError(f"{case.id}: verification_files must be a list of paths")
    if len(values) != len(set(values)):
        raise ValueError(f"{case.id}: verification_files contains duplicates")
    assert_no_symlinks(case.path / "starter", f"{case.id} starter fixture")
    fixtures: list[tuple[str, Path]] = []
    for value in values:
        source = resolve_relative_file(
            case.path / "starter",
            value,
            f"{case.id} verification fixture",
        )
        if source.is_symlink() or not source.is_file():
            raise ValueError(
                f"{case.id}: verification fixture must be a regular file: {value}"
            )
        fixtures.append((value, source))
    return fixtures


def verification_fixture_sha256(fixtures: list[tuple[str, Path]]) -> str:
    digest = hashlib.sha256()
    for relative_path, source in sorted(fixtures):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def empty_grade(
    case: BenchmarkCase,
    grading_snapshot: GradingSnapshot | None = None,
) -> dict[str, Any]:
    if grading_snapshot is None:
        grading_snapshot = grading_snapshot_from_fixture(
            case,
            snapshot_fixture_tree(case.path),
        )
    return {
        "passed": 0,
        "total": grading_snapshot.test_total,
        "pass_rate": 0.0,
        "acceptance_passed": 0,
        "acceptance_total": grading_snapshot.acceptance_total,
        "acceptance_pass_rate": 0.0,
        "task_success": False,
        "failures": [],
    }


def block_ids(text: str, block: str, prefix: str) -> list[str]:
    ids: list[str] = []
    inside = False
    pattern = re.compile(rf"^\s+({re.escape(prefix)}\d+):")
    for line in text.splitlines():
        if line and not line[0].isspace():
            inside = line.strip() == f"{block}:"
            continue
        if inside:
            match = pattern.match(line)
            if match:
                ids.append(match.group(1))
    return ids


def validate_semantic_text(case: BenchmarkCase, semantic: str) -> list[str]:
    errors: list[str] = []
    if not semantic.startswith("spec\n"):
        errors.append(f"{case.id}: semantic spec must start with 'spec'")
    entrypoint = str(case.manifest["entrypoint"])
    if entrypoint not in semantic:
        errors.append(f"{case.id}: semantic spec lacks exact entrypoint {entrypoint}")
    for pattern in case.manifest.get("semantic_required_patterns", []):
        if re.search(pattern, semantic, re.MULTILINE | re.IGNORECASE) is None:
            errors.append(
                f"{case.id}: semantic spec lacks required pattern {pattern}"
            )

    suite = read_json(case.path / "tests.json")
    if not suite.get("tests"):
        errors.append(f"{case.id}: test suite is empty")
        return errors
    acceptance_ids = {test.get("acceptance") for test in suite["tests"]}
    if None in acceptance_ids:
        errors.append(f"{case.id}: every test must map to an acceptance id")
    defined_acceptance_list = block_ids(semantic, "acceptance", "A")
    defined_acceptance_ids = set(defined_acceptance_list)
    duplicates = sorted({
        item
        for item in defined_acceptance_list
        if defined_acceptance_list.count(item) > 1
    })
    if duplicates:
        errors.append(f"{case.id}: duplicate acceptance ids: {', '.join(duplicates)}")
    unmapped = defined_acceptance_ids - acceptance_ids
    if unmapped:
        errors.append(
            f"{case.id}: acceptance ids without tests: {', '.join(sorted(unmapped))}"
        )
    missing_acceptance = {
        item for item in acceptance_ids if item
    } - defined_acceptance_ids
    if missing_acceptance:
        errors.append(
            f"{case.id}: acceptance block lacks "
            f"{', '.join(sorted(missing_acceptance))}"
        )
    return errors


def validate_execution_packet_artifact(
    case: BenchmarkCase,
    packet_path: Path,
    *,
    starter_path: Path | None = None,
) -> list[str]:
    errors: list[str] = []
    text = packet_path.read_text(encoding="utf-8")
    verification_command = case.manifest.get("verification_command")
    if not isinstance(verification_command, str) or not verification_command.strip():
        errors.append(f"{case.id}: execution packet requires verification_command")
    elif verification_command not in verification_commands(text):
        errors.append(f"{case.id}: packet lacks exact verification command")
    for pattern in case.manifest.get("packet_required_patterns", []):
        if re.search(pattern, text, re.MULTILINE | re.IGNORECASE) is None:
            errors.append(f"{case.id}: packet lacks required pattern {pattern}")

    checked = subprocess.run(
        [
            sys.executable,
            "-I",
            str(EXECUTION_PACKET_CHECK),
            str(starter_path or case.path / "starter"),
            str(packet_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if checked.returncode != 0:
        detail = checked.stdout.strip() or checked.stderr.strip()
        try:
            payload = json.loads(checked.stdout)
            detail = "; ".join(str(item) for item in payload.get("errors", [])) or detail
        except json.JSONDecodeError:
            pass
        errors.append(f"{case.id}: execution packet check failed: {detail}")
    return errors


def validate_case(case: BenchmarkCase) -> list[str]:
    errors: list[str] = []
    if CASE_ID.fullmatch(case.id) is None:
        return [f"invalid benchmark case id: {case.id!r}"]
    if case.path.name != case.id:
        return [
            f"benchmark case directory {case.path.name!r} does not match id {case.id!r}"
        ]
    try:
        assert_no_symlinks(case.path, f"{case.id} fixture")
    except ValueError as exc:
        return [str(exc)]
    try:
        verification_fixtures(case)
    except ValueError as exc:
        return [str(exc)]
    required = [
        case.path / "case.json",
        case.path / "baseline.md",
        case.path / "semantic.spec.ctx",
        case.path / "tests.json",
    ]
    for path in required:
        if not path.is_file():
            errors.append(f"{case.id}: missing {display_path(path)}")
    for directory in (case.path / "starter", case.path / "reference"):
        if not directory.is_dir():
            errors.append(f"{case.id}: missing {display_path(directory)}")
    if errors:
        return errors

    manifest_entrypoint = str(case.manifest.get("entrypoint", ""))
    try:
        starter_entrypoint = resolve_relative_file(
            case.path / "starter", manifest_entrypoint, f"{case.id} entrypoint"
        )
        reference_entrypoint = resolve_relative_file(
            case.path / "reference", manifest_entrypoint, f"{case.id} entrypoint"
        )
    except ValueError as exc:
        return [str(exc)]
    for path in (starter_entrypoint, reference_entrypoint):
        if not path.is_file():
            errors.append(f"{case.id}: missing {display_path(path)}")
    if errors:
        return errors

    semantic = case.spec_path("semantic").read_text(encoding="utf-8")
    errors.extend(validate_semantic_text(case, semantic))

    suite = read_json(case.path / "tests.json")
    tested_entrypoints = {
        str(test.get("entrypoint", case.manifest["entrypoint"]))
        for test in suite["tests"]
    }
    for entrypoint in sorted(tested_entrypoints):
        for variant in ("starter", "reference"):
            try:
                path = resolve_relative_file(
                    case.path / variant,
                    entrypoint,
                    f"{case.id} tested entrypoint",
                )
            except ValueError as exc:
                errors.append(str(exc))
                continue
            if not path.is_file():
                errors.append(
                    f"{case.id}: missing tested {variant} entrypoint "
                    f"{display_path(path)}"
                )
    if errors:
        return errors
    acceptance_ids = {test.get("acceptance") for test in suite["tests"]}
    baseline = case.spec_path("baseline").read_text(encoding="utf-8")
    for acceptance_id in sorted(item for item in acceptance_ids if item):
        if acceptance_id not in baseline:
            errors.append(f"{case.id}: baseline lacks {acceptance_id}")

    with tempfile.TemporaryDirectory(prefix=f"semantic-spec-reference-{case.id}-") as directory:
        reference_workspace = Path(directory) / "workspace"
        copy_fixture_tree(case.path / "starter", reference_workspace)
        copy_fixture_tree(
            case.path / "reference",
            reference_workspace,
            dirs_exist_ok=True,
        )
        reference_grade = run_grader(case, reference_workspace, trusted=True)
        verified = run_verification(
            case,
            reference_workspace,
            trusted=True,
            retain_sensitive_text=True,
        )
        if verified and verified["return_code"] != 0:
            errors.append(
                f"{case.id}: reference verification failed: "
                f"{verified['_stdout_tail'].strip()} "
                f"{verified['_stderr_tail'].strip()}"
            )
    if reference_grade["passed"] != reference_grade["total"]:
        errors.append(f"{case.id}: reference solution does not pass all tests")
    starter_grade = run_grader(case, case.path / "starter", trusted=True)
    if starter_grade["passed"] == starter_grade["total"]:
        errors.append(f"{case.id}: starter already passes all tests")
    verification_command = case.manifest.get("verification_command")
    if verification_command:
        semantic = case.spec_path("semantic").read_text(encoding="utf-8")
        if str(verification_command) not in verification_commands(semantic):
            errors.append(f"{case.id}: semantic spec lacks verification command")
        starter_verification = run_verification(
            case, case.path / "starter", trusted=True
        )
        if starter_verification and starter_verification["return_code"] == 0:
            errors.append(f"{case.id}: starter already passes visible verification")
    return errors


def validate(cases: list[BenchmarkCase]) -> list[str]:
    errors: list[str] = []
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        errors.append("duplicate case ids")
    for case in cases:
        errors.extend(validate_case(case))
    return errors


def load_semantic_specs(
    cases: list[BenchmarkCase], directory: Path | None
) -> dict[str, str]:
    if directory is None:
        return {
            case.id: case.spec_path("semantic").read_text(encoding="utf-8")
            for case in cases
        }
    directory = directory.resolve()
    if not directory.is_dir():
        raise ValueError(f"semantic spec directory does not exist: {directory}")
    specs: dict[str, str] = {}
    errors: list[str] = []
    for case in cases:
        path = directory / f"{case.id}.spec.ctx"
        if not path.is_file():
            errors.append(f"{case.id}: missing generated spec {path}")
            continue
        text = path.read_text(encoding="utf-8")
        specs[case.id] = text
        errors.extend(validate_semantic_text(case, text))
        if case.manifest.get("execution_packet"):
            errors.extend(validate_execution_packet_artifact(case, path))
    if errors:
        raise ValueError("generated semantic validation failed:\n" + "\n".join(errors))
    return specs


def safe_workspace(
    case: BenchmarkCase,
    root: Path,
    *,
    starter_snapshot: FixtureTreeSnapshot | None = None,
) -> Path:
    if CASE_ID.fullmatch(case.id) is None:
        raise ValueError(f"invalid benchmark case id: {case.id!r}")
    root = root.resolve()
    workspace = (root / case.id).resolve()
    try:
        workspace.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"benchmark workspace escapes root: {case.id!r}") from exc
    if workspace.exists():
        raise RuntimeError(f"refusing to overwrite workspace: {workspace}")
    if starter_snapshot is None:
        starter_snapshot = snapshot_fixture_tree(case.path / "starter")
    materialize_fixture_tree(starter_snapshot, workspace)
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "add", "--all"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    identity: dict[str, str] = {}
    for key, field in (("user.name", "name"), ("user.email", "email")):
        configured = subprocess.run(
            ["git", "config", "--get", key],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not configured:
            placeholder = "%an" if field == "name" else "%ae"
            configured = subprocess.run(
                ["git", "show", "-s", f"--format={placeholder}", "HEAD"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        identity[field] = configured
    commit_environment = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    }
    subprocess.run(
        [
            "git",
            "-c",
            f"user.name={identity['name']}",
            "-c",
            f"user.email={identity['email']}",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--quiet",
            "--message",
            "Benchmark starter snapshot",
        ],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
        env=commit_environment,
    )
    return workspace


def benchmark_prompt(
    spec: str,
    *,
    execution_gate: str | None = None,
    require_syntax_check: bool = True,
) -> str:
    normalized_gate = None
    if execution_gate is not None:
        normalized_gate = execution_gate.strip()
        if not normalized_gate:
            raise ValueError("execution gate must not be empty")
    if not require_syntax_check and normalized_gate is None:
        raise ValueError("syntax check can be replaced only by an execution gate")
    gate = (
        f"\nCapsule execution gate:\n{normalized_gate}\n"
        if normalized_gate is not None
        else " "
    )
    finish = (
        "Finish only after checking the implementation for syntax errors.\n\n"
        if require_syntax_check
        else "\n"
    )
    return (
        "Implement the specification below in the current workspace.\n"
        "Keep changes scoped to the requested behavior. Do not access files outside "
        "the workspace. Do not use network access."
        f"{gate}"
        f"{finish}"
        "--- BEGIN SPECIFICATION ---\n"
        f"{spec.rstrip()}\n"
        "--- END SPECIFICATION ---\n"
    )


def benchmark_case_snapshot(
    case: BenchmarkCase,
    semantic_spec: str,
    *,
    fixture_snapshot: FixtureTreeSnapshot | None = None,
    starter_snapshot: FixtureTreeSnapshot | None = None,
    grading_snapshot: GradingSnapshot | None = None,
) -> dict[str, Any]:
    fixture_snapshot = fixture_snapshot or snapshot_fixture_tree(case.path)
    derived_starter = fixture_subtree_snapshot(fixture_snapshot, "starter")
    if starter_snapshot is not None and starter_snapshot != derived_starter:
        raise RuntimeError(f"{case.id}: starter snapshot is not from fixture snapshot")
    starter_snapshot = derived_starter
    derived_grading = grading_snapshot_from_fixture(case, fixture_snapshot)
    if grading_snapshot is not None and grading_snapshot != derived_grading:
        raise RuntimeError(f"{case.id}: grading snapshot is not from fixture snapshot")
    grading_snapshot = derived_grading
    baseline = fixture_snapshot_file(fixture_snapshot, "baseline.md").data.decode(
        "utf-8"
    )
    variants = {"baseline": baseline, "semantic": semantic_spec}
    return {
        "fixture_sha256": fixture_snapshot.sha256,
        "starter_sha256": starter_snapshot.sha256,
        "verification_fixture_sha256": verification_fixture_sha256_from_snapshot(
            case,
            starter_snapshot,
        ),
        "grading": grading_snapshot_metadata(grading_snapshot),
        "specifications": {
            variant: attest_text(text) for variant, text in variants.items()
        },
        "variants": {
            variant: sha256_bytes(text.encode("utf-8"))
            for variant, text in variants.items()
        },
        "prompts": {
            variant: sha256_bytes(benchmark_prompt(text).encode("utf-8"))
            for variant, text in variants.items()
        },
        "metrics": {
            variant: text_metrics(text) for variant, text in variants.items()
        },
    }


def benchmark_snapshot_specifications(snapshot: Any) -> dict[str, str]:
    """Decode the two exact documents that attest one implementation input."""

    if not isinstance(snapshot, dict):
        raise ValueError("implementation fixture snapshot must be an object")
    values = snapshot.get("specifications")
    if not isinstance(values, dict) or set(values) != set(VARIANTS):
        raise ValueError("implementation fixture snapshot lacks exact specifications")
    return {
        variant: attested_text(
            values[variant],
            f"{variant} implementation specification",
        )
        for variant in VARIANTS
    }


def static_rows_from_benchmark_snapshots(
    cases: Iterable[BenchmarkCase],
    snapshots: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Recompute static rows from the exact attested specification bytes."""

    rows: list[dict[str, Any]] = []
    for case in cases:
        specifications = benchmark_snapshot_specifications(snapshots[case.id])
        baseline = text_metrics(specifications["baseline"])
        semantic = text_metrics(specifications["semantic"])
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
            "token_reduction_percent": None,
        })
    return rows


def require_benchmark_case_snapshot(
    case: BenchmarkCase,
    semantic_spec: str,
    expected: dict[str, Any],
) -> None:
    if benchmark_case_snapshot(case, semantic_spec) != expected:
        raise RuntimeError(f"{case.id}: benchmark fixture changed during run")


def _telemetry_relative_path(
    value: Any,
    workspace: Path | None,
) -> str | None:
    """Normalize a reported file-change path only when it stays in scope."""

    if not isinstance(value, str) or not value:
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        if workspace is None:
            return None
        try:
            candidate = candidate.resolve(strict=False).relative_to(
                workspace.resolve()
            )
        except ValueError:
            return None
    if not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    return candidate.as_posix()


def _file_change_paths(
    item: dict[str, Any],
    workspace: Path | None,
) -> tuple[set[str], bool]:
    """Return reported paths and whether a pre-edit change was classifiable.

    Codex currently reports ``changes`` entries, but accepting the documented
    single-path aliases makes the classifier tolerant of older event streams.
    Missing or malformed paths are deliberately not treated as an edit boundary.
    """

    raw_changes: Any
    if "changes" in item:
        raw_changes = item["changes"]
    elif "path" in item:
        raw_changes = [{"path": item["path"]}]
    elif "file_path" in item:
        raw_changes = [{"path": item["file_path"]}]
    else:
        return set(), False
    if not isinstance(raw_changes, list) or not raw_changes:
        return set(), False
    paths: set[str] = set()
    for change in raw_changes:
        if isinstance(change, str):
            value = change
        elif isinstance(change, dict):
            value = change.get("path", change.get("file_path"))
        else:
            return set(), False
        normalized = _telemetry_relative_path(value, workspace)
        if normalized is None:
            return set(), False
        paths.add(normalized)
    return paths, True


def _unwrapped_shell_command(command: str) -> str:
    """Return the command body emitted by Codex's standard shell wrapper."""

    stripped = command.strip()
    try:
        parts = shlex.split(stripped)
    except ValueError:
        return stripped
    if (
        len(parts) == 3
        and Path(parts[0]).name in {"bash", "sh"}
        and parts[1] in {"-c", "-lc"}
    ):
        return parts[2].strip()
    return stripped


def parse_codex_events(
    stdout: str,
    *,
    retain_sensitive_text: bool = False,
    substantive_edit_paths: Iterable[str] | None = None,
    declared_verification_command: str | None = None,
    workspace: Path | None = None,
) -> dict[str, Any]:
    usage: dict[str, int] = {}
    tool_calls: dict[str, int] = {}
    command_log: list[dict[str, Any]] = []
    command_categories = {name: 0 for name in COMMAND_CATEGORY_PATTERNS}
    pre_edit_command_categories = {
        name: 0 for name in COMMAND_CATEGORY_PATTERNS
    }
    pre_edit_command_executions = 0
    declared_verification_executions = 0
    pre_edit_declared_verification_executions = 0
    declared_verification = (
        declared_verification_command.strip()
        if isinstance(declared_verification_command, str)
        and declared_verification_command.strip()
        else None
    )
    target_paths = {
        normalized
        for value in substantive_edit_paths or ()
        if (normalized := _telemetry_relative_path(value, workspace)) is not None
    }
    pre_edit_active = True
    observed_target_paths: set[str] = set()
    file_change_events = 0
    unclassified_file_change_events = 0
    substantive_file_change_events = 0
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
                if item_type == "file_change":
                    file_change_events += 1
                    paths, classifiable = _file_change_paths(item, workspace)
                    if pre_edit_active:
                        if not target_paths or not classifiable:
                            # A generic file-change event cannot prove that the
                            # first substantive edit happened.  Keep charging
                            # subsequent commands to pre-edit and fail claims
                            # closed below rather than resetting on a README edit.
                            unclassified_file_change_events += 1
                        elif paths & target_paths:
                            substantive_file_change_events += 1
                            observed_target_paths.update(paths & target_paths)
                            pre_edit_active = observed_target_paths != target_paths
                elif item_type == "command_execution":
                    command = str(item.get("command", ""))
                    is_declared_verification = bool(
                        declared_verification is not None
                        and _unwrapped_shell_command(command)
                        == declared_verification
                    )
                    categories = {
                        name: bool(pattern.search(command))
                        for name, pattern in COMMAND_CATEGORY_PATTERNS.items()
                    }
                    pre_edit = pre_edit_active
                    command_log.append({
                        "categories": categories,
                        "command_bytes": len(command.encode("utf-8")),
                        "command_sha256": sha256_bytes(command.encode("utf-8")),
                        "exit_code": item.get("exit_code"),
                        "pre_edit": pre_edit,
                        "declared_verification": is_declared_verification,
                    })
                    if pre_edit:
                        pre_edit_command_executions += 1
                    if is_declared_verification:
                        declared_verification_executions += 1
                        if pre_edit:
                            pre_edit_declared_verification_executions += 1
                    for name in COMMAND_CATEGORY_PATTERNS:
                        matched = categories[name]
                        command_categories[name] += matched
                        if pre_edit:
                            pre_edit_command_categories[name] += matched

    if usage:
        usage = {key: int(value) for key, value in usage.items() if isinstance(value, int)}
        usage["uncached_input_tokens"] = max(
            usage.get("input_tokens", 0) - usage.get("cached_input_tokens", 0), 0
        )
    if target_paths and observed_target_paths == target_paths:
        telemetry_status = "routed_edit_observed"
    elif observed_target_paths:
        telemetry_status = "incomplete_routed_edit_observed"
    elif unclassified_file_change_events or not target_paths:
        telemetry_status = "unavailable"
    else:
        telemetry_status = "no_routed_edit_observed"
    result = {
        "usage": usage,
        "tool_calls": tool_calls,
        "tool_call_total": sum(tool_calls.values()),
        "command_log": command_log,
        "command_categories": command_categories,
        "pre_edit_command_categories": pre_edit_command_categories,
        "pre_edit_command_executions": pre_edit_command_executions,
        "declared_verification_executions": declared_verification_executions,
        "pre_edit_declared_verification_executions": (
            pre_edit_declared_verification_executions
        ),
        "pre_edit_telemetry": {
            "schema_version": 3,
            "status": telemetry_status,
            "target_count": len(target_paths),
            "observed_target_count": len(observed_target_paths),
            "file_change_events": file_change_events,
            "unclassified_file_change_events": unclassified_file_change_events,
            "substantive_file_change_events": substantive_file_change_events,
        },
        "thread_id": thread_id,
        "final_message_metadata": text_metadata(final_message),
        "event_errors": event_errors,
    }
    if retain_sensitive_text:
        result["_final_message"] = final_message
    return result


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
    *,
    retain_sensitive_text: bool = False,
    substantive_edit_paths: Iterable[str] | None = None,
    declared_verification_command: str | None = None,
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
    parsed = parse_codex_events(
        stdout,
        retain_sensitive_text=retain_sensitive_text,
        substantive_edit_paths=substantive_edit_paths,
        declared_verification_command=declared_verification_command,
        workspace=workspace,
    )
    parsed.update({
        "return_code": process.returncode,
        "duration_seconds": round(elapsed, 3),
        "stderr_metadata": text_metadata(stderr),
    })
    if retain_sensitive_text:
        parsed["_stderr_tail"] = stderr
    return parsed


def run_mock(case: BenchmarkCase, workspace: Path) -> dict[str, Any]:
    started = time.monotonic()
    shutil.copytree(case.path / "reference", workspace, dirs_exist_ok=True)
    return {
        "return_code": 0,
        "duration_seconds": round(time.monotonic() - started, 6),
        "usage": {},
        "tool_calls": {},
        "tool_call_total": None,
        "thread_id": None,
        "final_message_metadata": text_metadata(
            "mock provider copied the reference solution"
        ),
        "event_errors": [],
        "stderr_metadata": text_metadata(""),
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


def _git_relative_path(value: Path | str) -> str:
    raw = str(value).replace(os.sep, "/")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != raw
        or ":" in raw
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise ValueError(f"invalid repository-relative Git path: {value!r}")
    return path.as_posix()


def _git_environment() -> dict[str, str]:
    """Return a deterministic environment for read-only repository inspection."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["LC_ALL"] = "C"
    return environment


_GIT_READ_ONLY_PREFIX = (
    "git",
    "--no-pager",
    "-c",
    "core.fileMode=true",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
    "-c",
    "status.relativePaths=false",
)


def _git_capture(command: list[str], *, text: bool = False) -> bytes | str:
    try:
        result = subprocess.run(
            [*_GIT_READ_ONLY_PREFIX, *command],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=text,
            stdin=subprocess.DEVNULL,
            env=_git_environment(),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Git inspection failed") from exc
    if result.returncode != 0:
        raise RuntimeError(f"Git command failed: {' '.join(command[:2])}")
    return result.stdout


def _nul_records(output: bytes, label: str) -> list[bytes]:
    """Split a required NUL-terminated Git stream without lossy decoding."""

    if output == b"":
        return []
    if not output.endswith(b"\0"):
        raise RuntimeError(f"Git returned unterminated {label} output")
    records = output.split(b"\0")
    if not records or records[-1] != b"" or any(record == b"" for record in records[:-1]):
        raise RuntimeError(f"Git returned malformed {label} output")
    return records[:-1]


def _decode_git_path(raw: bytes) -> str:
    try:
        decoded = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise RuntimeError("Git path is not canonical UTF-8") from exc
    try:
        return _git_relative_path(decoded)
    except ValueError as exc:
        raise RuntimeError("Git returned an unsafe repository path") from exc


@dataclass(frozen=True)
class GitRawChange:
    """One NUL-delimited raw diff record, retaining both rename/copy paths."""

    old_mode: str
    new_mode: str
    status: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class GitWorktreeChange:
    """One porcelain-v2 worktree/index record."""

    kind: str
    xy: str | None
    submodule: str | None
    modes: tuple[str, ...]
    paths: tuple[str, ...]


_RAW_DIFF_HEADER = re.compile(
    rb":([0-7]{6}) ([0-7]{6}) ([0-9a-f]{40,64}) ([0-9a-f]{40,64}) "
    rb"([A-Z])(?:[0-9]{1,3})?\Z"
)
_GIT_MODE = re.compile(r"[0-7]{6}\Z")


def parse_git_raw_diff(output: bytes) -> tuple[GitRawChange, ...]:
    """Parse ``git diff --raw -z`` and fail closed on every ambiguity."""

    records = _nul_records(output, "raw diff")
    parsed: list[GitRawChange] = []
    index = 0
    while index < len(records):
        match = _RAW_DIFF_HEADER.fullmatch(records[index])
        if match is None:
            raise RuntimeError("Git returned a malformed raw diff header")
        index += 1
        status = match.group(5).decode("ascii")
        path_count = 2 if status in {"R", "C"} else 1
        if index + path_count > len(records):
            raise RuntimeError("Git raw diff record has missing paths")
        paths = tuple(
            _decode_git_path(raw) for raw in records[index : index + path_count]
        )
        index += path_count
        parsed.append(
            GitRawChange(
                match.group(1).decode("ascii"),
                match.group(2).decode("ascii"),
                status,
                paths,
            )
        )
    return tuple(parsed)


def _decode_ascii_field(raw: bytes, label: str) -> str:
    try:
        return raw.decode("ascii", "strict")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"Git returned a non-ASCII {label}") from exc


def parse_git_porcelain_v2(output: bytes) -> tuple[GitWorktreeChange, ...]:
    """Parse NUL-delimited porcelain v2, including rename/copy source paths."""

    records = _nul_records(output, "porcelain status")
    parsed: list[GitWorktreeChange] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if record.startswith((b"? ", b"! ")):
            parsed.append(
                GitWorktreeChange(
                    "untracked" if record[:1] == b"?" else "ignored",
                    None,
                    None,
                    (),
                    (_decode_git_path(record[2:]),),
                )
            )
            continue
        if record.startswith(b"1 "):
            fields = record.split(b" ", 8)
            if len(fields) != 9:
                raise RuntimeError("Git returned a malformed ordinary status record")
            xy = _decode_ascii_field(fields[1], "status code")
            submodule = _decode_ascii_field(fields[2], "submodule status")
            modes = tuple(_decode_ascii_field(value, "file mode") for value in fields[3:6])
            if (
                re.fullmatch(r"[.MADRCUT?!]{2}", xy) is None
                or any(_GIT_MODE.fullmatch(mode) is None for mode in modes)
            ):
                raise RuntimeError("Git returned invalid ordinary status metadata")
            parsed.append(
                GitWorktreeChange(
                    "ordinary",
                    xy,
                    submodule,
                    modes,
                    (_decode_git_path(fields[8]),),
                )
            )
            continue
        if record.startswith(b"2 "):
            fields = record.split(b" ", 9)
            if len(fields) != 10 or index >= len(records):
                raise RuntimeError("Git returned a malformed rename/copy status record")
            source = records[index]
            index += 1
            xy = _decode_ascii_field(fields[1], "status code")
            submodule = _decode_ascii_field(fields[2], "submodule status")
            modes = tuple(_decode_ascii_field(value, "file mode") for value in fields[3:6])
            change_kind = _decode_ascii_field(fields[8], "rename/copy score")
            if (
                re.fullmatch(r"[.MADRCUT?!]{2}", xy) is None
                or re.fullmatch(r"[RC][0-9]{1,3}", change_kind) is None
                or any(_GIT_MODE.fullmatch(mode) is None for mode in modes)
            ):
                raise RuntimeError("Git returned invalid rename/copy status metadata")
            parsed.append(
                GitWorktreeChange(
                    "rename_or_copy",
                    xy,
                    submodule,
                    modes,
                    (_decode_git_path(source), _decode_git_path(fields[9])),
                )
            )
            continue
        if record.startswith(b"u "):
            fields = record.split(b" ", 10)
            if len(fields) != 11:
                raise RuntimeError("Git returned a malformed unmerged status record")
            parsed.append(
                GitWorktreeChange(
                    "unmerged",
                    _decode_ascii_field(fields[1], "status code"),
                    _decode_ascii_field(fields[2], "submodule status"),
                    tuple(
                        _decode_ascii_field(value, "file mode")
                        for value in fields[3:7]
                    ),
                    (_decode_git_path(fields[10]),),
                )
            )
            continue
        raise RuntimeError("Git returned an unknown porcelain-v2 record")
    return tuple(parsed)


@dataclass(frozen=True)
class GitTreeEntry:
    """One canonical ``git ls-tree`` entry for a required repository path."""

    mode: str
    object_type: str
    object_id: str
    path: str


def git_tree_entry_at_revision(
    revision: str,
    path: Path | str,
) -> GitTreeEntry:
    """Read an exact Git tree entry, including mode and object type."""

    relative = _git_relative_path(path)
    output = _git_capture(
        ["ls-tree", "-z", "--full-tree", revision, "--", relative],
        text=False,
    )
    if not isinstance(output, bytes):  # pragma: no cover - subprocess contract
        raise RuntimeError("Git returned a non-byte tree entry")
    records = output.split(b"\0")
    if len(records) != 2 or records[1] != b"" or not records[0]:
        raise RuntimeError(f"required Git path has no exact tree entry: {relative}")
    try:
        header, raw_path = records[0].split(b"\t", 1)
        mode, object_type, object_id = header.decode("ascii").split(" ")
        entry_path = raw_path.decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"invalid Git tree entry for required path: {relative}") from exc
    if (
        entry_path != relative
        or re.fullmatch(r"[0-7]{6}", mode) is None
        or re.fullmatch(r"[a-z]+", object_type) is None
        or re.fullmatch(r"[0-9a-f]{40,64}", object_id) is None
    ):
        raise RuntimeError(f"non-canonical Git tree entry for required path: {relative}")
    return GitTreeEntry(mode, object_type, object_id, entry_path)


def git_blob_at_revision(revision: str, path: Path | str) -> bytes:
    """Read one required regular blob from a content-addressed Git revision."""

    relative = _git_relative_path(path)
    entry = git_tree_entry_at_revision(revision, relative)
    if entry.object_type != "blob" or entry.mode not in {"100644", "100755"}:
        raise RuntimeError(f"required Git path is not a blob: {relative}")
    payload = _git_capture(["show", f"{revision}:{relative}"], text=False)
    if not isinstance(payload, bytes):  # pragma: no cover - subprocess contract
        raise RuntimeError("Git returned a non-byte blob")
    return payload


def _git_status_bytes() -> bytes:
    output = _git_capture(
        [
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
            "--ignore-submodules=none",
            "--renames",
        ],
        text=False,
    )
    if not isinstance(output, bytes):  # pragma: no cover - subprocess contract
        raise RuntimeError("Git returned a non-byte status")
    return output


def _git_index_is_fully_observable() -> bool:
    """Reject index flags that can make worktree changes invisible to status."""

    output = _git_capture(["ls-files", "-v", "-z"], text=False)
    if not isinstance(output, bytes):  # pragma: no cover - subprocess contract
        raise RuntimeError("Git returned a non-byte index listing")
    for record in _nul_records(output, "index listing"):
        if len(record) < 3 or record[1:2] != b" " or record[:1] != b"H":
            return False
        _decode_git_path(record[2:])
    return True


def _capsule_release_path_kind(path: str) -> tuple[str, str | None]:
    if path in CAPSULE_RELEASE_DOCUMENT_PATHS:
        return "document", None
    parts = PurePosixPath(path).parts
    if (
        len(parts) == len(CAPSULE_RELEASE_ARTIFACT_PREFIX) + 2
        and parts[: len(CAPSULE_RELEASE_ARTIFACT_PREFIX)]
        == CAPSULE_RELEASE_ARTIFACT_PREFIX
        and CAPSULE_RELEASE_RUN_NAME.fullmatch(parts[-2]) is not None
        and parts[-1] in CAPSULE_RELEASE_ARTIFACT_FILES
    ):
        return "artifact", parts[-2]
    return "forbidden", None


def _raw_capsule_release_changes(
    output: bytes,
) -> tuple[set[str], set[str]] | None:
    changed_paths: set[str] = set()
    run_names: set[str] = set()
    for change in parse_git_raw_diff(output):
        if len(change.paths) != 1 or change.status in {"R", "C"}:
            return None
        path = change.paths[0]
        kind, run_name = _capsule_release_path_kind(path)
        if kind == "forbidden":
            return None
        if path in {"README.md", "benchmarks/README.md"}:
            if (
                change.status != "M"
                or change.old_mode != "100644"
                or change.new_mode != "100644"
            ):
                return None
        else:
            if (
                change.status != "A"
                or change.old_mode != "000000"
                or change.new_mode != "100644"
            ):
                return None
        changed_paths.add(path)
        if run_name is not None:
            run_names.add(run_name)
    return changed_paths, run_names


def _worktree_capsule_release_changes(
    output: bytes,
) -> tuple[set[str], set[str]] | None:
    changed_paths: set[str] = set()
    run_names: set[str] = set()
    for change in parse_git_porcelain_v2(output):
        if change.kind not in {"ordinary", "untracked"} or len(change.paths) != 1:
            return None
        path = change.paths[0]
        kind, run_name = _capsule_release_path_kind(path)
        if kind == "forbidden":
            return None
        if change.kind == "untracked":
            if path in {"README.md", "benchmarks/README.md"}:
                return None
        else:
            if change.submodule != "N..." or change.xy is None:
                return None
            allowed_statuses = {".M", "M.", "MM"}
            if path not in {"README.md", "benchmarks/README.md"}:
                allowed_statuses |= {"A.", "AM"}
            if (
                change.xy not in allowed_statuses
                or any(mode not in {"000000", "100644"} for mode in change.modes)
                or all(mode == "000000" for mode in change.modes)
            ):
                return None
        changed_paths.add(path)
        if run_name is not None:
            run_names.add(run_name)
    return changed_paths, run_names


def _current_raw_capsule_release_changes_are_valid(output: bytes) -> bool:
    """Reject staged/unstaged rename, copy, delete, type, and mode records."""

    for change in parse_git_raw_diff(output):
        if len(change.paths) != 1 or change.status not in {"A", "M"}:
            return False
        path = change.paths[0]
        kind, _ = _capsule_release_path_kind(path)
        if kind == "forbidden" or change.new_mode != "100644":
            return False
        if path in {"README.md", "benchmarks/README.md"}:
            if change.status != "M" or change.old_mode != "100644":
                return False
        elif (
            change.status == "A" and change.old_mode != "000000"
        ) or (
            change.status == "M" and change.old_mode != "100644"
        ):
            return False
    return True


def _worktree_path_is_non_executable_regular(path: str) -> bool:
    relative = PurePosixPath(path)
    current = ROOT
    for part in relative.parts[:-1]:
        current /= part
        metadata = os.lstat(current)
        if not stat.S_ISDIR(metadata.st_mode):
            return False
    snapshot = _stable_regular_file_snapshot(
        ROOT / path,
        f"Capsule release path {path}",
    )
    return (
        stat.S_IMODE(snapshot.mode)
        & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        == 0
    )


def _capsule_release_directory_is_safe(
    run_name: str,
    *,
    allow_partial: bool,
) -> bool:
    directory = ROOT.joinpath(*CAPSULE_RELEASE_ARTIFACT_PREFIX, run_name)
    metadata = os.lstat(directory)
    if not stat.S_ISDIR(metadata.st_mode):
        return False
    with os.scandir(directory) as entries:
        names = {entry.name for entry in entries}
    expected = set(CAPSULE_RELEASE_ARTIFACT_FILES)
    if (not names <= expected) or (not allow_partial and names != expected):
        return False
    return all(
        _worktree_path_is_non_executable_regular(
            PurePosixPath(*CAPSULE_RELEASE_ARTIFACT_PREFIX, run_name, name).as_posix()
        )
        for name in names
    )


def _git_path_is_absent(revision: str, path: str) -> bool:
    output = _git_capture(
        ["ls-tree", "-z", "--full-tree", revision, "--", path],
        text=False,
    )
    if not isinstance(output, bytes):  # pragma: no cover - subprocess contract
        raise RuntimeError("Git returned a non-byte tree listing")
    return output == b""


def capsule_release_changes_are_valid(commit: str, head: str) -> bool:
    """Allow only inert, single-release publication changes after ``commit``.

    This accepts an empty or partially generated publication state so a report
    can be rendered in stages.  The isolated release launcher separately
    requires the final root report and both per-run files to be present.
    """

    try:
        raw_output = _git_capture(
            [
                "diff",
                "--raw",
                "-z",
                "--no-abbrev",
                "--no-ext-diff",
                "--find-renames",
                "--find-copies",
                "--find-copies-harder",
                "--ignore-submodules=none",
                commit,
                head,
                "--",
            ],
            text=False,
        )
        if not isinstance(raw_output, bytes):  # pragma: no cover
            return False
        raw = _raw_capsule_release_changes(raw_output)
        if raw is None:
            return False
        status_before = _git_status_bytes()
        worktree = _worktree_capsule_release_changes(status_before)
        if worktree is None or not _git_index_is_fully_observable():
            return False
        raw_options = [
            "--raw",
            "-z",
            "--no-abbrev",
            "--no-ext-diff",
            "--find-renames",
            "--find-copies",
            "--find-copies-harder",
            "--ignore-submodules=none",
        ]
        staged_output = _git_capture(
            ["diff", "--cached", *raw_options, head, "--"],
            text=False,
        )
        unstaged_output = _git_capture(
            ["diff", *raw_options, "--"],
            text=False,
        )
        if (
            not isinstance(staged_output, bytes)
            or not isinstance(unstaged_output, bytes)
            or not _current_raw_capsule_release_changes_are_valid(staged_output)
            or not _current_raw_capsule_release_changes_are_valid(unstaged_output)
        ):
            return False
        changed_paths = raw[0] | worktree[0]
        run_names = raw[1] | worktree[1]
        if len(run_names) > 1:
            return False
        if not _git_path_is_absent(commit, "CAPSULE_BENCHMARK.md"):
            return False
        for path in changed_paths:
            kind, _ = _capsule_release_path_kind(path)
            if kind == "artifact" and not _git_path_is_absent(commit, path):
                return False
            if not _worktree_path_is_non_executable_regular(path):
                return False
        if run_names and not _capsule_release_directory_is_safe(
            next(iter(run_names)),
            allow_partial=True,
        ):
            return False
        status_after = _git_status_bytes()
        if status_after != status_before or not _git_index_is_fully_observable():
            return False
        return True
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def git_revision_attestation(
    required_paths: Iterable[Path | str],
    *,
    require_clean: bool,
) -> dict[str, Any]:
    """Capture the exact tracked code revision present at benchmark start."""

    paths = tuple(sorted({_git_relative_path(path) for path in required_paths}))
    if not paths:
        raise ValueError("Git revision attestation requires at least one code path")
    top = str(_git_capture(["rev-parse", "--show-toplevel"], text=True)).strip()
    if Path(top).resolve() != ROOT.resolve():
        raise RuntimeError("benchmark root is not the current Git worktree")

    def head() -> str:
        value = str(
            _git_capture(["rev-parse", "--verify", "HEAD^{commit}"], text=True)
        ).strip()
        if re.fullmatch(r"[0-9a-f]{40,64}", value) is None:
            raise RuntimeError("Git HEAD is not a full commit object id")
        return value

    def clean() -> bool:
        return _git_status_bytes() == b"" and _git_index_is_fully_observable()

    commit_before = head()
    clean_before = clean()
    entries: dict[str, dict[str, str]] = {}
    for path in paths:
        entry = git_tree_entry_at_revision(commit_before, path)
        if entry.object_type != "blob" or entry.mode not in {"100644", "100755"}:
            raise RuntimeError(f"required Git path is not a regular blob: {path}")
        entries[path] = {
            "git_mode": entry.mode,
            "git_type": entry.object_type,
            "git_object": entry.object_id,
            "sha256": sha256_bytes(git_blob_at_revision(commit_before, path)),
        }
    commit_after = head()
    clean_after = clean()
    if commit_after != commit_before:
        raise RuntimeError("Git HEAD changed while the run revision was captured")
    clean_at_start = clean_before and clean_after
    if require_clean and not clean_at_start:
        raise RuntimeError("Capsule benchmark requires a clean Git worktree at run start")
    return {
        "commit": commit_before,
        "worktree_clean_at_start": clean_at_start,
        "required_paths": entries,
    }


def git_revision_attestation_is_valid(
    value: Any,
    required_paths: Iterable[Path | str],
    *,
    environment_commit: Any,
) -> bool:
    """Verify required tree entries and worktree files against the run commit.

    A descendant commit is acceptable only for the explicit inert Capsule
    publication allowlist. Every required path must retain its exact mode, type,
    and blob, and the live regular file must retain matching bytes and
    executable bits.
    """

    try:
        paths = tuple(sorted({_git_relative_path(path) for path in required_paths}))
        if (
            not isinstance(value, dict)
            or set(value)
            != {"commit", "worktree_clean_at_start", "required_paths"}
            or value.get("worktree_clean_at_start") is not True
            or not isinstance(value.get("commit"), str)
            or re.fullmatch(r"[0-9a-f]{40,64}", value["commit"]) is None
            or environment_commit != value["commit"]
            or not isinstance(value.get("required_paths"), dict)
            or set(value["required_paths"]) != set(paths)
        ):
            return False
        commit = value["commit"]
        _git_capture(["cat-file", "-e", f"{commit}^{{commit}}"], text=False)
        head = str(
            _git_capture(["rev-parse", "--verify", "HEAD^{commit}"], text=True)
        ).strip()
        if re.fullmatch(r"[0-9a-f]{40,64}", head) is None:
            return False
        _git_capture(["merge-base", "--is-ancestor", commit, head], text=False)
        if not capsule_release_changes_are_valid(commit, head):
            return False
        for path in paths:
            recorded = value["required_paths"][path]
            if (
                not isinstance(recorded, dict)
                or set(recorded)
                != {"git_mode", "git_type", "git_object", "sha256"}
                or recorded.get("git_mode") not in {"100644", "100755"}
                or recorded.get("git_type") != "blob"
                or not isinstance(recorded.get("git_object"), str)
                or re.fullmatch(r"[0-9a-f]{40,64}", recorded["git_object"])
                is None
                or not isinstance(recorded.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", recorded["sha256"]) is None
            ):
                return False
            commit_entry = git_tree_entry_at_revision(commit, path)
            if (
                commit_entry.mode != recorded["git_mode"]
                or commit_entry.object_type != recorded["git_type"]
                or commit_entry.object_id != recorded["git_object"]
            ):
                return False
            attested_blob = git_blob_at_revision(commit, path)
            if sha256_bytes(attested_blob) != recorded["sha256"]:
                return False
            head_entry = git_tree_entry_at_revision(head, path)
            if (
                head_entry.mode != recorded["git_mode"]
                or head_entry.object_type != recorded["git_type"]
                or head_entry.object_id != recorded["git_object"]
            ):
                return False
            head_blob = git_blob_at_revision(head, path)
            if head_blob != attested_blob:
                return False
            worktree = _stable_regular_file_snapshot(
                ROOT / path,
                f"required Git worktree path {path}",
            )
            expected_executable_bits = (
                stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
                if recorded["git_mode"] == "100755"
                else 0
            )
            if (
                worktree.data != head_blob
                or stat.S_IMODE(worktree.mode)
                & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                != expected_executable_bits
            ):
                return False
        head_after = str(
            _git_capture(["rev-parse", "--verify", "HEAD^{commit}"], text=True)
        ).strip()
        if (
            head_after != head
            or not capsule_release_changes_are_valid(commit, head_after)
        ):
            return False
        return True
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def require_git_worktree_revision(
    value: dict[str, Any],
    required_paths: Iterable[Path | str],
) -> None:
    """Require loaded run code paths and HEAD to remain at the start revision."""

    paths = tuple(sorted({_git_relative_path(path) for path in required_paths}))
    commit = value.get("commit") if isinstance(value, dict) else None
    if not isinstance(commit, str):
        raise RuntimeError("run has no pinned Git code revision")
    head = str(
        _git_capture(["rev-parse", "--verify", "HEAD^{commit}"], text=True)
    ).strip()
    if head != commit:
        raise RuntimeError("Git HEAD changed after the run revision was captured")
    recorded_paths = value.get("required_paths")
    if not isinstance(recorded_paths, dict) or set(recorded_paths) != set(paths):
        raise RuntimeError("run Git code-path attestation is incomplete")
    diff = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", *paths],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    if diff.returncode != 0:
        raise RuntimeError("required benchmark code changed after run start")
    for path in paths:
        recorded = recorded_paths[path]
        if (
            not isinstance(recorded, dict)
            or set(recorded)
            != {"git_mode", "git_type", "git_object", "sha256"}
            or recorded.get("git_mode") not in {"100644", "100755"}
            or recorded.get("git_type") != "blob"
        ):
            raise RuntimeError(f"required benchmark code attestation is invalid: {path}")
        entry = git_tree_entry_at_revision(commit, path)
        if (
            entry.mode != recorded["git_mode"]
            or entry.object_type != recorded["git_type"]
            or entry.object_id != recorded.get("git_object")
        ):
            raise RuntimeError(f"required benchmark code tree entry changed: {path}")
        captured = _stable_regular_file_snapshot(
            ROOT / path,
            f"required benchmark code path {path}",
        )
        executable_bits = stat.S_IMODE(captured.mode) & (
            stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        expected_executable_bits = (
            stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            if recorded["git_mode"] == "100755"
            else 0
        )
        if executable_bits != expected_executable_bits:
            raise RuntimeError(f"required benchmark code mode changed: {path}")
        if sha256_bytes(captured.data) != recorded.get("sha256"):
            raise RuntimeError(f"required benchmark code bytes changed: {path}")


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


class ImplementationRunDocument(dict[str, Any]):
    """Serializable evidence paired with immutable provider-input trees."""

    __slots__ = ("starter_snapshots", "grading_snapshots")

    def __init__(
        self,
        payload: dict[str, Any],
        starter_snapshots: dict[str, FixtureTreeSnapshot],
        grading_snapshots: dict[str, GradingSnapshot],
    ) -> None:
        super().__init__(payload)
        self.starter_snapshots = starter_snapshots
        self.grading_snapshots = grading_snapshots


def create_run_document(
    args: argparse.Namespace,
    cases: list[BenchmarkCase],
    semantic_specs: dict[str, str],
    cases_dir: Path,
) -> ImplementationRunDocument:
    corpus = discover_cases(cases_dir=cases_dir)
    snapshots: dict[str, dict[str, Any]] = {}
    starter_snapshots: dict[str, FixtureTreeSnapshot] = {}
    grading_snapshots: dict[str, GradingSnapshot] = {}
    for case in cases:
        fixture = snapshot_fixture_tree(case.path)
        starter = fixture_subtree_snapshot(fixture, "starter")
        grading = grading_snapshot_from_fixture(case, fixture)
        if not getattr(args, "semantic_dir", None):
            curated = fixture_snapshot_file(fixture, "semantic.spec.ctx").data.decode(
                "utf-8"
            )
            if semantic_specs[case.id] != curated:
                raise RuntimeError(
                    f"{case.id}: curated semantic input changed before snapshot"
                )
        snapshots[case.id] = benchmark_case_snapshot(
            case,
            semantic_specs[case.id],
            fixture_snapshot=fixture,
            starter_snapshot=starter,
            grading_snapshot=grading,
        )
        starter_snapshots[case.id] = starter
        grading_snapshots[case.id] = grading
    return ImplementationRunDocument({
        "schema_version": 2,
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
        "case_suite": cases_dir.name,
        "full_corpus": {case.id for case in cases} == {case.id for case in corpus},
        "semantic_source": "generated" if args.semantic_dir else "curated",
        "oracle_exposure": (
            "reduced: descriptor-captured hidden tests are materialized only in a "
            "private grader fixture; visible smoke assertions are shared equally "
            "across arms"
        ),
        "fixture_snapshot": snapshots,
        "static": static_rows_from_benchmark_snapshots(cases, snapshots),
        "results": [],
    }, starter_snapshots, grading_snapshots)


def counterbalanced_variant_order(
    case_id: str,
    repetition: int,
    variants: Iterable[str],
    seed: int,
) -> tuple[str, ...]:
    """Rotate arm order per fixture so first-position exposure is balanced."""

    arms = tuple(variants)
    if not arms or len(arms) != len(set(arms)):
        raise ValueError("counterbalanced schedule requires distinct arms")
    if repetition < 1:
        raise ValueError("counterbalanced schedule repetition must be positive")
    digest = hashlib.sha256(
        f"semantic-spec-arm-order\0{seed}\0{case_id}".encode("utf-8")
    ).digest()
    initial_offset = int.from_bytes(digest[:8], "big") % len(arms)
    offset = (initial_offset + repetition - 1) % len(arms)
    return arms[offset:] + arms[:offset]


def implementation_job_schedule(
    cases: Iterable[BenchmarkCase],
    repetitions: int,
    seed: int,
) -> list[tuple[BenchmarkCase, int, str]]:
    """Return the seeded arm schedule from canonical fixture order.

    Report credibility recomputes this from on-disk fixtures, never from a
    result document's mutable ``cases`` order.
    """

    ordered_cases = sorted(cases, key=lambda case: case.id)
    pairs = [
        (case, repetition)
        for case in ordered_cases
        for repetition in range(1, repetitions + 1)
    ]
    random.Random(seed).shuffle(pairs)
    jobs: list[tuple[BenchmarkCase, int, str]] = []
    for case, repetition in pairs:
        variants = counterbalanced_variant_order(
            case.id,
            repetition,
            VARIANTS,
            seed,
        )
        jobs.extend((case, repetition, variant) for variant in variants)
    return jobs


def execute_benchmark(args: argparse.Namespace) -> Path:
    cases_dir = (args.cases_dir or CASES_DIR).resolve()
    cases = discover_cases(args.case, cases_dir)
    errors = validate(cases)
    if errors:
        raise RuntimeError("benchmark validation failed:\n" + "\n".join(errors))
    semantic_specs = load_semantic_specs(cases, args.semantic_dir)

    output = args.output or RESULTS_DIR / (
        datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ") + ".json"
    )
    output = lexical_output_path(output)
    document = create_run_document(args, cases, semantic_specs, cases_dir)
    checkpoint = open_result_checkpoint(output, document, force=args.force)
    jobs = implementation_job_schedule(cases, args.repetitions, args.seed)

    keep_root = RESULTS_DIR / "workspaces" / document["run_id"]
    temporary_context = None
    try:
        if args.keep_workspaces:
            keep_root.mkdir(parents=True, exist_ok=False)
            workspace_root = keep_root
        else:
            temporary_context = tempfile.TemporaryDirectory(prefix="semantic-spec-bench-")
            workspace_root = Path(temporary_context.name)
        for index, (case, repetition, variant) in enumerate(jobs, start=1):
            expected_snapshot = document["fixture_snapshot"][case.id]
            require_benchmark_case_snapshot(
                case, semantic_specs[case.id], expected_snapshot
            )
            run_root = workspace_root / f"{index:03d}-{case.id}-{variant}-r{repetition}"
            run_root.mkdir(parents=True, exist_ok=False)
            workspace = safe_workspace(
                case,
                run_root,
                starter_snapshot=document.starter_snapshots[case.id],
            )
            spec = benchmark_snapshot_specifications(expected_snapshot)[variant]
            prompt = benchmark_prompt(spec)
            print(
                f"[{index}/{len(jobs)}] {case.id} {variant} repetition={repetition}",
                flush=True,
            )
            provider_result = {
                "return_code": None,
                "duration_seconds": None,
                "usage": {},
                "tool_calls": {},
                "tool_call_total": None,
                "event_errors": ["provider did not start"],
                "final_message_metadata": text_metadata(""),
                "stderr_metadata": text_metadata(""),
            }
            provider_completed = False
            verification = None
            grading_snapshot = document.grading_snapshots[case.id]
            grade = empty_grade(case, grading_snapshot)
            run_errors: list[str] = []
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
                provider_completed = True
            except subprocess.TimeoutExpired:
                provider_result["duration_seconds"] = args.timeout_seconds
                provider_result["event_errors"] = ["provider timeout"]
                run_errors.append("provider timeout")
            except Exception as exc:  # noqa: BLE001 - preserve failed run and continue
                provider_result["event_errors"] = [f"{type(exc).__name__}: {exc}"]
                run_errors.append(f"provider {type(exc).__name__}: {exc}")

            if provider_completed:
                if provider_result.get("return_code") != 0:
                    run_errors.append(
                        f"provider exited with {provider_result.get('return_code')}"
                    )
                run_errors.extend(provider_result.get("event_errors", []))
                trusted = args.provider == "mock"
                verification_failed = False
                try:
                    verification = run_verification(
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
                    grade = run_grader(
                        case,
                        workspace,
                        trusted=trusted,
                        grading_snapshot=grading_snapshot,
                    )
                except subprocess.TimeoutExpired:
                    run_errors.append("grader timeout")
                except Exception as exc:  # noqa: BLE001 - preserve provider telemetry
                    run_errors.append(f"grader {type(exc).__name__}: {exc}")
                if verification_failed:
                    grade["task_success"] = False

            require_benchmark_case_snapshot(
                case, semantic_specs[case.id], expected_snapshot
            )
            error = "; ".join(run_errors) if run_errors else None

            usage = provider_result.get("usage", {})
            result = redact_result_telemetry({
                "case": case.id,
                "pair_id": f"{case.id}:r{repetition}",
                "variant": variant,
                "repetition": repetition,
                "run_order": index,
                "spec": text_metrics(spec),
                "provenance": {
                    "spec_sha256": expected_snapshot["variants"][variant],
                    "prompt_sha256": sha256_bytes(prompt.encode("utf-8")),
                    "starter_sha256": expected_snapshot["starter_sha256"],
                    "fixture_sha256": expected_snapshot["fixture_sha256"],
                },
                "provider": provider_result,
                "verification": verification,
                "grade": grade,
                "cost_usd": estimate_cost(usage, args.pricing),
                "error": error,
            })
            document["results"].append(result)
            checkpoint.write_json(document)
    finally:
        if temporary_context is not None:
            temporary_context.cleanup()
        checkpoint.close()

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
        "total_tool_calls": sum(
            result["provider"]["tool_call_total"]
            for result in selected
            if result["provider"].get("tool_call_total") is not None
        ),
        "total_command_executions": sum(
            result["provider"].get("tool_calls", {}).get("command_execution", 0)
            for result in selected
        ),
        "total_cost_usd": round(sum(
            result["cost_usd"] for result in selected if result["cost_usd"] is not None
        ), 6) if any(result["cost_usd"] is not None for result in selected) else None,
    }


def quality_preserved(results: list[dict[str, Any]]) -> bool:
    def not_worse(selected: list[dict[str, Any]]) -> bool:
        baseline = aggregate_variant(selected, "baseline")
        semantic = aggregate_variant(selected, "semantic")
        return bool(
            semantic["task_success_rate"] >= baseline["task_success_rate"]
            and semantic["acceptance_pass_rate"] >= baseline["acceptance_pass_rate"]
            and semantic["test_pass_rate"] >= baseline["test_pass_rate"]
        )

    if not not_worse(results):
        return False
    cases = {result["case"] for result in results}
    return all(
        not_worse([result for result in results if result["case"] == case])
        for case in cases
    )


def paired_result_index(
    results: list[dict[str, Any]],
    variants: Iterable[str] = VARIANTS,
) -> tuple[
    dict[tuple[str, int], dict[str, dict[str, Any]]],
    set[tuple[str, int]],
]:
    """Index paired records without allowing a duplicate arm to overwrite one.

    Report inputs are untrusted.  A duplicate must invalidate its entire pair;
    choosing the last row would let an attacker select favorable telemetry while
    retaining the expected result count.
    """

    allowed = set(variants)
    pairs: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    invalid: set[tuple[str, int]] = set()
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
            or variant not in allowed
        ):
            continue
        key = (case, repetition)
        pair = pairs.setdefault(key, {})
        if variant in pair:
            invalid.add(key)
            continue
        pair[variant] = result
    return pairs, invalid


def paired_reductions(results: list[dict[str, Any]], field: str) -> list[float]:
    pairs, invalid_pairs = paired_result_index(results)
    reductions = []
    for key, pair in pairs.items():
        if key in invalid_pairs:
            continue
        if set(pair) != set(VARIANTS):
            continue
        baseline = pair["baseline"]["provider"].get("usage", {}).get(field)
        semantic = pair["semantic"]["provider"].get("usage", {}).get(field)
        if baseline and semantic is not None:
            reductions.append((baseline - semantic) / baseline * 100)
    return reductions


def metric_delta_percent(baseline: float | int, semantic: float | int) -> float | None:
    if not baseline:
        return None
    return round((semantic - baseline) / baseline * 100, 3)


def paired_summary(results: list[dict[str, Any]], field: str) -> dict[str, Any]:
    pairs, invalid_pairs = paired_result_index(results)

    by_case: dict[str, list[float]] = {}
    values: list[float] = []
    for (case, repetition), pair in pairs.items():
        if (case, repetition) in invalid_pairs:
            continue
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
        "invalid_pairs": len(invalid_pairs),
    }


def format_delta(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.3f}%"


def recorded_case_corpus(document: dict[str, Any]) -> list[BenchmarkCase]:
    suite_name = document.get("case_suite", "cases")
    if not isinstance(suite_name, str) or CASE_ID.fullmatch(suite_name) is None:
        raise ValueError(f"invalid recorded case suite: {suite_name!r}")
    suite_dir = (BENCHMARKS / suite_name).resolve(strict=True)
    suite_dir.relative_to(BENCHMARKS.resolve())
    return discover_cases(cases_dir=suite_dir)


def implementation_report_is_credible(
    document: dict[str, Any],
    results: list[dict[str, Any]],
) -> bool:
    cases = document.get("cases")
    repetitions = document.get("repetitions")
    seed = document.get("seed")
    semantic_source = document.get("semantic_source", "curated")
    snapshots = document.get("fixture_snapshot")
    try:
        corpus = recorded_case_corpus(document)
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
        or not isinstance(snapshots, dict)
        or set(snapshots) != corpus_ids
        or not isinstance(document.get("static"), list)
        or not results
    ):
        return False

    specifications_by_case: dict[str, dict[str, str]] = {}
    grading_by_case: dict[str, GradingSnapshot] = {}
    cases_by_id = {case.id: case for case in corpus}
    try:
        for case in corpus:
            expected = snapshots[case.id]
            specifications = benchmark_snapshot_specifications(expected)
            fixture = snapshot_fixture_tree(case.path)
            starter = fixture_subtree_snapshot(fixture, "starter")
            baseline = fixture_snapshot_file(fixture, "baseline.md").data.decode(
                "utf-8"
            )
            if specifications["baseline"] != baseline:
                return False
            if semantic_source == "curated":
                curated = fixture_snapshot_file(
                    fixture,
                    "semantic.spec.ctx",
                ).data.decode("utf-8")
                if specifications["semantic"] != curated:
                    return False
            derived = benchmark_case_snapshot(
                case,
                specifications["semantic"],
                fixture_snapshot=fixture,
                starter_snapshot=starter,
            )
            if expected != derived:
                return False
            specifications_by_case[case.id] = specifications
            grading_by_case[case.id] = grading_snapshot_from_fixture(case, fixture)
        expected_static = static_rows_from_benchmark_snapshots(corpus, snapshots)
    except (KeyError, OSError, RuntimeError, TypeError, UnicodeError, ValueError):
        return False
    if document["static"] != expected_static:
        return False

    for case_id, specifications in specifications_by_case.items():
        snapshot = snapshots[case_id]
        for variant, specification in specifications.items():
            if (
                snapshot["variants"][variant]
                != sha256_bytes(specification.encode("utf-8"))
                or snapshot["metrics"][variant] != text_metrics(specification)
                or snapshot["prompts"][variant]
                != sha256_bytes(benchmark_prompt(specification).encode("utf-8"))
            ):
                return False

    expected_keys = {
        (case, repetition, variant)
        for case in corpus_ids
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
    run_orders = [result.get("run_order") for result in results]
    if (
        len(actual_keys) != len(set(actual_keys))
        or set(actual_keys) != expected_keys
        or any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in run_orders
        )
        or sorted(run_orders) != list(range(1, len(results) + 1))
    ):
        return False
    expected_order = [
        (case.id, repetition, variant)
        for case, repetition, variant in implementation_job_schedule(
            corpus, repetitions, seed
        )
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

    for result in results:
        case = cases_by_id[result["case"]]
        variant = result["variant"]
        snapshot = snapshots[case.id]
        specification = specifications_by_case[case.id][variant]
        provider = result.get("provider")
        usage = provider.get("usage") if isinstance(provider, dict) else None
        provenance = result.get("provenance")
        expected_provenance = {
            "spec_sha256": sha256_bytes(specification.encode("utf-8")),
            "prompt_sha256": sha256_bytes(
                benchmark_prompt(specification).encode("utf-8")
            ),
            "starter_sha256": snapshot["starter_sha256"],
            "fixture_sha256": snapshot["fixture_sha256"],
        }
        verification = result.get("verification")
        verification_command = case.manifest.get("verification_command")
        verification_valid = (
            verification is None
            if not verification_command
            else isinstance(verification, dict)
            and text_reference_matches(
                verification,
                "command",
                "command_metadata",
                verification_command,
            )
            and verification.get("fixture_sha256")
            == snapshot["verification_fixture_sha256"]
            and verification.get("return_code") == 0
        )
        grade = result.get("grade")
        if (
            result.get("pair_id") != f"{case.id}:r{result['repetition']}"
            or result.get("spec") != text_metrics(specification)
            or result.get("error") is not None
            or provenance != expected_provenance
            or not isinstance(provider, dict)
            or provider.get("return_code") != 0
            or provider.get("event_errors") != []
            or not nonnegative_number(provider.get("duration_seconds"))
            or not nonnegative_int(provider.get("tool_call_total"))
            or not isinstance(usage, dict)
            or any(
                not nonnegative_int(usage.get(field))
                for field in (
                    "input_tokens",
                    "uncached_input_tokens",
                    "output_tokens",
                )
            )
            or not verification_valid
            or not isinstance(grade, dict)
            or not isinstance(grade.get("task_success"), bool)
            or not grade_matches_grading_snapshot(
                grade,
                grading_by_case[case.id],
            )
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


def canonical_document_sha256(document: dict[str, Any]) -> str | None:
    """Return the canonical artifact hash used to identify legacy evidence."""

    try:
        payload = (
            json.dumps(document, indent=2, ensure_ascii=True, sort_keys=True) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return sha256_bytes(payload)


def is_historical_implementation_document(document: dict[str, Any]) -> bool:
    """Recognize the one tracked, immutable pre-snapshot result document only."""

    return canonical_document_sha256(document) == HISTORICAL_IMPLEMENTATION_SHA256


def _historical_report_is_credible(document: dict[str, Any]) -> bool:
    """Reproduce the historical presentation gate without weakening current trust."""

    try:
        results = document["results"]
        complete_pairs = len(results) == (
            len(document["cases"]) * document["repetitions"] * len(VARIANTS)
        )
        full_corpus = set(document["cases"]) == {
            case.id for case in discover_cases()
        }
        return bool(
            document["provider"] != "mock"
            and document.get("model")
            and document.get("reasoning_effort")
            and document["repetitions"] >= 3
            and full_corpus
            and complete_pairs
            and all(result.get("error") is None for result in results)
            and all(result["provider"].get("return_code") == 0 for result in results)
            and all(
                "input_tokens" in result["provider"].get("usage", {})
                for result in results
            )
        )
    except (KeyError, TypeError):
        return False


def render_report(document: dict[str, Any]) -> str:
    results = document["results"]
    baseline = aggregate_variant(results, "baseline")
    semantic = aggregate_variant(results, "semantic")
    input_reductions = paired_reductions(results, "input_tokens")
    uncached_reductions = paired_reductions(results, "uncached_input_tokens")
    input_summary = paired_summary(results, "input_tokens")
    uncached_summary = paired_summary(results, "uncached_input_tokens")
    historical = is_historical_implementation_document(document)
    credible = (
        _historical_report_is_credible(document)
        if historical
        else implementation_report_is_credible(document, results)
    )

    lines = [
        "# Semantic Spec Writer Benchmark",
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
    if not historical:
        lines.insert(-3, f"Case suite: `{document.get('case_suite', 'cases')}`  ")
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
    if not historical:
        total_metrics.extend([
            ("Shell command executions", "total_command_executions"),
            ("Tool calls", "total_tool_calls"),
        ])
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
    measured_quality_preserved = quality_preserved(results)
    input_saving = bool(
        credible
        and measured_quality_preserved
        and input_ci
        and input_ci[0] > 0
        and semantic["total_input_tokens"] < baseline["total_input_tokens"]
    )
    uncached_saving = bool(
        credible
        and measured_quality_preserved
        and uncached_ci
        and uncached_ci[0] > 0
        and semantic["total_uncached_input_tokens"]
        < baseline["total_uncached_input_tokens"]
    )
    if credible and not measured_quality_preserved:
        interpretation = (
            "The semantic variant did not preserve measured implementation quality. "
            "Token and latency deltas are not product benefits when acceptance behavior regresses."
        )
    elif not credible:
        interpretation = (
            "This is a directional smoke run. It can expose regressions and motivate a full "
            "run, but it does not support a general token or latency claim."
        )
    elif input_saving and uncached_saving:
        interpretation = (
            "This corpus shows a statistically supported reduction in both total and uncached "
            "input tokens while preserving measured implementation quality."
        )
    elif input_saving:
        interpretation = (
            "This corpus shows a statistically supported reduction in total input tokens, but "
            "not in uncached input tokens. Cached and uncached usage must not be presented as "
            "the same cost result."
        )
    else:
        interpretation = (
            "This run preserves the measured behavior but does not demonstrate a reliable "
            "end-to-end input-token saving when corpus totals and confidence intervals are "
            "considered."
        )
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
        "usage includes the full agent loop, not only the specification text. " + interpretation,
        "",
        "## Limitations",
        "",
        "- Small, synthetic Python fixtures are not representative of every codebase.",
        f"- Results cover one model (`{document.get('model') or 'provider default'}`) and "
        f"one reasoning effort (`{document.get('reasoning_effort') or 'provider default'}`).",
        "- The benchmark excludes the one-time cost of creating or reviewing a semantic spec.",
        (
            "- Acceptance tests are held outside the agent workspace, but the runner "
            "does not use a container and therefore cannot prove oracle isolation "
            "against a hostile agent."
            if historical
            else "- Hidden tests and hidden expected outputs stay outside the solution process. "
            "Visible smoke assertions are restored from immutable fixtures for both arms. "
            "Implementation agents use workspace-write; hidden solution calls run in a "
            "network-disabled read-only sandbox. This is not a full VM boundary."
        ),
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
    validate_parser.add_argument("--cases-dir", type=Path)

    static_parser = subparsers.add_parser("static", help="measure document sizes")
    static_parser.add_argument("--case", action="append", default=[])
    static_parser.add_argument("--cases-dir", type=Path)
    static_parser.add_argument("--semantic-dir", type=Path)
    static_parser.add_argument(
        "--token-encoding",
        help="optional tiktoken encoding, for example o200k_base",
    )
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
    run_parser.add_argument("--cases-dir", type=Path)
    run_parser.add_argument(
        "--semantic-dir",
        type=Path,
        help="use <case>.spec.ctx files from this directory instead of curated fixtures",
    )
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
            cases = discover_cases(args.case, args.cases_dir)
            errors = validate(cases)
            if errors:
                print("\n".join(errors), file=sys.stderr)
                return 1
            print(f"validated {len(cases)} benchmark cases")
            return 0
        if args.command == "static":
            cases = discover_cases(args.case, args.cases_dir)
            semantic_specs = load_semantic_specs(cases, args.semantic_dir)
            rows = static_rows(
                cases,
                semantic_specs,
                load_token_encoder(args.token_encoding),
            )
            print(json.dumps(rows, indent=2) if args.json else render_static_markdown(rows), end="")
            if args.check and any(
                row["semantic"]["bytes"] >= row["baseline"]["bytes"]
                or row["semantic"]["words"] >= row["baseline"]["words"]
                or (
                    args.token_encoding
                    and row["semantic"]["tokens"] >= row["baseline"]["tokens"]
                )
                for row in rows
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
            with open_pinned_json(args.result) as result_input:
                report = render_report(result_input.document)
                if args.output:
                    write_report_from_pinned_inputs(
                        args.output,
                        report,
                        overwrite=args.force,
                        inputs=[result_input],
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
