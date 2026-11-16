import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from risk_pr_agent import sdk_facts
from risk_pr_agent.commands import main

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sdk"


def run_coverage(repo, stdin, *options):
    out = io.StringIO()
    with patch.object(sdk_facts, "SDK_ROOT", FIXTURE_ROOT), patch("sys.stdin", io.StringIO(stdin)), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main(["sdk-coverage", "--repo", repo, *options])
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

    def test_bare_paths_report_no_submodule_check(self):
        code, output = run_coverage("org/project", "core/src/main/A.kt\n")
        self.assertEqual(code, 0)
        self.assertNotIn("submodules_without_dependencies", json.loads(output))

    def test_stage_input_flags_submodules_without_dependencies(self):
        stdin = ("100644 1111111111111111111111111111111111111111 0\tcore/src/main/A.kt\n"
                 "160000 2222222222222222222222222222222222222222 0\tcore/vendored\n"
                 "160000 3333333333333333333333333333333333333333 0\tweb/package.json\n"
                 "100644 4444444444444444444444444444444444444444 0\tdocs/with space.md\n")
        code, output = run_coverage("org/project", stdin, "--stage")
        report = json.loads(output)
        self.assertEqual(code, 1)
        self.assertEqual(report["total"], 4)
        self.assertEqual(report["unmapped"], [])
        self.assertEqual(report["submodules_without_dependencies"], ["core/vendored"])

    def test_stage_input_with_tagged_submodules_exits_zero(self):
        stdin = ("100644 1111111111111111111111111111111111111111 0\tcore/src/main/A.kt\n\n"
                 "160000 3333333333333333333333333333333333333333 0\tweb/package.json\n")
        code, output = run_coverage("org/project", stdin, "--stage")
        report = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["submodules_without_dependencies"], [])

    def test_stage_input_rejects_bare_paths(self):
        code, output = run_coverage("org/project", "core/src/main/A.kt\n", "--stage")
        self.assertEqual(code, 1)
        self.assertEqual(output, "")

    def test_repo_without_fact_sheet_exits_two(self):
        code, output = run_coverage("org/other", "a.py\n")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
