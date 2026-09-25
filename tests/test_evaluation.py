import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from risk_pr_agent.evaluation import evaluate_assisted_run, evaluate_holdout_once, evaluate_run, select_candidate


CONFIG = {"model": "typesafe-ai/jev", "variant": "A", "max_bytes": 65536, "versions": {"rubric": "jev-risk-v1", "context": "1", "sdk": "7.0.106"}}


def example(identifier, split="dev", stratum="functional"):
    return {"example_id": identifier, "repo": "example/project", "split": split, "strata": [stratum], "snapshot": {"base_sha": f"base-{identifier}", "head_sha": f"head-{identifier}"}}


def label(identifier, actual, split="dev", **kwargs):
    return {"example_id": identifier, "repo": "example/project", "split": split, "snapshot": example(identifier)["snapshot"], "risk_label": actual, "source": "human", "rationale": "Reviewed the changed behavior", "reviewer": "reviewer", "reviewed_at": "2026-01-01", "rubric_version": "jev-risk-v1", **kwargs}


def prediction(identifier, predicted, probabilities=None, **kwargs):
    return {"example_id": identifier, "repo": "example/project", "status": "ok", "risk_label": predicted, "probabilities": probabilities or {name: float(name == predicted) for name in ("low", "medium", "high")}, "configuration": copy.deepcopy(CONFIG), "snapshot": example(identifier)["snapshot"], "attempts": [{"status": "ok", "reported_cost_usd": 0.002, "estimated_cost_usd": 0.001, "usage": {"input_tokens": 10, "output_tokens": 0}, "latency_ms": 10}], "latency_ms": 10, **kwargs}


def baseline(identifier, predicted, **kwargs):
    return {"example_id": identifier, "baseline": "score_pr", "risk_label": predicted, "snapshot": example(identifier)["snapshot"], **kwargs}


def assisted_label(identifier, actual, **kwargs):
    return {**example(identifier), "source": "agent", "review_status": "proposed",
            "proposed_risk_label": actual, "rationale": "Reviewed changed behavior independently",
            "reviewer": "assistant", "reviewed_at": "2026-01-01", "rubric_version": "jev-risk-v1", **kwargs}


def development_selection():
    ids = ["d-low", "d-medium", "d-high"]
    classes = ["low", "medium", "high"]
    report = evaluate_run([prediction(i, c) for i, c in zip(ids, classes)], [label(i, c) for i, c in zip(ids, classes)], examples=[example(i) for i in ids])
    return select_candidate([report])


class EvaluationTests(unittest.TestCase):
    def test_low_review_requires_a_verifiable_expected_snapshot(self):
        report = evaluate_run([prediction("unverified", "low", provider_confidence=1)])
        self.assertEqual(report["coverage"]["predicted"], 1)
        self.assertEqual(report["low_review"]["predicted_low"], 0)
        self.assertEqual(report["low_review"]["prediction_coverage"]["by_status"], {"snapshot_unverified": 1})

        rows = [prediction("unknown-revision", "low", provider_confidence=1),
                prediction("missing-revision", "low", provider_confidence=1, snapshot={})]
        examples = [{**example("unknown-revision"), "snapshot": {}}, example("missing-revision")]
        report = evaluate_run(rows, examples=examples)
        self.assertEqual(report["low_review"]["prediction_coverage"], {
            "valid": 0, "missing_or_invalid": 2,
            "by_status": {"snapshot_unverified": 1, "snapshot_mismatch": 1},
        })
        for curve in report["low_review"]["curves"].values():
            self.assertTrue(all(row["eligible_count"] == 0 for row in curve))

    def test_low_review_curves_preserve_false_lows_unknown_references_and_all_case_denominators(self):
        ids = ["correct", "false-medium", "false-high", "missing", "unknown", "not-low", "stale", "failed", "invalid"]
        actual = {"correct": "low", "false-medium": "medium", "false-high": "high", "missing": "low",
                  "not-low": "medium", "stale": "low", "failed": "low", "invalid": "low"}
        predictions = [
            prediction("correct", "low", {"low": .9, "medium": .1, "high": 0}, provider_confidence=.8),
            prediction("false-medium", "low", {"low": .97, "medium": .02, "high": .01}, provider_confidence=.99),
            prediction("false-high", "low", {"low": .99, "medium": .01, "high": 0}, provider_confidence=.995),
            prediction("unknown", "low"), prediction("not-low", "medium", provider_confidence=1),
            prediction("stale", "low", snapshot={"base_sha": "old", "head_sha": "old"}, provider_confidence=1),
            prediction("failed", "low", status="context_rejected", provider_confidence=1),
            prediction("invalid", "low", {"low": 1, "medium": 1, "high": 1}, provider_confidence=1),
            prediction("unexpected", "low", provider_confidence=1),
        ]
        for evaluate, make_label in ((evaluate_run, label), (evaluate_assisted_run, assisted_label)):
            with self.subTest(evaluator=evaluate.__name__):
                report = evaluate(predictions, [make_label(identifier, value) for identifier, value in actual.items()],
                                  examples=[example(identifier) for identifier in ids])
                curve = report["low_review"]
                self.assertEqual(curve["expected"], 9)
                self.assertEqual(curve["class_support"], {"low": 5, "medium": 2, "high": 1})
                self.assertEqual(curve["unknown_reference_count"], 1)
                self.assertEqual(curve["prediction_coverage"], {
                    "valid": 5, "missing_or_invalid": 4,
                    "by_status": {"ok": 5, "context_rejected": 1, "invalid_prediction": 1, "missing": 1, "snapshot_mismatch": 1},
                })
                self.assertEqual(curve["predicted_low"], 4)
                self.assertEqual(curve["provider_confidence_coverage"], {"available": 3, "missing_or_invalid": 1})
                unfiltered = curve["curves"]["probability_low"][0]
                self.assertEqual(unfiltered["eligible_example_ids"], ["correct", "false-high", "false-medium", "unknown"])
                self.assertEqual(unfiltered["eligible_count"], 4)
                self.assertEqual(unfiltered["all_cases_fraction"], 4 / 9)
                self.assertEqual(unfiltered["known_label_count"], 3)
                self.assertEqual(unfiltered["unknown_label_count"], 1)
                self.assertEqual(unfiltered["correct_low"], 1)
                self.assertEqual(unfiltered["false_low_medium"], 1)
                self.assertEqual(unfiltered["false_low_high"], 1)
                self.assertEqual(unfiltered["error_example_ids"], {"medium": ["false-medium"], "high": ["false-high"]})
                self.assertEqual(unfiltered["precision_known"], 1 / 3)
                self.assertAlmostEqual(unfiltered["precision_known_ci95"][0], 0.06149194472039621)
                self.assertAlmostEqual(unfiltered["precision_known_ci95"][1], 0.7923403991979523)
                self.assertEqual(unfiltered["recall_expected_low"], 1 / 5)
                high_confidence = next(row for row in curve["curves"]["provider_confidence"] if row["threshold"] == .99)
                self.assertEqual(high_confidence["eligible_example_ids"], ["false-high", "false-medium"])
                self.assertEqual(high_confidence["precision_known"], 0)
                self.assertEqual(high_confidence["false_low_high"], 1)
                high_probability = next(row for row in curve["curves"]["probability_low"] if row["threshold"] == .99)
                self.assertEqual(high_probability["eligible_example_ids"], ["false-high", "unknown"])
                for rows in curve["curves"].values():
                    self.assertEqual([row["threshold"] for row in rows], [0, .5, .7, .8, .9, .95, .99])
                    for previous, current in zip(rows, rows[1:]):
                        self.assertLessEqual(set(current["eligible_example_ids"]), set(previous["eligible_example_ids"]))
                self.assertFalse(report["acceptance"]["accepted"])

    def test_low_review_provider_confidence_unknown_is_not_zero(self):
        invalid = [None, float("nan"), float("inf"), -.1, 1.1, True, ".8", 10**400, -(10**400)]
        rows = [prediction(f"invalid-{index}", "low", provider_confidence=value) for index, value in enumerate(invalid)]
        rows += [prediction("zero", "low", provider_confidence=0), prediction("one", "low", provider_confidence=1)]
        report = evaluate_run(rows, [label(row["example_id"], "low") for row in rows])
        curve = report["low_review"]
        self.assertEqual(curve["provider_confidence_coverage"], {"available": 2, "missing_or_invalid": 9})
        self.assertEqual(curve["curves"]["provider_confidence"][0]["eligible_example_ids"], ["one", "zero"])
        self.assertEqual(curve["curves"]["provider_confidence"][1]["eligible_example_ids"], ["one"])
        self.assertEqual(curve["curves"]["probability_low"][0]["eligible_count"], 11)
        json.dumps(report, allow_nan=False)

    def test_low_review_unreviewed_and_stale_references_never_receive_precision_credit(self):
        references = [assisted_label("null", None), assisted_label("invalid", "low", source="human"),
                      assisted_label("stale", "low", snapshot={"base_sha": "old", "head_sha": "old"})]
        ids = ["null", "invalid", "stale", "missing"]
        report = evaluate_assisted_run([prediction(identifier, "low", provider_confidence=1) for identifier in ids],
                                       references, examples=[example(identifier) for identifier in ids])
        curve = report["low_review"]
        self.assertEqual(curve["unknown_reference_count"], 4)
        self.assertEqual(curve["class_support"], {"low": 0, "medium": 0, "high": 0})
        for metric in curve["curves"].values():
            for row in metric:
                self.assertEqual(row["eligible_count"], 4)
                self.assertEqual(row["all_cases_fraction"], 1)
                self.assertEqual(row["known_label_count"], 0)
                self.assertEqual(row["unknown_label_count"], 4)
                self.assertEqual(row["correct_low"], 0)
                self.assertIsNone(row["precision_known"])
                self.assertIsNone(row["precision_known_ci95"])
                self.assertIsNone(row["recall_expected_low"])

    def test_low_review_empty_cohort_has_no_inferred_precision_or_recall(self):
        report = evaluate_run([])
        curve = report["low_review"]
        self.assertEqual(curve["expected"], 0)
        self.assertEqual(curve["prediction_coverage"], {"valid": 0, "missing_or_invalid": 0, "by_status": {}})
        for rows in curve["curves"].values():
            for row in rows:
                self.assertEqual(row["eligible_count"], 0)
                self.assertIsNone(row["all_cases_fraction"])
                self.assertIsNone(row["precision_known"])
                self.assertIsNone(row["precision_known_ci95"])
                self.assertIsNone(row["recall_expected_low"])

    def test_assisted_comparison_keeps_provenance_and_cannot_select_or_promote(self):
        references = [assisted_label("a", "low"), assisted_label("b", "high")]
        original = copy.deepcopy(references)
        report = evaluate_assisted_run(
            [prediction("a", "high"), prediction("b", "low")], references,
            baselines=[baseline("a", "low"), baseline("b", "high")],
            examples=[example("a"), example("b")],
        )
        self.assertEqual(report["reference_type"], "assisted")
        self.assertEqual(report["assisted_references"]["reviewed"], 2)
        self.assertNotIn("human_labels", report)
        self.assertNotIn("human_labels_hash", report)
        self.assertIn("assistant-reviewed", report["calibration"]["brier_definition"])
        self.assertEqual(report["classification"]["low_to_high"], 1)
        self.assertEqual(report["classification"]["high_to_low"], 1)
        self.assertEqual(report["baselines"]["score_pr"]["paired"], 2)
        self.assertFalse(report["acceptance"]["accepted"])
        self.assertEqual(references, original)
        with self.assertRaisesRegex(ValueError, "assisted reports cannot select"):
            select_candidate([report])
        references[0]["rationale"] = "Revised assessment evidence"
        updated = evaluate_assisted_run([], references, examples=[example("a"), example("b")])
        self.assertNotEqual(report["assisted_references_hash"], updated["assisted_references_hash"])

    def test_assisted_null_invalid_and_stale_references_are_reported_and_excluded(self):
        references = [assisted_label("a", "low"), assisted_label("b", None),
                      assisted_label("c", "high", snapshot={"base_sha": "old", "head_sha": "old"}),
                      assisted_label("d", "low", source="human"),
                      assisted_label("e", "low", review_status="approved"),
                      assisted_label("f", "low", rationale=" "),
                      assisted_label("g", "low", split="holdout")]
        report = evaluate_assisted_run([prediction(i, "low") for i in "abcdefgh"], references,
                                       examples=[example(i) for i in "abcdefgh"])
        self.assertEqual(report["assisted_references"]["reviewed"], 1)
        self.assertEqual(report["assisted_references"]["missing_or_invalid"], 7)
        self.assertEqual(report["assisted_references"]["excluded"], {
            "null_proposal": 1, "snapshot_mismatch": 1, "invalid_provenance": 2,
            "invalid_reference": 1, "wrong_split": 1, "missing_reference": 1,
        })
        self.assertEqual(report["assisted_references"]["excluded_example_ids"]["snapshot_mismatch"], ["c"])
        self.assertEqual(report["classification"]["total"], 1)
        self.assertEqual(report["calibration"]["expected"], 1)
        self.assertEqual(report["coverage"]["expected"], 8)
        for field in ("reviewer", "reviewed_at", "rubric_version", "snapshot"):
            with self.subTest(field=field):
                invalid = assisted_label("a", "low")
                del invalid[field]
                result = evaluate_assisted_run([], [invalid], examples=[example("a")])
                self.assertEqual(result["assisted_references"]["excluded"], {"invalid_reference": 1})

    def test_assisted_stale_predictions_remain_missing_in_classification(self):
        stale = prediction("a", "low", snapshot={"base_sha": "old", "head_sha": "old"})
        report = evaluate_assisted_run([stale], [assisted_label("a", "low")], examples=[example("a")])
        self.assertEqual(report["coverage"]["by_status"], {"snapshot_mismatch": 1})
        self.assertEqual(report["classification"]["confusion"]["low"]["missing"], 1)
        self.assertEqual(report["evaluable_example_ids"], [])

    def test_assisted_evaluation_cannot_use_holdout_or_representative_split(self):
        for split in ("holdout", "representative"):
            with self.subTest(split=split), self.assertRaisesRegex(ValueError, "restricted to the dev split"):
                evaluate_assisted_run([], [], split=split)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "human-reviewed holdout labels"):
                evaluate_holdout_once(directory, [prediction("h", "low")],
                                      [assisted_label("h", "low", split="holdout")],
                                      examples=[example("h", "holdout")], selection=development_selection())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_pending_or_agent_labels_never_become_ground_truth(self):
        labels = [label("a", "low", source="agent"), label("b", "high", reviewer=None), label("c", "medium")]
        report = evaluate_run([prediction("a", "high"), prediction("b", "low"), prediction("c", "medium")], labels, outcomes=[{"example_id": "a", "strong_outcome": True}], examples=[example(i) for i in "abc"])
        self.assertEqual(report["human_labels"], {"reviewed": 1, "pending_or_invalid": 2})
        self.assertEqual(report["classification"]["total"], 1)
        self.assertFalse(report["acceptance"]["accepted"])
        self.assertEqual(select_candidate([report])["status"], "insufficient_evidence")

    def test_failures_and_missing_predictions_remain_in_denominator(self):
        report = evaluate_run([prediction("a", "low"), prediction("b", None, status="context_limit", probabilities=None)], [label(i, "low") for i in "abc"], examples=[example(i) for i in "abc"])
        self.assertEqual(report["coverage"]["expected"], 3)
        self.assertEqual(report["coverage"]["predicted"], 1)
        self.assertEqual(report["classification"]["per_class"]["low"]["recall"], 1 / 3)
        self.assertEqual(report["classification"]["confusion"]["low"]["missing"], 2)
        self.assertEqual(report["calibration"]["missing"], 2)

    def test_confusion_brier_slices_and_rounded_probabilities(self):
        report = evaluate_run([prediction("a", "high"), prediction("b", "low"), prediction("c", "medium", {"low": 0.33, "medium": 0.33, "high": 0.33})], [label("a", "low"), label("b", "high"), label("c", "medium")], examples=[example("a", stratum="docs"), example("b"), example("c")])
        self.assertEqual(report["classification"]["low_to_high"], 1)
        self.assertEqual(report["classification"]["high_to_low"], 1)
        self.assertAlmostEqual(report["calibration"]["brier_score"], (4 + 0.67 ** 2 + 2 * 0.33 ** 2) / 3)
        self.assertEqual(report["slices"]["stratum"]["docs"]["low_to_high"], 1)
        self.assertEqual(report["coverage"]["rate"], 1)

    def test_duplicate_and_mixed_configuration_rows_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate prediction"):
            evaluate_run([prediction("a", "low"), prediction("a", "low")])
        changed = prediction("b", "low")
        changed["configuration"]["variant"] = "B"
        with self.assertRaisesRegex(ValueError, "one model/context"):
            evaluate_run([prediction("a", "low"), changed])

    def test_attempt_costs_include_retries_and_unknown_is_not_zero(self):
        row = prediction("a", "low", reported_cost_usd=100)
        row["attempts"].insert(0, {"status": "error", "reported_cost_usd": 0.003, "estimated_cost_usd": None, "usage": {"input_tokens": None, "output_tokens": None}})
        unknown = prediction("b", "low")
        unknown["attempts"][0].update(reported_cost_usd=None, estimated_cost_usd=None)
        report = evaluate_run([row, unknown], examples=[example("a"), example("b")])
        operations = report["operations"]
        self.assertEqual(operations["attempts"], 3)
        self.assertEqual(operations["retries"], 1)
        self.assertEqual(operations["attempt_statuses"], {"error": 1, "ok": 2})
        self.assertIn("sum of recorded model-attempt durations", operations["latency_definition"])
        self.assertIn("retry backoff", operations["latency_definition"])
        self.assertEqual(operations["costs"]["reported_usd"]["known_subtotal"], 0.005)
        self.assertIsNone(operations["costs"]["reported_usd"]["total"])
        self.assertIsNone(operations["costs"]["effective_usd"]["projected_1000"])
        self.assertEqual(operations["costs"]["estimated_usd"]["unknown_count"], 2)
        self.assertEqual(operations["usage"]["input_tokens"]["unknown_attempts"], 1)

    def test_retries_include_changed_context_requests(self):
        row = prediction("a", "low")
        row["attempts"] = [{"request_hash": request_hash, "overall_attempt": index,
                            "status": "ok" if index == 4 else "provider_error"}
                           for index, request_hash in enumerate(("full", "8k", "4k", "4k"), 1)]
        report = evaluate_run([row], examples=[example("a")])
        self.assertEqual(report["operations"]["attempts"], 4)
        self.assertEqual(report["operations"]["retries"], 3)

    def test_known_cost_distribution_and_projection(self):
        report = evaluate_run([prediction("a", "low"), prediction("b", "low")], examples=[example("a"), example("b")])
        costs = report["operations"]["costs"]["reported_usd"]
        self.assertEqual(costs["mean"], 0.002)
        self.assertEqual(costs["projected_1000"], 2)
        self.assertEqual(costs["projected_10000"], 20)

    def test_outcome_recall_counts_missing_predictions_and_requires_maturity(self):
        ids = [str(i) for i in range(20)]
        outcomes = [{"example_id": identifier, "valid": True, "mature": True, "observation_days": 30, "strong_outcome": identifier in ("0", "19"), "medium_outcome": False} for identifier in ids]
        outcomes.append({"example_id": "immature", "valid": True, "mature": False, "observation_days": 10, "strong_outcome": True})
        report = evaluate_run([prediction("0", "high")], outcomes=outcomes, examples=[example(i, "representative") for i in ids + ["immature"]], split="representative")
        historical = report["historical"]
        metrics = historical["metrics"]["strong_outcome"]
        self.assertEqual(historical["eligible"], 20)
        self.assertEqual(metrics["positive_outcomes"], 2)
        self.assertEqual(metrics["top_k"]["top_5"]["recall"], 0.5)
        self.assertEqual(metrics["top_k"]["top_30"]["precision"], 1 / 6)
        self.assertEqual(report["classification"]["total"], 0)

    def test_dev_ignores_holdout_labels_and_disallows_holdout_flag(self):
        report = evaluate_run([prediction("a", "low"), prediction("b", "high")], [label("a", "low"), label("b", "high", split="holdout")])
        self.assertEqual(report["example_ids"], ["a"])
        self.assertEqual(report["classification"]["total"], 1)
        with self.assertRaisesRegex(ValueError, "evaluate_holdout_once"):
            evaluate_run([], split="holdout")

    def test_baseline_requires_matching_exact_revision(self):
        wrong = baseline("a", "high")
        wrong["snapshot"]["head_sha"] = "different"
        report = evaluate_run([prediction("a", "low")], [label("a", "low")], baselines=[wrong], examples=[example("a")])
        self.assertEqual(report["baselines"]["score_pr"]["paired"], 0)
        self.assertEqual(report["baselines"]["score_pr"]["excluded"]["snapshot_mismatch_or_missing"], 1)

    def test_stale_human_labels_are_pending_and_stale_predictions_fail_coverage(self):
        stale_label = label("a", "low")
        stale_label["snapshot"]["head_sha"] = "older"
        stale_prediction = prediction("b", "high")
        stale_prediction["snapshot"]["base_sha"] = "other-base"
        report = evaluate_run([prediction("a", "low"), stale_prediction], [stale_label, label("b", "high")], examples=[example("a"), example("b")])
        self.assertEqual(report["human_labels"]["pending_or_invalid"], 1)
        self.assertEqual(report["coverage"]["by_status"]["snapshot_mismatch"], 1)
        self.assertEqual(report["classification"]["confusion"]["high"]["missing"], 1)

    def test_selection_rejects_variants_using_different_human_labels(self):
        rows = [prediction(c, c) for c in ("low", "medium", "high")]
        labels = [label(c, c) for c in ("low", "medium", "high")]
        first = evaluate_run(rows, labels)
        labels[0]["risk_label"] = "medium"
        second = evaluate_run(rows, labels)
        with self.assertRaisesRegex(ValueError, "same frozen human labels"):
            select_candidate([first, second])

    def test_selection_locks_config_and_breaks_quality_ties_by_cost(self):
        reports = []
        for variant, cost in (("A", 0.01), ("B", 0.001)):
            predictions = [prediction(c, c) for c in ("low", "medium", "high")]
            for row in predictions:
                row["configuration"]["variant"] = variant
                row["attempts"][0]["reported_cost_usd"] = cost
            reports.append(evaluate_run(predictions, [label(c, c) for c in ("low", "medium", "high")]))
        self.assertEqual(select_candidate(reports)["configuration"]["variant"], "B")
        reports[1]["example_ids"] = ["different"]
        with self.assertRaisesRegex(ValueError, "same development cohort"):
            select_candidate(reports)

    def test_selection_accepts_identical_partial_coverage_and_preserves_full_metrics(self):
        labels = [label(c, c) for c in ("low", "medium", "high")] + [label("large", "high")]
        reports = []
        for variant in ("A", "B"):
            predictions = [prediction(c, c) for c in ("low", "medium", "high")]
            predictions.append(prediction("large", None, status="diff_exceeds_budget", probabilities=None, attempts=[]))
            for row in predictions:
                row["configuration"]["variant"] = variant
            reports.append(evaluate_run(predictions, labels))
        selection = select_candidate(reports)
        self.assertEqual(selection["status"], "locked")
        self.assertEqual(selection["development_coverage"]["rate"], 0.75)
        self.assertEqual(selection["development_evaluable_example_ids"], ["high", "low", "medium"])
        self.assertEqual(reports[0]["classification"]["confusion"]["high"]["missing"], 1)
        self.assertEqual(reports[0]["classification"]["per_class"]["high"]["recall"], 0.5)
        self.assertEqual(reports[0]["classification_evaluable"]["macro_f1"], 1)

    def test_selection_rejects_equal_coverage_with_different_evaluable_ids(self):
        labels = [label(c, c) for c in ("low", "medium", "high")] + [label("extra-low", "low"), label("extra-high", "high")]
        reports = []
        for omitted in ("extra-low", "extra-high"):
            reports.append(evaluate_run([prediction(row["example_id"], row["risk_label"]) for row in labels if row["example_id"] != omitted], labels))
        self.assertEqual(reports[0]["coverage"]["rate"], reports[1]["coverage"]["rate"])
        with self.assertRaisesRegex(ValueError, "exactly the same evaluable"):
            select_candidate(reports)

    def test_partial_coverage_does_not_allow_unreviewed_abstentions_in_selection(self):
        labels = [label(c, c) for c in ("low", "medium", "high")] + [label("large", None)]
        report = evaluate_run([prediction(c, c) for c in ("low", "medium", "high")], labels)
        self.assertEqual(report["classification_evaluable"]["macro_f1"], 1)
        self.assertEqual(select_candidate([report])["status"], "insufficient_evidence")

    def test_holdout_once_accepts_measured_improvement_and_refuses_replay(self):
        rows = [(f"h-{risk}-{i}", risk) for risk in ("low", "medium", "high") for i in range(5)]
        labels = [label(identifier, risk, "holdout") for identifier, risk in rows]
        examples = [example(identifier, "holdout") for identifier, _ in rows]
        predictions = [prediction(identifier, risk) for identifier, risk in rows]
        baselines = [baseline(identifier, "high" if risk == "low" else risk) for identifier, risk in rows]
        with tempfile.TemporaryDirectory() as directory:
            result = evaluate_holdout_once(directory, predictions, labels, baselines=baselines, examples=examples, selection=development_selection())
            self.assertTrue(result["acceptance"]["accepted"])
            self.assertEqual(result["classification"]["macro_f1"], 1)
            self.assertEqual(len(list(Path(directory).glob("holdout-*.json"))), 1)
            labels[0]["rationale"] = "Edited rationale cannot reopen frozen benchmark"
            with self.assertRaisesRegex(ValueError, "already been"):
                evaluate_holdout_once(directory, predictions, labels, baselines=baselines, examples=examples, selection=development_selection())

    def test_holdout_accepts_common_evaluable_cohort_and_reports_abstentions(self):
        rows = [(f"h-{risk}-{i}", risk) for risk in ("low", "medium", "high") for i in range(5)]
        labels = [label(identifier, risk, "holdout") for identifier, risk in rows] + [label("large", "high", "holdout")]
        examples = [example(row["example_id"], "holdout") for row in labels]
        predictions = [prediction(identifier, risk) for identifier, risk in rows]
        predictions.append(prediction("large", None, status="diff_exceeds_budget", probabilities=None, attempts=[]))
        baselines = [baseline(identifier, "high" if risk == "low" else risk) for identifier, risk in rows]
        with tempfile.TemporaryDirectory() as directory:
            report = evaluate_holdout_once(directory, predictions, labels, baselines=baselines, examples=examples, selection=development_selection())
        gate = report["acceptance"]
        self.assertTrue(gate["accepted"])
        self.assertEqual((gate["evaluated"], gate["expected"]), (15, 16))
        self.assertEqual(gate["coverage"], 15 / 16)
        self.assertEqual(gate["abstention_statuses"], {"diff_exceeds_budget": 1})
        self.assertEqual(report["classification"]["total"], 16)
        self.assertEqual(report["classification"]["confusion"]["high"]["missing"], 1)
        self.assertEqual(report["classification_evaluable"]["total"], 15)

    def test_partial_holdout_still_requires_baseline_for_every_classified_example(self):
        rows = [(f"h-{risk}-{i}", risk) for risk in ("low", "medium", "high") for i in range(6)]
        labels = [label(identifier, risk, "holdout") for identifier, risk in rows] + [label("large", "high", "holdout")]
        predictions = [prediction(identifier, risk) for identifier, risk in rows]
        baselines = [baseline(identifier, "high" if risk == "low" else risk) for identifier, risk in rows[1:]]
        with tempfile.TemporaryDirectory() as directory:
            report = evaluate_holdout_once(directory, predictions, labels, baselines=baselines, examples=[example(row["example_id"], "holdout") for row in labels], selection=development_selection())
        self.assertFalse(report["acceptance"]["accepted"])
        self.assertEqual(report["acceptance"]["reasons"], ["incomplete_revision_matched_baseline_coverage"])
        self.assertEqual(report["acceptance"]["abstention_statuses"], {"missing": 1})

    def test_holdout_rejects_changed_config_without_consuming_receipt(self):
        changed = prediction("h", "low")
        changed["configuration"]["max_bytes"] = 131072
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "locked development configuration"):
                evaluate_holdout_once(directory, [changed], [label("h", "low", "holdout")], examples=[example("h", "holdout")], selection=development_selection())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_holdout_small_sample_reports_insufficient_evidence(self):
        rows = [(f"h-{risk}", risk) for risk in ("low", "medium", "high")]
        with tempfile.TemporaryDirectory() as directory:
            report = evaluate_holdout_once(directory, [prediction(i, c) for i, c in rows], [label(i, c, "holdout") for i, c in rows], baselines=[baseline(i, "high") for i, c in rows], examples=[example(i, "holdout") for i, c in rows], selection=development_selection())
            self.assertFalse(report["acceptance"]["accepted"])
            self.assertIn("insufficient_class_support", report["acceptance"]["reasons"])

    def test_holdout_rejects_more_false_highs_despite_fewer_low_to_high(self):
        rows = [(f"h-{risk}-{i}", risk, i) for risk in ("low", "medium", "high") for i in range(5)]
        # The candidate fixes all five baseline medium-to-low errors and the
        # low-to-high error, but introduces two medium-to-high false alarms.
        # Macro-F1 improves; total false highs increase from one to two.
        predictions = [prediction(identifier, "high" if risk == "medium" and i < 2 else risk) for identifier, risk, i in rows]
        baselines = [baseline(identifier, "high" if risk == "low" and i == 0 else "low" if risk == "medium" else risk) for identifier, risk, i in rows]
        with tempfile.TemporaryDirectory() as directory:
            report = evaluate_holdout_once(directory, predictions, [label(identifier, risk, "holdout") for identifier, risk, _ in rows], baselines=baselines, examples=[example(identifier, "holdout") for identifier, _, _ in rows], selection=development_selection())
        comparison = report["baselines"]["score_pr"]
        self.assertGreater(comparison["candidate"]["macro_f1"], comparison["baseline"]["macro_f1"])
        self.assertLess(comparison["candidate"]["low_to_high"], comparison["baseline"]["low_to_high"])
        self.assertEqual(comparison["candidate"]["false_high"], 2)
        self.assertEqual(comparison["baseline"]["false_high"], 1)
        self.assertFalse(report["acceptance"]["accepted"])
        self.assertEqual(report["acceptance"]["reasons"], ["false_high_not_reduced"])

    def test_holdout_stale_label_does_not_consume_receipt(self):
        stale = label("h", "low", "holdout")
        stale["snapshot"]["head_sha"] = "older"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "human label snapshot"):
                evaluate_holdout_once(directory, [prediction("h", "low")], [stale], examples=[example("h", "holdout")], selection=development_selection())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_malformed_baselines_do_not_consume_holdout(self):
        predictions = [prediction("h", "low")]
        labels = [label("h", "low", "holdout")]
        examples = [example("h", "holdout")]
        valid_baseline = baseline("h", "high")
        selection = development_selection()
        with tempfile.TemporaryDirectory() as directory:
            for malformed in ([valid_baseline, valid_baseline], [{"example_id": "h", "risk_label": "high"}]):
                with self.assertRaises(ValueError):
                    evaluate_holdout_once(directory, predictions, labels, baselines=malformed, examples=examples, selection=selection)
                self.assertEqual(list(Path(directory).iterdir()), [])
            report = evaluate_holdout_once(directory, predictions, labels, baselines=[valid_baseline], examples=examples, selection=selection)
            self.assertEqual(report["holdout_receipt"]["status"], "completed")

    def test_failed_completion_preserves_readable_reservation_and_blocks_replay(self):
        predictions = [prediction("h", "low")]
        labels = [label("h", "low", "holdout")]
        examples = [example("h", "holdout")]
        selection = development_selection()
        with tempfile.TemporaryDirectory() as directory:
            with patch("risk_pr_agent.evaluation.os.replace", side_effect=OSError("interrupted completion")):
                with self.assertRaisesRegex(OSError, "interrupted completion"):
                    evaluate_holdout_once(directory, predictions, labels, examples=examples, selection=selection)
            receipts = list(Path(directory).iterdir())
            self.assertEqual(len(receipts), 1)
            self.assertEqual(json.loads(receipts[0].read_text())["status"], "reserved")
            with self.assertRaisesRegex(ValueError, "already been reserved"):
                evaluate_holdout_once(directory, predictions, labels, examples=examples, selection=selection)


if __name__ == "__main__":
    unittest.main()
