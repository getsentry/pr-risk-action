import unittest

from risk_pr_agent.git_history import (
    git_commit_to_raw_pr_row,
    parse_numstat_lines,
    pr_number_from_subject,
    title_from_subject,
)


class GitHistoryTests(unittest.TestCase):
    def test_pr_number_from_subject_uses_reverter_pr_for_strict_reverts(self):
        self.assertEqual(
            pr_number_from_subject('Revert "feat: risky thing (#123)" (#456)'),
            456,
        )
        self.assertIsNone(pr_number_from_subject('Revert "feat: risky thing (#123)"'))
        self.assertEqual(pr_number_from_subject("feat: add thing (#123)"), 123)
        self.assertIsNone(pr_number_from_subject("chore: regenerate docs"))

    def test_title_from_subject_removes_trailing_pr_reference(self):
        self.assertEqual(title_from_subject("feat: add thing (#123)"), "feat: add thing")
        self.assertEqual(
            title_from_subject("Merge pull request #123 from getsentry/example"),
            "Merge pull request #123 from getsentry/example",
        )

    def test_git_commit_to_raw_pr_row_matches_pipeline_shape(self):
        commit = {
            "sha": "abc123",
            "committed_at": "2026-01-01T02:00:00+02:00",
            "author_name": "Alice Example",
            "author_email": "alice@example.com",
            "subject": "feat: add thing (#123)",
            "numstat": parse_numstat_lines(["10\t2\tsrc/main.py", "-\t-\tassets/logo.png"]),
        }

        row = git_commit_to_raw_pr_row("getsentry/example", commit, 123, "2026-01-02T00:00:00Z")

        self.assertEqual(row["data_source"], "local_git")
        self.assertEqual(row["created_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(row["merged_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(row["title"], "feat: add thing")
        self.assertEqual(row["metrics"]["additions"], 10)
        self.assertEqual(row["metrics"]["deletions"], 2)
        self.assertEqual(row["metrics"]["changed_files"], 2)
        self.assertEqual(row["files"][1]["changes"], 0)


if __name__ == "__main__":
    unittest.main()
