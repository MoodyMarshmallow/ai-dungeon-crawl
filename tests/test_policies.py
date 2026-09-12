from ai_dungeon_crawl.config import AgentConfig, SessionConfig
import asyncio
import json
import shutil
import sys
import unittest

import httpx2
from openai import AsyncOpenAI
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, RetryPromptPart, TextPart, ThinkingPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel, DeltaToolCall
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from ai_dungeon_crawl.contracts import GameAction, ExecutionResult, AgentTurn, GameObservation, GameStep, AgentTurnRecord
from ai_dungeon_crawl.agent.policies import CodexPolicy, PydanticPolicy, create_policy, SHELL_DESCRIPTION, SYSTEM_PROMPT
from ai_dungeon_crawl.agent.tools.execute_shell import execution_feedback
from helpers import TestGameSession
from ai_dungeon_crawl.episode import EpisodeRunner


OBSERVATION = GameObservation(0, "######\n#@...#\n######")


def response(code='await press("l")'):
    return ModelResponse(parts=[ToolCallPart("execute_shell", {"code": code})])


class PydanticPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_validation_repair_remains_in_subsequent_conversation(self):
        requests = []
        def model(messages, info):
            requests.append(list(messages))
            index = len(requests)
            return ModelResponse(parts=[ToolCallPart('execute_shell',
                {'code': 123 if index == 1 else ':'}, f'call_{index}')])
        policy = PydanticPolicy(AgentConfig(backend="pydantic", model="test:model"), model=FunctionModel(model))
        turn = await policy.request_turn(())
        await policy.request_turn((AgentTurnRecord(0, turn, ExecutionResult(OBSERVATION, 'real'), ()),))
        parts = [p for message in requests[2] for p in message.parts]
        self.assertEqual([p.tool_call_id for p in parts if isinstance(p, ToolCallPart)], ['call_1', 'call_2'])
        self.assertEqual([p.tool_call_id for p in parts if isinstance(p, RetryPromptPart)], ['call_1'])
        returns = [p for p in parts if isinstance(p, ToolReturnPart)]
        self.assertEqual([p.tool_call_id for p in returns], ['call_2'])
        self.assertEqual(json.loads(returns[0].content)['output'], 'real')

    async def test_phase_contexts_are_fresh(self):
        def model(messages, info):
            self.assertFalse(any(isinstance(message, ModelResponse) for message in messages))
            return response(':')
        for profile in ('action', 'review', 'action'):
            await PydanticPolicy(AgentConfig(backend="pydantic", model="test:model"), model=FunctionModel(model), profile=profile).request_turn(())

    async def test_shell_descriptions_prefer_ripgrep_for_both_profiles(self):
        descriptions = {}

        def model(messages, info):
            descriptions[current_profile] = info.output_tools[0].description
            return response(':')

        for current_profile in ('action', 'review'):
            await PydanticPolicy(AgentConfig(backend="pydantic", model="test:model"), model=FunctionModel(model), profile=current_profile).request_turn(
                ())

        for description in descriptions.values():
            self.assertIn('Prefer ripgrep (rg) for searching file contents when available.',
                          description)
        self.assertEqual(set(descriptions), {'action', 'review'})

    async def test_context_overflow_propagates_without_retry_or_history_loss(self):
        fail = False
        calls = []
        def model(messages, info):
            calls.append(list(messages))
            if fail:
                raise RuntimeError('context window exceeded')
            return response(':')
        policy = PydanticPolicy(AgentConfig(backend="pydantic", model="test:model"), model=FunctionModel(model))
        turn = await policy.request_turn(())
        history = (AgentTurnRecord(0, turn, ExecutionResult(OBSERVATION, 'result'), ()),)
        fail = True
        with self.assertRaisesRegex(RuntimeError, 'context window exceeded'):
            await policy.request_turn(history)
        self.assertEqual(len(calls), 2)
        fail = False
        await policy.request_turn(history)
        self.assertEqual([p.content for m in calls[2] for p in m.parts if isinstance(p, ToolReturnPart)],
                         [p.content for m in calls[1] for p in m.parts if isinstance(p, ToolReturnPart)])

    async def test_output_tool_preserves_timeout(self):
        def model(messages, info):
            schema = info.output_tools[0].parameters_json_schema["properties"]["timeout_ms"]
            self.assertIn('180000', json.dumps(schema))
            return ModelResponse(parts=[ToolCallPart("execute_shell", {"code": ":", "timeout_ms": 120000})])
        turn = await PydanticPolicy(AgentConfig(backend="pydantic", model="test:model"), model=FunctionModel(model)).request_turn(())
        self.assertEqual(turn.timeout_ms, 120000)

    def test_explicit_reasoning_rejects_other_providers(self):
        with self.assertRaisesRegex(ValueError, "requires a Codex or OpenAI"):
            create_policy(AgentConfig(backend="pydantic", model="anthropic:example", reasoning_effort="high"))
        self.assertEqual(create_policy(AgentConfig(backend="pydantic", model="anthropic:example")).config.reasoning_effort, "default")
        self.assertEqual(create_policy(AgentConfig(backend="pydantic", model="openai:example", reasoning_effort="high")).config.reasoning_effort, "high")

    async def test_returns_code_without_executing_it_and_counts_requests(self):
        calls = []

        def model(messages, info):
            calls.append(messages)
            self.assertEqual(info.function_tools, [])
            self.assertEqual([tool.name for tool in info.output_tools], ["execute_shell"])
            self.assertTrue(info.output_tools[0].description.startswith(SHELL_DESCRIPTION))
            self.assertLess(len(SYSTEM_PROMPT.split()), 40)
            self.assertNotIn('style_runs', SYSTEM_PROMPT)
            return response('raise RuntimeError("must not execute inside policy")')

        policy = PydanticPolicy(AgentConfig(backend="pydantic", model="test:model"), model=FunctionModel(model))
        turn = await policy.request_turn(())
        self.assertIn("must not execute", turn.code)
        self.assertEqual(turn.model_requests, 1)
        self.assertEqual(len(calls), 1)

    async def test_invalid_output_gets_one_bounded_repair(self):
        calls = 0

        def model(messages, info):
            nonlocal calls
            calls += 1
            return response(123 if calls == 1 else "pass")

        turn = await PydanticPolicy(AgentConfig(backend="pydantic", model="test:model"), model=FunctionModel(model)).request_turn(())
        self.assertEqual(turn.code, "pass")
        self.assertEqual(turn.model_requests, 2)

    async def test_repair_budget_is_finite(self):
        calls = 0

        def model(messages, info):
            nonlocal calls
            calls += 1
            return response(123)

        with self.assertRaises(UnexpectedModelBehavior):
            await PydanticPolicy(AgentConfig(backend="pydantic", model="test:model", max_requests=2), model=FunctionModel(model)).request_turn(())
        self.assertEqual(calls, 2)

    async def test_model_timeout_cancels_request(self):
        cancelled = asyncio.Event()

        async def model(messages, info):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with self.assertRaises(TimeoutError):
            await PydanticPolicy(AgentConfig(backend="pydantic", model="test:model", timeout_seconds=0.05), model=FunctionModel(model)).request_turn(())
        self.assertTrue(cancelled.is_set())

    async def test_full_history_without_automatic_screens(self):
        prompts = []

        def model(messages, info):
            user_parts = [part for message in messages for part in message.parts
                          if isinstance(part, UserPromptPart)]
            self.assertEqual(len(user_parts), 1)
            self.assertEqual(user_parts[0].content, 'Win')
            prompts.append([part for message in messages for part in message.parts
                            if isinstance(part, ToolReturnPart)])
            index = len(prompts) - 1
            self.assertEqual(len(prompts[-1]), index)
            for number, part in enumerate(prompts[-1]):
                self.assertEqual(part.tool_call_id, f'call_{number}')
                self.assertEqual(json.loads(part.content), {
                    'output': 'feedback' * 10000, 'error': 'failure',
                    'status': 'error', 'output_truncated': True})
            prior = [message for message in messages if isinstance(message, ModelResponse)]
            self.assertEqual(len(prior), index)
            for message in prior:
                self.assertEqual(message.parts[0].content, 'assistant text')
                self.assertEqual(message.parts[1].signature, 'opaque-private')
                self.assertEqual(message.provider_details, {'opaque': 'preserved'})
            return ModelResponse(parts=[TextPart('assistant text'),
                ThinkingPart('summary', signature='opaque-private', id='rs_1', provider_name='openai'),
                ToolCallPart('execute_shell', {'code': 'pass'}, f'call_{index}')],
                provider_details={'opaque': 'preserved'})

        after = GameObservation(1, "next screen")
        record = AgentTurnRecord(0, AgentTurn("pass"), ExecutionResult(after, "feedback"),
                            (GameStep(OBSERVATION, GameAction("l"), after),))
        history = ()
        policy = PydanticPolicy(AgentConfig(backend="pydantic", model="test:model"), model=FunctionModel(model), initial_prompt='Win')
        for index in range(7):
            turn = await policy.request_turn(history)
            history += (AgentTurnRecord(index, turn, ExecutionResult(after,
                'feedback' * 10000, 'failure', 'error', True), record.steps),)
        with self.assertRaisesRegex(ValueError, 'out of sync'):
            await policy.request_turn(())

    async def test_only_explicit_observe_output_is_in_context(self):
        before = GameObservation(1, 'UNREQUESTED_OLD_SCREEN')
        current = GameObservation(2, 'UNREQUESTED_CURRENT_SCREEN' * 10000)
        record = AgentTurnRecord(0, AgentTurn('crawl observe'),
                                ExecutionResult(before, 'EXPLICIT_OBSERVE_OUTPUT'), ())
        prompt = execution_feedback(record.execution)
        self.assertIn('EXPLICIT_OBSERVE_OUTPUT', prompt)
        self.assertNotIn('UNREQUESTED', prompt)
        self.assertNotIn('observation', json.loads(prompt))

    async def test_large_initial_prompt_is_sent_not_truncated(self):
        def model(messages, info):
            self.assertEqual(messages[0].parts[0].content, 'x' * 70000)
            return response('pass')

        await PydanticPolicy(AgentConfig(backend="pydantic", model="test:model"), model=FunctionModel(model), initial_prompt='x' * 70000).request_turn(())

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
                turn = await PydanticPolicy(AgentConfig(backend="pydantic", model="openai:test"), model=model).request_turn(())
        self.assertEqual(turn.code, "pass")
        self.assertEqual(turn.model_requests, 1)
        self.assertEqual(len(requests), 1)


    async def test_plain_text_is_not_executable_output(self):
        calls = []

        def model(messages, info):
            calls.append(messages)
            return ModelResponse(parts=[TextPart('{"code":"await press(\"l\")"}')])

        with self.assertRaises(UnexpectedModelBehavior):
            await PydanticPolicy(AgentConfig(backend="pydantic", model="test:model"), model=FunctionModel(model)).request_turn(())
        self.assertEqual(len(calls), 2)

    async def test_multiple_output_calls_are_rejected(self):
        def model(messages, info):
            return ModelResponse(parts=[ToolCallPart("execute_shell", {"code": "pass"}, "a"),
                                        ToolCallPart("execute_shell", {"code": "pass"}, "b")])

        with self.assertRaisesRegex(ValueError, "exactly one"):
            await PydanticPolicy(AgentConfig(backend="pydantic", model="test:model"), model=FunctionModel(model)).request_turn(())

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("sandbox-exec"),
                         "REPL integration requires macOS sandbox-exec")
    async def test_output_tool_ends_turn_and_feedback_starts_next_turn(self):
        prompts = []

        def model(messages, info):
            user_parts = [part for message in messages for part in message.parts
                          if isinstance(part, UserPromptPart)]
            self.assertEqual(len(user_parts), 1)
            prompts.append([part for message in messages for part in message.parts
                            if isinstance(part, ToolReturnPart)])
            return response('crawl press l\ncrawl press l' if len(prompts) == 1
                            else 'crawl press l')

        async def stream(messages, info):
            call = model(messages, info).parts[0]
            yield {0: DeltaToolCall(name=call.tool_name, json_args=json.dumps(call.args))}

        result = await EpisodeRunner(TestGameSession(), PydanticPolicy(AgentConfig(backend="pydantic", model="test:model"), model=FunctionModel(stream_function=stream))).run()
        self.assertEqual(result.stop_reason, "game_exited")
        self.assertEqual([len(turn.steps) for turn in result.turns], [2, 1])
        self.assertEqual(len(prompts), 2)
        self.assertEqual(len(prompts[1]), 1)
        self.assertEqual(json.loads(prompts[1][0].content), {
            'output': '', 'error': None, 'status': 'ok', 'output_truncated': False})


class RoutingTests(unittest.TestCase):
    def test_explicit_backend_selection(self):
        self.assertIsInstance(create_policy(AgentConfig(backend="codex", model="test-model")), CodexPolicy)
        self.assertIsInstance(create_policy(AgentConfig(backend="pydantic", model="openai-chat:example")), PydanticPolicy)
        with self.assertRaises(ValueError):
            create_policy(AgentConfig(backend="scripted", model=""))

    def test_bad_configuration_never_selects_another_backend(self):
        with self.assertRaises(ValueError):
            create_policy(AgentConfig(backend="unknown", model=""))
        with self.assertRaises(ValueError):
            create_policy(AgentConfig(backend="pydantic", model=""))
        with self.assertRaises(ValueError):
            create_policy(AgentConfig(backend="codex", model=""))
        with self.assertRaises(ValueError):
            create_policy(AgentConfig(backend="scripted", model="example"))


if __name__ == "__main__":
    unittest.main()
