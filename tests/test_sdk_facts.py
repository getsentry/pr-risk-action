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



class RealSheetSpotCheckTest(unittest.TestCase):
    """Spot checks against the shipped sheets, at the commits recorded in each sheet."""

    def tags(self, repo, path):
        return tag_path(load_fact_sheet(f"getsentry/{repo}"), path)

    def assert_families(self, repo, path, families, shared_core=False):
        tags = self.tags(repo, path)
        self.assertEqual(tags["families"], families, path)
        self.assertEqual(tags["shared_core"], shared_core, path)
        self.assertFalse(tags["unmapped"], path)

    def assert_has_kind(self, repo, path, kind):
        self.assertIn(kind, self.tags(repo, path)["kinds"], path)

    def assert_tests_only(self, repo, path):
        self.assertEqual(self.tags(repo, path),
                         {"families": [], "shared_core": False, "kinds": ["tests"], "unmapped": False})

    def test_sentry_java(self):
        self.assert_families("sentry-java", "sentry/src/main/java/io/sentry/Sentry.java",
                             ["mobile-native", "server"], shared_core=True)
        self.assert_families("sentry-java", "sentry-android-core/src/main/java/io/sentry/android/core/X.java",
                             ["mobile-native"])
        self.assert_families("sentry-java", "sentry-servlet/src/main/java/io/sentry/servlet/X.java", ["server"])
        self.assert_has_kind("sentry-java", "sentry/api/sentry.api", "public-api-record")
        self.assert_has_kind("sentry-java", "sentry-compose/api/android/sentry-compose.api", "public-api-record")
        self.assert_has_kind("sentry-java", "sentry-servlet/build.gradle.kts", "build-release")
        self.assert_has_kind("sentry-java", "gradle/libs.versions.toml", "dependencies")
        self.assert_tests_only("sentry-java", "sentry-servlet/src/test/kotlin/X.kt")
        test_support = self.tags("sentry-java", "sentry-test-support/src/main/kotlin/io/sentry/test/X.kt")
        self.assertEqual(test_support["kinds"], [])
        self.assertTrue(test_support["families"])

    def test_sentry_java_6625_fires_r5_and_r6(self):
        from risk_pr_agent.sdk_rules import evaluate_rules

        sheet = load_fact_sheet("getsentry/sentry-java")
        paths = ["gradle/libs.versions.toml", "sentry-servlet/build.gradle.kts",
                 "sentry-servlet/src/test/kotlin/io/sentry/servlet/SentryServletRequestListenerTest.kt"]
        row = {"risk_label": "low", "probabilities": {"low": 0.99}, "context_stage": "bounded_diff",
               "diff_truncated": False, "omitted": [],
               "sdk": {"files": [{"path": path, **tag_path(sheet, path)} for path in paths]}}
        fired = {entry["rule"]: entry["files"] for entry in evaluate_rules(row)}
        self.assertEqual(set(fired), {"R5", "R6"})
        self.assertEqual(fired["R5"], ["gradle/libs.versions.toml"])
        self.assertIn("sentry-servlet/build.gradle.kts", fired["R6"])

    def test_sentry_javascript(self):
        self.assert_families("sentry-javascript", "packages/core/src/index.ts", ["browser", "server"], shared_core=True)
        self.assert_families("sentry-javascript", "packages/nextjs/src/index.types.ts", ["browser", "server"])
        self.assert_families("sentry-javascript", "packages/browser/src/index.ts", ["browser"])
        self.assert_families("sentry-javascript", "packages/node/src/index.ts", ["server"])
        self.assert_families("sentry-javascript", "packages/vite-plugin/src/index.ts", ["tooling"])
        self.assert_has_kind("sentry-javascript", "packages/browser/package.json", "dependencies")
        license_kinds = self.tags("sentry-javascript", "packages/browser/LICENSE")["kinds"]
        self.assertIn("build-release", license_kinds)
        self.assertNotIn("docs", license_kinds)
        self.assert_has_kind("sentry-javascript", "dev-packages/rollup-utils/npmHelpers.mjs", "build-release")
        self.assert_tests_only("sentry-javascript", "packages/core/test/lib/x.test.ts")

    def test_sentry_python(self):
        self.assert_families("sentry-python", "sentry_sdk/integrations/django/__init__.py", ["server"])
        self.assert_families("sentry-python", "sentry_sdk/client.py", ["server"], shared_core=True)
        self.assert_tests_only("sentry-python", "tests/test_client.py")

    def test_sentry_dotnet(self):
        self.assert_has_kind("sentry-dotnet", "modules/sentry-cocoa", "dependencies")
        self.assertIn("hybrid", self.tags("sentry-dotnet", "src/Sentry.Maui/SentryMauiAppBuilderExtensions.cs")["families"])
        self.assertEqual(self.tags("sentry-dotnet", "src/Sentry/SentrySdk.cs")["shared_core"], True)
        self.assert_families("sentry-dotnet", "src/Sentry.AspNetCore/SentryMiddleware.cs", ["server"])
        self.assert_has_kind("sentry-dotnet", "src/.editorconfig", "build-release")
        self.assert_has_kind("sentry-dotnet", "test/Sentry.Tests/ApiApprovalTests.Run.DotNet8_0.verified.txt",
                             "public-api-record")

    def test_sentry_cocoa(self):
        self.assert_families("sentry-cocoa", "Sources/Swift/Helper/SentrySDK.swift", ["mobile-native"])
        for record in ("sdk_api.json", "sdk_api_objc.json", "sdk_api_v10.json", "sdk_api_sentryswiftui.json"):
            self.assert_has_kind("sentry-cocoa", record, "public-api-record")
        self.assert_has_kind("sentry-cocoa", "Package.swift", "build-release")
        self.assert_has_kind("sentry-cocoa", "Sources/SentryCrash/Recording/SentryCrashC.c", "dependencies")
        self.assert_tests_only("sentry-cocoa", "Tests/SentryTests/SentrySDKTests.swift")

    def test_sentry_react_native(self):
        self.assertIn("hybrid", self.tags("sentry-react-native", "packages/core/src/js/index.ts")["families"])
        self.assertIn("hybrid", self.tags("sentry-react-native", "packages/core/ios/RNSentry.mm")["families"])
        self.assertEqual(self.tags("sentry-react-native", "packages/core/metro.js")["families"], ["hybrid", "tooling"])
        self.assert_has_kind("sentry-react-native", "packages/core/RNSentry.podspec", "dependencies")
        self.assert_has_kind("sentry-react-native", "packages/core/android/build.gradle", "dependencies")
        self.assert_has_kind("sentry-react-native", "packages/core/etc/sentry-react-native.api.md", "public-api-record")
        self.assert_tests_only("sentry-react-native", "packages/core/test/x.test.ts")


if __name__ == "__main__":
    unittest.main()
