import io
import os
import ssl
import unittest
import urllib.error
from unittest.mock import patch

from risk_pr_agent import github, labels
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
        self.timeouts = []
        self.unexpected = []
        self.events = events if events is not None else []

    def __call__(self, method, url, body=None, timeout=None):
        self.calls.append((method, url, body))
        self.timeouts.append(timeout)
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
        # Production code turns transport exceptions into retried failures, so this
        # is also recorded; CheckedFakes fails the test when anything was recorded.
        self.unexpected.append((method, url))
        raise AssertionError(f"unexpected request {method} {url}")


class CheckedFakes:
    """Test mixin: every FakeGitHub made through ``fake()`` must see only expected requests."""

    def fake(self, **options):
        fake = FakeGitHub(**options)
        self.addCleanup(self.assert_only_expected_requests, fake)
        return fake

    def assert_only_expected_requests(self, fake):
        self.assertEqual(fake.unexpected, [], "FakeGitHub received unexpected requests")


def _raise(error):
    raise error


def deletes_after_add(fake):
    """DELETE requests sent after the first add, i.e. the publisher's own clean-up."""
    added = next(index for index, (method, url, _) in enumerate(fake.calls) if (method, url) == ("POST", ISSUE_LABELS))
    return [method for method, _, _ in fake.calls[added:]].count("DELETE")


def current(*answers):
    answers = list(answers)
    return lambda: answers.pop(0)


class RiskLabelsTests(CheckedFakes, unittest.TestCase):
    def labels(self, fake):
        return labels.RiskLabels(REPO, 7, transport=fake, sleep=lambda seconds: None)

    def test_constants_are_exact(self):
        self.assertEqual(labels.RISK_LABELS, {
            "low": ("risk: low", "0E8A16"), "medium": ("risk: medium", "FBCA04"),
            "high": ("risk: high", "B60205")})

    def test_clear_removes_every_risk_label_and_treats_absent_as_removed(self):
        fake = self.fake(pr_labels={"risk: low", "risk: high", "bug"})
        self.labels(fake).clear()
        self.assertEqual(fake.pr_labels, {"bug"})
        self.assertEqual([(method, url) for method, url, _ in fake.calls],
                         [("DELETE", f"{ISSUE_LABELS}/{ENCODED[level]}") for level in ("low", "medium", "high")])

    def test_clear_fails_on_any_other_status(self):
        for status in (401, 403, 500):
            with self.subTest(status=status):
                fake = self.fake(fail=lambda method, url, body: status if url.endswith(ENCODED["medium"]) else None)
                with self.assertRaises(labels.LabelError):
                    self.labels(fake).clear()

    def test_server_errors_are_retried(self):
        failures = [502]
        fake = self.fake(pr_labels={"risk: low"},
                          fail=lambda method, url, body: failures.pop() if failures else None)
        self.labels(fake).clear()
        self.assertEqual(fake.pr_labels, set())

    def test_publish_adds_existing_label_after_checking_revision(self):
        fake = self.fake()
        self.assertTrue(self.labels(fake).publish("medium", current(True, True)))
        self.assertEqual(fake.pr_labels, {"risk: medium"})
        self.assertEqual(fake.calls, [("GET", f"{REPO_LABELS}/{ENCODED['medium']}", None),
                                      ("DELETE", f"{ISSUE_LABELS}/{ENCODED['low']}", None),
                                      ("DELETE", f"{ISSUE_LABELS}/{ENCODED['high']}", None),
                                      ("POST", ISSUE_LABELS, {"labels": ["risk: medium"]})])

    def test_publish_removes_the_other_risk_labels_immediately_before_adding(self):
        # A label left by an earlier, interrupted run must not coexist with the new one.
        fake = self.fake(pr_labels={"risk: high", "risk: medium", "bug"})
        self.assertTrue(self.labels(fake).publish("low", current(True, True)))
        self.assertEqual(fake.pr_labels, {"bug", "risk: low"})
        self.assertEqual(fake.events, [("removed", "risk: medium"), ("removed", "risk: high"),
                                       ("added", "risk: low")])

    def test_publish_adds_nothing_when_removing_another_risk_label_fails(self):
        fake = self.fake(pr_labels={"risk: high"},
                         fail=lambda method, url, body: 403 if method == "DELETE" else None)
        with self.assertRaises(labels.LabelError) as caught:
            self.labels(fake).publish("low", current(True, True))
        self.assertFalse(caught.exception.cleanup_failed)
        self.assertEqual(fake.pr_labels, {"risk: high"})
        self.assertNotIn(("POST", ISSUE_LABELS), [(method, url) for method, url, _ in fake.calls])

    def test_publish_creates_a_missing_label_with_exact_colour_and_description(self):
        for level, (name, colour) in labels.RISK_LABELS.items():
            with self.subTest(level=level):
                fake = self.fake(repo_labels=())
                self.assertTrue(self.labels(fake).publish(level, current(True, True)))
                self.assertIn(("POST", REPO_LABELS, {"name": name, "color": colour,
                                                     "description": f"PR risk score: {level}"}), fake.calls)
                self.assertEqual(fake.pr_labels, {name})

    def test_publish_tolerates_a_label_created_concurrently(self):
        fake = self.fake(repo_labels=())
        fake.fail = lambda method, url, body: 422 if (method, url) == ("POST", REPO_LABELS) else None
        self.assertTrue(self.labels(fake).publish("low", current(True, True)))
        self.assertEqual(fake.pr_labels, {"risk: low"})

    def test_publish_does_nothing_when_pr_moved_before_publishing(self):
        fake = self.fake()
        self.assertFalse(self.labels(fake).publish("low", current(False)))
        self.assertEqual(fake.calls, [])

    def test_publish_removes_its_label_when_pr_moved_after_adding(self):
        fake = self.fake()
        self.assertFalse(self.labels(fake).publish("low", current(True, False)))
        self.assertEqual(fake.pr_labels, set())
        self.assertEqual(fake.calls[-1], ("DELETE", f"{ISSUE_LABELS}/{ENCODED['low']}", None))

    def test_clean_up_after_move_is_tried_three_times(self):
        for failures, cleaned in ((2, True), (3, False)):
            with self.subTest(failures=failures):
                remaining = [failures]

                def fail(method, url, body):
                    if method == "DELETE" and ("added", "risk: low") in fake.events and remaining[0]:
                        remaining[0] -= 1
                        return 403
                    return None

                fake = self.fake(fail=fail)
                if cleaned:
                    self.assertFalse(self.labels(fake).publish("low", current(True, False)))
                    self.assertEqual(fake.pr_labels, set())
                else:
                    with self.assertRaises(labels.LabelError) as caught:
                        self.labels(fake).publish("low", current(True, False))
                    self.assertTrue(caught.exception.cleanup_failed)
                    self.assertEqual(fake.pr_labels, {"risk: low"})
                self.assertEqual(deletes_after_add(fake), min(failures + 1, 3))

    def test_clean_up_after_error_is_tried_three_times(self):
        fake = self.fake(fail=lambda method, url, body: 403 if method == "DELETE" and fake.pr_labels else None)

        def is_current():
            if fake.pr_labels:
                raise GitHubError("final re-read failed")
            return True

        with self.assertRaises(labels.LabelError) as caught:
            self.labels(fake).publish("low", is_current)
        self.assertTrue(caught.exception.cleanup_failed)
        self.assertEqual(deletes_after_add(fake), 3)

    def test_transport_exceptions_become_label_errors(self):
        def raising(method):
            def fail(m, url, body):
                if m == method:
                    raise ssl.SSLError("tls failure")
            return fail

        fake = self.fake(pr_labels={"risk: low"}, fail=raising("DELETE"))
        with self.assertRaises(labels.LabelError):
            self.labels(fake).clear()
        fake = self.fake(fail=raising("POST"))
        with self.assertRaises(labels.LabelError) as caught:
            self.labels(fake).publish("low", current(True, True))
        self.assertFalse(caught.exception.cleanup_failed)
        self.assertEqual(fake.calls[-1][0], "DELETE")
        fake = self.fake()
        fake.fail = lambda m, url, body: (_raise(ssl.SSLError("tls")) if m == "DELETE" and fake.pr_labels else None)
        with self.assertRaises(labels.LabelError) as caught:
            self.labels(fake).publish("low", current(True, False))
        self.assertTrue(caught.exception.cleanup_failed)
        # Three clean-up attempts, each retried like any server error.
        self.assertEqual(deletes_after_add(fake), 3 * 3)
        self.assertEqual(fake.pr_labels, {"risk: low"})

    def test_cancellation_after_add_removes_the_label_and_propagates(self):
        for interrupt in (KeyboardInterrupt, SystemExit):
            with self.subTest(interrupt=interrupt.__name__):
                answers = [True]

                def is_current():
                    if answers:
                        return answers.pop()
                    raise interrupt

                fake = self.fake()
                with self.assertRaises(interrupt):
                    self.labels(fake).publish("low", is_current)
                self.assertEqual(fake.pr_labels, set())
                self.assertEqual(fake.events[-2:], [("added", "risk: low"), ("removed", "risk: low")])
                self.assertEqual(fake.timeouts[-1], 5)
                self.assertEqual(fake.timeouts[:-1], [30] * (len(fake.timeouts) - 1))

    def test_cancellation_clean_up_is_one_short_delete_without_sleep(self):
        for status in (500, 403):
            with self.subTest(status=status):
                answers = [True]

                def is_current():
                    if answers:
                        return answers.pop()
                    raise KeyboardInterrupt

                fake = self.fake(fail=lambda method, url, body: status if method == "DELETE" and fake.pr_labels else None)
                sleeps = []
                risk_labels = labels.RiskLabels(REPO, 7, transport=fake, sleep=sleeps.append)
                with self.assertRaises(KeyboardInterrupt):
                    risk_labels.publish("low", is_current)
                added = fake.calls.index(("POST", ISSUE_LABELS, {"labels": ["risk: low"]}))
                deletes = [timeout for (method, _, _), timeout in zip(fake.calls[added:], fake.timeouts[added:])
                           if method == "DELETE"]
                self.assertEqual(deletes, [5])
                self.assertEqual(sleeps, [])

    def test_unexpected_request_fails_the_fake_check(self):
        fake = FakeGitHub()
        wrong = labels.RiskLabels(REPO, 7, transport=lambda method, url, body, timeout: fake(
            method, url.replace("/issues/7/", "/issues/8/"), body, timeout), sleep=lambda seconds: None)
        with self.assertRaises(labels.LabelError):
            wrong.clear()  # the AssertionError is swallowed by production code ...
        self.assertTrue(fake.unexpected)
        with self.assertRaises(AssertionError):  # ... but the shared check catches it
            self.assert_only_expected_requests(fake)

    def test_failed_add_attempts_removal_and_raises(self):
        fake = self.fake(fail=lambda method, url, body: 403 if (method, url) == ("POST", ISSUE_LABELS) else None)
        with self.assertRaises(labels.LabelError):
            self.labels(fake).publish("high", current(True, True))
        self.assertEqual(fake.calls[-1], ("DELETE", f"{ISSUE_LABELS}/{ENCODED['high']}", None))

    def test_failed_final_reread_removes_the_label_and_raises(self):
        answers = [True]

        def is_current():
            if answers:
                return answers.pop()
            raise GitHubError("body with PRIVATE_MARKER")

        fake = self.fake()
        with self.assertRaises(labels.LabelError) as caught:
            self.labels(fake).publish("low", is_current)
        self.assertEqual(fake.pr_labels, set())
        self.assertNotIn("PRIVATE_MARKER", str(caught.exception))

    def test_failed_label_lookup_adds_nothing(self):
        fake = self.fake(fail=lambda method, url, body: 500 if method == "GET" else None)
        with self.assertRaises(labels.LabelError):
            self.labels(fake).publish("low", current(True, True))
        self.assertNotIn("POST", [method for method, _, _ in fake.calls])
        self.assertEqual(fake.pr_labels, set())

    def test_unknown_level_is_rejected_without_requests(self):
        fake = self.fake()
        with self.assertRaises(labels.LabelError):
            self.labels(fake).publish("critical", current(True, True))
        self.assertEqual(fake.calls, [])

    def test_urls_are_built_only_from_the_slug_number_and_constants(self):
        fake = self.fake(pr_labels=set(ALL_LABELS), repo_labels=())
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
            seen.append((request, timeout))
            return Response(b"{}")

        with patch.dict(os.environ, {"GITHUB_TOKEN": "github-test-key"}), \
                patch.object(labels._OPENER, "open", side_effect=open_request):
            transport = labels.default_transport()
            status = transport("POST", REPO_LABELS, {"name": "risk: low"})
            transport("DELETE", REPO_LABELS + "/x", None, timeout=5)
        self.assertEqual(status, 201)
        request, timeout = seen[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, REPO_LABELS)
        self.assertEqual(request.get_header("Authorization"), "Bearer github-test-key")
        self.assertEqual(request.data, b'{"name": "risk: low"}')
        self.assertEqual([timeout for _, timeout in seen], [30, 5])

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
        handler = github._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://example.invalid/"))


if __name__ == "__main__":
    unittest.main()
