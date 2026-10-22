import json
import threading
import time
import unittest
from unittest.mock import patch

from services import openai_backend_api
from services.config import config
from services.image_warm_pool import WARM_MAX_ACCOUNTS, WARM_TTL_SECS, ImageWarmPool, WarmState
from services.openai_backend_api import ChatRequirements, OpenAIBackendAPI
from services.proxy_service import ProxyRuntimeProfile
from utils.helper import UpstreamHTTPError

FILE_ID = "file_00000000" + "a" * 24

HANDOFF_PAYLOAD = json.dumps({
    "v": {
        "message": {
            "author": {"role": "assistant"},
            "status": "finished_successfully",
            "end_turn": False,
            "metadata": {"is_complete": True},
        },
    },
})
IMAGE_PAYLOAD = json.dumps({"v": {"message": {"content": {"parts": [f"sediment://{FILE_ID}"]}}}})
TERMINAL_PAYLOAD = json.dumps({
    "v": {
        "message": {
            "author": {"role": "assistant"},
            "status": "finished_successfully",
            "end_turn": True,
            "metadata": {"is_complete": True},
        },
    },
})


class FakeStreamResponse:
    """iter_content 依次吐出 SSE 行；block_after 为 True 时最后一直挂着不结束。"""

    status_code = 200

    def __init__(self, payloads: list[str], block_after: bool = False) -> None:
        self._payloads = payloads
        self._block_after = block_after
        self._release = threading.Event()
        self.closed = False

    def iter_content(self):
        for payload in self._payloads:
            yield f"data: {payload}\n\n".encode()
        if self._block_after:
            self._release.wait(timeout=10)

    def close(self) -> None:
        self.closed = True
        self._release.set()


def _backend(egress_key: str = "direct", device_id: str = "device-1") -> OpenAIBackendAPI:
    account = {"access_token": "token", "fp": {"oai-device-id": device_id}}
    with patch.object(openai_backend_api.account_service, "get_account", return_value=account):
        return OpenAIBackendAPI(access_token="token", proxy_profile=ProxyRuntimeProfile(egress_key=egress_key))


class ImageStreamTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._config_patch = patch.dict(config.data, {
            "image_warm_enabled": False,
            "image_poll_adaptive": True,
            "image_stream_post_tool_wait_secs": 45,
            "image_drop_extra_variants": True,
            "image_stream_direct_resolve": True,
        })
        self._config_patch.start()
        self.addCleanup(self._config_patch.stop)
        openai_backend_api._image_poll_factors.clear()
        self.addCleanup(openai_backend_api._image_poll_factors.clear)

    def _run_stream(self, backend: OpenAIBackendAPI, response: FakeStreamResponse) -> list[str]:
        requirements = ChatRequirements(token="req")
        with patch.object(backend, "_bootstrap") as bootstrap, \
                patch.object(backend, "_get_chat_requirements", return_value=requirements), \
                patch.object(backend, "_prepare_image_conversation", return_value="conduit"), \
                patch.object(backend, "_start_image_generation", return_value=response):
            payloads = list(backend._stream_picture_conversation("draw a cat", "gpt-image-2", []))
            self.bootstrap_calls = bootstrap.call_count
        return payloads


class ImageStreamContinuationTest(ImageStreamTestBase):
    def test_handoff_payload_keeps_reading_same_stream_for_image_ids(self) -> None:
        response = FakeStreamResponse([HANDOFF_PAYLOAD, IMAGE_PAYLOAD, TERMINAL_PAYLOAD, "after-terminal"])
        with _backend() as backend:
            payloads = self._run_stream(backend, response)
        self.assertEqual(payloads, [HANDOFF_PAYLOAD, IMAGE_PAYLOAD, TERMINAL_PAYLOAD])
        self.assertTrue(response.closed)

    def test_handoff_without_image_ids_falls_back_after_post_tool_wait(self) -> None:
        config.data["image_stream_post_tool_wait_secs"] = 0.2
        response = FakeStreamResponse([HANDOFF_PAYLOAD], block_after=True)
        started = time.monotonic()
        with _backend() as backend:
            payloads = self._run_stream(backend, response)
        # 超时平滑结束流（不抛异常），交给轮询路径继续等结果。
        self.assertEqual(payloads, [HANDOFF_PAYLOAD])
        self.assertLess(time.monotonic() - started, 5)

    def test_zero_post_tool_wait_keeps_legacy_break_at_handoff(self) -> None:
        config.data["image_stream_post_tool_wait_secs"] = 0
        response = FakeStreamResponse([HANDOFF_PAYLOAD, IMAGE_PAYLOAD])
        with _backend() as backend:
            payloads = self._run_stream(backend, response)
        self.assertEqual(payloads, [HANDOFF_PAYLOAD])

    def test_handoff_detection(self) -> None:
        self.assertTrue(OpenAIBackendAPI._is_image_stream_tool_handoff_payload(HANDOFF_PAYLOAD))
        self.assertFalse(OpenAIBackendAPI._is_image_stream_terminal_payload(HANDOFF_PAYLOAD))
        self.assertFalse(OpenAIBackendAPI._is_image_stream_tool_handoff_payload(TERMINAL_PAYLOAD))
        self.assertTrue(OpenAIBackendAPI._is_image_stream_terminal_payload(TERMINAL_PAYLOAD))
        patch_payload = json.dumps({"p": "/message/end_turn", "v": False, "is_complete": True, "status": "finished"})
        self.assertTrue(OpenAIBackendAPI._payload_continues_turn(json.loads(patch_payload)))


class AdaptivePollTest(ImageStreamTestBase):
    def test_rate_limit_doubles_until_upper_bound(self) -> None:
        with _backend() as backend:
            self.assertEqual(backend._adaptive_poll_secs(2.0), 2.0)
            backend._record_image_poll_outcome(rate_limited=True)
            self.assertEqual(backend._adaptive_poll_secs(2.0), 4.0)
            backend._record_image_poll_outcome(rate_limited=True)
            self.assertEqual(backend._adaptive_poll_secs(2.0), 8.0)
            for _ in range(10):
                backend._record_image_poll_outcome(rate_limited=True)
            self.assertEqual(backend._adaptive_poll_secs(2.0), 16.0)

    def test_success_decays_down_to_lower_bound(self) -> None:
        with _backend() as backend:
            for _ in range(50):
                backend._record_image_poll_outcome(rate_limited=False)
            self.assertEqual(backend._adaptive_poll_secs(2.0), 1.0)
            # 配置值本身低于 1 秒或高于 16 秒时以配置值为界，不改变旧配置含义。
            self.assertEqual(backend._adaptive_poll_secs(0.0), 0.0)
            self.assertEqual(backend._adaptive_poll_secs(0.6), 0.6)
            self.assertEqual(backend._adaptive_poll_secs(40.0), 20.0)

    def test_disabled_returns_configured_value(self) -> None:
        config.data["image_poll_adaptive"] = False
        with _backend() as backend:
            backend._record_image_poll_outcome(rate_limited=True)
            self.assertEqual(backend._adaptive_poll_secs(10.0), 10.0)

    def test_rate_limit_is_shared_by_accounts_on_same_egress(self) -> None:
        with _backend("egress-a", "device-1") as first, \
                _backend("egress-a", "device-2") as same_egress, \
                _backend("egress-b", "device-3") as other_egress:
            first._record_image_poll_outcome(rate_limited=True)
            self.assertEqual(same_egress._adaptive_poll_secs(2.0), 4.0)
            self.assertEqual(other_egress._adaptive_poll_secs(2.0), 2.0)

    def test_poll_loop_backs_off_on_429_and_records_outcome(self) -> None:
        config.data.update({
            "image_poll_initial_wait_secs": 0,
            "image_poll_interval_secs": 2,
            "image_settle_enabled": False,
            "image_check_before_hit_enabled": False,
        })
        conversation = {"mapping": {}}
        calls = {"count": 0}

        def get_conversation(_conversation_id):
            calls["count"] += 1
            if calls["count"] == 1:
                raise UpstreamHTTPError("conversation", 429, "slow down", retry_after=0)
            return conversation

        with _backend() as backend, \
                patch.object(backend, "_query_backend_tasks", return_value=[]), \
                patch.object(backend, "_get_conversation", side_effect=get_conversation), \
                patch.object(backend, "_extract_image_tool_records",
                             return_value=[{"file_ids": [FILE_ID], "sediment_ids": []}]), \
                patch.object(openai_backend_api.time, "sleep"):
            file_ids, _ = backend._poll_image_results("conv-1", timeout_secs=30)
            factors = dict(openai_backend_api._image_poll_factors)
        self.assertEqual(file_ids, [FILE_ID])
        # 429 翻倍（2.0）后一次成功查询回落（×0.9）。
        self.assertAlmostEqual(factors["account:device-1"], 1.8)
        self.assertAlmostEqual(factors["egress:direct"], 1.8)


class VariantAndDirectResolveTest(ImageStreamTestBase):
    def test_direct_resolve_keeps_only_first_variant(self) -> None:
        with _backend() as backend, \
                patch.object(backend, "_resolve_image_urls", return_value=["https://a", "https://b"]), \
                patch.object(backend, "_poll_image_results") as poll:
            urls = backend.resolve_conversation_image_urls("conv-1", [FILE_ID, "file_00000000" + "b" * 24], [])
        self.assertEqual(urls, ["https://a"])
        poll.assert_not_called()

    def test_variant_drop_can_be_disabled(self) -> None:
        config.data["image_drop_extra_variants"] = False
        with _backend() as backend, \
                patch.object(backend, "_resolve_image_urls", return_value=["https://a", "https://b"]):
            urls = backend.resolve_conversation_image_urls("conv-1", [FILE_ID], [])
        self.assertEqual(urls, ["https://a", "https://b"])

    def test_direct_resolve_failure_falls_back_to_poll(self) -> None:
        with _backend() as backend, \
                patch.object(backend, "_resolve_image_urls", side_effect=[[], ["https://late"]]), \
                patch.object(backend, "_poll_image_results", return_value=([FILE_ID], [])) as poll:
            urls = backend.resolve_conversation_image_urls("conv-1", [FILE_ID], [])
        self.assertEqual(urls, ["https://late"])
        poll.assert_called_once()

    def test_direct_resolve_disabled_uses_settle_poll(self) -> None:
        config.data.update({
            "image_stream_direct_resolve": False,
            "image_settle_enabled": True,
            "image_check_before_hit_enabled": True,
        })
        with _backend() as backend, \
                patch.object(backend, "_resolve_image_urls", return_value=["https://a"]), \
                patch.object(backend, "_poll_image_results", return_value=([FILE_ID], [])) as poll:
            backend.resolve_conversation_image_urls("conv-1", [FILE_ID], [])
        poll.assert_called_once()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _state(token: str, key=None) -> WarmState:
    return WarmState(
        key=key or (token, "direct", "chrome142", "device"),
        session_id="warm-session",
        cookies=[],
        pow_script_sources=["https://chatgpt.com/s.js"],
        pow_data_build="build-1",
        requirements=ChatRequirements(token="warm-req"),
    )


class ImageWarmPoolTest(unittest.TestCase):
    def _pool(self, idle: list[str], clock: FakeClock, warm_fn=None) -> ImageWarmPool:
        pool = ImageWarmPool(
            warm_fn=warm_fn or (lambda token: _state(token)),
            idle_tokens_fn=lambda: list(idle),
            enabled_fn=lambda: True,
            clock=clock,
        )
        self.addCleanup(pool.shutdown)
        return pool

    @staticmethod
    def _activate(pool: ImageWarmPool, clock: FakeClock) -> None:
        # 只记活跃时间，不启动后台循环，由测试手动 refill。
        pool._last_activity = clock()

    @staticmethod
    def _wait_ready(pool: ImageWarmPool, count: int) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            stats = pool.stats()
            if stats["ready"] >= count and stats["warming"] == 0:
                return
            time.sleep(0.01)
        raise AssertionError(f"warm pool not ready: {pool.stats()}")

    def test_state_is_single_use(self) -> None:
        clock = FakeClock()
        pool = self._pool(["t1"], clock)
        self._activate(pool, clock)
        self.assertEqual(pool.refill(), ["t1"])
        self._wait_ready(pool, 1)
        key = ("t1", "direct", "chrome142", "device")
        self.assertIsNotNone(pool.take(key))
        self.assertIsNone(pool.take(key))
        self.assertEqual((pool.hits, pool.misses), (1, 1))

    def test_state_expires_after_ttl(self) -> None:
        clock = FakeClock()
        pool = self._pool(["t1"], clock)
        self._activate(pool, clock)
        pool.refill()
        self._wait_ready(pool, 1)
        clock.now += WARM_TTL_SECS + 1
        self.assertIsNone(pool.take(("t1", "direct", "chrome142", "device")))

    def test_key_mismatch_is_not_used(self) -> None:
        clock = FakeClock()
        pool = self._pool(["t1"], clock)
        self._activate(pool, clock)
        pool.refill()
        self._wait_ready(pool, 1)
        self.assertIsNone(pool.take(("t1", "other-egress", "chrome142", "device")))

    def test_refill_is_capped_and_needs_recent_activity(self) -> None:
        clock = FakeClock()
        idle = [f"t{i}" for i in range(WARM_MAX_ACCOUNTS + 4)]
        pool = self._pool(idle, clock)
        self.assertEqual(pool.refill(), [])
        self._activate(pool, clock)
        self.assertEqual(len(pool.refill()), WARM_MAX_ACCOUNTS)
        self._wait_ready(pool, WARM_MAX_ACCOUNTS)
        self.assertEqual(pool.refill(), [])

    def test_warm_failure_does_not_raise(self) -> None:
        clock = FakeClock()

        def fail(_token):
            raise RuntimeError("boom")

        pool = self._pool(["t1"], clock, warm_fn=fail)
        self._activate(pool, clock)
        pool.refill()
        self._wait_ready(pool, 0)
        self.assertEqual(pool.stats()["ready"], 0)

    def test_shutdown_stops_loop_and_ignores_activity(self) -> None:
        clock = FakeClock()
        pool = self._pool(["t1"], clock)
        pool.note_activity()
        pool.shutdown()
        self.assertIsNone(pool._loop_thread)
        pool.note_activity()
        self.assertIsNone(pool._loop_thread)
        self.assertEqual(pool.refill(), [])


class BackendWarmAdoptionTest(ImageStreamTestBase):
    def test_adopts_warm_state_and_skips_bootstrap(self) -> None:
        config.data["image_warm_enabled"] = True
        response = FakeStreamResponse([TERMINAL_PAYLOAD])
        with _backend() as backend:
            warm = _state("token")
            with patch("services.image_warm_pool.image_warm_pool.take", return_value=warm), \
                    patch("services.image_warm_pool.image_warm_pool.note_activity"), \
                    patch.object(backend, "_get_chat_requirements") as get_requirements, \
                    patch.object(backend, "_bootstrap") as bootstrap, \
                    patch.object(backend, "_prepare_image_conversation", return_value="conduit") as prepare, \
                    patch.object(backend, "_start_image_generation", return_value=response):
                list(backend._stream_picture_conversation("draw", "gpt-image-2", []))
            bootstrap.assert_not_called()
            get_requirements.assert_not_called()
            self.assertEqual(prepare.call_args.args[1].token, "warm-req")
            self.assertEqual(backend.session_id, "warm-session")
            self.assertEqual(backend.session.headers["OAI-Session-Id"], "warm-session")
            self.assertEqual(backend.pow_data_build, "build-1")

    def test_rejected_warm_token_is_refetched_once(self) -> None:
        config.data["image_warm_enabled"] = True
        response = FakeStreamResponse([TERMINAL_PAYLOAD])
        fresh = ChatRequirements(token="fresh-req")
        with _backend() as backend, \
                patch("services.image_warm_pool.image_warm_pool.take", return_value=_state("token")), \
                patch("services.image_warm_pool.image_warm_pool.note_activity"), \
                patch.object(backend, "_get_chat_requirements", return_value=fresh), \
                patch.object(backend, "_bootstrap") as bootstrap, \
                patch.object(backend, "_prepare_image_conversation",
                             side_effect=[UpstreamHTTPError("prepare", 403, "bad token"), "conduit"]) as prepare, \
                patch.object(backend, "_start_image_generation", return_value=response):
            list(backend._stream_picture_conversation("draw", "gpt-image-2", []))
        bootstrap.assert_called_once()
        self.assertEqual(prepare.call_args.args[1].token, "fresh-req")

    def test_warm_disabled_uses_normal_flow(self) -> None:
        response = FakeStreamResponse([TERMINAL_PAYLOAD])
        with _backend() as backend, \
                patch("services.image_warm_pool.image_warm_pool.take") as take:
            self._run_stream(backend, response)
        take.assert_not_called()
        self.assertEqual(self.bootstrap_calls, 1)


if __name__ == "__main__":
    unittest.main()
