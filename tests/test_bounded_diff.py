"""Standard-profile context truncation across inference, cache and resume."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from risk_pr_agent.jev import build_request, score_snapshots
from test_jev import PRICE, binary_file, snapshot, success


class BoundedDiffTests(unittest.TestCase):
    def setUp(self):
        self.source = snapshot()
        self.source["files"][0].update(
            patch="diff --git a/src/widget.py b/src/widget.py\n@@ -1 +1,2000 @@\n-old()\n"
                  + "+change('café ☕')\n" * 2000,
            additions=2000,
            previous_path="src/old_widget.py",
        )
        self.source["files"].append({**snapshot()["files"][0], "path": "src/z_last.py"})
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        sleeper = patch("risk_pr_agent.jev.time.sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def score(self, worker, **kwargs):
        return score_snapshots([self.source], self.out, max_bytes=10000,
                               worker=worker, price_snapshot=PRICE, **kwargs)[0]

    def test_oversized_diff_preserves_metadata_and_classifies_with_explicit_limitations(self):
        self.source.update(outcomes="PRIVATE_OUTCOME", risk_label="PRIVATE_OUTCOME")
        self.source["pr_metadata"]["labels"] = "PRIVATE_OUTCOME"
        self.source["files"][0]["rationale"] = "PRIVATE_OUTCOME"
        built = build_request(self.source, max_bytes=10000)
        self.assertEqual(built["status"], "ready")
        self.assertTrue(built["diff_truncated"])
        self.assertLessEqual(built["request_bytes"], 10000)
        self.assertLessEqual(built["estimated_input_tokens"], 30000)
        self.assertEqual(built["context_budget"]["model_limit"], 32000)
        self.assertEqual(built["context_budget"]["reserve"], 2000)
        state = built["request"]["state"]
        self.assertEqual(state["pr"], {key: self.source["pr_metadata"][key] for key in ("title", "description")})
        self.assertEqual(state["line_totals"], {"additions": 2001, "deletions": 2})
        self.assertTrue(state["code_context"]["truncated"])
        for actual, original in zip(state["files"], self.source["files"]):
            for key in ("path", "status", "additions", "deletions", "previous_path"):
                self.assertEqual(actual.get(key), original.get(key))
        self.assertTrue(self.source["files"][0]["patch"].startswith(state["files"][0]["patch"]))
        self.assertNotIn("patch", state["files"][1])
        self.assertEqual({item["path"] for item in built["omitted"]}, {"src/widget.py", "src/z_last.py"})
        self.assertNotIn("PRIVATE_OUTCOME", json.dumps(built["request"]))
        self.assertEqual(build_request(copy.deepcopy(self.source), max_bytes=10000)["request_hash"], built["request_hash"])
        calls = []

        def worker(request):
            calls.append(request)
            return success()

        row = self.score(worker)
        self.assertEqual(calls, [built["request"]])
        self.assertEqual(row["risk_label"], "low")
        self.assertTrue(row["diff_truncated"])
        self.assertEqual(row["omitted"], built["omitted"])
        self.assertEqual(row["estimated_input_tokens"], built["estimated_input_tokens"])
        self.assertTrue(self.score(lambda _: self.fail("cached requests must not call the provider"))["cache_hit"])
        self.sleep.assert_not_called()

    def test_explicit_context_rejection_reduces_code_and_preserves_accounting(self):
        responses = iter([{"status": "context_rejected", "retryable": False}, success()])
        calls = []

        def worker(request):
            calls.append(request)
            return next(responses)

        row = self.score(worker)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["context_stage"], "reduced_diff")
        self.assertEqual(row["context_fallback_reason"], "provider_context_rejected")
        self.assertEqual([attempt["context_stage"] for attempt in row["attempts"]], ["bounded_diff", "reduced_diff"])
        self.assertLess(len(calls[1]["state"]["files"][0]["patch"]), len(calls[0]["state"]["files"][0]["patch"]))
        self.assertEqual(calls[0]["state"]["pr"], calls[1]["state"]["pr"])
        self.assertEqual(calls[0]["state"]["line_totals"], calls[1]["state"]["line_totals"])
        self.sleep.assert_called_once_with(10)
        self.assertIsNone(row["usage"]["input_tokens"])
        self.assertIsNone(row["reported_cost_usd"])
        self.assertEqual(row["reported_cost_usd_known"], success()["reported_cost_usd"])
        cached = self.score(lambda _: self.fail("completed stages must resume from the journal/cache"))
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(cached["attempts"], row["attempts"])
        self.sleep.assert_called_once_with(10)
        self.assertEqual(len((self.out / "attempts.jsonl").read_text().splitlines()), 4)

    def test_transient_retries_reduce_input_budget_and_resume_without_extra_calls(self):
        self.source["files"][0]["patch"] *= 3
        calls = []

        def worker(request):
            calls.append(request)
            return {"status": "provider_error", "retryable": True, "error": {"statusCode": 503}} if len(calls) < 4 else success()

        row = score_snapshots([self.source], self.out, worker=worker, price_snapshot=PRICE)[0]
        self.assertEqual(row["status"], "ok")
        self.assertEqual([a["context_budget"]["max_input_tokens"] for a in row["attempts"]], [30000, 8000, 4000, 4000])
        self.assertEqual([a["overall_attempt"] for a in row["attempts"]], [1, 2, 3, 4])
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [10, 20, 30])
        counts = [a["estimated_input_tokens"] for a in row["attempts"]]
        for actual, limit in zip(counts, (30000, 8000, 4000, 4000)):
            self.assertLessEqual(actual, limit)
            self.assertGreater(actual, limit - 30)
        self.assertEqual(calls[2], calls[3])
        self.assertEqual(len({a["request_hash"] for a in row["attempts"]}), 3)
        self.assertEqual(row["request_hash"], row["attempts"][-1]["request_hash"])
        self.assertEqual(row["context_fallback_reason"], "transient_error")
        for request in calls:
            self.assertEqual(request["state"]["pr"], calls[0]["state"]["pr"])
            self.assertEqual(request["state"]["line_totals"], calls[0]["state"]["line_totals"])
            for actual, original in zip(request["state"]["files"], self.source["files"]):
                for key in ("path", "status", "previous_path", "additions", "deletions"):
                    self.assertEqual(actual.get(key), original.get(key))
        self.assertIsNone(row["reported_cost_usd"])
        self.assertEqual(row["reported_cost_usd_known"], success()["reported_cost_usd"])
        cached = score_snapshots([self.source], self.out, worker=lambda _: self.fail("cached fallback must not call"), price_snapshot=PRICE)[0]
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(cached["attempts"], row["attempts"])
        self.assertEqual(self.sleep.call_count, 3)

    def test_small_requests_keep_code_but_track_each_retry_budget(self):
        self.source = snapshot()
        calls = []

        def worker(request):
            calls.append(request)
            return {"status": "timeout", "retryable": True} if len(calls) < 3 else success()

        row = self.score(worker)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(calls, [calls[0]] * 3)
        self.assertEqual([a["context_budget"]["max_input_tokens"] for a in row["attempts"]], [30000, 8000, 4000])
        self.assertEqual(len({a["request_hash"] for a in row["attempts"]}), 3)
        self.assertFalse(row["diff_truncated"])

    def test_retry_metadata_floor_stops_without_discarding_metadata_or_prior_cost(self):
        self.source["pr_metadata"]["description"] = "Required description. " * 2500
        calls = []

        def worker(request):
            calls.append(request)
            return {"status": "provider_error", "retryable": True, "reported_cost_usd": 0.002}

        row = score_snapshots([self.source], self.out, worker=worker, price_snapshot=PRICE)[0]
        self.assertEqual(row["status"], "metadata_exceeds_budget")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["state"]["pr"]["description"], self.source["pr_metadata"]["description"])
        self.assertEqual(row["context_budget"]["max_input_tokens"], 8000)
        self.assertIsNone(row["risk_label"])
        self.assertNotIn("request_hash", row)
        self.assertEqual(row["initial_request_hash"], row["attempts"][0]["request_hash"])
        self.assertEqual(row["reported_cost_usd"], 0.002)
        self.sleep.assert_not_called()

    def test_retry_budgets_preserve_configuration_for_mixed_outcome_evaluation(self):
        full = build_request(self.source)
        reduced = build_request(self.source, max_input_tokens=8000)
        self.assertEqual(full["configuration_hash"], reduced["configuration_hash"])
        self.assertEqual(full["configuration"], reduced["configuration"])
        self.assertNotEqual(full["request_hash"], reduced["request_hash"])

    def test_context_rejection_after_transients_can_use_the_final_attempt(self):
        responses = iter([{"status": "timeout", "retryable": True}] * 2 +
                         [{"status": "context_rejected", "retryable": False}, success()])
        row = self.score(lambda _: next(responses))
        self.assertEqual(row["status"], "ok")
        self.assertEqual([a["context_stage"] for a in row["attempts"]], ["bounded_diff"] * 3 + ["reduced_diff"])
        self.assertEqual([a["overall_attempt"] for a in row["attempts"]], [1, 2, 3, 4])

    def test_transient_retry_preserves_prior_context_rejection_and_reduced_stage(self):
        responses = iter([{"status": "context_rejected", "retryable": False},
                          {"status": "timeout", "retryable": True}, success()])
        row = self.score(lambda _: next(responses))
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["context_fallback_reason"], "provider_context_rejected")
        self.assertEqual([a["context_stage"] for a in row["attempts"]], ["bounded_diff", "reduced_diff", "reduced_diff"])
        self.assertEqual([a["context_budget"]["max_input_tokens"] for a in row["attempts"]], [30000, 8000, 4000])

    def test_transient_errors_and_context_reductions_share_four_attempts(self):
        responses = iter([
            {"status": "provider_error", "retryable": True, "error": {"statusCode": 503}},
            {"status": "context_rejected", "retryable": False},
            {"status": "provider_error", "retryable": True, "error": {"statusCode": 503}},
            {"status": "context_rejected", "retryable": False},
        ])
        calls = []

        def worker(request):
            calls.append(request)
            return next(responses)

        row = self.score(worker)
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[2], calls[3])
        self.assertNotEqual(calls[0], calls[2])
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [10, 20, 30])
        self.assertEqual([a["context_stage"] for a in row["attempts"]], ["bounded_diff"] * 2 + ["reduced_diff"] * 2)
        self.assertEqual(row["status"], "context_rejected")
        self.assertIsNone(row["risk_label"])
        resumed = self.score(lambda _: self.fail("resume cannot reset the shared attempt budget"))
        self.assertEqual(resumed["attempts"], row["attempts"])
        self.assertEqual(self.sleep.call_count, 3)

    def test_repeated_context_rejections_can_reach_metadata_only(self):
        calls = []

        def worker(request):
            calls.append(request)
            return {"status": "context_rejected", "retryable": False} if len(calls) < 3 else success()

        row = self.score(worker)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["context_stage"], "metadata_only")
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [10, 20])
        self.assertEqual(calls[-1]["state"]["pr"], calls[0]["state"]["pr"])
        self.assertTrue(calls[-1]["state"]["code_context"]["truncated"])
        self.assertEqual([file["path"] for file in calls[-1]["state"]["files"]], ["src/widget.py", "src/z_last.py"])
        self.assertTrue(all("patch" not in file for file in calls[-1]["state"]["files"]))

    def test_interrupted_reduced_context_resumes_with_remaining_global_budget(self):
        calls = []

        def interrupted(request):
            calls.append(request)
            if len(calls) == 1:
                return {"status": "context_rejected", "retryable": False}
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.score(interrupted)
        self.sleep.reset_mock()
        recovered = []

        def worker(request):
            recovered.append(request)
            return success()

        row = self.score(worker)
        self.assertEqual(recovered, [calls[-1]])
        self.assertEqual([a["status"] for a in row["attempts"]], ["context_rejected", "interrupted", "ok"])
        self.assertIsNone(row["usage"]["input_tokens"])
        self.assertIsNone(row["estimated_cost_usd"])
        self.sleep.assert_called_once_with(20)

    def test_metadata_that_cannot_fit_remains_unclassified_without_a_provider_call(self):
        self.source["pr_metadata"]["description"] = "Essential description. " * 10000
        row = self.score(lambda _: self.fail("metadata must not be silently discarded"))
        self.assertEqual(row["status"], "metadata_exceeds_budget")
        self.assertIsNone(row["risk_label"])
        self.assertEqual(row["attempts"], [])
        self.assertNotIn("request_hash", row)
        self.sleep.assert_not_called()

    def test_hash_and_cache_follow_sent_context_not_unseen_diff_tail(self):
        first = build_request(self.source, max_bytes=10000)
        original = self.source["files"][0]["patch"]
        self.source["files"][0]["patch"] = original[:-8] + "12345678"
        unseen_change = build_request(self.source, max_bytes=10000)
        self.assertEqual(first["request"], unseen_change["request"])
        self.assertEqual(first["request_hash"], unseen_change["request_hash"])
        self.score(lambda _: success())
        self.source["files"][0]["patch"] = original
        self.assertTrue(self.score(lambda _: self.fail("identical sent requests may reuse cached results"))["cache_hit"])
        self.source["files"][0]["patch"] = original.replace("-old()", "-OLD()", 1)
        included_change = build_request(self.source, max_bytes=10000)
        self.assertNotEqual(first["request_hash"], included_change["request_hash"])

    def test_binary_transition_can_truncate_readable_side_without_losing_binary_metadata(self):
        self.source["files"] = [binary_file(before="previous_behavior();\n" * 2000)]
        built = build_request(self.source, max_bytes=10000)
        self.assertEqual(built["status"], "ready")
        self.assertTrue(built["diff_truncated"])
        file = built["request"]["state"]["files"][0]
        self.assertEqual(file["content_metadata"], self.source["files"][0]["content_metadata"])
        self.assertEqual(file["content_not_inspected"], ["after"])
        self.assertLess(len(file["before"]), len(self.source["files"][0]["before"]))
        self.assertTrue(any(item.get("side") == "before" and item["kind"] == "file_content" for item in built["omitted"]))
