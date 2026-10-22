"""Unified log/image retention cleanup (hour based).

Ported from yukkcat/chatgpt2api (AGPL-3.0-only).

Differences from the upstream coordinator: this project runs a single worker
process, so the cross-process coordination repository is replaced by an
in-process ``RLock`` that serialises every cleanup run (automatic, manual and
cleanup-to-target) with the same semantics.
"""

from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Callable
from typing import Any

from services.config import config
from services.image_service import cleanup_image_retention, delete_to_target, preview_image_retention_cleanup
from services.log_service import log_service
from utils.log import logger
from utils.timezone import beijing_from_timestamp


RETENTION_CLEANUP_INTERVAL_SECS = 1800
RETENTION_STOP_POLL_SECS = 0.25
CleanupResult = dict[str, int | bool]
CleanupRunner = Callable[[int, bool], CleanupResult]
ImageSpaceRunner = Callable[[], dict[str, Any] | None]
ImageTargetRunner = Callable[[int, bool], dict[str, Any]]


def _run_log_cleanup(retention_hours: int, dry_run: bool) -> CleanupResult:
    if dry_run:
        return log_service.preview_cleanup_old_hours(retention_hours)
    return log_service.cleanup_old_hours(retention_hours)


def _run_image_cleanup(retention_hours: int, dry_run: bool) -> CleanupResult:
    if dry_run:
        return preview_image_retention_cleanup(retention_hours)
    return cleanup_image_retention(retention_hours)


def _enforce_image_free_space() -> dict[str, Any] | None:
    """Delete the oldest images when free disk space drops below image_min_free_mb."""
    min_free_mb = config.image_min_free_mb
    if min_free_mb <= 0:
        return None
    usage = shutil.disk_usage(config.images_dir)
    free_mb = usage.free // (1024 * 1024)
    if free_mb >= min_free_mb:
        return None
    return {
        "free_mb": free_mb,
        "min_free_mb": min_free_mb,
        "cleanup": delete_to_target(min_free_mb),
    }


def _retention_hours(value: int | None, fallback: int) -> int:
    try:
        return max(1, int(value or fallback))
    except (TypeError, ValueError):
        return max(1, int(fallback))


class RetentionCleanupCoordinator:
    def __init__(
        self,
        *,
        log_runner: CleanupRunner = _run_log_cleanup,
        image_runner: CleanupRunner = _run_image_cleanup,
        image_space_runner: ImageSpaceRunner = _enforce_image_free_space,
        image_target_runner: ImageTargetRunner = delete_to_target,
        interval_seconds: int = RETENTION_CLEANUP_INTERVAL_SECS,
    ) -> None:
        self._log_runner = log_runner
        self._image_runner = image_runner
        self._image_space_runner = image_space_runner
        self._image_target_runner = image_target_runner
        self.interval_seconds = max(1, int(interval_seconds))
        self._run_lock = threading.RLock()
        self._scheduler_lock = threading.Lock()
        self._scheduler_stop_event: threading.Event | None = None
        self._scheduler_thread: threading.Thread | None = None
        self._wake_event = threading.Event()
        self._status_lock = threading.Lock()
        self._running = False
        self._last_started_at: str | None = None
        self._last_finished_at: str | None = None
        self._next_run_at: str | None = None
        self._last_removed: dict[str, int] = {"logs": 0, "images": 0}
        self._last_error: dict[str, str | None] = {"logs": None, "images": None}

    @staticmethod
    def _timestamp(value: float | None = None) -> str:
        return beijing_from_timestamp(time.time() if value is None else value)

    # ---- status -----------------------------------------------------------------
    def _mark_started(self) -> None:
        with self._status_lock:
            self._running = True
            self._last_started_at = self._timestamp()

    def _mark_finished(
        self,
        *,
        results: dict[str, Any],
        errors: dict[str, str],
        domains: tuple[str, ...],
    ) -> None:
        with self._status_lock:
            for domain in domains:
                result = results.get(domain)
                self._last_removed[domain] = int(result.get("removed") or 0) if isinstance(result, dict) else 0
                self._last_error[domain] = errors.get(domain)
            self._last_finished_at = self._timestamp()
            self._running = False

    def _set_next_run(self, timestamp: float | None) -> None:
        with self._status_lock:
            self._next_run_at = self._timestamp(timestamp) if timestamp is not None else None

    def runtime_status(self) -> dict[str, Any]:
        with self._status_lock:
            return {
                "running": self._running,
                "last_started_at": self._last_started_at,
                "last_finished_at": self._last_finished_at,
                "next_run_at": self._next_run_at,
                "last_removed": dict(self._last_removed),
                "last_error": dict(self._last_error),
            }

    def notify_retention_change(self) -> None:
        """Retention settings changed: run the next automatic cleanup right away."""
        with self._status_lock:
            if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
                self._next_run_at = self._timestamp()
        self._wake_event.set()

    # ---- manual runs ------------------------------------------------------------
    def run_retention(
        self,
        *,
        log_retention_hours: int | None = None,
        image_retention_hours: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        log_hours = _retention_hours(log_retention_hours, config.log_retention_hours)
        image_hours = _retention_hours(image_retention_hours, config.image_retention_hours)
        with self._run_lock:
            if dry_run:
                logs = self._log_runner(log_hours, True)
                images = self._image_runner(image_hours, True)
            else:
                self._mark_started()
                results: dict[str, Any] = {}
                errors: dict[str, str] = {}
                try:
                    logs = self._log_runner(log_hours, False)
                    results["logs"] = logs
                    images = self._image_runner(image_hours, False)
                    results["images"] = images
                except Exception as exc:
                    domain = "images" if "logs" in results else "logs"
                    errors[domain] = str(exc)
                    domains = ("logs", "images") if domain == "images" else ("logs",)
                    self._mark_finished(results=results, errors=errors, domains=domains)
                    raise
                self._mark_finished(results=results, errors=errors, domains=("logs", "images"))
        total_removed = int(logs.get("removed") or 0) + int(images.get("removed") or 0)
        total_size = int(logs.get("removed_size_bytes") or 0) + int(images.get("removed_size_bytes") or 0)
        return {
            "dry_run": dry_run,
            "logs": {**logs, "retention_hours": log_hours},
            "images": {**images, "retention_hours": image_hours},
            "total_removed": total_removed,
            "total_size_bytes": total_size,
        }

    def _run_single(self, domain: str, runner: Callable[[], Any], *, dry_run: bool) -> Any:
        with self._run_lock:
            if dry_run:
                return runner()
            self._mark_started()
            try:
                result = runner()
            except Exception as exc:
                self._mark_finished(results={}, errors={domain: str(exc)}, domains=(domain,))
                raise
            self._mark_finished(results={domain: result}, errors={}, domains=(domain,))
            return result

    def run_logs(self, retention_hours: int | None = None, *, dry_run: bool = False) -> CleanupResult:
        hours = _retention_hours(retention_hours, config.log_retention_hours)
        return self._run_single("logs", lambda: self._log_runner(hours, dry_run), dry_run=dry_run)

    def run_images(self, retention_hours: int | None = None, *, dry_run: bool = False) -> CleanupResult:
        hours = _retention_hours(retention_hours, config.image_retention_hours)
        return self._run_single("images", lambda: self._image_runner(hours, dry_run), dry_run=dry_run)

    def cleanup_images_to_target(self, target_free_mb: int, *, dry_run: bool = False) -> dict[str, Any]:
        target = int(target_free_mb)
        return self._run_single("images", lambda: self._image_target_runner(target, dry_run), dry_run=dry_run)

    # ---- automatic runs ---------------------------------------------------------
    @staticmethod
    def _empty_automatic_result() -> dict[str, Any]:
        return {"logs": None, "images": None, "image_space": None, "errors": {}}

    def run_automatic(
        self,
        *,
        enforce_image_free_space: bool,
        stop_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        with self._run_lock:
            if stop_event is not None and stop_event.is_set():
                return self._empty_automatic_result()
            return self._run_automatic_owned(
                enforce_image_free_space=enforce_image_free_space,
                stop_event=stop_event,
            )

    def _run_automatic_owned(
        self,
        *,
        enforce_image_free_space: bool,
        stop_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        result = self._empty_automatic_result()
        self._mark_started()
        if stop_event is None or not stop_event.is_set():
            try:
                result["logs"] = self._log_runner(config.log_retention_hours, False)
            except Exception as exc:
                result["errors"]["logs"] = str(exc)
        if stop_event is None or not stop_event.is_set():
            try:
                result["images"] = self._image_runner(config.image_retention_hours, False)
                if enforce_image_free_space and (stop_event is None or not stop_event.is_set()):
                    result["image_space"] = self._image_space_runner()
            except Exception as exc:
                result["errors"]["images"] = str(exc)
        status_results = {"logs": result.get("logs"), "images": result.get("images")}
        image_space = result.get("image_space")
        if isinstance(image_space, dict) and isinstance(image_space.get("cleanup"), dict):
            image_result = dict(status_results.get("images") or {})
            image_result["removed"] = int(image_result.get("removed") or 0) + int(
                image_space["cleanup"].get("removed") or 0
            )
            status_results["images"] = image_result
        self._mark_finished(
            results=status_results,
            errors=result["errors"],
            domains=("logs", "images"),
        )
        return result

    def _log_automatic_result(self, result: dict[str, Any]) -> None:
        logs = result.get("logs") or {}
        if int(logs.get("removed") or 0) > 0:
            logger.info({"event": "log_auto_cleanup_done", **logs})
        images = result.get("images") or {}
        if int(images.get("removed") or 0) > 0:
            logger.info({"event": "image_retention_cleanup_done", **images})
        image_space = result.get("image_space")
        if image_space:
            logger.info({
                "event": "image_auto_cleanup",
                "free_mb": image_space["free_mb"],
                "min_free_mb": image_space["min_free_mb"],
            })
            logger.info({"event": "image_auto_cleanup_done", **image_space["cleanup"]})
        errors = result.get("errors") or {}
        if errors.get("logs"):
            logger.error({"event": "log_auto_cleanup_failed", "error": errors["logs"]})
        if errors.get("images"):
            logger.error({"event": "image_auto_cleanup_failed", "error": errors["images"]})

    def _wait_for_cleanup(self, stop_event: threading.Event, deadline: float) -> bool:
        while not stop_event.is_set():
            if self._wake_event.is_set():
                self._wake_event.clear()
                return not stop_event.is_set()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            if stop_event.wait(min(remaining, RETENTION_STOP_POLL_SECS)):
                return False
        return False

    def scheduler_worker(self, stop_event: threading.Event) -> None:
        """Run one cleanup at startup, then every interval (or sooner on settings change)."""
        try:
            result = self.run_automatic(enforce_image_free_space=True, stop_event=stop_event)
            self._log_automatic_result(result)
        except Exception as exc:
            logger.error({"event": "retention_cleanup_startup_failed", "error": str(exc)})
        while not stop_event.is_set():
            self._set_next_run(time.time() + self.interval_seconds)
            deadline = time.monotonic() + self.interval_seconds
            if not self._wait_for_cleanup(stop_event, deadline):
                break
            self._wake_event.clear()
            try:
                result = self.run_automatic(enforce_image_free_space=True, stop_event=stop_event)
                self._log_automatic_result(result)
            except Exception as exc:
                logger.error({"event": "retention_cleanup_scheduler_failed", "error": str(exc)})
        self._set_next_run(None)

    def start_scheduler(self, stop_event: threading.Event) -> threading.Thread:
        with self._scheduler_lock:
            if self._scheduler_stop_event is stop_event and self._scheduler_thread is not None:
                if self._scheduler_thread.is_alive() or stop_event.is_set():
                    return self._scheduler_thread
            thread = threading.Thread(
                target=self.scheduler_worker,
                args=(stop_event,),
                daemon=True,
                name="retention-cleanup",
            )
            self._scheduler_stop_event = stop_event
            self._scheduler_thread = thread
            self._set_next_run(time.time())
            thread.start()
            return thread


retention_cleanup_coordinator = RetentionCleanupCoordinator()


def start_retention_cleanup_scheduler(stop_event: threading.Event) -> threading.Thread:
    return retention_cleanup_coordinator.start_scheduler(stop_event)
