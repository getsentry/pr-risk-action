import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from risk_pr_agent import action_runner, commands
from risk_pr_agent.github import GitHubError, RepoRef


BASE = "a" * 40
HEAD = "b" * 40


def pull_request(base=BASE, head=HEAD):
    return {"number": 7, "id": 10, "state": "open", "merged_at": None,
            "title": "Clarify installation", "body": "Explain the existing setup.",
            "base": {"sha": base, "repo": {"private": False}}, "head": {"sha": head},
            "commits": 1, "changed_files": 1, "additions": 1, "deletions": 1}


def classification(base=BASE, head=HEAD):
    return {"schema_version": 2, "repo": "example/project", "number": 7,
            "candidate": True, "input_profile": "metadata-diff", "status": "ok",
            "risk_label": "low", "probabilities": {"low": .9, "medium": .08, "high": .02},
            "snapshot": {"source_base_sha": base, "source_head_sha": head}}


class ActionRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.environment = patch.dict(os.environ, {
            "AI_GATEWAY_API_KEY": "gateway-test-key", "GITHUB_TOKEN": "github-test-key",
            "GITHUB_WORKSPACE": str(self.directory), "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_OUTPUT": str(self.directory / "output"),
            "GITHUB_STEP_SUMMARY": str(self.directory / "summary"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def invoke(self, repo="example/project", number="7", expected=""):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = action_runner.run_action(repo, number, "result.json", expected)
        return code, json.loads((self.directory / "result.json").read_text()), stream.getvalue()

    def test_missing_key_fails_without_network_or_git_or_loading_caller_env(self):
        (self.directory / ".env").write_text("AI_GATEWAY_API_KEY=untrusted-env-key\n")
        with patch.dict(os.environ, {"AI_GATEWAY_API_KEY": ""}), \
                patch.object(action_runner, "GitHubClient") as client, \
                patch.object(action_runner, "_fetch_git") as fetch:
            code, result, summary = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "missing_credentials")
        self.assertIsNone(result["risk_label"])
        self.assertNotIn("untrusted-env-key", summary)
        client.assert_not_called()
        fetch.assert_not_called()

    def test_untrusted_inputs_are_rejected_before_network(self):
        cases = [("example/project;echo injected", "7", "", "invalid_repository"),
                 ("example/..", "7", "", "invalid_repository"),
                 ("example/project", "7\nstatus=ok", "", "invalid_pr_number"),
                 ("example/project", "--help", "", "invalid_pr_number"),
                 ("example/project", "7", "$(echo injected)", "invalid_revision")]
        for repo, number, expected, status in cases:
            with self.subTest(repo=repo, number=number, expected=expected), \
                    patch.object(action_runner, "GitHubClient") as client:
                code, result, _ = self.invoke(repo, number, expected)
                self.assertEqual(code, 1)
                self.assertEqual(result["status"], status)
                client.assert_not_called()

    def test_success_publishes_only_verified_result(self):
        with patch.object(action_runner.GitHubClient, "get_pull_request", return_value=pull_request()), \
                patch.object(action_runner, "_fetch_git"), \
                patch.object(action_runner, "_run_scorer", return_value=classification()):
            code, result, summary = self.invoke(expected=HEAD)
        self.assertEqual(code, 0)
        self.assertEqual(result["risk_label"], "low")
        self.assertIn("Jev candidate", summary)
        self.assertEqual((self.directory / "output").read_text(), "risk_label=low\nstatus=ok\n")

    def test_old_trigger_does_not_score_a_new_head(self):
        with patch.object(action_runner.GitHubClient, "get_pull_request", return_value=pull_request()), \
                patch.object(action_runner, "_fetch_git") as fetch:
            code, result, _ = self.invoke(expected="c" * 40)
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "pr_changed")
        fetch.assert_not_called()

    def test_movement_during_scoring_discards_label_and_probabilities(self):
        for source_changed in (True, False):
            with self.subTest(source_changed=source_changed):
                scored = classification(head="c" * 40) if source_changed else classification()
                responses = [pull_request(), pull_request(head="d" * 40)]
                with patch.object(action_runner.GitHubClient, "get_pull_request", side_effect=responses), \
                        patch.object(action_runner, "_fetch_git"), \
                        patch.object(action_runner, "_run_scorer", return_value=scored):
                    code, result, _ = self.invoke()
                self.assertEqual(code, 1)
                self.assertEqual(result["status"], "pr_changed")
                self.assertIsNone(result["risk_label"])
                self.assertIsNone(result["probabilities"])
                self.assertNotIn("Risk: `low`", (self.directory / "summary").read_text())
                self.assertNotIn("risk_label=low", (self.directory / "output").read_text())

    def test_context_rejection_remains_visible_and_unclassified(self):
        scored = classification()
        scored.update(status="context_rejected", risk_label=None, probabilities=None)
        with patch.object(action_runner.GitHubClient, "get_pull_request", return_value=pull_request()), \
                patch.object(action_runner, "_fetch_git"), \
                patch.object(action_runner, "_run_scorer", return_value=scored):
            code, result, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "context_rejected")
        self.assertIsNone(result["risk_label"])

    def test_private_closed_or_wrong_pr_fail_before_fetch(self):
        for field, value, status in [("private", True, "unsupported_repository"),
                                     ("state", "closed", "pr_not_open"),
                                     ("number", 8, "pr_identity_mismatch")]:
            response = pull_request()
            if field == "private":
                response["base"]["repo"][field] = value
            else:
                response[field] = value
            with self.subTest(field=field), \
                    patch.object(action_runner.GitHubClient, "get_pull_request", return_value=response), \
                    patch.object(action_runner, "_fetch_git") as fetch:
                code, result, _ = self.invoke()
                self.assertEqual(code, 1)
                self.assertEqual(result["status"], status)
                fetch.assert_not_called()

    def test_http_error_body_and_credentials_are_not_published(self):
        with patch.object(action_runner.GitHubClient, "get_pull_request",
                          side_effect=GitHubError("body contains github-test-key and PRIVATE_MARKER")):
            code, result, summary = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "github_error")
        for text in (json.dumps(result), summary):
            self.assertNotIn("github-test-key", text)
            self.assertNotIn("PRIVATE_MARKER", text)

    def test_missing_status_is_invalid_result(self):
        scored = classification()
        del scored["status"]
        with patch.object(action_runner.GitHubClient, "get_pull_request", return_value=pull_request()), \
                patch.object(action_runner, "_fetch_git"), \
                patch.object(action_runner, "_run_scorer", return_value=scored):
            code, result, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "invalid_result")

    def test_child_scoring_has_isolated_cwd_outputs_cache_and_source(self):
        child = self.directory / "child"
        child.mkdir()

        def run(args, **kwargs):
            self.assertEqual(args[:4], [sys.executable, "-m", "risk_pr_agent.cli", "score-pr"])
            self.assertIn("--candidate", args)
            self.assertEqual(kwargs["cwd"], child)
            self.assertEqual(kwargs["env"]["PYTHONPATH"], str(action_runner.SOURCE_ROOT / "src"))
            self.assertNotIn("GITHUB_OUTPUT", kwargs["env"])
            self.assertNotIn("GITHUB_STEP_SUMMARY", kwargs["env"])
            self.assertNotIn("GIT_DIR", kwargs["env"])
            self.assertEqual(kwargs["env"]["AI_GATEWAY_API_KEY"], "gateway-test-key")
            self.assertEqual(args[args.index("--cache-dir") + 1], str(child / "cache"))
            Path(args[args.index("--out") + 1]).write_text(json.dumps(classification()))

        with patch.dict(os.environ, {"GIT_DIR": "/untrusted/checkout/.git", "PYTHONPATH": "/untrusted/src"}), \
                patch.object(action_runner.subprocess, "run", side_effect=run):
            result = action_runner._run_scorer(RepoRef.parse("example/project"), 7, child)
        self.assertEqual(result["risk_label"], "low")

    def test_git_and_scorer_failures_have_safe_distinct_statuses(self):
        error = subprocess.CalledProcessError(1, ["PRIVATE_MARKER"], stderr=b"github-test-key")
        for operation, status in [("git", "git_fetch_failed"), ("scorer", "scorer_failed")]:
            with self.subTest(operation=operation), \
                    patch.object(action_runner.GitHubClient, "get_pull_request", return_value=pull_request()), \
                    patch.object(action_runner.subprocess, "run", side_effect=error):
                if operation == "scorer":
                    with patch.object(action_runner, "_fetch_git"):
                        code, result, summary = self.invoke()
                else:
                    code, result, summary = self.invoke()
            self.assertEqual(code, 1)
            self.assertEqual(result["status"], status)
            self.assertNotIn("PRIVATE_MARKER", summary + json.dumps(result))


    def test_actual_bare_git_and_public_cli_with_mocked_api_and_jev(self):
        source = self.directory / "untrusted-source"
        source.mkdir()
        original_run = subprocess.run

        def git(*args):
            return original_run(["git", "-C", str(source), *args], check=True,
                                capture_output=True).stdout.decode().strip()

        git("init", "-q")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        (source / "README.md").write_text("Install the package.\n")
        (source / ".env").write_text("AI_GATEWAY_API_KEY=untrusted-env-key\n")
        (source / "package.json").write_text(json.dumps({"scripts": {"postinstall": "touch OWNED"}}))
        git("add", ".")
        git("commit", "-qm", "base")
        base = git("rev-parse", "HEAD")
        (source / "README.md").write_text("Install the package with npm.\n")
        git("commit", "-qam", "change")
        head = git("rev-parse", "HEAD")
        api_pr = pull_request(base, head)
        requests = []
        temporary_paths = []

        def api_response(path, params=None):
            if path.endswith("/files"):
                return [{"filename": "README.md", "status": "modified", "additions": 1, "deletions": 1}]
            return copy.deepcopy(api_pr)

        def subprocess_boundary(args, **kwargs):
            if args[:3] == [sys.executable, "-m", "risk_pr_agent.cli"]:
                directory = Path(kwargs["cwd"])
                temporary_paths.append(directory)
                self.assertFalse((directory / ".env").exists())
                self.assertFalse((directory / "target.git" / "README.md").exists())
                self.assertEqual(original_run(["git", "-C", str(directory / "target.git"),
                                               "rev-parse", "--is-bare-repository"],
                                              check=True, capture_output=True).stdout.strip(), b"true")
                previous = Path.cwd()
                try:
                    os.chdir(directory)
                    with patch.dict(os.environ, kwargs["env"], clear=True):
                        self.assertEqual(commands.main(args[3:]), 0)
                finally:
                    os.chdir(previous)
                return subprocess.CompletedProcess(args, 0)
            if "fetch" in args:
                for secret in ("AI_GATEWAY_API_KEY", "GITHUB_TOKEN", "GH_TOKEN"):
                    self.assertNotIn(secret, kwargs["env"])
                self.assertEqual(kwargs["env"]["GIT_CONFIG_GLOBAL"], os.devnull)
                self.assertNotIn("GIT_CONFIG_VALUE_0", kwargs["env"])
                # Only the transport endpoint changes; Git still really fetches
                # both pinned revisions with their ancestry into a bare DB.
                args = [str(source) if value == "https://github.com/example/project.git" else
                        "protocol.file.allow=always" if value == "protocol.file.allow=never" else value
                        for value in args]
            return original_run(args, **kwargs)

        def classify(request):
            requests.append(request)
            return {"status": "ok", "risk_label": "low", "probabilities": {"low": .9, "medium": .08, "high": .02},
                    "usage": {"input_tokens": 100, "output_tokens": 3}, "reported_cost_usd": .001}

        with patch("risk_pr_agent.github.GitHubClient.request_json", side_effect=api_response), \
                patch("risk_pr_agent.jev.fetch_price_snapshot", return_value={"status": "unavailable"}), \
                patch("risk_pr_agent.jev.Worker") as worker, \
                patch.object(action_runner.subprocess, "run", side_effect=subprocess_boundary):
            worker.return_value.side_effect = classify
            code, result, _ = self.invoke(expected=head)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["risk_label"], "low")
        self.assertEqual(result["snapshot"]["head_sha"], head)
        self.assertEqual(result["snapshot"]["base_sha"], base)
        self.assertEqual(requests[0]["state"]["pr"]["description"], api_pr["body"])
        self.assertIn("+Install the package with npm.", requests[0]["state"]["files"][0]["patch"])
        self.assertFalse((source / "OWNED").exists())
        self.assertTrue(temporary_paths)
        self.assertTrue(all(not path.exists() for path in temporary_paths))


if __name__ == "__main__":
    unittest.main()
