import argparse
import asyncio
import json
import os
from pathlib import Path
import signal

from .cli import MOCK_GOAL, add_game_arguments, create_game
from .episode import EpisodeRunner
from .events import observe_events
from .policies import REASONING_EFFORTS, create_policy


def publish(event, data):
    """Write one display event for the Bun server, never raw provider responses."""
    print(json.dumps({"event": event, "data": data}), flush=True)


async def run(args):
    """Run one episode; SIGTERM cancels it through the runner's normal cleanup."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    def cancel_once():
        if not task.cancelling():
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, cancel_once)
    parent = os.getppid()

    async def watch_parent():
        while True:
            await asyncio.sleep(1)
            if os.getppid() != parent:
                cancel_once()
                return

    watcher = asyncio.create_task(watch_parent())
    try:
        policy = create_policy(args.policy, model=args.model,
                               goal=MOCK_GOAL if args.game == "mock" else "Play Dungeon Crawl Stone Soup and win.",
                               reasoning_summary=args.reasoning_summary,
                               reasoning_effort=args.reasoning_effort)
        with observe_events(publish):
            manual_path = args.manual_path
            if manual_path is None and args.game == "dcss":
                manual_path = Path(args.crawl_path).resolve().parent.parent / "docs" / "crawl_manual.rst"
            result = await EpisodeRunner(create_game(args.game, args.crawl_path), policy,
                                         manual_source=manual_path).run(
                max_turns=args.max_turns)
        publish("episode.finished", {"status": "completed", "stop_reason": result.stop_reason})
    except asyncio.CancelledError:
        publish("episode.finished", {"status": "stopped", "stop_reason": "cancelled"})
    except Exception as exc:
        # Provider exception text can contain secrets or request bodies.
        publish("episode.finished", {"status": "error", "error": type(exc).__name__})
    finally:
        watcher.cancel()


def main():
    parser = argparse.ArgumentParser(description="Private dashboard event bridge")
    add_game_arguments(parser)
    parser.add_argument("--policy", choices=("codex", "pydantic"), default="codex")
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort", choices=REASONING_EFFORTS, default="default")
    parser.add_argument("--max-turns", type=int, default=3)
    parser.add_argument("--reasoning-summary", action="store_true")
    args = parser.parse_args()
    if args.max_turns < 0:
        parser.error("Turn limit must be nonnegative")
    if args.policy == "codex" and not args.model:
        args.model = "gpt-5.6-luna"
    if not args.model:
        parser.error("The Pydantic backend requires --model provider:model")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
