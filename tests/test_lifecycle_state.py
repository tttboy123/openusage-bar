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


if __name__ == "__main__":
    unittest.main()
