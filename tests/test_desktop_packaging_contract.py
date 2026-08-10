import json
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop"
WORKFLOW = ROOT / ".github/workflows/desktop-build.yml"
IDENTITY_GATE = ROOT / "scripts/verify_artifact_build_identity.py"

PLATFORMS = {
    "mac": {
        "os": "macos-latest",
        "args": "--mac",
        "artifact": "UsageHub-*.dmg",
        "collector": "openusage-collector",
    },
    "win": {
        "os": "windows-latest",
        "args": "--win",
        "artifact": "UsageHub*.exe",
        "collector": "openusage-collector.exe",
    },
    "linux": {
        "os": "ubuntu-latest",
        "args": "--linux",
        "artifact": "UsageHub-*.AppImage",
        "collector": "openusage-collector",
    },
}

DESKTOP_MATRIX = [
    {
        "platform": "mac",
        "arch": "x64",
        "os": "macos-15-intel",
        "runner_arch": "X64",
        "args": "--mac",
        "builder_arch": "--x64",
        "package_root": "dist-desktop/mac/UsageHub.app",
        "upload_name": "usagehub-mac-x64",
        "artifact_arch": "x64",
        "artifact_ext": "dmg",
        "collector": "openusage-collector",
        "settings": "openusage-settings",
    },
    {
        "platform": "mac",
        "arch": "arm64",
        "os": "macos-15",
        "runner_arch": "ARM64",
        "args": "--mac",
        "builder_arch": "--arm64",
        "package_root": "dist-desktop/mac-arm64/UsageHub.app",
        "upload_name": "usagehub-mac-arm64",
        "artifact_arch": "arm64",
        "artifact_ext": "dmg",
        "collector": "openusage-collector",
        "settings": "openusage-settings",
    },
    {
        "platform": "win",
        "arch": "x64",
        "os": "windows-2025",
        "runner_arch": "X64",
        "args": "--win",
        "builder_arch": "--x64",
        "package_root": "dist-desktop/win-unpacked",
        "upload_name": "usagehub-win-x64",
        "artifact_arch": "x64",
        "artifact_ext": "exe",
        "collector": "openusage-collector.exe",
        "settings": "openusage-settings.exe",
    },
    {
        "platform": "win",
        "arch": "arm64",
        "os": "windows-11-arm",
        "runner_arch": "ARM64",
        "args": "--win",
        "builder_arch": "--arm64",
        "package_root": "dist-desktop/win-arm64-unpacked",
        "upload_name": "usagehub-win-arm64",
        "artifact_arch": "arm64",
        "artifact_ext": "exe",
        "collector": "openusage-collector.exe",
        "settings": "openusage-settings.exe",
    },
    {
        "platform": "linux",
        "arch": "x64",
        "os": "ubuntu-24.04",
        "runner_arch": "X64",
        "args": "--linux",
        "builder_arch": "--x64",
        "package_root": "dist-desktop/linux-unpacked",
        "upload_name": "usagehub-linux-x64",
        "artifact_arch": "x86_64",
        "artifact_ext": "AppImage",
        "collector": "openusage-collector",
        "settings": "openusage-settings",
    },
    {
        "platform": "linux",
        "arch": "arm64",
        "os": "ubuntu-24.04-arm",
        "runner_arch": "ARM64",
        "args": "--linux",
        "builder_arch": "--arm64",
        "package_root": "dist-desktop/linux-arm64-unpacked",
        "upload_name": "usagehub-linux-arm64",
        "artifact_arch": "arm64",
        "artifact_ext": "AppImage",
        "collector": "openusage-collector",
        "settings": "openusage-settings",
    },
]


def _scalar(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _mapping_list(source, *, section, item_indent):
    """Parse the workflow's flat matrix/step mappings, not arbitrary YAML."""
    lines = source.splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if line.strip() == f"{section}:"
    )
    entries = []
    current = None
    block_key = None
    block_indent = None
    item_prefix = " " * item_indent + "- "
    field_prefix = " " * (item_indent + 2)
    for line in lines[start + 1 :]:
        if line.startswith(item_prefix):
            if current is not None:
                entries.append(current)
            current = {}
            block_key = None
            field = line[len(item_prefix) :]
        elif current is not None and line.startswith(field_prefix):
            indentation = len(line) - len(line.lstrip())
            if block_key is not None and indentation < block_indent:
                block_key = None
                block_indent = None
            if block_key is not None:
                current[block_key] += line.strip() + "\n"
                continue
            field = line[len(field_prefix) :]
        elif current is not None and line.strip() and len(line) - len(line.lstrip()) <= item_indent:
            break
        else:
            continue
        if ":" in field:
            key, value = field.split(":", 1)
            key = key.strip()
            value = _scalar(value)
            if value in {"|", ">"}:
                current[key] = ""
                block_key = key
                block_indent = len(line) - len(line.lstrip()) + 2
            else:
                current[key] = value
    if current is not None:
        entries.append(current)
    return entries


def _event_paths(source, event):
    """Return one workflow event's explicit path filters."""

    lines = source.splitlines()
    event_start = next(
        index for index, line in enumerate(lines) if line == f"  {event}:"
    )
    paths_start = next(
        index
        for index, line in enumerate(lines[event_start + 1 :], event_start + 1)
        if line == "    paths:"
    )
    paths = []
    for line in lines[paths_start + 1 :]:
        if line.startswith("      - "):
            paths.append(_scalar(line.removeprefix("      - ")))
        elif line.strip() and not line.startswith("      "):
            break
    return paths


def _active_shell_commands(source):
    """Return tokenized active shell commands, joining continuations."""

    commands = []
    pending = ""
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        logical_line = f"{pending} {line}".strip()
        if logical_line.endswith("\\"):
            pending = logical_line[:-1].rstrip()
            continue
        pending = ""
        normalized = re.sub(
            r"\$\{\{\s*([^{}]*?)\s*\}\}",
            lambda match: "${{" + re.sub(r"\s+", "", match.group(1)) + "}}",
            logical_line,
        )
        tokens = shlex.split(normalized, comments=True, posix=True)
        if tokens:
            commands.append(tokens)
    if pending:
        commands.append(shlex.split(pending, comments=True, posix=True))
    return commands


def _command_has_option(command, option, value):
    try:
        return command[command.index(option) + 1] == value
    except (ValueError, IndexError):
        return False


def _write_identity_gate_fixture(root):
    for relative in (
        Path("openusage_bar/resources/artifact-build-identity.v1.json"),
        Path("openusage_bar/resources/product-version-truth.v1.json"),
        Path("web/public/product-build-identity.v1.json"),
        Path("swift_app/Resources/product-build-identity.v1.json"),
        Path("desktop/package.json"),
    ):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / relative).read_bytes())


def _run_identity_gate(root):
    return subprocess.run(
        [sys.executable, str(IDENTITY_GATE), "--root", str(root)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _resolver_harness():
    return r"""
const fs = require("fs");
const path = require("path");
const desktop = process.argv[1];
const candidates = [
  path.join(desktop, "collector_runtime.js"),
  path.join(desktop, "gateway_proxy.js"),
];
let resolveCollectorCommand = null;
for (const candidate of candidates) {
  if (!fs.existsSync(candidate)) continue;
  const loaded = require(candidate);
  if (typeof loaded.resolveCollectorCommand === "function") {
    resolveCollectorCommand = loaded.resolveCollectorCommand;
    break;
  }
}
if (!resolveCollectorCommand) {
  throw new Error("missing pure resolveCollectorCommand export");
}
const cases = [
  ["darwin", "/Applications/UsageHub.app/Contents/Resources", "/Applications/UsageHub.app/Contents/Resources/collector/openusage-collector"],
  ["linux", "/opt/UsageHub/resources", "/opt/UsageHub/resources/collector/openusage-collector"],
  ["win32", "C:\\UsageHub\\resources", "C:\\UsageHub\\resources\\collector\\openusage-collector.exe"],
];
const output = [];
for (const [platform, resourcesPath, expected] of cases) {
  const resolved = resolveCollectorCommand({
    isPackaged: true,
    resourcesPath,
    platform,
    environment: { USAGEHUB_COLLECTOR: "/tmp/external-collector" },
    pathExists(candidate) {
      return candidate === expected || candidate === "/tmp/external-collector";
    },
  });
  output.push({ platform, resolved, expected });
}
const relative = resolveCollectorCommand({
  isPackaged: true,
  resourcesPath: "/missing/resources",
  platform: "linux",
  environment: { USAGEHUB_COLLECTOR: "./attacker-controlled" },
  pathExists(candidate) { return candidate === "./attacker-controlled"; },
});
output.push({ platform: "relative-override", resolved: relative, expected: null });
const external = resolveCollectorCommand({
  isPackaged: true,
  resourcesPath: "/missing/resources",
  platform: "linux",
  environment: { USAGEHUB_COLLECTOR: "/opt/external-openusage-collector" },
  pathExists(candidate) { return candidate === "/opt/external-openusage-collector"; },
});
output.push({ platform: "absolute-override", resolved: external, expected: null });
process.stdout.write(JSON.stringify(output));
"""


class DesktopPackagingContractTests(unittest.TestCase):
    def test_each_target_copies_exactly_one_native_collector_to_the_fixed_resource_path(self):
        package = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))

        for platform, expected in PLATFORMS.items():
            with self.subTest(platform=platform):
                resources = package["build"][platform]["extraResources"]
                collector_resources = [
                    item
                    for item in resources
                    if isinstance(item, dict)
                    and str(item.get("to", "")).startswith("collector/")
                ]
                self.assertEqual(
                    collector_resources,
                    [{
                        "from": f"../dist-collector/{expected['collector']}",
                        "to": f"collector/{expected['collector']}",
                    }],
                )

    def test_packaged_resolution_prefers_the_bundled_os_native_collector_and_rejects_relative_override(self):
        completed = subprocess.run(
            ["node", "-e", _resolver_harness(), str(DESKTOP)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        for result in json.loads(completed.stdout):
            with self.subTest(platform=result["platform"]):
                self.assertEqual(result["resolved"], result["expected"])

    def test_main_consumes_the_pure_collector_resolver(self):
        source = (DESKTOP / "main.js").read_text(encoding="utf-8")
        imports = re.findall(
            r"const\s*\{(?P<names>.*?)\}\s*=\s*require\("
            r"\"(?P<module>\./(?:collector_runtime|gateway_proxy))\"\);",
            source,
            flags=re.DOTALL,
        )
        resolver_modules = {
            module
            for names, module in imports
            if "resolveCollectorCommand" in {
                name.strip() for name in names.split(",")
            }
        }
        self.assertEqual(len(resolver_modules), 1)

        start = source.index("function dashboardCommand()")
        end = source.index("\n}", start)
        self.assertIn("resolveCollectorCommand(", source[start:end])

    def test_packaged_main_bootstraps_and_reads_tray_state_without_the_legacy_dashboard(self):
        source = (DESKTOP / "main.js").read_text(encoding="utf-8")
        ready = re.search(
            r"app\.whenReady\(\)\.then\(async \(\) => \{(?P<body>.*?)\n\}\);",
            source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(ready)
        ready_body = ready.group("body")
        self.assertIn("ensurePrivateObserver()", ready_body)
        self.assertNotIn("ensureDashboard", ready_body)

        self.assertIn("fetchPrivateObserverJson", source)
        self.assertIn('"/v1/snapshot"', source)
        self.assertIn('"/v1/capacity"', source)
        for legacy_dependency in (
            "17822",
            "DASHBOARD_URL",
            "dashboardUp",
            "ensureDashboard",
            "fetchLegacyDashboardJson",
            "startOrProbeLegacyDashboard",
        ):
            with self.subTest(legacy_dependency=legacy_dependency):
                self.assertNotIn(legacy_dependency, source)

    def test_ci_declares_the_exact_six_native_architecture_rows(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        matrix = _mapping_list(source, section="include", item_indent=10)

        self.maxDiff = None
        self.assertEqual(matrix, DESKTOP_MATRIX)

    def test_ci_wires_architecture_and_deterministic_artifact_paths(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        matrix = _mapping_list(source, section="include", item_indent=10)
        steps = _mapping_list(source, section="steps", item_indent=6)
        setup_python = next(
            step
            for step in steps
            if step.get("uses", "").startswith("actions/setup-python@")
        )
        package_step = next(
            step for step in steps if step.get("name") == "Package desktop app"
        )
        audit_step = next(
            step for step in steps if step.get("name") == "Audit packaged collector"
        )
        upload_step = next(
            step
            for step in steps
            if step.get("uses", "").startswith("actions/upload-artifact@")
        )
        runner_arch_step = next(
            (
                step
                for step in steps
                if step.get("name") == "Verify runner identity"
            ),
            {},
        )
        resolve_artifact_step = next(
            (
                step
                for step in steps
                if step.get("name") == "Resolve final artifact"
            ),
            {},
        )
        audit_commands = _active_shell_commands(audit_step["run"])
        resolve_artifact_commands = _active_shell_commands(
            resolve_artifact_step.get("run", "")
        )
        package = json.loads(
            (DESKTOP / "package.json").read_text(encoding="utf-8")
        )
        forbidden_version_literals = tuple(dict.fromkeys((
            "0.8.6",
            str(package["version"]),
        )))
        observed = {
            "floating_runners": [
                row.get("os")
                for row in matrix
                if str(row.get("os", "")).endswith("-latest")
            ],
            "wildcard_artifacts": [
                f"{key}={value}"
                for row in matrix
                for key, value in row.items()
                if key in {"artifact", "final_file"}
                and any(mark in value for mark in "*?[")
            ],
            "find_quit_commands": [
                command
                for command in audit_commands
                if "find" in " ".join(command) and "-quit" in " ".join(command)
            ],
            "setup_python_architecture": setup_python.get("architecture"),
            "runner_arch_commands": _active_shell_commands(
                runner_arch_step.get("run", "")
            ),
            "package_commands": _active_shell_commands(package_step["run"]),
            "package_root_commands": [
                command
                for command in audit_commands
                if command == ["package_root=${{matrix.package_root}}"]
                or command == ["test", "-d", "$package_root"]
            ],
            "resolve_artifact": {
                "id": resolve_artifact_step.get("id"),
                "shell": resolve_artifact_step.get("shell"),
                "commands": resolve_artifact_commands,
            },
            "upload_name": upload_step.get("name"),
            "upload_path": upload_step.get("path"),
            "workflow_version_literals": [
                literal for literal in forbidden_version_literals if literal in source
            ],
        }
        expected = {
            "floating_runners": [],
            "wildcard_artifacts": [],
            "find_quit_commands": [],
            "setup_python_architecture": "${{ matrix.arch }}",
            "runner_arch_commands": [
                ["test", "$CI_RUNNER_ENVIRONMENT", "=", "github-hosted"],
                ["test", "$CI_RUNNER_OS", "=", "$RUNNER_OS"],
                ["test", "$CI_RUNNER_ARCH", "=", "${{matrix.runner_arch}}"],
            ],
            "package_commands": [[
                "npx",
                "electron-builder",
                "${{matrix.args}}",
                "${{matrix.builder_arch}}",
                "--publish",
                "never",
            ]],
            "package_root_commands": [
                ["package_root=${{matrix.package_root}}"],
                ["test", "-d", "$package_root"],
            ],
            "resolve_artifact": {
                "id": "artifact",
                "shell": "bash",
                "commands": [
                    ["version=$(node -p require(./desktop/package.json).version)"],
                    [
                        "artifact_path=dist-desktop/UsageHub-${version}-"
                        "${{matrix.platform}}-${{matrix.artifact_arch}}."
                        "${{matrix.artifact_ext}}"
                    ],
                    ["test", "-f", "$artifact_path"],
                    [
                        "printf",
                        "path=%s\\n",
                        "$artifact_path",
                        ">>",
                        "$GITHUB_OUTPUT",
                    ],
                    [
                        "printf",
                        "version=%s\\n",
                        "$version",
                        ">>",
                        "$GITHUB_OUTPUT",
                    ],
                ],
            },
            "upload_name": "${{ matrix.upload_name }}",
            "upload_path": "${{ steps.release_handoff.outputs.path }}",
            "workflow_version_literals": [],
        }

        self.maxDiff = None
        self.assertEqual(observed, expected)

    def test_desktop_artifact_name_is_os_and_arch_deterministic(self):
        package = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))

        self.assertEqual(
            package["build"].get("artifactName"),
            "${productName}-${version}-${os}-${arch}.${ext}",
        )

    def test_identity_source_gate_rejects_desktop_build_configuration_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            _write_identity_gate_fixture(fixture)
            baseline = _run_identity_gate(fixture)
            self.assertEqual(baseline.returncode, 0, baseline.stderr)
            self.assertEqual(
                baseline.stdout,
                "artifact_build_identity_ok product=UsageHub "
                "candidate=0.8.6 build=28\n",
            )
        cases = (
            "buildVersion",
            "buildNumber",
            "extraMetadata",
            "extraResources",
            "productVersionTruthFile",
            "buildIdentityCopyFile",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                fixture = Path(directory)
                _write_identity_gate_fixture(fixture)
                package_path = fixture / "desktop/package.json"
                package = json.loads(package_path.read_text(encoding="utf-8"))
                build = package["build"]
                if case == "buildVersion":
                    build["buildVersion"] = "29"
                elif case == "buildNumber":
                    build["buildNumber"] = "29"
                elif case == "extraMetadata":
                    build["extraMetadata"]["buildNumber"] = "29"
                elif case == "extraResources":
                    build["extraResources"] = [
                        value
                        for value in build["extraResources"]
                        if value.get("to") != "product-build-identity.v1.json"
                    ]
                elif case == "productVersionTruthFile":
                    build["files"].remove("product_version_truth.js")
                else:
                    build["files"].remove("build_identity_copy.js")
                package_path.write_text(
                    json.dumps(package, ensure_ascii=True, indent=2) + "\n",
                    encoding="utf-8",
                )

                verified = _run_identity_gate(fixture)

                self.assertEqual(verified.returncode, 1)
                self.assertEqual(verified.stdout, "")
                self.assertEqual(
                    verified.stderr,
                    "artifact_build_identity_invalid\n",
                )
                self.assertNotIn(str(fixture), verified.stderr)

    def test_ci_builds_smokes_packages_and_audits_each_native_collector_in_order(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        steps = _mapping_list(source, section="steps", item_indent=6)
        by_name = {step.get("name"): step for step in steps}
        required = [
            "Build bundled collector",
            "Smoke bundled collector status",
            "Smoke bundled Gateway modes",
            "Package desktop app",
            "Audit packaged collector",
        ]
        indices = [
            next(index for index, step in enumerate(steps) if step.get("name") == name)
            for name in required
        ]
        self.assertEqual(indices, sorted(indices))

        self.assertIn("openusage_collector.py", by_name[required[0]]["run"])
        self.assertIn("dist-collector", by_name[required[0]]["run"])
        status_run = by_name[required[1]]["run"]
        self.assertRegex(
            status_run,
            r"(?m)^\s*(?:&\s*)?[\"']?\./dist-collector/\$\{\{ matrix\.collector \}\}"
            r"[\"']?\s+--offline\s+status\s+--format\s+json\s*$",
        )
        self.assertEqual(
            by_name[required[1]].get("shell"),
            "bash",
            "use one explicit shell so the same direct collector command runs on all runners",
        )
        self.assertIn("scripts/smoke_gateway.py", by_name[required[2]]["run"])
        self.assertIn("electron-builder", by_name[required[3]]["run"])
        self.assertIn("release_artifact_audit.py", by_name[required[4]]["run"])
        self.assertIn("matrix.collector", by_name[required[4]]["run"])

    def test_ci_builds_and_smokes_the_fixed_settings_helper_before_packaging(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        steps = _mapping_list(source, section="steps", item_indent=6)
        by_name = {step.get("name"): step for step in steps}
        required = [
            "Build bundled settings helper",
            "Smoke bundled settings helper",
            "Package desktop app",
        ]
        indices = [
            next(index for index, step in enumerate(steps) if step.get("name") == name)
            for name in required
        ]
        self.assertEqual(indices, sorted(indices))
        build = by_name[required[0]]["run"]
        smoke = by_name[required[1]]["run"]
        self.assertIn("openusage_settings.py", build)
        self.assertIn("dist-settings", build)
        self.assertIn("matrix.settings", build)
        self.assertIn("--hidden-import tkinter", build)
        self.assertIn("--hidden-import tkinter.ttk", build)
        self.assertIn("./dist-settings/${{ matrix.settings }}", smoke)
        self.assertIn("gateway-account-mutate", smoke)
        self.assertIn('"version":1', smoke)
        self.assertIn('"action":"remove_pool"', smoke)
        self.assertIn("gateway-account-editor --ui-self-test", smoke)
        self.assertIn('{"version":1,"event":"ready"}', smoke)
        self.assertIn(
            '{"version":1,"event":"terminal","state":"succeeded","code":"ok"}',
            smoke,
        )

    def test_ci_runs_native_gateway_account_credential_smoke_after_tk_self_test(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        steps = _mapping_list(source, section="steps", item_indent=6)
        by_name = {step.get("name"): step for step in steps}
        smoke = by_name["Smoke bundled settings helper"]["run"]

        self.assertIn(
            "gateway-account-editor --ui-self-test",
            smoke,
            "Tk self-test must stay distinct from native credential roundtrip",
        )
        self.assertIn("scripts/smoke_gateway_account_credentials.py", smoke)
        self.assertIn("--settings-helper", smoke)
        self.assertIn("./dist-settings/${{ matrix.settings }}", smoke)
        self.assertIn(
            '{"version":1,"ok":true,"code":"ok"}',
            smoke,
            "native credential smoke success envelope must remain exact and safe",
        )
        self.assertLess(
            smoke.index("gateway-account-editor --ui-self-test"),
            smoke.index("scripts/smoke_gateway_account_credentials.py"),
        )
        self.assertNotRegex(
            smoke,
            r"OPENUSAGE_.*FAKE|FakeKeychain|MOCK_KEYCHAIN",
            "packaged native smoke must not fake the platform credential backend",
        )

    def test_native_gateway_account_credential_smoke_captures_safe_failure_output(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        steps = _mapping_list(source, section="steps", item_indent=6)
        smoke = next(
            step for step in steps if step.get("name") == "Smoke bundled settings helper"
        )["run"]

        self.assertEqual(smoke.count("account_smoke_status=$?"), 2)
        self.assertEqual(
            smoke.count('test "$account_smoke_status" -eq 0'),
            2,
            "native credential smoke must record exit status before asserting",
        )
        self.assertEqual(smoke.count("set +e"), 4)
        self.assertEqual(smoke.count("set -e"), 4)
        self.assertIn('printf \'%s\\n\' "$account_smoke_output"', smoke)
        self.assertNotRegex(
            smoke,
            r'account_smoke_output="\$\(\s*python scripts/smoke_gateway_account_credentials.py'
            r'(?:(?!account_smoke_status=\$\?).)*test "\$account_smoke_output"',
            "set -e command substitution must not hide the safe stdout envelope",
        )

    def test_linux_native_gateway_account_credential_smoke_uses_pinned_private_secret_service_session(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        steps = _mapping_list(source, section="steps", item_indent=6)
        smoke = next(
            step for step in steps if step.get("name") == "Smoke bundled settings helper"
        )["run"]

        self.assertIn("dbus-user-session=1.14.10-4ubuntu4.1", source)
        self.assertIn("gnome-keyring=46.1-2build1", source)
        self.assertIn("if [[ \"${{ matrix.platform }}\" == \"linux\" ]]; then", smoke)
        self.assertIn("dbus-run-session -- bash -euo pipefail", smoke)
        self.assertIn("gnome-keyring-daemon --unlock --components=secrets", smoke)
        for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
            self.assertIn(variable, smoke)

    def test_native_gateway_account_credential_smoke_is_a_portable_contract_and_path_trigger(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        contracts = next(
            step
            for step in _mapping_list(source, section="steps", item_indent=6)
            if step.get("name") == "Run portable Observer and Gateway contracts"
        )["run"]
        for test_module in (
            "tests.test_gateway_account_editor_model",
            "tests.test_gateway_account_editor_tk",
            "tests.test_keychain",
            "tests.test_openusage_settings_entrypoint",
            "tests.test_smoke_gateway_account_credentials",
        ):
            self.assertIn(test_module, contracts)
        for event in ("push", "pull_request"):
            paths = _event_paths(source, event)
            self.assertIn("scripts/smoke_gateway_account_credentials.py", paths)
            self.assertIn("tests/test_gateway_account_editor_model.py", paths)
            self.assertIn("tests/test_gateway_account_editor_tk.py", paths)
            self.assertIn("tests/test_keychain.py", paths)
            self.assertIn("tests/test_openusage_settings_entrypoint.py", paths)
            self.assertIn("tests/test_smoke_gateway_account_credentials.py", paths)

    def test_ci_smokes_exact_frozen_collector_across_modes_and_credential_failure(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        steps = _mapping_list(source, section="steps", item_indent=6)
        smoke_step = next(
            step for step in steps if step.get("name") == "Smoke bundled Gateway modes"
        )
        commands = _active_shell_commands(smoke_step["run"])
        collector = "./dist-collector/${{matrix.collector}}"
        frozen_smokes = [
            command
            for command in commands
            if command[:2] == ["python", "scripts/smoke_gateway.py"]
        ]

        self.assertTrue(
            any(
                "--all" in command
                and _command_has_option(command, "--collector", collector)
                for command in frozen_smokes
            ),
            "CI must pass the exact just-built Collector to the deterministic "
            "three-mode and credential-failure smoke harness",
        )

    def test_ci_audits_the_unpacked_collector_against_the_exact_built_binary(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        steps = _mapping_list(source, section="steps", item_indent=6)
        audit_step = next(
            step for step in steps if step.get("name") == "Audit packaged collector"
        )
        commands = _active_shell_commands(audit_step["run"])
        collector = "./dist-collector/${{matrix.collector}}"
        audits = [
            command
            for command in commands
            if command[:2] == ["python", "scripts/release_artifact_audit.py"]
        ]

        self.assertEqual(
            audits,
            [[
                "python",
                "scripts/release_artifact_audit.py",
                "--desktop-package",
                "$package_root",
                "${{matrix.collector}}",
                "--built-collector",
                collector,
                "--built-settings",
                "./dist-settings/${{matrix.settings}}",
            ]],
            "CI must pass one exact desktop audit command in the CLI contract order",
        )

    def test_ci_runs_its_packaging_contract_and_desktop_unit_tests(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        steps = _mapping_list(source, section="steps", item_indent=6)
        by_name = {step.get("name"): step for step in steps}
        missing = []

        contract_commands = _active_shell_commands(
            by_name["Run portable Observer and Gateway contracts"]["run"]
        )
        if not any(
            "tests.test_desktop_packaging_contract" in command
            for command in contract_commands
        ):
            missing.append("tests.test_desktop_packaging_contract")

        native_job_modules = {"tests.test_bounded_process_windows"}
        if not any(
            command[:3] == ["python", "-m", "unittest"]
            and native_job_modules.issubset(command)
            for command in contract_commands
        ):
            missing.append("explicit portable Windows Job tests")

        native_step = by_name.get("Run native Windows Job contracts", {})
        if (
            native_step.get("if") != "matrix.platform == 'win'"
            or ["python", "-m", "unittest", "tests.test_bounded_process_windows_native"]
            not in _active_shell_commands(native_step.get("run", ""))
        ):
            missing.append("explicit native Windows Job tests")

        desktop_test_steps = [
            step
            for step in steps
            if step.get("working-directory") == "desktop"
            and ["npm", "test"] in _active_shell_commands(step.get("run", ""))
        ]
        if not desktop_test_steps:
            missing.append("desktop npm test")

        self.assertEqual(missing, [], "missing active CI gates: " + ", ".join(missing))

        native_job_path = "tests/test_bounded_process_windows*.py"
        for event in ("push", "pull_request"):
            with self.subTest(event=event):
                self.assertEqual(
                    _event_paths(source, event).count(native_job_path),
                    1,
                    "each CI event must explicitly trigger on the narrow Windows "
                    "bounded-process test family",
                )

    def test_ci_fails_closed_on_high_or_critical_web_dependency_audit(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        steps = _mapping_list(source, section="steps", item_indent=6)
        audit_step = next(
            step for step in steps if step.get("name") == "Audit web dependencies"
        )

        self.assertEqual(
            _active_shell_commands(audit_step.get("run", "")),
            [["npm", "audit", "--omit=dev", "--audit-level=high"]],
        )
        self.assertNotEqual(
            audit_step.get("continue-on-error"),
            "true",
            "High/Critical production dependency findings must stop packaging",
        )

    def test_gateway_account_playwright_e2e_runs_once_on_linux_x64_without_handoff_upload(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        steps = _mapping_list(source, section="steps", item_indent=6)
        by_name = {step.get("name"): step for step in steps}
        web_install = by_name["Install web dependencies"]
        browser_install = by_name["Install Gateway Account Playwright browser"]
        e2e = by_name["Run Gateway Account browser E2E"]
        upload_steps = [
            step
            for step in steps
            if step.get("uses", "").startswith("actions/upload-artifact@")
        ]

        self.assertLess(steps.index(web_install), steps.index(browser_install))
        self.assertLess(steps.index(browser_install), steps.index(e2e))
        self.assertEqual(browser_install.get("working-directory"), "web")
        self.assertEqual(e2e.get("working-directory"), "web")
        self.assertEqual(
            browser_install.get("if"),
            "matrix.platform == 'linux' && matrix.arch == 'x64'",
        )
        self.assertEqual(
            e2e.get("if"),
            "matrix.platform == 'linux' && matrix.arch == 'x64'",
        )
        self.assertEqual(
            _active_shell_commands(browser_install["run"]),
            [["npx", "playwright", "install", "--with-deps", "chromium"]],
        )
        self.assertEqual(
            _active_shell_commands(e2e["run"]),
            [["npm", "run", "test:e2e:gateway-account"]],
        )
        self.assertEqual(
            [step.get("name") for step in upload_steps],
            ["${{ matrix.upload_name }}"],
            "Gateway Account E2E must not add a sixth retained handoff upload",
        )
        self.assertEqual(
            [step.get("path") for step in upload_steps],
            ["${{ steps.release_handoff.outputs.path }}"],
        )
        self.assertNotIn("playwright-report", source)
        self.assertNotIn("test-results", source)
        for event in ("push", "pull_request"):
            paths = _event_paths(source, event)
            self.assertIn("web/e2e/**", paths)
            self.assertIn("web/playwright.config.ts", paths)

    def test_ci_fails_closed_on_high_or_critical_desktop_dependency_audit(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        steps = _mapping_list(source, section="steps", item_indent=6)
        audit_step = next(
            step for step in steps if step.get("name") == "Audit desktop dependencies"
        )

        self.assertEqual(
            _active_shell_commands(audit_step.get("run", "")),
            [["npm", "audit", "--audit-level=high"]],
        )
        self.assertNotEqual(
            audit_step.get("continue-on-error"),
            "true",
            "Electron runtime/build-chain High/Critical findings must stop packaging",
        )

    def test_ci_audits_the_final_macos_dmg_read_only_before_upload(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        resolve = source.index("- name: Resolve final artifact")
        audit = source.index("- name: Audit final macOS DMG")
        upload = source.index("- name: Upload artifact")
        self.assertLess(resolve, audit)
        self.assertLess(audit, upload)

        final_gate = source[audit:upload]
        self.assertIn("if: matrix.platform == 'mac'", final_gate)
        self.assertIn(
            'snapshot_root="$(mktemp -d '
            '"$RUNNER_TEMP/usagehub-dmg-snapshot.XXXXXX")"',
            final_gate,
        )
        self.assertIn('/bin/cp "$artifact" "$snapshot"', final_gate)
        self.assertIn('/bin/chmod 0400 "$snapshot"', final_gate)
        self.assertEqual(
            final_gate.count('/usr/bin/cmp -s "$artifact" "$snapshot"'),
            1,
        )
        self.assertEqual(
            final_gate.count('/usr/bin/shasum -a 256 "$snapshot"'),
            2,
        )
        self.assertIn(
            'test "$final_snapshot_digest" = "$snapshot_digest"',
            final_gate,
        )
        self.assertIn("hdiutil verify", final_gate)
        self.assertRegex(
            final_gate,
            r"hdiutil attach\s+-readonly\s+-nobrowse\s+-noautoopen\s+-owners off",
        )
        self.assertIn(
            "FINAL_ARTIFACT: ${{ steps.artifact.outputs.path }}",
            final_gate,
        )
        windows_audit = source.index("- name: Audit final Windows NSIS payload")
        linux_audit = source.index("- name: Audit final Linux AppImage payload")
        windows_gate = source[windows_audit:linux_audit]
        self.assertIn(
            'OPENUSAGE_TRUST_STAGE_DIAGNOSTIC: "1"',
            windows_gate,
        )
        self.assertNotIn(
            "OPENUSAGE_TRUST_STAGE_DIAGNOSTIC",
            source[:windows_audit],
        )
        self.assertIn('--artifact "$snapshot"', final_gate)
        self.assertIn("- name: Select audited final artifact", final_gate)
        self.assertIn(
            "FINAL_ARTIFACT: ${{ steps.audited_artifact.outputs.path }}",
            final_gate,
        )
        self.assertIn(
            "${{ steps.release_handoff.outputs.path }}",
            source[upload:],
        )
        select = source.index("- name: Select audited final artifact")
        verify = source.index("- name: Verify distribution trust posture")
        select_gate = source[select:verify]
        self.assertIn(
            "MAC_SNAPSHOT: ${{ steps.final_mac.outputs.path }}",
            select_gate,
        )
        self.assertIn(
            "WINDOWS_SNAPSHOT: ${{ steps.final_win.outputs.path }}",
            select_gate,
        )
        self.assertIn(
            "LINUX_SNAPSHOT: ${{ steps.final_linux.outputs.path }}",
            select_gate,
        )
        self.assertNotIn("ORIGINAL_ARTIFACT", select_gate)
        self.assertIn('package_root="$mount_root/UsageHub.app"', final_gate)
        self.assertIn("python scripts/release_artifact_audit.py", final_gate)
        self.assertIn(
            '--built-collector "./dist-collector/${{ matrix.collector }}"',
            final_gate,
        )
        self.assertIn("python scripts/smoke_gateway.py --all", final_gate)


if __name__ == "__main__":
    unittest.main()
