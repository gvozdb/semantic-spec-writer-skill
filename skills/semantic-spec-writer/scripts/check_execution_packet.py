#!/usr/bin/env python3
"""Validate a repository-grounded semantic execution packet."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROUTE_LINE = re.compile(r"^  (read|edit|create):\s*(.+?)\s*$")
RANGE = re.compile(r"^(.*?):(\d+)-(\d+)$")
BASIS_LINE = re.compile(r"^basis:\s*route-sha256:([0-9a-f]{64})\s*$")
VERIFY_LINE = re.compile(r"^  V\d+:\s*`([^`]+)`\s*$")
ACTION_LINE = re.compile(r"^    do:\s*(.+?)\s*$")
EXPAND_LINE = re.compile(r"^  expand:\s*(.+?)\s*$")
EXECUTION_LINE = re.compile(r"^execution:\s*(.+?)\s*$")
BOUNDED_EXECUTION = (
    "routed read once -> all do -> V1 once -> stop on pass; "
    "expand only on contradiction/failure"
)


@dataclass(frozen=True)
class Target:
    kind: str
    raw: str
    relative_path: str
    start: int | None
    end: int | None
    anchor: str | None


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


def parse_verify_commands(text: str) -> list[str]:
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


def parse_execution_policies(text: str) -> list[str]:
    policies: list[str] = []
    for line in text.splitlines():
        match = EXECUTION_LINE.fullmatch(line)
        if match:
            policies.append(match.group(1))
    return policies


def route_sha256(repo: Path, targets: list[Target]) -> str:
    digest = hashlib.sha256()
    for target in sorted(targets, key=lambda item: (item.relative_path, item.kind)):
        digest.update(target.relative_path.encode("utf-8"))
        digest.update(b"\0")
        if target.kind == "create":
            digest.update(b"CREATE")
        else:
            digest.update(resolve_inside(repo, target.relative_path).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def resolve_inside(repo: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"route must be repository-relative: {relative_path}")
    resolved = (repo / candidate).resolve()
    try:
        resolved.relative_to(repo)
    except ValueError as exc:
        raise ValueError(f"route escapes repository: {relative_path}") from exc
    return resolved


def validate(
    repo: Path,
    packet: Path,
    encoder: Any | None,
    require_basis: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    text = packet.read_text(encoding="utf-8")
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

    seen: dict[Path, tuple[str, str]] = {}
    routed_text: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for target in targets:
        try:
            path = resolve_inside(repo, target.relative_path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if path in seen:
            previous_kind, previous_path = seen[path]
            canonical_path = path.relative_to(repo).as_posix()
            errors.append(
                f"duplicate route path {canonical_path}: "
                f"{previous_path} ({previous_kind}) and "
                f"{target.relative_path} ({target.kind})"
            )
            continue
        seen[path] = (target.kind, target.relative_path)

        exists = path.exists()
        if target.kind == "create":
            if exists:
                errors.append(f"create target already exists: {target.relative_path}")
            if target.start is not None or target.anchor is not None:
                errors.append(f"create target cannot have range or anchor: {target.raw}")
            rows.append({"kind": target.kind, "path": target.relative_path})
            continue
        if not path.is_file():
            errors.append(f"routed file does not exist: {target.relative_path}")
            continue

        file_text = path.read_text(encoding="utf-8")
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
            row["tokens"] = len(encoder.encode(selected))
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
        packet_metrics["tokens"] = len(encoder.encode(text))
        routed_metrics["tokens"] = sum(
            len(encoder.encode(value)) for value in routed_text.values()
        )

    computed_basis = None
    if targets and not any(
        "does not exist" in error or "escapes repository" in error
        for error in errors
    ):
        try:
            computed_basis = route_sha256(repo, targets)
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(f"cannot compute route basis: {exc}")
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
        repo = args.repo.resolve(strict=True)
        packet = args.packet.resolve(strict=True)
        if not repo.is_dir():
            raise ValueError(f"repository is not a directory: {repo}")
        encoder = load_encoder(args.encoding)
        result, errors = validate(repo, packet, encoder, not args.print_basis)
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
