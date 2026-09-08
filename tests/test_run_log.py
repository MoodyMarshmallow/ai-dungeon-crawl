import asyncio
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from pydantic_ai.models.function import FunctionModel, DeltaThinkingPart, DeltaToolCall

from ai_dungeon_crawl.cli import run_episode
from ai_dungeon_crawl.events import emit, events_enabled, observe_events, record_events
from ai_dungeon_crawl.policies import PydanticPolicy
from ai_dungeon_crawl.run_log import episode_log


class RunLogTests(unittest.TestCase):
    def test_flush_correlation_permissions_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "one"
            with episode_log(directory) as path:
                emit("turn.started", id=2)
                emit("model.started")
                emit("model.part", kind="reasoning", index=0, text="Exposed summary", replace=True)
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                self.assertEqual([row["sequence"] for row in rows], [0, 1, 2])
                self.assertEqual(rows[-1]["turn_id"], 2)
                self.assertEqual(rows[-1]["model_request_id"], 1)
                self.assertEqual(rows[-1]["run_id"], "one")
                self.assertTrue(rows[-1]["timestamp"].endswith("+00:00"))
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError), episode_log(directory):
                pass
            self.assertEqual(len(path.read_text().splitlines()), 3)

    def test_recording_enables_streaming_and_failures_are_not_silenced(self):
        def fail(*_):
            raise OSError("disk unavailable")
        with record_events(fail):
            self.assertTrue(events_enabled())
            with self.assertRaises(OSError):
                emit("model.started")


class LoggedEpisodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_reasoning_tools_and_full_output_survive_preview_limits(self):
        code = "python -c 'import sys; print(\"x\" * 12000); print(\"stderr marker\", file=sys.stderr)'\ncrawl press l\ncrawl press l\ncrawl press l"
        async def stream(messages, info):
            yield {0: DeltaThinkingPart(content="A public summary. ", signature="private-signature")}
            yield {0: DeltaThinkingPart(content="More detail.")}
            yield {1: DeltaToolCall(name="execute_shell", json_args=json.dumps({"code": code}))}
        policy = PydanticPolicy(FunctionModel(stream_function=stream))
        display = []
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "run"
            with patch("ai_dungeon_crawl.cli.create_policy", return_value=policy):
                result = await run_episode("codex", "test-model", 1, game="mock",
                    run_dir=directory, sink=lambda event, data: display.append((event, data)))
            raw = (directory / "events.jsonl").read_text()
            rows = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual(rows[0]["event"], "episode.started")
            self.assertEqual(rows[-1]["data"]["stop_reason"], "game_exited")
            parts = [row["data"] for row in rows if row["event"] == "model.part"]
            self.assertEqual("".join(part["text"] for part in parts if part["kind"] == "reasoning"),
                             "A public summary. More detail.")
            self.assertTrue(any(part["kind"] == "tool" for part in parts))
            self.assertNotIn("private-signature", raw)
            output = [row["data"] for row in rows if row["event"] == "execution.output"]
            self.assertEqual("".join(item["text"] for item in output if item["stream"] == "stdout"), "x" * 12000 + "\n")
            self.assertEqual("".join(item["text"] for item in output if item["stream"] == "stderr"), "stderr marker\n")
            self.assertEqual(len(result.turns[0].execution.output), 8000)
            self.assertTrue(result.turns[0].execution.output_truncated)
            self.assertEqual(sum(len(data["text"]) for event, data in display if event == "execution.output"), 8000)
            self.assertEqual(sum(row["event"] == "game.step" for row in rows), 3)

    async def test_setup_failure_is_logged_without_exception_body(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with patch("ai_dungeon_crawl.cli.create_policy", side_effect=ValueError("secret-token")):
                with self.assertRaises(ValueError):
                    await run_episode("codex", "test", 1, game="mock", run_dir=directory)
            raw = (directory / "events.jsonl").read_text()
            self.assertNotIn("secret-token", raw)
            self.assertEqual(json.loads(raw.splitlines()[-1])["data"], {"status": "error", "error": "ValueError"})

    async def test_cancellation_retains_partial_model_activity(self):
        started = asyncio.Event()
        class WaitingPolicy:
            async def request_turn(self, *_):
                emit("model.started")
                emit("model.part", index=0, kind="reasoning", text="Partial", replace=True)
                started.set()
                await asyncio.Event().wait()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with patch("ai_dungeon_crawl.cli.create_policy", return_value=WaitingPolicy()):
                task = asyncio.create_task(run_episode("codex", "test", 1, game="mock", run_dir=directory))
                await asyncio.wait_for(started.wait(), 5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            rows = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
            self.assertTrue(any(row["event"] == "model.part" for row in rows))
            self.assertEqual(rows[-1]["data"], {"status": "stopped", "stop_reason": "cancelled"})


if __name__ == "__main__":
    unittest.main()
