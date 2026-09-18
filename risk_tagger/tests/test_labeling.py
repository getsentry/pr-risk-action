import unittest

from risk_tagger.labeling import LabelConfig, assign_labels, is_safe_scope
from risk_tagger.evaluate import safety_metrics


def make_row(number, created_at, features, outcome=False):
    base = {
        "changed_lines": 0, "file_count": 1, "directory_count": 1,
        "max_file_churn_ratio": 0, "sum_churn_over_base_sloc": 0,
        "max_file_prior_bad_outcomes": 0, "max_dir_prior_bad_outcomes": 0,
    }
    base.update(features)
    return {
        "repo": "getsentry/sentry",
        "number": number,
        "created_at": created_at,
        "title": f"PR {number}",
        "prediction_features": base,
        "outcomes": {"is_merged": True, "strong_outcome": outcome, "medium_outcome_strict": outcome},
    }


def label_of(rows, number):
    labeled = assign_labels(rows, percentile_mode="as_of")
    return {r["number"]: r["risk"]["label"] for r in labeled}[number]


class LabelingTests(unittest.TestCase):
    def _filler(self, n=20):
        # background population so percentiles are well-defined
        return [
            make_row(i, f"2026-01-{i:02d}T00:00:00Z", {"changed_lines": i * 5, "file_count": 1 + i % 4})
            for i in range(1, n + 1)
        ]

    def test_tiny_migration_with_app_code_is_never_low_and_is_high(self):
        rows = self._filler()
        # smallest possible diff, but a migration touching app code -> force_high
        rows.append(make_row(
            999, "2026-02-01T00:00:00Z",
            {"changed_lines": 1, "file_count": 2, "migration_changed": True, "migration_file_count": 1},
        ))
        self.assertEqual(label_of(rows, 999), "high")

    def test_ci_deploy_change_forces_high(self):
        rows = self._filler()
        rows.append(make_row(999, "2026-02-01T00:00:00Z",
                             {"changed_lines": 2, "ci_or_deploy_changed": True}))
        self.assertEqual(label_of(rows, 999), "high")

    def test_docs_only_small_change_is_bypass_eligible_low(self):
        rows = self._filler()
        rows.append(make_row(999, "2026-02-01T00:00:00Z",
                             {"changed_lines": 1, "file_count": 1, "docs_only": True}))
        self.assertEqual(label_of(rows, 999), "low")

    def test_any_gating_signal_forbids_low(self):
        # a docs_only change that also trips a gating signal must not be bypass-eligible
        rows = self._filler()
        rows.append(make_row(999, "2026-02-01T00:00:00Z",
                             {"changed_lines": 1, "docs_only": True, "lockfile_changed": True}))
        self.assertNotEqual(label_of(rows, 999), "low")

    def test_is_safe_scope_rejects_sensitive_tests(self):
        cfg = LabelConfig()
        self.assertTrue(is_safe_scope({"docs_only": True}, cfg))
        self.assertTrue(is_safe_scope({"tests_only": True}, cfg))
        self.assertFalse(is_safe_scope({"tests_only": True, "ci_or_deploy_changed": True}, cfg))
        self.assertFalse(is_safe_scope({"code_changed_without_test_signal": True}, cfg))

    def test_safety_metric_counts_leakage_into_low(self):
        rows = self._filler()
        # a positive-outcome PR that is docs-only+small -> lands low (a leak)
        rows.append(make_row(999, "2026-02-01T00:00:00Z",
                             {"changed_lines": 1, "docs_only": True}, outcome=True))
        labeled = assign_labels(rows, percentile_mode="as_of")
        metrics = safety_metrics(labeled, "strong_outcome")
        self.assertEqual(metrics["positive_outcomes"], 1)
        self.assertEqual(metrics["leakage_into_low"], 1)
        self.assertEqual(metrics["leakage_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
