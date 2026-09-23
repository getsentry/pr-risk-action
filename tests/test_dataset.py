import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from risk_pr_agent.dataset import (
    DEFAULT_REPOS, derive_outcomes, prepare_dataset, prepare_snapshot, refresh_snapshot_metadata, related_test_score, select_dataset,
)
from risk_pr_agent.jev import build_request


def row(number, repo="getsentry/cli", day=None, **extra):
    when = day or (datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=number)).isoformat()
    return {"repo": repo, "number": number, "merged_at": when, "fetched_at": "2026-09-01T00:00:00Z", "files": [{"filename": "src/main.py", "changes": 2}], "metrics": {"commits": 1}, **extra}


class SelectionTests(unittest.TestCase):
    def test_reproducible_disjoint_stratified_temporal_sample(self):
        paths = ["docs/guide.md", "tests/test_main.py", "package-lock.json", "src/auth/login.py", "migrations/002.sql", "settings.toml", "src/feature.py"]
        rows = [row(number, repo, files=[{"filename": paths[number % len(paths)], "changes": 2000 if number % 9 == 0 else 3}]) for repo in DEFAULT_REPOS for number in range(1, 340)]
        result = select_dataset(rows)
        self.assertEqual(result, select_dataset(list(reversed(rows))))
        self.assertEqual(len(result["reviewed"]), 120)
        self.assertEqual(len(result["representative"]), 1000)
        self.assertFalse({item["example_id"] for item in result["reviewed"]} & {item["example_id"] for item in result["representative"]})
        for repo in DEFAULT_REPOS:
            dev = [item for item in result["reviewed"] if item["repo"] == repo and item["split"] == "dev"]
            held = [item for item in result["reviewed"] if item["repo"] == repo and item["split"] == "holdout"]
            self.assertEqual((len(dev), len(held)), (20, 10))
            self.assertLess(max(item["merged_at"] for item in dev), min(item["merged_at"] for item in held))
            strata = {kind for item in dev + held for kind in item["strata"]}
            self.assertTrue({"docs", "tests", "mechanical", "auth", "migration", "config", "large"}.issubset(strata))

    def test_sampling_does_not_depend_on_outcomes(self):
        rows = [row(number) for number in range(1, 40)]
        altered = [{**item, "strong_outcome": True, "risk_label": "high", "title": "Revert everything", "body": "fix #2"} for item in rows]
        self.assertEqual(select_dataset(rows, reviewed_per_repo=8, representative_per_repo=12), select_dataset(altered, reviewed_per_repo=8, representative_per_repo=12))

    def test_reports_insufficient_rows_and_deduplicates(self):
        result = select_dataset([row(1), row(1)], reviewed_per_repo=2, representative_per_repo=0)
        self.assertEqual(len(result["reviewed"]), 1)
        self.assertTrue(result["shortfalls"])

    def test_tied_merge_times_do_not_leak_across_splits(self):
        rows = [row(number, day="2025-01-01T00:00:00Z") for number in range(1, 7)]
        result = select_dataset(rows, reviewed_per_repo=6, representative_per_repo=0)
        self.assertEqual({item["split"] for item in result["reviewed"]}, {"holdout"})


class OutcomeTests(unittest.TestCase):
    def test_only_later_merged_references_within_cutoff_count(self):
        rows = [
            row(1, day="2026-01-01T00:00:00Z"),
            row(2, day="2026-01-10T00:00:00Z", title="Revert #1"),
            row(3, day="2025-12-30T00:00:00Z", title="Revert #1"),
            row(4, merged_at=None, title="Revert #1"),
            row(5, day="2026-03-01T00:00:00Z", title="Revert #1"),
            row(6, repo="getsentry/snuba", day="2026-01-08T00:00:00Z", title="Revert #1"),
            row(7, day="2026-01-15T00:00:00Z", title="fix #1"),
            row(8, day="2026-01-16T00:00:00Z", title="fix #1", files=[{"filename": "other.py"}]),
        ]
        outcomes = {item["example_id"]: item for item in derive_outcomes(rows, "2026-02-15T00:00:00Z")}
        first = outcomes["getsentry/cli#1"]
        self.assertTrue(first["strong_outcome"])
        self.assertTrue(first["medium_outcome"])
        self.assertEqual([item["number"] for item in first["events"]], [2, 7])
        self.assertFalse(outcomes["getsentry/cli#4"]["valid"])
        self.assertIsNone(outcomes["getsentry/cli#5"]["strong_outcome"])

    def test_observation_window_starts_at_merge_not_creation(self):
        rows = [row(1, day="2026-02-10T00:00:00Z", created_at="2025-01-01T00:00:00Z"), row(2, day="2026-01-01T00:00:00Z")]
        outcomes = derive_outcomes(rows, "2026-02-15T00:00:00Z")
        self.assertFalse(outcomes[0]["mature"])
        self.assertIsNone(outcomes[0]["medium_outcome"])
        self.assertFalse(outcomes[1]["medium_outcome"])
        self.assertNotIn("risk_label", outcomes[1])

    def test_revert_sha_reference_is_verified(self):
        rows = [row(1, merge_commit_sha="abcdef1234567890"), row(2, title="Revert update", body="This reverts commit abcdef1")]
        outcomes = derive_outcomes(rows, "2026-09-01T00:00:00Z")
        self.assertTrue(outcomes[0]["strong_outcome"])

    def test_fix_window_uses_merge_time_and_rejects_late_fixes(self):
        rows = [row(1, day="2026-01-01T00:00:00Z", created_at="2025-01-01T00:00:00Z"), row(2, day="2026-01-31T00:00:00Z", title="fix #1"), row(3, day="2026-02-01T00:00:00Z", title="fix #1")]
        outcomes = derive_outcomes(rows, "2026-03-01T00:00:00Z")
        self.assertEqual([event["number"] for event in outcomes[0]["events"]], [2])
        self.assertTrue(outcomes[0]["medium_outcome_strict"])


class MetadataRefreshTests(unittest.TestCase):
    def test_fetches_only_selected_local_rows_and_reuses_immutable_cache(self):
        rows = [row(1, data_source="local_git"), row(2, data_source="github_api"), row(3, data_source="local_git")]
        selection = {"reviewed": [{"example_id": "getsentry/cli#1"}, {"example_id": "getsentry/cli#2"}], "representative": []}
        response = {"number": 1, "merged_at": rows[0]["merged_at"], "merge_commit_sha": "a" * 40, "base": {"sha": "b" * 40}, "head": {"sha": "c" * 40}, "commits": 2, "additions": 3, "deletions": 1, "changed_files": 1, "title": "Captured title", "body": "Captured description", "updated_at": "2026-09-02T00:00:00Z", "user": {"login": "private-user"}}
        client = Mock()
        client.get_pull_request.return_value = response
        with tempfile.TemporaryDirectory() as out:
            first = refresh_snapshot_metadata(rows, selection, out, client)
            second = refresh_snapshot_metadata(rows, selection, out, client)
            self.assertEqual(client.get_pull_request.call_count, 1)
            self.assertEqual(first, second)
            self.assertEqual(set(first), {"getsentry/cli#1"})
            metadata = first["getsentry/cli#1"]
            self.assertEqual(metadata["files"], rows[0]["files"])
            self.assertFalse(metadata["files_authoritative"])
            self.assertTrue(metadata["metrics_authoritative"])
            self.assertEqual(metadata["data_source"], "github_api")
            self.assertEqual(metadata["metrics"]["commits"], 2)
            self.assertEqual(metadata["title"], response["title"])
            self.assertEqual(metadata["body"], response["body"])
            self.assertEqual(metadata["updated_at"], response["updated_at"])
            self.assertNotIn("private-user", json.dumps(first))
            self.assertEqual(json.loads((Path(out) / "snapshot-metadata.json").read_text()), first)

    def test_fetch_errors_preserve_successful_cache_and_retry_only_failures(self):
        rows = [row(1, data_source="local_git"), row(2, data_source="local_git")]
        selection = {"reviewed": [{"example_id": "getsentry/cli#1"}, {"example_id": "getsentry/cli#2"}], "representative": []}
        def fetch(repo, number):
            if number == 2:
                raise RuntimeError("Sensitive HTTP response")
            return {"number": number}
        client = Mock()
        client.get_pull_request.side_effect = fetch
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(RuntimeError) as error:
                refresh_snapshot_metadata(rows, selection, out, client)
            self.assertIn("getsentry/cli#2", str(error.exception))
            self.assertNotIn("Sensitive", str(error.exception))
            self.assertTrue((Path(out) / "snapshot-metadata" / "getsentry__cli__1.json").exists())
            client.get_pull_request.reset_mock()
            client.get_pull_request.side_effect = lambda repo, number: {"number": number}
            result = refresh_snapshot_metadata(rows, selection, out, client)
            self.assertEqual(client.get_pull_request.call_count, 1)
            self.assertEqual(client.get_pull_request.call_args.args[1], 2)
            self.assertEqual(len(result), 2)


class RelatedTestScoreTests(unittest.TestCase):
    def test_package_markers_support_files_and_assets_are_not_tests(self):
        paths = ["src/snuba/query/__init__.py", "src/snuba/query/foo.py"]
        for test in ("tests/__init__.py", "tests/query/__init__.py", "tests/query/conftest.py",
                     "tests/query/helpers.py", "tests/query/fixtures/foo.py", "tests/query/assets/foo.ts",
                     "tests/query/test_foo.json", "tests/query/foo.css", "tests/query/data/foo.py",
                     "tests/query/join_structures.py", "tests/query/utils/foo.ts"):
            with self.subTest(test=test):
                self.assertEqual(related_test_score(test, paths)[:2], (0, 0))

    def test_language_conventions_and_explicit_test_names_remain_eligible(self):
        for test in ("tests/foo.rs", "__tests__/foo.ts", "foo.test.js", "foo.spec.tsx",
                     "foo_test.go", "tests/fixtures/test_foo.py", "tests/helpers/foo.spec.ts",
                     "src/utils/__tests__/foo.ts", "crates/utils/tests/foo.rs"):
            with self.subTest(test=test):
                self.assertNotEqual(related_test_score(test, ["src/foo.rs"])[:2], (0, 0))

    def test_related_directories_outrank_unrelated_basename_and_respect_boundaries(self):
        paths = ["src/snuba/query/foo.py"]
        candidates = ["tests/admin/test_foo.py", "tests/query/test_bar.py", "tests/query/test_foo.py"]
        self.assertEqual(sorted(candidates, key=lambda test: related_test_score(test, paths)),
                         ["tests/query/test_foo.py", "tests/query/test_bar.py", "tests/admin/test_foo.py"])
        self.assertEqual(related_test_score("tests/querying/test_bar.py", paths)[:2], (0, 0))
        self.assertEqual(related_test_score("tests/sentry/api/test_bar.py", ["src/sentry/api/foo.py"])[:2], (-2, 0))

    def test_changed_test_paths_are_normalized_too(self):
        self.assertEqual(related_test_score("tests/query/test_foo.py", ["tests/query/foo_test.py"])[:2], (-1, -1))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.DEVNULL).decode().strip()

    def put(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode())

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def test_rename_delete_and_exact_historical_context(self):
        self.put("src/main.py", "def main():\n    return 1\n")
        self.put("src/old.py", "old implementation\n" * 4)
        self.put("src/deleted.py", "removed = True\n")
        self.put("src/tests/test_main.py", "def test_main():\n    assert main() == 1\n")
        self.put("README.md", "Original documentation\n")
        base = self.commit("base")
        self.put("src/main.py", "def main():\n    return 2\n")
        self.git("mv", "src/old.py", "src/renamed.py")
        self.git("rm", "-q", "src/deleted.py")
        head = self.commit("change")
        self.put("src/main.py", "future worktree contents\n")
        self.put("README.md", "future docs\n")
        snapshot = prepare_snapshot(row(1, merge_commit_sha=head, title="SECRET TITLE", strong_outcome=True), str(self.repo))
        self.assertEqual(snapshot["status"], "ready")
        self.assertEqual(snapshot["snapshot"]["base_sha"], base)
        files = {item["path"]: item for item in snapshot["files"]}
        self.assertEqual(files["src/renamed.py"]["previous_path"], "src/old.py")
        self.assertEqual(files["src/deleted.py"]["status"], "removed")
        self.assertEqual(files["src/deleted.py"]["after"], "")
        self.assertIn("return 2", files["src/main.py"]["after"])
        self.assertIn("return 1", files["src/main.py"]["before"])
        self.assertNotIn("future", json.dumps(snapshot))
        self.assertNotIn("SECRET TITLE", json.dumps(snapshot))
        self.assertNotIn("strong_outcome", snapshot)
        tests = [item for item in snapshot["repository_context"]["files"] if item["kind"] == "test"]
        self.assertEqual(len(tests), 1)
        self.assertIn("assert", tests[0]["content"])

    def test_open_pr_uses_merge_base_and_ignores_base_branch_changes(self):
        self.put("main.py", "base\n")
        base = self.commit("base")
        self.git("checkout", "-qb", "feature")
        self.put("feature.py", "feature\n")
        head = self.commit("feature")
        self.git("checkout", "-qb", "base-next", base)
        self.put("unrelated.py", "unrelated\n")
        later_base = self.commit("base change")
        snapshot = prepare_snapshot(row(1, merged_at=None, base={"sha": later_base}, head={"sha": head}), str(self.repo))
        self.assertEqual(snapshot["status"], "ready")
        self.assertEqual(snapshot["snapshot"]["base_sha"], base)
        self.assertEqual(snapshot["snapshot"]["kind"], "pr_head")
        self.assertEqual([item["path"] for item in snapshot["files"]], ["feature.py"])

    def test_binary_only_addition_has_inventory_without_payload(self):
        self.put("file.txt", "base\n")
        self.commit("base")
        content = b"\x00\x01\xff"
        self.put("image.bin", content)
        head = self.commit("binary")
        snapshot = prepare_snapshot(row(1, merge_commit_sha=head), str(self.repo))
        self.assertEqual(snapshot["status"], "ready")
        self.assertEqual(snapshot["missing"], [])
        self.assertEqual(len(snapshot["files"]), 1)
        file = snapshot["files"][0]
        self.assertTrue(file["binary"])
        self.assertEqual(file["content_metadata"], {
            "before": {"kind": "absent", "size_bytes": None, "sha256": None},
            "after": {"kind": "binary", "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()},
        })
        self.assertIn("new file mode", file["patch"])
        self.assertNotIn("GIT binary patch", file["patch"])
        self.assertNotIn("literal ", file["patch"])
        self.assertIsNone(file["after"])

    def test_literal_binary_marker_phrases_keep_the_complete_text_diff(self):
        self.put("guide.txt", "before\n")
        base = self.commit("base")
        content = "GIT binary patch\nBinary files a/example and b/example differ\n"
        self.put("guide.txt", content)
        head = self.commit("document binary markers")
        snapshot = prepare_snapshot(row(1, merge_commit_sha=head), str(self.repo))
        self.assertEqual(snapshot["status"], "ready")
        file = snapshot["files"][0]
        self.assertFalse(file["binary"])
        self.assertNotIn("content_metadata", file)
        self.assertEqual(file["after"], content)
        self.assertEqual(file["patch"].strip(), self.git(
            "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--find-renames",
            "--binary", "--full-index", "--unified=3", base, head, "--", "guide.txt"))
        self.assertIn("+GIT binary patch\n", file["patch"])
        self.assertIn("+Binary files a/example and b/example differ\n", file["patch"])

    def test_mixed_binary_changes_preserve_paths_hashes_and_complete_text_diff(self):
        self.put("file.txt", "before\n")
        self.put("modified.bin", b"\x00one")
        self.put("old.bin", b"\x00renamed content")
        self.put("deleted.bin", b"\x00deleted content")
        base = self.commit("base")
        self.put("file.txt", "after\n")
        self.put("modified.bin", b"\x00two")
        self.put("added.bin", b"\x00new content")
        self.git("mv", "old.bin", "renamed.bin")
        self.git("rm", "-q", "deleted.bin")
        head = self.commit("mixed changes")
        paths = ["file.txt", "modified.bin", "added.bin", "renamed.bin", "deleted.bin"]
        snapshot = prepare_snapshot(row(1, merge_commit_sha=head, data_source="github_api",
                                        metrics={"commits": 1, "changed_files": 5, "additions": 1, "deletions": 1},
                                        files_authoritative=True, files=[{"filename": path} for path in paths]), str(self.repo))
        self.assertEqual(snapshot["status"], "ready", snapshot["missing"])
        files = {file["path"]: file for file in snapshot["files"]}
        self.assertEqual(set(files), set(paths))
        self.assertEqual(files["file.txt"]["patch"].strip(), self.git(
            "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--find-renames",
            "--binary", "--full-index", "--unified=3", base, head, "--", "file.txt"))
        self.assertFalse(files["file.txt"]["binary"])
        modified = files["modified.bin"]["content_metadata"]
        self.assertEqual(modified["before"]["size_bytes"], modified["after"]["size_bytes"])
        self.assertEqual(modified["before"]["sha256"], hashlib.sha256(b"\x00one").hexdigest())
        self.assertEqual(modified["after"]["sha256"], hashlib.sha256(b"\x00two").hexdigest())
        self.assertNotEqual(modified["before"]["sha256"], modified["after"]["sha256"])
        renamed = files["renamed.bin"]
        self.assertEqual(renamed["status"], "renamed")
        self.assertEqual(renamed["previous_path"], "old.bin")
        self.assertEqual(renamed["content_metadata"]["before"], renamed["content_metadata"]["after"])
        self.assertIn("rename from old.bin", renamed["patch"])
        self.assertIn("rename to renamed.bin", renamed["patch"])
        self.assertEqual(files["deleted.bin"]["status"], "removed")
        self.assertEqual(files["deleted.bin"]["content_metadata"]["after"],
                         {"kind": "absent", "size_bytes": None, "sha256": None})
        self.assertEqual(files["added.bin"]["content_metadata"]["before"]["kind"], "absent")
        for file in files.values():
            if file["binary"]:
                self.assertNotIn("GIT binary patch", file["patch"])
                self.assertNotIn("literal ", file["patch"])

    def test_text_binary_transitions_keep_readable_sides_and_distinguish_empty_from_absent(self):
        self.put("file.dat", b"")
        self.commit("empty text")
        self.put("file.dat", b"\x00payload")
        head = self.commit("binary")
        binary = prepare_snapshot(row(1, merge_commit_sha=head), str(self.repo))
        self.assertEqual(binary["status"], "ready")
        file = binary["files"][0]
        self.assertEqual(file["before"], "")
        self.assertIsNone(file["after"])
        self.assertEqual(file["content_metadata"]["before"],
                         {"kind": "text", "size_bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()})
        self.assertEqual(file["content_metadata"]["after"]["kind"], "binary")
        self.put("file.dat", "enabled = True\n")
        restored = prepare_snapshot(row(2, merge_commit_sha=self.commit("restore text")), str(self.repo))
        self.assertEqual(restored["status"], "ready")
        file = restored["files"][0]
        self.assertTrue(file["binary"])
        self.assertIsNone(file["before"])
        self.assertEqual(file["after"], "enabled = True\n")
        self.assertEqual(file["content_metadata"]["before"]["kind"], "binary")
        self.assertEqual(file["content_metadata"]["after"]["kind"], "text")

    def test_non_utf8_hunks_are_replaced_with_headers_without_losing_line_counts(self):
        self.put("file.txt", b"old secret \xe9\n")
        self.commit("old non-UTF8 text")
        self.put("file.txt", b"new secret \xe9\n")
        snapshot = prepare_snapshot(row(1, merge_commit_sha=self.commit("new non-UTF8 text"),
                                        data_source="github_api",
                                        metrics={"commits": 1, "changed_files": 1, "additions": 1, "deletions": 1}), str(self.repo))
        self.assertEqual(snapshot["status"], "ready", snapshot["missing"])
        file = snapshot["files"][0]
        self.assertTrue(file["binary"])
        self.assertIsNone(file["before"])
        self.assertIsNone(file["after"])
        self.assertEqual((file["additions"], file["deletions"]), (1, 1))
        self.assertEqual(file["content_metadata"]["before"]["kind"], "binary")
        self.assertEqual(file["content_metadata"]["after"]["kind"], "binary")
        self.assertIn("diff --git", file["patch"])
        self.assertNotIn("@@", file["patch"])
        self.assertNotIn("secret", file["patch"])
        self.assertNotIn("\ufffd", file["patch"])

    def test_git_declared_binary_keeps_utf8_content_and_mode_headers(self):
        self.put(".gitattributes", "*.dat binary\n")
        self.put("file.dat", "old text\n")
        self.commit("base")
        self.put("file.dat", "new text\n")
        self.git("update-index", "--chmod=+x", "file.dat")
        (self.repo / "file.dat").chmod(0o755)
        snapshot = prepare_snapshot(row(1, merge_commit_sha=self.commit("binary-marked text")), str(self.repo))
        self.assertEqual(snapshot["status"], "ready")
        file = snapshot["files"][0]
        self.assertTrue(file["binary"])
        self.assertEqual((file["before"], file["after"]), ("old text\n", "new text\n"))
        self.assertEqual(file["content_metadata"]["before"]["kind"], "text")
        self.assertEqual(file["content_metadata"]["after"]["kind"], "text")
        self.assertIn("old mode 100644", file["patch"])
        self.assertIn("new mode 100755", file["patch"])
        self.assertNotIn("GIT binary patch", file["patch"])
        self.assertNotIn("new text", file["patch"])

    def test_missing_commit_or_blob_remains_incomplete(self):
        self.put("file.txt", "base\n")
        self.commit("base")
        self.put("image.bin", b"\x00\x01\xff")
        head = self.commit("binary")
        unavailable = prepare_snapshot(row(2, merge_commit_sha="f" * 40), str(self.repo))
        self.assertEqual(unavailable["status"], "incomplete")
        with patch("risk_pr_agent.dataset._blobs", return_value={}):
            unavailable = prepare_snapshot(row(1, merge_commit_sha=head), str(self.repo))
        self.assertEqual(unavailable["status"], "incomplete")
        self.assertIn("blob_unavailable:image.bin", unavailable["missing"])
        self.assertNotIn("content_metadata", unavailable["files"][0])
        blob_sha = self.git("rev-parse", f"{head}:image.bin")
        (self.repo / ".git" / "objects" / blob_sha[:2] / blob_sha[2:]).unlink()
        unavailable = prepare_snapshot(row(1, merge_commit_sha=head), str(self.repo))
        self.assertEqual(unavailable["status"], "incomplete")
        self.assertTrue(unavailable["missing"])

    def test_rebased_pr_includes_first_security_commit_and_final_docs_commit(self):
        self.put("auth.py", "allow = False\n")
        self.put("README.md", "Before\n")
        base = self.commit("base")
        self.put("auth.py", "allow = True\n")
        self.commit("security behavior")
        self.put("README.md", "After\n")
        head = self.commit("documentation")
        metadata = row(1, merge_commit_sha=head, base={"sha": base}, head={"sha": head}, data_source="github_api", metrics={"commits": 2, "changed_files": 2, "additions": 2, "deletions": 2})
        snapshot = prepare_snapshot(metadata, str(self.repo))
        self.assertEqual(snapshot["status"], "ready")
        self.assertEqual(snapshot["snapshot"]["kind"], "pr_head")
        self.assertEqual(snapshot["snapshot"]["source_merge_sha"], head)
        self.assertEqual(snapshot["snapshot"]["source_base_sha"], base)
        self.assertEqual(snapshot["snapshot"]["base_sha"], base)
        self.assertEqual({item["path"] for item in snapshot["files"]}, {"auth.py", "README.md"})
        unknown_count = {**metadata, "metrics": {**metadata["metrics"], "commits": 0}}
        self.assertEqual(prepare_snapshot(unknown_count, str(self.repo))["snapshot"], snapshot["snapshot"])
        ambiguous = prepare_snapshot({**metadata, "base": {}}, str(self.repo))
        self.assertEqual(ambiguous["status"], "incomplete")
        self.assertIn("ambiguous_merge_span", ambiguous["missing"])

    def test_authoritative_pr_metadata_rejects_partial_patch_and_wrong_paths(self):
        self.put("auth.py", "allow = False\n")
        self.put("README.md", "Before\n")
        self.commit("base")
        self.put("auth.py", "allow = True\n")
        self.commit("security behavior")
        self.put("README.md", "After\n")
        head = self.commit("documentation")
        # Even erroneous single-commit metadata must not bypass independent
        # total/path checks from GitHub's complete PR description.
        metadata = row(1, merge_commit_sha=head, data_source="github_api", metrics={"commits": 1, "changed_files": 2, "additions": 2, "deletions": 2}, files_authoritative=True, files=[{"filename": "auth.py"}, {"filename": "README.md"}])
        snapshot = prepare_snapshot(metadata, str(self.repo))
        self.assertEqual(snapshot["status"], "incomplete")
        for field in ("changed_files", "additions", "deletions", "paths"):
            self.assertIn(f"authoritative_{field}_mismatch", snapshot["missing"])
        local = prepare_snapshot({**metadata, "data_source": "local_git"}, str(self.repo))
        self.assertEqual(local["status"], "incomplete")
        self.assertIn("unverified_pr_span", local["missing"])

    def test_metadata_overlay_rebuilds_local_snapshot_without_changing_cohort(self):
        self.put("main.py", "before\n")
        base = self.commit("base")
        self.put("main.py", "after\n")
        head = self.commit("change")
        raw = Path(self.temp.name) / "raw.jsonl"
        raw.write_text(json.dumps(row(1, merge_commit_sha=head, data_source="local_git",
                                      title="Synthetic Git title", body="", id=None)) + "\n")
        out = Path(self.temp.name) / "dataset"
        first = prepare_dataset([str(raw)], {"getsentry/cli": str(self.repo)}, str(out), reviewed_per_repo=1, representative_per_repo=0)
        self.assertEqual(first["snapshot_counts"], {"incomplete": 1})
        metadata = {"getsentry/cli#1": {"data_source": "github_api", "base": {"sha": base}, "head": {"sha": head}, "metrics": {"commits": 1, "changed_files": 1, "additions": 1, "deletions": 1}}}
        second = prepare_dataset([str(raw)], {"getsentry/cli": str(self.repo)}, str(out), reviewed_per_repo=1, representative_per_repo=0, snapshot_metadata=metadata)
        self.assertEqual(second["snapshot_counts"], {"ready": 1})
        self.assertEqual(first["selection_hash"], second["selection_hash"])
        self.assertEqual(first["reviewed"][0]["example_id"], second["reviewed"][0]["example_id"])
        label = json.loads(Path(second["labels_path"]).read_text())
        self.assertEqual(label["snapshot"], {"base_sha": base, "head_sha": head})
        snapshot_path = out / "snapshots/getsentry__cli__1.json"
        without_text = json.loads(snapshot_path.read_text())
        self.assertFalse(without_text["pr_metadata"]["description_available"])
        self.assertIsNone(without_text["pr_metadata"]["title"])
        self.assertIsNone(without_text["pr_metadata"]["description"])
        self.assertEqual(build_request(without_text)["status"], "description_unavailable")
        with patch("risk_pr_agent.dataset.prepare_snapshot", side_effect=AssertionError("Git snapshot should be reused")):
            prepare_dataset([str(raw)], {"getsentry/cli": str(self.repo)}, str(out),
                            reviewed_per_repo=1, representative_per_repo=0, snapshot_metadata=metadata)
            self.assertEqual(json.loads(snapshot_path.read_text())["pr_metadata"], without_text["pr_metadata"])
            metadata["getsentry/cli#1"].update(title="CAPTURED_TITLE", body="",
                                               fetched_at="2026-09-02T00:00:00Z",
                                               merged_at=row(1)["merged_at"])
            refreshed_manifest = prepare_dataset([str(raw)], {"getsentry/cli": str(self.repo)}, str(out),
                                                 reviewed_per_repo=1, representative_per_repo=0, snapshot_metadata=metadata)
        refreshed = json.loads(snapshot_path.read_text())
        self.assertEqual(refreshed["cache_key"], without_text["cache_key"])
        self.assertEqual(refreshed_manifest["selection_hash"], second["selection_hash"])
        self.assertEqual(refreshed["pr_metadata"]["title"], "CAPTURED_TITLE")
        self.assertEqual(refreshed["pr_metadata"]["description"], "")
        self.assertTrue(refreshed["pr_metadata"]["description_available"])
        self.assertEqual(refreshed["pr_metadata"]["captured_at"], "2026-09-02T00:00:00Z")
        self.assertEqual(refreshed["pr_metadata"]["capture_kind"], "historical_collection_after_merge")
        self.assertEqual(build_request(refreshed)["status"], "ready")
        for packet_path in (out / "review").glob("getsentry__cli__1.*"):
            self.assertNotIn("CAPTURED_TITLE", packet_path.read_text())
            self.assertNotIn("pr_metadata", packet_path.read_text())

    def test_pr_text_is_captured_and_refreshed_without_rebuilding_or_exposing_blind_reviews(self):
        self.put("main.py", "before\n")
        base = self.commit("base")
        self.put("main.py", "after\n")
        head = self.commit("change")
        metadata = row(1, merge_commit_sha=head, data_source="github_api",
                       title="CAPTURED_TITLE", body="CAPTURED_BODY",
                       metrics={"commits": 1, "changed_files": 1, "additions": 1, "deletions": 1},
                       files=[{"filename": "main.py"}])
        snapshot = prepare_snapshot(metadata, str(self.repo))
        self.assertEqual(snapshot["status"], "ready")
        self.assertEqual(snapshot["pr_metadata"]["title"], "CAPTURED_TITLE")
        self.assertEqual(snapshot["pr_metadata"]["description"], "CAPTURED_BODY")
        self.assertTrue(snapshot["pr_metadata"]["description_available"])
        self.assertEqual(snapshot["pr_metadata"]["capture_kind"], "historical_collection_after_merge")
        raw = Path(self.temp.name) / "raw.jsonl"
        raw.write_text(json.dumps(metadata) + "\n")
        out = Path(self.temp.name) / "dataset"
        manifest = prepare_dataset([str(raw)], {"getsentry/cli": str(self.repo)}, str(out),
                                   reviewed_per_repo=1, representative_per_repo=0)
        snapshot_path = out / "snapshots/getsentry__cli__1.json"
        cached = json.loads(snapshot_path.read_text())
        original_cache_key = cached["cache_key"]
        # A ready cache created before PR text was captured must also refresh.
        del cached["pr_metadata"]
        snapshot_path.write_text(json.dumps(cached))
        metadata.update(title="REFRESHED_TITLE", body="REFRESHED_BODY", fetched_at="2026-09-02T00:00:00Z")
        raw.write_text(json.dumps(metadata) + "\n")
        with patch("risk_pr_agent.dataset.prepare_snapshot", side_effect=AssertionError("Git snapshot should be reused")):
            refreshed_manifest = prepare_dataset([str(raw)], {"getsentry/cli": str(self.repo)}, str(out),
                                                 reviewed_per_repo=1, representative_per_repo=0)
        refreshed = json.loads(snapshot_path.read_text())
        self.assertEqual(refreshed["cache_key"], original_cache_key)
        self.assertEqual(refreshed_manifest["selection_hash"], manifest["selection_hash"])
        self.assertEqual(refreshed["snapshot"]["base_sha"], base)
        self.assertEqual(refreshed["snapshot"]["head_sha"], head)
        self.assertEqual(refreshed["pr_metadata"]["title"], "REFRESHED_TITLE")
        self.assertEqual(refreshed["pr_metadata"]["description"], "REFRESHED_BODY")
        self.assertEqual(refreshed["pr_metadata"]["captured_at"], metadata["fetched_at"])
        exported = [json.loads(line) for path in manifest["inputs"].values()
                    for line in Path(path).read_text().splitlines()]
        self.assertEqual(exported[0]["pr_metadata"], refreshed["pr_metadata"])
        packets = list((out / "review").glob("getsentry__cli__1.*"))
        self.assertEqual({path.suffix for path in packets}, {".json", ".md"})
        for packet_path in packets:
            content = packet_path.read_text()
            self.assertNotIn("pr_metadata", content)
            self.assertNotIn("strata", content)
            self.assertNotIn("TITLE", content)
            self.assertNotIn("BODY", content)

    def test_older_rest_rows_without_source_marker_still_validate_counts_and_paths(self):
        self.put("main.py", "before\n")
        self.commit("base")
        self.put("main.py", "after\n")
        head = self.commit("change")
        metadata = row(1, id=10, merge_commit_sha=head,
                       metrics={"commits": 1, "changed_files": 1, "additions": 1, "deletions": 1},
                       files=[{"filename": "main.py"}])
        self.assertEqual(prepare_snapshot(metadata, str(self.repo))["status"], "ready")
        metadata["files"] = [{"filename": "other.py"}]
        metadata["metrics"]["additions"] = 2
        incomplete = prepare_snapshot(metadata, str(self.repo))
        self.assertEqual(incomplete["status"], "incomplete")
        self.assertIn("authoritative_paths_mismatch", incomplete["missing"])
        self.assertIn("authoritative_additions_mismatch", incomplete["missing"])

    def test_maximum_four_related_tests_with_full_assertions_and_boundaries(self):
        self.put("app/module.py", "original\n")
        for index in range(7):
            self.put(f"app/tests/test_{index}.py", "assert True\n")
        self.put("application/tests/test_unrelated.py", "assert False\n")
        self.commit("base")
        self.put("app/module.py", "new\n")
        head = self.commit("change")
        snapshot = prepare_snapshot(row(1, merge_commit_sha=head), str(self.repo))
        tests = [item for item in snapshot["repository_context"]["files"] if item["kind"] == "test"]
        self.assertEqual(len(tests), 4)
        self.assertTrue(all(item["path"].startswith("app/") for item in tests))
        self.assertTrue(all("assert True" in item["content"] for item in tests))

    def test_package_marker_changes_do_not_fill_related_test_slots(self):
        self.put("src/snuba/query/__init__.py", "original = True\n")
        self.put("src/snuba/query/foo.py", "def foo():\n    return 1\n")
        for path in ("tests/__init__.py", "tests/admin/__init__.py", "tests/query/__init__.py",
                     "tests/query/conftest.py", "tests/query/helpers.py", "tests/query/fixtures/foo.py"):
            self.put(path, "")
        for name in ("foo", "a", "b", "c", "d"):
            self.put(f"tests/query/test_{name}.py", "def test_query():\n    assert foo() == 1\n")
        self.put("tests/admin/test_foo.py", "assert unrelated()\n")
        self.put("tests/querying/test_case.py", "assert unrelated()\n")
        self.commit("base")
        self.put("src/snuba/query/__init__.py", "original = False\n")
        self.put("src/snuba/query/foo.py", "def foo():\n    return 2\n")
        snapshot = prepare_snapshot(row(1, merge_commit_sha=self.commit("change")), str(self.repo))
        tests = [item for item in snapshot["repository_context"]["files"] if item["kind"] == "test"]
        self.assertEqual([item["path"] for item in tests],
                         [f"tests/query/test_{name}.py" for name in ("foo", "a", "b", "c")])
        self.assertTrue(all("assert foo() == 1" in item["content"] for item in tests))

    def test_preparation_preserves_human_labels_and_separates_outcomes(self):
        self.put("main.py", "base\n")
        self.commit("base")
        rows = []
        for number in range(1, 5):
            self.put("main.py", f"value = {number}\n")
            rows.append(row(number, merge_commit_sha=self.commit("change")))
        raw = Path(self.temp.name) / "raw.jsonl"
        raw.write_text("\n".join(json.dumps(item) for item in rows))
        out = Path(self.temp.name) / "dataset"
        manifest = prepare_dataset([str(raw)], {"getsentry/cli": str(self.repo)}, str(out), reviewed_per_repo=3, representative_per_repo=1)
        self.assertEqual(manifest["snapshot_counts"], {"ready": 4})
        index = Path(manifest["review_index_path"]).read_text()
        self.assertIn("# Blind PR risk review", index)
        self.assertEqual(index.count("](getsentry__cli__"), 3)
        for packet_path in (out / "review").glob("getsentry__*.md"):
            packet = packet_path.read_text()
            self.assertIn("Snapshot kind:", packet)
            self.assertIn("```diff", packet)
            self.assertIn("diff --git", packet)
            self.assertNotIn("strong_outcome", packet)
            self.assertNotIn("strata", packet)
        labels = Path(manifest["labels_path"])
        original = labels.read_text()
        self.assertIn('"risk_label":null', original)
        first_label = json.loads(original.splitlines()[0])
        self.assertTrue(first_label["snapshot"]["head_sha"])
        self.assertTrue(all(item.get("snapshot", {}).get("head_sha") for item in manifest["reviewed"] + manifest["representative"]))
        snapshot_file = next((out / "snapshots").glob("*.json"))
        cached = json.loads(snapshot_file.read_text())
        cached["split"] = "stale"
        cached["strata"] = ["stale"]
        snapshot_file.write_text(json.dumps(cached))
        edited = original.replace('"risk_label":null', '"risk_label":"low"', 1)
        labels.write_text(edited)
        prepare_dataset([str(raw)], {"getsentry/cli": str(self.repo)}, str(out), reviewed_per_repo=3, representative_per_repo=1)
        self.assertEqual(labels.read_text(), edited)
        refreshed = json.loads(snapshot_file.read_text())
        self.assertNotEqual(refreshed["split"], "stale")
        self.assertNotEqual(refreshed["strata"], ["stale"])
        for path in manifest["inputs"].values():
            content = Path(path).read_text()
            self.assertNotIn('"risk_label"', content)
            self.assertNotIn('"outcomes"', content)
        for review_packet in (out / "review").glob("*.json"):
            self.assertNotIn("strata", json.loads(review_packet.read_text()))
        with self.assertRaisesRegex(ValueError, "selection changed"):
            prepare_dataset([str(raw)], {"getsentry/cli": str(self.repo)}, str(out), reviewed_per_repo=2, representative_per_repo=1)

    def test_resume_binds_only_untouched_label_templates_to_newly_available_commits(self):
        self.put("main.py", "base\n")
        self.commit("base")
        rows = []
        for number in range(1, 4):
            self.put("main.py", f"value = {number}\n")
            rows.append(row(number, merge_commit_sha=self.commit("change")))
        raw = Path(self.temp.name) / "raw.jsonl"
        raw.write_text("\n".join(json.dumps(item) for item in rows))
        out = Path(self.temp.name) / "dataset"
        first = prepare_dataset([str(raw)], {}, str(out), reviewed_per_repo=3, representative_per_repo=0)
        labels_path = Path(first["labels_path"])
        labels = [json.loads(line) for line in labels_path.read_text().splitlines()]
        self.assertTrue(all(item["snapshot"]["head_sha"] is None for item in labels))
        labels[1]["rationale"] = "A human has started reviewing this case."
        labels[2].update(risk_label="high", reviewer="human", reviewed_at="2026-09-01T00:00:00Z", rationale="Reviewed.")
        labels_path.write_text("\n".join(json.dumps(item) for item in labels) + "\n")
        prepare_dataset([str(raw)], {"getsentry/cli": str(self.repo)}, str(out), reviewed_per_repo=3, representative_per_repo=0)
        updated = [json.loads(line) for line in labels_path.read_text().splitlines()]
        self.assertTrue(updated[0]["snapshot"]["base_sha"])
        self.assertTrue(updated[0]["snapshot"]["head_sha"])
        self.assertEqual({key: value for key, value in updated[0].items() if key != "snapshot"}, {key: value for key, value in labels[0].items() if key != "snapshot"})
        self.assertEqual(updated[1:], labels[1:])


if __name__ == "__main__":
    unittest.main()
