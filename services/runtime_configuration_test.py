import asyncio
import os
import unittest
from unittest import mock

from services import log_service as log_module
from services.runtime_configuration import (
    DEFAULT_IMAGE_THREAD_TOKENS,
    IMAGE_THREAD_TOKENS_ENV,
    LEGACY_IMAGE_THREAD_TOKENS_ENV,
    image_thread_tokens,
)


class ImageThreadTokensTests(unittest.TestCase):
    def test_default_and_env_precedence(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(IMAGE_THREAD_TOKENS_ENV, None)
            os.environ.pop(LEGACY_IMAGE_THREAD_TOKENS_ENV, None)
            self.assertEqual(image_thread_tokens(), DEFAULT_IMAGE_THREAD_TOKENS)
            os.environ[LEGACY_IMAGE_THREAD_TOKENS_ENV] = "12"
            self.assertEqual(image_thread_tokens(), 12)
            os.environ[IMAGE_THREAD_TOKENS_ENV] = "7"
            self.assertEqual(image_thread_tokens(), 7)
            os.environ[IMAGE_THREAD_TOKENS_ENV] = "bad"
            self.assertEqual(image_thread_tokens(), DEFAULT_IMAGE_THREAD_TOKENS)


class ImageLimiterRoutingTests(unittest.TestCase):
    def setUp(self):
        log_module._image_thread_limiter = None

    def tearDown(self):
        log_module._image_thread_limiter = None

    def test_image_requests_use_dedicated_limiter(self):
        seen = []

        async def scenario():
            limiter = log_module.image_thread_limiter()

            def work():
                seen.append(limiter.borrowed_tokens)
                return "ok"

            self.assertEqual(await log_module._run_blocking(work, True), "ok")
            self.assertEqual(await log_module._run_blocking(lambda: "plain", False), "plain")

        asyncio.run(scenario())
        self.assertEqual(seen, [1])

    def test_iterate_with_image_limiter_yields_all_items_and_closes_source(self):
        closed = []

        def source():
            try:
                yield "a"
                yield "b"
            finally:
                closed.append(True)

        async def collect():
            return [item async for item in log_module._iterate_with_image_limiter(source())]

        self.assertEqual(asyncio.run(collect()), ["a", "b"])
        self.assertEqual(closed, [True])


if __name__ == "__main__":
    unittest.main()
