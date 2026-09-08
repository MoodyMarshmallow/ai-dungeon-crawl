from contextlib import contextmanager
from contextvars import ContextVar
import logging
from typing import Callable


_sink: ContextVar[Callable | None] = ContextVar("episode_events", default=None)
_recorder: ContextVar[Callable | None] = ContextVar("episode_recorder", default=None)


@contextmanager
def record_events(recorder: Callable):
    """Install a durable sink; write failures propagate instead of losing logs silently."""
    token = _recorder.set(recorder)
    try:
        yield
    finally:
        _recorder.reset(token)


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
    return _sink.get() is not None or _recorder.get() is not None


def emit(event: str, **data):
    """Record public events before publishing to a best-effort display observer."""
    recorder = _recorder.get()
    if recorder is not None:
        recorder(event, data)
    _publish(event, data)


def emit_output(text: str, preview: str, stream: str):
    """Archive complete shell output while keeping the display/model preview bounded."""
    recorder = _recorder.get()
    if recorder is not None and text:
        recorder("execution.output", {"text": text, "stream": stream})
    if preview:
        _publish("execution.output", {"text": preview})


def _publish(event: str, data: dict):
    sink = _sink.get()
    if sink is not None:
        try:
            sink(event, data)
        except Exception:
            logging.getLogger(__name__).exception("Episode observer failed")
