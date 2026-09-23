"""Provider-boundary tests for full-source context reduction and accounted resume."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from risk_pr_agent.jev import build_request, score_snapshots
from risk_pr_agent.evaluation import evaluate_run
from test_jev import PRICE, snapshot, success


class ContextFallbackTests(unittest.TestCase):
    def setUp(self):
        sleeper = patch("risk_pr_agent.jev.time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.source = snapshot()
        self.source["files"][0].update(additions=1, deletions=1)
        self.source["pr_metadata"] = {"title": "Change widget", "description": "A bounded change",
                                      "description_available": True}
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)

    def score(self, worker, **kwargs):
        return score_snapshots([self.source], self.out, input_profile="metadata-files",
                               worker=worker, price_snapshot=PRICE, **kwargs)[0]

    def events(self):
        return [json.loads(line) for line in (self.out / "attempts.jsonl").read_text().splitlines()]

    def test_provider_rejection_removes_only_full_content_and_accounts_both_requests(self):
        calls = []
        def worker(request):
            calls.append(request)
            if len(calls) == 1:
                return {"status": "context_rejected", "retryable": False}
            return success()
        row = self.score(worker)
        self.assertEqual(len(calls), 2)
        reduced = json.loads(json.dumps(calls[0]))
        del reduced["state"]["files"][0]["after"]
        self.assertEqual(calls[1], reduced)
        self.assertEqual(row["risk_label"], "low")
        self.assertEqual(row["context_stage"], "diff_only")
        self.assertEqual(row["context_fallback_reason"], "provider_context_rejected")
        self.assertNotEqual(row["initial_request_hash"], row["request_hash"])
        self.assertEqual([a["context_stage"] for a in row["attempts"]], ["full_files", "diff_only"])
        self.assertIsNone(row["reported_cost_usd"])
        self.assertIsNone(row["usage"]["input_tokens"])
        self.assertEqual(row["reported_cost_usd_known"], success()["reported_cost_usd"])
        operations = evaluate_run([row])["operations"]
        self.assertEqual(operations["context_fallbacks"], 1)
        self.assertEqual(operations["retries"], 0)
        self.assertEqual(operations["costs"]["reported_usd"]["unknown_attempts"], 1)
        self.assertEqual(len(self.events()), 4)
        # The rejected full request is remembered, and only the reduced success is cached.
        cached = self.score(lambda request: self.fail("resume must not call the provider"))
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(cached["attempts"], row["attempts"])
        self.assertEqual(cached["request_hash"], row["request_hash"])
        self.assertEqual(len(self.events()), 4)

    def test_rejected_essential_diff_stays_unclassified_without_metadata_only_fallback(self):
        calls = []
        def worker(request):
            calls.append(request)
            return {"status": "context_rejected", "retryable": False}
        row = self.score(worker)
        self.assertEqual(len(calls), 2)
        self.assertEqual(row["status"], "context_rejected")
        self.assertIsNone(row["risk_label"])
        self.assertIsNone(row["probabilities"])
        self.assertIn("patch", calls[-1]["state"]["files"][0])

    def test_transient_errors_retry_same_context_without_dropping_source(self):
        calls = []
        def worker(request):
            calls.append(request)
            return {"status": "provider_error", "retryable": True}
        with patch("risk_pr_agent.jev.time.sleep"):
            row = self.score(worker, max_attempts=2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(row["context_stage"], "full_files")
        self.assertNotIn("context_fallback_reason", row)
        self.assertIsNone(row["risk_label"])

    def test_resume_interrupted_fallback_keeps_unknown_attempts_and_does_not_repeat_rejection(self):
        def interrupted(request):
            if "after" in request["state"]["files"][0]:
                return {"status": "context_rejected", "retryable": False}
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.score(interrupted)
        calls = []
        def recovered(request):
            calls.append(request)
            self.assertNotIn("after", request["state"]["files"][0])
            return success()
        row = self.score(recovered)
        self.assertEqual(len(calls), 1)
        self.assertEqual([a["status"] for a in row["attempts"]], ["context_rejected", "interrupted", "ok"])
        self.assertEqual([a["attempt"] for a in row["attempts"]], [1, 1, 2])
        self.assertIsNone(row["reported_cost_usd"])
        self.assertEqual(row["status"], "ok")
        operations = evaluate_run([row])["operations"]
        self.assertEqual(operations["context_fallbacks"], 1)
        self.assertEqual(operations["retries"], 1)

    def test_explicit_local_guard_reduces_before_inference_and_preserves_billing(self):
        minimal = build_request(self.source, input_profile="metadata-files", context_stage="diff_only")
        calls = []
        def worker(request):
            calls.append(request)
            return success()
        row = self.score(worker, max_bytes=minimal["request_bytes"])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("after", calls[0]["state"]["files"][0])
        self.assertEqual(row["context_stage"], "diff_only")
        self.assertTrue(any(item["reason"] == "local_byte_budget" for item in row["omitted"]))
        self.assertEqual(row["reported_cost_usd"], success()["reported_cost_usd"])


if __name__ == "__main__":
    unittest.main()
