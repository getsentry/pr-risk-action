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
            '[[area]]\npaths = ["a/**"]\nfamilies = ["server"]\nshared_core = "yes"\n',  # non-bool shared_core
            '[[area]]\npaths = ["a/**"]\nfamilies = []\n',                    # empty families
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
            self.assertEqual(tag_path(sheet, ".gitattributes")["kinds"], ["build-release"])
            self.assertEqual(tag_path(sheet, "pkg/.gitattributes")["kinds"], ["build-release"])
            for path in ("mix.exs", "pkg/mix.lock", "gemfiles/rails-7.gemfile.lock", "pkg/gemfiles/rails-7.gemfile"):
                self.assertEqual(tag_path(sheet, path)["kinds"], ["dependencies"], path)
            self.assertEqual(tag_path(sheet, "pkg/README.MD")["kinds"], ["docs"])
            self.assertEqual(tag_path(sheet, "docker/linux.dockerfile")["kinds"], ["build-release"])
            # Roslyn analyzer release-tracking files are compiler inputs: the extra kind keeps them blocking.
            self.assertEqual(tag_path(sheet, "src/X.Analyzers/AnalyzerReleases.Shipped.md")["kinds"],
                             ["docs", "public-api-record"])



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
        self.assertEqual(self.tags("sentry-python", ".python-version")["kinds"], ["build-release"])

    def test_sentry_dotnet(self):
        self.assert_has_kind("sentry-dotnet", "modules/sentry-cocoa", "dependencies")
        self.assertIn("hybrid", self.tags("sentry-dotnet", "src/Sentry.Maui/SentryMauiAppBuilderExtensions.cs")["families"])
        self.assertEqual(self.tags("sentry-dotnet", "src/Sentry/SentrySdk.cs")["shared_core"], True)
        self.assert_families("sentry-dotnet", "src/Sentry.AspNetCore/SentryMiddleware.cs", ["server"])
        self.assert_has_kind("sentry-dotnet", "src/.editorconfig", "build-release")
        self.assert_has_kind("sentry-dotnet", "test/Sentry.Tests/ApiApprovalTests.Run.DotNet8_0.verified.txt",
                             "public-api-record")
        self.assert_has_kind("sentry-dotnet", "src/Sentry.Analyzers/AnalyzerReleases.Shipped.md", "public-api-record")

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


# (repo, path, families, shared_core, kinds) for the sheets added in the wider rollout, taken from
# the sheet authors' spot checks. Every path exists at the commit recorded on its sheet's first line.
SPOT_CHECKS = [
    ("sentry-go", "client.go", "server", True, ""),
    ("sentry-go", "gin/sentrygin.go", "server", False, ""),
    ("sentry-go", "mock_transport.go", "server", True, ""),
    ("sentry-go", "internal/testutils/mocks.go", "server", True, ""),
    ("sentry-go", "internal/testutils/maps_test.go", "", False, "tests"),
    ("sentry-go", "_examples/basic/main.go", "", False, "samples"),
    ("sentry-go", "go.mod", "server", True, "dependencies"),

    ("sentry-ruby", "sentry-ruby/lib/sentry/client.rb", "server", True, ""),
    ("sentry-ruby", "sentry-ruby/lib/sentry/test_helper.rb", "server", True, ""),
    ("sentry-ruby", "sentry-rails/app/jobs/sentry/send_event_job.rb", "server", False, ""),
    ("sentry-ruby", "sentry-sidekiq/spec/spec_helper.rb", "", False, "tests"),
    ("sentry-ruby", "lib/sentry/test/rake_tasks.rb", "", False, "tests"),
    ("sentry-ruby", "sentry-rails/gemfiles/ruby-2.7_rails-6.1.0.gemfile.lock", "server", False, "dependencies"),

    ("sentry-rust", "sentry-core/src/hub.rs", "server", True, ""),
    ("sentry-rust", "sentry-minidump/src/lib.rs", "mobile-native server", False, ""),
    ("sentry-rust", "sentry-contexts/build.rs", "server", True, ""),
    ("sentry-rust", "sentry/src/transports/embedded_svc_http.rs", "mobile-native server", True, ""),
    ("sentry-rust", "sentry/examples/panic-demo.rs", "", False, "samples"),
    ("sentry-rust", "sentry-core/tests/metrics.rs", "", False, "tests"),

    ("sentry-elixir", "lib/sentry/client.ex", "server", True, ""),
    ("sentry-elixir", "lib/mix/tasks/sentry.install.ex", "tooling", False, ""),
    ("sentry-elixir", "lib/sentry/integrations/oban/cron.ex", "server", False, ""),
    ("sentry-elixir", "mix.exs", "server", True, "dependencies"),
    ("sentry-elixir", "test/sentry_test.exs", "", False, "tests"),
    ("sentry-elixir", "config/config.exs", "", False, "build-release"),

    ("sentry-powershell", "modules/Sentry/public/Start-Sentry.ps1", "hybrid server", True, ""),
    ("sentry-powershell", "modules/Sentry/Sentry.psd1", "hybrid server", True, "public-api-record"),
    ("sentry-powershell", "modules/Sentry/assemblies-loader.ps1", "hybrid server", True, ""),
    ("sentry-powershell", "dependencies/Sentry.properties", "hybrid", False, "dependencies"),
    ("sentry-powershell", "tests/init.tests.ps1", "", False, "tests"),
    ("sentry-powershell", "samples/send-logs.ps1", "", False, "samples"),

    ("sentry-php", "src/Client.php", "server", True, ""),
    ("sentry-php", "src/Monolog/Handler.php", "server", False, ""),
    ("sentry-php", "tests/ClientTest.php", "", False, "tests"),
    ("sentry-php", "stubs/autoload.php", "", False, "dev-tooling"),
    ("sentry-php", "scripts/bump-version.sh", "", False, "build-release"),
    ("sentry-php", ".gitattributes", "", False, "build-release"),

    ("sentry-laravel", "config/sentry.php", "server", False, ""),
    ("sentry-laravel", "src/Sentry/Laravel/Console/PublishCommand.php", "server tooling", False, ""),
    ("sentry-laravel", "src/Sentry/Laravel/EventHandler.php", "server", False, ""),
    ("sentry-laravel", "test/Sentry/EventHandlerTest.php", "", False, "tests"),
    ("sentry-laravel", "composer.json", "server", True, "dependencies"),
    ("sentry-laravel", "scripts/versionbump.php", "", False, "build-release"),

    ("sentry-symfony", "src/Resources/config/services.yaml", "server", False, ""),
    ("sentry-symfony", "src/DependencyInjection/Configuration.php", "server", False, ""),
    ("sentry-symfony", "tests/End2End/App/Kernel.php", "", False, "tests"),
    ("sentry-symfony", "composer.json", "server", True, "dependencies"),
    ("sentry-symfony", "psalm.xml", "", False, "build-release"),
    ("sentry-symfony", "psalm-baseline.xml", "", False, "build-release"),
    ("sentry-symfony", "AUTHORS", "", False, "build-release"),

    ("sentry-native", "external/crashpad", "mobile-native", False, "dependencies"),
    ("sentry-native", "include/sentry.h", "mobile-native", True, "public-api-record"),
    ("sentry-native", "src/backends/sentry_backend_inproc.c", "mobile-native", False, ""),
    ("sentry-native", "vendor/libunwind/src/x86_64/Gstep.c", "mobile-native", False, "dependencies"),
    ("sentry-native", "ndk/lib/src/main/jni/sentry.c", "hybrid mobile-native", False, ""),
    ("sentry-native", "tests/unit/test_basic.c", "", False, "tests"),
    ("sentry-native", "examples/crash.c", "", False, "samples"),

    ("sentry-dart", "packages/dart/lib/src/hub.dart", "browser hybrid server", True, ""),
    ("sentry-dart", "packages/flutter/sentry-native/CMakeCache.txt", "hybrid", False, "dependencies"),
    ("sentry-dart", "packages/flutter/lib/src/web/sentry_js_sdk_version.dart", "browser hybrid", False, "dependencies"),
    ("sentry-dart", "packages/flutter/android/build.gradle", "hybrid tooling", False, "build-release dependencies"),
    ("sentry-dart", "packages/sqflite/lib/src/sentry_database.dart", "hybrid", False, ""),
    ("sentry-dart", "packages/flutter/example/lib/main.dart", "", False, "samples"),

    ("sentry-capacitor", "src/sdk.ts", "browser hybrid", False, ""),
    ("sentry-capacitor", "android/build.gradle", "hybrid", False, "build-release dependencies"),
    ("sentry-capacitor", "Package.swift", "hybrid", False, "dependencies"),
    ("sentry-capacitor", "scripts/check-siblings.js", "hybrid tooling", False, "build-release"),
    ("sentry-capacitor", "android/src/main/java/io/sentry/capacitor/SentryCapacitor.java", "hybrid", False, ""),
    ("sentry-capacitor", "test/sdk.test.ts", "", False, "tests"),

    ("sentry-electron", "src/main/integrations/sentry-minidump/index.ts", "hybrid mobile-native server", False, ""),
    ("sentry-electron", "src/renderer/sdk.ts", "browser hybrid", False, ""),
    ("sentry-electron", "src/common/ipc.ts", "browser hybrid server", True, ""),
    ("sentry-electron", "package.json", "browser hybrid mobile-native server", True, "dependencies"),
    ("sentry-electron", "test/e2e/versions.json", "", False, "tests"),
    ("sentry-electron", "rollup.config.mjs", "", False, "build-release"),

    ("sentry-lynx", "packages/lynx-react/src/sdk.ts", "browser hybrid", False, ""),
    ("sentry-lynx", "packages/lynx-plugin/src/index.ts", "tooling", False, ""),
    ("sentry-lynx", "packages/lynx-plugin/package.json", "tooling", False, "dependencies"),
    ("sentry-lynx", "samples/react/src/App.tsx", "", False, "samples"),
    ("sentry-lynx", "packages/lynx-react/test/lib/sdk.test.ts", "", False, "tests"),

    ("sentry-kotlin-multiplatform", "buildSrc/src/main/java/Config.kt", "hybrid mobile-native server", True, "build-release dependencies"),
    ("sentry-kotlin-multiplatform", "sentry-kotlin-multiplatform/src/nativeInterop/cinterop/SentryInternal/SentryHub.h", "hybrid mobile-native", False, "dependencies"),
    ("sentry-kotlin-multiplatform", "sentry-kotlin-multiplatform/api/jvm/sentry-kotlin-multiplatform.api", "", False, "public-api-record"),
    ("sentry-kotlin-multiplatform", "sentry-kotlin-multiplatform-gradle-plugin/src/main/java/io/sentry/kotlin/multiplatform/gradle/SentryPlugin.kt", "hybrid tooling", False, ""),
    ("sentry-kotlin-multiplatform", "sentry-kotlin-multiplatform/build.gradle.kts", "hybrid mobile-native server", True, "build-release"),
    ("sentry-kotlin-multiplatform", "sentry-kotlin-multiplatform/src/commonTest/kotlin/io/sentry/kotlin/multiplatform/SentryIntegrationTest.kt", "", False, "tests"),

    ("sentry-unity", "modules/sentry-native", "hybrid mobile-native", False, "dependencies"),
    ("sentry-unity", "src/sentry-dotnet", "browser hybrid mobile-native", True, "dependencies"),
    ("sentry-unity", "samples/unity-of-bugs/Assets/Scripts/BugFarmButtons.cs", "hybrid", False, ""),
    ("sentry-unity", "src/Sentry.Unity.Editor/Android/DebugSymbolUpload.cs", "hybrid tooling", False, ""),
    ("sentry-unity", "package-dev/Runtime/Sentry.Unity.Native.PlayStation.dll.meta", "hybrid", False, "build-release"),
    ("sentry-unity", "src/Sentry.Unity/SentryUnityOptions.cs", "browser hybrid mobile-native", True, ""),
    ("sentry-unity", "samples/unity-of-bugs/ProjectSettings/ProjectVersion.txt", "", False, "build-release samples"),

    ("sentry-unreal", "modules/sentry-native", "hybrid mobile-native", False, "dependencies"),
    ("sentry-unreal", "modules/sentry-cocoa.properties", "hybrid mobile-native", False, "dependencies"),
    ("sentry-unreal", "plugin-dev/Source/Sentry/Private/Tests/SentryEvent.spec.cpp", "hybrid mobile-native", False, ""),
    ("sentry-unreal", "plugin-dev/Source/Sentry/Private/SentrySubsystem.cpp", "hybrid mobile-native", True, ""),
    ("sentry-unreal", "plugin-dev/Scripts/upload_debug_symbols.py", "hybrid tooling", False, ""),
    ("sentry-unreal", "plugin-dev/Source/Sentry/Sentry_Android_UPL.xml", "hybrid mobile-native tooling", False, "build-release dependencies"),
    ("sentry-unreal", "plugin-dev/Sentry.uplugin", "hybrid", False, "build-release"),

    ("sentry-godot", "modules/godot-cpp", "browser hybrid mobile-native", True, "dependencies"),
    ("sentry-godot", "project/test/util/json_assert", "", False, "dependencies tests"),
    ("sentry-godot", "doc_classes/SentrySDK.xml", "hybrid", False, "public-api-record"),
    ("sentry-godot", "src/editor/csproj_patcher.cpp", "hybrid mobile-native tooling", False, ""),
    ("sentry-godot", "src/sentry/sentry_sdk.cpp", "browser hybrid mobile-native", True, ""),
    ("sentry-godot", "project/addons/sentry/user_feedback/user_feedback_form.gd", "hybrid", False, ""),
    ("sentry-godot", "project/main.gd", "", False, "samples"),
]


# Manifests, lockfiles, version pins and submodule pointers that feed a shipped artifact. A
# dependency-only PR touching just these must still get a family example block.
DEPENDENCY_PATHS = {
    "sentry-capacitor": ["package.json", "yarn.lock"],
    "sentry-cocoa": ["Package.swift"],
    "sentry-dart": ["packages/dart/pubspec.yaml", "packages/flutter/pubspec.yaml"],
    "sentry-dotnet": ["Directory.Build.props", ".gitmodules", "modules/sentry-cocoa", "modules/sentry-native",
                      "modules/Ben.Demystifier", "modules/perfview"],
    "sentry-electron": ["package.json", "yarn.lock"],
    "sentry-elixir": ["mix.exs", "mix.lock", "mix-1.18.lock"],
    "sentry-go": ["go.mod", "go.sum"],
    "sentry-godot": [".gitmodules", "modules/sentry-native", "modules/godot-cpp", "modules/sentry-cocoa.properties",
                     "gradle/libs.versions.toml"],
    "sentry-java": ["gradle/libs.versions.toml"],
    "sentry-javascript": ["pnpm-lock.yaml"],
    "sentry-kotlin-multiplatform": ["buildSrc/src/main/java/Config.kt"],
    "sentry-laravel": ["composer.json"],
    "sentry-lynx": ["yarn.lock", "packages/lynx-react/package.json"],
    "sentry-native": [".gitmodules", "external/crashpad"],
    "sentry-php": ["composer.json"],
    "sentry-powershell": ["dependencies/Sentry.properties", "dependencies/download.ps1"],
    "sentry-python": ["setup.py", "pyproject.toml", "uv.lock"],
    "sentry-react-native": ["packages/core/package.json", "yarn.lock"],
    "sentry-ruby": ["sentry-ruby/sentry-ruby.gemspec"],
    "sentry-rust": ["Cargo.toml", "Cargo.lock"],
    "sentry-symfony": ["composer.json"],
    "sentry-unity": [".gitmodules", "src/sentry-dotnet", "modules/sentry-native", "modules/sentry-cli.properties",
                     "global.json"],
    "sentry-unreal": [".gitmodules", "modules/sentry-native", "modules/sentry-cocoa.properties"],
}


class DependencyManifestAreaTest(unittest.TestCase):
    def test_every_shipped_sheet_has_dependency_paths(self):
        shipped = sorted(p.stem.split("__", 1)[1] for p in (sdk_facts.SDK_ROOT / "repos").glob("getsentry__*.toml"))
        self.assertEqual(sorted(DEPENDENCY_PATHS), shipped)

    def test_dependency_paths_get_families(self):
        for repo, paths in DEPENDENCY_PATHS.items():
            sheet = load_fact_sheet(f"getsentry/{repo}")
            for path in paths:
                with self.subTest(repo=repo, path=path):
                    tags = tag_path(sheet, path)
                    self.assertIn("dependencies", tags["kinds"])
                    self.assertTrue(tags["families"])


# Shipped-source spot checks of the six earlier sheets; the rollout sheets' come from SPOT_CHECKS.
SHIPPED_SOURCE_PATHS = {
    "sentry-java": ["sentry/src/main/java/io/sentry/Sentry.java",
                    "sentry-android-core/src/main/java/io/sentry/android/core/X.java",
                    "sentry-servlet/src/main/java/io/sentry/servlet/X.java",
                    "sentry-test-support/src/main/kotlin/io/sentry/test/X.kt"],
    "sentry-javascript": ["packages/core/src/index.ts", "packages/nextjs/src/index.types.ts",
                          "packages/browser/src/index.ts", "packages/node/src/index.ts",
                          "packages/vite-plugin/src/index.ts"],
    "sentry-python": ["sentry_sdk/integrations/django/__init__.py", "sentry_sdk/client.py"],
    "sentry-dotnet": ["src/Sentry.Maui/SentryMauiAppBuilderExtensions.cs", "src/Sentry/SentrySdk.cs",
                      "src/Sentry.AspNetCore/SentryMiddleware.cs"],
    "sentry-cocoa": ["Sources/Swift/Helper/SentrySDK.swift"],
    "sentry-react-native": ["packages/core/src/js/index.ts", "packages/core/ios/RNSentry.mm",
                            "packages/core/metro.js"],
}
for _repo, _path, _families, _shared, _kinds in SPOT_CHECKS:
    if _families and not _kinds:
        SHIPPED_SOURCE_PATHS.setdefault(_repo, []).append(_path)

# Directory names an author could plant inside a shipping source tree to have a file read as non-shipping.
PLANTED_SEGMENTS = ("test", "tests", "src/test", "__tests__", "spec", "samples", "sample", "examples", "docs",
                    "testing", "fixtures")

# Variants that are real non-shipping locations in that repository, so tagging them non-shipping is right.
# Real non-shipping roots inside a shipping area: a planted variant under one of these is a real
# test or sample location in that repository, so tagging it non-shipping is right.
REAL_NON_SHIPPING_ROOTS = {
    # <module>/src/test is each Gradle module's real test source set.
    "sentry-java": ["*/src/test/**"],
    # The Gradle plugin module's real test source set.
    "sentry-kotlin-multiplatform": ["sentry-kotlin-multiplatform-gradle-plugin/src/test/**"],
    # ndk/lib/src/test is the NDK library's unit-test source set; ndk/sample is the sample app.
    "sentry-native": ["ndk/lib/src/test/**", "ndk/sample/**"],
    # packages/core/test is the package's Jest test root.
    "sentry-react-native": ["packages/core/test/**"],
    # packages/<name>/test is each package's Vitest/Jest test root.
    "sentry-javascript": ["packages/*/test/**"],
    "sentry-lynx": ["packages/*/test/**"],
    # packages/<name>/test is each Dart package's test root; pub never compiles it into a dependant.
    "sentry-dart": ["packages/*/test/**"],
    # sentry-<gem>/spec is each gem's RSpec root, excluded from the gemspec's files.
    "sentry-ruby": ["sentry-*/spec/**"],
    # <crate>/examples and <crate>/tests are Cargo targets that are never part of the library crate.
    "sentry-rust": ["*/examples/**", "*/tests/**"],
}


def _under_real_root(repo, path):
    return any(matches(root, path) for root in REAL_NON_SHIPPING_ROOTS.get(repo, ()))


def _area_root_depth(sheet, path):
    """Segments of the deepest matching area pattern before its first ``**``."""
    depths = []
    for area in sheet["areas"]:
        for pattern in area["paths"]:
            if matches(pattern, path):
                segments = pattern.split("/")
                depths.append(segments.index("**") if "**" in segments else len(segments) - 1)
    return max(depths)


def planted_variants(sheet, path):
    """``path`` with each planted segment inserted at every depth inside its shipping area."""
    parts = path.split("/")
    variants = set()
    for depth in range(_area_root_depth(sheet, path), len(parts)):
        for segment in PLANTED_SEGMENTS:
            variants.add("/".join(parts[:depth] + [segment] + parts[depth:]))
    return sorted(variants)


# File names an author could plant next to shipped source to have it read as a test, story or mock.
PLANTED_FILE_NAMES = ("evil_test.go", "evil.test.ts", "evil.spec.ts", "EvilTests.cs", "EvilTest.java",
                      "evil_test.dart", "test_evil.py", "evil_spec.rb", "conftest.py", "evil.stories.tsx",
                      "evil.mock.ts")

# Planted file names that follow a compiler-enforced test convention in that repository.
REAL_TEST_FILE_NAMES = {
    # The Go toolchain never compiles _test.go files into a non-test build.
    ("sentry-go", "evil_test.go"),
}


def planted_files(path):
    directory = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
    return [directory + name for name in PLANTED_FILE_NAMES]


class PlantedDirectoryTest(unittest.TestCase):
    """A test- or sample-named directory created inside shipping source must not drop its families."""

    def test_every_sheet_has_shipped_source_spot_checks(self):
        shipped = sorted(p.stem.split("__", 1)[1] for p in (sdk_facts.SDK_ROOT / "repos").glob("getsentry__*.toml"))
        self.assertEqual(sorted(SHIPPED_SOURCE_PATHS), shipped)

    def test_spot_checks_are_shipped_source(self):
        for repo, paths in SHIPPED_SOURCE_PATHS.items():
            sheet = load_fact_sheet(f"getsentry/{repo}")
            for path in paths:
                with self.subTest(repo=repo, path=path):
                    tags = tag_path(sheet, path)
                    self.assertTrue(tags["families"])
                    self.assertEqual(tags["kinds"], [])

    def test_planted_directories_keep_shipping_tags(self):
        for repo, paths in sorted(SHIPPED_SOURCE_PATHS.items()):
            sheet = load_fact_sheet(f"getsentry/{repo}")
            for path in paths:
                for variant in planted_variants(sheet, path):
                    if _under_real_root(repo, variant):
                        continue
                    with self.subTest(repo=repo, variant=variant):
                        kinds = tag_path(sheet, variant)["kinds"]
                        self.assertFalse(kinds and all(kind in sdk_facts.NON_SHIPPING_KINDS for kind in kinds),
                                         f"{variant} is tagged only {kinds}")

    def test_planted_file_names_keep_shipping_tags(self):
        for repo, paths in sorted(SHIPPED_SOURCE_PATHS.items()):
            sheet = load_fact_sheet(f"getsentry/{repo}")
            for path in paths:
                for planted in planted_files(path):
                    if (repo, planted.rsplit("/", 1)[-1]) in REAL_TEST_FILE_NAMES:
                        continue
                    with self.subTest(repo=repo, planted=planted):
                        kinds = tag_path(sheet, planted)["kinds"]
                        self.assertFalse(kinds and all(kind in sdk_facts.NON_SHIPPING_KINDS for kind in kinds),
                                         f"{planted} is tagged only {kinds}")

    def test_non_shipping_patterns_are_anchored(self):
        # Only file-name patterns may match at any depth: Go keeps tests beside sources (the
        # toolchain never compiles _test.go into a non-test build), and docs by extension.
        any_depth = {("sentry-go", "**/*_test.go"), ("defaults", "**/*.md"), ("defaults", "**/*.MD"),
                     ("defaults", "**/*.mdx")}
        sheets = [sdk_facts.SDK_ROOT / "defaults.toml"] + sorted((sdk_facts.SDK_ROOT / "repos").glob("getsentry__*.toml"))
        for path in sheets:
            repo = path.stem.split("__", 1)[1] if "__" in path.stem else path.stem
            for kind in sdk_facts._read_toml(path).get("kind", []):
                if kind["kind"] not in sdk_facts.NON_SHIPPING_KINDS:
                    continue
                for pattern in kind["paths"]:
                    if (repo, pattern) in any_depth:
                        continue
                    with self.subTest(repo=repo, pattern=pattern):
                        self.assertFalse(pattern.startswith("**") or "/**/" in pattern)

    def test_known_planted_paths_from_the_review(self):
        for repo, path in (("sentry-java", "sentry/src/main/java/io/sentry/src/test/Evil.java"),
                           ("sentry-javascript", "packages/core/src/test/evil.ts"),
                           ("sentry-javascript", "packages/core/src/utils/test/index.ts"),
                           ("sentry-react-native", "packages/core/android/src/main/java/io/sentry/src/test/Evil.java")):
            with self.subTest(repo=repo, path=path):
                tags = tag_path(load_fact_sheet(f"getsentry/{repo}"), path)
                self.assertNotIn("tests", tags["kinds"])
                self.assertTrue(tags["families"])


# Paths that run on a customer's or maintainer's machine, so they must block low.
CODE_RUNNING_PATHS = [".githooks/pre-commit", "hooks/pre-commit", ".husky/pre-commit", "lefthook.yml", ".mcp.json",
                      "pkg/.mcp.json", ".run/Tests.run.xml", ".zed/tasks.json", ".vscode/tasks.json",
                      ".vscode/launch.json"]


class ExecutedConfigurationTest(unittest.TestCase):
    def test_defaults_tag_code_running_configuration_build_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "repos").mkdir()
            (root / "defaults.toml").write_text((sdk_facts.SDK_ROOT / "defaults.toml").read_text())
            (root / "repos" / "a__b.toml").write_text("")
            sheet = load_fact_sheet("a/b", root)
            for path in CODE_RUNNING_PATHS:
                with self.subTest(path=path):
                    self.assertIn("build-release", tag_path(sheet, path)["kinds"])
            self.assertEqual(tag_path(sheet, ".vscode/settings.json")["kinds"], ["dev-tooling"])

    def test_sheets_do_not_call_code_running_configuration_dev_tooling(self):
        for path in sorted((sdk_facts.SDK_ROOT / "repos").glob("getsentry__*.toml")):
            for kind in sdk_facts._read_toml(path).get("kind", []):
                if kind["kind"] != "dev-tooling":
                    continue
                for candidate in CODE_RUNNING_PATHS + [".githooks/x/y", "hooks/x/y", ".run/x/y", ".zed/x/y"]:
                    with self.subTest(sheet=path.name, path=candidate):
                        self.assertFalse(any(matches(p, candidate) for p in kind["paths"]))


class FixWaveSpotCheckTest(unittest.TestCase):
    def tags(self, repo, path):
        return tag_path(load_fact_sheet(f"getsentry/{repo}"), path)

    def test_native_test_and_example_cmake_scripts_run_at_configure_time(self):
        for path in ("tests/unit/CMakeLists.txt", "tests/fixtures/crash_reporter/CMakeLists.txt",
                     "tests/benchmark/CMakeLists.txt", "examples/x/CMakeLists.txt", "tests/x/helper.cmake"):
            with self.subTest(path=path):
                self.assertIn("build-release", self.tags("sentry-native", path)["kinds"])
        self.assertEqual(self.tags("sentry-native", "tests/unit/test_basic.c")["kinds"], ["tests"])

    def test_react_native_android_unit_tests_ship_in_the_npm_package(self):
        tags = self.tags("sentry-react-native",
                         "packages/core/android/src/test/java/io/sentry/react/RNSentryCaptureEnvelopeTest.java")
        self.assertIn("hybrid", tags["families"])
        self.assertNotIn("tests", tags["kinds"])

    def test_go_internal_helpers_ship_but_their_tests_do_not(self):
        self.assertEqual(self.tags("sentry-go", "internal/sentrytest/fixture.go")["kinds"], [])
        self.assertEqual(self.tags("sentry-go", "internal/sentrytest/fixture_test.go")["kinds"], ["tests"])

    def test_dart_flutter_config_is_only_the_detekt_baseline(self):
        self.assertEqual(self.tags("sentry-dart", "packages/flutter/config/detekt-bl.xml")["kinds"], ["dev-tooling"])
        self.assertNotIn("dev-tooling", self.tags("sentry-dart", "packages/flutter/config/other.yaml")["kinds"])

    def test_published_authors_files_are_build_release(self):
        for repo, path in (("sentry-php", "AUTHORS"), ("sentry-dart", "packages/dart/AUTHORS")):
            with self.subTest(repo=repo):
                kinds = self.tags(repo, path)["kinds"]
                self.assertIn("build-release", kinds)
                self.assertNotIn("docs", kinds)

    def test_kotlin_multiplatform_md_files_are_docs_from_the_defaults(self):
        self.assertEqual(self.tags("sentry-kotlin-multiplatform", "AGENTS.MD")["kinds"], ["docs"])


class RolloutSheetSpotCheckTest(unittest.TestCase):
    def test_spot_checks(self):
        for repo, path, families, shared_core, kinds in SPOT_CHECKS:
            with self.subTest(repo=repo, path=path):
                self.assertEqual(tag_path(load_fact_sheet(f"getsentry/{repo}"), path),
                                 {"families": families.split(), "shared_core": shared_core,
                                  "kinds": kinds.split(), "unmapped": False})


if __name__ == "__main__":
    unittest.main()
