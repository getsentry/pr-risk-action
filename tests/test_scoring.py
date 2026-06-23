import unittest

from risk_pr_agent.scoring import (
    apply_final_risk_label,
    evaluate_label_confusion,
    evaluate_prediction_scores,
    score_feature_rows,
)


def row(number, created_at, changed_lines):
    return {
        "repo": "getsentry/cli",
        "number": number,
        "created_at": created_at,
        "merged_at": created_at,
        "prediction_features": {
            "changed_lines": changed_lines,
            "file_count": 1,
            "directory_count": 1,
            "entropy_of_change": 0,
            "max_file_prior_reverts": 0,
            "max_dir_prior_reverts": 0,
        },
        "outcomes": {
            "is_merged": True,
            "strong_outcome": False,
            "medium_outcome": False,
        },
    }


class ScoringTests(unittest.TestCase):
    def test_as_of_percentiles_use_only_prior_rows_in_same_repo(self):
        scored = {
            item["number"]: item
            for item in score_feature_rows(
                [
                    row(3, "2026-01-03T00:00:00Z", 50),
                    row(1, "2026-01-01T00:00:00Z", 10),
                    row(2, "2026-01-02T00:00:00Z", 100),
                ]
            )
        }

        self.assertEqual(scored[1]["prediction_features"]["changed_lines_percentile_repo"], 0)
        self.assertEqual(scored[2]["prediction_features"]["changed_lines_percentile_repo"], 100)
        self.assertEqual(scored[3]["prediction_features"]["changed_lines_percentile_repo"], 50)
        self.assertEqual(scored[3]["prediction"]["percentile_mode"], "as_of")

    def test_as_of_percentiles_do_not_use_same_timestamp_peers(self):
        scored = {
            item["number"]: item
            for item in score_feature_rows(
                [
                    row(1, "2026-01-01T00:00:00Z", 10),
                    row(2, "2026-01-01T00:00:00Z", 100),
                ]
            )
        }

        self.assertEqual(scored[1]["prediction_features"]["changed_lines_percentile_repo"], 0)
        self.assertEqual(scored[2]["prediction_features"]["changed_lines_percentile_repo"], 0)

    def test_label_safety_counts_positive_outcomes_marked_low(self):
        rows = [
            predicted_row(1, True, "low", 10, "low", 0.1),
            predicted_row(2, True, "high", 95, "high", 0.9),
            predicted_row(3, False, "low", 20, "medium", 0.4),
            predicted_row(4, False, "medium", 75, "low", 0.2),
        ]

        evaluation = evaluate_prediction_scores(
            rows,
            outcome_name="strong_outcome",
            score_paths=[
                ("rule_percentile", ("prediction", "risk_percentile_repo")),
                ("logistic_probability", ("prediction", "logistic_probability")),
            ],
        )

        rule_safety = evaluation["label_safety"]["rule_percentile"]
        self.assertEqual(rule_safety["positive_outcomes_marked_low"], 1)
        self.assertEqual(rule_safety["positive_outcomes_marked_low_rate"], 0.5)
        self.assertEqual(rule_safety["non_low_recall_for_positive_outcomes"], 0.5)
        self.assertEqual(rule_safety["high_label_recall_for_positive_outcomes"], 0.5)
        self.assertEqual(rule_safety["low_label_positive_rate"], 0.5)

        logistic_safety = evaluation["label_safety"]["logistic_probability"]
        self.assertEqual(logistic_safety["positive_outcomes_marked_low"], 1)

    def test_audit_label_confusion_prioritizes_high_as_low(self):
        metrics = evaluate_label_confusion(
            [
                {"audit_label": "high", "model_label": "low"},
                {"audit_label": "high", "model_label": "medium"},
                {"audit_label": "medium", "model_label": "low"},
                {"audit_label": "low", "model_label": "high"},
            ]
        )

        self.assertEqual(metrics["high_as_low"], 1)
        self.assertEqual(metrics["high_as_low_rate"], 0.5)
        self.assertEqual(metrics["undercalls"], 3)
        self.assertEqual(metrics["overcalls"], 1)

    def test_final_risk_label_uses_hard_high_guardrails(self):
        prediction = {"logistic_risk_label": "low", "risk_percentile_repo": 91}

        apply_final_risk_label(prediction)

        self.assertEqual(prediction["final_risk_label"], "high")
        self.assertEqual(prediction["final_risk_policy"], "guardrail_hybrid_v2")

    def test_final_risk_label_treats_raw_rule_high_as_medium(self):
        prediction = {
            "logistic_risk_label": "low",
            "risk_label": "high",
            "risk_percentile_repo": 30,
            "risk_score_raw": 120,
        }

        apply_final_risk_label(prediction)

        self.assertEqual(prediction["final_risk_label"], "medium")

    def test_final_risk_label_promotes_sensitive_no_test_history_combo(self):
        prediction = {"logistic_risk_label": "low", "risk_percentile_repo": 30}
        features = {
            "code_changed_without_test_signal": True,
            "sensitive_area_changed": True,
            "max_file_prior_bad_outcomes": 1,
        }

        apply_final_risk_label(prediction, features)

        self.assertEqual(prediction["final_risk_label"], "high")


def predicted_row(number, positive, rule_label, rule_percentile, logistic_label, probability):
    item = row(number, f"2026-01-{number:02d}T00:00:00Z", number * 10)
    item["outcomes"]["strong_outcome"] = positive
    item["prediction"] = {
        "risk_label": rule_label,
        "risk_percentile_repo": rule_percentile,
        "logistic_risk_label": logistic_label,
        "logistic_probability": probability,
    }
    return item


if __name__ == "__main__":
    unittest.main()
