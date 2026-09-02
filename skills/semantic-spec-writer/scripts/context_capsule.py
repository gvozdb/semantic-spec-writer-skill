#!/usr/bin/env python3
"""Build and verify tamper-evident Capsule v5 execution contexts.

Capsules are deliberately small, self-contained byte streams.  Their packet and
source payloads are length-framed, so routed source can contain arbitrary UTF-8
text without relying on a delimiter that might occur in the source itself.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import importlib.util
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
CHECKER_PATH = SCRIPT_DIR / "check_execution_packet.py"


def _load_packet_checker() -> Any:
    """Load the sibling checker when this script is imported by file path."""

    module_name = "_semantic_spec_packet_checker"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, CHECKER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load Packet v3 checker: {CHECKER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


packet_checker = _load_packet_checker()

CAPSULE_VERSION = 5
LEGACY_CAPSULE_VERSION = 4
CAPSULE_EXECUTION_PROFILE = "capsule-v5-terminal-1"
CAPSULE_PROTOCOL = {
    "completion": "all_routed_edits_then_exact_V1_once",
    "edit_strategy": "single_bundled_file_change",
    "first_action": "file_change",
    "no_edit": "failure",
    "pre_edit_discovery": "forbidden",
    "pre_edit_read_budget": 0,
    "pre_edit_verification": "forbidden",
    "recovery": "normal_mode_after_exact_frame_mismatch_or_failed_V1",
    "source_authority": "sealed_current_source_frames",
    "terminal_lock": "sealed_tail_state_machine",
    "verification": "exact_V1_alone_once_after_all_routed_edits",
    "work_units": "route_actions_adjacent_to_source",
}
LEGACY_CAPSULE_PROTOCOL = {
    "first_action": "file_change_or_one_routed_read",
    "pre_edit_discovery": "forbidden",
    "pre_edit_read_budget": 1,
    "recovery": "exact_frame_mismatch_or_failed_V1",
    "source_authority": "sealed_frames",
    "verification": "V1_after_edit_once",
}
CAPSULE_CONTROL = (
    "IMPLEMENTATION REQUIRED. The packet frame is the task. Source frames are current "
    "pre-edit repository bytes, not patches or desired output. FIRST use one bundled "
    "file-change operation containing every edit/create route; do not split the edit. "
    "Each source frame's do list is mandatory; internally check every item before that "
    "single change without using a tool. Before the complete change, do not read files "
    "or run commands. THEN run the exact V1 command alone, exactly once. A pass ends "
    "the task: answer immediately with no status, diff, check, or other tool. The sealed "
    "terminal lock at the tail repeats this exact state transition. A failed V1 or "
    "exact-frame mismatch exits this fast path into normal recovery; any other tool "
    "call or a passing early V1 fails the fast path."
)
CAPSULE_EXECUTION = (
    "IMPLEMENTATION REQUIRED: packet=task; source frames=current pre-edit bytes, not "
    "desired output; each source frame do list=mandatory local work unit -> internally "
    "check every do, then one bundled file-change operation contains all routed work; do "
    "not split -> no read/command before complete change -> exact V1 alone exactly once "
    "-> pass=final answer immediately with zero tools remaining; sealed tail lock repeats "
    "the transition; failed V1/frame mismatch=normal recovery outside fast path; any "
    "other tool call or early V1=fast-path failure"
)
LEGACY_CAPSULE_EXECUTION = (
    "sealed frames are exact patch operands -> edit immediately or use at most one "
    "bundled routed read when needed; no discovery/status/baseline V1 -> edit all "
    "routed symbols in one pass -> V1 once -> stop on pass; expand only after exact-"
    "frame mismatch or failed V1"
)
MAGIC = b"CAPSULE-V5\n"
LEGACY_MAGIC = b"CAPSULE-V4\n"
EXECUTE_PREFIX = b"@EXECUTE "
HEADER_PREFIX = b"@HEADER "
FRAME_PREFIX = b"@FRAME "
TERMINAL_PREFIX = b"@TERMINAL "
SEAL_PREFIX = b"@SEAL "
TERMINAL_STOP_LINE = (
    b"@STOP V1_EXIT_0=FINAL_ANSWER_NOW;TOOLS_REMAINING=0;"
    b"NO_STATUS_DIFF_CHECK_OR_OTHER_TOOL\n"
)
CAPSULE_TERMINAL_PROFILE = (
    "CAPSULE-TERMINAL/1 authoritative execution control\n"
    "Allowed fast path: one bundled file_change for every sealed mutation path -> "
    "exact V1 once -> final answer.\n"
    "If exact V1 exits 0, return the final answer immediately; tools remaining=0.\n"
    "After that pass, do not run syntax, test, read, status, diff, discovery, "
    "verification, or any other tool."
)
EXECUTE_LINE = EXECUTE_PREFIX + CAPSULE_CONTROL.encode("ascii") + b"\n"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_SOURCE_COUNT = 100_000
# This bounds one complete wire artifact, rather than an individual Packet or
# routed regular file (which remain capped at 64 MiB by the Packet checker).
MAX_CAPSULE_BYTES = 128 * 1024 * 1024


class CapsuleError(ValueError):
    """A Capsule v5 input, framing, or validation failure."""


def _sha256(data: bytes | bytearray | memoryview) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_capsule_size(byte_length: int, label: str = "capsule") -> None:
    if byte_length > MAX_CAPSULE_BYTES:
        raise CapsuleError(
            f"{label} exceeds aggregate Capsule limit of "
            f"{MAX_CAPSULE_BYTES} bytes"
        )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value: {value}")


def _parse_canonical_json(payload: bytes, label: str) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CapsuleError(f"{label} is not UTF-8") from exc
    try:
        value = json.loads(text, parse_constant=_reject_json_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CapsuleError(f"malformed {label} JSON") from exc
    if not isinstance(value, dict):
        raise CapsuleError(f"{label} must be a JSON object")
    if _canonical_json(value) != payload:
        raise CapsuleError(f"{label} JSON is not canonical")
    return value


def _line(data: bytes, offset: int, label: str) -> tuple[bytes, int]:
    end = data.find(b"\n", offset)
    if end < 0:
        raise CapsuleError(f"malformed {label}: missing newline")
    return data[offset:end], end + 1


def _prefixed_json(
    data: bytes,
    offset: int,
    prefix: bytes,
    label: str,
) -> tuple[dict[str, Any], int]:
    line, offset = _line(data, offset, label)
    if not line.startswith(prefix):
        raise CapsuleError(f"malformed {label}")
    payload = line[len(prefix) :]
    if not payload:
        raise CapsuleError(f"malformed {label}: empty JSON")
    return _parse_canonical_json(payload, label), offset


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _require_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise CapsuleError(f"{label} has unexpected metadata keys")


def _read_frame(
    data: bytes,
    offset: int,
    label: str,
) -> tuple[dict[str, Any], bytes, int]:
    descriptor, offset = _prefixed_json(data, offset, FRAME_PREFIX, label)
    byte_length = descriptor.get("byte_length")
    content_sha256 = descriptor.get("content_sha256")
    if type(byte_length) is not int or byte_length < 0:
        raise CapsuleError(f"{label} has invalid byte_length")
    if not _is_sha256(content_sha256):
        raise CapsuleError(f"{label} has invalid content_sha256")
    end = offset + byte_length
    if end > len(data):
        raise CapsuleError(f"malformed {label}: byte_length exceeds capsule")
    payload = data[offset:end]
    return descriptor, payload, end


def _parse_capsule(
    data: bytes,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    bytes,
    list[tuple[dict[str, Any], bytes]],
    dict[str, Any] | None,
    str,
]:
    _require_capsule_size(len(data))
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CapsuleError("capsule is not UTF-8") from exc
    if data.startswith(MAGIC):
        wire_version = CAPSULE_VERSION
        offset = len(MAGIC)
        execute_line, offset = _line(data, offset, "execution control")
        if execute_line != EXECUTE_LINE.removesuffix(b"\n"):
            raise CapsuleError("capsule has invalid execution control")
        expected_protocol = CAPSULE_PROTOCOL
    elif data.startswith(LEGACY_MAGIC):
        wire_version = LEGACY_CAPSULE_VERSION
        offset = len(LEGACY_MAGIC)
        expected_protocol = LEGACY_CAPSULE_PROTOCOL
    else:
        raise CapsuleError("capsule has invalid magic")

    header, offset = _prefixed_json(data, offset, HEADER_PREFIX, "header")
    _require_keys(
        header,
        {
            "packet_sha256",
            "protocol",
            "route_sha256",
            "source_count",
            "version",
        },
        "header",
    )
    if type(header["version"]) is not int or header["version"] != wire_version:
        raise CapsuleError("capsule has unsupported version")
    if not _is_sha256(header["packet_sha256"]):
        raise CapsuleError("header has invalid packet_sha256")
    if not _is_sha256(header["route_sha256"]):
        raise CapsuleError("header has invalid route_sha256")
    if header["protocol"] != expected_protocol:
        raise CapsuleError(f"header has invalid Capsule v{wire_version} protocol")
    source_count = header["source_count"]
    if (
        type(source_count) is not int
        or source_count < 0
        or source_count > MAX_SOURCE_COUNT
    ):
        raise CapsuleError("header has invalid source_count")

    packet_descriptor, embedded_packet, offset = _read_frame(
        data, offset, "packet frame"
    )
    sources: list[tuple[dict[str, Any], bytes]] = []
    for index in range(source_count):
        descriptor, payload, offset = _read_frame(
            data, offset, f"source frame {index}"
        )
        sources.append((descriptor, payload))

    terminal: dict[str, Any] | None = None
    if wire_version == CAPSULE_VERSION:
        if offset >= len(data) or data[offset : offset + 1] != b"\n":
            raise CapsuleError("capsule has missing terminal lock separator")
        terminal, offset = _prefixed_json(
            data,
            offset + 1,
            TERMINAL_PREFIX,
            "terminal lock",
        )
        _validate_terminal_contract(terminal)
        stop_line, offset = _line(data, offset, "terminal stop control")
        if stop_line != TERMINAL_STOP_LINE.removesuffix(b"\n"):
            raise CapsuleError("capsule has invalid terminal stop control")

    seal_start = offset
    seal, offset = _prefixed_json(data, offset, SEAL_PREFIX, "seal")
    _require_keys(seal, {"sha256"}, "seal")
    if not _is_sha256(seal["sha256"]):
        raise CapsuleError("seal has invalid SHA-256")
    if offset != len(data):
        raise CapsuleError("capsule has trailing bytes")
    if _sha256(data[:seal_start]) != seal["sha256"]:
        raise CapsuleError("capsule seal SHA-256 mismatch")
    framed_payloads = [(packet_descriptor, embedded_packet, "packet frame")]
    framed_payloads.extend(
        (descriptor, payload, f"source frame {index}")
        for index, (descriptor, payload) in enumerate(sources)
    )
    for descriptor, payload, label in framed_payloads:
        if _sha256(payload) != descriptor["content_sha256"]:
            raise CapsuleError(f"{label} content SHA-256 mismatch")
    return (
        header,
        packet_descriptor,
        embedded_packet,
        sources,
        terminal,
        seal["sha256"],
    )


def _normalise_newlines(text: str) -> str:
    """Match ``Path.read_text`` universal-newline behavior used by Packet v3."""

    return text.replace("\r\n", "\n").replace("\r", "\n")


def _rewrite_execution_policy(text: str, old: str, new: str) -> str:
    rewritten: list[str] = []
    matched = 0
    for raw_line in text.splitlines(keepends=True):
        parts = raw_line.splitlines()
        body = parts[0] if parts else ""
        ending = raw_line[len(body) :]
        match = packet_checker.EXECUTION_LINE.fullmatch(body)
        if match is not None and match.group(1) == old:
            rewritten.append(
                body[: match.start(1)] + new + body[match.end(1) :] + ending
            )
            matched += 1
        else:
            rewritten.append(raw_line)
    if matched != 1:
        raise CapsuleError("packet does not have exactly one canonical execution policy")
    return "".join(rewritten)


def _embedded_packet(packet_text: str) -> str:
    return _rewrite_execution_policy(
        packet_text,
        packet_checker.BOUNDED_EXECUTION,
        CAPSULE_EXECUTION,
    )


def _reconstructed_packet(
    embedded_text: str,
    version: int = CAPSULE_VERSION,
) -> str:
    execution = (
        CAPSULE_EXECUTION
        if version == CAPSULE_VERSION
        else LEGACY_CAPSULE_EXECUTION
    )
    policies = packet_checker.parse_execution_policies(embedded_text)
    if policies != [execution]:
        raise CapsuleError(
            f"embedded packet requires exactly one canonical Capsule v{version} policy"
        )
    return _rewrite_execution_policy(
        embedded_text,
        execution,
        packet_checker.BOUNDED_EXECUTION,
    )


def _require_capsule_v1_verification(text: str) -> None:
    """Enforce the single verification step promised by Capsule v5."""

    entries = packet_checker.parse_verify_entries(text)
    if len(entries) != 1 or entries[0][0] != "V1":
        raise CapsuleError(
            "Capsule v5 requires exactly one verification entry named V1"
        )


def _terminal_contract(text: str, targets: list[Any]) -> dict[str, Any]:
    """Build the sealed final-state reminder from validated Packet data."""

    _require_capsule_v1_verification(text)
    command = packet_checker.parse_verify_entries(text)[0][1]
    edit_paths = [
        target.relative_path
        for target in targets
        if target.kind in {"edit", "create"}
    ]
    if not edit_paths:
        raise CapsuleError("Capsule v5 requires at least one edit or create route")
    return {
        "kind": "capsule_v5_execution_lock",
        "on_v1_failure": "normal_recovery_fast_path_failed",
        "on_v1_pass": "FINAL_ANSWER_NOW_NO_MORE_TOOLS",
        "step_1": {
            "count": 1,
            "mode": "bundled_all_routes",
            "paths": edit_paths,
            "tool": "file_change",
        },
        "step_2": {
            "command": command,
            "count": 1,
            "name": "V1",
            "tool": "command_execution",
        },
        "tool_call_budget": 2,
    }


def _validate_terminal_contract(value: dict[str, Any]) -> None:
    _require_keys(
        value,
        {
            "kind",
            "on_v1_failure",
            "on_v1_pass",
            "step_1",
            "step_2",
            "tool_call_budget",
        },
        "terminal lock",
    )
    step_1 = value["step_1"]
    step_2 = value["step_2"]
    if (
        value["kind"] != "capsule_v5_execution_lock"
        or value["on_v1_failure"] != "normal_recovery_fast_path_failed"
        or value["on_v1_pass"] != "FINAL_ANSWER_NOW_NO_MORE_TOOLS"
        or type(value["tool_call_budget"]) is not int
        or value["tool_call_budget"] != 2
        or not isinstance(step_1, dict)
        or not isinstance(step_2, dict)
    ):
        raise CapsuleError("terminal lock has invalid state machine")
    _require_keys(step_1, {"count", "mode", "paths", "tool"}, "terminal step 1")
    _require_keys(step_2, {"command", "count", "name", "tool"}, "terminal step 2")
    paths = step_1["paths"]
    if (
        type(step_1["count"]) is not int
        or step_1["count"] != 1
        or step_1["mode"] != "bundled_all_routes"
        or step_1["tool"] != "file_change"
        or not isinstance(paths, list)
        or not paths
        or any(not isinstance(path, str) or not path for path in paths)
        or type(step_2["count"]) is not int
        or step_2["count"] != 1
        or step_2["name"] != "V1"
        or step_2["tool"] != "command_execution"
        or not isinstance(step_2["command"], str)
        or not step_2["command"]
        or "\n" in step_2["command"]
        or "\r" in step_2["command"]
    ):
        raise CapsuleError("terminal lock has invalid action steps")
    if len(paths) != len(set(paths)):
        raise CapsuleError("terminal lock has duplicate mutation paths")


def _terminal_block(contract: dict[str, Any]) -> bytes:
    return (
        b"\n"
        + TERMINAL_PREFIX
        + _canonical_json(contract)
        + b"\n"
        + TERMINAL_STOP_LINE
    )


def _route_action_groups(text: str, targets: list[Any]) -> tuple[tuple[str, ...], ...]:
    """Return validated Packet actions in the same order as parsed routes."""

    groups: list[list[str]] = []
    current: list[str] | None = None
    in_route = False
    for line in text.splitlines():
        if line and not line[0].isspace():
            in_route = line.strip() == "route:"
            current = None
            continue
        if not in_route:
            continue
        route_match = packet_checker.ROUTE_LINE.fullmatch(line)
        if route_match is not None:
            current = []
            groups.append(current)
            continue
        action_match = packet_checker.ACTION_LINE.fullmatch(line)
        if action_match is not None:
            if current is None:
                raise CapsuleError("packet has an orphan routed action")
            current.append(action_match.group(1).strip())
            continue
        if packet_checker.EXPAND_LINE.fullmatch(line) is not None:
            current = None
    if len(groups) != len(targets):
        raise CapsuleError("packet route/action grouping changed during Capsule build")
    return tuple(tuple(group) for group in groups)


def _open_repo(repo: Path | str) -> Any:
    try:
        return packet_checker.secure_open_directory(repo, "repository")
    except (OSError, RuntimeError, ValueError) as exc:
        raise CapsuleError(str(exc)) from exc


def _open_regular(
    path: Path | str,
    label: str,
    *,
    max_bytes: int | None = None,
) -> Any:
    try:
        if max_bytes is None:
            return packet_checker.secure_open_regular(path, label)
        return packet_checker.secure_open_regular(
            path,
            label,
            max_bytes=max_bytes,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise CapsuleError(str(exc)) from exc


def _selected_source(target: Any, content: bytes) -> bytes:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CapsuleError(f"routed file is not UTF-8: {target.relative_path}") from exc

    if target.start is None:
        selected = content
    else:
        lines = text.splitlines(keepends=True)
        if target.end is None or target.end > len(lines):
            raise CapsuleError(
                f"line range exceeds {target.relative_path}: "
                f"{target.start}-{target.end} > {len(lines)}"
            )
        selected = "".join(lines[target.start - 1 : target.end]).encode("utf-8")

    if target.anchor:
        selected_text = selected.decode("utf-8")
        occurrences = selected_text.count(target.anchor)
        if occurrences != 1:
            detail = "not found" if occurrences == 0 else "ambiguous"
            raise CapsuleError(
                f"source anchor {detail} in routed context for "
                f"{target.relative_path}: {target.anchor}"
            )
    return selected


def _range_metadata(target: Any) -> dict[str, int] | None:
    if target.start is None:
        return None
    return {"start": target.start, "end": target.end}


def _packet_descriptor(
    payload: bytes,
    version: int = CAPSULE_VERSION,
) -> dict[str, Any]:
    descriptor = {
        "byte_length": len(payload),
        "content_sha256": _sha256(payload),
        "kind": "packet",
    }
    if version == CAPSULE_VERSION:
        descriptor["role"] = "authoritative_task_requirements"
    return descriptor


def _validate_packet_descriptor(
    descriptor: dict[str, Any],
    version: int = CAPSULE_VERSION,
) -> None:
    expected_keys = {"byte_length", "content_sha256", "kind"}
    if version == CAPSULE_VERSION:
        expected_keys.add("role")
    _require_keys(
        descriptor,
        expected_keys,
        "packet frame",
    )
    if descriptor["kind"] != "packet":
        raise CapsuleError("packet frame has invalid kind")
    if (
        version == CAPSULE_VERSION
        and descriptor["role"] != "authoritative_task_requirements"
    ):
        raise CapsuleError("packet frame has invalid role")


def _source_descriptor(
    index: int,
    target: Any,
    payload: bytes,
    version: int = CAPSULE_VERSION,
    *,
    actions: tuple[str, ...] = (),
) -> dict[str, Any]:
    descriptor = {
        "anchor": target.anchor,
        "byte_length": len(payload),
        "content_sha256": _sha256(payload),
        "kind": target.kind,
        "path": target.relative_path,
        "range": _range_metadata(target),
        "route_index": index,
    }
    if version == CAPSULE_VERSION:
        descriptor["do"] = list(actions)
        descriptor["role"] = "current_pre_edit_source"
    return descriptor


def _validate_source_descriptor(
    descriptor: dict[str, Any],
    version: int = CAPSULE_VERSION,
) -> None:
    expected_keys = {
        "anchor",
        "byte_length",
        "content_sha256",
        "kind",
        "path",
        "range",
        "route_index",
    }
    if version == CAPSULE_VERSION:
        expected_keys.add("do")
        expected_keys.add("role")
    _require_keys(
        descriptor,
        expected_keys,
        "source frame",
    )
    if descriptor["kind"] not in {"read", "edit"}:
        raise CapsuleError("source frame has invalid kind")
    if version == CAPSULE_VERSION and descriptor["role"] != "current_pre_edit_source":
        raise CapsuleError("source frame has invalid role")
    if version == CAPSULE_VERSION and (
        not isinstance(descriptor["do"], list)
        or any(not isinstance(action, str) or not action for action in descriptor["do"])
        or (descriptor["kind"] == "edit" and not descriptor["do"])
    ):
        raise CapsuleError("source frame has invalid do actions")
    if not isinstance(descriptor["path"], str) or not descriptor["path"]:
        raise CapsuleError("source frame has invalid path")
    if descriptor["anchor"] is not None and not isinstance(
        descriptor["anchor"], str
    ):
        raise CapsuleError("source frame has invalid anchor")
    if type(descriptor["route_index"]) is not int or descriptor["route_index"] < 0:
        raise CapsuleError("source frame has invalid route_index")
    line_range = descriptor["range"]
    if line_range is None:
        return
    if not isinstance(line_range, dict) or set(line_range) != {"start", "end"}:
        raise CapsuleError("source frame has invalid range")
    if (
        type(line_range["start"]) is not int
        or type(line_range["end"]) is not int
        or line_range["start"] < 1
        or line_range["end"] < line_range["start"]
    ):
        raise CapsuleError("source frame has invalid range")


def _frame(descriptor: dict[str, Any], payload: bytes) -> bytes:
    return FRAME_PREFIX + _canonical_json(descriptor) + b"\n" + payload


def _frame_length(descriptor: dict[str, Any], payload: bytes) -> int:
    return len(FRAME_PREFIX) + len(_canonical_json(descriptor)) + 1 + len(payload)


def _seal_line(seal: str) -> bytes:
    return SEAL_PREFIX + _canonical_json({"sha256": seal}) + b"\n"


def _token_count(capsule: bytes, encoder: Any | None) -> int | None:
    if encoder is None:
        return None
    return len(encoder.encode(capsule.decode("utf-8")))


def _validate_token_budget(
    capsule: bytes,
    encoder: Any | None,
    max_context_tokens: int | None,
) -> int | None:
    if max_context_tokens is None:
        return _token_count(capsule, encoder)
    if type(max_context_tokens) is not int or max_context_tokens < 1:
        raise CapsuleError("--max-context-tokens must be positive")
    if encoder is None:
        raise CapsuleError("--max-context-tokens requires --encoding")
    tokens = _token_count(capsule, encoder)
    assert tokens is not None
    if tokens > max_context_tokens:
        raise CapsuleError(
            f"context token budget exceeded: {tokens} > {max_context_tokens}"
        )
    return tokens


def _base_metrics(
    capsule: bytes,
    header: dict[str, Any],
    source_bytes: int,
    encoder: Any | None,
    tokens: int | None,
    seal_sha256: str,
    packet_bound: bool,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "capsule": {
            "bytes": len(capsule),
            "lines": len(capsule.decode("utf-8").splitlines()),
            "seal_sha256": seal_sha256,
        },
        "encoding": getattr(encoder, "name", None) if encoder is not None else None,
        "packet_sha256": header["packet_sha256"],
        "packet_bound": packet_bound,
        "route_sha256": header["route_sha256"],
        "source_bytes": source_bytes,
        "source_count": header["source_count"],
        "valid": True,
        "version": header["version"],
    }
    if tokens is not None:
        metrics["capsule"]["tokens"] = tokens
    return metrics


def _lexical_path_key(path: Path | str) -> str:
    """Canonicalize spelling only; POSIX // and / name the same local root."""

    absolute = os.path.abspath(os.fspath(path))
    if os.name == "posix":
        absolute = os.sep + absolute.lstrip(os.sep)
    return os.path.normpath(absolute)


class _InputReference:
    """A pinned input name, or a pinned parent plus an absent create name."""

    def __init__(
        self,
        display: Path,
        absolute_lexical: str,
        identity: tuple[int, int] | None,
        parent_identity: tuple[int, int] | None,
        leaf: str,
        fd: int | None = None,
        parent: Any | None = None,
        stat_result: os.stat_result | None = None,
        data: bytes | None = None,
        missing_parts: tuple[str, ...] = (),
    ) -> None:
        self.display = display
        self.absolute_lexical = absolute_lexical
        self.identity = identity
        self.parent_identity = parent_identity
        self.leaf = leaf
        self.fd = fd
        self.parent = parent
        self.stat_result = stat_result
        self.data = data
        self.missing_parts = missing_parts

    @classmethod
    def from_secure_file(cls, opened: Any) -> _InputReference:
        parent = opened.parent.clone(f"input parent {opened.path}")
        descriptor = -1
        try:
            descriptor = os.dup(opened.fd)
            metadata = os.fstat(descriptor)
            return cls(
                display=opened.path,
                absolute_lexical=_lexical_path_key(opened.path),
                identity=opened.identity,
                parent_identity=opened.parent_identity,
                leaf=opened.leaf,
                fd=descriptor,
                parent=parent,
                stat_result=metadata,
                data=opened.data,
            )
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            parent.close()
            raise

    @classmethod
    def missing(
        cls,
        display: Path,
        parent: Any,
        missing_parts: tuple[str, ...],
        parent_is_exact: bool,
    ) -> _InputReference:
        pinned_parent = parent.clone(f"create target parent {display}")
        return cls(
            display=display,
            absolute_lexical=_lexical_path_key(display),
            identity=None,
            parent_identity=(
                (parent.stat_result.st_dev, parent.stat_result.st_ino)
                if parent_is_exact
                else None
            ),
            leaf=missing_parts[-1],
            parent=pinned_parent,
            missing_parts=missing_parts,
        )

    @classmethod
    def lexical(cls, display: Path) -> _InputReference:
        return cls(
            display=display,
            absolute_lexical=_lexical_path_key(display),
            identity=None,
            parent_identity=None,
            leaf=display.name,
        )

    def revalidate(self) -> None:
        label = f"input {self.display}"
        if self.parent is None:
            return
        try:
            self.parent.validate_identity(f"{label} parent")
        except (OSError, RuntimeError, ValueError) as exc:
            raise CapsuleError(f"{label} changed during publication: {exc}") from exc

        if self.identity is None:
            if not self.missing_parts:
                raise CapsuleError(f"{label} has no missing-target binding")
            try:
                os.stat(
                    self.missing_parts[0],
                    dir_fd=self.parent.fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise CapsuleError(
                    f"cannot revalidate missing {label}: {exc}"
                ) from exc
            else:
                raise CapsuleError(f"missing {label} changed during publication")
            self.parent.validate_identity(f"{label} parent")
            return

        assert self.fd is not None
        assert self.stat_result is not None
        assert self.data is not None
        metadata = os.fstat(self.fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or packet_checker._changed_stat(self.stat_result, metadata)
        ):
            raise CapsuleError(f"{label} changed during publication")
        try:
            current_data = packet_checker._read_stable_regular_fd(self.fd, label)
        except (OSError, RuntimeError, ValueError) as exc:
            raise CapsuleError(f"{label} changed during publication: {exc}") from exc
        if (
            current_data != self.data
            or packet_checker._changed_stat(self.stat_result, os.fstat(self.fd))
        ):
            raise CapsuleError(f"{label} changed during publication")

        try:
            reopened = os.open(
                self.leaf,
                packet_checker._regular_flags(),
                dir_fd=self.parent.fd,
            )
        except OSError as exc:
            raise CapsuleError(f"{label} changed during publication: {exc}") from exc
        try:
            reopened_stat = os.fstat(reopened)
            reopened_data = packet_checker._read_stable_regular_fd(reopened, label)
            if (
                packet_checker._changed_stat(self.stat_result, reopened_stat)
                or reopened_data != self.data
                or packet_checker._changed_stat(
                    self.stat_result,
                    os.fstat(reopened),
                )
            ):
                raise CapsuleError(f"{label} changed during publication")
        finally:
            os.close(reopened)
        self.parent.validate_identity(f"{label} parent")

    def aliases_missing_output_ancestor(
        self,
        output_lexical: str,
        output_parent_identity: tuple[int, int],
        output_leaf: str,
    ) -> bool:
        """Whether an output would occupy this create route's missing path chain."""

        if self.identity is not None or self.parent is None or not self.missing_parts:
            return False

        ancestor = self.parent.path
        for part in self.missing_parts:
            ancestor = ancestor / part
            if output_lexical == _lexical_path_key(ancestor):
                return True

        return (
            _stat_identity(self.parent.stat_result) == output_parent_identity
            and output_leaf == self.missing_parts[0]
        )

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.parent is not None:
            self.parent.close()
            self.parent = None


def _close_input_references(references: list[_InputReference]) -> None:
    for reference in references:
        reference.close()


def _build_capsule(
    repo: Path | str,
    packet: Path | str,
    *,
    encoder: Any | None = None,
    max_context_tokens: int | None = None,
) -> tuple[bytes, dict[str, Any], list[_InputReference]]:
    repo_handle = _open_repo(repo)
    try:
        packet_file = _open_regular(packet, "packet")
    except Exception:
        repo_handle.close()
        raise
    snapshot = None
    input_references: list[_InputReference] = []
    try:
        packet_bytes = packet_file.data
        packet_text = packet_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        packet_file.close()
        repo_handle.close()
        raise CapsuleError("packet is not UTF-8") from exc

    try:
        v3_text = _normalise_newlines(packet_text)
        _require_capsule_v1_verification(v3_text)
        try:
            targets = packet_checker.parse_routes(v3_text)
            terminal = _terminal_contract(v3_text, targets)
            if len(targets) > MAX_SOURCE_COUNT:
                raise CapsuleError(
                    f"Packet v3 has too many routes for Capsule v5: "
                    f"{len(targets)} > {MAX_SOURCE_COUNT}"
                )
            snapshot = packet_checker.open_route_snapshot(
                repo_handle,
                targets,
                max_total_bytes=MAX_CAPSULE_BYTES,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise CapsuleError(f"invalid Packet v3 routes: {exc}") from exc
        try:
            v3_result, v3_errors = packet_checker.validate_text(
                repo_handle,
                v3_text,
                encoder,
                True,
                route_snapshot=snapshot,
                revalidate_snapshot=False,
            )
        except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
            raise CapsuleError(f"cannot validate Packet v3: {exc}") from exc
        if v3_errors:
            raise CapsuleError("invalid Packet v3: " + "; ".join(v3_errors))
        route_actions = _route_action_groups(v3_text, targets)
        route_hash = v3_result.get("route_sha256")
        if not _is_sha256(route_hash) or route_hash != snapshot.route_sha256():
            raise CapsuleError("Packet v3 did not produce the pinned route SHA-256")

        sources: list[tuple[dict[str, Any], bytes]] = []
        for index, (target, entry) in enumerate(
            zip(targets, snapshot.entries, strict=True)
        ):
            if target.kind == "create":
                continue
            if entry.file is None:
                raise CapsuleError(
                    f"routed file does not exist: {target.relative_path}"
                )
            payload = _selected_source(target, entry.file.data)
            sources.append((
                _source_descriptor(
                    index,
                    target,
                    payload,
                    actions=route_actions[index],
                ),
                payload,
            ))

        if len(sources) > MAX_SOURCE_COUNT:
            raise CapsuleError("Capsule has too many source frames")
        snapshot.assert_creates_absent()
        embedded = _embedded_packet(packet_text).encode("utf-8")
        terminal_block = _terminal_block(terminal)
        header = {
            "packet_sha256": _sha256(packet_bytes),
            "protocol": CAPSULE_PROTOCOL,
            "route_sha256": route_hash,
            "source_count": len(sources),
            "version": CAPSULE_VERSION,
        }
        header_line = HEADER_PREFIX + _canonical_json(header) + b"\n"
        embedded_descriptor = _packet_descriptor(embedded)
        planned_length = (
            len(MAGIC)
            + len(EXECUTE_LINE)
            + len(header_line)
            + _frame_length(embedded_descriptor, embedded)
            + sum(_frame_length(descriptor, payload) for descriptor, payload in sources)
            + len(terminal_block)
            # The final SHA-256 is always 64 ASCII hex characters, so this is
            # the exact trailer length without serializing the body first.
            + len(_seal_line("0" * 64))
        )
        _require_capsule_size(planned_length, "built capsule")

        # Size has been proven before this buffer grows. Keep the established
        # wire format and ordering so ordinary Capsules remain byte-for-byte
        # deterministic.
        body = bytearray(MAGIC)
        body.extend(EXECUTE_LINE)
        body.extend(header_line)
        body.extend(_frame(embedded_descriptor, embedded))
        for descriptor, payload in sources:
            body.extend(_frame(descriptor, payload))
        body.extend(terminal_block)
        seal = _sha256(body)
        body.extend(_seal_line(seal))
        if len(body) != planned_length:
            raise CapsuleError("Capsule serialization size changed unexpectedly")
        capsule = bytes(body)
        tokens = _validate_token_budget(capsule, encoder, max_context_tokens)

        # Success is gated on fresh descriptor-relative reads of every existing
        # route and repeated absence checks for every create target.
        try:
            snapshot.revalidate()
            packet_file.revalidate("packet")
            snapshot.assert_creates_absent()
        except (OSError, RuntimeError, ValueError) as exc:
            raise CapsuleError(f"inputs changed while building capsule: {exc}") from exc

        input_references.append(_InputReference.from_secure_file(packet_file))
        repo_display = repo_handle.path
        for entry in snapshot.entries:
            display = packet_checker.resolve_inside(
                repo_display,
                entry.target.relative_path,
            )
            if entry.file is None:
                if entry.create_parent is None:
                    raise CapsuleError(
                        f"create target has no pinned parent: {entry.target.relative_path}"
                    )
                input_references.append(
                    _InputReference.missing(
                        display,
                        entry.create_parent,
                        entry.create_missing_parts,
                        entry.create_parent_is_exact,
                    )
                )
            else:
                input_references.append(
                    _InputReference.from_secure_file(entry.file)
                )

        metrics = _base_metrics(
            capsule,
            header,
            sum(len(payload) for _, payload in sources),
            encoder,
            tokens,
            seal,
            True,
        )
        try:
            snapshot.revalidate()
            packet_file.revalidate("packet")
            snapshot.assert_creates_absent()
        except (OSError, RuntimeError, ValueError) as exc:
            raise CapsuleError(f"inputs changed while building capsule: {exc}") from exc
        return capsule, metrics, input_references
    except Exception:
        _close_input_references(input_references)
        raise
    finally:
        if snapshot is not None:
            snapshot.close()
        packet_file.close()
        repo_handle.close()


def build_capsule(
    repo: Path | str,
    packet: Path | str,
    *,
    encoder: Any | None = None,
    max_context_tokens: int | None = None,
) -> bytes:
    """Build a Capsule v5 byte stream without writing it to disk."""

    capsule, _, input_references = _build_capsule(
        repo,
        packet,
        encoder=encoder,
        max_context_tokens=max_context_tokens,
    )
    try:
        return capsule
    finally:
        _close_input_references(input_references)


def build_capsule_with_metrics(
    repo: Path | str,
    packet: Path | str,
    *,
    encoder: Any | None = None,
    max_context_tokens: int | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Build a Capsule v5 byte stream and return compact measurement data."""

    capsule, metrics, input_references = _build_capsule(
        repo,
        packet,
        encoder=encoder,
        max_context_tokens=max_context_tokens,
    )
    try:
        return capsule, metrics
    finally:
        _close_input_references(input_references)


def _read_capsule_input(
    capsule: bytes | bytearray | memoryview | Path | str,
) -> tuple[bytes, Any | None]:
    if isinstance(capsule, (Path, str)):
        opened = _open_regular(
            capsule,
            "capsule",
            max_bytes=MAX_CAPSULE_BYTES,
        )
        return opened.data, opened
    if isinstance(capsule, bytes):
        _require_capsule_size(len(capsule))
        return capsule, None
    if isinstance(capsule, bytearray):
        _require_capsule_size(len(capsule))
        return bytes(capsule), None
    if isinstance(capsule, memoryview):
        _require_capsule_size(capsule.nbytes)
        try:
            data = capsule.tobytes()
        except (BufferError, TypeError, ValueError) as exc:
            raise CapsuleError("capsule memoryview cannot be copied") from exc
        _require_capsule_size(len(data))
        return data, None
    raise CapsuleError("capsule must be bytes-like or a file path")


def _check_capsule(
    repo: Path | str,
    capsule: bytes | bytearray | memoryview | Path | str,
    *,
    packet: Path | str | None = None,
    encoder: Any | None = None,
    max_context_tokens: int | None = None,
) -> dict[str, Any]:
    if packet is None:
        raise CapsuleError("trusted packet is required for Capsule v5 validity")

    repo_handle = _open_repo(repo)
    capsule_file = None
    trusted_packet = None
    snapshot = None
    try:
        data, capsule_file = _read_capsule_input(capsule)
        trusted_packet = _open_regular(packet, "trusted packet")
        (
            header,
            packet_descriptor,
            embedded_packet,
            sources,
            terminal,
            seal,
        ) = _parse_capsule(data)
        version = header["version"]
        _validate_packet_descriptor(packet_descriptor, version)
        expected_packet_descriptor = _packet_descriptor(embedded_packet, version)
        if packet_descriptor != expected_packet_descriptor:
            raise CapsuleError("packet frame metadata does not match embedded packet")
        try:
            embedded_text = embedded_packet.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CapsuleError("embedded packet is not UTF-8") from exc
        original_text = _reconstructed_packet(embedded_text, version)
        original_bytes = original_text.encode("utf-8")
        if _sha256(original_bytes) != header["packet_sha256"]:
            raise CapsuleError("original packet SHA-256 mismatch")
        if trusted_packet.data != original_bytes:
            raise CapsuleError("capsule does not match trusted packet")

        v3_text = _normalise_newlines(original_text)
        _require_capsule_v1_verification(v3_text)
        try:
            targets = packet_checker.parse_routes(v3_text)
            if len(targets) > MAX_SOURCE_COUNT:
                raise CapsuleError(
                    f"Packet v3 has too many routes for Capsule v5: "
                    f"{len(targets)} > {MAX_SOURCE_COUNT}"
                )
            snapshot = packet_checker.open_route_snapshot(
                repo_handle,
                targets,
                max_total_bytes=MAX_CAPSULE_BYTES,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise CapsuleError(
                f"invalid reconstructed Packet v3 routes: {exc}"
            ) from exc
        try:
            v3_result, v3_errors = packet_checker.validate_text(
                repo_handle,
                v3_text,
                encoder,
                True,
                route_snapshot=snapshot,
                revalidate_snapshot=False,
            )
        except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
            raise CapsuleError(
                f"cannot validate reconstructed Packet v3: {exc}"
            ) from exc
        if v3_errors:
            raise CapsuleError(
                "reconstructed Packet v3 is invalid: " + "; ".join(v3_errors)
            )
        if version == CAPSULE_VERSION and terminal != _terminal_contract(
            v3_text,
            targets,
        ):
            raise CapsuleError("terminal lock does not match reconstructed Packet v3")
        route_actions = _route_action_groups(v3_text, targets)
        route_hash = v3_result.get("route_sha256")
        declared_route_hash = v3_result.get("declared_route_sha256")
        if (
            not _is_sha256(route_hash)
            or header["route_sha256"] != route_hash
            or declared_route_hash != route_hash
            or route_hash != snapshot.route_sha256()
        ):
            raise CapsuleError("route SHA-256 does not match reconstructed Packet v3")

        expected_sources: list[tuple[dict[str, Any], bytes]] = []
        for index, (target, entry) in enumerate(
            zip(targets, snapshot.entries, strict=True)
        ):
            if target.kind == "create":
                continue
            if entry.file is None:
                raise CapsuleError(
                    f"routed file does not exist: {target.relative_path}"
                )
            payload = _selected_source(target, entry.file.data)
            expected_sources.append(
                (
                    _source_descriptor(
                        index,
                        target,
                        payload,
                        version,
                        actions=route_actions[index],
                    ),
                    payload,
                )
            )
        if len(sources) != len(expected_sources):
            raise CapsuleError("capsule source frame count does not match packet routes")
        for index, ((descriptor, payload), (expected, current)) in enumerate(
            zip(sources, expected_sources, strict=True)
        ):
            _validate_source_descriptor(descriptor, version)
            if descriptor != expected:
                raise CapsuleError(f"source frame metadata mismatch at index {index}")
            if payload != current:
                raise CapsuleError(f"source frame content mismatch at index {index}")

        tokens = _validate_token_budget(data, encoder, max_context_tokens)
        snapshot.revalidate()
        trusted_packet.revalidate("trusted packet")
        if capsule_file is not None:
            capsule_file.revalidate("capsule")
        snapshot.assert_creates_absent()
        metrics = _base_metrics(
            data,
            header,
            sum(len(payload) for _, payload in expected_sources),
            encoder,
            tokens,
            seal,
            True,
        )
        snapshot.revalidate()
        trusted_packet.revalidate("trusted packet")
        if capsule_file is not None:
            capsule_file.revalidate("capsule")
        snapshot.assert_creates_absent()
        return metrics
    finally:
        if snapshot is not None:
            snapshot.close()
        if trusted_packet is not None:
            trusted_packet.close()
        if capsule_file is not None:
            capsule_file.close()
        repo_handle.close()


def check_capsule(
    repo: Path | str,
    capsule: bytes | bytearray | memoryview | Path | str,
    *,
    packet: Path | str | None = None,
    encoder: Any | None = None,
    max_context_tokens: int | None = None,
) -> dict[str, Any]:
    """Check a Capsule v5 without mutating the repository or capsule file.

    Validation failures are returned as JSON-ready metrics rather than raised so
    benchmark callers can assert fail-closed behavior without shelling out.
    """

    reported_version = CAPSULE_VERSION
    if isinstance(capsule, (bytes, bytearray, memoryview)):
        prefix = bytes(capsule[: len(LEGACY_MAGIC)])
        if prefix == LEGACY_MAGIC:
            reported_version = LEGACY_CAPSULE_VERSION

    try:
        return _check_capsule(
            repo,
            capsule,
            packet=packet,
            encoder=encoder,
            max_context_tokens=max_context_tokens,
        )
    except (
        CapsuleError,
        OSError,
        RuntimeError,
        UnicodeError,
        ValueError,
        TypeError,
    ) as exc:
        return {
            "errors": [str(exc)],
            "valid": False,
            "version": reported_version,
        }


class _OutputTarget:
    def __init__(
        self,
        display: Path,
        parent_path: Path,
        parent: Any,
        leaf: str,
        input_references: list[_InputReference],
    ) -> None:
        self.display = display
        self.parent_path = parent_path
        self.parent = parent
        self.leaf = leaf
        self.input_references = input_references

    @property
    def parent_identity(self) -> tuple[int, int]:
        metadata = self.parent.stat_result
        return metadata.st_dev, metadata.st_ino

    def revalidate_parent(self) -> None:
        try:
            self.parent.validate_identity("output parent ancestry")
        except (OSError, RuntimeError, ValueError) as exc:
            raise CapsuleError(
                f"output parent ancestry changed during publication: {exc}"
            ) from exc
        _assert_trusted_output_parent(os.fstat(self.parent.fd))
        try:
            reopened = packet_checker.secure_open_directory(
                self.parent_path,
                "output parent",
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise CapsuleError(f"output parent changed during publication: {exc}") from exc
        try:
            current = reopened.stat_result
            if (current.st_dev, current.st_ino) != self.parent_identity:
                raise CapsuleError("output parent changed during publication")
            _assert_trusted_output_parent(current)
        finally:
            reopened.close()

    def close(self) -> None:
        self.parent.close()


def _output_lstat(target: _OutputTarget, name: str | None = None) -> os.stat_result | None:
    leaf = target.leaf if name is None else name
    try:
        return os.stat(leaf, dir_fd=target.parent.fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CapsuleError(f"cannot inspect output {target.display}: {exc}") from exc


def _stat_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _assert_trusted_output_parent(metadata: os.stat_result) -> None:
    if metadata.st_uid != os.geteuid():
        raise CapsuleError("output parent must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) & stat.S_IWOTH:
        raise CapsuleError("output parent must not be world-writable")


def _assert_output_not_input(
    target: _OutputTarget,
    metadata: os.stat_result | None,
) -> None:
    output_lexical = _lexical_path_key(target.display)
    output_identity = _stat_identity(metadata) if metadata is not None else None
    for reference in target.input_references:
        if output_lexical == reference.absolute_lexical:
            raise CapsuleError(f"output path aliases input file: {target.display}")
        if reference.aliases_missing_output_ancestor(
            output_lexical,
            target.parent_identity,
            target.leaf,
        ):
            raise CapsuleError(f"output path aliases input file: {target.display}")
        if (
            reference.parent_identity == target.parent_identity
            and reference.leaf == target.leaf
        ):
            raise CapsuleError(f"output path aliases input file: {target.display}")
        if output_identity is not None and output_identity == reference.identity:
            raise CapsuleError(f"output path aliases input file: {target.display}")


def _assert_identity_not_input(
    target: _OutputTarget,
    metadata: os.stat_result,
    label: str,
) -> None:
    identity = _stat_identity(metadata)
    for reference in target.input_references:
        if reference.identity is not None and identity == reference.identity:
            raise CapsuleError(f"{label} aliases input file: {reference.display}")


def _revalidate_output_against_inputs(
    target: _OutputTarget,
) -> os.stat_result | None:
    """Recheck every input name and compare both sides of that work to output."""

    metadata = _output_lstat(target)
    _assert_output_not_input(target, metadata)
    for reference in target.input_references:
        reference.revalidate()
    current = _output_lstat(target)
    _assert_output_not_input(target, current)
    return current


def _check_output_path(
    output: Path | str,
    input_references: list[_InputReference],
) -> _OutputTarget:
    path = Path(output)
    leaf = path.name
    if leaf in {"", ".", ".."}:
        raise CapsuleError(f"output must name a file: {path}")
    try:
        parent = packet_checker.secure_open_directory(path.parent, "output parent")
    except (OSError, RuntimeError, ValueError) as exc:
        raise CapsuleError(str(exc)) from exc
    target = _OutputTarget(path, path.parent, parent, leaf, input_references)
    try:
        target.revalidate_parent()
        metadata = _output_lstat(target)
        if metadata is not None and stat.S_ISLNK(metadata.st_mode):
            raise CapsuleError(f"output path cannot be a symlink: {path}")
        if metadata is not None and not stat.S_ISREG(metadata.st_mode):
            raise CapsuleError(f"output path is not a regular file: {path}")
        _assert_output_not_input(target, metadata)
        _revalidate_output_against_inputs(target)
        return target
    except Exception:
        target.close()
        raise


_RENAME_EXCHANGE = 0x2


def _publication_libc() -> Any:
    if (
        os.name != "posix"
        or os.link not in os.supports_dir_fd
        or os.mkdir not in os.supports_dir_fd
        or os.unlink not in os.supports_dir_fd
    ):
        raise CapsuleError(
            "secure Capsule publication is unavailable on this platform"
        )
    library = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = library.renameat2
    except AttributeError as exc:
        raise CapsuleError(
            "secure Capsule publication primitives are unavailable"
        ) from exc
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


def _link_named_no_clobber(
    source_directory_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
) -> None:
    os.link(
        source_name,
        destination_name,
        src_dir_fd=source_directory_fd,
        dst_dir_fd=destination_fd,
        follow_symlinks=False,
    )


def _rename_exchange_at(
    library: Any,
    left_fd: int,
    left: str,
    right_fd: int,
    right: str,
    before_exchange: Any | None = None,
) -> None:
    if before_exchange is not None:
        before_exchange()
    _rename_exchange_syscall(library, left_fd, left, right_fd, right)


def _rename_exchange_syscall(
    library: Any,
    left_fd: int,
    left: str,
    right_fd: int,
    right: str,
) -> None:
    """Invoke RENAME_EXCHANGE after any userspace validation has completed."""

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


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        try:
            written = os.write(fd, data[offset:])
        except InterruptedError:
            continue
        if written <= 0:
            raise CapsuleError("short write while creating Capsule output")
        offset += written


def _open_output_regular(
    target: _OutputTarget,
    name: str | None = None,
) -> tuple[int, os.stat_result, bytes]:
    leaf = target.leaf if name is None else name
    try:
        descriptor = os.open(
            leaf,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=target.parent.fd,
        )
    except OSError as exc:
        raise CapsuleError(f"cannot securely open existing output: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise CapsuleError("existing output is not a regular file")
        data = packet_checker._read_stable_regular_fd(
            descriptor,
            "existing Capsule output",
            MAX_CAPSULE_BYTES,
        )
        metadata = os.fstat(descriptor)
        return descriptor, metadata, data
    except Exception:
        os.close(descriptor)
        raise


def _validate_capsule_artifact(data: bytes) -> None:
    _require_capsule_size(len(data), "existing Capsule output")
    header, packet_descriptor, embedded, sources, terminal, _ = _parse_capsule(data)
    version = header["version"]
    _validate_packet_descriptor(packet_descriptor, version)
    for descriptor, _ in sources:
        _validate_source_descriptor(descriptor, version)
    if version == CAPSULE_VERSION:
        try:
            embedded_text = embedded.decode("utf-8")
            original = _normalise_newlines(_reconstructed_packet(embedded_text, version))
            targets = packet_checker.parse_routes(original)
        except (UnicodeDecodeError, ValueError) as exc:
            raise CapsuleError("existing Capsule output has invalid terminal input") from exc
        if terminal != _terminal_contract(original, targets):
            raise CapsuleError("existing Capsule output has mismatched terminal lock")


def _same_named_inode(
    target: _OutputTarget,
    name: str,
    expected: tuple[int, int],
) -> bool:
    metadata = _output_lstat(target, name)
    return metadata is not None and _stat_identity(metadata) == expected


def _stat_revision(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return metadata that changes for a visible in-place file update."""

    return (
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _revision_without_ctime(
    revision: tuple[int, int, int, int, int],
) -> tuple[int, int, int, int]:
    """Fields an exchange must not change (rename legitimately changes ctime)."""

    mode, size, mtime_ns, _, link_count = revision
    return mode, size, mtime_ns, link_count


def _rebaseline_after_exchange(
    descriptor: int,
    expected_identity: tuple[int, int],
    expected_data: bytes,
    expected_revision: tuple[int, int, int, int, int],
    label: str,
    required_mode: int | None = None,
) -> tuple[int, int, int, int, int]:
    """Verify exchange-invariant state, then capture rename-updated ctime."""

    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or _stat_identity(metadata) != expected_identity
        or _revision_without_ctime(_stat_revision(metadata))
        != _revision_without_ctime(expected_revision)
        or (
            required_mode is not None
            and stat.S_IMODE(metadata.st_mode) != required_mode
        )
    ):
        raise CapsuleError(f"{label} changed during publication")
    try:
        current = packet_checker._read_stable_regular_fd(
            descriptor,
            label,
            MAX_CAPSULE_BYTES,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise CapsuleError(f"{label} changed during publication: {exc}") from exc
    metadata = os.fstat(descriptor)
    if (
        current != expected_data
        or _stat_identity(metadata) != expected_identity
        or _revision_without_ctime(_stat_revision(metadata))
        != _revision_without_ctime(expected_revision)
        or (
            required_mode is not None
            and stat.S_IMODE(metadata.st_mode) != required_mode
        )
    ):
        raise CapsuleError(f"{label} changed during publication")
    return _stat_revision(metadata)


def _verify_pinned_regular(
    descriptor: int,
    expected_identity: tuple[int, int],
    expected_data: bytes,
    expected_revision: tuple[int, int, int, int, int],
    label: str,
    required_mode: int | None = None,
) -> None:
    """Verify bytes and revision through the already-pinned regular-file fd."""

    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or _stat_identity(metadata) != expected_identity
        or _stat_revision(metadata) != expected_revision
        or (
            required_mode is not None
            and stat.S_IMODE(metadata.st_mode) != required_mode
        )
    ):
        raise CapsuleError(f"{label} changed during publication")
    try:
        data = packet_checker._read_stable_regular_fd(
            descriptor,
            label,
            MAX_CAPSULE_BYTES,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise CapsuleError(f"{label} changed during publication: {exc}") from exc
    metadata = os.fstat(descriptor)
    if (
        data != expected_data
        or not stat.S_ISREG(metadata.st_mode)
        or _stat_identity(metadata) != expected_identity
        or _stat_revision(metadata) != expected_revision
        or (
            required_mode is not None
            and stat.S_IMODE(metadata.st_mode) != required_mode
        )
    ):
        raise CapsuleError(f"{label} changed during publication")


def _verify_pinned_named_regular(
    target: _OutputTarget,
    name: str,
    descriptor: int,
    expected_identity: tuple[int, int],
    expected_data: bytes,
    expected_revision: tuple[int, int, int, int, int],
    label: str,
    required_mode: int | None = None,
) -> None:
    """Prove a pinned regular file is still the named publication entry."""

    _verify_pinned_regular(
        descriptor,
        expected_identity,
        expected_data,
        expected_revision,
        label,
        required_mode,
    )
    metadata = _output_lstat(target, name)
    if (
        metadata is None
        or not stat.S_ISREG(metadata.st_mode)
        or _stat_identity(metadata) != expected_identity
        or _stat_revision(metadata) != expected_revision
        or (
            required_mode is not None
            and stat.S_IMODE(metadata.st_mode) != required_mode
        )
    ):
        raise CapsuleError(f"{label} was substituted during publication")


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
        raise CapsuleError(f"cannot inspect {label}: {exc}") from exc


def _verify_pinned_private_regular(
    stage: _PrivateStage,
    descriptor: int,
    expected_identity: tuple[int, int],
    expected_data: bytes,
    expected_revision: tuple[int, int, int, int, int],
    label: str,
    required_mode: int | None = None,
) -> None:
    """Prove an entry inside the private staging directory is unchanged."""

    _verify_pinned_regular(
        descriptor,
        expected_identity,
        expected_data,
        expected_revision,
        label,
        required_mode,
    )
    metadata = _lstat_at(stage.fd, stage.entry, label)
    if (
        metadata is None
        or not stat.S_ISREG(metadata.st_mode)
        or _stat_identity(metadata) != expected_identity
        or _stat_revision(metadata) != expected_revision
        or (
            required_mode is not None
            and stat.S_IMODE(metadata.st_mode) != required_mode
        )
    ):
        raise CapsuleError(f"{label} was substituted during publication")


class _PrivateStage:
    """A random, mode-0700 directory that keeps force-exchange names private."""

    entry = "capsule"

    def __init__(
        self,
        name: str,
        descriptor: int,
        identity: tuple[int, int],
    ) -> None:
        self.name = name
        self.fd = descriptor
        self.identity = identity
        self._closed = False

    def validate(self, target: _OutputTarget) -> None:
        metadata = os.fstat(self.fd)
        named = _output_lstat(target, self.name)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or _stat_identity(metadata) != self.identity
            or named is None
            or not stat.S_ISDIR(named.st_mode)
            or stat.S_IMODE(named.st_mode) != 0o700
            or _stat_identity(named) != self.identity
        ):
            raise CapsuleError("private Capsule staging directory was substituted")

    def close(self) -> None:
        if not self._closed:
            os.close(self.fd)
            self._closed = True


def _create_private_stage(target: _OutputTarget) -> _PrivateStage:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
    for _ in range(16):
        name = f".{target.leaf}.{secrets.token_hex(16)}.capsule-stage"
        try:
            os.mkdir(name, 0o700, dir_fd=target.parent.fd)
        except FileExistsError:
            continue
        except OSError as exc:
            raise CapsuleError(f"cannot create private Capsule staging directory: {exc}") from exc
        descriptor = -1
        try:
            descriptor = os.open(name, flags, dir_fd=target.parent.fd)
            metadata = os.fstat(descriptor)
            identity = _stat_identity(metadata)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or not _same_named_inode(target, name, identity)
            ):
                raise CapsuleError("private Capsule staging directory was substituted")
            os.fchmod(descriptor, 0o700)
            stage = _PrivateStage(name, descriptor, identity)
            stage.validate(target)
            return stage
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise CapsuleError(
                f"cannot securely open private Capsule staging directory: {exc}"
            ) from exc
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            raise
    raise CapsuleError("cannot allocate private Capsule staging directory")


def _create_named_temporary(stage: _PrivateStage, data: bytes) -> tuple[
    int,
    tuple[int, int],
    tuple[int, int, int, int, int],
]:
    """Create the private named 0600 file used by both publication modes."""

    _require_capsule_size(len(data), "Capsule output")
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        descriptor = os.open(stage.entry, flags, 0o600, dir_fd=stage.fd)
    except OSError as exc:
        raise CapsuleError(f"cannot create private Capsule output: {exc}") from exc
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise CapsuleError("temporary Capsule inode is not private mode 0600")
        _write_all(descriptor, data)
        os.fsync(descriptor)
        if packet_checker._read_stable_regular_fd(
            descriptor,
            "temporary Capsule output",
            MAX_CAPSULE_BYTES,
        ) != data:
            raise CapsuleError("temporary Capsule bytes failed revalidation")
        metadata = os.fstat(descriptor)
        identity = _stat_identity(metadata)
        revision = _stat_revision(metadata)
        _verify_pinned_regular(
            descriptor,
            identity,
            data,
            revision,
            "temporary Capsule output",
            0o600,
        )
        os.fsync(stage.fd)
        return descriptor, identity, revision
    except Exception:
        os.close(descriptor)
        raise


def _discard_private_temporary(
    stage: _PrivateStage,
    temporary_fd: int,
    temporary_identity: tuple[int, int],
    temporary_data: bytes,
    temporary_revision: tuple[int, int, int, int, int],
) -> bool:
    """Remove only a still-private temporary entry; retain every other byte."""

    try:
        _verify_pinned_private_regular(
            stage,
            temporary_fd,
            temporary_identity,
            temporary_data,
            temporary_revision,
            "temporary Capsule staging entry",
            0o600,
        )
        os.unlink(stage.entry, dir_fd=stage.fd)
        os.fsync(stage.fd)
    except (CapsuleError, OSError):
        return False
    return True


def _remove_empty_private_stage(target: _OutputTarget, stage: _PrivateStage) -> None:
    """Best-effort cleanup; an uncertain staging directory is intentionally kept."""

    try:
        if _same_named_inode(target, stage.name, stage.identity):
            os.rmdir(stage.name, dir_fd=target.parent.fd)
    except (CapsuleError, OSError):
        pass


def _write_atomic(target: _OutputTarget, data: bytes, force: bool) -> None:
    _require_capsule_size(len(data), "Capsule output")
    library = _publication_libc()
    existing_fd: int | None = None
    stage: _PrivateStage | None = None
    temporary_fd: int | None = None
    preserve_stage = False
    temporary_identity: tuple[int, int] | None = None
    temporary_revision: tuple[int, int, int, int, int] | None = None
    try:
        _revalidate_output_against_inputs(target)
        stage = _create_private_stage(target)
        temporary_fd, temporary_identity, temporary_revision = (
            _create_named_temporary(stage, data)
        )
        stage.validate(target)

        existing = _revalidate_output_against_inputs(target)
        if not force:
            if existing is not None:
                raise CapsuleError(
                    f"refusing to overwrite existing output: {target.display}"
                )
            target.revalidate_parent()
            stage.validate(target)
            _verify_pinned_private_regular(
                stage,
                temporary_fd,
                temporary_identity,
                data,
                temporary_revision,
                "temporary Capsule staging entry",
                0o600,
            )
            _revalidate_output_against_inputs(target)
            try:
                _link_named_no_clobber(
                    stage.fd,
                    stage.entry,
                    target.parent.fd,
                    target.leaf,
                )
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    raise CapsuleError(
                        f"refusing to overwrite existing output: {target.display}"
                    ) from exc
                raise CapsuleError(f"cannot publish Capsule output: {exc}") from exc

            temporary_revision = _stat_revision(os.fstat(temporary_fd))
            _revalidate_output_against_inputs(target)
            target.revalidate_parent()
            _verify_pinned_named_regular(
                target,
                target.leaf,
                temporary_fd,
                temporary_identity,
                data,
                temporary_revision,
                "published Capsule output",
                0o600,
            )
            _verify_pinned_private_regular(
                stage,
                temporary_fd,
                temporary_identity,
                data,
                temporary_revision,
                "temporary Capsule staging entry",
                0o600,
            )
            os.unlink(stage.entry, dir_fd=stage.fd)
            os.fsync(stage.fd)
            temporary_revision = _stat_revision(os.fstat(temporary_fd))
            _remove_empty_private_stage(target, stage)
            os.fsync(target.parent.fd)
            target.revalidate_parent()
            _revalidate_output_against_inputs(target)
            _verify_pinned_named_regular(
                target,
                target.leaf,
                temporary_fd,
                temporary_identity,
                data,
                temporary_revision,
                "published Capsule output",
                0o600,
            )
            return

        if existing is None:
            raise CapsuleError("--force requires an existing supported Capsule artifact")
        if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
            raise CapsuleError(
                "--force target is not a regular supported Capsule artifact"
            )
        existing_fd, existing_stat, existing_data = _open_output_regular(target)
        old_identity = _stat_identity(existing_stat)
        old_revision = _stat_revision(existing_stat)
        _assert_output_not_input(target, existing_stat)
        _validate_capsule_artifact(existing_data)

        def compare_immediately_before_exchange() -> None:
            stage.validate(target)
            _verify_pinned_private_regular(
                stage,
                temporary_fd,
                temporary_identity,
                data,
                temporary_revision,
                "temporary Capsule staging entry",
                0o600,
            )
            current = _revalidate_output_against_inputs(target)
            if current is None:
                raise CapsuleError("existing Capsule output disappeared")
            _verify_pinned_named_regular(
                target,
                target.leaf,
                existing_fd,
                old_identity,
                existing_data,
                old_revision,
                "existing Capsule output",
            )
            target.revalidate_parent()
            current = _revalidate_output_against_inputs(target)
            if current is None:
                raise CapsuleError("existing Capsule output disappeared")
            _verify_pinned_named_regular(
                target,
                target.leaf,
                existing_fd,
                old_identity,
                existing_data,
                old_revision,
                "existing Capsule output",
            )

        try:
            _rename_exchange_at(
                library,
                stage.fd,
                stage.entry,
                target.parent.fd,
                target.leaf,
                compare_immediately_before_exchange,
            )
        except CapsuleError as exc:
            raise CapsuleError("force target changed before publication") from exc
        except OSError as exc:
            raise CapsuleError(f"cannot publish forced Capsule output: {exc}") from exc

        try:
            temporary_revision = _rebaseline_after_exchange(
                temporary_fd,
                temporary_identity,
                data,
                temporary_revision,
                "forced Capsule output",
                0o600,
            )
            old_revision = _rebaseline_after_exchange(
                existing_fd,
                old_identity,
                existing_data,
                old_revision,
                "previous Capsule output",
            )
            _revalidate_output_against_inputs(target)
            staged_metadata = _lstat_at(
                stage.fd,
                stage.entry,
                "previous Capsule output",
            )
            if staged_metadata is None:
                raise CapsuleError("previous Capsule output disappeared")
            _assert_identity_not_input(
                target,
                staged_metadata,
                "previous Capsule output",
            )
            _verify_pinned_named_regular(
                target,
                target.leaf,
                temporary_fd,
                temporary_identity,
                data,
                temporary_revision,
                "forced Capsule output",
                0o600,
            )
            _verify_pinned_private_regular(
                stage,
                existing_fd,
                old_identity,
                existing_data,
                old_revision,
                "previous Capsule output",
            )
            target.revalidate_parent()
            _revalidate_output_against_inputs(target)
            _verify_pinned_named_regular(
                target,
                target.leaf,
                temporary_fd,
                temporary_identity,
                data,
                temporary_revision,
                "forced Capsule output",
                0o600,
            )
            _verify_pinned_private_regular(
                stage,
                existing_fd,
                old_identity,
                existing_data,
                old_revision,
                "previous Capsule output",
            )
        except Exception as exc:
            # RENAME_EXCHANGE has no compare-and-exchange predicate. A writer
            # can act after the final userspace comparison and before this
            # syscall. Never exchange back after an uncertain publication: it
            # could overwrite a newer public entry. Keep the entry displaced
            # from the target in the private recovery directory instead.
            preserve_stage = True
            raise CapsuleError(
                "force publication became uncertain; retained exchanged target "
                "entry in "
                f"{stage.name}: {exc}"
            ) from exc

        try:
            os.unlink(stage.entry, dir_fd=stage.fd)
            os.fsync(stage.fd)
        except OSError as exc:
            preserve_stage = True
            raise CapsuleError(f"cannot finalize forced Capsule output: {exc}") from exc
        _remove_empty_private_stage(target, stage)
        os.fsync(target.parent.fd)
        target.revalidate_parent()
        _revalidate_output_against_inputs(target)
        _verify_pinned_named_regular(
            target,
            target.leaf,
            temporary_fd,
            temporary_identity,
            data,
            temporary_revision,
            "forced Capsule output",
            0o600,
        )
    finally:
        if existing_fd is not None:
            os.close(existing_fd)
        try:
            if (
                stage is not None
                and temporary_fd is not None
                and not preserve_stage
                and temporary_identity is not None
                and temporary_revision is not None
            ):
                _discard_private_temporary(
                    stage,
                    temporary_fd,
                    temporary_identity,
                    data,
                    temporary_revision,
                )
                _remove_empty_private_stage(target, stage)
        finally:
            if stage is not None:
                stage.close()
            if temporary_fd is not None:
                os.close(temporary_fd)


def _compact_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "check"):
        command = commands.add_parser(name)
        command.add_argument("repo", type=Path)
        command.add_argument("packet_or_capsule", type=Path)
        command.add_argument(
            "--encoding",
            default=os.environ.get("SEMANTIC_SPEC_TOKEN_ENCODING"),
            help="optional tiktoken encoding, for example o200k_base",
        )
        command.add_argument("--max-context-tokens", type=int)
    commands.choices["build"].add_argument("output", type=Path)
    commands.choices["build"].add_argument("--force", action="store_true")
    commands.choices["check"].add_argument(
        "--packet",
        type=Path,
        required=True,
        help="trusted original Packet v3 to bind requirements provenance",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        encoder = packet_checker.load_encoder(args.encoding)
        if args.max_context_tokens is not None and encoder is None:
            raise ValueError("--max-context-tokens requires --encoding")
        if args.max_context_tokens is not None and args.max_context_tokens < 1:
            raise ValueError("--max-context-tokens must be positive")
        if args.command == "build":
            capsule, metrics, input_references = _build_capsule(
                args.repo,
                args.packet_or_capsule,
                encoder=encoder,
                max_context_tokens=args.max_context_tokens,
            )
            output = None
            try:
                output = _check_output_path(args.output, input_references)
                _write_atomic(output, capsule, args.force)
                metrics["output"] = str(output.display)
                if args.max_context_tokens is not None:
                    metrics["max_context_tokens"] = args.max_context_tokens
                print(_compact_json(metrics))
                return 0
            finally:
                if output is not None:
                    output.close()
                _close_input_references(input_references)
        metrics = check_capsule(
            args.repo,
            args.packet_or_capsule,
            packet=args.packet,
            encoder=encoder,
            max_context_tokens=args.max_context_tokens,
        )
        if args.max_context_tokens is not None:
            metrics["max_context_tokens"] = args.max_context_tokens
        print(_compact_json(metrics))
        return 0 if metrics["valid"] else 1
    except CapsuleError as exc:
        print(
            _compact_json(
                {"errors": [str(exc)], "valid": False, "version": CAPSULE_VERSION}
            )
        )
        return 1
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
