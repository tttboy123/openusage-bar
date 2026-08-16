from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.measure_gateway_performance import detect_source_provenance, main


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )


def _initialize_repository(repository: Path) -> str:
    subprocess.run(
        ("git", "init", "--quiet", str(repository)),
        check=True,
    )
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "user.email", "test@example.invalid")
    (repository / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "--quiet", "-m", "base")
    return _git(repository, "rev-parse", "HEAD").stdout.strip()


class GatewayPerformanceEvidenceContractTests(unittest.TestCase):
    def test_source_provenance_binds_real_head_and_observes_untracked_drift(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            actual_head = _initialize_repository(repository)

            self.assertEqual(
                detect_source_provenance(repository),
                {"sourceCommit": actual_head, "sourceTreeState": "clean"},
            )

            (repository / "untracked.txt").write_text("drift\n", encoding="utf-8")

            self.assertEqual(
                detect_source_provenance(repository),
                {"sourceCommit": actual_head, "sourceTreeState": "dirty"},
            )

    def test_source_provenance_observes_every_worktree_drift_class(self):
        for drift_class in ("tracked", "staged", "untracked"):
            with self.subTest(drift_class=drift_class):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    repository = Path(temporary_directory)
                    _initialize_repository(repository)
                    if drift_class == "untracked":
                        (repository / "untracked.txt").write_text(
                            "drift\n",
                            encoding="utf-8",
                        )
                    else:
                        (repository / "tracked.txt").write_text(
                            "drift\n",
                            encoding="utf-8",
                        )
                        if drift_class == "staged":
                            _git(repository, "add", "tracked.txt")

                    self.assertEqual(
                        detect_source_provenance(repository)["sourceTreeState"],
                        "dirty",
                    )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            submodule_source = root / "submodule-source"
            repository = root / "repository"
            _initialize_repository(submodule_source)
            _initialize_repository(repository)
            _git(
                repository,
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "--quiet",
                str(submodule_source),
                "dependencies/example",
            )
            _git(
                repository,
                "config",
                "-f",
                ".gitmodules",
                "submodule.dependencies/example.ignore",
                "all",
            )
            _git(repository, "add", ".gitmodules", "dependencies/example")
            _git(repository, "commit", "--quiet", "-m", "add submodule")
            (repository / "dependencies/example/tracked.txt").write_text(
                "submodule drift\n",
                encoding="utf-8",
            )

            self.assertEqual(
                detect_source_provenance(repository)["sourceTreeState"],
                "dirty",
            )

    def test_cli_rejects_forged_provenance_before_measurement(self):
        observed_commit = "1" * 40
        observed = {
            "sourceCommit": observed_commit,
            "sourceTreeState": "dirty",
        }
        cases = (
            ("0" * 40, "auto"),
            (observed_commit, "clean"),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "report.json"
            for source_commit, source_tree_state in cases:
                with self.subTest(
                    source_commit=source_commit,
                    source_tree_state=source_tree_state,
                ):
                    with (
                        patch(
                            "scripts.measure_gateway_performance.detect_source_provenance",
                            return_value=observed,
                        ),
                        patch(
                            "scripts.measure_gateway_performance.run_performance_report"
                        ) as measurement,
                    ):
                        result = main(
                            (
                                "run",
                                "--output",
                                str(output),
                                "--source-commit",
                                source_commit,
                                "--source-tree-state",
                                source_tree_state,
                            )
                        )

                    self.assertEqual(result, 1)
                    measurement.assert_not_called()
                    self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
