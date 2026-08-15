from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openusage_bar.lifecycle_state import (
    DELETE_CONFIRMATION,
    LifecycleStateError,
    LifecycleStatePaths,
    current_user_runtime_is_active,
    delete_local_state,
)


class LifecycleStateTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "requires the POSIX account database")
    def test_linux_current_user_home_ignores_mutable_home_environment(self):
        import pwd

        authoritative_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ",
            {"HOME": str(Path(directory) / "hostile-home")},
        ):
            paths = LifecycleStatePaths.for_current_user(platform="linux")

        self.assertEqual(paths.home, authoritative_home)

    def test_windows_current_user_paths_ignore_mutable_profile_environment(self):
        profile = Path("C:/Users/Authoritative")
        local_app_data = profile / "AppData" / "Local"
        with patch(
            "openusage_bar.lifecycle_state._windows_known_folder_path",
            side_effect=(profile, local_app_data),
        ) as known_folder, patch.dict(
            "os.environ",
            {
                "USERPROFILE": "D:/Hostile",
                "LOCALAPPDATA": "D:/Hostile/Local",
            },
        ):
            paths = LifecycleStatePaths.for_current_user(platform="win32")

        self.assertEqual((paths.home, paths.local_app_data), (profile, local_app_data))
        self.assertEqual(known_folder.call_count, 2)

    @unittest.skipUnless(os.name == "posix", "requires POSIX symlinks")
    def test_broken_root_symlink_is_rejected_before_any_state_is_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            state_root = home / ".local" / "state" / "openusage-bar"
            config_root = home / ".config" / "openusage-bar"
            state_root.parent.mkdir(parents=True)
            config_root.mkdir(parents=True)
            state_root.symlink_to(home / "missing-state", target_is_directory=True)
            preserved = config_root / "providers.json"
            preserved.write_text("[]", encoding="utf-8")
            paths = LifecycleStatePaths(platform="linux", home=home)

            with self.assertRaises(LifecycleStateError) as raised:
                delete_local_state(
                    paths,
                    confirmation=DELETE_CONFIRMATION,
                    runtime_is_active=lambda: False,
                )

            self.assertTrue(state_root.is_symlink())
            self.assertEqual(preserved.read_text(encoding="utf-8"), "[]")
            self.assertNotIn(str(home), str(raised.exception))

    @unittest.skipUnless(os.name == "posix", "requires POSIX symlinks")
    def test_symlinked_home_is_rejected_before_following_it_to_state(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            actual_home = base / "actual-home"
            config_root = actual_home / ".config" / "openusage-bar"
            config_root.mkdir(parents=True)
            preserved = config_root / "providers.json"
            preserved.write_text("[]", encoding="utf-8")
            linked_home = base / "linked-home"
            linked_home.symlink_to(actual_home, target_is_directory=True)
            paths = LifecycleStatePaths(platform="linux", home=linked_home)

            with self.assertRaises(LifecycleStateError):
                delete_local_state(
                    paths,
                    confirmation=DELETE_CONFIRMATION,
                    runtime_is_active=lambda: False,
                )

            self.assertEqual(preserved.read_text(encoding="utf-8"), "[]")

    def test_constructed_windows_local_app_data_outside_profile_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            home.mkdir()
            foreign_local_app_data = base / "foreign" / "Local"
            runtime_root = foreign_local_app_data / "openusage-bar"
            runtime_root.mkdir(parents=True)
            preserved = runtime_root / "api.token"
            preserved.write_text("token", encoding="ascii")
            paths = LifecycleStatePaths(
                platform="win32",
                home=home,
                local_app_data=foreign_local_app_data,
            )

            with self.assertRaises(LifecycleStateError):
                delete_local_state(
                    paths,
                    confirmation=DELETE_CONFIRMATION,
                    runtime_is_active=lambda: False,
                )

            self.assertEqual(preserved.read_text(encoding="ascii"), "token")

    def test_all_target_types_are_validated_before_deletion_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            local_app_data = home / "AppData" / "Local"
            local_app_data.mkdir(parents=True)
            paths = LifecycleStatePaths(
                platform="win32",
                home=home,
                local_app_data=local_app_data,
            )
            state_root, config_root, runtime_root = paths.roots
            state_root.mkdir(parents=True)
            config_root.mkdir(parents=True)
            state_file = state_root / "activity.sqlite3"
            config_file = config_root / "providers.json"
            state_file.write_bytes(b"ledger")
            config_file.write_text("[]", encoding="utf-8")
            runtime_root.write_text("hostile", encoding="utf-8")

            with self.assertRaises(LifecycleStateError):
                delete_local_state(
                    paths,
                    confirmation=DELETE_CONFIRMATION,
                    runtime_is_active=lambda: False,
                )

            self.assertEqual(state_file.read_bytes(), b"ledger")
            self.assertEqual(config_file.read_text(encoding="utf-8"), "[]")

    @unittest.skipUnless(os.name == "posix", "requires POSIX symlinks")
    def test_linux_ancestor_swap_after_runtime_probe_preserves_every_target(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            state_parent = home / ".local" / "state"
            state_root = state_parent / "openusage-bar"
            config_root = home / ".config" / "openusage-bar"
            state_root.mkdir(parents=True)
            config_root.mkdir(parents=True)
            state_file = state_root / "activity.sqlite3"
            config_file = config_root / "providers.json"
            state_file.write_bytes(b"owned-ledger")
            config_file.write_text("owned-config", encoding="utf-8")

            foreign_parent = base / "PRIVATE_FOREIGN_STATE"
            foreign_root = foreign_parent / "openusage-bar"
            foreign_root.mkdir(parents=True)
            foreign_file = foreign_root / "foreign.txt"
            foreign_file.write_text("must-survive", encoding="utf-8")
            original_parent = base / "original-state-parent"
            paths = LifecycleStatePaths(platform="linux", home=home)

            def swap_ancestor_after_prevalidation() -> bool:
                state_parent.rename(original_parent)
                state_parent.symlink_to(foreign_parent, target_is_directory=True)
                return False

            with self.assertRaises(LifecycleStateError) as raised:
                delete_local_state(
                    paths,
                    confirmation=DELETE_CONFIRMATION,
                    runtime_is_active=swap_ancestor_after_prevalidation,
                )

            self.assertEqual(
                (original_parent / "openusage-bar" / state_file.name).read_bytes(),
                b"owned-ledger",
            )
            self.assertEqual(config_file.read_text(encoding="utf-8"), "owned-config")
            self.assertEqual(foreign_file.read_text(encoding="utf-8"), "must-survive")
            self.assertNotIn(str(foreign_parent), str(raised.exception))

    @unittest.skipUnless(os.name == "posix", "requires POSIX dirfd operations")
    def test_posix_target_swap_at_delete_open_preserves_foreign_and_all_owned_state(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            paths = LifecycleStatePaths(platform="linux", home=home)
            state_root, config_root = paths.roots
            state_root.mkdir(parents=True)
            config_root.mkdir(parents=True)
            state_file = state_root / "activity.sqlite3"
            config_file = config_root / "providers.json"
            state_file.write_bytes(b"owned-ledger")
            config_file.write_text("owned-config", encoding="utf-8")

            replacement_name = "PRIVATE_FOREIGN_TARGET"
            replacement = state_root.parent / replacement_name
            replacement.mkdir()
            foreign_file = replacement / "foreign.txt"
            foreign_file.write_text("must-survive", encoding="utf-8")
            original_name = "original-openusage-bar"
            original_root = state_root.parent / original_name
            real_open = os.open
            swapped = False

            def swap_at_target_open(path, flags, *args, **kwargs):
                nonlocal swapped
                descriptor = kwargs.get("dir_fd")
                if (
                    not swapped
                    and path == state_root.name
                    and descriptor is not None
                ):
                    self.assertEqual(path, state_root.name)
                    os.rename(
                        state_root.name,
                        original_name,
                        src_dir_fd=descriptor,
                        dst_dir_fd=descriptor,
                    )
                    os.rename(
                        replacement_name,
                        state_root.name,
                        src_dir_fd=descriptor,
                        dst_dir_fd=descriptor,
                    )
                    swapped = True
                return real_open(path, flags, *args, **kwargs)

            with patch(
                "openusage_bar.lifecycle_state.os.open",
                side_effect=swap_at_target_open,
            ):
                with self.assertRaises(LifecycleStateError) as raised:
                    delete_local_state(
                        paths,
                        confirmation=DELETE_CONFIRMATION,
                        runtime_is_active=lambda: False,
                    )

            self.assertTrue(swapped)
            self.assertEqual(
                (original_root / state_file.name).read_bytes(),
                b"owned-ledger",
            )
            self.assertEqual(config_file.read_text(encoding="utf-8"), "owned-config")
            self.assertEqual(
                (state_root / foreign_file.name).read_text(encoding="utf-8"),
                "must-survive",
            )
            self.assertNotIn(str(base), str(raised.exception))

    @unittest.skipUnless(os.name == "posix", "requires POSIX dirfd operations")
    def test_posix_delete_uses_bound_recursive_removal_not_shutil_rmtree(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            paths = LifecycleStatePaths(platform="linux", home=home)
            for root in paths.roots:
                root.mkdir(parents=True)
                (root / "owned.txt").write_text("owned", encoding="utf-8")
            foreign = paths.roots[0].parent / "foreign-sibling"
            foreign.mkdir()
            sentinel = foreign / "sentinel"
            sentinel.write_text("preserve", encoding="utf-8")

            with patch(
                "openusage_bar.lifecycle_state.shutil.rmtree",
                side_effect=AssertionError("obsolete path deletion called"),
            ) as rmtree:
                result = delete_local_state(
                    paths,
                    confirmation=DELETE_CONFIRMATION,
                    runtime_is_active=lambda: False,
                )

            self.assertTrue(result.deleted)
            rmtree.assert_not_called()
            self.assertTrue(all(not root.exists() for root in paths.roots))
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

    def test_registered_service_blocks_delete_even_without_local_api_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir()
            paths = LifecycleStatePaths(platform="linux", home=home)

            active = current_user_runtime_is_active(
                paths,
                service_is_active=lambda: True,
            )

        self.assertTrue(active)

    def test_windows_state_delete_fails_closed_before_probe_or_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            local_app_data = home / "AppData" / "Local"
            local_app_data.mkdir(parents=True)
            paths = LifecycleStatePaths(
                platform="win32",
                home=home,
                local_app_data=local_app_data,
            )
            owned_files = []
            for index, root in enumerate(paths.roots):
                root.mkdir(parents=True)
                owned = root / f"owned-{index}.txt"
                owned.write_text("owned", encoding="utf-8")
                owned_files.append(owned)
            auxiliary = paths.auxiliary_files[0]
            auxiliary.write_text("owned-task", encoding="utf-8")

            credential_sentinel = local_app_data / "credential-manager-sentinel"
            credential_sentinel.write_text("preserved", encoding="utf-8")

            def forbidden_runtime_probe() -> bool:
                raise AssertionError("Windows delete must fail before runtime probe")

            with patch("openusage_bar.lifecycle_state.os.name", "nt"), patch(
                "openusage_bar.lifecycle_state.shutil.rmtree"
            ) as rmtree:
                with self.assertRaises(LifecycleStateError) as raised:
                    delete_local_state(
                        paths,
                        confirmation=DELETE_CONFIRMATION,
                        runtime_is_active=forbidden_runtime_probe,
                    )

            rmtree.assert_not_called()
            self.assertTrue(all(path.exists() for path in owned_files))
            self.assertEqual(auxiliary.read_text(encoding="utf-8"), "owned-task")
            self.assertEqual(
                credential_sentinel.read_text(encoding="utf-8"), "preserved"
            )
            self.assertEqual(str(raised.exception), "state delete unavailable")


class LifecycleStateRuntimeTests(unittest.TestCase):
    def test_runtime_is_active_uses_posix_socket(self) -> None:
        import socket as socket_module

        directory = tempfile.mkdtemp(prefix="/tmp/ls-")
        try:
                home = Path(directory) / "h"
                state_dir = home / ".local" / "state" / "openusage-bar"
                state_dir.mkdir(parents=True)
                paths = LifecycleStatePaths(platform="linux", home=home)
                socket_path = state_dir / "openusage.sock"

                server = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
                server.bind(str(socket_path))
                server.listen(1)
                try:
                    self.assertTrue(
                        current_user_runtime_is_active(
                            paths, service_is_active=lambda: False
                        )
                    )
                finally:
                    server.close()

                self.assertFalse(
                    current_user_runtime_is_active(
                        paths, service_is_active=lambda: False
                    )
                )
        finally:
            import shutil
            shutil.rmtree(directory, ignore_errors=True)

    def test_delete_local_state_fails_closed_on_os_error(self) -> None:
        import openusage_bar.lifecycle_state as lifecycle

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            state_dir = home / ".local" / "state" / "openusage-bar"
            state_dir.mkdir(parents=True)
            paths = LifecycleStatePaths(platform="linux", home=home)
            with patch.object(lifecycle.os, "rmdir", side_effect=OSError("eio")):
                with self.assertRaisesRegex(LifecycleStateError, "state delete failed"):
                    lifecycle.delete_local_state(
                        paths,
                        confirmation=DELETE_CONFIRMATION,
                        runtime_is_active=lambda: False,
                    )

    def test_windows_known_folder_path_fails_closed(self) -> None:
        import openusage_bar.lifecycle_state as lifecycle

        with patch("ctypes.WinDLL", create=True) as win_dll:
            shell32 = win_dll.return_value
            shell32.SHGetKnownFolderPath.return_value = 1
            with self.assertRaisesRegex(LifecycleStateError, "state path unavailable"):
                lifecycle._windows_known_folder_path(
                    lifecycle._WINDOWS_PROFILE_FOLDER_ID
                )


class LifecycleStateEdgeTests(unittest.TestCase):
    def test_runtime_is_active_default_and_windows_branches(self) -> None:
        from unittest.mock import patch as _patch

        from openusage_bar.lifecycle_state import current_user_runtime_is_active

        with _patch(
            "openusage_bar.platform_services.service_is_registered",
            return_value=False,
        ):
            with tempfile.TemporaryDirectory() as directory:
                home = Path(directory) / "home"
                home.mkdir()
                linux = LifecycleStatePaths(platform="linux", home=home)
                self.assertFalse(current_user_runtime_is_active(linux))
                win = LifecycleStatePaths(
                    platform="win32", home=home, local_app_data=home / "appdata"
                )
                self.assertFalse(current_user_runtime_is_active(win))

    def test_validate_target_types_rejects_auxiliary_symlink(self) -> None:
        from openusage_bar.lifecycle_state import _validate_target_types

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir()
            app_data = home / "appdata"
            app_data.mkdir()
            auxiliary = app_data / "openusage-bar-task.xml"
            auxiliary.write_text("<task/>", encoding="utf-8")
            auxiliary.unlink()
            auxiliary.symlink_to(home)
            paths = LifecycleStatePaths(
                platform="win32", home=home, local_app_data=app_data,
            )
            with self.assertRaisesRegex(LifecycleStateError, "state path unsafe"):
                _validate_target_types(paths)


if __name__ == "__main__":
    unittest.main()


class LifecycleStateFailClosedTests(unittest.TestCase):
    def test_validate_parent_rejects_unsafe_inputs(self) -> None:
        from openusage_bar.lifecycle_state import _validate_parent

        with self.assertRaisesRegex(LifecycleStateError, "state path unsafe"):
            _validate_parent(Path("relative"))
        with self.assertRaisesRegex(LifecycleStateError, "state path unsafe"):
            _validate_parent(Path("/"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(LifecycleStateError, "state path unsafe"):
                _validate_parent(link)
            missing = root / "missing"
            with self.assertRaisesRegex(LifecycleStateError, "state path unsafe"):
                _validate_parent(missing)

    def test_validate_descendant_rejects_unsafe_chains(self) -> None:
        from openusage_bar.lifecycle_state import _validate_descendant

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "parent"
            parent.mkdir()
            child = parent / "child"
            child.mkdir()
            _validate_descendant(child, parent)
            with self.assertRaisesRegex(LifecycleStateError, "state path unsafe"):
                _validate_descendant(Path("relative"), parent)
            with self.assertRaisesRegex(LifecycleStateError, "state path unsafe"):
                _validate_descendant(parent, parent)
            outside = root / "outside"
            outside.mkdir()
            with self.assertRaisesRegex(LifecycleStateError, "state path unsafe"):
                _validate_descendant(outside, parent)
            link = child / "link"
            link.symlink_to(parent, target_is_directory=True)
            with self.assertRaisesRegex(LifecycleStateError, "state path unsafe"):
                _validate_descendant(link, parent)

    def test_validate_paths_and_target_types_fail_closed(self) -> None:
        from openusage_bar.lifecycle_state import _validate_paths, _validate_target_types

        with self.assertRaisesRegex(LifecycleStateError, "state path unsafe"):
            _validate_paths(object())
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir()
            paths = LifecycleStatePaths(platform="plan9", home=home)
            with self.assertRaisesRegex(LifecycleStateError, "state path unsafe"):
                _validate_paths(paths)
            paths_win = LifecycleStatePaths(platform="win32", home=home, local_app_data=None)
            with self.assertRaisesRegex(LifecycleStateError, "state path unsafe"):
                _validate_paths(paths_win)
            state_dir = home / ".local" / "state" / "openusage-bar"
            state_dir.parent.mkdir(parents=True)
            state_dir.symlink_to(home, target_is_directory=True)
            with self.assertRaisesRegex(LifecycleStateError, "state path unsafe"):
                _validate_target_types(paths)

    def test_delete_local_state_validation_and_runtime_branches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir()
            paths = LifecycleStatePaths(platform="linux", home=home)
            with self.assertRaisesRegex(LifecycleStateError, "state delete rejected"):
                delete_local_state(paths, confirmation="wrong", runtime_is_active=lambda: False)
            with self.assertRaisesRegex(LifecycleStateError, "state delete rejected"):
                delete_local_state(paths, confirmation=DELETE_CONFIRMATION, runtime_is_active=None)
            with self.assertRaisesRegex(LifecycleStateError, "state runtime active"):
                delete_local_state(paths, confirmation=DELETE_CONFIRMATION, runtime_is_active=lambda: True)
            with self.assertRaisesRegex(LifecycleStateError, "state runtime unavailable"):
                delete_local_state(paths, confirmation=DELETE_CONFIRMATION, runtime_is_active=lambda: (_ for _ in ()).throw(RuntimeError()))

    def test_posix_home_and_known_folder_fail_closed(self) -> None:
        import openusage_bar.lifecycle_state as lifecycle

        with patch("pwd.getpwuid", side_effect=KeyError("no such user")):
            with self.assertRaisesRegex(LifecycleStateError, "state path unavailable"):
                lifecycle._posix_current_home()
        with self.assertRaisesRegex(LifecycleStateError, "unsupported platform"):
            lifecycle.LifecycleStatePaths.for_current_user(platform="plan9")

