import subprocess
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/activity_install_process.sh"
INSTALL = ROOT / "scripts/install_app.sh"


class ActivityInstallProcessTests(unittest.TestCase):
    def build_process(
        self,
        root: Path,
        relative: str,
        *,
        ignores_term: bool = False,
        term_marker: Path | None = None,
        launch_marker: Path | None = None,
    ) -> Path:
        executable = root / relative
        executable.parent.mkdir(parents=True, exist_ok=True)
        source = executable.with_suffix(".c")
        def c_path(path: Path) -> str:
            return str(path).replace("\\", "\\\\").replace('"', '\\"')

        if term_marker is not None:
            term_handler = textwrap.dedent(
                f"""
                static void handle_term(int value) {{
                    (void)value;
                    int fd = open("{c_path(term_marker)}", O_WRONLY | O_CREAT, 0600);
                    if (fd >= 0) close(fd);
                }}
                """
            )
            handler = "signal(SIGTERM, handle_term);"
        else:
            term_handler = ""
            handler = "signal(SIGTERM, SIG_IGN);" if ignores_term else ""
        launch = ""
        if launch_marker is not None:
            launch = (
                f'int fd = open("{c_path(launch_marker)}", O_WRONLY | O_CREAT, 0600); '
                "if (fd >= 0) close(fd);"
            )
        source.write_text(
            textwrap.dedent(
                f"""
                #include <fcntl.h>
                #include <signal.h>
                #include <unistd.h>
                {term_handler}
                int main(void) {{
                    {handler}
                    {launch}
                    for (;;) pause();
                }}
                """
            ),
            encoding="utf-8",
        )
        subprocess.run(
            ["/usr/bin/clang", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(executable)],
            check=True,
        )
        return executable

    def run_helper(self, body: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/zsh", "-c", f'source "{HELPER}"\n{body}'],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def wait_for_command(self, process: subprocess.Popen, expected: str) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            observed = subprocess.run(
                ["/bin/ps", "-ww", "-p", str(process.pid), "-o", "command="],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            if observed == expected:
                return
            time.sleep(0.005)
        self.fail("fixture process command did not become stable")

    def wait_for_marker(
        self,
        marker: Path,
        process: subprocess.Popen | None = None,
        *,
        timeout: float = 5,
    ) -> None:
        deadline = time.monotonic() + timeout
        while not marker.exists() and time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                self.fail("fixture process exited before becoming ready")
            time.sleep(0.005)
        self.assertTrue(marker.exists(), "fixture process did not become ready")

    def test_stop_matches_only_the_exact_full_activity_command(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = self.build_process(
                root,
                "Applications/OpenUsage Bar.app/Contents/Helpers/OpenUsage Activity.app/Contents/MacOS/OpenUsage Activity",
            )
            same_name = self.build_process(
                root,
                "Elsewhere/OpenUsage Activity",
            )
            target_process = subprocess.Popen([str(target)])
            argument_process = subprocess.Popen([str(target), "--different-command"])
            unrelated_process = subprocess.Popen([str(same_name)])
            try:
                self.wait_for_command(target_process, str(target))
                self.wait_for_command(
                    argument_process, f"{target} --different-command"
                )
                self.wait_for_command(unrelated_process, str(same_name))
                result = self.run_helper(f'stop_exact_activity_processes "{target}" 20 0.01')
                self.assertEqual(result.returncode, 0, result.stderr)
                target_process.wait(timeout=2)
                self.assertIsNone(argument_process.poll())
                self.assertIsNone(unrelated_process.poll())
            finally:
                for process in (target_process, argument_process, unrelated_process):
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=2)

    def test_stop_accepts_only_supported_activity_route_arguments(self):
        with tempfile.TemporaryDirectory() as temp:
            target = self.build_process(Path(temp), "OpenUsage Activity")
            supported_routes = (
                "activity",
                "capacity",
                "api-spend",
                "local-tools",
                "providers",
                "health",
                "automation",
            )
            supported = [
                subprocess.Popen([str(target), "--route", route])
                for route in supported_routes
            ]
            unknown_route = subprocess.Popen(
                [str(target), "--route", "unknown-route"]
            )
            unrelated_arguments = subprocess.Popen(
                [str(target), "--different-command"]
            )
            try:
                result = self.run_helper(
                    f'stop_exact_activity_processes "{target}" 20 0.01'
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                for process in supported:
                    process.wait(timeout=2)
                self.assertIsNone(unknown_route.poll())
                self.assertIsNone(unrelated_arguments.poll())
            finally:
                for process in [*supported, unknown_route, unrelated_arguments]:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=2)

    def test_stop_escalates_to_kill_after_a_bounded_term_wait(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ready = root / "ready"
            target = self.build_process(
                root, "OpenUsage Activity", ignores_term=True, launch_marker=ready,
            )
            process = subprocess.Popen([str(target)])
            self.wait_for_marker(ready, process)
            signals = root / "signals"
            signaler = root / "record-signal"
            signaler.write_text(
                f'#!/bin/zsh\nprint -r -- "$1" >> "{signals}"\n/bin/kill "$1" "$2"\n',
                encoding="utf-8",
            )
            signaler.chmod(0o755)
            try:
                result = self.run_helper(
                    f'stop_exact_activity_processes "{target}" 4 0.01 "{signaler}"'
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                process.wait(timeout=2)
                self.assertEqual(
                    signals.read_text(encoding="utf-8").splitlines(),
                    ["-TERM", "-TERM", "-KILL"],
                )
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)

    def test_enumeration_is_set_e_safe_with_and_without_a_match(self):
        with tempfile.TemporaryDirectory() as temp:
            target = self.build_process(Path(temp), "OpenUsage Activity")
            process = subprocess.Popen([str(target)])
            try:
                for expected in (str(target), str(target) + ".missing"):
                    result = self.run_helper(
                        f'set -e\npids=$(activity_exact_pids "{expected}")\nprint -r -- reached'
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("reached", result.stdout)
            finally:
                process.kill()
                process.wait(timeout=2)

    def test_stop_is_set_e_safe_when_it_clears_a_real_match(self):
        with tempfile.TemporaryDirectory() as temp:
            target = self.build_process(Path(temp), "OpenUsage Activity")
            process = subprocess.Popen([str(target)])
            try:
                result = self.run_helper(
                    f'set -e\nstop_exact_activity_processes "{target}" 10 0.01\n'
                    'print -r -- "reached signalled=$ACTIVITY_STOP_SIGNALLED"'
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("reached signalled=1", result.stdout)
                process.wait(timeout=2)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)

    def test_stop_reenumerates_a_second_exact_process_started_during_term_wait(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / "term-seen"
            ready = root / "ready"
            target = self.build_process(
                root, "OpenUsage Activity", term_marker=marker, launch_marker=ready,
            )
            first = subprocess.Popen([str(target)])
            self.wait_for_marker(ready, first)
            spawned: list[subprocess.Popen[bytes]] = []

            def respawn() -> None:
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.001)
                if marker.exists():
                    spawned.append(subprocess.Popen([str(target)]))

            thread = threading.Thread(target=respawn)
            thread.start()
            try:
                result = self.run_helper(f'stop_exact_activity_processes "{target}" 20 0.01')
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(spawned, "fixture did not start the replacement process")
                first.wait(timeout=2)
                spawned[0].wait(timeout=2)
                self.assertFalse(self.run_helper(f'activity_has_exact_process "{target}"').returncode == 0)
            finally:
                for process in [first, *spawned]:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=2)

    def test_stop_observes_a_process_started_after_an_initial_empty_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ready = root / "ready"
            target = self.build_process(
                root, "OpenUsage Activity", launch_marker=ready,
            )
            first_snapshot = root / "first-empty-snapshot"
            try:
                result = self.run_helper(
                    'functions[real_activity_exact_processes]=$functions[activity_exact_processes]\n'
                    'activity_exact_processes() {\n'
                    f'  if [[ ! -e "{first_snapshot}" ]]; then\n'
                    '    snapshot=$(real_activity_exact_processes "$@") || return 1\n'
                    '    [[ -z "$snapshot" ]] || return 91\n'
                    f'    : > "{first_snapshot}"\n'
                    f'    "{target}" >/dev/null 2>&1 &!\n'
                    '    for attempt in {1..2000}; do\n'
                    f'      [[ -e "{ready}" ]] && break\n'
                    '      sleep 0.001\n'
                    '    done\n'
                    f'    [[ -e "{ready}" ]] || return 90\n'
                    '    print -r -- "$snapshot"\n'
                    '    return 0\n'
                    '  fi\n'
                    '  real_activity_exact_processes "$@"\n'
                    '}\n'
                    f'stop_exact_activity_processes "{target}" 10 0.01'
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(
                    ready.exists(),
                    "fixture did not launch after the injected empty snapshot",
                )
                self.assertNotEqual(
                    self.run_helper(f'activity_has_exact_process "{target}"').returncode,
                    0,
                )
            finally:
                self.run_helper(f'stop_exact_activity_processes "{target}" 10 0.01')

    def test_signal_race_treats_a_gone_process_as_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = self.build_process(root, "OpenUsage Activity", ignores_term=True)
            process = subprocess.Popen([str(target)])
            signaler = root / "gone-before-signal-result"
            signaler.write_text("#!/bin/zsh\n/bin/kill -KILL \"$2\" 2>/dev/null || true\nexit 1\n", encoding="utf-8")
            signaler.chmod(0o755)
            try:
                result = self.run_helper(
                    f'identity=$(activity_process_identity {process.pid} "{target}")\n'
                    f'signal_exact_activity_pid "{target}" {process.pid} "$identity" TERM "{signaler}"\n'
                    'print -r -- "signalled=$ACTIVITY_STOP_SIGNALLED"'
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("signalled=0", result.stdout)
                process.wait(timeout=2)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)

    def test_wrong_start_identity_is_never_signalled(self):
        with tempfile.TemporaryDirectory() as temp:
            target = self.build_process(Path(temp), "OpenUsage Activity")
            process = subprocess.Popen([str(target)])
            try:
                result = self.run_helper(
                    f'identity=$(activity_process_identity {process.pid} "{target}")\n'
                    f'signal_exact_activity_pid "{target}" {process.pid} "wrong-$identity" TERM'
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsNone(process.poll())
            finally:
                process.kill()
                process.wait(timeout=2)

    def test_persistent_signal_failure_exhausts_one_total_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = self.build_process(root, "OpenUsage Activity")
            process = subprocess.Popen([str(target)])
            deny = root / "deny-signal"
            calls = root / "signal-calls"
            deny.write_text(
                f'#!/bin/zsh\nprint call >> "{calls}"\nexit 1\n',
                encoding="utf-8",
            )
            deny.chmod(0o755)
            try:
                result = self.run_helper(f'stop_exact_activity_processes "{target}" 4 0.02 "{deny}"')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), ["call"] * 4)
                self.assertIsNone(process.poll())
            finally:
                process.kill()
                process.wait(timeout=2)

    def test_rollback_stop_failure_never_reopens_mixed_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "OpenUsage Activity.app"
            ready = root / "ready"
            target = self.build_process(
                app, "Contents/MacOS/OpenUsage Activity", launch_marker=ready,
            )
            process = subprocess.Popen([str(target)])
            try:
                self.wait_for_marker(ready, process)
                deny = root / "deny-signal"
                deny.write_text("#!/bin/zsh\nexit 1\n", encoding="utf-8")
                deny.chmod(0o755)
                target_state = root / "target-state"
                target_state.write_text("new", encoding="utf-8")
                live_plist = root / "live-plist"
                live_plist.write_text("new", encoding="utf-8")
                opened = root / "opened"
                opener = root / "open-fixture"
                opener.write_text(
                    f'#!/bin/zsh\nprint opened > "{opened}"\n', encoding="utf-8"
                )
                opener.chmod(0o755)
                result = self.run_helper(
                    'original_rc=73\n'
                    f'if clear_activity_for_runtime_rollback "{target}" 4 0.01 "{deny}"; then\n'
                    f'  print old > "{target_state}"\n'
                    f'  print old > "{live_plist}"\n'
                    f'  "{opener}" "{app}"\n'
                    'fi\n'
                    'exit $original_rc'
                )
                self.assertEqual(result.returncode, 73)
                self.assertIn("runtime rollback incomplete", result.stderr)
                self.assertEqual(target_state.read_text(encoding="utf-8"), "new")
                self.assertEqual(live_plist.read_text(encoding="utf-8"), "new")
                self.assertFalse(opened.exists())
                self.assertIsNone(process.poll())
            finally:
                self.run_helper(f'stop_exact_activity_processes "{target}" 10 0.01')
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)

    def test_rollback_after_swap_clears_a_late_activity_and_marks_reopen(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ready = root / "ready"
            target = self.build_process(
                root, "OpenUsage Activity", launch_marker=ready,
            )
            process = subprocess.Popen([str(target)])
            self.wait_for_marker(ready, process)
            try:
                result = self.run_helper(
                    'SWAPPED=1\nFIRST_INSTALLED=0\nACTIVITY_STOPPED=0\n'
                    'if (( SWAPPED || FIRST_INSTALLED || ACTIVITY_STOPPED )); then\n'
                    f'  clear_activity_for_runtime_rollback "{target}" 10 0.01\n'
                    '  if (( ACTIVITY_STOP_SIGNALLED )); then ACTIVITY_STOPPED=1; fi\n'
                    'fi\n'
                    'print -r -- "stopped=$ACTIVITY_STOPPED"'
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("stopped=1", result.stdout)
                process.wait(timeout=2)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)

    def test_swap_boundary_clears_old_image_before_reopening_new_binary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "OpenUsage Activity.app"
            target = self.build_process(app, "Contents/MacOS/OpenUsage Activity")
            old = subprocess.Popen([str(target)])
            old_between_swap = None
            new_process = None
            try:
                self.assertEqual(self.run_helper(f'stop_exact_activity_processes "{target}" 10 0.01').returncode, 0)
                old.wait(timeout=2)

                old_between_swap = subprocess.Popen([str(target)])
                replacement_root = root / "replacement"
                new_marker = root / "new-image-launched"
                replacement = self.build_process(
                    replacement_root, "OpenUsage Activity", launch_marker=new_marker,
                )
                replacement.replace(target)

                self.assertEqual(self.run_helper(f'stop_exact_activity_processes "{target}" 10 0.01').returncode, 0)
                old_between_swap.wait(timeout=2)
                new_process = subprocess.Popen([str(target)])
                deadline = time.monotonic() + 5
                while not new_marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(new_marker.exists())
            finally:
                for process in (old, old_between_swap, new_process):
                    if process is None:
                        continue
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=2)

    def test_reopen_uses_requested_bundle_and_verifies_exact_process(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "restored/OpenUsage Activity.app"
            executable = self.build_process(app, "Contents/MacOS/OpenUsage Activity")
            opened = root / "opened"
            opener = root / "open-fixture"
            opener.write_text(
                "#!/bin/zsh\nprint -r -- \"$1\" > \"$OPENED_MARKER\"\n\"$ACTIVITY_EXECUTABLE\" >/dev/null 2>&1 &!\n",
                encoding="utf-8",
            )
            opener.chmod(0o755)
            try:
                result = subprocess.run(
                    [
                        "/bin/zsh", "-c",
                        f'source "{HELPER}"\nreopen_exact_activity "{app}" "{executable}" "{opener}" 20 0.01',
                    ],
                    capture_output=True,
                    text=True,
                    env={
                        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                        "OPENED_MARKER": str(opened),
                        "ACTIVITY_EXECUTABLE": str(executable),
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(opened.read_text(encoding="utf-8").strip(), str(app))
            finally:
                self.run_helper(f'stop_exact_activity_processes "{executable}" 20 0.01')

    def test_install_reopens_only_activity_on_success_and_rollback(self):
        source = INSTALL.read_text(encoding="utf-8")
        helper = HELPER.read_text(encoding="utf-8")

        self.assertIn('source "$ROOT/scripts/activity_install_process.sh"', source)
        self.assertIn('ACTIVITY_EXECUTABLE="$TARGET/Contents/Helpers/OpenUsage Activity.app/Contents/MacOS/OpenUsage Activity"', source)
        self.assertIn('ACTIVITY_WAS_RUNNING=1', source)
        self.assertIn('stop_exact_activity_processes "$ACTIVITY_EXECUTABLE"', source)
        self.assertEqual(source.count('reopen_exact_activity "$ACTIVITY_APP" "$ACTIVITY_EXECUTABLE"'), 2)
        self.assertIn('clear_activity_for_runtime_rollback "$ACTIVITY_EXECUTABLE"', source)
        self.assertNotIn("Provider Settings.app/Contents/MacOS", source)
        self.assertNotIn("pkill", source)
        self.assertNotIn("killall", source)
        self.assertNotIn("osascript", source)
        self.assertIn('activity_process_matches "$pid" "$expected" "$started" || return 0', helper)
        self.assertIn('"$signaler" "-$signal" "$pid"', helper)
        self.assertIn("ACTIVITY_STOP_MATCHED=0", helper)
        self.assertIn("ACTIVITY_STOP_SIGNALLED=0", helper)
        self.assertIn("if (( ACTIVITY_STOP_SIGNALLED )); then", source)

        rollback = source.index("rollback()")
        rollback_end = source.index("trap rollback", rollback)
        rollback_source = source[rollback:rollback_end]
        self.assertIn("if (( SWAPPED || FIRST_INSTALLED || ACTIVITY_STOPPED )); then", rollback_source)
        rollback_clear = source.index('clear_activity_for_runtime_rollback "$ACTIVITY_EXECUTABLE"', rollback)
        rollback_reopen = source.index('reopen_exact_activity "$ACTIVITY_APP" "$ACTIVITY_EXECUTABLE"', rollback)
        rollback_restore = source.index("rollback_bundle_transaction", rollback)
        success_reopen = source.rindex('reopen_exact_activity "$ACTIVITY_APP" "$ACTIVITY_EXECUTABLE"')
        installed_verify = source.index('codesign --verify --deep --strict "$TARGET"')
        install_swap = source.index('install_bundle_transaction "$ATOMIC_SWAP" "$TARGET" "$NEW"')
        post_swap_stop = source.index('stop_exact_activity_processes "$ACTIVITY_EXECUTABLE"', install_swap)
        self.assertLess(rollback_clear, rollback_restore)
        self.assertIn("if (( activity_runtime_cleared )); then", source[rollback:rollback_restore])
        self.assertLess(rollback_restore, rollback_reopen)
        self.assertIn(
            "if (( HAD_TARGET && ACTIVITY_STOPPED && activity_runtime_cleared && bundle_restored )); then",
            rollback_source,
        )
        self.assertIn('exit "$code"', rollback_source)
        self.assertLess(install_swap, post_swap_stop)
        self.assertLess(installed_verify, success_reopen)


if __name__ == "__main__":
    unittest.main()
