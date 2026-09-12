import argparse
from dataclasses import FrozenInstanceError
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_dungeon_crawl.agent.prompts import base_prompts
from ai_dungeon_crawl.cli import add_session_arguments, session_config_from_args
from ai_dungeon_crawl.config import AgentConfig, SessionConfig, REASONING_EFFORTS
from ai_dungeon_crawl.contracts import AgentTurn, ExecutionResult, GameAction, GameObservation, GameSession, Policy
from ai_dungeon_crawl.session import run_session


class ConfigTests(unittest.TestCase):
    def test_configs_are_immutable_and_have_independent_agent_defaults(self):
        config = SessionConfig()
        with self.assertRaises(FrozenInstanceError):
            config.action_turn_limit = 4
        with self.assertRaises(FrozenInstanceError):
            config.action_agent.model = "changed"
        self.assertIsNot(config.action_agent, config.review_agent)

    def test_agent_validation_rejects_invalid_values(self):
        invalid = (
            {"backend": "unknown"}, {"model": ""}, {"model": 42},
            {"reasoning_effort": "strong"}, {"reasoning_summary": 1},
            {"timeout_seconds": 0}, {"timeout_seconds": True},
            {"max_requests": 0}, {"max_requests": True},
            {"backend": "pydantic", "model": "example"},
            {"backend": "pydantic", "model": "anthropic:x", "reasoning_effort": "high"},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                AgentConfig(**changes)
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                AgentConfig(timeout_seconds=value)

    def test_session_validation_rejects_invalid_limits_and_agents(self):
        for field in ("action_turn_limit", "review_turn_limit"):
            for value in (-1, True, 1.5):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    SessionConfig(**{field: value})
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SessionConfig(episode_limit=value)
        with self.assertRaises(ValueError):
            SessionConfig(action_agent="invalid")

    def test_shared_parser_defaults_preserve_cli_summary_variants(self):
        terminal = argparse.ArgumentParser()
        add_session_arguments(terminal)
        dashboard = argparse.ArgumentParser()
        add_session_arguments(dashboard, reasoning_summary=False)
        terminal_config = session_config_from_args(terminal.parse_args([]))
        dashboard_config = session_config_from_args(dashboard.parse_args([]))
        self.assertTrue(terminal_config.action_agent.reasoning_summary)
        self.assertFalse(dashboard_config.action_agent.reasoning_summary)
        self.assertEqual(terminal_config.action_agent.model, AgentConfig().model)
        self.assertEqual(set(REASONING_EFFORTS), set(terminal._option_string_actions["--reasoning-effort"].choices))


class ConfigPropagationTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_session_passes_distinct_action_and_review_configs(self):
        seen = []
        action_config = AgentConfig(backend="pydantic", model="test:action", reasoning_summary=False,
                                    timeout_seconds=11, max_requests=3, reasoning_effort="default")
        review_config = AgentConfig(backend="pydantic", model="test:review", reasoning_summary=True,
                                    timeout_seconds=22, max_requests=4, reasoning_effort="default")
        config = SessionConfig(action_agent=action_config, review_agent=review_config,
                               action_turn_limit=1, review_turn_limit=0, episode_limit=1)

        class Game(GameSession):
            outcome = "death"

            async def start(self):
                return GameObservation(0, "screen")
            async def step(self, action):
                return GameObservation(1, "dead", ended=True, outcome="death")
            async def close(self):
                pass

        class Terminal:
            def __init__(self, **kwargs):
                pass
            async def execute_shell(self, code, observation, press, **kwargs):
                if press is not None:
                    observation = await press(GameAction("x"))
                return ExecutionResult(observation, output=code)
            async def close(self):
                pass

        class PolicyDouble(Policy):
            def __init__(self, profile):
                self.profile = profile
            async def request_turn(self, history):
                return AgentTurn(":")
            def prompt_reference(self):
                return {"prompts": base_prompts().effective_text()}

        def policy_factory(agent_config, *, profile, prompts=None):
            seen.append((agent_config, profile))
            return PolicyDouble(profile)

        with tempfile.TemporaryDirectory() as temp, patch("ai_dungeon_crawl.session.ShellTerminal", Terminal):
            await run_session(directory=Path(temp) / "run", game_factory=lambda path: Game(),
                              policy_factory=policy_factory, config=config)
        self.assertEqual(seen, [(action_config, "action"), (review_config, "review")])
