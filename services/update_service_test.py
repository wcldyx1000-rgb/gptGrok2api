import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services import update_service as module
from services.update_service import ReleaseBundleInstaller, UpdateInstallError, UpdateService

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_update_bundle", PROJECT_ROOT / "scripts" / "build_update_bundle.py"
    )
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    return builder


def _populate(root: Path, marker: str) -> None:
    for target in module.UPDATE_TARGETS:
        path = root / target
        if target not in {"app", "api", "services", "utils", "scripts", "web_dist"}:
            path.parent.mkdir(parents=True, exist_ok=True)
            same = target in module.NODE_DEPENDENCY_FILES  # Node 依赖文件两版保持一致
            path.write_text("node-deps" if same else f"{marker}:{target}", encoding="utf-8")
        else:
            path.mkdir(parents=True, exist_ok=True)
            (path / "file.txt").write_text(f"{marker}:{target}", encoding="utf-8")
    (root / "VERSION").write_text(marker, encoding="utf-8")


class BundleBuilderTest(unittest.TestCase):
    def test_targets_match_builder(self):
        builder = _load_builder()
        self.assertEqual(tuple(builder.UPDATE_TARGETS), module.UPDATE_TARGETS)
        self.assertEqual(builder.ARCHIVE_NAME, module.UPDATE_ARCHIVE_NAME)
        self.assertEqual(builder.BUNDLE_DIR, module.UPDATE_BUNDLE_DIR)


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.root = base / "runtime"
        self.src = base / "src"
        self.out = base / "out"
        self.root.mkdir()
        self.src.mkdir()
        _populate(self.root, "1.0.0")
        _populate(self.src, "2.0.0")
        # 数据与配置不属于更新目标，必须保持不动。
        (self.root / "data").mkdir()
        (self.root / "data" / "keep.json").write_text("keep", encoding="utf-8")
        (self.root / "config.json").write_text("cfg", encoding="utf-8")
        self.builder = _load_builder()

    def installer(self):
        return ReleaseBundleInstaller(self.root, runtime_mode=lambda root: "managed_container")

    def build_release(self, version="2.0.0"):
        archive = self.builder.build(self.src, self.out, version)
        return archive, self.out / "checksums.txt"

    @staticmethod
    def fake_download(archive, checksum):
        def download(url, destination, *, max_bytes):
            source = archive if url.endswith(module.UPDATE_ARCHIVE_NAME) else checksum
            destination.write_bytes(source.read_bytes())

        return download

    @staticmethod
    def release():
        base = "https://github.com/x/y/releases/download/v2.0.0/"
        return {"assets": [
            {"name": module.UPDATE_ARCHIVE_NAME, "browser_download_url": base + module.UPDATE_ARCHIVE_NAME},
            {"name": module.UPDATE_CHECKSUM_NAME, "browser_download_url": base + "checksums.txt"},
        ]}

    def test_install_success_replaces_app_files_only(self):
        archive, checksum = self.build_release()
        stages = []
        with patch.object(ReleaseBundleInstaller, "_download", staticmethod(self.fake_download(archive, checksum))), \
                patch.object(ReleaseBundleInstaller, "_sync_dependencies", lambda self: None):
            self.installer().install(self.release(), "v2.0.0", progress=lambda *args: stages.append(args[0]))
        self.assertEqual((self.root / "VERSION").read_text(), "2.0.0")
        self.assertEqual((self.root / "services" / "file.txt").read_text(), "2.0.0:services")
        self.assertEqual((self.root / "data" / "keep.json").read_text(), "keep")
        self.assertEqual((self.root / "config.json").read_text(), "cfg")
        self.assertEqual(stages, ["downloading", "verifying", "installing", "syncing"])
        # 上一版本被保留用于手动回退
        self.assertEqual((self.root / module.UPDATE_BACKUP_DIR / "VERSION").read_text(), "1.0.0")
        leftovers = [p for p in self.root.iterdir() if p.name.startswith(".gptgrok2api-update-") and p.name != module.UPDATE_BACKUP_DIR]
        self.assertEqual(leftovers, [])

    def test_dependency_sync_failure_rolls_back(self):
        archive, checksum = self.build_release()
        calls = []

        def sync(self):
            calls.append(1)
            if len(calls) == 1:
                raise UpdateInstallError("boom")

        with patch.object(ReleaseBundleInstaller, "_download", staticmethod(self.fake_download(archive, checksum))), \
                patch.object(ReleaseBundleInstaller, "_sync_dependencies", sync):
            with self.assertRaises(UpdateInstallError) as ctx:
                self.installer().install(self.release(), "v2.0.0")
        self.assertIn("已恢复原版本", str(ctx.exception))
        self.assertEqual((self.root / "VERSION").read_text(), "1.0.0")
        self.assertEqual((self.root / "services" / "file.txt").read_text(), "1.0.0:services")
        self.assertEqual(len(calls), 2)  # 回退后重新同步旧依赖

    def test_checksum_mismatch_rejected_before_touching_files(self):
        archive, checksum = self.build_release()
        checksum.write_text(f"{'0' * 64}  {module.UPDATE_ARCHIVE_NAME}\n", encoding="utf-8")
        with patch.object(ReleaseBundleInstaller, "_download", staticmethod(self.fake_download(archive, checksum))):
            with self.assertRaises(UpdateInstallError):
                self.installer().install(self.release(), "v2.0.0")
        self.assertEqual((self.root / "VERSION").read_text(), "1.0.0")

    def test_version_mismatch_rejected(self):
        archive, checksum = self.build_release("2.0.0")
        with patch.object(ReleaseBundleInstaller, "_download", staticmethod(self.fake_download(archive, checksum))):
            with self.assertRaises(UpdateInstallError):
                self.installer().install(self.release(), "v3.0.0")
        self.assertEqual((self.root / "VERSION").read_text(), "1.0.0")

    def test_node_dependency_change_requires_new_image(self):
        (self.src / "package-lock.json").write_text("changed", encoding="utf-8")
        archive, checksum = self.build_release()
        with patch.object(ReleaseBundleInstaller, "_download", staticmethod(self.fake_download(archive, checksum))):
            with self.assertRaises(UpdateInstallError) as ctx:
                self.installer().install(self.release(), "v2.0.0")
        self.assertIn("Node", str(ctx.exception))
        self.assertEqual((self.root / "VERSION").read_text(), "1.0.0")

    def test_unsafe_archive_paths_rejected(self):
        evil = Path(self._tmp.name) / "evil.tar.gz"
        with tarfile.open(evil, "w:gz") as archive:
            data = b"x"
            info = tarfile.TarInfo("../escape.txt")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        with self.assertRaises(UpdateInstallError):
            ReleaseBundleInstaller._extract_archive(evil, Path(self._tmp.name) / "dest")

    def test_symlink_members_rejected(self):
        evil = Path(self._tmp.name) / "link.tar.gz"
        with tarfile.open(evil, "w:gz") as archive:
            info = tarfile.TarInfo("link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
        with self.assertRaises(UpdateInstallError):
            ReleaseBundleInstaller._extract_archive(evil, Path(self._tmp.name) / "dest2")

    def test_non_managed_runtime_refused(self):
        installer = ReleaseBundleInstaller(self.root, runtime_mode=lambda root: "immutable_container")
        with self.assertRaises(UpdateInstallError):
            installer.install(self.release(), "v2.0.0")

    def test_download_hosts_restricted_and_assets_required(self):
        with self.assertRaises(UpdateInstallError):
            module._safe_download_url("https://evil.example.com/a.tar.gz")
        with self.assertRaises(UpdateInstallError):
            module._safe_download_url("http://github.com/a.tar.gz")
        with self.assertRaises(UpdateInstallError):
            ReleaseBundleInstaller._asset_urls({"assets": []})

    def test_checksum_file_format(self):
        archive = Path(self._tmp.name) / "a.tar.gz"
        archive.write_bytes(b"abc")
        checksum = Path(self._tmp.name) / "sums.txt"
        digest = hashlib.sha256(b"abc").hexdigest()
        checksum.write_text(f"{digest} *{module.UPDATE_ARCHIVE_NAME}\n", encoding="utf-8")
        ReleaseBundleInstaller._verify_checksum(archive, checksum)


class FakeInstaller:
    def __init__(self, root: Path, error: Exception | None = None):
        self.root = root
        self.error = error
        self.installed = []

    def install(self, release, latest_tag, *, progress=None):
        if progress:
            progress("downloading", 2, "下载更新包", "x")
        if self.error:
            raise self.error
        self.installed.append(latest_tag)


class UpdateServiceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.exits = []

    def service(self, *, tag="v1.3.0", error=None, mode="managed_container"):
        self.installer = FakeInstaller(self.root, error)
        return UpdateService(
            fetch_release=lambda: {"tag_name": tag},
            installer=self.installer,
            runtime_mode=lambda root: mode,
            state_path=self.root / "update_task.json",
            run_worker=lambda callback: callback(),
            schedule_exit=lambda callback: callback(),
            exit_process=self.exits.append,
        )

    def test_idle_by_default_and_never_auto_starts(self):
        service = self.service()
        view = service.view("1.2.1")
        self.assertEqual(view.state, "idle")
        self.assertEqual(self.installer.installed, [])
        self.assertEqual(self.exits, [])

    def test_successful_update_installs_then_restarts(self):
        service = self.service()
        service.start("1.2.1")
        view = service.view("1.2.1")
        self.assertEqual(self.installer.installed, ["v1.3.0"])
        self.assertEqual(view.stage, "restarting")
        self.assertEqual(self.exits, [0])
        persisted = json.loads((self.root / "update_task.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["latest_tag"], "v1.3.0")

    def test_after_restart_version_matches_marks_success(self):
        self.service().start("1.2.1")
        view = self.service().view("1.3.0")
        self.assertEqual(view.state, "succeeded")

    def test_restart_without_new_version_marks_failed(self):
        self.service().start("1.2.1")
        view = self.service().view("1.2.1")
        self.assertEqual(view.state, "failed")
        self.assertTrue(view.error)

    def test_install_error_is_reported_and_no_restart(self):
        service = self.service(error=UpdateInstallError("更新包完整性校验失败。"))
        service.start("1.2.1")
        view = service.view("1.2.1")
        self.assertEqual(view.state, "failed")
        self.assertEqual(view.error, "更新包完整性校验失败。")
        self.assertEqual(self.exits, [])

    def test_unexpected_error_message_is_generic(self):
        service = self.service(error=RuntimeError("secret path /x"))
        service.start("1.2.1")
        view = service.view("1.2.1")
        self.assertEqual(view.state, "failed")
        self.assertNotIn("secret", view.error)

    def test_already_latest_does_nothing(self):
        service = self.service(tag="v1.2.1")
        service.start("1.2.1")
        self.assertEqual(self.installer.installed, [])
        self.assertEqual(service.view("1.2.1").status_label, "无需更新")
        self.assertEqual(self.exits, [])

    def test_unsupported_runtime_raises(self):
        service = self.service(mode="source")
        with self.assertRaises(UpdateInstallError):
            service.start("1.2.1")


if __name__ == "__main__":
    unittest.main()
