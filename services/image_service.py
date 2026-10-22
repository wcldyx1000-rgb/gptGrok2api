from __future__ import annotations

import hashlib
import io
import mimetypes
import os
import re
import shutil
import threading
import time
import zipfile
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageOps

from services.config import config
from services.genbox_push_view import genbox_push_state
from services.image_storage_service import ImageBatchDeleteError, image_storage_service
from services.image_tags_service import load_tags, remove_tags
from utils.log import logger
from utils.timezone import beijing_now, parse_to_beijing_naive

THUMBNAIL_SIZE = (320, 320)
IMAGE_LIST_MAINTENANCE_INTERVAL_SECS = 300
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v"}
MUSIC_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".vtt", ".lrc", ".txt"}
_maintenance_lock = threading.Lock()
_last_list_maintenance_at = 0.0
# 生成的图片文件名形如 `{时间戳}_{内容哈希}.png`，同一路径永远对应同一份像素，
# 可以长期缓存；其它文件（如手工放入的媒体）只做短期缓存。
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
SHORT_CACHE_CONTROL = "public, max-age=3600"
_CONTENT_ADDRESSED_NAME = re.compile(r"_[0-9a-f]{16,}\.[A-Za-z0-9]+$")
_THUMBNAIL_LOCKS = tuple(threading.Lock() for _ in range(64))
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}


def _thumbnail_lock(relative_path: str) -> threading.Lock:
    digest = hashlib.blake2b(relative_path.encode("utf-8"), digest_size=2).digest()
    return _THUMBNAIL_LOCKS[int.from_bytes(digest, "big") % len(_THUMBNAIL_LOCKS)]


def _cache_control_for(relative_path: str) -> str:
    if _CONTENT_ADDRESSED_NAME.search(Path(str(relative_path or "")).name):
        return IMMUTABLE_CACHE_CONTROL
    return SHORT_CACHE_CONTROL


def media_type_for_bytes(data: bytes, relative_path: str = "") -> str:
    """按文件头识别类型，识别不了再按扩展名猜，最后才回退 image/png。"""
    head = bytes(data[:16]) if data else b""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head[4:8] == b"ftyp":
        return "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    guessed = mimetypes.guess_type(str(relative_path or ""))[0]
    return guessed or "image/png"


def _cleanup_empty_dirs(root: Path) -> None:
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass


def _safe_relative_path(path: str) -> str:
    value = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not value:
        raise HTTPException(status_code=404, detail="image not found")
    parts = Path(value).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=404, detail="image not found")
    return Path(*parts).as_posix()


def _safe_image_path(relative_path: str) -> Path:
    rel = _safe_relative_path(relative_path)
    root = config.images_dir.resolve()
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="image not found") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="image not found")
    return path


def get_image_response(relative_path: str) -> FileResponse | Response:
    headers = {**_CORS_HEADERS, "Cache-Control": _cache_control_for(relative_path)}
    if image_storage_service.has_local(relative_path):
        return FileResponse(_safe_image_path(relative_path), headers=headers)
    payload = image_storage_service.get_bytes(relative_path)
    return Response(content=payload, media_type=media_type_for_bytes(payload, relative_path), headers=headers)


def _thumbnail_path(relative_path: str) -> Path:
    rel = _safe_relative_path(relative_path)
    return config.image_thumbnails_dir / f"{rel}.png"


def thumbnail_url(base_url: str, relative_path: str) -> str:
    return f"{base_url.rstrip('/')}/image-thumbnails/{_safe_relative_path(relative_path)}"


def _image_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            return image.size
    except Exception:
        return None


def _thumbnail_is_fresh(target: Path, source_mtime: float) -> bool:
    try:
        return not source_mtime or target.stat().st_mtime >= source_mtime
    except OSError:
        return False


def ensure_thumbnail(relative_path: str) -> Path:
    target = _thumbnail_path(relative_path)
    source_mtime = 0.0
    source: Path | None = None
    if image_storage_service.has_local(relative_path):
        source = _safe_image_path(relative_path)
        source_mtime = source.stat().st_mtime
    if _thumbnail_is_fresh(target, source_mtime):
        return target

    # 同一张图的并发请求只生成一次缩略图。
    with _thumbnail_lock(_safe_relative_path(relative_path)):
        if _thumbnail_is_fresh(target, source_mtime):
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            image_source = source if source is not None else io.BytesIO(image_storage_service.get_bytes(relative_path))
            with Image.open(image_source) as image:
                image = ImageOps.exif_transpose(image)
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                image.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
                image.save(temp_path, format="PNG", optimize=True)
            # 先写临时文件再原子替换，读取方永远看不到写了一半的缩略图。
            os.replace(temp_path, target)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=422, detail="failed to create thumbnail") from exc
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
    return target


def get_thumbnail_response(relative_path: str) -> FileResponse:
    headers = {**_CORS_HEADERS, "Cache-Control": _cache_control_for(relative_path)}
    return FileResponse(ensure_thumbnail(relative_path), headers=headers)


def get_image_download_response(relative_path: str) -> FileResponse:
    cors_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    }
    if image_storage_service.has_local(relative_path):
        path = _safe_image_path(relative_path)
        headers = {**cors_headers, "Content-Disposition": f'attachment; filename="{path.name}"'}
        return FileResponse(path, filename=path.name, headers=headers)
    rel = _safe_relative_path(relative_path)
    headers = {
        **cors_headers,
        "Content-Disposition": f'attachment; filename="{Path(rel).name}"',
    }
    payload = image_storage_service.get_bytes(rel)
    return Response(
        content=payload,
        media_type=media_type_for_bytes(payload, rel),
        headers=headers,
    )


def cleanup_image_thumbnails() -> int:
    thumbnails_root = config.image_thumbnails_dir
    removed = 0
    candidates: dict[Path, str] = {}
    for path in thumbnails_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(thumbnails_root).as_posix()
        if not rel.endswith(".png"):
            path.unlink()
            removed += 1
            continue
        candidates[path] = rel[:-4]
    # One index read for the whole batch instead of one per thumbnail.
    existing = image_storage_service.existing_paths(list(candidates.values()))
    for path, rel in candidates.items():
        if rel not in existing:
            path.unlink()
            removed += 1
    _cleanup_empty_dirs(thumbnails_root)
    return removed


def _run_periodic_list_maintenance() -> None:
    global _last_list_maintenance_at
    now = time.time()
    if now - _last_list_maintenance_at < IMAGE_LIST_MAINTENANCE_INTERVAL_SECS:
        return
    if not _maintenance_lock.acquire(blocking=False):
        return
    try:
        now = time.time()
        if now - _last_list_maintenance_at < IMAGE_LIST_MAINTENANCE_INTERVAL_SECS:
            return
        # Expiry is owned by the retention coordinator (services.retention_cleanup_service);
        # list maintenance only reconciles the index and orphaned thumbnails.
        image_storage_service.list_items("", refresh_index=False, verify_existing=True)
        cleanup_image_thumbnails()
        _last_list_maintenance_at = now
    finally:
        _maintenance_lock.release()


def _schedule_periodic_list_maintenance() -> None:
    now = time.time()
    if now - _last_list_maintenance_at < IMAGE_LIST_MAINTENANCE_INTERVAL_SECS:
        return
    if _maintenance_lock.locked():
        return
    threading.Thread(
        target=_run_periodic_list_maintenance,
        name="image-list-maintenance",
        daemon=True,
    ).start()


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _expiry_for_item(item: dict[str, object], retention_hours: int) -> tuple[bool, int | None]:
    """Return display-only expiration state from created_at and retention hours."""
    if retention_hours <= 0:
        return False, None
    created = parse_to_beijing_naive(item.get("created_at"))
    if created is None:
        return False, None
    remaining = retention_hours * 3600 - (beijing_now().replace(tzinfo=None) - created).total_seconds()
    if remaining <= 0:
        return True, 0
    return False, int(remaining)


def _expires_at_for_item(item: dict[str, object], retention_hours: int) -> str | None:
    """Absolute expiration time (Beijing, ``YYYY-MM-DD HH:MM:SS``) shown in the gallery card tooltip."""
    if retention_hours <= 0:
        return None
    created = parse_to_beijing_naive(item.get("created_at"))
    if created is None:
        return None
    return (created + timedelta(hours=retention_hours)).strftime("%Y-%m-%d %H:%M:%S")


def _media_type_for_item(item: dict[str, object]) -> str:
    explicit = _clean_text(item.get("type")).lower()
    if explicit in {"image", "video", "music"}:
        return explicit
    path = _clean_text(item.get("path") or item.get("rel") or item.get("name"))
    ext = Path(path).suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in MUSIC_EXTENSIONS:
        return "music"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return "image"


def _matches_image_search(item: dict[str, object], tags: list[str], search: str) -> bool:
    keyword = search.strip().lower()
    if not keyword:
        return True
    values = [
        item.get("name"),
        item.get("path"),
        item.get("rel"),
        item.get("created_at"),
        item.get("storage"),
        *tags,
    ]
    return any(keyword in _clean_text(value).lower() for value in values)


def _page_meta(total: int, limit: int, offset: int) -> tuple[int, int, int]:
    safe_limit = max(0, min(int(limit or 0), 500))
    safe_offset = max(0, int(offset or 0))
    if safe_limit <= 0:
        return 1, total, 1
    page = safe_offset // safe_limit + 1
    page_count = max(1, (total + safe_limit - 1) // safe_limit)
    return page, safe_limit, page_count


def _retention_hours(value: int | float | str | None, fallback: int) -> int:
    try:
        return max(1, int(float(value or fallback)))
    except (TypeError, ValueError):
        return max(1, int(fallback))


def _retention_cleanup_targets(retention_hours: int) -> list[tuple[str, int]]:
    hours = _retention_hours(retention_hours, config.image_retention_hours)
    cutoff = time.time() - hours * 3600
    root = config.images_dir.resolve()
    targets: list[tuple[str, int]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            path.relative_to(root)
        except ValueError:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime >= cutoff:
            continue
        targets.append((path.relative_to(root).as_posix(), stat.st_size))
    return targets


def preview_image_retention_cleanup(retention_hours: int | None = None) -> dict[str, int | bool]:
    hours = _retention_hours(retention_hours, config.image_retention_hours)
    targets = _retention_cleanup_targets(hours)
    return {
        "removed": len(targets),
        "removed_size_bytes": sum(size for _, size in targets),
        "retention_hours": hours,
        "dry_run": True,
    }


def _drop_asset_derivatives(rel: str) -> None:
    for thumbnail in (_thumbnail_path(rel), config.image_thumbnails_dir / _safe_relative_path(rel)):
        if thumbnail.is_file():
            thumbnail.unlink()
    remove_tags(rel)


def cleanup_image_retention(retention_hours: int | None = None) -> dict[str, int | bool]:
    """Delete expired images through ImageStorageService (tombstoned, WebDAV-aware)."""
    hours = _retention_hours(retention_hours, config.image_retention_hours)
    targets = _retention_cleanup_targets(hours)
    sizes = dict(targets)
    asset_rels: list[str] = []
    removed = 0
    removed_size_bytes = 0
    for rel, size in targets:
        suffix = Path(rel).suffix.lower()
        if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
            asset_rels.append(rel)
            continue
        # Non-asset leftovers (temp files, manual media) are not in the image catalog.
        try:
            (config.images_dir / rel).unlink()
            removed += 1
            removed_size_bytes += size
        except OSError:
            continue
    terminal_error: Exception | None = None
    try:
        removed_paths = image_storage_service.delete_many(asset_rels)
        completed = set(asset_rels)
    except ImageBatchDeleteError as exc:
        removed_paths = set()
        completed = set(exc.completed_rels)
        terminal_error = exc.cause
    # A failed remote delete stays tombstoned and is retried; derivatives follow completed assets only.
    counted = set(removed_paths) if terminal_error is None else completed
    for rel in completed:
        _drop_asset_derivatives(rel)
    for rel in counted:
        removed += 1
        removed_size_bytes += sizes.get(rel, 0)
    if terminal_error is None:
        try:
            image_storage_service.retry_pending_remote_deletes()
        except Exception as exc:
            logger.warning({"event": "image_remote_delete_retry_failed", "error": str(exc)})
    cleanup_image_thumbnails()
    _cleanup_empty_dirs(config.images_dir)
    _cleanup_empty_dirs(config.image_thumbnails_dir)
    if terminal_error is not None:
        raise terminal_error
    return {
        "removed": removed,
        "removed_size_bytes": removed_size_bytes,
        "retention_hours": hours,
        "dry_run": False,
    }


def list_images(
    base_url: str,
    start_date: str = "",
    end_date: str = "",
    *,
    limit: int = 0,
    offset: int = 0,
    media_type: str = "all",
    tag: str = "",
    search: str = "",
) -> dict[str, object]:
    paged = int(limit or 0) > 0
    if paged:
        _schedule_periodic_list_maintenance()
    else:
        _run_periodic_list_maintenance()
    all_tags = load_tags()
    retention_hours = config.image_retention_hours
    raw_items = image_storage_service.list_items(
        base_url,
        start_date,
        end_date,
        refresh_index=not paged,
        verify_existing=not paged,
    )
    normalized_items = []
    for item in raw_items:
        path = str(item["path"])
        tags = all_tags.get(path, [])
        current_type = _media_type_for_item(item)
        expired, expires_in_seconds = _expiry_for_item(item, retention_hours)
        normalized_items.append({
            **item,
            "type": current_type,
            "filename": str(item.get("name") or Path(path).name),
            "url": str(item.get("url") or f"{base_url.rstrip('/')}/images/{path}"),
            "thumbnail_url": thumbnail_url(base_url, path),
            "genbox_push": genbox_push_state(item.get("genbox_push")),
            "tags": tags,
            "expired": expired,
            "expires_in_seconds": expires_in_seconds,
            "expires_at": _expires_at_for_item(item, retention_hours),
        })

    tag_filter = tag.strip()
    search_filter = search.strip()
    base_items = [
        item for item in normalized_items
        if (not tag_filter or tag_filter == "all" or tag_filter in item.get("tags", []))
        and _matches_image_search(item, list(item.get("tags", [])), search_filter)
    ]
    counts = {
        "all": len(base_items),
        "image": sum(1 for item in base_items if item.get("type") == "image"),
        "video": sum(1 for item in base_items if item.get("type") == "video"),
        "music": sum(1 for item in base_items if item.get("type") == "music"),
    }
    wanted_type = media_type.strip().lower()
    if wanted_type and wanted_type != "all":
        items = [item for item in base_items if item.get("type") == wanted_type]
    else:
        items = base_items

    total = len(items)
    total_size = sum(int(item.get("size") or 0) for item in items)
    page, page_size, page_count = _page_meta(total, limit, offset)
    safe_offset = max(0, int(offset or 0))
    if int(limit or 0) > 0 and total > 0 and safe_offset >= total:
        safe_offset = (page_count - 1) * page_size
        page = page_count
    page_items = items[safe_offset:safe_offset + page_size] if int(limit or 0) > 0 else items
    groups: dict[str, list[dict[str, object]]] = {}
    for item in page_items:
        groups.setdefault(str(item["date"]), []).append(item)
    return {
        "items": page_items,
        "groups": [{"date": key, "items": value} for key, value in groups.items()],
        "total": total,
        "total_size": total_size,
        "counts": counts,
        "retention_hours": retention_hours,
        "genbox_push_enabled": bool(config.get_genbox_push_settings().get("enabled")),
        "limit": page_size,
        "offset": safe_offset,
        "page": page,
        "page_size": page_size,
        "page_count": page_count,
        "has_more": page < page_count,
    }


def delete_images(paths: list[str] | None = None, start_date: str = "", end_date: str = "", all_matching: bool = False) -> dict[str, int]:
    root = config.images_dir.resolve()
    targets = [
        str(item["path"])
        for item in image_storage_service.list_items("", start_date=start_date, end_date=end_date)
    ] if all_matching else (paths or [])
    valid_targets: list[str] = []
    for item in targets:
        path = (root / item).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        valid_targets.append(item)

    terminal_error: Exception | None = None
    try:
        removed_paths = image_storage_service.delete_many(valid_targets)
        completed_targets = valid_targets
    except ImageBatchDeleteError as exc:
        removed_paths = set()
        completed_targets = list(exc.completed_rels)
        terminal_error = exc.cause
    for item in completed_targets:
        _drop_asset_derivatives(item)
    if terminal_error is not None:
        raise terminal_error
    _cleanup_empty_dirs(root)
    _cleanup_empty_dirs(config.image_thumbnails_dir)
    return {"removed": len(removed_paths)}


def download_images_zip(paths: list[str]) -> io.BytesIO:
    root = config.images_dir.resolve()
    buf = io.BytesIO()
    added = 0
    used_names: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in paths:
            rel = _safe_relative_path(item)
            path = (root / rel).resolve()
            payload: bytes | None = None
            try:
                path.relative_to(root)
            except ValueError:
                continue
            if path.is_file():
                payload = path.read_bytes()
            else:
                try:
                    payload = image_storage_service.get_bytes(rel)
                except Exception:
                    continue
            name = path.name
            if name in used_names:
                stem = path.stem
                suffix = path.suffix
                counter = 2
                while f"{stem}_{counter}{suffix}" in used_names:
                    counter += 1
                name = f"{stem}_{counter}{suffix}"
            used_names.add(name)
            zf.writestr(name, payload)
            added += 1
    if added == 0:
        raise HTTPException(status_code=404, detail="no images found")
    buf.seek(0)
    return buf
def storage_stats() -> dict:
    import shutil
    usage = shutil.disk_usage(config.images_dir)
    total_mb = usage.total // (1024 * 1024)
    used_mb = usage.used // (1024 * 1024)
    free_mb = usage.free // (1024 * 1024)

    image_count = 0
    image_size = 0
    for p in config.images_dir.rglob("*"):
        if p.is_file():
            image_count += 1
            image_size += p.stat().st_size

    return {
        "disk_total_mb": total_mb,
        "disk_used_mb": used_mb,
        "disk_free_mb": free_mb,
        "image_count": image_count,
        "image_size_mb": image_size // (1024 * 1024),
        "image_size_bytes": image_size,
    }


def compress_images(quality: int = 60) -> dict:
    """重新压缩所有本地图片（经 ImageStorageService：同步索引大小与远端待同步状态）。"""
    return image_storage_service.compress_local_images(quality)


def delete_to_target(target_free_mb: int, dry_run: bool = False) -> dict:
    """删除最旧的本地图片副本直到剩余空间达到 target_free_mb。

    经 ImageStorageService 执行：待远端对账 / 与索引大小不符的本地副本会被保留；
    已同步到 WebDAV 的图片仅删本地副本，远端副本与标签保留。
    """
    import shutil
    mebibyte = 1024 * 1024
    usage = shutil.disk_usage(config.images_dir)
    target_free_bytes = max(0, int(target_free_mb)) * mebibyte
    current_free_bytes = int(usage.free)
    current_free = current_free_bytes // mebibyte
    if current_free_bytes >= target_free_bytes and not dry_run:
        return {"removed": 0, "current_free_mb": current_free, "target_free_mb": target_free_mb, "done": True}

    files = sorted(
        (p for p in config.images_dir.rglob("*.png") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
    )
    removals = image_storage_service.delete_local_copies_until(
        [p.relative_to(config.images_dir).as_posix() for p in files],
        max(0, target_free_bytes - current_free_bytes),
        dry_run=dry_run,
    )
    freed = sum(removal.size for removal in removals)
    if not dry_run:
        for removal in removals:
            for thumbnail in (_thumbnail_path(removal.rel), config.image_thumbnails_dir / _safe_relative_path(removal.rel)):
                if thumbnail.is_file():
                    thumbnail.unlink()
            if not removal.remote_remains:
                remove_tags(removal.rel)
        _cleanup_empty_dirs(config.images_dir)
        _cleanup_empty_dirs(config.image_thumbnails_dir)

    return {
        "removed": len(removals),
        "freed_mb": freed // mebibyte,
        "target_free_mb": target_free_mb,
        "current_free_mb": (current_free_bytes + freed) // mebibyte,
        "done": current_free_bytes + freed >= target_free_bytes,
        "dry_run": dry_run,
    }


# 定时清理已统一由 services.retention_cleanup_service 的协调器负责（按小时、含 image_min_free_mb 空间兜底）。
