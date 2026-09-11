from dataclasses import dataclass
from typing import Literal, Optional, Protocol, Tuple


@dataclass(frozen=True)
class ScreenStyle:
    """A contiguous run of visual terminal attributes on one screen row.

    Coordinates are zero-based. Styles describe rendering only; they do not
    carry gameplay meaning.
    """

    row: int
    col: int
    length: int
    fg: str = "default"
    bg: str = "default"
    bold: bool = False
    italics: bool = False
    underline: bool = False
    reverse: bool = False
    blink: bool = False


@dataclass(frozen=True)
class GameObservation:
    """A complete rendered screen at an input boundary, or after game exit.

    IDs increase within a session, including when the screen is unchanged.
    `screen` preserves whitespace; it is never a raw terminal-output chunk.
    A waiting observation may be a menu/prompt, not necessarily a game turn.
    `ended` means the process ended, not necessarily that the player died.
    `width` and `height` are the rendered screen bounds. `styles` contains
    visual runs only when attributes differ from their defaults, and `cursor`
    is a zero-based (row, column) position when known. None of this metadata
    carries gameplay meaning.
    """

    id: int
    screen: str
    ended: bool = False
    width: int = 0
    height: int = 0
    styles: Tuple[ScreenStyle, ...] = ()
    cursor: Optional[Tuple[int, int]] = None
    outcome: Optional[Literal["death", "win", "quit"]] = None


@dataclass(frozen=True)
class GameAction:
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
            raise ValueError("GameAction must contain one printable character or a named key")


@dataclass(frozen=True)
class GameStep:
    """One completed game action with its before and after observations."""

    before: GameObservation
    action: GameAction
    after: GameObservation


@dataclass(frozen=True)
class GameEpisodeResult:
    """The outcome and recorded progress of one game episode."""

    stop_reason: Literal["game_exited", "turn_limit", "death", "win", "quit"]
    final_observation: GameObservation
    steps: Tuple[GameStep, ...]
    turns: Tuple["AgentTurnRecord", ...]


def validate_source(code: str) -> None:
    """Raise ValueError unless source is text of at most 64 KiB in UTF-8.

    This checks input type and size, not syntax or execution safety.
    Empty source is allowed; syntax errors are handled during execution.
    """
    if not isinstance(code, str) or len(code.encode("utf-8")) > 65536:
        raise ValueError("Code must be text of at most 64 KiB")


def validate_timeout_ms(value: Optional[int]) -> None:
    """Allow the configured default or a bounded integer execution budget."""
    if value is not None and (type(value) is not int or not 1 <= value <= 180000):
        raise ValueError("timeout_ms must be an integer from 1 to 180000, or None")


class StopExecution(Exception):
    """Abort the current submitted script after a harness limit is reached."""


@dataclass(frozen=True)
class AgentTurn:
    """The accepted script output of one agent decision cycle.

    Submitting this code ends the agent turn; the harness executes it afterward.
    Execution may cause zero or many game actions, and its feedback starts the
    next agent turn. This value is not a transcript of individual model turns.

    model_requests counts logical model invocations including output repair (not hidden
    transport retries). Zero means no model requests; None means the backend
    does not expose a reliable count. One tool submission per agent turn.
    """

    code: str
    model_requests: Optional[int] = 0
    timeout_ms: Optional[int] = None

    def __post_init__(self) -> None:
        if self.model_requests is not None and self.model_requests < 0:
            raise ValueError("model_requests cannot be negative")
        validate_source(self.code)
        validate_timeout_ms(self.timeout_ms)


@dataclass(frozen=True)
class ExecutionResult:
    """Feedback for the next agent turn, including partial output on failure."""

    observation: GameObservation
    output: str = ""
    error: Optional[str] = None
    status: Literal["ok", "error", "timeout", "stopped"] = "ok"
    output_truncated: bool = False


@dataclass(frozen=True)
class AgentTurnRecord:
    """An agent turn's submission, execution feedback, and completed game steps."""

    id: int
    turn: AgentTurn
    execution: ExecutionResult
    steps: Tuple[GameStep, ...]


class GameSession(Protocol):
    """Owns one isolated game process, its terminal and its save directory.

    Calls are sequential: start once, step zero or more times, close always.
    start/step return only when input is accepted or the process has ended.
    A real adapter must document readiness detection and enforce timeouts.
    Timeouts/crashes raise exceptions; they are not successful observations.
    """

    async def start(self) -> GameObservation: ...

    async def step(self, action: GameAction) -> GameObservation:
        """Send once, then observe. Never automatically retry a sent action."""
        ...

    async def close(self) -> None:
        """Release the process and terminal, including after failed startup.

        Must be idempotent. Preserve save files; this is not a save command.
        """
        ...


class Policy(Protocol):
    """Run an agent decision cycle using game feedback and prior agent turns.

    Owns prompt construction, context selection and bounded model retries.
    A cycle may involve multiple model turns and ends with one accepted script.
    Has no game or terminal handle: the runner dispatches execute_shell after
    request_turn returns. History includes script output, errors and steps.
    Model clients are configured/closed by the caller, outside the episode.
    """

    async def request_turn(
        self, observation: GameObservation, history: Tuple[AgentTurnRecord, ...]
    ) -> AgentTurn:
        """Return the accepted script submission that ends this agent turn."""
        ...
