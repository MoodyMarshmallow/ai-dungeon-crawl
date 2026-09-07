"""Private JSON-line worker. Only launch through PythonRepl's OS sandbox."""

import ast
import asyncio
from collections import namedtuple
from contextlib import redirect_stderr, redirect_stdout
import inspect
import io
import json
import resource
import sys


# No secrets or game process live here. The parent validates all input requests.
_reader, _writer = sys.stdin, sys.stdout
GameObservation = namedtuple("GameObservation", "id screen ended")
_current = None


def send(message):
    """Flush one JSON message to the parent using the original stdout stream.

    Keep protocol messages separate from redirected script output such as print().
    """
    _writer.write(json.dumps(message) + "\n")
    _writer.flush()


def observe():
    """Return the latest observation without sending a key or polling the game."""
    return _current


async def press(key):
    """Ask the parent to apply one key and return the resulting observation.

    Wait for its reply before updating observe(). The parent validates the key
    and enforces game budgets; a validation error becomes a script ValueError.
    This helper uses blocking pipe reads, so it is intended for sequential use,
    not concurrent calls within a script.
    """
    global _current
    if not isinstance(key, str) or len(key) > 16:
        raise ValueError("press expects one printable character or a named key")
    send({"type": "press", "key": key})
    response = json.loads(_reader.readline())
    if "error" in response:
        raise ValueError(response["error"])
    _current = GameObservation(**response["observation"])
    return _current


class Output(io.TextIOBase):
    def __init__(self, limit):
        """Create a script-output sink with a shared character budget."""
        self.remaining = limit
        self.truncated = False

    def write(self, text):
        """Forward text within the budget and report truncation at most once.

        Return the full input length, as if all text was consumed, even when
        excess characters are discarded. Used for both stdout and stderr.
        """
        visible = text[:self.remaining]
        self.remaining -= len(visible)
        if visible:
            send({"type": "output", "text": visible})
        if len(visible) < len(text) and not self.truncated:
            self.truncated = True
            send({"type": "truncated"})
        return len(text)


async def evaluate(code, namespace):
    """Execute Python in the supplied namespace, supporting top-level await.

    Definitions and assignments stay in namespace for later scripts. Exceptions
    propagate to main(), which converts them into bounded error feedback.
    """
    compiled = compile(code, "<agent-turn>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    result = eval(compiled, namespace)
    if inspect.isawaitable(result):
        await result


def main():
    """Serve script requests until the parent closes stdin or stops the worker.

    Apply process resource limits and retain one namespace across requests.
    Refresh the game helpers and observation for each script, capture its output,
    and report completion or an error. Each script gets a new asyncio event loop;
    background async tasks do not survive between scripts.
    """
    global _current
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    namespace = {"__name__": "__repl__"}
    send({"type": "ready"})
    for line in _reader:
        request = json.loads(line)
        _current = GameObservation(**request["observation"])
        namespace.update(press=press, observe=observe)
        output = Output(request["max_output_chars"])
        error = None
        with redirect_stdout(output), redirect_stderr(output):
            try:
                asyncio.run(evaluate(request["code"], namespace))
            except BaseException as exc:
                error = (type(exc).__name__ + ": " + str(exc))[:2000]
        send({"type": "done", "error": error})


if __name__ == "__main__":
    main()
