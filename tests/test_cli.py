import argparse
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from risk_pr_agent.cli import (
    apply_pr_risk_label,
    apply_model_file,
    backfill_repo,
    combine_feature_files,
    score_feature_file,
    score_pull_request,
    train_feature_file,
)
from risk_pr_agent.github import RepoRef, write_jsonl


def raw_pr(number, title="Example PR", created_at="2026-01-01T00:00:00Z"):
    return {
        "schema_version": 1,
        "repo": "getsentry/cli",
        "fetched_at": "2026-01-01T00:00:00Z",
        "number": number,
        "id": number,
        "node_id": str(number),
        "html_url": f"https://github.com/getsentry/cli/pull/{number}",
        "state": "closed",
        "locked": False,
        "draft": False,
        "title": title,
        "body": "",
        "created_at": created_at,
        "updated_at": created_at,
        "closed_at": created_at,
        "merged_at": created_at,
        "merge_commit_sha": f"sha{number}",
        "author": {"login": "alice", "type": "User", "site_admin": False},
        "labels": [],
        "assignees": [],
        "requested_reviewers": [],
        "base": {"ref": "main", "sha": "base"},
        "head": {"ref": f"branch-{number}", "sha": f"head{number}", "repo": "getsentry/cli"},
        "metrics": {
            "commits": 1,
            "additions": 10,
            "deletions": 0,
            "changed_files": 1,
            "comments": 0,
            "review_comments": 0,
        },
        "files": [
            {
                "filename": "src/main.ts",
                "status": "modified",
                "additions": 10,
                "deletions": 0,
                "changes": 10,
                "previous_filename": None,
            }
        ],
        "reviews": [],
    }


def feature_row(number, changed_lines, medium_outcome, created_at):
    return {
        "repo": "getsentry/cli",
        "number": number,
        "html_url": f"https://github.com/getsentry/cli/pull/{number}",
        "title": f"Feature row {number}",
        "author": "alice",
        "created_at": created_at,
        "merged_at": created_at,
        "closed_at": created_at,
        "prediction_features": {
            "changed_lines": changed_lines,
            "added_lines": changed_lines,
            "deleted_lines": 0,
            "file_count": 1,
            "directory_count": 1,
            "entropy_of_change": 0,
            "max_file_prior_reverts": 0,
            "max_dir_prior_reverts": 0,
        },
        "outcomes": {
            "is_merged": True,
            "strong_outcome": False,
            "medium_outcome": medium_outcome,
        },
    }


class FakeGitHubClient:
    def __init__(self):
        self.fetched_prs = []

    def list_pull_requests(self, repo):
        yield {"number": 2, "created_at": "2026-01-02T00:00:00Z"}
        yield {"number": 1, "created_at": "2026-01-01T00:00:00Z"}

    def get_pull_request(self, repo, number):
        self.fetched_prs.append(number)
        return {
            "number": number,
            "id": number,
            "node_id": str(number),
            "html_url": f"https://github.com/getsentry/cli/pull/{number}",
            "state": "closed",
            "locked": False,
            "draft": False,
            "title": f"Fetched PR {number}",
            "body": "",
            "created_at": f"2026-01-0{number}T00:00:00Z",
            "updated_at": f"2026-01-0{number}T00:00:00Z",
            "closed_at": f"2026-01-0{number}T00:00:00Z",
            "merged_at": f"2026-01-0{number}T00:00:00Z",
            "merge_commit_sha": f"sha{number}",
            "user": {"login": "alice", "type": "User", "site_admin": False},
            "labels": [],
            "assignees": [],
            "requested_reviewers": [],
            "base": {"ref": "main", "sha": "base"},
            "head": {
                "ref": f"branch-{number}",
                "sha": f"head{number}",
                "repo": {"full_name": "getsentry/cli"},
            },
            "commits": 1,
            "additions": 10,
            "deletions": 0,
            "changed_files": 1,
            "comments": 0,
            "review_comments": 0,
        }

    def list_pull_files(self, repo, number):
        return [{"filename": "src/main.ts", "status": "modified", "additions": 10, "deletions": 0, "changes": 10}]

    def list_pull_reviews(self, repo, number):
        return []


class WindowFakeGitHubClient(FakeGitHubClient):
    def list_pull_requests(self, repo):
        yield {"number": 3, "created_at": "2026-01-03T00:00:00Z"}
        yield {"number": 2, "created_at": "2026-01-02T00:00:00Z"}
        yield {"number": 1, "created_at": "2026-01-01T00:00:00Z"}
        yield {"number": 0, "created_at": "2025-12-31T00:00:00Z"}

    def get_pull_request(self, repo, number):
        if number == 0:
            raise AssertionError("older PR should not be fetched")
        return super().get_pull_request(repo, number)


class DuplicateListingGitHubClient(FakeGitHubClient):
    def list_pull_requests(self, repo):
        yield {"number": 2, "created_at": "2026-01-02T00:00:00Z"}
        yield {"number": 2, "created_at": "2026-01-02T00:00:00Z"}
        yield {"number": 1, "created_at": "2026-01-01T00:00:00Z"}


class FailingGitHubClient(FakeGitHubClient):
    def list_pull_requests(self, repo):
        yield {"number": 3, "created_at": "2026-01-03T00:00:00Z"}
        yield {"number": 2, "created_at": "2026-01-02T00:00:00Z"}
        yield {"number": 1, "created_at": "2026-01-01T00:00:00Z"}

    def get_pull_request(self, repo, number):
        if number == 2:
            raise RuntimeError("simulated fetch failure")
        return super().get_pull_request(repo, number)


class CliTests(unittest.TestCase):
    def test_apply_pr_risk_label_replaces_existing_labels(self):
        class FakeLabelClient:
            def __init__(self):
                self.upserts = []
                self.removed = []
                self.added = []

            def upsert_label(self, repo, name, color, description):
                self.upserts.append((repo.slug, name, color, description))

            def remove_issue_label(self, repo, number, label):
                self.removed.append((repo.slug, number, label))

            def list_issue_labels(self, repo, number):
                return [{"name": "risk: low"}, {"name": "unrelated"}]

            def add_issue_labels(self, repo, number, labels):
                self.added.append((repo.slug, number, labels))

        client = FakeLabelClient()
        label = apply_pr_risk_label(
            RepoRef.parse("getsentry/cli"),
            123,
            {"prediction": {"final_risk_label": "medium"}},
            client=client,
        )

        self.assertEqual(label, "risk: medium")
        self.assertEqual(client.upserts[0][1], "risk: medium")
        self.assertEqual([item[2] for item in client.removed], ["risk: low"])
        self.assertEqual(client.added, [("getsentry/cli", 123, ["risk: medium"])])

    def test_resume_reuses_existing_rows_and_fetches_missing_prs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_root = Path(tmp)
            repo = RepoRef.parse("getsentry/cli")
            raw_dir = out_root / repo.path_slug
            raw_dir.mkdir(parents=True)
            write_jsonl(str(raw_dir / "prs.jsonl"), [raw_pr(1, title="Existing PR")])
            client = FakeGitHubClient()
            args = argparse.Namespace(
                resume=True,
                refresh=False,
                max_prs=None,
                skip_reviews=False,
            )

            raw_path = backfill_repo(
                client,
                repo,
                out_root,
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                None,
                args,
            )

            rows = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(client.fetched_prs, [2])
            self.assertEqual([row["number"] for row in rows], [2, 1])
            self.assertEqual(rows[1]["title"], "Existing PR")

    def test_since_until_window_filters_newer_and_older_prs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_root = Path(tmp)
            repo = RepoRef.parse("getsentry/cli")
            client = WindowFakeGitHubClient()
            args = argparse.Namespace(
                resume=False,
                refresh=False,
                max_prs=None,
                skip_reviews=False,
            )

            raw_path = backfill_repo(
                client,
                repo,
                out_root,
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 1, 2, 23, 59, 59, tzinfo=timezone.utc),
                args,
            )

            rows = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(client.fetched_prs, [2, 1])
            self.assertEqual([row["number"] for row in rows], [2, 1])

    def test_backfill_skips_duplicate_numbers_from_moving_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_root = Path(tmp)
            repo = RepoRef.parse("getsentry/cli")
            client = DuplicateListingGitHubClient()
            args = argparse.Namespace(
                resume=False,
                refresh=False,
                max_prs=None,
                skip_reviews=False,
            )

            raw_path = backfill_repo(
                client,
                repo,
                out_root,
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                None,
                args,
            )

            rows = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(client.fetched_prs, [2, 1])
            self.assertEqual([row["number"] for row in rows], [2, 1])

    def test_failed_resume_keeps_complete_raw_and_reuses_partial_next_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_root = Path(tmp)
            repo = RepoRef.parse("getsentry/cli")
            raw_dir = out_root / repo.path_slug
            raw_dir.mkdir(parents=True)
            write_jsonl(str(raw_dir / "prs.jsonl"), [raw_pr(1, title="Existing PR")])
            args = argparse.Namespace(
                resume=True,
                refresh=False,
                max_prs=None,
                skip_reviews=False,
            )

            with self.assertRaises(RuntimeError):
                backfill_repo(
                    FailingGitHubClient(),
                    repo,
                    out_root,
                    datetime(2026, 1, 1, tzinfo=timezone.utc),
                    None,
                    args,
                )

            raw_rows = [
                json.loads(line)
                for line in (raw_dir / "prs.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            partial_rows = [
                json.loads(line)
                for line in (raw_dir / "prs.in-progress.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["number"] for row in raw_rows], [1])
            self.assertEqual([row["number"] for row in partial_rows], [3])

            client = WindowFakeGitHubClient()
            raw_path = backfill_repo(
                client,
                repo,
                out_root,
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 1, 3, 23, 59, 59, tzinfo=timezone.utc),
                args,
            )

            rows = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(client.fetched_prs, [2])
            self.assertEqual([row["number"] for row in rows], [3, 2, 1])

    def test_score_feature_file_writes_multiple_outcome_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            feature_path = tmp_path / "features.jsonl"
            feature_rows = [
                {
                    "repo": "getsentry/cli",
                    "number": 1,
                    "html_url": "https://github.com/getsentry/cli/pull/1",
                    "title": "Example",
                    "author": "alice",
                    "created_at": "2026-01-01T00:00:00Z",
                    "merged_at": "2026-01-01T00:00:00Z",
                    "closed_at": "2026-01-01T00:00:00Z",
                    "prediction_features": {
                        "changed_lines": 10,
                        "file_count": 1,
                        "directory_count": 1,
                        "entropy_of_change": 0,
                        "max_file_prior_reverts": 0,
                        "max_dir_prior_reverts": 0,
                    },
                    "outcomes": {
                        "is_merged": True,
                        "strong_outcome": False,
                        "medium_outcome": True,
                    },
                }
            ]
            write_jsonl(str(feature_path), feature_rows)

            score_feature_file(
                feature_path,
                tmp_path,
                outcome_names=["strong_outcome", "medium_outcome"],
                include_unmerged=False,
            )

            self.assertTrue((tmp_path / "evaluation_strong_outcome.md").exists())
            self.assertTrue((tmp_path / "evaluation_medium_outcome.md").exists())
            self.assertTrue((tmp_path / "evaluation_strong_outcome.json").exists())
            self.assertTrue((tmp_path / "evaluation_medium_outcome.json").exists())

    def test_combine_feature_files_writes_multiple_outcome_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            inputs = []
            for index, repo in enumerate(["getsentry/cli", "getsentry/sentry"], start=1):
                feature_path = tmp_path / f"features-{index}.jsonl"
                write_jsonl(
                    str(feature_path),
                    [
                        {
                            "repo": repo,
                            "number": index,
                            "html_url": f"https://github.com/{repo}/pull/{index}",
                            "title": "Example",
                            "author": "alice",
                            "created_at": "2026-01-01T00:00:00Z",
                            "merged_at": "2026-01-01T00:00:00Z",
                            "closed_at": "2026-01-01T00:00:00Z",
                            "prediction_features": {
                                "changed_lines": 10,
                                "file_count": 1,
                                "directory_count": 1,
                                "entropy_of_change": 0,
                                "max_file_prior_reverts": 0,
                                "max_dir_prior_reverts": 0,
                            },
                            "outcomes": {
                                "is_merged": True,
                                "strong_outcome": index == 2,
                                "medium_outcome": True,
                            },
                        }
                    ],
                )
                inputs.append(feature_path)

            out_dir = tmp_path / "combined"
            combine_feature_files(
                inputs,
                out_dir,
                outcome_names=["strong_outcome", "medium_outcome"],
                include_unmerged=False,
            )

            rows = [
                json.loads(line)
                for line in (out_dir / "features.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(rows), 2)
            self.assertTrue((out_dir / "evaluation_strong_outcome.md").exists())
            self.assertTrue((out_dir / "evaluation_medium_outcome.md").exists())

    def test_combine_feature_files_can_dedupe_later_inputs_win(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            older_path = tmp_path / "older.jsonl"
            newer_path = tmp_path / "newer.jsonl"
            older = feature_row(1, 10, False, "2026-01-01T00:00:00Z")
            newer = feature_row(1, 100, True, "2026-01-02T00:00:00Z")
            write_jsonl(str(older_path), [older])
            write_jsonl(str(newer_path), [newer])

            out_dir = tmp_path / "combined"
            combine_feature_files(
                [older_path, newer_path],
                out_dir,
                outcome_names=["medium_outcome"],
                include_unmerged=False,
                dedupe=True,
            )

            rows = [
                json.loads(line)
                for line in (out_dir / "features.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["prediction_features"]["changed_lines"], 100)
            self.assertTrue(rows[0]["outcomes"]["medium_outcome"])

    def test_train_feature_file_writes_logistic_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            feature_path = tmp_path / "features.jsonl"
            write_jsonl(
                str(feature_path),
                [
                    feature_row(1, 1, False, "2026-01-01T00:00:00Z"),
                    feature_row(2, 2, False, "2026-01-02T00:00:00Z"),
                    feature_row(3, 3, False, "2026-01-03T00:00:00Z"),
                    feature_row(4, 100, True, "2026-01-04T00:00:00Z"),
                    feature_row(5, 120, True, "2026-01-05T00:00:00Z"),
                    feature_row(6, 140, True, "2026-01-06T00:00:00Z"),
                    feature_row(7, 160, True, "2026-01-07T00:00:00Z"),
                    feature_row(8, 4, False, "2026-01-08T00:00:00Z"),
                ],
            )

            out_dir = tmp_path / "model"
            train_feature_file(
                feature_path,
                out_dir,
                outcome_names=["medium_outcome"],
                include_unmerged=False,
                train_fraction=0.75,
                epochs=120,
                learning_rate=0.1,
                l2=0.0,
                class_balance=False,
            )

            model = json.loads((out_dir / "model_medium_outcome.json").read_text(encoding="utf-8"))
            self.assertEqual(model["feature_set"], "static_no_process")
            self.assertIn("validation", model)
            changed_lines = next(
                feature for feature in model["features"] if feature["feature"] == "changed_lines"
            )
            self.assertGreater(changed_lines["weight"], 0)
            self.assertTrue((out_dir / "model_medium_outcome.md").exists())
            self.assertTrue((out_dir / "modeled_medium_outcome.jsonl").exists())
            modeled_rows = [
                json.loads(line)
                for line in (out_dir / "modeled_medium_outcome.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("logistic_risk_label", modeled_rows[0]["prediction"])
            self.assertIn("final_risk_label", modeled_rows[0]["prediction"])

            applied_dir = tmp_path / "applied"
            applied_path = apply_model_file(
                feature_path,
                out_dir / "model_medium_outcome.json",
                applied_dir,
            )
            applied_rows = [
                json.loads(line)
                for line in applied_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("logistic_risk_label", applied_rows[0]["prediction"])
            self.assertIn("final_risk_label", applied_rows[0]["prediction"])
            self.assertTrue((applied_dir / "model_application_medium_outcome.json").exists())

    def test_train_feature_file_separates_static_and_in_review_feature_sets(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            feature_path = tmp_path / "features.jsonl"
            rows = [
                feature_row(1, 1, False, "2026-01-01T00:00:00Z"),
                feature_row(2, 2, False, "2026-01-02T00:00:00Z"),
                feature_row(3, 3, False, "2026-01-03T00:00:00Z"),
                feature_row(4, 100, True, "2026-01-04T00:00:00Z"),
                feature_row(5, 120, True, "2026-01-05T00:00:00Z"),
                feature_row(6, 140, True, "2026-01-06T00:00:00Z"),
            ]
            for row in rows:
                row["prediction_features"]["comments"] = 10 if row["outcomes"]["medium_outcome"] else 0
                row["prediction_features"]["review_comments"] = 5 if row["outcomes"]["medium_outcome"] else 0
                row["prediction_features"]["commits"] = 2
            write_jsonl(str(feature_path), rows)

            out_dir = tmp_path / "model"
            train_feature_file(
                feature_path,
                out_dir,
                outcome_names=["medium_outcome"],
                include_unmerged=False,
                train_fraction=0.67,
                epochs=20,
                learning_rate=0.1,
                l2=0.0,
                class_balance=False,
                feature_set="static_no_process",
            )
            train_feature_file(
                feature_path,
                out_dir,
                outcome_names=["medium_outcome"],
                include_unmerged=False,
                train_fraction=0.67,
                epochs=20,
                learning_rate=0.1,
                l2=0.0,
                class_balance=False,
                feature_set="in_review_final",
            )

            static_model = json.loads((out_dir / "model_medium_outcome.json").read_text(encoding="utf-8"))
            in_review = json.loads(
                (out_dir / "model_in_review_final_medium_outcome.json").read_text(encoding="utf-8")
            )
            static_features = {feature["feature"] for feature in static_model["features"]}
            in_review_features = {feature["feature"] for feature in in_review["features"]}
            self.assertFalse({"comments", "review_comments", "commits"} & static_features)
            self.assertTrue({"comments", "review_comments", "commits"} <= in_review_features)
            self.assertEqual(in_review["feature_set"], "in_review_final")

    def test_train_feature_file_supports_selected_static_v1_and_maturity_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            feature_path = tmp_path / "features.jsonl"
            rows = [
                feature_row(
                    number,
                    100 if number % 2 == 0 else 10,
                    number % 2 == 0,
                    f"2026-01-{number:02d}T00:00:00Z",
                )
                for number in range(1, 11)
            ]
            write_jsonl(str(feature_path), rows)

            out_dir = tmp_path / "model"
            train_feature_file(
                feature_path,
                out_dir,
                outcome_names=["medium_outcome"],
                include_unmerged=False,
                train_fraction=0.5,
                validation_fraction=0.25,
                epochs=20,
                learning_rate=0.1,
                l2=0.0,
                class_balance=False,
                feature_set="selected_static_v1",
                maturity_days=2,
            )

            model = json.loads(
                (out_dir / "model_selected_static_v1_medium_outcome.json").read_text(encoding="utf-8")
            )
            feature_names = {feature["feature"] for feature in model["features"]}
            self.assertEqual(model["feature_set"], "selected_static_v1")
            self.assertEqual(model["maturity_days"], 2)
            self.assertEqual(model["maturity_excluded_rows"], 2)
            self.assertEqual(model["test"]["created_at_max"], "2026-01-08T00:00:00Z")
            self.assertIn("author_touched_file_ratio", feature_names)
            self.assertNotIn("comments", feature_names)

    def test_score_pull_request_scores_current_pr_with_history_and_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            history_path = tmp_path / "history.jsonl.gz"
            model_path = tmp_path / "model.json"
            write_jsonl(str(history_path), [raw_pr(1)])
            model_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "model_type": "logistic_regression",
                        "outcome_name": "medium_outcome_strict",
                        "feature_set": "selected_static_v1",
                        "percentile_mode": "as_of",
                        "intercept": 0.0,
                        "features": [
                            {
                                "feature": "changed_lines",
                                "source_feature": "changed_lines",
                                "category_value": None,
                                "weight": 1.0,
                                "transform": "log1p",
                                "mean": 0.0,
                                "scale": 1.0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = score_pull_request(
                repo=RepoRef.parse("getsentry/cli"),
                pr_number=2,
                history_paths=[history_path],
                model_path=model_path,
                skip_reviews=True,
                client=FakeGitHubClient(),
            )

            self.assertEqual(result["number"], 2)
            self.assertEqual(result["history_rows"], 1)
            self.assertEqual(result["model"]["outcome_name"], "medium_outcome_strict")
            self.assertIn("logistic_probability", result["prediction"])
            self.assertIn("PR Risk Summary", result["markdown_summary"])


if __name__ == "__main__":
    unittest.main()
