import argparse
import asyncio
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

from .mock_game import MockGameSession
from .episode import EpisodeRunner
from .policies import REASONING_EFFORTS, create_policy
from .events import emit, observe_events
from .run_log import episode_log


MOCK_GOAL = (
    "This is a four-cell corridor mock, not DCSS yet. Move @ to the rightmost "
    "cell using only the key 'l'. Three successful inputs end the game. "
    "Use observe().ended to stop."
)

DEFAULT_CRAWL_PATH = Path(__file__).resolve().parents[3] / "crawl/crawl-ref/source/crawl-web-harness"


def add_game_arguments(parser):
    """Share game selection between terminal and dashboard launches."""
    parser.add_argument("--game", choices=("dcss", "mock"), default="dcss")
    parser.add_argument("--crawl-path", type=Path, default=DEFAULT_CRAWL_PATH,
                        help="Path to the instrumented WebTiles DCSS executable")
    parser.add_argument("--manual-path", type=Path, default=None,
                        help="Optional local Crawl manual to expose to shell scripts")


def create_game(game: str, crawl_path: Path = DEFAULT_CRAWL_PATH, *, save_dir: Path | None = None):
    """Start real games in separate persistent save directories; mocks remain explicit."""
    if game == "mock":
        return MockGameSession()
    if game != "dcss":
        raise ValueError(f"Unknown game: {game}")
    from .dcss import DCSSGameSession
    executable = Path(crawl_path).expanduser().resolve()
    if not executable.is_file():
        raise FileNotFoundError("DCSS build missing; run the DCSS build helper or pass --crawl-path")
    save_dir = save_dir or new_run_directory()
    return DCSSGameSession(
        executable, cwd=executable.parent, save_dir=save_dir,
        on_tiles=lambda messages: emit("game.tiles", messages=list(messages)),
    )


def new_run_directory() -> Path:
    return Path(__file__).resolve().parents[2] / "runs" / uuid4().hex


async def run_episode(backend: str, model: str | None, max_turns: int,
                      game: str = "dcss", crawl_path: Path = DEFAULT_CRAWL_PATH,
                      manual_path: Path | None = None, reasoning_effort: str = "default",
                      reasoning_summary: bool = True, sink=None, run_dir: Path | None = None):
    """Journal one CLI or dashboard episode, including setup errors and cancellation."""
    directory = run_dir if run_dir is not None else new_run_directory()
    with episode_log(directory) as log_path, (observe_events(sink) if sink else nullcontext()):
        config = {"backend": backend, "model": model, "max_turns": max_turns,
                  "game": game, "reasoning_effort": reasoning_effort,
                  "reasoning_summary": reasoning_summary}
        emit("episode.started", config=config, log_path=str(log_path))
        try:
            policy = create_policy(backend, model=model, reasoning_effort=reasoning_effort,
                                   reasoning_summary=reasoning_summary,
                                   goal=MOCK_GOAL if game == "mock" else "Play Dungeon Crawl Stone Soup and win.")
            if manual_path is None and game == "dcss":
                manual_path = Path(crawl_path).resolve().parent.parent / "docs" / "crawl_manual.rst"
            result = await EpisodeRunner(create_game(game, crawl_path, save_dir=directory), policy,
                                         manual_source=manual_path).run(max_turns=max_turns)
        except asyncio.CancelledError:
            emit("episode.finished", status="stopped", stop_reason="cancelled")
            raise
        except Exception as exc:
            # Exception messages may contain provider request bodies or credentials.
            emit("episode.finished", status="error", error=type(exc).__name__)
            raise
        else:
            emit("episode.finished", status="completed", stop_reason=result.stop_reason)
            return result


async def run(backend: str, model: str | None, max_turns: int,
              game: str = "dcss", crawl_path: Path = DEFAULT_CRAWL_PATH,
              manual_path: Path | None = None, reasoning_effort: str = "default"):
    print(f"Game: {game}; policy: {backend}", flush=True)
    directory = new_run_directory()
    print(f"Log: {directory / 'events.jsonl'}", flush=True)
    result = await run_episode(backend, model, max_turns, game, crawl_path,
                               manual_path, reasoning_effort, run_dir=directory)
    for record in result.turns:
        print(f"\nAgent turn {record.id}: {len(record.steps)} actions; {record.execution.status}")
        print(record.execution.output, end="")
        if record.execution.error:
            print(record.execution.error)
    print("\n" + result.final_observation.screen)
    print(f"Stopped: {result.stop_reason}; actions: {len(result.steps)}; turns: {len(result.turns)}")


def main():
    parser = argparse.ArgumentParser(description="Run an AI Dungeon Crawl episode.")
    add_game_arguments(parser)
    parser.add_argument("--policy", choices=("codex", "pydantic"), default="codex")
    parser.add_argument("--model", help="Codex model name, or PydanticAI provider:model")
    parser.add_argument("--reasoning-effort", choices=REASONING_EFFORTS, default="default")
    parser.add_argument("--max-turns", type=int, default=3)
    args = parser.parse_args()
    if args.max_turns < 0:
        parser.error("Turn limit must be nonnegative")
    if args.policy == "codex" and not args.model:
        args.model = "gpt-5.6-luna"
    if not args.model:
        parser.error("Model-backed policies require --model (provider:model for pydantic)")
    asyncio.run(run(args.policy, args.model, args.max_turns,
                     args.game, args.crawl_path, args.manual_path, args.reasoning_effort))
