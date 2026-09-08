import logging
from dataclasses import asdict
import sys
from pathlib import Path
from typing import Optional

from .contracts import GameEpisodeResult, GameSession, Policy, GameStep, AgentTurnRecord, StopExecution
from .shell import ShellTerminal
from .events import emit


class EpisodeRunner:
    """Run one episode with a fresh game, policy context and shell workspace."""

    def __init__(self, game: GameSession, policy: Policy,
                 terminal: Optional[ShellTerminal] = None,
                 manual_source: Optional[Path | str] = None):
        self._game = game
        self._policy = policy
        self._terminal = terminal if terminal is not None else ShellTerminal(
            manual_path=Path(manual_source) if manual_source is not None else None)

    async def run(self, *, max_turns: int = 50) -> GameEpisodeResult:
        if max_turns < 0:
            raise ValueError("Agent-turn limit cannot be negative")
        steps, turns = [], []
        try:
            observation = await self._game.start()
            emit("game.observation", **asdict(observation))

            async def press(action):
                nonlocal observation
                if observation.ended:
                    raise StopExecution("Game exited")
                following = await self._game.step(action)
                if following.id <= observation.id:
                    raise ValueError("GameObservation IDs must increase after each action")
                steps.append(GameStep(observation, action, following))
                observation = following
                emit("game.step", key=action.key, count=len(steps), observation=asdict(observation))
                return observation

            while not observation.ended and len(turns) < max_turns:
                emit("turn.started", id=len(turns))
                turn = await self._policy.request_turn(observation, tuple(turns))
                emit("execution.submitted", id=len(turns), code=turn.code, model_requests=turn.model_requests)
                first_step = len(steps)
                execution = await self._terminal.execute_shell(turn.code, observation, press)
                turns.append(AgentTurnRecord(len(turns), turn, execution, tuple(steps[first_step:])))
                emit("execution.finished", id=len(turns) - 1, **asdict(execution))
                if execution.status == "timeout":
                    break

            if observation.ended:
                reason = "game_exited"
            elif turns and turns[-1].execution.status == "timeout":
                reason = "execution_timeout"
            else:
                reason = "turn_limit"
            return GameEpisodeResult(reason, observation, tuple(steps), tuple(turns))
        finally:
            # Attempt both cleanups and preserve the original failure, if any.
            original_error = sys.exc_info()[1]
            cleanup_error = None
            for close in (self._terminal.close, self._game.close):
                try:
                    await close()
                except Exception as exc:
                    cleanup_error = cleanup_error or exc
                    logging.getLogger(__name__).exception("Episode cleanup failed")
            if original_error is None and cleanup_error is not None:
                raise cleanup_error
