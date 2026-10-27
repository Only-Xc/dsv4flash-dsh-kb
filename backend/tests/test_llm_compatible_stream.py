"""Transport contract tests, not substitutes for real provider acceptance."""
import json
import unittest
from unittest.mock import patch

import httpx

from app.config import Settings
from app.llm import LLMAdapter


class CompatibleLLMTests(unittest.IsolatedAsyncioTestCase):
    async def test_truncated_or_unterminated_stream_is_not_complete(self):
        for finish, done in (("length", True), ("content_filter", True), (None, False)):
            with self.subTest(finish=finish, done=done):
                frames = [{"choices": [{"delta": {"content": "Unfinished answer"}, "finish_reason": finish}]}]
                body = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
                if done:
                    body += "data: [DONE]\n\n"
                with self.client_patch(lambda request: httpx.Response(200, text=body)):
                    with self.assertRaises(RuntimeError):
                        _ = [text async for text in self.adapter().stream([])]

    async def test_malformed_stream_frame_is_not_silently_discarded(self):
        body = 'data: {broken}\n\ndata: [DONE]\n\n'
        with self.client_patch(lambda request: httpx.Response(200, text=body)):
            with self.assertRaises(RuntimeError):
                _ = [text async for text in self.adapter().stream([])]

    def adapter(self):
        return LLMAdapter(Settings(_env_file=None, llm_base_url="https://provider.test/v1",
                                   llm_api_key="test-only-key", llm_model="configured-model"))

    def client_patch(self, handler):
        client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        return patch("app.llm.httpx.AsyncClient", side_effect=lambda **kw: client(transport=transport, **kw))

    async def test_bilingual_stream_preserves_reasoning_and_ignores_usage(self):
        for text in ("Verified answer.\n\nNext step.", "إجابة موثوقة.\n\nالخطوة التالية."):
            with self.subTest(text=text):
                frames = [
                    {"choices": [{"delta": {"role": "assistant"}}]},
                    {"choices": [{"delta": {"reasoning_content": "private reasoning"}}]},
                    {"choices": [{"delta": {"content": text}}]},
                    {"choices": [{"delta": None, "finish_reason": "stop"}]},
                    {"choices": [], "usage": {"total_tokens": 10}},
                ]
                def handler(request):
                    payload = json.loads(request.content)
                    self.assertEqual(payload["model"], "configured-model")
                    self.assertTrue(payload["stream"])
                    body = "".join("data: " + json.dumps(f) + "\n\n" for f in frames)
                    return httpx.Response(200, text=body + "data: [DONE]\n\n")
                reasoning = []
                async def capture(value):
                    reasoning.append(value)
                with self.client_patch(handler):
                    answer = "".join([c async for c in self.adapter().stream(
                        [{"role": "user", "content": "test"}], on_reasoning=capture)])
                self.assertEqual(answer, text)
                self.assertEqual(reasoning, ["private reasoning"])

    async def test_missing_and_null_choices_are_not_answer_text(self):
        def handler(request):
            return httpx.Response(200, text='data: {"choices":null}\n\ndata: {"usage":{}}\n\ndata: [DONE]\n\n')
        with self.client_patch(handler):
            self.assertEqual([c async for c in self.adapter().stream([])], [])

    async def test_provider_error_is_not_swallowed_or_exposed_as_answer(self):
        def handler(request):
            return httpx.Response(200, text='data: {"error":{"message":"private provider error"}}\n\n')
        with self.client_patch(handler):
            with self.assertRaisesRegex(RuntimeError, "LLM provider returned a stream error") as caught:
                _ = [c async for c in self.adapter().stream([])]
        self.assertNotIn("private provider error", str(caught.exception))

    async def test_http_authentication_failure_propagates(self):
        with self.client_patch(lambda request: httpx.Response(401, json={"error": "invalid_key"})):
            with self.assertRaises(httpx.HTTPStatusError) as caught:
                _ = [c async for c in self.adapter().stream([])]
        self.assertEqual(caught.exception.response.status_code, 401)

    async def test_json_router_uses_same_configured_provider(self):
        expected = {"skillId": "general_knowledge", "confidence": 1}
        def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "configured-model")
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(expected)}}]})
        with self.client_patch(handler):
            result = await self.adapter().route_skill("test", [{"skillId": "general_knowledge"}])
        self.assertEqual(result["skillId"], expected["skillId"])

    async def test_empty_router_choices_fail_cleanly(self):
        with self.client_patch(lambda request: httpx.Response(200, json={"choices": []})):
            with self.assertRaisesRegex(ValueError, "empty output"):
                await self.adapter().route_skill("test", [])
