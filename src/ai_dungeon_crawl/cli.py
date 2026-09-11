import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .agent.policies import REASONING_EFFORTS, create_policy
from .agent.prompts import read_only_prompt
from .events import emit


DEFAULT_CRAWL_PATH = Path(__file__).resolve().parents[3] / "crawl/crawl-ref/source/crawl-web-harness"


def add_game_arguments(parser):
    """Share DCSS launch arguments between terminal and dashboard launches."""
    parser.add_argument("--crawl-path", type=Path, default=DEFAULT_CRAWL_PATH,
                        help="Path to the instrumented WebTiles DCSS executable")
    parser.add_argument("--manual-path", type=Path, default=None,
                        help="Optional local Crawl manual to expose to shell scripts")


def create_game(crawl_path: Path = DEFAULT_CRAWL_PATH, *, save_dir: Path | None = None):
    """Start a real DCSS game in a separate persistent save directory."""
    from .game.dcss import DCSSGameSession
    executable = Path(crawl_path).expanduser().resolve()
    if not executable.is_file():
        raise FileNotFoundError("DCSS build missing; run the DCSS build helper or pass --crawl-path")
    save_dir = save_dir or new_run_directory()
    return DCSSGameSession(
        executable, cwd=executable.parent, save_dir=save_dir,
        on_tiles=lambda messages: emit("game.tiles", messages=list(messages)),
        on_score=lambda score, game_turn, final, game_time: emit(
            "game.score", score=score, game_turn=game_turn, final=final, game_time=game_time),
    )


def new_run_directory() -> Path:
    """Name runs chronologically in UTC, with a random suffix for uniqueness."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S.%fZ")
    return Path(__file__).resolve().parents[2] / "runs" / f"{timestamp}_{uuid4().hex}"


async def run_episode(backend: str, model: str | None, max_turns: int,
                      crawl_path: Path = DEFAULT_CRAWL_PATH,
                      manual_path: Path | None = None, reasoning_effort: str = "default",
                      reasoning_summary: bool = True, sink=None, run_dir: Path | None = None,
                      *, action_turn_limit: int | None = None, review_turn_limit: int = 3,
                      episode_limit: int = 1):
    """Run one Start session, reviewing confirmed deaths before subsequent games."""
    from .session import run_session
    directory = run_dir if run_dir is not None else new_run_directory()
    action_limit = max_turns if action_turn_limit is None else action_turn_limit
    config = {"backend": backend, "model": model, "max_turns": action_limit,
                  "action_turn_limit": action_limit, "review_turn_limit": review_turn_limit,
                  "episode_limit": episode_limit,
                  "game": "dcss", "reasoning_effort": reasoning_effort,
                  "reasoning_summary": reasoning_summary}
    if manual_path is None:
        manual_path = Path(crawl_path).resolve().parent.parent / "docs" / "crawl_manual.rst"

    def policy_factory(profile, *, prompts=None):
        initial_prompt = read_only_prompt("review_agent/initial_prompt", single_line=True) if profile == "review" else prompts.initial_prompt
        return create_policy(backend, model=model, reasoning_effort=reasoning_effort,
                             reasoning_summary=reasoning_summary, initial_prompt=initial_prompt, profile=profile, prompts=prompts)

    return await run_session(directory=directory,
        game_factory=lambda path: create_game(crawl_path, save_dir=path),
        policy_factory=policy_factory, manual_path=manual_path, config=config, sink=sink,
        action_turn_limit=action_limit, review_turn_limit=review_turn_limit, episode_limit=episode_limit)


async def run(backend: str, model: str | None, max_turns: int,
              crawl_path: Path = DEFAULT_CRAWL_PATH,
              manual_path: Path | None = None, reasoning_effort: str = "default",
              review_turn_limit: int = 3, episode_limit: int = 1):
    print(f"Game: DCSS; policy: {backend}", flush=True)
    directory = new_run_directory()
    print(f"Log: {directory / 'model.jsonl'}", flush=True)
    result = await run_episode(backend, model, max_turns, crawl_path,
                               manual_path, reasoning_effort, run_dir=directory,
                               review_turn_limit=review_turn_limit, episode_limit=episode_limit)
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
    parser.add_argument("--max-turns", "--action-turn-limit", dest="max_turns", type=int, default=3)
    parser.add_argument("--review-turn-limit", type=int, default=3)
    parser.add_argument("--episode-limit", type=int, default=1)
    args = parser.parse_args()
    if args.max_turns < 0 or args.review_turn_limit < 0 or args.episode_limit < 1:
        parser.error("Turn limits must be nonnegative and episode limit must be positive")
    if args.policy == "codex" and not args.model:
        args.model = "gpt-5.6-luna"
    if not args.model:
        parser.error("Model-backed policies require --model (provider:model for pydantic)")
    asyncio.run(run(args.policy, args.model, args.max_turns,
                     args.crawl_path, args.manual_path, args.reasoning_effort,
                     args.review_turn_limit, args.episode_limit))
