from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
CAPSULE_SCRIPT = (
    ROOT
    / "skills"
    / "semantic-spec-writer"
    / "scripts"
    / "context_capsule.py"
)
FIXTURE = ROOT / "benchmarks" / "handoff-cases" / "tenant-settings"
EXPECTED_CAPSULE_SHA256 = (
    "e8add8cf8a8d49b02354da9690b8b0a2ce154d57dff3a1661b16fcac199cef7b"
)
FIXTURE_CAPSULE_SHA256 = {
    "refund-ledger": "97a6efbfa2f23fed9035026d09e439732b9d9ae9f95e9c1c03c63757b751f816",
    "tenant-settings": "e8add8cf8a8d49b02354da9690b8b0a2ce154d57dff3a1661b16fcac199cef7b",
    "webhook-dispatch": "ff0dbc4a8ff475270b2ac1165a95e3f8c44cad328341f79e8c60fbee57b132b7",
}


def load_capsule_module(name: str):
    spec = importlib.util.spec_from_file_location(name, CAPSULE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {CAPSULE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(os.name == "posix", "secure Capsule I/O is POSIX-only")
class CapsuleSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="capsule-security-")
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.packet = self.root / "packet.spec.ctx"
        shutil.copytree(FIXTURE / "starter", self.repo)
        self.repo.chmod(0o700)
        shutil.copy2(FIXTURE / "packet.spec.ctx", self.packet)
        self.capsule = load_capsule_module(f"capsule_security_{id(self)}")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _reseal_capsule_for_packet(self, capsule: bytes, packet_text: str) -> bytes:
        """Return a structurally valid capsule bound to ``packet_text`` for checks."""

        header, _, _, sources, _ = self.capsule._parse_capsule(capsule)
        header = dict(header)
        packet_bytes = packet_text.encode("utf-8")
        header["packet_sha256"] = hashlib.sha256(packet_bytes).hexdigest()
        embedded_packet = self.capsule._embedded_packet(packet_text).encode("utf-8")
        packet_descriptor = self.capsule._packet_descriptor(embedded_packet)
        body = bytearray(self.capsule.MAGIC)
        body.extend(self.capsule.EXECUTE_LINE)
        body.extend(
            self.capsule.HEADER_PREFIX
            + self.capsule._canonical_json(header)
            + b"\n"
        )
        body.extend(self.capsule._frame(packet_descriptor, embedded_packet))
        for descriptor, payload in sources:
            body.extend(self.capsule._frame(descriptor, payload))
        body.extend(self.capsule._seal_line(self.capsule._sha256(body)))
        return bytes(body)

    def _legacy_capsule(self) -> bytes:
        current = self.capsule.build_capsule(self.repo, self.packet)
        header, _, embedded, sources, _ = self.capsule._parse_capsule(current)
        header = dict(header)
        header["version"] = self.capsule.LEGACY_CAPSULE_VERSION
        header["protocol"] = self.capsule.LEGACY_CAPSULE_PROTOCOL
        legacy_packet = self.capsule._rewrite_execution_policy(
            embedded.decode("utf-8"),
            self.capsule.CAPSULE_EXECUTION,
            self.capsule.LEGACY_CAPSULE_EXECUTION,
        ).encode("utf-8")
        body = bytearray(self.capsule.LEGACY_MAGIC)
        body.extend(
            self.capsule.HEADER_PREFIX
            + self.capsule._canonical_json(header)
            + b"\n"
        )
        body.extend(
            self.capsule._frame(
                self.capsule._packet_descriptor(
                    legacy_packet,
                    self.capsule.LEGACY_CAPSULE_VERSION,
                ),
                legacy_packet,
            )
        )
        for descriptor, payload in sources:
            legacy_descriptor = dict(descriptor)
            legacy_descriptor.pop("role")
            body.extend(self.capsule._frame(legacy_descriptor, payload))
        body.extend(self.capsule._seal_line(self.capsule._sha256(body)))
        return bytes(body)

    def test_stable_build_check_and_publication_are_deterministic(self) -> None:
        first = self.capsule.build_capsule(self.repo, self.packet)
        second = self.capsule.build_capsule(self.repo, self.packet)
        self.assertEqual(first, second)
        self.assertEqual(hashlib.sha256(first).hexdigest(), EXPECTED_CAPSULE_SHA256)

        checked = self.capsule.check_capsule(
            self.repo,
            first,
            packet=self.packet,
        )
        self.assertTrue(checked["valid"], checked)
        self.assertTrue(checked["packet_bound"])

        output = self.root / "task.capsule"
        built = subprocess.run(
            [
                sys.executable,
                str(CAPSULE_SCRIPT),
                "build",
                str(self.repo),
                str(self.packet),
                str(output),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertEqual(output.read_bytes(), first)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        output.chmod(0o644)

        forced = subprocess.run(
            [
                sys.executable,
                str(CAPSULE_SCRIPT),
                "build",
                str(self.repo),
                str(self.packet),
                str(output),
                "--force",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual(output.read_bytes(), first)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.root.glob(".*.capsule-stage")), [])

    def test_legacy_v4_capsule_remains_readable_for_migration(self) -> None:
        legacy = self._legacy_capsule()
        self.assertTrue(legacy.startswith(self.capsule.LEGACY_MAGIC))
        self.assertNotIn(self.capsule.EXECUTE_LINE, legacy)

        checked = self.capsule.check_capsule(
            self.repo,
            legacy,
            packet=self.packet,
        )
        self.assertTrue(checked["valid"], checked)
        self.assertEqual(checked["version"], 4)

        corrupted = legacy[:-1] + bytes([legacy[-1] ^ 1])
        rejected = self.capsule.check_capsule(
            self.repo,
            corrupted,
            packet=self.packet,
        )
        self.assertFalse(rejected["valid"], rejected)
        self.assertEqual(rejected["version"], 4)

    def test_fixture_capsules_keep_exact_bytes(self) -> None:
        cases = ROOT / "benchmarks" / "handoff-cases"
        for name, expected_sha256 in FIXTURE_CAPSULE_SHA256.items():
            with self.subTest(name=name):
                capsule = self.capsule.build_capsule(
                    cases / name / "starter",
                    cases / name / "packet.spec.ctx",
                )
                self.assertEqual(
                    hashlib.sha256(capsule).hexdigest(),
                    expected_sha256,
                )

    def test_aggregate_capsule_limit_is_exact_and_bounds_every_input_form(self) -> None:
        capsule = self.capsule.build_capsule(self.repo, self.packet)
        output = self.root / "limit.capsule"
        output.write_bytes(capsule)
        maximum = len(capsule)

        # Patch the production cap down to this small fixture to exercise the
        # inclusive boundary without allocating a real 128 MiB artifact.
        with mock.patch.object(self.capsule, "MAX_CAPSULE_BYTES", maximum):
            self.assertEqual(
                self.capsule.build_capsule(self.repo, self.packet),
                capsule,
            )
            self.assertTrue(
                self.capsule.check_capsule(
                    self.repo,
                    output,
                    packet=self.packet,
                )["valid"]
            )
            for value in (capsule, bytearray(capsule), memoryview(capsule)):
                with self.subTest(input_type=type(value).__name__):
                    self.assertTrue(
                        self.capsule.check_capsule(
                            self.repo,
                            value,
                            packet=self.packet,
                        )["valid"]
                    )

        with mock.patch.object(self.capsule, "MAX_CAPSULE_BYTES", maximum - 1):
            with mock.patch.object(
                self.capsule,
                "_frame",
                wraps=self.capsule._frame,
            ) as frame:
                with self.assertRaisesRegex(
                    self.capsule.CapsuleError,
                    "aggregate Capsule limit",
                ):
                    self.capsule.build_capsule(self.repo, self.packet)
            self.assertEqual(frame.call_count, 0)

            path_result = self.capsule.check_capsule(
                self.repo,
                output,
                packet=self.packet,
            )
            self.assertFalse(path_result["valid"], path_result)
            for value in (capsule, bytearray(capsule), memoryview(capsule)):
                with self.subTest(oversized_input_type=type(value).__name__):
                    result = self.capsule.check_capsule(
                        self.repo,
                        value,
                        packet=self.packet,
                    )
                    self.assertFalse(result["valid"], result)
                    self.assertIn(
                        "aggregate Capsule limit",
                        "\n".join(result["errors"]),
                    )

    @unittest.skipUnless(sys.platform.startswith("linux"), "renameat2 is Linux-only")
    def test_named_stage_publication_works_after_dropping_root_privileges(self) -> None:
        with tempfile.TemporaryDirectory(prefix="capsule-unprivileged-") as directory:
            work = Path(directory)
            scripts = work / "scripts"
            scripts.mkdir()
            copied_capsule = scripts / "context_capsule.py"
            copied_packet = scripts / "check_execution_packet.py"
            shutil.copy2(CAPSULE_SCRIPT, copied_capsule)
            shutil.copy2(
                CAPSULE_SCRIPT.with_name("check_execution_packet.py"),
                copied_packet,
            )
            repo = work / "repo"
            packet = work / "packet.spec.ctx"
            shutil.copytree(self.repo, repo)
            shutil.copy2(self.packet, packet)
            output = work / "task.capsule"

            run_options: dict[str, object] = {}
            expected_uid = os.geteuid()
            if os.geteuid() == 0:
                try:
                    account = pwd.getpwnam("nobody")
                except KeyError:
                    self.skipTest("no unprivileged nobody account is available")
                expected_uid = account.pw_uid
                for path in [work, *work.rglob("*")]:
                    os.chown(path, account.pw_uid, account.pw_gid)

                def drop_privileges() -> None:
                    os.setgroups([])
                    os.setgid(account.pw_gid)
                    os.setuid(account.pw_uid)

                run_options["preexec_fn"] = drop_privileges

            command = [
                sys.executable,
                os.fspath(copied_capsule),
                "build",
                os.fspath(repo),
                os.fspath(packet),
                os.fspath(output),
            ]
            built = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                **run_options,
            )
            self.assertEqual(built.returncode, 0, built.stderr or built.stdout)
            forced = subprocess.run(
                [*command, "--force"],
                check=False,
                capture_output=True,
                text=True,
                **run_options,
            )
            self.assertEqual(forced.returncode, 0, forced.stderr or forced.stdout)
            metadata = output.stat()
            self.assertEqual(metadata.st_uid, expected_uid)
            self.assertEqual(metadata.st_mode & 0o777, 0o600)
            self.assertEqual(list(work.glob(".*.capsule-stage")), [])

    def test_missing_trusted_packet_never_returns_valid(self) -> None:
        capsule = self.capsule.build_capsule(self.repo, self.packet)
        unbound = self.capsule.check_capsule(self.repo, capsule)
        self.assertFalse(unbound["valid"])
        self.assertIn("trusted packet", "\n".join(unbound["errors"]))

        output = self.root / "task.capsule"
        output.write_bytes(capsule)
        command = subprocess.run(
            [
                sys.executable,
                str(CAPSULE_SCRIPT),
                "check",
                str(self.repo),
                str(output),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(command.returncode, 2)
        self.assertIn("--packet", command.stderr)

    def test_attacker_created_resealed_capsule_fails_trusted_binding(self) -> None:
        attacker_packet = self.root / "attacker.spec.ctx"
        attacker_packet.write_text(
            self.packet.read_text(encoding="utf-8").replace(
                "goal: strict typed setting precedence",
                "goal: attacker-controlled setting precedence",
            ),
            encoding="utf-8",
        )
        attacker_capsule = self.capsule.build_capsule(
            self.repo,
            attacker_packet,
        )
        self.assertTrue(
            self.capsule.check_capsule(
                self.repo,
                attacker_capsule,
                packet=attacker_packet,
            )["valid"]
        )
        rejected = self.capsule.check_capsule(
            self.repo,
            attacker_capsule,
            packet=self.packet,
        )
        self.assertFalse(rejected["valid"])
        self.assertIn("trusted packet", "\n".join(rejected["errors"]))

    def test_transient_external_symlink_swap_cannot_frame_or_validate(self) -> None:
        routed = self.repo / "settings" / "layers.py"
        parked = routed.with_suffix(".py.pinned")
        external = self.root / "external-secret.py"
        secret = b"EXTERNAL_CAPSULE_SECRET = True\n"
        external.write_bytes(secret)
        real_read = self.capsule.packet_checker._read_stable_regular_fd
        framed_payloads: list[bytes] = []
        swapped = False

        def raced_read(descriptor: int, label: str) -> bytes:
            nonlocal swapped
            if label == "routed file settings/layers.py" and not swapped:
                swapped = True
                routed.rename(parked)
                routed.symlink_to(external)
                try:
                    return real_read(descriptor, label)
                finally:
                    routed.unlink()
                    parked.rename(routed)
            return real_read(descriptor, label)

        real_frame = self.capsule._frame

        def recording_frame(descriptor: dict[str, object], payload: bytes) -> bytes:
            framed_payloads.append(payload)
            return real_frame(descriptor, payload)

        with (
            mock.patch.object(
                self.capsule.packet_checker,
                "_read_stable_regular_fd",
                side_effect=raced_read,
            ),
            mock.patch.object(
                self.capsule,
                "_frame",
                side_effect=recording_frame,
            ),
        ):
            with self.assertRaisesRegex(
                self.capsule.CapsuleError,
                "changed while (building capsule|being read)",
            ):
                self.capsule.build_capsule(self.repo, self.packet)

        self.assertTrue(swapped)
        self.assertTrue(all(secret not in payload for payload in framed_payloads))

    def _create_only_packet(self, target_name: str) -> Path:
        packet = self.root / "create.spec.ctx"
        text = (
            "spec\n"
            "route:\n"
            f"  create: {target_name}\n"
            "    do: create the generated file\n"
            "execution: routed read once -> all do -> V1 once -> stop on pass; "
            "expand only on contradiction/failure\n"
            f"basis: route-sha256:{'0' * 64}\n"
            "verify:\n"
            "  V1: `python3 -m py_compile generated.py`\n"
        )
        targets = self.capsule.packet_checker.parse_routes(text)
        route_hash = self.capsule.packet_checker.route_sha256(self.repo, targets)
        packet.write_text(text.replace("0" * 64, route_hash), encoding="utf-8")
        return packet

    def test_create_route_occupation_race_fails_build_and_check(self) -> None:
        target = self.repo / "generated.py"
        packet = self._create_only_packet(target.name)
        real_embedded = self.capsule._embedded_packet

        def occupy_during_build(text: str) -> str:
            target.write_text("ATTACKER = True\n", encoding="utf-8")
            return real_embedded(text)

        with mock.patch.object(
            self.capsule,
            "_embedded_packet",
            side_effect=occupy_during_build,
        ):
            with self.assertRaisesRegex(
                self.capsule.CapsuleError,
                "changed while building capsule",
            ):
                self.capsule.build_capsule(self.repo, packet)
        self.assertTrue(target.exists())

        target.unlink()
        capsule = self.capsule.build_capsule(self.repo, packet)
        real_budget = self.capsule._validate_token_budget

        def occupy_during_check(*args, **kwargs):
            target.write_text("ATTACKER = True\n", encoding="utf-8")
            return real_budget(*args, **kwargs)

        with mock.patch.object(
            self.capsule,
            "_validate_token_budget",
            side_effect=occupy_during_check,
        ):
            checked = self.capsule.check_capsule(
                self.repo,
                capsule,
                packet=packet,
            )
        self.assertFalse(checked["valid"], checked)
        self.assertTrue(target.exists())

    def test_output_rejects_double_slash_alias_of_missing_create_route(self) -> None:
        packet = self._create_only_packet("generated.py")
        double_slash_repo = "//" + os.fspath(self.repo).lstrip("/")
        _, _, references = self.capsule._build_capsule(
            double_slash_repo,
            packet,
        )
        try:
            create_reference = next(
                reference
                for reference in references
                if reference.identity is None
            )
            parent_stat = self.repo.stat()
            self.assertEqual(
                create_reference.parent_identity,
                (parent_stat.st_dev, parent_stat.st_ino),
            )
            self.assertEqual(create_reference.leaf, "generated.py")
            with self.assertRaisesRegex(
                self.capsule.CapsuleError,
                "output path aliases input file",
            ):
                self.capsule._check_output_path(
                    self.repo / "generated.py",
                    references,
                )
        finally:
            self.capsule._close_input_references(references)

    def test_output_rejects_missing_create_ancestor_before_publication(self) -> None:
        packet = self._create_only_packet("generated/child.py")
        output = self.repo / "generated"
        result = subprocess.run(
            [
                sys.executable,
                str(CAPSULE_SCRIPT),
                "build",
                str(self.repo),
                str(packet),
                str(output),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        metrics = json.loads(result.stdout)
        self.assertFalse(metrics["valid"])
        self.assertIn("output path aliases input file", "\n".join(metrics["errors"]))
        self.assertFalse(output.exists())
        self.assertEqual(list(self.repo.glob(".generated.*.capsule-stage")), [])

    def test_parent_swap_during_publication_cannot_redirect_output(self) -> None:
        data, _, references = self.capsule._build_capsule(self.repo, self.packet)
        output_parent = self.root / "publish"
        pinned_parent = self.root / "publish-pinned"
        external = self.root / "external"
        output_parent.mkdir(mode=0o700)
        external.mkdir(mode=0o700)
        target = self.capsule._check_output_path(
            output_parent / "task.capsule",
            references,
        )
        original_revalidate = target.revalidate_parent
        swapped = False

        def race_parent() -> None:
            nonlocal swapped
            if not swapped:
                swapped = True
                output_parent.rename(pinned_parent)
                output_parent.symlink_to(external, target_is_directory=True)
            original_revalidate()

        target.revalidate_parent = race_parent
        try:
            with self.assertRaisesRegex(
                self.capsule.CapsuleError,
                "output parent .*changed",
            ):
                self.capsule._write_atomic(target, data, False)
            self.assertFalse((external / "task.capsule").exists())
            self.assertFalse((pinned_parent / "task.capsule").exists())
        finally:
            target.close()
            self.capsule._close_input_references(references)
            if output_parent.is_symlink():
                output_parent.unlink()
            if pinned_parent.exists():
                pinned_parent.rename(output_parent)

    def test_world_writable_output_parent_is_rejected(self) -> None:
        output_parent = self.root / "shared-publish"
        output_parent.mkdir()
        output_parent.chmod(0o777)
        _, _, references = self.capsule._build_capsule(self.repo, self.packet)
        try:
            with self.assertRaisesRegex(
                self.capsule.CapsuleError,
                "world-writable",
            ):
                self.capsule._check_output_path(
                    output_parent / "task.capsule",
                    references,
                )
        finally:
            output_parent.chmod(0o700)
            self.capsule._close_input_references(references)

    def test_force_rejects_arbitrary_file_without_modifying_it(self) -> None:
        output = self.root / "arbitrary.txt"
        original = b"not a Capsule artifact\n"
        output.write_bytes(original)
        result = subprocess.run(
            [
                sys.executable,
                str(CAPSULE_SCRIPT),
                "build",
                str(self.repo),
                str(self.packet),
                str(output),
                "--force",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        metrics = json.loads(result.stdout)
        self.assertFalse(metrics["valid"])
        self.assertEqual(output.read_bytes(), original)

    def test_packet_execution_whitespace_round_trips_deterministically(self) -> None:
        packet = self.root / "whitespace.spec.ctx"
        bounded = self.capsule.packet_checker.BOUNDED_EXECUTION
        original_line = f"execution: {bounded}\n"
        whitespace_line = f"execution:\t  {bounded}\t  \n"
        text = self.packet.read_text(encoding="utf-8")
        self.assertIn(original_line, text)
        packet.write_text(
            text.replace(original_line, whitespace_line),
            encoding="utf-8",
        )

        first = self.capsule.build_capsule(self.repo, packet)
        second = self.capsule.build_capsule(self.repo, packet)
        self.assertEqual(first, second)
        _, _, embedded, _, _ = self.capsule._parse_capsule(first)
        embedded_text = embedded.decode("utf-8")
        self.assertEqual(
            self.capsule.packet_checker.parse_execution_policies(embedded_text),
            [self.capsule.CAPSULE_EXECUTION],
        )
        self.assertIn(
            f"execution:\t  {self.capsule.CAPSULE_EXECUTION}\t  \n",
            embedded_text,
        )
        checked = self.capsule.check_capsule(self.repo, first, packet=packet)
        self.assertTrue(checked["valid"], checked)

    def test_capsule_build_requires_exactly_one_v1_verification(self) -> None:
        original = self.packet.read_text(encoding="utf-8")
        v1_line = next(
            line for line in original.splitlines() if line.startswith("  V1:")
        )
        variants = {
            "v2-substitution": original.replace("  V1:", "  V2:", 1),
            "multiple-verifications": original.replace(
                f"{v1_line}\n",
                f"{v1_line}\n  V2: `python3 -m unittest -q test_smoke.py`\n",
                1,
            ),
        }

        for name, packet_text in variants.items():
            with self.subTest(name=name):
                packet = self.root / f"{name}.spec.ctx"
                packet.write_text(packet_text, encoding="utf-8")
                generic_result, generic_errors = (
                    self.capsule.packet_checker.validate_text(
                        self.repo,
                        packet_text,
                        None,
                    )
                )
                self.assertEqual(generic_errors, [], generic_result)
                with self.assertRaisesRegex(
                    self.capsule.CapsuleError,
                    "exactly one verification entry named V1",
                ):
                    self.capsule.build_capsule(self.repo, packet)

    def test_capsule_check_requires_exactly_one_v1_verification(self) -> None:
        original = self.packet.read_text(encoding="utf-8")
        capsule = self.capsule.build_capsule(self.repo, self.packet)
        v1_line = next(
            line for line in original.splitlines() if line.startswith("  V1:")
        )
        variants = {
            "v2-substitution": original.replace("  V1:", "  V2:", 1),
            "multiple-verifications": original.replace(
                f"{v1_line}\n",
                f"{v1_line}\n  V2: `python3 -m unittest -q test_smoke.py`\n",
                1,
            ),
        }

        for name, packet_text in variants.items():
            with self.subTest(name=name):
                packet = self.root / f"{name}.spec.ctx"
                packet.write_text(packet_text, encoding="utf-8")
                resealed = self._reseal_capsule_for_packet(capsule, packet_text)
                checked = self.capsule.check_capsule(
                    self.repo,
                    resealed,
                    packet=packet,
                )
                self.assertFalse(checked["valid"], checked)
                self.assertIn(
                    "exactly one verification entry named V1",
                    "\n".join(checked["errors"]),
                )

    def test_no_clobber_fails_if_leaf_is_replaced_after_link(self) -> None:
        output = self.root / "task.capsule"
        attacker = self.root / "attacker.capsule"
        attacker_bytes = b"attacker publication bytes\n"
        attacker.write_bytes(attacker_bytes)
        data, _, references = self.capsule._build_capsule(self.repo, self.packet)
        target = self.capsule._check_output_path(output, references)
        real_fsync = self.capsule.os.fsync
        substituted = False

        def substitute_after_link(descriptor: int) -> None:
            nonlocal substituted
            if descriptor == target.parent.fd and not substituted:
                substituted = True
                os.replace(attacker, output)
            real_fsync(descriptor)

        try:
            with mock.patch.object(
                self.capsule.os,
                "fsync",
                side_effect=substitute_after_link,
            ):
                with self.assertRaisesRegex(
                    self.capsule.CapsuleError,
                    "published Capsule output (changed|was substituted)",
                ):
                    self.capsule._write_atomic(target, data, False)
            self.assertTrue(substituted)
            self.assertEqual(output.read_bytes(), attacker_bytes)
        finally:
            target.close()
            self.capsule._close_input_references(references)

    def test_no_clobber_fails_if_linked_inode_is_modified_after_link(self) -> None:
        output = self.root / "task.capsule"
        data, _, references = self.capsule._build_capsule(self.repo, self.packet)
        target = self.capsule._check_output_path(output, references)
        real_fsync = self.capsule.os.fsync
        modified = False

        def modify_after_link(descriptor: int) -> None:
            nonlocal modified
            if descriptor == target.parent.fd and not modified:
                modified = True
                leaf_fd = os.open(
                    target.leaf,
                    os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=target.parent.fd,
                )
                try:
                    os.write(leaf_fd, b"ATTACKER")
                    real_fsync(leaf_fd)
                finally:
                    os.close(leaf_fd)
            real_fsync(descriptor)

        try:
            with mock.patch.object(
                self.capsule.os,
                "fsync",
                side_effect=modify_after_link,
            ):
                with self.assertRaisesRegex(
                    self.capsule.CapsuleError,
                    "published Capsule output changed",
                ):
                    self.capsule._write_atomic(target, data, False)
            self.assertTrue(modified)
            self.assertTrue(output.read_bytes().startswith(b"ATTACKER"))
        finally:
            target.close()
            self.capsule._close_input_references(references)

    def test_no_clobber_fails_if_linked_inode_mode_changes_after_link(self) -> None:
        output = self.root / "task.capsule"
        data, _, references = self.capsule._build_capsule(self.repo, self.packet)
        target = self.capsule._check_output_path(output, references)
        real_fsync = self.capsule.os.fsync
        modified = False

        def chmod_after_link(descriptor: int) -> None:
            nonlocal modified
            if descriptor == target.parent.fd and not modified:
                modified = True
                os.chmod(output, 0o644)
            real_fsync(descriptor)

        try:
            with mock.patch.object(
                self.capsule.os,
                "fsync",
                side_effect=chmod_after_link,
            ):
                with self.assertRaisesRegex(
                    self.capsule.CapsuleError,
                    "published Capsule output changed",
                ):
                    self.capsule._write_atomic(target, data, False)
            self.assertTrue(modified)
            self.assertEqual(output.stat().st_mode & 0o777, 0o644)
        finally:
            target.close()
            self.capsule._close_input_references(references)

    def test_open_output_regular_uses_nonblocking_open_for_fifo(self) -> None:
        output = self.root / "fifo.capsule"
        data, _, references = self.capsule._build_capsule(self.repo, self.packet)
        target = self.capsule._check_output_path(output, references)
        os.mkfifo(output)
        real_open = self.capsule.os.open
        observed_nonblocking = False

        def guarded_open(path, flags, *args, **kwargs):
            nonlocal observed_nonblocking
            if path == target.leaf and kwargs.get("dir_fd") == target.parent.fd:
                observed_nonblocking = True
                self.assertTrue(flags & os.O_NONBLOCK)
            return real_open(path, flags, *args, **kwargs)

        try:
            with mock.patch.object(self.capsule.os, "open", side_effect=guarded_open):
                with self.assertRaisesRegex(
                    self.capsule.CapsuleError,
                    "not a regular file",
                ):
                    self.capsule._open_output_regular(target)
            self.assertTrue(observed_nonblocking)
        finally:
            target.close()
            self.capsule._close_input_references(references)

    def test_force_rejects_in_place_update_between_compare_and_exchange(self) -> None:
        original = self.capsule.build_capsule(self.repo, self.packet)
        output = self.root / "existing.capsule"
        output.write_bytes(original)
        data, _, references = self.capsule._build_capsule(self.repo, self.packet)
        target = self.capsule._check_output_path(output, references)
        original_revalidate = target.revalidate_parent
        updated = False

        def update_before_exchange() -> None:
            nonlocal updated
            if not updated:
                updated = True
                descriptor = os.open(
                    target.leaf,
                    os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=target.parent.fd,
                )
                try:
                    os.write(descriptor, b"CONCURRENT")
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            original_revalidate()

        try:
            target.revalidate_parent = update_before_exchange
            with self.assertRaisesRegex(
                self.capsule.CapsuleError,
                "force target changed before publication",
            ):
                self.capsule._write_atomic(target, data, True)
            self.assertTrue(updated)
            self.assertTrue(output.read_bytes().startswith(b"CONCURRENT"))
        finally:
            target.close()
            self.capsule._close_input_references(references)

    def test_force_rejects_replacement_between_compare_and_exchange(self) -> None:
        original = self.capsule.build_capsule(self.repo, self.packet)
        output = self.root / "existing.capsule"
        output.write_bytes(original)
        replacement = self.root / "concurrent.capsule"
        replacement_bytes = b"concurrent replacement bytes\n"
        replacement.write_bytes(replacement_bytes)
        data, _, references = self.capsule._build_capsule(self.repo, self.packet)
        target = self.capsule._check_output_path(output, references)
        original_revalidate = target.revalidate_parent
        replaced = False

        def replace_before_exchange() -> None:
            nonlocal replaced
            if not replaced:
                replaced = True
                os.replace(replacement, output)
            original_revalidate()

        try:
            target.revalidate_parent = replace_before_exchange
            with self.assertRaisesRegex(
                self.capsule.CapsuleError,
                "force target changed before publication",
            ):
                self.capsule._write_atomic(target, data, True)
            self.assertTrue(replaced)
            self.assertEqual(output.read_bytes(), replacement_bytes)
        finally:
            target.close()
            self.capsule._close_input_references(references)

    def test_force_reports_post_validation_exchange_gap_and_keeps_recovery_bytes(self) -> None:
        for kind in ("in-place", "replacement"):
            with self.subTest(kind=kind):
                original = self.capsule.build_capsule(self.repo, self.packet)
                output = self.root / f"{kind}.capsule"
                output.write_bytes(original)
                replacement = self.root / f"{kind}-concurrent.capsule"
                replacement_bytes = b"concurrent bytes in exchange gap\n"
                if kind == "replacement":
                    replacement.write_bytes(replacement_bytes)
                data, _, references = self.capsule._build_capsule(
                    self.repo,
                    self.packet,
                )
                target = self.capsule._check_output_path(output, references)
                real_exchange = self.capsule._rename_exchange_syscall
                raced = False

                def update_after_validation(*args) -> None:
                    nonlocal raced
                    if not raced:
                        raced = True
                        if kind == "in-place":
                            descriptor = os.open(
                                target.leaf,
                                os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                                dir_fd=target.parent.fd,
                            )
                            try:
                                os.ftruncate(descriptor, 0)
                                os.write(descriptor, replacement_bytes)
                                os.fsync(descriptor)
                            finally:
                                os.close(descriptor)
                        else:
                            os.replace(replacement, output)
                    real_exchange(*args)

                try:
                    with mock.patch.object(
                        self.capsule,
                        "_rename_exchange_syscall",
                        side_effect=update_after_validation,
                    ):
                        with self.assertRaisesRegex(
                            self.capsule.CapsuleError,
                            "force publication became uncertain; retained exchanged "
                            "target entry in",
                        ):
                            self.capsule._write_atomic(target, data, True)
                    self.assertTrue(raced)
                    # The atomic exchange may already have published the new
                    # Capsule. Failure means the exchange could not be proved
                    # safe, not that it was rolled back.
                    self.assertEqual(output.read_bytes(), data)
                    stages = list(self.root.glob(f".{kind}.capsule.*.capsule-stage"))
                    self.assertEqual(len(stages), 1)
                    self.assertEqual(
                        (stages[0] / "capsule").read_bytes(),
                        replacement_bytes,
                    )
                finally:
                    target.close()
                    self.capsule._close_input_references(references)

    def test_force_post_cleanup_writer_race_has_no_recovery_entry(self) -> None:
        original = self.capsule.build_capsule(self.repo, self.packet)
        output = self.root / "existing.capsule"
        output.write_bytes(original)
        replacement = self.root / "post-cleanup-replacement.capsule"
        replacement_bytes = b"post-cleanup concurrent bytes\n"
        replacement.write_bytes(replacement_bytes)
        data, _, references = self.capsule._build_capsule(self.repo, self.packet)
        target = self.capsule._check_output_path(output, references)
        real_cleanup = self.capsule._remove_empty_private_stage
        raced = False

        def replace_after_cleanup(*args) -> None:
            nonlocal raced
            real_cleanup(*args)
            if not raced:
                raced = True
                self.assertFalse((self.root / args[1].name).exists())
                os.replace(replacement, output)

        try:
            with mock.patch.object(
                self.capsule,
                "_remove_empty_private_stage",
                side_effect=replace_after_cleanup,
            ):
                with self.assertRaisesRegex(
                    self.capsule.CapsuleError,
                    "forced Capsule output "
                    "(changed|was substituted) during publication",
                ):
                    self.capsule._write_atomic(target, data, True)
            self.assertTrue(raced)
            self.assertEqual(output.read_bytes(), replacement_bytes)
            self.assertEqual(
                list(self.root.glob(".existing.capsule.*.capsule-stage/capsule")),
                [],
            )
        finally:
            target.close()
            self.capsule._close_input_references(references)

    def test_force_failure_after_exchange_keeps_in_place_public_update(self) -> None:
        original = self.capsule.build_capsule(self.repo, self.packet)
        output = self.root / "existing.capsule"
        output.write_bytes(original)
        data, _, references = self.capsule._build_capsule(self.repo, self.packet)
        target = self.capsule._check_output_path(output, references)
        original_revalidate = target.revalidate_parent
        calls = 0

        def update_after_exchange() -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                descriptor = os.open(
                    target.leaf,
                    os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=target.parent.fd,
                )
                try:
                    os.write(descriptor, b"NEWER")
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                raise self.capsule.CapsuleError("injected post-exchange failure")
            original_revalidate()

        try:
            target.revalidate_parent = update_after_exchange
            with self.assertRaisesRegex(
                self.capsule.CapsuleError,
                "injected post-exchange failure",
            ):
                self.capsule._write_atomic(target, data, True)
            self.assertTrue(output.read_bytes().startswith(b"NEWER"))
        finally:
            target.close()
            self.capsule._close_input_references(references)

    def test_force_failure_after_exchange_keeps_replacement_public_update(self) -> None:
        original = self.capsule.build_capsule(self.repo, self.packet)
        output = self.root / "existing.capsule"
        output.write_bytes(original)
        replacement = self.root / "newer.capsule"
        replacement_bytes = b"newer replacement bytes\n"
        replacement.write_bytes(replacement_bytes)
        data, _, references = self.capsule._build_capsule(self.repo, self.packet)
        target = self.capsule._check_output_path(output, references)
        original_revalidate = target.revalidate_parent
        calls = 0

        def replace_after_exchange() -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                os.replace(replacement, output)
                raise self.capsule.CapsuleError("injected post-exchange failure")
            original_revalidate()

        try:
            target.revalidate_parent = replace_after_exchange
            with self.assertRaisesRegex(
                self.capsule.CapsuleError,
                "injected post-exchange failure",
            ):
                self.capsule._write_atomic(target, data, True)
            self.assertEqual(output.read_bytes(), replacement_bytes)
        finally:
            target.close()
            self.capsule._close_input_references(references)

    def test_force_failure_never_promotes_modified_staged_bytes(self) -> None:
        original = self.capsule.build_capsule(self.repo, self.packet)
        output = self.root / "existing.capsule"
        output.write_bytes(original)
        data, _, references = self.capsule._build_capsule(self.repo, self.packet)
        target = self.capsule._check_output_path(output, references)
        original_revalidate = target.revalidate_parent
        calls = 0
        attacker_bytes = b"ATTACKER-STAGED"

        def attack_stage_after_exchange() -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                stages = list(self.root.glob(".*.capsule-stage"))
                self.assertEqual(len(stages), 1)
                descriptor = os.open(
                    stages[0] / "capsule",
                    os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                )
                try:
                    os.write(descriptor, attacker_bytes)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                raise self.capsule.CapsuleError("injected staged-byte failure")
            original_revalidate()

        try:
            target.revalidate_parent = attack_stage_after_exchange
            with self.assertRaisesRegex(
                self.capsule.CapsuleError,
                "injected staged-byte failure",
            ):
                self.capsule._write_atomic(target, data, True)
            self.assertEqual(output.read_bytes(), data)
            stages = list(self.root.glob(".*.capsule-stage"))
            self.assertEqual(len(stages), 1)
            self.assertTrue((stages[0] / "capsule").read_bytes().startswith(attacker_bytes))
        finally:
            target.close()
            self.capsule._close_input_references(references)


if __name__ == "__main__":
    unittest.main()
