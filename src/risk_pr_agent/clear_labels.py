"""Remove the PR's risk labels; the action's first step, before any set-up.

It runs on the runner's system Python (3.9 or later) with ``-I``, so this
module and everything it imports use only the standard library and
syntax that Python 3.9 accepts. Exit status 0 means no risk label is on the PR.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Tuple

from .github import RepoRef
from .labels import RiskLabels


class TargetError(ValueError):
    """The repository, PR number or server is not one the action may act on."""


def parse_target(repo_value: str, number_value: str) -> Tuple[RepoRef, int]:
    """Validate the ``repo`` and ``pr-number`` inputs; the message is a status name."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo_value):
        raise TargetError("invalid_repository")
    if not re.fullmatch(r"[1-9][0-9]*", number_value):
        raise TargetError("invalid_pr_number")
    repo, number = RepoRef.parse(repo_value), int(number_value)
    if repo.name in (".", ".."):
        raise TargetError("invalid_repository")
    return repo, number


def check_server() -> None:
    """Writes go to api.github.com only, so refuse any other GitHub server."""
    if os.environ.get("GITHUB_SERVER_URL", "https://github.com") != "https://github.com":
        raise TargetError("unsupported_repository")


def _fail(status: str) -> int:
    # Fixed text and a status name only; nothing derived from the pull request.
    print(f"Risk labels not cleared: {status}", file=sys.stderr)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(f"Risk labels were not cleared (`{status}`); the job stopped before scoring.\n")
        except OSError:
            pass
    return 1


def main() -> int:
    try:
        repo, number = parse_target(os.environ.get("INPUT_REPO", ""), os.environ.get("INPUT_PR_NUMBER", ""))
        check_server()
        RiskLabels(repo, number).clear()
    except TargetError as error:
        return _fail(str(error))
    except Exception:
        # Errors never carry response bodies or the token; the status is enough.
        return _fail("label_error")
    print("Risk labels cleared.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
