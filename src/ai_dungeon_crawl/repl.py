"""Persistent Python execution, with all game effects mediated by the parent."""

import asyncio
from dataclasses import asdict
import json
import math
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Awaitable, Callable

from .contracts import Action, ExecutionResult, ModelTurn, Observation


class StopExecution(Exception):
    """The episode no longer accepts game inputs (exit or action budget)."""


class _WorkerTimeout(Exception):
    pass


class PythonRepl:
    """One sandboxed worker/namespace per episode; never run concurrently.

    macOS only for now. Unsupported platforms or unavailable sandboxing fail
    closed. No unsandboxed fallback. The timeout covers worker execution and
    IPC, not time spent awaiting the game's own independently bounded I/O.
    """

    def __init__(self, *, timeout_seconds: float = 5, max_output_chars: int = 8000):
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if not 0 <= max_output_chars <= 65536:
            raise ValueError("max_output_chars must be between zero and 65536")
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars
        self._process = None
        self._directory = None
        self._closed = False
        self._busy = False

    async def _start(self):
        if self._closed:
            raise RuntimeError("REPL is closed; use a fresh REPL for each episode")
        if self._process is not None:
            return
        sandbox = shutil.which("sandbox-exec")
        if sys.platform != "darwin" or sandbox is None:
            raise RuntimeError("PythonRepl requires macOS sandbox-exec; no unsafe fallback")
        executable = str(Path(sys.executable).resolve())
        runtime = str(Path(sys.base_prefix).resolve())
        worker = str(Path(__file__).with_name("_repl_worker.py").resolve())
        self._directory = tempfile.TemporaryDirectory(prefix="ai-dungeon-repl-")
        directory = str(Path(self._directory.name).resolve())
        # JSON quoting safely quotes these paths in the sandbox profile too.
        def literal(path):
            return "(literal " + json.dumps(path) + ")"

        def subpath(path):
            return "(subpath " + json.dumps(path) + ")"

        profile = "\n".join([
            "(version 1)", "(deny default)", "(allow file-read-metadata)",
            "(allow file-read-data " + " ".join([
                literal("/"), literal(directory), literal(executable), literal(worker),
                literal("/dev/null"), literal("/dev/urandom"), subpath(runtime),
                subpath("/System"), subpath("/usr/lib"),
            ]) + ")",
            "(allow process-exec " + literal(executable) + " " + subpath(runtime) + ")",
            "(allow sysctl-read)", "(allow mach-lookup)",
        ])
        self._process = await asyncio.create_subprocess_exec(
            sandbox, "-p", profile, executable, "-I", "-S", "-B", "-u", worker,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=directory,
            env={"LANG": "en_US.UTF-8", "PATH": "/usr/bin:/bin"}, limit=131072,
        )
        try:
            message = await asyncio.wait_for(self._read(), timeout=10)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("REPL sandbox startup timed out") from exc
        if message != {"type": "ready"}:
            raise RuntimeError("REPL worker did not initialize")

    async def _read(self):
        line = await self._process.stdout.readline()
        if not line:
            error = await self._process.stderr.read(2000)
            raise RuntimeError("REPL worker exited: " + error.decode(errors="replace"))
        message = json.loads(line)
        if not isinstance(message, dict):
            raise RuntimeError("Invalid REPL worker response")
        return message

    async def _send(self, message):
        self._process.stdin.write((json.dumps(message) + "\n").encode())
        await self._process.stdin.drain()

    async def _bounded(self, operation, remaining):
        try:
            return await asyncio.wait_for(operation, timeout=max(0, remaining))
        except asyncio.TimeoutError as exc:
            raise _WorkerTimeout() from exc

    async def execute_python(
        self, code: str, observation: Observation,
        press: Callable[[Action], Awaitable[Observation]],
    ) -> ExecutionResult:
        """Execute once; return output/errors and the latest real screen.

        Python errors are feedback, not rollback. Game/transport failures raise
        and close the worker. Timeouts stop the worker and end the episode.
        """
        ModelTurn(code)
        if self._busy:
            raise RuntimeError("Concurrent REPL executions are not supported")
        self._busy = True
        output = ""
        truncated = False
        loop = asyncio.get_running_loop()
        try:
            await self._start()
            deadline = loop.time() + self.timeout_seconds
            await self._bounded(self._send({
                "code": code, "observation": asdict(observation),
                "max_output_chars": self.max_output_chars,
            }), self.timeout_seconds)
            while True:
                if loop.time() >= deadline:
                    raise _WorkerTimeout()
                message = await self._bounded(self._read(), deadline - loop.time())
                kind = message.get("type")
                if kind == "output":
                    text = message["text"]
                    if not isinstance(text, str):
                        raise RuntimeError("Invalid REPL output")
                    space = self.max_output_chars - len(output)
                    output += text[:space]
                    truncated |= len(text) > space
                elif kind == "truncated":
                    truncated = True
                elif kind == "press":
                    try:
                        key = message.get("key")
                        if not isinstance(key, str):
                            raise ValueError("Action key must be text")
                        action = Action(key)
                    except ValueError as exc:
                        await self._bounded(self._send({"error": str(exc)}), deadline - loop.time())
                        continue
                    started = loop.time()
                    # Never turn game failures into catchable script errors:
                    # an input may already have been sent. Never retry it.
                    observation = await press(action)
                    deadline += loop.time() - started
                    await self._bounded(self._send({"observation": asdict(observation)}),
                                        deadline - loop.time())
                elif kind == "done":
                    error = message.get("error")
                    if error is not None and not isinstance(error, str):
                        raise RuntimeError("Invalid REPL error response")
                    return ExecutionResult(observation, output, error,
                                           "error" if error else "ok", truncated)
                else:
                    raise RuntimeError("Unknown REPL worker response")
        except StopExecution as exc:
            await self.close()
            return ExecutionResult(observation, output, str(exc), "stopped", truncated)
        except _WorkerTimeout:
            await self.close()
            return ExecutionResult(observation, output, "REPL execution timed out",
                                   "timeout", truncated)
        except BaseException:
            await self.close()
            raise
        finally:
            self._busy = False

    async def close(self) -> None:
        self._closed = True
        try:
            if self._process is not None:
                if self._process.returncode is None:
                    try:
                        self._process.kill()
                    except ProcessLookupError:
                        pass
                # Drain pipes after killing, including malformed/flooded output.
                await self._process.communicate()
        finally:
            if self._directory is not None:
                self._directory.cleanup()
