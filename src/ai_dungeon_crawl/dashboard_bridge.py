import argparse
import asyncio
import json
import os
import signal

from .cli import add_game_arguments, run_episode
from .policies import REASONING_EFFORTS


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
        await run_episode(args.policy, args.model, args.max_turns, args.game, args.crawl_path,
                          args.manual_path, args.reasoning_effort, args.reasoning_summary,
                          sink=publish)
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
