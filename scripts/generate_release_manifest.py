#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path


REQUIREMENT = re.compile(
    r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.-]+) --hash=sha256:([0-9a-f]{64})$"
)
SHA = re.compile(r"^[0-9a-f]{40}$")


class ManifestError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode()


def _requirements(path: Path) -> list[dict[str, str]]:
    rows = []
    for line in path.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        match = REQUIREMENT.fullmatch(line)
        if match is None:
            raise ManifestError("requirements")
        rows.append({"name": match.group(1), "version": match.group(2), "sha256": match.group(3)})
    return sorted(rows, key=lambda row: row["name"].casefold())


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise ManifestError("git")
    return result.stdout.strip()


def _executables(app: Path) -> list[dict[str, object]]:
    rows = []
    for path in sorted(item for item in app.rglob("*") if item.is_file()):
        if os.stat(path, follow_symlinks=False).st_mode & 0o111:
            rows.append({
                "path": path.relative_to(app).as_posix(),
                "sha256": _sha256(path),
                "size": path.stat().st_size,
            })
    return rows


def generate(
    *, root: Path, app: Path, archive: Path, requirements: Path,
    swift_dependencies: dict[str, object], commit: str, commit_time: str,
    manifest_path: Path, sbom_path: Path,
) -> None:
    if SHA.fullmatch(commit) is None:
        raise ManifestError("commit")
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    version = info["CFBundleShortVersionString"]
    build = info["CFBundleVersion"]
    minimum = info["LSMinimumSystemVersion"]
    if not all(isinstance(item, str) and item for item in (version, build, minimum)):
        raise ManifestError("metadata")
    dependencies = _requirements(requirements)
    executables = _executables(app)
    checksum = Path(f"{archive}.sha256")
    if not archive.is_file() or not checksum.is_file() or not executables:
        raise ManifestError("artifact")

    app_spdx = "SPDXRef-Package-OpenUsage-Bar"
    packages = [{
        "SPDXID": app_spdx, "name": "OpenUsage Bar", "versionInfo": version,
        "downloadLocation": "NOASSERTION", "filesAnalyzed": True,
        "licenseConcluded": "NOASSERTION", "licenseDeclared": "NOASSERTION",
    }]
    relationships = [{
        "spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES",
        "relatedSpdxElement": app_spdx,
    }]
    for index, dependency in enumerate(dependencies, 1):
        spdx_id = f"SPDXRef-Package-Python-{index}"
        packages.append({
            "SPDXID": spdx_id, "name": dependency["name"],
            "versionInfo": dependency["version"], "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False, "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION",
        })
        relationships.append({
            "spdxElementId": app_spdx, "relationshipType": "DEPENDS_ON",
            "relatedSpdxElement": spdx_id,
        })
    files = [
        {
            "SPDXID": f"SPDXRef-File-{index}", "fileName": row["path"],
            "checksums": [{"algorithm": "SHA256", "checksumValue": row["sha256"]}],
            "licenseConcluded": "NOASSERTION",
        }
        for index, row in enumerate(executables, 1)
    ]
    sbom = {
        "spdxVersion": "SPDX-2.3", "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT", "name": f"OpenUsage-Bar-{version}",
        "documentNamespace": f"https://github.com/tttboy123/openusage-bar/releases/{version}/{commit}",
        "creationInfo": {"created": commit_time, "creators": ["Tool: OpenUsage-Bar-release-manifest/1"]},
        "packages": packages, "files": files, "relationships": relationships,
    }
    sbom_path.write_bytes(_json_bytes(sbom))
    assets = [
        {"name": archive.name, "sha256": _sha256(archive), "size": archive.stat().st_size},
        {"name": checksum.name, "sha256": _sha256(checksum), "size": checksum.stat().st_size},
        {"name": sbom_path.name, "sha256": _sha256(sbom_path), "size": sbom_path.stat().st_size},
    ]
    manifest = {
        "schemaVersion": 1,
        "gitCommit": commit,
        "product": {
            "name": "OpenUsage Bar", "version": version, "build": build,
            "minimumMacOS": minimum, "architecture": "arm64",
        },
        "pythonDependencies": dependencies,
        "swiftDependencies": swift_dependencies,
        "executables": executables,
        "publishedAssets": sorted(assets, key=lambda row: row["name"]),
    }
    manifest_path.write_bytes(_json_bytes(manifest))


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--swift-package", type=Path)
    parser.add_argument("--swift-dependencies", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sbom-output", type=Path, required=True)
    parser.add_argument("--commit")
    parser.add_argument("--commit-time")
    parsed = parser.parse_args(arguments)
    root = Path.cwd().resolve()
    try:
        commit = parsed.commit or _git(root, "rev-parse", "HEAD")
        commit_time = parsed.commit_time or _git(root, "show", "-s", "--format=%cI", commit)
        if parsed.swift_dependencies:
            swift = json.loads(parsed.swift_dependencies.read_text("utf-8"))
        elif parsed.swift_package:
            result = subprocess.run(
                ["swift", "package", "--package-path", str(parsed.swift_package),
                 "show-dependencies", "--format", "json"],
                capture_output=True, text=True, check=False,
            )
            if result.returncode != 0:
                raise ManifestError("swift")
            swift = json.loads(result.stdout)
        else:
            raise ManifestError("swift")
        if not isinstance(swift, dict):
            raise ManifestError("swift")
        generate(
            root=root, app=parsed.app, archive=parsed.archive,
            requirements=parsed.requirements, swift_dependencies=swift,
            commit=commit, commit_time=commit_time,
            manifest_path=parsed.output, sbom_path=parsed.sbom_output,
        )
    except (ManifestError, OSError, UnicodeError, ValueError, json.JSONDecodeError, plistlib.InvalidFileException):
        print("release_manifest_invalid", file=sys.stderr)
        return 1
    print("release_manifest_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
