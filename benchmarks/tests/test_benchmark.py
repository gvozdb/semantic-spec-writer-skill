from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "benchmarks" / "benchmark.py"
LIFECYCLE = ROOT / "benchmarks" / "lifecycle.py"
CONTEXT = ROOT / "benchmarks" / "context.py"
HANDOFF = ROOT / "benchmarks" / "handoff.py"
sys.path.insert(0, str(ROOT / "benchmarks"))
import benchmark as benchmark_module
CONVERSION_CHECK = (
    ROOT
    / "skills"
    / "semantic-spec-writer"
    / "scripts"
    / "check_conversion.py"
)
PACKET_CHECK = (
    ROOT
    / "skills"
    / "semantic-spec-writer"
    / "scripts"
    / "check_execution_packet.py"
)
HANDOFF_CASES = ROOT / "benchmarks" / "handoff-cases"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BenchmarkCliTest(unittest.TestCase):
    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_all_cases_validate(self) -> None:
        result = self.run_cli("validate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("validated 8 benchmark cases", result.stdout)

    def test_handoff_cases_validate(self) -> None:
        result = subprocess.run(
            [sys.executable, str(HANDOFF), "validate"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("validated 3 execution-packet cases", result.stdout)

    def test_missing_or_empty_cases_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="empty-cases-") as directory:
            empty = Path(directory)
            with self.assertRaisesRegex(ValueError, "directory is empty"):
                benchmark_module.discover_cases(cases_dir=empty)
            with self.assertRaisesRegex(ValueError, "does not exist"):
                benchmark_module.discover_cases(cases_dir=empty / "missing")

    def test_fixture_paths_cannot_escape_workspace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fixture-path-") as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "repository-relative"):
                benchmark_module.resolve_relative_file(root, "../outside.py", "entrypoint")
            with self.assertRaisesRegex(ValueError, "repository-relative"):
                benchmark_module.resolve_relative_file(
                    root, str(root / "absolute.py"), "entrypoint"
                )

    def test_fixture_symlink_is_rejected_before_workspace_copy(self) -> None:
        source = HANDOFF_CASES / "tenant-settings"
        with tempfile.TemporaryDirectory(prefix="fixture-symlink-") as directory:
            root = Path(directory)
            case_dir = root / "tenant-settings"
            shutil.copytree(source, case_dir)
            secret = root / "secret.txt"
            secret.write_text("must-not-be-copied", encoding="utf-8")
            target = case_dir / "starter" / "settings" / "schema.py"
            target.unlink()
            target.symlink_to(secret)
            case = benchmark_module.BenchmarkCase(
                case_dir,
                json.loads((case_dir / "case.json").read_text(encoding="utf-8")),
            )

            errors = benchmark_module.validate_case(case)
            self.assertTrue(any("cannot contain symlinks" in error for error in errors))
            workspace_root = root / "workspaces"
            with self.assertRaisesRegex(ValueError, "cannot contain symlinks"):
                benchmark_module.safe_workspace(case, workspace_root)
            self.assertFalse(workspace_root.exists())

    def test_case_ids_cannot_escape_workspace_or_disagree_with_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="case-id-") as directory:
            cases_dir = Path(directory) / "cases"
            invalid_dir = cases_dir / "invalid"
            invalid_dir.mkdir(parents=True)
            (invalid_dir / "case.json").write_text(
                json.dumps({"id": "../escape"}), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "invalid benchmark case id"):
                benchmark_module.discover_cases(cases_dir=cases_dir)

            (invalid_dir / "case.json").write_text(
                json.dumps({"id": "different"}), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "does not match id"):
                benchmark_module.discover_cases(cases_dir=cases_dir)

            case = benchmark_module.BenchmarkCase(
                invalid_dir, {"id": "/tmp/escape"}
            )
            with self.assertRaisesRegex(ValueError, "invalid benchmark case id"):
                benchmark_module.safe_workspace(case, Path(directory) / "workspaces")

    def test_reference_must_own_every_tested_entrypoint(self) -> None:
        source = HANDOFF_CASES / "refund-ledger"
        with tempfile.TemporaryDirectory(prefix="reference-entrypoint-") as directory:
            case_dir = Path(directory) / "refund-ledger"
            shutil.copytree(source, case_dir)
            reference = case_dir / "reference" / "billing" / "events.py"
            reference.rename(reference.with_suffix(".omitted"))
            case = benchmark_module.BenchmarkCase(
                case_dir,
                json.loads((case_dir / "case.json").read_text(encoding="utf-8")),
            )
            errors = benchmark_module.validate_case(case)
            self.assertTrue(
                any("missing tested reference entrypoint" in error for error in errors),
                errors,
            )

    def test_semantic_variants_are_smaller(self) -> None:
        result = self.run_cli("static", "--json", "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual(len(rows), 8)
        for row in rows:
            self.assertGreater(row["byte_reduction_percent"], 0, row["case"])
            self.assertGreater(row["word_reduction_percent"], 0, row["case"])

    def test_static_rows_can_measure_tokens(self) -> None:
        class WhitespaceEncoder:
            @staticmethod
            def encode(text: str) -> list[str]:
                return text.split()

        rows = benchmark_module.static_rows(
            benchmark_module.discover_cases(["email-routing"]),
            token_encoder=WhitespaceEncoder(),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["baseline"]["tokens"], rows[0]["baseline"]["words"])
        self.assertGreater(rows[0]["token_reduction_percent"], 0)

    def test_quality_gate_rejects_case_regression_hidden_by_aggregate(self) -> None:
        def result(case: str, variant: str, passed: bool) -> dict[str, object]:
            return {
                "case": case,
                "variant": variant,
                "provider": {"usage": {}, "duration_seconds": 0},
                "grade": {
                    "passed": int(passed),
                    "total": 1,
                    "acceptance_passed": int(passed),
                    "acceptance_total": 1,
                    "task_success": passed,
                },
                "cost_usd": None,
            }

        rows = [
            result("case-a", "baseline", True),
            result("case-a", "semantic", False),
            result("case-b", "baseline", False),
            result("case-b", "semantic", True),
        ]
        self.assertFalse(benchmark_module.quality_preserved(rows))

    def test_partial_semantic_telemetry_does_not_crash_pair_reduction(self) -> None:
        results = [
            {
                "case": "case-a",
                "repetition": 1,
                "variant": "baseline",
                "provider": {"usage": {"input_tokens": 100}},
            },
            {
                "case": "case-a",
                "repetition": 1,
                "variant": "semantic",
                "provider": {"usage": {}},
            },
        ]
        self.assertEqual(
            benchmark_module.paired_reductions(results, "input_tokens"), []
        )

    def test_semantic_variants_are_direct_execution_documents(self) -> None:
        ambiguous_python_map_fields = (
            "event.type",
            "order.status",
            "user.id",
            "flag.key",
        )
        for case_dir in sorted((ROOT / "benchmarks" / "cases").iterdir()):
            if not case_dir.is_dir():
                continue
            manifest = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
            text = (case_dir / "semantic.spec.ctx").read_text(encoding="utf-8")
            self.assertIn(manifest["entrypoint"], text, manifest["id"])
            self.assertNotIn("open_questions: []", text, manifest["id"])
            self.assertNotIn("\nmeta:\n", text, manifest["id"])
            for field in ambiguous_python_map_fields:
                self.assertNotIn(field, text, manifest["id"])

    def test_mock_run_and_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="semantic-spec-test-") as directory:
            result_path = Path(directory) / "result.json"
            report_path = Path(directory) / "report.md"
            run = self.run_cli(
                "run",
                "--provider",
                "mock",
                "--case",
                "email-routing",
                "--repetitions",
                "1",
                "--output",
                str(result_path),
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            document = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(len(document["results"]), 2)
            self.assertTrue(all(item["grade"]["pass_rate"] == 1 for item in document["results"]))

            report = self.run_cli(
                "report", str(result_path), "--output", str(report_path)
            )
            self.assertEqual(report.returncode, 0, report.stderr)
            text = report_path.read_text(encoding="utf-8")
            self.assertIn("smoke test", text)
            self.assertIn("Acceptance pass rate", text)

    def test_mock_pair_order_is_adjacent_and_balanced(self) -> None:
        with tempfile.TemporaryDirectory(prefix="semantic-spec-order-") as directory:
            result_path = Path(directory) / "result.json"
            run = self.run_cli(
                "run",
                "--provider",
                "mock",
                "--repetitions",
                "1",
                "--output",
                str(result_path),
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            results = json.loads(result_path.read_text(encoding="utf-8"))["results"]
            self.assertEqual(len(results), 16)
            baseline_first = 0
            semantic_first = 0
            for index in range(0, len(results), 2):
                first, second = results[index:index + 2]
                self.assertEqual(first["pair_id"], second["pair_id"])
                self.assertEqual({first["variant"], second["variant"]}, {"baseline", "semantic"})
                baseline_first += first["variant"] == "baseline"
                semantic_first += first["variant"] == "semantic"
            self.assertEqual(baseline_first, semantic_first)

    def test_existing_result_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory(prefix="semantic-spec-overwrite-") as directory:
            result_path = Path(directory) / "result.json"
            result_path.write_text("keep", encoding="utf-8")
            run = self.run_cli(
                "run",
                "--provider",
                "mock",
                "--case",
                "email-routing",
                "--output",
                str(result_path),
            )
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(result_path.read_text(encoding="utf-8"), "keep")

    def test_run_accepts_generated_semantic_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="semantic-spec-generated-") as directory:
            root = Path(directory)
            specs = root / "specs"
            specs.mkdir()
            source = (
                ROOT
                / "benchmarks"
                / "cases"
                / "email-routing"
                / "semantic.spec.ctx"
            )
            (specs / "email-routing.spec.ctx").write_text(
                source.read_text(encoding="utf-8") + "# generated fixture\n",
                encoding="utf-8",
            )
            result_path = root / "result.json"
            run = self.run_cli(
                "run",
                "--provider",
                "mock",
                "--case",
                "email-routing",
                "--semantic-dir",
                str(specs),
                "--output",
                str(result_path),
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            document = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(document["semantic_source"], "generated")
            semantic = next(
                item for item in document["results"] if item["variant"] == "semantic"
            )
            self.assertGreater(semantic["spec"]["bytes"], source.stat().st_size)

    def test_generated_execution_packet_is_rechecked_against_original_starter(self) -> None:
        case = benchmark_module.discover_cases(
            ["tenant-settings"], HANDOFF_CASES
        )[0]
        with tempfile.TemporaryDirectory(prefix="generated-stale-packet-") as directory:
            specs = Path(directory) / "specs"
            specs.mkdir()
            packet = (case.path / "packet.spec.ctx").read_text(encoding="utf-8")
            packet = re.sub(
                r"basis: route-sha256:[0-9a-f]{64}",
                "basis: route-sha256:" + "0" * 64,
                packet,
                count=1,
            )
            (specs / "tenant-settings.spec.ctx").write_text(
                packet, encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "stale route basis"):
                benchmark_module.load_semantic_specs([case], specs)

    def test_mock_lifecycle_generation_and_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="semantic-spec-lifecycle-") as directory:
            root = Path(directory)
            generated = root / "generated"
            generation = subprocess.run(
                [
                    sys.executable,
                    str(LIFECYCLE),
                    "generate",
                    "--provider",
                    "mock",
                    "--case",
                    "email-routing",
                    "--output",
                    str(generated),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(generation.returncode, 0, generation.stderr)
            generation_document = json.loads(
                (generated / "generation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(generation_document["results"][0]["selected_attempt"], 1)
            self.assertEqual(len(generation_document["results"][0]["attempts"]), 1)
            implementation_path = root / "implementation.json"
            implementation = self.run_cli(
                "run",
                "--provider",
                "mock",
                "--case",
                "email-routing",
                "--semantic-dir",
                str(generated / "specs"),
                "--output",
                str(implementation_path),
            )
            self.assertEqual(implementation.returncode, 0, implementation.stderr)
            report_path = root / "report.md"
            report = subprocess.run(
                [
                    sys.executable,
                    str(LIFECYCLE),
                    "report",
                    str(generated / "generation.json"),
                    str(implementation_path),
                    "--output",
                    str(report_path),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(report.returncode, 0, report.stderr)
            text = report_path.read_text(encoding="utf-8")
            self.assertIn("Generated valid specs: **1/1**", text)
            self.assertIn("Break-even reuse", text)

    def test_mock_lifecycle_generates_valid_execution_packet(self) -> None:
        with tempfile.TemporaryDirectory(prefix="packet-generation-") as directory:
            output = Path(directory) / "generated"
            result = subprocess.run(
                [
                    sys.executable,
                    str(LIFECYCLE),
                    "generate",
                    "--provider",
                    "mock",
                    "--cases-dir",
                    str(HANDOFF_CASES),
                    "--case",
                    "tenant-settings",
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(
                (output / "generation.json").read_text(encoding="utf-8")
            )
            self.assertIsNone(document["results"][0]["error"])
            packet = output / "specs" / "tenant-settings.spec.ctx"
            checked = subprocess.run(
                [
                    sys.executable,
                    str(PACKET_CHECK),
                    str(HANDOFF_CASES / "tenant-settings" / "starter"),
                    str(packet),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_lifecycle_starter_snapshot_is_read_only_to_generation_workspace(self) -> None:
        if shutil.which("codex") is None:
            self.skipTest("Codex sandbox is not installed")
        lifecycle = load_module("benchmark_lifecycle_workspace", LIFECYCLE)
        case = benchmark_module.discover_cases(
            ["tenant-settings"], HANDOFF_CASES
        )[0]
        with tempfile.TemporaryDirectory(prefix="generation-inputs-") as directory:
            workspace, snapshot = lifecycle.prepare_generation_workspace(
                Path(directory) / "attempt", case
            )
            self.assertFalse((workspace / "repo").exists())
            self.assertFalse((workspace / "source.md").exists())
            self.assertFalse((workspace / "skill").exists())
            before = benchmark_module.tree_sha256(snapshot)
            target = snapshot / "solution.py"
            completed = benchmark_module.run_restricted_sandbox(
                [
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; "
                        f"Path({str(target)!r}).write_text('tampered', encoding='utf-8')"
                    ),
                ],
                workspace,
                [snapshot],
                [workspace],
            )
            self.assertNotEqual(completed.returncode, 0, completed)
            self.assertEqual(benchmark_module.tree_sha256(snapshot), before)

    def test_lifecycle_never_persists_symlinked_provider_artifact(self) -> None:
        lifecycle = load_module("benchmark_lifecycle_symlink", LIFECYCLE)
        original = lifecycle.core.run_codex
        with tempfile.TemporaryDirectory(prefix="generation-symlink-") as directory:
            root = Path(directory)
            secret = root / "secret.txt"
            secret.write_text("private-provider-oracle", encoding="utf-8")
            output = root / "generated"

            def malicious_provider(workspace, *_args, **_kwargs):
                (workspace / "result.spec.ctx").symlink_to(secret)
                raise RuntimeError("provider failed")

            lifecycle.core.run_codex = malicious_provider
            try:
                lifecycle.generate(Namespace(
                    provider="codex",
                    model="test-model",
                    reasoning_effort="medium",
                    token_encoding=None,
                    max_attempts=1,
                    cases_dir=None,
                    case=["email-routing"],
                    output=output,
                    timeout_seconds=30,
                ))
            finally:
                lifecycle.core.run_codex = original

            document_text = (output / "generation.json").read_text(encoding="utf-8")
            document = json.loads(document_text)
            attempt = document["results"][0]["attempts"][0]
            self.assertIsNone(attempt["artifact"])
            self.assertIsNone(attempt["semantic"])
            self.assertNotIn("private-provider-oracle", document_text)

    def test_mock_context_run_and_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="semantic-spec-context-") as directory:
            root = Path(directory)
            result_path = root / "context.json"
            run = subprocess.run(
                [
                    sys.executable,
                    str(CONTEXT),
                    "run",
                    "--provider",
                    "mock",
                    "--case",
                    "email-routing",
                    "--output",
                    str(result_path),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            document = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(document["kind"], "semantic-spec-context-load")
            self.assertEqual(len(document["results"]), 2)
            self.assertTrue(all(item["error"] is None for item in document["results"]))

            report_path = root / "context.md"
            rendered = subprocess.run(
                [
                    sys.executable,
                    str(CONTEXT),
                    "report",
                    str(result_path),
                    "--output",
                    str(report_path),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            text = report_path.read_text(encoding="utf-8")
            self.assertIn("Context Benchmark", text)
            self.assertIn("smoke run", text)
            self.assertIn("Total uncached input", text)

    def test_conversion_check_requires_fewer_bytes_and_words(self) -> None:
        with tempfile.TemporaryDirectory(prefix="semantic-spec-size-") as directory:
            root = Path(directory)
            source = root / "source.md"
            output = root / "result.spec.ctx"
            source.write_text("one two three four\n", encoding="utf-8")
            output.write_text("one two\n", encoding="utf-8")
            smaller = subprocess.run(
                [sys.executable, str(CONVERSION_CHECK), str(source), str(output)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(smaller.returncode, 0, smaller.stderr)

            output.write_text("one two three four five\n", encoding="utf-8")
            larger = subprocess.run(
                [sys.executable, str(CONVERSION_CHECK), str(source), str(output)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(larger.returncode, 1, larger.stderr)

    def test_conversion_check_can_reject_token_expansion(self) -> None:
        with tempfile.TemporaryDirectory(prefix="semantic-spec-token-size-") as directory:
            root = Path(directory)
            source = root / "source.md"
            output = root / "result.spec.ctx"
            source.write_text(
                "aaaaaaaaaa bbbbbbbbbb cccccccccc\n", encoding="utf-8"
            )
            output.write_text("a+b+c+d+e+f+g+h+i+j\n", encoding="utf-8")
            (root / "tiktoken.py").write_text(
                "class Encoder:\n"
                "    def encode(self, text):\n"
                "        return [0] * (len(text.split()) + text.count('+') * 10)\n"
                "def get_encoding(name):\n"
                "    return Encoder()\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(root)
            result = subprocess.run(
                [
                    sys.executable,
                    str(CONVERSION_CHECK),
                    str(source),
                    str(output),
                    "--encoding",
                    "test_encoding",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            metrics = json.loads(result.stdout)
            self.assertTrue(metrics["smaller_bytes"])
            self.assertTrue(metrics["smaller_words"])
            self.assertFalse(metrics["smaller_tokens"])

    def test_execution_packets_route_existing_anchored_files(self) -> None:
        for case_dir in sorted(HANDOFF_CASES.iterdir()):
            result = subprocess.run(
                [
                    sys.executable,
                    str(PACKET_CHECK),
                    str(case_dir / "starter"),
                    str(case_dir / "packet.spec.ctx"),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            metrics = json.loads(result.stdout)
            self.assertTrue(metrics["valid"], case_dir.name)
            self.assertGreater(metrics["routed_context"]["files"], 0)

    def test_execution_packet_requires_one_bounded_execution_policy(self) -> None:
        source = HANDOFF_CASES / "tenant-settings"
        execution_line = (
            "execution: routed read once -> all do -> V1 once -> stop on pass; "
            "expand only on contradiction/failure\n"
        )
        for replacement in ("", execution_line * 2):
            with self.subTest(replacement_count=replacement.count("execution:")), \
                    tempfile.TemporaryDirectory(prefix="packet-execution-") as directory:
                packet = Path(directory) / "packet.spec.ctx"
                text = (source / "packet.spec.ctx").read_text(encoding="utf-8")
                packet.write_text(
                    text.replace(execution_line, replacement), encoding="utf-8"
                )
                result = subprocess.run(
                    [
                        sys.executable,
                        str(PACKET_CHECK),
                        str(source / "starter"),
                        str(packet),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                metrics = json.loads(result.stdout)
                self.assertIn(
                    "canonical bounded execution policy",
                    "\n".join(metrics["errors"]),
                )

    def test_execution_packet_rejects_escape_duplicate_and_stale_anchor(self) -> None:
        with tempfile.TemporaryDirectory(prefix="execution-packet-") as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            (repo / "service.py").write_text("def current():\n    pass\n", encoding="utf-8")
            packet = root / "packet.spec.ctx"
            packet.write_text(
                "spec\nroute:\n"
                "  read: service.py::def stale\n"
                "  edit: ./service.py\n"
                "  edit: ../outside.py\n"
                "verify:\n"
                "  V1: `python -m py_compile service.py`\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(PACKET_CHECK), str(repo), str(packet)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            metrics = json.loads(result.stdout)
            self.assertIn("duplicate route path service.py", "\n".join(metrics["errors"]))
            self.assertIn("source anchor not found", "\n".join(metrics["errors"]))
            self.assertIn("repository-relative", "\n".join(metrics["errors"]))
            self.assertIn("file-owned do action", "\n".join(metrics["errors"]))

    def test_execution_packet_rejects_malformed_route_line(self) -> None:
        with tempfile.TemporaryDirectory(prefix="malformed-packet-") as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            (repo / "service.py").write_text("def current():\n    pass\n", encoding="utf-8")
            packet = root / "packet.spec.ctx"
            packet.write_text(
                "spec\nroute:\n"
                "  edit: service.py::def current\n"
                "    do: return one\n"
                "  edti: ignored.py\n"
                "verify:\n"
                "  V1: `python3 -m py_compile service.py`\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(PACKET_CHECK), str(repo), str(packet)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            metrics = json.loads(result.stdout)
            self.assertIn("malformed route line", "\n".join(metrics["errors"]))

    def test_execution_packet_rejects_duplicate_basis_declarations(self) -> None:
        case_dir = HANDOFF_CASES / "tenant-settings"
        with tempfile.TemporaryDirectory(prefix="duplicate-basis-") as directory:
            packet_path = Path(directory) / "packet.spec.ctx"
            packet = (case_dir / "packet.spec.ctx").read_text(encoding="utf-8")
            basis = re.search(
                r"^basis: route-sha256:[0-9a-f]{64}$", packet, re.MULTILINE
            )
            self.assertIsNotNone(basis)
            packet_path.write_text(
                packet.rstrip() + "\n" + basis.group(0) + "\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(PACKET_CHECK),
                    str(case_dir / "starter"),
                    str(packet_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            metrics = json.loads(result.stdout)
            self.assertIn(
                "packet has duplicate basis declarations", metrics["errors"]
            )

    def test_acceptance_ids_must_be_declared_in_acceptance_block(self) -> None:
        with tempfile.TemporaryDirectory(prefix="acceptance-block-") as directory:
            case_dir = Path(directory)
            (case_dir / "tests.json").write_text(
                json.dumps({
                    "tests": [{
                        "name": "covered",
                        "acceptance": "A1",
                        "call": "run",
                        "expect": True,
                    }]
                }),
                encoding="utf-8",
            )
            case = benchmark_module.BenchmarkCase(
                case_dir,
                {"id": "example", "entrypoint": "solution.py"},
            )
            errors = benchmark_module.validate_semantic_text(
                case,
                "spec\nentrypoint: solution.py\nnotes:\n  mention: A1\n",
            )
            self.assertIn("example: acceptance block lacks A1", errors)

    def test_codex_event_parser_preserves_and_classifies_commands(self) -> None:
        events = "\n".join([
            json.dumps({
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "rg --files && sed -n '1,80p' app.py",
                    "exit_code": 0,
                },
            }),
            json.dumps({
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "python3 -m py_compile app.py",
                    "exit_code": 0,
                },
            }),
        ])
        parsed = benchmark_module.parse_codex_events(events)
        self.assertEqual(parsed["tool_call_total"], 2)
        self.assertEqual(len(parsed["command_log"]), 2)
        self.assertEqual(parsed["command_categories"]["discovery"], 1)
        self.assertEqual(parsed["command_categories"]["read"], 1)
        self.assertEqual(parsed["command_categories"]["verify"], 1)

    def test_mock_handoff_suite_runs_all_pairs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="handoff-suite-") as directory:
            result_path = Path(directory) / "result.json"
            run = subprocess.run(
                [
                    sys.executable,
                    str(HANDOFF),
                    "run",
                    "--provider",
                    "mock",
                    "--output",
                    str(result_path),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            document = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(document["kind"], "semantic-execution-packet-comparison")
            self.assertTrue(document["full_corpus"])
            self.assertEqual(len(document["results"]), 9)
            self.assertTrue(
                all(result["grade"]["task_success"] for result in document["results"])
            )

    def test_handoff_rejects_duplicate_variant_flags(self) -> None:
        with tempfile.TemporaryDirectory(prefix="duplicate-arm-") as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    str(HANDOFF),
                    "run",
                    "--provider",
                    "mock",
                    "--variant",
                    "packet",
                    "--variant",
                    "packet",
                    "--output",
                    str(Path(directory) / "result.json"),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("duplicate --variant", result.stderr)

    def test_packet_verification_must_match_manifest_command(self) -> None:
        handoff = load_module("benchmark_handoff_verify", HANDOFF)
        source = HANDOFF_CASES / "tenant-settings"
        with tempfile.TemporaryDirectory(prefix="packet-verify-") as directory:
            case_dir = Path(directory) / "tenant-settings"
            shutil.copytree(source, case_dir)
            packet_path = case_dir / "packet.spec.ctx"
            expected = "python3 -m unittest -q test_smoke.py"
            packet = packet_path.read_text(encoding="utf-8")
            packet = packet.replace(
                "goal:", f"note: required command {expected}\ngoal:", 1
            ).replace(
                f"V1: `{expected}`",
                "V1: `python3 -m py_compile solution.py`",
            )
            packet_path.write_text(packet, encoding="utf-8")
            case = benchmark_module.BenchmarkCase(
                case_dir,
                json.loads((case_dir / "case.json").read_text(encoding="utf-8")),
            )
            errors = handoff.validate_packet(case)
            self.assertIn(
                "tenant-settings: packet lacks exact verification command",
                errors,
            )

    def test_handoff_case_requires_nonempty_verification_command(self) -> None:
        handoff = load_module("benchmark_handoff_required_verify", HANDOFF)
        source = HANDOFF_CASES / "tenant-settings"
        for missing_value in (None, ""):
            with self.subTest(value=missing_value), tempfile.TemporaryDirectory(
                prefix="required-verify-"
            ) as directory:
                case_dir = Path(directory) / "tenant-settings"
                shutil.copytree(source, case_dir)
                manifest = json.loads(
                    (case_dir / "case.json").read_text(encoding="utf-8")
                )
                if missing_value is None:
                    manifest.pop("verification_command")
                else:
                    manifest["verification_command"] = missing_value
                (case_dir / "case.json").write_text(
                    json.dumps(manifest), encoding="utf-8"
                )
                case = benchmark_module.BenchmarkCase(case_dir, manifest)
                errors = handoff.validate_cases([case])
                self.assertTrue(
                    any("requires verification_command" in error for error in errors),
                    errors,
                )

    def test_handoff_case_requires_immutable_verification_files(self) -> None:
        handoff = load_module("benchmark_handoff_required_files", HANDOFF)
        source = HANDOFF_CASES / "tenant-settings"
        for invalid_value in (None, []):
            with self.subTest(value=invalid_value), tempfile.TemporaryDirectory(
                prefix="required-verification-files-"
            ) as directory:
                case_dir = Path(directory) / "tenant-settings"
                shutil.copytree(source, case_dir)
                manifest = json.loads(
                    (case_dir / "case.json").read_text(encoding="utf-8")
                )
                if invalid_value is None:
                    manifest.pop("verification_files")
                else:
                    manifest["verification_files"] = invalid_value
                (case_dir / "case.json").write_text(
                    json.dumps(manifest), encoding="utf-8"
                )
                case = benchmark_module.BenchmarkCase(case_dir, manifest)
                errors = handoff.validate_cases([case])
                self.assertTrue(
                    any("requires verification_files" in error for error in errors),
                    errors,
                )

    def test_sandboxed_grader_cannot_read_outside_allowed_roots(self) -> None:
        if shutil.which("codex") is None:
            self.skipTest("Codex sandbox is not installed")
        with tempfile.TemporaryDirectory(prefix="grader-sandbox-") as directory:
            root = Path(directory)
            case_dir = root / "case"
            workspace = root / "workspace"
            case_dir.mkdir()
            workspace.mkdir()
            secret = root / "secret.txt"
            secret.write_text("private", encoding="utf-8")
            manifest = {"id": "sandbox", "entrypoint": "solution.py"}
            (case_dir / "case.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            (case_dir / "tests.json").write_text(
                json.dumps({
                    "tests": [{
                        "name": "cannot_read",
                        "acceptance": "A1",
                        "call": "read_secret",
                        "expect": {"sibling": "blocked", "proc": "isolated"},
                    }]
                }),
                encoding="utf-8",
            )
            (workspace / "solution.py").write_text(
                "def attempt(path):\n"
                "    try:\n"
                "        open(path, 'rb').read()\n"
                "        return 'readable'\n"
                "    except OSError:\n"
                "        return 'blocked'\n\n"
                "def read_secret():\n"
                "    return {\n"
                f"        'sibling': attempt({str(secret)!r}),\n"
                "        'proc': ('leaked' if b'BENCHMARK_PRIVATE_SENTINEL' in "
                "open('/proc/1/environ', 'rb').read() else 'isolated'),\n"
                "    }\n",
                encoding="utf-8",
            )
            case = benchmark_module.BenchmarkCase(case_dir, manifest)
            os.environ["BENCHMARK_PRIVATE_SENTINEL"] = "do-not-leak"
            try:
                grade = benchmark_module.run_grader(case, workspace)
            finally:
                os.environ.pop("BENCHMARK_PRIVATE_SENTINEL", None)
            self.assertTrue(grade["task_success"], grade)

    def test_solution_atexit_output_cannot_forge_worker_result(self) -> None:
        if shutil.which("codex") is None:
            self.skipTest("Codex sandbox is not installed")
        with tempfile.TemporaryDirectory(prefix="grader-atexit-") as directory:
            root = Path(directory)
            case_dir = root / "case"
            workspace = root / "workspace"
            case_dir.mkdir()
            workspace.mkdir()
            manifest = {"id": "atexit", "entrypoint": "solution.py"}
            (case_dir / "case.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            (case_dir / "tests.json").write_text(
                json.dumps({
                    "tests": [{
                        "name": "real_result",
                        "acceptance": "A1",
                        "call": "value",
                        "expect": "real",
                    }]
                }),
                encoding="utf-8",
            )
            forged = json.dumps({
                "ok": True,
                "result": "forged",
                "args": [],
                "kwargs": {},
            })
            (workspace / "solution.py").write_text(
                "import atexit\n"
                "import os\n\n"
                "def value():\n"
                f"    atexit.register(lambda: os.write(1, {((forged + chr(10)).encode())!r}))\n"
                "    return 'real'\n",
                encoding="utf-8",
            )
            case = benchmark_module.BenchmarkCase(case_dir, manifest)
            grade = benchmark_module.run_grader(case, workspace)
            self.assertTrue(grade["task_success"], grade)

    def test_sandboxed_grader_handles_multifile_reference(self) -> None:
        if shutil.which("codex") is None:
            self.skipTest("Codex sandbox is not installed")
        case = benchmark_module.discover_cases(
            ["tenant-settings"], HANDOFF_CASES
        )[0]
        with tempfile.TemporaryDirectory(prefix="multifile-grade-") as directory:
            workspace = Path(directory) / "workspace"
            shutil.copytree(case.path / "starter", workspace)
            shutil.copytree(case.path / "reference", workspace, dirs_exist_ok=True)
            grade = benchmark_module.run_grader(case, workspace)
            self.assertTrue(grade["task_success"], grade)

    def test_verification_runs_on_disposable_workspace_copy(self) -> None:
        if shutil.which("codex") is None:
            self.skipTest("Codex sandbox is not installed")
        with tempfile.TemporaryDirectory(prefix="verify-copy-") as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "mutate.py").write_text(
                "from pathlib import Path\nPath('marker.txt').write_text('changed')\n",
                encoding="utf-8",
            )
            case = benchmark_module.BenchmarkCase(
                root,
                {
                    "id": "verify-copy",
                    "entrypoint": "mutate.py",
                    "verification_command": "python3 mutate.py",
                },
            )
            verification = benchmark_module.run_verification(case, workspace)
            self.assertEqual(verification["return_code"], 0, verification)
            self.assertFalse((workspace / "marker.txt").exists())

    def test_verification_restores_trusted_test_fixture(self) -> None:
        with tempfile.TemporaryDirectory(prefix="verify-fixture-") as directory:
            root = Path(directory)
            starter = root / "starter"
            workspace = root / "workspace"
            starter.mkdir()
            workspace.mkdir()
            original_test = "raise SystemExit(7)\n"
            (starter / "test_smoke.py").write_text(
                original_test, encoding="utf-8"
            )
            (workspace / "test_smoke.py").write_text(
                "raise SystemExit(0)\n", encoding="utf-8"
            )
            case = benchmark_module.BenchmarkCase(
                root,
                {
                    "id": "verify-fixture",
                    "entrypoint": "solution.py",
                    "verification_command": "python3 test_smoke.py",
                    "verification_files": ["test_smoke.py"],
                },
            )

            verification = benchmark_module.run_verification(
                case, workspace, trusted=True
            )
            self.assertEqual(verification["return_code"], 7, verification)
            expected_hash = benchmark_module.verification_fixture_sha256([
                ("test_smoke.py", starter / "test_smoke.py")
            ])
            self.assertEqual(verification["fixture_sha256"], expected_hash)

    def test_lifecycle_rejects_zero_exit_provider_event_errors(self) -> None:
        lifecycle = load_module("benchmark_lifecycle_events", LIFECYCLE)
        failure = lifecycle.provider_failure({
            "return_code": 0,
            "event_errors": ["malformed event"],
            "stderr_tail": "",
        })
        self.assertEqual(failure, "provider reported errors: malformed event")

    def test_handoff_claim_requires_every_primary_pair_to_succeed(self) -> None:
        handoff = load_module("benchmark_handoff_coverage", HANDOFF)

        def result(
            variant: str,
            repetition: int,
            passed: bool,
            uncached_input_tokens: int,
        ) -> dict[str, object]:
            return {
                "case": "example",
                "variant": variant,
                "repetition": repetition,
                "provider": {
                    "duration_seconds": 1.0,
                    "tool_call_total": 1,
                    "tool_calls": {"command_execution": 1},
                    "command_categories": {"discovery": 1},
                    "usage": {
                        "input_tokens": uncached_input_tokens,
                        "uncached_input_tokens": uncached_input_tokens,
                        "output_tokens": 10,
                    },
                },
                "grade": {
                    "passed": int(passed),
                    "total": 1,
                    "acceptance_passed": int(passed),
                    "acceptance_total": 1,
                    "task_success": passed,
                },
                "error": None,
            }

        results = []
        for repetition in range(1, 4):
            results.append(result("markdown", repetition, True, 120))
        results.extend([
            result("semantic", 1, True, 100),
            result("semantic", 2, True, 100),
            result("semantic", 3, False, 100),
            result("packet", 1, True, 50),
            result("packet", 2, False, 50),
            result("packet", 3, True, 50),
        ])
        metrics = {
            variant: {"bytes": 100, "words": 10, "lines": 5}
            for variant in ("markdown", "semantic", "packet")
        }
        document = {
            "kind": "semantic-execution-packet-comparison",
            "run_id": "coverage",
            "provider": "codex",
            "model": "test-model",
            "reasoning_effort": "medium",
            "cases": ["example"],
            "repetitions": 3,
            "full_corpus": True,
            "variants": ["markdown", "semantic", "packet"],
            "static": [{"case": "example", "variants": metrics}],
            "results": results,
        }
        report = handoff.report(document)
        self.assertIn("Primary comparison coverage: **1/3**", report)
        self.assertIn("cannot establish a Packet v3 token-saving claim", report)

    def test_handoff_report_recomputes_credibility_fail_closed(self) -> None:
        handoff = load_module("benchmark_handoff_credible", HANDOFF)
        cases = benchmark_module.discover_cases(cases_dir=HANDOFF_CASES)
        results: list[dict[str, object]] = []
        run_order = 0
        for case in cases:
            for repetition in range(1, 4):
                for variant in ("markdown", "semantic", "packet"):
                    run_order += 1
                    specification = handoff.artifact_text(case, variant)
                    prompt = benchmark_module.benchmark_prompt(specification)
                    snapshot = handoff.case_snapshot(case)
                    results.append({
                        "case": case.id,
                        "pair_id": f"{case.id}:r{repetition}",
                        "variant": variant,
                        "repetition": repetition,
                        "run_order": run_order,
                        "spec": benchmark_module.text_metrics(specification),
                        "provenance": {
                            "spec_sha256": snapshot["variants"][variant],
                            "prompt_sha256": benchmark_module.sha256_bytes(
                                prompt.encode("utf-8")
                            ),
                            "starter_sha256": snapshot["starter_sha256"],
                            "fixture_sha256": snapshot["fixture_sha256"],
                        },
                        "provider": {
                            "return_code": 0,
                            "event_errors": [],
                            "duration_seconds": 1.0,
                            "tool_call_total": 1,
                            "tool_calls": {"command_execution": 1},
                            "command_categories": {
                                "discovery": 0,
                                "read": 0,
                                "verify": 1,
                            },
                            "usage": {
                                "input_tokens": 100,
                                "uncached_input_tokens": 100,
                                "output_tokens": 10,
                            },
                        },
                        "verification": {
                            "command": case.manifest["verification_command"],
                            "fixture_sha256": snapshot[
                                "verification_fixture_sha256"
                            ],
                            "return_code": 0,
                        },
                        "grade": {
                            "passed": 1,
                            "total": 1,
                            "acceptance_passed": 1,
                            "acceptance_total": 1,
                            "task_success": True,
                        },
                        "error": None,
                    })
        document = {
            "provider": "codex",
            "model": "test-model",
            "reasoning_effort": "medium",
            "packet_version": 3,
            "cases": [case.id for case in cases],
            "repetitions": 3,
            "variants": ["markdown", "semantic", "packet"],
            "full_corpus": False,
            "fixture_snapshot": {
                case.id: handoff.case_snapshot(case) for case in cases
            },
            "static": handoff.static_rows(cases),
        }
        self.assertTrue(handoff.report_run_is_credible(document, results))

        failed_provider = json.loads(json.dumps(results))
        failed_provider[0]["provider"]["return_code"] = 1
        self.assertFalse(
            handoff.report_run_is_credible(document, failed_provider)
        )

        event_error = json.loads(json.dumps(results))
        event_error[0]["provider"]["event_errors"] = ["failed event"]
        self.assertFalse(handoff.report_run_is_credible(document, event_error))

        failed_verification = json.loads(json.dumps(results))
        failed_verification[0]["verification"]["return_code"] = 1
        self.assertFalse(
            handoff.report_run_is_credible(document, failed_verification)
        )

        forged_provenance = json.loads(json.dumps(results))
        forged_provenance[0]["provenance"]["spec_sha256"] = "0" * 64
        self.assertFalse(
            handoff.report_run_is_credible(document, forged_provenance)
        )

        forged_subset = dict(document)
        forged_subset["cases"] = [cases[0].id]
        forged_subset["full_corpus"] = True
        subset_results = [
            result for result in results if result["case"] == cases[0].id
        ]
        self.assertFalse(
            handoff.report_run_is_credible(forged_subset, subset_results)
        )

        zero_case = dict(document)
        zero_case["cases"] = []
        zero_case["full_corpus"] = True
        self.assertFalse(handoff.report_run_is_credible(zero_case, []))

        original_cases_dir = handoff.CASES_DIR
        with tempfile.TemporaryDirectory(prefix="handoff-snapshot-") as directory:
            copied_cases = Path(directory) / "handoff-cases"
            shutil.copytree(HANDOFF_CASES, copied_cases)
            handoff.CASES_DIR = copied_cases
            try:
                self.assertTrue(handoff.report_run_is_credible(document, results))
                packet = copied_cases / cases[0].id / "packet.spec.ctx"
                packet.write_text(
                    packet.read_text(encoding="utf-8") + "\n# mutated\n",
                    encoding="utf-8",
                )
                self.assertFalse(
                    handoff.report_run_is_credible(document, results)
                )
            finally:
                handoff.CASES_DIR = original_cases_dir

    def test_implementation_report_requires_exact_result_keys(self) -> None:
        cases = benchmark_module.discover_cases()
        semantic_specs = {
            case.id: case.spec_path("semantic").read_text(encoding="utf-8")
            for case in cases
        }
        document = benchmark_module.create_run_document(
            Namespace(
                provider="codex",
                model="test-model",
                reasoning_effort="medium",
                repetitions=3,
                seed=20260901,
                pricing={},
                semantic_dir=None,
            ),
            cases,
            semantic_specs,
            benchmark_module.CASES_DIR,
        )
        results = []
        run_order = 0
        for case in cases:
            snapshot = document["fixture_snapshot"][case.id]
            for repetition in range(1, 4):
                for variant in ("baseline", "semantic"):
                    run_order += 1
                    results.append({
                        "case": case.id,
                        "pair_id": f"{case.id}:r{repetition}",
                        "variant": variant,
                        "repetition": repetition,
                        "run_order": run_order,
                        "spec": snapshot["metrics"][variant],
                        "provenance": {
                            "spec_sha256": snapshot["variants"][variant],
                            "prompt_sha256": snapshot["prompts"][variant],
                            "starter_sha256": snapshot["starter_sha256"],
                            "fixture_sha256": snapshot["fixture_sha256"],
                        },
                        "provider": {
                            "return_code": 0,
                            "event_errors": [],
                            "duration_seconds": 1.0,
                            "tool_call_total": 1,
                            "usage": {
                                "input_tokens": 100,
                                "uncached_input_tokens": 100,
                                "output_tokens": 10,
                            },
                        },
                        "verification": None,
                        "grade": {
                            "passed": 1,
                            "total": 1,
                            "acceptance_passed": 1,
                            "acceptance_total": 1,
                            "task_success": True,
                        },
                        "cost_usd": None,
                        "error": None,
                    })
        document["results"] = results
        self.assertTrue(
            benchmark_module.implementation_report_is_credible(document, results)
        )

        duplicate_replacing_missing = json.loads(json.dumps(results))
        duplicate_replacing_missing[-1] = duplicate_replacing_missing[0]
        self.assertFalse(
            benchmark_module.implementation_report_is_credible(
                document, duplicate_replacing_missing
            )
        )

    def test_lifecycle_report_requires_exact_generation_cases(self) -> None:
        lifecycle = load_module("benchmark_lifecycle_exact", LIFECYCLE)
        cases = benchmark_module.discover_cases()
        generation = lifecycle.generation_document(
            Namespace(
                provider="codex",
                model="test-model",
                reasoning_effort="medium",
                token_encoding=None,
                max_attempts=1,
                cases_dir=None,
            ),
            cases,
        )
        results = []
        for case in cases:
            source = case.spec_path("baseline").read_text(encoding="utf-8")
            semantic = case.spec_path("semantic").read_text(encoding="utf-8")
            snapshot = generation["fixture_snapshot"][case.id]
            spec_hash = benchmark_module.sha256_bytes(semantic.encode("utf-8"))
            prompt_hash = benchmark_module.sha256_bytes(
                lifecycle.generation_prompt(case, None).encode("utf-8")
            )
            provenance = {
                "spec_sha256": spec_hash,
                "prompt_sha256": prompt_hash,
                "source_sha256": snapshot["source_sha256"],
                "starter_sha256": snapshot["starter_sha256"],
                "fixture_sha256": snapshot["fixture_sha256"],
                "skill_sha256": generation["skill_sha256"],
            }
            provider = {
                "return_code": 0,
                "event_errors": [],
                "usage": {
                    "input_tokens": 100,
                    "uncached_input_tokens": 100,
                    "output_tokens": 10,
                },
            }
            semantic_metrics = benchmark_module.text_metrics(semantic)
            results.append({
                "case": case.id,
                "artifact": f"specs/{case.id}.spec.ctx",
                "selected_attempt": 1,
                "attempts": [{
                    "attempt": 1,
                    "semantic": semantic_metrics,
                    "provenance": provenance,
                    "provider": provider,
                    "error": None,
                }],
                "source": benchmark_module.text_metrics(source),
                "semantic": semantic_metrics,
                "provenance": provenance,
                "provider": provider,
                "error": None,
            })
        generation["results"] = results
        self.assertTrue(lifecycle.generation_report_is_credible(generation))

        duplicate_replacing_missing = json.loads(json.dumps(generation))
        duplicate_replacing_missing["results"][-1] = duplicate_replacing_missing[
            "results"
        ][0]
        self.assertFalse(
            lifecycle.generation_report_is_credible(duplicate_replacing_missing)
        )

    def test_lifecycle_retry_accounting_includes_failed_attempts(self) -> None:
        lifecycle = load_module("benchmark_lifecycle", LIFECYCLE)
        attempts = [
            {
                "provider": {
                    "return_code": 1,
                    "duration_seconds": 2.0,
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                    "tool_calls": {"command_execution": 1},
                    "tool_call_total": 1,
                    "event_errors": ["failed"],
                    "stderr_tail": "first",
                }
            },
            {
                "provider": {
                    "return_code": 0,
                    "duration_seconds": 3.0,
                    "usage": {"input_tokens": 200, "output_tokens": 20},
                    "tool_calls": {"file_change": 1},
                    "tool_call_total": 1,
                    "event_errors": [],
                    "stderr_tail": "",
                }
            },
        ]
        aggregate = lifecycle.aggregate_attempt_providers(attempts, 2)
        self.assertEqual(aggregate["return_code"], 0)
        self.assertEqual(aggregate["attempt_count"], 2)
        self.assertEqual(aggregate["duration_seconds"], 5.0)
        self.assertEqual(aggregate["usage"]["input_tokens"], 300)
        self.assertEqual(aggregate["usage"]["output_tokens"], 30)
        self.assertEqual(aggregate["tool_call_total"], 2)

    def test_lifecycle_uses_selected_attempt_token_metrics(self) -> None:
        lifecycle = load_module("benchmark_lifecycle_tokens", LIFECYCLE)
        generation = {
            "results": [{
                "case": "example",
                "selected_attempt": 2,
                "attempts": [
                    {"conversion_check": None},
                    {
                        "conversion_check": {
                            "source": {"bytes": 100, "words": 20, "tokens": 30},
                            "output": {"bytes": 60, "words": 10, "tokens": 18},
                        }
                    },
                ],
            }]
        }
        rows = lifecycle.generation_static_rows(generation)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["token_reduction_percent"], 40.0)
        self.assertEqual(rows[0]["byte_reduction_percent"], 40.0)

    def test_lifecycle_break_even_is_inapplicable_after_quality_regression(self) -> None:
        lifecycle = load_module("benchmark_lifecycle_quality", LIFECYCLE)
        case = benchmark_module.discover_cases(["email-routing"])[0]
        generation = {
            "run_id": "generation",
            "provider": "codex",
            "model": "test-model",
            "reasoning_effort": "medium",
            "cases": [case.id],
            "results": [{
                "case": case.id,
                "error": None,
                "provenance": {"spec_sha256": "generated"},
                "provider": {
                    "usage": {
                        "input_tokens": 100,
                        "uncached_input_tokens": 100,
                        "output_tokens": 10,
                    }
                },
            }],
        }

        def implementation_result(variant: str, passed: bool) -> dict[str, object]:
            return {
                "case": case.id,
                "variant": variant,
                "repetition": 1,
                "provenance": {
                    "spec_sha256": "generated" if variant == "semantic" else "baseline"
                },
                "provider": {
                    "return_code": 0,
                    "duration_seconds": 1.0,
                    "tool_call_total": 1,
                    "usage": {
                        "input_tokens": 100 if variant == "baseline" else 50,
                        "uncached_input_tokens": 100 if variant == "baseline" else 50,
                        "output_tokens": 10 if variant == "baseline" else 5,
                    },
                },
                "grade": {
                    "passed": 1 if passed else 0,
                    "total": 1,
                    "acceptance_passed": 1 if passed else 0,
                    "acceptance_total": 1,
                    "task_success": passed,
                },
                "cost_usd": None,
                "error": None,
            }

        implementation = {
            "run_id": "implementation",
            "provider": "codex",
            "model": "test-model",
            "reasoning_effort": "medium",
            "semantic_source": "generated",
            "repetitions": 1,
            "cases": [case.id],
            "static": benchmark_module.static_rows([case]),
            "results": [
                implementation_result("baseline", True),
                implementation_result("semantic", False),
            ],
        }
        report = lifecycle.render_report(generation, implementation)
        self.assertIn("not applicable", report)
        self.assertIn("regressed measured implementation quality", report)

    def test_tree_hash_ignores_python_bytecode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="semantic-spec-hash-") as directory:
            root = Path(directory)
            (root / "source.py").write_text("value = 1\n", encoding="utf-8")
            expected = benchmark_module.tree_sha256(root)
            cache = root / "__pycache__"
            cache.mkdir()
            (cache / "source.cpython-312.pyc").write_bytes(b"generated")
            self.assertEqual(benchmark_module.tree_sha256(root), expected)

    def test_safe_workspace_commits_clean_baseline(self) -> None:
        case = benchmark_module.discover_cases(["email-routing"])[0]
        with tempfile.TemporaryDirectory(prefix="staged-workspace-") as directory:
            workspace = benchmark_module.safe_workspace(case, Path(directory))
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=workspace,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(status.stdout, "")
            entrypoint = workspace / case.manifest["entrypoint"]
            entrypoint.write_text("changed = True\n", encoding="utf-8")
            changed = subprocess.run(
                ["git", "diff", "--name-only"],
                cwd=workspace,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(changed.stdout.strip(), case.manifest["entrypoint"])


class GraderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.grader = load_module("benchmark_grader", ROOT / "benchmarks" / "grader.py")

    def test_strict_equal_rejects_json_type_substitution(self) -> None:
        self.assertFalse(self.grader.strict_equal(True, 1))
        self.assertFalse(self.grader.strict_equal(1.0, 1))
        self.assertTrue(self.grader.strict_equal({"value": [1]}, {"value": [1]}))

    def test_package_entrypoint_supports_relative_imports(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grader-package-") as directory:
            root = Path(directory)
            case_dir = root / "case"
            workspace = root / "workspace"
            package = workspace / "package"
            case_dir.mkdir()
            package.mkdir(parents=True)
            (case_dir / "case.json").write_text(
                json.dumps({"entrypoint": "package/service.py"}),
                encoding="utf-8",
            )
            (case_dir / "tests.json").write_text(
                json.dumps({
                    "tests": [{
                        "name": "relative_import",
                        "acceptance": "A1",
                        "call": "value",
                        "expect": 7,
                    }]
                }),
                encoding="utf-8",
            )
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "models.py").write_text("VALUE = 7\n", encoding="utf-8")
            (package / "service.py").write_text(
                "from .models import VALUE\n\n"
                "def value():\n"
                "    return VALUE\n",
                encoding="utf-8",
            )
            grade = self.grader.grade(case_dir, workspace)
            self.assertTrue(grade["task_success"], grade)

    def test_namespace_package_overrides_standard_library_collision(self) -> None:
        import email  # noqa: F401 - preload the colliding standard-library package

        with tempfile.TemporaryDirectory(prefix="grader-namespace-") as directory:
            root = Path(directory)
            case_dir = root / "case"
            workspace = root / "workspace"
            package = workspace / "email"
            case_dir.mkdir()
            package.mkdir(parents=True)
            (case_dir / "case.json").write_text(
                json.dumps({"entrypoint": "email/service.py"}),
                encoding="utf-8",
            )
            (case_dir / "tests.json").write_text(
                json.dumps({
                    "tests": [{
                        "name": "namespace_relative_import",
                        "acceptance": "A1",
                        "call": "value",
                        "expect": 13,
                    }]
                }),
                encoding="utf-8",
            )
            (package / "models.py").write_text("VALUE = 13\n", encoding="utf-8")
            (package / "service.py").write_text(
                "from .models import VALUE\n\n"
                "def value():\n"
                "    return VALUE\n",
                encoding="utf-8",
            )

            grade = self.grader.grade(case_dir, workspace)
            self.assertTrue(grade["task_success"], grade)

    def test_package_init_entrypoint_supports_relative_imports(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grader-package-init-") as directory:
            root = Path(directory)
            case_dir = root / "case"
            workspace = root / "workspace"
            package = workspace / "package"
            case_dir.mkdir()
            package.mkdir(parents=True)
            (case_dir / "case.json").write_text(
                json.dumps({"entrypoint": "package/__init__.py"}),
                encoding="utf-8",
            )
            (case_dir / "tests.json").write_text(
                json.dumps({
                    "tests": [{
                        "name": "relative_import",
                        "acceptance": "A1",
                        "call": "value",
                        "expect": 11,
                    }]
                }),
                encoding="utf-8",
            )
            (package / "models.py").write_text("VALUE = 11\n", encoding="utf-8")
            (package / "__init__.py").write_text(
                "from .models import VALUE\n\n"
                "def value():\n"
                "    return VALUE\n",
                encoding="utf-8",
            )
            grade = self.grader.grade(case_dir, workspace)
            self.assertTrue(grade["task_success"], grade)


if __name__ == "__main__":
    unittest.main()
