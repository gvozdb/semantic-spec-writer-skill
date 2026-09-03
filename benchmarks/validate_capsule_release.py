#!/usr/bin/env python3
"""Isolated, fail-closed entrypoint for Capsule release validation."""

from __future__ import annotations

import sys


if not sys.flags.isolated:
    print(
        "Capsule release validation requires isolated mode; run: "
        "python3 -I benchmarks/validate_capsule_release.py",
        file=sys.stderr,
    )
    raise SystemExit(2)
if __file__ != "/dev/stdin":
    print(
        "Capsule release validation requires the attested Git-blob bootstrap; "
        "direct launcher execution is not trusted.",
        file=sys.stderr,
    )
    raise SystemExit(2)

# These imports are safe only after -I has removed the repository root and the
# script directory from sys.path.  No repository module is loaded before the
# attested Git tree and live worktree pass _preflight().
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import types
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path.cwd().absolute()
LAUNCHER_PATH = "benchmarks/validate_capsule_release.py"
LAUNCHER = ROOT / LAUNCHER_PATH
PUBLISHED = ROOT / "benchmarks" / "results" / "published"
ROOT_REPORT = "CAPSULE_BENCHMARK.md"
DOCUMENT_PATHS = frozenset({ROOT_REPORT, "README.md", "benchmarks/README.md"})
ARTIFACT_PREFIX = ("benchmarks", "results", "published")
ARTIFACT_FILES = frozenset({"capsule-r3.json", "CAPSULE.md"})
FIXTURE_PREFIX = "benchmarks/handoff-cases"
RUN_NAME = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
HEX_OBJECT = re.compile(r"[0-9a-f]{40,64}\Z")
HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
REQUIRED_CODE_PATHS = (
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
IMPORT_ROOTS = (
    ".",
    "benchmarks",
    "benchmarks/tests",
    "skills/semantic-spec-writer/scripts",
)
MAX_RESULT_BYTES = 256 * 1024 * 1024
MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_CODE_BYTES = 16 * 1024 * 1024
MAX_FIXTURE_BYTES = 128 * 1024 * 1024
GIT_PREFIX = (
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
RAW_HEADER = re.compile(
    rb":([0-7]{6}) ([0-7]{6}) ([0-9a-f]{40,64}) ([0-9a-f]{40,64}) "
    rb"([A-Z])(?:[0-9]{1,3})?\Z"
)


class ReleaseError(RuntimeError):
    """A release state that must fail closed."""


def _git_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["LC_ALL"] = "C"
    return environment


def _git(*arguments: str) -> bytes:
    try:
        completed = subprocess.run(
            [*GIT_PREFIX, *arguments],
            cwd=ROOT,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_environment(),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError("Git inspection failed") from exc
    if completed.returncode != 0:
        raise ReleaseError(f"Git inspection failed: {arguments[0]}")
    return completed.stdout


def _nul_records(output: bytes, label: str) -> list[bytes]:
    if output == b"":
        return []
    if not output.endswith(b"\0"):
        raise ReleaseError(f"unterminated Git {label} output")
    records = output.split(b"\0")
    if records[-1] != b"" or any(record == b"" for record in records[:-1]):
        raise ReleaseError(f"malformed Git {label} output")
    return records[:-1]


def _safe_git_path(raw: bytes | str) -> str:
    try:
        value = raw.decode("utf-8", "strict") if isinstance(raw, bytes) else raw
    except UnicodeDecodeError as exc:
        raise ReleaseError("Git path is not canonical UTF-8") from exc
    normalized = value.replace(os.sep, "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or path.as_posix() != normalized
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in normalized
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ReleaseError("unsafe repository path")
    return normalized


def _read_regular(relative: str, label: str, maximum: int) -> tuple[bytes, int]:
    safe = _safe_git_path(relative)
    parts = PurePosixPath(safe).parts
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        parent = os.open(ROOT, directory_flags)
    except OSError as exc:
        raise ReleaseError(f"cannot securely open the repository for {label}") from exc
    try:
        for part in parts[:-1]:
            try:
                following = os.open(part, directory_flags, dir_fd=parent)
            except OSError as exc:
                raise ReleaseError(
                    f"{label} has a non-directory or symlink parent"
                ) from exc
            os.close(parent)
            parent = following
        try:
            descriptor = os.open(parts[-1], file_flags, dir_fd=parent)
        except OSError as exc:
            raise ReleaseError(f"cannot open {label} as a regular file") from exc
        try:
            before = os.fstat(descriptor)
            named_before = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size > maximum
                or (before.st_dev, before.st_ino) != (
                    named_before.st_dev,
                    named_before.st_ino,
                )
            ):
                raise ReleaseError(f"{label} is not a bounded regular file")
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise ReleaseError(f"{label} changed while it was read")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1) != b"":
                raise ReleaseError(f"{label} grew while it was read")
            after = os.fstat(descriptor)
            named_after = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        before.st_nlink,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        after.st_nlink,
    )
    named_identity = (
        named_after.st_dev,
        named_after.st_ino,
        named_after.st_mode,
        named_after.st_size,
        named_after.st_mtime_ns,
        named_after.st_ctime_ns,
        named_after.st_nlink,
    )
    if identity_after != identity_before or named_identity != identity_after:
        raise ReleaseError(f"{label} changed while it was read")
    return b"".join(chunks), before.st_mode


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ReleaseError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _load_result(relative: str) -> tuple[dict[str, Any], bytes]:
    payload, mode = _read_regular(relative, "Capsule result", MAX_RESULT_BYTES)
    if stat.S_IMODE(mode) & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        raise ReleaseError("Capsule result must not be executable")
    try:
        document = json.loads(payload, object_pairs_hook=_json_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("Capsule result is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ReleaseError("Capsule result must be a JSON object")
    return document, payload


def _walk_capsule_artifacts() -> set[str]:
    try:
        published_metadata = os.lstat(PUBLISHED)
    except OSError as exc:
        raise ReleaseError("cannot inspect the published-results directory") from exc
    if not stat.S_ISDIR(published_metadata.st_mode):
        raise ReleaseError("published-results path must be a real directory")
    candidates: set[str] = set()
    stack: list[tuple[Path, tuple[str, ...]]] = [(PUBLISHED, ())]
    while stack:
        directory, prefix = stack.pop()
        try:
            with os.scandir(directory) as entries:
                children = list(entries)
        except OSError as exc:
            raise ReleaseError("cannot enumerate published Capsule artifacts") from exc
        for entry in children:
            parts = (*prefix, entry.name)
            try:
                is_symlink = entry.is_symlink()
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                raise ReleaseError("cannot inspect a published artifact") from exc
            if is_symlink:
                raise ReleaseError("published Capsule paths must not be symlinks")
            if entry.name in ARTIFACT_FILES:
                if (
                    len(parts) != 2
                    or RUN_NAME.fullmatch(parts[0]) is None
                    or is_directory
                ):
                    raise ReleaseError("Capsule artifact is outside one safe run directory")
                candidates.add(parts[0])
            if is_directory:
                stack.append((Path(entry.path), parts))
    return candidates


def _artifact_path(run_name: str, filename: str) -> str:
    return PurePosixPath(*ARTIFACT_PREFIX, run_name, filename).as_posix()


def _discover_pair(arguments: list[str]) -> tuple[str, str, str] | None:
    report_path = ROOT / ROOT_REPORT
    try:
        report_metadata = os.lstat(report_path)
        report_present = True
    except FileNotFoundError:
        report_present = False
        report_metadata = None
    except OSError as exc:
        raise ReleaseError("cannot inspect the root Capsule report") from exc
    candidates = _walk_capsule_artifacts()
    if not arguments and not report_present and not candidates:
        return None
    if len(arguments) not in {0, 2}:
        raise ReleaseError("expected either no arguments or RESULT REPORT")
    if not report_present or report_metadata is None or not stat.S_ISREG(report_metadata.st_mode):
        raise ReleaseError("CAPSULE_BENCHMARK.md must be one regular, non-symlink report")
    if stat.S_IMODE(report_metadata.st_mode) & (
        stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    ):
        raise ReleaseError("CAPSULE_BENCHMARK.md must not be executable")
    if len(candidates) != 1:
        raise ReleaseError("expected exactly one published Capsule result directory")
    run_name = next(iter(candidates))
    directory = PUBLISHED / run_name
    try:
        with os.scandir(directory) as entries:
            names = {entry.name for entry in entries}
    except OSError as exc:
        raise ReleaseError("cannot enumerate the Capsule result directory") from exc
    if names != set(ARTIFACT_FILES):
        raise ReleaseError("Capsule result directory must contain only capsule-r3.json and CAPSULE.md")
    result = _artifact_path(run_name, "capsule-r3.json")
    if arguments:
        if _safe_git_path(arguments[0]) != result or _safe_git_path(arguments[1]) != ROOT_REPORT:
            raise ReleaseError("release arguments do not identify the discovered Capsule pair")
    return run_name, result, ROOT_REPORT


def _reject_repository_import_shadows() -> None:
    """Reject repository entries that can shadow imports or execute bytecode.

    Stage 1 has no result attestation yet, so this check runs even when no
    Capsule artifacts exist.  CI can then execute repository Python without a
    committed ``json.py``/``argparse.py`` or sourceless-bytecode bypass.
    """

    stdlib_names = sys.stdlib_module_names
    stack = [ROOT]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                children = list(entries)
        except OSError as exc:
            raise ReleaseError("cannot inspect repository bytecode state") from exc
        for entry in children:
            if directory == ROOT and entry.name == ".git":
                continue
            if entry.name == "__pycache__" or entry.name.endswith((".pyc", ".pyo")):
                raise ReleaseError(
                    "repository bytecode is forbidden before validation: "
                    f"{Path(entry.path).relative_to(ROOT).as_posix()}"
                )
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
            except OSError as exc:
                raise ReleaseError("cannot inspect repository bytecode state") from exc

    for relative in IMPORT_ROOTS:
        directory = ROOT if relative == "." else ROOT / relative
        try:
            metadata = os.lstat(directory)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ReleaseError(f"import root is not a real directory: {relative}")
            with os.scandir(directory) as entries:
                children = list(entries)
        except OSError as exc:
            raise ReleaseError(f"cannot inspect import root: {relative}") from exc
        for entry in children:
            name = entry.name
            module_name = name.split(".", 1)[0]
            if module_name in stdlib_names:
                raise ReleaseError(
                    "repository path can shadow the standard library: "
                    f"{PurePosixPath(relative, name)}"
                )


def _verify_live_launcher_at_head() -> None:
    """Bind the live launcher path to the Git blob used by the bootstrap."""

    top = _git("rev-parse", "--show-toplevel").decode("utf-8", "strict").strip()
    if Path(top).resolve() != ROOT.resolve():
        raise ReleaseError("bootstrap is not running at the Git worktree root")
    entry = _tree_entry("HEAD", LAUNCHER_PATH)
    if entry is None or entry[0] not in {"100644", "100755"} or entry[1] != "blob":
        raise ReleaseError("HEAD does not contain a regular release launcher")
    blob = _git("cat-file", "blob", entry[2])
    live, live_mode = _read_regular(
        LAUNCHER_PATH,
        "live release launcher",
        MAX_CODE_BYTES,
    )
    executable = stat.S_IMODE(live_mode) & (
        stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    expected_executable = (
        stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        if entry[0] == "100755"
        else 0
    )
    if live != blob or executable != expected_executable:
        raise ReleaseError("live release launcher differs from its HEAD blob")


def _tree_entry(revision: str, path: str) -> tuple[str, str, str] | None:
    output = _git("ls-tree", "-z", "--full-tree", revision, "--", path)
    if output == b"":
        return None
    records = _nul_records(output, "tree")
    if len(records) != 1:
        raise ReleaseError("Git tree path is not exact")
    try:
        header, raw_path = records[0].split(b"\t", 1)
        mode, object_type, object_id = header.decode("ascii", "strict").split(" ")
    except (UnicodeError, ValueError) as exc:
        raise ReleaseError("malformed Git tree entry") from exc
    if (
        _safe_git_path(raw_path) != path
        or re.fullmatch(r"[0-7]{6}", mode) is None
        or re.fullmatch(r"[a-z]+", object_type) is None
        or HEX_OBJECT.fullmatch(object_id) is None
    ):
        raise ReleaseError("non-canonical Git tree entry")
    return mode, object_type, object_id


def _parse_raw_diff(output: bytes) -> list[tuple[str, str, str, tuple[str, ...]]]:
    records = _nul_records(output, "raw diff")
    changes: list[tuple[str, str, str, tuple[str, ...]]] = []
    index = 0
    while index < len(records):
        match = RAW_HEADER.fullmatch(records[index])
        if match is None:
            raise ReleaseError("malformed Git raw diff")
        index += 1
        status_code = match.group(5).decode("ascii")
        count = 2 if status_code in {"R", "C"} else 1
        if index + count > len(records):
            raise ReleaseError("Git raw diff has missing paths")
        paths = tuple(_safe_git_path(path) for path in records[index : index + count])
        index += count
        changes.append(
            (
                match.group(1).decode("ascii"),
                match.group(2).decode("ascii"),
                status_code,
                paths,
            )
        )
    return changes


def _parse_status(output: bytes) -> list[tuple[str, str | None, tuple[str, ...], tuple[str, ...]]]:
    records = _nul_records(output, "status")
    changes: list[tuple[str, str | None, tuple[str, ...], tuple[str, ...]]] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if record.startswith((b"? ", b"! ")):
            changes.append(
                (
                    "untracked" if record[:1] == b"?" else "ignored",
                    None,
                    (),
                    (_safe_git_path(record[2:]),),
                )
            )
            continue
        if record.startswith(b"1 "):
            fields = record.split(b" ", 8)
            if len(fields) != 9:
                raise ReleaseError("malformed ordinary Git status")
            try:
                xy = fields[1].decode("ascii", "strict")
                submodule = fields[2].decode("ascii", "strict")
                modes = tuple(field.decode("ascii", "strict") for field in fields[3:6])
            except UnicodeError as exc:
                raise ReleaseError("non-ASCII Git status metadata") from exc
            if submodule != "N...":
                raise ReleaseError("submodule worktree state is not allowed")
            changes.append(("ordinary", xy, modes, (_safe_git_path(fields[8]),)))
            continue
        if record.startswith(b"2 "):
            fields = record.split(b" ", 9)
            if len(fields) != 10 or index >= len(records):
                raise ReleaseError("malformed rename/copy Git status")
            source = records[index]
            index += 1
            changes.append(
                (
                    "rename_or_copy",
                    None,
                    (),
                    (_safe_git_path(source), _safe_git_path(fields[9])),
                )
            )
            continue
        if record.startswith(b"u "):
            fields = record.split(b" ", 10)
            if len(fields) != 11:
                raise ReleaseError("malformed unmerged Git status")
            changes.append(
                ("unmerged", None, (), (_safe_git_path(fields[10]),))
            )
            continue
        raise ReleaseError("unknown Git porcelain-v2 record")
    return changes


def _status_bytes() -> bytes:
    return _git(
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=all",
        "--ignored=matching",
        "--ignore-submodules=none",
        "--renames",
    )


def _index_is_observable() -> bool:
    for record in _nul_records(_git("ls-files", "-v", "-z"), "index"):
        if len(record) < 3 or record[:2] != b"H ":
            return False
        _safe_git_path(record[2:])
    return True


def _allowed_paths(run_name: str) -> tuple[set[str], set[str]]:
    artifacts = {
        ROOT_REPORT,
        _artifact_path(run_name, "capsule-r3.json"),
        _artifact_path(run_name, "CAPSULE.md"),
    }
    return set(DOCUMENT_PATHS) | artifacts, artifacts


def _validate_current_raw(output: bytes, allowed: set[str]) -> None:
    for old_mode, new_mode, status_code, paths in _parse_raw_diff(output):
        if (
            len(paths) != 1
            or paths[0] not in allowed
            or status_code not in {"A", "M"}
            or new_mode != "100644"
        ):
            raise ReleaseError(
                "index/worktree change has a forbidden path, copy, rename, type, or mode"
            )
        path = paths[0]
        if path in {"README.md", "benchmarks/README.md"}:
            valid = status_code == "M" and old_mode == "100644"
        else:
            valid = (
                status_code == "A" and old_mode == "000000"
            ) or (
                status_code == "M" and old_mode == "100644"
            )
        if not valid:
            raise ReleaseError("index/worktree release status is not allowed")


def _validate_changes(commit: str, head: str, run_name: str) -> None:
    allowed, artifacts = _allowed_paths(run_name)
    changed: set[str] = set()
    raw = _git(
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
    )
    for old_mode, new_mode, status_code, paths in _parse_raw_diff(raw):
        if len(paths) != 1 or paths[0] not in allowed:
            raise ReleaseError("descendant commit changes a path outside the release allowlist")
        path = paths[0]
        if path in {"README.md", "benchmarks/README.md"}:
            valid = status_code == "M" and old_mode == new_mode == "100644"
        else:
            valid = status_code == "A" and old_mode == "000000" and new_mode == "100644"
        if not valid:
            raise ReleaseError("descendant release change has a forbidden status, type, or mode")
        changed.add(path)

    status_before = _status_bytes()
    for kind, xy, modes, paths in _parse_status(status_before):
        if kind not in {"ordinary", "untracked"} or len(paths) != 1 or paths[0] not in allowed:
            raise ReleaseError("worktree contains a path outside the release allowlist")
        path = paths[0]
        if kind == "untracked":
            if path in {"README.md", "benchmarks/README.md"}:
                raise ReleaseError("README claims must modify tracked files")
        else:
            allowed_statuses = {".M", "M.", "MM"}
            if path not in {"README.md", "benchmarks/README.md"}:
                allowed_statuses |= {"A.", "AM"}
            if (
                xy not in allowed_statuses
                or any(mode not in {"000000", "100644"} for mode in modes)
                or all(mode == "000000" for mode in modes)
            ):
                raise ReleaseError("worktree release change has a forbidden status, type, or mode")
        changed.add(path)

    if not _index_is_observable():
        raise ReleaseError("Git index flags can hide worktree changes")
    raw_options = (
        "--raw",
        "-z",
        "--no-abbrev",
        "--no-ext-diff",
        "--find-renames",
        "--find-copies",
        "--find-copies-harder",
        "--ignore-submodules=none",
    )
    _validate_current_raw(
        _git("diff", "--cached", *raw_options, head, "--"),
        allowed,
    )
    _validate_current_raw(_git("diff", *raw_options, "--"), allowed)
    if not artifacts <= changed:
        raise ReleaseError("release artifacts must be created after the attested run commit")
    for artifact in artifacts:
        if _tree_entry(commit, artifact) is not None:
            raise ReleaseError("release artifact already existed in the attested run commit")
    for path in changed:
        _, mode = _read_regular(path, f"release path {path}", MAX_RESULT_BYTES)
        if stat.S_IMODE(mode) & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            raise ReleaseError("release paths must be regular and non-executable")
    if _status_bytes() != status_before or not _index_is_observable():
        raise ReleaseError("Git worktree changed during release preflight")


def _verify_required_code(
    attestation: dict[str, Any],
    commit: str,
    head: str,
) -> dict[str, bytes]:
    """Verify required code and return immutable bytes from the attested commit."""

    recorded_paths = attestation.get("required_paths")
    if not isinstance(recorded_paths, dict) or set(recorded_paths) != set(REQUIRED_CODE_PATHS):
        raise ReleaseError("code attestation does not contain the exact required path set")
    blobs: dict[str, bytes] = {}
    for path in REQUIRED_CODE_PATHS:
        recorded = recorded_paths[path]
        if (
            not isinstance(recorded, dict)
            or set(recorded) != {"git_mode", "git_type", "git_object", "sha256"}
            or recorded.get("git_mode") not in {"100644", "100755"}
            or recorded.get("git_type") != "blob"
            or not isinstance(recorded.get("git_object"), str)
            or HEX_OBJECT.fullmatch(recorded["git_object"]) is None
            or not isinstance(recorded.get("sha256"), str)
            or HEX_SHA256.fullmatch(recorded["sha256"]) is None
        ):
            raise ReleaseError(f"invalid code attestation for {path}")
        expected = (
            recorded["git_mode"],
            recorded["git_type"],
            recorded["git_object"],
        )
        if _tree_entry(commit, path) != expected or _tree_entry(head, path) != expected:
            raise ReleaseError(f"attested code tree entry changed: {path}")
        try:
            blob_size = int(
                _git("cat-file", "-s", recorded["git_object"]).decode(
                    "ascii", "strict"
                )
            )
        except (UnicodeError, ValueError) as exc:
            raise ReleaseError(f"attested code blob size is invalid: {path}") from exc
        if blob_size < 0 or blob_size > MAX_CODE_BYTES:
            raise ReleaseError(f"attested code blob is oversized: {path}")
        blob = _git("cat-file", "blob", recorded["git_object"])
        if (
            len(blob) != blob_size
            or hashlib.sha256(blob).hexdigest() != recorded["sha256"]
        ):
            raise ReleaseError(f"attested code blob is invalid: {path}")
        live, live_mode = _read_regular(path, f"attested code path {path}", MAX_CODE_BYTES)
        executable = stat.S_IMODE(live_mode) & (
            stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        expected_executable = (
            stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            if recorded["git_mode"] == "100755"
            else 0
        )
        if live != blob or executable != expected_executable:
            raise ReleaseError(f"attested code worktree file changed: {path}")
        blobs[path] = blob
    return blobs


def _verified_fixture_blobs(revision: str) -> dict[str, bytes]:
    """Capture the benchmark fixtures exclusively from one attested Git tree."""

    records = _nul_records(
        _git(
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            revision,
            "--",
            FIXTURE_PREFIX,
        ),
        "fixture tree",
    )
    blobs: dict[str, bytes] = {}
    total = 0
    prefix = f"{FIXTURE_PREFIX}/"
    for record in records:
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, object_type, object_id = header.decode("ascii", "strict").split(" ")
        except (UnicodeError, ValueError) as exc:
            raise ReleaseError("malformed attested fixture tree") from exc
        path = _safe_git_path(raw_path)
        if (
            mode != "100644"
            or object_type != "blob"
            or HEX_OBJECT.fullmatch(object_id) is None
            or not path.startswith(prefix)
            or path in blobs
        ):
            raise ReleaseError("attested fixture tree contains an unsafe entry")
        try:
            size = int(
                _git("cat-file", "-s", object_id).decode("ascii", "strict")
            )
        except (UnicodeError, ValueError) as exc:
            raise ReleaseError("attested fixture blob size is invalid") from exc
        if size < 0 or size > MAX_FIXTURE_BYTES - total:
            raise ReleaseError("attested fixture tree exceeds its byte limit")
        payload = _git("cat-file", "blob", object_id)
        if len(payload) != size:
            raise ReleaseError("attested fixture blob is invalid")
        blobs[path] = payload
        total += size
    if not blobs:
        raise ReleaseError("attested benchmark fixture tree is empty")
    return blobs


def _preflight(
    document: dict[str, Any],
    run_name: str,
) -> tuple[dict[str, bytes], dict[str, bytes]]:
    if not stat.S_ISREG(os.lstat(LAUNCHER).st_mode):
        raise ReleaseError("release launcher must be a regular, non-symlink file")
    top = _git("rev-parse", "--show-toplevel").decode("utf-8", "strict").strip()
    if Path(top).resolve() != ROOT.resolve():
        raise ReleaseError("launcher is not running in its Git worktree")
    attestation = document.get("code_revision")
    environment = document.get("environment")
    if (
        not isinstance(attestation, dict)
        or set(attestation) != {"commit", "worktree_clean_at_start", "required_paths"}
        or attestation.get("worktree_clean_at_start") is not True
        or not isinstance(attestation.get("commit"), str)
        or HEX_OBJECT.fullmatch(attestation["commit"]) is None
        or not isinstance(environment, dict)
        or environment.get("git_commit") != attestation["commit"]
    ):
        raise ReleaseError("Capsule result has no valid clean-run code attestation")
    commit = attestation["commit"]
    _git("cat-file", "-e", f"{commit}^{{commit}}")
    head = _git("rev-parse", "--verify", "HEAD^{commit}").decode("ascii", "strict").strip()
    if HEX_OBJECT.fullmatch(head) is None:
        raise ReleaseError("Git HEAD is not a full commit id")
    _git("merge-base", "--is-ancestor", commit, head)
    _validate_changes(commit, head, run_name)
    _verify_required_code(attestation, commit, head)
    fixture_blobs = _verified_fixture_blobs(commit)
    if _git("rev-parse", "--verify", "HEAD^{commit}").decode("ascii", "strict").strip() != head:
        raise ReleaseError("Git HEAD changed during release preflight")
    _validate_changes(commit, head, run_name)
    return _verify_required_code(attestation, commit, head), fixture_blobs


def _load_attested_module(
    name: str,
    relative: str,
    code_blobs: dict[str, bytes],
) -> Any:
    """Execute exact attested bytes without reopening a live repository path."""

    source = code_blobs.get(relative)
    if source is None:
        raise ReleaseError(f"missing attested module bytes: {relative}")
    filename = str(ROOT / relative)
    module = types.ModuleType(name)
    module.__file__ = filename
    module.__package__ = name.rpartition(".")[0]
    module.__loader__ = None
    module.__spec__ = None
    sys.modules[name] = module
    try:
        exec(compile(source, filename, "exec", dont_inherit=True), module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _write_private_file(path: Path, payload: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise ReleaseError("cannot materialize attested helper")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _materialize_fixture_blobs(
    directory: Path,
    fixture_blobs: dict[str, bytes],
) -> Path:
    cases = directory / "handoff-cases"
    cases.mkdir(mode=0o700)
    prefix = PurePosixPath(FIXTURE_PREFIX)
    for path, payload in sorted(fixture_blobs.items()):
        try:
            relative = PurePosixPath(path).relative_to(prefix)
        except ValueError as exc:  # pragma: no cover - preflight invariant
            raise ReleaseError("attested fixture path escaped its prefix") from exc
        target = cases.joinpath(*relative.parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_private_file(target, payload)
    return cases


def _materialize_code_subtree(
    directory: Path,
    prefix: str,
    code_blobs: dict[str, bytes],
) -> Path:
    target_root = directory / PurePosixPath(prefix).name
    target_root.mkdir(mode=0o700)
    source_prefix = PurePosixPath(prefix)
    found = False
    for path, payload in sorted(code_blobs.items()):
        source = PurePosixPath(path)
        try:
            relative = source.relative_to(source_prefix)
        except ValueError:
            continue
        target = target_root.joinpath(*relative.parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_private_file(target, payload)
        found = True
    if not found:
        raise ReleaseError(f"attested code subtree is empty: {prefix}")
    return target_root


def _remove_repository_import_paths() -> None:
    clean: list[str] = []
    root = ROOT.resolve()
    for entry in sys.path:
        try:
            resolved = Path(entry).resolve()
        except (OSError, RuntimeError):
            continue
        if resolved != root and root not in resolved.parents:
            clean.append(entry)
    sys.path[:] = clean


def main() -> int:
    _reject_repository_import_shadows()
    _verify_live_launcher_at_head()
    pair = _discover_pair(sys.argv[1:])
    if pair is None:
        print("No current Capsule release artifacts; stage-1 validation passed.")
        return 0
    run_name, result_path, report_path = pair
    document, result_payload = _load_result(result_path)
    report, report_mode = _read_regular(report_path, "root Capsule report", MAX_REPORT_BYTES)
    nested_report, nested_mode = _read_regular(
        _artifact_path(run_name, "CAPSULE.md"),
        "published Capsule report",
        MAX_REPORT_BYTES,
    )
    executable = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    if stat.S_IMODE(report_mode) & executable or stat.S_IMODE(nested_mode) & executable:
        raise ReleaseError("Capsule reports must not be executable")
    if nested_report != report:
        raise ReleaseError("published CAPSULE.md must exactly match CAPSULE_BENCHMARK.md")
    code_blobs, fixture_blobs = _preflight(document, run_name)

    # Execute only commit-addressed bytes.  Live paths remain useful filenames
    # for diagnostics, but are never reopened as Python source after preflight.
    sys.dont_write_bytecode = True
    _remove_repository_import_paths()
    _load_attested_module(
        "_semantic_spec_packet_checker",
        "skills/semantic-spec-writer/scripts/check_execution_packet.py",
        code_blobs,
    )
    _load_attested_module(
        "_semantic_spec_context_capsule",
        "skills/semantic-spec-writer/scripts/context_capsule.py",
        code_blobs,
    )
    benchmark = _load_attested_module(
        "benchmark",
        "benchmarks/benchmark.py",
        code_blobs,
    )
    lifecycle = _load_attested_module(
        "lifecycle",
        "benchmarks/lifecycle.py",
        code_blobs,
    )
    handoff = _load_attested_module(
        "_semantic_spec_capsule_release_handoff",
        "benchmarks/handoff.py",
        code_blobs,
    )
    with tempfile.TemporaryDirectory(prefix="capsule-release-check-") as directory:
        private_root = Path(directory)
        checker = private_root / "check_execution_packet.py"
        _write_private_file(
            checker,
            code_blobs[
                "skills/semantic-spec-writer/scripts/check_execution_packet.py"
            ],
        )
        benchmark.EXECUTION_PACKET_CHECK = checker
        private_skill = _materialize_code_subtree(
            private_root,
            "skills/semantic-spec-writer",
            code_blobs,
        )
        lifecycle.SKILL_DIR = private_skill
        lifecycle.SKILL = private_skill / "SKILL.md"
        handoff.SKILL_DIR = private_skill
        private_protocol = private_root / "capsule-lifecycle-v1.prereg.json"
        _write_private_file(
            private_protocol,
            code_blobs["benchmarks/capsule-lifecycle-v1.prereg.json"],
        )
        handoff.LIFECYCLE_PROTOCOL_PATH = private_protocol
        handoff.CASES_DIR = _materialize_fixture_blobs(
            private_root,
            fixture_blobs,
        )

        expected_attestation = document["code_revision"]
        expected_commit = document["environment"]["git_commit"]

        def preflighted_attestation_is_valid(
            value: Any,
            required_paths: Any,
            *,
            environment_commit: Any,
        ) -> bool:
            try:
                paths = tuple(sorted(str(path) for path in required_paths))
            except (TypeError, ValueError):
                return False
            return (
                value == expected_attestation
                and paths == tuple(sorted(REQUIRED_CODE_PATHS))
                and environment_commit == expected_commit
            )

        benchmark.git_revision_attestation_is_valid = (
            preflighted_attestation_is_valid
        )
        _remove_repository_import_paths()
        errors = handoff.validate_capsule_release(document, report)
        if errors:
            raise ReleaseError("\n".join(errors))
    _preflight(document, run_name)
    final_document, final_result_payload = _load_result(result_path)
    final_report, _ = _read_regular(
        report_path,
        "root Capsule report",
        MAX_REPORT_BYTES,
    )
    final_nested_report, _ = _read_regular(
        _artifact_path(run_name, "CAPSULE.md"),
        "published Capsule report",
        MAX_REPORT_BYTES,
    )
    if (
        final_document != document
        or final_result_payload != result_payload
        or final_report != report
        or final_nested_report != nested_report
    ):
        raise ReleaseError("release artifacts changed during validation")
    print("validated current Capsule release artifacts")
    return 0


if __name__ == "__main__":
    try:
        exit_status = main()
    except ReleaseError as exc:
        print(f"Capsule release validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except BaseException as exc:
        print(
            f"Capsule release validation failed closed: {type(exc).__name__}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    raise SystemExit(exit_status)
