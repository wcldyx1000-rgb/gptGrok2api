"""chat/responses 图片链路携带 image_attempts，以及并行多图尾部失败槽位的尝试记录保留。"""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from services import log_service
from services.log_service import LoggedCall
from services.protocol import conversation as conv
from services.protocol import openai_v1_chat_complete as chat
from services.protocol import openai_v1_response as resp


def _attempt(slot: int, attempt: int, status: str = "failed") -> dict:
    return {"slot": slot, "attempt": attempt, "status": status, "account_email": f"a{slot}@x.test"}


def _result(attempts=None) -> conv.ImageOutput:
    return conv.ImageOutput(
        kind="result", model="gpt-image-2", index=1, total=1,
        data=[{"b64_json": "aGk="}], image_attempts=list(attempts or []),
    )


class ChatImageAttemptsTest(unittest.TestCase):
    def test_stream_final_chunk_carries_attempts(self) -> None:
        attempts = [_attempt(1, 1)]
        chunks = list(chat.stream_image_chat_completion([_result(attempts)], "gpt-image-2"))
        carried = [c for c in chunks if c.get("_image_attempts")]
        self.assertEqual(len(carried), 1)
        self.assertEqual(carried[0]["_image_attempts"], attempts)
        self.assertEqual(log_service.collect_image_attempts(chunks)[0]["slot"], 1)

    def test_stream_without_attempts_adds_nothing(self) -> None:
        chunks = list(chat.stream_image_chat_completion([_result()], "gpt-image-2"))
        self.assertFalse(any("_image_attempts" in c for c in chunks))

    def test_trailing_progress_output_attempts_are_collected(self) -> None:
        trailing = conv.ImageOutput(kind="progress", model="m", index=2, total=2, image_attempts=[_attempt(2, 1)])
        chunks = list(chat.stream_image_chat_completion([_result(), trailing], "gpt-image-2"))
        self.assertEqual([a["slot"] for a in log_service.collect_image_attempts(chunks)], [2])

    def test_non_stream_response_carries_attempts(self) -> None:
        outputs = [_result([_attempt(1, 1)])]
        body = {"model": "gpt-image-2", "messages": [{"role": "user", "content": "draw"}]}
        with patch.object(chat, "stream_image_outputs_with_pool", return_value=iter(outputs)):
            response = chat.image_chat_response(body)
        self.assertEqual(response["_image_attempts"][0]["slot"], 1)


class ResponsesImageAttemptsTest(unittest.TestCase):
    def test_completed_event_carries_attempts(self) -> None:
        events = list(resp.stream_image_response([_result([_attempt(1, 1)])], "p", "gpt-image-2"))
        completed = events[-1]
        self.assertEqual(completed["_image_attempts"][0]["slot"], 1)
        self.assertEqual(completed["response"]["_image_attempts"][0]["slot"], 1)

    def test_failure_exception_carries_attempts(self) -> None:
        progress = conv.ImageOutput(kind="progress", model="m", index=1, total=1, image_attempts=[_attempt(1, 1)])
        with self.assertRaises(RuntimeError) as ctx:
            list(resp.stream_image_response([progress], "p", "gpt-image-2"))
        self.assertEqual(log_service.collect_image_attempts(ctx.exception)[0]["slot"], 1)


class ParallelTrailingFailureTest(unittest.TestCase):
    def _run(self, plan):
        """plan: {index: ("ok", attempts) | ("fail", attempts)}；失败的槽位等成功输出产出后再失败。"""
        yielded_first = {"done": False}

        def fake_single(request, index, total):
            kind, attempts = plan[index]
            if kind == "fail":
                # 等到成功输出已被消费后才失败。
                deadline = time.time() + 5
                while not yielded_first["done"] and time.time() < deadline:
                    time.sleep(0.005)
                exc = conv.ImageGenerationError("boom")
                exc.image_attempts = list(attempts)
                raise exc
            return [_result(attempts)]

        request = conv.ConversationRequest(prompt="p", model="gpt-image-2", n=2)
        out = []
        with patch.object(conv, "_generate_single_image", side_effect=fake_single):
            for output in conv.stream_image_outputs_with_pool(request):
                out.append(output)
                yielded_first["done"] = True
        return out

    def test_late_failed_slot_attempts_are_not_lost(self) -> None:
        outputs = self._run({1: ("ok", []), 2: ("fail", [_attempt(2, 1)])})
        all_attempts = [a for o in outputs for a in o.image_attempts]
        self.assertEqual([a["slot"] for a in all_attempts], [2])
        collected = conv.collect_image_outputs(outputs)
        self.assertEqual(collected["_image_attempts"][0]["slot"], 2)
        self.assertEqual(len(collected["data"]), 1)

    def test_image_chunks_log_only_event_is_hidden_from_client(self) -> None:
        outputs = self._run({1: ("ok", []), 2: ("fail", [_attempt(2, 1)])})
        chunks = list(conv.stream_image_chunks(outputs))
        self.assertTrue(any(c.get("_log_only") for c in chunks))
        call = LoggedCall(identity={}, endpoint="/v1/images/generations", model="gpt-image-2", summary="s")
        with patch.object(LoggedCall, "log"):
            forwarded = list(call.stream(iter(chunks)))
        self.assertEqual(len(forwarded), 1)
        self.assertNotIn("_log_only", forwarded[0])

    def test_all_fail_pool_error_keeps_attempts(self) -> None:
        def fake_single(request, index, total):
            exc = conv.ImageGenerationError("boom")
            exc.image_attempts = [_attempt(index, 1)]
            raise exc

        request = conv.ConversationRequest(prompt="p", model="gpt-image-2", n=2)
        with patch.object(conv, "_generate_single_image", side_effect=fake_single):
            with self.assertRaises(conv.ImageGenerationError) as ctx:
                list(conv.stream_image_outputs_with_pool(request))
        self.assertEqual(sorted(a["slot"] for a in log_service.collect_image_attempts(ctx.exception)), [1, 2])


if __name__ == "__main__":
    unittest.main()
