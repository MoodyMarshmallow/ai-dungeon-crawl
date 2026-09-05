"""Data and internal interfaces shared by the game and agent loop."""

from dataclasses import dataclass
from typing import Literal, Optional, Protocol, Tuple


@dataclass(frozen=True)
class Observation:
    """A complete rendered screen at an input boundary, or after game exit.

    IDs increase within a session, including when the screen is unchanged.
    `screen` preserves whitespace; it is never a raw terminal-output chunk.
    A waiting observation may be a menu/prompt, not necessarily a game turn.
    `ended` means the process ended, not necessarily that the player died.
    """

    id: int
    screen: str
    ended: bool = False


@dataclass(frozen=True)
class Action:
    """One printable character or named key; no macros or arbitrary bytes.

    Session implementations translate named keys to terminal input. This
    validates the input format, not whether the move is legal in the game.
    """

    key: str

    def __post_init__(self) -> None:
        named = {"ENTER", "ESC", "TAB", "BACKSPACE", "UP", "DOWN", "LEFT", "RIGHT"}
        control = len(self.key) == 6 and self.key.startswith("CTRL+") and "A" <= self.key[-1] <= "Z"
        printable = len(self.key) == 1 and self.key.isprintable()
        if not (printable or control or self.key in named):
            raise ValueError("Action must contain one printable character or a named key")


@dataclass(frozen=True)
class Step:
    before: Observation
    action: Action
    after: Observation


@dataclass(frozen=True)
class EpisodeResult:
    stop_reason: Literal["game_exited", "step_limit", "turn_limit", "repl_timeout"]
    final_observation: Observation
    steps: Tuple[Step, ...]
    turns: Tuple["TurnRecord", ...]


@dataclass(frozen=True)
class ModelTurn:
    """One execute_python submission; it may cause zero or many game actions.

    model_requests counts model calls including output repair (not hidden
    transport retries). Zero denotes a scripted policy; None means the backend
    does not expose a reliable count. One tool submission per model turn.
    """

    code: str
    model_requests: Optional[int] = 0

    def __post_init__(self) -> None:
        if self.model_requests is not None and self.model_requests < 0:
            raise ValueError("model_requests cannot be negative")
        if not isinstance(self.code, str) or len(self.code.encode("utf-8")) > 65536:
            raise ValueError("Code must be text of at most 64 KiB")


@dataclass(frozen=True)
class ExecutionResult:
    """Feedback for the next model turn, including partial output on failure."""

    observation: Observation
    output: str = ""
    error: Optional[str] = None
    status: Literal["ok", "error", "timeout", "stopped"] = "ok"
    output_truncated: bool = False


@dataclass(frozen=True)
class TurnRecord:
    id: int
    turn: ModelTurn
    execution: ExecutionResult
    steps: Tuple[Step, ...]


class GameSession(Protocol):
    """Owns one isolated game process, its terminal and its save directory.

    Calls are sequential: start once, step zero or more times, close always.
    start/step return only when input is accepted or the process has ended.
    A real adapter must document readiness detection and enforce timeouts.
    Timeouts/crashes raise exceptions; they are not successful observations.
    """

    async def start(self) -> Observation: ...

    async def step(self, action: Action) -> Observation:
        """Send once, then observe. Never automatically retry a sent action."""
        ...

    async def close(self) -> None:
        """Release the process and terminal, including after failed startup.

        Must be idempotent. Preserve save files; this is not a save command.
        """
        ...


class Policy(Protocol):
    """Requests one Python submission using observations and prior turn records.

    Owns prompt construction, context selection and bounded model retries.
    Has no game or REPL handle: the runner dispatches execute_python after
    request_turn returns. History includes script output, errors and steps.
    Model clients are configured/closed by the caller, outside the episode.
    """

    async def request_turn(
        self, observation: Observation, history: Tuple[TurnRecord, ...]
    ) -> ModelTurn: ...
