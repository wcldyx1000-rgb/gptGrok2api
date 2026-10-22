from __future__ import annotations

import unittest
from unittest.mock import patch

from services import model_catalog_service
from services.protocol import openai_v1_models


class ModelCatalogServiceTest(unittest.TestCase):
    def test_fallback_catalog_exposes_web_picker_models(self) -> None:
        with (
            patch.object(model_catalog_service.config, "get", return_value={}),
            patch.object(model_catalog_service.account_service, "list_accounts", return_value=[]),
        ):
            catalog = model_catalog_service.get_model_catalog()

        self.assertTrue({"auto", "gpt-6-t-mini", "gpt-6"}.issubset(catalog["chat_models"]))
        self.assertIn("gpt-6", catalog["all_models"])
        self.assertIn("gpt-image-2", catalog["image_edit_models"])

    def test_openai_models_output_includes_gpt_5_6_from_catalog(self) -> None:
        catalog = {
            "models": [],
            "chat_models": ["gpt-5-6"],
            "image_models": [],
        }
        with (
            patch.object(openai_v1_models, "get_model_catalog", return_value=catalog),
            patch.object(openai_v1_models, "_dynamic_image_models", return_value=[]),
        ):
            payload = openai_v1_models.list_models()

        self.assertEqual([item["id"] for item in payload["data"]], ["gpt-5-6"])

    def test_catalog_only_advertises_verified_grok_cli_oauth_models(self) -> None:
        from services.xai_cli_oauth_service import xai_cli_oauth_service

        with (
            patch.object(model_catalog_service.config, "get", return_value={}),
            patch.object(model_catalog_service.account_service, "list_accounts", return_value=[]),
            patch.object(
                xai_cli_oauth_service,
                "model_items",
                return_value=[
                    {
                        "id": "grok-4.5",
                        "object": "model",
                        "owned_by": "xai",
                        "provider": "grok",
                        "capabilities": ["chat"],
                    }
                ],
            ),
        ):
            catalog = model_catalog_service.get_model_catalog()

        self.assertIn("grok-4.5", catalog["chat_models"])
        self.assertEqual(catalog["models"][-1]["id"], "grok-4.5")

    def test_list_models_never_calls_upstream(self) -> None:
        catalog = {"models": [], "chat_models": ["auto"], "image_models": ["gpt-image-2"]}
        self.assertFalse(hasattr(openai_v1_models, "OpenAIBackendAPI"))
        with (
            patch.object(openai_v1_models, "get_model_catalog", return_value=catalog),
            patch.object(openai_v1_models, "_dynamic_image_models", return_value=[]),
        ):
            payload = openai_v1_models.list_models()
        self.assertEqual([item["id"] for item in payload["data"]], ["auto", "gpt-image-2"])

    def test_upstream_cache_get_is_passive_until_started_then_self_heals(self) -> None:
        import threading
        import time

        cache = model_catalog_service._UpstreamChatModels()
        cache.set(["auto"])
        with patch.object(cache, "refresh") as refresh:
            self.assertEqual(cache.get(), ["auto"])
            refresh.assert_not_called()
            cache._started = True
            cache._last_attempt_at = time.monotonic()
            cache.get()
            refresh.assert_not_called()
            cache._last_attempt_at = time.monotonic() - 10_000
            done = threading.Event()
            refresh.side_effect = lambda: done.set()
            self.assertEqual(cache.get(), ["auto"])  # 不阻塞，立即返回旧缓存
            self.assertTrue(done.wait(2))
        deadline = time.monotonic() + 2
        while cache._healing and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(cache._healing)


class ModelCatalogContractTest(unittest.TestCase):
    def _catalog(self, settings=None, image_models=None):
        settings = dict(settings or {})
        if image_models is not None:
            settings["image_generation"] = {"model_options": image_models}
        with (
            patch.object(model_catalog_service.config, "get", return_value=settings),
            patch.object(model_catalog_service.account_service, "list_accounts", return_value=[]),
        ):
            return model_catalog_service.get_model_catalog()

    def test_catalog_has_defaults_capabilities_and_revision(self) -> None:
        catalog = self._catalog(
            {"image_upscale_enabled": True},
            image_models=["gpt-image-2", "codex-gpt-image-2", "plus-codex-gpt-image-2"],
        )

        self.assertEqual(catalog["schema_version"], 1)
        self.assertEqual(catalog["defaults"]["image_model"], "gpt-image-2")
        self.assertIn(catalog["defaults"]["chat_model"], catalog["chat_models"])
        self.assertTrue(catalog["capabilities"]["image_upscale"])
        self.assertEqual(
            catalog["capabilities"]["high_resolution_image_models"],
            ["codex-gpt-image-2", "plus-codex-gpt-image-2"],
        )
        self.assertEqual(len(catalog["revision"]), 16)
        self.assertTrue(catalog["generated_at"].endswith("Z"))

    def test_revision_is_stable_and_changes_with_content(self) -> None:
        first = self._catalog(image_models=["gpt-image-2"])
        again = self._catalog(image_models=["gpt-image-2"])
        other = self._catalog(image_models=["gpt-image-2", "codex-gpt-image-2"])

        self.assertEqual(first["revision"], again["revision"])
        self.assertNotEqual(first["revision"], other["revision"])


if __name__ == "__main__":
    unittest.main()
