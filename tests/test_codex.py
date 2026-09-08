import json
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from pydantic_ai.exceptions import UnexpectedModelBehavior

from ai_dungeon_crawl.contracts import GameObservation
from ai_dungeon_crawl.policies import CodexPolicy
from ai_dungeon_crawl.events import observe_events


def events(code="pass", complete=True):
    arguments = json.dumps({"code": code})
    item = {"type": "function_call", "id": "fc_1", "call_id": "call_1",
            "name": "execute_python", "arguments": arguments, "status": "completed"}
    response = {"id": "resp_1", "object": "response", "created_at": 1,
                "model": "test-model", "status": "completed", "output": [item],
                "parallel_tool_calls": False, "tool_choice": "required", "tools": [],
                "usage": {"input_tokens": 10, "output_tokens": 8, "total_tokens": 18}}
    result = [
        {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {**item, "arguments": "", "status": "in_progress"}},
        {"type": "response.function_call_arguments.delta", "output_index": 0,
         "item_id": "fc_1", "delta": arguments},
        {"type": "response.function_call_arguments.done", "output_index": 0,
         "item_id": "fc_1", "arguments": arguments},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
    ]
    if complete:
        result.append({"type": "response.completed", "response": response})
    return "".join(f"data: {json.dumps({**event, 'sequence_number': i})}\n\n"
                   for i, event in enumerate(result))


class CodexTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.auth = Path(directory.name) / "auth.json"
        self.auth.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
            "access_token": "fake-oauth-token", "account_id": "fake-account"}}))

    async def request(self, handler, **kwargs):
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        try:
            return await CodexPolicy("test-model", auth_path=self.auth, http_client=client,
                                     **kwargs).request_turn(GameObservation(0, "screen"), ())
        finally:
            await client.aclose()

    async def test_subscription_request_uses_output_tool_not_final_json(self):
        requests = []

        def handle(request):
            requests.append(request)
            self.assertEqual(str(request.url), "https://chatgpt.com/backend-api/codex/responses")
            self.assertEqual(request.headers["authorization"], "Bearer fake-oauth-token")
            self.assertEqual(request.headers["chatgpt-account-id"], "fake-account")
            body = json.loads(request.content)
            self.assertTrue(body["stream"])
            self.assertFalse(body["store"])
            self.assertFalse(body["parallel_tool_calls"])
            self.assertNotIn("max_output_tokens", body)
            self.assertEqual([t["name"] for t in body["tools"]], ["execute_python"])
            self.assertNotIn("format", body.get("text", {}))
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, text=events())

        with patch.dict(os.environ, {"OPENAI_API_KEY": "not-used", "OPENAI_BASE_URL": "https://example.test"}):
            turn = await self.request(handle)
        self.assertEqual(turn.code, "pass")
        self.assertEqual(turn.model_requests, 1)
        self.assertEqual(len(requests), 1)  # No post-tool model continuation.

    async def test_incomplete_stream_never_submits_partial_code(self):
        with self.assertRaisesRegex(UnexpectedModelBehavior, "did not complete"):
            await self.request(lambda _: httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, text=events(complete=False)))

    async def test_invalid_tool_arguments_get_bounded_repair(self):
        count = 0

        def handle(request):
            nonlocal count
            count += 1
            return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                                   text=events(123 if count == 1 else "pass"))

        turn = await self.request(handle)
        self.assertEqual(turn.model_requests, 2)

    async def test_auth_failure_does_not_retry_or_expose_response_body(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx2.Response(401, json={"error": {"message": "fake-secret"}})

        with self.assertRaisesRegex(RuntimeError, "No API fallback") as error:
            await self.request(handle)
        self.assertNotIn("fake-secret", str(error.exception))
        self.assertEqual(len(requests), 1)

    async def test_api_key_login_is_rejected_before_network(self):
        self.auth.write_text('{"auth_mode":"apikey","OPENAI_API_KEY":"fake"}')
        with self.assertRaisesRegex(RuntimeError, "file-backed ChatGPT"):
            await self.request(lambda _: self.fail("must not contact endpoint"))

    async def test_timeout_cancels_transport(self):
        cancelled = asyncio.Event()

        async def handle(request):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with self.assertRaises(TimeoutError):
            await self.request(handle, timeout_seconds=0.05)
        self.assertTrue(cancelled.is_set())

    async def test_missing_login_is_actionable_without_network(self):
        self.auth = self.auth.with_name("missing.json")
        with self.assertRaisesRegex(RuntimeError, "codex login"):
            await self.request(lambda _: self.fail("must not contact endpoint"))

    async def test_observed_codex_uses_same_transport_and_validation(self):
        seen = []
        def handle(request):
            body = json.loads(request.content)
            self.assertTrue(body["stream"])
            self.assertFalse(body["store"])
            self.assertEqual(body["reasoning"]["summary"], "auto")
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, text=events())
        with observe_events(lambda event, data: seen.append((event, data))):
            turn = await self.request(handle, reasoning_summary=True)
        self.assertEqual(turn.code, "pass")
        self.assertTrue(any(event == "model.part" and data["kind"] == "tool" for event, data in seen))

    async def test_observed_codex_incomplete_stream_is_rejected(self):
        with observe_events(lambda *_: None):
            with self.assertRaisesRegex(UnexpectedModelBehavior, "did not complete"):
                await self.request(lambda _: httpx2.Response(
                    200, headers={"content-type": "text/event-stream"}, text=events(complete=False)))
