#!/usr/bin/env python3
from __future__ import annotations

import stat
import struct
import sys
from pathlib import Path


ELF_HEADER = struct.Struct("<16sHHIQQQIHHHHHH")
ELF_SECTION = struct.Struct("<IIQQQQIIQQ")
SQUASHFS_HEADER = struct.Struct("<IIIIIHHHHHHQQ")
APPIMAGE_MAGIC = b"AI\x02"
SQUASHFS_MAGIC = 0x73717368
SQUASHFS_SUPERBLOCK_BYTES = 96
MAX_SECTION_HEADERS = 4096
ARCHITECTURE_MACHINES = {"x64": 62, "arm64": 183}
SAFE_REASONS = frozenset({
    "appimage",
    "architecture",
    "bounds",
    "elf",
    "file",
    "input",
    "squashfs",
})


class AppImagePayloadError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _read_exact(handle, offset: int, size: int) -> bytes:
    try:
        handle.seek(offset)
        payload = handle.read(size)
    except OSError as error:
        raise AppImagePayloadError("file") from error
    if len(payload) != size:
        raise AppImagePayloadError("bounds")
    return payload


def appimage_payload_offset(path: Path, architecture: str) -> int:
    """Return a type-2 AppImage's SquashFS offset without executing it."""

    expected_machine = ARCHITECTURE_MACHINES.get(architecture)
    if expected_machine is None:
        raise AppImagePayloadError("input")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise AppImagePayloadError("file") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AppImagePayloadError("file")
    file_size = metadata.st_size
    if file_size < ELF_HEADER.size + SQUASHFS_SUPERBLOCK_BYTES:
        raise AppImagePayloadError("bounds")

    try:
        with path.open("rb") as handle:
            header = _read_exact(handle, 0, ELF_HEADER.size)
            values = ELF_HEADER.unpack(header)
            ident = values[0]
            if (
                ident[:4] != b"\x7fELF"
                or ident[4] != 2
                or ident[5] != 1
                or ident[6] != 1
            ):
                raise AppImagePayloadError("elf")
            if ident[8:11] != APPIMAGE_MAGIC:
                raise AppImagePayloadError("appimage")

            machine = values[2]
            elf_version = values[3]
            section_table_offset = values[6]
            header_size = values[8]
            section_header_size = values[11]
            section_count = values[12]
            if machine != expected_machine:
                raise AppImagePayloadError("architecture")
            if (
                elf_version != 1
                or header_size != ELF_HEADER.size
                or section_header_size != ELF_SECTION.size
                or not 1 <= section_count <= MAX_SECTION_HEADERS
                or section_table_offset < ELF_HEADER.size
            ):
                raise AppImagePayloadError("elf")

            section_table_end = (
                section_table_offset + section_header_size * section_count
            )
            if section_table_end > file_size:
                raise AppImagePayloadError("bounds")
            payload_offset = section_table_end
            for index in range(section_count):
                section = ELF_SECTION.unpack(
                    _read_exact(
                        handle,
                        section_table_offset + index * section_header_size,
                        section_header_size,
                    )
                )
                section_type = section[1]
                section_offset = section[4]
                section_size = section[5]
                if section_type == 8:  # SHT_NOBITS occupies no bytes in the file.
                    continue
                section_end = section_offset + section_size
                if section_offset > file_size or section_end > file_size:
                    raise AppImagePayloadError("bounds")
                payload_offset = max(payload_offset, section_end)

            if payload_offset + SQUASHFS_SUPERBLOCK_BYTES > file_size:
                raise AppImagePayloadError("bounds")
            superblock = _read_exact(
                handle,
                payload_offset,
                SQUASHFS_SUPERBLOCK_BYTES,
            )
    except AppImagePayloadError:
        raise
    except OSError as error:
        raise AppImagePayloadError("file") from error

    squashfs = SQUASHFS_HEADER.unpack_from(superblock)
    magic = squashfs[0]
    inode_count = squashfs[1]
    block_size = squashfs[3]
    block_log = squashfs[6]
    major = squashfs[9]
    minor = squashfs[10]
    bytes_used = squashfs[12]
    if magic != SQUASHFS_MAGIC or major != 4 or minor != 0 or inode_count == 0:
        raise AppImagePayloadError("squashfs")
    if (
        block_size < 4096
        or block_size > 1024 * 1024
        or block_size & (block_size - 1)
        or block_log >= 63
        or 1 << block_log != block_size
    ):
        raise AppImagePayloadError("squashfs")
    if (
        bytes_used < SQUASHFS_SUPERBLOCK_BYTES
        or payload_offset + bytes_used > file_size
    ):
        raise AppImagePayloadError("bounds")
    return payload_offset


def main(arguments: list[str]) -> int:
    if (
        len(arguments) != 4
        or arguments[0] != "--appimage"
        or arguments[2] != "--architecture"
        or arguments[3] not in ARCHITECTURE_MACHINES
    ):
        print("appimage_payload_invalid reason=input", file=sys.stderr)
        return 2
    try:
        offset = appimage_payload_offset(Path(arguments[1]), arguments[3])
    except AppImagePayloadError as error:
        reason = error.reason if error.reason in SAFE_REASONS else "input"
        print(f"appimage_payload_invalid reason={reason}", file=sys.stderr)
        return 1
    print(offset)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
