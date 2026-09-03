from __future__ import annotations

import ast
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "benchmarks" / "validate_capsule_release.py"
HANDOFF = ROOT / "benchmarks" / "handoff.py"
sys.path.insert(0, str(ROOT / "benchmarks"))
import benchmark as core


CORE_REQUIRED = (
    "benchmarks/handoff.py",
    "benchmarks/validate_capsule_release.py",
)
LAUNCHER_REQUIRED = (
    "benchmarks/benchmark.py",
    "benchmarks/capsule-lifecycle-v1.prereg.json",
    "benchmarks/capsule-lifecycle-v2.prereg.json",
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


def run_git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def write_file(repository: Path, relative: str, content: str) -> Path:
    path = repository / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def initialize_repository(repository: Path) -> None:
    run_git(repository, "init", "--quiet")
    run_git(repository, "config", "user.name", "Capsule Security Test")
    run_git(repository, "config", "user.email", "capsule@example.invalid")


def commit_all(repository: Path, message: str) -> None:
    run_git(repository, "add", "--all")
    run_git(repository, "commit", "--quiet", "-m", message)


def publish_pair(repository: Path, run_name: str = "safe-run") -> Path:
    directory = repository / "benchmarks" / "results" / "published" / run_name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "capsule-r3.json").write_text("{}\n", encoding="utf-8")
    (directory / "CAPSULE.md").write_text("report\n", encoding="utf-8")
    (repository / "CAPSULE_BENCHMARK.md").write_text(
        "report\n", encoding="utf-8"
    )
    return directory


def literal_assignment(path: Path, name: str) -> object:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for statement in module.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                return ast.literal_eval(statement.value)
    raise AssertionError(f"missing assignment {name} in {path}")


class GitReleasePolicyTest(unittest.TestCase):
    @contextlib.contextmanager
    def repository(self):
        with tempfile.TemporaryDirectory(prefix="capsule-release-policy-") as directory:
            repository = Path(directory)
            initialize_repository(repository)
            write_file(repository, ".gitignore", "__pycache__/\n*.pyc\n")
            write_file(repository, "README.md", "root claims\n")
            write_file(repository, "benchmarks/README.md", "benchmark claims\n")
            for path in CORE_REQUIRED:
                write_file(repository, path, f"# {path}\n")
            commit_all(repository, "attested code")
            with mock.patch.object(core, "ROOT", repository):
                attestation = core.git_revision_attestation(
                    CORE_REQUIRED,
                    require_clean=True,
                )
                yield repository, attestation

    def valid(self, attestation: dict[str, object]) -> bool:
        return core.git_revision_attestation_is_valid(
            attestation,
            CORE_REQUIRED,
            environment_commit=attestation["commit"],
        )

    def test_absent_and_exact_artifact_states_preserve_attestation(self) -> None:
        with self.repository() as (repository, attestation):
            self.assertTrue(self.valid(attestation))
            publish_pair(repository)
            self.assertTrue(self.valid(attestation))
            (repository / "README.md").write_text(
                "updated root claims\n", encoding="utf-8"
            )
            (repository / "benchmarks" / "README.md").write_text(
                "updated benchmark claims\n", encoding="utf-8"
            )
            self.assertTrue(self.valid(attestation))
            commit_all(repository, "publish exact Capsule pair")
            self.assertTrue(self.valid(attestation))

    def test_committed_import_shadows_and_arbitrary_files_are_rejected(self) -> None:
        for relative in (
            "benchmarks/json.py",
            "json.py",
            "release-config.yml",
            "scripts/publish.sh",
        ):
            with self.subTest(path=relative), self.repository() as (
                repository,
                attestation,
            ):
                publish_pair(repository)
                write_file(repository, relative, "import os\nos._exit(0)\n")
                commit_all(repository, f"add forbidden {relative}")
                self.assertFalse(self.valid(attestation))

    def test_untracked_and_ignored_import_shadows_are_rejected(self) -> None:
        for relative in (
            "json.py",
            "benchmarks/json.py",
            "json.pyc",
            "benchmarks/json.pyc",
            "benchmarks/__pycache__/json.cpython-312.pyc",
        ):
            with self.subTest(path=relative), self.repository() as (
                repository,
                attestation,
            ):
                path = repository / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"sourceless-shadow")
                self.assertFalse(self.valid(attestation))

    def test_second_directory_and_unexpected_descendants_are_rejected(self) -> None:
        with self.repository() as (repository, attestation):
            publish_pair(repository)
            publish_pair(repository, "second-run")
            self.assertFalse(self.valid(attestation))
        with self.repository() as (repository, attestation):
            directory = publish_pair(repository)
            (directory / "release.ini").write_text("unsafe=true\n", encoding="utf-8")
            self.assertFalse(self.valid(attestation))

    def test_symlink_type_and_executable_mode_changes_are_rejected(self) -> None:
        with self.repository() as (repository, attestation):
            directory = publish_pair(repository)
            report = directory / "CAPSULE.md"
            report.unlink()
            report.symlink_to("capsule-r3.json")
            self.assertFalse(self.valid(attestation))
        with self.repository() as (repository, attestation):
            publish_pair(repository)
            (repository / "CAPSULE_BENCHMARK.md").chmod(0o755)
            self.assertFalse(self.valid(attestation))
        for mutation in ("mode", "content"):
            with self.subTest(mutation=mutation), self.repository() as (
                repository,
                attestation,
            ):
                launcher = repository / "benchmarks" / "validate_capsule_release.py"
                if mutation == "mode":
                    launcher.chmod(0o755)
                else:
                    launcher.write_text("# changed launcher\n", encoding="utf-8")
                self.assertFalse(self.valid(attestation))

    def test_rename_copy_and_delete_records_are_rejected(self) -> None:
        with self.repository() as (repository, attestation):
            run_git(repository, "mv", "README.md", "CAPSULE_BENCHMARK.md")
            commit_all(repository, "rename README into report")
            self.assertFalse(self.valid(attestation))
        with self.repository() as (repository, attestation):
            shutil.copy2(
                repository / "README.md",
                repository / "CAPSULE_BENCHMARK.md",
            )
            run_git(repository, "add", "CAPSULE_BENCHMARK.md")
            self.assertFalse(self.valid(attestation))
        with self.repository() as (repository, attestation):
            shutil.copy2(
                repository / "README.md",
                repository / "CAPSULE_BENCHMARK.md",
            )
            commit_all(repository, "copy README into report")
            self.assertFalse(self.valid(attestation))
        with self.repository() as (repository, attestation):
            (repository / "README.md").unlink()
            commit_all(repository, "delete README")
            self.assertFalse(self.valid(attestation))

    def test_git_errors_and_hidden_index_flags_fail_closed(self) -> None:
        with self.repository() as (repository, attestation):
            run_git(repository, "update-index", "--assume-unchanged", "README.md")
            self.assertFalse(self.valid(attestation))
        with self.repository() as (_, attestation):
            with mock.patch.object(
                core,
                "_git_capture",
                side_effect=RuntimeError("simulated Git failure"),
            ):
                self.assertFalse(self.valid(attestation))


class IsolatedLauncherTest(unittest.TestCase):
    def test_launcher_and_handoff_attest_the_same_exact_code_paths(self) -> None:
        self.assertEqual(
            literal_assignment(LAUNCHER, "REQUIRED_CODE_PATHS"),
            literal_assignment(HANDOFF, "CAPSULE_CODE_PATHS"),
        )
        self.assertEqual(
            literal_assignment(LAUNCHER, "REQUIRED_CODE_PATHS"),
            LAUNCHER_REQUIRED,
        )

    @contextlib.contextmanager
    def repository(self):
        with tempfile.TemporaryDirectory(prefix="capsule-launcher-") as directory:
            repository = Path(directory)
            initialize_repository(repository)
            write_file(repository, ".gitignore", "__pycache__/\n*.pyc\n")
            write_file(repository, "README.md", "root claims\n")
            write_file(repository, "benchmarks/README.md", "benchmark claims\n")
            write_file(
                repository,
                "benchmarks/results/published/.gitkeep",
                "",
            )
            write_file(repository, "benchmarks/tests/.gitkeep", "")
            write_file(
                repository,
                "benchmarks/handoff-cases/probe/case.json",
                "trusted fixture\n",
            )
            for relative in LAUNCHER_REQUIRED:
                if relative == "benchmarks/validate_capsule_release.py":
                    destination = repository / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(LAUNCHER, destination)
                elif relative == "benchmarks/handoff.py":
                    write_file(
                        repository,
                        relative,
                        "import json\n"
                        "def validate_capsule_release(document, rendered_report):\n"
                        "    if document.get('fixture') == 'ok' and rendered_report == b'report\\n':\n"
                        "        return []\n"
                        "    return ['stub validation failed']\n",
                    )
                else:
                    write_file(repository, relative, f"# safe stub: {relative}\n")
            commit_all(repository, "attested launcher code")
            with mock.patch.object(core, "ROOT", repository):
                attestation = core.git_revision_attestation(
                    LAUNCHER_REQUIRED,
                    require_clean=True,
                )
            yield repository, attestation

    def publish(
        self,
        repository: Path,
        attestation: dict[str, object],
        run_name: str = "safe-run",
    ) -> Path:
        directory = (
            repository / "benchmarks" / "results" / "published" / run_name
        )
        directory.mkdir(parents=True, exist_ok=True)
        document = {
            "fixture": "ok",
            "environment": {"git_commit": attestation["commit"]},
            "code_revision": attestation,
        }
        (directory / "capsule-r3.json").write_text(
            json.dumps(document, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (directory / "CAPSULE.md").write_text("report\n", encoding="utf-8")
        (repository / "CAPSULE_BENCHMARK.md").write_text(
            "report\n", encoding="utf-8"
        )
        return directory

    def run_launcher(
        self,
        repository: Path,
        *arguments: str,
        isolated: bool = True,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        isolation = " -I" if isolated else ""
        bootstrap = (
            "env -i PATH=\"$PATH\" LC_ALL=C GIT_NO_REPLACE_OBJECTS=1 "
            "GIT_OPTIONAL_LOCKS=0 git --no-pager cat-file blob "
            "HEAD:benchmarks/validate_capsule_release.py | "
            f'"$1"{isolation} /dev/stdin "${{@:2}}"'
        )
        command = [
            "bash",
            "-o",
            "pipefail",
            "-c",
            bootstrap,
            "capsule-release-bootstrap",
            sys.executable,
            *arguments,
        ]
        return subprocess.run(
            command,
            cwd=repository,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_stage_one_absence_and_exact_stage_two_pair(self) -> None:
        with self.repository() as (repository, attestation):
            absent = self.run_launcher(repository)
            self.assertEqual(absent.returncode, 0, absent.stderr)
            self.assertIn("stage-1 validation passed", absent.stdout)
            self.publish(repository, attestation)
            exact = self.run_launcher(
                repository,
                "benchmarks/results/published/safe-run/capsule-r3.json",
                "CAPSULE_BENCHMARK.md",
            )
            self.assertEqual(exact.returncode, 0, exact.stderr)
            self.assertIn("validated current Capsule release artifacts", exact.stdout)

    def test_stage_one_rejects_repository_import_shadows(self) -> None:
        for relative in (
            "json.py",
            "benchmarks/json.py",
            "benchmarks/tests/argparse.py",
            "skills/semantic-spec-writer/scripts/hashlib.py",
            "json.pyc",
            "benchmarks/unrelated.pyc",
            "benchmarks/__pycache__/json.cpython-312.pyc",
        ):
            with self.subTest(path=relative), self.repository() as (
                repository,
                _,
            ):
                path = repository / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"import os\nos._exit(0)\n")
                failed = self.run_launcher(repository)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn("Capsule release validation failed", failed.stderr)

    def test_attested_modules_are_executed_from_git_blobs_not_live_paths(self) -> None:
        with self.repository() as (repository, _):
            marker = repository / "malicious-module-executed"
            safe_handoff = (
                "import builtins\n"
                "from pathlib import Path\n"
                "Path(__file__).write_text(\n"
                "    builtins._capsule_safe_handoff, encoding='utf-8'\n"
                ")\n"
                "def validate_capsule_release(document, rendered_report):\n"
                "    if document.get('fixture') == 'ok' and rendered_report == b'report\\n':\n"
                "        return []\n"
                "    return ['stub validation failed']\n"
            )
            malicious_handoff = (
                "import builtins\n"
                "from pathlib import Path\n"
                "import os\n"
                "Path(__file__).write_text(\n"
                "    builtins._capsule_safe_handoff, encoding='utf-8'\n"
                ")\n"
                f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
                "os._exit(0)\n"
            )
            benchmark_source = (
                "import builtins\n"
                "from pathlib import Path\n"
                "target = Path(__file__).with_name('handoff.py')\n"
                "builtins._capsule_safe_handoff = target.read_text(encoding='utf-8')\n"
                f"target.write_text({malicious_handoff!r}, encoding='utf-8')\n"
            )
            write_file(repository, "benchmarks/benchmark.py", benchmark_source)
            write_file(repository, "benchmarks/handoff.py", safe_handoff)
            commit_all(repository, "attest deterministic path-swap probe")
            with mock.patch.object(core, "ROOT", repository):
                attestation = core.git_revision_attestation(
                    LAUNCHER_REQUIRED,
                    require_clean=True,
                )
            self.publish(repository, attestation)

            completed = self.run_launcher(repository)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(marker.exists())

    def test_attested_fixtures_are_materialized_before_module_execution(self) -> None:
        with self.repository() as (repository, _):
            fixture = (
                repository
                / "benchmarks"
                / "handoff-cases"
                / "probe"
                / "case.json"
            )
            benchmark_source = (
                "from pathlib import Path\n"
                "Path(__file__).with_name('handoff-cases').joinpath(\n"
                "    'probe', 'case.json'\n"
                ").write_text('substituted fixture\\n', encoding='utf-8')\n"
            )
            handoff_source = (
                "from pathlib import Path\n"
                "def validate_capsule_release(document, rendered_report):\n"
                "    fixture = CASES_DIR / 'probe' / 'case.json'\n"
                "    observed = fixture.read_bytes()\n"
                "    Path(__file__).with_name('handoff-cases').joinpath(\n"
                "        'probe', 'case.json'\n"
                "    ).write_text('trusted fixture\\n', encoding='utf-8')\n"
                "    if (document.get('fixture') == 'ok'\n"
                "            and rendered_report == b'report\\n'\n"
                "            and observed == b'trusted fixture\\n'):\n"
                "        return []\n"
                "    return ['fixture was not captured from the attested commit']\n"
            )
            write_file(repository, "benchmarks/benchmark.py", benchmark_source)
            write_file(repository, "benchmarks/handoff.py", handoff_source)
            commit_all(repository, "attest deterministic fixture-swap probe")
            with mock.patch.object(core, "ROOT", repository):
                attestation = core.git_revision_attestation(
                    LAUNCHER_REQUIRED,
                    require_clean=True,
                )
            self.publish(repository, attestation)

            completed = self.run_launcher(repository)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(fixture.read_bytes(), b"trusted fixture\n")

    def test_artifact_descendant_and_readme_worktree_changes_are_valid(self) -> None:
        with self.repository() as (repository, attestation):
            self.publish(repository, attestation)
            (repository / "README.md").write_text(
                "updated root claims\n", encoding="utf-8"
            )
            current = self.run_launcher(repository)
            self.assertEqual(current.returncode, 0, current.stderr)
            commit_all(repository, "publish Capsule artifacts and claims")
            descendant = self.run_launcher(repository)
            self.assertEqual(descendant.returncode, 0, descendant.stderr)

    def test_lone_duplicate_extra_and_unsafe_artifacts_fail(self) -> None:
        with self.repository() as (repository, _):
            write_file(repository, "CAPSULE_BENCHMARK.md", "report\n")
            self.assertNotEqual(self.run_launcher(repository).returncode, 0)
        with self.repository() as (repository, attestation):
            directory = self.publish(repository, attestation)
            (directory / "CAPSULE.md").unlink()
            self.assertNotEqual(self.run_launcher(repository).returncode, 0)
        with self.repository() as (repository, attestation):
            self.publish(repository, attestation)
            second = self.publish(repository, attestation, "second-run")
            self.assertTrue(second.is_dir())
            self.assertNotEqual(self.run_launcher(repository).returncode, 0)
        with self.repository() as (repository, attestation):
            directory = self.publish(repository, attestation)
            (directory / "settings.ini").write_text("unsafe=true\n", encoding="utf-8")
            self.assertNotEqual(self.run_launcher(repository).returncode, 0)
        with self.repository() as (repository, attestation):
            self.publish(repository, attestation, "Unsafe-Run")
            self.assertNotEqual(self.run_launcher(repository).returncode, 0)

    def test_committed_json_shadow_exploit_does_not_return_success(self) -> None:
        for relative in ("benchmarks/json.py", "json.py"):
            with self.subTest(path=relative), self.repository() as (
                repository,
                attestation,
            ):
                self.publish(repository, attestation)
                write_file(repository, relative, "import os\nos._exit(0)\n")
                commit_all(repository, "attempt import-shadow bypass")
                exploited = self.run_launcher(repository)
                self.assertNotEqual(exploited.returncode, 0)
                self.assertIn("shadow the standard library", exploited.stderr)

    def test_ignored_sourceless_bytecode_fails_preflight(self) -> None:
        with self.repository() as (repository, attestation):
            self.publish(repository, attestation)
            (repository / "json.pyc").write_bytes(b"sourceless")
            cache = repository / "benchmarks" / "__pycache__"
            cache.mkdir()
            (cache / "json.cpython-312.pyc").write_bytes(b"sourceless")
            failed = self.run_launcher(repository)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("Capsule release validation failed", failed.stderr)

    def test_symlink_mode_and_launcher_changes_fail_preflight(self) -> None:
        with self.repository() as (repository, attestation):
            directory = self.publish(repository, attestation)
            nested = directory / "CAPSULE.md"
            nested.unlink()
            nested.symlink_to("capsule-r3.json")
            self.assertNotEqual(self.run_launcher(repository).returncode, 0)
        if hasattr(os, "mkfifo"):
            with self.repository() as (repository, attestation):
                directory = self.publish(repository, attestation)
                result = directory / "capsule-r3.json"
                result.unlink()
                os.mkfifo(result)
                self.assertNotEqual(self.run_launcher(repository).returncode, 0)
        for mutation in ("mode", "content"):
            with self.subTest(mutation=mutation), self.repository() as (
                repository,
                attestation,
            ):
                self.publish(repository, attestation)
                launcher = repository / "benchmarks" / "validate_capsule_release.py"
                if mutation == "mode":
                    launcher.chmod(0o755)
                else:
                    launcher.write_text(
                        "raise SystemExit(0)\n",
                        encoding="utf-8",
                    )
                self.assertNotEqual(self.run_launcher(repository).returncode, 0)

    def test_isolated_flag_ignores_pythonpath_and_is_mandatory(self) -> None:
        with self.repository() as (repository, _), tempfile.TemporaryDirectory(
            prefix="capsule-shadow-path-"
        ) as shadow_directory:
            shadow = Path(shadow_directory)
            marker = shadow / "imported"
            (shadow / "json.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')\n",
                encoding="utf-8",
            )
            environment = {
                **os.environ,
                "PYTHONPATH": str(shadow),
            }
            isolated = self.run_launcher(repository, environment=environment)
            self.assertEqual(isolated.returncode, 0, isolated.stderr)
            self.assertFalse(marker.exists())
            nonisolated = self.run_launcher(
                repository,
                isolated=False,
                environment=environment,
            )
            self.assertEqual(nonisolated.returncode, 2)
            self.assertIn("requires isolated mode", nonisolated.stderr)

    def test_direct_live_launcher_execution_is_rejected(self) -> None:
        with self.repository() as (repository, _):
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(repository / "benchmarks" / "validate_capsule_release.py"),
                ],
                cwd=repository,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("attested Git-blob bootstrap", completed.stderr)

    def test_old_direct_entrypoint_fails_before_json_shadow_import(self) -> None:
        with tempfile.TemporaryDirectory(prefix="capsule-old-entrypoint-") as directory:
            repository = Path(directory)
            benchmarks = repository / "benchmarks"
            benchmarks.mkdir()
            shutil.copy2(HANDOFF, benchmarks / "handoff.py")
            (benchmarks / "json.py").write_text(
                "import os\nos._exit(0)\n", encoding="utf-8"
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(benchmarks / "handoff.py"),
                    "validate-release",
                    "result.json",
                    "report.md",
                ],
                cwd=repository,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("requires the attested Git-blob bootstrap", completed.stderr)


if __name__ == "__main__":
    unittest.main()
