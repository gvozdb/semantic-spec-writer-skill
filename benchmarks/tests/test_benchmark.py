from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "benchmarks" / "benchmark.py"
LIFECYCLE = ROOT / "benchmarks" / "lifecycle.py"
CONTEXT = ROOT / "benchmarks" / "context.py"
sys.path.insert(0, str(ROOT / "benchmarks"))
import benchmark as benchmark_module
CONVERSION_CHECK = (
    ROOT
    / "skills"
    / "semantic-spec-writer"
    / "scripts"
    / "check_conversion.py"
)


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


class GraderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.grader = load_module("benchmark_grader", ROOT / "benchmarks" / "grader.py")

    def test_strict_equal_rejects_json_type_substitution(self) -> None:
        self.assertFalse(self.grader.strict_equal(True, 1))
        self.assertFalse(self.grader.strict_equal(1.0, 1))
        self.assertTrue(self.grader.strict_equal({"value": [1]}, {"value": [1]}))


if __name__ == "__main__":
    unittest.main()
