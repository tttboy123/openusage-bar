#!/usr/bin/env python3
"""Build a closed public receipt from an online GitHub prerelease read-back."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "github-release-receipt/v1"
REPOSITORY = "tttboy123/openusage-bar"
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TAG = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
RELEASE_ID = re.compile(r"^[1-9]\d*$")
MAX_RELEASE_JSON_BYTES = 1024 * 1024
MAX_ASSET_BYTES = 2 * 1024 * 1024 * 1024


class ReceiptError(ValueError):
    """One intentionally opaque public-receipt failure."""


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise ReceiptError()


def _fail() -> None:
    raise ReceiptError()


def _regular_metadata(path: Path, maximum: int) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        _fail()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > maximum
    ):
        _fail()
    return metadata


def _read_json(path: Path) -> dict[str, Any]:
    _regular_metadata(path, MAX_RELEASE_JSON_BYTES)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _fail()
    if type(payload) is not dict:
        _fail()
    return payload


def _file_record(path: Path) -> dict[str, Any]:
    before = _regular_metadata(path, MAX_ASSET_BYTES)
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                _fail()
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                total += len(chunk)
            after = os.fstat(stream.fileno())
    except ReceiptError:
        raise
    except OSError:
        _fail()
    if (
        total != before.st_size
        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        _fail()
    return {
        "name": path.name,
        "sha256": digest.hexdigest(),
        "sizeBytes": total,
    }


def _expected_names(version: str) -> tuple[str, ...]:
    prefix = f"OpenUsage-Bar-v{version}"
    native = f"{prefix}-macos-arm64"
    return (
        f"{native}.dmg",
        f"{native}.dmg.sha256",
        f"{native}.zip",
        f"{native}.zip.sha256",
        f"{prefix}-manifest.json",
        f"{prefix}-sbom.spdx.json",
    )


def _remote_assets(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    assets = payload.get("assets")
    if type(assets) is not list:
        _fail()
    result: dict[str, dict[str, Any]] = {}
    for value in assets:
        if type(value) is not dict:
            _fail()
        name = value.get("name")
        size = value.get("size")
        digest = value.get("digest")
        if (
            type(name) is not str
            or type(size) is not int
            or size <= 0
            or type(digest) is not str
            or not digest.startswith("sha256:")
            or SHA256.fullmatch(digest.removeprefix("sha256:")) is None
            or name in result
        ):
            _fail()
        result[name] = {
            "sizeBytes": size,
            "sha256": digest.removeprefix("sha256:"),
        }
    return result


def build_receipt(
    *,
    release_json: Path,
    assets_dir: Path,
    repository: str,
    tag: str,
    source_sha: str,
) -> dict[str, Any]:
    match = TAG.fullmatch(tag) if type(tag) is str else None
    if (
        repository != REPOSITORY
        or match is None
        or COMMIT_SHA.fullmatch(source_sha) is None
    ):
        _fail()
    version = ".".join(match.groups())
    names = _expected_names(version)
    try:
        directory = assets_dir.lstat()
    except OSError:
        _fail()
    if stat.S_ISLNK(directory.st_mode) or not stat.S_ISDIR(directory.st_mode):
        _fail()

    try:
        entries = tuple(assets_dir.iterdir())
    except OSError:
        _fail()
    if (
        {entry.name for entry in entries} != set(names)
        or len(entries) != len(names)
    ):
        _fail()

    records = [_file_record(assets_dir / name) for name in names]
    remote = _read_json(release_json)
    release_id_value = remote.get("databaseId")
    if type(release_id_value) is int and release_id_value > 0:
        release_id = str(release_id_value)
    elif type(release_id_value) is str and RELEASE_ID.fullmatch(release_id_value):
        release_id = release_id_value
    else:
        _fail()
    published_at = remote.get("publishedAt")
    if (
        remote.get("tagName") != tag
        or type(remote.get("isPrerelease")) is not bool
        or remote.get("isPrerelease") is not True
        or type(published_at) is not str
    ):
        _fail()
    try:
        datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        _fail()

    remote_assets = _remote_assets(remote)
    if set(remote_assets) != set(names):
        _fail()
    for record in records:
        if remote_assets[record["name"]] != {
            "sha256": record["sha256"],
            "sizeBytes": record["sizeBytes"],
        }:
            _fail()

    canonical_records = json.dumps(
        sorted(records, key=lambda value: value["name"]),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    manifest_name = f"OpenUsage-Bar-v{version}-manifest.json"
    manifest = next(record for record in records if record["name"] == manifest_name)
    return {
        "assetSetSha256": hashlib.sha256(canonical_records).hexdigest(),
        "manifestSha256": manifest["sha256"],
        "prerelease": True,
        "provenanceAttestation": "verified",
        "publishedAt": published_at,
        "releaseId": release_id,
        "repository": repository,
        "schemaVersion": SCHEMA_VERSION,
        "sourceSha": source_sha,
        "tag": tag,
    }


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    encoded = (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                _fail()
            remaining = remaining[written:]
        os.fsync(descriptor)
    except OSError:
        _fail()
    finally:
        if descriptor is not None:
            os.close(descriptor)


def main(arguments: list[str]) -> int:
    parser = SafeArgumentParser(add_help=True)
    parser.add_argument("--schema-version", required=True)
    parser.add_argument("--release-json", type=Path, required=True)
    parser.add_argument("--assets-dir", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    try:
        parsed = parser.parse_args(arguments)
        if parsed.schema_version != SCHEMA_VERSION:
            _fail()
        receipt = build_receipt(
            release_json=parsed.release_json,
            assets_dir=parsed.assets_dir,
            repository=parsed.repository,
            tag=parsed.tag,
            source_sha=parsed.source_sha,
        )
        _write_receipt(parsed.output, receipt)
    except ReceiptError:
        print("github_release_receipt_invalid", file=sys.stderr)
        return 1
    print("github_release_receipt_ok assets=6")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
