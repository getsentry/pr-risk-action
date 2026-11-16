import unittest

from risk_pr_agent.jev import _normalize_attempt

PRICE = {"input_usd_per_token": 0.0, "output_usd_per_token": 0.0}


def response(**extra):
    return {"status": "ok", "risk_label": "low", "probabilities": {"low": 0.95, "medium": 0.04, "high": 0.01},
            "usage": {"input_tokens": 1, "output_tokens": 1}, **extra}


class MechanismNormalizationTests(unittest.TestCase):
    def test_valid_mechanisms_are_preserved(self):
        attempt = _normalize_attempt(response(mechanisms={"m_stability": 0.07}), 1.0, PRICE)
        self.assertEqual(attempt["status"], "ok")
        self.assertEqual(attempt["mechanisms"], {"m_stability": 0.07})

    def test_invalid_mechanisms_make_the_attempt_invalid(self):
        for bad in ({"m_stability": 1.5}, {"m_stability": "high"}, []):
            with self.subTest(bad=bad):
                attempt = _normalize_attempt(response(mechanisms=bad), 1.0, PRICE)
                self.assertEqual(attempt["status"], "invalid_response")
                self.assertIsNone(attempt["risk_label"])

    def test_absent_mechanisms_leave_the_result_unchanged(self):
        attempt = _normalize_attempt(response(), 1.0, PRICE)
        self.assertEqual(attempt["status"], "ok")
        self.assertNotIn("mechanisms", attempt)
