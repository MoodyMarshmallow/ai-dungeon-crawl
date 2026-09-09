import asyncio
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_dungeon_crawl.contracts import GameAction, AgentTurn, GameObservation
from ai_dungeon_crawl.mock_game import MockGameSession
from ai_dungeon_crawl.episode import EpisodeRunner
from ai_dungeon_crawl.shell import ShellTerminal


class CodePolicy:
    def __init__(self, *scripts):
        self.scripts = scripts
        self.histories = []

    async def request_turn(self, observation, history):
        self.histories.append(history)
        return AgentTurn(self.scripts[len(history) % len(self.scripts)], model_requests=2)


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("sandbox-exec"),
                     "Shell integration requires macOS sandbox-exec")
class EpisodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_each_transition_and_closes_on_exit(self):
        game = MockGameSession()
        result = await EpisodeRunner(game, CodePolicy(
            'crawl press l >/dev/null\ncrawl press l >/dev/null',
            'crawl press l >/dev/null\nprintf "Total moves: 3\\n"',
        )).run()
        self.assertEqual(result.stop_reason, "game_exited")
        self.assertEqual(len(result.steps), 3)
        self.assertTrue(result.final_observation.ended)
        for step in result.steps:
            self.assertEqual(step.action.key, "l")
        self.assertEqual(result.turns[0].steps, result.steps[:2])
        self.assertEqual(result.turns[1].steps, result.steps[2:])
        self.assertEqual([len(turn.steps) for turn in result.turns], [2, 1])
        self.assertEqual(result.turns[1].execution.output, "Total moves: 3\n")
        self.assertEqual(result.turns[1].turn.model_requests, 2)
        for earlier, later in zip(result.steps, result.steps[1:]):
            self.assertEqual(earlier.after, later.before)
        self.assertTrue(game.closed)

    async def test_zero_budget_observes_without_applying_action(self):
        game = MockGameSession()
        result = await EpisodeRunner(game, CodePolicy('crawl press l')).run(max_turns=0)
        self.assertEqual(result.stop_reason, "turn_limit")
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
            await EpisodeRunner(game, CodePolicy('crawl press l')).run()
        self.assertEqual(game.position, 1)
        self.assertTrue(game.closed)

    async def test_turn_can_apply_many_keys_without_a_keypress_limit(self):
        class LongGame(MockGameSession):
            async def step(self, action):
                self.position += 1
                return GameObservation(self.position, "Still playing")
        game = LongGame()
        policy = CodePolicy('for i in {1..105}; do crawl press l; done')
        result = await EpisodeRunner(game, policy).run(max_turns=1)
        self.assertEqual(result.stop_reason, "turn_limit")
        self.assertEqual(len(result.steps), 105)
        self.assertEqual(len(result.turns), 1)
        self.assertEqual(result.turns[0].execution.status, "ok")
        self.assertEqual(game.position, 105)

    async def test_exit_prevents_further_keys_in_same_script(self):
        game = MockGameSession()
        result = await EpisodeRunner(game, CodePolicy(
            'for i in 1 2 3 4 5 6 7 8 9 10; do crawl press l; done')).run()
        self.assertEqual(result.stop_reason, "game_exited")
        self.assertEqual(len(result.steps), 3)

    async def test_script_error_preserves_steps_state_and_feedback(self):
        game = MockGameSession()
        policy = CodePolicy(
            'printf "41\\n" > saved\ncrawl press l >/dev/null\nprintf "moved\\n"\nexit 42',
            'awk "{print \\$1 + 1}" saved\ncrawl press l >/dev/null\ncrawl press l >/dev/null',
        )
        result = await EpisodeRunner(game, policy).run()
        first = policy.histories[1][0]
        self.assertEqual(first.execution.status, "error")
        self.assertEqual(first.execution.error, "Shell exited with status 42")
        self.assertEqual(first.execution.output, "moved\n")
        self.assertEqual(len(first.steps), 1)
        self.assertEqual(first.execution.observation.id, 1)
        self.assertEqual(result.turns[1].execution.output, "42\n")
        self.assertEqual(sum(turn.turn.model_requests for turn in result.turns), 4)
        self.assertEqual(result.turns[0].steps, result.steps[:1])
        self.assertEqual(result.turns[1].steps, result.steps[1:])

    async def test_syntax_error_does_not_execute_partial_source(self):
        result = await EpisodeRunner(MockGameSession(), CodePolicy(
            'if true; then\n  crawl press l\n')).run(max_turns=1)
        self.assertEqual(result.steps, ())
        self.assertEqual(result.turns[0].execution.status, "error")
        self.assertIn("syntax error", result.turns[0].execution.output)

    async def test_no_action_scripts_are_bounded_by_model_turn_limit(self):
        result = await EpisodeRunner(MockGameSession(), CodePolicy(
            'crawl observe >/dev/null')).run(max_turns=2)
        self.assertEqual(result.stop_reason, "turn_limit")
        self.assertEqual(len(result.turns), 2)
        self.assertEqual(result.steps, ())

    async def test_zero_turn_budget_never_requests_a_script(self):
        policy = CodePolicy('crawl press l')
        result = await EpisodeRunner(MockGameSession(), policy).run(max_turns=0)
        self.assertEqual(result.stop_reason, "turn_limit")
        self.assertEqual(policy.histories, [])

    async def test_output_limit_is_reported(self):
        result = await EpisodeRunner(MockGameSession(), CodePolicy(
            'python -c \'print("x" * 1000)\''),
                                     ShellTerminal(max_output_chars=12)).run(max_turns=1)
        self.assertEqual(result.turns[0].execution.output, "x" * 12)
        self.assertTrue(result.turns[0].execution.output_truncated)

    async def test_timeout_returns_feedback_and_preserves_workspace_for_next_turn(self):
        game, terminal = MockGameSession(), ShellTerminal(timeout_seconds=1)
        code = 'echo saved > saved\ncrawl press l\nprintf "before timeout\\n"\nwhile true; do :; done'
        policy = CodePolicy(code, 'cat saved\ncrawl press l\ncrawl press l')
        result = await EpisodeRunner(game, policy, terminal).run(max_turns=2)
        self.assertEqual(result.stop_reason, "game_exited")
        self.assertEqual(policy.histories[1][0].execution.status, "timeout")
        self.assertEqual(policy.histories[1][0].execution.observation.id, 1)
        self.assertEqual(result.turns[0].execution.output, "before timeout\n")
        self.assertEqual(result.turns[1].execution.output, "saved\n")
        self.assertEqual(result.turns[1].execution.status, "ok")
        self.assertEqual(len(result.steps), 3)
        self.assertTrue(game.closed)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await terminal.execute_shell(":", result.final_observation, game.step)

    async def test_repeated_timeouts_still_respect_turn_limit(self):
        result = await EpisodeRunner(MockGameSession(), CodePolicy('while true; do :; done'),
                                     ShellTerminal(timeout_seconds=.2)).run(max_turns=2)
        self.assertEqual(result.stop_reason, "turn_limit")
        self.assertEqual([turn.execution.status for turn in result.turns], ["timeout", "timeout"])

    async def test_shell_errors_allow_next_turn(self):
        for code in ('if true; then', 'missing_command_for_test', 'crawl press ll', 'cat /etc/passwd'):
            with self.subTest(code=code):
                policy = CodePolicy(code, 'crawl press l; crawl press l; crawl press l')
                result = await EpisodeRunner(MockGameSession(), policy).run(max_turns=2)
                self.assertEqual(policy.histories[1][0].execution.status, "error")
                self.assertEqual(result.stop_reason, "game_exited")

    async def test_invalid_key_becomes_feedback_without_game_input(self):
        result = await EpisodeRunner(MockGameSession(), CodePolicy('crawl press ll')).run(max_turns=1)
        self.assertEqual(result.steps, ())
        self.assertEqual(result.turns[0].execution.status, "error")
        self.assertIn("GameAction must contain", result.turns[0].execution.output)

    async def test_new_episode_gets_fresh_namespace(self):
        await EpisodeRunner(MockGameSession(), CodePolicy("printf '42\\n' > saved")).run(max_turns=1)
        result = await EpisodeRunner(MockGameSession(), CodePolicy("cat saved")).run(max_turns=1)
        self.assertEqual(result.turns[0].execution.status, "error")
        self.assertIn("No such file", result.turns[0].execution.output)

    async def test_cancellation_closes_game_and_worker(self):
        started = asyncio.Event()

        class WaitingGame(MockGameSession):
            async def step(self, action):
                started.set()
                await asyncio.Event().wait()

        game, terminal = WaitingGame(), ShellTerminal()
        task = asyncio.create_task(EpisodeRunner(game, CodePolicy('crawl press l'), terminal).run())
        await asyncio.wait_for(started.wait(), timeout=10)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(game.closed)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await terminal.execute_shell(":", GameObservation(0, ""), game.step)

    async def test_sandbox_denies_synthetic_file_network_and_environment_access(self):
        # Only artificial data: never probe real user secrets in security tests.
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "sentinel.txt"
            sentinel.write_text("fake secret")
            target = Path(directory) / "must-not-exist.txt"
            code = f'''python - <<'PY'
import os, socket
checks = [
    lambda: open({str(sentinel)!r}).read(),
    lambda: open({str(target)!r}, "w"),
    lambda: socket.create_connection(("127.0.0.1", 9), timeout=1),
]
for check in checks:
    try:
        check()
    except PermissionError:
        print("denied")
print(os.environ.get("AI_DUNGEON_TEST_SECRET", "absent"))
PY
'''
            with patch.dict("os.environ", {"AI_DUNGEON_TEST_SECRET": "fake credential"}):
                result = await EpisodeRunner(MockGameSession(), CodePolicy(code)).run(max_turns=1)
            execution = result.turns[0].execution
            self.assertIsNone(execution.error)
            self.assertEqual(execution.output, "denied\n" * 3 + "absent\n")
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
        terminal = ShellTerminal()
        with patch("ai_dungeon_crawl.shell.sys.platform", "unsupported"):
            with self.assertRaisesRegex(RuntimeError, "no unsafe fallback"):
                await terminal.execute_shell(":", GameObservation(0, ""), MockGameSession().step)


if __name__ == "__main__":
    unittest.main()
