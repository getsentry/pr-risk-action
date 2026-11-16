import json
import tomllib
import unittest

import tiktoken

from risk_pr_agent.sdk_facts import FAMILIES, SDK_ROOT
from risk_pr_agent.sdk_rubric import MECHANISMS, build_questions, rubric_version


class RubricTest(unittest.TestCase):
    def test_question_shape(self):
        questions = build_questions(["server"], False)
        self.assertEqual(set(questions), {"risk", *MECHANISMS})
        self.assertEqual(questions["risk"]["type"], "choice")
        self.assertEqual(list(questions["risk"]["criteria"]), ["low", "medium", "high"])
        for name in MECHANISMS:
            self.assertEqual(set(questions[name]), {"type", "instructions"})
            self.assertEqual(questions[name]["type"], "boolean")

    def test_family_blocks_are_selected(self):
        data = tomllib.loads((SDK_ROOT / "rubric.toml").read_text())
        none = build_questions([], False)["risk"]["instructions"]
        both = build_questions(["server", "mobile-native"], True)["risk"]["instructions"]
        for family in FAMILIES:
            self.assertNotIn(data["families"][family], none)
        self.assertIn(data["families"]["server"], both)
        self.assertIn(data["families"]["mobile-native"], both)
        self.assertNotIn(data["families"]["browser"], both)
        self.assertIn(data["shared_core"], both)
        self.assertNotIn(data["shared_core"], none)

    def test_criteria_do_not_vary_with_families(self):
        self.assertEqual(build_questions([], False)["risk"]["criteria"],
                         build_questions(list(FAMILIES), True)["risk"]["criteria"])

    def test_text_rules(self):
        everything = json.dumps(build_questions(list(FAMILIES), True)).lower()
        for banned in ("title", "description", "rollback of a deploy", "migration"):
            self.assertNotIn(banned, everything)
        for required in ("untrusted", "truncat", "cannot be rolled back"):
            self.assertIn(required, everything)
        self.assertLess(len(tiktoken.get_encoding("cl100k_base").encode(
            json.dumps(build_questions(list(FAMILIES), True)["risk"]))), 1500)

    def test_unknown_family_raises(self):
        with self.assertRaises(ValueError):
            build_questions(["desktop"], False)

    def test_family_order_does_not_matter(self):
        self.assertEqual(build_questions(["server", "browser"], False),
                         build_questions(["browser", "server"], False))

    def test_mechanism_questions_carry_preamble(self):
        data = tomllib.loads((SDK_ROOT / "rubric.toml").read_text())
        questions = build_questions(["tooling"], False)
        for name in MECHANISMS:
            instructions = questions[name]["instructions"]
            self.assertTrue(instructions.startswith(data["mechanism_preamble"]), name)
            self.assertIn(data["mechanisms"][name], instructions)

    def test_rubric_version(self):
        self.assertEqual(rubric_version(), "1")


if __name__ == "__main__":
    unittest.main()
