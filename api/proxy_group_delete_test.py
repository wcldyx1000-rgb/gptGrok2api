import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import system


class ProxyGroupDeleteGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(system.create_router("1.0.3"))
        self.client = TestClient(app)
        self.groups = [{"id": "g1", "nodes": []}, {"id": "g2", "nodes": []}]

    def _delete(self, group_id: str, references: list[str]):
        with (
            patch.object(system, "require_admin"),
            patch.object(system, "_config_dict_list", return_value=list(self.groups)),
            patch.object(system.proxy_settings, "proxy_group_references", return_value=references),
            patch.object(system.config, "update", return_value={"proxy_groups": [self.groups[1]]}) as update,
        ):
            response = self.client.delete(f"/api/proxy/groups/{group_id}")
        return response, update

    def test_referenced_group_cannot_be_deleted(self) -> None:
        response, update = self._delete("g1", ["全局默认代理", "3 个账号"])

        self.assertEqual(response.status_code, 409)
        body = response.json()["detail"]
        self.assertEqual(body["references"], ["全局默认代理", "3 个账号"])
        self.assertIn("still in use", body["error"])
        update.assert_not_called()

    def test_unreferenced_group_is_deleted(self) -> None:
        response, update = self._delete("g1", [])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted"], "g1")
        update.assert_called_once_with({"proxy_groups": [self.groups[1]]})

    def test_unknown_group_is_404_before_reference_check(self) -> None:
        response, update = self._delete("nope", ["x"])

        self.assertEqual(response.status_code, 404)
        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
