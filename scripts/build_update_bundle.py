#!/usr/bin/env python3
"""Build the online-update bundle attached to GitHub Releases.

Ported from yukkcat/chatgpt2api (AGPL-3.0-only).

Produces ``gptgrok2api-app.tar.gz`` (top-level directory ``gptgrok2api-app``)
and ``checksums.txt`` (sha256). The path list MUST stay identical to
``services.update_service.UPDATE_TARGETS`` (enforced by a unit test).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path

ARCHIVE_NAME = "gptgrok2api-app.tar.gz"
CHECKSUM_NAME = "checksums.txt"
BUNDLE_DIR = "gptgrok2api-app"
BUNDLE_FORMAT = 1
UPDATE_TARGETS = (
    "main.py",
    "VERSION",
    "CHANGELOG.md",
    "GROK2API_LICENSE",
    "config.example.yaml",
    "config.defaults.toml",
    "app",
    "api",
    "services",
    "utils",
    "scripts",
    "package.json",
    "package-lock.json",
    "pyproject.toml",
    "uv.lock",
    "web_dist",
)
_SKIP_PARTS = {"__pycache__", "node_modules", ".git"}
_SKIP_SUFFIXES = (".pyc", ".pyo")


def _iter_files(root: Path, relative: str):
    path = root / relative
    if path.is_file():
        yield relative
        return
    if not path.is_dir():
        raise FileNotFoundError(f"missing bundle path: {relative}")
    for item in sorted(path.rglob("*")):
        if not item.is_file() or item.is_symlink():
            continue
        parts = item.relative_to(root).parts
        if _SKIP_PARTS.intersection(parts) or item.name.endswith(_SKIP_SUFFIXES):
            continue
        yield item.relative_to(root).as_posix()


def build(root: Path, output: Path, version: str) -> Path:
    version = version.strip().removeprefix("v")
    output.mkdir(parents=True, exist_ok=True)
    archive_path = output / ARCHIVE_NAME
    manifest = json.dumps(
        {"format": BUNDLE_FORMAT, "version": version, "paths": list(UPDATE_TARGETS)},
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    with tarfile.open(archive_path, "w:gz") as archive:
        info = tarfile.TarInfo(f"{BUNDLE_DIR}/update-manifest.json")
        info.size = len(manifest)
        info.mtime = 0
        archive.addfile(info, io.BytesIO(manifest))
        for target in UPDATE_TARGETS:
            for relative in _iter_files(root, target):
                archive.add(root / relative, arcname=f"{BUNDLE_DIR}/{relative}", recursive=False)
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    (output / CHECKSUM_NAME).write_text(f"{digest}  {ARCHIVE_NAME}\n", encoding="utf-8")
    return archive_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", default=None, help="defaults to the VERSION file")
    args = parser.parse_args()
    version = args.version or (args.root / "VERSION").read_text(encoding="utf-8").strip()
    print(build(args.root, args.output, version))


if __name__ == "__main__":
    main()
