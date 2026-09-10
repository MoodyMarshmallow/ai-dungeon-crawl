import asyncio
from dataclasses import replace
import json
import shutil
import sys
import unittest

import httpx2
from openai import AsyncOpenAI
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel, DeltaToolCall
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from ai_dungeon_crawl.contracts import GameAction, ExecutionResult, AgentTurn, GameObservation, GameStep, AgentTurnRecord
from ai_dungeon_crawl.agent.policies import CodexPolicy, PydanticPolicy, create_policy, SHELL_DESCRIPTION, INSTRUCTIONS
from ai_dungeon_crawl.game.mock_game import MockGameSession
from ai_dungeon_crawl.episode import EpisodeRunner
from ai_dungeon_crawl.game.observation_json import observation_data


OBSERVATION = GameObservation(0, "######\n#@...#\n######")


def response(code='await press("l")'):
    return ModelResponse(parts=[ToolCallPart("execute_shell", {"code": code})])


class PydanticPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_output_tool_preserves_timeout(self):
        def model(messages, info):
            schema = info.output_tools[0].parameters_json_schema["properties"]["timeout_ms"]
            self.assertIn('180000', json.dumps(schema))
            return ModelResponse(parts=[ToolCallPart("execute_shell", {"code": ":", "timeout_ms": 120000})])
        turn = await PydanticPolicy(FunctionModel(model)).request_turn(OBSERVATION, ())
        self.assertEqual(turn.timeout_ms, 120000)

    def test_explicit_reasoning_rejects_other_providers(self):
        with self.assertRaisesRegex(ValueError, "requires a Codex or OpenAI"):
            create_policy("pydantic", model="anthropic:example", reasoning_effort="high")
        self.assertEqual(create_policy("pydantic", model="anthropic:example").reasoning_effort, "default")
        self.assertEqual(create_policy("pydantic", model="openai:example", reasoning_effort="high").reasoning_effort, "high")

    async def test_returns_code_without_executing_it_and_counts_requests(self):
        calls = []

        def model(messages, info):
            calls.append(messages)
            self.assertEqual(info.function_tools, [])
            self.assertEqual([tool.name for tool in info.output_tools], ["execute_shell"])
            self.assertTrue(info.output_tools[0].description.startswith(SHELL_DESCRIPTION))
            self.assertLess(len(INSTRUCTIONS.split()), 40)
            self.assertNotIn('style_runs', INSTRUCTIONS)
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

        after = GameObservation(1, "next screen")
        record = AgentTurnRecord(0, AgentTurn("pass"), ExecutionResult(after, "feedback"),
                            (GameStep(OBSERVATION, GameAction("l"), after),))
        history = (record, replace(record, id=1))
        policy = PydanticPolicy(FunctionModel(model), history_turns=1)
        await policy.request_turn(after, history)
        await policy.request_turn(OBSERVATION, ())
        self.assertEqual(prompts[0]["history"][0]["id"], 1)
        self.assertEqual(prompts[0]["history"][0]["execution"]["output"], "feedback")
        self.assertEqual(prompts[0]["history"][0]["executed_keys"], ["l"])
        self.assertEqual(prompts[0]["omitted_turns"], 1)
        self.assertEqual(prompts[1]["history"], [])
        self.assertEqual(prompts[0]["observation"], observation_data(after))
        self.assertNotIn('observation', prompts[0]['history'][0]['execution'])
        self.assertEqual(prompts[1]["observation"], observation_data(OBSERVATION))

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
            self.assertEqual(body["tools"][0]["function"]["name"], "execute_shell")
            self.assertEqual(len(body["tools"]), 1)
            return httpx2.Response(200, json={
                "id": "offline", "object": "chat.completion", "created": 0,
                "model": "offline-test", "usage": {
                    "prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18,
                },
                "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": "call_1", "type": "function", "function": {
                            "name": "execute_shell", "arguments": json.dumps({"code": "pass"})
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


    async def test_plain_text_is_not_executable_output(self):
        calls = []

        def model(messages, info):
            calls.append(messages)
            return ModelResponse(parts=[TextPart('{"code":"await press(\"l\")"}')])

        with self.assertRaises(UnexpectedModelBehavior):
            await PydanticPolicy(FunctionModel(model)).request_turn(OBSERVATION, ())
        self.assertEqual(len(calls), 2)

    async def test_multiple_output_calls_are_rejected(self):
        def model(messages, info):
            return ModelResponse(parts=[ToolCallPart("execute_shell", {"code": "pass"}, "a"),
                                        ToolCallPart("execute_shell", {"code": "pass"}, "b")])

        with self.assertRaisesRegex(ValueError, "exactly one"):
            await PydanticPolicy(FunctionModel(model)).request_turn(OBSERVATION, ())

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("sandbox-exec"),
                         "REPL integration requires macOS sandbox-exec")
    async def test_output_tool_ends_turn_and_feedback_starts_next_turn(self):
        prompts = []

        def model(messages, info):
            user_parts = [part for message in messages for part in message.parts
                          if isinstance(part, UserPromptPart)]
            self.assertEqual(len(user_parts), 1)
            prompts.append(json.loads(user_parts[0].content))
            return response('crawl press l\ncrawl press l' if len(prompts) == 1
                            else 'crawl press l')

        async def stream(messages, info):
            call = model(messages, info).parts[0]
            yield {0: DeltaToolCall(name=call.tool_name, json_args=json.dumps(call.args))}

        result = await EpisodeRunner(MockGameSession(), PydanticPolicy(FunctionModel(stream_function=stream))).run()
        self.assertEqual(result.stop_reason, "game_exited")
        self.assertEqual([len(turn.steps) for turn in result.turns], [2, 1])
        self.assertEqual(len(prompts), 2)
        self.assertEqual(prompts[1]["observation"]["id"], 2)
        self.assertEqual(prompts[1]["history"][0]["executed_keys"], ["l", "l"])


class RoutingTests(unittest.TestCase):
    def test_explicit_backend_selection(self):
        self.assertIsInstance(create_policy("codex", model="test-model"), CodexPolicy)
        self.assertIsInstance(create_policy("pydantic", model="openai-chat:example"), PydanticPolicy)
        with self.assertRaises(ValueError):
            create_policy("scripted")

    def test_bad_configuration_never_selects_another_backend(self):
        with self.assertRaises(ValueError):
            create_policy("unknown")
        with self.assertRaises(ValueError):
            create_policy("pydantic")
        with self.assertRaises(ValueError):
            create_policy("codex")
        with self.assertRaises(ValueError):
            create_policy("scripted", model="example")


if __name__ == "__main__":
    unittest.main()
