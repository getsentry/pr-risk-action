import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from risk_pr_agent.commands import _load_env, main
from risk_pr_agent.dataset import prepare_snapshot
from risk_pr_agent.github import RepoRef, normalize_pr, write_jsonl
from risk_pr_agent.jev import MODEL, VERSIONS, build_request


class CommandIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        source = self.repo / "main.py"
        source.write_text("before()\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        base = self.git("rev-parse", "HEAD")
        source.write_text("after()\n")
        self.git("commit", "-qam", "change")
        head = self.git("rev-parse", "HEAD")
        self.api_pr = {"number": 1, "id": 10, "state": "closed", "created_at": "2025-01-01T00:00:00Z",
                       "title": "Change behavior", "body": "PR_DESCRIPTION",
                       "merged_at": "2025-01-02T00:00:00Z", "base": {"sha": base}, "head": {"sha": head},
                       "merge_commit_sha": head, "commits": 1, "changed_files": 1, "additions": 1, "deletions": 1}
        self.files = [{"filename": "main.py", "status": "modified", "additions": 1, "deletions": 1}]
        raw = normalize_pr(RepoRef.parse("getsentry/cli"), self.api_pr, "2026-01-01T00:00:00Z")
        raw["files"] = self.files
        self.snapshot = prepare_snapshot(raw, str(self.repo))
        self.inputs = self.root / "inputs.jsonl"
        write_jsonl(str(self.inputs), [self.snapshot])

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.DEVNULL).decode().strip()

    def run_cli(self, args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = main(args)
        return code, output.getvalue()

    def github_response(self, path, params=None):
        if path.endswith("/files"):
            return self.files
        if path.endswith("/reviews"):
            return []
        if path.endswith("/pulls"):
            return [self.api_pr]
        return self.api_pr

    def test_score_build_and_score_pr_prepare_the_same_request_without_a_model_or_history(self):
        with patch("risk_pr_agent.github.GitHubClient.request_json", side_effect=self.github_response):
            invocations = [
                ["score", "--input", str(self.inputs), "--out", str(self.root / "score")],
                ["build", "--repo", "getsentry/cli", "--git-repo", f"getsentry/cli={self.repo}",
                 "--since", "2024-01-01", "--out", str(self.root / "build")],
                ["score-pr", "--repo", "getsentry/cli", "--pr", "1", "--git-repo", str(self.repo),
                 "--cache-dir", str(self.root / "single"), "--out", str(self.root / "result.json")],
            ]
            for args in invocations:
                with self.subTest(command=args[0]):
                    code, output = self.run_cli([*args, "--dry-run"])
                    self.assertEqual(code, 0, output)
        results = [json.loads(path.read_text()) for path in (
            self.root / "score/prepared.jsonl", self.root / "build/run/prepared.jsonl", self.root / "result.json")]
        self.assertEqual({row["status"] for row in results}, {"ready"})
        self.assertEqual(len({row["request_hash"] for row in results}), 1)
        self.assertTrue(all(row["risk_label"] is None for row in results))
        for result in results:
            self.assertEqual(result["input_profile"], "metadata-diff")
            self.assertEqual(result["configuration"]["max_bytes"], 1048576)
            state = result["request"]["state"]
            self.assertEqual(state["pr"], {"title": "Change behavior", "description": "PR_DESCRIPTION"})
            self.assertEqual(state["line_totals"], {"additions": 1, "deletions": 1})
            self.assertEqual(state["files"][0]["patch"], self.snapshot["files"][0]["patch"])
            self.assertNotIn("before", state["files"][0])
            self.assertNotIn("after", state["files"][0])

    def test_explicit_legacy_variants_keep_their_requests(self):
        for variant in ("A", "B", "C"):
            out = self.root / variant
            code, output = self.run_cli(["score", "--input", str(self.inputs), "--variant", variant,
                                         "--dry-run", "--out", str(out)])
            self.assertEqual(code, 0, output)
            prepared = json.loads((out / "prepared.jsonl").read_text())
            self.assertEqual(prepared["request_hash"], build_request(self.snapshot, variant)["request_hash"])
            self.assertNotIn("input_profile", prepared)

    def test_locked_holdout_context_takes_precedence_over_default(self):
        self.snapshot["split"] = "holdout"
        write_jsonl(str(self.inputs), [self.snapshot])
        for profile in (None, "metadata-diff"):
            config = build_request(self.snapshot, "A", input_profile=profile)["configuration"]
            selection = {"status": "locked", "configuration": config, "development_example_ids": []}
            selection["selection_hash"] = hashlib.sha256(json.dumps(selection, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            path = self.root / "selection.json"
            path.write_text(json.dumps(selection))
            out = self.root / f"held-{profile}"
            code, output = self.run_cli(["score", "--input", str(self.inputs), "--selection", str(path),
                                         "--dry-run", "--out", str(out)])
            self.assertEqual(code, 0, output)
            prepared = json.loads((out / "prepared.jsonl").read_text())
            self.assertEqual(prepared["request_hash"], build_request(self.snapshot, "A", input_profile=profile)["request_hash"])

    def test_default_inference_is_blocked_before_network_until_accepted(self):
        code, output = self.run_cli(["score", "--input", str(self.inputs), "--out", str(self.root / "run")])
        self.assertEqual(code, 1)
        self.assertIn("held-out gate", output)
        self.assertFalse((self.root / "run").exists())

    def test_input_eval_export_and_profile_dry_run_keep_answers_out_of_request(self):
        self.snapshot["split"] = "dev"
        write_jsonl(str(self.inputs), [self.snapshot])
        references = self.root / "references.jsonl"
        raw_path = self.root / "raw.jsonl"
        write_jsonl(str(references), [{"example_id": self.snapshot["example_id"], "split": "dev",
                                      "snapshot": self.snapshot["snapshot"], "source": "agent", "review_status": "proposed",
                                      "proposed_risk_label": "medium", "rationale": "REFERENCE_ONLY",
                                      "reviewer": "assistant", "reviewed_at": "2026-01-01", "rubric_version": "1"}])
        raw = normalize_pr(RepoRef.parse("getsentry/cli"),
                           {**self.api_pr, "title": "Change behavior", "body": "PR_DESCRIPTION"}, "2026-01-01T00:00:00Z")
        write_jsonl(str(raw_path), [raw])
        dataset = self.root / "portable-eval"
        code, output = self.run_cli(["prepare-input-eval", "--snapshots", str(self.inputs), "--references", str(references),
                                     "--raw", str(raw_path), "--repo", "getsentry/cli", "--out", str(dataset)])
        self.assertEqual(code, 0, output)
        case = json.loads((dataset / "cases.jsonl").read_text())
        self.assertEqual(case["expected"]["risk_label"], "medium")
        self.assertIn("after()", case["input"]["files"][0]["after"])
        out = self.root / "profile"
        code, output = self.run_cli(["score", "--dataset", str(dataset), "--input-profile", "paths-lines-description",
                                     "--dry-run", "--out", str(out)])
        self.assertEqual(code, 0, output)
        prepared = json.loads((out / "prepared.jsonl").read_text())
        self.assertEqual(prepared["status"], "ready")
        self.assertEqual(prepared["request"]["state"]["line_totals"], {"additions": 1, "deletions": 1})
        self.assertEqual(prepared["request"]["state"]["pr"]["description"], "PR_DESCRIPTION")
        self.assertNotIn("REFERENCE_ONLY", json.dumps(prepared["request"]))
        self.assertNotIn("patch", prepared["request"]["state"]["files"][0])
        predictions = self.root / "predictions.jsonl"
        write_jsonl(str(predictions), [{**prepared, "snapshot": self.snapshot["snapshot"],
                                       "status": "ok", "risk_label": "medium",
                                       "probabilities": {"low": .1, "medium": .8, "high": .1}}])
        report_path = self.root / "profile-report.json"
        code, output = self.run_cli(["evaluate", "--dataset", str(dataset), "--predictions", str(predictions),
                                     "--out", str(report_path)])
        self.assertEqual(code, 0, output)
        report = json.loads(report_path.read_text())
        self.assertEqual(report["reference_type"], "assisted")
        self.assertEqual(report["classification"]["confusion"]["medium"]["medium"], 1)
        self.assertNotIn("human_labels", report)

    def test_score_pr_can_explicitly_select_description_only(self):
        self.api_pr.update(title="Change behavior", body="PR_DESCRIPTION")
        destination = self.root / "description.json"
        with patch("risk_pr_agent.github.GitHubClient.request_json", side_effect=self.github_response):
            code, output = self.run_cli(["score-pr", "--repo", "getsentry/cli", "--pr", "1",
                                         "--git-repo", str(self.repo), "--cache-dir", str(self.root / "profile"),
                                         "--input-profile", "description", "--dry-run", "--out", str(destination)])
        self.assertEqual(code, 0, output)
        result = json.loads(destination.read_text())
        self.assertEqual(result["request"]["state"], {"pr": {"title": "Change behavior", "description": "PR_DESCRIPTION"}})
        self.assertEqual(result["input_profile"], "description")

    def test_assisted_evaluation_uses_only_proposals_and_refuses_selection(self):
        dataset = self.root / "dataset"
        dataset.mkdir()
        example = {"example_id": self.snapshot["example_id"], "split": "dev",
                   "repo": "example/project", "snapshot": self.snapshot["snapshot"]}
        (dataset / "manifest.json").write_text(json.dumps({"reviewed": [example]}))
        references = self.root / "proposals.jsonl"
        predictions = self.root / "predictions.jsonl"
        report_path = self.root / "assisted.json"
        write_jsonl(str(references), [{**example, "source": "agent", "review_status": "proposed",
                                      "proposed_risk_label": "low", "rationale": "Documentation-only fixture",
                                      "reviewer": "assistant", "reviewed_at": "2026-01-01", "rubric_version": "jev-risk-v1"}])
        write_jsonl(str(predictions), [{**example, "status": "ok", "risk_label": "high",
                                       "probabilities": {"low": 0, "medium": 0, "high": 1}}])
        code, output = self.run_cli(["evaluate", "--dataset", str(dataset), "--predictions", str(predictions),
                                     "--assisted-labels", str(references), "--out", str(report_path)])
        self.assertEqual(code, 0, output)
        report = json.loads(report_path.read_text())
        self.assertEqual(report["assisted_references"]["reviewed"], 1)
        self.assertEqual(report["classification"]["low_to_high"], 1)
        self.assertNotIn("human_labels", report)
        self.assertFalse(report["acceptance"]["accepted"])
        selection = self.root / "assisted-selection.json"
        code, output = self.run_cli(["select-candidate", "--report", str(report_path), "--out", str(selection)])
        self.assertEqual(code, 1, output)
        self.assertIn("assisted reports cannot select", output)
        self.assertFalse(selection.exists())

    def test_assisted_evaluation_rejects_non_dev_before_reading_files(self):
        destination = self.root / "report.json"
        code, output = self.run_cli(["evaluate", "--dataset", "unused", "--predictions", "unused",
                                     "--assisted-labels", "unused", "--split", "representative", "--out", str(destination)])
        self.assertEqual(code, 1, output)
        self.assertIn("restricted to the dev split", output)
        self.assertFalse(destination.exists())

    def test_holdout_rejects_changed_engine_before_inference_even_in_dry_run(self):
        self.snapshot["split"] = "holdout"
        write_jsonl(str(self.inputs), [self.snapshot])
        selection = {"status": "locked", "configuration": {"model": MODEL, "variant": "A", "max_bytes": 65536,
                     "versions": {**VERSIONS, "rubric": "obsolete"}}, "development_example_ids": []}
        selection["selection_hash"] = hashlib.sha256(json.dumps(selection, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        path = self.root / "selection.json"
        path.write_text(json.dumps(selection))
        code, output = self.run_cli(["score", "--input", str(self.inputs), "--selection", str(path),
                                     "--dry-run", "--out", str(self.root / "held")])
        self.assertEqual(code, 1)
        self.assertIn("Installed engine differs", output)
        self.assertFalse((self.root / "held").exists())

    def test_local_key_is_loaded_without_overwriting_environment_or_loading_other_keys(self):
        (self.root / ".env").write_text('AI_GATEWAY_API_KEY="fixture-key"\nUNRELATED_SECRET=do-not-load\n')
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.dict(os.environ, {}, clear=True):
                _load_env()
                self.assertEqual(os.environ["AI_GATEWAY_API_KEY"], "fixture-key")
                self.assertNotIn("UNRELATED_SECRET", os.environ)
                os.environ["AI_GATEWAY_API_KEY"] = "environment-key"
                _load_env()
                self.assertEqual(os.environ["AI_GATEWAY_API_KEY"], "environment-key")
        finally:
            os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
