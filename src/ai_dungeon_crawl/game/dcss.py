import asyncio
from dataclasses import replace
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import secrets
import signal
import socket
import struct
import tempfile
import termios
from typing import Callable, Sequence

from ..contracts import GameAction, GameObservation
from .terminal import BoundaryDecoder, TerminalScreen


def key_bytes(action: GameAction) -> bytes:
    keys = {"ENTER": b"\r", "ESC": b"\x1b", "TAB": b"\t", "BACKSPACE": b"\x7f",
            "UP": b"\x1bOA", "DOWN": b"\x1bOB", "RIGHT": b"\x1bOC", "LEFT": b"\x1bOD"}
    if action.key in keys:
        return keys[action.key]
    if action.key.startswith("CTRL+"):
        return bytes([ord(action.key[-1]) - ord("A") + 1])
    return action.key.encode("utf-8")


class DCSSGameSession:
    """One game: a real terminal plus a separate spectator tile stream.

    Requires the patched non-headless WebTiles build. Its marker follows the
    terminal refresh/tile flush and precedes blocking input. Missing markers
    fail with a timeout; idle time is never treated as readiness.
    """
    def __init__(self, executable: Path | str, *, cwd: Path | str | None = None,
                 save_dir: Path | str | None = None, width: int = 100, height: int = 30,
                 readiness_timeout: float = 30, extra_args: Sequence[str] = (),
                 on_tiles: Callable[[tuple[dict, ...]], None] | None = None,
                 on_score: Callable[[int | None, int, bool, int | None], None] | None = None) -> None:
        if not (80 <= width <= 300 and 24 <= height <= 150):
            raise ValueError("DCSS terminal bounds are 80–300 columns and 24–150 rows")
        if not math.isfinite(readiness_timeout) or readiness_timeout <= 0:
            raise ValueError("readiness_timeout must be finite and positive")
        self.executable = Path(executable).resolve()
        self.cwd = Path(cwd).resolve() if cwd else self.executable.parent
        self.save_dir = (Path(save_dir).resolve() if save_dir else
                         Path.cwd() / "runs" / secrets.token_hex(12))
        self.width, self.height = width, height
        self.readiness_timeout = readiness_timeout
        self.extra_args = tuple(extra_args)
        self.on_tiles = on_tiles
        self.on_score = on_score
        self._terminal = TerminalScreen(width, height)
        self._nonce = secrets.token_hex(24)
        self._decoder = BoundaryDecoder(
            f"\x1b]777;dcss-input;{self._nonce}\x07".encode(), self._terminal, self._boundary)
        self._queue: asyncio.Queue[GameObservation | Exception] = asyncio.Queue()
        self._process = None
        self._master = None
        self._socket = None
        self._socket_dir = None
        self._exit_task = None
        self._log = None
        self._tile_buffer = b""
        self._id = 0
        self._started = False
        self._closed = False
        self._failed = False
        self._ended = False
        self.outcome: str | None = None
        self._final_score_received = False

    async def start(self) -> GameObservation:
        if self._started or self._closed:
            raise RuntimeError("Session can only be started once")
        self._started = True
        self.save_dir.mkdir(parents=True, exist_ok=True)
        # Unix socket paths have a small fixed maximum; keep them out of long
        # project/run paths. Only these transient files are removed by close.
        self._socket_dir = tempfile.TemporaryDirectory(prefix="dcss-", dir="/tmp")
        game_socket = str(Path(self._socket_dir.name) / "game")
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._socket.setblocking(False)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 212992)
        self._socket.bind(str(Path(self._socket_dir.name) / "observer"))
        self._master, slave = os.openpty()
        os.set_blocking(self._master, False)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", self.height, self.width, 0, 0))
        env = dict(PATH=os.defpath, HOME=str(self.save_dir),
                   TERM="xterm-256color", LANG="en_US.UTF-8",
                   LC_ALL="en_US.UTF-8", COLUMNS=str(self.width), LINES=str(self.height),
                   DCSS_INPUT_MARKER=self._nonce, DCSS_HARNESS_NO_EXIT="1",
                   DCSS_HARNESS_SCORE="1", DCSS_HARNESS_OUTCOME="1")
        self._log = (self.save_dir / "process.log").open("ab")
        # DCSS rejects duplicate command-line options. Fill in only missing
        # character options, and leave weapon selection to the agent.
        supplied = {arg.lstrip("-").lower() for arg in self.extra_args if arg.startswith("-")}
        character_args = [arg for option, value in (
            ("name", "Agent"), ("species", "Minotaur"), ("background", "Berserker")
        ) if option not in supplied for arg in (f"-{option}", value)]
        args = [str(self.executable), "-webtiles-socket", game_socket, "-await-connection",
                "-rc", "/dev/null", "-dir", str(self.save_dir),
                *character_args,
                "-extra-opt-last", f"save_dir = {self.save_dir}",
                "-extra-opt-last", f"macro_dir = {self.save_dir / 'macros'}",
                "-extra-opt-last", f"morgue_dir = {self.save_dir / 'morgue'}",
                "-extra-opt-last", "restart_after_game = false",
                *self.extra_args]
        try:
            self._process = await asyncio.create_subprocess_exec(
                *args, stdin=slave, stdout=slave, stderr=self._log,
                cwd=self.cwd, env=env, start_new_session=True)
        finally:
            os.close(slave)
        loop = asyncio.get_running_loop()
        loop.add_reader(self._master, self._read_terminal)
        loop.add_reader(self._socket.fileno(), self._read_tiles)
        self._exit_task = asyncio.create_task(self._watch_exit())
        try:
            async with asyncio.timeout(self.readiness_timeout):
                while not Path(game_socket).exists():
                    if self._process.returncode is not None:
                        return await self._next()
                    await asyncio.sleep(0.01)
                # The process waits for this before sending its initialization.
                self._socket.sendto(b'{"msg":"attach","primary":false}', game_socket)
                return await self._next()
        except TimeoutError as exc:
            self._failed = True
            raise TimeoutError("DCSS startup did not reach an input boundary; use the patched "
                               "WebTiles binary and check process.log") from exc

    async def step(self, action: GameAction) -> GameObservation:
        if not self._started or self._closed or self._failed or self._ended:
            raise RuntimeError("Session is not accepting input")
        if not self._queue.empty():
            self._failed = True
            raise RuntimeError("Unexpected input boundary before action")
        try:
            data = key_bytes(action)
            if os.write(self._master, data) != len(data):
                raise RuntimeError("Partial game input write; action cannot be retried")
            return await self._next()
        except BaseException:
            self._failed = True
            raise

    async def _next(self) -> GameObservation:
        try:
            result = await asyncio.wait_for(self._queue.get(), self.readiness_timeout)
        except TimeoutError as exc:
            self._failed = True
            raise TimeoutError("DCSS did not reach the next input boundary; input is never retried") from exc
        if isinstance(result, Exception):
            self._failed = True
            raise result
        self._ended = result.ended
        return result

    def _boundary(self) -> None:
        self._read_tiles()
        self._id += 1
        self._queue.put_nowait(replace(
            self._terminal.observation(self._id, ended=self.outcome is not None),
            outcome=self.outcome))

    def _read_terminal(self) -> None:
        if self._master is None:
            return
        while True:
            try:
                data = os.read(self._master, 65536)
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno != errno.EIO:
                    self._queue.put_nowait(exc)
                data = b""
            if not data:
                asyncio.get_running_loop().remove_reader(self._master)
                return
            self._decoder.feed(data)

    def _read_tiles(self) -> None:
        if self._socket is None:
            return
        messages = []
        scores = []
        try:
            while True:
                try:
                    self._tile_buffer += self._socket.recv(131072)
                except BlockingIOError:
                    break
                if len(self._tile_buffer) > 8 * 1024 * 1024:
                    raise ValueError("DCSS tile message exceeds 8 MiB")
                while b"\n" in self._tile_buffer:
                    line, self._tile_buffer = self._tile_buffer.split(b"\n", 1)
                    if not line or line.startswith(b"*"):
                        continue  # Server control metadata, not client rendering.
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise ValueError("Invalid DCSS tile message")
                    if message.get("msg") == "harness_outcome":
                        outcome = message.get("outcome")
                        if outcome not in ("death", "win", "quit"):
                            raise ValueError("Invalid DCSS harness outcome")
                        if self.outcome is not None and self.outcome != outcome:
                            raise ValueError("Conflicting DCSS harness outcomes")
                        self.outcome = outcome
                    elif message.get("msg") == "harness_score":
                        required = ("score", "game_turn", "final", "game_time")
                        if any(field not in message for field in required):
                            raise ValueError("Invalid DCSS harness score message")
                        score = message.get("score")
                        game_turn = message.get("game_turn")
                        final = message.get("final")
                        game_time = message["game_time"]
                        if game_time is not None and (type(game_time) is not int or game_time < 0):
                            raise ValueError("Invalid DCSS elapsed game time")
                        if score is not None and (isinstance(score, bool) or
                                                  not isinstance(score, int) or score < 0):
                            raise ValueError("Invalid DCSS harness score")
                        if (isinstance(game_turn, bool) or not isinstance(game_turn, int) or
                                game_turn < 0):
                            raise ValueError("Invalid DCSS harness game turn")
                        if not isinstance(final, bool):
                            raise ValueError("Invalid DCSS harness score final flag")
                        if final and score is None:
                            raise ValueError("Final DCSS harness score must be an integer")
                        # Postmortem input prompts still emit live heartbeats;
                        # they must not replace the official final score.
                        if not self._final_score_received:
                            scores.append((score, game_turn, final, game_time))
                        self._final_score_received |= final
                    else:
                        messages.append(message)
            if self.on_score:
                for score, game_turn, final, game_time in scores:
                    self.on_score(score, game_turn, final, game_time)
            if messages and self.on_tiles:
                self.on_tiles(tuple(messages))
        except Exception as exc:
            self._failed = True
            asyncio.get_running_loop().remove_reader(self._socket.fileno())
            self._queue.put_nowait(exc)

    async def _watch_exit(self) -> None:
        code = await self._process.wait()
        if self._closed:
            return
        self._read_terminal()
        self._read_tiles()
        self._decoder.finish()
        if code:
            self._queue.put_nowait(RuntimeError(f"DCSS exited with status {code}; see {self.save_dir / 'process.log'}"))
        else:
            self._id += 1
            self._queue.put_nowait(replace(
                self._terminal.observation(self._id, ended=True), outcome=self.outcome))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process and self._process.returncode is None:
            try:
                os.killpg(self._process.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._process.wait(), 3)
            except TimeoutError:
                try:
                    os.killpg(self._process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await self._process.wait()
        if self._exit_task:
            await self._exit_task
        loop = asyncio.get_running_loop()
        if self._master is not None:
            loop.remove_reader(self._master)
            os.close(self._master)
            self._master = None
        if self._socket:
            loop.remove_reader(self._socket.fileno())
            self._socket.close()
            self._socket = None
        if self._log:
            self._log.close()
        if self._socket_dir:
            self._socket_dir.cleanup()
