import asyncio
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_dungeon_crawl.contracts import GameAction, AgentTurn, GameObservation
from ai_dungeon_crawl.demo import MockGameSession, ScriptedPolicy
from ai_dungeon_crawl.episode import EpisodeRunner
from ai_dungeon_crawl.repl import PythonRepl


class CodePolicy:
    def __init__(self, *scripts):
        self.scripts = scripts
        self.histories = []

    async def request_turn(self, observation, history):
        self.histories.append(history)
        return AgentTurn(self.scripts[len(history) % len(self.scripts)], model_requests=2)


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("sandbox-exec"),
                     "Local REPL integration requires macOS sandbox-exec")
class EpisodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_each_transition_and_closes_on_exit(self):
        game = MockGameSession()
        result = await EpisodeRunner(game, ScriptedPolicy()).run()
        self.assertEqual(result.stop_reason, "game_exited")
        self.assertEqual(len(result.steps), 3)
        self.assertTrue(result.final_observation.ended)
        for step in result.steps:
            self.assertEqual(step.action.key, "l")
        self.assertEqual(result.turns[0].steps, result.steps[:2])
        self.assertEqual(result.turns[1].steps, result.steps[2:])
        self.assertEqual([len(turn.steps) for turn in result.turns], [2, 1])
        self.assertEqual(result.turns[1].execution.output, "Total moves: 3\n")
        self.assertEqual(result.turns[1].turn.model_requests, 0)
        for earlier, later in zip(result.steps, result.steps[1:]):
            self.assertEqual(earlier.after, later.before)
        self.assertTrue(game.closed)

    async def test_zero_budget_observes_without_applying_action(self):
        game = MockGameSession()
        result = await EpisodeRunner(game, ScriptedPolicy()).run(max_steps=0)
        self.assertEqual(result.stop_reason, "step_limit")
        self.assertEqual(result.steps, ())
        self.assertEqual(game.position, 0)
        self.assertTrue(game.closed)

    async def test_policy_failure_closes_without_sending_input(self):
        class FailingPolicy:
            async def request_turn(self, observation, history):
                raise RuntimeError("Model unavailable")

        game = MockGameSession()
        with self.assertRaisesRegex(RuntimeError, "Model unavailable"):
            await EpisodeRunner(game, FailingPolicy()).run()
        self.assertEqual(game.position, 0)
        self.assertTrue(game.closed)

    async def test_game_failure_after_send_is_not_retried(self):
        class TimeoutGame(MockGameSession):
            async def step(self, action):
                self.position += 1
                raise asyncio.TimeoutError("Input sent, next screen unavailable")

        game = TimeoutGame()
        with self.assertRaises(asyncio.TimeoutError):
            await EpisodeRunner(game, ScriptedPolicy()).run()
        self.assertEqual(game.position, 1)
        self.assertTrue(game.closed)

    async def test_limit_is_enforced_inside_script_and_cannot_be_caught(self):
        game = MockGameSession()
        policy = CodePolicy('while True:\n    try:\n        await press("l")\n    except Exception:\n        pass')
        result = await EpisodeRunner(game, policy).run(max_steps=1)
        self.assertEqual(result.stop_reason, "step_limit")
        self.assertEqual(len(result.steps), 1)
        self.assertEqual(result.turns[0].execution.status, "stopped")
        self.assertEqual(game.position, 1)

    async def test_exit_prevents_further_keys_in_same_script(self):
        game = MockGameSession()
        result = await EpisodeRunner(game, CodePolicy('for _ in range(20):\n    await press("l")')).run()
        self.assertEqual(result.stop_reason, "game_exited")
        self.assertEqual(len(result.steps), 3)

    async def test_script_error_preserves_steps_state_and_feedback(self):
        game = MockGameSession()
        policy = CodePolicy(
            'saved = 41\nawait press("l")\nprint("moved")\nraise ValueError("oops")',
            'print(saved + 1)\nawait press("l")\nawait press("l")',
        )
        result = await EpisodeRunner(game, policy).run()
        first = policy.histories[1][0]
        self.assertEqual(first.execution.status, "error")
        self.assertEqual(first.execution.error, "ValueError: oops")
        self.assertEqual(first.execution.output, "moved\n")
        self.assertEqual(len(first.steps), 1)
        self.assertEqual(first.execution.observation.id, 1)
        self.assertEqual(result.turns[1].execution.output, "42\n")
        self.assertEqual(sum(turn.turn.model_requests for turn in result.turns), 4)
        self.assertEqual(result.turns[0].steps, result.steps[:1])
        self.assertEqual(result.turns[1].steps, result.steps[1:])

    async def test_syntax_error_does_not_execute_partial_source(self):
        result = await EpisodeRunner(MockGameSession(), CodePolicy('await press("l")\nif')).run(max_turns=1)
        self.assertEqual(result.steps, ())
        self.assertIn("SyntaxError", result.turns[0].execution.error)

    async def test_no_action_scripts_are_bounded_by_model_turn_limit(self):
        result = await EpisodeRunner(MockGameSession(), CodePolicy('print(observe().id)')).run(max_turns=2)
        self.assertEqual(result.stop_reason, "turn_limit")
        self.assertEqual(len(result.turns), 2)
        self.assertEqual(result.steps, ())

    async def test_zero_turn_budget_never_requests_a_script(self):
        policy = CodePolicy('await press("l")')
        result = await EpisodeRunner(MockGameSession(), policy).run(max_turns=0)
        self.assertEqual(result.stop_reason, "turn_limit")
        self.assertEqual(policy.histories, [])

    async def test_output_limit_is_reported(self):
        result = await EpisodeRunner(MockGameSession(), CodePolicy('print("x" * 1000)'),
                                     PythonRepl(max_output_chars=12)).run(max_turns=1)
        self.assertEqual(result.turns[0].execution.output, "x" * 12)
        self.assertTrue(result.turns[0].execution.output_truncated)

    async def test_timeout_retains_partial_progress_and_closes_worker(self):
        game, repl = MockGameSession(), PythonRepl(timeout_seconds=0.2)
        code = 'await press("l")\nprint("before timeout")\nwhile True:\n    pass'
        result = await EpisodeRunner(game, CodePolicy(code), repl).run()
        self.assertEqual(result.stop_reason, "repl_timeout")
        self.assertEqual(result.turns[0].execution.output, "before timeout\n")
        self.assertEqual(len(result.steps), 1)
        self.assertEqual(result.final_observation.id, 1)
        self.assertTrue(game.closed)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await repl.execute_python("pass", result.final_observation, game.step)

    async def test_invalid_key_becomes_feedback_without_game_input(self):
        result = await EpisodeRunner(MockGameSession(), CodePolicy('await press("ll")')).run(max_turns=1)
        self.assertEqual(result.steps, ())
        self.assertIn("ValueError", result.turns[0].execution.error)

    async def test_new_episode_gets_fresh_namespace(self):
        await EpisodeRunner(MockGameSession(), CodePolicy("saved = 42")).run(max_turns=1)
        result = await EpisodeRunner(MockGameSession(), CodePolicy("print(saved)")).run(max_turns=1)
        self.assertIn("NameError", result.turns[0].execution.error)

    async def test_cancellation_closes_game_and_worker(self):
        started = asyncio.Event()

        class WaitingGame(MockGameSession):
            async def step(self, action):
                started.set()
                await asyncio.Event().wait()

        game, repl = WaitingGame(), PythonRepl()
        task = asyncio.create_task(EpisodeRunner(game, CodePolicy('await press("l")'), repl).run())
        await asyncio.wait_for(started.wait(), timeout=10)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(game.closed)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await repl.execute_python("pass", GameObservation(0, ""), game.step)

    async def test_sandbox_denies_file_read_write_network_and_fork(self):
        # Only artificial data: never probe real user secrets in security tests.
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "sentinel.txt"
            sentinel.write_text("fake secret")
            target = Path(directory) / "must-not-exist.txt"
            code = f'''import os, socket
checks = [
    lambda: open({str(sentinel)!r}).read(),
    lambda: open({str(target)!r}, "w"),
    lambda: socket.create_connection(("127.0.0.1", 9), timeout=1),
    os.fork,
]
for check in checks:
    try:
        check()
    except PermissionError:
        print("denied")
print(os.environ.get("AI_DUNGEON_TEST_SECRET", "absent"))
'''
            with patch.dict("os.environ", {"AI_DUNGEON_TEST_SECRET": "fake credential"}):
                result = await EpisodeRunner(MockGameSession(), CodePolicy(code)).run(max_turns=1)
            execution = result.turns[0].execution
            self.assertIsNone(execution.error)
            self.assertEqual(execution.output, "denied\n" * 4 + "absent\n")
            self.assertFalse(target.exists())


class ContractTests(unittest.IsolatedAsyncioTestCase):
    def test_rejects_macros_and_raw_control_sequences(self):
        for key in ("ll", "\n", "\x1b[A", ""):
            with self.assertRaises(ValueError):
                GameAction(key)
        self.assertEqual(GameAction("CTRL+S").key, "CTRL+S")

    def test_agent_turn_limits_source_and_usage(self):
        with self.assertRaises(ValueError):
            AgentTurn("pass", model_requests=-1)
        with self.assertRaises(ValueError):
            AgentTurn("x" * 65537)

    async def test_unsupported_platform_fails_closed(self):
        repl = PythonRepl()
        with patch("ai_dungeon_crawl.repl.sys.platform", "unsupported"):
            with self.assertRaisesRegex(RuntimeError, "no unsafe fallback"):
                await repl.execute_python("pass", GameObservation(0, ""), MockGameSession().step)


if __name__ == "__main__":
    unittest.main()
