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
Observation = namedtuple("Observation", "id screen ended")
_current = None


def send(message):
    _writer.write(json.dumps(message) + "\n")
    _writer.flush()


def observe():
    return _current


async def press(key):
    global _current
    if not isinstance(key, str) or len(key) > 16:
        raise ValueError("press expects one printable character or a named key")
    send({"type": "press", "key": key})
    response = json.loads(_reader.readline())
    if "error" in response:
        raise ValueError(response["error"])
    _current = Observation(**response["observation"])
    return _current


class Output(io.TextIOBase):
    def __init__(self, limit):
        self.remaining = limit
        self.truncated = False

    def write(self, text):
        visible = text[:self.remaining]
        self.remaining -= len(visible)
        if visible:
            send({"type": "output", "text": visible})
        if len(visible) < len(text) and not self.truncated:
            self.truncated = True
            send({"type": "truncated"})
        return len(text)


async def evaluate(code, namespace):
    compiled = compile(code, "<model-turn>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    result = eval(compiled, namespace)
    if inspect.isawaitable(result):
        await result


def main():
    global _current
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    namespace = {"__name__": "__repl__"}
    send({"type": "ready"})
    for line in _reader:
        request = json.loads(line)
        _current = Observation(**request["observation"])
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
