import ast
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from risk_pr_agent import labels
from test_labels import CheckedFakes

try:
    from risk_pr_agent import clear_labels
except ImportError:  # the module does not exist before the fix; the tests below then fail
    clear_labels = None


ACTION_ROOT = Path(__file__).resolve().parents[1]
SOURCE = ACTION_ROOT / "src"
# Everything the first step imports, which must run on GitHub's system Python 3.10.
FIRST_STEP_MODULES = ("__init__.py", "clear_labels.py", "labels.py", "github.py")


def action_steps():
    """The composite steps of action.yml as (header line, block text), in order."""
    text = (ACTION_ROOT / "action.yml").read_text(encoding="utf-8")
    steps = text.split("\n  steps:\n", 1)[1]
    return [block for block in re.split(r"\n(?=    - )", steps) if block.startswith("    - ")]


def step_env(block):
    env = block.split("      env:\n", 1)[1]
    lines = []
    for line in env.splitlines():
        if not line.startswith("        "):
            break
        lines.append(line.strip())
    return dict(line.split(": ", 1) for line in lines)


class ClearLabelsMainTests(CheckedFakes, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.summary = Path(temporary.name) / "summary"
        self.summary.write_text("")
        summary = patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(self.summary)})
        summary.start()
        self.addCleanup(summary.stop)

    def run_main(self, environment, fake=None):
        stream = io.StringIO()
        with patch.dict(os.environ, environment), \
                patch.object(labels, "default_transport", return_value=fake) as transport, \
                patch.object(labels.time, "sleep"), \
                patch("sys.stdout", stream), patch("sys.stderr", stream):
            code = clear_labels.main()
        return code, transport, stream.getvalue()

    def test_clears_all_three_risk_labels(self):
        fake = self.fake(pr_labels={"risk: low", "risk: medium", "bug"})
        code, _, _ = self.run_main({"INPUT_REPO": "example/project", "INPUT_PR_NUMBER": "7",
                                    "GITHUB_SERVER_URL": "https://github.com", "GITHUB_TOKEN": "github-test-key"},
                                   fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.pr_labels, {"bug"})

    def test_failed_clear_exits_non_zero(self):
        fake = self.fake(pr_labels={"risk: low"}, fail=lambda method, url, body: 403)
        code, _, output = self.run_main({"INPUT_REPO": "example/project", "INPUT_PR_NUMBER": "7",
                                         "GITHUB_SERVER_URL": "https://github.com"}, fake)
        self.assertNotEqual(code, 0)
        self.assertEqual(fake.pr_labels, {"risk: low"})
        self.assertIn("label_error", output)

    def test_invalid_target_or_server_is_refused_without_a_request(self):
        cases = [("example/project;echo injected", "7", "https://github.com", "invalid_repository"),
                 ("example/..", "7", "https://github.com", "invalid_repository"),
                 ("example/project", "07", "https://github.com", "invalid_pr_number"),
                 ("example/project", "7\nstatus=ok", "https://github.com", "invalid_pr_number"),
                 ("example/project", "7", "https://ghe.example.invalid", "unsupported_repository")]
        for repo, number, server, status in cases:
            with self.subTest(status=status, repo=repo, number=number):
                code, transport, output = self.run_main(
                    {"INPUT_REPO": repo, "INPUT_PR_NUMBER": number, "GITHUB_SERVER_URL": server})
                self.assertNotEqual(code, 0)
                self.assertIn(status, output)
                transport.assert_not_called()

    def test_failure_leaves_one_summary_line_naming_the_status(self):
        cases = [({"INPUT_REPO": "example/..", "INPUT_PR_NUMBER": "7"}, None, "invalid_repository"),
                 ({"INPUT_REPO": "example/project", "INPUT_PR_NUMBER": "07"}, None, "invalid_pr_number"),
                 ({"INPUT_REPO": "example/project", "INPUT_PR_NUMBER": "7",
                   "GITHUB_SERVER_URL": "https://ghe.example.invalid"}, None, "unsupported_repository"),
                 ({"INPUT_REPO": "example/project", "INPUT_PR_NUMBER": "7"},
                  lambda: self.fake(pr_labels={"risk: low"}, fail=lambda method, url, body: 403), "label_error")]
        for environment, fake, status in cases:
            with self.subTest(status=status):
                self.summary.write_text("")
                code, _, _ = self.run_main({"GITHUB_SERVER_URL": "https://github.com", **environment},
                                           fake() if fake else None)
                self.assertEqual(code, 1)
                lines = self.summary.read_text().splitlines()
                self.assertEqual(len(lines), 1)
                self.assertIn(f"`{status}`", lines[0])
                self.assertNotIn("example", lines[0])

    def test_success_writes_no_summary(self):
        fake = self.fake(pr_labels={"risk: low"})
        code, _, _ = self.run_main({"INPUT_REPO": "example/project", "INPUT_PR_NUMBER": "7",
                                    "GITHUB_SERVER_URL": "https://github.com"}, fake)
        self.assertEqual(code, 0)
        self.assertEqual(self.summary.read_text(), "")

    def test_validation_is_the_one_run_action_uses(self):
        from risk_pr_agent import action_runner

        self.assertIs(action_runner.parse_target, clear_labels.parse_target)


class FirstStepTests(unittest.TestCase):
    def test_first_step_clears_before_any_set_up_and_only_when_publishing(self):
        first = action_steps()[0]
        self.assertIn("name: Clear previous risk label", first)
        self.assertIn("if: inputs.publish-label == 'true'", first)
        self.assertNotIn("uses:", first)
        run = first.split("      run: ", 1)[1]
        self.assertNotIn("${{", run)
        self.assertTrue(run.startswith("python3 -I "), run)
        env = step_env(first)
        self.assertEqual(env["GITHUB_TOKEN"], "${{ github.token }}")
        self.assertEqual(env["AI_GATEWAY_API_KEY"], '""')
        self.assertEqual(env["INPUT_REPO"], "${{ inputs.repo }}")
        self.assertEqual(env["INPUT_PR_NUMBER"], "${{ inputs.pr-number }}")

    def test_first_step_runs_on_old_system_python_without_installed_packages(self):
        first = action_steps()[0]
        run = first.split("      run: ", 1)[1].strip()
        python = shutil.which("python3", path="/usr/bin:/bin") or sys.executable
        command = run.replace("python3 ", f"{python} ", 1)
        environment = {"PATH": os.environ.get("PATH", ""), "GITHUB_ACTION_PATH": str(ACTION_ROOT),
                       "INPUT_REPO": "example/project", "INPUT_PR_NUMBER": "7",
                       "GITHUB_SERVER_URL": "https://github.com", "GITHUB_TOKEN": ""}
        # No token: the step must fail (non-zero) after importing, before any request.
        completed = subprocess.run(["bash", "-c", command], env=environment, cwd=str(ACTION_ROOT),
                                   capture_output=True, text=True, timeout=60)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("label_error", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_first_step_imports_only_the_standard_library_and_its_own_modules(self):
        python = shutil.which("python3", path="/usr/bin:/bin") or sys.executable
        check = ("import sys; sys.path.insert(0, sys.argv[1]); import risk_pr_agent.clear_labels; "
                 "print(sorted(name for name in sys.modules if name.startswith('risk_pr_agent'))); "
                 "print('tiktoken' in sys.modules)")
        completed = subprocess.run([python, "-I", "-c", check, str(SOURCE)], capture_output=True,
                                   text=True, check=True, timeout=60)
        loaded, tiktoken = completed.stdout.splitlines()
        self.assertEqual(loaded, str(sorted(["risk_pr_agent", "risk_pr_agent.clear_labels",
                                             "risk_pr_agent.labels", "risk_pr_agent.github"])))
        self.assertEqual(tiktoken, "False")

    def test_first_step_modules_parse_as_python_3_9(self):
        for name in FIRST_STEP_MODULES:
            with self.subTest(module=name):
                source = (SOURCE / "risk_pr_agent" / name).read_text(encoding="utf-8")
                ast.parse(source, feature_version=(3, 9))
                self.assertNotIn("tomllib", source)


class ActionPinTests(unittest.TestCase):
    def test_nested_actions_are_pinned_to_full_commit_shas(self):
        uses = re.findall(r"uses: (\S+)(.*)", (ACTION_ROOT / "action.yml").read_text(encoding="utf-8"))
        self.assertEqual(uses, [
            ("actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065", " # v5"),
            ("actions/setup-node@249970729cb0ef3589644e2896645e5dc5ba9c38", " # v6"),
        ])


if __name__ == "__main__":
    unittest.main()
