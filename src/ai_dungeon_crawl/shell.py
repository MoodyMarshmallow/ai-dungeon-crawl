import asyncio
import codecs
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

from .contracts import ExecutionResult, GameAction, validate_source, StopExecution
from .events import emit_output
from .observation_json import observation_data


class ShellTerminal:
    """Fresh shells in a persistent workspace with a parent-owned game broker.

    Seatbelt enforces filesystem/network isolation. Resource limits apply per
    process/file, not as aggregate disk or memory quotas. macOS only, fail closed.
    """
    def __init__(self, *, timeout_seconds=5, max_output_chars=32768, manual_path=None):
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError('timeout_seconds must be finite and positive')
        if not 0 <= max_output_chars <= 65536:
            raise ValueError('max_output_chars must be between zero and 65536')
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars
        self.manual_path = Path(manual_path).resolve() if manual_path else None
        self._directory = None
        self._process = None
        self._closed = False
        self._busy = False
        self._server = None
        self._socket = None
        self._clients = set()

    def _prepare(self):
        if self._closed:
            raise RuntimeError('Shell terminal is closed')
        sandbox = shutil.which('sandbox-exec')
        if sys.platform != 'darwin' or sandbox is None:
            raise RuntimeError('ShellTerminal requires macOS sandbox-exec; no unsafe fallback')
        if self._directory is None:
            self._directory = tempfile.TemporaryDirectory(prefix='crawl-shell-')
        root = Path(self._directory.name).resolve()
        directory = root / 'work'
        directory.mkdir(exist_ok=True)
        if self.manual_path and not (root / 'docs').exists():
            from .manual import prepare_manual
            provision = prepare_manual(self.manual_path, root / 'docs')
            self.manual_path = provision.manual_path
            for document in (root / 'docs').iterdir():
                (directory / document.name).symlink_to(document)
        executable = str(Path(sys.executable).resolve())
        runtime = str(Path(sys.base_prefix).resolve())
        client = str(Path(__file__).with_name('_shell_client.py').resolve())
        launcher = str(Path(__file__).with_name('_shell_launch.py').resolve())
        bindir = root / 'bin'
        bindir.mkdir(exist_ok=True)
        # These are conveniences, never security boundaries: the caller can
        # bypass the CLI, so the broker treats all requests as untrusted.
        import shlex
        (bindir / 'crawl').write_text('#!/bin/bash\nexec ' + shlex.quote(executable) +
                                    ' -I -S -B ' + shlex.quote(client) + ' "$@"\n')
        (bindir / 'crawl').chmod(0o700)
        for name in ('python', 'python3'):
            target = bindir / name
            if not target.exists():
                target.symlink_to(executable)
        rg = shutil.which('rg')
        rg = str(Path(rg).resolve()) if rg else None
        if rg and not (bindir / 'rg').exists():
            (bindir / 'rg').symlink_to(rg)
        self.workspace = directory
        self._socket = str(root / 'broker.sock')
        def literal(path):
            return '(literal ' + json.dumps(str(path)) + ')'
        def subpath(path):
            return '(subpath ' + json.dumps(str(path)) + ')'
        reads = [literal('/'), subpath(directory), subpath(bindir), literal(executable), literal(client),
                 literal(launcher), subpath(runtime), subpath('/System'), subpath('/usr/lib'),
                 subpath('/usr/share/locale'), subpath('/bin'), subpath('/usr/bin'),
                 literal('/dev/null'), literal('/dev/urandom')]
        if self.manual_path:
            reads.append(subpath(root / 'docs'))
        if rg:
            reads.append(literal(rg))
            libraries = subprocess.check_output(['/usr/bin/otool', '-L', rg], text=True)
            for line in libraries.splitlines()[1:]:
                library = line.strip().split(' (', 1)[0]
                if library.startswith('/'):
                    reads.append(literal(Path(library).resolve()))
        profile = '\n'.join([
            '(version 1)', '(deny default)', '(allow file-read-metadata)',
            '(allow file-read-data ' + ' '.join(reads) + ')',
            '(allow file-write* ' + subpath(directory) + ' ' + literal('/dev/null') + ')',
            '(allow process-exec)', '(allow process-fork)',
            # Prevent children escaping the process group used for cleanup.
            '(deny process-info-setcontrol)',
            # posix_spawn attributes can create a session/group without issuing
            # setsid/setpgid, so require ordinary fork+exec for subprocesses.
            '(deny syscall-unix (syscall-number SYS_setsid SYS_setpgid SYS_posix_spawn))',
            '(allow signal (target children))', '(allow sysctl-read)',
            '(allow network-outbound ' + literal(self._socket) + ')',
        ])
        environment = {'PATH': str(bindir) + ':/usr/bin:/bin', 'LANG': 'en_US.UTF-8',
                       'HOME': str(directory), 'TMPDIR': str(directory),
                       'CRAWL_SOCKET': self._socket}
        if self.manual_path:
            environment['CRAWL_MANUAL'] = str(self.manual_path)
        return sandbox, profile, executable, launcher, environment

    async def execute_shell(self, code, observation, press):
        validate_source(code)
        if '\0' in code:
            raise ValueError('Shell source cannot contain NUL')
        if self._busy:
            raise RuntimeError('Concurrent shell executions are not supported')
        self._busy = True
        output = ''
        truncated = False
        loop = asyncio.get_running_loop()
        fatal = loop.create_future()
        lock = asyncio.Lock()
        deadline = loop.time() + self.timeout_seconds
        in_action = False
        accepting = True

        async def serve(reader, writer):
            nonlocal observation, deadline, in_action
            task = asyncio.current_task()
            self._clients.add(task)
            response = None
            try:
                async with lock:
                    request = json.loads(await asyncio.wait_for(reader.readline(), 1))
                    if not isinstance(request, dict):
                        raise ValueError('Request must be an object')
                    if fatal.done() or not accepting:
                        return
                    if request.get('type') == 'press':
                        key = request.get('key')
                        if not isinstance(key, str) or len(key) > 16:
                            raise ValueError('Invalid game key')
                        action = GameAction(key)
                        started = loop.time()
                        in_action = True
                        try:
                            observation = await press(action)
                        except BaseException as exc:
                            if not fatal.done():
                                fatal.set_result(exc)
                            return
                        finally:
                            deadline += loop.time() - started
                            in_action = False
                        response = {}
                    elif request.get('type') == 'observe':
                        data = observation_data(observation)
                        response = {'observation': data}
                    else:
                        raise ValueError('Unknown game request')
            except (ValueError, asyncio.TimeoutError) as exc:
                response = {'error': str(exc)[:200]}
            except (ConnectionError, asyncio.CancelledError):
                return
            finally:
                if response is not None:
                    try:
                        writer.write((json.dumps(response) + '\n').encode())
                        await writer.drain()
                    except ConnectionError:
                        pass
                writer.close()
                self._clients.discard(task)

        async def collect(stream, name):
            nonlocal output, truncated
            decoder = codecs.getincrementaldecoder('utf-8')('replace')
            while True:
                chunk = await stream.read(4096)
                text = decoder.decode(chunk, final=not chunk)
                space = self.max_output_chars - len(output)
                output += text[:space]
                truncated |= len(text) > space
                emit_output(text, text[:space], name)
                if not chunk:
                    break

        readers = []
        status, error = 'ok', None
        try:
            sandbox, profile, executable, launcher, environment = self._prepare()
            # macOS NPROC counts all processes owned by this user. Reserve a
            # bounded allowance above existing host processes, not a fixed cap
            # that may already be exhausted on an ordinary desktop.
            existing = subprocess.check_output(['/bin/ps', '-U', str(os.getuid()), '-o', 'pid='])
            process_limit = len(existing.splitlines()) + 64
            self._server = await asyncio.start_unix_server(serve, path=self._socket, limit=1024)
            self._process = await asyncio.create_subprocess_exec(
                sandbox, '-p', profile, executable, '-I', '-S', '-B', launcher, code, str(process_limit),
                cwd=self.workspace, env=environment, start_new_session=True,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            readers = [asyncio.create_task(collect(stream, name))
                       for stream, name in ((self._process.stdout, 'stdout'),
                                            (self._process.stderr, 'stderr'))]
            deadline = loop.time() + self.timeout_seconds
            while self._process.returncode is None or in_action:
                if fatal.done():
                    raise fatal.result()
                if not in_action and loop.time() >= deadline:
                    status, error = 'timeout', 'Shell execution timed out'
                    break
                await asyncio.sleep(0.01)
            if fatal.done():
                raise fatal.result()
            if status == 'ok' and self._process.returncode:
                status, error = 'error', f'Shell exited with status {self._process.returncode}'
        except StopExecution as exc:
            status, error = 'stopped', str(exc)
        except BaseException:
            self._closed = True
            raise
        finally:
            accepting = False
            await self._finish_submission()
            if readers:
                await asyncio.gather(*readers)
            self._busy = False
            # A timeout kills this submission, not the persistent workspace.
            # The next turn receives its partial output and latest observation.
            if status == 'stopped' or self._closed:
                await self.close()
        return ExecutionResult(observation, output, error, status, truncated)

    async def _finish_submission(self):
        server = self._server
        if self._server:
            self._server.close()
            self._server = None
        if self._process:
            try:
                os.killpg(self._process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await self._process.wait()
            self._process = None
        for task in tuple(self._clients):
            task.cancel()
        await asyncio.gather(*self._clients, return_exceptions=True)
        self._clients.clear()
        if server:
            await server.wait_closed()
        if self._directory and self._socket:
            Path(self._socket).unlink(missing_ok=True)

    async def close(self):
        self._closed = True
        await self._finish_submission()
        if self._directory:
            self._directory.cleanup()
            self._directory = None
