"""Own the model-turn loop and mediate every game input from the REPL."""

import logging
import sys
from typing import Optional

from .contracts import EpisodeResult, GameSession, Policy, Step, TurnRecord
from .repl import PythonRepl, StopExecution


class EpisodeRunner:
    """Run one episode with a fresh game, policy context and REPL namespace."""

    def __init__(self, game: GameSession, policy: Policy, repl: Optional[PythonRepl] = None):
        self._game = game
        self._policy = policy
        self._repl = repl if repl is not None else PythonRepl()

    async def run(self, *, max_steps: int = 100, max_turns: int = 50) -> EpisodeResult:
        if max_steps < 0 or max_turns < 0:
            raise ValueError("Action and model-turn limits cannot be negative")
        steps, turns = [], []
        try:
            observation = await self._game.start()

            async def press(action):
                nonlocal observation
                if observation.ended:
                    raise StopExecution("Game exited")
                if len(steps) >= max_steps:
                    raise StopExecution("Action limit reached")
                following = await self._game.step(action)
                if following.id <= observation.id:
                    raise ValueError("Observation IDs must increase after each action")
                steps.append(Step(observation, action, following))
                observation = following
                return observation

            while not observation.ended and len(steps) < max_steps and len(turns) < max_turns:
                turn = await self._policy.request_turn(observation, tuple(turns))
                first_step = len(steps)
                execution = await self._repl.execute_python(turn.code, observation, press)
                turns.append(TurnRecord(len(turns), turn, execution, tuple(steps[first_step:])))
                if execution.status == "timeout":
                    break

            if observation.ended:
                reason = "game_exited"
            elif len(steps) >= max_steps:
                reason = "step_limit"
            elif turns and turns[-1].execution.status == "timeout":
                reason = "repl_timeout"
            else:
                reason = "turn_limit"
            return EpisodeResult(reason, observation, tuple(steps), tuple(turns))
        finally:
            # Attempt both cleanups and preserve the original failure, if any.
            original_error = sys.exc_info()[1]
            cleanup_error = None
            for close in (self._repl.close, self._game.close):
                try:
                    await close()
                except Exception as exc:
                    cleanup_error = cleanup_error or exc
                    logging.getLogger(__name__).exception("Episode cleanup failed")
            if original_error is None and cleanup_error is not None:
                raise cleanup_error
