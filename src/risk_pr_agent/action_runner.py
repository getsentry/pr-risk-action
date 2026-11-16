"""Run the trusted Jev scorer without checking out or executing PR contents."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
from typing import Any, Dict

from .commands import risk_summary
from .github import GitHubClient, GitHubError, RepoRef
from .labels import RISK_LABELS, LabelError, RiskLabels


SOURCE_ROOT = Path(__file__).resolve().parents[2]
# SDK-mode fields that describe the model's judgement; only an ``ok`` result carries them.
_VERDICT_FIELDS = ("model_label", "low_blocked_by", "mechanisms")


class ActionError(RuntimeError):
    """A bounded, safe-to-publish failure status."""


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise ActionError("invalid_revision")
    return value.lower()


def _revision(pr: Dict[str, Any], number: int) -> tuple:
    if pr.get("number") != number:
        raise ActionError("pr_identity_mismatch")
    if pr.get("state") != "open" or pr.get("merged_at"):
        raise ActionError("pr_not_open")
    if (pr.get("base", {}).get("repo") or {}).get("private") is not False:
        raise ActionError("unsupported_repository")
    return _sha(pr.get("base", {}).get("sha")), _sha(pr.get("head", {}).get("sha"))


def _git_environment() -> Dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    return environment


def _fetch_git(repo: RepoRef, base: str, head: str, directory: Path) -> None:
    # Public HTTPS fetches need no credentials. Config isolation also prevents
    # a caller checkout's credential helpers, filters or URL rewrites applying.
    environment = _git_environment()
    for key in ("AI_GATEWAY_API_KEY", "GITHUB_TOKEN", "GH_TOKEN"):
        environment.pop(key, None)
    options = {"env": environment, "check": True, "capture_output": True, "timeout": 300}
    try:
        subprocess.run(["git", "init", "--bare", str(directory)], **options)
        subprocess.run(["git", "-C", str(directory), "-c", "protocol.file.allow=never",
                        "-c", "protocol.ext.allow=never", "fetch", "--no-tags", "--no-recurse-submodules",
                        f"https://github.com/{repo.slug}.git", base, head], **options)
    except (subprocess.SubprocessError, OSError):
        raise ActionError("git_fetch_failed") from None


def _run_scorer(repo: RepoRef, number: int, directory: Path) -> Dict[str, Any]:
    output = directory / "scored.json"
    environment = _git_environment()
    environment["PYTHONPATH"] = str(SOURCE_ROOT / "src")
    # Publish only after checking that the model and GitHub still refer to the
    # revision we fetched. Neither the PR checkout nor its .env is ever loaded.
    environment.pop("GITHUB_OUTPUT", None)
    environment.pop("GITHUB_STEP_SUMMARY", None)
    try:
        # Four 65-second worker deadlines plus 60 seconds of backoff need room
        # for metadata collection, snapshot reconstruction and result writes.
        subprocess.run([
            sys.executable, "-m", "risk_pr_agent.cli", "score-pr",
            "--repo", repo.slug, "--pr", str(number), "--git-repo", str(directory / "target.git"),
            "--candidate", "--out", str(output), "--cache-dir", str(directory / "cache"),
        ], cwd=directory, env=environment, check=True, capture_output=True, timeout=420)
    except (subprocess.SubprocessError, OSError):
        raise ActionError("scorer_failed") from None
    return json.loads(output.read_text(encoding="utf-8"))


def _publish(result: Dict[str, Any], result_path: str, label_published: bool = False,
             label_cleanup_failed: bool = False) -> None:
    output = Path(result_path)
    if not output.is_absolute():
        output = Path(os.environ.get("GITHUB_WORKSPACE", os.getcwd())) / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = risk_summary(result) + "\n"
    if label_cleanup_failed:
        summary += "A risk label may still be on the PR; the next run will clear it.\n\n"
    summary += "Jev candidate result; this check does not authorize merging.\n"
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(summary)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            handle.write(f"risk_label={result.get('risk_label') or ''}\nstatus={result['status']}\n")
            delimiter = secrets.token_hex(16)
            while delimiter in summary:
                delimiter = secrets.token_hex(16)
            handle.write(f"summary<<{delimiter}\n{summary.rstrip(chr(10))}\n{delimiter}\n")
            handle.write(f"label_published={'true' if label_published else 'false'}\n")
    print(summary)


def _clear_risk_labels(repo: RepoRef, number: int) -> RiskLabels:
    try:
        risk_labels = RiskLabels(repo, number)
        risk_labels.clear()
    except Exception:
        # The old label may still be on the PR, so this run must not score it.
        raise ActionError("label_error") from None
    return risk_labels


def _publish_risk_label(risk_labels: RiskLabels, result: Dict[str, Any], client: GitHubClient,
                        repo: RepoRef, number: int, revision: tuple) -> None:
    def unchanged() -> bool:
        try:
            return _revision(client.get_pull_request(repo, number), number) == revision
        except ActionError:
            return False

    try:
        published = risk_labels.publish(result["risk_label"], unchanged)
    except LabelError as error:
        published, status = False, "label_error"
        if error.cleanup_failed:
            result["label_cleanup_failed"] = True
    except Exception:
        # Last resort: publish() wraps its own failures, but the result must always
        # be written, and nothing vouches that the label is gone.
        published, status = False, "label_error"
        result["label_cleanup_failed"] = True
    else:
        status = "ok" if published else "pr_changed"
    result["label_published"] = published
    if status != "ok":
        result.update(status=status, risk_label=None, probabilities=None)


def run_action(repo_value: str, number_value: str, result_path: str,
               expected_head: str = "", publish_label: bool = False) -> int:
    result: Dict[str, Any] = {"schema_version": 2, "candidate": True,
                              "input_profile": "metadata-diff", "risk_label": None}
    risk_labels = client = revision = None
    try:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo_value):
            raise ActionError("invalid_repository")
        if not re.fullmatch(r"[1-9][0-9]*", number_value):
            raise ActionError("invalid_pr_number")
        repo, number = RepoRef.parse(repo_value), int(number_value)
        if repo.name in (".", ".."):
            raise ActionError("invalid_repository")
        result.update(repo=repo.slug, number=number, example_id=f"{repo.slug}#{number}")
        if publish_label:
            # Writes go to api.github.com only, so check the server before clearing.
            # Clear before every later exit so no run leaves an earlier revision's label.
            if os.environ.get("GITHUB_SERVER_URL", "https://github.com") != "https://github.com":
                raise ActionError("unsupported_repository")
            risk_labels = _clear_risk_labels(repo, number)
        # SDK fact sheets and the rubric ship with the action. Without them every SDK
        # repository would silently fall back to default mode, so refuse to run.
        if not (SOURCE_ROOT / "sdk" / "defaults.toml").is_file():
            raise ActionError("missing_sdk_data")
        expected_head = _sha(expected_head) if expected_head else ""
        if not os.environ.get("AI_GATEWAY_API_KEY"):
            raise ActionError("missing_credentials")
        if os.environ.get("GITHUB_SERVER_URL", "https://github.com") != "https://github.com":
            raise ActionError("unsupported_repository")
        client = GitHubClient()
        base, head = _revision(client.get_pull_request(repo, number), number)
        if expected_head and head != expected_head:
            raise ActionError("pr_changed")
        with tempfile.TemporaryDirectory(prefix="jev-pr-") as temporary:
            directory = Path(temporary)
            _fetch_git(repo, base, head, directory / "target.git")
            result = _run_scorer(repo, number, directory)
            snapshot = result.get("snapshot") or {}
            if (snapshot.get("source_base_sha"), snapshot.get("source_head_sha")) != (base, head):
                raise ActionError("pr_changed")
            if _revision(client.get_pull_request(repo, number), number) != (base, head):
                raise ActionError("pr_changed")
            revision = (base, head)
        if not isinstance(result.get("status"), str) or not re.fullmatch(r"[a-z_]+", result["status"]):
            raise ActionError("invalid_result")
        if result.get("risk_label") not in (None, "low", "medium", "high") or (
                result.get("status") == "ok" and result.get("risk_label") is None):
            raise ActionError("invalid_result")
    except ActionError as error:
        result.update(status=str(error), risk_label=None, probabilities=None)
    except GitHubError:
        result.update(status="github_error", risk_label=None, probabilities=None)
    except (subprocess.SubprocessError, OSError, ValueError, TypeError, KeyError):
        # Child output and HTTP bodies can include arbitrary PR text. Keep them
        # out of logs and artifacts; the status makes the failure visible.
        result.update(status="action_error", risk_label=None, probabilities=None)
    if publish_label:
        result["label_published"] = False
        result.pop("label_cleanup_failed", None)
        if risk_labels and revision and result.get("status") == "ok" and result.get("risk_label") in RISK_LABELS:
            _publish_risk_label(risk_labels, result, client, repo, number, revision)
    if result.get("status") != "ok":
        # The action does not vouch for this result, so it publishes no model verdict.
        for key in _VERDICT_FIELDS:
            result.pop(key, None)
    _publish(result, result_path, label_published=publish_label and result["label_published"] is True,
             label_cleanup_failed=publish_label and result.get("label_cleanup_failed") is True)
    return 0 if result.get("status") == "ok" else 1


def _exit_on_sigterm(signum: int, frame: Any) -> None:
    # Cancellation may arrive as SIGTERM; raising lets label clean-up run as for SIGINT.
    raise SystemExit(128 + signum)


def main() -> int:
    signal.signal(signal.SIGTERM, _exit_on_sigterm)
    return run_action(os.environ.get("INPUT_REPO", ""), os.environ.get("INPUT_PR_NUMBER", ""),
                      os.environ.get("INPUT_RESULT_PATH", "risk-pr-result.json"),
                      os.environ.get("INPUT_EXPECTED_HEAD_SHA", ""),
                      publish_label=os.environ.get("INPUT_PUBLISH_LABEL", "false") == "true")


if __name__ == "__main__":
    raise SystemExit(main())
