import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


class BoundedProcessTests(unittest.TestCase):
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
                "import os,sys,time\nchild=os.fork()\n"
                "if child==0: time.sleep(30); raise SystemExit\n"
                "open(sys.argv[1],'w').write(str(child))\n",
            )
            with self.assertRaises(BoundedProcessError) as raised:
                run_bounded([str(helper), str(pidfile)], timeout=1)
            self.assertEqual(raised.exception.code, "timeout")
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
                result=run_bounded([str(helper)],timeout=2,text=True,encoding="utf-8")
                self.assertEqual((result.returncode,result.stdout),(0,"ok\n"))
                self.assertIsNone(unrelated.poll())
            finally:
                unrelated.kill()
                unrelated.wait()

    def test_production_callers_do_not_use_unbounded_run(self):
        root=Path(__file__).resolve().parents[1]/"openusage_bar"
        for name in ("kiro.py","collector_cli.py","openusage_adapter.py","daily_history.py"):
            source=(root/name).read_text(encoding="utf-8")
            self.assertNotIn("runner or subprocess.run",source)
            self.assertNotIn("capture_output=True",source)
            self.assertIn("run_bounded",source)
