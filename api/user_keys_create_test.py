from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import accounts


class UserKeyCreateResponseTest(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(accounts.create_router())
        self.client = TestClient(app)
        self.auth = MagicMock()
        self.auth.create_key.return_value = ({"id": "k1", "name": "demo", "role": "user"}, "sk-raw")
        self.auth.list_keys.return_value = [{"id": "k1"}]
        for target in (
            patch.object(accounts, "auth_service", self.auth),
            patch.object(accounts, "require_admin", return_value={"role": "admin"}),
        ):
            target.start()
            self.addCleanup(target.stop)

    def test_returns_key_and_raw_key_without_items_by_default(self) -> None:
        body = self.client.post("/api/auth/users", json={"name": "demo"}).json()
        self.assertEqual(body["key"], "sk-raw")
        self.assertEqual(body["raw_key"], "sk-raw")
        self.assertEqual(body["item"]["id"], "k1")
        self.assertNotIn("items", body)

    def test_include_items_returns_full_list(self) -> None:
        body = self.client.post("/api/auth/users?include_items=1", json={"name": "demo"}).json()
        self.assertEqual(body["items"], [{"id": "k1"}])
        self.assertEqual(body["raw_key"], "sk-raw")


if __name__ == "__main__":
    unittest.main()
