#!/usr/bin/env python3
"""Reproducible paired benchmark for Semantic Spec Writer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import re
import shutil
import shlex
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"
CASES_DIR = BENCHMARKS / "cases"
RESULTS_DIR = BENCHMARKS / "results"
VARIANTS = {
    "baseline": "baseline.md",
    "semantic": "semantic.spec.ctx",
}
COMMAND_CATEGORY_PATTERNS = {
    "discovery": re.compile(r"(?:^|[;&|()]|\s)(?:rg|grep|find|ls|tree)(?:\s|$)"),
    "read": re.compile(r"(?:^|[;&|()]|\s)(?:cat|head|tail|sed|nl)(?:\s|$)"),
    "verify": re.compile(
        r"(?:pytest|unittest|py_compile|npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test)"
    ),
}
VERIFY_LINE = re.compile(r"^  V\d+:\s*`([^`]+)`\s*$")
CASE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
EXECUTION_PACKET_CHECK = (
    ROOT
    / "skills"
    / "semantic-spec-writer"
    / "scripts"
    / "check_execution_packet.py"
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


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


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
) -> dict[str, Any]:
    workspace = workspace.resolve()
    command = [
        sys.executable,
        str(BENCHMARKS / "grader.py"),
        str(case.path),
        str(workspace),
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
    return {
        "command": str(declared),
        "fixture_sha256": verification_fixture_sha256(fixtures),
        "return_code": completed.returncode,
        "stdout_tail": completed.stdout[-2000:],
        "stderr_tail": completed.stderr[-2000:],
    }


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


def empty_grade(case: BenchmarkCase) -> dict[str, Any]:
    total = len(read_json(case.path / "tests.json")["tests"])
    return {
        "passed": 0,
        "total": total,
        "pass_rate": 0.0,
        "acceptance_passed": 0,
        "acceptance_total": 0,
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
            str(EXECUTION_PACKET_CHECK),
            str(case.path / "starter"),
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
        verified = run_verification(case, reference_workspace, trusted=True)
        if verified and verified["return_code"] != 0:
            errors.append(
                f"{case.id}: reference verification failed: "
                f"{verified['stdout_tail'].strip()} "
                f"{verified['stderr_tail'].strip()}"
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


def safe_workspace(case: BenchmarkCase, root: Path) -> Path:
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
    copy_fixture_tree(
        case.path / "starter",
        workspace,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
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


def benchmark_prompt(spec: str) -> str:
    return (
        "Implement the specification below in the current workspace.\n"
        "Keep changes scoped to the requested behavior. Do not access files outside "
        "the workspace. Do not use network access. Finish only after checking the "
        "implementation for syntax errors.\n\n"
        "--- BEGIN SPECIFICATION ---\n"
        f"{spec.rstrip()}\n"
        "--- END SPECIFICATION ---\n"
    )


def benchmark_case_snapshot(
    case: BenchmarkCase,
    semantic_spec: str,
) -> dict[str, Any]:
    baseline = case.spec_path("baseline").read_text(encoding="utf-8")
    variants = {"baseline": baseline, "semantic": semantic_spec}
    return {
        "fixture_sha256": tree_sha256(case.path),
        "starter_sha256": tree_sha256(case.path / "starter"),
        "verification_fixture_sha256": verification_fixture_sha256(
            verification_fixtures(case)
        ),
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


def require_benchmark_case_snapshot(
    case: BenchmarkCase,
    semantic_spec: str,
    expected: dict[str, Any],
) -> None:
    if benchmark_case_snapshot(case, semantic_spec) != expected:
        raise RuntimeError(f"{case.id}: benchmark fixture changed during run")


def parse_codex_events(stdout: str) -> dict[str, Any]:
    usage: dict[str, int] = {}
    tool_calls: dict[str, int] = {}
    command_log: list[dict[str, Any]] = []
    command_categories = {name: 0 for name in COMMAND_CATEGORY_PATTERNS}
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
                if item_type == "command_execution":
                    command = str(item.get("command", ""))
                    command_log.append({
                        "command": command[:2000],
                        "exit_code": item.get("exit_code"),
                    })
                    for name, pattern in COMMAND_CATEGORY_PATTERNS.items():
                        command_categories[name] += bool(pattern.search(command))

    if usage:
        usage = {key: int(value) for key, value in usage.items() if isinstance(value, int)}
        usage["uncached_input_tokens"] = max(
            usage.get("input_tokens", 0) - usage.get("cached_input_tokens", 0), 0
        )
    return {
        "usage": usage,
        "tool_calls": tool_calls,
        "tool_call_total": sum(tool_calls.values()),
        "command_log": command_log,
        "command_categories": command_categories,
        "thread_id": thread_id,
        "final_message": final_message[-2000:],
        "event_errors": event_errors,
    }


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
    parsed = parse_codex_events(stdout)
    parsed.update({
        "return_code": process.returncode,
        "duration_seconds": round(elapsed, 3),
        "stderr_tail": stderr[-2000:],
    })
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
        "final_message": "mock provider copied the reference solution",
        "event_errors": [],
        "stderr_tail": "",
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


def create_run_document(
    args: argparse.Namespace,
    cases: list[BenchmarkCase],
    semantic_specs: dict[str, str],
    cases_dir: Path,
) -> dict[str, Any]:
    corpus = discover_cases(cases_dir=cases_dir)
    return {
        "schema_version": 1,
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
            "reduced: hidden tests and hidden expected outputs stay in the grader "
            "parent; visible smoke assertions are shared equally across arms"
        ),
        "fixture_snapshot": {
            case.id: benchmark_case_snapshot(case, semantic_specs[case.id])
            for case in cases
        },
        "static": static_rows(cases, semantic_specs),
        "results": [],
    }


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
    output = output.resolve()
    if output.exists() and not args.force:
        raise RuntimeError(f"refusing to overwrite result: {output}; pass --force to replace it")
    document = create_run_document(args, cases, semantic_specs, cases_dir)
    pairs = [
        (case, repetition)
        for case in cases
        for repetition in range(1, args.repetitions + 1)
    ]
    random.Random(args.seed).shuffle(pairs)
    jobs: list[tuple[BenchmarkCase, int, str]] = []
    for pair_index, (case, repetition) in enumerate(pairs):
        order = ["baseline", "semantic"]
        if (pair_index + args.seed) % 2:
            order.reverse()
        jobs.extend((case, repetition, variant) for variant in order)

    keep_root = RESULTS_DIR / "workspaces" / document["run_id"]
    temporary_context = None
    if args.keep_workspaces:
        keep_root.mkdir(parents=True, exist_ok=False)
        workspace_root = keep_root
    else:
        temporary_context = tempfile.TemporaryDirectory(prefix="semantic-spec-bench-")
        workspace_root = Path(temporary_context.name)

    try:
        for index, (case, repetition, variant) in enumerate(jobs, start=1):
            expected_snapshot = document["fixture_snapshot"][case.id]
            require_benchmark_case_snapshot(
                case, semantic_specs[case.id], expected_snapshot
            )
            run_root = workspace_root / f"{index:03d}-{case.id}-{variant}-r{repetition}"
            run_root.mkdir(parents=True, exist_ok=False)
            workspace = safe_workspace(case, run_root)
            spec = (
                case.spec_path("baseline").read_text(encoding="utf-8")
                if variant == "baseline"
                else semantic_specs[case.id]
            )
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
                "stderr_tail": "",
            }
            provider_completed = False
            verification = None
            grade = empty_grade(case)
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
                    grade = run_grader(case, workspace, trusted=trusted)
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
            result = {
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
            }
            document["results"].append(result)
            write_json_atomic(output, document)
    finally:
        if temporary_context is not None:
            temporary_context.cleanup()

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


def paired_reductions(results: list[dict[str, Any]], field: str) -> list[float]:
    pairs: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    for result in results:
        key = (result["case"], result["repetition"])
        pairs.setdefault(key, {})[result["variant"]] = result
    reductions = []
    for pair in pairs.values():
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
    pairs: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    for result in results:
        key = (result["case"], result["repetition"])
        pairs.setdefault(key, {})[result["variant"]] = result

    by_case: dict[str, list[float]] = {}
    values: list[float] = []
    for (case, _), pair in pairs.items():
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
    semantic_source = document.get("semantic_source", "curated")
    snapshots = document.get("fixture_snapshot")
    try:
        corpus = recorded_case_corpus(document)
        current = {
            case.id: benchmark_case_snapshot(
                case, case.spec_path("semantic").read_text(encoding="utf-8")
            )
            for case in corpus
        }
    except (OSError, RuntimeError, ValueError):
        return False

    corpus_ids = {case.id for case in corpus}
    if (
        not isinstance(cases, list)
        or not cases
        or any(not isinstance(case, str) for case in cases)
        or len(cases) != len(set(cases))
        or set(cases) != corpus_ids
        or not isinstance(repetitions, int)
        or isinstance(repetitions, bool)
        or repetitions < 3
        or semantic_source not in {"curated", "generated"}
        or not isinstance(snapshots, dict)
        or set(snapshots) != corpus_ids
        or not isinstance(document.get("static"), list)
        or not results
    ):
        return False

    hash_pattern = re.compile(r"^[0-9a-f]{64}$")
    for case in corpus:
        expected = snapshots.get(case.id)
        actual = current[case.id]
        if not isinstance(expected, dict):
            return False
        if semantic_source == "curated":
            if expected != actual:
                return False
            continue
        if (
            set(expected) != {
                "fixture_sha256",
                "starter_sha256",
                "verification_fixture_sha256",
                "variants",
                "prompts",
                "metrics",
            }
            or expected.get("fixture_sha256") != actual["fixture_sha256"]
            or expected.get("starter_sha256") != actual["starter_sha256"]
            or expected.get("verification_fixture_sha256")
            != actual["verification_fixture_sha256"]
            or not isinstance(expected.get("variants"), dict)
            or expected["variants"].get("baseline")
            != actual["variants"]["baseline"]
            or not hash_pattern.fullmatch(str(expected["variants"].get("semantic", "")))
            or not isinstance(expected.get("prompts"), dict)
            or expected["prompts"].get("baseline")
            != actual["prompts"]["baseline"]
            or not hash_pattern.fullmatch(str(expected["prompts"].get("semantic", "")))
            or not isinstance(expected.get("metrics"), dict)
            or expected["metrics"].get("baseline") != actual["metrics"]["baseline"]
            or set(expected["metrics"].get("semantic", {}))
            != {"bytes", "characters", "words", "lines"}
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
                for value in expected["metrics"]["semantic"].values()
            )
        ):
            return False

    if any(
        not isinstance(row, dict) or not isinstance(row.get("case"), str)
        for row in document["static"]
    ):
        return False
    static_by_case = {row["case"]: row for row in document["static"]}
    if len(static_by_case) != len(document["static"]) or set(static_by_case) != corpus_ids:
        return False
    cases_by_id = {case.id: case for case in corpus}
    for case_id, row in static_by_case.items():
        snapshot = snapshots[case_id]
        baseline_metrics = snapshot["metrics"]["baseline"]
        semantic_metrics = snapshot["metrics"]["semantic"]
        expected_row = {
            "case": case_id,
            "title": cases_by_id[case_id].manifest["title"],
            "baseline": baseline_metrics,
            "semantic": semantic_metrics,
            "byte_reduction_percent": compression_percent(
                baseline_metrics["bytes"], semantic_metrics["bytes"]
            ),
            "word_reduction_percent": compression_percent(
                baseline_metrics["words"], semantic_metrics["words"]
            ),
            "token_reduction_percent": None,
        }
        if row != expected_row:
            return False

    expected_keys = {
        (case, repetition, variant)
        for case in cases
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
        provider = result.get("provider")
        usage = provider.get("usage") if isinstance(provider, dict) else None
        provenance = result.get("provenance")
        expected_provenance = {
            "spec_sha256": snapshot["variants"][variant],
            "prompt_sha256": snapshot["prompts"][variant],
            "starter_sha256": snapshot["starter_sha256"],
            "fixture_sha256": snapshot["fixture_sha256"],
        }
        verification = result.get("verification")
        verification_command = case.manifest.get("verification_command")
        verification_valid = (
            verification is None
            if not verification_command
            else isinstance(verification, dict)
            and verification.get("command") == verification_command
            and verification.get("fixture_sha256")
            == snapshot["verification_fixture_sha256"]
            and verification.get("return_code") == 0
        )
        grade = result.get("grade")
        if (
            result.get("pair_id") != f"{case.id}:r{result['repetition']}"
            or result.get("spec") != snapshot["metrics"][variant]
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


def render_report(document: dict[str, Any]) -> str:
    results = document["results"]
    baseline = aggregate_variant(results, "baseline")
    semantic = aggregate_variant(results, "semantic")
    input_reductions = paired_reductions(results, "input_tokens")
    uncached_reductions = paired_reductions(results, "uncached_input_tokens")
    input_summary = paired_summary(results, "input_tokens")
    uncached_summary = paired_summary(results, "uncached_input_tokens")
    credible = implementation_report_is_credible(document, results)

    lines = [
        "# Semantic Spec Writer Benchmark",
        "",
        f"Run: `{document['run_id']}`  ",
        f"Provider: `{document['provider']}`  ",
        f"Model: `{document.get('model') or 'provider default'}`  ",
        f"Reasoning effort: `{document.get('reasoning_effort') or 'provider default'}`  ",
        f"Cases: {len(document['cases'])}  ",
        f"Case suite: `{document.get('case_suite', 'cases')}`  ",
        f"Repetitions: {document['repetitions']}  ",
        f"Semantic source: `{document.get('semantic_source', 'curated')}`",
        "",
    ]
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
        ("Shell command executions", "total_command_executions"),
        ("Tool calls", "total_tool_calls"),
    ]
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
        "- Hidden tests and hidden expected outputs stay outside the solution process. "
        "Visible smoke assertions are restored from immutable fixtures for both arms. "
        "Implementation agents use workspace-write; hidden solution calls run in a "
        "network-disabled read-only sandbox. This is not a full VM boundary.",
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
            report = render_report(read_json(args.result))
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
