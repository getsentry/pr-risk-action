import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from risk_pr_agent.input_eval import prepare_input_eval, pr_metadata
from risk_pr_agent.jev import build_request, score_snapshots


class InputEvaluationDatasetTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "schema_version": 2, "example_id": "example/cli#7", "repo": "example/cli", "number": 7,
            "split": "dev", "status": "ready", "missing": [], "strata": ["functional"],
            "snapshot": {"base_sha": "a" * 40, "head_sha": "b" * 40},
            "files": [{"path": "src/main.py", "status": "modified", "before": "old()\n", "after": "new()\n",
                       "patch": "@@ -1 +1 @@\n-old()\n+new()\n", "additions": 1, "deletions": 1,
                       "outcome": "PRIVATE_OUTCOME"}],
            "repository_context": {}, "outcome": "PRIVATE_OUTCOME", "risk_label": "high",
        }
        self.reference = {"example_id": self.snapshot["example_id"], "repo": "example/cli", "split": "dev",
                          "snapshot": dict(self.snapshot["snapshot"]), "source": "agent", "review_status": "proposed",
                          "proposed_risk_label": "medium", "rationale": "PRIVATE_REFERENCE",
                          "reviewer": "assistant", "reviewed_at": "2026-01-01", "rubric_version": "1"}
        self.raw = {"repo": "example/cli", "number": 7, "id": 123, "title": "Change widget", "body": "Description",
                    "fetched_at": "2026-01-01T00:00:00Z", "merged_at": "2025-01-01T00:00:00Z",
                    "author": "PRIVATE_AUTHOR", "labels": ["high"]}

    def test_source_complete_case_separates_reference_from_inference_inputs(self):
        with tempfile.TemporaryDirectory() as out:
            manifest = prepare_input_eval([self.snapshot], [self.reference], [self.raw], ["example/cli"], out)
            case = json.loads(Path(manifest["cases_path"]).read_text())
            inputs = Path(manifest["inputs"]["dev"]).read_text()
            self.assertEqual(case["expected"]["risk_label"], "medium")
            self.assertEqual(case["expected"]["source"], "agent")
            self.assertEqual(case["input"]["files"][0]["after"], "new()\n")
            self.assertEqual(case["input"]["pr_metadata"]["description"], "Description")
            for marker in ("PRIVATE_REFERENCE", "PRIVATE_OUTCOME", "PRIVATE_AUTHOR", "risk_label"):
                self.assertNotIn(marker, inputs)
            self.assertEqual(manifest["risk_distribution"], {"medium": 1})
            with self.assertRaisesRegex(ValueError, "frozen"):
                prepare_input_eval([self.snapshot], [self.reference], [self.raw], ["example/cli"], out)

    def test_empty_captured_description_is_distinct_from_synthetic_git_body(self):
        observed = pr_metadata({**self.raw, "body": None})
        synthetic = pr_metadata({**self.raw, "id": None, "data_source": "local_git", "body": ""})
        self.assertEqual(observed["description"], "")
        self.assertTrue(observed["description_available"])
        self.assertEqual(observed["capture_kind"], "historical_collection_after_merge")
        self.assertFalse(synthetic["description_available"])
        self.assertIsNone(synthetic["description"])
        self.assertIsNone(pr_metadata({})["captured_at"])

    def test_binary_metadata_survives_export_without_reference_leakage(self):
        metadata = {
            "before": {"kind": "text", "size_bytes": 6, "sha256": hashlib.sha256(b"old()\n").hexdigest()},
            "after": {"kind": "binary", "size_bytes": 4, "sha256": hashlib.sha256(b"\x00new").hexdigest()},
        }
        self.snapshot["files"][0].update(binary=True, after=None, patch="diff --git a/src/main.py b/src/main.py\n",
                                         content_metadata={**metadata, "outcome": "PRIVATE_OUTCOME"})
        self.snapshot["files"][0]["content_metadata"]["after"] = {**metadata["after"], "reference": "PRIVATE_REFERENCE"}
        calls = []
        def worker(request):
            calls.append(request)
            return {"status": "ok", "risk_label": "medium", "probabilities": {"low": .1, "medium": .8, "high": .1}}
        with tempfile.TemporaryDirectory() as out:
            manifest = prepare_input_eval([self.snapshot], [self.reference], [self.raw], ["example/cli"], out)
            contents = Path(manifest["inputs"]["dev"]).read_text()
            exported = json.loads(contents)
            self.assertNotIn("PRIVATE_", contents)
            self.assertEqual(exported["files"][0]["content_metadata"], metadata)
            result = score_snapshots([exported], Path(out, "scored"), worker=worker, price_snapshot={"status": "unavailable"})[0]
            self.assertEqual(result["status"], "ok")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["state"]["files"][0]["before"], "old()\n")
            self.assertEqual(calls[0]["state"]["files"][0]["content_not_inspected"], ["after"])
            original = {**self.snapshot, "pr_metadata": exported["pr_metadata"]}
            self.assertEqual(result["request_hash"], build_request(original)["request_hash"])

    def test_later_git_row_cannot_replace_actual_description(self):
        with tempfile.TemporaryDirectory() as out:
            manifest = prepare_input_eval([self.snapshot], [self.reference],
                                         [self.raw, {**self.raw, "id": None, "data_source": "local_git", "body": "",
                                                     "fetched_at": "2027-01-01T00:00:00Z"}], ["example/cli"], out)
            row = json.loads(Path(manifest["inputs"]["dev"]).read_text())
            self.assertEqual(row["pr_metadata"]["description"], "Description")

    def test_stale_reference_is_rejected_before_writing(self):
        self.reference["snapshot"]["head_sha"] = "c" * 40
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaisesRegex(ValueError, "revision"):
                prepare_input_eval([self.snapshot], [self.reference], [self.raw], ["example/cli"], out)
            self.assertFalse(Path(out, "cases.jsonl").exists())

    def test_holdout_never_becomes_a_development_case(self):
        self.snapshot["split"] = "holdout"
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaisesRegex(ValueError, "development snapshots"):
                prepare_input_eval([self.snapshot], [self.reference], [self.raw], ["example/cli"], out)

    def test_missing_description_and_null_reference_remain_unknown(self):
        self.reference["proposed_risk_label"] = None
        with tempfile.TemporaryDirectory() as out:
            manifest = prepare_input_eval([self.snapshot], [self.reference], [], ["example/cli"], out)
            case = json.loads(Path(manifest["cases_path"]).read_text())
            self.assertIsNone(case["expected"]["risk_label"])
            self.assertEqual(manifest["description_availability"], {"unavailable": 1})
            self.assertEqual(manifest["risk_distribution"], {"unreviewable": 1})
