import argparse
import asyncio

from .mock_game import MockGameSession
from .episode import EpisodeRunner
from .policies import create_policy


MOCK_GOAL = (
    "This is a four-cell corridor mock, not DCSS yet. Move @ to the rightmost "
    "cell using only the key 'l'. Three successful inputs end the game. "
    "Use observe().ended to stop."
)


async def run(backend: str, model: str | None, max_steps: int, max_turns: int):
    policy = create_policy(backend, model=model, goal=MOCK_GOAL)
    print(f"Mock game; policy: {backend}", flush=True)
    result = await EpisodeRunner(MockGameSession(), policy).run(
        max_steps=max_steps, max_turns=max_turns,
    )
    for record in result.turns:
        print(f"\nAgent turn {record.id}: {len(record.steps)} actions; {record.execution.status}")
        print(record.execution.output, end="")
        if record.execution.error:
            print(record.execution.error)
    print("\n" + result.final_observation.screen)
    print(f"Stopped: {result.stop_reason}; actions: {len(result.steps)}; turns: {len(result.turns)}")


def main():
    parser = argparse.ArgumentParser(description="Run an AI Dungeon Crawl mock-game episode.")
    parser.add_argument("--policy", choices=("codex", "pydantic"), default="codex")
    parser.add_argument("--model", help="Codex model name, or PydanticAI provider:model")
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--max-turns", type=int, default=3)
    args = parser.parse_args()
    if args.max_steps < 0 or args.max_turns < 0:
        parser.error("Limits must be nonnegative")
    if args.policy == "codex" and not args.model:
        args.model = "gpt-5.6-luna"
    if not args.model:
        parser.error("Model-backed policies require --model (provider:model for pydantic)")
    asyncio.run(run(args.policy, args.model, args.max_steps, args.max_turns))
