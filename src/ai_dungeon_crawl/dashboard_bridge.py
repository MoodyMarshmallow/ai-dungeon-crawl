import argparse
import asyncio
import json
import os
import signal

from .cli import add_game_arguments, run_episode
from .agent.policies import REASONING_EFFORTS


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
        await run_episode(args.policy, args.model, args.max_turns, args.crawl_path,
                          args.manual_path, args.reasoning_effort, args.reasoning_summary,
                          sink=publish, review_turn_limit=args.review_turn_limit,
                          episode_limit=args.episode_limit)
    except asyncio.CancelledError:
        pass  # Session runner already emitted the terminal event.
    except Exception as exc:
        # Provider exception text can contain secrets or request bodies.
        pass  # Session runner already emitted the sanitized terminal event.
    finally:
        watcher.cancel()


def main():
    parser = argparse.ArgumentParser(description="Private dashboard event bridge")
    add_game_arguments(parser)
    parser.add_argument("--policy", choices=("codex", "pydantic"), default="codex")
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort", choices=REASONING_EFFORTS, default="default")
    parser.add_argument("--max-turns", "--action-turn-limit", dest="max_turns", type=int, default=3)
    parser.add_argument("--review-turn-limit", type=int, default=3)
    parser.add_argument("--episode-limit", type=int, default=1)
    parser.add_argument("--reasoning-summary", action="store_true")
    args = parser.parse_args()
    if args.max_turns < 0 or args.review_turn_limit < 0 or args.episode_limit < 1:
        parser.error("Turn limits must be nonnegative and episode limit must be positive")
    if args.policy == "codex" and not args.model:
        args.model = "gpt-5.6-luna"
    if not args.model:
        parser.error("The Pydantic backend requires --model provider:model")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
