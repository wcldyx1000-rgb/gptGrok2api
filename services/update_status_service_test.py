import unittest
from pathlib import Path
from unittest.mock import patch

from services import update_status_service as module
from services.update_status_service import UpdateStatusService


class VersionCompareTest(unittest.TestCase):
    def test_compare(self):
        self.assertEqual(module._compare_versions("1.2.2", "1.2.1"), 1)
        self.assertEqual(module._compare_versions("v1.2.1", "1.2.1"), 0)
        self.assertEqual(module._compare_versions("1.2.1-rc.1", "1.2.1"), -1)
        self.assertIsNone(module._compare_versions("abc", "1.0.0"))

    def test_repository_points_to_this_project(self):
        self.assertIn("AuuCoder/gptGrok2api", module.GITHUB_LATEST_RELEASE_URL)
        self.assertNotIn("yukkcat", module.GITHUB_LATEST_RELEASE_URL + module.GITHUB_RELEASES_URL)


class UpdateStatusServiceTest(unittest.TestCase):
    def service(self, tag="v1.3.0", changelog="## 1.3.0\n- [新增] x", clock=None):
        return UpdateStatusService(
            fetch_release=lambda: {"tag_name": tag, "body": "- [修复] y"},
            fetch_changelog=lambda: changelog,
            monotonic=clock or (lambda: 0.0),
        )

    def test_available_in_managed_container(self):
        with patch.object(module, "_runtime_update_mode", return_value="managed_container"):
            view = self.service().view("1.2.1")
        self.assertTrue(view.update_available)
        self.assertTrue(view.can_update)
        self.assertEqual(view.latest_tag, "v1.3.0")
        self.assertIn("AuuCoder/gptGrok2api", view.release_url)

    def test_immutable_container_cannot_update(self):
        with patch.object(module, "_runtime_update_mode", return_value="immutable_container"):
            view = self.service().view("1.2.1")
        self.assertTrue(view.update_available)
        self.assertFalse(view.can_update)
        self.assertIn("拉取新镜像", view.status_message)

    def test_up_to_date(self):
        with patch.object(module, "_runtime_update_mode", return_value="source"):
            view = self.service(tag="v1.2.1").view("1.2.1")
        self.assertFalse(view.update_available)
        self.assertFalse(view.can_update)

    def test_failure_returns_warning_view_without_network(self):
        def boom():
            raise module.UpdateCheckError("offline")

        service = UpdateStatusService(fetch_release=boom, fetch_changelog=lambda: "")
        with patch.object(module, "_runtime_update_mode", return_value="source"):
            view = service.view("1.2.1")
        self.assertEqual(view.status_label, "检查失败")
        self.assertFalse(view.update_available)

    def test_changelog_falls_back_to_release_body(self):
        def no_changelog():
            raise module.UpdateCheckError("x")

        service = UpdateStatusService(
            fetch_release=lambda: {"tag_name": "v1.3.0", "body": "- [修复] y", "published_at": "2026-10-01T00:00:00Z"},
            fetch_changelog=no_changelog,
        )
        with patch.object(module, "_runtime_update_mode", return_value="source"):
            view = service.view("1.2.1")
        self.assertIn("## 1.3.0 - 2026-10-01", view.changelog)

    def test_cache_and_force(self):
        calls = []
        now = [0.0]

        def fetch():
            calls.append(1)
            return {"tag_name": "v1.3.0"}

        service = UpdateStatusService(fetch_release=fetch, fetch_changelog=lambda: "", monotonic=lambda: now[0])
        with patch.object(module, "_runtime_update_mode", return_value="source"):
            service.view("1.2.1")
            service.view("1.2.1")
            self.assertEqual(len(calls), 1)
            service.view("1.2.1", force=True)
            self.assertEqual(len(calls), 2)
            now[0] = 10_000
            service.view("1.2.1")
            self.assertEqual(len(calls), 3)


class RuntimeModeTest(unittest.TestCase):
    def test_modes(self):
        root = Path(".")
        with patch.object(module, "_build_type", return_value="source"):
            self.assertEqual(module._runtime_update_mode(root), "source")
        with patch.object(module, "_build_type", return_value="release"), \
                patch.object(module, "_in_docker", return_value=True), \
                patch.object(module, "_has_dedicated_mount", return_value=False):
            self.assertEqual(module._runtime_update_mode(root), "immutable_container")
        with patch.object(module, "_build_type", return_value="release"), \
                patch.object(module, "_in_docker", return_value=True), \
                patch.object(module, "_has_dedicated_mount", return_value=True):
            # 缺少 marker 文件时仍视为不可变容器
            self.assertEqual(module._runtime_update_mode(Path(__file__).parent), "immutable_container")


if __name__ == "__main__":
    unittest.main()
