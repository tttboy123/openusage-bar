import ast
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from pathlib import PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup.py"
WORKFLOW = ROOT / ".github/workflows/desktop-build.yml"
REQUIRED_PACKAGES = {
    "openusage_bar.gateway",
    "openusage_bar.providers",
}
REQUIRED_PYTHON_MEMBERS = {
    "openusage_bar/__init__.py",
    "openusage_bar/gateway/__init__.py",
    "openusage_bar/gateway/api.py",
    "openusage_bar/gateway/contracts.py",
    "openusage_bar/gateway/egress.py",
    "openusage_bar/gateway/policy.py",
    "openusage_bar/gateway/providers.py",
    "openusage_bar/gateway/runtime.py",
    "openusage_bar/gateway/self_test.py",
    "openusage_bar/providers/__init__.py",
    "openusage_bar/providers/builtins.py",
    "openusage_bar/providers/contracts.py",
    "openusage_bar/providers/quota.py",
    "openusage_bar/providers/registry.py",
}
REQUIRED_JSON_RESOURCES = {
    "resources/design-tokens.v1.json",
    "resources/gateway-api-v1.schema.json",
    "resources/gateway-response-v1.schema.json",
    "resources/local-api-v1.schema.json",
    "resources/product-version-truth.v1.json",
    "resources/provider-catalog.v1.json",
    "resources/provider-config-presets.v1.json",
    "resources/release-state.v1.json",
    "resources/runtime-capability-v1.schema.json",
}
WHEEL_BUILDER_ENV = "OPENUSAGE_WHEEL_BUILDER_PYTHON"
LOCAL_WHEEL_BUILDER_REQUIRED = (
    "wheel content gate requires a local Python interpreter with setuptools "
    "bdist_wheel; set OPENUSAGE_WHEEL_BUILDER_PYTHON"
)
OFFLINE_WHEEL_BUILD_FAILED = (
    "offline wheel build failed; verify setup.py and local build dependencies"
)
FORBIDDEN_RUNTIME_DIRECTORIES = {
    ".cache",
    ".openusage",
    "cache",
    "private",
    "private-config",
    "private_config",
    "telemetry",
    "token",
    "tokens",
}
FORBIDDEN_RUNTIME_FILENAMES = {
    ".env",
    "cache.json",
    "config.json",
    "config.toml",
    "credentials.json",
    "private-config.json",
    "private_config.json",
    "secrets.json",
    "telemetry.json",
    "telemetry.jsonl",
    "token.json",
    "tokens.json",
}
FORBIDDEN_RUNTIME_SUFFIXES = {
    ".cache",
    ".db",
    ".env",
    ".key",
    ".log",
    ".pem",
    ".pid",
    ".secret",
    ".sock",
    ".sqlite",
    ".sqlite3",
    ".token",
    ".toml",
}
SENSITIVE_RUNTIME_WORDS = {
    "cache",
    "config",
    "credential",
    "credentials",
    "private",
    "secret",
    "secrets",
    "telemetry",
    "token",
    "tokens",
}


def setup_common_field(name):
    tree = ast.parse(SETUP.read_text(encoding="utf-8"), filename=str(SETUP))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "common"
                for target in node.targets
            )
            and isinstance(node.value, ast.Dict)
        ):
            for key, value in zip(node.value.keys, node.value.values):
                if isinstance(key, ast.Constant) and key.value == name:
                    return ast.literal_eval(value)
    raise AssertionError(f"setup.py common metadata is missing {name}")


def _forbidden_runtime_member(member):
    path = PurePosixPath(member)
    parts = tuple(part.casefold() for part in path.parts)
    if set(parts[:-1]) & FORBIDDEN_RUNTIME_DIRECTORIES:
        return True
    filename = parts[-1] if parts else ""
    if filename in FORBIDDEN_RUNTIME_FILENAMES:
        return True
    if any(filename.endswith(suffix) for suffix in FORBIDDEN_RUNTIME_SUFFIXES):
        return True
    if member in {
        f"openusage_bar/{resource}" for resource in REQUIRED_JSON_RESOURCES
    }:
        return False
    if path.suffix.casefold() in {".py", ".pyi"}:
        return False
    filename_words = set(filter(None, re.split(r"[-_.]+", filename)))
    return bool(filename_words & SENSITIVE_RUNTIME_WORDS)


class PythonDistributionContractTests(unittest.TestCase):
    def test_real_wheel_gate_is_explicit_in_cross_platform_desktop_ci(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        for path in (
            "setup.py",
            "tests/test_python_distribution.py",
        ):
            with self.subTest(path=path):
                self.assertEqual(workflow.count(f'"{path}"'), 2)

        contracts_start = workflow.index(
            "- name: Run portable Observer and Gateway contracts"
        )
        contracts_end = workflow.index("\n      - name:", contracts_start + 1)
        active_targets = {
            line.strip().removesuffix("\\").strip()
            for line in workflow[contracts_start:contracts_end].splitlines()
            if line.strip().startswith("tests.")
        }
        self.assertIn("tests.test_python_distribution", active_targets)
        self.assertIn('"setuptools==83.0.0"', workflow)

    def test_distribution_contains_gateway_provider_runtime_and_json_contracts(self):
        self.maxDiff = None
        packages = set(setup_common_field("packages"))
        package_data = setup_common_field("package_data")

        configured_python_members = set()
        for package in packages:
            package_root = ROOT.joinpath(*package.split("."))
            configured_python_members.update(
                path.relative_to(ROOT).as_posix()
                for path in package_root.glob("*.py")
            )

        resource_patterns = package_data.get("openusage_bar", ())
        missing = {
            "packages": sorted(REQUIRED_PACKAGES - packages),
            "python_members": sorted(
                REQUIRED_PYTHON_MEMBERS - configured_python_members
            ),
            "json_resources": sorted(
                resource
                for resource in REQUIRED_JSON_RESOURCES
                if (
                    not (ROOT / "openusage_bar" / resource).is_file()
                    or not any(
                        fnmatch.fnmatchcase(resource, pattern)
                        for pattern in resource_patterns
                    )
                )
            ),
        }

        self.assertEqual(
            missing,
            {"packages": [], "python_members": [], "json_resources": []},
        )

    def test_built_wheel_contains_runtime_packages_without_private_artifacts(self):
        configured_builder = os.environ.get(WHEEL_BUILDER_ENV)
        if configured_builder:
            candidates = [
                ROOT / configured_builder
                if not Path(configured_builder).is_absolute()
                else Path(configured_builder)
            ]
        else:
            candidates = [
                Path(sys.executable),
                ROOT / ".build-venv" / "bin" / "python",
            ]
        offline_environment = {
            **os.environ,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "PYTHONNOUSERSITE": "1",
            "UV_OFFLINE": "1",
        }
        offline_environment.pop("PYTHONHOME", None)
        offline_environment.pop("PYTHONPATH", None)
        builder = None
        for candidate in candidates:
            try:
                builder_probe = subprocess.run(
                    [
                        str(candidate),
                        "-I",
                        "-c",
                        "from setuptools.command.bdist_wheel import bdist_wheel",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=offline_environment,
                    timeout=20,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if builder_probe.returncode == 0:
                builder = candidate
                break
        if builder is None:
            self.fail(LOCAL_WHEEL_BUILDER_REQUIRED)

        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            source_root = temporary_root / "source"
            wheel_root = temporary_root / "wheel"
            source_root.mkdir()
            wheel_root.mkdir()
            for source_name in (
                "setup.py",
                "build_support.py",
                "openusage_settings.py",
            ):
                shutil.copy2(ROOT / source_name, source_root / source_name)
            shutil.copytree(ROOT / "openusage_bar", source_root / "openusage_bar")

            try:
                completed = subprocess.run(
                    [
                        str(builder),
                        "setup.py",
                        "bdist_wheel",
                        "--dist-dir",
                        str(wheel_root),
                    ],
                    cwd=source_root,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=offline_environment,
                    timeout=120,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                self.fail(OFFLINE_WHEEL_BUILD_FAILED)
            if completed.returncode != 0:
                self.fail(OFFLINE_WHEEL_BUILD_FAILED)

            wheels = sorted(wheel_root.glob("*.whl"))
            self.assertEqual(
                len(wheels),
                1,
                "offline wheel build must produce exactly one wheel",
            )
            with zipfile.ZipFile(wheels[0]) as archive:
                wheel_members = {
                    PurePosixPath(member.filename).as_posix()
                    for member in archive.infolist()
                    if not member.is_dir()
                }
                version = setup_common_field("version")
                metadata_member = (
                    f"openusage_bar-{version}.dist-info/METADATA"
                )
                self.assertIn(
                    metadata_member,
                    wheel_members,
                    "wheel distribution identity must remain openusage-bar on macOS",
                )
                metadata = archive.read(metadata_member).decode("utf-8")
                metadata_lines = metadata.splitlines()
                self.assertIn("Name: openusage-bar", metadata_lines)
                self.assertIn(f"Version: {version}", metadata_lines)

        source_python_members = {
            path.relative_to(ROOT).as_posix()
            for package in (
                ROOT / "openusage_bar",
                ROOT / "openusage_bar" / "gateway",
                ROOT / "openusage_bar" / "providers",
            )
            for path in package.glob("*.py")
        }
        required_wheel_members = (
            source_python_members
            | REQUIRED_PYTHON_MEMBERS
            | {
                f"openusage_bar/{resource}"
                for resource in REQUIRED_JSON_RESOURCES
            }
        )
        observed = {
            "missing": sorted(required_wheel_members - wheel_members),
            "private_artifacts": sorted(
                member
                for member in wheel_members
                if _forbidden_runtime_member(member)
            ),
            "unsafe_paths": sorted(
                member
                for member in wheel_members
                if PurePosixPath(member).is_absolute()
                or ".." in PurePosixPath(member).parts
            ),
        }
        self.maxDiff = None
        self.assertEqual(
            observed,
            {"missing": [], "private_artifacts": [], "unsafe_paths": []},
        )


if __name__ == "__main__":
    unittest.main()
