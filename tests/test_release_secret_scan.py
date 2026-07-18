import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "scripts/release_secret_scan.py"


class ReleaseSecretScanTests(unittest.TestCase):
    def run_scan(self, repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(ROOT / ".build-venv/bin/python"), str(SCANNER), *arguments],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )

    @staticmethod
    def commit(repo: Path, message: str) -> None:
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=OpenUsage Tests",
                "-c",
                "user.email=tests@localhost",
                "commit",
                "-m",
                message,
            ],
            cwd=repo,
            check=True,
            stdout=subprocess.DEVNULL,
        )

    def test_safe_tree_and_history_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            (repo / "README.md").write_text("safe public source\n", encoding="utf-8")
            self.commit(repo, "initial")

            result = self.run_scan(repo, "--history")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout,
                "release_secret_scan_matches=0 scopes=tree,history\n",
            )

    def test_source_code_secret_lookups_do_not_match_as_literal_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            (repo / "adapter.py").write_text(
                "secret = self.keychain.get(self.config.provider_id)\n",
                encoding="utf-8",
            )
            self.commit(repo, "safe lookup")

            result = self.run_scan(repo)

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_contextual_literal_credential_still_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            (repo / "adapter.py").write_text(
                'api_key = "abcdefghijklmnopqrstuvwxyz012345"\n',
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=repo, check=True)

            result = self.run_scan(repo)

            self.assertNotEqual(result.returncode, 0)

    def test_tree_failure_does_not_echo_path_or_secret(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            secret = "Q4KG" + "A" * 60
            path = repo / "private-provider.txt"
            path.write_text("Step Plan " + secret, encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)

            result = self.run_scan(repo)

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(
                result.stderr,
                "release_secret_scan_forbidden_material scope=tree\n",
            )
            self.assertNotIn(secret, result.stdout + result.stderr)
            self.assertNotIn(str(path), result.stdout + result.stderr)

    def test_removed_secret_still_fails_history(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            secret = "Q4KG" + "B" * 60
            path = repo / "provider.txt"
            path.write_text("Step Plan " + secret, encoding="utf-8")
            self.commit(repo, "unsafe")
            path.write_text("redacted\n", encoding="utf-8")
            self.commit(repo, "remove secret")

            tree = self.run_scan(repo)
            history = self.run_scan(repo, "--history")

            self.assertEqual(tree.returncode, 0, tree.stderr)
            self.assertNotEqual(history.returncode, 0)
            self.assertEqual(
                history.stderr,
                "release_secret_scan_forbidden_material scope=history\n",
            )
            self.assertNotIn(secret, history.stdout + history.stderr)


if __name__ == "__main__":
    unittest.main()
