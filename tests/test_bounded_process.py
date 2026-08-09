import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


class BoundedProcessTests(unittest.TestCase):
    if sys.platform == "win32":
        __unittest_skip__ = True
        __unittest_skip_why__ = "POSIX process-group test"
    def helper(self, root: Path, body: str) -> Path:
        path = root / "helper"
        path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
        path.chmod(0o700)
        return path

    def test_overflow_kills_and_reaps_process_group(self):
        from openusage_bar.bounded_process import BoundedProcessError, run_bounded
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); pidfile = root / "child.pid"
            helper = self.helper(root,
                "import os,sys,time\nchild=os.fork()\n"
                "if child==0: time.sleep(30); raise SystemExit\n"
                "open(sys.argv[1],'w').write(str(child))\n"
                "sys.stdout.buffer.write(b'x'*70000);sys.stdout.flush();time.sleep(30)\n")
            with self.assertRaises(BoundedProcessError) as raised:
                run_bounded([str(helper), str(pidfile)], timeout=2, stdout_limit=65536)
            self.assertEqual(raised.exception.code, "output_overflow")
            pid=int(pidfile.read_text())
            for _ in range(20):
                state=subprocess.run(["/bin/ps","-o","stat=","-p",str(pid)],capture_output=True,text=True).stdout.strip()
                if not state: break
                time.sleep(.05)
            self.assertEqual(state, "")

    def test_timeout_is_sanitized_and_reaped(self):
        from openusage_bar.bounded_process import BoundedProcessError, run_bounded
        with tempfile.TemporaryDirectory() as directory:
            helper=self.helper(Path(directory),"import time\ntime.sleep(30)\n")
            with self.assertRaises(BoundedProcessError) as raised:
                run_bounded([str(helper)], timeout=1)
            self.assertEqual(raised.exception.code,"timeout")
            self.assertNotIn(str(helper),str(raised.exception))

    def test_parent_exit_still_reaps_descendant_holding_pipe(self):
        from openusage_bar.bounded_process import BoundedProcessError, run_bounded
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); pidfile = root / "child.pid"
            helper = self.helper(
                root,
                "import os,sys,time\n"
                "release_read,release_write=os.pipe()\n"
                "child=os.fork()\n"
                "if child==0:\n"
                " os.close(release_write)\n"
                " os.read(release_read,1)\n"
                " os.close(release_read)\n"
                " sys.stdout.buffer.write(b'x'*70000)\n"
                " sys.stdout.flush()\n"
                " time.sleep(30)\n"
                " raise SystemExit\n"
                "os.close(release_read)\n"
                "with open(sys.argv[1],'w') as pidfile:\n"
                " pidfile.write(str(child))\n"
                "os.write(release_write,b'x')\n"
                "os.close(release_write)\n",
            )
            with self.assertRaises(BoundedProcessError) as raised:
                run_bounded(
                    [str(helper), str(pidfile)],
                    timeout=30,
                    stdout_limit=65536,
                )
            self.assertEqual(raised.exception.code, "output_overflow")
            pid = int(pidfile.read_text())
            state = ""
            for _ in range(20):
                state = subprocess.run(
                    ["/bin/ps", "-o", "stat=", "-p", str(pid)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ).stdout.strip()
                if not state:
                    break
                time.sleep(.05)
            self.assertEqual(state, "")

    def test_success_reaps_descendant_that_closed_inherited_pipes(self):
        from openusage_bar.bounded_process import run_bounded
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); pidfile = root / "child.pid"
            helper = self.helper(
                root,
                "import os,sys,time\nchild=os.fork()\n"
                "if child==0:\n"
                " for fd in (0,1,2):\n"
                "  try: os.close(fd)\n"
                "  except OSError: pass\n"
                " time.sleep(30)\n"
                " raise SystemExit\n"
                "open(sys.argv[1],'w').write(str(child))\n",
            )
            result = run_bounded([str(helper), str(pidfile)], timeout=2)
            self.assertEqual(result.returncode, 0)
            pid = int(pidfile.read_text())
            state = ""
            for _ in range(20):
                state = subprocess.run(
                    ["/bin/ps", "-o", "stat=", "-p", str(pid)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ).stdout.strip()
                if not state:
                    break
                time.sleep(.05)
            if state:
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
            self.assertEqual(state, "")

    def test_text_success_is_completed_process_compatible(self):
        from openusage_bar.bounded_process import run_bounded
        with tempfile.TemporaryDirectory() as directory:
            helper=self.helper(Path(directory),"print('ok')\n")
            unrelated = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                # This validates CompletedProcess-compatible text output, not
                # the timeout boundary. Leave enough room for process startup
                # while the complete build runs the full suite under load.
                result=run_bounded([str(helper)],timeout=5,text=True,encoding="utf-8")
                self.assertEqual((result.returncode,result.stdout),(0,"ok\n"))
                self.assertIsNone(unrelated.poll())
            finally:
                unrelated.kill()
                unrelated.wait()

    def test_private_input_is_delivered_without_appearing_in_argv(self):
        from openusage_bar.bounded_process import run_bounded
        with tempfile.TemporaryDirectory() as directory:
            helper = self.helper(
                Path(directory),
                "import json,sys\n"
                "payload=sys.stdin.buffer.read()\n"
                "sys.stdout.write(json.dumps({"
                "'argv':sys.argv[1:],'size':len(payload),'value':payload.decode()"
                "}))\n",
            )
            secret = b"private-session-value"

            result = run_bounded(
                [str(helper), "keychain-operation"],
                timeout=2,
                input_data=secret,
                text=True,
                encoding="utf-8",
            )

        payload = __import__("json").loads(result.stdout)
        self.assertEqual(payload["argv"], ["keychain-operation"])
        self.assertEqual(payload["size"], len(secret))
        self.assertEqual(payload["value"], secret.decode())
        self.assertNotIn(secret.decode(), " ".join(result.args))

    def test_production_callers_do_not_use_unbounded_run(self):
        root=Path(__file__).resolve().parents[1]/"openusage_bar"
        for name in ("kiro.py","collector_cli.py","openusage_adapter.py","daily_history.py"):
            source=(root/name).read_text(encoding="utf-8")
            self.assertNotIn("runner or subprocess.run",source)
            self.assertNotIn("capture_output=True",source)
            self.assertIn("run_bounded",source)
