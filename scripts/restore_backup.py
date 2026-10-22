#!/usr/bin/env python3
"""Verify and restore a chatgpt2api backup archive (backup-*.tar.gz / .tar.gz.enc).

Ported in spirit from yukkcat/chatgpt2api (AGPL-3.0-only) deployment runbook.

By default the script is READ-ONLY: it decrypts, unpacks in memory, validates
``backup-metadata.json`` and the SHA-256 manifest, and lists the contents.
Nothing is written to disk until ``--apply`` is passed.

``--apply`` first snapshots the current ``data/`` (and ``config.json``) next to
the target, then writes the archive members. Safety rules:

* SQLite database snapshots are NEVER written over the live database unless you
  pass ``--sqlite-target``; by default they land at
  ``data/application-database.sqlite3`` for you to move into place.
* PostgreSQL dumps are never restored automatically: the exact ``pg_restore``
  command is printed instead.
* Stop the service before ``--apply``; never restore into a running instance.

Examples::

    # verify only (decrypting with the passphrase from the environment)
    CHATGPT2API_BACKUP_PASSPHRASE=secret python scripts/restore_backup.py backup-20260101T000000Z-ab12.tar.gz.enc

    # restore into the project directory (stop the service first)
    python scripts/restore_backup.py backup.tar.gz --target . --apply
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path, PurePosixPath

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_INVALID = 2

SQLITE_SNAPSHOT = "data/application-database.sqlite3"
PGDUMP_SNAPSHOT = "data/application-database.pgdump"
METADATA_NAME = "backup-metadata.json"
MANIFEST_NAME = "backup-manifest.json"
PASSPHRASE_ENV = "CHATGPT2API_BACKUP_PASSPHRASE"


class RestoreError(RuntimeError):
    pass


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def decrypt_openssl(data: bytes, passphrase: str) -> bytes:
    """Decrypt the ``openssl enc -aes-256-cbc -pbkdf2`` payload produced by the backup service."""
    env = dict(os.environ)
    env[PASSPHRASE_ENV] = passphrase
    try:
        result = subprocess.run(
            ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-md", "sha256", "-pass", f"env:{PASSPHRASE_ENV}"],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            env=env,
        )
    except FileNotFoundError as exc:
        raise RestoreError("当前环境缺少 openssl，无法解密备份") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RestoreError(f"解密失败（口令错误或文件损坏）：{detail or 'openssl 执行失败'}") from exc
    return result.stdout


def read_archive(path: Path, passphrase: str | None) -> bytes:
    if not path.is_file():
        raise RestoreError(f"备份文件不存在：{path}")
    payload = path.read_bytes()
    if path.name.endswith(".enc"):
        if not passphrase:
            raise RestoreError(f"备份已加密：请通过环境变量 {PASSPHRASE_ENV} 或 --passphrase-env 提供口令")
        payload = decrypt_openssl(payload, passphrase)
    return payload


def safe_member_name(name: str) -> str:
    """Reject absolute paths, drive letters and parent traversal inside the archive."""
    value = str(name or "").replace("\\", "/")
    pure = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or pure.is_absolute()
        or any(part in {"", "..", "."} or ":" in part for part in value.split("/"))
    ):
        raise RestoreError(f"备份包含不安全的路径：{name!r}")
    return pure.as_posix()


def unpack(payload: bytes) -> dict[str, bytes]:
    """Return {member name: bytes} for every regular file, rejecting unsafe members."""
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            for member in archive.getmembers():
                if member.isdir():
                    continue
                if not member.isfile():
                    raise RestoreError(f"备份包含非普通文件成员：{member.name!r}")
                name = safe_member_name(member.name)
                handle = archive.extractfile(member)
                if handle is None:
                    raise RestoreError(f"无法读取备份成员：{name}")
                files[name] = handle.read()
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise RestoreError(f"备份压缩包已损坏或不是有效的 tar.gz：{exc}") from exc
    return files


def parse_json(files: dict[str, bytes], name: str) -> dict | None:
    raw = files.get(name)
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RestoreError(f"{name} 不是有效的 JSON：{exc}") from exc
    return value if isinstance(value, dict) else None


def verify(files: dict[str, bytes]) -> dict[str, object]:
    """Validate metadata + manifest hashes. Raises RestoreError on any mismatch."""
    metadata = parse_json(files, METADATA_NAME)
    if metadata is None:
        raise RestoreError(f"缺少 {METADATA_NAME}：这不是 chatgpt2api 的备份包")
    problems: list[str] = []
    warnings: list[str] = []

    manifest = parse_json(files, MANIFEST_NAME)
    checked = 0
    if manifest is None:
        warnings.append(f"备份中没有 {MANIFEST_NAME}（旧版本备份），无法逐文件校验哈希")
    else:
        entries = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
        for name, info in entries.items():
            if name not in files:
                problems.append(f"清单中的文件缺失：{name}")
                continue
            expected = str((info or {}).get("sha256") or "")
            if expected and sha256_hex(files[name]) != expected:
                problems.append(f"哈希不匹配：{name}")
            elif (info or {}).get("size") is not None and int(info["size"]) != len(files[name]):
                problems.append(f"大小不匹配：{name}")
            checked += 1
        extra = sorted(set(files) - set(entries) - {METADATA_NAME, MANIFEST_NAME})
        for name in extra:
            problems.append(f"文件不在清单中：{name}")

    snapshot = metadata.get("database_snapshot")
    if isinstance(snapshot, dict):
        if snapshot.get("status") == "ok":
            name = str(snapshot.get("file") or "")
            payload = files.get(name)
            if payload is None:
                problems.append(f"元数据声明的数据库快照缺失：{name}")
            elif snapshot.get("sha256") and sha256_hex(payload) != snapshot["sha256"]:
                problems.append(f"数据库快照哈希与元数据不一致：{name}")
        else:
            warnings.append(f"该备份跳过了数据库快照：{snapshot.get('reason') or '未知原因'}")

    if problems:
        raise RestoreError("备份校验失败：\n  - " + "\n  - ".join(problems))
    return {"metadata": metadata, "checked": checked, "warnings": warnings}


def describe(files: dict[str, bytes], result: dict[str, object]) -> str:
    metadata = result["metadata"]
    lines = [
        f"备份创建时间：{metadata.get('created_at')}  触发：{metadata.get('trigger')}  版本：{metadata.get('app_version')}",
        f"元数据版本：{metadata.get('version')}  已校验文件数：{result['checked']}",
    ]
    groups: dict[str, list[int]] = {}
    for name, payload in files.items():
        if name in {METADATA_NAME, MANIFEST_NAME}:
            continue
        key = "/".join(name.split("/")[:2]) if name.startswith("data/") and name.count("/") >= 2 else name
        bucket = groups.setdefault(key, [0, 0])
        bucket[0] += 1
        bucket[1] += len(payload)
    lines.append("内容：")
    for key in sorted(groups):
        count, size = groups[key]
        lines.append(f"  {key}  ({count} 个文件, {size} 字节)")
    for warning in result["warnings"]:
        lines.append(f"警告：{warning}")
    return "\n".join(lines)


def snapshot_current(target: Path, stamp: str) -> list[Path]:
    """Copy the current data/ and config.json beside the target before overwriting anything."""
    created: list[Path] = []

    def unique(base: str) -> Path:
        candidate = target / base
        counter = 2
        while candidate.exists():
            candidate = target / f"{base}-{counter}"
            counter += 1
        return candidate

    data_dir = target / "data"
    if data_dir.is_dir():
        destination = unique(f"data.pre-restore-{stamp}")
        shutil.copytree(data_dir, destination)
        created.append(destination)
    config_file = target / "config.json"
    if config_file.is_file():
        destination_file = unique(f"config.json.pre-restore-{stamp}")
        shutil.copy2(config_file, destination_file)
        created.append(destination_file)
    return created


def write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.restore.tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def apply_restore(
    files: dict[str, bytes],
    target: Path,
    *,
    sqlite_target: Path | None = None,
    stamp: str | None = None,
) -> dict[str, object]:
    stamp = stamp or time.strftime("%Y%m%dT%H%M%S")
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    snapshots = snapshot_current(target, stamp)
    written: list[str] = []
    notes: list[str] = []
    for name, payload in sorted(files.items()):
        if name in {METADATA_NAME, MANIFEST_NAME}:
            continue
        destination = (target / name).resolve()
        try:
            destination.relative_to(target)
        except ValueError as exc:
            raise RestoreError(f"拒绝写入目标目录之外：{name}") from exc
        if name == SQLITE_SNAPSHOT and sqlite_target is not None:
            destination = sqlite_target.resolve()
            for sidecar in (destination.with_name(destination.name + "-wal"), destination.with_name(destination.name + "-shm")):
                sidecar.unlink(missing_ok=True)
        write_atomic(destination, payload)
        written.append(name)
        if name == SQLITE_SNAPSHOT:
            if sqlite_target is None:
                notes.append(
                    f"SQLite 快照已写到 {destination}。请停止服务后，把它复制为 DATABASE_URL 指向的库文件"
                    "（默认 data/accounts.db，并删除同名 -wal/-shm 文件），再启动服务。"
                )
            else:
                notes.append(f"SQLite 快照已恢复到 {destination}（-wal/-shm 已清理）。")
        elif name == PGDUMP_SNAPSHOT:
            notes.append(
                "PostgreSQL 快照不会自动恢复。确认目标库可被覆盖后，手动执行：\n"
                f'    pg_restore --clean --if-exists --no-owner --no-privileges --dbname "$DATABASE_URL" "{destination}"'
            )
    return {"written": written, "snapshots": [str(path) for path in snapshots], "notes": notes}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="校验并恢复 chatgpt2api 备份（默认只校验，不写入）")
    parser.add_argument("archive", type=Path, help="backup-*.tar.gz 或 backup-*.tar.gz.enc")
    parser.add_argument("--passphrase-env", default=PASSPHRASE_ENV, help=f"保存加密口令的环境变量名（默认 {PASSPHRASE_ENV}）")
    parser.add_argument("--target", type=Path, default=Path("."), help="恢复目标目录（项目根，默认当前目录）")
    parser.add_argument("--apply", action="store_true", help="真正写入目标目录（会先自动快照现有 data/）")
    parser.add_argument("--sqlite-target", type=Path, default=None, help="把 SQLite 快照直接恢复到该库文件（默认不覆盖在线库）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = read_archive(args.archive, os.environ.get(args.passphrase_env))
        files = unpack(payload)
        result = verify(files)
    except RestoreError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_INVALID
    print(describe(files, result))
    if not args.apply:
        print("\n校验通过。未写入任何文件；确认后加 --apply 执行恢复（请先停止服务）。")
        return EXIT_OK
    try:
        outcome = apply_restore(files, args.target, sqlite_target=args.sqlite_target)
    except (RestoreError, OSError) as exc:
        print(f"错误：恢复失败：{exc}", file=sys.stderr)
        return EXIT_INVALID
    for item in outcome["snapshots"]:
        print(f"已快照现有数据：{item}")
    print(f"已写入 {len(outcome['written'])} 个文件到 {args.target.resolve()}")
    for note in outcome["notes"]:
        print(note)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
