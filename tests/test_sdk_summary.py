import unittest

from risk_pr_agent.commands import risk_summary
from risk_pr_agent.sdk_rules import LOW_PROBABILITY_THRESHOLD

MECHANISMS = {"m_stability": .1, "m_performance": .2, "m_contract": .9, "m_data": .3,
              "m_inherited": .4, "m_reach": .5, "m_ships": .6}
NAMES = ["host stability", "host performance", "public contract", "data sent",
         "inherited by customers", "reach", "ships to customers"]


def sdk_row(files=("core/api/core.api",), rule="R4", model="low", final="medium", **extra):
    row = {"status": "ok", "risk_label": final, "model_label": model, "variant": "x",
           "low_blocked_by": [{"rule": rule, "files": list(files)}], "mechanisms": dict(MECHANISMS),
           "sdk": {"rubric_version": "1", "facts_hash": "h", "families_touched": ["server"],
                   "shared_core": False, "files": []}}
    row.update(extra)
    return row


class SdkSummaryTests(unittest.TestCase):
    def test_non_sdk_row_is_unchanged(self):
        row = {"status": "ok", "risk_label": "medium", "input_profile": "metadata-diff",
               "probabilities": {"low": .1, "medium": .8, "high": .1}}
        self.assertEqual(risk_summary(row), (
            "# PR risk\n\nStatus: `ok`\nRisk: `medium`\nContext: `metadata-diff`\n"
            "Class probabilities: low=0.10, medium=0.80, high=0.10\n\n"
            "Class probabilities describe the risk rubric, not incident likelihood.\n"))

    def test_raised_label_path_and_mechanisms(self):
        summary = risk_summary(sdk_row())
        self.assertIn("Model label: `low`", summary)
        self.assertIn("Raised from low to medium by: R4.", summary)
        self.assertIn("`core/api/core.api`", summary)
        self.assertIn("Families touched: server", summary)
        for name in NAMES:
            self.assertIn(name, summary)
        self.assertLess(summary.index("public contract"), summary.index("ships to customers"))
        self.assertIn("0.90", summary)

    def test_layout_puts_sdk_lines_before_the_disclaimer(self):
        summary = risk_summary(sdk_row())
        disclaimer = "Class probabilities describe the risk rubric, not incident likelihood."
        self.assertTrue(summary.endswith("\n\n" + disclaimer + "\n"))
        lines = summary.split("\n")
        bullet = next(i for i, line in enumerate(lines) if line.startswith("- R4"))
        self.assertEqual((lines[bullet - 2], lines[bullet - 1], lines[bullet + 1]),
                         ("", "Rules that block low:", ""))
        self.assertEqual(lines[lines.index("Model label: `low`") - 1], "")
        self.assertLess(summary.index("Mechanisms:"), summary.index(disclaimer))
        self.assertNotIn("\n\n\n", summary)
        no_mechanisms = sdk_row()
        del no_mechanisms["mechanisms"]
        self.assertNotIn("\n\n\n", risk_summary(no_mechanisms))

    def test_empty_sanitised_path_is_named(self):
        self.assertIn("`(unnamed path)`", risk_summary(sdk_row(files=["``\n"])))

    def test_no_raise_sentence_when_labels_match_and_none_touched(self):
        row = sdk_row(model="medium", final="medium")
        row["sdk"]["families_touched"] = []
        summary = risk_summary(row)
        self.assertNotIn("Raised from", summary)
        self.assertIn("Families touched: none", summary)

    def test_r7_uses_the_threshold_constant(self):
        summary = risk_summary(sdk_row(rule="R7", files=[]))
        self.assertIn(f"{LOW_PROBABILITY_THRESHOLD:.2f}", summary)

    def test_seven_files_show_five_and_more(self):
        summary = risk_summary(sdk_row(files=[f"src/f{i}.kt" for i in range(7)]))
        for i in range(5):
            self.assertIn(f"`src/f{i}.kt`", summary)
        self.assertNotIn("src/f5.kt", summary)
        self.assertIn("and 2 more", summary)

    def test_hostile_path_is_inert(self):
        plain = risk_summary(sdk_row(files=["a.kt"]))
        hostile = risk_summary(sdk_row(files=["a`b\n@team [x](http://e)\r z"]))
        self.assertEqual(plain.count("\n"), hostile.count("\n"))
        self.assertIn("`ab@team [x](http://e)z`", hostile)
        long = risk_summary(sdk_row(files=["x" * 500]))
        self.assertIn("`" + "x" * 200 + "`", long)
        self.assertNotIn("x" * 201, long)

    def test_fired_rules_on_a_medium_row_have_a_heading(self):
        summary = risk_summary(sdk_row(model="medium", final="medium", rule="R3", files=["new/x.kt"]))
        self.assertNotIn("Raised from", summary)
        self.assertIn("\nRules that block low:\n- R3", summary)

    def test_no_heading_without_fired_rules(self):
        row = sdk_row(model="medium", final="medium")
        row["low_blocked_by"] = []
        self.assertNotIn("Rules that block low", risk_summary(row))

    def test_malformed_mechanisms_are_skipped(self):
        for mechanisms in ([0.1, 0.2], "m_data", {"m_data": "high"}, {"m_data": None}, {"m_data": True}):
            with self.subTest(mechanisms=mechanisms):
                summary = risk_summary(sdk_row(mechanisms=mechanisms))
                self.assertNotIn("Mechanisms:", summary)

    def test_failed_row_with_sdk_only_does_not_crash(self):
        row = {"status": "timeout", "risk_label": None, "sdk": {"families_touched": ["server"]}}
        self.assertIn("Families touched: server", risk_summary(row))
        self.assertIsInstance(risk_summary({"status": "x", "sdk": {}}), str)


if __name__ == "__main__":
    unittest.main()
