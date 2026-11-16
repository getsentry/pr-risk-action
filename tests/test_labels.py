import io
import os
import ssl
import unittest
import urllib.error
from unittest.mock import patch

from risk_pr_agent import labels
from risk_pr_agent.github import GitHubError, RepoRef


REPO = RepoRef.parse("example/project")
ISSUE_LABELS = "https://api.github.com/repos/example/project/issues/7/labels"
REPO_LABELS = "https://api.github.com/repos/example/project/labels"
ENCODED = {"low": "risk%3A%20low", "medium": "risk%3A%20medium", "high": "risk%3A%20high"}
ALL_LABELS = ("risk: low", "risk: medium", "risk: high")


class FakeGitHub:
    """Label endpoints of one repository and PR, with optional injected failures."""

    def __init__(self, pr_labels=(), repo_labels=ALL_LABELS, fail=None, events=None):
        self.pr_labels = set(pr_labels)
        self.repo_labels = set(repo_labels)
        self.fail = fail or (lambda method, url, body: None)
        self.calls = []
        self.events = events if events is not None else []

    def __call__(self, method, url, body=None):
        self.calls.append((method, url, body))
        status = self.fail(method, url, body)
        if status is not None:
            self.events.append(("failed", method, url))
            return status
        if method == "DELETE" and url.startswith(ISSUE_LABELS + "/"):
            name = labels.urllib.parse.unquote(url[len(ISSUE_LABELS) + 1:])
            if name not in self.pr_labels:
                return 404
            self.pr_labels.discard(name)
            self.events.append(("removed", name))
            return 200
        if method == "GET" and url.startswith(REPO_LABELS + "/"):
            return 200 if labels.urllib.parse.unquote(url[len(REPO_LABELS) + 1:]) in self.repo_labels else 404
        if method == "POST" and url == REPO_LABELS:
            if body["name"] in self.repo_labels:
                return 422
            self.repo_labels.add(body["name"])
            self.events.append(("created", body["name"]))
            return 201
        if method == "POST" and url == ISSUE_LABELS:
            self.pr_labels.update(body["labels"])
            self.events.extend(("added", name) for name in body["labels"])
            return 200
        raise AssertionError(f"unexpected request {method} {url}")


def _raise(error):
    raise error


def current(*answers):
    answers = list(answers)
    return lambda: answers.pop(0)


class RiskLabelsTests(unittest.TestCase):
    def labels(self, fake):
        return labels.RiskLabels(REPO, 7, transport=fake, sleep=lambda seconds: None)

    def test_constants_are_exact(self):
        self.assertEqual(labels.RISK_LABELS, {
            "low": ("risk: low", "0E8A16"), "medium": ("risk: medium", "FBCA04"),
            "high": ("risk: high", "B60205")})

    def test_clear_removes_every_risk_label_and_treats_absent_as_removed(self):
        fake = FakeGitHub(pr_labels={"risk: low", "risk: high", "bug"})
        self.labels(fake).clear()
        self.assertEqual(fake.pr_labels, {"bug"})
        self.assertEqual([(method, url) for method, url, _ in fake.calls],
                         [("DELETE", f"{ISSUE_LABELS}/{ENCODED[level]}") for level in ("low", "medium", "high")])

    def test_clear_fails_on_any_other_status(self):
        for status in (401, 403, 500):
            with self.subTest(status=status):
                fake = FakeGitHub(fail=lambda method, url, body: status if url.endswith(ENCODED["medium"]) else None)
                with self.assertRaises(labels.LabelError):
                    self.labels(fake).clear()

    def test_server_errors_are_retried(self):
        failures = [502]
        fake = FakeGitHub(pr_labels={"risk: low"},
                          fail=lambda method, url, body: failures.pop() if failures else None)
        self.labels(fake).clear()
        self.assertEqual(fake.pr_labels, set())

    def test_publish_adds_existing_label_after_checking_revision(self):
        fake = FakeGitHub()
        self.assertTrue(self.labels(fake).publish("medium", current(True, True)))
        self.assertEqual(fake.pr_labels, {"risk: medium"})
        self.assertEqual(fake.calls, [("GET", f"{REPO_LABELS}/{ENCODED['medium']}", None),
                                      ("POST", ISSUE_LABELS, {"labels": ["risk: medium"]})])

    def test_publish_creates_a_missing_label_with_exact_colour_and_description(self):
        for level, (name, colour) in labels.RISK_LABELS.items():
            with self.subTest(level=level):
                fake = FakeGitHub(repo_labels=())
                self.assertTrue(self.labels(fake).publish(level, current(True, True)))
                self.assertIn(("POST", REPO_LABELS, {"name": name, "color": colour,
                                                     "description": f"PR risk score: {level}"}), fake.calls)
                self.assertEqual(fake.pr_labels, {name})

    def test_publish_tolerates_a_label_created_concurrently(self):
        fake = FakeGitHub(repo_labels=())
        fake.fail = lambda method, url, body: 422 if (method, url) == ("POST", REPO_LABELS) else None
        self.assertTrue(self.labels(fake).publish("low", current(True, True)))
        self.assertEqual(fake.pr_labels, {"risk: low"})

    def test_publish_does_nothing_when_pr_moved_before_publishing(self):
        fake = FakeGitHub()
        self.assertFalse(self.labels(fake).publish("low", current(False)))
        self.assertEqual(fake.calls, [])

    def test_publish_removes_its_label_when_pr_moved_after_adding(self):
        fake = FakeGitHub()
        self.assertFalse(self.labels(fake).publish("low", current(True, False)))
        self.assertEqual(fake.pr_labels, set())
        self.assertEqual(fake.calls[-1], ("DELETE", f"{ISSUE_LABELS}/{ENCODED['low']}", None))

    def test_clean_up_after_move_is_tried_three_times(self):
        for failures, cleaned in ((2, True), (3, False)):
            with self.subTest(failures=failures):
                remaining = [failures]

                def fail(method, url, body):
                    if method == "DELETE" and remaining[0]:
                        remaining[0] -= 1
                        return 403
                    return None

                fake = FakeGitHub(fail=fail)
                if cleaned:
                    self.assertFalse(self.labels(fake).publish("low", current(True, False)))
                    self.assertEqual(fake.pr_labels, set())
                else:
                    with self.assertRaises(labels.LabelError) as caught:
                        self.labels(fake).publish("low", current(True, False))
                    self.assertTrue(caught.exception.cleanup_failed)
                    self.assertEqual(fake.pr_labels, {"risk: low"})
                self.assertEqual([method for method, _, _ in fake.calls].count("DELETE"), min(failures + 1, 3))

    def test_clean_up_after_error_is_tried_three_times(self):
        fake = FakeGitHub(fail=lambda method, url, body: 403 if method == "DELETE" else None)

        def is_current():
            if fake.pr_labels:
                raise GitHubError("final re-read failed")
            return True

        with self.assertRaises(labels.LabelError) as caught:
            self.labels(fake).publish("low", is_current)
        self.assertTrue(caught.exception.cleanup_failed)
        self.assertEqual([method for method, _, _ in fake.calls].count("DELETE"), 3)

    def test_transport_exceptions_become_label_errors(self):
        def raising(method):
            def fail(m, url, body):
                if m == method:
                    raise ssl.SSLError("tls failure")
            return fail

        fake = FakeGitHub(pr_labels={"risk: low"}, fail=raising("DELETE"))
        with self.assertRaises(labels.LabelError):
            self.labels(fake).clear()
        fake = FakeGitHub(fail=raising("POST"))
        with self.assertRaises(labels.LabelError) as caught:
            self.labels(fake).publish("low", current(True, True))
        self.assertFalse(caught.exception.cleanup_failed)
        self.assertEqual(fake.calls[-1][0], "DELETE")
        fake = FakeGitHub()
        fake.fail = lambda m, url, body: (_raise(ssl.SSLError("tls")) if m == "DELETE" else None)
        with self.assertRaises(labels.LabelError) as caught:
            self.labels(fake).publish("low", current(True, False))
        self.assertTrue(caught.exception.cleanup_failed)
        # Three clean-up attempts, each retried like any server error.
        self.assertEqual([m for m, _, _ in fake.calls].count("DELETE"), 3 * 3)
        self.assertEqual(fake.pr_labels, {"risk: low"})

    def test_cancellation_after_add_removes_the_label_and_propagates(self):
        answers = [True]

        def is_current():
            if answers:
                return answers.pop()
            raise KeyboardInterrupt

        fake = FakeGitHub()
        with self.assertRaises(KeyboardInterrupt):
            self.labels(fake).publish("low", is_current)
        self.assertEqual(fake.pr_labels, set())
        self.assertEqual(fake.events[-2:], [("added", "risk: low"), ("removed", "risk: low")])

    def test_failed_add_attempts_removal_and_raises(self):
        fake = FakeGitHub(fail=lambda method, url, body: 403 if (method, url) == ("POST", ISSUE_LABELS) else None)
        with self.assertRaises(labels.LabelError):
            self.labels(fake).publish("high", current(True, True))
        self.assertEqual(fake.calls[-1], ("DELETE", f"{ISSUE_LABELS}/{ENCODED['high']}", None))

    def test_failed_final_reread_removes_the_label_and_raises(self):
        answers = [True]

        def is_current():
            if answers:
                return answers.pop()
            raise GitHubError("body with PRIVATE_MARKER")

        fake = FakeGitHub()
        with self.assertRaises(labels.LabelError) as caught:
            self.labels(fake).publish("low", is_current)
        self.assertEqual(fake.pr_labels, set())
        self.assertNotIn("PRIVATE_MARKER", str(caught.exception))

    def test_failed_label_lookup_adds_nothing(self):
        fake = FakeGitHub(fail=lambda method, url, body: 500 if method == "GET" else None)
        with self.assertRaises(labels.LabelError):
            self.labels(fake).publish("low", current(True, True))
        self.assertNotIn("POST", [method for method, _, _ in fake.calls])
        self.assertEqual(fake.pr_labels, set())

    def test_unknown_level_is_rejected_without_requests(self):
        fake = FakeGitHub()
        with self.assertRaises(labels.LabelError):
            self.labels(fake).publish("critical", current(True, True))
        self.assertEqual(fake.calls, [])

    def test_urls_are_built_only_from_the_slug_number_and_constants(self):
        fake = FakeGitHub(pr_labels=set(ALL_LABELS), repo_labels=())
        risk_labels = self.labels(fake)
        risk_labels.clear()
        risk_labels.publish("low", current(True, False))
        allowed = {ISSUE_LABELS, REPO_LABELS} | {f"{ISSUE_LABELS}/{value}" for value in ENCODED.values()} | {
            f"{REPO_LABELS}/{value}" for value in ENCODED.values()}
        self.assertTrue(fake.calls)
        for _, url, _ in fake.calls:
            self.assertIn(url, allowed)


class TransportTests(unittest.TestCase):
    def test_missing_token_fails_without_a_request(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "", "GH_TOKEN": ""}), \
                patch.object(labels, "_OPENER") as opener:
            with self.assertRaises(labels.LabelError):
                labels.default_transport()
        opener.open.assert_not_called()

    def test_request_carries_token_and_json_and_returns_status(self):
        seen = []

        class Response(io.BytesIO):
            status = 201

        def open_request(request, timeout):
            seen.append(request)
            return Response(b"{}")

        with patch.dict(os.environ, {"GITHUB_TOKEN": "github-test-key"}), \
                patch.object(labels._OPENER, "open", side_effect=open_request):
            status = labels.default_transport()("POST", REPO_LABELS, {"name": "risk: low"})
        self.assertEqual(status, 201)
        self.assertEqual(seen[0].get_method(), "POST")
        self.assertEqual(seen[0].full_url, REPO_LABELS)
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer github-test-key")
        self.assertEqual(seen[0].data, b'{"name": "risk: low"}')

    def test_http_errors_return_their_status_without_exposing_the_body(self):
        error = urllib.error.HTTPError(REPO_LABELS, 403, "Forbidden", {}, io.BytesIO(b"PRIVATE_MARKER"))
        stream = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_TOKEN": "github-test-key"}), \
                patch.object(labels._OPENER, "open", side_effect=error), \
                patch("sys.stdout", stream), patch("sys.stderr", stream):
            self.assertEqual(labels.default_transport()("GET", REPO_LABELS + "/x", None), 403)
        self.assertEqual(stream.getvalue(), "")

    def test_network_and_tls_errors_become_a_server_error_status(self):
        for error in (urllib.error.URLError("down"), ssl.SSLError("tls"), OSError("reset"), TimeoutError()):
            with self.subTest(error=type(error).__name__), \
                    patch.dict(os.environ, {"GITHUB_TOKEN": "github-test-key"}), \
                    patch.object(labels._OPENER, "open", side_effect=error):
                self.assertGreaterEqual(labels.default_transport()("GET", REPO_LABELS + "/x", None), 500)

    def test_redirects_are_not_followed(self):
        handler = labels._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://example.invalid/"))


if __name__ == "__main__":
    unittest.main()
