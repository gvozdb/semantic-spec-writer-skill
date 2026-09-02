from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
BENCHMARKS = ROOT / "benchmarks"
BENCHMARK = BENCHMARKS / "benchmark.py"
CONTEXT = BENCHMARKS / "context.py"
HANDOFF = BENCHMARKS / "handoff.py"
LIFECYCLE = BENCHMARKS / "lifecycle.py"
HANDOFF_CASES = BENCHMARKS / "handoff-cases"
PUBLISHED_GENERATED = (
    BENCHMARKS
    / "results"
    / "published"
    / "gpt-5.6-terra-medium-20260901-generated"
)
PUBLISHED_PACKET = (
    BENCHMARKS
    / "results"
    / "published"
    / "gpt-5.6-terra-medium-20260902-execution-packet"
)

sys.path.insert(0, str(BENCHMARKS))
import benchmark as core


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EvidenceIntegrityTest(unittest.TestCase):
    def test_exact_attestation_rejects_oversized_payload(self) -> None:
        with mock.patch.object(core, "MAX_ATTESTED_ARTIFACT_BYTES", 4):
            with mock.patch.object(
                core.base64,
                "b64decode",
                wraps=core.base64.b64decode,
            ) as decode:
                with self.assertRaisesRegex(ValueError, "exceeds the 4-byte limit"):
                    core.attested_bytes(core.attest_bytes(b"12345"))
            decode.assert_not_called()

    def test_stable_fd_growth_probe_reads_at_most_limit_plus_one(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bounded-stable-read-") as directory:
            path = Path(directory) / "input"
            path.write_bytes(b"1234")
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
            requests: list[int] = []

            def growing_read(_descriptor: int, count: int) -> bytes:
                requests.append(count)
                return b"1234" if len(requests) == 1 else b"x"

            try:
                with mock.patch.object(os, "read", side_effect=growing_read):
                    with self.assertRaisesRegex(RuntimeError, "exceeds the 4-byte limit"):
                        core._read_stable_regular_fd(
                            descriptor,
                            "growing input",
                            max_bytes=4,
                        )
            finally:
                os.close(descriptor)
            self.assertEqual(requests, [5, 1])

    def test_pinned_json_rejects_oversized_input_before_reading(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oversized-result-") as directory:
            artifact = Path(directory) / "result.json"
            artifact.write_bytes(b'{"value":1}\n')
            with (
                mock.patch.object(core, "MAX_BENCHMARK_RESULT_BYTES", 4),
                mock.patch.object(os, "read", wraps=os.read) as read,
            ):
                with self.assertRaisesRegex(RuntimeError, "exceeds the 4-byte limit"):
                    core.open_pinned_json(artifact)
            read.assert_not_called()

    def _credible_context_document(self, context):
        cases = core.discover_cases()
        semantic_specs = {
            case.id: case.spec_path("semantic").read_text(encoding="utf-8")
            for case in cases
        }
        args = Namespace(
            provider="codex",
            model="evidence-test-model",
            reasoning_effort="medium",
            repetitions=3,
            seed=20260901,
            cases_dir=None,
            semantic_dir=None,
        )
        inputs = context.snapshot_context_inputs(
            cases,
            semantic_specs,
            require_curated_semantic=True,
        )
        document = context.create_document(args, cases, semantic_specs, inputs)
        results = []
        for run_order, (case, repetition, variant) in enumerate(
            context.paired_job_schedule(cases, args.repetitions, args.seed),
            start=1,
        ):
            snapshot = document["fixture_snapshot"][case.id]
            results.append({
                "case": case.id,
                "pair_id": f"{case.id}:r{repetition}",
                "variant": variant,
                "repetition": repetition,
                "run_order": run_order,
                "spec": dict(snapshot["metrics"][variant]),
                "prompt": dict(snapshot["prompt_metrics"][variant]),
                "provenance": {
                    "spec_sha256": snapshot["variants"][variant],
                    "prompt_sha256": snapshot["prompts"][variant],
                    "fixture_sha256": snapshot["fixture_sha256"],
                },
                "provider": {
                    "return_code": 0,
                    "duration_seconds": 1.0,
                    "usage": {
                        "input_tokens": 100,
                        "uncached_input_tokens": 100,
                        "output_tokens": 1,
                    },
                    "tool_calls": {},
                    "tool_call_total": 0,
                    "event_errors": [],
                    "final_message_metadata": core.text_metadata("OK"),
                },
                "error": None,
            })
        document["results"] = results
        return document

    def test_context_snapshot_bytes_survive_source_mutation_between_arms(self) -> None:
        context = load_module("context_snapshot_mutation", CONTEXT)
        # Keep the result outside the system temporary directory: the core
        # publisher intentionally pins output ancestry, while the benchmark's
        # disposable workspaces are themselves created under the system temp
        # root during this test.
        with tempfile.TemporaryDirectory(
            prefix="context-source-mutation-",
            dir=ROOT.parent,
        ) as directory:
            root = Path(directory)
            cases_dir = root / "cases"
            case_dir = cases_dir / "email-routing"
            shutil.copytree(BENCHMARKS / "cases" / "email-routing", case_dir)
            baseline = case_dir / "baseline.md"
            semantic = case_dir / "semantic.spec.ctx"
            original_baseline = baseline.read_bytes()
            original_semantic = semantic.read_bytes()
            observed_specifications: list[bytes] = []
            original_mock_provider = context.mock_provider

            def mutate_after_first_arm(specification: str):
                observed_specifications.append(specification.encode("utf-8"))
                if len(observed_specifications) == 1:
                    baseline.write_bytes(original_baseline + b"\nMUTATED AFTER SNAPSHOT\n")
                    semantic.write_bytes(original_semantic + b"\nMUTATED AFTER SNAPSHOT\n")
                return original_mock_provider(specification)

            output = root / "context.json"
            args = Namespace(
                provider="mock",
                model=None,
                reasoning_effort=None,
                case=["email-routing"],
                cases_dir=cases_dir,
                semantic_dir=None,
                repetitions=1,
                seed=20260901,
                timeout_seconds=30,
                output=output,
                force=False,
            )
            with mock.patch.object(
                context,
                "mock_provider",
                side_effect=mutate_after_first_arm,
            ):
                context.run(args)

            document = json.loads(output.read_text(encoding="utf-8"))
            snapshot = document["fixture_snapshot"]["email-routing"]
            self.assertEqual(len(observed_specifications), 2)
            self.assertEqual(len(document["results"]), 2)
            self.assertEqual(baseline.read_bytes(), original_baseline + b"\nMUTATED AFTER SNAPSHOT\n")
            self.assertEqual(semantic.read_bytes(), original_semantic + b"\nMUTATED AFTER SNAPSHOT\n")
            for result, supplied_bytes in zip(document["results"], observed_specifications):
                variant = result["variant"]
                self.assertNotIn(b"MUTATED AFTER SNAPSHOT", supplied_bytes)
                self.assertEqual(
                    core.sha256_bytes(supplied_bytes),
                    snapshot["variants"][variant],
                )
                self.assertEqual(result["spec"], snapshot["metrics"][variant])
                self.assertEqual(
                    result["provenance"]["spec_sha256"],
                    snapshot["variants"][variant],
                )

    def test_context_capture_closes_hash_read_hash_transient_substitution(self) -> None:
        context = load_module("context_transient_capture", CONTEXT)
        with tempfile.TemporaryDirectory(prefix="context-transient-") as directory:
            cases_dir = Path(directory) / "cases"
            case_dir = cases_dir / "email-routing"
            shutil.copytree(BENCHMARKS / "cases" / "email-routing", case_dir)
            case = core.discover_cases(["email-routing"], cases_dir=cases_dir)[0]
            baseline = case_dir / "baseline.md"
            original = baseline.read_bytes()
            transient = b"transient substituted baseline\n"
            calls = 0

            def obsolete_hash_gap(path: Path) -> str:
                nonlocal calls
                calls += 1
                if calls == 1:
                    digest = core.tree_sha256(path)
                    baseline.write_bytes(transient)
                    return digest
                if calls == 2:
                    baseline.write_bytes(original)
                return core.tree_sha256(path)

            semantic = case.spec_path("semantic").read_text(encoding="utf-8")
            with mock.patch.object(
                context.core,
                "tree_sha256",
                side_effect=obsolete_hash_gap,
            ):
                inputs = context.snapshot_context_inputs(
                    [case],
                    {case.id: semantic},
                    require_curated_semantic=True,
                )
            self.assertEqual(calls, 0)
            self.assertEqual(inputs.specifications[case.id]["baseline"], original)
            self.assertEqual(baseline.read_bytes(), original)

    def test_context_gate_rejects_forged_snapshot_evidence_and_row_sets(self) -> None:
        context = load_module("context_evidence_gate", CONTEXT)
        document = self._credible_context_document(context)
        self.assertTrue(context.report_run_is_credible(document, document["results"]))

        forged_provenance = copy.deepcopy(document)
        forged_provenance["results"][0]["provenance"]["spec_sha256"] = "0" * 64
        self.assertFalse(
            context.report_run_is_credible(
                forged_provenance,
                forged_provenance["results"],
            )
        )

        forged_metrics = copy.deepcopy(document)
        forged_metrics["results"][0]["prompt"]["bytes"] += 1
        self.assertFalse(
            context.report_run_is_credible(forged_metrics, forged_metrics["results"])
        )

        forged_static = copy.deepcopy(document)
        forged_static["static"][0]["baseline"]["bytes"] += 1
        self.assertFalse(
            context.report_run_is_credible(forged_static, forged_static["results"])
        )

        duplicate_replacing_missing = copy.deepcopy(document)
        duplicate_replacing_missing["results"][-1] = copy.deepcopy(
            duplicate_replacing_missing["results"][0]
        )
        self.assertFalse(
            context.report_run_is_credible(
                duplicate_replacing_missing,
                duplicate_replacing_missing["results"],
            )
        )

        missing = copy.deepcopy(document)
        missing["results"].pop()
        self.assertFalse(context.report_run_is_credible(missing, missing["results"]))

        forged_schedule = copy.deepcopy(document)
        left, right = forged_schedule["results"][:2]
        left["run_order"], right["run_order"] = right["run_order"], left["run_order"]
        self.assertFalse(
            context.report_run_is_credible(forged_schedule, forged_schedule["results"])
        )

        generated = copy.deepcopy(document)
        generated["semantic_source"] = "generated"
        self.assertTrue(
            context.report_run_is_credible(generated, generated["results"])
        )

        shape_only_hash = copy.deepcopy(generated)
        case_id = shape_only_hash["cases"][0]
        shape_only_hash["fixture_snapshot"][case_id]["variants"]["semantic"] = (
            "0" * 64
        )
        for result in shape_only_hash["results"]:
            if result["case"] == case_id and result["variant"] == "semantic":
                result["provenance"]["spec_sha256"] = "0" * 64
        self.assertFalse(
            context.report_run_is_credible(
                shape_only_hash,
                shape_only_hash["results"],
            )
        )

        forged_bytes = copy.deepcopy(generated)
        forged_bytes["fixture_snapshot"][case_id]["specifications"]["semantic"] = (
            core.attest_text("forged generated specification\n")
        )
        self.assertFalse(
            context.report_run_is_credible(forged_bytes, forged_bytes["results"])
        )

    def _credible_capsule_document(self, handoff):
        revision_check = mock.patch.object(
            handoff.core,
            "git_revision_attestation_is_valid",
            return_value=True,
        )
        revision_check.start()
        self.addCleanup(revision_check.stop)
        config = handoff.CAPSULE_V5
        cases = core.discover_cases(cases_dir=HANDOFF_CASES)
        args = Namespace(
            provider="codex",
            model="evidence-test-model",
            reasoning_effort="medium",
            repetitions=3,
            seed=20260901,
        )
        document = handoff.create_document(
            args,
            cases,
            list(config.variants),
            config,
        )
        artifacts = {
            (case.id, variant): handoff.artifact_text(case, variant, config)
            for case in cases
            for variant in config.variants
        }
        results = []
        for run_order, (case, repetition, variant) in enumerate(
            handoff.paired_job_schedule(
                cases,
                args.repetitions,
                list(config.variants),
                args.seed,
            ),
            start=1,
        ):
            specification = artifacts[(case.id, variant)]
            snapshot = document["fixture_snapshot"][case.id]
            results.append({
                "case": case.id,
                "pair_id": f"{case.id}:r{repetition}",
                "variant": variant,
                "repetition": repetition,
                "run_order": run_order,
                "spec": core.text_metrics(specification),
                "provenance": handoff.result_provenance(
                    snapshot,
                    variant,
                    specification,
                    config,
                ),
                "provider": {
                    "return_code": 0,
                    "duration_seconds": 1.0,
                    "usage": {
                        "input_tokens": 100,
                        "uncached_input_tokens": 100,
                        "output_tokens": 10,
                    },
                    "tool_calls": {"command_execution": 1},
                    "tool_call_total": 1,
                    "event_errors": [],
                    "command_categories": {
                        "discovery": 0,
                        "read": 0,
                        "verify": 0,
                    },
                    "pre_edit_command_categories": {
                        "discovery": 0,
                        "read": 0,
                        "verify": 0,
                    },
                    "pre_edit_telemetry": {
                        "schema_version": 2,
                        "status": "routed_edit_observed",
                        "target_count": 1,
                        "file_change_events": 1,
                        "unclassified_file_change_events": 0,
                        "substantive_file_change_events": 1,
                    },
                },
                "verification": {
                    "command_metadata": core.text_metadata(
                        case.manifest["verification_command"]
                    ),
                    "fixture_sha256": snapshot["verification_fixture_sha256"],
                    "return_code": 0,
                },
                "grade": {
                    "passed": snapshot["grading"]["test_total"],
                    "total": snapshot["grading"]["test_total"],
                    "pass_rate": 1.0,
                    "acceptance_passed": snapshot["grading"][
                        "acceptance_total"
                    ],
                    "acceptance_total": snapshot["grading"][
                        "acceptance_total"
                    ],
                    "acceptance_pass_rate": 1.0,
                    "task_success": True,
                    "failures": [],
                },
                "error": None,
            })
        document["results"] = results
        return document

    def test_capsule_gate_requires_current_redacted_grade_and_routed_edit_telemetry(self) -> None:
        handoff = load_module("handoff_capsule_evidence_gate", HANDOFF)
        document = self._credible_capsule_document(handoff)
        self.assertTrue(handoff.capsule_report_is_credible(document, document["results"]))

        with mock.patch.object(
            handoff.core,
            "git_revision_attestation_is_valid",
            return_value=False,
        ):
            self.assertFalse(
                handoff.capsule_report_is_credible(
                    document,
                    document["results"],
                )
            )

        raw_grade = copy.deepcopy(document)
        raw_grade["results"][0]["grade"]["acceptance"] = {"A1": True}
        self.assertFalse(
            handoff.capsule_report_is_credible(raw_grade, raw_grade["results"])
        )

        no_routed_edit_attestation = copy.deepcopy(document)
        del no_routed_edit_attestation["results"][0]["provider"][
            "pre_edit_telemetry"
        ]
        self.assertFalse(
            handoff.capsule_report_is_credible(
                no_routed_edit_attestation,
                no_routed_edit_attestation["results"],
            )
        )

        pathless_attestation = copy.deepcopy(document)
        pathless_attestation["results"][0]["provider"]["pre_edit_telemetry"][
            "status"
        ] = "unavailable"
        self.assertFalse(
            handoff.capsule_report_is_credible(
                pathless_attestation,
                pathless_attestation["results"],
            )
        )

    def test_current_capsule_release_validation_consumes_exact_pair(self) -> None:
        handoff = load_module("handoff_capsule_release", HANDOFF)
        document = self._credible_capsule_document(handoff)
        self.assertNotIn("token_encoding", document)
        self.assertTrue(
            all(
                "tokens" not in metrics
                for row in document["static"]
                for metrics in row["variants"].values()
            )
        )
        rendered = handoff.report(document).encode("utf-8")
        with (
            mock.patch.object(
                handoff.core,
                "validate",
                side_effect=AssertionError(
                    "release validation must not execute fixture validation"
                ),
            ),
            mock.patch.object(
                handoff.core,
                "run_grader",
                side_effect=AssertionError(
                    "release validation must not execute fixture graders"
                ),
            ),
            mock.patch.object(
                handoff.core,
                "run_verification",
                side_effect=AssertionError(
                    "release validation must not execute fixture commands"
                ),
            ),
        ):
            self.assertEqual(
                handoff.validate_capsule_release(document, rendered),
                [],
            )
        self.assertIn(
            "Capsule report is not the exact rendering of its result",
            handoff.validate_capsule_release(document, rendered + b"tampered\n"),
        )
        forged_tokens = copy.deepcopy(document)
        for metrics in forged_tokens["static"][0]["variants"].values():
            metrics["tokens"] = 1
        self.assertFalse(
            handoff.capsule_report_is_credible(
                forged_tokens,
                forged_tokens["results"],
            )
        )

    def test_current_capsule_release_rejects_provider_schema_injections(self) -> None:
        handoff = load_module("handoff_capsule_schema_injections", HANDOFF)
        document = self._credible_capsule_document(handoff)
        rendered = handoff.report(document).encode("utf-8")
        empty_hash = core.sha256_bytes(b"")

        redacted_command_log = copy.deepcopy(document)
        redacted_command_log["results"][0]["provider"]["command_log"] = [{
            "categories": {
                "discovery": False,
                "read": True,
                "verify": False,
            },
            "command_bytes": 11,
            "command_sha256": core.sha256_bytes(b"cat private"),
            "exit_code": 0,
            "pre_edit": True,
        }]
        self.assertTrue(
            handoff.capsule_report_is_credible(
                redacted_command_log,
                redacted_command_log["results"],
            )
        )

        def assert_rejected(name, mutate) -> None:
            forged = copy.deepcopy(document)
            mutate(forged["results"][0])
            with self.subTest(name=name):
                self.assertFalse(
                    handoff.capsule_report_is_credible(
                        forged,
                        forged["results"],
                    )
                )
                self.assertIn(
                    "Capsule result does not satisfy current credibility gates",
                    handoff.validate_capsule_release(forged, rendered),
                )

        for raw_key in (
            "stderr_tail",
            "stdout_tail",
            "command",
            "final_message",
            "unknown_provider_field",
        ):
            assert_rejected(
                raw_key,
                lambda result, key=raw_key: result["provider"].__setitem__(
                    key,
                    "private provider text",
                ),
            )

        assert_rejected(
            "unknown usage",
            lambda result: result["provider"]["usage"].__setitem__(
                "private_tokens",
                1,
            ),
        )
        assert_rejected(
            "unknown tool call",
            lambda result: result["provider"]["tool_calls"].__setitem__(
                "private_tool",
                1,
            ),
        )
        assert_rejected(
            "raw provider error",
            lambda result: result["provider"].__setitem__(
                "event_errors",
                ["private provider error"],
            ),
        )
        assert_rejected(
            "metadata nesting",
            lambda result: result["provider"].__setitem__(
                "final_message_metadata",
                {"bytes": 0, "sha256": empty_hash, "raw": "private"},
            ),
        )
        assert_rejected(
            "telemetry nesting",
            lambda result: result["provider"]["pre_edit_telemetry"].__setitem__(
                "raw",
                {"command": "private"},
            ),
        )
        assert_rejected(
            "command log prose",
            lambda result: result["provider"].__setitem__(
                "command_log",
                [{
                    "categories": {
                        "discovery": False,
                        "read": True,
                        "verify": False,
                    },
                    "command_bytes": 11,
                    "command_sha256": core.sha256_bytes(b"cat private"),
                    "exit_code": 0,
                    "pre_edit": True,
                    "command": "cat private",
                }],
            ),
        )
        assert_rejected(
            "raw verification command",
            lambda result: result["verification"].__setitem__(
                "command",
                "private verification command",
            ),
        )
        assert_rejected(
            "grade nesting",
            lambda result: result["grade"].__setitem__(
                "private",
                {"expected": "hidden"},
            ),
        )

    def _assert_report_alias_is_rejected(
        self,
        script: Path,
        arguments: list[str],
        input_artifact: Path,
        directory: Path,
    ) -> None:
        alias = directory / f"{script.stem}-alias.md"
        os.link(input_artifact, alias)
        before = input_artifact.read_bytes()
        completed = subprocess.run(
            [sys.executable, str(script), *arguments, "--output", str(alias), "--force"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn("aliases input", completed.stderr)
        self.assertEqual(input_artifact.read_bytes(), before)
        self.assertEqual(alias.read_bytes(), before)

    def test_reports_reject_hard_linked_input_aliases_even_with_force(self) -> None:
        with tempfile.TemporaryDirectory(prefix="report-input-alias-") as directory:
            root = Path(directory)
            context_input = root / "context.json"
            packet_input = root / "handoff.json"
            generation_input = root / "generation.json"
            implementation_input = root / "implementation.json"
            shutil.copy2(PUBLISHED_GENERATED / "context-r3.json", context_input)
            shutil.copy2(PUBLISHED_PACKET / "handoff-r3.json", packet_input)
            shutil.copy2(PUBLISHED_GENERATED / "generation.json", generation_input)
            shutil.copy2(
                PUBLISHED_GENERATED / "implementation-r3.json",
                implementation_input,
            )

            self._assert_report_alias_is_rejected(
                CONTEXT,
                ["report", str(context_input)],
                context_input,
                root,
            )
            self._assert_report_alias_is_rejected(
                BENCHMARK,
                ["report", str(implementation_input)],
                implementation_input,
                root,
            )
            self._assert_report_alias_is_rejected(
                HANDOFF,
                ["report", str(packet_input)],
                packet_input,
                root,
            )
            self._assert_report_alias_is_rejected(
                LIFECYCLE,
                ["report", str(generation_input), str(implementation_input)],
                implementation_input,
                root,
            )

    def test_pinned_report_input_closes_read_then_resnapshot_alias_race(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="report-input-swap-",
            dir=ROOT.parent,
        ) as directory:
            root = Path(directory)
            result = root / "result.json"
            replacement = root / "replacement.json"
            alias = root / "report.md"
            original = b'{"run_id":"pinned"}\n'
            result.write_bytes(original)
            replacement.write_bytes(b'{"run_id":"replacement"}\n')
            os.link(result, alias)

            with core.open_pinned_json(result) as pinned:
                self.assertEqual(pinned.document, {"run_id": "pinned"})
                os.replace(replacement, result)
                with self.assertRaisesRegex(RuntimeError, "result input changed"):
                    core.write_report_from_pinned_inputs(
                        alias,
                        "forged report\n",
                        overwrite=True,
                        inputs=[pinned],
                    )

            self.assertEqual(alias.read_bytes(), original)
            self.assertEqual(result.read_bytes(), b'{"run_id":"replacement"}\n')

    def test_pair_schedules_counterbalance_each_fixture(self) -> None:
        context = load_module("context_counterbalance", CONTEXT)
        handoff = load_module("handoff_counterbalance", HANDOFF)
        implementation_cases = core.discover_cases()
        handoff_cases = core.discover_cases(cases_dir=HANDOFF_CASES)

        schedules = (
            (
                context.paired_job_schedule(
                    implementation_cases,
                    3,
                    20260901,
                ),
                ("baseline", "semantic"),
            ),
            (
                core.implementation_job_schedule(
                    implementation_cases,
                    3,
                    20260901,
                ),
                ("baseline", "semantic"),
            ),
            (
                handoff.paired_job_schedule(
                    handoff_cases,
                    3,
                    list(handoff.CAPSULE_V5.variants),
                    20260901,
                ),
                handoff.CAPSULE_V5.variants,
            ),
            (
                handoff.paired_job_schedule(
                    handoff_cases,
                    3,
                    list(handoff.PACKET_V3.variants),
                    20260901,
                ),
                handoff.PACKET_V3.variants,
            ),
        )
        for schedule, variants in schedules:
            first: dict[tuple[str, int], str] = {}
            for case, repetition, variant in schedule:
                case_id = case.id if hasattr(case, "id") else str(case)
                first.setdefault((case_id, repetition), variant)
            for case_id in {key[0] for key in first}:
                counts = [
                    sum(
                        first[(case_id, repetition)] == variant
                        for repetition in range(1, 4)
                    )
                    for variant in variants
                ]
                self.assertLessEqual(max(counts) - min(counts), 1)
                if len(variants) == 2:
                    self.assertEqual(sorted(counts), [1, 2])
                else:
                    self.assertEqual(counts, [1, 1, 1])

    def test_implementation_workspace_materializes_captured_starter_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="implementation-capture-") as directory:
            root = Path(directory)
            cases_dir = root / "cases"
            case_dir = cases_dir / "email-routing"
            shutil.copytree(BENCHMARKS / "cases" / "email-routing", case_dir)
            case = core.discover_cases(["email-routing"], cases_dir=cases_dir)[0]
            semantic = case.spec_path("semantic").read_text(encoding="utf-8")
            document = core.create_run_document(
                Namespace(
                    provider="mock",
                    model=None,
                    reasoning_effort=None,
                    repetitions=1,
                    seed=20260901,
                    pricing={},
                    semantic_dir=None,
                ),
                [case],
                {case.id: semantic},
                cases_dir,
            )
            source = case_dir / "starter" / "solution.py"
            captured = source.read_bytes()
            source.write_bytes(b"transient replacement workspace\n")
            run_root = root / "run"
            run_root.mkdir()
            workspace = core.safe_workspace(
                case,
                run_root,
                starter_snapshot=document.starter_snapshots[case.id],
            )
            self.assertEqual((workspace / "solution.py").read_bytes(), captured)

    def test_implementation_grade_ignores_transient_live_oracle_substitution(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="implementation-grading-snapshot-",
            dir=ROOT.parent,
        ) as directory:
            root = Path(directory)
            cases_dir = root / "cases"
            case_dir = cases_dir / "email-routing"
            shutil.copytree(BENCHMARKS / "cases" / "email-routing", case_dir)
            case_json = case_dir / "case.json"
            tests_json = case_dir / "tests.json"
            original_case = case_json.read_bytes()
            original_tests = tests_json.read_bytes()
            transient_case = json.loads(original_case)
            transient_case["entrypoint"] = "transient_oracle.py"
            transient_tests = {
                "tests": [{
                    "name": "transient oracle",
                    "acceptance": "AX",
                    "call": "transient_only",
                    "expect": "forged",
                }]
            }
            real_grader = core.run_grader
            swaps = 0

            def grade_during_substitution(case, workspace, **kwargs):
                nonlocal swaps
                if kwargs.get("grading_snapshot") is None:
                    return real_grader(case, workspace, **kwargs)
                swaps += 1
                self.assertFalse((workspace / "case.json").exists())
                self.assertFalse((workspace / "tests.json").exists())
                case_json.write_text(json.dumps(transient_case), encoding="utf-8")
                tests_json.write_text(json.dumps(transient_tests), encoding="utf-8")
                try:
                    return real_grader(case, workspace, **kwargs)
                finally:
                    case_json.write_bytes(original_case)
                    tests_json.write_bytes(original_tests)

            output = root / "implementation.json"
            args = Namespace(
                provider="mock",
                model=None,
                reasoning_effort=None,
                repetitions=1,
                seed=20260901,
                pricing={},
                pricing_file=None,
                semantic_dir=None,
                cases_dir=cases_dir,
                case=["email-routing"],
                timeout_seconds=30,
                output=output,
                force=False,
                keep_workspaces=False,
            )
            with mock.patch.object(core, "run_grader", side_effect=grade_during_substitution):
                core.execute_benchmark(args)

            document = json.loads(output.read_text(encoding="utf-8"))
            expected = document["fixture_snapshot"]["email-routing"]["grading"]
            self.assertEqual(swaps, 2)
            self.assertEqual(case_json.read_bytes(), original_case)
            self.assertEqual(tests_json.read_bytes(), original_tests)
            self.assertTrue(all(result["grade"]["task_success"] for result in document["results"]))
            self.assertTrue(
                all(
                    result["grade"]["total"] == expected["test_total"]
                    and result["grade"]["acceptance_total"]
                    == expected["acceptance_total"]
                    for result in document["results"]
                )
            )

    def test_capsule_grade_ignores_transient_live_oracle_substitution(self) -> None:
        handoff = load_module("handoff_grading_snapshot", HANDOFF)
        with tempfile.TemporaryDirectory(
            prefix="capsule-grading-snapshot-",
            dir=ROOT.parent,
        ) as directory:
            root = Path(directory)
            cases_dir = root / "handoff-cases"
            case_dir = cases_dir / "tenant-settings"
            shutil.copytree(HANDOFF_CASES / "tenant-settings", case_dir)
            case_json = case_dir / "case.json"
            tests_json = case_dir / "tests.json"
            original_case = case_json.read_bytes()
            original_tests = tests_json.read_bytes()
            transient_case = json.loads(original_case)
            transient_case["entrypoint"] = "transient_oracle.py"
            transient_tests = {
                "tests": [{
                    "name": "transient oracle",
                    "acceptance": "AX",
                    "call": "transient_only",
                    "expect": "forged",
                }]
            }
            real_grader = core.run_grader
            swaps = 0

            def grade_during_substitution(case, workspace, **kwargs):
                nonlocal swaps
                if kwargs.get("grading_snapshot") is None:
                    return real_grader(case, workspace, **kwargs)
                swaps += 1
                self.assertFalse((workspace / "case.json").exists())
                self.assertFalse((workspace / "tests.json").exists())
                case_json.write_text(json.dumps(transient_case), encoding="utf-8")
                tests_json.write_text(json.dumps(transient_tests), encoding="utf-8")
                try:
                    return real_grader(case, workspace, **kwargs)
                finally:
                    case_json.write_bytes(original_case)
                    tests_json.write_bytes(original_tests)

            output = root / "capsule.json"
            args = Namespace(
                provider="mock",
                model=None,
                reasoning_effort=None,
                repetitions=1,
                seed=20260901,
                case=["tenant-settings"],
                comparison="capsule-v5",
                variant=[],
                timeout_seconds=30,
                output=output,
                force=False,
            )
            with (
                mock.patch.object(handoff, "CASES_DIR", cases_dir),
                mock.patch.object(
                    handoff.core,
                    "run_grader",
                    side_effect=grade_during_substitution,
                ),
            ):
                handoff.run(args)

            document = json.loads(output.read_text(encoding="utf-8"))
            expected = document["fixture_snapshot"]["tenant-settings"]["grading"]
            self.assertEqual(swaps, 2)
            self.assertEqual(case_json.read_bytes(), original_case)
            self.assertEqual(tests_json.read_bytes(), original_tests)
            self.assertTrue(all(result["grade"]["task_success"] for result in document["results"]))
            self.assertTrue(
                all(
                    result["grade"]["total"] == expected["test_total"]
                    and result["grade"]["acceptance_total"]
                    == expected["acceptance_total"]
                    for result in document["results"]
                )
            )

    def test_lifecycle_materializes_captured_source_skill_and_starter(self) -> None:
        lifecycle = load_module("lifecycle_exact_inputs", LIFECYCLE)
        with tempfile.TemporaryDirectory(prefix="lifecycle-capture-") as directory:
            root = Path(directory)
            cases_dir = root / "cases"
            case_dir = cases_dir / "email-routing"
            skill_dir = root / "skill"
            shutil.copytree(BENCHMARKS / "cases" / "email-routing", case_dir)
            shutil.copytree(ROOT / "skills" / "semantic-spec-writer", skill_dir)
            case = core.discover_cases(["email-routing"], cases_dir=cases_dir)[0]
            args = Namespace(
                provider="mock",
                model=None,
                reasoning_effort=None,
                token_encoding=None,
                max_attempts=1,
                cases_dir=cases_dir,
            )
            with mock.patch.object(lifecycle, "SKILL_DIR", skill_dir):
                document = lifecycle.generation_document(args, [case])
                source_bytes = lifecycle.generation_snapshot_source(
                    document["fixture_snapshot"][case.id],
                    case.id,
                )
                starter_file = case_dir / "starter" / "solution.py"
                skill_file = skill_dir / "SKILL.md"
                captured_starter = starter_file.read_bytes()
                captured_skill = skill_file.read_bytes()
                (case_dir / "baseline.md").write_bytes(b"transient source\n")
                starter_file.write_bytes(b"transient starter\n")
                skill_file.write_bytes(b"transient skill\n")

                workspace, repository = lifecycle.prepare_generation_workspace(
                    root / "attempt",
                    case,
                    source_bytes=source_bytes,
                    starter_snapshot=document.starter_snapshots[case.id],
                    skill_snapshot=document.skill_snapshot,
                )
            self.assertTrue(workspace.is_dir())
            self.assertEqual((repository.parent / "source.md").read_bytes(), source_bytes)
            self.assertEqual((repository / "solution.py").read_bytes(), captured_starter)
            self.assertEqual(
                (repository.parent / "skill" / "SKILL.md").read_bytes(),
                captured_skill,
            )

    def test_capsule_build_and_workspace_share_pre_substitution_snapshot(self) -> None:
        handoff = load_module("handoff_capsule_single_snapshot", HANDOFF)
        with tempfile.TemporaryDirectory(prefix="capsule-single-snapshot-") as directory:
            root = Path(directory)
            cases_dir = root / "handoff-cases"
            case_dir = cases_dir / "tenant-settings"
            shutil.copytree(HANDOFF_CASES / "tenant-settings", case_dir)
            case = core.discover_cases(["tenant-settings"], cases_dir=cases_dir)[0]
            routed = case_dir / "starter" / "settings" / "layers.py"
            original = routed.read_bytes()
            module = handoff.context_capsule_module()
            real_build = module.build_capsule

            def build_during_transient_substitution(repo, packet, **kwargs):
                routed.write_bytes(b"transient routed substitution\n")
                try:
                    return real_build(repo, packet, **kwargs)
                finally:
                    routed.write_bytes(original)

            args = Namespace(
                provider="mock",
                model=None,
                reasoning_effort=None,
                repetitions=1,
                seed=20260901,
            )
            with mock.patch.object(
                module,
                "build_capsule",
                side_effect=build_during_transient_substitution,
            ):
                document = handoff.create_document(
                    args,
                    [case],
                    list(handoff.CAPSULE_V5.variants),
                    handoff.CAPSULE_V5,
                )

            artifacts = handoff.capsule_snapshot_artifacts(
                document["fixture_snapshot"][case.id]
            )
            _, _, _, sources, _ = module._parse_capsule(
                artifacts["capsule"].encode("utf-8")
            )
            layers_frame = next(
                payload
                for descriptor, payload in sources
                if descriptor["path"] == "settings/layers.py"
            )
            self.assertEqual(layers_frame, original)
            self.assertNotIn(b"transient routed substitution", layers_frame)

            run_root = root / "run"
            run_root.mkdir()
            workspace = core.safe_workspace(
                case,
                run_root,
                starter_snapshot=document.starter_snapshots[case.id],
            )
            self.assertEqual(
                (workspace / "settings" / "layers.py").read_bytes(),
                original,
            )

    def test_capsule_snapshot_check_covers_ranges_and_create_absence(self) -> None:
        handoff = load_module("handoff_capsule_range_snapshot", HANDOFF)
        module = handoff.context_capsule_module()
        with tempfile.TemporaryDirectory(prefix="capsule-range-snapshot-") as directory:
            root = Path(directory)
            case_dir = root / "range-case"
            starter = case_dir / "starter"
            starter.mkdir(parents=True)
            source = starter / "source.py"
            source.write_bytes(b"first = 1\r\nvalue = 2\r\nlast = 3")
            packet = case_dir / "packet.spec.ctx"
            packet_text = (
                "spec\n"
                "route:\n"
                "  edit: source.py:2-2::value = 2\n"
                "    do: update the value\n"
                "  create: generated.py\n"
                "    do: add the generated module\n"
                "execution: routed read once -> all do -> V1 once -> stop on pass; "
                "expand only on contradiction/failure\n"
                "basis: route-sha256:" + "0" * 64 + "\n"
                "verify:\n"
                "  V1: `python3 -m py_compile source.py`\n"
            )
            targets = module.packet_checker.parse_routes(packet_text)
            route_hash = module.packet_checker.route_sha256(starter, targets)
            packet_bytes = packet_text.replace("0" * 64, route_hash).encode("utf-8")
            packet.write_bytes(packet_bytes)
            case = core.BenchmarkCase(
                case_dir,
                {
                    "id": "range-case",
                    "title": "Range case",
                    "entrypoint": "source.py",
                    "verification_files": [],
                },
            )
            captured = core.snapshot_fixture_tree(starter)
            capsule = module.build_capsule(starter, packet)
            handoff._check_capsule_snapshot(case, captured, packet_bytes, capsule)

            source.write_bytes(b"first = 1\r\nvalue = 9\r\nlast = 3")
            changed = core.snapshot_fixture_tree(starter)
            with self.assertRaisesRegex(RuntimeError, "validation failed"):
                handoff._check_capsule_snapshot(case, changed, packet_bytes, capsule)

            source.write_bytes(b"first = 1\r\nvalue = 2\r\nlast = 3")
            (starter / "generated.py").write_text("created = True\n", encoding="utf-8")
            create_present = core.snapshot_fixture_tree(starter)
            with self.assertRaisesRegex(RuntimeError, "validation failed"):
                handoff._check_capsule_snapshot(
                    case,
                    create_present,
                    packet_bytes,
                    capsule,
                )

    def test_capsule_frame_provenance_and_workspace_use_captured_bytes(self) -> None:
        handoff = load_module("handoff_capsule_fixture_snapshot", HANDOFF)
        with tempfile.TemporaryDirectory(prefix="capsule-fixture-snapshot-") as directory:
            root = Path(directory)
            cases_dir = root / "handoff-cases"
            shutil.copytree(HANDOFF_CASES, cases_dir)
            case = core.discover_cases(
                ["tenant-settings"],
                cases_dir=cases_dir,
            )[0]
            snapshot = handoff.case_snapshot(case, handoff.CAPSULE_V5)
            artifacts = handoff.capsule_snapshot_artifacts(snapshot)
            module = handoff.context_capsule_module()
            frame_hashes = handoff._capsule_source_hashes(
                module,
                artifacts["capsule"].encode("utf-8"),
            )
            self.assertEqual(frame_hashes, snapshot["capsule"]["source_hashes"])

            starter = core.snapshot_fixture_tree(
                case.path / "starter",
                expected_sha256=snapshot["starter_sha256"],
            )
            routed = case.path / "starter" / "settings" / "layers.py"
            captured = routed.read_bytes()
            routed.write_bytes(b"replacement supplied after validation\n")
            run_root = root / "run"
            run_root.mkdir()
            workspace = core.safe_workspace(
                case,
                run_root,
                starter_snapshot=starter,
            )
            self.assertEqual(
                (workspace / "settings" / "layers.py").read_bytes(),
                captured,
            )
            self.assertEqual(
                handoff._capsule_source_hashes(
                    module,
                    artifacts["capsule"].encode("utf-8"),
                ),
                frame_hashes,
            )

    def test_git_revision_attestation_accepts_later_artifact_commit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="benchmark-code-revision-") as directory:
            repository = Path(directory)
            subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
            subprocess.run(
                ["git", "config", "user.name", "Benchmark Test"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "benchmark@example.invalid"],
                cwd=repository,
                check=True,
            )
            required = (
                "benchmarks/handoff.py",
                "skills/semantic-spec-writer/scripts/context_capsule.py",
            )
            for relative in required:
                path = repository / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"# {relative}\n", encoding="utf-8")
            subprocess.run(["git", "add", "--all"], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "code revision"],
                cwd=repository,
                check=True,
            )

            with mock.patch.object(core, "ROOT", repository):
                attestation = core.git_revision_attestation(
                    required,
                    require_clean=True,
                )
                self.assertTrue(
                    all(
                        entry["git_mode"] == "100644"
                        and entry["git_type"] == "blob"
                        for entry in attestation["required_paths"].values()
                    )
                )
                artifact_directory = (
                    repository
                    / "benchmarks"
                    / "results"
                    / "published"
                    / "safe-run"
                )
                artifact_directory.mkdir(parents=True)
                (artifact_directory / "capsule-r3.json").write_text(
                    "{}\n", encoding="utf-8"
                )
                (artifact_directory / "CAPSULE.md").write_text(
                    "report\n", encoding="utf-8"
                )
                (repository / "CAPSULE_BENCHMARK.md").write_text(
                    "report\n", encoding="utf-8"
                )
                subprocess.run(["git", "add", "--all"], cwd=repository, check=True)
                subprocess.run(
                    ["git", "commit", "--quiet", "-m", "publish artifact"],
                    cwd=repository,
                    check=True,
                )
                self.assertTrue(
                    core.git_revision_attestation_is_valid(
                        attestation,
                        required,
                        environment_commit=attestation["commit"],
                    )
                )

                required_path = repository / required[0]
                original_required = required_path.read_bytes()
                original_mode = required_path.stat().st_mode & 0o777
                required_path.chmod(original_mode | 0o111)
                self.assertFalse(
                    core.git_revision_attestation_is_valid(
                        attestation,
                        required,
                        environment_commit=attestation["commit"],
                    )
                )
                required_path.chmod(original_mode)
                self.assertTrue(
                    core.git_revision_attestation_is_valid(
                        attestation,
                        required,
                        environment_commit=attestation["commit"],
                    )
                )

                required_path.chmod(original_mode | 0o111)
                subprocess.run(["git", "add", "--", required[0]], cwd=repository, check=True)
                subprocess.run(
                    ["git", "commit", "--quiet", "-m", "mode-only required change"],
                    cwd=repository,
                    check=True,
                )
                self.assertFalse(
                    core.git_revision_attestation_is_valid(
                        attestation,
                        required,
                        environment_commit=attestation["commit"],
                    )
                )
                required_path.chmod(original_mode)
                subprocess.run(["git", "add", "--", required[0]], cwd=repository, check=True)
                subprocess.run(
                    ["git", "commit", "--quiet", "-m", "restore required mode"],
                    cwd=repository,
                    check=True,
                )
                self.assertTrue(
                    core.git_revision_attestation_is_valid(
                        attestation,
                        required,
                        environment_commit=attestation["commit"],
                    )
                )

                required_path.unlink()
                required_path.symlink_to("handoff-target.py")
                subprocess.run(["git", "add", "--", required[0]], cwd=repository, check=True)
                subprocess.run(
                    ["git", "commit", "--quiet", "-m", "change required type"],
                    cwd=repository,
                    check=True,
                )
                self.assertFalse(
                    core.git_revision_attestation_is_valid(
                        attestation,
                        required,
                        environment_commit=attestation["commit"],
                    )
                )
                required_path.unlink()
                required_path.write_bytes(original_required)
                required_path.chmod(original_mode)
                subprocess.run(["git", "add", "--", required[0]], cwd=repository, check=True)
                subprocess.run(
                    ["git", "commit", "--quiet", "-m", "restore required type"],
                    cwd=repository,
                    check=True,
                )
                self.assertTrue(
                    core.git_revision_attestation_is_valid(
                        attestation,
                        required,
                        environment_commit=attestation["commit"],
                    )
                )

                required_path.write_text("# dirty required code\n", encoding="utf-8")
                self.assertFalse(
                    core.git_revision_attestation_is_valid(
                        attestation,
                        required,
                        environment_commit=attestation["commit"],
                    )
                )
                required_path.write_bytes(original_required)
                self.assertTrue(
                    core.git_revision_attestation_is_valid(
                        attestation,
                        required,
                        environment_commit=attestation["commit"],
                    )
                )

                required_path.write_text("# descendant code change\n", encoding="utf-8")
                subprocess.run(["git", "add", "--", required[0]], cwd=repository, check=True)
                subprocess.run(
                    ["git", "commit", "--quiet", "-m", "change required code"],
                    cwd=repository,
                    check=True,
                )
                self.assertFalse(
                    core.git_revision_attestation_is_valid(
                        attestation,
                        required,
                        environment_commit=attestation["commit"],
                    )
                )
                required_path.write_bytes(original_required)
                subprocess.run(["git", "add", "--", required[0]], cwd=repository, check=True)
                subprocess.run(
                    ["git", "commit", "--quiet", "-m", "restore required code"],
                    cwd=repository,
                    check=True,
                )
                self.assertTrue(
                    core.git_revision_attestation_is_valid(
                        attestation,
                        required,
                        environment_commit=attestation["commit"],
                    )
                )

                forged_hash = copy.deepcopy(attestation)
                forged_hash["required_paths"][required[0]]["sha256"] = "0" * 64
                self.assertFalse(
                    core.git_revision_attestation_is_valid(
                        forged_hash,
                        required,
                        environment_commit=attestation["commit"],
                    )
                )
                missing_path = copy.deepcopy(attestation)
                del missing_path["required_paths"][required[0]]
                self.assertFalse(
                    core.git_revision_attestation_is_valid(
                        missing_path,
                        required,
                        environment_commit=attestation["commit"],
                    )
                )
                missing_blob = copy.deepcopy(attestation)
                missing_blob["required_paths"]["benchmarks/missing.py"] = {
                    "git_mode": "100644",
                    "git_type": "blob",
                    "git_object": "0" * len(
                        next(iter(attestation["required_paths"].values()))[
                            "git_object"
                        ]
                    ),
                    "sha256": "0" * 64,
                }
                self.assertFalse(
                    core.git_revision_attestation_is_valid(
                        missing_blob,
                        (*required, "benchmarks/missing.py"),
                        environment_commit=attestation["commit"],
                    )
                )
                missing_revision = copy.deepcopy(attestation)
                missing_revision["commit"] = "0" * len(attestation["commit"])
                self.assertFalse(
                    core.git_revision_attestation_is_valid(
                        missing_revision,
                        required,
                        environment_commit=missing_revision["commit"],
                    )
                )

                (repository / required[0]).write_text(
                    "# dirty benchmark code\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(RuntimeError, "clean Git worktree"):
                    core.git_revision_attestation(required, require_clean=True)

    def test_historical_lifecycle_pair_renders_tracked_report_byte_for_byte(self) -> None:
        lifecycle = load_module("lifecycle_historical_bytes", LIFECYCLE)
        generation = json.loads(
            (PUBLISHED_GENERATED / "generation.json").read_text(encoding="utf-8")
        )
        implementation = json.loads(
            (PUBLISHED_GENERATED / "implementation-r3.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            lifecycle.render_report(generation, implementation).encode("utf-8"),
            (ROOT / "LIFECYCLE_BENCHMARK.md").read_bytes(),
        )

    def test_historical_context_report_renders_tracked_bytes_exactly(self) -> None:
        context = load_module("context_historical_bytes", CONTEXT)
        document = json.loads(
            (PUBLISHED_GENERATED / "context-r3.json").read_text(encoding="utf-8")
        )
        self.assertTrue(context.is_historical_context_document(document))
        self.assertEqual(
            context.report(document).encode("utf-8"),
            (ROOT / "CONTEXT_BENCHMARK.md").read_bytes(),
        )
        modified = copy.deepcopy(document)
        modified["run_id"] += "-modified"
        self.assertFalse(context.is_historical_context_document(modified))
        self.assertFalse(
            context.report_run_is_credible(modified, modified["results"])
        )


if __name__ == "__main__":
    unittest.main()
