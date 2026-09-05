"""A deliberately tiny simulation; this does not launch DCSS or call an LLM."""

import asyncio
from typing import Tuple

from .contracts import Action, ModelTurn, Observation, TurnRecord
from .episode import EpisodeRunner


class MockGameSession:
    def __init__(self) -> None:
        self.position = 0
        self.closed = False

    def _observe(self) -> Observation:
        row = ["."] * 4
        row[self.position] = "@"
        return Observation(
            id=self.position,
            screen="######\n#" + "".join(row) + "#\n######",
            ended=self.position == 3,
        )

    async def start(self) -> Observation:
        return self._observe()

    async def step(self, action: Action) -> Observation:
        if self.closed or self.position == 3:
            raise RuntimeError("Session is not accepting input")
        if action.key != "l":
            raise ValueError("This mock accepts only 'l' (move right)")
        self.position += 1
        return self._observe()

    async def close(self) -> None:
        self.closed = True


class ScriptedPolicy:
    async def request_turn(
        self, observation: Observation, history: Tuple[TurnRecord, ...]
    ) -> ModelTurn:
        if not history:
            return ModelTurn("""moves = 0
async def walk(count):
    global moves
    for _ in range(count):
        if observe().ended:
            break
        await press("l")
        moves += 1
await walk(2)
print("Moves so far:", moves)
""")
        return ModelTurn('await walk(1)\nprint("Total moves:", moves)')


async def run_demo() -> None:
    result = await EpisodeRunner(MockGameSession(), ScriptedPolicy()).run()
    print("REPL demo (simulated game, no model calls)")
    for turn in result.turns:
        print(f"\nModel turn {turn.id}: {len(turn.steps)} actions")
        print(turn.execution.output, end="")
    for step in result.steps:
        print(f"\nObservation {step.before.id}: send {step.action.key!r}")
        print(step.after.screen)
    print(f"\nStopped: {result.stop_reason}; actions: {len(result.steps)}; turns: {len(result.turns)}")


def main() -> None:
    asyncio.run(run_demo())
