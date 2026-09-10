from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import json

from .contracts import GameObservation, ScreenStyle
from .game.observation_json import observation_data


_journal = ContextVar("observation_journal", default=None)


@contextmanager
def observation_journal(path):
    """Bind the broker to this run's game-state file, never its model trajectory."""
    token = _journal.set(path)
    try:
        yield
    finally:
        _journal.reset(token)


def has_observation_journal():
    return _journal.get() is not None


def read_observations(*, since=None, until=None, limit=None):
    """Read the latest matching screens, oldest first, using inclusive DCSS ticks.

    No filters returns one screen; time filters default to 20. At most 100
    screens are returned. Unknown game times match only unfiltered queries.
    Sequence numbers distinguish separate keypresses at the same game tick.
    Only screen data and game-time metadata leave this module, never raw logs.
    """
    for name, value in (("since", since), ("until", until)):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"{name} must be a nonnegative integer game tick")
    if since is not None and until is not None and since > until:
        raise ValueError("since must not exceed until")
    if limit is None:
        limit = 20 if since is not None or until is not None else 1
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    path = _journal.get()
    if path is None:
        raise ValueError("No observation journal is available")
    selected = deque(maxlen=limit)
    with path.open(encoding="utf-8") as source:
        for line in source:
            # A reader must never expose an unfinished final record.
            if not line.endswith("\n"):
                break
            record = json.loads(line)
            if record["event"] not in {"game.observation", "game.step"}:
                continue
            tick = record["timestamp"]
            if since is not None and (tick is None or tick < since):
                continue
            if until is not None and (tick is None or tick > until):
                continue
            selected.append(record)
    result = []
    for record in selected:
        data = record["data"]
        raw = data["observation"] if record["event"] == "game.step" else data
        observation = GameObservation(**{
            **raw, "styles": tuple(ScreenStyle(**style) for style in raw.get("styles", ())),
            "cursor": tuple(raw["cursor"]) if raw.get("cursor") is not None else None,
        })
        result.append({**observation_data(observation), "timestamp": record["timestamp"],
                       "sequence": record["sequence"]})
    return result
