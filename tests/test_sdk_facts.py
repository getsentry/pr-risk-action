from pathlib import Path
import tempfile
import unittest

from risk_pr_agent import sdk_facts
from risk_pr_agent.sdk_facts import FactSheetError, load_fact_sheet, matches, tag_path

ROOT = Path(__file__).parent / "fixtures" / "sdk"


class PatternTest(unittest.TestCase):
    def test_semantics(self):
        cases = [
            ("core/**", "core/a/b.kt", True), ("core/**", "corex/a.kt", False),
            ("android*/**", "android-ndk/x.c", True), ("android*/**", "android/x.c", True),
            ("**/*.md", "README.md", True), ("**/*.md", "a/b/README.md", True),
            ("**/api/*.api", "core/api/core.api", True), ("**/api/*.api", "core/api/x/core.api", False),
            ("*.md", "a/README.md", False), ("a.b", "aXb", False),
        ]
        for pattern, path, expected in cases:
            self.assertEqual(matches(pattern, path), expected, (pattern, path))


class FactSheetTest(unittest.TestCase):
    def sheet(self):
        return load_fact_sheet("org/project", ROOT)

    def test_missing_sheet_is_none(self):
        self.assertIsNone(load_fact_sheet("org/other", ROOT))

    def test_slug_is_case_insensitive(self):
        self.assertEqual(load_fact_sheet("Org/Project", ROOT)["hash"], self.sheet()["hash"])

    def test_shipping_code_gets_families(self):
        self.assertEqual(tag_path(self.sheet(), "core/src/main/A.kt"),
                         {"families": ["mobile-native", "server"], "shared_core": True, "kinds": [], "unmapped": False})

    def test_non_shipping_kind_clears_families(self):
        self.assertEqual(tag_path(self.sheet(), "core/src/test/ATest.kt"),
                         {"families": [], "shared_core": False, "kinds": ["tests"], "unmapped": False})

    def test_blocking_kind_keeps_families_and_all_kinds_apply(self):
        self.assertEqual(tag_path(self.sheet(), "samples/app/build.gradle.kts")["kinds"], ["build-release", "samples"])
        self.assertEqual(tag_path(self.sheet(), "core/api/core.api"),
                         {"families": ["mobile-native", "server"], "shared_core": True,
                          "kinds": ["public-api-record"], "unmapped": False})

    def test_defaults_are_merged_and_unmapped_detected(self):
        self.assertEqual(tag_path(self.sheet(), ".github/workflows/ci.yml")["kinds"], ["build-release"])
        self.assertTrue(tag_path(self.sheet(), "newmodule/src/A.kt")["unmapped"])

    def test_sheet_shape(self):
        sheet = self.sheet()
        self.assertEqual(sheet["repo"], "org/project")
        self.assertEqual(len(sheet["hash"]), 64)
        self.assertEqual(sheet["areas"][0], {"paths": ["core/**"], "families": ["server", "mobile-native"], "shared_core": True})
        self.assertIn({"paths": [".cursor/**"], "kind": "dev-tooling"}, sheet["kinds"])

    def test_invalid_sheets_raise(self):
        bad = [
            '[[area]]\npaths = ["a/**"]\nfamilies = ["desktop"]\n',          # unknown family
            '[[area]]\npaths = []\nfamilies = ["server"]\n',                 # empty paths
            '[[kind]]\npaths = ["a/**"]\nkind = "docs"\nnote = "be nice"\n', # unknown key
            '[[kind]]\npaths = ["a/**"]\nkind = "internal"\n',               # unknown kind
            'title = "x"\n',                                                 # unknown top-level key
            '[[area]\n',                                                     # syntax error
        ]
        for text in bad:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "repos").mkdir()
                (root / "defaults.toml").write_text("")
                (root / "repos" / "org__project.toml").write_text(text)
                with self.assertRaises(FactSheetError, msg=text):
                    load_fact_sheet("org/project", root)

    def test_invalid_defaults_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "repos").mkdir()
            (root / "defaults.toml").write_text('[[area]]\npaths = ["a"]\nfamilies = ["server"]\n')
            (root / "repos" / "org__project.toml").write_text("")
            with self.assertRaises(FactSheetError):
                load_fact_sheet("org/project", root)

    def test_unusable_repo_slug_has_no_fact_sheet(self):
        for repo in ("../x", "../x/y", "not a slug", "org/..", "org/project/extra", ""):
            with self.subTest(repo=repo):
                self.assertIsNone(load_fact_sheet(repo, ROOT))


class ShippedSheetsTest(unittest.TestCase):
    def test_every_shipped_sheet_loads(self):
        repos = sdk_facts.SDK_ROOT / "repos"
        for path in sorted(repos.glob("*.toml")):
            slug = path.stem.replace("__", "/", 1)
            with self.subTest(slug=slug):
                self.assertIsNotNone(load_fact_sheet(slug))

    def test_real_defaults_load(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "repos").mkdir()
            (root / "defaults.toml").write_text((sdk_facts.SDK_ROOT / "defaults.toml").read_text())
            (root / "repos" / "a__b.toml").write_text("")
            sheet = load_fact_sheet("a/b", root)
            self.assertEqual(tag_path(sheet, "docs/x.md")["kinds"], ["docs"])
            self.assertEqual(tag_path(sheet, "pkg/package.json")["kinds"], ["dependencies"])


if __name__ == "__main__":
    unittest.main()
