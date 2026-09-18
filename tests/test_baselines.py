import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from risk_pr_agent.baselines import freeze_baselines
from risk_pr_agent.cli import score_pull_request
from risk_pr_agent.features import build_feature_rows
from risk_pr_agent.github import RepoRef, read_jsonl, write_jsonl
from risk_pr_agent.modeling import train_logistic_baseline
from risk_tagger.labeling import assign_labels


def raw_pr(number, repo="getsentry/cli"):
    timestamp = f"2026-01-{number:02d}T00:00:00Z"
    lines = number * 10
    return {
        "repo": repo, "number": number, "created_at": timestamp,
        "merged_at": timestamp, "closed_at": timestamp, "fetched_at": "2026-02-28T00:00:00Z",
        "title": f"Feature {number}", "body": "", "author": {"login": "fixture", "type": "User"},
        "state": "closed", "merge_commit_sha": f"{number:040x}", "labels": [], "reviews": [],
        "base": {"sha": f"{number + 100:040x}"}, "head": {"sha": f"{number:040x}"},
        "metrics": {"commits": 1, "additions": lines, "deletions": 0, "changed_files": 1, "comments": 0, "review_comments": 0},
        "files": [{"filename": "src/handler.py", "status": "modified", "additions": lines, "deletions": 0, "changes": lines}],
    }


def as_response(row):
    return {
        **{key: row[key] for key in ("number", "created_at", "merged_at", "closed_at", "title", "body", "state", "merge_commit_sha", "base")},
        "head": {**row["head"], "repo": {"full_name": row["repo"]}},
        "user": row["author"], **row["metrics"],
    }


class FrozenBaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.history = [raw_pr(number) for number in range(1, 17)]
        self.history[9]["title"] = "Revert #4"
        self.history[11]["title"] = "Revert #8"
        self.history[14]["files"] = [{"filename": "docs/guide.md", "status": "modified", "additions": 150, "deletions": 0, "changes": 150}]
        trained = train_logistic_baseline(
            build_feature_rows(self.history), "strong_outcome", train_fraction=.75,
            validation_fraction=.1, epochs=40, learning_rate=.1, l2=0,
            feature_set="selected_static_v1", percentile_mode="as_of",
        )
        self.model_path = self.root / "model.json"
        self.model_path.write_text(json.dumps(trained["model"]))
        self.patch = "diff --git a/src/auth.py b/src/auth.py\n--- a/src/auth.py\n+++ b/src/auth.py\n@@ -1,2 +1,2 @@\n---counter\n-allow = False\n+++counter\n+allow = True\n"
        self.snapshot = {
            "schema_version": 2, "example_id": "getsentry/cli#16", "repo": "getsentry/cli", "number": 16, "status": "ready",
            "snapshot": {"kind": "merge", "base_sha": "a" * 40, "head_sha": "b" * 40},
            "files": [{"path": "src/auth.py", "previous_path": None, "status": "modified", "patch": self.patch}],
        }

    def corrected_history(self):
        history = copy.deepcopy(self.history)
        history[-1]["files"] = [{"filename": "src/auth.py", "previous_filename": None, "status": "modified", "patch": self.patch, "additions": 2, "deletions": 2, "changes": 4}]
        history[-1]["metrics"].update(additions=2, deletions=2, changed_files=1)
        return history

    def test_freezes_both_real_engines_at_exact_snapshot_and_matches_original_score_pr(self):
        # Include a foreign repository in the raw population: the original CLI
        # restricts history to the target repository and baseline output must
        # retain the same label despite unrelated rows with colliding PR IDs.
        rows = self.history + [raw_pr(number, "getsentry/snuba") for number in range(1, 8)]
        original_rows = copy.deepcopy(rows)
        original_snapshot = copy.deepcopy(self.snapshot)
        out = self.root / "frozen"
        manifest = freeze_baselines([self.snapshot], rows, self.model_path, out)
        predictions = list(read_jsonl(str(out / "predictions.jsonl")))
        self.assertEqual(len(predictions), 2)
        self.assertEqual({item["baseline"] for item in predictions}, {"score_pr", "risk_tagger"})
        self.assertTrue(all(item["snapshot"] == self.snapshot["snapshot"] and item["example_id"] == self.snapshot["example_id"] for item in predictions))
        self.assertEqual(rows, original_rows)
        self.assertEqual(self.snapshot, original_snapshot)
        self.assertEqual(manifest["history_rows"], len(rows))
        self.assertEqual(manifest["ready_snapshots"], 1)
        self.assertEqual(manifest["model_sha256"], hashlib.sha256(self.model_path.read_bytes()).hexdigest())
        self.assertIn("src/risk_pr_agent/modeling.py", manifest["sources"])
        self.assertEqual(manifest["percentile_modes"], {"score_pr": "as_of", "risk_tagger": "global"})

        corrected = self.corrected_history()
        history_path = self.root / "history.jsonl"
        write_jsonl(str(history_path), rows)
        client = Mock()
        client.get_pull_request.return_value = as_response(corrected[-1])
        client.list_pull_files.return_value = corrected[-1]["files"]
        original = score_pull_request(RepoRef.parse("getsentry/cli"), 16, [history_path], self.model_path, skip_reviews=True, client=client)
        by_engine = {item["baseline"]: item for item in predictions}
        self.assertEqual(original["prediction_features"]["changed_lines"], 4)
        self.assertEqual(by_engine["score_pr"]["risk_label"], original["prediction"]["final_risk_label"])
        expected_tagger = next(item for item in assign_labels(build_feature_rows(corrected), percentile_mode="global") if item["number"] == 16)
        self.assertEqual(by_engine["risk_tagger"]["risk_label"], expected_tagger["risk"]["label"])

    def test_score_pr_retains_serialized_model_percentile_mode(self):
        model = json.loads(self.model_path.read_text())
        model["percentile_mode"] = "global"
        self.model_path.write_text(json.dumps(model))
        out = self.root / "global"
        manifest = freeze_baselines([self.snapshot], self.history, self.model_path, out)
        self.assertEqual(manifest["percentile_modes"]["score_pr"], "global")
        corrected = self.corrected_history()
        history_path = self.root / "history.jsonl"
        write_jsonl(str(history_path), self.history)
        client = Mock()
        client.get_pull_request.return_value = as_response(corrected[-1])
        client.list_pull_files.return_value = corrected[-1]["files"]
        original = score_pull_request(RepoRef.parse("getsentry/cli"), 16, [history_path], self.model_path, skip_reviews=True, client=client)
        prediction = next(item for item in read_jsonl(str(out / "predictions.jsonl")) if item["baseline"] == "score_pr")
        self.assertEqual(prediction["risk_label"], original["prediction"]["final_risk_label"])

    def test_literal_plus_and_minus_source_lines_count_as_changed_content(self):
        captured = []
        def features(rows):
            captured.extend(copy.deepcopy(rows))
            return build_feature_rows(rows)
        with patch("risk_pr_agent.baselines.build_feature_rows", side_effect=features):
            freeze_baselines([self.snapshot], self.history, self.model_path, self.root / "frozen")
        current = next(item for item in captured if item["number"] == 16)
        self.assertEqual(current["metrics"]["additions"], 2)
        self.assertEqual(current["metrics"]["deletions"], 2)
        self.assertEqual(current["files"][0]["changes"], 4)

    def test_incomplete_snapshots_are_not_labeled_and_frozen_artifacts_are_not_overwritten(self):
        incomplete = {**self.snapshot, "example_id": "getsentry/cli#15", "number": 15, "status": "incomplete"}
        out = self.root / "frozen"
        freeze_baselines([self.snapshot, incomplete], self.history, self.model_path, out)
        frozen_bytes = {path.name: path.read_bytes() for path in out.iterdir()}
        with self.assertRaisesRegex(ValueError, "already frozen"):
            freeze_baselines([self.snapshot], [], self.model_path, out)
        self.assertEqual(frozen_bytes, {path.name: path.read_bytes() for path in out.iterdir()})
        predictions = list(read_jsonl(str(out / "predictions.jsonl")))
        self.assertTrue(all(item["number"] == 16 for item in predictions))
        # A stopped freeze must not overwrite predictions merely because the
        # final manifest has not been written yet.
        (out / "manifest.json").unlink()
        before = (out / "predictions.jsonl").read_bytes()
        with self.assertRaisesRegex(ValueError, "already frozen"):
            freeze_baselines([self.snapshot], self.history, self.model_path, out)
        self.assertEqual((out / "predictions.jsonl").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
