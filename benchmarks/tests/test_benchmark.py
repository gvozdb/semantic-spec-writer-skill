from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "benchmarks" / "benchmark.py"


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
