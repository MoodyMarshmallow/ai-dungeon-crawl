import asyncio
import json
import shutil
import sys
import unittest

from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.models.function import FunctionModel, DeltaToolCall, DeltaThinkingPart

from ai_dungeon_crawl.contracts import GameObservation
from ai_dungeon_crawl.mock_game import MockGameSession
from ai_dungeon_crawl.episode import EpisodeRunner
from ai_dungeon_crawl.events import observe_events
from ai_dungeon_crawl.policies import PydanticPolicy


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_arrives_before_acceptance_and_excludes_signatures(self):
        release, preview = asyncio.Event(), asyncio.Event()
        seen = []

        def record(event, data):
            seen.append((event, data))
            if event == "model.part" and data.get("kind") == "tool":
                preview.set()

        async def stream(messages, info):
            self.assertFalse(info.allow_text_output)
            yield {0: DeltaThinkingPart(content="Check corridor.", signature="private-signature")}
            yield {1: DeltaToolCall(name="execute_python", json_args='{"code":"pa')}
            await release.wait()
            yield {1: DeltaToolCall(json_args='ss"}')}

        with observe_events(record):
            task = asyncio.create_task(PydanticPolicy(FunctionModel(stream_function=stream)).request_turn(
                GameObservation(0, "screen"), ()))
        try:
            await asyncio.wait_for(preview.wait(), 5)
            self.assertFalse(task.done())
            self.assertNotIn("private-signature", json.dumps(seen))
            release.set()
            turn = await task
        finally:
            release.set()
            if not task.done():
                task.cancel()
        self.assertEqual(turn.code, "pass")
        self.assertEqual(turn.model_requests, 1)
        self.assertEqual([event for event, _ in seen].count("model.started"), 1)
        self.assertEqual(seen[-1][0], "model.finished")

    async def test_streamed_plain_text_is_still_rejected(self):
        seen = []

        async def stream(messages, info):
            yield '{"code":"pass"}'

        with observe_events(lambda event, data: seen.append(event)):
            with self.assertRaises(UnexpectedModelBehavior):
                await PydanticPolicy(FunctionModel(stream_function=stream)).request_turn(GameObservation(0, "x"), ())
        self.assertEqual(seen.count("model.started"), 2)

    async def test_multiple_streamed_calls_are_still_rejected(self):
        async def stream(messages, info):
            yield {0: DeltaToolCall(name="execute_python", json_args='{"code":"pass"}', tool_call_id="a"),
                   1: DeltaToolCall(name="execute_python", json_args='{"code":"pass"}', tool_call_id="b")}
        with observe_events(lambda *_: None):
            with self.assertRaisesRegex(ValueError, "exactly one"):
                await PydanticPolicy(FunctionModel(stream_function=stream)).request_turn(GameObservation(0, "x"), ())

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("sandbox-exec"), "macOS sandbox required")
    async def test_real_repl_game_and_output_events_precede_completion(self):
        seen = []
        with observe_events(lambda event, data: seen.append((event, data))):
            async def stream(messages, info):
                yield {0: DeltaToolCall(name="execute_python", json_args=json.dumps({
                    "code": 'await press("l")\nprint("moved")'
                }), tool_call_id="step")}
            result = await EpisodeRunner(MockGameSession(), PydanticPolicy(FunctionModel(stream_function=stream))).run()
        kinds = [event for event, _ in seen]
        self.assertEqual(result.stop_reason, "game_exited")
        self.assertEqual(kinds.count("game.step"), 3)
        self.assertEqual(kinds.count("repl.submitted"), 3)
        self.assertLess(kinds.index("repl.submitted"), kinds.index("game.step"))
        self.assertLess(kinds.index("repl.output"), kinds.index("repl.finished"))
