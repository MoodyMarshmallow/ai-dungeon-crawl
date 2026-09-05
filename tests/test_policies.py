import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from openai import AsyncOpenAI
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from ai_dungeon_crawl.contracts import Action, ExecutionResult, ModelTurn, Observation, Step, TurnRecord
from ai_dungeon_crawl.policies import CodexPolicy, PydanticPolicy, create_policy


OBSERVATION = Observation(0, "######\n#@...#\n######")


def response(code='await press("l")'):
    return ModelResponse(parts=[ToolCallPart("execute_python", {"code": code})])


class PydanticPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_code_without_executing_it_and_counts_requests(self):
        calls = []

        def model(messages, info):
            calls.append(messages)
            self.assertEqual(info.function_tools, [])
            self.assertEqual([tool.name for tool in info.output_tools], ["execute_python"])
            return response('raise RuntimeError("must not execute inside policy")')

        policy = PydanticPolicy(FunctionModel(model))
        turn = await policy.request_turn(OBSERVATION, ())
        self.assertIn("must not execute", turn.code)
        self.assertEqual(turn.model_requests, 1)
        self.assertEqual(len(calls), 1)

    async def test_invalid_output_gets_one_bounded_repair(self):
        calls = 0

        def model(messages, info):
            nonlocal calls
            calls += 1
            return response(123 if calls == 1 else "pass")

        turn = await PydanticPolicy(FunctionModel(model)).request_turn(OBSERVATION, ())
        self.assertEqual(turn.code, "pass")
        self.assertEqual(turn.model_requests, 2)

    async def test_repair_budget_is_finite(self):
        calls = 0

        def model(messages, info):
            nonlocal calls
            calls += 1
            return response(123)

        with self.assertRaises(UnexpectedModelBehavior):
            await PydanticPolicy(FunctionModel(model), max_requests=2).request_turn(OBSERVATION, ())
        self.assertEqual(calls, 2)

    async def test_model_timeout_cancels_request(self):
        cancelled = asyncio.Event()

        async def model(messages, info):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with self.assertRaises(TimeoutError):
            await PydanticPolicy(FunctionModel(model), timeout_seconds=0.05).request_turn(OBSERVATION, ())
        self.assertTrue(cancelled.is_set())

    async def test_explicit_bounded_history_and_no_hidden_history(self):
        prompts = []

        def model(messages, info):
            user_parts = [part for message in messages for part in message.parts
                          if isinstance(part, UserPromptPart)]
            self.assertEqual(len(user_parts), 1)
            prompts.append(json.loads(user_parts[0].content))
            return response("pass")

        after = Observation(1, "next screen")
        record = TurnRecord(0, ModelTurn("pass"), ExecutionResult(after, "feedback"),
                            (Step(OBSERVATION, Action("l"), after),))
        history = (record, replace(record, id=1))
        policy = PydanticPolicy(FunctionModel(model), history_turns=1)
        await policy.request_turn(after, history)
        await policy.request_turn(OBSERVATION, ())
        self.assertEqual(prompts[0]["history"][0]["id"], 1)
        self.assertEqual(prompts[0]["history"][0]["execution"]["output"], "feedback")
        self.assertEqual(prompts[0]["history"][0]["executed_keys"], ["l"])
        self.assertEqual(prompts[0]["omitted_turns"], 1)
        self.assertEqual(prompts[1]["history"], [])

    async def test_large_current_screen_is_rejected_not_truncated(self):
        def model(messages, info):
            self.fail("Oversized context must fail before contacting the model")

        with self.assertRaisesRegex(ValueError, "context budget"):
            await PydanticPolicy(FunctionModel(model), max_context_chars=100).request_turn(OBSERVATION, ())

    async def test_real_openai_client_wire_format_without_network(self):
        requests = []

        def handle(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertEqual(request.url.path, "/v1/chat/completions")
            self.assertEqual(body["tools"][0]["function"]["name"], "execute_python")
            self.assertEqual(len(body["tools"]), 1)
            return httpx2.Response(200, json={
                "id": "offline", "object": "chat.completion", "created": 0,
                "model": "offline-test", "usage": {
                    "prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18,
                },
                "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": "call_1", "type": "function", "function": {
                            "name": "execute_python", "arguments": json.dumps({"code": "pass"})
                        },
                    }],
                }}],
            })

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http:
            async with AsyncOpenAI(api_key="fake-test-key", base_url="https://example.test/v1",
                                   http_client=http, max_retries=0) as client:
                model = OpenAIChatModel("offline-test", provider=OpenAIProvider(openai_client=client))
                turn = await PydanticPolicy(model).request_turn(OBSERVATION, ())
        self.assertEqual(turn.code, "pass")
        self.assertEqual(turn.model_requests, 1)
        self.assertEqual(len(requests), 1)


class CodexPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.executable = Path(directory.name) / "codex-stub"
        shutil.copyfile(Path(__file__).parent / "fixtures" / "codex_stub.py", self.executable)
        self.executable.chmod(0o700)

    def policy(self, mode="ok", **kwargs):
        return CodexPolicy(executable=str(self.executable), goal=mode, **kwargs)

    async def test_structured_output_with_subscription_only_configuration(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake", "CODEX_API_KEY": "fake",
                                     "OPENAI_BASE_URL": "https://example.test", "CODEX_ACCESS_TOKEN": "fake"}):
            turn = await self.policy().request_turn(OBSERVATION, ())
        self.assertEqual(turn.code, 'await press("l")')
        self.assertIsNone(turn.model_requests)

    async def test_invalid_structured_output_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.policy("invalid").request_turn(OBSERVATION, ())

    async def test_failure_does_not_leak_diagnostics_or_fall_back(self):
        with self.assertRaisesRegex(RuntimeError, "no API fallback") as context:
            await self.policy("failed").request_turn(OBSERVATION, ())
        self.assertNotIn("fake-sensitive-diagnostic", str(context.exception))

    async def test_incomplete_turn_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "did not complete"):
            await self.policy("incomplete").request_turn(OBSERVATION, ())

    async def test_unexpected_tool_use_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "unexpected tool"):
            await self.policy("tool").request_turn(OBSERVATION, ())

    async def test_timeout_kills_process(self):
        processes = []
        original = asyncio.create_subprocess_exec

        async def create(*args, **kwargs):
            process = await original(*args, **kwargs)
            processes.append(process)
            return process

        with patch("asyncio.create_subprocess_exec", create):
            with self.assertRaises(TimeoutError):
                await self.policy("timeout", timeout_seconds=0.1).request_turn(OBSERVATION, ())
        self.assertIsNotNone(processes[0].returncode)

    async def test_output_overflow_is_bounded(self):
        with self.assertRaisesRegex(RuntimeError, "capture limit"):
            await self.policy("overflow").request_turn(OBSERVATION, ())

    async def test_missing_cli_is_actionable(self):
        with self.assertRaisesRegex(RuntimeError, "Codex CLI is missing"):
            await CodexPolicy(executable="/nonexistent/codex").request_turn(OBSERVATION, ())


class RoutingTests(unittest.TestCase):
    def test_explicit_backend_selection(self):
        self.assertIsInstance(create_policy("codex"), CodexPolicy)
        self.assertIsInstance(create_policy("pydantic", model="openai-chat:example"), PydanticPolicy)
        self.assertEqual(type(create_policy("scripted")).__name__, "ScriptedPolicy")

    def test_bad_configuration_never_selects_another_backend(self):
        with self.assertRaises(ValueError):
            create_policy("unknown")
        with self.assertRaises(ValueError):
            create_policy("pydantic")
        with self.assertRaises(ValueError):
            create_policy("scripted", model="example")


if __name__ == "__main__":
    unittest.main()
