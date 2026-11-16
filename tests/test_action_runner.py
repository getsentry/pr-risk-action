import contextlib
import copy
import io
import json
import os
from pathlib import Path
import signal
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from risk_pr_agent import action_runner, commands, labels
from risk_pr_agent.github import GitHubError, RepoRef
from test_labels import CheckedFakes, ENCODED, FakeGitHub, ISSUE_LABELS, REPO_LABELS


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


def sdk_classification(risk_label="low"):
    """An SDK-mode scorer result, the only kind whose label is published."""
    scored = classification()
    scored.update(sdk_result_fields(), risk_label=risk_label, low_blocked_by=[])
    return scored


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


def output_values(text):
    """Single-line outputs from a GITHUB_OUTPUT file (the summary block is skipped)."""
    values, lines = {}, iter(text.splitlines())
    for line in lines:
        if "<<" in line and "=" not in line.split("<<", 1)[0]:
            delimiter = line.split("<<", 1)[1]
            for inner in lines:
                if inner == delimiter:
                    break
            continue
        key, _, value = line.partition("=")
        values.setdefault(key, []).append(value)
    return values


class ConsumerOutputTests(CheckedFakes, RunnerTestCase):
    def run_scored(self, scored, publish_label, responses=None):
        for path in ("summary", "output"):
            (self.directory / path).write_text("")
        with patch.object(labels, "default_transport", return_value=self.fake()), \
                patch.object(labels.time, "sleep"), \
                patch.object(action_runner.GitHubClient, "get_pull_request",
                             side_effect=responses or [pull_request()] * 4), \
                patch.object(action_runner, "_fetch_git"), \
                patch.object(action_runner, "_run_scorer", return_value=copy.deepcopy(scored)):
            code, result, _ = self.invoke(expected=HEAD, publish_label=publish_label)
        return code, result, output_values((self.directory / "output").read_text())

    def test_sdk_result_exposes_model_label_and_scored_revision(self):
        scored = sdk_classification("medium")
        for publish_label in (False, True):
            with self.subTest(publish_label=publish_label):
                code, _, values = self.run_scored(scored, publish_label)
                self.assertEqual(code, 0)
                self.assertEqual(values["model_label"], ["low"])
                self.assertEqual(values["head_sha"], [HEAD])
                self.assertEqual(values["base_sha"], [BASE])
                self.assertEqual(values["risk_label"], ["medium"])

    def test_default_mode_result_has_no_model_label_but_has_the_revision(self):
        code, _, values = self.run_scored(classification(), False)
        self.assertEqual(code, 0)
        self.assertEqual((values["model_label"], values["head_sha"], values["base_sha"]), ([""], [HEAD], [BASE]))

    def test_unclassified_result_exposes_nothing(self):
        code, _, values = self.run_scored(sdk_classification(), False,
                                          responses=[pull_request(), pull_request(head="d" * 40)])
        self.assertEqual(code, 1)
        self.assertEqual((values["status"], values["model_label"], values["head_sha"], values["base_sha"]),
                         (["pr_changed"], [""], [""], [""]))

    def test_early_exit_writes_empty_values(self):
        with patch.dict(os.environ, {"AI_GATEWAY_API_KEY": ""}):
            self.invoke()
        values = output_values((self.directory / "output").read_text())
        self.assertEqual((values["model_label"], values["head_sha"], values["base_sha"]), ([""], [""], [""]))

    def test_values_are_validated_before_writing(self):
        result = sdk_classification()
        result.update(model_label="low\nstatus=ok", snapshot={"source_head_sha": "b" * 39 + "\n",
                                                             "source_base_sha": "not-a-sha"})
        (self.directory / "output").write_text("")
        action_runner._publish(result, "result.json")
        values = output_values((self.directory / "output").read_text())
        self.assertEqual((values["model_label"], values["head_sha"], values["base_sha"]), ([""], [""], [""]))
        self.assertEqual(values["status"], ["ok"])

    def test_outputs_keep_their_order_and_end_with_label_published(self):
        self.run_scored(sdk_classification(), False)
        text = (self.directory / "output").read_text()
        self.assertTrue(text.startswith("risk_label=low\nstatus=ok\nsummary<<"))
        self.assertTrue(text.endswith(f"\nmodel_label=low\nhead_sha={HEAD}\nbase_sha={BASE}\nlabel_published=false\n"))

    def test_action_declares_the_outputs(self):
        text = (action_runner.SOURCE_ROOT / "action.yml").read_text(encoding="utf-8")
        for name, key in (("model-label", "model_label"), ("head-sha", "head_sha"), ("base-sha", "base_sha")):
            with self.subTest(name=name):
                self.assertIn(f"  {name}:\n", text)
                self.assertIn(f"value: ${{{{ steps.score.outputs.{key} }}}}", text)


class LabelPublishingTests(CheckedFakes, RunnerTestCase):
    def run_enabled(self, fake, scored=None, responses=None, events=None):
        events = fake.events if events is None else events
        scored = sdk_classification() if scored is None else scored

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

    def test_enabled_exits_before_clearing_touch_no_label(self):
        cases = [("example/..", "7", {}, "invalid_repository"),
                 ("example/project", "07", {}, "invalid_pr_number"),
                 ("example/project", "7", {"GITHUB_SERVER_URL": "https://ghe.example.invalid"},
                  "unsupported_repository")]
        for repo, number, environment, status in cases:
            with self.subTest(status=status), patch.dict(os.environ, environment), \
                    patch.object(labels, "default_transport") as transport:
                code, result, _ = self.invoke(repo, number, publish_label=True)
            self.assertEqual((code, result["status"], result["label_published"]), (1, status, False))
            transport.assert_not_called()

    def test_enabled_later_exits_have_already_cleared_old_labels(self):
        cases = [("missing_credentials", patch.dict(os.environ, {"AI_GATEWAY_API_KEY": ""})),
                 ("missing_sdk_data", patch.object(action_runner, "SOURCE_ROOT", self.directory / "incomplete")),
                 ("invalid_revision", None)]
        for status, condition in cases:
            with self.subTest(status=status), condition or contextlib.nullcontext(), \
                    patch.object(labels, "default_transport",
                                 return_value=self.fake(pr_labels={"risk: low", "bug"})) as transport, \
                    patch.object(action_runner, "GitHubClient") as client:
                code, result, _ = self.invoke(expected="not-a-sha" if status == "invalid_revision" else "",
                                              publish_label=True)
            self.assertEqual((code, result["status"], result["label_published"]), (1, status, False))
            self.assertEqual(transport.return_value.pr_labels, {"bug"})
            client.assert_not_called()

    def test_disabled_ignores_label_fields_from_the_scorer(self):
        scored = classification()
        scored.update(label_published=True, label_cleanup_failed=True)
        with patch.object(action_runner.GitHubClient, "get_pull_request", return_value=pull_request()), \
                patch.object(action_runner, "_fetch_git"), \
                patch.object(action_runner, "_run_scorer", return_value=scored):
            code, _, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertTrue((self.directory / "output").read_text().endswith("\nlabel_published=false\n"))
        self.assertNotIn("may still be on the PR", (self.directory / "summary").read_text())

    def test_tls_errors_from_the_transport_still_write_a_result(self):
        moved = [pull_request(), pull_request(), pull_request(), pull_request(head="d" * 40)]
        cases = [("clear", lambda method, url: method == "DELETE", None, False),
                 ("add", lambda method, url: (method, url) == ("POST", ISSUE_LABELS), None, False),
                 ("cleanup", lambda method, url: method == "DELETE" and ("added", "risk: low") in fake.events,
                  moved, True)]
        for name, raises, responses, cleanup_failed in cases:
            with self.subTest(name=name):
                fake = self.fake(pr_labels={"risk: high"})

                def fail(method, url, body):
                    if raises(method, url):
                        raise ssl.SSLError("tls failure")
                    return None

                fake.fail = fail
                for path in ("summary", "output"):
                    (self.directory / path).write_text("")
                code, result, output, _, scorer = self.run_enabled(fake, responses=responses)
                self.assertEqual((code, result["status"], result["label_published"]), (1, "label_error", False))
                self.assertIn("status=label_error\n", output)
                self.assertEqual(result.get("label_cleanup_failed", False), cleanup_failed)
                if name == "clear":
                    scorer.assert_not_called()
                if cleanup_failed:
                    # Clearing, re-clearing the other two before the add, then three
                    # clean-up attempts each retried like a server error.
                    self.assertEqual([m for m, _, _ in fake.calls].count("DELETE"), 3 + 2 + 3 * 3)
                    self.assertIn("may still be on the PR", (self.directory / "summary").read_text())

    def test_old_labels_are_cleared_before_scoring_then_final_label_added(self):
        fake = self.fake(pr_labels={"risk: low", "risk: high", "bug"})
        code, result, output, _, scorer = self.run_enabled(fake, sdk_classification("medium"))
        self.assertEqual(code, 0)
        self.assertEqual(fake.events, [("removed", "risk: low"), ("removed", "risk: high"), ("score",),
                                       ("added", "risk: medium")])
        self.assertEqual(fake.pr_labels, {"bug", "risk: medium"})
        self.assertIs(result["label_published"], True)
        self.assertEqual(result["status"], "ok")
        self.assertIn("risk_label=medium\n", output)
        self.assertTrue(output.endswith("\nlabel_published=true\n"))
        allowed = {ISSUE_LABELS, REPO_LABELS} | {f"{ISSUE_LABELS}/{value}" for value in ENCODED.values()} | {
            f"{REPO_LABELS}/{value}" for value in ENCODED.values()}
        for _, url, _ in fake.calls:
            self.assertIn(url, allowed)

    def test_missing_repository_label_is_created_and_concurrent_creation_tolerated(self):
        for concurrent in (False, True):
            with self.subTest(concurrent=concurrent):
                fake = self.fake(repo_labels=())
                if concurrent:
                    fake.fail = lambda method, url, body: 422 if (method, url) == ("POST", REPO_LABELS) else None
                code, result, _, _, _ = self.run_enabled(fake)
                self.assertEqual((code, result["status"], result["label_published"]), (0, "ok", True))
                self.assertIn(("POST", REPO_LABELS, {"name": "risk: low", "color": "0E8A16",
                                                     "description": "PR risk score: low"}), fake.calls)
                self.assertEqual(fake.pr_labels, {"risk: low"})

    def test_failed_clearing_stops_before_scoring(self):
        fake = self.fake(pr_labels={"risk: low"},
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
        fake = self.fake(pr_labels={"risk: low"})
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
                fake = self.fake()
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
        fake = self.fake()
        code, result, output, _, _ = self.run_enabled(
            fake, responses=[pull_request(), pull_request(), pull_request(), pull_request(head="d" * 40)])
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "pr_changed")
        self.assertIs(result["label_published"], False)
        self.assertEqual(fake.events[-2:], [("added", "risk: low"), ("removed", "risk: low")])
        self.assertEqual(fake.pr_labels, set())
        self.assertIn("risk_label=\n", output)

    def test_forbidden_add_reports_label_error_after_attempting_removal(self):
        fake = self.fake(fail=lambda method, url, body: 403 if (method, url) == ("POST", ISSUE_LABELS) else None)
        code, result, output, _, _ = self.run_enabled(fake)
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "label_error")
        self.assertIsNone(result["risk_label"])
        self.assertIs(result["label_published"], False)
        self.assertEqual(fake.calls[-1], ("DELETE", f"{ISSUE_LABELS}/{ENCODED['low']}", None))
        self.assertIn("risk_label=\nstatus=label_error\n", output)
        self.assertNotIn("risk_label=low", output)

    def test_failed_publish_reread_reports_label_error(self):
        fake = self.fake()
        code, result, _, _, _ = self.run_enabled(
            fake, responses=[pull_request(), pull_request(), GitHubError("PRIVATE_MARKER github-test-key")])
        self.assertEqual((code, result["status"], result["label_published"]), (1, "label_error", False))
        self.assertNotIn("PRIVATE_MARKER", json.dumps(result))

    def test_result_without_fact_sheet_publishes_no_label(self):
        # Default mode has no rules R1-R7, so its low must not look like a gated one.
        without_model_label = sdk_classification()
        del without_model_label["model_label"]
        without_sdk = sdk_classification()
        del without_sdk["sdk"]
        for name, scored in (("default_mode", classification()), ("no_model_label", without_model_label),
                             ("no_sdk", without_sdk)):
            with self.subTest(name=name):
                fake = self.fake(pr_labels={"risk: low", "bug"})
                for path in ("summary", "output"):
                    (self.directory / path).write_text("")
                code, result, output, _, _ = self.run_enabled(fake, scored)
                self.assertEqual((code, result["status"], result["risk_label"]), (0, "ok", "low"))
                self.assertIs(result["label_published"], False)
                self.assertEqual(result["label_skipped"], "no_fact_sheet")
                self.assertEqual(fake.pr_labels, {"bug"})
                self.assertNotIn("POST", [method for method, _, _ in fake.calls])
                self.assertIn("risk_label=low\nstatus=ok\n", output)
                self.assertTrue(output.endswith("\nlabel_published=false\n"))
                self.assertEqual((self.directory / "summary").read_text().count(
                    "No risk label is published for repositories without an SDK fact sheet."), 1)

    def test_scorer_label_skipped_field_is_not_trusted(self):
        scored = sdk_classification()
        scored["label_skipped"] = "no_fact_sheet"
        fake = self.fake()
        code, result, _, _, _ = self.run_enabled(fake, scored)
        self.assertEqual((code, result["label_published"]), (0, True))
        self.assertNotIn("label_skipped", result)
        self.assertNotIn("without an SDK fact sheet", (self.directory / "summary").read_text())

    def test_published_label_is_the_final_label_not_the_model_label(self):
        scored = classification()
        scored.update(sdk_result_fields(), risk_label="medium")
        fake = self.fake()
        code, result, _, _, _ = self.run_enabled(fake, scored)
        self.assertEqual((code, result["model_label"], result["risk_label"]), (0, "low", "medium"))
        self.assertEqual(fake.pr_labels, {"risk: medium"})

    def test_main_turns_sigterm_into_system_exit(self):
        previous = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        with patch.object(action_runner, "run_action", return_value=0):
            action_runner.main()
        handler = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(SystemExit) as caught:
            handler(signal.SIGTERM, None)
        self.assertEqual(caught.exception.code, 128 + signal.SIGTERM)

    def test_sigterm_handler_is_not_installed_at_import(self):
        check = ("import signal; before = signal.getsignal(signal.SIGTERM); "
                 "import risk_pr_agent.action_runner; print(signal.getsignal(signal.SIGTERM) is before)")
        environment = {**os.environ, "PYTHONPATH": str(action_runner.SOURCE_ROOT / "src")}
        completed = subprocess.run([sys.executable, "-c", check], env=environment, check=True,
                                   capture_output=True, text=True)
        self.assertEqual(completed.stdout.strip(), "True")

    def test_publish_label_input_must_be_exactly_true(self):
        for value, enabled in (("true", True), ("false", False), ("", False), ("True", False), ("yes", False)):
            with self.subTest(value=value), \
                    patch.dict(os.environ, {"INPUT_PUBLISH_LABEL": value, "INPUT_REPO": "example/project",
                                            "INPUT_PR_NUMBER": "7"}), \
                    patch.object(action_runner, "run_action", return_value=0) as run:
                action_runner.main()
            self.assertEqual(run.call_args.kwargs["publish_label"], enabled)

    def test_scorer_environment_does_not_depend_on_publish_label(self):
        seen = {}

        for enabled in (False, True):
            def run(args, **kwargs):
                seen[enabled] = kwargs["env"]
                Path(args[args.index("--out") + 1]).write_text(json.dumps(classification()))

            with patch.dict(os.environ, {"GH_TOKEN": "gh-test-key"}), \
                    patch.object(labels, "default_transport", return_value=self.fake()), \
                    patch.object(action_runner.GitHubClient, "get_pull_request", return_value=pull_request()), \
                    patch.object(action_runner, "_fetch_git"), \
                    patch.object(action_runner.subprocess, "run", side_effect=run):
                code, _, _ = self.invoke(publish_label=enabled)
            self.assertEqual(code, 0)
        self.assertEqual(seen[False], seen[True])
        self.assertEqual(seen[True]["GITHUB_TOKEN"], "github-test-key")
        self.assertEqual(seen[True]["GH_TOKEN"], "gh-test-key")

    def test_failed_clean_up_after_move_is_retried_then_reported(self):
        for failures, status in ((2, "pr_changed"), (3, "label_error")):
            with self.subTest(failures=failures):
                remaining = [failures]

                def fail(method, url, body):
                    if method == "DELETE" and ("added", "risk: low") in fake.events and remaining[0]:
                        remaining[0] -= 1
                        return 403
                    return None

                fake = self.fake(fail=fail)
                for path in ("summary", "output"):
                    (self.directory / path).write_text("")
                code, result, output, _, _ = self.run_enabled(
                    fake, responses=[pull_request(), pull_request(), pull_request(), pull_request(head="d" * 40)])
                summary = (self.directory / "summary").read_text()
                self.assertEqual((code, result["status"], result["label_published"]), (1, status, False))
                self.assertIsNone(result["risk_label"])
                if failures == 3:
                    self.assertIs(result["label_cleanup_failed"], True)
                    self.assertEqual(fake.pr_labels, {"risk: low"})
                    self.assertIn("A risk label may still be on the PR; the next run will clear it.", summary)
                else:
                    self.assertNotIn("label_cleanup_failed", result)
                    self.assertEqual(fake.pr_labels, set())
                    self.assertNotIn("may still be on the PR", summary)


if __name__ == "__main__":
    unittest.main()
