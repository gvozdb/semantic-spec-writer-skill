from __future__ import annotations

import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
BENCHMARKS = ROOT / "benchmarks"
sys.path.insert(0, os.fspath(BENCHMARKS))
import benchmark as core  # noqa: E402


class CheckpointSecurityTest(unittest.TestCase):
    def test_report_publication_rejects_world_writable_parent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="checkpoint-shared-parent-") as directory:
            parent = Path(directory) / "shared"
            parent.mkdir()
            parent.chmod(0o777)
            try:
                with self.assertRaisesRegex(RuntimeError, "world-writable"):
                    core.write_text_atomic(
                        parent / "report.md",
                        "safe\n",
                        overwrite=False,
                    )
            finally:
                parent.chmod(0o700)

    def test_publication_works_without_root_capabilities(self) -> None:
        with tempfile.TemporaryDirectory(prefix="checkpoint-unprivileged-") as directory:
            work = Path(directory)
            scripts = work / "scripts"
            scripts.mkdir()
            copied = scripts / "benchmark.py"
            shutil.copy2(BENCHMARKS / "benchmark.py", copied)
            output = work / "report.md"

            run_options: dict[str, object] = {}
            if os.geteuid() == 0:
                try:
                    account = pwd.getpwnam("nobody")
                except KeyError:
                    self.skipTest("no unprivileged nobody account is available")
                for path in (work, scripts, copied):
                    os.chown(path, account.pw_uid, account.pw_gid)

                def drop_privileges() -> None:
                    os.setgroups([])
                    os.setgid(account.pw_gid)
                    os.setuid(account.pw_uid)

                run_options["preexec_fn"] = drop_privileges

            command = [
                sys.executable,
                "-c",
                (
                    "import sys; from pathlib import Path; "
                    "sys.path.insert(0, sys.argv[1]); import benchmark; "
                    "benchmark.write_text_atomic(Path(sys.argv[2]), 'safe\\n', "
                    "overwrite=False)"
                ),
                os.fspath(scripts),
                os.fspath(output),
            ]
            result = subprocess.run(
                command,
                cwd=work,
                check=False,
                capture_output=True,
                text=True,
                **run_options,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertEqual(output.read_text(encoding="utf-8"), "safe\n")
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_checkpoint_rejects_in_place_update_and_preserves_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="checkpoint-in-place-") as directory:
            output = Path(directory) / "result.json"
            checkpoint = core.open_result_checkpoint(output, {"value": 1}, force=False)
            try:
                attacker = b'{"attacker":true}\n'
                descriptor = os.open(output, os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC)
                try:
                    os.write(descriptor, attacker)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                with self.assertRaisesRegex(RuntimeError, "current result checkpoint.*changed"):
                    checkpoint.write_json({"value": 2})
                self.assertEqual(output.read_bytes(), attacker)
            finally:
                checkpoint.close()

    def test_checkpoint_rejects_replacement_and_preserves_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="checkpoint-replacement-") as directory:
            root = Path(directory)
            output = root / "result.json"
            replacement = root / "replacement.json"
            checkpoint = core.open_result_checkpoint(output, {"value": 1}, force=False)
            try:
                replacement_bytes = b'{"replacement":true}\n'
                replacement.write_bytes(replacement_bytes)
                os.replace(replacement, output)
                with self.assertRaisesRegex(RuntimeError, "current result checkpoint.*changed"):
                    checkpoint.write_json({"value": 2})
                self.assertEqual(output.read_bytes(), replacement_bytes)
            finally:
                checkpoint.close()

    def test_checkpoint_parent_lease_rejects_concurrent_writer_without_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="checkpoint-lease-") as directory:
            root = Path(directory)
            output = root / "result.json"
            checkpoint = core.open_result_checkpoint(output, {"value": 1}, force=False)
            try:
                self.assertEqual({path.name for path in root.iterdir()}, {"result.json"})
                with self.assertRaisesRegex(RuntimeError, "directory is already leased"):
                    core.open_result_checkpoint(
                        root / "second.json",
                        {"value": 2},
                        force=False,
                    )
            finally:
                checkpoint.close()
            with core.open_result_checkpoint(
                root / "second.json",
                {"value": 2},
                force=False,
            ):
                pass
            self.assertEqual(
                {path.name for path in root.iterdir()},
                {"result.json", "second.json"},
            )

    def test_checkpoint_parent_swap_cannot_redirect_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="checkpoint-parent-") as directory:
            root = Path(directory)
            live = root / "live"
            parked = root / "parked"
            attacker = root / "attacker"
            live.mkdir(mode=0o700)
            attacker.mkdir(mode=0o700)
            output = live / "result.json"
            checkpoint = core.open_result_checkpoint(output, {"value": 1}, force=False)
            try:
                live.rename(parked)
                live.symlink_to(attacker, target_is_directory=True)
                with self.assertRaisesRegex(
                    RuntimeError,
                    "output parent.*(replaced|symlink|non-directory)",
                ):
                    checkpoint.write_json({"value": 2})
                self.assertFalse((attacker / "result.json").exists())
                self.assertEqual(
                    json.loads((parked / "result.json").read_text(encoding="utf-8")),
                    {"value": 1},
                )
            finally:
                checkpoint.close()

    def test_post_exchange_update_is_not_overwritten_by_recovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="checkpoint-exchange-") as directory:
            root = Path(directory)
            output = root / "result.json"
            checkpoint = core.open_result_checkpoint(output, {"value": 1}, force=False)
            real_exchange = core._rename_exchange_at
            raced = False

            def exchange_then_modify(*args) -> None:
                nonlocal raced
                real_exchange(*args)
                raced = True
                right_fd = args[3]
                right_name = args[4]
                descriptor = os.open(
                    right_name,
                    os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=right_fd,
                )
                try:
                    os.write(descriptor, b"newer public bytes\n")
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

            try:
                with mock.patch.object(
                    core,
                    "_rename_exchange_at",
                    side_effect=exchange_then_modify,
                ):
                    with self.assertRaisesRegex(RuntimeError, "publication changed"):
                        checkpoint.write_json({"value": 2})
                self.assertTrue(raced)
                self.assertEqual(output.read_bytes(), b"newer public bytes\n")
                recoveries = list(root.glob(".result.json.*.checkpoint-recovery"))
                self.assertEqual(len(recoveries), 1)
                self.assertEqual(stat.S_IMODE(recoveries[0].stat().st_mode), 0o700)
            finally:
                checkpoint.close()

    def test_historical_implementation_report_is_byte_identical(self) -> None:
        artifact = (
            BENCHMARKS
            / "results"
            / "published"
            / "gpt-5.6-terra-medium-20260901-generated"
            / "implementation-r3.json"
        )
        document = json.loads(artifact.read_text(encoding="utf-8"))
        self.assertEqual(core.render_report(document), (ROOT / "BENCHMARK.md").read_text())


if __name__ == "__main__":
    unittest.main()
