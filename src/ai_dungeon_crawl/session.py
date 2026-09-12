"""Fresh action/review contexts with artifacts scoped to one Start session."""
import asyncio
import json
import os
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from pathlib import Path

from .contracts import AgentTurnRecord, GameObservation, StopExecution
from .config import SessionConfig
from .episode import EpisodeRunner
from .events import emit, observe_events
from .run_log import episode_log
from .shell.terminal import ShellTerminal
from .agent.prompts import base_prompts, load_prompts


async def run_review(policy, terminal, *, max_turns):
    observation = GameObservation(0, "")
    turns = []

    async def no_game(action):
        raise StopExecution("No live game is available during review")

    try:
        for turn_id in range(max_turns):
            emit("turn.started", id=turn_id)
            turn = await policy.request_turn(tuple(turns))
            emit("execution.submitted", id=turn_id, code=turn.code,
                 model_requests=turn.model_requests, timeout_ms=turn.timeout_ms)
            try:
                execution = await terminal.execute_shell(
                    turn.code, observation, no_game, timeout_ms=turn.timeout_ms)
            except BaseException as exc:
                emit("execution.interrupted", id=turn_id,
                     error="cancelled" if isinstance(exc, asyncio.CancelledError) else type(exc).__name__)
                raise
            turns.append(AgentTurnRecord(turn_id, turn, execution, ()))
            emit("execution.finished", id=turn_id, **asdict(execution))
        if turns:
            await terminal.publish_artifacts()
        return tuple(turns)
    finally:
        await terminal.close()


async def run_session(*, directory: Path, game_factory, policy_factory,
                      config: SessionConfig, manual_path=None, sink=None):
    directory = Path(directory)
    artifacts = directory / "artifacts"
    # A Start owns a new directory: never reuse or clear a previous run's state.
    if artifacts.exists() or artifacts.is_symlink():
        raise FileExistsError(artifacts)
    next_turn_id = 0
    phase_ids = {}
    terminal_emitted = False

    @contextmanager
    def logged_phase(path, *, initial_game_time=None):
        nonlocal terminal_emitted
        with episode_log(path, initial_game_time=initial_game_time):
            try:
                yield
            except asyncio.CancelledError:
                terminal_emitted = True
                emit("episode.finished", status="stopped", stop_reason="cancelled")
                raise
            except Exception as exc:
                terminal_emitted = True
                emit("episode.finished", status="error", error=type(exc).__name__)
                raise

    def relay(event, data):
        nonlocal next_turn_id
        if event == "turn.started":
            phase_ids[data["id"]] = next_turn_id
            next_turn_id += 1
        if event in ("turn.started", "execution.submitted", "execution.finished", "execution.interrupted"):
            data = {**data, "id": phase_ids[data["id"]]}
        if sink:
            sink(event, data)

    with observe_events(relay) if sink else nullcontext():
        try:
            for number in range(1, config.episode_limit + 1):
                # Keep the established first-episode log location for CLI consumers.
                action_dir = directory if number == 1 else directory / f"episode-{number:03d}" / "action"
                phase_ids.clear()
                with logged_phase(action_dir):
                    if number == 1:
                        emit("episode.started", config=config.event_data(), log_path=str(action_dir / "model.jsonl"))
                    emit("mode.changed", mode="action", episode=number)
                    emit("phase.started", mode="action", episode=number, config=asdict(config.action_agent))
                    prompts = (load_prompts(artifacts / "snapshot" / "prompts" / "agent_editable" / "action")
                               if (artifacts / "snapshot").exists() else base_prompts())
                    policy = policy_factory(config.action_agent, profile="action", prompts=prompts)
                    reference = (policy.prompt_reference() if hasattr(policy, "prompt_reference")
                                 else {"prompts": prompts.effective_text()})
                    reference["session_config"] = config.event_data()
                    reference["shell_config"] = {"default_timeout_ms": 5000, "max_output_chars": 32768}
                    fd = os.open(action_dir / "action-prompts.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as prompt_log:
                        json.dump(reference, prompt_log, ensure_ascii=False, indent=2)
                    emit("action.prompts", **reference)
                    terminal = ShellTerminal(manual_path=manual_path, artifacts_path=artifacts)
                    game = game_factory(action_dir)
                    result = await EpisodeRunner(game, policy, terminal).run(max_turns=config.action_turn_limit)
                    emit("phase.finished", mode="action", episode=number, stop_reason=result.stop_reason)
                    if result.stop_reason != "death":
                        emit("episode.finished", status="completed", stop_reason=result.stop_reason)
                        terminal_emitted = True
                if result.stop_reason != "death":
                    break
                # The action journals are closed and fsynced before review snapshots them.
                final_tick = None
                with (action_dir / "game.jsonl").open() as journal:
                    for line in journal:
                        tick = json.loads(line).get("timestamp")
                        if tick is not None:
                            final_tick = tick
                phase_ids.clear()
                with logged_phase(directory / f"episode-{number:03d}" / "review",
                                  initial_game_time=final_tick):
                    emit("mode.changed", mode="review", episode=number)
                    emit("phase.started", mode="review", episode=number, config=asdict(config.review_agent))
                    policy = policy_factory(config.review_agent, profile="review")
                    terminal = ShellTerminal(profile="review", artifacts_path=artifacts,
                                             review_log_path=action_dir, manual_path=manual_path)
                    await run_review(policy, terminal, max_turns=config.review_turn_limit)
                    emit("phase.finished", mode="review", episode=number, stop_reason="turn_limit")
                    if number == config.episode_limit:
                        emit("episode.finished", status="completed", stop_reason=result.stop_reason)
                        terminal_emitted = True
            return result
        except asyncio.CancelledError:
            if not terminal_emitted:
                emit("episode.finished", status="stopped", stop_reason="cancelled")
            raise
        except Exception as exc:
            if not terminal_emitted:
                emit("episode.finished", status="error", error=type(exc).__name__)
            raise
