import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BuildScriptContractTests(unittest.TestCase):
    def test_bootstrap_creates_the_local_build_environment_from_pinned_dependencies(self):
        source = (ROOT / "scripts/bootstrap.sh").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements-build.txt").read_text(encoding="utf-8")

        self.assertIn('python3', source)
        self.assertIn('-m venv "$VENV"', source)
        self.assertIn('pip==26.1.2', requirements)
        self.assertIn(
            '--no-deps --require-hashes --requirement "$REQUIREMENTS"', source
        )
        self.assertIn('pip check', source)
        self.assertIn('release_secret_scan.py', source)
        self.assertNotIn('curl ', source)
        self.assertNotIn('/Users/', source)
        for line in requirements.splitlines():
            if line.strip():
                self.assertRegex(
                    line,
                    r"^[A-Za-z0-9_.-]+==[0-9][A-Za-z0-9_.-]* "
                    r"--hash=sha256:[0-9a-f]{64}$",
                )

    def test_build_uses_local_native_toolchain_and_verifies_release(self):
        source = (ROOT / "scripts/build_app.sh").read_text(encoding="utf-8")

        self.assertIn("unittest discover", source)
        self.assertIn("swift test", source)
        self.assertIn("-c release", source)
        self.assertIn("-warnings-as-errors", source)
        self.assertIn("OpenUsageBar", source)
        self.assertIn("OpenUsageActivity", source)
        self.assertIn("codesign --verify --deep --strict", source)
        self.assertNotIn("curl ", source)
        self.assertNotIn("npm ", source)
        self.assertNotIn("https://", source)

    def test_build_enforces_coverage_and_privacy_gates(self):
        source = (ROOT / "scripts/build_app.sh").read_text(encoding="utf-8")

        self.assertIn("--enable-code-coverage", source)
        self.assertIn("llvm-cov report", source)
        self.assertIn("SWIFT_MIN_LINE_COVERAGE=80", source)
        self.assertIn("swift_product_line_coverage", source)
        self.assertIn("scripts/privacy_scan.py", source)
        self.assertIn("scripts/release_secret_scan.py", source)
        self.assertIn("Delete :PythonInfoDict:PythonExecutable", source)
        self.assertIn("provider-catalog.v1.json", source)
        self.assertIn("GeneratedProviderCatalog.swift", source)
        self.assertIn("generate_local_api_schema.py --output", source)
        self.assertIn("generate_swift_activity_schema.py --output", source)
        self.assertIn("GeneratedActivitySchema.swift", source)
        self.assertIn("local-api-v1.schema.json", source)
        self.assertIn("python_coverage_gate.py", source)
        self.assertIn("--module unittest discover -s tests -v", source)
        self.assertIn('--package-root "$ROOT/openusage_bar"', source)
        self.assertNotIn("PYTHON_TOUCHED_MODULES", source)
        self.assertIn('actual=${SWIFT_LINE_COVERAGE}%', source)

    def test_ci_and_release_pin_the_same_xcode_toolchain_as_local_release_validation(self):
        for name in ("ci.yml", "release.yml"):
            workflow = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
            self.assertIn("DEVELOPER_DIR: /Applications/Xcode_26.6.app/Contents/Developer", workflow)

    def test_build_runs_a_failure_propagating_python_suite_before_trace(self):
        source = (ROOT / "scripts/build_app.sh").read_text(encoding="utf-8")
        direct = '"$PYTHON" -m unittest discover -s tests -v'
        traced = '"$PYTHON" -m trace --count --summary --missing'

        self.assertIn(direct, source)
        self.assertLess(source.index(direct), source.index(traced))

    def test_build_rejects_a_stale_generated_swift_provider_catalog(self):
        source = (ROOT / "scripts/build_app.sh").read_text(encoding="utf-8")
        generated = ROOT / "swift_app/Sources/UsageCore/GeneratedProviderCatalog.swift"
        generator = ROOT / "scripts/generate_swift_provider_catalog.py"

        self.assertIn("generate_swift_provider_catalog.py --output", source)
        self.assertIn('cmp -s "$CATALOG_TMP"', source)
        self.assertIn("generated Swift provider catalog is stale", source)
        subprocess.run(
            [str(ROOT / ".build-venv/bin/python"), str(generator), "--check"],
            cwd=ROOT, check=True,
        )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "GeneratedProviderCatalog.swift"
            subprocess.run(
                [str(ROOT / ".build-venv/bin/python"), str(generator), "--output", str(output)],
                cwd=ROOT, check=True,
            )
            self.assertEqual(output.read_bytes(), generated.read_bytes())
            output.write_text("// stale\n", encoding="utf-8")
            stale = subprocess.run(
                [
                    str(ROOT / ".build-venv/bin/python"), str(generator),
                    "--check", "--output", str(output),
                ],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertNotEqual(stale.returncode, 0)
            self.assertIn("stale", stale.stderr)

    def test_build_signs_nested_helpers_before_main_bundle(self):
        source = (ROOT / "scripts/build_app.sh").read_text(encoding="utf-8")
        settings = source.index('codesign --force --deep --sign "$CODESIGN_IDENTITY" "$SETTINGS_APP"')
        activity = source.index('codesign --force --deep --sign "$CODESIGN_IDENTITY" "$ACTIVITY_APP"')
        main = source.index('codesign --force --deep --sign "$CODESIGN_IDENTITY" "$APP"')

        self.assertLess(settings, main)
        self.assertLess(activity, main)

    def test_plists_define_one_status_host_and_two_regular_helpers(self):
        resources = ROOT / "swift_app/Resources"
        with (resources / "OpenUsageBar-Info.plist").open("rb") as handle:
            main = plistlib.load(handle)
        with (resources / "OpenUsageActivity-Info.plist").open("rb") as handle:
            activity = plistlib.load(handle)
        with (resources / "OpenUsageProviderSettings-Info.plist").open("rb") as handle:
            settings = plistlib.load(handle)

        self.assertEqual(main["CFBundleIdentifier"], "com.lune.openusagebar")
        self.assertEqual(activity["CFBundleIdentifier"], "com.lune.openusagebar.activity")
        self.assertEqual(settings["CFBundleIdentifier"], "com.lune.openusagebar.settings")
        self.assertIs(main["LSUIElement"], True)
        self.assertNotIn("LSUIElement", activity)
        self.assertNotIn("LSUIElement", settings)
        for payload in (main, activity, settings):
            self.assertEqual(payload["LSMinimumSystemVersion"], "15.0")
            self.assertIs(payload["NSHighResolutionCapable"], True)

    def test_launch_agents_have_distinct_roles_and_no_duplicate_status_host(self):
        resources = ROOT / "swift_app/Resources"
        with (resources / "com.lune.openusagebar.plist").open("rb") as handle:
            status = plistlib.load(handle)
        with (resources / "com.lune.openusagebar.collector.plist").open("rb") as handle:
            collector = plistlib.load(handle)

        self.assertEqual(status["Label"], "com.lune.openusagebar")
        self.assertEqual(status["ProgramArguments"][0], "__APP__/Contents/MacOS/OpenUsage Bar")
        self.assertEqual(status["ProgramArguments"][-1], "--background")
        self.assertEqual(collector["Label"], "com.lune.openusagebar.collector")
        self.assertTrue(collector["ProgramArguments"][0].startswith("__APP__/"))
        self.assertIn("daemon", collector["ProgramArguments"])
        self.assertIn("--api-socket", collector["ProgramArguments"])
        self.assertNotEqual(status["ProgramArguments"][0], collector["ProgramArguments"][0])

    def test_install_is_backup_first_atomic_and_has_rollback(self):
        source = (ROOT / "scripts/install_app.sh").read_text(encoding="utf-8")
        transaction = (ROOT / "scripts/install_app_transaction.sh").read_text(encoding="utf-8")

        self.assertIn('BACKUP_ROOT="$STATE_DIR/backups/app"', source)
        self.assertIn("create_complete_app_backup", source)
        self.assertIn("ditto", source)
        self.assertIn(".new-$$", source)
        self.assertIn("rollback", source)
        self.assertIn("codesign --verify --deep --strict", source)
        self.assertIn('"$LAUNCHCTL" bootstrap', source)
        self.assertIn('ATOMIC_SWAP="$SOURCE/Contents/Resources/atomic-swap"', source)
        self.assertNotIn('mv "$TARGET" "$PREVIOUS"', source)
        self.assertIn('install_bundle_transaction "$ATOMIC_SWAP" "$TARGET" "$NEW"', source)
        self.assertIn('commit_bundle_transaction "$NEW"', source)
        commit = source.index('commit_bundle_transaction "$NEW"')
        trap_off = source.index('trap - EXIT INT TERM', source.index('codesign --verify --deep --strict "$TARGET"'))
        cleanup = source.index('cleanup_legacy_previous_bundles', trap_off)
        self.assertLess(trap_off, commit)
        self.assertLess(commit, cleanup)
        self.assertLess(trap_off, cleanup)
        finalized = source[source.index('codesign --verify --deep --strict "$TARGET"'):trap_off]
        self.assertIn('SWAPPED=0', finalized)
        self.assertIn('FIRST_INSTALLED=0', finalized)
        self.assertIn('commit_bundle_transaction "$NEW" ||', source)
        transaction = (ROOT / "scripts/install_app_transaction.sh").read_text(encoding="utf-8")
        self.assertIn('HAD_TARGET=1', transaction)
        self.assertIn('SWAPPED=1', transaction)
        self.assertIn('"$atomic_swap" "$target" "$staged"', transaction)
        self.assertIn('elif (( FIRST_INSTALLED ))', transaction)
        self.assertIn('mv "$target" "$failed"', transaction)
        self.assertIn('CFBundleIdentifier', transaction)
        self.assertIn('OpenUsage\\ Bar.app.previous-', transaction)
        self.assertIn('HEALTH_PROBE=${OPENUSAGE_HEALTH_PROBE:-}', source)
        self.assertIn('verify_local_api_contract "$SOCKET" "$HEALTH_PROBE"', source)
        self.assertIn("wait_for_health", source)
        self.assertIn("wait_for_socket_release", source)
        self.assertIn('/v1/$route', transaction)
        self.assertIn('plutil -extract schemaVersion', transaction)
        self.assertIn("/usr/libexec/PlistBuddy", source)
        self.assertNotIn("plutil -replace ProgramArguments.0", source)
        self.assertIn("rollback 1", source)

    def test_public_install_and_release_scripts_are_relocatable_and_data_safe(self):
        install = (ROOT / "scripts/install_app.sh").read_text(encoding="utf-8")
        location = (ROOT / "scripts/install_location.sh").read_text(encoding="utf-8")
        uninstall = (ROOT / "scripts/uninstall_app.sh").read_text(encoding="utf-8")
        package = (ROOT / "scripts/package_release.sh").read_text(encoding="utf-8")

        self.assertIn("OPENUSAGE_INSTALL_DIR", location)
        self.assertIn("resolve_openusage_install_dir", install)
        self.assertIn("reveal_openusage_install", install)
        self.assertIn("ProgramArguments.0", install)
        self.assertIn('cleanup_legacy_previous_bundles "$INSTALL_DIR"', install)
        self.assertIn("local data and Keychain items were preserved", uninstall)
        self.assertIn("--purge-data", uninstall)
        self.assertNotIn("security delete-generic-password", uninstall)
        self.assertIn("scripts/install_app.sh", package)
        self.assertIn("scripts/install_location.sh", package)
        self.assertIn("scripts/uninstall_app.sh", package)
        self.assertIn("scripts/rollback_app.sh", package)
        self.assertIn("scripts/export_diagnostics.py", package)
        self.assertIn("docs/canary.md", package)
        self.assertIn("THIRD_PARTY_NOTICES.md", package)
        self.assertIn("shasum -a 256", package)

    def test_atomic_swap_helper_exchanges_two_directories_without_a_missing_target_window(self):
        helper = ROOT / "scripts/atomic_swap.c"
        self.assertTrue(helper.is_file())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / "atomic-swap"
            subprocess.run(
                ["/usr/bin/clang", "-Wall", "-Wextra", "-Werror", str(helper), "-o", str(binary)],
                check=True,
            )
            current = root / "current.app"
            staged = root / "current.app.new"
            current.mkdir()
            staged.mkdir()
            (current / "version").write_text("old", encoding="utf-8")
            (staged / "version").write_text("new", encoding="utf-8")

            subprocess.run([str(binary), str(current), str(staged)], check=True)

            self.assertEqual((current / "version").read_text(encoding="utf-8"), "new")
            self.assertEqual((staged / "version").read_text(encoding="utf-8"), "old")
            missing = root / "missing.app"
            failed = subprocess.run([str(binary), str(current), str(missing)], capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual((current / "version").read_text(encoding="utf-8"), "new")

    def test_real_transaction_rolls_back_updates_and_failed_first_install(self):
        helper = ROOT / "scripts/atomic_swap.c"
        transaction = ROOT / "scripts/install_app_transaction.sh"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / "atomic-swap"
            subprocess.run(["/usr/bin/clang", "-Wall", "-Wextra", "-Werror", str(helper), "-o", str(binary)], check=True)
            for scenario in ("update", "first"):
                case = root / scenario
                case.mkdir()
                target = case / "OpenUsage Bar.app"
                staged = case / "OpenUsage Bar.app.new"
                failed = case / "failed-new.app"
                staged.mkdir()
                (staged / "version").write_text("new", encoding="utf-8")
                if scenario == "update":
                    target.mkdir()
                    (target / "version").write_text("old", encoding="utf-8")
                script = f'''source "{transaction}"
HAD_TARGET=0
SWAPPED=0
FIRST_INSTALLED=0
install_bundle_transaction "{binary}" "{target}" "{staged}"
rollback_bundle_transaction "{binary}" "{target}" "{staged}" "{failed}"
'''
                subprocess.run(["/bin/zsh", "-c", script], check=True)
                if scenario == "update":
                    self.assertEqual((target / "version").read_text(encoding="utf-8"), "old")
                else:
                    self.assertFalse(target.exists())
                self.assertEqual((failed / "version").read_text(encoding="utf-8"), "new")
                self.assertFalse(staged.exists())

    def test_rollback_failed_after_commit_cleanup_is_best_effort(self):
        helper = ROOT / "scripts/atomic_swap.c"
        transaction = ROOT / "scripts/install_app_transaction.sh"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / "atomic-swap"
            subprocess.run(["/usr/bin/clang", "-Wall", "-Wextra", "-Werror", str(helper), "-o", str(binary)], check=True)
            target = root / "OpenUsage Bar.app"
            staged = root / "OpenUsage Bar.app.new"
            previous = root / "OpenUsage Bar.app.previous-20260714T131149Z"
            target.mkdir()
            staged.mkdir()
            previous.joinpath("Contents").mkdir(parents=True)
            (target / "version").write_text("old", encoding="utf-8")
            (staged / "version").write_text("new", encoding="utf-8")
            with (previous / "Contents/Info.plist").open("wb") as handle:
                plistlib.dump({"CFBundleIdentifier": "com.lune.openusagebar"}, handle)
            marker = root / "rollback-ran"
            script = f'''source "{transaction}"
HAD_TARGET=0
SWAPPED=0
FIRST_INSTALLED=0
trap 'print rollback_failed_after_commit > "{marker}"' EXIT
install_bundle_transaction "{binary}" "{target}" "{staged}"
commit_bundle_transaction "{staged}"
SWAPPED=0
FIRST_INSTALLED=0
HAD_TARGET=0
trap - EXIT INT TERM
chmod 0555 "{root}"
cleanup_legacy_previous_bundles "{root}" || true
'''
            try:
                result = subprocess.run(["/bin/zsh", "-c", script], capture_output=True, text=True)
            finally:
                root.chmod(0o755)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("command not found", result.stderr)
            self.assertFalse(marker.exists())
            self.assertEqual((target / "version").read_text(encoding="utf-8"), "new")
            self.assertFalse(staged.exists())

    def test_commit_partial_delete_failure_never_rolls_back_healthy_target(self):
        helper = ROOT / "scripts/atomic_swap.c"
        transaction = ROOT / "scripts/install_app_transaction.sh"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / "atomic-swap"
            subprocess.run(["/usr/bin/clang", "-Wall", "-Wextra", "-Werror", str(helper), "-o", str(binary)], check=True)
            target = root / "OpenUsage Bar.app"
            staged = root / "OpenUsage Bar.app.new"
            target.mkdir()
            staged.mkdir()
            (target / "version").write_text("old", encoding="utf-8")
            (target / "deletable").write_text("remove-first", encoding="utf-8")
            protected = target / "protected"
            protected.mkdir()
            (protected / "retained").write_text("old-remnant", encoding="utf-8")
            (staged / "version").write_text("new", encoding="utf-8")
            marker = root / "rollback-ran"
            result_file = root / "cleanup-result"
            script = f'''source "{transaction}"
HAD_TARGET=0
SWAPPED=0
FIRST_INSTALLED=0
MUTATED=1
trap 'print rollback_after_partial_commit > "{marker}"' EXIT
install_bundle_transaction "{binary}" "{target}" "{staged}"
SWAPPED=0
FIRST_INSTALLED=0
HAD_TARGET=0
MUTATED=0
trap - EXIT INT TERM
chmod 000 "{staged}/protected"
if commit_bundle_transaction "{staged}"; then
  print unexpected_success > "{result_file}"
else
  print cleanup_failed > "{result_file}"
fi
'''
            try:
                result = subprocess.run(["/bin/zsh", "-c", script], capture_output=True, text=True)
            finally:
                if (staged / "protected").exists():
                    (staged / "protected").chmod(0o755)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result_file.read_text(encoding="utf-8").strip(), "cleanup_failed")
            self.assertFalse(marker.exists())
            self.assertEqual((target / "version").read_text(encoding="utf-8"), "new")
            self.assertTrue(staged.exists())
            self.assertFalse((staged / "deletable").exists())
            self.assertTrue((staged / "protected/retained").exists())


if __name__ == "__main__":
    unittest.main()
