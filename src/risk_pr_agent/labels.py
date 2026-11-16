"""Publish the risk label on a pull request; the only code that writes to GitHub.

Write requests go only to the label endpoints of the repository and PR number the
action was given. Label names come only from ``RISK_LABELS``. Response bodies and
the token are never logged or put into errors.
"""

from __future__ import annotations

import json
import os
import http.client
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional

from .github import _OPENER, GITHUB_API, RepoRef


# Risk class -> (label name, colour). The description is "PR risk score: <class>".
RISK_LABELS = {"low": ("risk: low", "0E8A16"), "medium": ("risk: medium", "FBCA04"),
               "high": ("risk: high", "B60205")}
_ATTEMPTS = 3
_CLEANUP_ATTEMPTS = 3
_TIMEOUT = 30.0
# After SIGINT the runner sends SIGTERM about 7.5 s later, then SIGKILL.
_CANCEL_TIMEOUT = 5.0

# (method, url, json body or None, timeout=seconds) -> HTTP status.
# Network failures report a 5xx status.
Transport = Callable[..., int]


class LabelError(RuntimeError):
    """A label request failed; the message never contains response bodies.

    ``cleanup_failed`` is True when a label this run added may still be on the PR.
    """

    def __init__(self, message: str, cleanup_failed: bool = False) -> None:
        super().__init__(message)
        self.cleanup_failed = cleanup_failed


def default_transport() -> Transport:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise LabelError("no GitHub token for label requests")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "risk-pr-agent/0.1",
               "X-GitHub-Api-Version": "2022-11-28", "Authorization": f"Bearer {token}"}

    def send(method: str, url: str, body: Optional[Dict[str, Any]], timeout: float = _TIMEOUT) -> int:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method,
                                         headers={**headers, **({"Content-Type": "application/json"} if data else {})})
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                return response.status
        except urllib.error.HTTPError as error:
            error.close()
            return error.code
        except (OSError, http.client.HTTPException):
            # URLError, timeouts, connection and TLS errors (ssl.SSLError is raised
            # outside urllib's own wrapper). Treated like a server error.
            return 599

    return send


class RiskLabels:
    """The three risk labels on one pull request."""

    def __init__(self, repo: RepoRef, number: int, transport: Optional[Transport] = None,
                 sleep: Optional[Callable[[float], None]] = None) -> None:
        owner, name = (urllib.parse.quote(part, safe="") for part in (repo.owner, repo.name))
        self._repo_labels = f"{GITHUB_API}/repos/{owner}/{name}/labels"
        self._issue_labels = f"{GITHUB_API}/repos/{owner}/{name}/issues/{int(number)}/labels"
        self._transport = transport
        self._sleep = sleep or time.sleep

    def clear(self) -> None:
        """Remove every risk label from the PR; raise LabelError if any removal fails."""
        for level in RISK_LABELS:
            self._remove(level)

    def publish(self, level: str, is_current: Callable[[], bool]) -> bool:
        """Add the label for ``level`` if the PR still is the scored revision.

        The other two risk labels are removed immediately before the add.

        Returns True when the label is on the PR and the PR was unchanged after it
        was added, False when the PR changed (nothing left on the PR). Raises
        LabelError on any failure, after trying to remove a label it may have added;
        if that removal fails too, the error has ``cleanup_failed`` set.
        """
        if level not in RISK_LABELS:
            raise LabelError("unknown risk level")
        may_be_added = False
        try:
            if not is_current():
                return False
            self._ensure_exists(level)
            # An earlier run killed after adding its label may have left another one.
            for other in RISK_LABELS:
                if other != level:
                    self._remove(other)
            may_be_added = True
            self._send("POST", self._issue_labels, {"labels": [RISK_LABELS[level][0]]}, (200,))
            unchanged = is_current()
        except Exception:
            if may_be_added and not self._clean_up(level):
                raise LabelError("label publishing failed", cleanup_failed=True) from None
            raise LabelError("label publishing failed") from None
        except BaseException:
            # Cancellation (SIGINT, or SIGTERM as SystemExit): one short DELETE with no
            # retry, so it fits the runner's grace period; the next run clears the rest.
            if may_be_added:
                try:
                    self._remove(level, timeout=_CANCEL_TIMEOUT, attempts=1)
                except Exception:
                    pass
            raise
        if unchanged:
            return True
        if not self._clean_up(level):
            raise LabelError("pull request changed and label removal failed", cleanup_failed=True)
        return False

    def _clean_up(self, level: str) -> bool:
        """Try to remove the label up to three times; never raises an Exception."""
        for _ in range(_CLEANUP_ATTEMPTS):
            try:
                self._remove(level)
                return True
            except Exception:
                pass
        return False

    def _ensure_exists(self, level: str) -> None:
        name, colour = RISK_LABELS[level]
        if self._send("GET", f"{self._repo_labels}/{_encoded(name)}", None, (200, 404)) == 200:
            return
        # 422: another run created the label in the meantime.
        self._send("POST", self._repo_labels,
                   {"name": name, "color": colour, "description": f"PR risk score: {level}"}, (201, 422))

    def _remove(self, level: str, timeout: float = _TIMEOUT, attempts: int = _ATTEMPTS) -> None:
        # 404: the label is not on the PR, which is the state we want.
        self._send("DELETE", f"{self._issue_labels}/{_encoded(RISK_LABELS[level][0])}", None,
                   (200, 204, 404), timeout, attempts)

    def _send(self, method: str, url: str, body: Optional[Dict[str, Any]], accepted: tuple,
              timeout: float = _TIMEOUT, attempts: int = _ATTEMPTS) -> int:
        if self._transport is None:
            self._transport = default_transport()
        for attempt in range(attempts):
            try:
                status = self._transport(method, url, body, timeout=timeout)
            except Exception:
                status = 599  # never let a transport failure skip the caller's clean-up
            if status in accepted:
                return status
            if status < 500 or attempt == attempts - 1:
                break
            self._sleep(2 ** attempt)
        raise LabelError(f"GitHub label request {method} returned {status}")


def _encoded(name: str) -> str:
    return urllib.parse.quote(name, safe="")
