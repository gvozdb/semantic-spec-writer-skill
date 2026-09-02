#!/usr/bin/env python3
"""Validate a repository-grounded semantic execution packet."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ROUTE_LINE = re.compile(r"^  (read|edit|create):\s*(.+?)\s*$")
RANGE = re.compile(r"^(.*?):(\d+)-(\d+)$")
BASIS_LINE = re.compile(r"^basis:\s*route-sha256:([0-9a-f]{64})\s*$")
VERIFY_LINE = re.compile(r"^  V\d+:\s*`([^`]+)`\s*$")
VERIFY_ENTRY_LINE = re.compile(r"^  (V\d+):\s*`([^`]+)`\s*$")
ACTION_LINE = re.compile(r"^    do:\s*(.+?)\s*$")
EXPAND_LINE = re.compile(r"^  expand:\s*(.+?)\s*$")
EXECUTION_LINE = re.compile(r"^execution:\s*(.+?)\s*$")
BOUNDED_EXECUTION = (
    "routed read once -> all do -> V1 once -> stop on pass; "
    "expand only on contradiction/failure"
)
MAX_REGULAR_INPUT_BYTES = 64 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024


def _validate_max_bytes(max_bytes: int) -> None:
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("maximum regular input size must be a non-negative integer")


@dataclass(frozen=True)
class Target:
    kind: str
    raw: str
    relative_path: str
    start: int | None
    end: int | None
    anchor: str | None


def _require_secure_posix() -> None:
    """Fail closed unless descriptor-relative, no-follow I/O is available."""

    required_flags = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    missing = [name for name in required_flags if not hasattr(os, name)]
    if os.name != "posix" or os.open not in os.supports_dir_fd or missing:
        detail = ", ".join(missing) if missing else "openat/dirfd support"
        raise RuntimeError(f"secure POSIX filesystem primitives unavailable: {detail}")
    if os.stat not in os.supports_dir_fd:
        raise RuntimeError(
            "secure POSIX filesystem primitives unavailable: fstatat support"
        )


def _directory_flags() -> int:
    _require_secure_posix()
    return os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW


def _regular_flags() -> int:
    _require_secure_posix()
    return os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _changed_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        _identity(left) != _identity(right)
        or left.st_mode != right.st_mode
        or left.st_size != right.st_size
        or left.st_mtime_ns != right.st_mtime_ns
        or left.st_ctime_ns != right.st_ctime_ns
        or left.st_nlink != right.st_nlink
    )


def _changed_directory(left: os.stat_result, right: os.stat_result) -> bool:
    return _identity(left) != _identity(right)


def _secure_path_error(label: str, path: Path, exc: OSError) -> ValueError:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        return ValueError(
            f"{label} contains a symlink or non-directory component: {path}"
        )
    return ValueError(f"cannot securely open {label} {path}: {exc}")


@dataclass(frozen=True)
class DirectoryEdge:
    """A pinned parent descriptor and the child name reached through it."""

    parent_fd: int
    parent_stat: os.stat_result
    parent_path: Path
    child_name: str
    child_stat: os.stat_result


@dataclass
class SecureDirectory:
    """A directory pinned by an O_NOFOLLOW descriptor."""

    fd: int
    path: Path
    stat_result: os.stat_result
    ancestors: list[DirectoryEdge] = field(default_factory=list)
    _closed: bool = False

    def validate_identity(self, label: str) -> None:
        """Prove every pinned descriptor is still reached by its original name."""

        for edge in self.ancestors:
            current = os.fstat(edge.parent_fd)
            if (
                not stat.S_ISDIR(current.st_mode)
                or _identity(current) != _identity(edge.parent_stat)
            ):
                raise ValueError(
                    f"{label} directory identity changed after it was pinned: "
                    f"{edge.parent_path}"
                )
            try:
                named_child = os.stat(
                    edge.child_name,
                    dir_fd=edge.parent_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise ValueError(
                    f"{label} named ancestry changed after it was pinned: "
                    f"{edge.parent_path / edge.child_name}"
                ) from exc
            if (
                not stat.S_ISDIR(named_child.st_mode)
                or _identity(named_child) != _identity(edge.child_stat)
            ):
                raise ValueError(
                    f"{label} named ancestry changed after it was pinned: "
                    f"{edge.parent_path / edge.child_name}"
                )

        current = os.fstat(self.fd)
        if (
            not stat.S_ISDIR(current.st_mode)
            or _identity(current) != _identity(self.stat_result)
        ):
            raise ValueError(
                f"{label} directory identity changed after it was pinned: {self.path}"
            )

    def validate_stable(self, label: str) -> None:
        self.validate_identity(label)
        for edge in self.ancestors:
            if _changed_directory(edge.parent_stat, os.fstat(edge.parent_fd)):
                raise ValueError(
                    f"{label} directory changed after it was pinned: "
                    f"{edge.parent_path}"
                )
        if _changed_directory(self.stat_result, os.fstat(self.fd)):
            raise ValueError(
                f"{label} directory changed after it was pinned: {self.path}"
            )

    def clone(self, label: str = "directory") -> SecureDirectory:
        """Duplicate this pinned directory without losing named-edge baselines."""

        self.validate_stable(label)
        descriptor = -1
        cloned_edges: list[DirectoryEdge] = []
        try:
            for edge in self.ancestors:
                cloned_edges.append(
                    DirectoryEdge(
                        os.dup(edge.parent_fd),
                        edge.parent_stat,
                        edge.parent_path,
                        edge.child_name,
                        edge.child_stat,
                    )
                )
            descriptor = os.dup(self.fd)
            cloned = SecureDirectory(
                descriptor,
                self.path,
                self.stat_result,
                cloned_edges,
            )
            cloned.validate_stable(label)
            self.validate_stable(label)
            return cloned
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            for edge in cloned_edges:
                os.close(edge.parent_fd)
            raise

    def close(self) -> None:
        if not self._closed:
            os.close(self.fd)
            for edge in self.ancestors:
                os.close(edge.parent_fd)
            self._closed = True

    def __enter__(self) -> SecureDirectory:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def secure_open_directory(path: Path | str, label: str = "directory") -> SecureDirectory:
    """Open and pin every named directory edge with openat(2)/O_NOFOLLOW."""

    _require_secure_posix()
    candidate = Path(path)
    parts = list(candidate.parts)
    if candidate.is_absolute():
        anchor = candidate.anchor or os.sep
        try:
            current = os.open(anchor, _directory_flags())
        except OSError as exc:
            raise _secure_path_error(label, candidate, exc) from exc
        if parts and parts[0] == candidate.anchor:
            parts = parts[1:]
    else:
        try:
            current = os.open(".", _directory_flags())
        except OSError as exc:
            raise _secure_path_error(label, candidate, exc) from exc

    ancestors: list[DirectoryEdge] = []
    traversed = Path(candidate.anchor) if candidate.is_absolute() else Path(".")
    try:
        for part in parts:
            if part in {"", "."}:
                continue
            parent_stat = os.fstat(current)
            following = -1
            try:
                following = os.open(part, _directory_flags(), dir_fd=current)
            except OSError as exc:
                raise _secure_path_error(label, candidate, exc) from exc
            try:
                child_stat = os.fstat(following)
                named_child = os.stat(
                    part,
                    dir_fd=current,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(child_stat.st_mode)
                    or not stat.S_ISDIR(named_child.st_mode)
                    or _changed_directory(parent_stat, os.fstat(current))
                    or _identity(named_child) != _identity(child_stat)
                ):
                    raise ValueError(
                        f"{label} path changed while it was being pinned: "
                        f"{traversed / part}"
                    )
            except Exception:
                os.close(following)
                raise
            ancestors.append(
                DirectoryEdge(
                    current,
                    parent_stat,
                    traversed,
                    part,
                    child_stat,
                )
            )
            current = following
            traversed = traversed / part
        metadata = os.fstat(current)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"{label} is not a directory: {candidate}")
        opened = SecureDirectory(current, candidate, metadata, ancestors)
        opened.validate_stable(label)
        return opened
    except Exception:
        os.close(current)
        for edge in ancestors:
            os.close(edge.parent_fd)
        raise


def _read_stable_regular_fd(
    fd: int,
    label: str,
    max_bytes: int = MAX_REGULAR_INPUT_BYTES,
) -> bytes:
    """Read a stable regular file, rejecting inputs above the memory bound."""

    _validate_max_bytes(max_bytes)
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label} is not a regular file")
    if before.st_size > max_bytes:
        raise ValueError(
            f"{label} exceeds maximum regular input size of {max_bytes} bytes"
        )
    os.lseek(fd, 0, os.SEEK_SET)
    data = bytearray()
    while True:
        request_size = min(_READ_CHUNK_BYTES, max_bytes - len(data) + 1)
        try:
            chunk = os.read(fd, request_size)
        except InterruptedError:
            continue
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > max_bytes:
            raise ValueError(
                f"{label} exceeds maximum regular input size of {max_bytes} bytes"
            )
    after = os.fstat(fd)
    if not stat.S_ISREG(after.st_mode) or _changed_stat(before, after):
        raise ValueError(f"{label} changed while being read")
    if len(data) != after.st_size:
        raise ValueError(f"{label} changed while being read")
    return bytes(data)


def _read_regular_with_limit(fd: int, label: str, max_bytes: int) -> bytes:
    """Preserve the ordinary reader call shape unless a custom cap is requested."""

    _validate_max_bytes(max_bytes)
    if max_bytes == MAX_REGULAR_INPUT_BYTES:
        return _read_stable_regular_fd(fd, label)
    return _read_stable_regular_fd(fd, label, max_bytes)


@dataclass
class SecureFile:
    """A regular file and its parent directory, both pinned by descriptors."""

    fd: int
    parent: SecureDirectory
    leaf: str
    path: Path
    stat_result: os.stat_result
    data: bytes
    max_bytes: int = MAX_REGULAR_INPUT_BYTES
    _closed: bool = False

    @property
    def identity(self) -> tuple[int, int]:
        return _identity(self.stat_result)

    @property
    def parent_identity(self) -> tuple[int, int]:
        return _identity(self.parent.stat_result)

    def revalidate(self, label: str) -> None:
        self.parent.validate_stable(f"{label} parent")
        pinned_stat = os.fstat(self.fd)
        if _changed_stat(self.stat_result, pinned_stat):
            raise ValueError(f"{label} changed after its initial read")
        if _read_regular_with_limit(self.fd, label, self.max_bytes) != self.data:
            raise ValueError(f"{label} changed after its initial read")
        if _changed_stat(self.stat_result, os.fstat(self.fd)):
            raise ValueError(f"{label} changed after its initial read")
        try:
            reopened = os.open(
                self.leaf,
                _regular_flags(),
                dir_fd=self.parent.fd,
            )
        except OSError as exc:
            raise _secure_path_error(label, self.path, exc) from exc
        try:
            current_stat = os.fstat(reopened)
            if not stat.S_ISREG(current_stat.st_mode):
                raise ValueError(f"{label} is not a regular file: {self.path}")
            current = _read_regular_with_limit(reopened, label, self.max_bytes)
            if (
                _changed_stat(self.stat_result, current_stat)
                or current != self.data
                or _changed_stat(self.stat_result, os.fstat(reopened))
            ):
                raise ValueError(f"{label} changed after its initial read")
        finally:
            os.close(reopened)
        self.parent.validate_stable(f"{label} parent")

    def close(self) -> None:
        if not self._closed:
            os.close(self.fd)
            self.parent.close()
            self._closed = True

    def __enter__(self) -> SecureFile:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _split_file_path(path: Path | str, label: str) -> tuple[Path, str, Path]:
    candidate = Path(path)
    leaf = candidate.name
    if leaf in {"", ".", ".."}:
        raise ValueError(f"{label} must name a file: {candidate}")
    return candidate.parent, leaf, candidate


def secure_open_regular(
    path: Path | str,
    label: str = "file",
    *,
    max_bytes: int = MAX_REGULAR_INPUT_BYTES,
) -> SecureFile:
    """Securely open and snapshot a regular file without resolve/reopen."""

    _validate_max_bytes(max_bytes)
    parent_path, leaf, candidate = _split_file_path(path, label)
    parent = secure_open_directory(parent_path, f"{label} parent")
    try:
        try:
            descriptor = os.open(leaf, _regular_flags(), dir_fd=parent.fd)
        except OSError as exc:
            raise _secure_path_error(label, candidate, exc) from exc
        try:
            opened_metadata = os.fstat(descriptor)
            if not stat.S_ISREG(opened_metadata.st_mode):
                raise ValueError(f"{label} is not a regular file: {candidate}")
            data = _read_regular_with_limit(descriptor, label, max_bytes)
            metadata = os.fstat(descriptor)
            if _changed_stat(opened_metadata, metadata):
                raise ValueError(f"{label} changed while being read")
            return SecureFile(
                descriptor,
                parent,
                leaf,
                candidate,
                metadata,
                data,
                max_bytes,
            )
        except Exception:
            os.close(descriptor)
            raise
    except Exception:
        parent.close()
        raise


def load_encoder(name: str | None) -> Any | None:
    if not name:
        return None
    try:
        import tiktoken

        return tiktoken.get_encoding(name)
    except (ImportError, OSError, ValueError) as exc:
        raise RuntimeError(f"cannot load tokenizer {name}: {exc}") from exc


def parse_target(kind: str, value: str) -> Target:
    raw = value.strip()
    route, separator, anchor = raw.partition("::")
    anchor = anchor.strip() if separator else None
    if separator and not anchor:
        raise ValueError(f"empty source anchor: {raw}")

    start = end = None
    range_match = RANGE.fullmatch(route.strip())
    if range_match:
        route = range_match.group(1)
        start = int(range_match.group(2))
        end = int(range_match.group(3))
        if start < 1 or end < start:
            raise ValueError(f"invalid line range: {raw}")

    relative_path = route.strip()
    if not relative_path:
        raise ValueError(f"empty route target: {raw}")
    return Target(kind, raw, relative_path, start, end, anchor)


def parse_routes(text: str) -> list[Target]:
    routes: list[Target] = []
    in_route = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        if line and not line[0].isspace():
            in_route = line.strip() == "route:"
            continue
        if not in_route:
            continue
        if not line.strip():
            continue
        match = ROUTE_LINE.fullmatch(line)
        if match:
            kind, values = match.groups()
            routes.append(parse_target(kind, values))
            continue
        if ACTION_LINE.fullmatch(line) or EXPAND_LINE.fullmatch(line):
            continue
        raise ValueError(f"malformed route line {line_number}: {line.strip()}")
    return routes


def parse_action_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    current: Target | None = None
    in_route = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        if line and not line[0].isspace():
            in_route = line.strip() == "route:"
            current = None
            continue
        if not in_route:
            continue
        route_match = ROUTE_LINE.fullmatch(line)
        if route_match:
            kind, value = route_match.groups()
            current = parse_target(kind, value)
            continue
        if EXPAND_LINE.fullmatch(line):
            current = None
            continue
        if ACTION_LINE.fullmatch(line):
            if current is None or current.kind not in {"edit", "create"}:
                raise ValueError(
                    f"orphan file-owned action on route line {line_number}"
                )
            counts[current.raw] = counts.get(current.raw, 0) + 1
    return counts


def parse_bases(text: str) -> list[str]:
    bases: list[str] = []
    for line in text.splitlines():
        match = BASIS_LINE.fullmatch(line)
        if match:
            bases.append(match.group(1))
    return bases


def parse_verify_entries(text: str) -> list[tuple[str, str]]:
    """Return the labelled verification entries in a Packet v3 ``verify`` block."""

    entries: list[tuple[str, str]] = []
    in_verify = False
    for line in text.splitlines():
        if line and not line[0].isspace():
            in_verify = line.strip() == "verify:"
            continue
        if in_verify:
            match = VERIFY_ENTRY_LINE.fullmatch(line)
            if match:
                entries.append((match.group(1), match.group(2)))
    return entries


def parse_verify_commands(text: str) -> list[str]:
    """Return Packet v3 verification commands without changing its Vn compatibility."""

    return [command for _, command in parse_verify_entries(text)]


def parse_execution_policies(text: str) -> list[str]:
    policies: list[str] = []
    for line in text.splitlines():
        match = EXECUTION_LINE.fullmatch(line)
        if match:
            policies.append(match.group(1))
    return policies


def resolve_inside(repo: Path, relative_path: str) -> Path:
    """Return a lexical in-repository display path without following symlinks."""

    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"route must be repository-relative: {relative_path}")
    parts = [part for part in candidate.parts if part not in {"", "."}]
    if not parts:
        raise ValueError(f"route must name a repository file: {relative_path}")
    return Path(repo) / Path(*parts)


def _route_parts(relative_path: str) -> tuple[str, ...]:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"route must be repository-relative: {relative_path}")
    parts = tuple(part for part in candidate.parts if part not in {"", "."})
    if not parts:
        raise ValueError(f"route must name a repository file: {relative_path}")
    return parts


def _open_route_parent(
    repo: SecureDirectory,
    relative_path: str,
    *,
    allow_missing: bool,
) -> tuple[SecureDirectory | None, str]:
    parts = _route_parts(relative_path)
    current = os.dup(repo.fd)
    ancestors: list[DirectoryEdge] = []
    traversed = repo.path
    try:
        for part in parts[:-1]:
            parent_stat = os.fstat(current)
            following = -1
            try:
                following = os.open(part, _directory_flags(), dir_fd=current)
            except FileNotFoundError:
                if allow_missing:
                    os.close(current)
                    for edge in ancestors:
                        os.close(edge.parent_fd)
                    return None, parts[-1]
                raise ValueError(f"routed file does not exist: {relative_path}")
            except OSError as exc:
                path = repo.path / Path(*parts)
                raise _secure_path_error("routed file", path, exc) from exc
            try:
                child_stat = os.fstat(following)
                named_child = os.stat(
                    part,
                    dir_fd=current,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(child_stat.st_mode)
                    or not stat.S_ISDIR(named_child.st_mode)
                    or _changed_directory(parent_stat, os.fstat(current))
                    or _identity(named_child) != _identity(child_stat)
                ):
                    raise ValueError(
                        "routed file path changed while it was being pinned: "
                        f"{relative_path}"
                    )
            except Exception:
                os.close(following)
                raise
            ancestors.append(
                DirectoryEdge(
                    current,
                    parent_stat,
                    traversed,
                    part,
                    child_stat,
                )
            )
            current = following
            traversed = traversed / part
        metadata = os.fstat(current)
        display_parent = repo.path / Path(*parts[:-1])
        opened = SecureDirectory(current, display_parent, metadata, ancestors)
        opened.validate_stable("routed file parent")
        return opened, parts[-1]
    except Exception:
        os.close(current)
        for edge in ancestors:
            os.close(edge.parent_fd)
        raise


def _open_regular_beneath(
    repo: SecureDirectory,
    relative_path: str,
    *,
    max_bytes: int = MAX_REGULAR_INPUT_BYTES,
) -> SecureFile:
    _validate_max_bytes(max_bytes)
    parent, leaf = _open_route_parent(
        repo,
        relative_path,
        allow_missing=False,
    )
    assert parent is not None
    display = repo.path / Path(*_route_parts(relative_path))
    try:
        try:
            descriptor = os.open(leaf, _regular_flags(), dir_fd=parent.fd)
        except FileNotFoundError as exc:
            raise ValueError(f"routed file does not exist: {relative_path}") from exc
        except OSError as exc:
            raise _secure_path_error("routed file", display, exc) from exc
        try:
            opened_metadata = os.fstat(descriptor)
            if not stat.S_ISREG(opened_metadata.st_mode):
                raise ValueError(f"routed file is not a regular file: {relative_path}")
            data = _read_regular_with_limit(
                descriptor,
                f"routed file {relative_path}",
                max_bytes,
            )
            metadata = os.fstat(descriptor)
            if _changed_stat(opened_metadata, metadata):
                raise ValueError(
                    f"routed file changed while being read: {relative_path}"
                )
            return SecureFile(
                descriptor,
                parent,
                leaf,
                display,
                metadata,
                data,
                max_bytes,
            )
        except Exception:
            os.close(descriptor)
            raise
    except Exception:
        parent.close()
        raise


def _assert_create_missing(repo: SecureDirectory, relative_path: str) -> None:
    parent = _pin_create_missing(repo, relative_path)
    if parent is not None:
        parent.close()


def _pin_create_missing(
    repo: SecureDirectory,
    relative_path: str,
) -> SecureDirectory | None:
    parent, leaf = _open_route_parent(
        repo,
        relative_path,
        allow_missing=True,
    )
    if parent is None:
        return None
    try:
        try:
            os.stat(leaf, dir_fd=parent.fd, follow_symlinks=False)
        except FileNotFoundError:
            return parent
        except OSError as exc:
            display = repo.path / Path(*_route_parts(relative_path))
            raise _secure_path_error("create target", display, exc) from exc
        raise ValueError(f"create target already exists: {relative_path}")
    except Exception:
        parent.close()
        raise


def _pin_nearest_create_ancestor(
    repo: SecureDirectory,
    relative_path: str,
) -> tuple[SecureDirectory, tuple[str, ...]]:
    """Pin the nearest existing parent when a create route has missing parents."""

    parts = _route_parts(relative_path)
    current = os.dup(repo.fd)
    ancestors: list[DirectoryEdge] = []
    display = repo.path
    try:
        for index, part in enumerate(parts[:-1]):
            parent_stat = os.fstat(current)
            following = -1
            try:
                following = os.open(part, _directory_flags(), dir_fd=current)
            except FileNotFoundError:
                opened = SecureDirectory(
                    current,
                    display,
                    os.fstat(current),
                    ancestors,
                )
                opened.validate_stable("create target parent")
                return opened, parts[index:]
            except OSError as exc:
                raise _secure_path_error(
                    "create target",
                    repo.path / Path(*parts),
                    exc,
                ) from exc
            try:
                child_stat = os.fstat(following)
                named_child = os.stat(
                    part,
                    dir_fd=current,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(child_stat.st_mode)
                    or not stat.S_ISDIR(named_child.st_mode)
                    or _changed_directory(parent_stat, os.fstat(current))
                    or _identity(named_child) != _identity(child_stat)
                ):
                    raise ValueError(
                        "create target path changed while it was being pinned: "
                        f"{relative_path}"
                    )
            except Exception:
                os.close(following)
                raise
            ancestors.append(
                DirectoryEdge(
                    current,
                    parent_stat,
                    display,
                    part,
                    child_stat,
                )
            )
            current = following
            display = display / part
        opened = SecureDirectory(
            current,
            display,
            os.fstat(current),
            ancestors,
        )
        opened.validate_stable("create target parent")
        return opened, (parts[-1],)
    except Exception:
        os.close(current)
        for edge in ancestors:
            os.close(edge.parent_fd)
        raise


@dataclass
class RouteSnapshotEntry:
    target: Target
    normalized_path: str
    file: SecureFile | None
    create_parent: SecureDirectory | None = None
    create_parent_is_exact: bool = False
    create_missing_parts: tuple[str, ...] = ()


class RouteSnapshot:
    """One coherent set of pinned routed-file descriptors and bytes."""

    def __init__(
        self,
        repo: SecureDirectory,
        targets: list[Target],
        entries: list[RouteSnapshotEntry],
    ) -> None:
        self.repo = repo
        self.targets = list(targets)
        self.entries = entries
        self._closed = False

    def route_sha256(self) -> str:
        digest = hashlib.sha256()
        indexed = list(enumerate(self.targets))
        for index, target in sorted(
            indexed,
            key=lambda item: (item[1].relative_path, item[1].kind),
        ):
            digest.update(target.relative_path.encode("utf-8"))
            digest.update(b"\0")
            entry = self.entries[index]
            if target.kind == "create":
                digest.update(b"CREATE")
            else:
                assert entry.file is not None
                digest.update(entry.file.data)
            digest.update(b"\0")
        return digest.hexdigest()

    def assert_creates_absent(self) -> None:
        for entry in self.entries:
            target = entry.target
            if target.kind != "create":
                continue
            _assert_create_missing(self.repo, target.relative_path)
            if entry.create_parent is not None:
                entry.create_parent.validate_stable(
                    f"create target {target.relative_path} parent"
                )

    def revalidate(self) -> None:
        """Re-read every route and reject any path, inode, or byte change."""

        self.repo.validate_stable("repository")
        self.assert_creates_absent()
        for entry in self.entries:
            if entry.file is None:
                continue
            label = f"routed file {entry.target.relative_path}"
            entry.file.revalidate(label)
            reopened = _open_regular_beneath(
                self.repo,
                entry.target.relative_path,
                max_bytes=entry.file.max_bytes,
            )
            try:
                if (
                    reopened.identity != entry.file.identity
                    or reopened.data != entry.file.data
                ):
                    raise ValueError(
                        "repository changed while validating routed file "
                        f"{entry.target.relative_path}"
                    )
            finally:
                reopened.close()
        self.assert_creates_absent()

    def close(self) -> None:
        if self._closed:
            return
        for entry in self.entries:
            if entry.file is not None:
                entry.file.close()
            if entry.create_parent is not None:
                entry.create_parent.close()
        self.repo.close()
        self._closed = True

    def __enter__(self) -> RouteSnapshot:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def open_route_snapshot(
    repo: Path | str | SecureDirectory,
    targets: list[Target],
    *,
    max_total_bytes: int | None = None,
) -> RouteSnapshot:
    """Securely pin all existing routes and prove create routes are absent."""

    if max_total_bytes is not None:
        _validate_max_bytes(max_total_bytes)
    if isinstance(repo, SecureDirectory):
        repo_handle = repo.clone("repository")
    else:
        repo_handle = secure_open_directory(repo, "repository")
    entries: list[RouteSnapshotEntry] = []
    try:
        seen_paths: dict[str, Target] = {}
        seen_parts: dict[tuple[str, ...], Target] = {}
        seen_files: dict[tuple[int, int], Target] = {}
        retained_bytes = 0
        for target in targets:
            parts = _route_parts(target.relative_path)
            normalized = "/".join(parts)
            previous = seen_paths.get(normalized)
            if previous is not None:
                raise ValueError(
                    f"duplicate route path {normalized}: "
                    f"{previous.relative_path} ({previous.kind}) and "
                    f"{target.relative_path} ({target.kind})"
                )
            seen_paths[normalized] = target
            for previous_parts, previous_target in seen_parts.items():
                if (
                    previous_target.kind == "create"
                    and len(previous_parts) < len(parts)
                    and parts[: len(previous_parts)] == previous_parts
                ):
                    raise ValueError(
                        "conflicting create route hierarchy: "
                        f"{previous_target.relative_path} and {target.relative_path}"
                    )
                if (
                    target.kind == "create"
                    and len(parts) < len(previous_parts)
                    and previous_parts[: len(parts)] == parts
                ):
                    raise ValueError(
                        "conflicting create route hierarchy: "
                        f"{target.relative_path} and {previous_target.relative_path}"
                    )
            seen_parts[parts] = target
            if target.kind == "create":
                create_parent = _pin_create_missing(
                    repo_handle,
                    target.relative_path,
                )
                create_parent_is_exact = create_parent is not None
                create_missing_parts = (parts[-1],)
                if create_parent is None:
                    create_parent, create_missing_parts = _pin_nearest_create_ancestor(
                        repo_handle,
                        target.relative_path,
                    )
                entries.append(
                    RouteSnapshotEntry(
                        target,
                        normalized,
                        None,
                        create_parent,
                        create_parent_is_exact,
                        create_missing_parts,
                    )
                )
                continue
            max_file_bytes = MAX_REGULAR_INPUT_BYTES
            if max_total_bytes is not None:
                max_file_bytes = min(
                    max_file_bytes,
                    max_total_bytes - retained_bytes,
                )
            opened = _open_regular_beneath(
                repo_handle,
                target.relative_path,
                max_bytes=max_file_bytes,
            )
            previous_file = seen_files.get(opened.identity)
            if previous_file is not None:
                opened.close()
                raise ValueError(
                    "duplicate routed file alias: "
                    f"{previous_file.relative_path} and {target.relative_path}"
                )
            seen_files[opened.identity] = target
            entries.append(RouteSnapshotEntry(target, normalized, opened))
            retained_bytes += len(opened.data)
        return RouteSnapshot(repo_handle, targets, entries)
    except Exception:
        for entry in entries:
            if entry.file is not None:
                entry.file.close()
            if entry.create_parent is not None:
                entry.create_parent.close()
        repo_handle.close()
        raise


def route_sha256(
    repo: Path | str | SecureDirectory,
    targets: list[Target],
) -> str:
    with open_route_snapshot(repo, targets) as snapshot:
        route_hash = snapshot.route_sha256()
        snapshot.revalidate()
        return route_hash


def validate(
    repo: Path | str,
    packet: Path | str,
    encoder: Any | None,
    require_basis: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    with secure_open_regular(packet, "packet") as opened_packet:
        try:
            text = opened_packet.data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("packet is not UTF-8") from exc
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        result, errors = validate_text(repo, text, encoder, require_basis)
        try:
            opened_packet.revalidate("packet")
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(str(exc))
        result["valid"] = not errors
        result["errors"] = errors
        return result, errors


def _token_length(
    encoder: Any,
    text: str,
    owned_snapshot: RouteSnapshot | None,
) -> int:
    try:
        return len(encoder.encode(text))
    except BaseException:
        if owned_snapshot is not None:
            owned_snapshot.close()
        raise


def validate_text(
    repo: Path | str | SecureDirectory,
    text: str,
    encoder: Any | None,
    require_basis: bool = True,
    *,
    route_snapshot: RouteSnapshot | None = None,
    revalidate_snapshot: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    """Validate Packet v3 text against ``repo`` without reading a packet path.

    ``validate`` remains the public path-based API used by the existing CLI.
    Keeping this text-based variant lets consumers validate an in-memory packet
    snapshot without a temporary file or a second packet read.
    """
    errors: list[str] = []
    try:
        targets = parse_routes(text)
        action_counts = parse_action_counts(text)
    except ValueError as exc:
        targets = []
        action_counts = {}
        errors.append(str(exc))
    declared_bases = parse_bases(text)
    declared_basis = declared_bases[0] if declared_bases else None
    verify_commands = parse_verify_commands(text)
    execution_policies = parse_execution_policies(text)
    if not targets:
        errors.append("packet has no route read/edit/create targets")
    if not any(target.kind in {"edit", "create"} for target in targets):
        errors.append("packet route has no edit or create target")
    for target in targets:
        if target.kind in {"edit", "create"} and not action_counts.get(target.raw):
            errors.append(f"route target has no file-owned do action: {target.raw}")
    if not verify_commands:
        errors.append("packet has no exact Vn backtick command under verify")
    if len(declared_bases) > 1:
        errors.append("packet has duplicate basis declarations")
    if execution_policies != [BOUNDED_EXECUTION]:
        errors.append(
            "packet requires exactly one canonical bounded execution policy"
        )

    owned_snapshot: RouteSnapshot | None = None
    snapshot = route_snapshot
    if snapshot is not None and snapshot.targets != targets:
        errors.append("route snapshot does not match parsed packet routes")
        snapshot = None
    if snapshot is None and targets:
        try:
            owned_snapshot = open_route_snapshot(repo, targets)
            snapshot = owned_snapshot
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(str(exc))

    routed_text: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for index, target in enumerate(targets):
        if target.kind == "create":
            if target.start is not None or target.anchor is not None:
                errors.append(f"create target cannot have range or anchor: {target.raw}")
            rows.append({"kind": target.kind, "path": target.relative_path})
            continue
        if snapshot is None:
            continue
        entry = snapshot.entries[index]
        if entry.file is None:
            errors.append(f"routed file does not exist: {target.relative_path}")
            continue
        try:
            file_text = entry.file.data.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"routed file is not UTF-8: {target.relative_path}")
            continue
        file_text = file_text.replace("\r\n", "\n").replace("\r", "\n")
        lines = file_text.splitlines()
        if target.end is not None and target.end > len(lines):
            errors.append(
                f"line range exceeds {target.relative_path}: "
                f"{target.start}-{target.end} > {len(lines)}"
            )
        selected = (
            "\n".join(lines[target.start - 1 : target.end]) + "\n"
            if target.start is not None and target.end is not None
            else file_text
        )
        if target.anchor:
            occurrences = selected.count(target.anchor)
            if occurrences == 0:
                errors.append(
                    f"source anchor not found in routed context for "
                    f"{target.relative_path}: {target.anchor}"
                )
            elif occurrences > 1:
                errors.append(
                    f"source anchor is ambiguous in routed context for "
                    f"{target.relative_path}: {target.anchor}"
                )
        routed_text[target.relative_path] = selected
        row: dict[str, Any] = {
            "kind": target.kind,
            "path": target.relative_path,
            "bytes": len(selected.encode("utf-8")),
            "lines": len(selected.splitlines()),
        }
        if encoder is not None:
            row["tokens"] = _token_length(encoder, selected, owned_snapshot)
        rows.append(row)

    packet_metrics: dict[str, int] = {
        "bytes": len(text.encode("utf-8")),
        "lines": len(text.splitlines()),
    }
    routed_metrics: dict[str, int] = {
        "files": len(routed_text),
        "bytes": sum(len(value.encode("utf-8")) for value in routed_text.values()),
        "lines": sum(len(value.splitlines()) for value in routed_text.values()),
    }
    if encoder is not None:
        packet_metrics["tokens"] = _token_length(encoder, text, owned_snapshot)
        routed_metrics["tokens"] = sum(
            _token_length(encoder, value, owned_snapshot)
            for value in routed_text.values()
        )

    computed_basis = snapshot.route_sha256() if snapshot is not None else None
    if snapshot is not None and revalidate_snapshot:
        try:
            snapshot.revalidate()
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(f"cannot revalidate route snapshot: {exc}")
    if require_basis and declared_basis is None:
        errors.append("packet has no valid basis: route-sha256:<64 lowercase hex>")
    elif require_basis and computed_basis and declared_basis != computed_basis:
        errors.append(
            f"stale route basis: declared {declared_basis}, current {computed_basis}"
        )

    result: dict[str, Any] = {
        "valid": not errors,
        "encoding": getattr(encoder, "name", None) if encoder is not None else None,
        "route_sha256": computed_basis,
        "declared_route_sha256": declared_basis,
        "verify_commands": verify_commands,
        "execution_policy": (
            execution_policies[0] if len(execution_policies) == 1 else None
        ),
        "packet": packet_metrics,
        "routed_context": routed_metrics,
        "total_context": {
            key: packet_metrics[key] + routed_metrics[key]
            for key in ("bytes", "lines", "tokens")
            if key in packet_metrics and key in routed_metrics
        },
        "targets": rows,
        "file_owned_actions": action_counts,
        "errors": errors,
    }
    if owned_snapshot is not None:
        owned_snapshot.close()
    return result, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    parser.add_argument("packet", type=Path)
    parser.add_argument(
        "--encoding",
        default=os.environ.get("SEMANTIC_SPEC_TOKEN_ENCODING"),
        help="optional tiktoken encoding, for example o200k_base",
    )
    parser.add_argument("--max-context-tokens", type=int)
    parser.add_argument(
        "--print-basis",
        action="store_true",
        help="print the computed route-sha256 without requiring a declared basis",
    )
    args = parser.parse_args()

    try:
        encoder = load_encoder(args.encoding)
        result, errors = validate(
            args.repo,
            args.packet,
            encoder,
            not args.print_basis,
        )
        if args.max_context_tokens is not None:
            if encoder is None:
                raise ValueError("--max-context-tokens requires --encoding")
            if args.max_context_tokens < 1:
                raise ValueError("--max-context-tokens must be positive")
            result["max_context_tokens"] = args.max_context_tokens
            actual = result["total_context"]["tokens"]
            if actual > args.max_context_tokens:
                errors.append(
                    f"context token budget exceeded: {actual} > {args.max_context_tokens}"
                )
                result["valid"] = False
                result["errors"] = errors
    except (OSError, UnicodeError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.print_basis and result.get("route_sha256") and not errors:
        print(f"route-sha256:{result['route_sha256']}")
    else:
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
