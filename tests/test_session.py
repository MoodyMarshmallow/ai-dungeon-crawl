import asyncio
import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from ai_dungeon_crawl.contracts import AgentTurn, ExecutionResult, GameObservation, GameAction
from ai_dungeon_crawl.session import run_session, run_review
from ai_dungeon_crawl.shell.terminal import ShellTerminal
from ai_dungeon_crawl.agent.policies import PydanticPolicy, REVIEW_SHELL_DESCRIPTION
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart


class FakeGame:
    def __init__(self, outcome):
        self.outcome = outcome
        self.closed = False

    async def start(self):
        return GameObservation(0, "screen")

    async def step(self, action):
        return GameObservation(1, "done", ended=self.outcome is not None)

    async def close(self):
        self.closed = True


class FakeTerminal:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.published = False
        self.instances.append(self)
        if kwargs.get("profile") == "review":
            # Journal is complete before the review terminal is constructed.
            rows = (kwargs["review_log_path"] / "model.jsonl").read_text().splitlines()
            assert json.loads(rows[-1])["event"] == "phase.finished"

    async def execute_shell(self, code, observation, press, **kwargs):
        if self.kwargs.get("profile") != "review":
            observation = await press(GameAction("l"))
        return ExecutionResult(observation, output=code)

    async def publish_artifacts(self):
        self.published = True

    async def close(self):
        self.closed = True


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_review_with_real_uninitialized_terminal(self):
        with tempfile.TemporaryDirectory() as temp:
            terminal = ShellTerminal(profile="review", artifacts_path=Path(temp) / "artifacts",
                                     review_log_path=Path(temp))
            result = await run_review(None, terminal, max_turns=0)
            self.assertEqual(result, ())
            self.assertTrue(terminal._closed)
            self.assertFalse((Path(temp) / "artifacts").exists())

    async def test_review_output_tool_profile_never_injects_screen(self):
        def model(messages, info):
            self.assertTrue(info.output_tools[0].description.startswith(REVIEW_SHELL_DESCRIPTION))
            self.assertEqual(info.function_tools, [])
            prompts = [p.content for message in messages for p in message.parts
                       if isinstance(p, UserPromptPart)]
            self.assertNotIn("secret screen", str(prompts))
            return ModelResponse(parts=[ToolCallPart("execute_shell", {"code": "ls logs"})])
        policy = PydanticPolicy(FunctionModel(model), profile="review", initial_prompt="Review death")
        turn = await policy.request_turn(GameObservation(0, "secret screen"), ())
        self.assertEqual(turn.code, "ls logs")

    async def run_case(self, outcomes, *, review_turn_limit=2, episode_limit=3, cancel=False):
        FakeTerminal.instances = []
        games, policies, events = [], [], []
        review_started = asyncio.Event()

        def game_factory(path):
            game = FakeGame(outcomes[len(games)])
            games.append(game)
            return game

        def policy_factory(profile, *, prompts=None):
            class Policy:
                def __init__(self):
                    self.profile, self.histories = profile, []

                async def request_turn(self, observation, history):
                    self.histories.append(history)
                    if profile == "review" and cancel:
                        review_started.set()
                        await asyncio.Event().wait()
                    return AgentTurn("review" if profile == "review" else "action")
            policy = Policy()
            policies.append(policy)
            return policy

        with tempfile.TemporaryDirectory() as temp, patch(
                "ai_dungeon_crawl.session.ShellTerminal", FakeTerminal):
            task = asyncio.create_task(run_session(directory=Path(temp), game_factory=game_factory,
                policy_factory=policy_factory, action_turn_limit=1, review_turn_limit=review_turn_limit,
                episode_limit=episode_limit, sink=lambda event, data: events.append((event, data))))
            if cancel:
                await asyncio.wait_for(review_started.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            else:
                await task
        return games, policies, events

    async def test_death_review_fresh_next_action_and_global_ids(self):
        games, policies, events = await self.run_case(["death", "win"])
        self.assertEqual([p.profile for p in policies], ["action", "review", "action"])
        self.assertTrue(all(p.histories[0] == () for p in policies))
        self.assertEqual(len(policies[1].histories[1]), 1)
        self.assertEqual([d["id"] for e, d in events if e == "turn.started"], [0, 1, 2, 3])
        self.assertEqual([d["mode"] for e, d in events if e == "mode.changed"], ["action", "review", "action"])
        self.assertEqual(sum(e == "episode.finished" for e, _ in events), 1)
        self.assertTrue(all(g.closed for g in games))
        self.assertTrue(all(t.closed for t in FakeTerminal.instances))
        self.assertTrue(FakeTerminal.instances[1].published)
        self.assertEqual(FakeTerminal.instances[0].kwargs["artifacts_path"],
                         FakeTerminal.instances[2].kwargs["artifacts_path"])

    async def test_final_death_still_reviews(self):
        games, policies, _ = await self.run_case(["death"], episode_limit=1)
        self.assertEqual([p.profile for p in policies], ["action", "review"])

    async def test_non_death_exit_quit_win_and_limit_do_not_review(self):
        for outcome in ("win", "quit", None):
            games, policies, _ = await self.run_case([outcome])
            self.assertEqual(len(games), 1)
            self.assertEqual([p.profile for p in policies], ["action"])

    async def test_stop_during_review_closes_and_never_starts_next_game(self):
        games, policies, events = await self.run_case(["death"], cancel=True)
        self.assertEqual(len(games), 1)
        self.assertTrue(all(t.closed for t in FakeTerminal.instances))
        self.assertFalse(FakeTerminal.instances[1].published)
        self.assertEqual([d for e, d in events if e == "episode.finished"],
                         [{"status": "stopped", "stop_reason": "cancelled"}])

    async def test_zero_review_turns_proceeds_without_model_call(self):
        _, policies, _ = await self.run_case(["death", "win"], review_turn_limit=0)
        self.assertEqual(policies[1].histories, [])


@unittest.skipUnless(os.environ.get("DCSS_TEST_BINARY"), "Set DCSS_TEST_BINARY for real-game tests")
class RealSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_death_review_skill_and_new_game(self):
        """Exercise real game/sandbox boundaries with no model or provider calls."""
        from ai_dungeon_crawl.cli import create_game

        binary = Path(os.environ["DCSS_TEST_BINARY"]).resolve()
        manual = binary.parent.parent / "docs" / "crawl_manual.rst"
        games, policies, events = [], [], []
        death_keys = ("a", "&", *"wiz", "ENTER", "CTRL+T", *"you.die()", "ENTER", "Y")
        death_code = "set -e\n" + "\n".join("crawl press " + shlex.quote(key) for key in death_keys)
        review_code = """python - <<'PY'
import json, os, shutil
from pathlib import Path
assert sorted(p.name for p in Path('logs').iterdir()) == ['game.jsonl', 'model.jsonl']
assert Path('crawl_manual.rst').read_text()
assert 'CRAWL_SOCKET' not in os.environ
assert shutil.which('crawl') is None
model = [json.loads(line) for line in Path('logs/model.jsonl').read_text().splitlines()]
game = [json.loads(line) for line in Path('logs/game.jsonl').read_text().splitlines()]
assert model[-1]['event'] == 'phase.finished'
assert model[-1]['data']['stop_reason'] == 'death'
assert any(row['event'] == 'game.step' and row['data']['observation']['outcome'] == 'death' for row in game)
assert not any(row['event'] == 'game.tiles' for row in game + model)
assert any(row['event'] == 'game.score' and row['data']['final'] for row in game)
Path('skills/death-review.md').write_text('Consult the manual before taking a lethal action.\\n')
print('review-verified-manual-and-two-logs')
PY"""
        next_code = """set -e
python - <<'PY'
from pathlib import Path
assert Path('skills/death-review.md').read_text() == 'Consult the manual before taking a lethal action.\\n'
assert not Path('logs').exists()
try:
    Path('skills/death-review.md').write_text('overwrite')
except PermissionError:
    pass
else:
    raise AssertionError('action could overwrite review skill')
print('next-action-read-only-skill-verified')
PY
crawl press a
crawl observe"""

        def game_factory(path):
            game = create_game(binary, save_dir=path)
            games.append(game)
            return game

        def policy_factory(profile, *, prompts=None):
            if profile == "review":
                self.assertIsNotNone(games[0]._process.returncode)
                code = review_code
            else:
                code = death_code if len(games) == 1 else next_code

            class DeterministicPolicy:
                async def request_turn(inner, observation, history):
                    self.assertEqual(history, ())
                    return AgentTurn(code, timeout_ms=30000)

            policy = DeterministicPolicy()
            policies.append(policy)
            return policy

        with tempfile.TemporaryDirectory(prefix="dcss-review-session-test-") as temp:
            directory = Path(temp)
            result = await asyncio.wait_for(run_session(
                directory=directory, game_factory=game_factory, policy_factory=policy_factory,
                action_turn_limit=1, review_turn_limit=1, episode_limit=2, manual_path=manual,
                sink=lambda event, data: events.append((event, data))), 60)
            review_rows = [json.loads(line) for line in
                           (directory / "episode-001/review/model.jsonl").read_text().splitlines()]
            self.assertEqual((directory / "artifacts/snapshot/skills/death-review.md").read_text(),
                             "Consult the manual before taking a lethal action.\n")
            self.assertTrue(all(row['timestamp'] is not None for row in review_rows))

        self.assertEqual(len(games), 2)
        self.assertEqual(len({id(policy) for policy in policies}), 3)
        self.assertNotEqual(games[0]._process.pid, games[1]._process.pid)
        self.assertTrue(all(game._process.returncode is not None for game in games))
        self.assertEqual(result.stop_reason, "turn_limit")
        self.assertFalse(result.final_observation.ended)
        self.assertEqual([(data['mode'], data['episode']) for event, data in events
                          if event == 'mode.changed'], [('action', 1), ('review', 1), ('action', 2)])
        executions = [data for event, data in events if event == 'execution.finished']
        self.assertEqual([data['status'] for data in executions], ['ok', 'ok', 'ok'])
        self.assertIn('review-verified-manual-and-two-logs', executions[1]['output'])
        self.assertIn('next-action-read-only-skill-verified', executions[2]['output'])
        self.assertEqual(sum(event == 'episode.finished' for event, _ in events), 1)
