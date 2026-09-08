from contextlib import contextmanager
from contextvars import ContextVar
import logging
from typing import Callable


_sink: ContextVar[Callable | None] = ContextVar("episode_events", default=None)


@contextmanager
def observe_events(sink: Callable):
    """Observe this async task's episode without changing its return values.

    The sink receives (kind, data), must be fast and synchronous, and must not
    perform game actions. Child tasks inherit it; unrelated episodes do not.
    """
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def events_enabled():
    return _sink.get() is not None


def emit(event: str, **data):
    """Publish display-only data; a broken observer must not stop gameplay."""
    sink = _sink.get()
    if sink is not None:
        try:
            sink(event, data)
        except Exception:
            logging.getLogger(__name__).exception("Episode observer failed")
