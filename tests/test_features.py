import unittest

from risk_pr_agent.features import build_feature_rows, infer_revert_outcomes, summarize_files
from risk_pr_agent.git_history import parse_strict_revert_commit


def pr(
    number,
    title,
    created_at,
    files,
    body="",
    merge_commit_sha=None,
    author="alice",
    reviews=None,
):
    return {
        "repo": "getsentry/cli",
        "number": number,
        "html_url": f"https://github.com/getsentry/cli/pull/{number}",
        "title": title,
        "body": body,
        "created_at": created_at,
        "merged_at": created_at,
        "closed_at": created_at,
        "merge_commit_sha": merge_commit_sha,
        "author": {"login": author, "type": "User"},
        "labels": [],
        "head": {"sha": f"head{number}", "ref": f"branch-{number}"},
        "metrics": {
            "commits": 1,
            "additions": sum(item["additions"] for item in files),
            "deletions": sum(item["deletions"] for item in files),
            "changed_files": len(files),
            "comments": 0,
            "review_comments": 0,
        },
        "files": files,
        "reviews": reviews or [],
    }


def file(path, additions=10, deletions=0, status="modified", patch=None):
    return {
        "filename": path,
        "status": status,
        "additions": additions,
        "deletions": deletions,
        "changes": additions + deletions,
        "patch": patch,
    }


class FeatureExtractionTests(unittest.TestCase):
    def test_summarize_files_classifies_common_risk_paths(self):
        summary = summarize_files(
            [
                file(".github/workflows/test.yml", 3),
                file("src/auth/token.py", 20),
                file("tests/test_token.py", 15),
                file("migrations/0001_add_user.sql", 8),
                file("README.md", 1),
            ]
        )

        self.assertEqual(summary["ci_deploy_file_count"], 1)
        self.assertEqual(summary["auth_permission_file_count"], 1)
        self.assertEqual(summary["test_file_count"], 1)
        self.assertEqual(summary["migration_file_count"], 1)
        self.assertEqual(summary["docs_file_count"], 1)
        self.assertTrue(summary["tests_changed"])
        self.assertFalse(summary["docs_only"])

    def test_summarize_files_adds_status_and_sensitive_area_features(self):
        summary = summarize_files(
            [
                file("src/sentry/billing/subscription.py", 20, status="added"),
                file("src/sentry/ingest/consumer.py", 10),
                file("src/sentry/migrations/0001_add_org.py", 5),
            ]
        )

        self.assertEqual(summary["new_file_count"], 1)
        self.assertEqual(summary["modified_file_count"], 2)
        self.assertFalse(summary["only_new_files"])
        self.assertTrue(summary["billing_or_quota_changed"])
        self.assertTrue(summary["ingest_or_pipeline_changed"])
        self.assertTrue(summary["db_or_storage_changed"])
        self.assertTrue(summary["sensitive_area_changed"])

    def test_infer_revert_outcome_from_revert_commit_body(self):
        rows = [
            pr(1, "Add risky thing", "2026-01-01T00:00:00Z", [file("src/main.py")], merge_commit_sha="abc1234"),
            pr(
                2,
                'Revert "Add risky thing"',
                "2026-01-02T00:00:00Z",
                [file("src/main.py")],
                body="This reverts commit abc1234.",
                merge_commit_sha="def5678",
            ),
        ]

        outcomes = infer_revert_outcomes(rows)

        self.assertTrue(outcomes[1]["has_revert_outcome"])
        self.assertEqual(outcomes[1]["reverted_by_prs"], [2])
        self.assertTrue(outcomes[2]["is_revert_pr"])
        self.assertFalse(outcomes[2]["has_revert_outcome"])

    def test_later_fix_reference_marks_medium_not_strong(self):
        rows = [
            pr(1, "Add risky thing", "2026-01-01T00:00:00Z", [file("src/main.py")]),
            pr(
                2,
                "fix: address regression from #1",
                "2026-01-02T00:00:00Z",
                [file("src/main.py")],
            ),
        ]

        outcomes = infer_revert_outcomes(rows)

        self.assertFalse(outcomes[1]["strong_outcome"])
        self.assertTrue(outcomes[1]["medium_outcome"])
        self.assertEqual(outcomes[1]["followup_fix_by_prs"], [2])

    def test_strict_followup_fix_requires_recent_overlapping_change(self):
        rows = [
            pr(1, "Add risky thing", "2026-01-01T00:00:00Z", [file("src/main.py")]),
            pr(
                2,
                "fix: address regression from #1",
                "2026-01-10T00:00:00Z",
                [file("src/helpers.py")],
            ),
        ]

        outcomes = infer_revert_outcomes(rows)

        self.assertTrue(outcomes[1]["medium_outcome"])
        self.assertTrue(outcomes[1]["medium_outcome_strict"])
        self.assertEqual(outcomes[1]["followup_fix_strict_by_prs"], [2])

    def test_broad_followup_fix_without_overlap_is_not_strict(self):
        rows = [
            pr(1, "Add risky thing", "2026-01-01T00:00:00Z", [file("src/main.py")]),
            pr(
                2,
                "fix: address regression from #1",
                "2026-01-10T00:00:00Z",
                [file("docs/main.md")],
            ),
        ]

        outcomes = infer_revert_outcomes(rows)

        self.assertTrue(outcomes[1]["medium_outcome"])
        self.assertFalse(outcomes[1]["medium_outcome_strict"])
        self.assertEqual(outcomes[1]["followup_fix_strict_by_prs"], [])

    def test_broad_followup_fix_after_strict_window_is_not_strict(self):
        rows = [
            pr(1, "Add risky thing", "2026-01-01T00:00:00Z", [file("src/main.py")]),
            pr(
                2,
                "fix: address regression from #1",
                "2026-02-15T00:00:00Z",
                [file("src/main.py")],
            ),
        ]

        outcomes = infer_revert_outcomes(rows)

        self.assertTrue(outcomes[1]["medium_outcome"])
        self.assertFalse(outcomes[1]["medium_outcome_strict"])
        self.assertEqual(outcomes[1]["followup_fix_strict_by_prs"], [])

    def test_strict_followup_fix_does_not_match_unrelated_root_files(self):
        rows = [
            pr(1, "Add risky thing", "2026-01-01T00:00:00Z", [file("main.py")]),
            pr(
                2,
                "fix: address regression from #1",
                "2026-01-10T00:00:00Z",
                [file("setup.py")],
            ),
        ]

        outcomes = infer_revert_outcomes(rows)

        self.assertTrue(outcomes[1]["medium_outcome"])
        self.assertFalse(outcomes[1]["medium_outcome_strict"])

    def test_local_git_revert_marks_strong_outcome(self):
        rows = [
            pr(123, "feat: risky change", "2026-01-01T00:00:00Z", [file("src/main.py")]),
        ]
        git_reverts = {
            123: [
                {
                    "sha": "abc123",
                    "committed_at": "2026-01-02T00:00:00+00:00",
                    "subject": 'Revert "feat: risky change (#123)"',
                }
            ]
        }

        outcomes = infer_revert_outcomes(rows, git_reverts_by_pr=git_reverts)

        self.assertTrue(outcomes[123]["strong_outcome"])
        self.assertTrue(outcomes[123]["medium_outcome"])
        self.assertEqual(outcomes[123]["git_reverted_by_commits"][0]["sha"], "abc123")

    def test_strict_git_revert_parser_ignores_non_revert_subjects(self):
        valid = parse_strict_revert_commit(
            {
                "sha": "abc123",
                "committed_at": "2026-01-02T00:00:00+00:00",
                "subject": 'Revert "feat: risky change (#123)" (#456)',
                "body": "",
            }
        )

        self.assertEqual(valid["target_pr"], 123)
        for subject in [
            'Reapply "feat: risky change (#123)"',
            "revert-revert follow up (#123)",
            "fix: revert config flag",
        ]:
            self.assertIsNone(
                parse_strict_revert_commit(
                    {
                        "sha": "abc123",
                        "committed_at": "2026-01-02T00:00:00+00:00",
                        "subject": subject,
                        "body": "",
                    }
                )
            )

    def test_followup_reference_must_be_later_than_target(self):
        rows = [
            pr(
                1,
                "fix: address regression from #2",
                "2026-01-01T00:00:00Z",
                [file("src/main.py")],
            ),
            pr(2, "Add risky thing", "2026-01-02T00:00:00Z", [file("src/main.py")]),
        ]

        outcomes = infer_revert_outcomes(rows)

        self.assertFalse(outcomes[2]["medium_outcome"])
        self.assertEqual(outcomes[2]["followup_fix_by_prs"], [])

    def test_weak_review_churn_does_not_set_medium_outcome(self):
        rows = [
            pr(
                1,
                "Add debated thing",
                "2026-01-01T00:00:00Z",
                [file("src/main.py")],
                reviews=[
                    {"state": "CHANGES_REQUESTED"},
                    {"state": "CHANGES_REQUESTED"},
                ],
            ),
        ]

        feature_rows = build_feature_rows(rows)

        self.assertTrue(feature_rows[0]["outcomes"]["weak_review_churn"])
        self.assertFalse(feature_rows[0]["outcomes"]["medium_outcome"])

    def test_prior_revert_history_is_available_only_after_revert_pr(self):
        rows = [
            pr(1, "Add risky thing", "2026-01-01T00:00:00Z", [file("src/main.py")], merge_commit_sha="abc1234"),
            pr(
                2,
                'Revert "Add risky thing"',
                "2026-01-02T00:00:00Z",
                [file("src/main.py")],
                body="This reverts commit abc1234.",
                merge_commit_sha="def5678",
            ),
            pr(3, "Touch same file later", "2026-01-03T00:00:00Z", [file("src/main.py")], merge_commit_sha="fff9999"),
        ]

        feature_rows = {row["number"]: row for row in build_feature_rows(rows)}

        self.assertEqual(feature_rows[1]["prediction_features"]["max_file_prior_reverts"], 0)
        self.assertGreaterEqual(feature_rows[3]["prediction_features"]["max_file_prior_reverts"], 1)

    def test_author_and_file_history_are_as_of_current_pr_creation(self):
        rows = [
            pr(1, "Add file", "2026-01-01T00:00:00Z", [file("src/main.py", status="added")], author="alice"),
            pr(2, "Touch file", "2026-01-02T00:00:00Z", [file("src/main.py")], author="bob"),
            pr(3, "Touch file again", "2026-01-03T00:00:00Z", [file("src/main.py")], author="alice"),
        ]

        feature_rows = {row["number"]: row for row in build_feature_rows(rows)}
        first = feature_rows[1]["prediction_features"]
        second = feature_rows[2]["prediction_features"]
        third = feature_rows[3]["prediction_features"]

        self.assertEqual(first["max_file_prior_prs"], 0)
        self.assertEqual(second["max_file_prior_prs"], 1)
        self.assertEqual(second["max_file_prior_authors"], 1)
        self.assertEqual(second["author_repo_prior_prs"], 0)
        self.assertGreaterEqual(second["min_file_days_since_last_change"], 1.0)
        self.assertEqual(third["max_file_prior_prs"], 2)
        self.assertEqual(third["max_file_prior_authors"], 2)
        self.assertEqual(third["author_repo_prior_prs"], 1)
        self.assertEqual(third["author_max_file_prior_prs"], 1)
        self.assertEqual(third["author_created_file_count"], 1)
        self.assertEqual(third["author_touched_file_ratio"], 1.0)
        self.assertGreater(third["author_file_experience_share"], 0)

    def test_relative_churn_uses_prior_estimated_file_size(self):
        rows = [
            pr(
                1,
                "Add file",
                "2026-01-01T00:00:00Z",
                [file("src/main.py", additions=100, status="added")],
            ),
            pr(
                2,
                "Touch file",
                "2026-01-02T00:00:00Z",
                [file("src/main.py", additions=10, deletions=10)],
            ),
        ]

        feature_rows = {row["number"]: row for row in build_feature_rows(rows)}
        first = feature_rows[1]["prediction_features"]
        second = feature_rows[2]["prediction_features"]

        self.assertEqual(first["max_file_churn_ratio"], 1.0)
        self.assertEqual(second["known_prior_file_size_count"], 1)
        self.assertAlmostEqual(second["max_file_churn_ratio"], 0.2)
        self.assertAlmostEqual(second["sum_churn_over_base_sloc"], 0.2)

    def test_prior_bad_outcome_history_is_as_of_followup_evidence(self):
        rows = [
            pr(1, "Add risky thing", "2026-01-01T00:00:00Z", [file("src/main.py")]),
            pr(
                2,
                "fix: address regression from #1",
                "2026-01-03T00:00:00Z",
                [file("src/main.py")],
            ),
            pr(3, "Touch same file later", "2026-01-04T00:00:00Z", [file("src/main.py")]),
        ]

        feature_rows = {row["number"]: row for row in build_feature_rows(rows)}

        self.assertEqual(feature_rows[1]["prediction_features"]["max_file_prior_bad_outcomes"], 0)
        self.assertEqual(feature_rows[2]["prediction_features"]["max_file_prior_bad_outcomes"], 0)
        self.assertEqual(feature_rows[3]["prediction_features"]["max_file_prior_bad_outcomes"], 1)
        self.assertEqual(feature_rows[3]["prediction_features"]["max_file_prior_bad_outcomes_90d"], 1)

    def test_patch_signals_detect_comment_only_and_semantic_tokens(self):
        rows = [
            pr(
                1,
                "Comment update",
                "2026-01-01T00:00:00Z",
                [
                    file(
                        "src/main.py",
                        additions=1,
                        patch="@@\n+# explain fallback\n",
                    )
                ],
            ),
            pr(
                2,
                "Add error handling",
                "2026-01-02T00:00:00Z",
                [
                    file(
                        "src/main.py",
                        additions=2,
                        patch="@@\n+try:\n+    delete(payload)\n",
                    )
                ],
            ),
        ]

        feature_rows = {row["number"]: row for row in build_feature_rows(rows)}

        self.assertTrue(feature_rows[1]["prediction_features"]["comment_only"])
        self.assertTrue(feature_rows[2]["prediction_features"]["touches_error_handling"])
        self.assertTrue(feature_rows[2]["prediction_features"]["touches_data_deletion"])


if __name__ == "__main__":
    unittest.main()
