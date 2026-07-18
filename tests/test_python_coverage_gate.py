import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts/python_coverage_gate.py"


class PythonCoverageGateTests(unittest.TestCase):
    def run_gate(self, report: str, files=("aggregator.py",)):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "openusage_bar"
            package.mkdir()
            for name in files:
                target = package / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("VALUE = 1\n", encoding="utf-8")
            path = root / "trace.txt"
            path.write_text(report, encoding="utf-8")
            return subprocess.run(
                [str(ROOT / ".build-venv/bin/python"), str(GATE), "--report", str(path),
                 "--minimum", "80", "--package-root", str(package)],
                cwd=ROOT, capture_output=True, text=True,
            )

    def test_fails_when_a_touched_module_is_below_threshold_without_path_echo(self):
        result = self.run_gate(
            "  100    79%   openusage_bar.aggregator   (/private/source.py)\n",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr,
            "python_coverage_below_threshold openusage_bar.aggregator=79% minimum=80%\n",
        )
        self.assertNotIn("/private", result.stderr)

    def test_fails_closed_when_a_required_module_is_missing(self):
        result = self.run_gate("", files=("query.py",))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            result.stderr,
            "python_coverage_missing_module openusage_bar.query\n",
        )

    def test_reports_each_module_and_passes_at_threshold(self):
        result = self.run_gate(
            "  100    80%   openusage_bar.aggregator   (/source/a.py)\n"
            "  200    98%   openusage_bar.query   (/source/q.py)\n",
            files=("aggregator.py", "query.py"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "python_product_coverage openusage_bar.aggregator=80% "
            "openusage_bar.query=98% minimum=80%\n",
        )

    def test_new_product_module_is_discovered_without_updating_an_allowlist(self):
        result = self.run_gate(
            "  100    99%   openusage_bar.aggregator   (/source/a.py)\n",
            files=("aggregator.py", "example_module.py"),
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            result.stderr,
            "python_coverage_missing_module openusage_bar.example_module\n",
        )

    def test_only_checked_platform_boundaries_and_package_initializers_are_excluded(self):
        result = self.run_gate(
            "  100    80%   openusage_bar.aggregator   (/source/a.py)\n",
            files=("aggregator.py", "ui.py", "keychain.py", "providers/__init__.py"),
        )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
