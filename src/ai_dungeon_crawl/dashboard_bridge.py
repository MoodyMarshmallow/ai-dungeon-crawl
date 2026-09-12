import argparse
import asyncio
import json
import os
import signal

from .cli import add_game_arguments, add_session_arguments, session_config_from_args, run_episode
from .config import SessionConfig


def publish(event, data):
    """Write one display event for the Bun server, never raw provider responses."""
    print(json.dumps({"event": event, "data": data}), flush=True)


async def run(args, config: SessionConfig):
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
        await run_episode(config, args.crawl_path, args.manual_path, sink=publish)
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
    add_session_arguments(parser, reasoning_summary=False)
    args = parser.parse_args()
    try:
        config = session_config_from_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    asyncio.run(run(args, config))


if __name__ == "__main__":
    main()
