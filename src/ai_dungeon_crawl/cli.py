import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .agent.policies import create_policy
from .config import AgentConfig, SessionConfig, REASONING_EFFORTS
from .agent.prompts import read_only_prompt
from .events import emit


DEFAULT_CRAWL_PATH = Path(__file__).resolve().parents[3] / "crawl/crawl-ref/source/crawl-web-harness"


def add_game_arguments(parser):
    """Share DCSS launch arguments between terminal and dashboard launches."""
    parser.add_argument("--crawl-path", type=Path, default=DEFAULT_CRAWL_PATH,
                        help="Path to the instrumented WebTiles DCSS executable")
    parser.add_argument("--manual-path", type=Path, default=None,
                        help="Optional local Crawl manual to expose to shell scripts")


def add_session_arguments(parser, *, reasoning_summary=True):
    """Keep terminal and dashboard configuration ingress in one place."""
    defaults = SessionConfig()
    parser.add_argument("--policy", choices=("codex", "pydantic"), default=defaults.action_agent.backend)
    parser.add_argument("--model", help="Codex model name, or PydanticAI provider:model")
    parser.add_argument("--reasoning-effort", choices=REASONING_EFFORTS,
                        default=defaults.action_agent.reasoning_effort)
    parser.add_argument("--max-turns", "--action-turn-limit", dest="max_turns", type=int,
                        default=defaults.action_turn_limit)
    parser.add_argument("--review-turn-limit", type=int, default=defaults.review_turn_limit)
    parser.add_argument("--episode-limit", type=int, default=defaults.episode_limit)
    parser.add_argument("--reasoning-summary", action="store_true", default=reasoning_summary)


def session_config_from_args(args):
    model = args.model or (AgentConfig().model if args.policy == "codex" else "")
    agent = AgentConfig(backend=args.policy, model=model, reasoning_effort=args.reasoning_effort,
                        reasoning_summary=args.reasoning_summary)
    # One current UI selection initializes two independently configurable roles.
    from dataclasses import replace
    return SessionConfig(action_agent=agent, review_agent=replace(agent),
                         action_turn_limit=args.max_turns, review_turn_limit=args.review_turn_limit,
                         episode_limit=args.episode_limit)


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


async def run_episode(config: SessionConfig,
                      crawl_path: Path = DEFAULT_CRAWL_PATH,
                      manual_path: Path | None = None, *, sink=None, run_dir: Path | None = None):
    """Run one Start session, reviewing confirmed deaths before subsequent games."""
    from .session import run_session
    directory = run_dir if run_dir is not None else new_run_directory()
    if manual_path is None:
        manual_path = Path(crawl_path).resolve().parent.parent / "docs" / "crawl_manual.rst"

    def policy_factory(agent_config, *, profile, prompts=None):
        initial_prompt = read_only_prompt("review_agent/initial_prompt", single_line=True) if profile == "review" else prompts.initial_prompt
        return create_policy(agent_config, initial_prompt=initial_prompt, profile=profile, prompts=prompts)

    return await run_session(directory=directory,
        game_factory=lambda path: create_game(crawl_path, save_dir=path),
        policy_factory=policy_factory, manual_path=manual_path, config=config, sink=sink)


async def run(config: SessionConfig,
              crawl_path: Path = DEFAULT_CRAWL_PATH,
              manual_path: Path | None = None):
    print(f"Game: DCSS; policy: {config.action_agent.backend}", flush=True)
    directory = new_run_directory()
    print(f"Log: {directory / 'model.jsonl'}", flush=True)
    result = await run_episode(config, crawl_path, manual_path, run_dir=directory)
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
    add_session_arguments(parser)
    args = parser.parse_args()
    try:
        config = session_config_from_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    asyncio.run(run(config, args.crawl_path, args.manual_path))
