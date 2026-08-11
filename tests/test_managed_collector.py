from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openusage_bar.lifecycle_state import LifecycleStatePaths


class ManagedCollectorTests(unittest.TestCase):
    def _frozen_linux(self, source: Path, home: Path):
        return (
            patch("openusage_bar.managed_collector.sys.platform", "linux"),
            patch("openusage_bar.managed_collector.sys.frozen", True, create=True),
            patch("openusage_bar.managed_collector.sys.executable", str(source)),
            patch(
                "openusage_bar.managed_collector.LifecycleStatePaths.for_current_user",
                return_value=LifecycleStatePaths(platform="linux", home=home),
            ),
        )

    @unittest.skipIf(os.name == "nt", "requires POSIX dirfd and file modes")
    def test_install_copies_frozen_self_and_registers_only_the_stable_command(self):
        from openusage_bar import managed_collector

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            source = root / "package" / "openusage-collector"
            source.parent.mkdir()
            source.write_bytes(b"frozen collector")
            source.chmod(0o700)
            calls = []
            patches = self._frozen_linux(source, home)
            with patches[0], patches[1], patches[2], patches[3], patch(
                "openusage_bar.managed_collector.platform_services.install_service",
                side_effect=lambda **kwargs: calls.append(kwargs),
            ):
                managed_collector.install_managed_collector(interval=300)

            stable = (
                home / ".local" / "share" / "usagehub" / "runtime"
                / "openusage-collector"
            )
            self.assertEqual(stable.read_bytes(), b"frozen collector")
            self.assertEqual(stat.S_IMODE(stable.stat().st_mode), 0o700)
            self.assertEqual(calls, [{"interval": 300, "command": str(stable)}])

    @unittest.skipIf(os.name == "nt", "requires POSIX dirfd and file modes")
    def test_install_and_uninstall_honor_one_validated_xdg_data_root(self):
        from openusage_bar import managed_collector

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            data_root = root / "xdg-data"
            source = root / "package" / "openusage-collector"
            source.parent.mkdir()
            source.write_bytes(b"frozen collector")
            source.chmod(0o700)
            calls = []
            patches = self._frozen_linux(source, home)
            with patches[0], patches[1], patches[2], patches[3], patch.dict(
                managed_collector.os.environ,
                {"XDG_DATA_HOME": str(data_root)},
                clear=True,
            ), patch(
                "openusage_bar.managed_collector.platform_services.install_service",
                side_effect=lambda **kwargs: calls.append(("install", kwargs)),
            ), patch(
                "openusage_bar.managed_collector.platform_services.uninstall_service",
                side_effect=lambda: calls.append(("uninstall",)),
            ):
                managed_collector.install_managed_collector(interval=300)
                stable = (
                    data_root / "usagehub" / "runtime" / "openusage-collector"
                )
                self.assertEqual(stable.read_bytes(), b"frozen collector")
                managed_collector.uninstall_managed_collector()

            self.assertEqual(
                calls,
                [
                    ("install", {"interval": 300, "command": str(stable)}),
                    ("uninstall",),
                ],
            )
            self.assertFalse(stable.exists())
            self.assertFalse((data_root / "usagehub" / "runtime").exists())
            self.assertFalse(
                (home / ".local" / "share" / "usagehub").exists()
            )

    @unittest.skipIf(os.name == "nt", "requires POSIX symlink and dirfd semantics")
    def test_install_rejects_symlinked_data_parent_without_touching_foreign(self):
        from openusage_bar import managed_collector

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            foreign = root / "PRIVATE_FOREIGN"
            foreign.mkdir()
            sentinel = foreign / "sentinel"
            sentinel.write_text("preserve", encoding="utf-8")
            (home / ".local").symlink_to(foreign, target_is_directory=True)
            source = root / "openusage-collector"
            source.write_bytes(b"frozen collector")
            source.chmod(0o700)
            patches = self._frozen_linux(source, home)
            with patches[0], patches[1], patches[2], patches[3], patch(
                "openusage_bar.managed_collector.platform_services.install_service"
            ) as install:
                with self.assertRaises(managed_collector.ManagedCollectorError):
                    managed_collector.install_managed_collector()

            self.assertEqual(
                sorted(path.relative_to(foreign) for path in foreign.rglob("*")),
                [Path("sentinel")],
            )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
            install.assert_not_called()

    @unittest.skipIf(os.name == "nt", "requires POSIX dirfd and file modes")
    def test_install_failure_rolls_back_the_identity_bound_stable_copy(self):
        from openusage_bar import managed_collector

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            source = root / "openusage-collector"
            source.write_bytes(b"frozen collector")
            source.chmod(0o700)
            patches = self._frozen_linux(source, home)
            with patches[0], patches[1], patches[2], patches[3], patch(
                "openusage_bar.managed_collector.platform_services.install_service",
                side_effect=RuntimeError("PRIVATE_SERVICE_FAILURE"),
            ):
                with self.assertRaises(managed_collector.ManagedCollectorError):
                    managed_collector.install_managed_collector()

            stable = (
                home / ".local" / "share" / "usagehub" / "runtime"
                / "openusage-collector"
            )
            self.assertFalse(stable.exists())

    @unittest.skipIf(os.name == "nt", "requires POSIX dirfd and file modes")
    def test_fresh_install_does_not_overwrite_a_concurrently_published_stable(
        self,
    ) -> None:
        from openusage_bar import managed_collector

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            source = root / "openusage-collector"
            source.write_bytes(b"frozen collector")
            source.chmod(0o700)
            runtime = home / ".local" / "share" / "usagehub" / "runtime"
            stable = runtime / "openusage-collector"
            self.assertFalse(stable.exists())
            real_link = managed_collector.os.link
            concurrent_identity: tuple[int, int] | None = None

            def publish_foreign_at_link(
                source_name: str,
                target_name: str,
                *args,
                **kwargs,
            ):
                nonlocal concurrent_identity
                runtime_descriptor = kwargs["dst_dir_fd"]
                foreign = os.open(
                    target_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o700,
                    dir_fd=runtime_descriptor,
                )
                try:
                    os.write(foreign, b"PRIVATE_CONCURRENT_STABLE")
                    os.fsync(foreign)
                    metadata = os.fstat(foreign)
                    concurrent_identity = metadata.st_dev, metadata.st_ino
                finally:
                    os.close(foreign)
                return real_link(source_name, target_name, *args, **kwargs)

            patches = self._frozen_linux(source, home)
            with patches[0], patches[1], patches[2], patches[3], patch(
                "openusage_bar.managed_collector.os.link",
                side_effect=publish_foreign_at_link,
            ), patch(
                "openusage_bar.managed_collector.platform_services.install_service"
            ) as install:
                with self.assertRaises(
                    managed_collector.ManagedCollectorError
                ) as captured:
                    managed_collector.install_managed_collector()

            self.assertIsNotNone(concurrent_identity)
            self.assertEqual(
                str(captured.exception), "managed collector action failed"
            )
            self.assertNotIn(str(root), str(captured.exception))
            self.assertNotIn("PRIVATE", str(captured.exception))
            current = stable.lstat()
            self.assertEqual(
                (current.st_dev, current.st_ino), concurrent_identity
            )
            self.assertEqual(
                stable.read_bytes(), b"PRIVATE_CONCURRENT_STABLE"
            )
            self.assertEqual(
                sorted(path.name for path in runtime.iterdir()),
                ["openusage-collector"],
            )
            self.assertEqual(source.read_bytes(), b"frozen collector")
            install.assert_not_called()

    @unittest.skipIf(os.name == "nt", "requires POSIX dirfd and hardlink semantics")
    def test_fresh_install_cleanup_preserves_a_swapped_foreign_temporary(
        self,
    ) -> None:
        from openusage_bar import managed_collector

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            source = root / "openusage-collector"
            source.write_bytes(b"frozen collector")
            source.chmod(0o700)
            runtime = home / ".local" / "share" / "usagehub" / "runtime"
            stable = runtime / "openusage-collector"
            real_link = managed_collector.os.link
            temporary_name: str | None = None
            owned_identity: tuple[int, int] | None = None
            foreign_identity: tuple[int, int] | None = None

            def swap_temporary_at_publish(
                source_name: str,
                target_name: str,
                *args,
                **kwargs,
            ):
                nonlocal temporary_name, owned_identity, foreign_identity
                source_dir = kwargs["src_dir_fd"]
                temporary_name = source_name
                os.rename(
                    source_name,
                    "owned-original",
                    src_dir_fd=source_dir,
                    dst_dir_fd=source_dir,
                )
                owned = os.stat(
                    "owned-original",
                    dir_fd=source_dir,
                    follow_symlinks=False,
                )
                owned_identity = owned.st_dev, owned.st_ino
                foreign = os.open(
                    source_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o700,
                    dir_fd=source_dir,
                )
                try:
                    os.write(foreign, b"PRIVATE_FOREIGN_TEMPORARY")
                    os.fsync(foreign)
                    metadata = os.fstat(foreign)
                    foreign_identity = metadata.st_dev, metadata.st_ino
                finally:
                    os.close(foreign)
                return real_link(source_name, target_name, *args, **kwargs)

            patches = self._frozen_linux(source, home)
            with patches[0], patches[1], patches[2], patches[3], patch(
                "openusage_bar.managed_collector.os.link",
                side_effect=swap_temporary_at_publish,
            ), patch(
                "openusage_bar.managed_collector.platform_services.install_service"
            ) as install:
                with self.assertRaises(
                    managed_collector.ManagedCollectorError
                ) as captured:
                    managed_collector.install_managed_collector()

            self.assertIsNotNone(temporary_name)
            self.assertIsNotNone(owned_identity)
            self.assertIsNotNone(foreign_identity)
            assert temporary_name is not None
            foreign_temporary = runtime / temporary_name
            owned_original = runtime / "owned-original"
            self.assertEqual(
                str(captured.exception), "managed collector action failed"
            )
            self.assertNotIn(str(root), str(captured.exception))
            self.assertNotIn("PRIVATE", str(captured.exception))
            self.assertTrue(foreign_temporary.exists())
            foreign_now = foreign_temporary.lstat()
            self.assertEqual(
                (foreign_now.st_dev, foreign_now.st_ino), foreign_identity
            )
            self.assertEqual(
                foreign_temporary.read_bytes(), b"PRIVATE_FOREIGN_TEMPORARY"
            )
            owned_now = owned_original.lstat()
            self.assertEqual(
                (owned_now.st_dev, owned_now.st_ino), owned_identity
            )
            self.assertEqual(owned_original.read_bytes(), b"frozen collector")
            stable_now = stable.lstat()
            self.assertEqual(
                (stable_now.st_dev, stable_now.st_ino), foreign_identity
            )
            self.assertEqual(stable.read_bytes(), b"PRIVATE_FOREIGN_TEMPORARY")
            self.assertEqual(source.read_bytes(), b"frozen collector")
            install.assert_not_called()

    @unittest.skipIf(os.name == "nt", "requires POSIX open-file rename semantics")
    def test_copy_failure_cleanup_preserves_a_swapped_foreign_temporary(
        self,
    ) -> None:
        from openusage_bar import managed_collector

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            source = root / "openusage-collector"
            source.write_bytes(b"frozen collector")
            source.chmod(0o700)
            runtime = home / ".local" / "share" / "usagehub" / "runtime"
            temporary_name = (
                f".openusage-collector.tmp-{os.getpid()}-{'a' * 16}"
            )
            temporary = runtime / temporary_name
            owned_original = runtime / "owned-original"
            real_write = managed_collector.os.write
            swapped = False
            owned_identity: tuple[int, int] | None = None
            foreign_identity: tuple[int, int] | None = None

            def swap_at_first_write(descriptor: int, payload: bytes) -> int:
                nonlocal swapped, owned_identity, foreign_identity
                if swapped:
                    return real_write(descriptor, payload)
                temporary.rename(owned_original)
                owned = owned_original.lstat()
                owned_identity = owned.st_dev, owned.st_ino
                foreign = os.open(
                    temporary,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o700,
                )
                try:
                    real_write(foreign, b"PRIVATE_FOREIGN_TEMPORARY")
                    os.fsync(foreign)
                    metadata = os.fstat(foreign)
                    foreign_identity = metadata.st_dev, metadata.st_ino
                finally:
                    os.close(foreign)
                swapped = True
                raise OSError("PRIVATE_COPY_FAILURE")

            patches = self._frozen_linux(source, home)
            with patches[0], patches[1], patches[2], patches[3], patch(
                "openusage_bar.managed_collector.secrets.token_hex",
                return_value="a" * 16,
            ), patch(
                "openusage_bar.managed_collector.os.write",
                side_effect=swap_at_first_write,
            ), patch(
                "openusage_bar.managed_collector.platform_services.install_service"
            ) as install:
                with self.assertRaises(
                    managed_collector.ManagedCollectorError
                ) as captured:
                    managed_collector.install_managed_collector()

            self.assertTrue(swapped)
            self.assertIsNotNone(owned_identity)
            self.assertIsNotNone(foreign_identity)
            self.assertEqual(
                str(captured.exception), "managed collector action failed"
            )
            self.assertNotIn(str(root), str(captured.exception))
            self.assertNotIn("PRIVATE", str(captured.exception))
            self.assertTrue(temporary.exists())
            foreign_now = temporary.lstat()
            self.assertEqual(
                (foreign_now.st_dev, foreign_now.st_ino), foreign_identity
            )
            self.assertEqual(
                temporary.read_bytes(), b"PRIVATE_FOREIGN_TEMPORARY"
            )
            owned_now = owned_original.lstat()
            self.assertEqual(
                (owned_now.st_dev, owned_now.st_ino), owned_identity
            )
            self.assertEqual(owned_original.read_bytes(), b"")
            self.assertFalse((runtime / "openusage-collector").exists())
            self.assertEqual(source.read_bytes(), b"frozen collector")
            install.assert_not_called()

    @unittest.skipIf(os.name == "nt", "requires POSIX symlink and dirfd semantics")
    def test_install_rename_cannot_be_redirected_by_runtime_parent_swap(self):
        from openusage_bar import managed_collector

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            product = home / ".local" / "share" / "usagehub"
            runtime = product / "runtime"
            runtime.mkdir(parents=True)
            original_product = root / "original-usagehub"
            foreign_product = root / "PRIVATE_FOREIGN_USAGEHUB"
            foreign_runtime = foreign_product / "runtime"
            foreign_runtime.mkdir(parents=True)
            foreign_stable = foreign_runtime / "openusage-collector"
            foreign_stable.write_bytes(b"preserve foreign")
            source = root / "openusage-collector"
            source.write_bytes(b"frozen collector")
            source.chmod(0o700)
            real_link = managed_collector.os.link
            swapped = False

            def swap_at_link(source_name, target_name, *args, **kwargs):
                nonlocal swapped
                product.rename(original_product)
                product.symlink_to(foreign_product, target_is_directory=True)
                foreign_temporary = foreign_runtime / Path(source_name).name
                foreign_temporary.write_bytes(b"attacker temporary")
                swapped = True
                return real_link(source_name, target_name, *args, **kwargs)

            patches = self._frozen_linux(source, home)
            with patches[0], patches[1], patches[2], patches[3], patch(
                "openusage_bar.managed_collector.os.link",
                side_effect=swap_at_link,
            ), patch(
                "openusage_bar.managed_collector.platform_services.install_service"
            ) as install:
                with self.assertRaises(
                    managed_collector.ManagedCollectorError
                ) as captured:
                    managed_collector.install_managed_collector()

            self.assertTrue(swapped)
            self.assertEqual(
                str(captured.exception), "managed collector action failed"
            )
            self.assertNotIn(str(foreign_product), str(captured.exception))
            self.assertNotIn("PRIVATE", str(captured.exception))
            self.assertEqual(foreign_stable.read_bytes(), b"preserve foreign")
            install.assert_not_called()

    @unittest.skipIf(os.name == "nt", "requires POSIX dirfd and file modes")
    def test_install_rejects_source_change_during_atomic_copy(self):
        from openusage_bar import managed_collector

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            source = root / "openusage-collector"
            source.write_bytes(b"frozen collector version one")
            source.chmod(0o700)
            real_link = managed_collector.os.link
            mutated = False

            def mutate_source_at_link(source_name, target_name, *args, **kwargs):
                nonlocal mutated
                with source.open("ab") as stream:
                    stream.write(b" private mutation")
                mutated = True
                return real_link(source_name, target_name, *args, **kwargs)

            patches = self._frozen_linux(source, home)
            with patches[0], patches[1], patches[2], patches[3], patch(
                "openusage_bar.managed_collector.os.link",
                side_effect=mutate_source_at_link,
            ), patch(
                "openusage_bar.managed_collector.platform_services.install_service"
            ) as install:
                with self.assertRaises(managed_collector.ManagedCollectorError):
                    managed_collector.install_managed_collector()

            stable = (
                home / ".local" / "share" / "usagehub" / "runtime"
                / "openusage-collector"
            )
            self.assertTrue(mutated)
            self.assertFalse(stable.exists())
            install.assert_not_called()

    @unittest.skipIf(os.name == "nt", "requires POSIX symlink and dirfd semantics")
    def test_uninstall_detects_runtime_parent_swap_without_deleting_foreign(self):
        from openusage_bar import managed_collector

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            runtime = home / ".local" / "share" / "usagehub" / "runtime"
            runtime.mkdir(parents=True)
            stable = runtime / "openusage-collector"
            stable.write_bytes(b"owned stable")
            stable.chmod(0o700)
            original = root / "original-runtime"
            foreign = root / "PRIVATE_FOREIGN_RUNTIME"
            foreign.mkdir()
            foreign_stable = foreign / stable.name
            foreign_stable.write_bytes(b"preserve foreign")
            source = root / "packaged-collector"
            source.write_bytes(b"frozen collector")
            source.chmod(0o700)

            def swap_after_service_removal() -> None:
                runtime.rename(original)
                runtime.symlink_to(foreign, target_is_directory=True)

            patches = self._frozen_linux(source, home)
            with patches[0], patches[1], patches[2], patches[3], patch(
                "openusage_bar.managed_collector.platform_services.uninstall_service",
                side_effect=swap_after_service_removal,
            ):
                with self.assertRaises(managed_collector.ManagedCollectorError):
                    managed_collector.uninstall_managed_collector()

            self.assertEqual((original / stable.name).read_bytes(), b"owned stable")
            self.assertEqual(foreign_stable.read_bytes(), b"preserve foreign")

    @unittest.skipIf(os.name == "nt", "requires POSIX dirfd and file modes")
    def test_uninstall_removes_stable_copy_but_preserves_product_siblings(self):
        from openusage_bar import managed_collector

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            product = home / ".local" / "share" / "usagehub"
            runtime = product / "runtime"
            runtime.mkdir(parents=True)
            stable = runtime / "openusage-collector"
            stable.write_bytes(b"owned stable")
            stable.chmod(0o700)
            sibling = product / "keep.txt"
            sibling.write_text("preserve", encoding="utf-8")
            source = root / "packaged-collector"
            source.write_bytes(b"frozen collector")
            source.chmod(0o700)
            patches = self._frozen_linux(source, home)
            with patches[0], patches[1], patches[2], patches[3], patch(
                "openusage_bar.managed_collector.platform_services.uninstall_service"
            ) as uninstall:
                managed_collector.uninstall_managed_collector()

            uninstall.assert_called_once_with()
            self.assertFalse(stable.exists())
            self.assertFalse(runtime.exists())
            self.assertEqual(sibling.read_text(encoding="utf-8"), "preserve")

    def test_non_linux_or_non_frozen_execution_fails_closed(self):
        from openusage_bar import managed_collector

        for platform, frozen in (("darwin", True), ("linux", False)):
            with self.subTest(platform=platform, frozen=frozen), patch(
                "openusage_bar.managed_collector.sys.platform", platform
            ), patch(
                "openusage_bar.managed_collector.sys.frozen", frozen, create=True
            ):
                with self.assertRaises(managed_collector.ManagedCollectorError):
                    managed_collector.install_managed_collector()


if __name__ == "__main__":
    unittest.main()
