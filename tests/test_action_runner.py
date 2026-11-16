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

from risk_pr_agent import action_runner, commands, labels
from risk_pr_agent.github import GitHubError, RepoRef
from test_labels import ENCODED, FakeGitHub, ISSUE_LABELS, REPO_LABELS


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


def sdk_result_fields():
    return {"model_label": "low", "low_blocked_by": [{"rule": "R4", "files": ["core/api/core.api"]}],
            "mechanisms": {"m_contract": .9, "m_ships": .1},
            "sdk": {"rubric_version": "1", "facts_hash": "h", "families_touched": ["server"],
                    "shared_core": False, "files": []}}


class RunnerTestCase(unittest.TestCase):
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

    def invoke(self, repo="example/project", number="7", expected="", **options):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = action_runner.run_action(repo, number, "result.json", expected, **options)
        return code, json.loads((self.directory / "result.json").read_text()), stream.getvalue()

    def assert_no_model_verdict(self, result):
        for key in ("model_label", "low_blocked_by", "mechanisms"):
            self.assertNotIn(key, result)
        for text in ((self.directory / "summary").read_text(), (self.directory / "output").read_text()):
            for line in ("Model label", "Rules that block low", "Raised from", "- R4", "Mechanisms:"):
                self.assertNotIn(line, text)


class ActionRunnerTests(RunnerTestCase):
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

    def test_summary_output_block_is_delimited_safely(self):
        result = {**classification(), "status": "ok", "risk_label": "medium"}
        out = self.directory / "output"
        out.write_text("")
        action_runner._publish(result, "result.json")
        text = out.read_text()
        head, rest = text.split("summary<<", 1)
        delim, rest = rest.split("\n", 1)
        body, tail = rest.split("\n" + delim + "\n", 1)
        self.assertEqual(body + "\n", commands.risk_summary(result) + "\nJev candidate result; this check does not authorize merging.\n")
        self.assertNotIn(delim, body)
        self.assertIn("risk_label=medium", text)
        self.assertIn("status=ok", text)

    def test_summary_output_regenerates_a_colliding_delimiter(self):
        result = {**classification(), "status": "ok"}
        out = self.directory / "output"
        out.write_text("")
        with patch.object(action_runner.secrets, "token_hex", side_effect=["Status", "f" * 32]):
            action_runner._publish(result, "result.json")
        self.assertIn("summary<<" + "f" * 32 + "\n", out.read_text())

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
        output = (self.directory / "output").read_text()
        self.assertTrue(output.startswith("risk_label=low\nstatus=ok\nsummary<<"))
        self.assertEqual(output.count("risk_label="), 1)

    def test_old_trigger_does_not_score_a_new_head(self):
        with patch.object(action_runner.GitHubClient, "get_pull_request", return_value=pull_request()), \
                patch.object(action_runner, "_fetch_git") as fetch:
            code, result, _ = self.invoke(expected="c" * 40)
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "pr_changed")
        fetch.assert_not_called()

    def test_movement_during_scoring_discards_label_and_probabilities(self):
        for source_changed, sdk_shaped in ((True, False), (False, False), (True, True), (False, True)):
            with self.subTest(source_changed=source_changed, sdk_shaped=sdk_shaped):
                scored = classification(head="c" * 40) if source_changed else classification()
                if sdk_shaped:
                    scored.update(sdk_result_fields())
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
                self.assert_no_model_verdict(result)

    def test_rejected_sdk_result_publishes_no_model_verdict(self):
        cases = [("invalid_result", {"risk_label": "critical"}),
                 ("invalid_response", {"status": "invalid_response", "risk_label": None, "probabilities": None})]
        for status, change in cases:
            scored = classification()
            scored.update(sdk_result_fields(), **change)
            with self.subTest(status=status), \
                    patch.object(action_runner.GitHubClient, "get_pull_request", return_value=pull_request()), \
                    patch.object(action_runner, "_fetch_git"), \
                    patch.object(action_runner, "_run_scorer", return_value=scored):
                for name in ("summary", "output"):
                    (self.directory / name).write_text("")
                code, result, _ = self.invoke()
                self.assertEqual(code, 1)
                self.assertEqual(result["status"], status)
                self.assertIsNone(result["risk_label"])
                self.assertEqual(result["sdk"]["families_touched"], ["server"])
                self.assert_no_model_verdict(result)

    def test_missing_sdk_data_fails_before_network(self):
        with patch.object(action_runner, "SOURCE_ROOT", self.directory / "incomplete-checkout"), \
                patch.object(action_runner, "GitHubClient") as client, \
                patch.object(action_runner, "_fetch_git") as fetch:
            code, result, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "missing_sdk_data")
        self.assertIsNone(result["risk_label"])
        client.assert_not_called()
        fetch.assert_not_called()

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
            self.assertGreater(kwargs["timeout"], 4 * 65 + 10 + 20 + 30)
            self.assertLess(kwargs["timeout"], 600)
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


class LabelPublishingTests(RunnerTestCase):
    def run_enabled(self, fake, scored=None, responses=None, events=None):
        events = fake.events if events is None else events
        scored = classification() if scored is None else scored

        def score(*args, **kwargs):
            events.append(("score",))
            return copy.deepcopy(scored)

        prs = responses or [pull_request()] * 4
        with patch.object(labels, "default_transport", return_value=fake), \
                patch.object(labels.time, "sleep"), \
                patch.object(action_runner.GitHubClient, "get_pull_request", side_effect=prs), \
                patch.object(action_runner, "_fetch_git") as fetch, \
                patch.object(action_runner, "_run_scorer", side_effect=score) as scorer:
            code, result, _ = self.invoke(publish_label=True)
        return code, result, (self.directory / "output").read_text(), fetch, scorer

    def test_disabled_makes_no_write_and_keeps_result_and_outputs(self):
        with patch.object(labels, "default_transport") as transport, \
                patch.object(action_runner.GitHubClient, "get_pull_request", return_value=pull_request()), \
                patch.object(action_runner, "_fetch_git"), \
                patch.object(action_runner, "_run_scorer", return_value=classification()) as scorer:
            code, result, _ = self.invoke(expected=HEAD)
        self.assertEqual(code, 0)
        self.assertEqual(result, classification())
        transport.assert_not_called()
        self.assertTrue(scorer.call_args.kwargs.get("share_github_token", True))
        output = (self.directory / "output").read_text()
        self.assertTrue(output.startswith("risk_label=low\nstatus=ok\nsummary<<"))
        self.assertTrue(output.endswith("\nlabel_published=false\n"))

    def test_disabled_early_exit_writes_label_published_false_only_to_outputs(self):
        with patch.dict(os.environ, {"AI_GATEWAY_API_KEY": ""}), \
                patch.object(labels, "default_transport") as transport:
            code, result, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertNotIn("label_published", result)
        transport.assert_not_called()
        self.assertIn("label_published=false\n", (self.directory / "output").read_text())

    def test_enabled_early_exit_touches_no_label(self):
        with patch.dict(os.environ, {"AI_GATEWAY_API_KEY": ""}), \
                patch.object(labels, "default_transport") as transport:
            code, result, _ = self.invoke(publish_label=True)
        self.assertEqual((code, result["status"], result["label_published"]), (1, "missing_credentials", False))
        transport.assert_not_called()

    def test_old_labels_are_cleared_before_scoring_then_final_label_added(self):
        fake = FakeGitHub(pr_labels={"risk: low", "risk: high", "bug"})
        scored = classification()
        scored["risk_label"] = "medium"
        code, result, output, _, scorer = self.run_enabled(fake, scored)
        self.assertEqual(code, 0)
        self.assertEqual(fake.events, [("removed", "risk: low"), ("removed", "risk: high"), ("score",),
                                       ("added", "risk: medium")])
        self.assertEqual(fake.pr_labels, {"bug", "risk: medium"})
        self.assertIs(result["label_published"], True)
        self.assertEqual(result["status"], "ok")
        self.assertIn("risk_label=medium\n", output)
        self.assertTrue(output.endswith("\nlabel_published=true\n"))
        self.assertFalse(scorer.call_args.kwargs["share_github_token"])
        allowed = {ISSUE_LABELS, REPO_LABELS} | {f"{ISSUE_LABELS}/{value}" for value in ENCODED.values()} | {
            f"{REPO_LABELS}/{value}" for value in ENCODED.values()}
        for _, url, _ in fake.calls:
            self.assertIn(url, allowed)

    def test_missing_repository_label_is_created_and_concurrent_creation_tolerated(self):
        for concurrent in (False, True):
            with self.subTest(concurrent=concurrent):
                fake = FakeGitHub(repo_labels=())
                if concurrent:
                    fake.fail = lambda method, url, body: 422 if (method, url) == ("POST", REPO_LABELS) else None
                code, result, _, _, _ = self.run_enabled(fake)
                self.assertEqual((code, result["status"], result["label_published"]), (0, "ok", True))
                self.assertIn(("POST", REPO_LABELS, {"name": "risk: low", "color": "0E8A16",
                                                     "description": "PR risk score: low"}), fake.calls)
                self.assertEqual(fake.pr_labels, {"risk: low"})

    def test_failed_clearing_stops_before_scoring(self):
        fake = FakeGitHub(pr_labels={"risk: low"},
                          fail=lambda method, url, body: 403 if method == "DELETE" else None)
        code, result, output, fetch, scorer = self.run_enabled(fake)
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "label_error")
        self.assertIsNone(result["risk_label"])
        self.assertIs(result["label_published"], False)
        scorer.assert_not_called()
        fetch.assert_not_called()
        self.assertIn("risk_label=\nstatus=label_error\n", output)

    def test_unclassified_result_leaves_the_pr_without_a_risk_label(self):
        scored = classification()
        scored.update(sdk_result_fields(), status="invalid_fact_sheet", risk_label=None, probabilities=None)
        fake = FakeGitHub(pr_labels={"risk: low"})
        code, result, _, _, _ = self.run_enabled(fake, scored)
        self.assertEqual((code, result["status"], result["label_published"]), (1, "invalid_fact_sheet", False))
        self.assertEqual(fake.pr_labels, set())
        self.assertNotIn("POST", [method for method, _, _ in fake.calls])

    def test_pr_moved_or_closed_before_publishing_adds_nothing(self):
        closed = pull_request()
        closed["state"] = "closed"
        for name, moved in (("head", pull_request(head="d" * 40)), ("base", pull_request(base="e" * 40)),
                            ("closed", closed)):
            with self.subTest(name=name):
                scored = classification()
                scored.update(sdk_result_fields())
                fake = FakeGitHub()
                for path in ("summary", "output"):
                    (self.directory / path).write_text("")
                code, result, output, _, _ = self.run_enabled(fake, scored, [pull_request(), pull_request(), moved])
                self.assertEqual(code, 1)
                self.assertEqual(result["status"], "pr_changed")
                self.assertIsNone(result["risk_label"])
                self.assertIsNone(result["probabilities"])
                self.assertIs(result["label_published"], False)
                self.assertEqual(fake.calls, [("DELETE", f"{ISSUE_LABELS}/{ENCODED[level]}", None)
                                              for level in ("low", "medium", "high")])
                self.assert_no_model_verdict(result)

    def test_pr_moved_after_adding_removes_the_added_label(self):
        fake = FakeGitHub()
        code, result, output, _, _ = self.run_enabled(
            fake, responses=[pull_request(), pull_request(), pull_request(), pull_request(head="d" * 40)])
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "pr_changed")
        self.assertIs(result["label_published"], False)
        self.assertEqual(fake.events[-2:], [("added", "risk: low"), ("removed", "risk: low")])
        self.assertEqual(fake.pr_labels, set())
        self.assertIn("risk_label=\n", output)

    def test_forbidden_add_reports_label_error_after_attempting_removal(self):
        fake = FakeGitHub(fail=lambda method, url, body: 403 if (method, url) == ("POST", ISSUE_LABELS) else None)
        code, result, output, _, _ = self.run_enabled(fake)
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "label_error")
        self.assertIsNone(result["risk_label"])
        self.assertIs(result["label_published"], False)
        self.assertEqual(fake.calls[-1], ("DELETE", f"{ISSUE_LABELS}/{ENCODED['low']}", None))
        self.assertIn("risk_label=\nstatus=label_error\n", output)
        self.assertNotIn("risk_label=low", output)

    def test_failed_publish_reread_reports_label_error(self):
        fake = FakeGitHub()
        code, result, _, _, _ = self.run_enabled(
            fake, responses=[pull_request(), pull_request(), GitHubError("PRIVATE_MARKER github-test-key")])
        self.assertEqual((code, result["status"], result["label_published"]), (1, "label_error", False))
        self.assertNotIn("PRIVATE_MARKER", json.dumps(result))

    def test_published_label_is_the_final_label_not_the_model_label(self):
        scored = classification()
        scored.update(sdk_result_fields(), risk_label="medium")
        fake = FakeGitHub()
        code, result, _, _, _ = self.run_enabled(fake, scored)
        self.assertEqual((code, result["model_label"], result["risk_label"]), (0, "low", "medium"))
        self.assertEqual(fake.pr_labels, {"risk: medium"})

    def test_publish_label_input_must_be_exactly_true(self):
        for value, enabled in (("true", True), ("false", False), ("", False), ("True", False), ("yes", False)):
            with self.subTest(value=value), \
                    patch.dict(os.environ, {"INPUT_PUBLISH_LABEL": value, "INPUT_REPO": "example/project",
                                            "INPUT_PR_NUMBER": "7"}), \
                    patch.object(action_runner, "run_action", return_value=0) as run:
                action_runner.main()
            self.assertEqual(run.call_args.kwargs["publish_label"], enabled)

    def test_scorer_receives_github_token_only_when_publishing_is_off(self):
        child = self.directory / "child"
        child.mkdir()
        seen = []

        def run(args, **kwargs):
            seen.append(kwargs["env"])
            Path(args[args.index("--out") + 1]).write_text(json.dumps(classification()))

        with patch.dict(os.environ, {"GH_TOKEN": "gh-test-key"}), \
                patch.object(action_runner.subprocess, "run", side_effect=run):
            action_runner._run_scorer(RepoRef.parse("example/project"), 7, child)
            action_runner._run_scorer(RepoRef.parse("example/project"), 7, child, share_github_token=False)
        self.assertEqual(seen[0]["GITHUB_TOKEN"], "github-test-key")
        for key in ("GITHUB_TOKEN", "GH_TOKEN"):
            self.assertNotIn(key, seen[1])
        self.assertEqual(seen[1]["AI_GATEWAY_API_KEY"], "gateway-test-key")


if __name__ == "__main__":
    unittest.main()
