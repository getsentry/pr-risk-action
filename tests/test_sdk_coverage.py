import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from risk_pr_agent import sdk_facts
from risk_pr_agent.commands import main

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sdk"


def run_coverage(repo, stdin):
    out = io.StringIO()
    with patch.object(sdk_facts, "SDK_ROOT", FIXTURE_ROOT), patch("sys.stdin", io.StringIO(stdin)), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main(["sdk-coverage", "--repo", repo])
    return code, out.getvalue()


class SdkCoverageTest(unittest.TestCase):
    def test_reports_counts_and_unmapped_paths(self):
        code, output = run_coverage("org/project", "core/src/main/A.kt\nnewmodule/C.kt\nREADME.md\n")
        report = json.loads(output)
        self.assertEqual(code, 1)
        self.assertEqual(report["total"], 3)
        self.assertEqual(report["unmapped"], ["newmodule/C.kt"])
        self.assertEqual(report["by_kind"], {"docs": 1})
        self.assertEqual(report["by_family"], {"mobile-native": 1, "server": 1})
        self.assertEqual(report["shared_core"], 1)

    def test_fully_mapped_input_exits_zero_and_ignores_blank_lines(self):
        code, output = run_coverage("org/project", "core/src/main/A.kt\n\nREADME.md\n")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["total"], 2)

    def test_repo_without_fact_sheet_exits_two(self):
        code, output = run_coverage("org/other", "a.py\n")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
