from ai_dungeon_crawl.config import AgentConfig, SessionConfig
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pydantic_ai.messages import PartDeltaEvent, PartStartEvent, ToolCallPart, ToolCallPartDelta
from pydantic_ai.models.function import FunctionModel, DeltaThinkingPart, DeltaToolCall

from ai_dungeon_crawl.cli import run_episode, new_run_directory
from ai_dungeon_crawl.events import emit, events_enabled, observe_events, record_events
from ai_dungeon_crawl.agent.policies import PydanticPolicy
from ai_dungeon_crawl.contracts import Policy
from ai_dungeon_crawl.run_log import episode_log
from helpers import TestGameSession


class RunLogTests(unittest.TestCase):
    def test_game_ticks_and_wall_clock_are_separate_in_both_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with episode_log(directory):
                emit("episode.started")
                emit("game.score", game_time=0)
                emit("model.started")
                emit("game.score", game_time=17)
                emit("game.tiles", messages=[])
                emit("execution.output", text="done")
                emit("game.score", game_time=None)
                emit("episode.finished")
            review = [json.loads(line) for line in (directory / "model.jsonl").read_text().splitlines()]
            tiles = [json.loads(line) for line in (directory / "tiles.jsonl").read_text().splitlines()]
            game = [json.loads(line) for line in (directory / "game.jsonl").read_text().splitlines()]
            self.assertEqual([row["timestamp"] for row in review], [None, 0, 17, 17])
            self.assertEqual([row["timestamp"] for row in game], [0, 17, 17])
            self.assertEqual(tiles[0]["timestamp"], 17)
            for row in review + game + tiles:
                self.assertEqual(row["schema_version"], 2)
                self.assertEqual(datetime.fromisoformat(row["real_timestamp"]).utcoffset().total_seconds(), 0)

    def test_tiles_are_separate_with_shared_sequence_and_context(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            display = []
            with episode_log(directory), observe_events(lambda event, data: display.append(event)):
                emit("turn.started", id=3)
                emit("model.started")
                emit("game.tiles", messages=[{"msg": "map", "cells": []}])
                emit("execution.submitted", code=":")
                emit("game.score", score=42, game_turn=10, final=False)
                review = [json.loads(line) for line in (directory / "model.jsonl").read_text().splitlines()]
                tiles = [json.loads(line) for line in (directory / "tiles.jsonl").read_text().splitlines()]
                game = [json.loads(line) for line in (directory / "game.jsonl").read_text().splitlines()]
                self.assertEqual([row["sequence"] for row in review], [0, 1, 3])
                self.assertEqual(game[-1]["event"], "game.score")
                self.assertEqual(game[-1]["data"]["score"], 42)
                self.assertEqual(game[-1]["sequence"], 4)
                self.assertFalse(any(row["event"].startswith("game.") for row in review))
                self.assertTrue(all(row["event"].startswith("game.") for row in game))
                self.assertEqual(stat.S_IMODE((directory / "game.jsonl").stat().st_mode), 0o600)
                self.assertEqual([row["sequence"] for row in tiles], [2])
                self.assertNotIn("game.tiles", [row["event"] for row in review])
                self.assertEqual(tiles[0]["turn_id"], 3)
                self.assertEqual(tiles[0]["model_request_id"], 1)
                self.assertEqual(tiles[0]["data"]["messages"], [{"msg": "map", "cells": []}])
                self.assertIn("game.tiles", display)
                self.assertEqual(stat.S_IMODE((directory / "tiles.jsonl").stat().st_mode), 0o600)

    def test_existing_tiles_log_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            tiles = directory / "tiles.jsonl"
            tiles.write_text("existing replay\n")
            with self.assertRaises(FileExistsError), episode_log(directory):
                pass
            self.assertEqual(tiles.read_text(), "existing replay\n")
            self.assertFalse((directory / "model.jsonl").exists())

    def test_run_names_sort_by_utc_time_and_remain_unique(self):
        with patch("ai_dungeon_crawl.cli.datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 9, 14, 30, 0, 123456, tzinfo=timezone.utc)
            first, same_time = new_run_directory(), new_run_directory()
            clock.now.assert_called_with(timezone.utc)
            clock.now.return_value = datetime(2026, 9, 10, tzinfo=timezone.utc)
            later = new_run_directory()
        self.assertRegex(first.name, r"^2026-09-09T14-30-00\.123456Z_[0-9a-f]{32}$")
        self.assertNotEqual(first, same_time)
        self.assertLess(first.name, later.name)
        self.assertEqual(first.parent.name, "runs")

    def test_existing_trajectory_or_legacy_log_prevents_new_files(self):
        for name in ('model.jsonl', 'game.jsonl', 'tiles.jsonl', 'events.jsonl'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                (directory / name).write_text('existing\n')
                with self.assertRaises(FileExistsError), episode_log(directory):
                    pass
                self.assertEqual([file.name for file in directory.iterdir()], [name])
                self.assertEqual((directory / name).read_text(), 'existing\n')

    def test_flush_correlation_permissions_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "one"
            with episode_log(directory) as path:
                emit("turn.started", id=2)
                emit("model.started")
                emit("model.part", kind="reasoning", index=0, text="Exposed summary", replace=True)
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                self.assertEqual([row["event"] for row in rows], ["turn.started", "model.started"])
                emit("model.finished", finish_reason="stop")
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                self.assertEqual([row["sequence"] for row in rows], [0, 1, 2, 3])
                message = rows[2]
                self.assertEqual(message["event"], "model.message")
                self.assertEqual(message["data"], {"index": 0, "kind": "reasoning",
                                                   "text": "Exposed summary", "completed": True})
                self.assertEqual(message["turn_id"], 2)
                self.assertEqual(message["model_request_id"], 1)
                self.assertEqual(message["run_id"], "one")
                self.assertTrue(message["real_timestamp"].endswith("+00:00"))
                self.assertEqual(rows[3]["event"], "model.finished")
                self.assertNotIn("model.part", [row["event"] for row in rows])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError), episode_log(directory):
                pass
            self.assertEqual(len(path.read_text().splitlines()), 4)

    def test_recording_enables_streaming_and_failures_are_not_silenced(self):
        def fail(*_):
            raise OSError("disk unavailable")
        with record_events(fail):
            self.assertTrue(events_enabled())
            with self.assertRaises(OSError):
                emit("model.started")

    def test_failed_model_batch_is_not_replayed_during_close(self):
        original_dumps = json.dumps
        message_writes = 0

        def dumps(value, *args, **kwargs):
            nonlocal message_writes
            if value.get("event") == "model.message":
                message_writes += 1
                if message_writes == 2:
                    raise OSError("disk unavailable")
            return original_dumps(value, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with patch("ai_dungeon_crawl.run_log.json.dumps", side_effect=dumps):
                with self.assertRaises(OSError):
                    with episode_log(directory):
                        emit("model.started")
                        emit("model.part", index=0, kind="text", text="one", replace=True)
                        emit("model.part", index=1, kind="text", text="two", replace=True)
                        emit("model.finished")
            rows = [json.loads(line) for line in (directory / "model.jsonl").read_text().splitlines()]
            self.assertEqual([row["event"] for row in rows], ["model.started", "model.message"])
            self.assertEqual(message_writes, 2)


class LoggedEpisodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_reasoning_tools_and_full_output_survive_preview_limits(self):
        code = "python -c 'import sys; print(\"x\" * 40000); print(\"stderr marker\", file=sys.stderr)'\ncrawl press l\ncrawl press l\ncrawl press l"
        async def stream(messages, info):
            yield {0: DeltaThinkingPart(content="A public summary. ", signature="private-signature")}
            yield {0: DeltaThinkingPart(content="More detail.")}
            yield {1: DeltaToolCall(name="execute_shell", json_args=json.dumps({"code": code}))}
        policy = PydanticPolicy(AgentConfig(backend="pydantic", model="test:model"), model=FunctionModel(stream_function=stream))
        display = []
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "run"
            with patch("ai_dungeon_crawl.cli.create_policy", return_value=policy), \
                    patch("ai_dungeon_crawl.cli.create_game", return_value=TestGameSession()):
                result = await run_episode(SessionConfig(action_agent=AgentConfig(model="test-model"), action_turn_limit=1),
                    run_dir=directory, sink=lambda event, data: display.append((event, data)))
            raw = (directory / "model.jsonl").read_text()
            rows = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual(rows[0]["event"], "episode.started")
            self.assertEqual(rows[-1]["data"]["stop_reason"], "game_exited")
            messages = [row["data"] for row in rows if row["event"] == "model.message"]
            tool_calls = [row["data"] for row in rows if row["event"] == "model.tool_call"]
            self.assertEqual("".join(part["text"] for part in messages if part["kind"] == "reasoning"),
                             "A public summary. More detail.")
            self.assertEqual(len(tool_calls), 1)
            self.assertEqual(tool_calls[0]["name"], "execute_shell")
            self.assertTrue(tool_calls[0]["completed"])
            self.assertNotIn("model.part", [row["event"] for row in rows])
            self.assertNotIn("private-signature", raw)
            output = [row["data"] for row in rows if row["event"] == "execution.output"]
            self.assertEqual("".join(item["text"] for item in output if item["stream"] == "stdout"), "x" * 40000 + "\n")
            self.assertEqual("".join(item["text"] for item in output if item["stream"] == "stderr"), "stderr marker\n")
            self.assertEqual(len(result.turns[0].execution.output), 32768)
            self.assertTrue(result.turns[0].execution.output_truncated)
            self.assertEqual(sum(len(data["text"]) for event, data in display if event == "execution.output"), 32768)
            game = [json.loads(line) for line in (directory / "game.jsonl").read_text().splitlines()]
            self.assertEqual(sum(row["event"] == "game.step" for row in game), 3)
            self.assertFalse(any(row["event"].startswith("game.") for row in rows))

    async def test_setup_failure_is_logged_without_exception_body(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with patch("ai_dungeon_crawl.cli.create_policy", side_effect=ValueError("secret-token")), \
                    patch("ai_dungeon_crawl.cli.create_game", return_value=TestGameSession()):
                with self.assertRaises(ValueError):
                    await run_episode(SessionConfig(action_agent=AgentConfig(model="test"), action_turn_limit=1), run_dir=directory)
            raw = (directory / "model.jsonl").read_text()
            self.assertNotIn("secret-token", raw)
            self.assertEqual(json.loads(raw.splitlines()[-1])["data"], {"status": "error", "error": "ValueError"})

    async def test_cancellation_retains_partial_model_activity(self):
        started = asyncio.Event()
        class WaitingPolicy(Policy):
            async def request_turn(self, *_):
                emit("model.started")
                emit("model.part", index=0, kind="reasoning", text="Partial", replace=True)
                started.set()
                await asyncio.Event().wait()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with patch("ai_dungeon_crawl.cli.create_policy", return_value=WaitingPolicy()), \
                    patch("ai_dungeon_crawl.cli.create_game", return_value=TestGameSession()):
                task = asyncio.create_task(run_episode(SessionConfig(action_agent=AgentConfig(model="test"), action_turn_limit=1), run_dir=directory))
                await asyncio.wait_for(started.wait(), 5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            rows = [json.loads(line) for line in (directory / "model.jsonl").read_text().splitlines()]
            partial = [row for row in rows if row["event"] == "model.message"]
            self.assertEqual(len(partial), 1)
            self.assertEqual(partial[0]["data"], {"index": 0, "kind": "reasoning",
                                                  "text": "Partial", "completed": False})
            self.assertEqual(rows[-1]["data"], {"status": "stopped", "stop_reason": "cancelled"})

    def test_model_parts_are_aggregated_with_fragmented_tool_and_replace(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            display = []
            with episode_log(directory), observe_events(
                    lambda event, data: display.append((event, data))):
                emit("turn.started", id=7)
                emit("model.started")
                emit("model.part", index=0, kind="text", text="old", replace=True)
                emit("model.part", index=0, kind="text", text=" ignored")
                emit("model.part", index=0, kind="text", text="new", replace=True)
                emit("model.part", index=1, kind="tool", name="exe", text='{"c', replace=True)
                emit("model.part", index=1, kind="tool", name="cute_shell", text='ode":"pass"}')
                self.assertEqual([event for event, _ in display], [
                    "turn.started", "model.started", "model.part", "model.part",
                    "model.part", "model.part", "model.part"])
                emit("model.finished")
                emit("model.started")
                emit("model.part", index=0, kind="reasoning", text="second", replace=True)
                emit("model.finished")
            rows = [json.loads(line) for line in (directory / "model.jsonl").read_text().splitlines()]
            records = [row for row in rows if row["event"] in {"model.message", "model.tool_call"}]
            self.assertEqual([row["event"] for row in records],
                             ["model.message", "model.tool_call", "model.message"])
            self.assertEqual(records[0]["data"], {"index": 0, "kind": "text", "text": "new",
                                                   "completed": True})
            self.assertEqual(records[1]["data"], {"index": 1, "kind": "tool",
                                                   "text": '{"code":"pass"}',
                                                   "name": "execute_shell", "completed": True})
            self.assertEqual(records[2]["data"], {"index": 0, "kind": "reasoning",
                                                   "text": "second", "completed": True})
            self.assertEqual([(row["model_request_id"], row["turn_id"]) for row in records],
                             [(1, 7), (1, 7), (2, 7)])

    async def test_dict_tool_delta_is_persisted_as_merged_json(self):
        events = [
            PartStartEvent(index=0, part=ToolCallPart("execute_shell", {"code": "old"})),
            PartDeltaEvent(index=0, delta=ToolCallPartDelta(
                args_delta={"code": "new", "timeout_ms": 1000})),
        ]

        class Stream:
            def __init__(self):
                self.response = SimpleNamespace(
                    finish_reason="stop", usage=SimpleNamespace(input_tokens=1, output_tokens=2))
            def __aiter__(self):
                return self
            async def __anext__(self):
                if events:
                    return events.pop(0)
                raise StopAsyncIteration
            def get(self):
                return self.response

        class RequestStream:
            async def __aenter__(self):
                self.stream = Stream()
                return self.stream
            async def __aexit__(self, *exc):
                return False

        wrapped = FunctionModel(lambda _: {"code": "pass"})
        wrapped.request_stream = lambda *args, **kwargs: RequestStream()
        from ai_dungeon_crawl.agent.model_stream import ObservedModel
        seen = []
        with episode_log(Path(tempfile.mkdtemp())) as path, observe_events(
                lambda event, data: seen.append((event, data))):
            await ObservedModel(wrapped).request([], {}, SimpleNamespace())
            rows = [json.loads(line) for line in path.read_text().splitlines()]
        tool = next(row for row in rows if row["event"] == "model.tool_call")
        self.assertEqual(json.loads(tool["data"]["text"]), {"code": "new", "timeout_ms": 1000})
        self.assertTrue(tool["data"]["completed"])
        self.assertEqual([event for event, _ in seen], ["model.started", "model.part", "model.part", "model.finished"])


if __name__ == "__main__":
    unittest.main()
