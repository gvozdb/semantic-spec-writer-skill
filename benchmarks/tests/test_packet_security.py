from __future__ import annotations

import gc
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
PACKET_CHECK = (
    ROOT
    / "skills"
    / "semantic-spec-writer"
    / "scripts"
    / "check_execution_packet.py"
)


def load_packet_module(name: str):
    spec = importlib.util.spec_from_file_location(name, PACKET_CHECK)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {PACKET_CHECK}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(os.name == "posix", "secure Packet I/O is POSIX-only")
class PacketSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.packet = load_packet_module(f"packet_security_{id(self)}")

    def test_conflicting_create_hierarchy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="packet-create-conflict-") as directory:
            repo = Path(directory)
            targets = [
                self.packet.Target("create", "generated", "generated", None, None, None),
                self.packet.Target(
                    "create",
                    "generated/child.py",
                    "generated/child.py",
                    None,
                    None,
                    None,
                ),
            ]
            with self.assertRaisesRegex(ValueError, "conflicting create route hierarchy"):
                self.packet.open_route_snapshot(repo, targets)

    def test_fifo_input_is_rejected_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory(prefix="packet-fifo-") as directory:
            fifo = Path(directory) / "packet.spec.ctx"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(ValueError, "not a regular file"):
                self.packet.secure_open_regular(fifo, "packet")

    def test_regular_input_above_memory_bound_is_rejected_before_read(self) -> None:
        with tempfile.TemporaryDirectory(prefix="packet-size-bound-") as directory:
            oversized = Path(directory) / "packet.spec.ctx"
            with oversized.open("wb") as output:
                output.truncate(self.packet.MAX_REGULAR_INPUT_BYTES + 1)
            with self.assertRaisesRegex(ValueError, "exceeds maximum"):
                self.packet.secure_open_regular(oversized, "packet")

    def test_directory_swap_between_open_and_baseline_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="packet-directory-race-") as directory:
            root = Path(directory)
            repo = root / "repo"
            parked = root / "repo-pinned"
            attacker = root / "attacker"
            repo.mkdir()
            attacker.mkdir()
            real_open = self.packet.os.open
            raced = False

            def raced_open(path, flags, *args, **kwargs):
                nonlocal raced
                descriptor = real_open(path, flags, *args, **kwargs)
                if (
                    path == repo.name
                    and kwargs.get("dir_fd") is not None
                    and not raced
                ):
                    raced = True
                    repo.rename(parked)
                    repo.symlink_to(attacker, target_is_directory=True)
                return descriptor

            try:
                with mock.patch.object(self.packet.os, "open", new=raced_open):
                    self.packet.os.supports_dir_fd.add(raced_open)
                    with self.assertRaisesRegex(
                        ValueError,
                        "path changed while it was being pinned",
                    ):
                        self.packet.secure_open_directory(repo, "repository")
                self.assertTrue(raced)
            finally:
                self.packet.os.supports_dir_fd.discard(raced_open)
                if repo.is_symlink():
                    repo.unlink()
                if parked.exists():
                    parked.rename(repo)

    def test_failing_encoder_does_not_leak_route_descriptors(self) -> None:
        proc_fd = Path("/proc/self/fd")
        if not proc_fd.is_dir():
            self.skipTest("descriptor accounting requires /proc/self/fd")

        class FailingEncoder:
            name = "failing"

            def encode(self, _text: str):
                raise RuntimeError("encoder failed")

        with tempfile.TemporaryDirectory(prefix="packet-encoder-fd-") as directory:
            repo = Path(directory)
            (repo / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
            text = (
                "spec\n"
                "route:\n"
                "  edit: service.py::VALUE = 1\n"
                "    do: update VALUE\n"
                "execution: routed read once -> all do -> V1 once -> stop on pass; "
                "expand only on contradiction/failure\n"
                f"basis: route-sha256:{'0' * 64}\n"
                "verify:\n"
                "  V1: `python3 -m py_compile service.py`\n"
            )
            before = len(list(proc_fd.iterdir()))
            for _ in range(20):
                with self.assertRaisesRegex(RuntimeError, "encoder failed"):
                    self.packet.validate_text(repo, text, FailingEncoder())
            gc.collect()
            after = len(list(proc_fd.iterdir()))
            self.assertLessEqual(after, before + 1)

    def test_generic_packet_keeps_multiple_verification_entries(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="packet-multiple-verifications-"
        ) as directory:
            repo = Path(directory)
            (repo / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
            text = (
                "spec\n"
                "route:\n"
                "  edit: service.py::VALUE = 1\n"
                "    do: update VALUE\n"
                "execution: routed read once -> all do -> V1 once -> stop on pass; "
                "expand only on contradiction/failure\n"
                f"basis: route-sha256:{'0' * 64}\n"
                "verify:\n"
                "  V1: `python3 -m py_compile service.py`\n"
                "  V2: `python3 -m unittest -q`\n"
            )
            targets = self.packet.parse_routes(text)
            route_hash = self.packet.route_sha256(repo, targets)
            text = text.replace("0" * 64, route_hash)

            result, errors = self.packet.validate_text(repo, text, None)

            self.assertEqual(errors, [], result)
            self.assertEqual(
                self.packet.parse_verify_entries(text),
                [
                    ("V1", "python3 -m py_compile service.py"),
                    ("V2", "python3 -m unittest -q"),
                ],
            )
            self.assertEqual(
                result["verify_commands"],
                ["python3 -m py_compile service.py", "python3 -m unittest -q"],
            )


if __name__ == "__main__":
    unittest.main()
