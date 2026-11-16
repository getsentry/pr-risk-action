import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from risk_pr_agent.jev import score_snapshots
from risk_pr_agent.sdk_rubric import MECHANISMS
from risk_pr_agent.sdk_rules import apply_rules

from test_jev import PRICE, success
from test_sdk_request import FIXTURES, sdk_snapshot


def row(label="low", p_low=0.95, files=(), **extra):
    rest = (1 - p_low) / 2
    return {"status": "ok", "risk_label": label, "probabilities": {"low": p_low, "medium": rest, "high": rest},
            "context_stage": "bounded_diff", "diff_truncated": False, "omitted": [],
            "mechanisms": {name: 0.1 for name in MECHANISMS},
            "sdk": {"files": [{"path": path, "families": [], "shared_core": False, "kinds": list(kinds),
                               "unmapped": unmapped} for path, kinds, unmapped in files]}, **extra}


class RulesTest(unittest.TestCase):
    def fired(self, value):
        apply_rules(value)
        return [entry["rule"] for entry in value["low_blocked_by"]]

    def test_clean_low_stays_low(self):
        value = row(files=[("core/src/test/T.kt", ["tests"], False)])
        self.assertEqual(self.fired(value), [])
        self.assertEqual((value["model_label"], value["risk_label"]), ("low", "low"))

    def test_each_rule_raises_low_to_medium(self):
        cases = {
            "R1": row(diff_truncated=True),
            "R2": row(omitted=[{"kind": "binary_content", "path": "a.bin", "side": "after"}]),
            "R3": row(files=[("newmodule/C.kt", [], True)]),
            "R4": row(files=[("core/api/core.api", ["public-api-record"], False)]),
            "R5": row(files=[("package.json", ["dependencies"], False)]),
            "R6": row(files=[("samples/build.gradle.kts", ["build-release", "samples"], False)]),
            "R7": row(p_low=0.89),
        }
        for rule, value in cases.items():
            self.assertEqual(self.fired(value), [rule])
            self.assertEqual((value["model_label"], value["risk_label"]), ("low", "medium"), rule)

    def test_reduced_stage_fires_r1(self):
        self.assertEqual(self.fired(row(context_stage="reduced_diff")), ["R1"])

    def test_omitted_diff_and_file_content_fire_r1_with_files(self):
        value = row(omitted=[{"kind": "diff", "path": "b.kt"}, {"kind": "file_content", "path": "a.kt"},
                             {"kind": "test", "path": "t.kt"}])
        apply_rules(value)
        self.assertEqual(value["low_blocked_by"], [{"rule": "R1", "files": ["a.kt", "b.kt"]}])

    def test_threshold_is_inclusive_at_090(self):
        self.assertEqual(self.fired(row(p_low=0.90)), [])

    def test_rules_never_lower_or_reach_high(self):
        for label in ("medium", "high"):
            value = row(label=label, p_low=0.1, files=[("newmodule/C.kt", [], True)])
            self.assertEqual(self.fired(value), ["R3"])
            self.assertEqual(value["risk_label"], label)

    def test_rule_records_files(self):
        value = row(files=[("b/x.api", ["public-api-record"], False), ("a/y.api", ["public-api-record"], False)])
        apply_rules(value)
        self.assertEqual(value["low_blocked_by"], [{"rule": "R4", "files": ["a/y.api", "b/x.api"]}])

    def test_incomplete_mechanisms_invalidate(self):
        for mutate in (lambda m: m.pop("m_data"), lambda m: m.update(extra=0.1)):
            value = row()
            mutate(value["mechanisms"])
            apply_rules(value)
            self.assertEqual((value["status"], value["risk_label"], value["probabilities"]),
                             ("invalid_response", None, None))
            self.assertNotEqual(value.get("model_label"), "low")

    def test_missing_mechanisms_invalidate(self):
        value = row(mechanisms=None)
        apply_rules(value)
        self.assertEqual((value["status"], value["risk_label"]), ("invalid_response", None))
        self.assertIsNone(value["model_label"])

    def test_invalidation_clears_mechanisms_and_records_reason(self):
        value = row()
        value["mechanisms"].pop("m_data")
        apply_rules(value)
        self.assertIsNone(value["mechanisms"])
        self.assertEqual(value["error"], {"name": "invalid_mechanisms"})
        self.assertNotIn("low_blocked_by", value)

    def test_invalidation_keeps_an_existing_error(self):
        value = row(mechanisms=[0.1], error={"name": "Upstream"})
        apply_rules(value)
        self.assertIsNone(value["mechanisms"])
        self.assertEqual(value["error"], {"name": "Upstream"})

    def test_absent_context_stage_fires_r1(self):
        value = row()
        del value["context_stage"]
        self.assertEqual(self.fired(value), ["R1"])
        self.assertEqual(value["risk_label"], "medium")

    def test_non_ok_row_is_untouched(self):
        value = {"status": "timeout", "risk_label": None}
        apply_rules(value)
        self.assertEqual(value, {"status": "timeout", "risk_label": None})


@patch("risk_pr_agent.sdk_facts.SDK_ROOT", FIXTURES)
class ScoreSnapshotsTest(unittest.TestCase):
    def score(self, repo, path):
        source = sdk_snapshot([path])
        source["repo"] = repo
        worker = lambda _: success(probabilities={"low": .97, "medium": .02, "high": .01},
                                   mechanisms={name: 0.05 for name in MECHANISMS})
        with tempfile.TemporaryDirectory() as out:
            return score_snapshots([source], out, worker=worker, price_snapshot=PRICE)[0]

    def test_sdk_row_blocks_low_on_public_api_record(self):
        value = self.score("org/project", "core/api/core.api")
        self.assertEqual((value["status"], value["model_label"], value["risk_label"]), ("ok", "low", "medium"))
        self.assertEqual(value["low_blocked_by"][0]["rule"], "R4")
        self.assertEqual(set(value["mechanisms"]), set(MECHANISMS))

    def test_cache_hit_row_keeps_rules_and_final_label(self):
        source = sdk_snapshot(["core/api/core.api"])
        source["repo"] = "org/project"
        worker = lambda _: success(probabilities={"low": .97, "medium": .02, "high": .01},
                                   mechanisms={name: 0.05 for name in MECHANISMS})

        def refuse(_):
            raise AssertionError("the worker must not be called on a cache hit")

        with tempfile.TemporaryDirectory() as out:
            first = score_snapshots([source], out, worker=worker, price_snapshot=PRICE)[0]
            second = score_snapshots([source], out, worker=refuse, price_snapshot=PRICE)[0]
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual((second["model_label"], second["risk_label"]), ("low", "medium"))
        self.assertEqual(second["low_blocked_by"], first["low_blocked_by"])
        self.assertEqual(second["risk_label"], first["risk_label"])

    def test_invalid_response_row_carries_no_mechanisms(self):
        source = sdk_snapshot(["core/src/main/A.kt"])
        source["repo"] = "org/project"
        mechanisms = {name: 0.05 for name in MECHANISMS}
        mechanisms["m_data"] = 2
        worker = lambda _: success(probabilities={"low": .97, "medium": .02, "high": .01}, mechanisms=mechanisms)
        with tempfile.TemporaryDirectory() as out:
            value = score_snapshots([source], out, worker=worker, price_snapshot=PRICE)[0]
        self.assertEqual((value["status"], value["risk_label"]), ("invalid_response", None))
        self.assertIsNone(value["mechanisms"])
        self.assertNotIn("low_blocked_by", value)

    def test_non_sdk_row_gains_no_keys(self):
        value = self.score("org/other", "core/api/core.api")
        self.assertEqual(value["risk_label"], "low")
        for key in ("model_label", "low_blocked_by", "mechanisms", "sdk"):
            self.assertNotIn(key, value)


if __name__ == "__main__":
    unittest.main()
